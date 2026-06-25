import hashlib
import json
import threading
from datetime import datetime, timezone

import config
from mock_data import MOCK_MONITOR
import snowflake_client as sf
import kumo_repository as repo
from realtime_events import realtime_broker


class MonitorCache:
    def __init__(self, refresh_seconds):
        self.refresh_seconds = max(2, int(refresh_seconds or 5))
        self._lock = threading.RLock()
        self._payload = self._fallback_payload(source="starting")
        self._stop_event = threading.Event()
        self._thread = None
        self._last_error = None
        self._last_signature = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop,
            name="kumo-monitor-refresh",
            daemon=True,
        )
        self._thread.start()

    def _loop(self):
        while not self._stop_event.wait(self.refresh_seconds):
            self.refresh(force=True)

    def stop(self):
        self._stop_event.set()

    def get(self):
        with self._lock:
            return self._payload

    def refresh(self, force=False):
        try:
            payload = self._build_payload()
            should_publish = False
            signature = self._signature(payload)
            with self._lock:
                self._payload = payload
                self._last_error = None
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
                if signature != self._last_signature:
                    self._last_signature = signature
                    should_publish = True
            if should_publish:
                realtime_broker.publish("monitor_update", fallback)
            return fallback

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

        workflows = repo.load_monitor_rows()
        return {
            "source": "snowflake",
            "engine": repo.get_engine_state(),
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
