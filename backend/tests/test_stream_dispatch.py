"""Exercise actual dispatcher/finish JavaScript with durable event state."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'backend/migrations'))
from migration_support import procedure_parts

NODE = r'''
const x=JSON.parse(require('fs').readFileSync(0,'utf8'));
const queries=[],launches=[],finishes=[],q=x.events||[];
const roots=x.roots||{}, counts={}, dead=[];
function query(sql,b=[]) {
 queries.push({sql,binds:b});
 if(sql.includes('SP_GET_ENVIRONMENT'))return [{ENV:{DATABASE:'KUMO_DEV',META_SCHEMA:'MONITOR_META',MONITOR_APP_SCHEMA:'MONITOR_APP'}}];
 if(sql.includes('CURRENT_TASK_NAME'))return [{NAME:x.wrongTask?'OTHER':'KUMO_DEV.MONITOR_APP.TASK_ORCH_DISPATCH'}];
 if(sql.startsWith('INSERT')&&sql.includes('ORCH_DISPATCH_EVENTS_STREAM')){
  for(const e of q)if(e.STATE==='NEW')e.STATE=e.KIND==='WAKE'?'DONE':'READY';return [];
 }
 if(sql.startsWith('UPDATE')&&sql.includes("WHERE STATE = 'READY' AND ATTEMPTS >= 3")){for(const e of q)if(e.STATE==='READY'&&e.ATTEMPTS>=3)e.STATE='ERROR';return [];}
 if(sql.startsWith('SELECT COUNT(*) AS N'))return [{N:q.filter(e=>e.STATE==='READY'&&e.ATTEMPTS<3).length}];
 if(sql.startsWith('SELECT EVENT_ID'))return q.filter(e=>e.STATE==='READY'&&e.ATTEMPTS<3).slice(0,100).map(e=>({...e}));
 if(sql.startsWith('UPDATE')&&sql.includes('ATTEMPTS = ATTEMPTS + 1')){q.find(e=>e.EVENT_ID===b[0]).ATTEMPTS++;return [];}
 if(sql.startsWith('SELECT WORKFLOW_ID'))return roots[b[0]]?[roots[b[0]]]:[];
 if(sql.includes('.SP_ORCH_LAUNCH_RUN')){
  launches.push(b[0]);counts[b[0]]=(counts[b[0]]||0)+1;
  if(x.launchError)throw Error('Launch unavailable');
  return [{R:{ok:true,wakeupScheduled:!x.blocked}}];
 }
 if(sql.includes('.SP_ORCH_FINISH_RUN')){finishes.push(b[0]);return [{R:{ok:true}}];}
 if(sql.includes('.SP_ORCH_DISPATCH_DEPENDENCIES'))return [{R:{ok:true,errors:[]}}];
 if(sql.startsWith('UPDATE')&&sql.includes("IFF(ATTEMPTS >= 3")){
  const e=q.find(e=>e.EVENT_ID===b[1]);e.STATE=e.ATTEMPTS>=3?'ERROR':'READY';e.LAST_ERROR=b[0];return [];
 }
 if(sql.startsWith('UPDATE')&&sql.includes('SET STATE = ?')){q.find(e=>e.EVENT_ID===b[1]).STATE=b[0];return [];}
 if(sql.startsWith('INSERT')&&sql.includes("VALUES ('WAKE')")){
  if(q.some(e=>e.STATE==='READY'&&e.ATTEMPTS<3)&&!q.some(e=>e.STATE==='NEW'))q.push({EVENT_ID:'wake-'+q.length,KIND:'WAKE',STATE:'NEW',ATTEMPTS:0});return [];
 }
 if(sql.startsWith('DELETE'))return [];
 throw Error('Unhandled SQL: '+sql);
}
const sf={createStatement({sqlText,binds=[]}){let rows=[],keys=[];return {
 execute(){rows=query(sqlText,binds);keys=[...new Set(rows.flatMap(r=>Object.keys(r)))];let i=-1;return {next(){return ++i<rows.length},getColumnValue(n){return rows[i][keys[n-1]]}}},
 getColumnCount(){return keys.length},getColumnName(n){return keys[n-1]}};}};
let results=[],error;
let clock=0;
const testDate={now(){const value=clock;clock+=(x.clockStep||0);return value;}};
try{for(let i=0;i<(x.passes||1);i++)results.push(new Function('snowflake','Date',x.source)(sf,testDate));}catch(e){error=e.message;}
process.stdout.write(JSON.stringify({results,error,q,launches,finishes,queries}));
'''


def event(id, run=None, state='NEW', kind='LAUNCH', attempts=0):
    return dict(EVENT_ID=id, RUN_ID=run or id, KIND=kind, STATE=state, ATTEMPTS=attempts)


class DispatcherTests(unittest.TestCase):
    def dispatch(self, **values):
        source=procedure_parts((ROOT/'snowflake/ddl/KUMO_DEV/MONITOR_APP/procedures/SP_ORCH_DISPATCH.sql').read_text())[1]
        r=subprocess.run(['node','-e',NODE],input=json.dumps(dict(source=source,**values)),text=True,capture_output=True,check=True)
        return json.loads(r.stdout)

    def test_committed_work_is_dispatched_without_any_browser_or_python_worker(self):
        x=self.dispatch(events=[event('run')],roots={'run':dict(WORKFLOW_ID='wf',STATUS='QUEUED')})
        self.assertNotIn('error',x);self.assertEqual(x['launches'],['run']);self.assertEqual(x['q'][0]['STATE'],'DONE')

    def test_consumed_stream_does_not_lose_ready_delivery_on_native_retry(self):
        x=self.dispatch(events=[event('run',state='READY')],roots={'run':dict(WORKFLOW_ID='wf',STATUS='QUEUED')})
        self.assertEqual(x['launches'],['run'])

    def test_empty_stream_is_consumed_without_launch_or_continuation(self):
        x=self.dispatch(events=[])
        self.assertEqual(x['launches'],[]);self.assertEqual(x['q'],[])
        self.assertIn('ORCH_DISPATCH_EVENTS_STREAM',x['queries'][2]['sql'])
        self.assertFalse(any(r['sql'].startswith(('UPDATE KUMO_DEV.MONITOR_META.ORCH_DISPATCH_EVENTS ', 'DELETE FROM KUMO_DEV.MONITOR_META.ORCH_DISPATCH_EVENTS ', 'INSERT INTO KUMO_DEV.MONITOR_META.ORCH_DISPATCH_EVENTS ')) for r in x['queries']))

    def test_failures_stop_after_three_delivery_attempts(self):
        x=self.dispatch(events=[event('run')],roots={'run':dict(WORKFLOW_ID='wf',STATUS='QUEUED')},launchError=True,passes=6)
        self.assertEqual(x['launches'],['run']*3);self.assertEqual(x['q'][0]['STATE'],'ERROR')
        self.assertFalse(any(e['STATE']=='NEW' for e in x['q']))

    def test_blocked_delivery_never_self_requeues(self):
        x=self.dispatch(events=[event('run')],roots={'run':dict(WORKFLOW_ID='wf',STATUS='QUEUED')},blocked=True,passes=4)
        self.assertEqual(x['launches'],['run']);self.assertEqual(x['q'][0]['STATE'],'WAITING')

    def test_interruption_on_last_attempt_is_quarantined_without_another_launch(self):
        x=self.dispatch(events=[event('run',state='READY',attempts=3)],
                        roots={'run':dict(WORKFLOW_ID='wf',STATUS='QUEUED')})
        self.assertEqual(x['launches'],[])
        self.assertEqual(x['q'][0]['STATE'],'ERROR')
        self.assertFalse(any(e['STATE']=='NEW' for e in x['q']))

    def test_continuation_with_no_remaining_work_does_not_create_another_signal(self):
        x=self.dispatch(events=[event('wake',kind='WAKE')])
        self.assertEqual(x['launches'],[])
        self.assertEqual(len(x['q']),1)
        self.assertFalse(any(e['STATE']=='NEW' for e in x['q']))

    def test_done_or_running_execution_is_not_launched_again(self):
        for state in ['RUNNING','SUCCESS','FAILED','CANCELLED']:
            with self.subTest(state=state):
                x=self.dispatch(events=[event('run')],roots={'run':dict(WORKFLOW_ID='wf',STATUS=state)})
                self.assertEqual(x['launches'],[]);self.assertEqual(x['q'][0]['STATE'],'DONE')

    def test_only_one_launch_per_workflow_is_submitted_in_a_batch(self):
        x=self.dispatch(events=[event('a'),event('b')],roots={r:dict(WORKFLOW_ID='wf',STATUS='QUEUED') for r in ['a','b']})
        self.assertEqual(x['launches'],['a']);self.assertEqual(x['q'][1]['STATE'],'WAITING')

    def test_more_than_batch_limit_has_a_durable_continuation(self):
        ids=[str(i) for i in range(105)]
        x=self.dispatch(events=[event(i) for i in ids],roots={i:dict(WORKFLOW_ID=i,STATUS='QUEUED') for i in ids},passes=2)
        self.assertEqual(len(x['launches']),105)
        self.assertTrue(all(e['STATE']=='DONE' for e in x['q']))

    def test_slow_batch_yields_before_timeout_and_preserves_remaining_delivery(self):
        roots={i:dict(WORKFLOW_ID=i,STATUS='QUEUED') for i in ['a','b']}
        x=self.dispatch(events=[event('a'),event('b')],roots=roots,clockStep=70000)
        self.assertEqual(x['launches'],['a'])
        self.assertEqual(x['q'][1]['STATE'],'READY')
        self.assertEqual(x['q'][1]['ATTEMPTS'],0)
        self.assertTrue(any(e['KIND']=='WAKE' and e['STATE']=='NEW' for e in x['q']))
        x=self.dispatch(events=[event('a'),event('b')],roots=roots,clockStep=70000,passes=2)
        self.assertEqual(x['launches'],['a','b'])

    def test_invocation_outside_serialized_task_is_rejected(self):
        x=self.dispatch(events=[event('run')],wrongTask=True)
        self.assertIn('non-overlapping',x['error']);self.assertFalse(x['launches'])
        self.assertEqual(x['q'][0]['STATE'],'NEW')


if __name__=='__main__': unittest.main()
