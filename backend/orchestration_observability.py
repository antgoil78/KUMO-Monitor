"""DAGs and logs scoped to one new workflow execution and its job execution IDs."""
import json

import config
import orchestration_configuration as catalog_store
import snowflake_client as sf
from utils import normalize_rows


class ExecutionNotFound(ValueError):
    pass


def unpack(row):
    result = dict(row)
    for key in ('CONFIG_SNAPSHOT', 'RESULT'):
        if isinstance(result.get(key), str):
            result[key] = json.loads(result[key])
    return result


def execution_rows(workflow_id, run_id=None, job_id=None):
    params = dict(workflow=workflow_id, run=run_id)
    roots = sf.query_service(f'''SELECT * FROM {config.T_ORCHESTRATION_EXECUTION_LOG}
        WHERE WORKFLOW_ID=%(workflow)s AND JOB_ID IS NULL
          {'AND EXECUTION_ID=%(run)s' if run_id else ''}
        ORDER BY CASE WHEN STATUS IN ('RUNNING','CLAIMED') THEN 0 WHEN STATUS='QUEUED' THEN 1 ELSE 2 END,
                 IFF(STATUS IN ('RUNNING','CLAIMED','QUEUED'),REQUESTED_AT,NULL) ASC NULLS LAST,
                 REQUESTED_AT DESC, EXECUTION_ID DESC LIMIT 1''', params)
    if not roots:
        if run_id:
            raise ExecutionNotFound('Workflow execution not found')
        return None, []
    root = unpack(roots[0])
    rows = sf.query_service(f'''SELECT * FROM {config.T_ORCHESTRATION_EXECUTION_LOG}
        WHERE WORKFLOW_ID=%(workflow)s AND WORKFLOW_RUN_ID=%(run)s AND JOB_ID IS NOT NULL
        {'AND JOB_ID=%(job)s' if job_id else ''} ORDER BY JOB_POSITION''',
        dict(workflow=workflow_id, run=root['EXECUTION_ID'], job=job_id))
    if job_id and not rows:
        raise ExecutionNotFound('This job is not part of the workflow execution')
    return root, [unpack(row) for row in rows]


def sources_for(rows):
    ids = [row['EXECUTION_ID'] for row in rows if (row.get('CONFIG_SNAPSHOT') or {}).get('definition', {}).get('JOB_TYPE') == 'DBT']
    result = dict(modelProgress=[], testProgress=[], runLog=[])
    warnings = {}
    if not ids:
        return result, warnings
    params = {f'job_{index}': value for index, value in enumerate(ids)}
    placeholders = ','.join(f'%({key})s' for key in params)
    for key, table, order in (('modelProgress',config.MODEL_PROGRESS_TABLE,'PROGRESS_DTTM, MODEL_NAME'),
                              ('testProgress',config.TEST_PROGRESS_TABLE,'PROGRESS_DTTM, MODEL_NAME'),
                              ('runLog',config.RUN_LOG_TABLE,'LOG_DTTM')):
        try:
            values = sf.query_service(f'SELECT * FROM {table} WHERE RUN_ID IN ({placeholders}) ORDER BY {order} LIMIT 5000', params)
            names = {row['EXECUTION_ID']: row.get('CONFIG_SNAPSHOT', {}).get('definition', {}).get('JOB_NAME') for row in rows}
            result[key] = [{**row, 'JOB_NAME': names.get(row['RUN_ID'])} for row in values]
        except Exception:
            warnings[key] = 'This execution source is currently unavailable.'
    return result, warnings


def log_payload(workflow_id, run_id, job_id=None):
    root, jobs = execution_rows(workflow_id, run_id, job_id)
    selected = jobs[0] if job_id else root
    sources, warnings = sources_for(jobs)
    workflow = root.get('CONFIG_SNAPSHOT', {}).get('workflow', {})
    definition = selected.get('CONFIG_SNAPSHOT', {}).get('definition', {})
    name = definition.get('JOB_NAME') or workflow.get('WORKFLOW_NAME') or workflow_id
    history = dict(RUN_ID=selected['EXECUTION_ID'], WORKFLOW_ID=workflow_id, WORKFLOW_NAME=name,
                   WORKFLOW_TYPE='DBT' if any(row.get('CONFIG_SNAPSHOT', {}).get('definition', {}).get('JOB_TYPE') == 'DBT' for row in jobs) else 'SQL',
                   STATUS=selected['STATUS'], REQUESTED_BY=selected.get('REQUESTED_BY'), REQUESTED_AT=selected.get('REQUESTED_AT'),
                   START_TIME=selected.get('STARTED_AT'), END_TIME=selected.get('ENDED_AT'),
                   TRIGGER_SOURCE=selected.get('TRIGGER_SOURCE'), ERROR_MESSAGE=selected.get('ERROR_MESSAGE'))
    executions = [dict(JOB_NAME=row.get('CONFIG_SNAPSHOT', {}).get('definition', {}).get('JOB_NAME') or row['JOB_ID'],
                       JOB_POSITION=row.get('JOB_POSITION'), STATUS=row['STATUS'], STARTED_AT=row.get('STARTED_AT'),
                       ENDED_AT=row.get('ENDED_AT'), ERROR_MESSAGE=row.get('ERROR_MESSAGE'), QUERY_ID=row.get('QUERY_ID'),
                       EXECUTION_ID=row['EXECUTION_ID']) for row in jobs]
    return dict(ok=True, runId=root['EXECUTION_ID'], history=normalize_rows([history])[0],
                jobExecutions=normalize_rows(executions), warnings=warnings,
                **{key: normalize_rows(value) for key, value in sources.items()})


def dag_payload(workflow_id, run_id=None, job_id=None):
    root, jobs = execution_rows(workflow_id, run_id, job_id)
    if job_id and root:
        row = jobs[0]
        definition = row.get('CONFIG_SNAPSHOT', {}).get('definition', {})
        if definition.get('JOB_TYPE') == 'DBT':
            from kumo_repository import build_progress_dag
            sources, warnings = sources_for(jobs)
            errors = [row for row in sources['runLog'] if str(row.get('TYPE') or '').upper() in ('ERROR','FAILED','FAILURE','FATAL')]
            graph = build_progress_dag(workflow_id, definition.get('DBT_COMMAND'),
                                       dict(RUN_ID=root['EXECUTION_ID'],STATUS=row['STATUS']),
                                       sources['modelProgress'],sources['testProgress'],errors)
            graph.update(kind='models',jobId=job_id,warnings=list(warnings.values()))
            return graph
    if root:
        nodes = [dict(id=workflow_id,label=root.get('CONFIG_SNAPSHOT', {}).get('workflow', {}).get('WORKFLOW_NAME') or workflow_id,
                      status=root['STATUS'],type='WORKFLOW')]
        ids = {row['EXECUTION_ID']: row['JOB_ID'] for row in jobs}
        ids[root['EXECUTION_ID']] = workflow_id
        edges = []
        for row in jobs:
            definition = row.get('CONFIG_SNAPSHOT', {}).get('definition', {})
            nodes.append(dict(id=row['JOB_ID'],label=definition.get('JOB_NAME') or row['JOB_ID'],status=row['STATUS'],type=definition.get('JOB_TYPE')))
            edges.append(dict(source=ids.get(row.get('PARENT_EXECUTION_ID'),workflow_id),target=row['JOB_ID']))
        return dict(workflowId=workflow_id,kind='jobs',run=dict(RUN_ID=root['EXECUTION_ID'],STATUS=root['STATUS']),nodes=nodes,edges=edges,errors=[],tests=[])
    # A never-run workflow still has a useful configured job DAG.
    catalog = catalog_store.read_catalog()
    workflow = next((row for row in catalog['workflows'] if row['WORKFLOW_ID']==workflow_id),None)
    if workflow is None:
        raise ExecutionNotFound('Workflow not found')
    assignments = [row for row in catalog_store.assignments(catalog) if row['WORKFLOW_ID']==workflow_id]
    nodes=[dict(id=workflow_id,label=workflow['WORKFLOW_NAME'],status='NOT RUN',type='WORKFLOW')]
    nodes += [dict(id=row['JOB_ID'],label=row['JOB_NAME'],status='NOT RUN' if row.get('JOB_IS_ENABLED') else 'DISABLED',type=row.get('JOB_TYPE')) for row in assignments]
    return dict(workflowId=workflow_id,kind='jobs',run=None,nodes=nodes,
                edges=[dict(source=row.get('PARENT_JOB_ID') or workflow_id,target=row['JOB_ID']) for row in assignments],errors=[],tests=[])
