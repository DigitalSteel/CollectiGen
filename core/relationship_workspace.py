from __future__ import annotations

import copy
import json
import os
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from .automation_rule_conversion import RulePairAdmission, RuleConversionAdmissionError

from .knowledge_relationships import (
    ALLOWED_DIMENSION,
    ALLOWED_DIMENSION_STATE,
    ENTAILMENT,
    MODIFIER_VALUE,
    NO_ALLOWED_DIMENSIONS,
    SCOPED_CONSTRAINT,
    SPECIFICITY,
    KnowledgeRelationship,
    KnowledgeRelationshipRegistry,
)
from .tag_collection import normalize_tag
from .constraint_resolver import (
    constraint_pairs,
    owns_inline_constraint_projection,
)
from .relationship_store import (
    RelationshipStore,
    canonicalize_relationship_tag_endpoints,
    validate_relationships,
)
from .scoped_constraint_projection import (
    publish_scoped_constraint_storage_authority,
)
from .tag_database import TagDatabase


def relationship_fingerprint(relationship: KnowledgeRelationship) -> str:
    encoded = json.dumps(
        relationship.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RelationshipMigrationAssessment:
    legacy_total: int
    already_represented: int
    additions: tuple[KnowledgeRelationship, ...]
    issues: tuple[str, ...]
    counts_by_kind: tuple[tuple[str, int], ...]

    @property
    def can_apply(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class RelationshipWorkspaceEntry:
    relationship: KnowledgeRelationship
    authority: str
    migration_state: str
    compatibility_sources: tuple[str, ...] = ()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _semantic_key(relationship: KnowledgeRelationship):
    endpoints = tuple((item.kind, item.id) for item in relationship.endpoints)
    if not relationship.directed:
        endpoints = tuple(sorted(endpoints))
    return relationship.kind, endpoints, relationship.directed, relationship.scope


class _LegacyRepresentativeIndex:
    """Resolve compatibility rows without rescanning all typed authority."""

    def __init__(
        self, typed: Iterable[KnowledgeRelationship], *, legacy_kinds=None,
    ) -> None:
        needed_kinds=None if legacy_kinds is None else set(legacy_kinds)
        if needed_kinds is not None and ENTAILMENT in needed_kinds:
            needed_kinds.add(SPECIFICITY)
        self.by_id: dict[str, KnowledgeRelationship] = {}
        self.by_semantic_key: dict[tuple, KnowledgeRelationship] = {}
        self.allowed_by_source: dict[
            tuple[str, str], list[KnowledgeRelationship]
        ] = {}
        self.inline_constraints: list[
            tuple[KnowledgeRelationship, set[tuple[str, str]]]
        ] = []
        self.semantic_edges: dict[
            tuple[str, str, str], KnowledgeRelationship
        ] = {}
        self.modifier_values_by_source: dict[
            tuple[str, str], list[KnowledgeRelationship]
        ] = {}
        for relationship in typed:
            self.by_id.setdefault(relationship.id, relationship)
            # Exact/canonical IDs may cross kinds, including malformed old
            # records. Preserve those matches before limiting semantic indexes.
            if needed_kinds is not None and relationship.kind not in needed_kinds:
                continue
            self.by_semantic_key.setdefault(_semantic_key(relationship), relationship)
            if (
                relationship.kind == ALLOWED_DIMENSION
                and relationship.source_endpoint is not None
                and not relationship.disabled
            ):
                source_key = (
                    relationship.source_endpoint.kind,
                    relationship.source_endpoint.id,
                )
                self.allowed_by_source.setdefault(source_key, []).append(relationship)
            if owns_inline_constraint_projection(relationship):
                self.inline_constraints.append(
                    (relationship, constraint_pairs(relationship))
                )
            source = relationship.source_endpoint
            if (
                relationship.kind == MODIFIER_VALUE
                and source is not None
            ):
                self.modifier_values_by_source.setdefault(
                    (source.kind, source.id), []
                ).append(relationship)
            if (
                relationship.kind in {SPECIFICITY, ENTAILMENT}
                and relationship.directed
                and source is not None
                and source.kind == "tag"
            ):
                for endpoint in relationship.target_endpoints:
                    if endpoint.kind == "tag":
                        self.semantic_edges.setdefault(
                            (relationship.kind, source.id, endpoint.id), relationship
                        )

    def representative(
        self,
        legacy: KnowledgeRelationship,
    ) -> KnowledgeRelationship | None:
        canonical_id = dict(legacy.details).get("canonical_relationship_id", "")
        direct = self.by_id.get(legacy.id)
        if direct is not None:
            return direct
        if canonical_id:
            direct = self.by_id.get(canonical_id)
            if direct is not None:
                return direct
        if legacy.kind in {SPECIFICITY, ENTAILMENT} and legacy.directed:
            source = legacy.source_endpoint
            targets = tuple(
                endpoint
                for endpoint in legacy.target_endpoints
                if endpoint.kind == "tag"
            )
            if source is not None and source.kind == "tag" and len(targets) == 1:
                edge = self.semantic_edges.get(
                    (legacy.kind, source.id, targets[0].id)
                )
                if edge is not None:
                    return edge
                if legacy.kind == ENTAILMENT:
                    redundant = self.semantic_edges.get(
                        (SPECIFICITY, source.id, targets[0].id)
                    )
                    if redundant is not None:
                        return redundant
        semantic = self.by_semantic_key.get(_semantic_key(legacy))
        if semantic is not None:
            return semantic
        if (
            legacy.kind == ALLOWED_DIMENSION
            and legacy.source == "tag.relations.allowed_modifier_groups"
            and legacy.source_endpoint is not None
        ):
            source_key = (
                legacy.source_endpoint.kind,
                legacy.source_endpoint.id,
            )
            legacy_target = legacy.target_endpoints[0]
            for relationship in self.allowed_by_source.get(source_key, ()):
                typed_targets = relationship.target_endpoints
                if (
                    legacy_target.kind == ALLOWED_DIMENSION_STATE
                    and legacy_target.id == NO_ALLOWED_DIMENSIONS
                    and any(
                        target.kind == ALLOWED_DIMENSION_STATE
                        and target.id == NO_ALLOWED_DIMENSIONS
                        for target in typed_targets
                    )
                ):
                    return relationship
                if legacy_target.kind == "modifier_dimension":
                    canonical_family = dict(relationship.details).get(
                        "canonical_family_id", ""
                    )
                    if legacy_target.id == canonical_family or any(
                        target.kind == "modifier_dimension"
                        and target.id == legacy_target.id
                        for target in typed_targets
                    ):
                        return relationship
        if legacy.source == "tag.relations.conflicts":
            legacy_pairs = constraint_pairs(legacy)
            if legacy_pairs:
                for relationship, typed_pairs in self.inline_constraints:
                    if legacy_pairs.issubset(typed_pairs):
                        return relationship
        if (
            legacy.kind == MODIFIER_VALUE
            and legacy.source == "tag.relations.attributes"
            and legacy.source_endpoint is not None
            and len(legacy.target_endpoints) == 1
        ):
            legacy_target = legacy.target_endpoints[0]
            legacy_names = {
                normalize_tag(value)
                for value in (legacy_target.id, legacy_target.label)
                if normalize_tag(value)
            }
            matches = []
            source_key = (
                legacy.source_endpoint.kind,
                legacy.source_endpoint.id,
            )
            for relationship in self.modifier_values_by_source.get(source_key, ()):
                target_names = {
                    normalize_tag(value)
                    for endpoint in relationship.target_endpoints
                    for value in (endpoint.id, endpoint.label)
                    if normalize_tag(value)
                }
                if legacy_names.intersection(target_names):
                    matches.append(relationship)
            active_matches = [
                relationship
                for relationship in matches
                if not relationship.disabled
            ]
            if len(active_matches) == 1:
                return active_matches[0]
            if not active_matches and len(matches) == 1:
                return matches[0]
        return None


def _atomic_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".restore.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


class RelationshipWorkspaceService:
    """Safe default/user overlay used by the universal relationship workspace."""

    def __init__(
        self,
        default_path: str | Path,
        user_path: str | Path,
        *,
        tags: TagDatabase | None = None,
    ) -> None:
        self.tags = tags
        self._tag_path=Path(tags.path).absolute() if tags is not None else None
        self.rule_pair_admission=getattr(tags,"rule_pair_admission",None)
        if self.rule_pair_admission is not None and not self.rule_pair_admission.owns(tags.path,"knowledge"):
            raise ValueError("Relationship workspace must use its exact Knowledge participant.")
        self.default_store = RelationshipStore(default_path,cache_parsed=True)
        self.user_store = RelationshipStore(user_path)

    def _binding_for_write(self, compatibility_tags=None):
        binding=self.rule_pair_admission
        if self._tag_path is not None and self.tags is None:
            raise RuleConversionAdmissionError("Saved Knowledge changed. Reopen Relationships before saving.")
        if self.tags is not None and (
            Path(self.tags.path).absolute()!=self._tag_path
            or getattr(self.tags,"rule_pair_admission",None)!=binding
        ):
            raise RuleConversionAdmissionError("Saved Knowledge changed. Reopen Relationships before saving.")
        supplied=getattr(compatibility_tags,"rule_pair_admission",None)
        if compatibility_tags is not None:
            if self._tag_path is not None and Path(compatibility_tags.path).absolute()!=self._tag_path:
                raise RuleConversionAdmissionError("Relationship participants belong to different Knowledge profiles.")
            if supplied is not None and not supplied.owns(compatibility_tags.path,"knowledge"):
                raise RuleConversionAdmissionError("Relationship compatibility Knowledge binding changed.")
            if binding is not None and supplied is not None and binding!=supplied:
                raise RuleConversionAdmissionError("Relationship participants belong to different Knowledge profiles.")
        binding=binding or supplied
        if binding is not None and (
            (self.tags is not None and not binding.owns(self.tags.path,"knowledge"))
            or self.user_store.path.absolute()!=binding.root/"relationships.json"
        ):
            raise RuleConversionAdmissionError("Relationship destination does not belong to the bound Knowledge profile.")
        return binding

    def _admission(self, compatibility_tags=None):
        binding=self._binding_for_write(compatibility_tags)
        return binding.admit() if binding else nullcontext()

    @property
    def effective(self) -> tuple[KnowledgeRelationship, ...]:
        values = dict(self.default_store.relationships)
        values.update(self.user_store.relationships)
        relationships = tuple(values[key] for key in sorted(values))
        if self.tags is not None:
            relationships = canonicalize_relationship_tag_endpoints(
                relationships, self.tags,
            )
        return relationships

    def workspace_entries(
        self,
        legacy_registry: KnowledgeRelationshipRegistry | None = None,
        *, kinds: Iterable[str] = (),
    ) -> tuple[RelationshipWorkspaceEntry, ...]:
        """Return typed authority plus still-active compatibility records.

        A represented compatibility record remains disclosed on its canonical
        row so the workspace never implies that the old projection has already
        been retired. Unrepresented legacy records remain selectable review
        entries instead of disappearing from the universal workspace.
        """
        typed = self.effective
        allowed_kinds=set(kinds)
        legacy_relationships=(
            legacy_registry.relationships if legacy_registry is not None else ()
        )
        # Most typed records have no legacy projection. Allocate source sets
        # only for actual matches instead of one empty tracked set per rule.
        compatibility_by_id: dict[str, set[str]] = {}
        legacy_kinds={item.kind for item in legacy_relationships}
        if any(item.source=="tag.relations.conflicts" for item in legacy_relationships):
            legacy_kinds.add(SCOPED_CONSTRAINT)
        representative_index = _LegacyRepresentativeIndex(
            typed,legacy_kinds=legacy_kinds,
        )
        unrepresented: list[KnowledgeRelationship] = []
        for legacy in legacy_relationships:
            representative = representative_index.representative(legacy)
            if representative is None:
                unrepresented.append(legacy)
                continue
            compatibility_by_id.setdefault(representative.id, set()).add(legacy.source)

        entries = [
            RelationshipWorkspaceEntry(
                relationship,
                (
                    "personal"
                    if relationship.id in self.user_store.relationships
                    else "default"
                ),
                (
                    "compatibility_projection"
                    if compatibility_by_id.get(relationship.id)
                    else "canonical"
                ),
                tuple(sorted(compatibility_by_id.get(relationship.id, ()))),
            )
            for relationship in typed
            if not allowed_kinds or relationship.kind in allowed_kinds
        ]
        entries.extend(
            RelationshipWorkspaceEntry(
                relationship,
                "compatibility",
                "migration_required",
                (relationship.source,),
            )
            for relationship in unrepresented
            if not allowed_kinds or relationship.kind in allowed_kinds
        )
        return tuple(
            sorted(
                entries,
                key=lambda entry: (
                    entry.relationship.kind,
                    entry.relationship.label.casefold(),
                    entry.relationship.id,
                ),
            )
        )

    def registry(self, *, tags=None) -> KnowledgeRelationshipRegistry:
        return KnowledgeRelationshipRegistry(self.effective, tags=tags)

    def save_override(
        self,
        relationship: KnowledgeRelationship,
        *,
        expected_fingerprint: str | None = None,
        backup_root: str | Path | None = None,
        compatibility_tags: TagDatabase | None = None,
        legacy_relationship: KnowledgeRelationship | None = None,
    ) -> Path:
        with self._admission(compatibility_tags):
            return self._save_override_admitted(relationship,
                expected_fingerprint=expected_fingerprint,backup_root=backup_root,
                compatibility_tags=compatibility_tags,legacy_relationship=legacy_relationship)

    def _save_override_admitted(
        self,relationship: KnowledgeRelationship, *,expected_fingerprint: str | None=None,
        backup_root: str | Path | None=None,compatibility_tags: TagDatabase | None=None,
        legacy_relationship: KnowledgeRelationship | None=None,
    ) -> Path:
        if relationship.kind == SCOPED_CONSTRAINT and compatibility_tags is not None:
            return self._save_constraint_override(
                relationship,
                compatibility_tags,
                expected_fingerprint=expected_fingerprint,
                backup_root=backup_root,
                legacy_relationship=legacy_relationship,
            )
        current = next(
            (item for item in self.effective if item.id == relationship.id),
            None,
        )
        current_fingerprint = (
            relationship_fingerprint(current) if current is not None else "missing"
        )
        if (
            expected_fingerprint is not None
            and current_fingerprint != expected_fingerprint
        ):
            raise RuntimeError(
                "This relationship changed after it was opened. Reload and review it before saving."
            )
        updated = replace(
            relationship,
            layer="user",
            source="user_review",
        )
        user_values = dict(self.user_store.relationships)
        user_values[updated.id] = updated
        effective = dict(self.default_store.relationships)
        effective.update(user_values)
        issues = validate_relationships(effective.values())
        if issues:
            raise ValueError(" ".join(issue.message for issue in issues[:3]))
        self.user_store.replace(user_values.values())
        try:
            manifest = self.user_store.save_verified(backup_root=backup_root)
        except Exception:
            self.user_store.load()
            raise
        self.user_store.load()
        return manifest

    def reset_override(
        self,
        relationship_id: str,
        *,
        expected_fingerprint: str | None = None,
        backup_root: str | Path | None = None,
        compatibility_tags: TagDatabase | None = None,
    ) -> Path:
        with self._admission(compatibility_tags):
            return self._reset_override_admitted(relationship_id,
                expected_fingerprint=expected_fingerprint,backup_root=backup_root,
                compatibility_tags=compatibility_tags)

    def _reset_override_admitted(
        self,relationship_id: str, *,expected_fingerprint: str | None=None,
        backup_root: str | Path | None=None,compatibility_tags: TagDatabase | None=None,
    ) -> Path:
        current = next(
            (item for item in self.effective if item.id == relationship_id),
            None,
        )
        if expected_fingerprint is not None and (
            current is None
            or relationship_fingerprint(current) != expected_fingerprint
        ):
            raise RuntimeError(
                "This relationship changed after it was opened. Reload and review it before resetting."
            )
        if relationship_id not in self.user_store.relationships:
            raise ValueError("This relationship has no personal override to reset.")
        if (
            current is not None
            and current.kind == SCOPED_CONSTRAINT
            and compatibility_tags is not None
        ):
            return self._reset_constraint_override(
                current,
                compatibility_tags,
                backup_root=backup_root,
            )
        user_values = dict(self.user_store.relationships)
        user_values.pop(relationship_id)
        self.user_store.replace(user_values.values())
        try:
            manifest = self.user_store.save_verified(backup_root=backup_root)
        except Exception:
            self.user_store.load()
            raise
        self.user_store.load()
        return manifest

    def _save_constraint_override(
        self,
        relationship: KnowledgeRelationship,
        tags: TagDatabase,
        *,
        expected_fingerprint: str | None,
        backup_root: str | Path | None,
        legacy_relationship: KnowledgeRelationship | None,
    ) -> Path:
        current = next(
            (item for item in self.effective if item.id == relationship.id),
            None,
        )
        if current is None:
            current = legacy_relationship
        if expected_fingerprint is not None and (
            current is None or relationship_fingerprint(current) != expected_fingerprint
        ):
            raise RuntimeError(
                "This relationship changed after it was opened. Reload and review it before saving."
            )
        if legacy_relationship is not None:
            fresh_legacy = KnowledgeRelationshipRegistry.from_legacy(
                tags, kinds=(SCOPED_CONSTRAINT,)
            )
            fresh = next(
                (
                    item
                    for item in fresh_legacy.relationships
                    if item.id == legacy_relationship.id
                ),
                None,
            )
            if fresh is None or relationship_fingerprint(fresh) != relationship_fingerprint(
                legacy_relationship
            ):
                raise RuntimeError(
                    "The compatibility relationship changed after it was opened. Reload and review it before saving."
                )
            if legacy_relationship.source != "tag.relations.conflicts":
                raise ValueError(
                    "This compatibility relationship remains read-only until its source migration is implemented."
                )

        details = dict(relationship.details)
        details["compatibility_projection"] = "tag.relations.conflicts"
        extras = copy.deepcopy(relationship.extras)
        if current is not None and "overridden_authority" not in extras:
            extras["overridden_authority"] = {
                "id": current.id,
                "version": current.version,
                "source": current.source,
                "layer": current.layer,
                "disabled": bool(current.disabled),
            }
        if legacy_relationship is not None:
            legacy_payload = {"relationship": legacy_relationship.to_dict()}
            if legacy_relationship.source == "tag.relations.conflicts":
                inline_owners = set()
                for first, second in constraint_pairs(legacy_relationship):
                    if first in tags.tags and second in tags.tags[first].conflicts:
                        inline_owners.add(first)
                    if second in tags.tags and first in tags.tags[second].conflicts:
                        inline_owners.add(second)
                legacy_payload["inline_owners"] = sorted(inline_owners)
            extras["legacy_compatibility"] = legacy_payload
            details["migrated_from"] = legacy_relationship.source
        updated = replace(
            relationship,
            layer="user",
            source="user_review",
            details=tuple((str(key), str(value)) for key, value in details.items()),
            extras=extras,
        )
        user_values = dict(self.user_store.relationships)
        user_values[updated.id] = updated
        effective = dict(self.default_store.relationships)
        effective.update(user_values)
        issues = validate_relationships(effective.values())
        if issues:
            raise ValueError(" ".join(issue.message for issue in issues[:3]))
        return self._save_constraint_transaction(
            user_values,
            tags,
            current=current,
            active_relationships=tuple(effective.values()),
            backup_root=backup_root,
        )

    def _reset_constraint_override(
        self,
        current: KnowledgeRelationship,
        tags: TagDatabase,
        *,
        backup_root: str | Path | None,
    ) -> Path:
        user_values = dict(self.user_store.relationships)
        user_values.pop(current.id)
        effective = dict(self.default_store.relationships)
        effective.update(user_values)
        return self._save_constraint_transaction(
            user_values,
            tags,
            current=current,
            active_relationships=tuple(effective.values()),
            backup_root=backup_root,
            restore_legacy=current.extras.get("legacy_compatibility"),
        )

    def _save_constraint_transaction(
        self,
        user_values: dict[str, KnowledgeRelationship],
        tags: TagDatabase,
        *,
        current: KnowledgeRelationship | None,
        active_relationships: tuple[KnowledgeRelationship, ...],
        backup_root: str | Path | None,
        restore_legacy=None,
    ) -> Path:
        relationship_path = self.user_store.path
        tag_path = tags.path
        relationship_bytes = (
            relationship_path.read_bytes() if relationship_path.exists() else None
        )
        tag_bytes = tag_path.read_bytes() if tag_path.exists() else None
        if relationship_bytes != self.user_store._loaded_bytes:
            raise RuntimeError(
                "Relationships changed after this workspace was opened. Reload and review before saving."
            )
        if tag_bytes != getattr(tags, "_loaded_bytes", None):
            raise RuntimeError(
                "System Knowledge changed after this workspace was opened. Reload and review before saving."
            )

        binding=self._binding_for_write(tags)
        candidate_tags = TagDatabase(tag_path,rule_pair_admission=binding)
        old_pairs = set()
        if current is not None and (
            owns_inline_constraint_projection(current)
            or current.source == "tag.relations.conflicts"
        ):
            old_pairs = constraint_pairs(current)
        for first, second in old_pairs:
            if first in candidate_tags.tags:
                candidate_tags.tags[first].conflicts = [
                    value for value in candidate_tags.tags[first].conflicts if value != second
                ]
            if second in candidate_tags.tags:
                candidate_tags.tags[second].conflicts = [
                    value for value in candidate_tags.tags[second].conflicts if value != first
                ]

        if isinstance(restore_legacy, dict):
            legacy_payload = restore_legacy.get("relationship", restore_legacy)
            legacy = KnowledgeRelationship.from_dict(legacy_payload)
            has_inline_owners = "inline_owners" in restore_legacy
            inline_owners = {
                str(value) for value in restore_legacy.get("inline_owners", ())
            }
            for first, second in constraint_pairs(legacy):
                if first in candidate_tags.tags and second in candidate_tags.tags:
                    if has_inline_owners:
                        if first in inline_owners:
                            candidate_tags.tags[first].conflicts = list(
                                dict.fromkeys((*candidate_tags.tags[first].conflicts, second))
                            )
                        if second in inline_owners:
                            candidate_tags.tags[second].conflicts = list(
                                dict.fromkeys((*candidate_tags.tags[second].conflicts, first))
                            )
                        continue
                    candidate_tags.tags[first].conflicts = list(
                        dict.fromkeys((*candidate_tags.tags[first].conflicts, second))
                    )
                    candidate_tags.tags[second].conflicts = list(
                        dict.fromkeys((*candidate_tags.tags[second].conflicts, first))
                    )

        for active in active_relationships:
            if (
                active.kind != SCOPED_CONSTRAINT
                or active.disabled
                or not active.reviewed
                or not owns_inline_constraint_projection(active)
            ):
                continue
            for first, second in constraint_pairs(active):
                if first not in candidate_tags.tags or second not in candidate_tags.tags:
                    raise ValueError(
                        "A Contrary Values relationship references an unavailable SDXL Tag."
                    )
        publish_scoped_constraint_storage_authority(
            candidate_tags, active_relationships,
        )
        from .tag_alias_relationships import publish_tag_alias_storage_authority
        publish_tag_alias_storage_authority(candidate_tags,active_relationships)
        candidate_tags._rebuild_lookup()

        original_user_values = dict(self.user_store.relationships)
        self.user_store.replace(user_values.values())
        relationship_payload = self.user_store.payload()
        self.user_store.replace(original_user_values.values())
        tag_payload = candidate_tags._payload()

        root = (
            Path(backup_root)
            if backup_root is not None
            else relationship_path.parent / "backups" / "relationship-constraint"
        )
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = root / f"{stamp}_{uuid4().hex[:8]}"
        backup.mkdir(parents=True, exist_ok=False)
        entries = []
        for index, (path, payload) in enumerate(
            ((relationship_path, relationship_bytes), (tag_path, tag_bytes))
        ):
            name = f"{index}_{path.name}"
            if payload is not None:
                (backup / name).write_bytes(payload)
            entries.append(
                {
                    "target": str(path.resolve()),
                    "backup": name,
                    "existed": payload is not None,
                }
            )
        manifest_path = backup / "recovery.json"
        manifest = {
            "schema_version": 1,
            "status": "pending",
            "created_at": _utc_now(),
            "relationship_id": current.id if current is not None else "",
            "files": entries,
        }
        _atomic_json(manifest_path, manifest)
        try:
            _atomic_json(relationship_path, relationship_payload)
            _atomic_json(tag_path, tag_payload)
            saved_relationships = RelationshipStore(relationship_path)
            saved_tags = TagDatabase(tag_path,rule_pair_admission=binding)
            if saved_relationships.payload() != relationship_payload:
                raise RuntimeError(
                    "Relationship save failed semantic read-back verification."
                )
            if saved_tags._payload() != tag_payload:
                raise RuntimeError(
                    "Constraint compatibility projection failed semantic read-back verification."
                )
        except BaseException:
            for entry in entries:
                target = Path(entry["target"])
                saved = backup / entry["backup"]
                if entry["existed"]:
                    _atomic_bytes(target, saved.read_bytes())
                elif target.exists():
                    target.unlink()
            self.default_store.load()
            self.user_store.load()
            tags.load()
            manifest["status"] = "restored"
            manifest["restored_at"] = _utc_now()
            _atomic_json(manifest_path, manifest)
            raise
        manifest["status"] = "complete"
        manifest["completed_at"] = _utc_now()
        _atomic_json(manifest_path, manifest)
        self.user_store.load()
        tags.load()
        return manifest_path

    @classmethod
    def recover_interrupted_constraint_transactions(
        cls,
        data_root: str | Path,
    ) -> tuple[Path, ...]:
        with RulePairAdmission(Path(data_root).absolute()).admit():
            return cls._recover_interrupted_constraint_transactions_admitted(data_root)

    @classmethod
    def _recover_interrupted_constraint_transactions_admitted(cls,data_root: str | Path) -> tuple[Path, ...]:
        root = Path(data_root).resolve()
        backup_root = root / "backups" / "relationship-constraint"
        restored = []
        if not backup_root.exists():
            return ()
        allowed = {"relationships.json", "tags.json"}
        for manifest_path in sorted(backup_root.glob("*/recovery.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("status") != "pending":
                continue
            entries = []
            seen_targets = set()
            for raw_entry in manifest.get("files", ()):
                entry = dict(raw_entry)
                target = Path(str(entry.get("target") or "")).resolve()
                backup = (manifest_path.parent / str(entry.get("backup") or "")).resolve()
                if target.parent != root or target.name not in allowed:
                    raise RuntimeError(
                        "Relationship recovery manifest contains an unsafe target."
                    )
                if backup.parent != manifest_path.parent.resolve():
                    raise RuntimeError(
                        "Relationship recovery manifest contains an unsafe backup path."
                    )
                if target in seen_targets:
                    raise RuntimeError(
                        "Relationship recovery manifest repeats a target."
                    )
                if entry.get("existed") and not backup.is_file():
                    raise RuntimeError(
                        "Relationship recovery backup is missing."
                    )
                seen_targets.add(target)
                entries.append((entry, target, backup))
            for entry, target, backup in entries:
                if entry.get("existed"):
                    _atomic_bytes(target, backup.read_bytes())
                elif target.exists():
                    target.unlink()
            manifest["status"] = "restored"
            manifest["restored_at"] = _utc_now()
            _atomic_json(manifest_path, manifest)
            restored.append(manifest_path)
        return tuple(restored)

    def assess_legacy_migration(
        self,
        registry: KnowledgeRelationshipRegistry,
    ) -> RelationshipMigrationAssessment:
        effective = self.effective
        representative_index = _LegacyRepresentativeIndex(effective)
        additions = []
        represented = 0
        counts: dict[str, int] = {}
        for relationship in registry.relationships:
            counts[relationship.kind] = counts.get(relationship.kind, 0) + 1
            if representative_index.representative(relationship) is not None:
                represented += 1
                continue
            additions.append(replace(relationship, layer="user"))
        issues = tuple(
            issue.message for issue in validate_relationships(additions)
        )
        return RelationshipMigrationAssessment(
            legacy_total=len(registry.relationships),
            already_represented=represented,
            additions=tuple(additions),
            issues=issues,
            counts_by_kind=tuple(sorted(counts.items())),
        )


def effective_constraint_tag_ids(
    tags: TagDatabase,
    tag_id: str,
    *,
    default_path: str | Path,
    user_path: str | Path,
) -> tuple[str, ...]:
    """Resolve symmetric Contrary Values through typed authority plus legacy projection."""
    stable_id = str(tag_id)
    if stable_id not in tags.tags:
        return ()
    values = set(tags.legacy_conflict_ids(stable_id))
    service = RelationshipWorkspaceService(default_path, user_path, tags=tags)
    for relationship in service.effective:
        if relationship.kind != SCOPED_CONSTRAINT or relationship.directed:
            continue
        tag_ids = {
            endpoint.id for endpoint in relationship.endpoints if endpoint.kind == "tag"
        }
        if stable_id not in tag_ids:
            continue
        related = tag_ids - {stable_id}
        if owns_inline_constraint_projection(relationship) or relationship.id.startswith(
            "legacy.scoped_constraint."
        ):
            values.difference_update(related)
        if relationship.reviewed and not relationship.disabled:
            values.update(related)
    return tuple(
        sorted(
            (value for value in values if value in tags.tags),
            key=lambda value: (tags.tags[value].display.casefold(), value),
        )
    )
