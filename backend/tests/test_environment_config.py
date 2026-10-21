import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


CONFIG = Path(__file__).resolve().parents[1] / "config.py"


def load_config(**settings):
    with patch.dict(os.environ, settings, clear=True):
        return runpy.run_path(str(CONFIG))


def environment_rows(**changes):
    values = dict(DATABASE="KUMO_DEV", MONITOR_APP_SCHEMA="MONITOR_APP", META_SCHEMA="MONITOR_META",
                  APP_SCHEMA="META", RAW_SCHEMA="RAW_LIM", DBT_SCHEMA="DBT_APP", DBT_PROJECT="KUMO_DEV", DBT_TARGET="kumo_DEV")
    values.update(changes)
    return [dict(PARAMETER_KEY=key, PARAMETER_VALUE=value, VALUE_TYPE="STRING", ACTIVE_FL=True, IS_SECRET=False)
            for key, value in values.items()]


class EnvironmentConfigTests(unittest.TestCase):
    def test_unset_selector_uses_the_current_dev_layout(self):
        config = load_config()
        self.assertEqual(config["T_WORKFLOWS"], "KUMO_DEV.MONITOR_META.WORKFLOWS")
        self.assertEqual(config["MODEL_PROGRESS_TABLE"], "KUMO_DEV.META.MODEL_PROGRESS")
        self.assertEqual(config["ORCHESTRATION_NAMESPACE"], "KUMO_DEV.MONITOR_META")
        self.assertEqual(config["LIM_STAGE"], "KUMO_DEV.META.AZURE_LIM_STAGE")
        self.assertEqual(config["LIM_LOAD_PROCEDURE"], "KUMO_DEV.MONITOR_META.LOAD_RAW_LIM_FROM_STAGE")
        self.assertFalse(config["AUTO_RESUME_TASKS"])

    def test_each_environment_routes_all_application_objects_to_its_database(self):
        for environment in ("DEV", "TST", " dev "):
            with self.subTest(environment=environment):
                config = load_config(KUMO_ENV=environment)
                database = f"KUMO_{environment.strip().upper()}"
                self.assertEqual(config["SNOWFLAKE_DATABASE"], database)
                self.assertEqual(config["LIM_DATABASE"], database)
                self.assertEqual(config["DEPLOYMENT_ENVIRONMENT"], "DEV" if environment.strip().upper() == "DEV" else "TEST")
                self.assertEqual(config["APP_NAMESPACE"], database + ".MONITOR_APP")
                self.assertEqual(config["METADATA_NAMESPACE"], database + ".MONITOR_META")
                self.assertEqual(config["T_HISTORY"], database + ".MONITOR_META.WORKFLOW_HISTORY")
                self.assertEqual(config["T_ORCHESTRATION_JOB"], database + ".MONITOR_META.JOB")
                self.assertEqual(config["RAW_LIM_META_TABLE"], database + ".MONITOR_META.RAW_LIM_META")
                self.assertEqual(config["SET_READY_LATEST_TABLE"], database + ".META._TMP_SET_READY_LOG")
                self.assertEqual(config["SET_READY_HISTORY_TABLE"], database + ".MONITOR_META._SET_READY_LOG")
                self.assertEqual(config["RULE_VERIFY_PROCEDURE"], database + ".MONITOR_APP.SP_RULE_BASED_WORKFLOW_REQUEST_RUN")
                self.assertFalse(config["AUTO_RESUME_TASKS"])
                self.assertEqual(config["LIM_LOAD_PROCEDURE"], database + ".MONITOR_META.LOAD_RAW_LIM_FROM_STAGE")
                self.assertEqual(config["LIM_READINESS_SOURCE"], "BATCH_CONTROL")
                for key, value in config.items():
                    if key.startswith("T_") or key.endswith(("_TABLE", "_PROCEDURE", "_FQN")) or key == "LIM_STAGE":
                        self.assertTrue(value.startswith(database + "."), (key, value))

    def test_empty_compose_overrides_use_environment_defaults(self):
        config = load_config(KUMO_ENV="DEV", KUMO_DB="", KUMO_SCHEMA="", KUMO_METADATA_SCHEMA="", KUMO_ORCHESTRATION_SCHEMA="", SNOWFLAKE_SCHEMA="", SNOWFLAKE_DATABASE="", KUMO_LIM_DATABASE="", KUMO_MODEL_PROGRESS_TABLE="", KUMO_LIM_STAGE="", KUMO_AUTO_RESUME_TASKS="")
        self.assertEqual(config["DB"], "KUMO_DEV")
        self.assertEqual(config["MODEL_PROGRESS_TABLE"], "KUMO_DEV.META.MODEL_PROGRESS")
        self.assertEqual(config["LIM_STAGE"], "KUMO_DEV.META.AZURE_LIM_STAGE")
        self.assertEqual(config["SCHEMA"], "MONITOR_APP")
        self.assertEqual(config["METADATA_SCHEMA"], "MONITOR_META")
        self.assertEqual(config["ORCHESTRATION_SCHEMA"], "MONITOR_META")
        self.assertFalse(config["AUTO_RESUME_TASKS"])

    def test_parameter_table_can_live_outside_the_monitored_database(self):
        settings = load_config(KUMO_ENV="DEV", KUMO_ENVIRONMENT_TABLE="CONTROL.CONFIG.ENV_SETTINGS")
        rows = environment_rows(DATABASE="ANALYTICS_TEST", MONITOR_APP_SCHEMA="ENGINE", META_SCHEMA="RUNTIME",
                                APP_SCHEMA="APPDATA", RAW_SCHEMA="LANDING", DBT_SCHEMA="PROJECTS",
                                DBT_PROJECT="PROJECT_TEST", DBT_TARGET="MixedCaseTarget")
        with patch.dict(os.environ, {}, clear=True):
            settings["apply_environment"](rows)
        settings = settings["apply_environment"].__globals__
        self.assertEqual(settings["T_APPLICATION_PARAMETERS"], "CONTROL.CONFIG.ENV_SETTINGS")
        self.assertEqual(settings["APP_NAMESPACE"], "ANALYTICS_TEST.ENGINE")
        self.assertEqual(settings["T_HISTORY"], "ANALYTICS_TEST.RUNTIME.WORKFLOW_HISTORY")
        self.assertEqual(settings["T_ORCHESTRATION_JOB"], "ANALYTICS_TEST.RUNTIME.JOB")
        self.assertEqual(settings["MODEL_PROGRESS_TABLE"], "ANALYTICS_TEST.APPDATA.MODEL_PROGRESS")
        self.assertEqual(settings["LIM_STAGE"], "ANALYTICS_TEST.APPDATA.AZURE_LIM_STAGE")
        self.assertEqual(settings["RAW_NAMESPACE"], "ANALYTICS_TEST.LANDING")
        self.assertEqual(settings["DBT_TARGET"], "MixedCaseTarget")
        self.assertEqual(settings["SNOWFLAKE_DATABASE"], "ANALYTICS_TEST")

    def test_invalid_or_incomplete_parameters_are_rejected(self):
        for bad in (None, "", "META; DROP DATABASE OTHER", "two.parts"):
            settings = load_config()
            with self.subTest(value=bad), self.assertRaisesRegex(ValueError, "RAW_SCHEMA"):
                settings["apply_environment"](environment_rows(RAW_SCHEMA=bad))
        for field, value in (("ACTIVE_FL", False), ("IS_SECRET", True), ("VALUE_TYPE", "INTEGER")):
            rows = environment_rows()
            rows[0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "DATABASE"):
                load_config()["apply_environment"](rows)
        rows = environment_rows()
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            load_config()["apply_environment"](rows + rows[:1])
        with self.assertRaisesRegex(ValueError, "DBT_TARGET"):
            load_config()["apply_environment"](rows[:-1])

    def test_old_routing_overrides_cannot_bypass_the_parameter_store(self):
        for setting, value in (("KUMO_DB", "ANOTHER_DB"), ("KUMO_SCHEMA", "OTHER_ENGINE"),
                               ("KUMO_RUN_LOG_TABLE", "KUMO_DEV.OTHER.RUN_LOG")):
            with patch.dict(os.environ, {setting: value}, clear=True):
                settings = runpy.run_path(str(CONFIG))
                with self.subTest(setting=setting), self.assertRaisesRegex(ValueError, setting):
                    settings["apply_environment"](environment_rows())

    def test_invalid_selector_and_bootstrap_path_are_rejected(self):
        for settings in ({"KUMO_ENV": "SHARED"},
                         {"KUMO_ENVIRONMENT_TABLE": "CONFIG.PARAMETERS"},
                         {"KUMO_ENVIRONMENT_TABLE": "DB.SCHEMA.TABLE; DROP DATABASE OTHER"}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                load_config(**settings)

    def test_deployment_labels_do_not_redirect_database_references(self):
        for value, label in (("DEV", "DEV"), ("TST", "TEST"), (" test ", "TEST"), ("UAT", "UAT"), ("PRD", "PROD"), ("prod", "PROD")):
            with self.subTest(label=value):
                settings = load_config(KUMO_ENV="TST", KUMO_DEPLOYMENT_ENV=value)
                self.assertEqual(settings["DEPLOYMENT_ENVIRONMENT"], label)
                self.assertEqual(settings["T_WORKFLOWS"], "KUMO_TST.MONITOR_META.WORKFLOWS")
        self.assertEqual(load_config(KUMO_ENV="DEV", KUMO_DEPLOYMENT_ENV="")["DEPLOYMENT_ENVIRONMENT"], "DEV")
        self.assertEqual(load_config()["DEPLOYMENT_ENVIRONMENT"], "DEV")
        with self.assertRaisesRegex(ValueError, "KUMO_DEPLOYMENT_ENV"):
            load_config(KUMO_ENV="DEV", KUMO_DEPLOYMENT_ENV="PRODD")

    def test_live_startup_hydrates_routing_before_importing_blueprints(self):
        script = """
import json, os
import dotenv
dotenv.load_dotenv = lambda: None
import snowflake_client as sf
sf.is_configured = lambda: True
rows = json.loads(os.environ['TEST_ENVIRONMENT_ROWS'])
queries = []
def query(sql, **kwargs):
    queries.append((sql, kwargs))
    return rows
sf.query = query
import app, batch_control, file_ingestion
assert 'CONTROL.CONFIG.SETTINGS' in queries[0][0]
assert queries[0][1]['include_context'] is False
assert batch_control.TABLE == 'ANOTHER_DB.APPDATA.LIM_BATCH_CTRL'
assert file_ingestion.LIM_DATABASE == 'ANOTHER_DB'
assert file_ingestion.LIM_STAGE == 'ANOTHER_DB.APPDATA.AZURE_LIM_STAGE'
assert file_ingestion.RAW_LIM_META_TABLE == 'ANOTHER_DB.RUNTIME.RAW_LIM_META'
assert app.config.APP_NAMESPACE == 'ANOTHER_DB.ENGINE'
assert app.config.RAW_NAMESPACE == 'ANOTHER_DB.LANDING'
"""
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("KUMO_", "SNOWFLAKE_", "PYTHON_DOTENV_"))}
        environment.update(PYTHONPATH=str(CONFIG.parent), KUMO_ENVIRONMENT_TABLE="CONTROL.CONFIG.SETTINGS",
                           TEST_ENVIRONMENT_ROWS=__import__('json').dumps(environment_rows(
                               DATABASE="ANOTHER_DB", MONITOR_APP_SCHEMA="ENGINE", META_SCHEMA="RUNTIME",
                               APP_SCHEMA="APPDATA", RAW_SCHEMA="LANDING")))
        result = subprocess.run([sys.executable, "-c", script], cwd=CONFIG.parent, env=environment,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dotenv_is_loaded_before_blueprints_and_health_reports_selected_context(self):
        # A fresh interpreter catches import-order failures hidden by cached
        # config modules. Use a temporary dotenv file and no account credentials.
        script = """
import sys
import dotenv
load = dotenv.load_dotenv
dotenv.load_dotenv = lambda: load(sys.argv[1])
import app
import batch_control
import file_ingestion
assert batch_control.TABLE == 'KUMO_DEV.META.LIM_BATCH_CTRL'
assert 'FROM KUMO_DEV.META.LIM_BATCH_CTRL' in batch_control.GROUP_QUERY
assert file_ingestion.RAW_LIM_META_TABLE == 'KUMO_DEV.MONITOR_META.RAW_LIM_META'
assert file_ingestion.LIM_STAGE == 'KUMO_DEV.META.AZURE_LIM_STAGE'
with app.app.test_client() as client:
    response = client.get('/api/health')
    assert response.status_code == 200
    data = response.get_json()
    assert data['environment'] == 'DEV'
    assert data['deploymentEnvironment'] == 'DEV'
    assert data['db'] == 'KUMO_DEV'
    assert data['objectContext']['engineNamespace'] == 'KUMO_DEV.MONITOR_APP'
    assert data['objectContext']['metadataNamespace'] == 'KUMO_DEV.MONITOR_META'
    assert data['objectContext']['orchestrationNamespace'] == 'KUMO_DEV.MONITOR_META'
    assert data['objectContext']['rawLimMetaTable'] == file_ingestion.RAW_LIM_META_TABLE
"""
        environment = {key: value for key, value in os.environ.items() if not key.startswith(("KUMO_", "SNOWFLAKE_", "PYTHON_DOTENV_"))}
        environment["PYTHONPATH"] = str(CONFIG.parent)
        with tempfile.TemporaryDirectory() as directory:
            dotenv_path = Path(directory) / ".env"
            dotenv_path.write_text("KUMO_ENV=DEV\nKUMO_USE_MOCK=true\n")
            result = subprocess.run([sys.executable, "-c", script, str(dotenv_path)], cwd=CONFIG.parent, env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
