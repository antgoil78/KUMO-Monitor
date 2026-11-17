"""Enable DEV child-workflow dispatch after execution; read-only unless --apply.

Changes only admission, dispatch, executor and recovery procedures. No workflows,
tasks, schedules, execution records or services are changed by deployment.
"""
import argparse
import hashlib
import json

from migration_support import ROOT, connect
from apply_workflow_command_templates import definition_only, logical_body
from snowflake.connector import DictCursor
from apply_manual_dbt_full_refresh import restore_grants

APP = 'KUMO_DEV.MONITOR_APP'
META = 'KUMO_DEV.MONITOR_META'
NAMES = ('SP_ORCH_REQUEST_RUN', 'SP_ORCH_DISPATCH_DEPENDENCIES', 'SP_ORCH_EXECUTE_WORKFLOW', 'SP_ORCH_RECONCILE')
PREVIOUS = {
    'SP_ORCH_REQUEST_RUN': ('461afa164185098f2c9945fe8f5a464b07e39fde17a8b351936fe4ef26c8614b', '7acc16823a642b7a3f030a9643d7c8bd402c6f02dea6ec6922c5ec65540ffc1f'),
    'SP_ORCH_EXECUTE_WORKFLOW': ('8e6771827da8faaa52f9bc90cda131cdd083a26684762aad0cef2fc5622eabca',),
    'SP_ORCH_RECONCILE': ('df149272fffbcff1ccd2b1f80bb3a22167cf7399ee48b1ed3e767d4a98807043',),
}
SIGNATURE = '(VARCHAR,VARCHAR,VARCHAR,VARCHAR,VARCHAR,BOOLEAN,VARCHAR)'


def digest(ddl):
    return hashlib.sha256(logical_body(ddl).strip().encode()).hexdigest()


def upgrade(cursor, apply=False):
    def query(sql, params=()):
        cursor.execute(sql, params)
        return cursor.fetchall()
    environment = next(iter(query(f'CALL {META}.SP_GET_ENVIRONMENT()')[0].values()))
    environment = json.loads(environment) if isinstance(environment, str) else environment
    if (environment.get('DATABASE'), environment.get('META_SCHEMA'), environment.get('MONITOR_APP_SCHEMA')) != ('KUMO_DEV', 'MONITOR_META', 'MONITOR_APP'):
        raise ValueError('Expected DEV app routing')
    pending = query(f"SELECT COUNT(*) N FROM {META}.WORKFLOW_EXECUTION_LOG WHERE JOB_ID IS NULL AND STATUS IN ('QUEUED','CLAIMED','RUNNING')")[0]['N']
    if pending:
        raise ValueError('Finish pending DEV workflow runs before updating execution procedures')
    role = query('SELECT CURRENT_ROLE() NAME')[0]['NAME']
    definitions, usage, admission = {}, [], None
    for name in NAMES:
        ddl = definition_only((ROOT / 'snowflake/ddl/KUMO_DEV/MONITOR_APP/procedures' / (name + '.sql')).read_text())
        definitions[name] = ddl
        existing = query(f"SHOW PROCEDURES LIKE '{name}' IN SCHEMA {APP}")
        if not existing:
            if name != 'SP_ORCH_DISPATCH_DEPENDENCIES':
                raise ValueError('Missing maintained procedure: ' + name)
            continue
        if len(existing) != 1:
            raise ValueError('Unexpected procedure overloads: ' + name)
        count = existing[0]['max_num_arguments']
        if name == 'SP_ORCH_REQUEST_RUN':
            if count not in (5, 6, 7) or existing[0]['min_num_arguments'] != 5:
                raise ValueError('Unexpected admission signature')
            signature = '(VARCHAR,VARCHAR,VARCHAR,VARCHAR,VARCHAR' + (',BOOLEAN' if count >= 6 else '') + (',VARCHAR' if count >= 7 else '') + ')'
        else:
            if count != 1 or existing[0]['min_num_arguments'] != 1:
                raise ValueError('Unexpected procedure signature: ' + name)
            signature = '(VARCHAR)'
        address = APP + '.' + name + signature
        grants = query('SHOW GRANTS ON PROCEDURE ' + address)
        if not any(row['privilege'] == 'OWNERSHIP' and row['grantee_name'] == role for row in grants):
            raise ValueError('Use the DEV procedure owner role: ' + name)
        if any(row['granted_to'] not in ('ROLE', 'DATABASE_ROLE') for row in grants):
            raise ValueError('Unexpected procedure grant type: ' + name)
        current = query('SELECT GET_DDL(\'PROCEDURE\',%s,TRUE) DDL', (address,))[0]['DDL']
        if digest(current) not in (*PREVIOUS.get(name, ()), digest(ddl)):
            raise ValueError('Live procedure differs from the maintained version: ' + name)
        if name == 'SP_ORCH_REQUEST_RUN':
            admission = dict(address=address, ddl=current, grants=grants, count=count)
        if name == 'SP_ORCH_EXECUTE_WORKFLOW':
            usage = [row for row in grants if row['privilege'] == 'USAGE']
    if apply:
        for name in NAMES:
            signature = SIGNATURE if name == 'SP_ORCH_REQUEST_RUN' else '(VARCHAR)'
            address = APP + '.' + name + signature
            if name == 'SP_ORCH_REQUEST_RUN' and admission['count'] != 7:
                # Optional overloads cannot coexist. Keep the original in memory
                # only so failed CREATE can restore this one procedure/grants.
                cursor.execute('DROP PROCEDURE ' + admission['address'])
                try:
                    cursor.execute(definitions[name])
                except Exception:
                    cursor.execute(admission['ddl'])
                    restore_grants(cursor, admission['address'], admission['grants'])
                    raise
                restore_grants(cursor, address, admission['grants'])
            else:
                cursor.execute(definitions[name].replace('\nRETURNS ', '\nCOPY GRANTS\nRETURNS ', 1))
            if name == 'SP_ORCH_DISPATCH_DEPENDENCIES':
                for grant in usage:
                    kind = 'DATABASE ROLE' if grant['granted_to'] == 'DATABASE_ROLE' else 'ROLE'
                    parts = grant['grantee_name'].split('.') if kind == 'DATABASE ROLE' else [grant['grantee_name']]
                    grantee = '.'.join('"' + part.replace('"', '""') + '"' for part in parts)
                    cursor.execute(f'GRANT USAGE ON PROCEDURE {APP}.{name}(VARCHAR) TO {kind} {grantee}')
            deployed = query('SELECT GET_DDL(\'PROCEDURE\',%s,TRUE) DDL', (address,))[0]['DDL']
            if digest(deployed) != digest(definitions[name]):
                raise RuntimeError('Deployed procedure does not match source: ' + name)
    return dict(applied=apply, environment='DEV', procedures=list(NAMES), dispatch_enabled=True,
                result_field='WORKFLOW_EXECUTION_LOG.RESULT.dependencyEvaluation')


def run(apply=False):
    with connect() as connection, connection.cursor(DictCursor) as cursor:
        cursor.execute("ALTER SESSION SET QUERY_TAG='KUMO_DEV_DEPENDENCY_DISPATCH_DEPLOY'")
        return upgrade(cursor, apply)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    print(json.dumps(run(parser.parse_args().apply), indent=2))
