"""Complete catalog promotion and shared-worker Monitor projections on real PG."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime,timezone
import os
from pathlib import Path
import sys
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from task_engine.catalog import import_catalog,CatalogConfiguration,compile_catalog
from task_engine.dependencies import Graphs,DependencyWorker
from task_engine.monitor import Monitor,execution_scope
from task_engine.settings import Settings
from task_engine.store import Store,Conflict,WorkflowBusy
from task_engine.schedules import Schedules,Scheduler
from task_engine.worker import Worker
from test_postgres_dependencies import source,snapshot
from test_postgres_task_engine import FakeTasks


def catalog():
    data=source();data['raw_records']=data.pop('rules')
    for row in data['raw_records']:
        if not row.get('PARENT_ID'):row['PARENT_MUST_BE_NEWER']=False
    extra_wf=str(uuid.uuid4());extra_job=str(uuid.uuid4());unassigned=str(uuid.uuid4());dbt=str(uuid.uuid4());dbt_wf=str(uuid.uuid4())
    data['workflows'] += [dict(WORKFLOW_ID=extra_wf,WORKFLOW_NAME='Independent',IS_ENABLED=True,IS_SCHEDULE_ENABLED=True,SCHEDULE='* * * * *',JOB_EXECUTION_MODE='SEQUENTIAL'),
        dict(WORKFLOW_ID=dbt_wf,WORKFLOW_NAME='DBT workflow',IS_ENABLED=True,JOB_EXECUTION_MODE='SEQUENTIAL')]
    data['jobs'] += [dict(JOB_ID=extra_job,JOB_NAME='Independent SQL',JOB_TYPE='SQL',SQL_COMMAND='SELECT 2',IS_ENABLED=True,TIMEOUT_SECONDS=60),
        dict(JOB_ID=unassigned,JOB_NAME='Unassigned SQL',JOB_TYPE='SQL',SQL_COMMAND='SELECT 3',IS_ENABLED=True),
        dict(JOB_ID=dbt,JOB_NAME='DBT job',JOB_TYPE='DBT',DBT_COMMAND='dbt build',IS_ENABLED=True)]
    for workflow,job in [(extra_wf,extra_job),(dbt_wf,dbt)]:
        data['raw_records'] += [dict(WORKFLOW_ID=workflow,JOB_ID=None,PARENT_ID=None,TRIGGER_TYPE=None,ALLOW=True),
            dict(WORKFLOW_ID=workflow,JOB_ID=job,PARENT_ID=workflow,TRIGGER_TYPE='DEP',ALLOW=True)]
    return data


@unittest.skipUnless(os.getenv('KUMO_TASK_ENGINE_TEST_DSN'),'Requires isolated PostgreSQL test database')
class PostgresMonitorTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(Settings(os.environ['KUMO_TASK_ENGINE_TEST_DSN']))
        with self.store.connect() as conn:assert conn.execute('SELECT current_database() AS name').fetchone()['name'].endswith('_test')
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute('TRUNCATE kumo_task_engine.dependency_graphs,kumo_task_engine.dependency_workers,kumo_task_engine.schedules,kumo_task_engine.scheduler_workers,kumo_task_engine.runs,kumo_task_engine.events,kumo_task_engine.attempts RESTART IDENTITY CASCADE')
        self.raw=catalog();self.graph=import_catalog(self.store,self.raw,'IMPORTER')
        self.config=CatalogConfiguration(self.store);self.monitor=Monitor(self.store);self.graphs=Graphs(self.store)
        self.ids={w['WORKFLOW_NAME']:w['WORKFLOW_ID'] for w in self.raw['workflows']}

    def start(self,name='WF1',request=None,include_children=True):
        return self.graphs.start(self.graph['id'],dict(requestId=request or str(uuid.uuid4()),workflowId=self.ids[name],includeChildren=include_children),'RUNNER')['cascadeId']

    def complete(self,cascade,name,state='SUCCESS'):
        key=self.graph['definition']['workflowKeys'][self.ids[name]]
        node=next(n for n in self.graphs.read(cascade)['nodes'] if n['node_key']==key)
        with self.store.transaction() as conn:
            conn.execute("UPDATE kumo_task_engine.attempts SET state=%s,native_started_at=clock_timestamp()-interval '1 second',ended_at=clock_timestamp(),next_check_at=NULL WHERE run_id=%s",(state,node['run_id']))
            self.store.refresh(conn,node['run_id'])

    def finish(self):
        cascade=self.start();worker=DependencyWorker(self.store)
        worker.step();self.complete(cascade,'WF1');worker.step();self.complete(cascade,'WF2');self.complete(cascade,'WF3');worker.step();self.complete(cascade,'WF4');worker.step()
        return cascade

    def test_every_workflow_and_job_is_retained_including_dbt_and_unassigned(self):
        data=self.monitor.payload()
        self.assertEqual((data['workflowsCount'],data['jobsCount'],data['unassignedJobs']),(6,7,1))
        self.assertEqual({j['JOB_ID'] for w in data['rows'] for j in w['jobs']},{j['JOB_ID'] for j in self.raw['jobs']})
        dbt=next(w for w in data['rows'] if w['WORKFLOW_NAME']=='DBT workflow')
        self.assertFalse(dbt['canRun']);self.assertIn('DBT execution',dbt['runUnavailable'])
        self.assertFalse(next(w for w in data['rows'] if w['WORKFLOW_NAME']=='Independent')['IS_SCHEDULE_ENABLED'])
        self.assertEqual(self.monitor.history({})['runs'],[])

    def test_import_cannot_replace_master_and_does_not_admit_any_work(self):
        with self.assertRaises(Conflict):import_catalog(self.store,self.raw,'OVERWRITE')
        self.assertFalse(self.store.status()['runs'])
        self.assertEqual(self.config.read()['revision'],1)

    def test_promotion_preserves_existing_postgres_trial_edits_and_history(self):
        with self.store.connect() as conn:conn.execute('DELETE FROM kumo_task_engine.dependency_graphs')
        original=source();body,metadata=snapshot(original)
        body['nodes'][1]['name']='WF2 edited';body['nodes'][1]['plan']['jobs'][0]['sql']='SELECT 42'
        trial=self.graphs.install(body,'EDITOR',metadata)['graph']
        old=self.graphs.start(trial['id'],dict(requestId='old-history'),'RUNNER')['cascadeId']
        self.graphs.cancel(old);DependencyWorker(self.store).step()
        graph=import_catalog(self.store,self.raw,'PROMOTE')
        config=self.config.read()
        self.assertIn('WF2 edited',[w['WORKFLOW_NAME'] for w in config['workflows']])
        self.assertTrue(any(j.get('SQL_COMMAND')=='SELECT 42' for j in config['jobs']))
        self.assertEqual(len(self.graphs.read(old)['nodes']),4)
        self.assertEqual(len(self.monitor.history({})['runs']),4)
        with self.assertRaises(Conflict):self.graphs.start(trial['id'],dict(requestId='old-route'),'RUNNER')

    def test_root_can_start_the_full_supported_component(self):
        with self.assertRaises(ValueError):self.graphs.start(self.graph['id'],dict(requestId='malformed',workflowId=[]),'RUNNER')
        with self.assertRaises(Conflict):self.start('DBT workflow')
        group=self.start();detail=self.graphs.read(group)
        self.assertEqual(len(detail['nodes']),4)
        self.assertEqual(set(detail['cascade']['snapshot']['workflowIds'].values()),{self.ids[k] for k in ['WF1','WF2','WF3','WF4']})
        self.assertNotIn('catalog',detail['cascade']['snapshot'])
        with self.assertRaises(WorkflowBusy):self.start()

    def test_manual_middle_workflow_can_run_alone_or_with_downstream_children(self):
        only=self.start('WF2',include_children=False);DependencyWorker(self.store).step()
        self.assertEqual(len(self.graphs.read(only)['nodes']),1)
        self.assertIsNotNone(self.graphs.read(only)['nodes'][0]['run_id'])
        self.complete(only,'WF2');DependencyWorker(self.store).step()
        children=self.start('WF2');snapshot=self.graphs.read(children)['cascade']['snapshot']
        self.assertEqual({n['name'] for n in snapshot['nodes']},{'WF2','WF4'})
        self.assertEqual(next(n['parents'] for n in snapshot['nodes'] if n['name']=='WF2'),[])
        self.assertNotIn(self.ids['WF1'],snapshot['workflowIds'].values())

    def test_manual_overlap_checks_only_the_same_workflow_and_direct_neighbors(self):
        group=self.start('WF4',include_children=False);DependencyWorker(self.store).step()
        rows={w['WORKFLOW_NAME']:w for w in self.monitor.payload()['rows']}
        for name in ['WF2','WF3','WF4']:
            self.assertFalse(rows[name]['canRun']);self.assertIn('WF4',rows[name]['runUnavailable'])
            with self.assertRaises(WorkflowBusy):self.start(name,include_children=False)
        self.assertTrue(rows['Independent']['canRun'])
        self.assertTrue(rows['WF1']['canRun'])
        self.complete(group,'WF4');DependencyWorker(self.store).step()
        self.assertTrue(next(w['canRun'] for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='WF1'))
        parent=self.start('WF1',include_children=False)
        with self.assertRaises(WorkflowBusy):self.start('WF2',include_children=False)
        self.assertTrue(self.start('WF4',include_children=False))
        self.assertEqual(len(self.graphs.read(parent)['nodes']),1)

    def test_siblings_can_run_alone_concurrently_and_simultaneous_family_starts_are_serialized(self):
        siblings=[self.start(name,include_children=False) for name in ['WF2','WF3']]
        self.assertEqual(len(set(siblings)),2)
        worker=DependencyWorker(self.store)
        for _ in siblings:worker.step()
        for name,group in zip(['WF2','WF3'],siblings):self.complete(group,name)
        while worker.step():pass
        def submit(name):
            try:return self.start(name,include_children=False)
            except WorkflowBusy:return 'BLOCKED'
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(submit,['WF2','WF4']))
        self.assertEqual(results.count('BLOCKED'),1)

    def add_wf10(self):
        for name,parents in [('WF5',['WF4']),('WF6',['WF4']),('WF7',['WF5','WF6']),('WF8',['WF7']),('WF9',['WF7']),('WF10',['WF8','WF9'])]:
            identity=str(uuid.uuid4());job=str(uuid.uuid4());self.ids[name]=identity
            self.raw['workflows'].append(dict(WORKFLOW_ID=identity,WORKFLOW_NAME=name,IS_ENABLED=True,IS_SCHEDULE_ENABLED=False,JOB_EXECUTION_MODE='SEQUENTIAL'))
            self.raw['jobs'].append(dict(JOB_ID=job,JOB_NAME=name+' SQL',JOB_TYPE='SQL',SQL_COMMAND='SELECT 10',IS_ENABLED=True))
            self.raw['raw_records'].append(dict(WORKFLOW_ID=identity,JOB_ID=job,PARENT_ID=identity,TRIGGER_TYPE='DEP',ALLOW=True))
            for parent in parents:
                self.raw['raw_records'].append(dict(WORKFLOW_ID=identity,JOB_ID=None,PARENT_ID=self.ids[parent],TRIGGER_TYPE='DEP',ALLOW=True,SUCCESS=True,WARNING=False,ERROR=False,PARENT_MUST_BE_NEWER=False))
        with self.store.connect() as conn:conn.execute('DELETE FROM kumo_task_engine.dependency_graphs WHERE id=%s',(self.graph['id'],))
        self.graph=import_catalog(self.store,self.raw,'EXTENDED FIXTURE')

    def test_wf10_blocks_only_wf8_wf9_and_itself_and_downstream_work_waits_at_that_boundary(self):
        self.add_wf10();worker=DependencyWorker(self.store)
        leaf=self.start('WF10',include_children=False);worker.step()
        native=self.graphs.read(leaf)['nodes'][0]['run_id']
        with self.store.transaction() as conn:
            conn.execute("UPDATE kumo_task_engine.attempts SET state='RUNNING',native_started_at=clock_timestamp() WHERE run_id=%s",(native,));self.store.refresh(conn,native)
        before={w['WORKFLOW_NAME']:w for w in self.monitor.payload()['rows']}
        for name in [f'WF{i}' for i in range(1,11)]:
            self.assertEqual(before[name]['canRun'],name not in {'WF8','WF9','WF10'},name)
        for name in ['WF8','WF9','WF10']:
            with self.assertRaises(WorkflowBusy):self.start(name,include_children=False)
        group=self.start('WF1');worker.step()
        snapshot=self.graphs.read(group)['cascade']['snapshot']
        # A newer pending WF10 must not hide the actually running execution.
        during={w['WORKFLOW_NAME']:w for w in self.monitor.payload()['rows']}
        self.assertEqual(during['WF10']['run']['status'],'RUNNING')
        self.assertEqual(during['WF10']['cascadeId'],leaf)
        self.assertEqual(during['WF10']['execution']['status'],'RUNNING')
        self.assertEqual(during['WF10']['planning']['status'],'RUNNING')
        self.assertEqual(during['WF10']['planning']['waitingCount'],1)
        self.assertEqual(during['WF10']['planning']['cascadeId'],group)
        self.assertEqual(during['WF10']['queuedCount'],0);self.assertEqual(during['WF10']['waitingCount'],1)
        def node(name):
            return next(n for n in self.graphs.read(group)['nodes'] if n['node_key']==self.graph['definition']['workflowKeys'][self.ids[name]])
        self.assertIsNotNone(node('WF1')['run_id'])
        self.complete(group,'WF1')
        while worker.step():pass
        for name in ['WF2','WF3']:self.assertIsNotNone(node(name)['run_id'])
        for frontier in [['WF2','WF3'],['WF4'],['WF5','WF6'],['WF7']]:
            for name in frontier:self.complete(group,name)
            while worker.step():pass
        queued={w['WORKFLOW_NAME']:w for w in self.monitor.payload()['rows']}
        for name in ['WF8','WF9']:
            self.assertIsNone(node(name)['run_id'])
            self.assertEqual(queued[name]['run']['status'],'WAITING');self.assertIn('WF10',queued[name]['run']['message'])
            self.assertEqual(queued[name]['waitingCount'],1);self.assertEqual(queued[name]['queuedCount'],0)
        history=self.monitor.history(dict(status='WAITING'))['runs']
        self.assertTrue({'WF8','WF9'}.issubset({r['workflowName'] for r in history}))
        self.assertFalse({'WF8','WF9'} & {r['workflowName'] for r in self.monitor.history(dict(status='QUEUED'))['runs']})
        waiting_node=next(n for n in self.graphs.read(group)['nodes'] if n['node_key']==self.graph['definition']['workflowKeys'][self.ids['WF8']])
        self.assertEqual(self.monitor.log(self.ids['WF8'],waiting_node['id'])['history']['STATUS'],'WAITING')
        self.complete(leaf,'WF10');worker=DependencyWorker(Store(self.store.settings))
        while worker.step():pass
        admitted={w['WORKFLOW_NAME']:w for w in self.monitor.payload()['rows']}
        for name in ['WF8','WF9']:
            self.assertIsNotNone(node(name)['run_id'])
            self.assertEqual(admitted[name]['run']['status'],'QUEUED')
            self.assertEqual(admitted[name]['waitingCount'],0);self.assertEqual(admitted[name]['queuedCount'],1)
        for name in ['WF8','WF9']:self.complete(group,name)
        while worker.step():pass
        self.assertIsNotNone(node('WF10')['run_id'])
        self.assertEqual(self.graphs.read(group)['cascade']['snapshot'],snapshot)

    def test_wf1_only_can_start_while_distant_wf10_runs_without_creating_children(self):
        self.add_wf10();worker=DependencyWorker(self.store)
        self.start('WF10',include_children=False);worker.step()
        group=self.start('WF1',include_children=False);worker.step()
        nodes=self.graphs.read(group)['nodes']
        self.assertEqual(len(nodes),1);self.assertIsNotNone(nodes[0]['run_id'])
        self.assertEqual(len(self.store.status()['runs']),2)

    def test_starting_wf4_while_wf7_runs_shows_its_descendants_waiting(self):
        self.add_wf10();worker=DependencyWorker(self.store)
        older=self.start('WF7');worker.step()
        native=next(n['run_id'] for n in self.graphs.read(older)['nodes'] if n['run_id'])
        with self.store.transaction() as conn:
            conn.execute("UPDATE kumo_task_engine.attempts SET state='RUNNING',native_started_at=clock_timestamp() WHERE run_id=%s",(native,))
            self.store.refresh(conn,native)
        newer=self.start('WF4');worker.step()
        history=self.monitor.history(dict(status='WAITING'))['runs']
        descendants={r['workflowName']:r for r in history if r['cascadeId']==newer}
        self.assertEqual(set(descendants),{'WF5','WF6','WF7','WF8','WF9','WF10'})
        self.assertTrue(all(r['start'] is None and r['waitingFor'] for r in descendants.values()))
        self.assertTrue(all(n['run_id'] is None for n in self.graphs.read(newer)['nodes'] if n['node_key']!=self.graph['definition']['workflowKeys'][self.ids['WF4']]))
        rows={w['WORKFLOW_NAME']:w for w in self.monitor.payload()['rows']}
        self.assertEqual(rows['WF4']['run']['status'],'QUEUED')
        self.assertEqual(rows['WF4']['queuedCount'],1);self.assertEqual(rows['WF4']['waitingCount'],0)
        self.assertEqual(rows['WF7']['run']['status'],'RUNNING')
        self.assertEqual(rows['WF7']['cascadeId'],older)
        self.assertEqual(rows['WF7']['waitingCount'],1);self.assertEqual(rows['WF7']['queuedCount'],0)
        for name in ['WF5','WF6','WF8','WF9','WF10']:
            self.assertEqual(rows[name]['run']['status'],'WAITING')
            self.assertGreater(rows[name]['waitingCount'],0);self.assertEqual(rows[name]['queuedCount'],0)
        self.assertEqual(len(self.store.status()['runs']),2)

    def test_wf4_blocks_only_wf2_wf3_wf5_wf6_and_itself(self):
        self.add_wf10();worker=DependencyWorker(self.store)
        self.start('WF4',include_children=False);worker.step()
        rows={w['WORKFLOW_NAME']:w for w in self.monitor.payload()['rows']}
        blocked={'WF2','WF3','WF4','WF5','WF6'}
        for name in [f'WF{i}' for i in range(1,11)]:
            self.assertEqual(rows[name]['canRun'],name not in blocked,name)
        for name in blocked:
            with self.assertRaises(WorkflowBusy):self.start(name,include_children=False)
        self.assertTrue(self.start('WF1',include_children=False))
        self.assertTrue(self.start('WF10',include_children=False))
        while worker.step():pass
        self.assertEqual(len(self.store.status()['runs']),3)

    def test_manual_scope_is_part_of_idempotent_request_identity(self):
        self.start('WF2',request='scope',include_children=False)
        with self.assertRaises(Conflict):self.start('WF2',request='scope',include_children=True)
        with self.assertRaises(ValueError):self.graphs.start(self.graph['id'],dict(requestId='invalid',workflowId=self.ids['WF1'],includeChildren='false'),'RUNNER')

    def schedule(self,name,cron='* * * * *'):
        schedule=Schedules(self.store).create(dict(requestId=str(uuid.uuid4()),name=name,cron=cron,timezone='Europe/Stockholm',enabled=True,
            target=dict(graphId=str(self.graph['id']),workflowId=self.ids[name])),'SCHEDULER TEST')['schedule']
        with self.store.connect() as conn:conn.execute("UPDATE kumo_task_engine.schedules SET next_fire_at=clock_timestamp()-interval '1 second' WHERE id=%s",(schedule['id'],))
        return schedule

    def test_scheduled_child_waits_behind_running_parent_and_completion_releases_it_without_clients(self):
        parent=self.start('WF1',include_children=False);worker=DependencyWorker(self.store);worker.step()
        self.schedule('WF2');self.assertTrue(Scheduler(self.store).step());worker.step()
        row=next(w for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='WF2')
        self.assertEqual(row['run']['status'],'WAITING');self.assertIn('WF1',row['run']['message']);self.assertFalse(row['canRun'])
        self.assertIn(self.ids['WF2'],[r['workflowId'] for r in self.monitor.history(dict(status='WAITING'))['runs']])
        group=row['cascadeId'];self.assertTrue(all(n['run_id'] is None for n in self.graphs.read(group)['nodes']))
        # Restarting the dispatcher does not lose the queued request.
        worker=DependencyWorker(Store(self.store.settings));self.assertFalse(worker.step())
        self.complete(parent,'WF1')
        while worker.step():pass
        node=next(n for n in self.graphs.read(group)['nodes'] if n['node_key']==self.graph['definition']['workflowKeys'][self.ids['WF2']])
        self.assertIsNotNone(node['run_id'])
        self.assertEqual(len([r for r in self.store.status()['runs'] if r['state']=='QUEUED']),1)

    def test_scheduled_occurrence_reuses_waiting_child_of_active_cascade(self):
        group=self.start();worker=DependencyWorker(self.store);worker.step()
        schedule=self.schedule('WF2');Scheduler(self.store).step()
        occurrences=Schedules(self.store).status()['occurrences']
        self.assertEqual(str(occurrences[0]['cascade_id']),group)
        self.assertEqual(occurrences[0]['run_state'],'QUEUED')
        row=next(w for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='WF2')
        self.assertEqual(row['run']['status'],'WAITING');self.assertTrue(row['IS_SCHEDULE_ENABLED'])
        self.assertIn(self.ids['WF2'],[r['workflowId'] for r in self.monitor.history(dict(status='WAITING'))['runs']])
        self.assertEqual(row['scheduler']['id'],str(schedule['id']))
        self.complete(group,'WF1')
        while worker.step():pass
        self.assertEqual(len(self.store.status()['runs']),3)

    def test_scheduled_parent_waits_for_active_descendant_and_repeated_due_occurrences_coalesce(self):
        child=self.start('WF2',include_children=False);worker=DependencyWorker(self.store);worker.step()
        schedule=self.schedule('WF1');scheduler=Scheduler(self.store);scheduler.step();worker.step()
        with self.store.connect() as conn:
            conn.execute("UPDATE kumo_task_engine.schedules SET next_fire_at=next_fire_at-interval '1 minute' WHERE id=%s",(schedule['id'],))
        scheduler.step()
        self.assertEqual(len(self.graphs.status()['cascades']),2)
        self.complete(child,'WF2')
        while worker.step():pass
        row=next(w for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='WF1')
        self.assertEqual(row['run']['status'],'QUEUED')
        self.assertEqual(len(self.store.status()['runs']),2)

    def test_scheduled_child_preserves_failed_parent_rules_and_unknown_prevents_manual_overlap(self):
        parent=self.start('WF1',include_children=False);worker=DependencyWorker(self.store);worker.step()
        self.schedule('WF2');Scheduler(self.store).step();worker.step()
        self.complete(parent,'WF1','FAILED')
        while worker.step():pass
        row=next(w for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='WF2')
        self.assertEqual(row['run']['status'],'BLOCKED')
        parent=self.start('WF1',include_children=False);worker.step();self.complete(parent,'WF1','UNKNOWN')
        while worker.step():pass
        with self.assertRaises(WorkflowBusy):self.start('WF2',include_children=False)

    def test_middle_start_with_dependencies_uses_exact_other_parent_without_rerunning_ancestors(self):
        other=self.start('WF3',include_children=False);worker=DependencyWorker(self.store);worker.step()
        self.complete(other,'WF3')
        while worker.step():pass
        group=self.start('WF2');worker.step()
        detail=self.graphs.read(group);self.assertEqual(len(detail['nodes']),2)
        self.complete(group,'WF2')
        while worker.step():pass
        child=next(n for n in self.graphs.read(group)['nodes'] if n['node_key']==self.graph['definition']['workflowKeys'][self.ids['WF4']])
        self.assertIsNotNone(child['run_id'])
        self.assertEqual(len(child['parent_runs']),2)
        self.assertEqual(len(self.store.status()['runs']),3)

    def test_scheduled_grandparent_can_advance_alongside_a_nonadjacent_descendant(self):
        worker=DependencyWorker(self.store)
        other=self.start('WF3',include_children=False);worker.step();self.complete(other,'WF3')
        while worker.step():pass
        group=self.start('WF2');worker.step()
        self.schedule('WF1');Scheduler(self.store).step();worker.step()
        self.complete(group,'WF2')
        while worker.step():pass
        rows={w['WORKFLOW_NAME']:w for w in self.monitor.payload()['rows']}
        parent_key=self.graph['definition']['workflowKeys'][self.ids['WF1']]
        self.assertIsNotNone(next(n['run_id'] for n in self.graphs.read(rows['WF1']['cascadeId'])['nodes'] if n['node_key']==parent_key))
        old_child=next(n for n in self.graphs.read(group)['nodes'] if n['node_key']==self.graph['definition']['workflowKeys'][self.ids['WF4']])
        self.assertIsNotNone(old_child['run_id'])
        self.complete(group,'WF4')
        while worker.step():pass
        parent_key=self.graph['definition']['workflowKeys'][self.ids['WF1']]
        scheduled=self.graphs.read(rows['WF1']['cascadeId'])
        self.assertIsNotNone(next(n['run_id'] for n in scheduled['nodes'] if n['node_key']==parent_key))

    def test_cancelling_an_unadmitted_group_wakes_a_queued_ancestor_without_a_native_outcome(self):
        older=self.start('WF2',include_children=False)
        # Model an admission retry that is waiting for its backoff deadline.
        with self.store.connect() as conn:conn.execute("UPDATE kumo_task_engine.dependency_work SET next_check_at=clock_timestamp()+interval '1 minute' WHERE cascade_id=%s",(older,))
        self.schedule('WF1');Scheduler(self.store).step();worker=DependencyWorker(self.store);worker.step()
        self.assertEqual(len(self.store.status()['runs']),0)
        self.graphs.cancel(older)
        while worker.step():pass
        self.assertEqual(self.graphs.read(older)['cascade']['state'],'CANCELLED')
        self.assertEqual(len(self.store.status()['runs']),1)

    def test_disabling_a_workflow_pauses_its_schedule_and_pause_does_not_discard_accepted_work(self):
        schedule=self.schedule('Independent');Scheduler(self.store).step()
        self.config.workflow(self.ids['Independent'],dict(revision=1,enabled=False),'EDITOR')
        status=Schedules(self.store).status()
        self.assertEqual(status['enabledCount'],0)
        self.assertFalse(status['schedules'][0]['enabled']);self.assertIsNone(status['schedules'][0]['next_fire_at'])
        self.assertEqual(status['schedules'][0]['revision'],2)
        DependencyWorker(self.store).step()
        self.assertEqual(len(self.store.status()['runs']),1)
        self.assertEqual(str(status['occurrences'][0]['schedule_id']),str(schedule['id']))

    def test_competing_schedulers_admit_a_catalog_occurrence_once_and_audit_settings_conflicts(self):
        # Keep the next eligible occurrence well beyond the concurrent steps.
        schedule=self.schedule('Independent',cron='0 0 1 1 *')
        with ThreadPoolExecutor(max_workers=2) as pool:processed=list(pool.map(lambda _:Scheduler(self.store).step(),range(2)))
        self.assertEqual(sum(processed),1)
        self.assertEqual(len(Schedules(self.store).status()['occurrences']),1)
        self.assertEqual(len(self.graphs.status()['cascades']),1)
        body=dict(expectedRevision=1,enabled=False)
        Schedules(self.store).update(schedule['id'],body,'EDITOR')
        with self.assertRaises(Conflict):Schedules(self.store).update(schedule['id'],body,'STALE EDITOR')

    def test_independent_groups_run_concurrently_but_same_group_is_deduplicated(self):
        def submit(name):return self.start(name,request=name)
        with ThreadPoolExecutor(max_workers=2) as pool:groups=list(pool.map(submit,['WF1','Independent']))
        self.assertEqual(len(set(groups)),2)
        # Reapplying initialization must not recreate the superseded unique
        # graph-wide index after independent groups have been admitted.
        self.store.initialize();self.store.initialize()
        self.assertTrue(all(self.graphs.read(group)['cascade']['state']=='QUEUED' for group in groups))
        self.assertTrue(self.graphs.start(self.graph['id'],dict(requestId='WF1',workflowId=self.ids['WF1']),'RUNNER')['reused'])
        with self.assertRaises(Conflict):self.graphs.start(self.graph['id'],dict(requestId='WF1',workflowId=self.ids['Independent']),'RUNNER')

    def test_monitor_waits_for_parent_and_uses_one_execution_per_workflow(self):
        group=self.start();worker=DependencyWorker(self.store);worker.step()
        data=self.monitor.payload();by_name={w['WORKFLOW_NAME']:w for w in data['rows']}
        self.assertEqual(by_name['WF2']['run']['status'],'WAITING')
        self.assertEqual(by_name['WF1']['run']['status'],'QUEUED')
        self.assertTrue(all(w['canCancel'] for n,w in by_name.items() if n in ['WF1','WF2','WF3','WF4']))
        self.complete(group,'WF1');worker.step()
        by_name={w['WORKFLOW_NAME']:w for w in self.monitor.payload()['rows']}
        self.assertEqual(by_name['WF1']['run']['status'],'SUCCESS')
        self.assertEqual(by_name['WF4']['run']['status'],'WAITING')
        self.assertEqual(by_name['WF1']['jobs'][0]['run']['runId'],by_name['WF1']['run']['runId'])

    def test_native_running_status_and_times_are_not_invented(self):
        group=self.start();DependencyWorker(self.store).step()
        key=self.graph['definition']['workflowKeys'][self.ids['WF1']]
        node=next(n for n in self.graphs.read(group)['nodes'] if n['node_key']==key)
        with self.store.transaction() as conn:
            conn.execute("UPDATE kumo_task_engine.attempts SET state='SCHEDULED' WHERE run_id=%s",(node['run_id'],));self.store.refresh(conn,node['run_id'])
        root=next(w['run'] for w in self.monitor.payload()['rows'] if w['WORKFLOW_ID']==self.ids['WF1'])
        self.assertEqual(root['status'],'QUEUED');self.assertIsNone(root['start'])
        with self.store.transaction() as conn:
            conn.execute("UPDATE kumo_task_engine.attempts SET state='RUNNING',native_started_at=clock_timestamp() WHERE run_id=%s",(node['run_id'],));self.store.refresh(conn,node['run_id'])
        root=next(w['run'] for w in self.monitor.payload()['rows'] if w['WORKFLOW_ID']==self.ids['WF1'])
        self.assertEqual(root['status'],'RUNNING');self.assertIsNotNone(root['start'])

    def test_blocked_plan_keeps_previous_execution_jobs_progress_and_log(self):
        self.finish()
        before=next(w for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='WF4')
        group=self.start();worker=DependencyWorker(self.store);worker.step();self.complete(group,'WF1','FAILED')
        while worker.step():pass
        row=next(w for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='WF4')
        self.assertEqual(row['run']['status'],'BLOCKED')
        self.assertEqual(row['execution'],before['execution'])
        self.assertEqual(row['execution']['status'],'SUCCESS')
        self.assertEqual(row['jobs'],before['jobs']);self.assertEqual(row['progress'],before['progress'])
        self.assertEqual(row['planning']['status'],'BLOCKED');self.assertTrue(row['planning']['stopped'])
        self.assertEqual(row['planning']['waitingCount'],0)
        self.assertEqual(self.monitor.log(self.ids['WF4'],row['execution']['runId'])['history']['STATUS'],'SUCCESS')
        self.assertEqual(self.monitor.history(dict(workflowId=self.ids['WF4']))['runs'][0]['status'],'BLOCKED')

    def test_last_result_is_preserved_through_running_and_review_until_completion(self):
        self.finish()
        before=next(w for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='WF1')
        old=before['execution'];result=before['lastResult'];job_result=before['jobs'][0]['lastResult']
        self.assertEqual(result['status'],'SUCCESS')
        group=self.start(include_children=False);DependencyWorker(self.store).step()
        row=next(w for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='WF1')
        self.assertEqual(row['execution'],old);self.assertEqual(row['planning']['status'],'QUEUED')
        self.assertNotEqual(row['run']['runId'],old['runId'])
        native=self.graphs.read(group)['nodes'][0]['run_id']
        with self.store.transaction() as conn:
            conn.execute("UPDATE kumo_task_engine.attempts SET state='RUNNING',native_started_at=clock_timestamp() WHERE run_id=%s",(native,));self.store.refresh(conn,native)
        row=next(w for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='WF1')
        self.assertEqual(row['execution']['status'],'RUNNING');self.assertEqual(row['planning']['status'],'RUNNING')
        self.assertEqual(row['jobs'][0]['run']['runId'],row['execution']['runId'])
        self.assertEqual(row['execution']['runId'],row['run']['runId'])
        self.assertEqual(row['lastResult'],result);self.assertEqual(row['jobs'][0]['lastResult'],job_result)
        with self.store.transaction() as conn:
            conn.execute("UPDATE kumo_task_engine.attempts SET state='UNKNOWN' WHERE run_id=%s",(native,));self.store.refresh(conn,native)
        row=next(w for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='WF1')
        self.assertEqual(row['planning']['status'],'NEEDS REVIEW');self.assertEqual(row['lastResult'],result)
        self.complete(group,'WF1','FAILED');DependencyWorker(self.store).step()
        row=next(w for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='WF1')
        self.assertEqual(row['lastResult']['status'],'FAILED');self.assertEqual(row['lastResult']['runId'],row['run']['runId'])
        self.assertEqual(row['jobs'][0]['lastResult']['status'],'FAILED')

    def test_first_running_execution_has_no_result_until_it_finishes(self):
        group=self.start(include_children=False);DependencyWorker(self.store).step()
        native=self.graphs.read(group)['nodes'][0]['run_id']
        with self.store.transaction() as conn:
            conn.execute("UPDATE kumo_task_engine.attempts SET state='RUNNING',native_started_at=clock_timestamp() WHERE run_id=%s",(native,));self.store.refresh(conn,native)
        row=next(w for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='WF1')
        self.assertEqual(row['lastResult']['status'],'NO RESULT');self.assertEqual(row['planning']['status'],'RUNNING')
        self.assertEqual(row['jobs'][0]['lastResult']['status'],'NO RESULT')
        self.complete(group,'WF1');DependencyWorker(self.store).step()
        row=next(w for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='WF1')
        self.assertEqual(row['lastResult']['status'],'SUCCESS');self.assertEqual(row['planning']['status'],'NO PLAN')

    def test_cancellation_before_native_start_does_not_erase_a_completed_result(self):
        self.finish()
        before=next(w for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='WF1')
        group=self.start(include_children=False);DependencyWorker(self.store).step()
        native=self.graphs.read(group)['nodes'][0]['run_id']
        with self.store.transaction() as conn:
            conn.execute("UPDATE kumo_task_engine.attempts SET state='CANCELLED',ended_at=clock_timestamp() WHERE run_id=%s",(native,));self.store.refresh(conn,native)
        DependencyWorker(self.store).step()
        row=next(w for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='WF1')
        self.assertEqual(row['lastResult'],before['lastResult'])
        self.assertEqual(row['jobs'][0]['lastResult'],before['jobs'][0]['lastResult'])

    def test_job_result_updates_while_other_jobs_are_still_running(self):
        identity=self.ids['WF1'];second=str(uuid.uuid4())
        first=next(r['JOB_ID'] for r in self.raw['raw_records'] if r['WORKFLOW_ID']==identity and r.get('JOB_ID'))
        def add_job(raw):
            raw['jobs'].append(dict(JOB_ID=second,JOB_NAME='Second SQL',JOB_TYPE='SQL',SQL_COMMAND='SELECT 2',IS_ENABLED=True,TIMEOUT_SECONDS=60))
            raw['raw_records'].append(dict(WORKFLOW_ID=identity,JOB_ID=second,PARENT_ID=first,TRIGGER_TYPE='DEP',ALLOW=True))
        self.config.change(dict(revision=self.config.read()['revision']),'EDITOR',add_job)
        worker=DependencyWorker(self.store);old=self.start(include_children=False);worker.step()
        self.complete(old,'WF1');worker.step()
        before=next(w for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='WF1')
        new=self.start(include_children=False);worker.step();native=self.graphs.read(new)['nodes'][0]['run_id']
        with self.store.transaction() as conn:
            conn.execute("UPDATE kumo_task_engine.attempts SET state='SUCCESS',native_started_at=clock_timestamp(),ended_at=clock_timestamp() WHERE run_id=%s AND job_key=%s",(native,first))
            conn.execute("UPDATE kumo_task_engine.attempts SET state='RUNNING',native_started_at=clock_timestamp() WHERE run_id=%s AND job_key=%s",(native,second))
            self.store.refresh(conn,native)
        row=next(w for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='WF1')
        self.assertEqual(row['lastResult'],before['lastResult']);self.assertEqual(row['planning']['status'],'RUNNING')
        self.assertEqual(row['jobs'][0]['lastResult']['runId'],row['execution']['runId'])
        self.assertEqual(row['jobs'][1]['lastResult'],before['jobs'][1]['lastResult'])
        self.assertEqual(row['jobs'][1]['run']['status'],'RUNNING')

    def test_never_executed_blocked_plan_has_no_run_and_no_fake_job_results(self):
        group=self.start();worker=DependencyWorker(self.store);worker.step();self.complete(group,'WF1','FAILED')
        while worker.step():pass
        row=next(w for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='WF4')
        self.assertEqual(row['execution']['status'],'NO RUN');self.assertIsNone(row['execution']['runId'])
        self.assertEqual(row['planning']['status'],'BLOCKED')
        self.assertTrue(all(j['run']['status']=='NO RUN' and not j['run']['runId'] for j in row['jobs']))
        self.assertEqual(row['progress']['total'],0)

    def test_enabled_schedule_is_planning_state_and_does_not_invent_an_execution(self):
        self.schedule('Independent')
        row=next(w for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='Independent')
        self.assertEqual(row['execution']['status'],'NO RUN');self.assertEqual(row['planning']['status'],'SCHEDULED')
        self.assertTrue(row['planning']['scheduled']);self.assertEqual(row['planning']['nextRun'],row['scheduler']['nextRun'])
        self.assertFalse(self.store.status()['runs'])

    def test_finished_node_in_active_group_does_not_hide_a_newer_stopped_plan(self):
        self.add_wf10();worker=DependencyWorker(self.store)
        old=self.start();worker.step();self.complete(old,'WF1');worker.step()
        self.complete(old,'WF2');self.complete(old,'WF3');worker.step();self.complete(old,'WF4');worker.step()
        self.assertEqual(self.graphs.read(old)['cascade']['state'],'RUNNING')
        new=self.start();worker.step();self.complete(new,'WF1','FAILED')
        while worker.step():pass
        row=next(w for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='WF4')
        self.assertEqual(row['execution']['status'],'SUCCESS');self.assertEqual(row['execution']['cascadeId'],old)
        self.assertEqual(row['planning']['status'],'BLOCKED');self.assertEqual(row['planning']['cascadeId'],new)

    def test_pending_retry_preserves_failed_execution_and_restores_waiting_state(self):
        group=self.start();worker=DependencyWorker(self.store);worker.step();self.complete(group,'WF1','FAILED')
        while worker.step():pass
        failed=next(w for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='WF1')
        receipt=self.graphs.retry(group,failed['run']['runId'],'RETRY USER')
        rows={w['WORKFLOW_NAME']:w for w in self.monitor.payload()['rows']}
        self.assertEqual(rows['WF1']['execution'],failed['execution'])
        self.assertEqual(rows['WF1']['planning']['status'],'WAITING')
        self.assertEqual(rows['WF2']['execution']['status'],'NO RUN');self.assertEqual(rows['WF2']['planning']['status'],'WAITING')
        self.assertEqual(rows['WF2']['planning']['cascadeId'],receipt['cascadeId'])
        worker.step()
        row=next(w for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='WF1')
        self.assertEqual(row['execution']['status'],'FAILED');self.assertEqual(row['planning']['status'],'QUEUED')

    def test_history_is_bounded_paginated_and_logs_use_frozen_names(self):
        self.finish()
        first=self.monitor.history(dict(limit='2',days='30'))
        self.assertEqual(len(first['runs']),2);self.assertTrue(first['nextCursor'])
        second=self.monitor.history(dict(limit='2',cursor=first['nextCursor']))
        self.assertEqual(len(second['runs']),2)
        self.assertFalse({r['runId'] for r in first['runs']} & {r['runId'] for r in second['runs']})
        completed={r['workflowName']:r for r in first['runs']+second['runs']}
        self.assertEqual(set(completed['WF4']['parentExecutionIds']),{completed['WF2']['executionId'],completed['WF3']['executionId']})
        root=next(r for r in first['runs']+second['runs'] if r['workflowName']=='WF2')
        self.config.workflow(self.ids['WF2'],dict(revision=1,workflowName='Renamed workflow'),'EDITOR')
        log=self.monitor.log(root['workflowId'],root['runId'])
        self.assertEqual(log['history']['WORKFLOW_NAME'],'WF2');self.assertEqual(log['history']['STATUS'],'SUCCESS')
        self.assertTrue(log['jobExecutions']);self.assertTrue(log['runLog'])
        with self.assertRaises(LookupError):self.monitor.log(self.ids['Independent'],root['runId'])
        with self.assertRaises(LookupError):self.monitor.log(root['workflowId'],root['runId'],str(uuid.uuid4()))

    def test_failed_or_unknown_parent_blocks_dependents_and_remains_visible(self):
        group=self.start();worker=DependencyWorker(self.store);worker.step();self.complete(group,'WF1','FAILED');worker.step()
        data={w['WORKFLOW_NAME']:w for w in self.monitor.payload()['rows']}
        self.assertEqual(data['WF1']['run']['status'],'FAILED');self.assertEqual(data['WF4']['run']['status'],'BLOCKED')
        self.assertIn('Parent outcome',data['WF4']['run']['message'])

    def test_dispatch_error_is_visible_in_history_log_and_monitor_and_retry_preserves_finished_parent(self):
        group=self.start();worker=DependencyWorker(self.store);worker.step()
        parent=next(n for n in self.graphs.read(group)['nodes'] if n['node_key']==self.graph['definition']['workflowKeys'][self.ids['WF1']])
        self.complete(group,'WF1')
        with patch.object(self.store,'submit_in_transaction',side_effect=RuntimeError('Admission unavailable')):
            for _ in range(6):
                with self.store.connect() as conn:
                    conn.execute('UPDATE kumo_task_engine.dependency_work SET next_check_at=clock_timestamp() WHERE cascade_id=%s',(group,))
                worker.step()
        rows={r['WORKFLOW_NAME']:r for r in self.monitor.payload()['rows']}
        self.assertEqual(rows['WF1']['run']['status'],'SUCCESS')
        self.assertEqual(rows['WF1']['run']['message'],'')
        child=rows['WF2']['run']
        self.assertEqual(child['status'],'WAITING')
        self.assertIn('Dependency dispatch paused',child['message'])
        history=self.monitor.history(dict(workflowId=self.ids['WF2'],status='WAITING'))
        self.assertIn('Dependency dispatch paused',history['runs'][0]['message'])
        log=self.monitor.log(self.ids['WF2'],child['runId'])
        self.assertIn('Dependency dispatch paused',log['history']['ERROR_MESSAGE'])
        self.graphs.retry_dispatch(group)
        queued=self.monitor.payload()
        self.assertEqual(queued['dispatchErrors'],[])
        wf2=next(r for r in queued['rows'] if r['WORKFLOW_NAME']=='WF2')
        self.assertEqual(wf2['dispatch']['state'],'PENDING')
        self.assertFalse(wf2['dispatch']['canRetry'])
        self.assertNotIn('dispatch paused',wf2['run']['message'])
        worker.step()
        self.complete(group,'WF2');self.complete(group,'WF3');worker.step()
        self.complete(group,'WF4');worker.step()
        before={n['node_key']:n['run_id'] for n in self.graphs.read(group)['nodes']}
        self.assertEqual(before[parent['node_key']],parent['run_id'])
        self.assertEqual(self.graphs.read(group)['cascade']['state'],'SUCCESS')
        self.graphs.retry_dispatch(group);worker.step()
        self.assertEqual({n['node_key']:n['run_id'] for n in self.graphs.read(group)['nodes']},before)
        self.assertEqual(len(self.store.status()['runs']),4)

    def test_dispatch_retry_preserves_a_running_native_attempt(self):
        group=self.start();worker=DependencyWorker(self.store);worker.step()
        run=next(n['run_id'] for n in self.graphs.read(group)['nodes'] if n['node_key']==self.graph['definition']['workflowKeys'][self.ids['WF1']])
        with self.store.transaction() as conn:
            conn.execute("UPDATE kumo_task_engine.attempts SET state='RUNNING',native_started_at=clock_timestamp() WHERE run_id=%s",(run,))
            self.store.refresh(conn,run)
        self.graphs.retry_dispatch(group)
        with patch.object(worker,'evaluate',side_effect=RuntimeError('Dispatch unavailable')):
            for _ in range(6):
                with self.store.connect() as conn:
                    conn.execute('UPDATE kumo_task_engine.dependency_work SET next_check_at=clock_timestamp() WHERE cascade_id=%s',(group,))
                worker.step()
        row=next(r for r in self.monitor.payload()['rows'] if r['WORKFLOW_NAME']=='WF1')
        self.assertEqual(row['run']['status'],'RUNNING')
        before=self.store.read(run)['jobs']
        self.graphs.retry_dispatch(group);worker.step()
        after=self.store.read(run)['jobs']
        self.assertEqual([(a['id'],a['state']) for a in before],[(a['id'],a['state']) for a in after])
        self.assertEqual(after[0]['state'],'RUNNING')
        self.assertEqual(len(self.store.status()['runs']),1)

    def test_error_in_queued_group_is_visible_when_workflow_row_shows_older_running_instance(self):
        running=self.start('Independent',include_children=False);worker=DependencyWorker(self.store);worker.step()
        run=self.graphs.read(running)['nodes'][0]['run_id']
        with self.store.transaction() as conn:
            conn.execute("UPDATE kumo_task_engine.attempts SET state='RUNNING',native_started_at=clock_timestamp() WHERE run_id=%s",(run,))
            self.store.refresh(conn,run)
        self.schedule('Independent');Scheduler(self.store).step()
        queued=str(Schedules(self.store).status()['occurrences'][0]['cascade_id'])
        with self.store.connect() as conn:
            conn.execute("UPDATE kumo_task_engine.dependency_work SET state='ERROR',attempts=6,error='Dependency evaluation unavailable: RuntimeError',next_check_at=NULL WHERE cascade_id=%s",(queued,))
        data=self.monitor.payload()
        row=next(r for r in data['rows'] if r['WORKFLOW_NAME']=='Independent')
        self.assertEqual(row['cascadeId'],running)
        self.assertEqual(row['run']['status'],'RUNNING')
        self.assertEqual(row['dispatch']['state'],'DONE')
        self.assertEqual([e['cascadeId'] for e in data['dispatchErrors']],[queued])

    def test_terminal_group_errors_are_not_reported_as_active_dispatch_failures(self):
        group=self.start('WF1',include_children=False);worker=DependencyWorker(self.store);worker.step()
        self.complete(group,'WF1');worker.step()
        with self.store.connect() as conn:
            conn.execute("UPDATE kumo_task_engine.dependency_work SET state='ERROR',attempts=6,error='Historical dispatch error',next_check_at=NULL WHERE cascade_id=%s",(group,))
        data=self.monitor.payload()
        row=next(r for r in data['rows'] if r['WORKFLOW_NAME']=='WF1')
        self.assertEqual(data['dispatchErrors'],[])
        self.assertEqual(row['run']['status'],'SUCCESS')
        self.assertFalse(row['dispatch']['canRetry'])
        self.assertEqual(row['run']['message'],'')

    def test_configuration_edits_are_versioned_and_cannot_mutate_accepted_group(self):
        group=self.start();before=deepcopy(self.graphs.read(group)['cascade']['snapshot'])
        job=next(j for j in self.raw['jobs'] if j['JOB_NAME']=='WF2 SQL')
        self.config.job(job['JOB_ID'],dict(revision=1,sqlCommand='SELECT 99',enabled=False),'EDITOR')
        self.assertEqual(self.graphs.read(group)['cascade']['snapshot'],before)
        with self.assertRaises(Conflict):self.config.workflow(self.ids['WF1'],dict(revision=1,enabled=False),'STALE')
        self.assertEqual(self.config.read()['revision'],2)
        restarted=CatalogConfiguration(Store(self.store.settings)).read()
        self.assertFalse(next(j['IS_ENABLED'] for j in restarted['jobs'] if j['JOB_ID']==job['JOB_ID']))

    def test_editing_dbt_metadata_never_makes_it_executable(self):
        job=next(j for j in self.raw['jobs'] if j['JOB_TYPE']=='DBT')
        self.config.job(job['JOB_ID'],dict(revision=1,jobName='Updated dbt job',dbtCommand='dbt build --select model'),'EDITOR')
        row=next(w for w in self.monitor.payload()['rows'] if w['WORKFLOW_NAME']=='DBT workflow')
        self.assertFalse(row['canRun']);self.assertEqual(row['jobs'][0]['JOB_NAME'],'Updated dbt job')

    def test_editing_validated_sql_retains_the_execution_validation_guard(self):
        job=next(j for j in self.raw['jobs'] if j['JOB_NAME']=='WF2 SQL')
        for change in [dict(sqlCommand='DROP TABLE IMPORTANT'),dict(timeoutSeconds=301),dict(jobType='DBT')]:
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.config.job(job['JOB_ID'],dict(revision=1,**change),'INVALID')
        self.assertEqual(self.config.read()['revision'],1)

    def test_new_component_edges_cannot_overlap_an_active_older_group(self):
        group=self.start()
        data=self.config.read();row=next(r for r in data['rows'] if not r['jobId'] and r['workflowId']==self.ids['Independent'])
        row.update(parentId=self.ids['WF4'],trigger='DEP',success=True,warning=False,error=False,parentMustBeNewer=True)
        self.config.dependencies(dict(revision=1,rows=data['rows']),'EDITOR')
        with self.assertRaises(WorkflowBusy):self.start()
        self.assertEqual(len(self.graphs.read(group)['nodes']),4)

    def test_shared_worker_runs_without_clients_and_terminal_work_stops_snowflake_polling(self):
        self.start('Independent');dispatch=DependencyWorker(self.store);fake=FakeTasks(self.store);worker=Worker(self.store,fake)
        dispatch.step();worker.step()
        for native in fake.native.values():native['state']='SUCCEEDED'
        worker.step();worker.step();dispatch.step()
        before=(len(fake.executions),len(fake.polls),len(fake.drops))
        import snowflake_client
        with patch.object(snowflake_client,'connection',side_effect=AssertionError('Monitor must read PostgreSQL only')):
            for _ in range(3):
                self.monitor.payload();self.monitor.history({});self.assertFalse(worker.step());dispatch.step()
        self.assertEqual(before,(len(fake.executions),len(fake.polls),len(fake.drops)))
        self.assertEqual(before,(1,1,1))


if __name__=='__main__':unittest.main()
