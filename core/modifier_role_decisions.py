from __future__ import annotations

import copy
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping

from .knowledge_relationships import (
    MODIFIER_ROLE,
    MODIFIER_ROLE_STATE,
    ORDINARY_MODIFIER_ROLE,
    KnowledgeRelationship,
    RelationshipEndpoint,
)
from .relationship_workspace import (
    RelationshipWorkspaceService,
    relationship_fingerprint,
)


RETAIN_MODIFIER_ROLE = "retain_modifier_role"
CHANGE_MODIFIER_FAMILY = "change_family"
KEEP_ORDINARY = "keep_ordinary"
MODIFIER_ROLE_DECISIONS = (
    RETAIN_MODIFIER_ROLE,
    CHANGE_MODIFIER_FAMILY,
    KEEP_ORDINARY,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _file_fingerprint(path: str | Path) -> str:
    target = Path(path)
    return sha256(target.read_bytes()).hexdigest() if target.exists() else "missing"


def modifier_role_relationship_id(tag_id: str) -> str:
    digest = sha256(str(tag_id).encode("utf-8")).hexdigest()[:20]
    return f"modifier_role.{digest}"


@dataclass(frozen=True)
class ModifierRoleResolution:
    tag_id: str
    decision: str
    family_id: str
    relationship_id: str
    candidate_fingerprint: str
    reviewed_at: str
    record_fingerprint: str
    layer: str
    source: str

    @property
    def is_ordinary(self) -> bool:
        return self.decision == KEEP_ORDINARY


@dataclass(frozen=True)
class ModifierRoleDecisionPreview:
    tag_id: str
    action: str
    family_id: str
    candidate_fingerprint: str
    expected_relationship_fingerprint: str
    relationship: KnowledgeRelationship
    before_label: str
    after_label: str
    source_fingerprints: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ModifierRoleDecisionResult:
    relationship_id: str
    action: str
    family_id: str
    manifest_path: Path


def modifier_role_resolutions(
    relationships: Iterable[KnowledgeRelationship],
    *,
    categories: Any = None,
) -> dict[str, ModifierRoleResolution]:
    """Resolve one reviewed per-Tag role without interpreting lexical evidence."""
    family_ids = (
        set(categories.reusable_modifier_family_ids())
        if categories is not None else None
    )
    result: dict[str, ModifierRoleResolution] = {}
    for relationship in sorted(relationships, key=lambda item: item.id):
        if relationship.kind != MODIFIER_ROLE or relationship.disabled:
            continue
        if not relationship.reviewed or not relationship.directed or len(relationship.endpoints) != 2:
            raise ValueError(
                f"Modifier Role relationship {relationship.id!r} is not one reviewed directed decision."
            )
        source, target = relationship.endpoints
        if source.kind != "tag":
            raise ValueError(
                f"Modifier Role relationship {relationship.id!r} does not start from a Tag."
            )
        details = dict(relationship.details)
        decision = details.get("decision", "")
        family_id = ""
        if target.kind == "category":
            if decision not in {RETAIN_MODIFIER_ROLE, CHANGE_MODIFIER_FAMILY}:
                raise ValueError(
                    f"Modifier Role relationship {relationship.id!r} has an incompatible family decision."
                )
            family_id = target.id
            if family_ids is not None and family_id not in family_ids:
                raise ValueError(
                    f"Modifier Role relationship {relationship.id!r} references a Category that is not a reusable modifier family."
                )
        elif (
            target.kind == MODIFIER_ROLE_STATE
            and target.id == ORDINARY_MODIFIER_ROLE
            and decision == KEEP_ORDINARY
        ):
            pass
        else:
            raise ValueError(
                f"Modifier Role relationship {relationship.id!r} has an unsupported decision target."
            )
        if source.id in result:
            raise ValueError(f"Tag {source.id!r} has more than one Modifier Role decision.")
        result[source.id] = ModifierRoleResolution(
            tag_id=source.id,
            decision=decision,
            family_id=family_id,
            relationship_id=relationship.id,
            candidate_fingerprint=details.get("legacy_candidate_fingerprint", ""),
            reviewed_at=details.get("reviewed_at", ""),
            record_fingerprint=relationship_fingerprint(relationship),
            layer=relationship.layer,
            source=relationship.source,
        )
    return result


class ModifierRoleDecisionService:
    """Preview and save one stale-guarded legacy modifier-role review."""

    def __init__(
        self,
        tags: Any,
        categories: Any,
        foundation: Any,
        *,
        default_path: str | Path | None = None,
        user_path: str | Path | None = None,
        workspace: RelationshipWorkspaceService | None = None,
    ) -> None:
        self.tags = tags
        self.categories = categories
        self.foundation = foundation
        if workspace is None:
            if default_path is None or user_path is None:
                raise ValueError("Modifier Role decisions need both relationship store paths.")
            workspace = RelationshipWorkspaceService(
                default_path, user_path, tags=tags,
            )
        self.workspace = workspace

    def resolutions(self) -> dict[str, ModifierRoleResolution]:
        return modifier_role_resolutions(
            self.workspace.effective,
            categories=self.categories,
        )

    def inventory(self):
        return self.foundation.legacy_modifier_inventory(
            self.tags,
            self.categories,
            modifier_roles=self.resolutions(),
        )

    def family_choices(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (
                family_id,
                self.categories.path_label(family_id),
            )
            for family_id in self.categories.reusable_modifier_family_ids()
        )

    def _source_fingerprints(self) -> tuple[tuple[str, str], ...]:
        paths = (
            self.tags.path,
            self.categories.default_path,
            self.categories.user_path,
            self.foundation.modifiers_path,
            self.workspace.default_store.path,
            self.workspace.user_store.path,
        )
        unique: dict[str, Path] = {}
        for path in paths:
            target = Path(path).resolve()
            unique[str(target)] = target
        return tuple(
            (value, _file_fingerprint(path))
            for value, path in sorted(unique.items())
        )

    @staticmethod
    def _evidence(candidate) -> list[dict[str, Any]]:
        return [
            {
                "legacy_value_id": match.value_id,
                "display": match.display,
                "group": match.group,
                "sources": list(match.sources),
            }
            for match in candidate.matches
        ]

    def _before_label(
        self,
        candidate,
        resolution: ModifierRoleResolution | None,
    ) -> str:
        if resolution is not None:
            if resolution.is_ordinary:
                return "Reviewed ordinary Tag"
            return self.categories.path_label(resolution.family_id)
        if candidate.current_family_id:
            return candidate.current_family_label or candidate.current_family_id
        return "Ordinary Tag with no reviewed modifier role"

    def preview(
        self,
        tag_id: str,
        candidate_fingerprint: str,
        action: str,
        *,
        family_id: str = "",
    ) -> ModifierRoleDecisionPreview:
        if action not in MODIFIER_ROLE_DECISIONS:
            raise ValueError("Choose Retain Modifier Role, Change Family, or Keep Ordinary.")
        inventory = self.inventory()
        candidate = next(
            (item for item in inventory.candidates if item.tag_id == str(tag_id)),
            None,
        )
        if candidate is None or candidate.fingerprint != str(candidate_fingerprint):
            raise RuntimeError(
                "This legacy modifier candidate changed after it was opened. Reload and review it again."
            )
        family_ids = set(self.categories.reusable_modifier_family_ids())
        selected_family = str(family_id or "")
        if action == KEEP_ORDINARY:
            selected_family = ""
        elif selected_family not in family_ids:
            raise ValueError("Choose an available reusable modifier family for this decision.")

        resolutions = self.resolutions()
        current_resolution = resolutions.get(candidate.tag_id)
        relationship_id = (
            current_resolution.relationship_id
            if current_resolution is not None
            else modifier_role_relationship_id(candidate.tag_id)
        )
        current = next(
            (item for item in self.workspace.effective if item.id == relationship_id),
            None,
        )
        if current is not None and current.kind != MODIFIER_ROLE:
            raise ValueError(
                "The reserved Modifier Role relationship ID is already used by "
                "another relationship. Review that record before saving."
            )
        expected = relationship_fingerprint(current) if current is not None else "missing"
        reviewed_at = _utc_now()
        details = dict(current.details) if current is not None else {}
        details.update(
            {
                "decision": action,
                "legacy_candidate_fingerprint": candidate.fingerprint,
                "reviewed_at": reviewed_at,
            }
        )
        extras = copy.deepcopy(current.extras) if current is not None else {}
        history = [
            dict(item)
            for item in extras.get("modifier_role_history", ())
            if isinstance(item, Mapping)
        ]
        if current_resolution is not None:
            history.append(
                {
                    "changed_at": reviewed_at,
                    "decision": current_resolution.decision,
                    "family_id": current_resolution.family_id,
                    "candidate_fingerprint": current_resolution.candidate_fingerprint,
                    "relationship_fingerprint": current_resolution.record_fingerprint,
                    "reviewed_at": current_resolution.reviewed_at,
                    "layer": current_resolution.layer,
                    "source": current_resolution.source,
                }
            )
        if history:
            extras["modifier_role_history"] = history
        extras["legacy_modifier_review"] = {
            "candidate_fingerprint": candidate.fingerprint,
            "legacy_evidence": self._evidence(candidate),
            "category_id": candidate.category_id,
            "category_family_id": candidate.current_family_id,
            "suggested_family_ids": list(candidate.suggested_family_ids),
            "ambiguity_reasons": list(candidate.ambiguity_reasons),
        }
        tag_endpoint = RelationshipEndpoint(
            "tag", candidate.tag_id, candidate.display,
        )
        if action == KEEP_ORDINARY:
            target_endpoint = RelationshipEndpoint(
                MODIFIER_ROLE_STATE,
                ORDINARY_MODIFIER_ROLE,
                "Ordinary Tag",
            )
            after_label = "Ordinary Tag"
        else:
            after_label = self.categories.path_label(selected_family)
            target_endpoint = RelationshipEndpoint(
                "category", selected_family, after_label,
            )
        relationship = KnowledgeRelationship(
            id=relationship_id,
            kind=MODIFIER_ROLE,
            endpoints=(tag_endpoint, target_endpoint),
            directed=True,
            scope="system_knowledge",
            label=f"Modifier role for {candidate.display}",
            reviewed=True,
            source="user_review",
            details=tuple((str(key), str(value)) for key, value in details.items()),
            version=current.version if current is not None else 1,
            layer="user",
            disabled=False,
            extras=extras,
        )
        return ModifierRoleDecisionPreview(
            tag_id=candidate.tag_id,
            action=action,
            family_id=selected_family,
            candidate_fingerprint=candidate.fingerprint,
            expected_relationship_fingerprint=expected,
            relationship=relationship,
            before_label=self._before_label(candidate, current_resolution),
            after_label=after_label,
            source_fingerprints=self._source_fingerprints(),
        )

    def apply(
        self,
        preview: ModifierRoleDecisionPreview,
        *,
        backup_root: str | Path | None = None,
    ) -> ModifierRoleDecisionResult:
        for source_path, expected in preview.source_fingerprints:
            if _file_fingerprint(source_path) != expected:
                raise RuntimeError(
                    "System Knowledge changed after this decision was previewed. Reload and review it again."
                )
        inventory = self.inventory()
        candidate = next(
            (item for item in inventory.candidates if item.tag_id == preview.tag_id),
            None,
        )
        if candidate is None or candidate.fingerprint != preview.candidate_fingerprint:
            raise RuntimeError(
                "This legacy modifier candidate changed after it was previewed. Reload and review it again."
            )
        if preview.family_id and preview.family_id not in set(
            self.categories.reusable_modifier_family_ids()
        ):
            raise RuntimeError(
                "The selected modifier family changed after preview. Reload and review it again."
            )
        manifest = self.workspace.save_override(
            preview.relationship,
            expected_fingerprint=preview.expected_relationship_fingerprint,
            backup_root=backup_root,
        )
        saved = self.resolutions().get(preview.tag_id)
        if (
            saved is None
            or saved.decision != preview.action
            or saved.family_id != preview.family_id
        ):
            raise RuntimeError("The Modifier Role decision failed semantic read-back verification.")
        return ModifierRoleDecisionResult(
            relationship_id=preview.relationship.id,
            action=preview.action,
            family_id=preview.family_id,
            manifest_path=manifest,
        )
