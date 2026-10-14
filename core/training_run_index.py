"""Durable pointers to explicit local-training reviews.

The index never discovers, opens, starts, resumes, or deletes a run.  It stores
only caller-supplied pointers so existing execution services can later validate
the referenced review and journal.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from .external_write_transaction import atomic_verified_bytes
from .training_source_io import _assert_unredirected, _identity


SCHEMA_VERSION = 1
MAX_RECORDS = 10_000
MAX_PAGE_SIZE = 100
MAX_INDEX_BYTES = 16 * 1024 * 1024
MAX_RECOVERY_BYTES = 16 * 1024 * 1024
MAX_RECOVERY_DIRECTORIES = 100
ABSENT_REVISION = hashlib.sha256(b"<absent>").hexdigest()
SHA = re.compile(r"[0-9a-f]{64}\Z")
RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
KINDS = {"execution", "readiness", "anima", "validation"}
PHASES = {"prepared", "ready", "starting", "running", "held", "stopping",
          "recovery_required", "stopped", "cancelled", "failed", "interrupted",
          "verification_required", "registration_required", "completed"}
_RECORD_FIELDS = {"run_id", "kind", "review_path", "review_sha256",
    "plan_fingerprint", "destination", "registry_path", "registry_revision",
    "phase", "latest_journal_tip", "sequence"}
_INDEX_FIELDS = {"schema_version", "kind", "next_sequence", "records"}
_ANNOTATION_FIELD = "experiment_annotation"


class TrainingRunIndexError(RuntimeError):
    pass


class TrainingRunIndexRecoveryRequired(TrainingRunIndexError):
    pass


def _digest(raw: bytes | None) -> str:
    return hashlib.sha256(raw if raw is not None else b"<absent>").hexdigest()


def _path(value: str, label: str, *, optional=False) -> Path | None:
    if optional and value == "":
        return None
    if (type(value) is not str or not value or len(value) > 4096 or "\0" in value
            or not Path(value).is_absolute()):
        raise TrainingRunIndexError(f"The {label} pointer must be an absolute path.")
    return Path(value).absolute()


def _sha(value: str, label: str, *, optional=False) -> None:
    if optional and value == "":
        return
    if type(value) is not str or SHA.fullmatch(value) is None:
        raise TrainingRunIndexError(f"The {label} identity is invalid.")


def _canonical(value, *, limit=MAX_INDEX_BYTES) -> bytes:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
            allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise TrainingRunIndexError("The training run index is not serializable.") from exc
    if len(raw) > limit:
        raise TrainingRunIndexError("The training run index exceeds its size bound.")
    return raw


def _decode(raw: bytes, *, limit=MAX_INDEX_BYTES):
    if len(raw) > limit:
        raise TrainingRunIndexError("The training run index exceeds its size bound.")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise TrainingRunIndexError("The training run index repeats a field.")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)))
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise TrainingRunIndexError("The training run index is invalid JSON.") from exc
    if _canonical(value, limit=limit) != raw:
        raise TrainingRunIndexError("The training run index is not canonical.")
    return value


def _read_optional(path: Path, *, limit=MAX_INDEX_BYTES) -> bytes | None:
    _assert_unredirected(path)
    try:
        before = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise TrainingRunIndexError("A training run index record is not a bounded regular file.")
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0)) | int(getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(before):
            raise TrainingRunIndexError("A training run index record changed before reading.")
        raw = os.read(descriptor, limit + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    _assert_unredirected(path)
    if (len(raw) != before.st_size or _identity(opened) != _identity(after)
            or _identity(path.stat(follow_symlinks=False)) != _identity(before)):
        raise TrainingRunIndexError("A training run index record changed while reading.")
    return raw


def _write_new(path: Path, raw: bytes, label: str) -> None:
    if len(raw) > MAX_RECOVERY_BYTES:
        raise TrainingRunIndexError(f"{label} exceeds its size bound.")
    _assert_unredirected(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(raw); handle.flush(); os.fsync(handle.fileno())
    if _read_optional(path, limit=MAX_RECOVERY_BYTES) != raw:
        raise TrainingRunIndexError(f"{label} failed exact read-back verification.")


def _unknown(value: dict, known: set[str]) -> tuple[tuple[str, bytes], ...]:
    rows = []
    for key in sorted(set(value) - known):
        if type(key) is not str or not key or len(key) > 100:
            raise TrainingRunIndexError("A forward training run field name is invalid.")
        rows.append((key, _canonical(value[key])))
    return tuple(rows)


def _with_unknown(value: dict, rows: tuple[tuple[str, bytes], ...]) -> dict:
    result = dict(value)
    for key, raw in rows:
        if key in result:
            raise TrainingRunIndexError("A forward training run field collides with the current schema.")
        result[key] = _decode(raw)
    return result


@dataclass(frozen=True)
class TrainingRunRecord:
    run_id: str
    kind: str
    review_path: str
    review_sha256: str
    plan_fingerprint: str
    destination: str
    registry_path: str = ""
    registry_revision: str = ""
    phase: str = "prepared"
    latest_journal_tip: str = ""
    sequence: int = 0
    annotation: "TrainingRunAnnotation" = field(default_factory=lambda: TrainingRunAnnotation())
    _forward: tuple[tuple[str, bytes], ...] = field(default=(), repr=False)

    def validate(self, *, stored=False) -> None:
        if type(self.run_id) is not str or RUN_ID.fullmatch(self.run_id) is None:
            raise TrainingRunIndexError("The training run identity is invalid.")
        if type(self.kind) is not str or self.kind not in KINDS:
            raise TrainingRunIndexError("The training run pointer kind is invalid.")
        _path(self.review_path, "training review")
        _path(self.destination, "training destination")
        _sha(self.review_sha256, "training review")
        _sha(self.plan_fingerprint, "training plan")
        _sha(self.latest_journal_tip, "latest training journal", optional=True)
        if type(self.phase) is not str or self.phase not in PHASES:
            raise TrainingRunIndexError("The training run phase is unsupported.")
        if type(self.sequence) is not int or self.sequence < (1 if stored else 0):
            raise TrainingRunIndexError("The training run sequence is invalid.")
        if not isinstance(self.annotation, TrainingRunAnnotation):
            raise TrainingRunIndexError("The training run annotation is invalid.")
        self.annotation.validate()
        if self.kind == "execution":
            _path(self.registry_path, "model registry")
            _sha(self.registry_revision, "model registry")
        elif self.registry_path != "" or self.registry_revision != "":
            raise TrainingRunIndexError("A non-execution pointer cannot claim model registry authority.")
        seen = set()
        for key, raw in self._forward:
            if (type(key) is not str or not key or len(key) > 100 or key in _RECORD_FIELDS or key == _ANNOTATION_FIELD
                    or key in seen or type(raw) is not bytes):
                raise TrainingRunIndexError("A forward training run field is invalid.")
            _decode(raw)
            seen.add(key)

    def payload(self) -> dict:
        self.validate(stored=self.sequence > 0)
        value = {name: getattr(self, name) for name in _RECORD_FIELDS}
        if self.annotation != TrainingRunAnnotation():
            value[_ANNOTATION_FIELD] = self.annotation.payload()
        return _with_unknown(value, self._forward)

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or not _RECORD_FIELDS.issubset(value):
            raise TrainingRunIndexError("A training run pointer is incomplete.")
        record = cls(**{name: value[name] for name in _RECORD_FIELDS},
            annotation=TrainingRunAnnotation.from_dict(value.get(_ANNOTATION_FIELD, {})),
            _forward=_unknown(value, _RECORD_FIELDS | {_ANNOTATION_FIELD}))
        record.validate(stored=True)
        return record


@dataclass(frozen=True)
class TrainingRunAnnotation:
    name: str = ""
    notes: str = ""
    favorite: bool = False
    _forward: tuple[tuple[str, bytes], ...] = field(default=(), repr=False)

    def validate(self):
        if (type(self.name) is not str or len(self.name) > 100 or "\0" in self.name
                or type(self.notes) is not str or len(self.notes) > 4000 or "\0" in self.notes
                or type(self.favorite) is not bool):
            raise TrainingRunIndexError("The training run annotation is invalid.")
        seen = set()
        for key, raw in self._forward:
            if (type(key) is not str or not key or len(key) > 100
                    or key in {"name", "notes", "favorite"} or key in seen
                    or type(raw) is not bytes):
                raise TrainingRunIndexError("A forward training run annotation field is invalid.")
            _decode(raw)
            seen.add(key)

    def payload(self):
        self.validate()
        return _with_unknown({"name": self.name, "notes": self.notes,
            "favorite": self.favorite}, self._forward)

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict:
            raise TrainingRunIndexError("The training run annotation is invalid.")
        result = cls(value.get("name", ""), value.get("notes", ""),
            value.get("favorite", False),
            _unknown(value, {"name", "notes", "favorite"}))
        result.validate()
        return result


@dataclass(frozen=True)
class TrainingRunPage:
    records: tuple[TrainingRunRecord, ...]
    total: int
    offset: int
    next_offset: int | None
    revision: str


@dataclass(frozen=True)
class _State:
    records: tuple[TrainingRunRecord, ...]
    next_sequence: int
    forward: tuple[tuple[str, bytes], ...] = ()

    def payload(self):
        value = {"schema_version": SCHEMA_VERSION, "kind": "training_run_index",
            "next_sequence": self.next_sequence,
            "records": [record.payload() for record in sorted(self.records, key=lambda row: row.sequence)]}
        return _with_unknown(value, self.forward)


def _state(raw: bytes | None) -> _State:
    if raw is None:
        return _State((), 1)
    value = _decode(raw)
    if (type(value) is not dict or not _INDEX_FIELDS.issubset(value)
            or type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION
            or value["kind"] != "training_run_index" or type(value["next_sequence"]) is not int
            or type(value["records"]) is not list or len(value["records"]) > MAX_RECORDS):
        raise TrainingRunIndexError("The training run index schema is unsupported.")
    records = tuple(TrainingRunRecord.from_dict(row) for row in value["records"])
    ids = {row.run_id for row in records}
    sequences = {row.sequence for row in records}
    if (len(ids) != len(records) or len(sequences) != len(records)
            or value["next_sequence"] < 1
            or (records and value["next_sequence"] <= max(sequences))):
        raise TrainingRunIndexError("The training run index repeats or misorders a pointer.")
    result = _State(records, value["next_sequence"], _unknown(value, _INDEX_FIELDS))
    if _canonical(result.payload()) != raw:
        raise TrainingRunIndexError("The training run index has an invalid canonical structure.")
    return result


class TrainingRunIndex:
    """Bounded app-state index; construction performs no filesystem I/O."""

    def __init__(self, path):
        self.path = Path(path).absolute()
        self.pending = self.path.with_name(self.path.name + ".pending.json")
        self.recovery_root = self.path.parent / (self.path.stem + "-recovery")

    @contextmanager
    def _locked(self):
        _assert_unredirected(self.path.parent)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_name(self.path.name + ".lock")
        _assert_unredirected(lock)
        flags = os.O_RDWR | os.O_CREAT | int(getattr(os, "O_BINARY", 0)) | int(getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(lock, flags, 0o600)
        with os.fdopen(descriptor, "r+b") as handle:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0"); handle.flush(); os.fsync(handle.fileno())
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise TrainingRunIndexError("Another training run index update owns this location.") from exc
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read(self) -> tuple[bytes | None, _State]:
        if _read_optional(self.pending, limit=MAX_RECOVERY_BYTES) is not None:
            raise TrainingRunIndexRecoveryRequired(
                "A training run index update needs recovery before the index can be used."
            )
        raw = _read_optional(self.path)
        return raw, _state(raw)

    def snapshot(self) -> tuple[tuple[TrainingRunRecord, ...], str]:
        raw, current = self._read()
        return tuple(sorted(current.records, key=lambda row: row.sequence, reverse=True)), _digest(raw)

    def page(self, *, offset=0, limit=MAX_PAGE_SIZE) -> TrainingRunPage:
        if (type(offset) is not int or offset < 0 or type(limit) is not int
                or not 1 <= limit <= MAX_PAGE_SIZE):
            raise TrainingRunIndexError("Choose a valid bounded training run page.")
        records, revision = self.snapshot()
        selected = records[offset:offset + limit]
        next_offset = offset + len(selected) if offset + len(selected) < len(records) else None
        return TrainingRunPage(selected, len(records), offset, next_offset, revision)

    def register(self, record: TrainingRunRecord, expected_revision: str) -> str:
        if not isinstance(record, TrainingRunRecord):
            raise TrainingRunIndexError("Register a typed training run pointer.")
        record.validate()
        if record.sequence != 0:
            raise TrainingRunIndexError("A new training run pointer cannot claim an index sequence.")
        def change(current):
            if len(current.records) >= MAX_RECORDS:
                raise TrainingRunIndexError("The training run index reached its record bound.")
            if any(item.run_id == record.run_id for item in current.records):
                raise TrainingRunIndexError("This training run identity is already indexed.")
            return _State((*current.records, replace(record, sequence=current.next_sequence)),
                current.next_sequence + 1, current.forward)
        return self._mutate(expected_revision, change)

    def update_phase(self, run_id: str, phase: str, expected_revision: str,
                     *, latest_journal_tip=None) -> str:
        if type(run_id) is not str or RUN_ID.fullmatch(run_id) is None:
            raise TrainingRunIndexError("The training run identity is invalid.")
        if type(phase) is not str or phase not in PHASES:
            raise TrainingRunIndexError("The training run phase is unsupported.")
        if latest_journal_tip is not None:
            _sha(latest_journal_tip, "latest training journal", optional=True)
        def change(current):
            found = False
            records = []
            for item in current.records:
                if item.run_id == run_id:
                    found = True
                    item = replace(item, phase=phase,
                        latest_journal_tip=(item.latest_journal_tip
                            if latest_journal_tip is None else latest_journal_tip))
                records.append(item)
            if not found:
                raise TrainingRunIndexError("The selected training run is not indexed.")
            return _State(tuple(records), current.next_sequence, current.forward)
        return self._mutate(expected_revision, change)

    def update_annotation(self, run_id: str, annotation: TrainingRunAnnotation,
                          expected_revision: str) -> str:
        if type(run_id) is not str or RUN_ID.fullmatch(run_id) is None:
            raise TrainingRunIndexError("The training run identity is invalid.")
        if not isinstance(annotation, TrainingRunAnnotation):
            raise TrainingRunIndexError("Choose a typed training run annotation.")
        annotation.validate()
        def change(current):
            if not any(item.run_id == run_id for item in current.records):
                raise TrainingRunIndexError("The selected training run is not indexed.")
            prior = next(item for item in current.records if item.run_id == run_id)
            if (annotation._forward and prior.annotation._forward
                    and annotation._forward != prior.annotation._forward):
                raise TrainingRunIndexError("The training run annotation's forward fields changed.")
            preserved_forward = prior.annotation._forward or annotation._forward
            return _State(tuple(replace(item, annotation=replace(annotation,
                _forward=preserved_forward)) if item.run_id == run_id
                else item for item in current.records), current.next_sequence, current.forward)
        return self._mutate(expected_revision, change)

    def forget(self, run_id: str, expected_revision: str) -> str:
        if type(run_id) is not str or RUN_ID.fullmatch(run_id) is None:
            raise TrainingRunIndexError("The training run identity is invalid.")
        def change(current):
            records = tuple(item for item in current.records if item.run_id != run_id)
            if len(records) == len(current.records):
                raise TrainingRunIndexError("The selected training run is not indexed.")
            return _State(records, current.next_sequence, current.forward)
        return self._mutate(expected_revision, change)

    def _mutate(self, expected_revision: str, change) -> str:
        _sha(expected_revision, "previous training run index")
        with self._locked():
            before, current = self._read()
            if _digest(before) != expected_revision:
                raise TrainingRunIndexError(
                    "The training run index changed after review. Reload it and try again."
                )
            updated = change(current)
            after = _canonical(updated.payload())
            if before == after:
                return _digest(after)
            operation, recovery = self._new_recovery()
            receipt = {"schema_version": SCHEMA_VERSION, "kind": "training_run_index_recovery",
                "operation_id": operation, "target": str(self.path), "before_absent": before is None,
                "before_sha256": _digest(before), "after_sha256": _digest(after)}
            before_raw, receipt_raw = before or b"", _canonical(receipt, limit=MAX_RECOVERY_BYTES)
            _write_new(recovery / "before.bin", before_raw, "Training run index recovery backup")
            _write_new(recovery / "after.json", after, "Training run index recovery proposal")
            _write_new(recovery / "receipt.json", receipt_raw, "Training run index recovery receipt")
            atomic_verified_bytes(self.pending, receipt_raw, label="Training run index pending recovery")
            _assert_unredirected(self.path)
            atomic_verified_bytes(self.path, after, label="Training run index")
            if _read_optional(self.path) != after:
                raise TrainingRunIndexError("The training run index failed exact read-back verification.")
            self._finish_pending(receipt_raw)
            self._discard_recovery(recovery)
            return _digest(after)

    def _new_recovery(self):
        _assert_unredirected(self.recovery_root)
        self.recovery_root.mkdir(parents=True, exist_ok=True)
        count = 0
        try:
            with os.scandir(self.recovery_root) as entries:
                for entry in entries:
                    if count >= MAX_RECOVERY_DIRECTORIES or RUN_ID.fullmatch(entry.name) is None:
                        raise TrainingRunIndexRecoveryRequired(
                            "Training run index recovery contains unexpected or excessive retained work."
                        )
                    if not entry.is_dir(follow_symlinks=False):
                        raise TrainingRunIndexRecoveryRequired(
                            "Training run index recovery contains an unsafe entry."
                        )
                    count += 1
            if count >= MAX_RECOVERY_DIRECTORIES:
                raise TrainingRunIndexRecoveryRequired(
                    "Training run index recovery contains excessive retained work."
                )
        except TrainingRunIndexRecoveryRequired:
            raise
        except OSError as exc:
            raise TrainingRunIndexRecoveryRequired("Training run index recovery is unavailable.") from exc
        operation = uuid4().hex
        recovery = self.recovery_root / operation
        recovery.mkdir(exist_ok=False)
        return operation, recovery

    def _finish_pending(self, expected: bytes) -> None:
        if _read_optional(self.pending, limit=MAX_RECOVERY_BYTES) != expected:
            raise TrainingRunIndexRecoveryRequired("The pending training run index receipt changed.")
        _assert_unredirected(self.pending)
        self.pending.unlink()

    @staticmethod
    def _remove_recovery(recovery: Path) -> None:
        for name in ("before.bin", "after.json", "receipt.json"):
            path = recovery / name
            _assert_unredirected(path)
            path.unlink()
        recovery.rmdir()

    @classmethod
    def _discard_recovery(cls, recovery: Path) -> None:
        try:
            cls._remove_recovery(recovery)
        except (OSError, RuntimeError):
            # The index and pending-marker removal are the commit boundary.
            # A cleanup failure leaves only bounded app-state recovery evidence;
            # it must not make a committed pointer look absent to its caller.
            pass

    def recover(self) -> str:
        with self._locked():
            pending_raw = _read_optional(self.pending, limit=MAX_RECOVERY_BYTES)
            if pending_raw is None:
                return self.snapshot()[1]
            receipt = _decode(pending_raw, limit=MAX_RECOVERY_BYTES)
            fields = {"schema_version", "kind", "operation_id", "target", "before_absent",
                      "before_sha256", "after_sha256"}
            if (type(receipt) is not dict or set(receipt) != fields
                    or type(receipt["schema_version"]) is not int
                    or receipt["schema_version"] != SCHEMA_VERSION
                    or receipt["kind"] != "training_run_index_recovery"
                    or type(receipt["operation_id"]) is not str
                    or RUN_ID.fullmatch(receipt["operation_id"]) is None
                    or receipt["target"] != str(self.path)
                    or type(receipt["before_absent"]) is not bool):
                raise TrainingRunIndexRecoveryRequired("The training run index recovery receipt is invalid.")
            _sha(receipt["before_sha256"], "run index recovery backup")
            _sha(receipt["after_sha256"], "run index recovery proposal")
            recovery = self.recovery_root / receipt["operation_id"]
            saved_receipt = _read_optional(recovery / "receipt.json", limit=MAX_RECOVERY_BYTES)
            before = _read_optional(recovery / "before.bin", limit=MAX_RECOVERY_BYTES)
            after = _read_optional(recovery / "after.json", limit=MAX_RECOVERY_BYTES)
            if (saved_receipt != pending_raw or before is None or after is None
                    or _digest(after) != receipt["after_sha256"]
                    or _digest(None if receipt["before_absent"] else before) != receipt["before_sha256"]
                    or (receipt["before_absent"] and before != b"")):
                raise TrainingRunIndexRecoveryRequired("The retained training run index recovery evidence changed.")
            _state(after)
            current = _read_optional(self.path)
            if _digest(current) == receipt["before_sha256"]:
                _assert_unredirected(self.path)
                atomic_verified_bytes(self.path, after, label="Recovered training run index")
                current = _read_optional(self.path)
            if _digest(current) != receipt["after_sha256"]:
                raise TrainingRunIndexRecoveryRequired(
                    "The training run index differs from both retained recovery states."
                )
            self._finish_pending(pending_raw)
            self._discard_recovery(recovery)
            return receipt["after_sha256"]
