"""Windows atomic Job ownership for the retained Ollama root and descendants."""
import threading
from dataclasses import dataclass
from types import MappingProxyType
from core.training_process_windows import WindowsTrainingJob
import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path
import socket
import struct
import subprocess
import time
from core.ollama_owned_executor import ExecutorHold, require
from suite_core.discovery.component_staging import guard


def decode_listener_table(raw, port):
    require(len(raw)>=4,'listener_table_truncated')
    count=struct.unpack_from('<I',raw)[0]
    require(count<=65536 and len(raw)>=4+count*24,'listener_table_bound')
    rows=[struct.unpack_from('<6I',raw,4+i*24) for i in range(count)]
    # Only loopback IPv4 or wildcard (which also claims loopback), LISTEN=2.
    return {r[5] for r in rows if r[0]==2 and socket.ntohs(r[2]&0xffff)==port
            and r[1] in {0,struct.unpack('<I',socket.inet_aton('127.0.0.1'))[0]}}


@dataclass(frozen=True)
class BorrowedProcess:
    """Synchronous process-only transfer view; receiver must duplicate before return.

    This is neither a local polling owner nor a Job capability. Never retain it
    as the entry's helper: the original handle closes only after explicit Stop.
    """
    _handle: int
    pid: int


@dataclass(frozen=True)
class HelperAttempt:
    identity: str
    context: object


@dataclass(frozen=True)
class SettledNonCreation:
    """Trusted capability's positively resolved original-attempt evidence."""
    attempt: HelperAttempt
    evidence_id: str


@dataclass(frozen=True)
class PositiveNonCreation:
    """Native gate receipt: CreateProcess was never entered for this reservation."""
    reservation: object
    context: object
    create_process_entered: bool = False
    reason: str = 'cancelled_before_create'


class JobChild:
    def __init__(self,job):self.job=job;self.pid=job.pid;self._handle=job.process
    def poll(self):return self.job.poll()
    def wait(self,timeout):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            code=self.poll()
            if code is not None and self.job.active()==0:return code
            time.sleep(.05)
        raise TimeoutError('owned_job_exit_unconfirmed')


class WindowsOwnedProcess:
    termination_method="retained_owned_job_terminate"
    def __init__(self):
        require(os.name=='nt','windows_native_adapter_required')
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        self.tcp=ctypes.WinDLL('iphlpapi',use_last_error=True).GetExtendedTcpTable
        self.tcp.argtypes=[ctypes.c_void_p,ctypes.POINTER(w.DWORD),w.BOOL,w.ULONG,ctypes.c_int,w.ULONG]
        self.tcp.restype=w.DWORD
        self.times=self.kernel.GetProcessTimes
        self.times.argtypes=[w.HANDLE,*([ctypes.POINTER(w.FILETIME)]*4)];self.times.restype=w.BOOL
        self.image=self.kernel.QueryFullProcessImageNameW
        self.image.argtypes=[w.HANDLE,w.DWORD,w.LPWSTR,ctypes.POINTER(w.DWORD)];self.image.restype=w.BOOL
        self.logs={}

        self.job_factory=WindowsTrainingJob;self.job=None;self.drain=None
        self.drained=threading.Event();self.drain_failed=False;self.start_failed=False;self.verified_identities={}

    def bind_helper_capability(self, capability):
        # Trusted constructor injection only. Request payloads cannot enable this.
        require(self.job is None and not self.logs,'helper_binding_after_birth')
        require(all(callable(getattr(capability,name,None)) for name in
            ('reserve','register_suspended','settle_non_creation','reconcile_non_creation','drained')),
            'helper_capability_unqualified')
        self.helper_capability=capability
        self.helper_reservation=None
        self.helper_accounting_held=False
        self.helper_attempt=None;self._helper_attempt_capability=None
        self._helper_create_entered=False

    @property
    def helper_accounting_identity(self):
        attempt=getattr(self,'helper_attempt',None)
        return attempt.identity if attempt is not None and self.helper_accounting_held else None

    def observe_helper_accounting(self, reviewed_identity):
        """Bounded original-reply observation; never clears durable accounting."""
        attempt=self.helper_attempt;cap=self.helper_capability
        require(self.helper_accounting_held and attempt is not None and reviewed_identity==attempt.identity
                and cap is self._helper_attempt_capability,'exact_helper_attempt_review_required')
        return cap.observe_original_reply(attempt)

    def reconcile_helper_non_creation(self,reviewed_identity,*,record,cancelled=lambda:False):
        # Read-only evidence lookup, never replay reserve/settle. Local gate must
        # positively prove spawn_suspended was never invoked for this attempt.
        attempt=self.helper_attempt;cap=self.helper_capability
        require(self.helper_accounting_held and attempt is not None and
            reviewed_identity==attempt.identity,'exact_helper_attempt_review_required')
        require(cap is self._helper_attempt_capability,'helper_attempt_capability_changed')
        require(not self._helper_create_entered and not self.logs and self.job is None,
            'helper_non_creation_unproven')
        require(not cancelled(),'operation_cancelled')
        evidence=cap.reconcile_non_creation(attempt)
        require(type(evidence) is SettledNonCreation and evidence.attempt is attempt and
            type(evidence.evidence_id) is str and 0<len(evidence.evidence_id)<=256,
            'helper_settlement_evidence_unconfirmed')
        require(not cancelled(),'operation_cancelled')
        require(self.helper_attempt is attempt and cap is self.helper_capability,
            'helper_attempt_changed')
        record(evidence)  # Executor persists/readbacks before releasing the latch.
        self.helper_reservation=None;self.helper_accounting_held=False
        self.helper_attempt=None;self._helper_attempt_capability=None
        return evidence.evidence_id

    @property
    def helper_close_held(self):
        cap=getattr(self,'helper_capability',None)
        return bool(getattr(self,'helper_accounting_held',False) or
            cap is not None and cap.close_held)

    def listener_owners(self, port):
        size=w.DWORD(0)
        require(self.tcp(None,ctypes.byref(size),False,socket.AF_INET,3,0) in {0,122},'listener_query_failed')
        for _ in range(3):
            require(4<=size.value<=4+65536*24,'listener_table_bound')
            buf=ctypes.create_string_buffer(size.value)
            code=self.tcp(buf,ctypes.byref(size),False,socket.AF_INET,3,0)
            if code==122:continue
            require(code==0,'listener_query_failed')
            return decode_listener_table(buf.raw[:size.value],port)
        raise ExecutorHold('listener_table_unstable')

    def require_free_port(self, port):
        require(not self.listener_owners(port),'loopback_port_in_use')
        with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            try:sock.bind(('127.0.0.1',port))
            except OSError:raise ExecutorHold('loopback_port_not_bindable') from None
        # Reservation cannot be inherited by stock serve. Startup verifies actual
        # bound PID before any API request, refusing the release-to-spawn race.

    def spawn(self,program,environment,log,*,cancelled=lambda:False,helper_context=None):
        guard(program);guard(log)
        return self.spawn_command((str(program),'serve'),str(program.parent),dict(environment),
            cancelled=cancelled,helper_context=helper_context)

    def spawn_command(self,command,cwd,environment,*,cancelled=lambda:False,
                      helper_context=None,output_callback=None,output_limit=65536):
        """Run an already admitted command through the one original Job owner.

        Admission and native command verification belong to the helper capability.
        This seam preserves suspended registration and drains all output, delivering
        only a bounded prefix to the optional in-memory consumer.
        """
        require(type(command) is tuple and 1<=len(command)<=256 and
            all(type(arg) is str and arg and '\0' not in arg for arg in command),
            'exact_external_command_required')
        require(len(subprocess.list2cmdline(command).encode('utf-16-le'))//2<32767,
            'external_command_bound')
        require(isinstance(cwd,(str,Path)) and Path(cwd).is_absolute() and
            '\0' not in str(cwd) and Path(command[0]).is_absolute(),
            'absolute_external_launch_paths_required')
        require(type(environment) is dict and len(environment)<=128 and
            all(type(k) is str and type(v) is str and k and '=' not in k and
                '\0' not in k+v and len(k)<=128 and len(v)<=32767
                for k,v in environment.items()),'exact_external_environment_required')
        require(output_callback is None or callable(output_callback),'external_output_callback_required')
        require(type(output_limit) is int and 0<=output_limit<=1024*1024,'external_output_bound')
        guard(Path(command[0]));guard(Path(cwd))
        cwd=str(cwd);environment=dict(environment)
        cap=getattr(self,'helper_capability',None)
        require(not self.helper_close_held,'helper_accounting_unconfirmed')
        validate_launch=getattr(cap,'validate_launch',None)
        if callable(validate_launch):validate_launch(command,cwd,environment)
        require(not cancelled(),'operation_cancelled')
        self.start_failure=None;self.start_failure_phase=None
        if self.job is not None:
            require(self.job.handle is None and not self.logs and self.drained.is_set() and
                (self.drain is None or not self.drain.is_alive()),'previous_owned_job_closure_unconfirmed')
            self.job=None;self.drain=None;self.drained=threading.Event()
            self.drain_failed=False;self.start_failed=False;self.verified_identities={}
        require(not self.logs,'original_process_authority_still_retained')
        from uuid import uuid4
        if cap is not None:
            require(type(helper_context) is dict,'helper_launch_context_required')
            # Freeze the worker's reviewed installation/lifecycle context. The
            # reservation precedes even Job construction, and seals deny grants.
            context=MappingProxyType(dict(helper_context,helper_attempt_id=uuid4().hex))
            self.helper_attempt=HelperAttempt(context['helper_attempt_id'],context)
            self._helper_attempt_capability=cap;self._helper_create_entered=False
            self.helper_accounting_held=True
            try:
                self.helper_reservation=cap.reserve(context)
            except BaseException as error:
                from core.manager_launch_owner import LaunchHold
                finish=getattr(cap,'wait_for_original_reserve',None)
                if not (type(error) is LaunchHold and str(error)=='control_channel_idle_timeout'
                        and callable(finish)):
                    raise
                # Complete the retained original request; never call reserve twice.
                self.helper_reservation=finish(self.helper_attempt,cancelled=cancelled)
            if cancelled():
                cap.settle_non_creation(self.helper_reservation,
                    PositiveNonCreation(self.helper_reservation,context))
                self.helper_reservation=None;self.helper_accounting_held=False
                raise ExecutorHold('operation_cancelled')
        self.job=self.job_factory('CollectiGen-Ollama-owned-'+uuid4().hex)
        if cap is not None and cancelled():
            # No spawn_suspended call occurred. Close only the empty private Job.
            self.job.close();self.job=None
            cap.settle_non_creation(self.helper_reservation,
                PositiveNonCreation(self.helper_reservation,context))
            self.helper_reservation=None;self.helper_accounting_held=False
            raise ExecutorHold('operation_cancelled')
        # Inference descendants inherit this job, installed atomically before execution.
        spawn_failed=False
        phase='create_process'
        try:
            if cap is not None:self._helper_create_entered=True
            self.job.spawn_suspended(command,cwd,environment)
        except BaseException as error:
            # Do not discard any partially created original process/job handle.
            self.start_failure=error;self.start_failure_phase=phase
            if self.job.process is None:
                # Exception is NOT a positive zero-call receipt. Keep accounting
                # held; only the entry's native reconciliation can settle it.
                self.job.close();self.job=None;raise
            spawn_failed=True
        child=JobChild(self.job);self.logs[child]=None
        reader_ready=None
        def discard_output():
            consumer=output_callback;remaining=output_limit
            try:
                read=self.job.stdout.read
                reader_ready.set()
                while True:
                    raw=read(65536)
                    if not raw:break
                    if consumer is not None and remaining:
                        prefix=raw[:remaining];remaining-=len(prefix)
                        try:consumer(prefix)
                        except BaseException:
                            # Keep draining the original pipe even if the optional
                            # consumer fails; preserve the existing drain-failure state.
                            self.drain_failed=True;consumer=None
            except BaseException:self.drain_failed=True
            finally:self.drained.set()
        try:
            phase='helper_registration'
            if not spawn_failed and cap is not None:
                # Retain then register the suspended original before ANY output
                # reader construction/start. Never transfer the private Job.
                try:
                    cap.register_suspended(self.helper_reservation,
                        BorrowedProcess(int(child._handle),child.pid))
                except BaseException as error:
                    from core.manager_launch_owner import LaunchHold
                    finish=getattr(cap,'wait_for_original_transfer',None)
                    if not (type(error) is LaunchHold and str(error)=='control_channel_idle_timeout'
                            and callable(finish)):
                        raise
                    # Keep the original child suspended through verified registration.
                    finish(self.helper_reservation,cancelled=cancelled)
            require(not cancelled(),'operation_cancelled')
            phase='output_drain'
            reader_ready=threading.Event()
            self.drain=threading.Thread(target=discard_output,name='Owned-Ollama-output-drain',daemon=False)
            self.drain.start()
            require(reader_ready.wait(2) and not self.drain_failed,'owned_output_reader_not_ready')
            phase='resume'
            require(not cancelled(),'operation_cancelled')
            if not spawn_failed:self.job.resume()
            else:self.start_failed=True
        except BaseException as error:
            # Return the original owner so normal identity/startup failure cleanup
            # retains exact handles. No exception can orphan an assigned process.
            self.start_failed=True
            if self.start_failure is None:
                self.start_failure=error;self.start_failure_phase=phase
            if self.drain is None or self.drain.ident is None:self.drained.set()
        return child
    def identity(self, child, program):
        if self.start_failed:
            raise ExecutorHold('owned_job_start_or_drain_failed') from getattr(self,'start_failure',None)
        creation,exit_time,kernel,user=[w.FILETIME() for _ in range(4)]
        require(self.times(w.HANDLE(int(child._handle)),*[ctypes.byref(v) for v in (creation,exit_time,kernel,user)]),'retained_process_time_unavailable')
        image=ctypes.create_unicode_buffer(32768);size=w.DWORD(len(image))
        require(self.image(w.HANDLE(int(child._handle)),0,image,ctypes.byref(size)),'retained_process_image_unavailable')
        require(os.path.normcase(image.value)==os.path.normcase(str(program.resolve())),'retained_process_image_changed')
        value={'pid':child.pid,'created_100ns':(creation.dwHighDateTime<<32)|creation.dwLowDateTime,'program':str(program.resolve())}
        self.verified_identities.setdefault(child,dict(value));return value

    def check_identity(self,child,identity,program):
        self.check_retained_handle(child)
        require(child.job is self.job,'exact_owned_job_required')
        if child.poll() is not None:
            # An exited root may no longer answer image queries. Its original
            # handle, frozen creation/image identity and exact job remain owned.
            require(self.verified_identities.get(child)==identity,'retained_exited_root_identity_changed')
            require(identity.get('program')==str(program.resolve()),'retained_exited_root_program_changed')
        else:require(self.identity(child,program)==identity,'retained_process_identity_changed')
    def owned_execution_live(self,child):
        self.check_retained_handle(child)
        require(child.job is self.job,'exact_owned_job_required')
        # Root liveness and remaining job members are independent observations.
        return child.poll() is None or self.job.active()>0
    def terminate_owned(self,child):
        self.check_retained_handle(child);require(child.job is self.job,'exact_owned_job_required')
        self.job.terminate()  # Only atomically enrolled root and descendants.
    def close(self,child):
        require(child.poll() is not None and child.job.active()==0,'owned_job_not_empty')
        require(self.drained.wait(2),'owned_output_drain_unconfirmed')
        if self.drain is not None and self.drain.ident is not None:self.drain.join(2)
        require(self.drain is None or not self.drain.is_alive(),'owned_output_worker_not_exited')
        cap=getattr(self,'helper_capability',None)
        if cap is not None:
            # Root handle signal + inner Job zero + output worker exit were all
            # confirmed above. Lost ACK retains quiescent originals for Stop.
            cap.drained(self.helper_reservation,
                BorrowedProcess(int(child._handle),child.pid))
        child.job.close();child._handle=None;self.logs.pop(child)
        if cap is not None:
            self.helper_reservation=None;self.helper_accounting_held=False
    def check_retained_handle(self,child):
        require(child in self.logs and child._handle is not None,'original_spawn_handle_required')
    def job_empty(self):return self.job is not None and self.job.handle is None

    def pause(self, seconds):time.sleep(seconds)


def owned_version_probe():
    """Fixed direct loopback endpoint, no proxy, redirect or model route."""
    import http.client
    from core.generation_transport import strict_json
    connection=http.client.HTTPConnection('127.0.0.1',11439,timeout=1)
    response=None
    try:
        connection.request('GET','/api/version',headers={'Connection':'close'})
        response=connection.getresponse()
        require(response.status==200,'owned_version_http_failed')
        raw=response.read(4097);require(len(raw)<=4096,'owned_version_bound')
        return strict_json(raw).get('version')
    finally:
        if response is not None:response.close()
        connection.close()
