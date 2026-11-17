"""Add configurable parent freshness in DEV; read-only unless --apply.

Only DEPENDENCIES and SP_ORCH_EVALUATE_DEPENDENCIES are changed. Existing
workflow DEP rules gain freshness when the column is first installed. Job
assignments and later explicit choices are preserved on subsequent runs.
"""
import argparse
import hashlib
import json

from migration_support import ROOT, connect
from apply_workflow_command_templates import definition_only, logical_body
from snowflake.connector import DictCursor

META = 'KUMO_DEV.MONITOR_META'
ADDRESS = 'KUMO_DEV.MONITOR_APP.SP_ORCH_EVALUATE_DEPENDENCIES(VARCHAR)'
SOURCE = ROOT / 'snowflake/ddl/KUMO_DEV/MONITOR_APP/procedures/SP_ORCH_EVALUATE_DEPENDENCIES.sql'
PREVIOUS = '37b53ed378439e0c1c3f20b5ebcb477b6c2eb269992c502c2e27440f5b330e14'


def digest(ddl):
    return hashlib.sha256(logical_body(ddl).strip().encode()).hexdigest()


def upgrade(cursor, apply=False):
    def query(sql, params=()):
        cursor.execute(sql, params)
        return cursor.fetchall()
    env = next(iter(query(f'CALL {META}.SP_GET_ENVIRONMENT()')[0].values()))
    env = json.loads(env) if isinstance(env, str) else env
    if (env.get('DATABASE'), env.get('META_SCHEMA'), env.get('MONITOR_APP_SCHEMA')) != ('KUMO_DEV', 'MONITOR_META', 'MONITOR_APP'):
        raise ValueError('Expected DEV app routing')
    if query(f"SELECT COUNT(*) N FROM {META}.WORKFLOW_EXECUTION_LOG WHERE JOB_ID IS NULL AND STATUS IN ('QUEUED','CLAIMED','RUNNING')")[0]['N']:
        raise ValueError('Finish pending DEV runs before updating dependency rules')
    role = query('SELECT CURRENT_ROLE() NAME')[0]['NAME']
    for kind, address in [('TABLE', META + '.DEPENDENCIES'), ('PROCEDURE', ADDRESS)]:
        grants = query(f'SHOW GRANTS ON {kind} {address}')
        if not any(row['privilege'] == 'OWNERSHIP' and row['grantee_name'] == role for row in grants):
            raise ValueError('Use the DEV object owner role: ' + address)
    columns = query(f'DESCRIBE TABLE {META}.DEPENDENCIES')
    column = next((row for row in columns if row['name'] == 'PARENT_MUST_BE_NEWER'), None)
    if column and (column['type'] != 'BOOLEAN' or column['null?'] != 'N' or str(column['default']).upper() != 'FALSE'):
        raise ValueError('Unexpected parent freshness column definition')
    desired = definition_only(SOURCE.read_text())
    live = query("SELECT GET_DDL('PROCEDURE',%s,TRUE) DDL", (ADDRESS,))[0]['DDL']
    if digest(live) not in (PREVIOUS, digest(desired)):
        raise ValueError('Live evaluator differs from the maintained version')
    if apply:
        if not column:
            cursor.execute(f'ALTER TABLE {META}.DEPENDENCIES ADD COLUMN PARENT_MUST_BE_NEWER BOOLEAN DEFAULT FALSE NOT NULL')
            cursor.execute(f"UPDATE {META}.DEPENDENCIES SET PARENT_MUST_BE_NEWER = TRUE WHERE JOB_ID IS NULL AND PARENT_ID IS NOT NULL AND TRIGGER_TYPE = 'DEP'")
        cursor.execute(desired.replace('\nRETURNS ', '\nCOPY GRANTS\nRETURNS ', 1))
        deployed = query("SELECT GET_DDL('PROCEDURE',%s,TRUE) DDL", (ADDRESS,))[0]['DDL']
        if digest(deployed) != digest(desired):
            raise RuntimeError('Deployed evaluator does not match source')
    return dict(applied=apply, environment='DEV', column='PARENT_MUST_BE_NEWER',
                comparison='parent.ENDED_AT > MAX(child.STARTED_AT)', first_run='no freshness cutoff',
                enable_existing_workflow_rules=not bool(column))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with connect() as connection, connection.cursor(DictCursor) as cursor:
        print(json.dumps(upgrade(cursor, args.apply), indent=2))
