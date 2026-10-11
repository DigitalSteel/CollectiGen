"""Exclusion and fail-closed admission for explicitly bound Knowledge writers.

This foundation is not wired into canonical writers. It never enrolls history,
executes recovery, or changes canonical documents or linked receipts. Callers
must acquire settings/history guards first, then this lock, then any Character
companion lock. Advisory exclusion cannot authorize or repair external writes.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Mapping
from uuid import UUID

from .history_versions import history_lock, _reject_reparse
from .model_resource_registry import _bounded_read, _json


PARTICIPANT_FILES = (
    ("tags", "tags.json"),
    ("categories", "categories.json"),
    ("relationships", "relationships.json"),
)
MAX_RECEIPT_BYTES = 64 * 1024
MAX_PENDING_RECEIPTS = 128
MAX_PARTICIPANT_BYTES = 64 * 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
_RECEIPT_KEYS = frozenset({
    "schema_version", "kind", "authority_key", "operation_id",
    "transaction_id", "admission_fingerprint", "participants", "before", "after",
})


class KnowledgeAdmissionError(RuntimeError):
    """No canonical write or automatic recovery has been authorized."""


@dataclass(frozen=True)
class LinkedKnowledgeReceipt:
    operation_id: str
    transaction_id: str
    admission_fingerprint: str
    authority_key: str
    participants: tuple[str, ...]
    before: tuple[tuple[str, str | None], ...]
    after: tuple[tuple[str, str | None], ...]
    receipt_sha256: str


@dataclass(frozen=True)
class AdmittedKnowledgeWrite:
    """A momentary admission result, valid only inside the yielding context."""

    authority_key: str
    paths: tuple[tuple[str, Path], ...]


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _fingerprint(value, *, missing=False):
    if missing and value is None:
        return
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise KnowledgeAdmissionError("Knowledge fingerprints must be exact SHA256 values or explicit absence.")


def _checked_path(path, *, directory=False, required=False):
    try:
        _reject_reparse(path)
        info = path.lstat()
    except FileNotFoundError:
        if required:
            raise KnowledgeAdmissionError("The explicitly bound Knowledge authority is unavailable.")
        return None
    except (OSError, RuntimeError) as exc:
        raise KnowledgeAdmissionError("Knowledge authority cannot cross an unavailable or redirected path.") from exc
    predicate = stat.S_ISDIR if directory else stat.S_ISREG
    if not predicate(info.st_mode):
        raise KnowledgeAdmissionError("Knowledge authority has an unsupported file type.")
    return info


def _read(path, limit):
    # Check type before opening so a named pipe cannot block a bounded read.
    _checked_path(path)
    try:
        return _bounded_read(path, limit)
    except (OSError, RuntimeError, ValueError) as exc:
        raise KnowledgeAdmissionError("Knowledge evidence is unavailable, changed, redirected, or oversized.") from exc


@dataclass(frozen=True, init=False)
class KnowledgeWriteAdmission:
    """Bind an exact existing root; construction performs no filesystem reads.

    ``authority_key`` proves only a fixed path binding, never entity incarnation
    or saved-settings permission. The participant set cannot be extended by a
    receipt or caller. All entries in the separately routed pending namespace
    block ordinary writes, including otherwise disjoint participant changes.
    """

    root: Path
    lock_path: Path
    pending_root: Path
    paths: tuple[tuple[str, Path], ...]
    authority_key: str

    def __init__(self, root: str | Path):
        raw = Path(root)
        if not raw.is_absolute() or ".." in raw.parts:
            raise KnowledgeAdmissionError("Bind an exact absolute Knowledge authority root.")
        object.__setattr__(self, "root", raw)
        object.__setattr__(self, "lock_path", raw / ".knowledge-write.lock")
        object.__setattr__(self, "pending_root", raw / ".knowledge-linked-pending")
        object.__setattr__(self, "paths", tuple((key, raw / name) for key, name in PARTICIPANT_FILES))
        declaration = {
            "root": os.path.normcase(str(raw)),
            "participants": [(key, os.path.normcase(str(path))) for key, path in self.paths],
        }
        object.__setattr__(self, "authority_key", hashlib.sha256(json.dumps(
            declaration, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest())

    def _participants(self, participants):
        if not isinstance(participants, (tuple, list)):
            raise KnowledgeAdmissionError("Declare an explicit set of Knowledge participants.")
        keys = tuple(participants)
        allowed = dict(PARTICIPANT_FILES)
        if (not keys or len(keys) > len(allowed)
                or any(not isinstance(key, str) or key not in allowed for key in keys)
                or len(set(keys)) != len(keys)):
            raise KnowledgeAdmissionError("Knowledge participants are missing, repeated, or unsupported.")
        return tuple(sorted(keys))

    @contextmanager
    def locked(self):
        """Exclusion ONLY, for future explicit recovery routing; not write admission.

        Reuses the shared qualified thread-reentrant, cross-process primitive.
        The lock file is the only artifact this class can create; neither root
        nor linked-pending storage is created implicitly.
        """
        initial = _checked_path(self.root, directory=True, required=True)
        if _checked_path(self.lock_path) is None:
            try:
                descriptor = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.close(descriptor)
            except FileExistsError:
                pass  # A cooperating first writer may have created this same lock.
            except OSError as exc:
                raise KnowledgeAdmissionError("Knowledge exclusion is unavailable.") from exc
        lock_before = _checked_path(self.lock_path, required=True)
        with history_lock(self.lock_path):
            current = _checked_path(self.root, directory=True, required=True)
            if (initial.st_dev, initial.st_ino) != (current.st_dev, current.st_ino):
                raise KnowledgeAdmissionError("Knowledge authority changed while acquiring exclusion.")
            lock_after = _checked_path(self.lock_path, required=True)
            if (lock_before.st_dev, lock_before.st_ino) != (lock_after.st_dev, lock_after.st_ino):
                raise KnowledgeAdmissionError("Knowledge exclusion changed while acquiring the lock.")
            yield

    def validate_receipt(self, raw: bytes, *, filename: str) -> LinkedKnowledgeReceipt:
        """Validate bounded bytes without reading participant files or following paths."""
        if not isinstance(raw, bytes) or len(raw) > MAX_RECEIPT_BYTES:
            raise KnowledgeAdmissionError("Linked Knowledge receipt exceeds its supported bound.")
        try:
            value = _json(raw)
        except (RuntimeError, ValueError) as exc:
            raise KnowledgeAdmissionError("Linked Knowledge receipt is corrupt or ambiguous.") from exc
        if (not isinstance(value, dict) or set(value) != _RECEIPT_KEYS
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["kind"] != "knowledge-linked-write"):
            raise KnowledgeAdmissionError("Linked Knowledge receipt uses an unrecognized schema.")
        if value["authority_key"] != self.authority_key:
            raise KnowledgeAdmissionError("Linked Knowledge receipt belongs to another authority.")
        operation = value["operation_id"]
        try:
            valid_operation = isinstance(operation, str) and str(UUID(operation)) == operation
        except (ValueError, AttributeError):
            valid_operation = False
        if not valid_operation or filename != operation + ".json":
            raise KnowledgeAdmissionError("Linked Knowledge receipt has an invalid operation identity.")
        transaction = value["transaction_id"]
        if not isinstance(transaction, str) or not _TOKEN.fullmatch(transaction):
            raise KnowledgeAdmissionError("Linked Knowledge transaction identity is invalid.")
        _fingerprint(value["admission_fingerprint"])
        participants = self._participants(value["participants"])
        for label in ("before", "after"):
            states = value[label]
            if not isinstance(states, dict) or set(states) != set(participants):
                raise KnowledgeAdmissionError("Linked Knowledge receipt does not cover its exact participants.")
            for digest in states.values():
                _fingerprint(digest, missing=True)
        return LinkedKnowledgeReceipt(
            operation, transaction, value["admission_fingerprint"], self.authority_key,
            participants, tuple(sorted(value["before"].items())),
            tuple(sorted(value["after"].items())), hashlib.sha256(raw).hexdigest(),
        )

    def _pending_inventory(self):
        info = _checked_path(self.pending_root, directory=True)
        if info is None:
            return None, ()
        entries = []
        try:
            with os.scandir(self.pending_root) as iterator:
                for entry in iterator:
                    if len(entries) >= MAX_PENDING_RECEIPTS:
                        raise KnowledgeAdmissionError("Too many unresolved linked Knowledge receipts.")
                    state = _checked_path(self.pending_root / entry.name, required=True)
                    if not entry.name.endswith(".json") or state.st_size > MAX_RECEIPT_BYTES:
                        raise KnowledgeAdmissionError("Unrecognized or oversized linked Knowledge pending entry.")
                    entries.append((entry.name, _identity(state)))
        except OSError as exc:
            raise KnowledgeAdmissionError("Linked Knowledge pending inventory is unavailable.") from exc
        return _identity(info), tuple(sorted(entries))

    def _inspect_pending_locked(self):
        initial = self._pending_inventory()
        receipts = []
        transactions = set()
        for filename, _ in initial[1]:
            raw = _read(self.pending_root / filename, MAX_RECEIPT_BYTES)
            if raw is None:
                raise KnowledgeAdmissionError("Linked Knowledge receipt disappeared during admission.")
            receipt = self.validate_receipt(raw, filename=filename)
            if receipt.transaction_id in transactions:
                raise KnowledgeAdmissionError("Linked Knowledge receipts repeat a transaction identity.")
            transactions.add(receipt.transaction_id)
            receipts.append(receipt)
        if self._pending_inventory() != initial:
            raise KnowledgeAdmissionError("Linked Knowledge pending work changed during admission.")
        return tuple(receipts)

    def inspect_pending(self) -> tuple[LinkedKnowledgeReceipt, ...]:
        """Read exact linked receipts under exclusion; this grants no write/recovery."""
        with self.locked():
            return self._inspect_pending_locked()

    @contextmanager
    def admit(self, participants, *, expected_fingerprints: Mapping[str, str | None] | None = None):
        """Hold exclusion through the caller's complete canonical transaction.

        No stale checking is implied when expected_fingerprints is omitted:
        the canonical transaction must validate its own complete pre-state
        under this context. Provided fingerprints must cover every participant.
        """
        keys = self._participants(participants)
        if expected_fingerprints is not None and not isinstance(expected_fingerprints, Mapping):
            raise KnowledgeAdmissionError("Expected Knowledge state must be an explicit participant mapping.")
        expected = None if expected_fingerprints is None else dict(expected_fingerprints)
        if expected is not None:
            if set(expected) != set(keys):
                raise KnowledgeAdmissionError("Expected Knowledge state must cover every participant exactly.")
            for value in expected.values():
                _fingerprint(value, missing=True)
        with self.locked():
            if self._inspect_pending_locked():
                raise KnowledgeAdmissionError("Linked Knowledge work is unresolved; explicit recovery is required before saving.")
            paths = dict(self.paths)
            for key in keys:
                _checked_path(paths[key])
                if expected is not None:
                    raw = _read(paths[key], MAX_PARTICIPANT_BYTES)
                    actual = None if raw is None else hashlib.sha256(raw).hexdigest()
                    if actual != expected[key]:
                        raise KnowledgeAdmissionError("Knowledge changed since review; reload before saving.")
            # A nonparticipating receipt publisher must not slip pending work
            # into the interval spent reading current canonical evidence.
            if self._inspect_pending_locked():
                raise KnowledgeAdmissionError("Linked Knowledge work appeared during admission; explicit recovery is required.")
            yield AdmittedKnowledgeWrite(self.authority_key, tuple((key, paths[key]) for key in keys))
