"""Package-pinned direct runtime authority; no publication or second owner."""
from dataclasses import dataclass
from pathlib import Path
from hashlib import sha256
import json,os,re

from core.manager_ollama_selection import pinned_json
from core.manager_llamacpp_provider import LlamaCppSelection,RuntimeFile,inspect_gguf_reference,require
from core.model_resource_registry import ResourceInspection,_current
from suite_core.installation import _path


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
def digest(value):return sha256(canonical(value)).hexdigest()


def direct_environment():
    result={name:os.environ[name] for name in ('SYSTEMROOT','WINDIR') if name in os.environ}
    result['CUDA_VISIBLE_DEVICES']='-1'
    return result


def direct_command(selection,kind,port):
    require(type(selection) is LlamaCppSelection,'typed_direct_selection_required')
    require(type(port) is int and not isinstance(port,bool),'exact_direct_port_required')
    if kind=='version':
        require(port==0,'version_port_forbidden')
        return (str(selection.executable),'--version')
    require(kind=='server' and 1024<=port<=65535,'bounded_loopback_server_port_required')
    return (str(selection.executable),'-m',str(selection.model.path),'--host','127.0.0.1','--port',str(port),
        '--ctx-size','4096','--parallel','1','--threads','2','--threads-batch','2',
        '--device','none','--n-gpu-layers','0','--alias',selection.alias,'--no-webui',
        '--cors-origins',f'http://127.0.0.1:{port}','--no-cors-credentials')


@dataclass(frozen=True)
class DirectRuntimeSelection:
    root: Path
    reference_path: Path
    reference_sha256: str
    selection: LlamaCppSelection
    environment: dict
    model_observed_by_root: bool
    runtime_version: str

    @property
    def program_sha256(self):return next(r.digest for r in self.selection.inventory if r.name.casefold()=='llama-server.exe')

    def validate(self):
        pinned_json(self.reference_path,self.reference_sha256,128*1024)
        self.selection.validate()
        expected={r.name.casefold() for r in self.selection.inventory}
        files=list(self.selection.executable.parent.iterdir())
        require(len(files)<=256,'direct_runtime_directory_bound')
        members=[p.name.casefold() for p in files if p.is_file() and (p.suffix.casefold()=='.dll' or p.name.casefold()=='llama-server.exe')]
        require(len(members)==len(expected) and set(members)==expected,'direct_runtime_member_set_changed')
        require(self.environment==direct_environment(),'direct_environment_changed')

    def context(self,kind,port):
        command=direct_command(self.selection,kind,port)
        return {'descriptor_sha256':self.reference_sha256,'runtime_inventory_sha256':digest([vars(r) for r in self.selection.inventory]),
            'runtime_version_sha256':sha256(self.runtime_version.encode('utf-8')).hexdigest(),
            'model_resource_id':self.selection.model.resource_id,'program':str(self.selection.executable),
            'working_directory':str(self.selection.executable.parent),'environment_sha256':digest(self.environment),
            'command_kind':kind,'port':str(port),'argv_sha256':digest(list(command))}

    def admitted_context(self,value,attempt):
        require(type(value) is dict and type(attempt) is str and re.fullmatch('[0-9a-f]{32}',attempt),'exact_direct_attempt_required')
        require(type(value.get('port')) is str and re.fullmatch('0|[1-9][0-9]{0,4}',value['port']),'exact_direct_port_required')
        expected=dict(self.context(value.get('command_kind'),int(value['port'])),helper_attempt_id=attempt)
        require(value==expected,'independent_direct_context_changed')
        return expected


def load_direct_reference(root,reference,*,observe_model):
    """Only a reference from the independently package-pinned contract is accepted."""
    root=_path(Path(root))
    require(type(reference) is dict and set(reference)=={'schema','relative','sha256'} and type(reference['schema']) is int and reference['schema']==1,'versioned_direct_reference_required')
    relative=reference['relative']
    require(type(relative) is str and relative=='_bootstrap/direct-runtime.json','fixed_direct_reference_location_required')
    path=_path(root/relative);pin=reference['sha256'];value=pinned_json(path,pin,128*1024)
    require(type(value) is dict and set(value)=={'schema','executable','inventory','model','working_directory','environment','runtime_version'} and type(value['schema']) is int and value['schema']==1,'direct_descriptor_schema')
    require(type(value['runtime_version']) is str and 0<len(value['runtime_version'])<=4096 and '\0' not in value['runtime_version'],'exact_direct_runtime_version_required')
    executable=_path(Path(value['executable']))
    require(str(executable)==value['executable'] and value['working_directory']==str(executable.parent),'canonical_direct_runtime_required')
    require(type(value['inventory']) is list and 1<=len(value['inventory'])<=128,'bounded_direct_inventory_required')
    inventory=[];names=set()
    for row in value['inventory']:
        require(type(row) is dict and set(row)=={'name','size','digest'},'direct_inventory_row_schema')
        name=row['name'];size=row['size'];checksum=row['digest']
        require(type(name) is str and name==Path(name).name and '\\' not in name and ':' not in name and name.casefold() not in names,'canonical_direct_inventory_member')
        require(type(size) is int and 0<size<=512*1024*1024 and type(checksum) is str and re.fullmatch('[0-9a-f]{64}',checksum),'direct_inventory_pin_required')
        require(name.casefold()=='llama-server.exe' or name.casefold().endswith('.dll'),'direct_runtime_member_kind')
        names.add(name.casefold());inventory.append(RuntimeFile(name,size,checksum))
    model=value['model']
    require(sum(r.size for r in inventory)<=2*1024*1024*1024,'direct_inventory_total_bound')
    require(type(model) is dict and set(model)=={'path','resource_id','bytes','file_state','container'},'direct_model_descriptor_schema')
    model_path=_path(Path(model['path']));require(str(model_path)==model['path'],'canonical_direct_model_required')
    require(type(model['bytes']) is int and model['bytes']>0 and type(model['resource_id']) is str and re.fullmatch('sha256:[0-9a-f]{64}',model['resource_id']),'direct_model_pin_required')
    require(type(model['file_state']) is list and len(model['file_state'])==5 and all(type(v) is int for v in model['file_state']),'direct_model_file_identity_required')
    require(type(model['container']) is dict and model['container'].get('format')=='gguf','direct_model_container_required')
    expected=ResourceInspection(model_path,model['resource_id'],model['bytes'],tuple(model['file_state']),model['container'])
    _current(expected)
    if observe_model:
        actual=inspect_gguf_reference(model_path)
        require(actual==expected,'independent_direct_model_observation_changed')
        expected=actual
    selection=LlamaCppSelection(executable,tuple(inventory),expected)
    require(type(value['environment']) is dict and value['environment']==direct_environment(),'direct_minimal_environment_required')
    result=DirectRuntimeSelection(root,path,pin,selection,dict(value['environment']),observe_model,value['runtime_version'])
    result.validate();return result
