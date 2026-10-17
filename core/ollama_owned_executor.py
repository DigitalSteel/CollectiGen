"""Owned Ollama version publication and retained process lifecycle.

No external installation, default profile, global process kill or model operation.
Caller owns one Suite layout and one exact reviewed publication/lifecycle scope.
"""
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import time
from uuid import uuid4

from core.ollama_publication_plan import OllamaPublicationPlan
from suite_core.backend_contracts import CapabilityId
from suite_core.discovery.component_staging import guard, atomic_bytes, canonical
from suite_core.installation import (SuiteLayout, InstallationRegistry, InstalledComponent, InstallationId,
    ComponentId, ComponentKind, InterfaceBinding, LocalInventory, _path, _relative)


class ExecutorHold(RuntimeError):
    pass


def require(ok, code):
    if not ok: raise ExecutorHold(code)


def entry_mapping(entry):
    return {'installation_id':entry.installation_id.value,'component_id':entry.component_id.value,
        'software_version':entry.software_version,'kind':entry.kind.value,
        'capabilities':sorted(v.value for v in entry.capabilities),
        'interfaces':[asdict(v) for v in entry.interfaces], 'location':asdict(entry.location),
        'dependencies':[v.value for v in entry.dependencies]}


def local_entry(value):
    return InstalledComponent(InstallationId(value['installation_id']),ComponentId(value['component_id']),
        value['software_version'],ComponentKind(value['kind']),frozenset(CapabilityId(v) for v in value['capabilities']),
        tuple(InterfaceBinding(**v) for v in value['interfaces']),LocalInventory(**value['location']),
        tuple(InstallationId(v) for v in value['dependencies']))


def read_json(path, limit=256*1024):
    guard(path)
    with path.open('rb') as stream:raw=stream.read(limit+1)
    require(len(raw)<=limit, 'bounded_owned_record_required')
    from core.generation_transport import strict_json
    return strict_json(raw)


def digest_file(path, *, cancelled=lambda:False):
    require(not cancelled(),'operation_cancelled')
    guard(path);before=path.stat(); digest=sha256()
    with path.open('rb') as stream:
        while block:=stream.read(1024*1024):
            require(not cancelled(),'operation_cancelled')
            digest.update(block)
    guard(path);after=path.stat()
    require(not cancelled(),'operation_cancelled')
    require((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)==
        (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns),'owned_file_changed_during_hash')
    return digest.hexdigest()


def owned_lifecycle_environment(layout, plan, system_root):
    """Stock process-local environment; pure path admission, no profile/lock writes."""
    require(type(layout) is SuiteLayout and type(plan) is OllamaPublicationPlan,
            "typed_owned_deployment_required")
    require(plan.destination == layout.code_root/plan.registration.location.relative_directory,
            "exact_layout_destination_required")
    base = _path(layout.data_root/"Manager/Ollama"/plan.registration.installation_id.value)
    root=_path(Path(system_root))
    profile=base/'Profile';temp=base/'Temp'
    return {'SYSTEMROOT':str(root),'WINDIR':str(root),'USERPROFILE':str(profile),'HOME':str(profile),
        'APPDATA':str(profile/'Roaming'),'LOCALAPPDATA':str(profile/'Local'),'TEMP':str(temp),'TMP':str(temp),
        'OLLAMA_MODELS':str(base/'Models'),'OLLAMA_HOST':'127.0.0.1:11439','OLLAMA_NUM_PARALLEL':'1',
        'OLLAMA_CONTEXT_LENGTH':'64000','OLLAMA_NO_CLOUD':'1','OLLAMA_NOPRUNE':'1','OLLAMA_KEEP_ALIVE':'0'}

class OwnedOllamaExecutor:
    """Single-owner host worker service: install -> start -> stop -> restart.

    Filesystem failures keep the staged/published bytes and explicit journal.
    Lifecycle errors retain the original child handle and refuse new starts.
    Native adapter and metadata callback are separately injected/reviewed.
    """
    def __init__(self, layout, plan, *, other_registry=lambda:InstallationRegistry(()), native=None, version_probe=None, cache_probe=None):
        require(type(layout) is SuiteLayout and type(plan) is OllamaPublicationPlan,'typed_owned_deployment_required')
        require(plan.destination==layout.code_root/plan.registration.location.relative_directory,'exact_layout_destination_required')
        self.layout,self.plan,self.other_registry=layout,plan,other_registry
        self.native,self.version_probe=native,version_probe;self.cache_probe=cache_probe
        self.base=_path(layout.data_root/'Manager/Ollama'/plan.registration.installation_id.value)
        guard(self.base);self.base.mkdir(parents=True,exist_ok=True)
        self.marker=self.base/'owner.json'
        expected={'schema':1,'owner':'suite-ollama','installation_id':plan.registration.installation_id.value,
            'code_root':str(layout.code_root),'destination':str(plan.destination),'manifest_sha256':sha256(plan.manifest_bytes).hexdigest()}
        if self.marker.exists():require(read_json(self.marker)==expected,'owned_deployment_marker_changed')
        else:
            with self.marker.open('xb') as stream:stream.write(canonical(expected));stream.flush();os.fsync(stream.fileno())
        self._marker=expected;self.record=self.base/'installation.json';self.journal=self.base/'install-state.json'
        self.cache_record=self.base/'model-cache-admission.json'
        self.session=self.base/'session.json';self.lock_path=self.base/'lifetime.lock'
        self._fd=None;self.child=None;self.identity=None;self.state='idle';self._launch_attempted=False
        self._acquire()
        try:
            if self.session.exists():
                record=read_json(self.session)
                require(record.get('owner')==self._marker and record.get('phase') in {'stopped','stopped_listener_remaining','start_cancelled_before_spawn','helper_non_creation_resolved'},'unresolved_retained_process_owner')
        except BaseException:
            self.close();raise

    @property
    def plan_identity(self):
        return sha256(canonical({'source':str(self.plan.source_payload),'destination':str(self.plan.destination),
            'manifest_sha256':sha256(self.plan.manifest_bytes).hexdigest(),'inventory':self.plan.prepared_inventory_sha256,
            'registration':entry_mapping(self.plan.registration),'expected_registry':[entry_mapping(v) for v in self.plan.expected_registry.entries]})).hexdigest()

    def _acquire(self):
        guard(self.lock_path);fd=os.open(self.lock_path,os.O_CREAT|os.O_RDWR,0o600)
        try:
            if os.fstat(fd).st_size==0:os.write(fd,b'0')
            os.lseek(fd,0,os.SEEK_SET)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BaseException:
            os.close(fd);raise ExecutorHold('owned_lifetime_busy') from None
        self._fd=fd

    def _guard(self):
        require(self._fd is not None,'owned_lifetime_required')
        require(read_json(self.marker)==self._marker,'owned_deployment_marker_changed')
        require(self.child is None or self.state in {'starting','running','held','stopping'},'retained_child_state_changed')

    def registry(self):
        base=self.other_registry();require(type(base) is InstallationRegistry,'current_registry_required')
        if not self.record.exists():return base
        value=read_json(self.record);require(value.get('schema')==1 and value.get('owner')==self._marker,'installation_record_owner_changed')
        entry=local_entry(value['installation']);require(entry==self.plan.registration,'installation_record_changed')
        require(not any(v.installation_id==entry.installation_id for v in base.entries),'duplicate_external_installation_identity')
        return InstallationRegistry((*base.entries,entry))

    def _write(self,path,value):
        self._guard();atomic_bytes(path,canonical(value));require(read_json(path)==value,'owned_record_readback_failed')

    def _verify_payload(self, directory, *, cancelled=lambda:False):
        guard(directory);manifest=self._manifest();expected=set(manifest['runtime_files'])
        require(digest_file(directory/'runtime-manifest.json',cancelled=cancelled)==self.plan.registration.location.manifest_sha256,'published_manifest_changed')
        observed=set()
        for parent,dirs,files in os.walk(directory,followlinks=False):
            require(not cancelled(),'operation_cancelled')
            guard(Path(parent))
            for name in dirs:guard(Path(parent)/name)
            for name in files:
                path=Path(parent)/name;guard(path);relative=path.relative_to(directory).as_posix();observed.add(relative)
                require(relative in expected|{'runtime-manifest.json'},'published_inventory_changed')
        require(observed==expected|{'runtime-manifest.json'},'published_inventory_changed')
        for name in sorted(expected):
            require(digest_file(directory/name,cancelled=cancelled)==manifest['payload_sha256'][name],'published_payload_changed')

    def _manifest(self):
        manifest=json.loads(self.plan.manifest_bytes)
        names=manifest['runtime_files']
        require(type(names) is list and 1<=len(names)<=128 and 'ollama.exe' in names,'bounded_runtime_inventory_required')
        require(len({n.casefold() for n in names})==len(names),'unique_runtime_paths_required')
        for name in names:_relative(name)
        require(set(manifest['payload_sha256'])==set(names),'exact_payload_hashes_required')
        require(sha256(self.plan.manifest_bytes).hexdigest()==self.plan.registration.location.manifest_sha256,'manifest_plan_pin_changed')
        return manifest

    def lifecycle_environment(self, system_root):
        return owned_lifecycle_environment(self.layout, self.plan, system_root)

    def lifecycle_identity(self, environment, *, cancelled=lambda:False):
        return sha256(canonical({'publication':self.plan_identity,'environment':environment,
            'program_sha256':digest_file(self.plan.destination/'ollama.exe',cancelled=cancelled)})).hexdigest()

    def install(self, *, reviewed_identity, cancelled=lambda:False, progress=lambda *args:None, failpoint=lambda phase:None):
        self._guard();require(self.child is None,'child_blocks_publication')
        require(reviewed_identity==self.plan_identity,'exact_publication_review_required')
        require(not self.journal.exists() and not self.record.exists() and not self.plan.destination.exists(),'existing_install_requires_recovery')
        require(self.registry()==self.plan.expected_registry,'registry_snapshot_changed')
        guard(self.plan.destination.parent);self.plan.destination.parent.mkdir(parents=True,exist_ok=True)
        require(shutil.disk_usage(self.plan.destination.parent).free>=self.plan.free_disk_required_bytes,'publication_disk_budget_not_met')
        stage=self.plan.destination.parent/('.'+self.plan.destination.name+'.staging-'+uuid4().hex)
        journal={'schema':1,'owner':self._marker,'plan_identity':self.plan_identity,'stage':str(stage),
            'destination':str(self.plan.destination),'phase':'copying'}
        self._write(self.journal,journal);guard(stage);stage.mkdir()
        try:
            manifest=self._manifest();copied=0
            for name in manifest['runtime_files']:
                require(not cancelled(),'operation_cancelled');source=self.plan.source_payload/name;guard(source)
                target=stage/name;guard(target);target.parent.mkdir(parents=True,exist_ok=True)
                digest=sha256();before=source.stat()
                with source.open('rb') as incoming,target.open('xb') as outgoing:
                    while block:=incoming.read(64*1024):
                        require(not cancelled(),'operation_cancelled');outgoing.write(block);digest.update(block);copied+=len(block);progress('copy',copied,name)
                    outgoing.flush();os.fsync(outgoing.fileno())
                after=source.stat();require((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)==
                    (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns) and digest.hexdigest()==manifest['payload_sha256'][name],'prepared_source_changed_during_copy')
            with (stage/'runtime-manifest.json').open('xb') as stream:stream.write(self.plan.manifest_bytes);stream.flush();os.fsync(stream.fileno())
            self._verify_payload(stage,cancelled=cancelled)
            journal['phase']='publish_pending';self._write(self.journal,journal);failpoint('publish_pending')
            require(not cancelled() and self.registry()==self.plan.expected_registry,'publication_cancelled_or_registry_changed')
            guard(self.plan.destination);require(not self.plan.destination.exists(),'destination_collision')
            stage.rename(self.plan.destination)  # Windows no replacement; one immutable version.
            failpoint('published_before_journal')
            journal['phase']='published';self._write(self.journal,journal);failpoint('published_before_registration')
            return self._register(journal,failpoint=failpoint)
        except BaseException:
            self.state='install_held';raise

    def _register(self,journal,*,failpoint=lambda phase:None):
        self._verify_payload(self.plan.destination)
        require(self.registry()==self.plan.expected_registry,'registry_snapshot_changed')
        value={'schema':1,'owner':self._marker,'plan_identity':self.plan_identity,'installation':entry_mapping(self.plan.registration)}
        # Exclusive new record; never replaces an existing installation record.
        guard(self.record)
        with self.record.open('xb') as stream:stream.write(canonical(value));stream.flush();os.fsync(stream.fileno())
        failpoint('registered_before_readback')
        require(read_json(self.record)==value,'installation_record_readback_failed')
        require(self.registry()==InstallationRegistry((*self.plan.expected_registry.entries,self.plan.registration)),'registration_readback_failed')
        journal['phase']='registered';self._write(self.journal,journal);self.state='installed'
        return self.plan.registration

    def recover_install(self, *, reviewed_identity):
        self._guard();require(self.child is None and reviewed_identity==self.plan_identity,'exact_idle_recovery_review_required')
        journal=read_json(self.journal);require(journal.get('owner')==self._marker and journal.get('plan_identity')==self.plan_identity
            and journal.get('destination')==str(self.plan.destination),'recovery_journal_changed')
        stage=_path(Path(journal['stage']));require(stage.parent==self.plan.destination.parent and stage.name.startswith('.'+self.plan.destination.name+'.staging-'),'exact_recovery_stage_required')
        if self.record.exists():
            self._verify_payload(self.plan.destination);require(self.registry()==InstallationRegistry((*self.plan.expected_registry.entries,self.plan.registration)),'registry_snapshot_changed');journal['phase']='registered';self._write(self.journal,journal);self.state='installed';return self.plan.registration
        require(journal['phase'] in {'publish_pending','published'},'partial_copy_retained_requires_review')
        if not self.plan.destination.exists():
            self._verify_payload(stage);require(self.registry()==self.plan.expected_registry,'registry_snapshot_changed')
            guard(self.plan.destination);stage.rename(self.plan.destination)
        self._verify_payload(self.plan.destination)
        return self._register(journal)

    def admit_cache(self, admission, *, reviewed_identity, cancelled=lambda:False):
        from core.ollama_cache_admission import OwnedCacheAdmission,verify_cache
        self._guard();require(self.child is None,'retained_child_blocks_cache_admission');self.registry()
        require(type(admission) is OwnedCacheAdmission and reviewed_identity==admission.identity,'exact_cache_admission_review_required')
        verify_cache(admission,self.base,self._marker,cancelled=cancelled)
        require(not cancelled(),'operation_cancelled')
        value={'schema':1,'owner':self._marker,'phase':'verified_cache_admitted','admission_identity':admission.identity,'admission':admission.mapping()}
        guard(self.cache_record)
        if self.cache_record.exists():require(read_json(self.cache_record)==value,'existing_cache_admission_changed')
        else:
            with self.cache_record.open('xb') as stream:stream.write(canonical(value));stream.flush();os.fsync(stream.fileno())
        require(read_json(self.cache_record)==value,'cache_admission_readback_failed')
        require(not cancelled(),'operation_cancelled')
        return admission.identity

    def _admitted_cache(self, reviewed_identity, *, cancelled=lambda:False):
        from core.ollama_cache_admission import OwnedCacheAdmission,verify_cache
        value=read_json(self.cache_record)
        admission=OwnedCacheAdmission.from_mapping(value['admission'])
        require(value=={'schema':1,'owner':self._marker,'phase':'verified_cache_admitted','admission_identity':admission.identity,'admission':admission.mapping()},'cache_admission_record_changed')
        require(reviewed_identity==admission.identity and callable(self.cache_probe),'reviewed_cache_identity_and_probe_required')
        verify_cache(admission,self.base,self._marker,cancelled=cancelled)
        return admission

    def start(self, *, environment, reviewed_lifecycle_identity, key_generation_authorized=False, reviewed_cache_identity=None, cancelled=lambda:False):
        require(not cancelled(),'operation_cancelled')
        self._guard();require(self.child is None and self.native is not None and callable(self.version_probe),'owned_native_binding_required')
        require(self.record.exists(),'registered_runtime_required');self.registry();self._verify_payload(self.plan.destination,cancelled=cancelled)
        program=self.plan.destination/'ollama.exe'
        profile=self.base/'Profile';model_root=self.base/'Models';temp=self.base/'Temp';logs=self.base/'Logs'
        expected={'USERPROFILE':str(profile),'HOME':str(profile),'APPDATA':str(profile/'Roaming'),
            'LOCALAPPDATA':str(profile/'Local'),'TEMP':str(temp),'TMP':str(temp),'OLLAMA_MODELS':str(model_root),
            'OLLAMA_HOST':'127.0.0.1:11439','OLLAMA_NUM_PARALLEL':'1','OLLAMA_CONTEXT_LENGTH':'64000',
            'OLLAMA_NO_CLOUD':'1','OLLAMA_NOPRUNE':'1','OLLAMA_KEEP_ALIVE':'0'}
        require(all(environment.get(k)==v for k,v in expected.items()),'exact_process_local_environment_required')
        require(set(environment)<=set(expected)|{'SYSTEMROOT','WINDIR','HOMEDRIVE','HOMEPATH','OLLAMA_NOHISTORY','OLLAMA_MAX_LOADED_MODELS'},'inherited_environment_or_path_refused')
        identity=self.lifecycle_identity(environment,cancelled=cancelled)
        require(reviewed_lifecycle_identity==identity and key_generation_authorized is True,'lifecycle_and_owned_key_generation_review_required')
        # Initial provisioning stays empty. Populated startup requires the exact
        # immutable admission rooted in a completed pull, never discovery/adoption.
        admission=None
        if self.cache_record.exists():admission=self._admitted_cache(reviewed_cache_identity,cancelled=cancelled)
        else:
            require(reviewed_cache_identity is None,'cache_admission_record_required')
            if model_root.exists():
                for parent,dirs,files in os.walk(model_root):
                    require(not cancelled(),'operation_cancelled');guard(Path(parent));require(not files,'model_store_not_empty_for_runner_qualification')
        self.native.require_free_port(11439)  # bind/exclusive evidence; timeout != free.
        for path in (profile,profile/'Roaming',profile/'Local',model_root,temp,logs):guard(path);path.mkdir(parents=True,exist_ok=True)
        session={'schema':1,'owner':self._marker,'phase':'launch_pending','lifecycle_identity':identity,'pid':None,
            'model_cache_identity':None if admission is None else admission.identity}
        require(not cancelled(),'operation_cancelled')
        self._write(self.session,session);self.state='starting'
        try:
            if cancelled():
                session['phase']='start_cancelled_before_spawn';self._write(self.session,session)
                raise ExecutorHold('operation_cancelled')
            spawn_options={}
            if getattr(self.native,'helper_capability',None) is not None:
                spawn_options={'cancelled':cancelled,'helper_context':{
                    'installation_id':self.plan.registration.installation_id.value,
                    'lifecycle_identity':reviewed_lifecycle_identity,
                    'program':str(program.resolve()),
                    'publication_manifest_sha256':sha256(self.plan.manifest_bytes).hexdigest(),
                    'prepared_inventory_sha256':self.plan.prepared_inventory_sha256}}
            self.child=self.native.spawn(program,environment,logs/(uuid4().hex+'.log'),**spawn_options)
            session.update(phase='spawned_identity_pending',pid=self.child.pid);self._write(self.session,session)
            self.identity=self.native.identity(self.child,program)
            require(self.identity['pid']==self.child.pid,'owned_child_identity_required')
            session.update(phase='starting',pid=self.child.pid,identity=self.identity);self._write(self.session,session)
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                require(not cancelled(),'operation_cancelled')
                require(self.child.poll() is None,'owned_child_exited_during_start')
                self.native.check_identity(self.child,self.identity,program)
                owners=self.native.listener_owners(11439)
                require(not owners or owners=={self.child.pid},'listener_not_owned_by_child')
                if owners=={self.child.pid}:
                    try:version=self.version_probe()
                    except Exception:version=None
                    if version==self.plan.registration.software_version:
                        self.native.check_identity(self.child,self.identity,program)
                        require(self.native.listener_owners(11439)=={self.child.pid},'listener_owner_changed')
                        if admission is not None:
                            from core.ollama_cache_admission import verify_cached_models
                            require(not cancelled(),'operation_cancelled')
                            verify_cached_models(admission,self.cache_probe())
                            require(not cancelled(),'operation_cancelled')
                            self.native.check_identity(self.child,self.identity,program)
                            require(self.native.listener_owners(11439)=={self.child.pid},'cached_inventory_listener_changed')
                        require(not cancelled(),'operation_cancelled')
                        self.state='running';session['phase']='running';self._write(self.session,session);return self.identity
                self.native.pause(.1)
            raise ExecutorHold('owned_runtime_startup_timeout')
        except BaseException:
            self.state='held';raise

    def stop(self):
        self._guard();require(self.child is not None,'retained_owned_child_required')
        # A spawn-returned HANDLE is authority even when querying image/creation
        # identity immediately after spawn failed. Never reconstruct it from PID.
        self.native.check_retained_handle(self.child)
        if self.identity is not None:
            self.native.check_identity(self.child,self.identity,self.plan.destination/'ollama.exe')
        self.state='stopping'
        try:
            execution_live=getattr(self.native,'owned_execution_live',lambda child:child.poll() is None)
            active=execution_live(self.child);require(type(active) is bool,'owned_execution_state_unconfirmed')
            if active:self.native.terminate_owned(self.child)
            code=self.child.wait(timeout=10)
        except Exception:
            self.state='held';raise ExecutorHold('owned_child_termination_unconfirmed') from None
        require(type(code) is int,'owned_child_exit_unconfirmed')
        listener_error=None;owners=None
        try:owners=self.native.listener_owners(11439)
        except Exception:listener_error='listener_query_unavailable'
        phase='stopped' if owners==set() else 'stopped_listener_remaining'
        self._write(self.session,{'schema':1,'owner':self._marker,'phase':phase,'pid':self.child.pid,
            'identity':self.identity,'exit_code':code,'listener_owners':None if owners is None else sorted(owners),
            'listener_error':listener_error,'termination_method':getattr(self.native,'termination_method','retained_handle_terminate')})
        self.native.close(self.child)
        self.child=None;self.identity=None;self.state=phase
        require(owners==set(),'endpoint_cleanup_unconfirmed_owned_child_exited')
        return code

    def reconcile_helper_accounting(self,*,reviewed_identity,cancelled=lambda:False):
        self._guard();require(self.child is None,'retained_child_requires_explicit_stop')
        require(self.native is not None,'owned_native_binding_required')
        def record(evidence):
            self._write(self.session,{'owner':self._marker,'phase':'helper_non_creation_resolved',
                'helper_attempt_id':evidence.attempt.identity,'evidence_id':evidence.evidence_id})
        result=self.native.reconcile_helper_non_creation(reviewed_identity,
            record=record,cancelled=cancelled)
        self.state='idle'
        return result

    def close(self):
        require(not getattr(self.native,"helper_close_held",False),"helper_accounting_blocks_owner_release")
        require(self.child is None,'retained_child_blocks_owner_release')
        if self._fd is not None:os.close(self._fd);self._fd=None
