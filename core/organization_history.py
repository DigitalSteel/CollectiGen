"""Bounded, exact-operation history for complete image-family relocation.

The provider never selects the latest operation implicitly. Native transfer
receipts own identity; the legacy list is a bounded lookup/display source only.
Copy removal is reserved for a qualified Recycle provider, not legacy deletion.
"""
from __future__ import annotations

from dataclasses import asdict,dataclass,fields
import hashlib
import json
from pathlib import Path
from typing import Protocol

from .edit_service import MoveRecord
from .history_configuration import path_key
from .history_versions import _reject_reparse,_source_token
from .library_profiles import LibraryProfile,LibraryPrivacyPolicy,LibraryRole,LibraryBoundaryDecision
from .verified_transfer import VerifiedTransferPlan,TransferRecoveryRequired


MAX_HISTORY_BYTES=16*1024*1024
MAX_HISTORY_RECORDS=10000
MAX_QUERY_PATHS=1000
MAX_FAMILY_MEMBERS=4
MAX_QUERY_SCAN=200


def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False,default=str)


def _digest(value):return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class OrganizationHistoryRow:
    operation_id:str
    member_id:str
    operation_kind:str
    timestamp:str
    reason:str
    original_path:Path|None
    current_path:Path|None
    status:str
    coverage:str
    group_id:str=""
    restores_operation_id:str=""


@dataclass(frozen=True)
class OrganizationHistoryPage:
    rows:tuple[OrganizationHistoryRow,...]=()
    next_cursor:str|None=None
    status:str="available"
    issues:tuple[str,...]=()


@dataclass(frozen=True)
class OrganizationFileChange:
    current_path:Path
    proposed_path:Path
    current_sha256:str
    historical_sha256:str
    current_instance:str
    size:int

    @property
    def newer_data(self):return self.current_sha256!=self.historical_sha256


@dataclass(frozen=True)
class OrganizationRestorePreview:
    operation_id:str
    member_id:str=""
    group_id:str=""
    family:tuple[OrganizationFileChange,...]=()
    source_role:str=""
    destination_role:str=""
    boundary_decision:LibraryBoundaryDecision|None=None
    held:tuple[str,...]=()
    provider:str="relocation"
    record_digest:str=""
    transfer:VerifiedTransferPlan|None=None
    native_intent_json:str=""
    boundary_json:str=""
    fingerprint:str=""
    provider_state_json:str=""

    @property
    def ready(self):return not self.held and self.transfer is not None and bool(self.family)


@dataclass(frozen=True)
class OrganizationRestoreOutcome:
    operation_id:str
    member_id:str
    status:str
    message:str=""
    inverse_operation_id:str=""
    current_path:Path|None=None
    recycle_record_id:str=""
    provider:str="relocation"
    previous_path:Path|None=None


class OrganizationCopyProvider(Protocol):
    """Root-owned Recycle integration must qualify both directions and expiry."""
    def preview_restore(self,record,*,member_ids,cancelled)->OrganizationRestorePreview:...
    def apply_restore(self,preview,*,boundary_confirmed,cancelled)->tuple[OrganizationRestoreOutcome,...]:...


class OrganizationHistoryUnavailable(RuntimeError):
    def __init__(self,status,message):super().__init__(message);self.status=status


class OrganizationHistoryService:
    def __init__(self,moves,*,copy_provider:OrganizationCopyProvider|None=None,recycle_provider=None):
        self.moves=moves
        self.copy_provider=copy_provider or getattr(moves,"copy_history_provider",None)
        self.recycle_provider=recycle_provider or getattr(moves,"recycle_history_provider",None)

    def _records(self):
        path=self.moves.history_path
        _reject_reparse(path.absolute())
        if not path.exists():raise OrganizationHistoryUnavailable("missing","No organization history has been recorded at this location.")
        if path.stat().st_size>MAX_HISTORY_BYTES:
            raise OrganizationHistoryUnavailable("unavailable","Organization history exceeds the bounded reader; preserve it for indexed migration.")
        with path.open("rb") as stream:raw=stream.read(MAX_HISTORY_BYTES+1)
        if len(raw)>MAX_HISTORY_BYTES:raise OrganizationHistoryUnavailable("unavailable","Organization history grew beyond its supported bound.")
        try:value=json.loads(raw)
        except (ValueError,UnicodeError) as exc:
            raise OrganizationHistoryUnavailable("corrupt","Organization history is unreadable; preserve it for exact journal recovery.") from exc
        if not isinstance(value,list):raise OrganizationHistoryUnavailable("unsupported","Unsupported organization history document; no entries were inferred.")
        if any(not isinstance(item,dict) for item in value):
            raise OrganizationHistoryUnavailable("corrupt","Organization history contains malformed entries; restore is held before transfer.")
        if len(value)>MAX_HISTORY_RECORDS:
            raise OrganizationHistoryUnavailable("unavailable","Organization history exceeds the supported record bound.")
        return value,hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _validate_record(value):
        allowed={item.name for item in fields(MoveRecord)}
        if not isinstance(value,dict) or set(value)-allowed:
            raise OrganizationHistoryUnavailable("unsupported","An organization record has unsupported fields or schema.")
        for key in ("operation_id","reason","timestamp"):
            if not isinstance(value.get(key),str) or len(value[key])>(2048 if key=="reason" else 128):
                raise OrganizationHistoryUnavailable("unsupported","An organization record lacks bounded operation metadata.")
        if not value["operation_id"]:
            raise OrganizationHistoryUnavailable("unavailable","An older record has no exact operation identity.")
        for key in ("group_id","restores_operation_id"):
            if not isinstance(value.get(key,""),str) or len(value.get(key,""))>128:
                raise OrganizationHistoryUnavailable("unsupported","Unsupported organization relationship identity.")
        members=value.get("files")
        if not isinstance(members,list) or not 1<=len(members)<=MAX_FAMILY_MEMBERS:
            raise OrganizationHistoryUnavailable("unsupported","The complete file family is unavailable or exceeds its bound.")
        for member in members:
            if not isinstance(member,dict) or set(member)!={"from","to","sha256"}:
                raise OrganizationHistoryUnavailable("unsupported","An organization member lacks exact path/hash evidence.")
            if any(not isinstance(member.get(key),str) or not member[key] or len(member[key])>32768 for key in ("from","to")):
                raise OrganizationHistoryUnavailable("unsupported","Unsupported organization member path.")
            if any(not Path(member[key]).is_absolute() for key in ("from","to")):
                raise OrganizationHistoryUnavailable("unsupported","Relative legacy paths are not restore authority.")
            digest=member.get("sha256")
            if not isinstance(digest,str) or len(digest)!=64 or any(c not in "0123456789abcdef" for c in digest):
                raise OrganizationHistoryUnavailable("unavailable","An older family lacks exact integrity evidence.")
        if any(len({path_key(m[key]) for m in members})!=len(members) for key in ("from","to")):
            raise OrganizationHistoryUnavailable("corrupt","Duplicate paths make this organization record ambiguous.")
        return value

    def _record(self,operation_id):
        if not isinstance(operation_id,str) or not operation_id or len(operation_id)>128:
            raise OrganizationHistoryUnavailable("unsupported","Choose an exact bounded organization operation ID.")
        values,_=self._records()
        matches=[item for item in values if isinstance(item,dict) and item.get("operation_id")==operation_id]
        if len(matches)!=1:
            raise OrganizationHistoryUnavailable("unavailable","The selected operation is missing or has ambiguous duplicate evidence.")
        return self._validate_record(matches[0])

    def _row(self,value):
        kind=value.get("operation_kind","move")
        pointer=value.get("history_move")
        member_id="";coverage="native_relocation"
        if kind in {"copy","copy_recycle"}:coverage="copy_recycle_provider" if self.copy_provider is not None else "copy_provider_unavailable"
        elif kind=="recycle":coverage="recycle_provider" if self.recycle_provider is not None else "recycle_provider_unavailable"
        elif kind!="move":coverage="unsupported"
        elif not pointer:coverage="legacy_identity_unavailable"
        if pointer and self.moves.history_coordinator is not None:
            try:
                receipt=self.moves.history_coordinator._load(pointer)
                member_id=receipt["intent"]["entity_id"]
                if receipt["status"]!="complete":coverage="recovery_required"
            except Exception:coverage="native_receipt_unavailable"
        elif pointer:coverage="configuration_unavailable"
        return OrganizationHistoryRow(value["operation_id"],member_id,kind,value["timestamp"],value["reason"],
            Path(value["files"][0]["from"]),Path(value["files"][0]["to"]),
            "reversed" if value.get("undone") else "recorded",coverage,
            value.get("group_id",""),value.get("restores_operation_id",""))

    def query(self,*,paths=(),entity_ids=(),group_id="",cursor=None,limit=50):
        """Read bounded historical rows without loading current image contents."""
        try:
            if type(limit) is not int or not 1<=limit<=200:raise ValueError("Choose a history page size between 1 and 200.")
            from itertools import islice
            selected=tuple(islice(iter(paths),MAX_QUERY_PATHS+1))
            if len(selected)>MAX_QUERY_PATHS:raise ValueError("Organization history scope exceeds 1000 exact paths.")
            wanted={path_key(path) for path in selected}
            identities=tuple(islice(iter(entity_ids),MAX_QUERY_PATHS+1))
            if len(identities)>MAX_QUERY_PATHS or any(not isinstance(value,str) or len(value)!=32 or any(c not in "0123456789abcdef" for c in value) for value in identities):
                raise ValueError("Choose bounded exact native image identities.")
            wanted_entities=set(identities)
            if self.moves.history_coordinator is not None:
                from .history_versions import HistoryStore
                configuration=self.moves.history_coordinator.configuration
                for path in wanted:
                    descriptor=configuration.resolve(path=path)
                    if descriptor.status=="configured" and path.is_file():
                        entity=HistoryStore(descriptor.scope()).lookup(path)
                        if entity is not None:wanted_entities.add(entity.entity_id)
            if not isinstance(group_id,str) or len(group_id)>128:raise ValueError("Invalid exact organization group.")
            values,digest=self._records();offset=0
            scope_digest=_digest({"paths":sorted(map(str,wanted)),"entities":sorted(wanted_entities),"group":group_id})
            cursor_key=digest+"."+scope_digest
            if cursor is not None:
                prefix,position=cursor.split(":",1)
                if prefix!=cursor_key:return OrganizationHistoryPage(status="stale",issues=("History or scope changed during paging; reload its exact scope.",))
                offset=int(position)
                if not 0<=offset<=len(values):raise ValueError("Invalid organization history cursor.")
            rows=[];issues=[];counts={}
            for value in values:
                if isinstance(value,dict) and isinstance(value.get("operation_id"),str):
                    counts[value["operation_id"]]=counts.get(value["operation_id"],0)+1
            ordered=list(reversed(values));position=offset
            while position<len(ordered) and position-offset<MAX_QUERY_SCAN and len(rows)<limit:
                value=ordered[position];position+=1
                try:
                    value=self._validate_record(value)
                    if counts[value["operation_id"]]!=1:raise OrganizationHistoryUnavailable("corrupt","Duplicate organization operation identity.")
                    if group_id and value.get("group_id")!=group_id:continue
                    path_match=any(path_key(member[key]) in wanted for member in value["files"] for key in ("from","to"))
                    if wanted and not wanted_entities and not path_match:continue
                    row=self._row(value)
                    if wanted_entities:
                        if row.member_id not in wanted_entities and not (path_match and not row.member_id):continue
                    rows.append(row)
                except OrganizationHistoryUnavailable as exc:
                    if len(issues)<20:issues.append(f"{exc.status}: {exc}")
            next_cursor=f"{cursor_key}:{position}" if position<len(ordered) else None
            return OrganizationHistoryPage(tuple(rows),next_cursor,"partial" if issues else "available",tuple(issues))
        except OrganizationHistoryUnavailable as exc:return OrganizationHistoryPage(status=exc.status,issues=(str(exc),))
        except Exception as exc:return OrganizationHistoryPage(status="unavailable",issues=(str(exc),))

    def _boundary(self,source,target):
        coordinator=self.moves.history_coordinator
        if coordinator is None:raise OrganizationHistoryUnavailable("unavailable","Configure exact saved-history authority before restoring organization.")
        settings=coordinator.configuration.settings;registry=settings.library_registry()
        source_profile=registry.profile_for_path(source)
        target_profile=registry.profile_for_path(target)
        def profile(value,path):
            if value is not None:
                if value.recognized_role is None:raise ValueError("Unsupported destination/source storage role.")
                return value
            location=path_key(path)
            for key,label in (("problems_directory","Problems"),("recycle_directory","Recycle Bin")):
                root=getattr(settings,key,"")
                if root and location.is_relative_to(path_key(root)):
                    return LibraryProfile.create(label,path_key(root),LibraryRole.LOCAL_ONLY,stable_id="organization-"+key)
            ingest=getattr(settings,"ingest_directory","")
            if ingest and location.is_relative_to(path_key(ingest)):
                return LibraryProfile.create("Ingest",path_key(ingest),"unclassified",stable_id="organization-ingest")
            return LibraryProfile.create("Unclassified source",location.parent,"unclassified",stable_id="organization-unclassified")
        source_profile=profile(source_profile,source);target_profile=profile(target_profile,target)
        if "unclassified" in {source_profile.role,target_profile.role}:
            decision=LibraryBoundaryDecision(True,True,
                "This exact restore includes an unclassified location. Its folder path does not establish Local Only storage. Review the complete family and destination, then explicitly confirm before verified copying.")
        else:decision=LibraryPrivacyPolicy.movement(source_profile,target_profile)
        return decision,_canonical({"source":asdict(source_profile),"target":asdict(target_profile),"decision":asdict(decision)}),source_profile,target_profile

    @staticmethod
    def _preview_fingerprint(preview):
        value=asdict(preview);value.pop("fingerprint")
        return _digest(value)

    def preview_restore(self,operation_id,member_ids=None,*,cancelled=lambda:False):
        return self._preview_restore(operation_id,member_ids,cancelled=cancelled)

    def _preview_restore(self,operation_id,member_ids=None,*,cancelled=lambda:False,linked=False):
        try:
            if cancelled():raise InterruptedError("Organization preview cancelled.")
            record=self._record(operation_id)
            if record.get("undone"):raise ValueError("This legacy operation was already reversed; choose its recorded current operation.")
            if (record.get("recycle_lifecycle") or record.get("operation_kind")=="recycle") and not linked:
                if self.recycle_provider is None:return OrganizationRestorePreview(operation_id,provider="recycle",held=("The exact Recycle lifecycle provider is unavailable.",))
                return self.recycle_provider.preview_restore(record,member_ids=member_ids,cancelled=cancelled)
            if record.get("operation_kind","move") in {"copy","copy_recycle"} and not linked:
                if self.copy_provider is None:
                    return OrganizationRestorePreview(operation_id,provider="copy_recycle",held=("Copy removal requires the qualified Recycle recovery provider. Legacy permanent deletion is unavailable here.",))
                return self.copy_provider.preview_restore(record,member_ids=member_ids,cancelled=cancelled)
            if record.get("operation_kind","move") not in ({"move","copy_recycle"} if linked else {"move"}):raise ValueError("This organization operation has unsupported restore coverage.")
            pointer=record.get("history_move")
            coordinator=self.moves.history_coordinator
            if not pointer or coordinator is None:raise ValueError("Older organization evidence has no verified native image identity; use its qualified emergency recovery route.")
            receipt=coordinator._load(pointer)
            if receipt["status"]!="complete":raise ValueError("Complete exact transfer recovery before restoring its organization.")
            original=receipt["intent"]
            expected=[{"from":item["source"],"to":item["target"],"sha256":item["sha256"]} for item in original["transfer"]["files"]]
            if record["files"]!=expected:raise ValueError("The organization record differs from its private native receipt.")
            member_id=original["entity_id"]
            if member_ids is not None and tuple(member_ids)!=(member_id,):
                raise ValueError("Select this exact complete image family; companions cannot be restored separately.")
            image=Path(original["target"]);destination=Path(original["source"])
            recycle_root=str(getattr(coordinator.configuration.settings,"recycle_directory","") or "").strip()
            if not linked and recycle_root:
                configured=Path(recycle_root).expanduser()
                if configured.is_absolute():
                    configured=path_key(configured);_reject_reparse(configured)
                    endpoints=(image,destination,*(Path(member[key]) for member in record["files"] for key in ("from","to")))
                    if any(path_key(path).is_relative_to(configured) for path in endpoints):
                        raise ValueError("Use the exact Recycle record recovery; this lifecycle is not yet qualified for generic restoration.")
            if image.suffix.casefold()==".mp4":raise ValueError("Video organization requires its qualified temporal identity provider.")
            engine=self.moves._engine
            current=engine.related_files(image)
            recorded={path_key(item["to"]):item for item in record["files"]}
            if not current or current[0]!=image or not set(recorded).issubset({path_key(path) for path in current}):
                raise ValueError("An original family member is missing; review exact family recovery before relocation.")
            targets={image:destination,**{image.with_suffix(suffix):destination.with_suffix(suffix) for suffix in (".txt",".tags.json",".xmp")}}
            pairs=[]
            for path in current:
                if path not in targets:raise ValueError("The current family contains unsupported companions.")
                pairs.append((path,Path(recorded[path_key(path)]["from"]) if path_key(path) in recorded else targets[path]))
            decision,boundary_json,source_profile,target_profile=self._boundary(image,destination)
            transfer=engine.plan_exact(pairs,image=image,force_copy=LibraryPrivacyPolicy.requires_verified_copy(source_profile,target_profile))
            hashes=engine.prepare_hashes(transfer,cancelled)
            intent=coordinator.admit_organization_restore(transfer,hashes,selected_pointer=pointer,should_cancel=cancelled)
            family=tuple(OrganizationFileChange(member.source,member.target,hashes[str(member.source)],
                recorded.get(path_key(member.source),{}).get("sha256",""),intent["source_tokens"][str(path_key(member.source))],member.size)
                for member in transfer.members)
            preview=OrganizationRestorePreview(operation_id,member_id,record.get("group_id",""),family,
                source_profile.role,target_profile.role,decision,
                record_digest=_digest(record),transfer=transfer,native_intent_json=_canonical(intent),boundary_json=boundary_json)
            from dataclasses import replace
            return replace(preview,fingerprint=self._preview_fingerprint(preview))
        except Exception as exc:return OrganizationRestorePreview(str(operation_id),held=(str(exc),))

    def apply_restore(self,preview,*,boundary_confirmed=False,cancelled=lambda:False):
        inverse_id=""
        try:
            if not isinstance(preview,OrganizationRestorePreview):raise ValueError("Choose a supported organization restore preview.")
            if cancelled():return (OrganizationRestoreOutcome(preview.operation_id,preview.member_id,"cancelled","Cancelled before moving a family."),)
            if preview.provider=="copy_recycle":
                if self.copy_provider is None:raise ValueError("The qualified copy recovery provider is unavailable.")
                return self.copy_provider.apply_restore(preview,boundary_confirmed=boundary_confirmed,cancelled=cancelled)
            if preview.provider=="recycle":
                if self.recycle_provider is None:raise ValueError("The qualified Recycle recovery provider is unavailable.")
                return self.recycle_provider.apply_restore(preview,boundary_confirmed=boundary_confirmed,cancelled=cancelled)
            if not preview.ready:raise ValueError("; ".join(preview.held) or "Organization preview is not ready.")
            if preview.fingerprint!=self._preview_fingerprint(preview):raise ValueError("The immutable organization preview changed.")
            with self.moves._lock:
                record=self._record(preview.operation_id)
                if _digest(record)!=preview.record_digest:raise ValueError("Selected operation evidence changed after preview.")
                transfer=preview.transfer
                decision,boundary_json,source_profile,target_profile=self._boundary(transfer.image,transfer.members[0].target)
                if boundary_json!=preview.boundary_json:raise ValueError("Storage roles or privacy authority changed after preview.")
                if decision.requires_explicit_confirmation and not boundary_confirmed:raise PermissionError("Confirm this exact preview's Local Only boundary before restoring location.")
                if tuple(self.moves._engine.related_files(transfer.image))!=tuple(member.source for member in transfer.members):
                    raise ValueError("Current family membership changed after preview.")
                intent=json.loads(preview.native_intent_json)
                if intent.get("organization_restore",{}).get("pointer")!=record.get("history_move") or intent.get("entity_id")!=preview.member_id:
                    raise ValueError("Preview no longer identifies the selected native organization operation.")
                result=self.moves.move(transfer.image,transfer.destination,preview=transfer,collision_policy="error",
                    reason="Restore selected location: "+record["reason"],should_cancel=cancelled,
                    boundary_decision=decision,boundary_confirmed=boundary_confirmed,
                    source_profile_ids=(source_profile.stable_id,),destination_profile_id=target_profile.stable_id,
                    restores_operation_id=preview.operation_id,admitted_history=intent,group_id=preview.group_id)
            return (OrganizationRestoreOutcome(preview.operation_id,preview.member_id,"complete","The selected current family reached its reviewed location.",
                result.operation_id,Path(result.files[0]["to"])),)
        except InterruptedError as exc:status="cancelled";message=str(exc)
        except TransferRecoveryRequired as exc:
            status="recovery_required";message=str(exc);inverse_id=getattr(exc,"operation_id","")
        except Exception as exc:status="held";message=str(exc)
        return (OrganizationRestoreOutcome(getattr(preview,"operation_id",""),getattr(preview,"member_id",""),status,message,inverse_id),)
