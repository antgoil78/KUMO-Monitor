import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'migrations'))
import migrate_workflow_catalog as migration


def source(workflow_id, workflow_name, group, sql='SELECT 1', dbt=None):
    return dict(WORKFLOW_ID=workflow_id, WORKFLOW_NAME=workflow_name, WORKFLOW_GROUP=group,
                WORKFLOW_TYPE='DBT' if dbt else 'SQL', SQL_COMMAND=None if dbt else sql,
                DBT_COMMAND=dbt or '', DBT_TARGET='[DBT_TARGET]' if dbt else None,
                DBT_PROJECT_FQN='[DATABASE].[DBT_SCHEMA].[DBT_PROJECT]' if dbt else None,
                DBT_WORKSPACE='', IS_ENABLED=True, DESCRIPTION=workflow_name)


def fixture():
    workflows = [source('ready', 'PYMT READY TO LOAD', 'PYMT', sql='CALL FIRST();\nCALL SECOND();'),
                 source('dbt', 'PYMT SDL EDV', 'PYMT', dbt='dbt build -s models/data/SDL/PYMT models/data/EDV/PYMT'),
                 source('raw', 'LOAD RAW DAILY', 'GENERAL'),
                 source('maintenance', 'MAINTENANCE - CLEANUP', 'MAINTENANCE'),
                 source('excluded', 'RUN DEPENDENCIES', 'DEPS')]
    tasks = [dict(WORKFLOW_ID=row['WORKFLOW_ID'], SCHEDULE_CRON='0 7 * * 1-5', SCHEDULE_TIMEZONE='UTC',
                  IS_ENABLED=False, ON_SUCCESS=[], ON_FAIL=[]) for row in workflows]
    tasks[0]['ON_SUCCESS'] = '["dbt"]'
    tasks[2]['ON_SUCCESS'] = ['ready']
    return workflows, tasks


class WorkflowCatalogMigrationTests(unittest.TestCase):
    def setUp(self):
        self.workflows, self.tasks = fixture()

    def test_groups_readiness_and_split_dbt_jobs_and_preserves_commands_and_lineage(self):
        before = copy.deepcopy((self.workflows, self.tasks))
        plan, summary = migration.build_plan(self.workflows, self.tasks)
        self.assertEqual(summary['target_counts'], {'WORKFLOW': 3, 'JOB': 5, 'DEPENDENCIES': 8})
        self.assertEqual(summary['excluded'], ['RUN DEPENDENCIES'])
        workflow = next(row for row in plan['WORKFLOW'] if row['WORKFLOW_NAME'] == 'PYMT LOAD')
        self.assertFalse(workflow['IS_SCHEDULE_ENABLED'])
        self.assertEqual(workflow['SCHEDULE'], '0 7 * * 1-5')
        jobs = {row['JOB_NAME']: row for row in plan['JOB']}
        self.assertEqual(jobs['PYMT READY TO LOAD']['SQL_COMMAND'], 'CALL FIRST();\nCALL SECOND();')
        self.assertEqual(jobs['PYMT SDL']['DBT_COMMAND'], 'dbt build -s models/data/SDL/PYMT')
        self.assertEqual(jobs['PYMT EDV']['DBT_COMMAND'], 'dbt build -s models/data/EDV/PYMT')
        self.assertEqual(jobs['PYMT SDL']['JOB_LAYER'], 'SDL')
        self.assertEqual(jobs['PYMT EDV']['JOB_LAYER'], 'EDV')
        for job_name in ('PYMT SDL', 'PYMT EDV'):
            self.assertEqual(jobs[job_name]['DBT_TARGET'], '[DBT_TARGET]')
            self.assertEqual(jobs[job_name]['DBT_PROJECT_FQN'], '[DATABASE].[DBT_SCHEMA].[DBT_PROJECT]')
            self.assertEqual(jobs[job_name]['MIGRATED_FROM_WORKFLOW_ID'], 'dbt')
        self.assertEqual((self.workflows, self.tasks), before)
        second, _ = migration.build_plan(self.workflows, self.tasks)
        self.assertEqual(plan, second)

    def test_first_job_has_workflow_parent_and_success_edges_follow_the_chain(self):
        plan, _ = migration.build_plan(self.workflows, self.tasks)
        workflows = {row['WORKFLOW_NAME']: row for row in plan['WORKFLOW']}
        jobs = {row['JOB_NAME']: row for row in plan['JOB']}
        dependencies = {row['JOB_ID']: row for row in plan['DEPENDENCIES'] if row['JOB_ID']}
        workflow = workflows['PYMT LOAD']
        ready = dependencies[jobs['PYMT READY TO LOAD']['JOB_ID']]
        self.assertEqual(ready['PARENT_ID'], workflow['WORKFLOW_ID'])
        self.assertIsNone(ready['SUCCESS'])
        self.assertEqual(dependencies[jobs['PYMT SDL']['JOB_ID']]['PARENT_ID'], ready['JOB_ID'])
        self.assertEqual(dependencies[jobs['PYMT EDV']['JOB_ID']]['PARENT_ID'], jobs['PYMT SDL']['JOB_ID'])
        header = next(row for row in plan['DEPENDENCIES'] if row['WORKFLOW_ID'] == workflow['WORKFLOW_ID'] and row['JOB_ID'] is None)
        self.assertEqual(header['PARENT_ID'], workflows['LOAD RAW DAILY']['WORKFLOW_ID'])
        self.assertIs(header['SUCCESS'], True)
        self.assertIs(header['ERROR'], False)
        for workflow_name in ('LOAD RAW DAILY', 'MAINTENANCE - CLEANUP'):
            rows = [row for row in plan['DEPENDENCIES'] if row['WORKFLOW_ID'] == workflows[workflow_name]['WORKFLOW_ID'] and row['JOB_ID']]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['PARENT_ID'], workflows[workflow_name]['WORKFLOW_ID'])

    def test_ambiguous_commands_callbacks_or_active_intermediate_schedules_fail_before_writes(self):
        for mutate in (
            lambda workflows, tasks: workflows[1].update(DBT_COMMAND='dbt build -s models/data/SDL/PYMT+ --full-refresh'),
            lambda workflows, tasks: workflows[1].update(DBT_COMMAND='dbt build -s models/data/EDV/PYMT models/data/SDL/PYMT'),
            lambda workflows, tasks: tasks[1].update(IS_ENABLED=True),
            lambda workflows, tasks: tasks[0].update(ON_FAIL=['raw']),
            lambda workflows, tasks: tasks[2].update(ON_SUCCESS=['missing']),
            lambda workflows, tasks: tasks[3].update(ON_SUCCESS=['ready']),
            lambda workflows, tasks: tasks[3].update(ON_SUCCESS=['maintenance']),
        ):
            workflows, tasks = fixture()
            mutate(workflows, tasks)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                migration.build_plan(workflows, tasks)

    def test_replacement_commits_only_verified_new_tables_and_never_executes_job_commands(self):
        plan, _ = migration.build_plan(self.workflows, self.tasks)
        cursor = MagicMock()
        statements = []
        cursor.execute.side_effect = lambda sql: statements.append(sql)
        cursor.fetchall.side_effect = lambda: copy.deepcopy(plan[statements[-1].rsplit('.', 1)[1]])
        before = {table: {'N': 1, 'HASH': 123} for table in migration.SOURCES + migration.TARGETS}
        with patch.object(migration, 'fingerprint', side_effect=lambda cur, ns, table: before[table]):
            migration.replace_catalog(cursor, 'KUMO_DEV.MONITOR_META', plan, before, before)
        self.assertEqual(statements[-1], 'COMMIT')
        self.assertEqual([sql for sql in statements if sql.startswith('DELETE')], [
            'DELETE FROM KUMO_DEV.MONITOR_META.DEPENDENCIES',
            'DELETE FROM KUMO_DEV.MONITOR_META.JOB',
            'DELETE FROM KUMO_DEV.MONITOR_META.WORKFLOW',
        ])
        self.assertFalse(any(sql.startswith(('CALL', 'TRUNCATE', 'CREATE', 'ALTER')) for sql in statements))
        self.assertEqual(cursor.executemany.call_count, 3)

    def test_write_error_destination_change_or_failed_verification_rolls_back(self):
        plan, _ = migration.build_plan(self.workflows, self.tasks)
        before = {table: {'N': 1, 'HASH': 123} for table in migration.SOURCES + migration.TARGETS}
        for mode in ('insert', 'verification', 'destination_changed', 'source_changed'):
            cursor = MagicMock()
            statements = []
            cursor.execute.side_effect = lambda sql: statements.append(sql)
            cursor.fetchall.side_effect = lambda: ([] if mode == 'verification' else copy.deepcopy(plan[statements[-1].rsplit('.', 1)[1]]))
            if mode == 'insert':
                cursor.executemany.side_effect = RuntimeError('write failed')
            def fingerprint(cur, namespace, table):
                changed = mode == 'destination_changed' and table in migration.TARGETS or mode == 'source_changed' and table in migration.SOURCES
                return {'N': 2, 'HASH': 456} if changed else before[table]
            with self.subTest(mode=mode), patch.object(migration, 'fingerprint', side_effect=fingerprint):
                with self.assertRaises((ValueError, RuntimeError)):
                    migration.replace_catalog(cursor, 'KUMO_DEV.MONITOR_META', plan, before, before)
                self.assertEqual(statements[-1], 'ROLLBACK')
                self.assertNotIn('COMMIT', statements)
                if mode == 'destination_changed':
                    self.assertFalse(any(sql.startswith('DELETE') for sql in statements))


if __name__ == '__main__':
    unittest.main()
