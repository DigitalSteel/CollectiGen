"""Pure owned ZIP publication/registration preview; never copies or activates."""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path

from core.ollama_package_preparation import PreparedOllamaPackage, OllamaZipDescriptor, _windows_parts, fingerprint
from suite_core.backend_contracts import CapabilityId
from suite_core.discovery.component_staging import guard
from suite_core.installation import (SuiteLayout, InstallationRegistry, InstallationId, ComponentId,
    ComponentKind, InstalledComponent, InterfaceBinding, LocalInventory, _path)


def require(ok, reason):
    if not ok: raise ValueError(reason)


@dataclass(frozen=True)
class OllamaPublicationPlan:
    source_payload: Path
    destination: Path
    prepared_inventory_sha256: str
    manifest_bytes: bytes
    registration: InstalledComponent
    expected_registry: InstallationRegistry
    free_disk_required_bytes: int
    execution_authorized: bool = False
    publication_authorized: bool = False


def plan_owned_publication(layout, registry, descriptor, prepared, *, installation_id, owned_stage_root):
    """Reconcile exact prepared evidence and a new explicit runtime identity.

    Future executor must reserve the destination, read/hash source files, copy
    into exclusive staging, verify all payload hashes/manifest, then publish and
    compare-and-swap registry under the existing owning-host transaction. This
    preview is not an implemented publication transaction or launch permission.
    """
    require(type(layout) is SuiteLayout and type(registry) is InstallationRegistry and type(installation_id) is InstallationId, 'typed_installation_authority_required')
    require(type(descriptor) is OllamaZipDescriptor and type(prepared) is PreparedOllamaPackage, 'exact_prepared_package_required')
    root = _path(Path(owned_stage_root)); stage = _path(Path(prepared.stage_directory)); payload = _path(Path(prepared.payload_directory))
    require(stage != root and stage.is_relative_to(root) and payload == stage/'payload', 'exact_owned_stage_required')
    require(Path(prepared.receipt_path) == stage/'PACKAGE_PREPARED.json', 'exact_preparation_receipt_required')
    receipt_path = Path(prepared.receipt_path); guard(receipt_path)
    require(receipt_path.stat().st_size <= 4*1024*1024, 'bounded_preparation_receipt_required')
    receipt = json.loads(receipt_path.read_bytes())
    require(receipt == json.loads(json.dumps(asdict(prepared))), 'preparation_receipt_changed')
    require(prepared.descriptor_pin == descriptor.descriptor_pin and prepared.archive_sha256 == descriptor.archive_sha256, 'prepared_descriptor_changed')
    rows = [asdict(row) for row in prepared.files]
    require(prepared.inventory_sha256 == fingerprint(rows) and 1 <= len(rows) <= 128, 'bounded_prepared_inventory_required')
    names = [row.relative_path for row in prepared.files]
    require(len({name.casefold() for name in names}) == len(names) and 'ollama.exe' in names, 'unique_runtime_inventory_required')
    for row in prepared.files:
        target = payload.joinpath(*_windows_parts(row.relative_path)); guard(target)
        require(target.is_file() and target.stat().st_size == row.size_bytes and len(row.sha256) == 64, 'prepared_file_missing_or_changed')
        before = target.stat(); digest = sha256()
        with target.open('rb') as stream:
            while block := stream.read(1024*1024): digest.update(block)
        after = target.stat()
        require((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns) ==
            (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns) and digest.hexdigest() == row.sha256, 'prepared_content_changed')
    require(not any(row.installation_id == installation_id for row in registry.entries), 'installation_identity_exists')
    relative = 'Runtimes/'+installation_id.value
    destination = _path(layout.code_root/relative)
    require(destination.parent == _path(layout.code_root/'Runtimes') and not destination.exists(), 'new_owned_runtime_destination_required')
    manifest = {'schema_version':1,'module_id':'ollama','software_version':descriptor.version,
        'backend_contract_version':1,'runtime_files':names,'archive_sha256':descriptor.archive_sha256,
        'prepared_inventory_sha256':prepared.inventory_sha256,'payload_sha256':{r.relative_path:r.sha256 for r in prepared.files}}
    raw = json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()
    require(len(raw) <= 64*1024, 'runtime_manifest_too_large')
    registration = InstalledComponent(installation_id, ComponentId('ollama'), descriptor.version,
        ComponentKind.RUNTIME, frozenset({CapabilityId('backend.metadata')}),
        (InterfaceBinding('backend_contract_version',1),),LocalInventory(relative,'runtime-manifest.json',sha256(raw).hexdigest()))
    # Construction proves the proposed registry still obeys canonical dependency bounds.
    InstallationRegistry((*registry.entries,registration))
    return OllamaPublicationPlan(payload,destination,prepared.inventory_sha256,raw,registration,registry,
        sum(row.size_bytes for row in prepared.files)+2*1024**3)
