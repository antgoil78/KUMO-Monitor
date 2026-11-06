"""Move workflow/job configuration to WORKFLOW_DEPENDENCY once.

Default is read-only. Use --apply to extend only this table and copy existing
memberships. Source tables are retained. Multiple roots/parents are preserved
for explicit resolution in the editor. No tasks or execution data are changed.
"""
import argparse
from collections import Counter
import json
import re

from migration_support import connect
from snowflake.connector import DictCursor

COLUMNS = {
    'JOB_ID': 'VARCHAR(36)', 'PARENT_JOB_ID': 'VARCHAR(36)', 'JOB_SEQUENCE': 'NUMBER(9,0)',
    'TRIGGER_TYPE': 'VARCHAR(20)', 'SUCCESS': 'BOOLEAN', 'WARNING': 'BOOLEAN', 'ERROR': 'BOOLEAN',
    'SQL_CONDITION': 'VARCHAR', 'ALLOW': 'BOOLEAN NOT NULL DEFAULT TRUE',
    'CONFIG_VERSION': 'NUMBER(9,0) NOT NULL DEFAULT 0',
}


def status_flags(level, blocker=False):
    level = str(level or 'SUCCESS').upper()
    accepted = {'SUCCESS': (True, False, False), 'WARNING': (True, True, False),
                'ERROR': (True, True, True)}
    if level not in accepted:
        raise ValueError('Review an unsupported legacy status level: ' + level)
    # Blocker rows are kept as conflicts, not silently turned into triggers.
    return tuple(False if status == level else None for status in ('SUCCESS', 'WARNING', 'ERROR')) if blocker else accepted[level]


def run(namespace, apply=False):
    if not re.fullmatch(r'[A-Z_][A-Z0-9_$]*\.[A-Z_][A-Z0-9_$]*', namespace):
        raise ValueError('Use an unquoted database.schema namespace')
    table = namespace + '.WORKFLOW_DEPENDENCY'
    with connect() as connection, connection.cursor(DictCursor) as cursor:
        cursor.execute("ALTER SESSION SET QUERY_TAG='KUMO_UNIFY_ORCHESTRATION_DEPENDENCIES'")
        cursor.execute('USE ROLE KUMO_ADMIN')
        cursor.execute('DESCRIBE TABLE ' + table)
        columns = {row['name'] for row in cursor.fetchall()}
        if 'CONFIG_VERSION' in columns:
            cursor.execute(f'SELECT COUNT(*) AS N FROM {table} WHERE CONFIG_VERSION <> 1')
            if not cursor.fetchone()['N']:
                return dict(applied=False, already_migrated=True, namespace=namespace)
        cursor.execute(f'SELECT * FROM {namespace}.WORKFLOW ORDER BY WORKFLOW_NAME')
        workflows = cursor.fetchall()
        cursor.execute(f'SELECT * FROM {table} ORDER BY WORKFLOW_DEPENDENCY_ID')
        relationships = cursor.fetchall()
        cursor.execute(f'SELECT * FROM {namespace}.WORKFLOW_JOB ORDER BY WORKFLOW_ID, JOB_SEQUENCE')
        memberships = cursor.fetchall()
        counts = Counter(row['CHILD_WORKFLOW_ID'] for row in relationships if not row.get('JOB_ID'))
        roots = Counter(row['WORKFLOW_ID'] for row in memberships if not row.get('PARENT_JOB_ID'))
        report = dict(applied=False, namespace=namespace, workflows=len(workflows), assignments=len(memberships),
                      workflow_relationships=len(relationships),
                      multiple_workflow_parents=[row['WORKFLOW_NAME'] for row in workflows if counts[row['WORKFLOW_ID']] > 1],
                      invalid_entry_job_count=[row['WORKFLOW_NAME'] for row in workflows if roots[row['WORKFLOW_ID']] != 1])
        # Validate supported values before applying any schema mutation.
        for row in relationships + memberships:
            status_flags(row['MIN_STATUS_LEVEL'], row['DEPENDENCY_TYPE'] == 'BLOCKER')
        if not apply:
            return report
        for name, definition in COLUMNS.items():
            cursor.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "{name}" {definition}')
        cursor.execute(f'ALTER TABLE {table} ALTER COLUMN PARENT_WORKFLOW_ID DROP NOT NULL')
        cursor.execute('BEGIN')
        try:
            for row in relationships:
                if row.get('CONFIG_VERSION') == 1:
                    continue
                success, warning, error = status_flags(row['MIN_STATUS_LEVEL'], row['DEPENDENCY_TYPE'] == 'BLOCKER')
                cursor.execute(f'''UPDATE {table} SET TRIGGER_TYPE='DEP',
                    "SUCCESS"=%(success)s,"WARNING"=%(warning)s,"ERROR"=%(error)s,
                    CONFIG_VERSION=1 WHERE WORKFLOW_DEPENDENCY_ID=%(id)s''',
                    dict(success=success, warning=warning, error=error, id=row['WORKFLOW_DEPENDENCY_ID']))
            existing_ids = {row['WORKFLOW_DEPENDENCY_ID'] for row in relationships}
            for workflow in workflows:
                if not counts[workflow['WORKFLOW_ID']]:
                    cursor.execute(f'''INSERT INTO {table}
                        (CHILD_WORKFLOW_ID, CONFIG_VERSION) VALUES (%s,1)''', (workflow['WORKFLOW_ID'],))
            for row in memberships:
                if row['WORKFLOW_JOB_ID'] in existing_ids:
                    continue
                success, warning, error = status_flags(row['MIN_STATUS_LEVEL']) if row.get('PARENT_JOB_ID') else (None, None, None)
                cursor.execute(f'''INSERT INTO {table}
                    (WORKFLOW_DEPENDENCY_ID,CHILD_WORKFLOW_ID,JOB_ID,PARENT_WORKFLOW_ID,PARENT_JOB_ID,
                     JOB_SEQUENCE,TRIGGER_TYPE,"SUCCESS","WARNING","ERROR",IS_ENABLED,CONFIG_VERSION,
                     MIN_STATUS_LEVEL,DEPENDENCY_TYPE,CREATED_AT,CREATED_BY,UPDATED_AT,UPDATED_BY)
                    VALUES (%(id)s,%(workflow)s,%(job)s,%(parent_workflow)s,%(parent_job)s,
                     %(sequence)s,'DEP',%(success)s,%(warning)s,%(error)s,%(enabled)s,1,
                     %(level)s,%(type)s,%(created)s,%(creator)s,%(updated)s,%(updater)s)''',
                    dict(id=row['WORKFLOW_JOB_ID'], workflow=row['WORKFLOW_ID'], job=row['JOB_ID'],
                         parent_workflow=None if row.get('PARENT_JOB_ID') else row['WORKFLOW_ID'],
                         parent_job=row.get('PARENT_JOB_ID'), sequence=row['JOB_SEQUENCE'],
                         success=success, warning=warning, error=error, enabled=row['IS_ENABLED'],
                         level=row['MIN_STATUS_LEVEL'], type=row['DEPENDENCY_TYPE'],
                         created=row['CREATED_AT'], creator=row['CREATED_BY'], updated=row['UPDATED_AT'], updater=row['UPDATED_BY']))
            cursor.execute(f'SELECT COUNT(*) AS N FROM {table} WHERE JOB_ID IS NOT NULL')
            if cursor.fetchone()['N'] != len(memberships):
                raise RuntimeError('Membership count differs after migration')
            cursor.execute('COMMIT')
        except Exception:
            cursor.execute('ROLLBACK')
            raise
        # DDL is intentionally outside the data transaction.
        cursor.execute(f"ALTER TABLE {table} SET COMMENT = 'Workflow and job dependency configuration; definitions remain in WORKFLOW and JOB'")
        return {**report, 'applied': True, 'source_tables_retained': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--namespace', default='KUMO_DEV.MONITOR_META')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(args.namespace, args.apply), indent=2))
