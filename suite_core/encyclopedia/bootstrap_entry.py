"""Inert standalone stable-bootstrap backend over original ownership/staging.

No main(), native launch on import, profile activation or bootstrap self-update.
The future frozen entry must embed an independently qualified profile and wire
the visible reader/Manager control transport. Source fixtures are not native proof.
"""
from dataclasses import dataclass
from contextlib import contextmanager
from threading import Event
import base64
from hashlib import sha256

from core.component_acquisition import ComponentAcquisition
from core.encyclopedia_connection import COMPONENT, ReaderConnection, ReaderConnectionStore, review_directory, connection_from_reference, connection_from_release
from core.encyclopedia_installation import EncyclopediaInstallation
from core.encyclopedia_update_binding import (
    StandaloneEncyclopediaBinding, installed_data_contract, require_target_compatibility,
    require_contract_read_coverage,
    require_code_only_obligation_equality,
)
from core.encyclopedia_update_check import EncyclopediaUpdateCheck, EncyclopediaUpdateObservation
from core.manager_launch_owner import DeploymentOwner, LaunchHold
from suite_core.discovery.component_staging import (
    ComponentStageAdmission, ComponentStageBinding, canonical, component_stage_owner, read_json, require, validate_journal,
)

PROFILE = None


@dataclass(frozen=True)
class EncyclopediaManagerDelegate:
    bootstrap: object
    route: object
    subject: object
    manager_generation: str


class StandaloneEncyclopediaBootstrap:
    """Retains the lease beyond reader exit; one original confirmed request.

    Construction requires an already acquired owner and exact authenticated
    installation. It never acquires/adopts another deployment or repairs a flat
    ZIP. A Manager delegate controls this root, not its own staging destination.
    """
    def __init__(self, binding, acquisition, owner, *, _enrollment_subject=None):
        require(type(binding) is StandaloneEncyclopediaBinding and type(owner) is DeploymentOwner,
                'original_standalone_binding_and_owner_required')
        require(type(acquisition) is ComponentAcquisition and acquisition.trust == binding.trust_bytes
                and acquisition.trust_pin == binding.trust_sha256, 'original_component_acquisition_required')
        zone = binding.data_root / 'Encyclopedia/UpdateCache'
        require(acquisition.root != zone and acquisition.root.is_relative_to(zone), 'original_component_cache_required')
        self.binding, self.acquisition, self.owner = binding, acquisition, owner
        self.request = self.acquired = self.delegate = None
        self._operation = None
        self._original_install_check = None
        self.cancel = Event()
        self._installation = EncyclopediaInstallation(acquisition, ReaderConnectionStore(binding.settings_path),
            admission=ComponentStageAdmission(self._hold), native_health=self._native_health, schema2_identity=binding.schema2_identity)
        self.update_check = EncyclopediaUpdateCheck(acquisition, self.update_observation,
            channel=('stable' if binding.initial.selected.maturity == 'stable' else 'beta') if binding.initial.selected.release_schema == 2 else ('beta' if '-' in binding.initial.selected.version else 'stable'))
        # Issue last: all fallible service construction precedes the owner state
        # handoff. No child is created, entry forwarded or profile activated here.
        self.subject = (owner.arm_standalone_encyclopedia(binding) if _enrollment_subject is None
            else owner.handoff_standalone_enrollment(_enrollment_subject, binding))

    @contextmanager
    def _hold(self, component):
        if self._operation == 'install_metadata':
            require(callable(self._original_install_check), 'original_installation_handoff_required')
            self._original_install_check()
        with self.owner.standalone_encyclopedia_stage_hold(self.subject, component) as original:
            def check():
                original.check()
                if self._operation == 'install' and self.delegate is not None:
                    self._manager_owned(self.delegate)
                if self._operation == 'install_metadata':
                    self._original_install_check()
                if self._operation == 'retry_publication':
                    self._verify_publication_request(self.request, original.root,
                        original.root / 'State' / COMPONENT.value, original.owner)
            check()
            yield ComponentStageBinding(original.root, original.owner, check, original.data_root)
            check()

    def _native_health(self, selected):
        require(self.owner.poll_standalone_encyclopedia(self.subject), 'original_reader_still_running')
        return self.subject.reader.native_health(selected)

    @property
    def close_held(self):
        return bool(self.request is not None or self.owner._standalone_encyclopedia_attempt
            or self.subject.reader.close_held or self.owner.record.get('component_stage')
            or self.binding.current().phase == 'activated')

    def update_observation(self):
        self.owner._standalone_encyclopedia_owned(self.subject)
        current = self.binding.current()
        ownership = 'standalone'
        if self.delegate is not None:
            try:
                self._manager_owned(self.delegate)
            except (LaunchHold, ValueError, OSError):
                ownership = 'unknown'
            else:
                ownership = 'manager'
        identity = self.binding.identity
        if self.binding.schema2_identity:
            identity = sha256(canonical(dict(binding=identity, active=current.active_sha256,
                journal=current.journal_sha256, settings=current.settings_sha256,
                release=current.release_sha256, selected=current.selected.mapping(),
                trust=self.binding.trust_sha256, owner=self.owner.record['owner'], ownership=ownership,
                delegate=None if self.delegate is None else self.delegate.manager_generation))).hexdigest()
        return EncyclopediaUpdateObservation(current.selected.version, ownership, identity,
            current.phase == 'activated' or self.request is not None,
            current.selected.release_schema, current.selected.maturity)

    def installed_offer_context(self):
        """Original host observer for explicitly composed metadata acquisition."""
        return self.update_observation().installed_context()

    def launch_current(self):
        return self.owner.launch_standalone_encyclopedia(self.subject)

    def poll_reader(self):
        return self.owner.poll_standalone_encyclopedia(self.subject)

    def _manager_owned(self, delegate):
        from suite_core.manager.bootstrap_entry import NormalOwnerRoute
        require(type(delegate) is EncyclopediaManagerDelegate and delegate is self.delegate
                and delegate.bootstrap is self and delegate.subject is self.subject,
                'original_manager_delegate_required')
        route = delegate.route
        require(type(route) is NormalOwnerRoute and type(route.owner) is DeploymentOwner
                and route.owner is not self.owner and route.owner.record['owner'] == delegate.manager_generation,
                'original_manager_root_required')
        # Reuse the existing authentic native peer/Job/lease/epoch stage gate.
        # This temporary grant never authorizes writes under Manager's root.
        scope = route.owner.component_stage_scope(route, 'begin', None)
        route.owner.component_stage_scope(route, 'release', scope['scope_id'])

    def delegate_to_manager(self, route, *, confirmed=False):
        self.owner._standalone_encyclopedia_owned(self.subject)
        require(confirmed is True and self.delegate is None and self.request is None,
                'explicit_single_manager_delegate_required')
        candidate = EncyclopediaManagerDelegate(self, route, self.subject, route.owner.record['owner'])
        self.delegate = candidate
        try:
            self._manager_owned(candidate)
        except BaseException:
            self.delegate = None
            raise
        return candidate

    def hand_back_to_standalone(self, delegate, *, confirmed=False):
        # Explicit local handback can work with an offline Manager; physical root
        # ownership never moved, and a pending confirmed attempt cannot be erased.
        self.owner._standalone_encyclopedia_owned(self.subject)
        require(confirmed is True and type(delegate) is EncyclopediaManagerDelegate
                and delegate is self.delegate and self.request is None,
                'explicit_idle_manager_handback_required')
        self.delegate = None

    def confirm_update(self, offer, *, confirmed=False, delegate=None):
        if self.delegate is not None:
            self._manager_owned(delegate)
        else:
            require(delegate is None, 'standalone_has_no_manager_delegate')
        request = self.owner.confirm_standalone_encyclopedia(self.subject, self.acquisition, offer, confirmed=confirmed)
        self.request = request
        self.cancel.clear()
        return request

    def cancel_update(self, request):
        require(request is self.request, 'exact_original_confirmed_update_required')
        self.cancel.set()
        self.owner.cancel_standalone_encyclopedia_update(self.subject, request)
        self.request = None

    def request_cancel(self):
        self.cancel.set()  # Running transport cancellation does not claim rollback.

    def _confirmed_metadata_checker(self, request, delegate):
        acquisition, captured_delegate = self.acquisition, self.delegate
        owner, subject, binding, installation = self.owner, self.subject, self.binding, self._installation

        def check_current():
            require(request is not None and request is self.request
                    and self.acquisition is acquisition and self.delegate is captured_delegate
                    and self.owner is owner and self.subject is subject and self.binding is binding
                    and self._installation is installation,
                    'exact_original_confirmed_update_required')
            self.owner.check_standalone_metadata_request(self.subject, request)
            require(self.binding.schema2_identity, 'reader_identity_mode_required')
            if captured_delegate is not None:
                self._manager_owned(delegate)
            else:
                require(delegate is None, 'standalone_has_no_manager_delegate')
            context = request.offer.installed_context
            live = self.installed_offer_context()
            require(context is not None and not context.pending and live.pending
                    and (live.component_id, live.version, live.schema_version, live.maturity, live.observation_id)
                    == (context.component_id, context.version, context.schema_version,
                        context.maturity, context.observation_id), 'confirmed_metadata_observation_changed')
            require(acquisition._observe_installed() == live, 'original_metadata_observation_required')
        return check_current

    def revalidate_confirmed_metadata(self, request, *, delegate=None):
        """Explicit original-request metadata review; never starts acquisition."""
        acquisition = self.acquisition
        check_current = self._confirmed_metadata_checker(request, delegate)
        # All owner work remains in short serialized checks, never across HTTP.
        offer = request.offer if request is not None and request is self.request else None
        return acquisition.revalidate_exact_metadata(offer, check_current=check_current,
                                                     should_cancel=self.cancel.is_set)

    def apply_confirmed_metadata(self, request, *, progress=None, delegate=None):
        """Explicit original-request source route; no ordinary acquisition opt-in."""
        require(request is not None and request is self.request, 'exact_original_confirmed_update_required')
        acquisition, installation = self.acquisition, self._installation
        owner, subject, binding, captured_delegate = self.owner, self.subject, self.binding, self.delegate
        require(binding.schema2_identity and acquisition.schema2_metadata, 'original_metadata_mode_required')
        if captured_delegate is not None:
            self._manager_owned(delegate)
        else:
            require(delegate is None, 'standalone_has_no_manager_delegate')
        phase = owner.record.get('standalone_update', {}).get('phase')
        if phase == 'waiting_for_close':
            owner.begin_standalone_encyclopedia_update(subject, request)
        else:
            owner.check_standalone_metadata_request(subject, request)
        check_current = self._confirmed_metadata_checker(request, delegate)
        self._operation = 'install_metadata'
        self.acquired = None
        try:
            acquired = acquisition._acquire_original_metadata(request.offer, check_current=check_current,
                progress=progress, should_cancel=self.cancel.is_set)
            self.acquired = acquired
            check_current()
            require_target_compatibility(request.current.selected, acquired, original_contract=request.data_obligations)

            def stage_check():
                require(self.request is request and self.acquired is acquired and acquired.offer is request.offer
                    and self.acquisition is acquisition and self._installation is installation
                    and self.owner is owner and self.subject is subject and self.binding is binding
                    and self.delegate is captured_delegate, 'original_installation_handoff_required')
                owner.check_standalone_update_request(subject, request)
                if captured_delegate is not None:
                    self._manager_owned(delegate)
                acquisition._metadata_offer_current(request.offer, None)

            self._original_install_check = stage_check
            reviewed = installation._review_local(request.offer)
            require(reviewed.offer is request.offer and reviewed.installed.active_sha256 == request.current.active_sha256
                and reviewed.installed.journal_sha256 == request.current.journal_sha256
                and reviewed.settings.digest == request.current.settings_sha256, 'confirmed_installation_review_changed')
            check_current()
            def handoff_check(candidate):
                require(candidate is acquired and self.acquired is acquired,
                        'original_acquired_handoff_required')
                check_current()
            return installation._install_original_acquired(reviewed, acquired, check_original=handoff_check,
                progress=progress, should_cancel=self.cancel.is_set)
        except BaseException:
            try:
                owner.abandon_unpublished_standalone_update(subject, request)
            except (LaunchHold, ValueError, OSError):
                pass
            else:
                if self.request is request:
                    self.request = None
            raise
        finally:
            self._operation = None
            self._original_install_check = None

    def apply_confirmed(self, request, *, progress=None, delegate=None):
        require(request is self.request and request is not None, 'exact_original_confirmed_update_required')
        if self.delegate is not None:
            self._manager_owned(delegate)
        self.owner.begin_standalone_encyclopedia_update(self.subject, request)
        self._operation = 'install'
        try:
            if self.cancel.is_set():
                raise InterruptedError('Standalone update cancelled before acquisition.')
            self.acquired = self.acquisition.acquire(request.offer, progress=progress, should_cancel=self.cancel.is_set)
            if self.cancel.is_set():
                raise InterruptedError('Standalone update cancelled after acquisition; installation unchanged.')
            require_target_compatibility(request.current.selected, self.acquired, original_contract=request.data_obligations)
            if self.cancel.is_set():
                raise InterruptedError('Standalone update cancelled before installation review.')
            reviewed = self._installation.review(request.offer)
            require(reviewed.offer is request.offer and reviewed.installed.active_sha256 == request.current.active_sha256
                    and reviewed.installed.journal_sha256 == request.current.journal_sha256
                    and reviewed.settings.digest == request.current.settings_sha256, 'confirmed_installation_review_changed')
            return self._installation.install(reviewed, confirmed=True, progress=progress, should_cancel=self.cancel.is_set)
        except BaseException:
            # Only exact unchanged authenticated state may retire a prepublication
            # failure. A pending/activated transaction remains for explicit recovery.
            try:
                self.owner.abandon_unpublished_standalone_update(self.subject, request)
            except (LaunchHold, ValueError, OSError):
                pass
            else:
                self.request = None
            raise
        finally:
            self._operation = None

    def finish_target_health(self, request):
        require(request is not None and request is self.request, 'exact_original_confirmed_update_required')
        require(self.owner.record.get('standalone_update', {}).get('phase') == 'applying',
                'original_target_health_operation_required')
        self._installation.confirm_health()
        current = self.owner.settle_standalone_encyclopedia_update(self.subject, request)
        self.request = self.acquired = None
        return current

    def finish_initial_health(self):
        """Initial activated baseline needs the original reader's real outcome."""
        self.owner._standalone_encyclopedia_owned(self.subject)
        require(self.request is None and 'standalone_update' not in self.owner.record
                and self.binding.initial.phase == 'activated', 'original_initial_health_operation_required')
        current, initial = self.binding.current(), self.binding.initial
        require(current.phase == 'activated' and current.selected == initial.selected
                and current.active_sha256 == initial.active_sha256
                and current.journal_sha256 == initial.journal_sha256
                and current.release_sha256 == initial.release_sha256, 'original_initial_selection_changed')
        self._installation.confirm_health()
        return self.binding.current()

    def _verify_publication_request(self, request, root, base, owner):
        """Every save-only handoff stays bound to the original code outcome."""
        self.owner._standalone_encyclopedia_owned(self.subject)
        record = self.owner.record.get('standalone_update')
        require(request is not None and request is self.request
                and request is self.owner._standalone_encyclopedia_update
                and record is not None and record['identity'] == request.identity,
                'exact_original_publication_request_required')
        require(record['phase'] in ('applying', 'recovering'), 'original_publication_operation_required')
        recovering = record['phase'] == 'recovering'
        expected = request.current.selected if recovering else connection_from_release(root, request.offer.release)
        installed = self._installation._observe(root, base, owner)
        require(not installed.pending and installed.selected == expected,
                'original_publication_code_selection_changed')
        journal = read_json(base / 'transaction.json')
        validate_journal(journal, COMPONENT, owner, self.binding.trust_bytes, **({"allow_schema2": True} if self.binding.schema2_identity else {}))
        expected_release = request.current.release_sha256 if recovering else request.offer.release.manifest_sha256
        require(sha256(base64.b64decode(journal['manifest'], validate=True)).hexdigest() == expected_release,
                'original_publication_release_changed')
        if recovering:
            require(installed.journal_sha256 == request.current.journal_sha256,
                    'original_prior_publication_journal_changed')
        pending = base / 'connection-pending.json'
        if pending.exists():
            intent = read_json(pending)
            target = expected.mapping()
            offered = connection_from_release(root, request.offer.release).mapping()
            require(set(intent) == {'schema', 'journal_sha256', 'target', 'prior_selection'}
                    and type(intent['schema']) is int and intent['schema'] == 1 and intent['target'] == target
                    and intent['journal_sha256'] == sha256(canonical(journal)).hexdigest()
                    and intent['prior_selection'] in (request.current.selected.mapping(), offered),
                    'original_connection_publication_intent_changed')
        else:
            observed = self._installation.store.observe()
            require(self._installation.store.selected(observed) == expected,
                    'original_connection_publication_readback_required')

    def retry_connection(self, request):
        """Explicit original-request save retry; no acquire, stage or rollback."""
        require(request is not None and request is self.request, 'exact_original_publication_request_required')
        base = self.binding.root / 'State' / COMPONENT.value
        require((base / 'connection-pending.json').exists(), 'pending_original_connection_publication_required')
        self._operation = 'retry_publication'
        try:
            return self._installation.retry_connection()
        finally:
            self._operation = None

    def recover_update(self, request, *, confirmed=False):
        require(confirmed is True and request is self.request and request is not None, 'explicit_original_update_recovery_required')
        require(self.poll_reader(), 'close_original_reader_before_recovery')
        base = self.binding.root / 'State' / COMPONENT.value
        pending = base / 'pending-transaction.json'
        journal = read_json(pending if pending.exists() else base / 'transaction.json')
        validate_journal(journal, COMPONENT,
            component_stage_owner(self.binding.root, self.binding.data_root, COMPONENT), self.binding.trust_bytes,
            **({"allow_schema2": True} if self.binding.schema2_identity else {}))
        require(base64.b64decode(journal['manifest'], validate=True) == request.offer.manifest_bytes
                and base64.b64decode(journal['signature'], validate=True) == request.offer.signature_bytes,
                'original_target_recovery_transaction_required')
        target = self.binding.root / journal['target']['directory']
        if target.exists():
            selected = review_directory(target, expected=connection_from_reference(self.binding.root, journal['target']))
            require(installed_data_contract(request.current.selected) == request.data_obligations,
                    'original_data_obligations_changed')
            require_code_only_obligation_equality(request.data_obligations, installed_data_contract(selected))
            require_contract_read_coverage(request.data_obligations, installed_data_contract(request.current.selected))
            require_contract_read_coverage(installed_data_contract(selected),
                installed_data_contract(request.current.selected))
        else:
            require(self.acquired is not None and self.acquired.offer is request.offer, 'retained_target_compatibility_required')
            require_target_compatibility(request.current.selected, self.acquired, original_contract=request.data_obligations)
        self.owner.begin_standalone_encyclopedia_recovery(self.subject, request)
        return self._installation.recover(confirmed=True)

    def finish_recovery_health(self, request):
        require(request is not None and request is self.request, 'exact_original_confirmed_update_required')
        require(self.owner.record.get('standalone_update', {}).get('phase') == 'recovering'
                and self.binding.current().selected == request.current.selected,
                'exact_original_prior_reader_required')
        current = self.owner.settle_standalone_encyclopedia_update(self.subject, request)
        self.request = self.acquired = None
        return current

    def close(self):
        return self.owner.close_standalone_encyclopedia(self.subject)


def from_compiled_profile(*args, **kwargs):
    if PROFILE is None:
        return None
    require(not args and not kwargs, 'compiled_standalone_profile_arguments_forbidden')
    from .entry_binding import StandaloneEntryProfile, create_root_entry, create_enrollment_entry
    from core.encyclopedia_enrollment import EncyclopediaEnrollmentContext, compiled_enrollment_needed
    require(type(PROFILE) is StandaloneEntryProfile and PROFILE.channel in ('stable', 'beta'),
        'typed_explicit_compiled_profile_required')
    context = EncyclopediaEnrollmentContext(PROFILE.root, PROFILE.data_root, PROFILE.settings_path,
        PROFILE.bootstrap_sha256, PROFILE.trust_bytes, PROFILE.trust_sha256)
    if compiled_enrollment_needed(context):
        return create_enrollment_entry(PROFILE)
    return create_root_entry(PROFILE)
