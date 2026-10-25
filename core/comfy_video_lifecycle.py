"""Durable cache ownership for one pinned, dedicated Wan Comfy runtime.

One reservation spans all clips and idle gaps. This controller never attests
per-clip success; that remains the verified-output execution owner's duty.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from uuid import uuid4

from . import comfy_video_runtime as runtime
from . import comfy_video_supervisor as supervisor
from .model_cache import ModelCacheService, ModelRequirementSet
from .model_execution_cache import ModelExecutionCacheService, RECEIPT_NAME
from .model_storage_policy import load_policy
from .model_use_leases import digest, cache_state, cache_lock
from .model_workspace import _path, _pinned_parent

OWNER_ID = "collectigen.comfy-wan-runtime"
OWNER_VERSION = "1"
INTENT_NAME = "comfy-runtime-owner.json"
UNRESERVED_CLOSE_NAME = "runtime-unreserved-closed.json"
ADAPTER_VERSION = "collectigen-wan22-ti2v5b-comfy-v1"
_PROCESS_ENV = frozenset({"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "SYSTEMDRIVE",
    "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "PROCESSOR_LEVEL", "PROCESSOR_REVISION",
    "NUMBER_OF_PROCESSORS", "CUDA_PATH", "CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER",
    "PYTORCH_CUDA_ALLOC_CONF", "PYTORCH_ALLOC_CONF", "OMP_NUM_THREADS", "MKL_NUM_THREADS"})


def _config(value):
    value = deepcopy(value)
    for role in ("transformer", "text_encoder", "vae"):
        value[role] = runtime.RuntimeModel(**value[role])
    return runtime.RuntimeConfig(**value)


def _config_dict(config):
    return json.loads(json.dumps(asdict(config), default=str))


@dataclass(frozen=True)
class LifecyclePreview:
    review_id: str
    source_preview: runtime.RuntimePreview
    registry_token: str
    cache_preview: object | None
    resource_ids: tuple[str, ...]
    can_prepare: bool
    message: str


@dataclass(frozen=True)
class LifecycleStatus:
    locator: Path
    state: str
    execution_id: str
    bindings: tuple[tuple[str, str], ...]
    message: str
    runtime_fingerprint: str = ""
    protected_mode: bool = True
    owner_id: str = OWNER_ID
    owner_version: str = OWNER_VERSION


@contextmanager
def _owner_lock(directory):
    path = supervisor.checked_path(Path(directory) / "runtime-owner.lock")
    with _pinned_parent(path.parent, ()):
        with path.open("a+b") as handle:
            before = os.fstat(handle.fileno())
            after = path.stat(follow_symlinks=False)
            if before.st_nlink != 1 or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ValueError("Runtime owner lock changed.")
            if not before.st_size:
                handle.write(b"0"); handle.flush(); os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt": msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class _SupervisedProcess:
    """Popen-like held supervisor; kill never destroys the terminal witness owner."""
    def __init__(self, process, workspace, contract):
        self.process, self.workspace, self.contract = process, Path(workspace), contract
        self.pid = process.pid

    def poll(self):
        if os.path.lexists(self.workspace / "supervisor-terminal.json"):
            supervisor.terminal_evidence(self.workspace, self.contract)
            return 0
        return self.process.poll()

    def terminate(self):
        path = self.workspace / "supervisor-stop.json"
        value = {"action": "stop", "nonce": self.contract["nonce"], "launch_digest": supervisor.digest(self.contract)}
        if os.path.lexists(path):
            if supervisor.read(path) != supervisor.encoded(value):
                raise ValueError("The existing stop command differs; preserve runtime protection.")
        else:
            supervisor.publish(path, value)

    def wait(self, timeout):
        result = self.process.wait(timeout=timeout)
        supervisor.terminal_evidence(self.workspace, self.contract)
        return result

    def kill(self):
        raise runtime.RuntimeBoundaryError("Supervisor stop is uncertain; keep the exact runtime and its models protected.")


class ComfyVideoLifecycle:
    def __init__(self, registry, *, requirement_resolver, protected_roots=(), managed_roots=()):
        self.registry, self.requirement_resolver = registry, requirement_resolver
        self.protected_roots, self.managed_roots = tuple(protected_roots), tuple(managed_roots)
        self._reviews, self._proofs, self._held = {}, {}, {}
        self.cache = ModelExecutionCacheService(registry, owner_id=OWNER_ID, owner_version=OWNER_VERSION,
            protected_roots=protected_roots, managed_roots=managed_roots, transition_verifier=self._transition_proof)

    def _transition_proof(self, execution_id, record, proof):
        issued = self._proofs.get(proof)
        if issued is None or issued[:2] != (execution_id, digest(record)):
            return None
        return deepcopy(issued[2])

    def _update(self, locator, record, changes):
        receipt, _ = self.cache.read(locator.parent / RECEIPT_NAME)
        proof = uuid4().hex
        self._proofs[proof] = (receipt["execution_id"], digest(record), deepcopy(changes))
        try:
            return self.cache.update(locator.parent / RECEIPT_NAME, record, proof)
        finally:
            self._proofs.pop(proof, None)

    def _guard(self, locator):
        locator = _path(locator, (*self.protected_roots, *self.managed_roots))
        if locator.name != INTENT_NAME:
            raise ValueError("Choose the exact retained runtime owner locator.")
        return locator

    def _read(self, locator):
        locator = self._guard(locator)
        if os.path.lexists(locator.parent / UNRESERVED_CLOSE_NAME):
            raise ValueError("This unreserved locator is closed; it cannot own a new runtime reservation.")
        intent = json.loads(supervisor.read(locator))
        receipt, record = self.cache.read(locator.parent / RECEIPT_NAME)
        if (not isinstance(intent, dict) or intent.get("schema") != "collectigen.comfy-runtime-owner.v1"
                or intent.get("owner_id") != OWNER_ID or intent.get("owner_version") != OWNER_VERSION
                or intent.get("locator") != str(locator)
                or receipt.get("owner_fingerprint") != digest(intent)
                or receipt.get("owner_context") != {"locator": str(locator), "run_id": intent.get("run_id")}):
            raise ValueError("Runtime owner intent differs from its canonical reservation.")
        state = None
        if record is not None and record.get("review_path"):
            event_path = self._guard_event(locator, record["review_path"])
            raw = supervisor.read(event_path)
            state = json.loads(raw)
            if (not isinstance(state, dict) or digest(state) != record.get("verified_journal_tip") or state.get("intent_digest") != digest(intent)
                    or state.get("phase") != record["phase"] or state.get("execution_id") != receipt["execution_id"]):
                raise ValueError("Runtime owner journal no longer matches its reservation.")
        return locator, intent, receipt, record, state

    def _unreserved(self, locator):
        with cache_lock(self.registry, protected_roots=self.protected_roots):
            return self._unreserved_locked(locator)

    def _unreserved_locked(self, locator):
        """Project only a never-reserved locator; absence never releases a lease.

        Do not follow config/model/journal paths from an unanchored intent. Any
        canonical lease referring to this owner requires its original receipt,
        even when that lease says released. Mutating callers hold both locks.
        """
        locator = self._guard(locator)
        receipt_path = locator.parent / RECEIPT_NAME
        closed_path = locator.parent / UNRESERVED_CLOSE_NAME
        if os.path.lexists(receipt_path):
            if os.path.lexists(closed_path):
                raise ValueError("Closed unreserved evidence conflicts with a cache receipt; preserve both.")
            return None
        intent_digest = None
        if os.path.lexists(locator):
            intent = json.loads(supervisor.read(locator))
            if (not isinstance(intent, dict) or intent.get("schema") != "collectigen.comfy-runtime-owner.v1"
                    or intent.get("owner_id") != OWNER_ID or intent.get("owner_version") != OWNER_VERSION
                    or intent.get("locator") != str(locator)
                    or any(not isinstance(intent.get(key), str) or not re.fullmatch(r"[0-9a-f]{32}", intent[key])
                           for key in ("run_id", "nonce"))):
                raise ValueError("The unreserved runtime owner intent is invalid; preserve it for review.")
            intent_digest = digest(intent)
        leases = cache_state(self.registry.snapshot()[0])["leases"]
        for record in leases.values():
            retained = record.get("receipt_path")
            journal = record.get("review_path")
            if (record.get("owner_id") == OWNER_ID and (not isinstance(retained, str) or not Path(retained).is_absolute())
                    or isinstance(retained, str) and Path(retained) == receipt_path
                    or intent_digest is not None and record.get("owner_fingerprint") == intent_digest
                    or isinstance(journal, str) and Path(journal).parent == locator.parent):
                raise ValueError("A canonical runtime reservation is missing its original receipt; keep all protection in place.")
        expected = {"schema": "collectigen.comfy-unreserved-close.v1", "owner_id": OWNER_ID,
            "owner_version": OWNER_VERSION, "locator": str(locator), "intent_digest": intent_digest}
        # Cache receipts are published before their canonical reservation lock.
        # Do not project an absent receipt if that publication just completed.
        if os.path.lexists(receipt_path):
            return None
        closed = os.path.lexists(closed_path)
        if closed and supervisor.read(closed_path) != supervisor.encoded(expected):
            raise ValueError("The unreserved closure no longer matches its exact retained intent.")
        return expected, LifecycleStatus(locator, "discarded" if closed else "unreserved", "", (),
            "This never-reserved preparation is closed. Review a new owner directory; no model protections were changed."
            if closed else "No cache reservation was created for this locator. Discard Unstarted closes it without changing any model protections.",
            protected_mode=False)

    @staticmethod
    def _guard_event(locator, path):
        path = Path(path)
        if not path.is_absolute() or path.parent != locator.parent or not path.name.startswith("runtime-event-") or path.suffix != ".json":
            raise ValueError("Runtime transition escaped its exact owner directory.")
        return supervisor.checked_path(path)

    def _event(self, locator, intent, receipt, record, phase, **details):
        state = {"schema": 1, "intent_digest": digest(intent), "execution_id": receipt["execution_id"],
                 "previous": record.get("verified_journal_tip", ""), "phase": phase, **details}
        event_path = locator.parent / ("runtime-event-" + uuid4().hex + ".json")
        supervisor.publish(event_path, state)
        changes = {"phase": phase, "review_path": str(event_path), "verified_journal_tip": digest(state)}
        if phase in {"stopped", "discarded"}:
            changes.update(state="released", released_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        elif "runtime_fingerprint" in details:
            changes["plan_fingerprint"] = details["runtime_fingerprint"]
        self._update(locator, record, changes)
        return state

    def preview(self, config, requirements, *, should_cancel=None, progress=None):
        if (not isinstance(requirements, ModelRequirementSet) or requirements.qualification != "adapter_qualified"
                or requirements.adapter_version != ADAPTER_VERSION or len(requirements.resources) != 3
                or not callable(self.requirement_resolver)
                or self.requirement_resolver(requirements.adapter_id, requirements.adapter_version,
                    requirements.request_fingerprint) != requirements.resources):
            raise ValueError("Protected video preparation needs the exact qualified Wan companion set.")
        selected = {"sha256:" + model.sha256: model for model in (config.transformer, config.text_encoder, config.vae)}
        if set(selected) != {r.resource_id for r in requirements.resources}:
            raise ValueError("Runtime model identities differ from the qualified companion set.")
        roles = {"transformer": {"unet", "checkpoint"}, "text_encoder": {"text_encoder"}, "vae": {"vae"}}
        for resource in requirements.resources:
            model = selected[resource.resource_id]
            if resource.role not in roles[model.role] or Path(resource.archive_path) != Path(model.path):
                raise ValueError("Runtime source paths or roles differ from the qualified companion set.")
        source = runtime.preview_runtime(replace(config, protected_loader=True), protected_roots=self.protected_roots)
        value, token = self.registry.snapshot()
        cache_preview = None
        if load_policy(value.get("workspace", {})).mode == "tiered":
            service = ModelCacheService(self.registry, protected_roots=self.protected_roots,
                managed_roots=self.managed_roots, requirement_resolver=self.requirement_resolver)
            cache_preview = service.preview_prepare(requirements, expected_registry=token,
                should_cancel=should_cancel, progress=progress)
        ready = cache_preview is None or cache_preview.can_prepare
        review = LifecyclePreview(uuid4().hex, source, token, cache_preview, tuple(sorted(selected)), ready,
            "Prepare the complete set, then review and start its dedicated runtime." if ready else cache_preview.message)
        self._reviews[review.review_id] = (review, requirements)
        return review

    def prepare(self, review, owner_root, *, confirmed=False, should_cancel=None, progress=None):
        if confirmed is not True or not isinstance(review, LifecyclePreview) or self._reviews.get(review.review_id, (None,))[0] is not review:
            raise ValueError("Confirm this controller's exact runtime preparation review.")
        if not review.can_prepare:
            raise ValueError("The whole runtime set is held; resolve its preparation review first.")
        locator = self._guard(Path(owner_root) / INTENT_NAME)
        workspace = Path(review.source_preview.config.workspace_root)
        if workspace == locator.parent or workspace in locator.parents or locator.parent in workspace.parents:
            raise ValueError("Runtime workspace and durable owner directory must be separate.")
        with _owner_lock(locator.parent):
            if os.path.lexists(locator.parent / UNRESERVED_CLOSE_NAME):
                raise ValueError("This unreserved locator is closed; review a new owner directory.")
            fresh = runtime.preview_runtime(review.source_preview.config, protected_roots=self.protected_roots)
            if fresh.fingerprint != review.source_preview.fingerprint:
                raise ValueError("The source runtime changed after preparation review.")
            _, requirements = self._reviews[review.review_id]
            intent = {"schema": "collectigen.comfy-runtime-owner.v1", "owner_id": OWNER_ID, "owner_version": OWNER_VERSION,
                "locator": str(locator), "run_id": uuid4().hex, "nonce": uuid4().hex,
                "config": _config_dict(fresh.config), "source_fingerprint": fresh.fingerprint,
                "resource_ids": list(review.resource_ids), "qualification": {"adapter_id": requirements.adapter_id,
                    "adapter_version": requirements.adapter_version, "request_fingerprint": requirements.request_fingerprint}}
            supervisor.publish(locator, intent)
            self.cache.prepare(receipt_root=locator.parent, owner_fingerprint=digest(intent),
                owner_context={"locator": str(locator), "run_id": intent["run_id"]}, requirements=requirements,
                expected_registry=review.registry_token, cache_preview=review.cache_preview,
                requirement_resolver=self.requirement_resolver, should_cancel=should_cancel, progress=progress)
            self._reviews.pop(review.review_id, None)
            return self._bind(locator)

    def _bind(self, locator):
        locator, intent, receipt, record, _ = self._read(locator)
        status = self.cache.status(locator.parent / RECEIPT_NAME)
        if status.status != "ready" or record["phase"] != "prepared":
            return self.status(locator)
        mapping = dict(status.bindings)
        if set(mapping) != set(intent["resource_ids"]):
            raise ValueError("Prepared runtime has an incomplete exact binding set.")
        config = _config(intent["config"])
        changes = {role: replace(getattr(config, role), path=mapping["sha256:" + getattr(config, role).sha256])
                   for role in ("transformer", "text_encoder", "vae")}
        final = runtime.preview_runtime(replace(config, **changes), protected_roots=self.protected_roots)
        self._event(locator, intent, receipt, record, "bound", config=_config_dict(final.config),
                    runtime_fingerprint=final.fingerprint, binding_digest=runtime.protected_binding(final)["binding_digest"])
        return self.status(locator)

    def status(self, locator):
        unreserved = self._unreserved(locator)
        if unreserved is not None:
            return unreserved[1]
        locator, _, receipt, record, state = self._read(locator)
        cache = self.cache.status(locator.parent / RECEIPT_NAME)
        phase = record["phase"] if record else "unreserved"
        if cache.status == "recovery_required": phase = "recovery_required"
        return LifecycleStatus(locator, phase, receipt["execution_id"], cache.bindings,
            "Runtime models remain protected until exact owned stop or unstarted discard." if cache.status != "released"
            else "Runtime ownership is closed; independent Keep Warm and manual pins remain.",
            state.get("runtime_fingerprint", "") if state else "")

    def bound_config(self, locator):
        """Project an anchored unstarted configuration without probing its files.

        This is for restoring setup/model fields only. It neither adopts a
        runtime nor proves current file availability; start() revalidates both.
        """
        baseline = self._read(locator)
        locator, intent, receipt, record, state = baseline
        status = self.cache.status(locator.parent / RECEIPT_NAME)
        if (record is None or record["state"] != "active" or record["phase"] != "bound"
                or state is None or status.status != "ready"
                or status.execution_id != receipt["execution_id"]
                or tuple(tuple(pair) for pair in record["bindings"]) != status.bindings
                or record.get("plan_fingerprint") != state.get("runtime_fingerprint")
                or any(not isinstance(state.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", state[key])
                       for key in ("runtime_fingerprint", "binding_digest"))):
            raise ValueError("Only an active, completely bound unstarted preparation can restore setup.")
        try:
            source, bound = _config(intent["config"]), _config(state["config"])
            mapping = dict(status.bindings)
            roles = ("transformer", "text_encoder", "vae")
            identities = ["sha256:" + getattr(source, role).sha256 for role in roles]
            if (len(set(identities)) != 3 or len(status.bindings) != 3
                    or set(mapping) != set(identities) or sorted(identities) != receipt["resource_ids"]
                    or sorted(identities) != intent["resource_ids"]):
                raise ValueError("The retained runtime has an incomplete input set.")
            expected = replace(source, **{role: replace(getattr(source, role), path=mapping[identity])
                for role, identity in zip(roles, identities)})
            if bound != expected or bound.protected_loader is not True:
                raise ValueError("The retained setup differs from its exact admitted bindings.")
            # Lexical privacy checks only: never stat/resolve the retained
            # source, interpreter, workspace or model paths for display.
            boundaries = tuple(Path(os.path.abspath(root)) for root in (*self.protected_roots, *self.managed_roots))
            for config in (source, bound):
                paths = [config.source_root, config.python_executable, config.python_runtime_root, config.workspace_root,
                         *(getattr(config, role).path for role in roles)]
                for raw in paths:
                    if not isinstance(raw, (str, Path)) or not Path(raw).is_absolute():
                        raise ValueError("Retained setup needs explicit absolute locations.")
                    path = Path(os.path.abspath(raw))
                    if any(path == root or root in path.parents or path in root.parents for root in boundaries):
                        raise ValueError("Retained setup overlaps a protected location.")
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            raise ValueError("The bound runtime setup cannot be restored: " + str(exc)) from exc
        if self._read(locator) != baseline:
            raise ValueError("The retained runtime changed while restoring setup; read its status again.")
        return deepcopy(bound)

    def start(self, locator, *, confirmed=False, popen=subprocess.Popen):
        if confirmed is not True: raise ValueError("Confirm the reviewed dedicated runtime start.")
        locator = self._guard(locator)
        with _owner_lock(self.registry.path.parent), _owner_lock(locator.parent):
            locator, intent, receipt, record, state = self._read(locator)
            if record is None or record["state"] != "active" or record["phase"] != "bound" or not state:
                raise ValueError("This runtime is not an unstarted bound preparation; recover its exact owner.")
            leases = cache_state(self.registry.snapshot()[0]).get("leases", {})
            if any(key != receipt["execution_id"] and item.get("owner_id") == OWNER_ID
                   and item.get("state") == "active" and item.get("phase") in {"submitted", "uncertain"}
                   for key, item in leases.items()):
                raise ValueError("Another retained owned video runtime needs stop or recovery before starting.")
            preview = runtime.preview_runtime(_config(state["config"]), protected_roots=self.protected_roots)
            if preview.fingerprint != state["runtime_fingerprint"]:
                raise ValueError("Bound runtime source or exact prepared inputs changed.")
            self._event(locator, intent, receipt, record, "submitted", config=state["config"],
                runtime_fingerprint=preview.fingerprint, binding_digest=state["binding_digest"])
            def launch(command, **kwargs):
                workspace = Path(preview.config.workspace_root)
                helper = supervisor.read(Path(__file__).with_name("training_process_windows.py"))
                script = supervisor.read(Path(__file__).with_name("comfy_video_supervisor.py"))
                for name, raw in (("windows-owner.py", helper), ("supervisor.py", script)):
                    with supervisor.checked_path(workspace / name).open("xb") as stream:
                        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                    if supervisor.read(workspace / name) != raw: raise ValueError("Runtime supervisor copy changed.")
                # Retain required OS/GPU values, never arbitrary inherited API
                # credentials or unrelated app/model routes in the launch record.
                environment = {**{key: value for key, value in kwargs["env"].items() if key.upper() in _PROCESS_ENV},
                    **runtime.runtime_environment(workspace), "COLLECTIGEN_RUNTIME_NONCE": intent["nonce"]}
                contract = {"schema": supervisor.SCHEMA, "run_id": intent["run_id"], "nonce": intent["nonce"],
                    "runtime_fingerprint": preview.fingerprint, "command": list(command), "cwd": kwargs["cwd"],
                    "environment": environment, "helper_sha256": hashlib.sha256(helper).hexdigest()}
                supervisor.publish(workspace / "supervisor-launch.json", contract)
                _, _, _, current_record, current_state = self._read(locator)
                self._event(locator, intent, receipt, current_record, "submitted", config=current_state["config"],
                    runtime_fingerprint=preview.fingerprint, binding_digest=current_state["binding_digest"],
                    launch_digest=supervisor.digest(contract))
                child = popen((str(preview.config.python_executable), "-I", str(workspace / "supervisor.py"),
                    str(workspace / "supervisor-launch.json"), supervisor.digest(contract)), **{**kwargs, "env": environment})
                return _SupervisedProcess(child, workspace, contract)
            held = runtime.start_runtime(preview, confirmed=True, popen=launch)
            held.protected_verifier = lambda value: self.verify(locator, value)
            self._held[str(locator)] = held
            return held

    def _launch_contract(self, intent, state):
        workspace = Path(state["config"]["workspace_root"])
        raw = supervisor.read(workspace / "supervisor-launch.json")
        if hashlib.sha256(raw).hexdigest() != state.get("launch_digest"):
            raise ValueError("Supervisor launch no longer matches the reserved runtime.")
        contract = supervisor.load_contract(workspace / "supervisor-launch.json")
        if (contract["run_id"] != intent["run_id"] or contract["nonce"] != intent["nonce"]
                or contract["runtime_fingerprint"] != state["runtime_fingerprint"]
                or supervisor.digest(contract) != state.get("launch_digest")):
            raise ValueError("Supervisor launch no longer matches the reserved runtime.")
        return workspace, contract

    def verify(self, locator, held, *, identity_reader=None):
        locator, intent, receipt, record, state = self._read(locator)
        if (record is None or record["state"] != "active" or record["phase"] != "submitted"
                or self._held.get(str(locator)) is not held or held.process.poll() is not None
                or held.preview.fingerprint != state["runtime_fingerprint"]):
            raise ValueError("The exact held runtime no longer owns these inputs.")
        workspace, contract = self._launch_contract(intent, state)
        started = json.loads(supervisor.read(workspace / "supervisor-started.json"))
        if (started.get("launch_digest") != supervisor.digest(contract)
                or started.get("supervisor_pid") != held.pid or started.get("nonce") != intent["nonce"]):
            raise ValueError("The runtime's contained launch identity changed.")
        if os.path.lexists(workspace / "supervisor-terminal.json"):
            raise ValueError("The contained runtime has already stopped.")
        if identity_reader is None:
            from .comfy_video_transport import ComfyVideoTransport
            transport = ComfyVideoTransport(f"http://127.0.0.1:{held.preview.config.port}")
            identity_reader = lambda route: transport._json("GET", route)
        identity = identity_reader("/collectigen/runtime/" + intent["nonce"])
        if identity != {"contract": runtime.LOADER_CONTRACT, "binding_digest": state["binding_digest"],
                        "nonce": intent["nonce"], "pid": started["child_pid"]}:
            raise ValueError("The loopback service is not the exact owned loader-bound runtime.")
        return {"execution_id": receipt["execution_id"], "owner_id": OWNER_ID, "owner_version": OWNER_VERSION,
                "protected_mode": True, "owner_locator": str(locator),
                "runtime_fingerprint": state["runtime_fingerprint"], "resource_ids": tuple(intent["resource_ids"]),
                "required_set_digest": receipt["required_set_digest"], "binding_digest": state["binding_digest"]}

    def stop(self, locator, *, confirmed=False, timeout_seconds=15):
        if confirmed is not True: raise ValueError("Confirm stopping this exact dedicated runtime.")
        locator = self._guard(locator)
        with _owner_lock(locator.parent):
            locator, intent, _, record, state = self._read(locator)
            if record and record["state"] == "released": return self.status(locator)
            if not state or record["phase"] != "submitted":
                raise ValueError("This owner has no submitted runtime; use unstarted discard or recovery.")
            workspace, contract = self._launch_contract(intent, state)
            value = {"action": "stop", "nonce": intent["nonce"], "launch_digest": supervisor.digest(contract)}
            path = workspace / "supervisor-stop.json"
            if os.path.lexists(path):
                if supervisor.read(path) != supervisor.encoded(value): raise ValueError("Stop evidence conflicts.")
            else: supervisor.publish(path, value)
            until = time.monotonic() + min(max(timeout_seconds, 0), 30)
            while not os.path.lexists(workspace / "supervisor-terminal.json"):
                if time.monotonic() >= until:
                    raise ValueError("Owned stop is not yet proven; runtime models remain protected.")
                time.sleep(.05)
            return self._reconcile_stopped(locator)

    def _reconcile_stopped(self, locator):
        locator, intent, receipt, record, state = self._read(locator)
        if record["state"] == "released": return self.status(locator)
        workspace, contract = self._launch_contract(intent, state)
        witness = supervisor.terminal_evidence(workspace, contract)
        from .training_process_windows import WindowsTrainingJob
        if WindowsTrainingJob.active_named("Local\\CollectiGen.Comfy." + intent["run_id"]) != 0:
            raise ValueError("The exact contained Job still has consumers; its models remain protected.")
        self._event(locator, intent, receipt, record, "stopped", runtime_fingerprint=state["runtime_fingerprint"],
                    terminal_digest=digest(witness))
        self._held.pop(str(locator), None)
        return self.status(locator)

    def discard_unstarted(self, locator, *, confirmed=False):
        if confirmed is not True: raise ValueError("Confirm discarding this unstarted runtime preparation.")
        locator = self._guard(locator)
        # The UI retains a locator before its worker creates this unique folder.
        # Explicit discard may create just that folder to persist its closure.
        if not locator.parent.exists():
            with _pinned_parent(locator.parent.parent, (*self.protected_roots, *self.managed_roots)):
                locator.parent.mkdir(exist_ok=False)
        with _owner_lock(locator.parent):
            with cache_lock(self.registry, protected_roots=self.protected_roots):
                unreserved = self._unreserved_locked(locator)
                if unreserved is not None:
                    marker, status = unreserved
                    if status.state != "discarded":
                        supervisor.publish(locator.parent / UNRESERVED_CLOSE_NAME, marker)
                    return self._unreserved_locked(locator)[1]
            locator, intent, receipt, record, _ = self._read(locator)
            if record is None:
                self.cache.abandon_unreserved(locator.parent / RECEIPT_NAME)
            elif record["state"] == "released": return self.status(locator)
            elif record["phase"] in {"prepared", "bound"}:
                if self.cache.status(locator.parent / RECEIPT_NAME).status == "recovery_required":
                    raise ValueError("Resolve the interrupted cache preparation before discard.")
                self._event(locator, intent, receipt, record, "discarded")
            else:
                raise ValueError("A submitted or uncertain runtime cannot be discarded as unstarted.")
            return self.status(locator)

    def recover(self, locator, *, confirmed=False, should_cancel=None, progress=None):
        if confirmed is not True: raise ValueError("Confirm recovery of this exact retained runtime owner.")
        locator = self._guard(locator)
        if not locator.parent.exists():
            # Read-only projection for a locator whose worker never began.
            return self.status(locator)
        with _owner_lock(locator.parent):
            unreserved = self._unreserved(locator)
            if unreserved is not None:
                return unreserved[1]
            _, _, _, record, _ = self._read(locator)
            if record is None or record["state"] == "released": return self.status(locator)
            if record["phase"] == "prepared":
                self.cache.recover(locator.parent / RECEIPT_NAME, should_cancel=should_cancel, progress=progress)
                return self._bind(locator)
            if record["phase"] == "bound": return self.status(locator)
            # Positive tree-empty evidence only. No process adoption or resubmit.
            return self._reconcile_stopped(locator)
