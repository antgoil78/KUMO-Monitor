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
        self._last_refresh_at = None
        self._last_duration_ms = None
        self._last_phase_ms = {}
        self._refresh_count = 0
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
            with self._lock:
                duration_seconds = float(self._last_duration_ms or 0) / 1000.0
                interval_seconds = float(self.refresh_seconds)
            # Keep refresh starts on the configured cadence. Without subtracting
            # query duration, a 10s query plus a 30s wait becomes a 40s cycle.
            wait_seconds = max(0.5, interval_seconds - duration_seconds)
            self._refresh_requested.wait(timeout=wait_seconds)
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

    def set_refresh_seconds(self, seconds):
        seconds = max(2, int(seconds or self.refresh_seconds or 5))
        with self._lock:
            self.refresh_seconds = seconds
        self._refresh_requested.set()

    def get(self):
        with self._lock:
            return self._payload

    def diagnostics(self):
        with self._lock:
            age = None
            if self._last_refresh_monotonic:
                age = round(time.monotonic() - float(self._last_refresh_monotonic), 1)
            return {
                "enabled": bool(self._enabled),
                "threadAlive": bool(self._thread and self._thread.is_alive()),
                "refreshing": bool(self._refreshing),
                "refreshSeconds": int(self.refresh_seconds),
                "lastRefreshAt": self._last_refresh_at,
                "lastRefreshAgeSeconds": age,
                "lastDurationMs": self._last_duration_ms,
                "lastPhaseMs": dict(self._last_phase_ms),
                "refreshCount": int(self._refresh_count),
                "lastError": str(self._last_error) if self._last_error else None,
            }

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

        realtime_broker.publish("monitor_refresh_started", self.diagnostics())

        started = time.monotonic()
        try:
            payload = self._build_payload()
            should_publish = False
            signature = self._signature(payload)
            with self._lock:
                self._payload = payload
                self._last_error = None
                self._last_refresh_monotonic = time.monotonic()
                self._last_refresh_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                self._last_duration_ms = int((time.monotonic() - started) * 1000)
                self._refresh_count += 1
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
                self._last_refresh_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                self._last_duration_ms = int((time.monotonic() - started) * 1000)
                self._refresh_count += 1
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
            realtime_broker.publish("monitor_refresh_completed", self.diagnostics())

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
        with sf.query_tag("KUMO_MONITOR_SHARED_CACHE"):
            with sf.connection_scope(use_warehouse=True, include_context=True, force_service=True):
                connected_at = time.perf_counter()
                workflows_started = connected_at
                workflows = repo.load_monitor_rows()
                engine_started = time.perf_counter()
                engine = repo.get_engine_state()
        finished_at = time.perf_counter()
        phase_ms = {
            "connection": int((connected_at - started) * 1000),
            "workflows": int((engine_started - workflows_started) * 1000),
            "engine": int((finished_at - engine_started) * 1000),
            "total": int((finished_at - started) * 1000),
        }
        with self._lock:
            self._last_phase_ms = phase_ms
        logger.info(
            "KUMO_MONITOR_TIMING total_ms=%d connection_ms=%d workflows_ms=%d engine_ms=%d rows=%d",
            phase_ms["total"],
            phase_ms["connection"],
            phase_ms["workflows"],
            phase_ms["engine"],
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
