"""Organizer acquisition using the original component-bound root stage grant."""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re

from core.component_acquisition import ExactComponentOffer
from core.organizer_connection import COMPONENT, POLICY


@dataclass(frozen=True)
class CompiledOrganizerAcquisitionProfile:
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
        if (type(self.trust_bytes) is not bytes or sha256(self.trust_bytes).hexdigest()!=self.trust_sha256
                or type(self.transport_qualification_sha256) is not str
                or re.fullmatch('[0-9a-f]{64}',self.transport_qualification_sha256) is None
                or self.channel!='beta' or type(self.max_artifact_bytes) is not int
                or not 0<self.max_artifact_bytes<=POLICY.max_archive_bytes):
            raise ValueError('exact_compiled_organizer_profile_required')
        offer=self.expected_offer
        if (type(offer) is not ExactComponentOffer or offer.component_id!=COMPONENT
                or offer.trust_sha256!=self.trust_sha256
                or (offer.os,offer.architecture,offer.channel)!=('windows','x86_64',self.channel)
                or offer.artifact.size_bytes>self.max_artifact_bytes):
            raise ValueError('exact_compiled_organizer_offer_required')
        trust=_trust(self.trust_bytes)
        if not any(key['key_id']==offer.key_id and key.get('status','active')=='active' for key in trust['keys']):
            raise ValueError('explicit_public_publisher_required')
        ReleaseTransportPolicy(self.asset_redirect_origins)
        if not self.asset_redirect_origins:raise ValueError('qualified_asset_origins_required')


class OrganizerStageOwner:
    """Retain original session/Reader exclusion; never issue a local root lease."""
    def __init__(self, session, reader_owner, native_owner=None):
        from .bootstrap_entry import NormalEntrySession
        from core.encyclopedia_connection import OwnedEncyclopedia
        if (type(session) is not NormalEntrySession or type(reader_owner) is not OwnedEncyclopedia
                or reader_owner.normal_entry_session is not session):
            raise ValueError('original_organizer_session_required')
        self.session,self.reader_owner,self.native_owner=session,reader_owner,native_owner

    @contextmanager
    def hold(self, component):
        from suite_core.discovery.component_staging import ComponentStageBinding, component_stage_owner
        if component!=COMPONENT:raise ValueError('organizer_component_required')
        reader=self.reader_owner;reader.assert_idle()
        if self.native_owner is not None and self.native_owner.close_held:
            raise ValueError('organizer_native_owner_busy')
        session=self.session;scope=None;reader.component_stage_pending=True
        try:
            scope=session.request_component_stage('begin',component=COMPONENT)
            root,data=Path(scope['code_root']),Path(scope['data_root'])
            def check():
                if reader.native is not None or not reader.component_stage_pending:
                    raise ValueError('original_component_owner_changed')
                if self.native_owner is not None and self.native_owner.close_held:
                    raise ValueError('organizer_native_owner_busy')
                if session.request_component_stage('check',scope['scope_id'],component=COMPONENT)!=scope:
                    raise ValueError('original_organizer_scope_changed')
            check()
            yield ComponentStageBinding(root,component_stage_owner(root,data,COMPONENT),check,data)
        finally:
            # A lost release ACK remains held. Original session closure settles it.
            if scope is not None and session.state=='ready':
                session.request_component_stage('release',scope['scope_id'],component=COMPONENT)
                reader.component_stage_pending=False
            elif scope is None and session.state=='ready':reader.component_stage_pending=False


def factory_for_session(session):
    from .compiled_organizer_config import PROFILE
    if PROFILE is None:return None
    if type(PROFILE) is not CompiledOrganizerAcquisitionProfile:
        raise ValueError('typed_compiled_organizer_profile_required')
    from .bootstrap_entry import NormalEntrySession
    if type(session) is not NormalEntrySession or session.state not in ('offered','ready'):
        raise ValueError('original_native_normal_session_required')
    profile=PROFILE
    def create(reader_owner, settings_path, *, native_owner=None):
        from uuid import uuid4
        from core.component_acquisition import ComponentAcquisition
        from core.component_release_transport import ComponentReleaseTransport, ReleaseTransportPolicy
        from core.organizer_connection_settings import OrganizerConnectionStore
        from core.organizer_installation import OrganizerInstallation
        from suite_core.discovery.component_staging import ComponentStageAdmission, guard
        from .resource_executor import ResourceExecutorOwner
        from .version import VERSION
        from core.organizer_native_owner import OwnedOrganizer
        if native_owner is None:native_owner=OwnedOrganizer(session,reader_owner)
        if type(native_owner) is not OwnedOrganizer or native_owner.normal_entry_session is not session or native_owner.reader_owner is not reader_owner:
            raise ValueError('original_organizer_native_owner_required')
        stage=OrganizerStageOwner(session,reader_owner,native_owner)
        admitted=Path(session.admitted.settings_path)
        expected=admitted.with_suffix('.json') if admitted.suffix.casefold()=='.ini' else admitted
        if Path(settings_path)!=expected:raise ValueError('original_organizer_settings_binding_required')
        cache=session.admitted.layout.data_root/'Manager'/'OrganizerAcquisition'/uuid4().hex
        guard(cache);cache.mkdir(parents=True,exist_ok=False)
        acquisition=ComponentAcquisition(cache_root=cache,cache_owner=cache.name,
            opener=ComponentReleaseTransport(ReleaseTransportPolicy(profile.asset_redirect_origins)),
            host_version=VERSION,trust_bytes=profile.trust_bytes,trust_sha256=profile.trust_sha256,
            os_name='windows',architecture='x86_64',max_artifact_bytes=profile.max_artifact_bytes,
            manifest_name=profile.expected_offer.manifest.name,
            signature_name=profile.expected_offer.signature.name,
            expected_offer=profile.expected_offer)
        service=OrganizerInstallation(acquisition,OrganizerConnectionStore(settings_path),
            admission=ComponentStageAdmission(stage.hold),native_owner=native_owner)
        return service,ResourceExecutorOwner()
    return create


def root_selection_for_owner(owner, admitted):
    """Read signed original transaction evidence, then ask the retained root to issue."""
    from .compiled_organizer_config import PROFILE
    if PROFILE is None:return None
    if type(PROFILE) is not CompiledOrganizerAcquisitionProfile:
        raise ValueError('typed_compiled_organizer_profile_required')
    from core.manager_launch_owner import DeploymentOwner
    from core.organizer_connection_settings import OrganizerConnectionStore
    from suite_core.discovery.component_staging import (guard, read_json, validate_journal,
        component_stage_owner, prior_journal_bytes)
    import base64,json
    if (type(owner) is not DeploymentOwner or owner.root!=admitted.layout.code_root
            or owner.data_root!=admitted.layout.data_root):
        raise ValueError('original_organizer_roots_required')
    path=Path(admitted.settings_path)
    if path.suffix.casefold()=='.ini':path=path.with_suffix('.json')
    if (not path.is_absolute() or '..' in path.parts
            or not path.is_relative_to(owner.data_root/'Manager')):
        raise ValueError('original_organizer_settings_required')
    guard(path)
    selected=OrganizerConnectionStore(path).selected(OrganizerConnectionStore(path).observe())
    if selected is None:return None
    base=owner.root/'State'/COMPONENT.value
    origin=component_stage_owner(owner.root,owner.data_root,COMPONENT)
    journal=read_json(base/'transaction.json');seen=set()
    while True:
        validate_journal(journal,COMPONENT,origin,PROFILE.trust_bytes)
        pin=sha256(json.dumps(journal,sort_keys=True).encode()).hexdigest()
        if pin in seen:raise ValueError('cyclic_component_history')
        seen.add(pin);target=journal['target']
        if (owner.root/target['directory'],target['version'],target['inventory_sha256'])==(
                selected.directory,selected.version,selected.inventory_sha256):
            return owner.issue_organizer_selection(admitted.settings_path,
                manifest_bytes=base64.b64decode(journal['manifest'],validate=True),
                signature_bytes=base64.b64decode(journal['signature'],validate=True),trust_bytes=PROFILE.trust_bytes)
        raw=prior_journal_bytes(base,journal,COMPONENT,origin,PROFILE.trust_bytes)
        if raw is None:raise ValueError('signed_organizer_origin_unavailable')
        journal=json.loads(raw)
