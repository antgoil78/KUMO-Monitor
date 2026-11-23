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

    def start(self,name='WF1',request=None):
        return self.graphs.start(self.graph['id'],dict(requestId=request or str(uuid.uuid4()),workflowId=self.ids[name]),'RUNNER')['cascadeId']

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

    def test_only_root_can_start_the_full_supported_component(self):
        with self.assertRaises(ValueError):self.graphs.start(self.graph['id'],dict(requestId='malformed',workflowId=[]),'RUNNER')
        with self.assertRaises(Conflict):self.start('WF2')
        with self.assertRaises(Conflict):self.start('DBT workflow')
        group=self.start();detail=self.graphs.read(group)
        self.assertEqual(len(detail['nodes']),4)
        self.assertEqual(set(detail['cascade']['snapshot']['workflowIds'].values()),{self.ids[k] for k in ['WF1','WF2','WF3','WF4']})
        self.assertNotIn('catalog',detail['cascade']['snapshot'])
        with self.assertRaises(WorkflowBusy):self.start()

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
