-- Source: KUMO_ADMIN.META.SP_CHECK_VERSIONING(VARCHAR, VARCHAR, VARCHAR)
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_ADMIN";
USE SCHEMA "KUMO_ADMIN"."META";

CREATE OR REPLACE PROCEDURE KUMO_ADMIN.META.SP_CHECK_VERSIONING("DBT_DB" VARCHAR DEFAULT CURRENT_DATABASE(), "TABLE_SCHEMA" VARCHAR DEFAULT null, "TABLE_NAME" VARCHAR DEFAULT null)
RETURNS TABLE ("TABLE_NAME" VARCHAR, "KEY_COLUMNS" VARCHAR, "INCORRECT_RECORDS" NUMBER(38,0), "SAMPLE_KEYS" VARCHAR)
LANGUAGE SQL
EXECUTE AS CALLER
AS '
-- Validates versioning integrity (DW_VLD_FM_DTTM, DW_VLD_TO_DTTM, DW_CRNT_FL)
-- for SDL tables, satellites, and link-satellites by comparing each record''s
-- valid-to date and current flag against the expected values derived from LEAD().
-- Returns one row per table that has mismatches, along with a count and sample keys.
DECLARE
    v_meta_uk       VARCHAR DEFAULT :dbt_db || ''.META.UNIQUE_KEY'';
    v_fqn           VARCHAR;
    v_schema        VARCHAR;
    v_key_columns   VARCHAR;
    v_sql           VARCHAR;
    v_tbl           VARCHAR;
    v_object_type   VARCHAR;
    v_business_key  VARCHAR;
    v_surrogate_key VARCHAR;
    v_count         INTEGER;
    v_samples       VARCHAR;
    v_type_filter   VARCHAR;
    invalid_table_schema EXCEPTION (-20001, ''Invalid table_schema. Allowed values: SDL, EDV or NULL'');
    res RESULTSET;
    cur CURSOR FOR
        SELECT TABLE_NAME, OBJECT_TYPE, BUSINESS_KEY, SURROGATE_KEY
          FROM _check_versioning_keys;
BEGIN
    -- Resolve the object types to check from the optional table_schema parameter:
    -- SDL -> sdltable, EDV -> satellite/linksatellite, NULL -> all three; anything else fails
    CASE
        WHEN (:table_schema IS NULL) THEN
            v_type_filter := ''''''sdltable'''', ''''satellite'''', ''''linksatellite'''''';
        WHEN (UPPER(TRIM(:table_schema)) = ''SDL'') THEN
            v_type_filter := ''''''sdltable'''''';
        WHEN (UPPER(TRIM(:table_schema)) = ''EDV'') THEN
            v_type_filter := ''''''satellite'''', ''''linksatellite'''''';
        ELSE
            RAISE invalid_table_schema;
    END CASE;

    -- Create a temporary table to collect validation results across all tables
    CREATE OR REPLACE TEMPORARY TABLE _check_versioning_result (
        TABLE_NAME        VARCHAR,
        KEY_COLUMNS       VARCHAR,
        INCORRECT_RECORDS INTEGER,
        SAMPLE_KEYS       VARCHAR
    );

    -- Load the list of tables to check from the META.UNIQUE_KEY metadata table,
    -- filtered to the versioned object types resolved from table_schema
    -- and optionally filtered to a single table if the table_name parameter is provided
    EXECUTE IMMEDIATE
        ''CREATE OR REPLACE TEMPORARY TABLE _check_versioning_keys AS
         SELECT TABLE_NAME, OBJECT_TYPE, BUSINESS_KEY, SURROGATE_KEY
           FROM '' || v_meta_uk || ''
          WHERE LOWER(OBJECT_TYPE) IN ('' || v_type_filter || '')''
          || CASE WHEN :table_name IS NOT NULL THEN '' AND UPPER(TRIM(TABLE_NAME)) = '''''' || UPPER(TRIM(:table_name)) || '''''''' ELSE '''' END;

    -- Iterate over each table to validate its versioning columns
    OPEN cur;
    FOR rec IN cur DO
        v_tbl          := rec.TABLE_NAME;
        v_object_type  := rec.OBJECT_TYPE;
        v_business_key := rec.BUSINESS_KEY;
        v_surrogate_key:= rec.SURROGATE_KEY;

        -- Determine the schema and key columns based on the object type:
        -- SDL tables use the business key, EDV satellites use the surrogate key
        IF (LOWER(v_object_type) = ''sdltable'') THEN
            v_schema := ''SDL'';
            v_key_columns := v_business_key;
        ELSE
            v_schema := ''EDV'';
            v_key_columns := v_surrogate_key;
        END IF;

        v_fqn := :dbt_db || ''.'' || v_schema || ''.'' || v_tbl;

        -- Build a dynamic SQL query that:
        -- 1) Uses LEAD() to compute the expected DW_VLD_TO_DTTM and DW_CRNT_FL
        -- 2) Finds mismatched rows where actual values differ from expected,
        --    or where DW_VLD_TO_DTTM <= DW_VLD_FM_DTTM (invalid range)
        -- 3) Returns the mismatch count and up to 5 sample keys for debugging
        v_sql := ''
WITH versioned AS (
    SELECT '' || v_key_columns || '',
           DW_VLD_FM_DTTM,
           DW_VLD_TO_DTTM,
           DW_CRNT_FL,
           COALESCE(
               LEAD(DW_VLD_FM_DTTM) OVER (PARTITION BY '' || v_key_columns || '' ORDER BY DW_VLD_FM_DTTM),
               ''''2100-01-01''''::TIMESTAMP_NTZ
           ) AS EXPECTED_VLD_TO,
           CASE
               WHEN LEAD(DW_VLD_FM_DTTM) OVER (PARTITION BY '' || v_key_columns || '' ORDER BY DW_VLD_FM_DTTM) IS NULL
               THEN TRUE ELSE FALSE
           END AS EXPECTED_CRNT_FL
    FROM '' || v_fqn || ''
),
mismatched AS (
    SELECT *, CONCAT_WS('''', '''', '' || v_key_columns || '', DW_VLD_FM_DTTM) AS KEY_VALS
      FROM versioned
     WHERE DW_VLD_TO_DTTM != EXPECTED_VLD_TO OR DW_CRNT_FL != EXPECTED_CRNT_FL OR DW_VLD_TO_DTTM <= DW_VLD_FM_DTTM
),
samples AS (
    SELECT DISTINCT KEY_VALS
      FROM mismatched
     LIMIT 5
)
SELECT
    (SELECT COUNT(*) FROM mismatched) AS CNT,
    COALESCE((SELECT LISTAGG(KEY_VALS, '''' | '''') WITHIN GROUP (ORDER BY KEY_VALS) FROM samples), '''''''') AS SAMPLES'';

        -- Execute the validation query and capture results;
        -- errors (e.g. missing table/columns) are caught and logged with -1 count
        BEGIN
            EXECUTE IMMEDIATE v_sql;
            SELECT "CNT", "SAMPLES" INTO :v_count, :v_samples FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));

            -- Only record the table if mismatches were found
            IF (v_count > 0) THEN
                INSERT INTO _check_versioning_result VALUES (:v_tbl, :v_key_columns || '', DW_VLD_FM_DTTM'', :v_count, :v_samples);
            END IF;
        EXCEPTION
            WHEN OTHER THEN
                INSERT INTO _check_versioning_result VALUES (:v_tbl, :v_key_columns || '', DW_VLD_FM_DTTM'', -1, ''ERROR: '' || :SQLERRM);
        END;
    END FOR;
    CLOSE cur;

    -- Return all results ordered by severity (most mismatches first)
    res := (SELECT * FROM _check_versioning_result ORDER BY INCORRECT_RECORDS DESC);
    RETURN TABLE(res);
END;
';
