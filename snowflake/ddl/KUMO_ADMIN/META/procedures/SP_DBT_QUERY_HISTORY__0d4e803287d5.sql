-- Source: KUMO_ADMIN.META.SP_DBT_QUERY_HISTORY(VARCHAR, VARCHAR, VARCHAR)
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_ADMIN";
USE SCHEMA "KUMO_ADMIN"."META";

CREATE OR REPLACE PROCEDURE KUMO_ADMIN.META.SP_DBT_QUERY_HISTORY("RUN_ID" VARCHAR, "DBT_DB" VARCHAR DEFAULT CURRENT_DATABASE(), "LOG_TO_TABLE" VARCHAR DEFAULT null)
RETURNS TABLE ("RUN_ID" VARCHAR(36), "MODEL_NAME" VARCHAR, "DATABASE_NAME" VARCHAR, "USER_NAME" VARCHAR, "WAREHOUSE_NAME" VARCHAR, "START_TIME" TIMESTAMP_NTZ(9), "LATEST_CHANGE" TIMESTAMP_NTZ(9), "ELAPSED_S" NUMBER(38,0), "STATUS" VARCHAR, "LATEST_SQL" VARCHAR, "SQL" VARCHAR, "PARSED_JSON" VARIANT, "ERROR_MESSAGE" VARCHAR)
LANGUAGE SQL
EXECUTE AS CALLER
AS 'declare
    effective_run_id varchar;
    res resultset;
    v_meta_rl varchar;
    v_sql varchar;
begin
    v_meta_rl := :dbt_db || ''.META.RUN_LOG'';
    
    effective_run_id := (:run_id);
    

    v_sql := ''
    with 
     log as (
        SELECT 
            try_parse_json(QUERY_TAG):"run_id"::varchar(36) run_id,
            try_parse_json(QUERY_TAG):"model"::varchar model_name,
            start_time,
            execution_status,
            query_text,
            query_type,
            database_name,
            user_name,
            warehouse_name,
            datediff(s,start_time,case when end_time<start_time then current_timestamp() else end_time end) elapsed_time,
            error_message
        FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY_BY_USER(USER_NAME => current_user(), END_TIME_RANGE_START => CURRENT_TIMESTAMP() - INTERVAL ''''1 day'''', RESULT_LIMIT => 10000 ))
        WHERE 1=1  
        and run_id = '''''' || :effective_run_id || ''''''
        and database_name = current_database()
        and query_type not in (''''SHOW'''',''''COMMIT'''',''''BEGIN'''',''''DESCRIBE''''))
    ,agg as (
        select 
            run_id,
            model_name,
            database_name,
            user_name,
            warehouse_name,
            min(start_time) start_time,
            max(start_time) latest_change,
            max_by(query_text,start_time) latest_sql,
            listagg(query_text,'''';


/* >>> NEXT SQL >>> */


'''') within group (order by start_time) sql,
            sum(elapsed_time) elapsed_s,
            sum((EXECUTION_STATUS like ''''FAILED%'''')::integer) as STATUS_FAILED,
            sum((EXECUTION_STATUS = ''''SUCCESS'''')::integer) as STATUS_SUCCESS,
            sum((EXECUTION_STATUS = ''''RUNNING'''')::integer) as STATUS_RUNNING,
            sum((EXECUTION_STATUS = ''''RESUMING_WAREHOUSE'''')::integer) as STATUS_RESUMING,
            sum((EXECUTION_STATUS = ''''QUEUED'''')::integer) as STATUS_QUEUED,
            sum((EXECUTION_STATUS = ''''BLOCKED'''')::integer) as STATUS_BLOCKED,
            listagg(error_message,'''' 


/* >>> NEXT ERROR >>> */


 '''') within group (order by start_time) as error_message
        from log group by all)
    
    select 
        run_id,
        model_name,
        database_name,
        user_name,
        warehouse_name,
        convert_timezone(''''America/Los_Angeles'''',''''UTC'''',start_time) start_time,
        convert_timezone(''''America/Los_Angeles'''',''''UTC'''',latest_change) latest_change,
        elapsed_s,
        case
            when STATUS_FAILED=0 and STATUS_RUNNING>0 then ''''RUNNING''''
            when STATUS_FAILED>0 and STATUS_QUEUED+STATUS_RESUMING+STATUS_BLOCKED+STATUS_RUNNING>0 then ''''FAILING''''
            when STATUS_QUEUED+STATUS_RESUMING+STATUS_BLOCKED>0 then ''''WAITING''''
            when STATUS_BLOCKED+STATUS_FAILED+STATUS_QUEUED+STATUS_RESUMING+STATUS_RUNNING=0 and STATUS_SUCCESS>0 then ''''SUCCESS''''
            when STATUS_BLOCKED+STATUS_QUEUED+STATUS_RESUMING+STATUS_RUNNING=0 and STATUS_FAILED>0 then ''''ERROR''''
        else ''''UNKNOWN'''' end status,
        latest_sql,
        sql,
        parse_json(''''{RUNNING:''''||STATUS_RUNNING||'''', FAILED:''''||STATUS_FAILED||'''', SUCCESS:''''||STATUS_SUCCESS||'''', BLOCKED:''''||STATUS_BLOCKED||'''', QUEUED:''''||STATUS_QUEUED||'''', RESUMING:''''||STATUS_RESUMING||''''}'''')  status_json,
        error_message
    from agg order by case status when ''''WAITING'''' then 1 when ''''RUNNING'''' then 2 else 3 end, latest_change desc'';

    if (:log_to_table is not null) then
        v_sql := ''insert into '' || :dbt_db || ''.META.'' || :log_to_table || '' '' || :v_sql;
        execute immediate :v_sql;
        res := (execute immediate ''select * from '' || :dbt_db || ''.META.'' || :log_to_table || '' where 1=0'');
        return table(res);
    else
        res := (execute immediate :v_sql);
        return table(res);
    end if;
end;
';
