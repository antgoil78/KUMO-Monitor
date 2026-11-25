"""Explicit DEV-only disposable task lifecycle and idle check.

Run from the repository root after loading local environment files:
  python backend/tests/live_postgres_task_engine.py --run
No business workflow, schedule, service or static Snowflake DDL is changed.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from task_engine.settings import Settings
from task_engine.snowflake_tasks import SnowflakeTasks
from task_engine.store import Store
from task_engine.worker import Worker


class ObservedTasks(SnowflakeTasks):
    def __init__(self, settings):
        super().__init__(settings)
        self.executions = {}
        self.observations = {}
        self.lost_ack = False

    def execute(self, job):
        key = str(job['id'])
        self.executions[key] = self.executions.get(key, 0) + 1
        super().execute(job)
        if job['job_key'] == 'lost_ack':
            self.lost_ack = True
            raise ConnectionError('Injected acknowledgment loss after native EXECUTE succeeded')

    def history(self, job):
        result = super().history(job)
        if result:
            self.observations[str(job['id'])] = result
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', required=True)
    parser.add_argument('--idle-seconds', type=int, default=90)
    parser.add_argument('--resume', help='Resume a recorded fixture suffix without creating new runs')
    args = parser.parse_args()
    if args.idle_seconds < 60:
        parser.error('Use at least 60 seconds for the idle check')
    db = Store(Settings.from_environment())
    db.initialize()
    tasks = ObservedTasks(db.settings)
    worker = Worker(db, tasks)
    suffix = args.resume or uuid.uuid4().hex[:10]
    cases = {
        'cancel': dict(jobs=[dict(key='cancel', sql="SELECT SYSTEM$WAIT(90, 'SECONDS')", timeoutSeconds=120)]),
        'parallel': dict(mode='PARALLEL', jobs=[dict(key='one', sql="SELECT SYSTEM$WAIT(30, 'SECONDS')"), dict(key='two', sql="SELECT SYSTEM$WAIT(30, 'SECONDS')")]),
        'success': dict(jobs=[dict(key='first', sql='SELECT 1'), dict(key='second', sql='SELECT 2')]),
        'failure': dict(jobs=[dict(key='fails', sql='SELECT 1/0'), dict(key='skips', sql='SELECT 2')]),
        'timeout': dict(jobs=[dict(key='timeout', sql="SELECT SYSTEM$WAIT(30, 'SECONDS')", timeoutSeconds=5)]),
        'lost_ack': dict(jobs=[dict(key='lost_ack', sql='SELECT 1')]),
    }
    runs = {}
    for name, case in cases.items():
        body = dict(requestId='live-pg-'+name+'-'+suffix, **case)
        if args.resume:
            with db.connect() as conn:
                previous_run = conn.execute('SELECT id FROM kumo_task_engine.runs WHERE request_id=%s', (body['requestId'],)).fetchone()
            if not previous_run:
                raise ValueError('Fixture is incomplete; resume will not admit new work')
            runs[name] = str(previous_run['id'])
        else:
            runs[name] = db.submit(body, 'LOCAL_DEV_LIFECYCLE_TEST')['runId']
        assert db.submit(body, 'LOCAL_DEV_LIFECYCLE_TEST')['reused']
    print(json.dumps(dict(started=runs)), flush=True)
    deadline = time.monotonic() + 600
    cancelled = db.read(runs['cancel'])['run']['cancel_requested']
    ack_states = [e['facts'].get('state') for e in db.read(runs['lost_ack'])['events'] if e['kind'] == 'ATTEMPT_STATE']
    restarted = args.resume and 'SUBMITTING' in ack_states and 'SUBMITTED' not in ack_states
    previous = None
    while time.monotonic() < deadline:
        worker.step()
        if tasks.lost_ack and not restarted:
            assert db.read(runs['lost_ack'])['jobs'][0]['state'] == 'SUBMITTING'
            worker = Worker(db, tasks)
            restarted = True
        details = {name: db.read(run) for name, run in runs.items()}
        if not cancelled and details['cancel']['jobs'][0]['state'] in ('SUBMITTED', 'RUNNING'):
            db.cancel(runs['cancel'])
            cancelled = True
        summary = {name: [(j['state'], j['cleanup_state']) for j in data['jobs']] for name, data in details.items()}
        if summary != previous:
            print(json.dumps(summary), flush=True)
            previous = summary
        assert not any(j['state'] == 'UNKNOWN' for data in details.values() for j in data['jobs']), 'Review unresolved task before continuing'
        if all(j['state'] in ('SUCCESS', 'FAILED', 'CANCELLED', 'SKIPPED') and j['cleanup_state'] in ('NONE', 'DONE') for data in details.values() for j in data['jobs']):
            break
        time.sleep(.5)
    else:
        raise TimeoutError('Native lifecycle tests did not drain in ten minutes; inspect recorded attempts')
    expected = dict(cancel=['CANCELLED'], parallel=['SUCCESS', 'SUCCESS'], success=['SUCCESS', 'SUCCESS'], failure=['FAILED', 'SKIPPED'], timeout=['FAILED'], lost_ack=['SUCCESS'])
    # Retain the native task timeout outcome and its error code/message.
    for name, states in expected.items():
        actual = [j['state'] for j in details[name]['jobs']]
        assert actual == states, (name, actual, states)
    assert restarted and cancelled
    assert all(value == 1 for value in tasks.executions.values())
    native = {}
    for data in details.values():
        for job in data['jobs']:
            assert tasks.inspect(job, missing_ok=True) is None, 'Temporary task must be absent'
            if job['submit_started_at']:
                record = tasks.history(job)
                assert record and record['attempt_number'] == 1, 'Require one native invocation with no native retry'
                native[str(job['id'])] = record
    parallel = [native[str(job['id'])] for job in details['parallel']['jobs']]
    parallel_overlap = max(row['query_start_time'] for row in parallel) < min(row['completed_time'] for row in parallel)
    assert parallel_overlap, 'Native query timestamps must show overlapping execution'
    start = datetime.now(timezone.utc)
    before = tasks.calls
    idle_steps = 0
    while (datetime.now(timezone.utc) - start).total_seconds() < args.idle_seconds:
        assert not worker.step(), 'Unexpected eligible work during idle proof'
        idle_steps += 1
        time.sleep(1)
    end = datetime.now(timezone.utc)
    assert tasks.calls == before, 'Idle worker contacted Snowflake'
    tagged = tasks.query("SELECT COUNT(*) AS COUNT FROM TABLE(KUMO_DEV.INFORMATION_SCHEMA.QUERY_HISTORY(END_TIME_RANGE_START=>TO_TIMESTAMP_LTZ(%s),END_TIME_RANGE_END=>TO_TIMESTAMP_LTZ(%s),RESULT_LIMIT=>10000)) WHERE QUERY_TAG='KUMO_DEV_POSTGRES_TASK_PROTOTYPE'", (str(start), str(end)))
    assert tagged[0]['count'] == 0, tagged
    report = dict(ok=True, environment='DEV', fixture=suffix,
                  cases={name: dict(runId=runs[name], state=data['run']['state'], jobs=[dict(state=j['state'], cleanup=j['cleanup_state'], task=j['task_fqn'], queryId=j['query_id'], nativeGroup=j['native_group_id'], error=j['error']) for j in data['jobs']]) for name, data in details.items()},
                  assertions=dict(idempotentAdmission=True, nativeExecutionOnce=True, lostAcknowledgmentWorkerRestart=True, parallelOverlap=parallel_overlap, terminalTasksAbsent=True),
                  idle=dict(start=start.isoformat(), end=end.isoformat(), postgresChecks=idle_steps, snowflakeAdapterCalls=0, taggedSnowflakeQueries=tagged[0]['count']))
    path = Path('review/dev-postgres-task-engine-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.json')
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report, indent=2)+'\n')
    print('PASS '+str(path), flush=True)


if __name__ == '__main__':
    main()
