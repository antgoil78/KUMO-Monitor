import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from migrations import create_dev_monitor_procedures as migration


class DevMonitorProcedureTests(unittest.TestCase):
    def setUp(self):
        self.targets = migration.Targets(
            "KUMO_DEV.MONITOR_META.WORKFLOW_STATUS",
            "KUMO_TST.META.MODEL_STATUS",
            "KUMO_DEV.RAW_LIM", "DEV_TASK_WAREHOUSE",
        )
        self.definitions = migration.build(self.targets)

    def test_all_overloads_are_created_without_replacing_or_calling_source_objects(self):
        self.assertEqual(len(self.definitions), 10)
        for definition in self.definitions:
            sql = definition["sql"]
            self.assertTrue(sql.startswith("CREATE PROCEDURE KUMO_DEV.MONITOR_APP."))
            self.assertNotIn(migration.SOURCE, sql)
            self.assertNotIn("CREATE OR REPLACE PROCEDURE", sql)
            # Only explicit read targets may retain TST in this example mapping.
            self.assertNotIn("KUMO_TST.META.WORKFLOW_STATUS", sql)
            self.assertNotIn("USE DATABASE KUMO_TST", sql)
            self.assertNotIn("KUMO_DEV.MONITOR_APP.WORKFLOW_HISTORY", sql)

    def test_generated_task_bodies_and_completion_calls_use_explicit_targets(self):
        runners = [d for d in self.definitions if ".SP_RUN_WORKFLOW(" in d["target"]]
        self.assertEqual(len(runners), 2)
        for definition in runners:
            sql = definition["sql"]
            self.assertEqual(sql.count("USE DATABASE KUMO_DEV; USE SCHEMA RAW_LIM;"), 2)
            self.assertIn("WAREHOUSE = DEV_TASK_WAREHOUSE", sql)
            self.assertIn("KUMO_DEV.MONITOR_APP.TMP_WF_", sql)
            self.assertIn("CALL KUMO_DEV.MONITOR_APP.SP_DBT_COMPLETE", sql)
            self.assertIn("CALL KUMO_DEV.MONITOR_APP.SP_SQL_COMPLETE", sql)
            self.assertIn("KUMO_DEV.MONITOR_META.WORKFLOW_TMP_TASKS", sql)

    def test_dependency_rules_separate_app_metadata_and_dbt_status_sources(self):
        sql = next(d["sql"] for d in self.definitions if ".SP_RULE_BASED_" in d["target"])
        self.assertIn("let monitorSchema := ''KUMO_DEV.MONITOR_META'';", sql)
        self.assertIn("let appSchema := ''KUMO_DEV.MONITOR_APP'';", sql)
        self.assertIn("let ruleTable := ''KUMO_DEV.MONITOR_META.WORKFLOW_DEP_RULE'';", sql)
        self.assertIn("let workflowStatusTable := ''KUMO_DEV.MONITOR_META.WORKFLOW_STATUS'';", sql)
        self.assertIn("let modelStatusTable := ''KUMO_TST.META.MODEL_STATUS'';", sql)
        self.assertNotIn("metaSchema", sql)
        self.assertIn("procName varchar := appSchema", sql)

    def test_invalid_mapping_or_unrecognised_source_reference_fails_before_deployment(self):
        with self.assertRaises(ValueError):
            migration.Targets("KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_STATUS", self.targets.model_status_table, self.targets.execution_namespace, self.targets.execution_warehouse)
        with self.assertRaises(ValueError):
            migration.Targets(self.targets.workflow_status_table, self.targets.model_status_table, "KUMO_DEV.RAW_LIM; DROP DATABASE KUMO_TST", self.targets.execution_warehouse)
        original = self.definitions[0]["original"]
        with self.assertRaisesRegex(ValueError, "Unmapped monitor reference"):
            migration.transform(original + "\n-- KUMO_ADMIN.WORKFLOW_MANAGER.UNEXPECTED_OBJECT", self.targets)

    def test_existing_dev_procedures_abort_before_any_create(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [{"name": "SP_RUN_WORKFLOW", "schema_name": "MONITOR_APP"}]
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        with patch.object(migration, "connect") as connect:
            connect.return_value.__enter__.return_value = connection
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                migration.apply(self.definitions)
        self.assertEqual([call.args[0] for call in cursor.execute.call_args_list],
                         ["SHOW PROCEDURES IN SCHEMA KUMO_DEV.MONITOR_APP"])


if __name__ == "__main__":
    unittest.main()
