import sys
import unittest
from unittest.mock import MagicMock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'migrations'))
import apply_dependency_dispatch as migration


class DeploymentTests(unittest.TestCase):
    def cursor(self, pending=0, database='KUMO_DEV', drift=False, owner='OWNER', admission_count=7):
        cursor = MagicMock()
        name = None
        def execute(sql, params=()):
            nonlocal name
            if 'SP_GET_ENVIRONMENT' in sql:
                values = [dict(ENV=dict(DATABASE=database,META_SCHEMA='MONITOR_META',MONITOR_APP_SCHEMA='MONITOR_APP'))]
            elif sql.startswith('SELECT COUNT'):
                values = [dict(N=pending)]
            elif sql == 'SELECT CURRENT_ROLE() NAME':
                values = [dict(NAME='OWNER')]
            elif sql.startswith('SHOW PROCEDURES'):
                name = next(item for item in migration.NAMES if item in sql)
                values = [] if name == 'SP_ORCH_DISPATCH_DEPENDENCIES' else [dict(min_num_arguments=5 if name == 'SP_ORCH_REQUEST_RUN' else 1,max_num_arguments=admission_count if name == 'SP_ORCH_REQUEST_RUN' else 1)]
            elif sql.startswith('SHOW GRANTS'):
                values = [dict(privilege='OWNERSHIP',granted_to='ROLE',grantee_name=owner,grant_option='false'),
                          dict(privilege='USAGE',granted_to='ROLE',grantee_name='EXECUTOR ROLE',grant_option='false')]
            elif 'GET_DDL' in sql:
                name = next(item for item in migration.NAMES if item in params[0])
                ddl = (migration.ROOT / 'snowflake/ddl/KUMO_DEV/MONITOR_APP/procedures' / (name + '.sql')).read_text()
                if drift: ddl = ddl.replace('AS $$', 'AS $$\nthrow new Error("drift");', 1)
                values = [dict(DDL=ddl)]
            else:
                values = []
            cursor.fetchall.return_value = values
        cursor.execute.side_effect = execute
        return cursor

    def test_default_only_reads_and_applied_changes_only_four_procedures(self):
        cursor = self.cursor()
        result = migration.upgrade(cursor)
        self.assertFalse(result['applied'])
        self.assertTrue(all(call.args[0].startswith(('CALL ', 'SELECT ', 'SHOW ')) for call in cursor.execute.call_args_list))
        cursor = self.cursor()
        result = migration.upgrade(cursor, apply=True)
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        creates = [sql for sql in statements if sql.startswith('CREATE OR REPLACE PROCEDURE')]
        self.assertEqual(len(creates), 4)
        self.assertTrue(all('\nCOPY GRANTS\n' in sql for sql in creates))
        self.assertTrue(any(sql.startswith('GRANT USAGE ON PROCEDURE') and 'SP_ORCH_DISPATCH_DEPENDENCIES' in sql and '"EXECUTOR ROLE"' in sql for sql in statements))
        self.assertFalse(any(sql.startswith(('UPDATE ', 'DELETE ', 'INSERT ', 'ALTER TASK', 'EXECUTE TASK', 'ALTER SERVICE')) for sql in statements))
        self.assertTrue(result['dispatch_enabled'])

    def test_pending_runs_wrong_environment_drift_and_ownership_block_all_ddl(self):
        for options in ({'pending': 1}, {'database': 'KUMO_TST'}, {'drift': True}, {'owner': 'OTHER'}):
            cursor = self.cursor(**options)
            with self.subTest(options=options), self.assertRaises(ValueError):
                migration.upgrade(cursor, apply=True)
            self.assertFalse(any(call.args[0].startswith(('CREATE ', 'GRANT ')) for call in cursor.execute.call_args_list))

    def test_five_argument_admission_is_replaced_and_usage_is_restored(self):
        cursor = self.cursor(admission_count=5)
        migration.upgrade(cursor, apply=True)
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        dropped = [sql for sql in statements if sql.startswith('DROP PROCEDURE')]
        self.assertEqual(dropped, ['DROP PROCEDURE KUMO_DEV.MONITOR_APP.SP_ORCH_REQUEST_RUN(VARCHAR,VARCHAR,VARCHAR,VARCHAR,VARCHAR)'])
        self.assertTrue(any(sql.startswith('GRANT USAGE ON PROCEDURE') and 'SP_ORCH_REQUEST_RUN' + migration.SIGNATURE in sql for sql in statements))


if __name__ == '__main__':
    unittest.main()
