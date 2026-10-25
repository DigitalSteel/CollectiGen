from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Callable


class EventBus:
    """Small in-process publish/subscribe bus used by shared GUI state."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[..., None]]] = defaultdict(list)
        self._history: deque[dict[str, Any]] = deque(maxlen=100)
        self._lock = RLock()

    def subscribe(self, event: str, callback: Callable[..., None]) -> None:
        with self._lock:
            if callback not in self._subscribers[event]:
                self._subscribers[event].append(callback)

    def unsubscribe(self, event: str, callback: Callable[..., None]) -> None:
        with self._lock:
            callbacks = self._subscribers.get(event, [])
            if callback in callbacks:
                callbacks.remove(callback)

    def publish(self, event: str, **payload: Any) -> None:
        details: dict[str, Any] = {}
        for key in ("active", "busy", "current", "external", "resource", "state", "total"):
            value = payload.get(key)
            if isinstance(value, (bool, int)) or value is None:
                details[key] = value
            elif key in {"resource", "state"} and isinstance(value, str):
                details[key] = value[:80]
        for key in ("label", "owner"):
            value = payload.get(key)
            if isinstance(value, str):
                details[key] = value[:120]
        record = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": str(event)[:120],
            **details,
        }
        with self._lock:
            self._history.append(record)
            callbacks = list(self._subscribers.get(event, []))
        for callback in callbacks:
            callback(**payload)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return bounded, content-free event names and safe operation state."""
        with self._lock:
            return [dict(item) for item in list(self._history)[-max(0, int(limit)) :]]
