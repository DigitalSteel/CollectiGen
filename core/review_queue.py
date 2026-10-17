from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterable
from uuid import uuid4


_QUEUE_LOCKS_GUARD=RLock()
_QUEUE_LOCKS: dict[str,RLock]={}


def _queue_lock(path: Path) -> RLock:
    key=str(path.resolve(strict=False)).casefold()
    with _QUEUE_LOCKS_GUARD:
        return _QUEUE_LOCKS.setdefault(key,RLock())


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(value: Any) -> str:
    return str(value or "").strip()


@dataclass
class ReviewTask:
    id: str
    task_type: str
    title: str
    image_path: str = ""
    status: str = "pending"
    priority: str = "normal"
    description: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    source_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_type": self.task_type,
            "title": self.title,
            "image_path": self.image_path,
            "status": self.status,
            "priority": self.priority,
            "description": self.description,
            "details": dict(self.details),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source_key": self.source_key,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewTask":
        return cls(
            id=_clean(data.get("id")) or f"task_{uuid4().hex[:12]}",
            task_type=_clean(data.get("task_type")) or "other",
            title=_clean(data.get("title")) or "Review task",
            image_path=_clean(data.get("image_path")),
            status=_clean(data.get("status")) or "pending",
            priority=_clean(data.get("priority")) or "normal",
            description=_clean(data.get("description")),
            details=data.get("details", {}) if isinstance(data.get("details"), dict) else {},
            created_at=_clean(data.get("created_at")) or _utc_now(),
            updated_at=_clean(data.get("updated_at")) or _utc_now(),
            source_key=_clean(data.get("source_key")),
        )


class ReviewTaskDatabase:
    """Persistent queue shared by import, relationship, and review tools."""

    SCHEMA_VERSION = 2
    EVENT_FORMAT = "review_task_events"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.tasks: dict[str, ReviewTask] = {}
        self._source_lookup: dict[str, str] = {}
        self._event_stream=False
        self._lock = _queue_lock(self.path)
        self.load()

    @contextmanager
    def transaction(self):
        """Serialize one in-process queue reconciliation and persistence pass."""
        with self._lock:
            yield self

    @classmethod
    @contextmanager
    def path_transaction(cls,path: str|Path):
        """Serialize a multi-store workflow against this queue path."""
        with _queue_lock(Path(path)):
            yield

    def load(self) -> None:
        with self._lock:
            self.tasks.clear()
            self._source_lookup.clear()
            self._event_stream=False
            if not self.path.exists():
                return
            try:
                with self.path.open("r",encoding="utf-8") as handle:
                    first=handle.readline()
                    try:
                        header=json.loads(first)
                    except (json.JSONDecodeError,TypeError):
                        header=None
                    if (
                        isinstance(header,dict)
                        and header.get("format")==self.EVENT_FORMAT
                        and int(header.get("schema_version") or 0)==self.SCHEMA_VERSION
                    ):
                        self._event_stream=True
                        for line in handle:
                            self._apply_stream_line(line)
                        return
                    handle.seek(0)
                    payload=json.load(handle)
            except (OSError, json.JSONDecodeError, TypeError):
                return
            for item in payload.get("tasks", []):
                if isinstance(item, dict):
                    self._remember(ReviewTask.from_dict(item))

    def _remember(self,task: ReviewTask) -> None:
        previous=self.tasks.get(task.id)
        if previous is not None and previous.source_key:
            if self._source_lookup.get(previous.source_key)==task.id:
                self._source_lookup.pop(previous.source_key,None)
        self.tasks[task.id]=task
        if task.source_key:
            self._source_lookup.setdefault(task.source_key,task.id)

    def _apply_stream_line(self,line: str) -> None:
        try:
            event=json.loads(line)
        except (json.JSONDecodeError,TypeError):
            # A process interruption can leave only the final append partial.
            # Every earlier complete line remains independently valid.
            return
        if not isinstance(event,dict):
            return
        if event.get("event")=="task" and isinstance(event.get("task"),dict):
            self._remember(ReviewTask.from_dict(event["task"]))
            return
        if event.get("event")!="outcome":
            return
        task=self.tasks.get(_clean(event.get("task_id")))
        if task is None:
            return
        status=_clean(event.get("status"))
        if status:
            task.status=status
        updated_at=_clean(event.get("updated_at"))
        if updated_at:
            task.updated_at=updated_at
        if event.get("has_resolution") is True:
            resolution=event.get("resolution")
            task.details["resolution"]=(
                dict(resolution) if isinstance(resolution,dict) else {}
            )

    def _append_outcomes(self,tasks: Iterable[ReviewTask]) -> None:
        encoded=b"".join(
            (
                json.dumps({
                    "event":"outcome","task_id":task.id,
                    "status":task.status,"updated_at":task.updated_at,
                    "has_resolution":"resolution" in task.details,
                    "resolution":task.details.get("resolution"),
                },ensure_ascii=False,separators=(",",":"))+"\n"
            ).encode("utf-8")
            for task in tasks
        )
        if not encoded:
            return
        with self.path.open("r+b") as handle:
            handle.seek(0,os.SEEK_END)
            end=handle.tell()
            if end:
                handle.seek(end-1)
                if handle.read(1)!=b"\n":
                    # Discard only the interrupted final event before adding a
                    # new one. Complete earlier lines remain authoritative.
                    cursor=end
                    newline=-1
                    while cursor>0 and newline<0:
                        start=max(0,cursor-65536)
                        handle.seek(start)
                        chunk=handle.read(cursor-start)
                        found=chunk.rfind(b"\n")
                        if found>=0:
                            newline=start+found
                            break
                        cursor=start
                    if newline<0:
                        raise RuntimeError("The Review queue header is incomplete.")
                    handle.truncate(newline+1)
                    handle.flush(); os.fsync(handle.fileno())
            handle.seek(0,os.SEEK_END)
            offset=handle.tell()
            handle.write(encoded); handle.flush(); os.fsync(handle.fileno())
            handle.seek(offset)
            if handle.read(len(encoded))!=encoded:
                raise RuntimeError("The Review outcome failed read-back verification.")

    def _append_outcome(self,task: ReviewTask) -> None:
        self._append_outcomes((task,))

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(self.path.suffix + ".tmp")
            header={
                "schema_version":self.SCHEMA_VERSION,"format":self.EVENT_FORMAT,
            }
            with temp.open("w",encoding="utf-8",newline="\n") as handle:
                handle.write(json.dumps(header,separators=(",",":"))+"\n")
                for task in sorted(
                    self.tasks.values(),
                    key=lambda item:(
                        item.status!="pending",item.created_at,item.id,
                    ),
                ):
                    handle.write(json.dumps(
                        {"event":"task","task":task.to_dict()},
                        ensure_ascii=False,separators=(",",":"),
                    )+"\n")
                handle.flush(); os.fsync(handle.fileno())
            recovery_manifest=None
            recovery_payload=None
            backup=None
            if self.path.exists() and not self._event_stream:
                stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                recovery_dir=(
                    self.path.parent/"backups"/"review-queue-format"/
                    f"{stamp}_{uuid4().hex[:8]}"
                )
                recovery_dir.mkdir(parents=True,exist_ok=False)
                backup=recovery_dir/self.path.name
                shutil.copy2(self.path,backup)
                original_sha256=self._sha256(self.path)
                if self._sha256(backup)!=original_sha256:
                    raise RuntimeError(
                        "The Review queue recovery copy failed verification."
                    )
                recovery_manifest=recovery_dir/"recovery.json"
                recovery_payload={
                    "schema_version":1,
                    "operation":"review_queue_event_stream_upgrade",
                    "status":"prepared",
                    "target":str(self.path),
                    "backup":backup.name,
                    "original_bytes":backup.stat().st_size,
                    "original_sha256":original_sha256,
                    "created_at":_utc_now(),
                }
                self._write_recovery_manifest(
                    recovery_manifest,recovery_payload,
                )
            try:
                temp.replace(self.path)
                with self.path.open("r",encoding="utf-8") as handle:
                    saved_header=json.loads(handle.readline())
                if saved_header!=header:
                    raise RuntimeError(
                        "The upgraded Review queue failed read-back verification."
                    )
            except Exception:
                if backup is not None and backup.is_file():
                    restore=self.path.with_name(
                        self.path.name+f".{uuid4().hex}.restore"
                    )
                    shutil.copy2(backup,restore)
                    restore.replace(self.path)
                raise
            if recovery_manifest is not None and recovery_payload is not None:
                recovery_payload["status"]="complete"
                recovery_payload["completed_at"]=_utc_now()
                try:
                    self._write_recovery_manifest(
                        recovery_manifest,recovery_payload,
                    )
                except OSError:
                    # The queue and its exact backup are already durable. A
                    # stale "prepared" label must not make the saved decision
                    # appear to have failed and get applied a second time.
                    pass
            self._event_stream=True

    @staticmethod
    def _write_recovery_manifest(path: Path,payload: dict[str,Any]) -> None:
        temporary=path.with_name(path.name+f".{uuid4().hex}.tmp")
        with temporary.open("w",encoding="utf-8",newline="\n") as handle:
            json.dump(payload,handle,indent=2,ensure_ascii=False)
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        temporary.replace(path)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest=hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda:handle.read(1024*1024),b""):
                digest.update(chunk)
        return digest.hexdigest()

    def add_task(
        self,
        *,
        task_type: str,
        title: str,
        image_path: str | Path = "",
        description: str = "",
        details: dict[str, Any] | None = None,
        priority: str = "normal",
        source_key: str = "",
    ) -> ReviewTask:
        with self._lock:
            if source_key:
                existing = self.find_by_source_key(source_key)
                if existing:
                    if existing.status in {"resolved", "ignored"}:
                        return existing
                    next_details=dict(details or {})
                    if (
                        existing.title==title
                        and existing.description==description
                        and existing.details==next_details
                    ):
                        return existing
                    existing.title = title
                    existing.description = description
                    existing.details = next_details
                    existing.updated_at = _utc_now()
                    return existing

            task = ReviewTask(
                id=f"task_{uuid4().hex[:12]}",
                task_type=task_type,
                title=title,
                image_path=str(image_path) if image_path else "",
                priority=priority,
                description=description,
                details=dict(details or {}),
                source_key=source_key,
            )
            self.tasks[task.id] = task
            if source_key:
                self._source_lookup[source_key] = task.id
            return task

    def find_by_source_key(self, source_key: str) -> ReviewTask | None:
        with self._lock:
            task_id = self._source_lookup.get(source_key)
            return self.tasks.get(task_id) if task_id else None

    def set_status(self, task_id: str, status: str) -> bool:
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None or task.status == status:
                return False
            task.status = status
            task.updated_at = _utc_now()
            return True

    def save_status(
        self,task_id: str,status: str,*,resolution: dict|None=None,
    ) -> bool:
        """Append one verified outcome without rewriting an event-stream queue."""
        with self._lock:
            task=self.tasks.get(str(task_id))
            if task is None:
                return False
            changed=task.status!=str(status)
            previous_status=task.status
            previous_updated_at=task.updated_at
            had_resolution="resolution" in task.details
            previous_resolution=task.details.get("resolution")
            if resolution is not None and task.details.get("resolution")!=resolution:
                task.details["resolution"]=dict(resolution)
                changed=True
            if not changed:
                return False
            task.status=str(status); task.updated_at=_utc_now()
            try:
                if self._event_stream:
                    self._append_outcome(task)
                else:
                    # The first changed outcome upgrades the legacy monolithic
                    # JSON snapshot. Subsequent decisions are small appends.
                    self.save()
            except Exception:
                task.status=previous_status
                task.updated_at=previous_updated_at
                if had_resolution:
                    task.details["resolution"]=previous_resolution
                else:
                    task.details.pop("resolution",None)
                raise
            return True

    def pending(self) -> list[ReviewTask]:
        with self._lock:
            return [task for task in self.tasks.values() if task.status == "pending"]

    def reconcile_known_tags(self, knowledge) -> int:
        """Resolve obsolete unknown-tag questions using exact saved identities.

        Review questions keep their original evidence, but once the Tag
        Encyclopedia recognizes the exact SDXL Tag (or one of its aliases), the
        question no longer belongs in the pending queue.
        """
        with self._lock:
            changed = 0
            now = _utc_now()
            for task in self.pending():
                if task.task_type not in {"new_tag", "unknown_tag"}:
                    continue
                if bool(task.details.get("acknowledged")):
                    # Vocabulary intake deliberately creates permanent identity
                    # first, then keeps this separate semantic decision pending.
                    continue
                value = _clean(task.details.get("tag"))
                if not value:
                    continue
                record = knowledge.find(value)
                if record is None:
                    continue
                task.status = "resolved"
                task.updated_at = now
                task.details["resolution"] = {
                    "decision": "already_recognized",
                    "scope": "exact_sdxl_tag",
                    "stable_id": record.id,
                    "display": record.display,
                    "reason": "Recognized in Tag Encyclopedia",
                    "reconciled_at": now,
                }
                changed += 1
            if changed:
                self.save()
            return changed

    @staticmethod
    def queue_health(task: ReviewTask) -> tuple[str,str]:
        """Conservatively identify ingestion noise without deciding meaningful tags."""
        if task.task_type not in {"new_tag","unknown_tag"}:
            return "meaningful","Needs a human decision"
        value=_clean(task.details.get("tag") or task.title)
        normalized=" ".join(value.casefold().split())
        if not value or not re.search(r"[A-Za-z0-9]",value):
            return "punctuation","Contains only punctuation or spacing"
        if any(ord(char)<32 for char in value) or len(value)>160 or re.search(r"(?:https?://|[\\/]{2,})",value,re.I):
            return "invalid","Not a usable tag candidate"
        words=re.findall(r"[A-Za-z0-9']+",value)
        if len(words)>=7 or (len(words)>=4 and value.rstrip().endswith((".","!","?"))):
            return "caption_fragment","Looks like plain-language caption text"
        if not normalized:
            return "invalid","Not a usable tag candidate"
        return "meaningful","Needs a human decision"

    def triage_pending(self) -> dict[str,int]:
        with self._lock:
            counts: dict[str,int]={}; seen: dict[str,str]={}
            for task in sorted(self.pending(),key=lambda item:item.created_at):
                category,reason=self.queue_health(task)
                if task.task_type in {"new_tag","unknown_tag"}:
                    value=_clean(task.details.get("tag") or task.title)
                    key=" ".join(value.casefold().split())
                    if key and key in seen:
                        category,reason="duplicate",f"Duplicates queue item {seen[key]}"
                    elif key: seen[key]=task.id
                task.details["queue_health"]={"category":category,"reason":reason,"recommended_status":"ignored" if category!="meaningful" else ""}
                counts[category]=counts.get(category,0)+1
            return counts

    def bulk_set_status(self, task_ids: Iterable[str], status: str) -> int:
        with self._lock:
            changed=[]
            previous=[]
            for task_id in task_ids:
                task=self.tasks.get(str(task_id))
                if task is None or task.status==status:
                    continue
                previous.append((task,task.status,task.updated_at))
                task.status=str(status); task.updated_at=_utc_now()
                changed.append(task)
            if not changed:
                return 0
            try:
                if self._event_stream:
                    self._append_outcomes(changed)
                else:
                    self.save()
            except Exception:
                for task,old_status,old_updated_at in previous:
                    task.status=old_status; task.updated_at=old_updated_at
                raise
            return len(changed)

    def counts(self) -> dict[str, int]:
        with self._lock:
            result: dict[str, int] = {"all": len(self.tasks)}
            for task in self.tasks.values():
                result[task.status] = result.get(task.status, 0) + 1
                if task.status == "pending":
                    key = f"pending:{task.task_type}"
                    result[key] = result.get(key, 0) + 1
            return result

    def extend(self, tasks: Iterable[ReviewTask]) -> None:
        with self._lock:
            for task in tasks:
                self.tasks[task.id] = task
                if task.source_key:
                    self._source_lookup.setdefault(task.source_key, task.id)
