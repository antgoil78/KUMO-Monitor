"""Provision the two TST monitor schemas from the current saved DEV definitions.

Default is a read-only preflight. --apply creates missing objects and the eight
TST environment parameters, and relocates the earlier TST readiness-log table.
Existing objects must match; nothing is replaced. No DEV data is copied, no
workflow is run, and the master remains suspended. No DDL export is performed.
"""
import argparse
from collections import Counter
import json
import re
import sys

from migration_support import ROOT, connect
from apply_environment_routing import procedure_definitions, KEYS
from apply_workflow_command_templates import definition_only, logical_body
sys.path.insert(0, str(ROOT / 'snowflake'))
from export_ddl import signature as procedure_signature, object_key
from snowflake.connector import DictCursor

DATABASE = 'KUMO_TST'
APP = DATABASE + '.MONITOR_APP'
META = DATABASE + '.MONITOR_META'
STORE = META + '.APPLICATION_PARAMETERS'
OLD_LOG = DATABASE + '.META._TMP_SET_READY_LOG'
NEW_LOG = META + '._TMP_SET_READY_LOG'
MASTER = APP + '.TASK_WF_MASTER_DISPATCHER'
ENVIRONMENT = dict(DATABASE=DATABASE, MONITOR_APP_SCHEMA='MONITOR_APP', META_SCHEMA='MONITOR_META',
                   APP_SCHEMA='META', RAW_SCHEMA='RAW_LIM', DBT_SCHEMA='DBT_APP',
                   DBT_PROJECT=DATABASE, DBT_TARGET='kumo_TST')


def create_only(ddl):
    return re.sub(r'CREATE\s+OR\s+REPLACE\s+', 'CREATE ', ddl, count=1, flags=re.I).rstrip(';')


def definitions():
    manifest = json.loads((ROOT / 'snowflake/manifest.json').read_text())
    by_file = {item['file']: item for item in manifest['objects']}
    procs = {item['file']: (name, ddl) for item, name, ddl in procedure_definitions(ENVIRONMENT, STORE)}
    order = [by_file[path] for path in manifest['restore_order'] if path in by_file]
    # The parameter-reading helper is available before other procedures compile.
    order.sort(key=lambda item: (0 if item['type'] == 'TABLE' else
                                1 if item['name'].endswith('.SP_GET_ENVIRONMENT') else
                                2 if item['type'] == 'PROCEDURE' else
                                3 if item['type'] == 'STREAM' else 4))
    result = []
    for item in order:
        if item['type'] == 'PROCEDURE':
            name, ddl = procs[item['file']]
        else:
            source = (ROOT / 'snowflake' / item['file']).read_text()
            ddl = re.sub(r'\bKUMO_DEV\b', DATABASE, definition_only(source), flags=re.I)
            name = item['name'].replace('KUMO_DEV.', DATABASE + '.', 1)
        if re.search(r'\b(?:KUMO_DEV|X_KUMO_TST|X_KUMO_ADMIN)\b', ddl, re.I):
            raise ValueError('Unexpected source-environment reference in ' + name)
        schema = name.split('.')[1]
        if schema not in ('MONITOR_APP', 'MONITOR_META'):
            raise ValueError('Object is outside the two requested monitor schemas: ' + name)
        result.append((item['type'], name, create_only(ddl)))
    if Counter(kind for kind, _, _ in result) != dict(TABLE=22, PROCEDURE=18, STREAM=1, TASK=1):
        raise ValueError('Review unexpected source manifest counts before provisioning')
    return result


def existing_objects(cursor):
    keys = set()
    cursor.execute("SELECT TABLE_SCHEMA,TABLE_NAME,TABLE_TYPE FROM KUMO_TST.INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA IN ('MONITOR_APP','MONITOR_META')")
    for row in cursor.fetchall():
        kind = 'VIEW' if row['TABLE_TYPE'] == 'VIEW' else 'TABLE'
        keys.add(object_key(kind, DATABASE + '.' + row['TABLE_SCHEMA'] + '.' + row['TABLE_NAME']))
    for schema in ('MONITOR_APP', 'MONITOR_META'):
        cursor.execute('SHOW USER PROCEDURES IN SCHEMA ' + DATABASE + '.' + schema)
        for row in cursor.fetchall():
            if row.get('schema_name') == schema:
                keys.add(object_key('PROCEDURE', DATABASE + '.' + schema + '.' + row['name'], procedure_signature(row['arguments'])))
    for kind, schema in (('STREAM', META), ('TASK', APP)):
        cursor.execute('SHOW ' + kind + 'S IN SCHEMA ' + schema)
        for row in cursor.fetchall():
            keys.add(object_key(kind, schema + '.' + row['name']))
    return keys


def key_for(kind, name):
    if kind == 'PROCEDURE':
        name, args = name.split('(', 1)
        return object_key(kind, name, '(' + args)
    return object_key(kind, name)


def comparable(ddl):
    start = re.search(r'\bCREATE\s+(?:OR\s+REPLACE\s+)?', ddl, re.I)
    if not start:
        raise ValueError('Expected a CREATE statement')
    value = create_only(ddl[start.start():].strip())
    return re.sub(r'\s+', ' ', value).strip().upper()


def verify_existing(cursor, kind, name, desired):
    cursor.execute('SELECT GET_DDL(%s,%s,TRUE) AS DDL', (kind, name))
    live = cursor.fetchone()['DDL']
    if kind == 'PROCEDURE':
        # The shared parser expects the native GET_DDL CREATE OR REPLACE header.
        exported_desired = re.sub(r'^CREATE\s+', 'CREATE OR REPLACE ', desired, count=1, flags=re.I)
        matches = logical_body(live).strip() == logical_body(exported_desired).strip()
    elif kind == 'TASK':
        matches = live[live.index('DECLARE'):].strip().rstrip(';') == desired[desired.index('DECLARE'):].strip().rstrip(';')
    elif kind == 'STREAM':
        cursor.execute("SHOW STREAMS LIKE 'WORKFLOW_EXECUTION_QUEUE_STREAM' IN SCHEMA " + META)
        row = cursor.fetchone()
        matches = row and row['table_name'] == META + '.WORKFLOW_RUN_QUEUE'
    else:
        matches = comparable(live) == comparable(desired)
    if not matches:
        raise ValueError('Existing TST object differs from the current definition; nothing will replace it: ' + name)


def parameter_plan(cursor, table_exists):
    rows = {}
    if table_exists:
        cursor.execute(f"SELECT PARAMETER_KEY,PARAMETER_VALUE,VALUE_TYPE,ACTIVE_FL,IS_SECRET FROM {STORE} WHERE INSTANCE_NAME='DEFAULT' AND PARAMETER_GROUP='ENVIRONMENT'")
        rows = {row['PARAMETER_KEY']: row for row in cursor.fetchall()}
    for key, value in ENVIRONMENT.items():
        row = rows.get(key)
        if row and (row['PARAMETER_VALUE'] != value or row['VALUE_TYPE'] != 'STRING' or not row['ACTIVE_FL'] or row['IS_SECRET']):
            raise ValueError('Existing TST environment parameter conflicts: ' + key)
    return [key for key in KEYS if key not in rows]


def run(apply=False):
    desired = definitions()
    created = []
    with connect() as conn, conn.cursor(DictCursor) as cursor:
        cursor.execute("ALTER SESSION SET QUERY_TAG='KUMO_TST_MONITOR_SCHEMA_PROVISION'")
        cursor.execute('USE ROLE KUMO_ADMIN')
        existing = existing_objects(cursor)
        unexpected = existing - {key_for(kind, name) for kind, name, _ in desired}
        if unexpected:
            raise ValueError('Review unexpected objects in the destination before provisioning: ' + str(sorted(unexpected)))
        cursor.execute("SELECT TABLE_TYPE FROM KUMO_TST.INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA='META' AND TABLE_NAME='_TMP_SET_READY_LOG'")
        old = cursor.fetchone()
        move_log = bool(old)
        if old and (old['TABLE_TYPE'] != 'BASE TABLE' or key_for('TABLE', NEW_LOG) in existing):
            raise ValueError('Review conflicting readiness-log objects before relocating the earlier table')
        before_log = None
        if move_log:
            cursor.execute('SELECT COUNT(*) AS N,HASH_AGG(*) AS HASH FROM ' + OLD_LOG)
            before_log = cursor.fetchone()
        for kind, name, ddl in desired:
            if key_for(kind, name) in existing:
                verify_existing(cursor, kind, name, ddl)
        missing_parameters = parameter_plan(cursor, key_for('TABLE', STORE) in existing)
        cursor.execute('SHOW DBT PROJECTS IN SCHEMA KUMO_TST.DBT_APP')
        projects = cursor.fetchall()
        if not any(row['name'] == ENVIRONMENT['DBT_PROJECT'] and row.get('default_target') == ENVIRONMENT['DBT_TARGET'] for row in projects):
            raise ValueError('Expected the existing TST dbt project and target')
        plan = Counter(kind for kind, name, _ in desired if key_for(kind, name) not in existing and name != (NEW_LOG if move_log else ''))
        if not apply:
            return dict(applied=False, create=dict(plan), move_readiness_log=move_log,
                        readiness_rows_to_preserve=before_log['N'] if before_log else None,
                        environment_parameters_to_insert=missing_parameters, master_state='suspended')
        if move_log:
            cursor.execute('ALTER TABLE ' + OLD_LOG + ' RENAME TO ' + NEW_LOG)
            cursor.execute('SELECT COUNT(*) AS N,HASH_AGG(*) AS HASH FROM ' + NEW_LOG)
            if cursor.fetchone() != before_log:
                raise RuntimeError('Readiness log fingerprint differs after relocation')
            existing.add(key_for('TABLE', NEW_LOG))
            print('Relocated', NEW_LOG, flush=True)
        parameters_written = False
        for kind, name, ddl in desired:
            if kind != 'TABLE' and not parameters_written:
                for key in missing_parameters:
                    cursor.execute(f"INSERT INTO {STORE} (INSTANCE_NAME,PARAMETER_GROUP,PARAMETER_KEY,PARAMETER_VALUE,VALUE_TYPE,DESCRIPTION,IS_SECRET,ACTIVE_FL) VALUES ('DEFAULT','ENVIRONMENT',%s,%s,'STRING',%s,FALSE,TRUE)",
                                   (key, ENVIRONMENT[key], 'TST environment setting: ' + key))
                parameters_written = True
            if key_for(kind, name) in existing:
                continue
            cursor.execute('USE SCHEMA ' + '.'.join(name.split('.')[:2]))
            cursor.execute(ddl)
            created.append((kind, name))
            existing.add(key_for(kind, name))
            print('Created', kind, name, flush=True)
        # Verify the complete requested object set and TST runtime bindings.
        actual = existing_objects(cursor)
        if actual != {key_for(kind, name) for kind, name, _ in desired}:
            raise RuntimeError('Destination object inventory does not match the app definitions')
        cursor.execute('CALL ' + META + '.SP_GET_ENVIRONMENT()')
        environment = next(iter(cursor.fetchone().values()))
        if isinstance(environment, str):
            environment = json.loads(environment)
        if any(environment[key] != value for key, value in ENVIRONMENT.items()):
            raise RuntimeError('The TST engine is not reading the intended environment parameters')
        for definition in [dict(workflowType='SQL', sqlCommand='SELECT COUNT(*) FROM [DATABASE].[META_SCHEMA]._TMP_SET_READY_LOG'),
                           dict(workflowType='DBT', dbtCommand='build --select tag:smoke --target [DBT_TARGET]')]:
            cursor.execute(f'CALL {APP}.SP_RESOLVE_WORKFLOW_COMMAND(PARSE_JSON(%s),NULL)', (json.dumps(definition),))
            result = next(iter(cursor.fetchone().values()))
            if isinstance(result, str):
                result = json.loads(result)
            if not result.get('ok') or 'KUMO_DEV' in result.get('execSql', '') or '[' in result.get('execSql', ''):
                raise RuntimeError('TST command resolution failed: ' + str(result))
            if definition['workflowType'] == 'DBT' and (result['dbtProjectFqn'] != 'KUMO_TST.DBT_APP.KUMO_TST' or result['dbtTarget'] != 'kumo_TST'):
                raise RuntimeError('dbt resolved outside the intended TST project/target')
        cursor.execute("SELECT UNIQUE_CONSTRAINT_CATALOG FROM KUMO_TST.INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA='MONITOR_META'")
        if any(row['UNIQUE_CONSTRAINT_CATALOG'] != DATABASE for row in cursor.fetchall()):
            raise RuntimeError('A monitor foreign key references another database')
        cursor.execute("SHOW STREAMS LIKE 'WORKFLOW_EXECUTION_QUEUE_STREAM' IN SCHEMA " + META)
        if cursor.fetchone()['table_name'] != META + '.WORKFLOW_RUN_QUEUE':
            raise RuntimeError('Queue stream does not reference the TST queue')
        cursor.execute("SHOW TASKS LIKE 'TASK_WF_MASTER_DISPATCHER' IN SCHEMA " + APP)
        task = cursor.fetchone()
        if task['state'] != 'suspended' or 'KUMO_TST.MONITOR_META.WORKFLOW_EXECUTION_QUEUE_STREAM' not in task['condition']:
            raise RuntimeError('Master must be suspended and bound to the TST stream')
        cursor.execute(f'SELECT (SELECT COUNT(*) FROM {META}.WORKFLOW_RUN_QUEUE) AS QUEUE_ROWS,(SELECT COUNT(*) FROM {META}.WORKFLOW_HISTORY) AS HISTORY_ROWS')
        runtime_rows = cursor.fetchone()
        return dict(applied=True, created=dict(Counter(kind for kind, _ in created)),
                    total_objects=dict(Counter(kind for kind, _, _ in desired)),
                    relocated_readiness_log=move_log, parameters_inserted=len(missing_parameters),
                    parameter_table=STORE, master_state=task['state'],
                    resolution_verified=True, workflow_commands_executed=False, **runtime_rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    print(json.dumps(run(parser.parse_args().apply), indent=2))
