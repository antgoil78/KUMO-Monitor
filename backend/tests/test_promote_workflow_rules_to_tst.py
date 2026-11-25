import sys
import tempfile
import unittest
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'migrations'))
import promote_workflow_rules_to_tst as migration


class TstWorkflowPromotionTests(unittest.TestCase):
    def cursor(self, database='KUMO_TST', owner='KUMO_ADMIN', pending=0, drift=False,
               previous_resolver=False, dev_resolver_drift=False, metadata_min=5, optional=True):
        cursor = MagicMock()
        def execute(sql, params=()):
            if sql.startswith('CALL '):
                rows = [dict(ENV=dict(DATABASE=database, META_SCHEMA='MONITOR_META', MONITOR_APP_SCHEMA='MONITOR_APP'))]
            elif sql.startswith('SELECT COUNT'): rows = [dict(N=pending)]
            elif 'TASK_HISTORY(' in sql: rows = []
            elif sql.startswith('DESCRIBE TABLE'): rows = []
            elif sql.startswith('SHOW GRANTS'):
                rows = [dict(privilege='OWNERSHIP',grantee_name=owner,granted_to='ROLE'),
                        dict(privilege='USAGE',grantee_name='APP ROLE',granted_to='ROLE',grant_option='false')]
            elif sql.startswith('SHOW PROCEDURES'):
                name = sql.split("'")[1]
                count = 2 if name == 'SP_RESOLVE_WORKFLOW_COMMAND' else migration.PROCEDURES[name].count(',') + 1
                arguments='SP_ORCH_REQUEST_RUN(VARCHAR,VARCHAR,VARCHAR,VARCHAR,VARCHAR,DEFAULT BOOLEAN,DEFAULT VARCHAR) RETURN VARIANT'
                if not optional:arguments=arguments.replace('DEFAULT ','')
                rows = [] if name in migration.NEW else [dict(max_num_arguments=count, min_num_arguments=metadata_min if name=='SP_ORCH_REQUEST_RUN' else count,arguments=arguments)]
            elif 'GET_DDL' in sql:
                address = params[0]
                name = address.split('.')[-1].split('(')[0]
                if 'VIEW' in sql:
                    source = migration.DDL / 'MONITOR_APP/views/V_ORCH_WORKFLOW_RELATIONS.sql'
                elif name == 'SP_GET_ENVIRONMENT':
                    source = next((migration.DDL / 'MONITOR_META/procedures').glob(name+'__*.sql'))
                else:
                    folder = migration.DDL / 'MONITOR_APP/procedures'
                    source = next(folder.glob(name+'__*.sql')) if name=='SP_RESOLVE_WORKFLOW_COMMAND' else folder/(name+'.sql')
                ddl = source.read_text()
                if name=='SP_RESOLVE_WORKFLOW_COMMAND':
                    if previous_resolver:
                        ddl=re.sub(r"    if \(definition.fullRefresh === true\) \{\n.*?\n    \}\n", '', ddl, flags=re.S)
                    if dev_resolver_drift and address.startswith('KUMO_DEV.'):
                        ddl=ddl.replace('vars.runId = runId;', "vars.runId = 'unexpected';")
                if address.startswith('KUMO_TST.'):
                    ddl = ddl.replace('KUMO_DEV.', 'KUMO_TST.')
                    if drift and name=='SP_ORCH_EXECUTE_WORKFLOW': ddl=ddl.replace('AS $$','AS $$\nthrow Error("drift");')
                rows = [dict(DDL=ddl)]
            elif sql.startswith(('SHOW VIEWS','SHOW TASKS')): rows=[]
            elif sql.startswith('DESCRIBE SERVICE'):
                rows = [dict(owner='KUMO_ADMIN', is_upgrading='false', status='RUNNING', auto_resume='true', spec_digest='original')]
            else: raise AssertionError(sql)
            cursor.fetchall.return_value = rows
        cursor.execute.side_effect=execute
        cursor.fetchone.side_effect = lambda: cursor.fetchall.return_value[0]
        return cursor

    def test_default_only_reads_targeted_objects_and_reports_the_incremental_plan(self):
        cursor=self.cursor()
        result=migration.upgrade(cursor)
        self.assertFalse(result['applied'])
        self.assertEqual(set(result['add_columns']), {'JOB_EXECUTION_MODE','PARENT_MUST_BE_NEWER'})
        self.assertEqual(set(result['changed_procedures']), migration.NEW)
        self.assertTrue(all(c.args[0].startswith(('CALL ','SELECT ','SHOW ','DESCRIBE ')) for c in cursor.execute.call_args_list))

    def test_wrong_routing_pending_work_owner_and_source_drift_prevent_every_write(self):
        for options in ({'database':'KUMO_DEV'}, {'pending':1}, {'owner':'OTHER'}, {'drift':True}):
            cursor=self.cursor(**options)
            with self.subTest(options=options), self.assertRaises(ValueError):
                migration.upgrade(cursor, apply=True)
            self.assertFalse(any(c.args[0].startswith(('ALTER ','CREATE ','DROP ','UPDATE ','GRANT ')) for c in cursor.execute.call_args_list))

    def test_native_idle_check_is_scoped_to_tst_and_uses_actual_query_status(self):
        for running in (False,True):
            cursor=MagicMock()
            cursor.fetchall.side_effect=[[dict(N=0)],[dict(NAME='TASK_ORCH_MAINTENANCE',QUERY_ID='query')]]
            cursor.connection.is_still_running.return_value=running
            if running:
                with self.assertRaises(ValueError):migration.check_idle(cursor)
            else: migration.check_idle(cursor)
            sql=cursor.execute.call_args_list[1].args[0]
            self.assertIn("DATABASE_NAME='KUMO_TST' AND SCHEMA_NAME='MONITOR_APP'",sql)
            cursor.connection.get_query_status.assert_called_once_with('query')

    def test_known_previous_dev_resolver_allows_tst_full_refresh_upgrade(self):
        result=migration.upgrade(self.cursor(previous_resolver=True))
        self.assertEqual(result['dev_definitions_older_than_merged_release'], ['SP_RESOLVE_WORKFLOW_COMMAND'])
        self.assertIn('SP_RESOLVE_WORKFLOW_COMMAND',result['changed_procedures'])

    def test_unknown_dev_resolver_drift_is_rejected_before_writes(self):
        cursor=self.cursor(dev_resolver_drift=True)
        with self.assertRaisesRegex(ValueError,'Saved definition differs'):
            migration.upgrade(cursor,apply=True)
        self.assertFalse(any(c.args[0].startswith(('ALTER ','CREATE ','DROP ','UPDATE ','GRANT ')) for c in cursor.execute.call_args_list))

    def test_optional_argument_markers_are_checked_when_snowflake_reports_min_equals_max(self):
        self.assertFalse(migration.upgrade(self.cursor(metadata_min=7))['applied'])
        with self.assertRaisesRegex(ValueError,'Unexpected admission signature'):
            migration.upgrade(self.cursor(metadata_min=7,optional=False))

    def test_post_pause_race_stops_before_ddl_and_records_the_paused_state(self):
        cursor=MagicMock()
        plan=dict(columns={},procedures={},dev_differences=[],view_exists=False,tasks=[dict(name='TASK_ORCH_MAINTENANCE',state='started')],
                  service=dict(status='RUNNING',auto_resume='true',spec_digest='original'))
        with tempfile.TemporaryDirectory() as folder, patch.object(migration,'preflight',return_value=plan), \
                patch.object(migration,'catalog_fingerprints',return_value={}), \
                patch.object(migration,'check_idle',side_effect=ValueError('new admission')):
            path=Path(folder)/'result.json'
            with self.assertRaisesRegex(ValueError,'new admission'):
                migration.upgrade(cursor,apply=True,report_path=path)
            statements=[c.args[0] for c in cursor.execute.call_args_list]
            self.assertTrue(any(' SUSPEND' in s for s in statements))
            self.assertFalse(any(s.startswith(('CREATE ','DROP ','UPDATE ')) or ' RESUME' in s for s in statements))
            self.assertIn('FAILED_REQUIRES_REVIEW',path.read_text())


if __name__=='__main__': unittest.main()
