import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import orchestration_cache as cache_module
import orchestration_monitor as monitor
import snowflake_client as sf


class SharedMonitorTests(unittest.TestCase):
    def setUp(self):
        clients = patch.object(cache_module.realtime_broker, 'client_count', return_value=1)
        clients.start(); self.addCleanup(clients.stop)
        self.cache = cache_module.OrchestrationCache()
        self.payload = dict(ok=True, rows=[dict(WORKFLOW_ID='wf', canRun=True, run=dict(status='SUCCESS'))],
                            generatedAt='2026-11-10T12:00:00Z', warnings=[])

    def test_one_refresh_pushes_progress_and_many_snapshots_reuse_it(self):
        self.payload['rows'][0]['jobs'] = [dict(progress=dict(models=dict(completed=2,total=3)))]
        with patch.object(monitor, 'payload', return_value=self.payload) as read, \
             patch.object(cache_module.realtime_broker, 'publish') as publish:
            self.cache.refresh()
            for _ in range(5): self.cache.get()
        read.assert_called_once_with(metadata=self.cache._metadata)
        self.assertEqual(publish.call_args.args[0], 'orchestration_update')
        self.assertEqual(publish.call_args.args[1]['rows'][0]['jobs'][0]['progress']['models']['completed'], 2)
        self.assertEqual(self.cache.get()['revision'], 1)

    def test_refresh_failure_keeps_last_successful_rows_and_reports_the_error(self):
        with patch.object(monitor, 'payload', side_effect=[self.payload, RuntimeError('Snowflake unavailable')]):
            self.cache.refresh(); result = self.cache.refresh()
        self.assertEqual(result['rows'], self.payload['rows'])
        self.assertIn('unavailable', result['error'])

    def test_active_execution_and_submission_poll_faster_then_return_to_idle_rate(self):
        self.cache.set_refresh_seconds(10)
        with patch.object(monitor, 'payload', return_value=self.payload): self.cache.refresh()
        self.assertEqual(self.cache.get()['refreshSeconds'], 10)
        self.payload['rows'][0]['run']['status'] = 'RUNNING'
        self.assertEqual(self.cache.get()['refreshSeconds'], 2)
        self.payload['rows'][0]['run']['status'] = 'SUCCESS'
        self.assertEqual(self.cache.get()['refreshSeconds'], 10)
        self.cache.reserve('wf', dict(requestId='request', mode='WORKFLOW'), 'ACTOR')
        self.assertEqual(self.cache.get()['refreshSeconds'], 2)
        self.cache.update_submission('wf', 'request', status='FAILED')
        self.assertEqual(self.cache.get()['refreshSeconds'], 10)

    def test_submission_disables_run_until_its_durable_log_snapshot_is_visible(self):
        request = dict(requestId='request', mode='WORKFLOW', jobId=None)
        with patch.object(monitor, 'payload', return_value=self.payload): self.cache.refresh()
        self.cache.reserve('wf', request, 'ACTOR')
        self.assertFalse(self.cache.get()['rows'][0]['canRun'])
        self.cache.update_submission('wf', 'request', status='QUEUED', runId='run')
        with self.assertRaises(cache_module.SubmissionConflict):
            self.cache.reserve('wf', dict(request, requestId='other'), 'ACTOR')
        self.payload['rows'][0].update(canRun=False, run=dict(status='RUNNING',runId='run',requestId='request'))
        with patch.object(monitor, 'payload', return_value=self.payload): self.cache.refresh()
        self.assertEqual(self.cache.get()['submissions'], [])
        self.assertFalse(self.cache.get()['rows'][0]['canRun'])

    def test_same_request_id_cannot_change_selection_or_actor(self):
        request = dict(requestId='request', mode='WORKFLOW', jobId=None)
        self.cache.reserve('wf', request, 'ACTOR')
        self.cache.update_submission('wf','request',queryId='native-query',status='INITIATING')
        for body, actor in [(dict(request,mode='JOB_ONLY',jobId='job'),'ACTOR'),(request,'SPOOFED')]:
            with self.assertRaises(cache_module.SubmissionConflict): self.cache.reserve('wf',body,actor)

    def test_manual_refresh_invalidates_metadata_but_submission_updates_keep_it(self):
        self.cache._metadata.update(catalog='cached')
        with patch.object(monitor, 'payload', return_value=self.payload):
            self.cache.refresh_async(metadata=False)
            self.cache.refresh()
            self.assertEqual(self.cache._metadata, dict(catalog='cached'))
            self.cache.refresh_async()
            self.cache.refresh()
            self.assertEqual(self.cache._metadata, {})

    def test_worker_reuses_one_connection_and_closes_it_when_stopped(self):
        self.cache._enabled = True
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 3:
                self.cache.stop()
        with patch.object(sf, 'connection_scope') as scope, \
             patch.object(monitor, 'payload', return_value=self.payload) as read, \
             patch.object(self.cache._wake, 'wait', side_effect=wait):
            self.cache._loop()
        self.assertEqual(read.call_count, 3)
        scope.assert_called_once_with(force_service=True)
        scope.return_value.__exit__.assert_called_once()
        self.assertFalse(self.cache.diagnostics()['enabled'])

    def test_failed_poll_preserves_rows_then_reconnects_and_recovers(self):
        self.cache._enabled = True
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 3:
                self.cache.stop()
        with patch.object(sf, 'connection_scope') as scope, \
             patch.object(monitor, 'payload', side_effect=[self.payload, RuntimeError('connection lost'), self.payload]), \
             patch.object(self.cache._wake, 'wait', side_effect=wait):
            self.cache._loop()
        self.assertEqual(scope.call_count, 2)
        self.assertEqual(scope.return_value.__exit__.call_count, 2)
        self.assertIsNone(self.cache.get()['error'])
        self.assertEqual(self.cache.get()['rows'], self.payload['rows'])


class AsyncServiceQueryTests(unittest.TestCase):
    def test_native_submission_is_detached_and_returns_before_fetching_results(self):
        cursor = MagicMock(); cursor.sfqid = 'native-query-id'
        with patch.object(sf,'connection') as connect:
            connect.return_value.__enter__.return_value.cursor.return_value = cursor
            query_id = sf.submit_service_query('CALL SAFE_PROCEDURE(%(id)s)',{'id':'bound'})
        connect.assert_called_once_with(force_service=True)
        cursor.execute.assert_called_once_with('ALTER SESSION SET ABORT_DETACHED_QUERY = FALSE')
        cursor.execute_async.assert_called_once_with('CALL SAFE_PROCEDURE(%(id)s)',{'id':'bound'})
        cursor.fetchall.assert_not_called()
        cursor.close.assert_called_once()
        self.assertEqual(query_id,'native-query-id')

    def test_result_retrieval_uses_a_fresh_service_connection(self):
        cursor = MagicMock(); cursor.fetchall.return_value = [{'RESULT':{'ok':True}}]
        with patch.object(sf,'connection') as connect:
            connect.return_value.__enter__.return_value.cursor.return_value = cursor
            result = sf.service_query_result('native-query-id')
        connect.assert_called_once_with(force_service=True)
        cursor.get_results_from_sfqid.assert_called_once_with('native-query-id')
        self.assertTrue(result['RESULT']['ok'])


if __name__ == '__main__': unittest.main()
