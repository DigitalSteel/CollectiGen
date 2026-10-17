from __future__ import annotations

import json
import shutil
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .character_database import CharacterDatabase
from .automation_rule_conversion import RulePairAdmission, RuleConversionAdmissionError
from .character_storage_transaction import character_transaction_lock
from .character_projection_storage import character_record_storage_payload
from .entity_tag_relationships import (
    RECORD_KIND_TO_ENDPOINT_KIND,
    EntityTagChange,
    stage_entity_tag_changes,
)
from .knowledge_relationships import KnowledgeRelationship
from .tag_collection import normalize_tag
from .tag_database import TagDatabase


BridgeEntityKind = Literal["character", "outfit", "series"]
BridgeStatus = Literal["linked", "ready", "missing", "ambiguous", "conflicting"]
BridgeMatchBasis = Literal["stored", "preferred_name", "declared_alias", "manual_review", "none", "multiple"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_bytes(path, (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


@dataclass(frozen=True)
class CharacterTagBridgeRow:
    """One read-only identity-record to Character Identity tag assessment."""

    entity_kind: BridgeEntityKind
    entity_id: str
    entity_name: str
    character_source_id: str | None
    related_character_ids: tuple[str, ...]
    status: BridgeStatus
    tag_ids: tuple[str, ...]
    match_basis: BridgeMatchBasis
    review_reason: str = ""
    excluded_tag_ids: tuple[str, ...] = ()

    @property
    def stable_id(self) -> str:
        return f"{self.entity_kind}:{self.entity_id}"


@dataclass(frozen=True)
class CharacterTagBridgeReport:
    """Aggregate bridge state; producing a report never changes either database."""

    rows: tuple[CharacterTagBridgeRow, ...]
    character_source_count: int

    @property
    def linked_character_count(self) -> int:
        return sum(row.entity_kind == "character" and row.status == "linked" for row in self.rows)

    @property
    def linked_outfit_count(self) -> int:
        return sum(row.entity_kind == "outfit" and row.status == "linked" for row in self.rows)

    @property
    def linked_source_count(self) -> int:
        return sum(row.entity_kind == "series" and row.status == "linked" for row in self.rows)

    @property
    def ready_count(self) -> int:
        return sum(row.status == "ready" for row in self.rows)

    @property
    def missing_count(self) -> int:
        return sum(row.status == "missing" for row in self.rows)

    @property
    def ambiguous_count(self) -> int:
        return sum(row.status == "ambiguous" for row in self.rows)

    @property
    def conflicting_count(self) -> int:
        return sum(row.status == "conflicting" for row in self.rows)

    @property
    def needs_review_count(self) -> int:
        return self.missing_count + self.ambiguous_count + self.conflicting_count

    def row(self, entity_kind: BridgeEntityKind, entity_id: str) -> CharacterTagBridgeRow | None:
        stable_id = f"{entity_kind}:{entity_id}"
        return next((row for row in self.rows if row.stable_id == stable_id), None)


@dataclass(frozen=True)
class CharacterTagBridgeLinkPlan:
    """One explicit stable-ID link plus any reviewed competing-candidate exclusions."""

    entity_kind: BridgeEntityKind
    entity_id: str
    entity_name: str
    tag_id: str
    tag_name: str
    excluded_competitors: tuple[CharacterTagBridgeRow, ...] = ()

    @property
    def stable_id(self) -> str:
        return f"{self.entity_kind}:{self.entity_id}"


@dataclass(frozen=True)
class CharacterTagSetupResult:
    status: str
    entity_kind: str = ""
    entity_id: str = ""
    message: str = ""

    @property
    def changed(self) -> bool:
        return self.status in {"linked", "created"}


@dataclass(frozen=True)
class CharacterTagSetupRequest:
    tag_id: str
    tag_path: Path
    tag_bytes: bytes
    data_dir: Path
    category_default: Path
    category_user: Path
    category_fingerprints: tuple
    rule_pair_admission: RulePairAdmission | None = None


def setup_character_tag_record(request: CharacterTagSetupRequest) -> CharacterTagSetupResult:
    """Background entry point: load detached authority and reject stale designation."""
    binding = request.rule_pair_admission
    if binding is not None and (
        not binding.owns(request.tag_path, "knowledge")
        or request.data_dir.absolute() != binding.root
    ):
        raise RuleConversionAdmissionError("Character setup belongs to a different Knowledge profile. Reopen the Tag.")
    with binding.admit() if binding is not None else nullcontext():
        return _setup_character_tag_record_admitted(request)


def _setup_character_tag_record_admitted(request: CharacterTagSetupRequest) -> CharacterTagSetupResult:
    from hashlib import sha256
    from .category_registry import CategoryRegistry
    from .tag_alias_relationships import publish_tag_alias_storage_authority

    def check_designation():
        if request.tag_path.read_bytes() != request.tag_bytes:
            raise RuntimeError("The Tag changed during record setup. Save or reopen it before retrying.")
        for path, expected in request.category_fingerprints:
            actual = sha256(path.read_bytes()).digest() if path.exists() else ()
            if actual != expected:
                raise RuntimeError("The Category changed during record setup. Reopen the Tag before retrying.")
    check_designation()
    tags = TagDatabase(request.tag_path, rule_pair_admission=request.rule_pair_admission)
    categories = CategoryRegistry(request.category_default, request.category_user)
    characters = CharacterDatabase(request.data_dir, recover_interrupted=False)
    relationships = {item.id: item for item in characters._default_relationships}
    relationships.update((item.id, item) for item in characters._user_relationships)
    publish_tag_alias_storage_authority(tags, relationships.values())
    bridge = CharacterTagBridge(tags, characters, categories)
    check_designation()
    return bridge.ensure_tag_record(request.tag_id, check_designation=check_designation)


class CharacterTagBridge:
    """Resolve safe exact links without flattening or modifying typed identity data."""

    CHARACTER_IDENTITY_CATEGORY = "character_identity"
    CHARACTER_SOURCE_CATEGORY = "copyright_series"
    CHARACTER_OUTFIT_CATEGORY = "character_outfit"

    def __init__(self, tags: TagDatabase, characters: CharacterDatabase, categories=None) -> None:
        self.tags = tags
        self.characters = characters
        self.categories = categories
        self.rule_pair_admission = getattr(tags, "rule_pair_admission", None)
        try:
            self._validate_binding()
        except RuleConversionAdmissionError as error:
            # Existing editor entry handles invalid participants as ValueError;
            # operation-time changes still use the typed recovery refusal.
            raise ValueError(str(error)) from error
        self._initial_files = self._file_snapshot()
        self._initial_tag_file = self.tags.path.read_bytes() if self.tags.path.exists() else None

    def _validate_binding(self) -> None:
        binding = self.rule_pair_admission
        if getattr(self.tags, "rule_pair_admission", None) != binding or (
            binding is not None and (
                not binding.owns(self.tags.path, "knowledge")
                or self.characters.data_dir.absolute() != binding.root
                or self.characters.relationship_path.absolute() != binding.root / "relationships.json"
            )
        ):
            raise RuleConversionAdmissionError("Character Knowledge profile changed. Reopen the Tag before saving its links.")

    def _admission(self):
        self._validate_binding()
        return self.rule_pair_admission.admit() if self.rule_pair_admission is not None else nullcontext()

    @classmethod
    def _role_for_category(cls, category_id: str) -> str:
        return (
            "character_source"
            if category_id == cls.CHARACTER_SOURCE_CATEGORY
            else "character_outfit"
            if category_id == cls.CHARACTER_OUTFIT_CATEGORY
            else "character_identity"
        )

    def _tag_has_role(self, tag, role: str) -> bool:
        if self.categories is not None:
            return self.categories.effective_semantic_role(tag.category) == role
        expected = (
            self.CHARACTER_SOURCE_CATEGORY
            if role == "character_source"
            else self.CHARACTER_OUTFIT_CATEGORY
            if role == "character_outfit"
            else self.CHARACTER_IDENTITY_CATEGORY
        )
        return tag.category == expected

    def category_ids_for_role(self, role: str) -> tuple[str, ...]:
        if self.categories is not None:
            return self.categories.category_ids_for_role(role)
        return ({
            "character_source": (self.CHARACTER_SOURCE_CATEGORY,),
            "character_identity": (self.CHARACTER_IDENTITY_CATEGORY,),
            "character_outfit": (self.CHARACTER_OUTFIT_CATEGORY,),
        }.get(role, ()))

    def creation_category_for_role(self, role: str) -> str:
        """Return the one semantic-role owner used for reviewed Tag creation."""
        role=str(role or "").strip().casefold()
        fallback={
            "character_source":self.CHARACTER_SOURCE_CATEGORY,
            "character_identity":self.CHARACTER_IDENTITY_CATEGORY,
            "character_outfit":self.CHARACTER_OUTFIT_CATEGORY,
        }.get(role,"")
        if self.categories is None:return fallback
        role_ids=self.category_ids_for_role(role)
        owners=tuple(
            category_id for category_id in role_ids
            if self.categories.semantic_role_source(category_id)==category_id
        )
        if len(owners)==1:return owners[0]
        return ""

    def _knowledge_paths(self) -> tuple[Path, ...]:
        return (
            self.characters.series_path,
            self.characters.characters_path,
            self.characters.outfits_path,
            self.characters.relationship_path,
        )

    def _file_snapshot(self) -> dict[Path, bytes | None]:
        return {path: path.read_bytes() if path.exists() else None for path in self._knowledge_paths()}

    def report(self) -> CharacterTagBridgeReport:
        indexes: dict[str, tuple[dict[str, set[str]], dict[str, set[str]], set[str]]] = {}
        for category_id in (
            self.CHARACTER_IDENTITY_CATEGORY,
            self.CHARACTER_SOURCE_CATEGORY,
            self.CHARACTER_OUTFIT_CATEGORY,
        ):
            role = self._role_for_category(category_id)
            display_index: dict[str, set[str]] = {}
            alias_index: dict[str, set[str]] = {}
            tag_ids: set[str] = set()
            for tag in self.tags.tags.values():
                if not self._tag_has_role(tag, role):
                    continue
                tag_ids.add(tag.id)
                display_key = normalize_tag(tag.display)
                if display_key:
                    display_index.setdefault(display_key, set()).add(tag.id)
                for alias in self.tags.effective_aliases(tag):
                    alias_key = normalize_tag(alias)
                    if alias_key:
                        alias_index.setdefault(alias_key, set()).add(tag.id)
            indexes[category_id] = display_index, alias_index, tag_ids

        rows: list[CharacterTagBridgeRow] = []
        source_display, source_aliases, source_tag_ids = indexes[self.CHARACTER_SOURCE_CATEGORY]
        characters_by_source: dict[str, list[str]] = {}
        for character in self.characters.characters.values():
            source_id = self.characters.character_source_id("character", character.id)
            if source_id:
                characters_by_source.setdefault(source_id, []).append(character.id)
        for source in self.characters.series.values():
            rows.append(self._assess(
                entity_kind="series",
                entity_id=source.id,
                entity_name=source.name,
                aliases=tuple(source.aliases),
                stored_link=self.characters.effective_knowledge_tag("series", source.id),
                character_source_id=source.id,
                related_character_ids=tuple(characters_by_source.get(source.id, ())),
                display_index=source_display,
                alias_index=source_aliases,
                eligible_tag_ids=source_tag_ids,
                relationship_label="Character Source",
            ))
        identity_display, identity_aliases, identity_tag_ids = indexes[self.CHARACTER_IDENTITY_CATEGORY]
        for character in self.characters.characters.values():
            rows.append(self._assess(
                entity_kind="character",
                entity_id=character.id,
                entity_name=character.name,
                aliases=tuple(character.aliases),
                stored_link=self.characters.effective_knowledge_tag("character", character.id),
                character_source_id=self.characters.character_source_id("character", character.id),
                related_character_ids=(),
                display_index=identity_display,
                alias_index=identity_aliases,
                eligible_tag_ids=identity_tag_ids,
                relationship_label="Character Identity",
            ))
        outfit_display, outfit_aliases, outfit_tag_ids = indexes[self.CHARACTER_OUTFIT_CATEGORY]
        for outfit in self.characters.outfits.values():
            rows.append(self._assess(
                entity_kind="outfit",
                entity_id=outfit.id,
                entity_name=outfit.name,
                aliases=tuple(outfit.aliases),
                stored_link=self.characters.effective_knowledge_tag("outfit", outfit.id),
                character_source_id=self.characters.character_source_id("outfit", outfit.id),
                related_character_ids=self.characters.associated_character_ids(outfit.id),
                display_index=outfit_display,
                alias_index=outfit_aliases,
                eligible_tag_ids=outfit_tag_ids,
                relationship_label="Character Outfit",
            ))

        claims: dict[str, list[int]] = {}
        for index, row in enumerate(rows):
            if row.status in {"linked", "ready"}:
                claims.setdefault(row.tag_ids[0], []).append(index)
        for tag_id, indexes in claims.items():
            if len(indexes) < 2:
                continue
            tag = self.tags.tags.get(tag_id)
            label = tag.display if tag is not None else tag_id
            reason = f'Multiple identity records claim the same SDXL Tag: {label}. Choose the intended record before linking.'
            for index in indexes:
                rows[index] = replace(
                    rows[index], status="conflicting", match_basis="multiple", review_reason=reason,
                )

        rows.sort(key=lambda row: (row.entity_kind, row.entity_name.casefold(), row.entity_id.casefold()))
        return CharacterTagBridgeReport(tuple(rows), len(self.characters.series))

    @staticmethod
    def _assess(
        *,
        entity_kind: BridgeEntityKind,
        entity_id: str,
        entity_name: str,
        aliases: tuple[str, ...],
        stored_link: dict[str, Any],
        character_source_id: str | None,
        related_character_ids: tuple[str, ...],
        display_index: dict[str, set[str]],
        alias_index: dict[str, set[str]],
        eligible_tag_ids: set[str],
        relationship_label: str,
    ) -> CharacterTagBridgeRow:
        stored_tag_id = str(stored_link.get("tag_id") or "").strip()
        excluded_tag_ids = tuple(dict.fromkeys(
            str(value).strip() for value in stored_link.get("excluded_tag_ids", ())
            if str(value).strip()
        )) if isinstance(stored_link.get("excluded_tag_ids", ()), (list, tuple)) else ()
        if stored_link:
            if stored_tag_id in eligible_tag_ids and stored_link.get("status") == "reviewed":
                return CharacterTagBridgeRow(
                    entity_kind, entity_id, entity_name, character_source_id, related_character_ids,
                    "linked", (stored_tag_id,), "stored",
                )
            if stored_link.get("status") != "reviewed_exclusions":
                return CharacterTagBridgeRow(
                    entity_kind, entity_id, entity_name, character_source_id, related_character_ids,
                    "conflicting", (stored_tag_id,) if stored_tag_id else (), "multiple",
                    f"The stored {relationship_label} SDXL Tag link is missing, no longer belongs to "
                    f"{relationship_label}, or is not reviewed.",
                )
        primary_key = normalize_tag(entity_name)
        alias_keys = {normalize_tag(value) for value in aliases if normalize_tag(value)}
        primary_display = set(display_index.get(primary_key, ()))
        primary_alias = set(alias_index.get(primary_key, ()))
        alias_matches: set[str] = set()
        for key in alias_keys:
            alias_matches.update(display_index.get(key, ()))
            alias_matches.update(alias_index.get(key, ()))
        candidates = (primary_display | primary_alias | alias_matches).difference(excluded_tag_ids)

        if not candidates:
            reason = (
                f"The exact candidate was reviewed as belonging to another typed record. "
                f"Choose another existing {relationship_label} SDXL Tag or create one explicitly."
                if excluded_tag_ids else
                f"No exact {relationship_label} SDXL Tag matches this preferred name or a declared alias."
            )
            return CharacterTagBridgeRow(
                entity_kind, entity_id, entity_name, character_source_id, related_character_ids,
                "missing", (), "none",
                reason, excluded_tag_ids,
            )
        if len(candidates) > 1:
            return CharacterTagBridgeRow(
                entity_kind, entity_id, entity_name, character_source_id, related_character_ids,
                "ambiguous", tuple(sorted(candidates)), "multiple",
                f"More than one {relationship_label} SDXL Tag matches the preferred name or declared aliases.",
                excluded_tag_ids,
            )
        tag_id = next(iter(candidates))
        basis: BridgeMatchBasis = "preferred_name" if tag_id in primary_display else "declared_alias"
        return CharacterTagBridgeRow(
            entity_kind, entity_id, entity_name, character_source_id, related_character_ids,
            "ready", (tag_id,), basis,
            excluded_tag_ids=excluded_tag_ids,
        )

    def reviewed_link_plan(
        self, entity_kind: BridgeEntityKind, entity_id: str, tag_id: str,
    ) -> CharacterTagBridgeLinkPlan:
        """Validate one explicit choice and disclose the unlinked records it resolves."""
        entity_kind = str(entity_kind).strip().lower()  # type: ignore[assignment]
        entity_id = str(entity_id).strip()
        tag_id = str(tag_id).strip()
        if entity_kind not in {"character", "outfit", "series"}:
            raise ValueError("Choose a character, outfit, or Character Source record.")
        records = self._records_for_kind(entity_kind)
        record = records.get(entity_id)
        if record is None:
            raise ValueError("The selected typed Character Knowledge record is no longer available.")
        tag = self.tags.tags.get(tag_id)
        expected_role = (
            "character_source"
            if entity_kind == "series"
            else "character_outfit"
            if entity_kind == "outfit"
            else "character_identity"
        )
        if tag is None or not self._tag_has_role(tag, expected_role):
            label = (
                "Character Source"
                if entity_kind == "series"
                else "Character Outfit"
                if entity_kind == "outfit"
                else "Character Identity"
            )
            raise ValueError(f"Choose an existing SDXL Tag in {label}.")
        stored_link = self.characters.effective_knowledge_tag(entity_kind, entity_id)
        stored_tag_id = str(stored_link.get("tag_id") or "")
        stored_tag=self.tags.tags.get(stored_tag_id)
        stored_link_is_valid=bool(
            stored_tag is not None and self._tag_has_role(stored_tag,expected_role)
        )
        if (
            stored_link.get("status") == "reviewed" and stored_tag_id
            and stored_tag_id != tag_id and stored_link_is_valid
        ):
            raise ValueError(
                "This typed identity record already has a different reviewed stable-ID link. "
                "A reviewed ownership transfer requires a separate migration preview."
            )
        report = self.report()
        competitors: list[CharacterTagBridgeRow] = []
        for row in report.rows:
            if row.stable_id == f"{entity_kind}:{entity_id}" or tag_id not in row.tag_ids:
                continue
            other_records = self._records_for_kind(row.entity_kind)
            other = other_records.get(row.entity_id)
            stored = (
                self.characters.effective_knowledge_tag(row.entity_kind, row.entity_id)
                if other is not None else {}
            )
            if stored.get("status") == "reviewed" and stored.get("tag_id") == tag_id:
                raise ValueError(
                    f'The SDXL Tag “{tag.display}” is already linked to {row.entity_kind} “{row.entity_name}”. '
                    "Review that stored link before transferring it."
                )
            competitors.append(row)
        competitors.sort(key=lambda row: (row.entity_kind, row.entity_name.casefold(), row.entity_id))
        if (
            stored_link.get("status") == "reviewed"
            and stored_link.get("tag_id") == tag_id
            and not competitors
        ):
            raise ValueError("This typed identity record already has that reviewed stable-ID link.")
        return CharacterTagBridgeLinkPlan(
            entity_kind, entity_id, record.name, tag.id, tag.display, tuple(competitors),
        )

    def _records_for_kind(self, entity_kind: BridgeEntityKind):
        if entity_kind == "character":
            return self.characters.characters
        if entity_kind == "outfit":
            return self.characters.outfits
        return self.characters.series

    @classmethod
    def recover_interrupted(cls, directory: str | Path) -> list[Path]:
        with RulePairAdmission(Path(directory).absolute()).admit():
            with character_transaction_lock(directory):
                return cls._recover_interrupted_locked(directory)

    @classmethod
    def _recover_interrupted_locked(cls, directory: str | Path) -> list[Path]:
        data_root = Path(directory).resolve()
        backup_root = data_root / "backups" / "character-tag-bridge"
        restored: list[Path] = []
        if not backup_root.exists():
            return restored
        for manifest_path in sorted(backup_root.glob("*/recovery.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("status") != "pending":
                continue
            for entry in manifest.get("files", []):
                target = Path(entry["target"]).resolve()
                backup = (manifest_path.parent / entry["backup"]).resolve()
                if target.parent != data_root or target.name not in {
                    "series.json", "characters.json", "outfits.json", "relationships.json",
                }:
                    raise RuntimeError("Character Tag Link recovery manifest contains an unsafe target.")
                if backup.parent != manifest_path.parent.resolve():
                    raise RuntimeError("Character Tag Link recovery manifest contains an unsafe backup path.")
                if entry.get("existed"):
                    _atomic_bytes(target, backup.read_bytes())
                elif target.exists():
                    target.unlink()
            manifest["status"] = "restored"
            manifest["restored_at"] = _now()
            _atomic_json(manifest_path, manifest)
            restored.append(manifest_path.parent)
        return restored

    def ensure_tag_record(self, tag_id: str, *, check_designation=None) -> CharacterTagSetupResult:
        """Set up one designated identity/source; uncertain ownership stays read-only."""
        with self._admission():
            return self._ensure_tag_record_admitted(tag_id, check_designation=check_designation)

    def _ensure_tag_record_admitted(self, tag_id: str, *, check_designation=None) -> CharacterTagSetupResult:
        from .character_record_edit import CharacterRecordEditService

        with character_transaction_lock(self.characters.data_dir):
            if check_designation is not None:check_designation()
            self._assert_fresh()
            tag = self.tags.tags.get(tag_id)
            kind = next((kind for role, kind in (
                ("character_identity", "character"), ("character_source", "series"),
            ) if tag is not None and self._tag_has_role(tag, role)), "")
            if not kind:
                return CharacterTagSetupResult("unsupported")
            report = self.report()
            linked = [row for row in report.rows if row.status == "linked" and tag_id in row.tag_ids]
            if len(linked) == 1 and linked[0].entity_kind == kind:
                row = linked[0]
                return CharacterTagSetupResult("existing", kind, row.entity_id, "Linked record ready to edit.")
            names = {normalize_tag(value) for value in self.tags.identity_names(tag)}
            matches = []
            for row in report.rows:
                record = self._records_for_kind(row.entity_kind)[row.entity_id]
                record_names = {normalize_tag(value) for value in (record.name, *record.aliases)}
                # Respect competing identities, incompatible record kinds and prior exclusions.
                if (tag_id in row.tag_ids or tag_id in row.excluded_tag_ids
                    or (row.entity_kind == kind and names.intersection(record_names))):
                    matches.append(row)
            if len(matches) == 1 and matches[0].entity_kind == kind and matches[0].status == "ready" and matches[0].tag_ids == (tag_id,):
                row = matches[0]
                if check_designation is not None:check_designation()
                self._apply_updates({(kind, row.entity_id): {
                    "tag_id": tag_id, "status": "reviewed", "basis": row.match_basis,
                    "reviewed_at": _now(), "source": "character_tag_automatic_setup",
                }})
                return CharacterTagSetupResult("linked", kind, row.entity_id, "Exact existing record linked automatically. You can edit it below.")
            if matches or linked:
                return CharacterTagSetupResult("review", kind, message="Existing names, links, or prior decisions need review. Choose the correct record; no association was changed.")
            service = CharacterRecordEditService(self.characters, self.tags, self.categories)
            if check_designation is not None:check_designation()
            try:
                if kind == "series":
                    result = service.create_series(tag_id, name=tag.display)
                else:
                    result = service.create_character(tag_id, name=tag.display, series_id=None, identity_type="unknown")
            except ValueError as error:
                return CharacterTagSetupResult("review", kind, message=str(error))
            self._initial_files = self._file_snapshot()
            return CharacterTagSetupResult("created", kind, result.record_id,
                "Linked record created automatically. Add or update its details below; unspecified facts remain unset.")

    def apply_safe_links(self) -> "CharacterTagBridgeApplyResult":
        with self._admission():
            return self._apply_safe_links_admitted()

    def _apply_safe_links_admitted(self) -> "CharacterTagBridgeApplyResult":
        report = self.report()
        ready = tuple(row for row in report.rows if row.status == "ready")
        if not ready:
            raise ValueError("There are no unambiguous Character Tag links ready to save.")
        reviewed_at = _now()
        updates = {
            (row.entity_kind, row.entity_id): {
                "tag_id": row.tag_ids[0],
                "status": "reviewed",
                "basis": row.match_basis,
                "reviewed_at": reviewed_at,
                "source": "character_tag_bridge",
            }
            for row in ready
        }
        return self._apply_updates(updates)

    def apply_reviewed_link(
        self, entity_kind: BridgeEntityKind, entity_id: str, tag_id: str,
    ) -> "CharacterTagBridgeApplyResult":
        """Persist one explicit link and durable exclusions for its unlinked competitors."""
        with self._admission():
            return self._apply_reviewed_link_admitted(entity_kind, entity_id, tag_id)

    def _apply_reviewed_link_admitted(
        self, entity_kind: BridgeEntityKind, entity_id: str, tag_id: str,
    ) -> "CharacterTagBridgeApplyResult":
        plan = self.reviewed_link_plan(entity_kind, entity_id, tag_id)
        reviewed_at = _now()
        updates: dict[tuple[BridgeEntityKind, str], dict[str, Any]] = {
            (plan.entity_kind, plan.entity_id): {
                "tag_id": plan.tag_id,
                "status": "reviewed",
                "basis": "manual_review",
                "reviewed_at": reviewed_at,
                "source": "character_tag_bridge",
            },
        }
        for row in plan.excluded_competitors:
            records = self._records_for_kind(row.entity_kind)
            record = records[row.entity_id]
            stored_exclusions = self.characters.effective_knowledge_tag(
                row.entity_kind, row.entity_id,
            ).get("excluded_tag_ids", ())
            if not isinstance(stored_exclusions, (list, tuple)):
                stored_exclusions = ()
            exclusions = list(dict.fromkeys((
                *(str(value) for value in stored_exclusions if str(value).strip()), plan.tag_id,
            )))
            updates[(row.entity_kind, row.entity_id)] = {
                "status": "reviewed_exclusions",
                "excluded_tag_ids": exclusions,
                "reviewed_at": reviewed_at,
                "source": "character_tag_bridge",
            }
        return self._apply_updates(updates)

    def _assert_fresh(self) -> None:
        if self._file_snapshot() != self._initial_files:
            raise RuntimeError(
                "Character Source, character, or outfit knowledge changed after this review opened. "
                "Reopen the review and check the current records before linking."
            )
        current_tag_file = self.tags.path.read_bytes() if self.tags.path.exists() else None
        if current_tag_file != self._initial_tag_file:
            raise RuntimeError(
                "SDXL Tag knowledge changed after this review opened. Reopen the review and check the current records before linking."
            )

    def _apply_updates(
        self, updates: dict[tuple[BridgeEntityKind, str], dict[str, Any]],
    ) -> "CharacterTagBridgeApplyResult":
        with character_transaction_lock(self.characters.data_dir):
            return self._apply_updates_locked(updates)

    def _apply_updates_locked(
        self, updates: dict[tuple[BridgeEntityKind, str], dict[str, Any]],
    ) -> "CharacterTagBridgeApplyResult":
        if not updates:
            raise ValueError("Choose at least one reviewed Character Tag relationship to save.")
        self._assert_fresh()

        canonical_changes: list[EntityTagChange] = []
        for (kind, entity_id), value in updates.items():
            if value.get("status") != "reviewed":
                continue
            record = self._records_for_kind(kind).get(entity_id)
            tag = self.tags.tags.get(str(value.get("tag_id") or ""))
            if record is None or tag is None:
                raise RuntimeError("A reviewed Character Tag relationship is no longer available.")
            canonical_changes.append(
                EntityTagChange(
                    entity_kind=RECORD_KIND_TO_ENDPOINT_KIND[kind],
                    entity_id=entity_id,
                    tag_id=tag.id,
                    entity_label=record.name,
                    tag_label=tag.display,
                    source=str(value.get("source") or "character_tag_bridge"),
                    basis=str(value.get("basis") or ""),
                    reviewed_at=str(value.get("reviewed_at") or ""),
                    compatibility_link=value,
                )
            )
        relationship_payload = None
        if canonical_changes:
            relationship_payload, _relationship_ids = stage_entity_tag_changes(
                self.characters.default_relationship_path,
                self.characters.relationship_path,
                canonical_changes,
            )

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = self.characters.data_dir / "backups" / "character-tag-bridge" / f"{stamp}_{uuid4().hex[:8]}"
        backup.mkdir(parents=True, exist_ok=False)
        entries: list[dict[str, Any]] = []
        for index, path in enumerate(self._knowledge_paths()):
            name = f"{index}_{path.name}"
            if path.exists():
                shutil.copy2(path, backup / name)
            entries.append({"target": str(path.resolve()), "backup": name, "existed": path.exists()})
        manifest_path = backup / "recovery.json"
        manifest: dict[str, Any] = {
            "status": "pending", "created_at": _now(), "files": entries,
            "source_links": sum(kind == "series" and value.get("status") == "reviewed" for (kind, _), value in updates.items()),
            "character_links": sum(kind == "character" and value.get("status") == "reviewed" for (kind, _), value in updates.items()),
            "outfit_links": sum(kind == "outfit" and value.get("status") == "reviewed" for (kind, _), value in updates.items()),
            "exclusion_decisions": sum(value.get("status") == "reviewed_exclusions" for value in updates.values()),
        }
        _atomic_json(manifest_path, manifest)

        by_kind = {
            "series": {entity_id: value for (kind, entity_id), value in updates.items() if kind == "series"},
            "character": {entity_id: value for (kind, entity_id), value in updates.items() if kind == "character"},
            "outfit": {entity_id: value for (kind, entity_id), value in updates.items() if kind == "outfit"},
        }
        try:
            staged_user_relationships = (
                tuple(
                    KnowledgeRelationship.from_dict(item)
                    for item in relationship_payload.get("relationships", ())
                    if isinstance(item, dict)
                )
                if relationship_payload is not None
                else tuple(self.characters._user_relationships)
            )
            for path, collection, kind, schema in (
                (self.characters.series_path, "series", "series", CharacterDatabase.SERIES_SCHEMA_VERSION),
                (self.characters.characters_path, "characters", "character", CharacterDatabase.CHARACTERS_SCHEMA_VERSION),
                (self.characters.outfits_path, "outfits", "outfit", CharacterDatabase.OUTFITS_SCHEMA_VERSION),
            ):
                if not by_kind[kind]:
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
                records = payload.get(collection)
                if not isinstance(records, list):
                    raise RuntimeError(f"{path.name} does not contain a valid {collection} collection.")
                pending = dict(by_kind[kind])
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    relationship = pending.pop(str(record.get("id") or ""), None)
                    if relationship is None:
                        continue
                    record["knowledge_tag"] = relationship
                    normalized = character_record_storage_payload(
                        kind,
                        record,
                        self.characters._default_relationships,
                        staged_user_relationships,
                        projection_fields=record.keys(),
                    )
                    record.clear()
                    record.update(normalized)
                if pending:
                    raise RuntimeError(f"One or more reviewed {kind} records no longer exist.")
                payload["schema_version"] = max(int(payload.get("schema_version", 0) or 0), schema)
                _atomic_json(path, payload)

            if relationship_payload is not None:
                _atomic_json(self.characters.relationship_path, relationship_payload)

            reloaded = CharacterDatabase(
                self.characters.data_dir, recover_interrupted=False,
            )
            for (kind, entity_id), expected in updates.items():
                if kind == "character":
                    records = reloaded.characters
                elif kind == "outfit":
                    records = reloaded.outfits
                else:
                    records = reloaded.series
                stored = records[entity_id].knowledge_tag
                if stored != expected:
                    raise RuntimeError("Saved Character Tag links failed read-back verification.")
                if (
                    expected.get("status") == "reviewed"
                    and reloaded.entity_tag_id(kind, entity_id)
                    != str(expected.get("tag_id") or "")
                ):
                    raise RuntimeError("Canonical Entity Tag links failed read-back verification.")
        except BaseException:
            for entry in entries:
                target = Path(entry["target"])
                saved = backup / entry["backup"]
                if entry["existed"]:
                    _atomic_bytes(target, saved.read_bytes())
                elif target.exists():
                    target.unlink()
            self.characters.load()
            manifest["status"] = "restored"
            manifest["restored_at"] = _now()
            _atomic_json(manifest_path, manifest)
            raise

        manifest["status"] = "complete"
        manifest["completed_at"] = _now()
        _atomic_json(manifest_path, manifest)
        self.characters.load()
        self._initial_files = self._file_snapshot()
        return CharacterTagBridgeApplyResult(
            source_links=manifest["source_links"], character_links=manifest["character_links"],
            outfit_links=manifest["outfit_links"],
            backup_directory=backup, exclusion_decisions=manifest["exclusion_decisions"],
        )


@dataclass(frozen=True)
class CharacterTagBridgeApplyResult:
    character_links: int
    outfit_links: int
    backup_directory: Path
    source_links: int = 0
    exclusion_decisions: int = 0

    @property
    def total_links(self) -> int:
        return self.source_links + self.character_links + self.outfit_links
