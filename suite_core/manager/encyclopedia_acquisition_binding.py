"""Inactive frozen-entry composition over existing acquisition and native owners."""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from core.component_acquisition import ExactComponentOffer


@dataclass(frozen=True)
class CompiledEncyclopediaAcquisitionProfile:
    trust_bytes: bytes
    trust_sha256: str
    asset_redirect_origins: tuple[str, ...]
    transport_qualification_sha256: str
    channel: str = "beta"
    max_artifact_bytes: int = 64 * 1024 * 1024
    expected_offer: ExactComponentOffer | None = None

    def __post_init__(self):
        import re
        from suite_core.installation import ComponentId
        from core.component_release_transport import ReleaseTransportPolicy
        from suite_core.discovery.component_release_signature import _trust
        from suite_core.discovery.component_staging import MAX_ARCHIVE
        if (type(self.trust_bytes) is not bytes or type(self.trust_sha256) is not str
                or sha256(self.trust_bytes).hexdigest() != self.trust_sha256
                or type(self.transport_qualification_sha256) is not str
                or re.fullmatch('[0-9a-f]{64}',self.transport_qualification_sha256) is None
                or self.channel != 'beta' or type(self.max_artifact_bytes) is not int
                or not 0 < self.max_artifact_bytes <= MAX_ARCHIVE):
            raise ValueError('exact_compiled_acquisition_profile_required')
        if (type(self.expected_offer) is not ExactComponentOffer
                or self.expected_offer.component_id != ComponentId('collectigen.tag-encyclopedia')
                or self.expected_offer.trust_sha256 != self.trust_sha256
                or (self.expected_offer.os, self.expected_offer.architecture, self.expected_offer.channel) != ('windows','x86_64',self.channel)
                or self.expected_offer.artifact.size_bytes > self.max_artifact_bytes):
            raise ValueError('exact_compiled_qualification_offer_required')
        trust = _trust(self.trust_bytes)
        if not any(key['key_id'] == self.expected_offer.key_id and key.get('status','active') == 'active' for key in trust['keys']):
            raise ValueError('explicit_public_publisher_required')
        ReleaseTransportPolicy(self.asset_redirect_origins)
        if not self.asset_redirect_origins:
            raise ValueError('qualified_asset_origins_required')


def factory_for_session(session):
    """Canonical None is a zero-effect hold; a compiled profile is not a lease."""
    from .compiled_encyclopedia_config import PROFILE
    if PROFILE is None:
        return None
    if type(PROFILE) is not CompiledEncyclopediaAcquisitionProfile:
        raise ValueError('typed_compiled_acquisition_profile_required')
    from .bootstrap_entry import NormalEntrySession
    if type(session) is not NormalEntrySession or session.state not in ('offered','ready'):
        raise ValueError('original_native_normal_session_required')
    profile = PROFILE

    def create(reader_owner, settings_path):
        from uuid import uuid4
        from core.encyclopedia_connection import OwnedEncyclopedia, ReaderConnectionStore
        from core.component_acquisition import ComponentAcquisition
        from core.component_release_transport import ComponentReleaseTransport, ReleaseTransportPolicy
        from core.encyclopedia_installation import EncyclopediaInstallation
        from suite_core.discovery.component_staging import ComponentStageAdmission, guard
        from .resource_executor import ResourceExecutorOwner
        from .version import VERSION
        # Preserve the original INI admission; AppSettings owns its JSON sidecar.
        admitted_settings = Path(session.admitted.settings_path)
        connection_settings = (admitted_settings.with_suffix('.json')
            if admitted_settings.suffix.casefold() == '.ini' else admitted_settings)
        if (type(reader_owner) is not OwnedEncyclopedia or reader_owner.normal_entry_session is not session
                or Path(settings_path) != connection_settings):
            raise ValueError('original_reader_settings_binding_required')
        cache = session.admitted.layout.data_root / 'Manager' / 'EncyclopediaAcquisition' / uuid4().hex
        guard(cache)
        cache.mkdir(parents=True,exist_ok=False)
        acquisition = ComponentAcquisition(cache_root=cache,cache_owner=cache.name,
            opener=ComponentReleaseTransport(ReleaseTransportPolicy(profile.asset_redirect_origins)),
            host_version=VERSION,trust_bytes=profile.trust_bytes,trust_sha256=profile.trust_sha256,
            os_name='windows',architecture='x86_64',max_artifact_bytes=profile.max_artifact_bytes,
            manifest_name=profile.expected_offer.manifest.name,
            signature_name=profile.expected_offer.signature.name,
            expected_offer=profile.expected_offer)
        service = EncyclopediaInstallation(acquisition,ReaderConnectionStore(settings_path),
            admission=ComponentStageAdmission(reader_owner.component_stage_hold),native_health=reader_owner.native_health)
        return service, ResourceExecutorOwner()
    return create
