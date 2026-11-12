import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app as application
import config
import orchestration_engine as engine
import orchestration_schedules as schedules
import orchestration_observability as details
import orchestration_monitor as monitor
import snowflake_client as sf

WF='00000000-0000-0000-0000-000000000001'
JOB='00000000-0000-0000-0000-000000000002'
RUN='00000000-0000-0000-0000-000000000003'
EXEC='00000000-0000-0000-0000-000000000004'


def workflow(**values):
    return dict(WORKFLOW_ID=WF,WORKFLOW_NAME='Fixture',IS_ENABLED=values.get('IS_ENABLED',True),
                IS_SCHEDULE_ENABLED=values.get('IS_SCHEDULE_ENABLED',True),SCHEDULE='0 7 * * 1-5',
                SCHEDULE_TIMEZONE='Europe/Stockholm',WAREHOUSE_NAME='COMPUTE_WH',TASK_NAME=schedules.scheduler_fqn(WF))


def task(**values):
    return dict(name=schedules.scheduler_fqn(WF).rsplit('.',1)[1],
                comment='KUMO parallel orchestration v2: persistent scheduled workflow',
                definition=f"CALL {config.APP_NAMESPACE}.SP_ORCH_RUN_SCHEDULED('{WF}')",
                schedule='USING CRON 0 7 * * 1-5 Europe/Stockholm',warehouse='COMPUTE_WH',state='started',**values)


class ScheduleTests(unittest.TestCase):
    def test_enabling_creates_only_one_workflow_scheduler_and_resumes_without_manual_execution(self):
        with patch.object(sf,'query_service',return_value=[]),patch.object(sf,'execute_service') as execute:
            result=schedules.sync_workflow(workflow())
        sql=[call.args[0] for call in execute.call_args_list]
        self.assertEqual(sum(value.startswith('CREATE TASK') for value in sql),1)
        self.assertIn(f"SP_ORCH_RUN_SCHEDULED('{WF}')",sql[0])
        self.assertEqual(result['state'],'STARTED')
        self.assertTrue(any(value.endswith(' RESUME') for value in sql))
        self.assertFalse(any('EXECUTE TASK' in value or 'TMP_ORCH_' in value for value in sql))

    def test_disabling_keeps_the_scheduler_and_does_not_cancel_active_work(self):
        with patch.object(sf,'query_service',return_value=[task()]),patch.object(sf,'execute_service') as execute:
            schedules.sync_workflow(workflow(IS_SCHEDULE_ENABLED=False))
        self.assertEqual([call.args[0] for call in execute.call_args_list],['ALTER TASK '+schedules.scheduler_fqn(WF)+' SUSPEND'])

    def test_disabled_workflow_pauses_its_enabled_schedule(self):
        with patch.object(sf,'query_service',return_value=[task()]),patch.object(sf,'execute_service') as execute:
            result=schedules.sync_workflow(workflow(IS_ENABLED=False))
        self.assertEqual(result['state'],'SUSPENDED')
        self.assertFalse(any(call.args[0].endswith(' RESUME') for call in execute.call_args_list))

    def test_unexpected_native_task_is_never_modified(self):
        wrong=task();wrong['definition']='SELECT 1'
        with patch.object(sf,'query_service',return_value=[wrong]),patch.object(sf,'execute_service') as execute:
            with self.assertRaises(schedules.ScheduleConflict):schedules.sync_workflow(workflow())
        execute.assert_not_called()

    def test_next_run_comes_from_scoped_native_scheduler_and_disabled_schedules_are_not_queried(self):
        with patch.object(sf,'query_service',side_effect=[[task()],[{'NEXT_RUN':'2026-11-11T06:00:00+00:00'}]]) as query:
            rows,warnings=schedules.read_schedules({'workflows':[workflow()]})
        self.assertEqual(rows[WF]['nextRun'],'2026-11-11T06:00:00+00:00');self.assertEqual(warnings,[])
        self.assertEqual(query.call_args.args[1]['database'],config.DB)
        self.assertEqual(query.call_args.args[1]['schema'],config.SCHEMA)
        self.assertIn("STATE='SCHEDULED'",query.call_args.args[0])
        with patch.object(sf,'query_service') as query:
            self.assertEqual(schedules.read_schedules({'workflows':[workflow(IS_SCHEDULE_ENABLED=False)]}),({},[]))
        query.assert_not_called()

    def test_scheduler_sync_rechecks_saved_intent_after_ddl(self):
        first=workflow();latest=workflow(IS_ENABLED=False)
        with patch.object(sf,'query_service',side_effect=[[first],[latest],[latest],[latest]]),patch.object(schedules,'sync_workflow',return_value={}) as sync:
            schedules.sync_saved_workflow(WF)
        self.assertEqual([call.args[0]['IS_ENABLED'] for call in sync.call_args_list],[True,False])


class ControlApiTests(unittest.TestCase):
    def setUp(self):
        for patcher in (patch.object(config,'USE_MOCK',False),patch.object(config,'ORCHESTRATION_RUNS_ENABLED',True),
                        patch.object(sf,'is_configured',return_value=True),patch.object(application,'_renew_activity_lease')):
            patcher.start();self.addCleanup(patcher.stop)
        self.client=application.app.test_client()

    def test_quick_toggle_changes_only_the_flag_and_rejects_stale_state(self):
        for kind,path in [('job',f'/api/orchestration/jobs/{JOB}/state'),('workflow',f'/api/orchestration/workflows/{WF}/state')]:
            with patch.object(sf,'connection_scope'),patch.object(sf,'query_service',return_value=[dict(IS_ENABLED=True)]),patch.object(sf,'execute_service') as execute,patch.object(schedules,'sync_saved_workflow',return_value=None):
                response=self.client.patch(path,json={'enabled':False,'expectedEnabled':True})
            self.assertEqual(response.status_code,200,response.get_json())
            updates=[call.args[0] for call in execute.call_args_list if call.args[0].startswith('UPDATE') and 'IS_ENABLED=' in call.args[0]]
            self.assertEqual(len(updates),1);self.assertNotIn('SQL_COMMAND',updates[0]);self.assertNotIn('SCHEDULE=',updates[0])
            with patch.object(sf,'connection_scope'),patch.object(sf,'query_service',return_value=[dict(IS_ENABLED=False)]),patch.object(sf,'execute_service') as execute:
                response=self.client.patch(path,json={'enabled':False,'expectedEnabled':True})
            self.assertEqual(response.status_code,409,response.get_json())
            self.assertTrue(any(call.args[0]=='ROLLBACK' for call in execute.call_args_list))

    def test_missing_cron_cannot_enable_a_schedule(self):
        with patch.object(sf,'connection_scope'),patch.object(sf,'query_service',return_value=[dict(IS_SCHEDULE_ENABLED=False)]),patch.object(sf,'execute_service') as execute:
            response=self.client.patch(f'/api/orchestration/workflows/{WF}/schedule',json={'enabled':True})
        self.assertEqual(response.status_code,400,response.get_json())
        self.assertFalse(any('IS_SCHEDULE_ENABLED=' in call.args[0] for call in execute.call_args_list))

    def test_dag_api_returns_success_and_does_not_substitute_another_jobs_run(self):
        with patch.object(sf,'connection_scope'),patch.object(details,'dag_payload',return_value=dict(nodes=[],edges=[],run=None)) as read:
            response=self.client.get(f'/api/orchestration/workflows/{WF}/dag?runId={RUN}&jobId={JOB}')
        self.assertEqual(response.status_code,200,response.get_json())
        self.assertTrue(response.get_json()['ok'])
        read.assert_called_once_with(WF,RUN,JOB)
        with patch.object(sf,'connection_scope'),patch.object(details,'dag_payload',side_effect=details.ExecutionNotFound('No job execution')):
            response=self.client.get(f'/api/orchestration/workflows/{WF}/dag?runId={RUN}&jobId={JOB}')
        self.assertEqual(response.status_code,404)

    def test_configuration_preview_reads_the_saved_job_without_starting_a_run(self):
        import kumo_repository as repo
        job = dict(JOB_ID=JOB, JOB_NAME='Saved selection', JOB_TYPE='DBT', DBT_COMMAND='build --select tag:sdl',
                   DBT_PROJECT_FQN='[DATABASE].[DBT_SCHEMA].[DBT_PROJECT]', DBT_TARGET='[DBT_TARGET]')
        with patch.object(sf,'connection_scope'),patch.object(sf,'query_service',return_value=[job]) as read, \
             patch.object(sf,'execute_service') as write,patch.object(repo,'preview_dbt_definition',return_value=dict(nodes=[],edges=[])) as preview:
            response=self.client.post(f'/api/orchestration/jobs/{JOB}/dag-preview')
        self.assertEqual(response.status_code,200,response.get_json())
        self.assertEqual(read.call_args.args[1],{'job':JOB})
        self.assertIn(config.T_ORCHESTRATION_JOB,read.call_args.args[0])
        self.assertEqual(preview.call_args.args[0]['DBT_COMMAND'],job['DBT_COMMAND'])
        self.assertEqual(preview.call_args.args[0]['WORKFLOW_TYPE'],'DBT')
        write.assert_not_called()

    def test_configuration_preview_rejects_sql_and_missing_jobs(self):
        import kumo_repository as repo
        for rows,status in [([],404),([dict(JOB_TYPE='SQL')],400)]:
            with patch.object(sf,'connection_scope'),patch.object(sf,'query_service',return_value=rows), \
                 patch.object(repo,'preview_dbt_definition') as preview:
                response=self.client.post(f'/api/orchestration/jobs/{JOB}/dag-preview')
            self.assertEqual(response.status_code,status,response.get_json())
            preview.assert_not_called()


class DetailTests(unittest.TestCase):
    def test_logs_read_only_job_execution_ids_and_never_the_legacy_history_or_parent_run_id(self):
        root=dict(EXECUTION_ID=RUN,WORKFLOW_ID=WF,STATUS='SUCCESS',CONFIG_SNAPSHOT={'workflow':{'WORKFLOW_NAME':'Fixture'}})
        job=dict(EXECUTION_ID=EXEC,JOB_ID=JOB,STATUS='SUCCESS',CONFIG_SNAPSHOT={'definition':{'JOB_NAME':'dbt job','JOB_TYPE':'DBT'}})
        with patch.object(sf,'query_service',side_effect=[[root],[job],[],[],[]]) as query:
            result=details.log_payload(WF,RUN,JOB)
        self.assertEqual(result['history']['RUN_ID'],EXEC)
        self.assertEqual(result['jobExecutions'][0]['JOB_NAME'],'dbt job')
        for call in query.call_args_list[2:]:
            self.assertEqual(call.args[1],{'job_0':EXEC})
            self.assertNotIn('WORKFLOW_HISTORY',call.args[0])
        self.assertEqual(query.call_args_list[1].args[1],dict(workflow=WF,run=RUN,job=JOB))

    def test_job_from_another_execution_is_not_substituted(self):
        root=dict(EXECUTION_ID=RUN,WORKFLOW_ID=WF)
        with patch.object(sf,'query_service',side_effect=[[root],[]]):
            with self.assertRaises(details.ExecutionNotFound):details.log_payload(WF,RUN,JOB)

    def test_dag_shows_frozen_workflow_job_order_and_skip_states(self):
        root=dict(EXECUTION_ID=RUN,WORKFLOW_ID=WF,STATUS='SUCCESS',CONFIG_SNAPSHOT={'workflow':{'WORKFLOW_NAME':'Fixture'}})
        jobs=[dict(EXECUTION_ID=EXEC,JOB_ID=JOB,PARENT_EXECUTION_ID=RUN,STATUS='SKIPPED',CONFIG_SNAPSHOT={'definition':{'JOB_NAME':'Skipped job','JOB_TYPE':'SQL'}})]
        with patch.object(sf,'query_service',side_effect=[[root],jobs]):result=details.dag_payload(WF,RUN)
        self.assertEqual(result['nodes'][1]['status'],'SKIPPED')
        self.assertEqual(result['edges'],[{'source':WF,'target':JOB}])


if __name__=='__main__':unittest.main()
