"""Cut over TST to the approved DEV orchestration structure; read-only by default.

--apply pauses TST admissions/tasks, installs only the missing engine objects,
migrates TST business definitions, and archives legacy entry points with X_
names. Business schedules stay disabled. The service stays suspended for the
separate, digest-pinned image promotion. No business SQL/dbt is executed.
"""
import argparse
import json
from pathlib import Path
import re

from migration_support import ROOT, connect
from migrate_workflow_catalog import build_plan, comparable, fingerprint, replace_catalog, TARGETS
from apply_parallel_engine import PROCEDURES, task_ddl, task_fqn, require_pre_stream_release
from apply_workflow_command_templates import logical_body
from add_missing_tst_workflows_to_dev import COMMAND_FIELDS
import retire_legacy_engine_dev as retirement
from snowflake.connector import DictCursor

APP, META = 'KUMO_TST.MONITOR_APP', 'KUMO_TST.MONITOR_META'
SERVICE = APP + '.KUMO_MONITOR_SERVICE'
DDL = ROOT / 'snowflake/ddl/KUMO_DEV'
PROTECTED = ('WORKFLOWS', 'WORKFLOW_TASKS', 'WORKFLOW_HISTORY',
             'WORKFLOW_RUN_LOGS', 'RAW_LIM_PKG_GROUP_SOURCE')


def plan_catalog(workflows, tasks, warehouse):
    # IDs are those promoted from DEV; definitions and flags originate in TST.
    plan, summary = build_plan(workflows, tasks, 'KUMO_DEV.MONITOR_META')
    paused = []
    for row in plan['WORKFLOW']:
        if row['IS_SCHEDULE_ENABLED']:
            paused.append(row['WORKFLOW_NAME'])
        row.update(IS_SCHEDULE_ENABLED=False, WAREHOUSE_NAME=warehouse)
    for row in plan['JOB']:
        if row['JOB_NAME'] == 'MAINTENANCE - CLEANUP':
            row['SQL_COMMAND'] = re.sub(
                r'^\s*CALL\s+\[DATABASE\]\.\[MONITOR_APP_SCHEMA\]\.SP_CLEANUP_OLD_TMP_TASKS\s*\(\s*1\s*\)\s*;\s*',
                '', row['SQL_COMMAND'], count=1, flags=re.I)
    summary.update(paused_business_schedules=paused, copied_dev_execution_state=False)
    return plan, summary


def sql_from(path):
    text = path.read_text()
    match = re.search(r'\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|PROCEDURE|TASK)\b', text, re.I)
    if not match:
        raise ValueError('Expected a maintained definition: ' + str(path))
    return text[match.start():].strip().rstrip(';')


def check_idle(cursor):
    cursor.execute(f"SELECT COUNT(*) N FROM {META}.WORKFLOW_RUN_QUEUE WHERE STATUS IS NULL OR UPPER(TRIM(STATUS)) NOT IN ('DONE','SUCCESS','SUCCEEDED','FAILED','ERROR','SKIPPED','CANCELLED','CANCELED')")
    queued = cursor.fetchone()['N']
    cursor.execute(f"SELECT COUNT(*) N FROM {META}.WORKFLOW_HISTORY WHERE STATUS IN ('RUNNING','INITIATING','QUEUED','REQUESTED','CLAIMED','DISPATCHING','DISPATCHED')")
    if queued or cursor.fetchone()['N']:
        raise ValueError('Finish existing TST workflow executions before cutover')
    cursor.execute("SELECT NAME FROM TABLE(KUMO_TST.INFORMATION_SCHEMA.TASK_HISTORY(SCHEDULED_TIME_RANGE_START=>DATEADD('hour',-24,CURRENT_TIMESTAMP()),RESULT_LIMIT=>10000)) WHERE STATE='EXECUTING'")
    if any(re.match(r'(?:TASK_WF_|TMP_WF_)', row['NAME']) for row in cursor.fetchall()):
        raise ValueError('A legacy TST native task is executing')


def check_approved_jobs(cursor, plan):
    cursor.execute('SELECT * FROM KUMO_DEV.MONITOR_META.JOB')
    approved = {row['JOB_ID']: row for row in cursor.fetchall()}
    for row in plan['JOB']:
        if row['JOB_ID'] not in approved or any(row.get(key) != approved[row['JOB_ID']].get(key) for key in COMMAND_FIELDS):
            raise ValueError('TST command differs from the approved DEV definition: ' + row['JOB_NAME'])


def resolve_jobs(cursor, jobs):
    for index, row in enumerate(jobs, 1):
        definition = dict(jobType=row['JOB_TYPE'], sqlCommand=row['SQL_COMMAND'], dbtCommand=row['DBT_COMMAND'],
                          dbtTarget=row['DBT_TARGET'], dbtProjectFqn=row['DBT_PROJECT_FQN'])
        cursor.execute(f'CALL {APP}.SP_RESOLVE_WORKFLOW_COMMAND(PARSE_JSON(%s),NULL)', (json.dumps(definition),))
        result = next(iter(cursor.fetchone().values()))
        result = json.loads(result) if isinstance(result, str) else result
        if not result.get('ok') or re.search(r'\bKUMO_DEV\b', result.get('execSql', ''), re.I):
            raise ValueError('TST command resolution failed: ' + row['JOB_NAME'])
        if row['JOB_TYPE'] == 'DBT' and (result.get('dbtTarget') != 'kumo_TST' or result.get('dbtProjectFqn') != 'KUMO_TST.DBT_APP.KUMO_TST'):
            raise ValueError('dbt resolves outside TST: ' + row['JOB_NAME'])
        if index % 10 == 0:
            print('Resolved TST job definitions:', index, flush=True)


def run(apply=False, ddl_root=DDL):
    ddl_root = Path(ddl_root)
    with connect() as conn, conn.cursor(DictCursor) as cursor:
        cursor.execute("ALTER SESSION SET QUERY_TAG='KUMO_TST_ORCHESTRATION_CUTOVER'")
        cursor.execute('USE ROLE KUMO_ADMIN')
        require_pre_stream_release(cursor)
        cursor.execute('USE DATABASE KUMO_TST')
        cursor.execute('USE SCHEMA ' + APP)
        cursor.execute(f'CALL {META}.SP_GET_ENVIRONMENT()')
        env = next(iter(cursor.fetchone().values()))
        env = json.loads(env) if isinstance(env, str) else env
        if env.get('DATABASE') != 'KUMO_TST' or env.get('META_SCHEMA') != 'MONITOR_META' or env.get('MONITOR_APP_SCHEMA') != 'MONITOR_APP':
            raise ValueError('Expected TST environment routing')
        check_idle(cursor)
        hashes = {table: fingerprint(cursor, META, table) for table in PROTECTED}
        queue_hash = fingerprint(cursor, META, 'WORKFLOW_RUN_QUEUE')
        cursor.execute(f"SELECT COUNT(*) N,HASH_AGG(*) HASH FROM {META}.APPLICATION_PARAMETERS WHERE NOT (INSTANCE_NAME='DEFAULT' AND PARAMETER_GROUP='ORCHESTRATION' AND PARAMETER_KEY='LEGACY_ENGINE_RETIRED')")
        parameters_hash = cursor.fetchone()
        cursor.execute(f'SELECT * FROM {META}.WORKFLOWS');workflows = cursor.fetchall()
        cursor.execute(f'SELECT * FROM {META}.WORKFLOW_TASKS');tasks = cursor.fetchall()
        cursor.execute('SELECT CURRENT_WAREHOUSE() NAME');warehouse = cursor.fetchone()['NAME']
        plan, summary = plan_catalog(workflows, tasks, warehouse)
        check_approved_jobs(cursor, plan)
        # Refuse unknown/partial destination schemas; this cutover is for the
        # older TST catalog, not an overwrite of an already running new engine.
        cursor.execute("SELECT TABLE_NAME FROM KUMO_TST.INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA='MONITOR_META' AND TABLE_NAME IN ('DEPENDENCIES','WORKFLOW_EXECUTION_LOG')")
        if cursor.fetchall():
            raise ValueError('New TST engine tables already exist; review cutover progress before retrying')
        target_hashes = {table: fingerprint(cursor, META, table) for table in ('WORKFLOW','JOB')}
        cursor.execute('DESCRIBE SERVICE ' + SERVICE);service = cursor.fetchone()
        if service['owner'] != 'KUMO_ADMIN':
            raise ValueError('Use the TST service owner role')
        cursor.execute('SHOW TASKS IN SCHEMA ' + APP)
        old_tasks = [row for row in cursor.fetchall() if re.fullmatch(r'(?:TASK_WF_MASTER_DISPATCHER|TASK_WF_[0-9A-F_]+(?:_SCHEDULER)?|TMP_WF_[0-9A-F_]+)',row['name'])]
        if any(row['owner'] != 'KUMO_ADMIN' or json.loads(row.get('predecessors') or '[]') for row in old_tasks):
            raise ValueError('Review legacy task ownership/graphs before cutover')
        # Verify that the release files are exactly those currently used in DEV.
        procedure_sql = {}
        for name, signature in PROCEDURES.items():
            desired = sql_from(ddl_root/'MONITOR_APP/procedures'/(name+'.sql'))
            cursor.execute('SELECT GET_DDL(\'PROCEDURE\',%s,TRUE) DDL', ('KUMO_DEV.MONITOR_APP.'+name+signature,))
            if logical_body(cursor.fetchone()['DDL']).strip() != logical_body(desired).strip():
                raise ValueError('Saved engine definition differs from DEV: ' + name)
            procedure_sql[name] = desired.replace('KUMO_DEV.', 'KUMO_TST.')
        for schema, name, signature in [('MONITOR_META','SP_GET_ENVIRONMENT','()'), ('MONITOR_APP','SP_RESOLVE_WORKFLOW_COMMAND','(VARIANT,VARCHAR)')]:
            cursor.execute('SELECT GET_DDL(\'PROCEDURE\',%s,TRUE) DDL', (f'KUMO_DEV.{schema}.{name}{signature}',))
            desired = logical_body(cursor.fetchone()['DDL']).replace('KUMO_DEV','KUMO_TST').strip()
            cursor.execute('SELECT GET_DDL(\'PROCEDURE\',%s,TRUE) DDL', (f'KUMO_TST.{schema}.{name}{signature}',))
            if logical_body(cursor.fetchone()['DDL']).strip() != desired:
                raise ValueError('Review shared TST helper drift: ' + name)
        resolve_jobs(cursor, plan['JOB'])
        summary.update(applied=False, environment='TST', old_tasks_to_archive=len(old_tasks),
                       procedures_to_install=list(PROCEDURES), service_auto_resume_before=service['auto_resume'])
        if not apply:
            return summary
        print('Pausing TST service admissions and old tasks', flush=True)
        cursor.execute('ALTER SERVICE ' + SERVICE + ' SET AUTO_RESUME=FALSE')
        cursor.execute('ALTER SERVICE ' + SERVICE + ' SUSPEND')
        for row in old_tasks:
            cursor.execute('ALTER TASK ' + APP + '.' + row['name'] + ' SUSPEND')
        check_idle(cursor)
        for table in ('DEPENDENCIES','WORKFLOW_EXECUTION_LOG'):
            ddl = sql_from(ddl_root/'MONITOR_META/tables'/(table+'.sql')).replace('KUMO_DEV.', 'KUMO_TST.')
            ddl = re.sub(r'CREATE\s+OR\s+REPLACE\s+TABLE', 'CREATE TABLE IF NOT EXISTS', ddl, count=1, flags=re.I)
            cursor.execute(ddl)
        target_hashes['DEPENDENCIES'] = fingerprint(cursor,META,'DEPENDENCIES')
        print('Migrating TST business definitions in one guarded transaction', flush=True)
        replace_catalog(cursor,META,plan,{table:hashes[table] for table in ('WORKFLOWS','WORKFLOW_TASKS')},target_hashes)
        cursor.execute(f"ALTER TABLE {META}.WORKFLOW ADD COLUMN IF NOT EXISTS JOB_EXECUTION_MODE VARCHAR(20) NOT NULL DEFAULT 'SEQUENTIAL'")
        cursor.execute(sql_from(ddl_root/'MONITOR_APP/views/V_ORCH_WORKFLOW_RELATIONS.sql').replace('KUMO_DEV.', 'KUMO_TST.'))
        for name, ddl in procedure_sql.items():
            cursor.execute(ddl)
            cursor.execute('SELECT GET_DDL(\'PROCEDURE\',%s,TRUE) DDL', (APP+'.'+name+PROCEDURES[name],))
            if logical_body(cursor.fetchone()['DDL']).strip() != logical_body(ddl).strip():
                raise RuntimeError('Deployed procedure mismatch: ' + name)
        print('Archiving legacy TST entry points with X_ names', flush=True)
        summary['retirement'] = retirement.run(apply=True, environment='TST')
        for table, before in hashes.items():
            if fingerprint(cursor,META,table) != before:
                raise RuntimeError('Preserved TST configuration/history changed: ' + table)
        if fingerprint(cursor,META,'X_WORKFLOW_RUN_QUEUE') != queue_hash:
            raise RuntimeError('Archived TST queue differs')
        cursor.execute(f"SELECT COUNT(*) N,HASH_AGG(*) HASH FROM {META}.APPLICATION_PARAMETERS WHERE NOT (INSTANCE_NAME='DEFAULT' AND PARAMETER_GROUP='ORCHESTRATION' AND PARAMETER_KEY='LEGACY_ENGINE_RETIRED')")
        if cursor.fetchone() != parameters_hash:
            raise RuntimeError('Unrelated TST parameters changed')
        for workflow in plan['WORKFLOW']:
            if workflow['WORKFLOW_NAME'] not in summary['paused_business_schedules']:
                continue
            cursor.execute(task_ddl(workflow['WORKFLOW_ID'],APP,warehouse,cron=workflow['SCHEDULE'],timezone=workflow['SCHEDULE_TIMEZONE']))
            cursor.execute(f'UPDATE {META}.WORKFLOW SET TASK_NAME=%s WHERE WORKFLOW_ID=%s',(task_fqn(workflow['WORKFLOW_ID'],APP),workflow['WORKFLOW_ID']))
        cursor.execute(sql_from(ddl_root/'MONITOR_APP/tasks/TASK_ORCH_MAINTENANCE.sql').replace('KUMO_DEV.', 'KUMO_TST.'))
        cursor.execute('ALTER TASK '+APP+'.TASK_ORCH_MAINTENANCE RESUME')
        summary.update(applied=True, preserved_tables=list(PROTECTED), protected_fingerprints=hashes,
                       preserved_queue_fingerprint=queue_hash, environment_parameters_preserved=True,
                       business_schedules_started=False, business_commands_executed=False,
                       service_state='SUSPENDED_PENDING_IMAGE_PROMOTION')
        return summary


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--ddl-root',type=Path,default=DDL)
    args=parser.parse_args()
    print(json.dumps(run(args.apply,args.ddl_root),indent=2,default=str))
