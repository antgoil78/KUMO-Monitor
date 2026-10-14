-- Source: KUMO_TST.RAW_LIM.SP_LOAD_RAW_LIM_INCREMENTAL_PERSIST()
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_TST";
USE SCHEMA "KUMO_TST"."RAW_LIM";

CREATE OR REPLACE PROCEDURE KUMO_TST.RAW_LIM.SP_LOAD_RAW_LIM_INCREMENTAL_PERSIST()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS CALLER
AS '
DECLARE
    v_run_ts          TIMESTAMP_NTZ DEFAULT SYSDATE();
    v_table_start_ts  TIMESTAMP_NTZ;
    v_tbl             VARCHAR;
    v_fmt             VARCHAR;
    v_pattern         VARCHAR;
    v_table_ref       VARCHAR;
    v_copy_sql        VARCHAR;
    v_ins_sql         VARCHAR;
    v_upd_sql         VARCHAR;
    v_err_msg         VARCHAR;
    v_audit_rows      NUMBER DEFAULT 0;
    v_copy_ok         BOOLEAN DEFAULT FALSE;
    v_res             RESULTSET;

    cur CURSOR FOR
        SELECT TABLE_NAME
        FROM KUMO_TST.INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = ''RAW_LIM''
          AND TABLE_NAME LIKE ''RAW_LIM_%''
          AND TABLE_TYPE = ''BASE TABLE''
        ORDER BY TABLE_NAME;

BEGIN

    FOR rec IN cur DO

        v_tbl := rec.TABLE_NAME;
        v_fmt := SUBSTRING(v_tbl, 9);
        v_pattern := ''.*'' || v_fmt || ''[^/]*$'';
        v_copy_ok := FALSE;
        v_audit_rows := 0;

        v_table_ref :=
            ''"KUMO_TST"."RAW_LIM".'' ||
            ''"'' || REPLACE(v_tbl, ''"'', ''""'') || ''"'';

        v_copy_sql :=
            ''COPY INTO '' || v_table_ref || ''
             (
                 DW_FILE_NM,
                 DW_FILE_DTTM,
                 DW_FILE_CHECK_SUM,
                 DW_FILE_ROW_NUMBER,
                 DW_LOAD_DTTM,
                 DATA
             )
             FROM (
                 SELECT
                     SPLIT_PART(METADATA$FILENAME, ''''/'''', -1) AS DW_FILE_NM,
                     METADATA$FILE_LAST_MODIFIED              AS DW_FILE_DTTM,
                     METADATA$FILE_CONTENT_KEY                AS DW_FILE_CHECK_SUM,
                     METADATA$FILE_ROW_NUMBER                 AS DW_FILE_ROW_NUMBER,
                     SYSDATE()                                AS DW_LOAD_DTTM,
                     $1                                       AS DATA
                 FROM @KUMO_TST.META.AZURE_LIM_STAGE
             )
             FILE_FORMAT = (FORMAT_NAME = KUMO_TST.META.KUMO_LIM)
             PATTERN = '' || CHR(39) || REPLACE(v_pattern, CHR(39), CHR(39) || CHR(39)) || CHR(39);

        BEGIN
            v_table_start_ts := SYSDATE();
            EXECUTE IMMEDIATE v_copy_sql;
        EXCEPTION
            WHEN OTHER THEN
                v_err_msg := SQLERRM;
        END;

    END FOR;

    RETURN ''Done'';

END;
';
