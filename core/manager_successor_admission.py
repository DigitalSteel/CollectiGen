"""Read-only signed installed successor selection under the original owner.

Retained recovery metadata and signed archive inventory authorize selection.
Neither a mutable version pointer nor child-reported executable hashes do so.
"""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from core.manager_launch_owner import DeploymentOwner, LaunchHold
from core.manager_self_update_recovery import ManagerRecovery, digest, observe
from suite_core.discovery.component_staging import guard, require, MAX_METADATA_BYTES
from suite_core.discovery.component_release_signature import (
    verify_component_release, verify_component_artifact, _json,
)
from suite_core.discovery.manager_runtime_detached_admission import (
    MANAGER, PROFILE, PAYLOAD_ENTRY, produce_detached_inventory,
)
from suite_core.manager.entry_binding import validate_normal_launch


@dataclass(frozen=True)
class SignedSuccessorSelection:
    owner: object
    recovery: object
    request: object
    binding_sha256: str
    release_side: str
    version: str
    channel: str
    executable: Path
    executable_sha256: str
    manifest_sha256: str
    inventory_sha256: str
    artifact_sha256: str
    admitted: object
    installed_files: tuple

    def revalidate(self):
        fresh = admit_successor_selection(self.owner, self.recovery, self.request,
            release_side=self.release_side, argv=(str(self.executable), *self.admitted.restart_arguments))
        require(fresh == self, 'signed_successor_selection_changed')
        return self


@dataclass(frozen=True)
class OrdinarySuccessorBinding:
    root: Path
    initial_contract: dict
    initial_contract_sha256: str
    trust_sha256: str
    settings_path: Path
    contract: dict

    @property
    def version(self):return self.contract['manager_version']

    def revalidate(self):
        guard(self.root/'MANAGER_PACKAGE.json')
        require(sha256((self.root/'MANAGER_PACKAGE.json').read_bytes()).hexdigest()==self.initial_contract_sha256,
            'ordinary_initial_compiled_contract_changed')
        fresh=ordinary_successor_contract(self.root,self.initial_contract,
            initial_contract_sha256=self.initial_contract_sha256,expected_trust_sha256=self.trust_sha256,
            settings_path=self.settings_path)
        require(fresh==self.contract,'ordinary_signed_successor_binding_changed')
        return self


def admit_successor_selection(owner, recovery, request, *, release_side, argv):
    """Reverify retained signature/archive/proof and every fixed installed file.

    Prior selection is allowed only in the existing explicit repair health phase;
    it never weakens the supported-upgrade check for target admission/dispatch.
    """
    require(type(owner) is DeploymentOwner and type(recovery) is ManagerRecovery
        and recovery.paths['root'] == owner.root, 'original_successor_owner_required')
    owner._maintenance_context(request, allow_pending=True)
    journal = recovery.load()
    require(release_side in {'target', 'prior'}, 'exact_successor_release_side')
    phases = ({'launch_ready', 'awaiting_health', 'healthy'} if release_side == 'target'
              else {'awaiting_recovery_health', 'recovery_verified'})
    require(journal['phase'] in phases, 'successor_recovery_phase')
    require(journal['transaction'] == request.transaction_id, 'successor_transaction_changed')
    recovery.retained_context(journal)
    release = journal['binding'][release_side]
    files = release['files']
    def read(name):
        spec = files[name]
        require(spec['size'] <= MAX_METADATA_BYTES, 'successor_metadata_bounds')
        return Path(spec['path']).read_bytes()
    manifest, signature, trust, proof = (read(name) for name in ('manifest', 'signature', 'trust', 'proof'))
    if release_side == 'target':
        require(release['version'] == request.target_version and release['channel'] == request.channel
            and sha256(manifest).hexdigest() == request.release_sha256
            and sha256(trust).hexdigest() == request.trust_sha256
            and files['archive']['sha256'] == request.package_sha256,
            'original_signed_target_identity_changed')
    else:
        require(release['version'] == request.current_version and release['channel'] == request.channel,
            'original_signed_prior_identity_changed')
    verified = verify_component_release(manifest, signature, trust,
        expected_component=MANAGER, expected_version=release['version'],
        expected_channel=release['channel'], expected_os='windows', expected_architecture='x86_64')
    if release_side == 'target':
        from release_contract import verify_release_manifest, ensure_supported_upgrade
        ensure_supported_upgrade(request.current_version, verify_release_manifest(manifest, signature,
            _json(trust, 'successor trust'), expected_channel=release['channel']))
    archive = Path(files['archive']['path'])
    verify_component_artifact(archive, verified)
    observed = produce_detached_inventory(archive, inventory_profile=PROFILE,
        expected_version=release['version'], expected_channel=release['channel'])
    require(proof == observed and sha256(proof).hexdigest() == verified.artifact.portable_manifest_sha256,
        'successor_signed_inventory_changed')
    if release_side == 'target':
        require(sha256(proof).hexdigest() == request.inventory_sha256,'original_target_inventory_changed')
    rows = _json(proof, 'successor inventory')['files']
    current = owner.root / 'Apps/Manager/current'
    installed = []
    for row in rows:
        path = current / row['path']
        guard(path)
        require(path.is_relative_to(current) and observe(path) == dict(exists=True,
            size=row['size_bytes'], sha256=row['sha256']), 'successor_installed_payload_changed')
        installed.append((row['path'], row['size_bytes'], row['sha256']))
    by_name = {name: pin for name, size, pin in installed}
    require(files['executable']['sha256']==by_name[PAYLOAD_ENTRY]
        and files['inventory']['sha256']==sha256(proof).hexdigest(),'successor_retained_code_inventory_changed')
    executable = current / PAYLOAD_ENTRY
    require(tuple(argv)[0] == str(executable), 'fixed_successor_executable_required')
    admitted = validate_normal_launch(tuple(argv)[1:], executable=executable)
    require(admitted.layout.code_root == owner.root and admitted.layout.data_root == owner.data_root
        and owner.entry_binding is not None
        and str(admitted.settings_path) == owner.entry_binding['settings_path'],
        'original_successor_data_settings_required')
    recovery.retained_context(journal)
    require(recovery.load()['binding'] == journal['binding'], 'successor_binding_changed')
    return SignedSuccessorSelection(owner, recovery, request, digest(journal['binding']),
        release_side, release['version'], release['channel'], executable, by_name[PAYLOAD_ENTRY],
        by_name['sq.version'], sha256(proof).hexdigest(), verified.artifact.sha256,
        admitted, tuple(installed))


def commit_ordinary_successor(selection, *, initial_contract_sha256):
    """Commit only after durable verified health; keep exact pointer preimage."""
    from core.manager_self_update_recovery import atomic, encoded
    require(type(selection) is SignedSuccessorSelection,'signed_successor_required')
    selection.revalidate()
    recovery,owner = selection.recovery,selection.owner
    with recovery.owned():
        journal = recovery.load()
        require((selection.release_side,journal['phase']) in {('target','healthy'),('prior','recovery_verified')},
            'verified_health_before_ordinary_selection_required')
        contract = owner.root/'MANAGER_PACKAGE.json';guard(contract)
        require(sha256(contract.read_bytes()).hexdigest() == initial_contract_sha256,'original_bootstrap_contract_changed')
        relative = selection.admitted.settings_path.relative_to(owner.data_root).as_posix()
        value = dict(schema=1,transaction=recovery.id,binding_sha256=selection.binding_sha256,
            release_side=selection.release_side,version=selection.version,
            initial_contract_sha256=initial_contract_sha256,trust_sha256=selection.request.trust_sha256,
            settings_relative=relative,artifact_sha256=selection.artifact_sha256,
            inventory_sha256=selection.inventory_sha256)
        destination = owner.root/'_bootstrap/manager-successor.json';guard(destination)
        before = destination.read_bytes() if destination.exists() else None
        backup = recovery.home/'ordinary-successor-preimage.json'
        preimage = dict(exists=before is not None,hex=None if before is None else before.hex())
        if backup.exists():
            require(_json(backup.read_bytes(),'ordinary successor preimage') == preimage,
                'ordinary_successor_preimage_changed')
        else:
            atomic(backup,encoded(preimage))
        require((destination.read_bytes() if destination.exists() else None) == before,
            'stale_ordinary_successor_selection')
        selection.revalidate()
        atomic(destination,encoded(value))
        require(_json(destination.read_bytes(),'ordinary successor') == value,'ordinary_successor_readback_failed')
        return destination


def ordinary_successor_contract(root, initial, *, initial_contract_sha256, expected_trust_sha256, settings_path):
    """Locator triggers signed terminal readback; pointer values never authorize.

    Called after the immutable initial compiled contract is verified, before any
    new owner or child. Missing locator preserves the initial fixed launch path.
    """
    root = Path(root)
    locator = root/'_bootstrap/manager-successor.json';guard(locator)
    if not locator.exists():return initial
    require(type(expected_trust_sha256) is str and len(expected_trust_sha256)==64,
        'compiled_successor_release_trust_pin_required')
    require(locator.stat().st_size <= 16384,'ordinary_successor_locator_bounds')
    value = _json(locator.read_bytes(),'ordinary successor locator')
    require(set(value)=={'schema','transaction','binding_sha256','release_side','version','initial_contract_sha256',
        'trust_sha256','settings_relative','artifact_sha256','inventory_sha256'} and value['schema']==1
        and value['initial_contract_sha256']==initial_contract_sha256
        and value['trust_sha256']==expected_trust_sha256,'ordinary_successor_compiled_anchor_changed')
    import re
    require(type(value['transaction']) is str and re.fullmatch('[A-Za-z0-9_-]{1,64}',value['transaction'])
        and value['release_side'] in {'prior','target'},'ordinary_successor_locator_scope')
    relative = Path(value['settings_relative'])
    require(not relative.is_absolute() and '..' not in relative.parts and ':' not in value['settings_relative']
        and root/'Data'/relative == Path(settings_path),'ordinary_successor_settings_changed')
    journal_path = root/'Recovery'/value['transaction']/'transaction.json';guard(journal_path)
    require(journal_path.stat().st_size <= 1024*1024,'ordinary_successor_journal_bounds')
    journal = _json(journal_path.read_bytes(),'ordinary successor journal')
    deployment = journal['binding']['deployment']
    expected = dict(root=str(root),app_root=str(root/'Apps/Manager'),current=str(root/'Apps/Manager/current'),
        packages=str(root/'Apps/Manager/packages'),update_exe=str(root/'Apps/Manager/Update.exe'),
        manifest=str(root/'Apps/Manager/current/sq.version'),data=str(Path(settings_path).parent),
        recovery=str(root/'Recovery'),portable=True)
    require(deployment==expected,'ordinary_successor_deployment_changed')
    # Read-only gate, exact stored binding/phase. It supplies no execution receipt.
    def gate(name,document):
        require(name=='transaction_authority' and document['binding']==journal['binding'],
            'ordinary_successor_read_only_authority')
        return dict(status=name,transaction=value['transaction'],binding_sha256=digest(document['binding']))
    recovery = ManagerRecovery(expected,value['transaction'],gates=gate)
    checked = recovery.load();recovery.retained_context(checked)
    side = value['release_side'];release = checked['binding'][side]
    require(checked['phase']==('healthy' if side=='target' else 'recovery_verified')
        and digest(checked['binding'])==value['binding_sha256'],'ordinary_successor_terminal_required')
    files=release['files']
    def read(name):
        require(files[name]['size']<=MAX_METADATA_BYTES,'ordinary_successor_metadata_bounds')
        return Path(files[name]['path']).read_bytes()
    manifest,signature,trust,proof = (read(name) for name in ('manifest','signature','trust','proof'))
    require(sha256(trust).hexdigest()==expected_trust_sha256,'ordinary_successor_release_trust_changed')
    verified = verify_component_release(manifest,signature,trust,expected_component=MANAGER,
        expected_version=release['version'],expected_channel=release['channel'],
        expected_os='windows',expected_architecture='x86_64')
    archive=Path(files['archive']['path']);verify_component_artifact(archive,verified)
    require(proof==produce_detached_inventory(archive,inventory_profile=PROFILE,
        expected_version=release['version'],expected_channel=release['channel'])
        and sha256(proof).hexdigest()==verified.artifact.portable_manifest_sha256
        and value['version']==release['version'] and value['artifact_sha256']==verified.artifact.sha256
        and value['inventory_sha256']==verified.artifact.portable_manifest_sha256,
        'ordinary_successor_signed_artifact_changed')
    rows=_json(proof,'ordinary successor inventory')['files'];by_name={}
    for row in rows:
        path=root/'Apps/Manager/current'/row['path'];guard(path)
        require(observe(path)==dict(exists=True,size=row['size_bytes'],sha256=row['sha256']),
            'ordinary_successor_installed_payload_changed')
        by_name[row['path']]=row['sha256']
    require(files['executable']['sha256']==by_name[PAYLOAD_ENTRY]
        and files['inventory']['sha256']==sha256(proof).hexdigest(),'ordinary_successor_retained_code_inventory_changed')
    return dict(initial,manager_version=release['version'],manager_sha256=by_name[PAYLOAD_ENTRY])
