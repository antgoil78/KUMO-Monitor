"""Upgrade only manual run admission and command resolution; read-only by default.

Run before releasing the full-refresh UI. Keeps five-argument scheduled/manual
calls working through an optional boolean. No job definitions or tasks change.
"""
import argparse
import json

from migration_support import ROOT, connect
from apply_workflow_command_templates import definition_only
from snowflake.connector import DictCursor

OLD_SIGNATURE = '(VARCHAR,VARCHAR,VARCHAR,VARCHAR,VARCHAR)'
SIGNATURE = '(VARCHAR,VARCHAR,VARCHAR,VARCHAR,VARCHAR,BOOLEAN)'


def quote_role(value, kind):
    parts = value.split('.') if kind == 'DATABASE_ROLE' else [value]
    return '.'.join('"' + part.replace('"', '""') + '"' for part in parts)


def upgrade(cursor, database, apply=False):
    app, meta = database + '.MONITOR_APP', database + '.MONITOR_META'
    cursor.execute(f'CALL {meta}.SP_GET_ENVIRONMENT()')
    environment = next(iter(cursor.fetchone().values()))
    environment = json.loads(environment) if isinstance(environment, str) else environment
    if any(environment.get(key) != value for key, value in
           [('DATABASE', database), ('META_SCHEMA', 'MONITOR_META'), ('MONITOR_APP_SCHEMA', 'MONITOR_APP')]):
        raise ValueError('Unexpected environment routing')
    cursor.execute(f"SELECT COUNT(*) N FROM {meta}.WORKFLOW_EXECUTION_LOG WHERE JOB_ID IS NULL AND STATUS IN ('QUEUED','CLAIMED','RUNNING')")
    if cursor.fetchone()['N']:
        raise ValueError('Finish pending workflow runs before upgrading admission')
    cursor.execute(f"SHOW PROCEDURES LIKE 'SP_ORCH_REQUEST_RUN' IN SCHEMA {app}")
    procedures = cursor.fetchall()
    if len(procedures) != 1 or not str(procedures[0]['description']).startswith('KUMO parallel orchestration v'):
        raise ValueError('Expected exactly one maintained run-admission procedure')
    count = procedures[0]['max_num_arguments']
    if count not in (5, 6):
        raise ValueError('Unexpected run-admission signature')
    old_address = app + '.SP_ORCH_REQUEST_RUN' + (OLD_SIGNATURE if count == 5 else SIGNATURE)
    new_address = app + '.SP_ORCH_REQUEST_RUN' + SIGNATURE
    cursor.execute('SHOW GRANTS ON PROCEDURE ' + old_address)
    grants = cursor.fetchall()
    cursor.execute('SELECT CURRENT_ROLE() ROLE')
    role = cursor.fetchone()['ROLE']
    if not any(grant['privilege'] == 'OWNERSHIP' and grant['grantee_name'] == role for grant in grants):
        raise ValueError('Use the procedure owner role to preserve ownership')
    if any(grant['granted_to'] not in ('ROLE', 'DATABASE_ROLE') or grant['privilege'] not in ('USAGE', 'OWNERSHIP') for grant in grants):
        raise ValueError('Unexpected procedure grants')
    folder = ROOT / 'snowflake/ddl/KUMO_DEV/MONITOR_APP/procedures'
    resolver = definition_only(next(folder.glob('SP_RESOLVE_WORKFLOW_COMMAND__*.sql')).read_text()).replace('KUMO_DEV.', database + '.')
    admission = definition_only((folder / 'SP_ORCH_REQUEST_RUN.sql').read_text()).replace('KUMO_DEV.', database + '.')
    if apply:
        cursor.execute(resolver.replace('\nRETURNS ', '\nCOPY GRANTS\nRETURNS ', 1))
        if count == 5:
            # Optional arguments cannot coexist with the old overload in Snowflake.
            cursor.execute('SELECT GET_DDL(\'PROCEDURE\',%s,TRUE) DDL', (old_address,))
            original = cursor.fetchone()['DDL']
            cursor.execute('DROP PROCEDURE ' + old_address)
            try:
                cursor.execute(admission)
            except Exception:
                cursor.execute(original)
                restore_grants(cursor, old_address, grants)
                raise
            restore_grants(cursor, new_address, grants)
        else:
            cursor.execute(admission.replace('\nRETURNS ', '\nCOPY GRANTS\nRETURNS ', 1))
    return dict(applied=apply, environment=database, procedures=['SP_ORCH_REQUEST_RUN', 'SP_RESOLVE_WORKFLOW_COMMAND'])


def restore_grants(cursor, address, grants):
    for grant in grants:
        if grant['privilege'] == 'USAGE':
            kind = 'DATABASE ROLE' if grant['granted_to'] == 'DATABASE_ROLE' else 'ROLE'
            suffix = ' WITH GRANT OPTION' if str(grant['grant_option']).lower() == 'true' else ''
            cursor.execute(f'GRANT USAGE ON PROCEDURE {address} TO {kind} {quote_role(grant["grantee_name"], grant["granted_to"])}{suffix}')


def run(environment='DEV', apply=False):
    if environment not in ('DEV', 'TST'):
        raise ValueError('Select DEV or TST')
    with connect() as connection, connection.cursor(DictCursor) as cursor:
        cursor.execute("ALTER SESSION SET QUERY_TAG='KUMO_MANUAL_DBT_FULL_REFRESH'")
        return upgrade(cursor, 'KUMO_' + environment, apply)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', choices=('DEV', 'TST'), default='DEV')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(args.environment, args.apply), indent=2))
