from __future__ import annotations

import copy
import json
from dataclasses import dataclass,replace
from hashlib import sha256
from pathlib import Path
from typing import Any

from .category_registry import CategoryRecord,CategoryRegistry
from .category_base_relationships import (
    category_base_compatibility_payload,
    category_base_relationship_index,
)
from .default_sync import (
    load_effective_tag_defaults,
    shipped_tag_records,
)
from .knowledge_engine import FoundationKnowledge
from .knowledge_relationships import ENTAILMENT,SPECIFICITY,KnowledgeRelationship
from .relationship_store import RelationshipStore,validate_relationships
from .tag_collection import normalize_tag
from .tag_database import RemovedTagArchive,TagDatabase,TagRecord
from .tag_alias_relationships import (
    TagAliasCompatibilityIndex,
    publish_tag_alias_storage_authority,
    tag_alias_compatibility_index,
)
from .tag_semantic_relationships import (
    publish_tag_semantic_authority,
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


PORTABLE_REMOVAL_SCHEMA_VERSION=1
MAX_PORTABLE_REMOVAL_ARCHIVES=20_000
MAX_PORTABLE_REMOVAL_TAGS=20_000
MAX_PORTABLE_TRANSITION_RECORDS=100_000
REMOVAL_HOLD="hold"
_RECOVERY_KINDS=frozenset({"current_default","archived_user"})
_RELATIONSHIP_RECOVERY_KINDS=frozenset({"inherit_default","archived_user"})
_IMPORT_FINGERPRINT_KEY="knowledge_pack_source_fingerprint"
_IMPORT_ENVELOPE_KEY="knowledge_pack_source_envelope"
_TAG_TRANSITION_PATHS=frozenset({
    ("relations","parent"),
    ("relations","attributes"),
    ("relations","implies"),
    ("relations","conflicts"),
})
_CATEGORY_TRANSITION_PATHS=frozenset({
    ("base_tag_id",),
    ("base_tag_policy",),
})
_RELATIONSHIP_TRANSITION_PATHS=frozenset({
    ("endpoints",),("version",),("layer",),("source",),("disabled",),
    ("tag_removal",),
})


@dataclass(frozen=True)
class KnowledgePackRemovalDecision:
    stable_id: str
    label: str
    tag_ids: tuple[str,...]
    outcome: str
    reason: str


@dataclass(frozen=True)
class RemovedArchivePreparation:
    decisions: tuple[KnowledgePackRemovalDecision,...]
    tags_payload: dict[str,Any]
    categories_payload: dict[str,Any]
    relationships_payload: dict[str,Any]


def _read_payload(path: Path,*,schema_version: int=1,key: str="") -> dict[str,Any]:
    if not path.exists():
        return {"schema_version":schema_version,**({key:[]} if key else {})}
    payload=json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload,dict):
        raise ValueError(f"{path.name} must contain a JSON object.")
    return payload


def _canonical(value: Any) -> str:
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(",",":"))


def _fingerprint(value: Any) -> str:
    if value is None:return "missing"
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _envelope_fingerprint(envelope: dict[str,Any]) -> str:
    payload=copy.deepcopy(envelope); payload.pop("fingerprint",None)
    return _fingerprint(payload)


def _portable_tag_payload(value: dict[str,Any] | TagRecord) -> dict[str,Any]:
    payload=copy.deepcopy(value.to_dict() if isinstance(value,TagRecord) else value)
    payload["seen_count"]=0
    payload["first_seen"]=None
    payload["last_seen"]=None
    payload["provenance"]=[]
    return payload


def _category_payload(record: CategoryRecord) -> dict[str,Any]:
    return {"source":record.source,"record":record.to_dict()}


def _default_tag_records(app_root: Path) -> dict[str,dict[str,Any]]:
    effective=load_effective_tag_defaults(app_root/"data"/"defaults")
    records={
        str(item["id"]):TagRecord.from_dict(item).to_dict()
        for item in effective.tags
        if isinstance(item,dict) and str(item.get("id") or "")
    }
    retired_ids=set(effective.removed_tag_ids)-set(effective.merged_tag_ids)
    records.update({
        stable_id:record.to_dict()
        for stable_id,record in shipped_tag_records(
            app_root/"data"/"defaults",retired_ids,
        ).items()
    })
    return records


def _identity_names(record: dict[str,Any]) -> set[str]:
    return {
        normalized for value in (record.get("display"),*record.get("aliases",[]))
        if (normalized:=normalize_tag(str(value or "")))
    }


def _state(value: dict[str,Any],path: tuple[str,...]) -> tuple[bool,Any]:
    current: Any=value
    for key in path:
        if not isinstance(current,dict) or key not in current:return False,None
        current=current[key]
    return True,copy.deepcopy(current)


def _set_state(value: dict[str,Any],path: tuple[str,...],state: tuple[bool,Any]) -> None:
    current=value
    for key in path[:-1]:
        child=current.get(key)
        if not isinstance(child,dict):child={}; current[key]=child
        current=child
    exists,payload=state
    if exists:current[path[-1]]=copy.deepcopy(payload)
    else:current.pop(path[-1],None)


def _changed_paths(before: Any,after: Any,path: tuple[str,...]=()) -> tuple[tuple[str,...],...]:
    if isinstance(before,dict) and isinstance(after,dict):
        result=[]
        for key in sorted(set(before)|set(after)):
            if key not in before or key not in after:
                result.append((*path,str(key)))
            else:
                result.extend(_changed_paths(before[key],after[key],(*path,str(key))))
        return tuple(result)
    return (path,) if before!=after else ()


def _validate_tag_transition(
    stable_id: str,before: dict[str,Any],after: dict[str,Any],removed_ids: set[str],
) -> None:
    if str(before.get("id") or "")!=stable_id or str(after.get("id") or "")!=stable_id:
        raise ValueError(f'Removed-vocabulary Tag transition "{stable_id}" has mismatched IDs.')
    paths=set(_changed_paths(before,after))
    if not paths.issubset(_TAG_TRANSITION_PATHS):
        raise ValueError(
            f'Removed-vocabulary Tag transition "{stable_id}" changes fields outside '
            "parent, modifier, implication, or constraint compatibility authority."
        )
    before_record=TagRecord.from_dict(before); after_record=TagRecord.from_dict(after)
    if before_record.parent_id in removed_ids:
        if after_record.parent_id is not None:
            raise ValueError(f'Removed-vocabulary Tag transition "{stable_id}" retains a removed parent.')
    elif before_record.parent_id!=after_record.parent_id:
        raise ValueError(f'Removed-vocabulary Tag transition "{stable_id}" changes an unrelated parent.')
    for field in ("attributes","implies","conflicts"):
        prior=list(getattr(before_record,field)); expected=[item for item in prior if item not in removed_ids]
        if list(getattr(after_record,field))!=expected:
            raise ValueError(
                f'Removed-vocabulary Tag transition "{stable_id}" changes unrelated {field} values.'
            )


def _validate_category_transition(
    stable_id: str,before: dict[str,Any],after: dict[str,Any],removed_ids: set[str],
) -> None:
    before_record=before.get("record"); after_record=after.get("record")
    if not isinstance(before_record,dict) or not isinstance(after_record,dict):
        raise ValueError(f'Removed-vocabulary Category transition "{stable_id}" is incomplete.')
    if str(before_record.get("id") or "")!=stable_id or str(after_record.get("id") or "")!=stable_id:
        raise ValueError(f'Removed-vocabulary Category transition "{stable_id}" has mismatched IDs.')
    paths=set(_changed_paths(before_record,after_record))
    if not paths.issubset(_CATEGORY_TRANSITION_PATHS):
        raise ValueError(
            f'Removed-vocabulary Category transition "{stable_id}" changes fields outside Base Tag authority.'
        )
    if str(before_record.get("base_tag_id") or "") not in removed_ids:
        raise ValueError(f'Removed-vocabulary Category transition "{stable_id}" has no removed Base Tag.')
    if str(after_record.get("base_tag_id") or ""):
        raise ValueError(f'Removed-vocabulary Category transition "{stable_id}" retains a removed Base Tag.')


def _validate_relationship_transition(
    stable_id: str,before: dict[str,Any],after: dict[str,Any],
    removed_ids: set[str],
) -> None:
    before_record=KnowledgeRelationship.from_dict(before)
    after_record=KnowledgeRelationship.from_dict(after)
    if before_record.id!=stable_id:
        raise ValueError(f'Removed-vocabulary relationship "{stable_id}" has a mismatched prior ID.')
    if after_record.id!=stable_id:
        raise ValueError(f'Removed-vocabulary relationship "{stable_id}" has a mismatched resulting ID.')
    paths=set(_changed_paths(before,after))
    if not all(
        any(path[:len(root)]==root for root in _RELATIONSHIP_TRANSITION_PATHS)
        for path in paths
    ):
        raise ValueError(
            f'Removed-vocabulary relationship "{stable_id}" changes fields outside '
            "reviewed Tag-removal lifecycle authority."
        )
    audit=after_record.extras.get("tag_removal")
    if not isinstance(audit,dict):
        raise ValueError(f'Removed-vocabulary relationship "{stable_id}" has no removal audit.')
    audit_removed=audit.get("removed_tag_ids")
    if not isinstance(audit_removed,list) or (
        len(audit_removed)!=len(set(map(str,audit_removed)))
        or {str(value) for value in audit_removed}!=removed_ids
    ):
        raise ValueError(f'Removed-vocabulary relationship "{stable_id}" has the wrong removal batch.')
    if audit.get("previous_endpoints")!=[
        item.to_dict() for item in before_record.endpoints
    ]:
        raise ValueError(f'Removed-vocabulary relationship "{stable_id}" has an invalid prior-endpoint audit.')

    selected=tuple(
        index for index,item in enumerate(before_record.endpoints)
        if item.kind=="tag" and item.id in removed_ids
    )
    action=str(audit.get("action") or "")
    if selected:
        remaining=tuple(
            item for index,item in enumerate(before_record.endpoints)
            if index not in selected
        )
        source_removed=before_record.directed and 0 in selected
        retired=before_record.disabled or source_removed or len(remaining)<2
        expected_endpoints=(
            tuple(
                replace(item,kind="removed_tag") if index in selected else item
                for index,item in enumerate(before_record.endpoints)
            )
            if retired else remaining
        )
        expected_action=(
            "retired_removed_source" if source_removed
            else "recorded_removed_endpoint" if before_record.disabled
            else "retired_collapsed_relationship" if len(remaining)<2
            else "removed_tag_endpoints"
        )
        if action=="retired_duplicate_relationship":
            expected_disabled=True
        elif action==expected_action:
            expected_disabled=before_record.disabled or retired
        else:
            raise ValueError(f'Removed-vocabulary relationship "{stable_id}" has an invalid removal action.')
    else:
        if action!="retired_duplicate_relationship":
            raise ValueError(f'Removed-vocabulary relationship "{stable_id}" does not reference the removed batch.')
        expected_endpoints=before_record.endpoints
        expected_disabled=True
    if after_record.endpoints!=expected_endpoints:
        raise ValueError(f'Removed-vocabulary relationship "{stable_id}" changes unrelated endpoints.')
    if (
        after_record.version!=before_record.version+1
        or after_record.layer!="user"
        or after_record.source!="tag_removal"
        or after_record.disabled!=expected_disabled
    ):
        raise ValueError(f'Removed-vocabulary relationship "{stable_id}" has an invalid lifecycle result.')
    superseded=str(audit.get("superseded_by_relationship_id") or "")
    if (action=="retired_duplicate_relationship")!=(bool(superseded)) or superseded==stable_id:
        raise ValueError(f'Removed-vocabulary relationship "{stable_id}" has invalid duplicate-retirement authority.')


def _archive_envelope(
    archive: RemovedTagArchive,tags: TagDatabase,categories: CategoryRegistry,
    relationships: RelationshipStore,
) -> dict[str,Any]:
    removed_ids=set(archive.tag_ids)
    if not removed_ids or not removed_ids.issubset(tags.removed_tag_ids):
        raise ValueError(f'Removal archive "{archive.id}" is not an active complete batch.')
    removed_records=[]
    for stable_id in archive.tag_ids:
        record=archive.tag_record(stable_id)
        if record is None:raise ValueError(f'Removal archive "{archive.id}" is missing Tag "{stable_id}".')
        removed_records.append(_portable_tag_payload(record))

    default_relationships=RelationshipStore(
        categories.default_path.with_name("relationships.json")
    )
    effective_relationships=dict(default_relationships.relationships)
    effective_relationships.update(relationships.relationships)
    semantic_index=tag_semantic_relationship_index(
        effective_relationships.values(),
    )
    alias_index=tag_alias_compatibility_index(effective_relationships.values())
    foundation=FoundationKnowledge(
        categories.default_path.with_name("modifiers.json"),
        categories.default_path.with_name("concepts.json"),
    )
    modifier_index=reusable_modifier_compatibility_index(
        tags,effective_relationships.values(),
        categories=categories,foundation=foundation,
    )
    constraint_index=scoped_constraint_compatibility_index(
        effective_relationships.values(),
    )

    def compatibility_payload(record: TagRecord) -> dict[str,Any]:
        return tag_relationship_compatibility_payload(
            record,effective_relationships.values(),
            tags=tags,categories=categories,foundation=foundation,
            alias_index=alias_index,
            semantic_index=semantic_index,modifier_index=modifier_index,
            constraint_index=constraint_index,
        )

    related_before={}; related_after={}
    for stable_id,expected in archive.related_tags_after.items():
        current=tags.tags.get(stable_id)
        current_payload=(
            compatibility_payload(current)
            if current is not None else None
        )
        if _fingerprint(current_payload)!=expected:
            raise ValueError(
                f'Removal archive "{archive.id}" is stale because related Tag "{stable_id}" changed.'
            )
        before=archive.related_tags_before.get(stable_id)
        if not isinstance(before,dict) or current_payload is None:
            raise ValueError(f'Removal archive "{archive.id}" has an incomplete related Tag transition.')
        portable_before=_portable_tag_payload(before); portable_after=_portable_tag_payload(current_payload)
        _validate_tag_transition(stable_id,portable_before,portable_after,removed_ids)
        related_before[stable_id]=portable_before; related_after[stable_id]=portable_after

    categories_before={}; categories_after={}
    for stable_id,expected in archive.categories_after.items():
        current=categories.get(stable_id)
        current_payload=_category_payload(current) if current is not None else None
        if _fingerprint(current_payload)!=expected:
            raise ValueError(
                f'Removal archive "{archive.id}" is stale because Category "{stable_id}" changed.'
            )
        before=archive.categories_before.get(stable_id)
        if not isinstance(before,dict) or current_payload is None:
            raise ValueError(f'Removal archive "{archive.id}" has an incomplete Category transition.')
        _validate_category_transition(stable_id,before,current_payload,removed_ids)
        categories_before[stable_id]=copy.deepcopy(before)
        categories_after[stable_id]=copy.deepcopy(current_payload)

    relationships_before={}; relationships_after={}; relationship_recovery_kinds={}
    for stable_id,expected in archive.relationships_after.items():
        current=relationships.relationships.get(stable_id)
        current_payload=current.to_dict() if current is not None else None
        if _fingerprint(current_payload)!=expected:
            raise ValueError(
                f'Removal archive "{archive.id}" is stale because relationship "{stable_id}" changed.'
            )
        local_before=archive.relationships_before.get(stable_id)
        if local_before is None:
            default_before=default_relationships.relationships.get(stable_id)
            before=default_before.to_dict() if default_before is not None else None
            recovery="inherit_default"
        else:
            before=local_before
            recovery="archived_user"
        if not isinstance(before,dict) or not isinstance(current_payload,dict):
            raise ValueError(f'Removal archive "{archive.id}" has an incomplete relationship transition.')
        _validate_relationship_transition(stable_id,before,current_payload,removed_ids)
        relationships_before[stable_id]=copy.deepcopy(before)
        relationships_after[stable_id]=copy.deepcopy(current_payload)
        relationship_recovery_kinds[stable_id]=recovery

    extras={
        key:copy.deepcopy(value) for key,value in archive.extras.items()
        if key not in {_IMPORT_FINGERPRINT_KEY,_IMPORT_ENVELOPE_KEY}
    }
    envelope={
        "schema_version":PORTABLE_REMOVAL_SCHEMA_VERSION,
        "id":archive.id,"removed_at":archive.removed_at,
        "source":archive.source,"reason":archive.reason,
        "removed_tags":removed_records,
        "recovery_kinds":dict(archive.recovery_kinds),
        "related_tags_before":related_before,
        "related_tags_after":related_after,
        "categories_before":categories_before,
        "categories_after":categories_after,
        "relationships_before":relationships_before,
        "relationships_after":relationships_after,
        "relationship_recovery_kinds":relationship_recovery_kinds,
        "relationship_file_existed":archive.relationship_file_existed,
        "extras":extras,
    }
    envelope["fingerprint"]=_envelope_fingerprint(envelope)
    source_envelope=archive.extras.get(_IMPORT_ENVELOPE_KEY)
    if isinstance(source_envelope,dict) and (
        str(source_envelope.get("id") or "")==archive.id
        and str(source_envelope.get("fingerprint") or "")==_envelope_fingerprint(source_envelope)
        and {
            str(item.get("id") or "")
            for item in source_envelope.get("removed_tags",[])
            if isinstance(item,dict)
        }==removed_ids
    ):
        try:
            return copy.deepcopy(
                _validate_envelopes([source_envelope],removed_ids,set())[0]
            )
        except ValueError:
            pass
    return envelope


def portable_removed_archive_payload(
    app_root: Path,
) -> tuple[tuple[dict[str,Any],...],tuple[str,...]]:
    app_root=Path(app_root); user=app_root/"data"/"user"; defaults=app_root/"data"/"defaults"
    tags=TagDatabase(user/"tags.json")
    categories=CategoryRegistry(defaults/"categories.json",user/"categories.json")
    relationships=RelationshipStore(user/"relationships.json")
    envelopes=[
        _archive_envelope(archive,tags,categories,relationships)
        for archive in tags.active_removed_archives()
    ]
    archived_ids={stable_id for archive in tags.active_removed_archives() for stable_id in archive.tag_ids}
    legacy=sorted(tags.removed_tag_ids-set(tags.merged_tag_ids)-archived_ids)
    shipped_records=shipped_tag_records(defaults,legacy)
    legacy_only=[]
    for stable_id in legacy:
        shipped=shipped_records.get(stable_id)
        if shipped is None:
            legacy_only.append(stable_id); continue
        envelope={
            "schema_version":PORTABLE_REMOVAL_SCHEMA_VERSION,
            "id":f"legacy_removal.{sha256(stable_id.encode('utf-8')).hexdigest()[:20]}",
            "removed_at":"","source":"legacy_tombstone",
            "reason":"Legacy removed-default decision",
            "removed_tags":[_portable_tag_payload(shipped)],
            "recovery_kinds":{stable_id:"current_default"},
            "related_tags_before":{},"related_tags_after":{},
            "categories_before":{},"categories_after":{},
            "relationships_before":{},"relationships_after":{},
            "relationship_recovery_kinds":{},
            "relationship_file_existed":relationships.path.exists(),"extras":{},
        }
        envelope["fingerprint"]=_envelope_fingerprint(envelope); envelopes.append(envelope)
    validated=_validate_envelopes(
        envelopes,set(tags.removed_tag_ids),set(tags.merged_tag_ids),set(legacy_only)
    )
    return (
        tuple(sorted(validated,key=lambda item:(str(item.get("removed_at") or ""),str(item["id"])))),
        tuple(legacy_only),
    )


def portable_removed_archive_envelopes(app_root: Path) -> tuple[dict[str,Any],...]:
    return portable_removed_archive_payload(app_root)[0]


def _validate_envelopes(
    raw: Any,removed_tag_ids: set[str],merged_ids: set[str],
    legacy_removed_ids: set[str] | None=None,
) -> tuple[dict[str,Any],...]:
    if not isinstance(raw,list):
        raise ValueError("The removed-vocabulary archive list is invalid.")
    if len(raw)>MAX_PORTABLE_REMOVAL_ARCHIVES:
        raise ValueError("The Knowledge Pack contains too many removed-vocabulary archives.")
    envelopes=[]; archive_ids=set(); tag_ids=set(); transition_count=0
    for item in raw:
        if not isinstance(item,dict):raise ValueError("A removed-vocabulary archive is not a JSON object.")
        envelope=copy.deepcopy(item); archive_id=str(envelope.get("id") or "")
        if not archive_id or archive_id in archive_ids:
            raise ValueError("Removed-vocabulary archive IDs must be present and unique.")
        if int(envelope.get("schema_version",0) or 0)!=PORTABLE_REMOVAL_SCHEMA_VERSION:
            raise ValueError(f'Removed-vocabulary archive "{archive_id}" has an unsupported schema.')
        fingerprint=str(envelope.get("fingerprint") or "")
        if not fingerprint or fingerprint!=_envelope_fingerprint(envelope):
            raise ValueError(f'Removed-vocabulary archive "{archive_id}" failed fingerprint validation.')
        records=envelope.get("removed_tags")
        if not isinstance(records,list) or not records or any(not isinstance(value,dict) for value in records):
            raise ValueError(f'Removed-vocabulary archive "{archive_id}" has no complete Tag records.')
        ids=tuple(str(value.get("id") or "") for value in records)
        if any(not value for value in ids) or len(set(ids))!=len(ids):
            raise ValueError(f'Removed-vocabulary archive "{archive_id}" has invalid Tag IDs.')
        if set(ids).intersection(tag_ids):
            raise ValueError("A removed stable identity belongs to more than one imported archive.")
        if not set(ids).issubset(removed_tag_ids) or set(ids).intersection(merged_ids):
            raise ValueError(f'Removed-vocabulary archive "{archive_id}" does not match the pack tombstones.')
        kinds=envelope.get("recovery_kinds")
        if not isinstance(kinds,dict) or set(map(str,kinds))!=set(ids) or any(
            str(value) not in _RECOVERY_KINDS for value in kinds.values()
        ):
            raise ValueError(f'Removed-vocabulary archive "{archive_id}" has invalid recovery sources.')
        removed_ids=set(ids)
        maps={}
        for key in (
            "related_tags_before","related_tags_after","categories_before",
            "categories_after","relationships_before","relationships_after",
        ):
            value=envelope.get(key)
            if not isinstance(value,dict):
                raise ValueError(f'Removed-vocabulary archive "{archive_id}" has an invalid {key} map.')
            maps[key]={str(k):copy.deepcopy(v) for k,v in value.items()}
        if set(maps["related_tags_before"])!=set(maps["related_tags_after"]):
            raise ValueError(f'Removed-vocabulary archive "{archive_id}" has incomplete related Tag states.')
        if set(maps["categories_before"])!=set(maps["categories_after"]):
            raise ValueError(f'Removed-vocabulary archive "{archive_id}" has incomplete Category states.')
        if set(maps["relationships_before"])!=set(maps["relationships_after"]):
            raise ValueError(f'Removed-vocabulary archive "{archive_id}" has incomplete relationship states.')
        relationship_kinds=envelope.get("relationship_recovery_kinds")
        if not isinstance(relationship_kinds,dict) or set(map(str,relationship_kinds))!=set(maps["relationships_before"]) or any(
            str(value) not in _RELATIONSHIP_RECOVERY_KINDS
            for value in relationship_kinds.values()
        ):
            raise ValueError(f'Removed-vocabulary archive "{archive_id}" has invalid relationship recovery sources.')
        if not isinstance(envelope.get("extras"),dict):
            raise ValueError(f'Removed-vocabulary archive "{archive_id}" has invalid extension data.')
        for stable_id in maps["related_tags_before"]:
            before=maps["related_tags_before"][stable_id]; after=maps["related_tags_after"][stable_id]
            if not isinstance(before,dict) or not isinstance(after,dict):
                raise ValueError(f'Removed-vocabulary archive "{archive_id}" has an invalid Tag transition.')
            _validate_tag_transition(stable_id,before,after,removed_ids)
        for stable_id in maps["categories_before"]:
            before=maps["categories_before"][stable_id]; after=maps["categories_after"][stable_id]
            if not isinstance(before,dict) or not isinstance(after,dict):
                raise ValueError(f'Removed-vocabulary archive "{archive_id}" has an invalid Category transition.')
            _validate_category_transition(stable_id,before,after,removed_ids)
        for stable_id in maps["relationships_before"]:
            before=maps["relationships_before"][stable_id]; after=maps["relationships_after"][stable_id]
            if not isinstance(before,dict) or not isinstance(after,dict):
                raise ValueError(f'Removed-vocabulary archive "{archive_id}" has an invalid relationship transition.')
            _validate_relationship_transition(stable_id,before,after,removed_ids)
        transition_count+=sum(len(maps[key]) for key in (
            "related_tags_before","categories_before","relationships_before",
        ))
        envelope.update(maps)
        envelope["relationship_recovery_kinds"]={
            str(key):str(value) for key,value in relationship_kinds.items()
        }
        envelope["removed_tags"]=[_portable_tag_payload(value) for value in records]
        archive_ids.add(archive_id); tag_ids.update(ids); envelopes.append(envelope)
    if len(tag_ids)>MAX_PORTABLE_REMOVAL_TAGS or transition_count>MAX_PORTABLE_TRANSITION_RECORDS:
        raise ValueError("The Knowledge Pack removed-vocabulary lifecycle is too large to review safely.")
    expected=removed_tag_ids-merged_ids
    legacy_removed_ids=set(legacy_removed_ids or ())
    if (
        not legacy_removed_ids.issubset(expected)
        or legacy_removed_ids.intersection(tag_ids)
        or tag_ids|legacy_removed_ids!=expected
    ):
        missing=sorted(expected-tag_ids-legacy_removed_ids)
        raise ValueError(
            "Knowledge Pack schema 6 requires a complete recovery archive or an explicit legacy marker for every removed identity"
            +(f": {', '.join(missing[:4])}" if missing else ".")
        )
    return tuple(envelopes)


def parse_portable_removed_archives(tags_payload: dict[str,Any]) -> tuple[dict[str,Any],...]:
    removed={str(value) for value in tags_payload.get("removed_tag_ids",[]) if str(value)}
    merged={str(value) for value in dict(tags_payload.get("merged_tag_ids") or {}) if str(value)}
    raw_legacy=tags_payload.get("legacy_removed_tag_ids",[])
    if not isinstance(raw_legacy,list):
        raise ValueError("The legacy removed-identity marker list is invalid.")
    legacy={str(value) for value in raw_legacy if str(value)}
    if len(legacy)!=len(raw_legacy):
        raise ValueError("Legacy removed-identity markers must be non-empty and unique.")
    return _validate_envelopes(
        tags_payload.get("removed_tag_archives",[]),removed,merged,legacy
    )


def _remap_tag_payload(
    payload: dict[str,Any],tag_ids: dict[str,str],category_ids: dict[str,str],
    *,record_id: bool,
) -> dict[str,Any]:
    result=copy.deepcopy(payload)
    if record_id:
        stable_id=str(result.get("id") or "")
        result["id"]=tag_ids.get(stable_id,stable_id)
    category=str(result.get("category") or "")
    if category:result["category"]=category_ids.get(category,category)
    relations=result.get("relations")
    if isinstance(relations,dict):
        parent=str(relations.get("parent") or "")
        if parent:relations["parent"]=tag_ids.get(parent,parent)
        for key in ("attributes","implies","conflicts"):
            if isinstance(relations.get(key),list):
                relations[key]=[tag_ids.get(str(value),str(value)) for value in relations[key]]
    return result


def _remap_category_payload(
    payload: dict[str,Any],tag_ids: dict[str,str],category_ids: dict[str,str],
) -> dict[str,Any]:
    result=copy.deepcopy(payload); record=result.get("record")
    if not isinstance(record,dict):return result
    stable_id=str(record.get("id") or ""); record["id"]=category_ids.get(stable_id,stable_id)
    parent=str(record.get("parent_id") or "")
    if parent:record["parent_id"]=category_ids.get(parent,parent)
    base=str(record.get("base_tag_id") or "")
    if base:record["base_tag_id"]=tag_ids.get(base,base)
    return result


def _remap_relationship_payload(
    payload: dict[str,Any],tag_ids: dict[str,str],category_ids: dict[str,str],
) -> dict[str,Any]:
    result=copy.deepcopy(payload)
    def remap_endpoints(endpoints: Any) -> None:
        if not isinstance(endpoints,list):return
        for endpoint in endpoints:
            if not isinstance(endpoint,dict):continue
            stable_id=str(endpoint.get("id") or "")
            if endpoint.get("kind") in {"tag","removed_tag"}:
                endpoint["id"]=tag_ids.get(stable_id,stable_id)
            elif endpoint.get("kind") in {"category","modifier_dimension"}:
                endpoint["id"]=category_ids.get(stable_id,stable_id)
    remap_endpoints(result.get("endpoints"))
    audit=result.get("tag_removal")
    depth=0
    while isinstance(audit,dict):
        depth+=1
        if depth>64:raise ValueError("The relationship removal audit is nested too deeply.")
        values=audit.get("removed_tag_ids")
        if isinstance(values,list):
            audit["removed_tag_ids"]=[tag_ids.get(str(value),str(value)) for value in values]
        remap_endpoints(audit.get("previous_endpoints"))
        audit=audit.get("previous_tag_removal")
    details=result.get("details")
    if isinstance(details,dict):
        family=str(details.get("modifier_dimension_id") or "")
        if family:details["modifier_dimension_id"]=category_ids.get(family,family)
    return result


def _remap_map(
    values: dict[str,Any],id_map: dict[str,str],transform,
) -> dict[str,Any]:
    result={}
    for stable_id,payload in values.items():
        mapped=id_map.get(stable_id,stable_id)
        if mapped in result:raise ValueError(f'Multiple removal transitions map to stable ID "{mapped}".')
        result[mapped]=transform(payload)
    return result


def _remap_envelope(
    envelope: dict[str,Any],tag_ids: dict[str,str],category_ids: dict[str,str],
) -> dict[str,Any]:
    if not tag_ids and not category_ids:return envelope
    result=copy.deepcopy(envelope)
    result["removed_tags"]=[
        _remap_tag_payload(item,tag_ids,category_ids,record_id=False)
        for item in result["removed_tags"]
    ]
    result["related_tags_before"]=_remap_map(
        result["related_tags_before"],tag_ids,
        lambda value:_remap_tag_payload(value,tag_ids,category_ids,record_id=True),
    )
    result["related_tags_after"]=_remap_map(
        result["related_tags_after"],tag_ids,
        lambda value:_remap_tag_payload(value,tag_ids,category_ids,record_id=True),
    )
    result["categories_before"]=_remap_map(
        result["categories_before"],category_ids,
        lambda value:_remap_category_payload(value,tag_ids,category_ids),
    )
    result["categories_after"]=_remap_map(
        result["categories_after"],category_ids,
        lambda value:_remap_category_payload(value,tag_ids,category_ids),
    )
    result["relationships_before"]={
        stable_id:_remap_relationship_payload(value,tag_ids,category_ids)
        for stable_id,value in result["relationships_before"].items()
    }
    result["relationships_after"]={
        stable_id:_remap_relationship_payload(value,tag_ids,category_ids)
        for stable_id,value in result["relationships_after"].items()
    }
    return result


def _envelope_dependencies(envelope: dict[str,Any]) -> tuple[set[str],set[str]]:
    tag_ids=set(); category_ids=set()
    for item in envelope.get("removed_tags",[]):
        if not isinstance(item,dict):continue
        category=str(item.get("category") or "")
        if category:category_ids.add(category)
        relations=item.get("relations") if isinstance(item.get("relations"),dict) else {}
        parent=str(relations.get("parent") or "")
        if parent:tag_ids.add(parent)
        for key in ("attributes","implies","conflicts"):
            values=relations.get(key)
            if isinstance(values,list):tag_ids.update(str(value) for value in values if str(value))
    tag_ids.update(map(str,envelope.get("related_tags_before",{})))
    category_ids.update(map(str,envelope.get("categories_before",{})))
    for payload in envelope.get("categories_before",{}).values():
        record=payload.get("record") if isinstance(payload,dict) else None
        if not isinstance(record,dict):continue
        parent=str(record.get("parent_id") or ""); base=str(record.get("base_tag_id") or "")
        if parent:category_ids.add(parent)
        if base:tag_ids.add(base)
    for key in ("relationships_before","relationships_after"):
        for payload in envelope.get(key,{}).values():
            if not isinstance(payload,dict):continue
            for endpoint in payload.get("endpoints",[]):
                if not isinstance(endpoint,dict):continue
                stable_id=str(endpoint.get("id") or "")
                if endpoint.get("kind")=="tag" and stable_id:tag_ids.add(stable_id)
                elif endpoint.get("kind") in {"category","modifier_dimension"} and stable_id:
                    category_ids.add(stable_id)
    return tag_ids,category_ids


def _replace_records(original: list[Any],updates: dict[str,dict[str,Any] | None]) -> list[dict[str,Any]]:
    result=[]; seen=set()
    for item in original:
        if not isinstance(item,dict):continue
        stable_id=str(item.get("id") or "")
        if stable_id in updates:
            replacement=updates[stable_id]; seen.add(stable_id)
            if replacement is not None:result.append(copy.deepcopy(replacement))
        else:result.append(copy.deepcopy(item))
    for stable_id,replacement in updates.items():
        if stable_id not in seen and replacement is not None:result.append(copy.deepcopy(replacement))
    return result


def _apply_record_transition(
    current: dict[str,Any] | None,before: dict[str,Any],after: dict[str,Any],
    *,allowed_paths: frozenset[tuple[str,...]],label: str,
) -> tuple[dict[str,Any],dict[str,Any]]:
    paths=set(_changed_paths(before,after))
    if not all(any(path[:len(root)]==root for root in allowed_paths) for path in paths):
        raise ValueError(f"{label} changes unsupported lifecycle fields.")
    if current is None:return copy.deepcopy(before),copy.deepcopy(after)
    restore=copy.deepcopy(current); result=copy.deepcopy(current)
    for path in paths:
        current_state=_state(current,path); before_state=_state(before,path); after_state=_state(after,path)
        if current_state!=before_state and current_state!=after_state:
            raise ValueError(f"{label} changed locally after this removal decision.")
        _set_state(restore,path,before_state); _set_state(result,path,after_state)
    return restore,result

def _prepare_one(
    app_root: Path,envelope: dict[str,Any],tag_payload: dict[str,Any],
    category_payload: dict[str,Any],relationship_payload: dict[str,Any],
    *,relationship_file_existed: bool,incoming_tags: tuple[dict[str,Any],...]=(),
    incoming_categories: tuple[dict[str,Any],...]=(),
    default_tags: dict[str,dict[str,Any]] | None=None,
    default_categories: dict[str,dict[str,Any]] | None=None,
    default_relationships: dict[str,KnowledgeRelationship] | None=None,
    known_removed_ids: set[str] | None=None,
    tag_id_remap: dict[str,str] | None=None,
    category_id_remap: dict[str,str] | None=None,
) -> tuple[RemovedTagArchive,dict[str,Any],dict[str,Any],dict[str,Any]]:
    source_envelope=envelope
    tag_id_remap=tag_id_remap or {}; category_id_remap=category_id_remap or {}
    envelope=_remap_envelope(envelope,tag_id_remap,category_id_remap)
    defaults=app_root/"data"/"defaults"
    default_tags=default_tags if default_tags is not None else _default_tag_records(app_root)
    user_tags={
        str(item.get("id") or ""):TagRecord.from_dict(item).to_dict()
        for item in tag_payload.get("tags",[])
        if isinstance(item,dict) and str(item.get("id") or "")
    }
    live_tags=dict(default_tags); live_tags.update(user_tags)
    for item in incoming_tags:
        if isinstance(item,dict) and str(item.get("id") or ""):
            item=_remap_tag_payload(item,tag_id_remap,category_id_remap,record_id=True)
            item=TagRecord.from_dict(item).to_dict()
            live_tags.setdefault(str(item["id"]),item)
    known_removed_ids=known_removed_ids or set()
    live_tags={
        stable_id:value for stable_id,value in live_tags.items()
        if stable_id not in known_removed_ids
    }
    if default_categories is None:
        default_category_payload=_read_payload(defaults/"categories.json",schema_version=5,key="categories")
        default_categories={
            str(item.get("id") or ""):CategoryRecord.from_dict(item,source="default").to_dict()
            for item in default_category_payload.get("categories",[])
            if isinstance(item,dict) and str(item.get("id") or "")
        }
    user_categories={
        str(item.get("id") or ""):CategoryRecord.from_dict(item,source="user").to_dict()
        for item in category_payload.get("categories",[])
        if isinstance(item,dict) and str(item.get("id") or "")
    }
    live_categories=dict(default_categories); live_categories.update(user_categories)
    for item in incoming_categories:
        if isinstance(item,dict) and str(item.get("id") or ""):
            wrapped={"record":item}
            item=_remap_category_payload(wrapped,tag_id_remap,category_id_remap)["record"]
            item=CategoryRecord.from_dict(item,source="user").to_dict()
            live_categories.setdefault(str(item["id"]),item)
    user_relationships={
        str(item.get("id") or ""):KnowledgeRelationship.from_dict(item).to_dict()
        for item in relationship_payload.get("relationships",[])
        if isinstance(item,dict) and str(item.get("id") or "")
    }
    defaults_relationships=dict(
        default_relationships
        if default_relationships is not None
        else RelationshipStore(defaults/"relationships.json").relationships
    )
    effective_before=dict(defaults_relationships)
    effective_before.update({
        stable_id:KnowledgeRelationship.from_dict(value)
        for stable_id,value in user_relationships.items()
    })
    needs_tag_compatibility = bool(envelope["related_tags_before"]) or any(
        str(value) == "current_default"
        for value in envelope["recovery_kinds"].values()
    )
    category_view = None
    foundation = None
    if needs_tag_compatibility:
        category_view=CategoryRegistry(
            defaults/"categories.json",app_root/"data"/"user"/"categories.json",
        )
        category_view.categories={
            stable_id:CategoryRecord.from_dict(
                payload,
                source="user" if stable_id in user_categories else "default",
            )
            for stable_id,payload in live_categories.items()
        }
        foundation=FoundationKnowledge(
            defaults/"modifiers.json",defaults/"concepts.json",
        )

    def tag_view_for(
        tag_payloads: dict[str,dict[str,Any]],
    ) -> TagDatabase:
        tag_view=TagDatabase(app_root/"data"/"user"/"tags.json")
        tag_view.tags={
            stable_id:TagRecord.from_dict(value)
            for stable_id,value in tag_payloads.items()
        }
        tag_view._rebuild_lookup()
        return tag_view

    def compatibility_payload(
        payload: dict[str,Any],
        relationships: dict[str,KnowledgeRelationship],
        *,
        tag_view: TagDatabase,
        alias_index: TagAliasCompatibilityIndex,
        semantic_index: dict[tuple[str,str],tuple[KnowledgeRelationship,...]],
        modifier_index: ReusableModifierCompatibilityIndex,
        constraint_index: ScopedConstraintCompatibilityIndex,
    ) -> dict[str,Any]:
        if category_view is None or foundation is None:
            raise RuntimeError("Tag compatibility resources were not initialized.")
        return tag_relationship_compatibility_payload(
            TagRecord.from_dict(payload),relationships.values(),
            tags=tag_view,categories=category_view,foundation=foundation,
            alias_index=alias_index,
            semantic_index=semantic_index,modifier_index=modifier_index,
            constraint_index=constraint_index,
        )

    before_tag_view=tag_view_for(live_tags) if needs_tag_compatibility else None
    aliases_before=tag_alias_compatibility_index(effective_before.values())
    semantic_before=tag_semantic_relationship_index(effective_before.values())
    constraints_before=scoped_constraint_compatibility_index(
        effective_before.values(),
    )
    modifiers_before=(
        reusable_modifier_compatibility_index(
            before_tag_view,effective_before.values(),
            categories=category_view,foundation=foundation,
        )
        if before_tag_view is not None
        and category_view is not None
        and foundation is not None
        else None
    )
    category_base_before=category_base_relationship_index(
        effective_before.values(),
    )
    for stable_id,payload in tuple(live_categories.items()):
        source="user" if stable_id in user_categories else "default"
        live_categories[stable_id]=category_base_compatibility_payload(
            CategoryRecord.from_dict(payload,source=source),
            category_base_before,
        )

    removed_records={str(item.get("id") or ""):copy.deepcopy(item) for item in envelope["removed_tags"]}
    removed_ids=set(removed_records)
    identity_owners={}
    for stable_id,record in live_tags.items():
        for name in _identity_names(record):identity_owners.setdefault(name,set()).add(stable_id)
    removed_archive_records=[]; tag_updates={}
    for stable_id,portable in removed_records.items():
        recovery=str(envelope["recovery_kinds"][stable_id])
        current=user_tags.get(stable_id)
        if (
            current is not None
            and before_tag_view is not None
            and modifiers_before is not None
        ):
            current=compatibility_payload(
                current,effective_before,
                tag_view=before_tag_view,alias_index=aliases_before,
                semantic_index=semantic_before,
                modifier_index=modifiers_before,constraint_index=constraints_before,
            )
        shipped_payload=default_tags.get(stable_id)
        shipped=(
            compatibility_payload(
                shipped_payload,effective_before,
                tag_view=before_tag_view,alias_index=aliases_before,
                semantic_index=semantic_before,
                modifier_index=modifiers_before,constraint_index=constraints_before,
            )
            if shipped_payload is not None
            and before_tag_view is not None
            and modifiers_before is not None
            else None
        )
        if recovery=="current_default":
            if shipped is None:raise ValueError(f'Current shipped recovery for removed identity "{stable_id}" is unavailable.')
            if current is not None and _portable_tag_payload(current)!=_portable_tag_payload(shipped):
                raise ValueError(f'Live local Tag "{stable_id}" differs from the shipped record this pack removes.')
            removed_archive_records.append(_portable_tag_payload(shipped))
            if current is not None:tag_updates[stable_id]=None
        else:
            if stable_id in default_tags or current is not None:
                raise ValueError(f'Live local Tag "{stable_id}" blocks an imported archived-user removal.')
            removed_archive_records.append(copy.deepcopy(portable))
        for name in _identity_names(removed_archive_records[-1]):
            owners=identity_owners.get(name,set())-({stable_id} if recovery=="current_default" else set())
            if owners:
                raise ValueError(
                    f'Removed identity "{stable_id}" collides with live local identity '
                    f'"{sorted(owners)[0]}" through name or alias "{name}".'
                )

    related_before={}; related_after_fingerprints={}
    for stable_id,before in envelope["related_tags_before"].items():
        after=envelope["related_tags_after"][stable_id]
        current=live_tags.get(stable_id)
        if current is not None:
            assert before_tag_view is not None and modifiers_before is not None
            current=compatibility_payload(
                current,effective_before,
                tag_view=before_tag_view,alias_index=aliases_before,
                semantic_index=semantic_before,
                modifier_index=modifiers_before,constraint_index=constraints_before,
            )
        if current is None:
            for name in _identity_names(before):
                owners=identity_owners.get(name,set())-{stable_id}
                if owners:
                    raise ValueError(
                        f'Related Tag "{stable_id}" collides with live local identity '
                        f'"{sorted(owners)[0]}" through name or alias "{name}".'
                    )
        restore,result=_apply_record_transition(
            current,before,after,allowed_paths=_TAG_TRANSITION_PATHS,
            label=f'Related Tag "{stable_id}"',
        )
        related_before[stable_id]=restore
        related_after_fingerprints[stable_id]=_fingerprint(result)
        tag_updates[stable_id]=result; live_tags[stable_id]=result
        for name in _identity_names(result):identity_owners.setdefault(name,set()).add(stable_id)

    category_updates={}; categories_before={}; categories_after_fingerprints={}
    for stable_id,before_payload in envelope["categories_before"].items():
        after_payload=envelope["categories_after"][stable_id]
        current_record=live_categories.get(stable_id)
        if current_record is None:
            identity=(
                normalize_tag(str(before_payload["record"].get("name") or "")),
                str(before_payload["record"].get("parent_id") or ""),
            )
            owner=next((
                record_id for record_id,record in live_categories.items()
                if record_id!=stable_id and (
                    normalize_tag(str(record.get("name") or "")),
                    str(record.get("parent_id") or ""),
                )==identity
            ),None)
            if owner is not None:
                raise ValueError(
                    f'Category "{stable_id}" collides with live local Category "{owner}".'
                )
        restore,result=_apply_record_transition(
            current_record,before_payload["record"],after_payload["record"],
            allowed_paths=_CATEGORY_TRANSITION_PATHS,label=f'Category "{stable_id}"',
        )
        before_source="user" if stable_id in user_categories else "default"
        categories_before[stable_id]={"source":before_source,"record":restore}
        result["id"]=stable_id; category_updates[stable_id]=result; live_categories[stable_id]=result
        categories_after_fingerprints[stable_id]=_fingerprint({"source":"user","record":result})

    relationship_updates={}; relationships_before={}; relationships_after_fingerprints={}
    for stable_id,before in envelope["relationships_before"].items():
        after=envelope["relationships_after"][stable_id]
        recovery=envelope["relationship_recovery_kinds"][stable_id]
        current_user=user_relationships.get(stable_id)
        current_default=defaults_relationships.get(stable_id)
        if recovery=="inherit_default" and current_user is not None:
            raise ValueError(
                f'Local user relationship "{stable_id}" blocks an imported removal decision.'
            )
        current=(
            current_user
            if current_user is not None
            else current_default.to_dict() if current_default is not None else None
        )
        restore,result=_apply_record_transition(
            current,before,after,allowed_paths=_RELATIONSHIP_TRANSITION_PATHS,
            label=f'Canonical relationship "{stable_id}"',
        )
        relationships_before[stable_id]=(
            None if recovery=="inherit_default" else restore
        )
        relationships_after_fingerprints[stable_id]=_fingerprint(result)
        relationship_updates[stable_id]=result
        if result is None:user_relationships.pop(stable_id,None)
        else:user_relationships[stable_id]=result

    restored_user_relationships=dict(user_relationships)
    for stable_id,before in relationships_before.items():
        if before is None:
            restored_user_relationships.pop(stable_id,None)
        else:
            restored_user_relationships[stable_id]=before
    effective_restore=dict(defaults_relationships)
    effective_restore.update({
        stable_id:KnowledgeRelationship.from_dict(value)
        for stable_id,value in restored_user_relationships.items()
    })
    effective_result=dict(defaults_relationships)
    effective_result.update({
        stable_id:KnowledgeRelationship.from_dict(value)
        for stable_id,value in user_relationships.items()
    })
    semantic_restore=tag_semantic_relationship_index(
        effective_restore.values(),
    )
    semantic_result=tag_semantic_relationship_index(
        effective_result.values(),
    )
    aliases_restore=tag_alias_compatibility_index(effective_restore.values())
    aliases_result=tag_alias_compatibility_index(effective_result.values())
    constraints_restore=scoped_constraint_compatibility_index(
        effective_restore.values(),
    )
    constraints_result=scoped_constraint_compatibility_index(
        effective_result.values(),
    )
    restore_tag_payloads=dict(live_tags)
    restore_tag_payloads.update(related_before)
    result_tag_payloads=dict(live_tags)
    result_tag_payloads.update({
        stable_id:payload for stable_id,payload in tag_updates.items()
        if isinstance(payload,dict)
    })
    restore_tag_view=tag_view_for(restore_tag_payloads) if related_before else None
    result_tag_view=tag_view_for(result_tag_payloads) if related_before else None
    modifiers_restore=(
        reusable_modifier_compatibility_index(
            restore_tag_view,effective_restore.values(),
            categories=category_view,foundation=foundation,
        )
        if restore_tag_view is not None
        and category_view is not None
        and foundation is not None
        else None
    )
    modifiers_result=(
        reusable_modifier_compatibility_index(
            result_tag_view,effective_result.values(),
            categories=category_view,foundation=foundation,
        )
        if result_tag_view is not None
        and category_view is not None
        and foundation is not None
        else None
    )
    for stable_id in tuple(related_before):
        assert restore_tag_view is not None and modifiers_restore is not None
        assert result_tag_view is not None and modifiers_result is not None
        related_before[stable_id]=compatibility_payload(
            related_before[stable_id],effective_restore,
            tag_view=restore_tag_view,alias_index=aliases_restore,
            semantic_index=semantic_restore,
            modifier_index=modifiers_restore,constraint_index=constraints_restore,
        )
        result=compatibility_payload(
            tag_updates[stable_id],effective_result,
            tag_view=result_tag_view,alias_index=aliases_result,
            semantic_index=semantic_result,
            modifier_index=modifiers_result,constraint_index=constraints_result,
        )
        tag_updates[stable_id]=result
        related_after_fingerprints[stable_id]=_fingerprint(result)

    candidate_tag_payload=copy.deepcopy(tag_payload)
    candidate_tag_payload["schema_version"]=max(
        TagDatabase.SCHEMA_VERSION,
        int(candidate_tag_payload.get("schema_version",1) or 1),
    )
    candidate_tag_payload["tags"]=_replace_records(candidate_tag_payload.get("tags",[]),tag_updates)

    candidate_category_payload=copy.deepcopy(category_payload)
    candidate_category_payload["schema_version"]=max(
        CategoryRegistry.SCHEMA_VERSION,
        int(candidate_category_payload.get("schema_version",1) or 1),
    )
    candidate_category_payload["categories"]=_replace_records(candidate_category_payload.get("categories",[]),category_updates)
    effective_after=dict(defaults_relationships)
    effective_after.update({
        stable_id:KnowledgeRelationship.from_dict(value)
        for stable_id,value in user_relationships.items()
    })
    category_base_after=category_base_relationship_index(
        effective_after.values(),
    )
    if tag_updates:
        assert category_view is not None and foundation is not None
        candidate_live_tags=dict(default_tags)
        candidate_live_tags.update({
            str(item.get("id") or ""):item
            for item in candidate_tag_payload["tags"]
            if isinstance(item,dict) and str(item.get("id") or "")
        })
        candidate_tag_view=tag_view_for(candidate_live_tags)
        category_view.categories={
            stable_id:CategoryRecord.from_dict(
                payload,
                source="user" if stable_id in user_categories else "default",
            )
            for stable_id,payload in live_categories.items()
        }
        category_view.configure_category_base_authority(effective_after.values())
        publish_tag_alias_storage_authority(
            candidate_tag_view,effective_after.values(),
        )
        publish_tag_semantic_authority(
            candidate_tag_view,effective_after.values(),
        )
        publish_reusable_modifier_storage_authority(
            candidate_tag_view,effective_after.values(),
            categories=category_view,foundation=foundation,
        )
        publish_scoped_constraint_storage_authority(
            candidate_tag_view,effective_after.values(),
        )
        for item in candidate_tag_payload["tags"]:
            if not isinstance(item,dict):continue
            record=candidate_tag_view.tags.get(str(item.get("id") or ""))
            if record is not None:
                item["relations"]=candidate_tag_view._record_payload(record)["relations"]
    for item in candidate_category_payload["categories"]:
        if (
            isinstance(item,dict)
            and category_base_after.get(str(item.get("id") or ""))
        ):
            item.pop("base_tag_id",None)
            item.pop("base_tag_policy",None)
    candidate_relationship_payload=copy.deepcopy(relationship_payload)
    candidate_relationship_payload["schema_version"]=max(1,int(candidate_relationship_payload.get("schema_version",1) or 1))
    candidate_relationship_payload["relationships"]=_replace_records(candidate_relationship_payload.get("relationships",[]),relationship_updates)

    current_live=dict(default_tags)
    current_live.update({str(item.get("id") or ""):item for item in candidate_tag_payload["tags"] if str(item.get("id") or "")})
    current_live={
        stable_id:value for stable_id,value in current_live.items()
        if stable_id not in known_removed_ids and stable_id not in removed_ids
    }
    available=set(current_live)|removed_ids
    for record_payload in removed_archive_records:
        record=TagRecord.from_dict(record_payload)
        if record.category not in live_categories:
            raise ValueError(f'Removed Tag "{record.display}" depends on unavailable Category "{record.category}".')
        missing={value for value in (record.parent_id,*record.implies,*record.conflicts) if value and value not in available}
        if missing:raise ValueError(f'Removed Tag "{record.display}" has unavailable dependencies: {", ".join(sorted(missing)[:4])}.')

    effective=defaults_relationships
    effective.update({stable_id:KnowledgeRelationship.from_dict(value) for stable_id,value in user_relationships.items()})
    issues=validate_relationships(effective.values())
    if issues:raise ValueError("Imported removal state has an invalid relationship graph: "+" ".join(item.message for item in issues[:3]))
    for relationship in effective.values():
        missing={item.id for item in relationship.endpoints if item.kind=="tag" and item.id not in current_live and item.kind!="removed_tag"}
        if missing:raise ValueError(f'Relationship "{relationship.id}" depends on unavailable Tags: {", ".join(sorted(missing)[:4])}.')

    extras=copy.deepcopy(envelope.get("extras") or {})
    extras[_IMPORT_FINGERPRINT_KEY]=str(source_envelope["fingerprint"])
    extras[_IMPORT_ENVELOPE_KEY]=copy.deepcopy(source_envelope)
    archive=RemovedTagArchive(
        id=str(envelope["id"]),removed_at=str(envelope.get("removed_at") or ""),
        source=str(envelope.get("source") or "knowledge_pack"),reason=str(envelope.get("reason") or ""),
        removed_tags=removed_archive_records,
        recovery_kinds={str(k):str(v) for k,v in envelope["recovery_kinds"].items()},
        related_tags_before=related_before,related_tags_after=related_after_fingerprints,
        categories_before=categories_before,categories_after=categories_after_fingerprints,
        relationships_before=relationships_before,relationships_after=relationships_after_fingerprints,
        relationship_file_existed=relationship_file_existed,extras=extras,
    )
    return archive,candidate_tag_payload,candidate_category_payload,candidate_relationship_payload


def prepare_removed_archive_import(
    app_root: Path,envelopes: tuple[dict[str,Any],...],*,
    tags_payload: dict[str,Any] | None=None,categories_payload: dict[str,Any] | None=None,
    relationships_payload: dict[str,Any] | None=None,
    relationship_file_existed: bool | None=None,
    incoming_tags: tuple[dict[str,Any],...]=(),incoming_categories: tuple[dict[str,Any],...]=(),
    tag_id_remap: dict[str,str] | None=None,category_id_remap: dict[str,str] | None=None,
    ambiguous_tag_ids: set[str] | None=None,ambiguous_category_ids: set[str] | None=None,
) -> RemovedArchivePreparation:
    app_root=Path(app_root); user=app_root/"data"/"user"
    tags_payload=copy.deepcopy(
        tags_payload if tags_payload is not None
        else _read_payload(
            user/"tags.json",schema_version=TagDatabase.SCHEMA_VERSION,key="tags",
        )
    )
    categories_payload=copy.deepcopy(categories_payload if categories_payload is not None else _read_payload(user/"categories.json",schema_version=CategoryRegistry.SCHEMA_VERSION,key="categories"))
    relationships_payload=copy.deepcopy(relationships_payload if relationships_payload is not None else _read_payload(user/"relationships.json",schema_version=1,key="relationships"))
    relationship_file_existed=(user/"relationships.json").exists() if relationship_file_existed is None else bool(relationship_file_existed)
    current=TagDatabase(user/"tags.json")
    existing_by_id=dict(current.removed_tag_archives)
    existing_by_tag={stable_id:archive for archive in current.active_removed_archives() for stable_id in archive.tag_ids}
    default_tags=_default_tag_records(app_root)
    default_category_payload=_read_payload(
        app_root/"data"/"defaults"/"categories.json",schema_version=5,key="categories"
    )
    default_categories={
        str(item.get("id") or ""):CategoryRecord.from_dict(item,source="default").to_dict()
        for item in default_category_payload.get("categories",[])
        if isinstance(item,dict) and str(item.get("id") or "")
    }
    default_relationships=dict(
        RelationshipStore(app_root/"data"/"defaults"/"relationships.json").relationships
    )
    removed_ids={
        str(value) for value in tags_payload.get("removed_tag_ids",[]) if str(value)
    }
    tag_id_remap=dict(tag_id_remap or {}); category_id_remap=dict(category_id_remap or {})
    ambiguous_tag_ids=set(ambiguous_tag_ids or ()); ambiguous_category_ids=set(ambiguous_category_ids or ())
    decisions=[]; prepared=[]
    for envelope in envelopes:
        archive_id=str(envelope["id"]); tag_ids=tuple(str(item.get("id") or "") for item in envelope["removed_tags"])
        label=", ".join(str(item.get("display") or item.get("id") or "") for item in envelope["removed_tags"][:3])
        if len(tag_ids)>3:label+=f" and {len(tag_ids)-3:,} more"
        existing=existing_by_id.get(archive_id)
        if existing is not None:
            known=str(existing.extras.get(_IMPORT_FINGERPRINT_KEY) or "")
            if known==str(envelope["fingerprint"]):
                decisions.append(KnowledgePackRemovalDecision(archive_id,label,tag_ids,"already_present","The same complete removal archive is already present.")); continue
            decisions.append(KnowledgePackRemovalDecision(archive_id,label,tag_ids,REMOVAL_HOLD,"A different local removal archive uses this stable archive ID.")); continue
        overlap=next((existing_by_tag[value] for value in tag_ids if value in existing_by_tag),None)
        if overlap is not None:
            decisions.append(KnowledgePackRemovalDecision(archive_id,label,tag_ids,REMOVAL_HOLD,f'Removed identity already belongs to local archive "{overlap.id}".')); continue
        tag_dependencies,category_dependencies=_envelope_dependencies(envelope)
        ambiguous_tags=sorted(tag_dependencies&ambiguous_tag_ids)
        ambiguous_categories=sorted(category_dependencies&ambiguous_category_ids)
        if ambiguous_tags or ambiguous_categories:
            kind="Tag" if ambiguous_tags else "Category"
            values=ambiguous_tags or ambiguous_categories
            decisions.append(KnowledgePackRemovalDecision(
                archive_id,label,tag_ids,REMOVAL_HOLD,
                f'{kind} dependency "{values[0]}" matches multiple local identities.',
            )); continue
        try:
            archive,tags_payload,categories_payload,relationships_payload=_prepare_one(
                app_root,envelope,tags_payload,categories_payload,relationships_payload,
                relationship_file_existed=relationship_file_existed,
                incoming_tags=incoming_tags,incoming_categories=incoming_categories,
                default_tags=default_tags,default_categories=default_categories,
                default_relationships=default_relationships,
                known_removed_ids=removed_ids,
                tag_id_remap=tag_id_remap,category_id_remap=category_id_remap,
            )
        except (ValueError,KeyError,TypeError) as exc:
            decisions.append(KnowledgePackRemovalDecision(archive_id,label,tag_ids,REMOVAL_HOLD,str(exc))); continue
        prepared.append(archive)
        removed_ids.update(archive.tag_ids)
        decisions.append(KnowledgePackRemovalDecision(archive_id,label,tag_ids,"add","Add this complete removed-vocabulary recovery batch."))
        existing_by_id[archive.id]=archive
        for value in archive.tag_ids:existing_by_tag[value]=archive
    archives_payload=[archive.to_dict() for archive in current.removed_tag_archives.values()]
    archives_payload.extend(archive.to_dict() for archive in prepared)
    tags_payload["removed_tag_ids"]=sorted(removed_ids)
    tags_payload["removed_tag_archives"]=archives_payload
    return RemovedArchivePreparation(tuple(decisions),tags_payload,categories_payload,relationships_payload)
