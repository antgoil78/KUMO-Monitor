import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch


EXPORT_PATH = Path(__file__).resolve().parents[2] / "snowflake/export_ddl.py"
spec = importlib.util.spec_from_file_location("kumo_ddl_export", EXPORT_PATH)
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


class DdlExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scope = json.loads(EXPORT_PATH.with_name("export_scope.json").read_text())
        (self.root / "export_scope.json").write_text(json.dumps(self.scope))
        (self.root / "00_databases_schemas.sql").write_text("-- Existing app namespaces; preserve during targeted updates.\n")
        self.table = {"type": "TABLE", "name": "KUMO_DEV.MONITOR_META.KEEP_TABLE",
                      "file": "ddl/KUMO_DEV/MONITOR_META/tables/KEEP_TABLE.sql"}
        path = self.root / self.table["file"]
        path.parent.mkdir(parents=True)
        path.write_text("create TABLE KUMO_DEV.MONITOR_META.KEEP_TABLE (ID NUMBER);\n")
        (self.root / "manifest.json").write_text(json.dumps({"objects": [self.table]}))
        self.cursor = MagicMock()
        self.cursor.fetchone.side_effect = [
            {"ACCOUNT_NAME": "test-account", "ROLE_NAME": "test-role"},
            {"DDL": "CREATE OR REPLACE PROCEDURE KUMO_DEV.MONITOR_APP.SP_TEST(VARCHAR) RETURNS VARCHAR LANGUAGE SQL AS 'BEGIN RETURN NULL; END'"},
        ]
        self.cursor.fetchall.return_value = [
            {"name": "SP_TEST", "schema_name": "MONITOR_APP", "arguments": "SP_TEST(VARCHAR) RETURN VARCHAR"},
            {"name": "SP_TEST", "schema_name": "MONITOR_APP", "arguments": "SP_TEST(VARCHAR, VARCHAR) RETURN VARCHAR"},
            {"name": "SP_UNRELATED", "schema_name": "MONITOR_APP", "arguments": "SP_UNRELATED() RETURN VARCHAR"},
        ]
        self.conn = MagicMock()
        self.conn.cursor.return_value.__enter__.return_value = self.cursor

    def run_export(self):
        with patch.object(exporter, "ROOT", self.root), \
                patch.object(exporter, "load_dotenv"), \
                patch.dict(os.environ, {"SNOWFLAKE_ACCOUNT": "test", "SNOWFLAKE_USER": "test", "SNOWFLAKE_PASSWORD": "test-secret"}, clear=True), \
                patch.object(exporter.snowflake.connector, "connect") as connect, \
                patch("sys.stdout", new_callable=io.StringIO):
            connect.return_value.__enter__.return_value = self.conn
            exporter.export(items=[("PROCEDURE", "KUMO_DEV.MONITOR_APP.SP_TEST(VARCHAR)")])

    def test_no_action_prints_help_without_connecting(self):
        with patch.object(sys, "argv", ["export_ddl.py"]), \
                patch.object(exporter.snowflake.connector, "connect") as connect, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            exporter.main()
        connect.assert_not_called()
        self.assertIn("--refresh", output.getvalue())

    def test_targeted_export_reads_one_overload_and_preserves_untouched_definitions(self):
        before = (self.root / self.table["file"]).read_bytes()
        bootstrap = (self.root / "00_databases_schemas.sql").read_bytes()
        self.run_export()
        statements = [call.args for call in self.cursor.execute.call_args_list]
        ddl_reads = [call for call in statements if "GET_DDL" in call[0]]
        self.assertEqual(len(ddl_reads), 1)
        self.assertEqual(ddl_reads[0][1], ("PROCEDURE", '"KUMO_DEV"."MONITOR_APP"."SP_TEST"(VARCHAR)'))
        self.assertEqual([call[0] for call in statements if call[0].startswith("SHOW")],
                         ['SHOW PROCEDURES IN SCHEMA "KUMO_DEV"."MONITOR_APP"'])
        self.assertEqual((self.root / self.table["file"]).read_bytes(), before)
        self.assertEqual((self.root / "00_databases_schemas.sql").read_bytes(), bootstrap)
        manifest = json.loads((self.root / "manifest.json").read_text())
        self.assertEqual(len(manifest["objects"]), 2)
        self.assertIn(self.table, manifest["objects"])
        self.assertIn(self.table["file"], manifest["restore_order"])

    def test_missing_object_leaves_saved_files_unchanged(self):
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.cursor.fetchall.return_value = []
        with self.assertRaisesRegex(RuntimeError, "not found or visible"):
            self.run_export()
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_out_of_scope_objects_generated_tasks_and_missing_signatures_are_rejected(self):
        for item in [("TABLE", "KUMO_DEV.META.LIM_BATCH_CTRL"),
                     ("TABLE", "KUMO_TST.MONITOR_META.WORKFLOWS"),
                     ("TASK", "KUMO_DEV.MONITOR_APP.TMP_WF_EXAMPLE"),
                     ("TASK", "KUMO_DEV.MONITOR_APP.TASK_WF_1234ABCD_SCHEDULER"),
                     ("PROCEDURE", "KUMO_DEV.MONITOR_APP.SP_TEST")]:
            with self.subTest(item=item), self.assertRaises(ValueError):
                exporter.requested_objects([item], self.scope)
        self.assertTrue(exporter.requested_objects(
            [("TASK", "KUMO_DEV.MONITOR_APP.TASK_WF_MASTER_DISPATCHER")], self.scope))


if __name__ == "__main__":
    unittest.main()
