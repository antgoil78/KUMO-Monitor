"""Incrementally promote the approved DEV workflow engine to TST.

Read-only by default. --apply pauses TST admissions and its started engine
tasks, adds two columns and installs only changed procedures/the relation view.
It preserves the TST catalog and leaves the app/tasks paused for image promotion.
Restore only the task states recorded in --report after the new app is ready.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re

from migration_support import ROOT, connect
from apply_parallel_engine import PROCEDURES, require_pre_stream_release
from apply_workflow_command_templates import definition_only, logical_body
from apply_manual_dbt_full_refresh import quote_role, restore_grants
from apply_atomic_workflow_start import view_body
from snowflake.connector import DictCursor

APP, META = 'KUMO_TST.MONITOR_APP', 'KUMO_TST.MONITOR_META'
SERVICE = APP + '.KUMO_MONITOR_SERVICE'
DDL = ROOT / 'snowflake/ddl/KUMO_DEV'
# Logical bodies from the previously approved 78ba4fcb2425 release, rendered
# with TST namespaces. Unknown live changes require review, never replacement.
PREVIOUS = {
    'SP_RESOLVE_WORKFLOW_COMMAND': '255c7a13110401cdf67e9b548e80cd03d460ba2824bddf03e75090c70e33feac',
    'SP_ORCH_REQUEST_RUN': 'bd649a549316c860d03fa8c58128ea3aef30de50b8187a3ab1ffdb34e13a69ae',
    'SP_ORCH_EXECUTE_WORKFLOW': 'a2da781b24b3182a04947e1e4d92a71228d903e5e5b07ce3abc33e992b1128cc',
    'SP_ORCH_RECONCILE': '9e38163bd8ed8f7740616569809a1ae7366922f33fddcac14baa0fa71dea717a',
    'SP_ORCH_START_RUN': 'cdf7d4b320063c566d385f1573c950f8b22ad43267ac5798c638edfd48b44756',
}
NEW = {'SP_ORCH_EVALUATE_DEPENDENCIES', 'SP_ORCH_DISPATCH_DEPENDENCIES', 'SP_ORCH_EXECUTE_JOB'}


def digest(ddl):
    return hashlib.sha256(logical_body(ddl).strip().encode()).hexdigest()


def query(cursor, sql, params=()):
    cursor.execute(sql, params)
    return cursor.fetchall()


def check_idle(cursor):
    active = query(cursor, f"SELECT COUNT(*) N FROM {META}.WORKFLOW_EXECUTION_LOG "
                   "WHERE STATUS IN ('PENDING','INITIATING','QUEUED','CLAIMED','RUNNING')")[0]['N']
    if active:
        raise ValueError('Finish pending TST executions before promotion')
    tasks = query(cursor, "SELECT NAME,QUERY_ID FROM TABLE(KUMO_TST.INFORMATION_SCHEMA.TASK_HISTORY("
                  "SCHEDULED_TIME_RANGE_START=>DATEADD('day',-1,CURRENT_TIMESTAMP()),RESULT_LIMIT=>10000)) "
                  "WHERE DATABASE_NAME='KUMO_TST' AND SCHEMA_NAME='MONITOR_APP' AND STATE='EXECUTING' "
                  "AND (NAME LIKE 'TASK_ORCH_%' OR NAME LIKE 'TMP_ORCH_%')")
    # TASK_HISTORY can briefly lag a completed query. Consult its actual status.
    if any(cursor.connection.is_still_running(cursor.connection.get_query_status(row['QUERY_ID'])) for row in tasks):
        raise ValueError('A TST engine task is executing; let it finish')


def grants_for(cursor, kind, address):
    grants = query(cursor, f'SHOW GRANTS ON {kind} {address}')
    if not any(g['privilege'] == 'OWNERSHIP' and g['grantee_name'] == 'KUMO_ADMIN'
               and g['granted_to'] == 'ROLE' for g in grants):
        raise ValueError('Expected KUMO_ADMIN ownership: ' + address)
    if any(g['granted_to'] not in ('ROLE', 'DATABASE_ROLE') for g in grants):
        raise ValueError('Unexpected grants: ' + address)
    return grants


def preflight(cursor):
    require_pre_stream_release(cursor)
    env = next(iter(query(cursor, f'CALL {META}.SP_GET_ENVIRONMENT()')[0].values()))
    env = json.loads(env) if isinstance(env, str) else env
    if any(env.get(k) != v for k, v in [('DATABASE', 'KUMO_TST'),
            ('META_SCHEMA', 'MONITOR_META'), ('MONITOR_APP_SCHEMA', 'MONITOR_APP')]):
        raise ValueError('Expected TST routing')
    check_idle(cursor)
    columns = {}
    for table, name, expected_type, default in [('WORKFLOW', 'JOB_EXECUTION_MODE', 'VARCHAR(20)', "'SEQUENTIAL'"),
            ('DEPENDENCIES', 'PARENT_MUST_BE_NEWER', 'BOOLEAN', 'FALSE')]:
        grants_for(cursor, 'TABLE', META + '.' + table)
        rows = query(cursor, f'DESCRIBE TABLE {META}.{table}')
        column = next((r for r in rows if r['name'] == name), None)
        if column and (column['type'] != expected_type or column['null?'] != 'N'
                       or str(column['default']).upper() != default):
            raise ValueError('Unexpected column definition: ' + name)
        columns[name] = dict(table=table, exists=bool(column))
    folder = DDL / 'MONITOR_APP/procedures'
    definitions = {'SP_RESOLVE_WORKFLOW_COMMAND': (
        next(folder.glob('SP_RESOLVE_WORKFLOW_COMMAND__*.sql')), '(VARIANT,VARCHAR)')}
    definitions.update({name: (folder / (name + '.sql'), signature) for name, signature in PROCEDURES.items()})
    procedures, usage, dev_differences = {}, set(), []
    for name, (path, signature) in definitions.items():
        source = definition_only(path.read_text())
        desired = source.replace('KUMO_DEV.', 'KUMO_TST.')
        dev = query(cursor, "SELECT GET_DDL('PROCEDURE',%s,TRUE) DDL",
                    ('KUMO_DEV.MONITOR_APP.' + name + signature,))[0]['DDL']
        if digest(dev) != digest(source):
            # DEV's resolver still uses the approved pre-full-refresh body.
            # Promote the merged release's full-refresh support to TST, without
            # changing DEV or permitting any unrelated live source drift.
            if name != 'SP_RESOLVE_WORKFLOW_COMMAND' or digest(dev.replace('KUMO_DEV.', 'KUMO_TST.')) != PREVIOUS[name]:
                raise ValueError('Saved definition differs from approved DEV: ' + name)
            dev_differences.append(name)
        rows = query(cursor, f"SHOW PROCEDURES LIKE '{name}' IN SCHEMA {APP}")
        old, grants, old_address = None, [], None
        if rows:
            if len(rows) != 1:
                raise ValueError('Unexpected procedure overloads: ' + name)
            count = rows[0]['max_num_arguments']
            old_signature = signature
            if name == 'SP_ORCH_REQUEST_RUN':
                # SHOW reports min=max on some accounts even with DEFAULT
                # arguments; inspect the actual optional-argument markers too.
                arguments = str(rows[0].get('arguments', '')).upper()
                if (count not in (5, 6, 7) or rows[0]['min_num_arguments'] not in (5, count)
                        or (count >= 6 and 'DEFAULT BOOLEAN' not in arguments)
                        or (count == 7 and 'DEFAULT VARCHAR' not in arguments)):
                    raise ValueError('Unexpected admission signature')
                old_signature = '(VARCHAR,VARCHAR,VARCHAR,VARCHAR,VARCHAR' + (',BOOLEAN' if count >= 6 else '') + (',VARCHAR' if count >= 7 else '') + ')'
            elif count != signature.count(',') + 1:
                raise ValueError('Unexpected procedure signature: ' + name)
            old_address = APP + '.' + name + old_signature
            grants = grants_for(cursor, 'PROCEDURE', old_address)
            usage.update((g['granted_to'], g['grantee_name']) for g in grants if g['privilege'] == 'USAGE')
            old = query(cursor, "SELECT GET_DDL('PROCEDURE',%s,TRUE) DDL", (old_address,))[0]['DDL']
            if digest(old) not in (PREVIOUS.get(name), digest(desired)):
                raise ValueError('Unknown TST procedure drift: ' + name)
        elif name not in NEW:
            raise ValueError('Missing existing TST procedure: ' + name)
        procedures[name] = dict(desired=desired, address=APP + '.' + name + signature,
            old=old, old_address=old_address, grants=grants,
            changed=old is None or digest(old) != digest(desired))
    # The environment bootstrap is retained; it must already match this release.
    helper = next((DDL / 'MONITOR_META/procedures').glob('SP_GET_ENVIRONMENT__*.sql')).read_text()
    live = query(cursor, "SELECT GET_DDL('PROCEDURE',%s,TRUE) DDL", (META + '.SP_GET_ENVIRONMENT()',))[0]['DDL']
    if digest(live) != digest(helper.replace('KUMO_DEV.', 'KUMO_TST.')):
        raise ValueError('Review TST environment bootstrap drift')
    view = (DDL / 'MONITOR_APP/views/V_ORCH_WORKFLOW_RELATIONS.sql').read_text()
    view = view[view.index('CREATE OR REPLACE VIEW'):].strip().rstrip(';')
    dev = query(cursor, "SELECT GET_DDL('VIEW',%s,TRUE) DDL",
                ('KUMO_DEV.MONITOR_APP.V_ORCH_WORKFLOW_RELATIONS',))[0]['DDL']
    if view_body(dev) != view_body(view):
        raise ValueError('Saved relation view differs from DEV')
    view = view.replace('KUMO_DEV.', 'KUMO_TST.')
    view_exists = bool(query(cursor, f"SHOW VIEWS LIKE 'V_ORCH_WORKFLOW_RELATIONS' IN SCHEMA {APP}"))
    if view_exists:
        grants_for(cursor, 'VIEW', APP + '.V_ORCH_WORKFLOW_RELATIONS')
        live = query(cursor, "SELECT GET_DDL('VIEW',%s,TRUE) DDL", (APP + '.V_ORCH_WORKFLOW_RELATIONS',))[0]['DDL']
        if view_body(live) != view_body(view):
            raise ValueError('Unknown TST relation view drift')
    tasks = query(cursor, f"SHOW TASKS LIKE 'TASK_ORCH_%' IN SCHEMA {APP}")
    for task in tasks:
        if not re.fullmatch(r'TASK_ORCH_(?:MAINTENANCE|WF_[0-9A-F_]+_SCHEDULER)', task['name']) or task['owner'] != 'KUMO_ADMIN':
            raise ValueError('Unexpected TST engine task: ' + task['name'])
    service = query(cursor, 'DESCRIBE SERVICE ' + SERVICE)[0]
    if service['owner'] != 'KUMO_ADMIN' or str(service['is_upgrading']).lower() != 'false':
        raise ValueError('Expected stable KUMO_ADMIN-owned TST service')
    return dict(columns=columns, procedures=procedures, usage=usage, view=view,
                view_exists=view_exists, tasks=tasks, service=service, dev_differences=dev_differences)


def catalog_fingerprints(cursor):
    result = {}
    # Compare existing columns; the new column defaults are checked separately.
    for table in ('WORKFLOW', 'JOB', 'DEPENDENCIES', 'APPLICATION_PARAMETERS'):
        names = [r['name'] for r in query(cursor, f'DESCRIBE TABLE {META}.{table}')
                 if r['name'] not in ('JOB_EXECUTION_MODE', 'PARENT_MUST_BE_NEWER')]
        columns = ','.join('"' + n.replace('"', '""') + '"' for n in names)
        result[table] = query(cursor, f'SELECT COUNT(*) N,HASH_AGG({columns}) HASH FROM {META}.{table}')[0]
    return result


def upgrade(cursor, apply=False, report_path=None):
    plan = preflight(cursor)
    result = dict(environment='TST', applied=False, status='PREFLIGHT_PASSED',
        add_columns=[n for n, c in plan['columns'].items() if not c['exists']],
        changed_procedures=[n for n, p in plan['procedures'].items() if p['changed']],
        add_view=not plan['view_exists'],
        dev_definitions_older_than_merged_release=plan['dev_differences'],
        tasks_before=[{k: t.get(k) for k in ('name', 'state', 'schedule', 'warehouse', 'definition')} for t in plan['tasks']],
        service_before={k: plan['service'][k] for k in ('status', 'auto_resume', 'spec_digest')},
        dev_changed=False, catalog_replaced=False, business_commands_executed=False)
    if not apply:
        return result
    if not result['add_columns'] and not result['changed_procedures'] and not result['add_view']:
        result['status'] = 'ALREADY_CURRENT'
        return result
    def save():
        if report_path:
            Path(report_path).write_text(json.dumps(result, indent=2, default=str) + '\n')
    before = catalog_fingerprints(cursor)
    result['status'] = 'PAUSING'; save()
    try:
        query(cursor, 'ALTER SERVICE ' + SERVICE + ' SET AUTO_RESUME=FALSE')
        query(cursor, 'ALTER SERVICE ' + SERVICE + ' SUSPEND')
        for task in plan['tasks']:
            if task['state'] == 'started':
                query(cursor, 'ALTER TASK ' + APP + '.' + task['name'] + ' SUSPEND')
        check_idle(cursor)
        if catalog_fingerprints(cursor) != before:
            raise ValueError('TST configuration changed while admissions were paused')
        if not plan['columns']['JOB_EXECUTION_MODE']['exists']:
            query(cursor, f"ALTER TABLE {META}.WORKFLOW ADD COLUMN JOB_EXECUTION_MODE VARCHAR(20) NOT NULL DEFAULT 'SEQUENTIAL'")
        if not plan['columns']['PARENT_MUST_BE_NEWER']['exists']:
            query(cursor, f'ALTER TABLE {META}.DEPENDENCIES ADD COLUMN PARENT_MUST_BE_NEWER BOOLEAN DEFAULT FALSE NOT NULL')
            query(cursor, f"UPDATE {META}.DEPENDENCIES SET PARENT_MUST_BE_NEWER=TRUE WHERE JOB_ID IS NULL AND PARENT_ID IS NOT NULL AND TRIGGER_TYPE='DEP'")
        if not plan['view_exists']:
            query(cursor, plan['view'])
        for kind, name in sorted(plan['usage']):
            label = 'DATABASE ROLE' if kind == 'DATABASE_ROLE' else 'ROLE'
            role = quote_role(name, kind)
            query(cursor, f'GRANT SELECT ON VIEW {APP}.V_ORCH_WORKFLOW_RELATIONS TO {label} {role}')
        for name, procedure in plan['procedures'].items():
            if not procedure['changed']:
                continue
            if procedure['old_address'] and procedure['old_address'] != procedure['address']:
                query(cursor, 'DROP PROCEDURE ' + procedure['old_address'])
                try:
                    query(cursor, procedure['desired'])
                except Exception:
                    query(cursor, procedure['old'])
                    restore_grants(cursor, procedure['old_address'], procedure['grants'])
                    raise
                restore_grants(cursor, procedure['address'], procedure['grants'])
            else:
                ddl = procedure['desired']
                if procedure['old']:
                    ddl = ddl.replace('\nRETURNS ', '\nCOPY GRANTS\nRETURNS ', 1)
                query(cursor, ddl)
            if not procedure['old']:
                for kind, grantee in sorted(plan['usage']):
                    restore_grants(cursor, procedure['address'], [dict(privilege='USAGE',
                        granted_to=kind, grantee_name=grantee, grant_option='false')])
            live = query(cursor, "SELECT GET_DDL('PROCEDURE',%s,TRUE) DDL", (procedure['address'],))[0]['DDL']
            if digest(live) != digest(procedure['desired']):
                raise RuntimeError('Installed procedure mismatch: ' + name)
        if catalog_fingerprints(cursor) != before:
            raise RuntimeError('Existing TST catalog or parameters changed')
        check = preflight(cursor)
        if any(p['changed'] for p in check['procedures'].values()) or any(not c['exists'] for c in check['columns'].values()):
            raise RuntimeError('TST engine verification failed')
        result.update(applied=True, status='OBJECTS_VERIFIED_SERVICE_AND_TASKS_PAUSED',
                      catalog_and_parameters_preserved=True, procedure_definitions_match_merged_release=True)
        save()
        return result
    except Exception as error:
        result.update(status='FAILED_REQUIRES_REVIEW', error=str(error)); save()
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--report', type=Path, default=ROOT / 'review/tst-workflow-rules-objects.json')
    args = parser.parse_args()
    with connect() as connection, connection.cursor(DictCursor) as cursor:
        query(cursor, 'USE ROLE KUMO_ADMIN')
        query(cursor, "ALTER SESSION SET QUERY_TAG='KUMO_TST_WORKFLOW_RULES_PROMOTION'")
        print(json.dumps(upgrade(cursor, args.apply, args.report), indent=2, default=str))
