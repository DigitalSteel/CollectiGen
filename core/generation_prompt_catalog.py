"""Read-only prompt choices carried by the existing portable GenerationPlan.

This is a dated detached projection, never a Knowledge store or import authority.
Only deliberately applied prompt text affects a request. No file discovery or IO.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from types import MappingProxyType

from .generation_planning import GenerationPlan, GenerationPlanValidationError

CATALOG_KEY = "organizer_prompt_catalog"
MAX_TAGS = 50_000
MAX_ENTITIES = 10_000
MAX_NAMES = 256
MAX_CATALOG_BYTES = 12 * 1024 * 1024


def _text(value, *, empty=False):
    if type(value) is not str or len(value) > 4096 or (not empty and not value.strip()):
        raise GenerationPlanValidationError("Prompt catalog text is missing or too long.")
    return value


def _texts(value):
    if type(value) is not list or len(value) > MAX_NAMES:
        raise GenerationPlanValidationError("Prompt catalog names require a bounded list.")
    return [_text(item) for item in value]


def _rows(value, limit):
    if type(value) is not list or len(value) > limit or any(type(row) is not dict for row in value):
        raise GenerationPlanValidationError("Prompt catalog entries require a bounded object list.")
    seen = set()
    for row in value:
        identity = _text(row.get("id"))
        if identity in seen:
            raise GenerationPlanValidationError("Prompt catalog entries have duplicate identities.")
        seen.add(identity)
    return value


@dataclass(frozen=True)
class PromptTag:
    id: str
    display: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class PromptCatalog:
    """Validated immutable data; no native paths, methods, writers or authority."""
    _encoded: bytes

    @classmethod
    def from_payload(cls, payload):
        if type(payload) is not dict or set(payload) != {"version", "tags", "characters", "outfits"}:
            raise GenerationPlanValidationError("Unsupported prompt catalog fields.")
        if type(payload["version"]) is not int or payload["version"] != 1:
            raise GenerationPlanValidationError("Unsupported prompt catalog version.")
        tags, characters, outfits = [], [], []
        for row in _rows(payload["tags"], MAX_TAGS):
            if set(row) != {"id", "display", "aliases"}:
                raise GenerationPlanValidationError("Unsupported prompt Tag fields.")
            tags.append({"id": _text(row["id"]), "display": _text(row["display"]),
                         "aliases": _texts(row["aliases"])})
        for row in _rows(payload["characters"], MAX_ENTITIES):
            if set(row) != {"id", "name"}:
                raise GenerationPlanValidationError("Unsupported prompt Character fields.")
            characters.append({"id": _text(row["id"]), "name": _text(row["name"])})
        for row in _rows(payload["outfits"], MAX_ENTITIES):
            if set(row) != {"id", "name", "text", "reviewed", "associated_character_ids"} or row["reviewed"] is not True:
                raise GenerationPlanValidationError("Prompt Outfits require explicit reviewed text.")
            outfits.append({"id": _text(row["id"]), "name": _text(row["name"]),
                "text": _text(row["text"], empty=True), "reviewed": True,
                "associated_character_ids": _texts(row["associated_character_ids"])})
        encoded = json.dumps({"version": 1, "tags": tags, "characters": characters, "outfits": outfits},
                             ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_CATALOG_BYTES:
            raise GenerationPlanValidationError("Prompt catalog exceeds the portable draft byte limit.")
        return cls(encoded)

    @property
    def payload(self):
        return json.loads(self._encoded)

    @property
    def fingerprint(self):
        return sha256(self._encoded).hexdigest()

    @property
    def tags(self):
        return MappingProxyType({row["id"]: PromptTag(row["id"], row["display"], tuple(row["aliases"]))
                                 for row in self.payload["tags"]})

    @staticmethod
    def identity_names(record):
        return (record.display, *record.aliases)

    @property
    def characters(self):
        return tuple(self.payload["characters"])

    @property
    def outfits(self):
        return tuple(self.payload["outfits"])

    @property
    def summary(self):
        value = self.payload
        return f'{len(value["tags"]):,} Tags, {len(value["characters"]):,} Characters and {len(value["outfits"]):,} Outfits'


def capture_prompt_catalog(knowledge, characters):
    """Project only already-loaded canonical prompt names and associations.

    Caller owns UI save/readiness and explicit export consent. No native store
    is opened, recovered or written here. Unknown native fields stay at source.
    """
    if len(knowledge.tags) > MAX_TAGS or len(characters.characters) > MAX_ENTITIES or len(characters.outfits) > MAX_ENTITIES:
        raise GenerationPlanValidationError("Prompt catalog is too large; source records were not truncated.")
    payload = {"version": 1,
        "tags": [{"id": row.id, "display": row.display, "aliases": list(knowledge.identity_names(row))}
                 for row in knowledge.tags.values()],
        "characters": [{"id": row.id, "name": row.name} for row in characters.characters.values()],
        "outfits": [{"id": row.id, "name": row.name, "text": ", ".join(row.sdxl_tags),
                    "reviewed": True, "associated_character_ids": list(characters.associated_character_ids(row.id))}
                   for row in characters.outfits.values()]}
    return PromptCatalog.from_payload(payload)


def catalog_draft(catalog):
    if type(catalog) is not PromptCatalog:
        raise GenerationPlanValidationError("A validated prompt catalog is required.")
    return GenerationPlan.create(None, goal="New image using Organizer prompt catalog").revise(
        {"target_context": {CATALOG_KEY: catalog.payload}}, source="organizer_prompt_catalog",
        consequence="Retained read-only prompt choices; no Image or System Knowledge changed")


def catalog_from_plan(plan):
    value = plan.target_context
    return PromptCatalog.from_payload(value[CATALOG_KEY]) if CATALOG_KEY in value else None
