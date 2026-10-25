"""Exact single-model owned-cache admission; no discovery, download or blob write."""
from dataclasses import dataclass,asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from core.ollama_owned_executor import require,digest_file,read_json
from suite_core.discovery.component_staging import guard,canonical
from suite_core.installation import _relative

HEX=re.compile(r'[0-9a-f]{64}\Z')
TAG=re.compile(r'[a-z0-9][a-z0-9_.-]{0,63}:[a-z0-9][a-z0-9_.-]{0,63}\Z')

@dataclass(frozen=True)
class CacheFile:
    relative: str
    size_bytes: int
    sha256: str
    def __post_init__(self):
        _relative(self.relative)
        require(type(self.size_bytes) is int and 0<self.size_bytes<=4*1024**3,'bounded_cache_file_required')
        require(type(self.sha256) is str and HEX.fullmatch(self.sha256),'cache_digest_required')

@dataclass(frozen=True)
class OwnedCacheAdmission:
    cache_root: str
    owner_bytes: bytes
    model_name: str
    model_digest: str
    model_size_bytes: int
    pull_record_relative: str
    pull_record_sha256: str
    inventory: tuple
    evidence_sha256: str
    def __post_init__(self):
        require(type(self.cache_root) is str and Path(self.cache_root).is_absolute(),'absolute_owned_cache_required')
        require(type(self.owner_bytes) is bytes and len(self.owner_bytes)<=8192 and type(json.loads(self.owner_bytes)) is dict,'cache_owner_required')
        require(type(self.model_name) is str and TAG.fullmatch(self.model_name),'explicit_stock_model_tag_required')
        require(type(self.model_digest) is str and HEX.fullmatch(self.model_digest),'exact_model_digest_required')
        require(type(self.model_size_bytes) is int and 0<self.model_size_bytes<=4*1024**3,'bounded_model_size_required')
        _relative(self.pull_record_relative)
        require(self.pull_record_relative.startswith('Pulls/') and len(Path(self.pull_record_relative).parts)==2,'owned_pull_record_required')
        require(type(self.pull_record_sha256) is str and type(self.evidence_sha256) is str and
            HEX.fullmatch(self.pull_record_sha256) and HEX.fullmatch(self.evidence_sha256),'completed_pull_evidence_pin_required')
        require(type(self.inventory) is tuple and 1<=len(self.inventory)<=32 and all(type(f) is CacheFile for f in self.inventory),'bounded_exact_cache_inventory_required')
        names=[f.relative.casefold() for f in self.inventory]
        require(len(set(names))==len(names) and sum(f.size_bytes for f in self.inventory)<=4*1024**3,'unique_bounded_cache_inventory_required')
        require(self.mapping()['owner']==json.loads(self.owner_bytes),'cache_owner_mapping_required')
    def mapping(self):
        return {'schema':1,'cache_root':self.cache_root,'owner':json.loads(self.owner_bytes),'model_name':self.model_name,
            'model_digest':self.model_digest,'model_size_bytes':self.model_size_bytes,'pull_record_relative':self.pull_record_relative,
            'pull_record_sha256':self.pull_record_sha256,'inventory':[asdict(f) for f in self.inventory],'evidence_sha256':self.evidence_sha256}
    @property
    def identity(self):return sha256(canonical(self.mapping())).hexdigest()
    @classmethod
    def from_mapping(cls,value):
        require(type(value) is dict and value.get('schema')==1,'cache_admission_schema_required')
        plan=cls(value['cache_root'],canonical(value['owner']),value['model_name'],value['model_digest'],value['model_size_bytes'],
            value['pull_record_relative'],value['pull_record_sha256'],tuple(CacheFile(**f) for f in value['inventory']),value['evidence_sha256'])
        require(plan.mapping()==value,'cache_admission_record_changed');return plan


def verify_cache(admission,owner_base,owner_marker,*,cancelled=lambda:False):
    require(not cancelled(),'operation_cancelled')
    require(type(admission) is OwnedCacheAdmission,'typed_cache_admission_required')
    root=owner_base/'Models'
    require(Path(admission.cache_root)==root and json.loads(admission.owner_bytes)==owner_marker,'cache_owner_or_root_changed')
    pull_path=owner_base.joinpath(*_relative(admission.pull_record_relative))
    require(digest_file(pull_path,cancelled=cancelled)==admission.pull_record_sha256,'completed_pull_record_changed')
    pull=read_json(pull_path)
    require(pull.get('phase')=='stream_success_and_inventory_verified' and HEX.fullmatch(pull.get('fingerprint','')) and
        pull.get('endpoint_pin')==sha256(b'http://127.0.0.1:11439').hexdigest(),'completed_exact_pull_required')
    model=pull.get('model',{})
    require(model.get('name')==admission.model_name and model.get('digest')==admission.model_digest and model.get('size_bytes')==admission.model_size_bytes,'completed_pull_model_changed')
    expected={f.relative:f for f in admission.inventory};observed=set();snapshots={};allowed_dirs={''}
    for name in expected:
        parent=Path(name).parent
        while str(parent)!='.':allowed_dirs.add(parent.as_posix());parent=parent.parent
    guard(root)
    for parent,dirs,files in os.walk(root,followlinks=False):
        require(not cancelled(),'operation_cancelled')
        parent=Path(parent);guard(parent)
        require(len(dirs)<=32 and len(files)<=32,'cache_directory_bound')
        require(parent.relative_to(root).as_posix().removeprefix('.') in allowed_dirs,'unexplained_cache_directory')
        for name in dirs:
            guard(parent/name);require((parent/name).relative_to(root).as_posix() in allowed_dirs,'unexplained_cache_directory')
        for name in files:
            path=parent/name;guard(path);relative=path.relative_to(root).as_posix();observed.add(relative)
            require(relative in expected,'unexplained_cache_file_or_partial')
            stat=path.stat();require(stat.st_nlink==1 and stat.st_size==expected[relative].size_bytes,'cache_file_size_or_link_changed')
            snapshots[relative]=(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
    require(observed==set(expected),'cache_file_missing')
    for name in sorted(expected):require(digest_file(root/name,cancelled=cancelled)==expected[name].sha256,'cache_content_changed')
    stock_path='blobs/sha256-'+admission.model_digest
    require(stock_path in expected and expected[stock_path].sha256==admission.model_digest,'stock_manifest_pin_required')
    stock=read_json(root/stock_path,limit=16384)
    require(stock.get('schemaVersion')==2 and stock.get('mediaType')=='application/vnd.docker.distribution.manifest.v2+json','stock_manifest_required')
    layers=stock.get('layers');config=stock.get('config')
    require(type(layers) is list and 1<=len(layers)<=16 and type(config) is dict,'stock_manifest_layers_required')
    required={stock_path};total=0
    for layer in [config,*layers]:
        require(not cancelled(),'operation_cancelled')
        require(type(layer) is dict,'stock_layer_descriptor_required')
        digest=layer.get('digest','');size=layer.get('size')
        require(type(digest) is str and digest.startswith('sha256:') and HEX.fullmatch(digest[7:]) and type(size) is int and size>0,'stock_layer_descriptor_required')
        name='blobs/sha256-'+digest[7:];required.add(name);total+=size
        require(name in expected and expected[name].sha256==digest[7:] and expected[name].size_bytes==size,'stock_layer_inventory_changed')
    require(total==admission.model_size_bytes,'stock_model_size_changed')
    # Stock v0.40 aliases/anchors are metadata, not executable selection of MLX.
    family,tag=admission.model_name.split(':');legacy='manifests/registry.ollama.ai/library/'+family+'/'+tag
    modern='manifests-v2/ollama.com/library/'+family+'/'+tag
    require(legacy in expected and modern in expected,'exact_stock_tag_aliases_required')
    index=read_json(root/modern,limit=16384)
    require(index.get('mediaType')=='application/vnd.ollama.manifest.list.v2+json' and type(index.get('manifests')) is list,'stock_manifest_index_required')
    selected=[row for row in index['manifests'] if row.get('runner')=='llamacpp' and row.get('format')=='gguf']
    require(len(selected)==1 and selected[0].get('digest')=='sha256:'+admission.model_digest,'stock_selected_runner_changed')
    index_name='blobs/sha256-'+expected[modern].sha256
    require(index_name in expected and expected[index_name].sha256==expected[modern].sha256,'stock_index_blob_required')
    anchor=read_json(root/legacy,limit=16384)
    require(anchor.get('config')==config and anchor.get('layers')==layers+[
        {'mediaType':'application/vnd.ollama.manifest.list.v2+json','digest':'sha256:'+expected[modern].sha256,'size':expected[modern].size_bytes},
        {'mediaType':'application/vnd.docker.distribution.manifest.v2+json','digest':'sha256:'+admission.model_digest,'size':expected[stock_path].size_bytes}]
        and anchor.get('runner')=='llamacpp' and anchor.get('format')=='gguf','stock_legacy_anchor_changed')
    allowed=required|{legacy,modern,index_name}
    for layer in layers:
        if layer.get('mediaType') in {'application/vnd.ollama.image.model','application/vnd.ollama.image.projector'}:
            sidecar='metadata/sha256-'+layer['digest'][7:]+'.json'
            if sidecar in expected:allowed.add(sidecar)
    require(set(expected)==allowed,'unexplained_admitted_cache_inventory')
    # Earlier files must remain stable while later blobs/metadata are checked.
    fresh=set()
    for parent,dirs,files in os.walk(root,followlinks=False):
        require(not cancelled(),'operation_cancelled')
        parent=Path(parent);guard(parent)
        require(len(dirs)<=32 and len(files)<=32,'cache_directory_bound')
        for name in dirs:
            guard(parent/name);require((parent/name).relative_to(root).as_posix() in allowed_dirs,'unexplained_cache_directory')
        for name in files:
            path=parent/name;guard(path);relative=path.relative_to(root).as_posix();fresh.add(relative)
            require(relative in snapshots,'cache_changed_during_verification')
            stat=path.stat();require(stat.st_nlink==1 and
                (stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)==snapshots[relative],'cache_changed_during_verification')
    require(fresh==observed,'cache_changed_during_verification')
    require(not cancelled(),'operation_cancelled')
    return admission.identity


def verify_cached_models(admission,value):
    require(type(value) is dict and type(value.get('installed')) is dict and type(value.get('loaded')) is dict,'fresh_cached_inventory_required')
    rows=value['installed'].get('models')
    require(type(rows) is list and len(rows)==1 and value['loaded'].get('models')==[],'exact_unloaded_cache_inventory_required')
    row=rows[0]
    require(row.get('name')==admission.model_name and row.get('digest','').removeprefix('sha256:')==admission.model_digest and row.get('size')==admission.model_size_bytes,'cached_backend_model_changed')
