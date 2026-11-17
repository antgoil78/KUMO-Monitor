"""Install the DEV atomic workflow start gate; read-only unless --apply.

Adds one workflow-relation view and replaces only eligibility, launcher and
executor procedures. No execution data, schedules, tasks or services are changed.
"""
import argparse
import hashlib
import json
import re

from migration_support import ROOT, connect
from apply_workflow_command_templates import definition_only, logical_body
from snowflake.connector import DictCursor

APP, META = 'KUMO_DEV.MONITOR_APP', 'KUMO_DEV.MONITOR_META'
# Install the guard before simplifying the launcher. The new executor also
# accepts the old launcher's CLAIMED records during an upgrade.
NAMES = ('SP_ORCH_EXECUTE_WORKFLOW', 'SP_ORCH_START_RUN', 'SP_ORCH_EVALUATE_DEPENDENCIES')
PREVIOUS = {
    'SP_ORCH_EVALUATE_DEPENDENCIES': '51cf6cdf9eb8dbdb3f44e96fa67f293774a9a4a9378b9547a0f446487e38c1b6',
    'SP_ORCH_START_RUN': 'b8e116e1734e0ce3a26718eb9b599d87d73910fddec8cd5144daf122213b1a9e',
    'SP_ORCH_EXECUTE_WORKFLOW': '64ba04310497964900dcb363c8c94b1f2689e2bac341cc50884d985f67e0df3d',
}
DDL = ROOT / 'snowflake/ddl/KUMO_DEV/MONITOR_APP'
VIEW = APP + '.V_ORCH_WORKFLOW_RELATIONS'


def digest(ddl):
    return hashlib.sha256(logical_body(ddl).strip().encode()).hexdigest()


def view_body(ddl):
    body = re.split(r'\bAS\b', ddl[ddl.upper().index('CREATE OR REPLACE VIEW'):], maxsplit=1, flags=re.I)[1]
    return re.sub(r'\s+', ' ', body.replace('"', '')).strip().rstrip(';').upper()


def upgrade(cursor, apply=False):
    def query(sql, params=()):
        cursor.execute(sql, params)
        return cursor.fetchall()
    env = next(iter(query(f'CALL {META}.SP_GET_ENVIRONMENT()')[0].values()))
    env = json.loads(env) if isinstance(env, str) else env
    if (env.get('DATABASE'), env.get('META_SCHEMA'), env.get('MONITOR_APP_SCHEMA')) != ('KUMO_DEV', 'MONITOR_META', 'MONITOR_APP'):
        raise ValueError('Expected DEV app routing')
    if query(f"SELECT COUNT(*) N FROM {META}.WORKFLOW_EXECUTION_LOG WHERE JOB_ID IS NULL AND STATUS IN ('PENDING','INITIATING','QUEUED','CLAIMED','RUNNING')")[0]['N']:
        raise ValueError('Finish pending DEV runs before updating the start gate')
    role = query('SELECT CURRENT_ROLE() NAME')[0]['NAME']
    desired, usage = {}, set()
    for name in NAMES:
        address = APP + '.' + name + '(VARCHAR)'
        grants = query('SHOW GRANTS ON PROCEDURE ' + address)
        if not any(row['privilege'] == 'OWNERSHIP' and row['grantee_name'] == role for row in grants):
            raise ValueError('Use the DEV procedure owner role: ' + name)
        for grant in grants:
            if grant['privilege'] == 'USAGE':
                if grant['granted_to'] not in ('ROLE', 'DATABASE_ROLE'):
                    raise ValueError('Unexpected grant type: ' + name)
                usage.add((grant['granted_to'], grant['grantee_name']))
        desired[name] = definition_only((DDL / 'procedures' / (name + '.sql')).read_text())
        live = query("SELECT GET_DDL('PROCEDURE',%s,TRUE) DDL", (address,))[0]['DDL']
        if digest(live) not in (PREVIOUS[name], digest(desired[name])):
            raise ValueError('Live procedure differs from the maintained version: ' + name)
    view = (DDL / 'views/V_ORCH_WORKFLOW_RELATIONS.sql').read_text()
    view = view[view.index('CREATE OR REPLACE VIEW'):]
    existing = query(f"SHOW VIEWS LIKE 'V_ORCH_WORKFLOW_RELATIONS' IN SCHEMA {APP}")
    if existing:
        if len(existing) != 1:
            raise ValueError('Unexpected relation views')
        grants = query('SHOW GRANTS ON VIEW ' + VIEW)
        if not any(row['privilege'] == 'OWNERSHIP' and row['grantee_name'] == role for row in grants):
            raise ValueError('Use the DEV relation view owner role')
        live = query("SELECT GET_DDL('VIEW',%s,TRUE) DDL", (VIEW,))[0]['DDL']
        if view_body(live) != view_body(view):
            raise ValueError('Live relation view differs from source')
    if apply:
        if not existing:
            cursor.execute(view)
        for kind, name in sorted(usage):
            label = 'DATABASE ROLE' if kind == 'DATABASE_ROLE' else 'ROLE'
            parts = name.split('.') if kind == 'DATABASE_ROLE' else [name]
            grantee = '.'.join('"' + part.replace('"', '""') + '"' for part in parts)
            cursor.execute(f'GRANT SELECT ON VIEW {VIEW} TO {label} {grantee}')
        live = query("SELECT GET_DDL('VIEW',%s,TRUE) DDL", (VIEW,))[0]['DDL']
        if view_body(live) != view_body(view):
            raise RuntimeError('Deployed relation view does not match source')
        for name in NAMES:
            cursor.execute(desired[name].replace('\nRETURNS ', '\nCOPY GRANTS\nRETURNS ', 1))
            live = query("SELECT GET_DDL('PROCEDURE',%s,TRUE) DDL", (APP + '.' + name + '(VARCHAR)',))[0]['DDL']
            if digest(live) != digest(desired[name]):
                raise RuntimeError('Deployed procedure does not match source: ' + name)
    return dict(applied=apply, environment='DEV', procedures=list(NAMES), view=VIEW,
                claim_owner='executor', activity_levels=['parent', 'grandparent'],
                trigger_sources='all', queued_priority='upstream')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with connect() as connection, connection.cursor(DictCursor) as cursor:
        print(json.dumps(upgrade(cursor, args.apply), indent=2))
