"""Workflow/Job definitions and the unified WORKFLOW_DEPENDENCY configuration.

This module never executes configured conditions or creates execution tasks.
WORKFLOW_JOB is read only by the one-time migration, not by the application.
"""
import hashlib
import json
import uuid
from collections import defaultdict

import config
import snowflake_client as sf


class ConfigurationConflict(ValueError):
    pass


FIELDS = ('PARENT_WORKFLOW_ID', 'CHILD_WORKFLOW_ID', 'JOB_ID', 'PARENT_JOB_ID',
          'JOB_SEQUENCE', 'TRIGGER_TYPE', 'SUCCESS', 'WARNING', 'ERROR',
          'SQL_CONDITION', 'ALLOW', 'IS_ENABLED', 'CONFIG_VERSION', 'MIN_STATUS_LEVEL', 'DEPENDENCY_TYPE')


def read_catalog():
    return {
        'workflows': sf.query_service(f'SELECT * FROM {config.T_ORCHESTRATION_WORKFLOW} ORDER BY WORKFLOW_NAME'),
        'jobs': sf.query_service(f'SELECT * FROM {config.T_ORCHESTRATION_JOB} ORDER BY JOB_NAME'),
        'records': sf.query_service(f'SELECT * FROM {config.T_ORCHESTRATION_DEPENDENCY} ORDER BY CHILD_WORKFLOW_ID, JOB_SEQUENCE, WORKFLOW_DEPENDENCY_ID'),
    }


def revision(catalog):
    # Include definition edits and all configuration records, even conflicts.
    ordered = {key: sorted(value, key=lambda row: json.dumps(row, sort_keys=True, default=str))
               for key, value in catalog.items()}
    return hashlib.sha256(json.dumps(ordered, sort_keys=True, default=str).encode()).hexdigest()


def configuration_rows(catalog):
    headers, memberships = defaultdict(list), defaultdict(list)
    workflows = {row['WORKFLOW_ID']: row for row in catalog['workflows']}
    jobs = {row['JOB_ID']: row for row in catalog['jobs']}
    records = catalog['records']
    for record in records:
        (memberships if record.get('JOB_ID') else headers)[record['CHILD_WORKFLOW_ID']].append(record)
    membership_ids = {(row['CHILD_WORKFLOW_ID'], row['JOB_ID']): row['WORKFLOW_DEPENDENCY_ID']
                      for row in records if row.get('JOB_ID')}
    rows = []
    for workflow_id, workflow in workflows.items():
        links = headers.get(workflow_id, [])
        header = links[0] if len(links) == 1 else {}
        issue = ''
        if len(links) > 1:
            parents = ', '.join(f"{workflows.get(link.get('PARENT_WORKFLOW_ID'), {}).get('WORKFLOW_NAME', 'No parent')} ({link.get('DEPENDENCY_TYPE')}, {link.get('MIN_STATUS_LEVEL')})" for link in links)
            issue = f'Multiple workflow parents: {parents}. Select one parent, or explicitly choose No parent, to resolve.'
        parent_id = header.get('PARENT_WORKFLOW_ID')
        if header.get('DEPENDENCY_TYPE') == 'BLOCKER':
            issue = 'This legacy blocker requires review. Select its parent explicitly and review the status rules.'
        row = _row(workflow_id, workflow_id, workflow['WORKFLOW_NAME'], None, parent_id, header, issue)
        if issue:
            row['parentRules'] = {link.get('PARENT_WORKFLOW_ID'): dict(trigger=link.get('TRIGGER_TYPE'),
                success=link.get('SUCCESS'), warning=link.get('WARNING'), error=link.get('ERROR')) for link in links}
        rows.append(row)
        for record in sorted(memberships.get(workflow_id, []), key=lambda item: (item.get('JOB_SEQUENCE') or 0, item['WORKFLOW_DEPENDENCY_ID'])):
            job_id = record['JOB_ID']
            if job_id not in jobs:
                raise ValueError('A dependency references a job missing from the Job catalog: ' + job_id)
            parent_id = (membership_ids.get((workflow_id, record['PARENT_JOB_ID']))
                         if record.get('PARENT_JOB_ID') else record.get('PARENT_WORKFLOW_ID'))
            rows.append(_row(record['WORKFLOW_DEPENDENCY_ID'], workflow_id, jobs[job_id]['JOB_NAME'], job_id, parent_id, record))
    if any(record['CHILD_WORKFLOW_ID'] not in workflows for record in records):
        raise ValueError('A dependency references a workflow missing from the Workflow catalog')
    return rows


def _row(row_id, workflow_id, name, job_id, parent_id, record, issue=''):
    return dict(id=row_id, workflowId=workflow_id, name=name, jobId=job_id,
                parentId=parent_id, trigger=record.get('TRIGGER_TYPE') if not job_id else 'DEP',
                success=record.get('SUCCESS'), warning=record.get('WARNING'), error=record.get('ERROR'),
                sql=record.get('SQL_CONDITION') or '', allow=record.get('ALLOW', True),
                enabled=record.get('IS_ENABLED', True), issue=issue)


def payload(catalog=None):
    catalog = catalog or read_catalog()
    return dict(ok=True, source=config.ORCHESTRATION_NAMESPACE, rows=configuration_rows(catalog),
                jobs=catalog['jobs'], workflows=catalog['workflows'], revision=revision(catalog))


def validate_rows(rows, catalog):
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError('rows must be a list of configuration objects')
    workflows = {row['WORKFLOW_ID']: row for row in catalog['workflows']}
    jobs = {row['JOB_ID']: row for row in catalog['jobs']}
    by_id, headers, assignments = {}, set(), set()
    normalized = []
    existing = {row['id']: row for row in configuration_rows(catalog)}
    for supplied in rows:
        row = {key: supplied.get(key) for key in ('id', 'workflowId', 'jobId', 'parentId', 'trigger', 'success', 'warning', 'error', 'sql', 'allow')}
        if any(value is not None and not isinstance(value, str) for key, value in row.items()
               if key in ('id', 'workflowId', 'jobId', 'parentId', 'trigger', 'sql')):
            raise ValueError('Object references and SQL must be strings')
        if not row['id'] or len(row['id']) > 36 or row['id'] in by_id:
            raise ValueError('Object identities must be unique nonempty identifiers of at most 36 characters')
        workflow = workflows.get(row['workflowId'])
        if not workflow:
            raise ValueError('Choose a workflow from the Workflow catalog')
        if row['jobId']:
            job = jobs.get(row['jobId'])
            if not job:
                raise ValueError('Choose a job from the Job catalog')
            key = (row['workflowId'], row['jobId'])
            if key in assignments:
                raise ValueError('A job can only occur once within a workflow')
            assignments.add(key)
            row['name'] = job['JOB_NAME']
            if row['trigger'] != 'DEP' or not row['parentId']:
                raise ValueError('Jobs require a parent and must use DEP')
        else:
            if row['id'] != row['workflowId'] or row['workflowId'] in headers:
                raise ValueError('Each workflow requires exactly one configuration row')
            headers.add(row['workflowId'])
            row['name'] = workflow['WORKFLOW_NAME']
        if row['trigger'] not in (None, 'RUNNER', 'DEP'):
            raise ValueError('Trigger must be RUNNER, DEP or null')
        for field in ('success', 'warning', 'error'):
            if row[field] is not None and type(row[field]) is not bool:
                raise ValueError('Status rules must be true, false or null')
        if type(row['allow']) is not bool:
            raise ValueError('ALLOW must be true or false')
        if (row['trigger'] or any(row[field] is not None for field in ('success', 'warning', 'error'))) and not row['parentId']:
            raise ValueError('This trigger or status rule requires a parent')
        if existing.get(row['id'], {}).get('issue') and supplied.get('resolvedConflict') is not True:
            raise ValueError('Resolve the multiple workflow parents explicitly before saving')
        row['sql'] = (row['sql'] or '').strip()
        row['enabled'] = existing.get(row['id'], {}).get('enabled', True)
        by_id[row['id']] = row
        normalized.append(row)
    if headers != set(workflows):
        raise ValueError('Keep every workflow definition in the overview; manage definitions in the Workflow catalog')
    for row in normalized:
        parent = by_id.get(row['parentId'])
        if row['parentId'] and not parent:
            raise ValueError('A selected parent does not exist')
        if parent and ((not row['jobId'] and parent['jobId']) or (row['jobId'] and parent['workflowId'] != row['workflowId'])):
            raise ValueError('Workflow parents must be workflows; job parents must belong to the same workflow')
        seen = {row['id']}
        while parent:
            if parent['id'] in seen:
                raise ValueError('Parent relationships cannot contain cycles')
            seen.add(parent['id'])
            parent = by_id.get(parent['parentId'])
    for workflow_id in headers:
        if sum(bool(row['jobId']) and row['workflowId'] == workflow_id and row['parentId'] == workflow_id for row in normalized) != 1:
            raise ValueError(workflows[workflow_id]['WORKFLOW_NAME'] + ' must have exactly one entry job')
    return normalized


def lock_configuration():
    # Acquire the same standard-table UPDATE lock for every application writer.
    # Do not change audit timestamps while acquiring the lock.
    sf.execute_service(f'UPDATE {config.T_ORCHESTRATION_DEPENDENCY} SET CONFIG_VERSION = CONFIG_VERSION')


def save(rows, expected_revision):
    sf.execute_service('BEGIN')
    try:
        lock_configuration()
        catalog = read_catalog()
        if not expected_revision or expected_revision != revision(catalog):
            raise ConfigurationConflict('Configuration or definitions changed since loading. Discard the draft and refresh before saving again.')
        rows = validate_rows(rows, catalog)
        by_id = {row['id']: row for row in rows}
        original = {record['WORKFLOW_DEPENDENCY_ID']: record for record in catalog['records']}
        header_ids = defaultdict(list)
        for record in original.values():
            if not record.get('JOB_ID'):
                header_ids[record['CHILD_WORKFLOW_ID']].append(record['WORKFLOW_DEPENDENCY_ID'])
        desired, sequences = {}, defaultdict(int)
        for row in rows:
            parent = by_id.get(row['parentId'])
            if row['jobId']:
                record_id = row['id']
                sequences[row['workflowId']] += 1
            else:
                record_id = next(iter(header_ids[row['workflowId']]), str(uuid.uuid4()))
            if record_id in desired:
                raise ValueError('Configuration identities conflict')
            desired[record_id] = dict(
                PARENT_WORKFLOW_ID=parent['workflowId'] if parent and not parent['jobId'] else None,
                CHILD_WORKFLOW_ID=row['workflowId'], JOB_ID=row['jobId'],
                PARENT_JOB_ID=parent['jobId'] if parent and parent['jobId'] else None,
                JOB_SEQUENCE=sequences[row['workflowId']] if row['jobId'] else None,
                TRIGGER_TYPE=row['trigger'], SUCCESS=row['success'], WARNING=row['warning'], ERROR=row['error'],
                SQL_CONDITION=row['sql'] or None, ALLOW=row['allow'], IS_ENABLED=row['enabled'], CONFIG_VERSION=1,
                MIN_STATUS_LEVEL='ERROR' if row['error'] else 'WARNING' if row['warning'] else 'SUCCESS',
                DEPENDENCY_TYPE='SEQUENCE' if row['jobId'] else 'TRIGGER')
        for record_id in original.keys() - desired.keys():
            sf.execute_service(f'DELETE FROM {config.T_ORCHESTRATION_DEPENDENCY} WHERE WORKFLOW_DEPENDENCY_ID = %(record_id)s', {'record_id': record_id})
        for record_id, values in desired.items():
            if record_id in original:
                if all(original[record_id].get(field) == values[field] for field in FIELDS):
                    continue
                sets = ', '.join(f'"{field}" = %({field})s' for field in FIELDS)
                sf.execute_service(f'UPDATE {config.T_ORCHESTRATION_DEPENDENCY} SET {sets}, UPDATED_AT = CURRENT_TIMESTAMP(), UPDATED_BY = CURRENT_USER() WHERE WORKFLOW_DEPENDENCY_ID = %(record_id)s', {**values, 'record_id': record_id})
            else:
                columns = ', '.join(f'"{field}"' for field in FIELDS)
                bindings = ', '.join(f'%({field})s' for field in FIELDS)
                sf.execute_service(f'INSERT INTO {config.T_ORCHESTRATION_DEPENDENCY} (WORKFLOW_DEPENDENCY_ID, {columns}) VALUES (%(record_id)s, {bindings})', {**values, 'record_id': record_id})
        result = payload()
        sf.execute_service('COMMIT')
        return result
    except Exception:
        sf.execute_service('ROLLBACK')
        raise


def assignments(catalog):
    workflows = {row['WORKFLOW_ID']: row for row in catalog['workflows']}
    jobs = {row['JOB_ID']: row for row in catalog['jobs']}
    return [{**jobs[record['JOB_ID']], **record, 'JOB_IS_ENABLED': jobs[record['JOB_ID']]['IS_ENABLED'],
             'WORKFLOW_ID': record['CHILD_WORKFLOW_ID'], 'WORKFLOW_NAME': workflows[record['CHILD_WORKFLOW_ID']]['WORKFLOW_NAME'],
             'ASSIGNMENT_ID': record['WORKFLOW_DEPENDENCY_ID']}
            for record in catalog['records'] if record.get('JOB_ID')]


def add_workflow(workflow_id, entry_job_id):
    for job_id, parent_id, trigger in ((None, None, None), (entry_job_id, workflow_id, 'DEP')):
        sf.execute_service(f'''INSERT INTO {config.T_ORCHESTRATION_DEPENDENCY}
            (WORKFLOW_DEPENDENCY_ID, CHILD_WORKFLOW_ID, JOB_ID, PARENT_WORKFLOW_ID,
             JOB_SEQUENCE, TRIGGER_TYPE, CONFIG_VERSION)
            VALUES (%(id)s, %(workflow)s, %(job)s, %(parent)s, %(sequence)s, %(trigger)s, 1)''',
            dict(id=str(uuid.uuid4()), workflow=workflow_id, job=job_id, parent=parent_id,
                 sequence=1 if job_id else None, trigger=trigger))
