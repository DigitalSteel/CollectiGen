"""Explicit direct-GGUF reference under stock retained Windows Job ownership.

No acquisition, model copies, Ollama cache, credentials or automatic startup.
Only the original Job may be stopped; uncertain settlement keeps it retained.
"""
from dataclasses import dataclass
from pathlib import Path
from hashlib import sha256
from uuid import uuid4
import os,stat,struct,socket,threading,time,http.client,io,select,json
from datetime import datetime,timezone

from core.training_source_io import _assert_unredirected,_identity
from core.model_resource_registry import ResourceInspection,_current
from core.generation_transport import strict_json
from suite_core.discovery.component_staging import guard

DIRECT_CHAT_MAX_OUTPUT_TOKENS = 512

def require(condition,code):
    if not condition: raise ValueError(code)


def inspect_gguf_reference(path,*,cancelled=lambda:False):
    """Safetensors registry lacks GGUF inspection; reuse its stable-file identity."""
    path=Path(path).absolute();_assert_unredirected(path)
    require(path.suffix.lower()=='.gguf','direct_gguf_reference_required')
    before=path.stat(follow_symlinks=False)
    require(stat.S_ISREG(before.st_mode) and not getattr(before,'st_file_attributes',0)&(0x1000|0x40000|0x400000),'fully_local_regular_model_required')
    digest=sha256();total=0
    with path.open('rb') as handle:
        require(_identity(os.fstat(handle.fileno()))==_identity(before),'model_changed_before_read')
        prefix=handle.read(8)
        require(len(prefix)==8 and prefix[:4]==b'GGUF' and struct.unpack('<I',prefix[4:])[0] in (2,3),'supported_gguf_container_required')
        digest.update(prefix);total=8
        while True:
            require(not cancelled(),'reference_inspection_cancelled')
            raw=handle.read(4*1024*1024)
            if not raw:break
            digest.update(raw);total+=len(raw)
        after=os.fstat(handle.fileno())
    _assert_unredirected(path)
    require(_identity(before)==_identity(after)==_identity(path.stat(follow_symlinks=False)) and total==after.st_size,'model_changed_during_read')
    return ResourceInspection(path,'sha256:'+digest.hexdigest(),total,_identity(after),
        {'format':'gguf','version':struct.unpack('<I',prefix[4:])[0],'compatibility':'unqualified'})


@dataclass(frozen=True)
class RuntimeFile:
    name:str
    size:int
    digest:str


@dataclass(frozen=True)
class LlamaCppSelection:
    executable:Path
    inventory:tuple[RuntimeFile,...]
    model:ResourceInspection
    context_length:int=4096
    threads:int=2

    @property
    def alias(self):return 'collectigen-direct-'+self.model.resource_id.removeprefix('sha256:')[:24]

    def validate(self):
        require(type(self.model) is ResourceInspection and self.model.container.get('format')=='gguf','verified_gguf_reference_required')
        require(self.context_length==4096 and self.threads in (1,2),'bounded_cpu_configuration_required')
        require(self.executable.name.lower()=='llama-server.exe' and self.executable.is_absolute(),'exact_stock_server_required')
        _current(self.model)
        require(type(self.inventory) is tuple and 1<=len(self.inventory)<=128,'bounded_runtime_inventory_required')
        names=set()
        for row in self.inventory:
            require(type(row) is RuntimeFile and row.name==Path(row.name).name and row.name.lower() not in names,'exact_runtime_member_required')
            names.add(row.name.lower());path=self.executable.parent/row.name;guard(path)
            require(path.is_file() and path.stat().st_size==row.size and sha256(path.read_bytes()).hexdigest()==row.digest,'runtime_inventory_changed')
        require('llama-server.exe' in names,'server_inventory_required')


def inspect_runtime(executable,model,*,cancelled=lambda:False):
    executable=Path(executable).absolute();guard(executable)
    require(executable.name.lower()=='llama-server.exe' and executable.is_file(),'installed_llama_server_required')
    members=[executable,*sorted(executable.parent.glob('*.dll'))]
    require(len(members)<=128,'runtime_inventory_bound')
    rows=[];total=0
    for path in members:
        require(not cancelled(),'runtime_inspection_cancelled');guard(path)
        size=path.stat().st_size;total+=size
        require(size<=512*1024*1024 and total<=2*1024**3,'runtime_bytes_bound')
        rows.append(RuntimeFile(path.name,size,sha256(path.read_bytes()).hexdigest()))
    selection=LlamaCppSelection(executable,tuple(rows),model);selection.validate();return selection


class LlamaCppProvider:
    def __init__(self,selection,*,job_factory=None,native_factory=None,clock=time.monotonic,helper_factory=None,qualified_modules=False,expected_version=None):
        require(type(selection) is LlamaCppSelection,'explicit_direct_selection_required')
        self.selection=selection;self.job_factory=job_factory;self.native_factory=native_factory
        self.clock=clock;self.job=None;self.native=None;self.child=None;self.identity=None
        self.port=None;self.ready=False;self.output=bytearray();self.output_limit=65536
        self._output_lock=threading.Lock();self._busy=threading.Lock();self._unknown=False
        self.last_exit=None;self.last_identity=None;self.last_job_empty=False
        self.verified_version=''
        self.helper_factory=helper_factory;self.qualified_modules=qualified_modules;self.loaded_modules=()
        self.expected_version=expected_version

    @property
    def close_held(self):return self.child is not None or self.job is not None or bool(self.native is not None and getattr(self.native,'helper_close_held',False))

    @property
    def capabilities(self):
        if self.ready and (self.child is None or self.child.poll() is not None):self.ready=False
        return {'direct_reference':True,'ready':self.ready,'basic_chat':self.ready,
            'streaming_chat':self.ready,'tools':False,'downloads':False,'gpu_offload':False,
            'native_root_helper_qualified':self.helper_factory is not None and bool(self.loaded_modules)}

    def _spawn(self,command,*,cancelled=lambda:False):
        from core.training_process_windows import WindowsTrainingJob
        from core.ollama_owned_windows import WindowsOwnedProcess,JobChild
        require(not self.close_held,'original_direct_runtime_still_held')
        self.native=(self.native_factory or WindowsOwnedProcess)()
        if callable(getattr(self.native,'spawn_command',None)):
            if self.job_factory is not None:self.native.job_factory=self.job_factory
            if self.helper_factory is not None:self.native.bind_helper_capability(self.helper_factory(self.native,tuple(command)))
            environment={name:os.environ[name] for name in ('SYSTEMROOT','WINDIR') if name in os.environ};environment['CUDA_VISIBLE_DEVICES']='-1'
            def capture(raw):
                with self._output_lock:self.output.extend(raw[:max(0,self.output_limit-len(self.output))])
            try:
                self.child=self.native.spawn_command(tuple(command),str(self.selection.executable.parent),environment,
                    cancelled=cancelled,
                    helper_context=dict(self.native.helper_capability.reviewed) if self.helper_factory is not None else None,
                    output_callback=capture,output_limit=self.output_limit)
                self.job=self.native.job
                if self.native.start_failed:raise self.native.start_failure
                self.identity=self.native.identity(self.child,self.selection.executable);self.last_identity=dict(self.identity)
            except BaseException:
                self.job=self.native.job
                if self.child is None:
                    children=tuple(self.native.logs)
                    if len(children)==1:self.child=children[0]
                raise
            return
        require(self.helper_factory is None,'shared_registered_spawn_required')
        self.job=(self.job_factory or WindowsTrainingJob)('CollectiGen-direct-llamacpp-'+uuid4().hex)
        self.native.job=self.job
        environment={name:os.environ[name] for name in ('SYSTEMROOT','WINDIR') if name in os.environ}
        environment['CUDA_VISIBLE_DEVICES']='-1'
        try:
            self.job.spawn_suspended(tuple(command),str(self.selection.executable.parent),environment)
            self.child=JobChild(self.job);self.native.logs[self.child]=None
            self._start_output_reader()
            self.identity=self.native.identity(self.child,self.selection.executable)
            self.last_identity=dict(self.identity)
            self.job.resume()
        except BaseException:
            # Retain partially created original handles; Stop reconciles them.
            if self.job.process is None:
                self.job.close();self.job=None
            else:
                if self.child is None:
                    self.child=JobChild(self.job);self.native.logs[self.child]=None
                if self.native.drain is None:self._start_output_reader()
            raise

    def _start_output_reader(self):
        if self.job.stdout is None:
            self.native.drained.set();return
        def drain():
            try:
                while True:
                    raw=self.job.stdout.read(4096)
                    if not raw:break
                    with self._output_lock:
                        left=self.output_limit-len(self.output)
                        if left>0:self.output.extend(raw[:left])
            except BaseException:self.native.drain_failed=True
            finally:self.native.drained.set()
        self.native.drain=threading.Thread(target=drain,name='Manager-direct-runtime-output',daemon=False)
        self.native.drain.start()

    def version(self,*,cancelled=lambda:False,seconds=10):
        self.selection.validate();self.output.clear()
        original=None
        try:
            self._spawn((str(self.selection.executable),'--version'),cancelled=cancelled)
            deadline=self.clock()+seconds
            while self.child.poll() is None:
                require(not cancelled() and self.clock()<deadline,'version_probe_interrupted')
                time.sleep(.02)
            require(self.child.poll()==0,'stock_version_probe_failed')
            self.stop(cancelled=lambda:False)
            text=bytes(self.output).decode('utf-8','replace')
            require('version' in text.lower() or 'build' in text.lower(),'stock_version_unavailable')
            require(self.expected_version is None or text==self.expected_version,'admitted_stock_version_changed')
            self.verified_version=text
            return text
        except BaseException as error:
            original=error
            raise
        finally:
            if self.close_held:
                try:self.stop(cancelled=lambda:False)
                except BaseException as cleanup:
                    self.cleanup_error=cleanup
                    if original is None:raise
                    original.add_note('Original version-probe owner retained for exact Stop reconciliation.')

    def start(self,*,resources,cancelled=lambda:False,seconds=120):
        self.selection.validate()
        require(bool(self.verified_version),'stock_server_version_review_required')
        # CPU only: do not reserve or disturb the currently busy GPU.
        ram=[row for row in resources.readings if row.telemetry is not None and row.telemetry.free_ram_bytes is not None]
        require(len(ram)==1 and ram[0].state_at(datetime.now(timezone.utc))=='measured','fresh_ram_measurement_required')
        require(ram[0].telemetry.free_ram_bytes>=self.selection.model.size+8*1024**3,'direct_model_ram_headroom_required')
        with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as reservation:
            if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):reservation.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            reservation.bind(('127.0.0.1',0));self.port=reservation.getsockname()[1]
        self.ready=False;self._unknown=False;self.output.clear()
        command=(str(self.selection.executable),'-m',str(self.selection.model.path),'--host','127.0.0.1','--port',str(self.port),
            '--ctx-size','4096','--parallel','1','--threads',str(self.selection.threads),'--threads-batch',str(self.selection.threads),
            '--device','none','--n-gpu-layers','0','--alias',self.selection.alias,'--no-webui',
            '--cors-origins',f'http://127.0.0.1:{self.port}','--no-cors-credentials')
        try:
            self._spawn(command,cancelled=cancelled)
            deadline=self.clock()+seconds
            while self.clock()<deadline:
                require(not cancelled(),'direct_load_cancelled')
                self.native.check_identity(self.child,self.identity,self.selection.executable)
                require(self.child.poll() is None,'direct_server_exited_during_load')
                owners=self.native.listener_owners(self.port)
                require(not owners or owners=={self.child.pid},'direct_endpoint_foreign_owner')
                if owners:
                    health=self._get('/health')
                    if health and health.get('status')=='ok':
                        models=self._get('/v1/models')
                        require(models and any(row.get('id')==self.selection.alias for row in models.get('data',())),'direct_loaded_model_unverified')
                        if self.qualified_modules:self.verify_loaded_modules()
                        self.ready=True;return self.capabilities
                time.sleep(.1)
            raise TimeoutError('direct_load_deadline')
        except BaseException as error:
            try:self.stop(cancelled=lambda:False)
            except BaseException as cleanup:
                self.cleanup_error=cleanup
                error.add_note('Original direct runtime remains retained for exact Stop reconciliation.')
            raise

    def _get(self,route):
        require(route in ('/health','/v1/models'),'fixed_direct_read_route_required')
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=.5)
        try:
            conn.request('GET',route,headers={'Connection':'close'});response=conn.getresponse()
            if response.status!=200:return None
            require(response.getheader('Content-Encoding','identity')=='identity','encoded_direct_reply_refused')
            raw=response.read(65537);require(len(raw)<=65536,'direct_read_bound')
            value=strict_json(raw);require(type(value) is dict,'direct_read_object_required');return value
        except (OSError,http.client.HTTPException):return None
        finally:conn.close()

    def stop(self,*,cancelled=lambda:False,seconds=10):
        self.ready=False
        if self.job is None:
            if self.native is not None and getattr(self.native,'helper_close_held',False):
                identity=self.native.helper_accounting_identity
                self.native.reconcile_helper_non_creation(identity,record=lambda evidence:setattr(self,'last_non_creation',evidence),cancelled=cancelled)
            require(not self.close_held,'direct_helper_accounting_still_held')
            return True
        require(not self._busy.locked(),'direct_request_must_drain_before_server_stop')
        require(self.child is not None,'partial_direct_birth_requires_reconciliation')
        if self.identity is not None:self.native.check_identity(self.child,self.identity,self.selection.executable)
        else:
            # Exact atomically assigned creation handles still authorize stopping
            # this original Job after an image query/birth observation failed.
            self.native.check_retained_handle(self.child)
            require(self.child.job is self.job,'exact_partial_job_required')
        if self.child.poll() is None or self.job.active()>0:self.native.terminate_owned(self.child)
        deadline=self.clock()+seconds
        while self.child.poll() is None or self.job.active()>0:
            require(not cancelled() and self.clock()<deadline,'direct_stop_still_held');time.sleep(.02)
        self.last_exit=self.child.poll();self.last_job_empty=self.job.active()==0
        self.native.close(self.child)
        failed=self.native.drain_failed
        self.child=None;self.job=None;self.identity=None
        require(not failed,'direct_output_drain_failed_actor_closed')
        return True

    def verify_loaded_modules(self):
        """Observe only this original local Job; root transfer rights stay unchanged."""
        self.native.check_identity(self.child,self.identity,self.selection.executable)
        paths=self.job.module_paths(self.child.pid)
        expected={r.name.casefold():r for r in self.selection.inventory}
        system=Path(os.environ['SYSTEMROOT'])
        allowed=(system/'System32',system/'SysWOW64',system/'WinSxS')
        rows=[]
        for value in paths:
            path=Path(value)
            if path.parent==self.selection.executable.parent:
                guard(path)
                row=expected.get(path.name.casefold())
                require(row is not None and path.stat().st_size==row.size and sha256(path.read_bytes()).hexdigest()==row.digest,'unadmitted_runtime_module')
                rows.append({'name':row.name,'sha256':row.digest,'origin':'selected_runtime'})
            else:
                require(any(path.is_relative_to(directory) for directory in allowed),'foreign_non_system_module')
                # Windows servicing legitimately hard-links system DLLs into
                # WinSxS/System32. They are OS dependencies, outside our pinned
                # replacement inventory; still refuse any redirected path.
                _assert_unredirected(path)
                require(stat.S_ISREG(path.stat(follow_symlinks=False).st_mode),'regular_windows_module_required')
                rows.append({'name':path.name,'origin':'windows_system'})
        self.native.check_identity(self.child,self.identity,self.selection.executable)
        self.loaded_modules=tuple(rows)

    def stream_chat(self,request,cancelled):
        """Basic SSE adapter; original Job/alias must remain live for the request."""
        from suite_core.manager.model_chat_controller import ChatEvent
        require(self.ready and not self._unknown and request.target.model==self.selection.alias
            and request.target.digest==self.selection.model.resource_id,'direct_chat_not_ready')
        _current(self.selection.model)
        require(self._busy.acquire(blocking=False),'direct_single_slot_busy')
        conn=response=None;complete=False
        try:
            self.native.check_identity(self.child,self.identity,self.selection.executable)
            require(self.native.listener_owners(self.port)=={self.child.pid},'direct_endpoint_owner_changed')
            deadline=self.clock()+request.timeout_seconds
            def check():
                require(not cancelled() and self.clock()<deadline and self.ready,'direct_request_interrupted_outcome_unknown')
            class Reader(io.RawIOBase):
                def __init__(self,sock):self.sock=sock;self.base=sock.makefile('rb',buffering=0);sock.setblocking(False)
                def readable(self):return True
                def readinto(self,buffer):
                    while True:
                        check();size=self.base.readinto(buffer)
                        if size is not None:return size
                        select.select([self.sock],[],[],.02)
                def close(self):
                    try:self.base.close()
                    finally:super().close()
            class Socket:
                def makefile(self,mode):return io.BufferedReader(Reader(conn.sock))
            class Response(http.client.HTTPResponse):
                def __init__(self,sock,**kwargs):super().__init__(Socket(),**kwargs)
            payload={'model':self.selection.alias,'messages':[{'role':row.role,'content':row.text} for row in request.messages],
                'stream':True,'max_tokens':min(DIRECT_CHAT_MAX_OUTPUT_TOKENS,request.output_tokens),'temperature':.2,'chat_template_kwargs':{'enable_thinking':False}}
            raw=json.dumps(payload,allow_nan=False).encode();require(len(raw)<=131072,'direct_chat_payload_bound')
            conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=1);conn.response_class=Response
            check();conn.request('POST','/v1/chat/completions',raw,{'Content-Type':'application/json','Accept':'text/event-stream','Connection':'close'})
            response=conn.getresponse();check()
            require(response.status==200 and response.getheader('Content-Type','').split(';')[0]=='text/event-stream','direct_stream_rejected')
            require(response.getheader('Content-Encoding','identity')=='identity','direct_stream_encoded')
            total=0;finished=False
            while True:
                line=response.readline(65537);check();total+=len(line)
                self.native.check_identity(self.child,self.identity,self.selection.executable)
                require(self.child.poll() is None and self.native.listener_owners(self.port)=={self.child.pid},'direct_stream_owner_changed')
                require(line and line.endswith(b'\n') and len(line)<=65536 and total<=1048576,'direct_stream_bound_or_incomplete')
                if line.strip()==b'':continue
                require(line.startswith(b'data: '),'direct_sse_record_required')
                data=line[6:].strip()
                if data==b'[DONE]':
                    require(finished,'direct_chat_terminal_missing');complete=True;yield ChatEvent('',True);return
                value=strict_json(data);require(type(value) is dict and not value.get('error'),'direct_chat_rejected')
                choices=value.get('choices');require(type(choices) is list and len(choices)==1,'direct_single_choice_required')
                choice=choices[0];require(type(choice) is dict and choice.get('index',0)==0,'direct_choice_required')
                delta=choice.get('delta',{})
                require(type(delta) is dict and not delta.get('tool_calls') and delta.get('role','assistant')=='assistant','direct_tools_or_role_refused')
                text=delta.get('content') or '';require(type(text) is str,'direct_text_required')
                if choice.get('finish_reason') is not None:
                    require(choice['finish_reason'] in ('stop','length'),'direct_finish_reason_refused');finished=True
                if text:yield ChatEvent(text,False)
        finally:
            if not complete or cancelled():self._unknown=True
            if response is not None:response.close()
            if conn is not None:conn.close()
            self._busy.release()
