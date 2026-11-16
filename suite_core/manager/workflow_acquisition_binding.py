"""Generate/Train adapters over the original signed component update transaction."""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import base64
import json
import re

from core.component_acquisition import ExactComponentOffer
from core.workflow_connection import workflow_component
from suite_core.discovery.component_payload_policy import FROZEN_PAYLOAD_POLICY as POLICY


@dataclass(frozen=True)
class CompiledWorkflowAcquisitionProfile:
    component_id: str
    trust_bytes: bytes
    trust_sha256: str
    asset_redirect_origins: tuple[str, ...]
    transport_qualification_sha256: str
    expected_offer: ExactComponentOffer
    channel: str = 'beta'
    max_artifact_bytes: int = POLICY.max_archive_bytes

    def __post_init__(self):
        from core.component_release_transport import ReleaseTransportPolicy
        from suite_core.discovery.component_release_signature import _trust
        component = workflow_component(self.component_id)
        offer = self.expected_offer
        if (type(self.trust_bytes) is not bytes or sha256(self.trust_bytes).hexdigest() != self.trust_sha256
                or type(self.transport_qualification_sha256) is not str
                or re.fullmatch('[0-9a-f]{64}', self.transport_qualification_sha256) is None
                or self.channel != 'beta' or type(self.max_artifact_bytes) is not int
                or not 0 < self.max_artifact_bytes <= POLICY.max_archive_bytes
                or type(offer) is not ExactComponentOffer or offer.component_id != component
                or offer.trust_sha256 != self.trust_sha256
                or (offer.os, offer.architecture, offer.channel) != ('windows', 'x86_64', self.channel)
                or offer.artifact.size_bytes > self.max_artifact_bytes):
            raise ValueError('exact_compiled_workflow_profile_required')
        trust = _trust(self.trust_bytes)
        if not any(row['key_id'] == offer.key_id and row.get('status', 'active') == 'active' for row in trust['keys']):
            raise ValueError('explicit_public_publisher_required')
        ReleaseTransportPolicy(self.asset_redirect_origins)
        if not self.asset_redirect_origins:
            raise ValueError('qualified_asset_origins_required')


class WorkflowStageOwner:
    def __init__(self, session, reader_owner, component_id, *, assert_workspace_idle):
        from .bootstrap_entry import NormalEntrySession
        from core.encyclopedia_connection import OwnedEncyclopedia
        if (type(session) is not NormalEntrySession or type(reader_owner) is not OwnedEncyclopedia
                or reader_owner.normal_entry_session is not session or not callable(assert_workspace_idle)):
            raise ValueError('original_workflow_stage_owner_required')
        self.session, self.reader_owner = session, reader_owner
        self.component = workflow_component(component_id)
        self.assert_workspace_idle = assert_workspace_idle

    @contextmanager
    def hold(self, component):
        from suite_core.discovery.component_staging import ComponentStageBinding, component_stage_owner
        if component != self.component:
            raise ValueError('same_workflow_component_required')
        self.reader_owner.assert_idle()
        self.assert_workspace_idle()
        session, scope = self.session, None
        self.reader_owner.component_stage_pending = True
        try:
            scope = session.request_component_stage('begin', component=component)
            root, data = Path(scope['code_root']), Path(scope['data_root'])
            def check():
                self.assert_workspace_idle()
                if self.reader_owner.native is not None or not self.reader_owner.component_stage_pending:
                    raise ValueError('original_workflow_stage_changed')
                if session.request_component_stage('check', scope['scope_id'], component=component) != scope:
                    raise ValueError('original_workflow_scope_changed')
            check()
            yield ComponentStageBinding(root, component_stage_owner(root, data, component), check, data)
        finally:
            if scope is not None and session.state == 'ready':
                session.request_component_stage('release', scope['scope_id'], component=component)
                self.reader_owner.component_stage_pending = False
            elif scope is None and session.state == 'ready':
                self.reader_owner.component_stage_pending = False


def factory_for_session(session, component_id):
    from .compiled_workflow_config import PROFILES
    workflow_component(component_id)
    profile = PROFILES.get(component_id)
    if profile is None:
        return None
    if type(profile) is not CompiledWorkflowAcquisitionProfile or profile.component_id != component_id:
        raise ValueError('typed_compiled_workflow_profile_required')
    from .bootstrap_entry import NormalEntrySession
    if type(session) is not NormalEntrySession or session.state not in {'offered', 'ready'}:
        raise ValueError('original_native_normal_session_required')
    if getattr(session, 'workflow_contract_version', 0) != 1:
        return None  # Manager-only update cannot upgrade the original launcher.
    def create(reader_owner, settings_path, *, workspace_owner):
        from uuid import uuid4
        from core.component_acquisition import ComponentAcquisition
        from core.component_release_transport import ComponentReleaseTransport, ReleaseTransportPolicy
        from core.workflow_connection_settings import WorkflowConnectionStore
        from core.workflow_installation import WorkflowInstallation
        from suite_core.discovery.component_staging import ComponentStageAdmission, guard
        from .resource_executor import ResourceExecutorOwner
        from .version import VERSION
        expected = Path(session.admitted.settings_path)
        if expected.suffix.casefold() == '.ini':
            expected = expected.with_suffix('.json')
        if Path(settings_path) != expected:
            raise ValueError('original_workflow_settings_binding_required')
        from core.workflow_native_owner import WorkflowWorkspaceOwner
        if type(workspace_owner) is not WorkflowWorkspaceOwner or workspace_owner.session is not session:
            raise ValueError('original_workflow_native_owner_required')
        stage = WorkflowStageOwner(session, reader_owner, component_id,
                                   assert_workspace_idle=workspace_owner.assert_idle)
        cache = session.admitted.layout.data_root / 'Manager' / 'WorkflowAcquisition' / component_id / uuid4().hex
        guard(cache)
        cache.mkdir(parents=True, exist_ok=False)
        acquisition = ComponentAcquisition(cache_root=cache, cache_owner=cache.name,
            opener=ComponentReleaseTransport(ReleaseTransportPolicy(profile.asset_redirect_origins)),
            host_version=VERSION, trust_bytes=profile.trust_bytes, trust_sha256=profile.trust_sha256,
            os_name='windows', architecture='x86_64', max_artifact_bytes=profile.max_artifact_bytes,
            manifest_name=profile.expected_offer.manifest.name, signature_name=profile.expected_offer.signature.name)
        return (WorkflowInstallation(component_id, acquisition, WorkflowConnectionStore(settings_path, component_id),
                    admission=ComponentStageAdmission(stage.hold), native_owner=workspace_owner), ResourceExecutorOwner())
    return create


def root_selection_for_owner(owner, admitted, component_id):
    """Only original Root issuance from signed retained transaction history."""
    from .compiled_workflow_config import PROFILES
    from core.manager_launch_owner import DeploymentOwner
    from core.workflow_connection_settings import WorkflowConnectionStore
    from suite_core.discovery.component_staging import (guard, read_json, validate_journal,
                                                        component_stage_owner, prior_journal_bytes)
    component = workflow_component(component_id)
    profile = PROFILES.get(component_id)
    if profile is None:
        return None
    if type(profile) is not CompiledWorkflowAcquisitionProfile or profile.component_id != component_id:
        raise ValueError('typed_compiled_workflow_profile_required')
    if (type(owner) is not DeploymentOwner or owner.root != admitted.layout.code_root
            or owner.data_root != admitted.layout.data_root):
        raise ValueError('original_workflow_roots_required')
    settings = Path(admitted.settings_path)
    if settings.suffix.casefold() == '.ini':
        settings = settings.with_suffix('.json')
    if not settings.is_absolute() or '..' in settings.parts or not settings.is_relative_to(owner.data_root / 'Manager'):
        raise ValueError('original_workflow_settings_required')
    guard(settings)
    store = WorkflowConnectionStore(settings, component_id)
    selected = store.selected(store.observe())
    if selected is None:
        return None
    base = owner.root / 'State' / component_id
    origin = component_stage_owner(owner.root, owner.data_root, component)
    journal, seen = read_json(base / 'transaction.json'), set()
    while True:
        validate_journal(journal, component, origin, profile.trust_bytes)
        pin = sha256(json.dumps(journal, sort_keys=True).encode()).hexdigest()
        if pin in seen:
            raise ValueError('cyclic_component_history')
        seen.add(pin)
        target = journal['target']
        if (owner.root / target['directory'], target['version'], target['inventory_sha256']) == (
                selected.directory, selected.version, selected.inventory_sha256):
            return owner.issue_workflow_selection(component_id, settings,
                manifest_bytes=base64.b64decode(journal['manifest'], validate=True),
                signature_bytes=base64.b64decode(journal['signature'], validate=True), trust_bytes=profile.trust_bytes)
        raw = prior_journal_bytes(base, journal, component, origin, profile.trust_bytes)
        if raw is None:
            raise ValueError('signed_workflow_origin_unavailable')
        journal = json.loads(raw)
