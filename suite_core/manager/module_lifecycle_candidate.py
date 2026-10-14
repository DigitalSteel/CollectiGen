"""Inactive Manager-owned disposable component lifecycle; no production defaults.

One owning worker calls these methods. No Qt, endpoint, background polling,
fixture-only LifecycleController injection or Velopack package reinterpretation.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import get_ident
import os
import subprocess
from uuid import uuid4
from datetime import timedelta

from release_contract import verify_artifact, verify_release_manifest, ensure_supported_upgrade
from suite_core.installation import ComponentId, SuiteLayout, InstallMode, _path, _relative
from suite_core.backend_contracts import StableId
from suite_core.manager.host_contracts import (ApprovalGate, ExecutionPermit,
    LaunchPlan, OwnedProcess, OWNER, aware, require)
from suite_core.discovery.component_release_signature import verify_component_release, _json
from suite_core.discovery import component_staging as staging

from .version import VERSION
TEST_COMPONENT = ComponentId('collectigen.disposable-lifecycle-module')


class OperationCancelled(RuntimeError):
    pass


class LifetimeLease:
    """OS exclusion for the whole Manager owner/child/transaction lifetime.

    A durable unresolved-owner marker refuses automatic reacquisition after an
    abrupt host death even if the OS releases its lock. No PID scan/adjudication.
    """
    def __init__(self, root):
        self.root, self.held, self._fd = layout_for_root(root).code_root, False, None
        self.marker = self.root / '.manager-module-owner.json'
        path = bounded(self.root / '.manager-module-lifetime.lock', self.root)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b'\0')
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            require(not bounded(self.marker, self.root).exists(), 'unresolved_lifetime_owner')
            self._record = staging.canonical({'schema': 1, 'purpose': 'SYNTHETIC_TEST_ONLY', 'owner': uuid4().hex})
            staging.atomic_bytes(self.marker, self._record)
        except BaseException as error:
            os.close(fd)
            if isinstance(error, OSError):
                raise ValueError('component_lifetime_owned') from error
            raise
        self._fd, self.held = fd, True

    def release(self):
        require(self.held and self._fd is not None, 'lifetime_lease_required')
        require(staging.read_metadata_bytes(self.marker) == self._record, 'lifetime_owner_changed')
        self.marker.unlink()  # exact own metadata only, after verified owner/child drain
        os.close(self._fd)
        self._fd, self.held = None, False


@dataclass(frozen=True)
class ModuleSnapshot:
    phase: str
    reason: str
    active: tuple
    owned: OwnedProcess | None
    child_active: bool
    exit_code: int | None
    pending_transaction: bool
    ready: bool
    ambiguous: bool
    lease_held: bool
    observed_at: datetime
    serial: int


@dataclass(frozen=True)
class DisposableModuleBinding:
    root: Path
    provider: object
    trust_bytes: bytes
    runner_factory: object
    authority: object
    sentinels: tuple


class DisposableBatchAuthority:
    """One reviewed test plan; fresh internal permits, no eleven user prompts.

    Host approval confirms this exact root/owner, three package identities and
    allowed finite operations. Defaults deny; no production key or credential.
    """
    def __init__(self, *, root, offers, trust_bytes, reviewed=False, approval_reference='',
                 confirmation=None, expires_at=None):
        self.root = layout_for_root(root).code_root
        self._marker_pin = sha256(staging.read_metadata_bytes(self.root / '.synthetic-component-root.json')).hexdigest()
        require(type(offers) is tuple and len(offers) == 3 and {item.offer_id for item in offers} == {'A', 'B', 'C'},
                'exact_batch_offers_required')
        self.offers = offers
        self.trust_pin = sha256(trust_bytes).hexdigest()
        self.reviewed, self.reference, self.confirmation = reviewed is True, approval_reference, confirmation
        self.expires_at = expires_at
        self._issued = {}
        self._confirmed = False
        self._scope = (self.root, self.offers, self.trust_pin, self.reviewed, self.reference,
                       self.confirmation, self.expires_at)

    def _check_scope(self):
        require(self._scope == (self.root, self.offers, self.trust_pin, self.reviewed, self.reference,
            self.confirmation, self.expires_at), 'reviewed_batch_binding_changed')

    def bind(self, now):
        self._check_scope()
        require(not self._confirmed, 'batch_already_bound_to_owner')
        aware(now)
        require(self.reviewed and type(self.reference) is str and bool(self.reference) and callable(self.confirmation),
                'reviewed_batch_approval_required')
        aware(self.expires_at)
        require(now < self.expires_at <= now + timedelta(minutes=15), 'bounded_batch_expiry_required')
        require(self.confirmation(self) is True, 'fresh_batch_approval_not_confirmed')
        self._check_scope()
        self._confirmed = True
        return ApprovalGate(independently_reviewed=True, approval_lookup=self.accepts)

    def accepts(self, permit):
        return self._issued.get(permit.nonce) is permit

    def issue(self, prepared, now):
        self._check_scope()
        aware(now)
        require(self._confirmed and now < self.expires_at and len(self._issued) < 32, 'batch_scope_expired_or_exhausted')
        require(type(prepared) is PreparedOperation and prepared.operation in {'install', 'launch', 'health', 'recover'},
                'finite_batch_operation_required')
        require(sha256(staging.read_metadata_bytes(self.root / '.synthetic-component-root.json')).hexdigest() == self._marker_pin,
                'batch_root_owner_changed')
        require(prepared.offer in self.offers and prepared.plan.component_id == TEST_COMPONENT
                and bounded(prepared.plan.root, self.root) == self.root / 'Apps' / TEST_COMPONENT.value / prepared.offer.version,
                'exact_batch_package_root_required')
        permit = ExecutionPermit(StableId(uuid4().hex), prepared.plan.fingerprint, now,
            min(now + timedelta(minutes=1), self.expires_at), self.reference)
        self._issued[permit.nonce] = permit
        return permit


def layout_for_root(root):
    """Validate one explicit portable root using Suite's existing path contract.

    Layout validation is structural, not execution approval or live-install authority.
    Controller admission still requires the synthetic marker and reviewed host gate.
    """
    code = _path(Path(root))
    staging.guard(code)
    return SuiteLayout(code, code / 'Data', InstallMode.PORTABLE)


def bounded(path, root):
    approved = layout_for_root(root).code_root
    value = _path(Path(path))
    staging.guard(value)
    require(value.is_relative_to(approved), 'explicit_install_root_required')
    return value


@dataclass(frozen=True)
class PackageOffer:
    offer_id: str
    component: ComponentId
    version: str
    archive: Path
    manifest: bytes
    signature: bytes
    program_relative: str


class LocalPackageProvider:
    """Exact injected local transport; admission is still performed by Suite crypto."""
    def __init__(self, offers):
        require(type(offers) is tuple and 0 < len(offers) <= 3, 'bounded_offers_required')
        require(all(type(offer) is PackageOffer for offer in offers), 'typed_offers_required')
        require(len({offer.offer_id for offer in offers}) == len(offers), 'duplicate_offer_id')
        self._offers = offers

    def get(self, offer_id):
        matches = [offer for offer in self._offers if offer.offer_id == offer_id]
        require(len(matches) == 1, 'explicit_local_offer_required')
        return matches[0]


@dataclass(frozen=True)
class PreparedOperation:
    operation: str
    plan: LaunchPlan
    before_token: str
    offer: PackageOffer | None = None
    launch_id: str | None = None
    receipt_path: Path | None = None


@dataclass(frozen=True)
class ProcessObservation:
    identity: OwnedProcess
    active: bool
    exit_code: int | None


class NativeProcessRunner:
    """Retains the direct child handle; never searches PIDs, kills or uses a shell.

    Even an enabled runner cannot start without the controller's consumed fresh
    exact-plan approval. Review and native execution remain separate host gates.
    """
    simulation_only = False

    def __init__(self, *, enabled=False, review_reference=''):
        self.enabled = enabled is True
        self.review_reference = review_reference
        self._handle = None
        self._identity = None
        self._attempted = set()

    def start(self, plan, permit, now, *, consumed_gate):
        require(self.enabled and type(self.review_reference) is str and bool(self.review_reference),
                'native_runner_review_and_execution_disabled')
        require(type(consumed_gate) is ApprovalGate and permit.nonce in consumed_gate.used
                and permit.fingerprint == plan.fingerprint, 'consumed_exact_native_permit_required')
        require(self._handle is None or self._handle.poll() is not None, 'retained_child_still_active')
        require(permit.nonce not in self._attempted, 'native_permit_already_attempted')
        lease = getattr(consumed_gate, 'disposable_lifetime', None)
        require(type(lease) is LifetimeLease and lease.held, 'held_disposable_lifetime_required')
        program, root = bounded(plan.program, lease.root), bounded(plan.root, lease.root)
        require(type(lease) is LifetimeLease and lease.held and root.is_relative_to(lease.root / 'Apps'),
                'held_disposable_lifetime_required')
        require(program.parent == root and program.suffix.lower() == '.exe', 'direct_frozen_entry_required')
        output = bounded(Path(plan.arguments[1]).with_suffix('.process.txt'), lease.root)
        temporary = bounded(output.parent / 'temp', lease.root)
        temporary.mkdir(exist_ok=True)
        # No inherited credentials/config, PATH selection, registry or runtime discovery.
        environment = {'SYSTEMROOT': os.environ.get('SYSTEMROOT', r'C:\Windows'),
                       'TEMP': str(temporary), 'TMP': str(temporary)}
        self._attempted.add(permit.nonce)
        with output.open('xb') as stream:
            self._handle = subprocess.Popen([str(program), *plan.arguments], cwd=root,
                env=environment, shell=False, stdin=subprocess.DEVNULL,
                stdout=stream, stderr=subprocess.STDOUT)
        self._identity = OwnedProcess(permit.nonce, OWNER, plan.fingerprint,
                                     self._handle.pid, now)
        return self._identity

    def observe(self):
        require(self._handle is not None and self._identity is not None, 'direct_child_handle_required')
        code = self._handle.poll()
        return ProcessObservation(self._identity, code is None, code)


class ManagerModuleController:
    """Small real orchestration seam, disabled until explicitly injected.

    Disk mutations delegate to accepted marked-root staging/recovery. Process
    success and signed metadata are separate; health requires exact startup
    receipt AND accepted direct-child exit 0. No GUI readiness is asserted.
    """
    def __init__(self, *, enabled=False, root=None, provider=None, trust_bytes=None,
                 runner=None, gate=None, sentinels=()):
        self.enabled = enabled is True
        self.root = layout_for_root(root).code_root if self.enabled else None
        self.provider, self.runner, self.trust = provider, runner, trust_bytes
        self.gate = gate if gate is not None else ApprovalGate()
        self._owner_thread = get_ident()
        self._provider_get = getattr(provider, 'get', None)
        self._runner_start = getattr(runner, 'start', None)
        self._runner_observe = getattr(runner, 'observe', None)
        self._provider_offers = provider._offers if type(provider) is LocalPackageProvider else None
        self._binding = (self.root, provider, runner, trust_bytes, self.gate)
        self._prepared = None
        self._pending_offer = None
        self._identity = None
        self._launch = None
        self._observed = None
        self._ambiguous = False
        self._reopen_only = False
        self._serial = 0
        self._lease = None
        self.state = 'unconfigured'
        self.reason = 'No disposable lifecycle provider configured.'
        self.events = []
        self._sentinels = ()
        if self.enabled:
            require(type(provider) is LocalPackageProvider and type(trust_bytes) is bytes,
                    'explicit_local_provider_and_public_fixture_trust_required')
            require(TEST_COMPONENT != ComponentId('collectigen.suite-manager'), 'separate_module_identity')
            # Read-only marker admission before any observations directory is created.
            marker = self._marker()
            self._marker_bytes = marker
            require(type(sentinels) is tuple and 1 <= len(sentinels) <= 4, 'synthetic_sentinels_required')
            paths = tuple(self.root.joinpath(*_relative(name)) for name in sentinels)
            require(len(set(paths)) == len(paths) and all(path.is_relative_to(self.root / 'Data')
                    for path in paths), 'data_only_sentinel_paths_required')
            self._sentinels = tuple((bounded(path, self.root), sha256(path.read_bytes()).hexdigest()) for path in paths)
            require(getattr(self.gate, 'disposable_lifetime', None) is None, 'dedicated_lifetime_gate_required')
            self._lease = LifetimeLease(self.root)
            self.gate.disposable_lifetime = self._lease
            self.state, self.reason = 'idle', 'Disposable module controller ready; execution requires fresh approval.'

    def _marker(self):
        path = bounded(self.root / '.synthetic-component-root.json', self.root)
        raw = staging.read_metadata_bytes(path)
        marker = _json(raw, 'synthetic root')
        require(set(marker) == {'purpose', 'owner'} and marker['purpose'] == 'SYNTHETIC_TEST_ONLY'
                and type(marker['owner']) is str and len(marker['owner']) == 32
                and all(char in '0123456789abcdef' for char in marker['owner']),
                'marked_disposable_root_required')
        return raw

    def _check(self):
        require(self.enabled, 'production_lifecycle_unconfigured')
        require(self._lease is not None and self._lease.held and self.gate.disposable_lifetime is self._lease,
                'component_lifetime_required')
        require(get_ident() == self._owner_thread, 'single_manager_worker_required')
        require(self._binding == (self.root, self.provider, self.runner, self.trust, self.gate),
                'host_binding_changed')
        require(self._marker() == self._marker_bytes, 'disposable_owner_changed')
        require(self._lease.root == self.root and self.provider._offers == self._provider_offers,
                'explicit_root_or_package_scope_changed')
        for owner, name, bound in ((self.provider, 'get', self._provider_get),
                                  (self.runner, 'start', self._runner_start),
                                  (self.runner, 'observe', self._runner_observe)):
            current = getattr(owner, name, None)
            require(callable(bound) and getattr(bound, '__self__', None) is owner
                    and getattr(current, '__self__', None) is owner
                    and getattr(current, '__func__', None) is getattr(bound, '__func__', None),
                    'bound_provider_or_runner_changed')
        self._check_data()

    def _check_data(self):
        for path, digest in self._sentinels:
            require(sha256(bounded(path, self.root).read_bytes()).hexdigest() == digest,
                    'synthetic_user_data_changed_hold')

    def _active(self):
        base = bounded(self.root / 'State' / TEST_COMPONENT.value, self.root)
        require(not (base / 'exclusive.lock').exists(), 'component_busy_or_stale_lock')
        return staging.active(self.root, base, TEST_COMPONENT, {'os': 'windows', 'architecture': 'x86_64'})

    def _admit(self, offer_id):
        offer = self._provider_get(offer_id)
        self._check()
        require(type(offer) is PackageOffer and offer.component == TEST_COMPONENT, 'disposable_component_required')
        require(type(offer.offer_id) is str and offer.offer_id == offer_id, 'offer_identity_changed')
        parts = _relative(offer.program_relative)
        require(len(parts) == 1 and parts[0].lower().endswith('.exe'), 'single_frozen_module_entry_required')
        archive = _path(Path(offer.archive))
        staging.guard(archive)
        release = verify_component_release(offer.manifest, offer.signature, self.trust,
            expected_component=TEST_COMPONENT, expected_version=offer.version,
            expected_channel='beta' if '-' in offer.version else 'stable',
            expected_os='windows', expected_architecture='x86_64')
        verify_artifact(offer.archive, release.artifact)
        return offer, release

    def _idle(self):
        require(not self._ambiguous, 'ambiguous_child_outcome_retained')
        if self._identity is not None:
            observed = self.poll()
            require(not observed.active, 'direct_child_still_active')

    def _plan(self, operation, *, before, offer=None, launch_id=None, receipt=None):
        target = self.root if offer is None else self.root / 'Apps' / TEST_COMPONENT.value / offer.version
        program = target / ('unconfigured.exe' if offer is None else offer.program_relative)
        arguments = (() if receipt is None else ('--ready-receipt', str(receipt), '--component', TEST_COMPONENT.value,
            '--version', offer.version, '--launch-id', launch_id))
        raw = staging.canonical({'candidate': VERSION, 'operation': operation, 'root': str(self.root),
            'marker_sha256': sha256(self._marker_bytes).hexdigest(), 'before': before,
            'lifetime_owner_sha256': sha256(self._lease._record).hexdigest(),
            'manifest_sha256': None if offer is None else sha256(offer.manifest).hexdigest(),
            'signature_sha256': None if offer is None else sha256(offer.signature).hexdigest(),
            'trust_sha256': sha256(self.trust).hexdigest(), 'program': str(program), 'arguments': arguments,
            'data': [(str(path), digest) for path, digest in self._sentinels],
            'session': None if self._identity is None else self._identity.session.value})
        return LaunchPlan(TEST_COMPONENT, OWNER, str(target), str(program), arguments,
            '0' * 64 if offer is None else _json(offer.manifest, 'manifest')['artifacts'][0]['sha256'],
            '0' * 64, sha256(raw).hexdigest())

    def _set_prepared(self, operation, offer=None, launch_id=None, receipt=None):
        before = staging.active_token(self._active())
        plan = self._plan(operation, before=before, offer=offer, launch_id=launch_id, receipt=receipt)
        self._prepared = PreparedOperation(operation, plan, before, offer, launch_id, receipt)
        return self._prepared

    def prepare_install(self, offer_id):
        self._check(); self._idle()
        require(self._pending_offer is None, 'acknowledge_health_or_recover_first')
        offer, _ = self._admit(offer_id)
        before = self._active()
        journal_path = self.root / 'State' / TEST_COMPONENT.value / 'transaction.json'
        require(not (journal_path.parent / 'pending-transaction.json').exists(), 'recover_pending_transaction_first')
        if journal_path.exists():
            journal = staging.read_json(journal_path)
            staging.validate_journal(journal, TEST_COMPONENT, _json(self._marker_bytes, 'root')['owner'], self.trust)
            require(journal['phase'] in ('healthy', 'rolled_back', 'abandoned'), 'recover_pending_transaction_first')
            require((journal['target'] if journal['phase'] == 'healthy' else journal['before']) == before,
                    'prior_current_reference_mismatch')
        if before is not None:
            admitted = verify_release_manifest(offer.manifest, offer.signature,
                _json(self.trust, 'trust'), expected_channel='beta' if '-' in offer.version else 'stable')
            ensure_supported_upgrade(before['version'], admitted)
        require(not bounded(self.root / 'Apps' / TEST_COMPONENT.value / offer.version, self.root).exists(),
                'immutable_version_already_exists')
        return self._set_prepared('install', offer)

    def _consume(self, prepared, permit, now):
        self._check(); aware(now)
        require(type(prepared) is PreparedOperation and prepared is self._prepared,
                'exact_prepared_operation_required')
        require(staging.active_token(self._active()) == prepared.before_token, 'stale_active_reference')
        if prepared.offer is not None:
            require(self._admit(prepared.offer.offer_id)[0] == prepared.offer, 'prepared_package_changed')
        self._check()
        self.gate.consume(permit, prepared.plan, now)
        self._prepared = None  # failure cannot reuse consumed authority

    def install(self, prepared, permit, now, *, cancel=None):
        require(prepared.operation == 'install', 'prepared_install_required')
        if cancel is not None and cancel.is_set():
            raise OperationCancelled('cancelled_before_admission')
        self._idle()
        self._consume(prepared, permit, now)
        offer = prepared.offer
        self._pending_offer = offer  # retained before writes, including interrupted staging
        self._reopen_only = False
        self.state = 'installing'
        def cancellation_boundary(phase):
            if cancel is not None and cancel.is_set():
                raise OperationCancelled('cancelled_at_' + phase)
        try:
            target = staging.stage_and_activate(self.root, offer.archive, component=TEST_COMPONENT,
                version=offer.version, os_name='windows', architecture='x86_64',
                manifest_bytes=offer.manifest, signature_bytes=offer.signature, trust_bytes=self.trust,
                expected_active_token=prepared.before_token, operator_approved=True, idle=True,
                failpoint=cancellation_boundary)
            self._check_data()
            # The old direct child was positively drained before this install.
            # Its terminal handle remains with the runner; the new version has no startup observation.
            self._identity, self._observed, self._launch = None, None, None
            self.state, self.reason = 'awaiting_health', 'Installed bytes admitted; normal startup health unacknowledged.'
            self.events.append(('activated', offer.version))
            self._serial += 1
            return target
        except Exception:
            self.state, self.reason = 'held', 'Install outcome retained; explicit recovery required.'
            raise

    def prepare_launch(self):
        self._check(); self._idle()
        require(self._pending_offer is not None and self.state == 'awaiting_health', 'unacknowledged_install_required')
        active = self._active()
        require(active is not None and active['version'] == self._pending_offer.version, 'active_target_changed')
        directory = staging.payload(self.root, active, TEST_COMPONENT)
        profile = staging.verify_directory(directory, active['inventory_sha256'], TEST_COMPONENT, active['version'])
        require(self._pending_offer.program_relative in {item['path'] for item in profile['files']}, 'entry_not_in_signed_inventory')
        self._identity, self._observed, self._launch = None, None, None
        launch_id = uuid4().hex
        observations = bounded(self.root / 'Observations', self.root)
        observations.mkdir(exist_ok=True)
        receipt = observations / (launch_id + '.json')
        require(not receipt.exists(), 'fresh_startup_receipt_required')
        prepared = self._set_prepared('launch', self._pending_offer, launch_id, receipt)
        # LaunchPlan entrypoint pin uses the exact admitted executable, not an invented code claim.
        from dataclasses import replace
        entry_pin = next(item['sha256'] for item in profile['files'] if item['path'] == self._pending_offer.program_relative)
        prepared = replace(prepared, plan=replace(prepared.plan, entrypoint_pin=entry_pin))
        self._prepared = prepared
        return prepared

    def prepare_reopen(self, offer_id):
        self._check(); self._idle()
        require(self._pending_offer is None, 'acknowledge_health_or_recover_first')
        offer, release = self._admit(offer_id)
        active = self._active()
        require(active is not None and active['version'] == offer.version
                and active['inventory_sha256'] == release.artifact.portable_manifest_sha256,
                'reopen_exact_current_package_required')
        journal = staging.read_json(self.root / 'State' / TEST_COMPONENT.value / 'transaction.json')
        require(journal['phase'] == 'healthy' and journal['target'] == active, 'reopen_healthy_reference_required')
        self._pending_offer, self._reopen_only = offer, True
        self.state = 'awaiting_health'
        return self.prepare_launch()

    def launch(self, prepared, permit, now):
        require(prepared.operation == 'launch', 'prepared_launch_required')
        self._idle()
        self._consume(prepared, permit, now)
        require(not prepared.receipt_path.exists(), 'startup_receipt_already_present')
        self._launch, self._ambiguous = prepared, True
        self.state = 'starting'
        identity = self._runner_start(prepared.plan, permit, now, consumed_gate=self.gate)
        self._check()
        require(type(identity) is OwnedProcess and identity.owner == OWNER and identity.session == permit.nonce
                and identity.plan_fingerprint == prepared.plan.fingerprint and type(identity.pid) is int and identity.pid > 0,
                'exact_direct_child_identity_required')
        aware(identity.created_at)
        require(permit.approved_at <= identity.created_at <= now, 'child_creation_time_changed')
        self._identity, self._ambiguous = identity, False
        return identity

    def poll(self):
        self._check()
        require(self._identity is not None and not self._ambiguous, 'owned_child_required')
        try:
            observation = self._runner_observe()
            self._check()
            require(type(observation) is ProcessObservation and observation.identity == self._identity
                    and type(observation.active) is bool and
                    ((observation.active and observation.exit_code is None) or
                     (not observation.active and type(observation.exit_code) is int)), 'owned_observation_required')
            if self._observed is not None and not self._observed.active:
                require(observation == self._observed, 'terminal_child_cannot_resurrect')
            self._observed = observation
            self._serial += 1
            if not observation.active and self._pending_offer is not None and self._launch is not None and self._launch.offer == self._pending_offer:
                self.state = 'awaiting_health' if observation.exit_code == 0 else 'failed_start'
                self.reason = ('Direct child exited; exact ready receipt still required.' if observation.exit_code == 0
                    else 'Direct child failed; no automatic rollback. Explicit code recovery is available.')
            return observation
        except Exception:
            self._ambiguous = True
            self.state, self.reason = 'held', 'Owned observation unknown; retained child blocks retry and recovery.'
            raise

    def _ready(self):
        require(self._launch is not None and self._observed is not None and
                not self._observed.active and self._observed.exit_code == 0, 'successful_direct_child_completion_required')
        path = bounded(self._launch.receipt_path, self.root)
        require(path.stat().st_size <= 4096, 'bounded_health_receipt_required')
        receipt = _json(path.read_bytes(), 'health receipt')
        require(type(receipt.get('schema')) is int and type(receipt.get('pid')) is int,
                'typed_startup_receipt_required')
        require(receipt == {'schema': 1, 'component': TEST_COMPONENT.value,
                'version': self._pending_offer.version, 'launch_id': self._launch.launch_id,
                'pid': self._identity.pid, 'status': 'ready'}, 'exact_startup_health_receipt_required')
        return sha256(path.read_bytes()).hexdigest()

    def prepare_health(self):
        self._check(); self.poll()
        health_hash = self._ready()
        prepared = self._set_prepared('health', self._pending_offer)
        from dataclasses import replace
        prepared = replace(prepared, plan=replace(prepared.plan,
            fingerprint=sha256((prepared.plan.fingerprint + health_hash).encode()).hexdigest()))
        self._prepared = prepared
        self._health_hash = health_hash
        return prepared

    def acknowledge_health(self, prepared, permit, now):
        require(prepared.operation == 'health', 'prepared_health_required')
        self._check(); self.poll()
        require(self._ready() == self._health_hash, 'startup_receipt_changed')
        self._consume(prepared, permit, now)
        target = staging.finish_or_recover(self.root, component=TEST_COMPONENT, trust_bytes=self.trust,
                                           health_verified=not self._reopen_only)
        self._check_data()
        self.events.append(('healthy', self._pending_offer.version))
        self._serial += 1
        self._pending_offer = None
        self.state, self.reason = 'healthy', 'Exact module startup and normal close acknowledged; GUI readiness unqualified.'
        return target

    def prepare_recovery(self):
        self._check(); self._idle()
        require(self._pending_offer is not None, 'unacknowledged_transaction_required')
        return self._set_prepared('recover', self._pending_offer)

    def recover(self, prepared, permit, now):
        require(prepared.operation == 'recover', 'prepared_recovery_required')
        self._idle()
        self._consume(prepared, permit, now)
        before = staging.finish_or_recover(self.root, component=TEST_COMPONENT, trust_bytes=self.trust)
        self._check_data()
        self.events.append(('recovered', self._pending_offer.version))
        self._serial += 1
        self._pending_offer = None
        self.state, self.reason = 'recovered', 'Prior code reference restored and failed code retained; no data rollback claimed.'
        return before

    def snapshot(self):
        self._check()
        return {'candidate_version': VERSION, 'state': self.state, 'reason': self.reason,
                'active': self._active(), 'child_pid': None if self._identity is None else self._identity.pid,
                'ambiguous_child_held': self._ambiguous, 'production_configured': False,
                'sentinels_unchanged': True, 'simulation_only': getattr(self.runner, 'simulation_only', None)}

    def worker_snapshot(self, now):
        self._check(); aware(now)
        active = self._active()
        ready = False
        if self._pending_offer is not None and self._observed is not None and not self._observed.active and self._observed.exit_code == 0:
            try:
                self._ready(); ready = True
            except (ValueError, OSError):
                pass
        return ModuleSnapshot(self.state, self.reason, () if active is None else tuple(sorted(active.items())),
            self._identity, bool(self._observed is not None and self._observed.active),
            None if self._observed is None else self._observed.exit_code, self._pending_offer is not None,
            ready, self._ambiguous, self._lease.held, now, self._serial)

    def close_owner(self):
        self._check(); self._idle()
        require(self._pending_offer is None, 'recover_or_acknowledge_before_owner_close')
        self._lease.release()
        self.state, self.reason = 'closed', 'Exact child and transaction drained; component lifetime released.'
        return True
