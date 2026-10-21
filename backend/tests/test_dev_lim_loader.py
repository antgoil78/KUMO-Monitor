from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class DevLimLoaderTests(unittest.TestCase):
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
