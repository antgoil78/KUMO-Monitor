import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone

import config
from mock_data import MOCK_MONITOR
import snowflake_client as sf
import kumo_repository as repo
from realtime_events import realtime_broker

logger = logging.getLogger(__name__)


class MonitorCache:
    def __init__(self, refresh_seconds):
        self.refresh_seconds = max(2, int(refresh_seconds or 5))
        self._lock = threading.RLock()
        self._payload = self._fallback_payload(source="starting")
        self._stop_event = threading.Event()
        self._thread = None
        self._last_error = None
        self._last_signature = None
        self._last_refresh_monotonic = 0.0
        self._refreshing = False
        self._refresh_done = threading.Condition(self._lock)
        self._refresh_requested = threading.Event()
        self._enabled = False

    def start(self):
        with self._lock:
            self._enabled = True
            self._stop_event.clear()
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._loop,
                name="kumo-monitor-refresh",
                daemon=True,
            )
            self._thread.start()

    def _loop(self):
        self.refresh(force=True)
        while True:
            self._refresh_requested.wait(timeout=self.refresh_seconds)
            self._refresh_requested.clear()
            if self._stop_event.is_set() or not self.is_enabled():
                break
            self.refresh(force=True)
        with self._lock:
            self._thread = None

    def stop(self):
        with self._lock:
            self._enabled = False
            self._stop_event.set()
            self._refresh_requested.set()

    def is_enabled(self):
        with self._lock:
            return bool(self._enabled)

    def set_enabled(self, enabled):
        if enabled:
            self.start()
        else:
            self.stop()

    def get(self):
        with self._lock:
            return self._payload

    def get_or_refresh(self, max_age_seconds=None):
        max_age = self.refresh_seconds if max_age_seconds is None else max(0, float(max_age_seconds))
        with self._lock:
            age = time.monotonic() - float(self._last_refresh_monotonic or 0)
            has_real_payload = self._payload.get("source") not in ("starting", "error-fallback")
            if has_real_payload and age <= max_age:
                return self._payload
        return self.refresh(force=True)

    def refresh_async(self):
        if self.is_enabled():
            self._refresh_requested.set()
        return self.get()

    def refresh(self, force=False):
        with self._lock:
            if self._refreshing:
                self._refresh_done.wait(timeout=max(10.0, self.refresh_seconds * 4.0))
                return self._payload
            self._refreshing = True

        try:
            payload = self._build_payload()
            should_publish = False
            signature = self._signature(payload)
            with self._lock:
                self._payload = payload
                self._last_error = None
                self._last_refresh_monotonic = time.monotonic()
                if signature != self._last_signature:
                    self._last_signature = signature
                    should_publish = True
            if should_publish:
                realtime_broker.publish("monitor_update", payload)
            return payload
        except Exception as exc:
            fallback = self._error_payload(exc)
            should_publish = False
            signature = self._signature(fallback)
            with self._lock:
                self._payload = fallback
                self._last_error = str(exc)
                self._last_refresh_monotonic = time.monotonic()
                if signature != self._last_signature:
                    self._last_signature = signature
                    should_publish = True
            if should_publish:
                realtime_broker.publish("monitor_update", fallback)
            return fallback
        finally:
            with self._lock:
                self._refreshing = False
                self._refresh_done.notify_all()

    def _signature(self, payload):
        """Return a stable signature for fields that change the visible monitor state."""
        workflows = payload.get("workflows") or []
        significant = {
            "engine": (payload.get("engine") or {}).get("status"),
            "workflows": sorted([
                {
                    "workflowId": w.get("workflowId"),
                    "workflowName": w.get("workflowName"),
                    "workflowGroup": w.get("workflowGroup"),
                    "workflowType": w.get("workflowType"),
                    "workflowEnabled": w.get("workflowEnabled"),
                    "taskEnabled": w.get("taskEnabled"),
                    "lastRunId": w.get("lastRunId"),
                    "lastStatus": w.get("lastStatus"),
                    "lastStartTime": w.get("lastStartTime"),
                    "lastEndTime": w.get("lastEndTime"),
                    "lastRequestedAt": w.get("lastRequestedAt"),
                    "lastRequestedBy": w.get("lastRequestedBy"),
                }
                for w in workflows
            ], key=lambda row: str(row.get("workflowId") or "")),
        }
        raw = json.dumps(significant, default=str, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _build_payload(self):
        if config.USE_MOCK or not sf.is_configured():
            return self._fallback_payload(source="mock")

        started = time.perf_counter()
        with sf.connection_scope(use_warehouse=True, include_context=True, force_service=True):
            workflows_started = time.perf_counter()
            workflows = repo.load_monitor_rows()
            engine_started = time.perf_counter()
            engine = repo.get_engine_state()
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "KUMO_MONITOR_TIMING total_ms=%d workflows_ms=%d engine_ms=%d rows=%d",
            elapsed_ms,
            int((engine_started - workflows_started) * 1000),
            int((time.perf_counter() - engine_started) * 1000),
            len(workflows or []),
        )
        return {
            "source": "snowflake",
            "engine": engine,
            "summary": repo.build_summary(workflows),
            "workflows": workflows,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "refreshIntervalMs": self.refresh_seconds * 1000,
            "error": None,
        }

    def _error_payload(self, exc):
        return self._fallback_payload(source="error-fallback", error=str(exc))

    def _fallback_payload(self, source, error=None):
        payload = dict(MOCK_MONITOR)
        payload["source"] = source
        payload["generatedAt"] = datetime.now(timezone.utc).isoformat()
        payload["refreshIntervalMs"] = self.refresh_seconds * 1000
        payload["error"] = error
        return payload


monitor_cache = MonitorCache(config.REFRESH_SECONDS)
