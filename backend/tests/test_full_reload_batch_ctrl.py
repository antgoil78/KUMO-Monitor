import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from migrations import apply_raw_lim_full_reload_batch_ctrl as migration


class FullReloadBatchControlTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[2] / "snowflake/ddl/KUMO_TST/META/procedures/LOAD_RAW_LIM_FROM_STAGE__ec763e035e57.sql"
        self.original = migration.patch_definition(path.read_text())
        # The current snapshot becomes the deployed version after export.
        for old, new in reversed(migration.PATCHES):
            if new in self.original:
                self.original = self.original.replace(new, old, 1)

    def test_cleanup_is_full_reload_only_and_subject_scoped(self):
        updated = migration.patch_definition(self.original)
        start = updated.index("        -- " + migration.MARKER)
        end = updated.index("        -- 12d.", start)
        cleanup = updated[start:end]
        self.assertIn("IF (V_FULL_RELOAD) THEN\n            DELETE FROM META.LIM_BATCH_CTRL", cleanup)
        self.assertIn("SPLIT_PART(UPPER(DW_TABLE_NM), ''.'', -1) = :V_TARGET_TABLE_SHORT", cleanup)
        self.assertIn("SPLIT_PART(UPPER(DW_TABLE_NM), ''.'', -2) = ''RAW_LIM''", cleanup)
        self.assertIn("V_LIM_BATCH_CTRL_DELETED := SQLROWCOUNT;\n        END IF;", cleanup)
        self.assertLess(updated.index("IF (V_FILES_SELECTED = 0)"), start)
        self.assertLess(updated.index("IF (V_RELOAD) THEN"), start)
        self.assertIn("''LIM_BATCH_CTRL_DELETED'', V_LIM_BATCH_CTRL_DELETED", updated)
        self.assertIn("V_LIM_BATCH_CTRL_DELETED     NUMBER := 0", updated)

    def test_migration_is_idempotent_and_rejects_drift(self):
        updated = migration.patch_definition(self.original)
        self.assertEqual(migration.patch_definition(updated), updated)
        with self.assertRaises(RuntimeError):
            migration.patch_definition(self.original.replace("-- 12d.", "-- renamed 12d.", 1))
        with self.assertRaises(RuntimeError):
            migration.patch_definition(updated.replace("V_LIM_BATCH_CTRL_DELETED := SQLROWCOUNT;", ""))

    def test_existing_table_only_cleanup_is_upgraded_to_require_raw_lim_schema(self):
        updated = migration.patch_definition(self.original)
        legacy = updated.replace(migration.SCHEMA_CONDITION, migration.LEGACY_CONDITION, 1)
        self.assertNotEqual(legacy, updated)
        self.assertEqual(migration.patch_definition(legacy), updated)

    def test_deployment_preserves_grants_and_never_runs_cleanup(self):
        original = self.original[self.original.index("CREATE OR REPLACE PROCEDURE"):]
        expected = migration.patch_definition(original)
        cursor = MagicMock()
        cursor.fetchone.side_effect = [{"DDL": original}, {"DDL": expected}]
        conn = MagicMock()
        conn.cursor.return_value = cursor
        with patch.object(migration.sf, "connection") as connection:
            connection.return_value.__enter__.return_value = conn
            self.assertEqual(migration.apply(), "applied")
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertEqual(len(statements), 3)
        self.assertTrue(statements[0].startswith("SELECT GET_DDL"))
        self.assertTrue(statements[1].startswith("CREATE OR REPLACE PROCEDURE"))
        self.assertIn("\nCOPY GRANTS\n", statements[1])
        self.assertTrue(statements[2].startswith("SELECT GET_DDL"))


if __name__ == "__main__":
    unittest.main()
