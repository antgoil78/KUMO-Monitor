"""Restore completion emails in DEV only; read-only preview unless --apply.

Map legacy rules through workflow/job migration identities without altering the
original rows. Preserve existing new rules. Activate only future completions.
No emails, business jobs, schedules, tasks or services are run by this migration.
"""
import argparse
import hashlib
import json

from migration_support import ROOT, connect
from apply_workflow_command_templates import definition_only, logical_body
from snowflake.connector import DictCursor

APP, META = 'KUMO_DEV.MONITOR_APP', 'KUMO_DEV.MONITOR_META'
NAMES = ('SP_ORCH_NOTIFY', 'SP_ORCH_EXECUTE_WORKFLOW', 'SP_ORCH_RECONCILE')
PREVIOUS = {
    'SP_GET_ENVIRONMENT': 'bcdeb472d9af45cbbc50e797737d30e8e27e26829a32743bc9eb05c03d3cdcaf',
    'SP_ORCH_EXECUTE_WORKFLOW': 'ff23605fba52aac809c790ac810a02e794768f159fc17f6d8f8c4abd60b664bc',
    'SP_ORCH_RECONCILE': 'c56fa3dba7a524e938162e1012a9aca5cbd8520948369963d91ff7485c0077f7',
}
FIELDS = ('ON_SUCCESS_EMAIL', 'ON_FAIL_EMAIL', 'SUCCESS_GROUP', 'FAIL_GROUP', 'EMAIL_INTEGRATION', 'ENVIRONMENT')


def digest(ddl):
    return hashlib.sha256(logical_body(ddl).strip().encode()).hexdigest()


def build_plan(workflows, jobs, dependencies, notifications):
    by_id = {}
    for row in notifications:
        if row['WORKFLOW_ID'] in by_id:
            raise ValueError('Duplicate notification settings: ' + row['WORKFLOW_ID'])
        by_id[row['WORKFLOW_ID']] = row
    job_sources = {row['JOB_ID']: row.get('MIGRATED_FROM_WORKFLOW_ID') for row in jobs}
    plan = []
    for workflow in workflows:
        workflow_id, name = workflow['WORKFLOW_ID'], workflow['WORKFLOW_NAME']
        if workflow_id in by_id:
            continue
        sources = {workflow.get('MIGRATED_FROM_WORKFLOW_ID')}
        sources.update(job_sources.get(row['JOB_ID']) for row in dependencies
                       if row['WORKFLOW_ID'] == workflow_id and row.get('JOB_ID'))
        old = [by_id[source] for source in sorted(source for source in sources if source) if source in by_id]
        if not old:
            continue
        values = dict(WORKFLOW_ID=workflow_id, ON_SUCCESS_EMAIL=False, ON_FAIL_EMAIL=False,
                      SUCCESS_GROUP='', FAIL_GROUP='', EMAIL_INTEGRATION='MY_EMAIL_INT', ENVIRONMENT='DEV')
        active = []
        for channel in ('SUCCESS', 'FAIL'):
            rules = [row for row in old if row.get('ON_' + channel + '_EMAIL') and str(row.get(channel + '_GROUP') or '').strip()]
            groups = {row[channel + '_GROUP'] for row in rules}
            if len(groups) > 1:
                raise ValueError('Conflicting ' + channel.lower() + ' groups for ' + name)
            if groups:
                values['ON_' + channel + '_EMAIL'] = True
                values[channel + '_GROUP'] = next(iter(groups))
                active.extend(rules)
        integrations = {row.get('EMAIL_INTEGRATION') or 'MY_EMAIL_INT' for row in active}
        if len(integrations) > 1:
            raise ValueError('Conflicting email integrations for ' + name)
        if integrations:
            values['EMAIL_INTEGRATION'] = next(iter(integrations))
        plan.append(dict(workflow=name, values=values))
    return plan


def upgrade(cursor, apply=False):
    def query(sql, params=()):
        cursor.execute(sql, params)
        return cursor.fetchall()
    env = next(iter(query(f'CALL {META}.SP_GET_ENVIRONMENT()')[0].values()))
    env = json.loads(env) if isinstance(env, str) else env
    if (env.get('DATABASE'), env.get('META_SCHEMA'), env.get('MONITOR_APP_SCHEMA')) != ('KUMO_DEV', 'MONITOR_META', 'MONITOR_APP'):
        raise ValueError('Expected DEV app routing')
    if query(f"SELECT COUNT(*) N FROM {META}.WORKFLOW_EXECUTION_LOG WHERE JOB_ID IS NULL AND STATUS IN ('QUEUED','CLAIMED','RUNNING')")[0]['N']:
        raise ValueError('Finish pending DEV runs before updating notification procedures')
    plan = build_plan(*(query(f'SELECT * FROM {META}.{table}') for table in ('WORKFLOW','JOB','DEPENDENCIES','WORKFLOW_NOTIFICATIONS')))
    groups = {row['GROUP_NAME']: row for row in query(f'SELECT GROUP_NAME,RECIPIENTS FROM {META}.EMAIL_GROUPS')}
    for item in plan:
        for channel in ('FAIL','SUCCESS'):
            values = item['values']
            if values['ON_' + channel + '_EMAIL'] and not str(groups.get(values[channel + '_GROUP'], {}).get('RECIPIENTS') or '').strip():
                raise ValueError('Missing recipients for ' + item['workflow'])
    role = query('SELECT CURRENT_ROLE() NAME')[0]['NAME']
    definitions, changes, usage = {}, [], []
    helper_path = next((ROOT / 'snowflake/ddl/KUMO_DEV/MONITOR_META/procedures').glob('SP_GET_ENVIRONMENT__*.sql'))
    helper = definition_only(helper_path.read_text())
    helper_address = META + '.SP_GET_ENVIRONMENT()'
    helper_grants = query('SHOW GRANTS ON PROCEDURE ' + helper_address)
    if not any(row['privilege'] == 'OWNERSHIP' and row['grantee_name'] == role for row in helper_grants):
        raise ValueError('Use the DEV environment procedure owner role')
    current_helper = query("SELECT GET_DDL('PROCEDURE',%s,TRUE) DDL", (helper_address,))[0]['DDL']
    if digest(current_helper) not in (PREVIOUS['SP_GET_ENVIRONMENT'], digest(helper)):
        raise ValueError('Environment procedure differs from the reviewed version')
    helper_changed = digest(current_helper) != digest(helper)
    for name in NAMES:
        desired = definition_only((ROOT / 'snowflake/ddl/KUMO_DEV/MONITOR_APP/procedures' / (name + '.sql')).read_text())
        definitions[name] = desired
        found = query(f"SHOW PROCEDURES LIKE '{name}' IN SCHEMA {APP}")
        if not found:
            if name != 'SP_ORCH_NOTIFY':
                raise ValueError('Missing DEV procedure: ' + name)
            changes.append(name)
            continue
        if len(found) != 1 or found[0]['min_num_arguments'] != 1 or found[0]['max_num_arguments'] != 1:
            raise ValueError('Unexpected procedure signature: ' + name)
        address = APP + '.' + name + '(VARCHAR)'
        grants = query('SHOW GRANTS ON PROCEDURE ' + address)
        if not any(row['privilege'] == 'OWNERSHIP' and row['grantee_name'] == role for row in grants):
            raise ValueError('Use the DEV procedure owner role: ' + name)
        if name == 'SP_ORCH_EXECUTE_WORKFLOW':
            usage = [row for row in grants if row['privilege'] == 'USAGE']
            if any(row['granted_to'] not in ('ROLE','DATABASE_ROLE') for row in usage):
                raise ValueError('Unexpected executor grant type')
        current = query("SELECT GET_DDL('PROCEDURE',%s,TRUE) DDL", (address,))[0]['DDL']
        if digest(current) == digest(desired):
            continue
        if digest(current) != PREVIOUS.get(name):
            raise ValueError('Live procedure differs from the reviewed version: ' + name)
        changes.append(name)
    if apply:
        # COPY GRANTS preserves permissions; the helper is installed first.
        if helper_changed:
            cursor.execute(helper.replace('\nRETURNS ', '\nCOPY GRANTS\nRETURNS ', 1))
            actual = query("SELECT GET_DDL('PROCEDURE',%s,TRUE) DDL", (helper_address,))[0]['DDL']
            if digest(actual) != digest(helper):
                raise RuntimeError('Deployed environment procedure differs')
        for name in changes:
            cursor.execute(definitions[name].replace('\nRETURNS ', '\nCOPY GRANTS\nRETURNS ', 1))
            actual = query("SELECT GET_DDL('PROCEDURE',%s,TRUE) DDL", (APP + '.' + name + '(VARCHAR)',))[0]['DDL']
            if digest(actual) != digest(definitions[name]):
                raise RuntimeError('Deployed definition differs: ' + name)
        for grant in usage:
            kind = 'DATABASE ROLE' if grant['granted_to'] == 'DATABASE_ROLE' else 'ROLE'
            parts = grant['grantee_name'].split('.') if kind == 'DATABASE ROLE' else [grant['grantee_name']]
            grantee = '.'.join('"' + part.replace('"','""') + '"' for part in parts)
            cursor.execute(f'GRANT USAGE ON PROCEDURE {APP}.SP_ORCH_NOTIFY(VARCHAR) TO {kind} {grantee}')
        cursor.execute('BEGIN')
        try:
            for item in plan:
                values = item['values']
                columns = ('WORKFLOW_ID',) + FIELDS
                # Recheck identity at write time and never overwrite a new rule.
                cursor.execute(f'INSERT INTO {META}.WORKFLOW_NOTIFICATIONS (' + ','.join(columns) + ') SELECT ' +
                    ','.join('%s' for _ in columns) + f' WHERE NOT EXISTS (SELECT 1 FROM {META}.WORKFLOW_NOTIFICATIONS WHERE WORKFLOW_ID=%s)',
                    tuple(values[key] for key in columns) + (values['WORKFLOW_ID'],))
            cursor.execute(f'''MERGE INTO {META}.APPLICATION_PARAMETERS t USING (SELECT 'DEFAULT' INSTANCE_NAME,
                'ORCHESTRATION' PARAMETER_GROUP,'NOTIFICATIONS_START_AT' PARAMETER_KEY) s
                ON t.INSTANCE_NAME=s.INSTANCE_NAME AND t.PARAMETER_GROUP=s.PARAMETER_GROUP AND t.PARAMETER_KEY=s.PARAMETER_KEY
                WHEN NOT MATCHED THEN INSERT (INSTANCE_NAME,PARAMETER_GROUP,PARAMETER_KEY,PARAMETER_VALUE,VALUE_TYPE,DESCRIPTION,IS_SECRET,ACTIVE_FL)
                VALUES (s.INSTANCE_NAME,s.PARAMETER_GROUP,s.PARAMETER_KEY,TO_VARCHAR(SYSDATE(),'YYYY-MM-DD HH24:MI:SS.FF9'),
                'STRING','Notify only workflow completions at or after this UTC timestamp',FALSE,TRUE)''')
            cursor.execute('COMMIT')
        except Exception:
            cursor.execute('ROLLBACK')
            raise
    return dict(applied=apply, environment='DEV', changed_procedures=(['SP_GET_ENVIRONMENT'] if helper_changed else []) + changes,
                migrated_workflows=[dict(workflow=item['workflow'], failure_enabled=item['values']['ON_FAIL_EMAIL'], success_enabled=item['values']['ON_SUCCESS_EMAIL']) for item in plan],
                historical_notifications=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with connect() as connection, connection.cursor(DictCursor) as cursor:
        cursor.execute("ALTER SESSION SET QUERY_TAG='KUMO_DEV_WORKFLOW_NOTIFICATIONS'")
        print(json.dumps(upgrade(cursor, args.apply), indent=2))
