"""Synchronous Manager update seam over existing acquisition/admission/owners.

No default network, process execution, package promotion or data mutation.
The acquisition cache is the verified stage; launcher owns deployment writes.
"""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from hashlib import sha256
import json

from core.component_acquisition import ComponentAcquisition, ComponentOffer
from core.manager_stock_apply import StockApplyRequest, queue_stock_apply, readback
from core.connected_version_observation import VersionBinding, VersionObservation, VersionStatus, derive_version_status
from core.connected_version_projection import newer_offer_comparison
from suite_core.discovery.component_staging import require
from suite_core.discovery.manager_runtime_detached_admission import (
    MANAGER, PROFILE, DetachedAdmissionPlan, admit_detached, produce_detached_inventory,
)


@dataclass(frozen=True)
class PreparedManagerUpdate:
    offer: ComponentOffer
    artifact: Path
    proof: bytes
    admission: DetachedAdmissionPlan

    @property
    def production_install_authorized(self):
        return False


@dataclass(frozen=True)
class ManagerVersionCheck:
    """One existing check worker's immutable result; no installation authority."""
    offer: ComponentOffer | None
    status: VersionStatus

    def __post_init__(self):
        require((self.offer is None or type(self.offer) is ComponentOffer)
            and type(self.status) is VersionStatus, 'immutable_manager_version_check_required')


def reviewed_stage_identity(prepared):
    require(type(prepared) is PreparedManagerUpdate, 'exact_reviewed_stage_required')
    plan = prepared.admission
    return dict(version=plan.version, channel=plan.channel, artifact=str(prepared.artifact),
        artifact_sha256=plan.artifact_sha256, artifact_size=prepared.offer.release.artifact.size_bytes,
        release_sha256=plan.release_sha256, trust_sha256=plan.trust_sha256,
        inventory_sha256=plan.inventory_sha256)


@dataclass(frozen=True)
class ManagerUpdateHostBinding:
    """Explicit existing-service host inputs and original-root action adapters.

    Presence is configuration, never native apply permission. No default trust,
    transport or process/recovery provider is constructed here.
    """
    service: object
    transport_id: str
    child_cache_root: Path
    apply_review: object = None
    repair_review: object = None

    def profile(self, admitted):
        require(type(self.service) is ManagerUpdateService, 'explicit_manager_update_service_required')
        return self.service.host_profile(admitted,transport_id=self.transport_id,
            child_cache_root=self.child_cache_root)

    def handle(self, route, request):
        operation=request['operation'];body=request['body']
        if operation=='configure':
            require(body=={}, 'exact_update_configuration_request')
            return dict(status='reviewed',result=route.manager_update_profile)
        if operation=='apply':
            import zipfile
            try:
                prepared=self.service.admit_reviewed_stage(body,cache_root=self.child_cache_root)
            except (ValueError,OSError,zipfile.BadZipFile) as error:
                self.service._prepared=None
                return dict(status='held',result=dict(reason=str(error)[:240]))
            if self.apply_review is None:
                return dict(status='held',result=dict(reason='stock_hook_transport_unqualified'))
            require(callable(self.apply_review), 'original_root_apply_review_required')
            return self.apply_review(route,prepared,request)
        require(operation=='repair' and set(body)=={'transaction_id','binding_sha256'},
            'exact_original_repair_request')
        if self.repair_review is None:
            return dict(status='held',result=dict(reason='original_root_repair_adapter_unqualified'))
        require(callable(self.repair_review), 'original_root_repair_review_required')
        from core.manager_self_update_recovery import ManagerRecovery, digest
        recovery=getattr(route,'manager_update_recovery',None)
        require(type(recovery) is ManagerRecovery and recovery.paths['root']==route.owner.root,
            'original_root_repair_transaction_required')
        journal=recovery.load()
        require(body['transaction_id']==recovery.id and body['binding_sha256']==digest(journal['binding'])
            and journal['phase'] in {'startup_failed','preserving_b','restoring_data','data_restored'},
            'correlated_original_repair_transaction_required')
        return self.repair_review(route,body,request)


class ManagerUpdateService:
    """One synchronous UI-owner flow; no watcher or second transaction journal."""

    def __init__(self, acquisition, *, current_version, channel, installed_selection=None):
        require(type(acquisition) is ComponentAcquisition, "existing_acquisition_required")
        require(type(current_version) is str and current_version, "installed_version_required")
        require(channel in ("stable", "beta"), "explicit_channel_required")
        require(acquisition.os == "windows" and acquisition.arch == "x86_64", "manager_platform")
        self.acquisition = acquisition
        self.current_version, self.channel = current_version, channel
        self.installed_selection = installed_selection
        if installed_selection is not None:
            from core.manager_successor_admission import OrdinarySuccessorBinding
            require(type(installed_selection) is OrdinarySuccessorBinding
                and installed_selection.revalidate().version==current_version,
                'independently_signed_current_successor_required')
        self._offer = self._prepared = None

    def bind_normal_entry(self, admitted):
        """Explicit trusted host binding; never select trust, transport or cache.

        The factory is called only after inherited native peer acceptance.
        Its cache must belong to this deployment's separate Manager cache zone.
        """
        from suite_core.manager.entry_binding import FrozenEntryBinding
        from suite_core.manager.version import VERSION
        require(type(admitted) is FrozenEntryBinding, 'complete_update_entry_required')
        if self.installed_selection is None:
            require(self.current_version == VERSION, 'exact_current_manager_update_service_required')
        else:
            selection=self.installed_selection.revalidate()
            require(selection.version==self.current_version and selection.root==admitted.layout.code_root
                and selection.settings_path==admitted.settings_path,
                'independently_signed_current_successor_required')
        self.acquisition._trust()
        self.acquisition._check(None)
        zone = admitted.layout.data_root / 'Manager/UpdateCache'
        require(self.acquisition.root.is_relative_to(zone)
                and self.acquisition.root != zone, 'deployment_owned_update_cache_required')
        return self

    def host_profile(self, admitted, *, transport_id, child_cache_root=None):
        """Explicit versioned public configuration, not permission to install."""
        self.bind_normal_entry(admitted)
        require(type(transport_id) is str and 0<len(transport_id)<=128 and '\0' not in transport_id,
            'reviewed_transport_identity_required')
        cache=self.acquisition.root if child_cache_root is None else Path(child_cache_root)
        require(cache.is_absolute() and cache.is_relative_to(admitted.layout.data_root/'Manager/UpdateCache')
            and cache!=admitted.layout.data_root/'Manager/UpdateCache', 'deployment_owned_update_cache_required')
        profile = dict(schema=1, current_version=self.current_version, channel=self.channel,
            trust_sha256=self.acquisition.trust_pin, transport_id=transport_id,
            data_root=str(admitted.layout.data_root), settings_path=str(admitted.settings_path),
            cache_root=str(cache))
        profile['profile_sha256'] = sha256(json.dumps(profile,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return profile

    def admit_reviewed_stage(self, identity, *, cache_root, should_cancel=None):
        """Root independently discovers signed metadata and reads child staged bytes.

        UI fields are comparisons only; never trust an enabled button or supplied
        signature/admission object. Caller qualifies the original child's cache.
        """
        require(type(identity) is dict and set(identity)=={'version','channel','artifact',
            'artifact_sha256','artifact_size','release_sha256','trust_sha256','inventory_sha256'},
            'exact_reviewed_stage_identity_required')
        artifact = Path(identity['artifact'])
        require(artifact.is_absolute() and artifact.parent.is_relative_to(Path(cache_root))
                and artifact != Path(cache_root), 'original_child_cache_scope_required')
        offer = self.discover(should_cancel=should_cancel)
        require(offer is not None, 'independent_signed_offer_required')
        expected = dict(version=offer.release.software_version, channel=offer.release.channel,
            artifact_sha256=offer.release.artifact.sha256, artifact_size=offer.release.artifact.size_bytes,
            release_sha256=sha256(offer.manifest_bytes).hexdigest(), trust_sha256=offer.trust_sha256,
            inventory_sha256=offer.release.artifact.portable_manifest_sha256)
        require(all(identity[name]==value for name,value in expected.items()), 'changed_reviewed_stage_identity')
        if should_cancel and should_cancel(): raise InterruptedError('Root update review cancelled safely.')
        readback(artifact, expected['artifact_size'], expected['artifact_sha256'])
        proof = produce_detached_inventory(artifact, inventory_profile=PROFILE,
            expected_version=expected['version'], expected_channel=self.channel)
        admission = self._admit(artifact, offer, proof, trust_pin=offer.trust_sha256)
        if should_cancel and should_cancel(): raise InterruptedError('Root update review cancelled safely.')
        self._prepared = PreparedManagerUpdate(offer,artifact,proof,admission)
        return self._prepared

    def queue_owned(self, prepared, owner, request, *, native_review, create_process):
        """Use the original owner's single-attempt stock dispatch, never a receipt factory."""
        from core.manager_launch_owner import DeploymentOwner
        from core import manager_stock_apply
        require(type(owner) is DeploymentOwner, 'original_deployment_owner_required')
        self._request_plan(prepared, request)
        return owner.queue_maintenance(manager_stock_apply, request,
            verify_admission=lambda original: self.revalidate(prepared, artifact=original.package),
            native_review=native_review, create_process=create_process)

    def _request_plan(self, prepared, request):
        plan = self.revalidate(prepared)
        require(type(request) is StockApplyRequest
                and request.current_version == self.current_version
                and request.target_version == plan.version and request.channel == plan.channel
                and request.package_sha256 == plan.artifact_sha256
                and request.package_size == prepared.offer.release.artifact.size_bytes
                and request.release_sha256 == plan.release_sha256
                and request.trust_sha256 == plan.trust_sha256
                and request.inventory_sha256 == plan.inventory_sha256,
                'stock_request_stage_binding')
        return plan

    def discover(self, *, should_cancel=None):
        # Failed rediscovery also invalidates the preceding review/stage.
        self._offer = self._prepared = None
        offers = self.acquisition.discover(channel=self.channel,
            installed_versions={MANAGER.value: self.current_version}, should_cancel=should_cancel)
        selected = tuple(o for o in offers if o.release.component_id == MANAGER)
        require(len(selected) <= 1, "ambiguous_manager_offer")
        if not selected:
            return None
        offer = selected[0]
        require(offer.operation == "update" and offer.current_version == self.current_version,
                "manager_self_update_only")
        self._offer = offer
        return offer

    def discover_with_observation(self, *, running_version, should_cancel=None):
        """Observe only the already-admitted selection around existing discovery.

        The original discover/prepare/Apply contracts and current_version remain
        unchanged. Verification failures are status evidence, never new selection
        or permission. Caller uses the existing retained update worker.
        """
        def cancelled():
            if should_cancel and should_cancel():
                raise InterruptedError('Manager version check cancelled safely.')

        # Match ordinary rediscovery's invalidation even if observation is
        # cancelled before the release transport is reached.
        self._offer = self._prepared = None
        cancelled()
        selection = self.installed_selection
        binding = VersionBinding(MANAGER.value, 'original-manager-update-host', self.current_version,
            platform=self.acquisition.os, architecture=self.acquisition.arch, channel=self.channel)
        if selection is not None:
            binding = replace(binding, installation_ref=str(selection.root),
                binding_revision=selection.initial_contract_sha256 + ':' + selection.trust_sha256,
                source_identity=str(selection.root))
        started = datetime.now(timezone.utc)
        running = VersionObservation(binding, 'running', 'observed', 'configured', running_version,
            'manager-running-build', started, started, timedelta(seconds=30), comparator_id='semver')

        def observe(previous=None):
            attempted = datetime.now(timezone.utc)
            if selection is None:
                return VersionObservation(binding, 'installed', 'unknown', 'configured', None,
                    'manager-installed-selection', attempted, None, timedelta(seconds=30))
            try:
                require(self.installed_selection is selection, 'installed_selection_binding_changed')
                fresh = selection.revalidate()  # Existing read-only signed owner path.
                require(fresh is selection and str(fresh.root) == binding.source_identity
                    and fresh.initial_contract_sha256 + ':' + fresh.trust_sha256 == binding.binding_revision
                    and fresh.version == self.current_version, 'installed_selection_binding_changed')
                fingerprint = fresh.contract['manager_sha256']
                require(type(fingerprint) is str and len(fingerprint) == 64
                    and all(c in '0123456789abcdef' for c in fingerprint), 'installed_manager_fingerprint')
                if previous is not None and previous.state == 'observed':
                    require((fresh.version, fingerprint) == (previous.raw_version, previous.evidence_ref),
                        'installed_selection_changed_during_check')
                observed = datetime.now(timezone.utc)
                return VersionObservation(binding, 'installed', 'observed', 'locally_verified', fresh.version,
                    'manager-signed-installed-selection', attempted, observed, timedelta(seconds=30),
                    comparator_id='semver', installation_condition='unchanged', evidence_ref=fingerprint)
            except (OSError, ValueError, KeyError):
                if previous is not None:
                    return replace(previous, state='invalid', attempted_at=attempted,
                        failure_code='installed_selection_verification_failed',
                        installation_condition='changed_requires_review')
                return VersionObservation(binding, 'installed', 'invalid', 'configured', None,
                    'manager-installed-selection', attempted, None, timedelta(seconds=30),
                    failure_code='installed_selection_verification_failed',
                    installation_condition='changed_requires_review')

        before = observe()
        cancelled()
        offer = self.discover(should_cancel=should_cancel)
        cancelled()
        after = observe(before)
        cancelled()
        # A failed first verification cannot be promoted by a later success in
        # the same attempt; the user must request another coherent check.
        if before.state != 'observed' and after.state == 'observed':
            after = replace(after, state='invalid', failure_code='installed_selection_changed_during_check',
                installation_condition='changed_requires_review')
        checked = datetime.now(timezone.utc)
        comparison = newer_offer_comparison(binding, source_ref='manager-signed-component-discovery',
            checked_at=checked, latest_identity=offer.release.software_version if offer else None,
            authoritative=offer is not None, comparator_id='semver' if offer else None,
            matching_artifact_available=True if offer else None,
            evidence_ref=offer.trust_sha256 if offer else None)
        return ManagerVersionCheck(offer, derive_version_status(binding, after, running, comparison, now=checked))

    def _admit(self, artifact, offer, proof, *, trust_pin):
        return admit_detached(artifact, inventory_profile=PROFILE,
            detached_inventory_bytes=proof, manifest_bytes=offer.manifest_bytes,
            signature_bytes=offer.signature_bytes, trust_bytes=self.acquisition.trust,
            expected_trust_sha256=trust_pin,
            expected_inventory_sha256=offer.release.artifact.portable_manifest_sha256,
            expected_version=offer.release.software_version, expected_channel=self.channel,
            current_version=self.current_version)

    def prepare(self, offer, *, progress=None, should_cancel=None):
        require(offer is self._offer and type(offer) is ComponentOffer, "current_manager_offer_required")
        self._prepared = None
        acquired = self.acquisition.acquire(offer, progress=progress, should_cancel=should_cancel)
        if should_cancel and should_cancel():
            raise InterruptedError("Manager update staging cancelled safely.")
        require(offer.current_version == self.current_version
                and offer.release.channel == self.channel, "changed_installed_context")
        proof = produce_detached_inventory(acquired.artifact, inventory_profile=PROFILE,
            expected_version=offer.release.software_version, expected_channel=self.channel)
        admission = self._admit(acquired.artifact, offer, proof, trust_pin=offer.trust_sha256)
        if should_cancel and should_cancel():
            raise InterruptedError("Manager update staging cancelled safely.")
        require(offer is self._offer, "changed_manager_offer")
        require(offer.current_version == self.current_version
                and offer.release.channel == self.channel, "changed_installed_context")
        self._prepared = PreparedManagerUpdate(offer, acquired.artifact, proof, admission)
        return self._prepared

    def revalidate(self, prepared, *, artifact=None):
        """Offline readback of the cache or launcher-promoted exact package."""
        require(type(prepared) is PreparedManagerUpdate and prepared is self._prepared
                and prepared.offer is self._offer, "current_verified_stage_required")
        require(prepared.offer.current_version == self.current_version
                and prepared.offer.release.channel == self.channel, "changed_installed_context")
        return self._admit(prepared.artifact if artifact is None else Path(artifact),
            prepared.offer, prepared.proof, trust_pin=prepared.admission.trust_sha256)

    def handoff(self, prepared, request, *, lease, verify_lease, verify_closure,
                verify_transaction, executor=None, native_review=None):
        """Original launcher retains lease; returned process-created is not health."""
        require(type(request) is StockApplyRequest, "exact_stock_request_required")
        self._request_plan(prepared, request)
        return queue_stock_apply(request, lease=lease, verify_lease=verify_lease,
            verify_closure=verify_closure, verify_transaction=verify_transaction,
            verify_admission=lambda original: self.revalidate(prepared, artifact=original.package),
            executor=executor, native_review=native_review)


def manager_update_result(recovery):
    """Read the authenticated transaction result, not live installed inventory.

    The supplied original ManagerRecovery owns all authority gates and IO.
    Missing or unqualified journal evidence propagates as a hold.
    """
    journal = recovery.load()
    phase = journal["phase"]
    require(phase in ("prepared", "launch_ready", "awaiting_health", "healthy",
        "startup_failed", "preserving_b", "restoring_data", "data_restored",
        "apply_requested", "awaiting_recovery_health", "recovery_verified"), "unknown_update_phase")
    binding = journal["binding"]
    return dict(transaction=journal["transaction"], phase=phase,
        verified_version=(binding["target"]["version"] if phase == "healthy" else
                         binding["prior"]["version"] if phase == "recovery_verified" else None),
        target_version=binding["target"]["version"],
        update_succeeded=phase == "healthy", recovery_verified=phase == "recovery_verified",
        manual_repair_available=phase in ("startup_failed", "preserving_b", "restoring_data", "data_restored"),
        automatic_rollback=False)
