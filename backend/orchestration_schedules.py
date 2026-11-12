"""Native schedulers for the new engine. Manual workflow tasks are untouched."""
import re
import uuid
from threading import RLock
from zoneinfo import ZoneInfo

import config
import snowflake_client as sf
from utils import to_jsonable

_schedule_lock = RLock()


class ScheduleConflict(ValueError):
    pass


def scheduler_fqn(workflow_id):
    return config.APP_NAMESPACE + '.TASK_ORCH_WF_' + str(uuid.UUID(workflow_id)).replace('-', '_').upper() + '_SCHEDULER'


def schedule_values(cron, timezone):
    cron = re.sub(r'^USING\s+CRON\s+', '', str(cron or '').strip(), flags=re.I)
    timezone = str(timezone or 'UTC').strip()
    if len(cron.split()) != 5 or not re.fullmatch(r'[A-Za-z0-9*,/ \-]+', cron):
        raise ValueError('Enter a five-field cron expression')
    if not re.fullmatch(r'[A-Za-z0-9_/+:-]+', timezone):
        raise ValueError('Enter a valid schedule timezone')
    try:
        ZoneInfo(timezone)
    except Exception:
        raise ValueError('Unknown schedule timezone: ' + timezone)
    return cron, timezone


def native_scheduler(workflow_id):
    address = scheduler_fqn(workflow_id)
    name = address.rsplit('.', 1)[1]
    rows = sf.query_service(f"SHOW TASKS LIKE '{name}' IN SCHEMA {config.APP_NAMESPACE}")
    matches = [{key.lower(): value for key, value in row.items()} for row in rows if (row.get('name') or row.get('NAME')) == name]
    if len(matches) > 1:
        raise ScheduleConflict('Ambiguous workflow scheduler')
    task = matches[0] if matches else None
    if task:
        expected = f"CALL {config.APP_NAMESPACE}.SP_ORCH_RUN_SCHEDULED('{workflow_id}')"
        if (not str(task.get('comment') or '').startswith('KUMO parallel orchestration v2: persistent scheduled workflow')
                or ' '.join(str(task.get('definition') or '').split()).upper().rstrip(';') != expected.upper()):
            raise ScheduleConflict('The workflow scheduler has an unexpected definition')
    return task


def sync_workflow(workflow):
    """Apply saved intent. DDL is deliberately outside catalog transactions."""
    workflow_id = workflow['WORKFLOW_ID']
    address = scheduler_fqn(workflow_id)
    task = native_scheduler(workflow_id)
    scheduled = bool(workflow.get('IS_SCHEDULE_ENABLED'))
    if not scheduled:
        if task and task['state'].lower() != 'suspended':
            sf.execute_service('ALTER TASK ' + address + ' SUSPEND')
        return dict(taskFqn=address if task else None, state='SUSPENDED' if task else 'NOT SCHEDULED')
    cron, timezone = schedule_values(workflow.get('SCHEDULE'), workflow.get('SCHEDULE_TIMEZONE'))
    warehouse = workflow.get('WAREHOUSE_NAME') or config.DEFAULT_TASK_WAREHOUSE
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*', warehouse):
        raise ValueError('Configure a valid workflow warehouse')
    schedule = f'USING CRON {cron} {timezone}'
    if task is None:
        sf.execute_service(f"CREATE TASK {address} WAREHOUSE = {warehouse} SCHEDULE = '{schedule}' "
                           "USER_TASK_TIMEOUT_MS = 21600000 TASK_AUTO_RETRY_ATTEMPTS = 0 "
                           "COMMENT = 'KUMO parallel orchestration v2: persistent scheduled workflow' "
                           f"AS CALL {config.APP_NAMESPACE}.SP_ORCH_RUN_SCHEDULED('{workflow_id}')")
    elif task.get('schedule') != schedule or str(task.get('warehouse') or '').upper() != warehouse.upper():
        sf.execute_service('ALTER TASK ' + address + ' SUSPEND')
        sf.execute_service(f"ALTER TASK {address} SET WAREHOUSE = {warehouse} SCHEDULE = '{schedule}'")
    should_run = bool(workflow.get('IS_ENABLED'))
    if should_run:
        # RESUME is idempotent. It schedules future occurrences, never EXECUTE TASK.
        sf.execute_service('ALTER TASK ' + address + ' RESUME')
    elif task is None or task['state'].lower() != 'suspended':
        sf.execute_service('ALTER TASK ' + address + ' SUSPEND')
    sf.execute_service(f'UPDATE {config.T_ORCHESTRATION_WORKFLOW} SET TASK_NAME=%(task)s WHERE WORKFLOW_ID=%(workflow)s',
                       dict(task=address, workflow=workflow_id))
    return dict(taskFqn=address, state='STARTED' if should_run else 'SUSPENDED')


def sync_saved_workflow(workflow_id):
    def read():
        rows = sf.query_service(f'SELECT * FROM {config.T_ORCHESTRATION_WORKFLOW} WHERE WORKFLOW_ID=%(workflow)s', {'workflow': workflow_id})
        if len(rows) != 1:
            raise ValueError('Workflow not found')
        return rows[0]
    keys = ('IS_ENABLED','IS_SCHEDULE_ENABLED','SCHEDULE','SCHEDULE_TIMEZONE','WAREHOUSE_NAME')
    with _schedule_lock:
        for _ in range(3):
            row = read()
            if not row.get('IS_SCHEDULE_ENABLED') and not row.get('TASK_NAME'):
                return None
            result = sync_workflow(row)
            latest = read()
            if all(row.get(key) == latest.get(key) for key in keys):
                return result
        raise ScheduleConflict('Schedule configuration changed repeatedly. Refresh and retry synchronization.')


def after_catalog_write(workflow_id):
    if not config.ORCHESTRATION_RUNS_ENABLED or config.USE_MOCK or not sf.is_configured():
        return {}
    try:
        return dict(scheduler=sync_saved_workflow(workflow_id))
    except Exception as exc:
        return dict(scheduleSynced=False,warning='Saved, but the scheduler could not be synchronized: ' + str(exc))


def remove_scheduler(workflow_id):
    with _schedule_lock:
        task = native_scheduler(workflow_id)
        if task:
            address = scheduler_fqn(workflow_id)
            sf.execute_service('ALTER TASK ' + address + ' SUSPEND')
            sf.execute_service('DROP TASK ' + address)


def read_schedules(catalog):
    result, warnings = {}, []
    for workflow in catalog['workflows']:
        if not workflow.get('IS_SCHEDULE_ENABLED'):
            continue
        workflow_id = workflow['WORKFLOW_ID']
        try:
            task = native_scheduler(workflow_id)
            state = str((task or {}).get('state') or 'MISSING').upper()
            value = dict(state=state, nextRun=None)
            if state == 'STARTED' and workflow.get('IS_ENABLED'):
                rows = sf.query_service(f'''SELECT MIN(SCHEDULED_TIME) AS NEXT_RUN
                    FROM TABLE({config.DB}.INFORMATION_SCHEMA.TASK_HISTORY(
                        TASK_NAME=>%(task)s, DATABASE_NAME=>%(database)s, SCHEMA_NAME=>%(schema)s, RESULT_LIMIT=>1000))
                    WHERE STATE='SCHEDULED' AND SCHEDULED_FROM='SCHEDULE' AND SCHEDULED_TIME>CURRENT_TIMESTAMP()''',
                    dict(task=scheduler_fqn(workflow_id).rsplit('.', 1)[1], database=config.DB, schema=config.SCHEMA))
                value['nextRun'] = to_jsonable(rows[0].get('NEXT_RUN')) if rows else None
            result[workflow_id] = value
        except Exception:
            result[workflow_id] = dict(state='UNAVAILABLE', nextRun=None)
            warnings.append('Scheduler status is unavailable for ' + workflow['WORKFLOW_NAME'] + '.')
    return result, warnings
