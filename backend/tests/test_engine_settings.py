import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as application
import config
import kumo_repository as repo
import snowflake_client as sf


TASK = "TASK_WF_MASTER_DISPATCHER"


class EngineRepositoryTests(unittest.TestCase):
    def setUp(self):
        for patcher in (
            patch.object(repo, "_engine_state_cache", None),
            patch.object(repo, "_engine_state_cache_monotonic", 0.0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_resume_and_suspend_target_only_configured_namespace_using_service_context(self):
        for namespace in ("KUMO_DEV.MONITOR_APP", "KUMO_TST.MONITOR_APP", "KUMO_ADMIN.WORKFLOW_MANAGER"):
            for enabled, command, state, status in (
                (True, "RESUME", "started", "RUNNING"),
                (False, "SUSPEND", "suspended", "STOPPED"),
            ):
                with self.subTest(namespace=namespace, command=command), \
                     patch.object(config, "APP_NAMESPACE", namespace), \
                     patch.object(sf, "execute_service") as execute, \
                     patch.object(sf, "query_service", return_value=[{"name": TASK, "state": state}]) as query:
                    repo._engine_state_cache = {"status": "STALE"}
                    repo._engine_state_cache_monotonic = time.monotonic()
                    result = repo.set_engine_enabled(enabled)
                    execute.assert_called_once_with(f"ALTER TASK {namespace}.{TASK} {command}",
                                                    params={}, use_warehouse=True, include_context=True)
                    query.assert_called_once_with(f"SHOW TASKS LIKE '{TASK}' IN SCHEMA {namespace}",
                                                  params={}, use_warehouse=True, include_context=True)
                    self.assertEqual(result["taskFqn"], f"{namespace}.{TASK}")
                    self.assertEqual(result["status"], status)
                    self.assertEqual(repo.get_engine_state(), result)
                    self.assertEqual(query.call_count, 1)

    def test_status_refresh_bypasses_cached_state(self):
        with patch.object(repo, "_query", return_value=[{"NAME": TASK, "STATE": "SUSPENDED"}]) as query:
            repo._engine_state_cache = {"status": "RUNNING"}
            repo._engine_state_cache_monotonic = time.monotonic()
            self.assertEqual(repo.get_engine_state()["status"], "RUNNING")
            self.assertEqual(repo.get_engine_state(refresh=True)["status"], "STOPPED")
            query.assert_called_once()

    def test_missing_task_does_not_fall_back_to_other_schemas_or_like_matches(self):
        with patch.object(repo, "_query", return_value=[{"name": "TASKXWFXMASTERXDISPATCHER", "state": "started"}]) as query:
            self.assertEqual(repo.get_engine_state(refresh=True)["status"], "MISSING")
            query.assert_called_once()
            self.assertIn(f"IN SCHEMA {config.APP_NAMESPACE}", query.call_args.args[0])

    def test_read_error_is_unknown_and_can_be_propagated_for_settings(self):
        with patch.object(repo, "_query", side_effect=RuntimeError("permission denied")):
            result = repo.get_engine_state(refresh=True)
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertIn("permission denied", result["error"])
            self.assertIsNone(repo._engine_state_cache)
            with self.assertRaisesRegex(RuntimeError, "permission denied"):
                repo.get_engine_state(refresh=True, raise_on_error=True)

    def test_failed_command_is_propagated(self):
        with patch.object(repo, "_execute", side_effect=RuntimeError("OPERATE required")), \
             patch.object(repo, "_query") as query:
            repo._engine_state_cache = {"status": "STOPPED"}
            with self.assertRaisesRegex(RuntimeError, "OPERATE required"):
                repo.set_engine_enabled(True)
            self.assertIsNone(repo._engine_state_cache)
            query.assert_not_called()

    def test_read_failure_after_command_explains_action_was_applied_and_invalidates_cache(self):
        with patch.object(repo, "_execute"), patch.object(repo, "_query", side_effect=RuntimeError("timeout")):
            repo._engine_state_cache = {"status": "STOPPED"}
            with self.assertRaisesRegex(RuntimeError, "command succeeded.*Refresh"):
                repo.set_engine_enabled(True)
            self.assertIsNone(repo._engine_state_cache)

    def test_non_boolean_values_cannot_issue_task_commands(self):
        with patch.object(repo, "_execute") as execute:
            for value in (None, "false", "RESUME", 0, 1, [], {}):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    repo.set_engine_enabled(value)
            execute.assert_not_called()


class EngineSettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.actor = {"userName": "BROWSER_USER"}
        for patcher in (
            patch.object(config, "USE_MOCK", False),
            patch.object(config, "APP_NAMESPACE", "KUMO_DEV.MONITOR_APP"),
            patch.object(sf, "is_configured", return_value=True),
            patch.object(application, "_renew_activity_lease"),
            patch.object(application, "_actor_context", return_value=self.actor),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        audit = patch.object(application, "_record_interaction")
        refresh = patch.object(application.monitor_cache, "refresh_async")
        self.audit = audit.start()
        self.refresh = refresh.start()
        self.addCleanup(audit.stop)
        self.addCleanup(refresh.stop)
        self.client = application.app.test_client()

    def test_get_reads_fresh_state_without_mutation_or_audit(self):
        with patch.object(repo, "get_engine_state", return_value={"status": "STOPPED"}) as read, \
             patch.object(repo, "set_engine_enabled") as write:
            response = self.client.get("/api/settings/engine")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["source"], "snowflake")
            read.assert_called_once_with(refresh=True, raise_on_error=True)
            write.assert_not_called()
            self.audit.assert_not_called()

    def test_patch_audits_browser_user_and_requests_monitor_refresh(self):
        for enabled, action in ((True, "RESUME_ENGINE"), (False, "SUSPEND_ENGINE")):
            with self.subTest(enabled=enabled), patch.object(repo, "set_engine_enabled", return_value={"status": "RUNNING"}) as write:
                response = self.client.patch("/api/settings/engine", json={"enabled": enabled, "task": "ANOTHER_DATABASE.TASK"})
                self.assertEqual(response.status_code, 200)
                write.assert_called_once_with(enabled)
                self.assertEqual(self.audit.call_args.args, (action,))
                self.assertEqual(self.audit.call_args.kwargs["actor"], self.actor)
                self.assertEqual(self.audit.call_args.kwargs["entity_id"], f"KUMO_DEV.MONITOR_APP.{TASK}")
                self.refresh.assert_called()

    def test_patch_rejects_invalid_json_values_without_mutation(self):
        with patch.object(repo, "set_engine_enabled") as write:
            for body in ('{}', 'null', '[]', 'true', '{"enabled":"false"}', '{"enabled":0}', '{"enabled":null}', '{bad'):
                with self.subTest(body=body):
                    response = self.client.patch("/api/settings/engine", data=body, content_type="application/json")
                    self.assertEqual(response.status_code, 400)
            write.assert_not_called()

    def test_unconfigured_connection_cannot_simulate_success(self):
        with patch.object(sf, "is_configured", return_value=False), patch.object(repo, "set_engine_enabled") as write:
            for method in ("GET", "PATCH"):
                response = self.client.open("/api/settings/engine", method=method, json={"enabled": True})
                self.assertEqual(response.status_code, 503)
                self.assertFalse(response.json["ok"])
            write.assert_not_called()

    def test_failed_patch_reports_and_audits_failure(self):
        with patch.object(repo, "set_engine_enabled", side_effect=RuntimeError("OPERATE required")):
            response = self.client.patch("/api/settings/engine", json={"enabled": False})
            self.assertEqual(response.status_code, 500)
            self.assertIn("OPERATE required", response.json["error"])
            self.assertEqual(self.audit.call_args.args, ("SUSPEND_ENGINE",))
            self.assertFalse(self.audit.call_args.kwargs["success"])
            self.refresh.assert_not_called()

    def test_read_failure_is_reported_as_an_error(self):
        with patch.object(repo, "get_engine_state", side_effect=RuntimeError("not authorized")):
            response = self.client.get("/api/settings/engine")
            self.assertEqual(response.status_code, 500)
            self.assertIn("not authorized", response.json["error"])

    def test_demo_controls_persist_simulated_state_without_snowflake(self):
        with patch.object(config, "USE_MOCK", True), \
             patch.dict(application.MOCK_MONITOR, {"engine": {"task": TASK, "status": "RUNNING"}}), \
             patch.object(repo, "set_engine_enabled") as write, \
             patch.object(repo, "get_engine_state") as read:
            for enabled, status in ((False, "STOPPED"), (True, "RUNNING")):
                response = self.client.patch("/api/settings/engine", json={"enabled": enabled})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["source"], "mock")
                self.assertEqual(self.client.get("/api/settings/engine").json["engine"]["status"], status)
            write.assert_not_called()
            read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
