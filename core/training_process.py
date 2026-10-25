"""Internal, synthetic-qualified trainer process ownership and recovery.

No application entry point submits real trainers here. A process exit is never
a verified training result. Runtime/adapter qualification and launch UI authority
must be implemented separately before using this primitive with a real trainer.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from uuid import uuid4

from .background_jobs import BackgroundJobManager
from .training_source_io import _assert_unredirected
from .training_windows_file_identity import checked_path_identity, descriptor_identity
from .training_process_windows import WindowsTrainingJob, is_local_volume, windows_directory
from .training_file_inventory import FileInventoryError, SoftwareInventoryService, inspection_control
from .training_verification import verify_owned_inputs, verified_receipt_hashes


MAX_RECORD = 256 * 1024
MAX_EVENTS = 32
LEGACY_ENVIRONMENT = "diagnostic-offline-v1"
LOCAL_ENVIRONMENT = "local-processing-v1"
VERIFICATION_SECONDS = 180
PENDING_NAME = ".trainer-active.json"
TERMINAL = {"exited_unverified", "cancelled", "failed", "interrupted"}
TRANSITIONS = {"": {"reserved"}, "reserved": {"starting", "failed", "cancelled", "interrupted"},
    "starting": {"running", "stopping", "failed", "interrupted"},
    "running": {"stopping", "exited_unverified", "failed", "interrupted"},
    "stopping": {"cancelled", "failed", "interrupted"}}


class TrainingProcessError(RuntimeError):
    pass


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _failure_message(exc):
    if isinstance(exc, (TrainingProcessError, FileInventoryError)):
        return str(exc)
    if isinstance(exc, InterruptedError):
        return "The process request was cancelled."
    if isinstance(exc, OSError):
        return f"Process I/O failed (OS error {getattr(exc, 'winerror', None) or exc.errno or 'unavailable'}); retain evidence for recovery."
    return "The process owner failed; retain evidence and unverified outputs for recovery."


def _cancel(callback):
    if callback and callback():
        raise InterruptedError("The process request was cancelled.")


def _state(path: Path, *, directory=False):
    if os.name == "nt" and not is_local_volume(path.absolute().anchor):
        raise TrainingProcessError("Process inputs and workspaces require an available local volume.")
    _assert_unredirected(path)
    value = path.stat(follow_symlinks=False)
    if not (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)):
        raise TrainingProcessError("The process workspace or input has the wrong file type.")
    if int(getattr(value, "st_file_attributes", 0)) & (0x1000 | 0x40000 | 0x400000):
        raise TrainingProcessError("Process inputs and evidence must be fully local files.")
    return checked_path_identity(path, value)


def _directory(path):
    # Directory size/mtime legitimately changes as owned children are created.
    return _state(Path(path), directory=True)[:2]


def _read(path: Path, limit=MAX_RECORD):
    before = _state(path)
    if before[2] > limit:
        raise TrainingProcessError("Process recovery evidence exceeds its size bound.")
    with path.open("rb") as handle:
        if descriptor_identity(handle.fileno()) != before:
            raise TrainingProcessError("Process evidence changed before reading.")
        raw = handle.read(limit + 1)
        after = descriptor_identity(handle.fileno())
    if after != before or _state(path) != before or len(raw) != before[2]:
        raise TrainingProcessError("Process evidence changed while reading.")
    return raw


def _write_new(path: Path, raw: bytes):
    _assert_unredirected(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
        expected = descriptor_identity(handle.fileno())
    if _state(path) != expected or _read(path) != raw:
        raise TrainingProcessError("Process evidence failed read-back verification; retain it for review.")


@contextmanager
def _lease(root: Path):
    """One owner across app instances sharing this explicit local job root."""
    path = root / ".trainer-owner.lock"
    _assert_unredirected(path)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b") as handle:
        if descriptor_identity(handle.fileno()) != _state(path):
            raise TrainingProcessError("The process-owner lock changed.")
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0"); handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise TrainingProcessError("Another process owner or recovery review is active.") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class ProcessInput:
    path: str
    state: tuple[int, ...]
    sha256: str

    @classmethod
    def capture(cls, path, should_cancel=None):
        path = Path(path).absolute()
        before = _state(path)
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            if descriptor_identity(handle.fileno()) != before:
                raise TrainingProcessError("A process input changed before inspection.")
            while True:
                _cancel(should_cancel)
                block = handle.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
            after = descriptor_identity(handle.fileno())
        if before != after or _state(path) != before:
            raise TrainingProcessError("A process input changed during inspection.")
        return cls(str(path), before, digest.hexdigest())


@dataclass(frozen=True)
class ProcessInventory:
    root: str
    manifest_path: str
    manifest_sha256: str
    file_count: int
    byte_count: int


@dataclass(frozen=True)
class TrainingProcessPlan:
    run_id: str
    root: str
    root_identity: tuple[int, ...]
    name: str
    command: tuple[str, ...]
    inputs: tuple[ProcessInput, ...]
    preview_fingerprint: str
    timeout_seconds: float = 60
    log_limit: int = 1024 * 1024
    schema_version: int = 1
    inventories: tuple[ProcessInventory, ...] = ()
    environment_profile: str = LEGACY_ENVIRONMENT

    @property
    def fingerprint(self):
        value = asdict(self)
        if self.schema_version in {1, 2}:
            value.pop("environment_profile")
        if self.schema_version == 1:
            value.pop("inventories")  # retain legacy plan fingerprint semantics
        return _sha(_bytes(value))

    @property
    def destination(self):
        return Path(self.root) / self.name

    @property
    def owner_name(self):
        return "Local\\CollectiGen.Trainer." + self.run_id

    def validate(self):
        if type(self.schema_version) is not int or self.schema_version not in {1, 2, 3} or not re.fullmatch(r"[0-9a-f]{32}", self.run_id):
            raise TrainingProcessError("Unsupported process plan identity.")
        expected_profile = LOCAL_ENVIRONMENT if self.schema_version == 3 else LEGACY_ENVIRONMENT
        if type(self.environment_profile) is not str or self.environment_profile != expected_profile:
            raise TrainingProcessError("Unsupported process environment profile.")
        if (self.schema_version == 1 and self.inventories) or (self.schema_version == 2 and not 1 <= len(self.inventories) <= 4):
            raise TrainingProcessError("Unsupported software inventory plan.")
        if len(self.inventories) > 4:
            raise TrainingProcessError("Unsupported software inventory plan.")
        selected = {item.path: item for item in self.inputs}
        roots = set()
        for inventory in self.inventories:
            item = selected.get(inventory.manifest_path)
            if not Path(inventory.root).is_absolute() or inventory.root in roots or item is None or item.sha256 != inventory.manifest_sha256 or type(inventory.file_count) is not int or not 0 <= inventory.file_count <= 200_000 or type(inventory.byte_count) is not int or not 0 <= inventory.byte_count <= 64 * 1024 ** 3:
                raise TrainingProcessError("Invalid software inventory binding.")
            if Path(self.root).is_relative_to(Path(inventory.root)):
                raise TrainingProcessError("Process workspaces must stay outside inventoried software.")
            roots.add(inventory.root)
        if not re.fullmatch(r"[0-9a-f]{64}", self.preview_fingerprint):
            raise TrainingProcessError("An exact reviewed preview identity is required.")
        if not self.name or len(self.name) > 80 or any(c in self.name for c in '\\/:*?"<>|\0') or self.name in {".", ".."} or self.name[-1] in {".", " "}:
            raise TrainingProcessError("Choose a single unused process-workspace name.")
        if any(ord(c) < 32 for c in self.name) or self.name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}:
            raise TrainingProcessError("The process-workspace name is reserved by Windows.")
        if not Path(self.root).is_absolute() or Path(self.root).drive.startswith("\\"):
            raise TrainingProcessError("A local absolute process-workspace root is required.")
        if not self.command or len(self.command) > 128 or any(not isinstance(arg, str) or "\0" in arg for arg in self.command):
            raise TrainingProcessError("A bounded explicit argument list is required.")
        if not Path(self.command[0]).is_absolute() or sum(len(arg) + 3 for arg in self.command) > 30000:
            raise TrainingProcessError("An exact executable path and bounded command are required.")
        if not 1 <= len(self.inputs) <= 128 or self.inputs[0].path != self.command[0] or len({item.path for item in self.inputs}) != len(self.inputs):
            raise TrainingProcessError("The executable and selected inputs require unique exact identities.")
        if type(self.timeout_seconds) not in {int, float} or not math.isfinite(self.timeout_seconds) or not 0.1 <= self.timeout_seconds <= 86400:
            raise TrainingProcessError("Choose a finite process deadline up to one day.")
        if type(self.log_limit) is not int or not 1024 <= self.log_limit <= 16 * 1024 * 1024:
            raise TrainingProcessError("Choose a bounded process log capacity.")


@dataclass(frozen=True)
class TrainingProcessResult:
    status: str
    destination: Path
    message: str
    exit_code: int | None = None
    tree_stopped: bool = False
    journal_tip: str = ""


class TrainingRunJournal:
    def __init__(self, destination: Path):
        self.destination = Path(destination).absolute()
        self.identity = _directory(self.destination)
        self.events = self.destination / "events"
        self.event_identity = _directory(self.events)

    def _current(self):
        if _directory(self.destination) != self.identity or _directory(self.events) != self.event_identity:
            raise TrainingProcessError("The process workspace changed; recovery remains held.")

    def read(self):
        self._current()
        paths = []
        for path in self.events.iterdir():
            if len(paths) >= MAX_EVENTS or not re.fullmatch(r"\d{3}\.json", path.name):
                raise TrainingProcessError("Unknown or excessive process recovery evidence; leave it unchanged.")
            paths.append(path)
        rows, tip, state = [], "", ""
        for index, path in enumerate(sorted(paths)):
            if path.name != f"{index:03}.json":
                raise TrainingProcessError("Process recovery evidence has a missing event.")
            raw = _read(path)
            try:
                value = json.loads(raw)
                if _bytes(value) != raw or set(value) != {"schema_version", "sequence", "previous", "state", "details"}:
                    raise ValueError()
                if type(value["schema_version"]) is not int or value["schema_version"] != 1 or type(value["sequence"]) is not int or value["sequence"] != index or value["previous"] != tip or value["state"] not in TRANSITIONS.get(state, set()):
                    raise ValueError()
                if not isinstance(value["details"], dict):
                    raise ValueError()
            except (ValueError, TypeError, KeyError) as exc:
                raise TrainingProcessError("Process recovery evidence is invalid or from an unsupported version.") from exc
            state, tip = value["state"], _sha(raw)
            rows.append(value)
        self._current()
        return rows, tip

    def append(self, state: str, details: dict, expected_tip: str):
        rows, tip = self.read()
        previous = rows[-1]["state"] if rows else ""
        if tip != expected_tip or state not in TRANSITIONS.get(previous, set()) or len(rows) >= MAX_EVENTS:
            raise TrainingProcessError("The process state changed; reload its recovery evidence.")
        raw = _bytes({"schema_version": 1, "sequence": len(rows), "previous": tip, "state": state, "details": details})
        if len(raw) > MAX_RECORD:
            raise TrainingProcessError("The process receipt exceeds its size bound.")
        self._current()
        _write_new(self.events / f"{len(rows):03}.json", raw)
        return _sha(raw)


class TrainingProcessService:
    reports_process_invocation = True

    """Caller owns the reviewed plan; coordinator admission lasts until tree death."""
    def __init__(self, *, backend=WindowsTrainingJob):
        self.backend = backend
        self._held = {}
        self._held_lock = threading.RLock()

    def prepare(self, root, name, command, *, inputs=(), inventories=(), preview_fingerprint, timeout_seconds=60, log_limit=1024*1024, should_cancel=None, environment_profile=LEGACY_ENVIRONMENT):
        if type(environment_profile) is not str or environment_profile not in {LEGACY_ENVIRONMENT, LOCAL_ENVIRONMENT}:
            raise TrainingProcessError("Unsupported process environment profile.")
        root = Path(root).absolute()
        command = tuple(str(arg) for arg in command)
        if not command:
            raise TrainingProcessError("An explicit executable is required.")
        command = (str(Path(command[0]).absolute()), *command[1:])
        inventory_pairs = tuple((str(Path(folder).absolute()), str(Path(manifest).absolute())) for folder, manifest in inventories)
        if len(inventory_pairs) > 4:
            raise TrainingProcessError("Too many selected software inventories.")
        from .training_source_loader import VERIFIER_MODULES
        owned_root = Path(__file__).resolve().parents[1]
        verifier_inputs = [owned_root / 'core' / (name + '.py') for name in VERIFIER_MODULES]
        verifier_inputs.append(owned_root / 'tools/training_verification_worker.py')
        paths = tuple(dict.fromkeys((command[0], *(str(Path(path).absolute()) for path in inputs),
            *(manifest for _, manifest in inventory_pairs), *(str(path) for path in verifier_inputs))))
        if len(paths) > 128:
            raise TrainingProcessError("Too many process inputs.")
        captured = tuple(ProcessInput.capture(path, should_cancel) for path in paths)
        selected = {item.path: item for item in captured}
        bindings = []
        for folder, manifest in inventory_pairs:
            inventory = SoftwareInventoryService.load(folder, manifest)
            if ProcessInput.capture(manifest, should_cancel) != selected[manifest]:
                raise TrainingProcessError("The software inventory changed during preparation.")
            bindings.append(ProcessInventory(folder, manifest, selected[manifest].sha256, len(inventory.files), inventory.byte_count))
        plan = TrainingProcessPlan(uuid4().hex, str(root), _directory(root), str(name), command,
            captured, preview_fingerprint, timeout_seconds, log_limit,
            3 if environment_profile == LOCAL_ENVIRONMENT else 2 if bindings else 1, tuple(bindings), environment_profile)
        plan.validate()
        if os.path.lexists(plan.destination):
            raise TrainingProcessError("The process workspace is occupied; choose a new destination.")
        return plan

    @staticmethod
    def _verify_inputs(plan, cancel=None):
        cancel = inspection_control(cancel)
        for expected in plan.inputs:
            if ProcessInput.capture(expected.path, cancel) != expected:
                raise TrainingProcessError("A selected process input changed after review.")
        for inventory in plan.inventories:
            summary = SoftwareInventoryService().verify(inventory.root, inventory.manifest_path, should_cancel=cancel)
            if summary["inventory_sha256"] != inventory.manifest_sha256 or summary["files"] != inventory.file_count or summary["bytes"] != inventory.byte_count:
                raise TrainingProcessError("The software inventory changed after process review.")

    @staticmethod
    def _environment(work, profile=LEGACY_ENVIRONMENT):
        if type(profile) is not str or profile not in {LEGACY_ENVIRONMENT, LOCAL_ENVIRONMENT}:
            raise TrainingProcessError("Unsupported process environment profile.")
        system = windows_directory()
        environment = {"SystemRoot": system, "WINDIR": system, "PATH": str(Path(system)/"System32"),
            "TEMP": str(work/"tmp"), "TMP": str(work/"tmp"), "HOME": str(work/"home"), "USERPROFILE": str(work/"home"),
            "HF_HOME": str(work/"cache"), "HF_HUB_DISABLE_TELEMETRY": "1", "PYTHONUTF8": "1", "PYTHONNOUSERSITE": "1"}
        if profile == LEGACY_ENVIRONMENT:
            environment.update(HF_HUB_OFFLINE="1", HF_DATASETS_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        return environment

    @staticmethod
    def _pending(plan):
        return _bytes({"schema_version": 1, "name": plan.name, "run_id": plan.run_id, "fingerprint": plan.fingerprint})

    @staticmethod
    def _receipt(plan):
        # Raw arguments and selected input paths stay in the in-memory plan.
        # Recovery needs only owned paths and fingerprinted identities.
        receipt = {"schema_version": plan.schema_version, "run_id": plan.run_id, "root": plan.root,
            "root_identity": plan.root_identity, "name": plan.name,
            "process_fingerprint": plan.fingerprint, "preview_fingerprint": plan.preview_fingerprint,
            "command_sha256": _sha(_bytes(plan.command)),
            "inputs": [{"sha256": item.sha256, "size": item.state[2]} for item in plan.inputs],
            "timeout_seconds": plan.timeout_seconds, "log_limit": plan.log_limit}
        if plan.schema_version >= 2:
            receipt["inventories"] = [{"manifest_sha256": item.manifest_sha256,
                "root_sha256": _sha(item.root.encode("utf-8")), "files": item.file_count,
                "bytes": item.byte_count} for item in plan.inventories]
        if plan.schema_version == 3:
            receipt["environment_profile"] = plan.environment_profile
        return receipt

    @staticmethod
    def _validate_receipt(plan):
        required = {"schema_version", "run_id", "root", "root_identity", "name", "process_fingerprint",
            "preview_fingerprint", "command_sha256", "inputs", "timeout_seconds", "log_limit"}
        if isinstance(plan, dict) and type(plan.get("schema_version")) is int and plan["schema_version"] in {2, 3}:
            required.add("inventories")
        if isinstance(plan, dict) and plan.get("schema_version") == 3:
            required.add("environment_profile")
        if not isinstance(plan, dict) or set(plan) != required or type(plan["schema_version"]) is not int or plan["schema_version"] not in {1, 2, 3}:
            raise TrainingProcessError("The process recovery plan version or fields are unsupported.")
        if plan["schema_version"] == 3 and plan["environment_profile"] != LOCAL_ENVIRONMENT:
            raise TrainingProcessError("Unsupported process environment recovery evidence.")
        if plan["schema_version"] >= 2:
            inventories = plan["inventories"]
            if type(inventories) is not list or not (1 if plan["schema_version"] == 2 else 0) <= len(inventories) <= 4 or any(
                type(item) is not dict or set(item) != {"manifest_sha256", "root_sha256", "files", "bytes"}
                or any(type(item[key]) is not str or not re.fullmatch(r"[0-9a-f]{64}", item[key]) for key in ("manifest_sha256", "root_sha256"))
                or type(item["files"]) is not int or not 0 <= item["files"] <= 200_000
                or type(item["bytes"]) is not int or not 0 <= item["bytes"] <= 64 * 1024 ** 3 for item in inventories):
                raise TrainingProcessError("Unsupported software inventory recovery evidence.")
        if not all(isinstance(plan[key], str) for key in ("run_id", "root", "name", "process_fingerprint", "preview_fingerprint", "command_sha256")):
            raise TrainingProcessError("The process recovery identity is invalid.")
        if not re.fullmatch(r"[0-9a-f]{32}", plan["run_id"]) or any(not re.fullmatch(r"[0-9a-f]{64}", plan[key]) for key in ("process_fingerprint", "preview_fingerprint", "command_sha256")):
            raise TrainingProcessError("The process recovery fingerprints are invalid.")
        identity, inputs = plan["root_identity"], plan["inputs"]
        if not isinstance(identity, list) or len(identity) != 2 or any(type(item) is not int for item in identity):
            raise TrainingProcessError("The recorded recovery root is invalid.")
        if not isinstance(inputs, list) or not 1 <= len(inputs) <= 128 or any(
            not isinstance(item, dict) or set(item) != {"sha256", "size"}
            or not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
            or type(item["size"]) is not int or item["size"] < 0 for item in inputs):
            raise TrainingProcessError("The recorded process input identities are invalid.")
        if plan["schema_version"] >= 2:
            inventories = plan["inventories"]
            if len({item["root_sha256"] for item in inventories}) != len(inventories) or any(item["manifest_sha256"] not in {row["sha256"] for row in inputs} for item in inventories):
                raise TrainingProcessError("Software inventory evidence has no matching process input.")

    @staticmethod
    def _clear_pending(root, expected):
        path = Path(root) / PENDING_NAME
        if _read(path) != expected:
            raise TrainingProcessError("The active-process receipt changed; leave it for recovery.")
        path.unlink()
        if os.path.lexists(path):
            raise TrainingProcessError("The active-process receipt could not be cleared.")

    def execute(self, plan: TrainingProcessPlan, confirmed_fingerprint: str, jobs: BackgroundJobManager, *, running_observer=None, should_cancel=None, invocation_observer=None):
        plan.validate()
        if confirmed_fingerprint != plan.fingerprint:
            raise TrainingProcessError("Confirm this exact process plan before execution.")
        if os.name != "nt":
            raise TrainingProcessError("Contained process execution is unavailable on this platform.")
        job, acquired = jobs.try_acquire("Controlled trainer process", resource="gpu", key=plan.fingerprint)
        if not acquired:
            return TrainingProcessResult("held", plan.destination, "GPU work is already active; retry the unchanged plan when available.")
        stopped = True
        outcome = None
        try:
            reclaim=getattr(jobs,"prepare_ai_exclusive_work",None)
            if callable(reclaim):
                reclaim()
            if _directory(plan.root) != plan.root_identity:
                raise TrainingProcessError("The selected process root changed.")
            with _lease(Path(plan.root)):
                if os.path.lexists(Path(plan.root) / PENDING_NAME):
                    raise TrainingProcessError("A previous process requires recovery before another launch.")
                # Only this boundary can create a native owner. Earlier root,
                # lease, and pending-run failures need no recovery for this plan.
                if invocation_observer is not None:
                    try:
                        invocation_observer()
                    except Exception:
                        pass  # A UI observation cannot interrupt ownership.
                stopped = False  # An unexpected owner failure must not free GPU capacity.
                try:
                    outcome, stopped = self._execute_owned(plan, jobs, job, running_observer, should_cancel)
                finally:
                    if not stopped:
                        # Recovery uses this same root lease. Install the held
                        # slot before it can observe/reconcile the stopped tree.
                        with self._held_lock:
                            self._held[plan.run_id] = (jobs, job.id, plan.fingerprint)
            return outcome
        finally:
            # An unobservable tree deliberately keeps the slot held. A future UI
            # must require process/recovery review rather than reuse the GPU.
            if stopped:
                status = "complete" if outcome and outcome.status == "exited_unverified" else "cancelled" if outcome and outcome.status == "cancelled" else "failed"
                jobs.release(job.id, state=status, error=outcome.message if outcome and status == "failed" else "")
            else:
                jobs.update(job.id, 0, 0, "Process-tree termination is unverified; GPU capacity remains held for recovery")

    def _execute_owned(self, plan, jobs, job, running_observer=None, should_cancel=None):
        journal = owner = reader = None
        tip = ""
        stopped = True
        log_error = []
        log_summary = []
        code = None
        def cancel():
            return job.cancel_requested or jobs.shutting_down or bool(should_cancel and should_cancel())
        last_verification_progress = 0.0
        def verification_cancel():
            nonlocal last_verification_progress
            if time.monotonic() - last_verification_progress >= .25:
                job.update(0, 0, "Verifying selected software and process inputs")
                last_verification_progress = time.monotonic()
            return cancel()
        prelaunch_deadline = time.monotonic() + VERIFICATION_SECONDS
        try:
            _cancel(cancel)
            if _directory(plan.root) != plan.root_identity:
                raise TrainingProcessError("The process root changed before reservation.")
            plan.destination.mkdir(exist_ok=False)
            (plan.destination / "events").mkdir()
            work = plan.destination / "work"
            work.mkdir()
            for name in ("tmp", "home", "cache"):
                (work/name).mkdir()
            work_identity = _directory(work)
            journal = TrainingRunJournal(plan.destination)
            receipt = self._receipt(plan)
            tip = journal.append("reserved", {"plan": receipt, "fingerprint": _sha(_bytes(receipt))}, tip)
            _write_new(Path(plan.root) / PENDING_NAME, self._pending(plan))
            log_path = plan.destination / "process.log"
            _write_new(log_path, b"")
            log_identity = _state(log_path)[:2]
            with log_path.open("ab", buffering=0) as log:
                if descriptor_identity(log.fileno())[:2] != log_identity:
                    raise TrainingProcessError("The process log changed before launch.")
                owner = self.backend(plan.owner_name)
                stopped = False  # Helper processes belong to this same Job.
                self._verify_owned_inputs(plan, owner, "before-create", verification_cancel, prelaunch_deadline)
                tip = journal.append("starting", {"owner": plan.owner_name}, tip)
                _cancel(cancel)
                owner.spawn_suspended(plan.command, str(work), self._environment(work, plan.environment_profile))
                stopped = False
                # Recheck while the root is suspended, before any trainer code.
                self._verify_owned_inputs(plan, owner, "before-resume", verification_cancel, prelaunch_deadline)
                if _directory(work) != work_identity:
                    raise TrainingProcessError("The output workspace changed before launch.")
                tip = journal.append("running", {"pid": owner.pid, "owner": plan.owner_name, "pause_supported": False}, tip)
                def capture():
                    total = 0
                    digest = hashlib.sha256()
                    complete = False
                    exceeded = False
                    try:
                        while True:
                            chunk = owner.stdout.read(8192)
                            if not chunk:
                                complete = True
                                break
                            remaining = plan.log_limit - total
                            if remaining > 0:
                                digest.update(chunk[:remaining])
                                total += min(len(chunk), remaining)
                            if len(chunk) > remaining:
                                exceeded = True
                                raise TrainingProcessError("The process exceeded its log capacity.")
                    except BaseException as exc:
                        log_error.append(_failure_message(exc))
                    finally:
                        try:
                            # Unknown trainer output has no safe generic redactor.
                            # Persist an opaque bounded summary, never raw text.
                            summary = _bytes({"schema_version": 1, "policy": "opaque_child_output",
                                "bytes_hashed": total, "sha256": digest.hexdigest(),
                                "complete": complete, "limit_exceeded": exceeded})
                            if log.write(summary) != len(summary):
                                raise TrainingProcessError("The process log summary could not be written completely.")
                            os.fsync(log.fileno())
                            log_summary.append(summary)
                        except BaseException as exc:
                            log_error.append("Process log summary failed; " + _failure_message(exc))
                reader = threading.Thread(target=capture, daemon=True)
                reader.start()
                owner.resume()
                started = time.monotonic()
                next_progress = started
                reason = ""
                while True:
                    if cancel():
                        reason = "Application shutdown interrupted the process." if jobs.shutting_down else "The process was cancelled; resumable state is unsupported."
                        break
                    if job.state == "paused":
                        jobs.resume(job.id)  # A worker pause cannot suspend a trainer.
                    if log_error:
                        raise TrainingProcessError(log_error[0])
                    if time.monotonic() - started >= plan.timeout_seconds:
                        raise TrainingProcessError("The process deadline expired; an unexpected prompt or stalled process remains a failure.")
                    journal._current()
                    if _directory(work) != work_identity or _state(log_path)[:2] != log_identity:
                        raise TrainingProcessError("The output workspace or process log changed while running.")
                    if running_observer is not None:
                        def observer_control():
                            if time.monotonic() - started >= plan.timeout_seconds:
                                raise TrainingProcessError("The process deadline expired during observation.")
                            return cancel()
                        running_observer.poll(plan, owner, observer_control)
                    code = owner.poll()
                    if code is not None:
                        # A signaled root handle can precede job-accounting
                        # cleanup. Allow a short natural drain, retaining GPU.
                        if not self._wait_stopped(owner, timeout=.25):
                            raise TrainingProcessError("The root exited while descendant processes were still active.")
                        break
                    if time.monotonic() >= next_progress:
                        job.update(0, 0, "Process running; pause and checkpoint resume are unavailable")
                        next_progress = time.monotonic() + .25
                    time.sleep(.025)
                if reason:
                    tip = journal.append("stopping", {"reason": reason}, tip)
                    owner.terminate()
                stopped = self._wait_stopped(owner)
                if not stopped:
                    raise TrainingProcessError("Process-tree termination is unverified; GPU capacity remains held.")
                reader.join(3)
                if reader.is_alive() or log_error:
                    raise TrainingProcessError(log_error[0] if log_error else "Process log shutdown is unverified.")
                if not reason:
                    stopped = False  # Post-exit verifier also needs observed death.
                    self._verify_owned_inputs(plan, owner, "after-exit", verification_cancel, time.monotonic() + VERIFICATION_SECONDS)
                    stopped = self._wait_stopped(owner)
                    if not stopped:
                        raise TrainingProcessError("Input-verifier termination is unverified; retain recovery evidence.")
                persisted_log = _read(log_path, plan.log_limit)
                if len(log_summary) != 1 or persisted_log != log_summary[0]:
                    raise TrainingProcessError("The process log summary failed exact read-back verification.")
                status = "interrupted" if jobs.shutting_down else "cancelled" if reason else "exited_unverified" if code == 0 else "failed"
                message = reason or ("The process exited; outputs still require independent adapter verification." if code == 0 else f"The process failed with exit code {code}; retain partial outputs.")
                details = {"exit_code": code, "tree_stopped": True, "message": message,
                    "log_sha256": _sha(persisted_log), "outputs_verified": False}
                if not reason:
                    details["input_verification_sha256"] = verified_receipt_hashes(plan)
                    if code == 0 and running_observer is not None:
                        key = getattr(running_observer, "receipt_key", "native_observation_sha256")
                        if key not in {"native_observation_sha256", "preflight_receipt_sha256"}:
                            raise TrainingProcessError("Unsupported process completion evidence.")
                        details[key] = running_observer.finish(plan)
                tip = journal.append(status, details, tip)
                self._clear_pending(plan.root, self._pending(plan))
                return TrainingProcessResult(status, plan.destination, message, code, True, tip), True
        except BaseException as exc:
            if owner is not None:
                try:
                    owner.terminate()
                    stopped = self._wait_stopped(owner)
                except OSError:
                    stopped = False
            if reader is not None:
                reader.join(3)
            message = _failure_message(exc)
            status = "cancelled" if isinstance(exc, InterruptedError) else "failed"
            if journal is not None:
                try:
                    if not stopped:
                        raise TrainingProcessError("The process tree is still unverified.")
                    rows, current = journal.read()
                    if not rows or current != tip:
                        raise TrainingProcessError("Process evidence changed during failure handling.")
                    # A launch interrupted while suspended uses the stopping edge.
                    if rows[-1]["state"] in {"starting", "running"} and status == "cancelled":
                        tip = journal.append("stopping", {"reason": message}, tip)
                    tip = journal.append(status, {"message": message, "tree_stopped": stopped, "outputs_verified": False}, tip)
                    if os.path.lexists(Path(plan.root) / PENDING_NAME):
                        self._clear_pending(plan.root, self._pending(plan))
                except (OSError, ValueError, RuntimeError):
                    status = "recovery_required"
            elif os.path.lexists(plan.destination):
                status = "recovery_required"
            return TrainingProcessResult(status, plan.destination, message, code, stopped, tip), stopped
        finally:
            if owner is not None:
                owner.close()

    @staticmethod
    def _verify_owned_inputs(plan, owner, phase, cancel, deadline):
        verify_owned_inputs(plan, owner, phase,
            TrainingProcessService._environment(plan.destination / "work", plan.environment_profile), cancel, deadline)

    @staticmethod
    def _wait_stopped(owner, timeout=3):
        deadline = time.monotonic() + timeout
        while owner.active():
            if time.monotonic() >= deadline:
                return False
            time.sleep(.01)
        return True

    def recover(self, destination, expected_tip: str):
        """Reconcile one known stopped run; never kill a stored PID or restart it."""
        destination = Path(destination).absolute()
        with _lease(destination.parent):
            journal = TrainingRunJournal(destination)
            rows, tip = journal.read()
            if not rows or tip != expected_tip:
                raise TrainingProcessError("Recovery evidence changed or is incomplete; leave it unchanged.")
            reserved = rows[0]["details"]
            plan = reserved.get("plan", {})
            self._validate_receipt(plan)
            if _sha(_bytes(plan)) != reserved.get("fingerprint") or not re.fullmatch(r"[0-9a-f]{32}", str(plan.get("run_id", ""))):
                raise TrainingProcessError("Recovery has no exact process plan identity.")
            if str(destination) != str(Path(plan.get("root", "")) / plan.get("name", "")):
                raise TrainingProcessError("The recovery workspace is not the reserved destination.")
            if _directory(destination.parent) != tuple(plan.get("root_identity", ())):
                raise TrainingProcessError("The recovery root differs from its recorded identity.")
            if type(plan.get("schema_version")) is not int or plan["schema_version"] not in {1, 2, 3} or not re.fullmatch(r"[0-9a-f]{64}", str(plan.get("process_fingerprint", ""))):
                raise TrainingProcessError("The process recovery plan version or identity is unsupported.")
            pending = _bytes({"schema_version": 1, "name": plan["name"], "run_id": plan["run_id"], "fingerprint": plan["process_fingerprint"]})
            pending_path = destination.parent / PENDING_NAME
            if os.path.lexists(pending_path) and _read(pending_path) != pending:
                raise TrainingProcessError("Another active receipt differs from this recovery plan.")
            if self.backend.active_named("Local\\CollectiGen.Trainer." + plan["run_id"]):
                raise TrainingProcessError("The process tree is still active; recovery cannot claim it stopped.")
            state = rows[-1]["state"]
            if state not in TERMINAL:
                tip = journal.append("interrupted", {"tree_stopped": True, "outputs_verified": False,
                    "message": "The previous owner ended. Preserve partial outputs; retry requires a new reviewed workspace."}, tip)
                state = "interrupted"
            if os.path.lexists(pending_path):
                self._clear_pending(destination.parent, pending)
            with self._held_lock:
                held = self._held.get(plan["run_id"])
                if held is not None:
                    manager, job_id, fingerprint = held
                    job = manager.get(job_id)
                    if fingerprint != plan["process_fingerprint"] or job is None or job.key != fingerprint:
                        raise TrainingProcessError("The held GPU slot no longer matches this recovery plan.")
                    manager.release(job_id, state="failed", error="Recovery verified that the interrupted process tree stopped")
                    self._held.pop(plan["run_id"], None)
            return TrainingProcessResult(state, destination, "Recovery retained all evidence and outputs. No process was restarted or result registered.", tree_stopped=True, journal_tip=tip)
