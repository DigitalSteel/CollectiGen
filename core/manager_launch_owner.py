"""Stable launcher ownership. No Qt, SDK, PID adoption or apply.

Caller supplies directly spawned Popen handles and a complete-scope drain
acknowledgement. A lost owner always leaves a hold for separate recovery.
Model-free startup abort uses original authority only; termination is separately
reviewed and explicit, never a consequence of timeout or normal Close.
"""
from __future__ import annotations

import ctypes
from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from threading import RLock
from functools import wraps
import time
import uuid

from core.training_source_io import _assert_unredirected

PROTOCOL = 1


class LaunchHold(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelFreeNormalAdmission:
    owner: object
    lease: int
    binding_sha256: str
    argv: tuple
    attempt: str


@dataclass(frozen=True)
class NormalAbortTicket:
    owner: object
    transport: object
    scope: object
    generation: int
    cause: str
    job: object
    outputs: tuple
    api: object


@dataclass(frozen=True)
class NormalAbortedReceipt:
    owner_token: str
    attempt: str
    receipt_sha256: str
    outcome: str = 'normal_aborted'


@dataclass(frozen=True)
class MaintenanceClosedReceipt:
    owner_token: str
    transaction_id: str
    attempt_identity_sha256: str
    receipt_sha256: str
    outcome: str = 'maintenance_closed'


@dataclass(frozen=True)
class FailedNormalRecoveryAdmission:
    """Trusted host input; never deserialize this capability from a ledger.

    The provider must qualify the complete physical scope afresh under the
    supplied lease, including independently admitted original cleanup evidence.
    No production entry supplies this admission automatically.
    """
    deployment: dict
    owner_token: str
    marker_sha256: str
    provider_id: str
    verify_physical_closure: object
    failed_ollama_session: object = None


@dataclass(frozen=True)
class FailedNormalOllamaSession:
    """One independently admitted pending session in the failed normal scope."""
    installation_id: str
    session_sha256: str
    installation_owner: dict


@dataclass(frozen=True)
class FailedNormalPhysicalEvidence:
    admission: object
    recovery_id: str
    lease: int
    marker_sha256: str
    evidence_sha256: str


@dataclass(frozen=True)
class FailedNormalDisposition:
    owner_token: str
    marker_sha256: str
    archived_marker: str
    disposition_sha256: str
    outcome: str = 'failed_normal_physically_closed'


@dataclass
class _AbortTerminalEvidence:
    owner: object
    transport: object
    api: object
    ticket: object
    payload: dict


@dataclass
class _NormalTerminalEvidence:
    owner: object
    payload: dict


@dataclass(frozen=True)
class HelperTransferSnapshot:
    owner_token: str
    attempt_id: str
    context_sha256: str
    reservation: str
    role: str
    helper_generation: int
    transfer: dict
    scope: dict


@dataclass(frozen=True)
class HelperWireAdmission:
    context: dict
    program_sha256: str
    layout: object
    plan: object
    environment: dict
    executor_module: object


@dataclass(frozen=True)
class DirectHelperWireAdmission:
    context: dict
    program_sha256: str
    selection: object


@dataclass(frozen=True)
class ReaderHelperWireAdmission:
    context: dict
    program_sha256: str
    selection: object


@dataclass(frozen=True)
class _NativeWitnessBinding:
    owner: object
    process: object
    handle: int
    api: object
    role: str
    owner_token: str
    deployment_sha256: str
    attempt: tuple
    witness: tuple

    def identity(self):
        return dict(zip(('pid','creation_filetime','executable_sha256'),self.witness))


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _creation(process):
    if os.name != "nt":
        # Portable tests still retain the actual Popen object; this is not a
        # native identity receipt and cannot authorize abnormal-owner recovery.
        return None
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = api.GetProcessTimes
    fn.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    fn.restype = wintypes.BOOL
    times = [wintypes.FILETIME() for _ in range(4)]
    if not fn(int(process._handle), *(ctypes.byref(v) for v in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime


def _serialized(method):
    @wraps(method)
    def call(self,*args,**kwargs):
        with self.dispatch_lock:
            return method(self,*args,**kwargs)
    return call


class DeploymentOwner:
    """One lease spans normal execution, cleanup hold and future maintenance.

    Never use a context manager that releases on exception: uncertain execution
    must retain the marker. abandon() closes only this owner's lock descriptor.
    """
    def __init__(self, root, *, data_root=None, entry_binding=None):
        self.root = Path(root).absolute()
        self.data_root = Path(data_root).absolute() if data_root is not None else self.root / "Data"
        self.entry_binding = deepcopy(entry_binding)
        self.directory = self.data_root / "Manager" / "LaunchOwner"
        self.marker = self.directory / "owner.json"
        self.lock = self.directory / "deployment.lock"
        self.fd = None
        self.children = {}
        # Every newly created Popen enters this collection BEFORE fallible
        # identity/marker qualification. Keep the original reference on failure.
        self.pending = []
        self.persistence_fault = None
        self.record = None
        self.raw = None
        self.dispatch_lock = RLock()
        self.records = None
        self.scope_verifier = None
        self.transaction_verifier = None
        self.stock_adapter = None
        self.native_transport = None
        self._model_free_scope = None
        self._abort_ticket = None
        self._abort_terminal = None
        self._abort_receipt = None
        self._managed_helper_accounting = None
        self._helper_attempt_refs = {}
        self._creation_entered = False
        self._normal_terminal = None
        self._normal_receipt = None
        self._maintenance_receipt = None
        self._maintenance_terminal = None
        self._unstarted_scope = None
        self._helper_wire_route = None
        self._helper_wire_binding = None
        self._helper_wire_admissions = {}
        self._helper_wire_request = None
        self._helper_wire_sequence = 0
        self._native_witness_bindings = {}
        self._helper_query_retirement = {}
        self._verified_native_witnesses = [] # original local issuance/lifetime only, never durable authority

    def _native_attempt_binding(self, role):
        data=self._managed_role(role)
        if data is not None:
            return (data['attempt_id'],data['context_sha256'],tuple(data['generation']),data['helper_generation'],data['program_sha256'])
        if role=='manager':return ('manager',self._model_free_scope.attempt if self._model_free_scope is not None else None)
        return ('direct',role)

    def _retain_verified_native_witness(self, role, process, api, witness, *, exited_cleanup=False):
        """Private input is the just independently verified native observation."""
        if (role in self._native_witness_bindings or api is not self.native_transport.api or
                getattr(process,'api',None) is not api or type(process._handle) is not int or process._handle<=0 or
                not (self.children.get(role) is process or any(item['process'] is process for item in self.pending)) or
                type(witness) is not dict or set(witness)!={'pid','creation_filetime','executable_sha256'} or
                type(witness['pid']) is not int or witness['pid']<=0 or type(witness['creation_filetime']) is not int or witness['creation_filetime']<=0 or
                type(witness['executable_sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',witness['executable_sha256']) or
                (process.poll() is not None and not exited_cleanup)):
            raise LaunchHold('verified_original_native_witness_required')
        proof=_NativeWitnessBinding(self,process,process._handle,api,role,self.record['owner'],
            hashlib.sha256(_bytes(self.record['deployment'])).hexdigest(),self._native_attempt_binding(role),
            tuple(witness[key] for key in ('pid','creation_filetime','executable_sha256')))
        self._verified_native_witnesses.append(proof)
        self._native_witness_bindings[role]=proof
        return proof

    @_serialized
    def capture_manager_native_witness(self):
        """Original retained live Manager, before Resume; never load serialized proof."""
        self._owned(allow_reservations=True)
        process=self.children.get('manager');transport=self.native_transport
        if process is None or transport is None or process.poll() is not None:
            raise LaunchHold('live_retained_manager_capture_required')
        witness=_native_witness(transport.api,process._handle)
        committed=self.record['children']['manager']
        if (witness['pid']!=committed['pid'] or witness['creation_filetime']!=committed['creation_filetime'] or
                witness['executable_sha256']!=self.record['deployment']['entry_binding']['manager_sha256']):
            raise LaunchHold('original_manager_native_binding_mismatch')
        return self._retain_verified_native_witness('manager',process,transport.api,witness)

    def _bound_native_witness(self, role):
        proof=self._native_witness_bindings.get(role);process=self.children.get(role);transport=self.native_transport
        if (type(proof) is not _NativeWitnessBinding or not any(item is proof for item in self._verified_native_witnesses) or
                proof.owner is not self or proof.process is not process or proof.role!=role or
                process is None or process._handle!=proof.handle or getattr(process,'api',None) is not proof.api or
                transport is None or transport.api is not proof.api or proof.owner_token!=self.record['owner'] or
                proof.deployment_sha256!=hashlib.sha256(_bytes(self.record['deployment'])).hexdigest() or
                proof.attempt!=self._native_attempt_binding(role)):
            raise LaunchHold('original_native_witness_binding_required')
        witness=proof.identity();committed=self.record['children'][role]
        data=self._managed_role(role)
        pin=self.record['deployment']['entry_binding']['manager_sha256'] if role=='manager' else data['program_sha256'] if data else proof.witness[2]
        if (witness['pid']!=committed['pid'] or witness['creation_filetime']!=committed['creation_filetime'] or
                witness['executable_sha256']!=pin or data is not None and witness!=data['transfer']['identity']):
            raise LaunchHold('committed_native_witness_binding_changed')
        return proof

    def _bound_native_exit(self, role):
        from ctypes import wintypes as W
        proof=self._bound_native_witness(role)
        state=proof.api.WaitForSingleObject(proof.handle,0)
        if state==258:raise LaunchHold('bound_original_native_still_running')
        if state!=0:raise LaunchHold('bound_original_native_wait_failed')
        code=W.DWORD()
        if not proof.api.GetExitCodeProcess(proof.handle,ctypes.byref(code)):raise LaunchHold('bound_original_native_exit_code_failed')
        return proof,code.value

    def prepare_managed_helper_accounting(self, *args, **kwargs):
        raise LaunchHold('managed_helper_native_provider_unqualified')

    @_serialized
    def prepare_helper_wire(self, route=None, admissions=None, *, entry_module=None):
        """Trusted installed-package binding; each role must name its exact attempt.

        Public production selection is separately gated in launcher_main. This
        wrapper does not qualify pretransfer recovery or enable a live factory.
        """
        if (type(admissions) is not dict or not admissions
                or not set(admissions).issubset({'owned-ollama','owned-llamacpp','owned-encyclopedia'})
                or {'owned-ollama','owned-llamacpp'}.issubset(admissions)):
            raise LaunchHold('exact_normal_package_admission_required')
        self._bind_helper_wire(route, admissions, entry_module=entry_module)

    @_serialized
    def admit_helper_wire_direct(self, selection):
        from core.manager_direct_admission import DirectRuntimeSelection
        self._owned(allow_pending=True,allow_reservations=True)
        if type(selection) is not DirectRuntimeSelection or selection.root!=self.root or not selection.model_observed_by_root:
            raise LaunchHold('independent_root_direct_selection_required')
        selection.validate()
        return DirectHelperWireAdmission(selection.context('version',0),selection.program_sha256,selection)

    def _fresh_helper_admission(self, admission):
        if type(admission) is ReaderHelperWireAdmission:
            return self.admit_helper_wire_reader(admission.selection)
        if type(admission) is DirectHelperWireAdmission:
            return self.admit_helper_wire_direct(admission.selection)
        if type(admission) is not HelperWireAdmission:raise LaunchHold('typed_helper_admission_required')
        return self.admit_helper_wire_package(admission.layout,admission.plan,admission.environment,executor_module=admission.executor_module)

    @_serialized
    def admit_helper_wire_reader(self, selection):
        from core.encyclopedia_connection import RootReaderSelection
        self._owned(allow_pending=True,allow_reservations=True)
        if type(selection) is not RootReaderSelection or selection.root != self.root:
            raise LaunchHold('original_root_reader_selection_required')
        return ReaderHelperWireAdmission(selection.context(),selection.program_sha256,selection)

    @_serialized
    def admit_helper_wire_package(self, layout, plan, environment, *, executor_module):
        """Read-only stock publication readback; no executor init/lease/install."""
        self._owned(allow_pending=True,allow_reservations=True)
        if (type(layout) is not executor_module.SuiteLayout or type(plan) is not executor_module.OllamaPublicationPlan or
                layout.code_root != self.root or layout.data_root != self.data_root or
                plan.destination != layout.code_root/plan.registration.location.relative_directory or
                type(environment) is not dict or not environment or len(environment)>64 or
                any(type(k) is not str or type(v) is not str or not k or len(k)>128 or len(v)>4096 or '\0' in k+v for k,v in environment.items())):
            raise LaunchHold('independent_owned_package_admission_required')
        reader=executor_module.OwnedOllamaExecutor.__new__(executor_module.OwnedOllamaExecutor)
        reader.layout=layout;reader.plan=plan
        reader._verify_payload(plan.destination)
        marker={'schema':1,'owner':'suite-ollama','installation_id':plan.registration.installation_id.value,
            'code_root':str(layout.code_root),'destination':str(plan.destination),
            'manifest_sha256':hashlib.sha256(plan.manifest_bytes).hexdigest()}
        base=layout.data_root/'Manager/Ollama'/plan.registration.installation_id.value
        if (executor_module.read_json(base/'owner.json')!=marker or
                executor_module.read_json(base/'installation.json')!={'schema':1,'owner':marker,
                'plan_identity':reader.plan_identity,'installation':executor_module.entry_mapping(plan.registration)}):
            raise LaunchHold('published_installation_readback_required')
        pin=executor_module.digest_file(plan.destination/'ollama.exe')
        context={'installation_id':plan.registration.installation_id.value,
            'lifecycle_identity':reader.lifecycle_identity(environment), 'program':str((plan.destination/'ollama.exe').resolve()),
            'publication_manifest_sha256':hashlib.sha256(plan.manifest_bytes).hexdigest(),
            'prepared_inventory_sha256':plan.prepared_inventory_sha256}
        return HelperWireAdmission(context,pin,layout,plan,deepcopy(environment),executor_module)

    @_serialized
    def bind_helper_wire_for_component_test(self, route, admissions, *, entry_module):
        return self._bind_helper_wire(route, admissions, entry_module=entry_module)

    @_serialized
    def _bind_helper_wire(self, route, admissions, *, entry_module):
        """Retain launcher-local session originals only; no remote object identity."""
        self._owned()
        if (self._helper_wire_route is not None or self._managed_helper_accounting is not None or self._model_free_scope is not None or
                self.record.get('helper_attempts') or self.record['reservations'] or self.pending or
                type(route) is not entry_module.NormalOwnerRoute or route.owner is not self or not route._live_peer() or
                route.state not in ('offered','ready') or type(admissions) is not dict or not 1<=len(admissions)<=127):
            raise LaunchHold('fresh_bound_helper_wire_required')
        for role,admission in admissions.items():
            direct=role=='owned-llamacpp' and type(admission) is DirectHelperWireAdmission
            reader=role=='owned-encyclopedia' and type(admission) is ReaderHelperWireAdmission
            if (not direct and not reader and (not (role=='owned-ollama' or re.fullmatch('owned-ollama-(?:[1-9]|[1-9][0-9]|1[01][0-9]|12[0-7])',role)) or type(admission) is not HelperWireAdmission)):
                raise LaunchHold('bounded_admitted_helper_role_required')
            fresh=self._fresh_helper_admission(admission)
            if fresh.context!=admission.context or fresh.program_sha256!=admission.program_sha256:raise LaunchHold('helper_admission_changed')
        channel=route.channel;wire=route.wire;proof=wire.proof;document=deepcopy(wire.document)
        if (type(channel) is not WindowsControlChannel or type(wire) is not entry_module._NormalWire or
                type(proof) is not NativeChannelProof or proof.source!='original_inherited_native_handles' or
                proof.binding_sha256!=hashlib.sha256(_bytes(document)).hexdigest() or
                proof.channel_epoch!=document['channel_epoch'] or document['owner_generation']!=self.record['owner'] or
                channel.transport is not self.native_transport or channel.peer_handle!=self.children['manager']._handle or
                channel.transport.api is not channel.api or channel.transport._creation_api is not channel.api or
                channel.transport._creation_job!=channel.transport.job):
            raise LaunchHold('original_native_helper_wire_required')
        self._helper_wire_route=route
        self._helper_wire_binding={'wire':wire,'proof':proof,'channel':channel,'transport':channel.transport,
            'api':channel.api,'job':channel.transport.job,'manager':self.children['manager'],
            'document':document,'module':entry_module,'helper_generation':self.record['helper_generation']}
        self._helper_wire_admissions={role:(value,deepcopy(value.context),value.program_sha256) for role,value in admissions.items()}
        self._helper_wire_sequence=wire.incoming

    def _helper_wire_peer(self, route):
        self._owned(allow_pending=True,allow_reservations=True)
        bound=self._helper_wire_binding
        if (route is not self._helper_wire_route or bound is None or route.state=='held' or not route._live_peer() or
                route.wire is not bound['wire'] or route.wire.proof is not bound['proof'] or route.channel is not bound['channel'] or
                route.wire.document!=bound['document'] or self.native_transport is not bound['transport'] or
                route.channel.api is not bound['api'] or self.native_transport.api is not bound['api'] or self.native_transport.job!=bound['job'] or
                route.channel.read_fault is not None or route.channel.write_fault is not None or
                self.children.get('manager') is not bound['manager'] or
                self.record['owner']!=bound['document']['owner_generation'] or self.record['helper_generation']!=bound['helper_generation'] or
                route.channel.peer_witness()!=bound['document']['entry'] or route.channel.local_witness()!=bound['document']['launcher']):
            raise LaunchHold('authenticated_original_helper_wire_required')

    def _helper_wire_gate(self, data, kind):
        request=self._helper_wire_request
        if request is None or request['kind']!=kind or request['attempt_id']!=data['attempt_id']:
            raise LaunchHold('bound_helper_wire_transition_required')
        self._helper_wire_peer(self._helper_wire_route)
        return request

    def _helper_wire_reply(self, data):
        return {key:deepcopy(data[key]) for key in ('attempt_id','context_sha256','role','program_sha256','reservation','phase','helper_generation')} | {
            'disposition':data['phase'], 'scope':self.registration_snapshot(),
            'row_sha256':hashlib.sha256(_bytes(data)).hexdigest(),
            'transfer_mode':data.get('transfer',{}).get('mode'),
            'native_helper':deepcopy(data.get('transfer',{}).get('identity'))}

    @_serialized
    def dispatch_helper_wire(self, route, frame):
        """Called immediately after the existing _NormalWire.receive, before send."""
        self._helper_wire_peer(route)
        module=self._helper_wire_binding['module']
        raw=_bytes(frame);module.decode_control_frame(len(raw).to_bytes(4,'little')+raw)
        module.check_frame_sequence(frame,binding_sha256=route.wire.proof.binding_sha256,next_sequence=route.wire.incoming)
        if frame['sequence']<=self._helper_wire_sequence:raise LaunchHold('helper_wire_request_replay')
        self._helper_wire_sequence=frame['sequence']
        kind=frame['kind'];payload=frame['payload']
        common={'attempt_id','context_sha256','role','program_sha256','reservation'}
        additions={'helper-reserve':{'context','creation'},'helper-register':{'source_handle','native_helper','source_entry','cleanup_only'},
            'helper-non-creation':{'creation'},'helper-status':set(),'helper-drained':{'root_exited','private_job_zero','output_drained'}}
        if (kind not in additions or set(payload)!=common|additions[kind] or
                type(payload['attempt_id']) is not str or not re.fullmatch('[0-9a-f]{32}',payload['attempt_id']) or
                type(payload['context_sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',payload['context_sha256']) or
                type(payload['role']) is not str or not (payload['role'] in self._helper_wire_admissions or
                    any(base in self._helper_wire_admissions and payload['role']==base+'-'+payload['attempt_id'] for base in ('owned-ollama','owned-llamacpp','owned-encyclopedia')))):
            raise LaunchHold('exact_correlated_helper_payload_required')
        base=next((base for base in ('owned-llamacpp','owned-encyclopedia','owned-ollama')
            if payload['role']==base or payload['role'].startswith(base+'-')),'owned-ollama')
        admission,context,pin=self._helper_wire_admissions.get(payload['role'],self._helper_wire_admissions.get(base))
        data=self.record.get('helper_attempts',{}).get(payload['attempt_id'])
        if type(admission) is DirectHelperWireAdmission:
            candidate=payload.get('context') if kind=='helper-reserve' else data.get('admitted_context') if data else None
            expected_context=admission.selection.admitted_context(candidate,payload['attempt_id'])
        else:expected_context=dict(context,helper_attempt_id=payload['attempt_id'])
        if payload['program_sha256']!=pin or payload['context_sha256']!=hashlib.sha256(_bytes(expected_context)).hexdigest():
            raise LaunchHold('independently_admitted_helper_context_required')
        if kind=='helper-reserve':
            if data is not None or payload['reservation'] is not None or payload['context']!=expected_context or payload['creation']!='not_entered':
                raise LaunchHold('fresh_unentered_helper_attempt_required')
            if any(item['role']==payload['role'] for item in self.record.get('helper_attempts',{}).values()):raise LaunchHold('helper_role_already_correlated')
            fresh=self._fresh_helper_admission(admission)
            if fresh.context!=context or fresh.program_sha256!=pin:raise LaunchHold('helper_package_changed')
            if type(admission) is DirectHelperWireAdmission and expected_context['command_kind']=='server':
                from core.ollama_owned_windows import WindowsOwnedProcess
                WindowsOwnedProcess().require_free_port(int(expected_context['port']))
            self._retire_drained_helper_queries()
            next_record=deepcopy(self.record)
            next_record.setdefault('helper_attempts',{})[payload['attempt_id']]={
                **{key:payload[key] for key in common}, 'phase':'observed','authority':'authenticated_wire',
                'generation':[self.record['owner'],route.wire.proof.channel_epoch,route.wire.proof.binding_sha256],
                'helper_generation':self.record['helper_generation']}
            if type(admission) is DirectHelperWireAdmission:
                next_record['helper_attempts'][payload['attempt_id']]['admitted_context']=deepcopy(expected_context)
            self._persist(next_record)
            data=self.record['helper_attempts'][payload['attempt_id']]
        elif (data is None or data.get('authority')!='authenticated_wire' or
                data.get('generation')!=[self.record['owner'],route.wire.proof.channel_epoch,route.wire.proof.binding_sha256] or
                data.get('helper_generation')!=self._helper_wire_binding['helper_generation'] or
                any(payload[key]!=data[key] for key in common-{'reservation'}) or
                (payload['reservation']!=data['reservation'] and not (kind=='helper-status' and payload['reservation'] is None))):
            raise LaunchHold('exact_committed_helper_correlation_required')
        self._helper_wire_request={'kind':kind,'attempt_id':payload['attempt_id']}
        try:
            if kind=='helper-reserve':
                if route.state not in ('ready','sealing','sealed') or not self.record['helper_grants_open']:
                    next_record=deepcopy(self.record);next_record['helper_attempts'][data['attempt_id']]['phase']='non_grant';self._persist(next_record)
                else:self.reserve_helper(data['role'],pin,verify_entry=lambda subject:subject is self and route._live_peer())
                reply_kind='helper-grant'
            elif kind=='helper-register':
                if data['phase']!='granted' or route.state not in ('ready','sealing') or type(payload['cleanup_only']) is not bool:
                    raise LaunchHold('single_granted_helper_transfer_required')
                if (type(payload['source_handle']) is not int or payload['source_handle']<=0 or
                        payload['source_entry']!=route.wire.document['entry']):raise LaunchHold('original_manager_source_required')
                module.check_process(module.ProcessBinding(**payload['native_helper']))
                duplicate=route.channel.duplicate_from_peer(payload['source_handle'])
                # Native duplicate is retained before the first fallible query.
                self.pending.append({'role':'reserved_helper','process':duplicate})
                identity=_native_witness(route.channel.api,duplicate._handle)
                if (identity!=payload['native_helper'] or identity['executable_sha256']!=pin or not self.native_transport.includes(duplicate) or
                        any(item['pid']==identity['pid'] and item['creation_filetime']==identity['creation_filetime'] for item in self.record['children'].values()) or
                        (payload['cleanup_only'] and duplicate.poll() is None)):
                    raise LaunchHold('original_pinned_contained_transfer_required')
                # Freeze the independently observed original before package I/O.
                # Exit during revalidation changes accounting, never startup authority.
                self._retain_verified_native_witness(data['role'],duplicate,route.channel.api,identity,
                    exited_cleanup=True)
                fresh=self._fresh_helper_admission(admission)
                if fresh.context!=context or fresh.program_sha256!=pin:raise LaunchHold('helper_package_changed')
                if type(admission) is DirectHelperWireAdmission:
                    from core.manager_direct_admission import direct_command
                    expected_argv=direct_command(admission.selection.selection,expected_context['command_kind'],int(expected_context['port']))
                    if _native_process_argv(duplicate)!=expected_argv:raise LaunchHold('native_direct_command_line_changed')
                self._helper_wire_request['transfer']={'identity':identity,'source_process_handle':payload['source_handle'],
                    'mode':'cleanup_only' if payload['cleanup_only'] or duplicate.poll() is not None else 'normal','rights':'QUERY_LIMITED_INFORMATION|SYNCHRONIZE'}
                # Existing registration normally retains immediately; this object is already retained.
                self.register_reserved_helper(data['reservation'],duplicate,verify_transfer=lambda subject,offer,process:
                    subject is self and process is duplicate and offer=={'role':data['role'],'program_sha256':pin,'helper_generation':data['helper_generation']})
                route.scope=self.registration_snapshot();self.native_transport.assert_accounted_live_scope()
                reply_kind='helper-registered'
            elif kind=='helper-non-creation':
                if data['phase']!='granted' or payload['creation']!='not_entered':raise LaunchHold('bound_positive_non_creation_required')
                proof={'attempt_id':data['attempt_id'],'context_sha256':data['context_sha256'],
                    'generation':data['generation'],'creation':'not_entered','authenticated_sequence':frame['sequence']}
                self._helper_wire_request['proof']=proof
                self.settle_helper_non_creation(data['reservation'],proof,verify_non_creation=lambda *args:True)
                reply_kind='helper-settled'
            elif kind=='helper-drained':
                if data['phase'] not in ('transferred','drained') or any(payload[key] is not True for key in additions[kind]):
                    raise LaunchHold('bound_original_helper_drain_required')
                process=self.children[data['role']]
                proof,exit_code=self._bound_native_exit(data['role'])
                if data['phase']!='drained':
                    next_record=deepcopy(self.record);row=next_record['helper_attempts'][data['attempt_id']]
                    row['phase']='drained';row['exit_code']=exit_code
                    row['manager_attestation']={'private_job_zero':True,'output_drained':True,'sequence':frame['sequence']}
                    self._persist(next_record)
                reply_kind='helper-drained'
            else:reply_kind='helper-status'
            return reply_kind,self._helper_wire_reply(self.record['helper_attempts'][data['attempt_id']])
        finally:self._helper_wire_request=None

    def _retire_drained_helper_queries(self):
        """Retire only original exited copies with committed private drain evidence.

        Attempt/transfer history remains durable. A native close entered once is
        never retried after failure; a persistence failure continues to hold.
        """
        for data in tuple(self.record.get('helper_attempts',{}).values()):
            role=data['role']
            if data.get('authority')!='authenticated_wire' or data['phase']!='drained' or role not in self.children:
                continue
            if self._helper_query_retirement.get(role) is not None:
                raise LaunchHold('original_query_retirement_recovery_required')
            proof,code=self._bound_native_exit(role)
            if (code!=data['exit_code'] or data.get('manager_attestation',{}).get('private_job_zero') is not True
                    or data['manager_attestation'].get('output_drained') is not True):
                raise LaunchHold('committed_original_helper_drain_required')
            process=self.children[role]
            self._helper_query_retirement[role]='close_entered'
            process.close_after_exit()
            next_record=deepcopy(self.record)
            row=next_record['helper_attempts'][data['attempt_id']]
            row['retired_native_identity']=next_record['children'].pop(role)
            row['query_retired']=True
            next_record['generation']+=1
            self._persist(next_record)
            del self.children[role]
            del self._native_witness_bindings[role]
            self._helper_query_retirement[role]='retired'

    @_serialized
    def managed_helper_accounting_for_component_test(self, session, native, reviewed, program_pin,
            *, entry_module, runtime_module, verify_session):
        """Injected component provider only; does not qualify live cross-process wiring."""
        self._owned()
        if self._managed_helper_accounting is not None or self._model_free_scope is not None:
            raise LaunchHold('fresh_helper_enabled_owner_required')
        provider=_ManagedHelperAccounting(self,session,native,reviewed,program_pin,entry_module,runtime_module,verify_session)
        self._managed_helper_accounting=provider
        return provider

    def _managed_role(self, role):
        records=self.record.get('helper_attempts',{})
        values=[item for item in records.values() if item['role']==role]
        if not values:return None
        if len(values)!=1:raise LaunchHold('ambiguous_helper_attempt_role')
        return values[0]

    def _managed_reservation(self, reservation):
        values=[item for item in self.record.get('helper_attempts',{}).values() if item['reservation']==reservation and reservation is not None]
        if not values:return None
        if len(values)!=1:raise LaunchHold('ambiguous_helper_attempt_reservation')
        return values[0]

    def _managed_final_guard(self, *, local_acceptance=False):
        if self._normal_terminal is not None and (type(self._normal_terminal) is not _NormalTerminalEvidence or self._normal_terminal.owner is not self):
            raise LaunchHold('same_actor_normal_terminal_required')
        attempts=self.record.get('helper_attempts',{})
        if any(item['phase'] not in ('non_grant','non_creation_settled','drained') for item in attempts.values()):
            raise LaunchHold('managed_helper_attempts_unsettled')
        if self._managed_helper_accounting is not None and getattr(self.native_transport,'observation_handles',{}):
            raise LaunchHold('managed_scope_query_resources_unsettled')
        if attempts:
            if local_acceptance:
                if (self.children['manager'].poll() is not None or any(child.poll() is None for role,child in self.children.items() if role!='manager') or
                        self.native_transport.job_active_count()!=1):raise LaunchHold('managed_outer_local_scope_unsettled')
                if ((self._managed_helper_accounting is not None and self._managed_helper_accounting.session.state=='held') or
                        (self._helper_wire_route is not None and self._helper_wire_route.state=='held')):raise LaunchHold('helper_session_recovery_unqualified')
            elif self._normal_terminal is None and (any(child.poll() is None for child in self.children.values()) or self.native_transport.job_active_count()!=0):
                raise LaunchHold('managed_outer_physical_drain_unsettled')

    @_serialized
    def arm_model_free_normal(self, argv):
        """Qualified root only, before any creation; no caller scope booleans."""
        from suite_core.manager.entry_binding import binding
        self._owned()
        if (self._model_free_scope is not None or self.native_transport is not None or
                self.record['phase'] != 'launch_hold' or self.children or self.pending or
                self.record['reservations'] or self.record['settled_non_creation'] or
                self.record['helper_generation'] or self.record['local_report'] is not None or
                type(argv) is not tuple or len(argv) < 2 or
                any(type(x) is not str or not x or '\0' in x for x in argv)):
            raise LaunchHold('prebirth_model_free_admission_required')
        admitted = binding(argv[1:], executable=argv[0], frozen=True)
        digest = hashlib.sha256(json.dumps(admitted.restart_arguments,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        entry = self.record['deployment']['entry_binding']
        _assert_unredirected(Path(argv[0]))
        before=Path(argv[0]).stat()
        image=hashlib.sha256()
        with Path(argv[0]).open('rb') as stream:
            for part in iter(lambda:stream.read(1024*1024),b''): image.update(part)
        after=Path(argv[0]).stat()
        if ((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns) != (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns) or admitted.manager_arguments or
                admitted.layout.mode.value != 'portable' or admitted.layout.code_root != self.root or
                admitted.layout.data_root != self.root/'Data' or self.data_root != self.root/'Data' or
                type(entry) is not dict or entry.get('mode') != 'portable' or
                entry.get('settings_path') != str(admitted.settings_path) or
                entry.get('argv_sha256') != digest or entry.get('manager_sha256') != image.hexdigest()):
            raise LaunchHold('complete_model_free_binding_required')
        scope=ModelFreeNormalAdmission(self,self.fd,hashlib.sha256(_bytes(self.record['deployment'])).hexdigest(),argv,uuid.uuid4().hex)
        next_record=deepcopy(self.record)
        next_record['helper_grants_open']=False
        next_record['model_free_normal']={'attempt':scope.attempt,'binding_sha256':scope.binding_sha256,'protocol':PROTOCOL}
        self._persist(next_record)
        self._model_free_scope=scope
        return scope

    def _abort_owned(self, ticket=None):
        self._owned(allow_pending=True)
        scope=self._model_free_scope
        transport=self.native_transport
        if (type(scope) is not ModelFreeNormalAdmission or scope.owner is not self or scope.lease != self.fd or
                scope.binding_sha256 != hashlib.sha256(_bytes(self.record['deployment'])).hexdigest() or
                self.entry_binding != self.record['deployment']['entry_binding'] or
                self.record.get('model_free_normal') != {'attempt':scope.attempt,'binding_sha256':scope.binding_sha256,'protocol':PROTOCOL} or
                type(transport) is not WindowsNormalTransport or transport.owner is not self or transport.abort_scope is not scope or
                transport.api is not transport._creation_api or
                (self._abort_terminal is None and transport.job != transport._creation_job) or
                (self._abort_terminal is None and transport.creation_state=='born' and transport.creation_outputs()!=transport._creation_outputs) or
                self.record['helper_grants_open'] or self.record['reservations'] or self.record['helper_generation'] or
                self.record['settled_non_creation'] or self.record['local_report'] is not None or
                self.record.get('maintenance') is not None or set(self.children)-{'manager'} or
                any(item['role'] != 'manager' for item in self.pending)):
            raise LaunchHold('same_owner_model_free_scope_required')
        if ticket is not None:
            if (ticket is not self._abort_ticket or type(ticket) is not NormalAbortTicket or
                    ticket.owner is not self or ticket.transport is not transport or ticket.scope is not scope or
                    ticket.api is not transport.api or
                    ticket.generation != self.record['generation'] or self.record['phase'] not in ('normal_abort_hold','normal_aborted') or
                    (self._abort_terminal is None and (ticket.job != transport.job or ticket.outputs != transport.creation_outputs()))):
                raise LaunchHold('exact_live_abort_ticket_required')
            terminal=self._abort_terminal
            if terminal is not None and (type(terminal) is not _AbortTerminalEvidence or terminal.owner is not self or
                    terminal.transport is not transport or terminal.api is not transport.api or terminal.ticket is not ticket):
                raise LaunchHold('same_actor_terminal_evidence_required')
        return transport

    @_serialized
    def begin_normal_abort(self, cause):
        if cause == 'control_channel_idle_timeout':raise LaunchHold('idle_is_not_abort')
        if type(cause) is not str or not re.fullmatch('[A-Za-z0-9_.-]{1,80}',cause):raise LaunchHold('bounded_abort_cause_required')
        transport=self._abort_owned()
        if self._abort_ticket is not None:
            self._abort_owned(self._abort_ticket)
            if cause != self._abort_ticket.cause:raise LaunchHold('abort_cause_changed')
            return self._abort_ticket
        if self.record['phase'] not in ('launch_hold','normal'):raise LaunchHold('normal_abort_phase')
        ticket=NormalAbortTicket(self,transport,self._model_free_scope,self.record['generation'],cause,transport.job,transport.creation_outputs(),transport.api)
        next_record=deepcopy(self.record);next_record['phase']='normal_abort_hold'
        next_record['normal_abort']={'cause':cause,'attempt':ticket.scope.attempt,'creation':transport.creation_state}
        self._persist(next_record);self._abort_ticket=ticket
        return ticket

    def _abort_original(self, transport):
        state=transport.creation_state
        outputs=transport.creation_outputs()
        if state == 'uncertain':raise LaunchHold('native_creation_uncertain')
        if state in ('not_attempted','native_failure'):
            if any(outputs) or self.children or self.pending:raise LaunchHold('positive_non_creation_required')
            return None
        if state != 'born' or not all(outputs):raise LaunchHold('original_creation_outputs_required')
        process=transport.process
        if process is not None and (type(process) is not RetainedNativeProcess or process._handle != outputs[0] or process.pid != outputs[2]):
            raise LaunchHold('original_process_replaced')
        refs=list(self.children.values())+[item['process'] for item in self.pending]
        if any(ref is not process for ref in refs):raise LaunchHold('unexpected_original_reference')
        identity=_native_witness(transport.api,outputs[0])
        if (identity['pid'] != outputs[2] or type(identity['creation_filetime']) is not int or identity['creation_filetime'] <= 0 or
                identity['executable_sha256'] != self.entry_binding['manager_sha256']):
            raise LaunchHold('native_original_identity_unknown')
        registered=self.record['children'].get('manager')
        if registered is not None and registered != {'pid':identity['pid'],'creation_filetime':identity['creation_filetime']}:
            raise LaunchHold('registered_original_changed')
        return identity

    @_serialized
    def abort_original_manager(self, ticket, *, review_action=None):
        """Only an explicit reviewed original-process action; no Job/tree termination."""
        transport=self._abort_owned(ticket)
        if self._abort_terminal is not None:raise LaunchHold('abort_resources_already_settled')
        identity=self._abort_original(transport)
        if identity is None:raise LaunchHold('no_original_to_abort')
        if not callable(review_action) or review_action(self,ticket,deepcopy(identity)) is not True:
            raise LaunchHold('explicit_original_abort_review_required')
        # Callback cannot replace the actor or mutate its authority between checks.
        self._abort_owned(ticket)
        if identity != self._abort_original(transport):raise LaunchHold('abort_identity_changed')
        if transport.api.WaitForSingleObject(ticket.outputs[0],0) != 258:raise LaunchHold('original_not_live_for_abort')
        next_record=deepcopy(self.record);next_record['normal_abort']['action']={'kind':'terminate_original','identity':identity}
        self._persist(next_record)
        if not transport.api.TerminateProcess(ticket.outputs[0],254):raise LaunchHold('original_abort_call_failed')
        return {'status':'abort_requested','identity':identity} # NOT observed exit

    @_serialized
    def finish_normal_abort(self, ticket, timeout=1):
        """Wait-only terminal proof; no local-report fabrication or automatic kill."""
        if ticket is self._abort_ticket and self._abort_receipt is not None:return self._abort_receipt
        if type(timeout) not in (int,float) or not 0 <= timeout <= 30:raise LaunchHold('bounded_abort_wait_required')
        transport=self._abort_owned(ticket)
        if self._abort_terminal is None:
            identity=self._abort_original(transport)
            exit_code=None
            if identity is not None:
                result=transport.api.WaitForSingleObject(ticket.outputs[0],int(timeout*1000))
                if result==258:raise LaunchHold('original_abort_wait_timeout')
                if result!=0:raise LaunchHold('original_abort_wait_failed')
                from ctypes import wintypes as W
                code=W.DWORD()
                if not transport.api.GetExitCodeProcess(ticket.outputs[0],ctypes.byref(code)):raise LaunchHold('original_exit_query_failed')
                exit_code=code.value
            if transport.job_state not in ('created','not_attempted','native_failure'):raise LaunchHold('native_job_creation_uncertain')
            if transport.job_state=='created' and (transport.job is None or transport.job_active_count()!=0):raise LaunchHold('owned_job_descendants_unresolved')
            # Evidence is same-object RAM only; never accepted from a caller DTO.
            self._abort_terminal=_AbortTerminalEvidence(self,transport,transport.api,ticket,
                {'identity':identity,'exit_code':exit_code,'creation':transport.creation_state,
                 'job_creation':transport.job_state,'job_zero':transport.job_state=='created','resources':{}})
        evidence=self._abort_terminal.payload
        transport.settle_abort_resources(ticket,evidence['resources'])
        if self.record['phase']!='normal_aborted':
            next_record=deepcopy(self.record);next_record['phase']='normal_aborted'
            next_record['normal_abort']['terminal']=deepcopy(evidence)
            self._persist(next_record)
        receipt=self.directory/('closed-'+self.record['owner']+'.json')
        _assert_unredirected(receipt)
        if not receipt.exists():
            with receipt.open('xb') as stream:
                stream.write(self.raw);stream.flush();os.fsync(stream.fileno())
        else:
            with receipt.open('r+b') as stream:
                if stream.read()!=self.raw:raise LaunchHold('aborted_closure_readback_failed')
                stream.flush();os.fsync(stream.fileno())
        if receipt.read_bytes()!=self.raw:raise LaunchHold('aborted_closure_readback_failed')
        self._owned(allow_pending=True)
        self.marker.unlink()
        try:self.abandon()
        except BaseException:
            # Keep a durable held marker if lease-close outcome is uncertain.
            self.persistence_fault='abort_lease_close_uncertain'
            _assert_unredirected(self.marker)
            with self.marker.open('xb') as stream:
                stream.write(self.raw);stream.flush();os.fsync(stream.fileno())
            if self.marker.read_bytes()!=self.raw:raise LaunchHold('abort_marker_restore_failed')
            raise
        self._abort_receipt=NormalAbortedReceipt(self.record['owner'],ticket.scope.attempt,hashlib.sha256(self.raw).hexdigest())
        return self._abort_receipt

    def _persist(self, next_record=None):
        if self.persistence_fault is not None:
            raise LaunchHold("owner_persistence_fault_recovery_required")
        # Commit in-memory authority only after durable write AND read-back.
        # A failed/uncertain commit latches the owner even if marker bytes later
        # look valid; caller cannot continue release/maintenance from RAM.
        temp = None
        try:
            _assert_unredirected(self.directory)
            _assert_unredirected(self.marker)
            if self.raw is not None and self.marker.read_bytes() != self.raw:
                raise LaunchHold("owner_marker_changed")
            candidate = deepcopy(self.record if next_record is None else next_record)
            raw = _bytes(candidate)
            temp = self.directory / ("write-" + uuid.uuid4().hex + ".tmp")
            with temp.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            _assert_unredirected(self.directory)
            os.replace(temp, self.marker)
            if self.marker.read_bytes() != raw:
                raise LaunchHold("owner_readback_failed")
            temp.unlink(missing_ok=True)
            self.record = candidate
            self.raw = raw
        except BaseException as exc:
            self.persistence_fault = type(exc).__name__
            # Preserve the original fault if temporary-file cleanup also fails.
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    @_serialized
    def acquire(self):
        if self.fd is not None:
            raise LaunchHold("already_owned")
        _assert_unredirected(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        _assert_unredirected(self.lock)
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise LaunchHold("suite_already_open_or_maintenance_owned") from exc
        self.fd = fd
        try:
            _assert_unredirected(self.marker)
            recovery_hold = self.directory / 'failed-normal-recovery-hold.json'
            _assert_unredirected(recovery_hold)
            if recovery_hold.exists():
                raise LaunchHold('failed_normal_retirement_recovery_required')
            if self.marker.exists():
                raise LaunchHold("unresolved_owner_recovery_required")
            self.record = {"protocol": PROTOCOL, "owner": uuid.uuid4().hex,
                           "phase": "launch_hold", "children": {}, "drain": None,
                           "deployment": {"root": str(self.root), "data": str(self.data_root),
                                          "entry_binding": self.entry_binding},
                           "generation": 0, "scope_seal": None, "local_report": None,
                           "helper_grants_open": True, "helper_generation": 0, "reservations": {}, "settled_non_creation": []}
            self._persist()
        except BaseException:
            self.abandon()
            raise
        return self

    @_serialized
    def archive_failed_normal(self, admission):
        """Explicit recovery only: preserve failure, retire its exact marker.

        This returns neither an owner nor launch/update authority. Normal acquire
        remains unchanged. Partial archival is retryable only with identical
        original bytes and a newly qualified physical closure under the lease.
        """
        if (self.fd is not None or self.record is not None or self.pending or self.children or
                type(admission) is not FailedNormalRecoveryAdmission or
                not callable(admission.verify_physical_closure) or
                not re.fullmatch('[0-9a-f]{32}', admission.owner_token) or
                not re.fullmatch('[0-9a-f]{64}', admission.marker_sha256) or
                type(admission.provider_id) is not str or not admission.provider_id):
            raise LaunchHold('qualified_failed_normal_recovery_admission_required')
        if (admission.deployment.get('root') != str(self.root) or
                admission.deployment.get('data') != str(self.data_root)):
            raise LaunchHold('failed_normal_recovery_deployment_mismatch')
        _assert_unredirected(self.directory)
        _assert_unredirected(self.lock)
        fd = os.open(self.lock, os.O_RDWR)  # Recovery never creates a missing lease.
        try:
            if os.fstat(fd).st_size < 1:
                raise LaunchHold('failed_normal_recovery_existing_lease_required')
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        recovery_id = uuid.uuid4().hex
        archive = self.directory / 'Failed' / admission.owner_token
        original = archive / (admission.marker_sha256 + '.json')
        retired = archive / (admission.marker_sha256 + '.retired.json')
        disposition = archive / (admission.marker_sha256 + '.disposition.json')
        hold = self.directory / 'failed-normal-recovery-hold.json'
        hold_raw = _bytes({'protocol': PROTOCOL, 'owner': admission.owner_token,
            'marker_sha256': admission.marker_sha256, 'deployment': admission.deployment})
        renamed = False
        raw = None
        session_fd = None
        session_path = session_raw = session_retired = None
        session_renamed = False
        try:
            _assert_unredirected(self.marker)
            _assert_unredirected(archive)
            marker_present = self.marker.exists()
            source = self.marker if marker_present else retired
            _assert_unredirected(source)
            raw = source.read_bytes()
            if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != admission.marker_sha256:
                raise LaunchHold('failed_normal_recovery_marker_changed')
            record = json.loads(raw)
            if (record.get('protocol') != PROTOCOL or record.get('owner') != admission.owner_token or
                    record.get('deployment') != admission.deployment or record.get('phase') != 'normal' or
                    record.get('reservations') != {} or record.get('scope_seal') is not None or
                    record.get('drain') is not None or record.get('local_report') is not None or
                    any(key in record for key in ('maintenance', 'transaction_id', 'replacement', 'update'))):
                raise LaunchHold('supported_failed_normal_record_required')
            scope = record.get('native_scope', {})
            attempts = record.get('helper_attempts', {})
            if (scope.get('transport') != 'inherited_anonymous_pipes' or
                    scope.get('kill_on_close') is not False or
                    not re.fullmatch(r'Local\\CollectiGen\.Normal\.[0-9a-f]{32}', scope.get('job_name', '')) or
                    type(attempts) is not dict or type(record.get('children')) is not dict or
                    'manager' not in record['children']):
                raise LaunchHold('complete_failed_normal_scope_required')
            covered = {'manager'}
            for attempt in attempts.values():
                attestation = attempt.get('manager_attestation', {})
                identity = attempt.get('transfer', {}).get('identity', {})
                role = attempt.get('role')
                if (attempt.get('authority') != 'authenticated_wire' or attempt.get('phase') != 'drained' or
                        attestation.get('private_job_zero') is not True or attestation.get('output_drained') is not True or
                        type(attempt.get('exit_code')) is not int or not identity or role in covered or
                        record['children'].get(role) != {key: identity.get(key) for key in ('pid', 'creation_filetime')}):
                    raise LaunchHold('failed_normal_helper_scope_unresolved')
                covered.add(role)
            if covered != set(record['children']):
                raise LaunchHold('failed_normal_children_scope_unresolved')
            related = admission.failed_ollama_session
            if related is not None:
                if (type(related) is not FailedNormalOllamaSession or
                        not re.fullmatch('[a-z0-9][a-z0-9-]{0,63}', related.installation_id) or
                        not re.fullmatch('[0-9a-f]{64}', related.session_sha256)):
                    raise LaunchHold('exact_failed_ollama_session_admission_required')
                base = self.data_root / 'Manager' / 'Ollama' / related.installation_id
                _assert_unredirected(base)
                session_path = base / 'session.json'
                session_lock = base / 'lifetime.lock'
                _assert_unredirected(session_lock)
                session_fd = os.open(session_lock, os.O_RDWR)
                if os.fstat(session_fd).st_size < 1:
                    raise LaunchHold('existing_failed_ollama_lease_required')
                os.lseek(session_fd, 0, os.SEEK_SET)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(session_fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(session_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                _assert_unredirected(base / 'owner.json')
                if json.loads((base / 'owner.json').read_bytes()) != related.installation_owner:
                    raise LaunchHold('failed_ollama_installation_owner_changed')
                if (related.installation_owner.get('owner') != 'suite-ollama' or
                        related.installation_owner.get('installation_id') != related.installation_id or
                        related.installation_owner.get('code_root') != str(self.root)):
                    raise LaunchHold('failed_ollama_deployment_mismatch')
                session_retired = archive / (related.session_sha256 + '.session.retired.json')
                _assert_unredirected(session_path)
                _assert_unredirected(session_retired)
                session_source = session_path if session_path.exists() else session_retired
                session_raw = session_source.read_bytes()
                if len(session_raw) > 65536 or hashlib.sha256(session_raw).hexdigest() != related.session_sha256:
                    raise LaunchHold('failed_ollama_session_changed')
                session_record = json.loads(session_raw)
                if (session_record.get('schema') != 1 or session_record.get('owner') != related.installation_owner or
                        session_record.get('phase') != 'launch_pending' or session_record.get('pid') is not None):
                    raise LaunchHold('failed_pending_ollama_session_required')

            def verify():
                # Typed evidence is local provider issuance, not serialized truth.
                proof = admission.verify_physical_closure(deepcopy(record), recovery_id, fd)
                if (type(proof) is not FailedNormalPhysicalEvidence or proof.admission is not admission or
                        proof.recovery_id != recovery_id or proof.lease != fd or
                        proof.marker_sha256 != admission.marker_sha256 or
                        not re.fullmatch('[0-9a-f]{64}', proof.evidence_sha256)):
                    raise LaunchHold('fresh_complete_failed_normal_physical_proof_required')
                return proof

            proof = verify()
            archive.mkdir(parents=True, exist_ok=True)
            _assert_unredirected(archive)

            def preserve(path, value):
                _assert_unredirected(path)
                if not path.exists():
                    temp = path.parent / (path.name + '.' + uuid.uuid4().hex + '.pending')
                    with temp.open('xb') as stream:
                        stream.write(value)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if temp.read_bytes() != value:
                        raise LaunchHold('failed_normal_archive_staging_readback_failed')
                    _assert_unredirected(path)
                    # No-replace publication on every platform. Partial pending
                    # files are retained evidence and never interpreted as proof.
                    os.link(temp, path)
                    temp.unlink()
                if path.read_bytes() != value:
                    raise LaunchHold('failed_normal_archive_conflict')

            preserve(original, raw)
            if session_raw is not None:
                preserve(archive / (related.session_sha256 + '.session.json'), session_raw)
            payload = _bytes({'protocol': PROTOCOL, 'outcome': 'failed_normal_physically_closed',
                'owner': admission.owner_token, 'deployment': admission.deployment,
                'marker_sha256': admission.marker_sha256, 'provider_id': admission.provider_id,
                'evidence_sha256': proof.evidence_sha256, 'forced_cleanup': True,
                'normal_closure': False, 'update_success': False, 'qualification_passed': False,
                'failed_ollama_session_sha256': related.session_sha256 if related is not None else None})
            preserve(disposition, payload)
            preserve(hold, hold_raw)
            # Requalify just before retirement; the independent evidence pin stays
            # fixed while fresh native observations may be repeated.
            if verify().evidence_sha256 != proof.evidence_sha256:
                raise LaunchHold('failed_normal_recovery_evidence_changed')
            _assert_unredirected(self.marker)
            if session_path is not None and session_path.exists():
                if session_path.read_bytes() != session_raw:
                    raise LaunchHold('failed_ollama_session_retirement_stale')
                if session_retired.exists():
                    if session_retired.read_bytes() != session_raw:
                        raise LaunchHold('failed_ollama_session_retirement_stale')
                    session_retired = archive / (related.session_sha256 + '.' + recovery_id + '.session.retired.json')
                os.rename(session_path, session_retired)
                session_renamed = True
            if marker_present:
                if self.marker.read_bytes() != raw:
                    raise LaunchHold('failed_normal_retirement_stale')
                _assert_unredirected(retired)
                if retired.exists():
                    if retired.read_bytes() != raw:
                        raise LaunchHold('failed_normal_retirement_stale')
                    # A previous interrupted retirement restored this exact
                    # marker. Retain both bytes; rename to another unique witness.
                    retired = archive / (admission.marker_sha256 + '.' + recovery_id + '.retired.json')
                # Windows rename refuses an existing destination. Never replace
                # or delete the marker; retain its exact bytes as failed history.
                os.rename(self.marker, retired)
                renamed = True
            elif self.marker.exists():
                raise LaunchHold('failed_normal_retirement_stale')
            if retired.read_bytes() != raw or original.read_bytes() != raw or disposition.read_bytes() != payload:
                raise LaunchHold('failed_normal_retirement_readback_failed')
            if session_retired is not None and session_retired.read_bytes() != session_raw:
                raise LaunchHold('failed_ollama_session_retirement_readback_failed')
            if hold.read_bytes() != hold_raw:
                raise LaunchHold('failed_normal_retirement_hold_changed')
            hold.unlink()  # Only transient guard; original failed ledger remains.
            return FailedNormalDisposition(admission.owner_token, admission.marker_sha256,
                str(retired), hashlib.sha256(payload).hexdigest())
        except BaseException:
            if session_renamed:
                try:
                    _assert_unredirected(session_path)
                    with session_path.open('xb') as stream:
                        stream.write(session_raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                except BaseException:
                    pass
            if renamed and raw is not None:
                # Best-effort restoration is exclusive and never overwrites a
                # new owner. The durable hold remains even if this write fails.
                try:
                    _assert_unredirected(self.marker)
                    with self.marker.open('xb') as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if self.marker.read_bytes() != raw:
                        raise LaunchHold('failed_normal_marker_restoration_failed')
                except BaseException:
                    pass
            raise
        finally:
            if session_fd is not None:
                os.close(session_fd)
            os.close(fd)

    def _owned(self, *, allow_pending=False, allow_reservations=False):
        if any(value=='close_entered' for value in self._helper_query_retirement.values()):
            raise LaunchHold('original_query_retirement_recovery_required')
        if self.persistence_fault is not None:
            raise LaunchHold("owner_persistence_fault_recovery_required")
        if self.pending and not allow_pending:
            raise LaunchHold("unqualified_owned_children_recovery_required")
        if self.record is not None and self.record["reservations"] and not allow_reservations:
            raise LaunchHold("unsettled_helper_reservations")
        _assert_unredirected(self.marker)
        if self.fd is None or self.marker.read_bytes() != self.raw:
            raise LaunchHold("owner_lease_or_marker_lost")
        if _bytes(self.record) != self.raw:
            raise LaunchHold("owner_memory_marker_mismatch")

    @_serialized
    def component_stage_scope(self, route, operation, scope_id):
        """Same original lease excludes creation/maintenance during component I/O.

        This grants no release trust, download, installation or process health.
        The component transaction engine independently validates all those inputs.
        """
        self._owned()
        from suite_core.manager.bootstrap_entry import NormalOwnerRoute
        if (type(route) is not NormalOwnerRoute or route.owner is not self or not route._live_peer() or route.state!='ready'
                or type(route.channel) is not WindowsControlChannel
                or route.channel.transport is not self.native_transport
                or route.channel.peer_handle!=self.children['manager']._handle
                or self.native_transport.api is not self.native_transport._creation_api
                or self.native_transport.job!=self.native_transport._creation_job
                or self.native_transport.job_active_count()!=1):
            raise LaunchHold('original_component_stage_peer_required')
        if operation=='begin':
            if (scope_id is not None or 'component_stage' in self.record
                    or self.record['phase']!='normal' or 'maintenance' in self.record
                    or not self.record['helper_grants_open']
                    or any(role!='manager' and child.poll() is None for role,child in self.children.items())
                    or any(row.get('phase') not in {'drained','non_creation'}
                        for row in self.record.get('helper_attempts',{}).values())):
                raise LaunchHold('idle_original_component_stage_required')
            next_record=deepcopy(self.record)
            next_record['component_stage']={'id':uuid.uuid4().hex,'component':'collectigen.tag-encyclopedia',
                'owner':self.record['owner'],'epoch':route.wire.proof.channel_epoch}
            self._persist(next_record)
        else:
            stage=self.record.get('component_stage')
            if (operation not in {'check','release'} or type(stage) is not dict
                    or stage['id']!=scope_id or stage['owner']!=self.record['owner']
                    or stage['epoch']!=route.wire.proof.channel_epoch):
                raise LaunchHold('exact_original_component_stage_required')
        stage=deepcopy(self.record['component_stage'])
        if operation=='release':
            next_record=deepcopy(self.record);next_record.pop('component_stage');self._persist(next_record)
        return {'scope_id':stage['id'],'owner_generation':stage['owner'],
                'code_root':str(self.root),'data_root':str(self.data_root)}

    @_serialized
    def spawn(self, role, argv):
        """Trusted bootstrap-only argv, directly created while lease is held.

        Creation/registration failure retains the unresolved marker. No shell,
        hidden updater launch, discovery by PID, or child termination occurs.
        """
        self._owned()
        if self._model_free_scope is not None:raise LaunchHold('model_free_native_creation_only')
        if (self.record["phase"] not in ("launch_hold", "normal", "replacement_normal") or
                type(argv) is not tuple or not argv or
                any(type(arg) is not str or not arg or "\0" in arg for arg in argv) or
                type(role) is not str or not role or len(role) > 64 or
                len(self.children) >= 128 or role in self.children):
            raise LaunchHold("invalid_spawn_contract")
        if self.record["phase"] != "launch_hold" and not self.record["helper_grants_open"]:
            raise LaunchHold("helper_grants_closed")
        self._creation_entered = True
        process = subprocess.Popen(argv, cwd=self.root, stdin=subprocess.DEVNULL)
        # No marker read, identity query or callback intervenes here.
        self.pending.append({"role": role, "process": process})
        self.retain(role, process)
        return process

    @_serialized
    def retain(self, role, process):
        """Call immediately after direct spawn, before any entry acknowledgement.

        No discovery/adoption by PID. The launcher must keep Popen alive until
        drain. Failure here leaves launch_hold; caller must not auto-release.
        """
        if self._model_free_scope is not None and role != 'manager':raise LaunchHold('model_free_helpers_forbidden')
        if any(child is process for child in self.children.values()):
            raise LaunchHold("invalid_child_registration")
        pending = next((item for item in self.pending if item["process"] is process), None)
        if pending is None:
            pending = {"role": role, "process": process}
            self.pending.append(pending)
        self._owned(allow_pending=True)
        if (self.record["phase"] not in ("launch_hold", "normal", "replacement_normal") or
                type(role) is not str or not role or len(role) > 64 or
                len(self.children) >= 128 or role in self.children or
                pending["role"] != role):
            raise LaunchHold("invalid_child_registration")
        identity = {"pid": process.pid, "creation_filetime": _creation(process)}
        next_record = deepcopy(self.record)
        next_record["children"][role] = identity
        next_record["generation"] += 1
        self._persist(next_record)
        self.children[role] = process
        self.pending.remove(pending)

    @_serialized
    def normal_ready(self, owner_token):
        self._owned()
        if owner_token != self.record["owner"] or "manager" not in self.children:
            raise LaunchHold("normal_handoff_mismatch")
        if self.record["phase"] != "launch_hold":
            raise LaunchHold("normal_handoff_phase")
        if self.children["manager"].poll() is not None:
            raise LaunchHold("manager_exited_before_readiness")
        next_record = deepcopy(self.record)
        next_record["phase"] = "normal"
        self._persist(next_record)

    @_serialized
    def registration_snapshot(self):
        self._owned(allow_reservations=True)
        return {"generation": self.record["generation"], "roles": sorted(self.children),
            "roles_sha256": hashlib.sha256(_bytes(self.record["children"])).hexdigest()}

    @_serialized
    def cleanup_hold(self):
        self._owned()
        self.stop_helper_grants()
        self._owned()
        if self.record["phase"] not in ("launch_hold", "normal", "cleanup_hold", "replacement_normal"):
            raise LaunchHold("cleanup_phase")
        next_record = deepcopy(self.record)
        next_record["phase"] = "cleanup_hold"
        next_record["drain"] = None
        next_record["scope_seal"] = {"generation": self.record["generation"],
            "roles_sha256": hashlib.sha256(_bytes(self.record["children"])).hexdigest()}
        next_record["local_report"] = None
        self._persist(next_record)
        return deepcopy(self.record["scope_seal"])

    @_serialized
    def stop_helper_grants(self):
        """First stop creation grants; outstanding reservations must then settle."""
        self._owned(allow_pending=True, allow_reservations=True)
        if 'component_stage' in self.record:raise LaunchHold('component_stage_in_progress')
        if self.record["helper_grants_open"]:
            next_record = deepcopy(self.record)
            next_record["helper_grants_open"] = False
            self._persist(next_record)

    @_serialized
    def reserve_helper(self, role, program_sha256, *, verify_entry):
        if self.record is not None and 'component_stage' in self.record:raise LaunchHold('component_stage_in_progress')
        if self._model_free_scope is not None:raise LaunchHold('model_free_helpers_forbidden')
        self._owned(allow_reservations=True)
        managed=self._managed_role(role)
        if managed is not None:
            if managed.get('authority')=='authenticated_wire':self._helper_wire_gate(managed,'helper-reserve')
            else:self._managed_helper_accounting._grant_gate(managed,program_sha256)
        if (self.record["phase"] not in ("normal", "replacement_normal") or
                not self.record["helper_grants_open"] or type(role) is not str or
                not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", role) or role in self.children or
                any(item["role"] == role for item in self.record["reservations"].values()) or
                len(self.children) + len(self.record["reservations"]) >= 128 or
                type(program_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", program_sha256) or
                verify_entry(self) is not True):
            raise LaunchHold("qualified_helper_reservation_required")
        token = uuid.uuid4().hex
        next_record = deepcopy(self.record)
        next_record["reservations"][token] = {"role": role, "program_sha256": program_sha256,
            "helper_generation": self.record["helper_generation"]}
        if managed is not None:
            attempt=next_record['helper_attempts'][managed['attempt_id']]
            attempt['reservation']=token;attempt['phase']='granted'
        self._persist(next_record)
        return token

    @_serialized
    def register_reserved_helper(self, reservation, process, *, verify_transfer):
        # Successful native duplicate must reach this collection before queries.
        if not any(item['process'] is process for item in self.pending):
            self.pending.append({"role": "reserved_helper", "process": process})
        if self._model_free_scope is not None:raise LaunchHold('model_free_helpers_forbidden')
        self._owned(allow_pending=True, allow_reservations=True)
        offer = self.record["reservations"].get(reservation)
        if offer is None or verify_transfer(self, deepcopy(offer), process) is not True:
            raise LaunchHold("unqualified_reserved_helper_transfer")
        role = offer["role"]
        managed=self._managed_reservation(reservation)
        transfer=None
        if managed is not None:
            if managed.get('authority')=='authenticated_wire':transfer=self._helper_wire_gate(managed,'helper-register')['transfer']
            else:transfer=self._managed_helper_accounting._transfer_gate(managed,process)
        witness=transfer['identity'] if transfer is not None else _native_witness(self.native_transport.api,process._handle)
        if witness['executable_sha256']!=offer['program_sha256']:raise LaunchHold('original_helper_native_pin_changed')
        retained=self._native_witness_bindings.get(role)
        if retained is None:
            self._retain_verified_native_witness(role,process,self.native_transport.api,witness,
                exited_cleanup=transfer is not None and transfer.get('mode')=='cleanup_only')
        elif (retained.owner is not self or retained.role!=role or retained.owner_token!=self.record['owner'] or
                retained.deployment_sha256!=hashlib.sha256(_bytes(self.record['deployment'])).hexdigest() or
                retained.process is not process or retained.handle!=process._handle or
                retained.api is not self.native_transport.api or retained.identity()!=witness or
                retained.attempt!=self._native_attempt_binding(role) or
                not any(item is retained for item in self._verified_native_witnesses)):
            raise LaunchHold('original_native_witness_binding_required')
        if transfer is not None and transfer.get('mode')=='normal' and process.poll() is not None:
            transfer['mode']='cleanup_only'
        identity = {"pid": witness['pid'], "creation_filetime": witness['creation_filetime']}
        next_record = deepcopy(self.record)
        next_record["children"][role] = identity
        next_record["generation"] += 1
        del next_record["reservations"][reservation]
        if managed is not None:
            attempt=next_record['helper_attempts'][managed['attempt_id']]
            attempt['phase']='transferred';attempt['transfer']=transfer
        self._persist(next_record)
        self.children[role] = process
        self.pending[:] = [item for item in self.pending if item["process"] is not process]

    @_serialized
    def settle_helper_non_creation(self, reservation, proof, *, verify_non_creation):
        if self._model_free_scope is not None:raise LaunchHold('model_free_helpers_forbidden')
        self._owned(allow_reservations=True)
        managed=self._managed_reservation(reservation)
        if managed is not None:
            if managed.get('authority')=='authenticated_wire':
                if self._helper_wire_gate(managed,'helper-non-creation')['proof'] is not proof:raise LaunchHold('bound_non_creation_request_required')
            else:self._managed_helper_accounting._settlement_gate(managed,proof)
        if (reservation not in self.record["reservations"] or
                verify_non_creation(self, reservation, proof) is not True):
            raise LaunchHold("positive_non_creation_required")
        next_record = deepcopy(self.record)
        del next_record["reservations"][reservation]
        next_record["settled_non_creation"].append({"reservation": reservation,
            "proof_sha256": hashlib.sha256(_bytes(proof)).hexdigest()})
        if managed is not None:next_record['helper_attempts'][managed['attempt_id']]['phase']='non_creation_settled'
        self._persist(next_record)

    @_serialized
    def acknowledge_drain(self, owner_token, roles, *, complete_scope):
        """Trusted host seam, not a UI claim: all helper registration is closed.

        Child exit alone is insufficient. Every retained handle must report an
        exit, and the entry/helper owner must attest no unregistered work remains.
        """
        self._owned()
        self._managed_final_guard()
        if self.native_transport is not None:
            if self.record["local_report"] is None:
                raise LaunchHold("native_local_scope_acceptance_required")
            if self.native_transport.job_active_count() != 0:
                raise LaunchHold("owned_job_descendants_unresolved")
        if (type(roles) is not tuple or any(type(role) is not str for role in roles) or
                self.record["phase"] != "cleanup_hold" or
                owner_token != self.record["owner"] or complete_scope is not True or
                "manager" not in self.children or
                set(roles) != set(self.children) or len(roles) != len(self.children)):
            raise LaunchHold("incomplete_cleanup_scope")
        exits = {role: child.poll() for role, child in self.children.items()}
        if any(value is None for value in exits.values()):
            raise LaunchHold("owned_children_still_running")
        next_record = deepcopy(self.record)
        next_record["drain"] = {"roles": sorted(roles), "exits": exits}
        self._persist(next_record)

    @_serialized
    def settle_normal_closed_resources(self):
        """Same-owner retry; terminal native evidence precedes any handle close."""
        self._owned()
        transport = self.native_transport
        if (type(transport) is not WindowsNormalTransport or transport.owner is not self or
                self.record['phase'] != 'cleanup_hold' or self.record['drain'] is None or
                self.record['local_report'] is None):
            raise LaunchHold('normal_physical_drain_required')
        if self._normal_terminal is None:
            self._managed_final_guard()
            if (transport.api is not transport._creation_api or transport.job != transport._creation_job or
                    not transport.job or transport.job_active_count() != 0 or self.pending or self.record['reservations'] or
                    any(child.poll() is None for child in self.children.values())):
                raise LaunchHold('normal_physical_drain_required')
            identities = {}
            for role, child in self.children.items():
                binding,exit_code=self._bound_native_exit(role)
                witness=binding.identity()
                if exit_code!=self.record['drain']['exits'][role]:raise LaunchHold('original_native_exit_code_changed')
                committed = self.record['children'][role]
                if (not child._handle or type(witness['creation_filetime']) is not int or witness['creation_filetime'] <= 0 or
                        witness['pid'] != committed['pid'] or witness['creation_filetime'] != committed['creation_filetime']):
                    raise LaunchHold('original_normal_identity_changed')
                if role == 'manager' and witness['executable_sha256'] != self.record['deployment']['entry_binding']['manager_sha256']:
                    raise LaunchHold('original_normal_image_changed')
                identities[role] = witness
            self._normal_terminal = _NormalTerminalEvidence(self, {'transport':transport, 'api':transport.api, 'job':transport.job,
                'owner':self.record['owner'], 'generation':self.record['generation'],
                'deployment':deepcopy(self.record['deployment']), 'drain':deepcopy(self.record['drain']),
                'record':deepcopy(self.record),
                'children':dict(self.children), 'handles':{role:(child._handle,getattr(child,'thread',None)) for role,child in self.children.items()},
                'native_bindings':dict(self._native_witness_bindings),
                'identities':identities, 'resources':{}})
        if type(self._normal_terminal) is not _NormalTerminalEvidence or self._normal_terminal.owner is not self:
            raise LaunchHold('same_actor_normal_terminal_required')
        terminal = self._normal_terminal.payload
        current_record=deepcopy(self.record);current_record.pop('normal_resources',None)
        if (terminal['transport'] is not transport or terminal['api'] is not transport.api or
                any(self._native_witness_bindings.get(role) is not proof or not any(item is proof for item in self._verified_native_witnesses)
                    for role,proof in terminal['native_bindings'].items()) or
                terminal['record'] != current_record or
                terminal['owner'] != self.record['owner'] or terminal['generation'] != self.record['generation'] or
                terminal['deployment'] != self.record['deployment'] or terminal['drain'] != self.record['drain'] or
                any(self.children.get(role) is not child for role,child in terminal['children'].items()) or
                set(self.children) != set(terminal['children'])):
            raise LaunchHold('same_actor_normal_terminal_required')
        transport.settle_normal_resources(self._normal_terminal)
        if self.record.get('normal_resources') is None:
            next_record=deepcopy(self.record)
            next_record['normal_resources']={'identities':deepcopy(terminal['identities']), 'closed':True}
            self._persist(next_record)
        return True

    @_serialized
    def finish_unstarted_normal(self, argv, cause):
        """Positive pre-creation completion; never infer non-birth from missing transport."""
        if self._normal_receipt is not None and self._unstarted_scope is not None:
            if argv != self._unstarted_scope.argv or cause != self.record['normal_abort']['cause']:
                raise LaunchHold('original_unstarted_request_required')
            return self._normal_receipt
        self._owned()
        if (self._creation_entered or self.native_transport is not None or self.children or self.pending or
                self.record['reservations'] or self.record.get('helper_attempts') or self.record['helper_generation'] or
                self.record['settled_non_creation'] or self.record['local_report'] is not None or
                self.record.get('maintenance') is not None or self._managed_helper_accounting is not None or
                type(cause) is not str or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}',cause) or 'idle' in cause):
            raise LaunchHold('positive_unentered_creation_required')
        if self._unstarted_scope is None:
            if self.record['phase'] != 'launch_hold':raise LaunchHold('original_precreation_admission_required')
            scope=self._model_free_scope or self.arm_model_free_normal(argv)
            if (scope.argv != argv or scope.owner is not self or scope.lease != self.fd or
                    scope.binding_sha256 != hashlib.sha256(_bytes(self.record['deployment'])).hexdigest() or
                    self.entry_binding != self.record['deployment']['entry_binding']):
                raise LaunchHold('original_precreation_admission_required')
            self._unstarted_scope=scope
            next_record=deepcopy(self.record);next_record['phase']='normal_aborted'
            next_record['normal_abort']={'cause':cause,'terminal':{'creation':'not_attempted','transport':'not_attempted','identity':None}}
            self._persist(next_record)
        elif (self.record['phase'] != 'normal_aborted' or cause != self.record['normal_abort']['cause'] or
                argv != self._unstarted_scope.argv or self._unstarted_scope.binding_sha256 != hashlib.sha256(_bytes(self.record['deployment'])).hexdigest()):
            raise LaunchHold('original_unstarted_request_required')
        digest=self._close_receipt_and_lease()
        self._normal_receipt=NormalAbortedReceipt(self.record['owner'],self._unstarted_scope.attempt,digest)
        return self._normal_receipt

    def _close_receipt_and_lease(self):
        receipt=self.directory/('closed-'+self.record['owner']+'.json')
        _assert_unredirected(receipt)
        if not receipt.exists():
            with receipt.open('xb') as stream:
                stream.write(self.raw);stream.flush();os.fsync(stream.fileno())
        else:
            with receipt.open('r+b') as stream:
                if stream.read()!=self.raw:raise LaunchHold('closure_readback_failed')
                stream.flush();os.fsync(stream.fileno())
        if receipt.read_bytes()!=self.raw or self.marker.read_bytes()!=self.raw:raise LaunchHold('closure_readback_failed')
        digest=hashlib.sha256(self.raw).hexdigest()
        self.marker.unlink()
        try:self.abandon()
        except BaseException:
            self.persistence_fault='normal_lease_close_uncertain'
            with self.marker.open('xb') as stream:
                stream.write(self.raw);stream.flush();os.fsync(stream.fileno())
            if self.marker.read_bytes()!=self.raw:raise LaunchHold('normal_marker_restore_failed')
            raise
        return digest

    @_serialized
    def release_closed(self):
        if self.record is not None and self.record.get('maintenance') is not None:
            raise LaunchHold('maintenance_terminal_retirement_required')
        if self._normal_receipt is not None and self._unstarted_scope is None:return self._normal_receipt
        self._owned()
        self._managed_final_guard()
        if self.record["phase"] != "cleanup_hold" or self.record["drain"] is None:
            raise LaunchHold("cleanup_acknowledgement_required")
        if self.native_transport is not None:
            if self._normal_terminal is None or self.record.get('normal_resources',{}).get('closed') is not True:
                raise LaunchHold('normal_resource_settlement_required')
            self.settle_normal_closed_resources()
        elif any(child.poll() is None for child in self.children.values()):
            raise LaunchHold("owned_children_still_running")
        # Keep a durable exact closure receipt before removing the owner marker.
        self._normal_receipt=self._close_receipt_and_lease()
        return self._normal_receipt

    @_serialized
    def maintenance_hold(self):
        """Reserve the same locked deployment for stage-2 maintenance.

        This candidate deliberately has no apply/maintenance-release adapter.
        """
        self._owned()
        if self.record["phase"] != "cleanup_hold" or self.record["drain"] is None:
            raise LaunchHold("cleanup_acknowledgement_required")
        if any(child.poll() is None for child in self.children.values()):
            raise LaunchHold("owned_children_still_running")
        next_record = deepcopy(self.record)
        next_record["phase"] = "maintenance_hold"
        self._persist(next_record)

    @_serialized
    def abandon(self):
        """Close local descriptor only; leave durable unresolved marker intact."""
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    @_serialized
    def accept_local_report(self, report, *, verify_entry):
        """Accept entry's sealed local scope while it lives; NOT physical drain.

        verify_entry must authenticate its inherited process-bound session and
        original/duplicated helper handles. The pure classifier is insufficient.
        """
        self._owned()
        self._managed_final_guard(local_acceptance=True)
        seal = self.record["scope_seal"]
        if (self.record["phase"] != "cleanup_hold" or seal is None or
                type(report) is not dict or len(_bytes(report)) > 65536 or
                report.get("owner_token") != self.record["owner"] or
                report.get("generation") != seal["generation"] or
                report.get("roles_sha256") != seal["roles_sha256"] or
                not callable(verify_entry) or verify_entry(self, report, deepcopy(seal)) is not True):
            raise LaunchHold("unqualified_local_scope_report")
        next_record = deepcopy(self.record)
        next_record["local_report"] = deepcopy(report)
        self._persist(next_record)
        return {"status": "local_scope_accepted", **deepcopy(seal)}

    def _commit_record(self, kind, payload, transaction):
        """Use existing transaction journal's immutable record interface.

        No second journal/store default. Backend must pin its transaction and
        implement immutable append, physical read-back and recovery retention.
        """
        raw = _bytes(payload)
        if len(raw) > 65536:
            raise LaunchHold("maintenance_record_bound")
        pin = hashlib.sha256(raw).hexdigest()
        ref = {"kind": kind, "transaction": transaction, "owner": self.record["owner"], "sha256": pin}
        try:
            self.records.commit(ref, raw)
            if self.records.read(ref) != raw:
                raise LaunchHold("maintenance_record_readback")
        except BaseException as exc:
            self.persistence_fault = type(exc).__name__
            raise
        return ref

    def _read_record(self, ref):
        raw = self.records.read(deepcopy(ref))
        if (type(raw) is not bytes or len(raw) > 65536 or
                hashlib.sha256(raw).hexdigest() != ref["sha256"]):
            raise LaunchHold("committed_record_changed")
        return json.loads(raw)

    def _original_tokens(self, drain):
        return tuple(hashlib.sha256(_bytes({"owner": drain["owner"],
            "deployment": drain["deployment"], "role": role,
            "identity": drain["children"][role]})).hexdigest()
            for role in sorted(drain["children"]))

    @_serialized
    def prepare_maintenance(self, adapter, fields, *, records, scope_verifier, transaction_verifier):
        """Freeze committed drain/intent/attempt under the existing live lease.

        fields excludes the three generated receipt pins and original tokens.
        All original role identities are included; >64 holds, never truncates.
        """
        self._owned()
        if (self.record["phase"] != "cleanup_hold" or self.record["drain"] is None or
                self.record["local_report"] is None or "maintenance" in self.record or
                not 1 <= len(self.children) <= 64 or not callable(scope_verifier) or
                not callable(transaction_verifier)):
            raise LaunchHold("qualified_bounded_original_drain_required")
        if any(child.poll() is None for child in self.children.values()):
            raise LaunchHold("owned_children_still_running")
        supplied = deepcopy(fields)
        supplied["original_owner_tokens"] = self._original_tokens(self.record)
        # Pure lexical admission before committing any receipt; no fake pins are
        # ever returned or persisted. Final request uses actual committed hashes.
        seed = adapter.StockApplyRequest(**supplied, durable_drain_sha256="0"*64,
            durable_apply_intent_sha256="0"*64, attempt_identity_sha256="0"*64)
        adapter.validate_request(seed)
        # Freeze normalized default stock/elevation fields too; intent and final
        # request must have identical canonical context apart from generated pins.
        supplied = asdict(seed)
        for generated in ("durable_drain_sha256", "durable_apply_intent_sha256", "attempt_identity_sha256"):
            supplied.pop(generated)
        if seed.suite_root != str(self.root):
            raise LaunchHold("foreign_maintenance_deployment")
        self.records, self.scope_verifier, self.transaction_verifier = records, scope_verifier, transaction_verifier
        self.stock_adapter = adapter
        drain = self._commit_record("original_drain", json.loads(self.raw), seed.transaction_id)
        attempt = self._commit_record("attempt", {"owner": self.record["owner"],
            "deployment": self.record["deployment"], "transaction": seed.transaction_id,
            "nonce": uuid.uuid4().hex}, seed.transaction_id)
        intent = self._commit_record("apply_intent", {"fields": supplied,
            "drain_sha256": drain["sha256"], "attempt_sha256": attempt["sha256"]}, seed.transaction_id)
        request = adapter.StockApplyRequest(**supplied, durable_drain_sha256=drain["sha256"],
            durable_apply_intent_sha256=intent["sha256"], attempt_identity_sha256=attempt["sha256"])
        next_record = deepcopy(self.record)
        next_record["phase"] = "maintenance_hold"
        next_record["maintenance"] = {"request": json.loads(_bytes(asdict(request))),
            "drain": drain, "attempt": attempt, "intent": intent,
            "dispatch": "prepared", "roles": [], "replacement_dispatch": "not_started"}
        self._persist(next_record)
        return request

    def _maintenance_context(self, request, *, allow_pending=False, terminal_ticket=None):
        self._owned(allow_pending=allow_pending)
        m = self.record.get("maintenance")
        repair = m.get('repair') if m is not None else None
        if repair is not None and _bytes(repair['request']) == _bytes(asdict(request)):
            fields = dict(m['request']);fields['original_owner_tokens'] = tuple(fields['original_owner_tokens'])
            original = self.stock_adapter.StockApplyRequest(**fields)
            self._maintenance_context(original, allow_pending=allow_pending)
            if repair['dispatch'] == 'held':
                raise LaunchHold('original_repair_attempt_held')
            for key, pin_name in (('drain','durable_drain_sha256'),('attempt','attempt_identity_sha256'),
                                  ('intent','durable_apply_intent_sha256')):
                value = self._read_record(repair[key])
                if repair[key]['sha256'] != getattr(request,pin_name) or value['original_binding_sha256'] != original.binding_sha256:
                    raise LaunchHold('immutable_original_repair_attempt_required')
            intent = self._read_record(repair['intent'])
            fields = asdict(request)
            for key in ('durable_drain_sha256','durable_apply_intent_sha256','attempt_identity_sha256'):fields.pop(key)
            if (_bytes(intent['fields']) != _bytes(fields) or intent['drain_sha256'] != request.durable_drain_sha256
                    or intent['attempt_sha256'] != request.attempt_identity_sha256):
                raise LaunchHold('immutable_original_repair_intent_required')
            for role, closed in self._read_record(repair['drain'])['children'].items():
                child = self.children.get(role)
                if child is None or child.poll() is None:
                    raise LaunchHold('original_repair_closed_actor_changed')
            return repair
        if (m is None or m.get("dispatch") == "held" or
                _bytes(m["request"]) != _bytes(asdict(request))):
            raise LaunchHold("original_maintenance_context_required")
        if terminal_ticket is not None:
            if (terminal_ticket is not self._maintenance_terminal
                    or terminal_ticket['request'] is not request
                    or terminal_ticket['ref'] != m.get('terminal_verified')
                    or set(terminal_ticket['children']) != set(self.children)
                    or any(self.children[role] is not process for role,process in terminal_ticket['children'].items())):
                raise LaunchHold('same_original_maintenance_terminal_required')
            self._read_record(terminal_ticket['ref'])
        drain = self._read_record(m["drain"])
        attempt = self._read_record(m["attempt"])
        intent = self._read_record(m["intent"])
        fields = asdict(request)
        for generated in ("durable_drain_sha256", "durable_apply_intent_sha256", "attempt_identity_sha256"):
            fields.pop(generated)
        if (drain["drain"] is None or drain["local_report"] is None or
                drain["owner"] != self.record["owner"] or
                drain["deployment"] != self.record["deployment"] or
                self._original_tokens(drain) != request.original_owner_tokens or
                len(request.original_owner_tokens) > 64 or
                attempt["owner"] != drain["owner"] or attempt["transaction"] != request.transaction_id or
                m["drain"]["sha256"] != request.durable_drain_sha256 or
                m["intent"]["sha256"] != request.durable_apply_intent_sha256 or
                m["attempt"]["sha256"] != request.attempt_identity_sha256 or
                intent["drain_sha256"] != request.durable_drain_sha256 or
                intent["attempt_sha256"] != request.attempt_identity_sha256 or
                _bytes(intent["fields"]) != _bytes(fields)):
            raise LaunchHold("committed_maintenance_binding_mismatch")
        for role, identity in drain["children"].items():
            if (self.record["children"].get(role) != identity
                    or (terminal_ticket is None and self.children[role].poll() is None)):
                raise LaunchHold("original_native_owner_not_closed")
        return m

    def _stock_updater_role(self, request):
        attempt = self._maintenance_context(request, allow_pending=True)
        return 'repair_updater' if attempt is self.record['maintenance'].get('repair') else 'updater'

    @_serialized
    def prepare_repair_attempt(self, original, fields):
        """One distinct repair attempt; original approval/request never rebased."""
        self._maintenance_context(original)
        if ('repair' in self.record['maintenance'] or self.record['phase'] != 'replacement_wait_health'
                or 'replacement' not in self.children or self.children['replacement'].poll() is None
                or any(child.poll() is None for child in self.children.values())):
            raise LaunchHold('distinct_closed_failed_target_repair_required')
        exits = {}
        for role, child in self.children.items():
            binding, code = self._bound_native_exit(role)
            if binding.process is not child:
                raise LaunchHold('original_native_repair_drain_required')
            exits[role] = dict(identity=binding.identity(),exit_code=code)
        if exits['replacement']['exit_code'] == 0:
            raise LaunchHold('failed_target_native_exit_required')
        supplied = deepcopy(fields)
        supplied['original_owner_tokens'] = self._original_tokens(self.record)
        seed = self.stock_adapter.StockApplyRequest(**supplied,durable_drain_sha256='0'*64,
            durable_apply_intent_sha256='0'*64,attempt_identity_sha256='0'*64)
        self.stock_adapter.validate_request(seed)
        if (seed.transaction_id != original.transaction_id or seed.suite_root != original.suite_root
                or seed.manager_root != original.manager_root or seed.packages_dir != original.packages_dir
                or seed.update_exe != original.update_exe or seed.updater_sha256 != original.updater_sha256
                or seed.current_version != original.target_version or seed.target_version != original.current_version
                or seed.channel != original.channel):
            raise LaunchHold('exact_original_reverse_repair_context_required')
        supplied = asdict(seed)
        for key in ('durable_drain_sha256','durable_apply_intent_sha256','attempt_identity_sha256'):
            supplied.pop(key)
        common = dict(original_binding_sha256=original.binding_sha256,transaction_id=original.transaction_id)
        drain = self._commit_record('repair_closed',dict(common,children=exits),original.transaction_id)
        attempt = self._commit_record('repair_attempt',dict(common,nonce=uuid.uuid4().hex),original.transaction_id)
        intent = self._commit_record('repair_intent',dict(common,fields=supplied,
            drain_sha256=drain['sha256'],attempt_sha256=attempt['sha256']),original.transaction_id)
        request = self.stock_adapter.StockApplyRequest(**supplied,durable_drain_sha256=drain['sha256'],
            durable_apply_intent_sha256=intent['sha256'],attempt_identity_sha256=attempt['sha256'])
        next_record = deepcopy(self.record)
        next_record['maintenance']['repair'] = dict(request=json.loads(_bytes(asdict(request))),drain=drain,
            attempt=attempt,intent=intent,dispatch='prepared',roles=[],replacement_dispatch='not_started')
        self._persist(next_record)
        return request

    @_serialized
    def recovered_entry_healthy(self, original, repair_request, role, *, verify_health):
        self._maintenance_context(original)
        attempt = self._maintenance_context(repair_request)
        process = self.children.get(role)
        if (role not in {'restored','fresh_restored'} or process is None or process.poll() is not None
                or self.record['phase'] != 'replacement_wait_health'
                or role in attempt.get('health',{}) or not callable(verify_health)):
            raise LaunchHold('single_live_original_recovered_entry_required')
        self._scope('recovered_health',repair_request,process=process,role=role)
        proof = verify_health(self,repair_request,process)
        if (type(proof) is not dict or proof.get('status') != 'recovered_entry_healthy'
                or proof.get('role') != role or proof.get('binding_sha256') != repair_request.binding_sha256
                or proof.get('version') != original.current_version or proof.get('data_readback_verified') is not True):
            raise LaunchHold('signed_original_recovered_health_required')
        ref = self._commit_record('recovered_health',proof,original.transaction_id)
        next_record = deepcopy(self.record)
        next_record['maintenance']['repair'].setdefault('health',{})[role] = ref
        next_record['phase'] = 'replacement_normal';next_record['generation'] += 1
        next_record['helper_generation'] += 1;next_record['helper_grants_open'] = False
        next_record['scope_seal'] = next_record['local_report'] = next_record['drain'] = None
        self._persist(next_record)
        return deepcopy(proof)

    def _scope(self, stage, request, *, process=None, role=None):
        # Adapter code must implement original/duplicated native handle proof,
        # complete generation and updater lineage. There is no native default.
        value = self.scope_verifier(stage=stage, owner=self, request=request, process=process, role=role)
        if (type(value) is not dict or value.get("owner_token") != self.record["owner"] or
                value.get("attempt_identity_sha256") != request.attempt_identity_sha256 or
                value.get("stage") != stage or value.get("qualified") is not True or
                type(value.get("unknown_owners")) is not int or value["unknown_owners"] != 0):
            raise LaunchHold("unqualified_continuous_native_scope")
        return value

    def _transaction(self, adapter, request):
        self._maintenance_context(request)
        value = self.transaction_verifier(request)
        adapter.receipt(value, request, "original_transaction_current")
        if value.get("durable_apply_intent_sha256") != request.durable_apply_intent_sha256:
            raise LaunchHold("original_intent_authority_required")
        return value

    @_serialized
    def verify_maintenance_lease(self, lease, request):
        if self.native_transport is not None:
            from core.manager_stock_hook_provider import WindowsStockHookProvider
            provider = getattr(self.native_transport, 'stock_hook_provider', None)
            try:
                if type(provider) is not WindowsStockHookProvider:
                    raise LaunchHold('original_stock_provider_required')
                provider.verify_permission(self, request)
            except (LaunchHold, AttributeError, KeyError) as error:
                raise LaunchHold('stock_hook_transport_unqualified') from error
        if lease is not self:
            raise LaunchHold("same_live_owner_required")
        m = self._maintenance_context(request)
        if self.record["phase"] != "maintenance_hold" or m["dispatch"] != "prepared":
            raise LaunchHold("single_prepared_dispatch_required")
        self._scope("armed", request)
        names = ("durable_drain_sha256", "durable_apply_intent_sha256", "attempt_identity_sha256",
                 "suite_root", "manager_root", "package", "package_sha256", "updater_sha256",
                 "current_executable_sha256", "current_manifest_sha256")
        return {"status": "maintenance_held", "transaction_id": request.transaction_id,
            "binding_sha256": request.binding_sha256, "lease_token": self.record["owner"],
            "continuous_launch_exclusion": True, "durable_drain_committed": True,
            "updater_hook_scope_retained": True, **{name: getattr(request, name) for name in names}}

    def _closure(self, request, *, lease_token):
        self._maintenance_context(request)
        if lease_token != self.record["owner"]:
            raise LaunchHold("closure_owner_mismatch")
        self._scope("original_closed", request)
        return {"status": "original_owners_closed", "transaction_id": request.transaction_id,
            "binding_sha256": request.binding_sha256, "closed_owner_tokens": request.original_owner_tokens,
            "unknown_owners": 0}

    def _qualify_maintenance_child(self, role, process, request, stage):
        if not any(item["process"] is process for item in self.pending):
            self.pending.append({"role": role, "process": process})
        retained = [item for item in self.pending if item['process'] is process]
        if len(retained) != 1 or retained[0]['role'] != role:
            raise LaunchHold('original_maintenance_pending_role_changed')
        self._maintenance_context(request, allow_pending=True)
        if role in self.children or len(self.children) >= 128:
            raise LaunchHold("maintenance_role_registration")
        identity = {"pid": process.pid, "creation_filetime": _creation(process),
                    "attempt_identity_sha256": request.attempt_identity_sha256}
        self._scope(stage, request, process=process, role=role)
        next_record = deepcopy(self.record)
        next_record["children"][role] = identity
        next_record["generation"] += 1
        slot = next_record['maintenance']
        if self._stock_updater_role(request) == 'repair_updater':slot = slot['repair']
        slot['roles'].append(role)
        self._persist(next_record)
        self.children[role] = process
        self.pending[:] = [item for item in self.pending if item["process"] is not process]
        return hashlib.sha256(_bytes(identity)).hexdigest()

    def _maintenance_failed(self, reason):
        if self.persistence_fault is not None:
            return
        next_record = deepcopy(self.record)
        next_record["phase"] = "maintenance_failed"
        next_record["maintenance"]["dispatch"] = "held"
        next_record["maintenance"]["failure"] = str(reason)[:256]
        try:
            self._persist(next_record)
        except BaseException:
            pass  # _persist already latches; never mask the original exception.

    @_serialized
    def queue_maintenance(self, adapter, request, *, verify_admission, native_review, create_process):
        """Owner-controlled single attempt; stock leaf stays unchanged.

        create_process is a reviewed bare creation/handle-transfer primitive,
        never a coordinator or a receipt factory. Native default is absent.
        """
        self.verify_maintenance_lease(self, request)
        if not callable(create_process):
            raise LaunchHold("native_creation_disabled")
        def execute(argv, **kwargs):
            with self.dispatch_lock:
                m = self._maintenance_context(request)
                if m["dispatch"] != "prepared":
                    raise LaunchHold("attempt_already_reserved_no_replay")
                adapter.validate_argv(request, argv)
                expected = {"cwd": request.manager_root, "shell": False, "allow_elevation": False,
                    "transaction_id": request.transaction_id, "binding_sha256": request.binding_sha256,
                    "lease_token": self.record["owner"], "attempt_identity_sha256": request.attempt_identity_sha256}
                if kwargs != expected:
                    raise LaunchHold("executor_context_mismatch")
                self._transaction(adapter, request)
                self._scope("precreate", request)
                next_record = deepcopy(self.record)
                next_record["maintenance"]["dispatch"] = "reserved"
                self._persist(next_record)
                # Fresh authority AFTER durable dispatch reservation, immediately
                # before creation. Any failure preserves this one attempt.
                if self._maintenance_context(request)["dispatch"] != "reserved":
                    raise LaunchHold("dispatch_reservation_lost")
                self._transaction(adapter, request)
                self._scope("precreate", request)
                process = create_process(argv, **kwargs)
                if not any(item['process'] is process for item in self.pending):
                    self.pending.append({"role": "updater", "process": process})
                identity = self._qualify_maintenance_child("updater", process, request, "register_updater")
                next_record = deepcopy(self.record)
                next_record["phase"] = "applying"
                next_record["maintenance"]["dispatch"] = "created"
                self._persist(next_record)
                return {"status": "process_created", "transaction_id": request.transaction_id,
                    "binding_sha256": request.binding_sha256, "process_id": process.pid,
                    "process_identity_sha256": identity}
        try:
            result = adapter.queue_stock_apply(request, lease=self, verify_lease=self.verify_maintenance_lease,
                verify_admission=verify_admission, verify_closure=self._closure,
                verify_transaction=lambda req: self._transaction(adapter, req),
                executor=execute, native_review=native_review)
            if (result.get("process_id") != self.children["updater"].pid or
                    result.get("process_identity_sha256") != hashlib.sha256(_bytes(self.record["children"]["updater"])).hexdigest()):
                raise LaunchHold("uncorrelated_queue_receipt")
            return result
        except BaseException as exc:
            self._maintenance_failed(type(exc).__name__)
            raise

    @_serialized
    def register_stock_hook(self, role, process, request):
        """Original/qualified duplicated handle from the admitted stock lineage.

        Retain before checks. Names/CLI/parent PID alone are not lineage proof.
        """
        try:
            pending = next((item for item in self.pending if item['process'] is process), None)
            if pending is None:
                self.pending.append({"role": role, "process": process})
            elif pending['role'] != role:
                raise LaunchHold('original_stock_hook_pending_role_changed')
            self._maintenance_context(request, allow_pending=True)
            updater_role = self._stock_updater_role(request)
            phase = 'repair_applying' if updater_role == 'repair_updater' else 'applying'
            if (self.record["phase"] != phase or not role.startswith("hook:") or
                    len(role) > 64 or self.children[updater_role].poll() is not None):
                raise LaunchHold("unexpected_stock_hook")
            return self._qualify_maintenance_child(role, process, request, "register_hook")
        except BaseException as exc:
            self._maintenance_failed(type(exc).__name__)
            raise

    @_serialized
    def finish_apply(self, request, *, verify_payload):
        m = self._maintenance_context(request)
        if self.record["phase"] != "applying" or m["dispatch"] != "created":
            raise LaunchHold("actual_apply_attempt_required")
        if any(self.children[role].poll() is None for role in m["roles"]):
            raise LaunchHold("updater_or_hook_still_running")
        try:
            self._scope("maintenance_closed", request)
            if any(self.children[role].poll() != 0 for role in m["roles"]):
                raise LaunchHold("stock_apply_or_hook_failed")
            proof = verify_payload(self, request)
            if (type(proof) is not dict or proof.get("status") != "code_data_verified" or
                    proof.get("transaction_id") != request.transaction_id or
                    proof.get("binding_sha256") != request.binding_sha256 or
                    proof.get("target_version") != request.target_version or proof.get("data_unchanged") is not True):
                raise LaunchHold("code_data_verification_required")
            for key in ("current_executable_sha256", "current_manifest_sha256"):
                if type(proof.get(key)) is not str or not re.fullmatch(r"[0-9a-f]{64}", proof[key]):
                    raise LaunchHold("verified_replacement_pin_required")
            ref = self._commit_record("apply_verified", proof, request.transaction_id)
            next_record = deepcopy(self.record)
            next_record["maintenance"]["apply_verified"] = ref
            next_record["phase"] = "replacement_hold"
            self._persist(next_record)
            return deepcopy(proof)
        except BaseException as exc:
            self._maintenance_failed(type(exc).__name__)
            raise

    @_serialized
    def spawn_replacement(self, request, argv, *, admit_entry, create_process):
        from suite_core.manager.entry_binding import FrozenEntryBinding
        with self.dispatch_lock:
            m = self._maintenance_context(request)
            if self.record["phase"] != "replacement_hold" or m["replacement_dispatch"] != "not_started":
                raise LaunchHold("single_replacement_attempt_required")
            admitted = admit_entry(argv, owner=self, request=request)
            if (type(admitted) is not FrozenEntryBinding or tuple(argv[1:]) != admitted.restart_arguments or
                    admitted.layout.code_root != self.root or admitted.layout.data_root != self.data_root or
                    (self.entry_binding is not None and str(admitted.settings_path) != self.entry_binding["settings_path"]) or
                    argv[0] != str(self.root / "Apps/Manager/current/CollectiGenManager.exe")):
                raise LaunchHold("complete_replacement_entry_binding_required")
            proof = self._read_record(m["apply_verified"])
            # Reuse the accepted leaf's physical-path/hash readback; native creation
            # identity/channel must still be qualified by the entry bridge.
            self.stock_adapter.readback(Path(argv[0]), expected=proof["current_executable_sha256"])
            self.stock_adapter.readback(Path(argv[0]).parent / "sq.version", expected=proof["current_manifest_sha256"])
            next_record = deepcopy(self.record)
            next_record["maintenance"]["replacement_dispatch"] = "reserved"
            self._persist(next_record)
            try:
                self._scope("precreate_replacement", request)
                process = create_process(argv, cwd=str(self.root), shell=False, allow_elevation=False)
                if not any(item['process'] is process for item in self.pending):
                    self.pending.append({"role": "replacement", "process": process})
                self._qualify_maintenance_child("replacement", process, request, "register_replacement")
                next_record = deepcopy(self.record)
                next_record["phase"] = "replacement_wait_health"
                next_record["maintenance"]["replacement_dispatch"] = "created"
                self._persist(next_record)
                return process
            except BaseException as exc:
                self._maintenance_failed(type(exc).__name__)
                raise

    @_serialized
    def replacement_healthy(self, request, *, verify_health):
        m = self._maintenance_context(request)
        process = self.children.get("replacement")
        if self.record["phase"] != "replacement_wait_health" or process is None or process.poll() is not None:
            raise LaunchHold("live_registered_replacement_required")
        try:
            self._scope("replacement_health", request, process=process, role="replacement")
            proof = self._read_record(m["apply_verified"])
            current = self.root / "Apps/Manager/current"
            self.stock_adapter.readback(current / "CollectiGenManager.exe", expected=proof["current_executable_sha256"])
            self.stock_adapter.readback(current / "sq.version", expected=proof["current_manifest_sha256"])
            health = verify_health(self, request, process, deepcopy(proof))
            if (type(health) is not dict or health.get("status") != "replacement_healthy" or
                    health.get("target_version") != request.target_version or
                    health.get("binding_sha256") != request.binding_sha256 or
                    health.get("data_unchanged") is not True or
                    any(health.get(key) != proof[key] for key in ("current_executable_sha256", "current_manifest_sha256")) or
                    process.poll() is not None):
                raise LaunchHold("replacement_health_required")
            ref = self._commit_record("replacement_health", health, request.transaction_id)
            next_record = deepcopy(self.record)
            next_record["phase"] = "replacement_normal"
            next_record["helper_generation"] += 1
            next_record["generation"] += 1
            next_record["helper_grants_open"] = True
            next_record["scope_seal"] = None
            next_record["local_report"] = None
            next_record["drain"] = None
            next_record["maintenance"]["health"] = ref
            self._persist(next_record)
            return deepcopy(health)
        except BaseException as exc:
            self._maintenance_failed(type(exc).__name__)
            raise

    @_serialized
    def retire_maintenance(self, request, *, terminal, verify_terminal, settle_resources, retire_transaction):
        """Same-owner terminal release only; no ordinary-close maintenance bypass.

        Native hooks remain held until the separately reviewed actual transport
        qualifies observation and settlement. Adapter return alone is not health.
        """
        if self._maintenance_receipt is not None:
            receipt=self._maintenance_receipt
            if (receipt.transaction_id != request.transaction_id or
                    receipt.attempt_identity_sha256 != request.attempt_identity_sha256):
                raise LaunchHold('original_maintenance_terminal_required')
            return receipt
        ticket=self._maintenance_terminal
        maintenance=self._maintenance_context(request,terminal_ticket=ticket)
        if self.native_transport is not None:
            from core.manager_stock_hook_provider import WindowsStockHookProvider
            provider = getattr(self.native_transport, 'stock_hook_provider', None)
            if type(provider) is not WindowsStockHookProvider:
                raise LaunchHold('stock_hook_transport_unqualified')
            provider.verify_retirement(self, request)
            if verify_terminal != provider.verify_terminal or settle_resources != provider.settle_terminal_resources:
                raise LaunchHold('original_native_terminal_verifiers_required')
        if (type(terminal) is not dict or terminal.get('transaction') != request.transaction_id
                or terminal.get('phase') not in {'healthy','recovery_verified'}
                or not callable(verify_terminal) or not callable(settle_resources)
                or not callable(retire_transaction)):
            raise LaunchHold('verified_maintenance_terminal_required')
        if ticket is None and (maintenance['dispatch'] != 'created' or 'apply_verified' not in maintenance
                or self.record['phase'] not in {'replacement_normal','replacement_wait_health','cleanup_hold'}
                or (terminal['phase']=='healthy' and ('health' not in maintenance
                    or self.record['phase'] not in {'replacement_normal','cleanup_hold'}))):
            raise LaunchHold('observed_original_apply_terminal_required')
        self._read_record(maintenance['apply_verified'])
        if terminal['phase']=='healthy':self._read_record(maintenance['health'])
        if ticket is None:
            if any(process.poll() is None for process in self.children.values()):
                raise LaunchHold('maintenance_actors_still_running')
            self._scope('terminal_closed',request)
            proof=verify_terminal(self,request,deepcopy(terminal))
            if (type(proof) is not dict or proof.get('transaction_id') != request.transaction_id
                or proof.get('binding_sha256') != request.binding_sha256
                or proof.get('phase') != terminal['phase'] or proof.get('data_readback_verified') is not True
                or proof.get('code_readback_verified') is not True
                or proof.get('worker_output_closed') is not True
                or proof.get('closed_roles') != tuple(sorted(self.children))):
                raise LaunchHold('qualified_terminal_code_data_actor_output_required')
            ref=self._commit_record('terminal_verified',proof,request.transaction_id)
            ticket=dict(request=request,phase=terminal['phase'],children=dict(self.children),ref=ref,
                settle=settle_resources,retire=retire_transaction,resources_closed=False,transaction_retired=False)
            self._maintenance_terminal=ticket
            next_record=deepcopy(self.record);next_record['maintenance']['terminal_verified']=ref
            next_record['phase']='maintenance_terminal_hold';self._persist(next_record)
        elif (ticket['request'] is not request or ticket['phase']!=terminal['phase']
                or ticket['settle']!=settle_resources or ticket['retire']!=retire_transaction
                or set(ticket['children'])!=set(self.children)
                or any(self.children[role] is not process for role,process in ticket['children'].items())
                or self.record['maintenance'].get('terminal_verified')!=ticket['ref']):
            raise LaunchHold('same_original_maintenance_terminal_required')
        proof=self._read_record(ticket['ref'])
        # Reuse positive closure evidence; never poll a handle already retired
        # by a partially successful original resource settler.
        if not ticket['resources_closed']:
            if settle_resources(self,request,deepcopy(proof)) is not True:
                raise LaunchHold('original_maintenance_resources_unsettled')
            ticket['resources_closed']=True
        self._owned()
        self._scope('terminal_resources_closed',request)
        if not ticket['transaction_retired']:
            retire_transaction() # Existing recovery retirement while original lease is still held.
            ticket['transaction_retired']=True
        self._owned()
        next_record=deepcopy(self.record);next_record['phase']='maintenance_closed'
        next_record['maintenance']['terminal_resources_closed']=True;self._persist(next_record)
        digest=self._close_receipt_and_lease()
        self._maintenance_receipt=MaintenanceClosedReceipt(self.record['owner'],request.transaction_id,
            request.attempt_identity_sha256,digest)
        return self._maintenance_receipt


def _normal_native_api():
    """Reuse existing native spawn structs/API; add retained-handle queries only."""
    from core.training_process_windows import _api
    from ctypes import wintypes as W
    api = _api()
    signatures = {
        "GetCurrentProcess": ([], W.HANDLE),
        "GetProcessId": ([W.HANDLE], W.DWORD),
        "DuplicateHandle": ([W.HANDLE,W.HANDLE,W.HANDLE,ctypes.POINTER(W.HANDLE),W.DWORD,W.BOOL,W.DWORD],W.BOOL),
        "QueryFullProcessImageNameW": ([W.HANDLE,W.DWORD,W.LPWSTR,ctypes.POINTER(W.DWORD)],W.BOOL),
        "PeekNamedPipe": ([W.HANDLE,W.LPVOID,W.DWORD,W.LPVOID,ctypes.POINTER(W.DWORD),W.LPVOID],W.BOOL),
        "SetNamedPipeHandleState": ([W.HANDLE,ctypes.POINTER(W.DWORD),W.LPVOID,W.LPVOID],W.BOOL),
        "WriteFile": ([W.HANDLE,W.LPVOID,W.DWORD,ctypes.POINTER(W.DWORD),W.LPVOID],W.BOOL),
        "TerminateProcess": ([W.HANDLE,W.UINT],W.BOOL),
        "GetProcessTimes": ([W.HANDLE]+[ctypes.POINTER(W.FILETIME)]*4,W.BOOL),
        "GetNamedPipeClientProcessId": ([W.HANDLE,ctypes.POINTER(W.ULONG)],W.BOOL),
        "GetNamedPipeServerProcessId": ([W.HANDLE,ctypes.POINTER(W.ULONG)],W.BOOL),
        "OpenProcess": ([W.DWORD,W.BOOL,W.DWORD],W.HANDLE),
    }
    for name,(args,result) in signatures.items():
        getattr(api,name).argtypes, getattr(api,name).restype = args,result
    return api


def _native_witness(api, handle):
    """Actual birth/image observation on original/inherited/duplicated handle."""
    from ctypes import wintypes as W
    from types import SimpleNamespace
    pid = api.GetProcessId(handle)
    if not pid:raise LaunchHold('native_get_process_id_failed') from ctypes.WinError(ctypes.get_last_error())
    size = W.DWORD(32768)
    image = ctypes.create_unicode_buffer(size.value)
    if not api.QueryFullProcessImageNameW(handle,0,image,ctypes.byref(size)):
        raise LaunchHold('native_image_query_failed') from ctypes.WinError(ctypes.get_last_error())
    path = Path(image.value)
    _assert_unredirected(path)
    before = path.stat()
    if before.st_size > 256*1024*1024:
        raise LaunchHold("owned_native_image_bound")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b""): digest.update(chunk)
    after = path.stat()
    _assert_unredirected(path)
    if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns) != (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):
        raise LaunchHold("owned_native_image_changed")
    return {"pid":pid,"creation_filetime":_creation(SimpleNamespace(_handle=handle)),
            "executable_sha256":digest.hexdigest()}


def _native_process_argv(process):
    """Read a live retained process command line; never reconstruct from a PID.

    ProcessCommandLineInformation is independently observed before accepting a
    hook's reported stock argv. Unsupported native queries remain a hold.
    """
    from ctypes import wintypes as W
    if type(process) is not RetainedNativeProcess or not process._handle:
        raise LaunchHold('retained_native_command_line_required')
    class UnicodeString(ctypes.Structure):
        _fields_ = [('length', W.USHORT), ('maximum', W.USHORT), ('buffer', ctypes.c_void_p)]
    query = ctypes.WinDLL('ntdll', use_last_error=True).NtQueryInformationProcess
    query.argtypes = [W.HANDLE, W.ULONG, W.LPVOID, W.ULONG, ctypes.POINTER(W.ULONG)]
    query.restype = ctypes.c_long
    required = W.ULONG()
    status = query(process._handle, 60, None, 0, ctypes.byref(required)) & 0xffffffff
    if status not in (0xc0000004, 0xc0000023) or not ctypes.sizeof(UnicodeString) < required.value <= 65536 + 64:
        raise LaunchHold('native_command_line_query_unavailable')
    raw = ctypes.create_string_buffer(required.value)
    if query(process._handle, 60, raw, len(raw), ctypes.byref(required)) != 0 or required.value > len(raw):
        raise LaunchHold('native_command_line_query_failed')
    value = UnicodeString.from_buffer(raw)
    start = ctypes.addressof(raw)
    if (not value.buffer or not 0 < value.length <= 65536 or value.length % 2
            or value.maximum < value.length or value.buffer < start + ctypes.sizeof(UnicodeString)
            or value.buffer + value.length > start + len(raw)):
        raise LaunchHold('native_command_line_buffer_invalid')
    command = ctypes.wstring_at(value.buffer, value.length // 2)
    if '\0' in command: raise LaunchHold('native_command_line_invalid')
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [W.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(W.LPWSTR)
    process.api.LocalFree.argtypes, process.api.LocalFree.restype = [W.HLOCAL], W.HLOCAL
    count = ctypes.c_int()
    parsed = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not parsed: raise LaunchHold('native_command_line_parse_failed')
    try:
        if not 1 <= count.value <= 256: raise LaunchHold('native_command_line_arg_bound')
        return tuple(parsed[index] for index in range(count.value))
    finally:
        if process.api.LocalFree(ctypes.cast(parsed, W.HLOCAL)):
            raise LaunchHold('native_command_line_parse_resource_held')


class RetainedNativeProcess:
    """Original CreateProcess or qualified DuplicateHandle; never OpenProcess."""
    def __init__(self, api, handle, *, pid=None, thread=None):
        # No fallible queries before the caller puts this object in pending.
        self.api,self._handle,self._pid,self.thread = api,handle,pid,thread

    @property
    def pid(self):
        if self._pid is None:
            self._pid = self.api.GetProcessId(self._handle)
            if not self._pid: raise ctypes.WinError(ctypes.get_last_error())
        return self._pid

    def resume(self):
        if not self.thread or self.api.ResumeThread(self.thread) == 0xffffffff:
            raise LaunchHold("owned_native_resume_failed")
        if not self.api.CloseHandle(self.thread):raise LaunchHold('owned_native_thread_close_failed')
        self.thread = None

    def poll(self):
        from ctypes import wintypes as W
        state = self.api.WaitForSingleObject(self._handle,0)
        if state == 258: return None
        code = W.DWORD()
        if state != 0 or not self.api.GetExitCodeProcess(self._handle,ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value

    def wait(self, timeout=5):
        if not 0 <= timeout <= 30: raise LaunchHold("bounded_native_wait_required")
        if self.api.WaitForSingleObject(self._handle,int(timeout*1000)) != 0:
            raise LaunchHold("owned_native_exit_unresolved")
        return self.poll()

    def close_after_exit(self):
        if self.poll() is None: raise LaunchHold("live_owned_handle_retained")
        if self.thread:
            if not self.api.CloseHandle(self.thread):raise LaunchHold('owned_native_thread_close_failed')
            self.thread=None
        if not self.api.CloseHandle(self._handle):raise LaunchHold('owned_native_process_close_failed')
        self._handle = None


@dataclass(frozen=True)
class NativeChannelProof:
    binding_sha256: str
    channel_epoch: str
    local_creation_filetime: int
    peer_creation_filetime: int
    # Actual queried handles; not a decoded client DTO or token comparison.
    source: str = "original_inherited_native_handles"


class WindowsControlChannel:
    """Two explicit anonymous pipes; entry owns schema/sequence/RPC admission."""
    def __init__(self, api, reader, writer, peer_handle, *, transport=None, owns_peer=False, epoch=None):
        self.api,self.reader,self.writer,self.peer_handle = api,reader,writer,peer_handle
        self.transport,self.owns_peer = transport,owns_peer
        self.epoch = epoch
        self.io_lock = transport.owner.dispatch_lock if transport is not None else RLock()
        self.dispatch_lock = self.io_lock
        self.write_fault = None
        self.read_fault = None
        self._read_progress = 0

    @staticmethod
    def _bounded(raw):
        if (type(raw) is not bytes or not 5 <= len(raw) <= 65540 or
                int.from_bytes(raw[:4],"little") != len(raw)-4):
            raise LaunchHold("bounded_control_frame_required")

    @_serialized
    def write_frame(self, raw, timeout=5):
        """Synchronous nonblocking native byte-pipe writes; deadline never releases ownership."""
        if self.transport is not None and (self.transport.owner._abort_ticket is not None or self.transport.owner._normal_terminal is not None):raise LaunchHold('normal_dispatch_aborted')
        import msvcrt
        from ctypes import wintypes as W
        self._bounded(raw)
        if not 0 < timeout <= 30: raise LaunchHold("bounded_channel_wait_required")
        with self.io_lock:
            if self.write_fault is not None: raise LaunchHold("control_write_recovery_required")
            handle=msvcrt.get_osfhandle(self.writer)
            mode=W.DWORD(1)  # PIPE_NOWAIT: no outstanding blocking or overlapped I/O.
            if not self.api.SetNamedPipeHandleState(handle,ctypes.byref(mode),None,None):
                self.write_fault="mode_unavailable"
                raise LaunchHold("control_nonblocking_write_unavailable")
            deadline=time.monotonic()+timeout
            offset=0
            try:
                while offset<len(raw):
                    if time.monotonic()>=deadline: raise LaunchHold("control_write_timeout")
                    data=ctypes.create_string_buffer(raw[offset:])
                    written=W.DWORD()
                    ok=self.api.WriteFile(handle,data,len(raw)-offset,ctypes.byref(written),None)
                    if not ok:
                        error=ctypes.get_last_error()
                        if error!=232: raise LaunchHold("control_write_eof_or_unavailable")
                    if written.value>len(raw)-offset: raise LaunchHold("control_write_count_invalid")
                    offset+=written.value
                    if not written.value: time.sleep(.01)
            except BaseException:
                # Partial frame is uncertain. No subsequent frame/replay; keep
                # endpoint, native process/Job and lease until explicit recovery.
                self.write_fault={"bytes_written":offset,"frame_size":len(raw)}
                raise

    @_serialized
    def read_frame(self, timeout=5):
        if self.transport is not None and (self.transport.owner._abort_ticket is not None or self.transport.owner._normal_terminal is not None):raise LaunchHold('normal_dispatch_aborted')
        if not 0 < timeout <= 30: raise LaunchHold("bounded_channel_wait_required")
        if self.read_fault is not None: raise LaunchHold("control_read_recovery_required")
        self._read_progress=0
        try:
            return self._read_frame(timeout)
        except BaseException as exc:
            if isinstance(exc,LaunchHold) and str(exc)=="control_channel_timeout" and self._read_progress==0:
                raise LaunchHold("control_channel_idle_timeout") from exc
            self.read_fault={"bytes_received":self._read_progress,"reason":type(exc).__name__}
            raise

    def _read_frame(self, timeout=5):
        import msvcrt
        from ctypes import wintypes as W
        if not 0 < timeout <= 30: raise LaunchHold("bounded_channel_wait_required")
        deadline = time.monotonic()+timeout
        def exact(count):
            data = bytearray()
            while len(data) < count:
                if time.monotonic() >= deadline:
                    raise LaunchHold("control_channel_timeout")
                available = W.DWORD()
                if not self.api.PeekNamedPipe(msvcrt.get_osfhandle(self.reader),None,0,None,ctypes.byref(available),None):
                    raise LaunchHold("control_channel_eof_or_unavailable")
                if available.value:
                    chunk = os.read(self.reader,min(count-len(data),available.value))
                    if not chunk: raise LaunchHold("control_channel_eof")
                    data.extend(chunk)
                    self._read_progress+=len(chunk)
                elif time.monotonic() >= deadline:
                    raise LaunchHold("control_channel_timeout")
                else: time.sleep(.01)
            return bytes(data)
        prefix = exact(4)
        size = int.from_bytes(prefix,"little")
        if not 1 <= size <= 65536: raise LaunchHold("control_frame_bound")
        raw = prefix+exact(size)
        self._bounded(raw)
        return raw

    def local_witness(self): return _native_witness(self.api,self.api.GetCurrentProcess())
    def peer_witness(self): return _native_witness(self.api,self.peer_handle)

    @_serialized
    def qualify_binding(self, document, admitted, *, expected_peer_image_sha256):
        """Compare V1 offer to native handles AND independently admitted binding.

        Expected peer pin comes from reviewed bootstrap/package authority, not
        this document. Entry retains responsibility for wire sequence and SDK
        dispatch. Tests use independently pinned inert interpreter programs.
        """
        local,peer = self.local_witness(),self.peer_witness()
        entry,launcher = (peer,local) if self.transport is not None else (local,peer)
        if (type(document) is not dict or document.get("protocol") != 1 or
                document.get("channel_epoch") != self.epoch or document.get("entry") != entry or
                document.get("launcher") != launcher or peer["executable_sha256"] != expected_peer_image_sha256 or
                document.get("deployment_root") != str(admitted.layout.code_root) or
                document.get("data_root") != str(admitted.layout.data_root) or document.get("mode") != "portable" or
                admitted.layout.mode.value != "portable" or
                document.get("settings_path") != str(admitted.settings_path) or
                document.get("argv_sha256") != hashlib.sha256(json.dumps(admitted.restart_arguments,
                    ensure_ascii=False,separators=(",",":")).encode()).hexdigest()):
            raise LaunchHold("native_entry_offer_mismatch")
        if self.transport is not None:
            owner=self.transport.owner
            owner._owned()
            if document.get("owner_generation") != owner.record["owner"]:
                raise LaunchHold("committed_owner_generation_mismatch")
        return NativeChannelProof(hashlib.sha256(_bytes(document)).hexdigest(),self.epoch,
                                  local["creation_filetime"],peer["creation_filetime"])

    @_serialized
    def duplicate_from_peer(self, source_handle):
        """Root only: transfer from its original retained child handle.

        Returned duplicate MUST enter owner.pending before witness/pid queries.
        """
        from ctypes import wintypes as W
        if self.transport is not None and self.transport.owner._model_free_scope is not None:raise LaunchHold('model_free_helpers_forbidden')
        if self.transport is None or type(source_handle) is not int or source_handle <= 0:
            raise LaunchHold("original_peer_handle_required")
        target = W.HANDLE()
        if not self.api.DuplicateHandle(self.peer_handle,source_handle,self.api.GetCurrentProcess(),
                ctypes.byref(target),0x1000|0x100000,False,0):
            raise ctypes.WinError(ctypes.get_last_error())
        return RetainedNativeProcess(self.api,target.value)

    def duplicate_stock_hook_from_updater(self, request, source_handle):
        """Root receives a hook self-query copy through the original updater.

        No root process duplication right is exported. The hook deposits its
        own noninheritable query copy in the updater, whose original process
        handle is already held by this root. The actual pipe client/native
        observation must qualify the returned pending copy before SDK dispatch.
        """
        from ctypes import wintypes as W
        if (self.transport is None or type(source_handle) is not int or not 0 < source_handle < 2**63):
            raise LaunchHold('original_stock_hook_source_required')
        owner = self.transport.owner
        with owner.dispatch_lock:
            owner._maintenance_context(request, allow_pending=True)
            updater = owner.children.get(owner._stock_updater_role(request))
            if (type(updater) is not RetainedNativeProcess or updater.api is not self.api
                    or updater.poll() is not None or not self.transport.includes(updater)):
                raise LaunchHold('live_original_stock_updater_required')
            copy = RetainedNativeProcess(self.api, None)
            serial = len([item for item in owner.children if item.startswith('hook:')])
            role = 'hook:stock:' + str(serial + 1)
            if serial >= 4 or any(item['role'].startswith('hook:') for item in owner.pending):
                raise LaunchHold('bounded_single_stock_hook_transfer_required')
            owner.pending.append(dict(role=role, process=copy))
            output = W.HANDLE()
            ok = self.api.DuplicateHandle(updater._handle, source_handle,
                self.api.GetCurrentProcess(), ctypes.byref(output), 0x1000 | 0x100000, False, 0)
            copy._handle = output.value
            if not ok or not copy._handle:
                raise LaunchHold('stock_hook_transfer_uncertain')
            self.peer_handle = copy._handle
            return role, copy


    @classmethod
    def from_inherited_environment(cls):
        import msvcrt
        api = _normal_native_api()
        values = [int(os.environ.pop(key)) for key in
                  ("COLLECTIGEN_CONTROL_READ","COLLECTIGEN_CONTROL_WRITE","COLLECTIGEN_LAUNCHER_QUERY")]
        # Child endpoints/query witness are not inherited by helper grandchildren.
        for handle in values:
            if not api.SetHandleInformation(handle,1,0): raise ctypes.WinError(ctypes.get_last_error())
        reader = msvcrt.open_osfhandle(values[0],os.O_RDONLY|os.O_BINARY|os.O_NOINHERIT)
        writer = msvcrt.open_osfhandle(values[1],os.O_WRONLY|os.O_BINARY|os.O_NOINHERIT)
        return cls(api,reader,writer,values[2],owns_peer=True,epoch=os.environ.pop("COLLECTIGEN_CONTROL_EPOCH"))

    @_serialized
    def close(self):
        for name in ("reader","writer"):
            fd = getattr(self,name)
            if fd is not None: os.close(fd); setattr(self,name,None)
        if self.owns_peer and self.peer_handle:
            if not self.api.CloseHandle(self.peer_handle):raise LaunchHold('control_peer_close_failed')
            self.peer_handle=None


class _ManagedHelperAccounting:
    """Existing-owner component bridge. Native cross-process provider remains held."""
    production_authorized=False

    def __init__(self,owner,session,native,reviewed,pin,entry,runtime,verify_session):
        required={'installation_id','lifecycle_identity','program','publication_manifest_sha256','prepared_inventory_sha256'}
        if (type(session) is not entry.NormalEntrySession or type(native) is not runtime.WindowsOwnedProcess or
                type(reviewed) is not dict or set(reviewed)!=required or
                any(type(value) is not str or not 0<len(value)<=4096 for value in reviewed.values()) or
                type(pin) is not str or not re.fullmatch('[0-9a-f]{64}',pin) or not callable(verify_session)):
            raise LaunchHold('typed_reviewed_helper_provider_required')
        self.owner,self.session,self.native=owner,session,native
        self.reviewed,self.pin=deepcopy(reviewed),pin
        self.entry,self.runtime,self.verify_session=entry,runtime,verify_session
        self.transport=owner.native_transport;self.api=self.transport.api;self.job=self.transport.job
        self.manager=owner.children.get('manager');self.proof=session.wire.proof
        self.generation=(session.binding.owner_generation,session.binding.channel_epoch,session.wire.proof.binding_sha256)
        self.helper_generation=owner.record['helper_generation']
        self.issued={}
        self.transfer_snapshots={}
        self._session(session)

    def _session(self,session):
        owner=self.owner
        owner._owned(allow_pending=True,allow_reservations=True)
        manager=owner.children.get('manager')
        if (session is not self.session or session.wire.proof is not self.proof or
                owner.native_transport is not self.transport or self.transport.api is not self.api or self.transport.job!=self.job or
                manager is not self.manager or self.generation != (session.binding.owner_generation,session.binding.channel_epoch,session.wire.proof.binding_sha256) or
                self.generation[0]!=owner.record['owner'] or owner.record['helper_generation']!=self.helper_generation or
                owner._model_free_scope is not None or manager is None or manager.poll() is not None or
                owner.record['phase'] not in ('normal','cleanup_hold') or self.verify_session(session) is not True):
            raise LaunchHold('authenticated_original_helper_session_required')
        identity=_native_witness(self.api,manager._handle)
        recorded=owner.record['children']['manager']
        if identity!={'pid':recorded['pid'],'creation_filetime':recorded['creation_filetime'],'executable_sha256':owner.entry_binding['manager_sha256']}:
            raise LaunchHold('original_native_manager_binding_changed')

    def _original(self,session,slot):
        from types import MappingProxyType
        self._session(session)
        attempt=slot.get('attempt');cap=self.native.helper_capability
        if (type(attempt) is not self.runtime.HelperAttempt or type(attempt.context) is not MappingProxyType or
                not re.fullmatch('[0-9a-f]{32}',attempt.identity) or self.native.helper_attempt is not attempt or
                self.native._helper_attempt_capability is not cap or cap.session is not session or cap.native is not self.native or
                cap.generation!=self.generation or cap.program_pin!=self.pin or cap.attempts.get(attempt.identity) is not slot or
                slot.get('generation')!=self.generation or
                dict(attempt.context)!=dict(self.reviewed,helper_attempt_id=attempt.identity) or
                slot.get('context_sha256')!=hashlib.sha256(_bytes(dict(attempt.context))).hexdigest()):
            raise LaunchHold('original_typed_helper_attempt_required')
        return attempt

    def _correlated(self,session,slot):
        attempt=self._original(session,slot)
        ref=self.owner._helper_attempt_refs.get(attempt.identity)
        data=self.owner.record.get('helper_attempts',{}).get(attempt.identity)
        if (ref is None or ref['provider'] is not self or ref['slot'] is not slot or ref['attempt'] is not attempt or
                data is None or data['context_sha256']!=slot['context_sha256'] or data['generation']!=list(self.generation) or
                data['helper_generation']!=self.helper_generation or data['role']!=slot['role'] or data['program_sha256']!=self.pin or
                (slot.get('reservation') is not None and slot['reservation']!=data['reservation'])):
            raise LaunchHold('exact_retained_helper_correlation_required')
        return ref,data

    def _prebirth(self,session,slot):
        self._original(session,slot)
        if self.native._helper_create_entered or self.native.job is not None or self.native.logs or slot.get('copy') is not None:
            raise LaunchHold('positive_original_helper_non_creation_required')

    def reserve_attempt(self,session,slot,pin):
        with self.owner.dispatch_lock:
            attempt=self._original(session,slot);self._prebirth(session,slot)
            if (pin!=self.pin or attempt.identity in self.owner.record.get('helper_attempts',{}) or
                    attempt.identity in self.owner._helper_attempt_refs or len(self.owner._helper_attempt_refs)>=127 or
                    not re.fullmatch('owned-ollama-(?:[1-9]|[1-9][0-9]|1[01][0-9]|12[0-7])',slot['role'])):
                raise LaunchHold('fresh_bounded_original_helper_attempt_required')
            self.owner._helper_attempt_refs[attempt.identity]={'provider':self,'slot':slot,'attempt':attempt,'proof':None}
            next_record=deepcopy(self.owner.record)
            next_record.setdefault('helper_attempts',{})[attempt.identity]={
                'attempt_id':attempt.identity,'context_sha256':slot['context_sha256'],'generation':list(self.generation),
                'helper_generation':self.helper_generation,'role':slot['role'],'program_sha256':pin,'reservation':None,'phase':'observed'}
            self.owner._persist(next_record) # intake before grant I/O, no absence inference
            try:return session.reserve_helper(slot['role'],pin)
            except BaseException:
                # A committed grant after ACK loss remains a grant, never non-grant.
                if self.owner.persistence_fault is None:
                    self.owner._owned(allow_pending=True,allow_reservations=True)
                    data=self.owner.record['helper_attempts'][attempt.identity]
                    if data['phase']=='observed':
                        self._prebirth(session,slot)
                        next_record=deepcopy(self.owner.record);next_record['helper_attempts'][attempt.identity]['phase']='non_grant'
                        self.owner._persist(next_record)
                raise

    def _grant_gate(self,data,pin):
        ref=self.owner._helper_attempt_refs[data['attempt_id']]
        self._correlated(self.session,ref['slot']);self._prebirth(self.session,ref['slot'])
        if data['phase']!='observed' or pin!=self.pin:raise LaunchHold('helper_grant_replay_forbidden')

    def _born(self,slot):
        borrowed=slot.get('borrowed')
        if type(borrowed) is not self.runtime.BorrowedProcess or not self.native._helper_create_entered or self.native.job is None:
            raise LaunchHold('original_runtime_process_transfer_required')
        children=[child for child in self.native.logs if child._handle==borrowed._handle and child.pid==borrowed.pid and child.job is self.native.job]
        if len(children)!=1:raise LaunchHold('exact_retained_runtime_root_required')
        return children[0],borrowed

    def _transfer_gate(self,data,process):
        ref=self.owner._helper_attempt_refs[data['attempt_id']];slot=ref['slot'];self._correlated(self.session,slot)
        child,borrowed=self._born(slot)
        if data['phase']!='granted' or not self.native.job.thread or child.poll() is not None or process.poll() is not None:
            raise LaunchHold('original_root_must_remain_suspended')
        identity=_native_witness(self.owner.native_transport.api,process._handle)
        if identity['pid']!=borrowed.pid or identity['executable_sha256']!=self.pin or not self.owner.native_transport.includes(process):
            raise LaunchHold('independent_outer_helper_identity_required')
        if slot.get('copy') is None or slot['copy'].pid!=borrowed.pid:raise LaunchHold('retained_entry_query_transfer_required')
        return {'identity':identity,'source_process_handle':borrowed._handle,'entry_query_source_handle':slot['copy']._handle,
                'rights':'QUERY_LIMITED_INFORMATION|SYNCHRONIZE','scope':'managed_descendants'}

    def confirm_transfer(self,session,slot):
        with self.owner.dispatch_lock:
            ref,data=self._correlated(session,slot)
            if data['phase'] not in ('transferred','drained'):return False
            child,borrowed=self._born(slot);process=self.owner.children.get(data['role'])
            if (process is None or data['transfer']['source_process_handle']!=borrowed._handle or
                    self.owner._bound_native_witness(data['role']).identity()!=data['transfer']['identity']):
                raise LaunchHold('original_committed_transfer_unconfirmed')
            if (slot.get('copy') is None or slot['copy']._handle!=data['transfer']['entry_query_source_handle'] or
                    slot['copy'].pid!=borrowed.pid or slot['copy'].poll()!=process.poll()):
                raise LaunchHold('entry_query_copy_unconfirmed')
            return True # read-only; no register/reserve replay or local repair

    def lookup_transfer(self,session,slot):
        with self.owner.dispatch_lock:
            ref,data=self._correlated(session,slot)
            if not self.confirm_transfer(session,slot):return None
            scope=self.owner.registration_snapshot()
            snapshot=HelperTransferSnapshot(self.owner.record['owner'],data['attempt_id'],data['context_sha256'],
                data['reservation'],data['role'],self.helper_generation,deepcopy(data['transfer']),scope)
            key=hashlib.sha256(_bytes({'data':data,'scope':scope})).hexdigest()
            known=self.transfer_snapshots.get(data['attempt_id'])
            if known is None or known[1]!=key:self.transfer_snapshots[data['attempt_id']]=(snapshot,key)
            return self.transfer_snapshots[data['attempt_id']][0]

    def authenticate_transfer_snapshot(self,session,slot,snapshot):
        with self.owner.dispatch_lock:
            ref,data=self._correlated(session,slot)
            known=self.transfer_snapshots.get(data['attempt_id'])
            if known is None or known[0] is not snapshot or not self.confirm_transfer(session,slot):return False
            scope=self.owner.registration_snapshot()
            key=hashlib.sha256(_bytes({'data':data,'scope':scope})).hexdigest()
            expected=HelperTransferSnapshot(self.owner.record['owner'],data['attempt_id'],data['context_sha256'],data['reservation'],
                data['role'],self.helper_generation,data['transfer'],scope)
            return known[1]==key and snapshot==expected

    def _settlement_gate(self,data,proof):
        ref=self.owner._helper_attempt_refs[data['attempt_id']]
        self._correlated(self.session,ref['slot']);self._prebirth(self.session,ref['slot'])
        if data['phase']!='granted' or proof is not ref['proof']:raise LaunchHold('original_non_creation_authority_required')

    def settle_non_creation(self,session,slot,receipt):
        with self.owner.dispatch_lock:
            ref,data=self._correlated(session,slot);self._prebirth(session,slot)
            if (type(receipt) is not self.runtime.PositiveNonCreation or receipt.context is not ref['attempt'].context or
                    receipt.create_process_entered is not False or receipt.reason!='cancelled_before_create' or
                    receipt.reservation!=data['reservation'] or receipt.reservation is not self.native.helper_reservation):
                raise LaunchHold('original_native_non_creation_receipt_required')
            self._settle_existing(ref,data)
            return self._issue(ref,data['attempt_id'])

    def settle_lost_grant_before_creation(self,session,slot):
        """Explicit settlement action; lookup itself never mutates a grant."""
        with self.owner.dispatch_lock:
            ref,data=self._correlated(session,slot);self._prebirth(session,slot)
            if data['phase']!='granted' or slot.get('reservation') is not None or self.native.helper_reservation is not None:
                raise LaunchHold('exact_lost_prebirth_grant_required')
            self._settle_existing(ref,data)
            return self._issue(ref,data['attempt_id'])

    def _settle_existing(self,ref,data):
        proof={'attempt_id':data['attempt_id'],'context_sha256':data['context_sha256'],
               'generation':data['generation'],'positive_original_prebirth':True}
        ref['proof']=proof
        self.owner.settle_helper_non_creation(data['reservation'],proof,verify_non_creation=lambda *args:True)

    def _issue(self,ref,attempt_id):
        data=self.owner.record['helper_attempts'][attempt_id]
        if data['phase'] not in ('non_grant','non_creation_settled'):return None
        fingerprint=hashlib.sha256(_bytes(data)).hexdigest()
        known=self.issued.get(attempt_id)
        if known is None:
            evidence=self.entry.HelperOwnerSettlement(self.generation,attempt_id,data['context_sha256'],data['reservation'],
                'non_grant' if data['phase']=='non_grant' else 'grant_settled',fingerprint)
            self.issued[attempt_id]=(evidence,fingerprint)
        elif known[1]!=fingerprint:raise LaunchHold('settlement_record_changed')
        return self.issued[attempt_id][0]

    def lookup_non_creation(self,session,slot):
        with self.owner.dispatch_lock:
            ref,data=self._correlated(session,slot);self._prebirth(session,slot)
            return self._issue(ref,data['attempt_id']) # None for outstanding/uncertain grant

    def authenticate_settlement(self,session,slot,evidence):
        with self.owner.dispatch_lock:
            ref,data=self._correlated(session,slot);self._prebirth(session,slot)
            issued=self.issued.get(data['attempt_id'])
            return issued is not None and issued[0] is evidence and issued[1]==hashlib.sha256(_bytes(data)).hexdigest()

    def confirm_drained(self,session,slot):
        with self.owner.dispatch_lock:
            ref,data=self._correlated(session,slot)
            if not self.confirm_transfer(session,slot):return False
            child,borrowed=self._born(slot);process=self.owner.children[data['role']]
            self.owner._bound_native_exit(data['role'])
            if (child.poll() is None or process.poll() is None or self.native.job.active()!=0 or
                    not self.native.drained.is_set() or self.native.drain_failed or
                    self.native.drain is not None and self.native.drain.is_alive()):
                raise LaunchHold('original_runtime_worker_output_drain_required')
            if data['phase']=='transferred':
                next_record=deepcopy(self.owner.record);attempt=next_record['helper_attempts'][data['attempt_id']]
                attempt['phase']='drained';attempt['exit_code']=process.poll()
                self.owner._persist(next_record)
            return True # does not repair entry stream, local ACK or helper-capable abort


class WindowsNormalTransport:
    """Portable native normal route. No updater/hook transport is authorized.

    Reuses trainer's atomic Job-list creation structures, with NO kill-on-close
    and separate control endpoints instead of stdout protocol. Job descendants
    are closure evidence; original/duplicated handles remain identity authority.
    """
    def __init__(self, owner=None, abort_scope=None):
        from core.training_process_windows import _ProcessInfo
        self.owner=owner;self.abort_scope=abort_scope
        self.job=None;self.job_state='not_attempted'
        self.process=None;self.process_info=_ProcessInfo();self.creation_state='not_attempted';self.creation_return=None
        self.inherited_handles=();self.channel=None;self.raw_endpoints={};self.descriptors={}
        self.endpoint_slots={};self.null_stream=None;self.attributes=None;self.attributes_initialized=False
        self.observation_handles={}
        self.construction_error=None
        self.api=None;self._creation_api=None;self._creation_job=None;self._creation_outputs=None
        self.job_name="Local\\CollectiGen.Normal."+uuid.uuid4().hex
        if owner is not None:
            owner._creation_entered=True
            owner.native_transport=self # before first fallible native operation
        self.api = _normal_native_api()
        self._creation_api=self.api
        self.job_state='uncertain'
        self.job = self.api.CreateJobObjectW(None,self.job_name)
        self._creation_job=self.job
        self.job_state='created' if self.job else 'native_failure'
        if not self.job: raise ctypes.WinError(ctypes.get_last_error())

    def creation_outputs(self):
        return (self.process_info.process,self.process_info.thread,int(self.process_info.pid))

    def _create_original(self, *arguments):
        if self.creation_state!='not_attempted':raise LaunchHold('native_creation_retry_forbidden')
        self.creation_state='uncertain'
        self.creation_return=self.api.CreateProcessW(*arguments)
        outputs=self.creation_outputs()
        self._creation_outputs=outputs
        if self.creation_return and all(outputs):self.creation_state='born'
        elif not self.creation_return and not any(outputs):self.creation_state='native_failure'
        if self.creation_state!='born':raise LaunchHold('native_normal_creation_'+self.creation_state)

    @classmethod
    def spawn(cls, owner, argv, *, environment, abort_scope=None):
        with owner.dispatch_lock:
            return cls._spawn_locked(owner,argv,environment=environment,abort_scope=abort_scope)

    @classmethod
    def _spawn_locked(cls, owner, argv, *, environment, abort_scope=None):
        import msvcrt
        from ctypes import wintypes as W
        from core.training_process_windows import _Security,_StartupEx,_ProcessInfo
        owner._owned()
        if owner._model_free_scope is not None:
            if abort_scope is not owner._model_free_scope or abort_scope.argv != argv:raise LaunchHold('original_prebirth_admission_required')
        elif abort_scope is not None:raise LaunchHold('foreign_abort_scope')
        if (owner.data_root != owner.root/"Data" or owner.record["phase"] != "launch_hold" or
                "manager" in owner.children or type(argv) is not tuple or not argv):
            raise LaunchHold("portable_initial_normal_route_required")
        if owner.native_transport is not None:raise LaunchHold('native_creation_retry_forbidden')
        self = cls(owner=owner,abort_scope=abort_scope)
        security = _Security(ctypes.sizeof(_Security),None,True)
        to_child_r,to_child_w,from_child_r,from_child_w = [W.HANDLE() for _ in range(4)]
        launcher = W.HANDLE()
        self.endpoint_slots={'child_read':to_child_r,'parent_write':to_child_w,'parent_read':from_child_r,'child_write':from_child_w,'launcher':launcher}
        attributes=None
        initialized=False
        channel=None
        try:
            next_record=deepcopy(owner.record)
            next_record["native_scope"]={"job_name":self.job_name,"transport":"inherited_anonymous_pipes",
                                         "kill_on_close":False,"hook_transport":"held"}
            owner._persist(next_record)
            for names,reader,writer in ((("child_read","parent_write"),to_child_r,to_child_w),(("parent_read","child_write"),from_child_r,from_child_w)):
                ok=self.api.CreatePipe(ctypes.byref(reader),ctypes.byref(writer),ctypes.byref(security),65540)
                for name,handle in zip(names,(reader,writer)):
                    if handle.value:self.raw_endpoints[name]=handle.value
                if not ok: raise ctypes.WinError(ctypes.get_last_error())
            for handle in (to_child_w,from_child_r):
                if not self.api.SetHandleInformation(handle,1,0): raise ctypes.WinError(ctypes.get_last_error())
            if not self.api.DuplicateHandle(self.api.GetCurrentProcess(),self.api.GetCurrentProcess(),
                    self.api.GetCurrentProcess(),ctypes.byref(launcher),0x1000|0x100000,True,0):
                raise ctypes.WinError(ctypes.get_last_error())
            self.raw_endpoints['launcher']=launcher.value
            reader=msvcrt.open_osfhandle(from_child_r.value,os.O_RDONLY|os.O_BINARY|os.O_NOINHERIT)
            self.descriptors["reader"]=reader
            del self.raw_endpoints["parent_read"]
            from_child_r.value=None
            writer=msvcrt.open_osfhandle(to_child_w.value,os.O_WRONLY|os.O_BINARY|os.O_NOINHERIT)
            self.descriptors["writer"]=writer
            del self.raw_endpoints["parent_write"]
            to_child_w.value=None
            epoch=uuid.uuid4().hex
            channel=WindowsControlChannel(self.api,reader,writer,None,transport=self,epoch=epoch)
            self.channel=channel
            env=dict(environment)
            env.update(COLLECTIGEN_CONTROL_READ=str(to_child_r.value),COLLECTIGEN_CONTROL_WRITE=str(from_child_w.value),
                       COLLECTIGEN_LAUNCHER_QUERY=str(launcher.value),COLLECTIGEN_CONTROL_EPOCH=epoch)
            self.null_stream=open(os.devnull,"r+b",buffering=0)
            with self.null_stream as null:
                null_handle=msvcrt.get_osfhandle(null.fileno())
                os.set_handle_inheritable(null_handle,True)
                self.inherited_handles=(to_child_r.value,from_child_w.value,launcher.value,null_handle)
                if msvcrt.get_osfhandle(owner.fd) in self.inherited_handles:
                    raise LaunchHold("lease_inheritance_forbidden")
                inherited=(W.HANDLE*4)(*self.inherited_handles)
                jobs=(W.HANDLE*1)(self.job)
                size=ctypes.c_size_t()
                self.api.InitializeProcThreadAttributeList(None,2,0,ctypes.byref(size))
                attributes=ctypes.create_string_buffer(size.value)
                self.attributes=attributes
                if not self.api.InitializeProcThreadAttributeList(attributes,2,0,ctypes.byref(size)):
                    raise ctypes.WinError(ctypes.get_last_error())
                initialized=True
                self.attributes_initialized=True
                for key,value in ((0x20002,inherited),(0x2000D,jobs)):
                    if not self.api.UpdateProcThreadAttribute(attributes,0,key,value,ctypes.sizeof(value),None,None):
                        raise ctypes.WinError(ctypes.get_last_error())
                startup=_StartupEx(); startup.startup.cb=ctypes.sizeof(startup)
                # Interactive Manager owns its Qt window visibility. A zero
                # show value with STARTF_USESHOWWINDOW would hide its first
                # ordinary window; retain only inherited standard handles.
                startup.startup.flags=0x100
                startup.startup.stdin=startup.startup.stdout=startup.startup.stderr=null_handle
                startup.attributes=ctypes.cast(attributes,W.LPVOID)
                info=self.process_info # retained native outputs BEFORE the call
                block=ctypes.create_unicode_buffer("\0".join(f"{key}={value}" for key,value in sorted(env.items()))+"\0\0")
                command=ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
                self._create_original(argv[0],command,None,None,True,0x4|0x8000000|0x80000|0x400,
                        block,str(owner.root),ctypes.byref(startup),ctypes.byref(info))
                # Native originals retained BEFORE any query, cleanup or resume.
                self.process=RetainedNativeProcess(self.api,info.process,pid=info.pid,thread=info.thread)
                owner.pending.append({"role":"manager","process":self.process})
                channel.peer_handle=self.process._handle
            owner.retain("manager",self.process)
            owner.capture_manager_native_witness() # live suspended original; after retention, before Resume
            self.process.resume()
            return self.process,channel
        except BaseException as exc:
            self.construction_error=type(exc).__name__
            # Reachable via owner.native_transport even if no return occurred.
            # No endpoint, process, Job or lease is silently abandoned.
            raise
        finally:
            if initialized:
                self.api.DeleteProcThreadAttributeList(attributes);self.attributes_initialized=False
            for handle in (to_child_r,launcher):
                if handle.value:
                    value=handle.value
                    if self.api.CloseHandle(handle):
                        handle.value=None
                        for name,stored in tuple(self.raw_endpoints.items()):
                            if stored==value:del self.raw_endpoints[name]
            # Failed fd conversion leaves parent endpoints in raw_endpoints;
            # child writer copy is dispensable only after successful creation.
            if from_child_w.value:
                if self.api.CloseHandle(from_child_w):
                    from_child_w.value=None;self.raw_endpoints.pop("child_write",None)

    def recover_endpoints(self):
        """Explicit close of retained control resources, never child/Job/lease."""
        with self.owner.dispatch_lock:
            for name,slot in self.endpoint_slots.items():
                if slot.value:self.raw_endpoints[name]=slot.value
            if self.channel is not None:
                self.channel.close()
                self.descriptors.clear()
            else:
                for name,fd in tuple(self.descriptors.items()):
                    os.close(fd);del self.descriptors[name]
            for name,handle in tuple(self.raw_endpoints.items()):
                if not self.api.CloseHandle(handle):raise LaunchHold('control_endpoint_close_failed')
                del self.raw_endpoints[name]
                if name in self.endpoint_slots:self.endpoint_slots[name].value=None

    def _settle_control_resources(self, settled):
        if not settled.get('endpoints'):
            self.recover_scope_queries()
            self.recover_endpoints()
            if self.descriptors or self.raw_endpoints or any(slot.value for slot in self.endpoint_slots.values()):raise LaunchHold('control_resources_unsettled')
            if self.null_stream is not None and not self.null_stream.closed:self.null_stream.close()
            if self.attributes_initialized:
                self.api.DeleteProcThreadAttributeList(self.attributes);self.attributes_initialized=False
            settled['endpoints']=True

    def settle_normal_resources(self, terminal):
        if type(terminal) is not _NormalTerminalEvidence or terminal is not self.owner._normal_terminal or terminal.owner is not self.owner:
            raise LaunchHold('same_actor_normal_terminal_required')
        terminal=terminal.payload
        if terminal['transport'] is not self or terminal['api'] is not self.api:raise LaunchHold('same_actor_normal_terminal_required')
        settled=terminal['resources']
        self._settle_control_resources(settled)
        if (self.descriptors or self.raw_endpoints or self.observation_handles or any(slot.value for slot in self.endpoint_slots.values()) or
                (self.channel is not None and (self.channel.reader is not None or self.channel.writer is not None))):
            raise LaunchHold('control_resources_unsettled')
        for role,child in terminal['children'].items():
            for field,index in (('thread',1),('_handle',0)):
                key=role+':'+field
                if settled.get(key):
                    if getattr(child,field,None) is not None:raise LaunchHold('closed_normal_resource_replaced')
                    continue
                handle=terminal['handles'][role][index]
                if getattr(child,field,None)!=handle:raise LaunchHold('original_normal_resource_changed')
                if handle and not self.api.CloseHandle(handle):raise LaunchHold('normal_resource_close_failed')
                setattr(child,field,None);settled[key]=True
                if child is self.process:
                    if field=='thread':self.process_info.thread=None
                    else:self.process_info.process=None
        if not settled.get('job'):
            if self.job!=terminal['job']:raise LaunchHold('original_job_replaced')
            if not self.api.CloseHandle(self.job):raise LaunchHold('original_job_close_failed')
            self.job=None;settled['job']=True
        elif self.job is not None:raise LaunchHold('closed_normal_job_replaced')

    def settle_abort_resources(self, ticket, settled):
        """Exact original handles only; retry skips each positively closed resource."""
        if ticket.transport is not self:raise LaunchHold('foreign_abort_resources')
        self._settle_control_resources(settled)
        for name,index in (('thread',1),('process',0)):
            if settled.get(name):continue
            handle=ticket.outputs[index]
            current=self.process_info.thread if name=='thread' else self.process_info.process
            # Resume may already have positively transferred thread closure.
            if name=='thread' and self.process is not None and self.process.thread is None:
                self.process_info.thread=None;settled[name]=True;continue
            if current != handle:raise LaunchHold('original_resource_changed')
            if handle and not self.api.CloseHandle(handle):raise LaunchHold('original_'+name+'_close_failed')
            if name=='thread':self.process_info.thread=None
            else:self.process_info.process=None
            if self.process is not None:
                if name=='thread':self.process.thread=None
                else:self.process._handle=None
            settled[name]=True
        if not settled.get('job'):
            if self.job != ticket.job:raise LaunchHold('original_job_replaced')
            if self.job_state=='created' and self.job_active_count()!=0:raise LaunchHold('owned_job_descendants_unresolved')
            if self.job and not self.api.CloseHandle(self.job):raise LaunchHold('original_job_close_failed')
            self.job=None;settled['job']=True


    def job_active_count(self):
        from core.training_process_windows import WindowsTrainingJob
        return WindowsTrainingJob._active(self.api,self.job)

    def includes(self, process):
        from ctypes import wintypes as W
        value=W.BOOL()
        if not self.api.IsProcessInJob(process._handle,self.job,ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(value.value)

    def observe_stock_hook_peer(self, channel, process, request, argv, *, recovery):
        """Observe an already retained hook copy against its actual pipe client.

        The future stock provider must retain its transferred query handle before
        calling this method. No PID adoption, client DTO permission, SDK dispatch,
        or lease release occurs here. Anonymous normal pipes cannot satisfy the
        named-pipe peer checks. This observation alone does not arm stock apply.
        """
        import msvcrt
        from ctypes import wintypes as W
        from core.manager_self_update_recovery import ManagerRecovery, digest
        from suite_core.manager.bootstrap_entry import classify_invocation
        from core.manager_stock_hook_pipe import StockHookPipe
        owner = self.owner
        with owner.dispatch_lock:
            attempt = owner._maintenance_context(request, allow_pending=True)
            updater_role = owner._stock_updater_role(request)
            is_repair = updater_role == 'repair_updater'
            pending = [item for item in owner.pending if item['process'] is process]
            if (owner.native_transport is not self or type(channel) not in (WindowsControlChannel, StockHookPipe)
                    or channel.transport is not self or channel.api is not self.api
                    or type(process) is not RetainedNativeProcess or process.api is not self.api
                    or channel.peer_handle != process._handle or not process._handle
                    or len(pending) != 1 or not pending[0]['role'].startswith('hook:')
                    or pending[0]['role'] in owner.children
                    or owner.record['phase'] != ('repair_applying' if is_repair else 'applying')
                    or attempt['dispatch'] != 'created'):
                raise LaunchHold('original_pending_native_stock_hook_required')
            updater = owner.children.get(updater_role)
            if (type(updater) is not RetainedNativeProcess or updater.api is not self.api
                    or updater.poll() is not None or process.poll() is not None
                    or not self.includes(updater) or not self.includes(process)):
                raise LaunchHold('live_original_updater_hook_job_required')
            if type(recovery) is not ManagerRecovery or recovery.paths['root'] != owner.root:
                raise LaunchHold('original_hook_recovery_required')
            journal = recovery.load()
            recovery.retained_context(journal)
            binding = journal['binding']
            before, after = (binding['target'],binding['prior']) if is_repair else (binding['prior'],binding['target'])
            if (recovery.id != request.transaction_id
                    or before['version'] != request.current_version
                    or after['version'] != request.target_version
                    or after['files']['archive']['sha256'] != request.package_sha256
                    or after['channel'] != request.channel):
                raise LaunchHold('original_hook_transaction_changed')
            invocation = classify_invocation(tuple(argv))
            expected = {'--veloapp-obsolete': before,
                        '--veloapp-updated': after}.get(invocation.stage)
            if expected is None or invocation.version != expected['version'] or len(invocation.argv) != 2:
                raise LaunchHold('stock_apply_hook_stage_version_required')
            # Query the server and client on the actual root-side endpoint, not
            # PID values decoded from a hello. No OpenProcess is used.
            server, client = W.ULONG(), W.ULONG()
            handle = channel.handle if type(channel) is StockHookPipe else msvcrt.get_osfhandle(channel.reader)
            if (not self.api.GetNamedPipeServerProcessId(handle, ctypes.byref(server))
                    or not self.api.GetNamedPipeClientProcessId(handle, ctypes.byref(client))
                    or server.value != self.api.GetProcessId(self.api.GetCurrentProcess())):
                raise LaunchHold('original_local_hook_pipe_peer_required')
            hook_witness = _native_witness(self.api, process._handle)
            updater_witness = _native_witness(self.api, updater._handle)
            committed = owner.record['children'][updater_role]
            if (client.value != hook_witness['pid'] or hook_witness['pid'] != process.pid
                    or hook_witness['executable_sha256'] != expected['files']['executable']['sha256']
                    or updater_witness['pid'] != committed['pid']
                    or updater_witness['creation_filetime'] != committed['creation_filetime']
                    or updater_witness['executable_sha256'] != request.updater_sha256):
                raise LaunchHold('native_stock_hook_or_updater_binding_changed')
            actual_argv = _native_process_argv(process)
            if (not actual_argv or Path(actual_argv[0]) != owner.root / 'Apps/Manager/current/CollectiGenManager.exe'
                    or actual_argv[1:] != invocation.argv):
                raise LaunchHold('native_stock_hook_command_line_changed')
            epoch = channel.epoch
            if type(epoch) is not str or not re.fullmatch('[0-9a-f]{32}', epoch):
                raise LaunchHold('fresh_native_hook_epoch_required')
            observed = getattr(self, '_stock_hook_observations', {})
            if (epoch in observed or len(observed) >= 4
                    or any(item['hook'] == hook_witness for item in observed.values())):
                raise LaunchHold('stock_hook_observation_replay_or_bound')
            live = [child for child in owner.children.values() if child.poll() is None]
            pending_live = [item['process'] for item in owner.pending if item['process'].poll() is None]
            if (any(not self.includes(child) for child in (*live, *pending_live))
                    or self.job_active_count() != len(live) + len(pending_live)):
                raise LaunchHold('stock_hook_job_scope_incomplete')
            owner._maintenance_context(request, allow_pending=True)
            proof = dict(schema=1, transaction_id=request.transaction_id,
                attempt_identity_sha256=request.attempt_identity_sha256,
                binding_sha256=request.binding_sha256, owner_token=owner.record['owner'],
                recovery_binding_sha256=digest(binding), stage=invocation.stage,
                role=pending[0]['role'],
                version=invocation.version, argv_sha256=hashlib.sha256(
                    json.dumps(tuple(argv), ensure_ascii=False, separators=(',', ':')).encode()).hexdigest(),
                epoch=epoch, hook=hook_witness, updater=updater_witness)
            ref = owner._commit_record('stock_hook_native_observation', proof, request.transaction_id)
            owner._retain_verified_native_witness(pending[0]['role'], process, self.api, hook_witness)
            # Keep exact references alongside the immutable proof. A returned
            # dict or persisted JSON cannot substitute for these original actors.
            observed[epoch] = dict(proof, ref=ref, process=process, channel=channel,
                                   updater_process=updater, lease=owner.fd)
            self._stock_hook_observations = observed
            return deepcopy(proof)

    def assert_accounted_live_scope(self):
        self.owner._owned(allow_reservations=True)
        live=[process for process in self.owner.children.values() if process.poll() is None]
        if any(not self.includes(process) for process in live):
            raise LaunchHold("unregistered_or_foreign_job_descendants")
        managed={item['role'] for item in self.owner.record.get('helper_attempts',{}).values() if item['phase'] in ('transferred','drained')}
        if not managed:
            if self.job_active_count()!=len(live):raise LaunchHold('unregistered_or_foreign_job_descendants')
            return
        active=self.job_active_count()
        if active < len(live) or active > 128:
            raise LaunchHold("managed_outer_scope_incomplete")
        # Authenticated managed roots stay in the original no-breakaway outer Job.
        # Contained descendants need no ancestry reconstruction or adoption.

    def recover_scope_queries(self):
        with self.owner.dispatch_lock:
            for handle in tuple(getattr(self,'observation_handles',{})):
                if not self.api.CloseHandle(handle):raise LaunchHold('descendant_query_close_failed')
                del self.observation_handles[handle]

    def close_after_drain(self):
        with self.owner.dispatch_lock:
            if self.owner._normal_terminal is not None and self.owner._normal_terminal.payload['resources'].get('job'):
                return
            if self.job_active_count() != 0: raise LaunchHold("owned_job_descendants_unresolved")
            self.recover_scope_queries()
            if not self.api.CloseHandle(self.job):raise LaunchHold('original_job_close_failed')
            self.job=None

class StockHookSelfTransfer:
    """Hook-local temporary updater locator; never a deployment/process owner.

    The authenticated root supplies its original updater witness and the query
    Job handle deposited in that updater's table. This helper validates both
    before depositing only its OWN self-query handle there. No root DUP_HANDLE,
    lease, termination, inheritance or arbitrary-source API is exposed.
    """
    def __init__(self, api):
        self.api = api
        self.updater = self.job = self.remote = None
        self.retirement_source = None
        self.phase = 'fresh'

    def deposit(self, updater_witness, job_source_handle):
        from ctypes import wintypes as W
        if (self.phase != 'fresh' or type(updater_witness) is not dict
                or set(updater_witness) != {'pid','creation_filetime','executable_sha256'}
                or type(updater_witness['pid']) is not int or updater_witness['pid'] <= 0
                or type(job_source_handle) is not int or not 0 < job_source_handle < 2**63):
            raise LaunchHold('authenticated_original_updater_transfer_required')
        self.phase = 'locating'
        self.updater = self.api.OpenProcess(0x40 | 0x1000 | 0x100000, False, updater_witness['pid'])
        if not self.updater: raise LaunchHold('stock_updater_transfer_locator_unavailable')
        if _native_witness(self.api, self.updater) != updater_witness:
            raise LaunchHold('original_stock_updater_locator_changed')
        output = W.HANDLE()
        ok = self.api.DuplicateHandle(self.updater, job_source_handle,
            self.api.GetCurrentProcess(), ctypes.byref(output), 4, False, 0)
        self.job = output.value
        if not ok or not self.job: raise LaunchHold('original_stock_job_query_uncertain')
        for process in (self.updater, self.api.GetCurrentProcess()):
            inside = W.BOOL()
            if not self.api.IsProcessInJob(process, self.job, ctypes.byref(inside)) or not inside.value:
                raise LaunchHold('stock_hook_original_job_mismatch')
        self.phase = 'depositing'
        output = W.HANDLE()
        ok = self.api.DuplicateHandle(self.api.GetCurrentProcess(), self.api.GetCurrentProcess(),
            self.updater, ctypes.byref(output), 0x1000 | 0x100000, False, 0)
        self.remote = output.value
        if not ok or not self.remote: raise LaunchHold('stock_hook_self_deposit_uncertain')
        self.phase = 'deposited'
        return self.remote

    def retire_after_root_acceptance(self):
        """Only our exact API-created deposit; no wire-selected handle closure."""
        if self.phase == 'retirement_uncertain':
            # DUPLICATE_CLOSE_SOURCE closes the source even on failure. Its
            # numeric value may already name another handle: NEVER replay it.
            # Only the retained original updater's physical exit can settle
            # uncertainty about that process's complete handle table.
            if not self.updater or self.api.WaitForSingleObject(self.updater, 0) != 0:
                raise LaunchHold('stock_hook_original_updater_lifetime_unresolved')
            self.phase = 'retiring_locals'
        if self.phase not in ('deposited','retiring_locals'):
            raise LaunchHold('original_stock_hook_deposit_required')
        if self.phase == 'deposited':
            source = self.remote
            self.retirement_source = source  # diagnostic only; never executable
            self.remote = None
            self.phase = 'retirement_uncertain'  # consumed BEFORE the native call
            if not self.api.DuplicateHandle(self.updater, source, None, None, 0, False, 1):
                raise LaunchHold('stock_hook_deposit_retirement_unconfirmed')
            self.phase = 'retiring_locals'
        for field in ('job', 'updater'):
            handle = getattr(self, field)
            if handle and not self.api.CloseHandle(handle):
                raise LaunchHold('stock_hook_local_query_retirement_unconfirmed')
            setattr(self, field, None)
        self.phase = 'retired'

