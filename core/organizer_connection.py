"""Read-only, explicit Organizer package review; no settings, install or launch.

Signed release identity and exact directory inventory are separate from native
execution readiness. The owning host must retain its ordinary root/idle custody.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path

from core.connected_version_observation import VersionBinding, VersionObservation
from suite_core.installation import ComponentId
from suite_core.discovery.component_payload_policy import select_payload_policy
from suite_core.discovery.component_release_signature import verify_component_release
from suite_core.discovery.component_staging import guard, verify_directory

COMPONENT = ComponentId("collectigen.media-organizer")
POLICY = select_payload_policy(COMPONENT, "windows", "x86_64", "interpreter-portable")
REQUIRED_ENTRIES = frozenset({"Start_MediaOrganizer.bat", "Runtime/python/python.exe",
                              "suite_core/media_organizer/__main__.py"})


class OrganizerConnectionHold(ValueError):
    pass


@dataclass(frozen=True)
class OrganizerConnection:
    directory: Path
    version: str
    inventory_sha256: str
    manifest_sha256: str
    binding_revision: str

    @property
    def execution_authorized(self):
        return False

    @property
    def installation_authorized(self):
        return False

    @property
    def identity(self):
        raw = json.dumps([str(self.directory), self.version, self.inventory_sha256,
                          self.manifest_sha256, self.binding_revision], separators=(",", ":"))
        return sha256(raw.encode("utf-8")).hexdigest()


def review_existing_package(directory, *, manifest_bytes, signature_bytes, trust_bytes,
                            expected_version, binding_revision, expected=None):
    """Recheck one explicitly selected package; preserve stock verifier failures.

    No directory discovery or profile/data read. Optional expected binds a second
    review to the exact prior root, signed inventory and host connection revision.
    This check does not provide adversarial filesystem locking or launch permits.
    """
    if type(binding_revision) is not str or not 0 < len(binding_revision) <= 256:
        raise OrganizerConnectionHold("connection_revision_required")
    root = Path(directory)
    guard(root)
    if not root.is_dir():
        raise OrganizerConnectionHold("selected_package_directory_required")
    root = root.resolve(strict=True)
    release = verify_component_release(manifest_bytes, signature_bytes, trust_bytes,
        expected_component=COMPONENT, expected_version=expected_version,
        expected_channel="beta", expected_os="windows", expected_architecture="x86_64")
    selected = OrganizerConnection(root, expected_version,
        release.artifact.portable_manifest_sha256, release.manifest_sha256, binding_revision)
    if expected is not None and (type(expected) is not OrganizerConnection or selected != expected):
        raise OrganizerConnectionHold("connected_package_changed_review_required")
    profile = verify_directory(root, selected.inventory_sha256, COMPONENT,
                               expected_version, payload_policy=POLICY)
    if profile["platform"] != {"os": "windows", "architecture": "x86_64"}:
        raise OrganizerConnectionHold("organizer_windows_x64_required")
    if not REQUIRED_ENTRIES <= {row["path"] for row in profile["files"]}:
        raise OrganizerConnectionHold("organizer_entry_contract_missing")
    return selected


def observe_installed_version(selected, *, manifest_bytes, signature_bytes, trust_bytes,
                              binding_revision, now, valid_for=timedelta(seconds=30)):
    """Fresh package evidence only; no source-version or running-state inference."""
    if type(selected) is not OrganizerConnection:
        raise OrganizerConnectionHold("reviewed_connection_required")
    if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
        raise OrganizerConnectionHold("aware_observation_time_required")
    refreshed = review_existing_package(selected.directory, manifest_bytes=manifest_bytes,
        signature_bytes=signature_bytes, trust_bytes=trust_bytes,
        expected_version=selected.version, binding_revision=binding_revision, expected=selected)
    binding = VersionBinding(COMPONENT.value, refreshed.identity, binding_revision,
        connection_ref=refreshed.identity, platform="windows", architecture="x86_64",
        channel="beta", source_identity=refreshed.manifest_sha256)
    return VersionObservation(binding, "installed", "observed", "locally_verified",
        refreshed.version, "selected_package_signed_inventory", now, now, valid_for,
        comparator_id="semver", installation_condition="unchanged",
        evidence_ref=refreshed.inventory_sha256)
