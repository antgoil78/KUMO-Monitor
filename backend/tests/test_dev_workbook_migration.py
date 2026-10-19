import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from migrations import apply_dev_workbook_objects as migration


class DevWorkbookMigrationTests(unittest.TestCase):
    def setUp(self):
        self.plan = migration.load_plan()
        self.manifest = json.loads((migration.ROOT / "snowflake/manifest.json").read_text())

    def source(self, item):
        if item["type"] == "STAGE":
            return None
        return (migration.ROOT / "snowflake" / item["file"]).read_text()

    def test_plan_matches_announced_objects_and_explicit_workbook_locations(self):
        counts = {}
        for item in self.plan["objects"]:
            counts[item["type"]] = counts.get(item["type"], 0) + 1
            self.assertTrue(item["target"].startswith("KUMO_DEV."))
        self.assertEqual(counts, {"TABLE": 20, "PROCEDURE": 11, "STAGE": 1, "STREAM": 1, "TASK": 1})
        by_name = {item["source"].rsplit(".", 1)[1]: item for item in self.plan["objects"]}
        self.assertEqual(by_name["_TMP_SET_READY_LOG"]["target"], "KUMO_DEV.META._TMP_SET_READY_LOG")
        self.assertEqual(by_name["_SET_READY_LOG"]["target"], "KUMO_DEV.MONITOR_META._SET_READY_LOG")
        self.assertNotIn("WORKFLOW_STATUS", by_name)
        self.assertNotIn("LIM_BATCH_CTRL", by_name)
        self.assertNotIn("WORKFLOW_DEP_RULE", by_name)

    def test_procedure_bodies_are_database_local_and_owner_rights_are_preserved(self):
        for item in self.plan["objects"]:
            if item["type"] != "PROCEDURE":
                continue
            with self.subTest(procedure=item["target"], signature=item["signature"]):
                sql = migration.render(item, self.source(item), self.plan)
                self.assertTrue(sql.startswith("CREATE PROCEDURE KUMO_DEV.MONITOR_APP."))
                self.assertNotIn("KUMO_TST", sql)
                self.assertNotIn("KUMO_ADMIN", sql)
                source_header, _ = migration.procedure_parts(self.source(item))
                self.assertIn("EXECUTE AS CALLER" if "EXECUTE AS CALLER" in source_header else "EXECUTE AS OWNER", sql)
                if "EXECUTE AS CALLER" in source_header:
                    self.assertIn('"P_DATABASE" VARCHAR DEFAULT \'KUMO_DEV\'', sql)
                    self.assertIn("E_INVALID_DATABASE", sql)
                else:
                    _, body = migration.procedure_parts(sql.replace("CREATE PROCEDURE", "CREATE OR REPLACE PROCEDURE", 1))
                    self.assertNotIn("KUMO_DEV", body)

    def test_generated_run_tasks_capture_the_procedure_database(self):
        for item in self.plan["objects"]:
            if not item["source"].endswith(".SP_RUN_WORKFLOW"):
                continue
            sql = migration.render(item, self.source(item), self.plan)
            _, body = migration.procedure_parts(sql.replace("CREATE PROCEDURE", "CREATE OR REPLACE PROCEDURE", 1))
            self.assertIn("V_DATABASE VARCHAR DEFAULT CURRENT_DATABASE()", body)
            self.assertIn(":V_DATABASE || '.MONITOR_APP.TMP_WF_'", body)
            self.assertEqual(body.count("'   USE DATABASE \"' || :V_DATABASE || '\"; USE SCHEMA RAW_LIM;'"), 2)
            self.assertIn("CALL MONITOR_APP.SP_DBT_COMPLETE", body)
            self.assertIn("CALL MONITOR_APP.SP_SQL_COMPLETE", body)

    def test_exported_sql_string_escaping_is_preserved(self):
        for item in self.plan["objects"]:
            if item["type"] != "PROCEDURE":
                continue
            source = self.source(item)
            _, source_body = migration.procedure_parts(source)
            sql = migration.render(item, source, self.plan)
            self.assertIn("\nAS '", sql)
            _, target_body = migration.procedure_parts(sql.replace("CREATE PROCEDURE", "CREATE OR REPLACE PROCEDURE", 1))
            self.assertEqual(source_body.count("\\"), target_body.count("\\"))

    def test_caller_refresh_uses_explicit_database_for_data_and_metadata(self):
        item = next(item for item in self.plan["objects"] if item["source"].endswith(".SP_RAW_LIM_META_REFRESH"))
        sql = migration.render(item, self.source(item), self.plan)
        self.assertIn("V_DATABASE || ''.MONITOR_META.RAW_LIM_META''", sql)
        self.assertIn("V_DATABASE || ''.MONITOR_META.RAW_LIM_PKG_GROUP_SOURCE''", sql)
        self.assertIn("INSERT INTO IDENTIFIER(:V_METADATA_TABLE)", sql)
        self.assertIn(":V_DATABASE || ''.RAW_LIM.RAW_LIM_''", sql)

    def test_ddl_cannot_replace_existing_objects_or_copy_table_rows(self):
        for item in self.plan["objects"]:
            sql = migration.render(item, self.source(item), self.plan)
            first_line = sql.splitlines()[0].upper()
            self.assertTrue(first_line.startswith("CREATE "))
            self.assertIn(item["target"], first_line)
            self.assertNotIn("OR REPLACE", first_line)
            if item["type"] == "TABLE":
                self.assertNotIn(" AS SELECT ", sql.upper())
                self.assertNotIn(" CLONE ", sql.upper())
                self.assertNotIn("KUMO_TST", sql)
                self.assertNotIn("KUMO_ADMIN", sql)

    def test_stage_clone_avoids_persisting_credentials_and_dispatcher_is_not_resumed(self):
        stage = next(item for item in self.plan["objects"] if item["type"] == "STAGE")
        self.assertEqual(migration.render(stage, None, self.plan),
                         "CREATE STAGE KUMO_DEV.META.AZURE_LIM_STAGE CLONE KUMO_TST.META.AZURE_LIM_STAGE;\n")
        task = next(item for item in self.plan["objects"] if item["type"] == "TASK")
        sql = migration.render(task, self.source(task), self.plan)
        self.assertNotIn("RESUME", sql)
        self.assertNotIn("EXECUTE TASK", sql)
        self.assertIn("CALL MONITOR_APP.SP_RUN_WORKFLOW", sql)

    def test_existing_objects_are_skipped_and_cross_schema_duplicates_abort(self):
        item = next(item for item in self.plan["objects"] if item["type"] == "TABLE")
        name = item["target"].rsplit(".", 1)[1]
        schema = item["target"].split(".")[1]
        missing, skipped = migration.select_missing({"objects": [item]}, {"TABLE": [{"name": name, "schema_name": schema}]})
        self.assertEqual(missing, [])
        self.assertEqual(skipped, [item])
        with self.assertRaisesRegex(RuntimeError, "another DEV schema"):
            migration.select_missing({"objects": [item]}, {"TABLE": [{"name": name, "schema_name": "SOME_OTHER_SCHEMA"}]})


if __name__ == "__main__":
    unittest.main()
