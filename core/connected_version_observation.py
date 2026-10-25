"""Pure software/content evidence; no discovery, storage or update authority.

Hosts supply observations after their existing admission/verification. Evidence
levels describe those assertions; these DTOs do not authenticate an installation.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from release_contract import SemVer
from suite_core.registry.status_view import Freshness, _freshness


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _text(value, *, optional=False, maximum=256):
    _require(optional and value is None or type(value) is str and 0 < len(value) <= maximum
             and value == value.strip() and not any(ord(c) < 32 for c in value),
             'bounded_version_text_required')


def _time(value):
    _require(type(value) is datetime and value.utcoffset() is not None,
             'aware_version_timestamp_required')


def _ttl(value):
    _require(type(value) is timedelta and timedelta(0) < value <= timedelta(days=1),
             'bounded_version_validity_required')


@dataclass(frozen=True)
class VersionBinding:
    component_id: str
    installation_ref: str
    binding_revision: str
    connection_ref: str | None = None
    version_kind: str = 'software'
    platform: str | None = None
    architecture: str | None = None
    channel: str | None = None
    source_identity: str | None = field(default=None, repr=False)

    def __post_init__(self):
        for key in ('component_id', 'installation_ref', 'binding_revision'):
            _text(getattr(self, key))
        for key in ('connection_ref', 'platform', 'architecture', 'channel'):
            _text(getattr(self, key), optional=True)
        _require(self.version_kind in {'software', 'content'}, 'version_kind_required')
        _text(self.source_identity, optional=True, maximum=4096)

    @property
    def identity(self):
        return (self.component_id, self.installation_ref, self.connection_ref,
                self.binding_revision, self.version_kind, self.source_identity)


@dataclass(frozen=True)
class VersionObservation:
    binding: VersionBinding
    subject: str
    state: str
    evidence_level: str
    raw_version: str | None
    source_ref: str
    attempted_at: datetime
    observed_at: datetime | None
    valid_for: timedelta
    comparator_id: str | None = None
    failure_code: str | None = None
    installation_condition: str = 'unknown'
    evidence_ref: str | None = None
    comparison_identity: str | None = None

    def __post_init__(self):
        _require(type(self.binding) is VersionBinding, 'exact_version_binding_required')
        _require(self.subject in {'installed', 'running'}, 'version_subject_required')
        _require(self.state in {'observed', 'missing', 'unknown', 'unreachable',
                               'unsupported_endpoint', 'invalid'}, 'version_state_required')
        _require(self.evidence_level in {'configured', 'provider_reported', 'locally_verified'},
                 'version_evidence_level_required')
        _require(self.installation_condition in {'unchanged', 'locally_modified',
                 'changed_requires_review', 'restart_pending', 'unknown'},
                 'installation_condition_required')
        _text(self.source_ref)
        # Keep canonical provider/CLI text intact, including whitespace and lines.
        # The existing llama-server capture is bounded to 65536 bytes.
        _require(self.raw_version is None or type(self.raw_version) is str
                 and 0 < len(self.raw_version) <= 65536, 'bounded_raw_version_required')
        _text(self.failure_code, optional=True)
        _text(self.evidence_ref, optional=True)
        _text(self.comparison_identity, optional=True, maximum=512)
        _require(self.comparison_identity is None or self.raw_version is not None,
                 'comparison_requires_version_evidence')
        _require(self.comparator_id in {None, 'semver', 'exact'}, 'unsupported_version_comparator')
        _time(self.attempted_at)
        _ttl(self.valid_for)
        if self.observed_at is not None:
            _time(self.observed_at)
        _require((self.raw_version is None) == (self.observed_at is None),
                 'version_evidence_timestamp_required')
        if self.state == 'observed':
            _require(self.raw_version is not None, 'observed_version_required')
            _require(self.observed_at >= self.attempted_at, 'version_timestamp_order')
        elif self.state not in {'unknown', 'missing'}:
            _require(self.failure_code is not None, 'version_failure_code_required')
        if self.raw_version is not None and self.comparator_id == 'semver':
            identity = self.comparison_identity or self.raw_version
            _text(identity, maximum=512)
            SemVer.parse(identity)

    def freshness_at(self, now):
        _time(now)
        return _freshness(self.observed_at, self.valid_for, now)


@dataclass(frozen=True)
class ReleaseComparisonObservation:
    binding: VersionBinding
    state: str
    source_ref: str
    checked_at: datetime | None
    valid_for: timedelta
    latest_identity: str | None = None
    comparator_id: str | None = None
    complete: bool = False
    authoritative: bool = False
    matching_artifact_available: bool | None = None
    failure_code: str | None = None
    evidence_ref: str | None = None

    def __post_init__(self):
        _require(type(self.binding) is VersionBinding, 'exact_version_binding_required')
        _require(self.state in {'observed', 'unknown', 'unreachable', 'unsupported', 'invalid'},
                 'release_comparison_state_required')
        _text(self.source_ref)
        _text(self.latest_identity, optional=True, maximum=512)
        _text(self.failure_code, optional=True)
        _text(self.evidence_ref, optional=True)
        _ttl(self.valid_for)
        if self.checked_at is not None:
            _time(self.checked_at)
        _require(type(self.complete) is bool and type(self.authoritative) is bool,
                 'release_evidence_flags_required')
        _require(self.matching_artifact_available is None
                 or type(self.matching_artifact_available) is bool, 'artifact_availability_required')
        _require(self.comparator_id in {None, 'semver', 'exact'}, 'unsupported_version_comparator')
        if self.state == 'observed':
            _require(self.latest_identity is not None and self.checked_at is not None,
                     'observed_release_identity_required')
        if self.latest_identity is not None and self.comparator_id == 'semver':
            SemVer.parse(self.latest_identity)


@dataclass(frozen=True)
class VersionStatus:
    binding: VersionBinding
    installed: VersionObservation | None
    running: VersionObservation | None
    release: ReleaseComparisonObservation | None
    comparison: str
    installation_condition: str
    reason: str


def derive_version_status(binding, installed=None, running=None, release=None, *, now):
    """Reconcile exact supplied evidence. Empty newer-offer results stay unknown."""
    _require(type(binding) is VersionBinding, 'exact_version_binding_required')
    _time(now)
    for value, kind, subject in ((installed, VersionObservation, 'installed'),
                                (running, VersionObservation, 'running')):
        if value is not None:
            _require(type(value) is kind and value.binding == binding and value.subject == subject,
                     'local_version_binding_mismatch')
    if release is not None:
        _require(type(release) is ReleaseComparisonObservation
                 and release.binding.identity == binding.identity, 'release_version_binding_mismatch')
    condition = installed.installation_condition if installed else 'unknown'
    if installed and running and all(v.state == 'observed' and
            v.freshness_at(now) is Freshness.FRESH for v in (installed, running)):
        if installed.comparator_id is not None and installed.comparator_id == running.comparator_id:
            left = installed.comparison_identity or installed.raw_version
            right = running.comparison_identity or running.raw_version
            different = (SemVer.parse(left).compare(SemVer.parse(right)) != 0
                         if installed.comparator_id == 'semver' else left != right)
            if different and condition == 'unchanged':
                condition = 'restart_pending'

    def result(status, reason):
        return VersionStatus(binding, installed, running, release, status, condition, reason)

    local = installed or running
    if local is None:
        return result('unknown', 'no_version_observation')
    if local.state != 'observed':
        status = {'unreachable': 'unreachable', 'unsupported_endpoint': 'unsupported'}.get(local.state, 'unknown')
        return result(status, 'local_' + local.state)
    if local.freshness_at(now) is not Freshness.FRESH:
        return result('unknown', 'local_' + local.freshness_at(now).value)
    if release is None:
        return result('unknown', 'no_release_comparison')
    if release.binding.channel != binding.channel:
        return result('channel_mismatch', 'release_channel_mismatch')
    if (release.binding.platform, release.binding.architecture) != (binding.platform, binding.architecture):
        return result('platform_mismatch', 'release_platform_mismatch')
    if release.state != 'observed':
        return result(release.state if release.state in {'unreachable', 'unsupported'} else 'unknown',
                      'release_' + release.state)
    if binding.version_kind == 'software' and any(value is None for value in
            (binding.platform, binding.architecture, binding.channel)):
        return result('unknown', 'release_scope_unknown')
    freshness = _freshness(release.checked_at, release.valid_for, now)
    if freshness is not Freshness.FRESH:
        return result('unknown', 'release_' + freshness.value)
    if not release.authoritative or release.matching_artifact_available is not True:
        return result('unknown', 'release_not_applicable_or_authoritative')
    if local.evidence_level == 'configured':
        return result('unknown', 'configured_version_only')
    if condition in {'locally_modified', 'changed_requires_review', 'unknown'} and installed:
        return result('unknown', 'installation_' + condition)
    if local.comparator_id is None or local.comparator_id != release.comparator_id:
        return result('unknown', 'no_shared_version_comparator')
    identity = local.comparison_identity or local.raw_version
    if local.comparator_id == 'semver':
        order = SemVer.parse(identity).compare(SemVer.parse(release.latest_identity))
    else:
        if identity != release.latest_identity:
            return result('unknown', 'unordered_version_identities')
        order = 0
    if order < 0:
        return result('outdated', 'newer_applicable_release')
    if not release.complete:
        return result('unknown', 'incomplete_release_comparison')
    if order > 0:
        return result('ahead', 'installed_ahead_of_selected_channel')
    verified_install = installed is not None and local.evidence_level == 'locally_verified'
    return result('current' if verified_install else 'reported_current', 'equal_applicable_release')
