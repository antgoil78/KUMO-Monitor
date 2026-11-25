"""Run native completion recovery without Snowflake or business side effects."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'backend/migrations'))
from migration_support import procedure_parts

NODE=r'''
const x=JSON.parse(require('fs').readFileSync(0,'utf8'));
const id='00000000-0000-0000-0000-000000000002',wf='00000000-0000-0000-0000-000000000001';
const task='KUMO_DEV.MONITOR_APP.TMP_ORCH_WF_'+wf.replaceAll('-','_')+'_'+id.replaceAll('-','_');
const root={EXECUTION_ID:id,WORKFLOW_ID:wf,STATUS:x.status||'RUNNING',TASK_FQN:task,TASK_RUN_GROUP_ID:x.oldGroup?'new':'group'};
const queries=[],events=[];
function query(sql,b=[]){
 queries.push({sql,binds:b});
 if(sql.includes('SP_GET_ENVIRONMENT'))return [{ENV:{DATABASE:'KUMO_DEV',META_SCHEMA:'MONITOR_META',MONITOR_APP_SCHEMA:'MONITOR_APP'}}];
 if(sql.startsWith('SELECT * FROM'))return [root];
 if(sql.startsWith('SELECT STATE,'))return x.noHistory?[]:[{STATE:x.native||'FAILED',ERROR_CODE:'test',ERROR_MESSAGE:'Native task stopped'}];
 if(sql.startsWith('UPDATE')&&sql.includes('SET STATUS = IFF')){root.STATUS=b[0];return [];}
 if(sql.startsWith('INSERT INTO')&&sql.includes('ORCH_DISPATCH_EVENTS')){events.push('backfill');return [];}
 if(sql.includes('SP_ORCH_DISPATCH_DEPENDENCIES')){events.push('dependencies');return [{R:{ok:true,errors:[]}}];}
 if(sql.includes('SP_ORCH_NOTIFY')){events.push('notify');return [{R:{ok:true}}];}
 if(sql.startsWith('UPDATE')||sql.startsWith('DROP TASK'))return [];
 throw Error('Unexpected SQL: '+sql);
}
const sf={createStatement({sqlText,binds=[]}){let rows=[],keys=[];return {
 execute(){rows=query(sqlText,binds);keys=[...new Set(rows.flatMap(r=>Object.keys(r)))];let i=-1;return {next(){return ++i<rows.length},getColumnValue(n){return rows[i][keys[n-1]]}}},
 getColumnCount(){return keys.length},getColumnName(n){return keys[n-1]}};}};
let result,error;try{result=new Function('snowflake','P_RUN_ID','P_GROUP_ID',x.source)(sf,id,'group');}catch(e){error=e.message;}
process.stdout.write(JSON.stringify({root,result,error,queries,events}));
'''


class FinalizationTests(unittest.TestCase):
    def finish(self,**options):
        source=procedure_parts((ROOT/'snowflake/ddl/KUMO_DEV/MONITOR_APP/procedures/SP_ORCH_FINISH_RUN.sql').read_text())[1]
        r=subprocess.run(['node','-e',NODE],input=json.dumps(dict(source=source,**options)),text=True,capture_output=True,check=True)
        return json.loads(r.stdout)

    def test_native_failure_and_cancellation_recover_status_before_dependencies(self):
        for native,status in [('FAILED','FAILED'),('CANCELLED','CANCELLED')]:
            x=self.finish(native=native)
            self.assertNotIn('error',x);self.assertEqual(x['root']['STATUS'],status)
            self.assertEqual(x['events'],['backfill','dependencies','notify'])
            self.assertFalse(any(q['sql'].startswith('EXECUTE TASK') for q in x['queries']))

    def test_older_completion_cannot_end_or_drop_a_newer_invocation(self):
        x=self.finish(oldGroup=True)
        self.assertTrue(x['result']['stale']);self.assertEqual(x['root']['STATUS'],'RUNNING')
        self.assertFalse(any(q['sql'].startswith(('UPDATE','DROP')) for q in x['queries']))

    def test_task_history_lag_is_retriable_and_never_fabricates_failure(self):
        x=self.finish(noHistory=True)
        self.assertIn('not yet visible',x['error']);self.assertEqual(x['root']['STATUS'],'RUNNING')
        self.assertFalse(x['events'])

    def test_blocked_successful_native_invocation_waits_without_releasing_other_queues(self):
        x=self.finish(status='QUEUED',native='SUCCEEDED')
        self.assertTrue(x['result']['waiting']);self.assertEqual(x['root']['STATUS'],'QUEUED')
        self.assertFalse(x['events'])
        self.assertFalse(any("STATE = 'READY'" in q['sql'] for q in x['queries']))

    def test_terminal_result_is_preserved_and_finalizer_is_dropped_before_root(self):
        x=self.finish(status='SUCCESS',native='SUCCEEDED')
        self.assertEqual(x['root']['STATUS'],'SUCCESS')
        drops=[q['sql'] for q in x['queries'] if q['sql'].startswith('DROP TASK')]
        self.assertTrue(drops[0].endswith('_FINALIZER'));self.assertFalse(drops[1].endswith('_FINALIZER'))
        self.assertTrue(any("STATE = 'READY'" in q['sql'] for q in x['queries']))

    def test_native_success_without_a_terminal_log_fails_closed(self):
        x=self.finish(native='SUCCEEDED')
        self.assertEqual(x['root']['STATUS'],'FAILED')

if __name__=='__main__':unittest.main()
