"""Prepare a signed Tag-data source using stock extraction; never activate/apply.

Public trust and InstalledComponent declarations are explicit host admissions:
neither is carried by AcquiredTagDefault or declared by the local data inventory.
No feed, trust enrollment, registry write, recipient catalog or cleanup is added.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path

from core.component_acquisition import AcquiredComponent, ComponentOffer
from core.tag_default_acquisition import AcquiredTagDefault, TagDefaultBaseline, TAG_DATABASE_COMPONENT
from release_contract import verify_artifact
from suite_core.discovery.component_release_signature import verify_component_release
from suite_core.discovery.component_staging import (
    guard, require, verify_directory, extract_component_archive,
)
from suite_core.installation import InstalledComponent, LocalInventory, ComponentKind, _path
from suite_core.encyclopedia.publication import read
from suite_core.knowledge.native_tag_catalog_host import bind_foundation_source_release


@dataclass(frozen=True)
class PreparedTagDefaultPackage:
    root: Path  # Disposable cache container, not an activated installation.
    source_directory: Path
    component: InstalledComponent  # Host declaration, verified against signed inventory.
    pack: Path
    foundation: object
    source_snapshot: object
    release: object
    baseline: TagDefaultBaseline

    @property
    def production_install_authorized(self):
        return False


def prepare_tag_default_package(acquired, destination, should_cancel=None, *,
                                trust_bytes=None, component=None, acquisition_cache_root=None):
    """Return verified local source metadata; retain partial output on failure.

    destination must be a NEW absolute disposable cache directory with an
    existing parent, disjoint from the whole original acquisition cache. Host
    supplies acquisition_cache_root from its retained ComponentAcquisition.root,
    never inferred from a chosen destination. Its original owner marker and the
    stock attempt/artifact layout are checked before writes. Extraction
    goes under destination/Apps so the stock host's APP/LocalInventory contract
    can represent TagDatabase/data without moving or activating any files.
    Host must supply exact Apps/TagDatabase/data InstalledComponent metadata;
    missing trust/declarations refuse before filesystem mutation. Cancellation
    is checked before work, after each stock-extracted member and decoding.
    As with stock staging, the host owns/excludes the disposable cache. Path
    guards and before/after hashes are not hostile concurrent handle security.
    """
    require(type(acquired) is AcquiredTagDefault
        and type(acquired.acquired) is AcquiredComponent
        and type(acquired.acquired.offer) is ComponentOffer
        and type(acquired.baseline) is TagDefaultBaseline, "original_tag_acquisition_evidence_required")
    stock = acquired.acquired
    offer = stock.offer
    require(acquired.baseline.observation_status in {"known", "verified_empty"}
        and offer.current_version == acquired.baseline.distribution_version
        and offer.operation == ("install" if acquired.baseline.observation_status == "verified_empty" else "update"),
        "admitted_tag_acquisition_baseline_required")
    require(type(trust_bytes) is bytes and sha256(trust_bytes).hexdigest() == offer.trust_sha256,
        "explicit_matching_public_trust_required")
    verified = verify_component_release(offer.manifest_bytes, offer.signature_bytes, trust_bytes,
        expected_component=TAG_DATABASE_COMPONENT, expected_version=offer.release.software_version,
        expected_channel=offer.release.channel, expected_os=offer.release.os,
        expected_architecture=offer.release.architecture)
    require(verified == offer.release, "original_verified_tag_release_required")
    require(type(component) is InstalledComponent and component.component_id == TAG_DATABASE_COMPONENT
        and component.kind is ComponentKind.APP and type(component.location) is LocalInventory
        and component.location.relative_directory == "Apps/TagDatabase/data"
        and component.location.manifest == "BOOTSTRAP_INVENTORY.json"
        and component.software_version == verified.software_version
        and component.location.manifest_sha256 == verified.artifact.portable_manifest_sha256,
        "explicit_matching_tag_component_declaration_required")
    require(isinstance(acquisition_cache_root, Path), "original_acquisition_cache_root_required")
    cache_root = _path(acquisition_cache_root)
    guard(cache_root)
    marker = cache_root / ".component-acquisition-owner.json"
    guard(marker)
    require(marker.is_file() and marker.stat().st_size <= 1024, "original_acquisition_cache_marker_required")
    owner = json.loads(marker.read_bytes())
    require(type(owner) is dict and set(owner) == {"purpose", "owner"}
        and owner["purpose"] == "COMPONENT_ACQUISITION_CACHE"
        and type(owner["owner"]) is str and 1 <= len(owner["owner"]) <= 128,
        "original_acquisition_cache_marker_required")
    destination = _path(Path(destination))
    artifact = _path(Path(stock.artifact))
    guard(destination)
    guard(artifact)
    require(artifact.parent.parent == cache_root and artifact.parent.name.startswith("attempt-")
        and len(artifact.parent.name) == 40
        and all(char in "0123456789abcdef" for char in artifact.parent.name[8:]),
        "original_acquisition_cache_artifact_required")
    require(artifact.name == verified.artifact.name, "original_tag_artifact_name_required")
    require(not destination.exists() and destination.parent.is_dir(), "new_disposable_cache_required")
    require(not destination.is_relative_to(cache_root)
        and not cache_root.is_relative_to(destination), "disjoint_package_cache_required")
    def check():
        if should_cancel is not None and should_cancel():
            raise InterruptedError("Tag package preparation cancelled; retain partial cache for owner cleanup")
        guard(destination)
        guard(artifact)
    check()
    verify_artifact(artifact, verified.artifact)
    destination.mkdir()
    extraction = destination / "Apps"
    extraction.mkdir()
    extract_component_archive(artifact, extraction, member_extracted=check)
    check()
    require(tuple(path.name for path in extraction.iterdir()) == ("TagDatabase",),
        "exact_tag_delivery_root_required")
    source = extraction / "TagDatabase/data"
    def validate():
        check()
        profile = verify_directory(source, component.location.manifest_sha256,
            TAG_DATABASE_COMPONENT, component.software_version)
        require(profile["platform"] == {"os": verified.os, "architecture": verified.architecture},
            "tag_inventory_platform_mismatch")
        verify_artifact(artifact, verified.artifact)
    validate()
    pack = source / "TagDatabase.zip"
    companion = source / "metadata/TagDatabase.source-snapshot.json"
    guard(pack)
    guard(companion)
    # Import codec only after source/signature/closure admission; no host/store.
    from core import knowledge_pack as native
    foundation = native.load_foundation_pack(pack)
    snapshot = read(companion).snapshot
    release = bind_foundation_source_release(foundation, snapshot)
    require(release.revision.pack_id == acquired.baseline.pack_id,
        "tag_foundation_pack_identity_mismatch")
    validate()  # Exact archive/inventory closure again after both native decoders.
    check()  # Cancellation may arrive during the final inventory/artifact reads.
    return PreparedTagDefaultPackage(destination, source, component, pack,
        foundation, snapshot, release, acquired.baseline)
