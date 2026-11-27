"""DBT preparation and mixed execution on isolated PostgreSQL; no Snowflake calls."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
import unittest
import uuid
from unittest.mock import patch

from flask import Flask
import test_postgres_monitor as fixtures
from test_postgres_task_engine import FakeTasks
from task_engine.catalog import master
from task_engine.dependencies import DependencyWorker
from task_engine.integration import register
from task_engine.store import WorkflowBusy
from task_engine.worker import Worker


class NativeTasks(FakeTasks):
    def history(self, job):
        report = super().history(job)
        if report and report.get('query_id'):
            report['query_start_time'] = job['submit_started_at'] or datetime.now(timezone.utc)
        return report


@unittest.skipUnless(os.getenv('KUMO_TASK_ENGINE_TEST_DSN'), 'Requires isolated PostgreSQL test database')
class DBTPreparationTests(unittest.TestCase):
    setUp = fixtures.PostgresMonitorTests.setUp
    start = fixtures.PostgresMonitorTests.start

    def cli(self, *args):
        app = Flask(__name__)
        register(app, lambda: dict(userName='LOCAL_TEST'))
        with patch('task_engine.integration.store', return_value=self.store), patch('snowflake_client.query_service', side_effect=AssertionError('No Snowflake reads')):
            return app.test_cli_runner().invoke(args=['task-engine', 'recompile-catalog', *args])

    def counts(self):
        with self.store.connect() as conn:
            return {name: conn.execute('SELECT count(*) AS n FROM kumo_task_engine.' + name).fetchone()['n']
                    for name in ['cascades', 'runs', 'attempts', 'schedules', 'graph_revisions']}

    @patch.dict(os.environ, KUMO_PG_BUSINESS_JOBS_ENABLED='true')
    def test_preview_is_readonly_and_apply_retains_master_schedules_and_accepted_work(self):
        old = self.start(); frozen = deepcopy(self.graphs.read(old)['cascade']['snapshot'])
        raw = deepcopy(master(self.store)['definition']['catalog'])
        before = self.counts()
        preview = self.cli()
        self.assertEqual(preview.exit_code, 0, preview.output)
        data = json.loads(preview.output)
        self.assertFalse(data['applied']); self.assertEqual(data['revision'], 1)
        self.assertEqual(data['executableDBTWorkflows'], ['DBT workflow'])
        self.assertEqual(self.counts(), before)
        applied = self.cli('--apply', '--expected-revision', '1')
        self.assertEqual(applied.exit_code, 0, applied.output)
        self.assertEqual(json.loads(applied.output)['revision'], 2)
        self.assertEqual(master(self.store)['definition']['catalog'], raw)
        self.assertEqual(self.graphs.read(old)['cascade']['snapshot'], frozen)
        self.assertEqual({k: v for k, v in self.counts().items() if k != 'graph_revisions'},
                         {k: v for k, v in before.items() if k != 'graph_revisions'})
        self.assertEqual(self.cli('--apply', '--expected-revision', '2').exit_code, 0)
        self.assertEqual(master(self.store)['revision'], 2)

    @patch.dict(os.environ, KUMO_PG_BUSINESS_JOBS_ENABLED='true')
    def test_apply_requires_a_reviewed_current_revision(self):
        before = self.counts()
        missing = self.cli('--apply')
        self.assertNotEqual(missing.exit_code, 0); self.assertIn('Preview first', missing.output)
        stale = self.cli('--apply', '--expected-revision', '0')
        self.assertNotEqual(stale.exit_code, 0); self.assertIn('Preview again', stale.output)
        self.assertEqual(self.counts(), before)
        self.assertEqual(master(self.store)['revision'], 1)

    def prepare_mixed(self):
        self.dbt_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        def mutate(raw):
            root = self.ids['WF1']
            first = next(r['JOB_ID'] for r in raw['raw_records'] if r['WORKFLOW_ID'] == root and r.get('JOB_ID'))
            for name, identity in zip(['SDL build', 'EDV build'], self.dbt_ids):
                raw['jobs'].append(dict(JOB_ID=identity, JOB_NAME=name, JOB_TYPE='DBT', IS_ENABLED=True,
                    DBT_COMMAND='dbt build --select ' + name.split()[0], DBT_PROJECT_FQN='[DATABASE].[DBT_SCHEMA].[DBT_PROJECT]', DBT_TARGET='[DBT_TARGET]'))
                raw['raw_records'].append(dict(WORKFLOW_ID=root, JOB_ID=identity, PARENT_ID=first, TRIGGER_TYPE='DEP', ALLOW=True, SUCCESS=True, WARNING=False, ERROR=False))
                first = identity
            for name in ['WF2', 'WF3']:
                identity = next(r['JOB_ID'] for r in raw['raw_records'] if r['WORKFLOW_ID'] == self.ids[name] and r.get('JOB_ID'))
                job = next(j for j in raw['jobs'] if j['JOB_ID'] == identity)
                job.update(JOB_TYPE='DBT', DBT_COMMAND='build --select ' + name)
        self.config.change(dict(revision=1), 'DBT_TEST', mutate)
        self.graph = master(self.store)
        self.tasks = NativeTasks(self.store)
        self.worker = Worker(self.store, self.tasks)
        self.dispatcher = DependencyWorker(self.store)

    def until(self, predicate):
        for _ in range(40):
            if predicate(): return
            with self.store.connect() as conn:
                conn.execute('UPDATE kumo_task_engine.attempts SET next_check_at=clock_timestamp() WHERE next_check_at IS NOT NULL')
            self.worker.step(); self.dispatcher.step()
        self.fail('DBT test did not reach the expected durable state')

    def outcome(self, attempt, state='SUCCEEDED'):
        self.tasks.native[attempt]['state'] = state

    def rows(self):
        return {row['WORKFLOW_NAME']: row for row in self.monitor.payload()['rows']}

    @patch.dict(os.environ, KUMO_PG_BUSINESS_JOBS_ENABLED='true')
    def test_sql_then_dbt_jobs_and_parallel_dbt_parents_release_join_only_after_both_finish(self):
        self.prepare_mixed(); group = self.start()
        self.until(lambda: len(self.tasks.executions) == 1)
        node = next(n for n in self.graphs.read(group)['nodes'] if n['node_key'] == self.graph['definition']['workflowKeys'][self.ids['WF1']])
        frozen = deepcopy(self.store.read(node['run_id'])['jobs'])
        self.assertEqual([j['snapshot']['type'] for j in frozen], ['SQL', 'DBT', 'DBT'])
        self.config.job(self.dbt_ids[0], dict(revision=self.graph['revision'], dbtCommand='dbt compile --select changed'), 'EDITOR')
        self.assertEqual(self.store.read(node['run_id'])['jobs'][1]['snapshot'], frozen[1]['snapshot'])
        for count in [2, 3]:
            self.outcome(self.tasks.executions[-1]); self.until(lambda: len(self.tasks.executions) == count)
        self.assertEqual(self.rows()['WF1']['planning']['status'], 'RUNNING')
        self.outcome(self.tasks.executions[-1]); self.until(lambda: len(self.tasks.executions) == 5)
        self.assertEqual(self.rows()['WF1']['lastResult']['status'], 'SUCCESS')
        self.assertEqual(self.rows()['WF4']['planning']['status'], 'WAITING')
        with self.assertRaises(WorkflowBusy): self.start('WF4', include_children=False)
        self.outcome(self.tasks.executions[3])
        # Observe one DBT parent to completion, but keep the other one active.
        self.until(lambda: sum(self.rows()[name]['lastResult']['status'] == 'SUCCESS' for name in ['WF2', 'WF3']) == 1)
        self.assertEqual(len(self.tasks.executions), 5)
        self.assertEqual(self.rows()['WF4']['planning']['status'], 'WAITING')
        self.outcome(self.tasks.executions[4]); self.until(lambda: len(self.tasks.executions) == 6)
        self.outcome(self.tasks.executions[-1]); self.until(lambda: len(self.tasks.drops) == 6)
        self.assertEqual(self.rows()['WF4']['lastResult']['status'], 'SUCCESS')
        polls = list(self.tasks.polls)
        for _ in range(5): self.assertFalse(self.worker.step())
        self.assertEqual(self.tasks.polls, polls)

    @patch.dict(os.environ, KUMO_PG_BUSINESS_JOBS_ENABLED='true')
    def test_failed_dbt_job_skips_later_jobs_and_does_not_start_downstream_workflows(self):
        self.prepare_mixed(); group = self.start()
        self.until(lambda: len(self.tasks.executions) == 1)
        self.outcome(self.tasks.executions[0]); self.until(lambda: len(self.tasks.executions) == 2)
        self.outcome(self.tasks.executions[1], 'FAILED')
        self.until(lambda: self.graphs.read(group)['cascade']['state'] == 'FAILED' and len(self.tasks.drops) == 2)
        node = next(n for n in self.graphs.read(group)['nodes'] if n['node_key'] == self.graph['definition']['workflowKeys'][self.ids['WF1']])
        self.assertEqual([job['state'] for job in self.store.read(node['run_id'])['jobs']], ['SUCCESS', 'FAILED', 'SKIPPED'])
        self.assertEqual(len(self.tasks.executions), 2)
        row = self.rows()['WF1']
        self.assertEqual(row['lastResult']['status'], 'FAILED')
        self.assertEqual(row['jobs'][1]['lastResult']['status'], 'FAILED')
        self.assertTrue(all(n['run_id'] is None for n in self.graphs.read(group)['nodes'] if n['id'] != node['id']))
