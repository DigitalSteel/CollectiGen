"""Pure normalization of host-supplied version facts; no catalog or collection.

The host retains trust, transport, verification and refresh-worker ownership.
No normalizer calls discovery, verification, settings or native catalog methods.
"""
from datetime import timedelta
import json

from core.connected_version_observation import (
    VersionBinding, VersionObservation, ReleaseComparisonObservation,
    derive_version_status, _require, _time,
)


def observe_ollama_version(binding, report):
    """Normalize the already-returned VERSION read, preserving failure and age."""
    from suite_core.discovery.ollama_readonly import DiscoveryReport, ReadRoute
    _require(type(report) is DiscoveryReport and type(binding) is VersionBinding,
             'supplied_ollama_report_required')
    _require(binding.connection_ref == report.connection.connection_ref.value,
             'ollama_connection_mismatch')
    _require(binding.source_identity == report.binding.endpoint
             and binding.binding_revision == report.binding.approval_ref.value
             and report.binding.owner == report.connection.owner
             and report.binding.connection_ref == report.connection.connection_ref,
             'ollama_source_binding_mismatch')
    value = report.observation(ReadRoute.VERSION)
    state = {'available': 'observed', 'unavailable': 'unreachable',
             'unsupported': 'unsupported_endpoint', 'error': 'invalid', 'unknown': 'unknown'}[value.state.value]
    # Successful reads use completion time; failed refresh retains the old success.
    return VersionObservation(binding, 'running', state, 'provider_reported', value.version,
        'ollama:/api/version', value.attempted_at, value.observed_at, report.limits.valid_for,
        failure_code=value.failure_code, evidence_ref=value.response_sha256)


def observe_reader_version(binding, selection, *, observed_at, valid_for=timedelta(seconds=30),
                           installation_condition='unchanged'):
    """Host supplies review_directory's successful result, never saved settings alone."""
    _require(binding.version_kind == 'software', 'reader_software_version_required')
    _require(binding.source_identity is not None
             and binding.source_identity == str(selection.directory)
             and binding.binding_revision == selection.inventory_sha256,
             'reader_source_binding_mismatch')
    _require(type(selection.inventory_sha256) is str and len(selection.inventory_sha256) == 64
             and all(c in '0123456789abcdef' for c in selection.inventory_sha256),
             'reviewed_reader_inventory_required')
    return VersionObservation(binding, 'installed', 'observed', 'locally_verified', selection.version,
        'reader-inventory:' + selection.inventory_sha256, observed_at, observed_at, valid_for,
        comparator_id='semver', installation_condition=installation_condition,
        evidence_ref=selection.inventory_sha256)


def observe_default_content(binding, observation, *, observed_at, valid_for=timedelta(seconds=30)):
    """Normalize default_content() result; schema_version is deliberately unused."""
    _require(binding.version_kind == 'content', 'default_content_binding_required')
    approved = observation.approved_release
    version = observation.installed_version
    if version is None or approved is None:
        return VersionObservation(binding, 'installed', 'unknown', 'configured', None,
            'native-default-content', observed_at, None, valid_for)
    _require(type(approved) is dict and approved.get('content_version') == version
             and approved.get('pack_id') == binding.component_id,
             'approved_default_content_identity_mismatch')
    from core.default_catalog_release import ApprovedDefaultPackRelease
    # Reuse canonical release/fingerprint validation; preserve the host's ledger.
    decoded = ApprovedDefaultPackRelease.decode(json.dumps({
        key: approved.get(key) for key in ApprovedDefaultPackRelease.__dataclass_fields__}))
    native_pin = approved.get('native_pack_fingerprint')
    _require(type(native_pin) is str and len(native_pin) == 64
             and all(c in '0123456789abcdef' for c in native_pin),
             'approved_native_pack_fingerprint_required')
    # Exact content identity supports equality only; no lexical release ordering.
    return VersionObservation(binding, 'installed', 'observed', 'locally_verified', version,
        'native-default-content', observed_at, observed_at, valid_for,
        comparator_id='exact', installation_condition='unchanged',
        evidence_ref=decoded.release_fingerprint,
        comparison_identity=version + '@' + decoded.release_fingerprint)


def newer_offer_comparison(binding, *, source_ref, checked_at, latest_identity=None,
                           authoritative=False, comparator_id=None,
                           matching_artifact_available=None, evidence_ref=None,
                           valid_for=timedelta(seconds=30)):
    """Adapt newer-only discovery; empty/equal results can never prove Current.

An offer proves a newer applicable release only if its existing owner supplied
admitted authority and platform evidence. This helper authenticates nothing.
"""
    return ReleaseComparisonObservation(binding, 'unknown' if latest_identity is None else 'observed',
        source_ref, checked_at, valid_for, latest_identity, comparator_id,
        complete=False, authoritative=authoritative,
        matching_artifact_available=matching_artifact_available, evidence_ref=evidence_ref)


def project_connected_versions(bindings, observations=(), comparisons=(), *, now):
    """One bounded row per caller-selected binding, including unsupported/unknown.

No second registry: the exact selection comes from the host's existing owners.
Duplicate or foreign evidence is rejected rather than silently selected.
"""
    _time(now)
    for values, kind in ((bindings, VersionBinding), (observations, VersionObservation),
                         (comparisons, ReleaseComparisonObservation)):
        _require(type(values) is tuple and len(values) <= (128 if kind is VersionObservation else 64)
                 and all(type(v) is kind for v in values), 'bounded_version_inputs_required')
    by_identity = {b.identity: b for b in bindings}
    _require(len(by_identity) == len(bindings), 'duplicate_version_binding')
    local, remote = {}, {}
    for value in observations:
        _require(by_identity.get(value.binding.identity) == value.binding, 'foreign_version_observation')
        key = (value.binding.identity, value.subject)
        _require(key not in local, 'duplicate_version_observation')
        local[key] = value
    for value in comparisons:
        _require(value.binding.identity in by_identity, 'foreign_release_comparison')
        _require(value.binding.identity not in remote, 'duplicate_release_comparison')
        remote[value.binding.identity] = value
    return tuple(derive_version_status(b, local.get((b.identity, 'installed')),
        local.get((b.identity, 'running')), remote.get(b.identity), now=now) for b in bindings)
