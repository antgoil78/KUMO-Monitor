"""Load the exact selected RAW LIM files instead of rescanning with PATTERN.

Snowflake limits COPY INTO FILES to 1,000 names, so the procedure batches the
rows already selected in TMP_RAW_LIM_FILES.  The migration patches the live
procedure DDL and deliberately fails if its expected source blocks have drifted.
"""

from snowflake.connector import DictCursor

import snowflake_client as sf


PROCEDURE = "KUMO_TST.META.LOAD_RAW_LIM_FROM_STAGE(VARCHAR, DATE, DATE, BOOLEAN, BOOLEAN, BOOLEAN)"
MARKER = "RAW_LIM_COPY_SELECTED_FILES_V1"

STAGE_DECLARATION_NEEDLE = """    V_STAGE_REF            VARCHAR;
    V_FILE_FORMAT          VARCHAR;
"""

STAGE_DECLARATION_REPLACEMENT = """    V_STAGE_REF            VARCHAR;
    V_STAGE_URL            VARCHAR;
    V_STAGE_DESC_QUERY_ID  VARCHAR;
    V_FILE_FORMAT          VARCHAR;
"""

STAGE_SETUP_NEEDLE = """    V_STAGE_REF := ''@'' || V_DB || ''.META.AZURE_LIM_STAGE'';
    V_FILE_FORMAT := V_DB || ''.META.KUMO_LIM'';
"""

STAGE_SETUP_REPLACEMENT = """    V_STAGE_REF := ''@'' || V_DB || ''.META.AZURE_LIM_STAGE'';
    V_FILE_FORMAT := V_DB || ''.META.KUMO_LIM'';

    -- LIST returns full external URLs, while COPY FILES requires paths relative
    -- to the stage URL. Read that URL once so nested dataset folders are kept.
    EXECUTE IMMEDIATE ''DESC STAGE '' || SUBSTR(V_STAGE_REF, 2);
    V_STAGE_DESC_QUERY_ID := SQLID;
    SELECT RTRIM(PARSE_JSON("property_value")[0]::VARCHAR, ''/'')
      INTO :V_STAGE_URL
      FROM TABLE(RESULT_SCAN(:V_STAGE_DESC_QUERY_ID))
     WHERE UPPER("property") = ''URL'';
"""

FILE_SELECTION_NEEDLE = """    INSERT INTO TMP_RAW_LIM_FILES (FILE_PATH, FILE_NM, DLVY_END_DATE)
    SELECT
          "name",
          SPLIT_PART("name", ''/'', -1),
          TRY_TO_DATE(SPLIT_PART(SPLIT_PART("name", ''/'', -1), ''_'', -2), ''YYYYMMDD'')
      FROM TABLE(RESULT_SCAN(:V_LIST_QUERY_ID));
"""

FILE_SELECTION_REPLACEMENT = """    INSERT INTO TMP_RAW_LIM_FILES (FILE_PATH, FILE_NM, DLVY_END_DATE)
    SELECT
          IFF(
              STARTSWITH("name", :V_STAGE_URL || ''/''),
              SUBSTR("name", LENGTH(:V_STAGE_URL) + 2),
              "name"
          ),
          SPLIT_PART("name", ''/'', -1),
          TRY_TO_DATE(SPLIT_PART(SPLIT_PART("name", ''/'', -1), ''_'', -2), ''YYYYMMDD'')
      FROM TABLE(RESULT_SCAN(:V_LIST_QUERY_ID));
"""

FORCE_NEEDLE = """    -- 14. FORCE flag
    IF (V_RELOAD AND NOT V_FULL_RELOAD) THEN
        V_FORCE_FLAG := '' FORCE = TRUE'';
    ELSE
        V_FORCE_FLAG := '''';
    END IF;
"""

FORCE_REPLACEMENT = """    -- 14. FORCE flag. Reloads must ignore retained COPY load history.
    IF (V_RELOAD) THEN
        V_FORCE_FLAG := '' FORCE = TRUE'';
    ELSE
        V_FORCE_FLAG := '''';
    END IF;
"""

COPY_NEEDLE = """    -- 15. COPY INTO (transform with METADATA$ columns)
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
        || '' ON_ERROR = CONTINUE'';

    EXECUTE IMMEDIATE :V_COPY_SQL;
    V_COPY_COMMANDS := 1;

"""

COPY_REPLACEMENT = """    -- 15. RAW_LIM_COPY_SELECTED_FILES_V1
    -- COPY only the files selected above. FILES accepts at most 1,000 names,
    -- so large full reloads are split into deterministic batches.
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
"""


def _replace_once(ddl, needle, replacement, label):
    count = ddl.count(needle)
    if count != 1:
        raise RuntimeError(
            f"LOAD_RAW_LIM_FROM_STAGE {label} block matched {count} times; "
            "the procedure has drifted and was not changed"
        )
    return ddl.replace(needle, replacement, 1)


def apply():
    with sf.connection(use_warehouse=True, include_context=True, force_service=True) as conn:
        cur = conn.cursor(DictCursor)
        try:
            cur.execute("SELECT GET_DDL('PROCEDURE', %s) AS DDL", (PROCEDURE,))
            ddl = cur.fetchone()["DDL"]
            if MARKER in ddl:
                return "already applied"

            ddl = _replace_once(
                ddl, STAGE_DECLARATION_NEEDLE, STAGE_DECLARATION_REPLACEMENT, "stage declaration"
            )
            ddl = _replace_once(ddl, STAGE_SETUP_NEEDLE, STAGE_SETUP_REPLACEMENT, "stage setup")
            ddl = _replace_once(
                ddl, FILE_SELECTION_NEEDLE, FILE_SELECTION_REPLACEMENT, "file selection"
            )
            ddl = _replace_once(ddl, FORCE_NEEDLE, FORCE_REPLACEMENT, "FORCE")
            ddl = _replace_once(ddl, COPY_NEEDLE, COPY_REPLACEMENT, "COPY")
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
