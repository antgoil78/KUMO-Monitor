import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as application
import config
import orchestration_configuration as catalog_store
import orchestration_monitor as monitor
import snowflake_client as sf


def catalog():
    value = dict(workflows=[dict(WORKFLOW_ID=key, WORKFLOW_NAME=key, IS_ENABLED=True) for key in ('one', 'two')],
                 jobs=[dict(JOB_ID=key, JOB_NAME=key, IS_ENABLED=True) for key in ('shared', 'second')],
                 raw_records=[dict(WORKFLOW_ID='one', JOB_ID='shared', PARENT_ID='one'),
                              dict(WORKFLOW_ID='one', JOB_ID='second', PARENT_ID='shared'),
                              dict(WORKFLOW_ID='two', JOB_ID='shared', PARENT_ID='two')])
    value.update(catalog_store.normalize_dependencies(value))
    return value


def execution(workflow, run, job=None, status='SUCCESS', **fields):
    return dict(WORKFLOW_ID=workflow, WORKFLOW_RUN_ID=run, EXECUTION_ID=run if job is None else run + job,
                JOB_ID=job, STATUS=status, **fields)


class MonitorRowsTests(unittest.TestCase):
    def test_never_run_workflows_keep_real_assignments_without_invented_results(self):
        rows = monitor.build_rows(catalog(), [])
        self.assertEqual([len(row['jobs']) for row in rows], [2, 1])
        for workflow in rows:
            self.assertEqual(workflow['run']['status'], 'NOT RUN')
            self.assertEqual(workflow['progress']['total'], 0)
            for job in workflow['jobs']:
                self.assertEqual(job['run']['status'], 'NOT RUN')
                self.assertIsNone(job['run']['start'])

    def test_shared_jobs_and_partial_runs_do_not_borrow_other_execution_results(self):
        rows = monitor.build_rows(catalog(), [
            execution('one', 'r1', RUN_MODE='JOB_ONLY'), execution('one', 'r1', 'shared', JOB_POSITION=1),
            execution('one', 'older', 'second', status='FAILED'),
            execution('two', 'r2', status='RUNNING', QUEUED_COUNT=2),
            execution('two', 'r2', 'shared', status='RUNNING', JOB_POSITION=1),
        ])
        self.assertEqual([job['run']['status'] for job in rows[0]['jobs']], ['SUCCESS', 'NOT SELECTED'])
        self.assertEqual(rows[0]['progress'], dict(total=1, succeeded=1, failed=0, skipped=0, cancelled=0, completed=1, percent=100))
        self.assertEqual(rows[1]['jobs'][0]['run']['status'], 'RUNNING')
        self.assertEqual(rows[1]['queuedCount'], 2)

    def test_removed_jobs_are_visible_using_the_execution_snapshot(self):
        rows = monitor.build_rows(catalog(), [execution('one', 'r1'),
            execution('one', 'r1', 'removed', RUN_JOB_NAME='Historical job', JOB_POSITION=3)])
        job = rows[0]['jobs'][-1]
        self.assertEqual(job['JOB_NAME'], 'Historical job')
        self.assertTrue(job['runOnly'])
        self.assertEqual(job['run']['status'], 'SUCCESS')
        self.assertEqual(rows[0]['progress']['total'], 1)

    def test_every_job_has_its_own_run_status_and_progress(self):
        rows = monitor.build_rows(catalog(), [execution('one', 'r1', status='RUNNING'),
            execution('one', 'r1', 'shared', status='RUNNING'),
            execution('one', 'r1', 'second', status='PENDING'),
            execution('two', 'r2'), execution('two', 'r2', 'shared', status='SKIPPED')],
            {'r1shared': dict(percent=50, total=4, completed=2)})
        self.assertEqual(rows[0]['jobs'][0]['progress']['percent'], 50)
        self.assertEqual(rows[0]['jobs'][1]['run']['status'], 'PENDING')
        self.assertEqual(rows[0]['progress']['percent'], 25)
        self.assertIsNone(rows[1]['jobs'][0]['progress'])
        self.assertEqual(rows[1]['progress']['percent'], 100)

    def test_no_progress_is_not_an_invented_model_count(self):
        row = monitor.build_rows(catalog(), [execution('one', 'r1', status='RUNNING'),
            execution('one', 'r1', 'shared', status='RUNNING')], {'r1shared': dict(percent=None)})[0]
        self.assertEqual(row['progress']['percent'], 0)

    def test_last_run_keeps_the_latest_started_job_separate_from_current_skipping(self):
        started = datetime(2026,11,9,12)
        row = monitor.build_rows(catalog(), [execution('one','new',status='QUEUED'),
            execution('one','new','shared',status='SKIPPED'),
            execution('one','new','second',status='QUEUED')],
            last_runs=[execution('one','old','shared',STARTED_AT=started,ENDED_AT=datetime(2026,11,9,12,1))])[0]
        self.assertEqual(row['jobs'][0]['run']['status'],'SKIPPED')
        self.assertEqual(row['jobs'][0]['lastRun']['runId'],'old')
        self.assertEqual(row['jobs'][0]['lastRun']['start'],'2026-11-09T12:00:00+00:00')
        self.assertIsNone(row['jobs'][1]['lastRun']['start'])


class DbtProgressTests(unittest.TestCase):
    def test_reads_only_dbt_job_execution_ids_and_combines_models_and_tests(self):
        executions = [execution('one', 'r1'), execution('one', 'r1', 'shared', status='RUNNING', RUN_JOB_TYPE='DBT'),
                      execution('one', 'r1', 'sql', status='RUNNING', RUN_JOB_TYPE='SQL'),
                      execution('one', 'r1', 'skipped', status='SKIPPED', RUN_JOB_TYPE='DBT')]
        models = [dict(RUN_ID='r1shared', TOTAL=4, DONE=1, COMPLETED=2, RUNNING=1, FAILED=1)]
        tests = [dict(RUN_ID='r1shared', TOTAL=2, DONE=1, COMPLETED=1, QUEUED=1)]
        with patch.object(sf, 'query_service', side_effect=[models, tests]) as query:
            progress, warnings = monitor.read_dbt_progress(executions)
        self.assertEqual(warnings, [])
        self.assertEqual(progress['r1shared']['percent'], 50)
        self.assertEqual(progress['r1shared']['models']['failed'], 1)
        self.assertEqual(progress['r1shared']['tests']['queued'], 1)
        for call in query.call_args_list:
            self.assertEqual(call.args[1], {'run_0': 'r1shared'})
            self.assertIn('ROW_NUMBER() OVER (PARTITION BY RUN_ID, MODEL_NAME', call.args[0])

    def test_progress_telemetry_failure_does_not_hide_workflow_status(self):
        with patch.object(sf, 'query_service', side_effect=RuntimeError('unavailable')):
            progress, warnings = monitor.read_dbt_progress([
                execution('one', 'r1', 'shared', status='RUNNING', RUN_JOB_TYPE='DBT')])
        self.assertFalse(progress)
        self.assertEqual(len(warnings), 2)

    def test_no_dbt_execution_does_not_query_telemetry(self):
        with patch.object(sf, 'query_service') as query:
            self.assertEqual(monitor.read_dbt_progress([]), ({}, []))
        query.assert_not_called()

    def test_snowflake_ntz_execution_times_are_explicitly_utc(self):
        state = monitor.run_state(execution('one', 'r1', STARTED_AT=datetime(2026, 7, 1, 12)))
        self.assertEqual(state['start'], '2026-07-01T12:00:00+00:00')

    def test_live_state_preserves_the_saved_execution_parent_and_selection(self):
        state = monitor.run_state(execution('one', 'r1', 'shared', PARENT_EXECUTION_ID='parent-job', RUN_SELECTED=True))
        self.assertEqual(state['parentExecutionId'], 'parent-job')
        self.assertTrue(state['selected'])
        with patch.object(sf, 'query_service', return_value=[]) as query:
            monitor.read_executions()
        self.assertIn('l.PARENT_EXECUTION_ID', query.call_args.args[0])

    def test_workflow_parent_and_cancelled_job_remain_distinct_from_skipping(self):
        value = catalog()
        value['raw_records'].append(dict(WORKFLOW_ID='two', JOB_ID=None, PARENT_ID='one', SUCCESS=True, ERROR=False))
        value.update(catalog_store.normalize_dependencies(value))
        row = monitor.build_rows(value, [execution('two', 'r2', status='CANCELLED'),
                                       execution('two', 'r2', 'shared', status='CANCELLED')])[1]
        self.assertEqual(row['parentWorkflowName'], 'one')
        self.assertFalse(row['dependency']['ERROR'])
        self.assertEqual(row['progress']['cancelled'], 1)
        self.assertEqual(row['progress']['skipped'], 0)

    def test_run_controls_need_enabled_manual_workflow_and_enabled_assigned_jobs(self):
        value = catalog()
        for row in value['workflows']:
            row['ALLOW_MANUAL_TRIGGER'] = True
        with patch.object(config, 'ORCHESTRATION_RUNS_ENABLED', True):
            self.assertTrue(monitor.build_rows(value, [])[0]['canRun'])
            value['jobs'][0]['IS_ENABLED'] = False
            self.assertFalse(monitor.build_rows(value, [])[1]['canRun'])
            value['workflows'][0]['ALLOW_MANUAL_TRIGGER'] = False
            self.assertFalse(monitor.build_rows(value, [])[0]['canRun'])


class MonitorApiTests(unittest.TestCase):
    def setUp(self):
        patches = [patch.object(config, 'USE_MOCK', False), patch.object(sf, 'is_configured', return_value=True),
                   patch.object(application, '_renew_activity_lease')]
        for patcher in patches:
            patcher.start(); self.addCleanup(patcher.stop)
        self.client = application.app.test_client()

    def test_monitor_reads_only_the_four_parallel_tables_in_the_selected_environment(self):
        tables = {'T_ORCHESTRATION_WORKFLOW': 'WORKFLOW', 'T_ORCHESTRATION_JOB': 'JOB',
                  'T_ORCHESTRATION_DEPENDENCY': 'DEPENDENCIES', 'T_ORCHESTRATION_EXECUTION_LOG': 'WORKFLOW_EXECUTION_LOG'}
        settings = {key: 'KUMO_TST.MONITOR_META.' + value for key, value in tables.items()}
        settings['ORCHESTRATION_NAMESPACE'] = 'KUMO_TST.MONITOR_META'
        with patch.multiple(config, **settings), patch.object(sf, 'connection_scope') as scope, \
                patch.object(sf, 'query_service', return_value=[]) as query, patch.object(sf, 'execute_service') as execute:
            result = monitor.payload()
        self.assertEqual(result['rows'], [])
        scope.assert_called_once_with(force_service=True)
        self.assertEqual(query.call_count, 4)
        sql = '\n'.join(call.args[0] for call in query.call_args_list)
        for table in tables.values():
            self.assertIn('KUMO_TST.MONITOR_META.' + table, sql)
        self.assertNotIn('KUMO_DEV.', sql)
        self.assertNotIn('WORKFLOW_HISTORY', sql)
        self.assertNotIn('WORKFLOW_RUN_QUEUE', sql)
        execute.assert_not_called()

    def test_http_snapshot_does_not_query_snowflake_and_manual_refresh_only_wakes_shared_reader(self):
        import orchestration_engine as engine
        from orchestration_cache import OrchestrationCache
        cache = OrchestrationCache()
        with patch.object(engine, 'orchestration_cache', cache), patch.object(sf, 'query_service') as query:
            for _ in range(3):
                response = self.client.get('/api/orchestration/monitor')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()['transport'], 'sse')
            self.assertFalse(cache._wake.is_set())
            self.client.get('/api/orchestration/monitor?refresh=1')
            self.assertTrue(cache._wake.is_set())
        query.assert_not_called()

    def test_queued_claimed_and_running_workflows_cannot_be_run_again(self):
        value = catalog()
        for row in value['workflows']: row['ALLOW_MANUAL_TRIGGER'] = True
        with patch.object(config, 'ORCHESTRATION_RUNS_ENABLED', True):
            for status in ('INITIATING', 'QUEUED', 'CLAIMED', 'RUNNING'):
                row = monitor.build_rows(value, [execution('one','active',status=status)])[0]
                self.assertFalse(row['canRun'], status)
                self.assertIn('already queued or running', row['runUnavailable'])
            self.assertTrue(monitor.build_rows(value, [execution('one','done',status='SUCCESS')])[0]['canRun'])

    def test_mock_mode_requires_a_connection_instead_of_returning_sample_states(self):
        with patch.object(config, 'USE_MOCK', True), patch.object(sf, 'query_service') as query:
            response = self.client.get('/api/orchestration/monitor')
        self.assertEqual(response.status_code, 503)
        query.assert_not_called()


if __name__ == '__main__':
    unittest.main()
