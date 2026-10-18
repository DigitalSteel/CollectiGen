from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .character_relationships import (
    EffectiveCharacterRelationship,
    character_relationship_index,
    effective_character_relationship,
)
from .character_projection_storage import (
    CHARACTER_STORAGE_SCHEMA_VERSION,
    OUTFIT_STORAGE_SCHEMA_VERSION,
    SERIES_STORAGE_SCHEMA_VERSION,
    character_record_compatibility_payload,
    character_record_storage_payloads,
)
from .character_storage_transaction import (
    atomic_bytes as _transaction_atomic_bytes,
    atomic_json as _transaction_atomic_json,
    character_transaction_lock,
    commit_character_payloads,
    recover_interrupted_character_transactions,
    snapshot as character_file_snapshot,
)
from .entity_tag_relationships import (
    RECORD_KIND_TO_ENDPOINT_KIND,
    EffectiveEntityTagLink,
    entity_tag_relationship_index,
    effective_entity_tag_link,
)
from .knowledge_relationships import (
    ASSOCIATED_CHARACTER,
    CHARACTER_SOURCE,
    OTHER_APPEARANCE,
    SPECIES_TRAIT,
    VERSION_OF,
)
from .relationship_store import RelationshipStore, validate_relationships
from .tag_collection import normalize_tag


def _slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    return value or "item"


def _normalized(text: str) -> str:
    return normalize_tag(text)


def _atomic_bytes(path: Path, payload: bytes) -> None:
    _transaction_atomic_bytes(path, payload)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _transaction_atomic_json(path, payload)


CHARACTER_IDENTITY_VARIANT_TYPE_LABELS = {
    "ascension": "Ascension",
    "class_variant": "Class Variant",
    "form": "Form / Version",
}


@dataclass
class SeriesRecord:
    id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    original_style_tags: list[str] = field(default_factory=list)
    knowledge_tag: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.extras)
        payload.update({"id": self.id, "name": self.name, "aliases": list(self.aliases), "original_style_tags": list(self.original_style_tags)})
        if self.knowledge_tag:
            payload["knowledge_tag"] = dict(self.knowledge_tag)
        else:
            payload.pop("knowledge_tag", None)
        return payload


@dataclass
class CharacterRecord:
    id: str
    name: str
    series_id: str | None = None
    aliases: list[str] = field(default_factory=list)
    default_tags: list[str] = field(default_factory=list)
    sdxl_tags: list[str] = field(default_factory=list)
    plain_language: list[str] = field(default_factory=list)
    original_style_tags: list[str] = field(default_factory=list)
    identity_type: str = "unknown"
    origin_label: str = ""
    creator: str = ""
    tag_associations: list[dict[str, Any]] = field(default_factory=list)
    base_character_id: str | None = None
    variant_type: str = ""
    knowledge_tag: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.extras)
        payload.update({
            "id": self.id,
            "name": self.name,
            "series_id": self.series_id,
            "aliases": list(self.aliases),
            "default_tags": list(self.default_tags),
            "sdxl_tags": list(self.sdxl_tags or self.default_tags),
            "plain_language": list(self.plain_language),
            "original_style_tags": list(self.original_style_tags),
            "identity_type": self.identity_type,
            "origin_label": self.origin_label,
            "creator": self.creator,
            "tag_associations": [dict(value) for value in self.tag_associations],
        })
        if self.base_character_id:
            payload["base_character_id"] = self.base_character_id
        else:
            payload.pop("base_character_id", None)
        if self.variant_type:
            payload["variant_type"] = self.variant_type
        else:
            payload.pop("variant_type", None)
        if self.knowledge_tag:
            payload["knowledge_tag"] = dict(self.knowledge_tag)
        else:
            payload.pop("knowledge_tag", None)
        return payload


@dataclass
class OutfitRecord:
    id: str
    name: str
    character_ids: list[str] = field(default_factory=list)
    series_id: str | None = None
    aliases: list[str] = field(default_factory=list)
    sdxl_tags: list[str] = field(default_factory=list)
    plain_language: list[str] = field(default_factory=list)
    style_tags: list[str] = field(default_factory=list)
    notes: str = ""
    knowledge_tag: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.extras)
        payload.update({
            "id": self.id, "name": self.name,
            "character_ids": list(self.character_ids), "series_id": self.series_id,
            "aliases": list(self.aliases), "sdxl_tags": list(self.sdxl_tags),
            "plain_language": list(self.plain_language),
            "style_tags": list(self.style_tags), "notes": self.notes,
        })
        if self.knowledge_tag:
            payload["knowledge_tag"] = dict(self.knowledge_tag)
        else:
            payload.pop("knowledge_tag", None)
        return payload


class CharacterDatabase:
    SERIES_SCHEMA_VERSION = SERIES_STORAGE_SCHEMA_VERSION
    CHARACTERS_SCHEMA_VERSION = CHARACTER_STORAGE_SCHEMA_VERSION
    OUTFITS_SCHEMA_VERSION = OUTFIT_STORAGE_SCHEMA_VERSION

    def __init__(
        self, data_dir: str | Path, *, recover_interrupted: bool = True,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.series_path = self.data_dir / "series.json"
        self.characters_path = self.data_dir / "characters.json"
        self.outfits_path = self.data_dir / "outfits.json"
        self.relationship_path = self.data_dir / "relationships.json"
        self.default_relationship_path = self.data_dir.parent / "defaults" / "relationships.json"
        self.series: dict[str, SeriesRecord] = {}
        self.characters: dict[str, CharacterRecord] = {}
        self.outfits: dict[str, OutfitRecord] = {}
        self._series_extras: dict[str, Any] = {}
        self._characters_extras: dict[str, Any] = {}
        self._outfits_extras: dict[str, Any] = {}
        self._series_schema_version = self.SERIES_SCHEMA_VERSION
        self._characters_schema_version = self.CHARACTERS_SCHEMA_VERSION
        self._outfits_schema_version = self.OUTFITS_SCHEMA_VERSION
        self._entity_tag_index = {}
        self._character_relationship_index = {}
        self._default_relationships = ()
        self._user_relationships = ()
        self._default_entity_tag_index = {}
        self._user_entity_tag_index = {}
        self._default_character_relationship_index = {}
        self._user_character_relationship_index = {}
        self._projection_fields = {"series": {}, "character": {}, "outfit": {}}
        self._recover_interrupted = bool(recover_interrupted)
        self._loaded_files: dict[Path, bytes | None] = {}
        self.load()

    def _file_paths(self) -> tuple[Path, ...]:
        return (
            self.series_path,
            self.characters_path,
            self.outfits_path,
            self.relationship_path,
        )

    def _file_snapshot(self) -> dict[Path, bytes | None]:
        return character_file_snapshot(self._file_paths())

    def load(self) -> None:
        if not self._recover_interrupted:
            self._load_content()
            return
        with character_transaction_lock(self.data_dir):
            self._load_content()

    def _load_content(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self._recover_interrupted:
            recover_interrupted_character_transactions(
                self.data_dir, write_json=_atomic_json, write_bytes=_atomic_bytes,
            )
        self._loaded_files = self._file_snapshot()
        self.series = {}
        self.characters = {}
        self.outfits = {}
        self._series_extras = {}
        self._characters_extras = {}
        self._outfits_extras = {}
        self._projection_fields = {"series": {}, "character": {}, "outfit": {}}
        self._series_schema_version = self.SERIES_SCHEMA_VERSION
        self._characters_schema_version = self.CHARACTERS_SCHEMA_VERSION
        self._outfits_schema_version = self.OUTFITS_SCHEMA_VERSION

        if self.series_path.exists():
            data = json.loads(self.series_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("series.json must contain one JSON object.")
            self._series_schema_version = max(self.SERIES_SCHEMA_VERSION, int(data.get("schema_version", 0) or 0))
            self._series_extras = {key: value for key, value in data.items() if key not in {"schema_version", "series"}}
            rows = data.get("series", [])
            if not isinstance(rows, list):
                raise ValueError("series.json does not contain a valid series collection.")
            seen_ids: set[str] = set()
            for item in rows:
                if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
                    raise ValueError("series.json contains a record without a stable ID and name.")
                item_id = str(item["id"])
                if item_id in seen_ids:
                    raise ValueError(f'series.json contains duplicate stable ID “{item_id}”.')
                seen_ids.add(item_id)
                record = SeriesRecord(
                    id=item_id,
                    name=str(item["name"]),
                    aliases=[str(value) for value in item.get("aliases", [])],
                    original_style_tags=[str(value) for value in item.get("original_style_tags", [])],
                    knowledge_tag=dict(item.get("knowledge_tag") or {}) if isinstance(item.get("knowledge_tag"), dict) else {},
                    extras={key: value for key, value in item.items() if key not in {
                        "id", "name", "aliases", "original_style_tags", "knowledge_tag",
                    }},
                )
                self.series[record.id] = record
                self._projection_fields["series"][record.id] = frozenset(
                    {"knowledge_tag"}.intersection(item)
                )

        if self.characters_path.exists():
            data = json.loads(self.characters_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("characters.json must contain one JSON object.")
            self._characters_schema_version = max(self.CHARACTERS_SCHEMA_VERSION, int(data.get("schema_version", 0) or 0))
            self._characters_extras = {key: value for key, value in data.items() if key not in {"schema_version", "characters"}}
            rows = data.get("characters", [])
            if not isinstance(rows, list):
                raise ValueError("characters.json does not contain a valid characters collection.")
            seen_ids = set()
            for item in rows:
                if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
                    raise ValueError("characters.json contains a record without a stable ID and name.")
                item_id = str(item["id"])
                if item_id in seen_ids:
                    raise ValueError(f'characters.json contains duplicate stable ID “{item_id}”.')
                seen_ids.add(item_id)
                record = CharacterRecord(
                    id=item_id,
                    name=str(item["name"]),
                    series_id=str(item["series_id"]) if item.get("series_id") else None,
                    aliases=[str(value) for value in item.get("aliases", [])],
                    default_tags=[str(value) for value in item.get("default_tags", [])],
                    sdxl_tags=[str(value) for value in item.get("sdxl_tags", item.get("default_tags", []))],
                    plain_language=[str(value) for value in item.get("plain_language", [])],
                    original_style_tags=[str(value) for value in item.get("original_style_tags", [])],
                    identity_type=str(item.get("identity_type") or ("canonical" if item.get("series_id") else "unknown")),
                    origin_label=str(item.get("origin_label") or ""),
                    creator=str(item.get("creator") or ""),
                    tag_associations=[dict(value) for value in item.get("tag_associations", []) if isinstance(value, dict)],
                    base_character_id=str(item["base_character_id"]) if item.get("base_character_id") else None,
                    variant_type=str(item.get("variant_type") or ""),
                    knowledge_tag=dict(item.get("knowledge_tag") or {}) if isinstance(item.get("knowledge_tag"), dict) else {},
                    extras={key: value for key, value in item.items() if key not in {
                        "id", "name", "series_id", "aliases", "default_tags", "sdxl_tags", "plain_language",
                        "original_style_tags", "identity_type", "origin_label", "creator", "tag_associations",
                        "base_character_id", "variant_type", "knowledge_tag",
                    }},
                )
                self.characters[record.id] = record
                self._projection_fields["character"][record.id] = frozenset(
                    {
                        "knowledge_tag", "series_id", "base_character_id",
                        "variant_type",
                    }.intersection(item)
                )

        if self.outfits_path.exists():
            data=json.loads(self.outfits_path.read_text(encoding="utf-8"))
            if not isinstance(data,dict):
                raise ValueError("outfits.json must contain one JSON object.")
            self._outfits_schema_version = max(self.OUTFITS_SCHEMA_VERSION, int(data.get("schema_version",0) or 0))
            self._outfits_extras = {key: value for key, value in data.items() if key not in {"schema_version", "outfits"}}
            rows=data.get("outfits",[])
            if not isinstance(rows,list):
                raise ValueError("outfits.json does not contain a valid outfits collection.")
            seen_ids=set()
            for item in rows:
                if not isinstance(item,dict) or not item.get("id") or not item.get("name"):
                    raise ValueError("outfits.json contains a record without a stable ID and name.")
                item_id=str(item["id"])
                if item_id in seen_ids:
                    raise ValueError(f'outfits.json contains duplicate stable ID “{item_id}”.')
                seen_ids.add(item_id)
                record=OutfitRecord(
                    id=item_id, name=str(item["name"]),
                    character_ids=[str(value) for value in item.get("character_ids",[])],
                    series_id=str(item["series_id"]) if item.get("series_id") else None,
                    aliases=[str(value) for value in item.get("aliases",[])],
                    sdxl_tags=[str(value) for value in item.get("sdxl_tags",[])],
                    plain_language=[str(value) for value in item.get("plain_language",[])],
                    style_tags=[str(value) for value in item.get("style_tags",[])],
                    notes=str(item.get("notes") or ""),
                    knowledge_tag=dict(item.get("knowledge_tag") or {}) if isinstance(item.get("knowledge_tag"),dict) else {},
                    extras={key:value for key,value in item.items() if key not in {
                        "id","name","character_ids","series_id","aliases","sdxl_tags","plain_language",
                        "style_tags","notes","knowledge_tag",
                    }},
                )
                self.outfits[record.id]=record
                self._projection_fields["outfit"][record.id] = frozenset(
                    {"knowledge_tag", "series_id", "character_ids"}.intersection(item)
                )

        if not self.series_path.exists() or not self.characters_path.exists() or not self.outfits_path.exists():
            self.save()
        default_relationships = RelationshipStore(
            self.default_relationship_path
        ).relationships
        user_relationships = RelationshipStore(self.relationship_path).relationships
        if self.default_relationship_path == self.relationship_path:
            default_relationships = {}
        effective_relationships = dict(default_relationships)
        effective_relationships.update(user_relationships)
        issues = validate_relationships(effective_relationships.values())
        if issues:
            raise ValueError(" ".join(issue.message for issue in issues[:3]))
        self._entity_tag_index = entity_tag_relationship_index(
            effective_relationships.values()
        )
        self._character_relationship_index = character_relationship_index(
            effective_relationships.values()
        )
        self._default_relationships = tuple(default_relationships.values())
        self._user_relationships = tuple(user_relationships.values())
        self._default_entity_tag_index = entity_tag_relationship_index(
            self._default_relationships
        )
        self._user_entity_tag_index = entity_tag_relationship_index(
            self._user_relationships
        )
        self._default_character_relationship_index = character_relationship_index(
            self._default_relationships
        )
        self._user_character_relationship_index = character_relationship_index(
            self._user_relationships
        )
        self._hydrate_canonical_compatibility_fields()
        self._validate_cross_entity_references()
        self._loaded_files = self._file_snapshot()

    def _hydrate_canonical_compatibility_fields(self) -> None:
        """Keep the public record API compatible while live storage stays canonical."""

        for kind, records in (
            ("series", self.series),
            ("character", self.characters),
            ("outfit", self.outfits),
        ):
            for record_id, record in records.items():
                fields = self._projection_fields[kind].get(record_id, ())
                if "knowledge_tag" not in fields:
                    link = self.entity_tag_link(kind, record_id)
                    if link.represented:
                        record.knowledge_tag = self.compatibility_record_payload(
                            kind, record_id,
                        ).get("knowledge_tag", {})
                if kind in {"character", "outfit"} and "series_id" not in fields:
                    relationship = self.character_relationship(
                        CHARACTER_SOURCE, kind, record_id,
                    )
                    if relationship.represented:
                        record.series_id = relationship.target_id or None
                if kind == "outfit" and "character_ids" not in fields:
                    relationship = self.character_relationship(
                        ASSOCIATED_CHARACTER, "outfit", record_id,
                    )
                    if relationship.represented:
                        record.character_ids = list(relationship.target_ids)
                if kind == "character" and not {
                    "base_character_id", "variant_type",
                }.intersection(fields):
                    relationship = self.character_relationship(
                        VERSION_OF, "character", record_id,
                    )
                    if relationship.represented:
                        record.base_character_id = relationship.target_id or None
                        record.variant_type = relationship.details.get(
                            "variant_type", "",
                        ) if relationship.target_id else ""

    def _validate_cross_entity_references(self) -> None:
        issues: list[str] = []
        for record in self.characters.values():
            source = self.character_relationship(
                CHARACTER_SOURCE, "character", record.id,
            )
            if source.conflicting:
                issues.append(
                    f'Character “{record.id}” has ambiguous Character Source IDs.'
                )
            source_id = source.target_id or str(record.series_id or "")
            if source_id and source_id not in self.series:
                issues.append(
                    f'Character “{record.id}” references missing Character Source “{source_id}”.'
                )
            version = self.character_relationship(VERSION_OF, "character", record.id)
            if version.conflicting:
                issues.append(
                    f'Character “{record.id}” has ambiguous broader Character IDs.'
                )
            base_id, variant_type = self.version_of(record.id)
            if bool(base_id) != bool(variant_type):
                issues.append(
                    f'Character “{record.id}” has an incomplete version relationship.'
                )
            if base_id:
                base = self.characters.get(base_id)
                if base is None:
                    issues.append(
                        f'Character “{record.id}” references missing broader Character “{base_id}”.'
                    )
                elif base.id == record.id:
                    issues.append(
                        f'Character “{record.id}” cannot be its own broader Character.'
                    )
        for record in self.outfits.values():
            source = self.character_relationship(
                CHARACTER_SOURCE, "outfit", record.id,
            )
            if source.conflicting:
                issues.append(
                    f'Outfit “{record.id}” has ambiguous Character Source IDs.'
                )
            source_id = source.target_id or str(record.series_id or "")
            if source_id and source_id not in self.series:
                issues.append(
                    f'Outfit “{record.id}” references missing Character Source “{source_id}”.'
                )
            owners = self.character_relationship(
                ASSOCIATED_CHARACTER, "outfit", record.id,
            ).target_ids or tuple(record.character_ids)
            missing = sorted({value for value in owners if value not in self.characters})
            if missing:
                issues.append(
                    f'Outfit “{record.id}” references missing Character ID(s): '
                    + ", ".join(missing)
                    + "."
                )
        if issues:
            raise ValueError(" ".join(issues[:3]))

    def entity_tag_link(self, kind: str, record_id: str) -> EffectiveEntityTagLink:
        endpoint_kind = RECORD_KIND_TO_ENDPOINT_KIND.get(kind, kind)
        records = (
            self.series
            if endpoint_kind == "character_source"
            else self.characters
            if endpoint_kind == "character"
            else self.outfits
            if endpoint_kind == "outfit"
            else None
        )
        if records is None:
            raise ValueError(f"Unsupported Character Knowledge record kind {kind!r}.")
        record = records.get(str(record_id))
        compatibility = record.knowledge_tag if record is not None else {}
        projection_fields = self._projection_fields.get(
            "series" if endpoint_kind == "character_source" else endpoint_kind, {}
        ).get(str(record_id), ())
        user_records = self._user_entity_tag_index.get(
            (endpoint_kind, str(record_id)), ()
        )
        default_records = self._default_entity_tag_index.get(
            (endpoint_kind, str(record_id)), ()
        )
        if not user_records and default_records and "knowledge_tag" in projection_fields:
            default_projection = character_record_compatibility_payload(
                "series" if endpoint_kind == "character_source" else endpoint_kind,
                {"id": str(record_id)},
                self._default_relationships,
                _entity_index=self._default_entity_tag_index,
                _character_index=self._default_character_relationship_index,
            ).get("knowledge_tag", {})
            if compatibility != default_projection:
                return effective_entity_tag_link(
                    endpoint_kind, str(record_id), compatibility, {},
                )
        return effective_entity_tag_link(
            endpoint_kind,
            str(record_id),
            compatibility,
            self._entity_tag_index,
        )

    def effective_knowledge_tag(self, kind: str, record_id: str) -> dict[str, Any]:
        return self.entity_tag_link(kind, record_id).effective_link

    def reviewed_knowledge_tag_id(self, kind: str, record_id: str) -> str:
        """Resolve reviewed link authority without treating a candidate as reviewed."""
        link=self.effective_knowledge_tag(kind,record_id)
        if not isinstance(link,dict) or link.get("status")!="reviewed":return ""
        return str(link.get("tag_id") or "")

    def entity_tag_id(self, kind: str, record_id: str) -> str:
        return self.entity_tag_link(kind, record_id).tag_id

    def compatibility_record_payload(
        self, kind: str, record_id: str,
    ) -> dict[str, Any]:
        """Return one explicit old-reader record without changing live storage."""

        records = (
            self.series if kind == "series" else
            self.characters if kind == "character" else
            self.outfits if kind == "outfit" else None
        )
        if records is None:
            raise ValueError(f"Unsupported Character Knowledge record kind {kind!r}.")
        record = records.get(str(record_id))
        if record is None:
            raise KeyError(str(record_id))
        result = record.to_dict()
        link = self.entity_tag_link(kind, str(record_id))
        if link.represented:
            effective_relationships = {
                item.id: item for item in self._default_relationships
            }
            effective_relationships.update(
                (item.id, item) for item in self._user_relationships
            )
            canonical = character_record_compatibility_payload(
                kind,
                {"id": str(record_id)},
                effective_relationships.values(),
                _entity_index=self._entity_tag_index,
                _character_index=self._character_relationship_index,
            ).get("knowledge_tag")
            if canonical:
                result["knowledge_tag"] = canonical
            else:
                result.pop("knowledge_tag", None)
        if kind in {"character", "outfit"}:
            result["series_id"] = self.character_source_id(kind, str(record_id)) or None
        if kind == "outfit":
            result["character_ids"] = list(
                self.associated_character_ids(str(record_id))
            )
        if kind == "character":
            base_id, variant_type = self.version_of(str(record_id))
            if base_id:
                result["base_character_id"] = base_id
                result["variant_type"] = variant_type
            else:
                result.pop("base_character_id", None)
                result.pop("variant_type", None)
        return result

    def character_relationship(
        self,
        relationship_kind: str,
        source_kind: str,
        source_id: str,
    ) -> EffectiveCharacterRelationship:
        compatibility: tuple[str, ...] = ()
        projection_field = ""
        record_kind = source_kind
        if relationship_kind == CHARACTER_SOURCE:
            record = (
                self.characters.get(source_id)
                if source_kind == "character"
                else self.outfits.get(source_id)
                if source_kind == "outfit"
                else None
            )
            compatibility = (
                (str(record.series_id),)
                if record is not None and record.series_id else ()
            )
            projection_field = "series_id"
        elif relationship_kind == ASSOCIATED_CHARACTER and source_kind == "outfit":
            record = self.outfits.get(source_id)
            compatibility = tuple(record.character_ids) if record is not None else ()
            projection_field = "character_ids"
        elif relationship_kind == VERSION_OF and source_kind == "character":
            record = self.characters.get(source_id)
            compatibility = (
                (str(record.base_character_id),)
                if record is not None and record.base_character_id else ()
            )
            projection_field = "base_character_id"
        user_records = self._user_character_relationship_index.get(
            (relationship_kind, source_kind, source_id), ()
        )
        default_records = self._default_character_relationship_index.get(
            (relationship_kind, source_kind, source_id), ()
        )
        fields = self._projection_fields.get(record_kind, {}).get(source_id, ())
        explicitly_projected = projection_field in fields
        if relationship_kind == VERSION_OF:
            explicitly_projected = explicitly_projected or "variant_type" in fields
        if not user_records and default_records and explicitly_projected:
            default_resolved = effective_character_relationship(
                relationship_kind,
                source_kind,
                source_id,
                (),
                self._default_character_relationship_index,
            )
            variant_override = False
            if relationship_kind == VERSION_OF:
                record = self.characters.get(source_id)
                variant_override = (
                    record is not None
                    and str(record.variant_type or "")
                    != str(default_resolved.details.get("variant_type", ""))
                )
            if compatibility != default_resolved.target_ids or variant_override:
                return effective_character_relationship(
                    relationship_kind, source_kind, source_id, compatibility, {},
                )
        return effective_character_relationship(
            relationship_kind,
            source_kind,
            source_id,
            compatibility,
            self._character_relationship_index,
        )

    def character_source_id(self, source_kind: str, source_id: str) -> str:
        return self.character_relationship(
            CHARACTER_SOURCE, source_kind, source_id,
        ).target_id

    def associated_character_ids(self, outfit_id: str) -> tuple[str, ...]:
        return self.character_relationship(
            ASSOCIATED_CHARACTER, "outfit", outfit_id,
        ).target_ids

    def version_of(self, character_id: str) -> tuple[str, str]:
        resolved = self.character_relationship(
            VERSION_OF, "character", character_id,
        )
        record = self.characters.get(character_id)
        variant_type = resolved.details.get("variant_type", "")
        if (
            not variant_type
            and record is not None
            and resolved.target_id == str(record.base_character_id or "")
        ):
            variant_type = str(record.variant_type or "")
        return resolved.target_id, variant_type

    def other_appearance_ids(
        self, source_kind: str, source_id: str,
    ) -> tuple[str, ...]:
        return self.character_relationship(
            OTHER_APPEARANCE, source_kind, source_id,
        ).target_ids

    def species_trait_target_ids(
        self, source_kind: str, source_id: str,
    ) -> tuple[str, ...]:
        return self.character_relationship(
            SPECIES_TRAIT, source_kind, source_id,
        ).target_ids

    def character_ids_for_source(self, source_id: str) -> tuple[str, ...]:
        return tuple(sorted(
            record.id
            for record in self.characters.values()
            if self.character_source_id("character", record.id) == source_id
        ))

    def outfit_ids_for_character(self, character_id: str) -> tuple[str, ...]:
        return tuple(sorted(
            record.id
            for record in self.outfits.values()
            if character_id in self.associated_character_ids(record.id)
        ))

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._validate_cross_entity_references()
        series_payloads = character_record_storage_payloads(
            "series",
            (item.to_dict() for item in self.series.values()),
            self._default_relationships,
            self._user_relationships,
            projection_fields_by_id=self._projection_fields["series"],
        )
        character_payloads = character_record_storage_payloads(
            "character",
            (item.to_dict() for item in self.characters.values()),
            self._default_relationships,
            self._user_relationships,
            projection_fields_by_id=self._projection_fields["character"],
        )
        outfit_payloads = character_record_storage_payloads(
            "outfit",
            (item.to_dict() for item in self.outfits.values()),
            self._default_relationships,
            self._user_relationships,
            projection_fields_by_id=self._projection_fields["outfit"],
        )
        payloads = {
            self.series_path: {
                **self._series_extras,
                "schema_version": self._series_schema_version,
                "series": series_payloads,
            },
            self.characters_path: {
                **self._characters_extras,
                "schema_version": self._characters_schema_version,
                "characters": character_payloads,
            },
            self.outfits_path: {
                **self._outfits_extras,
                "schema_version": self._outfits_schema_version,
                "outfits": outfit_payloads,
            },
        }
        expected_files = dict(self._loaded_files)

        def verify() -> None:
            for path, payload in payloads.items():
                actual = json.loads(path.read_text(encoding="utf-8"))
                if actual != payload:
                    raise RuntimeError(
                        f"Saved {path.name} failed exact read-back verification."
                    )
            CharacterDatabase(self.data_dir, recover_interrupted=False)

        try:
            commit_character_payloads(
                self.data_dir,
                payloads,
                expected_files=expected_files,
                operation="database-save",
                kind="character-knowledge",
                record_id="",
                verify=verify,
                stale_message=(
                    "Character Source, character, outfit, or relationship "
                    "knowledge changed after this database loaded. Reload "
                    "before saving."
                ),
                backup_kind="character-database-save",
                write_json=_atomic_json,
                write_bytes=_atomic_bytes,
            )
        except BaseException:
            try:
                self.load()
            except Exception:
                pass
            raise
        self._projection_fields = {
            "series": {
                str(item.get("id") or ""): frozenset(
                    {"knowledge_tag"}.intersection(item)
                ) for item in series_payloads
            },
            "character": {
                str(item.get("id") or ""): frozenset(
                    {
                        "knowledge_tag", "series_id", "base_character_id",
                        "variant_type",
                    }.intersection(item)
                ) for item in character_payloads
            },
            "outfit": {
                str(item.get("id") or ""): frozenset(
                    {"knowledge_tag", "series_id", "character_ids"}.intersection(item)
                ) for item in outfit_payloads
            },
        }
        self._loaded_files = self._file_snapshot()

    def add_series(
        self,
        name: str,
        aliases: list[str] | None = None,
        original_style_tags: list[str] | None = None,
        knowledge_tag: dict[str, Any] | None = None,
    ) -> SeriesRecord:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Series name cannot be empty.")

        existing = self.find_series(clean_name)
        if existing:
            return existing

        base = _slug(clean_name)
        identifier = base
        counter = 2
        while identifier in self.series:
            identifier = f"{base}_{counter}"
            counter += 1

        record = SeriesRecord(
            identifier, clean_name, aliases or [], original_style_tags or [],
            dict(knowledge_tag or {}),
        )
        self.series[identifier] = record
        self.save()
        return record

    def add_character(
        self,
        name: str,
        *,
        series_id: str | None = None,
        aliases: list[str] | None = None,
        default_tags: list[str] | None = None,
        sdxl_tags: list[str] | None = None,
        plain_language: list[str] | None = None,
        original_style_tags: list[str] | None = None,
        identity_type: str | None = None,
        origin_label: str = "",
        creator: str = "",
        tag_associations: list[dict[str, Any]] | None = None,
        base_character_id: str | None = None,
        variant_type: str = "",
        knowledge_tag: dict[str, Any] | None = None,
    ) -> CharacterRecord:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Character name cannot be empty.")
        if series_id is not None and series_id not in self.series:
            raise KeyError(f"Unknown series_id: {series_id}")
        identity_type = identity_type or ("canonical" if series_id else "original")
        if identity_type not in {"canonical", "original", "generic", "unknown"}:
            raise ValueError(f"Unknown identity type: {identity_type}")
        if bool(base_character_id) != bool(variant_type):
            raise ValueError("A character variation needs both a broader character and a variation type.")
        if variant_type and variant_type not in CHARACTER_IDENTITY_VARIANT_TYPE_LABELS:
            raise ValueError(f"Unknown character variation type: {variant_type}")
        if base_character_id:
            base_character = self.characters.get(base_character_id)
            if base_character is None:
                raise KeyError(f"Unknown base_character_id: {base_character_id}")
            if (
                self.character_source_id("character",base_character.id) or None
            ) != (series_id or None):
                raise ValueError("A character variation and its broader character must share one Character Source.")

        existing = self.find_character(clean_name, series_id=series_id)
        if existing:
            return existing

        base = _slug(f"{clean_name}_{series_id}" if series_id else clean_name)
        identifier = base
        counter = 2
        while identifier in self.characters:
            identifier = f"{base}_{counter}"
            counter += 1

        record = CharacterRecord(
            id=identifier,
            name=clean_name,
            series_id=series_id,
            aliases=aliases or [],
            default_tags=default_tags or [],
            sdxl_tags=sdxl_tags or default_tags or [],
            plain_language=plain_language or [],
            original_style_tags=original_style_tags or [],
            identity_type=identity_type,
            origin_label=origin_label.strip(),
            creator=creator.strip(),
            tag_associations=[dict(value) for value in (tag_associations or [])],
            base_character_id=base_character_id,
            variant_type=variant_type,
            knowledge_tag=dict(knowledge_tag or {}),
        )
        self.characters[identifier] = record
        self.save()
        return record

    def find_series(self, query: str) -> SeriesRecord | None:
        key = _normalized(query)
        for record in self.series.values():
            names = [record.name, *record.aliases]
            if any(_normalized(value) == key for value in names):
                return record
        return None

    @staticmethod
    def _names_collide(values: list[str], record_values: list[str]) -> bool:
        wanted = {_normalized(value) for value in values if _normalized(value)}
        existing = {_normalized(value) for value in record_values if _normalized(value)}
        return bool(wanted.intersection(existing))

    def conflicting_series(self, values: list[str], *, exclude_id: str = "") -> SeriesRecord | None:
        return next((
            record for record in self.series.values()
            if record.id != exclude_id and self._names_collide(values, [record.name, *record.aliases])
        ), None)

    def conflicting_character(
        self, values: list[str], *, series_id: str | None, exclude_id: str = "",
    ) -> CharacterRecord | None:
        return next((
            record for record in self.characters.values()
            if record.id != exclude_id
            and (self.character_source_id("character", record.id) or None) == series_id
            and self._names_collide(values, [record.name, *record.aliases])
        ), None)

    def conflicting_outfit(
        self, values: list[str], *, character_ids: list[str], series_id: str | None,
        exclude_id: str = "",
    ) -> OutfitRecord | None:
        owners = set(character_ids)
        return next((
            record for record in self.outfits.values()
            if record.id != exclude_id
            and (self.character_source_id("outfit", record.id) or None) == (series_id or None)
            and (
                not owners
                or not self.associated_character_ids(record.id)
                or owners.intersection(self.associated_character_ids(record.id))
            )
            and self._names_collide(values, [record.name, *record.aliases])
        ), None)

    def add_outfit(
        self, name: str, *, character_ids: list[str] | None = None,
        series_id: str | None = None, aliases: list[str] | None = None,
        sdxl_tags: list[str] | None = None,
        plain_language: list[str] | None = None,
        style_tags: list[str] | None = None, notes: str = "",
        knowledge_tag: dict[str, Any] | None = None,
    ) -> OutfitRecord:
        clean=name.strip()
        if not clean:
            raise ValueError("Outfit name cannot be empty.")
        unknown=[value for value in (character_ids or []) if value not in self.characters]
        if unknown:
            raise KeyError(f"Unknown character_id: {unknown[0]}")
        if series_id and series_id not in self.series:
            raise KeyError(f"Unknown series_id: {series_id}")
        existing=self.find_outfit(clean,character_ids=character_ids)
        if existing:
            return existing
        base=_slug(clean); identifier=base; counter=2
        while identifier in self.outfits:
            identifier=f"{base}_{counter}"; counter+=1
        record=OutfitRecord(
            id=identifier,
            name=clean,
            character_ids=list(character_ids or []),
            series_id=series_id,
            aliases=list(aliases or []),
            sdxl_tags=list(sdxl_tags or []),
            plain_language=list(plain_language or []),
            style_tags=list(style_tags or []),
            notes=notes.strip(),
            knowledge_tag=dict(knowledge_tag or {}),
        )
        self.outfits[identifier]=record; self.save(); return record

    def find_outfit(self,query:str,*,character_ids:list[str]|None=None) -> OutfitRecord|None:
        key=_normalized(query); wanted=set(character_ids or [])
        for record in self.outfits.values():
            if wanted and not wanted.intersection(self.associated_character_ids(record.id)):
                continue
            if any(_normalized(value)==key for value in [record.name,*record.aliases]):
                return record
        return None

    def search_outfits(self,query:str="",*,character_id:str="") -> list[OutfitRecord]:
        key=_normalized(query)
        return sorted([
            record for record in self.outfits.values()
            if (not character_id or character_id in self.associated_character_ids(record.id))
            and (not key or key in " ".join([record.name,*record.aliases,*record.sdxl_tags]).casefold())
        ],key=lambda item:item.name.casefold())

    def find_character(
        self,
        query: str,
        *,
        series_id: str | None = None,
    ) -> CharacterRecord | None:
        key = _normalized(query)
        if series_id is None and " from " in key:
            name,series_name=key.rsplit(" from ",1); series=self.find_series(series_name)
            if series: return self.find_character(name,series_id=series.id)
        if series_id is None:
            match=re.match(r"^(.+?)\s*\(([^()]+)\)$",key)
            if match:
                series=self.find_series(match.group(2))
                if series:
                    found=self.find_character(match.group(1),series_id=series.id)
                    if found: return found
        for record in self.characters.values():
            record_source_id = self.character_source_id("character", record.id)
            if series_id is not None and record_source_id != series_id:
                continue
            names = [record.name, *record.aliases]
            if series_id is not None:
                names.append(re.sub(r"\s*\([^()]+\)\s*$","",record.name))
            if any(_normalized(value) == key for value in names):
                return record
        return None

    def search_characters(self, query: str = "") -> list[CharacterRecord]:
        key = _normalized(query)
        results = []
        for record in self.characters.values():
            source_id = self.character_source_id("character", record.id)
            series = self.series.get(source_id) if source_id else None
            haystack = " ".join(
                [record.name, *record.aliases, series.name if series else ""]
            ).lower()
            if not key or key in haystack:
                results.append(record)
        return sorted(
            results,
            key=lambda record: (
                self.series.get(self.character_source_id("character", record.id)).name.lower()
                if self.character_source_id("character", record.id) in self.series
                else "",
                record.name.lower(),
            ),
        )

    def character_label(self, character_id: str | None) -> str | None:
        if not character_id:
            return None
        record = self.characters.get(character_id)
        if not record:
            return None
        source_id = self.character_source_id("character", record.id)
        series = self.series.get(source_id) if source_id else None
        return f"{record.name} — {series.name}" if series else record.name
