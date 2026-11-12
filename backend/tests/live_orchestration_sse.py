"""Explicit DEV-only validation: one disposable workflow with harmless SQL jobs."""
import argparse
import json
from pathlib import Path
import sys
import threading
import time
from urllib.request import Request, urlopen
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'migrations'))
from migration_support import connect
from snowflake.connector import DictCursor

BASE = 'http://127.0.0.1:5001'
META = 'KUMO_DEV.MONITOR_META'
APP = 'KUMO_DEV.MONITOR_APP'


def api(path, body=None):
    request = Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                      headers={'Content-Type':'application/json'})
    with urlopen(request, timeout=30) as response:
        value = json.load(response)
    assert value.get('ok'), value
    return value


def run():
    assert api('/api/health')['db'] == 'KUMO_DEV'
    workflow = str(uuid.uuid4()); jobs = [str(uuid.uuid4()) for _ in range(2)]
    request_id = str(uuid.uuid4()); stop = threading.Event(); ready = threading.Event()
    updates = []; stream_errors = []
    def listen():
        try:
            with urlopen(BASE+'/api/events?page=Orchestration%20validation',timeout=25) as stream:
                for line in stream:
                    if stop.is_set(): return
                    if not line.startswith(b'data:'): continue
                    event = json.loads(line[5:])
                    if event['type'] == 'connected': ready.set()
                    if event['type'] == 'orchestration_update': updates.append(event['data'])
        except Exception as exc:
            if not stop.is_set(): stream_errors.append(str(exc))
    with connect() as conn, conn.cursor(DictCursor) as cursor:
        def query(sql, params=()): cursor.execute(sql,params); return cursor.fetchall()
        try:
            query(f'INSERT INTO {META}.WORKFLOW(WORKFLOW_ID,WORKFLOW_NAME,WORKFLOW_GROUP,WAREHOUSE_NAME) VALUES(%s,%s,%s,%s)',
                  (workflow,'SSE VALIDATION '+workflow[:8],'ENGINE_VALIDATION','KUMO_ELT_GEN_1'))
            query(f'INSERT INTO {META}.DEPENDENCIES(WORKFLOW_ID,ALLOW) VALUES(%s,TRUE)',(workflow,))
            for index, job in enumerate(jobs):
                query(f'INSERT INTO {META}.JOB(JOB_ID,JOB_NAME,JOB_TYPE,SQL_COMMAND) VALUES(%s,%s,%s,%s)',
                      (job,'SSE VALIDATION '+workflow[:8]+' '+str(index+1),'SQL','SELECT SYSTEM$WAIT(12)' if index==0 else 'SELECT 1'))
                query(f'INSERT INTO {META}.DEPENDENCIES(WORKFLOW_ID,JOB_ID,PARENT_ID,ALLOW) VALUES(%s,%s,%s,TRUE)',
                      (workflow,job,workflow if index==0 else jobs[index-1]))
            threading.Thread(target=listen,daemon=True).start()
            assert ready.wait(15),'SSE did not connect'
            start=time.monotonic()
            receipt=api(f'/api/orchestration/workflows/{workflow}/runs',dict(mode='WORKFLOW',requestId=request_id))
            submission_seconds=round(time.monotonic()-start,3)
            assert receipt['accepted'] and receipt['queryId'] and receipt['status']=='INITIATING',receipt
            print(json.dumps(dict(check='native query receipt returned before preparation completes',seconds=submission_seconds)),flush=True)
            assert submission_seconds<12,'Submission still waits for preparation'
            assert not query(f'SELECT EXECUTION_ID FROM {META}.WORKFLOW_EXECUTION_LOG WHERE WORKFLOW_ID=%s',(workflow,)), 'Preparation unexpectedly completed before receipt validation'
            start=time.monotonic()
            for _ in range(3): api('/api/orchestration/monitor')
            print(json.dumps(dict(check='three shared-cache HTTP reads',seconds=round(time.monotonic()-start,3))),flush=True)
            deadline=time.monotonic()+180
            while time.monotonic()<deadline:
                rows=[row for payload in updates for row in payload.get('rows',[]) if row['WORKFLOW_ID']==workflow and row['run'].get('requestId')==request_id]
                if rows and rows[-1]['run']['status']=='SUCCESS': break
                assert not stream_errors,stream_errors
                time.sleep(1)
            else: raise AssertionError(dict(streamErrors=stream_errors,states=[row['run']['status'] for row in rows]))
            active=[row for row in rows if row['run']['status'] in ('QUEUED','CLAIMED','RUNNING')]
            assert active and all(not row['canRun'] for row in active)
            assert any(row['run']['status']=='RUNNING' for row in active)
            assert rows[-1]['canRun']
            assert [job['run']['status'] for job in rows[-1]['jobs']]==['SUCCESS','SUCCESS']
            print(json.dumps(dict(check='SSE delivered queued, running and completed states; active run locked and completion unlocked',states=sorted({row['run']['status'] for row in rows}))),flush=True)
        finally:
            stop.set()
            runs=query(f'SELECT EXECUTION_ID,TASK_FQN,STATUS FROM {META}.WORKFLOW_EXECUTION_LOG WHERE WORKFLOW_ID=%s AND JOB_ID IS NULL',(workflow,))
            for row in runs:
                address=row['TASK_FQN']
                expected=APP+'.TMP_ORCH_WF_'+workflow.replace('-','_').upper()+'_'+row['EXECUTION_ID'].replace('-','_').upper()
                assert address==expected
                if row['STATUS'] in ('RUNNING','QUEUED','CLAIMED'):
                    try: query('SELECT SYSTEM$USER_TASK_CANCEL_ONGOING_EXECUTIONS(%s)',(address,))
                    except Exception: pass
                query('DROP TASK IF EXISTS '+address)
            query(f'DELETE FROM {META}.WORKFLOW_EXECUTION_LOG WHERE WORKFLOW_ID=%s',(workflow,))
            query(f'DELETE FROM {META}.DEPENDENCIES WHERE WORKFLOW_ID=%s',(workflow,))
            for job in jobs: query(f'DELETE FROM {META}.JOB WHERE JOB_ID=%s',(job,))
            query(f'DELETE FROM {META}.WORKFLOW WHERE WORKFLOW_ID=%s',(workflow,))
            api('/api/orchestration/monitor?refresh=1')
            print('Disposable SQL workflow, jobs, logs and task removed.',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run',action='store_true');args=parser.parse_args()
    if args.run: run()
    else: parser.print_help()
