"""Copy current DEV workflow configuration unchanged into the TST app schemas.

Read-only preflight by default; --apply inserts missing configuration tables in
one transaction. Targets must be empty or already identical. No definitions are
rewritten, no DEV rows removed, no environment parameters copied, and no native
tasks are created or resumed. Row contents never leave the live account except
for command definitions read in memory for resolution checks.
"""

import argparse
import json
import sys

from migration_support import ROOT, connect
from snowflake.connector import DictCursor

sys.path.insert(0, str(ROOT / 'backend'))
from workflow_commands import validate_definition

SOURCE = 'KUMO_DEV.MONITOR_META'
TARGET = 'KUMO_TST.MONITOR_META'
TABLES = ('WORKFLOW', 'JOB', 'WORKFLOW_JOB', 'WORKFLOW_DEPENDENCY',
          'WORKFLOWS', 'WORKFLOW_TASKS', 'EMAIL_GROUPS', 'WORKFLOW_NOTIFICATIONS')


def fingerprint(cursor, name):
    cursor.execute(f'SELECT COUNT(*) AS N,HASH_AGG(*) AS HASH FROM {name}')
    return cursor.fetchone()


def stopped(cursor):
    cursor.execute('SHOW TASKS IN SCHEMA KUMO_TST.MONITOR_APP')
    tasks = cursor.fetchall()
    if not any(row['name'] == 'TASK_WF_MASTER_DISPATCHER' for row in tasks):
        raise ValueError('The TST master dispatcher is missing')
    if any(row['state'] != 'suspended' for row in tasks):
        raise ValueError('TST tasks must remain suspended during configuration promotion')
    for table in ('WORKFLOW_RUN_QUEUE', 'WORKFLOW_HISTORY', 'WORKFLOW_TMP_TASKS'):
        if fingerprint(cursor, TARGET + '.' + table)['N']:
            raise ValueError('Expected the fresh, idle TST setup: ' + table)


def columns(cursor):
    result = {}
    for database in ('KUMO_DEV', 'KUMO_TST'):
        cursor.execute(f"SELECT TABLE_NAME,COLUMN_NAME,DATA_TYPE,IS_NULLABLE FROM {database}.INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA='MONITOR_META' ORDER BY TABLE_NAME,ORDINAL_POSITION")
        grouped = {table: [] for table in TABLES}
        for row in cursor.fetchall():
            if row['TABLE_NAME'] in grouped:
                grouped[row['TABLE_NAME']].append((row['COLUMN_NAME'], row['DATA_TYPE'], row['IS_NULLABLE']))
        result[database] = grouped
    if result['KUMO_DEV'] != result['KUMO_TST'] or any(not value for value in result['KUMO_DEV'].values()):
        raise ValueError('Source and destination configuration columns must match')
    return {table: ','.join('"' + col[0] + '"' for col in values)
            for table, values in result['KUMO_DEV'].items()}


def verify_commands(cursor):
    verified = set()
    for table, kind in (('WORKFLOWS', 'WORKFLOW_TYPE'), ('JOB', 'JOB_TYPE')):
        cursor.execute(f'SELECT {kind} AS KIND,SQL_COMMAND,DBT_COMMAND,DBT_PROJECT_FQN,DBT_TARGET FROM {SOURCE}.{table}')
        for row in cursor.fetchall():
            if str(row['KIND'] or 'DBT').upper() not in ('SQL', 'DBT'):
                continue
            definition = dict(workflowType=row['KIND'] or 'DBT', sqlCommand=row['SQL_COMMAND'],
                              dbtCommand=row['DBT_COMMAND'], dbtProjectFqn=row['DBT_PROJECT_FQN'],
                              dbtTarget=row['DBT_TARGET'])
            validate_definition(definition)
            encoded = json.dumps(definition, sort_keys=True)
            if encoded in verified:
                continue
            cursor.execute('CALL KUMO_TST.MONITOR_APP.SP_RESOLVE_WORKFLOW_COMMAND(PARSE_JSON(%s),NULL)', (encoded,))
            resolved = next(iter(cursor.fetchone().values()))
            if isinstance(resolved, str):
                resolved = json.loads(resolved)
            if not resolved.get('ok'):
                raise ValueError('A DEV command cannot resolve in TST: ' + str(resolved.get('error')))
            if 'KUMO_DEV' in resolved.get('execSql', '').upper():
                raise ValueError('A resolved command still references DEV')
            verified.add(encoded)
            if len(verified) % 20 == 0:
                print('Validated TST command previews:', len(verified), flush=True)
    return len(verified)


def run(apply=False):
    with connect() as conn, conn.cursor(DictCursor) as cursor:
        cursor.execute("ALTER SESSION SET QUERY_TAG='KUMO_DEV_WORKFLOWS_TO_TST'")
        cursor.execute('USE ROLE KUMO_ADMIN')
        stopped(cursor)
        fields = columns(cursor)
        before_parameters = fingerprint(cursor, TARGET + '.APPLICATION_PARAMETERS')
        sources = {table: fingerprint(cursor, SOURCE + '.' + table) for table in TABLES}
        destinations = {table: fingerprint(cursor, TARGET + '.' + table) for table in TABLES}
        to_copy = []
        for table in TABLES:
            if destinations[table] == sources[table]:
                continue
            if destinations[table]['N']:
                raise ValueError('Destination is populated and differs; nothing will be overwritten: ' + table)
            to_copy.append(table)
        verified = verify_commands(cursor)
        if apply:
            cursor.execute('BEGIN TRANSACTION')
            try:
                for table in to_copy:
                    if fingerprint(cursor, TARGET + '.' + table)['N']:
                        raise RuntimeError('Destination changed after preflight: ' + table)
                    cursor.execute(f'INSERT INTO {TARGET}.{table} ({fields[table]}) SELECT {fields[table]} FROM {SOURCE}.{table}')
                    print('Copied', table, sources[table]['N'], 'rows (pending verification/commit)', flush=True)
                for table in TABLES:
                    if fingerprint(cursor, SOURCE + '.' + table) != sources[table]:
                        raise RuntimeError('DEV configuration changed during copy; retry: ' + table)
                    if fingerprint(cursor, TARGET + '.' + table) != sources[table]:
                        raise RuntimeError('TST contents differ from DEV: ' + table)
                if fingerprint(cursor, TARGET + '.APPLICATION_PARAMETERS') != before_parameters:
                    raise RuntimeError('TST environment parameters changed during copy')
                stopped(cursor)
                cursor.execute('COMMIT')
            except Exception:
                cursor.execute('ROLLBACK')
                raise
        return dict(applied=apply, rows_to_copy={table: sources[table]['N'] for table in to_copy},
                    configuration_rows={table: sources[table]['N'] for table in TABLES},
                    distinct_commands_resolved_in_tst=verified,
                    exact_row_fingerprints_verified=apply, environment_parameters_preserved=True,
                    master_state='suspended', native_schedulers_created=False,
                    workflow_commands_executed=False, dev_definitions_changed=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    print(json.dumps(run(parser.parse_args().apply), indent=2))
