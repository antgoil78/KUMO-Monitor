import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'migrations'))
import apply_workflow_job_execution as migration


class WorkflowJobExecutionMigrationTests(unittest.TestCase):
    def cursor(self, pending=0, database='KUMO_DEV', owner='OWNER', drift=False, column_exists=False):
        cursor = MagicMock()
        def execute(sql, params=()):
            if 'SP_GET_ENVIRONMENT' in sql:
                rows = [dict(ENV=dict(DATABASE=database, META_SCHEMA='MONITOR_META', MONITOR_APP_SCHEMA='MONITOR_APP'))]
            elif sql.startswith('SELECT COUNT'): rows = [dict(N=pending)]
            elif sql == 'SELECT CURRENT_ROLE() NAME': rows = [dict(NAME='OWNER')]
            elif sql.startswith('SHOW COLUMNS'):
                rows = [dict(data_type=json.dumps(dict(type='TEXT')), default="'SEQUENTIAL'", **{'null?': 'NOT_NULL'})] if column_exists else []
            elif sql.startswith('SHOW GRANTS'):
                rows = [dict(privilege='OWNERSHIP', grantee_name=owner, granted_to='ROLE'),
                        dict(privilege='USAGE', grantee_name='APP ROLE', granted_to='ROLE')]
            elif sql.startswith('SHOW PROCEDURES'):
                rows = [] if 'SP_ORCH_EXECUTE_JOB' in sql else [dict(name='existing')]
            elif 'GET_DDL' in sql:
                name = params[0].split('.')[-1].split('(')[0]
                ddl = (migration.DDL / (name + '.sql')).read_text()
                if drift: ddl = ddl.replace('AS $$', 'AS $$\nthrow Error("drift");')
                rows = [dict(DDL=ddl)]
            else: rows = []
            cursor.fetchall.return_value = rows
        cursor.execute.side_effect = execute
        return cursor

    def test_default_is_read_only_and_apply_is_scoped_to_column_and_three_procedures(self):
        cursor = self.cursor()
        migration.upgrade(cursor)
        self.assertTrue(all(call.args[0].startswith(('CALL ', 'SELECT ', 'SHOW ')) for call in cursor.execute.call_args_list))
        cursor = self.cursor()
        migration.upgrade(cursor, apply=True)
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertEqual(sum(sql.startswith('ALTER TABLE ') for sql in statements), 1)
        self.assertEqual(sum(sql.startswith('CREATE OR REPLACE PROCEDURE ') for sql in statements), 3)
        self.assertTrue(any(sql.startswith('GRANT USAGE ON PROCEDURE ') and '"APP ROLE"' in sql for sql in statements))
        self.assertFalse(any(sql.startswith(('UPDATE ', 'INSERT ', 'DELETE ', 'ALTER TASK ', 'ALTER SERVICE ')) for sql in statements))

    def test_rerun_preserves_existing_mode_column(self):
        cursor = self.cursor(column_exists=True)
        migration.upgrade(cursor, apply=True)
        self.assertFalse(any(call.args[0].startswith('ALTER TABLE ') for call in cursor.execute.call_args_list))

    def test_preflight_rejects_active_runs_wrong_environment_ownership_and_drift(self):
        for options in ({'pending': 1}, {'database': 'KUMO_TST'}, {'owner': 'OTHER'}, {'drift': True}):
            with self.subTest(options=options):
                cursor = self.cursor(**options)
                with self.assertRaises(ValueError): migration.upgrade(cursor, apply=True)
                self.assertFalse(any(call.args[0].startswith(('ALTER ', 'CREATE ', 'GRANT ')) for call in cursor.execute.call_args_list))
