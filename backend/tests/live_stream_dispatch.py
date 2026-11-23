"""DEV-only opt-in stream dispatch integration tests with disposable SQL jobs.

Never runs business workflows, sends email, changes services or touches TST.
"""
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


def run(report_path=None, scenario='extended'):
    marker='STREAM_TEST_'+uuid.uuid4().hex[:12]
    workflows=[];jobs=[];runs=[];checks=[];synthetic=[];passed=False
    with connect() as conn,conn.cursor(DictCursor) as cur:
        def query(sql,params=()):
            cur.execute(sql,params);return cur.fetchall()
        def call(name,*args):
            value=next(iter(query('CALL '+APP+'.'+name+'('+','.join(['%s']*len(args))+')',args)[0].values()))
            value=json.loads(value) if isinstance(value,str) else value
            if not value.get('ok'):raise AssertionError(value)
            return value
        def wait(check,description,timeout=240):
            end=time.monotonic()+timeout
            while time.monotonic()<end:
                result=check()
                if result:return result
                time.sleep(2)
            errors=query('SELECT KIND,RUN_ID,STATE,ATTEMPTS,LAST_ERROR FROM '+META+".ORCH_DISPATCH_DELIVERIES WHERE STATE='ERROR' AND RUN_ID IN (SELECT EXECUTION_ID FROM "+META+'.WORKFLOW_EXECUTION_LOG WHERE REQUESTED_BY=%s)',(marker,))
            raise AssertionError(dict(timeout=description,delivery_errors=errors))
        def record(run_id):
            return query('SELECT EXECUTION_ID,JOB_ID,STATUS,STARTED_AT,ENDED_AT,TASK_FQN,TASK_RUN_GROUP_ID,ERROR_MESSAGE FROM '+META+'.WORKFLOW_EXECUTION_LOG WHERE WORKFLOW_RUN_ID=%s ORDER BY JOB_POSITION NULLS FIRST',(run_id,))
        def fixture(label,sql='SELECT 1',parent=None,dep=False,parallel=False):
            wf,j=str(uuid.uuid4()),str(uuid.uuid4());workflows.append(wf);jobs.append(j)
            query('INSERT INTO '+META+'.WORKFLOW (WORKFLOW_ID,WORKFLOW_NAME,DESCRIPTION,IS_ENABLED,ALLOW_MANUAL_TRIGGER,WAREHOUSE_NAME,JOB_EXECUTION_MODE) VALUES(%s,%s,%s,TRUE,TRUE,\'KUMO_ELT_GEN_1\',%s)',(wf,marker+' '+label,marker,'PARALLEL' if parallel else 'SEQUENTIAL'))
            query('INSERT INTO '+META+'.JOB (JOB_ID,JOB_NAME,DESCRIPTION,JOB_TYPE,SQL_COMMAND,IS_ENABLED,TIMEOUT_SECONDS) VALUES(%s,%s,%s,\'SQL\',%s,TRUE,60)',(j,marker+' '+label,marker,sql))
            for parent_id in (parent if isinstance(parent,list) else [parent]):
                query('INSERT INTO '+META+'.DEPENDENCIES (WORKFLOW_ID,JOB_ID,PARENT_ID,TRIGGER_TYPE,ALLOW,SUCCESS,PARENT_MUST_BE_NEWER) VALUES(%s,NULL,%s,%s,TRUE,TRUE,FALSE)',(wf,parent_id,'DEP' if dep else None))
            query('INSERT INTO '+META+'.DEPENDENCIES (WORKFLOW_ID,JOB_ID,PARENT_ID,ALLOW,SUCCESS) VALUES(%s,%s,%s,TRUE,TRUE)',(wf,j,wf))
            return wf,j
        def request(wf,mode='WORKFLOW',job=None,request_id=None):
            r=call('SP_ORCH_REQUEST_RUN',wf,mode,job,request_id or str(uuid.uuid4()),marker)
            runs.append(r['runId']);return r['runId']
        def done(run_id,status='SUCCESS',timeout=240):
            def check():
                rows=record(run_id)
                if rows and rows[0]['STATUS'] in ('FAILED','CANCELLED') and rows[0]['STATUS']!=status:raise AssertionError(rows)
                return rows if rows and rows[0]['STATUS']==status else None
            return wait(check,run_id+' '+status,timeout)
        def pass_check(name):
            checks.append(name);print('PASS: '+name,flush=True)
        query('USE ROLE KUMO_ADMIN')
        query("ALTER SESSION SET QUERY_TAG='KUMO_DEV_STREAM_DISPATCH_TEST',TIMEZONE='UTC'")
        env=next(iter(query('CALL '+META+'.SP_GET_ENVIRONMENT()')[0].values()))
        env=json.loads(env) if isinstance(env,str) else env
        assert env['DATABASE']=='KUMO_DEV',env
        try:
            wf,job=fixture('standalone')
            run_id=request(wf)
            done(run_id)
            pass_check('committed run launches through the stream with no browser or SP_ORCH_START_RUN call')
            wait(lambda:not query("SHOW TASKS LIKE 'TMP_ORCH_WF_"+wf.replace('-','_').upper()+"_%' IN SCHEMA "+APP),'execution and finalizer cleanup')
            pass_check('native finalization and both temporary tasks clean up')
            before=record(run_id)
            call('SP_ORCH_START_RUN',run_id)
            assert record(run_id)==before
            pass_check('terminal compatibility wakeup cannot replay work')
            pending=query('SELECT STATE,KIND,LAST_ERROR FROM '+META+'.ORCH_DISPATCH_DELIVERIES WHERE RUN_ID=%s AND STATE<>\'DONE\'',(run_id,))
            assert not pending,pending
            pass_check('launch and completion deliveries drain without a periodic task')
            if scenario == 'delivery':
                # Simulate durable status with lost completion/dependency hooks,
                # plus a process killed on its final allowed delivery attempt.
                execution_ids=[r['EXECUTION_ID'] for r in before]
                for table in ('ORCH_DISPATCH_DELIVERIES','ORCH_DISPATCH_EVENTS'):
                    query('DELETE FROM '+META+'.'+table+" WHERE KIND IN ('FINISH','DEPENDENCY') AND RUN_ID IN ("+','.join(['%s']*len(execution_ids))+')',execution_ids)
                interrupted=str(uuid.uuid4())
                query('INSERT INTO '+META+".ORCH_DISPATCH_DELIVERIES (EVENT_ID,KIND,RUN_ID,STATE,ATTEMPTS) VALUES(%s,'LAUNCH',%s,'READY',3)",(interrupted,run_id))
                repair=call('SP_ORCH_RECONCILE',wf)
                assert not repair.get('errors'),repair
                wait(lambda:query('SELECT COUNT(*) N FROM '+META+".ORCH_DISPATCH_EVENTS e JOIN "+META+".ORCH_DISPATCH_DELIVERIES d ON d.EVENT_ID=e.EVENT_ID WHERE e.KIND='DEPENDENCY' AND d.STATE='DONE' AND e.RUN_ID IN ("+','.join(['%s']*len(execution_ids))+')',execution_ids)[0]['N']==len(execution_ids),'missing completion/dependency repair')
                wait(lambda:query('SELECT STATE,ATTEMPTS FROM '+META+'.ORCH_DISPATCH_DELIVERIES WHERE EVENT_ID=%s',(interrupted,))[0]==dict(STATE='DONE',ATTEMPTS=1),'interrupted final-attempt repair')
                assert record(run_id)==before
                pass_check('explicit scoped repair restores lost completion/job dependency signals and exhausted delivery without replay')
                duplicate=str(uuid.uuid4())
                query('INSERT INTO '+META+".ORCH_DISPATCH_EVENTS (EVENT_ID,KIND,RUN_ID,TASK_RUN_GROUP_ID) VALUES(%s,'FINISH',%s,%s)",(duplicate,run_id,before[0]['TASK_RUN_GROUP_ID']))
                wait(lambda:query('SELECT COUNT(*) N FROM '+META+".ORCH_DISPATCH_DELIVERIES WHERE EVENT_ID=%s AND STATE='DONE'",(duplicate,))[0]['N']==1,'duplicate native completion after task cleanup')
                assert record(run_id)==before
                pass_check('duplicate native completion after graph cleanup is idempotent')
            if scenario == 'extended':
                wfq,jq=fixture('serial queue', "SELECT SYSTEM$WAIT(8, 'SECONDS')")
                first=request(wfq)
                second=request(wfq)
                one=done(first);two=done(second)
                assert two[0]['STARTED_AT'] >= one[0]['ENDED_AT'],(one,two)
                pass_check('two accepted requests for the same workflow execute serially and advance without polling')

                wff,jf=fixture('job failure', 'SELECT 1/0')
                failed=request(wff);rows=done(failed,'FAILED')
                assert rows[1]['STATUS']=='FAILED',rows
                pass_check('SQL job failure records terminal workflow/job states')

                wft,jt=fixture('job timeout', "SELECT SYSTEM$WAIT(15, 'SECONDS')")
                query('UPDATE '+META+'.JOB SET TIMEOUT_SECONDS=2 WHERE JOB_ID=%s',(jt,))
                timed=request(wft);done(timed,'FAILED')
                pass_check('job timeout finalizes and releases the workflow')

                wfc,jc=fixture('native cancellation', "SELECT SYSTEM$WAIT(90, 'SECONDS')")
                cancelled=request(wfc)
                active=wait(lambda:next((r for r in record(cancelled) if r['JOB_ID'] is None and r['STATUS']=='RUNNING'),None),'native run starts')
                query('SELECT SYSTEM$USER_TASK_CANCEL_ONGOING_EXECUTIONS(%s)',(active['TASK_FQN'],))
                done(cancelled,'CANCELLED')
                pass_check('native cancellation is recovered by finalizer event without maintenance')

                # Real completion releases requests blocked by related workflows,
                # including the reverse (running-descendant) activity gate.
                parent,jp=fixture('ancestor')
                child,jchild=fixture('descendant',"SELECT SYSTEM$WAIT(18, 'SECONDS')",parent=parent)
                cr=request(child)
                wait(lambda:record(cr)[0]['STATUS']=='RUNNING','descendant runs')
                pr=request(parent)
                child_done=done(cr);parent_done=done(pr)
                assert parent_done[0]['STARTED_AT'] >= child_done[0]['ENDED_AT'],(child_done,parent_done)
                pass_check('a related running descendant blocks its ancestor and completion releases it')

                a,ja=fixture('fork A')
                b,jb=fixture('fork B',parent=a,dep=True)
                c,jc=fixture('fork C',"SELECT SYSTEM$WAIT(8, 'SECONDS')",parent=a,dep=True)
                d,jd=fixture('join D',parent=[b,c],dep=True)
                # Seed an older C success: D must not join using it while the new
                # sibling C has only been admitted and B finishes quickly.
                old_c=request(c);done(old_c)
                root=request(a);done(root)
                def chain():
                    rows=query('SELECT EXECUTION_ID,WORKFLOW_ID,STATUS,STARTED_AT,ENDED_AT FROM '+META+'.WORKFLOW_EXECUTION_LOG WHERE JOB_ID IS NULL AND REQUESTED_BY=%s AND WORKFLOW_ID IN (%s,%s,%s) AND EXECUTION_ID<>%s',(marker,b,c,d,old_c))
                    return rows if len(rows)==3 and all(r['STATUS']=='SUCCESS' for r in rows) else None
                chain_rows=wait(chain,'fork and join complete',300)
                by_wf={r['WORKFLOW_ID']:r for r in chain_rows}
                assert by_wf[d]['STARTED_AT'] >= max(by_wf[b]['ENDED_AT'],by_wf[c]['ENDED_AT']),chain_rows
                pass_check('fork/join admits both siblings before launch and never joins an older parent success')

            if scenario in ('extended', 'delivery'):
                # More than one native batch of already-completed launch signals.
                ids=[str(uuid.uuid4()) for _ in range(105)]
                cur.executemany('INSERT INTO '+META+".ORCH_DISPATCH_EVENTS (EVENT_ID,KIND,RUN_ID) VALUES(%s,'LAUNCH',%s)",[(id,run_id) for id in ids])
                wait(lambda:query('SELECT COUNT(*) N FROM '+META+".ORCH_DISPATCH_DELIVERIES WHERE STATE='DONE' AND EVENT_ID IN ("+','.join(['%s']*len(ids))+')',ids)[0]['N']==len(ids),'105-event native batching',600)
                assert record(run_id)==before
                pass_check('105 duplicate delivery events cross the native batch limit without lost work or replay')

                missing=str(uuid.uuid4());synthetic.append(missing)
                query('INSERT INTO '+META+".ORCH_DISPATCH_EVENTS (KIND,RUN_ID) VALUES('LAUNCH',%s)",(missing,))
                err=wait(lambda:next((r for r in query('SELECT STATE,ATTEMPTS FROM '+META+'.ORCH_DISPATCH_DELIVERIES WHERE RUN_ID=%s',(missing,)) if r['STATE']=='ERROR'),None),'retry quarantine',360)
                assert err['ATTEMPTS']==3,err
                pass_check('invalid delivery stops after three attempts and remains visible for explicit repair')

                # Finish every finalizer before removing this isolated catalog.
                for wf in workflows:
                    wait(lambda wf=wf:not query("SHOW TASKS LIKE 'TMP_ORCH_WF_"+wf.replace('-','_').upper()+"_%' IN SCHEMA "+APP),'all temporary graphs clean up')
                pass_check('all success, failure and cancellation graphs clean up')
            passed=True

        finally:
            active=query('SELECT EXECUTION_ID,STATUS FROM '+META+'.WORKFLOW_EXECUTION_LOG WHERE REQUESTED_BY=%s AND JOB_ID IS NULL AND STATUS IN (\'QUEUED\',\'CLAIMED\',\'RUNNING\')',(marker,))
            if active:
                print('Keeping isolated fixtures for diagnosis: '+json.dumps(dict(marker=marker,active=active),default=str),flush=True)
            else:
                # Keep execution evidence on a failed assertion, rather than hiding
                # delivery failures. A successful test removes only its own rows.
                if passed:
                    query('DELETE FROM '+META+'.ORCH_DISPATCH_DELIVERIES WHERE RUN_ID IN (SELECT EXECUTION_ID FROM '+META+'.WORKFLOW_EXECUTION_LOG WHERE REQUESTED_BY=%s)',(marker,))
                    query('DELETE FROM '+META+'.ORCH_DISPATCH_EVENTS WHERE RUN_ID IN (SELECT EXECUTION_ID FROM '+META+'.WORKFLOW_EXECUTION_LOG WHERE REQUESTED_BY=%s)',(marker,))
                    query('DELETE FROM '+META+'.WORKFLOW_EXECUTION_LOG WHERE REQUESTED_BY=%s',(marker,))
                    for id in synthetic:
                        query('DELETE FROM '+META+'.ORCH_DISPATCH_DELIVERIES WHERE RUN_ID=%s',(id,))
                        query('DELETE FROM '+META+'.ORCH_DISPATCH_EVENTS WHERE RUN_ID=%s',(id,))
                    for wf in workflows:
                        query('DELETE FROM '+META+'.DEPENDENCIES WHERE WORKFLOW_ID=%s',(wf,))
                        query('DELETE FROM '+META+'.WORKFLOW WHERE WORKFLOW_ID=%s',(wf,))
                    for job in jobs:query('DELETE FROM '+META+'.JOB WHERE JOB_ID=%s',(job,))
    report=dict(environment='DEV',marker=marker,checks=checks,tst_changed=False,business_workflows_executed=False)
    if report_path:Path(report_path).write_text(json.dumps(report,indent=2)+'\n')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--report')
    parser.add_argument('--scenario',choices=('smoke','extended','delivery'),default='extended')
    args=parser.parse_args()
    if args.run:print(json.dumps(run(args.report,args.scenario),indent=2))
    else:parser.print_help()
