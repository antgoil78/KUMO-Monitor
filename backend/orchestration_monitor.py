"""Read-only view of the parallel catalog and a coherent execution per workflow."""
from collections import defaultdict
from datetime import datetime, timezone

import config
import orchestration_configuration as catalog_store
import snowflake_client as sf
from utils import to_jsonable


def read_executions():
    # Keep an active run visible ahead of subsequent queued requests. With no
    # active run, show the oldest queued request, otherwise the latest result.
    # Read job rows from that same run, never each job's independent last run.
    return sf.query_service(f'''
        WITH selected_runs AS (
            SELECT EXECUTION_ID, WORKFLOW_ID,
                   COUNT_IF(STATUS = 'QUEUED') OVER (PARTITION BY WORKFLOW_ID) AS QUEUED_COUNT
            FROM {config.T_ORCHESTRATION_EXECUTION_LOG}
            WHERE JOB_ID IS NULL AND WORKFLOW_ID IN (
                SELECT WORKFLOW_ID FROM {config.T_ORCHESTRATION_WORKFLOW})
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY WORKFLOW_ID
                ORDER BY CASE WHEN STATUS IN ('RUNNING', 'CLAIMED') THEN 0
                              WHEN STATUS = 'QUEUED' THEN 1 ELSE 2 END,
                         IFF(STATUS IN ('RUNNING', 'CLAIMED', 'QUEUED'), REQUESTED_AT, NULL) ASC NULLS LAST,
                         REQUESTED_AT DESC, EXECUTION_ID DESC) = 1
        )
        SELECT l.EXECUTION_ID, l.WORKFLOW_RUN_ID, l.WORKFLOW_ID, l.JOB_ID, l.PARENT_EXECUTION_ID,
               l.JOB_POSITION, l.RUN_MODE, l.REQUEST_ID, l.REQUESTED_JOB_ID, l.STATUS,
               l.REQUESTED_BY, l.REQUESTED_AT, l.STARTED_AT, l.ENDED_AT,
               l.ERROR_MESSAGE, l.QUERY_ID, r.QUEUED_COUNT,
               l.CONFIG_SNAPSHOT:selected::BOOLEAN AS RUN_SELECTED,
               l.CONFIG_SNAPSHOT:definition:JOB_NAME::VARCHAR AS RUN_JOB_NAME,
               l.CONFIG_SNAPSHOT:definition:JOB_TYPE::VARCHAR AS RUN_JOB_TYPE,
               l.CONFIG_SNAPSHOT:definition:JOB_LAYER::VARCHAR AS RUN_JOB_LAYER
        FROM {config.T_ORCHESTRATION_EXECUTION_LOG} l
        JOIN selected_runs r ON l.WORKFLOW_ID = r.WORKFLOW_ID
                           AND l.WORKFLOW_RUN_ID = r.EXECUTION_ID
        ORDER BY l.WORKFLOW_ID, l.JOB_POSITION NULLS FIRST
    ''')


def read_last_runs():
    return sf.query_service(f"""SELECT EXECUTION_ID,WORKFLOW_RUN_ID,WORKFLOW_ID,JOB_ID,STATUS,STARTED_AT,ENDED_AT
        FROM {config.T_ORCHESTRATION_EXECUTION_LOG}
        WHERE STARTED_AT IS NOT NULL AND WORKFLOW_ID IN (SELECT WORKFLOW_ID FROM {config.T_ORCHESTRATION_WORKFLOW})
        QUALIFY ROW_NUMBER() OVER(PARTITION BY WORKFLOW_ID,JOB_ID ORDER BY STARTED_AT DESC,EXECUTION_ID DESC)=1""")


def read_dbt_progress(executions):
    ids = sorted({row['EXECUTION_ID'] for row in executions if row.get('JOB_ID')
                  and row.get('RUN_JOB_TYPE') == 'DBT' and row.get('STATUS') in ('RUNNING', 'SUCCESS', 'FAILED', 'WARNING', 'CANCELLED')})
    if not ids:
        return {}, []
    params = {f'run_{index}': execution_id for index, execution_id in enumerate(ids)}
    placeholders = ','.join(f'%({key})s' for key in params)
    progress, warnings = defaultdict(dict), []
    for kind, table in (('models', config.MODEL_PROGRESS_TABLE), ('tests', config.TEST_PROGRESS_TABLE)):
        try:
            rows = sf.query_service(f'''
                WITH latest AS (
                    SELECT RUN_ID, MODEL_NAME, UPPER(PROGRESS) AS PROGRESS, UPPER(STATUS) AS STATUS
                    FROM {table} WHERE RUN_ID IN ({placeholders})
                    QUALIFY ROW_NUMBER() OVER (PARTITION BY RUN_ID, MODEL_NAME
                        ORDER BY PROGRESS_DTTM DESC NULLS LAST, FINISHED_DTTM DESC NULLS LAST,
                                 STARTED_DTTM DESC NULLS LAST) = 1
                )
                SELECT RUN_ID, COUNT(*) AS TOTAL,
                    COUNT_IF(PROGRESS = 'FINISHED') AS DONE,
                    COUNT_IF(PROGRESS IN ('FINISHED','SKIPPED') OR STATUS IN ('ERROR','FAILED','CANCELLED')) AS COMPLETED,
                    COUNT_IF(PROGRESS = 'STARTED' AND COALESCE(STATUS, '') NOT IN ('ERROR','FAILED','CANCELLED')) AS RUNNING,
                    COUNT_IF(PROGRESS = 'QUEUED') AS QUEUED,
                    COUNT_IF(PROGRESS = 'SKIPPED') AS SKIPPED,
                    COUNT_IF(STATUS IN ('ERROR','FAILED')) AS FAILED,
                    COUNT_IF(STATUS IN ('WARNING','WARN')) AS WARNING
                FROM latest GROUP BY RUN_ID
            ''', params)
            for row in rows:
                counts = {key.lower(): int(row.get(key) or 0) for key in
                          ('TOTAL', 'DONE', 'COMPLETED', 'RUNNING', 'QUEUED', 'SKIPPED', 'FAILED', 'WARNING')}
                progress[str(row['RUN_ID'])][kind] = counts
        except Exception:
            warnings.append(f'DBT {kind} progress is unavailable.')
    for value in progress.values():
        total = sum(counts['total'] for counts in value.values())
        completed = sum(counts['completed'] for counts in value.values())
        value.update(total=total, completed=completed, percent=round(completed / total * 100) if total else None)
    return progress, warnings


def run_state(row=None, missing='NOT RUN'):
    row = row or {}
    return dict(executionId=row.get('EXECUTION_ID'), runId=row.get('WORKFLOW_RUN_ID'),
                parentExecutionId=row.get('PARENT_EXECUTION_ID'), selected=row.get('RUN_SELECTED'),
                status=row.get('STATUS', missing), mode=row.get('RUN_MODE'), requestId=row.get('REQUEST_ID'),
                requestedJobId=row.get('REQUESTED_JOB_ID'), requestedBy=row.get('REQUESTED_BY'),
                requestedAt=to_jsonable(row.get('REQUESTED_AT')),
                start=to_jsonable(row.get('STARTED_AT')), end=to_jsonable(row.get('ENDED_AT')),
                message=row.get('ERROR_MESSAGE'), queryId=row.get('QUERY_ID'),
                position=row.get('JOB_POSITION'))


def build_rows(catalog, executions, dbt_progress=None, schedules=None, last_runs=None):
    dbt_progress = dbt_progress or {}
    schedules = schedules or {}
    last_by_job = {(row['WORKFLOW_ID'],row.get('JOB_ID')):row for row in (last_runs if last_runs is not None else executions) if row.get('STARTED_AT')}
    roots = {row['WORKFLOW_ID']: row for row in executions if row['JOB_ID'] is None}
    children = defaultdict(list)
    assignments = defaultdict(list)
    headers = {row['CHILD_WORKFLOW_ID']: row for row in catalog['records'] if not row['JOB_ID']}
    workflow_names = {row['WORKFLOW_ID']: row['WORKFLOW_NAME'] for row in catalog['workflows']}
    for row in executions:
        root = roots.get(row['WORKFLOW_ID'])
        if row['JOB_ID'] and root and row['WORKFLOW_RUN_ID'] == root['EXECUTION_ID']:
            children[row['WORKFLOW_ID']].append(row)
    for row in catalog_store.assignments(catalog):
        assignments[row['WORKFLOW_ID']].append(row)
    result = []
    for definition in catalog['workflows']:
        workflow_id = definition['WORKFLOW_ID']
        root = roots.get(workflow_id)
        runs = children[workflow_id]
        by_job = {row['JOB_ID']: row for row in runs}
        jobs = []
        for assignment in assignments[workflow_id]:
            job = {key: assignment.get(key) for key in (
                'JOB_ID', 'JOB_NAME', 'JOB_TYPE', 'JOB_LAYER', 'JOB_IS_ENABLED',
                'ASSIGNMENT_ID', 'JOB_SEQUENCE', 'PARENT_JOB_ID', 'PARENT_WORKFLOW_ID',
                'ALLOW', 'SUCCESS', 'WARNING', 'ERROR')}
            job['hasSqlCondition'] = bool(assignment.get('SQL_CONDITION'))
            job['runOnly'] = False
            job['run'] = run_state(by_job.get(job['JOB_ID']), 'NOT SELECTED' if root else 'NOT RUN')
            job['progress'] = dbt_progress.get(job['run']['executionId'])
            job['lastRun'] = run_state(last_by_job.get((workflow_id,job['JOB_ID'])))
            jobs.append(job)
        # An accepted execution survives catalog edits. Keep its removed jobs
        # visible using saved names, without attaching another workflow's run.
        assigned_ids = {job['JOB_ID'] for job in jobs}
        for row in sorted(runs, key=lambda item: item.get('JOB_POSITION') or 0):
            if row['JOB_ID'] not in assigned_ids:
                jobs.append(dict(JOB_ID=row['JOB_ID'], JOB_NAME=row.get('RUN_JOB_NAME') or row['JOB_ID'],
                                 JOB_TYPE=row.get('RUN_JOB_TYPE'), JOB_LAYER=row.get('RUN_JOB_LAYER'),
                                 JOB_SEQUENCE=row.get('JOB_POSITION'), runOnly=True, run=run_state(row),
                                 progress=dbt_progress.get(row['EXECUTION_ID']), lastRun=run_state(last_by_job.get((workflow_id,row['JOB_ID'])))))
        workflow = {key: definition.get(key) for key in (
            'WORKFLOW_ID', 'WORKFLOW_NAME', 'WORKFLOW_GROUP', 'IS_ENABLED', 'ALLOW_MANUAL_TRIGGER',
            'SCHEDULE', 'SCHEDULE_TIMEZONE', 'IS_SCHEDULE_ENABLED', 'ALLOW_EXTERNAL_TRIGGER')}
        header = headers.get(workflow_id, {})
        workflow['parentWorkflowName'] = (workflow_names.get(header['PARENT_WORKFLOW_ID'], 'Unknown workflow')
                                          if header.get('PARENT_WORKFLOW_ID') else None)
        workflow['dependency'] = {key: header.get(key) for key in ('ALLOW', 'SUCCESS', 'WARNING', 'ERROR')}
        workflow['dependency']['hasSqlCondition'] = bool(header.get('SQL_CONDITION'))
        workflow['runUnavailable'] = (
            'Manual execution is disabled.' if not config.ORCHESTRATION_RUNS_ENABLED else
            'Workflow is already queued or running.' if (root or {}).get('STATUS') in ('INITIATING', 'QUEUED', 'CLAIMED', 'RUNNING') else
            'Workflow is disabled.' if not definition.get('IS_ENABLED') else
            'Manual execution is disabled for this workflow.' if not definition.get('ALLOW_MANUAL_TRIGGER') else
            'Workflow ALLOW is false.' if header.get('ALLOW') is False else
            'Resolve dependency references before running.' if workflow_id in catalog.get('invalidWorkflows', []) else
            'No enabled jobs assigned.' if not any(not job.get('runOnly') and job.get('JOB_IS_ENABLED') is not False for job in jobs) else '')
        workflow['canRun'] = not workflow['runUnavailable']
        completed = sum(row['STATUS'] in ('SUCCESS', 'FAILED', 'WARNING', 'SKIPPED', 'CANCELLED') for row in runs)
        partial = sum(((dbt_progress.get(row['EXECUTION_ID']) or {}).get('percent') or 0) / 100
                      for row in runs if row['STATUS'] == 'RUNNING')
        workflow.update(run=run_state(root), lastRun=run_state(last_by_job.get((workflow_id,None))),
                        scheduler=schedules.get(workflow_id,dict(state='NOT SCHEDULED',nextRun=None)), jobs=jobs, queuedCount=int((root or {}).get('QUEUED_COUNT') or 0),
                        progress=dict(total=len(runs), succeeded=sum(row['STATUS'] == 'SUCCESS' for row in runs),
                                      failed=sum(row['STATUS'] == 'FAILED' for row in runs),
                                      skipped=sum(row['STATUS'] == 'SKIPPED' for row in runs),
                                      cancelled=sum(row['STATUS'] == 'CANCELLED' for row in runs),
                                      completed=completed, percent=round((completed + partial) / len(runs) * 100) if runs else None))
        result.append(workflow)
    return result


def payload():
    with sf.connection_scope(force_service=True):
        catalog = catalog_store.read_catalog()
        executions = read_executions()
        progress, warnings = read_dbt_progress(executions)
        last_runs = read_last_runs() if executions else []
        from orchestration_schedules import read_schedules
        schedules, schedule_warnings = read_schedules(catalog)
    return dict(ok=True, source=config.ORCHESTRATION_NAMESPACE,
                executionSource=config.T_ORCHESTRATION_EXECUTION_LOG,
                rows=build_rows(catalog, executions, progress, schedules, last_runs), warnings=catalog.get('warnings', []) + warnings + schedule_warnings,
                runsEnabled=config.ORCHESTRATION_RUNS_ENABLED,
                generatedAt=datetime.now(timezone.utc).isoformat(), refreshSeconds=max(5, config.REFRESH_SECONDS))
