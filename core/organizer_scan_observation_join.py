"""Read-only join of actual original hosted/native/scan observations.

No usage counts, writer exclusion, transport or removal
authority. Paths remain in local original observations; no serialization here.
"""
from dataclasses import dataclass, field
from core.organizer_handoff_scope import OrganizerHostedScopeReader, require_hosted_scope
from core.organizer_encyclopedia_admission import OrganizerCatalogAdmissionReader
from core.organizer_encyclopedia_handoffs import HandoffHold
from core.dataset_usage_coverage import DatasetUsageCoverageTerminal


@dataclass(frozen=True)
class RetainedOrganizerScanJoin:
    hosted_scope: object = field(repr=False)
    catalog: object = field(repr=False)
    observations: tuple = field(repr=False)
    writer_exclusion_available: bool = field(default=False,init=False)
    removal_available: bool = field(default=False,init=False)


@dataclass(frozen=True)
class ParticipantUsageContext:
    library_id: str
    role: str
    share_safety_policy: str
    require_reviewed: bool
    terminal: DatasetUsageCoverageTerminal = field(repr=False)
    freshness: str = field(default='retained_validated_scan_cache',init=False)
    global_coverage: bool = field(default=False,init=False)
    live_sidecar_proof: bool = field(default=False,init=False)

    @property
    def scan_policy(self):
        return self.terminal.policy

    @property
    def excluded_directories(self):
        return self.terminal.policy.excluded


@dataclass(frozen=True)
class ReadOnlyUsageContext:
    join: RetainedOrganizerScanJoin = field(repr=False)
    participants: tuple[ParticipantUsageContext, ...]
    usage_counts: None = field(default=None,init=False)
    writer_exclusion_available: bool = field(default=False,init=False)
    removal_available: bool = field(default=False,init=False)


def revalidate_scan_join(receiver,prepared,*,hosted_reader):
    if type(prepared) is not RetainedOrganizerScanJoin:
        raise HandoffHold('original_prepared_scan_join_required')
    fresh=prepare_retained_scan_join(receiver,hosted_reader=hosted_reader,
        expected_catalog_id=prepared.catalog.catalog_id,
        expected_catalog_revision=prepared.catalog.revision)
    require_hosted_scope(prepared.hosted_scope,fresh.hosted_scope)
    if (fresh.catalog.knowledge is not prepared.catalog.knowledge
            or fresh.catalog.tags!=prepared.catalog.tags
            or len(fresh.observations)!=len(prepared.observations)
            or any(old_id!=new_id or old is not new for (old_id,old),(new_id,new)
                   in zip(prepared.observations,fresh.observations))):
        raise HandoffHold('prepared_scan_join_changed_before_use')
    by_id=dict(fresh.observations)
    return ReadOnlyUsageContext(fresh,tuple(ParticipantUsageContext(p.library_id,p.role,
        p.share_safety_policy,p.require_reviewed,by_id[p.library_id])
        for p in fresh.hosted_scope.participants))


def prepare_retained_scan_join(receiver, *, hosted_reader, expected_catalog_id,
                              expected_catalog_revision):
    """Requires original readers and original host's fresh retained lookup.

    This supports the already implemented same-process hosted boundary only.
    Calling is not authorized by reading this file; real participant processing
    (especially Local Only) needs its separately explicit scope authorization.
    """
    if (type(hosted_reader) is not OrganizerHostedScopeReader
            or type(receiver.read_admitted_catalog) is not OrganizerCatalogAdmissionReader
            or hosted_reader.host is not receiver.host):
        raise HandoffHold('original_hosted_and_native_readers_required')
    receiver._check_host()
    host=receiver.host
    scope=hosted_reader()
    if scope.state is not host.state or scope.dataset is not host.state.dataset:
        raise HandoffHold('original_recipient_authority_changed')
    catalog=receiver.read_admitted_catalog(host.state)
    if (catalog.catalog_id != expected_catalog_id or catalog.revision != expected_catalog_revision
            or catalog.knowledge is not host.state.knowledge):
        raise HandoffHold('selected_native_catalog_changed')
    observations=[]
    for participant in scope.participants:
        terminal=host._current_dataset_scan_coverage(participant.library_id)
        if (type(terminal) is not DatasetUsageCoverageTerminal or not terminal.complete_scan
                or terminal.manager.value is not participant.manager
                or terminal.index.value is not participant.manager.index):
            raise HandoffHold('exact_participant_drained_scan_unavailable')
        # The original retained getter reconciles root/index token and its scan
        # participant policy. The native hosted reader reconciles current role,
        # privacy, root token, profile, scope epoch and original manager identity.
        observations.append((participant.library_id,terminal))
    require_hosted_scope(scope,hosted_reader())
    current=receiver.read_admitted_catalog(host.state)
    if (current.knowledge is not catalog.knowledge or current.catalog_id != catalog.catalog_id
            or current.revision != catalog.revision or current.tags != catalog.tags):
        raise HandoffHold('native_catalog_changed_during_scan_join')
    for library_id,terminal in observations:
        if host._current_dataset_scan_coverage(library_id) is not terminal:
            raise HandoffHold('retained_scan_changed_during_join')
    receiver._check_host()
    return RetainedOrganizerScanJoin(scope,catalog,tuple(observations))
