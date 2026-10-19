"""Read-only LIM batch summaries and their underlying files."""

import re

from flask import Blueprint, current_app, jsonify, request
from snowflake.connector import DictCursor

import config
import snowflake_client as sf
from utils import normalize_rows


batch_control_bp = Blueprint("batch_control", __name__)
TABLE = config.LIM_BATCH_CTRL_TABLE
COLUMNS = [
    {"key": key, "label": label, "type": kind}
    for key, label, kind in (
        ("DW_TABLE_NM", "Table", "text"),
        ("DW_FILE_NM", "File", "text"),
        ("DW_SRC_SYST", "Source", "text"),
        ("DW_VER_CTRL", "Version control", "text"),
        ("DW_VLD_FM_DTTM", "Valid from", "datetime"),
        ("NO_OF_RECS", "Records", "number"),
        ("READY_TO_LOAD_DTTM", "Ready to load", "datetime"),
        ("PROCESSED_DTTM", "Processed at", "datetime"),
        ("DW_BATCH_ID", "Batch ID", "text"),
        ("PROCESSED_FL", "Processed", "boolean"),
        ("DELETED_FL", "Deleted", "boolean"),
    )
]
COLUMN_KEYS = tuple(column["key"] for column in COLUMNS)
PRIMARY_KEY = ("DW_TABLE_NM", "DW_FILE_NM", "DW_SRC_SYST")
GROUP_COLUMNS = [
    {"key": key, "label": label, "type": kind}
    for key, label, kind in (
        ("LIM", "LIM format", "text"),
        ("READY_TO_LOAD_DTTM", "Ready to load", "datetime"),
        ("DW_VER_CTRL", "Version control", "text"),
        ("PROCESSED_DTTM", "Processed at", "datetime"),
        ("PROCESSED_FL", "Processed", "boolean"),
        ("DELETED_FL", "Deleted", "boolean"),
        ("FILES", "Files", "number"),
        ("RECS", "Records", "number"),
    )
]
# Build the same identity in both queries, before Python/JavaScript can round
# timestamp fractions. Include every grouping field, including nullable values.
GROUP_KEY_SQL = """SHA2(TO_JSON(ARRAY_CONSTRUCT(
    SUBSTR(DW_FILE_NM, 4, 7),
    TO_CHAR(READY_TO_LOAD_DTTM, 'YYYY-MM-DD HH24:MI:SS.FF9'),
    DW_VER_CTRL,
    TO_CHAR(PROCESSED_DTTM, 'YYYY-MM-DD HH24:MI:SS.FF9'),
    PROCESSED_FL, DELETED_FL
)), 256)"""
GROUP_QUERY = f"""SELECT
    {GROUP_KEY_SQL} AS GROUP_KEY,
    SUBSTR(DW_FILE_NM, 4, 7) AS LIM,
    READY_TO_LOAD_DTTM, DW_VER_CTRL, PROCESSED_DTTM, PROCESSED_FL, DELETED_FL,
    COUNT(*) AS FILES, SUM(NO_OF_RECS) AS RECS
FROM {TABLE}
GROUP BY ALL"""


def _search_clause(search, columns):
    if not search:
        return "", {}
    expressions = []
    for column in columns:
        key = column["key"]
        value = f'"{key}"'
        if column["type"] == "boolean":
            value = f"IFF({value}, 'Yes', 'No')"
        expressions.append(
            f"CONTAINS(LOWER(COALESCE(TO_VARCHAR({value}), '')), LOWER(%(search)s))"
        )
    return " WHERE (" + " OR ".join(expressions) + ")", {"search": search}


@batch_control_bp.get("/api/lim/batch-control/groups")
def batch_control_groups():
    return _browse(groups=True)


@batch_control_bp.get("/api/lim/batch-control")
def batch_control():
    return _browse(groups=False)


def _browse(groups):
    columns = GROUP_COLUMNS if groups else COLUMNS
    keys = tuple(column["key"] for column in columns)
    try:
        page = int(request.args.get("page", "1"))
        page_size = int(request.args.get("pageSize", "100"))
        sort_by = request.args.get("sortBy", "READY_TO_LOAD_DTTM")
        direction = request.args.get("sortDirection", "desc").lower()
        search = request.args.get("search", "").strip()
        group_key = request.args.get("groupKey")
        if page < 1 or not 1 <= page_size <= 500:
            raise ValueError("Page must be positive and page size must be between 1 and 500.")
        if sort_by not in keys or direction not in ("asc", "desc"):
            raise ValueError("Invalid sort column or direction.")
        if len(search) > 500:
            raise ValueError("Search must be at most 500 characters.")
        if group_key is not None and (groups or not re.fullmatch(r"[0-9a-f]{64}", group_key)):
            raise ValueError("Invalid batch group key.")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    if config.USE_MOCK or not sf.is_configured():
        return jsonify({
            "ok": True, "source": "mock", "columns": columns, "rows": [],
            "total": 0, "page": 1, "pageSize": page_size,
        })

    # Only allowlisted identifiers enter SQL. CONTAINS treats %, _ and quotes
    # in the bound search value literally rather than as LIKE wildcards.
    where, params = _search_clause(search, columns)
    if group_key is not None:
        where += (" AND " if where else " WHERE ") + GROUP_KEY_SQL + " = %(group_key)s"
        params["group_key"] = group_key
    order = f'"{sort_by}" {direction.upper()} NULLS LAST'
    if groups:
        order += "".join(
            f', "{key}" {direction} NULLS LAST'
            for key, direction in (
                ("READY_TO_LOAD_DTTM", "DESC"), ("LIM", "ASC"),
                ("PROCESSED_DTTM", "DESC"), ("GROUP_KEY", "ASC"),
            ) if key != sort_by
        )
    else:
        order += "".join(f', "{key}" ASC' for key in PRIMARY_KEY if key != sort_by)
    selected = ", ".join(f'"{key}"' for key in keys)
    if groups:
        selected += ', "GROUP_KEY"'
    prefix = f"WITH BATCH_GROUPS AS ({GROUP_QUERY}) " if groups else ""
    source = "BATCH_GROUPS" if groups else TABLE

    try:
        with sf.connection(use_warehouse=True, include_context=True, force_service=True) as conn:
            cur = conn.cursor(DictCursor)
            try:
                cur.execute(f"{prefix}SELECT COUNT(*) AS TOTAL FROM {source}{where}", params)
                total = int(cur.fetchone()["TOTAL"])
                page = min(page, max(1, (total + page_size - 1) // page_size))
                cur.execute(
                    f"{prefix}SELECT {selected} FROM {source}{where} ORDER BY {order} "
                    "LIMIT %(limit)s OFFSET %(offset)s",
                    {**params, "limit": page_size, "offset": (page - 1) * page_size},
                )
                rows = normalize_rows(cur.fetchall())
            finally:
                cur.close()
        return jsonify({
            "ok": True, "source": "snowflake", "columns": columns, "rows": rows,
            "total": total, "page": page, "pageSize": page_size,
        })
    except Exception as exc:
        current_app.logger.exception("Failed to read LIM batch control")
        return jsonify({"ok": False, "error": str(exc)}), 500
