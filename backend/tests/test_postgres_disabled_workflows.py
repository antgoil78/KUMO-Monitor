"""Disabled catalog workflows are omitted without inventing execution outcomes."""
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from task_engine.dependencies import DependencyWorker
from task_engine.schedules import Schedules, Scheduler
from task_engine.store import WorkflowBusy
import test_postgres_monitor as fixtures


@unittest.skipUnless(os.getenv('KUMO_TASK_ENGINE_TEST_DSN'),'Requires isolated PostgreSQL test database')
class DisabledWorkflowTests(unittest.TestCase):
    setUp=fixtures.PostgresMonitorTests.setUp
    start=fixtures.PostgresMonitorTests.start
    complete=fixtures.PostgresMonitorTests.complete

    def disable(self,name):
        self.config.workflow(self.ids[name],dict(revision=self.config.read()['revision'],enabled=False),'EDITOR')

    def nodes(self,group):
        keys={v:k for k,v in self.graph['definition']['workflowKeys'].items()}
        names={w['WORKFLOW_ID']:w['WORKFLOW_NAME'] for w in self.raw['workflows']}
        return {names[keys[n['node_key']]]:n for n in self.graphs.read(group)['nodes']}

    def chain(self):
        def replace(raw):
            for child,parent in [('WF3','WF2'),('WF4','WF3')]:
                raw['raw_records'][:]=[r for r in raw['raw_records'] if r['WORKFLOW_ID']!=self.ids[child] or r.get('JOB_ID')]
                raw['raw_records'].append(dict(WORKFLOW_ID=self.ids[child],JOB_ID=None,PARENT_ID=self.ids[parent],
                    TRIGGER_TYPE='DEP',ALLOW=True,SUCCESS=True,WARNING=False,ERROR=False,PARENT_MUST_BE_NEWER=True))
        self.config.change(dict(revision=self.config.read()['revision']),'EDITOR',replace)

    def test_enabled_child_waits_for_ancestor_through_disabled_middle(self):
        self.chain();self.disable('WF2')
        group=self.start();worker=DependencyWorker(self.store);worker.step()
        self.assertEqual(set(self.nodes(group)),{'WF1','WF3','WF4'})
        self.assertEqual(self.nodes(group)['WF3']['state'],'WAITING')
        self.assertIsNone(self.nodes(group)['WF3']['run_id'])
        root=self.graph['definition']['workflowKeys'][self.ids['WF1']]
        child=next(n for n in self.graphs.read(group)['cascade']['snapshot']['nodes'] if n['name']=='WF3')
        self.assertEqual([p['key'] for p in child['parents']],[root])
        self.complete(group,'WF1');worker.step()
        self.assertIsNotNone(self.nodes(group)['WF3']['run_id'])
        self.assertEqual(self.nodes(group)['WF4']['state'],'WAITING')

    def test_multiple_disabled_workflows_preserve_upstream_outcome_rules(self):
        self.chain();self.disable('WF2');self.disable('WF3')
        group=self.start();worker=DependencyWorker(self.store);worker.step()
        self.assertEqual(set(self.nodes(group)),{'WF1','WF4'})
        self.assertEqual(self.nodes(group)['WF4']['state'],'WAITING')
        self.complete(group,'WF1','FAILED');worker.step()
        self.assertEqual(self.nodes(group)['WF4']['state'],'BLOCKED')
        self.assertIsNone(self.nodes(group)['WF4']['run_id'])

    def test_shared_ancestor_is_checked_once_with_stricter_freshness(self):
        self.disable('WF2');self.disable('WF3')
        group=self.start()
        join=next(n for n in self.graphs.read(group)['cascade']['snapshot']['nodes'] if n['name']=='WF4')
        self.assertEqual(len(join['parents']),1)
        self.assertTrue(join['parents'][0]['parentMustBeNewer'])
        worker=DependencyWorker(self.store);worker.step();self.complete(group,'WF1');worker.step()
        self.assertIsNotNone(self.nodes(group)['WF4']['run_id'])

    def test_bypassed_ancestor_remains_a_direct_overlap_guard(self):
        self.chain();self.disable('WF2')
        older=self.start('WF1',include_children=False);worker=DependencyWorker(self.store);worker.step()
        row=next(w for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='WF3')
        self.assertFalse(row['canRun'])
        with self.assertRaises(WorkflowBusy):self.start('WF3',include_children=False)
        self.complete(older,'WF1');worker.step()
        self.assertTrue(self.start('WF3',include_children=False))

    def test_already_accepted_disabled_work_must_finish_before_overlap(self):
        self.chain()
        older=self.start('WF2',include_children=False);worker=DependencyWorker(self.store);worker.step()
        self.disable('WF2')
        with self.assertRaises(WorkflowBusy):self.start('WF3',include_children=False)
        self.complete(older,'WF2');worker.step()
        self.assertTrue(self.start('WF3',include_children=False))

    def test_disabled_root_does_not_require_old_outcome_or_timestamp_for_schedule(self):
        self.disable('WF1')
        schedule=Schedules(self.store).create(dict(requestId='disabled-parent-schedule',name='Child schedule',
            cron='* * * * *',timezone='UTC',enabled=True,target=dict(graphId=str(self.graph['id']),workflowId=self.ids['WF2'])),'EDITOR')['schedule']
        with self.store.connect() as c:
            c.execute("UPDATE kumo_task_engine.schedules SET next_fire_at=clock_timestamp()-interval '1 second' WHERE id=%s",(schedule['id'],))
        self.assertTrue(Scheduler(self.store).step())
        group=str(Schedules(self.store).status()['occurrences'][0]['cascade_id'])
        DependencyWorker(self.store).step()
        self.assertIsNotNone(self.nodes(group)['WF2']['run_id'])
        self.assertNotIn('WF1',self.nodes(group))

    def test_disabled_join_parent_is_omitted_and_its_rules_are_ignored(self):
        self.disable('WF2')
        def deny_disabled(raw):
            rule=next(r for r in raw['raw_records'] if r['WORKFLOW_ID']==self.ids['WF4'] and not r.get('JOB_ID') and r.get('PARENT_ID')==self.ids['WF2'])
            rule.update(ALLOW=False,SUCCESS=False,WARNING=False,ERROR=False,PARENT_MUST_BE_NEWER=True,SQL='SELECT FALSE')
        self.config.change(dict(revision=2),'EDITOR',deny_disabled)
        group=self.start();worker=DependencyWorker(self.store);worker.step()
        self.assertNotIn('WF2',self.nodes(group))
        self.complete(group,'WF1');worker.step()
        self.assertEqual(self.nodes(group)['WF4']['state'],'WAITING')
        self.complete(group,'WF3');worker.step()
        self.assertIsNotNone(self.nodes(group)['WF4']['run_id'])
        rows={w['WORKFLOW_NAME']:w for w in self.monitor.payload()['rows']}
        self.assertEqual(rows['WF2']['execution']['status'],'NO RUN')
        self.assertEqual(rows['WF2']['waitingCount'],0)
        with self.store.connect() as c:
            fact=c.execute("SELECT facts FROM kumo_task_engine.dependency_decisions WHERE cascade_id=%s AND decision='REQUEST_ACCEPTED'",(group,)).fetchone()['facts']
        self.assertEqual(fact['ignoredDisabledWorkflowKeys'],[self.graph['definition']['workflowKeys'][self.ids['WF2']]])

    def test_disabled_unsupported_jobs_do_not_disable_the_enabled_chain(self):
        self.disable('WF2')
        def unsupported(raw):
            job=next(j for j in raw['jobs'] if j['JOB_ID']==next(r['JOB_ID'] for r in raw['raw_records'] if r['WORKFLOW_ID']==self.ids['WF2'] and r.get('JOB_ID')))
            job.update(JOB_TYPE='COMMAND',SQL_COMMAND=None)
        self.config.change(dict(revision=2),'EDITOR',unsupported)
        rows={w['WORKFLOW_NAME']:w for w in self.monitor.payload()['rows']}
        self.assertTrue(rows['WF1']['canRunWithChildren'])
        group=self.start();DependencyWorker(self.store).step()
        self.assertNotIn('WF2',self.nodes(group))

    def test_disabling_does_not_change_previously_accepted_snapshots(self):
        group=self.start();before=self.graphs.read(group)['cascade']['snapshot']
        self.disable('WF2')
        self.assertEqual(self.graphs.read(group)['cascade']['snapshot'],before)
        worker=DependencyWorker(self.store);worker.step()
        self.complete(group,'WF1');worker.step()
        self.assertIsNotNone(self.nodes(group)['WF2']['run_id'])
