import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as application
import config
import snowflake_client as sf


class OrchestrationServiceContextTests(unittest.TestCase):
    def setUp(self):
        self.statements = []
        self.cursor = MagicMock()
        self.cursor.execute.side_effect = self.record_statement
        self.cursor.fetchall.side_effect = self.query_rows
        self.connection = MagicMock()
        self.connection.cursor.return_value = self.cursor
        patches = [
            patch.dict(os.environ, {
                "SNOWFLAKE_HOST": "test.snowflakecomputing.com",
                "SNOWFLAKE_ACCOUNT": "test-account",
            }),
            patch.object(sf, "_read_spcs_token", return_value="service-token"),
            patch.object(config, "SNOWFLAKE_ROLE", "SERVICE_OWNER"),
            patch.object(config, "SNOWFLAKE_WAREHOUSE", "APP_WAREHOUSE"),
            patch.object(config, "USE_MOCK", False),
            patch.object(application, "_renew_activity_lease"),
        ]
        # Run every read and write against DEV to detect remaining TST literals.
        for name, table in (
            ("T_ORCHESTRATION_WORKFLOW", "WORKFLOW"),
            ("T_ORCHESTRATION_JOB", "JOB"),
            ("T_ORCHESTRATION_WORKFLOW_JOB", "WORKFLOW_JOB"),
            ("T_ORCHESTRATION_DEPENDENCY", "WORKFLOW_DEPENDENCY"),
        ):
            patches.append(patch.object(config, name, f"KUMO_DEV.MONITOR_META.{table}"))
        patches.append(patch.object(config, "ORCHESTRATION_NAMESPACE", "KUMO_DEV.MONITOR_META"))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        connector = patch.object(sf.snowflake.connector, "connect", return_value=self.connection)
        self.connect = connector.start()
        self.addCleanup(connector.stop)
        self.client = application.app.test_client()

    def record_statement(self, sql, *args, **kwargs):
        self.statements.append(" ".join(sql.split()))

    def query_rows(self):
        sql = self.statements[-1]
        if sql.startswith("SELECT COUNT(*) AS CNT"):
            # Existing IDs are valid; names and assignments are available.
            count = 0 if "UPPER(" in sql or "FROM KUMO_DEV.MONITOR_META.WORKFLOW_JOB" in sql else 1
            return [{"CNT": count}]
        return []

    def request(self, method, path, payload=None):
        response = self.client.open(
            path, method=method, json=payload,
            headers={"Sf-Context-Current-User-Token": "browser-token"},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()["ok"])
        self.assertGreater(self.connect.call_count, 0)
        for call in self.connect.call_args_list:
            self.assertEqual(call.kwargs["token"], "service-token")
            self.assertEqual(call.kwargs["role"], "SERVICE_OWNER")
        self.assertFalse(sf.caller_token_present())
        self.assertTrue(any("KUMO_DEV.MONITOR_META." in sql for sql in self.statements))
        self.assertFalse(any("KUMO_TST." in sql or "KUMO_ADMIN." in sql for sql in self.statements))
        return response

    def test_catalog_and_flow_use_service_credentials_with_browser_caller(self):
        for path in ("/api/orchestration/catalog", "/api/orchestration/flow", "/api/workflow-flow"):
            with self.subTest(path=path):
                self.connect.reset_mock()
                response = self.request("GET", path)
                self.assertEqual(response.get_json()["source"], "KUMO_DEV.MONITOR_META")
                # All related reads share one service connection.
                self.assertEqual(self.connect.call_count, 1)

    def test_workflow_edits_and_job_assignments_share_service_transaction(self):
        payload = {"workflowName": "Shared workflow", "jobIds": ["job-1"]}
        for method, path in (
            ("POST", "/api/orchestration/workflows"),
            ("PATCH", "/api/orchestration/workflows/workflow-1"),
            ("DELETE", "/api/orchestration/workflows/workflow-1"),
        ):
            with self.subTest(method=method):
                self.connect.reset_mock()
                self.statements.clear()
                self.request(method, path, payload)
                self.assertEqual(self.connect.call_count, 1)
                self.assertIn("BEGIN", self.statements)
                self.assertIn("COMMIT", self.statements)

    def test_job_edits_use_service_credentials_with_browser_caller(self):
        payload = {"jobName": "Shared job", "jobType": "SQL", "sqlCommand": "SELECT 1"}
        for method, path in (
            ("POST", "/api/orchestration/jobs"),
            ("PATCH", "/api/orchestration/jobs/job-1"),
            ("DELETE", "/api/orchestration/jobs/job-1"),
        ):
            with self.subTest(method=method):
                self.connect.reset_mock()
                self.request(method, path, payload)

    def test_assignment_failure_rolls_back_on_same_service_connection(self):
        original_query_rows = self.query_rows

        def unknown_job():
            if self.statements[-1].startswith("SELECT COUNT(*) AS CNT FROM KUMO_DEV.MONITOR_META.JOB"):
                return [{"CNT": 0}]
            return original_query_rows()

        self.cursor.fetchall.side_effect = unknown_job
        response = self.client.post(
            "/api/orchestration/workflows",
            json={"workflowName": "Shared workflow", "jobIds": ["missing-job"]},
            headers={"Sf-Context-Current-User-Token": "browser-token"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.connect.call_count, 1)
        self.assertEqual(self.connect.call_args.kwargs["token"], "service-token")
        self.assertIn("ROLLBACK", self.statements)
        self.assertNotIn("COMMIT", self.statements)

    def test_user_session_still_uses_caller_credentials(self):
        handle = sf.set_ingress_user_token("browser-token")
        try:
            sf._basic_session_context()
        finally:
            sf.reset_ingress_user_token(handle)
        self.assertEqual(self.connect.call_args.kwargs["token"], "service-token.browser-token")
        self.assertNotIn("role", self.connect.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
