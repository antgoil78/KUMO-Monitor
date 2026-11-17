"""Real tasks exercise both directions of the two-level atomic gate in DEV."""
import argparse,json,sys,time,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1] / 'migrations'))
from migration_support import connect
from snowflake.connector import DictCursor
APP,META='KUMO_DEV.MONITOR_APP','KUMO_DEV.MONITOR_META'


def run():
    wfs={n:str(uuid.uuid4()) for n in ('grandparent','parent','child')}
    jobs={n:str(uuid.uuid4()) for n in wfs}
    marker='KUMO atomic start integration '+str(uuid.uuid4())
    run_ids=[]
    terminal=('SUCCESS','FAILED','SKIPPED','CANCELLED','ERROR')
    with connect() as conn,conn.cursor(DictCursor) as cur:
     def query(sql,p=()):
      cur.execute(sql,p);return cur.fetchall()
     def call(name,*args):
      result=next(iter(query('CALL '+APP+'.'+name+'('+','.join(['%s']*len(args))+')',args)[0].values()))
      result=json.loads(result) if isinstance(result,str) else result
      assert result.get('ok'),result
      return result
     def insert(table,row):
      query('INSERT INTO '+META+'.'+table+' ('+','.join('"'+k+'"' for k in row)+') VALUES ('+','.join(['%s']*len(row))+')',tuple(row.values()))
     def request(name):
      result=call('SP_ORCH_REQUEST_RUN',wfs[name],'WORKFLOW',None,str(uuid.uuid4()),'ATOMIC_START_VALIDATION')
      run_ids.append(result['runId']);return result['runId']
     def record(run):
      rows=query('SELECT EXECUTION_ID,WORKFLOW_ID,JOB_ID,STATUS,STARTED_AT,ENDED_AT,RESULT,ERROR_MESSAGE,CLAIM_TOKEN,TASK_FQN FROM '+META+'.WORKFLOW_EXECUTION_LOG WHERE WORKFLOW_RUN_ID=%s ORDER BY JOB_POSITION NULLS FIRST',(run,))
      for row in rows:
       if isinstance(row.get('RESULT'),str):row['RESULT']=json.loads(row['RESULT'])
      return rows
     def wait(run,predicate,label,timeout=240):
      deadline=time.monotonic()+timeout
      while time.monotonic()<deadline:
       rows=record(run);root=rows[0]
       assert root['STATUS'] not in ('FAILED','ERROR','CANCELLED'),rows
       if predicate(rows):return rows
       time.sleep(2)
      raise AssertionError(dict(timeout=label,rows=rows))
     def blocked(run,direction):
      return wait(run,lambda rows:rows[0]['STATUS']=='QUEUED' and any(b['DIRECTION']==direction for b in (rows[0].get('RESULT') or {}).get('startGate',{}).get('blockers',[])),'waiting '+direction)
     def done(run):
      return wait(run,lambda rows:rows[0]['STATUS']=='SUCCESS' and (rows[0].get('RESULT') or {}).get('dependencyEvaluation',{}).get('checkedAt'),'success and hook')
     env=next(iter(query('CALL '+META+'.SP_GET_ENVIRONMENT()')[0].values()))
     env=json.loads(env) if isinstance(env,str) else env
     assert env['DATABASE']=='KUMO_DEV' and env['META_SCHEMA']=='MONITOR_META' and env['MONITOR_APP_SCHEMA']=='MONITOR_APP',env
     wh=query('SELECT CURRENT_WAREHOUSE() NAME')[0]['NAME']
     try:
      query('BEGIN')
      query('UPDATE '+META+'.DEPENDENCIES SET ALLOW=ALLOW')
      for name in wfs:
       insert('WORKFLOW',dict(WORKFLOW_ID=wfs[name],WORKFLOW_NAME=marker+' '+name,WORKFLOW_GROUP='ATOMIC START VALIDATION',DESCRIPTION=marker,WAREHOUSE_NAME=wh,IS_ENABLED=True,IS_SCHEDULE_ENABLED=False,ALLOW_MANUAL_TRIGGER=True))
       insert('JOB',dict(JOB_ID=jobs[name],JOB_NAME=marker+' '+name,DESCRIPTION=marker,JOB_TYPE='SQL',IS_ENABLED=True,TIMEOUT_SECONDS=60,SQL_COMMAND="SELECT SYSTEM$WAIT(12, 'SECONDS')"))
       parent={'grandparent':None,'parent':'grandparent','child':'parent'}[name]
       # External trigger avoids automatic test cascades; relationships still apply
       # to the universal activity gate for these manually submitted runs.
       insert('DEPENDENCIES',dict(WORKFLOW_ID=wfs[name],JOB_ID=None,PARENT_ID=wfs[parent] if parent else None,TRIGGER_TYPE=None,ALLOW=True,PARENT_MUST_BE_NEWER=False))
       insert('DEPENDENCIES',dict(WORKFLOW_ID=wfs[name],JOB_ID=jobs[name],PARENT_ID=wfs[name],TRIGGER_TYPE='DEP',ALLOW=True,PARENT_MUST_BE_NEWER=False))
      query('COMMIT')
      print(json.dumps(dict(test='temporary isolated DEV fixtures created; no schedules or automatic cascades',marker=marker)),flush=True)
      child=request('child');ancestor=request('grandparent')
      call('SP_ORCH_START_RUN',child)
      held=blocked(child,'UPSTREAM')
      assert held[0]['STARTED_AT'] is None and held[0]['CLAIM_TOKEN'] is None and all(r['STARTED_AT'] is None for r in held),held
      print('PASS: queued grandparent blocked child before any job started',flush=True)
      call('SP_ORCH_START_RUN',ancestor)
      ancestor_done=done(ancestor)[0]
      call('SP_ORCH_START_RUN',child)
      child_done=done(child)[0]
      assert child_done['STARTED_AT']>=ancestor_done['ENDED_AT'],(ancestor_done,child_done)
      print('PASS: grandparent won queue priority; child resumed successfully after it finished',flush=True)
      query('BEGIN');query('UPDATE '+META+'.DEPENDENCIES SET ALLOW=ALLOW')
      query('UPDATE '+META+'.JOB SET SQL_COMMAND=%s,TIMEOUT_SECONDS=180 WHERE JOB_ID=%s',("SELECT SYSTEM$WAIT(90, 'SECONDS')",jobs['child']))
      query('COMMIT')
      child=request('child')
      call('SP_ORCH_START_RUN',child)
      running=wait(child,lambda rows:rows[0]['STATUS']=='RUNNING','child RUNNING')[0]
      ancestor=request('grandparent')
      call('SP_ORCH_START_RUN',ancestor)
      held=blocked(ancestor,'DOWNSTREAM')
      assert held[0]['STARTED_AT'] is None and held[0]['CLAIM_TOKEN'] is None,held
      print('PASS: already-running grandchild blocked a new grandparent start',flush=True)
      child_done=done(child)[0]
      call('SP_ORCH_START_RUN',ancestor)
      ancestor_done=done(ancestor)[0]
      assert ancestor_done['STARTED_AT']>=child_done['ENDED_AT'],(ancestor_done,child_done)
      print('PASS: grandparent resumed after grandchild finished; both directions have no workload overlap',flush=True)
      for run in run_ids:
       call('SP_ORCH_START_RUN',run)
       assert record(run)[0]['STATUS']=='SUCCESS'
      print('PASS: replayed wakeups did not replay any completed run',flush=True)
     finally:
      if conn.is_closed():raise RuntimeError('Connection closed before test cleanup')
      query('ROLLBACK')
      # Do not cancel or delete active work after a failed assertion. Leave its
      # tagged metadata for diagnosis rather than hiding a failed integration run.
      active=[]
      if run_ids:
       active=query('SELECT EXECUTION_ID,STATUS FROM '+META+'.WORKFLOW_EXECUTION_LOG WHERE JOB_ID IS NULL AND EXECUTION_ID IN ('+','.join(['%s']*len(run_ids))+') AND STATUS NOT IN (\'SUCCESS\',\'FAILED\',\'ERROR\',\'SKIPPED\',\'CANCELLED\')',tuple(run_ids))
      if not active:
       for wf in wfs.values():
        prefix='TMP_ORCH_WF_'+wf.replace('-','_').upper()+'_'
        tasks=query("SHOW TASKS LIKE '"+prefix+"%' IN SCHEMA "+APP)
        for task in tasks:
         if task['name'].startswith(prefix):query('DROP TASK IF EXISTS '+APP+'.'+task['name'])
       query('BEGIN');query('UPDATE '+META+'.DEPENDENCIES SET ALLOW=ALLOW')
       for table,key,values in [('WORKFLOW_EXECUTION_LOG','WORKFLOW_ID',wfs.values()),('DEPENDENCIES','WORKFLOW_ID',wfs.values()),('JOB','JOB_ID',jobs.values()),('WORKFLOW','WORKFLOW_ID',wfs.values())]:
        values=tuple(values);query('DELETE FROM '+META+'.'+table+' WHERE '+key+' IN ('+','.join(['%s']*len(values))+')',values)
       query('COMMIT')
       print('Temporary validation fixtures, logs and remaining run tasks removed; WF1-WF10 were not changed.',flush=True)
      else:
       print(json.dumps(dict(active_validation_runs_left_for_diagnosis=active,marker=marker)),flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true',help='Create isolated DEV fixtures, run native tasks, and remove successful test data')
    args=parser.parse_args()
    if args.run: run()
    else: parser.print_help()
