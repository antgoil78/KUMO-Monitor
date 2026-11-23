"""Enable completion emails in DEV/TST; read-only preview unless --apply.

Map legacy rules through workflow/job migration identities without altering the
original rows. DEV preserves coverage; TST enables failure emails for every
current workflow using explicit TST recipient settings. Activate future completions.
No emails, business jobs, schedules, tasks or services are run by this migration.
"""
import argparse
import hashlib
import json
import re

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


def build_plan(workflows, jobs, dependencies, notifications, environment='DEV', failure_group=None, email_integration=None):
    if environment not in ('DEV', 'TST'):
        raise ValueError('Select DEV or TST explicitly')
    if environment == 'DEV' and (failure_group or email_integration):
        raise ValueError('DEV restores existing rules only')
    if environment == 'TST' and (not str(failure_group or '').strip() or
            not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*', email_integration or '')):
        raise ValueError('TST requires an explicit failure group and email integration')
    by_id = {}
    for row in notifications:
        if row['WORKFLOW_ID'] in by_id:
            raise ValueError('Duplicate notification settings: ' + row['WORKFLOW_ID'])
        by_id[row['WORKFLOW_ID']] = row
    job_sources = {row['JOB_ID']: row.get('MIGRATED_FROM_WORKFLOW_ID') for row in jobs}
    plan = []
    for workflow in workflows:
        workflow_id, name = workflow['WORKFLOW_ID'], workflow['WORKFLOW_NAME']
        existing = by_id.get(workflow_id)
        if existing and environment == 'DEV':
            continue
        sources = {workflow.get('MIGRATED_FROM_WORKFLOW_ID')}
        sources.update(job_sources.get(row['JOB_ID']) for row in dependencies
                       if row['WORKFLOW_ID'] == workflow_id and row.get('JOB_ID'))
        old = [] if existing else [by_id[source] for source in sorted(source for source in sources if source) if source in by_id]
        if not old and environment == 'DEV':
            continue
        values = dict(WORKFLOW_ID=workflow_id, ON_SUCCESS_EMAIL=False, ON_FAIL_EMAIL=False,
                      SUCCESS_GROUP='', FAIL_GROUP='', EMAIL_INTEGRATION='MY_EMAIL_INT', ENVIRONMENT=environment)
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
        if environment == 'TST':
            # Retain current success rules and any selected failure destination.
            # An unmapped workflow gets the explicitly reviewed TST defaults.
            if existing:
                values.update({key: existing.get(key) for key in FIELDS})
            values.update(ON_FAIL_EMAIL=True, ENVIRONMENT='TST',
                          FAIL_GROUP=values.get('FAIL_GROUP') or failure_group,
                          EMAIL_INTEGRATION=(existing or {}).get('EMAIL_INTEGRATION') or email_integration)
            if existing and all(existing.get(key) == values[key] for key in FIELDS):
                continue
        plan.append(dict(workflow=name, values=values, existing=existing))
    return plan


def upgrade(cursor, apply=False, environment='DEV', failure_group=None, email_integration=None):
    if environment not in ('DEV', 'TST'):
        raise ValueError('Select DEV or TST explicitly')
    APP, META = 'KUMO_' + environment + '.MONITOR_APP', 'KUMO_' + environment + '.MONITOR_META'
    def render(ddl):
        return ddl.replace('KUMO_DEV.', 'KUMO_' + environment + '.')
    def normalized_digest(ddl):
        return digest(ddl.replace('KUMO_' + environment + '.', 'KUMO_DEV.'))
    def query(sql, params=()):
        cursor.execute(sql, params)
        return cursor.fetchall()
    env = next(iter(query(f'CALL {META}.SP_GET_ENVIRONMENT()')[0].values()))
    env = json.loads(env) if isinstance(env, str) else env
    if (env.get('DATABASE'), env.get('META_SCHEMA'), env.get('MONITOR_APP_SCHEMA')) != ('KUMO_' + environment, 'MONITOR_META', 'MONITOR_APP'):
        raise ValueError('Expected ' + environment + ' app routing')
    if env.get('_parameterTable', META + '.APPLICATION_PARAMETERS') != META + '.APPLICATION_PARAMETERS':
        raise ValueError('Review the environment bootstrap parameter table before migration')
    active_records = query(f"SELECT COUNT(*) N FROM {META}.WORKFLOW_EXECUTION_LOG WHERE STATUS IN ('PENDING','INITIATING','QUEUED','CLAIMED','RUNNING')")[0]['N']
    if apply and active_records:
        raise ValueError('Finish pending ' + environment + ' runs before updating notification procedures')
    workflows, jobs, dependencies, notifications = (query(f'SELECT * FROM {META}.{table}') for table in ('WORKFLOW','JOB','DEPENDENCIES','WORKFLOW_NOTIFICATIONS'))
    plan = build_plan(workflows, jobs, dependencies, notifications, environment, failure_group, email_integration)
    groups = {row['GROUP_NAME']: row for row in query(f'SELECT GROUP_NAME,RECIPIENTS FROM {META}.EMAIL_GROUPS')}
    for item in plan:
        for channel in ('FAIL','SUCCESS'):
            values = item['values']
            if values['ON_' + channel + '_EMAIL'] and not str(groups.get(values[channel + '_GROUP'], {}).get('RECIPIENTS') or '').strip():
                raise ValueError('Missing recipients for ' + item['workflow'])
    # Check only integrations used by the changed rules. Never print addresses.
    for integration in sorted({item['values']['EMAIL_INTEGRATION'] for item in plan
                               if item['values']['ON_FAIL_EMAIL'] or item['values']['ON_SUCCESS_EMAIL']}):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*', integration):
            raise ValueError('Invalid email integration name')
        properties = {row['property']: row['property_value'] for row in query('DESC NOTIFICATION INTEGRATION ' + integration)}
        if str(properties.get('ENABLED')).lower() != 'true':
            raise ValueError('Email integration is disabled: ' + integration)
        allowed = set(re.findall(r'[\w.+-]+@[\w.-]+', properties.get('ALLOWED_RECIPIENTS') or '', re.I))
        allowed = {address.lower() for address in allowed}
        if allowed:
            for item in plan:
                if item['values']['EMAIL_INTEGRATION'] != integration:
                    continue
                for channel in ('FAIL', 'SUCCESS'):
                    values = item['values']
                    if values['ON_' + channel + '_EMAIL']:
                        recipients = {address.lower() for address in re.findall(r'[\w.+-]+@[\w.-]+', groups[values[channel + '_GROUP']]['RECIPIENTS'])}
                        if not recipients or not recipients <= allowed:
                            raise ValueError('Recipient group is not allowed by integration: ' + item['workflow'])
    role = query('SELECT CURRENT_ROLE() NAME')[0]['NAME']
    definitions, changes, usage = {}, [], []
    helper_path = next((ROOT / 'snowflake/ddl/KUMO_DEV/MONITOR_META/procedures').glob('SP_GET_ENVIRONMENT__*.sql'))
    helper = render(definition_only(helper_path.read_text()))
    helper_address = META + '.SP_GET_ENVIRONMENT()'
    helper_grants = query('SHOW GRANTS ON PROCEDURE ' + helper_address)
    if not any(row['privilege'] == 'OWNERSHIP' and row['grantee_name'] == role for row in helper_grants):
        raise ValueError('Use the ' + environment + ' environment procedure owner role')
    current_helper = query("SELECT GET_DDL('PROCEDURE',%s,TRUE) DDL", (helper_address,))[0]['DDL']
    if normalized_digest(current_helper) not in (PREVIOUS['SP_GET_ENVIRONMENT'], normalized_digest(helper)):
        raise ValueError('Environment procedure differs from the reviewed version')
    helper_changed = digest(current_helper) != digest(helper)
    for name in NAMES:
        desired = render(definition_only((ROOT / 'snowflake/ddl/KUMO_DEV/MONITOR_APP/procedures' / (name + '.sql')).read_text()))
        definitions[name] = desired
        found = query(f"SHOW PROCEDURES LIKE '{name}' IN SCHEMA {APP}")
        if not found:
            if name != 'SP_ORCH_NOTIFY':
                raise ValueError('Missing ' + environment + ' procedure: ' + name)
            changes.append(name)
            continue
        if len(found) != 1 or found[0]['min_num_arguments'] != 1 or found[0]['max_num_arguments'] != 1:
            raise ValueError('Unexpected procedure signature: ' + name)
        address = APP + '.' + name + '(VARCHAR)'
        grants = query('SHOW GRANTS ON PROCEDURE ' + address)
        if not any(row['privilege'] == 'OWNERSHIP' and row['grantee_name'] == role for row in grants):
            raise ValueError('Use the ' + environment + ' procedure owner role: ' + name)
        if name == 'SP_ORCH_EXECUTE_WORKFLOW':
            usage = [row for row in grants if row['privilege'] == 'USAGE']
            if any(row['granted_to'] not in ('ROLE','DATABASE_ROLE') for row in usage):
                raise ValueError('Unexpected executor grant type')
        current = query("SELECT GET_DDL('PROCEDURE',%s,TRUE) DDL", (address,))[0]['DDL']
        if digest(current) == digest(desired):
            continue
        if normalized_digest(current) != PREVIOUS.get(name):
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
                if item['existing']:
                    cursor.execute(f'UPDATE {META}.WORKFLOW_NOTIFICATIONS SET ' + ','.join(key + '=%s' for key in FIELDS) +
                        ',UPDATED_AT=SYSDATE() WHERE WORKFLOW_ID=%s AND ' + ' AND '.join('EQUAL_NULL(' + key + ',%s)' for key in FIELDS),
                        tuple(values[key] for key in FIELDS) + (values['WORKFLOW_ID'],) + tuple(item['existing'].get(key) for key in FIELDS))
                else:
                    # Recheck identity at write time; never overwrite a new rule.
                    cursor.execute(f'INSERT INTO {META}.WORKFLOW_NOTIFICATIONS (' + ','.join(columns) + ') SELECT ' +
                        ','.join('%s' for _ in columns) + f' WHERE NOT EXISTS (SELECT 1 FROM {META}.WORKFLOW_NOTIFICATIONS WHERE WORKFLOW_ID=%s)',
                        tuple(values[key] for key in columns) + (values['WORKFLOW_ID'],))
                if cursor.rowcount != 1:
                    raise ValueError('Notification settings changed during migration: ' + item['workflow'])
            cursor.execute(f'''MERGE INTO {META}.APPLICATION_PARAMETERS t USING (SELECT 'DEFAULT' INSTANCE_NAME,
                'ORCHESTRATION' PARAMETER_GROUP,'NOTIFICATIONS_START_AT' PARAMETER_KEY) s
                ON t.INSTANCE_NAME=s.INSTANCE_NAME AND t.PARAMETER_GROUP=s.PARAMETER_GROUP AND t.PARAMETER_KEY=s.PARAMETER_KEY
                WHEN NOT MATCHED THEN INSERT (INSTANCE_NAME,PARAMETER_GROUP,PARAMETER_KEY,PARAMETER_VALUE,VALUE_TYPE,DESCRIPTION,IS_SECRET,ACTIVE_FL)
                VALUES (s.INSTANCE_NAME,s.PARAMETER_GROUP,s.PARAMETER_KEY,TO_VARCHAR(SYSDATE(),'YYYY-MM-DD HH24:MI:SS.FF9'),
                'STRING','Notify only workflow completions at or after this UTC timestamp',FALSE,TRUE)''')
            if environment == 'TST':
                coverage = query(f'''SELECT COUNT(*) TOTAL,COUNT(n.WORKFLOW_ID) CONFIGURED,
                    COUNT_IF(n.ON_FAIL_EMAIL AND NULLIF(TRIM(g.RECIPIENTS),'') IS NOT NULL AND n.ENVIRONMENT='TST') ENABLED
                    FROM {META}.WORKFLOW w LEFT JOIN {META}.WORKFLOW_NOTIFICATIONS n ON n.WORKFLOW_ID=w.WORKFLOW_ID
                    LEFT JOIN {META}.EMAIL_GROUPS g ON g.GROUP_NAME=n.FAIL_GROUP''')[0]
                if not (coverage['TOTAL'] == coverage['CONFIGURED'] == coverage['ENABLED'] == len(workflows)):
                    raise ValueError('Not every TST workflow has a valid failure-email rule')
            cursor.execute('COMMIT')
        except Exception:
            cursor.execute('ROLLBACK')
            raise
    return dict(applied=apply, environment=environment, workflow_count=len(workflows), active_execution_records=active_records,
                coverage='all_workflows' if environment == 'TST' else 'existing_rules_only',
                changed_procedures=(['SP_GET_ENVIRONMENT'] if helper_changed else []) + changes,
                migrated_workflows=[dict(workflow=item['workflow'], failure_enabled=item['values']['ON_FAIL_EMAIL'], success_enabled=item['values']['ON_SUCCESS_EMAIL']) for item in plan],
                historical_notifications=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--environment', choices=('DEV', 'TST'), default='DEV')
    parser.add_argument('--failure-group', help='Required TST default recipient group for failure emails')
    parser.add_argument('--email-integration', help='Required TST default Snowflake email integration')
    args = parser.parse_args()
    with connect() as connection, connection.cursor(DictCursor) as cursor:
        cursor.execute("ALTER SESSION SET QUERY_TAG='KUMO_" + args.environment + "_WORKFLOW_NOTIFICATIONS'")
        print(json.dumps(upgrade(cursor, args.apply, args.environment, args.failure_group, args.email_integration), indent=2))
