"""Host-bound Tag transactions; native writers/recovery retain all authority."""
from dataclasses import asdict, dataclass, replace
import sys
from hashlib import sha256
import json
from pathlib import Path

from suite_core.database_actions import DatabaseActionsController, DatabaseAction, DatabasePreviewStatus, CatalogConflict
from suite_core.knowledge.catalog_migration_preview import preview_catalog_transition
from suite_core.knowledge import TagLookup, normalize_term
from suite_core.knowledge import Category, EffectiveTag
from suite_core.encyclopedia.publication import publish, read
from typing import Callable
from suite_core.selected_catalog import SelectedCatalogSession
from suite_core.encyclopedia.publication import Publication, require_successor

SOFTWARE_VERSION = "0.1.0-alpha.4"
HOST_INTERFACE_VERSION = 1
RELEASE_DATE = "2026-10-09"


def _foundation(plan):
    proposal = getattr(plan, "foundation_proposal", None)
    if proposal is None:
        return None
    # Bind only the already-loaded native codec, without importing or activating
    # core. Host callbacks supply authority; arbitrary DTO methods are not run.
    owner = sys.modules.get(type(plan).__module__)
    if (owner is None or not owner.__name__.endswith(".knowledge_pack")
            or type(plan) is not getattr(owner, "KnowledgePackImportPlan", None)
            or type(proposal) is not getattr(owner, "FoundationProposal", None)):
        raise TypeError("Foundation review requires the exact loaded native proposal codec")
    proposal.__post_init__()
    return proposal


def _seal(plan):
    proposal = _foundation(plan)
    value = asdict(plan)
    if proposal is not None:
        inventory = dict(proposal.source_inventory)
        # The native codec just verified every exact byte digest. Seal the same
        # source evidence in payload order; never JSON-serialize raw byte blobs.
        value["foundation_proposal"]["payloads"] = [[name, inventory[name]] for name, _raw in proposal.payloads]
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class FoundationParticipantReview:
    relative: str
    before: str
    after: str
    outcome: str


@dataclass(frozen=True)
class FoundationReview:
    version: int
    source_reference: str
    source_inventory: tuple
    participants: tuple[FoundationParticipantReview, ...]
    catalog_identity_binding: tuple
    receipt_epoch: str


def _foundation_review(host, plan, available_release):
    proposal = _foundation(plan)
    if proposal is None:
        return None, ()
    holds = []
    if (not plan.catalog_identity_binding or not plan.catalog_receipt_epoch
            or not plan.approved_default_release_json or plan.record_patches
            or plan.imported_relationships or plan.removal_decisions):
        holds.append("Foundation requires enrolled native release authority without user mutations")
    metadata = json.loads(plan.approved_default_release_json) if plan.approved_default_release_json else {}
    expected = {"pack_id": available_release.revision.pack_id,
        "content_version": available_release.revision.content_version,
        "snapshot_fingerprint": available_release.revision.snapshot.fingerprint,
        "release_fingerprint": available_release.fingerprint,
        "released_on": available_release.released_on, "whats_new": list(available_release.whats_new),
        "source_ref": available_release.source_ref}
    if any(metadata.get(key) != value for key, value in expected.items()):
        holds.append("Foundation release binding differs from the approved available release")
    defaults = tuple(EffectiveTag(*row) for row in plan.foundation_default_tags)
    categories = tuple(Category(*row) for row in plan.foundation_default_categories)
    if (set(defaults) != set(available_release.revision.snapshot.tags)
            or set(categories) != set(available_release.revision.snapshot.categories)):
        holds.append("Foundation default review surface differs from the approved available release")
    before = {Path(path).relative_to(host.root).as_posix(): digest
              for path, digest in plan.authority_fingerprints if Path(path).is_relative_to(host.root / "data/defaults")}
    participants = tuple(FoundationParticipantReview(name, before[name], digest,
        "unchanged" if before[name] == digest else "add_default" if before[name] == "missing" else "replace_default")
        for name, digest in proposal.source_inventory)
    return FoundationReview(proposal.version, proposal.source_reference, proposal.source_inventory,
        participants, plan.catalog_identity_binding, plan.catalog_receipt_epoch), tuple(holds)


@dataclass(frozen=True)
class _NativeCatalogOwnership:
    retirement_admissions: tuple[str, ...]
    default_lifecycle_admissions: tuple[str, ...]
    user_record_ids: tuple[str, ...]


def _native_catalog_ownership(host, plan, preview):
    """Conservative native ownership: no persisted user Tag or relationship use.

    EffectiveTag equality cannot prove notes/properties/provenance ownership.
    Only exact plan-fingerprinted native participants can admit default-only IDs;
    even identical-looking stored/restored user records remain explicit review.
    This returns proposal evidence, not writer or image reassignment authority.
    """
    _foundation(plan)  # Exact already-loaded native plan/codec.
    native = sys.modules[type(plan).__module__]
    expected = dict(plan.authority_fingerprints)
    participants = (host.root / "data/user/tags.json", host.root / "data/user/relationships.json",
                    host.root / "data/user/default_knowledge_state.json")
    documents = []
    for path in participants:
        native._check_export_path(path)
        if expected.get(str(path)) != native._file_fingerprint(path):
            raise RuntimeError("Native retirement ownership changed after preview")
        documents.append(native._read_payload_checked(path) if path.exists() else {})
    tags, relationships, _tracking = documents
    if (not isinstance(tags.get("tags", []), list) or not isinstance(relationships.get("relationships", []), list)):
        raise RuntimeError("Native retirement ownership requires supported user participants")
    stored = {row.get("id") for row in tags.get("tags", ()) if isinstance(row, dict)}
    owned = set(stored)
    owned.update(tags.get("removed_tag_ids", ()))
    owned.update(dict(tags.get("merged_tag_ids") or {}))
    for row in relationships.get("relationships", ()):
        if isinstance(row, dict):
            owned.update(endpoint.get("id") for endpoint in row.get("endpoints", ())
                         if isinstance(endpoint, dict) and endpoint.get("kind") == "tag")
    for path in participants:
        if expected.get(str(path)) != native._file_fingerprint(path):
            raise RuntimeError("Native retirement ownership changed during review")
    old = {row.id: row for row in preview.base.snapshot.tags}
    local = {row.id: row for row in preview.local.tags}
    target_ids = {row.id for row in preview.target.snapshot.tags}
    reader = TagLookup(preview.target.snapshot)
    admitted = []
    for identity, _target in preview.target.snapshot.redirects:
        result = reader.get(identity)
        if (identity not in owned and identity in old and local.get(identity) == old[identity]
                and result.status == "redirected" and result.tag is not None):
            admitted.append(identity)
    # Effective Snapshot lifecycle includes shipped compiler equivalences and
    # tombstones. Exact prior-source provenance plus native absence distinguishes
    # those from user intent; an owned redirect target also protects the donor.
    local_redirects = dict(preview.local.redirects)
    base_redirects = dict(preview.base.snapshot.redirects)
    local_reader = TagLookup(preview.local)
    default_lifecycle = []
    for identity in target_ids & (set(local_redirects) | set(preview.local.removed_ids)):
        resolved = local_reader.get(identity)
        current, chain = identity, set()
        while current not in chain:
            chain.add(current)
            if current not in local_redirects:
                break
            current = local_redirects[current]
        # Ownership of any exact redirect node (including a retired terminal)
        # protects user intent, even when no active tag can be presented.
        if chain & owned or identity in local_redirects and resolved.tag is None:
            continue
        if (identity in local_redirects and local_redirects[identity] == base_redirects.get(identity)
                or identity in preview.local.removed_ids and identity in preview.base.snapshot.removed_ids):
            default_lifecycle.append(identity)
    return _NativeCatalogOwnership(tuple(sorted(admitted)), tuple(sorted(default_lifecycle)),
                                   tuple(sorted(stored & set(local))))


def _native_default_retirement_admissions(host, plan, preview):
    """Compatibility helper for the previously qualified retirement evidence."""
    return _native_catalog_ownership(host, plan, preview).retirement_admissions


def _reconcile_retirement_review(review, ownership, offers):
    """Re-run the same detached transition with native-owned evidence only.

    Preserve original binding/release/session inputs and every assignment. Never
    substitute native effective rows or discard either side of equality checks.
    """
    before = review.proposal.preview
    preview = preview_catalog_transition(before.base, before.target, before.local, before.assignments,
        mode=before.mode, offers=offers, retirement_admissions=ownership.retirement_admissions,
        default_lifecycle_admissions=ownership.default_lifecycle_admissions,
        native_user_record_ids=ownership.user_record_ids)
    known = {row.id for row in (*before.local.tags, *before.base.snapshot.tags, *before.target.snapshot.tags)}
    conflicts = []
    for reason in preview.holds:
        kind, _, value = reason.partition(":")
        ids = tuple(sorted(row.id for row in preview.records
            if value in {normalize_term(name) for name in (row.display, *row.aliases)})) if kind == "catalog_name_collision" else (
                (value,) if value in known else ())
        conflicts.append(CatalogConflict(reason, ids))
    choices = tuple(row for row in preview.reviews if row.reason != "unchanged_identity")
    proposed = {row.id for row in preview.records}; local = {row.id for row in before.local.tags}
    return replace(review, proposal=replace(review.proposal, preview=preview),
        status=DatabasePreviewStatus.REVIEW_REQUIRED if conflicts or choices else DatabasePreviewStatus.REVIEW_READY,
        additions=tuple(row for row in preview.changes if row.reason == "added_default"),
        updates=tuple(row for row in preview.changes if row.reason == "default_update"),
        protected_edits=tuple(row for row in preview.changes if row.protected_fields),
        retained_local_ids=tuple(sorted(local & proposed)), excluded_catalog_ids=tuple(sorted(local - proposed)),
        conflicts=tuple(conflicts), assignment_choices=choices)


@dataclass(frozen=True)
class TagCatalogHost:
    """Explicit already-selected native capabilities; no initialization/discovery.

    guard is a reentrant context-manager factory shared by every native operation.
    validate checks selected root/incarnation and pending-operation admission.
    plan/apply/recover must be the existing native owners bound to this exact root.
    recover takes an explicit selected receipt; it must retain native recovery checks.
    capture takes generation and returns a fresh native Publication. revision covers
    every capture participant, defaults and persisted identity, not just Tag bytes.
    These callbacks are authority supplied by the host, not caller DTO evidence.
    """
    root: Path
    guard: Callable
    validate: Callable
    plan: Callable
    apply: Callable
    recover: Callable
    capture: Callable
    revision: Callable
    publication_path_guard: Callable | None = None
    plan_default_release: Callable | None = None

    def __post_init__(self):
        path = Path(self.root)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("Bind an explicit absolute selected native root")
        object.__setattr__(self, "root", path)
        for name in ("guard", "validate", "plan", "apply", "recover", "capture", "revision"):
            if not callable(getattr(self, name)):
                raise TypeError("Host capability must be callable: " + name)
        if self.plan_default_release is not None and not callable(self.plan_default_release):
            raise TypeError("Optional approved default-release planner must be callable")
        if self.publication_path_guard is not None and not callable(self.publication_path_guard):
            raise TypeError("Optional publication path guard must be callable")


@dataclass(frozen=True)
class TransactionReview:
    database: object
    native: object
    pack: Path
    seal: str
    token: str
    holds: tuple
    foundation: FoundationReview | None = None

    @property
    def can_apply(self):
        return not self.holds and self.native is not None and self.native.can_apply


@dataclass(frozen=True)
class AppliedCatalog:
    native_result: dict
    publication: object | None
    publication_error: str = ""


class TagCatalogTransactions:
    """Review -> explicit native commit -> verified publication -> reader refresh.

    The host supplies an approved prepared Pack preserving full native fields.
    This service compares it with the existing detached proposal and keeps native
    conflicts held; it never serializes lossy snapshot DTOs into native records.
    """
    def __init__(self, host: TagCatalogHost, session: SelectedCatalogSession, *, destination):
        if type(host) is not TagCatalogHost or type(session) is not SelectedCatalogSession:
            raise TypeError("Explicit host capabilities and selected catalog session required")
        self.host, self.session = host, session
        self.capture, self.revision = host.capture, host.revision
        self.destination = Path(destination).absolute()
        self._check_destination()
        self.current = None
        self.controller = None
        self.pending_result = None
        self.pending_expected_tags = None
        self.generation = session.binding.generation
        # Construction is passive: an interrupted host must be reconstructible
        # before its selected recovery callback restores coherent capture readiness.
        # Preview/reload enforce fresh capture; apply enforces native plan admission.

    def _check_destination(self):
        selected = self.host.root.resolve()
        resolved = self.destination.resolve()
        if not resolved.is_relative_to(selected):
            raise ValueError("Publication must remain inside the selected catalog root")
        relative = resolved.relative_to(selected)
        if not relative.parts or relative.parts[0].casefold() in {"data", "backups"}:
            raise ValueError("Publication cannot replace native data or recovery authority")
        if self.host.publication_path_guard is not None:
            self.host.publication_path_guard(self.destination)

    def _capture(self, generation):
        observed = self.capture(generation)
        if (type(observed) is not Publication or observed.generation != generation
                or observed.catalog_id != self.session.binding.catalog_id
                or observed.snapshot.source.revision != self.revision()):
            raise RuntimeError("Capture does not belong to the selected native catalog/revision")
        return observed

    def preview(self, action, *, base_release, available_release, pack,
                installed, assignments=(), offers=(), mode=None):
        self.current = None
        with self.host.guard():
            self.host.validate()
            self._check_destination()
            observed = self._capture(self.generation)
            if observed.snapshot.fingerprint != self.session.binding.snapshot_fingerprint:
                raise RuntimeError("Selected reader is stale; reload before preview")
            controller = DatabaseActionsController(self.session, base_release=base_release,
                available_release=available_release, installed=installed)
            review = controller.preview(action, mode=mode, assignments=assignments, offers=offers)
            if available_release is None:
                token = sha256(review.binding.token.encode()).hexdigest()
                self.controller = controller
                self.current = TransactionReview(review, None, Path(pack), "", token,
                                                 ("Approved available release not supplied",))
                return self.current
            plan = (self.host.plan_default_release(Path(pack), base_release, available_release, installed)
                    if self.host.plan_default_release is not None else self.host.plan(Path(pack)))
            foundation, foundation_holds = _foundation_review(self.host, plan, available_release)
            if (foundation is not None and not foundation_holds and plan.can_apply
                    and review.proposal is not None and review.mode == "merged"):
                ownership = _native_catalog_ownership(self.host, plan, review.proposal.preview)
                review = _reconcile_retirement_review(review, ownership, offers)
            holds = list(review.proposal.preview.holds if review.proposal else ("No proposal",))
            holds.extend(foundation_holds)
            if foundation is not None and mode == "merged" and not installed:
                from suite_core.manager.tag_database_controller import empty_default_layer
                if not empty_default_layer(getattr(self.host.capture, "__self__", None)):
                    holds.append("First default import requires the same admitted empty default layer")
            if foundation is not None:
                effective = tuple(EffectiveTag(*row) for row in plan.foundation_effective_tags)
                if review.proposal is None or set(effective) != set(review.proposal.preview.records):
                    holds.append("Foundation effective native view differs from the detached merge review; retained overrides require review")
                    if review.proposal is not None:
                        expected = {row.id: row for row in effective}
                        detached = {row.id: row for row in review.proposal.preview.records}
                        differing = sorted(identity for identity in set(expected) | set(detached)
                            if expected.get(identity) != detached.get(identity))
                        # Keep strict equality. A detached DTO cannot prove an
                        # identical-looking local restoration or alias authority.
                        # Report bounded IDs so the owning native identity/editor
                        # review can resolve intent without dropping corrections.
                        holds.append("Review native identity/alias authority for: " + ", ".join(differing[:8]) +
                                     (f" (and {len(differing) - 8} more)" if len(differing) > 8 else ""))
            if foundation is None:
                # This bounded bridge admits Tags plus their already selected categories.
                # It never translates lifecycle, Rules or relationship writers implicitly.
                if (plan.schema_version < 8 or plan.imported_relationships or plan.removal_decisions
                        or any(p.kind not in {"tag", "category"} for p in plan.record_patches)):
                    holds.append("Pack requires a different native owner")
                proposed = {t.id: t for t in review.proposal.preview.records} if review.proposal else {}
                represented = set()
                for patch in plan.record_patches:
                    if patch.kind != "tag":
                        if any(d.kind == "category" and d.outcome != "already_present"
                               for d in plan.record_decisions):
                            holds.append("Category changes require their own review")
                        continue
                    represented.add(patch.stable_id)
                    target = proposed.get(patch.stable_id)
                    payload = patch.payload or {}
                    if (target is None or patch.source_id != patch.stable_id
                            or patch.local_id != patch.stable_id
                            or payload.get("display") != target.display
                            or payload.get("category") != target.category_id
                            or tuple(payload.get("aliases", ())) != target.aliases):
                        holds.append("Native Pack differs from the reviewed Tag proposal")
                    sensitivity = payload.get("content_sensitivity", {})
                    if target and (sensitivity.get("minimum_rating", "") != target.minimum_rating
                            or sensitivity.get("classification_status", "unclassified") != target.classification_status):
                        holds.append("Native Pack differs from reviewed content safety")
                if represented != set(proposed):
                    holds.append("Pack must represent the complete proposed Tag set")
            seal = _seal(plan)
            token = sha256((review.binding.token + seal + available_release.fingerprint).encode()).hexdigest()
            self.controller = controller
            self.current = TransactionReview(review, plan, Path(pack), seal, token, tuple(holds), foundation)
            return self.current

    def apply(self, review, *, confirmed_token):
        with self.host.guard():
            self.host.validate()
            self._check_destination()
            if (review is not self.current or not review.can_apply or confirmed_token != review.token
                    or _seal(review.native) != review.seal
                    or not self.controller.accepts(review.database) or not review.can_apply):
                raise RuntimeError("Explicit current review required")
            if (review.foundation is not None and review.database.mode == "merged"
                    and review.database.action == DatabaseAction.INSTALL_DEFAULT
                    and review.database.installed_version is None):
                from suite_core.manager.tag_database_controller import empty_default_layer
                if not empty_default_layer(getattr(self.host.capture, "__self__", None)):
                    raise RuntimeError("Default-layer membership or tracking changed after review; preview again")
            self.current = None  # Attempt consumes host authority, even after exact rollback.
            result = self.host.apply(review.pack, review.native)
            self.pending_result = result
            self.pending_expected_tags = tuple(review.database.proposal.preview.records)
            try:
                publication = self.reload(expected_tags=review.database.proposal.preview.records)
            except Exception as error:
                # Native commit is retained. Retry reload, never replay this import.
                return AppliedCatalog(result, None, str(error))
            return AppliedCatalog(result, publication)

    def reload(self, *, expected_tags=None):
        with self.host.guard():
            self.host.validate()
            self._check_destination()
            previous = read(self.destination) if self.destination.exists() else None
            captured = self._capture(self.generation)
            snapshot = captured.snapshot
            if self.pending_expected_tags is not None:
                if expected_tags is not None and set(expected_tags) != set(self.pending_expected_tags):
                    raise RuntimeError("Pending publication retains its exact committed review")
                expected_tags = self.pending_expected_tags
            if expected_tags is not None and set(snapshot.tags) != set(expected_tags):
                raise RuntimeError("Saved native Tags differ from the reviewed proposal")
            if previous is not None and snapshot == previous.snapshot:
                self.session.refresh(previous)
                self.pending_expected_tags = None
                return previous
            generation = max(self.generation, previous.generation if previous else 0) + 1
            if previous is None and snapshot.fingerprint == self.session.binding.snapshot_fingerprint:
                generation = self.generation
            # Publish the exact snapshot whose committed expectation was verified.
            # A second independent capture could silently replace that reviewed set.
            observed = Publication(captured.catalog_id, generation, snapshot)
            if previous is not None:
                require_successor(previous, observed)
            publish(observed, self.destination, expected_token=previous.token if previous else None,
                    revision=self.revision, guard=self.host.guard())
            persisted = read(self.destination)
            if persisted != observed:
                raise RuntimeError("Published snapshot read-back mismatch")
            self.session.refresh(persisted)
            self.generation = generation
            self.pending_expected_tags = None
            return persisted

    def recover(self, receipt):
        with self.host.guard():
            self.current = None
            # Stock selected-catalog admission and exact durable receipt recovery.
            result = self.host.recover(Path(receipt))
            self.reload()
            return result
