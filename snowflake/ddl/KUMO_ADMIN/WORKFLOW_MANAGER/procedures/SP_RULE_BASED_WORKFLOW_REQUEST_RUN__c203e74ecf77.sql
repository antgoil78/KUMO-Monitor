-- Source: KUMO_ADMIN.WORKFLOW_MANAGER.SP_RULE_BASED_WORKFLOW_REQUEST_RUN(VARCHAR, VARCHAR)
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_ADMIN";
USE SCHEMA "KUMO_ADMIN"."WORKFLOW_MANAGER";

CREATE OR REPLACE PROCEDURE KUMO_ADMIN.WORKFLOW_MANAGER.SP_RULE_BASED_WORKFLOW_REQUEST_RUN("P_PARENT_RUN_ID" VARCHAR, "P_EVALUATE_WORKFLOW_ID" VARCHAR)
RETURNS TABLE ()
LANGUAGE SQL
EXECUTE AS CALLER
AS 'begin 

let monitorSchema := ''KUMO_ADMIN.WORKFLOW_MANAGER'';
let metaSchema := ''META'';
let procName varchar := monitorSchema || ''.SP_WORKFLOW_REQUEST_RUN'';

let sql := 
    ''with data as (
        select 
            r.workflow_id,
            
            r.dependee_type dependee_type,
            nvl(coalesce(wf_dep.workflow_id,m_dep.model_name),r.dependee_id) dependee_name,
            
            iff(r.success,''''|SUCCESS'''','''''''')||iff(r.warning,''''|WARNING'''','''''''')||iff(r.error,''''|ERROR'''','''''''')||''''|'''' accepted_status,
            coalesce(wf_dep.status,m_dep.status) dep_status,
            nvl(contains(accepted_status,dep_status),false) result_status,
    
            r.newer must_be_newer,
            nvl(wf.loaded_dttm,''''1901-01-01'''') as loaded_dttm,
            coalesce(wf_dep.loaded_dttm,m_dep.loaded_dttm) dep_loaded_dttm,
            case when must_be_newer
            then nvl(dep_loaded_dttm,''''1001-01-01'''') > nvl(wf.loaded_dttm,''''1901-01-01'''')
            else dep_loaded_dttm is not null end as result_must_be_newer
            
        from '' || metaSchema || ''.workflow_dep_rule r
        left outer join '' || metaSchema || ''.workflow_status wf
            on r.workflow_id = wf.workflow_id
        left outer join '' || metaSchema || ''.workflow_status wf_dep
            on r.dependee_id = wf_dep.workflow_id
            and r.dependee_type = ''''workflow''''
        left outer join '' || metaSchema || ''.model_status m_dep
            on r.dependee_id = m_dep.model_name
            and r.dependee_type = ''''model''''
        where r.active_fl
    ),
    result as (
        select
            workflow_id
        from 
            data
        group by workflow_id
        having min(least(result_status,result_must_be_newer))
    )
    
    select nvl(result.workflow_id=data.workflow_id,false) as result_ruleset,
        data.* 
    from data 
        left outer join result on data.workflow_id = result.workflow_id'';

    if (P_EVALUATE_WORKFLOW_ID is not null) then
        sql := sql || '' where data.workflow_id = '''''' || P_EVALUATE_WORKFLOW_ID || '''''''';
        sql := sql || '' order by data.workflow_id, dependee_type desc, dependee_name'';
    end if;

    let ruleResult resultset := (execute immediate sql);
    let ruleResultQid varchar := last_query_id();
    let resultLog := '''';

    if (P_EVALUATE_WORKFLOW_ID is null) then  -- Que the workflows
    
        let c1 cursor for select distinct WORKFLOW_ID
                            from table(result_scan(?)) 
                            where RESULT_RULESET;
        
        open c1 using (ruleResultQid);
        
        for wf in c1 do 
            let currentWfId varchar := wf.WORKFLOW_ID;

            call identifier(:procName)(:currentWfId, ''DEPENDENCY_RULE'', null, null, :P_PARENT_RUN_ID);
            let runId varchar(36) := (select $1 from table(result_scan(last_query_id())));

            resultLog := resultLog || ''Rule for workflow '''' ||
(select WORKFLOW_NAME from '' || monitorSchema || ''.WORKFLOWS where WORKFLOW_ID = '''''' || currentWfId || '''''') || '''' evaulated true, run requested as run_id '' || runId || ''\\n'';

        end for;
        close c1;
    
    end if;

    if (resultLog != '''') then
        sql := ''update '' || monitorSchema || ''.WORKFLOW_HISTORY '';
        sql := sql || ''set PAYLOAD_MESSAGE = '''''' || resultLog || '''''' '';
        sql := sql || ''where RUN_ID = '''''' || P_PARENT_RUN_ID || '''''''';
    else
        sql := ''update '' || monitorSchema || ''.WORKFLOW_HISTORY '';
        sql := sql || ''set PAYLOAD_MESSAGE = ''''No rules evaluated true, nothing to do'''''';
        sql := sql || ''where RUN_ID = '''''' || P_PARENT_RUN_ID || '''''''';
    end if;
    execute immediate sql;

    let retVal resultset := (select * exclude (WORKFLOW_ID) from table(result_scan(:ruleResultQid)));
    return table(retval);

end';
