"""Explicit structured-domain history; canonical adapters retain write authority.

No settings, source files, images or legacy stores are discovered here. Adapters
hold canonical locks and validate identity while providing exact immutable state.
The journal is authoritative. Its disposable in-memory index is replayed in bounded
slices on reopen; indexed results are checked against their original event bytes.
"""
from __future__ import annotations

import base64
from bisect import bisect_left
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
from typing import Callable
from threading import local
from uuid import uuid4

from .history_versions import history_lock, _reject_reparse
from .operation_journal import OperationJournal, _canonical_bytes

MAX_EVENT_BYTES = 128 * 1024
MAX_STATE_BYTES = 64 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 96 * 1024 * 1024
MAX_UNITS = 1000
MAX_CHANGES = 128
MAX_INDEX_EVENTS = 250_000
MAX_PAGE = 200
MAX_TRANSACTION_BYTES = 64 * 1024 * 1024
_ACTIVE_GUARDS = local()


class StructuredHistoryError(RuntimeError):
    """Exact history authority could not be established; leave current data alone."""


class StructuredIndexPending(StructuredHistoryError):
    """Continue bounded refresh_index work before using this store."""


def _text(value, label, maximum=256):
    if type(value) is not str or not value or len(value.encode("utf-8")) > maximum or "\0" in value:
        raise StructuredHistoryError(f"Invalid {label}.")
    return value


def _digest(value):
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _id():
    return uuid4().hex


def _hex(value):
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


@dataclass(frozen=True)
class DomainScopeRef:
    binding_id: str
    generation: int
    profile_authority_id: str
    zone_root: Path
    history_root: Path
    authority_ids: tuple[str, ...]
    configuration_fingerprint: str
    domain_versions: tuple[tuple[str, int], ...]
    writable_domains: tuple[str, ...] | None = None

    def __post_init__(self):
        for value in (self.binding_id, self.profile_authority_id, self.configuration_fingerprint):
            _text(value, "scope identity")
        if type(self.generation) is not int or self.generation < 1:
            raise StructuredHistoryError("Invalid binding generation.")
        if type(self.authority_ids) is not tuple or not 0 < len(self.authority_ids) <= MAX_CHANGES:
            raise StructuredHistoryError("An explicit bounded authority set is required.")
        if len(set(self.authority_ids)) != len(self.authority_ids):
            raise StructuredHistoryError("Repeated canonical authority.")
        for value in self.authority_ids:
            _text(value, "canonical authority")
        if type(self.domain_versions) is not tuple or not 0 < len(self.domain_versions) <= MAX_CHANGES:
            raise StructuredHistoryError("Explicit domain schemas are required.")
        for entry in self.domain_versions:
            if type(entry) is not tuple or len(entry) != 2 or type(entry[1]) is not int or entry[1] != 1:
                raise StructuredHistoryError("Unsupported domain schema.")
            _text(entry[0], "domain")
        if len(dict(self.domain_versions)) != len(self.domain_versions):
            raise StructuredHistoryError("Repeated domain schema.")
        writable = tuple(dict(self.domain_versions)) if self.writable_domains is None else self.writable_domains
        if type(writable) is not tuple or len(set(writable)) != len(writable) or not set(writable) <= set(dict(self.domain_versions)):
            raise StructuredHistoryError("Invalid writable domain admission.")
        object.__setattr__(self, "writable_domains", writable)
        for name in ("zone_root", "history_root"):
            raw = Path(getattr(self, name))
            if not raw.is_absolute() or ".." in raw.parts or str(raw).startswith("\\\\"):
                raise StructuredHistoryError("Choose explicit private local storage paths.")
            object.__setattr__(self, name, Path(os.path.normpath(str(raw))))
        if self.zone_root == Path(self.zone_root.anchor) or not self.history_root.is_relative_to(self.zone_root):
            raise StructuredHistoryError("History must stay in a bounded private zone.")

    @property
    def key(self):
        return _digest({"binding": self.binding_id, "generation": self.generation,
            "profile": self.profile_authority_id, "zone": str(self.zone_root),
            "root": str(self.history_root)})


@dataclass(frozen=True)
class DomainEntityRef:
    domain: str
    authority_id: str
    kind: str
    key: str
    incarnation: str

    def __post_init__(self):
        for value in (self.domain, self.authority_id, self.kind, self.key, self.incarnation):
            _text(value, "entity identity", 4096)

    @property
    def identity(self):
        return _digest(_entity_json(self))


def _entity_json(entity):
    return {name: getattr(entity, name) for name in ("domain", "authority_id", "kind", "key", "incarnation")}


@dataclass(frozen=True)
class DomainUnit:
    key: str
    schema_version: int
    value_json: str | None
    dependencies: tuple[DomainEntityRef, ...] = ()

    def __post_init__(self):
        _text(self.key, "unit key", 4096)
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise StructuredHistoryError("Unsupported unit schema.")
        if self.value_json is not None:
            if type(self.value_json) is not str or len(self.value_json.encode("utf-8")) > MAX_STATE_BYTES:
                raise StructuredHistoryError("Unit payload exceeds its bound.")
            try:
                json.loads(self.value_json, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            except (ValueError, RecursionError) as exc:
                raise StructuredHistoryError("Unit payload is not supported JSON.") from exc
        if type(self.dependencies) is not tuple or len(self.dependencies) > MAX_CHANGES:
            raise StructuredHistoryError("Unit dependencies exceed their bound.")
        if any(not isinstance(item, DomainEntityRef) for item in self.dependencies):
            raise StructuredHistoryError("Invalid unit dependency.")


@dataclass(frozen=True)
class CanonicalEvidence:
    authority_id: str
    raw: bytes | None

    def __post_init__(self):
        _text(self.authority_id, "evidence authority")
        if self.raw is not None and (type(self.raw) is not bytes or len(self.raw) > MAX_STATE_BYTES):
            raise StructuredHistoryError("Canonical evidence exceeds its bound.")


@dataclass(frozen=True)
class DomainState:
    entity: DomainEntityRef
    present: bool
    units: tuple[DomainUnit, ...]
    evidence: tuple[CanonicalEvidence, ...]

    def __post_init__(self):
        if not isinstance(self.entity, DomainEntityRef) or type(self.present) is not bool:
            raise StructuredHistoryError("Invalid domain state.")
        if type(self.units) is not tuple or not 0 < len(self.units) <= MAX_UNITS:
            raise StructuredHistoryError("An explicit bounded unit set is required.")
        if any(not isinstance(unit, DomainUnit) for unit in self.units) or len({unit.key for unit in self.units}) != len(self.units):
            raise StructuredHistoryError("Invalid or duplicate domain units.")
        if not self.present and any(unit.value_json is not None for unit in self.units):
            raise StructuredHistoryError("Absent entities cannot contain present units.")
        if type(self.evidence) is not tuple or not 0 < len(self.evidence) <= MAX_CHANGES:
            raise StructuredHistoryError("Exact canonical evidence is required.")
        if any(not isinstance(value, CanonicalEvidence) for value in self.evidence) or len({e.authority_id for e in self.evidence}) != len(self.evidence):
            raise StructuredHistoryError("Invalid or duplicate canonical evidence.")
        if self.entity.authority_id not in {value.authority_id for value in self.evidence}:
            raise StructuredHistoryError("The entity authority is absent from canonical evidence.")
        if sum(len(e.raw or b"") for e in self.evidence) + sum(len((u.value_json or "").encode("utf-8")) for u in self.units) > MAX_STATE_BYTES:
            raise StructuredHistoryError("Domain state exceeds its bound.")


def _state_json(state):
    return {"schema": 1, "entity": _entity_json(state.entity), "present": state.present,
        "units": [{"key": u.key, "schema_version": u.schema_version, "value_json": u.value_json,
            "dependencies": [_entity_json(e) for e in u.dependencies]} for u in state.units],
        "evidence": [{"authority_id": e.authority_id,
            "raw": None if e.raw is None else base64.b64encode(e.raw).decode("ascii")} for e in state.evidence]}


def _state_from(value):
    try:
        if type(value["schema"]) is not int or value["schema"] != 1:
            raise StructuredHistoryError("Unsupported state schema.")
        return DomainState(DomainEntityRef(**value["entity"]), value["present"],
            tuple(DomainUnit(u["key"], u["schema_version"], u["value_json"],
                tuple(DomainEntityRef(**e) for e in u["dependencies"])) for u in value["units"]),
            tuple(CanonicalEvidence(e["authority_id"], None if e["raw"] is None else
                base64.b64decode(e["raw"], validate=True)) for e in value["evidence"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise StructuredHistoryError("Invalid saved domain payload.") from exc


def _meaning(state):
    value = _state_json(state)
    value.pop("evidence")
    return value


@dataclass(frozen=True)
class DomainChange:
    before: DomainState
    after: DomainState

    def __post_init__(self):
        if self.before.entity != self.after.entity:
            raise StructuredHistoryError("A change cannot substitute entity identity.")
        if tuple(u.key for u in self.before.units) != tuple(u.key for u in self.after.units):
            raise StructuredHistoryError("Capture both presence and absence for the exact same unit scope.")
        if tuple(e.authority_id for e in self.before.evidence) != tuple(e.authority_id for e in self.after.evidence):
            raise StructuredHistoryError("Canonical authority scope changed within the mutation.")


@dataclass(frozen=True)
class DomainVersionRef:
    scope_key: str
    entity: DomainEntityRef
    version_id: str
    operation_id: str
    transaction_id: str
    parent_id: str
    restored_from: str
    payload: str
    origin: str
    sequence: int
    created_at: str


@dataclass(frozen=True)
class PreparedMember:
    entity: DomainEntityRef
    before: str
    after: str
    parent_id: str
    version_id: str
    authority_ids: tuple[str, ...]
    restored_from: str = ""


@dataclass(frozen=True)
class PreparedDomainReceipt:
    scope_key: str
    admission_fingerprint: str
    transaction_id: str
    operation_id: str
    operation_kind: str
    reason: str
    members: tuple[PreparedMember, ...]


@dataclass(frozen=True)
class DomainCursor:
    scope_key: str
    entity_identity: str
    operation_id: str
    upper_sequence: int
    before_sequence: int


@dataclass(frozen=True)
class DomainHistoryPage:
    versions: tuple[DomainVersionRef, ...]
    next_cursor: DomainCursor | None


@dataclass(frozen=True)
class DomainObservationResult:
    version: DomainVersionRef
    outcome: str


class StructuredHistoryStore:
    """History only. Every public operation enters explicit current authority.

    authority_guard(scope) is a context manager holding configuration authority;
    nested use of the same factory/scope enters it once (it need not be reentrant).
    Lock order: guard, store, then adapter-owned canonical locks.
    Adapters can wrap prepare/write/commit in ``with store.locked():``. A
    read_current callback must prove identities and return exactly the selected
    states under the canonical locks; it must never silently repair data.
    """

    def __init__(self, scope: DomainScopeRef, *, authority_guard):
        if not isinstance(scope, DomainScopeRef) or not callable(authority_guard):
            raise StructuredHistoryError("Explicit scope and authority guard are required.")
        self.scope, self.authority_guard = scope, authority_guard
        self._journal = None
        self.lock_path = scope.history_root / "versions.lock"
        self._reset_index()

    @property
    def journal(self):
        # OperationJournal establishes a resolved-path lock. Defer that metadata
        # access until guarded use; declaring the domain store performs no I/O.
        if self._journal is None:
            self._journal = OperationJournal(self.scope.history_root / "versions.jsonl", durable_history=True)
        return self._journal

    def _reset_index(self):
        self._index_failure = ""
        self._offset = 0
        self._sequence = 0
        self._identity = None
        self._last_pointer = None
        self._versions = {}
        self._heads = {}
        self._by_entity = {}
        self._by_operation = {}
        self._receipts = {}
        self._terminal = {}
        self._pending_ids = set()

    def _paths(self):
        for path in (self.scope.zone_root, self.scope.history_root, self.lock_path,
                     self.journal.path, self.journal.snapshot_root):
            _reject_reparse(path)
        if self.scope.history_root.resolve() != self.scope.history_root or not self.scope.history_root.is_relative_to(self.scope.zone_root):
            raise StructuredHistoryError("Structured history storage changed its boundary.")

    @contextmanager
    def locked(self):
        active = getattr(_ACTIVE_GUARDS, "values", None)
        if active is None:
            active = _ACTIVE_GUARDS.values = set()
        key = (id(self.authority_guard), self.scope)
        if key in active:
            self._paths()
            with history_lock(self.lock_path):
                yield
            return
        with self.authority_guard(self.scope):
            active.add(key)
            try:
                self._paths()
                with history_lock(self.lock_path):
                    self._paths()
                    yield
            finally:
                active.remove(key)

    def _entity(self, entity):
        if not isinstance(entity, DomainEntityRef) or entity.domain not in dict(self.scope.domain_versions) or entity.authority_id not in self.scope.authority_ids:
            raise StructuredHistoryError("Entity is outside the approved domain authority.")

    def _state(self, state):
        self._entity(state.entity)
        for evidence in state.evidence:
            if evidence.authority_id not in self.scope.authority_ids:
                raise StructuredHistoryError("Evidence is outside approved canonical authority.")
        for unit in state.units:
            for dependency in unit.dependencies:
                self._entity(dependency)

    def _writable(self, entity):
        self._entity(entity)
        if entity.domain not in self.scope.writable_domains:
            raise StructuredHistoryError("This domain is retained read-only; new writes are not admitted.")

    def _read_event(self, pointer):
        offset, length, digest = pointer
        if type(offset) is not int or type(length) is not int or offset < 0 or not 0 < length <= MAX_EVENT_BYTES:
            raise StructuredHistoryError("History index pointer is invalid; rebuild it.")
        with self.journal.path.open("rb") as handle:
            handle.seek(offset)
            raw = handle.read(length)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise StructuredHistoryError("History event/index integrity changed; rebuild only verified evidence.")
        return self._decode(raw)

    def _decode(self, raw):
        try:
            event = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise StructuredHistoryError("Corrupt structured history event.") from exc
        if (not raw.endswith(b"\n") or type(event) is not dict or
                type(event.get("structured_schema")) is not int or event["structured_schema"] != 1):
            raise StructuredHistoryError("Unsupported or interrupted structured history schema.")
        if event.get("scope") != self.scope.key:
            raise StructuredHistoryError("History event belongs to another scope.")
        if type(event.get("sequence")) is not int or event["sequence"] < 1:
            raise StructuredHistoryError("Invalid history sequence.")
        if event.get("kind") not in {"baseline", "observed", "prepared", "committed", "unchanged"}:
            raise StructuredHistoryError("Unsupported structured history event kind.")
        try:
            stamp = event["at"]
            if type(stamp) is not str or len(stamp) > 48:
                raise ValueError("Invalid timestamp")
            parsed = datetime.fromisoformat(stamp)
            if parsed.utcoffset() != timedelta(0):
                raise ValueError("History time must be recorded in UTC")
        except (KeyError, ValueError) as exc:
            raise StructuredHistoryError("Invalid recorded history timestamp.") from exc
        return event

    def _version_from(self, row, event):
        try:
            entity = DomainEntityRef(**row["entity"])
            for key in ("version_id", "operation_id"):
                _text(row[key], key)
            if not _hex(row["payload"]):
                raise StructuredHistoryError("Invalid history payload reference.")
            if row["origin"] not in {"baseline", "observed", "save", "restore"}:
                raise StructuredHistoryError("Unknown version origin.")
            return DomainVersionRef(self.scope.key, entity, row["version_id"], row["operation_id"],
                row["transaction_id"], row["parent_id"], row["restored_from"], row["payload"],
                row["origin"], event["sequence"], event["at"])
        except (KeyError, TypeError) as exc:
            raise StructuredHistoryError("Invalid saved version envelope.") from exc

    def _receipt_from(self, event):
        try:
            members = tuple(PreparedMember(DomainEntityRef(**m["entity"]), m["before"], m["after"],
                m["parent_id"], m["version_id"], tuple(m["authority_ids"]), m["restored_from"]) for m in event["members"])
            receipt = PreparedDomainReceipt(self.scope.key, event["admission_fingerprint"], event["transaction_id"], event["operation_id"],
                event["operation_kind"], event["reason"], members)
            for value in (receipt.transaction_id, receipt.operation_id, receipt.operation_kind):
                _text(value, "prepared identity")
            if not 0 < len(members) <= MAX_CHANGES or len({m.entity for m in members}) != len(members):
                raise StructuredHistoryError("Invalid prepared selection.")
            for member in members:
                if not member.authority_ids or len(set(member.authority_ids)) != len(member.authority_ids):
                    raise StructuredHistoryError("Invalid prepared canonical authorities.")
                for value in member.authority_ids:
                    _text(value, "prepared canonical authority")
                if not _hex(member.before) or not _hex(member.after):
                    raise StructuredHistoryError("Invalid prepared payload reference.")
            return receipt
        except (KeyError, TypeError) as exc:
            raise StructuredHistoryError("Invalid prepared history envelope.") from exc

    def _index_event(self, event, pointer):
        if event["sequence"] != self._sequence + 1:
            raise StructuredHistoryError("Structured history sequence is discontinuous.")
        kind = event["kind"]
        versions = tuple(self._version_from(row, event) for row in event.get("versions", ()))
        if kind in {"baseline", "observed"} and len(versions) != 1:
            raise StructuredHistoryError("Invalid baseline coverage.")
        if kind == "prepared":
            receipt = self._receipt_from(event)
            if receipt.transaction_id in self._receipts or versions:
                raise StructuredHistoryError("Repeated or invalid prepared transaction.")
            self._receipts[receipt.transaction_id] = (receipt, pointer)
            self._pending_ids.add(receipt.transaction_id)
        elif kind in {"committed", "unchanged"}:
            tx = event.get("transaction_id")
            if tx not in self._receipts or tx in self._terminal:
                raise StructuredHistoryError("History completion lacks one exact pending transaction.")
            receipt = self._receipts[tx][0]
            if kind == "committed" and [self._version_row(m, receipt) for m in receipt.members] != event.get("versions"):
                raise StructuredHistoryError("Committed versions do not match their prepared scope.")
            if kind == "unchanged" and versions:
                raise StructuredHistoryError("Unchanged recovery cannot publish versions.")
            self._terminal[tx] = (kind, pointer)
            self._pending_ids.remove(tx)
        for version in versions:
            if version.version_id in self._versions or version.parent_id != self._heads.get(version.entity.identity, ""):
                raise StructuredHistoryError("History version identity or parent changed.")
            self._versions[version.version_id] = (version, pointer)
            self._heads[version.entity.identity] = version.version_id
            row = (version.sequence, version.version_id)
            self._by_entity.setdefault(version.entity.identity, []).append(row)
            self._by_operation.setdefault((version.entity.identity, version.operation_id), []).append(row)
        self._sequence = event["sequence"]
        self._last_pointer = pointer

    def refresh_index(self, *, max_events=256, rebuild=False):
        if type(max_events) is not int or not 1 <= max_events <= 4096:
            raise StructuredHistoryError("Choose a bounded index slice.")
        with self.locked():
            if rebuild:
                self._reset_index()
            if self._index_failure:
                raise StructuredHistoryError("History replay failed; explicit verified rebuild is required: " + self._index_failure)
            try:
                return self._replay(max_events)
            except Exception as exc:
                # A malformed linked event can fail after partial in-memory
                # adoption. No operation may reuse that state until full replay.
                self._index_failure = str(exc)
                if isinstance(exc, StructuredHistoryError):
                    raise
                raise StructuredHistoryError("History replay failed; retained journal was not changed.") from exc

    def _replay(self, max_events):
        if not self.journal.path.exists():
            if self._offset:
                raise StructuredHistoryError("History journal disappeared.")
            return True
        info = self.journal.path.stat()
        identity = (info.st_dev, info.st_ino)
        if self._identity is not None and self._identity != identity or info.st_size < self._offset:
            raise StructuredHistoryError("History journal was replaced or truncated.")
        self._identity = identity
        if self._last_pointer:
            self._read_event(self._last_pointer)
        with self.journal.path.open("rb") as handle:
            handle.seek(self._offset)
            for _ in range(max_events):
                raw = handle.readline(MAX_EVENT_BYTES + 1)
                if not raw:
                    return True
                if len(raw) > MAX_EVENT_BYTES or self._sequence >= MAX_INDEX_EVENTS:
                    raise StructuredHistoryError("Structured history exceeds its supported bound.")
                event = self._decode(raw)
                pointer = (self._offset, len(raw), hashlib.sha256(raw).hexdigest())
                self._index_event(event, pointer)
                self._offset += len(raw)
            return handle.tell() == info.st_size

    def _ready(self):
        if not self.refresh_index():
            raise StructuredIndexPending("History indexing is pending; continue bounded refresh_index.")

    def _append(self, kind, **values):
        self._ready()
        event = {"structured_schema": 1, "scope": self.scope.key, "sequence": self._sequence + 1,
            "kind": kind, **values}
        if len(_canonical_bytes(event)) + 256 > MAX_EVENT_BYTES:
            raise StructuredHistoryError("Structured event exceeds its bound.")
        self.journal._append(event)
        self._ready()

    def _payload(self, digest):
        try:
            state = _state_from(self.journal.load_verified_snapshot(digest, max_bytes=MAX_SNAPSHOT_BYTES))
            self._state(state)
            return state
        except (OSError, ValueError, RecursionError) as exc:
            raise StructuredHistoryError("History payload is missing, corrupt or unsupported.") from exc

    def _snapshot(self, state):
        self._state(state)
        return self.journal.store_verified_snapshot(_state_json(state), max_bytes=MAX_SNAPSHOT_BYTES)

    def _checked_version(self, version_id):
        try:
            version, pointer = self._versions[version_id]
        except KeyError as exc:
            raise StructuredHistoryError("Selected version was not recorded or is unavailable.") from exc
        event = self._read_event(pointer)
        if version not in tuple(self._version_from(row, event) for row in event.get("versions", ())):
            raise StructuredHistoryError("History index version changed; rebuild it.")
        return version

    def version(self, entity, version_id):
        with self.locked():
            self._entity(entity); self._ready()
            version = self._checked_version(version_id)
            if version.entity != entity:
                raise StructuredHistoryError("Selected version belongs to another exact entity.")
            return version

    def payload(self, version):
        with self.locked():
            if version != self.version(version.entity, version.version_id):
                raise StructuredHistoryError("Selected version reference changed.")
            state = self._payload(version.payload)
            if state.entity != version.entity:
                raise StructuredHistoryError("History payload substituted entity identity.")
            return state

    def current_head(self, entity):
        with self.locked():
            self._entity(entity); self._ready()
            identity = self._heads.get(entity.identity)
            return self._checked_version(identity) if identity else None

    def _checked_receipt(self, transaction_id):
        try:
            receipt, pointer = self._receipts[transaction_id]
        except KeyError as exc:
            raise StructuredHistoryError("Exact prepared transaction is unavailable.") from exc
        if self._receipt_from(self._read_event(pointer)) != receipt:
            raise StructuredHistoryError("Prepared history index changed.")
        return receipt

    def receipt(self, transaction_id):
        with self.locked():
            self._ready()
            receipt = self._checked_receipt(transaction_id)
            for member in receipt.members:
                self._entity(member.entity)
                if not set(member.authority_ids) <= set(self.scope.authority_ids):
                    raise StructuredHistoryError("Prepared authority is no longer approved.")
            return receipt

    def pending(self):
        with self.locked():
            self._ready()
            result = []
            for tx in sorted(self._pending_ids):
                receipt = self._checked_receipt(tx)
                if all(m.entity.domain in dict(self.scope.domain_versions) and
                       set(m.authority_ids) <= set(self.scope.authority_ids) for m in receipt.members):
                    result.append(receipt)
            return tuple(result)

    def query(self, entity, *, operation_id="", cursor=None, limit=50):
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
            raise StructuredHistoryError("Choose a bounded history page.")
        if operation_id != "":
            _text(operation_id, "operation selection")
        with self.locked():
            self._entity(entity); self._ready()
            if cursor is not None and (not isinstance(cursor, DomainCursor) or
                    (cursor.scope_key, cursor.entity_identity, cursor.operation_id) !=
                    (self.scope.key, entity.identity, operation_id) or
                    type(cursor.before_sequence) is not int or type(cursor.upper_sequence) is not int or
                    not 0 < cursor.before_sequence <= cursor.upper_sequence <= self._sequence):
                raise StructuredHistoryError("History cursor scope changed.")
            upper = cursor.upper_sequence if cursor else self._sequence
            before = cursor.before_sequence if cursor else upper + 1
            values = []
            rows = (self._by_operation.get((entity.identity, operation_id), ()) if operation_id
                    else self._by_entity.get(entity.identity, ()))
            position = bisect_left(rows, (before, "")) - 1
            while position >= 0:
                sequence, identity = rows[position]
                position -= 1
                version = self._checked_version(identity)
                if (version.sequence != sequence or version.sequence >= before or version.sequence > upper or
                        version.entity != entity or operation_id and version.operation_id != operation_id):
                    raise StructuredHistoryError("History query index changed; rebuild it.")
                values.append(version)
                if len(values) > limit:
                    break
            more = len(values) > limit
            values = tuple(values[:limit])
            following = DomainCursor(self.scope.key, entity.identity, operation_id, upper, values[-1].sequence) if more else None
            return DomainHistoryPage(values, following)

    @staticmethod
    def _version_row(member, receipt):
        return {"entity": _entity_json(member.entity), "version_id": member.version_id,
            "operation_id": receipt.operation_id, "transaction_id": receipt.transaction_id,
            "parent_id": member.parent_id, "restored_from": member.restored_from,
            "payload": member.after, "origin": "restore" if member.restored_from else "save"}

    def observe_current(self, state, *, expected_head_id, operation_id, read_current, reason=""):
        """Record explicitly reviewed current evidence; never change canonical data.

        The adapter holds its canonical lock and supplies exact read-back. A stable
        operation ID recognizes a verified append whose result was not delivered.
        Ordinary prepare/no-op semantics and automatic gap refusal stay unchanged.
        """
        if not isinstance(state, DomainState) or not callable(read_current):
            raise StructuredHistoryError("Observation needs exact state and canonical read-back.")
        if type(expected_head_id) is not str:
            raise StructuredHistoryError("Observation needs the exact reviewed history head.")
        if expected_head_id: _text(expected_head_id, "reviewed history head")
        _text(operation_id, "observation operation ID")
        if type(reason) is not str or len(reason.encode("utf-8")) > 4096:
            raise StructuredHistoryError("Observation reason exceeds its bound.")
        with self.locked():
            self._ready(); self._writable(state.entity); self._state(state)
            authorities = {item.authority_id for item in state.evidence}
            for receipt in self.pending():
                if authorities & {value for member in receipt.members for value in member.authority_ids}:
                    raise StructuredHistoryError("Resolve pending canonical history before recording an observation.")

            def verify_current():
                current = read_current()
                if not isinstance(current, DomainState) or current != state:
                    raise StructuredHistoryError("Current canonical state changed after observation review.")
                self._state(current)

            verify_current()
            head = self.current_head(state.entity)
            page = self.query(state.entity, operation_id=operation_id, limit=2)
            if page.versions:
                if len(page.versions) != 1 or page.next_cursor is not None:
                    raise StructuredHistoryError("Observation operation identifies more than one version.")
                recorded = page.versions[0]
                event = self._read_event(self._versions[recorded.version_id][1])
                if (recorded != head or recorded.origin not in {"baseline", "observed"}
                        or recorded.parent_id != expected_head_id or recorded.transaction_id or recorded.restored_from
                        or event.get("observation_schema") != 1
                        or event.get("observation_expected_head") != expected_head_id
                        or event.get("admission_fingerprint") != self.scope.configuration_fingerprint
                        or event.get("reason") != reason or self.payload(recorded) != state):
                    raise StructuredHistoryError("Observation operation or history changed; review the current state again.")
                return DomainObservationResult(recorded, "already_recorded")
            if (head.version_id if head else "") != expected_head_id:
                raise StructuredHistoryError("History head changed after observation review.")
            if head is not None and _meaning(self.payload(head)) == _meaning(state):
                return DomainObservationResult(head, "already_current")
            payload = self._snapshot(state)
            verify_current()  # Snapshot publication must not hide canonical drift.
            origin = "baseline" if head is None else "observed"
            row = {"entity": _entity_json(state.entity), "version_id": _id(), "operation_id": operation_id,
                "transaction_id": "", "parent_id": expected_head_id, "restored_from": "", "payload": payload,
                "origin": origin}
            self._append(origin, versions=[row], observation_schema=1, observation_expected_head=expected_head_id,
                admission_fingerprint=self.scope.configuration_fingerprint, reason=reason)
            return DomainObservationResult(self.version(state.entity, row["version_id"]),
                "recorded_baseline" if origin == "baseline" else "recorded_observation")

    def prepare(self, changes, *, operation_kind, operation_id=None, reason="", restored_from=None, observed_gaps=False):
        if type(changes) is not tuple or not 0 < len(changes) <= MAX_CHANGES or any(not isinstance(c, DomainChange) for c in changes):
            raise StructuredHistoryError("Freeze one bounded exact change selection.")
        if len({c.before.entity for c in changes}) != len(changes):
            raise StructuredHistoryError("Select each entity once.")
        _text(operation_kind, "operation kind")
        operation_id = operation_id or "op_" + _id()
        _text(operation_id, "operation ID")
        if type(reason) is not str or len(reason.encode("utf-8")) > 4096:
            raise StructuredHistoryError("Operation reason exceeds its bound.")
        restored_from = dict(restored_from or {})
        if set(restored_from) - {c.before.entity for c in changes}:
            raise StructuredHistoryError("Restore selection broadened beyond changed entities.")
        with self.locked():
            self._ready()
            authorities = set()
            evidence = {}
            for change in changes:
                self._writable(change.before.entity)
                for side, state in (("before", change.before), ("after", change.after)):
                    self._state(state)
                    for item in state.evidence:
                        key = (side, item.authority_id)
                        if key in evidence and evidence[key] != item.raw:
                            raise StructuredHistoryError("Linked members disagree about exact canonical evidence.")
                        evidence[key] = item.raw
                        authorities.add(item.authority_id)
            if sum(len(raw or b"") for raw in evidence.values()) > MAX_TRANSACTION_BYTES:
                raise StructuredHistoryError("Linked canonical evidence exceeds its transaction bound.")
            if len(self._pending_ids) >= MAX_CHANGES:
                raise StructuredHistoryError("Resolve pending transactions before starting more work.")
            for tx in self._pending_ids:
                pending = self._checked_receipt(tx)
                touched = {a for m in pending.members for a in m.authority_ids}
                if authorities & touched:
                    raise StructuredHistoryError("Resolve exact pending history before changing these authorities.")
            # Validate every member before any baseline/intent publication.
            for change in changes:
                head = self.current_head(change.before.entity)
                if head and _meaning(self.payload(head)) != _meaning(change.before) and not observed_gaps:
                    raise StructuredHistoryError("Unrecorded current-state gap requires explicit observed_gaps admission.")
                if change.before.entity in restored_from:
                    self.payload(self.version(change.before.entity, restored_from[change.before.entity]))
            if all(c.before == c.after for c in changes):
                return None
            members = []
            for change in changes:
                entity = change.before.entity
                head = self.current_head(entity)
                before_digest, after_digest = self._snapshot(change.before), self._snapshot(change.after)
                if head is None or _meaning(self.payload(head)) != _meaning(change.before):
                    origin = "baseline" if head is None else "observed"
                    row = {"entity": _entity_json(entity), "version_id": _id(), "operation_id": "op_" + _id(),
                        "transaction_id": "", "parent_id": head.version_id if head else "", "restored_from": "",
                        "payload": before_digest, "origin": origin}
                    self._append(origin, versions=[row])
                    head = self.current_head(entity)
                members.append(PreparedMember(entity, before_digest, after_digest, head.version_id, _id(),
                    tuple(e.authority_id for e in change.before.evidence), restored_from.get(entity, "")))
            receipt = PreparedDomainReceipt(self.scope.key, self.scope.configuration_fingerprint, _id(), operation_id, operation_kind, reason, tuple(members))
            self._append("prepared", admission_fingerprint=receipt.admission_fingerprint, transaction_id=receipt.transaction_id, operation_id=operation_id,
                operation_kind=operation_kind, reason=reason,
                members=[{**m.__dict__, "entity": _entity_json(m.entity)} for m in members])
            return receipt

    def _verified_current(self, receipt, read_current):
        if not callable(read_current):
            raise StructuredHistoryError("Canonical adapter read-back is required.")
        current = read_current()
        if (type(current) is not tuple or any(not isinstance(s, DomainState) for s in current) or
                tuple(s.entity for s in current) != tuple(m.entity for m in receipt.members)):
            raise StructuredHistoryError("Canonical read-back did not cover the exact prepared selection.")
        for state in current:
            self._state(state)
        return current

    def commit(self, receipt, *, read_current: Callable):
        with self.locked():
            if not isinstance(receipt, PreparedDomainReceipt) or receipt.scope_key != self.scope.key or receipt.admission_fingerprint != self.scope.configuration_fingerprint or self.receipt(receipt.transaction_id) != receipt:
                raise StructuredHistoryError("Prepared receipt or admission changed.")
            if receipt.transaction_id in self._terminal:
                raise StructuredHistoryError("This prepared transaction is already resolved.")
            for member in receipt.members:
                self._writable(member.entity)
            current = self._verified_current(receipt, read_current)
            for member, state in zip(receipt.members, current):
                if state != self._payload(member.after):
                    raise StructuredHistoryError("Canonical read-back differs; retain exact pending recovery.")
                if self.current_head(member.entity).version_id != member.parent_id:
                    raise StructuredHistoryError("Prepared history parent changed.")
            self._append("committed", transaction_id=receipt.transaction_id,
                versions=[self._version_row(m, receipt) for m in receipt.members])
            return tuple(self.version(m.entity, m.version_id) for m in receipt.members)

    def reconcile(self, transaction_id, *, read_current: Callable):
        """Classify exact canonical state; never perform canonical repair/rollback."""
        with self.locked():
            receipt = self.receipt(transaction_id)
            if transaction_id in self._terminal:
                kind, pointer = self._terminal[transaction_id]
                event = self._read_event(pointer)
                if event["kind"] != kind or event.get("transaction_id") != transaction_id:
                    raise StructuredHistoryError("Recovery index changed.")
                return "completed" if kind == "committed" else "unchanged"
            if receipt.admission_fingerprint != self.scope.configuration_fingerprint:
                raise StructuredHistoryError("Prepared recovery admission changed.")
            for member in receipt.members:
                self._writable(member.entity)
            current = self._verified_current(receipt, read_current)
            if current == tuple(self._payload(m.after) for m in receipt.members):
                self.commit(receipt, read_current=lambda: current)
                return "completed"
            if current == tuple(self._payload(m.before) for m in receipt.members):
                self._append("unchanged", transaction_id=transaction_id)
                return "unchanged"
            return "held"
