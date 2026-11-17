import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'migrations'))
import apply_dependency_activity as migration


class ActivityMigrationTests(unittest.TestCase):
    def cursor(self, pending=0, database='KUMO_DEV', owner='OWNER', drift=False):
        cursor = MagicMock()
        def execute(sql, params=()):
            if 'SP_GET_ENVIRONMENT' in sql:
                result = [dict(ENV=dict(DATABASE=database, META_SCHEMA='MONITOR_META', MONITOR_APP_SCHEMA='MONITOR_APP'))]
            elif sql.startswith('SELECT COUNT'):
                result = [dict(N=pending)]
            elif sql == 'SELECT CURRENT_ROLE() NAME':
                result = [dict(NAME='OWNER')]
            elif sql.startswith('SHOW GRANTS'):
                result = [dict(privilege='OWNERSHIP', grantee_name=owner)]
            elif 'GET_DDL' in sql:
                ddl = migration.SOURCE.read_text()
                result = [dict(DDL=ddl.replace('AS $$', 'AS $$\nthrow Error("drift");') if drift else ddl)]
            else:
                result = []
            cursor.fetchall.return_value = result
        cursor.execute.side_effect = execute
        return cursor

    def test_default_reads_and_apply_replaces_only_evaluator_with_grants(self):
        cursor = self.cursor()
        self.assertFalse(migration.upgrade(cursor)['applied'])
        self.assertTrue(all(call.args[0].startswith(('CALL ', 'SHOW ', 'SELECT ')) for call in cursor.execute.call_args_list))
        cursor = self.cursor()
        result = migration.upgrade(cursor, apply=True)
        writes = [call.args[0] for call in cursor.execute.call_args_list if not call.args[0].startswith(('CALL ', 'SHOW ', 'SELECT '))]
        self.assertEqual(len(writes), 1)
        self.assertTrue(writes[0].startswith('CREATE OR REPLACE PROCEDURE ' + migration.ADDRESS.split('(')[0]))
        self.assertIn('\nCOPY GRANTS\n', writes[0])
        self.assertEqual(result['activity_levels'], ['parent', 'grandparent'])

    def test_preflight_failures_prevent_all_writes(self):
        for options in ({'pending': 1}, {'database': 'KUMO_TST'}, {'owner': 'OTHER'}, {'drift': True}):
            with self.subTest(options=options):
                cursor = self.cursor(**options)
                with self.assertRaises(ValueError):
                    migration.upgrade(cursor, apply=True)
                self.assertFalse(any(call.args[0].startswith('CREATE ') for call in cursor.execute.call_args_list))
