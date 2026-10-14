-- Source: KUMO_ADMIN.WORKFLOW_MANAGER.SP_FETCH_RUN_LOG(VARCHAR)
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_ADMIN";
USE SCHEMA "KUMO_ADMIN"."WORKFLOW_MANAGER";

CREATE OR REPLACE PROCEDURE KUMO_ADMIN.WORKFLOW_MANAGER.SP_FETCH_RUN_LOG("P_RUN_ID" VARCHAR)
RETURNS TABLE ("RUN_ID" VARCHAR, "MODEL" VARCHAR, "DATABASE_NAME" VARCHAR, "USER_NAME" VARCHAR, "WAREHOUSE_NAME" VARCHAR, "START_TIME" TIMESTAMP_NTZ(9), "LATEST_CHANGE" TIMESTAMP_NTZ(9), "ELAPSED_S" FLOAT, "STATUS" VARCHAR, "ERROR_MESSAGE" VARCHAR)
LANGUAGE SQL
EXECUTE AS OWNER
AS '
DECLARE
    res RESULTSET;
    sql_text VARCHAR;
BEGIN
    sql_text := ''
        WITH log AS (
            SELECT 
                TRY_PARSE_JSON(QUERY_TAG):"run_id"::VARCHAR AS run_id,
                TRY_PARSE_JSON(QUERY_TAG):"model"::VARCHAR AS model,
                start_time,
                execution_status,
                database_name,
                user_name,
                warehouse_name,
                GREATEST(0, total_elapsed_time) AS total_elapsed_time,
                error_message
            FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
            WHERE --start_time >= CURRENT_TIMESTAMP() - INTERVAL ''''1 day''''
            start_time >= ''''2026-03-06 12:20:35.366''''
              AND TRY_PARSE_JSON(QUERY_TAG):"run_id"::VARCHAR = '''''' || P_RUN_ID || ''''''
              AND query_type NOT IN (''''SHOW'''',''''COMMIT'''',''''BEGIN'''',''''DESCRIBE'''')
        ),
        agg AS (
            SELECT 
                run_id,
                model,
                database_name,
                user_name,
                warehouse_name,
                MIN(start_time) AS start_time,
                MAX(start_time) AS latest_change,
                ROUND(SUM(total_elapsed_time)/1000, 1) AS elapsed_s,
                SUM((EXECUTION_STATUS LIKE ''''FAILED%'''')::INTEGER) AS STATUS_FAILED,
                SUM((EXECUTION_STATUS = ''''SUCCESS'''')::INTEGER) AS STATUS_SUCCESS,
                SUM((EXECUTION_STATUS = ''''RUNNING'''')::INTEGER) AS STATUS_RUNNING,
                SUM((EXECUTION_STATUS = ''''RESUMING_WAREHOUSE'''')::INTEGER) AS STATUS_RESUMING,
                SUM((EXECUTION_STATUS = ''''QUEUED'''')::INTEGER) AS STATUS_QUEUED,
                SUM((EXECUTION_STATUS = ''''BLOCKED'''')::INTEGER) AS STATUS_BLOCKED,
                LISTAGG(error_message, '''' >>>>>>>>>> NEXT ERROR MESSAGE >>>>>>>>>> '''') AS error_message
            FROM log 
            GROUP BY ALL
        )
        SELECT 
            run_id::VARCHAR,
            model::VARCHAR,
            database_name::VARCHAR,
            user_name::VARCHAR,
            warehouse_name::VARCHAR,
            CONVERT_TIMEZONE(''''America/Los_Angeles'''', ''''Europe/Stockholm'''', start_time)::TIMESTAMP_NTZ AS start_time,
            CONVERT_TIMEZONE(''''America/Los_Angeles'''', ''''Europe/Stockholm'''', latest_change)::TIMESTAMP_NTZ AS latest_change,
            elapsed_s::FLOAT,
            CASE
                WHEN STATUS_QUEUED + STATUS_RESUMING > 0 THEN ''''WAITING''''
                WHEN STATUS_FAILED + STATUS_BLOCKED = 0 AND STATUS_RUNNING > 0 THEN ''''RUNNING''''
                WHEN STATUS_FAILED + STATUS_BLOCKED + STATUS_RUNNING = 0 AND STATUS_SUCCESS > 0 THEN ''''SUCCESS''''
                WHEN STATUS_FAILED + STATUS_BLOCKED > 0 AND STATUS_RUNNING > 0 THEN ''''FAILING''''
                WHEN STATUS_FAILED + STATUS_BLOCKED > 0 THEN ''''FAILED''''
                ELSE ''''UNKNOWN'''' 
            END::VARCHAR AS status,
            error_message::VARCHAR
        FROM agg 
        ORDER BY latest_change DESC'';
    
    res := (EXECUTE IMMEDIATE :sql_text);
    RETURN TABLE(res);
END;
';
