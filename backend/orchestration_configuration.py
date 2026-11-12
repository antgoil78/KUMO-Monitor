"""WORKFLOW and JOB definitions with parent chains stored in DEPENDENCIES.

The API keeps assignment identities scoped to a workflow. Snowflake stores the
actual workflow/job IDs in PARENT_ID; job order is derived from that chain.
This module never executes conditions or creates execution tasks.
"""
import hashlib
import json
import uuid
from collections import defaultdict

import config
import snowflake_client as sf


class ConfigurationConflict(ValueError):
    pass


def read_catalog():
    catalog = {
        'workflows': sf.query_service(f'SELECT * FROM {config.T_ORCHESTRATION_WORKFLOW} ORDER BY WORKFLOW_NAME'),
        'jobs': sf.query_service(f'SELECT * FROM {config.T_ORCHESTRATION_JOB} ORDER BY JOB_NAME'),
        'raw_records': sf.query_service(f'SELECT * FROM {config.T_ORCHESTRATION_DEPENDENCY} ORDER BY WORKFLOW_ID, JOB_ID'),
    }
    catalog.update(normalize_dependencies(catalog))
    return catalog


def assignment_id(workflow_id, job_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([workflow_id, job_id])))


def normalize_dependencies(catalog):
    workflows = {row['WORKFLOW_ID'] for row in catalog['workflows']}
    jobs = {row['JOB_ID'] for row in catalog['jobs']}
    records, warnings, invalid, seen = [], [], set(), set()
    for source in catalog['raw_records']:
        workflow, job, parent = (source.get(key) for key in ('WORKFLOW_ID', 'JOB_ID', 'PARENT_ID'))
        if workflow not in workflows or (job is not None and job not in jobs):
            warnings.append(f'DEPENDENCIES references an unknown workflow or job: {workflow} / {job or "(workflow)"}. This row has been preserved.')
            if workflow in workflows:
                invalid.add(workflow)
            continue
        key = (workflow, job)
        if key in seen:
            warnings.append(f'DEPENDENCIES contains duplicate rows for {workflow} / {job or "(workflow)"}. Resolve these before editing this workflow.')
            invalid.add(workflow)
            continue
        seen.add(key)
        records.append(dict(
            WORKFLOW_DEPENDENCY_ID=assignment_id(workflow, job), CHILD_WORKFLOW_ID=workflow,
            JOB_ID=job, PARENT_WORKFLOW_ID=parent if not job or parent == workflow else None,
            PARENT_JOB_ID=parent if job and parent != workflow else None,
            JOB_SEQUENCE=None, TRIGGER_TYPE=source.get('TRIGGER_TYPE'),
            SUCCESS=source.get('SUCCESS'), WARNING=source.get('WARNING'), ERROR=source.get('ERROR'),
            SQL_CONDITION=source.get('SQL'), ALLOW=source.get('ALLOW'), IS_ENABLED=True,
            DEPENDENCY_TYPE='SEQUENCE' if job else 'TRIGGER', MIN_STATUS_LEVEL='SUCCESS', CONFIG_VERSION=1,
        ))
    # The table has no sequence column. Follow parents, never physical row order.
    for workflow in workflows:
        members = [row for row in records if row['CHILD_WORKFLOW_ID'] == workflow and row['JOB_ID']]
        by_job = {row['JOB_ID']: row for row in members}
        def depth(row, visited):
            if row['JOB_ID'] in visited:
                return len(members) + 1
            parent = by_job.get(row['PARENT_JOB_ID'])
            return 1 + depth(parent, visited | {row['JOB_ID']}) if parent else 0
        members.sort(key=lambda row: (depth(row, set()), row['JOB_ID']))
        for position, row in enumerate(members, 1):
            expected_parent = members[position - 2]['JOB_ID'] if position > 1 else workflow
            if (row['PARENT_JOB_ID'] or row['PARENT_WORKFLOW_ID']) != expected_parent:
                warnings.append(f'Workflow {workflow} has a branching, missing or cyclic job parent. Reorder its jobs to save a single parent chain.')
                break
        # Keep all display positions even for a malformed chain.
        for position, row in enumerate(members, 1):
            row['JOB_SEQUENCE'] = position
    return dict(records=records, warnings=warnings, invalidWorkflows=sorted(invalid))


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
                sql=record.get('SQL_CONDITION') or '', allow=True if record.get('ALLOW') is None else record['ALLOW'],
                enabled=record.get('IS_ENABLED', True), issue=issue)


def payload(catalog=None):
    catalog = catalog or read_catalog()
    return dict(ok=True, source=config.ORCHESTRATION_NAMESPACE, rows=configuration_rows(catalog),
                jobs=catalog['jobs'], workflows=catalog['workflows'], assignments=assignments(catalog),
                warnings=catalog.get('warnings', []), invalidWorkflows=catalog.get('invalidWorkflows', []),
                revision=revision(catalog))


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
            if row['trigger'] not in ('DEP', None) or not row['parentId']:
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
        members = [row for row in normalized if row['jobId'] and row['workflowId'] == workflow_id]
        if members and sum(row['parentId'] == workflow_id for row in members) != 1:
            raise ValueError(workflows[workflow_id]['WORKFLOW_NAME'] + ' must have exactly one entry job')
    return normalized


def lock_configuration():
    # Acquire the same standard-table UPDATE lock for every application writer.
    # Do not change audit timestamps while acquiring the lock.
    sf.execute_service(f'UPDATE {config.T_ORCHESTRATION_DEPENDENCY} SET ALLOW = ALLOW')


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
        if catalog.get('invalidWorkflows'):
            raise ValueError('Resolve duplicate or unknown dependency references before saving the configuration.')
        desired = {}
        for row in rows:
            parent = by_id.get(row['parentId'])
            desired[assignment_id(row['workflowId'], row['jobId'])] = dict(
                WORKFLOW_ID=row['workflowId'], JOB_ID=row['jobId'],
                PARENT_ID=(parent['jobId'] or parent['workflowId']) if parent else None,
                TRIGGER_TYPE=row['trigger'], SUCCESS=row['success'], WARNING=row['warning'],
                ERROR=row['error'], SQL=row['sql'] or None, ALLOW=row['allow'])
        original = {assignment_id(record['CHILD_WORKFLOW_ID'], record.get('JOB_ID')): native_record(record)
                    for record in original.values()}
        persist_records(original, desired)
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
            for record in sorted(catalog['records'], key=lambda row: (row['CHILD_WORKFLOW_ID'], row.get('JOB_SEQUENCE') or 0)) if record.get('JOB_ID')]


NATIVE_FIELDS = ('WORKFLOW_ID', 'JOB_ID', 'PARENT_ID', 'TRIGGER_TYPE', 'SUCCESS', 'WARNING', 'ERROR', 'SQL', 'ALLOW')


def native_record(record):
    return dict(WORKFLOW_ID=record['CHILD_WORKFLOW_ID'], JOB_ID=record.get('JOB_ID'),
                PARENT_ID=record.get('PARENT_JOB_ID') or record.get('PARENT_WORKFLOW_ID'),
                TRIGGER_TYPE=record.get('TRIGGER_TYPE'), SUCCESS=record.get('SUCCESS'),
                WARNING=record.get('WARNING'), ERROR=record.get('ERROR'),
                SQL=record.get('SQL_CONDITION'), ALLOW=record.get('ALLOW'))


def persist_records(original, desired):
    # Scope every write to a workflow and job. Unmatched sample rows are retained.
    for identity in original.keys() | desired.keys():
        before, after = original.get(identity), desired.get(identity)
        if before == after:
            continue
        values = after or before
        where = 'WORKFLOW_ID = %(WORKFLOW_ID)s AND ' + ('JOB_ID = %(JOB_ID)s' if values['JOB_ID'] is not None else 'JOB_ID IS NULL')
        if after is None:
            sf.execute_service(f'DELETE FROM {config.T_ORCHESTRATION_DEPENDENCY} WHERE {where}', values)
        elif before is not None:
            sets = ', '.join(f'"{field}" = %({field})s' for field in NATIVE_FIELDS[2:])
            sf.execute_service(f'UPDATE {config.T_ORCHESTRATION_DEPENDENCY} SET {sets} WHERE {where}', values)
        else:
            columns = ', '.join(f'"{field}"' for field in NATIVE_FIELDS)
            bindings = ', '.join(f'%({field})s' for field in NATIVE_FIELDS)
            sf.execute_service(f'INSERT INTO {config.T_ORCHESTRATION_DEPENDENCY} ({columns}) VALUES ({bindings})', values)


def add_workflow(workflow_id, entry_job_id=None):
    header = dict(WORKFLOW_ID=workflow_id, JOB_ID=None, PARENT_ID=None, TRIGGER_TYPE=None,
                  SUCCESS=None, WARNING=None, ERROR=None, SQL=None, ALLOW=True)
    records = {assignment_id(workflow_id, None): header}
    if entry_job_id:
        records[assignment_id(workflow_id, entry_job_id)] = dict(header, JOB_ID=entry_job_id, PARENT_ID=workflow_id, TRIGGER_TYPE='DEP')
    persist_records({}, records)


def check_revision(catalog, expected):
    if not expected or expected != revision(catalog):
        raise ConfigurationConflict('Configuration or definitions changed since loading. Discard the draft and refresh before saving again.')


def apply_orders(catalog, orders):
    if not isinstance(orders, list) or not orders:
        raise ValueError('Provide at least one workflow job order')
    workflows = {row['WORKFLOW_ID'] for row in catalog['workflows']}
    jobs = {row['JOB_ID'] for row in catalog['jobs']}
    seen = set()
    changes = []
    for order in orders:
        if not isinstance(order, dict):
            raise ValueError('Each order must contain workflowId and jobIds')
        workflow, job_ids = order.get('workflowId'), order.get('jobIds')
        if not isinstance(workflow, str) or workflow not in workflows or workflow in seen:
            raise ValueError('Choose a valid, unique workflow')
        seen.add(workflow)
        if workflow in catalog.get('invalidWorkflows', []):
            raise ValueError('Resolve duplicate or unknown dependency references for this workflow first')
        if (not isinstance(job_ids, list) or any(not isinstance(job, str) or job not in jobs for job in job_ids)
                or len(job_ids) != len(set(job_ids))):
            raise ValueError('Choose unique jobs from the Job catalog')
        original = {assignment_id(workflow, row['JOB_ID']): native_record(row)
                    for row in catalog['records'] if row['CHILD_WORKFLOW_ID'] == workflow and row.get('JOB_ID')}
        desired = {}
        parent = workflow
        for job in job_ids:
            identity = assignment_id(workflow, job)
            values = original.get(identity, dict(WORKFLOW_ID=workflow, JOB_ID=job, TRIGGER_TYPE='DEP',
                                                SUCCESS=None, WARNING=None, ERROR=None, SQL=None, ALLOW=True))
            desired[identity] = dict(values, PARENT_ID=parent)
            parent = job
        changes.append((original, desired))
    # Validate the whole batch before the first write.
    for original, desired in changes:
        persist_records(original, desired)


def save_orders(orders, expected_revision):
    sf.execute_service('BEGIN')
    try:
        lock_configuration()
        catalog = read_catalog()
        check_revision(catalog, expected_revision)
        apply_orders(catalog, orders)
        result = payload()
        sf.execute_service('COMMIT')
        return result
    except Exception:
        sf.execute_service('ROLLBACK')
        raise
