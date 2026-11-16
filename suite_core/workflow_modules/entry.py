"""Real frozen Generate/Train intake; no workspace services before authentication.

Initial distribution software-evidence contract (writer remains a build/release
integration gap): _bootstrap/workflow-manager-admission/initial/{release.json,
release.sig.json,inventory.json}. These are the existing signed Manager release
envelope and detached inventory bytes, copied without rewriting. No trust file
here can enroll a key. Updated installations use the original successor journal.
"""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import argparse
import base64
import json
import ctypes
from ctypes import wintypes as W
import re
import sys

from core.training_source_io import read_stable_training_source
from core.workflow_connection import WorkflowConnection, review_existing_package, workflow_component
from suite_core.discovery.component_payload_policy import FROZEN_PAYLOAD_POLICY
from suite_core.discovery.component_release_signature import _json, _version, verify_component_release
from suite_core.discovery.component_staging import (
    component_origin, component_stage_owner, guard, prior_journal_bytes, validate_journal,
)
from suite_core.workflow_modules import compiled_admission
from suite_core.workflow_modules.compiled_admission import CompiledWorkflowAdmission
from suite_core.workflow_modules.contracts import WORKFLOW_EXECUTABLES
from suite_core.workflow_modules.entry_binding import admit_module_entry, require
from suite_core.workflow_modules.session import ModuleEntrySession

INITIAL_MANAGER_EVIDENCE = Path('_bootstrap/workflow-manager-admission/initial')
MAX_METADATA = 65536
MAX_JOURNAL = 1024 * 1024
MAX_COMPONENT_HISTORY = 128


@dataclass(frozen=True)
class WorkflowEntryLocations:
    component_id: str
    code_root: Path
    data_root: Path
    package_root: Path
    executable: Path


def _read(path, limit):
    guard(path)
    raw, _metadata = read_stable_training_source(path, max_bytes=limit)
    return raw


def _document(path, limit=MAX_METADATA):
    return _json(_read(path, limit), 'workflow retained software metadata')


def entry_locations(component_id, argv=None):
    """Only two CLI locators; frozen executable placement independently fixes them."""
    require(component_id in WORKFLOW_EXECUTABLES and getattr(sys, 'frozen', False) is True,
            'frozen_finite_workflow_entry_required')
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    require(arguments == tuple(sys.argv[1:]) and len(arguments) == 4
            and arguments[0] == '--suite-code-root' and arguments[2] == '--suite-data-root',
            'exact_workflow_entry_locators_required')
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument('--suite-code-root', required=True)
    parser.add_argument('--suite-data-root', required=True)
    try:
        values = parser.parse_args(arguments)
    except SystemExit as error:
        raise ValueError('exact_workflow_entry_locators_required') from error
    executable = Path(sys.executable)
    require(executable.is_absolute() and '..' not in executable.parts and len(executable.parents) >= 4,
            'absolute_frozen_workflow_executable_required')
    package = executable.parent
    _version(package.name)
    code, data = Path(values.suite_code_root), Path(values.suite_data_root)
    require(code.is_absolute() and data.is_absolute() and '..' not in code.parts and '..' not in data.parts
            and code == executable.parents[3] and data == code / 'Data'
            and package == code / 'Apps' / component_id / package.name
            and executable.name == WORKFLOW_EXECUTABLES[component_id], 'exact_frozen_workflow_roots_required')
    for path in (code, data, package, executable):
        guard(path)
    return WorkflowEntryLocations(component_id, code, data, package, executable)


def _module_admission(locations, profile):
    """Read software transaction state only; never Manager settings or user work."""
    component = workflow_component(locations.component_id)
    base = locations.code_root / 'State' / locations.component_id
    require(locations.package_root.name == profile.software_version, 'compiled_workflow_version_changed')
    require(_document(base / 'component-origin.json') == component_origin(
        locations.code_root, locations.data_root, component), 'original_workflow_component_origin_required')
    active_raw = _read(base / 'active.json', MAX_METADATA)
    active = _json(active_raw, 'active workflow software')
    require(set(active) == {'component_id', 'version', 'directory', 'inventory_sha256'}
            and active['component_id'] == locations.component_id and active['version'] == profile.software_version
            and active['directory'] == f'Apps/{locations.component_id}/{profile.software_version}',
            'exact_active_workflow_package_required')
    owner = component_stage_owner(locations.code_root, locations.data_root, component)
    journal = _document(base / 'transaction.json', MAX_JOURNAL)
    validate_journal(journal, component, owner, profile.trust_bytes)
    reference = journal['target'] if journal['phase'] in ('activated', 'healthy') else journal['before']
    require(reference == active, 'active_workflow_transaction_changed')
    seen = set()
    for _ in range(MAX_COMPONENT_HISTORY):
        validate_journal(journal, component, owner, profile.trust_bytes)
        identity = sha256(json.dumps(journal, sort_keys=True).encode()).hexdigest()
        require(identity not in seen, 'cyclic_workflow_software_history')
        seen.add(identity)
        if journal['target'] == active:
            manifest = base64.b64decode(journal['manifest'], validate=True)
            signature = base64.b64decode(journal['signature'], validate=True)
            signed = _json(manifest, 'workflow release')
            selected = review_existing_package(locations.component_id, locations.package_root,
                manifest_bytes=manifest, signature_bytes=signature, trust_bytes=profile.trust_bytes,
                expected_version=profile.software_version, binding_revision='journal:' + identity,
                channel=signed['channel'])
            require(selected.inventory_sha256 == active['inventory_sha256']
                    and _read(base / 'active.json', MAX_METADATA) == active_raw,
                    'active_workflow_package_changed_during_review')
            return selected, manifest, signature
        prior = prior_journal_bytes(base, journal, component, owner, profile.trust_bytes)
        require(prior is not None, 'signed_workflow_origin_unavailable')
        journal = _json(prior, 'prior workflow software transaction')
    raise ValueError('workflow_software_history_bound')


def _successor_manager_metadata(root, profile):
    """Reuse stock software recovery evidence; no data operations or native grants.

    The unsigned locator selects retained signed software evidence only. It does
    not replace Root's independent bootstrap/transaction authority validation.
    """
    from core.manager_self_update_recovery import ManagerRecovery, digest
    from suite_core.discovery.manager_runtime_detached_admission import read_manager_inventory_proof
    locator = _document(root / '_bootstrap/manager-successor.json', 16384)
    require(set(locator) == {'schema', 'transaction', 'binding_sha256', 'release_side', 'version',
                            'initial_contract_sha256', 'trust_sha256', 'settings_relative',
                            'artifact_sha256', 'inventory_sha256'} and locator['schema'] == 1
            and type(locator['transaction']) is str and re.fullmatch('[A-Za-z0-9_-]{1,64}', locator['transaction'])
            and locator['release_side'] in ('prior', 'target')
            and locator['trust_sha256'] == profile.trust_sha256, 'exact_manager_successor_software_locator')
    settings = Path(locator['settings_relative'])
    require(not settings.is_absolute() and '..' not in settings.parts
            and ':' not in str(settings) and settings.parts and settings.parts[0] == 'Manager',
            'manager_successor_software_settings_locator')
    path = root / 'Recovery' / locator['transaction'] / 'transaction.json'
    journal = _document(path, MAX_JOURNAL)
    deployment = dict(root=str(root), app_root=str(root / 'Apps/Manager'), current=str(root / 'Apps/Manager/current'),
        packages=str(root / 'Apps/Manager/packages'), update_exe=str(root / 'Apps/Manager/Update.exe'),
        manifest=str(root / 'Apps/Manager/current/sq.version'), data=str((root / 'Data' / settings).parent),
        recovery=str(root / 'Recovery'), portable=True)
    require(journal['binding']['deployment'] == deployment and digest(journal['binding']) == locator['binding_sha256']
            and journal['phase'] == ('healthy' if locator['release_side'] == 'target' else 'recovery_verified'),
            'terminal_manager_software_evidence_required')
    # This read-only gate is scoped to extracting signed software identity, not
    # approving SDK operations, data access, process creation or a Root lease.
    def gate(name, document):
        require(name == 'transaction_authority' and document['binding'] == journal['binding'],
                'manager_identity_evidence_read_only')
        return dict(status=name, transaction=locator['transaction'], binding_sha256=digest(document['binding']))
    recovery = ManagerRecovery(deployment, locator['transaction'], gates=gate)
    checked = recovery.load()
    release = checked['binding'][locator['release_side']]
    # Bound all retained software before the stock verifier hashes any file.
    files = release['files']
    limits = {'archive': 96 * 1024 * 1024, 'manifest': MAX_METADATA, 'signature': MAX_METADATA,
              'trust': MAX_METADATA, 'proof': 256 * 1024, 'inventory': 256 * 1024,
              'executable': 256 * 1024 * 1024}
    require(type(files) is dict and set(files) == set(limits), 'exact_manager_retained_software_files')
    for name, limit in limits.items():
        spec = files[name]
        require(type(spec) is dict and type(spec.get('size')) is int and 0 < spec['size'] <= limit,
                'bounded_manager_retained_software_required')
    recovery.release(release)  # Existing retained software path/hash guards.
    require(files['trust']['sha256'] == profile.trust_sha256, 'retained_manager_trust_changed')
    manifest = _read(Path(files['manifest']['path']), MAX_METADATA)
    signature = _read(Path(files['signature']['path']), MAX_METADATA)
    proof = read_manager_inventory_proof(files['proof'])
    require(sha256(manifest).hexdigest() == files['manifest']['sha256']
            and sha256(signature).hexdigest() == files['signature']['sha256']
            and release['version'] == locator['version']
            and sha256(proof).hexdigest() == locator['inventory_sha256'], 'manager_retained_software_changed')
    return manifest, signature, proof, True


def manager_peer_pin(root, profile):
    """Derive a peer pin only from compiled publisher trust and installed software."""
    from core.manager_successor_admission import installed_manager_payload_path
    from core.manager_self_update_recovery import observe
    from suite_core.discovery.manager_runtime_detached_admission import (
        MANAGER, PROFILE as MANAGER_PROFILE, PAYLOAD_ENTRY, MANAGER_MAX_PROOF_BYTES, manager_inventory_json,
    )
    locator = root / '_bootstrap/manager-successor.json'
    guard(locator)
    if locator.exists():
        manifest, signature, proof, stock_applied = _successor_manager_metadata(root, profile)
    else:
        base = root / INITIAL_MANAGER_EVIDENCE
        manifest = _read(base / 'release.json', MAX_METADATA)
        signature = _read(base / 'release.sig.json', MAX_METADATA)
        proof = _read(base / 'inventory.json', MANAGER_MAX_PROOF_BYTES)
        stock_applied = False
    metadata = _json(manifest, 'Manager parent signed release')
    version = _version(metadata['version'])
    require(version.compare(_version(profile.minimum_manager_version)) >= 0
            and version.compare(_version(profile.maximum_manager_version_exclusive)) < 0,
            'manager_parent_version_outside_compiled_contract')
    verified = verify_component_release(manifest, signature, profile.trust_bytes,
        expected_component=MANAGER, expected_version=metadata['version'], expected_channel=metadata['channel'],
        expected_os='windows', expected_architecture='x86_64')
    require(sha256(proof).hexdigest() == verified.artifact.portable_manifest_sha256,
            'signed_manager_parent_inventory_changed')
    inventory = manager_inventory_json(proof)
    require(inventory.get('inventory_profile') == MANAGER_PROFILE
            and inventory.get('component_id') == MANAGER.value
            and inventory.get('software_version') == verified.software_version
            and inventory.get('platform') == {'os': 'windows', 'architecture': 'x86_64'}
            and inventory.get('runtime_kind') == 'frozen-portable'
            and inventory.get('archive_sha256') == verified.artifact.sha256
            and inventory.get('archive_size_bytes') == verified.artifact.size_bytes,
            'exact_signed_manager_parent_inventory_required')
    require(set(inventory) == {'schema_version', 'component_id', 'software_version', 'platform',
                              'runtime_kind', 'files', 'inventory_profile', 'archive_sha256', 'archive_size_bytes'}
            and type(inventory['schema_version']) is int and inventory['schema_version'] == 1
            and type(inventory['files']) is list and 0 < len(inventory['files']) <= 1536,
            'bounded_manager_parent_inventory_schema')
    seen, total = set(), 0
    for row in inventory['files']:
        require(type(row) is dict and set(row) == {'path', 'size_bytes', 'sha256'}
                and type(row['path']) is str and row['path'].casefold() not in seen
                and type(row['size_bytes']) is int and 0 <= row['size_bytes'] <= 256 * 1024 * 1024
                and type(row['sha256']) is str and re.fullmatch('[0-9a-f]{64}', row['sha256']),
                'exact_manager_parent_inventory_row')
        seen.add(row['path'].casefold())
        total += row['size_bytes']
    require(total <= 256 * 1024 * 1024 and {PAYLOAD_ENTRY.casefold(), 'squirrel.exe', 'sq.version',
            'collectigen.suite-manager_executionstub.exe'} <= seen, 'complete_bounded_manager_parent_inventory')
    executable_pin = None
    for row in inventory['files']:
        path = installed_manager_payload_path(root, row['path'], stock_applied=stock_applied)
        require(path.is_file() and path.stat().st_size == row['size_bytes'],
                'signed_installed_manager_parent_changed')
        require(observe(path) == {'exists': True, 'size': row['size_bytes'], 'sha256': row['sha256']},
                'signed_installed_manager_parent_changed')
        if row['path'] == PAYLOAD_ENTRY:
            require(executable_pin is None, 'one_manager_parent_entry_required')
            executable_pin = row['sha256']
    require(executable_pin is not None, 'signed_manager_parent_entry_required')
    return executable_pin


class _ProcessBasicInformation(ctypes.Structure):
    _fields_ = [('exit_status', W.LONG), ('peb', W.LPVOID), ('affinity', ctypes.c_size_t),
                ('base_priority', W.LONG), ('pid', ctypes.c_size_t), ('parent_pid', ctypes.c_size_t)]


def _direct_native_parent(channel):
    """Query only this process; the inherited peer handle remains parent authority."""
    query = ctypes.WinDLL('ntdll', use_last_error=True).NtQueryInformationProcess
    query.argtypes = [W.HANDLE, W.ULONG, W.LPVOID, W.ULONG, ctypes.POINTER(W.ULONG)]
    query.restype = W.LONG
    result, size = _ProcessBasicInformation(), W.ULONG()
    current = channel.api.GetCurrentProcess()
    status = query(current, 0, ctypes.byref(result), ctypes.sizeof(result), ctypes.byref(size))
    local, peer = channel.local_witness(), channel.peer_witness()
    require(status == 0 and size.value == ctypes.sizeof(result)
            and result.pid == local['pid'] and result.parent_pid == peer['pid']
            and peer['creation_filetime'] <= local['creation_filetime']
            and channel.api.WaitForSingleObject(channel.peer_handle, 0) == 258,
            'actual_live_manager_parent_required')


def _exact_parent_path(channel, root):
    size = W.DWORD(32768)
    image = ctypes.create_unicode_buffer(size.value)
    require(channel.api.QueryFullProcessImageNameW(channel.peer_handle, 0, image, ctypes.byref(size)),
            'original_manager_parent_image_query_required')
    observed = Path(image.value)
    guard(observed)
    require(observed == root / 'Apps/Manager/current/CollectiGenManager.exe',
            'exact_installed_manager_parent_path_required')
    _direct_native_parent(channel)


def entry_session(component_id, *, argv=None, timeout=30):
    """Return a ready authenticated child session; never construct workspace services."""
    profile = compiled_admission.PROFILE
    require(type(profile) is CompiledWorkflowAdmission, 'compiled_workflow_admission_unavailable')
    profile.validate(component_id)
    locations = entry_locations(component_id, argv)
    selected, manifest, signature = _module_admission(locations, profile)
    peer_pin = manager_peer_pin(locations.code_root, profile)
    admitted = admit_module_entry(code_root=locations.code_root, data_root=locations.data_root,
        selected=selected, manifest_bytes=manifest, signature_bytes=signature, trust_bytes=profile.trust_bytes,
        expected_trust_sha256=profile.trust_sha256, expected_peer_image_sha256=peer_pin)
    from core.manager_launch_owner import WindowsControlChannel
    channel = WindowsControlChannel.from_inherited_environment()
    # Ownership transfers only after authentication succeeds. On startup refusal,
    # close control resources but never signal drain/release or terminate a peer.
    try:
        from core.workflow_native_channel import WorkflowImageScope
        # The inherited epoch is only a locator. Native/package admission still
        # authenticates both images before any workspace service is constructed.
        image_scope = WorkflowImageScope(channel, component_id, custodian=channel)
        _exact_parent_path(channel, locations.code_root)
        session = ModuleEntrySession.accept(channel, admitted, timeout=timeout)
        image_scope.bind(session.offer.owner_generation, session.offer.attempt_id, session.offer.grant_id)
        _exact_parent_path(channel, locations.code_root)
        return session
    except BaseException:
        channel.close()
        raise


def run(component_id, application_entry, *, argv=None):
    """Trusted __main__ composition: application_entry(session) owns UI and cleanup."""
    require(callable(application_entry), 'explicit_workflow_application_entry_required')
    session = entry_session(component_id, argv=argv)
    return application_entry(session)
