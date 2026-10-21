import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import kumo_repository as repo
from workflow_commands import validate_definition

SOURCE = Path(__file__).resolve().parents[1] / "migrations/workflow_command_resolver.js"
RUN_ID = "12345678-1234-1234-1234-123456789abc"
NODE = r"""
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const resolve = new Function('snowflake', 'P_DEFINITION', 'P_RUN_ID', input.source);
function rows(values) { let i = -1; return {next() {return ++i < values.length}, getColumnValue(n) {return values[i][n-1]}}; }
const queries = [];
const snowflake = {execute({sqlText}) {
  queries.push(sqlText);
  if (sqlText.includes('SP_GET_ENVIRONMENT()')) return rows([[new Function('snowflake', input.environmentSource)(snowflake)]]);
  if (sqlText.includes('FROM IDENTIFIER(?)')) return rows(Object.entries(input.parameters).map(([key, value]) =>
    [key, value.value, value.active ?? true, value.secret ?? false, value.type ?? 'STRING']));
  if (sqlText === 'SELECT CURRENT_DATABASE(), CURRENT_SCHEMA()') return rows([[input.database, 'MONITOR_META']]);
  throw new Error('Unexpected SQL execution: ' + sqlText);
}};
process.stdout.write(JSON.stringify({result: resolve(snowflake, input.definition, input.runId ?? null), queries}));
"""


class WorkflowCommandResolverTests(unittest.TestCase):
    def resolve(self, definition, environment="DEV", changes=None, run_id=RUN_ID):
        values = {"DATABASE": "KUMO_" + environment, "MONITOR_APP_SCHEMA": "MONITOR_APP",
                  "META_SCHEMA": "MONITOR_META", "APP_SCHEMA": "META", "RAW_SCHEMA": "RAW_LIM", "DBT_SCHEMA": "DBT" if environment == "TST" else "DBT_APP",
                  "DBT_PROJECT": "KUMO_" + environment, "DBT_TARGET": "kumo_" + environment}
        parameters = {key: {"value": value} for key, value in values.items()}
        parameters.update(changes or {})
        result = subprocess.run(["node", "-e", NODE], input=json.dumps({
            "source": SOURCE.read_text(), "environmentSource": SOURCE.with_name("environment_context.js").read_text(), "database": "KUMO_" + environment,
            "parameters": parameters, "definition": definition, "runId": run_id,
        }), text=True, capture_output=True, check=True)
        result = json.loads(result.stdout)
        self.assertTrue(all((query.strip().startswith("SELECT") or "SP_GET_ENVIRONMENT()" in query) for query in result["queries"]))
        return result["result"]

    def test_same_sql_definition_routes_to_each_environment(self):
        definition = {"workflowType": "SQL", "sqlCommand": "CALL [DATABASE].[MONITOR_APP_SCHEMA].SP_LOAD_RAW_LIM_INCREMENTAL_PERSIST('[DATABASE]'); SELECT '[run_id]'"}
        for env in ("DEV", "TST"):
            result = self.resolve(definition, env)
            self.assertTrue(result["ok"], result)
            self.assertIn(f"CALL KUMO_{env}.MONITOR_APP.SP_LOAD_RAW_LIM_INCREMENTAL_PERSIST('KUMO_{env}')", result["execSql"])
            self.assertIn(RUN_ID, result["execSql"])
        self.assertIn("[DATABASE]", definition["sqlCommand"])

    def test_raw_schema_is_rendered_and_required_even_without_a_raw_command(self):
        result = self.resolve({"workflowType": "SQL", "sqlCommand": "SELECT * FROM [DATABASE].[RAW_SCHEMA].RAW_LIM_PYMT"},
                              changes={"RAW_SCHEMA": {"value": "LANDING"}})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["execSql"], "SELECT * FROM KUMO_DEV.LANDING.RAW_LIM_PYMT")
        result = self.resolve({"workflowType": "SQL", "sqlCommand": "SELECT 1"},
                              changes={"RAW_SCHEMA": {"value": "RAW_LIM", "active": False}})
        self.assertFalse(result["ok"])
        self.assertIn("RAW_SCHEMA", result["error"])

    def test_sql_literals_are_escaped_and_arrays_and_comments_are_preserved(self):
        result = self.resolve({"workflowType": "SQL", "sqlCommand": "SELECT '[LABEL]', payload[0], '[run_id]' -- [UNDEFINED]\n/* [UNDEFINED] */"},
                              changes={"LABEL": {"value": "O'Reilly"}})
        self.assertTrue(result["ok"], result)
        self.assertIn("'O''Reilly'", result["execSql"])
        self.assertIn("payload[0]", result["execSql"])
        self.assertIn("-- [UNDEFINED]", result["execSql"])
        result = self.resolve({"workflowType": "SQL", "sqlCommand": "SELECT [run_id]"})
        self.assertFalse(result["ok"])
        self.assertIn("single quotes", result["error"])

    def test_bad_parameters_and_legacy_syntax_fail_without_execution(self):
        for changes in ({"APP_SCHEMA": {"value": "META", "active": False}},
                        {"APP_SCHEMA": {"value": "SECRET", "secret": True}},
                        {"APP_SCHEMA": {"value": None}}, {"APP_SCHEMA": {"value": "META; DROP DATABASE X"}}):
            result = self.resolve({"workflowType": "SQL", "sqlCommand": "SELECT * FROM [DATABASE].[APP_SCHEMA].TEST"}, changes=changes)
            self.assertFalse(result["ok"], result)
            self.assertIn("APP_SCHEMA", result["error"])
        for sql in ("SELECT '[UNKNOWN]'", "SELECT '#[run_id]#'"):
            self.assertFalse(self.resolve({"workflowType": "SQL", "sqlCommand": sql})["ok"])

    def test_dbt_target_and_run_id_are_independent_and_existing_vars_are_preserved(self):
        for target in ("", " --target [DBT_TARGET]", " --target=[DBT_TARGET]", " -t [DBT_TARGET]"):
            result = self.resolve({"workflowType": "DBT", "dbtCommand": "dbt build --select tag:daily --vars '{\"keep\":42,\"runId\":\"old\"}'" + target})
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["dbtVars"], {"keep": 42, "runId": RUN_ID})
            self.assertEqual(result["dbtProjectFqn"], "KUMO_DEV.DBT_APP.KUMO_DEV")
            self.assertEqual(result["dbtCommand"].count("--target"), 1)
            self.assertNotIn("[DBT_", result["execSql"])
        result = self.resolve({"workflowType": "DBT", "dbtCommand": "build --target kumo_TST"})
        self.assertFalse(result["ok"])
        self.assertIn("conflicts", result["error"])

    def test_preview_uses_an_explicit_example_id_and_the_destination_parameters(self):
        result = self.resolve({"workflowType": "DBT", "dbtCommand": "build --select tag:daily"}, "TST", run_id=None)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["runIdIsExample"])
        self.assertEqual(result["dbtVars"]["runId"], "00000000-0000-0000-0000-000000000000")
        self.assertEqual(result["dbtTarget"], "kumo_TST")
        self.assertIn("KUMO_TST.DBT.KUMO_TST", result["execSql"])

    def test_conflicting_database_invalid_json_and_unfinished_commands_are_rejected(self):
        result = self.resolve({"workflowType": "SQL", "sqlCommand": "SELECT 1"}, changes={"DATABASE": {"value": "KUMO_TST"}})
        self.assertFalse(result["ok"])
        for command in ("build --vars invalid", "build --vars '[]'", "build --select 'unfinished", "build --target [DBT_TARGET] --target [DBT_TARGET]"):
            self.assertFalse(self.resolve({"workflowType": "DBT", "dbtCommand": command})["ok"])


class WorkflowCommandApiTests(unittest.TestCase):
    def test_repository_binds_the_template_to_the_read_only_resolver(self):
        definition = {"workflowType": "SQL", "sqlCommand": "SELECT '[DATABASE]'"}
        expected = {"ok": True, "execSql": "SELECT 'KUMO_DEV'"}
        with patch.object(repo, "_query", return_value=[{"RESULT": json.dumps(expected)}]) as query:
            self.assertEqual(repo.resolve_workflow_command(definition), expected)
        sql, binds = query.call_args.args
        self.assertTrue(sql.startswith(f"CALL {config.APP_NAMESPACE}.SP_RESOLVE_WORKFLOW_COMMAND"))
        self.assertEqual(json.loads(binds["definition"])["sqlCommand"], definition["sqlCommand"])
        self.assertIsNone(binds["run_id"])

    def test_preview_returns_resolution_errors_as_bad_requests(self):
        import app as application
        with patch.object(config, "USE_MOCK", False), patch.object(repo.sf, "is_configured", return_value=True), \
                patch.object(repo.sf, "connection_scope"), patch.object(application, "_renew_activity_lease"), \
                patch.object(repo, "resolve_workflow_command", side_effect=ValueError("Missing or inactive environment parameter: [MISSING]")):
            response = application.app.test_client().post('/api/workflow-command-preview', json={"workflowType": "SQL", "sqlCommand": "SELECT '[MISSING]'"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("MISSING", response.get_json()["error"])

    def test_new_definitions_cannot_save_dev_or_test_specific_names(self):
        validate_definition({"sqlCommand": "SELECT * FROM [DATABASE].[APP_SCHEMA].TEST"})
        for field, value in [("sqlCommand", "SELECT * FROM KUMO_DEV.META.TEST"), ("dbtTarget", "kumo_TST"),
                             ("sqlCommand", "SELECT '#[run_id]#'")]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_definition({field: value})


if __name__ == "__main__":
    unittest.main()
