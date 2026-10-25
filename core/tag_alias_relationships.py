from __future__ import annotations

import copy
import json
import os
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

from .automation_rule_conversion import RulePairAdmission, RuleConversionAdmissionError

from .knowledge_relationships import (
    IDENTITY_ALIAS,
    KnowledgeRelationship,
    RelationshipEndpoint,
)
from .relationship_store import RelationshipStore, validate_relationships
from .relationship_workspace import (
    RelationshipWorkspaceService,
    relationship_fingerprint,
)
from .scoped_constraint_projection import (
    publish_scoped_constraint_storage_authority,
)
from .tag_collection import normalize_tag
from .tag_database import TagDatabase


DEFAULT_ALIAS_COMPILER = "reviewed_tag_aliases_v1"
DEFAULT_ALIAS_SOURCE = "CollectiGen reviewed shipped Tag aliases"
ALIAS_COMPATIBILITY_PROJECTION = "tag.aliases"
DEFAULT_ALIAS_SOURCE_SCHEMA_VERSION = 1
SHIPPED_TAG_ALIAS_SCHEMA_VERSION = 5


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _file_fingerprint(path: str | Path) -> str:
    target=Path(path)
    return sha256(target.read_bytes()).hexdigest() if target.exists() else "missing"


def _atomic_bytes(path: Path,payload: bytes) -> None:
    temporary=path.with_suffix(path.suffix+".restore.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary,path)


def _atomic_json(path: Path,payload: dict[str,Any]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+".tmp")
    temporary.write_text(
        json.dumps(payload,indent=2,ensure_ascii=False)+"\n",encoding="utf-8",
    )
    os.replace(temporary,path)


def tag_alias_relationship_id(alias: str) -> str:
    key=normalize_tag(alias)
    if not key:
        raise ValueError("An Other Name cannot be empty.")
    digest=sha256(key.encode("utf-8")).hexdigest()[:20]
    return f"tag_identity_alias.{digest}"


def build_default_tag_alias_relationships(
    tags: TagDatabase,
    aliases_by_tag: Mapping[str,Iterable[str]]|None=None,
) -> tuple[KnowledgeRelationship, ...]:
    """Compile declared shipped Other Names as exact identity authority.

    The inline ``TagRecord.aliases`` values remain unchanged old-reader
    projections. A redundant alias equal to its own canonical name is omitted;
    empty names, duplicate ownership, canonical-name collisions, and stable-ID
    collisions block the build instead of becoming ambiguous authority.
    """
    display_owners: dict[str, str] = {}
    for record in sorted(tags.tags.values(), key=lambda item: item.id):
        key = normalize_tag(record.display)
        previous = display_owners.setdefault(key, record.id)
        if previous != record.id:
            raise ValueError(
                f"Shipped Tags {previous!r} and {record.id!r} share the canonical "
                f"name {record.display!r}."
            )

    source_aliases=(
        {
            str(tag_id):tuple(str(value) for value in values)
            for tag_id,values in aliases_by_tag.items()
        }
        if aliases_by_tag is not None else None
    )
    if source_aliases is not None:
        unknown=sorted(set(source_aliases)-set(tags.tags))
        if unknown:
            raise ValueError(
                "Reviewed shipped Other Names reference unavailable Tags: "
                +", ".join(unknown[:10])
            )
    alias_owners: dict[str, str] = {}
    relationships: list[KnowledgeRelationship] = []
    for record in sorted(tags.tags.values(), key=lambda item: item.id):
        display_key = normalize_tag(record.display)
        seen_for_owner: set[str] = set()
        declared=(
            source_aliases.get(record.id,())
            if source_aliases is not None else record.aliases
        )
        for alias_order,raw_alias in enumerate(declared):
            alias = str(raw_alias).strip()
            key = normalize_tag(alias)
            if not key:
                raise ValueError(
                    f"Shipped Tag {record.id!r} has an empty declared Other Name."
                )
            if key == display_key:
                continue
            if key in seen_for_owner:
                raise ValueError(
                    f"Shipped Tag {record.id!r} repeats the Other Name {alias!r}."
                )
            seen_for_owner.add(key)
            canonical_owner = display_owners.get(key)
            if canonical_owner is not None and canonical_owner != record.id:
                raise ValueError(
                    f"Shipped Other Name {alias!r} for {record.id!r} is the "
                    f"canonical name of {canonical_owner!r}."
                )
            stable_owner = tags.tags.get(alias)
            if stable_owner is not None and stable_owner.id != record.id:
                raise ValueError(
                    f"Shipped Other Name {alias!r} for {record.id!r} is the "
                    f"stable ID of {stable_owner.id!r}."
                )
            previous_owner = alias_owners.setdefault(key, record.id)
            if previous_owner != record.id:
                raise ValueError(
                    f"Shipped Other Name {alias!r} is declared by both "
                    f"{previous_owner!r} and {record.id!r}."
                )
            relationships.append(
                KnowledgeRelationship(
                    id=tag_alias_relationship_id(alias),
                    kind=IDENTITY_ALIAS,
                    endpoints=(
                        RelationshipEndpoint("alias_text", key, alias),
                        RelationshipEndpoint("tag", record.id, record.display),
                    ),
                    directed=True,
                    scope="any",
                    label="Other Name",
                    reviewed=True,
                    source=DEFAULT_ALIAS_SOURCE,
                    details=(
                        ("compatibility_projection", ALIAS_COMPATIBILITY_PROJECTION),
                        ("default_compiler", DEFAULT_ALIAS_COMPILER),
                        ("alias_order", str(alias_order)),
                    ),
                    layer="default",
                )
            )
    return tuple(relationships)


def tag_alias_source_payload(
    records: Iterable[Any],
) -> dict[str,Any]:
    """Serialize reviewed compiler/recovery input outside live Tag records."""

    return tag_alias_source_payload_from_mapping({
        str(record.id):tuple(
            str(value) for value in record.aliases if str(value).strip()
        )
        for record in records
    })


def tag_alias_source_payload_from_mapping(
    aliases_by_tag: Mapping[str,Iterable[str]],
) -> dict[str,Any]:
    values=[]
    for tag_id,raw_aliases in sorted(aliases_by_tag.items()):
        aliases=[str(value) for value in raw_aliases if str(value).strip()]
        if aliases:
            values.append({"tag_id":str(tag_id),"aliases":aliases})
    return {
        "schema_version":DEFAULT_ALIAS_SOURCE_SCHEMA_VERSION,
        "source":DEFAULT_ALIAS_SOURCE,
        "tags":values,
    }


def load_tag_alias_source(path: str|Path) -> dict[str,tuple[str,...]]:
    payload=json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload,dict):
        raise ValueError("Reviewed shipped Other Names must be one JSON object.")
    if int(payload.get("schema_version",0) or 0)!=DEFAULT_ALIAS_SOURCE_SCHEMA_VERSION:
        raise ValueError("Reviewed shipped Other Names use an unsupported schema.")
    result={}
    for item in payload.get("tags",()):
        if not isinstance(item,dict):
            raise ValueError("Reviewed shipped Other Names contain an invalid row.")
        tag_id=str(item.get("tag_id") or "").strip()
        if not tag_id or tag_id in result:
            raise ValueError("Reviewed shipped Other Names repeat or omit a Tag identity.")
        aliases=tuple(str(value) for value in item.get("aliases",()) if str(value).strip())
        if not aliases:
            raise ValueError(f"Reviewed shipped Other Names for {tag_id!r} are empty.")
        result[tag_id]=aliases
    return result


def tag_alias_source_updates_from_payloads(
    payloads: Iterable[Mapping[str,Any]],
) -> dict[str,tuple[str,...]]:
    """Return complete alias-list edits carried by explicit Tag payloads.

    Pre-retirement layers used ``Tag.aliases`` as their source of truth.  In
    schema-v5 layers, however, the field may contain only an unrepresented
    self-alias retained for exact old-reader output.  Such compatibility data
    must not replace the reviewed compiler/recovery source.  A schema-v5 field
    is authoritative only when its review provenance explicitly includes the
    alias list (including an intentionally empty list).
    """

    updates: dict[str,tuple[str,...]]={}
    for payload in payloads:
        legacy=int(payload.get("schema_version",1) or 1)<SHIPPED_TAG_ALIAS_SCHEMA_VERSION
        for item in payload.get("tags",()):
            if not isinstance(item,Mapping) or "aliases" not in item:
                continue
            reviewed=any(
                isinstance(entry,Mapping)
                and "aliases" in {
                    str(value) for value in entry.get("reviewed_fields",())
                }
                for entry in item.get("provenance",())
            )
            if not legacy and not reviewed:
                continue
            tag_id=str(item.get("id") or "").strip()
            if not tag_id:
                raise ValueError("Reviewed Other Name update omits its Tag identity.")
            updates[tag_id]=tuple(
                str(value) for value in item.get("aliases",())
                if str(value).strip()
            )
    return updates


def retire_shipped_tag_alias_projection_payload(
    payload: Mapping[str,Any],
) -> dict[str,Any]:
    """Remove compiler-only aliases from one validated shipped Tag layer."""

    result=copy.deepcopy(dict(payload))
    result["schema_version"]=max(
        SHIPPED_TAG_ALIAS_SCHEMA_VERSION,
        int(result.get("schema_version",1) or 1),
    )
    for item in result.get("tags",()):
        if isinstance(item,dict):
            display_key=normalize_tag(item.get("display") or item.get("id") or "")
            retained=[
                str(value) for value in item.get("aliases",())
                if normalize_tag(value)==display_key
            ]
            if retained:
                item["aliases"]=retained
            else:
                item.pop("aliases",None)
    return result


@dataclass(frozen=True)
class TagAliasMove:
    alias: str
    from_tag_id: str


@dataclass(frozen=True)
class TagAliasChange:
    tag_id: str
    aliases: tuple[str,...]
    moves: tuple[TagAliasMove,...]=()
    source: str="user_review"
    reason: str=""


@dataclass(frozen=True)
class EffectiveTagAliases:
    tag_id: str
    aliases: tuple[str,...]
    relationship_ids: tuple[str,...]
    represented_aliases: tuple[str,...]
    compatibility_aliases: tuple[str,...]


@dataclass(frozen=True)
class TagAliasCompatibilityIndex:
    """Prebuilt old-reader alias projection state for bulk serializers."""

    relationships: tuple[KnowledgeRelationship,...]
    by_tag: Mapping[str,tuple[KnowledgeRelationship,...]]
    represented_keys: frozenset[str]


@dataclass(frozen=True)
class TagAliasResolution:
    value: str
    normalized: str
    status: str
    tag_id: str=""
    owner_ids: tuple[str,...]=()
    relationship_ids: tuple[str,...]=()
    retired_ids: tuple[str,...]=()
    unavailable_ids: tuple[str,...]=()

    @property
    def resolved(self) -> bool:
        return bool(self.tag_id) and len(self.owner_ids)==1


@dataclass(frozen=True)
class TagAliasSaveResult:
    tag_ids: tuple[str,...]
    relationship_ids: tuple[str,...]
    aliases: tuple[str,...]
    manifest_path: Path


@dataclass(frozen=True)
class TagAliasEvidenceImpact:
    alias: str
    observed_images: int=0
    confirmed_images: int=0
    txt_images: int=0
    metadata_images: int=0
    ai_images: int=0
    available: bool=True


@dataclass(frozen=True)
class TagAliasProposal:
    alias: str
    target_id: str
    target_name: str=""


@dataclass(frozen=True)
class TagAliasPreflightEntry:
    alias: str
    target_id: str
    target_name: str
    status: str
    owner_ids: tuple[str,...]=()
    removed_ids: tuple[str,...]=()
    relationship_ids: tuple[str,...]=()
    impact: TagAliasEvidenceImpact=field(
        default_factory=lambda:TagAliasEvidenceImpact("")
    )

    @property
    def blocked(self) -> bool:
        return self.status in {
            "ambiguous","batch_collision","confirmed_use","impact_unavailable",
            "invalid","owner_collision","removed_identity","stale","target_missing",
        }


@dataclass(frozen=True)
class TagAliasPreflight:
    action: str
    entries: tuple[TagAliasPreflightEntry,...]

    @property
    def blockers(self) -> tuple[TagAliasPreflightEntry,...]:
        return tuple(entry for entry in self.entries if entry.blocked)

    @property
    def can_apply(self) -> bool:
        return bool(self.entries) and not self.blockers


class TagAliasPreflightError(ValueError):
    def __init__(self,plan: TagAliasPreflight) -> None:
        self.plan=plan
        names=", ".join(entry.alias for entry in plan.blockers[:3])
        remainder=max(0,len(plan.blockers)-3)
        suffix=f" and {remainder:,} more" if remainder else ""
        super().__init__(
            "Review the blocked Other Name identity decision"
            f"{'s' if len(plan.blockers)!=1 else ''} before saving: {names}{suffix}."
        )


def tag_alias_evidence_impacts(
    aliases: Iterable[str],records: Iterable[Any]|None,
) -> tuple[TagAliasEvidenceImpact,...]:
    """Count cached evidence by authority class without reading image files."""
    labels=[]; keys={}
    for raw in aliases:
        label=str(raw).strip(); key=normalize_tag(label)
        if key and key not in keys:
            keys[key]=len(labels); labels.append(label)
    if records is None:
        return tuple(TagAliasEvidenceImpact(label,available=False) for label in labels)
    counts=[{"observed":0,"confirmed":0,"txt":0,"metadata":0,"ai":0} for _ in labels]
    for record in records:
        tag_sources={
            normalize_tag(key):tuple(str(value) for value in values)
            for key,values in dict(getattr(record,"tag_sources",{}) or {}).items()
        }
        present={normalize_tag(value) for value in getattr(record,"tags",()) if normalize_tag(value)}
        present.update(tag_sources)
        for key,index in keys.items():
            if key not in present:continue
            counts[index]["observed"]+=1
            sources=tuple(value.casefold() for value in tag_sources.get(key,()))
            if any("confirmed image knowledge" in value for value in sources):
                counts[index]["confirmed"]+=1
            if any("txt" in value for value in sources):
                counts[index]["txt"]+=1
            if any(
                token in value
                for value in sources
                for token in ("metadata","xmp","iptc","exif")
            ):
                counts[index]["metadata"]+=1
            if any(
                token in value
                for value in sources
                for token in ("ai","joycaption","wd tagger","interrogator")
            ):
                counts[index]["ai"]+=1
    return tuple(
        TagAliasEvidenceImpact(
            label,values["observed"],values["confirmed"],values["txt"],
            values["metadata"],values["ai"],True,
        )
        for label,values in zip(labels,counts)
    )


class TagAliasCollisionError(ValueError):
    def __init__(
        self,alias: str,requested_tag_id: str,owner_ids: Iterable[str],*,
        owner_kind: str="alias",
    ) -> None:
        self.alias=str(alias)
        self.requested_tag_id=str(requested_tag_id)
        self.owner_ids=tuple(dict.fromkeys(str(value) for value in owner_ids if str(value)))
        self.owner_kind=str(owner_kind)
        super().__init__(
            f'{self.alias!r} already identifies another live SDXL Tag and cannot '
            "be reassigned without an explicit identity decision."
        )


def _alias_key(relationship: KnowledgeRelationship) -> str:
    if relationship.kind!=IDENTITY_ALIAS or not relationship.directed:
        return ""
    endpoint=relationship.source_endpoint
    if endpoint is None or endpoint.kind!="alias_text":return ""
    return normalize_tag(endpoint.id or endpoint.label)


def _alias_label(relationship: KnowledgeRelationship) -> str:
    endpoint=relationship.source_endpoint
    return str(endpoint.label or endpoint.id) if endpoint is not None else ""


def _alias_target(relationship: KnowledgeRelationship) -> str:
    if relationship.disabled or not relationship.reviewed:return ""
    targets=[item.id for item in relationship.target_endpoints if item.kind=="tag"]
    return targets[0] if len(targets)==1 else ""


def _alias_order(relationship: KnowledgeRelationship) -> int:
    try:
        return max(0,int(dict(relationship.details).get("alias_order", "")))
    except (TypeError,ValueError):
        return 2**31-1


def owns_tag_alias_projection(relationship: KnowledgeRelationship) -> bool:
    """Return whether one canonical alias belongs in the old Tag field view.

    Approved donor-name redirects are identity lookup compatibility, not aliases
    that historically appeared on the surviving Tag record. Every ordinary
    reviewed Other Name does own that explicit old-reader projection.
    """

    if relationship.kind!=IDENTITY_ALIAS or not relationship.directed:
        return False
    details=dict(relationship.details)
    if details.get("compatibility_projection")==ALIAS_COMPATIBILITY_PROJECTION:
        return True
    return details.get("identity_resolution")!="merge_approved"


def _relationship_indexes(
    relationships: Iterable[KnowledgeRelationship],
    tags: TagDatabase | None = None,
) -> tuple[
    dict[str,tuple[KnowledgeRelationship,...]],
    dict[str,tuple[str,...]],
    set[str],
]:
    records: dict[str,list[KnowledgeRelationship]]={}
    for relationship in relationships:
        key=_alias_key(relationship)
        if key:records.setdefault(key,[]).append(relationship)
    frozen={key:tuple(sorted(values,key=lambda item:item.id)) for key,values in records.items()}
    owners={
        key:tuple(sorted({resolved for item in values
                          if (target:=_alias_target(item))
                          if (resolved:=tags.resolve_tag_id(target) if tags is not None else target)}))
        for key,values in frozen.items()
    }
    return frozen,owners,set(frozen)


def _alias_diagnostics(tags, records):
    """Keep nonassignable endpoints without admitting them as active owners."""
    retired, unavailable = set(), set()
    for row in records:
        target = _alias_target(row)
        if not target or tags.resolve_tag_id(target) is not None:
            continue
        current, seen = target, set()
        while current in tags.merged_tag_ids and current not in seen:
            seen.add(current)
            current = tags.merged_tag_ids[current]
        if current not in seen and current in tags.removed_tag_ids and current not in tags.tags:
            retired.add(current)
        else:
            unavailable.add(target)
    return tuple(sorted(retired)), tuple(sorted(unavailable))


def _alias_identity_token(tags):
    context=getattr(tags,"_default_identity_context",None)
    if context is None:
        return None
    return (tags.default_identity_generation,
            id(getattr(tags,"_loaded_bytes",None)),context.revision)


def _represented_resolution(tags, raw, key, owners, records, diagnostics=None):
    retired, unavailable = diagnostics if diagnostics is not None else _alias_diagnostics(tags, records)
    status = ("ambiguous" if len(owners)>1 else "canonical_alias" if owners else
              "unavailable" if unavailable else "retired" if retired else "disabled")
    # An unresolved eligible endpoint prevents selection of a sole live owner.
    # It may represent a second identity rather than harmless disabled history.
    if owners and (retired or unavailable):
        status = "ambiguous"
    return TagAliasResolution(raw,key,status,
        owners[0] if status=="canonical_alias" else "",owners,
        tuple(item.id for item in records),retired,unavailable)


def tag_alias_compatibility_index(
    relationships: Iterable[KnowledgeRelationship],
) -> TagAliasCompatibilityIndex:
    """Build the alias portion of the explicit old-reader projection once."""

    values=tuple(relationships)
    by_tag: dict[str,list[KnowledgeRelationship]]={}
    represented=set()
    for relationship in values:
        if not owns_tag_alias_projection(relationship):
            continue
        key=_alias_key(relationship)
        if not key:
            continue
        represented.add(key)
        target=_alias_target(relationship)
        if target:
            by_tag.setdefault(target,[]).append(relationship)
    return TagAliasCompatibilityIndex(
        values,
        {
            tag_id:tuple(sorted(records,key=lambda item:(_alias_order(item),_alias_key(item))))
            for tag_id,records in by_tag.items()
        },
        frozenset(represented),
    )


def tag_alias_compatibility_payload(
    record: Any,
    relationships: Iterable[KnowledgeRelationship],
    *,
    index: TagAliasCompatibilityIndex|None=None,
) -> dict[str,Any]:
    """Serialize the exact active old-reader ``aliases`` view for one Tag."""

    resolved=(index if index is not None else tag_alias_compatibility_index(relationships))
    canonical={
        _alias_key(item):_alias_label(item)
        for item in resolved.by_tag.get(str(record.id),())
        if _alias_key(item)
    }
    values=[]
    for raw_alias in record.aliases:
        key=normalize_tag(raw_alias)
        if not key:
            continue
        if key in canonical:
            values.append(canonical.pop(key))
        elif key not in resolved.represented_keys:
            values.append(str(raw_alias))
    values.extend(
        canonical[key]
        for key in canonical
    )
    deduplicated=[]; seen=set()
    for value in values:
        key=normalize_tag(value)
        if key and key not in seen:
            seen.add(key); deduplicated.append(value)
    projected=copy.deepcopy(record)
    projected.aliases=deduplicated
    return projected.to_dict()


def publish_tag_alias_storage_authority(
    tags: TagDatabase,
    relationships: Iterable[KnowledgeRelationship],
) -> TagAliasCompatibilityIndex:
    """Publish canonical alias ownership before any ordinary Tag-store write."""

    values=tuple(relationships)
    records,owners,represented=_relationship_indexes(values,tags)
    labels={
        key:next(
            (_alias_label(item) for item in entries if _alias_target(item)),key,
        )
        for key,entries in records.items()
        if len(owners.get(key,()))==1
    }
    storage_represented={
        key for key,entries in records.items()
        if any(
            owns_tag_alias_projection(item)
            and "alias_order" in dict(item.details)
            for item in entries
        )
    }
    owners={key:value if not any(_alias_diagnostics(tags,records[key])) else ()
            for key,value in owners.items()}
    tags.configure_alias_authority(
        owners,represented,alias_labels=labels,
        storage_represented_aliases=storage_represented,
    )
    return tag_alias_compatibility_index(values)


def _display_owners(tags: TagDatabase) -> dict[str,tuple[str,...]]:
    values: dict[str,set[str]]={}
    for record in tags.tags.values():
        key=normalize_tag(record.display)
        if key:values.setdefault(key,set()).add(record.id)
    return {key:tuple(sorted(owners)) for key,owners in values.items()}


def _inline_alias_owners(tags: TagDatabase) -> dict[str,tuple[str,...]]:
    values: dict[str,set[str]]={}
    for record in tags.tags.values():
        for alias in record.aliases:
            key=normalize_tag(alias)
            if key:values.setdefault(key,set()).add(record.id)
    return {key:tuple(sorted(owners)) for key,owners in values.items()}


def _effective_tag_aliases_from_indexes(
    tags: TagDatabase,
    tag_id: str,
    records: Mapping[str, tuple[KnowledgeRelationship, ...]],
    canonical: Mapping[str, tuple[str, ...]],
    represented: set[str],
    displays: Mapping[str, tuple[str, ...]],
    *,
    represented_keys: Iterable[str] | None = None,
) -> EffectiveTagAliases:
    record=tags.tags.get(tag_id)
    if record is None:raise ValueError("The SDXL Tag is no longer available.")
    canonical_labels: dict[str,str]={}
    relationship_ids=[]
    represented_for_tag=[]
    keys=tuple(
        sorted(
            represented if represented_keys is None else represented_keys,
            key=lambda key:(
                min((_alias_order(item) for item in records.get(key,())),default=2**31-1),
                key,
            ),
        )
    )
    for key in keys:
        owners=canonical.get(key,())
        if tag_id not in owners:continue
        represented_for_tag.append(key)
        relationship_ids.extend(item.id for item in records[key])
        if len(owners)!=1 or any(_alias_diagnostics(tags,records[key])):continue
        display_owner=displays.get(key,())
        if display_owner and display_owner!=(tag_id,):continue
        label=next(
            (_alias_label(item) for item in records[key] if (target:=_alias_target(item)) and tags.resolve_tag_id(target)==tag_id),
            key,
        )
        if normalize_tag(label)!=normalize_tag(record.display):canonical_labels[key]=label
    values=[]
    compatibility=[]
    for alias in record.aliases:
        key=normalize_tag(alias)
        if not key:continue
        if key in canonical_labels:
            values.append(canonical_labels.pop(key)); continue
        if key in represented:continue
        if key in displays:continue
        compatibility.append(alias)
        values.append(alias)
    values.extend(canonical_labels[key] for key in keys if key in canonical_labels)
    deduplicated=[]; seen=set()
    for value in values:
        key=normalize_tag(value)
        if key and key not in seen:
            seen.add(key); deduplicated.append(value)
    return EffectiveTagAliases(
        tag_id,tuple(deduplicated),tuple(dict.fromkeys(relationship_ids)),
        tuple(represented_for_tag),tuple(compatibility),
    )


def resolve_tag_alias(
    tags: TagDatabase,
    relationships: Iterable[KnowledgeRelationship],
    value: str,
) -> TagAliasResolution:
    raw=str(value)
    if raw in tags.tags:
        return TagAliasResolution(raw,normalize_tag(raw),"stable_id",raw,(raw,))
    key=normalize_tag(raw)
    if not key:return TagAliasResolution(raw,key,"missing")
    displays=_display_owners(tags).get(key,())
    if displays:
        return TagAliasResolution(
            raw,key,"canonical_name" if len(displays)==1 else "ambiguous",
            displays[0] if len(displays)==1 else "",displays,
        )
    records,canonical,represented=_relationship_indexes(relationships,tags)
    if key in represented:
        owners=canonical.get(key,())
        return _represented_resolution(tags,raw,key,owners,records.get(key,()))
    owners=_inline_alias_owners(tags).get(key,())
    return TagAliasResolution(
        raw,key,
        "compatibility_alias" if len(owners)==1 else "ambiguous" if owners else "missing",
        owners[0] if len(owners)==1 else "",owners,
    )


def effective_tag_aliases(
    tags: TagDatabase,
    relationships: Iterable[KnowledgeRelationship],
    tag_id: str,
) -> EffectiveTagAliases:
    tag_id=str(tag_id)
    records,canonical,represented=_relationship_indexes(relationships,tags)
    displays=_display_owners(tags)
    return _effective_tag_aliases_from_indexes(
        tags,tag_id,records,canonical,represented,displays,
    )


def _history_entry(relationship: KnowledgeRelationship) -> dict[str,Any]:
    return {
        "changed_at":_utc_now(),"relationship":relationship.to_dict(),
        "relationship_fingerprint":relationship_fingerprint(relationship),
    }


def _updated_alias_relationship(
    current: KnowledgeRelationship|None,*,relationship_id: str,alias: str,
    target_id: str,target_label: str,disabled: bool,source: str,reason: str,
    alias_order: int,
) -> KnowledgeRelationship:
    endpoints=(
        RelationshipEndpoint("alias_text",normalize_tag(alias),alias),
        RelationshipEndpoint("tag",target_id,target_label),
    )
    if current is None:
        return KnowledgeRelationship(
            id=relationship_id,kind=IDENTITY_ALIAS,endpoints=endpoints,
            directed=True,scope="any",label="Other Name",reviewed=True,
            source=source,layer="user",disabled=disabled,
            details=(
                ("compatibility_projection",ALIAS_COMPATIBILITY_PROJECTION),
                ("alias_order",str(max(0,alias_order))),
            ),
            extras={"alias_authority":{"reason":reason,"created_at":_utc_now()}},
        )
    history=[
        dict(item) for item in current.extras.get("alias_history",())
        if isinstance(item,Mapping)
    ]
    extras=copy.deepcopy(current.extras)
    extras["alias_history"]=[*history,_history_entry(current)]
    extras["alias_authority"]={"reason":reason,"changed_at":_utc_now()}
    details=[
        (str(key),str(value)) for key,value in current.details
        if str(key) not in {"compatibility_projection","alias_order"}
    ]
    details.extend((
        ("compatibility_projection",ALIAS_COMPATIBILITY_PROJECTION),
        ("alias_order",str(max(0,alias_order))),
    ))
    return replace(
        current,endpoints=endpoints,directed=True,reviewed=True,source=source,
        version=current.version+1,layer="user",disabled=disabled,
        details=tuple(details),extras=extras,
    )


def stage_tag_alias_changes(
    tags: TagDatabase,
    workspace: RelationshipWorkspaceService,
    changes: Iterable[TagAliasChange],
) -> tuple[dict[str,KnowledgeRelationship],tuple[str,...],tuple[str,...]]:
    """Stage canonical alias decisions and synchronized old-reader projections."""
    user_values=dict(workspace.user_store.relationships)
    effective=dict(workspace.default_store.relationships); effective.update(user_values)
    changed_ids=[]; affected_keys=[]; desired_order: dict[str,list[str]]={}
    for change in changes:
        tag_id=str(change.tag_id)
        record=tags.tags.get(tag_id)
        if record is None:raise ValueError("The SDXL Tag being edited is no longer available.")
        desired=[]; desired_by_key={}
        for raw_alias in change.aliases:
            alias=str(raw_alias).strip(); key=normalize_tag(alias)
            if not key or key==normalize_tag(record.display) or key in desired_by_key:continue
            desired.append(alias); desired_by_key[key]=alias
        desired_order[tag_id]=desired
        desired_positions={
            normalize_tag(value):index for index,value in enumerate(desired)
        }
        moves={normalize_tag(item.alias):str(item.from_tag_id) for item in change.moves}
        current_effective=effective_tag_aliases(tags,effective.values(),tag_id)
        old_by_key={normalize_tag(alias):alias for alias in current_effective.aliases}
        records_by_key,canonical_owners,represented=_relationship_indexes(effective.values(),tags)
        displays=_display_owners(tags); inline=_inline_alias_owners(tags)
        relevant=set(desired_by_key)|set(old_by_key)
        for key in desired_by_key:
            display_owners=tuple(value for value in displays.get(key,()) if value!=tag_id)
            if display_owners:
                raise TagAliasCollisionError(
                    desired_by_key[key],tag_id,display_owners,owner_kind="canonical_name",
                )
            owners=tuple(value for value in canonical_owners.get(key,()) if value!=tag_id)
            if not owners and key not in represented:
                owners=tuple(value for value in inline.get(key,()) if value!=tag_id)
            if owners and (len(owners)!=1 or moves.get(key)!=owners[0]):
                raise TagAliasCollisionError(desired_by_key[key],tag_id,owners)
        for key in sorted(relevant):
            alias=desired_by_key.get(key) or old_by_key[key]
            current_records=records_by_key.get(key,())
            primary=current_records[0] if current_records else None
            if (
                key in desired_by_key
                and key in old_by_key
                and alias==old_by_key[key]
                and key not in moves
                and primary is not None
                and not primary.disabled
                and primary.reviewed
                and _alias_target(primary)==tag_id
            ):
                continue
            relationship_id=(
                primary.id if primary is not None else tag_alias_relationship_id(alias)
            )
            reserved=effective.get(relationship_id)
            if reserved is not None and reserved.kind!=IDENTITY_ALIAS:
                raise ValueError("The reserved Other Name relationship ID is already in use.")
            enabled=key in desired_by_key
            updated=_updated_alias_relationship(
                primary,relationship_id=relationship_id,alias=alias,
                target_id=tag_id,target_label=record.display,disabled=not enabled,
                source=str(change.source or "user_review"),reason=str(change.reason or ""),
                alias_order=desired_positions.get(
                    key,_alias_order(primary) if primary is not None else len(desired),
                ),
            )
            user_values[updated.id]=updated; effective[updated.id]=updated
            changed_ids.append(updated.id); affected_keys.append(key)
            for extra in current_records[1:]:
                disabled=replace(
                    extra,layer="user",source=str(change.source or "user_review"),
                    version=extra.version+1,disabled=True,
                    extras={
                        **copy.deepcopy(extra.extras),
                        "alias_history":[
                            *[
                                dict(item) for item in extra.extras.get("alias_history",())
                                if isinstance(item,Mapping)
                            ],
                            _history_entry(extra),
                        ],
                    },
                )
                user_values[disabled.id]=disabled; effective[disabled.id]=disabled
                changed_ids.append(disabled.id)

    affected=set(affected_keys)
    records_by_key,canonical_owners,_represented=_relationship_indexes(effective.values(),tags)
    displays=_display_owners(tags)
    for record in tags.tags.values():
        record.aliases=[
            value for value in record.aliases if normalize_tag(value) not in affected
        ]
    labels_by_target: dict[str,list[str]]={}
    for key in affected:
        owners=canonical_owners.get(key,())
        if len(owners)>1:
            raise TagAliasCollisionError(key,"",owners)
        if not owners:continue
        target_id=owners[0]
        display_owners=displays.get(key,())
        if display_owners and display_owners!=(target_id,):
            raise TagAliasCollisionError(key,target_id,display_owners,owner_kind="canonical_name")
        label=next(
            (_alias_label(item) for item in records_by_key[key] if _alias_target(item)==target_id),
            key,
        )
        labels_by_target.setdefault(target_id,[]).append(label)
    for target_id,labels in labels_by_target.items():
        record=tags.tags.get(target_id)
        if record is None:continue
        preferred=desired_order.get(target_id,[])
        order={normalize_tag(value):index for index,value in enumerate(preferred)}
        labels.sort(key=lambda value:(order.get(normalize_tag(value),len(order)),normalize_tag(value)))
        for label in labels:
            if normalize_tag(label)!=normalize_tag(record.display):record.aliases.append(label)
    # Preserve the reviewed order even when an unchanged alias is supplied by
    # the default relationship layer rather than the old-reader projection.
    # The Tag record remains a compatibility projection; this does not create
    # a redundant user-layer relationship override.
    for target_id,preferred in desired_order.items():
        record=tags.tags.get(target_id)
        if record is None:continue
        record.aliases=[
            value for value in preferred
            if normalize_tag(value)
            and normalize_tag(value)!=normalize_tag(record.display)
        ]
    issues=validate_relationships(effective.values())
    if issues:raise ValueError(" ".join(issue.message for issue in issues[:3]))
    tags._rebuild_lookup()
    return user_values,tuple(dict.fromkeys(changed_ids)),tuple(dict.fromkeys(affected_keys))


class TagAliasRelationshipService:
    """Canonical Other Name authority with synchronized Tag-record projections."""

    def __init__(
        self,tags: TagDatabase,*,default_path: str|Path,user_path: str|Path,
        workspace: RelationshipWorkspaceService|None=None,
    ) -> None:
        self.tags=tags
        self.rule_pair_admission=getattr(tags,"rule_pair_admission",None)
        if self.rule_pair_admission is not None and not self.rule_pair_admission.owns(tags.path,"knowledge"):
            raise ValueError("Other Names must use their exact Knowledge participant.")
        self.workspace=workspace or RelationshipWorkspaceService(
            default_path,user_path,tags=tags,
        )
        self._authority_fingerprints=((
            str(self.workspace.default_store.path.resolve()),
            _file_fingerprint(self.workspace.default_store.path),
        ),)
        self._rebuild_index()

    def _admission(self):
        binding=self.rule_pair_admission
        if getattr(self.tags,"rule_pair_admission",None)!=binding or (
            binding is not None and (
                not binding.owns(self.tags.path,"knowledge")
                or self.workspace.user_store.path.absolute()!=binding.root/"relationships.json"
            )
        ):
            raise RuleConversionAdmissionError("Saved Knowledge changed. Reopen Other Names before saving.")
        return binding.admit() if binding else nullcontext()

    def _rebuild_index(self) -> None:
        effective=self.workspace.effective
        self._records,self._owners,self._represented=_relationship_indexes(
            effective,self.tags,
        )
        self._diagnostics={key:_alias_diagnostics(self.tags,rows)
                           for key,rows in self._records.items()}
        # Only unambiguously assignable names enter TagDatabase identity owners.
        authority_owners={key:owners if not any(self._diagnostics[key]) else ()
                          for key,owners in self._owners.items()}
        self._displays=dict(self.tags._display_name_owners)
        self._inline=dict(self.tags._inline_alias_owners)
        keys_by_owner: dict[str,list[str]]={}
        for key,owners in self._owners.items():
            for owner in owners:
                keys_by_owner.setdefault(owner,[]).append(key)
        self._keys_by_owner={
            owner:tuple(sorted(keys)) for owner,keys in keys_by_owner.items()
        }
        labels={
            key:next(
                (_alias_label(item) for item in records if _alias_target(item)),key,
            )
            for key,records in self._records.items()
            if len(self._owners.get(key,()))==1
        }
        self.tags.configure_alias_authority(
            authority_owners,self._represented,alias_labels=labels,
        )
        publish_scoped_constraint_storage_authority(
            self.tags,effective,
        )
        self._index_identity=_alias_identity_token(self.tags)

    def _require_index_identity(self):
        from .default_tag_identity import DefaultTagIdentityHold
        current=_alias_identity_token(self.tags)
        if current != self._index_identity:
            raise DefaultTagIdentityHold("Alias identity context changed; explicitly rebuild")

    def resolve(self,value: str) -> TagAliasResolution:
        self._require_index_identity()
        raw=str(value)
        if raw in self.tags.tags:
            return TagAliasResolution(raw,normalize_tag(raw),"stable_id",raw,(raw,))
        key=normalize_tag(raw)
        if not key:return TagAliasResolution(raw,key,"missing")
        displays=self._displays.get(key,())
        if displays:
            return TagAliasResolution(
                raw,key,"canonical_name" if len(displays)==1 else "ambiguous",
                displays[0] if len(displays)==1 else "",displays,
            )
        if key in self._represented:
            owners=self._owners.get(key,())
            return _represented_resolution(self.tags,raw,key,owners,
                self._records.get(key,()),self._diagnostics.get(key))
        owners=self._inline.get(key,())
        return TagAliasResolution(
            raw,key,
            "compatibility_alias" if len(owners)==1 else "ambiguous" if owners else "missing",
            owners[0] if len(owners)==1 else "",owners,
        )

    def effective_for_tag(self,tag_id: str) -> EffectiveTagAliases:
        self._require_index_identity()
        stable_id=str(tag_id)
        return _effective_tag_aliases_from_indexes(
            self.tags,stable_id,self._records,self._owners,self._represented,
            self._displays,represented_keys=self._keys_by_owner.get(stable_id,()),
        )

    def preflight(
        self,proposals: Iterable[TagAliasProposal],*,records: Iterable[Any]|None=None,
        action: str="attach",
    ) -> TagAliasPreflight:
        """Resolve alias lifecycle and cached source impact without reading files."""
        if action not in {"attach","remove"}:
            raise ValueError("Other Name preflight supports attach or remove only.")
        prepared=[]; targets_by_key: dict[str,set[str]]={}
        for proposal in proposals:
            alias=str(proposal.alias).strip(); target_id=str(proposal.target_id)
            target_name=str(proposal.target_name).strip()
            key=normalize_tag(alias)
            prepared.append((alias,key,target_id,target_name))
            if key:targets_by_key.setdefault(key,set()).add(target_id)
        impacts={
            normalize_tag(item.alias):item
            for item in tag_alias_evidence_impacts(
                (alias for alias,_key,_target,_name in prepared),records,
            )
        }
        entries=[]; seen=set()
        for alias,key,target_id,proposed_target_name in prepared:
            duplicate_key=(key,target_id)
            if duplicate_key in seen:continue
            seen.add(duplicate_key)
            target=self.tags.tags.get(target_id)
            target_name=target.display if target is not None else proposed_target_name
            impact=impacts.get(key,TagAliasEvidenceImpact(alias,available=False))
            status="ready"; owners=(); removed_ids=(); relationship_ids=()
            if not key:
                status="invalid"
            elif target is None and not proposed_target_name:
                status="target_missing"
            elif len(targets_by_key.get(key,()))>1:
                status="batch_collision"; owners=tuple(sorted(targets_by_key[key]))
            else:
                resolution=self.resolve(alias)
                owners=resolution.owner_ids
                relationship_ids=resolution.relationship_ids
                if action=="remove":
                    if target_id not in owners:
                        status="stale"
                    elif not impact.available:
                        status="impact_unavailable"
                    elif impact.confirmed_images:
                        status="confirmed_use"
                    else:
                        status="ready_remove"
                elif resolution.status=="ambiguous":
                    status="ambiguous"
                elif owners:
                    status=(
                        "unchanged"
                        if owners==(target_id,) else
                        "ambiguous" if len(owners)>1 else "owner_collision"
                    )
                else:
                    removed_ids=tuple(sorted(set(self.tags.removed_identity_ids(alias))
                                             | set(resolution.retired_ids)))
                    if removed_ids:
                        status="removed_identity"
                    elif resolution.status=="ambiguous":
                        status="ambiguous"
                    elif resolution.status=="unavailable":
                        status="target_missing"
                    elif resolution.status=="disabled":
                        status="retired"
                    else:
                        status="ready"
            entries.append(TagAliasPreflightEntry(
                alias,target_id,target_name,status,
                owners,removed_ids,relationship_ids,impact,
            ))
        return TagAliasPreflight(action,tuple(entries))

    def _verify_authority(self) -> None:
        for path,expected in self._authority_fingerprints:
            if _file_fingerprint(path)!=expected:
                raise RuntimeError(
                    "Shipped relationship authority changed after this Tag was opened. Reload and review it again."
                )
        relationship_path=self.workspace.user_store.path
        relationship_bytes=relationship_path.read_bytes() if relationship_path.exists() else None
        if relationship_bytes!=self.workspace.user_store._loaded_bytes:
            raise RuntimeError(
                "Relationships changed after this Tag was opened. Reload and review it again."
            )
        tag_path=self.tags.path
        tag_bytes=tag_path.read_bytes() if tag_path.exists() else None
        if tag_bytes!=getattr(self.tags,"_loaded_bytes",None):
            raise RuntimeError(
                "System Knowledge changed after this Tag was opened. Reload and review it again."
            )

    def apply(
        self,changes: Iterable[TagAliasChange],*,backup_root: str|Path|None=None,
    ) -> TagAliasSaveResult:
        with self._admission():
            return self._apply_admitted(changes,backup_root=backup_root)

    def _apply_admitted(
        self,changes: Iterable[TagAliasChange],*,backup_root: str|Path|None=None,
    ) -> TagAliasSaveResult:
        changes=tuple(changes)
        if not changes:raise ValueError("No Other Name change was provided.")
        additions=[]
        for change in changes:
            current={
                normalize_tag(value)
                for value in self.effective_for_tag(str(change.tag_id)).aliases
            }
            additions.extend(
                TagAliasProposal(value,str(change.tag_id))
                for value in change.aliases
                if normalize_tag(value) and normalize_tag(value) not in current
            )
        if additions:
            plan=self.preflight(additions)
            lifecycle_blockers=tuple(
                entry for entry in plan.blockers
                if entry.status=="removed_identity"
            )
            if lifecycle_blockers:
                raise TagAliasPreflightError(TagAliasPreflight(
                    plan.action,lifecycle_blockers,
                ))
        self._verify_authority()
        original_user_values=dict(self.workspace.user_store.relationships)
        original_tags=copy.deepcopy(self.tags.tags)
        try:
            user_values,changed_ids,affected=stage_tag_alias_changes(
                self.tags,self.workspace,changes,
            )
        except BaseException:
            self.tags.tags=original_tags; self.tags._rebuild_lookup(); raise
        relationship_path=self.workspace.user_store.path; tag_path=self.tags.path
        relationship_bytes=relationship_path.read_bytes() if relationship_path.exists() else None
        tag_bytes=tag_path.read_bytes() if tag_path.exists() else None
        try:
            self.workspace.user_store.replace(user_values.values())
            relationship_payload=self.workspace.user_store.payload()
            effective=dict(self.workspace.default_store.relationships)
            effective.update(user_values)
            publish_tag_alias_storage_authority(
                self.tags,effective.values(),
            )
            publish_scoped_constraint_storage_authority(
                self.tags,effective.values(),
            )
            tag_payload=self.tags._payload()
            self.workspace.user_store.replace(original_user_values.values())
            root=(
                Path(backup_root) if backup_root is not None
                else tag_path.parent/"backups"/"relationship-tag-aliases"
            )
            stamp=datetime.now().strftime("%Y%m%d_%H%M%S")
            backup=root/f"{stamp}_{uuid4().hex[:8]}"; backup.mkdir(parents=True,exist_ok=False)
            entries=[]
            for index,(path,payload) in enumerate(((relationship_path,relationship_bytes),(tag_path,tag_bytes))):
                backup_name=f"{index}_{path.name}"
                if payload is not None:(backup/backup_name).write_bytes(payload)
                entries.append({"target":str(path.resolve()),"backup":backup_name,"existed":payload is not None})
            manifest_path=backup/"recovery.json"
            manifest={
                "schema_version":1,"status":"pending","created_at":_utc_now(),
                "kind":"tag_alias_relationships",
                "tag_ids":[change.tag_id for change in changes],
                "relationship_ids":list(changed_ids),"aliases":list(affected),
                "files":entries,
            }
            _atomic_json(manifest_path,manifest)
        except BaseException:
            self.workspace.user_store.replace(original_user_values.values())
            self.tags.load(); self._rebuild_index(); raise
        try:
            _atomic_json(relationship_path,relationship_payload)
            _atomic_json(tag_path,tag_payload)
            saved_relationships=RelationshipStore(relationship_path)
            saved_tags=TagDatabase(tag_path,rule_pair_admission=self.rule_pair_admission)
            if saved_relationships.payload()!=relationship_payload:
                raise RuntimeError("Other Name save failed read-back verification.")
            saved_effective=dict(self.workspace.default_store.relationships)
            saved_effective.update(saved_relationships.relationships)
            publish_tag_alias_storage_authority(
                saved_tags,saved_effective.values(),
            )
            if saved_tags._payload()!=tag_payload:
                raise RuntimeError("Other Name compatibility projection failed read-back verification.")
            verified_workspace=RelationshipWorkspaceService(
                self.workspace.default_store.path,relationship_path,
                tags=saved_tags,
            )
            for change in changes:
                resolved=effective_tag_aliases(
                    saved_tags,verified_workspace.effective,str(change.tag_id),
                )
                wanted=tuple(dict.fromkeys(
                    normalize_tag(value) for value in change.aliases
                    if normalize_tag(value)
                    and normalize_tag(value)!=normalize_tag(saved_tags.tags[str(change.tag_id)].display)
                ))
                actual=tuple(normalize_tag(value) for value in resolved.aliases)
                if actual!=wanted:
                    raise RuntimeError("Other Names failed canonical read-back verification.")
        except BaseException:
            for entry in entries:
                target=Path(entry["target"]); saved=backup/entry["backup"]
                if entry["existed"]:_atomic_bytes(target,saved.read_bytes())
                elif target.exists():target.unlink()
            self.workspace.default_store.load(); self.workspace.user_store.load(); self.tags.load()
            manifest["status"]="restored"; manifest["restored_at"]=_utc_now()
            _atomic_json(manifest_path,manifest); self._rebuild_index(); raise
        manifest["status"]="complete"; manifest["completed_at"]=_utc_now()
        _atomic_json(manifest_path,manifest)
        self.workspace=verified_workspace; self._rebuild_index()
        self.tags._loaded_bytes=tag_path.read_bytes()
        return TagAliasSaveResult(
            tuple(dict.fromkeys(str(change.tag_id) for change in changes)),
            changed_ids,affected,manifest_path,
        )

    @classmethod
    def recover_interrupted(cls,data_root: str|Path) -> tuple[Path,...]:
        with RulePairAdmission(Path(data_root).absolute()).admit():
            return cls._recover_interrupted_admitted(data_root)

    @classmethod
    def _recover_interrupted_admitted(cls,data_root: str|Path) -> tuple[Path,...]:
        root=Path(data_root).resolve()
        backup_root=root/"backups"/"relationship-tag-aliases"
        restored=[]
        if not backup_root.exists():return ()
        allowed={"relationships.json","tags.json"}
        for manifest_path in sorted(backup_root.glob("*/recovery.json")):
            manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("status")!="pending":continue
            entries=[]; seen_targets=set()
            for raw_entry in manifest.get("files",()):
                entry=dict(raw_entry)
                target=Path(str(entry.get("target") or "")).resolve()
                backup=(manifest_path.parent/str(entry.get("backup") or "")).resolve()
                if target.parent!=root or target.name not in allowed:
                    raise RuntimeError("Other Name recovery manifest contains an unsafe target.")
                if backup.parent!=manifest_path.parent.resolve():
                    raise RuntimeError("Other Name recovery manifest contains an unsafe backup path.")
                if target in seen_targets:
                    raise RuntimeError("Other Name recovery manifest repeats a target.")
                if entry.get("existed") and not backup.is_file():
                    raise RuntimeError("Other Name recovery backup is missing.")
                seen_targets.add(target); entries.append((entry,target,backup))
            for entry,target,backup in entries:
                if entry.get("existed"):_atomic_bytes(target,backup.read_bytes())
                elif target.exists():target.unlink()
            manifest["status"]="restored"; manifest["restored_at"]=_utc_now()
            _atomic_json(manifest_path,manifest); restored.append(manifest_path)
        return tuple(restored)
