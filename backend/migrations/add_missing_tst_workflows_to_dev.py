"""Add missing executable TST legacy workflows to the new DEV catalog.

Read-only by default; --apply inserts complete missing workflows, jobs and
DEPENDENCIES in DEV only. Existing definitions, schedules and execution state
are preserved. Never executes job SQL/dbt, enables schedules, or changes DDL.
The older TST WORKFLOW/JOB sandbox prototypes are not execution definitions.
"""
import argparse
import copy
import json
import re

from migration_support import connect
from migrate_workflow_catalog import build_plan, comparable, fingerprint, SOURCES, TARGETS
from snowflake.connector import DictCursor


SOURCE = 'KUMO_TST.MONITOR_META'
TARGET = 'KUMO_DEV.MONITOR_META'
MAPPING = 'RAW_LIM_PKG_GROUP_SOURCE'
PACKAGE_KEY = ('PKG_GROUP_NAME', 'SUBJECT_AREA', 'SOURCE_ID', 'PKG_ID')
SOURCE_TABLES = SOURCES + (MAPPING,)
TARGET_TABLES = TARGETS + (MAPPING,)
COMMAND_FIELDS = ('JOB_NAME', 'JOB_TYPE', 'JOB_LAYER', 'SQL_COMMAND', 'DBT_COMMAND',
                  'DBT_TARGET', 'DBT_PROJECT_FQN', 'DBT_WORKSPACE',
                  'MIGRATED_FROM_WORKFLOW_ID', 'MIGRATED_FROM_WORKFLOW_STEP',
                  'COMMAND_LINE', 'ON_SUCCESS_COMMAND_LINE',
                  'ON_WARNING_COMMAND_LINE', 'ON_ERROR_COMMAND_LINE')


def read(cursor, namespace, tables):
    result = {}
    for table in tables:
        cursor.execute(f'SELECT * FROM {namespace}.{table}')
        result[table] = cursor.fetchall()
    return result


def missing_packages(source, current, jobs):
    groups = {match for job in jobs if job['JOB_TYPE'] == 'SQL'
              for match in re.findall(r"PKG_GROUP_NAME\s*=>\s*'([^']+)'", job['SQL_COMMAND'], re.I)}
    relevant = [row for row in source if row['PKG_GROUP_NAME'] in groups]
    if groups != {row['PKG_GROUP_NAME'] for row in relevant if row['ACTIVE_FL']}:
        raise ValueError('A readiness package group has no active TST source mapping')
    key = lambda row: tuple(row[field] for field in PACKAGE_KEY)
    if len({key(row) for row in relevant}) != len(relevant) or len({key(row) for row in current}) != len(current):
        raise ValueError('Resolve duplicate package source mappings before migration')
    existing, additions = {key(row): row for row in current}, []
    for row in relevant:
        if key(row) in existing:
            if row['ACTIVE_FL'] != existing[key(row)]['ACTIVE_FL']:
                raise ValueError('Review differing DEV package activation: ' + str(key(row)))
        else:
            # TST's year/sequence are mutable processing checkpoints. A new
            # DEV mapping starts at DEV's first unhandled delivery instead.
            additions.append({**{field: row[field] for field in PACKAGE_KEY},
                              'ACTIVE_FL': row['ACTIVE_FL'], 'DLVY_PKG_YEAR': None,
                              'DLVY_PKG_YEAR_SEQ_NO': None})
    return additions


def missing_plan(source, current, warehouse):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*', warehouse or ''):
        raise ValueError('Configure a valid DEV warehouse before adding workflows')
    expected, summary = build_plan(source['WORKFLOWS'], source['WORKFLOW_TASKS'], TARGET)
    # This call was deliberately removed when the old DEV engine was retired.
    # Preserve the remaining business cleanup SQL exactly as configured in TST.
    for job in expected['JOB']:
        if job['JOB_NAME'] == 'MAINTENANCE - CLEANUP':
            job['SQL_COMMAND'] = re.sub(
                r'^\s*CALL\s+\[DATABASE\]\.\[MONITOR_APP_SCHEMA\]\.SP_CLEANUP_OLD_TMP_TASKS\s*\(\s*1\s*\)\s*;\s*',
                '', job['SQL_COMMAND'], count=1, flags=re.I)
    indexes = {}
    for table, key, label in [('WORKFLOW', 'WORKFLOW_ID', 'WORKFLOW_NAME'), ('JOB', 'JOB_ID', 'JOB_NAME')]:
        rows = current[table]
        if len({row[key] for row in rows}) != len(rows) or len({row[label].upper() for row in rows}) != len(rows):
            raise ValueError('Resolve duplicate DEV identities/names in ' + table)
        indexes[table] = {row[key]: row for row in rows}
        for row in expected[table]:
            existing = indexes[table].get(row[key])
            if existing:
                fields = COMMAND_FIELDS if table == 'JOB' else ('WORKFLOW_NAME', 'MIGRATED_FROM_WORKFLOW_ID')
                differences = [field for field in fields if row.get(field) != existing.get(field)]
                if differences:
                    raise ValueError('Review existing DEV ' + row[label] + ': ' + ', '.join(differences))
            elif any(row[label].upper() == item[label].upper() for item in rows):
                raise ValueError('DEV name already has a different identity: ' + row[label])
    existing_ids = set(indexes['WORKFLOW'])
    for workflow in expected['WORKFLOW']:
        wid = workflow['WORKFLOW_ID']
        if wid in existing_ids:
            old = [row for row in current['DEPENDENCIES'] if row['WORKFLOW_ID'] == wid]
            desired = [row for row in expected['DEPENDENCIES'] if row['WORKFLOW_ID'] == wid]
            if comparable(old) != comparable(desired):
                raise ValueError('Review existing DEV dependencies: ' + workflow['WORKFLOW_NAME'])
    missing = {row['WORKFLOW_ID'] for row in expected['WORKFLOW'] if row['WORKFLOW_ID'] not in existing_ids}
    additions = {table: [] for table in TARGET_TABLES}
    additions['WORKFLOW'] = [copy.deepcopy(row) for row in expected['WORKFLOW'] if row['WORKFLOW_ID'] in missing]
    additions['DEPENDENCIES'] = [copy.deepcopy(row) for row in expected['DEPENDENCIES'] if row['WORKFLOW_ID'] in missing]
    job_ids = {row['JOB_ID'] for row in additions['DEPENDENCIES'] if row['JOB_ID']}
    additions['JOB'] = [copy.deepcopy(row) for row in expected['JOB'] if row['JOB_ID'] in job_ids]
    for job in additions['JOB']:
        if job['JOB_ID'] in indexes['JOB']:
            raise ValueError('Partial DEV migration requires review: ' + job['JOB_NAME'])
    for workflow in additions['WORKFLOW']:
        workflow.update(WAREHOUSE_NAME=warehouse, IS_SCHEDULE_ENABLED=False,
                        ALLOW_EXTERNAL_TRIGGER=False, IS_ENABLED=False)
    if any(row['WORKFLOW_ID'] in missing for row in current['DEPENDENCIES']):
        raise ValueError('Orphan DEV dependency rows conflict with missing workflows')
    additions[MAPPING] = missing_packages(source[MAPPING], current[MAPPING], expected['JOB'])
    summary.update(missing_workflows=[row['WORKFLOW_NAME'] for row in additions['WORKFLOW']],
                   missing_jobs=[row['JOB_NAME'] for row in additions['JOB']],
                   rows_to_insert={table: len(rows) for table, rows in additions.items()},
                   sql_jobs_compared=sum(row['JOB_TYPE'] == 'SQL' for row in expected['JOB']),
                   maintenance_exception='Retired SP_CLEANUP_OLD_TMP_TASKS call omitted',
                   tst_processing_checkpoints_copied=False)
    return additions, expected, summary


def validate_commands(cursor, jobs):
    count = 0
    for env in ('DEV', 'TST'):
        for job in jobs:
            definition = dict(jobType=job['JOB_TYPE'], sqlCommand=job['SQL_COMMAND'],
                              dbtCommand=job['DBT_COMMAND'], dbtTarget=job['DBT_TARGET'],
                              dbtProjectFqn=job['DBT_PROJECT_FQN'])
            cursor.execute(f'CALL KUMO_{env}.MONITOR_APP.SP_RESOLVE_WORKFLOW_COMMAND(PARSE_JSON(%s), NULL)',
                           (json.dumps(definition),))
            result = next(iter(cursor.fetchone().values()))
            if isinstance(result, str):
                result = json.loads(result)
            if not result.get('ok'):
                raise ValueError(f"{env} command resolution failed for {job['JOB_NAME']}: {result.get('error')}")
            command = result.get('execSql', '')
            other = 'TST' if env == 'DEV' else 'DEV'
            if re.search(r'\bKUMO_' + other + r'\b|\[[A-Za-z_][A-Za-z0-9_]*\]', command, re.I):
                raise ValueError(f"Unresolved/cross-environment command for {job['JOB_NAME']} in {env}")
            count += 1
    return count


def insert_missing(cursor, additions, before, source_hashes, target_hashes):
    cursor.execute('BEGIN')
    try:
        cursor.execute(f'UPDATE {TARGET}.DEPENDENCIES SET ALLOW = ALLOW')
        for namespace, hashes in ((SOURCE, source_hashes), (TARGET, target_hashes)):
            for table, value in hashes.items():
                if fingerprint(cursor, namespace, table) != value:
                    raise ValueError('Catalog changed after preflight; retry: ' + namespace + '.' + table)
        for table, rows in additions.items():
            if not rows:
                continue
            fields = ', '.join('"' + key + '"' for key in rows[0])
            bindings = ', '.join('%(' + key + ')s' for key in rows[0])
            if table == MAPPING:
                fields += ', UPDATED_AT'
                bindings += ', SYSDATE()'
            cursor.executemany(f'INSERT INTO {TARGET}.{table} ({fields}) VALUES ({bindings})', rows)
        after = read(cursor, TARGET, TARGET_TABLES)
        for table, key_fields in [('WORKFLOW', ('WORKFLOW_ID',)), ('JOB', ('JOB_ID',)),
                                  ('DEPENDENCIES', ('WORKFLOW_ID',)), (MAPPING, PACKAGE_KEY)]:
            key = lambda row: tuple(row[field] for field in key_fields)
            added_ids = {key(row) for row in additions[table]}
            retained = [row for row in after[table] if key(row) not in added_ids]
            if comparable(retained) != comparable(before[table]):
                raise RuntimeError('Existing DEV configuration changed: ' + table)
            inserted = [{field: row.get(field) for field in additions[table][0]}
                        for row in after[table] if key(row) in added_ids] if additions[table] else []
            if comparable(inserted) != comparable(additions[table]):
                raise RuntimeError('Inserted configuration differs: ' + table)
        for table, value in source_hashes.items():
            if fingerprint(cursor, SOURCE, table) != value:
                raise ValueError('TST source changed during insertion; retry')
        cursor.execute('COMMIT')
    except Exception:
        cursor.execute('ROLLBACK')
        raise


def run(apply=False):
    with connect() as conn, conn.cursor(DictCursor) as cursor:
        cursor.execute("ALTER SESSION SET QUERY_TAG='KUMO_TST_WORKFLOW_DEV_RECONCILIATION'")
        source_hashes = {table: fingerprint(cursor, SOURCE, table) for table in SOURCE_TABLES}
        target_hashes = {table: fingerprint(cursor, TARGET, table) for table in TARGET_TABLES}
        source, before = read(cursor, SOURCE, SOURCE_TABLES), read(cursor, TARGET, TARGET_TABLES)
        cursor.execute('SELECT CURRENT_WAREHOUSE() AS NAME')
        additions, expected, summary = missing_plan(source, before, cursor.fetchone()['NAME'])
        summary['commands_resolved_without_execution'] = validate_commands(cursor, expected['JOB'])
        for namespace, hashes in ((SOURCE, source_hashes), (TARGET, target_hashes)):
            for table, value in hashes.items():
                if fingerprint(cursor, namespace, table) != value:
                    raise ValueError('Catalog changed during preflight; retry')
        if apply and any(additions.values()):
            insert_missing(cursor, additions, before, source_hashes, target_hashes)
        summary.update(applied=apply, source=SOURCE, target=TARGET,
                       existing_dev_configuration_preserved=True, tst_changed=False,
                       new_workflows_enabled=False, schedules_enabled=False,
                       job_commands_executed=False, ddl_changed=False)
        return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    print(json.dumps(run(parser.parse_args().apply), indent=2))
