"""Read-only browsing of LIM batch control, with server-side filtering."""

from flask import Blueprint, current_app, jsonify, request
from snowflake.connector import DictCursor

import config
import snowflake_client as sf
from utils import normalize_rows


batch_control_bp = Blueprint("batch_control", __name__)
TABLE = "KUMO_TST.META.LIM_BATCH_CTRL"
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


@batch_control_bp.get("/api/lim/batch-control")
def batch_control():
    try:
        page = int(request.args.get("page", "1"))
        page_size = int(request.args.get("pageSize", "100"))
        sort_by = request.args.get("sortBy", "READY_TO_LOAD_DTTM")
        direction = request.args.get("sortDirection", "desc").lower()
        search = request.args.get("search", "").strip()
        if page < 1 or not 1 <= page_size <= 500:
            raise ValueError("Page must be positive and page size must be between 1 and 500.")
        if sort_by not in COLUMN_KEYS or direction not in ("asc", "desc"):
            raise ValueError("Invalid sort column or direction.")
        if len(search) > 500:
            raise ValueError("Search must be at most 500 characters.")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    if config.USE_MOCK or not sf.is_configured():
        return jsonify({
            "ok": True, "source": "mock", "columns": COLUMNS, "rows": [],
            "total": 0, "page": 1, "pageSize": page_size,
        })

    # Only allowlisted identifiers enter SQL. CONTAINS treats %, _ and quotes
    # in the bound search value literally rather than as LIKE wildcards.
    params = {}
    where = ""
    if search:
        params["search"] = search
        where = " WHERE (" + " OR ".join(
            f'CONTAINS(LOWER(COALESCE(TO_VARCHAR("{key}"), \'\')), LOWER(%(search)s))'
            for key in COLUMN_KEYS
        ) + ")"
    order = f'"{sort_by}" {direction.upper()} NULLS LAST'
    order += "".join(f', "{key}" ASC' for key in PRIMARY_KEY if key != sort_by)
    selected = ", ".join(f'"{key}"' for key in COLUMN_KEYS)

    try:
        with sf.connection(use_warehouse=True, include_context=True, force_service=True) as conn:
            cur = conn.cursor(DictCursor)
            try:
                cur.execute(f"SELECT COUNT(*) AS TOTAL FROM {TABLE}{where}", params)
                total = int(cur.fetchone()["TOTAL"])
                page = min(page, max(1, (total + page_size - 1) // page_size))
                cur.execute(
                    f"SELECT {selected} FROM {TABLE}{where} ORDER BY {order} "
                    "LIMIT %(limit)s OFFSET %(offset)s",
                    {**params, "limit": page_size, "offset": (page - 1) * page_size},
                )
                rows = normalize_rows(cur.fetchall())
            finally:
                cur.close()
        return jsonify({
            "ok": True, "source": "snowflake", "columns": COLUMNS, "rows": rows,
            "total": total, "page": page, "pageSize": page_size,
        })
    except Exception as exc:
        current_app.logger.exception("Failed to read LIM batch control")
        return jsonify({"ok": False, "error": str(exc)}), 500
