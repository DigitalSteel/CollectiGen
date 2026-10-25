"""Isolated shared Tag-data enrollment proposal; no default activation or Apply."""
from dataclasses import dataclass
from datetime import datetime, timezone
from core.connected_version_observation import VersionBinding
from core.connected_version_projection import observe_default_content
from hashlib import sha256
from pathlib import Path
from threading import get_ident
from uuid import uuid4
import re

from core.component_acquisition import ComponentAcquisition, ExactComponentOffer
from core.component_release_transport import ComponentReleaseTransport, ReleaseTransportPolicy
from core.tag_default_acquisition import TagDefaultAcquisition, TagDefaultBaseline, TAG_DATABASE_COMPONENT
from suite_core.discovery.component_release_signature import _trust, _version
from suite_core.discovery.component_staging import guard, require
from suite_core.installation import InstalledComponent, LocalInventory, ComponentKind, _path
from suite_core.manager.tag_database_controller import (
    TagDatabaseController, TagDatabaseChoice, _observe, empty_default_layer,
)

DEFAULT_FEED = None  # Import/startup never enrolls a public feed implicitly.


@dataclass(frozen=True)
class TagDataFeedRegistration:
    pack_id: str
    trust_bytes: bytes
    trust_sha256: str
    expected_offer: ExactComponentOffer
    package_component: InstalledComponent
    asset_redirect_origins: tuple[str, ...]
    transport_qualification_sha256: str
    max_artifact_bytes: int = 64 * 1024 * 1024

    def __post_init__(self):
        require(type(self.pack_id) is str and self.pack_id.strip() == self.pack_id
            and 0 < len(self.pack_id) <= 512, 'explicit_tag_pack_id_required')
        require(type(self.trust_bytes) is bytes and type(self.trust_sha256) is str
            and sha256(self.trust_bytes).hexdigest() == self.trust_sha256,
            'pinned_existing_public_trust_required')
        expected = self.expected_offer
        require(type(expected) is ExactComponentOffer
            and expected.component_id == TAG_DATABASE_COMPONENT
            and expected.trust_sha256 == self.trust_sha256
            and (expected.os, expected.architecture) == ('windows', 'x86_64'),
            'explicit_exact_tag_data_offer_required')
        trust = _trust(self.trust_bytes)
        require(any(key['key_id'] == expected.key_id and key.get('status', 'active') == 'active'
            for key in trust['keys']), 'existing_active_public_identity_required')
        require(type(self.transport_qualification_sha256) is str
            and re.fullmatch('[0-9a-f]{64}', self.transport_qualification_sha256),
            'explicit_transport_qualification_pin_required')
        require(type(self.max_artifact_bytes) is int
            and 0 < self.max_artifact_bytes <= 512 * 1024 * 1024
            and expected.artifact.size_bytes <= self.max_artifact_bytes,
            'bounded_exact_data_artifact_required')
        require(bool(self.asset_redirect_origins), 'explicit_asset_redirect_origins_required')
        ReleaseTransportPolicy(self.asset_redirect_origins)
        component = self.package_component
        require(type(component) is InstalledComponent
            and component.component_id == TAG_DATABASE_COMPONENT
            and component.software_version == expected.software_version
            and component.kind is ComponentKind.APP
            and type(component.location) is LocalInventory
            and component.location.relative_directory == 'Apps/TagDatabase/data'
            and component.location.manifest == 'BOOTSTRAP_INVENTORY.json',
            'existing_data_preparation_declaration_required')
        # APP is the existing stock preparation transport vocabulary only.
        # No executable manifest, activation, installation registry or service is made.


def _disjoint(one, two):
    require(not one.is_relative_to(two) and not two.is_relative_to(one),
        'data_cache_catalog_overlap')


class NativeFreshDataBaseline:
    """Worker-only bridge to the original shared Databases native observation."""
    def __init__(self, controller, pack_id, cache, preparation, native_selection=None):
        self.controller, self.pack_id = controller, pack_id
        self.cache, self.preparation = cache, preparation
        self.native_selection = native_selection
        self.owner = get_ident()
        self.observation = None  # Existing immutable VersionObservation, not a store.

    def __call__(self):
        self.observation = None
        require(get_ident() != self.owner, 'native_feed_observation_worker_required')
        controller = self.controller
        require(not controller.closed and not controller.faulted
            and not controller.close_held and not controller.pending,
            'original_tag_controller_not_ready')
        choice = controller.choice
        if choice is None and self.native_selection is not None:
            return self._reader_baseline()
        require(type(choice) is TagDatabaseChoice and choice.native_adapter is not None,
            'select_enrolled_native_catalog_before_content_discovery')
        require(choice.base_release.revision.pack_id == self.pack_id,
            'selected_catalog_pack_binding_changed')
        adapter = choice.native_adapter
        _disjoint(self.cache, adapter.root)
        _disjoint(self.preparation, adapter.root)
        with adapter.guard():
            observed = _observe(choice, include_capabilities=False)
            self.observation = observed.version_observation
            require(observed.observation_status == 'current'
                and not observed.recovery_receipts, 'native_baseline_unknown_or_recovery_pending')
            require(not observed.installed, 'existing_default_content_upgrades_remain_held')
            require(empty_default_layer(adapter), 'verified_empty_default_layer_required')
            revision = adapter.revision()
        require(controller.choice is choice and not controller.closed
            and not controller.close_held, 'selected_native_baseline_changed')
        # An installed content version is never guessed to be a distribution version.
        return TagDefaultBaseline(adapter.catalog_id, revision, self.pack_id,
            observation_status='verified_empty')


    def _reader_baseline(self):
        # Standalone Open uses an admitted reader view, not an import choice.
        # Reuse that native owner without manufacturing a release/Apply binding.
        from core.encyclopedia_catalog import EncyclopediaCatalogView
        from suite_core.knowledge.native_tag_catalog_host import NativeTagCatalogAdapter
        view = self.native_selection()
        require(type(view) is EncyclopediaCatalogView and not view.read_only
            and type(view.adapter) is NativeTagCatalogAdapter
            and view.root == view.adapter.root
            and view.publication.catalog_id == view.adapter.catalog_id,
            'select_enrolled_native_catalog_before_content_discovery')
        adapter = view.adapter
        _disjoint(self.cache, adapter.root)
        _disjoint(self.preparation, adapter.root)
        with adapter.guard():
            adapter.validate()
            require(empty_default_layer(adapter), 'existing_default_content_or_recovery_remains_held')
            observed = adapter.default_content(self.pack_id)
            require(observed.approved_release is None, 'existing_default_content_upgrades_remain_held')
            self.observation = observe_default_content(VersionBinding(self.pack_id,
                adapter.catalog_id, observed.tracking_fingerprint, version_kind='content',
                source_identity=str(adapter.root)), observed, observed_at=datetime.now(timezone.utc))
            revision = adapter.revision()
        controller = self.controller
        require(self.native_selection() is view and controller.choice is None
            and not controller.closed and not controller.close_held and not controller.pending
            and not controller.faulted, 'selected_native_baseline_changed')
        return TagDefaultBaseline(adapter.catalog_id, revision, self.pack_id,
            observation_status='verified_empty')


def factory_for_tag_data(registration=DEFAULT_FEED, *, opt_in=False,
                         cache_parent=None, host_version=None):
    """Same five-field enrollment for standalone and Manager owner entry shims.

    None/False is inert. Enabled factory creation validates configuration only.
    The host call creates a bounded cache; it never observes a catalog or uses
    the network. Original host executor, monotonic generation and callbacks own
    later refresh/confirmed download. No catalog Apply authority is granted.
    """
    require(type(opt_in) is bool, 'explicit_tag_feed_opt_in_required')
    if not opt_in or registration is None:
        return None
    require(type(registration) is TagDataFeedRegistration, 'typed_tag_feed_registration_required')
    parent = Path(cache_parent)
    guard(parent)
    require(parent.is_absolute() and _path(parent) == parent and parent.is_dir(),
        'original_owned_cache_parent_required')
    _version(host_version)
    used = False

    def create(host):
        nonlocal used
        require(not used, 'retain_original_tag_feed_enrollment')
        controller = getattr(host, 'controller', None)
        invalidate = getattr(host, 'invalidate_default_baseline', None)
        generation = getattr(host, 'baseline_generation', None)
        require(type(controller) is TagDatabaseController and controller._owner == get_ident()
            and callable(invalidate) and controller.before_baseline_change == invalidate
            and type(generation) is int and generation >= 0,
            'original_shared_controller_freshness_binding_required')
        require(not controller.closed and not controller.faulted
            and not controller.pending and not controller.close_held,
            'original_tag_controller_not_ready')
        guard(parent)
        require(parent.is_dir() and _path(parent) == parent, 'original_cache_parent_changed')
        token = uuid4().hex
        cache, preparation = parent / ('tag-feed-' + token), parent / ('tag-source-' + token)
        if controller.choice is not None and controller.choice.native_adapter is not None:
            _disjoint(cache, controller.choice.native_adapter.root)
            _disjoint(preparation, controller.choice.native_adapter.root)
        native_selection = getattr(host, 'tag_data_native_view', None)
        require(native_selection is None or callable(native_selection), 'original_native_selection_required')
        used = True  # Failure retains evidence/cache; never silently adopts a new owner.
        cache.mkdir(exist_ok=False)
        acquisition = ComponentAcquisition(cache_root=cache, cache_owner=cache.name,
            opener=ComponentReleaseTransport(ReleaseTransportPolicy(registration.asset_redirect_origins)),
            host_version=host_version, trust_bytes=registration.trust_bytes,
            trust_sha256=registration.trust_sha256, expected_offer=registration.expected_offer,
            os_name='windows', architecture='x86_64',
            max_artifact_bytes=registration.max_artifact_bytes)
        return dict(service=TagDefaultAcquisition(acquisition=acquisition,
                component_id=TAG_DATABASE_COMPONENT),
            observe_baseline=NativeFreshDataBaseline(controller, registration.pack_id, cache, preparation, native_selection),
            package_component=registration.package_component, preparation_directory=preparation,
            channel=registration.expected_offer.channel)

    return create
