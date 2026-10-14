-- Source: KUMO_TST.RAW_LIM.X_SP_RUN_AND_DECIDE_WAIT_FOR_FILES(VARCHAR, VARCHAR, NUMBER, NUMBER)
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_TST";
USE SCHEMA "KUMO_TST"."RAW_LIM";

CREATE OR REPLACE PROCEDURE KUMO_TST.RAW_LIM.X_SP_RUN_AND_DECIDE_WAIT_FOR_FILES("SUBJECT_AREA" VARCHAR, "PKG_GROUP_NAME" VARCHAR, "RETRIES" NUMBER(38,0), "DELAY_BETWEEN_RETRIES_SEC" NUMBER(38,0))
RETURNS VARIANT
LANGUAGE SQL
COMMENT='user-defined procedure'
EXECUTE AS CALLER
AS '
DECLARE
    v_res       VARIANT;
    v_status    STRING;
    v_decision  STRING;
    v_call_sql  STRING;

    v_subject   STRING := TRIM(SUBJECT_AREA);
    v_group     STRING := PKG_GROUP_NAME;  -- can be NULL

    v_err_msg   STRING;
    v_fail_sql  STRING;
BEGIN
    -------------------------------------------------------------------------
    -- Call underlying procedure
    -------------------------------------------------------------------------
    v_call_sql :=
        ''CALL KUMO_TST.RAW_LIM.SP_WAIT_FOR_FILES_FROM_000(''''''
        || REPLACE(v_subject, '''''''', '''''''''''')
        || '''''', ''
        || IFF(v_group IS NULL,
               ''NULL'',
               '''''''' || REPLACE(TRIM(v_group), '''''''', '''''''''''') || '''''''')
        || '', ''
        || TO_VARCHAR(RETRIES)
        || '', ''
        || TO_VARCHAR(DELAY_BETWEEN_RETRIES_SEC)
        || '')'';

    EXECUTE IMMEDIATE v_call_sql;

    -------------------------------------------------------------------------
    -- Read VARIANT returned from CALL
    -------------------------------------------------------------------------
    SELECT $1::VARIANT
      INTO :v_res
    FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));

    v_status := COALESCE(v_res:"status"::STRING, ''UNKNOWN'');

    -------------------------------------------------------------------------
    -- Decide
    -------------------------------------------------------------------------
    v_decision := CASE
        WHEN v_status = ''SUCCESS_ALL_GROUPS'' THEN ''PROCEED''
        WHEN v_status IN (''NO_ROWS_IN_RAW_TABLE'', ''NO_ACTIVE_GROUPS_FOUND'') THEN ''STOP_NO_WORK''
        WHEN v_status IN (''WAITING_FOR_PACKAGES'', ''BLOCKED_BY_VALIDATION'') THEN ''STOP_RETRY_LATER''
        WHEN v_status LIKE ''MISSING%'' THEN ''STOP_ERROR''
        WHEN v_status = ''ERROR'' THEN ''STOP_ERROR''
        ELSE ''STOP_UNKNOWN''
    END;

    -------------------------------------------------------------------------
    -- If stop => FAIL the task run (force an error)
    -------------------------------------------------------------------------
    IF (v_decision <> ''PROCEED'') THEN
        v_err_msg :=
            ''SP_RUN_AND_DECIDE_WAIT_FOR_FILES STOP. decision='' || v_decision ||
            '', status='' || v_status ||
            '', subject='' || v_subject ||
            '', group='' || COALESCE(v_group, ''NULL'') ||
            '', run_id='' || COALESCE(v_res:"run_id"::STRING, ''n/a'') ||
            '', result='' || TO_VARCHAR(v_res);

        -- Force an error that includes our message in the error text
        v_fail_sql := ''SELECT TO_NUMBER('''''' || REPLACE(v_err_msg, '''''''', '''''''''''') || '''''')'';
        EXECUTE IMMEDIATE v_fail_sql;
    END IF;

    RETURN OBJECT_CONSTRUCT(
        ''decision'', v_decision,
        ''status'', v_status,
        ''result'', v_res
    );
END;
';
