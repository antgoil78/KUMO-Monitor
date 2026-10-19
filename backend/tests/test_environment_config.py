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


class EnvironmentConfigTests(unittest.TestCase):
    def test_unset_selector_preserves_split_deployment(self):
        config = load_config()
        self.assertEqual(config["T_WORKFLOWS"], "KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOWS")
        self.assertEqual(config["MODEL_PROGRESS_TABLE"], "KUMO_TST.META.MODEL_PROGRESS")
        self.assertEqual(config["ORCHESTRATION_NAMESPACE"], "KUMO_TST.MONITOR_APP")
        self.assertEqual(config["LIM_STAGE"], "KUMO_TST.META.AZURE_LIM_STAGE")
        self.assertEqual(config["LIM_LOAD_PROCEDURE"], "KUMO_TST.META.LOAD_RAW_LIM_FROM_STAGE")
        self.assertTrue(config["AUTO_RESUME_TASKS"])

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

    def test_conflicting_database_and_object_overrides_fail_at_startup(self):
        settings = {
            "KUMO_DB": "KUMO_ADMIN",
            "SNOWFLAKE_DATABASE": "KUMO_TST",
            "KUMO_LIM_DATABASE": "KUMO_TST",
            "KUMO_MODEL_PROGRESS_TABLE": "KUMO_TST.META.MODEL_PROGRESS",
            "KUMO_TEST_PROGRESS_TABLE": "KUMO_TST.META.TEST_PROGRESS",
            "KUMO_RUN_LOG_TABLE": "KUMO_TST.META.RUN_LOG",
            "KUMO_WORKFLOW_DEP_RULE_TABLE": "KUMO_TST.META.WORKFLOW_DEP_RULE",
            "KUMO_MODEL_STATUS_TABLE": "KUMO_TST.META.MODEL_STATUS",
            "KUMO_WORKFLOW_STATUS_TABLE": "KUMO_TST.META.WORKFLOW_STATUS",
            "KUMO_RULE_VERIFY_PROCEDURE": "KUMO_ADMIN.WORKFLOW_MANAGER.SP_RULE_BASED_WORKFLOW_REQUEST_RUN",
            "KUMO_LIM_STAGE": "KUMO_TST.META.AZURE_LIM_STAGE",
            "KUMO_LIM_LOAD_PROCEDURE": "KUMO_TST.META.LOAD_RAW_LIM_FROM_STAGE",
            "KUMO_PROGRESS_TABLE": "KUMO_TST.META.MODEL_PROGRESS",
        }
        for key, value in settings.items():
            with self.subTest(setting=key), self.assertRaisesRegex(ValueError, key):
                load_config(KUMO_ENV="DEV", **{key: value})

    def test_schema_and_same_environment_object_overrides_remain_available(self):
        config = load_config(KUMO_ENV="DEV", KUMO_SCHEMA="workflow_monitor", KUMO_METADATA_SCHEMA="custom_metadata", KUMO_ORCHESTRATION_SCHEMA="catalog", KUMO_RUN_LOG_TABLE="KUMO_DEV.CUSTOM.RUN_LOG")
        self.assertEqual(config["APP_NAMESPACE"], "KUMO_DEV.WORKFLOW_MONITOR")
        self.assertEqual(config["T_HISTORY"], "KUMO_DEV.CUSTOM_METADATA.WORKFLOW_HISTORY")
        self.assertEqual(config["T_ORCHESTRATION_JOB"], "KUMO_DEV.CATALOG.JOB")
        self.assertEqual(config["RUN_LOG_TABLE"], "KUMO_DEV.CUSTOM.RUN_LOG")

    def test_invalid_selector_and_identifiers_are_rejected(self):
        for settings in (
            {"KUMO_ENV": "PROD"},
            {"KUMO_ENV": "SHARED"},
            {"KUMO_ENV": "DEV", "KUMO_SCHEMA": "PUBLIC; DROP DATABASE KUMO_TST"},
            {"KUMO_ENV": "DEV", "KUMO_RUN_LOG_TABLE": "META.RUN_LOG"},
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                load_config(**settings)

    def test_deployment_labels_do_not_redirect_database_references(self):
        for value, label in (("DEV", "DEV"), ("TST", "TEST"), (" test ", "TEST"), ("UAT", "UAT"), ("PRD", "PROD"), ("prod", "PROD")):
            with self.subTest(label=value):
                settings = load_config(KUMO_ENV="TST", KUMO_DEPLOYMENT_ENV=value)
                self.assertEqual(settings["DEPLOYMENT_ENVIRONMENT"], label)
                self.assertEqual(settings["T_WORKFLOWS"], "KUMO_TST.MONITOR_META.WORKFLOWS")
        self.assertEqual(load_config(KUMO_ENV="DEV", KUMO_DEPLOYMENT_ENV="")["DEPLOYMENT_ENVIRONMENT"], "DEV")
        self.assertEqual(load_config()["DEPLOYMENT_ENVIRONMENT"], "")
        with self.assertRaisesRegex(ValueError, "KUMO_DEPLOYMENT_ENV"):
            load_config(KUMO_ENV="DEV", KUMO_DEPLOYMENT_ENV="PRODD")

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
