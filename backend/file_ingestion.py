"""LIM file ingestion monitoring API for KUMO Monitor.

Migrated from the Streamlit File Ingestion Monitor. The overview endpoint keeps
initial page loads small; detail endpoints load RAW / READY / history rows only
when a user opens a detail dialog.
"""

import json
import os
import re
import threading
import time
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
LIM_STAGE = os.getenv("KUMO_LIM_STAGE", "KUMO_TST.META.AZURE_LIM_STAGE")
LIM_FORMAT_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,49}$")
FQN_PATTERN = re.compile(r"^[A-Z][A-Z0-9_$]{0,254}(?:\.[A-Z][A-Z0-9_$]{0,254}){0,2}$")
LIM_HEADER_FIELDS = (
    ("DLVY_REC_TYPE_ID", 0, 2),
    ("DLVY_SOURCE_ID", 2, 3),
    ("DLVY_SUBJECT_AREA_ID", 5, 4),
    ("DLVY_PKG_ID", 9, 3),
    ("DLVY_PKG_YEAR", 12, 4),
    ("DLVY_PKG_YEAR_SEQ_NO", 16, 5),
    ("DLVY_LIM_OBJ_SEQ_NO", 21, 3),
    ("DLVY_LIM_OBJ_VER_NO", 24, 3),
    ("DLVY_START_DATE", 27, 10),
    ("DLVY_END_DATE", 37, 10),
    ("DLVY_COPY_NAME", 47, 10),
    ("DLVY_DELTA_EVENT_TYPE", 57, 1),
)

_dashboard_attention_lock = threading.Lock()
_dashboard_attention_cache = {"payload": None, "cached_at": 0.0}
_DASHBOARD_ATTENTION_CACHE_SECONDS = 30


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


def _fqn(value, label):
    value = str(value or "").strip().upper()
    if not FQN_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid {label}.")
    return value


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
        "UPDATED": "Ready",
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


def _dashboard_attention_snapshot():
    """Return the dashboard LIM check using the overview's canonical status logic."""
    now = time.monotonic()
    with _dashboard_attention_lock:
        cached = _dashboard_attention_cache.get("payload")
        age = now - float(_dashboard_attention_cache.get("cached_at") or 0)
        if cached is not None and age < _DASHBOARD_ATTENTION_CACHE_SECONDS:
            return {**cached, "cached": True}

        data = _load_overview(DEFAULT_HISTORY_DAYS)
        summary = data.get("summary") or _build_summary([])
        attention = []
        for row in data.get("overview") or []:
            if row.get("STATUS_KIND") not in ("ATTENTION", "ROWCOUNT_ISSUE", "MISSING_FILES"):
                continue
            attention.append({
                "groupName": row.get("PKG_GROUP_NAME"),
                "subjectArea": row.get("SUBJECT_AREA"),
                "statusKind": row.get("STATUS_KIND"),
                "statusLabel": row.get("STATUS_LABEL"),
                "latestStatuses": row.get("LATEST_STATUS_LIST"),
            })
        payload = {
            "ok": True,
            "source": "file-ingestion-overview",
            "status": summary.get("engineStatus") or "READY",
            "hasAttention": bool(summary.get("attentionGroups") or summary.get("missingGroups")),
            "summary": summary,
            "attention": attention[:8],
        }
        _dashboard_attention_cache["payload"] = payload
        _dashboard_attention_cache["cached_at"] = now
        return {**payload, "cached": False}


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
            source_catalog = _load_source_catalog(cur)
            catalog_map = {}
            for source_row in source_catalog:
                group = source_row.get("PKG_GROUP_NAME")
                if group and group not in catalog_map:
                    catalog_map[group] = {
                        "SUBJECT_AREA": source_row.get("SUBJECT_AREA") or "Unknown subject area",
                        "PKG_GROUP_NAME": group,
                    }
            catalog = list(catalog_map.values())
            groups = [str(row.get("PKG_GROUP_NAME")) for row in catalog if row.get("PKG_GROUP_NAME")]
            if not groups:
                return {"overview": [], "summary": _build_summary([]), "subjectAreas": 0}

            placeholders, params = _group_params(groups)

            # RAW metadata and history are intentionally detail-only. Querying
            # them here makes the landing page scan large blocked backlogs.
            meta_rows = []
            source_meta_rows = []
            raw_readiness_rows = []

            cur.execute(
                f"""
                WITH LATEST_PACKAGE_RUNS AS (
                    SELECT *
                    FROM {SET_READY_HISTORY_TABLE}
                    WHERE PKG_GROUP_NAME IN ({placeholders})
                    QUALIFY CONTROL_DATE = MAX(CONTROL_DATE) OVER (
                        PARTITION BY PKG_GROUP_NAME
                    )
                )
                SELECT PKG_GROUP_NAME,
                       IFF(GROUPING(UPPER(DLVY_SOURCE_ID)) = 1, NULL, UPPER(DLVY_SOURCE_ID)) AS SOURCE_ID,
                       COUNT(*) AS LATEST_LOG_ROWS,
                       COUNT_IF(STATUS = 'UPDATED') AS LATEST_UPDATED_ROWS,
                       COUNT_IF(STATUS IN ('STOPPED', 'FAILED', 'SKIPPED', 'BLOCKED')) AS LATEST_ATTENTION_ROWS,
                       LISTAGG(DISTINCT STATUS, ', ') WITHIN GROUP (ORDER BY STATUS) AS LATEST_STATUS_LIST,
                       MAX(CONTROL_DATE) AS LATEST_CONTROL_DATE
                FROM LATEST_PACKAGE_RUNS
                GROUP BY GROUPING SETS ((PKG_GROUP_NAME), (PKG_GROUP_NAME, UPPER(DLVY_SOURCE_ID)))
                """,
                params,
            )
            all_latest_rows = normalize_rows(cur.fetchall())

            cur.execute(
                f"""
                WITH LATEST_META AS (
                    SELECT *
                    FROM {RAW_LIM_META_TABLE}
                    WHERE PKG_GROUP_NAME IN ({placeholders})
                    QUALIFY RUN_DTTM = MAX(RUN_DTTM) OVER (PARTITION BY PKG_GROUP_NAME)
                )
                SELECT PKG_GROUP_NAME,
                       IFF(GROUPING(UPPER(DLVY_SOURCE_ID)) = 1, NULL, UPPER(DLVY_SOURCE_ID)) AS SOURCE_ID,
                       MAX(RUN_DTTM) AS VALIDATED_AT,
                       COUNT_IF(RECEIVED_FL = FALSE
                                OR IS_VALID_SEQUENCE = FALSE
                                OR COALESCE(ROWCOUNT_STATUS, 'ROWCOUNT_OK') <> 'ROWCOUNT_OK') AS VALIDATION_ISSUES
                FROM LATEST_META
                GROUP BY GROUPING SETS ((PKG_GROUP_NAME), (PKG_GROUP_NAME, UPPER(DLVY_SOURCE_ID)))
                """,
                params,
            )
            validation_by_scope = {
                (str(row.get("PKG_GROUP_NAME") or ""), str(row.get("SOURCE_ID") or "")): row
                for row in normalize_rows(cur.fetchall())
            }
            for latest_row in all_latest_rows:
                scope = (str(latest_row.get("PKG_GROUP_NAME") or ""), str(latest_row.get("SOURCE_ID") or ""))
                validation = validation_by_scope.get(scope)
                if not validation or _num(validation.get("VALIDATION_ISSUES")):
                    continue
                validated_at = validation.get("VALIDATED_AT")
                control_date = latest_row.get("LATEST_CONTROL_DATE")
                if validated_at and control_date and validated_at > control_date:
                    latest_row["LATEST_ATTENTION_ROWS"] = 0
                    latest_row["LATEST_UPDATED_ROWS"] = _num(latest_row.get("LATEST_LOG_ROWS"))
                    latest_row["LATEST_STATUS_LIST"] = "RESOLVED / READY TO RETRY"
            latest_rows = [row for row in all_latest_rows if not row.get("SOURCE_ID")]
            source_latest_rows = [row for row in all_latest_rows if row.get("SOURCE_ID")]
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
               ORIGINAL_FILE_NAME,
               FILE_NAME,
               FILE_KEY,
               CONTROL_FILE_NAME,
               FILE_TYPE,
               PKG_GROUP_NAME,
               DLVY_SOURCE_ID,
               DLVY_SUBJECT_AREA_ID,
               DLVY_LIM_OBJ_SEQ_NO,
               DLVY_LIM_OBJ_VER_NO,
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
               DATA_ROWS,
               ACTUAL_ROWS,
               LOADED_AT,
               RUN_DTTM
        FROM {RAW_LIM_META_TABLE}
        WHERE PKG_GROUP_NAME = %(group_name)s
        QUALIFY RUN_DTTM = MAX(RUN_DTTM) OVER (PARTITION BY PKG_GROUP_NAME)
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
        WITH LATEST_PACKAGE_RUN AS (
            SELECT *
            FROM {SET_READY_HISTORY_TABLE}
            WHERE PKG_GROUP_NAME = %(group_name)s
            QUALIFY CONTROL_DATE = MAX(CONTROL_DATE) OVER (
                PARTITION BY PKG_GROUP_NAME
            )
        )
        SELECT log.PKG_GROUP_NAME,
               log.DLVY_END_DATE,
               log.DLVY_SOURCE_ID,
               log.DLVY_PKG_ID,
               log.DLVY_PKG_YEAR,
               log.DLVY_PKG_YEAR_SEQ_NO,
               log.STATUS,
               log.ROWS_UPDATED,
               log.REASON,
               log.CONTROL_DATE,
               cfg.DLVY_PKG_YEAR AS CONFIGURED_PKG_YEAR,
               cfg.DLVY_PKG_YEAR_SEQ_NO AS CONFIGURED_SEQUENCE
        FROM LATEST_PACKAGE_RUN log
        LEFT JOIN {ADMIN_PKG_GROUP_SOURCE} cfg
          ON cfg.PKG_GROUP_NAME = log.PKG_GROUP_NAME
         AND UPPER(cfg.SOURCE_ID) = UPPER(log.DLVY_SOURCE_ID)
         AND cfg.ACTIVE_FL = TRUE
        ORDER BY log.CONTROL_DATE DESC, log.DLVY_END_DATE DESC, log.DLVY_SOURCE_ID
        """,
        params={"group_name": group_name},
        use_warehouse=True,
        include_context=True,
    )
    normalized = normalize_rows(rows)

    # SET_READY history is immutable audit data. If a newer RAW metadata
    # snapshot proves that a previously stopped package now validates, present
    # the old result as resolved instead of continuing to call it the current
    # first error.
    validation_rows = normalize_rows(sf.query_service(
        f"""
        WITH LATEST_META AS (
          SELECT *
          FROM {RAW_LIM_META_TABLE}
          WHERE PKG_GROUP_NAME = %(group_name)s
          QUALIFY RUN_DTTM = MAX(RUN_DTTM) OVER (PARTITION BY PKG_GROUP_NAME)
        )
        SELECT DLVY_END_DATE, UPPER(DLVY_SOURCE_ID) AS DLVY_SOURCE_ID,
               DLVY_PKG_ID, DLVY_PKG_YEAR, DLVY_PKG_YEAR_SEQ_NO,
               MAX(RUN_DTTM) AS VALIDATED_AT,
               COUNT_IF(RECEIVED_FL = FALSE
                        OR IS_VALID_SEQUENCE = FALSE
                        OR COALESCE(ROWCOUNT_STATUS, 'ROWCOUNT_OK') <> 'ROWCOUNT_OK') AS VALIDATION_ISSUES
        FROM LATEST_META
        GROUP BY DLVY_END_DATE, UPPER(DLVY_SOURCE_ID), DLVY_PKG_ID,
                 DLVY_PKG_YEAR, DLVY_PKG_YEAR_SEQ_NO
        """,
        params={"group_name": group_name}, use_warehouse=True, include_context=True,
    ))

    def package_key(row):
        return (
            str(row.get("DLVY_END_DATE") or "")[:10],
            str(row.get("DLVY_SOURCE_ID") or "").upper(),
            str(row.get("DLVY_PKG_ID") or ""),
            str(row.get("DLVY_PKG_YEAR") or ""),
            str(row.get("DLVY_PKG_YEAR_SEQ_NO") or ""),
        )

    current_validation = {package_key(row): row for row in validation_rows}
    for row in normalized:
        if row.get("STATUS") not in ("STOPPED", "FAILED", "SKIPPED", "BLOCKED"):
            continue
        validation = current_validation.get(package_key(row))
        if not validation or _num(validation.get("VALIDATION_ISSUES")):
            continue
        validated_at = validation.get("VALIDATED_AT")
        control_date = row.get("CONTROL_DATE")
        if validated_at and control_date and validated_at <= control_date:
            continue
        row["ORIGINAL_STATUS"] = row.get("STATUS")
        row["STATUS"] = "READY_TO_RETRY" if row.get("ORIGINAL_STATUS") == "BLOCKED" else "RESOLVED"
        row["RESOLVED_AT"] = validated_at
    return normalized


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
        "updated": sum(1 for row in rows if row.get("STATUS") in ("UPDATED", "RESOLVED", "READY_TO_RETRY")),
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


@file_ingestion_bp.get("/api/dashboard/lim-attention")
def dashboard_lim_attention():
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({
            "ok": True,
            "source": "mock",
            "status": "READY",
            "hasAttention": False,
            "summary": _build_summary([]),
            "attention": [],
            "cached": False,
        })
    try:
        return jsonify(_dashboard_attention_snapshot())
    except Exception as exc:
        current_app.logger.exception("Failed to load dashboard LIM attention check")
        return _json_error(exc, 500)


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


@file_ingestion_bp.get("/api/file-ingestion/raw-status")
def file_ingestion_raw_status():
    """Load the expensive live RAW readiness snapshot only on demand."""
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"ok": True, "source": "mock", "rows": []})
    try:
        with sf.connection(use_warehouse=True, include_context=True, force_service=True) as conn:
            cur = conn.cursor(DictCursor)
            try:
                source_catalog = _load_source_catalog(cur)
                catalog = list({
                    (row.get("PKG_GROUP_NAME"), row.get("SUBJECT_AREA"))
                    for row in source_catalog if row.get("PKG_GROUP_NAME")
                })
                catalog = [
                    {"PKG_GROUP_NAME": group, "SUBJECT_AREA": subject}
                    for group, subject in catalog
                ]
                rows = _load_raw_table_readiness(cur, catalog)
            finally:
                cur.close()
        return jsonify({"ok": True, "source": "snowflake", "rows": rows})
    except Exception as exc:
        current_app.logger.exception("Failed to load optional RAW readiness status")
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


@file_ingestion_bp.get("/api/file-ingestion/<path:group_name>/investigate-rowcount")
def investigate_rowcount(group_name):
    """Run sequential, read-only checks for one ROWCOUNT_MISMATCH file."""
    try:
        file_name = str(request.args.get("fileName") or "").strip()
        if not file_name or len(file_name) > 500:
            return _json_error("A valid fileName is required.", 400)
        source_id = _source_id()

        if config.USE_MOCK or not sf.is_configured():
            return jsonify({"ok": True, "source": "mock", "fileName": file_name, "checks": []})

        meta = normalize_rows(sf.query_service(
            f"""
            SELECT RAW_TABLE, FILE_NAME, EXPECTED_ROWS, ACTUAL_ROWS, DLVY_SOURCE_ID
            FROM {RAW_LIM_META_TABLE}
            WHERE PKG_GROUP_NAME = %(group_name)s
              AND FILE_NAME = %(file_name)s
              {"AND UPPER(DLVY_SOURCE_ID) = %(source_id)s" if source_id else ""}
            ORDER BY RUN_DTTM DESC
            LIMIT 1
            """,
            params={"group_name": group_name, "file_name": file_name, **({"source_id": source_id} if source_id else {})},
            use_warehouse=True, include_context=True,
        ))
        if not meta:
            return _json_error("The file was not found in the latest LIM metadata.", 404)

        raw_table = _fqn(meta[0].get("RAW_TABLE"), "RAW table")
        stage = _fqn(LIM_STAGE, "LIM stage")
        canonical_pattern = (
            "REGEXP_SUBSTR(REGEXP_SUBSTR(DW_FILE_NM, '[^/]+$'), "
            "'^[A-Z0-9]{10}_[0-9]{3}_[0-9]{3}_0000_[0-9]{9}_[0-9]{8}')"
        )
        # A ROWCOUNT_MISMATCH is reported against the 000_000 control file.
        # Duplicate-row checks must inspect its actual data files instead: same
        # source/subject and package suffix, excluding the control object itself.
        data_file_prefix = file_name[:7]
        package_suffix = file_name[14:] if len(file_name) > 14 else ""
        data_file_filter = (
            f"{canonical_pattern} LIKE %(data_file_pattern)s "
            f"AND {canonical_pattern} <> %(file_name)s"
        )
        investigation_params = {
            "file_name": file_name,
            "data_file_pattern": f"{data_file_prefix}%{package_suffix}",
        }
        physical_files = normalize_rows(sf.query_service(
            f"""
            SELECT %(file_name)s AS DATA_FILE_NAME,
                   DW_FILE_NM, DW_FILE_CHECK_SUM, MIN(DW_LOAD_DTTM) AS LOADED_AT,
                   COUNT(*) AS PHYSICAL_ROWS,
                   COUNT_IF(SUBSTR(TRIM(DATA), 1, 2) = '10') AS DATA_ROWS,
                   HASH_AGG(DATA) AS CONTENT_SIGNATURE
            FROM {raw_table}
            WHERE {canonical_pattern} = %(file_name)s
            GROUP BY DW_FILE_NM, DW_FILE_CHECK_SUM
            ORDER BY LOADED_AT DESC
            """,
            params={"file_name": file_name}, use_warehouse=True, include_context=True,
        ))
        duplicate_file_count = max(0, len(physical_files) - 1)
        headers = []
        if duplicate_file_count:
            parsed_header_sql = ",\n                   ".join(
                f"TRIM(SUBSTR(DATA, {offset + 1}, {length})) AS {name}"
                for name, offset, length in LIM_HEADER_FIELDS
            )
            headers = normalize_rows(sf.query_service(
                f"""
                SELECT DW_FILE_NM, DW_FILE_ROW_NUMBER,
                       {parsed_header_sql}
                FROM {raw_table}
                WHERE {canonical_pattern} = %(file_name)s
                  AND SUBSTR(TRIM(DATA), 1, 2) = '00'
                ORDER BY DW_FILE_NM, DW_FILE_ROW_NUMBER
                """,
                params={"file_name": file_name}, use_warehouse=True, include_context=True,
            ))
        checks = [{
            "key": "duplicate_files", "title": "Duplicate files",
            "status": "WARNING" if duplicate_file_count else "PASS",
            "summary": f"{len(physical_files)} physical files match; {duplicate_file_count} possible duplicate copies.",
            "rows": physical_files, "headers": headers,
        }]

        related_physical_files = normalize_rows(sf.query_service(
            f"""
            SELECT {canonical_pattern} AS DATA_FILE_NAME,
                   DW_FILE_NM, DW_FILE_CHECK_SUM,
                   MIN(DW_LOAD_DTTM) AS LOADED_AT,
                   COUNT(*) AS PHYSICAL_ROWS,
                   COUNT_IF(SUBSTR(TRIM(DATA), 1, 2) = '10') AS DATA_ROWS,
                   HASH_AGG(DATA) AS CONTENT_SIGNATURE
            FROM {raw_table}
            WHERE {data_file_filter}
            GROUP BY {canonical_pattern}, DW_FILE_NM, DW_FILE_CHECK_SUM
            ORDER BY DATA_FILE_NAME, LOADED_AT DESC, DW_FILE_NM DESC
            """,
            params=investigation_params, use_warehouse=True, include_context=True,
        ))
        canonical_counts = {}
        for row in related_physical_files:
            canonical_name = str(row.get("DATA_FILE_NAME") or "")
            canonical_counts[canonical_name] = canonical_counts.get(canonical_name, 0) + 1
        duplicate_data_files = [
            row for row in related_physical_files
            if canonical_counts.get(str(row.get("DATA_FILE_NAME") or ""), 0) > 1
        ]
        data_headers = []
        if duplicate_data_files:
            parsed_header_sql = ",\n                   ".join(
                f"TRIM(SUBSTR(DATA, {offset + 1}, {length})) AS {name}"
                for name, offset, length in LIM_HEADER_FIELDS
            )
            duplicate_names = list({row.get("DATA_FILE_NAME") for row in duplicate_data_files})
            name_placeholders = ", ".join(f"%(duplicate_name_{index})s" for index in range(len(duplicate_names)))
            data_headers = normalize_rows(sf.query_service(
                f"""
                SELECT DW_FILE_NM, DW_FILE_ROW_NUMBER, {parsed_header_sql}
                FROM {raw_table}
                WHERE {canonical_pattern} IN ({name_placeholders})
                  AND SUBSTR(TRIM(DATA), 1, 2) = '00'
                ORDER BY DW_FILE_NM, DW_FILE_ROW_NUMBER
                """,
                params={f"duplicate_name_{index}": value for index, value in enumerate(duplicate_names)},
                use_warehouse=True, include_context=True,
            ))
        checks.append({
            "key": "duplicate_data_files", "title": "Duplicate data files",
            "status": "WARNING" if duplicate_data_files else "PASS",
            "summary": f"Found {len(duplicate_data_files)} physical copies across related data files." if duplicate_data_files else "No related data file has multiple physical copies.",
            "rows": duplicate_data_files, "headers": data_headers,
        })

        package_pattern = f".*{re.escape(data_file_prefix)}.*{re.escape(package_suffix)}.*".replace("'", "''")
        stage_rows = normalize_rows(sf.query_service(
            f"LIST @{stage} PATTERN='{package_pattern}'",
            use_warehouse=True, include_context=True,
        ))
        raw_by_file = {
            str(row.get("DW_FILE_NM")): row
            for row in [*physical_files, *related_physical_files]
        }
        canonical_filename_re = re.compile(
            r"^[A-Z0-9]{10}_[0-9]{3}_[0-9]{3}_0000_[0-9]{9}_[0-9]{8}", re.IGNORECASE
        )
        disk_rows = []
        for stage_row in stage_rows[:200]:
            stage_name = str(stage_row.get("NAME") or "")
            physical_name = stage_name.rsplit("/", 1)[-1]
            is_quarantined = physical_name.lower().startswith("deleted_")
            effective_name = physical_name[8:] if is_quarantined else physical_name
            canonical_match = canonical_filename_re.match(effective_name)
            canonical_name = canonical_match.group(0) if canonical_match else ""
            raw_row = raw_by_file.get(effective_name, {})
            disk_rows.append({
                "DW_FILE_NM": physical_name,
                "DATA_FILE_NAME": canonical_name,
                "FILE_TYPE": "QUARANTINED" if is_quarantined else "CONTROL" if canonical_name[7:14] == "000_000" else "DATA",
                "IS_QUARANTINED": is_quarantined,
                "DATA_ROWS": raw_row.get("DATA_ROWS"),
                "PHYSICAL_ROWS": raw_row.get("PHYSICAL_ROWS"),
                "DW_FILE_CHECK_SUM": stage_row.get("MD5"),
                "SIZE": stage_row.get("SIZE"),
                "LOADED_AT": stage_row.get("LAST_MODIFIED"),
                "STAGE_URL": stage_name,
                "SELECTION_ID": stage_name,
            })
        disk_canonical_counts = {}
        for row in disk_rows:
            canonical_name = str(row.get("DATA_FILE_NAME") or "")
            if canonical_name and not row.get("IS_QUARANTINED"):
                disk_canonical_counts[canonical_name] = disk_canonical_counts.get(canonical_name, 0) + 1
        for row in disk_rows:
            duplicate_copies = disk_canonical_counts.get(str(row.get("DATA_FILE_NAME") or ""), 0)
            row["IS_DUPLICATE"] = duplicate_copies > 1 and not row.get("IS_QUARANTINED")
            row["DUPLICATE_COPIES"] = duplicate_copies if row["IS_DUPLICATE"] else None
        duplicate_disk_files = sum(1 for row in disk_rows if row.get("IS_DUPLICATE"))
        duplicate_disk_sets = sum(1 for count in disk_canonical_counts.values() if count > 1)
        quarantined_disk_files = sum(1 for row in disk_rows if row.get("IS_QUARANTINED"))
        checks.append({
            "key": "disk_files", "title": "Control and data files on disk",
            "status": "WARNING" if duplicate_disk_files or not disk_rows else "PASS",
            "summary": (
                f"Found {duplicate_disk_sets} duplicate set(s) containing {duplicate_disk_files} active files; {quarantined_disk_files} quarantined file(s) are preserved."
                if duplicate_disk_files
                else f"Found {len(disk_rows) - quarantined_disk_files} active control/data object(s) and {quarantined_disk_files} quarantined file(s) in @{stage}."
                if disk_rows else f"No package object was found in @{stage}."
            ),
            "rows": disk_rows,
        })
        return jsonify({
            "ok": True, "source": "snowflake", "fileName": file_name,
            "expectedRows": meta[0].get("EXPECTED_ROWS"), "actualRows": meta[0].get("ACTUAL_ROWS"),
            "checks": checks,
        })
    except ValueError as exc:
        return _json_error(exc, 400)
    except Exception as exc:
        current_app.logger.exception("Failed ROWCOUNT_MISMATCH investigation for %s", group_name)
        return _json_error(exc, 500)


@file_ingestion_bp.post("/api/file-ingestion/<path:group_name>/resolve-duplicates")
def resolve_duplicate_files(group_name):
    """Remove explicitly selected physical copies from RAW and the stage."""
    payload = request.get_json(silent=True) or {}
    try:
        file_name = str(payload.get("fileName") or "").strip()
        if not file_name or len(file_name) > 500:
            return _json_error("A valid fileName is required.", 400)
        if payload.get("confirmation") != "REMOVE_DUPLICATES":
            return _json_error("Duplicate removal confirmation is required.", 400)
        requested_files = payload.get("removeFiles")
        if not isinstance(requested_files, list) or not requested_files:
            return _json_error("Select at least one duplicate file to remove.", 400)
        if any(not isinstance(value, str) or not value.strip() or len(value) > 2000 for value in requested_files):
            return _json_error("Every selected file must have a valid filename.", 400)
        requested_files = list(dict.fromkeys(value.strip() for value in requested_files))

        source_id = _identifier(payload.get("sourceId"), "source ID") if payload.get("sourceId") else None
        if config.USE_MOCK or not sf.is_configured():
            return jsonify({"ok": True, "source": "mock", "retainedFiles": [file_name],
                            "removedFiles": [], "rawRowsDeleted": 0, "stageFilesRenamed": 0})

        meta = normalize_rows(sf.query_service(
            f"""
            SELECT RAW_TABLE
            FROM {RAW_LIM_META_TABLE}
            WHERE PKG_GROUP_NAME = %(group_name)s
              AND FILE_NAME = %(file_name)s
              {"AND UPPER(DLVY_SOURCE_ID) = %(source_id)s" if source_id else ""}
            ORDER BY RUN_DTTM DESC
            LIMIT 1
            """,
            params={"group_name": group_name, "file_name": file_name,
                    **({"source_id": source_id} if source_id else {})},
            use_warehouse=True, include_context=True,
        ))
        if not meta:
            return _json_error("The file was not found in the latest LIM metadata.", 404)

        raw_table = _fqn(meta[0].get("RAW_TABLE"), "RAW table")
        stage = _fqn(LIM_STAGE, "LIM stage")
        canonical_pattern = (
            "REGEXP_SUBSTR(REGEXP_SUBSTR(DW_FILE_NM, '[^/]+$'), "
            "'^[A-Z0-9]{10}_[0-9]{3}_[0-9]{3}_0000_[0-9]{9}_[0-9]{8}')"
        )
        data_file_prefix = file_name[:7]
        package_suffix = file_name[14:] if len(file_name) > 14 else ""
        physical_files = normalize_rows(sf.query_service(
            f"""
            SELECT {canonical_pattern} AS DATA_FILE_NAME, DW_FILE_NM,
                   MIN(DW_LOAD_DTTM) AS LOADED_AT, HASH_AGG(DATA) AS CONTENT_SIGNATURE
            FROM {raw_table}
            WHERE {canonical_pattern} = %(file_name)s
               OR ({canonical_pattern} LIKE %(data_file_pattern)s AND {canonical_pattern} <> %(file_name)s)
            GROUP BY {canonical_pattern}, DW_FILE_NM
            ORDER BY DATA_FILE_NAME, LOADED_AT DESC, DW_FILE_NM DESC
            """,
            params={"file_name": file_name, "data_file_pattern": f"{data_file_prefix}%{package_suffix}"},
            use_warehouse=True, include_context=True,
        ))
        package_pattern = f".*{re.escape(data_file_prefix)}.*{re.escape(package_suffix)}.*".replace("'", "''")
        stage_rows = normalize_rows(sf.query_service(
            f"LIST @{stage} PATTERN='{package_pattern}'", use_warehouse=True, include_context=True,
        ))
        stage_by_url = {str(row.get("NAME")): row for row in stage_rows
                        if not str(row.get("NAME") or "").rsplit("/", 1)[-1].lower().startswith("deleted_")}
        stage_by_basename = {}
        for stage_url, row in stage_by_url.items():
            stage_by_basename.setdefault(stage_url.rsplit("/", 1)[-1], []).append(row)

        selected_stage_rows = []
        for selected in requested_files:
            if selected in stage_by_url:
                selected_stage_rows.append(stage_by_url[selected])
                continue
            basename_matches = stage_by_basename.get(selected, [])
            if len(basename_matches) != 1:
                return _json_error("A selected staged file is missing or ambiguous. Run the investigation again.", 409)
            selected_stage_rows.append(basename_matches[0])
        removed_files = [str(row.get("NAME")).rsplit("/", 1)[-1] for row in selected_stage_rows]
        retained_files = [str(row.get("DW_FILE_NM")) for row in physical_files
                          if str(row.get("DW_FILE_NM")) not in removed_files]
        raw_rows_deleted = 0
        stage_files_renamed = 0
        role = _identifier(LIM_ROLE, "LIM role")
        with sf.connection_scope(force_service=True) as conn:
            cur = conn.cursor(DictCursor)
            try:
                cur.execute(f"USE ROLE {role}")
                cur.execute(f"SELECT GET_STAGE_LOCATION(@{stage}) AS STAGE_LOCATION")
                stage_location_row = cur.fetchone() or {}
                stage_location = str(stage_location_row.get("STAGE_LOCATION") or "").rstrip("/") + "/"
                # COPY FILES with a target filename creates the quarantined
                # object first. Only after it is verified do we remove the
                # original stage name and its already-loaded RAW rows.
                for stage_row in selected_stage_rows:
                    source_url = str(stage_row.get("NAME"))
                    if not stage_location.strip("/") or not source_url.startswith(stage_location):
                        raise RuntimeError(f"Could not resolve {source_url} relative to the LIM stage.")
                    relative_source = source_url[len(stage_location):]
                    source_stage_url = f"@{stage}/{relative_source}"
                    physical_name = source_url.rsplit("/", 1)[-1]
                    source_folder = relative_source.rpartition("/")[0]
                    quarantined_name = f"{source_folder + '/' if source_folder else ''}deleted_{physical_name}"
                    escaped_source = source_stage_url.replace("'", "''")
                    escaped_target = quarantined_name.replace("'", "''")
                    escaped_quarantine_pattern = re.escape(quarantined_name).replace("'", "''")
                    cur.execute(f"LIST @{stage} PATTERN='.*{escaped_quarantine_pattern}$'")
                    if cur.fetchone():
                        return _json_error(f"The quarantine target {quarantined_name} already exists.", 409)
                    cur.execute(
                        f"COPY FILES INTO @{stage} FROM (SELECT '{escaped_source}', '{escaped_target}')"
                    )
                    cur.execute(f"LIST @{stage} PATTERN='.*{escaped_quarantine_pattern}$'")
                    if not cur.fetchone():
                        raise RuntimeError(f"Could not verify quarantined file {quarantined_name}.")
                    escaped_source_location = source_stage_url.replace("'", "''")
                    cur.execute(f"REMOVE '{escaped_source_location}'")
                    escaped_original_pattern = re.escape(relative_source).replace("'", "''")
                    cur.execute(f"LIST @{stage} PATTERN='.*{escaped_original_pattern}$'")
                    if cur.fetchone():
                        raise RuntimeError(
                            f"The renamed copy was preserved, but the original file {physical_name} could not be removed. RAW was not changed."
                        )
                    stage_files_renamed += 1

                placeholders = ", ".join(f"%(removed_file_{index})s" for index in range(len(removed_files)))
                delete_params = {f"removed_file_{index}": value for index, value in enumerate(removed_files)}
                cur.execute(
                    f"DELETE FROM {raw_table} WHERE DW_FILE_NM IN ({placeholders})",
                    delete_params,
                )
                raw_rows_deleted = max(0, int(cur.rowcount or 0))
            finally:
                cur.close()

        return jsonify({"ok": True, "source": "snowflake", "retainedFiles": retained_files,
                        "removedFiles": removed_files, "rawRowsDeleted": raw_rows_deleted,
                        "stageFilesRenamed": stage_files_renamed})
    except ValueError as exc:
        return _json_error(exc, 400)
    except Exception as exc:
        current_app.logger.exception("Failed duplicate resolution for %s", group_name)
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
