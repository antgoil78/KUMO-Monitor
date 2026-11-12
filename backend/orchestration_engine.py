"""Parallel execution API. No use of the existing engine's repository or state."""
from contextlib import nullcontext
import json
import uuid
import logging
import threading
from snowflake.connector.errors import ProgrammingError

from flask import Blueprint, current_app, jsonify, request

import config
import snowflake_client as sf
from orchestration_cache import orchestration_cache, SubmissionConflict
from realtime_events import realtime_broker

orchestration_engine_bp = Blueprint('orchestration_engine', __name__)
MODES = {'WORKFLOW', 'JOB_ONLY', 'FROM_JOB'}


@orchestration_engine_bp.route('/api/orchestration/history')
def execution_history():
    from orchestration_history import filters, history_cache
    if config.USE_MOCK or not sf.is_configured():
        return jsonify(ok=False, error='Connect to Snowflake to view execution history.'), 503
    try:
        return jsonify(history_cache.get(filters(request.args), refresh=request.args.get('refresh')=='1'))
    except (ValueError, TypeError) as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500


@orchestration_engine_bp.route('/api/orchestration/monitor')
def monitor():
    if config.USE_MOCK or not sf.is_configured():
        return jsonify(ok=False, error='Connect to Snowflake to monitor workflow and job executions.'), 503
    try:
        if request.args.get('refresh') == '1':
            orchestration_cache.refresh_async()
        return jsonify(orchestration_cache.get())
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500


def task_fqn(workflow_id, run_id):
    workflow_id = str(uuid.UUID(workflow_id))
    run_id = str(uuid.UUID(run_id))
    return config.APP_NAMESPACE + '.TMP_ORCH_WF_' + workflow_id.replace('-', '_').upper() + '_' + run_id.replace('-', '_').upper()


def call_result(sql, params):
    row = sf.query_one_service(sql, params)
    return decode_result(row)


def decode_result(row):
    result = next(iter(row.values()), {})
    result = json.loads(result) if isinstance(result, str) else result
    if not isinstance(result, dict) or not result.get('ok'):
        raise ValueError(result.get('error', 'Parallel engine operation failed') if isinstance(result, dict) else 'Invalid engine response')
    return result


def installed():
    if config.USE_MOCK or not sf.is_configured():
        return False
    row = sf.query_one_service(f"SELECT COUNT(*) AS N FROM {config.DB}.INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = %(schema)s AND TABLE_NAME = 'WORKFLOW_EXECUTION_LOG'", {'schema': config.METADATA_SCHEMA})
    return bool(row.get('N'))


def require_runs():
    if config.USE_MOCK or not sf.is_configured() or not config.ORCHESTRATION_RUNS_ENABLED:
        raise PermissionError('Workflow execution controls are not enabled in this environment.')


def request_run(workflow_id, payload, requested_by):
    require_runs()
    workflow_id = str(uuid.UUID(workflow_id))
    if not isinstance(payload, dict):
        raise ValueError('A JSON object is required')
    mode = payload.get('mode', 'WORKFLOW')
    job_id = payload.get('jobId')
    request_id = payload.get('requestId')
    if mode not in MODES or (mode == 'WORKFLOW' and job_id) or (mode != 'WORKFLOW' and not job_id):
        raise ValueError('Choose WORKFLOW, JOB_ONLY or FROM_JOB and a matching job selection')
    if job_id:
        job_id = str(uuid.UUID(job_id))
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
        raise ValueError('A request ID of at most 128 characters is required')
    request_id = request_id.strip()
    previous = orchestration_cache.reserve(workflow_id, dict(mode=mode, jobId=job_id, requestId=request_id), requested_by)
    if previous:
        return previous
    try:
        query_id = sf.submit_service_query(
            f'CALL {config.APP_NAMESPACE}.SP_ORCH_REQUEST_RUN(%(workflow)s,%(mode)s,%(job)s,%(request)s,%(actor)s)',
            dict(workflow=workflow_id, mode=mode, job=job_id, request=request_id, actor=requested_by))
    except Exception as exc:
        orchestration_cache.update_submission(workflow_id, request_id, status='FAILED', error=str(exc))
        raise
    receipt = dict(ok=True, accepted=True, workflowId=workflow_id, requestId=request_id, queryId=query_id,
                   status='INITIATING', mode=mode, jobId=job_id, requestedBy=requested_by)
    orchestration_cache.update_submission(workflow_id, request_id, status='INITIATING', queryId=query_id)
    threading.Thread(target=complete_submission, args=(workflow_id, request_id, query_id),
                     name='kumo-orchestration-submit', daemon=True).start()
    return receipt


def complete_submission(workflow_id, request_id, query_id):
    # Snowflake already owns the preparation query. The static maintenance task
    # can start its committed run even if this process restarts before completion.
    attempt = 0
    while True:
        realtime_broker.wait_for_clients()
        try:
            row = sf.service_query_result(query_id)
            break
        except ProgrammingError as exc:
            orchestration_cache.update_submission(workflow_id, request_id, status='FAILED', error=str(exc))
            return
        except Exception as exc:
            attempt += 1
            if attempt >= 3:
                orchestration_cache.update_submission(workflow_id, request_id, status='UNKNOWN',
                    error='Snowflake accepted the request; confirmation is delayed. Retrying in the background.')
            logging.getLogger(__name__).warning('Retrying workflow submission confirmation: %s', exc)
            threading.Event().wait(min(30, 2 ** min(attempt, 5)))
    try:
        result = decode_result(row)
        if result.get('taskFqn') != task_fqn(workflow_id, result['runId']):
            raise ValueError('Unexpected workflow task returned by the engine')
    except (ValueError, KeyError, TypeError) as exc:
        orchestration_cache.update_submission(workflow_id, request_id, status='FAILED', error=str(exc))
        return
    orchestration_cache.update_submission(workflow_id, request_id, status=result['status'], runId=result['runId'], error=None)
    if result['status'] in ('QUEUED', 'CLAIMED'):
        realtime_broker.wait_for_clients()
        try:
            call_result(f'CALL {config.APP_NAMESPACE}.SP_ORCH_START_RUN(%(run)s)', {'run': result['runId']})
        except Exception as exc:
            # Keep the committed request visible; native maintenance retries it.
            logging.getLogger(__name__).warning('Workflow task start deferred to maintenance: %s', exc)
    orchestration_cache.refresh_async()


@orchestration_engine_bp.route('/api/orchestration/engine')
def engine_status():
    try:
        with sf.connection_scope(force_service=True) if not config.USE_MOCK and sf.is_configured() else nullcontext():
            ready = installed()
            count = sf.query_one_service(f'SELECT COUNT(*) AS N FROM {config.T_ORCHESTRATION_WORKFLOW}').get('N', 0) if ready else 0
        return jsonify(ok=True, installed=ready, runsEnabled=ready and config.ORCHESTRATION_RUNS_ENABLED,
                       preparedWorkflows=count, schedulingEnabled=ready and config.ORCHESTRATION_RUNS_ENABLED, taskLifecycle='TEMPORARY',
                       message='Manual execution is enabled.' if ready and config.ORCHESTRATION_RUNS_ENABLED else
                       'Parallel engine prepared; manual execution is disabled.' if ready else 'Parallel engine has not been prepared.')
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 503


@orchestration_engine_bp.route('/api/orchestration/workflows/<workflow_id>/runs', methods=['GET', 'POST'])
def workflow_runs(workflow_id):
    try:
        workflow_id = str(uuid.UUID(workflow_id))
        if request.method == 'POST':
            # Resolve identity using the same service/caller rules as the app;
            # never accept a browser-supplied REQUESTED_BY value.
            require_runs()
            actor = current_app.extensions['orchestration_actor_context']()
            result = request_run(workflow_id, request.get_json(silent=True), actor.get('userName') or 'UNKNOWN')
            return jsonify(result), 202
        if config.USE_MOCK or not sf.is_configured():
            raise PermissionError('Connect to Snowflake to view parallel executions.')
        limit = max(1, min(100, int(request.args.get('limit', 20))))
        with sf.connection_scope(force_service=True):
            rows = sf.query_service(f'''SELECT * FROM {config.T_ORCHESTRATION_EXECUTION_LOG}
                WHERE WORKFLOW_ID = %(workflow)s AND WORKFLOW_RUN_ID IN (
                    SELECT EXECUTION_ID FROM {config.T_ORCHESTRATION_EXECUTION_LOG}
                    WHERE WORKFLOW_ID = %(workflow)s AND JOB_ID IS NULL
                    ORDER BY REQUESTED_AT DESC, EXECUTION_ID DESC LIMIT {limit})
                ORDER BY REQUESTED_AT DESC, WORKFLOW_RUN_ID, JOB_POSITION NULLS FIRST''', {'workflow': workflow_id})
        from utils import normalize_rows
        for row in rows:
            for key in ('CONFIG_SNAPSHOT', 'RESULT'):
                if isinstance(row.get(key), str):
                    row[key] = json.loads(row[key])
        return jsonify(ok=True, rows=normalize_rows(rows), source=config.T_ORCHESTRATION_EXECUTION_LOG)
    except SubmissionConflict as exc:
        return jsonify(ok=False, error=str(exc)), 409
    except PermissionError as exc:
        return jsonify(ok=False, error=str(exc)), 503
    except (ValueError, TypeError, AttributeError) as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500


@orchestration_engine_bp.route('/api/orchestration/workflows/<workflow_id>/reconcile', methods=['POST'])
def reconcile_workflow(workflow_id):
    try:
        workflow_id = str(uuid.UUID(workflow_id))
        return jsonify(call_result(f'CALL {config.APP_NAMESPACE}.SP_ORCH_RECONCILE(%(workflow)s)', {'workflow': workflow_id}))
    except (ValueError, TypeError, AttributeError) as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500


def save_quick_state(kind, entity_id, body):
    """Change one flag without overwriting another user's definition edits."""
    import orchestration_configuration as catalog_store
    from orchestration_schedules import ScheduleConflict, schedule_values, sync_saved_workflow
    require_runs()
    entity_id = str(uuid.UUID(entity_id))
    if not isinstance(body, dict) or not isinstance(body.get('enabled'), bool):
        raise ValueError('enabled must be a boolean')
    expected = body.get('expectedEnabled')
    if expected is not None and not isinstance(expected, bool):
        raise ValueError('expectedEnabled must be a boolean')
    table = config.T_ORCHESTRATION_JOB if kind == 'job' else config.T_ORCHESTRATION_WORKFLOW
    key = 'JOB_ID' if kind == 'job' else 'WORKFLOW_ID'
    flag = 'IS_SCHEDULE_ENABLED' if kind == 'schedule' else 'IS_ENABLED'
    with sf.connection_scope(force_service=True):
        sf.execute_service('BEGIN')
        try:
            catalog_store.lock_configuration()
            rows = sf.query_service(f'SELECT * FROM {table} WHERE {key}=%(id)s', {'id':entity_id})
            if len(rows) != 1:
                raise LookupError('Job not found' if kind == 'job' else 'Workflow not found')
            previous = rows[0]
            if expected is not None and previous[flag] != expected:
                raise ScheduleConflict('The state changed in another session. Refresh and try again.')
            params = dict(id=entity_id, enabled=body['enabled'])
            if kind == 'schedule':
                cron = body.get('cron', previous.get('SCHEDULE'))
                timezone = body.get('timezone', previous.get('SCHEDULE_TIMEZONE') or 'UTC')
                if body['enabled']:
                    cron, timezone = schedule_values(cron, timezone)
                params.update(cron=cron, timezone=timezone)
                sf.execute_service(f'''UPDATE {table} SET IS_SCHEDULE_ENABLED=%(enabled)s,
                    SCHEDULE=%(cron)s, SCHEDULE_TIMEZONE=%(timezone)s,
                    EXTERNAL_TRIGGERS=ARRAY_CONSTRUCT_COMPACT(IFF(%(enabled)s,'SCHEDULE',NULL),IFF(ALLOW_EXTERNAL_TRIGGER,'EXTERNAL',NULL)),
                    UPDATED_AT=CURRENT_TIMESTAMP(),UPDATED_BY=CURRENT_USER() WHERE {key}=%(id)s''',params)
            else:
                sf.execute_service(f'UPDATE {table} SET IS_ENABLED=%(enabled)s,UPDATED_AT=CURRENT_TIMESTAMP(),UPDATED_BY=CURRENT_USER() WHERE {key}=%(id)s',params)
            sf.execute_service('COMMIT')
        except Exception:
            sf.execute_service('ROLLBACK')
            raise
        result = dict(ok=True, enabled=body['enabled'], entityId=entity_id)
        if kind != 'job':
            try:
                result['scheduler'] = sync_saved_workflow(entity_id)
            except Exception as exc:
                result.update(scheduleSynced=False,warning='State saved, but the scheduler could not be synchronized: ' + str(exc))
        return result


@orchestration_engine_bp.route('/api/orchestration/workflows/<workflow_id>/state',methods=['PATCH'])
@orchestration_engine_bp.route('/api/orchestration/workflows/<workflow_id>/schedule',methods=['PATCH'])
def workflow_quick_state(workflow_id):
    from orchestration_schedules import ScheduleConflict
    try:
        return jsonify(save_quick_state('schedule' if request.path.endswith('/schedule') else 'workflow',workflow_id,request.get_json(silent=True)))
    except PermissionError as exc:
        return jsonify(ok=False,error=str(exc)),503
    except LookupError as exc:
        return jsonify(ok=False,error=str(exc)),404
    except ScheduleConflict as exc:
        return jsonify(ok=False,error=str(exc)),409
    except (ValueError,TypeError) as exc:
        return jsonify(ok=False,error=str(exc)),400
    except Exception as exc:
        return jsonify(ok=False,error=str(exc)),500


@orchestration_engine_bp.route('/api/orchestration/jobs/<job_id>/state',methods=['PATCH'])
def job_quick_state(job_id):
    try:
        return jsonify(save_quick_state('job',job_id,request.get_json(silent=True)))
    except PermissionError as exc:
        return jsonify(ok=False,error=str(exc)),503
    except LookupError as exc:
        return jsonify(ok=False,error=str(exc)),404
    except (ValueError,TypeError) as exc:
        from orchestration_schedules import ScheduleConflict
        return jsonify(ok=False,error=str(exc)),409 if isinstance(exc,ScheduleConflict) else 400
    except Exception as exc:
        return jsonify(ok=False,error=str(exc)),500


@orchestration_engine_bp.route('/api/orchestration/jobs/<job_id>/dag-preview', methods=['POST'])
def job_dag_preview(job_id):
    from kumo_repository import preview_dbt_definition
    try:
        if config.USE_MOCK or not sf.is_configured():
            raise PermissionError('Connect to Snowflake to preview the saved DBT selection.')
        job_id = str(uuid.UUID(job_id))
        with sf.connection_scope(force_service=True):
            rows = sf.query_service(f'SELECT * FROM {config.T_ORCHESTRATION_JOB} WHERE JOB_ID=%(job)s', {'job': job_id})
            if not rows:
                return jsonify(ok=False, error='Job not found'), 404
            job = rows[0]
            if job.get('JOB_TYPE') != 'DBT':
                raise ValueError('DAG preview is only available for DBT jobs.')
            definition = dict(job, WORKFLOW_ID=job_id, WORKFLOW_NAME=job['JOB_NAME'], WORKFLOW_TYPE='DBT')
            return jsonify(ok=True, **preview_dbt_definition(definition))
    except PermissionError as exc:
        return jsonify(ok=False, error=str(exc)), 503
    except (ValueError, TypeError) as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500


@orchestration_engine_bp.route('/api/orchestration/workflows/<workflow_id>/dag')
@orchestration_engine_bp.route('/api/orchestration/workflows/<workflow_id>/log')
def execution_detail(workflow_id):
    from orchestration_observability import ExecutionNotFound,dag_payload,log_payload
    try:
        if config.USE_MOCK or not sf.is_configured():
            raise PermissionError('Connect to Snowflake to view execution details.')
        workflow_id=str(uuid.UUID(workflow_id))
        run_id=str(uuid.UUID(request.args['runId'])) if request.args.get('runId') else None
        job_id=str(uuid.UUID(request.args['jobId'])) if request.args.get('jobId') else None
        with sf.connection_scope(force_service=True):
            if request.path.endswith('/log'):
                if not run_id:
                    raise ValueError('A workflow execution ID is required')
                return jsonify(log_payload(workflow_id,run_id,job_id))
            return jsonify(ok=True, **dag_payload(workflow_id,run_id,job_id))
    except PermissionError as exc:
        return jsonify(ok=False,error=str(exc)),503
    except ExecutionNotFound as exc:
        return jsonify(ok=False,error=str(exc)),404
    except (ValueError,TypeError) as exc:
        return jsonify(ok=False,error=str(exc)),400
    except Exception as exc:
        return jsonify(ok=False,error=str(exc)),500
