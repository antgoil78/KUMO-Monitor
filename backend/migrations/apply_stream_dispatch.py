"""Install stream-triggered dispatch in DEV only; read-only unless --apply.

No service/image updates, TST changes, schema exports or business executions.
Admission is briefly closed during the idle cutover. Existing grants are kept.
"""
import argparse
import hashlib
import json
from pathlib import Path

from migration_support import ROOT, connect
from apply_workflow_command_templates import definition_only, logical_body
from snowflake.connector import DictCursor

APP, META = 'KUMO_DEV.MONITOR_APP', 'KUMO_DEV.MONITOR_META'
DDL = ROOT / 'snowflake/ddl/KUMO_DEV'
SIGNATURES = {
    'SP_ORCH_LAUNCH_RUN': '(VARCHAR)',
    'SP_ORCH_FINISH_RUN': '(VARCHAR,VARCHAR)',
    'SP_ORCH_DISPATCH': '()',
    'SP_ORCH_START_RUN': '(VARCHAR)',
    'SP_ORCH_DISPATCH_DEPENDENCIES': '(VARCHAR)',
    'SP_ORCH_EXECUTE_WORKFLOW': '(VARCHAR)',
    'SP_ORCH_RECONCILE': '(VARCHAR)',
    'SP_ORCH_RUN_SCHEDULED': '(VARCHAR)',
    'SP_ORCH_REQUEST_RUN': '(VARCHAR,VARCHAR,VARCHAR,VARCHAR,VARCHAR,BOOLEAN,VARCHAR)',
}
PREVIOUS = {
    'SP_ORCH_DISPATCH': ('a3c1317e93dbf44d91c4361a625b2b0f393de94b3be97f1259dc1755b9c79190', 'c2b083b2263843e7c6ae56ed663c95fc601d76fe6b4d8a3504708f58b6d5c6cc'),
    'SP_ORCH_FINISH_RUN': ('9f278e5b2175686fac3f36d1135c6fa3cb5040fbb2423b536c1d223caec38640', '40bb8a029af1d2fbe627fdae72a88376825c2182ed6e034ecabd7b204274b9d6'),
    'SP_ORCH_REQUEST_RUN': 'ba82ce31f15d0a135d490e73e86429979b9504c5b4ed3c61469b69157f2b24ae',
    'SP_ORCH_START_RUN': '3f8926ae3ceef70e743ea57189871e44ac8b57bd81c2f12fa446c5dae6c9f365',
    'SP_ORCH_EXECUTE_WORKFLOW': '299b08387a9013328642418d75c3c436b5b59d72f3409ce262c0acb02ce8f72f',
    'SP_ORCH_RUN_SCHEDULED': '5be51838175ac642a2201cdccb5a034230a7663174885053f77a62a19f1d63d5',
    'SP_ORCH_DISPATCH_DEPENDENCIES': 'a419e8d7615a9193f384c65d0bb5e0f0388ff15acff79273a360d3a47cd7df94',
    'SP_ORCH_RECONCILE': ('77f2ed42f6989c6147a3f346f20cd132b265e8c24d07b3fa1b3f9455d39fd633', '9c6d64eba208c7d108323efc005b108b71b681b4ac528ee83a58e529404ee845', 'd1fe20818522d27b2a85cb861af0ca459ced8cdf35f1982bc1e3359976b1a9a2', '8ad28d062e25424a266a82d1edf1659b10910743bdf339688f1b014836d1b66e'),
}


def digest(ddl):
    return hashlib.sha256(logical_body(ddl).strip().encode()).hexdigest()


def upgrade(cursor, apply=False):
    def query(sql, params=()):
        cursor.execute(sql, params)
        return cursor.fetchall()
    env = next(iter(query(f'CALL {META}.SP_GET_ENVIRONMENT()')[0].values()))
    env = json.loads(env) if isinstance(env, str) else env
    if any(env.get(key) != expected for key, expected in (
        ('DATABASE', 'KUMO_DEV'), ('META_SCHEMA', 'MONITOR_META'), ('MONITOR_APP_SCHEMA', 'MONITOR_APP'))):
        raise ValueError('This migration is DEV only')
    if env.get('_parameterTable', META + '.APPLICATION_PARAMETERS') != META + '.APPLICATION_PARAMETERS':
        raise ValueError('Review the DEV bootstrap parameter table before cutover')
    role = query('SELECT CURRENT_ROLE() NAME')[0]['NAME']
    if role != 'KUMO_ADMIN':
        raise ValueError('Use the DEV owner role KUMO_ADMIN')
    definitions, live = {}, {}
    for name, signature in SIGNATURES.items():
        definitions[name] = definition_only((DDL / ('MONITOR_APP/procedures/' + name + '.sql')).read_text())
        existing = query(f"SHOW PROCEDURES LIKE '{name}' IN SCHEMA {APP}")
        if not existing:
            if name in PREVIOUS and name not in ('SP_ORCH_DISPATCH', 'SP_ORCH_FINISH_RUN'):
                raise ValueError('Missing current DEV procedure: ' + name)
            continue
        if len(existing) != 1:
            raise ValueError('Unexpected overload: ' + name)
        grants = query('SHOW GRANTS ON PROCEDURE ' + APP + '.' + name + signature)
        if not any(g['privilege'] == 'OWNERSHIP' and g['grantee_name'] == role for g in grants):
            raise ValueError('Unexpected procedure owner: ' + name)
        live[name] = query("SELECT GET_DDL('PROCEDURE',%s,TRUE) DDL", (APP + '.' + name + signature,))[0]['DDL']
        previous = PREVIOUS.get(name, ())
        previous = (previous,) if isinstance(previous, str) else previous
        if digest(live[name]) not in (*previous, digest(definitions[name])):
            raise ValueError('Unknown DEV source drift: ' + name)
    tasks = query(f"SHOW TASKS LIKE 'TASK_ORCH_%' IN SCHEMA {APP}")
    for task in tasks:
        if task['owner'] != role or not str(task.get('comment') or '').startswith(('KUMO parallel orchestration', 'KUMO stream dispatch', 'KUMO orchestration')):
            raise ValueError('Unexpected engine task: ' + task['name'])
    existing_tables = query(f"SHOW TABLES LIKE 'ORCH_DISPATCH_EVENTS' IN SCHEMA {META}")
    existing_tables += query(f"SHOW TABLES LIKE 'ORCH_DISPATCH_DELIVERIES' IN SCHEMA {META}")
    existing_streams = query(f"SHOW STREAMS LIKE 'ORCH_DISPATCH_EVENTS_STREAM' IN SCHEMA {META}")
    for row in existing_tables + existing_streams:
        if row['owner'] != role:
            raise ValueError('Unexpected dispatch object owner')
    if existing_streams and (str(existing_streams[0].get('stale')).lower() == 'true' or
                             not str(existing_streams[0].get('table_name', '')).upper().endswith('.ORCH_DISPATCH_EVENTS')):
        raise ValueError('Review stale or misdirected stream; never replace its offset automatically')

    def check_idle():
        if query(f"SELECT COUNT(*) N FROM {META}.WORKFLOW_EXECUTION_LOG WHERE STATUS IN ('PENDING','INITIATING','QUEUED','CLAIMED','RUNNING')")[0]['N']:
            raise ValueError('Finish pending DEV executions before cutover')
        running = query("SELECT NAME,QUERY_ID FROM TABLE(KUMO_DEV.INFORMATION_SCHEMA.TASK_HISTORY("
                        "SCHEDULED_TIME_RANGE_START=>DATEADD('day',-1,CURRENT_TIMESTAMP()),RESULT_LIMIT=>10000)) "
                        "WHERE DATABASE_NAME='KUMO_DEV' AND SCHEMA_NAME='MONITOR_APP' AND STATE='EXECUTING' "
                        "AND (NAME LIKE 'TASK_ORCH_%' OR NAME LIKE 'TMP_ORCH_%')")
        if any(cursor.connection.is_still_running(cursor.connection.get_query_status(r['QUERY_ID'])) for r in running):
            raise ValueError('A DEV native engine task is executing')
    check_idle()
    result = dict(applied=False, environment='DEV', procedures=list(SIGNATURES),
                  dispatch='STREAM', periodic_maintenance=False, tst_changed=False, service_changed=False,
                  previous_task_states=[dict(name=t['name'], state=t['state'], schedule=t.get('schedule')) for t in tasks])
    if not apply:
        return result
    # No admission transaction may arrive between the idle check and replacing
    # its executor. Close this one entry point without touching the SPCS service.
    admission = 'SP_ORCH_REQUEST_RUN'
    header = definitions[admission].split('AS $$', 1)[0]
    gate = header + "AS $$ throw new Error('DEV stream dispatch upgrade in progress; retry shortly'); $$"
    cursor.execute(gate.replace('\nRETURNS ', '\nCOPY GRANTS\nRETURNS ', 1))
    paused = []
    try:
        for task in tasks:
            if task['state'] == 'started':
                cursor.execute('ALTER TASK ' + APP + '.' + task['name'] + ' SUSPEND')
                paused.append(task['name'])
        check_idle()
    except Exception:
        cursor.execute(definition_only(live[admission]).replace('\nRETURNS ', '\nCOPY GRANTS\nRETURNS ', 1))
        for name in paused:
            cursor.execute('ALTER TASK ' + APP + '.' + name + ' RESUME')
        raise
    # After this point failures deliberately leave admission and tasks paused;
    # a partial migration is reviewed before retry, never hidden by rollback.
    cursor.execute((DDL/'MONITOR_META/tables/ORCH_DISPATCH_EVENTS.sql').read_text())
    cursor.execute((DDL/'MONITOR_META/tables/ORCH_DISPATCH_DELIVERIES.sql').read_text())
    # Upgrade delivery state from the first DEV trial without replacing the
    # stream or losing consumed-but-unfinished events. Fresh installs copy none.
    cursor.execute(f"INSERT INTO {META}.ORCH_DISPATCH_DELIVERIES SELECT e.* FROM {META}.ORCH_DISPATCH_EVENTS e "
                   f"WHERE e.STATE <> 'NEW' AND e.KIND <> 'WAKE' AND NOT EXISTS (SELECT 1 FROM {META}.ORCH_DISPATCH_DELIVERIES d WHERE d.EVENT_ID=e.EVENT_ID)")
    cursor.execute((DDL/'MONITOR_META/streams/ORCH_DISPATCH_EVENTS_STREAM.sql').read_text())
    for name, source in definitions.items():
        if name == admission:
            continue
        cursor.execute(source.replace('\nRETURNS ', '\nCOPY GRANTS\nRETURNS ', 1))
    cursor.execute('ALTER TASK ' + APP + '.TASK_ORCH_MAINTENANCE UNSET SCHEDULE')
    cursor.execute("ALTER TASK " + APP + ".TASK_ORCH_MAINTENANCE SET COMMENT='KUMO orchestration: manual recovery only; no schedule'")
    cursor.execute((DDL/'MONITOR_APP/tasks/TASK_ORCH_DISPATCH.sql').read_text())
    for name, source in definitions.items():
        if name == admission:
            continue
        installed = query("SELECT GET_DDL('PROCEDURE',%s,TRUE) DDL", (APP+'.'+name+SIGNATURES[name],))[0]['DDL']
        if digest(installed) != digest(source):
            raise RuntimeError('Installed definition mismatch: ' + name)
    cursor.execute(f"MERGE INTO {META}.APPLICATION_PARAMETERS p USING (SELECT 'DEFAULT' I, 'ORCHESTRATION' G, 'STREAM_DISPATCH_ENABLED' K) s "
                   "ON p.INSTANCE_NAME=s.I AND p.PARAMETER_GROUP=s.G AND p.PARAMETER_KEY=s.K "
                   "WHEN MATCHED THEN UPDATE SET PARAMETER_VALUE='true',VALUE_TYPE='BOOLEAN',ACTIVE_FL=TRUE,IS_SECRET=FALSE,UPDATED_DTTM=SYSDATE(),UPDATED_BY=CURRENT_USER() "
                   "WHEN NOT MATCHED THEN INSERT (INSTANCE_NAME,PARAMETER_GROUP,PARAMETER_KEY,PARAMETER_VALUE,VALUE_TYPE,ACTIVE_FL,IS_SECRET) VALUES(s.I,s.G,s.K,'true','BOOLEAN',TRUE,FALSE)")
    cursor.execute(definitions[admission].replace('\nRETURNS ', '\nCOPY GRANTS\nRETURNS ', 1))
    cursor.execute('ALTER TASK ' + APP + '.TASK_ORCH_DISPATCH RESUME')
    for name in paused:
        if name not in ('TASK_ORCH_MAINTENANCE', 'TASK_ORCH_DISPATCH'):
            cursor.execute('ALTER TASK ' + APP + '.' + name + ' RESUME')
    result.update(applied=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    with connect() as connection, connection.cursor(DictCursor) as cursor:
        cursor.execute('USE ROLE KUMO_ADMIN')
        report = upgrade(cursor, args.apply)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2, default=str) + '\n')
    print(json.dumps(report, indent=2, default=str))
