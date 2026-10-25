from __future__ import annotations

from dataclasses import dataclass
from contextlib import nullcontext
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable

from .allowed_dimension_policy import (
    AllowedDimensionPolicyResult,
    AllowedDimensionPolicyService,
    AllowedDimensionPolicyUpdate,
    AllowedDimensionValueScope,
)
from .knowledge_relationships import (
    ENTAILMENT,
    SCOPED_CONSTRAINT,
    SPECIFICITY,
    KnowledgeRelationship,
    RelationshipEndpoint,
)
from .relationship_workspace import (
    RelationshipWorkspaceService,
    relationship_fingerprint,
)
from .tag_semantic_relationships import (
    TagSemanticChange,
    TagSemanticRelationshipService,
    TagSemanticSaveResult,
)


SET_BROADER_TAG = "set_broader_tag"
ADD_ALSO_MEANS = "add_also_means"
REMOVE_ALSO_MEANS = "remove_also_means"
SET_REUSABLE_VALUE_SCOPE = "set_reusable_value_scope"
REMOVE_REUSABLE_COLLECTION = "remove_reusable_collection"
RESET_REUSABLE_POLICY = "reset_reusable_policy"
CREATE_CONTRARY_SET = "create_contrary_set"


@dataclass(frozen=True)
class RelationshipBatchRow:
    """One exact before/after outcome disclosed by a batch preview."""

    tag_id: str
    display: str
    before: str
    after: str
    outcome: str = "change"
    reason: str = ""


@dataclass(frozen=True)
class RelationshipBatchPlan:
    """An immutable, selection-first relationship action ready for review."""

    operation: str
    title: str
    rows: tuple[RelationshipBatchRow, ...]
    payload: Any
    expected: tuple[tuple[str, str], ...] = ()

    @property
    def changed_rows(self) -> tuple[RelationshipBatchRow, ...]:
        return tuple(row for row in self.rows if row.outcome == "change")

    @property
    def unchanged_rows(self) -> tuple[RelationshipBatchRow, ...]:
        return tuple(row for row in self.rows if row.outcome == "unchanged")

    @property
    def held_rows(self) -> tuple[RelationshipBatchRow, ...]:
        return tuple(row for row in self.rows if row.outcome == "hold")

    @property
    def changed_tag_ids(self) -> tuple[str, ...]:
        return tuple(row.tag_id for row in self.changed_rows)

    @property
    def summary(self) -> str:
        return (
            f"{len(self.changed_rows):,} will change; "
            f"{len(self.unchanged_rows):,} already match; "
            f"{len(self.held_rows):,} held for review."
        )


@dataclass(frozen=True)
class RelationshipBatchUndo:
    operation: str
    payload: Any


@dataclass(frozen=True)
class RelationshipBatchResult:
    operation: str
    tag_ids: tuple[str, ...]
    manifest_path: Path
    undo: RelationshipBatchUndo


class RelationshipBatchService:
    """Preview and apply one atomic relationship action to many Tags.

    This boundary intentionally delegates persistence to the established
    relationship writers.  It never serially saves one selected row at a time.
    """

    def __init__(
        self,
        tags,
        categories,
        foundation,
        *,
        default_path: str | Path,
        user_path: str | Path,
    ) -> None:
        self.tags = tags
        self.categories = categories
        self.foundation = foundation
        self.default_path = Path(default_path)
        self.user_path = Path(user_path)
        self._reload_services()

    def _reload_services(self) -> None:
        self.workspace = RelationshipWorkspaceService(
            self.default_path, self.user_path, tags=self.tags,
        )
        self.semantic = TagSemanticRelationshipService(
            self.tags,
            default_path=self.default_path,
            user_path=self.user_path,
            workspace=self.workspace,
        )
        self.allowed = AllowedDimensionPolicyService(
            self.tags,
            self.categories,
            self.foundation,
            default_path=self.default_path,
            user_path=self.user_path,
            workspace=self.workspace,
        )

    def _records(self, tag_ids: Iterable[str]) -> tuple[Any, ...]:
        records = []
        seen = set()
        for raw_tag_id in tag_ids:
            tag_id = str(raw_tag_id)
            if tag_id in seen:
                continue
            seen.add(tag_id)
            record = self.tags.tags.get(tag_id)
            if record is None:
                raise ValueError(f"Tag {tag_id!r} is no longer available.")
            records.append(record)
        if not records:
            raise ValueError("Select at least one Tag first.")
        return tuple(records)

    @staticmethod
    def _names(ids: Iterable[str], tags) -> str:
        values = []
        for tag_id in ids:
            record = tags.tags.get(str(tag_id))
            values.append(str(getattr(record, "display", "") or tag_id))
        return ", ".join(values) if values else "None"

    def plan_set_broader_tag(
        self, tag_ids: Iterable[str], broader_tag_id: str,
    ) -> RelationshipBatchPlan:
        records = self._records(tag_ids)
        target_id = str(broader_tag_id or "")
        target = self.tags.tags.get(target_id) if target_id else None
        if target_id and target is None:
            raise ValueError("The selected Broader Tag is no longer available.")
        rows = []
        changes = []
        undo = []
        after_label = str(getattr(target, "display", "") or "None")
        for record in records:
            current = self.semantic.effective_for_tag(record.id)
            before_label = self._names((current.parent_id,), self.tags)
            if record.id == target_id:
                rows.append(RelationshipBatchRow(
                    record.id, record.display, before_label, after_label,
                    "hold", "A Tag cannot be broader than itself.",
                ))
                continue
            removes_duplicate = bool(target_id and target_id in current.implies)
            outcome = (
                "unchanged"
                if current.parent_id == target_id and not removes_duplicate
                else "change"
            )
            rows.append(RelationshipBatchRow(
                record.id, record.display, before_label, after_label, outcome,
                (
                    "The duplicate Also Means link will be removed because the "
                    "Broader Tag already supplies that meaning."
                    if removes_duplicate else ""
                ),
            ))
            if outcome == "change":
                kinds = (
                    (SPECIFICITY, ENTAILMENT)
                    if removes_duplicate else (SPECIFICITY,)
                )
                changes.append(TagSemanticChange(
                    record.id,
                    parent_id=target_id,
                    implies=tuple(
                        value for value in current.implies if value != target_id
                    ),
                    kinds=kinds,
                    reason="relationship_workbench_batch",
                ))
                undo.append(TagSemanticChange(
                    record.id,
                    parent_id=current.parent_id,
                    implies=current.implies,
                    kinds=kinds,
                    reason="relationship_workbench_undo",
                ))
        return RelationshipBatchPlan(
            SET_BROADER_TAG, "Set Broader Tag", tuple(rows),
            (tuple(changes), tuple(undo)),
        )

    def plan_also_means(
        self,
        tag_ids: Iterable[str],
        implied_tag_id: str,
        *,
        remove: bool = False,
    ) -> RelationshipBatchPlan:
        records = self._records(tag_ids)
        target_id = str(implied_tag_id)
        target = self.tags.tags.get(target_id)
        if target is None:
            raise ValueError("The selected meaning Tag is no longer available.")
        rows = []
        changes = []
        undo = []
        for record in records:
            current = self.semantic.effective_for_tag(record.id)
            before = tuple(current.implies)
            if record.id == target_id and not remove:
                rows.append(RelationshipBatchRow(
                    record.id, record.display, self._names(before, self.tags),
                    self._names(before, self.tags), "hold",
                    "A Tag cannot also mean itself.",
                ))
                continue
            if current.parent_id == target_id and not remove:
                rows.append(RelationshipBatchRow(
                    record.id,
                    record.display,
                    self._names(before, self.tags),
                    self._names(before, self.tags),
                    "unchanged",
                    "The Broader Tag already supplies this meaning.",
                ))
                continue
            after = tuple(
                value for value in before if value != target_id
            ) if remove else tuple(dict.fromkeys((*before, target_id)))
            outcome = "unchanged" if after == before else "change"
            rows.append(RelationshipBatchRow(
                record.id, record.display, self._names(before, self.tags),
                self._names(after, self.tags), outcome,
            ))
            if outcome == "change":
                changes.append(TagSemanticChange(
                    record.id, implies=after, kinds=(ENTAILMENT,),
                    reason="relationship_workbench_batch",
                ))
                undo.append(TagSemanticChange(
                    record.id, implies=before, kinds=(ENTAILMENT,),
                    reason="relationship_workbench_undo",
                ))
        operation = REMOVE_ALSO_MEANS if remove else ADD_ALSO_MEANS
        title = "Remove Also Means" if remove else "Add Also Means"
        return RelationshipBatchPlan(
            operation, title, tuple(rows), (tuple(changes), tuple(undo)),
        )

    @staticmethod
    def _scope_label(scope: AllowedDimensionValueScope) -> str:
        if scope.mode == "all":
            return "All values"
        if scope.mode == "only":
            count = len(scope.value_ids) + len(scope.category_ids)
            return f"Only {count:,} chosen value/branch item{'s' if count != 1 else ''}"
        if scope.mode == "all_except":
            count = len(scope.exception_value_ids) + len(scope.exception_category_ids)
            return f"All except {count:,} chosen value/branch item{'s' if count != 1 else ''}"
        return "Needs value review"

    def plan_reusable_collection(
        self,
        tag_ids: Iterable[str],
        family_id: str,
        *,
        scope: AllowedDimensionValueScope | None = None,
        remove: bool = False,
    ) -> RelationshipBatchPlan:
        records = self._records(tag_ids)
        stable_family = str(family_id)
        if not stable_family:
            raise ValueError("Choose one reusable value collection.")
        desired_scope = scope or AllowedDimensionValueScope(stable_family)
        rows = []
        updates = {}
        expected = {}
        for record in records:
            current = self.allowed.effective_for_tag(record.id)
            if current.blocked_reason:
                rows.append(RelationshipBatchRow(
                    record.id, record.display, "Blocked policy", "No change",
                    "hold", current.blocked_reason,
                ))
                continue
            groups = list(current.groups)
            scopes = {item.family_id: item for item in current.value_scopes}
            before_scope = scopes.get(stable_family)
            if remove:
                if stable_family in groups:
                    groups.remove(stable_family)
                scopes.pop(stable_family, None)
                after_text = "Collection not allowed"
                unchanged = before_scope is None and stable_family not in current.groups
            else:
                if stable_family not in groups:
                    groups.append(stable_family)
                scopes[stable_family] = desired_scope
                after_text = self._scope_label(desired_scope)
                unchanged = (
                    stable_family in current.groups
                    and before_scope == desired_scope
                    and tuple(groups) == current.groups
                )
            before_text = (
                self._scope_label(before_scope)
                if before_scope is not None else "Collection not allowed"
            )
            outcome = "unchanged" if unchanged else "change"
            rows.append(RelationshipBatchRow(
                record.id, record.display, before_text, after_text, outcome,
            ))
            if outcome == "change":
                updates[record.id] = AllowedDimensionPolicyUpdate(
                    tuple(groups),
                    tuple(scopes[group] for group in groups),
                )
                expected[record.id] = current.record_fingerprint
        operation = (
            REMOVE_REUSABLE_COLLECTION if remove else SET_REUSABLE_VALUE_SCOPE
        )
        title = (
            "Remove Reusable Collection" if remove
            else "Set Reusable Value Scope"
        )
        return RelationshipBatchPlan(
            operation, title, tuple(rows), updates,
            tuple(sorted(expected.items())),
        )

    def plan_reset_reusable_policy(
        self, tag_ids: Iterable[str],
    ) -> RelationshipBatchPlan:
        records = self._records(tag_ids)
        rows = []
        updates = {}
        expected = {}
        for record in records:
            current = self.allowed.effective_for_tag(record.id)
            if current.blocked_reason:
                rows.append(RelationshipBatchRow(
                    record.id, record.display, "Blocked policy", "No change",
                    "hold", current.blocked_reason,
                ))
                continue
            outcome = "change" if current.layer == "user" else "unchanged"
            rows.append(RelationshipBatchRow(
                record.id, record.display,
                ", ".join(current.groups) or "No reusable collections",
                "Reveal shipped or inherited policy", outcome,
            ))
            if outcome == "change":
                updates[record.id] = None
                expected[record.id] = current.record_fingerprint
        return RelationshipBatchPlan(
            RESET_REUSABLE_POLICY, "Reset Reusable Policy", tuple(rows),
            updates, tuple(sorted(expected.items())),
        )

    def plan_contrary_set(
        self,
        tag_ids: Iterable[str],
        *,
        label: str = "",
        scope: str = "same_context",
    ) -> RelationshipBatchPlan:
        records = self._records(tag_ids)
        if len(records) < 2:
            raise ValueError("Select at least two Tags for a Contrary Values set.")
        if len(records) > 100:
            pair_count = len(records) * (len(records) - 1) // 2
            raise ValueError(
                f"This selection would create {pair_count:,} pair projections. "
                "Filter it to 100 or fewer genuinely interchangeable values, "
                "then review the narrower set."
            )
        stable_ids = tuple(sorted(record.id for record in records))
        digest = sha256("\x1f".join(stable_ids).encode("utf-8")).hexdigest()[:20]
        relationship_id = f"constraint.batch.{digest}"
        relationship = KnowledgeRelationship(
            relationship_id,
            SCOPED_CONSTRAINT,
            tuple(
                RelationshipEndpoint("tag", record.id, record.display)
                for record in sorted(records, key=lambda item: item.id)
            ),
            directed=False,
            scope=str(scope or "same_context"),
            label=str(label or "Mutually contrary values"),
            source="user_review",
            layer="user",
        )
        current = next(
            (item for item in self.workspace.effective if item.id == relationship_id),
            None,
        )
        if current is not None and current.kind != SCOPED_CONSTRAINT:
            raise ValueError(
                "The generated Contrary Values identity is already used by "
                "another relationship type. No change was planned."
            )
        prior_user = self.workspace.user_store.relationships.get(relationship_id)
        unchanged = current is not None and (
            current.kind == relationship.kind
            and current.endpoints == relationship.endpoints
            and current.scope == relationship.scope
            and current.label == relationship.label
            and not current.disabled
        )
        pair_count = len(records) * (len(records) - 1) // 2
        outcome = "unchanged" if unchanged else "change"
        rows = tuple(
            RelationshipBatchRow(
                record.id, record.display,
                "Already in this set" if unchanged else "Not in this set",
                f"Contrary to {len(records)-1:,} other selected Tag(s)", outcome,
            )
            for record in records
        )
        payload = None if unchanged else (relationship, current, prior_user)
        expected = ((relationship_id, (
            relationship_fingerprint(current) if current is not None else "missing"
        )),)
        title = f"Create Contrary Values Set ({pair_count:,} pairs)"
        return RelationshipBatchPlan(
            CREATE_CONTRARY_SET, title, rows, payload, expected,
        )

    def apply(
        self,
        plan: RelationshipBatchPlan,
        *,
        backup_root: str | Path | None = None,
    ) -> RelationshipBatchResult:
        with (self.allowed._admission() if plan.operation in {
            SET_REUSABLE_VALUE_SCOPE,REMOVE_REUSABLE_COLLECTION,RESET_REUSABLE_POLICY,
        } else self.semantic._admission() if plan.operation in {
            SET_BROADER_TAG, ADD_ALSO_MEANS, REMOVE_ALSO_MEANS,
        } else self.workspace._admission(self.tags) if plan.operation==CREATE_CONTRARY_SET
        else nullcontext()):
            return self._apply_admitted(plan,backup_root=backup_root)

    def _apply_admitted(
        self,plan: RelationshipBatchPlan, *,backup_root: str | Path | None = None,
    ) -> RelationshipBatchResult:
        if not plan.changed_rows or plan.payload is None:
            raise ValueError("This preview has no changes to apply.")
        if plan.operation in {
            SET_BROADER_TAG, ADD_ALSO_MEANS, REMOVE_ALSO_MEANS,
        }:
            changes, inverse = plan.payload
            result: TagSemanticSaveResult = self.semantic.apply(
                changes, backup_root=backup_root,
            )
            batch = RelationshipBatchResult(
                plan.operation, result.tag_ids, result.manifest_path,
                RelationshipBatchUndo(plan.operation, inverse),
            )
        elif plan.operation in {
            SET_REUSABLE_VALUE_SCOPE,
            REMOVE_REUSABLE_COLLECTION,
            RESET_REUSABLE_POLICY,
        }:
            result: AllowedDimensionPolicyResult = self.allowed.apply(
                plan.payload,
                expected_fingerprints=dict(plan.expected),
                operation="relationship_workbench_batch",
                review_source="relationship_workbench",
                backup_root=backup_root,
            )
            batch = RelationshipBatchResult(
                plan.operation, result.tag_ids, result.manifest_path,
                RelationshipBatchUndo(plan.operation, result),
            )
        elif plan.operation == CREATE_CONTRARY_SET:
            relationship, current, prior_user = plan.payload
            expected = dict(plan.expected)[relationship.id]
            manifest = self.workspace.save_override(
                relationship,
                expected_fingerprint=(None if expected == "missing" else expected),
                backup_root=backup_root,
                compatibility_tags=self.tags,
            )
            batch = RelationshipBatchResult(
                plan.operation, plan.changed_tag_ids, manifest,
                RelationshipBatchUndo(
                    plan.operation, (relationship.id, prior_user),
                ),
            )
        else:
            raise ValueError(f"Unsupported relationship batch {plan.operation!r}.")
        self._reload_services()
        return batch

    def undo(
        self,
        result: RelationshipBatchResult,
        *,
        backup_root: str | Path | None = None,
    ) -> RelationshipBatchResult:
        with (self.allowed._admission() if result.undo.operation in {
            SET_REUSABLE_VALUE_SCOPE,REMOVE_REUSABLE_COLLECTION,RESET_REUSABLE_POLICY,
        } else self.semantic._admission() if result.undo.operation in {
            SET_BROADER_TAG, ADD_ALSO_MEANS, REMOVE_ALSO_MEANS,
        } else self.workspace._admission(self.tags) if result.undo.operation==CREATE_CONTRARY_SET
        else nullcontext()):
            return self._undo_admitted(result,backup_root=backup_root)

    def _undo_admitted(
        self,result: RelationshipBatchResult, *,backup_root: str | Path | None = None,
    ) -> RelationshipBatchResult:
        operation = result.undo.operation
        if operation in {SET_BROADER_TAG, ADD_ALSO_MEANS, REMOVE_ALSO_MEANS}:
            restored = self.semantic.apply(
                result.undo.payload, backup_root=backup_root,
            )
            undone = RelationshipBatchResult(
                operation, restored.tag_ids, restored.manifest_path,
                RelationshipBatchUndo(operation, ()),
            )
        elif operation in {
            SET_REUSABLE_VALUE_SCOPE,
            REMOVE_REUSABLE_COLLECTION,
            RESET_REUSABLE_POLICY,
        }:
            restored = self.allowed.restore(
                result.undo.payload, backup_root=backup_root,
            )
            undone = RelationshipBatchResult(
                operation, restored.tag_ids, restored.manifest_path,
                RelationshipBatchUndo(operation, None),
            )
        elif operation == CREATE_CONTRARY_SET:
            relationship_id, prior = result.undo.payload
            current = next(
                item for item in self.workspace.effective
                if item.id == relationship_id
            )
            expected = relationship_fingerprint(current)
            if prior is None:
                manifest = self.workspace.reset_override(
                    relationship_id,
                    expected_fingerprint=expected,
                    backup_root=backup_root,
                    compatibility_tags=self.tags,
                )
            else:
                manifest = self.workspace.save_override(
                    prior,
                    expected_fingerprint=expected,
                    backup_root=backup_root,
                    compatibility_tags=self.tags,
                )
            undone = RelationshipBatchResult(
                operation, result.tag_ids, manifest,
                RelationshipBatchUndo(operation, None),
            )
        else:
            raise ValueError("This batch has no supported Undo action.")
        self._reload_services()
        return undone
