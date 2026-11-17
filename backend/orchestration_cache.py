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
        self._metadata_dirty = threading.Event()
        self._metadata = {}
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

    def refresh_async(self, metadata=True):
        with self._lock:
            if metadata:
                self._metadata_dirty.set()
            self._wake.set()

    def set_refresh_seconds(self, seconds):
        with self._lock:
            self.refresh_seconds = max(5, int(seconds))
        self.refresh_async(metadata=False)

    def get(self):
        with self._lock:
            submissions = [dict(row) for row in self._submissions.values()]
            busy = {row['workflowId'] for row in submissions if row['status'] in ACTIVE}
            rows = [dict(row, canRun=False, runUnavailable='Workflow is being submitted or is active.')
                    if row['WORKFLOW_ID'] in busy else row for row in self._data['rows']]
            return dict(self._data, rows=rows, submissions=submissions, transport='sse',
                        revision=self._revision, cacheId=self._cache_id,
                        refreshSeconds=self._poll_seconds())

    def _poll_seconds(self):
        # One shared reader polls faster while a run is being submitted/queued
        # or executing. Idle readers retain the configured refresh interval.
        with self._lock:
            active = any(row['run']['status'] in ACTIVE for row in self._data['rows']) or any(
                row['status'] in ACTIVE for row in self._submissions.values())
            return min(2, self.refresh_seconds) if active else self.refresh_seconds

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
        self.refresh_async(metadata=False)

    def refresh(self):
        if not realtime_broker.client_count():
            return self.get()
        from orchestration_monitor import payload
        started = time.monotonic()
        try:
            with self._lock:
                if self._metadata_dirty.is_set():
                    self._metadata_dirty.clear()
                    self._metadata.clear()
            data = payload(metadata=self._metadata)
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
            self._refresh_error(exc)
        return self.get()

    def _refresh_error(self, exc):
        logger.warning('Orchestration shared refresh failed: %s', exc)
        with self._lock:
            self._data = dict(self._data, error=str(exc))
            self._publish()

    def _has_clients(self):
        with self._lock:
            return self._enabled and bool(realtime_broker.client_count())

    def _loop(self):
        try:
            while self._has_clients():
                try:
                    # Reuse authentication/session setup across polls. Bound the
                    # lifetime, close on disconnect and reconnect after a failure.
                    with sf.connection_scope(force_service=True):
                        expires = time.monotonic() + 300
                        while self._has_clients() and time.monotonic() < expires:
                            self._wake.clear()
                            started = time.monotonic()
                            if self.refresh().get('error'):
                                break
                            self._wake.wait(max(.5, self._poll_seconds() - (time.monotonic() - started)))
                except Exception as exc:
                    self._refresh_error(exc)
                if self.get().get('error') and self._has_clients():
                    self._wake.wait(self.refresh_seconds)
        finally:
            with self._lock:
                self._thread = None
                restart = self._enabled and bool(realtime_broker.client_count())
                if not restart:
                    self._enabled = False
            if restart:
                self.start()

    def diagnostics(self):
        with self._lock:
            return dict(enabled=self._enabled, threadAlive=bool(self._thread and self._thread.is_alive()),
                        refreshSeconds=self._poll_seconds(), idleRefreshSeconds=self.refresh_seconds,
                        lastDurationMs=self._duration_ms, lastRefreshAt=self._data.get('generatedAt'),
                        lastError=self._data.get('error'), pendingSubmissions=len(self._submissions))


orchestration_cache = OrchestrationCache()
