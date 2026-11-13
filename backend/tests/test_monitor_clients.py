"""Monitor polling follows live SSE connections, rather than recent HTTP requests."""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as application
import config
import monitor_cache as legacy_cache
import orchestration_cache as cache_module
import orchestration_engine as engine
import orchestration_monitor as monitor
from realtime_events import RealtimeEventBroker


class ClientLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.broker = RealtimeEventBroker()
        self.broker.set_client_count_callback(application._set_realtime_client_count)
        for patcher in (
            patch.object(application, 'realtime_broker', self.broker),
            patch.object(application, 'monitor_cache'),
            patch.object(application, 'orchestration_cache'),
            patch.object(application, 'status_coordinator'),
            patch.object(application, '_start_dashboard_cache'),
            patch.object(application, '_stop_dashboard_cache'),
            patch.object(application, '_realtime_runtime_state', return_value={}),
        ):
            patcher.start(); self.addCleanup(patcher.stop)
        application.monitor_cache.get.return_value = {}
        application.monitor_cache.diagnostics.return_value = {}
        application.monitor_cache.refresh_seconds = 5
        application.orchestration_cache.diagnostics.return_value = {}

    def test_first_connect_starts_shared_readers_last_disconnect_stops_them(self):
        first = self.broker.subscribe(); second = self.broker.subscribe()
        application.monitor_cache.start.assert_called()
        application.orchestration_cache.start.assert_called()
        self.broker.unsubscribe(first)
        application.monitor_cache.stop.assert_not_called()
        application.orchestration_cache.stop.assert_not_called()
        self.broker.unsubscribe(second)
        application.monitor_cache.stop.assert_called_once()
        application.orchestration_cache.stop.assert_called_once()
        application._stop_dashboard_cache.assert_called_once()
        application.status_coordinator.set_client_count.assert_called_with(0)
        self.assertFalse(self.broker._clients_available.is_set())

    def test_http_activity_does_not_start_pollers_with_no_connected_clients(self):
        with patch.object(engine, 'orchestration_cache', application.orchestration_cache), \
             patch.object(config, 'USE_MOCK', False), patch.object(engine.sf, 'is_configured', return_value=True), \
             patch.object(application, 'Thread') as thread:
            application.orchestration_cache.get.return_value = dict(ok=True,rows=[])
            client = application.app.test_client()
            for path in ('/api/health', '/api/activity', '/api/orchestration/monitor?refresh=1'):
                self.assertEqual(client.get(path).status_code, 200)
        application.monitor_cache.start.assert_not_called()
        application.orchestration_cache.start.assert_not_called()
        application._start_dashboard_cache.assert_not_called()
        thread.assert_not_called()

    def test_delayed_notification_uses_actual_connections(self):
        application._set_realtime_client_count(50)
        application.monitor_cache.start.assert_not_called()
        client = self.broker.subscribe()
        application._set_realtime_client_count(0)
        application.monitor_cache.stop.assert_called_once()  # Only the initial real zero.
        application.status_coordinator.set_client_count.assert_called_with(1)
        self.broker.unsubscribe(client)

    def test_dashboard_reads_the_shared_workflow_cache_without_legacy_queries(self):
        snapshot = dict(ok=True, rows=[dict(WORKFLOW_ID='new-workflow', jobs=[])], submissions=[], runsEnabled=True)
        application.orchestration_cache.get.return_value = snapshot
        with patch.object(application, '_health_snapshot', return_value={}), \
             patch.object(application, '_runtime_refresh_settings', return_value={}), \
             patch.object(application, '_live_active_users', return_value=[]), \
             patch.object(application, '_renew_activity_lease'), \
             patch.object(application, '_reconcile_live_run_locks') as reconcile, \
             patch.object(application.sf, 'query_service') as query:
            response = application.app.test_client().get('/api/dashboard')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['monitor'], snapshot)
        application.monitor_cache.get.assert_not_called()
        reconcile.assert_not_called(); query.assert_not_called()


class ReaderDemandTests(unittest.TestCase):
    def test_refresh_and_manual_wake_never_read_snowflake_without_clients(self):
        broker = RealtimeEventBroker()
        cache = cache_module.OrchestrationCache()
        old_cache = legacy_cache.MonitorCache(5)
        with patch.object(cache_module, 'realtime_broker', broker), patch.object(legacy_cache, 'realtime_broker', broker), \
             patch.object(monitor, 'payload') as read, patch.object(old_cache, '_build_payload') as old_read:
            cache.start(); old_cache.start()
            cache.refresh_async(); old_cache.refresh_async()
            cache.refresh(); old_cache.refresh(force=True)
        read.assert_not_called(); old_read.assert_not_called()
        self.assertIsNone(cache._thread); self.assertIsNone(old_cache._thread)

    def test_fifty_clients_share_one_reader_and_reconnect_restarts_it(self):
        broker = RealtimeEventBroker(); cache = cache_module.OrchestrationCache()
        entered = threading.Event(); release = threading.Event(); refreshed = threading.Event()
        payload = dict(ok=True,rows=[],warnings=[],generatedAt='2026-11-10T12:00:00Z')
        def load():
            entered.set(); release.wait(2); refreshed.set()
            return payload
        def demand(count):
            if broker.client_count(): cache.start()
            else: cache.stop()
        broker.set_client_count_callback(demand)
        with patch.object(config,'USE_MOCK',False),patch.object(cache_module.sf,'is_configured',return_value=True), \
             patch.object(cache_module,'realtime_broker',broker),patch.object(monitor,'payload',side_effect=load) as read:
            clients = [broker.subscribe() for _ in range(50)]
            self.assertTrue(entered.wait(1)); worker = cache._thread
            for client in clients[:-1]: broker.unsubscribe(client)
            self.assertTrue(cache.diagnostics()['enabled'])
            broker.unsubscribe(clients[-1])
            self.assertFalse(cache.diagnostics()['enabled'])
            release.set(); worker.join(2)
            self.assertFalse(worker.is_alive()); self.assertEqual(read.call_count,1)
            entered.clear(); client = broker.subscribe()
            self.assertTrue(entered.wait(1)); worker = cache._thread
            broker.unsubscribe(client); worker.join(2)
            self.assertEqual(read.call_count,2)

    def test_submission_confirmation_waits_without_database_queries_until_reconnect(self):
        broker = RealtimeEventBroker(); cache = cache_module.OrchestrationCache()
        workflow='00000000-0000-0000-0000-000000000001'; run='00000000-0000-0000-0000-000000000002'
        cache.reserve(workflow,dict(requestId='request',mode='WORKFLOW'), 'ACTOR')
        result = dict(RESULT=dict(ok=True,runId=run,status='SUCCESS',taskFqn=engine.task_fqn(workflow,run)))
        blocked = threading.Event()
        original_wait = broker.wait_for_clients
        def wait(): blocked.set(); original_wait()
        with patch.object(engine,'realtime_broker',broker),patch.object(engine,'orchestration_cache',cache), \
             patch.object(broker,'wait_for_clients',side_effect=wait),patch.object(engine.sf,'service_query_result',return_value=result) as read:
            worker=threading.Thread(target=engine.complete_submission,args=(workflow,'request','query'),daemon=True)
            worker.start(); self.assertTrue(blocked.wait(1)); read.assert_not_called()
            client=broker.subscribe(); worker.join(2); broker.unsubscribe(client)
            self.assertFalse(worker.is_alive()); read.assert_called_once_with('query')


if __name__ == '__main__': unittest.main()
