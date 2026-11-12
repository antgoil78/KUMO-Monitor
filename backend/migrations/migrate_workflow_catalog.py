"""Rebuild WORKFLOW, JOB and DEPENDENCIES from the existing execution catalog.

Read-only preview by default. --apply clears and populates only those three
tables in one transaction. Source definitions, tasks and execution state are
never modified, and configured SQL/dbt commands are never executed.
"""

import argparse
from collections import defaultdict
import json
import re
import shlex
import sys
import uuid

from migration_support import ROOT, connect
from snowflake.connector import DictCursor

sys.path.insert(0, str(ROOT / 'backend'))
from workflow_commands import validate_definition

TARGETS = ('WORKFLOW', 'JOB', 'DEPENDENCIES')
SOURCES = ('WORKFLOWS', 'WORKFLOW_TASKS')


def name(value):
    return re.sub(r'[_\s]+', ' ', value or '').strip().upper()


def identity(namespace, kind, source_id, step=''):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([namespace, kind, source_id, step])))


def dbt_steps(command):
    """Split the reviewed path selectors; reject syntax that needs interpretation."""
    tokens = shlex.split(command or '')
    if len(tokens) < 4 or tokens[0] != 'dbt' or tokens[1] not in ('build', 'run', 'test') or tokens[2] not in ('-s', '--select'):
        raise ValueError('Review this dbt command before migrating: ' + str(command))
    selectors = tokens[3:]
    if len(selectors) != len(set(selectors)):
        raise ValueError('Duplicate dbt selectors require review')
    result = []
    for selector in selectors:
        match = re.fullmatch(r'models/data/(RAW|SDL|EDV|BDL)/([A-Za-z0-9_]+)', selector)
        if not match:
            raise ValueError('Review this dbt selector before migrating: ' + selector)
        layer, subject = match.groups()
        result.append(dict(layer=layer, subject=subject.upper(), selector=selector,
                           command=shlex.join(tokens[:3] + [selector])))
    return result


def callbacks(value):
    if isinstance(value, str):
        value = json.loads(value)
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError('Workflow callbacks must be arrays of workflow IDs')
    return value


def build_plan(workflows, tasks, namespace='KUMO_DEV.MONITOR_META'):
    by_name, by_id = {}, {}
    for source in workflows:
        key = name(source['WORKFLOW_NAME'])
        if key in by_name or source['WORKFLOW_ID'] in by_id:
            raise ValueError('Duplicate source workflow identity or name')
        by_name[key] = source
        by_id[source['WORKFLOW_ID']] = source
    task_by_id = {}
    for task in tasks:
        if task['WORKFLOW_ID'] in task_by_id:
            raise ValueError('Duplicate source schedule')
        task_by_id[task['WORKFLOW_ID']] = task
    excluded = {row['WORKFLOW_ID'] for row in workflows if name(row['WORKFLOW_NAME']) == 'RUN DEPENDENCIES'}
    units, used = [], set(excluded)
    for source_name, ready in sorted(by_name.items()):
        if not source_name.endswith(' READY TO LOAD'):
            continue
        subject = source_name.removesuffix(' READY TO LOAD')
        transform = by_name.get(subject + ' SDL EDV')
        if not transform or str(ready.get('WORKFLOW_TYPE')).upper() != 'SQL' or str(transform.get('WORKFLOW_TYPE')).upper() != 'DBT':
            raise ValueError('Expected a readiness SQL and SDL/EDV dbt pair for ' + subject)
        steps = dbt_steps(transform['DBT_COMMAND'])
        # Some source definitions (CARD in TST) deliberately select SDL only,
        # even though their display name still ends in "SDL EDV". Preserve
        # the actual command rather than inventing an EDV execution.
        if [(step['layer'], step['subject']) for step in steps] not in (
            [('SDL', subject)], [('SDL', subject), ('EDV', subject)],
        ):
            raise ValueError('Expected SDL, optionally followed by EDV, for ' + subject)
        # Only the entry schedule belongs to the combined workflow. Never silently
        # discard a separately enabled schedule on a later source step.
        if task_by_id.get(transform['WORKFLOW_ID'], {}).get('IS_ENABLED'):
            raise ValueError('The SDL/EDV source has a separate enabled schedule: ' + subject)
        units.append((subject + ' LOAD', ready, [(ready, None)] + [(transform, step) for step in steps]))
        used.update((ready['WORKFLOW_ID'], transform['WORKFLOW_ID']))
    for source in sorted(workflows, key=lambda row: name(row['WORKFLOW_NAME'])):
        if source['WORKFLOW_ID'] in used:
            continue
        steps = dbt_steps(source['DBT_COMMAND']) if str(source.get('WORKFLOW_TYPE')).upper() == 'DBT' else [None]
        units.append((name(source['WORKFLOW_NAME']), source, [(source, step) for step in steps]))
        used.add(source['WORKFLOW_ID'])
    if used != set(by_id):
        raise ValueError('Some source workflows were not classified')

    result = dict(WORKFLOW=[], JOB=[], DEPENDENCIES=[])
    source_to_workflow, source_jobs, source_positions = {}, defaultdict(list), {}
    summary = []
    for workflow_name, anchor, members in units:
        workflow_id = identity(namespace, 'workflow', anchor['WORKFLOW_ID'])
        task = task_by_id.get(anchor['WORKFLOW_ID'], {})
        result['WORKFLOW'].append(dict(
            WORKFLOW_ID=workflow_id, WORKFLOW_NAME=workflow_name, WORKFLOW_GROUP=anchor['WORKFLOW_GROUP'],
            DESCRIPTION=anchor.get('DESCRIPTION'), TASK_NAME=None, WAREHOUSE_NAME=None,
            SCHEDULE=task.get('SCHEDULE_CRON'), SCHEDULE_TIMEZONE=task.get('SCHEDULE_TIMEZONE') or 'UTC',
            IS_SCHEDULE_ENABLED=bool(task.get('IS_ENABLED')), ALLOW_MANUAL_TRIGGER=True,
            ALLOW_EXTERNAL_TRIGGER=False, IS_ENABLED=anchor.get('IS_ENABLED') is not False,
            MIGRATED_FROM_WORKFLOW_ID=anchor['WORKFLOW_ID'],
        ))
        parent, job_names = workflow_id, []
        for position, (source, step) in enumerate(members):
            source_id = source['WORKFLOW_ID']
            source_to_workflow[source_id] = workflow_id
            step_name = step['selector'] if step else 'SQL'
            job_id = identity(namespace, 'job', source_id, step_name)
            job_name = step['subject'] + ' ' + step['layer'] if step else name(source['WORKFLOW_NAME'])
            job_type = 'DBT' if step else str(source.get('WORKFLOW_TYPE') or 'SQL').upper()
            if job_type not in ('SQL', 'DBT'):
                raise ValueError('Unsupported source workflow type: ' + job_type)
            sql = source.get('SQL_COMMAND') if job_type == 'SQL' else None
            dbt = step['command'] if step else None
            if not str(sql or dbt or '').strip():
                raise ValueError('Missing command in ' + job_name)
            validate_definition(dict(sqlCommand=sql, dbtCommand=dbt,
                                     dbtTarget=source.get('DBT_TARGET'), dbtProjectFqn=source.get('DBT_PROJECT_FQN')))
            result['JOB'].append(dict(
                JOB_ID=job_id, JOB_NAME=job_name, DESCRIPTION=source.get('DESCRIPTION'), JOB_TYPE=job_type,
                JOB_LAYER=step['layer'] if step else ('RAW' if job_name.endswith('READY TO LOAD') or job_name == 'LOAD RAW DAILY' else 'GENERAL'),
                TASK_NAME=None, COMMAND_LINE=None, SQL_COMMAND=sql, DBT_COMMAND=dbt,
                DBT_TARGET=source.get('DBT_TARGET') if step else None,
                DBT_WORKSPACE=source.get('DBT_WORKSPACE') if step else None,
                DBT_PROJECT_FQN=source.get('DBT_PROJECT_FQN') if step else None,
                TIMEOUT_SECONDS=None, IS_ENABLED=source.get('IS_ENABLED') is not False,
                MIGRATED_FROM_WORKFLOW_ID=source_id, MIGRATED_FROM_WORKFLOW_STEP=step_name,
                ON_SUCCESS_COMMAND_LINE=None, ON_WARNING_COMMAND_LINE=None, ON_ERROR_COMMAND_LINE=None,
            ))
            result['DEPENDENCIES'].append(dict(
                WORKFLOW_ID=workflow_id, JOB_ID=job_id, PARENT_ID=parent, TRIGGER_TYPE='DEP',
                SUCCESS=True if position else None, WARNING=False if position else None,
                ERROR=False if position else None, SQL=None, ALLOW=True,
            ))
            parent = job_id
            job_names.append(job_name)
            source_jobs[source_id].append(job_id)
            source_positions.setdefault(source_id, [position, position])[1] = position
        summary.append(dict(workflow=workflow_name, jobs=job_names))

    # Translate the existing success callbacks into workflow parents. Edges inside
    # a combined workflow are already represented by its readiness/SDL/EDV chain.
    parents = defaultdict(set)
    for source_id, source_workflow_id in source_to_workflow.items():
        task = task_by_id.get(source_id, {})
        if callbacks(task.get('ON_FAIL')):
            raise ValueError('Failure callbacks require explicit migration rules: ' + by_id[source_id]['WORKFLOW_NAME'])
        for child_id in callbacks(task.get('ON_SUCCESS')):
            if child_id in excluded:
                continue
            if child_id not in source_to_workflow:
                raise ValueError('A success callback references an unknown source workflow')
            child_workflow_id = source_to_workflow[child_id]
            if child_workflow_id == source_workflow_id:
                if source_positions[child_id][0] <= source_positions[source_id][1]:
                    raise ValueError('A source callback conflicts with the proposed job order')
            else:
                parents[child_workflow_id].add(source_workflow_id)
    for workflow in result['WORKFLOW']:
        workflow_id = workflow['WORKFLOW_ID']
        if len(parents[workflow_id]) > 1:
            raise ValueError('Multiple workflow parents require explicit migration rules: ' + workflow['WORKFLOW_NAME'])
        parent = next(iter(parents[workflow_id]), None)
        result['DEPENDENCIES'].append(dict(
            WORKFLOW_ID=workflow_id, JOB_ID=None, PARENT_ID=parent, TRIGGER_TYPE='DEP' if parent else None,
            SUCCESS=True if parent else None, WARNING=False if parent else None,
            ERROR=False if parent else None, SQL=None, ALLOW=True,
        ))
    for table, key in (('WORKFLOW', 'WORKFLOW_NAME'), ('JOB', 'JOB_NAME')):
        if len({row[key] for row in result[table]}) != len(result[table]):
            raise ValueError('The migration would create duplicate names in ' + table)
    for workflow_id in parents:
        seen, current = set(), workflow_id
        while parents.get(current):
            if current in seen:
                raise ValueError('Source workflow callbacks contain a cycle')
            seen.add(current)
            current = next(iter(parents[current]))
    if set(source_jobs) != set(by_id) - excluded:
        raise ValueError('Every included source workflow must produce at least one job')
    return result, dict(workflows=summary, excluded=[by_id[source_id]['WORKFLOW_NAME'] for source_id in sorted(excluded)],
                        source_workflows=len(workflows), migrated_source_workflows=len(source_jobs),
                        target_counts={table: len(rows) for table, rows in result.items()})


def fingerprint(cursor, namespace, table):
    cursor.execute(f'SELECT COUNT(*) AS N, HASH_AGG(*) AS HASH FROM {namespace}.{table}')
    return cursor.fetchone()


def comparable(rows):
    return sorted(json.dumps(row, sort_keys=True, default=str) for row in rows)


def replace_catalog(cursor, namespace, plan, sources_before, targets_before):
    cursor.execute('BEGIN')
    try:
        # Same lock used by the app's catalog writers. DELETE participates in the
        # transaction; TRUNCATE and CREATE OR REPLACE must not be used here.
        cursor.execute(f'UPDATE {namespace}.DEPENDENCIES SET ALLOW = ALLOW')
        for table in TARGETS:
            if fingerprint(cursor, namespace, table) != targets_before[table]:
                raise ValueError('Destination changed after preflight; retry: ' + table)
        for table in ('DEPENDENCIES', 'JOB', 'WORKFLOW'):
            cursor.execute(f'DELETE FROM {namespace}.{table}')
        for table in TARGETS:
            rows = plan[table]
            if not rows:
                raise ValueError('Refusing to replace the catalog with an empty ' + table)
            fields = ', '.join('"' + key + '"' for key in rows[0])
            bindings = ', '.join('%(' + key + ')s' for key in rows[0])
            cursor.executemany(f'INSERT INTO {namespace}.{table} ({fields}) VALUES ({bindings})', rows)
            cursor.execute(f'SELECT {fields} FROM {namespace}.{table}')
            if comparable(cursor.fetchall()) != comparable(rows):
                raise RuntimeError('Inserted rows do not match the migration plan: ' + table)
        for table in SOURCES:
            if fingerprint(cursor, namespace, table) != sources_before[table]:
                raise RuntimeError('Source definitions changed during migration: ' + table)
        cursor.execute('COMMIT')
    except Exception:
        cursor.execute('ROLLBACK')
        raise


def run(apply=False, namespace='KUMO_DEV.MONITOR_META'):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*', namespace):
        raise ValueError('Namespace must be an unquoted DATABASE.SCHEMA address')
    namespace = namespace.upper()
    with connect() as conn, conn.cursor(DictCursor) as cursor:
        sources_before = {table: fingerprint(cursor, namespace, table) for table in SOURCES}
        targets_before = {table: fingerprint(cursor, namespace, table) for table in TARGETS}
        cursor.execute(f'SELECT * FROM {namespace}.WORKFLOWS ORDER BY WORKFLOW_NAME')
        workflows = cursor.fetchall()
        cursor.execute(f'SELECT * FROM {namespace}.WORKFLOW_TASKS ORDER BY WORKFLOW_ID')
        tasks = cursor.fetchall()
        plan, summary = build_plan(workflows, tasks, namespace)
        for table in SOURCES:
            if fingerprint(cursor, namespace, table) != sources_before[table]:
                raise ValueError('Source changed during preflight; retry: ' + table)
        summary.update(namespace=namespace, applied=False,
                       target_counts_before={table: targets_before[table]['N'] for table in TARGETS},
                       source_tables_preserved=True, native_tasks_changed=False, commands_executed=False)
        if apply:
            replace_catalog(cursor, namespace, plan, sources_before, targets_before)
            summary['applied'] = True
        return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--namespace', default='KUMO_DEV.MONITOR_META')
    args = parser.parse_args()
    print(json.dumps(run(args.apply, args.namespace), indent=2))
