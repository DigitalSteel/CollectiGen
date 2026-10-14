from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock,RLock
from typing import Any, Iterable
from uuid import uuid4


_LOCKS: dict[str,RLock] = {}
_LOCKS_GUARD = Lock()


def _path_lock(path: Path) -> RLock:
    key=str(path.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key,RLock())


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass
class JournalOperation:
    id: str
    kind: str
    status: str
    created_at: str
    updated_at: str
    reason: str = ""
    expected: int = 0
    completed: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    changes: list[dict[str, Any]] = field(default_factory=list)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)


class OperationJournal:
    """Append-only transaction journal with content-addressed payload snapshots."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path, *, retention: int = 50,
                 durable_history: bool = False) -> None:
        self.path = Path(path)
        self.snapshot_root = self.path.with_name(self.path.stem + "_snapshots")
        self.retention = max(1, int(retention))
        self.durable_history = durable_history
        self._lock=_path_lock(self.path)

    def _append(self, event: dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"schema_version": self.SCHEMA_VERSION, "at": _utc_now(), **event}
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def store_snapshot(self, value: Any) -> str:
        content = _canonical_bytes(value)
        digest = hashlib.sha256(content).hexdigest()
        target = self.snapshot_root / f"{digest}.json"
        with self._lock:
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(".json.tmp")
                with temporary.open("wb") as handle:
                    handle.write(content + b"\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(target)
        return digest

    def load_snapshot(self, digest: str) -> Any:
        return json.loads((self.snapshot_root / f"{digest}.json").read_text(encoding="utf-8"))

    def load_verified_snapshot(self, digest: str, *, max_bytes: int) -> Any:
        """Bounded H1 admission; callers establish zone/entity authority first.

        Legacy load_snapshot semantics intentionally remain unchanged. The hash
        names canonical JSON bytes, excluding the single storage newline.
        """
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or max_bytes < 1:
            raise ValueError("Invalid history snapshot reference or bound.")
        root = self.snapshot_root.resolve()
        target = root / f"{digest}.json"
        if target.resolve().parent != root or target.is_symlink():
            raise ValueError("History snapshot escaped its storage boundary.")
        if getattr(target.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError("History snapshot uses an unapproved reparse path.")
        with target.open("rb") as handle:
            if os.fstat(handle.fileno()).st_size > max_bytes:
                raise ValueError("History snapshot exceeds its admitted size.")
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError("History snapshot exceeds its admitted size.")
        content = raw[:-1] if raw.endswith(b"\n") else raw
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("History snapshot integrity check failed.")
        value = json.loads(content)
        if _canonical_bytes(value) != content:
            raise ValueError("History snapshot is not canonical JSON.")
        return value

    def store_verified_snapshot(self, value: Any, *, max_bytes: int) -> str:
        content = _canonical_bytes(value)
        if len(content) + 1 > max_bytes:
            raise ValueError("History snapshot exceeds its admitted size.")
        digest = hashlib.sha256(content).hexdigest()
        target = self.snapshot_root / f"{digest}.json"
        with self._lock:
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(f".{digest}.{uuid4().hex}.tmp")
                try:
                    with temporary.open("xb") as handle:
                        handle.write(content + b"\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    # Cooperating H1 writers hold the enclosing scope lock.
                    # Publish without replacing a competing snapshot target.
                    if os.name == "nt":
                        try:
                            os.rename(temporary, target)
                        except FileExistsError:
                            pass
                    else:
                        try:
                            os.link(temporary, target)
                        except FileExistsError:
                            pass
                finally:
                    temporary.unlink(missing_ok=True)
            self.load_verified_snapshot(digest, max_bytes=max_bytes)
        return digest

    def begin(self, kind: str, *, expected: int = 0, reason: str = "", metadata: dict[str, Any] | None = None) -> str:
        operation_id = f"op_{uuid4().hex}"
        self._append({
            "event": "begin", "operation_id": operation_id, "kind": kind,
            "expected": int(expected), "reason": reason, "metadata": dict(metadata or {}),
        })
        return operation_id

    def append_change(self, operation_id: str, *, item: str, before: Any, after: Any,
                      reason: str = "", metadata: dict[str, Any] | None = None) -> None:
        self._append({
            "event": "change", "operation_id": operation_id, "item": item,
            "before": self.store_snapshot(before), "after": self.store_snapshot(after),
            "reason": reason, "metadata": dict(metadata or {}),
        })

    def append_checkpoint(
        self,operation_id: str,*,item: str,
        metadata: dict[str,Any]|None=None,
    ) -> None:
        """Durably record completed non-undoable work for a resumable job."""
        self._append({
            "event":"checkpoint","operation_id":operation_id,
            "item":str(item),"metadata":dict(metadata or {}),
        })

    def resume(self,operation_id: str,*,message: str="") -> None:
        self._append({
            "event":"resume","operation_id":operation_id,
            "message":str(message or ""),
        })

    def finish(
        self,
        operation_id: str,
        status: str = "complete",
        *,
        message: str = "",
        result: dict[str, Any] | None = None,
    ) -> None:
        if status not in {"complete", "cancelled", "failed", "undone"}:
            raise ValueError(f"Unsupported operation status: {status}")
        self._append({
            "event": status,
            "operation_id": operation_id,
            "message": message,
            "result": dict(result or {}),
        })

    def operations(self) -> list[JournalOperation]:
        result: dict[str, JournalOperation] = {}
        with self._lock:
            if not self.path.exists():
                return []
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return []
        for line in lines:
            try:
                event = json.loads(line)
                operation_id = str(event["operation_id"])
                event_type = str(event.get("event") or "")
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if event_type == "begin":
                stamp = str(event.get("at") or _utc_now())
                result[operation_id] = JournalOperation(
                    id=operation_id,
                    kind=str(event.get("kind") or "operation"),
                    status="applying",
                    created_at=stamp,
                    updated_at=stamp,
                    reason=str(event.get("reason") or ""),
                    expected=int(event.get("expected") or 0),
                    completed=0,
                    metadata=dict(event.get("metadata") or {}),
                    changes=[],
                    checkpoints=[],
                    result={},
                )
                continue
            operation = result.get(operation_id)
            if operation is None:
                continue
            operation.updated_at = str(event.get("at") or operation.updated_at)
            if event_type == "change":
                operation.changes.append({
                    "item": str(event.get("item") or ""), "before": str(event.get("before") or ""),
                    "after": str(event.get("after") or ""), "reason": str(event.get("reason") or ""),
                    "metadata": dict(event.get("metadata") or {}),
                })
                operation.completed = max(
                    len(operation.changes),len({
                        str(value.get("item") or "")
                        for value in operation.checkpoints
                    }),
                )
            elif event_type == "checkpoint":
                operation.checkpoints.append({
                    "item":str(event.get("item") or ""),
                    "metadata":dict(event.get("metadata") or {}),
                })
                operation.completed=max(
                    len(operation.changes),len({
                        str(value.get("item") or "")
                        for value in operation.checkpoints
                    }),
                )
            elif event_type == "resume":
                operation.status="applying"
                operation.result={}
            elif event_type in {"complete", "cancelled", "failed", "undone"}:
                operation.status = event_type
                operation.result = dict(event.get("result") or {})
        return list(result.values())

    def require_intact_events(self, *, max_line_bytes: int = 4 * 1024 * 1024) -> None:
        """Reject damaged JSONL before using absence of an event as recovery evidence.

        Ordinary history browsing deliberately tolerates damaged unrelated rows;
        an active batch cannot do that when deciding whether a transfer began.
        This check streams the file so it does not materialize a large journal.
        """
        if max_line_bytes < 1:
            raise ValueError("Choose a positive journal event bound.")
        with self._lock:
            if not self.path.exists():
                raise ValueError("The saved operation journal is missing.")
            begun: set[str] = set()
            with self.path.open("rb") as handle:
                number = 0
                while raw := handle.readline(max_line_bytes + 1):
                    number += 1
                    if len(raw) > max_line_bytes or not raw.endswith(b"\n"):
                        raise ValueError(f"Operation journal event {number} is incomplete or too large.")
                    try:
                        event = json.loads(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ValueError(f"Operation journal event {number} is damaged.") from exc
                    if (not isinstance(event, dict)
                            or event.get("schema_version") != self.SCHEMA_VERSION
                            or not isinstance(event.get("event"), str)
                            or not isinstance(event.get("operation_id"), str)
                            or not event["operation_id"]):
                        raise ValueError(f"Operation journal event {number} is incomplete.")
                    kind = event["event"]
                    operation_id = event["operation_id"]
                    if kind == "begin":
                        if (operation_id in begun
                                or not isinstance(event.get("kind"), str)
                                or not isinstance(event.get("metadata"), dict)
                                or type(event.get("expected")) is not int):
                            raise ValueError(f"Operation journal begin {number} is invalid.")
                        begun.add(operation_id)
                    elif operation_id not in begun:
                        raise ValueError(f"Operation journal event {number} has no begin.")
                    elif kind == "checkpoint":
                        if (not isinstance(event.get("item"), str)
                                or not isinstance(event.get("metadata"), dict)):
                            raise ValueError(f"Operation journal checkpoint {number} is invalid.")
                    elif kind == "change":
                        if (not isinstance(event.get("item"), str)
                                or not isinstance(event.get("before"), str)
                                or not isinstance(event.get("after"), str)
                                or not isinstance(event.get("metadata"), dict)):
                            raise ValueError(f"Operation journal change {number} is invalid.")
                    elif kind in {"complete", "cancelled", "failed", "undone"}:
                        if not isinstance(event.get("result"), dict):
                            raise ValueError(f"Operation journal completion {number} is invalid.")
                    elif kind != "resume":
                        raise ValueError(f"Operation journal event {number} is unsupported.")

    def latest(self, *, kind: str = "", statuses: Iterable[str] = ()) -> JournalOperation | None:
        wanted = set(statuses)
        matches = [item for item in self.operations() if (not kind or item.kind == kind) and (not wanted or item.status in wanted)]
        return matches[-1] if matches else None

    def prune(self) -> dict[str, int]:
        """Compact old terminal operations while retaining every active/recent transaction."""
        if self.durable_history:
            return {"operations_removed": 0, "snapshots_removed": 0}
        with self._lock:
            if not self.path.exists():
                return {"operations_removed": 0, "snapshots_removed": 0}
            # Pruning is destructive: never erase a malformed event that may
            # be the only evidence of a begun family transfer.
            self.require_intact_events()
            operations = self.operations()
            terminal = [item for item in operations if item.status != "applying"]
            keep_ids = {item.id for item in operations if item.status == "applying"}
            active_batches = {
                item.id for item in operations
                if item.kind == "ingest_add_batch" and item.status == "applying"
            }
            keep_ids.update(
                item.id for item in operations
                if str(item.metadata.get("batch_parent_id") or "") in active_batches
            )
            keep_ids.update(item.id for item in terminal[-self.retention:])
            if len(keep_ids) == len(operations):
                return {"operations_removed": 0, "snapshots_removed": 0}
            kept_events=[]; referenced=set()
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try: event=json.loads(line)
                except json.JSONDecodeError: continue
                if str(event.get("operation_id") or "") not in keep_ids: continue
                kept_events.append(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
                for key in ("before", "after"):
                    if event.get(key): referenced.add(str(event[key]))
                if event.get("event") == "begin" and event.get("kind") == "ingest_add_batch":
                    metadata = event.get("metadata")
                    if isinstance(metadata, dict) and isinstance(metadata.get("chunks"), list):
                        referenced.update(str(digest) for digest in metadata["chunks"])
                result_payload=event.get("result")
                if isinstance(result_payload,dict) and result_payload.get("snapshot"):
                    referenced.add(str(result_payload["snapshot"]))
            temporary=self.path.with_suffix(self.path.suffix+".tmp")
            temporary.write_text("\n".join(kept_events)+("\n" if kept_events else ""),encoding="utf-8")
            temporary.replace(self.path)
            removed=0
            if self.snapshot_root.exists():
                for snapshot in self.snapshot_root.glob("*.json"):
                    if snapshot.stem not in referenced:
                        snapshot.unlink(); removed+=1
            return {"operations_removed": len(operations)-len(keep_ids), "snapshots_removed": removed}
