"""Optional isolated live environment-routing check.

Creates a disposable database, empty table structures and synthetic environment
parameters. Runs one SQL marker workflow through the master body and async task,
checks dbt preview/completion and cleanup, then drops only that test database.
No existing workflow or business data is executed/copied. Requires CREATE DATABASE.
Run explicitly with --run; this is not part of routine unit-test discovery.
"""
import argparse
import json
from pathlib import Path
import sys
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'migrations'))
from migration_support import connect
from apply_environment_routing import procedure_definitions, task_body
from snowflake.connector import DictCursor

def run_check():
    run = uuid.uuid4().hex[:10].upper()
    db = 'KUMO_ENV_CHECK_' + run
    values = dict(DATABASE=db, MONITOR_APP_SCHEMA='ENGINE', META_SCHEMA='CONFIG', APP_SCHEMA='APPDATA', RAW_SCHEMA='RAWZONE', DBT_SCHEMA='PROJECTS', DBT_PROJECT='PROJECT_OBJECT', DBT_TARGET='target_case')
    meta = db + '.CONFIG'
    app = db + '.ENGINE'
    table = db + '.BOOTSTRAP.ENV_SETTINGS'
    with connect() as conn, conn.cursor(DictCursor) as c:
        c.execute("ALTER SESSION SET QUERY_TAG='KUMO_ISOLATED_ENV_ROUTING_CHECK'")
        created = False
        try:
            c.execute('CREATE TRANSIENT DATABASE ' + db)
            created = True
            for schema in ['ENGINE', 'CONFIG', 'APPDATA', 'RAWZONE', 'BOOTSTRAP']:
                c.execute('CREATE SCHEMA ' + db + '.' + schema)
            print('Created isolated routing database', db, flush=True)
            c.execute('CREATE TABLE ' + table + ' LIKE KUMO_DEV.MONITOR_META.APPLICATION_PARAMETERS')
            for k, v in values.items():
                c.execute('INSERT INTO ' + table + " (INSTANCE_NAME,PARAMETER_GROUP,PARAMETER_KEY,PARAMETER_VALUE,VALUE_TYPE,ACTIVE_FL,IS_SECRET) VALUES ('DEFAULT','ENVIRONMENT',%s,%s,'STRING',TRUE,FALSE)", (k, v))
            for name in ['WORKFLOWS', 'WORKFLOW_TASKS', 'WORKFLOW_HISTORY', 'WORKFLOW_RUN_QUEUE', 'WORKFLOW_RUN_LOGS', 'WORKFLOW_NOTIFICATIONS', 'EMAIL_GROUPS', 'APP_WORKFLOW_RUN_LOCKS', 'WORKFLOW_TMP_TASKS', 'RAW_LIM_PKG_GROUP_SOURCE']:
                c.execute(f'CREATE TABLE {meta}.{name} LIKE KUMO_DEV.MONITOR_META.{name}')
            c.execute(f'CREATE STREAM {meta}.WORKFLOW_EXECUTION_QUEUE_STREAM ON TABLE {meta}.WORKFLOW_RUN_QUEUE')
            c.execute(f'CREATE TABLE {db}.APPDATA.WORKFLOW_STATUS LIKE KUMO_DEV.META.WORKFLOW_STATUS')
            c.execute(f'CREATE TABLE {db}.APPDATA.MODEL_STATUS (MODEL_NAME VARCHAR, STATUS VARCHAR, LOADED_DTTM TIMESTAMP_NTZ)')
            c.execute(f'CREATE TABLE {db}.APPDATA.WORKFLOW_DEP_RULE (WORKFLOW_ID VARCHAR, DEPENDEE_TYPE VARCHAR, DEPENDEE_ID VARCHAR, SUCCESS BOOLEAN, WARNING BOOLEAN, ERROR BOOLEAN, NEWER BOOLEAN, ACTIVE_FL BOOLEAN)')
            c.execute(f'CREATE TABLE {db}.APPDATA.ROUTING_RESULT (DB_NAME VARCHAR, SCHEMA_NAME VARCHAR, RESOLVED_DATABASE VARCHAR, RESOLVED_RAW_SCHEMA VARCHAR)')
            for item, sig, ddl in procedure_definitions(values, table):
                c.execute(ddl)
                print('Compiled', sig.rsplit('.', 1)[-1], flush=True)
            c.execute(f'CALL {meta}.SP_GET_ENVIRONMENT()')
            environment = json.loads(next(iter(c.fetchone().values())))
            assert all((environment[k] == v for k, v in values.items()))
            c.execute(f'CALL {app}.SP_RESOLVE_WORKFLOW_COMMAND(PARSE_JSON(%s),NULL)', (json.dumps({'workflowType': 'DBT', 'dbtCommand': 'build --select example'}),))
            result = json.loads(next(iter(c.fetchone().values())))
            assert result['ok'] and result['dbtProjectFqn'] == db + '.PROJECTS.PROJECT_OBJECT' and (result['dbtTarget'] == 'target_case'), result
            print('Verified dbt preview against alternate parameter table and all custom schemas', flush=True)
            for command in [f"CALL {meta}.LOAD_RAW_LIM_FROM_STAGE('PYMT',NULL,NULL,FALSE,FALSE,FALSE,'KUMO_DEV')", f"CALL {app}.SP_RAW_LIM_META_REFRESH('PYMT','KUMO_DEV')", f"CALL {app}.SP_RAW_LIM_SET_READY('MISSING_GROUP',FALSE,FALSE,'KUMO_DEV')", f"CALL {app}.SP_PURGE_LIM_BATCH('KUMO_DEV')"]:
                try:
                    c.execute(command)
                except Exception as e:
                    assert 'Invalid' in str(e) or 'must match' in str(e), str(e)
                else:
                    raise AssertionError('Cross-database invocation was allowed')
            c.execute(f"CALL {app}.SP_RULE_BASED_WORKFLOW_REQUEST_RUN('00000000-0000-0000-0000-000000000000','not-a-workflow')")
            c.fetchall()
            workflow = str(uuid.uuid4())
            sql = "INSERT INTO [DATABASE].[APP_SCHEMA].ROUTING_RESULT SELECT CURRENT_DATABASE(),CURRENT_SCHEMA(),'[DATABASE]','[RAW_SCHEMA]'"
            c.execute(f"INSERT INTO {meta}.WORKFLOWS (WORKFLOW_ID,WORKFLOW_NAME,WORKFLOW_GROUP,DBT_COMMAND,WORKFLOW_TYPE,IS_ENABLED,SQL_COMMAND) VALUES (%s,'ISOLATED_ENV_ROUTING','TEST','','SQL',TRUE,%s)", (workflow, sql))
            c.execute(f"CALL {app}.SP_WORKFLOW_REQUEST_RUN(%s,'MANUAL',NULL,NULL,NULL)", (workflow,))
            runid = next(iter(c.fetchone().values()))
            c.execute('USE DATABASE ' + db)
            c.execute('USE SCHEMA ENGINE')
            c.execute('EXECUTE IMMEDIATE %s', (task_body(values),))
            c.fetchall()
            deadline = time.monotonic() + 100
            while time.monotonic() < deadline:
                c.execute(f'SELECT STATUS,ERROR_MESSAGE FROM {meta}.WORKFLOW_HISTORY WHERE RUN_ID=%s', (runid,))
                r = c.fetchone()
                if r['STATUS'] in ['SUCCESS', 'FAILED']:
                    if r['STATUS'] != 'SUCCESS':
                        c.execute(f'SELECT LOG_LEVEL,MESSAGE FROM {meta}.WORKFLOW_RUN_LOGS WHERE RUN_ID=%s ORDER BY LOG_TIME', (runid,))
                        print(c.fetchall(), flush=True)
                    assert r['STATUS'] == 'SUCCESS', r
                    break
                time.sleep(2)
            else:
                raise AssertionError('Isolated workflow did not finish: ' + str(r))
            c.execute(f'SELECT * FROM {db}.APPDATA.ROUTING_RESULT')
            r = c.fetchall()
            assert len(r) == 1 and r[0]['SCHEMA_NAME'] == 'RAWZONE' and (r[0]['DB_NAME'] == db) and (r[0]['RESOLVED_RAW_SCHEMA'] == 'RAWZONE'), r
            c.execute(f"INSERT INTO {db}.APPDATA.WORKFLOW_STATUS (WORKFLOW_ID,STATUS,RUN_ID) VALUES (%s,'PROCESSING',%s)", (workflow, runid))
            c.execute(f'CALL {app}.SP_DBT_COMPLETE(%s,TRUE)', (runid,))
            c.fetchall()
            c.execute(f'SELECT STATUS FROM {db}.APPDATA.WORKFLOW_STATUS WHERE RUN_ID=%s', (runid,))
            assert c.fetchone()['STATUS'] == 'SUCCESS'
            c.execute(f"UPDATE {meta}.WORKFLOW_TMP_TASKS SET CREATED_AT=DATEADD('minute',-2,SYSDATE()) WHERE RUN_ID=%s", (runid,))
            c.execute(f'CALL {app}.SP_CLEANUP_OLD_TMP_TASKS(1)')
            cleanup = json.loads(next(iter(c.fetchone().values())))
            assert len(cleanup['cleaned']) == 1 and (not cleanup['failed']), cleanup
            print('PASS: master -> dispatcher -> async SQL task -> completion, with all custom schemas and external bootstrap table', flush=True)
        finally:
            if created:
                c.execute('DROP DATABASE ' + db)
                print('Removed isolated test database and its tasks', flush=True)
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Create and remove the isolated test database')
    if parser.parse_args().run:
        run_check()
    else:
        parser.print_help()
