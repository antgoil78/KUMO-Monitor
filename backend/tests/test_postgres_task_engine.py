"""Real PostgreSQL transactions and deterministic external-task failure boundaries.

Set KUMO_TASK_ENGINE_TEST_DSN to an isolated database ending in _test.
Never connects to Snowflake or runs business jobs.
"""
from copy import deepcopy
from datetime import datetime,timezone
import os
from pathlib import Path
import sys
import unittest
import uuid
from unittest.mock import MagicMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from task_engine.settings import Settings
from task_engine.store import Store,Conflict,normalize
from task_engine.worker import Worker
from task_engine.snowflake_tasks import SnowflakeTasks,TaskDrift


def plan(mode='SEQUENTIAL',jobs=None):
    return dict(requestId=str(uuid.uuid4()),mode=mode,jobs=jobs or [dict(key='a',sql='SELECT 1')])


class FakeTasks:
    def __init__(self,store):
        self.store=store;self.native={};self.executions=[];self.polls=[];self.drops=[]
        self.lost_ack=False;self.create_lost_ack=False;self.drop_fails=False;self.hide=False

    def ensure(self,job):
        self.native.setdefault(str(job['id']),dict(task_id=str(uuid.uuid4()),state=None,group=str(uuid.uuid4())))
        if self.create_lost_ack:self.create_lost_ack=False;raise RuntimeError('created, reply lost')
        return self.native[str(job['id'])]['task_id']

    def execute(self,job):
        self.executions.append(str(job['id']));self.native[str(job['id'])]['state']='EXECUTING'
        if self.lost_ack:self.lost_ack=False;raise RuntimeError('accepted, reply lost')

    def history(self,job):
        self.polls.append(str(job['id']))
        if self.hide:return None
        n=self.native.get(str(job['id']),{})
        if not n.get('state'):return None
        return dict(state=n['state'],graph_run_group_id=n['group'],attempt_number=1,
                    query_id=None if n['state']=='SCHEDULED' else 'query-'+str(job['id']),
                    completed_time=datetime.now(timezone.utc) if n['state'] in ('SUCCEEDED','FAILED','CANCELLED') else None,
                    error_message='SQL failed' if n['state']=='FAILED' else None)

    def cancel(self,job):self.native[str(job['id'])]['state']='CANCELLED'

    def drop(self,job):
        current=next(j for j in self.store.read(job['run_id'])['jobs'] if j['id']==job['id'])
        assert current['state'] in ('SUCCESS','FAILED','CANCELLED','SKIPPED'),'outcome must commit before DROP'
        self.drops.append(str(job['id']))
        if self.drop_fails:raise RuntimeError('drop unavailable')
        self.native.pop(str(job['id']),None)


class ValidationTests(unittest.TestCase):
    def test_dml_tables_external_functions_and_extra_statements_are_rejected(self):
        for sql in ['SELECT * FROM KUMO_DEV.MONITOR_META.WORKFLOW','DELETE FROM x','SELECT 1; SELECT 2',
                    'SELECT SYSTEM$CANCEL_QUERY(\'q\')','SELECT SOME_UDF()','SELECT 1 -- hi',
                    "SELECT GET_DDL('TABLE','x')"]:
            with self.subTest(sql=sql),self.assertRaises(ValueError):normalize(plan(jobs=[dict(sql=sql)]))

    def test_invalid_dependency_graphs_and_timeouts_never_admit(self):
        for jobs in [[dict(key='a',sql='SELECT 1',dependsOn=['b'])],
                     [dict(key='a',sql='SELECT 1'),dict(key='a',sql='SELECT 2')],
                     [dict(sql='SELECT 1',timeoutSeconds=True)], [dict(sql='SELECT 1',timeoutSeconds=301)]]:
            with self.subTest(jobs=jobs),self.assertRaises(ValueError):normalize(plan(jobs=jobs))

    def test_settings_reject_non_dev_and_unsafe_identifiers(self):
        for args in [dict(namespace='KUMO_TST.MONITOR_APP'),dict(warehouse='X; DROP TASK X'),dict(lease_seconds=10)]:
            with self.subTest(args=args),self.assertRaises(ValueError):Settings('unused',**args)


@unittest.skipUnless(os.getenv('KUMO_TASK_ENGINE_TEST_DSN'),'Requires isolated PostgreSQL test database')
class PostgresWorkerTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(Settings(os.environ['KUMO_TASK_ENGINE_TEST_DSN']))
        with self.store.connect() as conn:
            assert conn.execute('SELECT current_database() AS name').fetchone()['name'].endswith('_test'),'Never truncate a non-test database'
        self.store.initialize()
        with self.store.connect() as conn:conn.execute('TRUNCATE kumo_task_engine.schedules,kumo_task_engine.scheduler_workers,kumo_task_engine.events,kumo_task_engine.attempts,kumo_task_engine.runs RESTART IDENTITY CASCADE')
        self.tasks=FakeTasks(self.store);self.worker=Worker(self.store,self.tasks)

    def submit(self,body=None):
        return self.store.submit(body or plan(),'TRUSTED_USER')['runId']

    def due(self):
        with self.store.connect() as conn:
            conn.execute('UPDATE kumo_task_engine.attempts SET next_check_at=clock_timestamp() WHERE next_check_at IS NOT NULL')

    def tick(self,count=1):
        for _ in range(count):self.due();self.worker.step()

    def test_atomic_admission_and_frozen_idempotent_request(self):
        body=plan();run=self.submit(body);body['jobs'][0]['sql']='SELECT 2'
        row=self.store.read(run)
        self.assertEqual(row['jobs'][0]['snapshot']['sql'],'SELECT 1')
        self.assertEqual(row['run']['requested_by'],'TRUSTED_USER')
        with self.assertRaises(Conflict):self.submit(body)
        body['jobs'][0]['sql']='SELECT 1'
        self.assertTrue(self.store.submit(body,'OTHER')['reused'])
        self.assertEqual(len(self.store.read(run)['events']),1)
        self.assertFalse(self.tasks.executions)

    def test_worker_with_no_work_never_contacts_snowflake(self):
        for _ in range(5):self.assertFalse(self.worker.step())
        self.assertFalse(self.tasks.polls);self.assertFalse(self.tasks.executions)

    def test_exclusive_claim_and_expired_owner_cannot_mutate(self):
        run=self.submit();owner=uuid.uuid4();job=self.store.claim(owner)
        self.assertIsNone(self.store.claim(uuid.uuid4()))
        with self.store.connect() as conn:conn.execute("UPDATE kumo_task_engine.attempts SET lease_until=clock_timestamp()-interval '1 second'")
        with self.assertRaises(Conflict):self.store.update(job,owner,state='RUNNING')
        self.assertIsNotNone(self.store.claim(uuid.uuid4()))

    def test_completion_commits_before_cleanup_and_terminal_work_is_never_polled(self):
        run=self.submit();self.tick();key=self.tasks.executions[0]
        self.tasks.native[key]['state']='SUCCEEDED';self.tick()
        self.assertEqual(self.store.read(run)['run']['state'],'SUCCESS')
        self.assertFalse(self.tasks.drops)
        self.tick();before=len(self.tasks.polls)
        for _ in range(5):self.assertFalse(self.worker.step())
        self.assertEqual(len(self.tasks.polls),before)
        self.assertEqual(self.store.read(run)['jobs'][0]['cleanup_state'],'DONE')
        self.assertEqual([e['kind'] for e in self.store.read(run)['events']].count('FINISHED'),1)

    def test_lost_execute_ack_is_recovered_by_another_worker_without_execute_retry(self):
        run=self.submit();self.tasks.lost_ack=True;self.tick()
        self.assertEqual(self.store.read(run)['jobs'][0]['state'],'SUBMITTING')
        self.worker=Worker(self.store,self.tasks);self.tick()
        self.assertEqual(self.store.read(run)['jobs'][0]['state'],'RUNNING')
        self.assertEqual(len(self.tasks.executions),1)
        self.tasks.native[self.tasks.executions[0]]['state']='SUCCEEDED';self.tick(2)
        self.assertEqual(self.store.read(run)['run']['state'],'SUCCESS')
        self.assertEqual(len(self.tasks.executions),1)

    def test_create_reply_loss_reuses_object_then_executes_once(self):
        run=self.submit();self.tasks.create_lost_ack=True;self.tick()
        original=next(iter(self.tasks.native.values()))['task_id']
        self.tick();job=self.store.read(run)['jobs'][0]
        self.assertEqual(job['task_id'],original);self.assertEqual(len(self.tasks.executions),1)

    def test_crash_after_submitting_commit_before_execute_never_causes_blind_submission(self):
        run=self.submit();original=self.store.update
        class Crash(BaseException):pass
        def update(job,owner,**changes):
            original(job,owner,**changes)
            if changes.get('state')=='SUBMITTING':raise Crash()
        with patch.object(self.store,'update',side_effect=update),self.assertRaises(Crash):self.worker.step()
        self.worker=Worker(self.store,self.tasks)
        self.tick(6)
        self.assertEqual(self.store.read(run)['run']['state'],'UNKNOWN')
        self.assertFalse(self.tasks.executions)
        self.assertFalse(self.worker.step())

    def test_missing_history_has_bounded_recovery_and_does_not_fabricate_failure(self):
        run=self.submit();self.tick();self.tasks.hide=True;self.tick(6)
        row=self.store.read(run)
        self.assertEqual(row['jobs'][0]['state'],'UNKNOWN')
        self.assertEqual(row['jobs'][0]['recovery_checks'],6)
        self.assertIsNone(row['jobs'][0]['ended_at'])
        before=len(self.tasks.polls)
        self.assertFalse(self.worker.step());self.assertEqual(len(self.tasks.polls),before)

    def test_native_scheduled_without_query_id_is_still_observed(self):
        run=self.submit();self.tick();self.tasks.native[self.tasks.executions[0]]['state']='SCHEDULED';self.tick()
        job=self.store.read(run)['jobs'][0]
        self.assertEqual(job['state'],'SCHEDULED');self.assertIsNone(job['query_id'])
        self.assertIsNotNone(job['next_check_at'])

    def test_waiting_dependencies_do_not_generate_status_requests(self):
        run=self.submit(plan(jobs=[dict(key='a',sql='SELECT 1'),dict(key='b',sql='SELECT 2')]))
        self.tick(2) # a is RUNNING with a future check; b has no native task.
        before=len(self.tasks.polls)
        self.assertFalse(self.worker.step());self.assertEqual(len(self.tasks.polls),before)
        self.assertEqual([j['state'] for j in self.store.read(run)['jobs']],['RUNNING','QUEUED'])

    def test_failure_cascades_to_skipped_dependants_without_creating_their_tasks(self):
        run=self.submit(plan(jobs=[dict(key='a',sql='SELECT 1'),dict(key='b',sql='SELECT 2'),dict(key='c',sql='SELECT 3')]))
        self.tick();self.tasks.native[self.tasks.executions[0]]['state']='FAILED';self.tick(4)
        self.assertEqual([j['state'] for j in self.store.read(run)['jobs']],['FAILED','SKIPPED','SKIPPED'])
        self.assertEqual(self.store.read(run)['run']['state'],'FAILED')
        self.assertEqual(len(self.tasks.executions),1)

    def test_parallel_sibling_continues_after_other_job_failure(self):
        run=self.submit(plan('PARALLEL',[dict(key='a',sql='SELECT 1'),dict(key='b',sql='SELECT 2')]))
        self.tick(4);self.assertEqual(len(self.tasks.executions),2)
        self.tasks.native[self.tasks.executions[0]]['state']='FAILED';self.tick()
        self.assertEqual(self.store.read(run)['run']['state'],'RUNNING')
        self.tick();self.tasks.native[self.tasks.executions[1]]['state']='SUCCEEDED';self.tick(3)
        self.assertEqual([j['state'] for j in self.store.read(run)['jobs']],['FAILED','SUCCESS'])
        self.assertEqual(self.store.read(run)['run']['state'],'FAILED')

    def test_queued_cancellation_never_creates_a_task(self):
        run=self.submit();self.store.cancel(run)
        self.assertFalse(self.worker.step());self.assertEqual(self.store.read(run)['run']['state'],'CANCELLED')
        self.assertFalse(self.tasks.executions)

    def test_running_cancellation_waits_for_native_confirmation(self):
        run=self.submit();self.tick();self.store.cancel(run);self.tick()
        self.assertEqual(self.store.read(run)['run']['state'],'RUNNING')
        self.tick();self.assertEqual(self.store.read(run)['run']['state'],'CANCELLED')

    def test_cancellation_is_observed_before_older_queued_submissions(self):
        run=self.submit();self.tick()
        other=self.submit()
        self.store.cancel(run)
        self.tick()
        self.assertEqual(len(self.tasks.executions),1)
        self.assertEqual(self.store.read(other)['jobs'][0]['state'],'QUEUED')
        self.assertEqual(self.tasks.native[self.tasks.executions[0]]['state'],'CANCELLED')

    def test_cleanup_failure_is_bounded_and_does_not_change_workload_success(self):
        run=self.submit();self.tick();self.tasks.native[self.tasks.executions[0]]['state']='SUCCEEDED';self.tick()
        self.tasks.drop_fails=True;self.tick(3)
        row=self.store.read(run)
        self.assertEqual(row['run']['state'],'SUCCESS');self.assertEqual(row['jobs'][0]['cleanup_state'],'ERROR')
        self.assertEqual(row['jobs'][0]['cleanup_attempts'],3)
        self.assertFalse(self.worker.step());self.assertEqual(len(self.tasks.executions),1)

    def test_postgres_outcome_failure_prevents_drop_and_recovers_later(self):
        run=self.submit();self.tick();self.tasks.native[self.tasks.executions[0]]['state']='SUCCEEDED'
        original=self.store.update
        def update(job,owner,**changes):
            if changes.get('state')=='SUCCESS':raise RuntimeError('database unavailable')
            return original(job,owner,**changes)
        with patch.object(self.store,'update',side_effect=update):self.tick()
        self.assertFalse(self.tasks.drops)
        self.worker=Worker(self.store,self.tasks);self.tick(2)
        self.assertEqual(self.store.read(run)['run']['state'],'SUCCESS')
        self.assertEqual(len(self.tasks.executions),1)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.settings=Settings('unused');self.adapter=SnowflakeTasks(self.settings)
        self.id=uuid.uuid4();self.job=dict(id=self.id,task_id='native-id',task_fqn=self.settings.namespace+'.TMP_PG_ORCH_'+self.id.hex.upper(),
                    submit_started_at=datetime.now(timezone.utc),snapshot=dict(sql='SELECT 1',timeoutSeconds=5))
        self.native=dict(name=self.job['task_fqn'].split('.')[-1],id='native-id',owner='KUMO_ADMIN',warehouse='KUMO_ELT_GEN_1',
                         schedule=None,condition=None,comment='KUMO PostgreSQL prototype attempt '+str(self.id),definition='SELECT 1')

    def test_tracked_cursor_without_context_manager_is_closed_and_batch_reuses_connection(self):
        from contextlib import contextmanager
        class Cursor:
            def __init__(self):self.closed=False
            def execute(self,*args):pass
            def fetchall(self):return [{'VALUE':1}]
            def close(self):self.closed=True
        cursors=[];connections=[]
        class Connection:
            def cursor(self,*args):
                cursor=Cursor();cursors.append(cursor);return cursor
        @contextmanager
        def connect():
            connections.append(1);yield Connection()
        adapter=SnowflakeTasks(self.settings,connect)
        with adapter.batch():
            self.assertFalse(connections)
            self.assertEqual(adapter.query('SELECT 1'),[dict(value=1)])
            adapter.query('SELECT 2')
        self.assertEqual(len(connections),1)
        self.assertTrue(all(cursor.closed for cursor in cursors))
        self.assertIsNone(adapter._batch_connection)

    def test_created_tasks_are_unscheduled_retry_disabled_and_propagate_sql_errors(self):
        with patch.object(self.adapter,'query',side_effect=[[],[self.native]]) as query:
            self.assertEqual(self.adapter.ensure(self.job),'native-id')
        sql=query.call_args_list[0].args[0]
        self.assertIn('TASK_AUTO_RETRY_ATTEMPTS=0',sql);self.assertIn('USER_TASK_TIMEOUT_MS=5000',sql)
        self.assertTrue(sql.endswith(' AS SELECT 1'));self.assertNotIn('SCHEDULE=',sql)

    def test_changed_native_identity_or_body_prevents_drop(self):
        for values in [dict(id='replacement'),dict(definition='SELECT 2'),dict(owner='OTHER'),dict(schedule='1 MINUTE')]:
            with self.subTest(values=values),patch.object(self.adapter,'query',return_value=[dict(self.native,**values)]) as query:
                with self.assertRaises(TaskDrift):self.adapter.drop(self.job)
                self.assertEqual(query.call_count,1)

    def test_drop_missing_task_is_idempotent(self):
        with patch.object(self.adapter,'query',return_value=[]) as query:self.adapter.drop(self.job)
        self.assertEqual(query.call_count,1)

    def test_task_address_cannot_escape_disposable_namespace(self):
        job=dict(self.job,task_fqn='KUMO_TST.MONITOR_APP.TASK_ORCH_MAINTENANCE')
        with patch.object(self.adapter,'query') as query,self.assertRaises(TaskDrift):self.adapter.execute(job)
        query.assert_not_called()

    def test_multiple_native_invocations_are_quarantined(self):
        with patch.object(self.adapter,'query',return_value=[dict(graph_run_group_id='a'),dict(graph_run_group_id='b')]),self.assertRaises(TaskDrift):
            self.adapter.history(self.job)


class ApiTests(unittest.TestCase):
    def setUp(self):
        import app as application
        from task_engine import integration
        self.application=application;self.integration=integration
        self.client=application.app.test_client()
        self.database=MagicMock()
        self.database.status.return_value=dict(ok=True,counts={},runs=[],cleanupRetrying=0)
        self.database.submit.return_value=dict(ok=True,runId=str(uuid.uuid4()),reused=False)
        for item in (patch.object(integration,'store',return_value=self.database),
                     patch.object(application,'_ensure_environment',side_effect=AssertionError('No Snowflake bootstrap')),
                     patch.object(application.orchestration_cache,'refresh_async',side_effect=AssertionError('No Snowflake refresh')),
                     patch.object(application.sf,'connection',side_effect=AssertionError('No Snowflake connection')),
                     patch.dict(os.environ,{'KUMO_ORCHESTRATION_RUNS_ENABLED':'true'})):
            item.start();self.addCleanup(item.stop)

    def test_status_admission_and_cancel_never_bootstrap_or_refresh_snowflake(self):
        self.assertEqual(self.client.get(self.integration.PREFIX).status_code,200)
        body=plan()
        with patch.dict(self.application.app.extensions,task_prototype_actor_context=lambda:dict(userName='TRUSTED')):
            self.assertEqual(self.client.post(self.integration.PREFIX+'/runs',json=body).status_code,202)
        self.database.submit.assert_called_once_with(body,'TRUSTED')
        self.database.cancel.return_value=dict(ok=True,run={})
        self.assertEqual(self.client.post(self.integration.PREFIX+'/runs/'+str(uuid.uuid4())+'/cancel').status_code,200)

    def test_database_errors_never_expose_credentials(self):
        self.database.status.side_effect=RuntimeError('postgresql://user:secret@private-host/db')
        response=self.client.get(self.integration.PREFIX)
        self.assertEqual(response.status_code,503)
        self.assertNotIn('secret',response.get_data(as_text=True))
        self.assertNotIn('private-host',response.get_data(as_text=True))

    def test_feature_flag_and_non_dev_environment_prevent_database_access(self):
        with patch.object(self.integration,'store',wraps=lambda:Store(Settings.from_environment())):
            for values in [dict(KUMO_TASK_PROTOTYPE_ENABLED='false'),dict(KUMO_TASK_PROTOTYPE_ENABLED='true',KUMO_ENV='TST')]:
                with self.subTest(values=values),patch.dict(os.environ,values),patch.object(Store,'connect') as connect:
                    self.assertEqual(self.client.get(self.integration.PREFIX).status_code,503)
                    connect.assert_not_called()

    def test_disabled_execution_controls_reject_admission(self):
        with patch.dict(os.environ,KUMO_ORCHESTRATION_RUNS_ENABLED='false'):
            response=self.client.post(self.integration.PREFIX+'/runs',json=plan())
        self.assertEqual(response.status_code,503)
        self.database.submit.assert_not_called()

    def test_schedule_routes_use_trusted_actor_and_never_access_snowflake(self):
        with patch('task_engine.schedules.Schedules') as service,patch.dict(self.application.app.extensions,task_prototype_actor_context=lambda:dict(userName='TRUSTED')):
            service.return_value.status.return_value=dict(ok=True,schedules=[])
            service.return_value.create.return_value=dict(ok=True,schedule={})
            service.return_value.update.return_value=dict(ok=True,schedule={})
            self.assertEqual(self.client.get(self.integration.PREFIX+'/schedules').status_code,200)
            self.assertEqual(self.client.post(self.integration.PREFIX+'/schedules',json={'name':'Test'}).status_code,201)
            service.return_value.create.assert_called_once_with({'name':'Test'},'TRUSTED')
            self.assertEqual(self.client.patch(self.integration.PREFIX+'/schedules/id',json={'expectedRevision':1,'enabled':False}).status_code,200)
            service.return_value.update.assert_called_once_with('id',{'expectedRevision':1,'enabled':False},'TRUSTED')

    def test_disabled_execution_controls_block_schedule_mutations(self):
        with patch('task_engine.schedules.Schedules') as service,patch.dict(os.environ,KUMO_ORCHESTRATION_RUNS_ENABLED='false'):
            for method,path in [('post','/schedules'),('patch','/schedules/id')]:
                self.assertEqual(getattr(self.client,method)(self.integration.PREFIX+path,json={}).status_code,503)
            service.assert_not_called()


if __name__=='__main__':unittest.main()
