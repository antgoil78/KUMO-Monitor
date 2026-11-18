"""Exercise the actual notifier body and the rule migration without sending mail."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from flask import Flask

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT / 'backend/migrations'))
from migration_support import procedure_parts
from apply_workflow_notifications import build_plan
import notification_admin as admin
import config

WF = '00000000-0000-0000-0000-000000000001'
RUN = '00000000-0000-0000-0000-000000000002'

NODE = r"""
const input=JSON.parse(require('fs').readFileSync(0,'utf8'));
const root={EXECUTION_ID:input.run,WORKFLOW_ID:input.workflow,STATUS:input.status||'FAILED',
  ERROR_MESSAGE:'One or more jobs failed',RUN_MODE:'WORKFLOW',TRIGGER_SOURCE:'MANUAL',
  CONFIG_SNAPSHOT:{workflow:{WORKFLOW_NAME:'Test LOAD',WORKFLOW_GROUP:'TEST'}},RESULT:{succeeded:1,dependencyEvaluation:{ok:true}}};
if(input.previous) root.RESULT.notification=input.previous;
const sends=[],queries=[];let affected=1, persisted=false;
function query(sql,b){
  queries.push({sql,binds:b});
  if(sql.includes('SP_GET_ENVIRONMENT')) return [{ENV:{DATABASE:'KUMO_DEV',META_SCHEMA:'MONITOR_META',MONITOR_APP_SCHEMA:'MONITOR_APP',_parameterTable:'CUSTOM.CONFIG.PARAMETERS'}}];
  if(sql.startsWith('SELECT * FROM')) return input.job?[]:[root];
  if(sql.startsWith('SELECT 1 AS ENABLED')) return input.historical?[]:[{ENABLED:1}];
  if(sql.includes('UUID_STRING')) return [{ID:'claim'}];
  if(sql.startsWith('UPDATE')){
    affected=input.claimLost&&!persisted?0:1;
    const report=JSON.parse(b[0]);
    if(input.persistFails&&report.status==='SENT') throw Error('Result write failed after sending');
    if(affected) root.RESULT.notification=report;
    persisted=true;return [];
  }
  if(sql.startsWith('SELECT n.*')) return input.unconfigured?[]:[{
    ON_FAIL_EMAIL:!input.disabled,ON_SUCCESS_EMAIL:!!input.successEnabled,
    FAIL_GROUP:input.noGroup?'':'Ops',SUCCESS_GROUP:'Success',RECIPIENTS:input.empty?'':'ops@example.test',
    EMAIL_INTEGRATION:'MY_EMAIL_INT',ENVIRONMENT:'DEV'}];
  if(sql.startsWith('SELECT EXECUTION_ID, JOB_ID')) return [{JOB_NAME:'SDL',STATUS:'FAILED',ERROR_MESSAGE:'Division by zero',QUERY_ID:'job-query'}];
  if(sql.includes('SP_SEND_WORKFLOW_EMAIL')){
    sends.push(b);if(input.sendFails)throw Error('Mail unavailable');return [{SENT:'EMAIL_SENT'}];
  }
  throw Error('Unexpected query: '+sql);
}
const snowflake={createStatement({sqlText,binds=[]}){
  let values=[],keys=[];return {execute(){values=query(sqlText,binds);keys=[...new Set(values.flatMap(r=>Object.keys(r)))];let i=-1;
    return {next(){return ++i<values.length},getColumnValue(n){return values[i][keys[n-1]]}};},
    getColumnCount(){return keys.length},getColumnName(n){return keys[n-1]},getNumRowsAffected(){return affected}};
}};
const fn=new Function('snowflake','P_RUN_ID',input.source),reports=[];
for(let i=0;i<(input.repeat?2:1);i++)reports.push(fn(snowflake,input.run));
process.stdout.write(JSON.stringify({root,sends,reports,queries}));
"""


class NotifierTests(unittest.TestCase):
    def execute(self, **options):
        source = procedure_parts((ROOT / 'snowflake/ddl/KUMO_DEV/MONITOR_APP/procedures/SP_ORCH_NOTIFY.sql').read_text())[1]
        run = subprocess.run(['node', '-e', NODE], input=json.dumps(dict(source=source, run=RUN, workflow=WF, **options)),
                             text=True, capture_output=True, check=True)
        return json.loads(run.stdout)

    def test_failed_workflow_sends_once_with_job_details_and_preserves_other_results(self):
        output = self.execute(repeat=True)
        self.assertEqual(len(output['sends']), 1)
        self.assertEqual(output['reports'][0]['status'], 'SENT')
        self.assertEqual(output['root']['STATUS'], 'FAILED')
        self.assertEqual(output['root']['RESULT']['succeeded'], 1)
        self.assertTrue(output['root']['RESULT']['dependencyEvaluation']['ok'])
        self.assertEqual(json.loads(output['sends'][0][-1])['failed_jobs'][0]['ERROR_MESSAGE'], 'Division by zero')
        self.assertTrue(any('CUSTOM.CONFIG.PARAMETERS' in query['sql'] for query in output['queries']))

    def test_skip_paths_never_send(self):
        for options, reason in [({'historical': True}, 'BEFORE_ACTIVATION'), ({'job': True}, 'NOT_WORKFLOW'),
                                ({'status': 'RUNNING'}, 'NOT_TERMINAL'), ({'status': 'SKIPPED'}, 'NOT_TERMINAL'),
                                ({'disabled': True}, 'DISABLED'), ({'unconfigured': True}, 'NOT_CONFIGURED'),
                                ({'noGroup': True}, 'NO_GROUP'), ({'claimLost': True}, 'ALREADY_CLAIMED')]:
            with self.subTest(options=options):
                output = self.execute(**options)
                self.assertEqual(output['sends'], [])
                self.assertEqual(output['reports'][0]['reason'], reason)

    def test_success_and_warning_require_success_rule_cancellation_uses_failure_rule(self):
        for status in ('SUCCESS', 'WARNING'):
            self.assertEqual(self.execute(status=status)['sends'], [])
            output = self.execute(status=status, successEnabled=True)
            self.assertEqual(output['reports'][0]['group'], 'Success')
            self.assertEqual(len(output['sends']), 1)
        self.assertEqual(self.execute(status='CANCELLED')['reports'][0]['group'], 'Ops')

    def test_mail_and_recording_errors_are_durable_and_never_repeat_send(self):
        for options in ({'sendFails': True}, {'persistFails': True}, {'empty': True}):
            output = self.execute(repeat=True, **options)
            self.assertFalse(output['reports'][0]['ok'])
            self.assertEqual(output['reports'][0]['status'], 'ERROR')
            self.assertLessEqual(len(output['sends']), 1)
            self.assertEqual(output['root']['STATUS'], 'FAILED')
        self.assertEqual(self.execute(previous={'ok': True, 'status': 'SENDING'})['sends'], [])


class MigrationTests(unittest.TestCase):
    def plan(self, notifications, **kwargs):
        return build_plan([dict(WORKFLOW_ID=WF,WORKFLOW_NAME='LOAD',MIGRATED_FROM_WORKFLOW_ID='ready')],
                          [dict(JOB_ID='job',MIGRATED_FROM_WORKFLOW_ID='dbt')],
                          [dict(WORKFLOW_ID=WF,JOB_ID='job')], notifications, **kwargs)

    def rule(self, source, group='Ops', **values):
        return dict(WORKFLOW_ID=source,ON_FAIL_EMAIL=True,FAIL_GROUP=group,ON_SUCCESS_EMAIL=False,
                    SUCCESS_GROUP='',EMAIL_INTEGRATION='MY_EMAIL_INT',ENVIRONMENT='DEV',**values)

    def test_maps_all_combined_job_sources_and_deduplicates_same_rule(self):
        rows = [self.rule('ready'), self.rule('dbt')]
        self.assertEqual(len(self.plan(rows)), 1)
        self.assertTrue(self.plan(rows)[0]['values']['ON_FAIL_EMAIL'])
        self.assertEqual(rows[0]['WORKFLOW_ID'], 'ready')

    def test_disabled_or_empty_old_groups_do_not_gain_recipients(self):
        plan = self.plan([self.rule('ready', group='')])
        self.assertFalse(plan[0]['values']['ON_FAIL_EMAIL'])
        self.assertEqual(plan[0]['values']['FAIL_GROUP'], '')

    def test_no_legacy_rule_means_no_new_rule_and_existing_new_rules_are_preserved(self):
        self.assertEqual(self.plan([]), [])
        self.assertEqual(self.plan([self.rule(WF),self.rule('ready')]), [])

    def test_conflicting_recipients_integrations_and_duplicate_rows_block_migration(self):
        for rows in ([self.rule('ready'),self.rule('dbt',group='Other')],
                     [self.rule('ready'),{**self.rule('dbt'),'EMAIL_INTEGRATION':'OTHER'}],
                     [self.rule('ready'),self.rule('ready')]):
            with self.assertRaises(ValueError): self.plan(rows)


class NotificationApiTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(admin.notification_admin_bp)
        self.client = app.test_client()
        self.values = dict(onFailEmail=True,onSuccessEmail=False,failGroup='Ops',successGroup='',emailIntegration='MY_EMAIL_INT')

    def test_save_uses_current_catalog_and_bound_settings(self):
        with patch.object(config,'USE_MOCK',False),patch.object(config,'LEGACY_ENGINE_RETIRED',True), \
             patch.object(admin.sf,'is_configured',return_value=True),patch.object(admin.sf,'connection_scope'), \
             patch.object(admin.sf,'query_one_service',side_effect=[{'WORKFLOW_ID':WF},{'RECIPIENTS':'ops@example.test'}]) as read, \
             patch.object(admin.sf,'execute_service') as write:
            response = self.client.patch('/api/notification-workflows/'+WF,json=self.values)
        self.assertEqual(response.status_code,200,response.get_json())
        self.assertIn(config.T_ORCHESTRATION_WORKFLOW,read.call_args_list[0].args[0])
        self.assertEqual(write.call_args.args[1]['workflow_id'],WF)
        self.assertTrue(write.call_args.args[1]['on_fail'])

    def test_invalid_booleans_and_missing_groups_fail_before_writing(self):
        for values in ({**self.values,'onFailEmail':'false'},{**self.values,'failGroup':''},None):
            with patch.object(admin.sf,'execute_service') as write:
                response = self.client.patch('/api/notification-workflows/'+WF,json=values)
                self.assertEqual(response.status_code,400,response.get_json())
                write.assert_not_called()

    def test_missing_workflow_and_empty_recipient_group_do_not_write(self):
        for rows,status in [([{}],404),([{'WORKFLOW_ID':WF},{'RECIPIENTS':''}],400)]:
            with patch.object(config,'USE_MOCK',False),patch.object(admin.sf,'is_configured',return_value=True), \
                 patch.object(admin.sf,'connection_scope'),patch.object(admin.sf,'query_one_service',side_effect=rows), \
                 patch.object(admin.sf,'execute_service') as write:
                response = self.client.patch('/api/notification-workflows/'+WF,json=self.values)
            self.assertEqual(response.status_code,status,response.get_json())
            write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
