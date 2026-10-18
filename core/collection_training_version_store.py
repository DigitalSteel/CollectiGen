"""Immutable, exact-ID storage for collection version evidence.

No library discovery or mutable index. Retained recovery bytes are never pruned.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re

from .collection_training_version import CollectionTrainingVersion, MAX_VERSION_BYTES
from .external_write_transaction import atomic_verified_bytes
from .history_versions import history_lock
from .model_resource_registry import _bounded_read
from .training_source_io import _assert_unredirected


@dataclass(frozen=True)
class CollectionVersionStorageReceipt:
    status: str
    version_id: str


class CollectionTrainingVersionStore:
    def __init__(self, root):
        self.root = Path(root).absolute()

    def _path(self, zone, version_id):
        if not isinstance(version_id, str) or not re.fullmatch("[0-9a-f]{64}", version_id):
            raise ValueError("An exact Collection version ID is required.")
        path = self.root / zone / (version_id + ".json")
        _assert_unredirected(path)
        return path

    @contextmanager
    def _locked(self):
        _assert_unredirected(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / ".collection-versions.lock"
        _assert_unredirected(lock)
        with history_lock(lock):
            _assert_unredirected(self.root)
            _assert_unredirected(lock)
            yield

    @staticmethod
    def _decode(raw, version_id):
        if raw is None:
            raise FileNotFoundError("The exact Collection version evidence is unavailable.")
        version = CollectionTrainingVersion.from_bytes(raw)
        if version.version_id != version_id:
            raise ValueError("Stored Collection version differs from its exact identity.")
        return version

    def read(self, version_id):
        path = self._path("versions", version_id)
        return self._decode(_bounded_read(path, MAX_VERSION_BYTES), version_id)

    def _retain(self, path, raw):
        current = _bounded_read(path, MAX_VERSION_BYTES)
        if current is not None:
            if current != raw:
                raise ValueError("Immutable Collection version storage changed; preserve it for review.")
            return
        _assert_unredirected(path)
        atomic_verified_bytes(path, raw, label="Collection version recovery evidence")
        if _bounded_read(path, MAX_VERSION_BYTES) != raw:
            raise ValueError("Collection version retained evidence failed read-back.")

    def _publish(self, version, raw):
        target = self._path("versions", version.version_id)
        current = _bounded_read(target, MAX_VERSION_BYTES)
        if current is not None:
            self._decode(current, version.version_id)
            if current != raw:
                raise ValueError("Stored Collection envelope differs; nothing was overwritten.")
            return CollectionVersionStorageReceipt("current", version.version_id)
        # The independent recovery copy survives interruption/corruption of the
        # publication inode. The staging name is retained as owned evidence too.
        recovery = self._path("recovery", version.version_id)
        stage = self._path("staging", version.version_id)
        self._retain(recovery, raw)
        self._retain(stage, raw)
        target.parent.mkdir(parents=True, exist_ok=True)
        _assert_unredirected(target)
        _assert_unredirected(stage)
        try:
            # Atomic no-replace publication on the same explicit storage volume.
            # Unsupported hard links fail with retained exact recovery evidence.
            os.link(stage, target, follow_symlinks=False)
        except FileExistsError:
            if _bounded_read(target, MAX_VERSION_BYTES) != raw:
                raise ValueError("Collection version target became occupied; nothing was overwritten.")
        published = _bounded_read(target, MAX_VERSION_BYTES)
        self._decode(published, version.version_id)
        if published != raw:
            raise ValueError("Collection version publication failed exact read-back.")
        return CollectionVersionStorageReceipt("created", version.version_id)

    def save(self, version):
        raw = version.to_bytes()
        validated = CollectionTrainingVersion.from_bytes(raw)
        with self._locked():
            return self._publish(validated, raw)

    def recover(self, version_id):
        """Reconcile only one explicitly selected retained version, never scan."""
        with self._locked():
            raw = _bounded_read(self._path("recovery", version_id), MAX_VERSION_BYTES)
            version = self._decode(raw, version_id)
            result = self._publish(version, raw)
            return CollectionVersionStorageReceipt(
                "recovered" if result.status == "created" else "current", version_id)
