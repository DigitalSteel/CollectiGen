"""Authenticated installed selections for the existing component transaction.

DTOs are context, never native authority. Only the original DeploymentOwner
issues an in-memory subject/stage capability. Flat packages are not enrolled.
"""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import base64
import zipfile
import re

from core.encyclopedia_connection import COMPONENT, ReaderConnection, ReaderConnectionStore, review_directory, connection_from_reference
from core.component_acquisition import ComponentAcquisition, ComponentOffer
from release_contract import safe_zip_members, sha256_file, verify_artifact
from suite_core.installation import _path
from suite_core.discovery.component_release_signature import _json, verify_component_release, MAX_METADATA_BYTES
from suite_core.discovery.component_staging import (
    active, active_token, canonical, component_origin, component_stage_owner,
    guard, prior_journal_bytes, read_json, require, validate_journal, verify_directory,
)

MODULE = 'TagEncyclopedia/MODULE_MANIFEST.json'

# Closed footprint and fixed read IDs qualified in QUALIFICATION_HANDOFF13.
# These bounded read semantics grant no writer, migration or native authority.
DATA_PARTICIPANTS = frozenset(('tag-store', 'category-store', 'relationship-store',
    'series-store', 'character-store', 'outfit-store', 'catalog-identity', 'record-lifecycle',
    'default-sync-state', 'app-settings', 'encyclopedia-reader-connection', 'tag-catalog-connection'))
_READ_CONTRACT_IDS = {
    'tag-store': frozenset(('collectigen/tag-store/legacy1-11-enrolled12-read-v1',)),
    'category-store': frozenset(('collectigen/category-store/legacy1-6-enrolled7-read-v1',)),
    'relationship-store': frozenset(('collectigen/relationship-store/known-records-opaque-preservation-v1',)),
    'series-store': frozenset(('collectigen/series-store/known1-4-lineage-read-v1',)),
    'character-store': frozenset(('collectigen/character-store/known1-6-lineage-read-v1',)),
    'outfit-store': frozenset(('collectigen/outfit-store/known1-4-lineage-read-v1',)),
    'catalog-identity': frozenset(('collectigen/catalog-identity/schema1-read-v1',)),
    'record-lifecycle': frozenset(('collectigen/record-lifecycle/schema1-inventory-read-v1',)),
    'default-sync-state': frozenset(('collectigen/default-sync-state/saved-identity-lifecycle-release1-read-v1',)),
    'app-settings': frozenset(('collectigen/app-settings/validator-opaque-preservation-v1',)),
    'encyclopedia-reader-connection': frozenset(('collectigen/encyclopedia-reader-connection/schema1-opaque-preservation-v1',)),
    'tag-catalog-connection': frozenset(('collectigen/tag-catalog-connection/schema1-opaque-preservation-v1',)),
}
_WRITE_CONTRACT_IDS = {participant: frozenset() for participant in DATA_PARTICIPANTS}
# Fixed host obligations, independent of optional reader vocabulary extensions.
_HOST_REQUIRED_READ_CONTRACTS = tuple((p, tuple(sorted(ids))) for p, ids in sorted(_READ_CONTRACT_IDS.items()))
CONNECTION_SETTINGS_CONTRACT = 'collectigen/encyclopedia-connection-settings/opaque-preservation-v1'


@dataclass(frozen=True)
class EncyclopediaDataCompatibility:
    read_contracts: tuple
    snapshot_interfaces: tuple
    write_contracts: tuple
    connection_settings_contract: str
    required_read_contracts: tuple
    required_snapshot_interfaces: tuple


@dataclass(frozen=True)
class EncyclopediaInstalledSnapshot:
    selected: ReaderConnection
    active_sha256: str
    journal_sha256: str
    settings_sha256: str
    phase: str
    release_sha256: str


@dataclass(frozen=True)
class StandaloneEncyclopediaBinding:
    root: Path
    data_root: Path
    settings_path: Path
    bootstrap_program: Path
    bootstrap_sha256: str
    trust_bytes: bytes
    trust_sha256: str
    initial: EncyclopediaInstalledSnapshot
    schema2_identity: bool = False

    @property
    def identity(self):
        return sha256(canonical(dict(root=str(self.root), data=str(self.data_root),
            settings=str(self.settings_path), bootstrap=str(self.bootstrap_program),
            bootstrap_sha256=self.bootstrap_sha256, trust_sha256=self.trust_sha256,
            initial=self.initial.selected.mapping(), **({"schema2_identity": True} if self.schema2_identity else {})))).hexdigest()

    def validate_static(self):
        require(type(self.schema2_identity) is bool, 'explicit_reader_identity_mode')
        require(self.schema2_identity or self.initial.selected.release_schema == 1, 'reader_identity_mode_required')
        require(_path(self.root) == self.root and _path(self.data_root) == self.data_root,
                'exact_original_component_roots_required')
        component_origin(self.root, self.data_root, COMPONENT)
        require(self.bootstrap_program == self.root / 'TagEncyclopedia.exe', 'stable_external_bootstrap_required')
        guard(self.bootstrap_program)
        require(sha256_file(self.bootstrap_program) == self.bootstrap_sha256, 'stable_bootstrap_changed')
        require(type(self.trust_bytes) is bytes and sha256(self.trust_bytes).hexdigest() == self.trust_sha256,
                'explicit_pinned_component_trust_required')
        require(_path(self.settings_path) == self.settings_path and self.settings_path.is_relative_to(self.data_root)
                and self.settings_path != self.data_root, 'original_component_settings_scope_required')
        # Initial immutable payload remains retained through target activation/recovery.
        review_directory(self.initial.selected.directory, expected=self.initial.selected)

    def current(self):
        self.validate_static()
        return _installed(self.root, self.data_root, self.settings_path, self.trust_bytes, schema2_identity=self.schema2_identity)

    def revalidate_initial(self):
        require(self.current() == self.initial, 'initial_component_installation_changed')
        return self.initial


def _installed(root, data, settings, trust, *, schema2_identity=False):
    require(type(schema2_identity) is bool, "explicit_reader_identity_mode")
    options = {"allow_schema2": True} if schema2_identity else {}
    base = root / 'State' / COMPONENT.value
    require(read_json(base / 'component-origin.json') == component_origin(root, data, COMPONENT),
            'authenticated_enrolled_component_origin_required')
    require(not (base / 'pending-transaction.json').exists() and not (base / 'connection-pending.json').exists(),
            'recover_original_component_transaction_first')
    owner = component_stage_owner(root, data, COMPONENT)
    journal = read_json(base / 'transaction.json')
    validate_journal(journal, COMPONENT, owner, trust, **options)
    previous = prior_journal_bytes(base, journal, COMPONENT, owner, trust, **options)
    require(journal['phase'] in ('healthy', 'activated', 'rolled_back'), 'settled_component_baseline_required')
    reference = (read_json(base / 'active.json') if (base / 'active.json').exists() else None) if schema2_identity else active(root, base, COMPONENT, {'os': 'windows', 'architecture': 'x86_64'})
    expected = journal['target'] if journal['phase'] in ('healthy', 'activated') else journal['before']
    require(reference is not None and reference == expected, 'signed_current_component_reference_required')
    # Rollback references need their own retained signed origin, not just the new
    # target's signature. Bound the retained history walk and validate each link.
    signed = journal
    for _ in range(32):
        if signed['target'] == reference:
            break
        require(previous is not None, 'independently_signed_prior_component_required')
        signed = _json(previous, 'prior component transaction')
        validate_journal(signed, COMPONENT, owner, trust, **options)
        previous = prior_journal_bytes(base, signed, COMPONENT, owner, trust, **options)
    else:
        raise ValueError('bounded_component_history_required')
    require(signed['target'] == reference, 'signed_current_component_reference_required')
    if schema2_identity:
        verified = validate_journal(signed, COMPONENT, owner, trust, **options)
        active(root, base, COMPONENT, {'os': 'windows', 'architecture': 'x86_64'}, release=verified)
    selected = review_directory(root / reference['directory'], expected=connection_from_reference(root, reference))
    store = ReaderConnectionStore(settings)
    observed = store.observe()
    require(store.selected(observed) == selected, 'original_saved_component_selection_required')
    return EncyclopediaInstalledSnapshot(selected, active_token(reference),
        sha256(canonical(journal)).hexdigest(), observed.digest, journal['phase'],
        sha256(base64.b64decode(signed['manifest'], validate=True)).hexdigest())


def admit_standalone_binding(*, root, data_root, settings_path, bootstrap_sha256,
                             trust_bytes, trust_sha256, schema2_identity=False):
    root, data, settings = _path(Path(root)), _path(Path(data_root)), _path(Path(settings_path))
    require(type(trust_bytes) is bytes and sha256(trust_bytes).hexdigest() == trust_sha256,
            'explicit_pinned_component_trust_required')
    initial = _installed(root, data, settings, trust_bytes, schema2_identity=schema2_identity)
    binding = StandaloneEncyclopediaBinding(root, data, settings, root / 'TagEncyclopedia.exe',
        bootstrap_sha256, trust_bytes, trust_sha256, initial, schema2_identity)
    binding.validate_static()
    return binding


@dataclass(frozen=True)
class ConfirmedEncyclopediaUpdate:
    subject: object
    current: EncyclopediaInstalledSnapshot
    offer: ComponentOffer
    binding_sha256: str
    owner_generation: str
    normal_close_attempt: int | None = None
    data_obligations: EncyclopediaDataCompatibility | None = None

    @property
    def identity(self):
        return dict(binding=self.binding_sha256, generation=self.owner_generation,
            active=self.current.active_sha256, journal=self.current.journal_sha256,
            settings=self.current.settings_sha256, current=self.current.selected.mapping(),
            current_release_sha256=self.current.release_sha256,
            target=self.offer.release.software_version, release_sha256=self.offer.release.manifest_sha256,
            **({'target_schema': 2, 'target_maturity': self.offer.release.maturity} if self.offer.release.schema_version == 2 else {}),
            target_inventory_sha256=self.offer.release.artifact.portable_manifest_sha256,
            normal_close_attempt=self.normal_close_attempt,
            data_obligations_sha256=sha256(canonical({'reads': self.data_obligations.required_read_contracts,
                'interfaces': self.data_obligations.required_snapshot_interfaces,
                'settings': self.data_obligations.connection_settings_contract})).hexdigest()
                if self.data_obligations is not None else None,
            trust_sha256=self.offer.trust_sha256, package_sha256=self.offer.release.artifact.sha256)


def admit_confirmed_offer(binding, subject, acquisition, offer, generation):
    require(type(binding) is StandaloneEncyclopediaBinding and type(acquisition) is ComponentAcquisition,
            'original_binding_and_acquisition_required')
    current = binding.current()
    require(current.phase in ('healthy', 'rolled_back'), 'finish_original_component_health_first')
    require(type(offer) is ComponentOffer and offer.release.component_id == COMPONENT
            and offer.operation == 'update' and offer.current_version == current.selected.version,
            'exact_current_encyclopedia_update_required')
    require(acquisition.trust == binding.trust_bytes and acquisition.trust_pin == binding.trust_sha256,
            'original_component_trust_required')
    require(binding.schema2_identity or offer.release.schema_version == 1, 'reader_identity_mode_required')
    require(offer.release.schema_version == 1 or acquisition.schema2_metadata, 'original_metadata_observation_required')
    if acquisition.schema2_metadata:
        context = offer.installed_context
        require(context is not None and context.component_id == COMPONENT
                and (context.version, context.schema_version, context.maturity) ==
                (current.selected.version, current.selected.release_schema, current.selected.maturity),
                'exact_installed_identity_required')
    acquisition.revalidate_offer(offer)
    verified = verify_component_release(offer.manifest_bytes, offer.signature_bytes, binding.trust_bytes,
        expected_component=COMPONENT, expected_version=offer.release.software_version,
        expected_channel=offer.release.channel, expected_os='windows', expected_architecture='x86_64', expected_maturity=offer.release.maturity)
    require(verified == offer.release, 'signed_target_component_changed')
    require(binding.current() == current, 'current_component_changed_during_review')
    obligations = installed_data_contract(current.selected)
    return ConfirmedEncyclopediaUpdate(subject, current, offer, binding.identity, generation, data_obligations=obligations)


def _data_contract(raw, version):
    module = _json(raw, 'signed component module')
    require(module.get('module_id') == COMPONENT.value and module.get('software_version') == version,
            'component_schema_identity_required')
    contract = module.get('update_data_contract')
    required = {'schema', 'mode', 'migration', 'read_contracts', 'snapshot_interface_versions',
                'connection_settings_contract', 'required_read_contracts', 'required_snapshot_interface_versions'}
    require(type(contract) is dict and required <= set(contract) <= required | {'write_contracts'}
            and type(contract['schema']) is int and contract['schema'] == 2,
            'signed_named_data_compatibility_contract_required')
    require(contract['mode'] == 'code_only' and contract['migration'] == 'none', 'code_only_no_migration_required')
    require(contract['connection_settings_contract'] == CONNECTION_SETTINGS_CONTRACT,
            'updater_connection_settings_preservation_required')
    def named(values, vocabulary, *, complete, strict=False):
        require(type(values) is dict and len(values) <= 32
                and (set(values) == DATA_PARTICIPANTS if strict else (DATA_PARTICIPANTS <= set(values) if complete else True))
                and all(type(p) is str and re.fullmatch('[a-z][a-z0-9-]{0,63}', p) for p in values),
                'exact_named_data_participants_required')
        result = []
        for participant, ids in sorted(values.items()):
            require(type(ids) is list and 0 < len(ids) <= 32 and all(type(i) is str
                    and re.fullmatch('[A-Za-z0-9][A-Za-z0-9./:_-]{0,191}', i) for i in ids)
                    and len(set(ids)) == len(ids), 'exact_named_data_contract_ids_required')
            if strict:
                require(set(ids) <= vocabulary[participant], 'qualified_named_data_contract_ids_required')
            result.append((participant, tuple(sorted(ids))))
        return tuple(result)
    interfaces = contract['snapshot_interface_versions']
    require(type(interfaces) is list and 0 < len(interfaces) <= 16
            and all(type(v) is int and 0 < v <= 65535 for v in interfaces)
            and len(set(interfaces)) == len(interfaces), 'exact_supported_snapshot_interfaces_required')
    required_interfaces = contract['required_snapshot_interface_versions']
    require(type(required_interfaces) is list and 0 < len(required_interfaces) <= 2
            and all(type(v) is int and v in (1, 2) for v in required_interfaces)
            and len(set(required_interfaces)) == len(required_interfaces), 'known_required_snapshot_interfaces_required')
    parsed = EncyclopediaDataCompatibility(named(contract['read_contracts'], _READ_CONTRACT_IDS, complete=True),
        tuple(sorted(interfaces)), named(contract.get('write_contracts', {}), _WRITE_CONTRACT_IDS, complete=False),
        contract['connection_settings_contract'], named(contract['required_read_contracts'], _READ_CONTRACT_IDS,
            complete=True, strict=True), tuple(sorted(required_interfaces)))
    require_contract_read_coverage(parsed, parsed)
    return parsed


def require_contract_read_coverage(prior, target):
    """Named obligations only; optional catalog writes do not gate code update."""
    require(type(prior) is EncyclopediaDataCompatibility and type(target) is EncyclopediaDataCompatibility,
            'parsed_named_data_contracts_required')
    target_reads = dict(target.read_contracts)
    require(all(participant in target_reads and set(ids) <= set(target_reads[participant])
                and set(ids) <= _READ_CONTRACT_IDS[participant] for participant, ids in prior.required_read_contracts)
            and set(prior.required_snapshot_interfaces) <= set(target.snapshot_interfaces) & {1, 2}
            and prior.connection_settings_contract == target.connection_settings_contract == CONNECTION_SETTINGS_CONTRACT,
            'component_data_schema_change_held')


def require_code_only_obligation_equality(prior, target):
    """No migration means neither increasing nor forgetting persisted obligations."""
    require(type(prior) is EncyclopediaDataCompatibility and type(target) is EncyclopediaDataCompatibility
            and prior.required_read_contracts == target.required_read_contracts
            and prior.required_snapshot_interfaces == target.required_snapshot_interfaces,
            'component_data_schema_change_held')


def installed_data_contract(selected):
    profile = verify_directory(selected.directory, selected.inventory_sha256, COMPONENT, selected.version)
    require(any(row['path'] == MODULE for row in profile['files']), 'signed_component_schema_manifest_required')
    path = selected.directory / MODULE
    guard(path)
    require(path.stat().st_size <= MAX_METADATA_BYTES, 'bounded_component_schema_required')
    return _data_contract(path.read_bytes(), selected.version)


def acquired_data_contract(acquired):
    """Read the existing authenticated archive/inventory/module chain only."""
    offer = acquired.offer
    verify_artifact(acquired.artifact, offer.release.artifact)
    with zipfile.ZipFile(acquired.artifact) as archive:
        safe_zip_members(i.filename for i in archive.infolist())
        inventory_info, module_info = archive.getinfo('BOOTSTRAP_INVENTORY.json'), archive.getinfo(MODULE)
        require(inventory_info.file_size <= MAX_METADATA_BYTES and module_info.file_size <= MAX_METADATA_BYTES,
                'bounded_component_schema_required')
        inventory = archive.read(inventory_info)
        require(sha256(inventory).hexdigest() == offer.release.artifact.portable_manifest_sha256,
                'signed_component_inventory_changed')
        profile = _json(inventory, 'signed component inventory')
        rows = [row for row in profile['files'] if row['path'] == MODULE]
        require(len(rows) == 1, 'signed_component_schema_manifest_required')
        module = archive.read(module_info)
        require(rows[0]['size_bytes'] == len(module) and rows[0]['sha256'] == sha256(module).hexdigest(),
                'signed_component_schema_changed')
        target = _data_contract(module, offer.release.software_version)
    verify_artifact(acquired.artifact, offer.release.artifact)
    return target


def standalone_host_data_contract():
    """Actual qualified host reads and lossless snapshot interfaces; no version floor."""
    from suite_core.encyclopedia.version import LEGACY_SNAPSHOT_INTERFACE_VERSION, SNAPSHOT_INTERFACE_VERSION
    reads = _HOST_REQUIRED_READ_CONTRACTS
    interfaces = tuple(sorted({LEGACY_SNAPSHOT_INTERFACE_VERSION, SNAPSHOT_INTERFACE_VERSION}))
    return EncyclopediaDataCompatibility(reads, interfaces, (), CONNECTION_SETTINGS_CONTRACT, reads, interfaces)


class StandaloneReaderCompatibilityHold(ValueError):
    """Exact acquired package was rejected before any installation mutation."""


def require_standalone_install_compatibility(prior, acquired):
    """After exact-package consent/acquisition, gate BEFORE staging/publication."""
    try:
        return _require_standalone_install_compatibility(prior, acquired)
    except (ValueError, KeyError, zipfile.BadZipFile) as error:
        raise StandaloneReaderCompatibilityHold(str(error)) from error


def _require_standalone_install_compatibility(prior, acquired):
    target = acquired_data_contract(acquired)
    host = standalone_host_data_contract()
    require_contract_read_coverage(host, target)
    require_code_only_obligation_equality(host, target)
    if prior is not None:
        previous = installed_data_contract(prior)
        require_code_only_obligation_equality(previous, target)
        require_contract_read_coverage(previous, target)
        require_contract_read_coverage(target, previous)  # rollback before mutation
    return target


def require_target_compatibility(prior, acquired, *, original_contract=None):
    """Signed named read coverage; no migration, scan or user-data restore."""
    target = acquired_data_contract(acquired)
    previous = installed_data_contract(prior)
    require(original_contract is None or previous == original_contract, 'original_data_obligations_changed')
    require_code_only_obligation_equality(previous, target)
    require_contract_read_coverage(previous, target)
    require_contract_read_coverage(target, previous)  # prove rollback BEFORE mutation
