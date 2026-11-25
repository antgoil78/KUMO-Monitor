"""Connection outages keep accepted work recoverable without duplicate execution."""
from concurrent.futures import ThreadPoolExecutor
import os
import errno
import socket
import ssl
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_postgres_task_engine as engine
from task_engine.recovery import connection_failure,recovery_stage,retry_delay
from task_engine.store import Conflict
from task_engine.worker import Worker
from task_engine.snowflake_tasks import TaskDrift
from snowflake.connector.errors import OperationalError,ProgrammingError


class ClassificationTests(unittest.TestCase):
    def test_transport_failures_and_bounded_backoff(self):
        for error in [socket.gaierror(socket.EAI_AGAIN,'temporary DNS failure'),TimeoutError(),ConnectionResetError(),OSError(errno.ENETUNREACH,'offline'),OperationalError(errno=250001,msg='private connection material')]:
            self.assertIsNotNone(connection_failure(error))
        self.assertEqual(retry_delay(1),2)
        self.assertEqual(retry_delay(8),256)
        self.assertEqual(retry_delay(100000),300)

    def test_authentication_permissions_certificates_and_postgres_are_not_network_retries(self):
        import psycopg
        for error in [ProgrammingError(errno=390100,msg='wrong password'),ProgrammingError(errno=3001,msg='insufficient privilege'),
                      ssl.SSLCertVerificationError('bad certificate'),psycopg.OperationalError('database down'),RuntimeError('other failure')]:
            self.assertIsNone(connection_failure(error))

    def test_creation_recovery_refuses_conflicting_native_execution_evidence(self):
        base=dict(state='UNKNOWN',error='OperationalError: task creation unavailable')
        self.assertEqual(recovery_stage(base),'CREATING')
        for field in ['native_group_id','native_attempt','query_id','native_started_at','observed_at','ended_at']:
            self.assertIsNone(recovery_stage(dict(base,**{field:'present'})))


@unittest.skipUnless(os.getenv('KUMO_TASK_ENGINE_TEST_DSN'),'Requires isolated PostgreSQL test database')
class ConnectionRecoveryTests(unittest.TestCase):
    setUp=engine.PostgresWorkerTests.setUp
    submit=engine.PostgresWorkerTests.submit
    due=engine.PostgresWorkerTests.due
    tick=engine.PostgresWorkerTests.tick

    def job(self,run):return self.store.read(run)['jobs'][0]

    def legacy_unknown(self,run,*,submitted=False):
        with self.store.transaction() as conn:
            conn.execute("UPDATE kumo_task_engine.attempts SET state='UNKNOWN',error=%s,recovery_checks=6,next_check_at=NULL WHERE run_id=%s",
                ('Submission/status unavailable: OperationalError' if submitted else 'OperationalError: task creation unavailable',run))
            self.store.refresh(conn,run)

    def test_creation_outage_survives_eight_failures_and_worker_restart_then_executes_once(self):
        run=self.submit();identity=self.job(run)['id']
        original=self.tasks.ensure
        def lost_create_ack(job):
            original(job)
            raise socket.gaierror(socket.EAI_AGAIN,'DO NOT STORE connection secrets')
        with patch.object(self.tasks,'ensure',side_effect=lost_create_ack):
            self.tick(8)
        job=self.job(run);native=next(iter(self.tasks.native.values()))['task_id']
        self.assertEqual(job['state'],'CREATING');self.assertEqual(job['recovery_kind'],'NETWORK')
        self.assertEqual(job['recovery_checks'],8);self.assertIsNotNone(job['next_check_at'])
        self.assertNotIn('secrets',job['error']);self.assertEqual(job['id'],identity)
        self.assertFalse(self.tasks.executions)
        self.assertFalse(self.worker.step(),'Backoff must not contact Snowflake before due time')
        self.worker=Worker(self.store,self.tasks);self.tick()
        self.assertEqual(self.job(run)['task_id'],native)
        self.assertIsNone(self.job(run)['recovery_kind']);self.assertEqual(self.tasks.executions,[str(identity)])
        self.tasks.native[str(identity)]['state']='SUCCEEDED';self.tick(2)
        self.assertEqual(self.store.read(run)['run']['state'],'SUCCESS')
        with self.store.connect() as conn:
            audit=conn.execute("SELECT decision,facts FROM kumo_task_engine.dependency_decisions WHERE run_id=%s",(run,)).fetchall()
        self.assertEqual(sum(a['decision']=='NATIVE_CONNECTION_RETRY' for a in audit),8)
        self.assertTrue(any(a['decision']=='NATIVE_CONNECTION_RESTORED' for a in audit))
        self.assertNotIn('secrets',str(audit))

    def test_lost_execute_ack_and_long_history_outage_never_repeat_execute(self):
        run=self.submit();original=self.tasks.execute
        def lost_ack(job):original(job);raise ConnectionResetError('reply lost')
        with patch.object(self.tasks,'execute',side_effect=lost_ack):self.tick()
        submitted=self.job(run)['submit_started_at']
        with patch.object(self.tasks,'history',side_effect=TimeoutError()):self.tick(8)
        self.assertEqual(self.job(run)['state'],'SUBMITTING')
        self.assertEqual(self.job(run)['recovery_kind'],'NETWORK')
        self.worker=Worker(self.store,self.tasks);self.tick()
        self.assertEqual(self.job(run)['state'],'RUNNING')
        self.assertEqual(self.job(run)['submit_started_at'],submitted)
        self.assertEqual(len(self.tasks.executions),1)

    def test_network_outage_does_not_consume_the_missing_history_budget(self):
        run=self.submit();self.tick()
        with patch.object(self.tasks,'history',side_effect=ConnectionError()):self.tick(8)
        self.tasks.hide=True;self.tick()
        self.assertEqual(self.job(run)['recovery_checks'],1)
        self.assertIsNone(self.job(run)['recovery_kind'])
        self.tick(5);self.assertEqual(self.job(run)['state'],'UNKNOWN')
        self.assertIsNone(self.job(run)['next_check_at']);self.assertEqual(len(self.tasks.executions),1)

    def test_cleanup_outage_preserves_sql_failure_and_retries_without_status_polling(self):
        run=self.submit();self.tick();key=self.tasks.executions[0]
        self.tasks.native[key]['state']='FAILED';self.tick();polls=len(self.tasks.polls)
        with patch.object(self.tasks,'drop',side_effect=ConnectionError()):self.tick(8)
        job=self.job(run)
        self.assertEqual(job['state'],'FAILED');self.assertEqual(job['error'],'SQL failed')
        self.assertEqual(job['cleanup_state'],'PENDING');self.assertEqual(job['recovery_kind'],'NETWORK')
        self.tick();self.assertEqual(self.job(run)['cleanup_state'],'DONE')
        self.assertEqual(self.job(run)['error'],'SQL failed');self.assertEqual(len(self.tasks.polls),polls)
        self.assertFalse(self.worker.step())

    def test_permanent_configuration_failure_is_bounded_and_identity_drift_cannot_be_recovered(self):
        run=self.submit()
        with patch.object(self.tasks,'ensure',side_effect=ProgrammingError(errno=3001,msg='permission denied')):self.tick(6)
        self.assertEqual(self.job(run)['state'],'UNKNOWN');self.assertIsNone(self.job(run)['recovery_kind'])
        other=self.submit()
        with patch.object(self.tasks,'ensure',side_effect=TaskDrift('Task ownership changed')):self.tick()
        with self.assertRaises(Conflict):self.store.recover(other,'OPERATOR')
        self.assertFalse(self.tasks.executions)

    def test_task_drift_after_network_restores_connection_but_requires_review(self):
        run=self.submit()
        with patch.object(self.tasks,'ensure',side_effect=TimeoutError()):self.tick()
        with patch.object(self.tasks,'ensure',side_effect=TaskDrift('Task ownership changed')):self.tick()
        self.assertEqual(self.job(run)['state'],'UNKNOWN')
        self.assertIsNone(self.job(run)['recovery_kind']);self.assertIsNone(self.job(run)['next_check_at'])
        with self.assertRaises(Conflict):self.store.recover(run,'OPERATOR')

    def test_legacy_creation_unknown_recovers_same_attempt_and_thirty_clients_admit_recovery_once(self):
        run=self.submit();identity=self.job(run)['id'];self.legacy_unknown(run)
        def recover(i):
            try:self.store.recover(run,f'USER_{i}');return True
            except Conflict:return False
        with ThreadPoolExecutor(max_workers=30) as pool:accepted=list(pool.map(recover,range(30)))
        self.assertEqual(sum(accepted),1);self.assertEqual(self.job(run)['id'],identity)
        self.tick();self.assertEqual(self.tasks.executions,[str(identity)])
        with self.store.connect() as conn:
            logs=conn.execute("SELECT actor,facts FROM kumo_task_engine.dependency_decisions WHERE decision='NATIVE_RECOVERY_REQUESTED' AND run_id=%s",(run,)).fetchall()
        self.assertEqual(len(logs),1);self.assertTrue(logs[0]['actor'].startswith('USER_'))
        self.assertEqual(logs[0]['facts']['stage'],'CREATING')

    def test_submitted_unknown_recovery_only_reads_history(self):
        run=self.submit();self.tick();self.legacy_unknown(run,submitted=True)
        self.store.recover(run,'OPERATOR');self.tick()
        self.assertEqual(self.job(run)['state'],'RUNNING');self.assertEqual(len(self.tasks.executions),1)

    def test_cancellation_during_outage_is_honoured_after_reconnection(self):
        run=self.submit()
        with patch.object(self.tasks,'ensure',side_effect=TimeoutError()):self.tick(8)
        self.store.cancel(run)
        # Cancellation must not wait for the long network retry deadline.
        self.assertTrue(self.worker.step());self.tick()
        self.assertEqual(self.store.read(run)['run']['state'],'CANCELLED');self.assertFalse(self.tasks.executions)

    def test_cancelled_submitted_unknown_is_observed_and_cancelled_without_replay(self):
        run=self.submit();self.tick();self.legacy_unknown(run,submitted=True)
        self.store.cancel(run);self.store.recover(run,'OPERATOR');self.tick(2)
        self.assertEqual(self.store.read(run)['run']['state'],'CANCELLED');self.assertEqual(len(self.tasks.executions),1)

    def test_cancelled_legacy_creation_unknown_recovers_to_cancelled_without_execute(self):
        run=self.submit();self.legacy_unknown(run);self.store.cancel(run)
        self.store.recover(run,'OPERATOR');self.tick(2)
        self.assertEqual(self.store.read(run)['run']['state'],'CANCELLED')
        self.assertFalse(self.tasks.executions)

    def test_dependency_chain_resumes_after_wf4_outage_and_monitor_shows_recovery(self):
        from task_engine.catalog import import_catalog
        from task_engine.dependencies import Graphs,DependencyWorker
        from task_engine.monitor import Monitor
        from test_postgres_dependency_scenarios import full_catalog
        raw=full_catalog();graph=import_catalog(self.store,raw,'RECOVERY FIXTURE')
        ids={w['WORKFLOW_NAME']:w['WORKFLOW_ID'] for w in raw['workflows']}
        group=Graphs(self.store).start(graph['id'],dict(requestId=str(uuid.uuid4()),workflowId=ids['WF1'],includeChildren=True),'TEST')['cascadeId']
        dependencies=DependencyWorker(self.store);original=self.tasks.ensure;failures=[]
        def ensure(job):
            if 'WF4' in job['snapshot']['sql'] and len(failures)<8:
                failures.append(job['id']);raise socket.gaierror(socket.EAI_AGAIN,'offline')
            return original(job)
        with patch.object(self.tasks,'ensure',side_effect=ensure):
            for _ in range(150):
                while dependencies.step():pass
                self.due();self.worker.step()
                for native in self.tasks.native.values():
                    if native['state']=='EXECUTING':native['state']='SUCCEEDED'
                if failures and len(failures)<=8:
                    rows={w['WORKFLOW_NAME']:w for w in Monitor(self.store).payload()['rows']}
                    if rows['WF4']['run'].get('recovery',{}):
                        self.assertTrue(rows['WF4']['run']['recovery']['automatic'])
                        self.assertEqual(rows['WF4']['run']['status'],'QUEUED')
                        self.assertIn('automatic recovery',rows['WF4']['run']['message'])
                        self.assertEqual(rows['WF5']['run']['status'],'WAITING')
                if Graphs(self.store).read(group)['cascade']['state']=='SUCCESS':break
            else:self.fail('Dependency group did not recover to completion')
        self.assertEqual(len(failures),8);self.assertEqual(len(set(failures)),1)
        self.assertEqual(len(self.tasks.executions),10);self.assertEqual(len(set(self.tasks.executions)),10)
        while self.worker.step():pass
        before=len(self.tasks.polls);self.assertFalse(self.worker.step());self.assertEqual(len(self.tasks.polls),before)
