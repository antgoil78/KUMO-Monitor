import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'migrations'))
import apply_dependency_freshness as migration


class FreshnessMigrationTests(unittest.TestCase):
    def cursor(self, installed=False, pending=0, database='KUMO_DEV', owner='OWNER', drift=False):
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
            elif sql.startswith('DESCRIBE'):
                result = [{'name': 'PARENT_MUST_BE_NEWER', 'type': 'BOOLEAN', 'null?': 'N', 'default': 'FALSE'}] if installed else []
            elif 'GET_DDL' in sql:
                ddl = migration.SOURCE.read_text()
                result = [dict(DDL=ddl.replace('AS $$', 'AS $$\nthrow Error("drift");') if drift else ddl)]
            else:
                result = []
            cursor.fetchall.return_value = result
        cursor.execute.side_effect = execute
        return cursor

    def test_read_only_default_and_only_table_and_evaluator_are_changed(self):
        cursor = self.cursor()
        self.assertFalse(migration.upgrade(cursor)['applied'])
        self.assertTrue(all(call.args[0].startswith(('CALL ', 'SHOW ', 'SELECT ', 'DESCRIBE ')) for call in cursor.execute.call_args_list))
        cursor = self.cursor()
        migration.upgrade(cursor, apply=True)
        writes = [call.args[0] for call in cursor.execute.call_args_list if call.args[0].startswith(('ALTER ', 'UPDATE ', 'CREATE '))]
        self.assertEqual(len(writes), 3)
        self.assertIn('DEFAULT FALSE NOT NULL', writes[0])
        self.assertIn("JOB_ID IS NULL AND PARENT_ID IS NOT NULL AND TRIGGER_TYPE = 'DEP'", writes[1])
        self.assertIn('\nCOPY GRANTS\n', writes[2])

    def test_repeat_preserves_explicit_configuration_choices(self):
        cursor = self.cursor(installed=True)
        migration.upgrade(cursor, apply=True)
        self.assertFalse(any(call.args[0].startswith(('ALTER ', 'UPDATE ')) for call in cursor.execute.call_args_list))

    def test_preflight_failures_do_not_write(self):
        for options in ({'pending': 1}, {'database': 'KUMO_TST'}, {'owner': 'OTHER'}, {'drift': True}):
            with self.subTest(options=options):
                cursor = self.cursor(**options)
                with self.assertRaises(ValueError):
                    migration.upgrade(cursor, apply=True)
                self.assertFalse(any(call.args[0].startswith(('ALTER ', 'UPDATE ', 'CREATE ')) for call in cursor.execute.call_args_list))
