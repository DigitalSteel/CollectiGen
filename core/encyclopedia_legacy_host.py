"""Fixed legacy launcher admission and one retained post-Close request.

The profile is build input, never supplied by a request. No entry arguments,
program paths or native evidence are accepted from the Manager payload.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import re
import os

from core.encyclopedia_connection import COMPONENT
from core.encyclopedia_legacy_handoff import prepare_legacy_reader_handoff
from release_contract import sha256_file
from suite_core.discovery.component_staging import (ComponentStageAdmission,
    ComponentStageBinding, component_stage_owner, guard, canonical, require)
from suite_core.discovery.component_release_signature import _json, _trust


@dataclass(frozen=True)
class CompiledLegacyReaderProfile:
    trust_bytes: bytes
    trust_sha256: str
    bootstrap_inventory_sha256: str

    def __post_init__(self):
        require(type(self.trust_bytes) is bytes
                and sha256(self.trust_bytes).hexdigest() == self.trust_sha256
                and type(self.bootstrap_inventory_sha256) is str
                and re.fullmatch('[0-9a-f]{64}',self.bootstrap_inventory_sha256),
                'compiled_legacy_reader_profile_required')
        _trust(self.trust_bytes)

    @property
    def identity(self):
        return sha256(canonical(dict(trust=self.trust_sha256,
            bootstrap_inventory=self.bootstrap_inventory_sha256))).hexdigest()

    def verify_bootstrap(self, root):
        inventory_path=root/'_legacy-reader/BOOTSTRAP_INVENTORY.json'
        guard(inventory_path)
        require(inventory_path.stat().st_size <= 1024*1024,'bounded_legacy_bootstrap_inventory_required')
        raw=inventory_path.read_bytes()
        require(sha256(raw).hexdigest()==self.bootstrap_inventory_sha256,
                'legacy_bootstrap_inventory_changed')
        inventory=_json(raw,'legacy bootstrap inventory')
        require(inventory.get('software_version')=='0.2.2-alpha.1'
                and inventory.get('platform')=={'os':'windows','architecture':'x86_64'}
                and type(inventory.get('files')) is list,'exact_legacy_bootstrap_inventory_required')
        names=set()
        for row in inventory['files']:
            name=row['path']
            require(type(name) is str and name not in names and name
                    and '\\' not in name and ':' not in name and not name.startswith('/')
                    and all(part not in ('','.','..') for part in name.split('/')),
                    'legacy_bootstrap_member_required')
            names.add(name)
            # Executable/runtime retain their original fixed layout. Original
            # documentation/notices are retained separately to avoid replacing
            # Manager's own README/LICENSE or top-level inventory.
            path=(root/name if name=='TagEncyclopedia.exe' or name.startswith('_internal/')
                  else root/'_legacy-reader'/name)
            guard(path)
            require(path.stat().st_size==row['size_bytes'] and sha256_file(path)==row['sha256'],
                    'legacy_bootstrap_file_changed:'+name)
        require('TagEncyclopedia.exe' in names and any(n.startswith('_internal/') for n in names),
                'complete_legacy_bootstrap_required')
        actual=set()
        for directory,dirs,files in os.walk(root/'_internal',followlinks=False):
            from pathlib import Path
            directory=Path(directory);guard(directory)
            for name in dirs:guard(directory/name)
            for name in files:
                path=directory/name;guard(path)
                actual.add(path.relative_to(root).as_posix())
        require(actual=={name for name in names if name.startswith('_internal/')},
                'legacy_bootstrap_runtime_members_changed')
        return sha256_file(root/'TagEncyclopedia.exe')


class LegacyReaderHost:
    """Only the original authenticated normal Root constructs this host."""
    def __init__(self, profile, owner, admitted, selection):
        from core.manager_launch_owner import DeploymentOwner
        require(type(profile) is CompiledLegacyReaderProfile and type(owner) is DeploymentOwner
                and selection is not None and selection.root==owner.root
                and admitted.layout.code_root==owner.root,'original_legacy_reader_host_required')
        self.profile,self.owner,self.admitted,self.selection=profile,owner,admitted,selection
        self.plan=None
        self.plan_id=None
        self.committed=False
        self.consumed=False
        self.created_authority=None

    def _admission(self, route):
        @contextmanager
        def hold(component):
            require(component==COMPONENT,'legacy_component_required')
            scope=self.owner.component_stage_scope(route,'begin',None)
            def check():
                require(self.owner.component_stage_scope(route,'check',scope['scope_id'])==scope,
                        'legacy_original_stage_changed')
            try:
                yield ComponentStageBinding(self.owner.root,
                    component_stage_owner(self.owner.root,self.owner.data_root,COMPONENT),check,self.owner.data_root)
            finally:
                self.owner.component_stage_scope(route,'release',scope['scope_id'])
        return ComponentStageAdmission(hold)

    def handle(self, route, request):
        require(route.owner is self.owner and route.reader_helper_selection is self.selection
                and route.state=='ready' and route._live_peer(), 'original_legacy_request_peer_required')
        operation=request['operation']
        if operation=='prepare':
            require((self.plan is None or self.consumed and not self.committed) and request['handoff_id'] is None,
                    'legacy_request_already_prepared')
            self.plan=None;self.plan_id=None;self.consumed=False
            from core.encyclopedia_legacy_handoff import LegacyReaderRefusal
            try:pin=self.profile.verify_bootstrap(self.owner.root)
            except (ValueError,OSError) as error:raise LegacyReaderRefusal(str(error)) from error
            plan=prepare_legacy_reader_handoff(self._admission(route),
                manager_settings_path=self.selection.settings_path,
                expected_selection=self.selection.selected,bootstrap_sha256=pin,
                trust_bytes=self.profile.trust_bytes,trust_sha256=self.profile.trust_sha256,
                admitted_manager_settings_path=self.admitted.settings_path)
            self.plan=plan
            self.consumed=False
            self.plan_id=sha256(canonical(dict(binding=plan.binding.identity,
                generation=self.owner.record['owner'],request_id=request['request_id']))).hexdigest()
        else:
            require(self.plan is not None and request['handoff_id']==self.plan_id and not self.consumed,
                    'original_legacy_handoff_required')
            if operation=='commit':
                self.plan.revalidate()
                self.profile.verify_bootstrap(self.owner.root)
                require(not self.committed,'legacy_handoff_already_committed')
                from core.encyclopedia_legacy_handoff import commit_legacy_selection_authority
                self.created_authority=commit_legacy_selection_authority(self._admission(route),self.plan,self.admitted.settings_path)
                self.committed=True
            elif operation=='cancel':
                from core.encyclopedia_legacy_handoff import cancel_legacy_selection_authority
                cancel_legacy_selection_authority(self._admission(route),self.created_authority)
                self.created_authority=None
                self.committed=False
                self.consumed=True
            else:
                require(operation=='status','closed_legacy_operation_required')
        return dict(status='ready',handoff_id=self.plan_id,committed=self.committed,consumed=self.consumed,reason=None)

    def take_after_close(self, launcher):
        require(not self.consumed and self.committed and self.plan is not None,
                'committed_legacy_handoff_required')
        require(launcher.owner is self.owner and launcher.normal_closure_receipt is not None
                and launcher.route.owner is self.owner and launcher.route.state=='closed'
                and launcher.route.physical_drain_acknowledged
                and launcher.normal_closure_receipt==self.owner._normal_receipt
                and launcher.route.closure_receipt==launcher.normal_closure_receipt
                and launcher.can_close() and self.owner.fd is None,
                'original_manager_complete_retirement_required')
        require(getattr(launcher,'normal_exit_code',None)==0,'normal_manager_exit_required')
        self.plan.revalidate()
        self.profile.verify_bootstrap(self.owner.root)
        self.consumed=True
        return self.plan
