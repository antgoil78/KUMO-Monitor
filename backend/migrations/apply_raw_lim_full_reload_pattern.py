"""Use PATTERN for full RAW LIM reloads while retaining FILES for other modes."""

from snowflake.connector import DictCursor

import snowflake_client as sf


PROCEDURE = "KUMO_TST.META.LOAD_RAW_LIM_FROM_STAGE(VARCHAR, DATE, DATE, BOOLEAN, BOOLEAN, BOOLEAN)"
MARKER = "RAW_LIM_FULL_RELOAD_PATTERN_V1"

START_MARKER = "    -- 15. RAW_LIM_COPY_SELECTED_FILES_V1\n"
END_MARKER = "    -- RAW_LIM_SET_READY_SELECTED_FILES_V1\n"

COPY_REPLACEMENT = """    -- 15. RAW_LIM_FULL_RELOAD_PATTERN_V1
    -- Full reload performance test: use one subject-wide PATTERN COPY. Partial
    -- and normal loads retain exact FILES batches to preserve their selection.
    IF (V_FULL_RELOAD) THEN
        V_COPY_SQL :=
              ''COPY INTO '' || V_TARGET_TABLE
            || '' (DW_FILE_NM, DW_FILE_DTTM, DW_FILE_CHECK_SUM, DW_FILE_ROW_NUMBER, DW_LOAD_DTTM, DATA)''
            || '' FROM (SELECT''
            || ''   SPLIT_PART(METADATA$FILENAME, ''''/'''', -1)''
            || '' , METADATA$FILE_LAST_MODIFIED''
            || '' , METADATA$FILE_CONTENT_KEY''
            || '' , METADATA$FILE_ROW_NUMBER''
            || '' , SYSDATE()''
            || '' , $1''
            || '' FROM '' || V_STAGE_REF || '')''
            || '' FILE_FORMAT = (FORMAT_NAME = '''''' || V_FILE_FORMAT || '''''')''
            || '' PATTERN = '''''' || V_LIST_PATTERN || ''''''''
            || V_FORCE_FLAG
            || '' ON_ERROR = ABORT_STATEMENT'';

        EXECUTE IMMEDIATE :V_COPY_SQL;
        V_COPY_COMMANDS := 1;
    ELSE
        LET V_FILE_BATCHES RESULTSET := (
            SELECT
                BATCH_NO,
                LISTAGG(
                    '''''''' || REPLACE(FILE_PATH, '''''''', '''''''''''') || '''''''',
                    '',''
                ) WITHIN GROUP (ORDER BY FILE_PATH) AS FILE_LIST
            FROM (
                SELECT
                    FILE_PATH,
                    FLOOR((ROW_NUMBER() OVER (ORDER BY FILE_PATH) - 1) / 1000) AS BATCH_NO
                FROM TMP_RAW_LIM_FILES
            )
            GROUP BY BATCH_NO
        );

        FOR V_FILE_BATCH IN V_FILE_BATCHES DO
            V_COPY_SQL :=
                  ''COPY INTO '' || V_TARGET_TABLE
                || '' (DW_FILE_NM, DW_FILE_DTTM, DW_FILE_CHECK_SUM, DW_FILE_ROW_NUMBER, DW_LOAD_DTTM, DATA)''
                || '' FROM (SELECT''
                || ''   SPLIT_PART(METADATA$FILENAME, ''''/'''', -1)''
                || '' , METADATA$FILE_LAST_MODIFIED''
                || '' , METADATA$FILE_CONTENT_KEY''
                || '' , METADATA$FILE_ROW_NUMBER''
                || '' , SYSDATE()''
                || '' , $1''
                || '' FROM '' || V_STAGE_REF || '')''
                || '' FILES = ('' || V_FILE_BATCH.FILE_LIST || '')''
                || '' FILE_FORMAT = (FORMAT_NAME = '''''' || V_FILE_FORMAT || '''''')''
                || V_FORCE_FLAG
                || '' ON_ERROR = ABORT_STATEMENT'';

            EXECUTE IMMEDIATE :V_COPY_SQL;
            V_COPY_COMMANDS := V_COPY_COMMANDS + 1;
        END FOR;
    END IF;

"""


def apply():
    with sf.connection(use_warehouse=True, include_context=True, force_service=True) as conn:
        cur = conn.cursor(DictCursor)
        try:
            cur.execute("SELECT GET_DDL('PROCEDURE', %s) AS DDL", (PROCEDURE,))
            ddl = cur.fetchone()["DDL"]
            if MARKER in ddl:
                return "already applied"

            start_count = ddl.count(START_MARKER)
            end_count = ddl.count(END_MARKER)
            if start_count != 1 or end_count != 1:
                raise RuntimeError(
                    "LOAD_RAW_LIM_FROM_STAGE COPY markers did not match exactly once; "
                    "the procedure has drifted and was not changed"
                )

            start = ddl.index(START_MARKER)
            end = ddl.index(END_MARKER, start)
            ddl = ddl[:start] + COPY_REPLACEMENT + ddl[end:]
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
