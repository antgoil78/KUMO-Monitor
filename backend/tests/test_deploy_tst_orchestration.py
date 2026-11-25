import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'migrations'))
import deploy_tst_orchestration as deployment
import retire_legacy_engine_dev as retirement
from test_migrate_workflow_catalog import fixture


class TstCutoverTests(unittest.TestCase):
    def test_plan_preserves_tst_commands_enabled_workflows_and_parents_but_pauses_schedules(self):
        workflows, tasks = fixture()
        tasks[2]['IS_ENABLED'] = True
        workflows[1]['DBT_COMMAND'] = 'dbt build -s models/data/SDL/PYMT'
        workflows[3]['SQL_COMMAND'] = 'CALL [DATABASE].[MONITOR_APP_SCHEMA].SP_CLEANUP_OLD_TMP_TASKS(1);\nSELECT 1'
        before = copy.deepcopy((workflows, tasks))
        plan, summary = deployment.plan_catalog(workflows, tasks, 'TST_WH')
        self.assertEqual(summary['paused_business_schedules'], ['LOAD RAW DAILY'])
        self.assertTrue(all(row['IS_ENABLED'] and not row['IS_SCHEDULE_ENABLED'] for row in plan['WORKFLOW']))
        self.assertTrue(all(row['WAREHOUSE_NAME'] == 'TST_WH' for row in plan['WORKFLOW']))
        self.assertNotIn('PYMT EDV', [row['JOB_NAME'] for row in plan['JOB']])
        self.assertEqual(next(row for row in plan['JOB'] if row['JOB_NAME']=='MAINTENANCE - CLEANUP')['SQL_COMMAND'], 'SELECT 1')
        ready = next(row for row in plan['WORKFLOW'] if row['WORKFLOW_NAME']=='PYMT LOAD')
        raw = next(row for row in plan['WORKFLOW'] if row['WORKFLOW_NAME']=='LOAD RAW DAILY')
        header = next(row for row in plan['DEPENDENCIES'] if row['WORKFLOW_ID']==ready['WORKFLOW_ID'] and row['JOB_ID'] is None)
        self.assertEqual(header['PARENT_ID'], raw['WORKFLOW_ID'])
        self.assertEqual((workflows,tasks), before)

    def test_idle_guard_rejects_queue_active_history_and_running_native_tasks(self):
        for queue, history, tasks in [(1,0,[]),(0,1,[]),(0,0,[{'NAME':'TMP_WF_ABC'}])]:
            cursor = MagicMock()
            cursor.fetchone.side_effect = [{'N':queue},{'N':history}]
            cursor.fetchall.return_value = tasks
            with self.subTest(queue=queue,history=history,tasks=tasks), self.assertRaises(ValueError):
                deployment.check_idle(cursor)
            self.assertTrue(all(call.args[0].startswith('SELECT') for call in cursor.execute.call_args_list))

    def test_approved_definition_drift_is_rejected(self):
        workflows,tasks = fixture()
        plan,_ = deployment.plan_catalog(workflows,tasks,'TST_WH')
        cursor=MagicMock();cursor.fetchall.return_value=copy.deepcopy(plan['JOB'])
        deployment.check_approved_jobs(cursor,plan)
        cursor.fetchall.return_value[0]['SQL_COMMAND']='SELECT 999'
        with self.assertRaisesRegex(ValueError,'approved DEV'):
            deployment.check_approved_jobs(cursor,plan)

    def test_saved_sql_discards_namespace_setup_without_executing_it(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'table.sql'
            path.write_text('-- header\nUSE DATABASE KUMO_DEV;\nCREATE OR REPLACE TABLE KUMO_DEV.MONITOR_META.X (ID VARCHAR);')
            self.assertEqual(deployment.sql_from(path),'CREATE OR REPLACE TABLE KUMO_DEV.MONITOR_META.X (ID VARCHAR)')

    def test_retirement_requires_explicit_valid_target_and_checks_store_before_writes(self):
        with patch.object(retirement,'connect') as connect:
            with self.assertRaises(ValueError): retirement.run(True,'PRD')
            connect.assert_not_called()
        for target,wrong in [('DEV','KUMO_TST'),('TST','KUMO_DEV')]:
            connection=MagicMock();cursor=MagicMock()
            connection.__enter__.return_value=connection
            connection.cursor.return_value.__enter__.return_value=cursor
            cursor.fetchall.return_value=[{'PARAMETER_VALUE':wrong}]
            with self.subTest(target=target),patch.object(retirement,'connect',return_value=connection),self.assertRaisesRegex(ValueError,'environment routing'):
                retirement.run(True,target)
            statements=[call.args[0] for call in cursor.execute.call_args_list]
            self.assertEqual(statements[:2],['USE DATABASE KUMO_'+target,'USE SCHEMA KUMO_'+target+'.MONITOR_APP'])
            self.assertFalse(any(sql.startswith(('ALTER','UPDATE','DELETE','DROP','CREATE','MERGE')) for sql in statements))


if __name__=='__main__':unittest.main()
