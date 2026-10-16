"""Clear the selected subject's batch control only during a full RAW LIM reload.

This migration replaces the procedure definition; it never executes a reload.
Refresh saved definitions afterwards with python snowflake/export_ddl.py.
"""

from snowflake.connector import DictCursor

import snowflake_client as sf


PROCEDURE = "KUMO_TST.META.LOAD_RAW_LIM_FROM_STAGE(VARCHAR, DATE, DATE, BOOLEAN, BOOLEAN, BOOLEAN)"
MARKER = "RAW_LIM_FULL_RELOAD_BATCH_CTRL_V1"
LEGACY_CONDITION = "             WHERE SPLIT_PART(UPPER(DW_TABLE_NM), ''.'', -1) = :V_TARGET_TABLE_SHORT;"
SCHEMA_CONDITION = """             WHERE SPLIT_PART(UPPER(DW_TABLE_NM), ''.'', -2) = ''RAW_LIM''
               AND SPLIT_PART(UPPER(DW_TABLE_NM), ''.'', -1) = :V_TARGET_TABLE_SHORT;"""
PATCHES = (
    (
        "    V_RAW_LIM_META_DELETED       NUMBER := 0;\n",
        "    V_RAW_LIM_META_DELETED       NUMBER := 0;\n"
        "    V_LIM_BATCH_CTRL_DELETED     NUMBER := 0;\n",
    ),
    (
        "        -- 12d. Delete RAW_LOAD_RESULTS\n",
        """        -- RAW_LIM_FULL_RELOAD_BATCH_CTRL_V1
        -- Clear all dates/statuses for this RAW subject, including qualified
        -- DW_TABLE_NM values retained by earlier batch registration versions.
        IF (V_FULL_RELOAD) THEN
            DELETE FROM META.LIM_BATCH_CTRL
             WHERE SPLIT_PART(UPPER(DW_TABLE_NM), ''.'', -2) = ''RAW_LIM''
               AND SPLIT_PART(UPPER(DW_TABLE_NM), ''.'', -1) = :V_TARGET_TABLE_SHORT;
            V_LIM_BATCH_CTRL_DELETED := SQLROWCOUNT;
        END IF;

        -- 12d. Delete RAW_LOAD_RESULTS
""",
    ),
    (
        "        ''RAW_LIM_META_DELETED'', V_RAW_LIM_META_DELETED,\n",
        "        ''RAW_LIM_META_DELETED'', V_RAW_LIM_META_DELETED,\n"
        "        ''LIM_BATCH_CTRL_DELETED'', V_LIM_BATCH_CTRL_DELETED,\n",
    ),
)


def patch_definition(ddl):
    if MARKER in ddl:
        # Upgrade the earlier cleanup that matched only the table component.
        if ddl.count(LEGACY_CONDITION) == 1:
            ddl = ddl.replace(LEGACY_CONDITION, SCHEMA_CONDITION, 1)
        if any(ddl.count(new) != 1 for _, new in PATCHES):
            raise RuntimeError("Full-reload batch cleanup is incomplete; procedure was not changed")
        return ddl
    if any(ddl.count(old) != 1 for old, _ in PATCHES):
        raise RuntimeError("LOAD_RAW_LIM_FROM_STAGE has drifted; procedure was not changed")
    for old, new in PATCHES:
        ddl = ddl.replace(old, new, 1)
    return ddl


def apply():
    with sf.connection(use_warehouse=False, include_context=False, force_service=True) as conn:
        cur = conn.cursor(DictCursor)
        try:
            cur.execute("SELECT GET_DDL('PROCEDURE', %s, TRUE) AS DDL", (PROCEDURE,))
            original = cur.fetchone()["DDL"]
            expected = patch_definition(original)
            if expected == original:
                return "already applied"
            if expected.count("\nRETURNS VARIANT\n") != 1:
                raise RuntimeError("Unexpected procedure return declaration")
            updated = expected.replace("\nRETURNS VARIANT\n", "\nCOPY GRANTS\nRETURNS VARIANT\n", 1)
            cur.execute(updated)
            cur.execute("SELECT GET_DDL('PROCEDURE', %s, TRUE) AS DDL", (PROCEDURE,))
            if cur.fetchone()["DDL"].replace("\nCOPY GRANTS", "", 1) != expected:
                raise RuntimeError("Deployed definition differs from the intended batch-control patch")
            return "applied"
        finally:
            cur.close()


if __name__ == "__main__":
    print(apply())
