"""Verify migration atomicity, dormant scheduling and preservation on real PG."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from migrations.migrate_dev_postgres_schedules import migrate
from task_engine.catalog import CatalogConfiguration, import_catalog
from task_engine.schedules import Scheduler, Schedules
from task_engine.settings import Settings
from task_engine.store import Store, Conflict
from test_postgres_dependencies import source


@unittest.skipUnless(os.getenv('KUMO_TASK_ENGINE_TEST_DSN'),'Requires isolated PostgreSQL test database')
class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(Settings(os.environ['KUMO_TASK_ENGINE_TEST_DSN']))
        with self.store.connect() as conn:
            assert conn.execute('SELECT current_database() AS name').fetchone()['name'].endswith('_test')
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute('TRUNCATE kumo_task_engine.dependency_decisions,kumo_task_engine.dependency_graphs,kumo_task_engine.schedules,kumo_task_engine.scheduler_workers,kumo_task_engine.runs RESTART IDENTITY CASCADE')
        self.raw=source();self.raw['raw_records']=self.raw.pop('rules')
        for index,w in enumerate(self.raw['workflows']):
            w.update(SCHEDULE='0 7 * * 1-5' if index != 2 else None,SCHEDULE_TIMEZONE='UTC',IS_SCHEDULE_ENABLED=False)
        self.raw['workflows'][1]['IS_ENABLED']=False
        self.graph=import_catalog(self.store,self.raw,'FIXTURE')

    def rows(self,table):
        with self.store.connect() as conn:return conn.execute('SELECT * FROM kumo_task_engine.'+table).fetchall()

    def test_preview_and_apply_preserve_catalog_history_and_do_not_admit_runs(self):
        self.store.submit(dict(requestId='existing-history',jobs=[dict(sql='SELECT 1')]),'USER')
        runs=self.rows('runs');attempts=self.rows('attempts')
        self.assertEqual(len(migrate(self.store)['planned']),3)
        self.assertEqual(self.rows('schedules'),[])
        result=migrate(self.store,apply=True,expected_revision=1)
        self.assertEqual(len(result['created']),3)
        self.assertTrue(all(not s['enabled'] and s['next_fire_at'] is None for s in self.rows('schedules')))
        self.assertFalse(Scheduler(self.store).step())
        self.assertEqual(self.rows('runs'),runs)
        self.assertEqual(self.rows('attempts'),attempts)
        self.assertEqual(CatalogConfiguration(self.store).read()['revision'],1)
        self.assertEqual(len(self.rows('graph_revisions')),1)
        self.assertEqual(len(self.rows('schedule_events')),3)
        self.assertEqual(sum(r['decision']=='SCHEDULE_MIGRATED' for r in self.rows('dependency_decisions')),3)

    def test_rerun_preserves_user_schedule_edits_and_enabled_state(self):
        migrate(self.store,apply=True,expected_revision=1)
        row=next(s for s in self.rows('schedules') if s['plan']['workflowId']==self.raw['workflows'][0]['WORKFLOW_ID'])
        Schedules(self.store).update(row['id'],dict(expectedRevision=1,cron='15 9 * * 1-5',enabled=True),'USER')
        before=self.rows('schedules')
        self.assertEqual(migrate(self.store,apply=True,expected_revision=1)['created'],[])
        self.assertEqual(self.rows('schedules'),before)
        self.assertEqual(len(self.rows('schedule_events')),4)

    def test_stale_or_missing_revision_cannot_write(self):
        with self.assertRaises(Conflict):migrate(self.store,apply=True,expected_revision=2)
        with self.assertRaises(ValueError):migrate(self.store,apply=True)
        self.assertEqual(self.rows('schedules'),[])

    def test_invalid_later_schedule_rolls_back_the_whole_migration(self):
        config=CatalogConfiguration(self.store)
        config.change(dict(revision=1),'FIXTURE',lambda raw:raw['workflows'][-1].update(SCHEDULE='99 * * * *'))
        with self.assertRaises(ValueError):migrate(self.store,apply=True,expected_revision=2)
        self.assertEqual(self.rows('schedules'),[])
        self.assertEqual(self.rows('schedule_events'),[])

    def test_disabled_workflow_is_retained_but_cannot_be_activated(self):
        migrate(self.store,apply=True,expected_revision=1)
        row=next(s for s in self.rows('schedules') if s['plan']['workflowId']==self.raw['workflows'][1]['WORKFLOW_ID'])
        with self.assertRaisesRegex(ValueError,'disabled'):
            Schedules(self.store).update(row['id'],dict(expectedRevision=1,enabled=True),'USER')
        self.assertTrue(all(not s['enabled'] for s in self.rows('schedules')))

    def test_competing_migrations_create_each_schedule_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:migrate(self.store,apply=True,expected_revision=1),range(2)))
        self.assertEqual(sorted(len(r['created']) for r in results),[0,3])
        self.assertEqual(len(self.rows('schedules')),3)
        self.assertEqual(len(self.rows('schedule_events')),3)
