"""Failed workflow recovery, with real PostgreSQL and no Snowflake access."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from task_engine.catalog import CatalogConfiguration
from task_engine.dependencies import DependencyWorker
from task_engine.store import Conflict,Store
import test_postgres_dependency_scenarios as scenarios


@unittest.skipUnless(os.getenv('KUMO_TASK_ENGINE_TEST_DSN'),'Requires isolated PostgreSQL test database')
class WorkflowRetryTests(unittest.TestCase):
    setUp=scenarios.DependencyScenarioTests.setUp
    start=scenarios.DependencyScenarioTests.start
    node=scenarios.DependencyScenarioTests.node
    drain=scenarios.DependencyScenarioTests.drain
    outcome=scenarios.DependencyScenarioTests.outcome
    rows_by_name=scenarios.DependencyScenarioTests.rows_by_name
    finish_groups=scenarios.DependencyScenarioTests.finish_groups
    decisions=scenarios.DependencyScenarioTests.decisions
    clear_work=scenarios.DependencyScenarioTests.clear_work

    def failed_pair(self,finish_three=True):
        two=self.start('WF2');three=self.start('WF3',children=True);self.drain()
        self.outcome(two,'WF2','FAILED')
        if finish_three:self.outcome(three,'WF3')
        self.drain()
        self.assertEqual(self.node(three,'WF4')['state'],'BLOCKED')
        return two,three

    def retry(self,group,name='WF2',actor='RETRY USER'):
        return self.graphs.retry(group,self.node(group,name)['id'],actor)

    def test_external_failed_parent_retry_restores_waiting_and_preserves_success_and_history(self):
        two,three=self.failed_pair()
        original=deepcopy(self.graphs.read(three));original_failed=self.node(two,'WF2')['run_id']
        old_three=self.node(three,'WF3')['run_id']
        # A repaired workflow uses current SQL; descendants retain their accepted plans.
        config=CatalogConfiguration(self.store).read()
        wf2job=next(a['JOB_ID'] for a in config['assignments'] if a['WORKFLOW_ID']==self.ids['WF2'])
        CatalogConfiguration(self.store).job(wf2job,dict(revision=config['revision'],sqlCommand='SELECT 42'),'FIXER')
        config=CatalogConfiguration(self.store).read()
        wf4job=next(a['JOB_ID'] for a in config['assignments'] if a['WORKFLOW_ID']==self.ids['WF4'])
        CatalogConfiguration(self.store).job(wf4job,dict(revision=config['revision'],sqlCommand='SELECT 99'),'EDITOR')
        receipt=self.retry(two);group=receipt['cascadeId']
        self.assertEqual(set(receipt['resumedWorkflows']),{'WF2','WF4','WF5','WF6','WF7','WF8','WF9','WF10'})
        before=self.rows_by_name()
        for name in receipt['resumedWorkflows']:
            self.assertEqual(before[name]['run']['status'],'WAITING')
            self.assertTrue(before[name]['canCancel'])
        self.assertIn('Waiting for parent WF2',before['WF4']['run']['message'])
        self.assertEqual(before['WF3']['run']['status'],'SUCCESS')
        self.assertEqual(self.node(three,'WF3')['run_id'],old_three)
        self.assertEqual(self.graphs.read(three),original)
        resumed_four=next(d for d in self.graphs.read(group)['cascade']['snapshot']['nodes'] if d['name']=='WF4')
        original_four=next(d for d in original['cascade']['snapshot']['nodes'] if d['name']=='WF4')
        self.assertEqual(resumed_four['plan'],original_four['plan'])
        self.worker=DependencyWorker(Store(self.store.settings));self.drain()
        self.assertEqual(self.rows_by_name()['WF2']['run']['status'],'QUEUED')
        self.assertEqual(self.rows_by_name()['WF4']['run']['status'],'WAITING')
        new_run=self.node(group,'WF2')['run_id']
        self.assertNotEqual(new_run,original_failed)
        self.assertEqual(self.store.read(new_run)['jobs'][0]['snapshot']['sql'],'SELECT 42')
        self.outcome(group,'WF2');self.drain()
        self.assertIsNotNone(self.node(group,'WF4')['run_id'])
        self.finish_groups([group])
        self.assertEqual(self.store.read(original_failed)['run']['state'],'FAILED')
        self.assertEqual(self.graphs.read(three),original)
        self.assertEqual(len(self.store.status()['runs']),10)
        audit=self.decisions(group)
        self.assertEqual(len([d for d in audit if d['decision']=='BLOCKED_WORKFLOW_RESUMED']),7)
        self.assertTrue(all(d['actor']=='RETRY USER' for d in audit if d['component']=='dependency-retry'))

    def test_internal_failed_parent_retains_successful_sibling_and_ancestor(self):
        original=self.start('WF1',children=True);self.drain()
        self.outcome(original,'WF1');self.drain()
        self.outcome(original,'WF2','FAILED');self.outcome(original,'WF3');self.drain()
        receipt=self.retry(original);retry=receipt['cascadeId']
        self.assertNotIn('WF1',receipt['resumedWorkflows']);self.assertNotIn('WF3',receipt['resumedWorkflows'])
        self.finish_groups([retry])
        self.assertEqual(self.node(original,'WF1')['run_state'],'SUCCESS')
        self.assertEqual(self.node(original,'WF3')['run_state'],'SUCCESS')
        self.assertEqual(self.graphs.read(original)['cascade']['state'],'FAILED')

    def test_retry_waits_for_the_original_still_running_sibling(self):
        two,three=self.failed_pair(finish_three=False)
        group=self.retry(two)['cascadeId'];self.drain()
        self.outcome(group,'WF2');self.drain()
        self.assertIsNone(self.node(group,'WF4')['run_id'])
        self.assertIn('WF3',self.rows_by_name()['WF4']['run']['message'])
        self.outcome(three,'WF3');self.drain()
        self.assertIsNotNone(self.node(group,'WF4')['run_id'])
        self.finish_groups([group])

    def test_retry_can_fail_again_and_another_retry_resumes_the_same_branch_once(self):
        two,_=self.failed_pair()
        first=self.retry(two)['cascadeId'];self.drain()
        self.outcome(first,'WF2','FAILED');self.drain()
        self.assertEqual(self.rows_by_name()['WF4']['run']['status'],'BLOCKED')
        second=self.retry(first)['cascadeId']
        self.assertNotEqual(first,second)
        self.assertEqual(self.rows_by_name()['WF4']['run']['status'],'WAITING')
        self.finish_groups([second])
        self.assertEqual(self.node(first,'WF2')['run_state'],'FAILED')

    def test_two_failed_parents_must_both_be_retried_before_the_join_starts(self):
        two=self.start('WF2');three=self.start('WF3',children=True);self.drain()
        self.outcome(two,'WF2','FAILED');self.outcome(three,'WF3','FAILED');self.drain()
        first=self.retry(two)['cascadeId'];self.drain();self.outcome(first,'WF2');self.drain()
        self.assertEqual(self.node(first,'WF4')['state'],'BLOCKED')
        second=self.retry(three,'WF3')['cascadeId']
        self.assertEqual(set(self.graphs.read(second)['cascade']['snapshot']['workflowIds'].values()),
            {self.ids[n] for n in ['WF3','WF4','WF5','WF6','WF7','WF8','WF9','WF10']})
        self.finish_groups([second])
        self.assertEqual(self.node(first,'WF2')['run_state'],'SUCCESS')

    def test_thirty_simultaneous_retries_create_one_durable_recovery_group(self):
        two,_=self.failed_pair()
        with ThreadPoolExecutor(max_workers=30) as pool:
            receipts=list(pool.map(lambda i:self.retry(two,actor=f'USER {i}'),range(30)))
        self.assertEqual(len({r['cascadeId'] for r in receipts}),1)
        self.assertEqual(sum(not r['reused'] for r in receipts),1)
        group=receipts[0]['cascadeId'];self.finish_groups([group])
        self.assertEqual(len([d for d in self.decisions(group) if d['decision']=='WORKFLOW_RETRY_REQUESTED']),1)
        self.assertEqual(len(self.store.status()['runs']),10)

    def test_newer_successful_child_supersedes_old_blocked_branch(self):
        two,_=self.failed_pair()
        fresh=self.start('WF4');self.drain();self.outcome(fresh,'WF4');self.drain()
        receipt=self.retry(two)
        self.assertEqual(receipt['resumedWorkflows'],['WF2'])
        self.finish_groups([receipt['cascadeId']])

    def test_retry_rejects_superseded_failure_and_records_the_rejection(self):
        two,_=self.failed_pair()
        fresh=self.start('WF2');self.drain();self.outcome(fresh,'WF2');self.drain()
        with self.assertRaises(Conflict):self.retry(two)
        self.assertEqual(self.decisions()[-1]['decision'],'WORKFLOW_RETRY_REJECTED')

    def test_cancelled_group_is_not_reopened(self):
        two,three=self.failed_pair(finish_three=False)
        self.graphs.cancel(three);self.drain()
        receipt=self.retry(two)
        self.assertEqual(receipt['resumedWorkflows'],['WF2'])

    def test_unknown_and_cancelled_source_are_not_retried(self):
        for state in ['UNKNOWN','CANCELLED','SUCCESS']:
            with self.subTest(state=state):
                self.clear_work();group=self.start('WF2');self.drain();self.outcome(group,'WF2',state);self.drain()
                with self.assertRaises(Conflict):self.retry(group)

    def test_a_running_direct_parent_blocks_retry_but_a_completed_parent_releases_it(self):
        two,_=self.failed_pair()
        parent=self.start('WF1');self.drain()
        with self.assertRaises(Conflict):self.retry(two)
        self.outcome(parent,'WF1');self.drain()
        self.finish_groups([self.retry(two)['cascadeId']])

    def test_cancellation_of_a_group_with_a_failed_branch_prevents_resumption(self):
        group=self.start('WF1',children=True);self.drain()
        self.outcome(group,'WF1');self.drain();self.outcome(group,'WF2','FAILED');self.drain()
        self.graphs.cancel(group);self.drain()
        with self.assertRaises(Conflict):self.retry(group)

    def test_cancelling_recovery_does_not_start_blocked_descendants(self):
        two,three=self.failed_pair()
        group=self.retry(two)['cascadeId'];self.drain()
        self.graphs.cancel(group);self.drain()
        self.assertEqual(self.graphs.read(group)['cascade']['state'],'CANCELLED')
        self.assertTrue(all(n['run_id'] is None for n in self.graphs.read(group)['nodes'] if self.names[n['node_key']]!='WF2'))
        self.assertEqual(self.node(three,'WF3')['run_state'],'SUCCESS')

    def test_disabled_workflow_cannot_be_retried(self):
        two,_=self.failed_pair()
        config=CatalogConfiguration(self.store).read()
        CatalogConfiguration(self.store).workflow(self.ids['WF2'],dict(revision=config['revision'],enabled=False),'DISABLER')
        with self.assertRaises(Conflict):self.retry(two)

    def test_audit_failure_rolls_back_recovery_and_leaves_original_history_unchanged(self):
        two,three=self.failed_pair();original=self.graphs.read(three)
        with patch('task_engine.retries.record',side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):self.retry(two)
        self.assertEqual(self.graphs.read(three),original)
        self.assertEqual(len(self.graphs.status()['cascades']),2)
        self.finish_groups([self.retry(two)['cascadeId']])
