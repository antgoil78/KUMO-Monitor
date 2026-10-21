-- Source: KUMO_DEV.MONITOR_APP.SP_LOAD_RAW_LIM_INCREMENTAL_PERSIST(VARCHAR)
-- Refresh: python snowflake/export_ddl.py --object PROCEDURE 'KUMO_DEV.MONITOR_APP.SP_LOAD_RAW_LIM_INCREMENTAL_PERSIST(VARCHAR)'
USE DATABASE "KUMO_DEV";
USE SCHEMA "KUMO_DEV"."MONITOR_APP";

CREATE OR REPLACE PROCEDURE KUMO_DEV.MONITOR_APP.SP_LOAD_RAW_LIM_INCREMENTAL_PERSIST("P_DATABASE" VARCHAR DEFAULT null)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS CALLER
AS '
DECLARE
  V_ENV VARIANT;

    V_DATABASE VARCHAR DEFAULT UPPER(TRIM(P_DATABASE));
    V_INFORMATION_TABLES VARCHAR;
    V_TABLES RESULTSET;
    E_INVALID_DATABASE EXCEPTION (-20002, ''Invalid database identifier'');
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



BEGIN
  CALL KUMO_DEV.MONITOR_META.SP_GET_ENVIRONMENT() INTO :V_ENV;
  V_DATABASE := COALESCE(V_DATABASE, V_ENV:DATABASE::VARCHAR);

    IF (V_DATABASE IS NULL OR V_DATABASE <> V_ENV:DATABASE::VARCHAR) THEN
        RAISE E_INVALID_DATABASE;
    END IF;
    V_INFORMATION_TABLES := V_DATABASE || ''.INFORMATION_SCHEMA.TABLES'';
    V_TABLES := (SELECT TABLE_NAME
        FROM IDENTIFIER(:V_INFORMATION_TABLES)
        WHERE TABLE_SCHEMA = :V_ENV:RAW_SCHEMA::VARCHAR
          AND TABLE_NAME LIKE ''RAW_LIM_%''
          AND TABLE_TYPE = ''BASE TABLE''
        ORDER BY TABLE_NAME);

    FOR rec IN V_TABLES DO

        v_tbl := rec.TABLE_NAME;
        v_fmt := SUBSTRING(v_tbl, 9);
        v_pattern := ''^(?!.*(?i:deleted_)).*'' || v_fmt || ''[^/]*$'';
        v_copy_ok := FALSE;
        v_audit_rows := 0;

        v_table_ref :=
            ''"'' || V_DATABASE || ''"."'' || V_ENV:RAW_SCHEMA::VARCHAR || ''".'' ||
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
                 FROM @'' || V_DATABASE || ''.'' || V_ENV:APP_SCHEMA::VARCHAR || ''.AZURE_LIM_STAGE
             )
             FILE_FORMAT = (FORMAT_NAME = '' || V_DATABASE || ''.'' || V_ENV:APP_SCHEMA::VARCHAR || ''.KUMO_LIM)
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
