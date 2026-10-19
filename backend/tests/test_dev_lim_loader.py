from pathlib import Path
import re
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "migrations"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import create_dev_lim_loader as migration


class DevLimLoaderTests(unittest.TestCase):
    def setUp(self):
        self.source = (migration.ROOT / "snowflake/ddl/KUMO_TST/META/procedures/LOAD_RAW_LIM_FROM_STAGE__ec763e035e57.sql").read_text()

    def test_loader_does_not_inherit_the_callers_database_for_data_or_cleanup(self):
        sql = migration.render_loader(self.source)
        self.assertTrue(sql.startswith("CREATE PROCEDURE KUMO_DEV.MONITOR_META.LOAD_RAW_LIM_FROM_STAGE"))
        self.assertIn("EXECUTE AS CALLER", sql)
        self.assertIn('"P_DATABASE" VARCHAR DEFAULT \'KUMO_DEV\'', sql)
        self.assertNotIn("CURRENT_DATABASE()", sql)
        self.assertNotIn("KUMO_TST", sql)
        self.assertNotIn("KUMO_ADMIN", sql)
        self.assertIn("V_DB := UPPER(TRIM(P_DATABASE))", sql)
        self.assertIn("REGEXP_LIKE(V_DB", sql)
        for relation in (
            "RAW_LOAD_RESULTS", "RAW_LIM_META", "RAW_LIM_PKG_GROUP_SOURCE", "_SET_READY_LOG",
        ):
            self.assertIn(".MONITOR_META." + relation + "''", sql)
        self.assertIn(".META._TMP_SET_READY_LOG''", sql)
        self.assertIn(".META.LIM_BATCH_CTRL''", sql)
        # Every static SQL relation in the loader is now an explicit identifier.
        _, body = migration.procedure_parts(sql.replace("CREATE PROCEDURE", "CREATE OR REPLACE PROCEDURE", 1))
        body = re.sub(r"--[^\n]*", "", body)
        unbound = re.findall(r"\b(?:FROM|JOIN|UPDATE|USING|INTO|TRUNCATE TABLE|TEMPORARY TABLE)\s+((?:RAW_LIM|META|INFORMATION_SCHEMA)\.[A-Z_]+|TMP_RAW_LIM_[A-Z_]+)", body, re.I)
        self.assertEqual(unbound, [])
        self.assertIn("'CREATE TABLE ' || :V_DB || '.SDL.'", body)
        self.assertIn("' CLONE ' || :V_DB || '.SDL.'", body)
        self.assertIn("'DROP TABLE IF EXISTS ' || :V_DB || '.SDL.'", body)
        self.assertEqual(body.count("\\"), migration.procedure_parts(self.source)[1].count("\\"))

    def test_source_drift_aborts_before_deployment(self):
        drifted = self.source.replace("V_DB := CURRENT_DATABASE();", "V_DB := 'OTHER_DB';")
        with self.assertRaisesRegex(ValueError, "Source definition drift"):
            migration.render_loader(drifted)

    def test_results_table_is_created_empty_without_replacement(self):
        source = (migration.ROOT / "snowflake/ddl/KUMO_TST/RAW_LIM/tables/RAW_LOAD_RESULTS.sql").read_text()
        sql = migration.render(migration.PLAN["objects"][0], source)
        self.assertTrue(sql.upper().startswith("CREATE TABLE KUMO_DEV.MONITOR_META.RAW_LOAD_RESULTS"))
        self.assertNotIn("OR REPLACE", sql.upper())
        self.assertNotIn(" CLONE ", sql.upper())
        self.assertNotIn(" AS SELECT ", sql.upper())

    def test_reload_backend_calls_the_selected_loader_and_binds_dev_database(self):
        import file_ingestion
        app = MagicMock()
        params = {"database": "KUMO_DEV", "role": "KUMO_ADMIN", "lim_format": "DISB",
                  "from_date": None, "to_date": None, "reload": False, "reset": False, "ready": False}
        cursor = MagicMock()
        cursor.fetchone.return_value = {"RESULT": {"STATUS": "SUCCESS"}}
        connection = MagicMock()
        connection.cursor.return_value = cursor
        with patch.object(file_ingestion.sf, "connection_scope") as scope, \
                patch.object(file_ingestion.config, "ENVIRONMENT", "DEV"), \
                patch.object(file_ingestion.config, "LIM_LOAD_PROCEDURE", "KUMO_DEV.MONITOR_META.LOAD_RAW_LIM_FROM_STAGE"):
            scope.return_value.__enter__.return_value = connection
            file_ingestion._run_reload_job(app, "test-job", params)
        query, binds = cursor.execute.call_args_list[-1].args
        self.assertIn("CALL KUMO_DEV.MONITOR_META.LOAD_RAW_LIM_FROM_STAGE(", query)
        self.assertIn("P_DATABASE => %(database)s", query)
        self.assertEqual(binds["database"], "KUMO_DEV")

        cursor.reset_mock()
        with patch.object(file_ingestion.sf, "connection_scope") as scope, \
                patch.object(file_ingestion.config, "ENVIRONMENT", ""), \
                patch.object(file_ingestion.config, "LIM_LOAD_PROCEDURE", "KUMO_TST.META.LOAD_RAW_LIM_FROM_STAGE"):
            scope.return_value.__enter__.return_value = connection
            file_ingestion._run_reload_job(app, "test-job", {**params, "database": "KUMO_TST"})
        query, _ = cursor.execute.call_args_list[-1].args
        self.assertIn("CALL KUMO_TST.META.LOAD_RAW_LIM_FROM_STAGE(", query)
        self.assertNotIn("P_DATABASE =>", query)


if __name__ == "__main__":
    unittest.main()
