import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ActivityJournal:
    def __init__(self, max_entries=2000):
        self._lock = threading.RLock()
        self._entries = deque(maxlen=max(100, int(max_entries)))

    def start(self, category, action, label, details=None, actor=None):
        normalized_category = str(category or "APPLICATION").upper()
        if not actor and normalized_category in ("APPLICATION", "DATABASE"):
            actor = {
                "userName": "BACKEND",
                "displayName": "KUMO backend",
                "roleName": "Application service",
            }
        entry = {
            "id": uuid.uuid4().hex,
            "category": normalized_category,
            "action": str(action or "ACTIVITY").upper(),
            "label": str(label or action or "Activity")[:500],
            "status": "RUNNING",
            "startedAt": _now_iso(),
            "completedAt": None,
            "durationMs": None,
            "actor": actor or {},
            "details": details or {},
            "_startedMonotonic": time.monotonic(),
        }
        with self._lock:
            self._entries.append(entry)
        return entry["id"]

    def finish(self, activity_id, status="SUCCESS", details=None, error=None, actor=None):
        with self._lock:
            entry = next((item for item in reversed(self._entries) if item.get("id") == activity_id), None)
            if not entry:
                return
            entry["status"] = str(status or "SUCCESS").upper()
            entry["completedAt"] = _now_iso()
            entry["durationMs"] = int((time.monotonic() - float(entry.get("_startedMonotonic") or time.monotonic())) * 1000)
            if details:
                entry["details"] = {**(entry.get("details") or {}), **details}
            if actor:
                entry["actor"] = actor
            if error:
                entry["error"] = str(error)[:2000]

    def add(self, category, action, label, status="SUCCESS", details=None, actor=None):
        activity_id = self.start(category, action, label, details=details, actor=actor)
        self.finish(activity_id, status=status)
        return activity_id

    def snapshot(self, limit=500):
        limit = max(1, min(int(limit or 500), 2000))
        with self._lock:
            rows = list(self._entries)[-limit:]
            return [
                {k: v for k, v in dict(item).items() if not str(k).startswith("_")}
                for item in reversed(rows)
            ]


activity_journal = ActivityJournal()
