"""Cutover guards: DEV routing, known source, idle executions and no side effects in preview."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'migrations'))
import apply_stream_dispatch as m
from apply_parallel_engine import require_pre_stream_release


class MigrationTests(unittest.TestCase):
    def cursor(self, database='KUMO_DEV', active=0, drift=False, stale=False):
        c=MagicMock()
        def execute(sql,params=()):
            if sql.startswith('CALL '):rows=[dict(ENV=dict(DATABASE=database,META_SCHEMA='MONITOR_META',MONITOR_APP_SCHEMA='MONITOR_APP'))]
            elif sql=='SELECT CURRENT_ROLE() NAME':rows=[dict(NAME='KUMO_ADMIN')]
            elif sql.startswith('SHOW PROCEDURES'):rows=[dict(name=sql.split("'")[1])]
            elif sql.startswith('SHOW GRANTS'):rows=[dict(privilege='OWNERSHIP',grantee_name='KUMO_ADMIN')]
            elif 'GET_DDL' in sql:
                name=params[0].split('.')[-1].split('(')[0]
                source=(m.DDL/('MONITOR_APP/procedures/'+name+'.sql')).read_text()
                rows=[dict(DDL=source.replace('AS $$','AS $$ throw Error("drift");',1) if drift else source)]
            elif sql.startswith('SHOW TASKS'):rows=[dict(name='TASK_ORCH_MAINTENANCE',owner='KUMO_ADMIN',state='suspended',schedule='1 MINUTE',comment='KUMO parallel orchestration v2')]
            elif sql.startswith('SHOW STREAMS'):rows=[dict(name='ORCH_DISPATCH_EVENTS_STREAM',owner='KUMO_ADMIN',stale=stale,table_name='KUMO_DEV.MONITOR_META.ORCH_DISPATCH_EVENTS')]
            elif sql.startswith('SELECT COUNT'):rows=[dict(N=active)]
            else:rows=[]
            c.fetchall.return_value=rows
        c.execute.side_effect=execute
        return c

    def test_preview_never_mutates_anything(self):
        c=self.cursor();r=m.upgrade(c)
        self.assertFalse(r['applied'])
        self.assertTrue(all(x.args[0].startswith(('CALL ','SELECT ','SHOW ')) for x in c.execute.call_args_list))

    def test_unknown_source_active_runs_wrong_environment_or_stale_stream_prevent_cutover(self):
        for opts in (dict(database='KUMO_TST'),dict(active=1),dict(drift=True),dict(stale=True)):
            with self.subTest(opts=opts):
                c=self.cursor(**opts)
                with self.assertRaises(ValueError):m.upgrade(c,True)
                self.assertFalse(any(x.args[0].startswith(('CREATE ','ALTER ','MERGE ')) for x in c.execute.call_args_list))

    def test_apply_keeps_maintenance_suspended_and_never_changes_tst_or_services(self):
        c=self.cursor();r=m.upgrade(c,True)
        statements=[x.args[0] for x in c.execute.call_args_list]
        self.assertTrue(r['applied'])
        self.assertIn('ALTER TASK KUMO_DEV.MONITOR_APP.TASK_ORCH_MAINTENANCE UNSET SCHEDULE',statements)
        self.assertNotIn('ALTER TASK KUMO_DEV.MONITOR_APP.TASK_ORCH_MAINTENANCE RESUME',statements)
        self.assertIn('ALTER TASK KUMO_DEV.MONITOR_APP.TASK_ORCH_DISPATCH RESUME',statements)
        self.assertFalse(any('ALTER SERVICE' in sql or 'KUMO_TST' in sql for sql in statements))
        self.assertFalse(any(sql.startswith('CREATE OR REPLACE STREAM') for sql in statements))

    def test_old_tst_upgraders_stop_before_writes_when_dev_stream_trial_is_enabled(self):
        c=MagicMock();c.fetchone.return_value={'N':1}
        with self.assertRaisesRegex(ValueError,'dedicated, reviewed TST promotion'):
            require_pre_stream_release(c)
        self.assertTrue(all(x.args[0].startswith('SELECT ') for x in c.execute.call_args_list))

if __name__=='__main__':unittest.main()
