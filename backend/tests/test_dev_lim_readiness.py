from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "migrations"))
import config
import file_ingestion
import lim_readiness
import apply_dev_lim_readiness as migration


class DevLimReadinessTests(unittest.TestCase):
    def test_migration_rejects_unreviewed_source_before_generating_sql(self):
        for name in migration.TARGETS:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "drift"):
                migration.render(name, "CREATE OR REPLACE PROCEDURE unexpected()")

    def test_migration_rerun_preserves_deployed_procedure(self):
        source = "-- " + migration.MARKER + "\nCREATE OR REPLACE PROCEDURE existing()"
        self.assertEqual(migration.render("ready", source), source)

    def test_dev_detail_uses_exact_file_registration_without_raw_scan(self):
        with patch.object(config, "LIM_READINESS_SOURCE", "BATCH_CONTROL"), \
                patch.object(file_ingestion.sf, "query_service", return_value=[]) as query:
            self.assertEqual(file_ingestion._load_raw_detail("test-group"), [])
        sql = query.call_args.args[0]
        self.assertIn("b.DW_FILE_NM = m.ORIGINAL_FILE_NAME", sql)
        self.assertIn("accepted.DLVY_PKG_YEAR_SEQ_NO = m.DLVY_PKG_YEAR_SEQ_NO", sql)
        self.assertNotIn("raw.DW_READY_TO_LOAD_FL", sql)
        self.assertNotIn("COUNT_IF(COALESCE(DW_READY_TO_LOAD_FL", sql)
        self.assertEqual(query.call_args.kwargs["params"], {"group_name": "test-group"})

    def test_dev_optional_raw_status_uses_batch_control_and_validated_empty_files(self):
        cur = MagicMock()
        cur.fetchall.return_value = []
        with patch.object(config, "LIM_READINESS_SOURCE", "BATCH_CONTROL"):
            file_ingestion._load_raw_table_readiness(cur, [{"SUBJECT_AREA": "PAAR"}])
        sql = cur.execute.call_args.args[0]
        self.assertIn(config.LIM_BATCH_CTRL_TABLE, sql)
        self.assertIn("files.DATA_ROWS = 0 AND files.ACTUAL_ROWS = 0", sql)
        self.assertIn("files.FOOTERS > 0", sql)
        self.assertNotIn("COALESCE(DW_READY_TO_LOAD_FL", sql)

    def test_tst_optional_raw_status_preserves_old_flag_query(self):
        cur = MagicMock()
        cur.fetchall.return_value = []
        with patch.object(config, "LIM_READINESS_SOURCE", "RAW_FLAG"):
            file_ingestion._load_raw_table_readiness(cur, [{"SUBJECT_AREA": "PAAR"}])
        self.assertIn("COUNT_IF(COALESCE(DW_READY_TO_LOAD_FL", cur.execute.call_args.args[0])

    def test_raw_queries_use_the_configured_schema(self):
        with patch.object(config, "RAW_SCHEMA", "LANDING"):
            sql = lim_readiness.raw_file_readiness_sql("PYMT")
            self.assertIn("FROM KUMO_DEV.LANDING.RAW_LIM_PYMT", sql)
            self.assertIn("'LANDING.RAW_LIM_PYMT'", sql)
            cur = MagicMock()
            cur.fetchall.return_value = [{"TABLE_NAME": "RAW_LIM_PYMT"}]
            self.assertEqual(file_ingestion._load_lim_subject_areas(cur, "KUMO_DEV"), ["PYMT"])
            self.assertIn("TABLE_SCHEMA = 'LANDING'", cur.execute.call_args.args[0])
            self.assertNotIn("TABLE_SCHEMA = 'RAW_LIM'", cur.execute.call_args.args[0])

    def test_subject_in_query_cannot_inject_sql(self):
        with self.assertRaises(ValueError):
            lim_readiness.raw_file_readiness_sql("PAAR; DROP DATABASE KUMO_DEV")


if __name__ == "__main__":
    unittest.main()
