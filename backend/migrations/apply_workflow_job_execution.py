"""Add DEV workflow-wide Sequential/Parallel jobs. Read-only unless --apply."""
import argparse
import hashlib
import json

from migration_support import ROOT, connect
from apply_workflow_command_templates import definition_only, logical_body
from snowflake.connector import DictCursor

APP, META = 'KUMO_DEV.MONITOR_APP', 'KUMO_DEV.MONITOR_META'
DDL = ROOT / 'snowflake/ddl/KUMO_DEV/MONITOR_APP/procedures'
PROCEDURES = {
    'SP_ORCH_EXECUTE_JOB': '(VARCHAR,VARCHAR)',
    'SP_ORCH_EXECUTE_WORKFLOW': '(VARCHAR)',
    'SP_ORCH_REQUEST_RUN': '(VARCHAR,VARCHAR,VARCHAR,VARCHAR,VARCHAR,BOOLEAN,VARCHAR)',
}
PREVIOUS = {
    'SP_ORCH_EXECUTE_WORKFLOW': ('b8efd90e998015bf4e8c4d0fbd7ab269e5e44a1dad46ac29349a4c157f40f643',
                                 'd1dd3790daf017ad48a7fb4579492ff4092f37f2df14d47b3edc8b960c3cac49'),
    'SP_ORCH_REQUEST_RUN': ('a2441b4f2c033f307d3b1009b478a1391b8ced50e12709cbed575032d418713b',),
}


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
        raise ValueError('Finish pending DEV runs before updating execution procedures')
    role = query('SELECT CURRENT_ROLE() NAME')[0]['NAME']
    columns = query(f"SHOW COLUMNS LIKE 'JOB_EXECUTION_MODE' IN TABLE {META}.WORKFLOW")
    if columns and (len(columns) != 1 or json.loads(columns[0]['data_type']).get('type') != 'TEXT' or
                    columns[0]['default'] != "'SEQUENTIAL'" or columns[0]['null?'] != 'NOT_NULL'):
        raise ValueError('Unexpected JOB_EXECUTION_MODE column definition')
    grants = query(f'SHOW GRANTS ON TABLE {META}.WORKFLOW')
    if not any(row['privilege'] == 'OWNERSHIP' and row['grantee_name'] == role for row in grants):
        raise ValueError('Use the DEV workflow table owner role')
    desired, recipients = {}, set()
    for name, signature in PROCEDURES.items():
        desired[name] = definition_only((DDL / (name + '.sql')).read_text())
        found = query(f"SHOW PROCEDURES LIKE '{name}' IN SCHEMA {APP}")
        if not found:
            if name in PREVIOUS:
                raise ValueError('Expected the installed DEV procedure: ' + name)
            continue
        if len(found) != 1:
            raise ValueError('Ambiguous procedure: ' + name)
        address = APP + '.' + name + signature
        grants = query('SHOW GRANTS ON PROCEDURE ' + address)
        if not any(row['privilege'] == 'OWNERSHIP' and row['grantee_name'] == role for row in grants):
            raise ValueError('Use the DEV procedure owner role: ' + name)
        if name == 'SP_ORCH_EXECUTE_WORKFLOW':
            for grant in grants:
                if grant['privilege'] == 'USAGE':
                    if grant['granted_to'] not in ('ROLE', 'DATABASE_ROLE'):
                        raise ValueError('Unexpected executor grant type')
                    recipients.add((grant['granted_to'], grant['grantee_name']))
        live = query("SELECT GET_DDL('PROCEDURE',%s,TRUE) DDL", (address,))[0]['DDL']
        if digest(live) not in (*PREVIOUS.get(name, ()), digest(desired[name])):
            raise ValueError('Live procedure differs from the maintained version: ' + name)
    if apply:
        if not columns:
            cursor.execute(f"ALTER TABLE {META}.WORKFLOW ADD COLUMN JOB_EXECUTION_MODE VARCHAR(20) NOT NULL DEFAULT 'SEQUENTIAL'")
        for name, signature in PROCEDURES.items():
            cursor.execute(desired[name].replace('\nRETURNS ', '\nCOPY GRANTS\nRETURNS ', 1))
            live = query("SELECT GET_DDL('PROCEDURE',%s,TRUE) DDL", (APP + '.' + name + signature,))[0]['DDL']
            if digest(live) != digest(desired[name]):
                raise RuntimeError('Deployed procedure does not match source: ' + name)
        for kind, name in sorted(recipients):
            label = 'DATABASE ROLE' if kind == 'DATABASE_ROLE' else 'ROLE'
            grantee = '.'.join('"' + part.replace('"', '""') + '"' for part in (name.split('.') if kind == 'DATABASE_ROLE' else [name]))
            cursor.execute(f'GRANT USAGE ON PROCEDURE {APP}.SP_ORCH_EXECUTE_JOB(VARCHAR,VARCHAR) TO {label} {grantee}')
    return dict(applied=apply, environment='DEV', column='WORKFLOW.JOB_EXECUTION_MODE', default='SEQUENTIAL',
                procedures=list(PROCEDURES), options=['SEQUENTIAL', 'PARALLEL'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with connect() as connection, connection.cursor(DictCursor) as cursor:
        print(json.dumps(upgrade(cursor, args.apply), indent=2))
