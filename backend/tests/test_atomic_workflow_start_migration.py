import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'migrations'))
import apply_atomic_workflow_start as migration


class AtomicStartMigrationTests(unittest.TestCase):
    def cursor(self, pending=0, database='KUMO_DEV', owner='OWNER', drift=False, view_exists=False):
        cursor=MagicMock()
        def execute(sql,params=()):
            if 'SP_GET_ENVIRONMENT' in sql:
                result=[dict(ENV=dict(DATABASE=database,META_SCHEMA='MONITOR_META',MONITOR_APP_SCHEMA='MONITOR_APP'))]
            elif sql.startswith('SELECT COUNT'):
                result=[dict(N=pending)]
            elif sql=='SELECT CURRENT_ROLE() NAME':result=[dict(NAME='OWNER')]
            elif sql.startswith('SHOW GRANTS'):
                result=[dict(privilege='OWNERSHIP',grantee_name=owner,granted_to='ROLE'),dict(privilege='USAGE',grantee_name='APP ROLE',granted_to='ROLE')]
            elif sql.startswith('SHOW VIEWS'):result=[dict(name='V_ORCH_WORKFLOW_RELATIONS')] if view_exists else []
            elif 'GET_DDL' in sql:
                if 'VIEW' in sql:
                    ddl=(migration.DDL/'views/V_ORCH_WORKFLOW_RELATIONS.sql').read_text()
                else:
                    name=next(name for name in migration.NAMES if name in params[0])
                    ddl=(migration.DDL/'procedures'/(name+'.sql')).read_text()
                    if drift:ddl=ddl.replace('AS $$','AS $$\nthrow Error("drift");')
                result=[dict(DDL=ddl)]
            else:result=[]
            cursor.fetchall.return_value=result
        cursor.execute.side_effect=execute
        return cursor

    def test_default_reads_and_apply_only_installs_view_and_three_procedures(self):
        cursor=self.cursor()
        self.assertFalse(migration.upgrade(cursor)['applied'])
        self.assertTrue(all(c.args[0].startswith(('CALL ','SELECT ','SHOW ')) for c in cursor.execute.call_args_list))
        cursor=self.cursor()
        result=migration.upgrade(cursor,apply=True)
        statements=[c.args[0] for c in cursor.execute.call_args_list]
        self.assertEqual(sum(s.startswith('CREATE OR REPLACE VIEW') for s in statements),1)
        creates=[s for s in statements if s.startswith('CREATE OR REPLACE PROCEDURE')]
        self.assertEqual(len(creates),3)
        self.assertIn('SP_ORCH_EXECUTE_WORKFLOW', creates[0])
        self.assertTrue(all('\nCOPY GRANTS\n' in s for s in creates))
        self.assertTrue(any(s.startswith('GRANT SELECT ON VIEW') and '"APP ROLE"' in s for s in statements))
        self.assertFalse(any(s.startswith(('UPDATE ','INSERT ','DELETE ','ALTER TASK','ALTER SERVICE')) for s in statements))
        self.assertEqual(result['claim_owner'],'executor')

    def test_repeat_does_not_replace_view_or_lose_its_grants(self):
        cursor=self.cursor(view_exists=True)
        migration.upgrade(cursor,apply=True)
        self.assertFalse(any(c.args[0].startswith('CREATE OR REPLACE VIEW') for c in cursor.execute.call_args_list))

    def test_preflight_errors_prevent_all_mutations(self):
        for options in ({'pending':1},{'database':'KUMO_TST'},{'owner':'OTHER'},{'drift':True}):
            with self.subTest(options=options):
                cursor=self.cursor(**options)
                with self.assertRaises(ValueError):migration.upgrade(cursor,apply=True)
                self.assertFalse(any(c.args[0].startswith(('CREATE ','GRANT ')) for c in cursor.execute.call_args_list))
