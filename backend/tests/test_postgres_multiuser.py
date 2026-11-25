"""Thirty concurrent HTTP users against the maintained app and isolated PG.

Only Snowflake identity resolution is simulated. Flask middleware, per-client
identity binding, HTTP routes, SSE broker, PG admission and monitor reads are real.
The server uses an ephemeral loopback port; it never targets the DEV API.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import sys
from threading import Barrier, Event, Lock, Thread
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from task_engine.catalog import import_catalog
from task_engine.dependencies import DependencyWorker, Graphs
from task_engine.settings import Settings
from task_engine.store import Store
import test_postgres_dependency_scenarios as scenarios

USERS=30
PREFIX='/api/orchestration/task-engine'


def sized_catalog():
    raw=scenarios.full_catalog()
    for i in range(14):
        identity,job=str(uuid.uuid4()),str(uuid.uuid4())
        name=f'Independent {i+2}'
        raw['workflows'].append(dict(WORKFLOW_ID=identity,WORKFLOW_NAME=name,
            IS_ENABLED=True,IS_SCHEDULE_ENABLED=False,JOB_EXECUTION_MODE='SEQUENTIAL'))
        raw['jobs'].append(dict(JOB_ID=job,JOB_NAME=name+' SQL',JOB_TYPE='SQL',
            SQL_COMMAND='SELECT 1',IS_ENABLED=True,TIMEOUT_SECONDS=60))
        raw['raw_records'].extend([
            dict(WORKFLOW_ID=identity,JOB_ID=None,PARENT_ID=None,TRIGGER_TYPE=None,ALLOW=True),
            dict(WORKFLOW_ID=identity,JOB_ID=job,PARENT_ID=identity,TRIGGER_TYPE='DEP',ALLOW=True)])
    for wf in [w for w in raw['workflows'] if w['WORKFLOW_NAME']!='DBT workflow'][:24]:
        identity=wf['WORKFLOW_ID']
        parent=next(r['JOB_ID'] for r in raw['raw_records'] if r['WORKFLOW_ID']==identity and r.get('JOB_ID'))
        job=str(uuid.uuid4())
        raw['jobs'].append(dict(JOB_ID=job,JOB_NAME=wf['WORKFLOW_NAME']+' second SQL',
            JOB_TYPE='SQL',SQL_COMMAND='SELECT 2',IS_ENABLED=True,TIMEOUT_SECONDS=60))
        raw['raw_records'].append(dict(WORKFLOW_ID=identity,JOB_ID=job,PARENT_ID=parent,TRIGGER_TYPE='DEP',ALLOW=True))
    assert len(raw['workflows'])==26 and len(raw['jobs'])==51
    return raw


class ObservedStore(Store):
    def __init__(self,settings):
        super().__init__(settings)
        self.lock=Lock();self.open_connections=0;self.peak_connections=0;self.connections=0

    @contextmanager
    def connect(self):
        connection=super().connect()
        with self.lock:
            self.open_connections+=1;self.connections+=1
            self.peak_connections=max(self.peak_connections,self.open_connections)
        try:
            with connection as conn:
                yield conn
        finally:
            with self.lock:self.open_connections-=1


def latencies(values):
    ordered=sorted(values)
    return dict(count=len(values),**{name:round(ordered[min(len(ordered)-1,math.ceil(q*len(ordered))-1)]*1000,2)
        for name,q in [('p50Ms',.5),('p95Ms',.95),('p99Ms',.99),('maxMs',1)]}) if values else dict(count=0)


@unittest.skipUnless(os.getenv('KUMO_TASK_ENGINE_TEST_DSN'),'Requires isolated PostgreSQL test database')
class MultiuserTests(unittest.TestCase):
    def setUp(self):
        import app as application
        from task_engine import integration
        from realtime_events import RealtimeEventBroker
        from werkzeug.serving import make_server
        self.application=application
        self.store=ObservedStore(Settings(os.environ['KUMO_TASK_ENGINE_TEST_DSN']))
        with self.store.connect() as conn:
            assert conn.execute('SELECT current_database() AS name').fetchone()['name'].endswith('_test')
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute('TRUNCATE kumo_task_engine.dependency_decisions,kumo_task_engine.dependency_graphs,'
                'kumo_task_engine.dependency_workers,kumo_task_engine.schedules,kumo_task_engine.scheduler_workers,'
                'kumo_task_engine.runs RESTART IDENTITY CASCADE')
        self.raw=sized_catalog();self.graph=import_catalog(self.store,self.raw,'MULTIUSER FIXTURE')
        self.ids={w['WORKFLOW_NAME']:w['WORKFLOW_ID'] for w in self.raw['workflows']}
        self.graphs=Graphs(self.store);self.worker=DependencyWorker(self.store)
        self.broker=RealtimeEventBroker()
        self.broker.set_client_count_callback(application._set_realtime_client_count)
        def identity():
            token=application.sf._ingress_user_token.get()
            index=int(token.removeprefix('multiuser-token-'))
            return dict(ok=True,userName=f'USER_{index:02}',displayName=f'Test user {index:02}',
                roleName='TEST_ROLE',mode='caller-rights',callerRightsActive=True,callerTokenPresent=True)
        self.no_snowflake=patch.object(application.sf,'connection',side_effect=AssertionError('No Snowflake in multiuser tests'))
        self.no_bootstrap=patch.object(application,'_ensure_environment',side_effect=AssertionError('No Snowflake bootstrap'))
        for patcher in [
            patch.object(integration,'store',return_value=self.store),
            patch.dict(os.environ,{'KUMO_TASK_PROTOTYPE_ENABLED':'true','KUMO_ORCHESTRATION_RUNS_ENABLED':'true','KUMO_ENV':'DEV'}),
            patch.object(application.config,'ENVIRONMENT','DEV'),patch.object(application.config,'USE_MOCK',False),
            patch.object(application.sf,'is_configured',return_value=True),patch.object(application.sf,'session_context',side_effect=identity),
            patch.object(application,'_client_actors',{}),patch.object(application,'_session_cache',{}),
            patch.object(application,'_active_users',{}),patch.object(application,'realtime_broker',self.broker),
        ]:
            patcher.start();self.addCleanup(patcher.stop)
        self.sf_connection=self.no_snowflake.start();self.addCleanup(self.no_snowflake.stop)
        self.bootstrap=self.no_bootstrap.start();self.addCleanup(self.no_bootstrap.stop)
        self.readers=[]
        for owner,method in [(application.monitor_cache,'start'),(application.orchestration_cache,'start'),
                             (application,'_start_dashboard_cache')]:
            patcher=patch.object(owner,method)
            self.readers.append(patcher.start());self.addCleanup(patcher.stop)
        self.server=make_server('127.0.0.1',0,application.app,threaded=True)
        self.server_thread=Thread(target=self.server.serve_forever,daemon=True)
        self.server_thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        self.streams=[]
        self.addCleanup(self.stop_server)
        sessions=self.race(lambda i:self.http(i,'/api/session'))
        self.assertEqual({body['userName'] for status,body,_ in sessions},{f'USER_{i:02}' for i in range(USERS)})
        self.assertTrue(all(status==200 for status,_,_ in sessions))

    def tearDown(self):
        self.sf_connection.assert_not_called();self.bootstrap.assert_not_called()
        for reader in self.readers:reader.assert_not_called()

    def headers(self,index):
        return {'X-Kumo-Client-Id':f'multiuser-client-{index:02}',
                'Sf-Context-Current-User-Token':f'multiuser-token-{index:02}'}

    def http(self,index,path,method='GET',body=None):
        headers=self.headers(index)
        if body is not None:headers['Content-Type']='application/json'
        request=Request(self.base+path,headers=headers,method=method,
            data=json.dumps(body).encode() if body is not None else None)
        started=time.monotonic()
        try:response=urlopen(request,timeout=10)
        except HTTPError as exc:response=exc
        with response:
            return response.status,json.load(response),time.monotonic()-started

    def race(self,action):
        barrier=Barrier(USERS)
        def perform(i):
            barrier.wait(timeout=10)
            return action(i)
        with ThreadPoolExecutor(max_workers=USERS) as pool:
            return list(pool.map(perform,range(USERS)))

    def start(self,name,children=False,request=None):
        return self.graphs.start(self.graph['id'],dict(requestId=request or str(uuid.uuid4()),
            workflowId=self.ids[name],includeChildren=children),'TEST DRIVER')['cascadeId']

    def start_request(self,index,name='WF1',request=None):
        return self.http(index,PREFIX+f"/graphs/{self.graph['id']}/runs",'POST',
            dict(requestId=request or f'user-{index}',workflowId=self.ids[name],includeChildren=False))

    def drain(self):
        for _ in range(100):
            if not self.worker.step():return
        self.fail('Dependency worker failed to become idle')

    def outcome(self,run,state='SUCCESS'):
        with self.store.transaction() as conn:
            conn.execute('''UPDATE kumo_task_engine.attempts SET state=%s,
                native_started_at=COALESCE(native_started_at,clock_timestamp()-interval '1 second'),
                ended_at=clock_timestamp(),next_check_at=NULL WHERE run_id=%s''',(state,run))
            self.store.refresh(conn,run)

    def finish(self,group):
        for _ in range(30):
            self.drain();data=self.graphs.read(group)
            if data['cascade']['state']=='SUCCESS':return
            runnable=[n for n in data['nodes'] if n['run_state'] in ('QUEUED','RUNNING')]
            self.assertTrue(runnable,'Group stalled with no admitted workflow')
            for node in runnable:self.outcome(node['run_id'])
        self.fail('Group did not finish')

    def open_presence(self):
        def connect(i):
            # EventSource reconnects can lack the ingress token. The registered
            # per-tab actor must still resolve without another identity query.
            response=urlopen(Request(self.base+f'/api/events?orchestrationSource=POSTGRES&clientId=multiuser-client-{i:02}&page=orchestrationMonitor',
                headers={'X-Kumo-Client-Id':f'multiuser-client-{i:02}'}),timeout=10)
            while True:
                line=response.readline()
                if not line:raise AssertionError('Realtime stream closed before connected event')
                if line.startswith(b'data:'):
                    self.assertEqual(json.loads(line[5:])['type'],'connected')
                    break
            return response
        self.streams=self.race(connect)
        self.assertEqual(self.broker.client_count(),USERS)
        status,presence,_=self.http(0,'/api/users/active')
        self.assertEqual(status,200);self.assertEqual(presence['count'],USERS)
        self.assertEqual({u['userName'] for u in presence['users']},{f'USER_{i:02}' for i in range(USERS)})
        self.assertEqual(self.store.open_connections,0,'SSE must not hold PostgreSQL connections')

    def close_presence(self):
        for stream in self.streams:stream.close()
        self.streams=[]
        deadline=time.monotonic()+5
        while self.broker.client_count() and time.monotonic()<deadline:
            # Wake socket writers after client close; do not wait for the normal
            # 15-second heartbeat to detect disconnected test streams.
            self.broker.publish('test_cleanup',{})
            Event().wait(.05)
        self.assertEqual(self.broker.client_count(),0)

    def stop_server(self):
        self.close_presence()
        self.server.shutdown();self.server.server_close();self.server_thread.join(5)

    def test_30_users_starting_one_workflow_admit_once_and_keep_the_winning_actor(self):
        responses=self.race(lambda i:self.start_request(i))
        self.assertEqual(Counter(status for status,_,_ in responses),{202:1,409:29})
        winner=next(i for i,(status,_,_) in enumerate(responses) if status==202)
        with self.store.connect() as conn:
            groups=conn.execute('SELECT * FROM kumo_task_engine.cascades').fetchall()
            rejected=conn.execute("SELECT actor FROM kumo_task_engine.dependency_decisions WHERE decision='REQUEST_REJECTED'").fetchall()
        self.assertEqual(len(groups),1);self.assertEqual(groups[0]['requested_by'],f'USER_{winner:02}')
        self.assertEqual({r['actor'] for r in rejected},{f'USER_{i:02}' for i in range(USERS) if i!=winner})
        self.drain();self.assertEqual(len(self.store.status()['runs']),1)
        self.observed=dict(statuses=dict(Counter(str(s) for s,_,_ in responses)),winningActor=groups[0]['requested_by'],latency=latencies([t for _,_,t in responses]))

    def test_30_request_replays_share_one_durable_group(self):
        responses=self.race(lambda i:self.start_request(i,request='shared-retry'))
        self.assertTrue(all(status==202 for status,_,_ in responses))
        self.assertEqual(len({body['cascadeId'] for _,body,_ in responses}),1)
        self.drain();self.assertEqual(len(self.store.status()['runs']),1)

    def test_30_realtime_users_disconnect_and_reconnect_without_identity_leaks_or_new_identity_queries(self):
        self.assertEqual(self.application.sf.session_context.call_count,USERS)
        self.open_presence();self.close_presence()
        self.assertEqual(self.http(0,'/api/users/active')[1]['count'],0)
        self.open_presence()
        self.assertTrue(all(u['connectionCount']==1 for u in self.http(0,'/api/users/active')[1]['users']))
        self.assertEqual(self.application.sf.session_context.call_count,USERS)
        self.close_presence()
        self.assertEqual(self.http(0,'/api/users/active')[1]['count'],0)

    def test_30_starts_allow_only_one_adjacent_workflow_but_four_nonadjacent_workflows(self):
        responses=self.race(lambda i:self.start_request(i,'WF2' if i%2 else 'WF4'))
        self.assertEqual(Counter(s for s,_,_ in responses),{202:1,409:29})
        with self.store.connect() as conn:
            conn.execute('TRUNCATE kumo_task_engine.cascades,kumo_task_engine.runs RESTART IDENTITY CASCADE')
        names=['WF1','WF4','WF7','WF10']
        responses=self.race(lambda i:self.start_request(i,names[i%4],request=f'nonadjacent-{i}'))
        self.assertEqual(Counter(s for s,_,_ in responses),{202:4,409:26})
        self.drain();self.assertEqual(len(self.store.status()['runs']),4)

    def test_30_editors_have_one_revision_winner_and_no_lost_update(self):
        path=PREFIX+'/configuration/workflows/'+self.ids['WF1']
        responses=self.race(lambda i:self.http(i,path,'PATCH',dict(revision=1,description=f'Edit by USER_{i:02}')))
        self.assertEqual(Counter(s for s,_,_ in responses),{200:1,409:29})
        winner=next(i for i,(status,_,_) in enumerate(responses) if status==200)
        with self.store.connect() as conn:
            graph=conn.execute('SELECT * FROM kumo_task_engine.dependency_graphs WHERE id=%s',(self.graph['id'],)).fetchone()
        self.assertEqual(graph['revision'],2);self.assertEqual(graph['updated_by'],f'USER_{winner:02}')
        wf=next(w for w in graph['definition']['catalog']['workflows'] if w['WORKFLOW_ID']==self.ids['WF1'])
        self.assertEqual(wf['DESCRIPTION'],f'Edit by USER_{winner:02}')

    def test_30_mixed_cancel_retry_and_start_requests_preserve_native_run_until_confirmation(self):
        group=self.start('WF1',children=True);self.drain()
        root=next(n for n in self.graphs.read(group)['nodes'] if n['run_id'])
        with self.store.transaction() as conn:
            conn.execute("UPDATE kumo_task_engine.attempts SET state='RUNNING',native_started_at=clock_timestamp() WHERE run_id=%s AND position=0",(root['run_id'],))
            self.store.refresh(conn,root['run_id'])
            conn.execute("UPDATE kumo_task_engine.dependency_work SET state='ERROR',attempts=6,error='Dependency evaluation unavailable: RuntimeError',next_check_at=NULL WHERE cascade_id=%s",(group,))
        stop=Event()
        def dispatch():
            worker=DependencyWorker(self.store)
            while not stop.is_set():
                worker.step();stop.wait(.01)
        with ThreadPoolExecutor(max_workers=1) as pool:
            worker=pool.submit(dispatch)
            try:
                def action(i):
                    if i%3==0:return self.http(i,PREFIX+f'/cascades/{group}/cancel','POST')
                    if i%3==1:return self.http(i,PREFIX+f'/cascades/{group}/retry-dispatch','POST')
                    return self.start_request(i,request=f'blocked-during-cancel-{i}')
                responses=self.race(action)
            finally:
                stop.set();worker.result(timeout=10)
        self.assertEqual(Counter(s for s,_,_ in responses),{200:20,409:10})
        with self.store.connect() as conn:
            audit=conn.execute('''SELECT decision,actor FROM kumo_task_engine.dependency_decisions
                WHERE cascade_id=%s AND decision IN ('CANCEL_REQUESTED','DISPATCH_RETRY_REQUESTED')''',(group,)).fetchall()
        for decision,remainder in [('CANCEL_REQUESTED',0),('DISPATCH_RETRY_REQUESTED',1)]:
            self.assertEqual({r['actor'] for r in audit if r['decision']==decision},
                {f'USER_{i:02}' for i in range(USERS) if i%3==remainder})
        self.drain();data=self.graphs.read(group)
        self.assertTrue(data['cascade']['cancel_requested'])
        self.assertEqual(data['cascade']['state'],'RUNNING')
        self.assertEqual([n['run_id'] for n in data['nodes'] if n['run_id']],[root['run_id']])
        self.outcome(root['run_id'],'CANCELLED');self.drain()
        self.assertEqual(self.graphs.read(group)['cascade']['state'],'CANCELLED')
        self.assertEqual(len(self.store.status()['runs']),1)

    def test_30_monitor_users_keep_realtime_health_and_live_pg_status_responsive(self):
        duration=float(os.getenv('KUMO_MULTIUSER_LOAD_SECONDS','10'))
        self.assertTrue(6<=duration<=120)
        for _ in range(10):
            history=self.start('WF1');self.finish(history)
        group=self.start('WF1',children=True);self.drain()
        root=next(n for n in self.graphs.read(group)['nodes'] if n['run_id'])
        with self.store.transaction() as conn:
            conn.execute("UPDATE kumo_task_engine.attempts SET state='RUNNING',native_started_at=clock_timestamp() WHERE run_id=%s AND position=0",(root['run_id'],))
            self.store.refresh(conn,root['run_id'])
        self.open_presence()
        start=time.monotonic();barrier=Barrier(USERS+1)
        def observe(i):
            barrier.wait(timeout=10)
            timings=[];history_timings=[];statuses=[];tick=0;next_due=start
            while time.monotonic()-start<duration:
                status,data,elapsed=self.http(i,PREFIX+'/monitor')
                self.assertEqual(status,200);self.assertEqual(data['source'],'POSTGRES')
                self.assertEqual((data['workflowsCount'],data['jobsCount']),(26,51))
                self.assertEqual(data['dispatchErrors'],[])
                timings.append(elapsed)
                statuses.append(next(r['run']['status'] for r in data['rows'] if r['WORKFLOW_NAME']=='WF1'))
                if i%5==0 and tick%5==0:
                    status,_,elapsed=self.http(i,PREFIX+'/history?limit=20')
                    self.assertEqual(status,200);history_timings.append(elapsed)
                tick+=1;next_due+=2
                while next_due<time.monotonic():next_due+=2
                Event().wait(max(0,min(next_due,start+duration)-time.monotonic()))
            return timings,history_timings,statuses
        health=[];advanced=False;finished=False
        with ThreadPoolExecutor(max_workers=USERS) as pool:
            futures=[pool.submit(observe,i) for i in range(USERS)]
            barrier.wait(timeout=10)
            while not all(f.done() for f in futures):
                status,_,elapsed=self.http(0,'/api/health');self.assertEqual(status,200);health.append(elapsed)
                age=time.monotonic()-start
                if age>=duration/3 and not advanced:
                    self.outcome(root['run_id']);self.drain();advanced=True
                if age>=duration*2/3 and not finished:
                    self.finish(group);finished=True
                Event().wait(.5)
            results=[f.result() for f in futures]
        if not finished:self.finish(group)
        self.assertTrue(all(len(t)>=2 for t,_,_ in results))
        self.assertEqual(self.broker.client_count(),USERS)
        final=self.race(lambda i:self.http(i,PREFIX+'/monitor'))
        self.assertTrue(all(s==200 and next(r['run']['status'] for r in d['rows'] if r['WORKFLOW_NAME']=='WF1')=='SUCCESS' for s,d,_ in final))
        self.close_presence()
        self.assertEqual(self.http(0,'/api/users/active')[1]['count'],0)
        self.drain();self.assertFalse(self.worker.step())
        self.observed=dict(users=USERS,catalog=dict(workflows=26,jobs=51),durationSeconds=duration,pollSeconds=2,
            monitor=latencies([t for timings,_,_ in results for t in timings]),
            history=latencies([t for _,timings,_ in results for t in timings]),health=latencies(health),
            peakPostgresConnections=self.store.peak_connections,openedPostgresConnections=self.store.connections,
            allUsersSeeSuccess=True,presenceAtEnd=0,snowflakeConnections=0,
            userObservedStatuses=[sorted(set(states)) for _,_,states in results])
        print('MULTIUSER_METRICS '+json.dumps(self.observed),flush=True)


if __name__=='__main__':unittest.main()
