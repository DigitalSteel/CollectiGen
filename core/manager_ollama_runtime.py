"""Concrete Manager worker/registry binding for the qualified owned Ollama runner.

Qt polls detached completions. Reuses the existing retained ResourceExecutorOwner,
metadata binding, exact worker tickets and installation DTOs; no new scheduler.
"""
from dataclasses import dataclass,field,replace
from datetime import datetime,timezone
from hashlib import sha256
from threading import Event,Lock,get_ident
from uuid import uuid4
import math,time,re

from core.ollama_owned_executor import OwnedOllamaExecutor,entry_mapping,require,read_json,digest_file
from core.ollama_cache_admission import OwnedCacheAdmission
from core.generation_transport import strict_json
from core.ollama_owned_windows import WindowsOwnedProcess,owned_version_probe
from core.ollama_publication_plan import OllamaPublicationPlan
from core.ollama_pull_outcome import PullOutcomeRecord
from core.manager_ollama_service import ExistingRunnerModelService,ModelActionPlan,installed_choices
from suite_core.discovery.component_staging import canonical
from suite_core.installation import SuiteLayout,InstallationRegistry
from suite_core.backend_contracts import ApplicationId,BackendId,ConnectionId,HostId,StableId
from suite_core.registry.status_view import ConfiguredConnection
from suite_core.discovery.swarm_readonly import EndpointBinding,ReadLimits
from suite_core.discovery.ollama_readonly import LocalOllamaTransport,ReadOnlyOllamaClient,ReadRoute
from suite_core.shared_backend_routing_candidate import ExistingBackend
from suite_core.manager.metadata_binding import ManagerMetadataBinding
from suite_core.manager.async_lifecycle_bridge import WorkTicket
from suite_core.manager.resource_executor import ResourceExecutorOwner


@dataclass(frozen=True)
class RuntimeSnapshot:
    state: str
    registry: InstallationRegistry | None
    endpoint: str
    runtime_version: str
    process_pin: str | None
    pid: int | None
    ownership_held: bool
    reconciliation_required: bool = False
    process_state: str = 'unobserved'


@dataclass(frozen=True)
class RuntimeCompletion:
    ticket: WorkTicket
    snapshot: RuntimeSnapshot
    value: object = field(default=None,repr=False)
    error: str | None = None
    cancelled: bool = False


class ManagerOllamaRuntimeBinding:
    """One host-owned installed record, process handle, worker and admission lock.

    Construction is inert. Explicit requests alone acquire the installation lock,
    read records or invoke a native action. Caller retains this binding across UI
    failure/closure until close_held is false. No implicit stop, startup or retry.
    """
    def __init__(self,layout,plan,environment,*,configured_registry=lambda:InstallationRegistry(()),
                 executor=None,native_factory=WindowsOwnedProcess,version_probe=owned_version_probe,
                 transport_factory=LocalOllamaTransport,model_api=None,clock=None,monotonic=time.monotonic,helper_capability=None):
        require(type(layout) is SuiteLayout and type(plan) is OllamaPublicationPlan,'typed_runtime_binding_required')
        require(plan.destination==layout.code_root/plan.registration.location.relative_directory,'runtime_layout_changed')
        require(type(environment) is dict and environment.get('OLLAMA_HOST')=='127.0.0.1:11439','reviewed_owned_endpoint_required')
        self.layout,self.plan,self.environment=layout,plan,dict(environment)
        self.configured_registry=configured_registry
        self.executor=executor if executor is not None else ResourceExecutorOwner()
        self.native_factory,self.version_probe=native_factory,version_probe
        self.helper_capability=helper_capability
        self.transport_factory,self.model_api=transport_factory,model_api
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.monotonic=monotonic
        self.bridge_id=uuid4().hex;self.generation=0;self._serial=0;self._ui_owner=get_ident()
        self._lock=Lock();self._pending=None;self._future=None;self._runtime=None;self._service=None
        self._last=None;self._progress=None;self._diagnostic=None;self.closed=False;self._reconciliation_required=False

    @property
    def owned_by_current_thread(self):return get_ident()==self._ui_owner

    @property
    def publication_identity(self):
        return sha256(canonical({'source':str(self.plan.source_payload),'destination':str(self.plan.destination),
            'manifest_sha256':sha256(self.plan.manifest_bytes).hexdigest(),'inventory':self.plan.prepared_inventory_sha256,
            'registration':entry_mapping(self.plan.registration),'expected_registry':[entry_mapping(v) for v in self.plan.expected_registry.entries]})).hexdigest()

    @property
    def pending_ticket(self):
        with self._lock:return None if self._pending is None else self._pending[0]

    @property
    def last_completion(self):return self._last

    @property
    def worker_diagnostic(self):
        """Detached code-only failure context; never exception text or native data."""
        return None if self._diagnostic is None else dict(self._diagnostic)

    def _record_failure(self, action, phase, failure):
        text=str(failure)
        trusted=type(failure).__name__ in {'ExecutorHold','RoutingError','InstallationHold','EntryHold','LaunchHold'}
        code=text if trusted and re.fullmatch(r'[a-z][a-z0-9_]{0,127}',text) else None
        self._diagnostic={'operation':action,'phase':phase,
            'exception_type':type(failure).__name__,'code':code}
        runtime=getattr(self,'_runtime',None)
        native=getattr(runtime,'native',None)
        original=getattr(native,'start_failure',None) if action=='start' else None
        if original is not None:
            detail=str(original)
            safe=type(original).__name__ in {'ExecutorHold','EntryHold','LaunchHold'}
            self._diagnostic.update(native_phase=getattr(native,'start_failure_phase',None),
                cause_type=type(original).__name__,
                cause_code=detail if safe and re.fullmatch(r'[a-z][a-z0-9_]{0,127}',detail) else None)
            cause=original
            for _ in range(4):
                winerror=getattr(cause,'winerror',None)
                if type(winerror) is int and 0<=winerror<=0xffffffff:
                    self._diagnostic['winerror']=winerror;break
                cause=getattr(cause,'__cause__',None)
                if cause is None:break

    @property
    def progress(self):
        """Latest detached bounded pull event; no unbounded queue or UI callback."""
        with self._lock:return None if self._progress is None else dict(self._progress)

    def _accept_progress(self,ticket,cancel,value):
        safe={k:value[k] for k in ('status','digest','total','completed') if k in value}
        require(len(canonical(safe))<=4096,'pull_progress_view_bound')
        with self._lock:
            if self._pending==(ticket,cancel) and not cancel.is_set():self._progress=safe

    @property
    def close_held(self):
        return (self._pending is not None or self.executor.close_held or
            self.helper_capability is not None and self.helper_capability.close_held or
            self._runtime is not None and (self._runtime.child is not None or
                getattr(self._runtime.native,"helper_close_held",False)))

    @property
    def helper_accounting_identity(self):
        # Passive reviewed identity only; capability/network evidence is worker-only.
        return None if self._runtime is None else getattr(self._runtime.native,'helper_accounting_identity',None)

    @property
    def endpoint(self):return 'http://127.0.0.1:11439'

    def request_recovery_ui(self, session, recovery_id):
        """Use the original worker owner for the authenticated visibility notice."""
        from suite_core.manager.bootstrap_entry import NormalEntrySession
        require(type(session) is NormalEntrySession,'typed_native_normal_session_required')
        require(type(recovery_id) is str and re.fullmatch('[0-9a-f]{32}',recovery_id) is not None,
            'exact_recovery_ui_identity_required')
        return self.request('claim_recovery_ui',reviewed_identity=(session,recovery_id))

    def request(self,action,*,reviewed_identity=None,key_generation_authorized=False,model_plan=None,
                pull_outcome=None,pull_authorized=False,cache_admission=None,reviewed_cache_identity=None,deadline_monotonic=None):
        require(action in {'registry','review_start','install','recover_install','start','stop','metadata','model','reconcile_pull','reconcile_helper_accounting','observe_helper_accounting','claim_recovery_ui'},'unsupported_owned_action')
        admission=None
        if cache_admission is not None:
            require(action=='start','cache_admission_only_for_start')
            admission=OwnedCacheAdmission.from_mapping(cache_admission)
            require(reviewed_cache_identity==admission.identity,'exact_cache_admission_review_required')
        require(reviewed_cache_identity is None or action=='start' and admission is not None,'reviewed_cache_mapping_required')
        require(deadline_monotonic is None or type(deadline_monotonic) in {int,float} and math.isfinite(deadline_monotonic),'finite_worker_deadline_required')
        with self._lock:
            if self.closed or self._pending is not None or self.executor.close_held:return None
            require(not self._reconciliation_required or action in {'registry','stop','recover_install','reconcile_helper_accounting','observe_helper_accounting','claim_recovery_ui'},'owned_runtime_reconciliation_required')
            if action in {'model','reconcile_pull'}:
                require(type(model_plan) is ModelActionPlan and reviewed_identity==model_plan.fingerprint,'reviewed_model_plan_required')
                if action=='model':
                    require(model_plan.action!='pull' or pull_authorized is True and pull_outcome is not None,'reviewed_owned_model_store_required')
                else:require(model_plan.action=='pull' and pull_outcome is not None,'reviewed_pull_reconciliation_required')
            self._serial+=1
            ticket=WorkTicket(self.bridge_id,self._serial,self.generation,'ollama_model_action' if action=='model' else 'ollama_runtime_'+action)
            cancel=Event();self._progress=None;self._diagnostic=None;self._pending=(ticket,cancel)
            payload=(action,reviewed_identity,key_generation_authorized,model_plan,pull_outcome,admission,reviewed_cache_identity,deadline_monotonic)
            self._future=self.executor.submit(self._work,ticket,cancel,payload)
            return ticket

    def cancel(self):
        with self._lock:
            if self._pending is None:return False
            self._pending[1].set();return True

    def _current(self,ticket,cancel):
        with self._lock:
            require(not self.closed and self._pending==(ticket,cancel) and ticket.generation==self.generation,'stale_owned_runtime_request')
            require(not cancel.is_set(),'owned_runtime_request_cancelled')

    def _ensure_runtime(self):
        if self._runtime is None:
            native=self.native_factory()
            if self.helper_capability is not None:
                native.bind_helper_capability(self.helper_capability)
            self._runtime=OwnedOllamaExecutor(self.layout,self.plan,other_registry=self.configured_registry,
                native=native,version_probe=self.version_probe,cache_probe=self._cached_inventory)
        return self._runtime

    def _cached_inventory(self):
        # Worker-only stock reads after executor proved original PID/listener/version.
        require(get_ident()!=self._ui_owner,'cache_probe_on_ui_owner_refused')
        endpoint=EndpointBinding(ApplicationId('suite-manager'),ConnectionId('manager.owned-ollama'),
            StableId('owned.'+self.plan.registration.installation_id.value),self.endpoint)
        transport=self.transport_factory(endpoint,ReadLimits())
        require(transport.binding==endpoint,'owned_cache_probe_endpoint_changed')
        return {'installed':strict_json(transport.read(ReadRoute.INSTALLED)),
            'loaded':strict_json(transport.read(ReadRoute.LOADED))}

    def _saved_cache_review(self, runtime, cancelled):
        """Review only the existing admission and completed receipt, not blobs/API.

        Actual Start still runs stock admit_cache/start verification under the
        reviewed identity; this read never grants acquisition or execution.
        """
        if not runtime.cache_record.exists():return None
        value=read_json(runtime.cache_record)
        admission=OwnedCacheAdmission.from_mapping(value['admission'])
        require(value=={'schema':1,'owner':runtime._marker,'phase':'verified_cache_admitted',
            'admission_identity':admission.identity,'admission':admission.mapping()},'cache_admission_record_changed')
        require(admission.cache_root==str(runtime.base/'Models'),'cache_owner_or_root_changed')
        receipt=runtime.base/admission.pull_record_relative
        require(digest_file(receipt,cancelled=cancelled)==admission.pull_record_sha256,'completed_pull_record_changed')
        pull=read_json(receipt)
        require(pull.get('phase')=='stream_success_and_inventory_verified'
            and re.fullmatch('[0-9a-f]{64}',pull.get('fingerprint',''))
            and pull.get('endpoint_pin')==sha256(self.endpoint.encode()).hexdigest(),'completed_exact_pull_required')
        model=pull.get('model',{})
        require(model.get('name')==admission.model_name and model.get('digest')==admission.model_digest
            and model.get('size_bytes')==admission.model_size_bytes,'completed_pull_model_changed')
        require(not cancelled(),'operation_cancelled')
        return admission.mapping()

    def _capture(self):
        runtime=self._runtime;identity=runtime.identity
        process_state='none'
        if runtime.child is not None:
            try:process_state='alive' if runtime.child.poll() is None else 'exited'
            except Exception:process_state='unconfirmed'
        return RuntimeSnapshot(runtime.state,runtime.registry(),self.endpoint,self.plan.registration.software_version,
            self._process_pin(),None if runtime.child is None else runtime.child.pid,runtime.child is not None,self._reconciliation_required,process_state)

    def _held_capture(self):
        # UI-owner read of retained in-memory ownership only, after worker teardown.
        # Registry failure cannot erase or reconstruct the original child HANDLE.
        runtime=self._runtime
        child=None if runtime is None else runtime.child
        return RuntimeSnapshot('outcome_held',None,self.endpoint,self.plan.registration.software_version,
            self._process_pin(),None if child is None else child.pid,child is not None,True)

    def _process_pin(self):
        runtime=self._runtime
        if runtime is None or runtime.child is None:return None
        # Retained spawn ownership remains reviewable if image verification failed.
        return sha256(canonical({'publication':self.publication_identity,
            'pid':runtime.child.pid,'verified_identity':runtime.identity})).hexdigest()

    def _ensure_service(self):
        if self._service is not None:return self._service
        connection=ConfiguredConnection(ApplicationId('suite-manager'),BackendId('ollama'),ConnectionId('manager.owned-ollama'),HostId('local'))
        endpoint=EndpointBinding(connection.owner,connection.connection_ref,StableId('owned.'+self.plan.registration.installation_id.value),self.endpoint)
        transport=self.transport_factory(endpoint,ReadLimits());client=ReadOnlyOllamaClient(connection,transport,clock=self.clock)
        registration=ExistingBackend(connection,client)
        binding=ManagerMetadataBinding(registration,lambda:(registration,),lambda:self._runtime is not None and self._runtime.state=='running' and self._runtime.child is not None)
        self._service=ExistingRunnerModelService(binding,self.model_api)
        return self._service

    def plan_model(self,*args,**kwargs):
        require(self._service is not None and self._last is not None and self._last.snapshot.state=='running','owned_model_metadata_binding_required')
        return self._service.plan(*args,**kwargs)  # pure, no metadata/settings read

    def _verify_owned_endpoint(self):
        runtime=self._runtime
        require(runtime.state=='running' and runtime.child is not None and runtime.child.poll() is None,'owned_runner_not_running')
        runtime.native.check_identity(runtime.child,runtime.identity,runtime.plan.destination/'ollama.exe')
        require(runtime.native.listener_owners(11439)=={runtime.child.pid},'owned_endpoint_listener_changed')
        require(self.version_probe()==runtime.plan.registration.software_version,'owned_endpoint_version_changed')

    def _work(self,ticket,cancel,payload):
        require(get_ident()!=self._ui_owner,'native_operation_on_ui_owner_refused')
        self._current(ticket,cancel);action,review,key_authorized,model_plan,pull_outcome,admission,cache_identity,deadline=payload
        cancelled=lambda:cancel.is_set() or deadline is not None and self.monotonic()>=deadline
        if action=='claim_recovery_ui':
            error=None;value=None
            try:
                from suite_core.manager.bootstrap_entry import NormalEntrySession
                session,recovery_id=review
                require(type(session) is NormalEntrySession and type(recovery_id) is str
                    and re.fullmatch('[0-9a-f]{32}',recovery_id) is not None,'exact_recovery_ui_identity_required')
                require(not cancelled(),'owned_runtime_request_cancelled_or_expired')
                value=session.claim_recovery_ui(recovery_id,timeout=1)
                require(type(value) is bool,'typed_recovery_ui_ack_required')
            except Exception as failure:
                self._record_failure(action,'recovery_visibility',failure)
                error=type(failure).__name__
            snapshot=self._capture() if self._runtime is not None else self._held_capture()
            return RuntimeCompletion(ticket,snapshot,value,error,cancelled())
        runtime=self._ensure_runtime();value=None;error=None
        try:
            require(not cancelled(),'owned_runtime_request_cancelled_or_expired')
            if action=='registry':value=runtime.registry()
            elif action=='review_start':
                value={'environment':dict(self.environment),'lifecycle_identity':runtime.lifecycle_identity(self.environment,cancelled=cancelled),
                    'cache_admission':self._saved_cache_review(runtime,cancelled)}
            elif action=='install':value=runtime.install(reviewed_identity=review,cancelled=cancelled)
            elif action=='recover_install':value=runtime.recover_install(reviewed_identity=review)
            elif action=='start':
                if admission is not None:runtime.admit_cache(admission,reviewed_identity=cache_identity,cancelled=cancelled)
                value=runtime.start(environment=self.environment,reviewed_lifecycle_identity=review,key_generation_authorized=key_authorized,
                    reviewed_cache_identity=cache_identity,cancelled=cancelled)
            elif action=='reconcile_helper_accounting':
                value=runtime.reconcile_helper_accounting(reviewed_identity=review,cancelled=cancelled)
            elif action=='observe_helper_accounting':
                require(review is not None and review==self.helper_accounting_identity,'exact_helper_attempt_review_required')
                value=runtime.native.observe_helper_accounting(review)
                require(type(value) is dict and type(value.get('phase')) is str
                    and type(value.get('creation_entered')) is bool and type(value.get('terminal')) is bool,
                    'detached_helper_observation_required')
                value=dict(value)  # Observation never clears the reconciliation latch.
            elif action=='stop':
                require(runtime.child is not None and review==self._process_pin(),'exact_retained_process_review_required')
                value=runtime.stop()
            elif action in {'metadata','model','reconcile_pull'}:
                self._verify_owned_endpoint();service=self._ensure_service()
                if action=='reconcile_pull' or action=='model' and model_plan.action=='pull':
                    require(type(pull_outcome) is PullOutcomeRecord and pull_outcome.path.parent==runtime.base/'Pulls','owned_pull_record_required')
                    require(model_plan.endpoint_pin==sha256(self.endpoint.encode()).hexdigest(),'pull_endpoint_changed')
                if action=='metadata':value=service.binding.registration.client.discover()
                elif action=='reconcile_pull':
                    report=service.binding.registration.client.discover()
                    require(service.binding.scope.accepts(report),'pull_reconciliation_scope_changed')
                    value=pull_outcome.reconcile(model_plan,installed=installed_choices(report,self.clock()),reviewed_fingerprint=review)
                else:
                    service.pull_outcome=pull_outcome
                    value=service.work(ticket,model_plan,reviewed_fingerprint=review,
                        current_pending=lambda:service.binding.pending(ticket) if self._pending==(ticket,cancel) else None,
                        cancelled=cancelled,now=self.clock,inventory=service.binding.registration.client.discover,
                        progress=lambda event:self._accept_progress(ticket,cancel,event))
                self._verify_owned_endpoint()
        except Exception as failure:
            self._record_failure(action,'runtime_action',failure)
            error=str(failure) if type(failure).__name__ in {'ExecutorHold','RoutingError','InstallationHold'} else type(failure).__name__
        return RuntimeCompletion(ticket,self._capture(),value if error is None else None,error,cancelled())

    def poll(self):
        """UI-owner admission; pending retirement is distinct from thread teardown."""
        require(get_ident()==self._ui_owner,'ui_admission_owner_required')
        with self._lock:
            if self._future is None or not self._future.done():return None
            ticket,cancel=self._pending
            if self.executor.close_held and (self._future.cancelled() or self._future.exception() is not None):
                return None  # Do not repeatedly re-raise a retained failed Future while held.
            try:
                completion=self._future.result()
                require(type(completion) is RuntimeCompletion and completion.ticket==ticket,'owned_runtime_completion_mismatch')
            except BaseException as failure:
                if self.executor.close_held:return None  # Future failure is not thread teardown.
                self._record_failure(ticket.operation,'worker_prepare_or_snapshot',failure)
                self._reconciliation_required=True
                completion=RuntimeCompletion(ticket,self._held_capture(),None,'owned_runtime_outcome_unconfirmed',cancel.is_set())
            # Even a cancelled/stale result exposes retained process/registry state;
            # it cannot turn a native side effect into a cancellation-success claim.
            if completion.cancelled or cancel.is_set() or ticket.generation!=self.generation:
                completion=RuntimeCompletion(ticket,completion.snapshot,None,'owned_action_outcome_requires_reconciliation',True)
            elif completion.error is None and ticket.operation=='ollama_model_action':
                try:
                    value=self._service.admit(completion.value,
                        current_pending=lambda:self._service.binding.pending(ticket),
                        cancelled=cancel.is_set,now=self.clock())
                    completion=RuntimeCompletion(ticket,completion.snapshot,value)
                except Exception:
                    completion=RuntimeCompletion(ticket,completion.snapshot,None,
                        'model_action_outcome_unknown_reconcile_before_retry')
            if (self._reconciliation_required and completion.error is None
                    and ticket.operation in {'ollama_runtime_registry','ollama_runtime_stop','ollama_runtime_recover_install','ollama_runtime_reconcile_helper_accounting'}
                    and completion.snapshot.registry is not None and not completion.snapshot.ownership_held
                    and completion.snapshot.state in {'idle','installed','stopped'}):
                self._reconciliation_required=False
                completion=replace(completion,snapshot=replace(completion.snapshot,reconciliation_required=False))
            self._last=completion;self._pending=None;self._future=None
            return completion

    def begin_close(self):
        # No automatic child stop. UI must explicitly review/request Stop first.
        if self.close_held:return False
        if self._runtime is not None:self._runtime.close()
        self.executor.begin_close();self.closed=True
        return not self.executor.close_held
