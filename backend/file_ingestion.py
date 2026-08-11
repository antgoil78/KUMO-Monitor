"""LIM file ingestion monitoring API for KUMO Monitor.

Migrated from the Streamlit File Ingestion Monitor. The overview endpoint keeps
initial page loads small; detail endpoints load RAW / READY / history rows only
when a user opens a detail dialog.
"""

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


def _json_error(error, status=500):
    return jsonify({"ok": False, "error": str(error or "Unexpected error")}), status


def _history_days():
    raw = request.args.get("historyDays", DEFAULT_HISTORY_DAYS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_HISTORY_DAYS
    return max(1, min(value, MAX_HISTORY_DAYS))


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
    if latest_updated > 0:
        return "UPDATED"
    if file_rows > 0 and ready_rows == file_rows:
        return "READY"
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


def _group_params(groups):
    params = {f"g{i}": name for i, name in enumerate(groups)}
    placeholders = ", ".join(f"%({key})s" for key in params)
    return placeholders, params


def _build_overview(catalog, meta_rows, latest_rows, history_rows):
    meta = {str(row.get("PKG_GROUP_NAME")): row for row in meta_rows}
    latest = {str(row.get("PKG_GROUP_NAME")): row for row in latest_rows}
    history = {str(row.get("PKG_GROUP_NAME")): row for row in history_rows}

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

        kind = _status_kind(row)
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


def _load_overview(history_days):
    with sf.connection(use_warehouse=True, include_context=True, force_service=True) as conn:
        cur = conn.cursor(DictCursor)
        try:
            catalog = _load_catalog(cur)
            groups = [str(row.get("PKG_GROUP_NAME")) for row in catalog if row.get("PKG_GROUP_NAME")]
            if not groups:
                return {"overview": [], "summary": _build_summary([]), "subjectAreas": 0}

            placeholders, params = _group_params(groups)

            cur.execute(
                f"""
                SELECT PKG_GROUP_NAME,
                       COUNT(*) AS FILE_ROWS,
                       COUNT_IF(RECEIVED_FL = TRUE) AS RECEIVED_ROWS,
                       COUNT_IF(RECEIVED_FL = FALSE) AS MISSING_ROWS,
                       COUNT_IF(DW_READY_TO_LOAD_FL = TRUE) AS READY_ROWS,
                       COUNT_IF(DW_READY_TO_LOAD_FL = FALSE) AS NOT_READY_ROWS,
                       COUNT_IF(ROWCOUNT_STATUS = 'ROWCOUNT_OK') AS ROWCOUNT_OK_ROWS,
                       COUNT_IF(ROWCOUNT_STATUS IS NOT NULL AND ROWCOUNT_STATUS <> 'ROWCOUNT_OK') AS ROWCOUNT_BAD_ROWS,
                       MAX(DLVY_END_DATE) AS LATEST_DLVY_END_DATE,
                       MAX(LOADED_AT) AS LAST_LOADED_AT
                FROM {RAW_LIM_META_TABLE}
                WHERE PKG_GROUP_NAME IN ({placeholders})
                GROUP BY PKG_GROUP_NAME
                """,
                params,
            )
            meta_rows = normalize_rows(cur.fetchall())

            try:
                cur.execute(
                    f"""
                    SELECT PKG_GROUP_NAME,
                           COUNT(*) AS LATEST_LOG_ROWS,
                           COUNT_IF(STATUS = 'UPDATED') AS LATEST_UPDATED_ROWS,
                           COUNT_IF(STATUS IN ('STOPPED', 'FAILED', 'SKIPPED')) AS LATEST_ATTENTION_ROWS,
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
                history_params = dict(params)
                history_params["history_days"] = int(history_days)
                cur.execute(
                    f"""
                    SELECT PKG_GROUP_NAME,
                           COUNT(*) AS HISTORY_ROWS,
                           COUNT(DISTINCT TO_DATE(CONTROL_DATE)) AS HISTORY_DAYS,
                           COUNT_IF(STATUS = 'UPDATED') AS HISTORY_UPDATED_ROWS,
                           COUNT_IF(STATUS IN ('STOPPED', 'FAILED', 'SKIPPED')) AS HISTORY_ATTENTION_ROWS,
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

    overview = _build_overview(catalog, meta_rows, latest_rows, history_rows)
    subject_areas = len({str(row.get("SUBJECT_AREA") or "Unknown subject area") for row in catalog})
    return {
        "overview": overview,
        "summary": _build_summary(overview),
        "subjectAreas": subject_areas,
    }


def _load_raw_detail(group_name):
    rows = sf.query_service(
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
        ORDER BY DLVY_END_DATE DESC, DLVY_SOURCE_ID, FILE_NAME
        """,
        params={"group_name": group_name},
        use_warehouse=True,
        include_context=True,
    )
    return normalize_rows(rows)


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
            1 for row in rows if row.get("STATUS") in ("STOPPED", "FAILED", "SKIPPED")
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
            1 for row in rows if row.get("STATUS") in ("STOPPED", "FAILED", "SKIPPED")
        ),
    }


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


@file_ingestion_bp.get("/api/file-ingestion/<path:group_name>/raw")
def file_ingestion_raw(group_name):
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"ok": True, "source": "mock", "rows": [], "metrics": _raw_metrics([])})

    try:
        rows = _load_raw_detail(group_name)
        return jsonify({"ok": True, "source": "snowflake", "rows": rows, "metrics": _raw_metrics(rows)})
    except Exception as exc:
        current_app.logger.exception("Failed to load RAW LIM detail for %s", group_name)
        return _json_error(exc, 500)


@file_ingestion_bp.get("/api/file-ingestion/<path:group_name>/ready")
def file_ingestion_ready(group_name):
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"ok": True, "source": "mock", "rows": [], "metrics": _ready_metrics([])})

    try:
        rows = _load_ready_detail(group_name)
        return jsonify({"ok": True, "source": "snowflake", "rows": rows, "metrics": _ready_metrics(rows)})
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
        rows = _load_history_detail(group_name, history_days)
        return jsonify(
            {
                "ok": True,
                "source": "snowflake",
                "historyDays": history_days,
                "rows": rows,
                "metrics": _history_metrics(rows),
            }
        )
    except Exception as exc:
        current_app.logger.exception("Failed to load LIM history detail for %s", group_name)
        return _json_error(exc, 500)
