"""Monitor occurrences follow plan lifetime, without changing execution policy."""
import os
import unittest
from unittest.mock import patch

import test_postgres_monitor as fixtures
from task_engine.dependencies import DependencyWorker


@unittest.skipUnless(os.getenv('KUMO_TASK_ENGINE_TEST_DSN'), 'Requires isolated PostgreSQL test database')
class PlanRequestTests(unittest.TestCase):
    setUp = fixtures.PostgresMonitorTests.setUp
    start = fixtures.PostgresMonitorTests.start
    complete = fixtures.PostgresMonitorTests.complete
    finish = fixtures.PostgresMonitorTests.finish

    def rows(self):
        with patch('snowflake_client.query_service', side_effect=AssertionError('Monitor must read PostgreSQL only')):
            return {row['WORKFLOW_NAME']: row for row in self.monitor.payload()['rows']}

    def test_completed_workflow_and_jobs_remain_until_the_whole_plan_completes(self):
        group = self.start(); worker = DependencyWorker(self.store)
        worker.step(); self.complete(group, 'WF1'); worker.step()
        rows = self.rows()
        root = rows['WF1']['requests'][0]
        self.assertEqual(root['status'], 'SUCCESS')
        self.assertEqual(root['planStatus'], 'RUNNING')
        self.assertEqual(root['jobs'][0]['status'], 'SUCCESS')
        self.assertEqual(root['trigger']['kind'], 'MANUAL')
        self.assertEqual(rows['WF2']['requests'][0]['trigger']['kind'], 'PARENT')
        self.assertEqual(rows['WF2']['requests'][0]['trigger']['parents'], [dict(name='WF1', status='SUCCESS')])
        self.complete(group, 'WF2'); self.complete(group, 'WF3'); worker.step()
        self.complete(group, 'WF4'); worker.step()
        rows = self.rows()
        self.assertTrue(all(not row['requests'] for row in rows.values()))
        self.assertEqual(rows['WF1']['lastResult']['status'], 'SUCCESS')
        self.assertEqual(rows['WF4']['lastResult']['status'], 'SUCCESS')
        self.assertEqual(rows['WF1']['lastResult']['triggerSource'], 'MANUAL')
        self.assertEqual(rows['WF1']['jobs'][0]['lastResult']['triggerSource'], 'MANUAL')
        self.assertEqual(rows['WF4']['lastResult']['triggerSource'], 'PARENT')
        self.assertEqual(rows['WF4']['jobs'][0]['lastResult']['triggerSource'], 'PARENT')

    def test_trigger_configuration_is_visible_without_a_plan_and_independent_of_manual_entry(self):
        row = self.rows()['WF2']
        self.assertEqual([parent['name'] for parent in row['configuredParents']], ['WF1'])
        self.assertEqual(row['requests'], [])
        group = self.start('WF2', include_children=False)
        worker = DependencyWorker(self.store); worker.step()
        self.complete(group, 'WF2'); worker.step()
        row = self.rows()['WF2']
        self.assertEqual(row['lastResult']['triggerSource'], 'MANUAL')
        self.assertEqual([parent['name'] for parent in row['configuredParents']], ['WF1'])
        self.assertEqual(row['requests'], [])

    def test_new_manual_request_does_not_relabel_completed_scheduled_execution(self):
        with self.store.transaction() as conn:
            group = self.graphs.start_in_transaction(conn,self.graph['id'],
                dict(requestId='scheduled-entry',workflowId=self.ids['WF1'],includeChildren=False),
                'SCHEDULER',trigger='SCHEDULED')['cascadeId']
        worker = DependencyWorker(self.store); worker.step()
        self.complete(group, 'WF1'); worker.step()
        completed = self.rows()['WF1']['lastResult']
        self.assertEqual(completed['triggerSource'], 'SCHEDULED')
        self.start(include_children=False)
        row = self.rows()['WF1']
        self.assertEqual(row['requests'][0]['trigger']['kind'], 'MANUAL')
        self.assertEqual(row['lastResult'], completed)
        self.assertEqual(row['jobs'][0]['lastResult']['triggerSource'], 'SCHEDULED')

    def test_paused_schedule_configuration_remains_visible_without_plan_entries(self):
        from task_engine.schedules import Schedules
        Schedules(self.store).create(dict(requestId='paused-wf1',name='Paused WF1',cron='0 7 * * 1-5',
            timezone='Europe/Stockholm',enabled=False,
            target=dict(graphId=str(self.graph['id']),workflowId=self.ids['WF1'])), 'TEST')
        row = self.rows()['WF1']
        self.assertFalse(row['IS_SCHEDULE_ENABLED'])
        self.assertEqual(row['SCHEDULE'], '0 7 * * 1-5')
        self.assertEqual(row['requests'], [])

    def test_terminal_failed_plan_is_removed_but_failure_result_remains(self):
        group = self.start(); worker = DependencyWorker(self.store)
        worker.step(); self.complete(group, 'WF1', 'FAILED'); worker.step()
        self.assertEqual(self.graphs.read(group)['cascade']['state'], 'FAILED')
        rows = self.rows()
        self.assertTrue(all(not row['requests'] for row in rows.values()))
        self.assertEqual(rows['WF1']['lastResult']['status'], 'FAILED')
        self.assertEqual(rows['WF4']['lastResult']['status'], 'NO RESULT')

    def test_multiple_requests_keep_separate_ids_jobs_and_parent_reasons(self):
        self.finish()
        def disable_freshness(raw):
            for rule in raw['raw_records']:rule['PARENT_MUST_BE_NEWER']=False
        self.config.change(dict(revision=1),'TEST',disable_freshness)
        # Scheduled plans can reserve the same downstream workflow while its
        # direct-neighbor protection prevents overlapping manual requests.
        groups=[]
        for name in ['WF2', 'WF3']:
            with self.store.transaction() as conn:
                groups.append(self.graphs.start_in_transaction(conn,self.graph['id'],
                    dict(requestId='scheduled-'+name,workflowId=self.ids[name],includeChildren=True),
                    'SCHEDULER',trigger='SCHEDULED')['cascadeId'])
        worker = DependencyWorker(self.store)
        worker.step(); worker.step()
        requests = self.rows()['WF4']['requests']
        self.assertEqual(len(requests), 2)
        self.assertEqual({request['cascadeId'] for request in requests}, set(groups))
        self.assertEqual(len({request['runId'] for request in requests}), 2)
        self.assertTrue(all(request['status'] == 'WAITING' for request in requests))
        self.assertTrue(all(len(request['jobs']) == 1 for request in requests))
        for request in requests:
            self.assertTrue(request['requestedAt'])
            self.assertTrue(request['waitingFor'])
        # A third independent single-workflow plan can finish without clearing these.
        independent = self.start('Independent', include_children=False); worker.step()
        self.complete(independent, 'Independent')
        # Completion also wakes the two other groups. Drain those decisions
        # before asserting that this plan has reached its terminal projection.
        for _ in range(4):
            if self.graphs.read(independent)['cascade']['state']=='SUCCESS':break
            worker.step()
        self.assertEqual(len(self.rows()['WF4']['requests']), 2)
        self.assertEqual(self.rows()['Independent']['requests'], [])

    def test_idle_and_scheduled_intent_do_not_create_plan_requests(self):
        self.assertTrue(all(not row['requests'] for row in self.rows().values()))
        from task_engine.schedules import Schedules
        Schedules(self.store).create(dict(requestId='future-wf1',name='Future WF1', cron='0 7 * * 1-5', timezone='Europe/Stockholm', enabled=True,
            target=dict(graphId=str(self.graph['id']), workflowId=self.ids['WF1'])), 'TEST')
        root = self.rows()['WF1']
        self.assertTrue(root['IS_SCHEDULE_ENABLED'])
        self.assertEqual(root['requests'], [])
        self.assertTrue(root['scheduler']['nextRun'])

    def test_parent_waits_distinguish_current_plan_from_previous_plan_overlap(self):
        previous = self.start(); worker = DependencyWorker(self.store); worker.step()
        node = next(n for n in self.graphs.read(previous)['nodes'] if n['node_key']==self.graph['definition']['workflowKeys'][self.ids['WF1']])
        with self.store.transaction() as conn:
            conn.execute("UPDATE kumo_task_engine.attempts SET state='RUNNING',native_started_at=clock_timestamp() WHERE run_id=%s",(node['run_id'],))
            self.store.refresh(conn,node['run_id'])
            current = self.graphs.start_in_transaction(conn,self.graph['id'],
                dict(requestId='next-plan',workflowId=self.ids['WF1'],includeChildren=True),
                'SCHEDULER',trigger='SCHEDULED')['cascadeId']
        worker.step()
        requests = self.rows()['WF2']['requests']
        self.assertEqual(len(requests), 2)
        request = next(r for r in requests if r['cascadeId']==current)
        parents = [p for p in request['waitingFor'] if p['name']=='WF1']
        self.assertEqual({(p['cascadeId'],p['status'],p['overlap']) for p in parents},
            {(previous,'RUNNING',True),(current,'WAITING',False)})
        self.assertEqual(request['status'], 'WAITING')
        # Reading the projection never changes admission or the actual runs.
        self.assertIsNone(next(n for n in self.graphs.read(current)['nodes'] if n['node_key']==self.graph['definition']['workflowKeys'][self.ids['WF2']])['run_id'])
