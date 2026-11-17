"""Opt-in DEV validation of parallel jobs, frozen mode, timeout and sequential order."""
import argparse
import json
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'migrations'))
from migration_support import connect
from snowflake.connector import DictCursor

APP, META = 'KUMO_DEV.MONITOR_APP', 'KUMO_DEV.MONITOR_META'
TERMINAL = ('SUCCESS', 'FAILED', 'SKIPPED', 'CANCELLED')


def run(timeout_only=False):
    workflows = {mode: str(uuid.uuid4()) for mode in ('PARALLEL', 'SEQUENTIAL')}
    jobs = {mode: [str(uuid.uuid4()) for _ in range(3)] for mode in workflows}
    marker = 'KUMO job execution validation ' + str(uuid.uuid4())
    runs = []
    with connect() as connection, connection.cursor(DictCursor) as cursor:
        def query(sql, params=()):
            cursor.execute(sql, params)
            return cursor.fetchall()

        def call(name, *args):
            result = next(iter(query('CALL ' + APP + '.' + name + '(' + ','.join(['%s'] * len(args)) + ')', args)[0].values()))
            result = json.loads(result) if isinstance(result, str) else result
            assert result.get('ok'), result
            return result

        def insert(table, record):
            query('INSERT INTO ' + META + '.' + table + ' (' + ','.join('"' + key + '"' for key in record) +
                  ') VALUES (' + ','.join(['%s'] * len(record)) + ')', tuple(record.values()))

        def request(mode):
            result = call('SP_ORCH_REQUEST_RUN', workflows[mode], 'WORKFLOW', None, str(uuid.uuid4()), 'JOB_EXECUTION_VALIDATION')
            runs.append(result['runId'])
            return result['runId']

        def records(run_id):
            rows = query('SELECT EXECUTION_ID,JOB_ID,STATUS,STARTED_AT,ENDED_AT,PARENT_EXECUTION_ID,RESULT,ERROR_MESSAGE,TASK_FQN,CONFIG_SNAPSHOT FROM ' +
                         META + '.WORKFLOW_EXECUTION_LOG WHERE WORKFLOW_RUN_ID=%s ORDER BY JOB_POSITION NULLS FIRST', (run_id,))
            for row in rows:
                for key in ('RESULT', 'CONFIG_SNAPSHOT'):
                    if isinstance(row.get(key), str): row[key] = json.loads(row[key])
            return rows

        def done(run_id, expected):
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                rows = records(run_id)
                root = rows[0]
                if root['STATUS'] in TERMINAL:
                    assert root['STATUS'] == expected, rows
                    if (root.get('RESULT') or {}).get('dependencyEvaluation', {}).get('checkedAt'):
                        return rows
                time.sleep(2)
            raise AssertionError(dict(timeout=run_id, rows=rows))

        environment = next(iter(query('CALL ' + META + '.SP_GET_ENVIRONMENT()')[0].values()))
        environment = json.loads(environment) if isinstance(environment, str) else environment
        assert (environment['DATABASE'], environment['META_SCHEMA'], environment['MONITOR_APP_SCHEMA']) == ('KUMO_DEV', 'MONITOR_META', 'MONITOR_APP')
        warehouse = query('SELECT CURRENT_WAREHOUSE() NAME')[0]['NAME']
        try:
            query('BEGIN')
            query('UPDATE ' + META + '.DEPENDENCIES SET ALLOW=ALLOW')
            for mode, workflow_id in workflows.items():
                insert('WORKFLOW', dict(WORKFLOW_ID=workflow_id, WORKFLOW_NAME=marker + ' ' + mode,
                    WORKFLOW_GROUP='JOB EXECUTION VALIDATION', DESCRIPTION=marker, JOB_EXECUTION_MODE=mode,
                    WAREHOUSE_NAME=warehouse, IS_ENABLED=True, IS_SCHEDULE_ENABLED=False, ALLOW_MANUAL_TRIGGER=True))
                insert('DEPENDENCIES', dict(WORKFLOW_ID=workflow_id, ALLOW=True))
                for index, job_id in enumerate(jobs[mode]):
                    seconds = (12 if index == 0 else 20) if mode == 'PARALLEL' else 5
                    insert('JOB', dict(JOB_ID=job_id, JOB_NAME=marker + ' ' + mode + ' ' + str(index + 1),
                        DESCRIPTION=marker, JOB_TYPE='SQL', IS_ENABLED=True, TIMEOUT_SECONDS=120,
                        SQL_COMMAND='SELECT 1' if index == 2 else 'SELECT SYSTEM$WAIT(' + str(seconds) + ')'))
                    insert('DEPENDENCIES', dict(WORKFLOW_ID=workflow_id, JOB_ID=job_id,
                        PARENT_ID=workflow_id if index == 0 else jobs[mode][index - 1], ALLOW=True,
                        SUCCESS=True, ERROR=False, WARNING=False, SQL='SELECT FALSE' if index == 2 else None))
            query('COMMIT')
            print('Created isolated DEV validation workflows; no schedules or downstream triggers.', flush=True)
            if not timeout_only:
                parallel = request('PARALLEL')
                # Queued snapshots must retain the mode that was accepted.
                query('UPDATE ' + META + ".WORKFLOW SET JOB_EXECUTION_MODE='SEQUENTIAL' WHERE WORKFLOW_ID=%s", (workflows['PARALLEL'],))
                call('SP_ORCH_START_RUN', parallel)
                rows = done(parallel, 'SUCCESS')
                root, first, second, skipped = rows
                assert first['STARTED_AT'] < second['ENDED_AT'] and second['STARTED_AT'] < first['ENDED_AT'], rows
                assert all(job['PARENT_EXECUTION_ID'] == parallel for job in rows[1:]), rows
                assert skipped['STATUS'] == 'SKIPPED', rows
                assert root['ENDED_AT'] >= max(job['ENDED_AT'] for job in rows[1:]), rows
                assert root['CONFIG_SNAPSHOT']['jobExecutionMode'] == 'PARALLEL', root
                print('PASS: real parallel job intervals overlap; false condition skips independently; workflow waits for all; frozen mode survives edits.', flush=True)

                sequential = request('SEQUENTIAL')
                call('SP_ORCH_START_RUN', sequential)
                rows = done(sequential, 'SUCCESS')
                assert rows[2]['STARTED_AT'] >= rows[1]['ENDED_AT'], rows
                assert rows[2]['PARENT_EXECUTION_ID'] == rows[1]['EXECUTION_ID'], rows
                print('PASS: sequential jobs retain ordered execution and parent links.', flush=True)

            query('UPDATE ' + META + ".WORKFLOW SET JOB_EXECUTION_MODE='PARALLEL' WHERE WORKFLOW_ID=%s", (workflows['PARALLEL'],))
            query('UPDATE ' + META + '.JOB SET TIMEOUT_SECONDS=3,SQL_COMMAND=%s WHERE JOB_ID=%s', ('SELECT SYSTEM$WAIT(25)', jobs['PARALLEL'][1]))
            failing = request('PARALLEL')
            call('SP_ORCH_START_RUN', failing)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                pending = records(failing)
                if pending[2]['STATUS'] == 'FAILED':
                    assert pending[0]['STATUS'] == 'RUNNING' and pending[1]['STATUS'] == 'RUNNING', pending
                    print('PASS: short timeout is visible while the longer sibling is still running.', flush=True)
                    break
                time.sleep(1)
            else: raise AssertionError('Short job timeout was not recorded promptly')
            rows = done(failing, 'FAILED')
            assert [job['STATUS'] for job in rows[1:]] == ['SUCCESS', 'FAILED', 'SKIPPED'], rows
            assert rows[2]['ERROR_MESSAGE'], rows
            assert rows[0]['ENDED_AT'] >= rows[1]['ENDED_AT'], rows
            print('PASS: one job times out independently; its sibling completes; workflow reports failure only after all jobs settle.', flush=True)
        finally:
            ids = tuple(workflows.values())
            active = query('SELECT EXECUTION_ID,STATUS,TASK_FQN FROM ' + META + '.WORKFLOW_EXECUTION_LOG WHERE JOB_ID IS NULL AND WORKFLOW_ID IN (%s,%s) AND STATUS NOT IN (\'SUCCESS\',\'FAILED\',\'SKIPPED\',\'CANCELLED\')', ids)
            if active:
                print(json.dumps(dict(active_validation_runs_left_for_diagnosis=active, marker=marker), default=str), flush=True)
            else:
                for workflow_id in ids:
                    prefix = 'TMP_ORCH_WF_' + workflow_id.replace('-', '_').upper() + '_'
                    for task in query("SHOW TASKS LIKE '" + prefix + "%' IN SCHEMA " + APP):
                        if task['name'].startswith(prefix): query('DROP TASK IF EXISTS ' + APP + '.' + task['name'])
                query('BEGIN')
                query('UPDATE ' + META + '.DEPENDENCIES SET ALLOW=ALLOW')
                for table in ('WORKFLOW_EXECUTION_LOG', 'DEPENDENCIES', 'WORKFLOW'):
                    query('DELETE FROM ' + META + '.' + table + ' WHERE WORKFLOW_ID IN (%s,%s)', ids)
                all_jobs = tuple(value for values in jobs.values() for value in values)
                query('DELETE FROM ' + META + '.JOB WHERE JOB_ID IN (' + ','.join(['%s'] * len(all_jobs)) + ')', all_jobs)
                query('COMMIT')
                print('Removed validation definitions, execution logs and tasks. WF1–WF10 were unchanged.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--timeout-only', action='store_true')
    args = parser.parse_args()
    if args.run: run(timeout_only=args.timeout_only)
    else: parser.print_help()
