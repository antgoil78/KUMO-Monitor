"""Run the actual executor/recovery bodies through a Snowflake API harness."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend/migrations'))
from migration_support import procedure_parts

NODE = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const wf='00000000-0000-0000-0000-000000000001', run='00000000-0000-0000-0000-000000000002';
const jobId='00000000-0000-0000-0000-000000000003';
const task='KUMO_DEV.MONITOR_APP.TMP_ORCH_WF_'+wf.replaceAll('-','_')+'_'+run.replaceAll('-','_');
const root={EXECUTION_ID:run,WORKFLOW_ID:wf,STATUS:input.recovery?'RUNNING':(input.initialStatus||'QUEUED'),TRIGGER_SOURCE:input.triggerSource||'MANUAL',RUN_MODE:input.runMode||'WORKFLOW',TASK_FQN:task};
const job={EXECUTION_ID:jobId,STATUS:input.recovery?'RUNNING':'INITIATING'};
const queries=[], calls=[], notificationCalls=[];
function query(sql,b) {
  queries.push({sql,binds:b});
  if(sql.includes('SP_GET_ENVIRONMENT')) return [{ENV:{DATABASE:'KUMO_DEV',META_SCHEMA:'MONITOR_META',MONITOR_APP_SCHEMA:'MONITOR_APP',_parameterTable:'KUMO_DEV.MONITOR_META.APPLICATION_PARAMETERS'}}];
  if(sql.includes('SP_ORCH_NOTIFY')) {
    notificationCalls.push({id:b[0],rootStatus:root.STATUS});
    if(input.notificationFails) throw Error('Notification unavailable');
    return [{REPORT:{ok:true,status:'SENT'}}];
  }
  if(sql.startsWith('SELECT e.EXECUTION_ID')) return ['FAILED','CANCELLED'].includes(root.STATUS)?[{EXECUTION_ID:run}]:[];
  if(sql.includes('SYSTEM$TASK_RUNTIME_INFO')) return [{NAME:task,GROUP_ID:'group',SCHEDULED_AT:'2026-11-17'}];
  if(sql.includes('UUID_STRING')) return [{ID:'claim'}];
  if(sql.startsWith('SHOW PARAMETERS')) return [{value:'60'}];
  if(sql.startsWith('SELECT * FROM')) return [root];
  if(sql.startsWith('WITH relations AS')) { if(input.gateFails) throw Error('Start gate unavailable'); return input.blockers||[]; }
  if(sql.startsWith('SELECT STATUS')) return [{STATUS:root.STATUS}];
  if(sql.startsWith('SELECT EXECUTION_ID, WORKFLOW_ID, STATUS, TASK_FQN')) return [root];
  if(sql.startsWith('SELECT STATE,')) return [{STATE:input.cancelled?'CANCELLED':'FAILED',ERROR_CODE:'test',ERROR_MESSAGE:'Native task stopped'}];
  if(sql.startsWith('SELECT EXECUTION_ID, CONFIG_SNAPSHOT')) return [{EXECUTION_ID:jobId,CONFIG_SNAPSHOT:{
    definition:{IS_ENABLED:true,JOB_TYPE:'SQL',TIMEOUT_SECONDS:60},dependency:{ALLOW:!input.skip},
    resolved:{execSql:'SELECT 1'},sqlCondition:input.conditionFalse?'SELECT FALSE':null}}];
  if(sql.startsWith('SELECT COUNT(*)')) return [{N:0}];
  if(sql==='SELECT FALSE') return [{VALUE:false}];
  if(sql.includes('SP_ORCH_DISPATCH_DEPENDENCIES') || sql.includes('SP_ORCH_EVALUATE_DEPENDENCIES')) {
    calls.push({id:b[0],rootStatus:root.STATUS,jobStatus:job.STATUS});
    if(input.evaluationFails) throw Error('Evaluator unavailable');
    return [{REPORT:{ok:true,evaluated:true,children:[{workflowName:'child',eligible:root.STATUS==='SUCCESS'}]}}];
  }
  if(sql.includes("'dependencyEvaluation', PARSE_JSON")) {
    if(input.persistenceFails) throw Error('Report write unavailable');
    const target=b[1]===run?root:job;
    target.RESULT={...(target.RESULT||{}),dependencyEvaluation:JSON.parse(b[0])};return [];
  }
  if(sql.startsWith('SELECT EXECUTION_ID') && sql.includes('RESULT:dependencyEvaluation IS NULL'))
    return [job,root].filter(row=>!row.RESULT?.dependencyEvaluation).map(row=>({EXECUTION_ID:row.EXECUTION_ID}));
  if(sql.startsWith('SELECT EXECUTION_ID')) return [];
  if(sql.startsWith('INSERT INTO') && sql.includes('ORCH_DISPATCH_EVENTS')) return [];
  if(sql.startsWith('UPDATE')) {
    if(sql.includes('SET STATUS = IFF')) {
      root.STATUS=input.recovery?(input.cancelled?'CANCELLED':'FAILED'):'FAILED';job.STATUS=root.STATUS;return [];
    }
    if(sql.includes("'startGate', PARSE_JSON")) { root.STATUS='QUEUED';root.RESULT={startGate:JSON.parse(b[0])};return []; }
    if(sql.includes("SET STATUS = 'QUEUED'")) { if(input.fatal) throw Error('Executor interrupted'); job.STATUS='QUEUED';return []; }
    if(sql.includes("SET STATUS = 'RUNNING'")) { if(sql.includes('JOB_ID IS NULL'))root.STATUS='RUNNING';else job.STATUS='RUNNING';return []; }
    if(sql.includes('SET STATUS = ?')) {
      if(sql.includes('HEARTBEAT_AT = SYSDATE(), RESULT')) {root.STATUS=b[0];root.RESULT=JSON.parse(b[1]);}
      else {job.STATUS=b[0];job.RESULT=JSON.parse(b[3]);}
      return [];
    }
    return [];
  }
  if(sql.startsWith('EXECUTE IMMEDIATE') && input.workloadFails) throw Error('Workload failed');
  if(sql.startsWith('SHOW TASKS')) return [];
  if(['BEGIN','COMMIT','ROLLBACK'].includes(sql) || /^(ALTER SESSION|DROP TASK|EXECUTE IMMEDIATE)/.test(sql)) return [];
  throw Error('Unexpected query: '+sql);
}
const snowflake={createStatement({sqlText,binds=[]}) {
  let values=[],keys=[];
  return {execute(){values=query(sqlText,binds);keys=[...new Set(values.flatMap(row=>Object.keys(row)))];let i=-1;
    return {next(){return ++i<values.length},getColumnValue(n){return values[i][keys[n-1]]}};},
    getColumnCount(){return keys.length},getColumnName(n){return keys[n-1]},getNumRowsAffected(){return 1},getQueryId(){return 'query'}};
}};
let result,error;
try { result=new Function('snowflake',input.recovery?'P_WORKFLOW_ID':'P_RUN_ID',input.source)(snowflake,input.recovery?wf:run); }
catch(e){error=e.message;}
process.stdout.write(JSON.stringify({result,error,root,job,calls,queries,notificationCalls}));
"""


class CompletionTests(unittest.TestCase):
    def execute(self, **options):
        name = 'SP_ORCH_RECONCILE' if options.get('recovery') else 'SP_ORCH_EXECUTE_WORKFLOW'
        source = procedure_parts((ROOT / 'snowflake/ddl/KUMO_DEV/MONITOR_APP/procedures' / (name + '.sql')).read_text())[1]
        result = subprocess.run(['node', '-e', NODE], input=json.dumps(dict(source=source, **options)),
                                text=True, capture_output=True, check=True)
        output = json.loads(result.stdout)
        self.assertFalse(any('SP_ORCH_REQUEST_RUN' in row['sql'] for row in output['queries']))
        return output

    def test_job_is_evaluated_after_its_status_and_workflow_after_final_status(self):
        output = self.execute()
        self.assertNotIn('error', output, output)
        self.assertEqual([row['rootStatus'] for row in output['calls']], ['RUNNING', 'SUCCESS'])
        self.assertEqual([row['jobStatus'] for row in output['calls']], ['SUCCESS', 'SUCCESS'])
        self.assertFalse(output['job']['RESULT']['dependencyEvaluation']['children'][0]['eligible'])
        self.assertTrue(output['root']['RESULT']['dependencyEvaluation']['children'][0]['eligible'])
        self.assertTrue(output['job']['RESULT']['commandCompleted'])
        self.assertEqual(output['root']['RESULT']['succeeded'], 1)

    def test_failed_and_skipped_jobs_also_get_reports(self):
        for options, status in [({'workloadFails': True}, 'FAILED'), ({'skip': True}, 'SKIPPED'), ({'conditionFalse': True}, 'SKIPPED')]:
            with self.subTest(options=options):
                output = self.execute(**options)
                self.assertNotIn('error', output, output)
                self.assertEqual(output['job']['STATUS'], status)
                self.assertEqual(len(output['calls']), 2)
                self.assertIn('dependencyEvaluation', output['job']['RESULT'])

    def test_evaluator_or_report_failure_does_not_fail_successful_workload(self):
        for options in ({'evaluationFails': True}, {'persistenceFails': True}):
            with self.subTest(options=options):
                output = self.execute(**options)
                self.assertNotIn('error', output, output)
                self.assertEqual(output['root']['STATUS'], 'SUCCESS')
                self.assertEqual(output['job']['STATUS'], 'SUCCESS')
                self.assertTrue(output['result']['warnings'])
                if options.get('evaluationFails'):
                    self.assertFalse(output['root']['RESULT']['dependencyEvaluation']['ok'])
                    self.assertTrue(output['job']['RESULT']['commandCompleted'])

    def test_executor_failure_evaluates_terminated_records_and_keeps_original_error(self):
        output = self.execute(fatal=True)
        self.assertEqual(output['error'], 'Executor interrupted')
        self.assertEqual(output['root']['STATUS'], 'FAILED')
        self.assertEqual(len(output['calls']), 2)
        self.assertIn('dependencyEvaluation', output['root']['RESULT'])

    def test_native_task_failure_and_cancellation_evaluate_recovered_records(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                output = self.execute(recovery=True, cancelled=cancelled)
                self.assertNotIn('error', output, output)
                self.assertEqual(output['root']['STATUS'], 'CANCELLED' if cancelled else 'FAILED')
                self.assertEqual(len(output['calls']), 2)
                self.assertIn('dependencyEvaluation', output['root']['RESULT'])
                self.assertEqual(len(output['result']['recoveredRunIds']), 1)

    def test_recovery_evaluation_errors_do_not_prevent_workload_recovery(self):
        output = self.execute(recovery=True, evaluationFails=True)
        self.assertNotIn('error', output, output)
        self.assertEqual(output['root']['STATUS'], 'FAILED')
        self.assertEqual(len(output['result']['recoveredRunIds']), 1)
        self.assertFalse(output['root']['RESULT']['dependencyEvaluation']['ok'])
        self.assertTrue(output['result']['errors'])

    def test_workflow_completion_and_native_recovery_call_notifier_once_for_root(self):
        for options,status in [({},'SUCCESS'),({'workloadFails':True},'FAILED'),({'fatal':True},'FAILED'),
                               ({'recovery':True},'FAILED'),({'recovery':True,'cancelled':True},'CANCELLED')]:
            with self.subTest(options=options):
                output = self.execute(**options)
                self.assertEqual(len(output['notificationCalls']),1,output)
                self.assertEqual(output['notificationCalls'][0]['rootStatus'],status)

    def test_email_errors_do_not_change_success_or_block_native_failure_recovery(self):
        output = self.execute(notificationFails=True)
        self.assertNotIn('error',output,output)
        self.assertEqual(output['root']['STATUS'],'SUCCESS')
        self.assertTrue(output['result']['warnings'])
        output = self.execute(recovery=True,notificationFails=True)
        self.assertNotIn('error',output,output)
        self.assertEqual(output['root']['STATUS'],'FAILED')
        self.assertTrue(output['result']['errors'])


if __name__ == '__main__':
    unittest.main()
