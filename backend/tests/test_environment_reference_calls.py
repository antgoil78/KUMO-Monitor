import importlib.util
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import kumo_repository as repo
from test_environment_config import load_config

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("render_kumo_service", ROOT / "snowflake/spcs/render_service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class EnvironmentReferenceCallsTests(unittest.TestCase):
    def settings(self, environment):
        return {key: value for key, value in load_config(KUMO_ENV=environment).items() if key.isupper()}

    def test_scheduler_reads_metadata_calls_app_and_remains_suspended(self):
        columns = {"SCHEDULE_CRON": "VARCHAR", "SCHEDULE_TIMEZONE": "VARCHAR", "IS_ENABLED": "BOOLEAN"}
        row = {"CRON": "0 0 * * *", "TZ": "UTC", "SCHEDULE_ENABLED": True, "WORKFLOW_ENABLED": True}
        for env in ("DEV", "TST"):
            with self.subTest(environment=env), patch.multiple(config, **self.settings(env)), \
                    patch.object(repo, "describe_table", return_value=columns), \
                    patch.object(repo, "_query", return_value=[row]) as query, \
                    patch.object(repo, "_execute") as execute:
                repo.create_or_replace_sf_task("workflow-1")
                self.assertIn(f"KUMO_{env}.MONITOR_META.WORKFLOW_TASKS", query.call_args.args[0])
                statements = [call.args[0] for call in execute.call_args_list]
                create = next(sql for sql in statements if "CREATE OR REPLACE TASK" in sql)
                self.assertIn(f"TASK KUMO_{env}.MONITOR_APP.", create)
                self.assertIn(f"CALL KUMO_{env}.MONITOR_APP.SP_WORKFLOW_REQUEST_RUN", create)
                self.assertTrue(statements[-1].endswith(" SUSPEND"))
                self.assertFalse(any("RESUME" in sql for sql in statements))

    def test_rule_verification_supplies_the_selected_database(self):
        for env in ("DEV", "TST"):
            cursor = MagicMock()
            cursor.fetchall.return_value = []
            connection = MagicMock()
            connection.cursor.return_value = cursor
            with self.subTest(environment=env), patch.multiple(config, **self.settings(env)), \
                    patch.object(repo.sf, "connection") as scope:
                scope.return_value.__enter__.return_value = connection
                self.assertEqual(repo.verify_dependency_rules("workflow-1"), [])
            query, binds = cursor.execute.call_args.args
            self.assertIn(f"CALL KUMO_{env}.MONITOR_APP.SP_RULE_BASED_WORKFLOW_REQUEST_RUN", query)
            self.assertIn("%(database)s", query)
            self.assertEqual(binds["database"], f"KUMO_{env}")

    def test_spcs_service_context_and_container_selector_route_to_the_same_objects(self):
        for env in ("DEV", "TST"):
            with self.subTest(environment=env):
                sql = service.render(env, "/kumo_shared/monitor_app/images/kumo-monitor:review",
                                     "DEV_POOL", "QUERY_WH", "SERVICE_OWNER")
                location = re.search(r"^CREATE SERVICE (\S+)", sql, re.M).group(1)
                database, schema, _ = location.split(".")
                configured_env = re.search(r'KUMO_ENV: "(\w+)"', sql).group(1)
                runtime = load_config(KUMO_ENV=configured_env, SNOWFLAKE_DATABASE=database, SNOWFLAKE_SCHEMA=schema)
                self.assertEqual(runtime["APP_NAMESPACE"], database + "." + schema)
                self.assertEqual(runtime["T_WORKFLOWS"], database + ".MONITOR_META.WORKFLOWS")
                for managed in ("SNOWFLAKE_DATABASE:", "SNOWFLAKE_SCHEMA:", "SNOWFLAKE_PASSWORD:", "SNOWFLAKE_ROLE:"):
                    self.assertNotIn(managed, sql)

    def test_service_renderer_rejects_unknown_environments_and_sql_metacharacters(self):
        with self.assertRaises(ValueError):
            service.render("PRD", "/shared/app/images/monitor:latest", "POOL", "WH", "OWNER")
        with self.assertRaises(ValueError):
            service.render("DEV", "/shared/app/images/monitor:latest\n$$;DROP DATABASE X;", "POOL", "WH", "OWNER")
        with self.assertRaises(ValueError):
            service.render("DEV", "/shared/app/images/monitor:latest", "POOL; DROP DATABASE X", "WH", "OWNER")


if __name__ == "__main__":
    unittest.main()
