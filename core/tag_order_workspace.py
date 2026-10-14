from __future__ import annotations

import copy
import hashlib
import json
import shutil
import time
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .automation_rule_conversion import RulePairAdmission

from .category_registry import (
    CategoryRecord,
    CategoryRegistry,
    REUSABLE_MODIFIER_FAMILY_EXTRA,
    category_is_protected,
    category_own_semantic_role,
    exact_category_anchor_ids,
)
from .category_base_relationships import (
    CATEGORY_BASE_MEMBER_SCOPE_DIRECT,
    DirectCategoryBaseAuthority,
    category_base_relationship_index,
    direct_category_base_authority,
    normalize_category_base_member_scope,
    stage_category_base_change,
    stage_category_base_restore,
)
from .pattern_registry import PatternRecord, PatternRegistry
from .character_database import CharacterDatabase
from .knowledge_engine import FoundationKnowledge
from .knowledge_relationships import (
    CATEGORY_BASE,ENTAILMENT,MODIFIER_VALUE,SPECIFICITY,
    KnowledgeRelationship,RelationshipEndpoint,
)
from .modifier_composition import (
    modifier_composition_relationship_id,modifier_composition_resolutions,
)
from .relationship_store import RelationshipStore, validate_relationships
from .relationship_workspace import RelationshipWorkspaceService
from .constraint_resolver import constraint_pairs, owns_inline_constraint_projection
from .tag_collection import normalize_tag
from .tag_database import RemovedTagArchive, TagDatabase, TagRecord


def _cooperative_deepcopy_mapping(
    values,*,batch_size: int=32,cooperative: bool=False,prepare_copy=None,
):
    """Copy record mappings without monopolizing another thread's interpreter."""
    result={}
    for index,(key,value) in enumerate(values.items()):
        if cooperative and index and index%batch_size==0:
            time.sleep(0.001)
        copied_key=copy.deepcopy(key)
        copied_value=copy.deepcopy(value)
        result[copied_key]=(
            prepare_copy(copied_value) if prepare_copy is not None else copied_value
        )
    return result


def prepare_category_structure_search_index(
    session,tags,*,review_filter_tag_ids=None,
):
    """Build immutable Category Workspace search inputs without Qt widgets."""
    records={record.id:record for record in session.all_categories()}
    children={}
    paths={}
    ancestors={}
    category_haystacks={}
    for record in records.values():
        children.setdefault(
            record.parent_id if record.parent_id in records else None,[]
        ).append(record)
        path=session.path_label(record.id)
        paths[record.id]=path
        ancestors[record.id]=session.path_ids(record.id)
        category_haystacks[record.id]=(
            f"{record.name} {path} {record.description}"
        ).casefold()
    for values in children.values():
        values.sort(key=lambda item:(item.name.casefold(),item.id))
    all_tags_by_category={}
    for tag in session.all_tags():
        all_tags_by_category.setdefault(tag.category,[]).append(tag)
    review_ids=(
        None if review_filter_tag_ids is None
        else frozenset(str(value) for value in review_filter_tag_ids)
    )
    tags_by_category={}
    tag_haystacks={}
    for category_id,values in all_tags_by_category.items():
        filtered=(
            values if review_ids is None else
            [tag for tag in values if tag.id in review_ids]
        )
        if not filtered:
            continue
        # Stable passes retain the exact (casefolded display, ID) order without
        # allocating a tracked tuple for every Tag during each worker refresh.
        filtered.sort(key=lambda item:item.id)
        filtered.sort(key=lambda item:item.display.casefold())
        tags_by_category[category_id]=filtered
        for tag in filtered:
            tag_haystacks[tag.id]=(
                " ".join(tags.identity_names(tag)).casefold()
            )
    return (
        records,children,tags_by_category,paths,ancestors,
        category_haystacks,tag_haystacks,
    )


from .tag_alias_relationships import (
    TagAliasCompatibilityIndex,
    publish_tag_alias_storage_authority,
    tag_alias_compatibility_index,
)
from .tag_ordering import (
    MAX_TAG_PRIORITY,MIN_TAG_PRIORITY,ORDER_GROUP_DEFAULT_PRIORITY,
    ORDER_GROUP_RANK,TagOrdering,
)
from .tag_semantic_relationships import (
    TagSemanticChange,effective_tag_semantic_links_from_index,
    publish_tag_semantic_authority,stage_tag_semantic_changes,
    tag_semantic_relationship_index,
)
from .reusable_modifier_projection import (
    ReusableModifierCompatibilityIndex,
    publish_reusable_modifier_storage_authority,
    reusable_modifier_compatibility_index,
)
from .scoped_constraint_projection import (
    ScopedConstraintCompatibilityIndex,
    publish_scoped_constraint_storage_authority,
    scoped_constraint_compatibility_index,
)
from .tag_relationship_compatibility import tag_relationship_compatibility_payload
from .workspace_record_storage import (
    compact_staged_tag_record,
    materialize_staged_tag_record,
)


OrderKind = Literal["category", "tag"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".restore.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _payload_fingerprint(payload: Any) -> str:
    if payload is None:
        return "missing"
    return hashlib.sha256(
        json.dumps(
            payload,sort_keys=True,ensure_ascii=False,separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _tag_default_comparable(record: TagRecord) -> dict[str, Any]:
    payload=record.to_dict(
        include_parent_projection=True,
        include_attributes_projection=True,
        include_implies_projection=True,
        include_conflicts_projection=True,
    )
    for key in ("seen_count","first_seen","last_seen"):
        payload.pop(key,None)
    return payload


@dataclass(frozen=True)
class OrderImpact:
    changed_categories: tuple[str, ...] = ()
    changed_tags: tuple[str, ...] = ()
    affected_tag_ids: tuple[str, ...] = ()
    affected_tags: int = 0
    affected_image_paths: tuple[str, ...] | None = None
    caption_examples_before: tuple[str, ...] = ()
    caption_examples_after: tuple[str, ...] = ()
    captions_may_be_stale: bool = False
    metadata_may_be_stale: bool = False
    organization_affected: bool = False
    created_categories: tuple[str, ...] = ()
    removed_categories: tuple[str, ...] = ()
    removed_tags: tuple[str, ...] = ()
    reassigned_tags: tuple[str, ...] = ()
    reassigned_patterns: tuple[str, ...] = ()

    @property
    def affected_images(self) -> int | None:
        return None if self.affected_image_paths is None else len(self.affected_image_paths)


@dataclass(frozen=True)
class OrderSaveResult:
    impact: OrderImpact
    backup_directory: Path


@dataclass(frozen=True)
class CategoryMergeImpact:
    keep_id: str
    remove_ids: tuple[str, ...]
    child_categories: tuple[str, ...] = ()
    tag_ids: tuple[str, ...] = ()
    pattern_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CategoryDeletionImpact:
    category_id: str
    destination_id: str
    child_categories: tuple[str, ...] = ()
    tag_ids: tuple[str, ...] = ()
    pattern_ids: tuple[str, ...] = ()
    disables_default: bool = False


@dataclass(frozen=True)
class TagRemovalImpact:
    remove_ids: tuple[str, ...]
    relationship_record_ids: tuple[str, ...] = ()
    parent_links: int = 0
    implication_links: int = 0
    conflict_links: int = 0
    category_base_refs: tuple[str, ...] = ()
    canonical_relationship_ids: tuple[str, ...] = ()
    retired_relationship_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class TagRestorationImpact:
    requested_tag_ids: tuple[str, ...]
    restored_tag_ids: tuple[str, ...]
    archive_ids: tuple[str, ...]
    related_tag_ids: tuple[str, ...] = ()
    category_ids: tuple[str, ...] = ()
    relationship_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CategoryBaseApplicationPlan:
    category_id: str
    category: str
    target_id: str
    target: str
    match_basis: Literal["stable_id", "exact_canonical_name", "declared_alias"]
    member_scope: Literal["direct", "descendants"]
    broader_review: bool
    effective_target_unchanged: bool
    direct_member_count: int
    affected_direct_member_count: int
    descendant_category_count: int
    descendant_member_count: int
    affected_descendant_member_count: int
    changed_effective_base_count: int
    tag_specific_base_override_count: int
    fingerprint: str

    @property
    def evidence_label(self) -> str:
        return {
            "stable_id":"Stable ID equals Category ID",
            "exact_canonical_name":"Exact canonical Tag name",
            "declared_alias":"Exact declared Other Name",
        }[self.match_basis]

    @property
    def scope_label(self) -> str:
        return (
            "Direct and descendant members"
            if self.member_scope == "descendants"
            else "Direct members only"
        )


@dataclass(frozen=True)
class CategoryStructuralIssue:
    code: str
    area: str
    summary: str
    record_keys: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class MemberBroaderTagResetRow:
    tag_id: str
    category_id: str
    scope: Literal["direct", "descendant"]
    status: Literal["override", "ambiguous", "cycle", "unavailable", "no_override"]
    authority: Literal["canonical_default", "canonical_user", "canonical_mixed", "compatibility", "none"]
    target_ids: tuple[str, ...] = ()
    relationship_ids: tuple[str, ...] = ()
    result_base_id: str = ""
    result_source_id: str = ""
    result_kind: Literal["inherited", "anchor", "none"] = "none"

    @property
    def can_reset(self) -> bool:
        return self.status in {"override", "ambiguous", "cycle"}


@dataclass(frozen=True)
class MemberBroaderTagResetPlan:
    category_id: str
    include_descendants: bool
    category_ids: tuple[str, ...]
    rows: tuple[MemberBroaderTagResetRow, ...]

    @property
    def reset_tag_ids(self) -> tuple[str, ...]:
        return tuple(row.tag_id for row in self.rows if row.can_reset)

    @property
    def relationship_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            relationship_id
            for row in self.rows if row.can_reset
            for relationship_id in row.relationship_ids
        ))

    def count(self, status: str) -> int:
        return sum(row.status == status for row in self.rows)


def affected_order_projection_paths(
    impact: OrderImpact,tag_records,image_records,*,aliases_for=None,
) -> tuple[str, ...]:
    """Return indexed images whose confirmed tags consume the changed order."""
    changed_ids=set(impact.changed_tags); changed_categories=set(impact.changed_categories); names=set()
    records=tag_records.values() if isinstance(tag_records,dict) else tag_records
    for record in records:
        if record.id in changed_ids or record.category in changed_categories:
            aliases=(aliases_for(record) if aliases_for is not None else record.aliases)
            names.update(
                normalize_tag(value) for value in (record.display,*aliases)
                if normalize_tag(value)
            )
    paths=[]
    for record in image_records:
        if names.intersection(normalize_tag(value) for value in record.tags):paths.append(str(record.path))
    return tuple(dict.fromkeys(paths))


@dataclass
class _State:
    categories: dict[str, CategoryRecord]
    tags: dict[str, TagRecord]
    disabled_default_ids: set[str] = field(default_factory=set)
    patterns: dict[str, PatternRecord] = field(default_factory=dict)
    actions: list[dict[str, Any]] = field(default_factory=list)
    changed: bool = False
    removed_tag_ids: set[str] = field(default_factory=set)
    removed_tag_archives: dict[str, RemovedTagArchive] = field(default_factory=dict)
    merged_tag_ids: dict[str, str] = field(default_factory=dict)
    relationships: dict[str, KnowledgeRelationship] = field(default_factory=dict)
    relationship_file_exists: bool = False


class TagOrderWorkspaceSession:
    """Stage relative caption-order edits without mutating live knowledge.

    Numeric priorities remain a private compatibility detail. Moves record stable
    neighbors and only renumber the relevant parent/category sibling set when no
    sparse rank is available.
    """

    RANK_STEP = 10

    def __init__(
        self,
        tags: TagDatabase,
        categories: CategoryRegistry,
        patterns: PatternRegistry | None = None,
        characters: CharacterDatabase | None = None,
        *,
        default_relationship_path: str | Path | None = None,
        user_relationship_path: str | Path | None = None,
        foundation: FoundationKnowledge | None = None,
        publish_live_authority: bool = True,
    ) -> None:
        self.live_tags = tags
        self.live_categories = categories
        self.live_patterns = patterns
        self.characters = characters
        self._ownership_state = "active"
        self._ownership_generation = 0
        self._prepared_predecessor = None
        self._prepared_predecessor_state = None
        self._prepared_predecessor_initial = None
        self._prepared_predecessor_generation = None
        self._prepared_predecessor_baseline = None
        self._prepared_source_snapshot = None
        self._prepared_candidate_state = None
        self._prepared_candidate_initial = None
        self._prepared_initial_files = None
        self.default_relationship_path = Path(
            default_relationship_path
            or categories.default_path.with_name("relationships.json")
        )
        self.user_relationship_path = Path(
            user_relationship_path or tags.path.with_name("relationships.json")
        )
        shared_workspace=getattr(tags,"_app_relationship_workspace",None)
        shared_default=getattr(shared_workspace,"default_store",None)
        shared_user=getattr(shared_workspace,"user_store",None)
        if (
            shared_default is not None
            and shared_user is not None
            and Path(shared_default.path).resolve()
            ==self.default_relationship_path.resolve()
            and Path(shared_user.path).resolve()==self.user_relationship_path.resolve()
        ):
            # Application startup already loaded and validated these exact
            # immutable/default and current user stores. The staged session
            # copies their relationship dictionaries below, so reparsing the
            # same JSON would only hold the interpreter during first-open prep.
            self.default_relationships=shared_default
            self.user_relationships=shared_user
        else:
            self.default_relationships=RelationshipStore(
                self.default_relationship_path,
            )
            self.user_relationships=RelationshipStore(
                self.user_relationship_path,
            )
        self._modifier_foundation = foundation or FoundationKnowledge(
            categories.default_path.with_name("modifiers.json"),
            categories.default_path.with_name("concepts.json"),
        )
        self._cooperative_yields=not publish_live_authority
        # Background first-open preparation must yield often enough for Qt, but
        # yielding every 16 records adds seconds of artificial delay on large
        # vocabularies. A 128-record checkpoint stays comfortably bounded while
        # avoiding hundreds of unnecessary sleeps.
        self._cooperative_batch_size=128
        self._default_categories = _cooperative_deepcopy_mapping(
            categories.default_categories,
            batch_size=self._cooperative_batch_size,
            cooperative=self._cooperative_yields,
        )
        self._default_patterns = (
            _cooperative_deepcopy_mapping(
                patterns.default_patterns,
                batch_size=self._cooperative_batch_size,
                cooperative=self._cooperative_yields,
            )
            if patterns is not None else {}
        )
        self._initial = _State(
            _cooperative_deepcopy_mapping(
                categories.categories,
                batch_size=self._cooperative_batch_size,
                cooperative=self._cooperative_yields,
            ),
            _cooperative_deepcopy_mapping(
                tags.tags,
                batch_size=self._cooperative_batch_size,
                cooperative=self._cooperative_yields,
                # Release empty copies immediately instead of keeping one set
                # per Tag until all authority projections have finished.
                # Those projections still update relation_extras in place.
                prepare_copy=lambda record: compact_staged_tag_record(
                    record,keep_relation_extras_mutable=True,
                ),
            ),
            set(categories.disabled_default_ids),
            _cooperative_deepcopy_mapping(
                patterns.patterns,
                batch_size=self._cooperative_batch_size,
                cooperative=self._cooperative_yields,
            )
            if patterns is not None else {},
            removed_tag_ids=set(tags.removed_tag_ids),
            removed_tag_archives=copy.deepcopy(tags.removed_tag_archives),
            merged_tag_ids=dict(tags.merged_tag_ids),
            relationships=dict(self.user_relationships.relationships),
            relationship_file_exists=self.user_relationship_path.exists(),
        )
        self._state = self._snapshot(self._initial)
        self._undo: list[_State] = []
        self._redo: list[_State] = []
        self.last_history_target_was_saved = False
        tag_parent=self.live_tags.path.parent
        defaults_dir=(
            tag_parent.parent/"defaults"
            if tag_parent.name.casefold()=="user" else None
        )
        self._restore_default_tag_paths=(
            tuple(
                defaults_dir/name for name in (
                    "foundation_tags.json","community_tags.json","promoted_tags.json",
                )
            )
            if defaults_dir is not None and any(
                archive.active and "current_default" in archive.recovery_kinds.values()
                for archive in tags.removed_tag_archives.values()
            ) else ()
        )
        self._initial_files = self._file_snapshot()
        self._exact_category_anchor_cache: dict[str,str] | None = None
        self._category_children_cache: dict[str | None,tuple[str,...]] | None = None
        self._category_descendant_cache: dict[str,frozenset[str]] = {}
        self._direct_tag_count_cache: dict[str,int] | None = None
        self._refresh_category_base_authority(
            publish_live=publish_live_authority,
        )
        self._refresh_tag_semantic_authority(
            publish_live=publish_live_authority,
        )
        self._refresh_reusable_modifier_authority(
            publish_live=publish_live_authority,
        )
        self._refresh_scoped_constraint_authority(
            publish_live=publish_live_authority,
        )
        for record in self._state.tags.values():
            compact_staged_tag_record(record)
        self._initial = self._snapshot(self._state)
        loaded=self.reload_source_snapshot()
        self._reload_baseline=(
            self._initial_files+loaded[len(self._initial_files):]
            if (
                loaded[:len(self._initial_files)]==self._initial_files
                and self._rebase_source_receipts_match()
            ) else None
        )

    @property
    def ownership_state(self) -> str:
        """Return whether this staged snapshot is active, prepared, or retired."""
        return self._ownership_state

    def _require_active_ownership(self, action: str) -> None:
        if self._ownership_state != "active":
            raise RuntimeError(
                f"Category Workspace snapshot is {self._ownership_state}; "
                f"it cannot {action}."
            )

    def _require_readable_ownership(self) -> None:
        if self._ownership_state == "retired":
            raise RuntimeError(
                "Category Workspace snapshot is retired; it cannot expose "
                "staged records."
            )

    def _retire_ownership(self) -> None:
        """Release staged maps after ownership moved or preparation failed."""
        self._ownership_state = "retired"
        self._ownership_generation += 1
        self._state = _State({}, {})
        self._initial = _State({}, {})
        self._undo = []
        self._redo = []
        self._initial_files = ()
        self._reload_baseline = None
        self._prepared_predecessor = None
        self._prepared_predecessor_state = None
        self._prepared_predecessor_initial = None
        self._prepared_predecessor_generation = None
        self._prepared_predecessor_baseline = None
        self._prepared_source_snapshot = None
        self._prepared_candidate_state = None
        self._prepared_candidate_initial = None
        self._prepared_initial_files = None
        self._exact_category_anchor_cache = None
        self._category_children_cache = None
        self._category_descendant_cache = {}
        self._direct_tag_count_cache = None

    def _knowledge_paths(self) -> tuple[Path, ...]:
        paths=[self.live_categories.user_path,self.live_tags.path]
        if self.live_patterns is not None:
            paths.append(self.live_patterns.user_path)
        return tuple(paths)

    def _file_snapshot_paths(self) -> tuple[Path, ...]:
        character_paths = (
            (
                self.characters.series_path,
                self.characters.characters_path,
                self.characters.outfits_path,
            )
            if self.characters is not None
            else ()
        )
        return tuple(dict.fromkeys((
            *self._knowledge_paths(),
            self.user_relationship_path,
            self.default_relationship_path,
            *self._restore_default_tag_paths,
            *character_paths,
        )))

    def _file_snapshot(self) -> tuple[bytes | None, ...]:
        return tuple(path.read_bytes() if path.exists() else None for path in self._file_snapshot_paths())

    @staticmethod
    def _source_fingerprint(path: Path) -> bytes | tuple[()]:
        raw=path.read_bytes() if path.exists() else None
        return hashlib.sha256(raw).digest() if raw is not None else ()

    def _rebase_source_receipts_match(self) -> bool:
        """Prove supplied authorities were loaded from the current sources."""
        tag_bytes=(
            self.live_tags.path.read_bytes()
            if self.live_tags.path.exists() else None
        )
        if tag_bytes!=getattr(self.live_tags,"_loaded_bytes",None):
            return False
        for owner,paths in (
            (
                self.live_categories,
                (
                    self.live_categories.default_path,
                    self.live_categories.user_path,
                ),
            ),
            (
                self.live_patterns,
                (() if self.live_patterns is None else (
                    self.live_patterns.default_path,
                    self.live_patterns.user_path,
                )),
            ),
            (
                self._modifier_foundation,
                (
                    self._modifier_foundation.modifiers_path,
                    self._modifier_foundation.concepts_path,
                ),
            ),
        ):
            if owner is None:
                continue
            receipts=getattr(owner,"_loaded_source_fingerprints",None)
            if not isinstance(receipts,dict) or any(
                receipts.get(path)!=self._source_fingerprint(path)
                for path in paths
            ):
                return False
        for store in (self.default_relationships,self.user_relationships):
            raw=store.path.read_bytes() if store.path.exists() else None
            if raw!=getattr(store,"_loaded_bytes",None):
                return False
        return True

    def _reload_extra_source_paths(self) -> tuple[Path, ...]:
        paths=[self.live_categories.default_path,
               self._modifier_foundation.modifiers_path,
               self._modifier_foundation.concepts_path]
        paths.extend(self.live_categories.default_path.with_name(name) for name in (
            "foundation_tags.json","community_tags.json","promoted_tags.json",
        ))
        if self.live_patterns is not None:paths.append(self.live_patterns.default_path)
        if self.characters is not None:
            paths.extend((self.characters.relationship_path,self.characters.default_relationship_path))
        return tuple(paths)

    def _reload_source_paths(self) -> tuple[Path, ...]:
        return self._file_snapshot_paths()+self._reload_extra_source_paths()

    def reload_source_snapshot(self) -> tuple[bytes | None, ...]:
        """Exact inputs for a detached, read-only workspace refresh."""
        return self._file_snapshot()+tuple(
            path.read_bytes() if path.exists() else None for path in self._reload_extra_source_paths()
        )

    def prepare_tag_retirement(self, tag_ids, *, reason: str, source: str="remove_everywhere"):
        """Prepare one clean detached native retirement, without publishing it.

        The returned candidate owns its archive identity/timestamps. Do not stage
        the removal again on another session or silently include unrelated edits.
        Image correction and later save admission remain separate authorities.
        """
        from .category_retirement import (
            CategoryRetirementHold, _plan_native_retirement, _retirement_target_ids,
            preflight_category_retirements, source_pin,
        )
        from .category_retirement_handoff import _configure_application_bundle, live_authority_pin
        self._require_active_ownership("prepare Tag retirement")
        admission = self.live_tags.rule_pair_admission
        if admission is None or not admission.owns(self.live_tags.path, "knowledge"):
            raise CategoryRetirementHold("Native retirement needs the exact bound Category save admission.")
        if (self.has_changes or self._undo or self._redo or self._state.actions
                or self._full_signature(self._state) != self._full_signature(self._initial)):
            raise CategoryRetirementHold("Save or discard unrelated Category edits before reviewing Tag retirement.")
        targets = _retirement_target_ids(tag_ids)
        with admission.admit():
            root = self.live_tags.path.parent
            preflight_category_retirements(root)
            live_before = live_authority_pin(self)
            before = source_pin(self._reload_source_paths())
            if not self._rebase_source_receipts_match() or self._file_snapshot() != self._initial_files:
                raise CategoryRetirementHold("Category sources changed. Reload and review retirement again.")
            prepared = self.prepare_detached_reload()
            if prepared is None:
                raise CategoryRetirementHold("Category sources changed during retirement review.")
            _, candidate = prepared
            candidate = _configure_application_bundle(self, candidate)
            if live_authority_pin(self, include_revision=False)[1] != live_authority_pin(candidate, include_revision=False)[1]:
                raise CategoryRetirementHold("Shared Knowledge has unsaved native edits. Keep them before reviewing retirement.")
            if not set(targets).issubset(candidate._state.tags):
                raise CategoryRetirementHold("Retire only the exact selected active Tags.")
            original_relationships = candidate._effective_relationships()
            original_records = tuple(sorted(
                (target, json.dumps(candidate._tag_compatibility_payload(
                    candidate._state.tags[target], original_relationships,
                    tags=candidate._state.tags), sort_keys=True, ensure_ascii=False))
                for target in targets
            ))
            impact = candidate.remove_tags(targets, source=source, reason=reason)
            if set(impact.remove_ids) != set(targets) or len(set(targets)) != len(targets):
                raise CategoryRetirementHold("The retirement preview does not match the exact selected Tags.")
            if not candidate._tag_retirement_transition_matches(targets, original_records):
                raise CategoryRetirementHold("The retirement must preserve exact original records and removal history.")
            plan = _plan_native_retirement(root, targets, before,
                candidate.transaction_payloads(include_patterns=True))
            if (source_pin(self._reload_source_paths()) != before
                    or source_pin(candidate._reload_source_paths()) != before
                    or live_authority_pin(self) != live_before):
                raise CategoryRetirementHold("Category sources changed during retirement review.")
            candidate._prepared_retirement_plan = plan
            candidate._retirement_predecessor = self
            candidate._retirement_live_pin = live_before
            candidate._prepared_retirement_original_records = original_records
            candidate._prepared_retirement_transition = (
                candidate._full_signature(candidate._initial),
                candidate._full_signature(candidate._state),
            )
            return candidate, plan

    def _tag_retirement_transition_matches(self, target_ids, original_records) -> bool:
        """Admit active-row retirement, including an existing legacy tombstone."""
        targets = set(target_ids)
        before, after = self._initial, self._state
        if (not targets or not targets.issubset(before.tags)
                or set(after.tags) != set(before.tags) - targets
                or after.removed_tag_ids != before.removed_tag_ids | targets
                or after.merged_tag_ids != before.merged_tag_ids):
            return False
        if any(after.removed_tag_archives.get(key) != archive
               for key, archive in before.removed_tag_archives.items()):
            return False
        added = set(after.removed_tag_archives) - set(before.removed_tag_archives)
        if len(added) != 1:
            return False
        archive = after.removed_tag_archives[next(iter(added))]
        if not archive.active or len(archive.removed_tags) != len(targets):
            return False
        archived = tuple(sorted(
            (str(row.get("id") or ""), json.dumps(row, sort_keys=True, ensure_ascii=False))
            for row in archive.removed_tags
        ))
        return archived == original_records

    def _publish_prepared_tag_retirement(self, plan, *, operation_id: str):
        """Private worker-owned native save; no visible retry route is enabled.

        Returns fresh detached saved authority and native save result, with the
        fast reference still held. Shared-authority adoption and image-outcome
        handoff remain separate gates; this result does not admit ordinary work.
        """
        from .category_retirement import (
            CategoryRetirementHold, _plan_native_retirement,
            _publish_native_retirement, preflight_category_retirements, source_pin,
        )
        self._require_active_ownership("publish prepared Tag retirement")
        admission = self.live_tags.rule_pair_admission
        if (admission is None or not admission.owns(self.live_tags.path, "knowledge")
                or getattr(self, "_prepared_retirement_plan", None) is not plan):
            raise CategoryRetirementHold("Publish only this native owner's reviewed retirement candidate.")
        saved_authority = None
        with admission.admit():
            root = self.live_tags.path.parent
            preflight_category_retirements(root)
            pin = source_pin(self._reload_source_paths())
            if pin != plan.source_sha256:
                raise CategoryRetirementHold("Retirement sources changed. Review a fresh attempt before saving.")
            current = _plan_native_retirement(root, plan.target_ids, pin,
                self.transaction_payloads(include_patterns=True))
            if (current != plan
                    or not self._tag_retirement_transition_matches(plan.target_ids,
                        getattr(self, "_prepared_retirement_original_records", ()))
                    or getattr(self, "_prepared_retirement_transition", None) != (
                        self._full_signature(self._initial), self._full_signature(self._state))):
                raise CategoryRetirementHold("The staged retirement differs from the exact reviewed plan.")
            initial_issues = {(issue.code, issue.summary, issue.record_keys)
                for issue in self.structural_issue_records(self._initial)}
            if any((issue.code, issue.summary, issue.record_keys) not in initial_issues
                    for issue in self.structural_issue_records()):
                raise CategoryRetirementHold("Resolve health issues introduced by the retirement before saving.")
            impact = self.impact()
            participant_paths = {root / name for name, _, _ in plan.participants}
            unwritten_sources = tuple(path for path in self._reload_source_paths() if path not in participant_paths)
            unwritten_pin = source_pin(unwritten_sources)
            if source_pin(self._reload_source_paths()) != plan.source_sha256:
                raise CategoryRetirementHold("Retirement sources changed before publication.")
            def verify_sources():
                if source_pin(unwritten_sources) != unwritten_pin:
                    raise CategoryRetirementHold("Unwritten retirement sources changed during publication.")
            def reload_authority(after):
                nonlocal saved_authority
                prepared = self.prepare_detached_reload()
                if prepared is None:
                    raise CategoryRetirementHold("Retirement authority changed during reload.")
                _, fresh = prepared
                expected = self._state if after else self._initial
                if self._full_signature(fresh._state) != self._full_signature(expected):
                    raise CategoryRetirementHold("Native retirement authority failed saved read-back.")
                saved_authority = fresh
            backup = _publish_native_retirement(root, admission.pending_root, plan,
                operation_id, reload_authority, verify_sources)
            result = OrderSaveResult(impact, backup)
            saved_pin = source_pin(self._reload_source_paths())
            verify_sources()
            from .category_retirement import classify_category_retirement
            state = classify_category_retirement(root, operation_id, expected_plan_sha256=plan.plan_sha256)
            if state.outcome != "complete":
                raise CategoryRetirementHold("Completed retirement changed before worker return.")
            self._completed_retirement = (operation_id, plan, saved_pin, state.receipt_sha256, result)
            return saved_authority, result

    def prepare_verified_tag_rebase(self, change, saved_bytes):
        """Prepare one clean single-create rebase without duplicating all Tags.

        ``change`` is the trusted request consumed by the verified record writer.
        The returned candidate permits trusted derived-view reads while it is
        prepared, but rejects session mutations until ownership is activated.
        Record objects remain session-owned; callers must not retain and mutate
        raw records outside the session API.
        Unsupported or stale changes retain the complete detached-reload path.
        """
        self._require_active_ownership("prepare a verified Tag rebase")
        baseline=self._reload_baseline
        predecessor_generation=self._ownership_generation
        predecessor_state=self._state
        predecessor_initial=self._initial
        if (baseline is None or not change.created or self.has_changes
            or self._undo or self._redo or self._state.actions):
            return None
        paths=tuple(dict.fromkeys(self._knowledge_paths()))
        change_path=Path(change.path)
        if change_path!=self.live_tags.path or change_path not in paths:
            return None
        position=paths.index(change_path)
        if baseline[position]!=change.expected_bytes:
            return None
        if not isinstance(saved_bytes,bytes):
            return None
        expected=list(baseline)
        expected[position]=saved_bytes
        source_snapshot=self.reload_source_snapshot()
        if source_snapshot!=tuple(expected):
            return None
        try:
            record=TagRecord.from_dict(json.loads(change.record_json))
        except (TypeError,ValueError,json.JSONDecodeError):
            return None
        if (record.id in self._state.tags or record.id in self._state.removed_tag_ids
            or record.id in self._state.merged_tag_ids):
            return None
        # A formerly missing endpoint can change projections on existing Tags.
        # Keep those cases with the complete authority rebuild.
        if any(
            endpoint.kind=="tag" and endpoint.id==record.id
            for relationship in self._effective_relationships().values()
            for endpoint in relationship.endpoints
        ):
            return None
        candidate=copy.copy(self)
        candidate._state=self._snapshot(self._state)
        candidate._state.tags[record.id]=record
        if not record.ordering.reviewed:
            group,priority,_=candidate._category_effective(record.category)
            record.ordering.group=group
            record.ordering.priority=priority
        compact_staged_tag_record(record)
        candidate._initial=self._snapshot(candidate._state)
        candidate._initial_files=source_snapshot[:len(self._initial_files)]
        candidate._reload_baseline=source_snapshot
        candidate._undo=[]
        candidate._redo=[]
        candidate._exact_category_anchor_cache=None
        candidate._category_children_cache=None
        candidate._category_descendant_cache={}
        candidate._direct_tag_count_cache=None
        candidate._cooperative_yields=True
        candidate._ownership_state="prepared"
        candidate._prepared_predecessor=self
        candidate._prepared_predecessor_state=predecessor_state
        candidate._prepared_predecessor_initial=predecessor_initial
        candidate._prepared_predecessor_generation=predecessor_generation
        candidate._prepared_predecessor_baseline=baseline
        candidate._prepared_source_snapshot=source_snapshot
        candidate._prepared_candidate_state=candidate._state
        candidate._prepared_candidate_initial=candidate._initial
        candidate._prepared_initial_files=candidate._initial_files
        if (
            self._ownership_state != "active"
            or self._ownership_generation != predecessor_generation
            or self._state is not predecessor_state
            or self._initial is not predecessor_initial
            or self._reload_baseline != baseline
            or self.has_changes
            or self._undo
            or self._redo
            or self._state.actions
            or source_snapshot != self.reload_source_snapshot()
        ):
            candidate._retire_ownership()
            return None
        return source_snapshot,candidate

    def activate_verified_tag_rebase(
        self,
        expected_predecessor: "TagOrderWorkspaceSession",
        exact_source_snapshot: tuple[bytes | None, ...],
    ) -> "TagOrderWorkspaceSession":
        """Atomically transfer staged-record ownership to a prepared candidate."""
        if self._ownership_state != "prepared":
            raise RuntimeError(
                "Only a prepared Category Workspace snapshot can be activated."
            )
        try:
            snapshot=tuple(exact_source_snapshot)
            predecessor=self._prepared_predecessor
            if predecessor is not expected_predecessor:
                raise RuntimeError(
                    "The prepared Category Workspace predecessor does not match."
                )
            if predecessor is None or predecessor._ownership_state!="active":
                raise RuntimeError(
                    "The prepared Category Workspace predecessor is no longer active."
                )
            if (
                predecessor._ownership_generation
                != self._prepared_predecessor_generation
                or predecessor._state is not self._prepared_predecessor_state
                or predecessor._initial is not self._prepared_predecessor_initial
                or predecessor._reload_baseline
                != self._prepared_predecessor_baseline
                or predecessor.has_changes
                or predecessor._undo
                or predecessor._redo
                or predecessor._state.actions
            ):
                raise RuntimeError(
                    "The predecessor changed after this Tag rebase was prepared."
                )
            if snapshot!=self._prepared_source_snapshot:
                raise RuntimeError(
                    "The checked source snapshot does not match the prepared Tag rebase."
                )
            if (
                self._ownership_generation
                != self._prepared_predecessor_generation
                or self._state is not self._prepared_candidate_state
                or self._initial is not self._prepared_candidate_initial
                or self._initial_files != self._prepared_initial_files
                or self.has_changes
                or self._undo
                or self._redo
                or self._state.actions
            ):
                raise RuntimeError(
                    "The prepared Tag rebase changed before activation."
                )
            if (
                self.reload_source_snapshot()!=snapshot
                or predecessor.reload_source_snapshot()!=snapshot
                or self._reload_baseline!=snapshot
                or self._initial_files!=snapshot[:len(self._initial_files)]
            ):
                raise RuntimeError(
                    "System Knowledge changed after this Tag rebase was prepared."
                )
            if (
                self._state is predecessor._state
                or self._initial is predecessor._initial
                or self._state.tags is predecessor._state.tags
                or self._initial.tags is predecessor._initial.tags
            ):
                raise RuntimeError(
                    "The prepared Tag rebase does not own distinct staged maps."
                )
        except Exception:
            self._retire_ownership()
            raise
        next_generation=predecessor._ownership_generation+1
        predecessor._retire_ownership()
        self._ownership_state="active"
        self._ownership_generation=next_generation
        self._prepared_predecessor=None
        self._prepared_predecessor_state=None
        self._prepared_predecessor_initial=None
        self._prepared_predecessor_generation=None
        self._prepared_predecessor_baseline=None
        self._prepared_source_snapshot=None
        self._prepared_candidate_state=None
        self._prepared_candidate_initial=None
        self._prepared_initial_files=None
        return self

    def prepare_detached_reload(self):
        """Load saved authority without reading or publishing mutable live records.

        The caller must compare the returned snapshot again before adoption.
        A concurrent save discards this derived result; it never writes files.
        """
        self._require_active_ownership("prepare a detached reload")
        before=self.reload_source_snapshot()
        tags=TagDatabase(
            self.live_tags.path,
            rule_pair_admission=self.live_tags.rule_pair_admission,
        )
        categories=CategoryRegistry(self.live_categories.default_path,self.live_categories.user_path)
        patterns=(PatternRegistry(self.live_patterns.default_path,self.live_patterns.user_path)
                  if self.live_patterns is not None else None)
        characters=(CharacterDatabase(self.characters.data_dir,recover_interrupted=False)
                    if self.characters is not None else None)
        tags.apply_category_ordering(categories)
        session=CategoryWorkspaceSession(
            tags,categories,patterns,characters,
            default_relationship_path=self.default_relationship_path,
            user_relationship_path=self.user_relationship_path,
            foundation=FoundationKnowledge(self._modifier_foundation.modifiers_path,
                                           self._modifier_foundation.concepts_path),
            publish_live_authority=False,
        )
        if before!=self.reload_source_snapshot():return None
        return before,session

    @property
    def has_changes(self) -> bool:
        return self._state.changed

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def _category_effective(self, category_id: str) -> tuple[str, int, str]:
        seen: set[str] = set()
        current = self._state.categories.get(category_id)
        while current is not None and current.id not in seen:
            seen.add(current.id)
            candidate = current
            if not candidate.ordering_group and current.id in self._default_categories:
                candidate = self._default_categories[current.id]
            if candidate.ordering_group:
                return candidate.ordering_group, int(candidate.ordering_priority or ORDER_GROUP_DEFAULT_PRIORITY[candidate.ordering_group]), current.id
            current = self._state.categories.get(current.parent_id or "")
        return "other", ORDER_GROUP_DEFAULT_PRIORITY["other"], category_id

    def effective_semantic_role(self, category_id: str | None) -> str:
        seen: set[str] = set()
        current = self._state.categories.get(str(category_id or ""))
        while current is not None and current.id not in seen:
            seen.add(current.id)
            role = category_own_semantic_role(current)
            if role == "ordinary":
                return ""
            if role:
                return role
            current = self._state.categories.get(current.parent_id or "")
        return ""

    def category_ids_for_role(self, role: str) -> tuple[str, ...]:
        wanted=str(role or "").strip().casefold()
        return tuple(
            sorted(
                (
                    record.id for record in self._state.categories.values()
                    if self.effective_semantic_role(record.id)==wanted
                ),
                key=lambda value:self.path_label(value).casefold(),
            )
        )

    def semantic_role_source(self, category_id: str | None) -> str:
        seen: set[str] = set()
        current = self._state.categories.get(str(category_id or ""))
        while current is not None and current.id not in seen:
            seen.add(current.id)
            if category_own_semantic_role(current):
                return current.id
            current = self._state.categories.get(current.parent_id or "")
        return ""

    def is_protected_category(self, category_id: str | None) -> bool:
        return category_is_protected(self._state.categories.get(str(category_id or "")))

    def category_order(self, group: str, *, parent_id: str | None = None) -> list[CategoryRecord]:
        self._require_readable_ownership()
        rows = []
        for record in self._state.categories.values():
            effective_group, priority, _ = self._category_effective(record.id)
            if effective_group == group and record.parent_id == parent_id:
                rows.append((record, priority))
        return [item[0] for item in sorted(rows, key=lambda item: (-item[1], item[0].name.casefold(), item[0].id))]

    def tag_order(self, category_id: str) -> list[TagRecord]:
        self._require_readable_ownership()
        rows = [record for record in self._state.tags.values() if record.category == category_id]
        return sorted(rows, key=lambda record: (ORDER_GROUP_RANK[record.ordering.group], -record.ordering.priority, record.display.casefold(), record.id))

    def all_categories(self) -> list[CategoryRecord]:
        self._require_readable_ownership()
        return list(self._state.categories.values())

    def all_tags(self) -> list[TagRecord]:
        self._require_readable_ownership()
        return list(self._state.tags.values())

    def get(self, category_id: str | None) -> CategoryRecord | None:
        self._require_readable_ownership()
        return self._state.categories.get(category_id or "")

    def direct_category_base(
        self, category_id: str | None,
    ) -> DirectCategoryBaseAuthority | None:
        record = self._state.categories.get(str(category_id or ""))
        if record is None:
            return None
        return direct_category_base_authority(
            record, self._category_base_source_index,
        )

    def effective_color(self, category_id: str):
        view=CategoryRegistry.__new__(CategoryRegistry)
        view.categories=self._state.categories
        return CategoryRegistry.effective_color(view,category_id)

    def exact_category_anchor_ids(self) -> dict[str,str]:
        if self._exact_category_anchor_cache is None:
            self._exact_category_anchor_cache=exact_category_anchor_ids(
                self._state.categories.values(),self._state.tags.values(),
            )
        return dict(self._exact_category_anchor_cache)

    def effective_base_tag(self, category_id: str, *, derived_anchors=None):
        view=CategoryRegistry.__new__(CategoryRegistry); view.categories=self._state.categories
        view._category_base_source_index=self._category_base_source_index
        anchors=self.exact_category_anchor_ids() if derived_anchors is None else derived_anchors
        return CategoryRegistry.effective_base_tag(
            view,category_id,derived_anchors=anchors,
        )

    def preview_category_base_application(
        self,category_id: str,target_id: str,*,expected_match_basis: str = "",
        member_scope: str = CATEGORY_BASE_MEMBER_SCOPE_DIRECT,
        allow_broader_review: bool = False,
    ) -> CategoryBaseApplicationPlan:
        """Revalidate one exact candidate and calculate its complete branch impact."""
        selected_scope=normalize_category_base_member_scope(member_scope)
        category=self._state.categories.get(str(category_id or ""))
        target=self._state.tags.get(str(target_id or ""))
        if category is None or target is None:
            raise ValueError(
                "This Category Base candidate is no longer available. Run Find New Knowledge again."
            )
        if target.category!=category.id:
            raise ValueError(
                "The proposed Base SDXL Tag is no longer a direct member of this Category. Run Find New Knowledge again."
            )
        direct=self.direct_category_base(category.id)
        if (
            direct is None or direct.ambiguous or direct.represented
            or direct.policy=="own"
        ):
            raise ValueError(
                "This Category now has direct or ambiguous Category Base authority. Review its current editor state instead."
            )
        descendants=self.descendant_ids(category.id)
        if descendants and not allow_broader_review:
            raise ValueError(
                "This Category now has descendant Categories. Its Base scope needs a broader explicit review."
            )
        category_key=normalize_tag(category.name)
        if target.id==category.id:
            match_basis="stable_id"
        elif normalize_tag(target.display)==category_key:
            match_basis="exact_canonical_name"
        elif category_key in {
            normalize_tag(value) for value in self.live_tags.effective_aliases(target)
        }:
            match_basis="declared_alias"
        else:
            raise ValueError(
                "The Category and proposed Base SDXL Tag no longer share exact reviewed identity. Run Find New Knowledge again."
            )
        if expected_match_basis and match_basis!=str(expected_match_basis):
            raise ValueError(
                "The exact identity evidence changed after discovery. Run Find New Knowledge again before applying it."
            )
        effective_id,effective_source,_policy=self.effective_base_tag(category.id)
        effective_target_unchanged=(
            effective_id==target.id and effective_source==category.id
        )
        if not effective_target_unchanged and not allow_broader_review:
            raise ValueError(
                "Applying this candidate would now change the effective Base target. It needs a broader explicit review."
            )
        direct_members=tuple(sorted(
            (record for record in self._state.tags.values() if record.category==category.id),
            key=lambda item:item.id,
        ))
        descendant_members=tuple(sorted(
            (
                record for record in self._state.tags.values()
                if record.category in descendants
            ),
            key=lambda item:item.id,
        ))
        branch_members=(*direct_members,*descendant_members)
        semantic_index=tag_semantic_relationship_index(
            self._effective_relationships().values(),
        )
        member_semantics=tuple(
            (
                record.id,
                record.category,
                effective_tag_semantic_links_from_index(
                    record,semantic_index,
                ).parent_id,
            )
            for record in branch_members
        )
        override_count=sum(
            bool(parent_id) for stable_id,_category_id,parent_id in member_semantics
            if stable_id!=target.id
        )
        candidate_user,_changed_ids=stage_category_base_change(
            category,self._state.tags,
            self.default_relationships.relationships,self._state.relationships,
            policy="own",base_tag_id=target.id,member_scope=selected_scope,
            source="category_workspace",
            reason="Previewed exact Category Base discovery candidate.",
        )
        candidate_effective=dict(self.default_relationships.relationships)
        candidate_effective.update(candidate_user)
        after_view=CategoryRegistry.__new__(CategoryRegistry)
        after_view.categories=self._state.categories
        after_view._category_base_source_index=category_base_relationship_index(
            candidate_effective.values(),
        )
        anchors=self.exact_category_anchor_ids()
        before_results={
            record.id:self.live_tags.effective_base_tag(
                record,self,derived_anchors=anchors,
            )
            for record in branch_members
        }
        after_results={
            record.id:self.live_tags.effective_base_tag(
                record,after_view,derived_anchors=anchors,
            )
            for record in branch_members
        }
        changed_effective_base_count=sum(
            before_results[record.id][0]!=after_results[record.id][0]
            for record in branch_members
        )
        fingerprint=_payload_fingerprint({
            "category":category.to_dict(),
            "target":target.to_dict(),
            "direct":{
                "base_tag_id":direct.base_tag_id,"policy":direct.policy,
                "represented":direct.represented,"ambiguous":direct.ambiguous,
                "relationship_ids":direct.relationship_ids,
            },
            "descendants":[
                self._state.categories[value].to_dict()
                for value in sorted(descendants)
            ],
            "member_semantics":member_semantics,
            "before_results":before_results,
            "after_results":after_results,
            "effective_base":[effective_id,effective_source],
            "match_basis":match_basis,
            "member_scope":selected_scope,
            "broader_review":bool(allow_broader_review),
        })
        return CategoryBaseApplicationPlan(
            category_id=category.id,category=category.name,
            target_id=target.id,target=target.display,match_basis=match_basis,
            member_scope=selected_scope,
            broader_review=bool(allow_broader_review),
            effective_target_unchanged=effective_target_unchanged,
            direct_member_count=len(direct_members),
            affected_direct_member_count=max(0,len(direct_members)-1),
            descendant_category_count=len(descendants),
            descendant_member_count=len(descendant_members),
            affected_descendant_member_count=(
                len(descendant_members) if selected_scope=="descendants" else 0
            ),
            changed_effective_base_count=changed_effective_base_count,
            tag_specific_base_override_count=override_count,
            fingerprint=fingerprint,
        )

    def apply_category_base_application(
        self,plan: CategoryBaseApplicationPlan,
    ) -> CategoryBaseApplicationPlan:
        """Stage one still-current reviewed discovery plan for atomic saving."""
        if not isinstance(plan,CategoryBaseApplicationPlan):
            raise TypeError("A Category Base application plan is required.")
        current=self.preview_category_base_application(
            plan.category_id,plan.target_id,
            expected_match_basis=plan.match_basis,
            member_scope=plan.member_scope,
            allow_broader_review=plan.broader_review,
        )
        if current.fingerprint!=plan.fingerprint:
            raise RuntimeError(
                "The Category Base candidate changed after preview. Nothing was applied; review a fresh preview."
            )
        category=self._state.categories[plan.category_id]
        self.update_category(
            category.id,name=category.name,parent_id=category.parent_id,
            description=category.description,color=category.color,
            color_policy=category.color_policy,base_tag_id=plan.target_id,
            base_tag_policy="own",
            base_member_scope=plan.member_scope,
            sensitivity_rating=category.sensitivity_rating,
            sensitivity_policy=category.sensitivity_policy,
            reusable_modifier_family=(
                category.extras.get(REUSABLE_MODIFIER_FAMILY_EXTRA) is True
            ),
        )
        return current

    def _category_dependency_snapshot(self, category_id: str) -> dict[str,tuple]:
        """Capture exact per-Tag policies inherited through one category branch."""
        selected={category_id,*self.descendant_ids(category_id)}
        anchors=self.exact_category_anchor_ids()
        policies={
            stable_id:(
                self.effective_base_tag(
                    stable_id,derived_anchors=anchors,
                ),
                self.modifier_family_source(stable_id),
            )
            for stable_id in selected
        }
        return {
            record.id:policies[record.category]
            for record in self._state.tags.values()
            if record.category in selected
        }

    def _refresh_category_base_authority(self, *, publish_live: bool = False) -> None:
        effective = self._effective_relationships()
        self._category_base_source_index = category_base_relationship_index(
            effective.values(),
        )
        self._clear_represented_category_base_fields(
            self._state.categories, self._category_base_source_index,
        )
        if publish_live:
            self.live_categories.configure_category_base_authority(
                effective.values(),
            )

    @staticmethod
    def _clear_represented_category_base_fields(
        categories: dict[str, CategoryRecord],
        source_index: dict[str, tuple[Any, ...]],
    ) -> None:
        for record in categories.values():
            if source_index.get(record.id):
                record.base_tag_id = ""
                record.base_tag_policy = (
                    "inherit" if record.parent_id else "none"
                )

    def effective_sensitivity(self, category_id: str):
        view=CategoryRegistry.__new__(CategoryRegistry); view.categories=self._state.categories
        return CategoryRegistry.effective_sensitivity(view,category_id)

    def is_reusable_modifier_family(self, category_id: str | None) -> bool:
        view=CategoryRegistry.__new__(CategoryRegistry); view.categories=self._state.categories
        return CategoryRegistry.is_reusable_modifier_family(view,category_id)

    def modifier_family_source(self, category_id: str | None) -> str:
        view=CategoryRegistry.__new__(CategoryRegistry); view.categories=self._state.categories
        return CategoryRegistry.modifier_family_source(view,category_id)

    def reusable_modifier_family_ids(self) -> tuple[str,...]:
        view=CategoryRegistry.__new__(CategoryRegistry); view.categories=self._state.categories
        return CategoryRegistry.reusable_modifier_family_ids(view)

    def path_ids(self, category_id: str) -> list[str]:
        result=[]; seen=set(); current=self._state.categories.get(category_id)
        while current is not None and current.id not in seen:
            result.append(current.id); seen.add(current.id)
            current=self._state.categories.get(current.parent_id or "")
        return list(reversed(result))

    def path_label(self, category_id: str) -> str:
        ids=self.path_ids(category_id)
        return " → ".join(self._state.categories[value].name for value in ids) if ids else category_id

    def descendant_ids(self, category_id: str) -> set[str]:
        cached=self._category_descendant_cache.get(category_id)
        if cached is not None:
            return set(cached)
        if self._category_children_cache is None:
            children: dict[str | None,list[str]]={}
            for record in self._state.categories.values():
                children.setdefault(record.parent_id,[]).append(record.id)
            self._category_children_cache={
                parent:tuple(values) for parent,values in children.items()
            }
        result=set(); pending=[category_id]
        while pending:
            parent=pending.pop()
            for stable_id in self._category_children_cache.get(parent,()):
                if stable_id not in result:
                    result.add(stable_id); pending.append(stable_id)
        result.discard(category_id)
        self._category_descendant_cache[category_id]=frozenset(result)
        return set(result)

    def direct_tag_count(self, category_id: str) -> int:
        if self._direct_tag_count_cache is None:
            counts: dict[str,int]={}
            for record in self._state.tags.values():
                counts[record.category]=counts.get(record.category,0)+1
            self._direct_tag_count_cache=counts
        return self._direct_tag_count_cache.get(category_id,0)

    def total_tag_count(self, category_id: str) -> int:
        categories={category_id,*self.descendant_ids(category_id)}
        return sum(self.direct_tag_count(value) for value in categories)

    def duplicate_groups(self) -> list[list[CategoryRecord]]:
        groups={}
        for record in self._state.categories.values():
            key=(record.parent_id," ".join(record.name.casefold().split()))
            groups.setdefault(key,[]).append(record)
        return [values for values in groups.values() if len(values)>1]

    def structural_issue_records(
        self, state: _State | None = None,
    ) -> tuple[CategoryStructuralIssue, ...]:
        workspace = state or self._state
        issues=[]
        category_base_index = category_base_relationship_index(
            self._effective_relationships(state).values(),
        )
        groups={}
        for record in workspace.categories.values():
            key=(record.parent_id," ".join(record.name.casefold().split()))
            groups.setdefault(key,[]).append(record)
        for values in (items for items in groups.values() if len(items)>1):
            issues.append(CategoryStructuralIssue(
                "duplicate_siblings","Categories",
                "Duplicate sibling categories: "+", ".join(sorted(record.name for record in values)),
                tuple(("category",record.id) for record in values),
            ))
        for record in workspace.categories.values():
            if record.parent_id and record.parent_id not in workspace.categories:
                issues.append(CategoryStructuralIssue(
                    "missing_parent","Categories",f"{record.name}: missing parent {record.parent_id}",
                    (("category",record.id),),
                ))
            seen={record.id}; current=workspace.categories.get(record.parent_id or "")
            while current is not None:
                if current.id in seen:
                    issues.append(CategoryStructuralIssue(
                        "parent_cycle","Categories",f"{record.name}: category parent cycle",
                        (("category",record.id),),
                    )); break
                seen.add(current.id); current=workspace.categories.get(current.parent_id or "")
            direct_base = direct_category_base_authority(
                record, category_base_index,
            )
            if direct_base.ambiguous:
                issues.append(CategoryStructuralIssue(
                    "ambiguous_category_base","Categories",
                    f"{record.name}: canonical Category Base authority is ambiguous",
                    (("category",record.id),),
                ))
            elif direct_base.policy=="own" and (
                not direct_base.base_tag_id
                or direct_base.base_tag_id not in workspace.tags
            ):
                issues.append(CategoryStructuralIssue(
                    "missing_category_base_tag","Categories",f"{record.name}: missing base SDXL Tag reference",
                    (("category",record.id),),
                ))
        for record in workspace.tags.values():
            if record.category not in workspace.categories:
                issues.append(CategoryStructuralIssue(
                    "missing_tag_category","SDXL Tags",f"{record.display}: missing category {record.category}",
                    (("tag",record.id),),
                ))
        for record in workspace.patterns.values():
            if record.category not in workspace.categories:
                issues.append(CategoryStructuralIssue(
                    "missing_pattern_category","Reusable patterns",f"{record.name}: missing category {record.category}",
                    (("pattern",record.id),),
                ))
        return tuple({(issue.code,issue.summary):issue for issue in issues}.values())

    def structural_issues(self) -> tuple[str, ...]:
        return tuple(issue.summary for issue in self.structural_issue_records())

    def _find_sibling(self, name: str, parent_id: str | None, *, exclude_id: str = "") -> CategoryRecord | None:
        wanted=" ".join(name.strip().casefold().split())
        return next((record for record in self._state.categories.values() if record.id!=exclude_id and record.parent_id==parent_id and " ".join(record.name.casefold().split())==wanted),None)

    def _validate_parent(self, category_id: str, parent_id: str | None) -> None:
        if parent_id and parent_id not in self._state.categories:
            raise ValueError("Choose an existing parent category.")
        if parent_id and (parent_id==category_id or category_id in self.path_ids(parent_id)):
            raise ValueError("A category cannot be placed beneath itself or one of its descendants.")

    def _stage_category_base_for_record(
        self,
        record: CategoryRecord,
        *,
        policy: str | None = None,
        base_tag_id: str | None = None,
        member_scope: str | None = None,
        previous_base_tag_id: str = "",
        reason: str = "",
    ) -> tuple[str, ...]:
        current = self.direct_category_base(record.id)
        selected_policy = (
            str(policy)
            if policy is not None
            else str(getattr(current, "policy", record.base_tag_policy))
        )
        selected_base_tag = (
            str(base_tag_id or "")
            if base_tag_id is not None
            else str(getattr(current, "base_tag_id", record.base_tag_id) or "")
        )
        selected_member_scope = (
            normalize_category_base_member_scope(member_scope)
            if member_scope is not None
            else str(
                getattr(
                    current,
                    "member_scope",
                    CATEGORY_BASE_MEMBER_SCOPE_DIRECT,
                )
            )
        )
        relationships, changed_ids = stage_category_base_change(
            record,
            self._state.tags,
            self.default_relationships.relationships,
            self._state.relationships,
            policy=selected_policy,
            base_tag_id=selected_base_tag,
            member_scope=selected_member_scope,
            previous_base_tag_id=previous_base_tag_id,
            source="category_workspace",
            reason=reason,
        )
        self._state.relationships = relationships
        if changed_ids:
            self._state.relationship_file_exists = True
        self._refresh_category_base_authority()
        return changed_ids

    def create_category(self, name: str, *, parent_id: str | None = None, description: str = "",
                        color: str = "", color_policy: str | None = None,
                        base_tag_id: str = "", base_tag_policy: str | None = None,
                        base_member_scope: str = CATEGORY_BASE_MEMBER_SCOPE_DIRECT,
                        sensitivity_rating: str = "", sensitivity_policy: str | None = None,
                        stable_id: str = "", extras: dict[str,Any] | None = None) -> CategoryRecord:
        from .category_registry import normalize_category_color, normalize_category_color_policy, normalize_category_reference_policy
        cleaned=name.strip()
        if not cleaned:
            raise ValueError("Enter a category name.")
        self._validate_parent("",parent_id)
        if self._find_sibling(cleaned,parent_id):
            raise ValueError("A category with this name already exists under the selected parent.")
        requested_id=str(stable_id or "").strip()
        if requested_id and requested_id in self._state.categories:
            raise ValueError("That stable Category identity is already in use.")
        base=CategoryRegistry.slug(cleaned); category_id=requested_id or base; counter=2
        while category_id in self._state.categories:
            category_id=f"{base}_{counter}"; counter+=1
        normalized_color=normalize_category_color(color)
        if color_policy is None and parent_id and not normalized_color:color_policy="parent_lighter"
        self._checkpoint()
        normalized_sensitivity=str(sensitivity_rating or "").casefold()
        if normalized_sensitivity not in {"general","sensitive","questionable","explicit"}:normalized_sensitivity=""
        normalized_base_policy=normalize_category_reference_policy(
            base_tag_policy,parent_id=parent_id,own_value=base_tag_id,
        )
        record=CategoryRecord(
            id=category_id,name=cleaned,parent_id=parent_id,description=description.strip(),source="user",
            color=normalized_color,
            color_policy=normalize_category_color_policy(color_policy,color=normalized_color,parent_id=parent_id),
            base_tag_id=(str(base_tag_id or "") if normalized_base_policy=="own" else ""),
            base_tag_policy=normalized_base_policy,
            sensitivity_rating=normalized_sensitivity,
            sensitivity_policy=normalize_category_reference_policy(sensitivity_policy,parent_id=parent_id,own_value=normalized_sensitivity),
            extras=copy.deepcopy(extras or {}),
        )
        if parent_id:
            record.ordering_group=""
        else:
            record.ordering_group="other"; record.ordering_priority=ORDER_GROUP_DEFAULT_PRIORITY["other"]
            record.ordering_reviewed=False
        self._state.categories[record.id]=record
        relationship_ids = self._stage_category_base_for_record(
            record,
            policy=normalized_base_policy,
            base_tag_id=base_tag_id,
            member_scope=base_member_scope,
            reason="Created with an explicit Category Base Tag.",
        ) if normalized_base_policy == "own" else ()
        self._add_action({
            "kind":"category","id":record.id,"action":"create",
            "relationship_ids":list(relationship_ids),
        })
        return record

    def update_category(self, category_id: str, *, name: str, parent_id: str | None,
                        description: str = "", color: str = "", color_policy: str | None = None,
                        base_tag_id: str | None = None, base_tag_policy: str | None = None,
                        base_member_scope: str | None = None,
                        sensitivity_rating: str | None = None, sensitivity_policy: str | None = None,
                        reusable_modifier_family: bool | None = None) -> CategoryRecord:
        from .category_registry import normalize_category_color, normalize_category_color_policy, normalize_category_reference_policy
        record=self._state.categories[category_id]; cleaned=name.strip()
        if category_is_protected(record) and record.parent_id!=parent_id:
            raise ValueError(
                f"{record.name} has a protected system role. Its semantic parent cannot be changed."
            )
        if not cleaned:
            raise ValueError("Enter a category name.")
        self._validate_parent(category_id,parent_id)
        if self._find_sibling(cleaned,parent_id,exclude_id=category_id):
            raise ValueError("A category with this name already exists under the selected parent.")
        normalized_color=normalize_category_color(color)
        if record.parent_id is None and parent_id and not normalized_color and record.color_policy=="none" and color_policy in {None,"none"}:
            color_policy="parent_lighter"
        normalized_policy=normalize_category_color_policy(color_policy,color=normalized_color,parent_id=parent_id)
        current_base = self.direct_category_base(category_id)
        current_base_tag = str(getattr(current_base, "base_tag_id", "") or "")
        current_base_policy = (
            "none"
            if getattr(current_base, "ambiguous", False)
            else str(getattr(current_base, "policy", record.base_tag_policy))
        )
        current_base_scope = str(
            getattr(
                current_base,
                "member_scope",
                CATEGORY_BASE_MEMBER_SCOPE_DIRECT,
            )
        )
        selected_base_tag=current_base_tag if base_tag_id is None else str(base_tag_id or "")
        selected_base_policy=current_base_policy if base_tag_policy is None else str(base_tag_policy or "")
        selected_base_scope=(
            current_base_scope
            if base_member_scope is None
            else normalize_category_base_member_scope(base_member_scope)
        )
        selected_sensitivity=record.sensitivity_rating if sensitivity_rating is None else str(sensitivity_rating or "").casefold()
        selected_sensitivity_policy=record.sensitivity_policy if sensitivity_policy is None else str(sensitivity_policy or "")
        if record.parent_id is None and parent_id:
            if base_tag_policy in {None,"none"} and current_base_policy=="none" and not selected_base_tag:selected_base_policy="inherit"
            if sensitivity_policy in {None,"none"} and record.sensitivity_policy=="none" and not selected_sensitivity:selected_sensitivity_policy="inherit"
        if selected_sensitivity not in {"general","sensitive","questionable","explicit"}:selected_sensitivity=""
        normalized_base_policy=normalize_category_reference_policy(selected_base_policy,parent_id=parent_id,own_value=selected_base_tag)
        if normalized_base_policy!="own":selected_base_tag=""
        normalized_sensitivity_policy=normalize_category_reference_policy(selected_sensitivity_policy,parent_id=parent_id,own_value=selected_sensitivity)
        migrate_base_projection=(
            normalized_base_policy=="own"
            and record.id not in self._category_base_source_index
        )
        migrate_base_scope=(
            normalized_base_policy=="own"
            and bool(getattr(current_base,"represented",False))
            and not bool(getattr(current_base,"scope_reviewed",True))
            and base_member_scope is not None
        )
        selected_family=(
            record.extras.get(REUSABLE_MODIFIER_FAMILY_EXTRA) is True
            if reusable_modifier_family is None else bool(reusable_modifier_family)
        )
        signature=(record.name,record.parent_id,record.description,record.color,record.color_policy,current_base_tag,current_base_policy,current_base_scope,record.sensitivity_rating,record.sensitivity_policy,record.extras.get(REUSABLE_MODIFIER_FAMILY_EXTRA) is True)
        if (
            signature==(cleaned,parent_id,description.strip(),normalized_color,normalized_policy,selected_base_tag,normalized_base_policy,selected_base_scope,selected_sensitivity,normalized_sensitivity_policy,selected_family)
            and not migrate_base_projection
            and not migrate_base_scope
        ):
            return record
        old_name=record.name
        old_parent=record.parent_id
        old_base_tag_id=current_base_tag
        old_base_policy=current_base_policy
        dependency_may_change=bool(
            old_parent==parent_id
            and (
                old_name!=cleaned
                or (old_base_tag_id,old_base_policy,current_base_scope)!=(
                    selected_base_tag,normalized_base_policy,selected_base_scope,
                )
                or (
                    record.extras.get(REUSABLE_MODIFIER_FAMILY_EXTRA) is True
                )!=selected_family
            )
        )
        dependency_before=(
            self._category_dependency_snapshot(category_id)
            if dependency_may_change else {}
        )
        sensitivity_changed=(
            record.sensitivity_rating,record.sensitivity_policy
        )!=(selected_sensitivity,normalized_sensitivity_policy)
        inherited_tag_ids=self._inherited_tag_ids() if old_parent!=parent_id else ()
        # Exact category anchors depend on a category's canonical name and its
        # direct tag names/categories. Presentation, policy, and parent changes
        # cannot alter that derived lookup, so keep the warm cache for those
        # common inspector edits instead of rescanning a large tag library.
        self._checkpoint(
            categories=(category_id,),
            tags=inherited_tag_ids,
            invalidate_category_anchors=record.name != cleaned,
        )
        record=self._state.categories[category_id]
        record.name=cleaned; record.parent_id=parent_id; record.description=description.strip()
        record.color=normalized_color; record.color_policy=normalized_policy; record.source="user"
        record.base_tag_id=selected_base_tag; record.base_tag_policy=normalized_base_policy
        record.sensitivity_rating=selected_sensitivity; record.sensitivity_policy=normalized_sensitivity_policy
        if selected_family:record.extras[REUSABLE_MODIFIER_FAMILY_EXTRA]=True
        else:record.extras.pop(REUSABLE_MODIFIER_FAMILY_EXTRA,None)
        if old_parent!=parent_id and parent_id and not record.ordering_reviewed:
            record.ordering_group=""; record.ordering_priority=None
            self._refresh_inherited_tags()
        base_changed=(old_base_tag_id,old_base_policy,current_base_scope)!=(
            record.base_tag_id,record.base_tag_policy,selected_base_scope,
        )
        refresh_base_authority=(
            base_changed
            or migrate_base_projection
            or migrate_base_scope
            or (
                old_name!=record.name
                and record.id in self._category_base_source_index
            )
        )
        relationship_ids = self._stage_category_base_for_record(
            record,
            policy=normalized_base_policy,
            base_tag_id=selected_base_tag,
            member_scope=selected_base_scope,
            previous_base_tag_id=old_base_tag_id,
            reason="Reviewed in Category Workspace.",
        ) if refresh_base_authority else ()
        if not refresh_base_authority:
            self._clear_represented_category_base_fields(
                {record.id:record},self._category_base_source_index,
            )
        dependency_after=(
            self._category_dependency_snapshot(category_id)
            if dependency_may_change else {}
        )
        dependency_tag_ids=tuple(
            stable_id for stable_id,value in dependency_before.items()
            if dependency_after.get(stable_id)!=value
        )
        self._add_action({
            "kind":"category","id":record.id,"action":"update",
            "old_parent_id":old_parent,"parent_id":parent_id,
            "sensitivity_changed":sensitivity_changed,
            "base_changed":base_changed,
            "category_base_changed":bool(relationship_ids),
            "dependency_tag_ids":list(dependency_tag_ids),
            "relationship_ids":list(relationship_ids),
        })
        return record

    def restore_category_default(self, category_id: str) -> None:
        default=self._default_categories.get(category_id)
        if default is None:
            raise ValueError("User-created categories do not have shipped defaults to restore.")
        current=self._state.categories.get(category_id)
        if current is not None and current.source=="default" and category_id not in self._state.disabled_default_ids:
            return
        self._checkpoint(tags=self._inherited_tag_ids()); self._state.categories[category_id]=copy.deepcopy(default)
        relationships, changed_ids = stage_category_base_restore(
            category_id,
            self.default_relationships.relationships,
            self._state.relationships,
        )
        self._state.relationships = relationships
        if changed_ids:
            self._state.relationship_file_exists = (
                bool(relationships) or self._initial.relationship_file_exists
            )
        self._refresh_category_base_authority()
        self._state.disabled_default_ids.discard(category_id)
        self._add_action({
            "kind":"category","id":category_id,"action":"restore_default",
            "relationship_ids":list(changed_ids),
        })
        self._refresh_inherited_tags()

    def change_tag_categories(self, tag_ids, category_id: str) -> None:
        if category_id not in self._state.categories:
            raise ValueError("Choose an existing category.")
        selected=tuple(dict.fromkeys(str(value) for value in tag_ids))
        if not selected:
            raise ValueError("Choose at least one SDXL Tag.")
        records=[self._state.tags[value] for value in selected]
        changed=[record for record in records if record.category!=category_id]
        if not changed:
            return
        changed_ids=tuple(record.id for record in changed)
        self._checkpoint(tags=changed_ids)
        changed=[self._state.tags[value] for value in changed_ids]
        for record in changed:
            old=record.category; record.category=category_id
            if not record.ordering.reviewed:
                group,priority,_=self._category_effective(category_id)
                record.ordering.group=group; record.ordering.priority=priority
            record.provenance.append({"source":"category_workspace","reviewed_at":_now(),"action":"change_category","previous_category":old,"category":category_id})
        self._add_action({"kind":"tag","ids":[record.id for record in changed],"action":"change_category","category":category_id})


    def stage_reviewed_template_tags(
        self, entries: list[dict[str,Any]],
    ) -> tuple[tuple[str,...],tuple[str,...]]:
        """Stage one structured template scope with bounded shared indexes."""

        if not entries:raise ValueError("Choose at least one reviewed template Tag.")
        effective=self._effective_relationships()
        semantic_index=tag_semantic_relationship_index(effective.values())
        resolutions=modifier_composition_resolutions(
            effective.values(),tags=self._tag_view(self._state.tags),
            categories=self._staged_modifier_categories(),
            foundation=self._modifier_foundation,
        )
        identity_owners={}
        for record in self._state.tags.values():
            for identity_name in (record.display,*record.aliases):
                normalized_identity=normalize_tag(identity_name)
                if normalized_identity:identity_owners.setdefault(normalized_identity,record)
        planned=[]; stable_ids=set(); normalized_names=set()
        for raw in entries:
            item=dict(raw); stable_id=str(item.get("tag_id") or "").strip()
            name=str(item.get("display") or "").strip()
            category_id=str(item.get("category_id") or "").strip()
            anchor_id=str(item.get("anchor_id") or "").strip()
            additional=tuple(
                (str(value.get("tag_id") or "").strip(),str(value.get("role") or "").strip())
                for value in item.get("additional_anchors",()) if isinstance(value,dict)
            )
            modifier_values=tuple(
                (str(value.get("family_id") or "").strip(),str(value.get("value_id") or "").strip())
                for value in item.get("modifier_values",()) if isinstance(value,dict)
            ) or ((
                str(item.get("modifier_family_id") or "").strip(),
                str(item.get("value_id") or "").strip(),
            ),)
            if not stable_id or not name or stable_id in stable_ids:
                raise ValueError("Each generated SDXL Tag needs one unique stable identity and name.")
            normalized=normalize_tag(name)
            if not normalized or normalized in normalized_names:
                raise ValueError("The reviewed template scope contains duplicate rendered Tag names.")
            stable_ids.add(stable_id); normalized_names.add(normalized)
            if category_id not in self._state.categories:
                raise ValueError("The reviewed template destination Category is unavailable.")
            anchor=self._state.tags.get(anchor_id)
            if anchor is None or any(
                not stable_anchor or stable_anchor not in self._state.tags
                or role not in {"component","implication"}
                for stable_anchor,role in additional
            ):
                raise ValueError("A reviewed template anchor is unavailable or has an unresolved role.")
            if not modifier_values or any(not family or not value for family,value in modifier_values):
                raise ValueError("A reviewed template result has no complete typed modifier values.")
            if len({family for family,_value in modifier_values})!=len(modifier_values):
                raise ValueError("One generated Tag cannot store conflicting values in the same single-valued modifier dimension.")
            for family_id,value_id in modifier_values:
                value=self._state.tags.get(value_id)
                if value is None:raise ValueError("A reviewed template modifier value is unavailable.")
                value_role=self._modifier_foundation.modifier_for_tag(
                    value,self._staged_modifier_categories(),
                )
                if str(getattr(value_role,"group","") or "")!=family_id:
                    raise ValueError("A selected modifier value no longer belongs to the reviewed dimension.")
            existing=self._state.tags.get(stable_id)
            if existing is None:
                if identity_owners.get(normalized) is not None:
                    raise RuntimeError("A generated Tag became available under another stable ID. Refresh the organization plan.")
                if stable_id in self._state.removed_tag_ids or stable_id in self._state.merged_tag_ids:
                    raise RuntimeError("Deletion or merge history reserves a generated stable Tag identity.")
            elif normalized!=normalize_tag(existing.display) and normalized not in {
                normalize_tag(alias) for alias in existing.aliases
            }:
                raise RuntimeError("A planned stable Tag identity now belongs to another name.")
            semantic=(effective_tag_semantic_links_from_index(existing,semantic_index) if existing is not None else None)
            needs_parent=not bool(
                semantic is not None and semantic.specificity_represented
                and semantic.parent_id==anchor_id
            )
            if semantic is not None and semantic.parent_id not in {"",anchor_id}:
                raise RuntimeError("A planned Tag now has a conflicting reviewed Broader Tag.")
            needs_modifiers=[]
            for family_id,value_id in modifier_values:
                slot=resolutions.get(stable_id,{}).get(family_id)
                if slot is not None and (not slot.is_set or str(slot.value_id or "")!=value_id):
                    raise RuntimeError("A planned Tag now has a conflicting reviewed modifier value.")
                if slot is None:needs_modifiers.append((family_id,value_id))
            additional_needs=[]
            for stable_anchor,role in additional:
                relation_id="tag_template_anchor."+hashlib.sha256(
                    f"{stable_id}\x1f{stable_anchor}\x1f{role}".encode("utf-8")
                ).hexdigest()[:20]
                current=effective.get(relation_id)
                if current is not None and current.kind!=ENTAILMENT:
                    raise RuntimeError("A reviewed composite-anchor relationship identity is reserved by incompatible authority.")
                if current is not None:
                    endpoint_ids=tuple(
                        str(endpoint.id) for endpoint in current.endpoints
                    )
                    details=dict(current.details)
                    if (
                        endpoint_ids!=(stable_id,stable_anchor)
                        or details.get("template_anchor_role")!=role
                    ):
                        raise RuntimeError(
                            "A reviewed composite-anchor relationship no longer matches this template role."
                        )
                if current is None:additional_needs.append((stable_anchor,role,relation_id))
            provenance={
                "source":"reviewed_tag_template","template_id":str(item.get("template_id") or ""),
                "template_version":int(item.get("template_version") or 0),
                "result_id":str(item.get("result_id") or ""),
                "anchor_ids":[anchor_id,*[value[0] for value in additional]],
                "anchor_roles":["base",*[value[1] for value in additional]],
                "modifier_family_ids":[value[0] for value in modifier_values],
                "value_ids":[value[1] for value in modifier_values],
                "selected_slot_values":copy.deepcopy(item.get("selected_slot_values",[])),
                "rendered_name":name,"created_at":str(item.get("created_at") or ""),
                "operation_id":str(item.get("operation_id") or ""),
            }
            has_provenance=bool(existing is not None and any(
                value.get("source")=="reviewed_tag_template"
                and str(value.get("template_id") or "")==provenance["template_id"]
                and str(value.get("template_version") or "")==str(provenance["template_version"])
                and str(value.get("result_id") or "")==provenance["result_id"]
                for value in existing.provenance if isinstance(value,dict)
            ))
            planned.append({
                **item,"tag_id":stable_id,"display":name,"category_id":category_id,
                "anchor_id":anchor_id,"additional_anchors":additional,
                "modifier_values":modifier_values,"existing":existing,
                "needs_parent":needs_parent,"needs_modifiers":tuple(needs_modifiers),
                "additional_needs":tuple(additional_needs),
                "provenance":provenance,"has_provenance":has_provenance,
            })
        changed=[item for item in planned if (
            item["existing"] is None or item["needs_parent"] or item["needs_modifiers"]
            or item["additional_needs"] or not item["has_provenance"]
        )]
        if not changed:return (),()
        self._checkpoint(tags=tuple(item["tag_id"] for item in changed if item["existing"] is not None))
        created=[]
        for item in changed:
            if item["existing"] is None:
                group,priority,_source=self._category_effective(item["category_id"])
                record=TagRecord(
                    id=item["tag_id"],display=item["display"],category=item["category_id"],
                    first_seen=item["created_at"],last_seen=item["created_at"],
                    provenance=[item["provenance"]],
                    ordering=TagOrdering(group=group,priority=priority,reviewed=False),
                    extras={"tag_template_creation":copy.deepcopy(item["provenance"])},
                )
                self._state.tags[record.id]=record; created.append(record.id)
            elif not item["has_provenance"]:
                self._state.tags[item["tag_id"]].provenance.append(item["provenance"])
        candidate=self._tag_view(self._state.tags)
        semantic_changes=tuple(
            TagSemanticChange(
                tag_id=item["tag_id"],parent_id=item["anchor_id"],kinds=(SPECIFICITY,),
                source="reviewed_tag_template",
                reason=f"Reviewed reusable template {item['template_id']} v{item['template_version']}.",
            ) for item in changed if item["needs_parent"]
        )
        changed_relationship_ids=[]
        if semantic_changes:
            workspace=RelationshipWorkspaceService(
                self.default_relationship_path,self.user_relationship_path,tags=candidate,
            )
            workspace.user_store.schema_version=self.user_relationships.schema_version
            workspace.user_store.extras=copy.deepcopy(self.user_relationships.extras)
            workspace.user_store.relationships=dict(self._state.relationships)
            staged,semantic_ids=stage_tag_semantic_changes(candidate,workspace,semantic_changes)
            self._state.tags=candidate.tags; self._state.relationships=staged
            changed_relationship_ids.extend(semantic_ids)
        effective=self._effective_relationships()
        for item in changed:
            provenance=item["provenance"]
            for stable_anchor,role,relationship_id in item["additional_needs"]:
                relationship=KnowledgeRelationship(
                    id=relationship_id,kind=ENTAILMENT,endpoints=(
                        RelationshipEndpoint("tag",item["tag_id"],item["display"]),
                        RelationshipEndpoint("tag",stable_anchor,self._state.tags[stable_anchor].display),
                    ),directed=True,scope="any",
                    label=f"{item['display']} {role} anchor",reviewed=True,
                    source="reviewed_tag_template",details=(
                        ("template_anchor_role",role),("template_id",item["template_id"]),
                        ("template_version",str(item["template_version"])),
                        ("result_id",item["result_id"]),("operation_id",item["operation_id"]),
                    ),layer="user",disabled=False,
                    extras={"tag_template_creation":copy.deepcopy(provenance)},
                )
                self._state.relationships[relationship_id]=relationship
                effective[relationship_id]=relationship; changed_relationship_ids.append(relationship_id)
            for family_id,value_id in item["needs_modifiers"]:
                relationship_id=modifier_composition_relationship_id(item["tag_id"],family_id)
                reserved=effective.get(relationship_id)
                if reserved is not None and reserved.kind!=MODIFIER_VALUE:
                    raise ValueError("A reserved Modifier Value relationship identity is already in use.")
                details={
                    "modifier_dimension_id":family_id,"operation":"reviewed_tag_template",
                    "base_tag_id":item["anchor_id"],"tag_name":normalize_tag(item["display"]),
                    "reviewed_at":item["created_at"],"template_id":item["template_id"],
                    "template_version":str(item["template_version"]),
                    "result_id":item["result_id"],"operation_id":item["operation_id"],
                    "compatibility_projection":"tag.relations.attributes",
                }
                relationship=KnowledgeRelationship(
                    id=relationship_id,kind=MODIFIER_VALUE,endpoints=(
                        RelationshipEndpoint("tag",item["tag_id"],item["display"]),
                        RelationshipEndpoint("tag",value_id,self._state.tags[value_id].display),
                    ),directed=True,scope="any",label=f"{item['display']} modifier composition",
                    reviewed=True,source="reviewed_tag_template",details=tuple(details.items()),
                    version=(reserved.version+1 if reserved is not None else 1),
                    layer="user",disabled=False,
                    extras={"tag_template_creation":copy.deepcopy(provenance)},
                )
                self._state.relationships[relationship_id]=relationship
                effective[relationship_id]=relationship; changed_relationship_ids.append(relationship_id)
        issues=validate_relationships(self._effective_relationships().values())
        if issues:raise ValueError(" ".join(issue.message for issue in issues[:3]))
        if changed_relationship_ids:self._state.relationship_file_exists=True
        self._refresh_tag_semantic_authority()
        self._refresh_reusable_modifier_authority()
        self._add_action({
            "kind":"tag","ids":[item["tag_id"] for item in changed],
            "action":"reviewed_template_batch","created_ids":created,
            "relationship_ids":list(dict.fromkeys(changed_relationship_ids)),
        })
        return tuple(created),tuple(dict.fromkeys(changed_relationship_ids))

    def _tag_view(self,tags: dict[str,TagRecord]) -> TagDatabase:
        view=copy.copy(self.live_tags); view.tags=tags; view._rebuild_lookup(); return view

    def _effective_relationships(
        self, state: _State | None = None,
    ) -> dict[str, KnowledgeRelationship]:
        effective = dict(self.default_relationships.relationships)
        effective.update((state or self._state).relationships)
        return effective

    @staticmethod
    def _specificity_cycle(tag_id: str, active_targets: dict[str, tuple[str, ...]]) -> bool:
        pending=list(active_targets.get(tag_id,()))
        visited=set()
        while pending:
            current=pending.pop()
            if current==tag_id:return True
            if current in visited:continue
            visited.add(current); pending.extend(active_targets.get(current,()))
        return False

    def preview_member_broader_tag_reset(
        self, category_id: str, *, include_descendants: bool = False,
    ) -> MemberBroaderTagResetPlan:
        """Preview explicit member Broader Tags that can return to category inheritance."""
        category_id=str(category_id)
        if category_id not in self._state.categories:
            raise ValueError("Choose an existing category.")
        descendants=self.descendant_ids(category_id)
        category_ids=(category_id,*sorted(
            descendants,key=lambda value:(self.path_label(value).casefold(),value),
        )) if include_descendants else (category_id,)
        selected_categories=set(category_ids)
        effective=self._effective_relationships()
        source_index=tag_semantic_relationship_index(effective.values())
        active_targets={
            source_id:tuple(dict.fromkeys(
                endpoint.id
                for relationship in relationships
                if relationship.reviewed and not relationship.disabled
                for endpoint in relationship.target_endpoints
                if endpoint.kind=="tag"
            ))
            for (kind,source_id),relationships in source_index.items()
            if kind==SPECIFICITY
        }
        for record in self._state.tags.values():
            if record.id not in active_targets and record.parent_id:
                active_targets[record.id]=(str(record.parent_id),)
        rows=[]
        for record in sorted(
            (item for item in self._state.tags.values() if item.category in selected_categories),
            key=lambda item:(self.path_label(item.category).casefold(),item.display.casefold(),item.id),
        ):
            relationships=tuple(source_index.get((SPECIFICITY,record.id),()))
            target_ids=active_targets.get(record.id,())
            represented=bool(relationships)
            if represented:
                user_ids={item.id for item in relationships if item.id in self._state.relationships}
                default_ids={item.id for item in relationships if item.id in self.default_relationships.relationships}
                authority=(
                    "canonical_mixed" if user_ids and default_ids and user_ids!=default_ids else
                    "canonical_user" if user_ids else "canonical_default"
                )
            elif record.parent_id:
                authority="compatibility"
                target_ids=(str(record.parent_id),)
            else:
                authority="none"
            result_base_id,result_source_id,_policy=self.effective_base_tag(record.category)
            result_kind=(
                "anchor" if result_base_id==record.id else
                "inherited" if result_base_id else "none"
            )
            unavailable=bool(
                any(target_id not in self._state.tags for target_id in target_ids)
                or (result_base_id and result_base_id not in self._state.tags)
            )
            if unavailable:status="unavailable"
            elif not target_ids:status="no_override"
            elif len(target_ids)>1:status="ambiguous"
            elif self._specificity_cycle(record.id,active_targets):status="cycle"
            else:status="override"
            rows.append(MemberBroaderTagResetRow(
                tag_id=record.id,category_id=record.category,
                scope="direct" if record.category==category_id else "descendant",
                status=status,authority=authority,target_ids=target_ids,
                relationship_ids=tuple(item.id for item in relationships),
                result_base_id=result_base_id,result_source_id=str(result_source_id or ""),
                result_kind=result_kind,
            ))
        return MemberBroaderTagResetPlan(
            category_id,include_descendants,tuple(category_ids),tuple(rows),
        )

    def reset_member_broader_tag_overrides(
        self, plan: MemberBroaderTagResetPlan,
    ) -> MemberBroaderTagResetPlan:
        """Stage one undoable canonical reset after validating the preview is current."""
        current=self.preview_member_broader_tag_reset(
            plan.category_id,include_descendants=plan.include_descendants,
        )
        if current!=plan:
            raise RuntimeError(
                "Category membership or Broader Tag relationships changed after the preview. "
                "Review the refreshed scope before resetting anything."
            )
        tag_ids=current.reset_tag_ids
        if not tag_ids:
            raise ValueError("No member Broader Tag overrides need resetting in this scope.")
        candidate=copy.copy(self.live_tags)
        candidate.tags=copy.deepcopy(self._state.tags)
        candidate._rebuild_lookup()
        workspace=RelationshipWorkspaceService(
            self.default_relationship_path,self.user_relationship_path,
            tags=candidate,
        )
        workspace.user_store.schema_version=self.user_relationships.schema_version
        workspace.user_store.extras=copy.deepcopy(self.user_relationships.extras)
        workspace.user_store.relationships=dict(self._state.relationships)
        staged_relationships,changed_relationship_ids=stage_tag_semantic_changes(
            candidate,workspace,(
                TagSemanticChange(
                    tag_id=tag_id,kinds=(SPECIFICITY,),
                    source="category_inheritance_reset",
                    reason=(
                        f"Reset from Category Workspace category {plan.category_id}; "
                        f"descendants included: {plan.include_descendants}."
                    ),
                )
                for tag_id in tag_ids
            ),
        )
        self._checkpoint(tags=tag_ids)
        for tag_id in tag_ids:
            self._state.tags[tag_id]=candidate.tags[tag_id]
        self._state.relationships=staged_relationships
        self._state.relationship_file_exists=True
        self._add_action({
            "kind":"tag","action":"reset_broader_tag_overrides",
            "ids":list(tag_ids),"category_ids":list(plan.category_ids),
            "category_id":plan.category_id,
            "include_descendants":plan.include_descendants,
            "relationship_ids":list(changed_relationship_ids),
        })
        return current

    def _publish_live_semantic_authority(self) -> None:
        effective=dict(self.default_relationships.relationships)
        effective.update(self.user_relationships.relationships)
        publish_tag_alias_storage_authority(self.live_tags,effective.values())
        publish_tag_semantic_authority(self.live_tags,effective.values())

    def _publish_live_scoped_constraint_authority(
        self,
        relationships: dict[str, KnowledgeRelationship] | None = None,
    ) -> None:
        effective = (
            relationships
            if relationships is not None else self._effective_relationships()
        )
        publish_scoped_constraint_storage_authority(
            self.live_tags,effective.values(),
        )

    def _clear_represented_scoped_constraint_fields(
        self,
        tags: dict[str, TagRecord],
        relationships: dict[str, KnowledgeRelationship],
    ) -> None:
        tag_view = copy.copy(self.live_tags)
        tag_view.tags = tags
        publish_scoped_constraint_storage_authority(
            tag_view,relationships.values(),
        )
        for index,record in enumerate(tags.values()):
            if (
                self._cooperative_yields
                and index
                and index%self._cooperative_batch_size==0
            ):
                time.sleep(0.001)
            # Only canonical owners can alter these storage projections.
            # Unrelated records retain their exact staged fields.
            if (not tag_view._represented_conflict_targets.get(record.id)):
                continue
            stored = TagRecord.from_dict(tag_view._record_payload(record))
            record.conflicts = stored.conflicts
            record.conflicts_projection_present = (
                stored.conflicts_projection_present
            )
            if "constraint_provenance" in stored.relation_extras:
                record.relation_extras["constraint_provenance"] = copy.deepcopy(
                    stored.relation_extras["constraint_provenance"]
                )
            else:
                record.relation_extras.pop("constraint_provenance", None)

    def _refresh_scoped_constraint_authority(
        self, *, publish_live: bool = False,
    ) -> None:
        effective = self._effective_relationships()
        self._clear_represented_scoped_constraint_fields(
            self._state.tags,effective,
        )
        if publish_live:
            self._publish_live_scoped_constraint_authority(effective)

    def _publish_live_reusable_modifier_authority(
        self,
        relationships: dict[str, KnowledgeRelationship] | None = None,
        *,
        categories: CategoryRegistry | None = None,
    ) -> None:
        effective = (
            relationships
            if relationships is not None else self._effective_relationships()
        )
        publish_reusable_modifier_storage_authority(
            self.live_tags,
            effective.values(),
            categories=categories or self.live_categories,
            foundation=self._modifier_foundation,
        )

    def _staged_modifier_categories(self) -> CategoryRegistry:
        view = copy.copy(self.live_categories)
        view.categories = self._state.categories
        view.disabled_default_ids = self._state.disabled_default_ids
        return view

    def _clear_represented_reusable_modifier_fields(
        self,
        tags: dict[str, TagRecord],
        relationships: dict[str, KnowledgeRelationship],
        *,
        categories: CategoryRegistry | None = None,
    ) -> None:
        tag_view = copy.copy(self.live_tags)
        tag_view.tags = tags
        tag_view._rebuild_lookup()
        publish_reusable_modifier_storage_authority(
            tag_view,
            relationships.values(),
            categories=categories or self._staged_modifier_categories(),
            foundation=self._modifier_foundation,
        )
        for index,record in enumerate(tags.values()):
            if (
                self._cooperative_yields
                and index
                and index%self._cooperative_batch_size==0
            ):
                time.sleep(0.001)
            # Only canonical owners can alter these storage projections.
            # Unrelated records retain their exact staged fields.
            if (record.id not in tag_view._modifier_families_by_source
                and record.id not in tag_view._represented_allowed_dimension_sources):
                continue
            stored = TagRecord.from_dict(tag_view._record_payload(record))
            record.attributes = stored.attributes
            record.attributes_projection_present = (
                stored.attributes_projection_present
            )
            record.allowed_modifier_groups = stored.allowed_modifier_groups
            for key in (
                "modifier_value_provenance",
                "allowed_modifier_groups_provenance",
            ):
                if key in stored.relation_extras:
                    record.relation_extras[key] = copy.deepcopy(
                        stored.relation_extras[key]
                    )
                else:
                    record.relation_extras.pop(key, None)

    def _refresh_reusable_modifier_authority(
        self, *, publish_live: bool = False,
    ) -> None:
        effective = self._effective_relationships()
        self._clear_represented_reusable_modifier_fields(
            self._state.tags, effective,
        )
        if publish_live:
            self._publish_live_reusable_modifier_authority(effective)

    def _tag_compatibility_payload(
        self,
        record: TagRecord,
        relationships: dict[str, KnowledgeRelationship],
        *,
        tags: dict[str, TagRecord],
        semantic_index: dict[
            tuple[str, str], tuple[KnowledgeRelationship, ...]
        ] | None = None,
        alias_index: TagAliasCompatibilityIndex | None = None,
        modifier_index: ReusableModifierCompatibilityIndex | None = None,
        constraint_index: ScopedConstraintCompatibilityIndex | None = None,
    ) -> dict[str, Any]:
        tag_view = copy.copy(self.live_tags)
        tag_view.tags = tags
        tag_view._rebuild_lookup()
        return tag_relationship_compatibility_payload(
            record,
            relationships.values(),
            tags=tag_view,
            categories=self._staged_modifier_categories(),
            foundation=self._modifier_foundation,
            alias_index=alias_index,
            semantic_index=semantic_index,
            modifier_index=modifier_index,
            constraint_index=constraint_index,
        )

    def _reusable_modifier_compatibility_index(
        self,
        relationships: dict[str, KnowledgeRelationship],
        *,
        tags: dict[str, TagRecord],
    ) -> ReusableModifierCompatibilityIndex:
        tag_view = copy.copy(self.live_tags)
        tag_view.tags = tags
        tag_view._rebuild_lookup()
        return reusable_modifier_compatibility_index(
            tag_view,
            relationships.values(),
            categories=self._staged_modifier_categories(),
            foundation=self._modifier_foundation,
        )

    def _clear_represented_semantic_fields(
        self,tags: dict[str, TagRecord],
        source_index: dict[tuple[str,str], tuple[Any, ...]],
    ) -> None:
        for index,record in enumerate(tags.values()):
            if (
                self._cooperative_yields
                and index
                and index%self._cooperative_batch_size==0
            ):
                time.sleep(0.001)
            if source_index.get((SPECIFICITY, record.id)):
                record.parent_id = None
                record.parent_projection_present = False
            if source_index.get((ENTAILMENT, record.id)):
                record.implies = []
                record.implies_projection_present = False

    def _refresh_tag_semantic_authority(
        self, *, publish_live: bool = False,
    ) -> None:
        effective = self._effective_relationships()
        self._clear_represented_semantic_fields(
            self._state.tags,
            tag_semantic_relationship_index(effective.values()),
        )
        if publish_live:
            publish_tag_alias_storage_authority(
                self.live_tags,effective.values(),
            )
            publish_tag_semantic_authority(
                self.live_tags, effective.values(),
            )

    @staticmethod
    def _relationship_endpoint_key(relationship: KnowledgeRelationship):
        endpoints = tuple((item.kind, item.id) for item in relationship.endpoints)
        if not relationship.directed:
            endpoints = tuple(sorted(endpoints))
        return (
            relationship.kind,
            relationship.directed,
            relationship.scope,
            endpoints,
        )

    @classmethod
    def _relationship_semantic_key(cls, relationship: KnowledgeRelationship):
        details = {
            key: value
            for key, value in relationship.details
            if key not in {
                "canonical_relationship_id",
                "compatibility_projection",
                "migrated_from",
            }
        }
        extras = {
            key: value
            for key, value in relationship.extras.items()
            if key not in {"identity_merge", "legacy_compatibility", "tag_removal"}
        }
        return (
            cls._relationship_endpoint_key(relationship),
            json.dumps(details, sort_keys=True, ensure_ascii=False, default=str),
            json.dumps(extras, sort_keys=True, ensure_ascii=False, default=str),
        )

    @staticmethod
    def _removal_audit_extras(
        relationship: KnowledgeRelationship,
        *,
        removed_tag_ids: tuple[str, ...],
        previous_endpoints: tuple[RelationshipEndpoint, ...],
        action: str,
        superseded_by: str = "",
    ) -> dict[str, Any]:
        extras = copy.deepcopy(relationship.extras)
        previous = extras.get("tag_removal")
        audit: dict[str, Any] = {
            "removed_at": _now(),
            "removed_tag_ids": list(removed_tag_ids),
            "action": action,
            "previous_endpoints": [item.to_dict() for item in previous_endpoints],
        }
        if superseded_by:
            audit["superseded_by_relationship_id"] = superseded_by
        if previous is not None:
            audit["previous_tag_removal"] = copy.deepcopy(previous)
        extras["tag_removal"] = audit
        return extras

    def _remove_relationship_endpoints(
        self,
        relationship: KnowledgeRelationship,
        *,
        removed_tag_ids: tuple[str, ...],
    ) -> tuple[KnowledgeRelationship, bool]:
        removed = set(removed_tag_ids)
        selected_indexes = tuple(
            index
            for index, endpoint in enumerate(relationship.endpoints)
            if endpoint.kind == "tag" and endpoint.id in removed
        )
        if not selected_indexes:
            return relationship, False

        previous_endpoints = relationship.endpoints
        details = dict(relationship.details)
        remaining = tuple(
            endpoint
            for index, endpoint in enumerate(previous_endpoints)
            if index not in selected_indexes
        )
        source_removed = relationship.directed and 0 in selected_indexes
        retired = relationship.disabled or source_removed or len(remaining) < 2
        if retired:
            endpoints = tuple(
                replace(endpoint, kind="removed_tag")
                if index in selected_indexes
                else endpoint
                for index, endpoint in enumerate(previous_endpoints)
            )
            action = (
                "retired_removed_source"
                if source_removed
                else "recorded_removed_endpoint"
                if relationship.disabled
                else "retired_collapsed_relationship"
            )
        else:
            endpoints = remaining
            action = "removed_tag_endpoints"
        if relationship.kind == CATEGORY_BASE:
            source = relationship.source_endpoint
            category = self._state.categories.get(
                source.id if source is not None and source.kind == "category" else ""
            )
            details["policy"] = (
                "inherit" if category is not None and category.parent_id else "none"
            )
        return replace(
            relationship,
            endpoints=endpoints,
            details=tuple(
                (str(key), str(value)) for key, value in details.items()
            ),
            version=relationship.version + 1,
            layer="user",
            source="tag_removal",
            disabled=relationship.disabled or retired,
            extras=self._removal_audit_extras(
                relationship,
                removed_tag_ids=removed_tag_ids,
                previous_endpoints=previous_endpoints,
                action=action,
            ),
        ), True

    def _candidate_relationship_removal(
        self, removed_tag_ids: tuple[str, ...],
    ) -> tuple[
        dict[str, KnowledgeRelationship],
        dict[str, KnowledgeRelationship],
        set[str],
    ]:
        original_effective = self._effective_relationships()
        candidate_user = dict(self._state.relationships)
        candidate_effective = dict(original_effective)
        changed_ids: set[str] = set()
        for relationship_id, relationship in original_effective.items():
            updated, changed = self._remove_relationship_endpoints(
                relationship, removed_tag_ids=removed_tag_ids,
            )
            if not changed:
                continue
            candidate_user[relationship_id] = updated
            candidate_effective[relationship_id] = updated
            changed_ids.add(relationship_id)

        active_groups: dict[tuple[Any, ...], list[KnowledgeRelationship]] = {}
        for relationship in candidate_effective.values():
            if relationship.disabled or not relationship.reviewed:
                continue
            active_groups.setdefault(
                self._relationship_endpoint_key(relationship), []
            ).append(relationship)
        for relationships in active_groups.values():
            if len(relationships) <= 1 or not changed_ids.intersection(
                item.id for item in relationships
            ):
                continue
            if len({self._relationship_semantic_key(item) for item in relationships}) > 1:
                ids = ", ".join(sorted(item.id for item in relationships))
                raise ValueError(
                    "Removing this Tag would make canonical relationships "
                    f"{ids} overlap while preserving different reviewed details or "
                    "conditions. Review those records in Manage Relationships first."
                )
            winner = sorted(
                relationships,
                key=lambda item: (
                    not owns_inline_constraint_projection(item),
                    item.layer != "user",
                    item.id in changed_ids,
                    item.id,
                ),
            )[0]
            for relationship in relationships:
                if relationship.id == winner.id:
                    continue
                original = original_effective[relationship.id]
                retired = replace(
                    relationship,
                    version=original.version + 1,
                    layer="user",
                    source="tag_removal",
                    disabled=True,
                    extras=self._removal_audit_extras(
                        relationship,
                        removed_tag_ids=removed_tag_ids,
                        previous_endpoints=original.endpoints,
                        action="retired_duplicate_relationship",
                        superseded_by=winner.id,
                    ),
                )
                candidate_user[retired.id] = retired
                candidate_effective[retired.id] = retired
                changed_ids.add(retired.id)

        issues = validate_relationships(candidate_effective.values())
        if issues:
            raise ValueError(" ".join(issue.message for issue in issues[:3]))
        return candidate_user, candidate_effective, changed_ids

    def _character_tag_references(self, selected_ids: set[str]) -> tuple[str, ...]:
        if self.characters is None:
            return ()
        references: list[str] = []
        tag_ids_by_name: dict[str, str] = {}
        for tag in self._state.tags.values():
            for value in self.live_tags.identity_names(tag):
                normalized = normalize_tag(value)
                if normalized:
                    tag_ids_by_name.setdefault(normalized, tag.id)
        for kind, record_kind, records in (
            ("Character Source", "series", self.characters.series.values()),
            ("Character Identity", "character", self.characters.characters.values()),
            ("Character Outfit", "outfit", self.characters.outfits.values()),
        ):
            for record in records:
                link = self.characters.effective_knowledge_tag(record_kind, record.id)
                direct = str(link.get("tag_id") or "")
                excluded = {
                    str(value)
                    for value in link.get("excluded_tag_ids", ())
                    if str(value)
                }
                if direct in selected_ids or selected_ids.intersection(excluded):
                    references.append(f"{kind} {record.name} ({record.id})")
        for character in self.characters.characters.values():
            for association in character.tag_associations:
                if str(association.get("status") or "observed") != "reviewed":
                    continue
                tag_name = str(association.get("tag") or "")
                normalized = normalize_tag(tag_name)
                linked_id = tag_ids_by_name.get(normalized, "")
                if linked_id in selected_ids:
                    references.append(
                        f"Character Identity {character.name} ({character.id}, reviewed association)"
                    )
        return tuple(dict.fromkeys(references))

    @staticmethod
    def _project_removed_constraints(
        candidate_tags: dict[str, TagRecord],
        *,
        original_relationships: dict[str, KnowledgeRelationship],
        candidate_relationships: dict[str, KnowledgeRelationship],
        affected_relationship_ids: set[str],
    ) -> None:
        removed_pairs: set[tuple[str, str]] = set()
        for relationship_id in affected_relationship_ids:
            relationship = original_relationships.get(relationship_id)
            if relationship is None or not owns_inline_constraint_projection(relationship):
                continue
            removed_pairs.update(constraint_pairs(relationship))
        for first, second in removed_pairs:
            if first in candidate_tags:
                candidate_tags[first].conflicts = [
                    value for value in candidate_tags[first].conflicts if value != second
                ]
            if second in candidate_tags:
                candidate_tags[second].conflicts = [
                    value for value in candidate_tags[second].conflicts if value != first
                ]
        for relationship in candidate_relationships.values():
            pairs = constraint_pairs(relationship)
            if (
                relationship.disabled
                or not relationship.reviewed
                or not owns_inline_constraint_projection(relationship)
                or (
                    relationship.id not in affected_relationship_ids
                    and not pairs.intersection(removed_pairs)
                )
            ):
                continue
            for first, second in pairs:
                if first not in candidate_tags or second not in candidate_tags:
                    raise ValueError(
                        "A remaining Contrary Values relationship references an unavailable SDXL Tag."
                    )

    def preview_tag_removal(self, tag_ids) -> TagRemovalImpact:
        selected=tuple(dict.fromkeys(str(value) for value in tag_ids))
        if not selected:
            raise ValueError("Choose at least one SDXL Tag to remove from System Knowledge.")
        missing=[value for value in selected if value not in self._state.tags]
        if missing:
            raise ValueError("One or more selected SDXL Tags are no longer available.")
        selected_set=set(selected)
        character_references = self._character_tag_references(selected_set)
        if character_references:
            examples = ", ".join(character_references[:4])
            remaining = len(character_references) - 4
            if remaining:
                examples += f" and {remaining:,} more"
            raise ValueError(
                "Remove or review the Character Knowledge Tag links first: "
                f"{examples}. No System Knowledge or image data was changed."
            )
        _candidate_user, candidate_effective, changed_ids = (
            self._candidate_relationship_removal(selected)
        )
        original_effective = self._effective_relationships()
        retired_ids = tuple(sorted(
            relationship_id
            for relationship_id in changed_ids
            if candidate_effective[relationship_id].disabled
            and not original_effective[relationship_id].disabled
        ))
        relationship_ids=[]; parent_links=implication_links=conflict_links=0
        effective_relationships=self._effective_relationships()
        semantic_index=tag_semantic_relationship_index(
            effective_relationships.values(),
        )
        for record in self._state.tags.values():
            if record.id in selected_set:
                continue
            semantic=effective_tag_semantic_links_from_index(
                record,semantic_index,
            )
            parent_links+=int(semantic.parent_id in selected_set)
            implication_links+=sum(
                value in selected_set for value in semantic.implies
            )
            conflict_links+=sum(value in selected_set for value in record.conflicts)
            effective_link=bool(
                semantic.parent_id in selected_set
                or selected_set.intersection(semantic.implies)
                or selected_set.intersection(record.conflicts)
            )
            compatibility_cleanup=bool(
                record.parent_id in selected_set
                or selected_set.intersection(record.implies)
                or selected_set.intersection(record.attributes)
            )
            if effective_link or compatibility_cleanup:
                relationship_ids.append(record.id)
        canonical_category_base_refs={
            relationship.source_endpoint.id
            for relationship in effective_relationships.values()
            if (
                relationship.kind==CATEGORY_BASE
                and relationship.reviewed
                and not relationship.disabled
                and relationship.source_endpoint is not None
                and relationship.source_endpoint.kind=="category"
                and any(
                    endpoint.kind=="tag" and endpoint.id in selected_set
                    for endpoint in relationship.target_endpoints
                )
            )
        }
        category_base_refs=tuple(sorted({
            *canonical_category_base_refs,
            *(
                record.id for record in self._state.categories.values()
                if record.base_tag_id in selected_set
            ),
        }))
        return TagRemovalImpact(
            selected,tuple(sorted(relationship_ids)),parent_links,implication_links,
            conflict_links,category_base_refs,tuple(sorted(changed_ids)),retired_ids,
        )

    def _shipped_tag_for_recovery(self, stable_id: str) -> TagRecord | None:
        parent=self.live_tags.path.parent
        if parent.name.casefold()!="user":
            return None
        from .default_sync import shipped_tag_record
        return shipped_tag_record(parent.parent/"defaults",stable_id)

    def _tag_removal_recovery_kind(self, record: TagRecord) -> str:
        shipped=self._shipped_tag_for_recovery(record.id)
        if shipped is None:
            return "archived_user"
        return (
            "current_default"
            if _tag_default_comparable(record)==_tag_default_comparable(shipped)
            else "archived_user"
        )

    @staticmethod
    def _category_archive_payload(record: CategoryRecord) -> dict[str, Any]:
        return {"source":record.source,"record":record.to_dict()}

    def remove_tags(
        self, tag_ids, *, source: str="category_workspace",
        reason: str="Removed from System Knowledge",
    ) -> TagRemovalImpact:
        impact=self.preview_tag_removal(tag_ids); selected=set(impact.remove_ids)
        original_relationships = self._effective_relationships()
        candidate_user, candidate_effective, changed_ids = (
            self._candidate_relationship_removal(impact.remove_ids)
        )
        removed_records={
            record_id:copy.deepcopy(self._state.tags[record_id])
            for record_id in impact.remove_ids
        }
        related_before={
            record_id:copy.deepcopy(self._state.tags[record_id])
            for record_id in impact.relationship_record_ids
        }
        categories_before={
            record_id:copy.deepcopy(self._state.categories[record_id])
            for record_id in impact.category_base_refs
        }
        relationships_before={
            record_id:copy.deepcopy(self._state.relationships.get(record_id))
            for record_id in changed_ids
        }
        relationship_file_existed=self._state.relationship_file_exists
        recovery_kinds={
            record_id:self._tag_removal_recovery_kind(record)
            for record_id,record in removed_records.items()
        }
        affected=(*impact.remove_ids,*impact.relationship_record_ids)
        self._checkpoint(categories=impact.category_base_refs,tags=affected)
        for record_id in impact.relationship_record_ids:
            record=self._state.tags[record_id]
            if record.parent_id in selected:
                record.parent_id=None
            record.attributes=[
                value for value in record.attributes if value not in selected
            ]
            record.implies=[value for value in record.implies if value not in selected]
            record.conflicts=[value for value in record.conflicts if value not in selected]
            record.provenance.append({
                "source":"category_workspace","reviewed_at":_now(),
                "action":"remove_dangling_tag_links","removed_tag_ids":sorted(selected),
            })
        for record_id in impact.remove_ids:
            self._state.tags.pop(record_id,None)
        for category_id in impact.category_base_refs:
            category=self._state.categories[category_id]; category.base_tag_id=""
            category.base_tag_policy="inherit" if category.parent_id else "none"; category.source="user"
        self._project_removed_constraints(
            self._state.tags,
            original_relationships=original_relationships,
            candidate_relationships=candidate_effective,
            affected_relationship_ids=changed_ids,
        )
        self._state.relationships = candidate_user
        if changed_ids:
            self._state.relationship_file_exists = True
        self._refresh_category_base_authority()
        self._refresh_tag_semantic_authority()
        self._refresh_reusable_modifier_authority()
        self._refresh_scoped_constraint_authority()
        self._state.removed_tag_ids.update(impact.remove_ids)
        compatibility_tags_before = dict(self._state.tags)
        compatibility_tags_before.update(removed_records)
        compatibility_tags_before.update(related_before)
        semantics_before = tag_semantic_relationship_index(
            original_relationships.values()
        )
        semantics_after = tag_semantic_relationship_index(
            candidate_effective.values()
        )
        aliases_before = tag_alias_compatibility_index(
            original_relationships.values()
        )
        aliases_after = tag_alias_compatibility_index(
            candidate_effective.values()
        )
        modifiers_before = self._reusable_modifier_compatibility_index(
            original_relationships, tags=compatibility_tags_before,
        )
        modifiers_after = self._reusable_modifier_compatibility_index(
            candidate_effective, tags=self._state.tags,
        )
        constraints_before = scoped_constraint_compatibility_index(
            original_relationships.values(),
        )
        constraints_after = scoped_constraint_compatibility_index(
            candidate_effective.values(),
        )
        archive_id=f"tag_removal_{uuid4().hex}"
        archive=RemovedTagArchive(
            id=archive_id,removed_at=_now(),source=str(source),reason=str(reason),
            removed_tags=[
                self._tag_compatibility_payload(
                    removed_records[record_id], original_relationships,
                    tags=compatibility_tags_before,
                    alias_index=aliases_before,
                    semantic_index=semantics_before,
                    modifier_index=modifiers_before,
                    constraint_index=constraints_before,
                )
                for record_id in impact.remove_ids
            ],
            recovery_kinds=recovery_kinds,
            related_tags_before={
                record_id:self._tag_compatibility_payload(
                    record, original_relationships,
                    tags=compatibility_tags_before,
                    alias_index=aliases_before,
                    semantic_index=semantics_before,
                    modifier_index=modifiers_before,
                    constraint_index=constraints_before,
                )
                for record_id,record in related_before.items()
            },
            related_tags_after={
                record_id:_payload_fingerprint(
                    self._tag_compatibility_payload(
                        self._state.tags[record_id], candidate_effective,
                        tags=self._state.tags,
                        alias_index=aliases_after,
                        semantic_index=semantics_after,
                        modifier_index=modifiers_after,
                        constraint_index=constraints_after,
                    )
                    if record_id in self._state.tags else None
                )
                for record_id in related_before
            },
            categories_before={
                record_id:self._category_archive_payload(record)
                for record_id,record in categories_before.items()
            },
            categories_after={
                record_id:_payload_fingerprint(
                    self._category_archive_payload(self._state.categories[record_id])
                    if record_id in self._state.categories else None
                )
                for record_id in categories_before
            },
            relationships_before={
                record_id:(record.to_dict() if record is not None else None)
                for record_id,record in relationships_before.items()
            },
            relationships_after={
                record_id:_payload_fingerprint(
                    self._state.relationships[record_id].to_dict()
                    if record_id in self._state.relationships else None
                )
                for record_id in relationships_before
            },
            relationship_file_existed=relationship_file_existed,
        )
        self._state.removed_tag_archives[archive.id]=archive
        self._add_action({
            "kind":"tag","ids":list(impact.remove_ids),
            "action":"remove_from_system_knowledge","archive_id":archive.id,
        })
        return impact

    def _active_archives_for_tags(
        self, tag_ids: tuple[str, ...],
    ) -> tuple[RemovedTagArchive, ...]:
        requested=set(tag_ids)
        matches=[]
        for archive in sorted(
            self._state.removed_tag_archives.values(),
            key=lambda item:(item.removed_at,item.id),reverse=True,
        ):
            if archive.active and requested.intersection(archive.tag_ids):
                matches.append(archive)
                requested.difference_update(archive.tag_ids)
        if requested:
            raise ValueError(
                "Complete archived recovery details are unavailable for removed "
                "SDXL Tag identities: "+", ".join(sorted(requested)[:4])+"."
            )
        return tuple(matches)

    def _restoration_records(
        self, archives: tuple[RemovedTagArchive, ...],
    ) -> dict[str, TagRecord]:
        records={}
        for archive in archives:
            for stable_id in archive.tag_ids:
                archived=archive.tag_record(stable_id)
                if archived is None:
                    raise ValueError(
                        f'Removed SDXL Tag "{stable_id}" has no complete archived record.'
                    )
                if archive.recovery_kinds.get(stable_id)=="current_default":
                    current_default=self._shipped_tag_for_recovery(stable_id)
                    if current_default is None:
                        raise ValueError(
                            f'The current shipped default for removed SDXL Tag "{stable_id}" '
                            "is unavailable. Restore the matching Knowledge Pack or System "
                            "Knowledge backup before continuing."
                        )
                    records[stable_id]=current_default
                else:
                    records[stable_id]=archived
        return records

    def preview_tag_restoration(self, tag_ids) -> TagRestorationImpact:
        requested=tuple(dict.fromkeys(str(value) for value in tag_ids if str(value)))
        if not requested:
            raise ValueError("Choose at least one removed SDXL Tag to restore.")
        archives=self._active_archives_for_tags(requested)
        restored_ids=tuple(dict.fromkeys(
            stable_id for archive in archives for stable_id in archive.tag_ids
        ))
        restored_set=set(restored_ids)
        unavailable=[
            stable_id for stable_id in restored_ids
            if stable_id not in self._state.removed_tag_ids
            or stable_id in self._state.tags
            or stable_id in self._state.merged_tag_ids
        ]
        if unavailable:
            raise ValueError(
                "The removed lifecycle state changed for: "+
                ", ".join(sorted(unavailable)[:4])+". Refresh and review it again."
            )
        restored=self._restoration_records(archives)
        live_names={
            normalize_tag(name):record.id
            for record in self._state.tags.values()
            for name in self.live_tags.identity_names(record)
            if normalize_tag(name)
        }
        restored_names={}
        for record in restored.values():
            for name in (record.display,*record.aliases):
                key=normalize_tag(name)
                if not key:continue
                owner=live_names.get(key) or restored_names.get(key)
                if owner and owner!=record.id:
                    raise ValueError(
                        f'Cannot restore "{record.display}" because "{name}" now belongs '
                        f'to live stable identity "{owner}".'
                    )
                restored_names[key]=record.id

        related_ids=set(); category_ids=set(); relationship_ids=set()
        current_effective_relationships = dict(
            self.default_relationships.relationships,
        )
        current_effective_relationships.update(self._state.relationships)
        current_semantics = tag_semantic_relationship_index(
            current_effective_relationships.values()
        )
        current_aliases = tag_alias_compatibility_index(
            current_effective_relationships.values()
        )
        current_modifiers = self._reusable_modifier_compatibility_index(
            current_effective_relationships, tags=self._state.tags,
        )
        for archive in archives:
            for record_id,expected in archive.related_tags_after.items():
                current=self._state.tags.get(record_id)
                current_payload=(
                    self._tag_compatibility_payload(
                        current, current_effective_relationships,
                        tags=self._state.tags,
                        alias_index=current_aliases,
                        semantic_index=current_semantics,
                        modifier_index=current_modifiers,
                    )
                    if current else None
                )
                if _payload_fingerprint(current_payload)!=expected:
                    raise ValueError(
                        f'Related SDXL Tag "{record_id}" changed after removal. '
                        "Review its current relationships before restoring this archive."
                    )
                related_ids.add(record_id)
            for record_id,expected in archive.categories_after.items():
                current=self._state.categories.get(record_id)
                payload=self._category_archive_payload(current) if current else None
                if _payload_fingerprint(payload)!=expected:
                    raise ValueError(
                        f'Category "{record_id}" changed after removal. Review its current '
                        "Base Tag before restoring this archive."
                    )
                category_ids.add(record_id)
            for record_id,expected in archive.relationships_after.items():
                current=self._state.relationships.get(record_id)
                if _payload_fingerprint(current.to_dict() if current else None)!=expected:
                    raise ValueError(
                        f'Canonical relationship "{record_id}" changed after removal. '
                        "Review it in Manage Relationships before restoring this archive."
                    )
                relationship_ids.add(record_id)

        candidate_tags=copy.deepcopy(self._state.tags)
        candidate_tags.update(copy.deepcopy(restored))
        candidate_categories=copy.deepcopy(self._state.categories)
        candidate_user_relationships=dict(self._state.relationships)
        for archive in archives:
            for record_id,payload in archive.related_tags_before.items():
                candidate_tags[record_id]=TagRecord.from_dict(payload)
            for record_id,payload in archive.categories_before.items():
                record_payload=payload.get("record") if isinstance(payload,dict) else None
                if not isinstance(record_payload,dict):
                    raise ValueError(
                        f'Archived Category "{record_id}" is incomplete and cannot be restored.'
                    )
                candidate_categories[record_id]=CategoryRecord.from_dict(
                    record_payload,source=str(payload.get("source") or "user"),
                )
            for record_id,payload in archive.relationships_before.items():
                if payload is None:
                    candidate_user_relationships.pop(record_id,None)
                else:
                    candidate_user_relationships[record_id]=KnowledgeRelationship.from_dict(payload)

        available=set(candidate_tags)
        for record in restored.values():
            if record.category not in candidate_categories:
                raise ValueError(
                    f'Category "{record.category}" for removed SDXL Tag '
                    f'"{record.display}" is unavailable.'
                )
            missing={
                value for value in (record.parent_id,*record.implies,*record.conflicts)
                if value and value not in available
            }
            if missing:
                raise ValueError(
                    f'Removed SDXL Tag "{record.display}" still depends on unavailable '
                    "stable identities: "+", ".join(sorted(missing)[:4])+"."
                )
        candidate_effective=dict(self.default_relationships.relationships)
        candidate_effective.update(candidate_user_relationships)
        issues=validate_relationships(candidate_effective.values())
        if issues:
            raise ValueError(
                "Restoring the archived relationship state is invalid: "+
                " ".join(issue.message for issue in issues[:3])
            )
        for relationship in candidate_effective.values():
            missing={
                endpoint.id for endpoint in relationship.endpoints
                if endpoint.kind=="tag" and endpoint.id not in available
            }
            if missing:
                raise ValueError(
                    f'Canonical relationship "{relationship.id}" would still reference '
                    "unavailable SDXL Tags: "+", ".join(sorted(missing)[:4])+"."
                )
        return TagRestorationImpact(
            requested,restored_ids,tuple(archive.id for archive in archives),
            tuple(sorted(related_ids)),tuple(sorted(category_ids)),
            tuple(sorted(relationship_ids)),
        )

    def restore_tags(
        self, tag_ids, *, source: str="removed_vocabulary_review",
    ) -> TagRestorationImpact:
        impact=self.preview_tag_restoration(tag_ids)
        archives=tuple(
            self._state.removed_tag_archives[archive_id]
            for archive_id in impact.archive_ids
        )
        restored=self._restoration_records(archives)
        self._checkpoint(
            categories=impact.category_ids,
            tags=impact.related_tag_ids,
        )
        for archive_id in impact.archive_ids:
            self._state.removed_tag_archives[archive_id]=copy.deepcopy(
                self._state.removed_tag_archives[archive_id]
            )
        archives=tuple(
            self._state.removed_tag_archives[archive_id]
            for archive_id in impact.archive_ids
        )
        for archive in archives:
            for record_id,payload in archive.related_tags_before.items():
                self._state.tags[record_id]=TagRecord.from_dict(payload)
            for record_id,payload in archive.categories_before.items():
                self._state.categories[record_id]=CategoryRecord.from_dict(
                    payload["record"],source=str(payload.get("source") or "user"),
                )
            for record_id,payload in archive.relationships_before.items():
                if payload is None:
                    self._state.relationships.pop(record_id,None)
                else:
                    self._state.relationships[record_id]=KnowledgeRelationship.from_dict(payload)
            archive.restored_at=_now(); archive.restored_by=str(source)
        for record_id,record in restored.items():
            restored_record=copy.deepcopy(record)
            archive=next(item for item in archives if record_id in item.tag_ids)
            if archive.recovery_kinds.get(record_id)!="current_default":
                restored_record.provenance.append({
                    "source":str(source),"reviewed_at":_now(),
                    "action":"restore_removed_system_knowledge",
                    "removal_archive_id":archive.id,
                    "removed_at":archive.removed_at,
                })
            self._state.tags[record_id]=restored_record
            self._state.removed_tag_ids.discard(record_id)
        self._state.relationship_file_exists=(
            bool(self._state.relationships)
            or any(archive.relationship_file_existed for archive in archives)
        )
        self._refresh_category_base_authority()
        self._refresh_tag_semantic_authority()
        self._refresh_reusable_modifier_authority()
        self._refresh_scoped_constraint_authority()
        self._add_action({
            "kind":"tag","ids":list(impact.restored_tag_ids),
            "action":"restore_removed_system_knowledge",
            "archive_ids":list(impact.archive_ids),
        })
        return impact

    def preview_category_merge(self, keep_id: str, remove_ids) -> CategoryMergeImpact:
        if keep_id not in self._state.categories:
            raise ValueError("Choose a category to keep.")
        removed=tuple(value for value in dict.fromkeys(str(item) for item in remove_ids) if value!=keep_id)
        if not removed or any(value not in self._state.categories for value in removed):
            raise ValueError("Choose one or more existing categories to merge.")
        protected=[self._state.categories[value].name for value in removed if self.is_protected_category(value)]
        if protected:
            raise ValueError(
                f"{protected[0]} has a protected system role and cannot be merged away."
            )
        remove_set=set(removed)
        if keep_id in {descendant for value in remove_set for descendant in self.descendant_ids(value)}:
            raise ValueError("A category cannot be merged into one of its descendants.")
        children=tuple(sorted(record.id for record in self._state.categories.values() if record.parent_id in remove_set and record.id not in remove_set))
        tags=tuple(sorted(record.id for record in self._state.tags.values() if record.category in remove_set))
        patterns=tuple(sorted(record.id for record in self._state.patterns.values() if record.category in remove_set))
        return CategoryMergeImpact(keep_id,removed,children,tags,patterns)

    def merge_categories(self, keep_id: str, remove_ids) -> CategoryMergeImpact:
        impact=self.preview_category_merge(keep_id,remove_ids); remove_set=set(impact.remove_ids)
        target=self._state.categories[keep_id]
        sibling_names={
            " ".join(record.name.casefold().split()) for record in self._state.categories.values()
            if record.parent_id==keep_id and record.id not in remove_set
        }
        for child_id in impact.child_categories:
            child=self._state.categories[child_id]
            normalized_name=" ".join(child.name.casefold().split())
            if normalized_name in sibling_names:
                raise ValueError(f"Merge would create a duplicate child category named {child.name}.")
            sibling_names.add(normalized_name)
        inherited_tag_ids={*self._inherited_tag_ids(),*impact.tag_ids}
        category_ids={keep_id,*impact.child_categories}
        self._checkpoint(categories=category_ids,tags=inherited_tag_ids,patterns=impact.pattern_ids)
        target=self._state.categories[keep_id]
        for child_id in impact.child_categories:
            child=self._state.categories[child_id]; child.parent_id=keep_id; child.source="user"
        for tag_id in impact.tag_ids:
            record=self._state.tags[tag_id]; previous=record.category; record.category=keep_id
            if not record.ordering.reviewed:
                group,priority,_=self._category_effective(keep_id); record.ordering.group=group; record.ordering.priority=priority
            record.provenance.append({"source":"category_workspace","reviewed_at":_now(),"action":"merge_category","previous_category":previous,"category":keep_id})
        for pattern_id in impact.pattern_ids:
            record=self._state.patterns[pattern_id]; record.category=keep_id; record.source="user"
        relationship_ids=[]
        for category_id in impact.remove_ids:
            removed_record=copy.deepcopy(self._state.categories[category_id])
            direct_base=self.direct_category_base(category_id)
            previous_base=str(getattr(direct_base,"base_tag_id","") or "")
            relationship_ids.extend(self._stage_category_base_for_record(
                removed_record,
                policy="none",
                base_tag_id="",
                previous_base_tag_id=previous_base,
                reason=f"Category merged into {keep_id}.",
            ))
            self._state.categories.pop(category_id,None)
            if category_id in self._default_categories:self._state.disabled_default_ids.add(category_id)
        target.source="user" if target.id not in self._default_categories or target.source=="user" else target.source
        self._add_action({
            "kind":"category","action":"merge","keep_id":keep_id,
            "remove_ids":list(impact.remove_ids),
            "relationship_ids":list(dict.fromkeys(relationship_ids)),
        })
        self._refresh_inherited_tags()
        return impact

    def preview_category_deletion(self, category_id: str, *, replacement_id: str | None = None) -> CategoryDeletionImpact:
        record=self._state.categories.get(category_id)
        if record is None:
            raise ValueError("The selected category is no longer available.")
        if self.is_protected_category(category_id):
            raise ValueError(
                f"{record.name} has a protected system role and cannot be deleted."
            )
        if category_id=="uncategorized":
            raise ValueError("Uncategorized cannot be deleted.")
        if record.parent_id is None:
            if not replacement_id:
                raise ValueError("Choose a replacement category before deleting a top-level category.")
            destination_id=str(replacement_id)
        else:
            destination_id=record.parent_id
            if replacement_id is not None and str(replacement_id)!=destination_id:
                raise ValueError("A nested category promotes its direct members to its semantic parent.")
        if destination_id not in self._state.categories:
            raise ValueError("Choose an existing replacement category.")
        if destination_id==category_id or destination_id in self.descendant_ids(category_id):
            raise ValueError("A category cannot be deleted into itself or one of its descendants.")
        children=tuple(sorted(item.id for item in self._state.categories.values() if item.parent_id==category_id))
        destination_names={
            " ".join(item.name.casefold().split()):item.name for item in self._state.categories.values()
            if item.parent_id==destination_id and item.id!=category_id
        }
        arriving=set()
        for child_id in children:
            child=self._state.categories[child_id]; normalized=" ".join(child.name.casefold().split())
            if normalized in destination_names or normalized in arriving:
                raise ValueError(f"Deleting this category would create a duplicate child category named {child.name}.")
            arriving.add(normalized)
        tags=tuple(sorted(item.id for item in self._state.tags.values() if item.category==category_id))
        patterns=tuple(sorted(item.id for item in self._state.patterns.values() if item.category==category_id))
        return CategoryDeletionImpact(category_id,destination_id,children,tags,patterns,category_id in self._default_categories)

    def delete_category(self, category_id: str, *, replacement_id: str | None = None) -> CategoryDeletionImpact:
        impact=self.preview_category_deletion(category_id,replacement_id=replacement_id)
        inherited_tag_ids={*self._inherited_tag_ids(),*impact.tag_ids}
        self._checkpoint(categories=impact.child_categories,tags=inherited_tag_ids,patterns=impact.pattern_ids)
        for child_id in impact.child_categories:
            child=self._state.categories[child_id]; child.parent_id=impact.destination_id; child.source="user"
            if not child.ordering_reviewed:
                child.ordering_group=""; child.ordering_priority=None
        for tag_id in impact.tag_ids:
            tag=self._state.tags[tag_id]; previous=tag.category; tag.category=impact.destination_id
            tag.provenance.append({
                "source":"category_workspace","reviewed_at":_now(),"action":"delete_category",
                "previous_category":previous,"category":impact.destination_id,
            })
        for pattern_id in impact.pattern_ids:
            pattern=self._state.patterns[pattern_id]; pattern.category=impact.destination_id; pattern.source="user"
        removed_record=copy.deepcopy(self._state.categories[category_id])
        direct_base=self.direct_category_base(category_id)
        previous_base=str(getattr(direct_base,"base_tag_id","") or "")
        relationship_ids=self._stage_category_base_for_record(
            removed_record,
            policy="none",
            base_tag_id="",
            previous_base_tag_id=previous_base,
            reason=f"Category deleted into {impact.destination_id}.",
        )
        self._state.categories.pop(category_id,None)
        if impact.disables_default:self._state.disabled_default_ids.add(category_id)
        self._add_action({
            "kind":"category","action":"delete","id":category_id,"destination_id":impact.destination_id,
            "child_categories":list(impact.child_categories),"tag_ids":list(impact.tag_ids),"pattern_ids":list(impact.pattern_ids),
            "relationship_ids":list(relationship_ids),
        })
        self._refresh_inherited_tags()
        return impact

    @staticmethod
    def _snapshot(state: _State) -> _State:
        """Capture immutable history while sharing records until they are edited."""
        return _State(
            categories=dict(state.categories),
            tags=dict(state.tags),
            disabled_default_ids=set(state.disabled_default_ids),
            patterns=dict(state.patterns),
            actions=list(state.actions),
            changed=state.changed,
            removed_tag_ids=set(state.removed_tag_ids),
            removed_tag_archives=dict(state.removed_tag_archives),
            merged_tag_ids=dict(state.merged_tag_ids),
            relationships=dict(state.relationships),
            relationship_file_exists=state.relationship_file_exists,
        )

    def _inherited_tag_ids(self) -> tuple[str, ...]:
        return tuple(record.id for record in self._state.tags.values() if not record.ordering.reviewed)

    def _checkpoint(
        self, *, categories=(), tags=(), patterns=(),
        invalidate_category_anchors: bool = True,
    ) -> None:
        self._require_active_ownership("change staged records")
        self._ownership_generation += 1
        if invalidate_category_anchors:
            self._exact_category_anchor_cache=None
        self._category_children_cache=None
        self._category_descendant_cache.clear()
        self._direct_tag_count_cache=None
        self._undo.append(self._snapshot(self._state))
        self._redo.clear()
        # Copy only records this operation can mutate into history. The live
        # staged objects retain their identity for inspectors and callers, while
        # untouched records remain safely shared across immutable snapshots.
        self._state=self._snapshot(self._state)
        for mapping_name,record_ids in (("categories",categories),("tags",tags),("patterns",patterns)):
            current_mapping=getattr(self._state,mapping_name)
            for record_id in record_ids:
                current=current_mapping.get(record_id)
                if current is None:continue
                initial_mapping=getattr(self._initial,mapping_name)
                if initial_mapping.get(record_id) is current:
                    initial_mapping[record_id]=copy.deepcopy(current)
                detached=None
                for history in self._undo:
                    history_mapping=getattr(history,mapping_name)
                    if history_mapping.get(record_id) is current:
                        if detached is None:detached=copy.deepcopy(current)
                        history_mapping[record_id]=detached
                if mapping_name=="tags":
                    materialize_staged_tag_record(current)

    def _add_action(self, action: dict[str, Any]) -> None:
        self._require_active_ownership("record a staged change")
        self._state.actions.append(action)
        self._state.changed=True

    def undo(self) -> bool:
        self._require_active_ownership("undo changes")
        if not self._undo:
            return False
        self._redo.append(self._snapshot(self._state))
        target=self._undo.pop(); self.last_history_target_was_saved=not target.changed
        self._state = target
        self._exact_category_anchor_cache=None
        self._category_children_cache=None
        self._category_descendant_cache.clear()
        self._direct_tag_count_cache=None
        self._refresh_category_base_authority()
        self._state.changed=self._full_signature(self._state)!=self._full_signature(self._initial)
        self._ownership_generation += 1
        return True

    def redo(self) -> bool:
        self._require_active_ownership("redo changes")
        if not self._redo:
            return False
        self._undo.append(self._snapshot(self._state))
        target=self._redo.pop(); self.last_history_target_was_saved=not target.changed
        self._state = target
        self._exact_category_anchor_cache=None
        self._category_children_cache=None
        self._category_descendant_cache.clear()
        self._direct_tag_count_cache=None
        self._refresh_category_base_authority()
        self._state.changed=self._full_signature(self._state)!=self._full_signature(self._initial)
        self._ownership_generation += 1
        return True

    def reset(self) -> None:
        self._require_active_ownership("reset changes")
        if not self.has_changes:
            return
        self._checkpoint()
        self._state = copy.deepcopy(self._initial)
        for record in self._state.tags.values():
            compact_staged_tag_record(record)
        self._exact_category_anchor_cache=None
        self._category_children_cache=None
        self._category_descendant_cache.clear()
        self._direct_tag_count_cache=None
        self._refresh_category_base_authority()

    @staticmethod
    def _neighbors(rows: list[Any], index: int) -> tuple[str | None, str | None]:
        return (rows[index - 1].id if index else None, rows[index + 1].id if index + 1 < len(rows) else None)

    @staticmethod
    def _rank_between(before: int | None, after: int | None, *, baseline: int) -> int | None:
        if before is None and after is None:
            return baseline
        if before is None:
            candidate = int(after) + TagOrderWorkspaceSession.RANK_STEP
            return candidate if candidate <= MAX_TAG_PRIORITY else None
        if after is None:
            candidate = int(before) - TagOrderWorkspaceSession.RANK_STEP
            return candidate if candidate >= MIN_TAG_PRIORITY else None
        if before - after > 1:
            return after + ((before - after) // 2)
        return None

    @classmethod
    def _renumber(cls, rows: list[Any], setter, baseline: int) -> None:
        if not rows:
            return
        top = min(MAX_TAG_PRIORITY, baseline + (len(rows) // 2) * cls.RANK_STEP)
        bottom = top - (len(rows) - 1) * cls.RANK_STEP
        if bottom < MIN_TAG_PRIORITY:
            top = MIN_TAG_PRIORITY + (len(rows) - 1) * cls.RANK_STEP
        if top > MAX_TAG_PRIORITY:
            raise ValueError("This sibling set is too large to assign compatible numeric ranks.")
        for offset, row in enumerate(rows):
            setter(row, top - offset * cls.RANK_STEP)

    @classmethod
    def _ranks_for_block(cls, before: int | None, after: int | None, count: int, *, baseline: int) -> list[int] | None:
        if count<=0:
            return []
        if before is None and after is None:
            top=min(MAX_TAG_PRIORITY,baseline+((count-1)//2)*cls.RANK_STEP)
            ranks=[top-offset*cls.RANK_STEP for offset in range(count)]
        elif before is None:
            ranks=[int(after)+cls.RANK_STEP*(count-offset) for offset in range(count)]
        elif after is None:
            ranks=[int(before)-cls.RANK_STEP*(offset+1) for offset in range(count)]
        else:
            gap=int(before)-int(after)
            if gap<=count:
                return None
            ranks=[int(before)-((gap*(offset+1))//(count+1)) for offset in range(count)]
        if not ranks or ranks[0]>MAX_TAG_PRIORITY or ranks[-1]<MIN_TAG_PRIORITY or len(set(ranks))!=count:
            return None
        return ranks

    def move_category(self, category_id: str, new_index: int, *, target_group: str | None = None, confirm_cross_group: bool = False) -> None:
        self.move_categories((category_id,),new_index,target_group=target_group,confirm_cross_group=confirm_cross_group)

    def move_categories(self, category_ids, new_index: int, *, target_group: str | None = None, confirm_cross_group: bool = False) -> None:
        selected_ids=tuple(dict.fromkeys(str(value) for value in category_ids))
        if not selected_ids:
            raise ValueError("Choose at least one category to move.")
        records=[self._state.categories[value] for value in selected_ids]
        parent_ids={record.parent_id for record in records}
        old_groups={self._category_effective(record.id)[0] for record in records}
        if len(parent_ids)!=1 or len(old_groups)!=1:
            raise ValueError("Selected categories must share one parent and output group.")
        parent_id=next(iter(parent_ids)); old_group=next(iter(old_groups))
        group = target_group or old_group
        if group != old_group and not confirm_cross_group:
            raise PermissionError("Moving a category to another output group requires confirmation.")
        source=self.category_order(old_group,parent_id=parent_id)
        selected_set=set(selected_ids)
        if not selected_set.issubset({row.id for row in source}):
            raise ValueError("Selected categories are not in the same effective sibling set.")
        selected=[row for row in source if row.id in selected_set]
        rows=(self.category_order(group,parent_id=parent_id) if group!=old_group else source)
        rows=[row for row in rows if row.id not in selected_set]
        new_index=max(0,min(int(new_index),len(rows)))
        rows[new_index:new_index]=selected
        affected_category_ids=tuple(row.id for row in rows)
        self._checkpoint(categories=affected_category_ids,tags=self._inherited_tag_ids())
        rows=[self._state.categories[row.id] for row in rows]
        selected=[self._state.categories[row.id] for row in selected]
        before_id=rows[new_index-1].id if new_index else None
        after_index=new_index+len(selected)
        after_id=rows[after_index].id if after_index<len(rows) else None
        before_rank = self._category_effective(before_id)[1] if before_id else None
        after_rank = self._category_effective(after_id)[1] if after_id else None
        ranks=self._ranks_for_block(before_rank,after_rank,len(selected),baseline=ORDER_GROUP_DEFAULT_PRIORITY[group])

        def set_rank(item: CategoryRecord, value: int) -> None:
            item.source = "user"
            item.ordering_group = group
            item.ordering_priority = value
            item.ordering_reviewed = True

        if ranks is None:
            self._renumber(rows, set_rank, ORDER_GROUP_DEFAULT_PRIORITY[group])
        else:
            for record,rank in zip(selected,ranks):set_rank(record,rank)
        for offset,record in enumerate(selected):
            index=new_index+offset
            record.ordering_provenance.append({"source":"tag_order_workspace","reviewed_at":_now(),"scope":"category","before_id":rows[index-1].id if index else None,"after_id":rows[index+1].id if index+1<len(rows) else None,"group":group,"batch_size":len(selected)})
        self._add_action({"kind":"category","ids":[record.id for record in selected],"before_id":before_id,"after_id":after_id,"group":group})
        self._refresh_inherited_tags()

    def move_tag(self, tag_id: str, new_index: int, *, target_group: str | None = None, confirm_cross_group: bool = False) -> None:
        self.move_tags((tag_id,),new_index,target_group=target_group,confirm_cross_group=confirm_cross_group)

    def move_tags(self, tag_ids, new_index: int, *, target_group: str | None = None, confirm_cross_group: bool = False) -> None:
        selected_ids=tuple(dict.fromkeys(str(value) for value in tag_ids))
        if not selected_ids:
            raise ValueError("Choose at least one SDXL Tag to move.")
        records=[self._state.tags[value] for value in selected_ids]
        categories={record.category for record in records}; old_groups={record.ordering.group for record in records}
        if len(categories)!=1 or len(old_groups)!=1:
            raise ValueError("Selected SDXL Tags must share one category and output group.")
        category=next(iter(categories)); old_group=next(iter(old_groups))
        group = target_group or old_group
        if group != old_group and not confirm_cross_group:
            raise PermissionError("Moving an SDXL Tag to another output group requires confirmation.")
        source=[row for row in self.tag_order(category) if row.ordering.group==old_group]
        selected_set=set(selected_ids)
        if not selected_set.issubset({row.id for row in source}):
            raise ValueError("Selected SDXL Tags are not in the same effective sibling set.")
        selected=[row for row in source if row.id in selected_set]
        rows=([row for row in self.tag_order(category) if row.ordering.group==group] if group!=old_group else source)
        rows=[row for row in rows if row.id not in selected_set]
        new_index=max(0,min(int(new_index),len(rows)))
        rows[new_index:new_index]=selected
        affected_tag_ids=tuple(row.id for row in rows)
        self._checkpoint(tags=affected_tag_ids)
        rows=[self._state.tags[row.id] for row in rows]
        selected=[self._state.tags[row.id] for row in selected]
        before_id=rows[new_index-1].id if new_index else None
        after_index=new_index+len(selected)
        after_id=rows[after_index].id if after_index<len(rows) else None
        before_rank = self._state.tags[before_id].ordering.priority if before_id else None
        after_rank = self._state.tags[after_id].ordering.priority if after_id else None
        _, category_baseline, _ = self._category_effective(category)
        ranks=self._ranks_for_block(before_rank,after_rank,len(selected),baseline=category_baseline)

        def set_rank(item: TagRecord, value: int) -> None:
            item.ordering.group = group
            item.ordering.priority = value
            item.ordering.reviewed = True

        if ranks is None:
            self._renumber(rows, set_rank, category_baseline)
        else:
            for record,rank in zip(selected,ranks):set_rank(record,rank)
        for offset,record in enumerate(selected):
            index=new_index+offset
            record.ordering.provenance.append({"source":"tag_order_workspace","reviewed_at":_now(),"scope":"tag","before_id":rows[index-1].id if index else None,"after_id":rows[index+1].id if index+1<len(rows) else None,"group":group,"batch_size":len(selected)})
        self._add_action({"kind":"tag","ids":[record.id for record in selected],"before_id":before_id,"after_id":after_id,"group":group})

    def restore_tag_inheritance(self, tag_id: str) -> None:
        record = self._state.tags[tag_id]
        if not record.ordering.reviewed:
            return
        self._checkpoint(tags=(tag_id,))
        record = self._state.tags[tag_id]
        group, priority, _ = self._category_effective(record.category)
        record.ordering.group = group
        record.ordering.priority = priority
        record.ordering.reviewed = False
        record.ordering.provenance.append({"source": "tag_order_workspace", "reviewed_at": _now(), "scope": "restore_inheritance"})
        self._add_action({"kind": "tag", "id": tag_id, "restore": True})

    def restore_category_inheritance(self, category_id: str) -> None:
        record = self._state.categories[category_id]
        if not record.parent_id or not record.ordering_group:
            return
        self._checkpoint(categories=(category_id,),tags=self._inherited_tag_ids())
        record = self._state.categories[category_id]
        record.ordering_group = ""
        record.ordering_priority = None
        record.ordering_reviewed = False
        record.source = "user"
        record.ordering_provenance.append({"source": "tag_order_workspace", "reviewed_at": _now(), "scope": "restore_inheritance"})
        self._add_action({"kind": "category", "id": category_id, "restore": True})
        self._refresh_inherited_tags()

    def _refresh_inherited_tags(self) -> None:
        for record in self._state.tags.values():
            if not record.ordering.reviewed:
                group, priority, _ = self._category_effective(record.category)
                record.ordering.group = group
                record.ordering.priority = priority

    @staticmethod
    def _ordering_signature(state: _State) -> tuple[Any, ...]:
        categories = tuple(sorted((item.id, item.ordering_group, item.ordering_priority, item.ordering_reviewed) for item in state.categories.values()))
        tags = tuple(sorted((item.id, item.ordering.group, item.ordering.priority, item.ordering.reviewed) for item in state.tags.values()))
        return categories, tags

    @staticmethod
    def _full_signature(state: _State) -> tuple[Any, ...]:
        categories=tuple(sorted((item.id,item.source,json.dumps(item.to_dict(),sort_keys=True,ensure_ascii=False)) for item in state.categories.values()))
        tags=tuple(sorted((item.id,json.dumps(item.to_dict(),sort_keys=True,ensure_ascii=False)) for item in state.tags.values()))
        patterns=tuple(sorted((item.id,item.source,json.dumps(item.to_dict(),sort_keys=True,ensure_ascii=False)) for item in state.patterns.values()))
        relationships=tuple(sorted(
            (
                item.id,
                json.dumps(item.to_dict(),sort_keys=True,ensure_ascii=False),
            )
            for item in state.relationships.values()
        ))
        return (
            categories,tags,tuple(sorted(state.disabled_default_ids)),patterns,
            tuple(sorted(state.removed_tag_ids)),
            tuple(
                sorted(
                    (
                        archive.id,
                        json.dumps(
                            archive.to_dict(),sort_keys=True,ensure_ascii=False,
                        ),
                    )
                    for archive in state.removed_tag_archives.values()
                )
            ),
            tuple(sorted(state.merged_tag_ids.items())),
            relationships,state.relationship_file_exists,
        )

    def _relationship_state_changed(self) -> bool:
        return (
            self._state.relationship_file_exists
            != self._initial.relationship_file_exists
            or self._state.relationships != self._initial.relationships
        )

    def _category_base_only_relationship_change(self) -> bool:
        changed_ids={
            relationship_id
            for relationship_id in (
                set(self._initial.relationships)|set(self._state.relationships)
            )
            if self._initial.relationships.get(relationship_id)
            != self._state.relationships.get(relationship_id)
        }
        return bool(changed_ids) and all(
            (
                self._state.relationships.get(relationship_id)
                or self._initial.relationships.get(relationship_id)
            ).kind==CATEGORY_BASE
            for relationship_id in changed_ids
        )

    def _relationship_payload(self) -> dict[str, Any]:
        candidate = RelationshipStore(self.user_relationship_path)
        candidate.schema_version = self.user_relationships.schema_version
        candidate.extras = copy.deepcopy(self.user_relationships.extras)
        candidate.replace(self._state.relationships.values())
        return candidate.payload()

    def transaction_payloads(self, *, include_patterns: bool = False) -> dict[Path,dict[str,Any] | None]:
        """Serialize staged stores; explicit native mode includes Patterns.

        Native mode represents relationship-file absence with None and detaches
        returned data from the live authorities. This is a payload preview, not
        current-source admission, a recovery classifier or retirement authority.
        The default retains the narrower existing conversion-preview contract.
        """

        if type(include_patterns) is not bool:
            raise ValueError("Choose the explicit native transaction preview mode.")
        if not self.has_changes:
            raise ValueError("There are no staged Category Workspace changes.")
        if self.live_patterns is not None and not include_patterns:
            raise ValueError(
                "This payload preview is limited to Categories, Tags, and relationships."
            )
        effective=self._effective_relationships()
        category_view=copy.copy(self.live_categories)
        category_view.categories=copy.deepcopy(self._state.categories)
        category_view.disabled_default_ids=set(self._state.disabled_default_ids)
        category_view.configure_category_base_authority(effective.values())
        tag_view=copy.copy(self.live_tags)
        tag_view.tags=copy.deepcopy(self._state.tags)
        tag_view.removed_tag_ids=set(self._state.removed_tag_ids)
        tag_view.removed_tag_archives=copy.deepcopy(self._state.removed_tag_archives)
        tag_view.merged_tag_ids=dict(self._state.merged_tag_ids)
        tag_view._rebuild_lookup()
        publish_tag_semantic_authority(tag_view,effective.values())
        publish_reusable_modifier_storage_authority(
            tag_view,effective.values(),categories=category_view,
            foundation=self._modifier_foundation,
        )
        publish_scoped_constraint_storage_authority(tag_view,effective.values())
        payloads={
            self.live_categories.user_path:category_view.user_payload(
                category_base_relationships=effective.values(),
            ),
            self.live_tags.path:tag_view._payload(),
        }
        if self.live_patterns is not None:
            pattern_view=copy.copy(self.live_patterns)
            pattern_view.patterns=copy.deepcopy(self._state.patterns)
            payloads[self.live_patterns.user_path]=pattern_view.user_payload()
        if self._relationship_state_changed():
            payloads[self.user_relationship_path]=(
                None if include_patterns and not self._state.relationship_file_exists
                else self._relationship_payload()
            )
        return copy.deepcopy(payloads) if include_patterns else payloads

    def impact(self, *, organization_depends_on_order: bool = False, affected_image_paths: tuple[str, ...] | None = None) -> OrderImpact:
        initial_categories = {item.id:(item.source,item.to_dict()) for item in self._initial.categories.values()}
        current_categories = {item.id:(item.source,item.to_dict()) for item in self._state.categories.values()}
        initial_tags = {item.id:item.to_dict() for item in self._initial.tags.values()}
        current_tags = {item.id:item.to_dict() for item in self._state.tags.values()}
        initial_patterns={item.id:item.to_dict() for item in self._initial.patterns.values()}
        current_patterns={item.id:item.to_dict() for item in self._state.patterns.values()}
        changed_categories = tuple(sorted(value for value in set(initial_categories)|set(current_categories) if initial_categories.get(value)!=current_categories.get(value)))
        changed_tags = tuple(sorted(value for value in set(initial_tags)|set(current_tags) if initial_tags.get(value)!=current_tags.get(value)))
        created_categories=tuple(sorted(set(current_categories)-set(initial_categories)))
        removed_categories=tuple(sorted(set(initial_categories)-set(current_categories)))
        removed_tags=tuple(sorted(set(initial_tags)-set(current_tags)))
        reassigned_tags=tuple(sorted(value for value in set(initial_tags)&set(current_tags) if initial_tags[value].get("category")!=current_tags[value].get("category")))
        reassigned_patterns=tuple(sorted(value for value in set(initial_patterns)&set(current_patterns) if initial_patterns[value].get("category")!=current_patterns[value].get("category")))
        affected_category_ids = set(changed_categories)
        affected_ids=set(changed_tags)
        affected_ids.update(item.id for item in self._state.tags.values() if item.category in affected_category_ids)
        affected = len(affected_ids)
        before = self._representative(self._initial)
        after = self._representative(self._state)
        changed = self._ordering_signature(self._state) != self._ordering_signature(self._initial)
        return OrderImpact(
            changed_categories=changed_categories,changed_tags=changed_tags,
            affected_tag_ids=tuple(sorted(affected_ids)),affected_tags=affected,
            affected_image_paths=affected_image_paths,caption_examples_before=before,caption_examples_after=after,
            captions_may_be_stale=changed,metadata_may_be_stale=changed,
            organization_affected=bool(changed and organization_depends_on_order),
            created_categories=created_categories,removed_categories=removed_categories,
            removed_tags=removed_tags,reassigned_tags=reassigned_tags,reassigned_patterns=reassigned_patterns,
        )

    @staticmethod
    def _representative(state: _State, limit: int = 12) -> tuple[str, ...]:
        rows = sorted(state.tags.values(), key=lambda item: (ORDER_GROUP_RANK[item.ordering.group], -item.ordering.priority, item.display.casefold(), item.id))
        return tuple(item.display for item in rows[:limit])

    @classmethod
    def recover_interrupted(cls, directory: str | Path) -> list[Path]:
        data_root = Path(directory).resolve()
        # Recovery can restore tags.json directly. Hold the same exclusion as
        # native Tag writes before inspecting or changing any recovery file.
        with RulePairAdmission(data_root).admit():
            return cls._recover_interrupted_admitted(data_root)

    @classmethod
    def _recover_interrupted_admitted(cls, data_root: Path) -> list[Path]:
        from .category_retirement import preflight_category_retirements
        # Preflight *all* typed attempts before restoring any legacy receipt.
        preflight_category_retirements(data_root)
        restored = []
        roots=(data_root / "backups" / "tag-order",data_root / "backups" / "category-workspace")
        manifests=[path for root in roots if root.exists() for path in root.glob("*/recovery.json")]
        for manifest_path in sorted(manifests):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("status") != "pending":
                continue
            for entry in manifest.get("files", []):
                target = Path(entry["target"]).resolve()
                backup = (manifest_path.parent / entry["backup"]).resolve()
                if target.parent != data_root or target.name not in {"tags.json","categories.json","patterns.json","relationships.json"}:
                    raise RuntimeError("Category Workspace recovery manifest contains an unsafe target.")
                if backup.parent != manifest_path.parent.resolve():
                    raise RuntimeError("Category Workspace recovery manifest contains an unsafe backup path.")
                if entry.get("existed"):
                    _atomic_bytes(target, backup.read_bytes())
                elif target.exists():
                    target.unlink()
            manifest["status"] = "restored"
            _atomic_text(manifest_path, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
            restored.append(manifest_path.parent)
        return restored

    def save(self, *, organization_depends_on_order: bool = False, affected_image_paths: tuple[str, ...] | None = None,
             preserve_history: bool = False, category_details_only: bool = False,
             recovery_id: str = "") -> OrderSaveResult:
        admission = self.live_tags.rule_pair_admission
        with (admission.admit() if admission is not None else nullcontext()):
            return self._save_admitted(
                organization_depends_on_order=organization_depends_on_order,
                affected_image_paths=affected_image_paths,
                preserve_history=preserve_history,
                category_details_only=category_details_only,
                recovery_id=recovery_id,
            )

    def _save_admitted(self, *, organization_depends_on_order: bool = False, affected_image_paths: tuple[str, ...] | None = None,
                       preserve_history: bool = False, category_details_only: bool = False,
                       recovery_id: str = "") -> OrderSaveResult:
        self._require_active_ownership("save changes")
        impact = self.impact(organization_depends_on_order=organization_depends_on_order,affected_image_paths=affected_image_paths)
        if not self.has_changes:
            raise ValueError("There are no staged ordering changes to save.")
        relationship_state_changed = self._relationship_state_changed()
        if category_details_only and (
            impact.changed_tags
            or impact.reassigned_patterns
            or impact.removed_tags
            or (
                relationship_state_changed
                and not self._category_base_only_relationship_change()
            )
        ):
            raise ValueError("A category-details save cannot include SDXL Tag, removal, reusable-pattern, or non-Category-Base relationship changes.")
        if self._file_snapshot() != self._initial_files:
            raise RuntimeError("System Knowledge changed after this order was opened. Reset the staged order and review the current records before saving again.")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        issue_records=self.structural_issue_records()
        initial_issue_keys={
            (issue.code,issue.summary,issue.record_keys)
            for issue in self.structural_issue_records(self._initial)
        }
        introduced_issues=tuple(
            issue for issue in issue_records
            if (issue.code,issue.summary,issue.record_keys) not in initial_issue_keys
        )
        if introduced_issues:
            raise ValueError(
                "Resolve Category Workspace health issues introduced by this change before saving: "
                +"; ".join(issue.summary for issue in introduced_issues[:3])
            )
        requested_recovery_id=str(recovery_id or "").strip()
        if requested_recovery_id and (
            not requested_recovery_id.startswith("op_")
            or not requested_recovery_id.removeprefix("op_").isalnum()
        ):
            raise ValueError("A Category Workspace recovery identity is invalid.")
        backup = self.live_tags.path.parent / "backups" / "category-workspace" / (
            requested_recovery_id or f"{stamp}_{uuid4().hex[:8]}"
        )
        backup.mkdir(parents=True, exist_ok=False)
        paths = (
            tuple(dict.fromkeys((
                self.live_categories.user_path,
                *((self.user_relationship_path,) if relationship_state_changed else ()),
            )))
            if category_details_only
            else tuple(dict.fromkeys((
                *self._knowledge_paths(),
                *((self.user_relationship_path,) if relationship_state_changed else ()),
            )))
        )
        entries = []
        for index, path in enumerate(paths):
            name = f"{index}_{path.name}"
            if path.exists():
                shutil.copy2(path, backup / name)
            entries.append({"target": str(path.resolve()), "backup": name, "existed": path.exists()})
        manifest_path = backup / "recovery.json"
        manifest = {"status": "pending", "created_at": _now(), "files": entries}
        _atomic_text(manifest_path, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        try:
            self.live_categories.categories = copy.deepcopy(self._state.categories)
            self.live_categories.disabled_default_ids=set(self._state.disabled_default_ids)
            effective_relationships = self._effective_relationships()
            self.live_categories.save_user(
                category_base_relationships=effective_relationships.values(),
            )
            if category_details_only and relationship_state_changed:
                if self._state.relationship_file_exists:
                    _atomic_text(
                        self.user_relationship_path,
                        json.dumps(
                            self._relationship_payload(),
                            indent=2,
                            ensure_ascii=False,
                        ) + "\n",
                    )
                elif self.user_relationship_path.exists():
                    self.user_relationship_path.unlink()
            reloaded_categories = CategoryRegistry(self.live_categories.default_path, self.live_categories.user_path)
            if category_details_only:
                reloaded_relationships=RelationshipStore(self.user_relationship_path)
                reloaded_effective_relationships=dict(
                    self.default_relationships.relationships,
                )
                reloaded_effective_relationships.update(
                    reloaded_relationships.relationships,
                )
                reloaded_categories.configure_category_base_authority(
                    reloaded_effective_relationships.values(),
                )
                actual = _State(
                    reloaded_categories.categories,self._state.tags,set(reloaded_categories.disabled_default_ids),
                    self._state.patterns,removed_tag_ids=set(self._state.removed_tag_ids),
                    removed_tag_archives=copy.deepcopy(self._state.removed_tag_archives),
                    merged_tag_ids=dict(self._state.merged_tag_ids),
                    relationships=dict(reloaded_relationships.relationships),
                    relationship_file_exists=self.user_relationship_path.exists(),
                )
            else:
                self.live_tags.tags = copy.deepcopy(self._state.tags)
                self.live_tags.removed_tag_ids=set(self._state.removed_tag_ids)
                self.live_tags.removed_tag_archives=copy.deepcopy(
                    self._state.removed_tag_archives
                )
                self.live_tags.merged_tag_ids=dict(self._state.merged_tag_ids)
                self.live_tags._rebuild_lookup()
                publish_tag_semantic_authority(
                    self.live_tags,effective_relationships.values(),
                )
                self._publish_live_reusable_modifier_authority(
                    effective_relationships,
                    categories=reloaded_categories,
                )
                self._publish_live_scoped_constraint_authority(
                    effective_relationships,
                )
                self.live_tags.save()
                if self.live_patterns is not None:
                    self.live_patterns.patterns=copy.deepcopy(self._state.patterns)
                    self.live_patterns.save_user()
                if relationship_state_changed:
                    if self._state.relationship_file_exists:
                        _atomic_text(
                            self.user_relationship_path,
                            json.dumps(
                                self._relationship_payload(),
                                indent=2,
                                ensure_ascii=False,
                            ) + "\n",
                        )
                    elif self.user_relationship_path.exists():
                        self.user_relationship_path.unlink()
                reloaded_tags = TagDatabase(
                    self.live_tags.path,
                    rule_pair_admission=self.live_tags.rule_pair_admission,
                ); reloaded_tags.apply_category_ordering(reloaded_categories)
                reloaded_patterns=PatternRegistry(self.live_patterns.default_path,self.live_patterns.user_path) if self.live_patterns is not None else None
                reloaded_relationships = RelationshipStore(self.user_relationship_path)
                reloaded_effective_relationships=dict(
                    self.default_relationships.relationships,
                )
                reloaded_effective_relationships.update(
                    reloaded_relationships.relationships,
                )
                reloaded_categories.configure_category_base_authority(
                    reloaded_effective_relationships.values(),
                )
                actual = _State(
                    reloaded_categories.categories,reloaded_tags.tags,set(reloaded_categories.disabled_default_ids),
                    reloaded_patterns.patterns if reloaded_patterns is not None else {},
                    removed_tag_ids=set(reloaded_tags.removed_tag_ids),
                    removed_tag_archives=copy.deepcopy(
                        reloaded_tags.removed_tag_archives
                    ),
                    merged_tag_ids=dict(reloaded_tags.merged_tag_ids),
                    relationships=dict(reloaded_relationships.relationships),
                    relationship_file_exists=self.user_relationship_path.exists(),
                )
            self._clear_represented_category_base_fields(
                actual.categories,
                category_base_relationship_index(
                    reloaded_effective_relationships.values(),
                ),
            )
            self._clear_represented_semantic_fields(
                actual.tags,
                tag_semantic_relationship_index(
                    reloaded_effective_relationships.values(),
                ),
            )
            self._clear_represented_reusable_modifier_fields(
                actual.tags,
                reloaded_effective_relationships,
                categories=reloaded_categories,
            )
            self._clear_represented_scoped_constraint_fields(
                actual.tags,reloaded_effective_relationships,
            )
            if self._full_signature(actual) != self._full_signature(self._state):
                raise RuntimeError("Saved Category Workspace changes failed read-back verification.")
        except BaseException:
            for entry in entries:
                target = Path(entry["target"])
                saved = backup / entry["backup"]
                if entry["existed"]:
                    _atomic_bytes(target, saved.read_bytes())
                elif target.exists():
                    target.unlink()
            self.live_categories.load()
            if not category_details_only:
                self.live_tags.load()
                if self.live_patterns is not None:self.live_patterns.load()
            if relationship_state_changed:
                self.user_relationships.load()
            self._refresh_category_base_authority()
            persisted_relationships=dict(
                self.default_relationships.relationships,
            )
            persisted_relationships.update(
                self.user_relationships.relationships,
            )
            self.live_categories.configure_category_base_authority(
                persisted_relationships.values(),
            )
            if not category_details_only:
                self._publish_live_semantic_authority()
                self._refresh_reusable_modifier_authority(publish_live=True)
                self._refresh_scoped_constraint_authority(publish_live=True)
            manifest["status"] = "restored"
            _atomic_text(manifest_path, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
            raise
        manifest["status"] = "complete"
        _atomic_text(manifest_path, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        self._state.changed=False
        if relationship_state_changed:
            self.user_relationships.load()
        self._refresh_category_base_authority(publish_live=True)
        if not category_details_only:
            self._refresh_tag_semantic_authority(publish_live=True)
            self._refresh_scoped_constraint_authority(publish_live=True)
        for record in self._state.tags.values():
            compact_staged_tag_record(record)
        self._initial = self._snapshot(self._state)
        self._initial_files = self._file_snapshot()
        loaded=self.reload_source_snapshot()
        self._reload_baseline=(
            self._initial_files+loaded[len(self._initial_files):]
            if (
                loaded[:len(self._initial_files)]==self._initial_files
                and self._rebase_source_receipts_match()
            ) else None
        )
        if not preserve_history:
            self._undo.clear()
            self._redo.clear()
        return OrderSaveResult(impact, backup)


# CATUX-01 broadens the existing staged ordering session into the shared
# Category Workspace transaction while retaining the prior public name for
# compatible callers and recovery manifests.
CategoryWorkspaceSession = TagOrderWorkspaceSession
