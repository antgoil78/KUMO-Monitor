import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "migrations"))
import apply_dev_workflow_configuration as migration
from dev_workflow_support import OBJECTS, render_procedure


class DevWorkflowConfigurationTests(unittest.TestCase):
    def source(self, item):
        db, schema, name = item["source"].split(".")
        paths = list((migration.ROOT / "snowflake/ddl" / db / schema / "procedures").glob(name + "__*.sql"))
        self.assertEqual(len(paths), 1)
        return paths[0].read_text()

    def test_configuration_moves_call_targets_and_dbt_project_without_changing_ids(self):
        row = {"WORKFLOW_ID": "original-id", "SQL_COMMAND": "CALL KUMO_TST.RAW_LIM.SP_LOAD_RAW_LIM_INCREMENTAL_PERSIST(); CALL KUMO_ADMIN.META.SP_PURGE_LIM_BATCH(); SELECT * FROM kumo_tst.META.MODEL_PROGRESS;",
               "DBT_PROJECT_FQN": "KUMO_TST.DBT.KUMO_TST", "DBT_TARGET": "kumo_TST", "DBT_COMMAND": "build --target kumo_TST"}
        result = migration.transform_row("WORKFLOWS", row, "kumo_DEV")
        self.assertEqual(result["WORKFLOW_ID"], "original-id")
        self.assertEqual(result["DBT_PROJECT_FQN"], "KUMO_DEV.DBT_APP.KUMO_DEV")
        self.assertEqual(result["DBT_TARGET"], "kumo_DEV")
        self.assertEqual(result["DBT_COMMAND"], "build --target kumo_DEV")
        self.assertIn("KUMO_DEV.MONITOR_APP.SP_LOAD_RAW_LIM_INCREMENTAL_PERSIST()", result["SQL_COMMAND"])
        self.assertIn("KUMO_DEV.MONITOR_APP.SP_PURGE_LIM_BATCH()", result["SQL_COMMAND"])
        self.assertIn("KUMO_DEV.META.MODEL_PROGRESS", result["SQL_COMMAND"])
        self.assertIn("KUMO_TST", row["SQL_COMMAND"])
        with self.assertRaisesRegex(ValueError, "Unmapped ADMIN"):
            migration.rewrite_references("CALL KUMO_ADMIN.UNKNOWN.SP_OTHER()", "kumo_DEV")

    def test_copied_schedules_are_disabled_and_old_task_names_are_removed(self):
        result = migration.transform_row("WORKFLOW_TASKS", {"IS_ENABLED": True, "NEXT_SCHEDULE_TS": "old", "LAST_SCHEDULE_TS": "old"}, "kumo_DEV")
        self.assertFalse(result["IS_ENABLED"])
        self.assertIsNone(result["NEXT_SCHEDULE_TS"])
        self.assertIsNone(result["LAST_SCHEDULE_TS"])
        result = migration.transform_row("WORKFLOW", {"IS_SCHEDULE_ENABLED": True, "TASK_NAME": "old"}, "kumo_DEV")
        self.assertFalse(result["IS_SCHEDULE_ENABLED"])
        self.assertIsNone(result["TASK_NAME"])
        self.assertEqual(migration.transform_row("WORKFLOW_NOTIFICATIONS", {"ENVIRONMENT": "PROD"}, "kumo_DEV")["ENVIRONMENT"], "DEV")

    def test_existing_rows_are_idempotent_and_conflicting_rows_are_never_overwritten(self):
        row = {"ID": "id1", "NAME": "workflow", "CHAIN": '["child"]'}
        select = lambda incoming, existing: migration.select_rows_to_insert("TEST", incoming, existing, ("ID",), (("NAME",),), {"CHAIN"})
        self.assertEqual(select([row], [{**row, "CHAIN": '[ "child" ]'}]), [])
        with self.assertRaisesRegex(ValueError, "will not be overwritten"):
            select([row], [{**row, "CHAIN": '["different-child"]'}])
        with self.assertRaisesRegex(ValueError, "another ID"):
            select([row], [{**row, "ID": "id2"}])
        with self.assertRaisesRegex(ValueError, "Duplicate source"):
            select([row, row], [])

    def test_sql_configuration_values_are_bound_and_cannot_be_executed_during_copy(self):
        cursor = MagicMock()
        cursor.rowcount = 1
        value = "SELECT 1; DROP DATABASE KUMO_DEV;"
        plan = {"target": "KUMO_DEV.MONITOR_META.WORKFLOWS", "columns": ["WORKFLOW_ID", "SQL_COMMAND"],
                "variants": set(), "keys": ["WORKFLOW_ID"], "missing": [{"WORKFLOW_ID": "id1", "SQL_COMMAND": value}]}
        self.assertEqual(migration.insert_configuration(cursor, plan), 1)
        sql, binds = cursor.execute.call_args.args
        self.assertTrue(sql.startswith("INSERT INTO KUMO_DEV.MONITOR_META.WORKFLOWS"))
        self.assertNotIn("DROP DATABASE", sql)
        self.assertIn(value, binds)

    def test_existing_support_interfaces_are_skipped_and_other_overloads_are_rejected(self):
        item = next(item for item in OBJECTS if item["source"].endswith(".SP_CLEANUP_OLD_TMP_TASKS"))
        row = {"name": "SP_CLEANUP_OLD_TMP_TASKS", "schema_name": "MONITOR_APP",
               "arguments": "SP_CLEANUP_OLD_TMP_TASKS(FLOAT) RETURN VARIANT"}
        inventory = {"PROCEDURE": [row]}
        self.assertEqual(migration.missing_objects([item], inventory), ([], [item]))
        different = {**row, "arguments": "SP_CLEANUP_OLD_TMP_TASKS() RETURN VARIANT"}
        for existing in ([different], [row, different]):
            with self.subTest(existing=existing), self.assertRaisesRegex(ValueError, "another interface"):
                migration.missing_objects([item], {"PROCEDURE": existing})

    def test_procedures_preserve_rights_and_remove_source_database_references(self):
        for item in OBJECTS:
            if item["type"] != "PROCEDURE":
                continue
            with self.subTest(procedure=item["target"]):
                source = self.source(item)
                rendered = render_procedure(item, source)
                self.assertTrue(rendered.startswith("CREATE PROCEDURE KUMO_DEV.MONITOR_APP."))
                self.assertNotIn("KUMO_TST", rendered.upper())
                self.assertNotIn("KUMO_ADMIN", rendered.upper())
                self.assertIn("EXECUTE AS OWNER" if "EXECUTE AS OWNER" in source else "EXECUTE AS CALLER", rendered)
                if "SP_RAW_LIM_SET_READY" in item["source"]:
                    self.assertIn(".MONITOR_META.RAW_LIM_CONTROL_ARCHIVE''", rendered)
                    self.assertIn(".META._TMP_SET_READY_LOG''", rendered)
                if "SP_PURGE_LIM_BATCH" in item["source"]:
                    self.assertIn("SPLIT_PART(UPPER(V_TABLE)", rendered)
                    self.assertIn("E_INVALID_TARGET", rendered)
                    self.assertIn("DATEADD(''day'', -3", rendered)
                if "SP_LOAD_RAW_LIM_INCREMENTAL_PERSIST" in item["source"]:
                    self.assertIn("FROM @'' || V_DATABASE", rendered)
                    self.assertIn("IDENTIFIER(:V_INFORMATION_TABLES)", rendered)

    def test_cleanup_rejects_task_names_in_the_original_instance(self):
        item = next(item for item in OBJECTS if item["source"].endswith(".SP_CLEANUP_OLD_TMP_TASKS"))
        sql = render_procedure(item, self.source(item)).replace("CREATE PROCEDURE", "CREATE OR REPLACE PROCEDURE", 1)
        body = migration.procedure_parts(sql)[1].replace("\\\\", "\\")
        script = '''
const fs=require('fs'), assert=require('assert/strict');
const body=JSON.parse(fs.readFileSync(0,'utf8'));
const commands=[];
function rows(data){let i=-1;return {next(){return ++i<data.length},getColumnValue(n){return data[i][n-1]}}}
const snowflake={execute({sqlText}){
 if(sqlText==='SELECT CURRENT_DATABASE()')return rows([['KUMO_DEV']]);
 if(sqlText.includes('SELECT')&&sqlText.includes('FROM MONITOR_META.WORKFLOW_TMP_TASKS'))
   return rows([['KUMO_ADMIN.WORKFLOW_MANAGER.TMP_WF_OLD','old'],['KUMO_DEV.MONITOR_APP.TMP_WF_NEW','new']]);
 commands.push(sqlText);return rows([]);
}};
const result=new Function('snowflake','P_MINUTES_OLD',body)(snowflake,1);
assert.deepEqual(result.cleaned,['KUMO_DEV.MONITOR_APP.TMP_WF_NEW']);
assert.equal(result.failed.length,1);
assert(commands.some(s=>s==='DROP TASK IF EXISTS KUMO_DEV.MONITOR_APP.TMP_WF_NEW'));
assert(!commands.some(s=>s.includes('DROP TASK')&&s.includes('KUMO_ADMIN')));
'''
        result = subprocess.run(["node", "-e", script], input=json.dumps(body), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
