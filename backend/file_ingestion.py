"""LIM file ingestion monitoring API for KUMO Monitor.

Migrated from the Streamlit File Ingestion Monitor. The overview endpoint keeps
initial page loads small; detail endpoints load RAW / READY / history rows only
when a user opens a detail dialog.
"""

import json
import os
import re
from datetime import date

from flask import Blueprint, current_app, jsonify, request
from snowflake.connector import DictCursor

import config
import snowflake_client as sf
from utils import normalize_rows


file_ingestion_bp = Blueprint("file_ingestion", __name__)

ADMIN_PKG_GROUP_SOURCE = f"{config.DB}.{config.SCHEMA}.RAW_LIM_PKG_GROUP_SOURCE"
RAW_LIM_META_TABLE = "KUMO_TST.RAW_LIM.RAW_LIM_META"
SET_READY_LATEST_TABLE = "KUMO_TST.RAW_LIM._TMP_SET_READY_LOG"
SET_READY_HISTORY_TABLE = "KUMO_TST.RAW_LIM._SET_READY_LOG"
SUBJECT_AREA_COLUMN = "SUBJECT_AREA"
DEFAULT_HISTORY_DAYS = 30
MAX_HISTORY_DAYS = 365
LIM_DATABASE = os.getenv("KUMO_LIM_DATABASE", "KUMO_TST")
LIM_ROLE = os.getenv("KUMO_LIM_ROLE", "KUMO_ADMIN")
LIM_FORMAT_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,49}$")


def _json_error(error, status=500):
    return jsonify({"ok": False, "error": str(error or "Unexpected error")}), status


def _identifier(value, label):
    value = str(value or "").strip().upper()
    if not LIM_FORMAT_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid {label}.")
    return value


def _procedure_result(row):
    if not row:
        return {}
    normalized = {str(key).upper(): value for key, value in dict(row).items()}
    value = next(iter(normalized.values()), {})
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {"MESSAGE": value}
    return value if isinstance(value, dict) else {"RESULT": value}


def _history_days():
    raw = request.args.get("historyDays", DEFAULT_HISTORY_DAYS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_HISTORY_DAYS
    return max(1, min(value, MAX_HISTORY_DAYS))


def _source_id():
    value = request.args.get("sourceId")
    return _identifier(value, "source ID") if value else None


def _filter_source(rows, source_id):
    if not source_id:
        return rows
    return [
        row for row in rows
        if str(row.get("DLVY_SOURCE_ID") or "").strip().upper() == source_id
    ]


def _num(value):
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _status_kind(row):
    latest_attention = _num(row.get("LATEST_ATTENTION_ROWS"))
    latest_updated = _num(row.get("LATEST_UPDATED_ROWS"))
    latest_rows = _num(row.get("LATEST_LOG_ROWS"))

    file_rows = _num(row.get("FILE_ROWS"))
    ready_rows = _num(row.get("READY_ROWS"))
    missing_rows = _num(row.get("MISSING_ROWS"))
    rowcount_bad_rows = _num(row.get("ROWCOUNT_BAD_ROWS"))

    if latest_attention > 0:
        return "ATTENTION"
    if rowcount_bad_rows > 0:
        return "ROWCOUNT_ISSUE"
    if missing_rows > 0:
        return "MISSING_FILES"
    if file_rows > 0 and ready_rows == file_rows:
        return "READY"
    if latest_updated > 0:
        return "UPDATED"
    if latest_rows > 0:
        return "LATEST_NO_UPDATE"
    if file_rows > 0:
        return "WAITING"
    return "NO_DATA"


def _status_label(kind):
    return {
        "ATTENTION": "Attention",
        "ROWCOUNT_ISSUE": "Rowcount issue",
        "MISSING_FILES": "Missing files",
        "UPDATED": "Updated",
        "READY": "Ready",
        "LATEST_NO_UPDATE": "Checked",
        "WAITING": "Waiting",
        "NO_DATA": "No data",
    }.get(kind, kind)


def _status_sort(kind):
    return {
        "ATTENTION": 1,
        "ROWCOUNT_ISSUE": 2,
        "MISSING_FILES": 3,
        "WAITING": 4,
        "LATEST_NO_UPDATE": 5,
        "UPDATED": 6,
        "READY": 7,
        "NO_DATA": 8,
    }.get(kind, 99)


def _load_catalog(cur):
    try:
        cur.execute(
            f"""
            SELECT DISTINCT
                   COALESCE({SUBJECT_AREA_COLUMN}, 'Unknown subject area') AS SUBJECT_AREA,
                   PKG_GROUP_NAME
            FROM {ADMIN_PKG_GROUP_SOURCE}
            WHERE ACTIVE_FL = TRUE
              AND PKG_GROUP_NAME IS NOT NULL
            ORDER BY SUBJECT_AREA, PKG_GROUP_NAME
            """
        )
        return normalize_rows(cur.fetchall())
    except Exception:
        # Keep parity with the Streamlit implementation: environments where the
        # configured subject-area column is absent still remain usable.
        cur.execute(
            f"""
            SELECT DISTINCT
                   'Unknown subject area' AS SUBJECT_AREA,
                   PKG_GROUP_NAME
            FROM {ADMIN_PKG_GROUP_SOURCE}
            WHERE ACTIVE_FL = TRUE
              AND PKG_GROUP_NAME IS NOT NULL
            ORDER BY PKG_GROUP_NAME
            """
        )
        return normalize_rows(cur.fetchall())


def _load_source_catalog(cur):
    cur.execute(
        f"""
        SELECT DISTINCT
               COALESCE({SUBJECT_AREA_COLUMN}, 'Unknown subject area') AS SUBJECT_AREA,
               PKG_GROUP_NAME,
               UPPER(SOURCE_ID) AS SOURCE_ID
        FROM {ADMIN_PKG_GROUP_SOURCE}
        WHERE ACTIVE_FL = TRUE
          AND PKG_GROUP_NAME IS NOT NULL
          AND SOURCE_ID IS NOT NULL
        ORDER BY SUBJECT_AREA, PKG_GROUP_NAME, SOURCE_ID
        """
    )
    return normalize_rows(cur.fetchall())


def _group_params(groups):
    params = {f"g{i}": name for i, name in enumerate(groups)}
    placeholders = ", ".join(f"%({key})s" for key in params)
    return placeholders, params


def _build_overview(catalog, meta_rows, latest_rows, history_rows,
                    raw_readiness_rows=None, source_catalog=None, source_meta_rows=None,
                    source_latest_rows=None):
    meta = {str(row.get("PKG_GROUP_NAME")): row for row in meta_rows}
    latest = {str(row.get("PKG_GROUP_NAME")): row for row in latest_rows}
    history = {str(row.get("PKG_GROUP_NAME")): row for row in history_rows}
    raw_readiness = {
        str(row.get("PKG_GROUP_NAME")): row for row in (raw_readiness_rows or [])
        if not row.get("SOURCE_ID")
    }

    numeric_cols = [
        "FILE_ROWS",
        "RECEIVED_ROWS",
        "MISSING_ROWS",
        "READY_ROWS",
        "NOT_READY_ROWS",
        "ROWCOUNT_OK_ROWS",
        "ROWCOUNT_BAD_ROWS",
        "LATEST_LOG_ROWS",
        "LATEST_UPDATED_ROWS",
        "LATEST_ATTENTION_ROWS",
        "HISTORY_ROWS",
        "HISTORY_DAYS",
        "HISTORY_ATTENTION_ROWS",
        "HISTORY_UPDATED_ROWS",
    ]

    overview = []
    for catalog_row in catalog:
        group = str(catalog_row.get("PKG_GROUP_NAME") or "")
        row = {
            "SUBJECT_AREA": catalog_row.get("SUBJECT_AREA") or "Unknown subject area",
            "PKG_GROUP_NAME": group,
            **meta.get(group, {}),
            **latest.get(group, {}),
            **history.get(group, {}),
        }

        for col in numeric_cols:
            row[col] = _num(row.get(col))

        live_raw = raw_readiness.get(group, {})
        raw_file_count = _num(live_raw.get("RAW_FILE_COUNT"))
        row["FILE_ROWS"] = raw_file_count
        row["RECEIVED_ROWS"] = raw_file_count
        row["READY_ROWS"] = _num(live_raw.get("RAW_READY_FILES"))
        row["NOT_READY_ROWS"] = _num(live_raw.get("RAW_NOT_READY_FILES"))
        row["RAW_FILE_COUNT"] = raw_file_count
        row["RAW_LATEST_LOAD_DTTM"] = live_raw.get("RAW_LATEST_LOAD_DTTM")

        row["sources"] = _build_source_rows(
            group, source_catalog or [], source_meta_rows or [],
            source_latest_rows or [], raw_readiness_rows or []
        )
        if row["sources"]:
            for column in ("FILE_ROWS", "RECEIVED_ROWS", "MISSING_ROWS", "READY_ROWS",
                           "NOT_READY_ROWS", "ROWCOUNT_OK_ROWS", "ROWCOUNT_BAD_ROWS"):
                row[column] = sum(_num(source.get(column)) for source in row["sources"])
        kind = _status_kind(row)
        if any(source.get("STATUS_KIND") == "ATTENTION" for source in row["sources"]):
            kind = "ATTENTION"
        row["STATUS_KIND"] = kind
        row["STATUS_LABEL"] = _status_label(kind)
        row["STATUS_SORT"] = _status_sort(kind)
        overview.append(row)

    overview.sort(
        key=lambda row: (
            str(row.get("SUBJECT_AREA") or "").lower(),
            _num(row.get("STATUS_SORT")),
            str(row.get("PKG_GROUP_NAME") or "").lower(),
        )
    )
    return overview


def _build_source_rows(group, source_catalog, source_meta_rows, source_latest_rows, raw_readiness_rows):
    keys = lambda rows: {
        (str(row.get("PKG_GROUP_NAME")), str(row.get("SOURCE_ID") or "").upper()): row
        for row in rows if row.get("SOURCE_ID")
    }
    meta = keys(source_meta_rows)
    latest = keys(source_latest_rows)
    live = keys(raw_readiness_rows)
    sources = []
    candidates = {
        str(item.get("SOURCE_ID") or "").upper(): {**item, "IS_CONFIGURED": True}
        for item in source_catalog if str(item.get("PKG_GROUP_NAME")) == group
    }
    for item in raw_readiness_rows:
        if str(item.get("PKG_GROUP_NAME")) == group and item.get("SOURCE_ID"):
            candidates.setdefault(str(item.get("SOURCE_ID")).upper(), {
                "PKG_GROUP_NAME": group,
                "SOURCE_ID": item.get("SOURCE_ID"),
                "IS_CONFIGURED": False,
            })
    for item in candidates.values():
        source_id = str(item.get("SOURCE_ID") or "").upper()
        key = (group, source_id)
        row = {
            "SUBJECT_AREA": item.get("SUBJECT_AREA"),
            "PKG_GROUP_NAME": group,
            "SOURCE_ID": source_id,
            "IS_CONFIGURED": bool(item.get("IS_CONFIGURED")),
            **meta.get(key, {}),
            **latest.get(key, {}),
        }
        for column in ("FILE_ROWS", "RECEIVED_ROWS", "MISSING_ROWS", "READY_ROWS",
                       "NOT_READY_ROWS", "ROWCOUNT_OK_ROWS", "ROWCOUNT_BAD_ROWS",
                       "LATEST_LOG_ROWS", "LATEST_UPDATED_ROWS", "LATEST_ATTENTION_ROWS"):
            row[column] = _num(row.get(column))
        raw = live.get(key, {})
        raw_files = _num(raw.get("RAW_FILE_COUNT"))
        row["FILE_ROWS"] = raw_files
        row["RECEIVED_ROWS"] = raw_files
        row["READY_ROWS"] = _num(raw.get("RAW_READY_FILES"))
        row["NOT_READY_ROWS"] = _num(raw.get("RAW_NOT_READY_FILES"))
        row["RAW_LATEST_LOAD_DTTM"] = raw.get("RAW_LATEST_LOAD_DTTM")
        kind = _status_kind(row)
        if not row["IS_CONFIGURED"]:
            kind = "ATTENTION"
        row["STATUS_KIND"] = kind
        row["STATUS_LABEL"] = (
            _status_label(kind) if row["IS_CONFIGURED"] else "Unconfigured source"
        )
        row["STATUS_SORT"] = _status_sort(kind)
        sources.append(row)
    return sorted(sources, key=lambda row: str(row.get("SOURCE_ID")))


def _build_summary(overview):
    total_groups = len(overview)
    attention_groups = sum(
        1 for row in overview if row.get("STATUS_KIND") in ("ATTENTION", "ROWCOUNT_ISSUE")
    )
    missing_groups = sum(1 for row in overview if row.get("STATUS_KIND") == "MISSING_FILES")
    updated_groups = sum(1 for row in overview if row.get("STATUS_KIND") == "UPDATED")
    ready_groups = sum(1 for row in overview if row.get("STATUS_KIND") == "READY")
    waiting_groups = sum(
        1 for row in overview if row.get("STATUS_KIND") in ("WAITING", "LATEST_NO_UPDATE")
    )

    total_files = sum(_num(row.get("FILE_ROWS")) for row in overview)
    ready_files = sum(_num(row.get("READY_ROWS")) for row in overview)
    missing_files = sum(_num(row.get("MISSING_ROWS")) for row in overview)
    readiness_pct = round((ready_files / total_files) * 100) if total_files else 0

    if attention_groups:
        engine_status = "ATTENTION"
    elif missing_groups:
        engine_status = "MISSING"
    else:
        engine_status = "READY"

    return {
        "engineStatus": engine_status,
        "totalGroups": total_groups,
        "attentionGroups": attention_groups,
        "missingGroups": missing_groups,
        "updatedGroups": updated_groups,
        "readyGroups": ready_groups,
        "waitingGroups": waiting_groups,
        "totalFiles": total_files,
        "readyFiles": ready_files,
        "missingFiles": missing_files,
        "readinessPct": readiness_pct,
    }


def _load_raw_table_readiness(cur, catalog):
    database = _identifier(LIM_DATABASE, "LIM database")
    subjects = sorted({
        _identifier(row.get("SUBJECT_AREA"), "LIM subject area")
        for row in catalog
        if row.get("SUBJECT_AREA") and str(row.get("SUBJECT_AREA")).upper() != "UNKNOWN SUBJECT AREA"
    })
    if not subjects:
        return []

    branches = []
    for subject in subjects:
        branches.append(
            f"""
            SELECT '{subject}' AS SUBJECT_AREA,
                   SUBSTR(DW_FILE_NM, 1, POSITION('{subject}' IN UPPER(DW_FILE_NM)) - 1) AS SOURCE_ID,
                   DW_FILE_NM,
                   IFF(COUNT_IF(COALESCE(DW_READY_TO_LOAD_FL, FALSE) = FALSE) = 0, TRUE, FALSE) AS FILE_READY,
                   MAX(DW_LOAD_DTTM) AS FILE_LOAD_DTTM
            FROM {database}.RAW_LIM.RAW_LIM_{subject}
            WHERE DW_FILE_NM IS NOT NULL
              AND POSITION('{subject}' IN UPPER(DW_FILE_NM)) > 1
              AND SUBSTR(DW_FILE_NM, POSITION('{subject}' IN UPPER(DW_FILE_NM)) + {len(subject)}, 3) <> '000'
            GROUP BY DW_FILE_NM
            """
        )

    cur.execute(
        f"""
        WITH RAW_FILES AS (
          {' UNION ALL '.join(branches)}
        ),
        ACTIVE_SOURCE_MAP AS (
          SELECT DISTINCT
                 UPPER(SUBJECT_AREA) AS SUBJECT_AREA,
                 UPPER(SOURCE_ID) AS SOURCE_ID,
                 PKG_GROUP_NAME
          FROM {ADMIN_PKG_GROUP_SOURCE}
          WHERE ACTIVE_FL = TRUE
        ),
        SUBJECT_GROUP_MAP AS (
          SELECT UPPER(SUBJECT_AREA) AS SUBJECT_AREA,
                 MIN(PKG_GROUP_NAME) AS PKG_GROUP_NAME,
                 COUNT(DISTINCT PKG_GROUP_NAME) AS GROUP_COUNT
          FROM {ADMIN_PKG_GROUP_SOURCE}
          WHERE ACTIVE_FL = TRUE
          GROUP BY UPPER(SUBJECT_AREA)
        ),
        MAPPED_RAW_FILES AS (
          SELECT COALESCE(exact.PKG_GROUP_NAME, subject_map.PKG_GROUP_NAME) AS PKG_GROUP_NAME,
                 r.SOURCE_ID,
                 r.DW_FILE_NM,
                 r.FILE_READY,
                 r.FILE_LOAD_DTTM
          FROM RAW_FILES r
          LEFT JOIN ACTIVE_SOURCE_MAP exact
            ON exact.SUBJECT_AREA = r.SUBJECT_AREA
           AND exact.SOURCE_ID = UPPER(r.SOURCE_ID)
          JOIN SUBJECT_GROUP_MAP subject_map
            ON subject_map.SUBJECT_AREA = r.SUBJECT_AREA
          WHERE exact.PKG_GROUP_NAME IS NOT NULL OR subject_map.GROUP_COUNT = 1
        )
        SELECT m.PKG_GROUP_NAME,
               IFF(GROUPING(r.SOURCE_ID) = 1, NULL, r.SOURCE_ID) AS SOURCE_ID,
               COUNT(DISTINCT r.DW_FILE_NM) AS RAW_FILE_COUNT,
               COUNT(DISTINCT IFF(r.FILE_READY, r.DW_FILE_NM, NULL)) AS RAW_READY_FILES,
               COUNT(DISTINCT IFF(NOT r.FILE_READY, r.DW_FILE_NM, NULL)) AS RAW_NOT_READY_FILES,
               MAX(r.FILE_LOAD_DTTM) AS RAW_LATEST_LOAD_DTTM
        FROM MAPPED_RAW_FILES r
        JOIN (SELECT DISTINCT PKG_GROUP_NAME FROM {ADMIN_PKG_GROUP_SOURCE} WHERE ACTIVE_FL = TRUE) m
          ON m.PKG_GROUP_NAME = r.PKG_GROUP_NAME
        GROUP BY GROUPING SETS ((m.PKG_GROUP_NAME), (m.PKG_GROUP_NAME, r.SOURCE_ID))
        """
    )
    return normalize_rows(cur.fetchall())


def _load_overview(history_days):
    with sf.connection(use_warehouse=True, include_context=True, force_service=True) as conn:
        cur = conn.cursor(DictCursor)
        try:
            catalog = _load_catalog(cur)
            source_catalog = _load_source_catalog(cur)
            groups = [str(row.get("PKG_GROUP_NAME")) for row in catalog if row.get("PKG_GROUP_NAME")]
            if not groups:
                return {"overview": [], "summary": _build_summary([]), "subjectAreas": 0}

            placeholders, params = _group_params(groups)

            cur.execute(
                f"""
                SELECT PKG_GROUP_NAME,
                       COUNT_IF(UPPER(COALESCE(FILE_TYPE, '')) <> 'CONTROL') AS FILE_ROWS,
                       COUNT_IF(UPPER(COALESCE(FILE_TYPE, '')) <> 'CONTROL' AND RECEIVED_FL = TRUE) AS RECEIVED_ROWS,
                       COUNT_IF(UPPER(COALESCE(FILE_TYPE, '')) <> 'CONTROL' AND RECEIVED_FL = FALSE) AS MISSING_ROWS,
                       COUNT_IF(UPPER(COALESCE(FILE_TYPE, '')) <> 'CONTROL' AND DW_READY_TO_LOAD_FL = TRUE) AS READY_ROWS,
                       COUNT_IF(UPPER(COALESCE(FILE_TYPE, '')) <> 'CONTROL' AND DW_READY_TO_LOAD_FL = FALSE) AS NOT_READY_ROWS,
                       COUNT_IF(UPPER(COALESCE(FILE_TYPE, '')) <> 'CONTROL' AND ROWCOUNT_STATUS = 'ROWCOUNT_OK') AS ROWCOUNT_OK_ROWS,
                       COUNT_IF(UPPER(COALESCE(FILE_TYPE, '')) <> 'CONTROL' AND ROWCOUNT_STATUS IS NOT NULL AND ROWCOUNT_STATUS <> 'ROWCOUNT_OK') AS ROWCOUNT_BAD_ROWS,
                       MAX(DLVY_END_DATE) AS LATEST_DLVY_END_DATE,
                       MAX(LOADED_AT) AS LAST_LOADED_AT
                FROM {RAW_LIM_META_TABLE}
                WHERE PKG_GROUP_NAME IN ({placeholders})
                GROUP BY PKG_GROUP_NAME
                """,
                params,
            )
            meta_rows = normalize_rows(cur.fetchall())

            cur.execute(
                f"""
                SELECT PKG_GROUP_NAME,
                       UPPER(DLVY_SOURCE_ID) AS SOURCE_ID,
                       COUNT_IF(UPPER(COALESCE(FILE_TYPE, '')) <> 'CONTROL') AS FILE_ROWS,
                       COUNT_IF(UPPER(COALESCE(FILE_TYPE, '')) <> 'CONTROL' AND RECEIVED_FL = TRUE) AS RECEIVED_ROWS,
                       COUNT_IF(UPPER(COALESCE(FILE_TYPE, '')) <> 'CONTROL' AND RECEIVED_FL = FALSE) AS MISSING_ROWS,
                       COUNT_IF(UPPER(COALESCE(FILE_TYPE, '')) <> 'CONTROL' AND DW_READY_TO_LOAD_FL = TRUE) AS READY_ROWS,
                       COUNT_IF(UPPER(COALESCE(FILE_TYPE, '')) <> 'CONTROL' AND DW_READY_TO_LOAD_FL = FALSE) AS NOT_READY_ROWS,
                       COUNT_IF(UPPER(COALESCE(FILE_TYPE, '')) <> 'CONTROL' AND ROWCOUNT_STATUS = 'ROWCOUNT_OK') AS ROWCOUNT_OK_ROWS,
                       COUNT_IF(UPPER(COALESCE(FILE_TYPE, '')) <> 'CONTROL' AND ROWCOUNT_STATUS IS NOT NULL AND ROWCOUNT_STATUS <> 'ROWCOUNT_OK') AS ROWCOUNT_BAD_ROWS,
                       MAX(DLVY_END_DATE) AS LATEST_DLVY_END_DATE,
                       MAX(LOADED_AT) AS LAST_LOADED_AT
                FROM {RAW_LIM_META_TABLE}
                WHERE PKG_GROUP_NAME IN ({placeholders})
                  AND DLVY_SOURCE_ID IS NOT NULL
                GROUP BY PKG_GROUP_NAME, UPPER(DLVY_SOURCE_ID)
                """,
                params,
            )
            source_meta_rows = normalize_rows(cur.fetchall())

            try:
                raw_readiness_rows = _load_raw_table_readiness(cur, catalog)
            except Exception:
                current_app.logger.exception("Failed to load readiness from RAW LIM tables")
                raw_readiness_rows = []

            try:
                cur.execute(
                    f"""
                    SELECT PKG_GROUP_NAME,
                           COUNT(*) AS LATEST_LOG_ROWS,
                           COUNT_IF(STATUS = 'UPDATED') AS LATEST_UPDATED_ROWS,
                           COUNT_IF(STATUS IN ('STOPPED', 'FAILED', 'SKIPPED', 'BLOCKED')) AS LATEST_ATTENTION_ROWS,
                           LISTAGG(DISTINCT STATUS, ', ') WITHIN GROUP (ORDER BY STATUS) AS LATEST_STATUS_LIST,
                           MAX(CONTROL_DATE) AS LATEST_CONTROL_DATE
                    FROM {SET_READY_LATEST_TABLE}
                    WHERE PKG_GROUP_NAME IN ({placeholders})
                    GROUP BY PKG_GROUP_NAME
                    """,
                    params,
                )
                latest_rows = normalize_rows(cur.fetchall())
            except Exception:
                latest_rows = []

            try:
                cur.execute(
                    f"""
                    SELECT PKG_GROUP_NAME,
                           UPPER(DLVY_SOURCE_ID) AS SOURCE_ID,
                           COUNT(*) AS LATEST_LOG_ROWS,
                           COUNT_IF(STATUS = 'UPDATED') AS LATEST_UPDATED_ROWS,
                           COUNT_IF(STATUS IN ('STOPPED', 'FAILED', 'SKIPPED', 'BLOCKED')) AS LATEST_ATTENTION_ROWS,
                           LISTAGG(DISTINCT STATUS, ', ') WITHIN GROUP (ORDER BY STATUS) AS LATEST_STATUS_LIST,
                           MAX(CONTROL_DATE) AS LATEST_CONTROL_DATE
                    FROM {SET_READY_LATEST_TABLE}
                    WHERE PKG_GROUP_NAME IN ({placeholders})
                      AND DLVY_SOURCE_ID IS NOT NULL
                    GROUP BY PKG_GROUP_NAME, UPPER(DLVY_SOURCE_ID)
                    """,
                    params,
                )
                source_latest_rows = normalize_rows(cur.fetchall())
            except Exception:
                source_latest_rows = []

            try:
                history_params = dict(params)
                history_params["history_days"] = int(history_days)
                cur.execute(
                    f"""
                    SELECT PKG_GROUP_NAME,
                           COUNT(*) AS HISTORY_ROWS,
                           COUNT(DISTINCT TO_DATE(CONTROL_DATE)) AS HISTORY_DAYS,
                           COUNT_IF(STATUS = 'UPDATED') AS HISTORY_UPDATED_ROWS,
                           COUNT_IF(STATUS IN ('STOPPED', 'FAILED', 'SKIPPED', 'BLOCKED')) AS HISTORY_ATTENTION_ROWS,
                           MAX(CONTROL_DATE) AS HISTORY_LAST_CONTROL_DATE
                    FROM {SET_READY_HISTORY_TABLE}
                    WHERE PKG_GROUP_NAME IN ({placeholders})
                      AND CONTROL_DATE >= DATEADD(day, -%(history_days)s, CURRENT_TIMESTAMP())
                    GROUP BY PKG_GROUP_NAME
                    """,
                    history_params,
                )
                history_rows = normalize_rows(cur.fetchall())
            except Exception:
                history_rows = []
        finally:
            cur.close()

    overview = _build_overview(
        catalog, meta_rows, latest_rows, history_rows, raw_readiness_rows,
        source_catalog, source_meta_rows, source_latest_rows
    )
    subject_areas = len({str(row.get("SUBJECT_AREA") or "Unknown subject area") for row in catalog})
    return {
        "overview": overview,
        "summary": _build_summary(overview),
        "subjectAreas": subject_areas,
    }


def _load_raw_detail(group_name):
    meta_rows = sf.query_service(
        f"""
        SELECT RAW_TABLE,
               FILE_NAME,
               FILE_TYPE,
               PKG_GROUP_NAME,
               DLVY_SOURCE_ID,
               DLVY_PKG_ID,
               DLVY_PKG_YEAR,
               DLVY_PKG_YEAR_SEQ_NO,
               DLVY_END_DATE,
               READY_STATUS,
               ROWCOUNT_STATUS,
               RECEIVED_FL,
               DW_READY_TO_LOAD_FL,
               IS_VALID_SEQUENCE,
               ALL_SOURCES_FL,
               EXPECTED_ROWS,
               ACTUAL_ROWS,
               LOADED_AT
        FROM {RAW_LIM_META_TABLE}
        WHERE PKG_GROUP_NAME = %(group_name)s
          AND UPPER(COALESCE(FILE_TYPE, '')) <> 'CONTROL'
        ORDER BY DLVY_END_DATE DESC, DLVY_SOURCE_ID, FILE_NAME
        """,
        params={"group_name": group_name},
        use_warehouse=True,
        include_context=True,
    )
    rows = normalize_rows(meta_rows)

    try:
        subject_rows = sf.query_service(
            f"""
            SELECT DISTINCT UPPER(SUBJECT_AREA) AS SUBJECT_AREA
            FROM {ADMIN_PKG_GROUP_SOURCE}
            WHERE ACTIVE_FL = TRUE
              AND PKG_GROUP_NAME = %(group_name)s
            """,
            params={"group_name": group_name},
        )
        subject = _identifier(subject_rows[0]["SUBJECT_AREA"], "LIM subject area") if subject_rows else None
        database = _identifier(LIM_DATABASE, "LIM database")
        if subject:
            live_rows = sf.query_service(
                f"""
                SELECT DW_FILE_NM,
                       IFF(COUNT_IF(COALESCE(DW_READY_TO_LOAD_FL, FALSE) = FALSE) = 0, TRUE, FALSE) AS FILE_READY
                FROM {database}.RAW_LIM.RAW_LIM_{subject}
                WHERE DW_FILE_NM IS NOT NULL
                  AND SUBSTR(DW_FILE_NM, POSITION('{subject}' IN UPPER(DW_FILE_NM)) + {len(subject)}, 3) <> '000'
                  AND SUBSTR(DW_FILE_NM, 1, POSITION('{subject}' IN UPPER(DW_FILE_NM)) - 1) IN (
                    SELECT UPPER(SOURCE_ID)
                    FROM {ADMIN_PKG_GROUP_SOURCE}
                    WHERE ACTIVE_FL = TRUE
                      AND PKG_GROUP_NAME = %(group_name)s
                  )
                GROUP BY DW_FILE_NM
                """,
                params={"group_name": group_name},
            )
            live_ready = {}
            for live_row in normalize_rows(live_rows):
                raw_key = re.sub(r"\.gz$", "", str(live_row.get("DW_FILE_NM") or ""), flags=re.IGNORECASE).lower()
                ready = bool(live_row.get("FILE_READY"))
                aliases = {raw_key, raw_key.rsplit("_", 1)[0] if "_" in raw_key else raw_key}
                for alias in aliases:
                    live_ready[alias] = live_ready.get(alias, True) and ready
            for row in rows:
                file_key = re.sub(r"\.gz$", "", str(row.get("FILE_NAME") or ""), flags=re.IGNORECASE).lower()
                if file_key in live_ready:
                    row["DW_READY_TO_LOAD_FL"] = live_ready[file_key]
                    row["READY_STATUS"] = "READY" if live_ready[file_key] else "WAITING"
    except Exception:
        current_app.logger.exception("Failed to overlay live RAW readiness for %s", group_name)

    return rows


def _load_ready_detail(group_name):
    rows = sf.query_service(
        f"""
        SELECT PKG_GROUP_NAME,
               DLVY_END_DATE,
               DLVY_SOURCE_ID,
               DLVY_PKG_ID,
               DLVY_PKG_YEAR,
               DLVY_PKG_YEAR_SEQ_NO,
               STATUS,
               ROWS_UPDATED,
               REASON,
               CONTROL_DATE
        FROM {SET_READY_LATEST_TABLE}
        WHERE PKG_GROUP_NAME = %(group_name)s
        ORDER BY CONTROL_DATE DESC, DLVY_END_DATE DESC, DLVY_SOURCE_ID
        """,
        params={"group_name": group_name},
        use_warehouse=True,
        include_context=True,
    )
    return normalize_rows(rows)


def _load_history_detail(group_name, history_days):
    rows = sf.query_service(
        f"""
        SELECT PKG_GROUP_NAME,
               DLVY_END_DATE,
               DLVY_SOURCE_ID,
               DLVY_PKG_ID,
               DLVY_PKG_YEAR,
               DLVY_PKG_YEAR_SEQ_NO,
               STATUS,
               ROWS_UPDATED,
               REASON,
               CONTROL_DATE,
               TO_DATE(CONTROL_DATE) AS CONTROL_RUN_DATE
        FROM {SET_READY_HISTORY_TABLE}
        WHERE PKG_GROUP_NAME = %(group_name)s
          AND CONTROL_DATE >= DATEADD(day, -%(history_days)s, CURRENT_TIMESTAMP())
        ORDER BY CONTROL_DATE DESC, DLVY_END_DATE DESC, DLVY_SOURCE_ID
        """,
        params={"group_name": group_name, "history_days": int(history_days)},
        use_warehouse=True,
        include_context=True,
    )
    return normalize_rows(rows)


def _raw_metrics(rows):
    return {
        "rows": len(rows),
        "received": sum(1 for row in rows if row.get("RECEIVED_FL") is True),
        "missing": sum(1 for row in rows if row.get("RECEIVED_FL") is False),
        "dwReady": sum(1 for row in rows if row.get("DW_READY_TO_LOAD_FL") is True),
        "rowcountIssues": sum(
            1
            for row in rows
            if row.get("ROWCOUNT_STATUS") not in (None, "", "ROWCOUNT_OK")
        ),
    }


def _ready_metrics(rows):
    return {
        "rows": len(rows),
        "updated": sum(1 for row in rows if row.get("STATUS") == "UPDATED"),
        "attention": sum(
            1 for row in rows if row.get("STATUS") in ("STOPPED", "FAILED", "SKIPPED", "BLOCKED")
        ),
        "rowsUpdated": sum(_num(row.get("ROWS_UPDATED")) for row in rows),
    }


def _history_metrics(rows):
    run_days = {str(row.get("CONTROL_RUN_DATE")) for row in rows if row.get("CONTROL_RUN_DATE")}
    return {
        "rows": len(rows),
        "runDays": len(run_days),
        "updated": sum(1 for row in rows if row.get("STATUS") == "UPDATED"),
        "attention": sum(
            1 for row in rows if row.get("STATUS") in ("STOPPED", "FAILED", "SKIPPED", "BLOCKED")
        ),
    }


def _load_lim_subject_areas(cur, database):
    prefix = "RAW_LIM_"

    def subjects_from_names(names):
        non_subject_tables = {"RAW_LIM_META", "RAW_LIM_CONTROL_ARCHIVE"}
        return sorted({
            name[len(prefix):]
            for name in (str(value or "").upper() for value in names)
            if name.startswith(prefix) and name not in non_subject_tables and len(name) > len(prefix)
        })

    try:
        cur.execute(
            f"""
            SELECT TABLE_NAME
            FROM {database}.INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = 'RAW_LIM'
              AND TABLE_TYPE = 'BASE TABLE'
              AND STARTSWITH(TABLE_NAME, 'RAW_LIM_')
              AND TABLE_NAME <> 'RAW_LIM_META'
            ORDER BY TABLE_NAME
            """
        )
        subjects = subjects_from_names(row.get("TABLE_NAME") for row in normalize_rows(cur.fetchall()))
        if subjects:
            return subjects
    except Exception as exc:
        current_app.logger.warning("INFORMATION_SCHEMA subject discovery failed: %s", exc)

    try:
        cur.execute(f"SHOW TABLES LIKE 'RAW_LIM_%' IN SCHEMA {database}.RAW_LIM")
        show_rows = normalize_rows(cur.fetchall())
        subjects = subjects_from_names(row.get("NAME") for row in show_rows)
        if subjects:
            return subjects
    except Exception as exc:
        current_app.logger.warning("SHOW TABLES subject discovery failed: %s", exc)

    cur.execute(
        f"""
        SELECT DISTINCT UPPER(SUBJECT_AREA) AS SUBJECT_AREA
        FROM {ADMIN_PKG_GROUP_SOURCE}
        WHERE ACTIVE_FL = TRUE
          AND SUBJECT_AREA IS NOT NULL
        ORDER BY SUBJECT_AREA
        """
    )
    return [str(row.get("SUBJECT_AREA")) for row in normalize_rows(cur.fetchall()) if row.get("SUBJECT_AREA")]


@file_ingestion_bp.get("/api/file-ingestion")
def file_ingestion_overview():
    history_days = _history_days()

    if config.USE_MOCK or not sf.is_configured():
        return jsonify(
            {
                "ok": True,
                "source": "mock",
                "historyDays": history_days,
                "subjectAreas": 0,
                "overview": [],
                "summary": _build_summary([]),
            }
        )

    try:
        data = _load_overview(history_days)
        return jsonify(
            {
                "ok": True,
                "source": "snowflake",
                "historyDays": history_days,
                **data,
            }
        )
    except Exception as exc:
        current_app.logger.exception("Failed to load LIM file ingestion overview")
        return _json_error(exc, 500)


@file_ingestion_bp.post("/api/file-ingestion/reload")
def file_ingestion_reload():
    payload = request.get_json(silent=True) or {}
    try:
        lim_format = _identifier(payload.get("limFormat"), "LIM format")
        mode = str(payload.get("mode") or "").strip().upper()
        if mode not in ("NORMAL", "PARTIAL_RELOAD", "FULL_RELOAD"):
            return _json_error("Mode must be NORMAL, PARTIAL_RELOAD or FULL_RELOAD.", 400)

        reload_enabled = mode != "NORMAL"
        from_date = payload.get("fromDate") or None
        to_date = payload.get("toDate") or None
        if mode == "FULL_RELOAD":
            from_date = None
            to_date = None
        elif bool(from_date) != bool(to_date):
            return _json_error("Select both delivery end dates or leave both blank.", 400)
        elif from_date and to_date:
            try:
                from_date = date.fromisoformat(str(from_date)).isoformat()
                to_date = date.fromisoformat(str(to_date)).isoformat()
            except ValueError:
                return _json_error("Delivery end dates must use YYYY-MM-DD.", 400)
            if from_date > to_date:
                return _json_error("From date cannot be after to date.", 400)

        reset_package_check = bool(payload.get("resetPackageCheck", reload_enabled))
        set_ready_to_load = bool(payload.get("setReadyToLoad", False))
        effective_mode = "FULL_RELOAD" if reload_enabled and not from_date and not to_date else mode
        if reload_enabled and payload.get("confirmation") != "RELOAD":
            return _json_error("Reload confirmation is required.", 400)

        if config.USE_MOCK or not sf.is_configured():
            return jsonify({"ok": True, "source": "mock", "result": {
                "STATUS": "SUCCESS", "LOAD_MODE": effective_mode, "LIM_FORMAT": lim_format,
                "FROM_DLVY_END_DATE": from_date, "TO_DLVY_END_DATE": to_date,
                "FILES_SELECTED": 0, "RAW_ROWS_DELETED": 0, "COPY_COMMANDS_EXECUTED": 0,
            }})

        database = _identifier(LIM_DATABASE, "LIM database")
        role = _identifier(LIM_ROLE, "LIM role")
        # Loads are an application-owned administrative operation. In SPCS the
        # browser caller token may not have visibility of KUMO_TST.META even
        # though the container service role does, so use the service context.
        with sf.connection_scope(force_service=True) as conn:
            cur = conn.cursor(DictCursor)
            try:
                cur.execute(f"USE ROLE {role}")
                cur.execute(f"USE DATABASE {database}")
                subject_areas = _load_lim_subject_areas(cur, database)
                if lim_format not in subject_areas:
                    return _json_error(f"RAW_LIM_{lim_format} was not found in {database}.RAW_LIM.", 400)
                cur.execute(
                    f"""
                    CALL {database}.META.LOAD_RAW_LIM_FROM_STAGE(
                      P_LIM_FORMAT => %(lim_format)s,
                      P_FROM_DLVY_END_DATE => %(from_date)s::DATE,
                      P_TO_DLVY_END_DATE => %(to_date)s::DATE,
                      P_RELOAD => %(reload)s,
                      P_RESET_DLVY_PKG_CHECK => %(reset)s,
                      P_SET_READY_TO_LOAD => %(ready)s
                    )
                    """,
                    {"lim_format": lim_format, "from_date": from_date, "to_date": to_date,
                     "reload": reload_enabled, "reset": reset_package_check, "ready": set_ready_to_load},
                )
                row = cur.fetchone()
            finally:
                cur.close()

        return jsonify({"ok": True, "source": "snowflake", "result": _procedure_result(row)})
    except ValueError as exc:
        return _json_error(exc, 400)
    except Exception as exc:
        current_app.logger.exception("Failed to execute RAW LIM load/reload")
        return _json_error(exc, 500)


@file_ingestion_bp.get("/api/file-ingestion/reload/subject-areas")
def file_ingestion_reload_subject_areas():
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"ok": True, "source": "mock", "subjectAreas": ["PAAR", "CACT"]})
    try:
        database = _identifier(LIM_DATABASE, "LIM database")
        with sf.connection_scope(force_service=True) as conn:
            cur = conn.cursor(DictCursor)
            try:
                subject_areas = _load_lim_subject_areas(cur, database)
            finally:
                cur.close()
        return jsonify({"ok": True, "source": "snowflake", "subjectAreas": subject_areas})
    except Exception as exc:
        current_app.logger.exception("Failed to list RAW LIM subject areas")
        return _json_error(exc, 500)


@file_ingestion_bp.get("/api/file-ingestion/<path:group_name>/raw")
def file_ingestion_raw(group_name):
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"ok": True, "source": "mock", "rows": [], "metrics": _raw_metrics([])})

    try:
        source_id = _source_id()
        rows = _filter_source(_load_raw_detail(group_name), source_id)
        return jsonify({"ok": True, "source": "snowflake", "rows": rows, "metrics": _raw_metrics(rows)})
    except ValueError as exc:
        return _json_error(exc, 400)
    except Exception as exc:
        current_app.logger.exception("Failed to load RAW LIM detail for %s", group_name)
        return _json_error(exc, 500)


@file_ingestion_bp.get("/api/file-ingestion/<path:group_name>/ready")
def file_ingestion_ready(group_name):
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"ok": True, "source": "mock", "rows": [], "metrics": _ready_metrics([])})

    try:
        source_id = _source_id()
        rows = _filter_source(_load_ready_detail(group_name), source_id)
        return jsonify({"ok": True, "source": "snowflake", "rows": rows, "metrics": _ready_metrics(rows)})
    except ValueError as exc:
        return _json_error(exc, 400)
    except Exception as exc:
        current_app.logger.exception("Failed to load READY LIM detail for %s", group_name)
        return _json_error(exc, 500)


@file_ingestion_bp.get("/api/file-ingestion/<path:group_name>/history")
def file_ingestion_history(group_name):
    history_days = _history_days()

    if config.USE_MOCK or not sf.is_configured():
        return jsonify(
            {
                "ok": True,
                "source": "mock",
                "historyDays": history_days,
                "rows": [],
                "metrics": _history_metrics([]),
            }
        )

    try:
        source_id = _source_id()
        rows = _filter_source(_load_history_detail(group_name, history_days), source_id)
        return jsonify(
            {
                "ok": True,
                "source": "snowflake",
                "historyDays": history_days,
                "rows": rows,
                "metrics": _history_metrics(rows),
            }
        )
    except ValueError as exc:
        return _json_error(exc, 400)
    except Exception as exc:
        current_app.logger.exception("Failed to load LIM history detail for %s", group_name)
        return _json_error(exc, 500)
