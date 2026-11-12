"""Deploy the parallel engine with temporary manual runs and persistent schedulers.

Default is read-only. --apply replaces this engine's suspended v1 manual tasks,
prepares configured workflow schedulers suspended, and enables housekeeping.
Existing engine objects, schedules and execution tables are never changed.
"""
import argparse
import json
import re
import sys

from migration_support import ROOT, connect
from snowflake.connector import DictCursor

sys.path.insert(0, str(ROOT / 'backend'))
import config

PROCEDURES = {
    'SP_ORCH_REQUEST_RUN': '(VARCHAR,VARCHAR,VARCHAR,VARCHAR,VARCHAR)',
    'SP_ORCH_EXECUTE_WORKFLOW': '(VARCHAR)',
    'SP_ORCH_RECONCILE': '(VARCHAR)',
    'SP_ORCH_START_RUN': '(VARCHAR)',
    'SP_ORCH_RUN_SCHEDULED': '(VARCHAR)',
}
DDL = ROOT / 'snowflake' / 'ddl' / 'KUMO_DEV'


def task_fqn(workflow_id, namespace, run_id=None):
    for identifier in (workflow_id, run_id) if run_id is not None else (workflow_id,):
        if not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', identifier):
            raise ValueError('Workflow and run IDs must be UUIDs')
    workflow = workflow_id.replace('-', '_').upper()
    if run_id is not None:
        return namespace + '.TMP_ORCH_WF_' + workflow + '_' + run_id.replace('-', '_').upper()
    return namespace + '.TASK_ORCH_WF_' + workflow + '_SCHEDULER'


def task_ddl(workflow_id, namespace, warehouse, run_id=None, cron=None, timezone='UTC'):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*', warehouse):
        raise ValueError('A single unquoted task warehouse identifier is required')
    address = task_fqn(workflow_id, namespace, run_id)
    if run_id is not None:
        return (f'CREATE TASK {address} WAREHOUSE = {warehouse} '
                "USER_TASK_TIMEOUT_MS = 21600000 TASK_AUTO_RETRY_ATTEMPTS = 0 "
                f"COMMENT = 'KUMO parallel orchestration v2: temporary workflow run {run_id}' "
                f"AS CALL {namespace}.SP_ORCH_EXECUTE_WORKFLOW('{run_id}')")
    cron = re.sub(r'^USING\s+CRON\s+', '', (cron or '').strip(), flags=re.I)
    if len(cron.split()) != 5 or not re.fullmatch(r'[A-Za-z0-9*,/ \-]+', cron):
        raise ValueError('A five-field Snowflake cron expression is required')
    if not re.fullmatch(r'[A-Za-z0-9_/+:-]+', timezone):
        raise ValueError('A valid schedule timezone is required')
    return (f'CREATE OR REPLACE TASK {address} WAREHOUSE = {warehouse} '
            f"SCHEDULE = 'USING CRON {cron} {timezone}' "
            "USER_TASK_TIMEOUT_MS = 21600000 TASK_AUTO_RETRY_ATTEMPTS = 0 "
            "COMMENT = 'KUMO parallel orchestration v2: persistent scheduled workflow' "
            f"AS CALL {namespace}.SP_ORCH_RUN_SCHEDULED('{workflow_id}')")


def render(path):
    return (path.read_text().replace('KUMO_DEV.MONITOR_APP', config.APP_NAMESPACE)
            .replace('KUMO_DEV.MONITOR_META', config.METADATA_NAMESPACE))


def run(apply=False, environment_table='KUMO_DEV.MONITOR_META.APPLICATION_PARAMETERS', warehouse=None):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*){2}', environment_table):
        raise ValueError('Use a full unquoted environment table address')
    with connect() as conn, conn.cursor(DictCursor) as cursor:
        cursor.execute(f"SELECT PARAMETER_KEY, PARAMETER_VALUE, VALUE_TYPE, ACTIVE_FL, IS_SECRET FROM {environment_table} "
                       "WHERE INSTANCE_NAME = 'DEFAULT' AND PARAMETER_GROUP = 'ENVIRONMENT'")
        config.apply_environment(cursor.fetchall())
        warehouse = warehouse or config.DEFAULT_TASK_WAREHOUSE
        cursor.execute(f'SELECT WORKFLOW_ID, WORKFLOW_NAME, TASK_NAME, WAREHOUSE_NAME, SCHEDULE, SCHEDULE_TIMEZONE, IS_SCHEDULE_ENABLED FROM {config.T_ORCHESTRATION_WORKFLOW} ORDER BY WORKFLOW_NAME')
        workflows = cursor.fetchall()
        definitions = [(row, task_ddl(row['WORKFLOW_ID'], config.APP_NAMESPACE, row['WAREHOUSE_NAME'] or warehouse, cron=row['SCHEDULE'], timezone=row['SCHEDULE_TIMEZONE'] or 'UTC'))
                       for row in workflows if row['IS_SCHEDULE_ENABLED'] and row['SCHEDULE']]
        cursor.execute(f"SELECT TABLE_NAME FROM {config.DB}.INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'WORKFLOW_EXECUTION_LOG'", (config.METADATA_SCHEMA,))
        exists = bool(cursor.fetchall())
        if exists:
            cursor.execute(f"SELECT COUNT(*) AS N FROM {config.METADATA_NAMESPACE}.WORKFLOW_EXECUTION_LOG WHERE JOB_ID IS NULL AND STATUS IN ('QUEUED','CLAIMED','RUNNING')")
            if cursor.fetchone()['N']:
                raise ValueError('Finish or cancel pending parallel executions before deploying engine procedures')
        cursor.execute(f"SHOW TASKS LIKE 'TASK_ORCH_WF_%' IN SCHEMA {config.APP_NAMESPACE}")
        tasks = {row['name']: row for row in cursor.fetchall()}
        old_tasks = []
        for row in workflows:
            scheduled_address = task_fqn(row['WORKFLOW_ID'], config.APP_NAMESPACE)
            old_address = scheduled_address.removesuffix('_SCHEDULER')
            if row['TASK_NAME'] not in (None, '', old_address, scheduled_address):
                raise ValueError('A workflow is linked to an unexpected task: ' + row['WORKFLOW_NAME'])
            for address in (old_address, scheduled_address):
                task = tasks.get(address.rsplit('.', 1)[1])
                if not task:
                    continue
                if task['state'] != 'suspended' or not str(task.get('comment', '')).startswith('KUMO parallel orchestration v'):
                    raise ValueError('An existing parallel task needs review: ' + address)
                if address == old_address:
                    expected = f"CALL {config.APP_NAMESPACE}.SP_ORCH_EXECUTE_WORKFLOW('{row['WORKFLOW_ID']}')"
                    if task.get('schedule') or ' '.join(task['definition'].split()).upper() != expected.upper():
                        raise ValueError('Unexpected old parallel task definition: ' + address)
                    old_tasks.append(address)
        # Preflight only these known new procedure names; never replace a name
        # that belongs to an unrelated implementation.
        cursor.execute(f"SELECT PROCEDURE_NAME, COMMENT FROM {config.DB}.INFORMATION_SCHEMA.PROCEDURES WHERE PROCEDURE_SCHEMA = %s AND PROCEDURE_NAME IN (" + ','.join('%s' for _ in PROCEDURES) + ')', (config.SCHEMA, *PROCEDURES))
        for row in cursor.fetchall():
            if not str(row.get('COMMENT') or '').startswith('KUMO parallel orchestration v'):
                raise ValueError('Unexpected existing procedure: ' + row['PROCEDURE_NAME'])
        if apply:
            cursor.execute(render(DDL / 'MONITOR_META/tables/WORKFLOW_EXECUTION_LOG.sql'))
            for procedure in PROCEDURES:
                cursor.execute(render(DDL / ('MONITOR_APP/procedures/' + procedure + '.sql')))
            for address in old_tasks:
                cursor.execute('DROP TASK IF EXISTS ' + address)
            for row, ddl in definitions:
                cursor.execute(ddl)  # CREATE creates schedulers suspended; no activation here.
            scheduled = {row['WORKFLOW_ID'] for row, _ in definitions}
            for row in workflows:
                address = task_fqn(row['WORKFLOW_ID'], config.APP_NAMESPACE) if row['WORKFLOW_ID'] in scheduled else None
                cursor.execute(f'UPDATE {config.T_ORCHESTRATION_WORKFLOW} SET TASK_NAME = %s, WAREHOUSE_NAME = COALESCE(WAREHOUSE_NAME, %s) WHERE WORKFLOW_ID = %s',
                               (address, warehouse, row['WORKFLOW_ID']))
            housekeeping = render(DDL / 'MONITOR_APP/tasks/TASK_ORCH_MAINTENANCE.sql')
            housekeeping = housekeeping.replace('WAREHOUSE = KUMO_ELT_GEN_1', 'WAREHOUSE = ' + warehouse)
            cursor.execute(housekeeping)
            cursor.execute(f'ALTER TASK {config.APP_NAMESPACE}.TASK_ORCH_MAINTENANCE RESUME')
        return dict(applied=apply, namespace=config.METADATA_NAMESPACE, execution_table=config.T_ORCHESTRATION_EXECUTION_LOG,
                    workflow_tasks='created per execution', job_tasks=0, schedulers_prepared=len(definitions),
                    scheduled_workflows_suspended=True, replaced_manual_tasks=len(old_tasks), housekeeping_enabled=apply,
                    legacy_engine_changed=False, procedures=list(PROCEDURES))



if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--environment-table', default='KUMO_DEV.MONITOR_META.APPLICATION_PARAMETERS')
    parser.add_argument('--warehouse')
    args = parser.parse_args()
    print(json.dumps(run(args.apply, args.environment_table, args.warehouse), indent=2))
