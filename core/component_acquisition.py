"""Explicit component acquisition only: no default trust/network, install or launch."""
from dataclasses import dataclass
from hashlib import sha256
import json
import re
from pathlib import Path
from uuid import uuid4
from release_contract import verify_release_manifest, ensure_supported_upgrade

from core.application_update import ApplicationUpdateService, ReleaseAssetSource, MANIFEST_ASSET, SIGNATURE_ASSET
from suite_core.discovery.github_component_releases import (
    REPOSITORY, MAX_BYTES, MAX_RELEASES, ReleaseLocator, release_asset_identity,
    discover_component_ids, discover_releases, _pairs, _constant, _version,
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
class ExactOfferAsset:
    name: str
    size_bytes: int
    sha256: str
    url: str
    asset_id: int

    def __post_init__(self):
        require(type(self.size_bytes) is int and 0 < self.size_bytes <= 2**40, "exact_asset_size")
        require(type(self.asset_id) is int and 0 < self.asset_id < 2**63, "exact_asset_id")
        require(type(self.sha256) is str and re.fullmatch('[0-9a-f]{64}', self.sha256), "exact_asset_hash")
        require(release_asset_identity(self.url)[1] == self.name, "exact_asset_url")


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

    def __post_init__(self):
        require(type(self.component_id) is ComponentId and self.repository == REPOSITORY, "exact_offer_repository_component")
        version = _version(self.software_version)
        require(bool(version.prerelease) == (self.channel == "beta"), "exact_offer_channel_version")
        require(self.tag == self.component_id.value + '/v' + self.software_version, "exact_offer_tag")
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
        require(all(release_asset_identity(a.url) == (self.tag, a.name) for a in assets), "exact_offer_asset_tag")

    @classmethod
    def from_reviewed_offer(cls, offer, *, repository_id):
        """Explicit caller preparation only; never enroll from a live feed automatically."""
        require(type(offer) is ComponentOffer and type(offer.locator) is ReleaseLocator, "reviewed_offer_locator_required")
        assets = {a.name: a for a in offer.locator.assets}
        def pin(name, raw=None):
            a = assets[name]
            digest = offer.release.artifact.sha256 if raw is None else sha256(raw).hexdigest()
            return ExactOfferAsset(name, a.declared_size, digest, a.url, a.asset_id)
        return cls(offer.release.component_id, offer.release.software_version, offer.release.channel,
            offer.release.os, offer.release.architecture, offer.release.key_id, offer.trust_sha256,
            repository_id, offer.locator.release_id, offer.locator.tag,
            pin(release_asset_identity(offer.manifest_url)[1], offer.manifest_bytes),
            pin(release_asset_identity(offer.signature_url)[1], offer.signature_bytes),
            pin(offer.release.artifact.name))

    def check_locator(self, locator):
        expected = {a.name: (a.url, a.size_bytes, a.asset_id) for a in (self.manifest, self.signature, self.artifact)}
        if (type(locator) is not ReleaseLocator or
                (locator.component_id, locator.software_version, locator.channel, locator.tag, locator.release_id) !=
                (self.component_id, self.software_version, self.channel, self.tag, self.release_id) or
                {a.name: (a.url, a.declared_size, a.asset_id) for a in locator.assets} != expected):
            raise ExactOfferHold(EXACT_OFFER_HOLD)

    def check_offer(self, offer):
        self.check_locator(offer.locator)
        r = offer.release
        if ((r.component_id, r.software_version, r.channel, r.os, r.architecture, r.key_id) !=
                (self.component_id, self.software_version, self.channel, self.os, self.architecture, self.key_id) or
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
                 manifest_name=MANIFEST_ASSET, signature_name=SIGNATURE_ASSET, expected_offer=None):
        require(expected_offer is None or type(expected_offer) is ExactComponentOffer, "typed_exact_offer_required")
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
        # The default app trust path is never read: only transport methods are used.
        self.transport = ApplicationUpdateService(root, host_version, opener=opener)
        self._offers = {}

    def _check(self, cancel):
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
        if self._expected_offer is not None:
            raw = self._fetch(f"https://api.github.com/repos/{REPOSITORY}", should_cancel, MAX_BYTES)
            value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
            repository_id = value.get('id') if type(value) is dict else None
            if type(repository_id) is not int or repository_id != self._expected_offer.repository_id or value.get('full_name') != REPOSITORY:
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

    def revalidate_offer(self, offer, *, should_cancel=None):
        """Before confirmation and acquisition: fresh identity, bytes and crypto."""
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

    def acquire(self, offer, *, progress=None, should_cancel=None):
        self._trust()
        self._check(should_cancel)
        require(type(offer) is ComponentOffer and self._offers.get(offer.release.manifest_sha256) is offer,
                "current_signed_offer_required")
        require(offer.trust_sha256 == self.trust_pin, "changed_trust_evidence")
        self.revalidate_offer(offer, should_cancel=should_cancel)
        # Fresh exact metadata prevents mutation between review and transfer.
        require(self._fetch(offer.manifest_url, should_cancel) == offer.manifest_bytes
                and self._fetch(offer.signature_url, should_cancel) == offer.signature_bytes,
                "changed_offer_metadata")
        verified = verify_component_release(offer.manifest_bytes, offer.signature_bytes, self.trust,
            expected_component=offer.release.component_id, expected_version=offer.release.software_version,
            expected_channel=offer.release.channel, expected_os=self.os, expected_architecture=self.arch)
        require(verified == offer.release and verified.artifact.size_bytes <= self.maximum, "changed_verified_offer")
        cached = self._acquired.get(verified.manifest_sha256)
        if cached is not None:
            guard(cached)
            verify_component_artifact(cached, verified)
            return AcquiredComponent(offer, cached)
        used = sum(path.stat().st_size for path in self._acquired.values())
        require(used + verified.artifact.size_bytes <= self.cache_maximum, "cache_bound")
        attempt = self.root / ("attempt-" + uuid4().hex)
        attempt.mkdir()
        names = [verified.artifact.name, verified.artifact.name + ".part", verified.artifact.name + ".part.rejected"]
        try:
            artifact = self.transport.download_artifact(offer.artifact_source, verified.artifact,
                cache_directory=attempt, progress=progress, should_cancel=should_cancel)
            self._check(should_cancel)
            self._trust()
            require(offer.trust_sha256 == self.trust_pin
                    and self._offers.get(offer.release.manifest_sha256) is offer, "changed_inflight_offer")
            guard(artifact)
            require(artifact.parent == attempt and artifact.name == verified.artifact.name, "cache_artifact_binding")
            verify_component_artifact(artifact, verified)
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
