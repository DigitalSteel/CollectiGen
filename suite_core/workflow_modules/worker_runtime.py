"""Finite ModuleEntrySession adapter for the original signed Manager worker.

No baseline means unavailable. This creates no updater or alternate worker/store;
all provisioning/execution remains with the existing reviewed worker services.
"""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import RLock
import json

from core.manager_worker_evidence import verify_retained_manager_worker
from core.training_source_io import read_stable_training_source
from core.video_decoder import VideoDecoderLifetime
from suite_core.discovery.manager_runtime_detached_admission import manager_inventory_json
from suite_core.manager.bootstrap_entry import ProcessBinding
from suite_core.manager.entry_binding import (
    _bind_training_worker, _bind_manager_video_runtime, _bind_manager_caption_runtime,
    _checked_explicit_worker_configs,
)
from suite_core.workflow_modules.entry_binding import ModuleEntryBinding, require
from suite_core.workflow_modules.session import ModuleEntrySession


@dataclass(frozen=True)
class _WorkerWorkspaceBinding:
    layout: object
    manager_arguments: tuple = ()


class ModuleWorkerSessionAdapter:
    """Explicit local adapter; never a NormalEntrySession or native grant."""
    def __init__(self, session, owner):
        require(type(session) is ModuleEntrySession and type(session.admitted_binding) is ModuleEntryBinding,
                'original_typed_module_worker_session_required')
        self.session, self.owner = session, owner
        self.admitted = _WorkerWorkspaceBinding(session.admitted_binding.layout)

    @property
    def state(self):
        return self.session.state

    def check(self):
        if self.owner.closing:
            raise InterruptedError('The original workflow worker is closing.')
        self.session.assert_ready()
        require(self.session.admitted_binding is self.owner.admitted_binding,
                'original_module_worker_binding_changed')


class ModuleDatasetVerifier:
    """Wrap cancellation/lifetime only; delegate to the original reviewed verifier."""
    def __init__(self, original, owner):
        self.original, self.owner = original, owner

    @property
    def inventory(self): return self.original.inventory

    @property
    def python_relative(self): return self.original.python_relative

    @property
    def work_root(self): return self.original.work_root

    def _cancel(self, should_cancel):
        def check():
            if should_cancel is not None and should_cancel():
                return True
            self.owner.adapter.check()
            return False
        return check

    def __call__(self, package, *, should_cancel):
        with self.owner.lock:
            self.owner.adapter.check()
            self.owner.dataset_calls += 1
        try:
            return self.original(package, should_cancel=self._cancel(should_cancel))
        finally:
            with self.owner.lock:
                self.owner.dataset_calls -= 1

    def prepare(self, package, *, source, environment, python_base, jobs, should_cancel=None, progress=None):
        with self.owner.lock:
            self.owner.adapter.check()
            self.owner.dataset_calls += 1
            if all(item is not jobs for item in self.owner.dataset_jobs):
                self.owner.dataset_jobs.append(jobs)
        try:
            return self.original.prepare(package, source=source, environment=environment,
                python_base=python_base, jobs=jobs, should_cancel=self._cancel(should_cancel), progress=progress)
        finally:
            with self.owner.lock:
                self.owner.dataset_calls -= 1


class ModuleWorkerCapabilities:
    """Retained runtime owners; drain claims do not release any Root grant."""
    def __init__(self, session):
        self.session, self.admitted_binding = session, session.admitted_binding
        self.lock = RLock()
        self.closing = False
        self.dataset_calls = 0
        self.dataset_jobs = []
        self.dataset_verifier = self.caption_runtime = None
        self.video_decoder_lifetime = VideoDecoderLifetime()
        self.adapter = ModuleWorkerSessionAdapter(session, self)
        self.signed_worker = None
        self.source_root = self.worker_python_version = None

    @property
    def close_held(self):
        with self.lock:
            if self.dataset_calls or any(jobs.active() for jobs in self.dataset_jobs):
                return True
        return (self.video_decoder_lifetime.active or
                self.caption_runtime is not None and self.caption_runtime.close_held)

    def begin_close(self):
        self.closing = True
        self.video_decoder_lifetime.begin_mutation()
        return not self.close_held


def _signed_source_version(admitted, signed_worker):
    plan, proof = signed_worker
    require(sha256(proof).hexdigest() == plan.inventory_sha256, 'module_worker_inventory_changed')
    source = admitted.layout.code_root / 'Apps/Manager/current/_internal'
    rows = {row['path']: row for row in manager_inventory_json(proof)['files']}
    pin = rows.get('_internal/tools/runtime-pins.json')
    require(pin is not None, 'signed_manager_worker_config_missing')
    raw, _ = read_stable_training_source(source / 'tools/runtime-pins.json', max_bytes=64 * 1024)
    require(len(raw) == pin['size_bytes'] and sha256(raw).hexdigest() == pin['sha256'],
            'signed_manager_worker_config_changed')
    version = json.loads(raw)['python']['version']
    _checked_explicit_worker_configs(admitted, signed_worker, source, version)
    return source, version


def bind_worker_capabilities(session):
    """Return the original-verifier adapter and expose retained runtimes on session.

    Requires session.worker_baseline already accepted from its authenticated
    Manager offer. Never searches installed Python or manufactures a baseline.
    Exposes worker_capabilities, dataset_verifier, caption_runtime,
    video_decoder_lifetime and the four existing admit/review/install/recover
    Manager Video callbacks. Consumers must retain/use this decoder lifetime and
    call worker_capabilities.begin_close() before reporting module local drain.
    """
    require(type(session) is ModuleEntrySession and type(session.admitted_binding) is ModuleEntryBinding,
            'original_typed_module_worker_session_required')
    session.assert_ready()
    if getattr(session, '_worker_capabilities_bound', False):
        return getattr(session, 'dataset_verifier', None)
    require(not getattr(session, '_worker_capabilities_attempted', False),
            'original_worker_binding_attempt_requires_review')
    session._worker_capabilities_attempted = True
    reference = getattr(session, 'worker_baseline', None)
    if reference is None:
        session.worker_capabilities = session.dataset_verifier = None
        session._worker_capabilities_bound = True
        return None
    admitted = session.admitted_binding
    manager = ProcessBinding(**session.wire.document['launcher'])
    require(manager.executable_sha256 == admitted.expected_peer_image_sha256,
            'original_module_manager_worker_peer_changed')
    signed_worker = verify_retained_manager_worker(root=admitted.layout.code_root, reference=reference,
        expected_version=reference['version'], expected_channel=reference['channel'],
        trust_bytes=admitted.trust_bytes, trust_sha256=admitted.expected_trust_sha256,
        manager_identity=manager, check=session.assert_ready)
    require(signed_worker is not None, 'explicit_module_worker_baseline_required')
    if signed_worker[0].training_worker_file_rows(signed_worker[1]) is None:
        session.worker_capabilities = session.dataset_verifier = None
        session._worker_capabilities_bound = True
        return None
    owner = ModuleWorkerCapabilities(session)
    session.worker_capabilities = owner  # retained before provisioning writes
    session.retain_cleanup_owner(owner, is_drained=lambda: not owner.close_held)
    owner.adapter.check()
    source, python_version = _signed_source_version(owner.adapter.admitted, signed_worker)
    owner.signed_worker, owner.source_root, owner.worker_python_version = signed_worker, source, python_version
    verifier = _bind_training_worker(owner.adapter.admitted, signed_worker, prepare_workspace=True)
    require(verifier is not None, 'admitted_module_worker_unavailable')
    owner.dataset_verifier = ModuleDatasetVerifier(verifier, owner)
    owner.caption_runtime = _bind_manager_caption_runtime(owner.adapter, verifier, signed_worker,
        source_root=source, worker_python_version=python_version, admission_check=owner.adapter.check)
    video = _bind_manager_video_runtime(owner.adapter, verifier, signed_worker,
        source_root=source, worker_python_version=python_version, admission_check=owner.adapter.check)
    owner.adapter.check()
    session.dataset_verifier = owner.dataset_verifier
    session.caption_runtime = owner.caption_runtime
    session.video_decoder_lifetime = owner.video_decoder_lifetime
    (session.admit_manager_video_runtime, session.review_manager_video_setup,
     session.install_manager_video_setup, session.recover_manager_video_setup) = video
    session._worker_capabilities_bound = True
    return owner.dataset_verifier
