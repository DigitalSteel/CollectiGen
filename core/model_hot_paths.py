"""Read-only qualification of exact prepared Swarm Fast-folder namespaces.

Cache authority and Windows file/ancestor handles stay held through the caller's
settings transaction. This is a bounded point-in-time qualification, not a
filesystem sandbox or proof about independently configured backend paths.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from dataclasses import asdict, dataclass
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from .model_cache import ModelCacheService
from .model_engine_locations import HotFolderProjection, project_swarm_hot_folders
from .model_resource_registry import ModelResourceError
from .model_storage_policy import validate_policy_roots
from .model_use_leases import cache_lock, cache_state, digest
from .model_workspace import _path, _pinned_model_file, _pinned_parent
from .swarm_model_paths import ModelPathInventory

MAX_NAMESPACE_ITEMS = 4096


@dataclass(frozen=True)
class HotFolderQualification:
    qualification_id: str
    projection: HotFolderProjection
    directories: tuple
    files: tuple


class ModelHotPathService:
    def __init__(self, registry, *, protected_roots=(), managed_roots=()):
        self.registry = registry
        self.protected_roots = tuple(protected_roots)
        self.managed_roots = tuple(managed_roots)
        self._issued = {}

    def _authority(self, projection):
        if not isinstance(projection, HotFolderProjection):
            raise ModelResourceError("Review an exact prepared-folder projection first.")
        value, token = self.registry.snapshot()
        if token != projection.registry_token:
            raise ModelResourceError("The registry changed; review prepared folders again.")
        inventory = ModelPathInventory(projection.before_model_root, dict(projection.before),
            dict(projection.inventory_binding), projection.inventory_fingerprint)
        fresh = project_swarm_hot_folders(value, inventory, projection.operation_ids, registry_token=token)
        if fresh != projection:
            raise ModelResourceError("The prepared folder projection changed; review again.")
        if projection.held:
            raise ModelResourceError(" ".join(projection.held))
        if len(projection.entry_ids) > MAX_NAMESPACE_ITEMS or len(projection.directory_entries) > MAX_NAMESPACE_ITEMS:
            raise ModelResourceError("Prepared folder contents exceed the bounded review limit.")
        policy = validate_policy_roots(value.get("workspace", {}),
            protected_roots=self.protected_roots, managed_roots=self.managed_roots)
        return value, Path(policy.fast_root).absolute()

    @staticmethod
    def _cancel(should_cancel):
        if should_cancel and should_cancel():
            raise InterruptedError("Prepared-folder qualification cancelled; no engine settings changed.")

    def _inside(self, raw, root):
        # Lexical containment precedes any inspection, including reparse checks.
        path = Path(raw).absolute()
        if path != root and root not in path.parents:
            raise ModelResourceError("Prepared folder escapes the current Fast root.")
        path = _path(path, self.protected_roots)
        for ancestor in (path, *path.parents):
            try:
                info = ancestor.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if int(getattr(info, "st_file_attributes", 0)) & (0x1000 | 0x40000 | 0x400000):
                raise ModelResourceError("Offline or cloud-placeholder model paths need local qualification first.")
        return path

    def _directory(self, path, pins):
        """Pin existing directory or its closest existing ancestor, never mkdir."""
        cursor = path
        while not cursor.exists():
            cursor = cursor.parent
        cursor = _path(cursor, self.protected_roots)
        if not cursor.is_dir():
            raise ModelResourceError("A prepared folder or ancestor is not a directory.")
        pins.enter_context(_pinned_parent(cursor, self.protected_roots))
        before = cursor.stat(follow_symlinks=False)
        _path(path, self.protected_roots)
        if path.exists() != (cursor == path):
            raise ModelResourceError("A prepared directory changed during qualification.")
        return (str(path), cursor == path, str(cursor), before.st_dev, before.st_ino)

    def _inspect(self, projection, pins, should_cancel=None, progress=None):
        self._cancel(should_cancel)
        value, root = self._authority(projection)
        state = cache_state(value)
        verifier = ModelCacheService(self.registry, protected_roots=self.protected_roots,
            managed_roots=self.managed_roots)
        files, directories = [], []
        remaining = MAX_NAMESPACE_ITEMS

        def children(path):
            nonlocal remaining
            rows = []
            with os.scandir(path) as listing:
                for item in listing:
                    self._cancel(should_cancel)
                    remaining -= 1
                    if remaining < 0:
                        raise ModelResourceError("Prepared folder contents exceed the bounded review limit.")
                    child = self._inside(item.path, root)
                    info = child.stat(follow_symlinks=False)
                    rows.append((child.name, info.st_dev, info.st_ino, info.st_mode,
                        info.st_size, info.st_mtime_ns, info.st_nlink))
            return tuple(sorted(rows))

        def empty(path):
            evidence = self._directory(path, pins)
            if evidence[1] and children(path):
                raise ModelResourceError("An inactive or implicit engine folder is populated; review its contents before this handoff.")
            directories.append((*evidence, ()))

        for folder, entry_ids in projection.directory_entries:
            self._cancel(should_cancel)
            directory = self._inside(folder, root)
            evidence = self._directory(directory, pins)
            if not evidence[1]:
                raise ModelResourceError("A prepared model folder disappeared. Prepare it again.")
            expected = {}
            for entry_id in entry_ids:
                self._cancel(should_cancel)
                entry = state["entries"][entry_id]
                path = self._inside(entry["path"], root)
                if path.parent != directory or path.name in expected:
                    raise ModelResourceError("A prepared directory has conflicting file ownership.")
                pins.enter_context(_pinned_model_file(path, self.protected_roots))
                info = path.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ModelResourceError("Prepared model has linked aliases or is not a regular file.")
                verified = verifier._verify_entry(value, entry_id, should_cancel=should_cancel, progress=progress)
                expected[path.name] = entry_id
                files.append((entry_id, str(path), tuple(verified["file_state"])))
            membership = children(directory)
            seen = set()
            for name, dev, ino, mode, size, mtime, links in membership:
                if name in expected and stat.S_ISREG(mode):
                    seen.add(name)
                elif stat.S_ISDIR(mode) and (match := re.fullmatch(r"\.collectigen-cache-([0-9a-f]{32})-([0-9a-f]{32})", name)):
                    # Retired entries leave their exact empty staging folders.
                    # Verify the immutable receipt instead of blocking future
                    # cleanup/reprepare cycles or trusting a dot-name alone.
                    operation_id, entry_id = match.groups()
                    owner, receipt = verifier._load_operation(value, operation_id)
                    rows = [row for row in receipt.get("rows", []) if row.get("entry_id") == entry_id]
                    member = verifier._member(owner, entry_id)
                    if (owner.get("kind") != "prepare" or owner.get("phase") != "complete" or len(rows) != 1
                            or Path(rows[0]["stage"]).parent != directory / name
                            or tuple(member.get("stage_parent", ())) != (dev, ino)):
                        raise ModelResourceError("A staging directory has no exact completed ownership receipt.")
                    empty(directory / name)
                else:
                    raise ModelResourceError("An exposed role folder contains unowned or unsupported contents; review it before sharing.")
            if seen != set(expected):
                raise ModelResourceError("A prepared model disappeared during folder qualification.")
            directories.append((*evidence, membership))
        # Inspect only the new current-Fast namespaces. BEFORE paths are never
        # interpreted or traversed, even if relative, cold, broad or unknown.
        for folder in dict.fromkeys((*projection.empty_directories, *projection.implicit_directories)):
            self._cancel(should_cancel)
            empty(self._inside(folder, root))
        # Hashing later members can be slow. Repeat every bounded membership
        # check after all hashes, immediately before issuing/entering the guard.
        remaining = MAX_NAMESPACE_ITEMS
        for raw, present, ancestor, dev, ino, membership in directories:
            self._cancel(should_cancel)
            path = self._inside(raw, root)
            info = Path(ancestor).stat(follow_symlinks=False)
            if (info.st_dev, info.st_ino) != (dev, ino) or path.exists() != present or (present and children(path) != membership):
                raise ModelResourceError("Prepared folder contents changed during qualification; review again.")
        if self.registry.snapshot()[1] != projection.registry_token:
            raise ModelResourceError("The registry changed during qualification; review again.")
        return tuple(directories), tuple(files)

    def qualify(self, projection, *, should_cancel=None, progress=None):
        with cache_lock(self.registry, protected_roots=self.protected_roots), ExitStack() as pins:
            directories, files = self._inspect(projection, pins, should_cancel, progress)
            result = HotFolderQualification(uuid4().hex, projection, directories, files)
            if len(self._issued) >= 32:
                self._issued.pop(next(iter(self._issued)))
            self._issued[result.qualification_id] = digest(asdict(result))
            return result

    @contextmanager
    def guard(self, qualification, *, should_cancel=None, progress=None):
        if (not isinstance(qualification, HotFolderQualification)
                or self._issued.get(qualification.qualification_id) != digest(asdict(qualification))):
            raise ModelResourceError("Qualify unchanged prepared folders in this session before applying.")
        with cache_lock(self.registry, protected_roots=self.protected_roots), ExitStack() as pins:
            directories, files = self._inspect(qualification.projection, pins, should_cancel, progress)
            if directories != qualification.directories or files != qualification.files:
                raise ModelResourceError("Prepared files or folder contents changed; qualify and review again.")
            self._cancel(should_cancel)
            yield
