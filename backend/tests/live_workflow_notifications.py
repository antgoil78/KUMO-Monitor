"""Opt-in DEV notification SQL validation; a disposable sender never sends mail.

Run after apply_workflow_notifications.py --apply. A private transient execution
table and procedure copies isolate all fixture mail from engine housekeeping.
Only fixture objects/configuration are removed. No business workflows are run.
"""
import json
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'migrations'))
from migration_support import connect
from apply_workflow_command_templates import logical_body
from snowflake.connector import DictCursor

META, APP = 'KUMO_DEV.MONITOR_META', 'KUMO_DEV.MONITOR_APP'


def validate_engine(cursor):
    """A disposable division-by-zero job checks the real completion hook."""
    workflow, job, request = (str(uuid.uuid4()) for _ in range(3))
    def query(sql, params=()):
        cursor.execute(sql, params)
        return cursor.fetchall()
    def call(name, params):
        value = next(iter(query('CALL ' + APP + '.' + name + '(' + ','.join('%s' for _ in params) + ')', params)[0].values()))
        value = json.loads(value) if isinstance(value, str) else value
        assert value.get('ok'), value
        return value
    def tasks():
        prefix = 'TMP_ORCH_WF_' + workflow.replace('-', '_').upper() + '_'
        return [row for row in query(f"SHOW TASKS LIKE '{prefix}%' IN SCHEMA {APP}") if row['name'].startswith(prefix)]
    try:
        warehouse = query('SELECT CURRENT_WAREHOUSE() WH')[0]['WH']
        assert warehouse, 'Validation requires an active warehouse'
        query(f'INSERT INTO {META}.WORKFLOW(WORKFLOW_ID,WORKFLOW_NAME,WORKFLOW_GROUP,WAREHOUSE_NAME) VALUES(%s,%s,%s,%s)',
              (workflow,'NOTIFICATION ENGINE VALIDATION '+workflow[:8],'ENGINE_VALIDATION',warehouse))
        query(f'INSERT INTO {META}.JOB(JOB_ID,JOB_NAME,JOB_TYPE,SQL_COMMAND) VALUES(%s,%s,%s,%s)',
              (job,'NOTIFICATION FAILURE '+job[:8],'SQL','SELECT 1 / 0'))
        query(f'INSERT INTO {META}.DEPENDENCIES(WORKFLOW_ID,ALLOW) VALUES(%s,TRUE)', (workflow,))
        query(f'INSERT INTO {META}.DEPENDENCIES(WORKFLOW_ID,JOB_ID,PARENT_ID,ALLOW,SUCCESS) VALUES(%s,%s,%s,TRUE,TRUE)', (workflow,job,workflow))
        # No notification rule: the actual engine must record NOT_CONFIGURED and
        # cannot email anyone, even if housekeeping races this validation.
        run_id = call('SP_ORCH_REQUEST_RUN',(workflow,'WORKFLOW',None,request,'NOTIFICATION_VALIDATION'))['runId']
        call('SP_ORCH_START_RUN',(run_id,))
        deadline = time.monotonic()+120
        while time.monotonic()<deadline:
            result = query(f'''SELECT STATUS,RESULT:notification:status::VARCHAR NOTIFICATION_STATUS,
                RESULT:notification:reason::VARCHAR REASON FROM {META}.WORKFLOW_EXECUTION_LOG WHERE EXECUTION_ID=%s''',(run_id,))[0]
            if result['STATUS']=='FAILED' and result['NOTIFICATION_STATUS']:
                assert result['NOTIFICATION_STATUS']=='SKIPPED' and result['REASON']=='NOT_CONFIGURED',result
                break
            time.sleep(2)
        else:
            raise AssertionError('Timed out waiting for the real workflow notification hook: '+str(result))
        call('SP_ORCH_RECONCILE',(workflow,))
        print('PASS: a real disposable failing SQL job invokes the deployed notifier and preserves FAILED; unconfigured workflows do not send mail',flush=True)
    finally:
        for task in tasks():
            address=APP+'.'+task['name']
            query('SELECT SYSTEM$USER_TASK_CANCEL_ONGOING_EXECUTIONS(%s)',(address,))
            query('DROP TASK IF EXISTS '+address)
        query(f'DELETE FROM {META}.WORKFLOW_EXECUTION_LOG WHERE WORKFLOW_ID=%s',(workflow,))
        query(f'DELETE FROM {META}.DEPENDENCIES WHERE WORKFLOW_ID=%s',(workflow,))
        query(f'DELETE FROM {META}.JOB WHERE JOB_ID=%s',(job,))
        query(f'DELETE FROM {META}.WORKFLOW WHERE WORKFLOW_ID=%s',(workflow,))
        print('Removed disposable engine validation workflow/job/run',flush=True)


def run():
    suffix = uuid.uuid4().hex.upper()
    workflow, run_id, job_id = (str(uuid.uuid4()) for _ in range(3))
    table = META + '.TMP_NOTIFY_VALIDATION_' + suffix
    notifier = APP + '.TMP_NOTIFY_VALIDATION_' + suffix
    sender = APP + '.TMP_NOTIFY_SENDER_' + suffix
    sender_signature = '(VARCHAR,VARCHAR,VARCHAR,VARCHAR,VARCHAR,VARCHAR,VARCHAR,VARCHAR,VARCHAR,VARCHAR,TIMESTAMP_NTZ,FLOAT,VARIANT)'
    with connect() as conn, conn.cursor(DictCursor) as cursor:
        def query(sql, params=()):
            cursor.execute(sql, params)
            return cursor.fetchall()
        def call():
            result = next(iter(query('CALL ' + notifier + '(%s)', (run_id,))[0].values()))
            return json.loads(result) if isinstance(result, str) else result
        try:
            cursor.execute("ALTER SESSION SET QUERY_TAG='KUMO_DEV_NOTIFICATION_VALIDATION'")
            query('CREATE TRANSIENT TABLE ' + table + ' LIKE ' + META + '.WORKFLOW_EXECUTION_LOG')
            ddl = query("SELECT GET_DDL('PROCEDURE',%s,TRUE) DDL", (APP + '.SP_ORCH_NOTIFY(VARCHAR)',))[0]['DDL']
            body = logical_body(ddl)
            body = body.replace("var log = ns + '.WORKFLOW_EXECUTION_LOG';", "var log = '" + table + "';")
            body = body.replace("app + '.SP_SEND_WORKFLOW_EMAIL(", "app + '." + sender.rsplit('.', 1)[1] + "(")
            if '.SP_SEND_WORKFLOW_EMAIL(' in body or "var log = ns + '.WORKFLOW_EXECUTION_LOG'" in body:
                raise AssertionError('Validation could not isolate sender/execution table')
            query('CREATE PROCEDURE ' + notifier + '(P_RUN_ID VARCHAR) RETURNS VARIANT LANGUAGE JAVASCRIPT EXECUTE AS OWNER AS $$' + body + '$$')
            query('CREATE PROCEDURE ' + sender + '''(P_INTEGRATION_NAME VARCHAR,P_TO VARCHAR,P_ENV VARCHAR,P_SEVERITY VARCHAR,
                P_RUN_ID VARCHAR,P_SUBJECT_AREA VARCHAR,P_SOURCE_ID VARCHAR,P_PKG_GROUP_NAME VARCHAR,P_PHASE VARCHAR,
                P_EVENT_TYPE VARCHAR,P_EVENT_DTTM TIMESTAMP_NTZ,P_ROWS_AFFECTED FLOAT,P_DETAILS_JSON VARIANT)
                RETURNS VARCHAR LANGUAGE JAVASCRIPT EXECUTE AS OWNER AS $$
                if (!P_TO || P_ENV !== 'DEV' || P_DETAILS_JSON.failed_jobs[0].ERROR_MESSAGE !== 'Validation failure') throw new Error('Invalid email payload');
                snowflake.execute({sqlText: "UPDATE ''' + table + ''' SET RESULT=OBJECT_INSERT(COALESCE(RESULT,OBJECT_CONSTRUCT()),'senderCalls',COALESCE(RESULT:senderCalls::NUMBER,0)+1,TRUE) WHERE EXECUTION_ID=?",binds:[P_RUN_ID]});
                return 'EMAIL_SENT'; $$''')
            # This identity exists only in the private log, never the engine log.
            query(f'''INSERT INTO {META}.WORKFLOW_NOTIFICATIONS
                (WORKFLOW_ID,ON_FAIL_EMAIL,ON_SUCCESS_EMAIL,FAIL_GROUP,EMAIL_INTEGRATION,ENVIRONMENT)
                VALUES(%s,TRUE,FALSE,'KUMO_DEV','MY_EMAIL_INT','DEV')''', (workflow,))
            snapshot = json.dumps(dict(workflow=dict(WORKFLOW_NAME='NOTIFICATION VALIDATION',WORKFLOW_GROUP='ENGINE_VALIDATION')))
            for execution_id,job,status in [(run_id,None,'FAILED'),(job_id,job_id,'FAILED')]:
                query('INSERT INTO ' + table + '''(EXECUTION_ID,WORKFLOW_RUN_ID,WORKFLOW_ID,JOB_ID,REQUEST_ID,RUN_MODE,STATUS,
                    REQUESTED_BY,TASK_FQN,CONFIG_SNAPSHOT,ENDED_AT,ERROR_MESSAGE,RESULT)
                    SELECT %s,%s,%s,%s,%s,'WORKFLOW',%s,'NOTIFICATION_VALIDATION','VALIDATION',PARSE_JSON(%s),SYSDATE(),'Validation failure',OBJECT_CONSTRUCT('succeeded',0)''',
                    (execution_id,run_id,workflow,job,run_id,status,snapshot))
            report = call()
            assert report.get('status') == 'SENT', report
            assert call().get('status') == 'SENT'
            result = query('SELECT STATUS, RESULT:senderCalls::NUMBER AS CALLS, RESULT:succeeded::NUMBER AS SUCCEEDED FROM ' + table + ' WHERE EXECUTION_ID=%s', (run_id,))[0]
            assert result == dict(STATUS='FAILED',CALLS=1,SUCCEEDED=0), result
            print('PASS: live notifier SQL sends one payload through an isolated sender, includes failure details, preserves run status and suppresses duplicates', flush=True)
            query('UPDATE ' + table + " SET RESULT=OBJECT_CONSTRUCT(),ENDED_AT='2000-01-01'::TIMESTAMP_NTZ WHERE EXECUTION_ID=%s", (run_id,))
            assert call().get('reason') == 'BEFORE_ACTIVATION'
            query('UPDATE ' + table + ' SET RESULT=OBJECT_CONSTRUCT(),ENDED_AT=SYSDATE() WHERE EXECUTION_ID=%s', (run_id,))
            query(f'UPDATE {META}.WORKFLOW_NOTIFICATIONS SET ON_FAIL_EMAIL=FALSE WHERE WORKFLOW_ID=%s', (workflow,))
            assert call().get('reason') == 'DISABLED'
            print('PASS: historical completions and disabled rules do not call the sender', flush=True)
        finally:
            query('DROP PROCEDURE IF EXISTS ' + notifier + '(VARCHAR)')
            query('DROP PROCEDURE IF EXISTS ' + sender + sender_signature)
            query('DROP TABLE IF EXISTS ' + table)
            query(f'DELETE FROM {META}.WORKFLOW_NOTIFICATIONS WHERE WORKFLOW_ID=%s', (workflow,))
            print('Removed notification validation fixtures; no email was sent', flush=True)
        validate_engine(cursor)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true',required=True)
    parser.parse_args()
    run()
