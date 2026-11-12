"""Bounded, on-demand history from the new engine's immutable execution snapshots."""
import base64
from collections import OrderedDict
from datetime import datetime, timezone
import json
import threading
import time
import uuid

import config
from orchestration_monitor import run_state
import snowflake_client as sf
from utils import to_jsonable

STATUSES = {'QUEUED', 'CLAIMED', 'RUNNING', 'INITIATING', 'PENDING', 'SUCCESS', 'WARNING', 'FAILED', 'SKIPPED', 'CANCELLED'}


def filters(args):
    values = {key: str(args.get(key) or '').strip() for key in ('workflowId', 'jobId', 'status', 'search', 'cursor')}
    for key in ('workflowId', 'jobId'):
        if values[key]: values[key] = str(uuid.UUID(values[key]))
    if values['status'] and values['status'] not in STATUSES:
        raise ValueError('Invalid execution status')
    if len(values['search']) > 200:
        raise ValueError('Search must be at most 200 characters')
    values['days'] = int(args.get('days', 30))
    values['limit'] = int(args.get('limit', 25))
    if values['days'] not in (0, 1, 7, 30, 90) or not 1 <= values['limit'] <= 100:
        raise ValueError('Choose a valid date range and page size (1–100)')
    bounds = [str(args.get(key) or '').strip() for key in ('start', 'end')]
    if any(bounds):
        try:
            start, end = [datetime.fromisoformat(value.replace('Z', '+00:00')) for value in bounds]
            if start.tzinfo is None or end.tzinfo is None: raise ValueError()
            start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
            if not 0 < (end - start).total_seconds() <= 31 * 86400: raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('Choose a start and end with timezones, up to 31 days apart') from None
        values.update(start=start.isoformat(), end=end.isoformat())
    if values['cursor']: decode_cursor(values['cursor'])
    return values


def decode_cursor(cursor):
    try:
        if len(cursor) > 512: raise ValueError()
        value = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True))
        timestamp = datetime.fromisoformat(value['at'])
        if timestamp.tzinfo is None: raise ValueError()
        return timestamp.astimezone(timezone.utc).replace(tzinfo=None), str(uuid.UUID(value['id']))
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError('Invalid history page cursor') from None


def encode_cursor(row):
    value = dict(at=to_jsonable(row['REQUESTED_AT']), id=row['EXECUTION_ID'])
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')


def read_history(values):
    log = config.T_ORCHESTRATION_EXECUTION_LOG
    where = ['l.JOB_ID IS NULL']
    params = {}
    if values['workflowId']:
        where.append('l.WORKFLOW_ID=%(workflow)s'); params['workflow'] = values['workflowId']
    if values.get('start'):
        # Include executions that began before the viewport and still overlap it.
        where.extend(['COALESCE(l.STARTED_AT,l.REQUESTED_AT) <= %(rangeEnd)s',
                      """COALESCE(l.ENDED_AT,CASE WHEN l.STATUS IN ('QUEUED','CLAIMED','RUNNING','INITIATING','PENDING')
                          THEN SYSDATE() ELSE COALESCE(l.STARTED_AT,l.REQUESTED_AT) END) >= %(rangeStart)s"""])
        params.update(rangeStart=datetime.fromisoformat(values['start']).replace(tzinfo=None),
                      rangeEnd=datetime.fromisoformat(values['end']).replace(tzinfo=None))
    elif values['days']:
        where.append('l.REQUESTED_AT >= DATEADD(day, -%(days)s, SYSDATE())'); params['days'] = values['days']
    job_conditions = ['j.WORKFLOW_ID=l.WORKFLOW_ID', 'j.WORKFLOW_RUN_ID=l.EXECUTION_ID', 'j.JOB_ID IS NOT NULL']
    if values['jobId']:
        job_conditions.append('j.JOB_ID=%(job)s'); params['job'] = values['jobId']
    if values['status']:
        if values['jobId']: job_conditions.append('j.STATUS=%(status)s')
        else: where.append('l.STATUS=%(status)s')
        params['status'] = values['status']
    if values['jobId']:
        where.append(f'EXISTS (SELECT 1 FROM {log} j WHERE ' + ' AND '.join(job_conditions) + ')')
    if values['search']:
        params['search'] = '%' + values['search'].replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
        where.append(f'''(COALESCE(l.CONFIG_SNAPSHOT:workflow:WORKFLOW_NAME::VARCHAR,l.WORKFLOW_ID) ILIKE %(search)s ESCAPE '!'
            OR l.EXECUTION_ID ILIKE %(search)s ESCAPE '!' OR l.REQUESTED_BY ILIKE %(search)s ESCAPE '!'
            OR EXISTS (SELECT 1 FROM {log} j WHERE j.WORKFLOW_ID=l.WORKFLOW_ID AND j.WORKFLOW_RUN_ID=l.EXECUTION_ID
                AND j.CONFIG_SNAPSHOT:definition:JOB_NAME::VARCHAR ILIKE %(search)s ESCAPE '!'))''')
    if values['cursor']:
        params['before'], params['beforeId'] = decode_cursor(values['cursor'])
        where.append('(l.REQUESTED_AT < %(before)s OR (l.REQUESTED_AT=%(before)s AND l.EXECUTION_ID < %(beforeId)s))')
    with sf.connection_scope(force_service=True):
        roots = sf.query_service(f'''SELECT l.EXECUTION_ID,l.WORKFLOW_RUN_ID,l.WORKFLOW_ID,l.PARENT_EXECUTION_ID,l.STATUS,l.RUN_MODE,
            l.REQUEST_ID,l.REQUESTED_JOB_ID,l.REQUESTED_BY,l.REQUESTED_AT,l.STARTED_AT,l.ENDED_AT,
            l.TRIGGER_SOURCE,l.ERROR_MESSAGE,l.QUERY_ID,
            l.CONFIG_SNAPSHOT:workflow:WORKFLOW_NAME::VARCHAR AS WORKFLOW_NAME,
            l.CONFIG_SNAPSHOT:workflow:WORKFLOW_GROUP::VARCHAR AS WORKFLOW_GROUP
            FROM {log} l WHERE {' AND '.join(where)}
            ORDER BY l.REQUESTED_AT DESC,l.EXECUTION_ID DESC LIMIT {values['limit'] + 1}''', params)
        more = len(roots) > values['limit']; roots = roots[:values['limit']]
        jobs = []
        if roots:
            ids = {f'run_{index}': row['EXECUTION_ID'] for index,row in enumerate(roots)}
            placeholders = ','.join(f'%({key})s' for key in ids)
            jobs = sf.query_service(f'''SELECT EXECUTION_ID,WORKFLOW_RUN_ID,WORKFLOW_ID,JOB_ID,JOB_POSITION,PARENT_EXECUTION_ID,
                STATUS,RUN_MODE,REQUEST_ID,REQUESTED_BY,REQUESTED_AT,STARTED_AT,ENDED_AT,ERROR_MESSAGE,QUERY_ID,
                CONFIG_SNAPSHOT:definition:JOB_NAME::VARCHAR AS JOB_NAME,
                CONFIG_SNAPSHOT:definition:JOB_TYPE::VARCHAR AS JOB_TYPE,
                CONFIG_SNAPSHOT:definition:JOB_LAYER::VARCHAR AS JOB_LAYER,
                CONFIG_SNAPSHOT:selected::BOOLEAN AS RUN_SELECTED
                FROM {log} WHERE JOB_ID IS NOT NULL AND WORKFLOW_RUN_ID IN ({placeholders})
                ORDER BY WORKFLOW_RUN_ID,JOB_POSITION,EXECUTION_ID''', ids)
    runs = []
    for row in roots:
        children = [dict(run_state(job), jobId=job['JOB_ID'], name=job.get('JOB_NAME') or job['JOB_ID'],
                         type=job.get('JOB_TYPE'), layer=job.get('JOB_LAYER'))
                    for job in jobs if job['WORKFLOW_ID']==row['WORKFLOW_ID'] and job['WORKFLOW_RUN_ID']==row['EXECUTION_ID']]
        runs.append(dict(run_state(row), workflowId=row['WORKFLOW_ID'], workflowName=row.get('WORKFLOW_NAME') or row['WORKFLOW_ID'],
                         workflowGroup=row.get('WORKFLOW_GROUP'), triggerSource=row.get('TRIGGER_SOURCE'), jobs=children))
    return dict(ok=True,runs=runs,nextCursor=encode_cursor(roots[-1]) if more else None,
                generatedAt=datetime.now(timezone.utc).isoformat(),source=log)


class HistoryCache:
    """Coalesce identical history reads; no timers or background Snowflake queries."""
    def __init__(self):
        self._condition = threading.Condition()
        self._busy = set()
        self._entries = OrderedDict()

    def get(self, values, refresh=False):
        key = json.dumps(values, sort_keys=True)
        with self._condition:
            waited = False
            while key in self._busy:
                waited = True; self._condition.wait()
            previous = self._entries.get(key)
            if previous and previous[0] > time.monotonic() and (not refresh or waited):
                self._entries.move_to_end(key)
                if previous[2]: raise RuntimeError(previous[2])
                return previous[1]
            self._busy.add(key)
        try:
            result = read_history(values)
        except Exception as exc:
            with self._condition:
                self._entries[key] = (time.monotonic()+3, None, str(exc))
            raise
        else:
            with self._condition:
                self._entries[key] = (time.monotonic()+15, result, None)
            return result
        finally:
            with self._condition:
                while len(self._entries)>64: self._entries.popitem(last=False)
                self._busy.discard(key); self._condition.notify_all()


history_cache = HistoryCache()
