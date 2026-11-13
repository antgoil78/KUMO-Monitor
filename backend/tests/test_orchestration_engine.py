import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as application
import config
import orchestration_engine as engine
import snowflake_client as sf
from orchestration_cache import OrchestrationCache

WORKFLOW = '00000000-0000-0000-0000-000000000001'
JOB = '00000000-0000-0000-0000-000000000002'
RUN = '00000000-0000-0000-0000-000000000003'


class ParallelEngineTests(unittest.TestCase):
    def setUp(self):
        patches = [patch.object(config, 'USE_MOCK', False), patch.object(config, 'ORCHESTRATION_RUNS_ENABLED', True),
                   patch.object(config, 'APP_NAMESPACE', 'KUMO_DEV.MONITOR_APP'),
                   patch.object(config, 'T_ORCHESTRATION_EXECUTION_LOG', 'KUMO_DEV.MONITOR_META.WORKFLOW_EXECUTION_LOG'),
                   patch.object(sf, 'is_configured', return_value=True),
                   patch.object(application, '_renew_activity_lease'),
                   patch.object(engine.realtime_broker, 'wait_for_clients'),
                   patch.object(engine, 'orchestration_cache', OrchestrationCache()),
                   patch.dict(application.app.extensions, {'orchestration_actor_context': lambda: {'userName': 'TRUSTED_USER'}})]
        for patcher in patches:
            patcher.start(); self.addCleanup(patcher.stop)
        self.client = application.app.test_client()
        self.payload = dict(mode='JOB_ONLY', jobId=JOB, requestId='stable-request-id', requestedBy='SPOOFED_USER')

    def query(self, sql, params=None):
        if 'SP_ORCH_RECONCILE' in sql:
            return {'RESULT': {'ok': True, 'recoveredRunIds': []}}
        if 'SP_ORCH_START_RUN' in sql:
            return {'RESULT': {'ok': True, 'wakeupScheduled': True}}
        return {'RESULT': {'ok': True, 'runId': RUN, 'status': 'QUEUED', 'taskFqn': engine.task_fqn(WORKFLOW, RUN)}}

    def test_disabled_engine_cannot_enqueue_or_start_a_task(self):
        with patch.object(config, 'ORCHESTRATION_RUNS_ENABLED', False), patch.object(sf, 'query_one_service') as query, patch.object(sf, 'execute_service') as execute:
            response = self.client.post(f'/api/orchestration/workflows/{WORKFLOW}/runs', json=self.payload)
        self.assertEqual(response.status_code, 503)
        query.assert_not_called(); execute.assert_not_called()

    def test_all_modes_submit_one_durable_query_without_waiting_for_preparation_or_task_creation(self):
        for mode in ('WORKFLOW', 'JOB_ONLY', 'FROM_JOB'):
            body = dict(self.payload, mode=mode, jobId=None if mode == 'WORKFLOW' else JOB)
            with self.subTest(mode=mode), patch.object(engine, 'orchestration_cache', OrchestrationCache()), \
                 patch.object(sf, 'submit_service_query', return_value='native-query-id') as submit, \
                 patch.object(sf, 'query_one_service') as query, patch.object(engine.threading, 'Thread') as thread:
                response = self.client.post(f'/api/orchestration/workflows/{WORKFLOW}/runs', json=body)
                self.assertEqual(response.status_code, 202, response.get_json())
                self.assertEqual(response.get_json()['queryId'], 'native-query-id')
                self.assertEqual(response.get_json()['status'], 'INITIATING')
                self.assertTrue(response.get_json()['accepted'])
                self.assertIn('SP_ORCH_REQUEST_RUN', submit.call_args.args[0])
                self.assertEqual(submit.call_args.args[1]['actor'], 'TRUSTED_USER')
                query.assert_not_called()
                thread.return_value.start.assert_called_once()

    def test_pending_duplicate_does_not_submit_another_query(self):
        with patch.object(sf, 'submit_service_query', return_value='native-query-id') as submit, patch.object(engine.threading, 'Thread'):
            first = engine.request_run(WORKFLOW, self.payload, 'TEST')
            same = engine.request_run(WORKFLOW, self.payload, 'TEST')
            conflict = self.client.post(f'/api/orchestration/workflows/{WORKFLOW}/runs', json=dict(self.payload, requestId='another'))
        self.assertEqual(first['queryId'], same['queryId'])
        self.assertTrue(same['reused']); submit.assert_called_once()
        self.assertEqual(conflict.status_code, 409)

    def test_full_refresh_reaches_the_native_request_and_is_part_of_idempotency(self):
        body = dict(self.payload, fullRefresh=True)
        with patch.object(sf, 'submit_service_query', return_value='query') as submit, patch.object(engine.threading, 'Thread'):
            response = self.client.post(f'/api/orchestration/workflows/{WORKFLOW}/runs', json=body)
            conflict = self.client.post(f'/api/orchestration/workflows/{WORKFLOW}/runs', json=self.payload)
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()['fullRefresh'])
        self.assertTrue(submit.call_args.args[1]['full_refresh'])
        self.assertIn('%(full_refresh)s', submit.call_args.args[0])
        self.assertEqual(conflict.status_code, 409)
        submit.assert_called_once()

    def test_invalid_full_refresh_options_never_submit(self):
        for body in (dict(self.payload, fullRefresh='true'), dict(self.payload, fullRefresh=1),
                     dict(self.payload, fullRefresh=None), dict(self.payload, mode='WORKFLOW', jobId=None, fullRefresh=True)):
            with self.subTest(body=body), patch.object(sf, 'submit_service_query') as submit:
                with self.assertRaises(ValueError): engine.request_run(WORKFLOW, body, 'TEST')
                submit.assert_not_called()

    def test_background_completion_starts_only_the_containing_workflow_task(self):
        engine.orchestration_cache.reserve(WORKFLOW, self.payload, 'TEST')
        with patch.object(sf, 'service_query_result', return_value=self.query('request')), \
             patch.object(sf, 'query_one_service', side_effect=self.query) as query:
            engine.complete_submission(WORKFLOW, self.payload['requestId'], 'native-query-id')
        query.assert_called_once_with(f'CALL {config.APP_NAMESPACE}.SP_ORCH_START_RUN(%(run)s)', {'run': RUN})
        self.assertEqual(engine.orchestration_cache.get()['submissions'][0]['runId'], RUN)

    def test_wakeup_failure_keeps_committed_request_visible_for_native_recovery(self):
        engine.orchestration_cache.reserve(WORKFLOW, self.payload, 'TEST')
        with patch.object(sf, 'service_query_result', return_value=self.query('request')), \
             patch.object(sf, 'query_one_service', side_effect=RuntimeError('network failure')):
            engine.complete_submission(WORKFLOW, self.payload['requestId'], 'native-query-id')
        result = engine.orchestration_cache.get()['submissions'][0]
        self.assertEqual(result['runId'], RUN)
        self.assertEqual(result['status'], 'QUEUED')

    def test_terminal_idempotent_request_does_not_restart_workflow(self):
        result = self.query('request'); result['RESULT'].update(status='SUCCESS', reused=True)
        engine.orchestration_cache.reserve(WORKFLOW, self.payload, 'TEST')
        with patch.object(sf, 'service_query_result', return_value=result), patch.object(sf, 'query_one_service') as query:
            engine.complete_submission(WORKFLOW, self.payload['requestId'], 'native-query-id')
        query.assert_not_called()
        self.assertEqual(engine.orchestration_cache.get()['submissions'][0]['status'], 'SUCCESS')

    def test_unexpected_task_address_is_never_executed(self):
        result = self.query('request'); result['RESULT']['taskFqn'] = 'KUMO_DEV.MONITOR_APP.TASK_WF_MASTER_DISPATCHER'
        engine.orchestration_cache.reserve(WORKFLOW, self.payload, 'TEST')
        with patch.object(sf, 'service_query_result', return_value=result), patch.object(sf, 'query_one_service') as query:
            engine.complete_submission(WORKFLOW, self.payload['requestId'], 'native-query-id')
        query.assert_not_called()
        self.assertEqual(engine.orchestration_cache.get()['submissions'][0]['status'], 'FAILED')

    def test_preparation_rejection_is_published_as_failure_and_unlocks_retry(self):
        engine.orchestration_cache.reserve(WORKFLOW, self.payload, 'TEST')
        with patch.object(sf, 'service_query_result', return_value={'RESULT':dict(ok=False,error='Workflow is disabled')}), \
             patch.object(sf, 'query_one_service') as query:
            engine.complete_submission(WORKFLOW, self.payload['requestId'], 'native-query-id')
        result = engine.orchestration_cache.get()['submissions'][0]
        self.assertEqual(result['status'], 'FAILED'); self.assertIn('disabled', result['error'])
        query.assert_not_called()
        engine.orchestration_cache.reserve(WORKFLOW, self.payload, 'TEST')

    def test_confirmation_recovers_after_a_network_outage_without_resubmitting(self):
        engine.orchestration_cache.reserve(WORKFLOW, self.payload, 'TEST')
        with patch.object(sf, 'service_query_result', side_effect=[RuntimeError('DNS unavailable')]*3+[self.query('request')]), \
             patch.object(sf, 'submit_service_query') as submit, patch.object(sf, 'query_one_service', side_effect=self.query), \
             patch.object(engine.threading, 'Event'):
            engine.complete_submission(WORKFLOW, self.payload['requestId'], 'native-query-id')
        submit.assert_not_called()
        self.assertEqual(engine.orchestration_cache.get()['submissions'][0]['status'], 'QUEUED')

    def test_invalid_modes_job_context_and_identifiers_do_not_reach_snowflake(self):
        for body in (None, [], {'mode': 'FROM_JOB', 'requestId': 'x'},
                     {'mode': 'WORKFLOW', 'jobId': JOB, 'requestId': 'x'},
                     {'mode': 'JOB_ONLY', 'jobId': 'unsafe;drop task', 'requestId': 'x'},
                     {'mode': 'WORKFLOW', 'requestId': ''}):
            with self.subTest(body=body), patch.object(sf, 'submit_service_query') as submit:
                with self.assertRaises((ValueError, TypeError, AttributeError)):
                    engine.request_run(WORKFLOW, body, 'TEST')
                submit.assert_not_called()

    def test_history_reads_only_new_log_with_workflow_context(self):
        rows = [{'CONFIG_SNAPSHOT': '{"definition":{"JOB_NAME":"Snapshot job"}}', 'RESULT': '{"commandCompleted":true}'}]
        with patch.object(sf, 'connection_scope'), patch.object(sf, 'query_service', return_value=rows) as query:
            response = self.client.get(f'/api/orchestration/workflows/{WORKFLOW}/runs?limit=2000')
        self.assertEqual(response.status_code, 200)
        self.assertIn('LIMIT 100', query.call_args.args[0])
        self.assertIn('WORKFLOW_EXECUTION_LOG', query.call_args.args[0])
        self.assertNotIn('WORKFLOW_HISTORY', query.call_args.args[0])
        self.assertEqual(query.call_args.args[1]['workflow'], WORKFLOW)
        self.assertEqual(response.get_json()['rows'][0]['CONFIG_SNAPSHOT']['definition']['JOB_NAME'], 'Snapshot job')

    def test_workflow_cannot_be_deleted_while_a_snapshot_is_waiting_to_run(self):
        def counts(sql, params=None):
            return {'CNT': 0 if 'DEPENDENCIES' in sql else 1}
        with patch.object(engine, 'installed', return_value=True), patch.object(sf, 'connection_scope'), \
                patch.object(sf, 'query_one_service', side_effect=counts), patch.object(sf, 'execute_service') as execute:
            response = self.client.delete(f'/api/orchestration/workflows/{WORKFLOW}')
        self.assertEqual(response.status_code, 409)
        self.assertIn('queued or active', response.get_json()['error'])
        self.assertFalse(any(call.args[0].startswith('DELETE') for call in execute.call_args_list))


if __name__ == '__main__':
    unittest.main()
