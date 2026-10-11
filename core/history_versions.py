"""Immutable, explicitly scoped saved-version storage.

The journal and its verified blobs are authority for history only. SQLite holds
disposable event locations; image sidecars remain current knowledge authority.
No library discovery or implicit enrollment is performed here.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
import hashlib
import json
import os
import stat
from pathlib import Path
import sqlite3
from threading import local
from typing import Iterator
from uuid import uuid4

from .operation_journal import OperationJournal, _canonical_bytes, _path_lock


MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 12 * 1024 * 1024
MAX_EVENT_BYTES = 64 * 1024
_HELD = local()


def _entity_locked(method):
    @wraps(method)
    def guarded(self, entity, *args, **kwargs):
        if entity.scope_key != self.scope.key:
            raise HistoryError("History entity is outside this zone/library.")
        image = self.scope.image(self.scope.library_root / entity.locator)
        with self.locked(image):
            self._ready()
            return method(self, entity, *args, **kwargs)
    return guarded


class HistoryError(RuntimeError):
    """History was held without granting a write or restore authority."""


class HistoryIndexPending(HistoryError):
    """Call refresh_index in a background job before retrying this query."""


@contextmanager
def history_lock(path: Path) -> Iterator[None]:
    """Reentrant thread + OS advisory lock for cooperating processes.

    The file is never removed: unlinking a live lock would split its authority.
    Noncooperating writers are protected only by stale/read-back checks.
    """
    path = path.resolve()
    with _path_lock(path):
        held = getattr(_HELD, "paths", None)
        if held is None:
            held = _HELD.paths = set()
        if path in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            held.add(path)
            try:
                yield
            finally:
                held.remove(path)
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class HistoryScope:
    zone_id: str
    library_id: str
    zone_root: Path
    library_root: Path
    history_root: Path
    source_zone_root: Path | None = field(default=None, kw_only=True)

    def __post_init__(self):
        if not self.zone_id or not self.library_id:
            raise HistoryError("Explicit zone and library authority is required.")
        for name in ("zone_root", "library_root", "history_root"):
            raw = Path(getattr(self, name)).absolute()
            _reject_reparse(raw)
            object.__setattr__(self, name, raw.resolve())
        if self.source_zone_root is not None:
            raw = Path(self.source_zone_root).absolute()
            _reject_reparse(raw)
            object.__setattr__(self, "source_zone_root", raw.resolve())
        self.validate()

    def validate(self):
        for path in (self.zone_root, self.library_root, self.history_root):
            _reject_reparse(path)
        if self.source_zone_root is not None:
            _reject_reparse(self.source_zone_root)
            if self.source_zone_root != self.library_root:
                raise HistoryError("Explicit source authority must bind the exact library root.")
        source_root = self.source_zone_root or self.zone_root
        for path, boundary in ((self.library_root, source_root), (self.history_root, self.zone_root)):
            if not path.resolve().is_relative_to(boundary):
                raise HistoryError("History storage/library escaped its authorized zone.")
        if self.history_root.is_relative_to(self.library_root) or self.library_root.is_relative_to(self.history_root):
            raise HistoryError("History and library roots must not overlap.")

    @property
    def key(self) -> str:
        value = {
            "zone": self.zone_id, "library": self.library_id,
            "root": str(self.library_root), "storage": str(self.history_root),
        }
        if self.source_zone_root is not None:
            value.update(source_zone_root=str(self.source_zone_root), zone_root=str(self.zone_root))
        return hashlib.sha256(_canonical_bytes(value)).hexdigest()

    def image(self, path: str | Path) -> Path:
        self.validate()
        raw_path = Path(path).absolute()
        if not raw_path.is_relative_to(self.library_root) or ".." in raw_path.parts:
            raise HistoryError("Image is outside the explicitly authorized library.")
        _reject_reparse(raw_path)
        image = Path(path).resolve()
        if not image.is_relative_to(self.library_root) or image.is_relative_to(self.history_root):
            raise HistoryError("Image is outside the explicitly authorized library.")
        sidecar = image.with_suffix(".tags.json")
        _reject_reparse(sidecar)
        if sidecar.resolve().parent != image.parent or sidecar.is_symlink():
            raise HistoryError("Image Knowledge escaped its image boundary.")
        return image


@dataclass(frozen=True)
class HistoryEntityRef:
    scope_key: str
    entity_id: str
    locator: str
    source_token: str


@dataclass(frozen=True)
class VersionRef:
    scope_key: str
    entity_id: str
    version_id: str
    operation_id: str
    parent_id: str
    restored_from: str
    snapshot: str
    origin: str
    sequence: int
    record_schema: int | None
    coverage: tuple[str, ...]
    status: str = "committed"
    committed_at: str = ""
    observed_at: str = ""
    operation_kind: str = ""
    reason: str = ""
    producer: str = ""
    provenance: str = "native"
    source_timestamp: str = ""
    legacy_reference: str = ""
    legacy_operation_id: str = ""
    restored_units: tuple[str, ...] = ()


@dataclass(frozen=True)
class HistoryPage:
    versions: tuple[VersionRef, ...]
    next_cursor: int | None


@dataclass(frozen=True)
class CaptureIntent:
    entity: HistoryEntityRef
    transaction_id: str
    operation_id: str
    before: str
    after: str
    parent_id: str
    version_id: str
    restored_from: str = ""
    operation_kind: str = ""
    reason: str = ""
    producer: str = ""
    restored_units: tuple[str, ...] = ()


def _reject_reparse(path: Path):
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise HistoryError("History authority cannot cross a reparse/symlink path.")


def _source_token(image: Path) -> str:
    info = image.stat()
    if not image.is_file():
        raise HistoryError("History source is not a regular image file.")
    # Identity evidence, not content equivalence. A replaced/changed source is
    # held for an explicit future binding adapter, never silently merged.
    return hashlib.sha256(_canonical_bytes([
        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
        getattr(info, "st_birthtime_ns", info.st_ctime_ns),
    ])).hexdigest()


def read_record(path: Path) -> bytes | None:
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if before.st_size > MAX_RECORD_BYTES:
                raise HistoryError("Image Knowledge exceeds the admitted read bound.")
            raw = handle.read(MAX_RECORD_BYTES + 1)
            after = os.fstat(handle.fileno())
        current = path.stat()
    except FileNotFoundError:
        if path.exists():
            raise HistoryError("Image Knowledge changed during its read.")
        return None
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    if len(raw) > MAX_RECORD_BYTES or identity(before) != identity(after) or identity(after) != identity(current):
        raise HistoryError("Image Knowledge changed during its bounded read.")
    return raw


def record_token(raw: bytes | None) -> str:
    return "missing" if raw is None else hashlib.sha256(raw).hexdigest()


class HistoryStore:
    def __init__(self, scope: HistoryScope):
        self.scope = scope
        scope.validate()
        self.journal = OperationJournal(scope.history_root / "versions.jsonl", durable_history=True)
        self.index_path = scope.history_root / "versions-index.sqlite"
        self.lock_path = scope.history_root / "versions.lock"
        self._fresh_snapshots: set[tuple[str, str]] = set()

    @contextmanager
    def locked(self, image: Path | None = None):
        self.scope.validate()
        # Check containment BEFORE creating any directory, lock or cache.
        for path in (self.journal.path, self.index_path, self.lock_path, self.journal.snapshot_root,
                     Path(str(self.index_path) + "-journal"), Path(str(self.index_path) + "-wal"), Path(str(self.index_path) + "-shm")):
            _reject_reparse(path)
            if not path.resolve().is_relative_to(self.scope.history_root):
                raise HistoryError("History storage escaped its scope.")
        target = self.scope.image(image) if image is not None else None
        with history_lock(self.lock_path):
            if target is None:
                yield
            else:
                target_lock = target.with_suffix(".tags.json.history.lock")
                _reject_reparse(target_lock)
                with history_lock(target_lock):
                    yield

    @contextmanager
    def _db(self):
        self.scope.history_root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.index_path)
        db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, offset INTEGER, length INTEGER, digest TEXT, kind TEXT, entity TEXT, version TEXT, tx TEXT)")
        db.execute("CREATE INDEX IF NOT EXISTS entity_events ON events(entity,kind,seq)")
        db.execute("CREATE INDEX IF NOT EXISTS version_events ON events(version,kind)")
        db.execute("CREATE INDEX IF NOT EXISTS tx_events ON events(tx,kind)")
        db.execute("CREATE TABLE IF NOT EXISTS snapshot_refs (entity TEXT, digest TEXT, event_seq INTEGER, PRIMARY KEY(entity,digest))")
        if "operation" not in {row[1] for row in db.execute("PRAGMA table_info(events)")}:
            # Disposable older caches cannot answer an exact operation query.
            # Rebuild from the journal through the normal bounded index slices.
            db.execute("ALTER TABLE events ADD COLUMN operation TEXT NOT NULL DEFAULT ''")
            db.execute("DELETE FROM events")
            db.execute("DELETE FROM snapshot_refs")
        db.execute("CREATE INDEX IF NOT EXISTS operation_events ON events(entity,operation,kind,seq)")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _event_at(self, row) -> dict:
        seq, offset, length, digest = row[:4]
        if length <= 0 or length > MAX_EVENT_BYTES or offset < 0:
            raise HistoryError("Invalid history cache location; rebuild the index.")
        with self.journal.path.open("rb") as stream:
            stream.seek(offset)
            raw = stream.read(length)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise HistoryError("History event changed; rebuild/inspect the index.")
        event = self._decode_event(raw)
        if event.get("history_sequence") != seq:
            raise HistoryError("History cache sequence disagrees with authoritative event.")
        event["sequence"] = seq
        return event

    def _decode_event(self, raw: bytes) -> dict:
        try:
            event = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise HistoryError("Invalid history journal event.") from exc
        if not raw.endswith(b"\n") or not isinstance(event, dict):
            raise HistoryError("Incomplete history journal event; recovery required.")
        if type(event.get("schema_version")) is not int or type(event.get("history_schema")) is not int or event.get("schema_version") != 1 or event.get("history_schema") not in {1,2}:
            raise HistoryError("Unsupported history schema; read/restore held.")
        if event.get("scope") != self.scope.key:
            raise HistoryError("History event belongs to another zone/library.")
        if event.get("event") not in {"entity", "binding", "move-instance", "version", "imported-version", "prepare", "recovery-receipt", "abort"}:
            raise HistoryError("Unknown history event; history is held.")
        if event.get("event")=="binding" and event.get("binding_state") not in {"active","moving","retired","transit"}:
            raise HistoryError("Unsupported history binding transition.")
        return event

    def refresh_index(self, *, max_events: int = 256, rebuild: bool = False) -> bool:
        """Bounded incremental work. False requests another background slice."""
        if not 1 <= max_events <= 10000:
            raise ValueError("Invalid history indexing budget.")
        with self.locked(), self._db() as db:
            if rebuild:
                db.execute("DELETE FROM events")
                db.execute("DELETE FROM snapshot_refs")
            last = db.execute("SELECT seq,offset,length,digest FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            offset, seq = 0, 0
            if last:
                self._event_at(last)
                seq, offset = last[0], last[1] + last[2]
            if not self.journal.path.exists():
                return True
            with self.journal.path.open("rb") as stream:
                stream.seek(offset)
                for _ in range(max_events):
                    raw = stream.readline(MAX_EVENT_BYTES + 1)
                    if not raw:
                        return True
                    if len(raw) > MAX_EVENT_BYTES:
                        raise HistoryError("History journal event exceeds its bound.")
                    event = self._decode_event(raw)
                    seq += 1
                    if event.get("history_sequence") != seq:
                        raise HistoryError("History event ordering changed; recovery required.")
                    db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", (
                        seq, offset, len(raw), hashlib.sha256(raw).hexdigest(),
                        event["event"], event.get("entity_id", ""), event.get("version_id", ""), event.get("transaction_id", ""), event.get("operation_id", ""),
                    ))
                    for key in ("snapshot", "before", "after"):
                        if isinstance(event.get(key), str):
                            db.execute("INSERT OR IGNORE INTO snapshot_refs VALUES (?,?,?)", (event.get("entity_id", ""), event[key], seq))
                    offset += len(raw)
                return not stream.read(1)

    def _ready(self):
        if not self.refresh_index():
            raise HistoryIndexPending("History indexing is pending; continue bounded refresh_index work.")

    def _append(self, event: dict):
        self._ready()
        with self._db() as db:
            sequence = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
            last = db.execute("SELECT seq,offset,length,digest FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        # Once an identity transition is used, old readers must keep holding
        # even after a later ordinary save or enrollment becomes the last event.
        schema = 2 if event.get("event") in {"binding","move-instance"} or (last and self._event_at(last).get("history_schema")==2) else 1
        payload = {"scope": self.scope.key, "history_sequence": sequence, **event, "history_schema":schema}
        if len(_canonical_bytes(payload)) + 256 > MAX_EVENT_BYTES:
            raise HistoryError("History event exceeds its storage bound.")
        self.journal._append(payload)
        self._ready()

    def _find(self, *, kind: str, entity: str = "", version: str = "", tx: str = ""):
        clauses, values = ["kind=?"], [kind]
        for name, value in (("entity", entity), ("version", version), ("tx", tx)):
            if value:
                clauses.append(f"{name}=?")
                values.append(value)
        with self._db() as db:
            row = db.execute("SELECT seq,offset,length,digest FROM events WHERE " + " AND ".join(clauses) + " ORDER BY seq DESC LIMIT 1", values).fetchone()
        if row is None:
            return None
        event = self._event_at(row)
        if (event.get("event") != kind
                or (entity and event.get("entity_id") != entity)
                or (version and event.get("version_id") != version)
                or (tx and event.get("transaction_id") != tx)):
            raise HistoryError("History cache metadata disagrees with its event; rebuild required.")
        return event

    def binding(self, *, locator="", entity_id=""):
        """Read the last journaled binding, including reservations/tombstones.

        Callers hold the store lock. This does not enroll or inspect an image.
        """
        if bool(locator)==bool(entity_id):
            raise HistoryError("Choose one exact binding locator or entity.")
        column="version" if locator else "entity"
        value=hashlib.sha256(locator.casefold().encode("utf-8")).hexdigest() if locator else entity_id
        with self._db() as db:
            row=db.execute(f"SELECT seq,offset,length,digest FROM events WHERE kind IN ('entity','binding') AND {column}=? ORDER BY seq DESC LIMIT 1",(value,)).fetchone()
        event=self._event_at(row) if row is not None else None
        if event is not None and ((locator and event.get("locator","").casefold()!=locator.casefold()) or (entity_id and event.get("entity_id")!=entity_id)):
            raise HistoryError("History binding index disagrees with its journal.")
        return event

    def transition_binding(self,entity_id,locator,source_token,*,state,receipt_id,transit_path=""):
        """Private move adapter publication; ordinary saves cannot rebind."""
        if state not in {"active","moving","retired","transit"} or not receipt_id:
            raise HistoryError("A verified move receipt is required for binding changes.")
        self.scope.image(self.scope.library_root/locator)
        self._append({"event":"binding","entity_id":entity_id,"locator":locator,
            "version_id":hashlib.sha256(locator.casefold().encode("utf-8")).hexdigest(),
            "source_token":source_token,"binding_state":state,"receipt_id":receipt_id,
            "transit_path":str(transit_path)})

    @staticmethod
    def _active_binding(event):
        return event is not None and event.get("binding_state","active")=="active"

    def bind(self, image: str | Path) -> HistoryEntityRef:
        image = self.scope.image(image)
        locator = image.relative_to(self.scope.library_root).as_posix()
        entity_id = hashlib.sha256(locator.casefold().encode("utf-8")).hexdigest()
        # Locator hash is only an index key, never the entity identity.
        with self.locked(image):
            self._ready()
            event = self.binding(locator=locator)
            token = _source_token(image)
            if event is None or event.get("binding_state")=="retired":
                event = {"event": "entity", "entity_id": uuid4().hex,
                         "version_id": entity_id, "locator": locator, "source_token": token}
                self._append(event)
            if not self._active_binding(event):
                raise HistoryError("History identity is reserved by a managed move; complete exact move recovery first.")
            if event.get("locator", "").casefold() != locator.casefold() or event.get("source_token") != token:
                raise HistoryError("Image identity changed; an explicit binding decision is required.")
            return HistoryEntityRef(self.scope.key, event["entity_id"], event["locator"], token)

    def lookup(self, image: str | Path) -> HistoryEntityRef | None:
        """Read an existing binding without enrollment, locks, or cache writes.

        A missing/stale cache asks the browser's explicit background index job
        to refresh; opening a single image never scans the whole journal.
        """
        image = self.scope.image(image)
        for path in (self.journal.path, self.index_path):
            _reject_reparse(path)
            if not path.resolve().is_relative_to(self.scope.history_root):
                raise HistoryError("History lookup escaped its admitted storage boundary.")
        if not self.journal.path.exists():
            if self.index_path.exists():
                raise HistoryError("History journal is unavailable; its index is not authority.")
            return None
        if not self.index_path.exists():
            raise HistoryIndexPending("History lookup requires an explicit bounded index refresh.")
        locator = image.relative_to(self.scope.library_root).as_posix()
        locator_key = hashlib.sha256(locator.casefold().encode("utf-8")).hexdigest()
        try:
            db = sqlite3.connect(self.index_path.as_uri() + "?mode=ro&immutable=1", uri=True)
            try:
                last = db.execute("SELECT seq,offset,length,digest FROM events ORDER BY seq DESC LIMIT 1").fetchone()
                row = db.execute("SELECT seq,offset,length,digest FROM events WHERE kind IN ('entity','binding') AND version=? ORDER BY seq DESC LIMIT 1", (locator_key,)).fetchone()
            finally:
                db.close()
        except sqlite3.Error as exc:
            raise HistoryIndexPending("History lookup index is unavailable; rebuild it explicitly.") from exc
        if last is None or last[1] + last[2] != self.journal.path.stat().st_size:
            raise HistoryIndexPending("History lookup index is stale; refresh it explicitly.")
        self._event_at(last)
        if row is None:
            return None
        event = self._event_at(row)
        if event.get("event") not in {"entity","binding"} or event.get("version_id") != locator_key or event.get("locator", "").casefold() != locator.casefold():
            raise HistoryError("History lookup cache disagrees with its authoritative binding.")
        if event.get("binding_state")=="retired":return None
        if not self._active_binding(event):
            raise HistoryError("History identity is reserved by a managed move; complete exact move recovery first.")
        token = _source_token(image)
        if event.get("source_token") != token:
            raise HistoryError("Source identity changed; existing history cannot be silently rebound.")
        return HistoryEntityRef(self.scope.key, event["entity_id"], event["locator"], token)

    def validate_entity(self, entity: HistoryEntityRef) -> Path:
        if entity.scope_key != self.scope.key:
            raise HistoryError("History entity is outside this zone/library.")
        image = self.scope.image(self.scope.library_root / entity.locator)
        event = self.binding(entity_id=entity.entity_id)
        if not self._active_binding(event) or event.get("locator") != entity.locator or event.get("source_token") != entity.source_token:
            raise HistoryError("History entity binding is not authoritative.")
        if _source_token(image) != entity.source_token:
            raise HistoryError("Source identity changed; history read/restore held.")
        return image

    @_entity_locked
    def snapshot(self, entity: HistoryEntityRef, raw: bytes | None) -> str:
        self.validate_entity(entity)
        if raw is not None and len(raw) > MAX_RECORD_BYTES:
            raise HistoryError("Record exceeds snapshot bound.")
        value = {"history_schema": 1, "scope": self.scope.key, "entity_id": entity.entity_id,
                 "exists": raw is not None, "raw": base64.b64encode(raw).decode("ascii") if raw is not None else None}
        digest = self.journal.store_verified_snapshot(value, max_bytes=MAX_SNAPSHOT_BYTES)
        self._fresh_snapshots.add((entity.entity_id, digest))
        return digest

    @_entity_locked
    def payload(self, entity: HistoryEntityRef, digest: str) -> bytes | None:
        self.validate_entity(entity)  # BEFORE touching the referenced blob.
        if (entity.entity_id, digest) not in self._fresh_snapshots:
            with self._db() as db:
                row = db.execute("SELECT e.seq,e.offset,e.length,e.digest FROM snapshot_refs r JOIN events e ON e.seq=r.event_seq WHERE r.entity=? AND r.digest=?", (entity.entity_id, digest)).fetchone()
            if row is None:
                raise HistoryError("Snapshot authority is not admitted for this entity.")
            event = self._event_at(row)
            if event.get("entity_id") != entity.entity_id or digest not in (event.get("snapshot"), event.get("before"), event.get("after")):
                raise HistoryError("Snapshot authority index disagrees with its journal event.")
        try:
            value = self.journal.load_verified_snapshot(digest, max_bytes=MAX_SNAPSHOT_BYTES)
            if type(value.get("history_schema")) is not int or value.get("history_schema") != 1 or value.get("scope") != self.scope.key or value.get("entity_id") != entity.entity_id:
                raise HistoryError("Snapshot authority/schema does not match its entity.")
            if value.get("exists") is False and value.get("raw") is None:
                return None
            if value.get("exists") is not True or not isinstance(value.get("raw"), str):
                raise HistoryError("Invalid history presence marker.")
            raw = base64.b64decode(value["raw"], validate=True)
            if len(raw) > MAX_RECORD_BYTES:
                raise HistoryError("Snapshot record exceeds its read bound.")
            return raw
        except (ValueError, OSError, TypeError, AttributeError) as exc:
            raise HistoryError(f"History snapshot unavailable or invalid: {exc}") from exc

    def _version(self, event) -> VersionRef:
        if event.get("event") not in {"version", "imported-version"}:
            raise HistoryError("History cache redirected a version to another event kind.")
        return VersionRef(self.scope.key, event["entity_id"], event["version_id"], event["operation_id"],
                          event.get("parent_id", ""), event.get("restored_from", ""), event["snapshot"], event["origin"], event["sequence"],
                          event.get("record_schema"), tuple(event.get("coverage", ())),
                          committed_at=event.get("at", ""), observed_at=event.get("at", ""),
                          operation_kind=event.get("operation_kind", ""), reason=event.get("reason", ""),
                          producer=event.get("producer", ""), provenance=event.get("provenance", "native"),
                          source_timestamp=event.get("source_timestamp", ""), legacy_reference=event.get("legacy_reference", ""),
                          legacy_operation_id=event.get("legacy_operation_id", ""), restored_units=tuple(event.get("restored_units", ())))

    def version(self, entity: HistoryEntityRef, version_id: str) -> VersionRef:
        with self.locked():
            self._ready()
            self.validate_entity(entity)
            event = self._find(kind="version", entity=entity.entity_id, version=version_id)
            if event is None:
                event = self._find(kind="imported-version", entity=entity.entity_id, version=version_id)
            if event is None:
                raise HistoryError("Version is unavailable for this entity.")
            return self._version(event)

    def query(self, entity: HistoryEntityRef, *, cursor: int | None = None, limit: int = 50, operation_id: str = "") -> HistoryPage:
        if not 1 <= limit <= 200 or (cursor is not None and cursor <= 0):
            raise ValueError("Invalid history page request.")
        with self.locked():
            self._ready()
            self.validate_entity(entity)
            with self._db() as db:
                operation_clause = " AND operation=?" if operation_id else ""
                values = [entity.entity_id, cursor or 2**63-1]
                if operation_id:
                    values.append(operation_id)
                rows = db.execute("SELECT seq,offset,length,digest FROM events WHERE entity=? AND kind IN ('version','imported-version') AND seq<?" + operation_clause + " ORDER BY seq DESC LIMIT ?",
                                  (*values, limit + 1)).fetchall()
            versions = tuple(self._version(self._event_at(row)) for row in rows[:limit])
            if any(version.entity_id != entity.entity_id for version in versions):
                raise HistoryError("History cache entity differs from authoritative event.")
            if operation_id and any(version.operation_id != operation_id for version in versions):
                raise HistoryError("History cache operation differs from authoritative event.")
            return HistoryPage(versions, versions[-1].sequence if len(rows) > limit else None)

    def _record_version(self, entity, snapshot, *, operation_id, parent_id="", origin="save", version_id=None, restored_from="", transaction_id="",
                        operation_kind="", reason="", producer="", provenance="native", source_timestamp="", legacy_reference="", legacy_operation_id="", coverage=None, restored_units=()):
        version_id = version_id or uuid4().hex
        raw = self.payload(entity, snapshot)
        try:
            value = json.loads(raw) if raw is not None else {}
            schema = value.get("schema_version") if isinstance(value, dict) else None
        except (ValueError, UnicodeError):
            schema = None
        self._append({"event": "imported-version" if origin == "imported" else "version", "entity_id": entity.entity_id, "version_id": version_id,
                      "snapshot": snapshot, "operation_id": operation_id, "parent_id": parent_id,
                      "origin": origin, "restored_from": restored_from, "transaction_id": transaction_id,
                      "record_schema": schema, "coverage": coverage if coverage is not None else (["image-knowledge-record"] if raw is not None else ["record-absence"]),
                      "operation_kind": operation_kind, "reason": reason, "producer": producer,
                      "provenance": provenance, "source_timestamp": source_timestamp, "legacy_reference": legacy_reference,
                      "legacy_operation_id": legacy_operation_id, "restored_units": list(restored_units)})
        return version_id

    @_entity_locked
    def current_head(self, entity: HistoryEntityRef) -> VersionRef | None:
        self.validate_entity(entity)
        event = self._find(kind="version", entity=entity.entity_id)
        return self._version(event) if event is not None else None

    @_entity_locked
    def next_version(self, entity: HistoryEntityRef, target: VersionRef) -> VersionRef:
        """Earliest saved boundary after the target, used as the undo base."""
        if self.version(entity, target.version_id) != target:
            raise HistoryError("Target version authority changed.")
        with self._db() as db:
            row = db.execute("SELECT seq,offset,length,digest FROM events WHERE entity=? AND kind='version' AND seq>? ORDER BY seq LIMIT 1", (entity.entity_id, target.sequence)).fetchone()
        result = self._version(self._event_at(row)) if row else target
        if result.entity_id != entity.entity_id or (row and result.sequence <= target.sequence):
            raise HistoryError("History base cache disagrees with its event.")
        return result

    @_entity_locked
    def prepare(self, entity: HistoryEntityRef, before: bytes | None, after: bytes | None,
                *, operation_id: str, restored_from: str = "", operation_kind: str = "", reason: str = "", producer: str = "", restored_units: tuple[str, ...] = ()) -> CaptureIntent | None:
        self._ready()
        image = self.validate_entity(entity)
        self.reconcile(entity)
        if read_record(image.with_suffix(".tags.json")) != before:
            raise HistoryError("Current record changed before history preparation.")
        current_head = self.current_head(entity)
        previous = (current_head,) if current_head is not None else ()
        before_ref = self.snapshot(entity, before)
        parent = previous[0].version_id if previous else ""
        if not previous or previous[0].snapshot != before_ref:
            parent = self._record_version(entity, before_ref, operation_id=operation_id,
                                          parent_id=parent, origin="baseline" if not previous else "observed-current",
                                          operation_kind=operation_kind, reason=reason, producer=producer)
        # Semantic no-op preserves original saved bytes without fabricating a save.
        if before == after or (before is not None and after is not None and json.loads(before) == json.loads(after)):
            return None
        after_ref = self.snapshot(entity, after)
        intent = CaptureIntent(entity, uuid4().hex, operation_id, before_ref, after_ref, parent, uuid4().hex, restored_from, operation_kind, reason, producer, tuple(restored_units))
        self._append({"event": "prepare", "entity_id": entity.entity_id, "transaction_id": intent.transaction_id,
                      "operation_id": operation_id, "before": before_ref, "after": after_ref,
                      "parent_id": parent, "version_id": intent.version_id, "restored_from": restored_from,
                      "operation_kind": operation_kind, "reason": reason, "producer": producer, "restored_units": list(restored_units)})
        return intent

    def commit(self, intent: CaptureIntent):
        with self.locked(self.scope.library_root / intent.entity.locator):
            self._ready()
            return self._commit(intent)

    def _commit(self, intent: CaptureIntent):
        image = self.validate_entity(intent.entity)
        prepared = self._find(kind="prepare", tx=intent.transaction_id)
        expected = {
            "entity_id": intent.entity.entity_id, "transaction_id": intent.transaction_id,
            "operation_id": intent.operation_id, "before": intent.before, "after": intent.after,
            "parent_id": intent.parent_id, "version_id": intent.version_id,
            "restored_from": intent.restored_from,
            "operation_kind": intent.operation_kind, "reason": intent.reason, "producer": intent.producer,
            "restored_units": list(intent.restored_units),
        }
        if prepared is None or any(prepared.get(key, [] if key == "restored_units" else "") != value for key, value in expected.items()):
            raise HistoryError("Capture intent differs from its immutable journaled preparation.")
        if self._find(kind="abort", tx=intent.transaction_id) is not None:
            raise HistoryError("An aborted capture cannot be committed.")
        existing = self._find(kind="version", tx=intent.transaction_id)
        if existing is not None:
            committed = {key: value for key, value in expected.items() if key not in {"before", "after"}}
            committed["snapshot"] = intent.after
            committed["origin"] = "restore" if intent.restored_from else "save"
            if any(existing.get(key, [] if key == "restored_units" else "") != value for key, value in committed.items()):
                raise HistoryError("Committed version disagrees with its immutable preparation.")
            return  # Idempotent history completion never rewrites newer current data.
        if read_record(image.with_suffix(".tags.json")) != self.payload(intent.entity, intent.after):
            raise HistoryError("Write/history commit interrupted or changed; reconciliation required.")
        self._verify_recovery_receipt(intent)
        self._record_version(intent.entity, intent.after, operation_id=intent.operation_id,
                             parent_id=intent.parent_id, origin="restore" if intent.restored_from else "save",
                             version_id=intent.version_id, restored_from=intent.restored_from,
                             transaction_id=intent.transaction_id, operation_kind=intent.operation_kind,
                             reason=intent.reason, producer=intent.producer, restored_units=intent.restored_units)

    def attach_recovery_receipt(self, intent: CaptureIntent, manifest: Path):
        """Bind a canonical recovery descriptor before its storage mutation."""
        with self.locked(self.scope.library_root / intent.entity.locator):
            self._ready()
            self.validate_entity(intent.entity)
            prepared = self._find(kind="prepare", entity=intent.entity.entity_id, tx=intent.transaction_id)
            if prepared is None or prepared.get("before") != intent.before or prepared.get("after") != intent.after:
                raise HistoryError("Recovery receipt lacks an exact prepared capture.")
            manifest = Path(manifest).absolute()
            _reject_reparse(manifest)
            if not manifest.is_relative_to(self.scope.history_root):
                raise HistoryError("Saved-record recovery receipt must stay in history storage.")
            raw = read_record(manifest)
            if raw is None or len(raw) > MAX_EVENT_BYTES:
                raise HistoryError("Recovery receipt is missing or exceeds its bound.")
            value = json.loads(raw)
            descriptor = {key: value for key, value in value.items() if key not in {"status", "updated_at"}}
            self._append({"event": "recovery-receipt", "entity_id": intent.entity.entity_id,
                          "transaction_id": intent.transaction_id, "manifest": manifest.relative_to(self.scope.history_root).as_posix(),
                          "descriptor": hashlib.sha256(_canonical_bytes(descriptor)).hexdigest()})

    def _verify_recovery_receipt(self, intent):
        receipt = self._find(kind="recovery-receipt", entity=intent.entity.entity_id, tx=intent.transaction_id)
        before, after = self.payload(intent.entity, intent.before), self.payload(intent.entity, intent.after)
        if receipt is None:
            if after is None and before is not None:
                raise HistoryError("Absence completion requires a canonical quarantine receipt.")
            return
        manifest = self.scope.history_root / receipt.get("manifest", "")
        _reject_reparse(manifest.absolute())
        if not manifest.resolve().is_relative_to(self.scope.history_root):
            raise HistoryError("Recovery receipt escaped history storage.")
        raw = read_record(manifest)
        if raw is None or len(raw) > MAX_EVENT_BYTES:
            raise HistoryError("Recovery receipt is missing or exceeds its bound.")
        value = json.loads(raw)
        descriptor = {key: item for key, item in value.items() if key not in {"status", "updated_at"}}
        if hashlib.sha256(_canonical_bytes(descriptor)).hexdigest() != receipt.get("descriptor"):
            raise HistoryError("Recovery receipt descriptor changed after preparation.")
        image = self.validate_entity(intent.entity)
        if (value.get("entity_id") != intent.entity.entity_id or value.get("target") != str(image.with_suffix(".tags.json"))
                or value.get("before_token") != record_token(before) or value.get("after_token") != record_token(after)):
            raise HistoryError("Recovery receipt does not describe the prepared entity/states.")
        backup = manifest.parent / value.get("backup", "")
        quarantine = Path(value.get("quarantine", ""))
        _reject_reparse(backup.absolute())
        _reject_reparse(quarantine.absolute())
        if backup.resolve().parent != manifest.parent.resolve() or quarantine.parent != image.parent or not quarantine.name.startswith(f".{image.name}.") or not quarantine.name.endswith(".history-quarantine"):
            raise HistoryError("Recovery receipt paths are outside their exact boundaries.")
        if before is not None and read_record(backup) != before:
            raise HistoryError("Recovery receipt backup differs from protected current data.")
        if after is None and before is not None and read_record(quarantine) != before:
            raise HistoryError("Interrupted absence completion requires the exact retained quarantine.")

    @_entity_locked
    def has_pending(self, entity: HistoryEntityRef) -> bool:
        """Metadata-only recovery readiness; comparison must not commit writes."""
        self.validate_entity(entity)
        with self._db() as db:
            rows = db.execute("SELECT p.seq,p.offset,p.length,p.digest FROM events p WHERE p.kind='prepare' AND p.entity=? AND NOT EXISTS (SELECT 1 FROM events f WHERE f.tx=p.tx AND f.kind IN ('version','abort')) ORDER BY p.seq LIMIT 101", (entity.entity_id,)).fetchall()
        if len(rows) > 100:
            raise HistoryError("Too many interrupted history transactions; recovery required.")
        for row in rows:
            event = self._event_at(row)
            if event.get("event") != "prepare" or event.get("entity_id") != entity.entity_id:
                raise HistoryError("Recovery readiness cache disagrees with its authoritative event.")
        return bool(rows)

    @_entity_locked
    def reconcile(self, entity: HistoryEntityRef) -> tuple[str, ...]:
        """Reconcile only this entity; divergent current remains held and intact."""
        with self.locked():
            self._ready()
            image = self.validate_entity(entity)
            with self._db() as db:
                rows = db.execute("SELECT p.seq,p.offset,p.length,p.digest FROM events p WHERE p.kind='prepare' AND p.entity=? AND NOT EXISTS (SELECT 1 FROM events f WHERE f.tx=p.tx AND f.kind IN ('version','abort')) ORDER BY p.seq LIMIT 101", (entity.entity_id,)).fetchall()
            if len(rows) > 100:
                raise HistoryError("Too many interrupted history transactions; recovery required.")
            recovered = []
            for row in rows:
                event = self._event_at(row)
                current = read_record(image.with_suffix(".tags.json"))
                after = self.payload(entity, event["after"])
                before = self.payload(entity, event["before"])
                if current == after:
                    self.commit(CaptureIntent(entity, event["transaction_id"], event["operation_id"], event["before"], event["after"], event["parent_id"], event["version_id"], event.get("restored_from", ""), event.get("operation_kind", ""), event.get("reason", ""), event.get("producer", ""), tuple(event.get("restored_units", ()))))
                elif current == before:
                    self._append({"event": "abort", "entity_id": entity.entity_id, "transaction_id": event["transaction_id"]})
                else:
                    raise HistoryError("Interrupted history has divergent current data; preserve it and resolve recovery.")
                recovered.append(event["transaction_id"])
            return tuple(recovered)
