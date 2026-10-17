"""Original transport's bounded stock hook endpoints and native member creator.

Existing DeploymentOwner and stock apply adapter own dispatch/lease/records.
This provider prepares listeners before dispatch, services the stock SDK hook
exchange and retains the atomically created updater. It supplies no generic
native permission receipt and cannot bypass existing qualification gates.
"""
from dataclasses import asdict
import json

from core.manager_launch_owner import LaunchHold, WindowsNormalTransport
from core.manager_native_member import WindowsMaintenanceMember
from core.manager_stock_hook_pipe import StockHookPipe, _api
from core.manager_stock_hook_admission import StockHookContext, StockHookOwnerSession


class WindowsStockHookProvider:
    def __init__(self, transport, request, recovery, admitted, *, environment):
        from core.manager_self_update_recovery import ManagerRecovery, digest
        if (type(transport) is not WindowsNormalTransport or transport.owner.native_transport is not transport
                or type(recovery) is not ManagerRecovery or recovery.paths['root'] != transport.owner.root
                or admitted.layout.code_root != transport.owner.root or admitted.layout.data_root != transport.owner.data_root
                or type(environment) is not dict):
            raise LaunchHold('original_deployment_stock_provider_required')
        self.transport, self.owner, self.request = transport, transport.owner, request
        self.api = _api(transport.api)
        self.job = transport.job
        self.recovery, self.admitted, self.environment = recovery, admitted, dict(environment)
        self.lease = (self.owner.fd, self.owner.record['owner'])
        journal = recovery.load()
        recovery.retained_context(journal)
        self.recovery_binding = digest(journal['binding'])
        self.pipes, self.contexts, self.sessions = {}, {}, {}
        self.member = None
        self.state = 'new'
        if getattr(transport, 'stock_hook_provider', None) is not None:
            raise LaunchHold('original_stock_provider_no_replay')
        transport.stock_hook_provider = self  # retain before first listener/output

    def _original(self, *, allow_pending=False):
        from core.manager_self_update_recovery import digest
        self.owner._maintenance_context(self.request, allow_pending=allow_pending)
        if (self.owner.native_transport is not self.transport or self.transport.stock_hook_provider is not self
                or self.transport.api is not self.api
                or self.transport.api is not getattr(self.transport, '_creation_api', None)
                or self.transport.job != self.job or self.job != getattr(self.transport, '_creation_job', None)
                or (self.owner.fd, self.owner.record['owner']) != self.lease
                or self.recovery.id != self.request.transaction_id
                or digest(self.recovery.load()['binding']) != self.recovery_binding):
            raise LaunchHold('original_stock_provider_binding_changed')

    def prepare(self):
        if self.state != 'new':
            raise LaunchHold('stock_listener_creation_no_replay')
        self._original()
        maintenance = self.owner.record['maintenance']
        if self.owner.record['phase'] != 'maintenance_hold' or maintenance['dispatch'] != 'prepared':
            raise LaunchHold('original_predispatch_stock_listeners_required')
        self.state = 'preparation_uncertain'
        for stage, version in (('--veloapp-obsolete', self.request.current_version),
                               ('--veloapp-updated', self.request.target_version)):
            pipe = StockHookPipe.fresh(api=self.api)
            self.pipes[stage] = pipe  # listener resources retained before kernel creation
            context = StockHookContext(self.request.transaction_id, self.request.attempt_identity_sha256,
                self.request.binding_sha256, pipe.name.rsplit('.', 1)[-1], stage, version).checked()
            self.contexts[stage] = context
            pipe.create_server()
        self.state = 'prepared'
        return self

    def locator_environment(self):
        self._original()
        if self.state != 'prepared' or any(pipe.state != 'listening' for pipe in self.pipes.values()):
            raise LaunchHold('original_precreated_stock_listeners_required')
        relative = self.admitted.settings_path.relative_to(self.owner.data_root).as_posix()
        descriptor = dict(schema=1, settings_relative=relative, bindings={stage:
            dict(pipe=pipe.name, context=asdict(self.contexts[stage])) for stage, pipe in self.pipes.items()})
        environment = dict(self.environment)
        environment['COLLECTIGEN_STOCK_HOOK_CONTEXT'] = json.dumps(descriptor, sort_keys=True, separators=(',', ':'))
        return environment

    def create_process(self, argv, **kwargs):
        """Bare single native birth primitive consumed by owner.queue_maintenance."""
        from core.manager_stock_apply import validate_argv
        self._original()
        validate_argv(self.request, argv)
        expected = dict(cwd=self.request.manager_root, shell=False, allow_elevation=False,
            transaction_id=self.request.transaction_id, binding_sha256=self.request.binding_sha256,
            lease_token=self.lease[1], attempt_identity_sha256=self.request.attempt_identity_sha256)
        if kwargs != expected or self.owner.record['maintenance']['dispatch'] != 'reserved' or self.member is not None:
            raise LaunchHold('single_reserved_original_stock_creation_required')
        environment = self.locator_environment()
        self.member = WindowsMaintenanceMember(self.transport, 'updater')
        self.state = 'creation_uncertain'
        process = self.member.create(argv, environment=environment, cwd=kwargs['cwd'])
        self.state = 'suspended'
        return process

    def resume_registered(self):
        self._original()
        if self.state != 'suspended' or self.owner.record['maintenance']['dispatch'] != 'created':
            raise LaunchHold('registered_original_stock_dispatch_required')
        self.member.resume_registered()
        self.state = 'running'

    def service_hooks(self, *, cancelled=lambda: False, timeout=5):
        """Two fresh stage channels; root ACK always precedes actual SDK callback."""
        if self.state != 'running':
            raise LaunchHold('single_running_stock_hook_pump_required')
        self.state = 'hooks_uncertain'
        for stage in ('--veloapp-obsolete', '--veloapp-updated'):
            self._original()
            pipe = self.pipes[stage]
            pipe.connect(timeout=timeout, cancelled=cancelled)
            session = StockHookOwnerSession(pipe, self.contexts[stage], transport=self.transport,
                request=self.request, recovery=self.recovery, admitted=self.admitted,
                job_source_handle=self.member.job_source_handle)
            self.sessions[stage] = session
            session.register()
            session.observe_callback()
        self.state = 'callbacks_observed'
        return dict(self.sessions)

    def observe_closed(self):
        """Actual exits and broken endpoint observations, not callback presence."""
        import ctypes
        from ctypes import wintypes as W
        self._original()
        if self.state != 'callbacks_observed' or self.member.process.poll() != 0:
            raise LaunchHold('successful_original_updater_exit_required')
        for stage, session in self.sessions.items():
            proof, code = self.owner._bound_native_exit(session.role)
            if code != 0 or session.state != 'callback_observed':
                raise LaunchHold('successful_original_stock_callback_exit_required')
            available = W.DWORD()
            if (self.api.PeekNamedPipe(self.pipes[stage].handle, None, 0, None, ctypes.byref(available), None)
                    or ctypes.get_last_error() not in (109, 233)):
                raise LaunchHold('original_stock_hook_endpoint_closure_required')
        if self.transport.job_active_count() != 0:
            raise LaunchHold('original_stock_job_not_closed')
        self.state = 'closed_observed'
        return True

    def verify_permission(self, owner, request):
        """Positive retained native closure/listener proof, never a presence flag."""
        self._original()
        if owner is not self.owner or request is not self.request:
            raise LaunchHold('original_stock_permission_binding_required')
        if self.state != 'prepared' or set(self.pipes) != {'--veloapp-obsolete', '--veloapp-updated'}:
            raise LaunchHold('original_precreated_stock_listeners_required')
        if any(not pipe.server or pipe.state != 'listening' or not pipe.handle or pipe.operation is not None
               or pipe.api is not self.api for pipe in self.pipes.values()):
            raise LaunchHold('original_stock_listener_resources_required')
        drain = owner._read_record(owner.record['maintenance']['drain'])
        for role in drain['children']:
            proof, code = owner._bound_native_exit(role)
            if code != 0 or proof.process is not owner.children[role]:
                raise LaunchHold('successful_original_native_drain_required')
        if self.transport.job_active_count() != 0:
            raise LaunchHold('original_stock_prebirth_job_not_empty')
        return True

    def bind_scope(self):
        self.verify_permission(self.owner, self.request)
        self.owner.scope_verifier = self.verify_scope
        return self

    def verify_scope(self, *, stage, owner, request, process=None, role=None):
        """Original native API/handles, immutable hook observations and Job count."""
        from core.manager_launch_owner import RetainedNativeProcess, _native_witness, _native_process_argv
        from core.manager_stock_apply import fixed_argv
        if stage == 'terminal_resources_closed':
            self.verify_retirement(owner, request)
            resource = getattr(self, 'resource_terminal', None)
            if (not getattr(self, 'terminal_resources_closed', False) or resource is None
                    or owner._normal_terminal is not resource or not resource.payload['resources'].get('job')
                    or self.transport.job is not None or self.transport.descriptors or self.transport.raw_endpoints
                    or self.transport.observation_handles
                    or any(child._handle or getattr(child,'thread',None) for child in owner.children.values())
                    or any(pipe.handle or pipe.operation for pipe in (*self.pipes.values(),*getattr(self,'repair_pipes',{}).values()))
                    or any(member.channel.reader is not None or member.channel.writer is not None
                        for member in getattr(self,'recovered_members',{}).values())
                    or self.successor_member.channel.reader is not None or self.successor_member.channel.writer is not None):
                raise LaunchHold('positive_original_terminal_resources_required')
            return dict(owner_token=owner.record['owner'], attempt_identity_sha256=request.attempt_identity_sha256,
                stage=stage, qualified=True, unknown_owners=0)
        self._original(allow_pending=True)
        if owner is not self.owner or request not in (self.request,getattr(self,'repair_request',None)):
            raise LaunchHold('original_stock_scope_binding_required')
        live = [child for child in owner.children.values() if child.poll() is None]
        live.extend(item['process'] for item in owner.pending if item['process'].poll() is None)
        if (len({id(child) for child in live}) != len(live)
                or any(type(child) is not RetainedNativeProcess or child.api is not self.api
                       or not child._handle or not self.transport.includes(child) for child in live)
                or self.transport.job_active_count() != len(live)):
            raise LaunchHold('original_stock_job_scope_incomplete')
        if stage == 'register_updater':
            member = self.repair_member if request is getattr(self,'repair_request',None) else self.member
            updater_role = owner._stock_updater_role(request)
            if member is None or member.process is not process or role != updater_role:
                raise LaunchHold('original_stock_member_required')
            witness = _native_witness(self.api, process._handle)
            if witness['executable_sha256'] != request.updater_sha256 or _native_process_argv(process) != fixed_argv(request):
                raise LaunchHold('actual_original_stock_updater_arguments_or_image_changed')
            owner._retain_verified_native_witness(role, process, self.api, witness)
        elif stage == 'register_hook':
            rows = [row for row in getattr(self.transport, '_stock_hook_observations', {}).values()
                    if row['process'] is process and row['role'] == role and row['lease'] == owner.fd
                    and row['binding_sha256'] == request.binding_sha256]
            if len(rows) != 1:
                raise LaunchHold('original_stock_hook_observation_required')
            owner._read_record(rows[0]['ref'])
        elif stage in {'register_replacement','register_recovered'}:
            member = self.successor_member if stage == 'register_replacement' else self.recovered_members[role]
            selection = (self.selection if stage == 'register_replacement' else self.recovered_selections[role]).revalidate()
            if member.process is not process or role not in {'replacement','restored','fresh_restored'}:
                raise LaunchHold('original_selected_successor_member_required')
            witness = _native_witness(self.api, process._handle)
            if (witness['executable_sha256'] != selection.executable_sha256
                    or _native_process_argv(process) != (str(selection.executable), *selection.admitted.restart_arguments)):
                raise LaunchHold('actual_selected_successor_arguments_or_image_changed')
            owner._retain_verified_native_witness(role, process, self.api, witness)
        elif stage in {'maintenance_closed', 'terminal_closed'}:
            if live or self.state not in {'closed_observed', 'successor_running', 'successor_closed','recovered_closed'}:
                raise LaunchHold('observed_original_stock_closure_required')
        return dict(owner_token=owner.record['owner'], attempt_identity_sha256=request.attempt_identity_sha256,
                    stage=stage, qualified=True, unknown_owners=0)

    def verify_recovery_gate(self, name, journal):
        if name == 'exclusive_owner' and getattr(self, 'terminal_resources_closed', False):
            ticket = self.owner._maintenance_terminal
            if (ticket is None or ticket['settle'] != self.settle_terminal_resources
                    or not ticket['resources_closed'] or self.transport.job is not None
                    or self.owner.fd != self.lease[0] or self.owner.record['owner'] != self.lease[1]):
                raise LaunchHold('positive_original_terminal_settlement_required')
            return True
        self._original(allow_pending=True)
        if name in {'prepare_admission', 'exclusive_owner'}:
            if self.transport.job_active_count() != 0:
                raise LaunchHold('original_stock_exclusive_native_scope_required')
        elif name in {'target_starting', 'target_healthy', 'target_failed_and_closed'}:
            process = self.owner.children.get('replacement')
            if process is None or not self.transport.includes(process):
                raise LaunchHold('original_stock_target_required')
            if name == 'target_failed_and_closed':
                binding, code = self.owner._bound_native_exit('replacement')
                if code == 0 or self.transport.job_active_count() != 0:
                    raise LaunchHold('actual_failed_and_closed_target_required')
            elif process.poll() is not None:
                raise LaunchHold('live_original_stock_target_required')
            if name == 'target_healthy' and getattr(getattr(self, 'successor_route', None), 'state', None) != 'ready':
                raise LaunchHold('actual_successor_readiness_required')
        elif name == 'recovery_admission':
            self.verify_prior_release(journal)
            binding, code = self.owner._bound_native_exit('replacement')
            if code == 0 or self.transport.job_active_count() != 0:
                raise LaunchHold('closed_failed_native_target_before_repair_required')
        elif name == 'prior_reopened_healthy_and_closed':
            if self.state != 'recovered_closed' or self.transport.job_active_count() != 0:
                raise LaunchHold('distinct_fresh_recovered_entries_closed_required')
            identities = []
            for role in ('restored','fresh_restored'):
                route = self.recovered_routes[role]
                health = self.owner.record['maintenance']['repair'].get('health',{}).get(role)
                if route.state != 'maintenance_ready' or health is None:
                    raise LaunchHold('actual_recovered_entry_health_and_cleanup_required')
                self.owner._read_record(health)
                binding, code = self.owner._bound_native_exit(role)
                if code != 0:raise LaunchHold('successful_recovered_entry_exit_required')
                identities.append(binding.identity())
                self.recovered_selections[role].revalidate()
            if identities[0] == identities[1] or self.recovered_members['restored'].process is self.recovered_members['fresh_restored'].process:
                raise LaunchHold('distinct_recovered_reopen_required')
        else:
            raise LaunchHold('original_native_recovery_gate_unqualified_' + name)
        return True

    def verify_prior_release(self, journal):
        """Stock signed retained A; the original trust pin does not rebase."""
        from hashlib import sha256
        from pathlib import Path
        from suite_core.discovery.component_release_signature import verify_component_release, verify_component_artifact, _json
        from suite_core.discovery.component_staging import MAX_METADATA_BYTES
        from suite_core.discovery.manager_runtime_detached_admission import MANAGER, PROFILE, produce_detached_inventory
        self.recovery.retained_context(journal)
        prior = journal['binding']['prior'];files = prior['files']
        def read(name):
            if files[name]['size'] > MAX_METADATA_BYTES:raise LaunchHold('prior_metadata_bounds')
            return Path(files[name]['path']).read_bytes()
        manifest,signature,trust,proof = (read(name) for name in ('manifest','signature','trust','proof'))
        if (sha256(trust).hexdigest() != self.request.trust_sha256
                or prior['version'] != self.request.current_version or prior['channel'] != self.request.channel):
            raise LaunchHold('original_prior_release_trust_and_version_required')
        verified = verify_component_release(manifest,signature,trust,expected_component=MANAGER,
            expected_version=prior['version'],expected_channel=prior['channel'],
            expected_os='windows',expected_architecture='x86_64')
        archive = Path(files['archive']['path'])
        verify_component_artifact(archive,verified)
        if (proof != produce_detached_inventory(archive,inventory_profile=PROFILE,
                expected_version=prior['version'],expected_channel=prior['channel'])
                or sha256(proof).hexdigest() != verified.artifact.portable_manifest_sha256):
            raise LaunchHold('original_signed_prior_inventory_required')
        rows={row['path']:row for row in _json(proof,'signed prior inventory')['files']}
        if (rows['CollectiGenManager.exe']['sha256']!=files['executable']['sha256']
                or rows['CollectiGenManager.exe']['sha256']!=self.request.current_executable_sha256
                or files['inventory']['sha256']!=sha256(proof).hexdigest()):
            raise LaunchHold('original_signed_prior_code_identity_required')
        return verified

    def prepare_repair(self, launch, *, explicitly_requested):
        from pathlib import Path
        from dataclasses import asdict
        from uuid import uuid4
        from core.manager_stock_apply import readback
        from core.manager_self_update_recovery import digest
        from suite_core.discovery.component_staging import guard
        import os
        self._original()
        if (explicitly_requested is not True or launch.owner is not self.owner
                or launch.state != 'update_repair_available' or hasattr(self,'repair_request')):
            raise LaunchHold('single_explicit_original_repair_required')
        journal = self.recovery.load()
        if journal['phase'] != 'startup_failed':raise LaunchHold('original_failed_recovery_before_repair_required')
        release = self.verify_prior_release(journal)
        files = journal['binding']['prior']['files']
        source = Path(files['archive']['path'])
        destination = Path(self.request.packages_dir) / source.name
        guard(destination)
        readback(source,release.artifact.size_bytes,release.artifact.sha256)
        if not destination.exists():
            with destination.open('xb') as output, source.open('rb') as stream:
                for block in iter(lambda:stream.read(1024*1024),b''):output.write(block)
                output.flush();os.fsync(output.fileno())
        readback(destination,release.artifact.size_bytes,release.artifact.sha256)
        fields = asdict(self.request)
        for key in ('durable_drain_sha256','durable_apply_intent_sha256','attempt_identity_sha256','original_owner_tokens'):
            fields.pop(key)
        fields.update(package=str(destination),package_size=release.artifact.size_bytes,package_sha256=release.artifact.sha256,
            current_version=self.request.target_version,target_version=self.request.current_version,
            current_executable_sha256=journal['binding']['target']['files']['executable']['sha256'],
            current_manifest_sha256=self.selection.manifest_sha256,release_sha256=release.manifest_sha256,
            inventory_sha256=release.artifact.portable_manifest_sha256,stale_token=uuid4().hex*2)
        self.repair_request = self.owner.prepare_repair_attempt(self.request, fields)
        self.repair_pipes,self.repair_contexts,self.repair_sessions = {},{},{}
        self.state = 'repair_preparation_uncertain'
        for stage,version in (('--veloapp-obsolete',self.repair_request.current_version),
                              ('--veloapp-updated',self.repair_request.target_version)):
            context = StockHookContext(self.request.transaction_id,self.repair_request.attempt_identity_sha256,
                self.repair_request.binding_sha256,uuid4().hex,stage,version)
            pipe = StockHookPipe(r'\\.\pipe\CollectiGen.StockHook.'+context.epoch,api=self.api)
            self.repair_contexts[stage],self.repair_pipes[stage] = context,pipe
            pipe.create_server()
        self.state = 'repair_prepared'
        return self.repair_request

    def repair_executor(self, document):
        """Called only by existing explicit recovery exact-downgrade intent."""
        from copy import deepcopy
        from core.manager_self_update_recovery import digest
        from core.manager_stock_apply import fixed_argv, readback
        self._original()
        journal = self.recovery.load()
        expected = dict(transaction=self.recovery.id,binding_sha256=digest(journal['binding']),
            deployment=self.recovery.binding,prior=journal['binding']['prior'],
            observed_current_version=self.request.target_version,allow_version_downgrade=True,
            full_only=True,deltas=[],required_is_downgrade=True)
        if self.state != 'repair_prepared' or journal['phase'] != 'apply_requested' or document != expected:
            raise LaunchHold('original_explicit_exact_downgrade_intent_required')
        request = self.repair_request
        readback(request.package,request.package_size,request.package_sha256)
        readback(request.update_exe,request.updater_size,request.updater_sha256)
        environment = dict(self.environment)
        environment['COLLECTIGEN_STOCK_HOOK_CONTEXT'] = json.dumps(dict(schema=1,
            settings_relative=self.admitted.settings_path.relative_to(self.owner.data_root).as_posix(),
            bindings={stage:dict(pipe=self.repair_pipes[stage].name,context=asdict(context))
                for stage,context in self.repair_contexts.items()}),sort_keys=True,separators=(',',':'))
        self.repair_member = WindowsMaintenanceMember(self.transport,'repair_updater')
        self.state = 'repair_creation_uncertain'
        next_record = deepcopy(self.owner.record);next_record['maintenance']['repair']['dispatch']='reserved'
        self.owner._persist(next_record)
        process = self.repair_member.create(fixed_argv(request),environment=environment,cwd=str(self.owner.root))
        self.owner._qualify_maintenance_child('repair_updater',process,request,'register_updater')
        next_record = deepcopy(self.owner.record);next_record['maintenance']['repair']['dispatch']='created'
        next_record['phase']='repair_applying';self.owner._persist(next_record)
        self.repair_member.resume_registered()
        for stage in ('--veloapp-obsolete','--veloapp-updated'):
            pipe = self.repair_pipes[stage];pipe.connect(timeout=5)
            session = StockHookOwnerSession(pipe,self.repair_contexts[stage],transport=self.transport,
                request=request,recovery=self.recovery,admitted=self.admitted,job_source_handle=self.repair_member.job_source_handle)
            self.repair_sessions[stage]=session
            session.register();session.observe_callback()
        if process.wait(timeout=5) != 0:raise LaunchHold('successful_original_repair_updater_exit_required')
        for session in self.repair_sessions.values():
            session.process.wait(timeout=5)
            binding,code=self.owner._bound_native_exit(session.role)
            if code != 0:raise LaunchHold('successful_original_repair_hook_exit_required')
        self._verify_repair_endpoint_closure()
        if self.transport.job_active_count()!=0:raise LaunchHold('original_repair_job_not_closed')
        self.state='repair_closed_observed'
        return dict(status='apply_requested',transaction=self.recovery.id,binding_sha256=document['binding_sha256'])

    def _verify_repair_endpoint_closure(self):
        """Exited callbacks cannot leave a connected or unsettled endpoint."""
        import ctypes
        from ctypes import wintypes as W
        if set(self.repair_pipes) != {'--veloapp-obsolete','--veloapp-updated'}:
            raise LaunchHold('original_repair_hook_endpoints_required')
        for pipe in self.repair_pipes.values():
            if pipe.operation is not None:
                raise LaunchHold('original_repair_hook_io_unsettled')
            available = W.DWORD()
            if (self.api.PeekNamedPipe(pipe.handle,None,0,None,ctypes.byref(available),None)
                    or ctypes.get_last_error() not in (109,233)):
                raise LaunchHold('original_repair_hook_endpoint_closure_required')

    def start_recovered(self, launch, *, fresh=False):
        """Direct signed A with distinct inherited channel; never public root."""
        from copy import deepcopy
        from core.manager_successor_admission import admit_successor_selection
        from suite_core.manager.bootstrap_entry import NormalOwnerRoute
        self._original()
        role = 'fresh_restored' if fresh else 'restored'
        expected = 'restored_closed' if fresh else 'repair_closed_observed'
        if type(fresh) is not bool or self.state != expected or launch.state != 'update_recovery_health_pending':
            raise LaunchHold('single_distinct_recovered_start_required')
        if not hasattr(self,'recovered_members'):
            self.recovered_members,self.recovered_routes,self.recovered_selections = {},{},{}
        argv = (str(self.selection.executable),*self.admitted.restart_arguments)
        selection = admit_successor_selection(self.owner,self.recovery,self.request,release_side='prior',argv=argv)
        member = WindowsMaintenanceMember(self.transport,role,normal_channel=True)
        self.recovered_members[role],self.recovered_selections[role] = member,selection
        self.state = role+'_creation_uncertain'
        process = member.create(argv,environment=self.environment,cwd=str(self.owner.root))
        self.owner._qualify_maintenance_child(role,process,self.repair_request,'register_recovered')
        next_record=deepcopy(self.owner.record);next_record['phase']='replacement_wait_health';self.owner._persist(next_record)
        route = NormalOwnerRoute(self.owner,member.channel,selection.admitted,
            manager_sha256=selection.executable_sha256,helper_pins={},successor_selection=selection,
            peer_role=role,successor_ready=lambda route:self.recovered_healthy(role,route))
        self.recovered_routes[role] = route
        member.resume_registered();self.state=role+'_running'
        return process

    def recovered_healthy(self, role, route):
        self._original()
        if route is not self.recovered_routes[role] or route.state != 'ready' or self.state != role+'_running':
            raise LaunchHold('original_recovered_normal_readiness_required')
        selection = self.recovered_selections[role].revalidate()
        def verify(owner,request,process):
            if (owner is not self.owner or request is not self.repair_request
                    or process is not self.recovered_members[role].process or process.poll() is not None):
                raise LaunchHold('live_signed_recovered_entry_required')
            selection.revalidate();self.recovery.check_data(self.recovery.load(),'before')
            return dict(status='recovered_entry_healthy',role=role,binding_sha256=request.binding_sha256,
                version=selection.version,data_readback_verified=True,
                executable_sha256=selection.executable_sha256,manifest_sha256=selection.manifest_sha256)
        return self.owner.recovered_entry_healthy(self.request,self.repair_request,role,verify_health=verify)

    def finish_recovered_close(self, *, fresh=False):
        self._original()
        role = 'fresh_restored' if fresh else 'restored'
        route = self.recovered_routes[role]
        if self.state != role+'_running' or route.state != 'local_accepted':
            raise LaunchHold('original_recovered_normal_cleanup_required')
        route.retain_for_maintenance()
        self.state = 'recovered_closed' if fresh else 'restored_closed'
        return True

    def run_explicit_repair(self, launch):
        """Existing worker supervises two visible A starts and same-owner retire."""
        self.prepare_repair(launch,explicitly_requested=True)
        launch.repair_update(explicitly_requested=True,executor=self.repair_executor)
        for fresh in (False,True):
            self.start_recovered(launch,fresh=fresh)
            role = 'fresh_restored' if fresh else 'restored'
            route = self.recovered_routes[role]
            while route.state != 'local_accepted':route.handle_next()
            self.finish_recovered_close(fresh=fresh)
        launch.verify_update_recovered()
        from core.manager_successor_admission import commit_ordinary_successor
        commit_ordinary_successor(self.recovered_selections['fresh_restored'],
            initial_contract_sha256=launch.initial_contract_sha256)
        return launch.retire_update(verify_terminal=self.verify_terminal,settle_resources=self.settle_terminal_resources)

    def start_successor(self, launch, argv):
        """Select signed B, register suspended child, offer fresh normal channel."""
        from core.manager_successor_admission import admit_successor_selection
        from suite_core.manager.bootstrap_entry import NormalOwnerRoute
        self._original()
        if self.state != 'closed_observed' or launch.owner is not self.owner:
            raise LaunchHold('observed_stock_closure_before_successor_required')
        if (launch.successor_trust_sha256 != self.request.trust_sha256
                or not getattr(launch,'initial_contract_sha256',None)):
            raise LaunchHold('compiled_ordinary_successor_anchor_required')
        self.selection = admit_successor_selection(self.owner, self.recovery, self.request,
            release_side='target', argv=argv)
        self.successor_member = WindowsMaintenanceMember(self.transport, 'replacement', normal_channel=True)
        self.state = 'successor_creation_uncertain'
        def admit(arguments, **context):
            if arguments != argv or context != dict(owner=self.owner, request=self.request):
                raise LaunchHold('original_selected_successor_arguments_required')
            return self.selection.revalidate().admitted
        def create(arguments, **options):
            self.selection.revalidate()
            return self.successor_member.create(arguments, environment=self.environment, **options)
        process = launch.start_update_target(argv, admit_entry=admit, create_process=create)
        self.successor_route = NormalOwnerRoute(self.owner, self.successor_member.channel,
            self.selection.admitted, manager_sha256=self.selection.executable_sha256,
            helper_pins={}, successor_selection=self.selection, peer_role='replacement',
            successor_ready=lambda route: self.successor_healthy(launch, route))
        self.successor_member.resume_registered()
        self.state = 'successor_running'
        return process

    def successor_healthy(self, launch, route):
        """Root owns health transition before fresh-entry readiness ACK."""
        self._original()
        if (launch.owner is not self.owner or route is not self.successor_route
                or route.state != 'ready' or self.state != 'successor_running'):
            raise LaunchHold('original_successor_normal_readiness_required')
        selection = self.selection.revalidate()
        def verify(owner, request, process, proof):
            if (owner is not self.owner or request is not self.request
                    or process is not self.successor_member.process
                    or process.poll() is not None):
                raise LaunchHold('live_selected_successor_required')
            selection.revalidate()
            self.recovery.check_data(self.recovery.load(), 'expected_b')
            return dict(status='replacement_healthy',target_version=selection.version,
                binding_sha256=request.binding_sha256,data_unchanged=True,
                current_executable_sha256=selection.executable_sha256,
                current_manifest_sha256=selection.manifest_sha256)
        result = launch.update_target_healthy(verify_health=verify)
        from core.manager_successor_admission import commit_ordinary_successor
        commit_ordinary_successor(selection,initial_contract_sha256=launch.initial_contract_sha256)
        return result

    def finish_successor_close(self):
        self._original()
        if self.state != 'successor_running' or self.successor_route.state != 'local_accepted':
            raise LaunchHold('accepted_successor_normal_cleanup_required')
        self.successor_route.retain_for_maintenance()
        self.state = 'successor_closed'
        return True

    def verify_terminal(self, owner, request, terminal):
        self.verify_retirement(owner, request)
        recovered = terminal['phase'] == 'recovery_verified'
        (self.recovered_selections['fresh_restored'] if recovered else self.selection).revalidate()
        self.recovery.check_data(terminal, 'before' if recovered else 'expected_b')
        for role in owner.children:
            binding, code = owner._bound_native_exit(role)
            expected_code = (self.owner._read_record(owner.record['maintenance']['repair']['drain'])['children']['replacement']['exit_code']
                if recovered and role == 'replacement' else 0)
            if code != expected_code or binding.process is not owner.children[role]:
                raise LaunchHold('successful_native_terminal_roles_required')
        return dict(transaction_id=request.transaction_id,binding_sha256=request.binding_sha256,
            phase=terminal['phase'],data_readback_verified=True,code_readback_verified=True,
            worker_output_closed=True,closed_roles=tuple(sorted(owner.children)))

    def verify_retirement(self, owner, request):
        if owner is not self.owner or request is not self.request:
            raise LaunchHold('original_stock_terminal_binding_required')
        if owner._maintenance_terminal is not None:
            ticket = owner._maintenance_terminal
            if ticket['settle'] != self.settle_terminal_resources or ticket['request'] is not request:
                raise LaunchHold('original_stock_terminal_retry_required')
            return True
        self._original()
        recovered = self.state == 'recovered_closed'
        route = self.recovered_routes['fresh_restored'] if recovered else self.successor_route if hasattr(self,'successor_route') else None
        if (self.state not in {'successor_closed','recovered_closed'} or route is None or route.state != 'maintenance_ready'
                or owner.record['phase'] != 'cleanup_hold' or owner.record['local_report'] is None
                or owner.record['drain'] is None or self.transport.job_active_count() != 0):
            raise LaunchHold('actual_successor_closed_terminal_required')
        return True

    def settle_terminal_resources(self, owner, request, proof):
        """Positive resource closes remembered by existing terminal machinery."""
        from core.manager_launch_owner import _NormalTerminalEvidence
        self.verify_retirement(owner, request)
        ticket = owner._maintenance_terminal
        if ticket is None or ticket['settle'] != self.settle_terminal_resources or ticket['phase'] not in {'healthy','recovery_verified'}:
            raise LaunchHold('original_healthy_resource_terminal_required')
        if not hasattr(self, 'resource_terminal'):
            if self.transport.job_active_count() != 0 or any(child.poll() is None for child in owner.children.values()):
                raise LaunchHold('original_closed_resource_snapshot_required')
            self.resource_terminal = _NormalTerminalEvidence(owner,dict(transport=self.transport,api=self.api,
                job=self.transport.job,children=dict(owner.children),
                handles={role:(child._handle,getattr(child,'thread',None)) for role,child in owner.children.items()},
                resources={}))
            if owner._normal_terminal is not None:
                raise LaunchHold('original_resource_snapshot_no_replacement')
            owner._normal_terminal = self.resource_terminal
        for pipe in self.pipes.values():
            pipe.close()
        for pipe in getattr(self,'repair_pipes',{}).values():pipe.close()
        self.successor_member.channel.close()
        for member in getattr(self,'recovered_members',{}).values():member.channel.close()
        self.transport.settle_normal_resources(self.resource_terminal)
        self.terminal_resources_closed = True
        return True
