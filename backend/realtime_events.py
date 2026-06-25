import json
import queue
import time
import uuid
from datetime import datetime, timezone
from threading import RLock


class RealtimeEventBroker:
    """Small in-process Server-Sent Events broker.

    This keeps the app dependency-free. It is intentionally one-way because the
    UI only needs server -> browser status notifications; normal HTTP endpoints
    still handle commands like Run workflow.
    """

    def __init__(self):
        self._lock = RLock()
        self._clients = set()
        self._client_count_callback = None

    def set_client_count_callback(self, callback):
        with self._lock:
            self._client_count_callback = callback

    def client_count(self):
        with self._lock:
            return len(self._clients)

    def _notify_client_count(self, count):
        callback = None
        with self._lock:
            callback = self._client_count_callback
        if callback:
            try:
                callback(count)
            except Exception:
                pass

    def publish(self, event_type, data=None):
        event = {
            "id": str(uuid.uuid4()),
            "type": str(event_type or "message"),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "data": data if data is not None else {},
        }
        with self._lock:
            clients = list(self._clients)

        stale = []
        for client in clients:
            try:
                client.put_nowait(event)
            except queue.Full:
                # Drop the oldest pending message for that client and keep the
                # newest state event. A slow browser should not block Flask.
                try:
                    client.get_nowait()
                    client.put_nowait(event)
                except Exception:
                    stale.append(client)
            except Exception:
                stale.append(client)

        if stale:
            with self._lock:
                for client in stale:
                    self._clients.discard(client)

    def subscribe(self):
        client = queue.Queue(maxsize=100)
        with self._lock:
            self._clients.add(client)
            count = len(self._clients)
        self._notify_client_count(count)
        return client

    def unsubscribe(self, client):
        with self._lock:
            self._clients.discard(client)
            count = len(self._clients)
        self._notify_client_count(count)

    def stream(self, heartbeat_seconds=15):
        client = self.subscribe()

        def encode(event):
            event_type = event.get("type") or "message"
            payload = json.dumps(event, default=str, separators=(",", ":"))
            return f"id: {event.get('id')}\nevent: {event_type}\ndata: {payload}\n\n"

        try:
            yield encode({
                "id": str(uuid.uuid4()),
                "type": "connected",
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "data": {"ok": True},
            })
            while True:
                try:
                    event = client.get(timeout=heartbeat_seconds)
                    yield encode(event)
                except queue.Empty:
                    # Comments are valid SSE heartbeats and are ignored by EventSource.
                    yield f": ping {int(time.time())}\n\n"
        finally:
            self.unsubscribe(client)


realtime_broker = RealtimeEventBroker()
