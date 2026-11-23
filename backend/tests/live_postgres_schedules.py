"""Explicit local/DEV cron admission, native execution and idle proof.

Load the local environment before --run. Stop other prototype workers while
this harness drives its own worker. Both created schedules finish paused.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from task_engine.settings import Settings
from task_engine.store import Store
from task_engine.schedules import Scheduler, Schedules
from task_engine.snowflake_tasks import SnowflakeTasks
from task_engine.worker import Worker


class Tasks(SnowflakeTasks):
    def __init__(self, settings):
        super().__init__(settings)
        self.executions = 0

    def execute(self, job):
        self.executions += 1
        return super().execute(job)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', required=True)
    args = parser.parse_args()
    db = Store(Settings.from_environment())
    db.initialize()
    schedules = Schedules(db)
    scheduler = Scheduler(db)
    tasks = Tasks(db.settings)
    worker = Worker(db, tasks)
    suffix = uuid.uuid4().hex[:10]
    fixtures = []
    try:
        body = dict(requestId='live-pg-schedule-'+suffix, name='Cron boundary test '+suffix, cron='* * * * *',
                    timezone='Europe/Stockholm', enabled=True, graceSeconds=60,
                    plan=dict(workflowKey='live-schedule-'+suffix, jobs=[dict(key='scheduled', sql='SELECT 1')]))
        row = schedules.create(body, 'LOCAL_DEV_SCHEDULE_TEST')['schedule']
        fixtures.append(row['id'])
        assert schedules.create(body, 'LOCAL_DEV_SCHEDULE_TEST')['reused']
        print('Waiting for real cron boundary '+row['next_fire_at'].isoformat(), flush=True)
        due = row['next_fire_at']
        admitted = None
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            scheduler.step()
            current = schedules.status()
            found = next((o for o in current['occurrences'] if o['schedule_id'] == row['id'] and o['state'] == 'ADMITTED'), None)
            if found and admitted is None:
                admitted = found
                schedules.update(row['id'], dict(expectedRevision=1, enabled=False), 'LOCAL_DEV_SCHEDULE_TEST')
                scheduler = Scheduler(db)  # Replace the process identity after admission committed.
                print('Admitted once; schedule paused; scheduler replaced', flush=True)
            worker.step()
            if admitted:
                detail = db.read(admitted['run_id'])
                job = detail['jobs'][0]
                if job['state'] == 'UNKNOWN':
                    raise AssertionError('Native execution needs review: '+str(job['error']))
                if job['cleanup_state'] == 'DONE':
                    assert job['state'] == 'SUCCESS'
                    break
            time.sleep(.5)
        else:
            raise TimeoutError('Scheduled native run did not finish within three minutes')
        assert admitted['scheduled_for'] == due
        assert tasks.executions == 1
        assert tasks.inspect(job, missing_ok=True) is None
        native = tasks.history(job)
        assert native['attempt_number'] == 1
        future = datetime.now(timezone.utc) + timedelta(minutes=10)
        future_body = dict(body, requestId='live-pg-future-'+suffix, name='Future idle test '+suffix,
                           cron=f'{future.minute} {future.hour} * * *', timezone='UTC')
        future_row = schedules.create(future_body, 'LOCAL_DEV_SCHEDULE_TEST')['schedule']
        fixtures.append(future_row['id'])
        start = datetime.now(timezone.utc)
        before = tasks.calls
        checks = 0
        print('Beginning 90-second idle proof with an enabled future schedule', flush=True)
        while (datetime.now(timezone.utc)-start).total_seconds() < 90:
            assert not scheduler.step()
            assert not worker.step()
            checks += 1
            time.sleep(1)
        end = datetime.now(timezone.utc)
        assert tasks.calls == before
        snapshot = schedules.status()
        assert snapshot['scheduler']['state'] == 'HEALTHY'
        assert next(s for s in snapshot['schedules'] if s['id'] == future_row['id'])['next_fire_at'] == future_row['next_fire_at']
        tagged = tasks.query("SELECT COUNT(*) AS COUNT FROM TABLE(KUMO_DEV.INFORMATION_SCHEMA.QUERY_HISTORY(END_TIME_RANGE_START=>TO_TIMESTAMP_LTZ(%s),END_TIME_RANGE_END=>TO_TIMESTAMP_LTZ(%s),RESULT_LIMIT=>10000)) WHERE QUERY_TAG='KUMO_DEV_POSTGRES_TASK_PROTOTYPE'", (str(start), str(end)))
        assert tagged[0]['count'] == 0
        occurrences = [o for o in snapshot['occurrences'] if o['schedule_id'] == row['id']]
        assert len(occurrences) == 1
        report = dict(ok=True, environment='DEV', fixture=suffix, scheduleId=str(row['id']), runId=str(admitted['run_id']),
                      plannedAt=due.isoformat(), admittedAt=admitted['observed_at'].isoformat(),
                      native=dict(task=job['task_fqn'], queryId=job['query_id'], group=job['native_group_id'], outcome=job['state'], cleanup=job['cleanup_state'], submissions=tasks.executions),
                      assertions=dict(realCronBoundary=True, durableIdempotentCreation=True, schedulerReplacedAfterAdmission=True, singleOccurrence=True, nativeTaskAbsent=True),
                      idle=dict(start=start.isoformat(), end=end.isoformat(), combinedSchedulerWorkerChecks=checks, enabledFutureSchedule=True, schedulerHealth=snapshot['scheduler']['state'], snowflakeAdapterCalls=0, taggedSnowflakeQueries=tagged[0]['count']))
    finally:
        for schedule_id in fixtures:
            with db.connect() as conn:
                saved = conn.execute('SELECT revision,enabled FROM kumo_task_engine.schedules WHERE id=%s', (schedule_id,)).fetchone()
            if saved and saved['enabled']:
                schedules.update(schedule_id, dict(expectedRevision=saved['revision'], enabled=False), 'LOCAL_DEV_SCHEDULE_TEST_CLEANUP')
    report['fixturesPaused'] = True
    path = Path('review/dev-postgres-schedules-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.json')
    path.write_text(json.dumps(report, indent=2)+'\n')
    print('PASS '+str(path), flush=True)


if __name__ == '__main__':
    main()
