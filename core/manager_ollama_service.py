"""Explicit existing-runner model actions, called by an owning host worker.

No scheduler, endpoint store, runtime launch or tool execution. POST mutations
require an exact reviewed plan. Interrupted submissions remain uncertain.
"""
from dataclasses import dataclass, field
from datetime import timedelta
from hashlib import sha256
import http.client
import io
import json
import select
import socket
import ssl
import threading
from time import monotonic
from urllib.parse import urlsplit

from core.generation_transport import strict_json
from suite_core.discovery.ollama_readonly import ReadRoute, ReadState
from suite_core.discovery.swarm_readonly import EndpointBinding
from suite_core.manager.metadata_binding import ManagerMetadataBinding
from suite_core.shared_backend_routing_candidate import PendingMetadataOperation, RoutingError, _private_errors


def require(ok, code):
    if not ok: raise RoutingError(code)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


@dataclass(frozen=True)
class ModelChoice:
    name: str
    digest: str
    size_bytes: int
    loaded: bool


@dataclass(frozen=True)
class ContextMemoryEstimate:
    context_length: int
    full_attention_layers: int
    kv_heads: int
    head_dimension: int
    bytes_per_element: int
    parallel_slots: int
    additional_state_bytes: int
    runtime_overhead_bytes: int
    architecture_reference: str

    @property
    def kv_bytes(self):
        # K and V, per full-attention layer/token/parallel slot. Hybrid recurrent
        # state is separately supplied, never silently treated as dense KV.
        return 2*self.context_length*self.full_attention_layers*self.kv_heads*self.head_dimension*self.bytes_per_element*self.parallel_slots

    def resident_estimate(self, model):
        return model.size_bytes+self.kv_bytes+self.additional_state_bytes+self.runtime_overhead_bytes


@dataclass(frozen=True)
class HermesRunnerConfiguration:
    binding: EndpointBinding = field(repr=False)
    model: ModelChoice
    runtime_version: str
    native_context_limit: int
    memory: ContextMemoryEstimate
    advertised_capabilities: tuple[str, ...]
    model_metadata_sha256: str
    available_ram_bytes: int
    available_vram_bytes: int | None
    observed_served_context: int | None = None
    provider: str = 'ollama'

    def __post_init__(self):
        require(type(self.binding) is EndpointBinding and type(self.model) is ModelChoice and type(self.memory) is ContextMemoryEstimate, 'typed_runner_configuration_required')
        require(self.provider == 'ollama', 'truthful_ollama_provider_required')
        require(type(self.native_context_limit) is int and 64000 <= self.memory.context_length <= self.native_context_limit, 'hermes_64000_context_required')
        require({'completion','tools'} <= set(self.advertised_capabilities), 'native_tools_model_required')
        values = (self.memory.full_attention_layers,self.memory.kv_heads,self.memory.head_dimension,self.memory.bytes_per_element,self.memory.parallel_slots)
        require(all(type(v) is int and v > 0 for v in values) and self.memory.parallel_slots == 1, 'reviewed_context_geometry_required')
        require(type(self.memory.additional_state_bytes) is int and self.memory.additional_state_bytes >= 0 and type(self.memory.runtime_overhead_bytes) is int and self.memory.runtime_overhead_bytes >= 2*1024**3, 'explicit_runtime_overhead_required')
        require(bool(self.memory.architecture_reference) and len(self.model_metadata_sha256) == 64 and bool(self.runtime_version), 'runtime_model_architecture_evidence_required')
        require(type(self.available_ram_bytes) is int and self.available_ram_bytes >= self.memory.resident_estimate(self.model), 'context_ram_fit_unproven')
        require(self.observed_served_context is None or type(self.observed_served_context) is int, 'served_context_observation_required')

    @property
    def hermes_ready(self):
        # Planned/requested context is not the currently served allocation.
        return self.observed_served_context is not None and self.observed_served_context >= self.memory.context_length

    def as_mapping(self):
        return {'provider':self.provider,'endpoint':self.binding.endpoint,'openai_api_base':self.binding.endpoint.rstrip('/')+'/v1',
            'model':self.model.name,'model_digest':self.model.digest,'runtime_version':self.runtime_version,
            'context_length':self.memory.context_length,'native_context_limit':self.native_context_limit,
            'observed_served_context':self.observed_served_context,'hermes_ready':self.hermes_ready,
            'model_metadata_sha256':self.model_metadata_sha256,'architecture_reference':self.memory.architecture_reference,
            'kv_bytes_estimate':self.memory.kv_bytes,'resident_bytes_estimate':self.memory.resident_estimate(self.model),
            'gpu_residency_estimate_fits':None if self.available_vram_bytes is None else self.available_vram_bytes >= self.memory.resident_estimate(self.model),
            'context_geometry':self.memory.__dict__,'model_size_bytes':self.model.size_bytes,
            'available_ram_bytes':self.available_ram_bytes,'available_vram_bytes':self.available_vram_bytes}

    @property
    def identity(self):
        return sha256(encoded(self.as_mapping())).hexdigest()


def installed_choices(report, now):
    """Exact fresh provider identities; not weight-file or tool qualification."""
    installed = report.observation(ReadRoute.INSTALLED)
    require(installed.state is ReadState.AVAILABLE and installed.freshness_at(now, report.limits).value == 'fresh', 'fresh_installed_inventory_required')
    loaded = report.observation(ReadRoute.LOADED)
    active = set()
    if loaded.state is ReadState.AVAILABLE and loaded.freshness_at(now, report.limits).value == 'fresh':
        active = {row.identity for row in loaded.models or ()}
    rows = tuple(ModelChoice(row.name, row.digest, row.size_bytes, row.identity in active)
        for row in installed.models or () if row.digest and row.size_bytes is not None)
    require(len({row.name for row in rows}) == len(rows), 'ambiguous_model_name')
    return rows


@dataclass(frozen=True)
class PullTransferPolicy:
    total_seconds: int
    idle_seconds: int
    def __post_init__(self):
        require(type(self.total_seconds) is int and 1<=self.total_seconds<=86400,'explicit_pull_total_deadline_required')
        require(type(self.idle_seconds) is int and 1<=self.idle_seconds<=600 and self.idle_seconds<=self.total_seconds,'explicit_pull_idle_deadline_required')


class LocalModelApi:
    """Small fixed-route native HTTP adapter for stock Ollama JSON APIs.

    Existing metadata binding owns loopback, credentials and endpoint policy.
    No proxy/DNS/redirect/retry. Deadline closes the socket, not a process.
    A closed pull/chat connection does not prove backend cancellation.
    """
    def __init__(self, binding):
        require(type(binding) is EndpointBinding, 'typed_loopback_binding_required')
        self.binding = binding

    def stream_chat(self, payload, *, cancelled, seconds=120, limit=1048576):
        """Bounded native NDJSON, for an already admitted basic-text chat.

        Source contract: https://docs.ollama.com/api/streaming and /api/chat;
        pinned native handler: ollama/ollama v0.40.1 server/routes.go.
        The host owns model/readiness admission. Disconnecting this request is
        not evidence that inference stopped; observe the backend again.
        """
        from suite_core.manager.model_chat_controller import ollama_event

        require(type(seconds) is int and 1 <= seconds <= 120 and
                type(limit) is int and 1 <= limit <= 1048576,
                'bounded_chat_request_required')
        require(type(payload) is dict and set(payload) <=
                {'model', 'messages', 'stream', 'options', 'truncate', 'think'},
                'basic_chat_payload_required')
        model = payload.get('model')
        require(type(model) is str and 0 < len(model) <= 300 and
                payload.get('stream') is True, 'explicit_chat_stream_required')
        messages = payload.get('messages')
        require(type(messages) is list and 1 <= len(messages) <= 65,
                'bounded_chat_messages_required')
        for message in messages:
            require(type(message) is dict and set(message) == {'role', 'content'}
                    and type(message['role']) is str
                    and message['role'] in {'system', 'user', 'assistant'}
                    and type(message['content']) is str, 'basic_chat_message_required')
        options = payload.get('options')
        require(type(options) is dict and set(options) == {'num_ctx', 'num_predict'}
                and type(options['num_ctx']) is int and 512 <= options['num_ctx'] <= 262144
                and type(options['num_predict']) is int and 1 <= options['num_predict'] <= 4096,
                'bounded_chat_options_required')
        require('truncate' not in payload or payload['truncate'] is False,
                'chat_truncation_refused')
        require('think' not in payload or type(payload['think']) is bool,
                'basic_chat_thinking_policy_required')
        try:
            body = encoded(payload)
        except (ValueError, TypeError, UnicodeError):
            raise RoutingError('invalid_chat_payload') from None
        require(len(body) <= 131072, 'chat_payload_bound')

        def stopped():
            try: return bool(cancelled())
            except Exception: return True

        require(not stopped(), 'chat_cancelled_before_submission')
        parts = urlsplit(self.binding.endpoint)
        cls = http.client.HTTPSConnection if parts.scheme == 'https' else http.client.HTTPConnection
        conn = cls(parts.hostname, parts.port, timeout=min(seconds, 5))
        response = abort_socket = request_socket = None
        drain, expired = threading.Event(), threading.Event()
        deadline_at = monotonic() + seconds

        def abort():
            for sock in (request_socket, abort_socket, conn.sock):
                if sock is not None:
                    try: sock.shutdown(socket.SHUT_RDWR)
                    except OSError: pass

        def deadline():
            expired.set(); abort()

        def watch_cancel():
            while not drain.wait(.02):
                if stopped():
                    abort()

        def check():
            require(not stopped(), 'chat_interrupted_outcome_unknown')
            require(not expired.is_set() and monotonic() < deadline_at,
                    'chat_deadline_outcome_unknown')

        class CancellableReader(io.RawIOBase):
            # Stock SocketIO owns the original socket reference. Windows
            # shutdown alone does not reliably wake a pending select/recv.
            def __init__(self, sock):
                self.sock = sock
                self.base = sock.makefile('rb', buffering=0)
                sock.setblocking(False)

            def readable(self): return True

            def readinto(self, buffer):
                while True:
                    check()
                    try: size = self.base.readinto(buffer)
                    except ssl.SSLWantReadError: size = None
                    except ssl.SSLWantWriteError:
                        select.select([], [self.sock], [], .02)
                        continue
                    if size is not None: return size
                    select.select([self.sock], [], [], .02)

            def close(self):
                try: self.base.close()
                finally: super().close()

        class ResponseSocket:
            def makefile(self, mode):
                return io.BufferedReader(CancellableReader(request_socket))

        class ChatResponse(http.client.HTTPResponse):
            def __init__(self, sock, **kwargs):
                super().__init__(ResponseSocket(), **kwargs)

        conn.response_class = ChatResponse
        timer = threading.Timer(seconds, deadline)
        watcher = threading.Thread(target=watch_cancel, name='ollama-chat-cancel', daemon=True)
        timer.daemon = True
        timer.start(); watcher.start()
        try:
            conn.connect()
            # HTTPConnection closes its socket object for Connection: close,
            # while the response file retains the OS connection. Keep an owned
            # duplicate handle solely for shutdown, including HTTPS sockets.
            abort_socket = socket.socket.dup(conn.sock)
            request_socket = conn.sock
            conn.sock.settimeout(seconds)
            check()
            conn.request('POST', '/api/chat', body,
                         {'Content-Type': 'application/json', 'Accept': 'application/x-ndjson',
                          'Connection': 'close'})
            response = conn.getresponse()
            check()
            require(response.status == 200, 'chat_request_rejected')
            require(response.getheader('Content-Type', '').split(';')[0].strip().lower()
                    == 'application/x-ndjson', 'chat_response_not_ndjson')
            require(response.getheader('Content-Encoding', 'identity').strip().lower()
                    == 'identity', 'encoded_chat_response_refused')
            total = 0
            while True:
                check()
                raw = response.readline(min(limit, 65536) + 1)
                check()
                require(bool(raw), 'chat_completion_missing_outcome_unknown')
                total += len(raw)
                require(len(raw) <= 65536 and total <= limit, 'chat_response_bound')
                require(raw.endswith(b'\n'), 'chat_record_unterminated')
                try:
                    value = strict_json(raw.decode('utf-8'))
                    ollama_event(value)
                except (ValueError, TypeError, UnicodeError, RecursionError):
                    raise RoutingError('chat_record_rejected') from None
                require('model' not in value or value['model'] == model, 'chat_model_changed')
                require(value.get('message', {}).get('role', 'assistant') == 'assistant',
                        'chat_assistant_message_required')
                check()
                yield value
                if value['done']:
                    return
        except (OSError, http.client.HTTPException):
            check()
            raise RoutingError('chat_transport_failed_outcome_unknown') from None
        finally:
            drain.set()
            timer.cancel()
            watcher.join(); timer.join()
            if response is not None: response.close()
            conn.close()
            if abort_socket is not None: abort_socket.close()

    def post(self, route, payload, *, cancelled, seconds=120, limit=1024*1024, progress=lambda value:None, pull_policy=None):
        require(route in {'show', 'generate', 'chat', 'pull'}, 'unsupported_model_route')
        require(type(seconds) is int and 1 <= seconds <= 120 and type(limit) is int and 1 <= limit <= 1024*1024, 'bounded_model_request_required')
        if route=='pull':require(type(pull_policy) is PullTransferPolicy,'explicit_pull_transfer_policy_required')
        deadline_seconds=pull_policy.total_seconds if route=='pull' else seconds
        idle_seconds=pull_policy.idle_seconds if route=='pull' else seconds
        require(not cancelled(), 'model_action_cancelled_before_submission')
        parts = urlsplit(self.binding.endpoint)
        cls = http.client.HTTPSConnection if parts.scheme == 'https' else http.client.HTTPConnection
        conn = cls(parts.hostname, parts.port, timeout=min(seconds, 5))
        response = timer = watcher = None
        drain=threading.Event()
        expired = threading.Event()
        try:
            conn.connect()
            sock = conn.sock
            sock.settimeout(idle_seconds)
            def abort():
                expired.set()
                try: sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass
            timer = threading.Timer(deadline_seconds, abort); timer.daemon = True; timer.start()
            if route=='pull':
                def watch_cancel():
                    while not drain.wait(.05):
                        try:stopped=cancelled()
                        except Exception:stopped=True
                        if stopped:
                            abort();return
                watcher=threading.Thread(target=watch_cancel,daemon=True);watcher.start()
            require(not cancelled(), 'model_action_cancelled_before_submission')
            conn.request('POST', '/api/'+route, encoded(payload), {'Content-Type':'application/json','Accept':'application/json','Connection':'close'})
            response = conn.getresponse()
            require(response.status == 200, 'model_request_rejected')
            content_type=response.getheader('Content-Type','').split(';')[0].strip().lower()
            require(content_type in ({'application/json','application/x-ndjson'} if route=='pull' else {'application/json'}), 'model_response_not_json')
            require(response.getheader('Content-Encoding','identity').lower() == 'identity', 'encoded_model_response_refused')
            if route=='pull':
                require(payload.get('stream') is True,'pull_progress_stream_required')
                total=0;last=None
                while True:
                    require(not cancelled() and not expired.is_set(),'pull_interrupted_outcome_unknown')
                    raw=response.readline(16385)
                    if not raw:break
                    total+=len(raw)
                    require(len(raw)<=16384 and total<=16*1024*1024,'pull_progress_bound')
                    value=strict_json(raw)
                    require(type(value) is dict and type(value.get('status')) is str and not value.get('error'),'pull_progress_rejected')
                    for key in ('total','completed'):
                        require(key not in value or type(value[key]) is int and value[key]>=0,'pull_progress_invalid_count')
                    require(not cancelled() and not expired.is_set(),'pull_interrupted_outcome_unknown')
                    progress(value);last=value
                require(last is not None and last.get('status')=='success','pull_completion_missing')
                require(not cancelled() and not expired.is_set(),'pull_interrupted_outcome_unknown')
                return last
            raw = response.read(limit+1)
            require(len(raw) <= limit and not expired.is_set(), 'model_response_limit_or_deadline')
            require(not cancelled(), 'model_action_cancelled_after_submission')
            value = strict_json(raw)
            require(type(value) is dict and not value.get('error'), 'model_response_rejected')
            return value
        finally:
            drain.set()
            if watcher is not None:watcher.join()
            if timer is not None: timer.cancel(); timer.join()
            if response is not None: response.close()
            conn.close()


@dataclass(frozen=True)
class ModelActionPlan:
    action: str
    model: ModelChoice
    endpoint_pin: str
    observed_at: object
    payload_bytes: bytes = field(repr=False)
    ram_budget_bytes: int = 0
    runner_configuration_identity: str = ''
    expected_runtime_version: str = ''
    pull_policy: PullTransferPolicy | None = None

    @property
    def fingerprint(self):
        return sha256(encoded({'action':self.action,'model':self.model.__dict__, 'endpoint_pin':self.endpoint_pin,
            'observed_at':self.observed_at.isoformat(),'payload_sha256':sha256(self.payload_bytes).hexdigest(),'ram_budget_bytes':self.ram_budget_bytes,
            'runner_configuration_identity':self.runner_configuration_identity,'expected_runtime_version':self.expected_runtime_version,'pull_policy':None if self.pull_policy is None else self.pull_policy.__dict__})).hexdigest()


@dataclass(frozen=True)
class ModelActionCompletion:
    ticket: object = field(repr=False)
    plan: ModelActionPlan
    response_bytes: bytes = field(repr=False)


class ExistingRunnerModelService:
    """One exact host binding. Host keeps existing worker, pending and admission lock.

    'show' is explicit read-only model inspection. load/unload/chat/pull are
    independent mutations. No tool call is executed by this class.
    """
    def __init__(self, binding, api=None, *, pull_outcome=None):
        require(type(binding) is ManagerMetadataBinding, 'manager_metadata_binding_required')
        self.binding = binding
        self.api = api or LocalModelApi(binding.scope.binding)
        require(self.api.binding == binding.scope.binding, 'model_api_binding_changed')
        self.pull_outcome=pull_outcome
        self._claims = {}
        self._claim_lock = threading.Lock()

    def _guard(self):
        with _private_errors('model_host_failed'):
            self._guard_inner()

    def validate_chat_scope(self):
        """Recheck the existing host binding; model admission stays with host."""
        self._guard()

    def _guard_inner(self):
        policy = self.binding.endpoint_policy()
        require(type(policy) is bool and policy, 'endpoint_network_refused')
        registrations = self.binding._snapshot()
        require(type(registrations) is tuple and len(registrations) == 1 and registrations[0] is self.binding.registration, 'configured_backend_changed')
        self.binding.registration.validate()
        require(self.api.binding == self.binding.scope.binding, 'model_api_binding_changed')

    def inspect(self, model, *, cancelled):
        self._guard()
        result = self.api.post('show', {'model':model.name,'verbose':False}, cancelled=cancelled)
        self._guard()
        capabilities = result.get('capabilities', [])
        require(type(capabilities) is list and len(capabilities) <= 16 and all(type(v) is str for v in capabilities), 'invalid_model_capabilities')
        return tuple(capabilities)

    def plan(self, action, model, now, *, available_ram_bytes, available_disk_bytes=0, capabilities=(), prompt='', tools=(), context_length=4096, hermes_configuration=None, pull_policy=None):
        require(type(model) is ModelChoice and model.digest and type(model.size_bytes) is int and model.size_bytes > 0, 'exact_model_choice_required')
        require(action in {'load','unload','chat','pull'}, 'explicit_model_action_required')
        require(type(context_length) is int and 1 <= context_length <= 262144, 'bounded_context_required')
        if hermes_configuration is not None:
            require(type(hermes_configuration) is HermesRunnerConfiguration and hermes_configuration.binding == self.binding.scope.binding
                and hermes_configuration.model == model and hermes_configuration.memory.context_length == context_length
                and (action in {'load','unload'} or hermes_configuration.hermes_ready), 'hermes_served_context_not_qualified')
        elif context_length > 4096:
            raise RoutingError('large_context_resource_review_required')
        require(now.utcoffset() is not None, 'aware_time_required')
        # Conservative review floor only; not a measured VRAM/KV-cache guarantee.
        budget = model.size_bytes + 2*1024**3 if hermes_configuration is None else hermes_configuration.memory.resident_estimate(model)
        require(type(available_ram_bytes) is int and (action == 'unload' or available_ram_bytes >= budget), 'model_ram_review_required')
        require(type(prompt) is str and len(prompt.encode()) <= 16384 and type(tools) is tuple and len(tools) <= 8, 'bounded_chat_required')
        payload = {'model':model.name,'stream':False}
        if action == 'pull':
            require(type(pull_policy) is PullTransferPolicy,'explicit_pull_transfer_policy_required')
            payload['stream']=True
            require(not prompt and not tools, 'download_is_not_inference')
            require(type(available_disk_bytes) is int and available_disk_bytes >= budget, 'model_disk_review_required')
        elif action in {'load','unload'}:
            require(not prompt and not tools, 'load_is_not_inference')
            payload.update(prompt='', keep_alive='5m' if action == 'load' else 0, options={'num_ctx':context_length})
        else:
            require('completion' in capabilities and (not tools or 'tools' in capabilities), 'model_capability_review_required')
            payload.update(messages=[{'role':'user','content':prompt}], options={'num_ctx':context_length,'num_predict':128}, keep_alive='5m')
            if tools: payload['tools'] = list(tools)
        raw = encoded(payload); require(len(raw) <= 65536, 'bounded_model_payload_required')
        return ModelActionPlan(action, model, sha256(self.binding.scope.binding.endpoint.encode()).hexdigest(), now, raw, budget,
            '' if hermes_configuration is None else hermes_configuration.identity,
            '' if hermes_configuration is None else hermes_configuration.runtime_version, pull_policy if action=='pull' else None)

    def _current(self, ticket, plan, now, current_pending, cancelled, *, before_dispatch=False):
        self._guard()
        require(ticket.operation == 'ollama_model_action' and ticket.request_id > 0 and ticket.generation >= 0, 'model_ticket_required')
        pending = current_pending()
        connection = self.binding.registration.connection
        require(type(pending) is PendingMetadataOperation and pending.ticket == ticket and pending.owner == connection.owner and pending.backend_id == connection.backend_id, 'stale_model_action')
        stopped = cancelled(); require(type(stopped) is bool and not stopped, 'model_action_cancelled')
        completion_seconds=plan.pull_policy.total_seconds+30 if plan.action=='pull' and type(plan.pull_policy) is PullTransferPolicy else 150
        require(timedelta(0) <= now-plan.observed_at <= timedelta(seconds=30 if before_dispatch else completion_seconds), 'model_plan_stale')
        require(plan.endpoint_pin == sha256(self.binding.scope.binding.endpoint.encode()).hexdigest(), 'model_endpoint_changed')

    def work(self, ticket, plan, *, reviewed_fingerprint, current_pending, cancelled, now, inventory, progress=lambda value:None):
        require(type(plan) is ModelActionPlan and reviewed_fingerprint == plan.fingerprint, 'exact_model_plan_review_required')
        require(plan.action in {'load','unload','chat','pull'}, 'explicit_model_action_required')
        self._current(ticket, plan, now(), current_pending, cancelled, before_dispatch=True)
        report = inventory()
        require(self.binding.scope.accepts(report), 'model_inventory_scope_changed')
        choices = installed_choices(report, now())
        if plan.expected_runtime_version:
            version = report.observation(ReadRoute.VERSION)
            require(version.state is ReadState.AVAILABLE and version.freshness_at(now(),report.limits).value == 'fresh'
                and version.version == plan.expected_runtime_version, 'runtime_configuration_changed')
            if plan.action == 'chat':
                loaded = report.observation(ReadRoute.LOADED)
                require(loaded.state is ReadState.AVAILABLE and loaded.freshness_at(now(),report.limits).value == 'fresh'
                    and any(row.name == plan.model.name and row.digest == plan.model.digest and row.context_length is not None
                        and row.context_length >= strict_json(plan.payload_bytes)['options']['num_ctx'] for row in loaded.models or ()), 'hermes_served_context_changed')
        if plan.action != 'pull': require(plan.model in choices, 'model_inventory_changed')
        self._current(ticket, plan, now(), current_pending, cancelled, before_dispatch=True)
        route = {'load':'generate','unload':'generate','chat':'chat','pull':'pull'}[plan.action]
        # Existing host remains the lifetime/recovery owner. Each worker ticket
        # can submit at most once here, including after an uncertain outcome.
        with self._claim_lock:
            require(type(ticket.bridge_id) is str and 0 < len(ticket.bridge_id) <= 128, 'model_ticket_required')
            position = (ticket.generation, ticket.request_id)
            previous = self._claims.get(ticket.bridge_id)
            require(previous is None or position > previous, 'model_ticket_already_submitted')
            require(previous is not None or len(self._claims) < 16, 'model_service_lifetime_bound')
            self._claims[ticket.bridge_id] = position
        if route=='pull':
            require(self.pull_outcome is not None,'durable_pull_outcome_owner_required')
            self.pull_outcome.begin(plan)
        try:
            value = self.api.post(route, strict_json(plan.payload_bytes), cancelled=cancelled, **({'progress':progress,'pull_policy':plan.pull_policy} if route=='pull' else {}))
            require(value.get('done') is True if plan.action != 'pull' else value.get('status') == 'success', 'model_completion_missing')
            if plan.action == 'pull':
                report = inventory()
                require(self.binding.scope.accepts(report) and plan.model in installed_choices(report, now()), 'downloaded_model_identity_unverified')
            self._current(ticket, plan, now(), current_pending, cancelled)
            if route=='pull':self.pull_outcome.complete(plan)
            return ModelActionCompletion(ticket, plan, encoded(value))
        except Exception:
            # Backend may continue after disconnect. Owning host must reconcile
            # tags/ps before retry; never turn an ambiguous result into success.
            raise RoutingError('model_action_outcome_unknown_reconcile_before_retry') from None

    def admit(self, completion, *, current_pending, cancelled, now):
        require(type(completion) is ModelActionCompletion, 'model_completion_required')
        self._current(completion.ticket, completion.plan, now, current_pending, cancelled)
        # Private response, including tool proposals; host authorizes any tools.
        return strict_json(completion.response_bytes)


def connect_existing_runner(setup, *, api=None, pull_outcome=None):
    """Use the existing Setup owner's selected binding; no implicit settings read."""
    binding = setup.selected_binding()
    require(type(binding) is ManagerMetadataBinding, 'metadata_configuration_missing')
    return ExistingRunnerModelService(binding, api,pull_outcome=pull_outcome)
