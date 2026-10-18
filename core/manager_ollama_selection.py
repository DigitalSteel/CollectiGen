"""Explicit pinned prepared-package selection; no download, install or native action."""
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from core.generation_transport import strict_json
from core.ollama_package_preparation import (OllamaZipDescriptor, ArchiveLimits,
    PreparedOllamaPackage, PreparedFile, _windows_parts)
from core.ollama_publication_plan import plan_owned_publication, OllamaPublicationPlan
from core.ollama_owned_executor import owned_lifecycle_environment, local_entry, require, digest_file
from suite_core.backend_contracts import CapabilityId
from suite_core.discovery.component_staging import guard
from suite_core.installation import InstallationRegistry, InstallationId, SuiteLayout, ComponentId, ComponentKind, InterfaceBinding, _path


def pinned_json(path, digest, limit):
    require(type(digest) is str and len(digest)==64 and all(c in '0123456789abcdef' for c in digest),
            'exact_selection_hash_required')
    path=_path(Path(path));guard(path)
    with path.open('rb') as stream:raw=stream.read(limit+1)
    require(len(raw)<=limit and sha256(raw).hexdigest()==digest,'selected_record_changed')
    return strict_json(raw)


@dataclass(frozen=True)
class PinnedRegistryReader:
    path: Path
    digest: str

    def read(self):
        value=pinned_json(self.path,self.digest,256*1024)
        require(type(value) is dict and set(value)=={'schema_version','entries'}
                and type(value['schema_version']) is int and value['schema_version']==1
                and type(value['entries']) is list,'exact_selected_registry_required')
        return InstallationRegistry(tuple(local_entry(row) for row in value['entries']))


@dataclass(frozen=True)
class SelectedOwnedPackage:
    layout: SuiteLayout
    descriptor: OllamaZipDescriptor
    plan: object
    environment: tuple
    registry_reader: PinnedRegistryReader = field(repr=False)
    selection_path: Path
    selection_pin: str


def select_owned_package(path, digest, *, layout, system_root):
    """Fresh exact receipt/content check through the stock publication planner.

    The separately supplied hash and approved layout are host authority. Neither
    file contents nor a registry label grant installation/start permission.
    """
    require(type(layout) is SuiteLayout,'typed_selected_layout_required')
    value=pinned_json(path,digest,256*1024)
    fields={'schema_version','descriptor','prepared_receipt','prepared_receipt_sha256',
            'registry_path','registry_sha256','owned_stage_root','installation_id','publication_pin'}
    saved={'publication_plan_path','publication_plan_sha256'}
    require(type(value) is dict and set(value) in (fields, fields|saved) and type(value['schema_version']) is int
            and value['schema_version']==1,'exact_owned_selection_schema_required')
    data=dict(value['descriptor']);data['limits']=ArchiveLimits(**data['limits'])
    descriptor=OllamaZipDescriptor(**data)
    raw=pinned_json(value['prepared_receipt'],value['prepared_receipt_sha256'],4*1024*1024)
    raw['files']=tuple(PreparedFile(**row) for row in raw['files'])
    prepared=PreparedOllamaPackage(**raw)
    require(Path(value['prepared_receipt'])==Path(prepared.receipt_path),'exact_selected_receipt_required')
    registry=PinnedRegistryReader(_path(Path(value['registry_path'])),value['registry_sha256'])
    if 'publication_plan_path' not in value:
        plan=plan_owned_publication(layout,registry.read(),descriptor,prepared,
            installation_id=InstallationId(value['installation_id']),owned_stage_root=Path(value['owned_stage_root']))
    else:
        # Reopening a previously reviewed installed runner must retain its original
        # plan, not mint a new publication identity or adopt an external instance.
        record=pinned_json(value['publication_plan_path'],value['publication_plan_sha256'],256*1024)
        require(set(record)=={'source_payload','destination','prepared_inventory_sha256','manifest_bytes',
            'registration','expected_registry','free_disk_required_bytes','execution_authorized',
            'publication_authorized'},'exact_saved_publication_plan_required')
        record['source_payload']=_path(Path(record['source_payload']))
        record['destination']=_path(Path(record['destination']))
        record['manifest_bytes']=record['manifest_bytes'].encode('utf-8')
        record['registration']=local_entry(record['registration'])
        record['expected_registry']=InstallationRegistry(tuple(local_entry(v) for v in record['expected_registry']))
        plan=OllamaPublicationPlan(**record)
        manifest=strict_json(plan.manifest_bytes)
        require(plan.source_payload==_path(Path(prepared.payload_directory))
            and plan.prepared_inventory_sha256==prepared.inventory_sha256
            and prepared.descriptor_pin==descriptor.descriptor_pin
            and prepared.archive_sha256==descriptor.archive_sha256
            and manifest['software_version']==descriptor.version==plan.registration.software_version
            and manifest['archive_sha256']==descriptor.archive_sha256
            and manifest['prepared_inventory_sha256']==prepared.inventory_sha256
            and plan.registration.installation_id==InstallationId(value['installation_id'])
            and plan.registration.component_id==ComponentId('ollama')
            and plan.registration.kind is ComponentKind.RUNTIME
            and plan.registration.capabilities==frozenset({CapabilityId('backend.metadata')})
            and plan.registration.interfaces==(InterfaceBinding('backend_contract_version',1),)
            and plan.registration.location.relative_directory=='Runtimes/'+value['installation_id']
            and plan.registration.location.manifest=='runtime-manifest.json'
            and sha256(plan.manifest_bytes).hexdigest()==plan.registration.location.manifest_sha256
            and plan.destination==layout.code_root/plan.registration.location.relative_directory
            and registry.read()==plan.expected_registry,'saved_publication_selection_changed')
        root=_path(Path(value['owned_stage_root']));stage=_path(Path(prepared.stage_directory))
        require(stage!=root and stage.is_relative_to(root) and plan.source_payload==stage/'payload'
                and Path(prepared.receipt_path)==stage/'PACKAGE_PREPARED.json','exact_owned_stage_required')
        require(manifest['runtime_files']==[row.relative_path for row in prepared.files]
                and manifest['payload_sha256']=={row.relative_path:row.sha256 for row in prepared.files},
                'saved_prepared_inventory_changed')
        require(1<=len(prepared.files)<=128 and 'ollama.exe' in manifest['runtime_files']
            and len({v.casefold() for v in manifest['runtime_files']})==len(prepared.files)
            and plan.execution_authorized is False and plan.publication_authorized is False
            and plan.free_disk_required_bytes==sum(row.size_bytes for row in prepared.files)+2*1024**3,
            'saved_publication_contract_changed')
        for row in prepared.files:
            source=plan.source_payload.joinpath(*_windows_parts(row.relative_path));guard(source)
            require(source.is_file() and source.stat().st_size==row.size_bytes
                    and digest_file(source)==row.sha256,'prepared_content_changed')
    environment=owned_lifecycle_environment(layout,plan,system_root)
    from core.manager_ollama_runtime import ManagerOllamaRuntimeBinding
    # Construction is inert; only reviewed visible actions can acquire a worker/native owner.
    binding=ManagerOllamaRuntimeBinding(layout,plan,environment,configured_registry=registry.read)
    require(binding.publication_identity==value['publication_pin'],'selected_publication_changed')
    pinned_json(path,digest,256*1024)  # Recheck after all stock callbacks/reads.
    return SelectedOwnedPackage(layout,descriptor,plan,tuple(sorted(environment.items())),
                                registry,_path(Path(path)),digest)
