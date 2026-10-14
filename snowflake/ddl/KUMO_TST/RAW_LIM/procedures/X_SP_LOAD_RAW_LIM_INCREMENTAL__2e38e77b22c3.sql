-- Source: KUMO_TST.RAW_LIM.X_SP_LOAD_RAW_LIM_INCREMENTAL()
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_TST";
USE SCHEMA "KUMO_TST"."RAW_LIM";

CREATE OR REPLACE PROCEDURE KUMO_TST.RAW_LIM.X_SP_LOAD_RAW_LIM_INCREMENTAL()
RETURNS TABLE ("TABLE_NAME" VARCHAR, "FILE" VARCHAR, "STATUS" VARCHAR, "ROWS_PARSED" NUMBER(38,0), "ROWS_LOADED" NUMBER(38,0), "FIRST_ERROR" VARCHAR)
LANGUAGE SQL
COMMENT='user-defined procedure'
EXECUTE AS CALLER
AS '
BEGIN
    CREATE OR REPLACE TEMPORARY TABLE _load_results (
        table_name VARCHAR,
        file VARCHAR,
        status VARCHAR,
        rows_parsed INT,
        rows_loaded INT,
        error_limit INT,
        errors_seen INT,
        first_error VARCHAR,
        first_error_line INT,
        first_error_character INT,
        first_error_column_name VARCHAR
    );

    LET cur CURSOR FOR
        SELECT TABLE_NAME
        FROM KUMO_TST.INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = ''RAW_LIM''
          AND TABLE_NAME LIKE ''RAW_LIM_%''
          AND TABLE_TYPE = ''BASE TABLE''
        ORDER BY TABLE_NAME;

    FOR rec IN cur DO
        LET tbl VARCHAR := rec.TABLE_NAME;
        LET fmt VARCHAR := SUBSTRING(:tbl, 9);
        LET copy_sql VARCHAR :=
            ''COPY INTO KUMO_TST.RAW_LIM.'' || :tbl ||
            ''(DW_FILE_NM, DW_FILE_DTTM, DW_FILE_CHECK_SUM, DW_FILE_ROW_NUMBER, DW_LOAD_DTTM, DATA)
             FROM (
               SELECT
                 SPLIT_PART(METADATA$FILENAME, ''''/'''', -1) AS DW_FILE_NM,
                 METADATA$FILE_LAST_MODIFIED               AS DW_FILE_DTTM,
                 METADATA$FILE_CONTENT_KEY                 AS DW_FILE_CHECK_SUM,
                 METADATA$FILE_ROW_NUMBER                  AS DW_FILE_ROW_NUMBER,
                 SYSDATE()                                 AS DW_LOAD_DTTM,
                 $1                                        AS DATA
               FROM @KUMO_TST.META.AZURE_LIM_STAGE
             )
             FILE_FORMAT = (FORMAT_NAME = KUMO_TST.META.KUMO_LIM)
             PATTERN = ''''.*/*'' || :fmt || ''[^/]*$'''''';

        EXECUTE IMMEDIATE :copy_sql;
        LET qid VARCHAR := LAST_QUERY_ID();

        BEGIN
            INSERT INTO _load_results
                SELECT :tbl, * FROM TABLE(RESULT_SCAN(:qid));
        EXCEPTION
            WHEN OTHER THEN NULL;
        END;
    END FOR;

    LET res RESULTSET := (
        SELECT table_name, file, status, rows_parsed, rows_loaded, first_error
        FROM _load_results
        ORDER BY table_name
    );
    RETURN TABLE(res);
END;
';
