"""Offline, explicit-file model identity and provenance; never a model loader.

Local locations live only in this private registry. Generation evidence and
training manifests continue to carry portable identities, not these paths.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .annotation import GenerationResource
from .training_source_io import _assert_unredirected, _identity


# Read-only identity checks also run in the isolated preflight interpreter.
# Keep application/transaction dependencies out of that bootstrap; preserve the
# existing patchable write functions for the registry's recovery boundary.
def atomic_verified_bytes(*args, **kwargs):
    from .external_write_transaction import atomic_verified_bytes as write
    return write(*args, **kwargs)


def write_verified_json(*args, **kwargs):
    from .external_write_transaction import write_verified_json as write
    return write(*args, **kwargs)


MAX_HEADER_BYTES = 4 * 1024 * 1024
MAX_STORE_BYTES = 32 * 1024 * 1024
MAX_TENSORS = 100_000
MAX_RESOURCES = 20_000
HASH_CHUNK_BYTES = 1024 * 1024
RESOURCE_ROLES = ("checkpoint", "unet", "text_encoder", "vae", "lora", "embedding", "extension",
                  "vision_encoder", "control_model", "upscaler", "video_companion", "tagger", "captioner")
_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DTYPE_BYTES = {"BOOL": 1, "U8": 1, "I8": 1, "I16": 2, "U16": 2, "F16": 2, "BF16": 2,
                "I32": 4, "U32": 4, "F32": 4, "I64": 8, "U64": 8, "F64": 8,
                "F8_E4M3": 1, "F8_E5M2": 1, "F8_E4M3FNUZ": 1, "F8_E5M2FNUZ": 1}


class ModelResourceError(RuntimeError):
    pass


class ResourceRecoveryRequired(ModelResourceError):
    pass


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ModelResourceError("Duplicate JSON fields are not supported.")
        result[key] = value
    return result


def _json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ModelResourceError("The resource record or tensor header is invalid JSON.") from exc


def _bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def _digest(raw: bytes | None) -> str:
    return hashlib.sha256(raw if raw is not None else b"<absent>").hexdigest()


def _cancel(should_cancel):
    if should_cancel and should_cancel():
        raise InterruptedError("Model resource work was cancelled.")


def _current(inspection: ResourceInspection):
    if hasattr(inspection, "check_current"):
        inspection.check_current()
        return
    _assert_unredirected(inspection.path)
    if _identity(inspection.path.stat(follow_symlinks=False)) != inspection.file_state:
        raise ModelResourceError("A model resource changed after verification; inspect it again.")


def _bounded_read(path: Path, limit: int = MAX_STORE_BYTES) -> bytes | None:
    _assert_unredirected(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or _identity(before) != _identity(path.stat(follow_symlinks=False)):
                raise ModelResourceError("A resource record must be a regular file.")
            raw = handle.read(limit + 1)
            after = os.fstat(handle.fileno())
            if _identity(before) != _identity(after) or _identity(after) != _identity(path.stat(follow_symlinks=False)):
                raise ModelResourceError("The resource record changed while reading; retry.")
        _assert_unredirected(path)
    except FileNotFoundError:
        return None
    if len(raw) > limit:
        raise ModelResourceError("The resource record exceeds the supported size limit.")
    return raw


def _header(raw: bytes, data_bytes: int) -> dict:
    if not raw.startswith(b"{"):
        raise ModelResourceError("The tensor header must begin with a JSON object.")
    value = _json(raw)
    if not isinstance(value, dict) or len(value) > MAX_TENSORS + 1:
        raise ModelResourceError("The tensor header has an unsupported structure or count.")
    metadata = value.get("__metadata__", {})
    if not isinstance(metadata, dict) or any(not isinstance(v, str) for v in metadata.values()):
        raise ModelResourceError("Tensor metadata must contain string values.")
    intervals = []
    dtypes = set()
    for name, tensor in value.items():
        if name == "__metadata__":
            continue
        if not isinstance(tensor, dict) or set(tensor) != {"dtype", "shape", "data_offsets"}:
            raise ModelResourceError("A tensor uses an unsupported header schema.")
        dtype, shape, offsets = tensor["dtype"], tensor["shape"], tensor["data_offsets"]
        if not isinstance(dtype, str) or dtype not in _DTYPE_BYTES:
            raise ModelResourceError("This tensor dtype is not yet supported for safe inspection.")
        if not isinstance(shape, list) or len(shape) > 32 or any(type(n) is not int or n < 0 or n > 2**63 - 1 for n in shape):
            raise ModelResourceError("A tensor shape is invalid or exceeds supported bounds.")
        if not isinstance(offsets, list) or len(offsets) != 2 or any(type(n) is not int for n in offsets):
            raise ModelResourceError("Tensor data offsets are invalid.")
        start, end = offsets
        if not 0 <= start <= end <= data_bytes or math.prod(shape) * _DTYPE_BYTES[dtype] != end - start:
            raise ModelResourceError("Tensor shape, dtype and data bounds do not agree.")
        intervals.append((start, end))
        dtypes.add(dtype)
    if not intervals:
        raise ModelResourceError("The resource contains no tensors.")
    cursor = 0
    for start, end in sorted(intervals):
        if start != cursor:
            raise ModelResourceError("Tensor data contains overlapping ranges or unclaimed bytes.")
        cursor = end
    if cursor != data_bytes:
        raise ModelResourceError("The resource has unclaimed trailing data.")
    return {"format": "safetensors", "tensor_count": len(intervals),
            "dtypes": sorted(dtypes), "metadata": metadata,
            "header_sha256": hashlib.sha256(raw).hexdigest(),
            "inspection_version": 1}


@dataclass(frozen=True)
class ResourceInspection:
    path: Path
    resource_id: str
    size: int
    file_state: tuple[int, ...]
    container: dict

    def stored_identity(self) -> dict:
        """Private inspection record; embedded metadata is not portable/export-safe."""
        return {"resource_id": self.resource_id, "size": self.size, "container": deepcopy(self.container)}


def inspect_model_resource(path: str | Path, *, progress=None, should_cancel=None, header_observer=None) -> ResourceInspection:
    """Inspect a bounded header and hash one stable file without loading tensors."""
    source = Path(path).absolute()
    if source.suffix.casefold() != ".safetensors":
        raise ModelResourceError("Choose a .safetensors file. Other model formats remain unsupported.")
    _cancel(should_cancel)
    _assert_unredirected(source)
    attributes = int(getattr(source.stat(follow_symlinks=False), "st_file_attributes", 0))
    if attributes & (0x1000 | 0x40000 | 0x400000):
        raise ModelResourceError("The selected model is not fully available locally; implicit cloud hydration is disabled.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or _identity(before) != _identity(source.stat(follow_symlinks=False)):
            raise ModelResourceError("The selected model is not a stable regular file.")
        prefix = handle.read(8)
        if len(prefix) != 8:
            raise ModelResourceError("The tensor file is truncated.")
        header_size = int.from_bytes(prefix, "little")
        if not 2 <= header_size <= MAX_HEADER_BYTES or header_size > before.st_size - 8:
            raise ModelResourceError("The tensor header is truncated or exceeds the inspection limit.")
        raw_header = handle.read(header_size)
        container = _header(raw_header, before.st_size - 8 - header_size)
        # Internal read-only analyses share this validated header and stable
        # handle. Their tentative result has no identity authority until the
        # full hash and final stale checks below succeed. Never store it in the
        # registry or expose private tensor names through ResourceInspection.
        if header_observer is not None:
            header_observer(raw_header)
        digest = hashlib.sha256(prefix + raw_header)
        completed = 8 + header_size
        while True:
            _cancel(should_cancel)
            block = handle.read(HASH_CHUNK_BYTES)
            if not block:
                break
            digest.update(block)
            completed += len(block)
            if progress:
                progress(completed, before.st_size, "Verifying selected model bytes")
        after = os.fstat(handle.fileno())
    _assert_unredirected(source)
    if _identity(before) != _identity(after) or _identity(after) != _identity(source.stat(follow_symlinks=False)) or completed != after.st_size:
        raise ModelResourceError("The selected model changed during inspection; inspect it again.")
    _cancel(should_cancel)
    if progress:
        progress(completed, completed, "Model identity verified; compatibility remains unverified")
    return ResourceInspection(source, "sha256:" + digest.hexdigest(), completed, _identity(after), container)


@dataclass(frozen=True)
class ResourceResolution:
    status: str
    resource_id: str = ""
    inspection: ResourceInspection | None = None
    reason: str = ""
    compatibility: str = "unverified"


class ModelResourceRegistry:
    """Lazy private registry with explicit reads/writes and exact recovery.

    No constructor IO, directory discovery, metadata promotion or model loading.
    Each writer takes a process lock and an optimistic snapshot precondition.
    """
    def __init__(self, path: str | Path):
        self.path = Path(path).absolute()
        self.pending = self.path.with_name(self.path.name + ".pending.json")

    def snapshot(self) -> tuple[dict, str]:
        if _bounded_read(self.pending, 4096) is not None:
            raise ResourceRecoveryRequired("A model registry update was interrupted. Recover it before continuing.")
        raw = _bounded_read(self.path)
        value = _json(raw) if raw is not None else {"schema_version": 1, "resources": {}}
        if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] not in (1, 2):
            raise ModelResourceError("This model registry schema is unsupported; its data was left unchanged.")
        records = value.get("resources")
        if not isinstance(records, dict) or len(records) > MAX_RESOURCES:
            raise ModelResourceError("The model registry has an invalid or unsupported resource set.")
        for key, record in records.items():
            from .managed_model_identity import PACK_ID, validate_pack_identity
            pack = bool(value["schema_version"] == 2 and PACK_ID.fullmatch(key))
            if not (_ID.fullmatch(key) or pack) or not isinstance(record, dict) or not isinstance(record.get("identity"), dict) or record["identity"].get("resource_id") != key:
                raise ModelResourceError("The model registry contains an invalid identity.")
            if pack:
                validate_pack_identity(record["identity"])
            if any(not isinstance(record.get(name), list) for name in ("locations", "declarations", "lineage")):
                raise ModelResourceError("The model registry contains an invalid resource record.")
            if any(not isinstance(row, dict) or not isinstance(row.get("path"), str) or not Path(row["path"]).is_absolute() for row in record["locations"]):
                raise ModelResourceError("The model registry contains an invalid local location.")
            if any(not isinstance(row, dict) for row in record["declarations"] + record["lineage"]):
                raise ModelResourceError("The model registry contains invalid declarations or lineage.")
            if any(not isinstance(row.get("role"), str) or not isinstance(row.get("family"), str) for row in record["declarations"]):
                raise ModelResourceError("The model registry contains invalid role/family declarations.")
        return value, _digest(raw)

    @contextmanager
    def _writer(self):
        _assert_unredirected(self.path.parent)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_name(self.path.name + ".lock")
        _assert_unredirected(lock)
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "r+b") as handle:
            state = os.fstat(handle.fileno())
            if not stat.S_ISREG(state.st_mode) or _identity(state) != _identity(lock.stat(follow_symlinks=False)):
                raise ModelResourceError("The model registry lock is not a stable regular file.")
            if state.st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ModelResourceError("Another model registry update is active; retry shortly.") from exc
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _save(self, value: dict, expected: str):
        if len(value["resources"]) > MAX_RESOURCES:
            raise ModelResourceError("The model registry exceeds the supported resource count.")
        before = _bounded_read(self.path)
        if _digest(before) != expected:
            raise ModelResourceError("The model registry changed after preview; reload and retry.")
        after = _bytes(value)
        if len(after) > MAX_STORE_BYTES:
            raise ModelResourceError("The model registry exceeds the supported size limit.")
        if before == after:
            return
        operation = uuid4().hex
        recovery = self.path.parent / "model-resource-recovery" / operation
        _assert_unredirected(recovery)
        recovery.mkdir(parents=True, exist_ok=False)
        atomic_verified_bytes(recovery / "before.json", before or b"", label="Model registry backup")
        atomic_verified_bytes(recovery / "after.json", after, label="Model registry proposed state")
        receipt = {"schema_version": 1, "operation_id": operation, "before_absent": before is None,
                   "before_sha256": _digest(before), "after_sha256": _digest(after)}
        write_verified_json(recovery / "receipt.json", receipt)
        write_verified_json(self.pending, receipt)
        # If anything after pending publication fails, recovery reconciles only
        # the exact before/after bytes. It never overwrites unrelated changes.
        if _digest(_bounded_read(self.path)) != expected:
            raise ResourceRecoveryRequired("The model registry changed during publication; recovery is required.")
        atomic_verified_bytes(self.path, after, label="Model resource registry")
        self._finish_recovery(receipt)

    def _finish_recovery(self, receipt):
        operation = receipt.get("operation_id", "")
        if not isinstance(operation, str) or not re.fullmatch(r"[0-9a-f]{32}", operation):
            raise ResourceRecoveryRequired("The model registry recovery identity is invalid.")
        recovery = self.path.parent / "model-resource-recovery" / operation
        saved = _bounded_read(recovery / "receipt.json", 4096)
        if saved is None or _json(saved) != receipt:
            raise ResourceRecoveryRequired("The model registry recovery receipt changed.")
        before, after = _bounded_read(recovery / "before.json"), _bounded_read(recovery / "after.json")
        if before is None or after is None or _digest(None if receipt.get("before_absent") else before) != receipt.get("before_sha256") or _digest(after) != receipt.get("after_sha256"):
            raise ResourceRecoveryRequired("Model registry recovery evidence is missing or damaged.")
        current = _bounded_read(self.path)
        if current == after:
            outcome = "committed"
        elif current == (None if receipt.get("before_absent") else before):
            outcome = "unchanged"
        else:
            raise ResourceRecoveryRequired("Model registry content differs from both recovery states; preserve it for review.")
        write_verified_json(recovery / "result.json", {"outcome": outcome, "operation_id": operation})
        if _json(_bounded_read(self.pending, 4096) or b"null") != receipt:
            raise ResourceRecoveryRequired("The pending model registry receipt changed.")
        self.pending.unlink()
        return outcome

    def recover(self) -> str:
        with self._writer():
            raw = _bounded_read(self.pending, 4096)
            if raw is None:
                return "nothing_pending"
            receipt = _json(raw)
            if not isinstance(receipt, dict) or receipt.get("schema_version") != 1:
                raise ResourceRecoveryRequired("The model registry recovery version is unsupported.")
            return self._finish_recovery(receipt)

    def register(self, inspection: ResourceInspection, *, role: str, family: str,
                 expected_registry: str, source: str = "", license_note: str = "",
                 progress=None, should_cancel=None, lineage: dict | None = None) -> str:
        if role not in RESOURCE_ROLES or not isinstance(family, str) or not family.strip() or len(family) > 160:
            raise ModelResourceError("Choose a resource role and declared model family.")
        if not isinstance(source, str) or not isinstance(license_note, str) or len(source) > 2000 or len(license_note) > 2000:
            raise ModelResourceError("Resource attribution is too long.")
        fresh = inspect_model_resource(inspection.path, progress=progress, should_cancel=should_cancel)
        if fresh != inspection:
            raise ModelResourceError("The selected model changed after preview; inspect it again.")
        with self._writer():
            value, current = self.snapshot()
            if current != expected_registry:
                raise ModelResourceError("The model registry changed after preview; reload and retry.")
            records = value["resources"]
            existing = records.get(fresh.resource_id)
            record = deepcopy(existing) if existing else {"identity": fresh.stored_identity(), "locations": [], "declarations": [], "lineage": []}
            if record["identity"] != fresh.stored_identity():
                raise ModelResourceError("The registered resource inspection differs; review the existing record.")
            location = next((deepcopy(row) for row in record["locations"] if str(row.get("path", "")).casefold() == str(fresh.path).casefold()), {})
            location.update({"path": str(fresh.path), "file_state": list(fresh.file_state)})
            record["locations"] = [row for row in record["locations"] if str(row.get("path", "")).casefold() != str(fresh.path).casefold()] + [location]
            declaration = {"role": role, "family": family.strip(), "source": source, "license_note": license_note,
                           "authority": "user_declared", "compatibility": "unverified"}
            if declaration not in record["declarations"]:
                record["declarations"].append(declaration)
            if lineage is not None:
                raise ModelResourceError("Derived provenance must use register_training_result.")
            records[fresh.resource_id] = record
            _cancel(should_cancel)
            _current(fresh)
            self._save(value, current)
        return fresh.resource_id

    def attach_verified_location(self, resource_id: str, inspection: ResourceInspection, *,
                                 expected_registry: str, progress=None, should_cancel=None) -> str:
        """Attach one exact copy to an existing single-file record, never redeclare it.

        ModelWorkspace owns the protected-root guard before calling this method.
        This writer rechecks bytes and revision and preserves every other field.
        """
        if not isinstance(resource_id, str) or not _ID.fullmatch(resource_id):
            raise ModelResourceError("Exact-copy recovery supports registered single-file SHA-256 identities only.")
        if not isinstance(inspection, ResourceInspection) or inspection.resource_id != resource_id:
            raise ModelResourceError("The selected copy does not match the registered model identity.")
        value, token = self.snapshot()
        if token != expected_registry:
            raise ModelResourceError("The model registry changed; preview the exact copy again.")
        record = value["resources"].get(resource_id)
        if record is None or record["identity"] != inspection.stored_identity():
            raise ModelResourceError("The registered model is missing or its recorded identity differs; reload before recovery.")
        fresh = inspect_model_resource(inspection.path, progress=progress, should_cancel=should_cancel)
        if fresh != inspection:
            raise ModelResourceError("The selected copy changed after preview; preview it again.")
        with self._writer():
            value, token = self.snapshot()
            if token != expected_registry:
                raise ModelResourceError("The model registry changed during verification; preview the exact copy again.")
            record = value["resources"][resource_id]
            key = os.path.normcase(os.path.abspath(fresh.path))
            location = next((row for row in record["locations"]
                if os.path.normcase(os.path.abspath(row["path"])) == key), None)
            _cancel(should_cancel)
            _current(fresh)
            if location is not None and location.get("file_state") == list(fresh.file_state):
                return resource_id  # same location and bytes: no registry publication
            if location is None:
                record["locations"].append({"path": str(fresh.path), "file_state": list(fresh.file_state)})
            else:
                location["file_state"] = list(fresh.file_state)
            self._save(value, token)
        return resource_id

    def register_managed_pack(self, inspection, *, expected_registry, protected_roots=(), progress=None, should_cancel=None):
        from .managed_model_identity import inspect_managed_pack, canonical_pack_identity
        from .ai_interrogation import MODEL_ACQUISITIONS, WD_TAGGER_MODELS, PLAIN_LANGUAGE_MODELS
        fresh = inspect_managed_pack(inspection.path, inspection.identity["model_id"],
            protected_roots=protected_roots, progress=progress, should_cancel=should_cancel)
        if fresh != inspection:
            raise ModelResourceError("The managed pack changed after preview; inspect it again.")
        model_id = fresh.identity["model_id"]
        role = "tagger" if model_id in WD_TAGGER_MODELS else "captioner"
        display = {**WD_TAGGER_MODELS, **PLAIN_LANGUAGE_MODELS}[model_id][0]
        family = "WD v3" if role == "tagger" else display
        with self._writer():
            value, current = self.snapshot()
            if current != expected_registry:
                raise ModelResourceError("The model registry changed after preview; reload and retry.")
            record = value["resources"].setdefault(fresh.resource_id, {
                "identity": fresh.stored_identity(), "locations": [], "declarations": [], "lineage": []})
            if canonical_pack_identity(record["identity"]) != canonical_pack_identity(fresh.stored_identity()):
                raise ModelResourceError("The managed pack identity changed; review the existing record.")
            location = next((deepcopy(r) for r in record["locations"] if r["path"].casefold() == str(fresh.path).casefold()), {})
            location.update(path=str(fresh.path), file_state=list(fresh.file_state))
            record["locations"] = [r for r in record["locations"] if r["path"].casefold() != str(fresh.path).casefold()] + [location]
            declaration = {"role": role, "family": family, "version": fresh.identity["revision"],
                "source": MODEL_ACQUISITIONS[model_id].source_url, "authority": "installed_component", "compatibility": "unverified"}
            if declaration not in record["declarations"]:
                record["declarations"].append(declaration)
            record.setdefault("user", {}).setdefault("display_name", display)
            # Only explicit registration upgrades the store. Unknown fields and
            # old identities survive the same stale-guarded recovery transaction.
            value["schema_version"] = 2
            _cancel(should_cancel)
            _current(fresh)
            self._save(value, current)
        return fresh.resource_id

    def resolve(self, reference: str | GenerationResource, *, role: str = "", family: str = "", progress=None, should_cancel=None, location_path=None) -> ResourceResolution:
        if not isinstance(reference, str):
            from .annotation import GenerationResource
        if not isinstance(reference, str) and isinstance(reference, GenerationResource):
            raw_hash = reference.hashes.get("sha256", "")
            raw_hash = raw_hash.lower() if isinstance(raw_hash, str) else ""
            resource_id = "sha256:" + raw_hash if re.fullmatch(r"[0-9a-f]{64}", raw_hash) else ""
        else:
            resource_id = reference
        from .managed_model_identity import PACK_ID, inspect_managed_pack, canonical_pack_identity
        pack = isinstance(resource_id, str) and bool(PACK_ID.fullmatch(resource_id))
        if not isinstance(resource_id, str) or not (_ID.fullmatch(resource_id) or pack):
            return ResourceResolution("unresolved", reason="An exact SHA-256 identity is required; names and short hashes remain evidence.")
        value, _ = self.snapshot()
        record = value["resources"].get(resource_id)
        if record is None:
            return ResourceResolution("unregistered", resource_id, reason="Register the explicitly selected local file first.")
        if (role or family) and not any((not role or d.get("role") == role) and (not family or d.get("family", "").casefold() == family.casefold()) for d in record["declarations"]):
            return ResourceResolution("declaration_mismatch", resource_id, reason="The recorded role/family does not match this request.")
        issues = []
        for location in record["locations"]:
            if location_path is not None and Path(location["path"]) != Path(location_path):
                continue
            _cancel(should_cancel)
            try:
                if pack:
                    inspection = inspect_managed_pack(location["path"], record["identity"]["model_id"], progress=progress, should_cancel=should_cancel)
                else:
                    inspection = inspect_model_resource(location["path"], progress=progress, should_cancel=should_cancel)
            except (OSError, ModelResourceError, RuntimeError) as exc:
                if isinstance(exc, InterruptedError):
                    raise
                issues.append("A registered location is missing, changed or unsupported.")
                continue
            matches = (canonical_pack_identity(inspection.stored_identity()) == canonical_pack_identity(record["identity"])) if pack else inspection.stored_identity() == record["identity"]
            if inspection.resource_id == resource_id and matches:
                return ResourceResolution("identity_verified", resource_id, inspection, "Exact bytes verified; adapter compatibility is still unverified.")
            issues.append("A registered file no longer matches its content identity.")
        return ResourceResolution("unavailable", resource_id, reason=" ".join(dict.fromkeys(issues)) or "No local location is registered.")

    def register_training_result(self, inspection: ResourceInspection, *, package_path: str | Path,
                                 expected_package_fingerprint: str, expected_registry: str,
                                 parent_ids: tuple[str, ...], trainer_id: str, trainer_version: str,
                                 settings: dict, role: str, family: str, progress=None, should_cancel=None) -> str:
        """Record an explicitly supplied output and attributed training claim.

        The package and output are byte-verified. This API does not assert that
        training occurred or its outcome is good; future job receipts own that.
        """
        from .training_package import TrainingPackageService
        verification = TrainingPackageService().verify(package_path, should_cancel=should_cancel)
        if not verification.valid or verification.package_fingerprint != expected_package_fingerprint:
            raise ModelResourceError("The training package changed or failed verification.")
        if not isinstance(parent_ids, tuple) or not parent_ids or len(parent_ids) > 64 or any(not isinstance(p, str) or not _ID.fullmatch(p) for p in parent_ids) or len(set(parent_ids)) != len(parent_ids) or inspection.resource_id in parent_ids:
            raise ModelResourceError("Derived resource parents must be distinct and cannot include the output.")
        base = verification.manifest["training_configuration"]["target"]["base_model_id"]
        if not isinstance(base, str) or not _ID.fullmatch(base):
            raise ModelResourceError("Rebuild the package with a verified sha256: base identity before registering a result.")
        if base not in parent_ids or any(not isinstance(v, str) or not v.strip() or len(v) > 160 for v in (trainer_id, trainer_version, family)) or not isinstance(settings, dict):
            raise ModelResourceError("Record the exact package base, trainer version and settings.")
        try:
            settings_bytes = _bytes(settings)
        except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
            raise ModelResourceError("Training settings must be bounded JSON values.") from exc
        if len(settings_bytes) > 128 * 1024:
            raise ModelResourceError("Training settings exceed the supported size limit.")
        # Freeze the caller's settings before potentially long parent hashing.
        # The stored settings and hash must describe that same accepted snapshot.
        settings_snapshot = _json(settings_bytes)
        parents = []
        for parent in parent_ids:
            resolved = self.resolve(parent, progress=progress, should_cancel=should_cancel)
            if resolved.status != "identity_verified":
                raise ModelResourceError("A parent resource is unavailable or changed.")
            parents.append(resolved.inspection)
        lineage = {"schema_version": 1, "operation": "training", "parent_ids": list(parent_ids),
                   "package_fingerprint": verification.package_fingerprint,
                   "plan_fingerprint": verification.manifest["candidate_plan"]["plan_fingerprint"],
                   "training_configuration": deepcopy(verification.manifest["training_configuration"]),
                   "trainer_id": trainer_id, "trainer_version": trainer_version,
                   "settings": settings_snapshot, "settings_sha256": _digest(settings_bytes),
                   "output_id": inspection.resource_id, "authority": "user_attributed",
                   "execution_receipt": "unavailable", "quality": "unmeasured"}
        if len(_bytes(lineage)) > 256 * 1024:
            raise ModelResourceError("The resource lineage record exceeds the supported size limit.")
        # Register identity and provenance together; never leave a derived
        # registration partially committed without its lineage.
        fresh = inspect_model_resource(inspection.path, progress=progress, should_cancel=should_cancel)
        if fresh != inspection or role not in RESOURCE_ROLES or not family.strip():
            raise ModelResourceError("The output changed or its declared role/family is missing.")
        with self._writer():
            value, current = self.snapshot()
            if current != expected_registry:
                raise ModelResourceError("The model registry changed after preview; reload and retry.")
            if fresh.resource_id in value["resources"]:
                raise ModelResourceError("This output identity is already registered; existing provenance was preserved.")
            value["resources"][fresh.resource_id] = {
                "identity": fresh.stored_identity(),
                "locations": [{"path": str(fresh.path), "file_state": list(fresh.file_state)}],
                "declarations": [{"role": role, "family": family, "source": "", "license_note": "",
                                  "authority": "user_declared", "compatibility": "unverified"}],
                "lineage": [lineage],
            }
            _cancel(should_cancel)
            for parent in parents:
                _current(parent)
            _current(fresh)
            self._save(value, current)
        return fresh.resource_id
