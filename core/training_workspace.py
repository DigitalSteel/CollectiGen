"""Read-only summaries of saved training collections and plan drafts.

The catalog grants no training or source-read readiness. Collection rules are
only reported here; Dataset owns their refresh at a collection handoff.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from .concept_groups import ConceptGroupError, ConceptGroupStore, group_fingerprint
from .model_resource_registry import ModelResourceError
from .training_plan_drafts import TrainingPlanDraft, TrainingPlanDraftStore


class TrainingWorkspaceError(RuntimeError):
    """A selected saved record cannot be opened at its displayed revision."""


@dataclass(frozen=True)
class TrainingWorkspaceRow:
    key: str
    name: str
    detail: str
    fingerprint: str
    member_count: int | None = None


@dataclass(frozen=True)
class TrainingWorkspacePage:
    rows: tuple[TrainingWorkspaceRow, ...]
    total: int
    offset: int
    next_offset: int | None
    issues: tuple[str, ...]


class TrainingWorkspace:
    """Reopen canonical stores for each read so row identity is never cached."""

    def __init__(self, *, concept_store_path: str | Path, draft_store_path: str | Path):
        self.concept_store_path = Path(concept_store_path)
        self.draft_store_path = Path(draft_store_path)

    def page(
        self, kind: str = "collections", offset: int = 0, limit: int = 100,
        query: str = "", related_package_manifest: dict | None = None,
    ) -> TrainingWorkspacePage:
        if kind not in {"collections", "drafts"}:
            raise ValueError("Choose collections or drafts.")
        if type(offset) is not int or offset < 0:
            raise ValueError("Page offset must be a nonnegative integer.")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Page limit must be between 1 and 100.")
        if not isinstance(query, str):
            raise ValueError("Search query must be text.")
        if related_package_manifest is not None and kind != "drafts":
            raise ValueError("Package plan matching requires saved drafts.")

        if kind == "collections":
            try:
                groups, _revision = ConceptGroupStore(self.concept_store_path).snapshot()
            except (ConceptGroupError, ModelResourceError, OSError, ValueError) as exc:
                rows = ()
                issues = (f"Saved training collections are unavailable: {exc}",)
            else:
                rows = tuple(
                    TrainingWorkspaceRow(
                        key=key,
                        name=group["name"],
                        detail=self._collection_detail(group),
                        fingerprint=group_fingerprint(group),
                        member_count=len(group["members"]),
                    )
                    for key, group in groups["groups"].items()
                )
                issues = ()
        else:
            try:
                store = TrainingPlanDraftStore(self.draft_store_path)
            except OSError as exc:
                rows = ()
                issues = (f"Saved training drafts are unavailable: {exc}",)
            else:
                rows = tuple(
                    self._draft_row(draft) for draft in store.all_drafts()
                    if related_package_manifest is None or self._related_to_package(
                        draft.plan_manifest, related_package_manifest,
                    )
                )
                issues = tuple(issue.message for issue in store.issues)

        needle = query.strip().casefold()
        if needle:
            rows = tuple(row for row in rows if needle in row.name.casefold() or needle in row.detail.casefold())
        rows = tuple(sorted(rows, key=lambda row: (row.name.casefold(), row.key)))
        total = len(rows)
        return TrainingWorkspacePage(
            rows[offset:offset + limit], total, offset,
            offset + limit if offset + limit < total else None, issues,
        )

    @staticmethod
    def _related_to_package(saved: dict, staged: dict) -> bool:
        """Find likely source drafts; only live core recovery proves exact equivalence."""
        if not isinstance(staged, dict):
            return False
        saved_scope = saved.get("selection_scope")
        staged_scope = staged.get("selection_scope")
        if (
            saved.get("kind") != "collectigen_training_candidate_plan"
            or staged.get("kind") != saved.get("kind")
            or not isinstance(saved.get("schema_version"), int)
            or saved.get("schema_version") != staged.get("schema_version")
            or not isinstance(saved_scope, dict) or not isinstance(staged_scope, dict)
            or any(
                not isinstance(manifest.get(key), dict)
                for manifest in (saved, staged)
                for key in ("goal", "profile", "training_configuration", "permission_review")
            )
            or any(
                not isinstance(manifest.get("candidates"), list)
                for manifest in (saved, staged)
            )
        ):
            return False
        selection = saved_scope.get("frozen_selection")
        if (
            not isinstance(selection, list) or not selection
            or any(
                not isinstance(item, dict)
                or item.get("kind") != "library_image"
                or not item.get("library_id")
                or not item.get("relative_image_path")
                for item in selection
            )
        ):
            return False
        def explicit_overrides(manifest):
            return {
                item.get("stable_id"): (
                    item.get("decision"), item.get("role"), item.get("decision_reason"),
                )
                for item in manifest.get("candidates", ())
                if isinstance(item, dict) and item.get("user_override")
            }
        def permission_intent(manifest):
            value = manifest.get("permission_review") or {}
            return tuple(value.get(key) for key in (
                "status", "reason", "unknown_evidence_acknowledged", "policy_version",
            )) if isinstance(value, dict) else ()
        return bool(
            selection == staged_scope.get("frozen_selection")
            and saved.get("goal") == staged.get("goal")
            and saved.get("profile") == staged.get("profile")
            and saved.get("training_configuration") == staged.get("training_configuration")
            and explicit_overrides(saved) == explicit_overrides(staged)
            and permission_intent(saved) == permission_intent(staged)
        )

    @staticmethod
    def _collection_detail(group: dict) -> str:
        count = len(group["members"])
        parts = [f"{count:,} saved image{'s' if count != 1 else ''}"]
        if group.get("rules") is not None:
            parts.append("Dynamic rules; refresh in Dataset before handoff")
        description = group["description"].strip()
        if description:
            parts.append(description)
        return " · ".join(parts)

    @staticmethod
    def _draft_row(draft: TrainingPlanDraft) -> TrainingWorkspaceRow:
        goal = draft.goal
        target = " ".join(part for part in (goal.target_model_family, goal.target_model_version) if part)
        parts = [goal.text]
        if target:
            parts.append(target)
        parts.append(f"Updated {draft.updated_at}")
        return TrainingWorkspaceRow(draft.draft_id, draft.name, " · ".join(parts), draft.fingerprint)

    def resolve_collection(self, key: str, expected_fingerprint: str) -> dict:
        try:
            groups, _revision = ConceptGroupStore(self.concept_store_path).snapshot()
        except (ConceptGroupError, ModelResourceError, OSError, ValueError) as exc:
            raise TrainingWorkspaceError(f"Saved training collections are unavailable: {exc}") from exc
        group = groups["groups"].get(key)
        if group is None or group_fingerprint(group) != expected_fingerprint:
            raise TrainingWorkspaceError("This collection changed or disappeared. Reload the list.")
        return deepcopy(group)

    def resolve_draft(self, key: str, expected_fingerprint: str) -> TrainingPlanDraft:
        try:
            store = TrainingPlanDraftStore(self.draft_store_path)
        except OSError as exc:
            raise TrainingWorkspaceError(f"Saved training drafts are unavailable: {exc}") from exc
        if store.load_error:
            raise TrainingWorkspaceError(store.load_error)
        draft = store.drafts_by_id.get(key)
        if draft is None or draft.fingerprint != expected_fingerprint:
            raise TrainingWorkspaceError("This draft changed or disappeared. Reload the list.")
        return deepcopy(draft)
