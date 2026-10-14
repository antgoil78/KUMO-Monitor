-- Source: KUMO_ADMIN.META.SP_FIX_VERSIONING(VARCHAR, VARCHAR, UUID, UUID)
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_ADMIN";
USE SCHEMA "KUMO_ADMIN"."META";

CREATE OR REPLACE PROCEDURE KUMO_ADMIN.META.SP_FIX_VERSIONING("TABLE_NAME" VARCHAR DEFAULT 'INVALID TABLE', "DBT_DB" VARCHAR DEFAULT CURRENT_DATABASE(), "RUN_ID" UUID DEFAULT null, "DBT_INVOCATION_ID" UUID DEFAULT null)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS CALLER
AS '
DECLARE
    v_object_type   VARCHAR;
    v_business_key  VARCHAR;
    v_surrogate_key VARCHAR;
    v_schema        VARCHAR;
    v_key_columns   VARCHAR;
    v_join_cond     VARCHAR;
    v_fqn           VARCHAR;
    v_sql           VARCHAR;
    v_rows_updated  INTEGER;
    v_message       VARCHAR;
    v_meta_uk       VARCHAR;
    v_meta_rl       VARCHAR;
    duplicate_error EXCEPTION (-20001, ''Duplicate key+DW_VLD_FM_DTTM found'');
BEGIN
    v_meta_uk := :dbt_db || ''.META.UNIQUE_KEY'';
    v_meta_rl := :dbt_db || ''.META.RUN_LOG'';

    SELECT OBJECT_TYPE, BUSINESS_KEY, SURROGATE_KEY
      INTO :v_object_type, :v_business_key, :v_surrogate_key
      FROM IDENTIFIER(:v_meta_uk) uk
     WHERE UPPER(TRIM(uk.table_name)) = UPPER(TRIM(:table_name));

    IF (v_object_type IS NULL) THEN
        RETURN ''ERROR: Table '' || :table_name || '' not found in META.UNIQUE_KEY'';
    END IF;

    IF (LOWER(v_object_type) = ''sdltable'') THEN
        v_schema := ''SDL'';
        v_key_columns := v_business_key;
    ELSEIF (LOWER(v_object_type) IN (''satellite'', ''linksatellite'')) THEN
        v_schema := ''EDV'';
        v_key_columns := v_surrogate_key;
    ELSE
        RETURN ''ERROR: Unsupported object_type "'' || v_object_type || ''" for table '' || :table_name;
    END IF;

    SELECT LISTAGG(''tgt.'' || TRIM(value) || '' = src.'' || TRIM(value), '' AND '')
      INTO :v_join_cond
      FROM TABLE(SPLIT_TO_TABLE(:v_key_columns, '',''));

    v_fqn := :dbt_db || ''.'' || v_schema || ''.'' || :table_name;

    v_sql := ''SELECT COUNT(*) AS DUP_CNT FROM (
        SELECT '' || v_key_columns || '', DW_VLD_FM_DTTM, COUNT(*) AS CNT
          FROM '' || v_fqn || ''
         GROUP BY '' || v_key_columns || '', DW_VLD_FM_DTTM
        HAVING CNT > 1)'';
    EXECUTE IMMEDIATE v_sql;
    SELECT "DUP_CNT" INTO :v_rows_updated FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (v_rows_updated > 0) THEN
        v_message := ''ERROR: '' || v_rows_updated || '' duplicate key+DW_VLD_FM_DTTM combinations found in '' || v_fqn || ''. Remove duplicates before fixing versioning.'';
        IF (:run_id IS NOT NULL AND :dbt_invocation_id IS NOT NULL) THEN
            INSERT INTO IDENTIFIER(:v_meta_rl) (RUN_ID, DBT_INVOCATION_ID, TYPE, ORIGIN, MESSAGE)
            VALUES (:run_id, :dbt_invocation_id, ''INFO'', UPPER(:table_name), :v_message);
            RAISE duplicate_error;
        ELSE
            RETURN v_message;
        END IF;
    END IF;

    v_sql := ''
MERGE INTO '' || v_fqn || '' AS tgt
USING (
    SELECT
        '' || v_key_columns || '',
        DW_VLD_FM_DTTM,
        COALESCE(
            LEAD(DW_VLD_FM_DTTM) OVER (PARTITION BY '' || v_key_columns || '' ORDER BY DW_VLD_FM_DTTM),
            ''''2100-01-01''''::TIMESTAMP_NTZ
        ) AS NEW_VLD_TO,
        CASE
            WHEN LEAD(DW_VLD_FM_DTTM) OVER (PARTITION BY '' || v_key_columns || '' ORDER BY DW_VLD_FM_DTTM) IS NULL
            THEN TRUE ELSE FALSE
        END AS NEW_CRNT_FL
    FROM '' || v_fqn || ''
) AS src
ON  tgt.DW_VLD_FM_DTTM = src.DW_VLD_FM_DTTM
AND '' || v_join_cond || ''
WHEN MATCHED AND (tgt.DW_VLD_TO_DTTM != src.NEW_VLD_TO OR tgt.DW_CRNT_FL != src.NEW_CRNT_FL) THEN
    UPDATE SET
        tgt.DW_VLD_TO_DTTM = src.NEW_VLD_TO,
        tgt.DW_CRNT_FL     = src.NEW_CRNT_FL'';

    EXECUTE IMMEDIATE v_sql;

    SELECT "number of rows updated" INTO :v_rows_updated FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));

    IF (v_rows_updated = 0) THEN
        v_message := ''SP_FIX_VERSIONING - no invalid records found'';
    ELSE
        v_message := ''SP_FIX_VERSIONING - '' || v_rows_updated || '' invalid records fixed'';
    END IF;

    IF (:run_id IS NOT NULL AND :dbt_invocation_id IS NOT NULL) THEN
        INSERT INTO IDENTIFIER(:v_meta_rl) (RUN_ID, DBT_INVOCATION_ID, TYPE, ORIGIN, MESSAGE)
        VALUES (:run_id, :dbt_invocation_id, ''INFO'', UPPER(:table_name), :v_message);
    ELSE
        v_message := REPLACE(v_message, ''SP_FIX_VERSIONING'', :table_name);
    END IF;

    RETURN v_message;
END;
';
