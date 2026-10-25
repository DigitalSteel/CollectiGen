"""Explicit byte identity for installed preparation packs; never install or load.

The source adapter reads only an explicitly selected, admitted component folder.
Its identity is independent of location and of Component Manager readiness.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .model_resource_registry import ModelResourceError, _bounded_read, _bytes, _cancel, _json
from .training_source_io import _assert_unredirected, _identity

PACK_ID = re.compile(r"pack-sha256:[0-9a-f]{64}\Z")
MAX_PACK_FILES = 4096
MAX_PACK_ENTRIES = 8192
IGNORED = {".cache", ".git", "__pycache__"}


def _path(root, protected_roots=()):
    from .model_workspace import _path as protected_path
    return protected_path(root, protected_roots)


def _files(root, should_cancel=None):
    pending, paths, count = [root], [], 0
    while pending:
        directory = pending.pop()
        _assert_unredirected(directory)
        if int(getattr(directory.stat(follow_symlinks=False), "st_file_attributes", 0)) & (0x1000 | 0x40000 | 0x400000):
            raise ModelResourceError("The managed model must be fully available locally.")
        with os.scandir(directory) as entries:
            for entry in entries:
                _cancel(should_cancel)
                count += 1
                if count > MAX_PACK_ENTRIES:
                    raise ModelResourceError("The managed pack exceeds the supported entry limit.")
                if entry.name in IGNORED:
                    continue
                path = Path(entry.path)
                _assert_unredirected(path)
                if int(getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)) & (0x1000 | 0x40000 | 0x400000):
                    raise ModelResourceError("The managed model must be fully available locally.")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    paths.append(path.relative_to(root).as_posix())
                    if len(paths) > MAX_PACK_FILES:
                        raise ModelResourceError("The managed pack exceeds the supported file limit.")
                else:
                    raise ModelResourceError("Managed packs require ordinary local files.")
    names = sorted(paths)
    if len({name.casefold() for name in names}) != len(names):
        raise ModelResourceError("Managed pack filenames are ambiguous on Windows.")
    return names


def _hash(path, should_cancel=None, progress=None):
    _cancel(should_cancel)
    _assert_unredirected(path)
    before = path.stat(follow_symlinks=False)
    if int(getattr(before, "st_file_attributes", 0)) & (0x1000 | 0x40000 | 0x400000):
        raise ModelResourceError("The managed model must be fully available locally.")
    if not stat.S_ISREG(before.st_mode):
        raise ModelResourceError("A managed pack entry must be a regular file.")
    digest, total = hashlib.sha256(), 0
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        if _identity(os.fstat(handle.fileno())) != _identity(before):
            raise ModelResourceError("A managed pack file changed before verification.")
        while True:
            _cancel(should_cancel)
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            total += len(block)
            if progress:
                progress(total, before.st_size, "Verifying managed model files")
        after = os.fstat(handle.fileno())
    _assert_unredirected(path)
    if _identity(before) != _identity(after) or _identity(after) != _identity(path.stat(follow_symlinks=False)) or total != after.st_size:
        raise ModelResourceError("A managed pack file changed during verification.")
    return digest.hexdigest(), total, _identity(after)


def canonical_pack_identity(identity):
    """Return hash-bound fields while allowing retained future annotations."""
    result = {key: identity[key] for key in ("kind", "pack_version", "model_id", "revision", "resource_id", "size")}
    result["files"] = [{key: row[key] for key in ("path", "sha256", "size")} for row in identity["files"]]
    return result


def validate_pack_identity(identity):
    if not isinstance(identity, dict) or identity.get("kind") != "managed_model_pack" or identity.get("pack_version") != 1:
        raise ModelResourceError("The managed pack identity version is unsupported.")
    model, revision, files = identity.get("model_id"), identity.get("revision"), identity.get("files")
    if not isinstance(model, str) or not model or len(model) > 200 or not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ModelResourceError("Managed packs need an exact model and immutable revision.")
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_PACK_FILES:
        raise ModelResourceError("The managed pack file manifest is invalid.")
    names = []
    for row in files:
        if not isinstance(row, dict):
            raise ModelResourceError("The managed pack file record is invalid.")
        name = row.get("path", "")
        if not isinstance(name, str) or not name or "\\" in name or ":" in name or PurePosixPath(name).is_absolute() or any(p in {"", ".", "..", *IGNORED} for p in name.split("/")):
            raise ModelResourceError("The managed pack contains an unsafe relative filename.")
        if not isinstance(row.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) or type(row.get("size")) is not int or row["size"] < 0:
            raise ModelResourceError("The managed pack file hash or size is invalid.")
        names.append(name)
    if names != sorted(names) or len(set(n.casefold() for n in names)) != len(names):
        raise ModelResourceError("The managed pack manifest must be uniquely ordered.")
    body = {"kind": "managed_model_pack", "pack_version": 1, "model_id": model, "revision": revision,
        "files": [{key: row[key] for key in ("path", "sha256", "size")} for row in files]}
    if identity.get("resource_id") != "pack-sha256:" + hashlib.sha256(_bytes(body)).hexdigest() or identity.get("size") != sum(row["size"] for row in files):
        raise ModelResourceError("The managed pack identity does not match its manifest.")


@dataclass(frozen=True)
class ManagedPackInspection:
    path: Path
    resource_id: str
    size: int
    file_state: tuple
    identity: dict
    states: tuple

    def stored_identity(self):
        return deepcopy(self.identity)

    def check_current(self):
        _assert_unredirected(self.path)
        if _files(self.path) != [row["path"] for row in self.identity["files"]]:
            raise ModelResourceError("The managed pack file set changed after verification.")
        for name, state in self.states:
            path = self.path / name
            _assert_unredirected(path)
            if _identity(path.stat(follow_symlinks=False)) != state:
                raise ModelResourceError("The managed pack changed after verification.")


def inspect_managed_pack(root, model_id, *, protected_roots=(), progress=None, should_cancel=None):
    from .ai_interrogation import MODEL_ACQUISITIONS, MODEL_INSTALLATION_MANIFEST, WD_TAGGER_MODELS
    if model_id not in MODEL_ACQUISITIONS:
        raise ModelResourceError("Choose an admitted preparation model in Component Manager.")
    root = _path(root, protected_roots)
    _cancel(should_cancel)
    names = _files(root, should_cancel)
    marker_raw = _bounded_read(root / MODEL_INSTALLATION_MANIFEST, 64 * 1024)
    marker = _json(marker_raw or b"null")
    acquisition = MODEL_ACQUISITIONS[model_id]
    if not isinstance(marker, dict) or marker.get("model_id") != model_id or marker.get("revision") != acquisition.revision:
        raise ModelResourceError("The installed component has no current pinned revision; review it in Component Manager.")
    if model_id in WD_TAGGER_MODELS:
        required = {"model.onnx", "selected_tags.csv"}
    else:
        required = {"config.json", "tokenizer_config.json", "preprocessor_config.json"}
        if not any(name.endswith(".safetensors") for name in names) or not ({"tokenizer.json", "vocab.json", "tokenizer.model"} & set(names)):
            raise ModelResourceError("The captioner pack is missing weights or tokenizer files.")
        for index in (name for name in names if name.endswith(".safetensors.index.json")):
            value = _json(_bounded_read(root / index, 4 * 1024 * 1024) or b"null")
            mapping = value.get("weight_map") if isinstance(value, dict) else None
            if not isinstance(mapping, dict) or not mapping or not all(isinstance(v, str) and v in names for v in mapping.values()):
                raise ModelResourceError("The captioner pack is missing required weight shards.")
    if not required.issubset(names):
        raise ModelResourceError("The managed pack is missing required model files.")
    files, states = [], []
    for name in names:
        digest, size, state = _hash(root / name, should_cancel, progress)
        files.append({"path": name, "sha256": digest, "size": size})
        states.append((name, state))
    identity = {"kind": "managed_model_pack", "pack_version": 1, "model_id": model_id, "revision": acquisition.revision, "files": files}
    identity["resource_id"] = "pack-sha256:" + hashlib.sha256(_bytes(identity)).hexdigest()
    identity["size"] = sum(row["size"] for row in files)
    result = ManagedPackInspection(root, identity["resource_id"], identity["size"], _identity(root.stat()), identity, tuple(states))
    result.check_current()
    if _bounded_read(root / MODEL_INSTALLATION_MANIFEST, 64 * 1024) != marker_raw:
        raise ModelResourceError("The installed component revision changed during verification.")
    _cancel(should_cancel)
    return result


class InstalledComponentModelSource:
    """Settings-to-component adapter with no constructor IO or runtime probes."""
    def __init__(self, settings, *, protected_roots=()):
        self.settings, self.protected_roots = settings, tuple(protected_roots)

    def choices(self, role=""):
        from .ai_interrogation import WD_TAGGER_MODELS, PLAIN_LANGUAGE_MODELS
        values = []
        for models, typed_role in ((WD_TAGGER_MODELS, "tagger"), (PLAIN_LANGUAGE_MODELS, "captioner")):
            if role and role != typed_role:
                continue
            for model_id, row in models.items():
                values.append((model_id, row[0], typed_role, "WD v3" if typed_role == "tagger" else row[0]))
        return tuple(values)

    def target(self, model_id):
        from .ai_interrogation import WD_TAGGER_MODELS, PLAIN_LANGUAGE_MODELS
        row = {**WD_TAGGER_MODELS, **PLAIN_LANGUAGE_MODELS}.get(model_id)
        directory = str(getattr(self.settings, "ai_models_directory", "") or "")
        if row is None or not directory or not Path(directory).is_absolute():
            raise ModelResourceError("Configure the managed model location in Setup first.")
        return _path(Path(directory) / row[1], self.protected_roots)

    def inspect(self, model_id, **kwargs):
        return inspect_managed_pack(self.target(model_id), model_id, protected_roots=self.protected_roots, **kwargs)
