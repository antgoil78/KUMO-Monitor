import threading
from datetime import datetime, timezone

import config
from mock_data import MOCK_MONITOR
import snowflake_client as sf
import kumo_repository as repo


class MonitorCache:
    def __init__(self, refresh_seconds):
        self.refresh_seconds = max(2, int(refresh_seconds or 5))
        self._lock = threading.RLock()
        self._payload = None
        self._stop_event = threading.Event()
        self._thread = None
        self._last_error = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self.refresh(force=True)
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
        if self._payload is None:
            self.refresh(force=True)
        with self._lock:
            return self._payload

    def refresh(self, force=False):
        try:
            payload = self._build_payload()
            with self._lock:
                self._payload = payload
                self._last_error = None
            return payload
        except Exception as exc:
            fallback = self._error_payload(exc)
            with self._lock:
                self._payload = fallback
                self._last_error = str(exc)
            return fallback

    def _build_payload(self):
        if config.USE_MOCK or not sf.is_configured():
            payload = dict(MOCK_MONITOR)
            payload["refreshIntervalMs"] = self.refresh_seconds * 1000
            payload["generatedAt"] = datetime.now(timezone.utc).isoformat()
            return payload

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
        payload = dict(MOCK_MONITOR)
        payload["source"] = "error-fallback"
        payload["generatedAt"] = datetime.now(timezone.utc).isoformat()
        payload["refreshIntervalMs"] = self.refresh_seconds * 1000
        payload["error"] = str(exc)
        return payload


monitor_cache = MonitorCache(config.REFRESH_SECONDS)
