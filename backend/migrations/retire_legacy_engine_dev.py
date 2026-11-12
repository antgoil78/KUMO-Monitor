"""Retire DEV execution entry points; preserve old history and all TST objects.

Read-only unless --apply. Suspend old tasks, archive their entry points and queue
with X_ names, and persist the environment-specific retirement setting. No
schedules are activated and no business jobs are run. Renames preserve grants
and contents. Reruns resume a partially applied retirement.
"""
import argparse
import json
import re
from migration_support import connect
from snowflake.connector import DictCursor

APP, META = 'KUMO_DEV.MONITOR_APP', 'KUMO_DEV.MONITOR_META'
PROCEDURES = (
    ('SP_WORKFLOW_REQUEST_RUN','VARCHAR,VARCHAR,VARCHAR,NUMBER,VARCHAR'),
    ('SP_WORKFLOW_DISPATCH','VARCHAR'), ('SP_WORKFLOW_DISPATCH','VARCHAR,VARCHAR'),
    ('SP_RUN_WORKFLOW','VARCHAR'), ('SP_RUN_WORKFLOW','VARCHAR,VARCHAR'),
    ('SP_SQL_COMPLETE','VARCHAR,BOOLEAN,VARCHAR'), ('SP_DBT_COMPLETE','VARCHAR,BOOLEAN,VARCHAR'),
    ('SP_RULE_BASED_WORKFLOW_REQUEST_RUN','VARCHAR,VARCHAR,VARCHAR'),
    ('SP_CLEANUP_OLD_TMP_TASKS','FLOAT'),
)


def run(apply=False):
    with connect() as connection, connection.cursor(DictCursor) as cursor:
        def query(sql, params=()):
            cursor.execute(sql,params); return cursor.fetchall()
        query('USE DATABASE KUMO_DEV')
        query('USE SCHEMA '+APP)
        # Hard guard: this migration cannot be redirected to TST by local env.
        env=query(f"SELECT PARAMETER_VALUE FROM {META}.APPLICATION_PARAMETERS WHERE INSTANCE_NAME='DEFAULT' AND PARAMETER_GROUP='ENVIRONMENT' AND PARAMETER_KEY='DATABASE' AND ACTIVE_FL=TRUE")
        if len(env)!=1 or env[0]['PARAMETER_VALUE']!='KUMO_DEV': raise ValueError('Expected DEV environment routing')
        tables={row['TABLE_NAME'] for row in query("SELECT TABLE_NAME FROM KUMO_DEV.INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA='MONITOR_META' AND TABLE_NAME IN ('WORKFLOW_RUN_QUEUE','X_WORKFLOW_RUN_QUEUE')")}
        if len(tables)!=1: raise ValueError('Expected exactly one original or archived queue')
        queue=next(iter(tables))
        pending=query(f"SELECT COUNT(*) N FROM {META}.{queue} WHERE UPPER(TRIM(STATUS)) NOT IN ('DONE','SUCCESS','SUCCEEDED','FAILED','ERROR','SKIPPED','CANCELLED','CANCELED') OR STATUS IS NULL")[0]['N']
        active=query(f"SELECT COUNT(*) N FROM {META}.WORKFLOW_HISTORY WHERE STATUS IN ('RUNNING','INITIATING','QUEUED','REQUESTED','CLAIMED','DISPATCHING','DISPATCHED')")[0]['N']
        new_active=query(f"SELECT COUNT(*) N FROM {META}.WORKFLOW_EXECUTION_LOG WHERE JOB_ID IS NULL AND STATUS IN ('RUNNING','CLAIMED','QUEUED')")[0]['N']
        if pending or active:
            raise ValueError(f'Finish old executions before retirement (old queue={pending}, old history={active})')
        # New runs are independent. Keep them running, but refuse to retire a
        # procedure referenced by an unfinished snapshotted new job.
        active_commands=query(f"SELECT CONFIG_SNAPSHOT:resolved:execSql::VARCHAR COMMAND FROM {META}.WORKFLOW_EXECUTION_LOG WHERE JOB_ID IS NOT NULL AND STATUS IN ('INITIATING','PENDING','QUEUED','RUNNING')")
        retired_names={name for name,_ in PROCEDURES}
        if any(any(re.search(r'\b'+name+r'\b',row['COMMAND'] or '',re.I) for name in retired_names) for row in active_commands):
            raise ValueError('An unfinished new job references an old procedure; let it finish before retirement')
        tasks=query('SHOW TASKS IN SCHEMA '+APP)
        old_tasks=[row for row in tasks if re.fullmatch(r'(?:X_)?(?:TASK_WF_MASTER_DISPATCHER|TASK_WF_[0-9A-F_]+(?:_SCHEDULER)?|TMP_WF_[0-9A-F_]+)',row['name'])]
        names={row['name'] for row in tasks}
        by_name={row['name']:row for row in tasks}
        role=query('SELECT CURRENT_ROLE() ROLE')[0]['ROLE']
        grants={}
        for row in old_tasks:
            if json.loads(row.get('predecessors') or '[]'):raise ValueError('Review task graphs before archiving')
            if row['name'].startswith('X_'):continue
            if any(APP+'.'+row['name'] in json.loads(task.get('predecessors') or '[]') for task in tasks):raise ValueError('An old task has dependent tasks; review its graph')
            if row['owner']!=role:raise ValueError('Use the old task owner role to preserve task ownership')
            archived=by_name.get('X_'+row['name'])
            if archived and (archived['state']!='suspended' or any(archived.get(key)!=row.get(key) for key in ('definition','warehouse','schedule','condition','comment'))):raise ValueError('Task archive name collision')
            grants[row['name']]=query('SHOW GRANTS ON TASK '+APP+'.'+row['name'])
            if any(grant['granted_to'] not in ('ROLE','DATABASE_ROLE') for grant in grants[row['name']]):raise ValueError('Unexpected task grant recipient')
        running=query("SELECT NAME FROM TABLE(KUMO_DEV.INFORMATION_SCHEMA.TASK_HISTORY(SCHEDULED_TIME_RANGE_START=>DATEADD('hour',-24,CURRENT_TIMESTAMP()),RESULT_LIMIT=>10000)) WHERE STATE='EXECUTING'")
        if any(row['NAME'] in {task['name'] for task in old_tasks} for row in running): raise ValueError('An old task is executing; let it finish')
        schedules=query(f'SELECT COUNT(*) N FROM {META}.WORKFLOW_TASKS WHERE IS_ENABLED=TRUE')[0]['N']
        procedure_names=sorted({prefix+name for name,_ in PROCEDURES for prefix in ('','X_')})
        rows=query("SELECT PROCEDURE_NAME,ARGUMENT_SIGNATURE FROM KUMO_DEV.INFORMATION_SCHEMA.PROCEDURES WHERE PROCEDURE_SCHEMA='MONITOR_APP' AND PROCEDURE_NAME IN ("+','.join('%s' for _ in procedure_names)+')',tuple(procedure_names))
        existing={(row['PROCEDURE_NAME'],re.sub(r'\b[A-Za-z_][A-Za-z0-9_]*\s+(?=(?:VARCHAR|BOOLEAN|NUMBER|FLOAT)\b)','',row['ARGUMENT_SIGNATURE']).replace(' ','').strip('()')) for row in rows}
        for name,signature in PROCEDURES:
            if sum((prefix+name,signature) in existing for prefix in ('','X_'))!=1: raise ValueError('Missing or ambiguous procedure '+name+'('+signature+')')
        streams=query(f"SHOW STREAMS LIKE '%WORKFLOW_EXECUTION_QUEUE_STREAM' IN SCHEMA {META}")
        streams=[row for row in streams if row['name'] in ('WORKFLOW_EXECUTION_QUEUE_STREAM','X_WORKFLOW_EXECUTION_QUEUE_STREAM')]
        if len(streams)!=1: raise ValueError('Expected exactly one queue stream')
        if streams[0]['owner']!=role:raise ValueError('Use the queue stream owner role to preserve ownership')
        # New maintenance uses the new engine housekeeping. Remove only the
        # explicit old temporary-task cleanup call from the migrated SQL job.
        cleanup=query(f"SELECT j.JOB_ID,j.SQL_COMMAND FROM {META}.JOB j JOIN {META}.DEPENDENCIES d ON j.JOB_ID=d.JOB_ID JOIN {META}.WORKFLOW w ON w.WORKFLOW_ID=d.WORKFLOW_ID WHERE w.WORKFLOW_NAME='MAINTENANCE - CLEANUP'")
        if len(cleanup)!=1: raise ValueError('Expected one migrated maintenance job')
        command=cleanup[0]['SQL_COMMAND']
        updated,count=re.subn(r'(?im)^\s*CALL\s+(?:\[DATABASE\]|KUMO_DEV)\.(?:\[MONITOR_APP_SCHEMA\]|MONITOR_APP)\.SP_CLEANUP_OLD_TMP_TASKS\s*\(\s*1\s*\)\s*;\s*', '', command)
        if 'SP_CLEANUP_OLD_TMP_TASKS' in updated.upper(): raise ValueError('Unexpected maintenance cleanup invocation')
        result=dict(applied=apply,environment='DEV',tasks=[row['name'] for row in old_tasks],procedures=[name+'('+signature+')' for name,signature in PROCEDURES],queue=queue,old_history_preserved=True,tst_changed=False,new_schedules_activated=False,old_schedule_flags_preserved=schedules,new_runs_preserved=new_active,maintenance_call_removed=bool(count))
        if not apply:return result
        for row in old_tasks:query('ALTER TASK '+APP+'.'+row['name']+' SUSPEND')
        if queue=='WORKFLOW_RUN_QUEUE':query(f'ALTER TABLE {META}.WORKFLOW_RUN_QUEUE RENAME TO {META}.X_WORKFLOW_RUN_QUEUE')
        if query(f"SELECT COUNT(*) N FROM {META}.X_WORKFLOW_RUN_QUEUE WHERE UPPER(TRIM(STATUS)) NOT IN ('DONE','SUCCESS','SUCCEEDED','FAILED','ERROR','SKIPPED','CANCELLED','CANCELED') OR STATUS IS NULL")[0]['N']:raise ValueError('A request arrived during cutover; queue is archived and dispatcher suspended; review it before continuing')
        for name,signature in PROCEDURES:
            if (name,signature) in existing:query(f'ALTER PROCEDURE {APP}.{name}({signature}) RENAME TO X_{name}')
        for row in old_tasks:
            if row['name'].startswith('X_'):continue
            original=APP+'.'+row['name'];archived=APP+'.X_'+row['name']
            # Snowflake does not support ALTER TASK RENAME. Clone under the
            # archive name (always suspended), restore grants, then remove the
            # old name. No workload is executed by these statements.
            if 'X_'+row['name'] not in names:query('CREATE TASK '+archived+' CLONE '+original)
            for grant in grants[row['name']]:
                if grant['privilege']=='OWNERSHIP':continue
                recipient='"'+grant['grantee_name'].replace('"','""')+'"'
                query('GRANT '+grant['privilege']+' ON TASK '+archived+' TO '+grant['granted_to'].replace('_',' ')+' '+recipient+(' WITH GRANT OPTION' if str(grant['grant_option']).lower()=='true' else ''))
            query('DROP TASK '+original)
        if streams[0]['name']=='WORKFLOW_EXECUTION_QUEUE_STREAM':
            # Cloning preserves the offset and COPY GRANTS keeps access. A
            # stream, like a task, has no native RENAME operation.
            query(f'CREATE STREAM {META}.X_WORKFLOW_EXECUTION_QUEUE_STREAM CLONE {META}.WORKFLOW_EXECUTION_QUEUE_STREAM COPY GRANTS')
            query(f'DROP STREAM {META}.WORKFLOW_EXECUTION_QUEUE_STREAM')
        if count:query(f'UPDATE {META}.JOB SET SQL_COMMAND=%s WHERE JOB_ID=%s AND SQL_COMMAND=%s',(updated,cleanup[0]['JOB_ID'],command))
        query(f"MERGE INTO {META}.APPLICATION_PARAMETERS t USING (SELECT 'DEFAULT' INSTANCE_NAME,'ORCHESTRATION' PARAMETER_GROUP,'LEGACY_ENGINE_RETIRED' PARAMETER_KEY) s ON t.INSTANCE_NAME=s.INSTANCE_NAME AND t.PARAMETER_GROUP=s.PARAMETER_GROUP AND t.PARAMETER_KEY=s.PARAMETER_KEY WHEN MATCHED THEN UPDATE SET PARAMETER_VALUE='true',VALUE_TYPE='BOOLEAN',ACTIVE_FL=TRUE,UPDATED_DTTM=CURRENT_TIMESTAMP() WHEN NOT MATCHED THEN INSERT (INSTANCE_NAME,PARAMETER_GROUP,PARAMETER_KEY,PARAMETER_VALUE,VALUE_TYPE,ACTIVE_FL,DESCRIPTION) VALUES(s.INSTANCE_NAME,s.PARAMETER_GROUP,s.PARAMETER_KEY,'true','BOOLEAN',TRUE,'DEV uses the new orchestration engine; old execution entry points are archived with X_ names')")
        return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--apply',action='store_true')
    print(json.dumps(run(parser.parse_args().apply),indent=2))
