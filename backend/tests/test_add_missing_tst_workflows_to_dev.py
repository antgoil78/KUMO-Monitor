import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'migrations'))
import add_missing_tst_workflows_to_dev as migration
from migrate_workflow_catalog import build_plan
from test_migrate_workflow_catalog import fixture, source


def catalogs():
    workflows, tasks = fixture()
    current, _ = build_plan(workflows, tasks)
    workflows += [source('card-ready', 'CARD READY TO LOAD', 'CARD'),
                  source('card-dbt', 'CARD SDL EDV', 'CARD', dbt='dbt build -s models/data/SDL/CARD')]
    tasks += [dict(WORKFLOW_ID='card-ready', ON_SUCCESS=['card-dbt'], IS_ENABLED=False),
              dict(WORKFLOW_ID='card-dbt', ON_SUCCESS=[], IS_ENABLED=False)]
    next(task for task in tasks if task['WORKFLOW_ID'] == 'raw')['ON_SUCCESS'].append('card-ready')
    return dict(WORKFLOWS=workflows, WORKFLOW_TASKS=tasks, RAW_LIM_PKG_GROUP_SOURCE=[]), {**current, migration.MAPPING: []}


class MissingTstWorkflowTests(unittest.TestCase):
    def test_adds_only_missing_complete_workflow_with_source_parent_and_no_schedule_activation(self):
        source_catalog, current = catalogs()
        before = copy.deepcopy(current)
        additions, expected, summary = migration.missing_plan(source_catalog, current, 'DEV_WH')
        self.assertEqual(summary['rows_to_insert'], dict(WORKFLOW=1, JOB=2, DEPENDENCIES=3, RAW_LIM_PKG_GROUP_SOURCE=0))
        workflow = additions['WORKFLOW'][0]
        self.assertEqual(workflow['WORKFLOW_NAME'], 'CARD LOAD')
        self.assertEqual(workflow['WAREHOUSE_NAME'], 'DEV_WH')
        self.assertFalse(workflow['IS_SCHEDULE_ENABLED'])
        self.assertFalse(workflow['ALLOW_EXTERNAL_TRIGGER'])
        self.assertFalse(workflow['IS_ENABLED'])
        self.assertEqual([row['JOB_NAME'] for row in additions['JOB']], ['CARD READY TO LOAD', 'CARD SDL'])
        header = next(row for row in additions['DEPENDENCIES'] if row['JOB_ID'] is None)
        raw = next(row for row in expected['WORKFLOW'] if row['WORKFLOW_NAME'] == 'LOAD RAW DAILY')
        self.assertEqual(header['PARENT_ID'], raw['WORKFLOW_ID'])
        self.assertTrue(header['SUCCESS'])
        self.assertEqual(current, before)

    def test_repeat_is_noop_and_preserves_dev_execution_flags(self):
        source_catalog, current = catalogs()
        additions, _, _ = migration.missing_plan(source_catalog, current, 'DEV_WH')
        for table, rows in additions.items():
            current[table].extend(rows)
        current['WORKFLOW'][0].update(IS_SCHEDULE_ENABLED=True, ALLOW_MANUAL_TRIGGER=False)
        current['JOB'][0]['IS_ENABLED'] = False
        additions, _, _ = migration.missing_plan(source_catalog, current, 'DEV_WH')
        self.assertFalse(any(additions.values()))

    def test_command_drift_name_collision_partial_migration_and_dependency_drift_fail(self):
        for mode in ('command', 'name', 'partial', 'dependency'):
            source_catalog, current = catalogs()
            if mode == 'command':
                current['JOB'][0]['SQL_COMMAND'] = 'SELECT 999'
            elif mode == 'name':
                current['WORKFLOW'].append(dict(WORKFLOW_ID='other', WORKFLOW_NAME='CARD LOAD'))
            elif mode == 'partial':
                additions, _, _ = migration.missing_plan(source_catalog, current, 'DEV_WH')
                current['JOB'].extend(additions['JOB'])
            else:
                current['DEPENDENCIES'][0]['ALLOW'] = False
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                migration.missing_plan(source_catalog, current, 'DEV_WH')

    def test_only_retired_cleanup_call_is_removed_and_other_sql_drift_still_fails(self):
        source_catalog, current = catalogs()
        cleanup = next(row for row in source_catalog['WORKFLOWS'] if row['WORKFLOW_NAME'] == 'MAINTENANCE - CLEANUP')
        cleanup['SQL_COMMAND'] = 'CALL [DATABASE].[MONITOR_APP_SCHEMA].SP_CLEANUP_OLD_TMP_TASKS(1);\nSELECT 1'
        migration.missing_plan(source_catalog, current, 'DEV_WH')
        cleanup['SQL_COMMAND'] += '; SELECT 2'
        with self.assertRaisesRegex(ValueError, 'SQL_COMMAND'):
            migration.missing_plan(source_catalog, current, 'DEV_WH')

    def test_transaction_inserts_only_and_preserves_existing_rows(self):
        source_catalog, current = catalogs()
        additions, _, _ = migration.missing_plan(source_catalog, current, 'DEV_WH')
        after = {table: current[table] + additions[table] for table in current}
        cursor = MagicMock()
        with patch.object(migration, 'fingerprint', return_value={'HASH': 1}), patch.object(migration, 'read', return_value=after):
            migration.insert_missing(cursor, additions, current, {'WORKFLOWS': {'HASH': 1}}, {'JOB': {'HASH': 1}})
        self.assertEqual(cursor.execute.call_args_list[-1].args[0], 'COMMIT')
        self.assertEqual(cursor.executemany.call_count, 3)
        for call in cursor.execute.call_args_list:
            self.assertFalse(call.args[0].startswith(('DELETE', 'CREATE', 'TRUNCATE', 'ALTER', 'CALL')))

    def test_missing_package_configuration_omits_tst_checkpoints_and_preserves_existing_dev_state(self):
        source_row = dict(PKG_GROUP_NAME='PKG_CARD_1', SUBJECT_AREA='CARD', SOURCE_ID='CCM',
                          PKG_ID='001', ACTIVE_FL=True, DLVY_PKG_YEAR='2026', DLVY_PKG_YEAR_SEQ_NO='00225')
        jobs = [dict(JOB_TYPE='SQL', SQL_COMMAND="CALL READY(PKG_GROUP_NAME => 'PKG_CARD_1');")]
        additions = migration.missing_packages([source_row], [], jobs)
        self.assertIsNone(additions[0]['DLVY_PKG_YEAR'])
        self.assertIsNone(additions[0]['DLVY_PKG_YEAR_SEQ_NO'])
        existing = [{**source_row, 'DLVY_PKG_YEAR_SEQ_NO': '00001'}]
        self.assertEqual(migration.missing_packages([source_row], existing, jobs), [])
        self.assertEqual(existing[0]['DLVY_PKG_YEAR_SEQ_NO'], '00001')
        with self.assertRaisesRegex(ValueError, 'no active'):
            migration.missing_packages([], [], jobs)

    def test_resolution_is_read_only_in_both_environments_and_rejects_cross_environment_sql(self):
        jobs = [dict(JOB_NAME='READY', JOB_TYPE='SQL', SQL_COMMAND='SELECT 1',
                     DBT_COMMAND=None, DBT_TARGET=None, DBT_PROJECT_FQN=None)]
        cursor = MagicMock()
        cursor.fetchone.side_effect = [dict(RESULT=dict(ok=True, execSql='SELECT 1'))] * 2
        self.assertEqual(migration.validate_commands(cursor, jobs), 2)
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertEqual(len(statements), 2)
        self.assertTrue(all('SP_RESOLVE_WORKFLOW_COMMAND' in sql for sql in statements))
        self.assertIn('KUMO_DEV.', statements[0])
        self.assertIn('KUMO_TST.', statements[1])
        cursor.fetchone.side_effect = [dict(RESULT=dict(ok=True, execSql='CALL KUMO_TST.APP.RUN()'))]
        with self.assertRaisesRegex(ValueError, 'cross-environment'):
            migration.validate_commands(cursor, jobs)

    def test_concurrent_change_insert_failure_or_readback_failure_rolls_back(self):
        source_catalog, current = catalogs()
        additions, _, _ = migration.missing_plan(source_catalog, current, 'DEV_WH')
        for mode in ('concurrent', 'insert', 'readback'):
            cursor = MagicMock()
            if mode == 'insert':
                cursor.executemany.side_effect = RuntimeError('insert failed')
            with self.subTest(mode=mode), patch.object(migration, 'fingerprint', return_value={'HASH': 2 if mode == 'concurrent' else 1}), patch.object(migration, 'read', return_value=current):
                with self.assertRaises((ValueError, RuntimeError)):
                    migration.insert_missing(cursor, additions, current, {'WORKFLOWS': {'HASH': 1}}, {'JOB': {'HASH': 1}})
            self.assertEqual(cursor.execute.call_args_list[-1].args[0], 'ROLLBACK')
            self.assertNotIn('COMMIT', [call.args[0] for call in cursor.execute.call_args_list])


if __name__ == '__main__':
    unittest.main()
