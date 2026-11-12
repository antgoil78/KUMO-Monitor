"""Disposable SQL runs verify complete job states and run-scoped dbt telemetry reads."""
import json
from pathlib import Path
import sys
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'migrations'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from migration_support import connect
from snowflake.connector import DictCursor
import orchestration_monitor as monitor
import config

META, APP = 'KUMO_DEV.MONITOR_META', 'KUMO_DEV.MONITOR_APP'


def run(progress_only=False):
    workflow = str(uuid.uuid4()); jobs = [str(uuid.uuid4()) for _ in range(4)]
    telemetry = str(uuid.uuid4())
    with connect() as connection, connection.cursor(DictCursor) as cursor:
        def query(sql, params=()):
            cursor.execute(sql, params); return cursor.fetchall()
        def call(name, params):
            value = next(iter(query(f'CALL {APP}.{name}(' + ','.join('%s' for _ in params) + ')', params)[0].values()))
            value = json.loads(value) if isinstance(value, str) else value
            assert value.get('ok'), value
            return value
        def records(run_id):
            return query(f'SELECT * FROM {META}.WORKFLOW_EXECUTION_LOG WHERE WORKFLOW_RUN_ID=%s ORDER BY JOB_POSITION NULLS FIRST', (run_id,))
        def states(run_id):
            return [row['STATUS'] for row in records(run_id)[1:]]
        def wait(predicate, seconds=300):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                if predicate(): return
                time.sleep(1)
            raise AssertionError('Timed out waiting for fixture execution')
        def tasks():
            prefix = 'TMP_ORCH_WF_' + workflow.replace('-', '_').upper() + '_'
            return [row for row in query(f"SHOW TASKS LIKE '{prefix}%' IN SCHEMA {APP}") if row['name'].startswith(prefix)]
        try:
            if not progress_only:
                query(f'INSERT INTO {META}.WORKFLOW(WORKFLOW_ID,WORKFLOW_NAME,WORKFLOW_GROUP,WAREHOUSE_NAME) VALUES(%s,%s,%s,%s)', (workflow,'EXECUTION STATES '+workflow[:8],'ENGINE_VALIDATION','KUMO_ELT_GEN_1'))
                query(f'INSERT INTO {META}.DEPENDENCIES(WORKFLOW_ID,ALLOW) VALUES(%s,TRUE)', (workflow,))
                parent = workflow
                for index, job in enumerate(jobs):
                    query(f'INSERT INTO {META}.JOB(JOB_ID,JOB_NAME,JOB_TYPE,SQL_COMMAND,IS_ENABLED) VALUES(%s,%s,%s,%s,%s)', (job,'STATE '+str(index),'SQL','SELECT SYSTEM$WAIT(25)' if index==0 else 'SELECT 1',index!=3))
                    query(f'INSERT INTO {META}.DEPENDENCIES(WORKFLOW_ID,JOB_ID,PARENT_ID,ALLOW,SUCCESS) VALUES(%s,%s,%s,TRUE,TRUE)',(workflow,job,parent));parent=job
                full = call('SP_ORCH_REQUEST_RUN',(workflow,'WORKFLOW',None,str(uuid.uuid4()),'STATE_VALIDATION'))['runId']
                assert states(full)[0] in ('INITIATING','QUEUED') and states(full)[1:]==['PENDING','PENDING','SKIPPED'], states(full)
                only = call('SP_ORCH_REQUEST_RUN',(workflow,'JOB_ONLY',jobs[1],str(uuid.uuid4()),'STATE_VALIDATION'))['runId']
                onward = call('SP_ORCH_REQUEST_RUN',(workflow,'FROM_JOB',jobs[1],str(uuid.uuid4()),'STATE_VALIDATION'))['runId']
                assert states(only)[0]=='SKIPPED' and states(only)[1] in ('INITIATING','QUEUED') and states(only)[2:]==['SKIPPED','SKIPPED'], states(only)
                assert states(onward)[0]=='SKIPPED' and states(onward)[1] in ('INITIATING','QUEUED') and states(onward)[2:]==['PENDING','SKIPPED'], states(onward)
                call('SP_ORCH_START_RUN',(full,))
                call('SP_ORCH_START_RUN',(only,))
                assert states(only)==['SKIPPED','QUEUED','SKIPPED','SKIPPED'], states(only)
                wait(lambda: states(full)[0]=='RUNNING')
                assert states(full)==['RUNNING','PENDING','PENDING','SKIPPED'], states(full)
                wait(lambda: all(records(run_id)[0]['STATUS']=='SUCCESS' for run_id in [full,only,onward]))
                assert states(full)==['SUCCESS','SUCCESS','SUCCESS','SKIPPED']
                assert states(only)==['SKIPPED','SUCCESS','SKIPPED','SKIPPED']
                assert states(onward)==['SKIPPED','SUCCESS','SUCCESS','SKIPPED']
                wait(lambda: not tasks(), 45)
                print('PASS: every job has a state; initiating/queued/running/pending/skipped; all three run scopes and temporary task cleanup',flush=True)
            # Mimic dbt hooks with a private execution ID; read real progress
            # schemas without executing any data models or touching other runs.
            for table in (config.MODEL_PROGRESS_TABLE,config.TEST_PROGRESS_TABLE):
                for name,progress,status,seconds in [('a','FINISHED','OK',-1),('b','STARTED','OK',0),('c','QUEUED','OK',0),('d','SKIPPED','OK',0)]:
                    query(f'INSERT INTO {table}(RUN_ID,MODEL_NAME,PROGRESS,STATUS,PROGRESS_DTTM) SELECT %s,%s,%s,%s,DATEADD(second,%s,SYSDATE())',(telemetry,name,progress,status,seconds))
            original = monitor.sf.query_service
            def service(sql, params=None):
                cursor.execute(sql,params or {});return cursor.fetchall()
            monitor.sf.query_service = service
            try: progress,warnings=monitor.read_dbt_progress([dict(EXECUTION_ID=telemetry,JOB_ID=jobs[1],RUN_JOB_TYPE='DBT',STATUS='RUNNING')])
            finally: monitor.sf.query_service=original
            assert not warnings, warnings
            assert progress[telemetry]['percent']==50, progress
            assert progress[telemetry]['models']['total']==4 and progress[telemetry]['tests']['total']==4, progress
            print('PASS: real model/test telemetry queries calculate run-scoped 50 percent progress',flush=True)
        finally:
            for task in tasks():
                address=APP+'.'+task['name']
                query('SELECT SYSTEM$USER_TASK_CANCEL_ONGOING_EXECUTIONS(%s)',(address,));query('DROP TASK IF EXISTS '+address)
            for table in (config.MODEL_PROGRESS_TABLE,config.TEST_PROGRESS_TABLE):query(f'DELETE FROM {table} WHERE RUN_ID=%s',(telemetry,))
            query(f'DELETE FROM {META}.WORKFLOW_EXECUTION_LOG WHERE WORKFLOW_ID=%s',(workflow,))
            query(f'DELETE FROM {META}.DEPENDENCIES WHERE WORKFLOW_ID=%s',(workflow,))
            for job in jobs:query(f'DELETE FROM {META}.JOB WHERE JOB_ID=%s',(job,))
            query(f'DELETE FROM {META}.WORKFLOW WHERE WORKFLOW_ID=%s',(workflow,))
            print('Fixture rows and tasks removed',flush=True)


if __name__=='__main__': run(progress_only='--progress-only' in sys.argv)
