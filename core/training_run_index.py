"""Durable pointers to explicit local-training reviews.

The index never discovers, opens, starts, resumes, or deletes a run.  It stores
only caller-supplied pointers so existing execution services can later validate
the referenced review and journal.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, fields, replace
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from .external_write_transaction import atomic_verified_bytes
from .training_source_io import _assert_unredirected, _identity, _redirected


SCHEMA_VERSION = 1
MAX_RECORDS = 10_000
MAX_PAGE_SIZE = 100
MAX_INDEX_BYTES = 16 * 1024 * 1024
MAX_RECOVERY_BYTES = 16 * 1024 * 1024
MAX_RECOVERY_DIRECTORIES = 100
ABSENT_REVISION = hashlib.sha256(b"<absent>").hexdigest()
SHA = re.compile(r"[0-9a-f]{64}\Z")
RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
KINDS = {"execution", "readiness", "anima", "validation", "preflight"}
PHASES = {"prepared", "ready", "starting", "running", "held", "stopping",
          "recovery_required", "stopped", "cancelled", "failed", "interrupted",
          "verification_required", "registration_required", "completed"}
_RECORD_FIELDS = {"run_id", "kind", "review_path", "review_sha256",
    "plan_fingerprint", "destination", "registry_path", "registry_revision",
    "phase", "latest_journal_tip", "sequence"}
_INDEX_FIELDS = {"schema_version", "kind", "next_sequence", "records"}
_ANNOTATION_FIELD = "experiment_annotation"
_VALIDATION_REPORT_FIELD = "validation_report_sha256"
_REGISTERED_RESULT_FIELD = "registered_result"


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
class RegisteredTrainingResult:
    resource_id: str
    receipt_sha256: str
    journal_tip: str

    def validate(self):
        if type(self.resource_id) is not str or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.resource_id):
            raise TrainingRunIndexError("The registered training resource identity is invalid.")
        _sha(self.receipt_sha256, "registered training receipt")
        _sha(self.journal_tip, "registered training journal")


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
    validation_report_sha256: str = ""
    registered_result: RegisteredTrainingResult | None = None

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
        _sha(self.validation_report_sha256, "validation report", optional=True)
        if self.validation_report_sha256 and self.kind != "validation":
            raise TrainingRunIndexError("Only validation pointers can bind a measurement report.")
        if type(self.phase) is not str or self.phase not in PHASES:
            raise TrainingRunIndexError("The training run phase is unsupported.")
        if self.registered_result is not None:
            if (type(self.registered_result) is not RegisteredTrainingResult
                    or self.kind != "execution" or self.phase != "completed"):
                raise TrainingRunIndexError("Only completed execution pointers can bind registered results.")
            self.registered_result.validate()
            if self.registered_result.journal_tip != self.latest_journal_tip:
                raise TrainingRunIndexError(
                    "The registered training result journal changed. Keep a copy of the current Training History "
                    "index. If you have a verified backup for this profile, restore it before choosing Refresh."
                )
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
            if (type(key) is not str or not key or len(key) > 100 or key in _RECORD_FIELDS
                    or key in {_ANNOTATION_FIELD, _VALIDATION_REPORT_FIELD, _REGISTERED_RESULT_FIELD}
                    or key in seen or type(raw) is not bytes):
                raise TrainingRunIndexError("A forward training run field is invalid.")
            _decode(raw)
            seen.add(key)

    def payload(self) -> dict:
        self.validate(stored=self.sequence > 0)
        value = {name: getattr(self, name) for name in _RECORD_FIELDS}
        if self.annotation != TrainingRunAnnotation():
            value[_ANNOTATION_FIELD] = self.annotation.payload()
        if self.validation_report_sha256:
            value[_VALIDATION_REPORT_FIELD] = self.validation_report_sha256
        if self.registered_result is not None:
            from dataclasses import asdict

            value[_REGISTERED_RESULT_FIELD] = asdict(self.registered_result)
        return _with_unknown(value, self._forward)

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or not _RECORD_FIELDS.issubset(value):
            raise TrainingRunIndexError("A training run pointer is incomplete.")
        result = None
        if _REGISTERED_RESULT_FIELD in value:
            saved = value[_REGISTERED_RESULT_FIELD]
            if type(saved) is dict and set(saved) == {"resource_id", "receipt_sha256", "journal_tip"}:
                result = RegisteredTrainingResult(**saved)
            elif type(saved) is list and len(saved) == 2:
                result = RegisteredTrainingResult(saved[1], saved[0], value["latest_journal_tip"])
            else:
                raise TrainingRunIndexError("The saved training result is invalid.")
        record = cls(**{name: value[name] for name in _RECORD_FIELDS},
            annotation=TrainingRunAnnotation.from_dict(value.get(_ANNOTATION_FIELD, {})),
            _forward=_unknown(value, _RECORD_FIELDS | {_ANNOTATION_FIELD, _VALIDATION_REPORT_FIELD, _REGISTERED_RESULT_FIELD}),
            validation_report_sha256=value.get(_VALIDATION_REPORT_FIELD, ""), registered_result=result)
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
    if any(type(row.get(_REGISTERED_RESULT_FIELD)) is list for row in value["records"]):
        for saved, record in zip(value["records"], records):
            if type(saved.get(_REGISTERED_RESULT_FIELD)) is list:
                saved[_REGISTERED_RESULT_FIELD] = record.payload()[_REGISTERED_RESULT_FIELD]
        raw = _canonical(value)
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
                     *, latest_journal_tip=None, validation_report_sha256=None, registered_result=None) -> str:
        if type(run_id) is not str or RUN_ID.fullmatch(run_id) is None:
            raise TrainingRunIndexError("The training run identity is invalid.")
        if type(phase) is not str or phase not in PHASES:
            raise TrainingRunIndexError("The training run phase is unsupported.")
        if latest_journal_tip is not None:
            _sha(latest_journal_tip, "latest training journal", optional=True)
        if validation_report_sha256 is not None:
            _sha(validation_report_sha256, "validation report")
        if registered_result is not None and (type(registered_result) is not RegisteredTrainingResult
                or phase != "completed" or registered_result.journal_tip != latest_journal_tip):
            raise TrainingRunIndexError("A registered result requires the completed execution journal.")
        def change(current):
            found = False
            records = []
            for item in current.records:
                if item.run_id == run_id:
                    found = True
                    if validation_report_sha256 and item.validation_report_sha256 not in {"", validation_report_sha256}:
                        raise TrainingRunIndexError("The saved validation report identity changed.")
                    if item.registered_result is not None and (
                            registered_result not in (None, item.registered_result)
                            or latest_journal_tip not in (None, item.latest_journal_tip)):
                        raise TrainingRunIndexError("The saved registered result identity changed.")
                    item = replace(item, phase=phase,
                        latest_journal_tip=(item.latest_journal_tip
                            if latest_journal_tip is None else latest_journal_tip),
                        validation_report_sha256=validation_report_sha256 or item.validation_report_sha256,
                        registered_result=registered_result or item.registered_result)
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
        return self._recover()

    def _recover(self, *, review_only=False, validate=None):
        with self._locked():
            pending_raw = _read_optional(self.pending, limit=MAX_RECOVERY_BYTES)
            if pending_raw is None:
                if validate is not None:
                    raise TrainingRunIndexRecoveryRequired("No pending Training History write was found.")
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
            current_digest = _digest(current)
            if validate is not None:
                if current_digest not in (receipt["before_sha256"], receipt["after_sha256"]):
                    raise TrainingRunIndexRecoveryRequired("The training run index differs from both retained recovery states.")
                reviewed = validate(pending_raw, _state(None if receipt["before_absent"] else before), _state(after))
                if review_only:
                    return reviewed
            if current_digest == receipt["before_sha256"]:
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


class ScopedTrainingHistory:
    """Read-only history for a host whose run state is confined to one root."""

    def __init__(self, root, *, model_registry_path=None):
        self._root = Path(root).absolute()
        self._index = TrainingRunIndex(self._root / "training-runs.json")
        self._model_registry_path = model_registry_path

    def _check_records(self, records):
        for record in records:
            for value in (record.review_path, record.destination):
                self._check_path(Path(value))
            if record.registry_path and Path(record.registry_path) != self._model_registry_path:
                self._check_path(Path(record.registry_path))

    def _check_path(self, path):
        if (path == self._root or not path.is_relative_to(self._root)
                or any(":" in part or part.endswith((".", " ")) for part in path.parts[1:])):
            raise TrainingRunIndexError("A saved review refers outside this host's Training history.")

    def _check_read_path(self, path):
        self._check_path(path)
        for part in (*reversed(path.parents), path):
            if _redirected(part):
                raise TrainingRunIndexError("The selected training receipt uses a redirected path.")

    def page(self, *, offset=0, limit=MAX_PAGE_SIZE):
        page = self._index.page(offset=offset, limit=limit)
        self._check_records(page.records)
        return page

    def snapshot(self):
        records, revision = self._index.snapshot()
        self._check_records(records)
        return records, revision

    def verify_selected(self, record, revision):
        """Verify the indexed receipt bytes without following paths in the receipt."""
        self._selected_receipt(record, revision)
        return record

    def review_preparation(self, record, revision):
        """Return the checked preparation deadline without opening linked files."""
        if record.kind in {"execution", "readiness"}:
            return self._review_sdxl_plan(record, revision).timeout_seconds
        plan, _ = self._preparation(record, revision)
        return plan.timeout_seconds

    def review_anima_settings(self, record, revision):
        """Read saved Anima settings without opening linked training inputs."""
        if record.kind != "anima":
            raise TrainingRunIndexError("Choose a saved Anima run.")
        plan, value = self._preparation(record, revision)
        return self._anima_settings(plan, value)

    @staticmethod
    def _anima_settings(plan, value):
        from .training_anima import AnimaSettings

        saved = value.get("settings")
        if type(saved) is not dict or set(saved) != {field.name for field in fields(AnimaSettings)}:
            raise TrainingRunIndexError("This receipt does not contain a complete set of saved Anima settings.")
        try:
            settings = AnimaSettings(**saved)
            settings.validate()
        except ValueError as exc:
            raise TrainingRunIndexError("The saved Anima settings are invalid.") from exc
        if settings.timeout_seconds != plan.timeout_seconds:
            raise TrainingRunIndexError("The saved Anima settings do not match the run's time limit.")
        return settings

    def review_unlisted_anima(self, receipt):
        """Review one Manager-owned preparation without following its linked inputs."""
        from .training_run_recovery import _plan

        receipt = Path(receipt)
        self._check_read_path(receipt)
        parts = receipt.relative_to(self._root).parts
        if (len(parts) != 3 or not parts[0].startswith("anima-run-")
                or parts[1:] != (".anima.anima-preparation", "anima-review.json")
                or any(part in {".", ".."} for part in parts)):
            raise TrainingRunIndexError("Choose a retained Anima preparation from this Manager's Training folder.")
        run_root = receipt.parent.parent
        destination = run_root / "anima"
        self._check_read_path(destination)
        if destination.exists():
            raise TrainingRunIndexError("This Anima run already has execution files. Use its existing recovery path.")
        raw = _read_optional(receipt, limit=256 * 1024)
        if raw is None:
            raise TrainingRunIndexError("The selected Anima preparation is unavailable.")
        try:
            value = _decode(raw)
            if (type(value) is not dict or type(value.get("schema_version")) is not int
                    or value["schema_version"] != 1 or value.get("kind") != "collectigen_anima_retained_review"):
                raise ValueError("Unexpected receipt format")
            plan = _plan(value["plan"])
            contract = receipt.with_name("anima-contract.json")
            if (plan.destination != destination or Path(value["preparation_root"]) != receipt.parent
                    or Path(value["contract_path"]) != contract):
                raise ValueError("Unexpected preparation layout")
            settings = self._anima_settings(plan, value)
            contract_raw = _read_optional(contract, limit=64 * 1024)
            if contract_raw is None or hashlib.sha256(contract_raw).hexdigest() != plan.preview_fingerprint:
                raise ValueError("The preparation contract changed")
            contract_value = _decode(contract_raw)
            if (type(contract_value) is not dict or contract_value.get("kind") != "collectigen_anima_dit_lora"
                    or contract_value.get("settings") != settings.payload()):
                raise ValueError("The preparation contract does not match the saved settings")
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            raise TrainingRunIndexError("The selected Anima preparation is invalid or changed.") from exc
        record = TrainingRunRecord(plan.run_id, "anima", str(receipt), hashlib.sha256(raw).hexdigest(),
                                   plan.fingerprint, str(destination), phase="prepared")
        record.validate()
        return record, settings

    def _pending_anima_review(self, pending, before, after):
        if (len(after.records) != len(before.records) + 1
                or after.records[:-1] != before.records
                or after.next_sequence != before.next_sequence + 1
                or after.forward != before.forward):
            raise TrainingRunIndexRecoveryRequired("The pending write is not one Manager Anima addition.")
        added = after.records[-1]
        if added.kind != "anima" or added.sequence != before.next_sequence:
            raise TrainingRunIndexRecoveryRequired("The pending write is not one Manager Anima addition.")
        record, settings = self.review_unlisted_anima(added.review_path)
        if replace(record, sequence=before.next_sequence) != added:
            raise TrainingRunIndexRecoveryRequired("The pending Anima preparation changed.")
        return _digest(pending), record, settings

    def review_pending_anima(self):
        return self._index._recover(review_only=True, validate=self._pending_anima_review)

    def recover_pending_anima(self, reviewed, *, confirmed, session_ready):
        if confirmed is not True or not session_ready():
            raise TrainingRunIndexError("An active Manager session and confirmation are required.")

        def validate(pending, before, after):
            if self._pending_anima_review(pending, before, after) != reviewed or not session_ready():
                raise TrainingRunIndexRecoveryRequired("The pending Anima write or Manager session changed. Review it again.")

        return self._index._recover(validate=validate)

    def save_unlisted_anima(self, reviewed, *, confirmed, session_ready):
        """Add the exact reviewed preparation to History after fresh confirmation."""
        if confirmed is not True or not session_ready():
            raise TrainingRunIndexError("An active Manager session and confirmation are required.")
        record, _ = reviewed
        if self.review_unlisted_anima(record.review_path) != reviewed:
            raise TrainingRunIndexError("The Anima preparation changed. Review it again before saving.")
        rows, revision = self.snapshot()
        existing = next((row for row in rows if row.run_id == record.run_id), None)
        if not session_ready():
            raise TrainingRunIndexError("The Manager session ended before saving the preparation.")
        if existing is not None:
            if replace(existing, sequence=0) != record:
                raise TrainingRunIndexError("Training History already has a different record for this run.")
            return existing
        self._index.register(record, revision)
        saved = next((row for row in self.snapshot()[0] if row.run_id == record.run_id), None)
        if saved is None or replace(saved, sequence=0) != record:
            raise TrainingRunIndexError("The saved Anima preparation could not be read back. Refresh Training History.")
        return saved

    def _review_sdxl_plan(self, record, revision):
        from .training_run_recovery import restore_indexed_review

        records, current = self.snapshot()
        if current != revision or record not in records:
            raise TrainingRunIndexError("Training history changed. Refresh and select the review again.")
        self._check_read_path(Path(record.review_path))
        return restore_indexed_review(self, record.run_id, revision).plan

    def review_execution_outcome(self, record, revision):
        """Read a stopped process outcome without inspecting outputs or live inputs."""
        from .training_process import TERMINAL, TrainingProcessService, TrainingRunJournal, _bytes

        if record.kind != "execution" or not record.latest_journal_tip:
            raise TrainingRunIndexError("Choose an execution with a saved process journal.")
        plan = self._review_sdxl_plan(record, revision)
        self._check_read_path(plan.destination / "events")
        rows, tip = TrainingRunJournal(plan.destination).read()
        reserved = TrainingProcessService._receipt(plan)
        expected = {"plan": reserved, "fingerprint": hashlib.sha256(_bytes(reserved)).hexdigest()}
        if (not rows or tip != record.latest_journal_tip or _bytes(rows[0]["details"]) != _bytes(expected)
                or rows[-1]["state"] not in TERMINAL or rows[-1]["details"].get("tree_stopped") is not True):
            raise TrainingRunIndexError("The saved execution journal changed or does not confirm a stopped process.")
        if self.snapshot()[1] != revision:
            raise TrainingRunIndexError("Training history changed while opening the review.")
        terminal = rows[-1]
        exit_code = terminal["details"].get("exit_code")
        if exit_code is not None and (type(exit_code) is not int or not -(2**31) <= exit_code < 2**32):
            raise TrainingRunIndexError("The saved execution journal has an invalid exit code.")
        registered = record.registered_result if record.phase == "completed" else None
        if registered is not None and (registered.journal_tip != tip
                or terminal["state"] != "exited_unverified" or exit_code != 0):
            raise TrainingRunIndexError("The saved registered result does not match the stopped execution.")
        return terminal["state"], exit_code, registered

    def review_validation(self, record, revision):
        """Read a completed saved validation measurement from the host's run store."""
        from .training_validation import RESULT_NAME, read_retained_report, restore_prepared

        if record.kind != "validation" or record.phase != "completed" or not record.latest_journal_tip:
            raise TrainingRunIndexError("Choose a completed held-out validation with a saved journal.")
        plan, value = self._preparation(record, revision)
        request = value.get("request_path")
        expected = Path(plan.root) / (plan.name + ".validation-request.json")
        if type(request) is not str or Path(request) != expected:
            raise TrainingRunIndexError("The saved validation request does not match its run.")
        self._check_read_path(expected)
        self._check_read_path(plan.destination / "events")
        self._check_read_path(plan.destination / "work" / RESULT_NAME)
        prepared = restore_prepared(record.review_path, record.review_sha256)
        report = read_retained_report(prepared, record.latest_journal_tip)
        if record.validation_report_sha256:
            from .training_process import _bytes
            if hashlib.sha256(_bytes(report)).hexdigest() != record.validation_report_sha256:
                raise TrainingRunIndexError("The saved validation measurement changed after indexing.")
        if self.snapshot()[1] != revision:
            raise TrainingRunIndexError("Training history changed while opening the review.")
        return report

    def _preparation(self, record, revision):
        from .training_run_recovery import _plan

        raw = self._selected_receipt(record, revision)
        kinds = {
            "execution": "sdxl_training_execution_review",
            "readiness": "sdxl_training_readiness_review",
            "anima": "collectigen_anima_retained_review",
            "validation": "sdxl_heldout_validation_review",
        }
        try:
            value = _decode(raw)
            if (type(value) is not dict or type(value.get("schema_version")) is not int
                    or value["schema_version"] != 1 or value.get("kind") != kinds[record.kind]):
                raise ValueError("Unexpected receipt format")
            plan = _plan(value["plan"])
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            raise TrainingRunIndexError("The selected training preparation receipt is invalid.") from exc
        if (plan.run_id != record.run_id or plan.fingerprint != record.plan_fingerprint
                or str(plan.destination) != record.destination
                or (record.kind in {"execution", "readiness"}
                    and value.get("fingerprint") != plan.fingerprint)):
            raise TrainingRunIndexError("The training preparation differs from the saved run identity.")
        return plan, value

    def _selected_receipt(self, record, revision):
        records, current = self.snapshot()
        if current != revision or record not in records:
            raise TrainingRunIndexError("Training history changed. Refresh and select the review again.")
        receipt = Path(record.review_path)
        self._check_read_path(receipt)
        limit = 256 * 1024
        if record.kind == "readiness":
            from .training_run_recovery import MAX_REVIEW_BYTES

            limit = MAX_REVIEW_BYTES
        elif record.kind == "execution":
            from .training_execution import MAX_REVIEW_BYTES

            limit = MAX_REVIEW_BYTES
        raw = _read_optional(receipt, limit=limit)
        if raw is None or hashlib.sha256(raw).hexdigest() != record.review_sha256:
            raise TrainingRunIndexError("The selected training receipt changed or is unavailable.")
        if self.snapshot()[1] != revision:
            raise TrainingRunIndexError("Training history changed while opening the review.")
        return raw


class ScopedPreparationHistory(ScopedTrainingHistory):
    """Manager preparation pointers in their own rollback-compatible index."""

    def __init__(self, root):
        super().__init__(root)
        self._index = TrainingRunIndex(self._root / "sdxl-preflight-runs.json")

    def _check_records(self, records):
        super()._check_records(records)
        for record in records:
            destination = Path(record.destination)
            if (record.kind != "preflight" or record.phase != "prepared"
                    or record.registry_path or record.registry_revision
                    or destination.name != "preflight-" + record.run_id
                    or Path(record.review_path) != destination / "preflight-result.json"
                    or not record.latest_journal_tip):
                raise TrainingRunIndexError("The saved SDXL preparation pointer is invalid.")

    def retain(self, directory, *, receipt_sha256, journal_tip):
        from .training_preflight import read_preparation_receipt

        directory = Path(directory)
        receipt = read_preparation_receipt(directory, owned_root=self._root,
            receipt_sha256=receipt_sha256, journal_tip=journal_tip)
        record = TrainingRunRecord(receipt["nonce"], "preflight",
            str(directory / "preflight-result.json"), receipt_sha256,
            receipt["process_fingerprint"], str(directory), latest_journal_tip=journal_tip)
        self._check_records((record,))
        records, revision = self.snapshot()
        previous = next((row for row in records if row.run_id == record.run_id), None)
        if previous is not None:
            if replace(previous, sequence=0) != record:
                raise TrainingRunIndexError("The saved SDXL preparation pointer changed.")
            return revision
        return self._index.register(record, revision)

    def review_preparation(self, record, revision):
        from .training_preflight import read_preparation_receipt

        records, current = self.snapshot()
        if revision != current or record not in records:
            raise TrainingRunIndexError("Training history changed. Refresh and select the review again.")
        try:
            receipt = read_preparation_receipt(record.destination, owned_root=self._root,
                receipt_sha256=record.review_sha256, journal_tip=record.latest_journal_tip)
        except FileNotFoundError as exc:
            raise TrainingRunIndexError(
                "Saved preparation files are missing. Restore the original files or prepare the package again."
            ) from exc
        if (receipt["nonce"] != record.run_id or receipt["process_fingerprint"] != record.plan_fingerprint
                or self.snapshot()[1] != revision):
            raise TrainingRunIndexError("The saved SDXL preparation differs from the selected record.")
        return receipt
