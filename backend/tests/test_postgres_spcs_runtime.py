"""SPCS startup, portable business commands and durable mixed SQL/dbt execution."""
from copy import deepcopy
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from task_engine import commands
from task_engine.catalog import CatalogConfiguration
from task_engine.dependencies import Graphs, DependencyWorker
from task_engine.monitor import Monitor
from task_engine.runtime import child_commands, readiness, supervise
from task_engine.settings import Settings
from task_engine.snowflake_tasks import body_for
from task_engine.store import normalize
from task_engine.worker import Worker
import test_postgres_monitor as fixtures
from test_postgres_task_engine import FakeTasks


def dbt_command():
    return dict(workflowType='DBT', dbtCommand='dbt compile --select test_model',
                dbtProjectFqn='[DATABASE].[DBT_SCHEMA].[DBT_PROJECT]', dbtTarget='[DBT_TARGET]')


class RuntimeUnitTests(unittest.TestCase):
    def test_primary_engine_cannot_reenable_the_legacy_dispatcher(self):
        from test_environment_config import load_config
        settings=load_config(KUMO_ENV='DEV',KUMO_TASK_PROTOTYPE_ENABLED='true',KUMO_POSTGRES_ENGINE_PRIMARY='true')
        apply=settings['apply_orchestration_settings']
        apply([dict(PARAMETER_KEY='STREAM_DISPATCH_ENABLED',ACTIVE_FL=True,VALUE_TYPE='BOOLEAN',PARAMETER_VALUE='true')])
        self.assertTrue(apply.__globals__['LEGACY_ENGINE_RETIRED'])
        self.assertFalse(apply.__globals__['STREAM_DISPATCH_ENABLED'])
        with self.assertRaises(ValueError):load_config(KUMO_ENV='TST',KUMO_POSTGRES_ENGINE_PRIMARY='true',KUMO_TASK_PROTOTYPE_ENABLED='true')

    def test_postgres_service_renderer_keeps_private_endpoint_and_persistent_volume(self):
        import importlib.util
        directory=Path(__file__).resolve().parents[2]/'snowflake'/'spcs'
        with patch.object(sys,'path',[str(directory),*sys.path]):
            spec=importlib.util.spec_from_file_location('postgres_renderer',directory/'render_postgres_service.py')
            renderer=importlib.util.module_from_spec(spec);spec.loader.exec_module(renderer)
            sql=renderer.render('/kumo_shared/monitor_app/monitor_repository/kumo-monitor-postgres:pg17-test')
        self.assertIn('source: block',sql);self.assertIn('public: false',sql)
        self.assertIn('KUMO_MONITOR_POSTGRES_CREDENTIALS',sql);self.assertNotIn('CREATE OR REPLACE',sql)
        self.assertNotIn('PASSWORD:',sql)
        with self.assertRaises(ValueError):renderer.render('/repo/image:latest; DROP SERVICE X')

    def test_spcs_credentials_are_read_from_secrets_without_a_dsn_in_the_spec(self):
        from psycopg.conninfo import conninfo_to_dict
        with patch.dict(os.environ,{'KUMO_ENV':'DEV','KUMO_TASK_PROTOTYPE_ENABLED':'true','KUMO_POSTGRES_DSN':'',
                'KUMO_POSTGRES_HOST':'postgres.internal','KUMO_POSTGRES_USER':'engine','KUMO_POSTGRES_PASSWORD':'x y \' z'},clear=True):
            values=conninfo_to_dict(Settings.from_environment().dsn)
        self.assertEqual(values['password'],"x y ' z")
        self.assertEqual(values['host'],'postgres.internal')

    def test_business_execution_requires_explicit_opt_in(self):
        with patch.dict(os.environ, KUMO_PG_BUSINESS_JOBS_ENABLED='false'):
            with self.assertRaises(ValueError):
                normalize(dict(requestId='dbt', jobs=[dict(command=dbt_command())]))

    @patch.dict(os.environ, KUMO_PG_BUSINESS_JOBS_ENABLED='true')
    def test_portable_dbt_and_business_sql_freeze_sequential_dependencies(self):
        _, plan = normalize(dict(requestId='mixed', jobs=[dict(key='sql',command=dict(workflowType='SQL',sqlCommand='SELECT * FROM [DATABASE].META.JOB')),
            dict(key='dbt',type='DBT',command=dbt_command(),timeoutSeconds=3600)]))
        self.assertEqual(plan['jobs'][1]['dependsOn'], ['sql'])
        self.assertEqual(plan['jobs'][1]['type'], 'DBT')
        self.assertEqual(plan['jobs'][1]['timeoutSeconds'], 3600)
        self.assertNotIn('sql', plan['jobs'][1])

    @patch.dict(os.environ, KUMO_PG_BUSINESS_JOBS_ENABLED='true')
    def test_malformed_commands_never_become_native_tasks(self):
        for command in [dict(workflowType='COMMAND',sqlCommand='SELECT 1'), dict(dbt_command(),sqlCommand='DROP DATABASE X'),
                        dict(dbt_command(),dbtCommand=''), dict(dbt_command(),dbtProjectFqn='KUMO_TST.DBT_APP.PROJECT')]:
            with self.subTest(command=command), self.assertRaises(ValueError):
                normalize(dict(requestId='bad',jobs=[dict(command=command)]))

    def test_native_body_uses_frozen_definition_and_preserves_native_errors(self):
        command=dict(dbt_command(),dbtCommand='dbt compile --vars \'{"text":"O\'Brien\\\\x"}\'')
        run=uuid.uuid4(); body=body_for(dict(snapshot=dict(command=command),run_id=run))
        self.assertIn('SP_RESOLVE_WORKFLOW_COMMAND',body)
        self.assertIn(str(run),body)
        self.assertIn("O''Brien",body)
        self.assertIn('RAISE resolution_failed',body)
        self.assertIn('EXECUTE IMMEDIATE command_sql',body)
        self.assertNotIn('WHEN OTHER',body)

    def test_all_three_processes_use_the_maintained_application(self):
        processes=child_commands()
        self.assertEqual(set(processes),{'web','worker','scheduler'})
        self.assertIn('backend.app:app',processes['web'])
        self.assertEqual(processes['worker'][-2:],['task-engine','work'])
        self.assertEqual(processes['scheduler'][-2:],['task-engine','schedule'])

    def test_child_exit_stops_every_remaining_process(self):
        dead=MagicMock();dead.poll.return_value=0
        live=MagicMock();live.poll.return_value=None
        stop=MagicMock();stop.wait.return_value=False
        self.assertEqual(supervise({'worker':['work'],'web':['web']},stop,popen=MagicMock(side_effect=[dead,live])),1)
        live.terminate.assert_called_once();dead.terminate.assert_not_called()

    def test_shutdown_forwards_termination_without_replaying_work(self):
        process=MagicMock();process.poll.return_value=None
        stop=threading.Event();stop.set()
        self.assertEqual(supervise({'worker':['work']},stop,popen=MagicMock(return_value=process)),0)
        process.terminate.assert_called_once();process.kill.assert_not_called()


@unittest.skipUnless(os.getenv('KUMO_TASK_ENGINE_TEST_DSN'),'Requires isolated PostgreSQL test database')
class BusinessPostgresTests(unittest.TestCase):
    setUp=fixtures.PostgresMonitorTests.setUp
    start=fixtures.PostgresMonitorTests.start
    complete=fixtures.PostgresMonitorTests.complete

    @patch.dict(os.environ, KUMO_PG_BUSINESS_JOBS_ENABLED='true')
    def test_recompile_enables_dbt_without_changing_accepted_sql_snapshots(self):
        old=self.start();frozen=deepcopy(self.graphs.read(old)['cascade']['snapshot'])
        updated=self.config.recompile('SPCS_RUNTIME')
        self.assertEqual(updated['revision'],2)
        self.assertEqual(self.graphs.read(old)['cascade']['snapshot'],frozen)
        self.assertEqual(self.config.recompile('SPCS_RUNTIME')['revision'],2)
        self.assertTrue(next(r for r in self.monitor.payload()['rows'] if r['WORKFLOW_NAME']=='DBT workflow')['canRun'])

    @patch.dict(os.environ, KUMO_PG_BUSINESS_JOBS_ENABLED='true')
    def test_dbt_uses_native_worker_lifecycle_and_monitor_keeps_job_type(self):
        self.config.recompile('SPCS_RUNTIME')
        group=self.start('DBT workflow',include_children=False);dispatcher=DependencyWorker(self.store);dispatcher.step()
        node=self.graphs.read(group)['nodes'][0];run=node['run_id']
        tasks=FakeTasks(self.store);worker=Worker(self.store,tasks)
        worker.step()
        self.assertEqual(len(tasks.executions),1)
        tasks.native[next(iter(tasks.native))]['state']='SUCCEEDED'
        worker.step();worker.step();dispatcher.step()
        row=next(w for w in Monitor(self.store).payload()['rows'] if w['WORKFLOW_NAME']=='DBT workflow')
        self.assertEqual(row['run']['status'],'SUCCESS')
        self.assertEqual(row['run']['jobs'][0]['type'],'DBT')
        self.assertEqual(self.monitor.log(self.ids['DBT workflow'],row['run']['runId'])['history']['WORKFLOW_TYPE'],'DBT')
        self.assertEqual(self.store.read(run)['jobs'][0]['snapshot']['command'],dbt_command() | {'dbtCommand':'dbt build'})
        self.assertEqual(self.store.read(run)['jobs'][0]['snapshot']['timeoutSeconds'],21600)
        self.assertEqual(len(tasks.executions),1);self.assertEqual(len(tasks.drops),1)

    def test_readiness_requires_database_master_and_both_process_heartbeats(self):
        self.assertFalse(readiness(self.store)['ok'])
        DependencyWorker(self.store).step(execution_enabled=False)
        from task_engine.schedules import Scheduler
        Scheduler(self.store).step(execution_enabled=False)
        with patch('snowflake_client.connection',side_effect=AssertionError('Readiness must not query Snowflake')):
            self.assertTrue(readiness(self.store)['ok'])
        with self.store.connect() as conn:
            conn.execute("UPDATE kumo_task_engine.scheduler_workers SET last_seen=clock_timestamp()-interval '31 seconds'")
        self.assertFalse(readiness(self.store)['checks']['scheduler'])


if __name__=='__main__':unittest.main()
