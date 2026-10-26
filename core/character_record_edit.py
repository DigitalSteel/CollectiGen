from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from .character_database import CharacterDatabase
from .character_projection_storage import (
    character_record_storage_payload,
)
from .character_relationships import (
    CharacterRelationshipChange,
    stage_character_relationship_changes,
)
from .character_storage_transaction import (
    atomic_bytes as _transaction_atomic_bytes,
    atomic_json as _transaction_atomic_json,
    commit_character_payloads,
    now as _transaction_now,
    recover_interrupted_character_transactions,
)
from .character_vocabulary import trailing_parenthetical_groups
from .entity_tag_relationships import (
    RECORD_KIND_TO_ENDPOINT_KIND,
    EntityTagChange,
    stage_entity_tag_changes,
)
from .knowledge_relationships import (
    ASSOCIATED_CHARACTER,
    CHARACTER_SOURCE,
    VERSION_OF,
    KnowledgeRelationship,
)
from .tag_collection import normalize_tag


CharacterRecordKind = Literal["series", "character", "outfit"]


def _now() -> str:
    return _transaction_now()


def _atomic_bytes(path: Path, payload: bytes) -> None:
    _transaction_atomic_bytes(path, payload)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _transaction_atomic_json(path, payload)


def _clean_values(values) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        text = str(value).strip()
        key = normalize_tag(text)
        if not text or not key or key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    return cleaned


@dataclass(frozen=True)
class CharacterRecordEditResult:
    kind: CharacterRecordKind
    record_id: str
    backup_directory: Path


@dataclass(frozen=True)
class CharacterTagRelationshipSuggestions:
    source_tag_id: str = ""
    outfit_tag_ids: tuple[str, ...] = ()
    review_notes: tuple[str, ...] = ()


class CharacterRecordEditService:
    """Validated automatic edits for typed records linked behind SDXL Tags."""

    CHARACTER_IDENTITY_CATEGORY = "character_identity"
    CHARACTER_SOURCE_CATEGORY = "copyright_series"
    CHARACTER_OUTFIT_CATEGORY = "character_outfit"

    def __init__(self, database: CharacterDatabase, tag_database=None, categories=None) -> None:
        self.database = database
        self.tag_database = tag_database
        self.categories = categories
        self._initial_files = self._file_snapshot()
        self._initial_tag_file = (
            tag_database.path.read_bytes() if tag_database is not None and tag_database.path.exists() else None
        )

    def _paths(self) -> tuple[Path, ...]:
        return (
            self.database.series_path,
            self.database.characters_path,
            self.database.outfits_path,
            self.database.relationship_path,
        )

    def _file_snapshot(self) -> dict[Path, bytes | None]:
        return {path: path.read_bytes() if path.exists() else None for path in self._paths()}

    def _assert_fresh(self) -> None:
        if self._file_snapshot() != self._initial_files:
            raise RuntimeError(
                "Character Source, character, or outfit knowledge changed after this editor opened. "
                "Close and reopen Tag Details to review the current record."
            )

    def _assert_tag_fresh(self) -> None:
        if self.tag_database is None:
            return
        current = self.tag_database.path.read_bytes() if self.tag_database.path.exists() else None
        if current != self._initial_tag_file:
            raise RuntimeError(
                "SDXL Tag knowledge changed after this editor opened. "
                "Close and reopen Tag Details to review current relationships."
            )

    def _link(self, kind: CharacterRecordKind, record_id: str) -> dict[str, Any]:
        return self.database.effective_knowledge_tag(kind, record_id)

    def _stage_entity_tag_payload(
        self,
        values: list[tuple[CharacterRecordKind, str, str, dict[str, Any]]],
    ) -> dict[str, Any]:
        changes: list[EntityTagChange] = []
        for kind, record_id, record_name, link in values:
            tag_id = str(link.get("tag_id") or "")
            tag = self.tag_database.tags.get(tag_id) if self.tag_database is not None else None
            changes.append(
                EntityTagChange(
                    entity_kind=RECORD_KIND_TO_ENDPOINT_KIND[kind],
                    entity_id=record_id,
                    tag_id=tag_id,
                    entity_label=record_name,
                    tag_label=str(getattr(tag, "display", "") or ""),
                    source=str(link.get("source") or "character_record_edit"),
                    basis=str(link.get("basis") or ""),
                    reviewed_at=str(link.get("reviewed_at") or ""),
                    compatibility_link=link,
                )
            )
        payload, _relationship_ids = stage_entity_tag_changes(
            self.database.default_relationship_path,
            self.database.relationship_path,
            changes,
        )
        return payload

    def _stage_character_relationship_payload(
        self,
        changes: list[CharacterRelationshipChange],
        *,
        user_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload, _relationship_ids = stage_character_relationship_changes(
            self.database.default_relationship_path,
            self.database.relationship_path,
            changes,
            user_payload=user_payload,
        )
        return payload

    def _user_relationships_from_payload(
        self, payload: dict[str, Any] | None = None,
    ) -> tuple[KnowledgeRelationship, ...]:
        if payload is None:
            return tuple(self.database._user_relationships)
        return tuple(
            KnowledgeRelationship.from_dict(item)
            for item in payload.get("relationships", ())
            if isinstance(item, dict)
        )

    def _normalize_record_storage(
        self,
        kind: CharacterRecordKind,
        record: dict[str, Any],
        *,
        user_relationship_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return character_record_storage_payload(
            kind,
            record,
            self.database._default_relationships,
            self._user_relationships_from_payload(user_relationship_payload),
            projection_fields=record.keys(),
        )

    def _record_relationship_changes(
        self,
        kind: CharacterRecordKind,
        record_id: str,
        values: dict[str, Any],
        *,
        previous: dict[str, Any] | None = None,
    ) -> list[CharacterRelationshipChange]:
        previous = previous or {}
        source_kind = "character" if kind == "character" else "outfit"
        source_label = str(values.get("name") or record_id)
        changes: list[CharacterRelationshipChange] = []
        if kind in {"character", "outfit"}:
            source_id = str(values.get("series_id") or "")
            previous_source_id = str(previous.get("series_id") or "")
            source = self.database.series.get(source_id)
            changes.append(CharacterRelationshipChange(
                CHARACTER_SOURCE,
                source_kind,
                record_id,
                (source_id,) if source_id else (),
                source_label=source_label,
                target_labels=((source_id, source.name),) if source is not None else (),
                compatibility_target_ids=(previous_source_id,) if previous_source_id else (),
            ))
        if kind == "outfit":
            character_ids = tuple(dict.fromkeys(
                str(value) for value in values.get("character_ids", ()) if str(value)
            ))
            previous_ids = tuple(dict.fromkeys(
                str(value) for value in previous.get("character_ids", ()) if str(value)
            ))
            changes.append(CharacterRelationshipChange(
                ASSOCIATED_CHARACTER,
                "outfit",
                record_id,
                character_ids,
                source_label=source_label,
                target_labels=tuple(
                    (character_id, self.database.characters[character_id].name)
                    for character_id in character_ids
                    if character_id in self.database.characters
                ),
                compatibility_target_ids=previous_ids,
            ))
        if kind == "character":
            base_id = str(values.get("base_character_id") or "")
            previous_base_id = str(previous.get("base_character_id") or "")
            base = self.database.characters.get(base_id)
            variant_type = str(values.get("variant_type") or "")
            if base_id or previous_base_id:
                changes.append(CharacterRelationshipChange(
                    VERSION_OF,
                    "character",
                    record_id,
                    (base_id,) if base_id else (),
                    source_label=source_label,
                    target_labels=((base_id, base.name),) if base is not None else (),
                    compatibility_target_ids=(previous_base_id,) if previous_base_id else (),
                    details=(("variant_type", variant_type),) if variant_type else (),
                ))
        return changes

    @staticmethod
    def _trailing_parenthetical_groups(value: str) -> tuple[tuple[str, ...], bool]:
        _stem, groups, nested = trailing_parenthetical_groups(value)
        return groups, nested

    def _require_tag_database(self) -> None:
        if self.tag_database is None:
            raise RuntimeError(
                "Character Source and outfit relationships require the current SDXL Tag database."
            )

    def _clean_component_tags(
        self, tag_id: str, values, *, representative_values=(), existing_values=(),
    ) -> list[str]:
        """Keep a collection's representative Tag out of new component data.

        Older local records may already contain that duplicate. Preserve it on
        an unrelated edit, but do not allow a newly created or newly added
        duplicate to extend the legacy misunderstanding.
        """
        cleaned = _clean_values(values)
        if self.tag_database is None or not str(tag_id or ""):
            return cleaned
        representative = self.tag_database.tags.get(str(tag_id))
        if representative is None:
            return cleaned
        representative_forms = {
            normalize_tag(value)
            for value in (*self.tag_database.identity_names(representative),*representative_values)
            if normalize_tag(value)
        }
        existing_forms = {
            normalize_tag(value) for value in existing_values if normalize_tag(value)
        }
        duplicate = next((
            value for value in cleaned
            if normalize_tag(value) in representative_forms
            and normalize_tag(value) not in existing_forms
        ), "")
        if duplicate:
            raise ValueError(
                f'"{duplicate}" is the collection\'s representative SDXL Tag, not a component. '
                "Add only the appearance, apparel, or style Tags that describe the collection."
            )
        return cleaned

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
        if tag is None:
            return False
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

    def _matching_tag_ids(self, value: str, category_id: str) -> tuple[str, ...]:
        self._require_tag_database()
        key = normalize_tag(value)
        role = self._role_for_category(category_id)
        return tuple(
            record.id
            for record in self.tag_database.tags.values()
            if self._tag_has_role(record, role)
            and key
            and key in {
                normalize_tag(name)
                for name in self.tag_database.identity_names(record)
                if normalize_tag(name)
            }
        )

    def _series_resolution(
        self, tag_id: str, *, expected_series_id: str = "",
    ) -> tuple[str, bool]:
        self._require_tag_database()
        self._assert_tag_fresh()
        tag = self.tag_database.tags.get(str(tag_id))
        if not self._tag_has_role(tag, "character_source"):
            raise ValueError("Choose one existing Character Source SDXL Tag.")
        reviewed = [
            record for record in self.database.series.values()
            if self._link("series", record.id).get("status") == "reviewed"
            and str(self._link("series", record.id).get("tag_id") or "") == tag.id
        ]
        if len(reviewed) > 1:
            raise ValueError(
                "This Character Source SDXL Tag has multiple reviewed typed owners. "
                "Use Review Character Tag Links before selecting it."
            )
        if reviewed:
            resolved = reviewed[0]
            if expected_series_id and resolved.id != expected_series_id:
                raise ValueError(
                    "The inferred Character Source conflicts with the character's reviewed source."
                )
            return resolved.id, False

        tag_names = {
            normalize_tag(value)
            for value in self.tag_database.identity_names(tag)
            if normalize_tag(value)
        }
        matches = [
            record for record in self.database.series.values()
            if tag_names.intersection({
                normalize_tag(value)
                for value in (record.name, *record.aliases)
                if normalize_tag(value)
            })
        ]
        if expected_series_id:
            matches = [record for record in matches if record.id == expected_series_id]
        if len(matches) != 1:
            reason = "does not have" if not matches else "has more than one"
            raise ValueError(
                f"This Character Source SDXL Tag {reason} exact typed source match. "
                "Use Review Character Tag Links before selecting it."
            )
        match = matches[0]
        existing = self._link("series", match.id)
        if (
            existing.get("status") == "reviewed"
            and str(existing.get("tag_id") or "") != tag.id
        ):
            raise ValueError(
                "The exact typed Character Source already owns a different reviewed SDXL Tag. "
                "Use Review Character Tag Links before selecting it."
            )
        excluded = existing.get("excluded_tag_ids", ())
        if (
            existing.get("status") == "reviewed_exclusions"
            and isinstance(excluded, (list, tuple))
            and tag.id in {str(value) for value in excluded}
        ):
            raise ValueError(
                "This Character Source SDXL Tag was explicitly excluded from the exact typed source. "
                "Use Review Character Tag Links before selecting it."
            )
        raise ValueError(
            "This Character Source SDXL Tag has one exact typed source candidate, but the stable-ID "
            "link has not been reviewed. Use Review Character Tag Links before selecting it."
        )

    def _outfit_ids_for_tags(
        self, tag_ids, *, source_series_id: str | None = None,
    ) -> tuple[str, ...]:
        self._require_tag_database()
        self._assert_tag_fresh()
        resolved: list[str] = []
        for tag_id in dict.fromkeys(str(value).strip() for value in tag_ids if str(value).strip()):
            tag = self.tag_database.tags.get(tag_id)
            if not self._tag_has_role(tag, "character_outfit"):
                raise ValueError("Choose existing Character Outfit SDXL Tags for associated outfits.")
            owners = [
                record for record in self.database.outfits.values()
                if self._link("outfit", record.id).get("status") == "reviewed"
                and str(self._link("outfit", record.id).get("tag_id") or "") == tag_id
            ]
            if len(owners) != 1:
                raise ValueError(
                    "Each associated outfit tag must have exactly one reviewed typed outfit owner. "
                    "Use Review Character Tag Links before selecting it."
                )
            owner = owners[0]
            owner_source_id = self.database.character_source_id("outfit", owner.id)
            if source_series_id and owner_source_id and owner_source_id != source_series_id:
                raise ValueError(
                    f'Outfit "{tag.display}" belongs to a different Character Source.'
                )
            resolved.append(owner.id)
        return tuple(resolved)

    def source_tag_error(self, tag_ids) -> str:
        values = tuple(str(value) for value in tag_ids if str(value))
        if len(values) > 1:
            return "Choose one Character Source SDXL Tag."
        if not values:
            return ""
        try:
            self._series_resolution(values[0])
        except (RuntimeError, ValueError) as error:
            return str(error)
        return ""

    def outfit_tag_error(self, tag_ids, *, source_tag_id: str = "") -> str:
        try:
            source_id = self._series_resolution(source_tag_id)[0] if source_tag_id else None
            self._outfit_ids_for_tags(tag_ids, source_series_id=source_id)
        except (RuntimeError, ValueError) as error:
            return str(error)
        return ""

    def character_id_for_tag(self, tag_id: str) -> str:
        self._require_tag_database()
        tag = self.tag_database.tags.get(str(tag_id))
        if not self._tag_has_role(tag, "character_identity"):
            raise ValueError("Choose one existing Character Identity SDXL Tag.")
        owners = [
            record.id
            for record in self.database.characters.values()
            if self.database.entity_tag_id("character", record.id) == tag.id
        ]
        if len(owners) != 1:
            raise ValueError(
                "The Character Identity SDXL Tag must have exactly one reviewed typed owner. "
                "Use Review Character Tag Links before selecting it."
            )
        return owners[0]

    def _validate_version(
        self,
        base_character_id: str,
        variant_type: str,
        *,
        series_id: str | None,
        record_id: str = "",
    ) -> None:
        if bool(base_character_id) != bool(variant_type):
            raise ValueError("Choose both a broader character and a Version Type, or clear both.")
        if not base_character_id:
            return
        if variant_type not in {"ascension", "class_variant", "form"}:
            raise ValueError("Choose a valid Version Type.")
        if base_character_id == record_id:
            raise ValueError("A character cannot be a version of itself.")
        base = self.database.characters.get(base_character_id)
        if base is None:
            raise ValueError("Choose an available broader Character Identity Tag.")
        base_source_id = self.database.character_source_id("character", base.id)
        if (base_source_id or None) != (series_id or None):
            raise ValueError(
                "The broader character must use the same reviewed Character Source."
            )

    def relationship_suggestions(
        self, character_tag_id: str, *, expected_series_id: str = "",
    ) -> CharacterTagRelationshipSuggestions:
        self._require_tag_database()
        character_tag = self.tag_database.tags.get(str(character_tag_id))
        if not self._tag_has_role(character_tag, "character_identity"):
            return CharacterTagRelationshipSuggestions(
                review_notes=("The linked Character Identity SDXL Tag is unavailable.",),
            )
        groups, nested = self._trailing_parenthetical_groups(character_tag.display)
        if not groups:
            return CharacterTagRelationshipSuggestions()
        notes: list[str] = []
        if nested:
            notes.append("Nested parenthetical naming needs relationship review.")

        source_tag_id = ""
        source_series_id: str | None = None
        source_matches = self._matching_tag_ids(groups[-1], self.CHARACTER_SOURCE_CATEGORY)
        if len(source_matches) == 1 and not nested:
            try:
                source_series_id, _link_required = self._series_resolution(
                    source_matches[0], expected_series_id=expected_series_id,
                )
                source_tag_id = source_matches[0]
            except ValueError as error:
                notes.append(str(error))
        elif len(source_matches) > 1:
            notes.append(f'Character Source name "{groups[-1]}" matches multiple SDXL Tags.')
        else:
            notes.append(f'Character Source name "{groups[-1]}" has no exact known SDXL Tag.')

        outfit_tag_ids: list[str] = []
        if source_tag_id and len(groups) > 1 and not nested:
            for group in groups[:-1]:
                matches = self._matching_tag_ids(group, self.CHARACTER_OUTFIT_CATEGORY)
                if len(matches) != 1:
                    notes.append(
                        f'Outfit name "{group}" '
                        + ("matches multiple SDXL Tags." if matches else "has no exact known SDXL Tag.")
                    )
                    continue
                try:
                    self._outfit_ids_for_tags(
                        matches, source_series_id=source_series_id,
                    )
                except ValueError as error:
                    notes.append(str(error))
                    continue
                outfit_tag_ids.append(matches[0])
        return CharacterTagRelationshipSuggestions(
            source_tag_id=source_tag_id,
            outfit_tag_ids=tuple(dict.fromkeys(outfit_tag_ids)),
            review_notes=tuple(dict.fromkeys(notes)),
        )

    @staticmethod
    def _record_id(name: str, existing, *, series_id: str | None = None) -> str:
        source = f"{name}_{series_id}" if series_id else name
        base = re.sub(r"[^a-z0-9]+", "_", source.strip().lower()).strip("_") or "item"
        identifier = base
        counter = 2
        while identifier in existing:
            identifier = f"{base}_{counter}"
            counter += 1
        return identifier

    def _assert_tag_can_create(self, tag_id: str, *, kind: CharacterRecordKind) -> None:
        if self.tag_database is None:
            raise RuntimeError("Linked typed-record creation requires the current SDXL Tag database.")
        current = self.tag_database.path.read_bytes() if self.tag_database.path.exists() else None
        if current != self._initial_tag_file:
            raise RuntimeError(
                "System Knowledge changed after this creation editor opened. "
                "Close and reopen Tag Details before creating the linked record."
            )
        tag = self.tag_database.tags.get(str(tag_id))
        expected_role = (
            "character_source"
            if kind == "series"
            else "character_outfit"
            if kind == "outfit"
            else "character_identity"
        )
        expected_label = (
            "Character Source"
            if kind == "series"
            else "Character Outfit"
            if kind == "outfit"
            else "Character Identity"
        )
        if not self._tag_has_role(tag, expected_role):
            raise ValueError(
                f"Choose an available SDXL Tag in {expected_label} before creating a typed record."
            )
        candidates: tuple[tuple[CharacterRecordKind, Any], ...] = (
            tuple(("series", record) for record in self.database.series.values())
            if kind == "series"
            else (
                *(("character", record) for record in self.database.characters.values()),
                *(("outfit", record) for record in self.database.outfits.values()),
            )
        )
        for record_kind, record in candidates:
            link = self._link(record_kind, record.id)
            if link.get("status") == "reviewed" and str(link.get("tag_id") or "") == tag.id:
                raise ValueError(f'This SDXL Tag is already linked to typed record “{record.name}”.')
        tag_names = {
            normalize_tag(value) for value in self.tag_database.identity_names(tag)
            if normalize_tag(value)
        }
        for record_kind, record in candidates:
            link = self._link(record_kind, record.id)
            excluded = link.get("excluded_tag_ids", ())
            if (
                link.get("status") == "reviewed_exclusions"
                and isinstance(excluded, (list, tuple))
                and tag.id in {str(value).strip() for value in excluded}
            ):
                continue
            record_names = {normalize_tag(value) for value in (record.name, *record.aliases) if normalize_tag(value)}
            if tag_names.intersection(record_names):
                raise ValueError(
                    f'Existing typed record “{record.name}” may already represent this SDXL Tag. '
                    "Use Review Character Tag Links instead of creating a duplicate."
                )

    def create_source_from_name(self, name: str, identity_service) -> str:
        """Create a reviewed source through the existing two verified writers.

        The Tag is independently durable, just as in ordinary Tag creation.
        A failed typed-record write leaves that Tag available for a safe retry.
        Keep this editor's Character Knowledge snapshot throughout both steps.
        """
        self._assert_fresh()
        self._assert_tag_fresh()
        name = str(name).strip()
        if not name:
            raise ValueError("Enter a Character Source name.")
        if identity_service.knowledge is not self.tag_database:
            raise ValueError("Source creation requires the same current Tag database.")
        plan = identity_service.plan((name,), confirmation=True)
        if plan.blockers:
            raise ValueError(" ".join(entry.message for entry in plan.blockers))
        if plan.new_entries:
            conflict = self.database.conflicting_series([name])
            if conflict is not None:
                raise ValueError(
                    f'Character Source “{conflict.name}” already uses this name. '
                    "Use Review Source Tag Links to link its existing record."
                )
            resolution = identity_service.commit(
                plan, source="character_source_picker", category="copyright_series",
            )
            # Only advance the Tag snapshot after our own verified Tag write.
            # Character files retain their original stale guard.
            tag_id = resolution.stable_ids[0]
            try:
                self.accept_verified_tag_write()
            except (OSError, RuntimeError) as error:
                raise RuntimeError(
                    f'The reusable Tag “{name}” was saved, but its source record '
                    f"was not created. Reopen the editor to continue. {error}"
                ) from error
        else:
            tag_id = identity_service.require_existing(
                (name,), confirmation=True,
            ).stable_ids[0]
        tag = self.tag_database.tags[tag_id]
        if not self._tag_has_role(tag, "character_source"):
            raise ValueError("This name already belongs to a Tag outside Character Source. Review its Category in Tag Details.")
        try:
            self._series_resolution(tag_id)
        except ValueError:
            try:
                self.create_series(tag_id, name=tag.display)
            except (OSError, RuntimeError, ValueError) as error:
                raise ValueError(
                    f'The reusable Tag “{tag.display}” is saved, but its source record '
                    f"was not created. {error}"
                ) from error
        return tag_id

    def accept_verified_tag_write(self) -> None:
        """Advance only the Tag guard after this workflow's verified write."""
        self._assert_fresh()
        current = self.tag_database.path.read_bytes() if self.tag_database.path.exists() else None
        if current != getattr(self.tag_database, "_loaded_bytes", None):
            raise RuntimeError("SDXL Tag knowledge changed again. Reopen the editor before continuing.")
        self._initial_tag_file = current

    def assert_fresh(self) -> None:
        """Check both reusable authorities before a nested editing action."""
        self._assert_fresh()
        self._assert_tag_fresh()

    def source_creation_error(self, tag_id: str) -> str:
        """Explain a held source creation before offering its UI action."""
        try:
            self._assert_fresh()
            self._assert_tag_can_create(tag_id, kind="series")
        except (OSError, RuntimeError, ValueError) as error:
            return str(error)
        return ""

    def create_series(
        self, tag_id: str, *, name: str, aliases=(), original_style_tags=(),
    ) -> CharacterRecordEditResult:
        self._assert_fresh()
        self._assert_tag_can_create(tag_id, kind="series")
        name = str(name).strip()
        if not name:
            raise ValueError("Enter a preferred Character Source name.")
        aliases = _clean_values(aliases)
        conflict = self.database.conflicting_series([name, *aliases])
        if conflict is not None:
            raise ValueError(
                f'Character Source “{conflict.name}” already uses this name or alias.'
            )
        record_id = self._record_id(name, self.database.series)
        return self._create("series", record_id, {
            "id": record_id,
            "name": name,
            "aliases": aliases,
            "original_style_tags": _clean_values(original_style_tags),
            "knowledge_tag": {
                "tag_id": str(tag_id),
                "status": "reviewed",
                "basis": "manual_create",
                "reviewed_at": _now(),
            },
        })

    def create_character(
        self, tag_id: str, *, name: str, series_id: str | None,
        identity_type: str, aliases=(), sdxl_tags=(), plain_language=(),
        original_style_tags=(), origin_label: str = "", creator: str = "",
        source_tag_id: str = "", outfit_tag_ids=(),
        base_character_id: str = "", variant_type: str = "",
    ) -> CharacterRecordEditResult:
        self._assert_fresh()
        self._assert_tag_can_create(tag_id, kind="character")
        name = str(name).strip()
        if not name:
            raise ValueError("Enter a preferred character name.")
        if series_id and series_id not in self.database.series:
            raise ValueError("Choose an existing Character Source or No Character Source.")
        if identity_type not in {"canonical", "original", "generic", "unknown"}:
            raise ValueError("Choose a valid identity type.")
        aliases = _clean_values(aliases)
        source_link_required = False
        if source_tag_id:
            resolved_series_id, source_link_required = self._series_resolution(source_tag_id)
            if series_id and series_id != resolved_series_id:
                raise ValueError("The selected Character Source tag conflicts with the typed source.")
            series_id = resolved_series_id
        self._validate_version(
            str(base_character_id), str(variant_type), series_id=series_id,
        )
        conflict = self.database.conflicting_character([name, *aliases], series_id=series_id)
        if conflict is not None:
            raise ValueError(f'Character “{conflict.name}” already uses this name or alias in the selected Character Source.')
        record_id = self._record_id(name, self.database.characters, series_id=series_id)
        outfit_ids = self._outfit_ids_for_tags(
            outfit_tag_ids, source_series_id=series_id,
        )
        tags = self._clean_component_tags(
            tag_id, sdxl_tags, representative_values=(name, *aliases),
        )
        character_payload = {
            "id": record_id, "name": name, "series_id": series_id,
            "aliases": aliases, "default_tags": tags, "sdxl_tags": tags,
            "plain_language": _clean_values(plain_language),
            "original_style_tags": _clean_values(original_style_tags),
            "identity_type": identity_type, "origin_label": str(origin_label).strip(),
            "creator": str(creator).strip(), "tag_associations": [],
            "knowledge_tag": {"tag_id": str(tag_id), "status": "reviewed", "basis": "manual_create", "reviewed_at": _now()},
        }
        if base_character_id:
            character_payload["base_character_id"] = str(base_character_id)
            character_payload["variant_type"] = str(variant_type)
        return self._apply_character_bundle(
            record_id,
            character_payload,
            operation="create",
            source_tag_id=str(source_tag_id),
            source_link_required=source_link_required,
            source_relationship_explicit=bool(series_id or source_tag_id),
            outfit_ids=outfit_ids,
            version_relationship_explicit=bool(base_character_id),
        )

    def create_outfit(
        self, tag_id: str, *, name: str, series_id: str | None,
        character_ids=(), aliases=(), sdxl_tags=(), plain_language=(),
        style_tags=(), notes: str = "", source_tag_id: str = "",
    ) -> CharacterRecordEditResult:
        self._assert_fresh()
        self._assert_tag_can_create(tag_id, kind="outfit")
        name = str(name).strip()
        if not name:
            raise ValueError("Enter a preferred outfit name.")
        if series_id and series_id not in self.database.series:
            raise ValueError("Choose an existing Character Source or No Character Source.")
        if source_tag_id:
            resolved_series_id, _link_required = self._series_resolution(source_tag_id)
            if series_id and series_id != resolved_series_id:
                raise ValueError("The selected Character Source tag conflicts with the typed source.")
            series_id = resolved_series_id
        owners = list(dict.fromkeys(str(value).strip() for value in character_ids if str(value).strip()))
        if any(value not in self.database.characters for value in owners):
            raise ValueError("One or more associated characters are no longer available.")
        aliases = _clean_values(aliases)
        conflict = self.database.conflicting_outfit([name, *aliases], character_ids=owners, series_id=series_id)
        if conflict is not None:
            raise ValueError(f'Outfit “{conflict.name}” already uses this name or alias for the selected relationship.')
        record_id = self._record_id(name, self.database.outfits)
        return self._create("outfit", record_id, {
            "id": record_id, "name": name, "character_ids": owners, "series_id": series_id,
            "aliases": aliases,
            "sdxl_tags": self._clean_component_tags(
                tag_id, sdxl_tags, representative_values=(name, *aliases),
            ),
            "plain_language": _clean_values(plain_language), "style_tags": _clean_values(style_tags),
            "notes": str(notes).strip(),
            "knowledge_tag": {"tag_id": str(tag_id), "status": "reviewed", "basis": "manual_create", "reviewed_at": _now()},
        })

    def update_character(
        self, record_id: str, *, name: str, series_id: str | None,
        identity_type: str, aliases=(), sdxl_tags=(), plain_language=(),
        original_style_tags=(), origin_label: str = "", creator: str = "",
        source_tag_id: str = "", source_tag_explicit: bool = False,
        outfit_tag_ids=None,
        base_character_id: str | None = None, variant_type: str = "",
        version_explicit: bool = False,
    ) -> CharacterRecordEditResult:
        record = self.database.characters.get(record_id)
        if record is None:
            raise ValueError("The linked character record is no longer available.")
        name = str(name).strip()
        if not name:
            raise ValueError("Enter a preferred character name.")
        if series_id and series_id not in self.database.series:
            raise ValueError("Choose an existing Character Source or No Character Source.")
        if identity_type not in {"canonical", "original", "generic", "unknown"}:
            raise ValueError("Choose a valid identity type.")
        aliases = _clean_values(aliases)
        source_link_required = False
        if source_tag_explicit:
            series_id = None
            if source_tag_id:
                series_id, source_link_required = self._series_resolution(source_tag_id)
        if version_explicit:
            self._validate_version(
                str(base_character_id or ""), str(variant_type),
                series_id=series_id, record_id=record_id,
            )
        conflict = self.database.conflicting_character(
            [name, *aliases], series_id=series_id, exclude_id=record_id,
        )
        if conflict is not None:
            raise ValueError(
                f'Character “{conflict.name}” already uses this name or alias in the selected Character Source.'
            )
        representative_link = self._link("character", record.id)
        representative_tag_id = (
            str(representative_link.get("tag_id") or "")
            if representative_link.get("status") == "reviewed" else ""
        )
        tags = self._clean_component_tags(
            representative_tag_id, sdxl_tags,
            representative_values=(name, *aliases), existing_values=record.sdxl_tags,
        )
        character_changes = {
            "name": name, "series_id": series_id, "identity_type": identity_type,
            "aliases": aliases, "default_tags": tags, "sdxl_tags": tags,
            "plain_language": _clean_values(plain_language),
            "original_style_tags": _clean_values(original_style_tags),
            "origin_label": str(origin_label).strip(), "creator": str(creator).strip(),
        }
        if (
            not source_tag_explicit
            and self.database.character_relationship(
                CHARACTER_SOURCE, "character", record_id,
            ).represented
        ):
            character_changes.pop("series_id", None)
        if version_explicit:
            character_changes.update({
                "base_character_id": str(base_character_id or "") or None,
                "variant_type": str(variant_type),
            })
        if source_tag_explicit or outfit_tag_ids is not None or version_explicit:
            outfit_ids = (
                self._outfit_ids_for_tags(outfit_tag_ids, source_series_id=series_id)
                if outfit_tag_ids is not None else None
            )
            return self._apply_character_bundle(
                record_id,
                character_changes,
                operation="update",
                source_tag_id=str(source_tag_id) if source_tag_explicit else "",
                source_link_required=source_link_required,
                source_relationship_explicit=source_tag_explicit,
                outfit_ids=outfit_ids,
                version_relationship_explicit=version_explicit,
            )
        return self._apply("character", record_id, character_changes)

    def update_series(
        self, record_id: str, *, name: str, aliases=(), original_style_tags=(),
    ) -> CharacterRecordEditResult:
        record = self.database.series.get(record_id)
        if record is None:
            raise ValueError("The linked Character Source record is no longer available.")
        name = str(name).strip()
        if not name:
            raise ValueError("Enter a preferred Character Source name.")
        aliases = _clean_values(aliases)
        conflict = self.database.conflicting_series(
            [name, *aliases], exclude_id=record_id,
        )
        if conflict is not None:
            raise ValueError(
                f'Character Source “{conflict.name}” already uses this name or alias.'
            )
        return self._apply(
            "series",
            record_id,
            {
                "name": name,
                "aliases": aliases,
                "original_style_tags": _clean_values(original_style_tags),
            },
        )

    def update_outfit(
        self, record_id: str, *, name: str, series_id: str | None,
        character_ids=(), aliases=(), sdxl_tags=(), plain_language=(),
        style_tags=(), notes: str = "", source_tag_id: str = "",
        source_tag_explicit: bool = False,
        character_ids_explicit: bool = True,
    ) -> CharacterRecordEditResult:
        record = self.database.outfits.get(record_id)
        if record is None:
            raise ValueError("The linked outfit record is no longer available.")
        name = str(name).strip()
        if not name:
            raise ValueError("Enter a preferred outfit name.")
        if series_id and series_id not in self.database.series:
            raise ValueError("Choose an existing Character Source or No Character Source.")
        if source_tag_explicit:
            series_id = None
            if source_tag_id:
                series_id, _link_required = self._series_resolution(source_tag_id)
        owners = list(dict.fromkeys(
            str(value).strip()
            for value in (
                character_ids if character_ids_explicit else record.character_ids
            )
            if str(value).strip()
        ))
        unknown = [value for value in owners if value not in self.database.characters]
        if unknown:
            raise ValueError("One or more associated characters are no longer available.")
        aliases = _clean_values(aliases)
        conflict = self.database.conflicting_outfit(
            [name, *aliases], character_ids=owners, series_id=series_id, exclude_id=record_id,
        )
        if conflict is not None:
            raise ValueError(
                f'Outfit “{conflict.name}” already uses this name or alias for the selected relationship.'
            )
        changes = {
            "name": name, "series_id": series_id, "character_ids": owners,
            "aliases": aliases,
            "sdxl_tags": self._clean_component_tags(
                self.database.entity_tag_id("outfit", record.id),
                sdxl_tags,
                representative_values=(name, *aliases),
                existing_values=record.sdxl_tags,
            ),
            "plain_language": _clean_values(plain_language),
            "style_tags": _clean_values(style_tags), "notes": str(notes).strip(),
        }
        if (
            not source_tag_explicit
            and self.database.character_relationship(
                CHARACTER_SOURCE, "outfit", record_id,
            ).represented
        ):
            changes.pop("series_id", None)
        if (
            not character_ids_explicit
            and self.database.character_relationship(
                ASSOCIATED_CHARACTER, "outfit", record_id,
            ).represented
        ):
            changes.pop("character_ids", None)
        return self._apply(
            "outfit", record_id,
            changes,
            relationship_kinds=(
                (CHARACTER_SOURCE,) if source_tag_explicit else ()
            ) + ((ASSOCIATED_CHARACTER,) if character_ids_explicit else ()),
        )

    @classmethod
    def recover_interrupted(cls, directory: str | Path) -> list[Path]:
        return recover_interrupted_character_transactions(
            directory, write_json=_atomic_json, write_bytes=_atomic_bytes,
        )

    def _apply_character_bundle(
        self, record_id: str, character_values: dict[str, Any], *,
        operation: Literal["create", "update"], source_tag_id: str,
        source_link_required: bool, source_relationship_explicit: bool,
        outfit_ids: tuple[str, ...] | None,
        version_relationship_explicit: bool,
    ) -> CharacterRecordEditResult:
        self._assert_fresh()
        paths = {
            "series": self.database.series_path,
            "characters": self.database.characters_path,
            "outfits": self.database.outfits_path,
        }
        payloads = {
            key: json.loads(path.read_text(encoding="utf-8"))
            for key, path in paths.items()
        }
        originals = json.loads(json.dumps(payloads))
        series_rows = payloads["series"].get("series")
        character_rows = payloads["characters"].get("characters")
        outfit_rows = payloads["outfits"].get("outfits")
        if not isinstance(series_rows, list) or not isinstance(character_rows, list) or not isinstance(outfit_rows, list):
            raise RuntimeError("Typed Character Knowledge contains an invalid record collection.")

        stored_character = next((
            item for item in character_rows
            if isinstance(item, dict) and str(item.get("id") or "") == record_id
        ), None)
        if operation == "create":
            if stored_character is not None:
                raise RuntimeError("A typed record with the generated stable ID already exists. Reopen and try again.")
            stored_character = json.loads(json.dumps(character_values))
            character_rows.append(stored_character)
            original_character_link = stored_character.get("knowledge_tag")
            expected_character = json.loads(json.dumps(stored_character))
            removed_character_keys: set[str] = set()
            previous_character: dict[str, Any] = {}
        else:
            if stored_character is None:
                raise RuntimeError("The linked typed record no longer exists.")
            previous_character = json.loads(json.dumps(stored_character))
            original_character_link = (
                json.loads(json.dumps(stored_character.get("knowledge_tag")))
                if "knowledge_tag" in stored_character
                else self.database.compatibility_record_payload(
                    "character", record_id,
                ).get("knowledge_tag")
            )
            expected_character = {
                key: json.loads(json.dumps(value))
                for key, value in character_values.items()
            }
            removed_character_keys = {
                key
                for key in ("base_character_id","variant_type")
                if key in expected_character and not expected_character[key]
            }
            for key in removed_character_keys:
                expected_character.pop(key,None)
                stored_character.pop(key,None)
            stored_character.update(expected_character)
            if original_character_link is None:
                stored_character.pop("knowledge_tag", None)
            else:
                stored_character["knowledge_tag"] = original_character_link

        canonical_links: list[
            tuple[CharacterRecordKind, str, str, dict[str, Any]]
        ] = []
        if operation == "create" and isinstance(original_character_link, dict):
            canonical_links.append(
                (
                    "character",
                    record_id,
                    str(stored_character.get("name") or record_id),
                    original_character_link,
                )
            )

        linked_series_id = str(stored_character.get("series_id") or "")
        if source_link_required:
            stored_series = next((
                item for item in series_rows
                if isinstance(item, dict) and str(item.get("id") or "") == linked_series_id
            ), None)
            if stored_series is None:
                raise RuntimeError("The selected typed Character Source no longer exists.")
            stored_series["knowledge_tag"] = {
                "tag_id": source_tag_id,
                "status": "reviewed",
                "basis": "exact_name",
                "reviewed_at": _now(),
            }
            canonical_links.append(
                (
                    "series",
                    linked_series_id,
                    str(stored_series.get("name") or linked_series_id),
                    stored_series["knowledge_tag"],
                )
            )

        desired_outfit_ids = set(outfit_ids or ())
        managed_outfit_ids = {
            record.id for record in self.database.outfits.values()
            if self.database.entity_tag_id("outfit", record.id)
        } if outfit_ids is not None else set()
        previous_outfits = {
            record.id: self.database.compatibility_record_payload(
                "outfit", record.id,
            )
            for record in self.database.outfits.values()
        }
        for stored_outfit in outfit_rows:
            if not isinstance(stored_outfit, dict):
                continue
            outfit_id = str(stored_outfit.get("id") or "")
            if outfit_id not in managed_outfit_ids:
                continue
            owners = list(dict.fromkeys(
                str(value)
                for value in (
                    stored_outfit.get("character_ids", ())
                    if "character_ids" in stored_outfit
                    else self.database.associated_character_ids(outfit_id)
                )
                if str(value)
            ))
            if outfit_id in desired_outfit_ids:
                if record_id not in owners:
                    owners.append(record_id)
            else:
                owners = [value for value in owners if value != record_id]
            stored_outfit["character_ids"] = owners

        payloads["series"]["schema_version"] = max(
            int(payloads["series"].get("schema_version", 0) or 0),
            CharacterDatabase.SERIES_SCHEMA_VERSION,
        )
        payloads["characters"]["schema_version"] = max(
            int(payloads["characters"].get("schema_version", 0) or 0),
            CharacterDatabase.CHARACTERS_SCHEMA_VERSION,
        )
        payloads["outfits"]["schema_version"] = max(
            int(payloads["outfits"].get("schema_version", 0) or 0),
            CharacterDatabase.OUTFITS_SCHEMA_VERSION,
        )
        changed: dict[Path, dict[str, Any]] = {}
        if canonical_links:
            changed[self.database.relationship_path] = self._stage_entity_tag_payload(
                canonical_links,
            )
        cross_changes = [
            change
            for change in self._record_relationship_changes(
                "character", record_id, stored_character,
                previous=previous_character,
            )
            if (
                change.kind == CHARACTER_SOURCE and source_relationship_explicit
            ) or (
                change.kind == VERSION_OF and version_relationship_explicit
            )
        ]
        for stored_outfit in outfit_rows:
            if not isinstance(stored_outfit, dict):
                continue
            outfit_id = str(stored_outfit.get("id") or "")
            if outfit_id not in managed_outfit_ids:
                continue
            cross_changes.extend(
                change
                for change in self._record_relationship_changes(
                    "outfit",
                    outfit_id,
                    stored_outfit,
                    previous=previous_outfits.get(outfit_id),
                )
                if change.kind == ASSOCIATED_CHARACTER
            )
        if cross_changes:
            changed[self.database.relationship_path] = self._stage_character_relationship_payload(
                cross_changes,
                user_payload=changed.get(self.database.relationship_path),
            )
        expected_base_id = str(stored_character.get("base_character_id") or "")
        expected_variant_type = str(stored_character.get("variant_type") or "")
        relationship_payload = changed.get(self.database.relationship_path)

        def normalize_in_place(
            kind: CharacterRecordKind, record: dict[str, Any],
        ) -> None:
            normalized = self._normalize_record_storage(
                kind,
                record,
                user_relationship_payload=relationship_payload,
            )
            record.clear()
            record.update(normalized)

        normalize_in_place("character", stored_character)
        if source_link_required:
            normalize_in_place("series", stored_series)
        for stored_outfit in outfit_rows:
            if (
                isinstance(stored_outfit, dict)
                and str(stored_outfit.get("id") or "") in managed_outfit_ids
            ):
                normalize_in_place("outfit", stored_outfit)
        record_changes = {
            paths[key]: payload
            for key, payload in payloads.items()
            if payload != originals[key]
        }
        record_changes.update(changed)
        changed = record_changes

        def verify(reloaded: CharacterDatabase) -> None:
            saved = reloaded.characters.get(record_id)
            if saved is None:
                raise RuntimeError("Saved character record was missing during read-back verification.")
            saved_payload = reloaded.compatibility_record_payload(
                "character", record_id,
            )
            if operation == "create":
                if saved_payload != expected_character:
                    raise RuntimeError("Created character record failed semantic read-back verification.")
                if reloaded.entity_tag_id("character", record_id) != str(
                    (original_character_link or {}).get("tag_id") or ""
                ):
                    raise RuntimeError("The canonical Character Identity Tag link failed read-back verification.")
            else:
                if (
                    any(
                        saved_payload.get(key) != value
                        for key,value in expected_character.items()
                    )
                    or any(key in saved_payload for key in removed_character_keys)
                ):
                    raise RuntimeError("Saved character record failed read-back verification.")
                saved_link = saved_payload.get("knowledge_tag") if "knowledge_tag" in saved_payload else None
                if saved_link != original_character_link:
                    raise RuntimeError("The reviewed Character Identity SDXL Tag link changed unexpectedly.")
            if source_relationship_explicit and reloaded.character_source_id(
                "character", record_id,
            ) != linked_series_id:
                raise RuntimeError("The canonical Character Source relationship failed read-back verification.")
            if version_relationship_explicit and reloaded.version_of(record_id) != (
                expected_base_id, expected_variant_type,
            ):
                raise RuntimeError("The canonical Version Of relationship failed read-back verification.")
            if source_link_required:
                source = reloaded.series.get(linked_series_id)
                if (
                    source is None
                    or reloaded.entity_tag_id("series", linked_series_id) != source_tag_id
                ):
                    raise RuntimeError("The Character Source SDXL Tag link failed read-back verification.")
            if outfit_ids is not None:
                for outfit_id in managed_outfit_ids:
                    saved_outfit = reloaded.outfits.get(outfit_id)
                    if saved_outfit is None:
                        raise RuntimeError("An associated outfit disappeared during read-back verification.")
                    expected_membership = outfit_id in desired_outfit_ids
                    if (
                        record_id in reloaded.associated_character_ids(outfit_id)
                    ) != expected_membership:
                        raise RuntimeError("Associated outfit links failed read-back verification.")

        return self._commit_payloads(
            changed,
            kind="character",
            record_id=record_id,
            operation=operation,
            verify=verify,
        )

    def _commit_payloads(
        self, payloads: dict[Path, dict[str, Any]], *, kind: CharacterRecordKind,
        record_id: str, operation: str, verify: Callable[[CharacterDatabase], None],
    ) -> CharacterRecordEditResult:
        if not payloads:
            return CharacterRecordEditResult(kind, record_id, Path())
        try:
            backup = commit_character_payloads(
                self.database.data_dir,
                payloads,
                expected_files=self._initial_files,
                operation=operation,
                kind=kind,
                record_id=record_id,
                verify=lambda: verify(CharacterDatabase(
                    self.database.data_dir, recover_interrupted=False,
                )),
                stale_message=(
                    "Character Source, character, or outfit knowledge changed "
                    "after this editor opened. Close and reopen Tag Details to "
                    "review the current record."
                ),
                write_json=_atomic_json,
                write_bytes=_atomic_bytes,
            )
        except BaseException:
            try:
                self.database.load()
                self._initial_files = self._file_snapshot()
            except Exception:
                pass
            raise
        self.database.load()
        self._initial_files = self._file_snapshot()
        return CharacterRecordEditResult(kind, record_id, backup)

    def _create(
        self, kind: CharacterRecordKind, record_id: str, record_payload: dict[str, Any],
    ) -> CharacterRecordEditResult:
        self._assert_fresh()
        path, collection, schema = self._record_store(kind)
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = payload.get(collection)
        if not isinstance(records, list):
            raise RuntimeError(f"{path.name} does not contain a valid {collection} collection.")
        if any(isinstance(item, dict) and str(item.get("id") or "") == record_id for item in records):
            raise RuntimeError("A typed record with the generated stable ID already exists. Reopen and try again.")
        records.append(json.loads(json.dumps(record_payload)))
        payload["schema_version"] = max(int(payload.get("schema_version", 0) or 0), schema)
        link = record_payload.get("knowledge_tag")
        if not isinstance(link, dict) or link.get("status") != "reviewed":
            raise RuntimeError("Linked typed-record creation requires one reviewed representative Tag.")
        relationship_payload = self._stage_entity_tag_payload(
            [(kind, record_id, str(record_payload.get("name") or record_id), link)],
        )
        cross_changes = self._record_relationship_changes(
            kind, record_id, record_payload,
        )
        if cross_changes:
            relationship_payload = self._stage_character_relationship_payload(
                cross_changes,
                user_payload=relationship_payload,
            )
        normalized = self._normalize_record_storage(
            kind,
            records[-1],
            user_relationship_payload=relationship_payload,
        )
        records[-1].clear()
        records[-1].update(normalized)

        def verify(reloaded: CharacterDatabase) -> None:
            saved = self._records_for_kind(reloaded, kind).get(record_id)
            if (
                saved is None
                or reloaded.compatibility_record_payload(kind, record_id)
                != record_payload
            ):
                raise RuntimeError("Created typed record failed semantic read-back verification.")
            if reloaded.entity_tag_id(kind, record_id) != str(link.get("tag_id") or ""):
                raise RuntimeError("The canonical Entity Tag link failed read-back verification.")
            if kind in {"character", "outfit"} and reloaded.character_source_id(
                kind, record_id,
            ) != str(record_payload.get("series_id") or ""):
                raise RuntimeError("The canonical Character Source link failed read-back verification.")
            if kind == "outfit" and set(
                reloaded.associated_character_ids(record_id)
            ) != set(record_payload.get("character_ids", ())):
                raise RuntimeError("Canonical Associated Characters failed read-back verification.")
            if kind == "character" and reloaded.version_of(record_id) != (
                str(record_payload.get("base_character_id") or ""),
                str(record_payload.get("variant_type") or ""),
            ):
                raise RuntimeError("The canonical Version Of link failed read-back verification.")

        return self._commit_payloads(
            {
                path: payload,
                self.database.relationship_path: relationship_payload,
            },
            kind=kind,
            record_id=record_id,
            operation="create",
            verify=verify,
        )

    def _apply(
        self, kind: CharacterRecordKind, record_id: str, changes: dict[str, Any],
        *,
        relationship_kinds: tuple[str, ...] = (),
    ) -> CharacterRecordEditResult:
        self._assert_fresh()
        path, collection, schema = self._record_store(kind)
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = payload.get(collection)
        if not isinstance(records, list):
            raise RuntimeError(f"{path.name} does not contain a valid {collection} collection.")
        stored = next((item for item in records if isinstance(item, dict) and str(item.get("id") or "") == record_id), None)
        if stored is None:
            raise RuntimeError("The linked typed record no longer exists.")
        previous = json.loads(json.dumps(stored))
        original_link = (
            json.loads(json.dumps(stored.get("knowledge_tag")))
            if "knowledge_tag" in stored
            else self.database.compatibility_record_payload(
                kind, record_id,
            ).get("knowledge_tag")
        )
        expected = {key: json.loads(json.dumps(value)) for key, value in changes.items()}
        record_changed = not all(
            stored.get(key) == value for key,value in expected.items()
        )
        if not record_changed and not relationship_kinds:
            return CharacterRecordEditResult(kind, record_id, Path())

        stored.update(expected)
        expected_source_id = str(stored.get("series_id") or "")
        expected_character_ids = tuple(
            str(value) for value in stored.get("character_ids", ()) if str(value)
        )
        if original_link is None:
            stored.pop("knowledge_tag", None)
        else:
            stored["knowledge_tag"] = original_link
        payload["schema_version"] = max(int(payload.get("schema_version", 0) or 0), schema)
        changed = {path: payload} if record_changed else {}
        relationship_payload = None
        if kind == "outfit" and relationship_kinds:
            relationship_payload = self._stage_character_relationship_payload(
                [
                    change
                    for change in self._record_relationship_changes(
                        kind, record_id, stored, previous=previous,
                    )
                    if change.kind in relationship_kinds
                ]
            )
            changed[self.database.relationship_path] = relationship_payload
        normalized = self._normalize_record_storage(
            kind,
            stored,
            user_relationship_payload=relationship_payload,
        )
        if normalized != stored:
            stored.clear()
            stored.update(normalized)
            changed[path] = payload

        def verify(reloaded: CharacterDatabase) -> None:
            saved_records = self._records_for_kind(reloaded, kind)
            saved = saved_records.get(record_id)
            if saved is None:
                raise RuntimeError("Saved typed record was missing during read-back verification.")
            saved_payload = reloaded.compatibility_record_payload(kind, record_id)
            if any(saved_payload.get(key) != value for key, value in expected.items()):
                raise RuntimeError("Saved typed record failed read-back verification.")
            saved_link = saved_payload.get("knowledge_tag") if "knowledge_tag" in saved_payload else None
            if saved_link != original_link:
                raise RuntimeError("The reviewed Character Identity SDXL Tag link changed unexpectedly.")
            if kind == "outfit" and relationship_kinds:
                if (
                    CHARACTER_SOURCE in relationship_kinds
                    and reloaded.character_source_id("outfit", record_id)
                    != expected_source_id
                ):
                    raise RuntimeError("The canonical outfit Character Source failed read-back verification.")
                if (
                    ASSOCIATED_CHARACTER in relationship_kinds
                    and set(reloaded.associated_character_ids(record_id))
                    != set(expected_character_ids)
                ):
                    raise RuntimeError("Canonical Associated Characters failed read-back verification.")

        return self._commit_payloads(
            changed,
            kind=kind,
            record_id=record_id,
            operation="update",
            verify=verify,
        )

    def _record_store(self, kind: CharacterRecordKind) -> tuple[Path, str, int]:
        if kind == "series":
            return self.database.series_path, "series", CharacterDatabase.SERIES_SCHEMA_VERSION
        if kind == "character":
            return (
                self.database.characters_path,
                "characters",
                CharacterDatabase.CHARACTERS_SCHEMA_VERSION,
            )
        return self.database.outfits_path, "outfits", CharacterDatabase.OUTFITS_SCHEMA_VERSION

    @staticmethod
    def _records_for_kind(database: CharacterDatabase, kind: CharacterRecordKind):
        if kind == "series":
            return database.series
        if kind == "character":
            return database.characters
        return database.outfits
