"""Durable, explicitly selected local-training locations and safe prefills.

This module records locations that the user selected through Setup.  Resolving a
record rereads the selected inventories and tokenizer files, but it never starts
native code, discovers other installations, or claims current execution
readiness.
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
from uuid import uuid4

from .external_write_transaction import atomic_verified_bytes
from .trainer_environment import TrainerEnvironmentService, TrainerProfile
from .training_file_inventory import SoftwareInventory, SoftwareInventoryService, _check_path, canonical
from .training_source_io import _assert_unredirected, _identity
from .training_tokenizer_files import files_identity, read_slot


SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 256 * 1024
MAX_RECOVERY_RECORDS = 1000
SHA = re.compile(r"[0-9a-f]{64}\Z")
GENERATION = re.compile(r"[0-9a-f]{32}\Z")
TOKENIZER_IDENTITY = re.compile(r"clip-files-v1:[0-9a-f]{64}\Z")
RUN_NAME = re.compile(r'[^\\/:*?"<>|\x00-\x1f]+\Z')
RESERVED_NAME = re.compile(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?\Z', re.IGNORECASE)
ABSENT_REVISION = hashlib.sha256(b"<absent>").hexdigest()
_MODES = {"managed", "existing"}


class TrainingSetupSelectionError(RuntimeError):
    pass


class TrainingSetupSelectionRecoveryRequired(TrainingSetupSelectionError):
    pass


def _digest(raw: bytes | None) -> str:
    return hashlib.sha256(raw if raw is not None else b"<absent>").hexdigest()


def _path(value: str, label: str) -> Path:
    if (type(value) is not str or not value or len(value) > 32767 or "\0" in value
            or not Path(value).is_absolute()):
        raise TrainingSetupSelectionError(f"Select an absolute {label} location.")
    return Path(value).absolute()


def _sha(value: str, label: str) -> None:
    if type(value) is not str or SHA.fullmatch(value) is None:
        raise TrainingSetupSelectionError(f"The selected {label} identity is invalid.")


def _canonical(value) -> bytes:
    try:
        raw = canonical(value)
    except (TypeError, ValueError) as exc:
        raise TrainingSetupSelectionError("The training Setup selection is not serializable.") from exc
    if len(raw) > MAX_RECORD_BYTES:
        raise TrainingSetupSelectionError("The training Setup selection exceeds its size bound.")
    return raw


def _decode(raw: bytes):
    if len(raw) > MAX_RECORD_BYTES:
        raise TrainingSetupSelectionError("The training Setup selection exceeds its size bound.")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise TrainingSetupSelectionError("The training Setup selection repeats a field.")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)))
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, TrainingSetupSelectionError):
            raise
        raise TrainingSetupSelectionError("The training Setup selection is invalid JSON.") from exc
    if _canonical(value) != raw:
        raise TrainingSetupSelectionError("The training Setup selection is not canonical.")
    return value


def _read_optional(path: Path) -> bytes | None:
    _assert_unredirected(path)
    try:
        before = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_RECORD_BYTES:
        raise TrainingSetupSelectionError("The training Setup record is not a bounded regular file.")
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0)) | int(getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(before):
            raise TrainingSetupSelectionError("The training Setup record changed before reading.")
        raw = os.read(descriptor, MAX_RECORD_BYTES + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    _assert_unredirected(path)
    if (len(raw) != before.st_size or _identity(opened) != _identity(after)
            or _identity(path.stat(follow_symlinks=False)) != _identity(before)):
        raise TrainingSetupSelectionError("The training Setup record changed while reading.")
    return raw


def _write_new(path: Path, raw: bytes, label: str) -> None:
    if len(raw) > MAX_RECORD_BYTES:
        raise TrainingSetupSelectionError(f"{label} exceeds its size bound.")
    _assert_unredirected(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_unredirected(path.parent)
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    if _read_optional(path) != raw:
        raise TrainingSetupSelectionError(f"{label} failed exact read-back verification.")


def _directory_identity(path: Path) -> tuple[int, ...]:
    try:
        # Child creation changes a directory timestamp.  A reusable output
        # preference binds the directory object, not its ordinary contents.
        return tuple(_check_path(path, directory=True)[:2])
    except (OSError, RuntimeError, ValueError) as exc:
        raise TrainingSetupSelectionError(
            "The selected training output folder is missing, redirected, or unavailable."
        ) from exc


def _tokenizer_identities(parent: Path) -> tuple[str, str]:
    try:
        return tuple(files_identity(read_slot(parent / name)) for name in ("tokenizer", "tokenizer_2"))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise TrainingSetupSelectionError(
            "The selected original SDXL tokenizer files are missing, changed, or invalid."
        ) from exc


def _disjoint(roots: tuple[Path, ...]) -> None:
    resolved = tuple(path.absolute() for path in roots)
    for index, first in enumerate(resolved):
        for second in resolved[index + 1:]:
            if first == second or first.is_relative_to(second) or second.is_relative_to(first):
                raise TrainingSetupSelectionError(
                    "Trainer software, tokenizers, and output must use separate selected folders."
                )


@dataclass(frozen=True)
class SoftwareInventoryBinding:
    root: str
    manifest: str
    inventory_sha256: str

    def validate(self) -> None:
        root = _path(self.root, "software inventory")
        manifest = _path(self.manifest, "software inventory manifest")
        _sha(self.inventory_sha256, "software inventory")
        if manifest == root or manifest.is_relative_to(root):
            raise TrainingSetupSelectionError(
                "Keep a reviewed software inventory outside the selected software directory."
            )

    def payload(self) -> dict:
        self.validate()
        return {"root": self.root, "manifest": self.manifest,
            "inventory_sha256": self.inventory_sha256}

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != {"root", "manifest", "inventory_sha256"}:
            raise TrainingSetupSelectionError("A reviewed software inventory binding is invalid.")
        result = cls(**value)
        result.validate()
        return result


@dataclass(frozen=True)
class ManagedEnvironmentSelection:
    root: str
    generation_id: str

    def validate(self) -> None:
        _path(self.root, "managed trainer environment")
        if type(self.generation_id) is not str or GENERATION.fullmatch(self.generation_id) is None:
            raise TrainingSetupSelectionError("The selected managed trainer generation is invalid.")

    def payload(self) -> dict:
        self.validate()
        return {"root": self.root, "generation_id": self.generation_id}


@dataclass(frozen=True)
class ExistingEnvironmentSelection:
    source: SoftwareInventoryBinding
    environment: SoftwareInventoryBinding
    python_base: SoftwareInventoryBinding
    python: str

    def validate(self) -> None:
        if any(type(item) is not SoftwareInventoryBinding for item in
               (self.source, self.environment, self.python_base)):
            raise TrainingSetupSelectionError("The existing trainer inventory bindings are invalid.")
        for item in (self.source, self.environment, self.python_base):
            item.validate()
        _path(self.python, "Python executable")
        roots = tuple(_path(item.root, "software inventory") for item in
                      (self.source, self.environment, self.python_base))
        _disjoint(roots)
        manifests = tuple(_path(item.manifest, "software inventory manifest") for item in
                          (self.source, self.environment, self.python_base))
        if any(manifest == root or manifest.is_relative_to(root)
               for manifest in manifests for root in roots):
            raise TrainingSetupSelectionError(
                "Keep reviewed inventory manifests outside every selected software directory."
            )

    def payload(self) -> dict:
        self.validate()
        return {"source": self.source.payload(), "environment": self.environment.payload(),
            "python_base": self.python_base.payload(), "python": self.python}


@dataclass(frozen=True)
class TrainingSetupSelection:
    mode: str
    profile_sha256: str
    managed: ManagedEnvironmentSelection | None
    existing: ExistingEnvironmentSelection | None
    tokenizers_root: str
    tokenizer_identities: tuple[str, str]
    output_root: str
    output_root_identity: tuple[int, ...]

    def validate(self) -> None:
        if self.mode not in _MODES or type(self.mode) is not str:
            raise TrainingSetupSelectionError("The selected trainer environment mode is invalid.")
        _sha(self.profile_sha256, "trainer profile")
        if ((self.mode == "managed") != (type(self.managed) is ManagedEnvironmentSelection)
                or (self.mode == "existing") != (type(self.existing) is ExistingEnvironmentSelection)
                or (self.managed is None) == (self.existing is None)):
            raise TrainingSetupSelectionError("Choose exactly one managed or existing trainer environment.")
        if self.managed is not None:
            self.managed.validate()
        if self.existing is not None:
            self.existing.validate()
        _path(self.tokenizers_root, "tokenizer parent")
        _path(self.output_root, "training output")
        if (type(self.tokenizer_identities) is not tuple or len(self.tokenizer_identities) != 2
                or any(type(value) is not str or TOKENIZER_IDENTITY.fullmatch(value) is None
                       for value in self.tokenizer_identities)):
            raise TrainingSetupSelectionError("The selected original tokenizer identities are invalid.")
        if (type(self.output_root_identity) is not tuple or len(self.output_root_identity) != 2
                or any(type(value) is not int or value < 0 for value in self.output_root_identity)):
            raise TrainingSetupSelectionError("The selected training output identity is invalid.")

    def payload(self) -> dict:
        self.validate()
        return {"schema_version": SCHEMA_VERSION, "kind": "training_setup_selection",
            "mode": self.mode, "profile_sha256": self.profile_sha256,
            "managed": self.managed.payload() if self.managed is not None else None,
            "existing": self.existing.payload() if self.existing is not None else None,
            "tokenizers": {"root": self.tokenizers_root,
                "slots": [{"role": 1, "identity": self.tokenizer_identities[0]},
                          {"role": 2, "identity": self.tokenizer_identities[1]}]},
            "output": {"root": self.output_root,
                "root_identity": list(self.output_root_identity)}}

    @classmethod
    def from_dict(cls, value):
        fields = {"schema_version", "kind", "mode", "profile_sha256", "managed",
                  "existing", "tokenizers", "output"}
        if (type(value) is not dict or set(value) != fields
                or type(value["schema_version"]) is not int
                or value["schema_version"] != SCHEMA_VERSION
                or value["kind"] != "training_setup_selection"):
            raise TrainingSetupSelectionError("The training Setup selection schema is invalid.")
        managed = value["managed"]
        if managed is not None:
            if type(managed) is not dict or set(managed) != {"root", "generation_id"}:
                raise TrainingSetupSelectionError("The managed trainer selection is invalid.")
            managed = ManagedEnvironmentSelection(**managed)
        existing = value["existing"]
        if existing is not None:
            if type(existing) is not dict or set(existing) != {"source", "environment", "python_base", "python"}:
                raise TrainingSetupSelectionError("The existing trainer selection is invalid.")
            existing = ExistingEnvironmentSelection(
                SoftwareInventoryBinding.from_dict(existing["source"]),
                SoftwareInventoryBinding.from_dict(existing["environment"]),
                SoftwareInventoryBinding.from_dict(existing["python_base"]), existing["python"])
        tokenizers, output = value["tokenizers"], value["output"]
        if (type(tokenizers) is not dict or set(tokenizers) != {"root", "slots"}
                or type(tokenizers["slots"]) is not list or len(tokenizers["slots"]) != 2
                or any(type(row) is not dict or set(row) != {"role", "identity"}
                       for row in tokenizers["slots"])
                or [row["role"] for row in tokenizers["slots"]] != [1, 2]
                or type(output) is not dict or set(output) != {"root", "root_identity"}
                or type(output["root_identity"]) is not list):
            raise TrainingSetupSelectionError("The tokenizer or output selection is invalid.")
        result = cls(value["mode"], value["profile_sha256"], managed, existing,
            tokenizers["root"], tuple(row["identity"] for row in tokenizers["slots"]),
            output["root"], tuple(output["root_identity"]))
        result.validate()
        return result


@dataclass(frozen=True)
class ResolvedTrainingSetup:
    selection_revision: str
    source_root: str
    environment_root: str
    python_base_root: str
    python: str
    tokenizers_root: str
    output_root: str
    run_name: str
    checkpoint_interval: int = 0

    def request(self) -> dict:
        return {name: getattr(self, name) for name in ("source_root", "environment_root",
            "python_base_root", "python", "tokenizers_root", "output_root", "run_name",
            "checkpoint_interval")}


def _verified_profile(profile) -> TrainerProfile:
    if not isinstance(profile, TrainerProfile):
        raise TrainingSetupSelectionError("A reviewed trainer profile is required.")
    try:
        current = TrainerProfile.from_dict(profile.payload())
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise TrainingSetupSelectionError("The reviewed trainer profile is invalid.") from exc
    if current.sha256 != profile.sha256:
        raise TrainingSetupSelectionError("The reviewed trainer profile changed.")
    return current


def _verify_binding(binding: SoftwareInventoryBinding, *, should_cancel=None, progress=None) -> SoftwareInventory:
    binding.validate()
    service = SoftwareInventoryService()
    try:
        expected = service.load(binding.root, binding.manifest)
        if expected.fingerprint != binding.inventory_sha256:
            raise TrainingSetupSelectionError("A reviewed software inventory identity changed.")
        actual = service.capture(binding.root, should_cancel=should_cancel, progress=progress)
    except InterruptedError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        if isinstance(exc, TrainingSetupSelectionError):
            raise
        raise TrainingSetupSelectionError("The selected software inventory could not be verified.") from exc
    if actual != expected:
        raise TrainingSetupSelectionError("Selected software bytes or directory membership changed.")
    return actual


def _interpreter_member(python: Path, inventories: tuple[SoftwareInventory, ...]) -> bool:
    for inventory in inventories:
        root = Path(inventory.root)
        try:
            relative = python.relative_to(root).as_posix()
        except ValueError:
            continue
        if any(name == relative for name, _state, _digest_value in inventory.files):
            return True
    return False


def _cancel(should_cancel) -> None:
    if should_cancel and should_cancel():
        raise InterruptedError("Training Setup selection verification was cancelled.")


class TrainingSetupSelectionService:
    """Persist and recheck one explicit training-location selection.

    Construction is lexical only.  All filesystem work occurs in the explicit
    prepare, snapshot, save, recover, or resolve methods.
    """

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
                raise TrainingSetupSelectionError(
                    "Another training Setup selection update owns this location."
                ) from exc
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def snapshot(self) -> tuple[TrainingSetupSelection | None, str]:
        if _read_optional(self.pending) is not None:
            raise TrainingSetupSelectionRecoveryRequired(
                "A training Setup selection update needs recovery before it can be used."
            )
        raw = _read_optional(self.path)
        if raw is None:
            return None, ABSENT_REVISION
        return TrainingSetupSelection.from_dict(_decode(raw)), _digest(raw)

    def _new_recovery(self) -> tuple[str, Path]:
        _assert_unredirected(self.recovery_root)
        self.recovery_root.mkdir(parents=True, exist_ok=True)
        try:
            _check_path(self.recovery_root, directory=True)
            count = 0
            with os.scandir(self.recovery_root) as entries:
                for entry in entries:
                    if count >= MAX_RECOVERY_RECORDS or GENERATION.fullmatch(entry.name) is None:
                        raise TrainingSetupSelectionError(
                            "Training Setup recovery history is full or contains an unexpected entry."
                        )
                    _check_path(Path(entry.path), directory=True)
                    count += 1
        except TrainingSetupSelectionError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise TrainingSetupSelectionError(
                "Training Setup recovery history is unavailable."
            ) from exc
        if count >= MAX_RECOVERY_RECORDS:
            raise TrainingSetupSelectionError("Training Setup recovery history is full.")
        operation = uuid4().hex
        recovery = self.recovery_root / operation
        _assert_unredirected(recovery)
        recovery.mkdir(exist_ok=False)
        return operation, recovery

    def save(self, selection: TrainingSetupSelection, expected_revision: str) -> str:
        if not isinstance(selection, TrainingSetupSelection):
            raise TrainingSetupSelectionError("Save a typed training Setup selection.")
        selection.validate()
        _sha(expected_revision, "previous selection")
        after = _canonical(selection.payload())
        with self._locked():
            if _read_optional(self.pending) is not None:
                raise TrainingSetupSelectionRecoveryRequired(
                    "A training Setup selection update needs recovery before another save."
                )
            before = _read_optional(self.path)
            if _digest(before) != expected_revision:
                raise TrainingSetupSelectionError(
                    "The training Setup selection changed after review. Reload it and save again."
                )
            if before == after:
                return _digest(after)
            operation, recovery = self._new_recovery()
            before_raw = before or b""
            receipt = {"schema_version": SCHEMA_VERSION,
                "kind": "training_setup_selection_recovery", "operation_id": operation,
                "target": str(self.path), "before_absent": before is None,
                "before_sha256": _digest(before), "after_sha256": _digest(after)}
            _write_new(recovery / "before.bin", before_raw, "Training Setup recovery backup")
            _write_new(recovery / "after.json", after, "Training Setup recovery proposal")
            receipt_raw = _canonical(receipt)
            _write_new(recovery / "receipt.json", receipt_raw, "Training Setup recovery receipt")
            atomic_verified_bytes(self.pending, receipt_raw,
                label="Training Setup pending recovery")
            _assert_unredirected(self.path)
            atomic_verified_bytes(self.path, after, label="Training Setup selection")
            if _read_optional(self.path) != after:
                raise TrainingSetupSelectionError("The training Setup selection failed read-back verification.")
            self._finish_pending(receipt_raw)
            return _digest(after)

    def _finish_pending(self, expected: bytes) -> None:
        if _read_optional(self.pending) != expected:
            raise TrainingSetupSelectionRecoveryRequired(
                "The training Setup pending recovery record changed."
            )
        _assert_unredirected(self.pending)
        self.pending.unlink()

    def recover(self) -> str:
        with self._locked():
            pending_raw = _read_optional(self.pending)
            if pending_raw is None:
                return self.snapshot()[1]
            receipt = _decode(pending_raw)
            fields = {"schema_version", "kind", "operation_id", "target", "before_absent",
                      "before_sha256", "after_sha256"}
            if (type(receipt) is not dict or set(receipt) != fields
                    or type(receipt["schema_version"]) is not int
                    or receipt["schema_version"] != SCHEMA_VERSION
                    or receipt["kind"] != "training_setup_selection_recovery"
                    or type(receipt["operation_id"]) is not str
                    or GENERATION.fullmatch(receipt["operation_id"]) is None
                    or receipt["target"] != str(self.path)
                    or type(receipt["before_absent"]) is not bool):
                raise TrainingSetupSelectionRecoveryRequired(
                    "The training Setup recovery receipt is invalid."
                )
            _sha(receipt["before_sha256"], "recovery backup")
            _sha(receipt["after_sha256"], "recovery proposal")
            recovery = self.recovery_root / receipt["operation_id"]
            saved_receipt = _read_optional(recovery / "receipt.json")
            before = _read_optional(recovery / "before.bin")
            after = _read_optional(recovery / "after.json")
            if (saved_receipt != pending_raw or before is None or after is None
                    or _digest(after) != receipt["after_sha256"]
                    or _digest(None if receipt["before_absent"] else before) != receipt["before_sha256"]
                    or (receipt["before_absent"] and before != b"")):
                raise TrainingSetupSelectionRecoveryRequired(
                    "The retained training Setup recovery evidence changed."
                )
            TrainingSetupSelection.from_dict(_decode(after))
            current = _read_optional(self.path)
            if _digest(current) == receipt["before_sha256"]:
                _assert_unredirected(self.path)
                atomic_verified_bytes(self.path, after, label="Recovered training Setup selection")
                current = _read_optional(self.path)
            if _digest(current) != receipt["after_sha256"]:
                raise TrainingSetupSelectionRecoveryRequired(
                    "The training Setup selection differs from both retained recovery states."
                )
            self._finish_pending(pending_raw)
            return receipt["after_sha256"]

    def prepare_managed(self, environment: TrainerEnvironmentService, profile: TrainerProfile,
                        generation_id: str, tokenizers_root, output_root, *, should_cancel=None):
        profile = _verified_profile(profile)
        if not isinstance(environment, TrainerEnvironmentService):
            raise TrainingSetupSelectionError("A managed trainer environment service is required.")
        if environment.profile.sha256 != profile.sha256:
            raise TrainingSetupSelectionError("The managed trainer profile changed.")
        _cancel(should_cancel)
        try:
            generation = environment.verify_generation(generation_id, cancel=should_cancel)
        except InterruptedError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise TrainingSetupSelectionError("The selected managed trainer generation is unavailable or changed.") from exc
        if generation.profile_sha256 != profile.sha256:
            raise TrainingSetupSelectionError("The selected managed trainer generation uses another profile.")
        software = generation.software_root
        executable = profile.payload()["python"]["executable"]
        if (software != Path(environment.root) / "generations" / generation_id / "software"
                or executable != "python/python.exe"):
            raise TrainingSetupSelectionError("The managed trainer does not use the reviewed fixed runtime layout.")
        token_root, output = _path(str(tokenizers_root), "tokenizer parent"), _path(str(output_root), "training output")
        _disjoint((software / "source", software / "environment", software / "python",
                   token_root, output))
        identities = _tokenizer_identities(token_root)
        output_identity = _directory_identity(output)
        result = TrainingSetupSelection("managed", profile.sha256,
            ManagedEnvironmentSelection(str(environment.root), generation_id), None,
            str(token_root), identities, str(output), output_identity)
        result.validate()
        return result

    def prepare_existing(self, profile: TrainerProfile, source: SoftwareInventoryBinding,
                         environment: SoftwareInventoryBinding, python_base: SoftwareInventoryBinding,
                         python, tokenizers_root, output_root, *, should_cancel=None, progress=None):
        profile = _verified_profile(profile)
        existing = ExistingEnvironmentSelection(source, environment, python_base, str(Path(python).absolute()))
        existing.validate()
        token_root, output = _path(str(tokenizers_root), "tokenizer parent"), _path(str(output_root), "training output")
        roots = tuple(Path(item.root) for item in (source, environment, python_base))
        _disjoint((*roots, token_root, output))
        manifests = tuple(Path(item.manifest) for item in (source, environment, python_base))
        if any(manifest == root or manifest.is_relative_to(root)
               for manifest in manifests for root in (token_root, output)):
            raise TrainingSetupSelectionError(
                "Keep reviewed inventory manifests outside tokenizer and output folders."
            )
        inventories = tuple(_verify_binding(item, should_cancel=should_cancel, progress=progress)
                            for item in (source, environment, python_base))
        python_path = _path(existing.python, "Python executable")
        if not _interpreter_member(python_path, inventories[1:]):
            raise TrainingSetupSelectionError(
                "The selected Python executable is not a member of the reviewed environment or Python-base inventory."
            )
        identities = _tokenizer_identities(token_root)
        output_identity = _directory_identity(output)
        result = TrainingSetupSelection("existing", profile.sha256, None, existing,
            str(token_root), identities, str(output), output_identity)
        result.validate()
        return result

    def resolve(self, profile: TrainerProfile, run_name: str, *, managed_environment=None,
                checkpoint_interval: int = 0, should_cancel=None, progress=None) -> ResolvedTrainingSetup:
        selection, revision = self.snapshot()
        if selection is None:
            raise TrainingSetupSelectionError("Choose a trainer environment and local resources in Setup.")
        profile = _verified_profile(profile)
        if profile.sha256 != selection.profile_sha256:
            raise TrainingSetupSelectionError("The selected trainer profile changed. Review Setup again.")
        if (type(run_name) is not str or not run_name or len(run_name) > 80
                or RUN_NAME.fullmatch(run_name) is None or run_name in {".", ".."}
                or run_name.endswith((" ", ".")) or RESERVED_NAME.fullmatch(run_name) is not None
                or type(checkpoint_interval) is not int or not 0 <= checkpoint_interval <= 1_000_000_000):
            raise TrainingSetupSelectionError("Choose a valid new run name and checkpoint interval.")
        _cancel(should_cancel)
        token_root, output = Path(selection.tokenizers_root), Path(selection.output_root)
        if _tokenizer_identities(token_root) != selection.tokenizer_identities:
            raise TrainingSetupSelectionError("The selected original tokenizer identities changed.")
        if _directory_identity(output) != selection.output_root_identity:
            raise TrainingSetupSelectionError("The selected training output location changed.")
        if selection.mode == "managed":
            if not isinstance(managed_environment, TrainerEnvironmentService):
                raise TrainingSetupSelectionError("The selected managed trainer environment is unavailable.")
            managed = selection.managed
            assert managed is not None
            if (Path(managed_environment.root).absolute() != Path(managed.root)
                    or managed_environment.profile.sha256 != profile.sha256):
                raise TrainingSetupSelectionError("The selected managed trainer environment or profile changed.")
            try:
                generation = managed_environment.verify_generation(managed.generation_id, cancel=should_cancel)
            except InterruptedError:
                raise
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                raise TrainingSetupSelectionError("The selected managed trainer generation is unavailable or changed.") from exc
            expected = Path(managed.root) / "generations" / managed.generation_id / "software"
            if generation.software_root != expected or generation.profile_sha256 != profile.sha256:
                raise TrainingSetupSelectionError("The selected managed trainer generation changed.")
            values = profile.payload()
            executable = values["python"]["executable"]
            if executable != "python/python.exe":
                raise TrainingSetupSelectionError("The managed trainer does not use the reviewed fixed runtime layout.")
            source, environment, python_base = (expected / name for name in ("source", "environment", "python"))
            for path in (source, environment, python_base):
                try:
                    _check_path(path, directory=True)
                except (OSError, RuntimeError, ValueError) as exc:
                    raise TrainingSetupSelectionError("The managed trainer fixed runtime layout is incomplete.") from exc
            python = expected / executable
            roots = (source, environment, python_base)
        else:
            existing = selection.existing
            assert existing is not None
            inventories = tuple(_verify_binding(item, should_cancel=should_cancel, progress=progress)
                                for item in (existing.source, existing.environment, existing.python_base))
            python = Path(existing.python)
            if not _interpreter_member(python, inventories[1:]):
                raise TrainingSetupSelectionError(
                    "The selected Python executable is no longer in its reviewed software inventory."
                )
            source, environment, python_base = (Path(item.root) for item in
                (existing.source, existing.environment, existing.python_base))
            roots = (source, environment, python_base)
        _disjoint((*roots, token_root, output))
        _cancel(should_cancel)
        with self._locked():
            if self.snapshot()[1] != revision:
                raise TrainingSetupSelectionError(
                    "The training Setup selection changed during verification. Review Setup again."
                )
            return ResolvedTrainingSetup(revision, str(source), str(environment), str(python_base),
                str(python), str(token_root), str(output), run_name, checkpoint_interval)


def inventory_binding(root, manifest, *, should_cancel=None, progress=None) -> SoftwareInventoryBinding:
    """Recheck a saved inventory and return its exact typed binding."""
    root, manifest = Path(root).absolute(), Path(manifest).absolute()
    service = SoftwareInventoryService()
    try:
        expected = service.load(root, manifest)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise TrainingSetupSelectionError("The reviewed software inventory is unavailable or invalid.") from exc
    binding = SoftwareInventoryBinding(str(root), str(manifest), expected.fingerprint)
    _verify_binding(binding, should_cancel=should_cancel, progress=progress)
    return binding
