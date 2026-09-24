"""Make LOAD_RAW_LIM_FROM_STAGE honor P_SET_READY_TO_LOAD."""

from snowflake.connector import DictCursor

import snowflake_client as sf


PROCEDURE = "KUMO_TST.META.LOAD_RAW_LIM_FROM_STAGE(VARCHAR, DATE, DATE, BOOLEAN, BOOLEAN, BOOLEAN)"
MARKER = "RAW_LIM_SET_READY_SELECTED_FILES_V1"


def apply():
    with sf.connection(use_warehouse=True, include_context=True, force_service=True) as conn:
        cur = conn.cursor(DictCursor)
        try:
            cur.execute("SELECT GET_DDL('PROCEDURE', %s) AS DDL", (PROCEDURE,))
            ddl = cur.fetchone()["DDL"]
            if MARKER in ddl:
                return "already applied"

            needle = """    EXECUTE IMMEDIATE :V_COPY_SQL;
    V_COPY_COMMANDS := 1;
"""
            replacement = needle + """
    -- RAW_LIM_SET_READY_SELECTED_FILES_V1
    IF (V_SET_READY_TO_LOAD) THEN
        UPDATE IDENTIFIER(:V_TARGET_TABLE)
           SET DW_READY_TO_LOAD_FL = TRUE
         WHERE DW_FILE_NM IN (SELECT FILE_NM FROM TMP_RAW_LIM_FILES)
           AND COALESCE(DW_READY_TO_LOAD_FL, FALSE) = FALSE;
        V_ROWS_READY_UPDATED := SQLROWCOUNT;
    END IF;
"""
            if needle not in ddl:
                raise RuntimeError("LOAD_RAW_LIM_FROM_STAGE COPY marker did not match the expected version")

            ddl = ddl.replace(needle, replacement, 1)
            ddl = ddl.replace(
                'CREATE OR REPLACE PROCEDURE "LOAD_RAW_LIM_FROM_STAGE"',
                'CREATE OR REPLACE PROCEDURE KUMO_TST.META."LOAD_RAW_LIM_FROM_STAGE"',
                1,
            )
            cur.execute(ddl)
            return "applied"
        finally:
            cur.close()


if __name__ == "__main__":
    print(apply())
