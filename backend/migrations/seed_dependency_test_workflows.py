"""Extend the DEV dependency test fixtures to WF1-WF10 with observable delays.

Read-only unless --apply. Preserves existing dependency rule settings and adds
only missing test definitions/edges. Does not start runs or create schedules.
"""
import argparse
import json
import re
import uuid

from migration_support import connect
from snowflake.connector import DictCursor

META = 'KUMO_DEV.MONITOR_META'
MARKER = 'KUMO dependency rule test fixture WF1-WF10 v2'
LEGACY_MARKER = 'KUMO dependency rule test fixture WF1-WF4 v1'
NAMES = ['WF' + str(n) for n in range(1, 11)]
IDS = {name: str(uuid.uuid5(uuid.NAMESPACE_URL, 'kumo-monitor:DEV:dependency-rule-test:' + name)) for name in NAMES}
JOBS = {name: str(uuid.uuid5(uuid.NAMESPACE_URL, 'kumo-monitor:DEV:dependency-rule-test:' + name + ':job')) for name in NAMES}
PARENTS = {'WF1': [], 'WF2': ['WF1'], 'WF3': ['WF1'], 'WF4': ['WF2', 'WF3'],
           'WF5': ['WF4'], 'WF6': ['WF4'], 'WF7': ['WF5', 'WF6'],
           'WF8': ['WF7'], 'WF9': ['WF7'], 'WF10': ['WF8', 'WF9']}
DELAYS = dict(zip(NAMES, (20, 30, 60, 20, 30, 60, 20, 30, 60, 20)))


def command(name):
    return ("SELECT '" + name + "' AS WORKFLOW_NAME, TRUE AS DUMMY_JOB, "
            "SYSTEM$WAIT(" + str(DELAYS[name]) + ", 'SECONDS') AS DELAY_RESULT")


def placeholders(values):
    return ','.join(['%s'] * len(values))


def plan(workflows, jobs, rules, warehouse):
    if not warehouse:
        raise ValueError('Select a DEV task warehouse')
    existing_workflows, existing_jobs = {}, {}
    for rows, ids, identity, name_field, target in (
            (workflows, IDS, 'WORKFLOW_ID', 'WORKFLOW_NAME', existing_workflows),
            (jobs, JOBS, 'JOB_ID', 'JOB_NAME', existing_jobs)):
        reverse = {value: name for name, value in ids.items()}
        for row in rows:
            name = reverse.get(row[identity])
            if (not name or name in target or row[name_field] != (name if identity == 'WORKFLOW_ID' else name + ' TEST JOB')
                    or row.get('DESCRIPTION') not in (MARKER, LEGACY_MARKER)):
                raise ValueError('Conflicting or duplicate test workflow/job name or identity')
            target[name] = row
    desired_rules = []
    for name in NAMES:
        for parent in PARENTS[name] or [None]:
            desired_rules.append(dict(WORKFLOW_ID=IDS[name], JOB_ID=None,
                PARENT_ID=IDS[parent] if parent else None, TRIGGER_TYPE='DEP' if parent else None,
                SUCCESS=True if parent else None, WARNING=False if parent else None,
                ERROR=False if parent else None, SQL=None, ALLOW=True, PARENT_MUST_BE_NEWER=bool(parent)))
        desired_rules.append(dict(WORKFLOW_ID=IDS[name], JOB_ID=JOBS[name], PARENT_ID=IDS[name],
            TRIGGER_TYPE='DEP', SUCCESS=None, WARNING=None, ERROR=None, SQL=None, ALLOW=True,
            PARENT_MUST_BE_NEWER=False))
    def key(row):
        return (row['WORKFLOW_ID'], row['JOB_ID'], row['PARENT_ID'])
    expected = {key(row): row for row in desired_rules}
    actual = {}
    for row in rules:
        identity = key(row)
        if identity not in expected or identity in actual:
            raise ValueError('Unexpected or duplicate dependency touching these test workflows/jobs')
        actual[identity] = row
    for name in existing_workflows:
        expected_keys = {k for k in expected if k[0] == IDS[name]}
        actual_keys = {k for k in actual if k[0] == IDS[name]}
        if expected_keys != actual_keys:
            raise ValueError('Existing fixture parent/job graph was edited: ' + name)
    inserts = {'WORKFLOW': [], 'JOB': [], 'DEPENDENCIES': [row for k, row in expected.items() if k not in actual]}
    updates = {'WORKFLOW': [], 'JOB': []}
    for name in NAMES:
        workflow = existing_workflows.get(name)
        if workflow:
            if workflow.get('IS_SCHEDULE_ENABLED') or workflow.get('WORKFLOW_GROUP') != 'DEPENDENCY TEST':
                raise ValueError('Existing fixture is scheduled or has a different group: ' + name)
            if workflow['DESCRIPTION'] != MARKER:
                updates['WORKFLOW'].append(dict(WORKFLOW_ID=IDS[name], DESCRIPTION=MARKER))
        else:
            inserts['WORKFLOW'].append(dict(WORKFLOW_ID=IDS[name], WORKFLOW_NAME=name,
                WORKFLOW_GROUP='DEPENDENCY TEST', DESCRIPTION=MARKER, WAREHOUSE_NAME=warehouse,
                IS_ENABLED=True, IS_SCHEDULE_ENABLED=False, ALLOW_MANUAL_TRIGGER=True,
                ALLOW_EXTERNAL_TRIGGER=False, TASK_NAME=None, SCHEDULE=None))
        job = existing_jobs.get(name)
        if job:
            base = "SELECT '" + name + "' AS WORKFLOW_NAME, TRUE AS DUMMY_JOB"
            delayed = re.escape(base) + r", SYSTEM\$WAIT\(\d+, 'SECONDS'\) AS DELAY_RESULT"
            if job.get('JOB_TYPE') != 'SQL' or not (job.get('SQL_COMMAND') == base or re.fullmatch(delayed, job.get('SQL_COMMAND') or '')):
                raise ValueError('Existing fixture SQL was changed: ' + name)
            change = dict(JOB_ID=JOBS[name], SQL_COMMAND=command(name), TIMEOUT_SECONDS=180, DESCRIPTION=MARKER)
            if any(job.get(field) != value for field, value in change.items()):
                updates['JOB'].append(change)
        else:
            inserts['JOB'].append(dict(JOB_ID=JOBS[name], JOB_NAME=name + ' TEST JOB', DESCRIPTION=MARKER,
                JOB_TYPE='SQL', JOB_LAYER='GENERAL', IS_ENABLED=True, TIMEOUT_SECONDS=180, SQL_COMMAND=command(name)))
    return inserts, updates


def run(apply=False):
    with connect() as conn, conn.cursor(DictCursor) as cursor:
        def query(sql, args=()):
            cursor.execute(sql, args)
            return cursor.fetchall()
        env = next(iter(query(f'CALL {META}.SP_GET_ENVIRONMENT()')[0].values()))
        env = json.loads(env) if isinstance(env, str) else env
        if (env.get('DATABASE'), env.get('META_SCHEMA'), env.get('MONITOR_APP_SCHEMA')) != ('KUMO_DEV', 'MONITOR_META', 'MONITOR_APP'):
            raise ValueError('Expected DEV app routing')
        transaction = False
        try:
            if apply:
                query('BEGIN'); transaction = True
                query(f'UPDATE {META}.DEPENDENCIES SET ALLOW = ALLOW')
            pending = query(f"SELECT COUNT(*) N FROM {META}.WORKFLOW_EXECUTION_LOG WHERE JOB_ID IS NULL AND WORKFLOW_ID IN ({placeholders(IDS)}) AND STATUS IN ('PENDING','INITIATING','QUEUED','CLAIMED','RUNNING')", tuple(IDS.values()))[0]['N']
            if pending:
                raise ValueError('Finish active test workflows before changing their fixtures')
            wf = query(f'SELECT * FROM {META}.WORKFLOW WHERE WORKFLOW_ID IN ({placeholders(IDS)}) OR WORKFLOW_NAME IN ({placeholders(NAMES)})', tuple(IDS.values()) + tuple(NAMES))
            jobs = query(f'SELECT * FROM {META}.JOB WHERE JOB_ID IN ({placeholders(JOBS)}) OR JOB_NAME IN ({placeholders(JOBS)})', tuple(JOBS.values()) + tuple(name + ' TEST JOB' for name in NAMES))
            rules = query(f'SELECT * FROM {META}.DEPENDENCIES WHERE WORKFLOW_ID IN ({placeholders(IDS)}) OR PARENT_ID IN ({placeholders(IDS)}) OR JOB_ID IN ({placeholders(JOBS)})', tuple(IDS.values()) + tuple(IDS.values()) + tuple(JOBS.values()))
            warehouse = next((row['WAREHOUSE_NAME'] for row in wf if row['WORKFLOW_ID'] == IDS['WF1'] and row.get('WAREHOUSE_NAME')), None)
            warehouse = warehouse or query('SELECT CURRENT_WAREHOUSE() NAME')[0]['NAME']
            inserts, updates = plan(wf, jobs, rules, warehouse)
            if apply:
                for table, rows in inserts.items():
                    for row in rows:
                        columns = ','.join('"' + key + '"' for key in row)
                        query(f'INSERT INTO {META}.{table} ({columns}) VALUES ({placeholders(row)})', tuple(row.values()))
                for table, rows in updates.items():
                    identity = 'WORKFLOW_ID' if table == 'WORKFLOW' else 'JOB_ID'
                    for row in rows:
                        fields = [key for key in row if key != identity]
                        sets = ','.join('"' + key + '"=%s' for key in fields)
                        query(f'UPDATE {META}.{table} SET {sets},UPDATED_AT=SYSDATE(),UPDATED_BY=CURRENT_USER() WHERE {identity}=%s', tuple(row[key] for key in fields) + (row[identity],))
                query('COMMIT'); transaction = False
            return dict(applied=apply, environment='DEV',
                inserted={key: len(value) for key, value in inserts.items()},
                updated={key: len(value) for key, value in updates.items()},
                workflows=[dict(name=name, parents=PARENTS[name], delay_seconds=DELAYS[name]) for name in NAMES],
                existing_rule_settings_preserved=True, executions_started=0, schedules_created=0)
        except Exception:
            if transaction:
                query('ROLLBACK')
            raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    print(json.dumps(run(parser.parse_args().apply), indent=2))
