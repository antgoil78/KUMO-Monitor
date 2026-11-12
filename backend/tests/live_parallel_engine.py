"""Explicit disposable validation of temporary workflow runs and scheduled runs.

Never executes migrated jobs. Creates SELECT/dbt-parse fixtures, checks isolated
scopes, enabled-job order, queueing, cleanup and timeout recovery, then removes
its own rows/tasks and compares the existing engine's definitions and rows.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'migrations'))
from migration_support import connect
from apply_parallel_engine import task_ddl, task_fqn
from snowflake.connector import DictCursor

META = 'KUMO_DEV.MONITOR_META'
APP = 'KUMO_DEV.MONITOR_APP'


def unpack(row):
    value = next(iter(row.values()))
    return json.loads(value) if isinstance(value, str) else value


def run(timeout_only=False, scheduled_only=False):
    workflow = str(uuid.uuid4())
    jobs = [str(uuid.uuid4()) for _ in range(3)]
    scheduler = task_fqn(workflow, APP)
    old_objects = [('TABLE', META + '.' + name) for name in ('WORKFLOW_HISTORY', 'WORKFLOW_RUN_QUEUE', 'WORKFLOWS', 'WORKFLOW_TASKS')]
    old_objects += [('PROCEDURE', APP + '.SP_RUN_WORKFLOW(VARCHAR,VARCHAR)'),
                    ('PROCEDURE', APP + '.SP_WORKFLOW_REQUEST_RUN(VARCHAR,VARCHAR,VARCHAR,NUMBER,VARCHAR)'),
                    ('TASK', APP + '.TASK_WF_MASTER_DISPATCHER')]
    with connect() as conn, conn.cursor(DictCursor) as cursor:
        def query(sql, params=()):
            cursor.execute(sql, params)
            return cursor.fetchall()
        def object_hashes():
            return {address: hashlib.sha256(next(iter(query('SELECT GET_DDL(%s,%s,TRUE)', (kind, address))[0].values())).encode()).hexdigest()
                    for kind, address in old_objects}
        def old_state():
            return {table: query(f"SELECT COUNT(*) AS N,HASH_AGG(*) AS H FROM {META}.{('X_' if retired and retired[0]['PARAMETER_VALUE']=='true' and table=='WORKFLOW_RUN_QUEUE' else '')+table}")[0]
                    for table in ('WORKFLOWS', 'WORKFLOW_TASKS', 'WORKFLOW_HISTORY', 'WORKFLOW_RUN_QUEUE')}
        def request(mode='WORKFLOW', job=None, request_id=None, expect_ok=True):
            result = unpack(query(f'CALL {APP}.SP_ORCH_REQUEST_RUN(%s,%s,%s,%s,%s)',
                                  (workflow, mode, job, request_id or str(uuid.uuid4()), 'PARALLEL_ENGINE_VALIDATION'))[0])
            assert bool(result.get('ok')) == expect_ok, result
            return result
        def fire(run_id):
            result = unpack(query(f'CALL {APP}.SP_ORCH_START_RUN(%s)', (run_id,))[0])
            assert result.get('ok'), result
            return result
        def records(run_id):
            return query(f'SELECT * FROM {META}.WORKFLOW_EXECUTION_LOG WHERE WORKFLOW_RUN_ID=%s ORDER BY JOB_POSITION NULLS FIRST', (run_id,))
        def await_runs(run_ids, timeout=300, recover=False):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if recover:
                    result = unpack(query(f'CALL {APP}.SP_ORCH_RECONCILE(%s)', (workflow,))[0])
                    assert not result.get('errors'), result
                all_rows = [records(run_id) for run_id in run_ids]
                if all(rows and rows[0]['STATUS'] not in ('QUEUED', 'CLAIMED', 'RUNNING') for rows in all_rows):
                    return all_rows
                time.sleep(2)
            raise AssertionError([[(row['JOB_ID'], row['STATUS'], row['ERROR_MESSAGE']) for row in rows] for rows in all_rows])
        def temporary_tasks():
            prefix = 'TMP_ORCH_WF_' + workflow.replace('-', '_').upper() + '_'
            return [row for row in query(f"SHOW TASKS LIKE '{prefix}%' IN SCHEMA {APP}") if row['name'].startswith(prefix)]
        def await_cleanup():
            deadline = time.monotonic() + 45
            while temporary_tasks() and time.monotonic() < deadline:
                time.sleep(1)
            assert not temporary_tasks(), 'Temporary workflow tasks were not removed'
        retired = query(f"SELECT PARAMETER_VALUE FROM {META}.APPLICATION_PARAMETERS WHERE INSTANCE_NAME='DEFAULT' AND PARAMETER_GROUP='ORCHESTRATION' AND PARAMETER_KEY='LEGACY_ENGINE_RETIRED' AND ACTIVE_FL=TRUE")
        if retired and retired[0]['PARAMETER_VALUE']=='true':
            old_objects = [(kind, address.replace('.WORKFLOW_RUN_QUEUE','.X_WORKFLOW_RUN_QUEUE').replace('.SP_RUN_WORKFLOW(','.X_SP_RUN_WORKFLOW(').replace('.SP_WORKFLOW_REQUEST_RUN(','.X_SP_WORKFLOW_REQUEST_RUN(').replace('.TASK_WF_MASTER_DISPATCHER','.X_TASK_WF_MASTER_DISPATCHER')) for kind,address in old_objects]
        baseline_objects, baseline_state = object_hashes(), old_state()
        results = {}
        print('Disposable validation workflow: ' + workflow, flush=True)
        try:
            query(f'INSERT INTO {META}.WORKFLOW(WORKFLOW_ID,WORKFLOW_NAME,WORKFLOW_GROUP,WAREHOUSE_NAME) VALUES(%s,%s,%s,%s)',
                  (workflow, 'PARALLEL ENGINE VALIDATION ' + workflow[:8], 'ENGINE_VALIDATION', 'KUMO_ELT_GEN_1'))
            query(f'INSERT INTO {META}.DEPENDENCIES(WORKFLOW_ID,ALLOW) VALUES(%s,TRUE)', (workflow,))
            parent = workflow
            for index, job in enumerate(jobs):
                query(f'INSERT INTO {META}.JOB(JOB_ID,JOB_NAME,JOB_TYPE,SQL_COMMAND) VALUES(%s,%s,%s,%s)',
                      (job, 'ENGINE VALIDATION ' + workflow[:8] + ' ' + str(index + 1), 'SQL', f"SELECT '{index + 1}' AS FIXTURE; SELECT {index + 1} AS VALUE;"))
                query(f'INSERT INTO {META}.DEPENDENCIES(WORKFLOW_ID,JOB_ID,PARENT_ID,TRIGGER_TYPE,SUCCESS,WARNING,ERROR,ALLOW) VALUES(%s,%s,%s,%s,%s,%s,%s,TRUE)',
                      (workflow, job, parent, 'DEP', True if index else None, False if index else None, False if index else None))
                parent = job
            assert not temporary_tasks()
            housekeeping = unpack(query(f'CALL {APP}.SP_ORCH_RECONCILE(NULL)')[0])
            assert housekeeping.get('ok') and not housekeeping.get('errors'), housekeeping
            results['no_task_for_idle_unscheduled_workflow'] = True
            if not timeout_only and not scheduled_only:
                stable = str(uuid.uuid4())
                full, repeated = request(request_id=stable), request(request_id=stable)
                assert full['runId'] == repeated['runId'] and repeated['reused']
                only, onward = request('JOB_ONLY', jobs[1]), request('FROM_JOB', jobs[1])
                assert len({row['taskFqn'] for row in (full, only, onward)}) == 3
                try:
                    query(f'CALL {APP}.SP_ORCH_EXECUTE_WORKFLOW(%s)', (full['runId'],))
                except Exception:
                    results['direct_calls_rejected'] = True
                else:
                    raise AssertionError('Executor allowed CALL outside a workflow task')
                fire(full['runId'])
                full_rows, only_rows, onward_rows = await_runs([full['runId'], only['runId'], onward['runId']])
                assert [row['JOB_ID'] for row in full_rows[1:]] == jobs
                assert [row['JOB_ID'] for row in only_rows[1:] if row['STATUS'] != 'SKIPPED'] == jobs[1:2]
                assert [row['JOB_ID'] for row in onward_rows[1:] if row['STATUS'] != 'SKIPPED'] == jobs[1:]
                assert all(row['STATUS'] in ('SUCCESS','SKIPPED') for rows in (full_rows, only_rows, onward_rows) for row in rows), (full_rows, only_rows, onward_rows)
                assert len({rows[0]['TASK_RUN_GROUP_ID'] for rows in (full_rows, only_rows, onward_rows)}) == 3
                assert all(row['QUERY_ID'] for rows in (full_rows, only_rows, onward_rows) for row in rows[1:] if row['STATUS']=='SUCCESS')
                await_cleanup()
                assert fire(full['runId'])['wakeupScheduled'] is False
                results['distinct_full_only_from_tasks_and_queue_drain'] = True
                print('PASS: full / only / from have separate temporary workflow tasks; queue drains and tasks remove themselves', flush=True)

                # Disabled middle jobs must neither resolve failing SQL nor
                # propagate SKIPPED to the next enabled job.
                query(f'UPDATE {META}.JOB SET IS_ENABLED=FALSE,SQL_COMMAND=%s WHERE JOB_ID=%s', ('SELECT 1/0;', jobs[1]))
                enabled = request()
                fire(enabled['runId'])
                enabled_rows = await_runs([enabled['runId']])[0]
                assert [row['JOB_ID'] for row in enabled_rows[1:] if row['STATUS']=='SUCCESS'] == [jobs[0], jobs[2]]
                assert [row['STATUS'] for row in enabled_rows] == ['SUCCESS','SUCCESS','SKIPPED','SUCCESS'], enabled_rows
                assert [row['JOB_POSITION'] for row in enabled_rows[1:] if row['STATUS']=='SUCCESS'] == [1, 3]
                results['enabled_jobs_execute_in_order_across_disabled_jobs'] = True
                query(f'UPDATE {META}.JOB SET IS_ENABLED=TRUE,JOB_TYPE=%s,SQL_COMMAND=NULL,DBT_COMMAND=%s,DBT_TARGET=%s,DBT_PROJECT_FQN=%s WHERE JOB_ID=%s',
                      ('DBT', 'dbt parse', '[DBT_TARGET]', '[DATABASE].[DBT_SCHEMA].[DBT_PROJECT]', jobs[1]))
                parse = request('JOB_ONLY', jobs[1])
                fire(parse['runId'])
                assert [row['STATUS'] for row in await_runs([parse['runId']])[0]] == ['SUCCESS','SKIPPED','SUCCESS','SKIPPED']
                results['temporary_task_dbt_parse'] = True
                print('PASS: enabled-job order and temporary-task dbt parse', flush=True)

            if not timeout_only:
                query(f'UPDATE {META}.JOB SET IS_ENABLED=TRUE,JOB_TYPE=%s,SQL_COMMAND=NULL,DBT_COMMAND=%s,DBT_TARGET=%s,DBT_PROJECT_FQN=%s WHERE JOB_ID=%s',
                      ('DBT', 'dbt parse', '[DBT_TARGET]', '[DATABASE].[DBT_SCHEMA].[DBT_PROJECT]', jobs[1]))
                # A scheduled workflow keeps its scheduler. A manual partial
                # run still gets a separate task and never edits that scheduler.
                query(f'UPDATE {META}.WORKFLOW SET IS_SCHEDULE_ENABLED=TRUE,SCHEDULE=%s,SCHEDULE_TIMEZONE=%s,TASK_NAME=%s WHERE WORKFLOW_ID=%s',
                      ('0 0 29 2 *', 'UTC', scheduler, workflow))
                query(task_ddl(workflow, APP, 'KUMO_ELT_GEN_1', cron='0 0 29 2 *'))
                scheduler_before = query('SELECT GET_DDL(%s,%s)', ('TASK', scheduler))
                partial = request('JOB_ONLY', jobs[0])
                fire(partial['runId'])
                assert [row['STATUS'] for row in await_runs([partial['runId']])[0]] == ['SUCCESS','SUCCESS','SKIPPED','SKIPPED']
                assert query('SELECT GET_DDL(%s,%s)', ('TASK', scheduler)) == scheduler_before
                query('EXECUTE TASK ' + scheduler)
                deadline = time.monotonic() + 60
                scheduled = []
                while not scheduled and time.monotonic() < deadline:
                    scheduled = query(f"SELECT EXECUTION_ID FROM {META}.WORKFLOW_EXECUTION_LOG WHERE WORKFLOW_ID=%s AND JOB_ID IS NULL AND TRIGGER_SOURCE='SCHEDULED'", (workflow,))
                    if not scheduled:
                        time.sleep(2)
                assert scheduled, 'Scheduled task did not record an execution'
                scheduled_rows = await_runs([scheduled[0]['EXECUTION_ID']])[0]
                assert len(scheduled_rows) == 4 and all(row['STATUS'] == 'SUCCESS' for row in scheduled_rows), scheduled_rows
                assert all(row['TASK_FQN'] == scheduler for row in scheduled_rows)
                assert query('SELECT GET_DDL(%s,%s)', ('TASK', scheduler)) == scheduler_before
                results['persistent_scheduler_and_separate_manual_scope'] = True
                print('PASS: scheduled workflow runs all enabled jobs in its persistent task, including dbt; manual partial task leaves it unchanged', flush=True)
                query(f'UPDATE {META}.WORKFLOW SET IS_SCHEDULE_ENABLED=FALSE WHERE WORKFLOW_ID=%s', (workflow,))

            query(f'UPDATE {META}.JOB SET JOB_TYPE=%s,SQL_COMMAND=%s,DBT_COMMAND=NULL WHERE JOB_ID=%s', ('SQL', 'SELECT SYSTEM$WAIT(120);', jobs[1]))
            timed = request('JOB_ONLY', jobs[1])
            # Create the exact run-specific task with a shorter test timeout
            # before launch, so EXECUTE TASK captures that timeout in its version.
            query(task_ddl(workflow, APP, 'KUMO_ELT_GEN_1', run_id=timed['runId']).replace('21600000', '45000'))
            fire(timed['runId'])
            timeout_rows = await_runs([timed['runId']], timeout=180, recover=True)[0]
            assert timeout_rows[0]['STATUS'] in ('FAILED', 'CANCELLED'), timeout_rows
            assert timeout_rows[2]['STATUS'] in ('FAILED', 'CANCELLED')
            assert timeout_rows[1]['STATUS'] == timeout_rows[3]['STATUS'] == 'SKIPPED', timeout_rows
            await_cleanup()
            results['task_failure_reconciliation_and_cleanup'] = True
            print('PASS: task timeout recovered from task history; temporary task removed', flush=True)
        finally:
            for task in temporary_tasks() + [{'name': scheduler.rsplit('.', 1)[1]}]:
                address = APP + '.' + task['name']
                try:
                    query('SELECT SYSTEM$USER_TASK_CANCEL_ONGOING_EXECUTIONS(%s)', (address,))
                except Exception:
                    pass
                query('DROP TASK IF EXISTS ' + address)
            query(f'DELETE FROM {META}.WORKFLOW_EXECUTION_LOG WHERE WORKFLOW_ID=%s', (workflow,))
            query(f'DELETE FROM {META}.DEPENDENCIES WHERE WORKFLOW_ID=%s', (workflow,))
            for job in jobs:
                query(f'DELETE FROM {META}.JOB WHERE JOB_ID=%s', (job,))
            query(f'DELETE FROM {META}.WORKFLOW WHERE WORKFLOW_ID=%s', (workflow,))
            assert object_hashes() == baseline_objects, 'Existing engine object definitions changed'
            assert old_state() == baseline_state, 'Existing engine catalog or execution rows changed'
            results['legacy_definitions_and_rows_unchanged'] = True
            results['fixtures_removed'] = True
        return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--timeout-only', action='store_true')
    parser.add_argument('--scheduled-only', action='store_true', help='Check scheduling and timeout recovery after earlier scope checks')
    args = parser.parse_args()
    if not args.run:
        parser.print_help()
    else:
        print(json.dumps(run(timeout_only=args.timeout_only, scheduled_only=args.scheduled_only), indent=2))
