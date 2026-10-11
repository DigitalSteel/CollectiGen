"""Explicit component acquisition only: no default trust/network, install or launch."""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4
from release_contract import verify_release_manifest, ensure_supported_upgrade

from core.application_update import ApplicationUpdateService, ReleaseAssetSource, MANIFEST_ASSET, SIGNATURE_ASSET
from suite_core.discovery.github_component_releases import (
    REPOSITORY, MAX_BYTES, MAX_RELEASES, discover_component_ids, discover_releases, _pairs, _constant,
)
from suite_core.discovery.component_release_signature import (
    verify_component_release, verify_component_artifact, MAX_METADATA_BYTES, VerifiedComponentRelease,
)
from suite_core.discovery.component_staging import guard, require


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
                 manifest_name=MANIFEST_ASSET, signature_name=SIGNATURE_ASSET):
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

    def discover(self, *, channel, installed_versions, should_cancel=None):
        self._trust()
        require(type(installed_versions) is dict, "explicit_installed_versions")
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
        listing = json.dumps(rows, separators=(",", ":")).encode()
        offers = []
        # Commit offers only after complete successful signed discovery.
        for component in discover_component_ids(listing, complete=True):
            current = installed_versions.get(component.value)
            selected = discover_releases(listing, component_id=component, channel=channel,
                                         current_version=current, complete=True).candidate
            if selected is None:
                continue
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
                ReleaseAssetSource(source.name, source.url, source.declared_size), self.trust_pin, current)
            offers.append(offer)
        self._check(should_cancel)
        self._offers = {o.release.manifest_sha256: o for o in offers}
        return tuple(offers)

    def acquire(self, offer, *, progress=None, should_cancel=None):
        self._trust()
        self._check(should_cancel)
        require(type(offer) is ComponentOffer and self._offers.get(offer.release.manifest_sha256) is offer,
                "current_signed_offer_required")
        require(offer.trust_sha256 == self.trust_pin, "changed_trust_evidence")
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
