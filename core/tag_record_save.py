"""Immutable single-record Tag writes through the existing verified recovery path."""
from __future__ import annotations

import json
import hashlib
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

from .automation_rule_conversion import RulePairAdmission
from .tag_database import TagDatabase


class _PayloadTagDatabase(TagDatabase):
    """Private byte-backed writer; never a parsed knowledge authority.

    Only inherited save_verified/save/atomic methods may be used. Their
    rollback calls load solely to reset this private writer's byte receipt.
    The request supplies the exact raw payload before any transaction starts;
    final GUI authority is always loaded through a normal TagDatabase.
    """
    def load(self):
        self._loaded_bytes=self.path.read_bytes() if self.path.exists() else None


@dataclass(frozen=True)
class TagRecordSaveRequest:
    path: Path
    expected_bytes: bytes | None
    record_json: str
    created: bool
    rule_pair_admission: RulePairAdmission | None = None


@dataclass(frozen=True)
class TagRecordSaveResult:
    saved_bytes: bytes
    backup_path: Path


def save_tag_record(request: TagRecordSaveRequest) -> TagRecordSaveResult:
    """Patch only the requested record; never borrow mutable application authority.

    Unedited records and future fields stay exactly as represented in the loaded
    JSON. The detached database uses the same stale guard, backup, read-back and
    rollback implementation as ordinary verified Tag writes.
    """
    record = json.loads(request.record_json)
    stable_id = str(record.get("id") or "")
    if not stable_id:
        raise ValueError("The Tag save request has no stable identity.")
    current=request.path.read_bytes() if request.path.exists() else None
    if current != request.expected_bytes:
        raise RuntimeError("System Knowledge changed after this SDXL Tag was opened. Reload it and review the current record before saving again.")
    payload = (json.loads(request.expected_bytes.decode("utf-8"))
               if request.expected_bytes is not None else {"schema_version": TagDatabase.SCHEMA_VERSION, "tags": [], "merge_rules": []})
    if not isinstance(payload, dict) or not isinstance(payload.get("tags", []), list):
        raise ValueError("The saved Tag authority is not a supported object.")
    payload["schema_version"] = max(int(payload.get("schema_version", 0)), TagDatabase.SCHEMA_VERSION)
    records = payload.setdefault("tags", [])
    matches = [i for i, item in enumerate(records)
               if isinstance(item, dict) and str(item.get("id") or "") == stable_id]
    if (request.created and matches) or (not request.created and len(matches) != 1):
        raise RuntimeError("The SDXL Tag identity changed before this save. Reload and review it again.")
    if request.created:
        if stable_id in payload.get("removed_tag_ids",()) or stable_id in payload.get("merged_tag_ids",{}):
            raise RuntimeError("This stable Tag identity is retired. Review its recovery action before saving.")
        records.append(record)
    else:
        records[matches[0]] = record
    # The serialized request already has canonical storage projections removed
    # by TagDatabase._record_payload on the GUI-owned snapshot of this one Tag.
    # The GUI saves a detached record, not its bound live TagDatabase. Carry
    # the exact live authority binding into its verified backup/write/rollback.
    database=_PayloadTagDatabase(
        request.path, rule_pair_admission=request.rule_pair_admission,
    )
    database._loaded_bytes=request.expected_bytes
    database._payload = lambda: payload
    backup = database.save_verified()
    return TagRecordSaveResult(database._loaded_bytes, backup)


@dataclass(frozen=True)
class TagSaveAuthorities:
    category_default: Path
    category_user: Path
    modifiers: Path
    concepts: Path
    relationship_default: Path
    relationship_user: Path
    relationship_default_bytes: bytes | None
    relationship_user_bytes: bytes | None
    source_stats: tuple


@dataclass(frozen=True)
class TagSaveImpactRequest:
    # The disposable index is a locked, per-connection read facade. No live
    # Tag records, services or Qt objects cross this boundary.
    index: object
    source_names: tuple[str, ...] = ()
    renamed: bool = False


@dataclass(frozen=True)
class TagSaveImpact:
    snapshot_token: tuple | None = None
    affected_paths: tuple[str, ...] | None = None
    audit: object = None
    identity_refresh_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class TagDetailsSaveRequest:
    record: TagRecordSaveRequest
    authorities: TagSaveAuthorities
    mode: str = "record"
    changes_json: str = "{}"
    prepare_palette: bool = True
    impact: TagSaveImpactRequest | None = None
    prepare_live_delta: bool = False


@dataclass(frozen=True)
class TagCreatedSaveDelta:
    saved_bytes: bytes
    record: TagRecordSaveRequest
    relationship_bytes: bytes | None
    impact: TagSaveImpact = field(default_factory=TagSaveImpact)


@dataclass(frozen=True)
class TagDetailsSaveResult:
    database: object
    services: dict
    record_id: str
    branch_result: object = None
    palette_index: object = None
    relationship_bytes: bytes | None = None
    impact: TagSaveImpact = field(default_factory=TagSaveImpact)
    # Internal evidence from the verified single-record writer. Relationship
    # transactions require a full workspace rebuild and do not expose this.
    record_change: TagRecordSaveRequest | None = None


def source_stamp(path: Path) -> tuple:
    try:
        return hashlib.sha256(path.read_bytes()).digest()
    except FileNotFoundError:
        return ()


def _check_sources(authorities, *, relationships=True):
    for path, expected in authorities.source_stats:
        if source_stamp(path)!=expected:
            raise RuntimeError("System Knowledge sources changed during this edit. Reload and review it again.")
    checked=[(authorities.relationship_default,authorities.relationship_default_bytes)]
    if relationships:checked.append((authorities.relationship_user,authorities.relationship_user_bytes))
    for path,expected in checked:
        actual=path.read_bytes() if path.exists() else None
        if actual!=expected:
            raise RuntimeError("Relationships changed after this Tag was opened. Reload and review it again.")


def tag_save_authority_is_current(database, services):
    """Check the returned authority's own load receipts before GUI publication."""
    def current_bytes(path):
        return path.read_bytes() if path.exists() else None
    try:
        if current_bytes(database.path)!=database._loaded_bytes:
            return False
        categories=database._categories
        foundation=services["foundation_snapshot"]
        for owner,paths in (
            (categories,(categories.default_path,categories.user_path)),
            (foundation,(foundation.modifiers_path,foundation.concepts_path)),
        ):
            loaded=owner._loaded_source_fingerprints
            if any(path not in loaded or source_stamp(path)!=loaded[path] for path in paths):
                return False
        workspace=services["relationship_workspace"]
        return all(current_bytes(store.path)==store._loaded_bytes
                   for store in (workspace.default_store,workspace.user_store))
    except (OSError,AttributeError,KeyError,TypeError):
        return False


def _finish_steps(steps):
    while True:
        try:
            next(steps)
        except StopIteration as completed:
            return completed.value


def load_tag_save_authority(path, authorities, *, prepared_database=None, rule_pair_admission=None):
    return _finish_steps(prepare_tag_save_authority_steps(
        path,authorities,prepared_database=prepared_database,
        rule_pair_admission=rule_pair_admission))


def prepare_tag_save_authority_steps(path, authorities, *, prepared_database=None, inherited_record=None,
                                     rule_pair_admission=None):
    """Construct worker-owned authority and indexes; never publish to live objects."""
    from .category_registry import CategoryRegistry
    from .knowledge_engine import FoundationKnowledge
    from .relationship_workspace import RelationshipWorkspaceService
    from .tag_alias_relationships import TagAliasRelationshipService
    from .tag_semantic_relationships import TagSemanticRelationshipService
    from .modifier_composition import ModifierCompositionService
    from .modifier_role_decisions import modifier_role_resolutions
    from .allowed_dimension_policy import AllowedDimensionPolicyService

    if rule_pair_admission is not None and not rule_pair_admission.owns(path,"knowledge"):
        raise ValueError("The Tag save authority does not match its rule-pair binding.")
    tags=(TagDatabase(path,rule_pair_admission=rule_pair_admission)
          if prepared_database is None else prepared_database)
    if (prepared_database is not None and rule_pair_admission is not None
            and prepared_database.rule_pair_admission != rule_pair_admission):
        raise ValueError("The prepared Tag authority lost its rule-pair binding.")
    categories=CategoryRegistry(authorities.category_default,authorities.category_user)
    if prepared_database is None:
        tags.apply_category_ordering(categories)
    else:
        tags._categories=categories
        if inherited_record is not None and not inherited_record.ordering.reviewed:
            group,priority,_,_=categories.effective_ordering(inherited_record.category)
            inherited_record.ordering.group=group
            inherited_record.ordering.priority=priority
    yield
    foundation=FoundationKnowledge(authorities.modifiers,authorities.concepts)
    yield
    workspace=RelationshipWorkspaceService(authorities.relationship_default,authorities.relationship_user,tags=tags)
    yield
    def optional(build):
        try:return build(),""
        except (OSError,RuntimeError,ValueError) as error:return None,str(error)
    roles,role_error=optional(lambda:modifier_role_resolutions(workspace.effective,categories=categories))
    roles=roles or {}
    yield
    policy,policy_error=optional(lambda:AllowedDimensionPolicyService(tags,categories,foundation,workspace=workspace,
        default_path=authorities.relationship_default,user_path=authorities.relationship_user,publish_shared_authority=False))
    yield
    composition,composition_error=optional(lambda:ModifierCompositionService(tags,categories,foundation,modifier_roles=roles,workspace=workspace,publish_alias_authority=False))
    if role_error:composition=None;composition_error=role_error
    resolutions,resolution_error=optional(lambda:composition.resolutions()) if composition is not None else ({},composition_error)
    if resolution_error:composition=None;composition_error=resolution_error
    yield
    semantic,semantic_error=optional(lambda:TagSemanticRelationshipService(tags,default_path=authorities.relationship_default,user_path=authorities.relationship_user,workspace=workspace))
    yield
    alias,alias_error=optional(lambda:TagAliasRelationshipService(tags,default_path=authorities.relationship_default,user_path=authorities.relationship_user,workspace=workspace))
    return tags,{
        "relationship_workspace":workspace,"foundation_snapshot":foundation,
        "modifier_roles":roles,"modifier_role_error":role_error,
        "modifier_policy_service":policy,"modifier_policy_error":policy_error,
        "modifier_composition_service":composition,"modifier_compositions":resolutions or {},"modifier_composition_error":composition_error,
        "tag_semantic_service":semantic,"tag_semantic_error":semantic_error,
        "tag_alias_service":alias,"tag_alias_error":alias_error,
    }



def save_tag_details(request: TagDetailsSaveRequest) -> TagDetailsSaveResult | TagCreatedSaveDelta:
    """Hold conversion exclusion through each branch's write and read-back."""
    admission=request.record.rule_pair_admission
    if admission is not None and not admission.owns(request.record.path,"knowledge"):
        raise ValueError("The Tag save request does not match its rule-pair binding.")
    with (admission.admit() if admission is not None else nullcontext()):
        return _save_tag_details_admitted(request)


def _save_tag_details_admitted(request: TagDetailsSaveRequest) -> TagDetailsSaveResult | TagCreatedSaveDelta:
    from .tag_database import TagRecord
    from .tag_alias_relationships import TagAliasChange
    from .tag_semantic_relationships import TagSemanticChange

    _check_sources(request.authorities)
    proposed=TagRecord.from_dict(json.loads(request.record.record_json))
    changes=json.loads(request.changes_json)
    result=None
    committed_relationships=request.authorities.relationship_user_bytes
    selected={"aliases":"tag_alias_service","semantics":"tag_semantic_service","modifiers":"modifier_composition_service"}.get(request.mode)
    if request.mode=="record":
        receipt=save_tag_record(request.record)
        committed_bytes=receipt.saved_bytes
        if request.prepare_live_delta and request.record.created:
            _check_sources(request.authorities)
            if request.record.path.read_bytes()!=committed_bytes:
                raise RuntimeError("The saved Tag changed before its result was prepared.")
            impact=prepare_identity_save_impact(request.impact)
            return TagCreatedSaveDelta(committed_bytes,request.record,committed_relationships,impact)
    else:
        tags,services=load_tag_save_authority(request.record.path,request.authorities,
                                              rule_pair_admission=request.record.rule_pair_admission)
        if tags._loaded_bytes!=request.record.expected_bytes:
            raise RuntimeError("System Knowledge changed after this SDXL Tag was opened. Reload it and review the current record before saving again.")
        if selected is None or services[selected] is None:
            raise RuntimeError("The selected relationship authority is unavailable.")
        if (request.record.created and proposed.id in tags.tags) or (not request.record.created and proposed.id not in tags.tags):
            raise RuntimeError("The Tag identity changed before this save. Reload and review it again.")
        _check_sources(request.authorities)
        tags.tags[proposed.id]=proposed
        tags._rebuild_lookup()
        if request.mode=="aliases":
            result=services["tag_alias_service"].apply((TagAliasChange(
                proposed.id,tuple(changes["aliases"]),source="qt_knowledge_editor",reason="Tag Details automatic save"),))
        elif request.mode=="semantics":
            result=services["tag_semantic_service"].apply((TagSemanticChange(
                proposed.id,parent_id=changes["parent_id"],implies=tuple(changes["implies"]),
                kinds=tuple(changes["kinds"]),source="qt_knowledge_editor",reason="Tag Details automatic save"),))
        elif request.mode=="modifiers":
            result=services["modifier_composition_service"].apply(proposed.id,changes["attributes"],changes["changes"],
                base_tag_id=changes["base_tag_id"],tag_name=proposed.display,expected_fingerprints=changes["fingerprints"])
        else:
            raise ValueError("Unsupported Tag save operation.")
        verified_workspace=services[selected].workspace
        committed_bytes=verified_workspace.tags._loaded_bytes
        committed_relationships=verified_workspace.user_store._loaded_bytes
        del tags,services,verified_workspace
    # Load the verified storage representation and all its canonical indexes in
    # the worker. Adoption on the UI thread only transfers ownership.
    tags,services=load_tag_save_authority(request.record.path,request.authorities,
                                          rule_pair_admission=request.record.rule_pair_admission)
    _check_sources(request.authorities,relationships=False)
    if (tags._loaded_bytes!=committed_bytes or
        services["relationship_workspace"].user_store._loaded_bytes!=committed_relationships):
        raise RuntimeError("The Tag was saved, then changed before its result was prepared. Review the latest System Knowledge before continuing.")
    palette=None
    if request.prepare_palette:
        from .tag_palette_index import prepare_tag_palette_index
        try:palette=prepare_tag_palette_index(tags,tags._categories)
        except Exception:
            # Derived presentation must not turn a verified durable write into
            # a failed save. The editor retains its ordinary refresh fallback;
            # any later UI error is reported separately from persistence.
            palette=None
    impact=prepare_tag_save_impact(tags,proposed.id,request.impact)
    return TagDetailsSaveResult(
        tags,services,proposed.id,result,palette,committed_relationships,impact,
        request.record if request.mode=="record" else None,
    )


def prepare_created_tag_adoption(live, request, delta, *, expected_records, expected_revision):
    return _finish_steps(prepare_created_tag_adoption_steps(live,request,delta,
        expected_records=expected_records,expected_revision=expected_revision))


def prepare_created_tag_adoption_steps(live, request, delta, *, expected_records, expected_revision):
    """Prepare a verified create on the owner thread without cloning old Tags.

    No live records cross the save worker boundary. Candidate maps and derived
    authority are private until publication; unchanged records retain their
    owner and only the newly created record receives inherited ordering.
    """
    import copy
    from .tag_database import TagRecord
    if (delta.record is not request.record or not request.record.created
        or request.mode!="record"
        or live.path!=request.record.path or live.tags is not expected_records
        or getattr(live,"_relationship_signature_revision",0)!=expected_revision
        or live._loaded_bytes!=request.record.expected_bytes):
        raise RuntimeError("The live Tag snapshot changed before adoption.")
    _check_sources(request.authorities)
    if live.path.read_bytes()!=delta.saved_bytes:
        raise RuntimeError("The saved Tag changed before adoption.")
    record=TagRecord.from_dict(json.loads(request.record.record_json))
    if record.id in live.tags or record.id in live.removed_tag_ids or record.id in live.merged_tag_ids:
        raise RuntimeError("The created Tag identity is no longer available.")
    candidate=copy.copy(live)
    candidate.tags=dict(live.tags)
    candidate.tags[record.id]=record
    candidate._loaded_bytes=delta.saved_bytes
    candidate._extend_created_record_lookup(record)
    yield
    candidate,services=yield from prepare_tag_save_authority_steps(
        live.path,request.authorities,prepared_database=candidate,inherited_record=record,
        rule_pair_admission=request.record.rule_pair_admission)
    yield
    palette=None
    if request.prepare_palette:
        from .tag_palette_index import prepare_tag_palette_index_steps
        try:
            palette=yield from prepare_tag_palette_index_steps(candidate,candidate._categories)
        except Exception:
            # Optional presentation cannot invalidate a verified durable save.
            # GeneratorExit still closes abandoned preparation normally.
            palette=None
    yield
    _check_sources(request.authorities)
    if (live.path.read_bytes()!=delta.saved_bytes or live.tags is not expected_records
        or live._loaded_bytes!=request.record.expected_bytes
        or getattr(live,"_relationship_signature_revision",0)!=expected_revision):
        raise RuntimeError("System Knowledge changed while adoption was prepared.")
    return TagDetailsSaveResult(candidate,services,record.id,palette_index=palette,
        relationship_bytes=delta.relationship_bytes,impact=delta.impact,record_change=request.record)


def prepare_identity_save_impact(request):
    """Resolve a newly known name through cached evidence, without a catalog read."""
    if request is None:return TagSaveImpact()
    from .evidence_query import EvidenceQueryService
    try:
        index=request.index; before=index.evidence_snapshot_token()
        if not _valid_impact_token(before) or not request.source_names:return TagSaveImpact()
        paths=tuple(EvidenceQueryService().query(index,(request.source_names,),False))
        if index.evidence_snapshot_token()!=before:return TagSaveImpact()
        return TagSaveImpact(before,paths,identity_refresh_paths=paths)
    except Exception:return TagSaveImpact()


def prepare_tag_save_impact(tags,record_id,request):
    """Read only cached evidence; unknown/stale coverage never proves no uses."""
    if request is None:return TagSaveImpact()
    from .confirmed_tag_identity import ConfirmedTagIdentityService
    from .evidence_query import EvidenceQueryService
    try:
        index=request.index
        before=index.evidence_snapshot_token()
        if not _valid_impact_token(before):return TagSaveImpact()
        record=tags.tags[record_id]
        names=tuple(tags.identity_names(record))
        audit=ConfirmedTagIdentityService.audit_cached_uses(
            (record,),index.all_records(),
            names_for=lambda _:(*names,*request.source_names),
        )
        query_names=tuple(dict.fromkeys((*names,*request.source_names)))
        paths=tuple(EvidenceQueryService().query(index,(query_names,),False)) if request.renamed else ()
        if before!=index.evidence_snapshot_token():return TagSaveImpact()
        return TagSaveImpact(before,tuple(dict.fromkeys((*audit.used_paths,*audit.uncertain_paths))),audit,paths)
    except Exception:
        return TagSaveImpact()


def _valid_impact_token(token):
    if not isinstance(token,tuple) or not token:return False
    if token[0]=="federated":
        return len(token)==2 and all(_valid_impact_token(tuple(item[1:])) for item in token[1])
    return len(token)>=3 and isinstance(token[-1],int) and token[-1]>=0
