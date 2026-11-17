"""Check active parents and grandparents in DEV; read-only unless --apply.

Replaces only SP_ORCH_EVALUATE_DEPENDENCIES with COPY GRANTS. No configuration,
execution data, tasks, schedules, services or other environments are changed.
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
PREVIOUS = 'a67b2b078ad82d91cf5f385805d70b9abdc442212304bd2fda12a2169249cd99'


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
    if query(f"SELECT COUNT(*) N FROM {META}.WORKFLOW_EXECUTION_LOG WHERE JOB_ID IS NULL AND STATUS IN ('PENDING','INITIATING','QUEUED','CLAIMED','RUNNING')")[0]['N']:
        raise ValueError('Finish pending DEV runs before updating dependency evaluation')
    role = query('SELECT CURRENT_ROLE() NAME')[0]['NAME']
    grants = query('SHOW GRANTS ON PROCEDURE ' + ADDRESS)
    if not any(row['privilege'] == 'OWNERSHIP' and row['grantee_name'] == role for row in grants):
        raise ValueError('Use the DEV evaluator owner role')
    desired = definition_only(SOURCE.read_text())
    live = query("SELECT GET_DDL('PROCEDURE',%s,TRUE) DDL", (ADDRESS,))[0]['DDL']
    if digest(live) not in (PREVIOUS, digest(desired)):
        raise ValueError('Live evaluator differs from the maintained version')
    if apply:
        cursor.execute(desired.replace('\nRETURNS ', '\nCOPY GRANTS\nRETURNS ', 1))
        deployed = query("SELECT GET_DDL('PROCEDURE',%s,TRUE) DDL", (ADDRESS,))[0]['DDL']
        if digest(deployed) != digest(desired):
            raise RuntimeError('Deployed evaluator does not match source')
    return dict(applied=apply, environment='DEV', procedure=ADDRESS,
                activity_levels=['parent', 'grandparent'], recheck_on='parent or grandparent completion')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with connect() as connection, connection.cursor(DictCursor) as cursor:
        print(json.dumps(upgrade(cursor, args.apply), indent=2))
