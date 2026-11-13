import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend/migrations'))
from migration_support import procedure_parts
from apply_workflow_command_templates import logical_body
import apply_manual_dbt_full_refresh as migration

NODE = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const workflow = {WORKFLOW_ID:'wf', IS_ENABLED:true, ALLOW_MANUAL_TRIGGER:true, WAREHOUSE_NAME:'WH'};
const definitions = [
  {JOB_ID:'first', JOB_NAME:'First', JOB_TYPE:input.jobType || 'DBT', DBT_COMMAND:'dbt build --select first', IS_ENABLED:true,TIMEOUT_SECONDS:null},
  {JOB_ID:'later', JOB_NAME:'Later', JOB_TYPE:'DBT', DBT_COMMAND:'dbt run --select later', IS_ENABLED:true,TIMEOUT_SECONDS:null}
];
const dependencies = [{JOB_ID:null,ALLOW:true}, {JOB_ID:'first',PARENT_ID:'wf',ALLOW:true}, {JOB_ID:'later',PARENT_ID:'first',ALLOW:true}];
const resolved = [], snapshots = [];
function query(sql, binds) {
  if (sql.includes('SP_GET_ENVIRONMENT')) return [{SP_GET_ENVIRONMENT:{DATABASE:'KUMO_DEV',META_SCHEMA:'MONITOR_META',MONITOR_APP_SCHEMA:'MONITOR_APP'}}];
  if (sql.startsWith('SELECT WORKFLOW_RUN_ID')) return input.previous ? [input.previous] : [];
  if (sql.startsWith('SELECT * FROM') && sql.includes('.WORKFLOW WHERE')) return [workflow];
  if (sql.startsWith('SELECT * FROM') && sql.includes('.DEPENDENCIES')) return dependencies;
  if (sql.startsWith('SELECT * FROM') && sql.includes('.JOB WHERE')) return definitions.filter(job=>job.JOB_ID===binds[0]);
  if (sql.includes('UUID_STRING')) return [{ID:'id-'+snapshots.length}];
  if (sql.includes('SP_RESOLVE_WORKFLOW_COMMAND')) {
    const definition=JSON.parse(binds[0]); resolved.push(definition);
    return [{SP_RESOLVE_WORKFLOW_COMMAND:{ok:true,execSql:'SELECT 1',fullRefresh:definition.fullRefresh}}];
  }
  if (sql.startsWith('INSERT INTO')) {snapshots.push(JSON.parse(binds[binds.length===10 ? 9 : 13])); return [];}
  if (['BEGIN','COMMIT','ROLLBACK'].includes(sql) || sql.startsWith('UPDATE')) return [];
  throw Error('Unexpected query: '+sql);
}
const snowflake = {createStatement({sqlText,binds=[]}) {
  let values=[], keys=[];
  return {execute() {values=query(sqlText,binds); keys=[...new Set(values.flatMap(row=>Object.keys(row)))]; let index=-1;
    return {next(){return ++index<values.length},getColumnValue(n){return values[index][keys[n-1]]}}},
    getColumnCount(){return keys.length},getColumnName(n){return keys[n-1]},getNumRowsAffected(){return 1}};
}};
const run = new Function('snowflake','P_WORKFLOW_ID','P_RUN_MODE','P_JOB_ID','P_REQUEST_ID','P_REQUESTED_BY','P_FULL_REFRESH',input.source);
const result=run(snowflake,'wf',input.mode||'FROM_JOB',input.mode==='WORKFLOW'?null:'first','request','actor',input.fullRefresh??true);
process.stdout.write(JSON.stringify({result,resolved,snapshots,definitions}));
"""


class AdmissionTests(unittest.TestCase):
    def admission(self, **options):
        source = procedure_parts((ROOT / 'snowflake/ddl/KUMO_DEV/MONITOR_APP/procedures/SP_ORCH_REQUEST_RUN.sql').read_text())[1]
        output = subprocess.run(['node', '-e', NODE], input=json.dumps(dict(source=source, **options)), text=True, capture_output=True, check=True)
        return json.loads(output.stdout)

    def test_refresh_changes_only_selected_job_resolution_and_snapshot(self):
        result = self.admission()
        self.assertTrue(result['result']['ok'], result)
        self.assertEqual([row['fullRefresh'] for row in result['resolved']], [True, False])
        self.assertTrue(result['snapshots'][0]['fullRefresh'])
        self.assertEqual(result['definitions'][0]['DBT_COMMAND'], 'dbt build --select first')
        self.assertNotIn('fullRefresh', result['definitions'][0])

    def test_job_only_excludes_later_jobs_and_default_workflow_runs_stay_incremental(self):
        only = self.admission(mode='JOB_ONLY')
        self.assertTrue(only['result']['ok'], only)
        self.assertEqual(len(only['resolved']), 1)
        normal = self.admission(mode='WORKFLOW', fullRefresh=False)
        self.assertTrue(normal['result']['ok'], normal)
        self.assertTrue(all(not row['fullRefresh'] for row in normal['resolved']))

    def test_non_dbt_selection_and_changed_request_options_are_rejected(self):
        result = self.admission(jobType='SQL')
        self.assertFalse(result['result']['ok'])
        self.assertEqual(result['snapshots'], [])
        previous = dict(WORKFLOW_RUN_ID='old', RUN_MODE='FROM_JOB', REQUESTED_JOB_ID='first', STATUS='SUCCESS',
                        TASK_FQN='task', CONFIG_SNAPSHOT={'fullRefresh': False})
        result = self.admission(previous=previous)
        self.assertFalse(result['result']['ok'])
        self.assertIn('different request', result['result']['error'])
        previous['CONFIG_SNAPSHOT']['fullRefresh'] = True
        self.assertTrue(self.admission(previous=previous)['result']['reused'])

    def test_saved_resolver_matches_the_maintained_javascript_source(self):
        path = next((ROOT / 'snowflake/ddl/KUMO_DEV/MONITOR_APP/procedures').glob('SP_RESOLVE_WORKFLOW_COMMAND__*.sql'))
        expected = (ROOT / 'backend/migrations/workflow_command_resolver.js').read_text().replace('[DATABASE].[META_SCHEMA]', 'KUMO_DEV.MONITOR_META').strip()
        self.assertEqual(logical_body(path.read_text()).strip(), expected)


class MigrationTests(unittest.TestCase):
    def cursor(self, active=0, argument_count=5):
        cursor = MagicMock()
        grants = [dict(privilege='OWNERSHIP', grantee_name='OWNER', granted_to='ROLE'),
                  dict(privilege='USAGE', grantee_name='APP ROLE', granted_to='ROLE', grant_option='true')]
        def execute(sql, params=()):
            if 'SP_GET_ENVIRONMENT' in sql:
                cursor.fetchone.return_value = dict(RESULT=dict(DATABASE='KUMO_TST', META_SCHEMA='MONITOR_META', MONITOR_APP_SCHEMA='MONITOR_APP'))
            elif 'COUNT(*)' in sql: cursor.fetchone.return_value = dict(N=active)
            elif sql.startswith('SHOW PROCEDURES'):
                cursor.fetchall.return_value = [dict(description='KUMO parallel orchestration v2', max_num_arguments=argument_count)]
            elif sql.startswith('SHOW GRANTS'): cursor.fetchall.return_value = grants
            elif sql.startswith('SELECT CURRENT_ROLE'): cursor.fetchone.return_value = dict(ROLE='OWNER')
            elif 'GET_DDL' in sql: cursor.fetchone.return_value = dict(DDL='CREATE ORIGINAL')
            return cursor
        cursor.execute.side_effect = execute
        return cursor

    def test_default_is_read_only_and_only_inspects_the_selected_environment(self):
        cursor = self.cursor()
        result = migration.upgrade(cursor, 'KUMO_TST')
        self.assertFalse(result['applied'])
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertFalse(any(sql.startswith(('CREATE', 'DROP', 'GRANT', 'ALTER')) for sql in statements))
        self.assertFalse(any('KUMO_DEV' in sql for sql in statements))

    def test_upgrade_replaces_the_old_signature_and_preserves_usage_grants(self):
        cursor = self.cursor()
        migration.upgrade(cursor, 'KUMO_TST', apply=True)
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertEqual(sum(sql.startswith('CREATE OR REPLACE PROCEDURE') for sql in statements), 2)
        self.assertTrue(any(sql.startswith('DROP PROCEDURE') and sql.endswith(migration.OLD_SIGNATURE) for sql in statements))
        self.assertTrue(any(sql.startswith('GRANT USAGE') and migration.SIGNATURE in sql and '"APP ROLE" WITH GRANT OPTION' in sql for sql in statements))
        self.assertFalse(any('KUMO_DEV' in sql for sql in statements))

    def test_active_runs_block_replacement(self):
        cursor = self.cursor(active=1)
        with self.assertRaisesRegex(ValueError, 'Finish pending'):
            migration.upgrade(cursor, 'KUMO_TST', apply=True)
        self.assertFalse(any(call.args[0].startswith(('CREATE', 'DROP', 'GRANT')) for call in cursor.execute.call_args_list))

    def test_rerun_preserves_grants_without_dropping_the_new_signature(self):
        cursor = self.cursor(argument_count=6)
        migration.upgrade(cursor, 'KUMO_TST', apply=True)
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertFalse(any(sql.startswith('DROP') for sql in statements))
        self.assertEqual(sum('COPY GRANTS' in sql for sql in statements), 2)

    def test_failed_replacement_restores_the_original_entrypoint_and_grants(self):
        cursor = self.cursor()
        original_execute = cursor.execute.side_effect
        def execute(sql, params=()):
            if sql.startswith('CREATE OR REPLACE PROCEDURE') and 'P_FULL_REFRESH' in sql:
                raise RuntimeError('create failed')
            return original_execute(sql, params)
        cursor.execute.side_effect = execute
        with self.assertRaisesRegex(RuntimeError, 'create failed'):
            migration.upgrade(cursor, 'KUMO_TST', apply=True)
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertIn('CREATE ORIGINAL', statements)
        self.assertTrue(any(sql.startswith('GRANT USAGE') and migration.OLD_SIGNATURE in sql for sql in statements))


if __name__ == '__main__':
    unittest.main()
