"""Explicit component acquisition only: no default trust/network, install or launch."""
from dataclasses import dataclass
from hashlib import sha256
import json
import re
from pathlib import Path
from uuid import uuid4
from release_contract import verify_release_manifest, ensure_supported_upgrade

from core.component_release_identity import ComponentReleaseIdentity, require_component_maturity_upgrade
from core.application_update import ApplicationUpdateService, ReleaseAssetSource, MANIFEST_ASSET, SIGNATURE_ASSET
from suite_core.discovery.github_component_releases import (
    REPOSITORY, MAX_BYTES, MAX_RELEASES, ReleaseLocator, release_asset_identity,
    discover_component_ids, discover_releases, discover_candidates, _pairs, _constant, _version,
)
from suite_core.discovery.component_release_signature import (
    verify_component_release, verify_component_artifact, MAX_METADATA_BYTES, VerifiedComponentRelease,
)
from suite_core.discovery.component_staging import guard, require
from suite_core.installation import ComponentId


EXACT_OFFER_HOLD = "This release changed or differs from the exact package approved for this test. Installation is held; review the approved release again."


class ExactOfferHold(ValueError):
    pass


@dataclass(frozen=True)
class InstalledOfferContext:
    """Original-observer adapter value, never a lease or ownership proof."""
    component_id: ComponentId
    version: str | None
    schema_version: int | None
    maturity: str | None
    observation_id: str
    pending: bool = False

    def __post_init__(self):
        require(type(self.component_id) is ComponentId and type(self.pending) is bool, "installed_context_type")
        require(type(self.observation_id) is str and 0 < len(self.observation_id.encode('utf-8')) <= 1024
            and "\0" not in self.observation_id, "installed_observation_token")
        if self.version is None:
            require(self.schema_version is None and self.maturity is None, "explicit_first_install_context")
        else:
            require(type(self.schema_version) is int and self.schema_version in (1,2), "installed_context_schema")
            if self.schema_version == 1:
                _version(self.version); require(self.maturity is None, "legacy_context_maturity")
            else:
                ComponentReleaseIdentity(self.version, self.maturity)


@dataclass(frozen=True)
class ExactOfferAsset:
    name: str
    size_bytes: int
    sha256: str
    url: str
    asset_id: int
    schema_version: int = 1

    def __post_init__(self):
        require(type(self.size_bytes) is int and 0 < self.size_bytes <= 2**40, "exact_asset_size")
        require(type(self.asset_id) is int and 0 < self.asset_id < 2**63, "exact_asset_id")
        require(type(self.sha256) is str and re.fullmatch('[0-9a-f]{64}', self.sha256), "exact_asset_hash")
        require(type(self.schema_version) is int and self.schema_version in (1,2), "exact_asset_schema")
        tag, name = release_asset_identity(self.url, include_schema2=self.schema_version==2)
        require(name == self.name and ("/releases/schema2/" in tag) == (self.schema_version==2), "exact_asset_url")


@dataclass(frozen=True)
class ExactComponentOffer:
    """Reviewed qualification expectations, never signature or installation authority."""
    component_id: ComponentId
    software_version: str
    channel: str
    os: str
    architecture: str
    key_id: str
    trust_sha256: str
    repository_id: int
    release_id: int
    tag: str
    manifest: ExactOfferAsset
    signature: ExactOfferAsset
    artifact: ExactOfferAsset
    repository: str = REPOSITORY
    schema_version: int = 1
    maturity: str | None = None

    def __post_init__(self):
        require(type(self.component_id) is ComponentId and self.repository == REPOSITORY, "exact_offer_repository_component")
        version = _version(self.software_version)
        require(type(self.schema_version) is int and self.schema_version in (1,2), "exact_offer_schema")
        if self.schema_version == 2:
            identity = ComponentReleaseIdentity(self.software_version, self.maturity)
            require((identity.maturity!='stable') == (self.channel=='beta'), "exact_offer_channel_version")
            tag = self.component_id.value + '/releases/schema2/' + identity.version + '/' + identity.maturity
        else:
            require(self.maturity is None and bool(version.prerelease) == (self.channel == "beta"), "exact_offer_channel_version")
            tag = self.component_id.value + '/v' + self.software_version
        require(self.tag == tag, "exact_offer_tag")
        require(self.channel in ('beta', 'stable') and self.os in ('windows', 'linux')
                and self.architecture in ('x86_64', 'aarch64'), "exact_offer_platform_channel")
        require(type(self.key_id) is str and 0 < len(self.key_id) <= 128
                and all(32 <= ord(c) < 127 for c in self.key_id), "exact_offer_key")
        require(type(self.trust_sha256) is str and re.fullmatch('[0-9a-f]{64}', self.trust_sha256), "exact_offer_trust")
        for value in (self.repository_id, self.release_id):
            require(type(value) is int and 0 < value < 2**63, "exact_offer_github_id")
        assets = (self.manifest, self.signature, self.artifact)
        require(all(type(a) is ExactOfferAsset for a in assets), "exact_offer_assets")
        require(len({a.name for a in assets}) == 3 and len({a.asset_id for a in assets}) == 3, "exact_offer_unique_assets")
        require(all(a.schema_version == self.schema_version and
            release_asset_identity(a.url,include_schema2=self.schema_version==2) == (self.tag,a.name)
            for a in assets), "exact_offer_asset_tag")

    @classmethod
    def from_reviewed_offer(cls, offer, *, repository_id):
        """Explicit caller preparation only; never enroll from a live feed automatically."""
        require(type(offer) is ComponentOffer and type(offer.locator) is ReleaseLocator, "reviewed_offer_locator_required")
        assets = {a.name: a for a in offer.locator.assets}
        def pin(name, raw=None):
            a = assets[name]
            digest = offer.release.artifact.sha256 if raw is None else sha256(raw).hexdigest()
            return ExactOfferAsset(name, a.declared_size, digest, a.url, a.asset_id, offer.release.schema_version)
        return cls(offer.release.component_id, offer.release.software_version, offer.release.channel,
            offer.release.os, offer.release.architecture, offer.release.key_id, offer.trust_sha256,
            repository_id, offer.locator.release_id, offer.locator.tag,
            pin(release_asset_identity(offer.manifest_url,include_schema2=offer.release.schema_version==2)[1], offer.manifest_bytes),
            pin(release_asset_identity(offer.signature_url,include_schema2=offer.release.schema_version==2)[1], offer.signature_bytes),
            pin(offer.release.artifact.name), schema_version=offer.release.schema_version, maturity=offer.release.maturity)

    def check_locator(self, locator):
        expected = {a.name: (a.url, a.size_bytes, a.asset_id) for a in (self.manifest, self.signature, self.artifact)}
        if (type(locator) is not ReleaseLocator or
                (locator.component_id, locator.software_version, locator.channel, locator.tag, locator.release_id, locator.schema_version, locator.maturity) !=
                (self.component_id, self.software_version, self.channel, self.tag, self.release_id, self.schema_version, self.maturity)):
            raise ExactOfferHold(EXACT_OFFER_HOLD)
        # Reuse stock validation for every locator, including ancillary assets.
        # Only the three selected assets confer exact package/metadata authority.
        try:
            row = dict(id=locator.release_id, tag_name=locator.tag, draft=False,
                prerelease=locator.channel == 'beta', assets=[dict(name=a.name,
                    browser_download_url=a.url, size=a.declared_size, id=a.asset_id)
                    for a in locator.assets])
            if self.schema_version == 2:
                rows = discover_candidates(json.dumps([row]).encode(),component_id=self.component_id,
                    channel=self.channel,complete=True,include_schema2=True)
                require(len(rows)==1,"exact_locator_required"); checked=rows[0]
            else:
                checked = discover_releases(json.dumps([row]).encode(),
                    component_id=self.component_id, channel=self.channel,
                    current_version=None, complete=True).candidate
        except (ValueError, TypeError, AttributeError):
            raise ExactOfferHold(EXACT_OFFER_HOLD) from None
        selected = {a.name: (a.url, a.declared_size, a.asset_id)
                    for a in checked.assets if a.name in expected}
        if selected != expected:
            raise ExactOfferHold(EXACT_OFFER_HOLD)

    def check_offer(self, offer):
        self.check_locator(offer.locator)
        r = offer.release
        if ((r.component_id, r.software_version, r.channel, r.os, r.architecture, r.key_id, r.schema_version, r.maturity) !=
                (self.component_id, self.software_version, self.channel, self.os, self.architecture, self.key_id, self.schema_version, self.maturity) or
                offer.repository_id != self.repository_id or offer.trust_sha256 != self.trust_sha256 or
                r.manifest_sha256 != self.manifest.sha256 or
                sha256(offer.manifest_bytes).hexdigest() != self.manifest.sha256 or len(offer.manifest_bytes) != self.manifest.size_bytes or
                sha256(offer.signature_bytes).hexdigest() != self.signature.sha256 or len(offer.signature_bytes) != self.signature.size_bytes or
                (r.artifact.name, r.artifact.size_bytes, r.artifact.sha256, offer.artifact_source.url, offer.artifact_source.name, offer.artifact_source.size_bytes) !=
                (self.artifact.name, self.artifact.size_bytes, self.artifact.sha256, self.artifact.url, self.artifact.name, self.artifact.size_bytes) or
                offer.manifest_url != self.manifest.url or offer.signature_url != self.signature.url):
            raise ExactOfferHold(EXACT_OFFER_HOLD)


@dataclass(frozen=True)
class ComponentOffer:
    release: VerifiedComponentRelease
    operation: str
    release_notes: str  # exact signed text; unsigned title/category never promoted
    manifest_bytes: bytes
    signature_bytes: bytes
    manifest_url: str
    signature_url: str
    artifact_source: ReleaseAssetSource
    trust_sha256: str
    current_version: str | None
    locator: ReleaseLocator | None = None
    repository_id: int | None = None
    installed_context: InstalledOfferContext | None = None
    discovery_epoch: int | None = None

    @property
    def production_install_authorized(self):
        return False


@dataclass(frozen=True)
class AcquiredComponent:
    offer: ComponentOffer
    artifact: Path

    @property
    def production_install_authorized(self):
        return False


class ComponentAcquisition:
    """Leaf orchestration over stock transport and signature admission.

    Cache must be an empty explicit absolute directory, owned solely by this
    instance. Every failed attempt removes only its known files; interruptions
    retry fresh. No installation state or user-data path is consumed.
    """
    def __init__(self, *, cache_root, cache_owner, opener, host_version,
                 trust_bytes=None, trust_sha256=None, os_name="windows",
                 architecture="x86_64", max_artifact_bytes=512 * 1024 * 1024,
                 max_cache_bytes=1024 * 1024 * 1024,
                 manifest_name=MANIFEST_ASSET, signature_name=SIGNATURE_ASSET, expected_offer=None,
                 schema2_metadata=False, observe_installed=None, expected_repository_id=None):
        require(expected_offer is None or type(expected_offer) is ExactComponentOffer, "typed_exact_offer_required")
        require(type(schema2_metadata) is bool,"explicit_schema2_metadata_mode")
        if schema2_metadata:
            require(callable(observe_installed) and type(expected_repository_id) is int
                and 0 < expected_repository_id < 2**63,"explicit_metadata_context_repository")
            require((manifest_name,signature_name)==(MANIFEST_ASSET,SIGNATURE_ASSET),"canonical_schema2_metadata_assets")
            require(expected_offer is None or expected_offer.repository_id==expected_repository_id,"exact_repository_mismatch")
        else:
            require(observe_installed is None and expected_repository_id is None
                and (expected_offer is None or expected_offer.schema_version==1),"metadata_optin_required")
        self.schema2_metadata, self._observe_installed = schema2_metadata, observe_installed
        self._repository_id, self._offer_epoch = expected_repository_id, 0
        self._expected_offer = expected_offer
        require(callable(opener), "explicit_transport_required")
        root = Path(cache_root)
        require(root.is_absolute(), "absolute_cache_required")
        guard(root)
        require(root.is_dir() and not any(root.iterdir()), "empty_owned_cache_required")
        require(type(cache_owner) is str and 1 <= len(cache_owner) <= 128, "cache_owner")
        require(type(max_artifact_bytes) is int and 0 < max_artifact_bytes <= 512 * 1024 * 1024, "artifact_bound")
        require(type(max_cache_bytes) is int and 0 < max_cache_bytes <= 1024 * 1024 * 1024, "cache_bound")
        require(os_name in ("windows", "linux") and architecture in ("x86_64", "aarch64"), "target_platform")
        for name in (manifest_name, signature_name):
            require(type(name) is str and Path(name).name == name and name not in ("", ".", "..")
                    and not any(c in name for c in "/\\:"), "metadata_asset_name")
        self.root, self.os, self.arch = root, os_name, architecture
        self.trust, self.trust_pin = trust_bytes, trust_sha256
        self.maximum = max_artifact_bytes
        self.cache_maximum = max_cache_bytes
        self._acquired = {}
        self.manifest_name, self.signature_name = manifest_name, signature_name
        self._owner = json.dumps(dict(purpose="COMPONENT_ACQUISITION_CACHE", owner=cache_owner), sort_keys=True).encode()
        self._marker = root / ".component-acquisition-owner.json"
        self._marker.write_bytes(self._owner)
        require(self._marker.read_bytes() == self._owner, "cache_owner_readback")
        self._cache_binding = (self.root, self._marker, self._owner)
        # The default app trust path is never read: only transport methods are used.
        self.transport = ApplicationUpdateService(root, host_version, opener=opener)
        self._offers = {}

    def _check(self, cancel):
        require((self.root, self._marker, self._owner) == self._cache_binding, "cache_owner_changed")
        guard(self.root)
        guard(self._marker)
        require(self._marker.stat().st_size == len(self._owner), "cache_owner_changed")
        require(self._marker.read_bytes() == self._owner, "cache_owner_changed")
        if cancel is not None and cancel():
            raise InterruptedError("Component acquisition cancelled safely.")

    def _trust(self):
        require(type(self.trust) is bytes and type(self.trust_pin) is str
                and sha256(self.trust).hexdigest() == self.trust_pin, "explicit_pinned_public_trust_required")

    def _fetch(self, url, cancel, limit=MAX_METADATA_BYTES):
        self._check(cancel)
        result = self.transport.fetch_metadata(url, limit=limit)
        self._check(cancel)
        return result

    def _listing(self, should_cancel):
        repository_id = None
        if self._expected_offer is not None or self.schema2_metadata:
            raw = self._fetch(f"https://api.github.com/repos/{REPOSITORY}", should_cancel, MAX_BYTES)
            value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
            repository_id = value.get('id') if type(value) is dict else None
            if type(repository_id) is not int or repository_id != (self._repository_id if self.schema2_metadata else self._expected_offer.repository_id) or value.get('full_name') != REPOSITORY:
                raise ExactOfferHold(EXACT_OFFER_HOLD)
        rows = []
        # 50 + 50 + empty: explicit completion, never silently truncate at 100.
        for page in range(1, 4):
            data = self._fetch(f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=50&page={page}",
                               should_cancel, MAX_BYTES)
            try:
                values = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise ValueError("release_listing_syntax") from exc
            require(type(values) is list and len(values) <= 50, "release_page_bound")
            rows.extend(values)
            require(len(rows) <= MAX_RELEASES, "release_listing_bound")
            if len(values) < 50:
                break
        else:
            raise ValueError("complete_release_listing_required")
        return json.dumps(rows, separators=(",", ":")).encode(), repository_id

    def discover(self, *, channel, installed_versions, should_cancel=None, component_id=None):
        """Discover all components by default, or one explicit typed component.

        Scope precedes candidate platform checks; selected offers retain the same
        complete listing, signature, trust, compatibility and artifact admission.
        """
        if self.schema2_metadata:
            return self._discover_metadata(channel,installed_versions,component_id,should_cancel)
        self._trust()
        require(type(installed_versions) is dict, "explicit_installed_versions")
        require(component_id is None or type(component_id) is ComponentId, "explicit_component_scope")
        if self._expected_offer is not None and channel != self._expected_offer.channel:
            raise ExactOfferHold(EXACT_OFFER_HOLD)
        if (self._expected_offer is not None and component_id is not None
                and component_id != self._expected_offer.component_id):
            raise ExactOfferHold(EXACT_OFFER_HOLD)
        listing, repository_id = self._listing(should_cancel)
        offers = []
        # Commit offers only after complete successful signed discovery.
        components = ((component_id,) if component_id is not None else
                      (self._expected_offer.component_id,) if self._expected_offer is not None else
                      discover_component_ids(listing, complete=True))
        for component in components:
            current = installed_versions.get(component.value)
            selected = discover_releases(listing, component_id=component, channel=channel,
                                         current_version=current, complete=True).candidate
            if selected is None:
                continue
            if self._expected_offer is not None: self._expected_offer.check_locator(selected)
            assets = {asset.name: asset for asset in selected.assets}
            require(self.manifest_name in assets and self.signature_name in assets, "signed_asset_pair_required")
            manifest_url, signature_url = assets[self.manifest_name].url, assets[self.signature_name].url
            message = self._fetch(manifest_url, should_cancel)
            signature = self._fetch(signature_url, should_cancel)
            verified = verify_component_release(message, signature, self.trust,
                expected_component=component, expected_version=selected.software_version,
                expected_channel=channel, expected_os=self.os, expected_architecture=self.arch)
            if current is not None:
                release = verify_release_manifest(message, signature, json.loads(self.trust), expected_channel=channel)
                ensure_supported_upgrade(current, release)
            require(verified.artifact.size_bytes <= self.maximum, "artifact_bound")
            source = assets.get(verified.artifact.name)
            require(source is not None and source.declared_size == verified.artifact.size_bytes, "signed_asset_binding")
            offer = ComponentOffer(verified, "install" if current is None else "update",
                json.loads(message.decode("utf-8-sig"))["release_notes"], message, signature,
                manifest_url, signature_url,
                ReleaseAssetSource(source.name, source.url, source.declared_size), self.trust_pin, current,
                selected, repository_id)
            if self._expected_offer is not None: self._expected_offer.check_offer(offer)
            offers.append(offer)
        self._check(should_cancel)
        self._offers = {o.release.manifest_sha256: o for o in offers}
        return tuple(offers)

    def _installed_context(self, component, version):
        context = self._observe_installed()
        require(type(context) is InstalledOfferContext and context.component_id==component
            and context.version==version and not context.pending,"installed_context_held_or_changed")
        return context

    def _select_metadata(self, channel, component, context, cancel):
        """Shared read-only selection; caller alone owns epoch/cache publication."""
        self._trust(); trust_before=(self.trust,self.trust_pin)
        listing, repository_id = self._listing(cancel)
        locators = discover_candidates(listing,component_id=component,channel=channel,complete=True,include_schema2=True)
        def number(value):
            parsed=_version(value);return parsed.major,parsed.minor,parsed.patch
        candidates=[]
        if locators:
            highest=max(number(l.software_version) for l in locators)
            if context.version is None or highest>=number(context.version):
                for locator in locators:
                    if number(locator.software_version)!=highest:continue
                    self._check(cancel)
                    require(type(locator.release_id) is int and all(type(a.asset_id) is int for a in locator.assets),"metadata_github_ids_required")
                    assets={a.name:a for a in locator.assets}
                    require(self.manifest_name in assets and self.signature_name in assets,"signed_asset_pair_required")
                    m,s=assets[self.manifest_name],assets[self.signature_name]
                    message=self._fetch(m.url,cancel);signature=self._fetch(s.url,cancel)
                    require(len(message)==m.declared_size and len(signature)==s.declared_size,"metadata_asset_size_binding")
                    verified=verify_component_release(message,signature,self.trust,expected_component=component,
                        expected_version=locator.software_version,expected_channel=channel,expected_os=self.os,
                        expected_architecture=self.arch,expected_maturity=locator.maturity)
                    require((verified.schema_version,verified.maturity)==(locator.schema_version,locator.maturity),"metadata_schema_stage_binding")
                    require(verified.artifact.size_bytes<=self.maximum,"artifact_bound")
                    source=assets.get(verified.artifact.name)
                    require(source is not None and source.declared_size==verified.artifact.size_bytes,"signed_asset_binding")
                    candidates.append(ComponentOffer(verified,"install" if context.version is None else "update",
                        json.loads(message.decode('utf-8-sig'))['release_notes'],message,signature,m.url,s.url,
                        ReleaseAssetSource(source.name,source.url,source.declared_size),self.trust_pin,context.version,
                        locator,repository_id,context,self._offer_epoch))
        selected=None
        if candidates:
            require(len({o.release.schema_version for o in candidates})==1,"mixed_schema_candidate_group")
            selected=candidates[0]
            def compare(left,right):
                if left.schema_version==2:
                    return ComponentReleaseIdentity(left.software_version,left.maturity).compare(
                        ComponentReleaseIdentity(right.software_version,right.maturity))
                return _version(left.software_version).compare(_version(right.software_version))
            for offer in candidates[1:]:
                order=compare(offer.release,selected.release)
                if order==0:
                    require(offer.release==selected.release,"conflicting_equal_release_identity")
                if order>0:selected=offer
            release=selected.release
            if context.version is not None:
                if release.schema_version==2:
                    floor=json.loads(selected.manifest_bytes.decode('utf-8-sig'))['supported_from']
                    require_component_maturity_upgrade(context.version,ComponentReleaseIdentity(release.software_version,release.maturity),
                        current_schema=context.schema_version,current_maturity=context.maturity,supported_from=floor)
                else:
                    require(context.schema_version==1,"schema_regression_not_admitted")
                    verified=verify_release_manifest(selected.manifest_bytes,selected.signature_bytes,json.loads(self.trust),expected_channel=channel)
                    ensure_supported_upgrade(context.version,verified)
            if self._expected_offer is not None:self._expected_offer.check_offer(selected)
        self._check(cancel);self._trust()
        require((self.trust,self.trust_pin)==trust_before,"changed_metadata_trust")
        return () if selected is None else (selected,)

    def _discover_metadata(self, channel, versions, component, cancel):
        self._offer_epoch+=1;self._offers={}
        epoch=self._offer_epoch
        self._check(cancel);self._trust()
        require(type(versions) is dict and type(component) is ComponentId,"explicit_metadata_component_scope")
        require(type(channel) is str and channel in ('stable','beta'),"explicit_metadata_channel")
        if self._expected_offer is not None:
            require((component,channel)==(self._expected_offer.component_id,self._expected_offer.channel),"exact_metadata_scope")
        context=self._installed_context(component,versions.get(component.value))
        offers=self._select_metadata(channel,component,context,cancel)
        self._check(cancel)
        require(self._offer_epoch==epoch and self._installed_context(component,context.version)==context,
            "changed_metadata_publication_context")
        self._trust()
        require(all(o.trust_sha256==self.trust_pin for o in offers),"changed_metadata_trust")
        self._check(None)
        self._offers={o.release.manifest_sha256:o for o in offers}
        return offers

    def _revalidate_metadata(self, offer, should_cancel):
        require(type(offer) is ComponentOffer and self._offers.get(offer.release.manifest_sha256) is offer,
            "current_signed_offer_required")
        try:
            self._trust();self._check(should_cancel)
            require(offer.discovery_epoch==self._offer_epoch and type(offer.installed_context) is InstalledOfferContext,"stale_offer_epoch")
            context=self._installed_context(offer.release.component_id,offer.current_version)
            require(context==offer.installed_context,"changed_installed_context")
            selected=self._select_metadata(offer.release.channel,offer.release.component_id,context,should_cancel)
            require(len(selected)==1 and selected[0]==offer,"changed_authenticated_offer")
            self._check(should_cancel)
            require(self._offer_epoch==offer.discovery_epoch and
                self._installed_context(offer.release.component_id,offer.current_version)==context,
                "changed_metadata_publication_context")
            self._trust();require(offer.trust_sha256==self.trust_pin,"changed_metadata_trust")
            self._check(None)
            return
        except BaseException:
            self._offers={};self._offer_epoch+=1
            raise

    def revalidate_offer(self, offer, *, should_cancel=None):
        """Before confirmation and acquisition: fresh identity, bytes and crypto."""
        if self.schema2_metadata:
            return self._revalidate_metadata(offer,should_cancel)
        self._trust()
        self._check(should_cancel)
        require(type(offer) is ComponentOffer and self._offers.get(offer.release.manifest_sha256) is offer,
                "current_signed_offer_required")
        expected = self._expected_offer
        if expected is None: return
        expected.check_offer(offer)
        listing, repository_id = self._listing(should_cancel)
        selected = discover_releases(listing, component_id=expected.component_id, channel=expected.channel,
            current_version=offer.current_version, complete=True).candidate
        expected.check_locator(selected)
        if selected != offer.locator or repository_id != offer.repository_id:
            raise ExactOfferHold(EXACT_OFFER_HOLD)
        message = self._fetch(offer.manifest_url, should_cancel)
        signature = self._fetch(offer.signature_url, should_cancel)
        if message != offer.manifest_bytes or signature != offer.signature_bytes:
            raise ExactOfferHold(EXACT_OFFER_HOLD)
        verified = verify_component_release(message, signature, self.trust,
            expected_component=expected.component_id, expected_version=expected.software_version,
            expected_channel=expected.channel, expected_os=expected.os, expected_architecture=expected.architecture)
        if verified != offer.release: raise ExactOfferHold(EXACT_OFFER_HOLD)
        self._check(should_cancel)

    def _metadata_offer_current(self, offer, should_cancel):
        self._check(should_cancel)
        self._check(None)  # Recheck owner bytes after the cancellation callback.
        self._trust()
        require(type(offer) is ComponentOffer
            and self._offers.get(offer.release.manifest_sha256) is offer,
            "current_signed_offer_required")
        require(offer.discovery_epoch == self._offer_epoch
            and type(offer.installed_context) is InstalledOfferContext
            and not offer.installed_context.pending, "stale_offer_epoch")
        require(offer.trust_sha256 == self.trust_pin, "changed_metadata_trust")

    def revalidate_exact_metadata(self, offer, *, check_current, should_cancel=None):
        """Read-only fresh metadata; callback supplies no installation capability.

        The original host must bracket this operation with its own authority checks.
        Ordinary discovery/revalidation retain their live pending-state rejection.
        """
        try:
            require(self.schema2_metadata and callable(check_current), "explicit_metadata_review_required")
            self._metadata_offer_current(offer, should_cancel)
            check_current()
            self._metadata_offer_current(offer, should_cancel)
            selected = self._select_metadata(offer.release.channel, offer.release.component_id,
                                             offer.installed_context, should_cancel)
            require(len(selected) == 1 and selected[0] == offer, "changed_authenticated_offer")
            self._check(should_cancel)
            check_current()
            self._metadata_offer_current(offer, should_cancel)
        except BaseException:
            self._offers = {}
            self._offer_epoch += 1
            raise

    def acquire(self, offer, *, progress=None, should_cancel=None):
        require(not (type(offer) is ComponentOffer and type(offer.release) is VerifiedComponentRelease
            and offer.release.schema_version==2),"schema2_installation_not_admitted")
        self._trust()
        self._check(should_cancel)
        require(type(offer) is ComponentOffer and self._offers.get(offer.release.manifest_sha256) is offer,
                "current_signed_offer_required")
        require(offer.trust_sha256 == self.trust_pin, "changed_trust_evidence")
        self.revalidate_offer(offer, should_cancel=should_cancel)
        return self._acquire_artifact(offer, progress=progress, should_cancel=should_cancel)

    def _acquire_original_metadata(self, offer, *, check_current, progress=None, should_cancel=None):
        """Private host composition; callback/DTO alone is no installation authority."""
        try:
            self.revalidate_exact_metadata(offer, check_current=check_current, should_cancel=should_cancel)
            def original():
                check_current()
                self._metadata_offer_current(offer, should_cancel)
            download = self.transport.download_artifact
            if offer.release.schema_version == 2:
                from core.component_release_transport import ComponentReleaseTransport
                require(type(self.transport.opener) is ComponentReleaseTransport,
                        "original_scoped_artifact_transport_required")
                original()
                scoped = self.transport.opener.for_exact_artifact(offer.artifact_source)
                download = ApplicationUpdateService(self.root, self.transport.current_version,
                                                     opener=scoped).download_artifact
            return self._acquire_artifact(offer, progress=progress, should_cancel=should_cancel,
                                          check_current=original, download_artifact=download)
        except BaseException:
            self._offers = {}
            self._offer_epoch += 1
            raise

    def _validate_acquired(self, acquired):
        require(type(acquired) is AcquiredComponent, "original_acquired_component_required")
        offer = acquired.offer
        self._check(None); self._trust()
        require(type(offer) is ComponentOffer
            and self._offers.get(offer.release.manifest_sha256) is offer
            and offer.trust_sha256 == self.trust_pin
            and self._acquired.get(offer.release.manifest_sha256) == acquired.artifact,
            "original_service_artifact_required")
        self._owned_artifact_path(acquired.artifact, offer.release)
        verify_component_artifact(acquired.artifact, offer.release)
        self._check(None); self._trust()
        require(self._offers.get(offer.release.manifest_sha256) is offer
            and offer.trust_sha256 == self.trust_pin
            and self._acquired.get(offer.release.manifest_sha256) == acquired.artifact,
            "original_service_artifact_required")
        if self.schema2_metadata:
            self._metadata_offer_current(offer, None)

    def _owned_artifact_path(self, artifact, release):
        require(isinstance(artifact, Path) and artifact.parent.parent == self.root
            and artifact.parent.name.startswith('attempt-') and artifact.name == release.artifact.name,
            "cache_artifact_binding")
        guard(artifact)

    def _acquire_artifact(self, offer, *, progress, should_cancel, check_current=None, download_artifact=None):
        def check():
            if check_current is not None:
                check_current()
            self._check(should_cancel); self._check(None); self._trust()
            require(self._offers.get(offer.release.manifest_sha256) is offer
                and offer.trust_sha256 == self.trust_pin, "changed_inflight_offer")
        check()
        # Fresh exact metadata prevents mutation between review and transfer.
        require(self._fetch(offer.manifest_url, should_cancel) == offer.manifest_bytes
                and self._fetch(offer.signature_url, should_cancel) == offer.signature_bytes,
                "changed_offer_metadata")
        verified = verify_component_release(offer.manifest_bytes, offer.signature_bytes, self.trust,
            expected_component=offer.release.component_id, expected_version=offer.release.software_version,
            expected_channel=offer.release.channel, expected_os=self.os, expected_architecture=self.arch,
            expected_maturity=offer.release.maturity)
        require(verified == offer.release and verified.artifact.size_bytes <= self.maximum, "changed_verified_offer")
        check()
        cached = self._acquired.get(verified.manifest_sha256)
        if cached is not None:
            self._owned_artifact_path(cached, verified)
            verify_component_artifact(cached, verified)
            check()
            return AcquiredComponent(offer, cached)
        used = sum(path.stat().st_size for path in self._acquired.values())
        require(used + verified.artifact.size_bytes <= self.cache_maximum, "cache_bound")
        attempt = self.root / ("attempt-" + uuid4().hex)
        attempt.mkdir()
        names = [verified.artifact.name, verified.artifact.name + ".part", verified.artifact.name + ".part.rejected"]
        try:
            download = self.transport.download_artifact if download_artifact is None else download_artifact
            artifact = download(offer.artifact_source, verified.artifact,
                cache_directory=attempt, progress=progress, should_cancel=should_cancel)
            self._check(should_cancel)
            self._trust()
            require(offer.trust_sha256 == self.trust_pin
                    and self._offers.get(offer.release.manifest_sha256) is offer, "changed_inflight_offer")
            guard(artifact)
            require(artifact.parent == attempt and artifact.name == verified.artifact.name, "cache_artifact_binding")
            verify_component_artifact(artifact, verified)
            check()
            self._acquired[verified.manifest_sha256] = artifact
            return AcquiredComponent(offer, artifact)
        except BaseException:
            # Exact files in this fresh owned attempt only; no recursive cleanup.
            self._check(None)
            for name in names:
                path = attempt / name
                guard(path)
                path.unlink(missing_ok=True)
            attempt.rmdir()
            raise
