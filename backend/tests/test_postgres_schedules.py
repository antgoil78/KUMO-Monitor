"""Calendar policy and real isolated PostgreSQL scheduling transactions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from task_engine.schedules import Scheduler, Schedules, calendar, next_fire
from task_engine.settings import Settings
from task_engine.store import Store, Conflict, WorkflowBusy

UTC = timezone.utc


def instant(text):
    return datetime.fromisoformat(text)


def definition(**values):
    return dict(dict(requestId=str(uuid.uuid4()), name='Trial schedule', cron='* * * * *', timezone='Europe/Stockholm',
                     enabled=True, graceSeconds=60, plan=dict(workflowKey='trial', jobs=[dict(key='one', sql='SELECT 1')])), **values)


class CalendarTests(unittest.TestCase):
    def test_weekday_calendar_uses_local_time_in_winter_and_summer(self):
        for before, expected in [('2027-01-03T12:00:00+00:00','2027-01-04T06:00:00+00:00'),
                                 ('2027-07-04T12:00:00+00:00','2027-07-05T05:00:00+00:00')]:
            self.assertEqual(next_fire('0 7 * * 1-5','Europe/Stockholm',instant(before)),instant(expected))

    def test_missing_spring_time_is_skipped(self):
        self.assertEqual(next_fire('30 2 * * *','Europe/Stockholm',instant('2027-03-27T23:00:00+00:00')),instant('2027-03-29T00:30:00+00:00'))

    def test_repeated_autumn_time_uses_first_occurrence_only(self):
        first=next_fire('30 2 * * *','Europe/Stockholm',instant('2027-10-30T23:00:00+00:00'))
        self.assertEqual(first,instant('2027-10-31T00:30:00+00:00'))
        for after in [first,instant('2027-10-31T01:00:00+00:00'),instant('2027-10-31T01:45:00+00:00')]:
            self.assertEqual(next_fire('30 2 * * *','Europe/Stockholm',after),instant('2027-11-01T01:30:00+00:00'))

    def test_minutely_calendar_does_not_repeat_the_fallback_hour(self):
        self.assertEqual(next_fire('* * * * *','Europe/Stockholm',instant('2027-10-31T00:59:00+00:00')),instant('2027-10-31T02:00:00+00:00'))
        self.assertEqual(next_fire('* * * * *','Europe/Stockholm',instant('2027-10-31T01:05:00+00:00')),instant('2027-10-31T02:00:00+00:00'))

    def test_whole_skipped_day_and_fractional_timezone(self):
        self.assertEqual(next_fire('0 12 * * *','Pacific/Apia',instant('2011-12-30T00:00:00+00:00')),instant('2011-12-30T22:00:00+00:00'))
        self.assertEqual(next_fire('0 7 * * *','Asia/Kathmandu',instant('2027-01-01T00:00:00+00:00')),instant('2027-01-01T01:15:00+00:00'))

    def test_cron_lists_steps_day_or_and_strict_next_instant(self):
        self.assertEqual(next_fire('*/15 7-9 * * 1,3,5','UTC',instant('2027-01-04T07:15:00+00:00')),instant('2027-01-04T07:30:00+00:00'))
        self.assertEqual(next_fire('0 0 15 * 1','UTC',instant('2027-01-04T00:00:00+00:00')),instant('2027-01-11T00:00:00+00:00'))

    def test_invalid_or_unsupported_calendars_and_naive_timestamps_are_rejected(self):
        for cron in ['@hourly','* * * * * *','99 * * * *','0 0 * * MON','0 0 L * *','0 0 * * *; DROP']:
            with self.subTest(cron=cron),self.assertRaises(ValueError):calendar(cron,'UTC')
        with self.assertRaises(ValueError):calendar('* * * * *','Missing/Zone')
        with self.assertRaises(ValueError):next_fire('0 0 30 2 *','UTC',instant('2027-01-01T00:00:00+00:00'))
        with self.assertRaises(ValueError):next_fire('* * * * *','UTC',datetime(2027,1,1))


@unittest.skipUnless(os.getenv('KUMO_TASK_ENGINE_TEST_DSN'),'Requires isolated PostgreSQL test database')
class PostgresScheduleTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(Settings(os.environ['KUMO_TASK_ENGINE_TEST_DSN']))
        with self.store.connect() as conn:
            assert conn.execute('SELECT current_database() AS name').fetchone()['name'].endswith('_test')
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute('TRUNCATE kumo_task_engine.schedules,kumo_task_engine.scheduler_workers,kumo_task_engine.events,kumo_task_engine.attempts,kumo_task_engine.runs RESTART IDENTITY CASCADE')
        self.schedules=Schedules(self.store);self.worker=Scheduler(self.store)

    def create(self,**values):
        return self.schedules.create(definition(**values),'TRUSTED')['schedule']

    def due(self,row,seconds=1):
        with self.store.connect() as conn:
            due=conn.execute("UPDATE kumo_task_engine.schedules SET next_fire_at=clock_timestamp()-(%s*interval '1 second') WHERE id=%s RETURNING next_fire_at",(seconds,row['id'])).fetchone()['next_fire_at']
        return due

    def rows(self,table):
        assert table in ('schedules','schedule_occurrences','runs','attempts','schedule_events','events')
        with self.store.connect() as conn:return conn.execute('SELECT * FROM kumo_task_engine.'+table).fetchall()

    def test_create_paused_and_idempotent_then_enable_uses_future_time(self):
        body=definition(enabled=False);row=self.schedules.create(body,'TRUSTED')['schedule']
        self.assertIsNone(row['next_fire_at']);self.assertFalse(self.worker.step())
        self.assertTrue(self.schedules.create(body,'OTHER')['reused'])
        self.assertEqual(row['created_by'],'TRUSTED')
        enabled=self.schedules.update(row['id'],dict(expectedRevision=1,enabled=True),'EDITOR')['schedule']
        self.assertGreater(enabled['next_fire_at'],enabled['updated_at'])
        self.assertEqual(enabled['revision'],2)
        self.assertEqual(len(self.rows('schedule_events')),2)
        self.assertTrue(self.schedules.create(body,'OTHER')['reused'])

    def test_changed_creation_request_conflicts_and_sql_restrictions_apply(self):
        body=definition();self.schedules.create(body,'T')
        body['cron']='0 * * * *'
        with self.assertRaises(Conflict):self.schedules.create(body,'T')
        for values in [dict(plan=dict(workflowKey='x',jobs=[dict(sql='SELECT * FROM SECRET')])),dict(graceSeconds=True),dict(enabled='true')]:
            with self.subTest(values=values),self.assertRaises(ValueError):self.create(**values)
        self.assertEqual(len(self.rows('schedules')),1)

    def test_atomic_occurrence_admission_and_future_advance(self):
        row=self.create();due=self.due(row);self.assertTrue(self.worker.step())
        occurrence=self.rows('schedule_occurrences')[0];run=self.rows('runs')[0]
        self.assertEqual(occurrence['scheduled_for'],due);self.assertEqual(occurrence['run_id'],run['id'])
        self.assertEqual(occurrence['state'],'ADMITTED');self.assertEqual(run['state'],'QUEUED')
        self.assertEqual(run['workflow_key'],'trial')
        self.assertEqual(self.rows('attempts')[0]['snapshot']['sql'],'SELECT 1')
        self.assertGreater(self.rows('schedules')[0]['next_fire_at'],due)
        self.assertEqual([e['kind'] for e in self.rows('events')],['ADMITTED','SCHEDULED'])

    def test_admission_failure_rolls_back_occurrence_and_due_time(self):
        row=self.create();due=self.due(row)
        original=self.store.submit_in_transaction
        def fail(*args):
            original(*args);raise RuntimeError('Injected failure after admission SQL')
        with patch.object(self.store,'submit_in_transaction',side_effect=fail),self.assertRaises(RuntimeError):self.worker.step()
        self.assertEqual(self.rows('schedules')[0]['next_fire_at'],due)
        self.assertFalse(self.rows('schedule_occurrences'));self.assertFalse(self.rows('runs'));self.assertFalse(self.rows('attempts'))
        self.assertTrue(Scheduler(self.store).step());self.assertEqual(len(self.rows('runs')),1)

    def test_restart_after_commit_does_not_admit_same_occurrence_twice(self):
        row=self.create();self.due(row);self.worker.step()
        self.assertFalse(Scheduler(self.store).step())
        self.assertEqual(len(self.rows('runs')),1);self.assertEqual(len(self.rows('schedule_occurrences')),1)

    def test_competing_workers_admit_one_run(self):
        row=self.create();self.due(row)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:Scheduler(self.store).step(),range(4)))
        self.assertEqual(sum(results),1);self.assertEqual(len(self.rows('runs')),1)
        self.assertEqual(len(self.rows('schedule_occurrences')),1)

    def test_concurrent_manual_and_scheduled_admissions_share_overlap_gate(self):
        row=self.create();self.due(row)
        def manual():
            try:return self.store.submit(dict(requestId='manual',workflowKey='trial',jobs=[dict(sql='SELECT 1')]),'USER')
            except WorkflowBusy:return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(manual);b=pool.submit(self.worker.step);a.result();b.result()
        self.assertEqual(len(self.rows('runs')),1)
        self.assertIn(self.rows('schedule_occurrences')[0]['state'],('ADMITTED','SKIPPED_OVERLAP'))

    def test_unknown_or_queued_work_blocks_future_occurrences(self):
        row=self.create();self.due(row);self.worker.step()
        for state in ['QUEUED','UNKNOWN']:
            with self.store.connect() as conn:conn.execute('UPDATE kumo_task_engine.runs SET state=%s',(state,))
            self.due(row);self.worker.step()
            self.assertEqual(len(self.rows('runs')),1)
        self.assertEqual([o['state'] for o in sorted(self.rows('schedule_occurrences'),key=lambda o:o['observed_at'])],['ADMITTED','SKIPPED_OVERLAP','SKIPPED_OVERLAP'])

    def test_overlapping_schedules_for_same_workflow_skip_second(self):
        first=self.create();second=self.create(name='Second')
        self.due(first);self.due(second);self.worker.step();self.worker.step()
        self.assertEqual(len(self.rows('runs')),1)
        self.assertEqual({o['state'] for o in self.rows('schedule_occurrences')},{'ADMITTED','SKIPPED_OVERLAP'})

    def test_terminal_outcome_releases_overlap_gate_and_request_retry_reuses_run(self):
        row=self.create();self.due(row);self.worker.step();run=self.rows('runs')[0]
        request=dict(requestId=run['request_id'],workflowKey='trial',jobs=[dict(key='one',sql='SELECT 1')])
        self.assertTrue(self.store.submit(request,'OTHER')['reused'])
        self.store.cancel(run['id']);self.due(row);self.worker.step()
        self.assertEqual(len(self.rows('runs')),2)

    def test_pause_does_not_cancel_admitted_run_and_resume_discards_paused_window(self):
        row=self.create();self.due(row);self.worker.step();run=self.rows('runs')[0]
        paused=self.schedules.update(row['id'],dict(expectedRevision=1,enabled=False),'U')['schedule']
        self.assertIsNone(paused['next_fire_at']);self.assertFalse(self.worker.step())
        self.assertFalse(self.store.read(run['id'])['run']['cancel_requested'])
        resumed=self.schedules.update(row['id'],dict(expectedRevision=2,enabled=True),'U')['schedule']
        self.assertGreater(resumed['next_fire_at'],resumed['updated_at'])
        self.assertEqual(len(self.rows('schedule_occurrences')),1)

    def test_stale_edit_is_rejected_and_admitted_plan_stays_frozen(self):
        row=self.create();self.due(row);self.worker.step()
        self.schedules.update(row['id'],dict(expectedRevision=1,cron='0 7 * * 1-5',timezone='UTC'),'EDITOR')
        with self.assertRaises(Conflict):self.schedules.update(row['id'],dict(expectedRevision=1,enabled=False),'OTHER')
        with self.assertRaises(ValueError):self.schedules.update(row['id'],dict(expectedRevision=2,plan={}), 'OTHER')
        self.assertEqual(self.rows('schedule_occurrences')[0]['snapshot']['cron'],'* * * * *')

    def test_overnight_backlog_is_one_missed_window_and_at_most_one_admission(self):
        row=self.create(graceSeconds=3600);original=self.due(row,86400)
        self.worker.step();occurrences=self.rows('schedule_occurrences')
        self.assertEqual(len(occurrences),2)
        missed=next(o for o in occurrences if o['state']=='MISSED')
        self.assertEqual(missed['scheduled_for'],original);self.assertIsNotNone(missed['window_end'])
        self.assertEqual(len(self.rows('runs')),1)
        self.assertFalse(self.worker.step())

    def test_expired_daily_occurrence_is_missed_without_admission(self):
        row=self.create(cron='0 0 1 1 *',timezone='UTC',graceSeconds=0)
        self.due(row,120);self.worker.step()
        self.assertFalse(self.rows('runs'))
        self.assertEqual(self.rows('schedule_occurrences')[0]['state'],'MISSED')

    def test_repeated_occurrence_after_recovery_never_replays_even_after_revision_change(self):
        row=self.create();due=self.due(row);self.worker.step()
        with self.store.connect() as conn:
            conn.execute('UPDATE kumo_task_engine.schedules SET next_fire_at=%s,revision=revision+1 WHERE id=%s',(due,row['id']))
        self.worker.step()
        self.assertEqual(len(self.rows('runs')),1);self.assertEqual(len(self.rows('schedule_occurrences')),1)

    def test_invalid_persisted_calendar_stops_schedule_without_partial_occurrence(self):
        row=self.create();self.due(row)
        with self.store.connect() as conn:conn.execute("UPDATE kumo_task_engine.schedules SET cron='bad'")
        self.worker.step()
        self.assertEqual(self.schedules.status()['schedules'][0]['state'],'INVALID')
        self.assertFalse(self.rows('runs'));self.assertFalse(self.rows('schedule_occurrences'))
        self.assertFalse(self.worker.step())

    def test_heartbeat_health_stales_and_disabled_execution_keeps_due_time(self):
        row=self.create();due=self.due(row)
        self.assertEqual(self.schedules.status()['scheduler']['state'],'NOT_STARTED')
        self.assertFalse(self.worker.step(execution_enabled=False))
        self.assertEqual(self.schedules.status()['scheduler']['state'],'EXECUTION_DISABLED')
        self.assertEqual(self.rows('schedules')[0]['next_fire_at'],due)
        self.worker.step()
        self.assertEqual(self.schedules.status()['scheduler']['state'],'HEALTHY')
        for age,state in [(15,'DELAYED'),(45,'UNAVAILABLE')]:
            with self.store.connect() as conn:conn.execute("UPDATE kumo_task_engine.scheduler_workers SET last_seen=clock_timestamp()-(%s*interval '1 second')",(age,))
            self.assertEqual(self.schedules.status()['scheduler']['state'],state)

    def test_idle_or_future_schedule_never_connects_to_snowflake(self):
        self.create(cron='0 0 1 1 *')
        import snowflake_client
        with patch.object(snowflake_client,'connection',side_effect=AssertionError('No Snowflake')):
            for _ in range(3):self.assertFalse(self.worker.step())
            self.assertEqual(self.schedules.status()['scheduler']['state'],'HEALTHY')
        self.assertFalse(self.rows('runs'))

    def test_migration_from_v1_preserves_existing_run_and_is_repeatable(self):
        run=uuid.uuid4()
        with self.store.connect() as conn:
            conn.execute('DROP SCHEMA kumo_task_engine CASCADE')  # Isolated _test database asserted in setUp.
            conn.execute(Path(__file__).parents[1].joinpath('task_engine/schema.sql').read_text())
            conn.execute("INSERT INTO kumo_task_engine.runs(id,request_id,definition,definition_hash,requested_by,state) VALUES(%s,'v1-history','{}','old','USER','SUCCESS')",(run,))
        self.store.initialize();self.store.initialize()
        self.assertEqual(self.store.read(run)['run']['request_id'],'v1-history')
        with self.store.connect() as conn:
            self.assertEqual([r['version'] for r in conn.execute('SELECT version FROM kumo_task_engine.schema_version ORDER BY version').fetchall()],[1,2])


if __name__=='__main__':unittest.main()
