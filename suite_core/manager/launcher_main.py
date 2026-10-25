"""Portable public entry using the one existing deployment/process owner.

Root package pin is supplied by the frozen entry's compiled build contract.
No discovery, update dispatch, helper enrollment, backend or implicit termination.
"""
from pathlib import Path
from copy import deepcopy
import argparse
import hashlib
import json
import os
import sys
import uuid

from core.training_source_io import _assert_unredirected

_HELD_PUBLIC_LAUNCHES = []
MAX_CONTRACT = 16384
# Normal-only candidate wiring is present; production/pretransfer remains held.
OWNED_HELPER_PRODUCTION_QUALIFIED = False


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('duplicate_public_package_key')
        result[key] = value
    return result


def load_contract(root, expected_sha256):
    path = root / 'MANAGER_PACKAGE.json'
    _assert_unredirected(path)
    with path.open('rb') as stream: raw = stream.read(MAX_CONTRACT + 1)
    if len(raw) > MAX_CONTRACT or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('reviewed_public_package_contract_required')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_json_pairs)
    required = {'schema', 'manager_version', 'manager_sha256', 'protocol',
                'minimum_bootstrap', 'maximum_bootstrap_exclusive', 'manager_relative'}
    if type(value) is not dict or set(value) not in (required,required|{'direct_runtime'}) or type(value['schema']) is not int or value['schema'] != 1:
        raise ValueError('public_package_schema')
    if 'direct_runtime' in value:
        reference=value['direct_runtime']
        if type(reference) is not dict or set(reference)!={'schema','relative','sha256'} or reference['schema']!=1 or reference['relative']!='_bootstrap/direct-runtime.json':
            raise ValueError('versioned_package_direct_reference_required')
    return value


def initial_settings(root, explicit=None):
    """Exclusive first-run creation; existing settings remain byte-for-byte ours untouched."""
    data = root / 'Data'
    path = Path(os.path.abspath(explicit)) if explicit is not None else data / 'Manager/settings.ini'
    if not path.is_relative_to(data) or path == data:
        raise ValueError('portable_manager_settings_scope')
    _assert_unredirected(path)
    if explicit is not None and not path.is_file():
        raise ValueError('existing_explicit_manager_settings_required')
    if explicit is None and not path.exists():
        _assert_unredirected(path.parent)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open('xb') as stream:
                stream.write(b'[General]\n'); stream.flush(); os.fsync(stream.fileno())
        except FileExistsError:
            pass  # Another first launch won exclusive creation; do not overwrite.
    _assert_unredirected(path)
    if not path.is_file(): raise ValueError('normal_manager_settings_file_required')
    return path


def parse_public_arguments(argv, root):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument('--manager-settings-path')
    parser.add_argument('--theme', choices=('system', 'dark', 'light', 'midnight'), default='system')
    parser.add_argument('--color-blind-mode', action='store_true')
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--owned-ollama-selection')
    parser.add_argument('--owned-ollama-selection-sha256')
    parser.add_argument('--owned-ollama-system-root')
    parser.add_argument('--allow-reviewed-owned-ollama',action='store_true')
    args = parser.parse_args(argv)
    selected=any((args.owned_ollama_selection,args.owned_ollama_selection_sha256,
                  args.owned_ollama_system_root,args.allow_reviewed_owned_ollama))
    if selected:
        if not OWNED_HELPER_PRODUCTION_QUALIFIED:
            raise ValueError('owned_helper_production_qualification_pending')
        if not all((args.owned_ollama_selection,args.owned_ollama_selection_sha256,
                    args.owned_ollama_system_root,args.allow_reviewed_owned_ollama)):
            raise ValueError('complete_explicit_owned_package_selection_required')
    if args.theme != 'system' or args.demo or args.color_blind_mode:
        raise ValueError('model_free_abort_entry_variant_unqualified')
    settings = initial_settings(root, args.manager_settings_path)
    full = ['--suite-mode', 'portable', '--suite-code-parent', str(root),
            '--suite-data-parent', str(root / 'Data'), '--manager-settings-path', str(settings),
            ]
    if selected:
        full.extend(('--owned-ollama-selection',args.owned_ollama_selection,
            '--owned-ollama-selection-sha256',args.owned_ollama_selection_sha256,
            '--owned-ollama-system-root',args.owned_ollama_system_root,'--allow-reviewed-owned-ollama'))
    return args, tuple(full)


class PublicLaunch:
    """Retain the same owner, native construction state and RPC route on failure."""
    def __init__(self, root, *, abort_action_admission=None, update_after_close=None, update_host_factory=None,
                 successor_trust_sha256=None):
        if abort_action_admission is not None and not callable(abort_action_admission):
            raise ValueError('trusted_root_abort_action_callable_required')
        if update_after_close is not None and not callable(update_after_close):
            raise ValueError('trusted_original_update_continuation_required')
        if update_host_factory is not None and not callable(update_host_factory):
            raise ValueError('trusted_update_host_factory_required')
        self.root, self.run_id = root, uuid.uuid4().hex
        self.owner = self.route = self.error = None
        self.state, self.report_paths = 'unstarted', []
        self.theme = 'system'
        self.acquired = False
        self.abort_scope = self.abort_ticket = self.abort_receipt = None
        self.original_argv = None
        self.unstarted_receipt = False
        self.normal_closure_receipt = self.recovery_error = None
        self.abort_action_admission = abort_action_admission
        self.preowner_import_error = None
        self.update_after_close = update_after_close
        self.successor_trust_sha256 = successor_trust_sha256
        self.update_service = self.update_prepared = self.update_request = self.update_recovery = None
        self.update_lease = None
        self.update_owner = self.update_recovery_gate = None
        self.update_terminal_receipt = None
        self.update_host_factory = update_host_factory

    def record(self, kind, **details):
        directory = self.root / 'Data/Manager/PublicLaunchReports'
        _assert_unredirected(directory); directory.mkdir(parents=True, exist_ok=True)
        path = directory / f'{self.run_id}-{len(self.report_paths):03d}-{kind}.json'
        _assert_unredirected(path)
        raw = json.dumps({'schema': 1, 'run_id': self.run_id, 'kind': kind, 'state': self.state,
                          **details}, sort_keys=True, allow_nan=False).encode('utf-8')
        if len(raw) > 65536: raise ValueError('bounded_public_launch_report')
        with path.open('xb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        if path.read_bytes() != raw: raise ValueError('public_report_readback')
        self.report_paths.append(path)

    def launch(self, argv, *, expected_package_sha256, environment):
        try:
            from core.manager_launch_owner import DeploymentOwner
            from suite_core.discovery.manager_bootstrap import (StableLaunchContract, stable_launch_plan,
                bind_stable_launch, stable_owner_binding)
            from .entry_binding import validate_normal_launch
            from .bootstrap_entry import NormalOwnerRoute
        except BaseException as error:
            # No owner construction/acquire has occurred. Do not fabricate an
            # owner receipt or enter Qt recovery for an unavailable import.
            self.preowner_import_error = self.error = error
            self.state = 'startup_unavailable'
            try:self.record('startup-unavailable', no_owner_created=True,
                reason=type(error).__name__+': '+str(error)[:256])
            except BaseException:pass
            return self
        try:
            contract = load_contract(self.root, expected_package_sha256)
            args, arguments = parse_public_arguments(argv, self.root)
            direct_selection=None
            if 'direct_runtime' in contract:
                from core.manager_direct_admission import load_direct_reference
                direct_selection=load_direct_reference(self.root,contract['direct_runtime'],observe_model=True)
                arguments=(*arguments,'--direct-runtime-reference',str(direct_selection.reference_path),
                    '--direct-runtime-reference-sha256',direct_selection.reference_sha256)
            from core.manager_successor_admission import ordinary_successor_contract, OrdinarySuccessorBinding
            self.initial_contract_sha256 = expected_package_sha256
            initial_contract = contract
            contract = ordinary_successor_contract(self.root,initial_contract,initial_contract_sha256=expected_package_sha256,
                expected_trust_sha256=self.successor_trust_sha256,
                settings_path=Path(arguments[arguments.index('--manager-settings-path')+1]))
            self.current_successor_selection = (OrdinarySuccessorBinding(self.root,initial_contract,
                expected_package_sha256,self.successor_trust_sha256,
                Path(arguments[arguments.index('--manager-settings-path')+1]),contract)
                if (self.root/'_bootstrap/manager-successor.json').exists() else None)
            self.active_manager_version = contract['manager_version']
            self.theme = args.theme
            compatibility = StableLaunchContract(contract['protocol'], contract['minimum_bootstrap'],
                contract['maximum_bootstrap_exclusive'], contract['manager_relative'])
            plan = stable_launch_plan(self.root, compatibility, manager_sha256=contract['manager_sha256'])
            admitted = bind_stable_launch(plan, arguments, validate_normal_launch=validate_normal_launch)
            from .entry_binding import selected_owned_package
            selection=selected_owned_package(admitted)
            from core.encyclopedia_connection import root_reader_selection
            try:
                reader_selection=root_reader_selection(admitted.layout.code_root,admitted.settings_path)
            except (ValueError,OSError) as error:
                # A stale optional connection must not block ordinary local work.
                reader_selection=None
                self.reader_selection_error=error
            self.owner = DeploymentOwner(**stable_owner_binding(plan, admitted))
            self.owner.acquire()
            self.acquired = True
            self.state = 'owned'
            from .organizer_acquisition_binding import root_selection_for_owner
            try:
                organizer_selection=root_selection_for_owner(self.owner,admitted)
            except (ValueError,OSError) as error:
                organizer_selection=None
                self.organizer_selection_error=error

            self.original_argv = (str(plan.manager_executable), *admitted.restart_arguments)
            if selection is not None and direct_selection is not None:
                raise ValueError('one_explicit_runtime_admission_required')
            if selection is None and direct_selection is None and reader_selection is None and organizer_selection is None:
                self.abort_scope = self.owner.arm_model_free_normal(self.original_argv)
            self.record('acquired', manager_version=contract['manager_version'],
                argv_sha256=self.owner.record['deployment']['entry_binding']['argv_sha256'])
            route_options={}
            if self.update_host_factory is not None:
                from core.manager_update_service import ManagerUpdateHostBinding
                host=self.update_host_factory(owner=self.owner,admitted=admitted,launcher=self)
                if type(host) is not ManagerUpdateHostBinding:raise ValueError('explicit_update_host_binding_required')
                route_options['manager_update_host']=host
            self.route = NormalOwnerRoute.spawn(self.owner, admitted, manager_executable=plan.manager_executable,
                manager_sha256=plan.manager_sha256, helper_pins={}, environment=environment,
                timeout=1, abort_scope=self.abort_scope, reader_selection=reader_selection, organizer_selection=organizer_selection, **route_options)
            self.route.owned_helper_selection=selection
            self.route.direct_helper_selection=direct_selection
            self.state = 'running'
            self.record('spawned', manager=self.route.wire.document['entry'])
            while self.route.state != 'local_accepted':
                self.route.handle_next()  # Native empty idle is retryable; partial/EOF holds.
            self.state = 'local_accepted'
            self.record('local-accepted', seal=self.route.seal)
            code = self.owner.children['manager'].wait(timeout=5)
            if self.update_after_close is not None:
                drain = self.route.retain_for_maintenance(timeout=1)
                self.state = 'maintenance_ready'
                self.record('maintenance-drained', child_exit_code=code, drain_sha256=drain)
                # Trusted original host only. No CLI switch/default apply and no
                # new owner or lost-lease continuation. Return retains supervision.
                if self not in _HELD_PUBLIC_LAUNCHES: _HELD_PUBLIC_LAUNCHES.append(self)
                self.update_after_close(self)
                if self.can_close():
                    if self in _HELD_PUBLIC_LAUNCHES:_HELD_PUBLIC_LAUNCHES.remove(self)
                    return code
                return self
            receipt = self.route.finish_physical_drain(timeout=1)
            self.normal_closure_receipt = receipt
            self.state = 'closed'
            self.record('closed', child_exit_code=code, closure_receipt_sha256=receipt)
            return code
        except BaseException as error:
            if self.owner is None and not self.acquired:
                # Explicit production/argument rejection occurred before the
                # inert owner constructor. No lifecycle actor needs a hold UI.
                self.preowner_import_error = self.error = error
                self.state = 'startup_unavailable'
                try:self.record('startup-unavailable',no_owner_created=True,
                    reason=type(error).__name__+': '+str(error)[:256])
                except BaseException:pass
                return self
            self.error, self.state = error, 'held'
            if self not in _HELD_PUBLIC_LAUNCHES: _HELD_PUBLIC_LAUNCHES.append(self)
            try: self.record('held', reason=type(error).__name__ + ': ' + str(error)[:256])
            except BaseException: pass  # Diagnostic failure never discards original authority.
            if self.route is None or (getattr(self.route,'owned_helper_selection',None) is None
                    and getattr(self.route,'direct_helper_selection',None) is None
                    and getattr(self.route,'reader_helper_selection',None) is None):
                self._wait_only_abort(timeout=1)
            return self

    def _update_owned(self, *, allow_pending=False):
        from core.manager_launch_owner import DeploymentOwner
        if type(self.owner) is not DeploymentOwner or not self.acquired:
            raise ValueError('original_update_owner_required')
        self.owner._owned(allow_pending=allow_pending)
        if self.owner.root != self.root or (self.update_owner is not None and self.owner is not self.update_owner):
            raise ValueError('same_original_update_owner_required')
        if self.update_lease is not None and (self.owner.fd, self.owner.record['owner']) != self.update_lease:
            raise ValueError('original_continuous_update_lease_required')
        if self.update_recovery_gate is not None and self.update_recovery.gates is not self.update_recovery_gate:
            raise ValueError('original_update_recovery_gate_changed')

    def review_update_for_close(self, route, prepared, request):
        """Authenticated Apply review; reserve exact stage for post-drain host.

        This is permission to enter normal cleanup, never native apply permission.
        The retained continuation must still prepare/admit the original owner,
        transaction, listeners and native stock attempt after physical drain.
        """
        from core.manager_update_service import PreparedManagerUpdate, ManagerUpdateHostBinding, reviewed_stage_identity
        self._update_owned()
        if (route is not self.route or route.owner is not self.owner or route.state != 'ready'
                or self.state != 'running' or type(prepared) is not PreparedManagerUpdate
                or not callable(self.update_after_close)
                or getattr(self, 'reviewed_update_for_close', None) is not None
                or self.update_request is not None):
            return dict(status='held',result=dict(reason='original_post_drain_update_continuation_required'))
        if request['operation'] != 'apply' or request['profile_sha256'] != route.manager_update_profile_sha256:
            raise ValueError('original_apply_review_profile_required')
        host = getattr(route.manager_update_handler, '__self__', None)
        if (type(host) is not ManagerUpdateHostBinding or host.apply_review != self.review_update_for_close
                or not route._live_peer() or request['body'] != reviewed_stage_identity(prepared)):
            raise ValueError('original_root_live_signed_apply_review_required')
        host.service.revalidate(prepared)
        # Host handler already independently rediscovered/re-admitted these bytes.
        # Retain exact identity before sending ACK; no new transaction writes here.
        self.reviewed_update_for_close = (route, prepared, request['request_id'], self.owner.fd,
            self.owner.record['owner'])
        return dict(status='reviewed',result=dict(action='close_for_apply'))

    def prepare_update(self, service, prepared, fields, *, records, scope_verifier,
                       transaction_verifier, recovery, should_cancel=None):
        """Bind existing staged bytes/recovery to this launcher's original drain.

        Host retains signed A/B packets and prepares ManagerRecovery first.
        No new updater, journal, credentials or implicit recovery is created.
        """
        from core.manager_update_service import ManagerUpdateService, PreparedManagerUpdate
        from core.manager_self_update_recovery import ManagerRecovery, digest
        from core import manager_stock_apply
        self._update_owned()
        if (self.state != 'maintenance_ready' or self.route is None
                or self.route.owner is not self.owner or self.route.state != 'maintenance_ready'
                or self.update_request is not None):
            raise ValueError('single_original_update_drain_required')
        if type(service) is not ManagerUpdateService or type(prepared) is not PreparedManagerUpdate:
            raise ValueError('exact_staged_update_service_required')
        if type(recovery) is not ManagerRecovery or recovery.paths['root'] != self.root:
            raise ValueError('same_deployment_recovery_required')
        if should_cancel and should_cancel():
            raise InterruptedError('Update cancelled before durable apply intent; original lease retained.')
        plan = service.revalidate(prepared)
        expected = dict(current_version=service.current_version, target_version=plan.version,
            channel=plan.channel, package_sha256=plan.artifact_sha256,
            package_size=prepared.offer.release.artifact.size_bytes,
            release_sha256=plan.release_sha256, trust_sha256=plan.trust_sha256,
            inventory_sha256=plan.inventory_sha256)
        if any(fields.get(name) != pin for name, pin in expected.items()):
            raise ValueError('stock_request_stage_binding')
        # Pin the existing transaction and retained full A/B artifacts before
        # committing the owner's stock intent. JSON alone is never authority.
        journal = recovery.load()
        recovery.retained_context(journal)
        binding = journal['binding']
        if (journal['phase'] != 'prepared' or recovery.id != fields.get('transaction_id')
                or binding['prior']['version'] != service.current_version
                or binding['target']['version'] != plan.version
                or binding['target']['files']['archive']['sha256'] != plan.artifact_sha256
                or binding['target']['channel'] != plan.channel):
            raise ValueError('exact_prepared_recovery_stage_required')
        # The promoted package must still be the exact signed reviewed bytes.
        service.revalidate(prepared, artifact=fields['package'])
        if should_cancel and should_cancel():
            raise InterruptedError('Update cancelled before durable apply intent; original lease retained.')
        self.update_lease = (self.owner.fd, self.owner.record['owner'])
        self.update_owner = self.owner
        original_gate = recovery.gates
        recovery_binding = digest(binding)
        transaction_id = recovery.id
        def gate(name, document):
            with self.update_owner.dispatch_lock:
                # Loading immutable transaction context must remain possible
                # while a native hook copy is retained pending qualification.
                # Every execution/mutation/health gate still requires settlement.
                self._update_owned(allow_pending=name == 'transaction_authority')
                if (document.get('transaction') != transaction_id
                        or recovery.id != transaction_id or recovery.paths['root'] != self.root
                        or digest(document.get('binding')) != recovery_binding):
                    raise ValueError('original_update_recovery_binding_changed')
                # A synthetic callback cannot qualify actual production hooks.
                if self.owner.native_transport is not None and name != 'transaction_authority':
                    from core.manager_stock_hook_provider import WindowsStockHookProvider
                    provider = getattr(self.owner.native_transport, 'stock_hook_provider', None)
                    if type(provider) is not WindowsStockHookProvider:
                        raise ValueError('stock_hook_transport_unqualified')
                    provider.verify_recovery_gate(name, document)
                result = original_gate(name, document)
                self._update_owned(allow_pending=name == 'transaction_authority')
                return result
        recovery.gates = gate
        self.update_recovery_gate = gate
        self.update_service, self.update_prepared, self.update_recovery = service, prepared, recovery
        self.route.manager_update_recovery = recovery
        self.state = 'update_preparing'
        try:
            self.update_request = self.owner.prepare_maintenance(manager_stock_apply, fields,
                records=records, scope_verifier=scope_verifier, transaction_verifier=transaction_verifier)
        except BaseException as error:
            self.error, self.state = error, 'update_held'
            raise
        self.state = 'update_prepared'
        return self.update_request

    def queue_update(self, *, native_review, create_process, should_cancel=None):
        self._update_owned()
        if self.state != 'update_prepared' or self.update_request is None:
            raise ValueError('single_prepared_update_required')
        if should_cancel and should_cancel():
            raise InterruptedError('Update cancelled before dispatch; original lease and rollback retained.')
        try:
            with self.update_owner.dispatch_lock:
                self._update_owned()
                self.update_recovery.authorize_apply()
                result = self.update_service.queue_owned(self.update_prepared, self.owner,
                    self.update_request, native_review=native_review, create_process=create_process)
            self.state = 'update_applying'
            return result  # Process creation is not installed/healthy success.
        except BaseException as error:
            self.error, self.state = error, 'update_held'
            raise

    def repair_update(self, *, explicitly_requested, executor):
        self._update_owned()
        if self.update_request is None or self.update_recovery is None:
            raise ValueError('original_update_recovery_required')
        try:
            def original_executor(request):
                self._update_owned()
                result = executor(request)
                self._update_owned()
                return result
            with self.update_owner.dispatch_lock:
                self._update_owned()
                result = self.update_recovery.repair(explicitly_requested=explicitly_requested,
                    executor=original_executor if callable(executor) else executor)
            self.state = 'update_recovery_health_pending'
            return result
        except BaseException as error:
            self.error, self.state = error, 'update_held'
            raise

    def finish_update(self, *, verify_payload):
        """Observe actual stock completion and code/data readback, not SDK return."""
        self._update_owned()
        if self.state != 'update_applying':
            raise ValueError('original_apply_observation_required')
        try:
            with self.update_owner.dispatch_lock:
                result = self.owner.finish_apply(self.update_request, verify_payload=verify_payload)
            self.state = 'update_replacement_ready'
            return result
        except BaseException as error:
            self.error, self.state = error, 'update_held'
            raise

    def start_update_target(self, argv, *, admit_entry, create_process):
        self._update_owned()
        if self.state != 'update_replacement_ready':
            raise ValueError('verified_replacement_required')
        try:
            with self.update_owner.dispatch_lock:
                process = self.owner.spawn_replacement(self.update_request, argv,
                    admit_entry=admit_entry, create_process=create_process)
                self.update_recovery.starting(dict(owner_token=self.owner.record['owner'],
                    attempt_identity_sha256=self.update_request.attempt_identity_sha256,
                    original_process=deepcopy(self.owner.record['children']['replacement'])))
            self.state = 'update_target_health_pending'
            return process
        except BaseException as error:
            self.error, self.state = error, 'update_held'
            raise

    def update_target_failed(self):
        self._update_owned()
        if self.state != 'update_target_health_pending':
            raise ValueError('original_target_observation_required')
        try:
            with self.update_owner.dispatch_lock:
                process = self.owner.children.get('replacement')
                if process is None or process.poll() is None:
                    raise ValueError('original_failed_target_closure_required')
                result = self.update_recovery.startup_failed()
            self.state = 'update_repair_available'
            if self.error is None:
                self.error = RuntimeError('Updated Manager exited before verified startup. Its original recovery transaction is retained.')
            return result
        except BaseException as error:
            self.error, self.state = error, 'update_held'
            raise

    def update_target_healthy(self, *, verify_health):
        self._update_owned()
        if self.state != 'update_target_health_pending':
            raise ValueError('original_target_observation_required')
        try:
            with self.update_owner.dispatch_lock:
                self.owner.replacement_healthy(self.update_request, verify_health=verify_health)
                result = self.update_recovery.healthy()
            self.state = 'update_target_healthy'
            return result
        except BaseException as error:
            self.error, self.state = error, 'update_held'
            raise

    def verify_update_recovered(self):
        self._update_owned()
        if self.update_request is None or self.update_recovery is None:
            raise ValueError('original_update_recovery_required')
        try:
            with self.update_owner.dispatch_lock:
                self._update_owned()
                result = self.update_recovery.verify_recovered()
            self.state = 'update_recovery_verified'
            return result  # Retirement still requires final native scope closure.
        except BaseException as error:
            self.error, self.state = error, 'update_held'
            raise

    def retire_update(self, *, verify_terminal, settle_resources):
        self._update_owned()
        if (self.state not in {'update_target_healthy','update_recovery_verified'}
                and not (self.state=='update_held' and self.owner._maintenance_terminal is not None)):
            raise ValueError('verified_update_terminal_required')
        try:
            with self.update_owner.dispatch_lock:
                terminal=self.update_recovery.load()
                # Existing recovery slot is retired only under the original
                # guarded exclusive-owner gate; pending recovery cannot retire.
                receipt=self.owner.retire_maintenance(self.update_request,terminal=terminal,
                    verify_terminal=verify_terminal,settle_resources=settle_resources,
                    retire_transaction=self.update_recovery.retire)
            self.update_terminal_receipt=receipt;self.state='update_closed'
            if self in _HELD_PUBLIC_LAUNCHES:_HELD_PUBLIC_LAUNCHES.remove(self)
            return receipt
        except BaseException as error:
            self.error, self.state = error, 'update_held'
            raise

    def _wait_only_abort(self, *, timeout):
        from core.manager_launch_owner import NormalAbortedReceipt
        if not self.acquired:
            return False
        if self.route is not None and self.route.state in {'local_accepted', 'closed', 'maintenance_ready'}:
            return False  # Genuine accepted normal drain keeps its separate route.
        try:
            if self.owner.native_transport is None:
                if self.original_argv is None:
                    return False
                # Absence is only routing, never proof. The original owner's
                # positive creation gate and complete argv decide admission.
                receipt = self.owner.finish_unstarted_normal(self.original_argv, 'startup_failure')
                scope = self.owner._unstarted_scope
                expected = self.owner._normal_receipt
                unstarted = True
            else:
                if self.abort_scope is None:
                    if self.route is not None or self.state!='held':return False
                    self.abort_scope=self.owner.arm_failed_unready_normal(self.original_argv)
                if self.abort_ticket is None:
                    self.abort_ticket = self.owner.begin_normal_abort('normal_startup_failed')
                receipt = self.owner.finish_normal_abort(self.abort_ticket, timeout=timeout)
                scope = self.abort_scope
                expected = self.owner._abort_receipt
                unstarted = False
            if (type(receipt) is not NormalAbortedReceipt or expected is not receipt or scope is None
                    or receipt.owner_token != self.owner.record['owner']
                    or receipt.attempt != scope.attempt or receipt.outcome != 'normal_aborted'
                    or self.owner.fd is not None):
                raise ValueError('typed_same_owner_abort_receipt_required')
            self.unstarted_receipt = unstarted
            self.abort_receipt, self.state = receipt, 'aborted'
            try: self.record('aborted', attempt=receipt.attempt, closure_receipt_sha256=receipt.receipt_sha256)
            except BaseException: pass  # Diagnostic failure cannot undo verified physical closure.
            return True
        except BaseException as error:
            self.recovery_error = error
            return False

    def request_explicit_abort(self):
        if self.abort_ticket is None or self.abort_action_admission is None:
            return False
        try:
            self.owner.abort_original_manager(self.abort_ticket, review_action=self.abort_action_admission)
        except BaseException as error:
            self.recovery_error = error
            return False
        return self._wait_only_abort(timeout=.1)

    def can_close(self):
        if getattr(self,'update_worker',None) is not None and self.update_worker.close_held:
            return False
        if not self.acquired: return True  # No owned launch was started.
        if self.update_terminal_receipt is not None:
            from core.manager_launch_owner import MaintenanceClosedReceipt
            return (type(self.update_terminal_receipt) is MaintenanceClosedReceipt
                and self.update_terminal_receipt is self.owner._maintenance_receipt
                and self.state=='update_closed' and self.owner.fd is None)
        if self.abort_receipt is not None:
            from core.manager_launch_owner import NormalAbortedReceipt
            return (type(self.abort_receipt) is NormalAbortedReceipt
                    and (self.owner._normal_receipt if self.unstarted_receipt else self.owner._abort_receipt)
                        is self.abort_receipt and self.owner.fd is None)
        if self.normal_closure_receipt is None: return False
        # The accepted normal route has already verified exits and retired its
        # native handles. Querying those closed handles would invent a new hold.
        return (self.owner.fd is None and self.route is not None and self.route.state == 'closed')

    def recheck(self):
        if self.can_close():return True  # Never re-query retired native handles.
        # Only an already accepted local report permits the existing final drain.
        if self.route is not None and self.route.state == 'local_accepted' and self.abort_ticket is None:
            try:
                self.normal_closure_receipt = self.route.finish_physical_drain(timeout=.1)
                self.state = 'closed'
                self.record('closed-after-wait')
            except BaseException as error: self.error = error
        elif self.abort_receipt is None: self._wait_only_abort(timeout=.1)
        return self.can_close()

    def present(self):
        """Stock Qt guarded failure presentation; no new child/process authority."""
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout, QLabel, QPushButton, QGroupBox
        from qt_app.theme import apply_theme
        app = QApplication.instance() or QApplication(sys.argv[:1])
        apply_theme(app, self.theme, False)
        held = self
        class GuardedDialog(QDialog):
            def closeEvent(self, event):
                if held.can_close(): event.accept()
                else: event.ignore()
            def reject(self): self.close()
        dialog = GuardedDialog(); dialog.setWindowTitle('CollectiGen startup needs review')
        dialog.resize(660, 300)
        layout = QVBoxLayout(dialog)
        label = QLabel('Manager could not complete startup. Its original startup state is retained. '
            'Keep this window open while closure is unresolved. No runner or helper is stopped automatically.')
        label.setWordWrap(True); layout.addWidget(label)
        recorded_reason = type(self.error).__name__ + ': ' + str(self.error)[:256]
        status = QLabel('Checking whether Manager can finish recovery. Keep this review open until closure is confirmed.')
        status.setObjectName('startupRecoveryStatus'); status.setWordWrap(True); status.setTextFormat(Qt.PlainText)
        layout.addWidget(status)
        reason = QLabel(recorded_reason); reason.setWordWrap(True); reason.setTextFormat(Qt.PlainText)
        reason.setObjectName('startupRecoveryReason')
        details=QGroupBox('Details');details.setCheckable(True);details.setChecked(False)
        details_layout=QVBoxLayout(details);details_layout.addWidget(reason);reason.hide()
        details.toggled.connect(reason.setVisible);layout.addWidget(details)
        location = None
        if self.report_paths:
            location = QLabel('Startup details: ' + str(self.report_paths[-1])); location.setWordWrap(True)
            details_layout.addWidget(location);location.hide();details.toggled.connect(location.setVisible)
        check = QPushButton('Check closure'); close = QPushButton('Close')
        check.setObjectName('startupRecoveryCheck'); close.setObjectName('startupRecoveryClose')
        def refresh():
            try: confirmed = held.recheck()
            except BaseException as error:
                held.error = error
                confirmed = held.can_close()
            close.setEnabled(confirmed)
            if confirmed:
                if held.state == 'update_closed':
                    status.setText('Recovery verified and all owned resources closed. Choose Close to finish.')
                    label.setText('The prior Manager reopened successfully in a distinct fresh process. Recovery evidence is retained.')
                    reason.setText('Original update failure: ' + recorded_reason)
                else:
                    status.setText('Closure confirmed. Choose Close to finish this failed startup review.')
                    label.setText('Startup failed; its original process scope has now closed. No runner is stopped by closing this review.')
                    reason.setText('Recorded startup failure: ' + recorded_reason)
            else:
                status.setText('Manager recovery is still unresolved. Review Details if it cannot finish cleanup.')
                reason.setText(type(held.error).__name__ + ': ' + str(held.error)[:256])
            if location is not None and held.report_paths:
                location.setText('Startup details: ' + str(held.report_paths[-1]))
        check.clicked.connect(refresh); close.clicked.connect(dialog.close)
        close.setEnabled(self.can_close()); layout.addWidget(check); layout.addWidget(close)
        if self.state == 'update_repair_available':
            from core.manager_stock_hook_provider import WindowsStockHookProvider
            from .resource_executor import ResourceExecutorOwner
            provider = getattr(self.owner.native_transport,'stock_hook_provider',None)
            if type(provider) is WindowsStockHookProvider and provider.owner is self.owner:
                dialog.resize(660,460)
                status.setText('Repair is available for this exact failed update. The deployment remains held until recovery and closure are verified.')
                label.setText('The updated Manager failed to start. The original launcher still holds this deployment. '
                    'Repair restores the verified prior Manager and its declared settings backup. '
                    'The failed update and recovery evidence stay available.')
                consequence = QLabel('Choose Repair once. Close each restored Manager window when finished; '
                    'the launcher opens a distinct fresh Manager to verify recovery. Keep this review open until recovery and closure are confirmed.')
                consequence.setWordWrap(True);layout.addWidget(consequence)
                repair = QPushButton('Repair to prior Manager');repair.setObjectName('managerUpdateRepair')
                layout.addWidget(repair)
                self.update_worker = ResourceExecutorOwner()
                self.update_repair_future = None
                def request_repair():
                    repair.setEnabled(False);check.setEnabled(False);close.setEnabled(False)
                    status.setText('Repair requested. The original launcher is verifying and restoring this exact transaction.')
                    held.update_repair_future = held.update_worker.submit(provider.run_explicit_repair,held)
                    repair_timer.start()
                def poll_repair():
                    future = held.update_repair_future
                    if future is None or not future.done() or held.update_worker.close_held:return
                    repair_timer.stop();check.setEnabled(True)
                    try:
                        result=future.result()
                        if result is not held.update_terminal_receipt or not held.can_close():
                            raise RuntimeError('original_recovery_terminal_receipt_required')
                        status.setText('Recovery verified and all owned resources closed. Choose Close to finish.')
                        reason.setText('Prior Manager reopened successfully in a distinct fresh process.')
                    except BaseException as error:
                        held.error = error
                        status.setText('Repair is held. Retained recovery evidence requires review before another action.')
                        reason.setText(type(error).__name__+': '+str(error)[:256])
                    close.setEnabled(held.can_close())
                repair_timer = QTimer(dialog);repair_timer.setInterval(50);repair_timer.timeout.connect(poll_repair)
                repair.clicked.connect(request_repair)
        if self.abort_action_admission is not None:
            consequence = QLabel('Abort ends this exact failed Manager process. Unsaved changes may be lost. '
                'This action does not stop a runner, external instance or process tree.')
            consequence.setWordWrap(True); layout.addWidget(consequence)
            abort = QPushButton('Abort this Manager startup')
            def act():
                held.request_explicit_abort(); close.setEnabled(held.can_close())
            abort.clicked.connect(act)
            abort.setEnabled(self.abort_ticket is not None and self.abort_receipt is None)
            layout.addWidget(abort)
        self.dialog, self.app = dialog, app
        route=self.route
        if (route is not None and (getattr(route,'owned_helper_selection',None) is not None
                or getattr(route,'direct_helper_selection',None) is not None)
                and route.state=='held' and not self.can_close()):
            from .resource_executor import ResourceExecutorOwner
            self.recovery_observer=ResourceExecutorOwner()
            self.recovery_observation=self.recovery_observer.submit(route.observe_recovery_ui,1)
            observing=QTimer(dialog);observing.setInterval(100)
            def observe():
                future=held.recovery_observation
                if not future.done() or held.recovery_observer.close_held:return
                observing.stop();held.recovery_observer.begin_close()
                try:visible=future.result()
                except BaseException as observation_error:
                    held.recovery_observation_error=observation_error;visible=None
                if visible is not None:
                    # Exact child owns the only visible surface; root retains its
                    # original hold and remains in this Qt supervision loop.
                    dialog.hide()
                else:dialog.show()
            observing.timeout.connect(observe);observing.start()
            QTimer.singleShot(0,dialog.hide)
        dialog.exec()
        if not self.can_close(): raise RuntimeError('public_launch_recovery_still_held')
        return 1


def main(argv=None, *, expected_package_sha256=None, executable=None, abort_action_admission=None,
         update_host_factory=None, update_after_close=None, successor_trust_sha256=None,
         compiled_update_profile=None):
    if expected_package_sha256 is None: raise ValueError('compiled_public_package_pin_required')
    if compiled_update_profile is not None:
        from core.manager_update_host import CompiledManagerUpdateProfile
        if (type(compiled_update_profile) is not CompiledManagerUpdateProfile
                or update_host_factory is not None or update_after_close is not None):
            raise ValueError('single_explicit_compiled_update_profile_required')
        compiled_update_profile.check()
        update_host_factory=compiled_update_profile.root_host
        successor_trust_sha256=compiled_update_profile.trust_sha256
    exe = Path(sys.executable if executable is None else executable).absolute()
    _assert_unredirected(exe)
    if exe.name != 'CollectiGen.exe' or not exe.is_file(): raise ValueError('exact_public_root_entry_required')
    if not (exe.parent / '_bootstrap').is_dir(): raise ValueError('public_bootstrap_payload_required')
    controller = PublicLaunch(exe.parent, abort_action_admission=abort_action_admission,
        update_host_factory=update_host_factory,update_after_close=update_after_close,
        successor_trust_sha256=successor_trust_sha256)
    result = controller.launch(tuple(sys.argv[1:] if argv is None else argv),
        expected_package_sha256=expected_package_sha256, environment=dict(os.environ))
    if (result is controller and controller.preowner_import_error is not None
            and controller.owner is None and not controller.acquired):
        return 1  # Handled pre-owner diagnostic; natural exit, no frozen error dialog.
    return result.present() if type(result) is PublicLaunch else result
