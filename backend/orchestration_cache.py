"""One shared Snowflake snapshot and SSE stream for the maintained Monitor."""
import logging
import threading
import time
import uuid
from datetime import datetime, timezone

import config
import snowflake_client as sf
from realtime_events import realtime_broker

logger = logging.getLogger(__name__)
ACTIVE = {'SUBMITTING', 'INITIATING', 'QUEUED', 'CLAIMED', 'RUNNING', 'UNKNOWN'}


class SubmissionConflict(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


class OrchestrationCache:
    def __init__(self):
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread = None
        self._enabled = False
        self._submissions = {}
        self._data = dict(ok=True, rows=[], warnings=[], warming=True, generatedAt=now())
        self._duration_ms = None
        self._revision = 0
        self._cache_id = str(uuid.uuid4())
        self.refresh_seconds = max(5, config.REFRESH_SECONDS)

    def start(self):
        if config.USE_MOCK or not sf.is_configured() or not realtime_broker.client_count():
            return
        with self._lock:
            self._enabled = True
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name='kumo-orchestration-refresh', daemon=True)
            self._thread.start()

    def stop(self):
        with self._lock:
            self._enabled = False
            self._wake.set()

    def refresh_async(self):
        self._wake.set()

    def set_refresh_seconds(self, seconds):
        with self._lock:
            self.refresh_seconds = max(5, int(seconds))
        self.refresh_async()

    def get(self):
        with self._lock:
            submissions = [dict(row) for row in self._submissions.values()]
            busy = {row['workflowId'] for row in submissions if row['status'] in ACTIVE}
            rows = [dict(row, canRun=False, runUnavailable='Workflow is being submitted or is active.')
                    if row['WORKFLOW_ID'] in busy else row for row in self._data['rows']]
            return dict(self._data, rows=rows, submissions=submissions, transport='sse',
                        revision=self._revision, cacheId=self._cache_id,
                        refreshSeconds=self.refresh_seconds)

    def _publish(self):
        # Called under the lock so a slower refresh cannot publish an older
        # submission state after a newer receipt has already been sent.
        self._revision += 1
        realtime_broker.publish('orchestration_update', self.get())

    def reserve(self, workflow_id, payload, actor):
        key = (workflow_id, payload['requestId'])
        with self._lock:
            previous = self._submissions.get(key)
            if previous and previous['status'] in ACTIVE:
                if (previous['mode'], previous.get('jobId'), previous.get('fullRefresh', False), previous['requestedBy']) != (payload['mode'], payload.get('jobId'), payload.get('fullRefresh', False), actor):
                    raise SubmissionConflict('Request ID already belongs to a different request.')
                if previous.get('queryId'):
                    return dict(previous, ok=True, accepted=True, reused=True)
                raise SubmissionConflict('This request is already being submitted.')
            if any(row['workflowId'] == workflow_id and row['status'] in ACTIVE for row in self._submissions.values()):
                raise SubmissionConflict('Workflow is already being submitted or is active.')
            workflow = next((row for row in self._data['rows'] if row['WORKFLOW_ID'] == workflow_id), None)
            if workflow and workflow['run']['status'] in {'INITIATING', 'QUEUED', 'CLAIMED', 'RUNNING'}:
                raise SubmissionConflict('Workflow is already queued or running.')
            self._submissions[key] = dict(workflowId=workflow_id, requestId=payload['requestId'], mode=payload['mode'],
                                         jobId=payload.get('jobId'), fullRefresh=payload.get('fullRefresh', False), requestedBy=actor, status='SUBMITTING', submittedAt=now())
            self._publish()
        return None

    def update_submission(self, workflow_id, request_id, **values):
        with self._lock:
            key = (workflow_id, request_id)
            if key not in self._submissions:
                return
            self._submissions[key] = dict(self._submissions[key], **values)
            self._publish()
        self.refresh_async()

    def refresh(self):
        if not realtime_broker.client_count():
            return self.get()
        from orchestration_monitor import payload
        started = time.monotonic()
        try:
            data = payload()
            with self._lock:
                self._data = dict(data, warming=False, error=None)
                # The durable log supersedes the short-lived submission receipt.
                known = {(row['WORKFLOW_ID'], row['run'].get('requestId')) for row in data['rows']}
                self._submissions = {key: row for key, row in self._submissions.items()
                                     if key not in known and (row['status'] in ACTIVE or
                                         (datetime.now(timezone.utc) - datetime.fromisoformat(row['submittedAt'])).total_seconds() < 300)}
                self._duration_ms = round((time.monotonic() - started) * 1000)
                self._publish()
        except Exception as exc:
            logger.warning('Orchestration shared refresh failed: %s', exc)
            with self._lock:
                self._data = dict(self._data, error=str(exc))
                self._publish()
        return self.get()

    def _loop(self):
        while True:
            with self._lock:
                if not self._enabled or not realtime_broker.client_count():
                    self._enabled = False
                    self._thread = None
                    return
            self._wake.clear()
            started = time.monotonic()
            self.refresh()
            self._wake.wait(max(.5, self.refresh_seconds - (time.monotonic() - started)))

    def diagnostics(self):
        with self._lock:
            return dict(enabled=self._enabled, threadAlive=bool(self._thread and self._thread.is_alive()),
                        refreshSeconds=self.refresh_seconds,
                        lastDurationMs=self._duration_ms, lastRefreshAt=self._data.get('generatedAt'),
                        lastError=self._data.get('error'), pendingSubmissions=len(self._submissions))


orchestration_cache = OrchestrationCache()
