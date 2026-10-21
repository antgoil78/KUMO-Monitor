"""Bind the engine to KUMO_ENVIRONMENT_TABLE and use its parameters for routing.

Default is a read-only preflight. --apply adds RAW_SCHEMA if missing, deploys
only changed engine procedures, and alters the static master task in place.
The master is briefly suspended and its original state is restored. Existing
active runs cause a clean stop before any DDL changes. No workflows are run.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from migration_support import ROOT, connect, procedure_parts
from apply_workflow_command_templates import logical_body
from snowflake.connector import DictCursor

KEYS = ('DATABASE', 'MONITOR_APP_SCHEMA', 'META_SCHEMA', 'APP_SCHEMA', 'RAW_SCHEMA',
        'DBT_SCHEMA', 'DBT_PROJECT', 'DBT_TARGET')


def identifier_path(value, parts=3):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*' + r'(?:\.[A-Za-z_][A-Za-z0-9_$]*)' * (parts - 1), value):
        raise ValueError('Expected an unquoted identifier path: ' + value)
    return value.upper()


def load_environment(cursor, table, raw_schema='RAW_LIM'):
    table = identifier_path(table)
    cursor.execute(f"SELECT PARAMETER_KEY, CASE WHEN IS_SECRET THEN NULL ELSE PARAMETER_VALUE END AS PARAMETER_VALUE, ACTIVE_FL, IS_SECRET, VALUE_TYPE FROM {table} WHERE INSTANCE_NAME = 'DEFAULT' AND PARAMETER_GROUP = 'ENVIRONMENT'")
    rows = cursor.fetchall()
    if len({row['PARAMETER_KEY'] for row in rows}) != len(rows):
        raise ValueError('Duplicate environment parameter')
    values, add_raw = {}, not any(row['PARAMETER_KEY'] == 'RAW_SCHEMA' for row in rows)
    if add_raw:
        rows.append(dict(PARAMETER_KEY='RAW_SCHEMA', PARAMETER_VALUE=identifier_path(raw_schema, 1), ACTIVE_FL=True, IS_SECRET=False, VALUE_TYPE='STRING'))
    for key in KEYS:
        row = next((row for row in rows if row['PARAMETER_KEY'] == key), {})
        value = row.get('PARAMETER_VALUE')
        if not row.get('ACTIVE_FL') or row.get('IS_SECRET') or row.get('VALUE_TYPE') != 'STRING' or not isinstance(value, str):
            raise ValueError('Missing or invalid environment parameter: ' + key)
        identifier_path(value, 1)
        values[key] = value if key == 'DBT_TARGET' else value.upper()
    return values, add_raw


def render_procedure(item, environment, table):
    source = (ROOT / 'snowflake' / item['file']).read_text()
    header, body = procedure_parts(source)
    body = logical_body(source)
    schema_key = 'MONITOR_APP_SCHEMA' if '.MONITOR_APP.' in item['name'] else 'META_SCHEMA'
    name = environment['DATABASE'] + '.' + environment[schema_key] + '.' + item['name'].rsplit('.', 1)[1]
    signature = name + item['signature']
    header = header.replace(item['name'], name, 1)
    body = body.replace('KUMO_DEV.MONITOR_META.SP_GET_ENVIRONMENT', environment['DATABASE'] + '.' + environment['META_SCHEMA'] + '.SP_GET_ENVIRONMENT')
    if name.endswith('.SP_GET_ENVIRONMENT'):
        body = re.sub(r"var parameterTable = '[^']+';", "var parameterTable = '" + table + "';", body)
    return signature, header + '\nAS $$' + body + '$$'


def procedure_definitions(environment, table):
    manifest = json.loads((ROOT / 'snowflake/manifest.json').read_text())
    items = [item for item in manifest['objects'] if item['type'] == 'PROCEDURE']
    if not any(item['name'].endswith('.SP_GET_ENVIRONMENT') for item in items):
        items.append(dict(name='KUMO_DEV.MONITOR_META.SP_GET_ENVIRONMENT', signature='()',
                          file='ddl/KUMO_DEV/MONITOR_META/procedures/SP_GET_ENVIRONMENT.sql'))
    items.sort(key=lambda item: (not item['name'].endswith('.SP_GET_ENVIRONMENT'), item['name'], item['signature']))
    return [(item, *render_procedure(item, environment, table)) for item in items]


def task_body(environment):
    source = (ROOT / 'snowflake/ddl/KUMO_DEV/MONITOR_APP/tasks/TASK_WF_MASTER_DISPATCHER.sql').read_text()
    body = source[source.index('DECLARE'):].strip().rstrip(';')
    return body.replace('KUMO_DEV.MONITOR_META.SP_GET_ENVIRONMENT', environment['DATABASE'] + '.' + environment['META_SCHEMA'] + '.SP_GET_ENVIRONMENT')


def body_hash(ddl):
    return hashlib.sha256(logical_body(ddl).strip().encode()).hexdigest()


# Reviewed bodies immediately before this migration; reject live DDL drift.
BEFORE = {
  "SP_CLEANUP_OLD_TMP_TASKS__f1f22a5fb754.sql": "d253e832df3517970d6dbdc37ecbcf7f84bdff294239066d18a90d9c1c409232",
  "SP_DBT_COMPLETE__9ee53188061e.sql": "91835f2a8bb07b39ef55d4c4759111ffdadd667ad0d16e7d4190171d8f1ca17c",
  "SP_LOAD_RAW_LIM_INCREMENTAL_PERSIST__a0a986b618dc.sql": "102ba4442d1555abc6b03ae544a3d0363cefe069f053bf65c791088bd79ba64e",
  "SP_PURGE_LIM_BATCH__a0a986b618dc.sql": "100a385f00d447c0c582f55a71892330b6ad63672e677b59ee7e1cb4c4cf26d6",
  "SP_RAW_LIM_META_REFRESH__c203e74ecf77.sql": "79d89f8c72c6cfd8647eba3ac68a0e5f912aa67daa1000cd3e9da82d75a5d47e",
  "SP_RAW_LIM_SET_READY__3b5f66d9f2b5.sql": "d34e7cef846f09299b14350158289704e97647b629c68fd00663ea5b18215499",
  "SP_RESOLVE_WORKFLOW_COMMAND__741f064d0255.sql": "711082133a45633091b500811cf6fa715ae38db0ed0c4146273263b5d62aa05b",
  "SP_RULE_BASED_WORKFLOW_REQUEST_RUN__0d4e803287d5.sql": "c5485196180b779527f4b2474333aa32217a48dad4ae1b962e2125e92416e176",
  "SP_RUN_WORKFLOW__a0a986b618dc.sql": "52e271473484d407ec79804e6a404f2ea4dc7b05febba92c39b394610fa3d145",
  "SP_RUN_WORKFLOW__c203e74ecf77.sql": "e7db4b04283a5b58b7817d5046887e76393980ddf7e663a7727e643d9f382c5e",
  "SP_SEND_NOTIFICATION__4f11ef95dbd7.sql": "51a781b91855119342b7c3ed8cad68bca86bb291b6b89ae5976e5c2be06b1cc6",
  "SP_SEND_WORKFLOW_EMAIL__bad29e9e5780.sql": "d909adfcdc182dbf14b875e7b1c007dfa49e8f8bedc2d3a45951e0c4381dd59c",
  "SP_SQL_COMPLETE__9ee53188061e.sql": "44c167e64d8c35e9a845aab800950d0c42da485f40093d757cff9f752afcd9d7",
  "SP_WORKFLOW_DISPATCH__a0a986b618dc.sql": "aa1969440b0536b870db189ecab7ced24c7f52f7e6688d0326c8fc2db9a38596",
  "SP_WORKFLOW_DISPATCH__c203e74ecf77.sql": "858a9e6e9b00c6cc339b16f9b32c4bc1f3ff99d940dec1a97477b45f3269d601",
  "SP_WORKFLOW_REQUEST_RUN__9d4a0f2e146d.sql": "aedbe8c1942da0f4eddc331d4793dcfe7dde8d6f081ac617ff0b1281733362e6",
  "LOAD_RAW_LIM_FROM_STAGE__06532d90d921.sql": "2483d475c1285d665167bdefbb336d6ef05e623aca962c575b441a45f6b2fa5d"
}
BEFORE_TASK = 'de228993cd247b6a4cb28389432b19b2890771677b91c8a3ec3b9b4b3dc42721'


def run(table, apply=False, raw_schema='RAW_LIM'):
    table = identifier_path(table)
    changed, resume = [], False
    try:
        with connect() as conn, conn.cursor(DictCursor) as cursor:
            cursor.execute("ALTER SESSION SET QUERY_TAG = 'KUMO_ENVIRONMENT_ROUTING'")
            environment, add_raw = load_environment(cursor, table, raw_schema)
            app = environment['DATABASE'] + '.' + environment['MONITOR_APP_SCHEMA']
            meta = environment['DATABASE'] + '.' + environment['META_SCHEMA']
            task = app + '.TASK_WF_MASTER_DISPATCHER'
            changes = []
            for item, signature, desired in procedure_definitions(environment, table):
                cursor.execute(f"SHOW PROCEDURES LIKE '{item['name'].rsplit('.', 1)[1]}' IN SCHEMA {signature.rsplit('.', 1)[0]}")
                exists = bool(cursor.fetchall())
                if exists:
                    cursor.execute("SELECT GET_DDL('PROCEDURE', %s, TRUE) AS DDL", (signature,))
                    live = cursor.fetchone()['DDL']
                    if body_hash(live) == body_hash(desired):
                        continue
                    if body_hash(live) not in (BEFORE.get(Path(item['file']).name), body_hash(desired)):
                        raise ValueError('Review live procedure drift before deployment: ' + signature)
                    desired = desired.replace('\nRETURNS ', '\nCOPY GRANTS\nRETURNS ', 1)
                changes.append((signature, desired))
            cursor.execute(f"SHOW TASKS LIKE 'TASK_WF_MASTER_DISPATCHER' IN SCHEMA {app}")
            task_rows = cursor.fetchall()
            if len(task_rows) != 1:
                raise ValueError('Expected the existing master task in the configured engine schema')
            original_state = task_rows[0]['state']
            cursor.execute("SELECT GET_DDL('TASK', %s, TRUE) AS DDL", (task,))
            live_task = cursor.fetchone()['DDL']
            existing_body = live_task[live_task.index('DECLARE'):].strip().rstrip(';')
            desired_body = task_body(environment)
            stream = meta + '.WORKFLOW_EXECUTION_QUEUE_STREAM'
            desired_when = "SYSTEM$STREAM_HAS_DATA('" + stream + "')"
            change_task = existing_body != desired_body or task_rows[0].get('condition') != desired_when
            if existing_body != desired_body and hashlib.sha256(existing_body.encode()).hexdigest() != BEFORE_TASK:
                raise ValueError('Review live master task drift before deployment')
            if not apply:
                return dict(applied=False, parameter_table=table, add_raw_schema=add_raw,
                            procedures_to_update=[sig for sig,_ in changes], update_master=change_task,
                            master_state=original_state)
            try:
                if changes or change_task:
                    if original_state == 'started':
                        cursor.execute(f'ALTER TASK {task} SUSPEND')
                        resume = True
                    cursor.execute(f"SELECT COUNT(*) AS N FROM {meta}.WORKFLOW_HISTORY WHERE UPPER(TRIM(STATUS)) IN ('RUNNING', 'STARTING')")
                    if cursor.fetchone()['N']:
                        raise ValueError('Wait for the active workflow runs to finish before migrating the engine')
                if add_raw:
                    cursor.execute(f"INSERT INTO {table} (INSTANCE_NAME, PARAMETER_GROUP, PARAMETER_KEY, PARAMETER_VALUE, VALUE_TYPE, DESCRIPTION, IS_SECRET, ACTIVE_FL) VALUES ('DEFAULT','ENVIRONMENT','RAW_SCHEMA',%s,'STRING','Schema containing raw LIM data tables.',FALSE,TRUE)", (environment['RAW_SCHEMA'],))
                for signature, ddl in changes:
                    cursor.execute(ddl)
                    changed.append(('PROCEDURE',signature))
                    print('Updated',signature,flush=True)
                # CALLER procedures need the same USAGE access to the new helper.
                if any(sig.endswith('.SP_GET_ENVIRONMENT()') for sig,_ in changes):
                    cursor.execute(f'SHOW GRANTS ON PROCEDURE {meta}.LOAD_RAW_LIM_FROM_STAGE(VARCHAR,DATE,DATE,BOOLEAN,BOOLEAN,BOOLEAN,VARCHAR)')
                    grants = cursor.fetchall()
                    for grant in grants:
                        if grant['privilege'] == 'USAGE' and grant['granted_to'] in ('ROLE', 'DATABASE_ROLE'):
                            grantee = '.'.join('"' + part.replace('"','""') + '"' for part in grant['grantee_name'].split('.'))
                            cursor.execute(f"GRANT USAGE ON PROCEDURE {meta}.SP_GET_ENVIRONMENT() TO {grant['granted_to'].replace('_', ' ')} {grantee}")
                cursor.execute(f'CALL {meta}.SP_GET_ENVIRONMENT()')
                result = next(iter(cursor.fetchone().values()))
                if isinstance(result,str): result=json.loads(result)
                if any(result[key] != environment[key] for key in KEYS):
                    raise RuntimeError('Environment readback does not match the deployment')
                if change_task:
                    cursor.execute(f'ALTER TASK {task} MODIFY AS ' + desired_body)
                    changed.append(('TASK',task))
                    cursor.execute(f'ALTER TASK {task} MODIFY WHEN ' + desired_when)
            finally:
                if resume:
                    cursor.execute(f'ALTER TASK {task} RESUME')
                    resume = False
            return dict(applied=True, parameter_table=table, added_raw_schema=add_raw,
                        changed_objects=changed, master_state=original_state,
                        workflow_commands_executed=False)
    finally:
        # Only the two maintained DEV schemas have repository DDL copies.
        saved = [(kind,name) for kind,name in changed if name.startswith(('KUMO_DEV.MONITOR_APP.','KUMO_DEV.MONITOR_META.'))]
        if saved:
            command=[sys.executable,str(ROOT/'snowflake/export_ddl.py')]
            for kind,name in saved: command += ['--object',kind,name]
            subprocess.run(command,check=True,cwd=ROOT)
            # The exporter assigns the canonical signature hash to the new helper.
            placeholder=ROOT/'snowflake/ddl/KUMO_DEV/MONITOR_META/procedures/SP_GET_ENVIRONMENT.sql'
            if placeholder.exists(): placeholder.unlink()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment-table', default=os.getenv('KUMO_ENVIRONMENT_TABLE','KUMO_DEV.MONITOR_META.APPLICATION_PARAMETERS'))
    parser.add_argument('--raw-schema',default='RAW_LIM',help='Value to insert only if RAW_SCHEMA is missing')
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    print(json.dumps(run(args.environment_table,args.apply,args.raw_schema),indent=2))
