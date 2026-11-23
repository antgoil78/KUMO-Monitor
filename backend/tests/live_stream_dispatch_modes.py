"""DEV-only SQL fixtures for stream dispatch scopes, frozen snapshots, parallelism and schedules."""
import argparse
import json
from pathlib import Path
import sys
import time
import uuid
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'backend/migrations'))
from migration_support import connect
from snowflake.connector import DictCursor
APP,META='KUMO_DEV.MONITOR_APP','KUMO_DEV.MONITOR_META'


def run(report_path=None, scenario="all"):
    marker='STREAM_MODES_'+uuid.uuid4().hex[:10];wfs=[];jobs=[];schedulers=[];checks=[];passed=False
    with connect() as c,c.cursor(DictCursor) as cur:
        def q(sql,p=()):cur.execute(sql,p);return cur.fetchall()
        def call(name,*args):
            x=next(iter(q('CALL '+APP+'.'+name+'('+','.join(['%s']*len(args))+')',args)[0].values()))
            x=json.loads(x) if isinstance(x,str) else x
            assert x.get('ok'),x
            return x
        def wait(fn,label,seconds=300):
            end=time.monotonic()+seconds
            while time.monotonic()<end:
                result=fn()
                if result:return result
                time.sleep(2)
            raise AssertionError('Timeout: '+label)
        def fixture(label,commands,mode='SEQUENTIAL',timeouts=None):
            wf=str(uuid.uuid4());wfs.append(wf)
            q('INSERT INTO '+META+'.WORKFLOW(WORKFLOW_ID,WORKFLOW_NAME,DESCRIPTION,IS_ENABLED,ALLOW_MANUAL_TRIGGER,WAREHOUSE_NAME,JOB_EXECUTION_MODE) VALUES(%s,%s,%s,TRUE,TRUE,\'KUMO_ELT_GEN_1\',%s)',(wf,marker+' '+label,marker,mode))
            q('INSERT INTO '+META+'.DEPENDENCIES(WORKFLOW_ID,ALLOW) VALUES(%s,TRUE)',(wf,))
            parent=wf;ids=[]
            for i,sql in enumerate(commands):
                j=str(uuid.uuid4());jobs.append(j);ids.append(j)
                q('INSERT INTO '+META+'.JOB(JOB_ID,JOB_NAME,DESCRIPTION,JOB_TYPE,SQL_COMMAND,IS_ENABLED,TIMEOUT_SECONDS) VALUES(%s,%s,%s,\'SQL\',%s,TRUE,%s)',(j,marker+' '+label+' '+str(i),marker,sql,(timeouts or [60]*len(commands))[i]))
                q('INSERT INTO '+META+'.DEPENDENCIES(WORKFLOW_ID,JOB_ID,PARENT_ID,ALLOW,SUCCESS) VALUES(%s,%s,%s,TRUE,TRUE)',(wf,j,parent));parent=j
            return wf,ids
        def rows(run):return q('SELECT JOB_ID,STATUS,STARTED_AT,ENDED_AT,CONFIG_SNAPSHOT FROM '+META+'.WORKFLOW_EXECUTION_LOG WHERE WORKFLOW_RUN_ID=%s ORDER BY JOB_POSITION NULLS FIRST',(run,))
        def done(run,status='SUCCESS'):
            return wait(lambda:next((r for r in [rows(run)] if r and r[0]['STATUS']==status),None),'run '+status)
        def request(wf,mode='WORKFLOW',job=None):return call('SP_ORCH_REQUEST_RUN',wf,mode,job,str(uuid.uuid4()),marker)['runId']
        def check(label):checks.append(label);print('PASS: '+label,flush=True)
        q('USE ROLE KUMO_ADMIN');q("ALTER SESSION SET QUERY_TAG='KUMO_DEV_STREAM_MODES_TEST',TIMEZONE='UTC'")
        env=next(iter(q('CALL '+META+'.SP_GET_ENVIRONMENT()')[0].values()));env=json.loads(env) if isinstance(env,str) else env
        assert env['DATABASE']=='KUMO_DEV'
        try:
            if scenario == 'all':
                wf,ids=fixture('scopes',['SELECT 1']*3)
                only=request(wf,'JOB_ONLY',ids[1]);onward=request(wf,'FROM_JOB',ids[1])
                a=done(only);b=done(onward)
                assert [r['STATUS'] for r in a[1:]]==['SKIPPED','SUCCESS','SKIPPED'],a
                assert [r['STATUS'] for r in b[1:]]==['SKIPPED','SUCCESS','SUCCESS'],b
                check('JOB_ONLY and FROM_JOB retain their frozen job scope through stream dispatch')

                wf,ids=fixture('frozen',["SELECT SYSTEM$WAIT(8, 'SECONDS')",'SELECT 1'])
                frozen=request(wf)
                q('UPDATE '+META+'.JOB SET SQL_COMMAND=\'SELECT 1/0\',IS_ENABLED=FALSE WHERE JOB_ID=%s',(ids[1],))
                a=done(frozen)
                assert all(r['STATUS']=='SUCCESS' for r in a),a
                snap=a[2]['CONFIG_SNAPSHOT'];snap=json.loads(snap) if isinstance(snap,str) else snap
                assert snap['resolved']['execSql']=='SELECT 1',snap
                check('catalog edits after admission do not change queued execution commands or selection')

            wf,ids=fixture('parallel',["SELECT SYSTEM$WAIT(30, 'SECONDS')","SELECT SYSTEM$WAIT(30, 'SECONDS')"],mode='PARALLEL',timeouts=[90,15])
            a=done(request(wf),'FAILED')
            assert [r['STATUS'] for r in a[1:]]==['SUCCESS','FAILED'],a
            assert a[2]['STARTED_AT'] < a[1]['ENDED_AT'] and a[2]['ENDED_AT'] < a[1]['ENDED_AT'],a
            check('parallel jobs overlap and a short job timeout does not cancel its longer sibling')

            wf,ids=fixture('schedule',["SELECT SYSTEM$WAIT(45, 'SECONDS')"])
            scheduler=APP+'.TASK_ORCH_WF_'+wf.replace('-','_').upper()+'_SCHEDULER';schedulers.append(scheduler)
            q('UPDATE '+META+'.WORKFLOW SET IS_SCHEDULE_ENABLED=TRUE WHERE WORKFLOW_ID=%s',(wf,))
            # No schedule is created/resumed: explicitly execute two isolated native
            # occurrences to test the real wrapper and native context validation.
            q('CREATE TASK '+scheduler+" WAREHOUSE=KUMO_ELT_GEN_1 COMMENT='KUMO stream dispatch schedule test' AS CALL "+APP+".SP_ORCH_RUN_SCHEDULED('"+wf+"')")
            q('EXECUTE TASK '+scheduler)
            def active():
                return next((r for r in q('SELECT EXECUTION_ID,STATUS FROM '+META+'.WORKFLOW_EXECUTION_LOG WHERE WORKFLOW_ID=%s AND JOB_ID IS NULL',(wf,)) if r['STATUS']=='RUNNING'),None)
            run_id=wait(active,'scheduled request starts')['EXECUTION_ID']
            q('EXECUTE TASK '+scheduler)
            # Native scheduler is enqueue-only and its second occurrence should
            # be skipped by admission while the first workload is still running.
            wait(lambda:len(q("SELECT STATE FROM TABLE(KUMO_DEV.INFORMATION_SCHEMA.TASK_HISTORY(TASK_NAME=>%s,SCHEDULED_TIME_RANGE_START=>DATEADD('hour',-1,CURRENT_TIMESTAMP()),RESULT_LIMIT=>50)) WHERE DATABASE_NAME='KUMO_DEV' AND SCHEMA_NAME='MONITOR_APP' AND STATE='SUCCEEDED'",(scheduler.split('.')[-1],)))>=2,'two schedule invocations finish')
            a=done(run_id)
            roots=q('SELECT EXECUTION_ID,TRIGGER_SOURCE,REQUEST_ID,TASK_FQN FROM '+META+'.WORKFLOW_EXECUTION_LOG WHERE WORKFLOW_ID=%s AND JOB_ID IS NULL',(wf,))
            assert len(roots)==1 and roots[0]['TRIGGER_SOURCE']=='SCHEDULED' and roots[0]['REQUEST_ID'].startswith('SCHEDULED:'),roots
            assert roots[0]['TASK_FQN']!=scheduler,roots
            check('native scheduler only admits, execution uses a separate task, and overlap does not queue another occurrence')
            passed=True
        finally:
            if passed:
                for wf in wfs:
                    wait(lambda wf=wf:not q("SHOW TASKS LIKE 'TMP_ORCH_WF_"+wf.replace('-','_').upper()+"_%' IN SCHEMA "+APP),'finalizer cleanup')
                for scheduler in schedulers:q('DROP TASK IF EXISTS '+scheduler)
                for wf in wfs:
                    q('DELETE FROM '+META+'.ORCH_DISPATCH_DELIVERIES WHERE RUN_ID IN (SELECT EXECUTION_ID FROM '+META+'.WORKFLOW_EXECUTION_LOG WHERE WORKFLOW_ID=%s)',(wf,))
                    q('DELETE FROM '+META+'.ORCH_DISPATCH_EVENTS WHERE RUN_ID IN (SELECT EXECUTION_ID FROM '+META+'.WORKFLOW_EXECUTION_LOG WHERE WORKFLOW_ID=%s)',(wf,))
                    q('DELETE FROM '+META+'.WORKFLOW_EXECUTION_LOG WHERE WORKFLOW_ID=%s',(wf,))
                    q('DELETE FROM '+META+'.DEPENDENCIES WHERE WORKFLOW_ID=%s',(wf,))
                    q('DELETE FROM '+META+'.WORKFLOW WHERE WORKFLOW_ID=%s',(wf,))
                for job in jobs:q('DELETE FROM '+META+'.JOB WHERE JOB_ID=%s',(job,))
            else:print('Retained isolated fixtures for diagnosis: '+marker,flush=True)
    result=dict(environment='DEV',checks=checks,business_workflows_executed=False,tst_changed=False)
    if report_path:Path(report_path).write_text(json.dumps(result,indent=2)+'\n')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',action='store_true');p.add_argument('--report');p.add_argument('--scenario',choices=('all','parallel_schedule'),default='all');a=p.parse_args()
    if a.run:print(json.dumps(run(a.report,a.scenario),indent=2))
    else:p.print_help()
