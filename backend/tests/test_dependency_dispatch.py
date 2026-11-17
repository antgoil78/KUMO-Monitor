"""Exercise admission deduplication and enqueue-before-start dispatch ordering."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend/migrations'))
from migration_support import procedure_parts

NODE = r"""
const input=JSON.parse(require('fs').readFileSync(0,'utf8'));
const queries=[],events=[],snapshots=[],log=[];
let pass=0, sequence=0;
const source='00000000-0000-0000-0000-000000000001';
const parentRules=[{parentWorkflowId:'a',parentExecutionId:'a-run',state:'READY'}, {parentWorkflowId:'b',parentExecutionId:'b-run',state:'READY'}];
function report(){
  if(input.kind==='dispatch')return input.report || {ok:true,evaluated:true,children:[{workflowId:'wf2',workflowName:'WF2',eligible:true,rules:[]},{workflowId:'wf3',workflowName:'WF3',eligible:true,rules:[]}]};
  let rules=structuredClone(parentRules);
  if(pass && input.reverse)rules.reverse();
  if(pass && input.changedParent)rules[1].parentExecutionId='new-b';
  return {ok:true,evaluated:true,children:[{workflowId:'wf',eligible:!input.notReady && (!pass || input.changedParent),rules}]};
}
function query(sql,b){
 queries.push({sql,binds:b});
 if(sql.includes('SP_GET_ENVIRONMENT'))return [{SP_GET_ENVIRONMENT:{DATABASE:'KUMO_DEV',META_SCHEMA:'MONITOR_META',MONITOR_APP_SCHEMA:'MONITOR_APP'}}];
 if(sql.includes('SP_ORCH_EVALUATE_DEPENDENCIES'))return [{SP_ORCH_EVALUATE_DEPENDENCIES:report()}];
 if(sql.startsWith('SELECT REQUESTED_BY'))return [{REQUESTED_BY:'original-actor'}];
 if(sql.includes('SP_ORCH_REQUEST_RUN')){
   events.push('admit:'+b[0]);
   if(input.admissionFails && b[0]==='wf2')return [{R:{ok:false,error:'Rejected'}}];
   if(input.admissionNotReady && b[0]==='wf2')return [{R:{ok:true,eligible:false}}];
   return [{R:{ok:true,runId:b[0]+'-run',status:input.alreadyFinished?'SUCCESS':'QUEUED',reused:!!input.alreadyFinished}}];
 }
 if(sql.includes('SP_ORCH_START_RUN')){
   events.push('start:'+b[0]);
   if(input.startFails)return [{R:{ok:false,error:'Wakeup unavailable'}}];
   return [{R:{ok:true,wakeupScheduled:true}}];
 }
 if(sql.startsWith('SELECT SHA2'))return [{ID:require('crypto').createHash('sha256').update(b[0]).digest('hex')}];
 if(sql.startsWith('SELECT WORKFLOW_RUN_ID'))return log.filter(r=>r.REQUEST_ID===b[1]);
 if(sql.startsWith('SELECT * FROM') && sql.includes('.WORKFLOW WHERE'))return [{WORKFLOW_ID:'wf',IS_ENABLED:true,ALLOW_MANUAL_TRIGGER:false,WAREHOUSE_NAME:'WH'}];
 if(sql.startsWith('SELECT WORKFLOW_ID, PARENT_ID'))return input.cycle?[{WORKFLOW_ID:'wf',PARENT_ID:'a'},{WORKFLOW_ID:'a',PARENT_ID:'wf'}]:[{WORKFLOW_ID:'wf',PARENT_ID:'a'},{WORKFLOW_ID:'wf',PARENT_ID:'b'}];
 if(sql.startsWith('SELECT * FROM') && sql.includes('.DEPENDENCIES'))return [
   {JOB_ID:null,PARENT_ID:'a',ALLOW:true},{JOB_ID:null,PARENT_ID:input.duplicateParent?'a':'b',ALLOW:true},
   {JOB_ID:'job',PARENT_ID:'wf',ALLOW:true}];
 if(sql.startsWith('SELECT * FROM') && sql.includes('.JOB WHERE'))return [{JOB_ID:'job',JOB_NAME:'Test',IS_ENABLED:true,JOB_TYPE:'SQL',SQL_COMMAND:'SELECT 1',TIMEOUT_SECONDS:60}];
 if(sql.includes('UUID_STRING'))return [{ID:'id-'+(++sequence)}];
 if(sql.includes('SP_RESOLVE_WORKFLOW_COMMAND'))return [{SP_RESOLVE_WORKFLOW_COMMAND:{ok:true,execSql:'SELECT 1'}}];
 if(sql.startsWith('INSERT INTO')){
   if(b.length===10){
     const snapshot=JSON.parse(b[9]);snapshots.push(snapshot);
     log.push({WORKFLOW_RUN_ID:b[0],RUN_MODE:b[4],REQUESTED_JOB_ID:b[5],TRIGGER_SOURCE:b[6],REQUEST_ID:b[3],STATUS:'QUEUED',TASK_FQN:b[8],CONFIG_SNAPSHOT:snapshot});
   }
   return [];
 }
 if(['BEGIN','COMMIT','ROLLBACK'].includes(sql)||sql.startsWith('UPDATE'))return [];
 throw Error('Unexpected SQL: '+sql);
}
const snowflake={createStatement({sqlText,binds=[]}){let values=[],keys=[];
 return {execute(){values=query(sqlText,binds);keys=[...new Set(values.flatMap(r=>Object.keys(r)))];let i=-1;
  return {next(){return ++i<values.length},getColumnValue(n){return values[i][keys[n-1]]}};},
 getColumnCount(){return keys.length},getColumnName(n){return keys[n-1]},getNumRowsAffected(){return 1}};
}};
const results=[];
if(input.kind==='dispatch')results.push(new Function('snowflake','P_EXECUTION_ID',input.source)(snowflake,source));
else {
 const admit=new Function('snowflake','P_WORKFLOW_ID','P_RUN_MODE','P_JOB_ID','P_REQUEST_ID','P_REQUESTED_BY','P_FULL_REFRESH','P_SOURCE_EXECUTION_ID',input.source);
 for(pass=0;pass<(input.repeat?2:1);pass++)results.push(admit(snowflake,'wf','DEPENDENCY',null,null,'original-actor',false,source));
}
process.stdout.write(JSON.stringify({results,log,snapshots,queries,events}));
"""


class DispatchTests(unittest.TestCase):
    def run_procedure(self, kind='admission', **options):
        name = 'SP_ORCH_REQUEST_RUN' if kind == 'admission' else 'SP_ORCH_DISPATCH_DEPENDENCIES'
        source = procedure_parts((ROOT / 'snowflake/ddl/KUMO_DEV/MONITOR_APP/procedures' / (name + '.sql')).read_text())[1]
        output = subprocess.run(['node','-e',NODE],input=json.dumps(dict(source=source,kind=kind,**options)),text=True,capture_output=True,check=True)
        return json.loads(output.stdout)

    def test_two_parent_admission_keeps_both_rules_and_uses_dependency_trigger(self):
        output = self.run_procedure()
        self.assertTrue(output['results'][0]['ok'],output)
        self.assertEqual(len(output['log']),1)
        self.assertEqual(output['log'][0]['TRIGGER_SOURCE'],'DEPENDENCY')
        self.assertTrue(output['log'][0]['REQUEST_ID'].startswith('DEP:'))
        self.assertEqual(len(output['snapshots'][0]['workflowRules']),2)
        self.assertEqual(output['snapshots'][0]['dependency']['parents'],[['a','a-run'],['b','b-run']])
        self.assertEqual(output['log'][0]['RUN_MODE'],'WORKFLOW')

    def test_repeated_events_reuse_same_run_even_while_child_is_busy(self):
        output = self.run_procedure(repeat=True,reverse=True)
        self.assertEqual(len(output['log']),1)
        self.assertTrue(output['results'][1]['reused'])
        self.assertEqual(output['results'][0]['runId'],output['results'][1]['runId'])

    def test_changed_parent_execution_produces_a_new_request(self):
        output = self.run_procedure(repeat=True,changedParent=True)
        self.assertEqual(len(output['log']),2)
        self.assertNotEqual(output['log'][0]['REQUEST_ID'],output['log'][1]['REQUEST_ID'])

    def test_ineligible_recheck_or_invalid_graph_does_not_insert(self):
        for options in ({'notReady':True},{'cycle':True},{'duplicateParent':True}):
            with self.subTest(options=options):
                output=self.run_procedure(**options)
                self.assertEqual(output['log'],[])
                self.assertFalse(output['results'][0].get('eligible',output['results'][0]['ok']))

    def test_all_siblings_are_admitted_before_any_task_wakeup(self):
        output=self.run_procedure(kind='dispatch')
        self.assertEqual(output['events'],['admit:wf2','admit:wf3','start:wf2-run','start:wf3-run'])
        self.assertEqual(output['results'][0]['errors'],[])

    def test_rechecks_and_failures_do_not_launch_wrong_or_duplicate_work(self):
        for options, expected in [({'alreadyFinished':True},['admit:wf2','admit:wf3']),
                                 ({'admissionNotReady':True},['admit:wf2','admit:wf3','start:wf3-run']),
                                 ({'admissionFails':True},['admit:wf2','admit:wf3','start:wf3-run'])]:
            with self.subTest(options=options):
                self.assertEqual(self.run_procedure(kind='dispatch',**options)['events'],expected)
        output=self.run_procedure(kind='dispatch',startFails=True)
        self.assertEqual(len(output['results'][0]['dispatches']),2)
        self.assertEqual(len(output['results'][0]['errors']),2)

    def test_waiting_and_partial_execution_reports_never_request_children(self):
        for report in ({'ok':True,'evaluated':False,'children':[]},
                       {'ok':True,'evaluated':True,'children':[{'workflowId':'child','eligible':False,'rules':[{'state':'WAITING'}]}]}):
            with self.subTest(report=report):
                output=self.run_procedure(kind='dispatch',report=report)
                self.assertEqual(output['events'],[])


if __name__=='__main__':
    unittest.main()
