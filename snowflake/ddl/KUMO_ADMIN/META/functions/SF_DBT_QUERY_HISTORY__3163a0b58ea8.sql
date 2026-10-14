-- Source: KUMO_ADMIN.META.SF_DBT_QUERY_HISTORY(UUID)
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_ADMIN";
USE SCHEMA "KUMO_ADMIN"."META";

CREATE OR REPLACE FUNCTION KUMO_ADMIN.META.SF_DBT_QUERY_HISTORY("RUNID" UUID)
RETURNS TABLE ("MODEL" VARCHAR, "DATABASE_NAME" VARCHAR, "USER_NAME" VARCHAR, "WAREHOUSE_NAME" VARCHAR, "START_TIME" TIMESTAMP_NTZ(9), "LATEST_CHANGE" TIMESTAMP_NTZ(9), "ELAPSED_S" NUMBER(38,0), "STATUS" VARCHAR, "LATEST_SQL" VARCHAR, "ERROR_MESSAGE" VARCHAR)
LANGUAGE SQL
AS '
    with log as (
        SELECT 
            try_parse_json(QUERY_TAG):"run_id"::uuid run_id,
            try_parse_json(QUERY_TAG):"model"::varchar model,
            start_time,
            execution_status,
            query_text,
            query_type,
            database_name,
            user_name,
            warehouse_name,
            datediff(s,start_time,case when end_time<start_time then current_timestamp() else end_time end) elapsed_time,
            error_message
        FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY(END_TIME_RANGE_START => CURRENT_TIMESTAMP() - INTERVAL ''1 day'', RESULT_LIMIT => 5000 ))
        WHERE 1=1  
        and run_id = (runId)::uuid  /* <<<<<<<<<<<< set wanted run_id here */
        and query_type not in (''SHOW'',''COMMIT'',''BEGIN'',''DESCRIBE''))
    ,agg as (
        select 
            run_id,
            model,
            database_name,
            user_name,
            warehouse_name,
            min(start_time) start_time,
            max(start_time) latest_change,
            max_by(query_text, start_time) latest_sql,
            sum(elapsed_time) elapsed_s,
            sum((EXECUTION_STATUS like ''FAILED%'')::integer) as STATUS_FAILED,
            sum((EXECUTION_STATUS = ''SUCCESS'')::integer) as STATUS_SUCCESS,
            sum((EXECUTION_STATUS = ''RUNNING'')::integer) as STATUS_RUNNING,
            sum((EXECUTION_STATUS = ''RESUMING_WAREHOUSE'')::integer) as STATUS_RESUMING,
            sum((EXECUTION_STATUS = ''QUEUED'')::integer) as STATUS_QUEUED,
            sum((EXECUTION_STATUS = ''BLOCKED'')::integer) as STATUS_BLOCKED,
            listagg(error_message,'' >>>>>>>>>> NEXT ERROR MESSAGE >>>>>>>>>> '') as error_message
        from log group by all)
    
    select 
        model,
        database_name,
        user_name,
        warehouse_name,
        convert_timezone(''America/Los_Angeles'',''Europe/Stockholm'',start_time) start_time,
        convert_timezone(''America/Los_Angeles'',''Europe/Stockholm'',latest_change) latest_change,
        elapsed_s,
        case
            when STATUS_QUEUED+STATUS_RESUMING >0 then ''WAITING''
            when STATUS_FAILED+STATUS_BLOCKED = 0 and STATUS_RUNNING >0 then ''RUNNING''
            when STATUS_FAILED+STATUS_BLOCKED+STATUS_RUNNING = 0 and STATUS_SUCCESS >0 then ''SUCCESS''
            when STATUS_FAILED+STATUS_BLOCKED > 0 and STATUS_RUNNING >0 then ''FAILING''
            when STATUS_FAILED+STATUS_BLOCKED > 0 then ''FAILED''
        else ''UNKNOWN'' end status,
        latest_sql,
        error_message
    from agg order by case status when ''RUNNING'' then 1 else 2 end, latest_change desc
';
