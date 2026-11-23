"""Workflow dependency policy, targeted import and real PostgreSQL event recovery."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import os
from pathlib import Path
import sys
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from task_engine.dependencies import Graphs,DependencyWorker,accepts,graph_definition
from task_engine.settings import Settings
from task_engine.store import Store,Conflict,WorkflowBusy
from task_engine.wf1 import snapshot


def diamond():
    return dict(key='trial',name='SQL diamond',enabled=True,nodes=[
        dict(key=key,plan=dict(jobs=[dict(key='sql',sql='SELECT 1')]),parents=[dict(key=p) for p in parents])
        for key,parents in [('WF1',[]),('WF2',['WF1']),('WF3',['WF1']),('WF4',['WF2','WF3'])]])


def source():
    ids={key:str(uuid.uuid5(uuid.NAMESPACE_URL,key)) for key in ['WF1','WF2','WF3','WF4']}
    workflows=[];jobs=[];rules=[]
    for name,parents in [('WF1',[]),('WF2',['WF1']),('WF3',['WF1']),('WF4',['WF2','WF3'])]:
        job=str(uuid.uuid5(uuid.NAMESPACE_URL,name+'-job'));workflow=ids[name]
        workflows.append(dict(WORKFLOW_ID=workflow,WORKFLOW_NAME=name,WORKFLOW_GROUP='DEPENDENCY TEST',IS_ENABLED=True,IS_SCHEDULE_ENABLED=False,JOB_EXECUTION_MODE='SEQUENTIAL'))
        jobs.append(dict(JOB_ID=job,JOB_NAME=name+' SQL',JOB_TYPE='SQL',IS_ENABLED=True,SQL_COMMAND="SELECT '"+name+"', SYSTEM$WAIT(20, 'SECONDS')",TIMEOUT_SECONDS=180))
        rules.append(dict(WORKFLOW_ID=workflow,JOB_ID=job,PARENT_ID=workflow,TRIGGER_TYPE='DEP',SUCCESS=None,WARNING=None,ERROR=None,SQL=None,ALLOW=True,PARENT_MUST_BE_NEWER=False))
        for p in parents or [None]:
            rules.append(dict(WORKFLOW_ID=workflow,JOB_ID=None,PARENT_ID=ids[p] if p else None,TRIGGER_TYPE='DEP' if p else None,
                              SUCCESS=True if p else None,WARNING=False if p else None,ERROR=False if p else None,SQL=None,ALLOW=True,PARENT_MUST_BE_NEWER=p!='WF3'))
    return dict(workflows=workflows,jobs=jobs,rules=rules)


class PolicyTests(unittest.TestCase):
    def test_status_flags_preserve_true_false_and_null_semantics(self):
        rule=dict(allow=True,success=True,warning=False,error=False)
        for state,field in [('SUCCESS','success'),('WARNING','warning'),('FAILED','error')]:
            for value in [True,False,None]:
                self.assertEqual(accepts(state,dict(rule,**{field:value})),value is not False)
        for state in ['UNKNOWN','QUEUED','RUNNING','CANCELLED','SKIPPED','BLOCKED']:
            self.assertFalse(accepts(state,dict(allow=True,success=None,warning=None,error=None)))
        self.assertFalse(accepts('SUCCESS',dict(rule,allow=False)))

    def test_invalid_cycles_references_duplicates_and_business_sql_are_rejected(self):
        base=diamond()
        cases=[]
        bad=deepcopy(base);bad['nodes'][0]['parents']=[dict(key='WF4')];cases.append(bad)
        bad=deepcopy(base);bad['nodes'][1]['parents']=[dict(key='missing')];cases.append(bad)
        bad=deepcopy(base);bad['nodes'][1]['parents']*=2;cases.append(bad)
        bad=deepcopy(base);bad['nodes'][0]['plan']['jobs'][0]['sql']='SELECT * FROM SECRET';cases.append(bad)
        bad=deepcopy(base);bad['nodes'][1]['parents'][0]['success']='true';cases.append(bad)
        for body in cases:
            with self.subTest(body=body),self.assertRaises(ValueError):graph_definition(body)

    def test_import_keeps_source_sql_ids_and_mixed_parent_freshness(self):
        data=source();body,metadata=snapshot(data);frozen=graph_definition(body)
        self.assertEqual([n['key'] for n in frozen['nodes']],['WF1','WF2','WF3','WF4'])
        rules=frozen['nodes'][-1]['parents']
        self.assertEqual([(r['key'],r['parentMustBeNewer']) for r in rules],[('WF2',True),('WF3',False)])
        self.assertEqual(body['nodes'][0]['plan']['jobs'][0]['sql'],data['jobs'][0]['SQL_COMMAND'])
        self.assertEqual(len(metadata['workflows']),4)

    def test_import_refuses_sql_conditions_partial_graph_or_disabled_jobs(self):
        for change in ['condition','disabled','external_parent','dbt','gate']:
            data=source()
            if change=='condition':data['rules'][1]['SQL']='SELECT TRUE'
            if change=='disabled':data['jobs'][0]['IS_ENABLED']=False
            if change=='external_parent':data['rules'][3]['PARENT_ID']='outside'
            if change=='dbt':data['jobs'][0]['JOB_TYPE']='DBT'
            if change=='gate':data['rules'][0]['SUCCESS']=True
            with self.subTest(change=change),self.assertRaises(ValueError):snapshot(data)


@unittest.skipUnless(os.getenv('KUMO_TASK_ENGINE_TEST_DSN'),'Requires isolated PostgreSQL test database')
class DependencyPostgresTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(Settings(os.environ['KUMO_TASK_ENGINE_TEST_DSN']))
        with self.store.connect() as conn:
            assert conn.execute('SELECT current_database() AS name').fetchone()['name'].endswith('_test')
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute('TRUNCATE kumo_task_engine.dependency_graphs,kumo_task_engine.dependency_workers,kumo_task_engine.schedules,kumo_task_engine.scheduler_workers,kumo_task_engine.runs,kumo_task_engine.events,kumo_task_engine.attempts RESTART IDENTITY CASCADE')
        self.graphs=Graphs(self.store);self.dispatch=DependencyWorker(self.store)
        self.body=diamond();self.graph=self.graphs.install(self.body,'TRUSTED')['graph']
        self.cascade=self.graphs.start(self.graph['id'],dict(requestId='root-request'),'TRUSTED')['cascadeId']

    def read(self):return self.graphs.read(self.cascade)
    def nodes(self):return {n['node_key']:n for n in self.read()['nodes']}
    def states(self):return {k:n['state'] for k,n in self.nodes().items()}
    def due(self):
        with self.store.connect() as conn:conn.execute("UPDATE kumo_task_engine.dependency_work SET next_check_at=clock_timestamp() WHERE state='PENDING'")
    def tick(self):self.due();return self.dispatch.step()
    def complete(self,key,state='SUCCESS',ended_at=None,never_started=False):
        run=self.nodes()[key]['run_id'];now=datetime.now(timezone.utc)
        with self.store.transaction() as conn:
            conn.execute('''UPDATE kumo_task_engine.attempts SET state=%s,native_started_at=%s,ended_at=%s,next_check_at=NULL
                WHERE run_id=%s''',(state,None if never_started else (ended_at or now)-timedelta(seconds=1),None if state=='UNKNOWN' else ended_at or now,run))
            self.store.refresh(conn,run)
        return run
    def finish_all(self):
        self.tick();self.complete('WF1');self.tick();self.complete('WF2');self.complete('WF3');self.tick();self.complete('WF4');self.tick()

    def test_full_diamond_waits_for_both_parents_and_pins_exact_executions(self):
        self.tick();self.assertEqual(self.states(),dict(WF1='ADMITTED',WF2='WAITING',WF3='WAITING',WF4='WAITING'))
        root=self.complete('WF1');self.tick()
        self.assertEqual(self.states(),dict(WF1='SUCCESS',WF2='ADMITTED',WF3='ADMITTED',WF4='WAITING'))
        a=self.complete('WF2');self.tick();self.assertEqual(self.states()['WF4'],'WAITING')
        self.assertEqual(self.nodes()['WF4']['blockers'][0]['parent'],'WF3')
        b=self.complete('WF3');self.tick()
        self.assertEqual(self.nodes()['WF4']['parent_runs'],dict(WF2=str(a),WF3=str(b)))
        self.complete('WF4');self.tick();self.assertEqual(self.read()['cascade']['state'],'SUCCESS')
        self.assertFalse(self.dispatch.step())
        self.assertEqual(len(self.store.status()['runs']),4)

    def test_root_request_retries_and_duplicate_completion_delivery_do_not_duplicate_runs(self):
        self.assertTrue(self.graphs.start(self.graph['id'],dict(requestId='root-request'),'OTHER')['reused'])
        with self.assertRaises(WorkflowBusy):self.graphs.start(self.graph['id'],dict(requestId='other'),'USER')
        self.tick();root=self.complete('WF1');self.tick()
        before={k:n['run_id'] for k,n in self.nodes().items()}
        with self.store.transaction() as conn:self.store.queue_dependency_work(conn,root)
        self.tick();self.assertEqual(before,{k:n['run_id'] for k,n in self.nodes().items()})
        self.assertEqual(len(self.store.status()['runs']),3)

    def test_sibling_admission_failure_rolls_back_both_and_preserves_parent_success(self):
        self.tick();root=self.complete('WF1');original=self.store.submit_in_transaction
        def fail(conn,body,actor,**kwargs):
            result=original(conn,body,actor,**kwargs)
            if body['workflowKey'].endswith('WF3'):raise RuntimeError('Injected interruption between siblings')
            return result
        with patch.object(self.store,'submit_in_transaction',side_effect=fail):self.tick()
        self.assertEqual(self.store.read(root)['run']['state'],'SUCCESS')
        self.assertIsNone(self.nodes()['WF2']['run_id']);self.assertIsNone(self.nodes()['WF3']['run_id'])
        self.assertEqual(self.read()['dispatch']['state'],'PENDING')
        self.dispatch=DependencyWorker(self.store);self.tick()
        self.assertEqual(self.states()['WF2'],'ADMITTED');self.assertEqual(self.states()['WF3'],'ADMITTED')

    def test_competing_dispatchers_admit_each_node_once(self):
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:DependencyWorker(self.store).step(),range(4)))
        self.assertEqual(sum(results),1);self.assertEqual(len(self.store.status()['runs']),1)
        self.complete('WF1')
        with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(lambda _:DependencyWorker(self.store).step(),range(4)))
        self.assertEqual(len(self.store.status()['runs']),3)

    def test_failed_or_cancelled_parent_never_uses_older_success(self):
        for state in ['FAILED','CANCELLED']:
            # Use a separate graph for each outcome; old history cannot satisfy a new trial.
            body=diamond();body['key']='outcome-'+state
            graph=self.graphs.install(body,'U')['graph'];self.cascade=self.graphs.start(graph['id'],dict(requestId=state),'U')['cascadeId']
            while self.states()['WF1']=='WAITING':self.tick()
            self.complete('WF1',state);self.tick()
            self.assertEqual(self.states()['WF2'],'BLOCKED');self.assertEqual(self.states()['WF4'],'BLOCKED')
            self.assertEqual(self.read()['cascade']['state'],'FAILED')

    def test_unknown_parent_stops_descendants_and_new_graph_runs(self):
        self.tick();self.complete('WF1','UNKNOWN');self.tick()
        self.assertEqual(self.read()['cascade']['state'],'UNKNOWN')
        self.assertEqual(self.states()['WF2'],'WAITING')
        self.assertIn('review',self.nodes()['WF2']['blockers'][0]['reason'])
        self.assertFalse(self.dispatch.step())
        with self.assertRaises(WorkflowBusy):self.graphs.start(self.graph['id'],dict(requestId='again'),'U')

    def test_error_outcome_can_release_only_a_rule_that_explicitly_accepts_it(self):
        body=diamond();body['key']='error-rule';body['nodes'][1]['parents'][0]['error']=True
        graph=self.graphs.install(body,'U')['graph'];self.cascade=self.graphs.start(graph['id'],dict(requestId='error-rule'),'U')['cascadeId']
        self.tick();self.tick();self.complete('WF1','FAILED');self.tick()
        self.assertEqual(self.states()['WF2'],'ADMITTED');self.assertEqual(self.states()['WF3'],'BLOCKED')

    def test_disabled_and_disallowed_nodes_do_not_admit_work(self):
        for flag in ['enabled','allow']:
            body=diamond();body['key']='disabled-'+flag;body['nodes'][1][flag]=False
            graph=self.graphs.install(body,'U')['graph'];self.cascade=self.graphs.start(graph['id'],dict(requestId=flag),'U')['cascadeId']
            while self.states()['WF1']=='WAITING':self.tick()
            self.complete('WF1');self.tick()
            self.assertEqual(self.states()['WF2'],'SKIPPED');self.assertIsNone(self.nodes()['WF2']['run_id'])
            self.assertEqual(self.states()['WF3'],'ADMITTED');self.assertEqual(self.states()['WF4'],'BLOCKED')

    def test_latest_current_trial_parents_are_used_instead_of_previous_trial_success(self):
        self.finish_all();old=self.nodes()['WF3']['run_id']
        self.cascade=self.graphs.start(self.graph['id'],dict(requestId='second-trial'),'U')['cascadeId']
        self.tick();self.complete('WF1');self.tick();self.complete('WF2');self.tick()
        self.assertEqual(self.states()['WF4'],'WAITING')
        self.assertNotEqual(self.nodes()['WF3']['run_id'],old)
        self.assertNotEqual(self.nodes()['WF4']['parent_runs'].get('WF3'),str(old))

    def test_freshness_uses_last_actual_child_start_and_preserves_per_parent_choice(self):
        body=diamond();body['key']='fresh';body['nodes'][-1]['parents'][0]['parentMustBeNewer']=True
        graph=self.graphs.install(body,'U')['graph'];self.cascade=self.graphs.start(graph['id'],dict(requestId='fresh'),'U')['cascadeId']
        child_key=self.nodes()['WF4']['workflow_key']
        with self.store.transaction() as conn:
            old=self.store.submit_in_transaction(conn,dict(requestId='historical-child',workflowKey=child_key,jobs=[dict(sql='SELECT 1')]),'HISTORY',dependency_admission=True)
            conn.execute("UPDATE kumo_task_engine.attempts SET state='SUCCESS',native_started_at=clock_timestamp()-interval '2 seconds',ended_at=clock_timestamp(),next_check_at=NULL WHERE run_id=%s",(uuid.UUID(old['runId']),))
            self.store.refresh(conn,uuid.UUID(old['runId']))
        while self.states()['WF1']=='WAITING':self.tick()
        self.complete('WF1');self.tick()
        stale=datetime.now(timezone.utc)-timedelta(seconds=10)
        self.complete('WF2',ended_at=stale);self.complete('WF3',ended_at=stale);self.tick()
        self.assertEqual(self.states()['WF4'],'BLOCKED')
        self.assertEqual([b['parent'] for b in self.nodes()['WF4']['blockers']],['WF2'])

    def test_revision_changes_do_not_rewrite_an_accepted_trial(self):
        body=diamond();body['nodes'][-1]['plan']['jobs'][0]['sql']='SELECT 99'
        updated=self.graphs.install(body,'EDITOR',expected_revision=1)['graph']
        self.assertEqual(updated['revision'],2)
        self.assertEqual(self.read()['cascade']['snapshot']['nodes'][-1]['plan']['jobs'][0]['sql'],'SELECT 1')
        with self.assertRaises(Conflict):self.graphs.install(diamond(),'OTHER',expected_revision=1)
        self.finish_all()
        self.cascade=self.graphs.start(self.graph['id'],dict(requestId='revision-two'),'U')['cascadeId']
        self.assertEqual(self.read()['cascade']['graph_revision'],2)

    def test_evaluation_errors_are_bounded_and_safe_to_retry_without_parent_reexecution(self):
        self.tick();root=self.complete('WF1')
        with patch.object(self.dispatch,'evaluate',side_effect=RuntimeError('Unavailable')):
            for _ in range(6):self.tick()
        self.assertEqual(self.read()['dispatch']['state'],'ERROR')
        self.assertEqual(self.store.read(root)['run']['state'],'SUCCESS')
        self.assertFalse(self.dispatch.step())
        self.graphs.retry_dispatch(self.cascade);self.tick()
        self.assertEqual(self.nodes()['WF1']['run_id'],root)
        self.assertEqual(self.states()['WF2'],'ADMITTED')

    def test_cancellation_before_admission_creates_no_run(self):
        self.graphs.cancel(self.cascade);self.tick()
        self.assertEqual(set(self.states().values()),{'CANCELLED'})
        self.assertEqual(self.read()['cascade']['state'],'CANCELLED')
        self.assertFalse(self.store.status()['runs'])

    def test_cancellation_waits_for_native_outcome_and_prevents_child_admission(self):
        self.tick();root=self.nodes()['WF1']['run_id']
        # Model a native attempt already submitted, so queued cancellation cannot finish it.
        with self.store.connect() as conn:conn.execute("UPDATE kumo_task_engine.attempts SET state='SUBMITTED' WHERE run_id=%s",(root,))
        self.graphs.cancel(self.cascade);self.tick()
        self.assertEqual(self.read()['cascade']['state'],'RUNNING')
        self.assertEqual(self.states()['WF2'],'CANCELLED')
        self.complete('WF1','CANCELLED');self.tick()
        self.assertEqual(self.read()['cascade']['state'],'CANCELLED')

    def test_parent_outcome_and_pending_dependency_event_are_one_transaction(self):
        self.tick();root=self.nodes()['WF1']['run_id']
        with patch.object(self.store,'queue_dependency_work',side_effect=RuntimeError('Outbox unavailable')),self.assertRaises(RuntimeError):
            self.complete('WF1')
        self.assertEqual(self.store.read(root)['run']['state'],'QUEUED')
        self.assertFalse(any(e['kind']=='FINISHED' for e in self.store.read(root)['events']))
        self.complete('WF1');self.assertEqual(self.read()['dispatch']['state'],'PENDING')

    def test_partial_runs_reserved_keys_and_wrong_request_scope_are_rejected(self):
        with self.assertRaises(ValueError):self.graphs.start(self.graph['id'],dict(requestId='partial',mode='JOB_ONLY'),'U')
        with self.assertRaises(ValueError):self.store.submit(dict(requestId='bypass',workflowKey=self.nodes()['WF1']['workflow_key'],jobs=[dict(sql='SELECT 1')]),'U')

    def test_idle_waiting_and_disabled_dispatch_never_contacts_snowflake(self):
        import snowflake_client
        with patch.object(snowflake_client,'connection',side_effect=AssertionError('No Snowflake')):
            self.assertFalse(self.dispatch.step(execution_enabled=False))
            self.assertEqual(self.graphs.status()['dispatcher']['state'],'EXECUTION_DISABLED')
            self.tick()
            for _ in range(4):self.assertFalse(self.dispatch.step())
        self.assertIsNone(self.nodes()['WF2']['run_id'])


if __name__=='__main__':unittest.main()
