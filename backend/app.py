import json
import logging
import os
import socket
import time
import uuid
from datetime import datetime, timezone
from threading import Event, Lock, RLock, Thread
from flask import Flask, Response, jsonify, request, send_from_directory, g, stream_with_context, has_request_context
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
from dotenv import load_dotenv

load_dotenv()

import config
import kumo_repository as repo
from monitor_cache import monitor_cache
from mock_data import MOCK_HISTORY, MOCK_MONITOR
from realtime_events import realtime_broker
import snowflake_client as sf

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)
logging.basicConfig(level=logging.INFO)

_RUNTIME_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
_active_users_lock = Lock()
_active_users = {}
_live_run_locks_lock = Lock()
_live_run_locks = {}
_shared_run_locks_lock = Lock()
_shared_run_locks = {}
_recent_run_events_lock = Lock()
_recent_run_events = {}
_run_event_sequences_lock = Lock()
_run_event_sequences = {}
_RECENT_RUN_EVENT_TTL_SECONDS = 180
_TERMINAL_RUN_STATUSES = {
    "SUCCESS", "SUCCEEDED", "COMPLETED", "OK",
    "FAILED", "FAILURE", "ERROR", "CANCELLED", "CANCELED", "SKIPPED",
}
_RUN_STATUS_RANK = {
    "INITIATING": 10,
    "REQUESTED": 20,
    "PENDING": 20,
    "SCHEDULED": 20,
    "QUEUED": 20,
    "STARTING": 30,
    "RUNNING": 30,
    "IN_PROGRESS": 30,
    "EXECUTING": 30,
    "SUCCESS": 40,
    "SUCCEEDED": 40,
    "COMPLETED": 40,
    "OK": 40,
    "FAILED": 40,
    "FAILURE": 40,
    "ERROR": 40,
    "CANCELLED": 40,
    "CANCELED": 40,
    "SKIPPED": 40,
}
_dashboard_cache_lock = Lock()
_dashboard_cache_wake = Event()
_dashboard_cache_thread = None
_dashboard_cache = {
    "ping": {"ok": False, "mode": sf.connection_mode(), "error": "Dashboard cache is starting"},
    "activeUsers": {"ok": True, "source": "starting", "users": [], "count": 0},
    "generatedAt": None,
    "durationMs": None,
    "refreshing": False,
    "error": None,
}
_dashboard_cache_enabled = False


def _build_info():
    return {
        "buildSha": os.getenv("KUMO_BUILD_SHA", "").strip() or "local",
        "runtimeId": _RUNTIME_ID,
    }


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _next_run_event_sequence(workflow_id):
    workflow_id = str(workflow_id or "")
    with _run_event_sequences_lock:
        current = int(_run_event_sequences.get(workflow_id) or 0) + 1
        _run_event_sequences[workflow_id] = current
        return current


def _remember_run_event(event):
    workflow_id = str((event or {}).get("workflowId") or "")
    if not workflow_id:
        return event
    if event.get("sequence") is None:
        event["sequence"] = _next_run_event_sequence(workflow_id)
    item = dict(event or {})
    item["rememberedAt"] = _now_iso()
    item["_rememberedMonotonic"] = time.monotonic()
    with _recent_run_events_lock:
        _recent_run_events[workflow_id] = item
        _prune_recent_run_events_locked()
    return item


def _publish_run_event(event_type, event):
    prepared = _remember_run_event(event)
    realtime_broker.publish(event_type, prepared)
    return prepared


def _prune_recent_run_events_locked():
    now = time.monotonic()
    for workflow_id, event in list(_recent_run_events.items()):
        if now - float(event.get("_rememberedMonotonic") or now) > _RECENT_RUN_EVENT_TTL_SECONDS:
            _recent_run_events.pop(workflow_id, None)


def _recent_run_event_list():
    with _recent_run_events_lock:
        _prune_recent_run_events_locked()
        return [
            {k: v for k, v in event.items() if not str(k).startswith("_")}
            for event in _recent_run_events.values()
        ]


def _upsert_live_run_lock(lock):
    if not lock or not lock.get("workflowId"):
        return None
    item = dict(lock)
    item["updatedAt"] = _now_iso()
    item["_updatedMonotonic"] = time.monotonic()
    with _live_run_locks_lock:
        previous = _live_run_locks.get(str(item["workflowId"]), {})
        previous_status = str(previous.get("status") or "").upper()
        next_status = str(item.get("status") or "").upper()
        previous_run = str(previous.get("runId") or "")
        next_run = str(item.get("runId") or previous_run or "")
        if (
            previous_status
            and next_status
            and previous_run == next_run
            and _RUN_STATUS_RANK.get(previous_status, 0) > _RUN_STATUS_RANK.get(next_status, 0)
        ):
            item["status"] = previous_status
        _live_run_locks[str(item["workflowId"])] = {**previous, **item}
        return dict(_live_run_locks[str(item["workflowId"])])


def _active_local_run_lock(workflow_id):
    workflow_id = str(workflow_id or "")
    if not workflow_id:
        return None
    with _live_run_locks_lock:
        lock = dict(_live_run_locks.get(workflow_id) or {})
    if not lock:
        return None
    status = str(lock.get("status") or "").upper()
    if status in _TERMINAL_RUN_STATUSES:
        return None
    updated = float(lock.get("_updatedMonotonic") or time.monotonic())
    ttl_seconds = max(60, int(getattr(config, "KUMO_RUN_LOCK_TTL_MINUTES", 360)) * 60)
    if time.monotonic() - updated > ttl_seconds:
        _release_live_run_lock(workflow_id)
        return None
    return {k: v for k, v in lock.items() if not str(k).startswith("_")}


def _persist_live_run_lock_async(lock, actor=None):
    if config.USE_MOCK or not sf.is_configured() or not lock:
        return
    client_ip = _client_ip() if has_request_context() else ""
    user_agent = _user_agent() if has_request_context() else ""

    def worker():
        try:
            _persist_live_run_lock(lock, actor=actor, client_ip=client_ip, user_agent=user_agent)
        except Exception as exc:
            app.logger.warning("Could not persist shared run lock: %s", exc)

    Thread(target=worker, name="kumo-lock-persist", daemon=True).start()


def _persist_live_run_lock(lock, actor=None, client_ip="", user_agent=""):
    if config.USE_MOCK or not sf.is_configured() or not lock:
        return
    repo.upsert_workflow_run_lock(lock, actor=actor, client_ip=client_ip, user_agent=user_agent)


def _release_live_run_lock(workflow_id):
    if not workflow_id:
        return
    with _live_run_locks_lock:
        _live_run_locks.pop(str(workflow_id), None)
    with _shared_run_locks_lock:
        _shared_run_locks.pop(str(workflow_id), None)


def _reconcile_live_run_locks(payload):
    terminal = {
        "SUCCESS", "SUCCEEDED", "COMPLETED", "OK",
        "FAILED", "FAILURE", "ERROR", "CANCELLED", "CANCELED", "SKIPPED",
    }
    workflows = payload.get("workflows") if isinstance(payload, dict) else []
    by_id = {str(w.get("workflowId")): w for w in workflows or []}
    with _live_run_locks_lock:
        for workflow_id, lock in list(_live_run_locks.items()):
            workflow = by_id.get(str(workflow_id))
            if not workflow:
                continue
            same_run = lock.get("runId") and str(workflow.get("lastRunId") or "") == str(lock.get("runId"))
            status = str(workflow.get("lastStatus") or "").upper()
            if same_run and status in terminal:
                _live_run_locks.pop(workflow_id, None)


def _live_run_lock_list():
    ttl_seconds = max(60, int(getattr(config, "KUMO_RUN_LOCK_TTL_MINUTES", 360)) * 60)
    now = time.monotonic()
    with _live_run_locks_lock:
        for workflow_id, lock in list(_live_run_locks.items()):
            if now - float(lock.get("_updatedMonotonic") or now) > ttl_seconds:
                _live_run_locks.pop(workflow_id, None)
        memory_locks = [
            {k: v for k, v in lock.items() if not str(k).startswith("_")}
            for lock in _live_run_locks.values()
        ]
    with _shared_run_locks_lock:
        shared_locks = list(_shared_run_locks.values())
    merged = {str(lock.get("workflowId")): lock for lock in shared_locks if lock.get("workflowId")}
    for lock in memory_locks:
        if lock.get("workflowId"):
            merged[str(lock.get("workflowId"))] = {**merged.get(str(lock.get("workflowId")), {}), **lock}
    return list(merged.values())


def _refresh_shared_run_locks_once():
    if config.USE_MOCK or not sf.is_configured():
        return
    try:
        locks = repo.load_active_run_locks()
    except Exception as exc:
        app.logger.warning("Could not refresh shared run locks: %s", exc)
        return
    with _shared_run_locks_lock:
        _shared_run_locks.clear()
        for lock in locks:
            if lock.get("workflowId"):
                _shared_run_locks[str(lock.get("workflowId"))] = lock


def _start_shared_lock_sync():
    def worker():
        interval = max(2, int(getattr(config, "KUMO_SHARED_LOCK_SYNC_SECONDS", 5) or 5))
        while True:
            _refresh_shared_run_locks_once()
            time.sleep(interval)

    Thread(target=worker, name="kumo-shared-lock-sync", daemon=True).start()


class StatusCoordinator:
    def __init__(self):
        self._lock = RLock()
        self._wake = Event()
        self._thread = None
        self._client_count = 0
        self._active_runs = {}
        self._last_monitor_poll = 0.0
        self._last_lock_poll = 0.0
        self._shared_lock_signatures = {}

    def set_client_count(self, count):
        with self._lock:
            self._client_count = max(0, int(count or 0))
            self._ensure_thread_locked()
        self._wake.set()

    def track_run(self, workflow_id, workflow_name, run_id, actor=None, requested_by=None):
        if not workflow_id or not run_id:
            return
        key = (str(workflow_id), str(run_id))
        with self._lock:
            previous = self._active_runs.get(key) or {}
            self._active_runs[key] = {
                **previous,
                "workflowId": str(workflow_id),
                "workflowName": workflow_name or str(workflow_id),
                "runId": str(run_id),
                "actor": actor or previous.get("actor") or {},
                "requestedBy": requested_by or previous.get("requestedBy") or "",
                "lastStatus": previous.get("lastStatus"),
                "trackedAt": previous.get("trackedAt") or time.monotonic(),
                "deadline": max(float(previous.get("deadline") or 0), time.monotonic() + 30 * 60),
            }
            self._ensure_thread_locked()
        self._wake.set()

    def _ensure_thread_locked(self):
        if self._thread and self._thread.is_alive():
            return
        if self._client_count <= 0:
            return
        self._thread = Thread(target=self._loop, name="kumo-status-coordinator", daemon=True)
        self._thread.start()

    def _snapshot(self):
        with self._lock:
            return self._client_count, list(self._active_runs.values())

    def diagnostics(self):
        with self._lock:
            return {
                "clientCount": self._client_count,
                "activeRuns": [
                    {
                        "workflowId": run.get("workflowId"),
                        "workflowName": run.get("workflowName"),
                        "runId": run.get("runId"),
                        "lastStatus": run.get("lastStatus"),
                    }
                    for run in self._active_runs.values()
                ],
                "threadAlive": bool(self._thread and self._thread.is_alive()),
            }

    def _remove_run(self, workflow_id, run_id):
        with self._lock:
            self._active_runs.pop((str(workflow_id), str(run_id)), None)

    def _update_run_status(self, run, status_row):
        workflow_id = run["workflowId"]
        workflow_name = run["workflowName"]
        run_id = run["runId"]
        actor = run.get("actor") or {}
        requested_by = run.get("requestedBy") or status_row.get("lastRequestedBy") or ""
        status = str(status_row.get("status") or "").upper()
        if not status:
            return False

        with _live_run_locks_lock:
            current_status = str((_live_run_locks.get(str(workflow_id)) or {}).get("status") or "").upper()
        if _RUN_STATUS_RANK.get(current_status, 0) > _RUN_STATUS_RANK.get(status, 0):
            return False

        if status == run.get("lastStatus"):
            return status in _TERMINAL_RUN_STATUSES

        run["lastStatus"] = status
        app.logger.info(
            "KUMO_RUN_TIMING workflow_id=%s step=status_observed run_id=%s status=%s elapsed_ms=%d",
            workflow_id,
            run_id,
            status,
            int((time.monotonic() - float(run.get("trackedAt") or time.monotonic())) * 1000),
        )
        current_lock = _upsert_live_run_lock({
            "workflowId": workflow_id,
            "workflowName": workflow_name,
            "runId": run_id,
            "status": status,
            "requestedBy": requested_by,
            "requestedAt": status_row.get("lastRequestedAt"),
            "lastStartTime": status_row.get("lastStartTime"),
            "lastEndTime": status_row.get("lastEndTime"),
            "message": f"Run is {status}.",
        })
        _persist_live_run_lock_async(current_lock, actor=actor)
        event = {
            "workflowId": workflow_id,
            "workflowName": workflow_name,
            "runId": run_id,
            "status": status,
            "lock": current_lock,
            "actor": actor,
            "requestedBy": requested_by,
            **status_row,
        }
        _publish_run_event("workflow_run_status", event)

        if status in _TERMINAL_RUN_STATUSES:
            _release_live_run_lock(workflow_id)
            try:
                repo.release_workflow_run_lock_for_run(workflow_id, run_id=run_id, status=status, reason="HISTORY_TERMINAL")
            except Exception as exc:
                app.logger.warning("Could not release shared run lock: %s", exc)
            terminal_event = {
                "workflowId": workflow_id,
                "workflowName": workflow_name,
                "runId": run_id,
                "status": status,
                "lock": None,
                "actor": actor,
                "requestedBy": requested_by,
                **status_row,
            }
            _publish_run_event("workflow_run_status", terminal_event)
            return True
        return False

    def _poll_active_runs(self, active_runs):
        now = time.monotonic()
        for run in active_runs:
            if now > float(run.get("deadline") or 0):
                self._remove_run(run["workflowId"], run["runId"])
                continue
            try:
                status_row = repo.get_workflow_run_status(run["workflowId"], run["runId"])
            except Exception as exc:
                app.logger.warning("Could not poll workflow run status: %s", exc)
                continue
            if not status_row:
                continue
            if self._update_run_status(run, status_row):
                self._remove_run(run["workflowId"], run["runId"])

    def _sync_shared_locks(self):
        locks = repo.load_active_run_locks()
        with _shared_run_locks_lock:
            _shared_run_locks.clear()
            for lock in locks:
                if lock.get("workflowId"):
                    _shared_run_locks[str(lock.get("workflowId"))] = lock

        seen = set()
        for lock in locks:
            workflow_id = str(lock.get("workflowId") or "")
            if not workflow_id:
                continue
            seen.add(workflow_id)
            run_id = str(lock.get("runId") or "")
            status = str(lock.get("status") or "QUEUED").upper()
            signature = (run_id, status)
            if self._shared_lock_signatures.get(workflow_id) != signature:
                self._shared_lock_signatures[workflow_id] = signature
                current_lock = _upsert_live_run_lock(lock)
                if status == "INITIATING":
                    event_type = "workflow_run_requested"
                elif status == "QUEUED":
                    event_type = "workflow_run_queued"
                else:
                    event_type = "workflow_run_status"
                event = {
                    "workflowId": workflow_id,
                    "workflowName": lock.get("workflowName") or workflow_id,
                    "runId": run_id,
                    "status": status,
                    "lock": current_lock,
                    "requestedBy": lock.get("requestedBy") or "",
                }
                _publish_run_event(event_type, event)
            if run_id and status not in _TERMINAL_RUN_STATUSES:
                self.track_run(
                    workflow_id,
                    lock.get("workflowName") or workflow_id,
                    run_id,
                    actor={},
                    requested_by=lock.get("requestedBy") or "",
                )

        for workflow_id in list(self._shared_lock_signatures):
            if workflow_id not in seen:
                self._shared_lock_signatures.pop(workflow_id, None)

    def _poll_monitor_if_due(self, client_count, active_count):
        if config.USE_MOCK or not sf.is_configured():
            return
        now = time.monotonic()
        slow_interval = max(5.0, float(getattr(config, "KUMO_STATUS_IDLE_POLL_SECONDS", 30) or 30))
        interval = slow_interval
        if client_count <= 0:
            return
        if now - self._last_monitor_poll < interval:
            return
        self._last_monitor_poll = now
        payload = monitor_cache.get()
        _reconcile_live_run_locks(payload)

    def _poll_shared_locks_if_due(self, client_count, active_count):
        if config.USE_MOCK or not sf.is_configured():
            return
        now = time.monotonic()
        active_interval = max(0.5, float(getattr(config, "KUMO_STATUS_ACTIVE_POLL_SECONDS", 1) or 1))
        interval = active_interval
        if client_count <= 0:
            return
        if now - self._last_lock_poll < interval:
            return
        self._last_lock_poll = now
        self._sync_shared_locks()

    def _loop(self):
        idle_since = None
        active_interval = max(0.5, float(getattr(config, "KUMO_STATUS_ACTIVE_POLL_SECONDS", 1) or 1))
        idle_grace = max(1.0, float(getattr(config, "KUMO_STATUS_COORDINATOR_IDLE_GRACE_SECONDS", 10) or 10))
        while True:
            client_count, active_runs = self._snapshot()
            active_count = len(active_runs)
            if client_count <= 0:
                if idle_since is None:
                    idle_since = time.monotonic()
                if time.monotonic() - idle_since >= idle_grace:
                    with self._lock:
                        if self._client_count <= 0:
                            self._thread = None
                            return
                self._wake.wait(timeout=idle_grace)
                self._wake.clear()
                continue

            idle_since = None
            if active_runs:
                self._poll_active_runs(active_runs)
            self._poll_shared_locks_if_due(client_count, active_count)
            self._poll_monitor_if_due(client_count, active_count)
            self._wake.wait(timeout=active_interval if (active_runs or client_count > 0) else 5.0)
            self._wake.clear()


status_coordinator = StatusCoordinator()


def _set_realtime_client_count(count):
    count = max(0, int(count or 0))
    status_coordinator.set_client_count(count)
    monitor_cache.set_enabled(count > 0)
    _set_dashboard_cache_enabled(count > 0)


realtime_broker.set_client_count_callback(_set_realtime_client_count)


def _user_key(session_info):
    user_name = str(session_info.get("userName") or "").strip()
    if user_name and user_name.upper() != "UNKNOWN":
        return user_name.upper()
    display_name = str(session_info.get("displayName") or "").strip()
    return display_name.upper() if display_name else "UNKNOWN"


def _register_active_user(session_info, source="session"):
    key = _user_key(session_info)
    if not key or key == "UNKNOWN":
        return None

    now = _now_iso()
    item = {
        "userName": session_info.get("userName") or key,
        "displayName": session_info.get("displayName") or session_info.get("userName") or key,
        "firstName": session_info.get("firstName") or "",
        "lastName": session_info.get("lastName") or "",
        "roleName": session_info.get("roleName") or "Unknown role",
        "warehouseName": session_info.get("warehouseName") or "Not selected",
        "mode": session_info.get("mode") or "unknown",
        "callerRightsActive": bool(session_info.get("callerRightsActive")),
        "callerTokenPresent": bool(session_info.get("callerTokenPresent")),
        "source": source,
    }

    with _active_users_lock:
        previous = _active_users.get(key, {})
        item["firstSeenAt"] = previous.get("firstSeenAt") or now
        item["lastSeenAt"] = now
        item["hitCount"] = int(previous.get("hitCount") or 0) + 1
        _active_users[key] = item

    return item


def _active_user_list():
    with _active_users_lock:
        rows = list(_active_users.values())
    rows.sort(key=lambda r: r.get("lastSeenAt") or "", reverse=True)
    return rows


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.headers.get("X-Real-IP") or request.remote_addr or ""


def _user_agent():
    return request.headers.get("User-Agent", "")[:1000]


def _json_dumps(value):
    try:
        return json.dumps(value if value is not None else {}, default=str)
    except Exception:
        return json.dumps({"serializationError": str(value)[:1000]})


def _audit_enabled():
    return bool(getattr(config, "KUMO_AUDIT_ENABLED", True)) and sf.is_configured() and not config.USE_MOCK


def _persist_user_session(session_info, source="session", last_action=None):
    """Persist latest seen user data to KUMO APP_USER_SESSIONS.

    This uses the service context, not the caller context, so normal users do not
    need direct DML privileges on the application audit tables.
    """
    _register_active_user(session_info, source=source)
    if not _audit_enabled():
        return

    user_name = str(session_info.get("userName") or "").strip() or "UNKNOWN"
    if user_name.upper() == "UNKNOWN":
        return

    params = {
        "user_name": user_name,
        "display_name": session_info.get("displayName") or user_name,
        "first_name": session_info.get("firstName") or "",
        "last_name": session_info.get("lastName") or "",
        "role_name": session_info.get("roleName") or "Unknown role",
        "warehouse_name": session_info.get("warehouseName") or "Not selected",
        "session_mode": session_info.get("mode") or sf.connection_mode(),
        "caller_rights_active": bool(session_info.get("callerRightsActive")),
        "caller_token_present": bool(session_info.get("callerTokenPresent")),
        "last_action": last_action or source,
        "client_ip": _client_ip(),
        "user_agent": _user_agent(),
        "extra_json": _json_dumps({"source": source, "raw": session_info.get("raw") or {}}),
    }

    sql = f"""
    MERGE INTO {config.T_APP_USER_SESSIONS} t
    USING (
      SELECT
        %(user_name)s AS USER_NAME,
        %(display_name)s AS DISPLAY_NAME,
        %(first_name)s AS FIRST_NAME,
        %(last_name)s AS LAST_NAME,
        %(role_name)s AS ROLE_NAME,
        %(warehouse_name)s AS WAREHOUSE_NAME,
        %(session_mode)s AS SESSION_MODE,
        %(caller_rights_active)s AS CALLER_RIGHTS_ACTIVE,
        %(caller_token_present)s AS CALLER_TOKEN_PRESENT,
        %(last_action)s AS LAST_ACTION,
        %(client_ip)s AS CLIENT_IP,
        %(user_agent)s AS USER_AGENT,
        PARSE_JSON(%(extra_json)s) AS EXTRA
    ) s
    ON t.USER_NAME = s.USER_NAME
    WHEN MATCHED THEN UPDATE SET
      DISPLAY_NAME = s.DISPLAY_NAME,
      FIRST_NAME = s.FIRST_NAME,
      LAST_NAME = s.LAST_NAME,
      ROLE_NAME = s.ROLE_NAME,
      WAREHOUSE_NAME = s.WAREHOUSE_NAME,
      SESSION_MODE = s.SESSION_MODE,
      CALLER_RIGHTS_ACTIVE = s.CALLER_RIGHTS_ACTIVE,
      CALLER_TOKEN_PRESENT = s.CALLER_TOKEN_PRESENT,
      LAST_SEEN_AT = CURRENT_TIMESTAMP(),
      LAST_INTERACTION_AT = IFF(s.LAST_ACTION IS NULL, t.LAST_INTERACTION_AT, CURRENT_TIMESTAMP()),
      LAST_ACTION = COALESCE(s.LAST_ACTION, t.LAST_ACTION),
      HIT_COUNT = COALESCE(t.HIT_COUNT, 0) + 1,
      CLIENT_IP = s.CLIENT_IP,
      USER_AGENT = s.USER_AGENT,
      EXTRA = s.EXTRA,
      UPDATED_AT = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT (
      USER_NAME, DISPLAY_NAME, FIRST_NAME, LAST_NAME, ROLE_NAME, WAREHOUSE_NAME,
      SESSION_MODE, CALLER_RIGHTS_ACTIVE, CALLER_TOKEN_PRESENT,
      FIRST_SEEN_AT, LAST_SEEN_AT, LAST_INTERACTION_AT, LAST_ACTION, HIT_COUNT,
      CLIENT_IP, USER_AGENT, EXTRA, UPDATED_AT
    ) VALUES (
      s.USER_NAME, s.DISPLAY_NAME, s.FIRST_NAME, s.LAST_NAME, s.ROLE_NAME, s.WAREHOUSE_NAME,
      s.SESSION_MODE, s.CALLER_RIGHTS_ACTIVE, s.CALLER_TOKEN_PRESENT,
      CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), s.LAST_ACTION, 1,
      s.CLIENT_IP, s.USER_AGENT, s.EXTRA, CURRENT_TIMESTAMP()
    )
    """
    try:
        sf.execute_service(sql, params=params, use_warehouse=True, include_context=True)
    except Exception as exc:
        app.logger.warning("Could not persist app user session: %s", exc)


def _load_persistent_users(limit=50):
    if not _audit_enabled():
        return _active_user_list()
    try:
        rows = sf.query_service(
            f"""
            SELECT
              USER_NAME,
              DISPLAY_NAME,
              FIRST_NAME,
              LAST_NAME,
              ROLE_NAME,
              WAREHOUSE_NAME,
              SESSION_MODE,
              CALLER_RIGHTS_ACTIVE,
              CALLER_TOKEN_PRESENT,
              FIRST_SEEN_AT,
              LAST_SEEN_AT,
              LAST_INTERACTION_AT,
              LAST_ACTION,
              HIT_COUNT,
              CLIENT_IP
            FROM {config.T_APP_USER_SESSIONS}
            ORDER BY LAST_SEEN_AT DESC NULLS LAST
            LIMIT {int(limit)}
            """,
            use_warehouse=True,
            include_context=True,
        )
        out = []
        for row in rows:
            r = {str(k).upper(): v for k, v in dict(row).items()}
            out.append({
                "userName": r.get("USER_NAME"),
                "displayName": r.get("DISPLAY_NAME") or r.get("USER_NAME"),
                "firstName": r.get("FIRST_NAME") or "",
                "lastName": r.get("LAST_NAME") or "",
                "roleName": r.get("ROLE_NAME") or "Unknown role",
                "warehouseName": r.get("WAREHOUSE_NAME") or "Not selected",
                "mode": r.get("SESSION_MODE") or "unknown",
                "callerRightsActive": bool(r.get("CALLER_RIGHTS_ACTIVE")),
                "callerTokenPresent": bool(r.get("CALLER_TOKEN_PRESENT")),
                "firstSeenAt": r.get("FIRST_SEEN_AT"),
                "lastSeenAt": r.get("LAST_SEEN_AT"),
                "lastInteractionAt": r.get("LAST_INTERACTION_AT"),
                "lastAction": r.get("LAST_ACTION") or "",
                "hitCount": int(r.get("HIT_COUNT") or 0),
                "clientIp": r.get("CLIENT_IP") or "",
            })
        return out
    except Exception as exc:
        app.logger.warning("Could not load persistent app users: %s", exc)
        return _active_user_list()


def _dashboard_session_snapshot():
    if config.USE_MOCK or not sf.is_configured():
        return {
            "ok": True,
            "displayName": "Andreas Larsson",
            "firstName": "Andreas",
            "lastName": "Larsson",
            "userName": "ANDREAS",
            "roleName": "KUMO_ADMIN_ROLE",
            "warehouseName": config.SNOWFLAKE_WAREHOUSE or config.DEFAULT_TASK_WAREHOUSE,
            "mode": "mock",
            "callerRightsActive": False,
            "callerTokenPresent": False,
        }

    if sf.connection_mode() == "password":
        display_name = os.getenv("KUMO_DISPLAY_NAME", "").strip() or config.SNOWFLAKE_USER or "KUMO user"
        parts = display_name.split()
        return {
            "ok": True,
            "displayName": display_name,
            "firstName": parts[0] if parts else "",
            "lastName": parts[-1] if len(parts) > 1 else "",
            "userName": config.SNOWFLAKE_USER or "UNKNOWN",
            "roleName": config.SNOWFLAKE_ROLE or "Unknown role",
            "warehouseName": config.SNOWFLAKE_WAREHOUSE or "Not selected",
            "mode": "password",
            "callerRightsActive": False,
            "callerTokenPresent": False,
        }

    return {
        "ok": True,
        "displayName": "KUMO user",
        "firstName": "",
        "lastName": "",
        "userName": "UNKNOWN",
        "roleName": "Unknown role",
        "warehouseName": config.SNOWFLAKE_WAREHOUSE or "Not selected",
        "mode": sf.connection_mode(),
        "callerRightsActive": sf.connection_mode() == "spcs-caller-oauth",
        "callerTokenPresent": sf.caller_token_present(),
    }


def _dashboard_ping_snapshot():
    if config.USE_MOCK or not sf.is_configured():
        return {
            "ok": False,
            "mode": sf.connection_mode(),
            "error": "Snowflake is not configured or mock mode is enabled",
        }
    try:
        return {"ok": True, "mode": sf.connection_mode(), "snowflake": sf.ping()}
    except Exception as exc:
        return {"ok": False, "mode": sf.connection_mode(), "error": str(exc)}


def _refresh_dashboard_cache_once():
    global _dashboard_cache
    started = time.perf_counter()
    with _dashboard_cache_lock:
        _dashboard_cache = {**_dashboard_cache, "refreshing": True}

    error = None
    try:
        ping = _dashboard_ping_snapshot()
        users = _load_persistent_users()
        active_users = {
            "ok": True,
            "source": "snowflake" if _audit_enabled() else "memory",
            "users": users,
            "count": len(users),
        }
    except Exception as exc:
        error = str(exc)
        app.logger.warning("Could not refresh dashboard cache: %s", exc)
        with _dashboard_cache_lock:
            ping = _dashboard_cache.get("ping") or {"ok": False, "mode": sf.connection_mode(), "error": error}
            active_users = _dashboard_cache.get("activeUsers") or {"ok": True, "source": "memory", "users": [], "count": 0}

    with _dashboard_cache_lock:
        _dashboard_cache = {
            "ping": ping,
            "activeUsers": active_users,
            "generatedAt": _now_iso(),
            "durationMs": int((time.perf_counter() - started) * 1000),
            "refreshing": False,
            "error": error,
        }


def _dashboard_cache_loop():
    global _dashboard_cache_thread
    interval = max(5, int(getattr(config, "KUMO_DASHBOARD_CACHE_SECONDS", config.REFRESH_SECONDS) or config.REFRESH_SECONDS))
    _refresh_dashboard_cache_once()
    while True:
        _dashboard_cache_wake.wait(timeout=interval)
        _dashboard_cache_wake.clear()
        with _dashboard_cache_lock:
            if not _dashboard_cache_enabled:
                _dashboard_cache_thread = None
                return
        _refresh_dashboard_cache_once()


def _start_dashboard_cache():
    global _dashboard_cache_enabled, _dashboard_cache_thread
    with _dashboard_cache_lock:
        _dashboard_cache_enabled = True
        if _dashboard_cache_thread and _dashboard_cache_thread.is_alive():
            return
        _dashboard_cache_thread = Thread(target=_dashboard_cache_loop, name="kumo-dashboard-refresh", daemon=True)
        thread = _dashboard_cache_thread
    thread.start()


def _stop_dashboard_cache():
    global _dashboard_cache_enabled
    with _dashboard_cache_lock:
        _dashboard_cache_enabled = False
    _dashboard_cache_wake.set()


def _set_dashboard_cache_enabled(enabled):
    if enabled:
        _start_dashboard_cache()
    else:
        _stop_dashboard_cache()


def _request_dashboard_cache_refresh():
    with _dashboard_cache_lock:
        enabled = bool(_dashboard_cache_enabled)
    if enabled:
        _dashboard_cache_wake.set()


def _dashboard_cache_snapshot():
    with _dashboard_cache_lock:
        return dict(_dashboard_cache)


def _record_interaction(action, actor=None, entity_type=None, entity_id=None, workflow_id=None,
                        run_id=None, status="SUCCESS", success=True, error_message=None,
                        payload=None, response=None):
    """Write one audit row to APP_USER_INTERACTIONS and update APP_USER_SESSIONS."""
    if not actor:
        actor = _actor_context()

    _persist_user_session(actor, source="interaction", last_action=action)

    if not _audit_enabled():
        return

    user_name = str(actor.get("userName") or "UNKNOWN")
    params = {
        "user_name": user_name,
        "display_name": actor.get("displayName") or user_name,
        "first_name": actor.get("firstName") or "",
        "last_name": actor.get("lastName") or "",
        "role_name": actor.get("roleName") or "Unknown role",
        "warehouse_name": actor.get("warehouseName") or "Not selected",
        "session_mode": actor.get("mode") or sf.connection_mode(),
        "caller_rights_active": bool(actor.get("callerRightsActive")),
        "caller_token_present": bool(actor.get("callerTokenPresent")),
        "app_version": getattr(config, "KUMO_APP_VERSION", "dev"),
        "request_method": request.method,
        "request_path": request.path,
        "action": action,
        "entity_type": entity_type or "APP",
        "entity_id": entity_id or workflow_id or "",
        "workflow_id": workflow_id or "",
        "run_id": run_id or "",
        "status": status,
        "success": bool(success),
        "http_status": 200 if success else 500,
        "error_message": str(error_message or "")[:8000],
        "payload_json": _json_dumps(payload or {}),
        "response_json": _json_dumps(response or {}),
        "client_ip": _client_ip(),
        "user_agent": _user_agent(),
    }

    sql = f"""
    INSERT INTO {config.T_APP_USER_INTERACTIONS} (
      INTERACTION_ID, EVENT_TS,
      USER_NAME, DISPLAY_NAME, FIRST_NAME, LAST_NAME, ROLE_NAME, WAREHOUSE_NAME,
      SESSION_MODE, CALLER_RIGHTS_ACTIVE, CALLER_TOKEN_PRESENT, APP_VERSION,
      REQUEST_METHOD, REQUEST_PATH, ACTION, ENTITY_TYPE, ENTITY_ID,
      WORKFLOW_ID, RUN_ID, STATUS, SUCCESS, HTTP_STATUS, ERROR_MESSAGE,
      PAYLOAD, RESPONSE, CLIENT_IP, USER_AGENT
    )
    SELECT
      UUID_STRING(), CURRENT_TIMESTAMP(),
      %(user_name)s, %(display_name)s, %(first_name)s, %(last_name)s, %(role_name)s, %(warehouse_name)s,
      %(session_mode)s, %(caller_rights_active)s, %(caller_token_present)s, %(app_version)s,
      %(request_method)s, %(request_path)s, %(action)s, %(entity_type)s, %(entity_id)s,
      %(workflow_id)s, %(run_id)s, %(status)s, %(success)s, %(http_status)s, %(error_message)s,
      PARSE_JSON(%(payload_json)s), PARSE_JSON(%(response_json)s), %(client_ip)s, %(user_agent)s
    """
    try:
        sf.execute_service(sql, params=params, use_warehouse=True, include_context=True)
    except Exception as exc:
        app.logger.warning("Could not persist app interaction audit row: %s", exc)


def _json_error(message, status=400):
    return jsonify({"ok": False, "error": str(message)}), status


@app.before_request
def bind_request_context():
    ingress_token = request.headers.get("Sf-Context-Current-User-Token")
    g.sf_token_handle = sf.set_ingress_user_token(ingress_token)
    g.caller_token_present = bool(ingress_token)
    app.logger.info(
        "REQUEST method=%s path=%s caller_token_present=%s",
        request.method,
        request.path,
        bool(ingress_token),
    )


@app.after_request
def log_response(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store"
    app.logger.info(
        "RESPONSE method=%s path=%s status=%s caller_token_present=%s",
        request.method,
        request.path,
        response.status_code,
        bool(getattr(g, "caller_token_present", False)),
    )
    return response


@app.teardown_request
def release_request_context(exception=None):
    token_handle = getattr(g, "sf_token_handle", None)
    if token_handle is not None:
        sf.reset_ingress_user_token(token_handle)


@app.route("/api/health")
def health():
    return jsonify(_health_snapshot())


def _health_snapshot():
    return {
        "ok": True,
        "app": "KUMO Monitor",
        **_build_info(),
        "mock": bool(config.USE_MOCK),
        "snowflakeConfigured": sf.is_configured(),
        "snowflakeConnectionMode": sf.connection_mode(),
        "callerTokenPresent": sf.caller_token_present(),
        "refreshSeconds": config.REFRESH_SECONDS,
        "db": config.DB,
        "schema": config.SCHEMA,
    }


@app.route("/api/session")
def session_context():
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({
            "ok": True,
            "displayName": "Andreas Larsson",
            "firstName": "Andreas",
            "lastName": "Larsson",
            "userName": "ANDREAS",
            "roleName": "KUMO_ADMIN_ROLE",
            "warehouseName": config.SNOWFLAKE_WAREHOUSE or config.DEFAULT_TASK_WAREHOUSE,
            "mode": "mock",
            "callerRightsActive": False,
            "callerTokenPresent": False,
        })
    try:
        session_info = sf.session_context()
        _persist_user_session(session_info, source="session", last_action="SESSION_REFRESH")
        _record_interaction("SESSION_REFRESH", actor=session_info, entity_type="SESSION", status="SUCCESS", success=True)
        return jsonify({"ok": True, **session_info, "activeUsers": _load_persistent_users()})
    except Exception as exc:
        return jsonify({
            "ok": False,
            "error": str(exc),
            "displayName": "KUMO user",
            "firstName": "",
            "lastName": "",
            "userName": "UNKNOWN",
            "roleName": "Unknown role",
            "warehouseName": config.SNOWFLAKE_WAREHOUSE or "",
            "mode": sf.connection_mode(),
            "callerRightsActive": sf.connection_mode() == "spcs-caller-oauth",
            "callerTokenPresent": sf.caller_token_present(),
        }), 200


@app.route("/api/users/active")
def active_users():
    if realtime_broker.client_count() > 0:
        _request_dashboard_cache_refresh()
    snapshot = _dashboard_cache_snapshot()
    return jsonify({**(snapshot.get("activeUsers") or {}), "cached": True, "cacheGeneratedAt": snapshot.get("generatedAt")})


@app.route("/api/snowflake/ping")
def snowflake_ping():
    if realtime_broker.client_count() > 0:
        _request_dashboard_cache_refresh()
    snapshot = _dashboard_cache_snapshot()
    return jsonify({**(snapshot.get("ping") or {}), "cached": True, "cacheGeneratedAt": snapshot.get("generatedAt")}), 200


@app.route("/api/dashboard")
def dashboard():
    if realtime_broker.client_count() > 0:
        monitor_cache.refresh_async()
        _request_dashboard_cache_refresh()
    cache = _dashboard_cache_snapshot()
    monitor_payload = monitor_cache.get()
    _reconcile_live_run_locks(monitor_payload)
    return jsonify({
        "ok": True,
        "source": "server-cache",
        "health": _health_snapshot(),
        "session": _dashboard_session_snapshot(),
        "ping": cache.get("ping") or {},
        "activeUsers": cache.get("activeUsers") or {"ok": True, "users": [], "count": 0},
        "monitor": monitor_payload,
        "cache": {
            "dashboardGeneratedAt": cache.get("generatedAt"),
            "dashboardDurationMs": cache.get("durationMs"),
            "dashboardRefreshing": bool(cache.get("refreshing")),
            "dashboardError": cache.get("error"),
            "monitorGeneratedAt": monitor_payload.get("generatedAt") if isinstance(monitor_payload, dict) else None,
        },
        "generatedAt": _now_iso(),
    })


@app.route("/api/monitor")
def monitor():
    if realtime_broker.client_count() > 0:
        monitor_cache.refresh_async()
    payload = monitor_cache.get()
    _reconcile_live_run_locks(payload)
    return jsonify(payload)


@app.route("/api/monitor/refresh", methods=["POST"])
def refresh_monitor():
    payload = monitor_cache.refresh_async()
    _reconcile_live_run_locks(payload)
    return jsonify({**payload, "refreshQueued": True})


@app.route("/api/events")
def events():
    response = Response(stream_with_context(realtime_broker.stream()), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Connection"] = "keep-alive"
    return response


@app.route("/api/workflow-run-locks")
def workflow_run_locks():
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"ok": True, "locks": []})
    try:
        _reconcile_live_run_locks(monitor_cache.get())
        return jsonify({"ok": True, "locks": _live_run_lock_list(), "source": "live-memory+shared"})
    except Exception as exc:
        return _json_error(exc, 500)


@app.route("/api/realtime/state")
def realtime_state():
    return jsonify({
        "ok": True,
        **_build_info(),
        "clientCount": realtime_broker.client_count(),
        "coordinator": status_coordinator.diagnostics(),
        "locks": _live_run_lock_list(),
        "events": _recent_run_event_list(),
        "generatedAt": _now_iso(),
    })


def _actor_context():
    if config.USE_MOCK or not sf.is_configured():
        return {
            "displayName": "Andreas Larsson",
            "userName": "ANDREAS",
            "roleName": "KUMO_ADMIN_ROLE",
            "callerRightsActive": False,
        }
    if sf.connection_mode() == "password":
        user_name = config.SNOWFLAKE_USER or "UNKNOWN"
        return {
            "displayName": os.getenv("KUMO_DISPLAY_NAME", "").strip() or user_name,
            "userName": user_name,
            "roleName": config.SNOWFLAKE_ROLE or "Unknown role",
            "warehouseName": config.SNOWFLAKE_WAREHOUSE or "Not selected",
            "mode": "password",
            "callerRightsActive": False,
            "callerTokenPresent": False,
        }
    try:
        actor = sf.session_context()
        _register_active_user(actor, source="action")
        return actor
    except Exception as exc:
        app.logger.warning("Could not resolve actor context: %s", exc)
        return {
            "displayName": "KUMO user",
            "userName": "UNKNOWN",
            "roleName": "Unknown role",
            "callerRightsActive": sf.connection_mode() == "spcs-caller-oauth",
        }


@app.route("/api/workflows/<workflow_id>/run", methods=["POST"])
def run_workflow(workflow_id):
    return _run_workflow_impl(workflow_id, request.get_json(silent=True) or {}, "POST")


@app.route("/api/workflows/<workflow_id>/run-fallback")
def run_workflow_fallback(workflow_id):
    return _run_workflow_impl(workflow_id, {
        "triggerSource": request.args.get("triggerSource") or "MANUAL",
        "workflowName": request.args.get("workflowName") or "",
        "requestedBy": request.args.get("requestedBy") or "",
    }, "GET_FALLBACK")


def _complete_workflow_run_request(workflow_id, workflow_name, trigger_source, requested_by, actor, lock, requested_at, started_at, payload, client_ip, user_agent):
    with app.app_context():
        try:
            run_id = repo.request_run(
                workflow_id=workflow_id,
                trigger_source=trigger_source,
                requested_by=requested_by,
            )

            app.logger.info(
                "KUMO_RUN_TIMING workflow_id=%s step=queue_inserted run_id=%s elapsed_ms=%d",
                workflow_id,
                run_id,
                int((time.perf_counter() - started_at) * 1000),
            )

            queued_lock = None
            try:
                queued_lock = repo.mark_workflow_run_lock_queued(workflow_id, lock.get("lockId"), run_id)
            except Exception as exc:
                app.logger.warning("Could not mark shared run lock queued: %s", exc)

            current_lock = _upsert_live_run_lock({
                **(lock or {}),
                **(queued_lock or {}),
                "lockId": (queued_lock or {}).get("lockId") or lock.get("lockId") or f"live-{uuid.uuid4()}",
                "workflowId": workflow_id,
                "workflowName": workflow_name,
                "runId": run_id,
                "status": "QUEUED",
                "requestedBy": requested_by,
                "requestedByUser": actor.get("userName") or "",
                "requestedByRole": actor.get("roleName") or "",
                "requestedAt": (queued_lock or {}).get("requestedAt") or lock.get("requestedAt") or requested_at,
                "message": "Queued. Waiting for dispatcher pickup.",
            })

            queued_event = {
                "workflowId": workflow_id,
                "workflowName": workflow_name,
                "runId": run_id,
                "status": "QUEUED",
                "lock": current_lock,
                "actor": actor,
                "requestedBy": requested_by,
            }

            _publish_run_event("workflow_run_queued", queued_event)
            status_coordinator.track_run(workflow_id, workflow_name, run_id, actor, requested_by)

            if not queued_lock:
                try:
                    _persist_live_run_lock(
                        current_lock,
                        actor=actor,
                        client_ip=client_ip,
                        user_agent=user_agent,
                    )
                except Exception as exc:
                    app.logger.warning("Could not persist shared queued lock: %s", exc)
        except Exception as exc:
            _release_live_run_lock(workflow_id)
            try:
                repo.release_workflow_run_lock(
                    lock_id=(lock or {}).get("lockId"),
                    workflow_id=workflow_id,
                    status="FAILED",
                    reason="REQUEST_FAILED",
                    error_message=str(exc),
                )
            except Exception:
                pass

            failed_event = {
                "workflowId": workflow_id,
                "workflowName": workflow_name,
                "runId": (lock or {}).get("runId"),
                "status": "FAILED",
                "lock": lock,
                "actor": actor,
                "requestedBy": requested_by,
                "error": str(exc),
            }
            _publish_run_event("workflow_run_failed", failed_event)
            app.logger.warning("Could not complete workflow run request: %s", exc)


def _run_workflow_impl(workflow_id, payload, request_method):
    started_at = time.perf_counter()
    payload = payload or {}
    lock = None
    workflow_name = payload.get("workflowName") or workflow_id
    trigger_source = str(payload.get("triggerSource") or "MANUAL").upper()
    requested_at = _now_iso()

    if not config.USE_MOCK and sf.is_configured():
        existing_lock = _active_local_run_lock(workflow_id)
        if existing_lock:
            status = str(existing_lock.get("status") or "ACTIVE").upper()
            event_type = "workflow_run_queued" if status == "QUEUED" else "workflow_run_status"
            if status == "INITIATING":
                event_type = "workflow_run_requested"
            active_event = _publish_run_event(event_type, {
                "workflowId": workflow_id,
                "workflowName": existing_lock.get("workflowName") or workflow_name,
                "runId": existing_lock.get("runId") or "pending",
                "status": status,
                "lock": existing_lock,
                "requestedBy": existing_lock.get("requestedBy") or "",
            })
            return jsonify({
                "ok": False,
                "error": f"{workflow_name} is already {status}.",
                "status": status,
                "lock": existing_lock,
                "sequence": active_event.get("sequence"),
            }), 409

        lock = _upsert_live_run_lock({
            "lockId": f"request-{uuid.uuid4()}",
            "workflowId": workflow_id,
            "workflowName": workflow_name,
            "runId": "pending",
            "status": "INITIATING",
            "requestedBy": payload.get("requestedBy") or "",
            "requestedByUser": "",
            "requestedByRole": "",
            "requestedAt": requested_at,
            "message": "Initiating. Validating request.",
        })
        _publish_run_event("workflow_run_requested", {
            "workflowId": workflow_id,
            "workflowName": workflow_name,
            "runId": "pending",
            "status": "INITIATING",
            "lock": lock,
            "requestedBy": lock.get("requestedBy") or "",
            "requestedAt": requested_at,
        })

    actor = _actor_context()
    requested_by = (
        payload.get("requestedBy")
        or actor.get("displayName")
        or actor.get("userName")
        or "UNKNOWN"
    )

    app.logger.info(
        "KUMO_ACTION action=run_workflow method=%s workflow_id=%s actor=%s user=%s role=%s caller_rights=%s",
        request_method,
        workflow_id,
        actor.get("displayName"),
        actor.get("userName"),
        actor.get("roleName"),
        actor.get("callerRightsActive"),
    )

    if trigger_source == "MANUAL":
        try:
            upstream = repo.upstream_workflows_for(workflow_id)
        except Exception as exc:
            app.logger.warning("Could not validate manual run upstream dependencies: %s", exc)
            upstream = []
        if upstream:
            message = "Manual runs are disabled for workflows that are triggered by parent workflows."
            _release_live_run_lock(workflow_id)
            _publish_run_event("workflow_run_failed", {
                "workflowId": workflow_id,
                "workflowName": workflow_name,
                "runId": (lock or {}).get("runId") or "pending",
                "status": "FAILED",
                "lock": None,
                "actor": actor,
                "requestedBy": requested_by,
                "error": message,
            })
            _record_interaction(
                "RUN_WORKFLOW",
                actor=actor,
                entity_type="WORKFLOW",
                entity_id=workflow_id,
                workflow_id=workflow_id,
                status="BLOCKED",
                success=False,
                error_message=message,
                payload=payload,
                response={"ok": False, "upstreamWorkflows": upstream},
            )
            return jsonify({
                "ok": False,
                "error": message,
                "upstreamWorkflows": upstream,
                "actor": actor,
            }), 409

    if config.USE_MOCK or not sf.is_configured():
        response = {
            "ok": True,
            "runId": "mock-run",
            "status": "QUEUED",
            "actor": actor,
            "message": "Mock mode: run request accepted",
        }
        _record_interaction(
            "RUN_WORKFLOW",
            actor=actor,
            entity_type="WORKFLOW",
            entity_id=workflow_id,
            workflow_id=workflow_id,
            run_id="mock-run",
            payload=payload,
            response=response,
        )
        return jsonify(response)

    try:
        durable_lock = repo.acquire_workflow_run_lock(
            workflow_id,
            workflow_name=workflow_name,
            actor=actor,
            client_ip=_client_ip(),
            user_agent=_user_agent(),
        )
        lock = _upsert_live_run_lock({
            **(lock or {}),
            **(durable_lock or {}),
            "workflowId": workflow_id,
            "workflowName": workflow_name,
            "status": "INITIATING",
            "requestedBy": requested_by,
            "requestedByUser": actor.get("userName") or "",
            "requestedByRole": actor.get("roleName") or "",
            "requestedAt": (durable_lock or {}).get("requestedAt") or (lock or {}).get("requestedAt") or requested_at,
            "message": "Initiating. Waiting for Snowflake queue insert.",
        })

        requested_event = {
            "workflowId": workflow_id,
            "workflowName": workflow_name,
            "runId": lock.get("runId") or "pending",
            "status": "INITIATING",
            "lock": lock,
            "actor": actor,
            "requestedBy": requested_by,
            "requestedAt": lock.get("requestedAt") or requested_at,
        }

        requested_event = _publish_run_event("workflow_run_requested", requested_event)

        app.logger.info(
            "KUMO_RUN_TIMING workflow_id=%s step=run_requested_broadcast elapsed_ms=%d",
            workflow_id,
            int((time.perf_counter() - started_at) * 1000),
        )

        client_ip = _client_ip()
        user_agent = _user_agent()
        Thread(
            target=_complete_workflow_run_request,
            name=f"kumo-run-request-{str(workflow_id)[:8]}",
            daemon=True,
            args=(workflow_id, workflow_name, trigger_source, requested_by, actor, lock, requested_at, started_at, payload, client_ip, user_agent),
        ).start()

        # Do not force a full monitor refresh here.
        # It is slower and may briefly return the previous persisted status before
        # the dispatcher/history has caught up. Server-sent events plus the
        # run-lock overlay give all open browsers the immediate cross-user status instead.
        response = {
            "ok": True,
            "runId": lock.get("runId") or "pending",
            "status": "INITIATING",
            "lock": lock,
            "actor": actor,
            "sequence": requested_event.get("sequence"),
        }

        _record_interaction(
            "RUN_WORKFLOW",
            actor=actor,
            entity_type="WORKFLOW",
            entity_id=workflow_id,
            workflow_id=workflow_id,
            run_id=lock.get("runId") or "pending",
            payload=payload,
            response=response,
        )
        return jsonify(response)

    except repo.WorkflowAlreadyActive as exc:
        current_lock = exc.lock or {}
        if current_lock.get("workflowId"):
            _release_live_run_lock(workflow_id)
            _upsert_live_run_lock(current_lock)
            status = str(current_lock.get("status") or "ACTIVE").upper()
            event_type = "workflow_run_queued" if status == "QUEUED" else "workflow_run_status"
            if status == "INITIATING":
                event_type = "workflow_run_requested"
            active_event = {
                "workflowId": workflow_id,
                "workflowName": current_lock.get("workflowName") or workflow_name,
                "runId": current_lock.get("runId") or "pending",
                "status": status,
                "lock": current_lock,
                "actor": actor,
                "requestedBy": current_lock.get("requestedBy") or "",
            }
            _publish_run_event(event_type, active_event)
        _record_interaction(
            "RUN_WORKFLOW",
            actor=actor,
            entity_type="WORKFLOW",
            entity_id=workflow_id,
            workflow_id=workflow_id,
            status="ACTIVE",
            success=False,
            error_message=str(exc),
            payload=payload,
        )
        return jsonify({
            "ok": False,
            "error": str(exc),
            "status": current_lock.get("status") or "ACTIVE",
            "lock": current_lock,
            "actor": actor,
        }), 409

    except Exception as exc:
        _release_live_run_lock(workflow_id)

        try:
            repo.release_workflow_run_lock(
                lock_id=(lock or {}).get("lockId"),
                workflow_id=workflow_id,
                status="FAILED",
                reason="REQUEST_FAILED",
                error_message=str(exc),
            )
        except Exception:
            pass

        _publish_run_event("workflow_run_failed", {
            "workflowId": workflow_id,
            "workflowName": workflow_name,
            "runId": (lock or {}).get("runId"),
            "status": "FAILED",
            "lock": lock,
            "actor": actor,
            "requestedBy": requested_by,
            "error": str(exc),
        })

        _record_interaction(
            "RUN_WORKFLOW",
            actor=actor,
            entity_type="WORKFLOW",
            entity_id=workflow_id,
            workflow_id=workflow_id,
            status="FAILED",
            success=False,
            error_message=str(exc),
            payload=payload,
        )
        return _json_error(exc, 500)


@app.route("/api/workflows/<workflow_id>")
def workflow_detail(workflow_id):
    if config.USE_MOCK or not sf.is_configured():
        workflow = next((w for w in MOCK_MONITOR["workflows"] if w["workflowId"] == workflow_id), None)
        if not workflow:
            return _json_error("Workflow not found", 404)
        return jsonify({
            "ok": True,
            "workflowId": workflow_id,
            "workflowName": workflow["workflowName"],
            "workflowGroup": workflow["workflowGroup"],
            "workflowType": workflow["workflowType"],
            "workflowEnabled": workflow["workflowEnabled"],
            "description": "Mock workflow",
            "dbtCommand": "dbt build --select tag:daily" if workflow["workflowType"] == "DBT" else "",
            "sqlCommand": "select 1" if workflow["workflowType"] == "SQL" else "",
            "dbtProjectFqn": "KUMO_TST.META.DBT_PROJECT",
            "dbtTarget": "prod",
            "scheduleCron": workflow["scheduleCron"],
            "scheduleTimezone": workflow["scheduleTimezone"],
            "taskEnabled": workflow["taskEnabled"],
            "onSuccess": [],
            "onFail": [],
            "notifications": {"onSuccessEmail": False, "onFailEmail": True, "successGroup": "", "failGroup": "Ops", "emailIntegration": "MY_EMAIL_INT", "environment": "PROD"},
            "workflowOptions": [{"workflowId": w["workflowId"], "label": f"{w['workflowGroup']} / {w['workflowName']}"} for w in MOCK_MONITOR["workflows"] if w["workflowId"] != workflow_id],
            "emailGroups": ["Ops", "Data Platform"],
        })
    # Keep this endpoint fast. It is used only to populate the Edit modal.
    # Do not resolve actor context or write audit rows here; those extra Snowflake
    # connections made the modal take 15-25 seconds. Save/clone/delete/toggle/run
    # actions are still audited.
    try:
        result = {"ok": True, **repo.get_workflow_detail(workflow_id)}
        return jsonify(result)
    except Exception as exc:
        return _json_error(exc, 404)


@app.route("/api/workflows/<workflow_id>", methods=["PATCH"])
def update_workflow(workflow_id):
    payload = request.get_json(silent=True) or {}
    actor = _actor_context()
    if config.USE_MOCK or not sf.is_configured():
        response = {"ok": True, "workflowId": workflow_id, "message": "Mock mode: workflow update accepted"}
        _record_interaction("UPDATE_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, payload=payload, response=response)
        return jsonify(response)
    try:
        result = repo.update_workflow_detail(workflow_id, payload)
        monitor_cache.refresh_async()
        response = {"ok": True, "workflow": result}
        _record_interaction("UPDATE_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, payload=payload, response={"ok": True})
        return jsonify(response)
    except Exception as exc:
        _record_interaction("UPDATE_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, status="FAILED", success=False, error_message=str(exc), payload=payload)
        return _json_error(exc, 500)


@app.route("/api/workflows/<workflow_id>/clone", methods=["POST"])
def clone_workflow(workflow_id):
    actor = _actor_context()
    if config.USE_MOCK or not sf.is_configured():
        response = {"ok": True, "workflowId": "mock-clone", "message": "Mock mode: clone accepted"}
        _record_interaction("CLONE_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, response=response)
        return jsonify(response)
    try:
        result = repo.clone_workflow(workflow_id)
        monitor_cache.refresh_async()
        response = {"ok": True, "workflow": result}
        _record_interaction("CLONE_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, response={"ok": True, "newWorkflow": result})
        return jsonify(response)
    except Exception as exc:
        _record_interaction("CLONE_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, status="FAILED", success=False, error_message=str(exc))
        return _json_error(exc, 500)


@app.route("/api/workflows/<workflow_id>", methods=["DELETE"])
def delete_workflow(workflow_id):
    actor = _actor_context()
    if config.USE_MOCK or not sf.is_configured():
        response = {"ok": True, "workflowId": workflow_id, "message": "Mock mode: delete accepted"}
        _record_interaction("DELETE_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, response=response)
        return jsonify(response)
    try:
        result = repo.delete_workflow(workflow_id)
        monitor_cache.refresh_async()
        response = {"ok": True, **result}
        _record_interaction("DELETE_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, response=response)
        return jsonify(response)
    except Exception as exc:
        _record_interaction("DELETE_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, status="FAILED", success=False, error_message=str(exc))
        return _json_error(exc, 500)


@app.route("/api/workflows/<workflow_id>/workflow-enabled", methods=["POST"])
def workflow_enabled(workflow_id):
    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get("enabled"))
    actor = _actor_context()
    if config.USE_MOCK or not sf.is_configured():
        response = {"ok": True, "workflowId": workflow_id, "workflowEnabled": enabled}
        _record_interaction("TOGGLE_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, payload=payload, response=response)
        return jsonify(response)
    try:
        result = repo.toggle_workflow(workflow_id, enabled)
        monitor_cache.refresh_async()
        response = {"ok": True, **result}
        _record_interaction("TOGGLE_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, payload=payload, response=response)
        return jsonify(response)
    except Exception as exc:
        _record_interaction("TOGGLE_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, status="FAILED", success=False, error_message=str(exc), payload=payload)
        return _json_error(exc, 500)


@app.route("/api/workflows/<workflow_id>/schedule-enabled", methods=["POST"])
def schedule_enabled(workflow_id):
    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get("enabled"))
    actor = _actor_context()
    if config.USE_MOCK or not sf.is_configured():
        response = {"ok": True, "workflowId": workflow_id, "taskEnabled": enabled}
        _record_interaction("TOGGLE_SCHEDULE", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, payload=payload, response=response)
        return jsonify(response)
    try:
        result = repo.toggle_schedule(workflow_id, enabled)
        monitor_cache.refresh_async()
        response = {"ok": True, **result}
        _record_interaction("TOGGLE_SCHEDULE", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, payload=payload, response=response)
        return jsonify(response)
    except Exception as exc:
        _record_interaction("TOGGLE_SCHEDULE", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, status="FAILED", success=False, error_message=str(exc), payload=payload)
        return _json_error(exc, 500)


@app.route("/api/workflows/<workflow_id>/history")
def workflow_history(workflow_id):
    limit = request.args.get("limit", "100")
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"ok": True, "source": "mock", "rows": [r for r in MOCK_HISTORY if r.get("WORKFLOW_ID") == workflow_id]})
    actor = _actor_context()
    try:
        rows = repo.load_workflow_history(workflow_id, limit)
        _record_interaction("VIEW_WORKFLOW_HISTORY", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, payload={"limit": limit}, response={"rowCount": len(rows)})
        return jsonify({"ok": True, "source": "snowflake", "rows": rows})
    except Exception as exc:
        _record_interaction("VIEW_WORKFLOW_HISTORY", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, status="FAILED", success=False, error_message=str(exc), payload={"limit": limit})
        return _json_error(exc, 500)


@app.route("/api/workflows/<workflow_id>/dag")
def workflow_dag(workflow_id):
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"ok": True, "run": {"RUN_ID": "mock-run", "STATUS": "RUNNING"}, "nodes": [], "edges": [], "errors": []})
    actor = _actor_context()
    try:
        result = repo.load_dag_run(workflow_id)
        _record_interaction("VIEW_DAG_RUN", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, response={"ok": True})
        return jsonify({"ok": True, **result})
    except Exception as exc:
        _record_interaction("VIEW_DAG_RUN", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, status="FAILED", success=False, error_message=str(exc))
        return _json_error(exc, 500)


@app.route("/api/history")
def history():
    limit = request.args.get("limit", "200")
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"ok": True, "source": "mock", "rows": MOCK_HISTORY})
    try:
        rows = repo.load_history(limit)
        return jsonify({"ok": True, "source": "snowflake", "rows": rows})
    except Exception as exc:
        app.logger.exception("Failed to load history")
        return jsonify({"ok": False, "source": "error", "error": str(exc), "rows": []}), 500


@app.route("/api/notifications")
def notifications():
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"source": "mock", "rows": []})
    try:
        rows = sf.query_service(f"SELECT * FROM {config.T_NOTIFICATIONS} ORDER BY WORKFLOW_ID", use_warehouse=True, include_context=True)
        from utils import normalize_rows
        return jsonify({"ok": True, "source": "snowflake", "rows": normalize_rows(rows)})
    except Exception as exc:
        return jsonify({"source": "error", "error": str(exc), "rows": []}), 200


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/sfc_oauth_complete")
@app.route("/sfc_oatch_complete")
def snowflake_oauth_complete():
    app.logger.info("Snowflake OAuth callback path reached app: %s", request.path)
    return send_from_directory(app.static_folder, "index.html")


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    if isinstance(error, HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": error.description, "type": error.__class__.__name__}), error.code
        return error

    app.logger.exception("Unhandled application error")
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": str(error), "type": error.__class__.__name__}), 500
    return jsonify({"ok": False, "error": str(error), "type": error.__class__.__name__}), 500


@app.errorhandler(404)
def not_found(error):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "API route not found", "path": request.path}), 404
    return send_from_directory(app.static_folder, "index.html")
