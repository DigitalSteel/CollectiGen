from __future__ import annotations
import hashlib, json, re, threading
from functools import wraps
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from .image_record import ImageRecord
from .tag_collection import TagCollection, normalize_tag
from .operation_journal import OperationJournal
from .organization_history_scope import (
    AddRecoveryInspection, AddRecoveryResult, OrganizationHistoryScope, read_interrupted_add_receipt,
)
from .history_versions import _reject_reparse, _source_token
from .confirmed_tag_identity import ConfirmedTagIdentityService
from .library_profiles import LibraryBoundaryDecision
from .verified_transfer import (
    LIBRARY_NAMING_KEEP,TransferRecoveryRequired,VerifiedTransferEngine,
    VerifiedTransferPlan,
)

MOVE_HISTORY_MAX_BYTES=16*1024*1024
MOVE_HISTORY_MAX_RECORDS=10000


def _guard_linked_route(operation):
    """Keep a restored-copy route atomic with its Recycle lifecycle publisher."""
    @wraps(operation)
    def guarded(self,image,destination,**kwargs):
        previous=self.record_for_path(Path(image)) if self.history_coordinator and kwargs.get("admitted_history") is None else None
        if previous is not None and (previous.recycle_link or previous.recycle_lifecycle):
            with self._lock:return operation(self,image,destination,**kwargs)
        return operation(self,image,destination,**kwargs)
    return guarded

@dataclass
class ChangeRecord:
    image: str
    before: list[str]
    after: list[str]
    reason: str
    timestamp: str
    before_rejected: list[str] = field(default_factory=list)
    after_rejected: list[str] = field(default_factory=list)
    operation_id: str = ""
    before_confirmed_absent: list[str] | None = None
    after_confirmed_absent: list[str] | None = None
    before_export_excluded: list[str] | None = None
    after_export_excluded: list[str] | None = None

@dataclass
class MoveRecord:
    files: list[dict[str, str]]
    reason: str
    timestamp: str
    undone: bool = False
    operation_id: str = ""
    operation_kind: str = "move"
    history_move: dict | None = None
    group_id: str = ""
    restores_operation_id: str = ""
    recycle_link: dict | None = None
    recycle_lifecycle: dict | None = None

class TagEditService:
    def __init__(self, history_path: Path, knowledge=None):
        self.history_path=Path(history_path)
        self.knowledge=knowledge
        self.journal=OperationJournal(self.history_path.with_name("operation_journal.jsonl"))

    def _load_tags(self,image:Path)->tuple[ImageRecord,list[str]]:
        rec=ImageRecord(image); return rec,list(rec.annotation.sdxl_tags)

    @staticmethod
    def _apply_tag_changes(
        values,
        *,
        remove_set: set[str],
        replacements: dict[str, str],
    ) -> list[str]:
        out=[]; seen=set()
        for tag in values:
            key=normalize_tag(tag)
            if key in remove_set:
                continue
            value=replacements.get(key,tag)
            normalized=normalize_tag(value)
            if normalized and normalized not in seen:
                seen.add(normalized)
                out.append(str(value).strip())
        return out

    def apply(self,image:Path, *, add=(), remove=(), replacements=None, reject=(), reason="manual edit") -> ChangeRecord:
        rec,before=self._load_tags(image)
        before_rejected=list(rec.annotation.source.get("rejected_tags",[]))
        before_confirmed_absent=list(rec.annotation.confirmed_absent)
        before_export_excluded=list(rec.annotation.export_excluded)
        remove_set={normalize_tag(x) for x in remove}
        raw_replacements=dict(replacements or {})
        proposed_values=[*add,*raw_replacements.values()]
        identity_service=(
            ConfirmedTagIdentityService(self.knowledge)
            if self.knowledge is not None else None
        )
        resolution=(
            identity_service.require_existing(
                proposed_values,
                stored_ids=rec.annotation.tag_identities,
            )
            if proposed_values else None
        )
        canonical={
            normalize_tag(raw):(
                self.knowledge.tags[stable_id].display
                if stable_id in self.knowledge.tags else str(raw).strip()
            )
            for raw,stable_id in (
                (
                    raw,
                    resolution.identity_by_value.get(normalize_tag(raw),""),
                )
                for raw in proposed_values
            )
        } if resolution is not None else {}
        replacements={
            normalize_tag(key):canonical.get(normalize_tag(value),value)
            for key,value in raw_replacements.items()
        }
        out=self._apply_tag_changes(
            rec.annotation.sdxl_tags,
            remove_set=remove_set,
            replacements=replacements,
        )
        seen={normalize_tag(tag) for tag in out}
        for raw_tag in add:
            tag=canonical.get(normalize_tag(raw_tag),raw_tag)
            n=normalize_tag(tag)
            if n and n not in seen: seen.add(n); out.append(tag.strip())
        rec.annotation.sdxl_tags=TagCollection(out).to_list()
        confirmed_absent=self._apply_tag_changes(
            before_confirmed_absent,
            remove_set=remove_set,
            replacements=replacements,
        )
        confirmed={normalize_tag(tag) for tag in rec.annotation.all_structured_tags()}
        rec.annotation.confirmed_absent=[
            tag for tag in confirmed_absent
            if normalize_tag(tag) not in confirmed
        ]
        export_excluded=self._apply_tag_changes(
            before_export_excluded,
            remove_set=remove_set,
            replacements=replacements,
        )
        rec.annotation.export_excluded=[
            tag for tag in export_excluded
            if normalize_tag(tag) in confirmed
        ]
        rejected=TagCollection(before_rejected); rejected.extend(reject)
        accepted={normalize_tag(tag) for tag in add}
        accepted.update(normalize_tag(value) for value in replacements.values())
        rejected_values=[tag for tag in rejected if normalize_tag(tag) not in accepted]
        final_values=rec.annotation.all_structured_tags()
        final_resolution=(
            ConfirmedTagIdentityService(self.knowledge).require_existing(
                final_values,
                stored_ids=rec.annotation.tag_identities,
                confirmation=False,
            )
            if final_values else None
        )
        operation_id=self.journal.begin("tag_edit",expected=1,reason=reason,metadata={"image":str(image)})
        rec.annotation.source["rejected_tags"]=rejected_values
        if final_resolution is not None:
            ConfirmedTagIdentityService.bind_annotation(
                rec.annotation,
                final_resolution,
                values=final_values,
            )
        rec.annotation.source["latest_operation"]={"id":operation_id,"kind":"tag_edit"}
        rec.modified=True
        try:
            rec.save_sidecar(force=True)
            rec.export_caption(knowledge=self.knowledge)
        except Exception as exc:
            self.journal.finish(operation_id,"failed",message=str(exc)); raise
        after=list(rec.annotation.sdxl_tags)
        after_confirmed_absent=list(rec.annotation.confirmed_absent)
        after_export_excluded=list(rec.annotation.export_excluded)
        change=ChangeRecord(
            image=str(image),
            before=before,
            after=after,
            reason=reason,
            timestamp=datetime.now().isoformat(timespec='seconds'),
            before_rejected=before_rejected,
            after_rejected=rejected_values,
            operation_id=operation_id,
            before_confirmed_absent=before_confirmed_absent,
            after_confirmed_absent=after_confirmed_absent,
            before_export_excluded=before_export_excluded,
            after_export_excluded=after_export_excluded,
        )
        self.journal.append_change(
            operation_id,
            item=str(image),
            before={
                "tags":before,
                "rejected":before_rejected,
                "confirmed_absent":before_confirmed_absent,
                "export_excluded":before_export_excluded,
            },
            after={
                "tags":after,
                "rejected":rejected_values,
                "confirmed_absent":after_confirmed_absent,
                "export_excluded":after_export_excluded,
            },
            reason=reason,
        )
        self.journal.finish(operation_id); self.journal.prune()
        self._append(change); return change

    def restore(self, change: ChangeRecord) -> None:
        rec=ImageRecord(Path(change.image)); operation_id=self.journal.begin("tag_edit_restore",expected=1,reason=f"Undo: {change.reason}",metadata={"restores":change.operation_id})
        current={
            "tags":list(rec.annotation.sdxl_tags),
            "rejected":list(rec.annotation.source.get("rejected_tags",[])),
            "confirmed_absent":list(rec.annotation.confirmed_absent),
            "export_excluded":list(rec.annotation.export_excluded),
        }
        rec.annotation.sdxl_tags=TagCollection(change.before).to_list()
        rec.annotation.source["rejected_tags"]=list(change.before_rejected)
        if change.before_confirmed_absent is not None:
            rec.annotation.confirmed_absent=TagCollection(change.before_confirmed_absent).to_list()
        if change.before_export_excluded is not None:
            rec.annotation.export_excluded=TagCollection(change.before_export_excluded).to_list()
        rec.annotation.source["latest_operation"]={"id":operation_id,"kind":"tag_edit_restore"}
        if self.knowledge is not None:
            ConfirmedTagIdentityService(
                self.knowledge
            ).synchronize_annotation(rec.annotation)
        rec.modified=True
        try:
            rec.save_sidecar(force=True)
            rec.export_caption(knowledge=self.knowledge)
        except Exception as exc:
            self.journal.finish(operation_id,"failed",message=str(exc)); raise
        restored={
            "tags":list(rec.annotation.sdxl_tags),
            "rejected":list(rec.annotation.source.get("rejected_tags",[])),
            "confirmed_absent":list(rec.annotation.confirmed_absent),
            "export_excluded":list(rec.annotation.export_excluded),
        }
        self.journal.append_change(operation_id,item=change.image,before=current,after=restored,reason=f"Undo: {change.reason}")
        self.journal.finish(operation_id); self.journal.prune()

    def _append(self,change:ChangeRecord):
        self.history_path.parent.mkdir(parents=True,exist_ok=True)
        data=[]
        if self.history_path.exists():
            try:data=json.loads(self.history_path.read_text(encoding='utf-8'))
            except Exception:data=[]
        data.append(change.__dict__); data=data[-1000:]
        self.history_path.write_text(json.dumps(data,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')

class ProblemFileService:
    """Compatibility facade over the shared verified transfer engine.

    Application workflows use ``MoveHistoryService`` so every transaction is
    journaled. This retained API delegates to the same engine for older callers.
    """

    def __init__(self) -> None:
        self._engine=VerifiedTransferEngine()

    @staticmethod
    def related_files(image: Path) -> list[Path]:
        return list(VerifiedTransferEngine.related_files(image))

    def move_pair(
        self,image: Path,destination: Path,*,relative_to: Path | None=None,
    ) -> list[Path]:
        plan=self._engine.preview(
            image,destination,relative_to=relative_to,
            collision_policy="increment",
        )
        hashes=self._engine.prepare_hashes(plan)
        self._engine.execute(plan,hashes,f"compat_{uuid4().hex}")
        return [member.target for member in plan.members]


class MoveHistoryService:
    """Verified complete-family transfer, recovery, history, and undo authority."""

    def __init__(self, history_path: Path, *, history_coordinator=None, recover=True):
        self.history_path = Path(history_path)
        self._lock = threading.RLock()
        self._engine = VerifiedTransferEngine()
        self.journal=OperationJournal(self.history_path.with_name("operation_journal.jsonl"))
        self.recovery_issues: list[str] = []
        self.startup_add_recovery_results: tuple[AddRecoveryResult, ...] = ()
        self.history_coordinator = history_coordinator
        if recover:
            self.recover_pending()

    def recover_pending(self):
        pending=[]
        for operation in self.journal.operations():
            group=operation.metadata.get("group_id")
            if (operation.status!="applying" or operation.kind not in {"file_copy","file_move"}
                    or not isinstance(group,str) or not group.startswith("ingest-add-")):
                continue
            try:
                receipt=read_interrupted_add_receipt(
                    self.journal,OrganizationHistoryScope(group_id=group,
                        recovery_operation_id=operation.id),
                )
            except ValueError:
                receipt=None
            pending.append((operation.id,group,
                "copy" if operation.kind=="file_copy" else "move",receipt))
        decisions=self._recover_interrupted()
        self._recover_complete_history()
        current={operation.id:operation.status for operation in self.journal.operations()}
        pending_ids={operation_id for operation_id,_,_,_ in pending}
        held_reasons={}
        for issue in self.recovery_issues:
            operation_id,separator,reason=issue.partition(": ")
            if separator and operation_id in pending_ids:
                held_reasons[operation_id]=reason
        self.startup_add_recovery_results=tuple(AddRecoveryResult(
            operation_id,group,mode,
            "completed" if decision=="completed" and current.get(operation_id)=="complete"
            else "rolled_back" if decision=="rolled_back" and current.get(operation_id)=="cancelled"
            else "held",receipt,held_reasons.get(operation_id,"")
        ) for operation_id,group,mode,receipt in pending
          for decision in (decisions.get(operation_id),))

    def inspect_add_recovery(self, operation_id: str, *, group_id: str,
                             should_cancel=None) -> AddRecoveryInspection:
        """Read only one recorded family; never invoke a recovery writer here."""
        with self._lock:
            if should_cancel and should_cancel():
                raise InterruptedError("Add recovery check cancelled before changing files.")
            receipt=read_interrupted_add_receipt(self.journal,OrganizationHistoryScope(
                group_id=group_id,recovery_operation_id=operation_id,
            ))
            operation=next(item for item in self.journal.operations() if item.id==operation_id)
            if (operation.status!="applying" or operation.metadata.get("engine_version")!=2
                    or operation.metadata.get("group_id")!=group_id
                    or operation.kind not in {"file_copy","file_move"}):
                raise ValueError("The recorded Add operation is no longer available for this recovery check.")
            payload=self.journal.load_verified_snapshot(operation.changes[-1]["after"],max_bytes=8*1024*1024)
            files=payload.get("files")
            if (not isinstance(files,list) or len(files)!=len(receipt.files)
                    or any(not isinstance(member,dict) for member in files)
                    or tuple((member.get("source"),member.get("target"),member.get("sha256")) for member in files)
                    !=tuple((str(source),str(target),digest) for source,target,digest in receipt.files)
                    or receipt.mode!=("copy" if operation.kind=="file_copy" else "move")):
                raise ValueError("The exact recorded Add family changed during inspection. Check it again.")
            mode=payload.get("mode")
            if mode not in {"copy","copy_remove","rename"} or (mode=="copy")!=(operation.kind=="file_copy"):
                raise ValueError("The recorded Add action and transfer mode disagree.")
            observations=[]
            def observe(path,expected):
                if should_cancel and should_cancel():
                    raise InterruptedError("Add recovery check cancelled before changing files.")
                try:
                    _reject_reparse(path)
                    if not path.exists():
                        observations.append((str(path),"missing"));return "missing"
                    before=_source_token(path)
                    digest=self._engine._sha256(path,should_cancel)
                    after=_source_token(path)
                    state="verified" if digest==expected and before==after else "changed"
                    observations.append((str(path),state,before,after,digest))
                    return state
                except InterruptedError:
                    raise
                except (OSError,RuntimeError,ValueError) as exc:
                    observations.append((str(path),"unavailable",str(exc)))
                    return "unavailable"
            sources=[];targets=[];reason=""
            for source,target,digest in receipt.files:
                source_state=observe(source,digest);target_state=observe(target,digest)
                sources.append(source_state);targets.append(target_state)
                for path in (self._engine._temporary_target(target,operation_id),
                             source.with_name(f".{source.name}.collectigen-restore-{operation_id}.tmp")):
                    staging_state=observe(path,digest)
                    if staging_state in {"changed","unavailable"} and not reason:
                        reason=f"Recorded recovery staging needs attention: {path}"
                if not reason:
                    if source_state in {"changed","unavailable"}:
                        reason=f"The recorded source is {source_state}: {source}"
                    elif target_state in {"changed","unavailable"}:
                        reason=f"The recorded destination is {target_state}: {target}"
                    elif source_state==target_state=="missing":
                        reason=f"Both recorded copies are missing: {source.name}"
            if not reason and mode=="copy" and "missing" in sources:
                reason="A source from the source-preserving Add copy is missing."
            if not reason and mode=="rename" and any(s==t=="verified" for s,t in zip(sources,targets)):
                reason="Both source and destination copies exist after the recorded same-volume move."
            if (not reason and mode=="copy_remove" and "missing" in sources
                    and "missing" in targets):
                reason="Source removal began before every recorded destination member was present."
            latest=next((item for item in self.journal.operations() if item.id==operation_id),None)
            if latest!=operation:
                raise ValueError("The recorded Add operation changed during inspection. Check this family again.")
            fingerprint=hashlib.sha256(json.dumps(
                {"operation":operation.__dict__,"files":observations},
                sort_keys=True,separators=(",",":"),ensure_ascii=False,
            ).encode("utf-8")).hexdigest()
            return AddRecoveryInspection(operation_id,group_id,receipt,fingerprint,
                tuple(sources),tuple(targets),not reason,
                reason or "The recorded files can be checked by exact recovery; saved-history authority will be verified again.")

    def recover_add(self, inspection: AddRecoveryInspection, *, should_cancel=None) -> AddRecoveryResult:
        """Reconcile only the inspected family; never replay an Add batch."""
        if not isinstance(inspection,AddRecoveryInspection):
            raise ValueError("Choose and check an exact Add family before recovery.")
        def result(status,reason):
            outcome=AddRecoveryResult(inspection.operation_id,inspection.group_id,
                inspection.receipt.mode,status,inspection.receipt,reason)
            self.startup_add_recovery_results=tuple(
                outcome if item.operation_id==inspection.operation_id else item
                for item in self.startup_add_recovery_results
            )
            return outcome
        with self._lock:
            admitted_operation=next((item for item in self.journal.operations()
                if item.id==inspection.operation_id),None)
            try:
                current=self.inspect_add_recovery(inspection.operation_id,
                    group_id=inspection.group_id,should_cancel=should_cancel)
            except (OSError,RuntimeError,ValueError) as exc:
                if isinstance(exc,InterruptedError):raise
                return result("held",f"Check this exact family again before recovery: {exc}")
            if current!=inspection:
                return result("held","The recorded operation or files changed after inspection. Check this family again.")
            if not current.ready:
                return result("held",current.reason)
            if should_cancel and should_cancel():
                raise InterruptedError("Add recovery cancelled before changing files.")
            operation=next(item for item in self.journal.operations() if item.id==inspection.operation_id)
            if operation!=admitted_operation:
                return result("held","The recorded operation changed during admission. Check this family again.")
            payload=self.journal.load_verified_snapshot(operation.changes[-1]["after"],max_bytes=8*1024*1024)
            issue_start=len(self.recovery_issues)
            try:
                decisions=self._recover_interrupted(selected_operation=operation,selected_payload=payload)
                saved=next(item for item in self.journal.operations() if item.id==operation.id)
                decision=decisions.get(operation.id)
                if decision=="rolled_back" and saved.status=="cancelled":
                    return result("rolled_back","The partial Add was rolled back and its exact journal result was saved.")
                if decision=="completed" and saved.status=="complete":
                    expected=self._record_value(self._record_from_operation(operation,payload))
                    records=[item for item in self._load() if item.get("operation_id")==operation.id]
                    if records==[expected] and saved.result.get("record")==expected:
                        return result("completed","The exact Add family completed and its saved history was verified.")
                issues=self.recovery_issues[issue_start:]
                reason=(issues[-1].partition(": ")[2] if issues else
                    "The exact saved recovery result could not be verified. Preserve both locations and check recovery again.")
                return result("held",reason)
            except Exception as exc:
                return result("held",f"Exact Add recovery still needs attention: {exc}")

    def preview(
        self,
        image: Path,
        destination: Path,
        *,
        relative_to: Path | None = None,
        collision_policy: str = "increment",
        force_copy: bool = False,
        preserve_source: bool = False,
        naming_policy: str = LIBRARY_NAMING_KEEP,
        should_cancel=None,
    ) -> VerifiedTransferPlan:
        return self._engine.preview(
            image,destination,relative_to=relative_to,
            collision_policy=collision_policy,force_copy=force_copy,
            preserve_source=preserve_source,
            naming_policy=naming_policy,should_cancel=should_cancel,
        )

    @_guard_linked_route
    def move(
        self,
        image: Path,
        destination: Path,
        *,
        relative_to: Path | None = None,
        reason: str = "file move",
        preview: VerifiedTransferPlan | None = None,
        collision_policy: str = "increment",
        force_copy: bool = False,
        preserve_source: bool = False,
        should_cancel=None,
        boundary_decision: LibraryBoundaryDecision | None = None,
        boundary_confirmed: bool = False,
        source_profile_ids: tuple[str, ...] = (),
        destination_profile_id: str = "",
        naming_policy: str = LIBRARY_NAMING_KEEP,
        protected_backup_manifest: str | Path | None = None,
        protected_scope_fingerprint: str = "",
        external_policy_fingerprint: str = "",
        group_id: str = "",
        batch_parent_id: str = "",
        batch_item_id: str = "",
        batch_attempt_id: str = "",
        batch_item_fingerprint: str = "",
        restores_operation_id: str = "",
        admitted_history: dict | None = None,
        prepared_operation=None,
    ) -> MoveRecord:
        image = Path(image)
        destination = Path(destination)
        for identifier in (group_id,restores_operation_id):
            if not isinstance(identifier,str) or len(identifier)>128:
                raise ValueError("Organization operation references exceed their supported bound.")
        batch_fields=(batch_parent_id,batch_item_id,batch_attempt_id,batch_item_fingerprint)
        if any(batch_fields):
            if (not re.fullmatch(r"op_[0-9a-f]{32}", batch_parent_id)
                    or not re.fullmatch(r"item_[0-9]{1,6}", batch_item_id)
                    or not re.fullmatch(r"attempt_[0-9a-f]{32}", batch_attempt_id)
                    or not re.fullmatch(r"[0-9a-f]{64}", batch_item_fingerprint)):
                raise ValueError("An Add Images batch needs complete exact item and attempt identities.")
            if admitted_history is not None or prepared_operation is not None:
                raise ValueError("A saved Add Images batch cannot use a linked History transfer.")
        ordinary_link=bool(admitted_history and admitted_history.get("recycle_lifecycle"))
        if admitted_history is not None and (self.history_coordinator is None or (not restores_operation_id and not ordinary_link)):
            raise ValueError("Frozen organization admission requires its exact native restore operation.")
        linked=bool(admitted_history and (admitted_history.get("copy_recycle") or ordinary_link))
        if linked != (prepared_operation is not None):
            raise ValueError("Linked copy retention requires its exact prepared-operation publisher.")
        if admitted_history is not None and not (ordinary_link or admitted_history.get("organization_restore") or
                (linked and admitted_history.get("copy_recycle_initial"))):
            raise ValueError("Organization restoration requires its selected native receipt.")
        if (
            boundary_decision is not None
            and boundary_decision.blocked_by_default
            and not boundary_confirmed
        ):
            raise PermissionError(
                "This transfer crosses a Local Only privacy boundary and requires the exact preview confirmation."
            )
        plan = preview or self.preview(
            image,destination,relative_to=relative_to,
            collision_policy=collision_policy,force_copy=force_copy,
            preserve_source=preserve_source,
            naming_policy=naming_policy,should_cancel=should_cancel,
        )
        if preview is not None and (
            plan.image.resolve() != image.resolve()
            or (relative_to is None and plan.destination.resolve() != destination.resolve())
            or plan.preserve_source != preserve_source
        ):
            raise ValueError("The verified transfer preview does not match this source and destination.")
        hashes = self._engine.prepare_hashes(plan,should_cancel)
        if admitted_history is not None:
            if admitted_history.get("transfer")!=plan.journal_payload(hashes):
                raise ValueError("The frozen organization family changed before transfer.")
            native=admitted_history
        else:
            previous = self.record_for_path(image) if self.history_coordinator else None
            if (batch_parent_id and previous is not None
                    and (previous.recycle_link or previous.recycle_lifecycle)):
                raise ValueError("A linked History image cannot enter a saved Add Images batch.")
            if previous is not None and (previous.recycle_link or previous.recycle_lifecycle):
                provider=getattr(self,"recycle_history_provider" if previous.recycle_lifecycle else "copy_history_provider",None)
                if provider is None:raise ValueError("Use the linked copied-family lifecycle to move this retained copy.")
                native=provider.admit_restored_route(previous,plan,hashes,should_cancel=should_cancel)
            else:
                native = self.history_coordinator.admit(
                    plan, hashes, prior=previous.history_move if previous else None,
                    should_cancel=should_cancel,
                ) if self.history_coordinator else None
        if linked:self.history_coordinator.validate_linked(native)
        timestamp = datetime.now().isoformat(timespec="seconds")
        metadata = {
            "engine_version":1 if ordinary_link else 3 if linked else 2,
            "image":str(image),
            "destination":str(plan.destination),
            "file_count":len(plan.members),
            "timestamp":timestamp,
            "mode":plan.mode,
            "source_profile_ids":list(source_profile_ids),
            "destination_profile_id":str(destination_profile_id),
            "privacy_boundary_confirmed":bool(boundary_confirmed),
            "transfer_kind":"copy" if preserve_source else "move",
            "naming_policy":plan.naming_policy,
            "image_sha256":plan.image_sha256,
            "original_image_name":image.name,
            "original_image_path":str(image),
            "destination_image_name":(
                plan.members[0].target.name if plan.members else ""
            ),
            "protected_backup_manifest":str(protected_backup_manifest or ""),
            "protected_scope_fingerprint":str(protected_scope_fingerprint or ""),
            "external_policy_fingerprint":str(external_policy_fingerprint or ""),
            "group_id":group_id,"restores_operation_id":restores_operation_id,
        }
        if batch_parent_id:
            metadata.update(batch_parent_id=batch_parent_id,
                            batch_item_id=batch_item_id,
                            batch_attempt_id=batch_attempt_id,
                            batch_item_fingerprint=batch_item_fingerprint)
        if linked:metadata["recycle_lifecycle" if ordinary_link else "recycle_link"]=dict(native["recycle_lifecycle" if ordinary_link else "copy_recycle"])
        journal_kind = "file_move_recycle" if ordinary_link else "file_move_linked" if linked else "file_copy" if preserve_source else "file_move"
        operation_id=self.journal.begin(
            journal_kind,expected=len(plan.members),reason=reason,metadata=metadata,
        )
        payload=plan.journal_payload(hashes)
        if linked:
            native=json.loads(json.dumps(native))
            native["recycle_lifecycle" if ordinary_link else "copy_recycle"]["operation_id"]=operation_id
        if native is not None:
            payload["history_move"] = self.history_coordinator.pointer(native)
        self.journal.append_change(
            operation_id,item=str(image),
            before={"files":[str(member.source) for member in plan.members]},
            after=payload,reason=reason,
            metadata={"stage":"verified_transfer_plan"},
        )
        record = MoveRecord(
            files=[{"from":str(member.source),"to":str(member.target),"sha256":hashes[str(member.source)]}
                for member in plan.members],reason=reason,timestamp=timestamp,operation_id=operation_id,
            operation_kind="recycle" if ordinary_link else "copy_recycle" if linked else "copy" if preserve_source else "move",
            history_move=payload.get("history_move"),group_id=group_id,restores_operation_id=restores_operation_id,
            recycle_link=dict(native["copy_recycle"]) if linked and not ordinary_link else None,
            recycle_lifecycle=dict(native["recycle_lifecycle"]) if ordinary_link else None,
        )
        try:
            if linked:
                self.history_coordinator.prepare_linked(native)
                prepared_operation(record)
            self._execute_transfer(plan, hashes, operation_id, native, should_cancel)
        except TransferRecoveryRequired as exc:
            exc.operation_id=operation_id
            self.recovery_issues.append(f"{operation_id}: {exc}")
            raise
        except InterruptedError as exc:
            if linked:
                interrupted=TransferRecoveryRequired("Linked copy cancellation needs exact restart reconciliation: "+str(exc))
                interrupted.operation_id=operation_id
                raise interrupted from exc
            self.journal.finish(operation_id,"cancelled",message=str(exc))
            raise
        except Exception as exc:
            if linked:
                interrupted=TransferRecoveryRequired("Linked copy retention needs exact restart recovery: "+str(exc))
                interrupted.operation_id=operation_id
                raise interrupted from exc
            self.journal.finish(operation_id,"failed",message=str(exc)); raise
        try:
            self._append_record(record)
        except Exception as exc:
            message=(
                "The files reached the verified destination, but durable history still needs "
                f"recovery on restart: {exc}"
            )
            self.recovery_issues.append(f"{operation_id}: {message}")
            interrupted=TransferRecoveryRequired(message);interrupted.operation_id=operation_id
            raise interrupted from exc
        try:
            self.journal.finish(operation_id,result={"record":self._record_value(record),"engine_version":1 if ordinary_link else 3 if linked else 2})
            if not batch_parent_id:
                self.journal.prune()
        except Exception as exc:
            if not restores_operation_id and not linked:raise
            interrupted=TransferRecoveryRequired("The inverse family was saved; its exact journal completion needs recovery: "+str(exc))
            interrupted.operation_id=operation_id
            self.recovery_issues.append(f"{operation_id}: {interrupted}")
            raise interrupted from exc
        return record

    def _execute_transfer(self, plan, hashes, operation_id, native, should_cancel):
        if native is None:
            self._engine.execute(plan, hashes, operation_id, should_cancel=should_cancel)
        else:
            self.history_coordinator.execute(native, lambda observer: self._engine.execute(
                plan, hashes, operation_id, should_cancel=should_cancel,
                identity_observer=observer,
            ))

    def copy(
        self,
        image: Path,
        destination: Path,
        *,
        relative_to: Path | None = None,
        reason: str = "file copy",
        preview: VerifiedTransferPlan | None = None,
        collision_policy: str = "increment",
        should_cancel=None,
        boundary_decision: LibraryBoundaryDecision | None = None,
        boundary_confirmed: bool = False,
        source_profile_ids: tuple[str, ...] = (),
        destination_profile_id: str = "",
        group_id: str = "",
        batch_parent_id: str = "",
        batch_item_id: str = "",
        batch_attempt_id: str = "",
        batch_item_fingerprint: str = "",
    ) -> MoveRecord:
        """Create a verified source-preserving family copy with contextual Undo."""
        return self.move(
            image,
            destination,
            relative_to=relative_to,
            reason=reason,
            preview=preview,
            collision_policy=collision_policy,
            force_copy=True,
            preserve_source=True,
            should_cancel=should_cancel,
            boundary_decision=boundary_decision,
            boundary_confirmed=boundary_confirmed,
            source_profile_ids=source_profile_ids,
            destination_profile_id=destination_profile_id,
            group_id=group_id,
            batch_parent_id=batch_parent_id,
            batch_item_id=batch_item_id,
            batch_attempt_id=batch_attempt_id,
            batch_item_fingerprint=batch_item_fingerprint,
        )

    def _append_record(self, record: MoveRecord) -> None:
        with self._lock:
            history=self._load()
            if any(str(item.get("operation_id") or "")==record.operation_id for item in history):
                return
            history.append(self._record_value(record))
            # Native receipts are the only authority for transit return/Undo.
            # Global operation retention must not erase a still-reversible
            # private-history pointer and make its image appear untracked.
            self._save([
                item for index,item in enumerate(history)
                if index>=len(history)-1000 or (item.get("history_move") and not item.get("undone",False))
            ])

    @staticmethod
    def _record_value(record):
        value=dict(record.__dict__)
        if value.get("recycle_link") is None:value.pop("recycle_link",None)
        if value.get("recycle_lifecycle") is None:value.pop("recycle_lifecycle",None)
        return value

    def _payload_for_operation(self, operation) -> dict:
        if not operation.changes:
            return {}
        try:
            value=self.journal.load_snapshot(operation.changes[-1]["after"])
            return value if isinstance(value,dict) else {}
        except Exception as exc:
            self.recovery_issues.append(
                f"{operation.id}: recovery evidence could not be read: {exc}"
            )
            return {"recovery_evidence_error":str(exc)}

    @staticmethod
    def _record_from_operation(operation,payload: dict) -> MoveRecord:
        files=[{
            "from":str(value.get("source") or ""),
            "to":str(value.get("target") or ""),
            "sha256":str(value.get("sha256") or ""),
        } for value in payload.get("files",[]) if isinstance(value,dict)]
        return MoveRecord(
            files=files,reason=operation.reason,
            timestamp=str(operation.metadata.get("timestamp") or operation.created_at),
            operation_id=operation.id,
            operation_kind=str(
                ("recycle" if operation.kind=="file_move_recycle" else "copy_recycle" if operation.kind=="file_move_linked" else operation.metadata.get("transfer_kind"))
                or ("copy" if operation.kind == "file_copy" else "move")
            ),
            history_move=payload.get("history_move"),
            group_id=str(operation.metadata.get("group_id") or ""),
            restores_operation_id=str(operation.metadata.get("restores_operation_id") or ""),
            recycle_link=({**operation.metadata["recycle_link"],"operation_id":operation.id}
                if operation.kind=="file_move_linked" and operation.metadata.get("recycle_link") else None),
            recycle_lifecycle=({**operation.metadata["recycle_lifecycle"],"operation_id":operation.id}
                if operation.kind=="file_move_recycle" and operation.metadata.get("recycle_lifecycle") else None),
        )

    def _recover_interrupted(self, *, selected_operation=None, selected_payload=None) -> dict[str,str]:
        decisions={}
        operations=(selected_operation,) if selected_operation is not None else self.journal.operations()
        for operation in operations:
            if operation.status!="applying" or int(operation.metadata.get("engine_version") or 0)!=(1 if operation.kind=="file_move_recycle" else 3 if operation.kind=="file_move_linked" else 2):
                continue
            if operation.kind not in {
                "file_move", "file_move_undo", "file_copy", "file_copy_undo", "file_move_linked", "file_move_recycle",
            }:
                continue
            group=operation.metadata.get("group_id")
            if (operation.kind in {"file_copy","file_move"} and isinstance(group,str)
                    and group.startswith("ingest-add-")):
                try:
                    read_interrupted_add_receipt(self.journal,OrganizationHistoryScope(
                        group_id=group,recovery_operation_id=operation.id,
                    ))
                except ValueError as exc:
                    self.recovery_issues.append(
                        f"{operation.id}: Add Images recovery needs its exact recorded family: {exc}"
                    )
                    continue
            payload=selected_payload if selected_operation is not None else self._payload_for_operation(operation)
            if payload.get("recovery_evidence_error"):
                continue
            try:
                if operation.kind in {"file_move_linked","file_move_recycle"}:
                    if not payload.get("history_move") or self.history_coordinator is None:
                        raise RuntimeError("Linked recovery requires its exact native receipt; unmanaged recovery is unavailable.")
                    native=self.history_coordinator._load(payload["history_move"])
                    ordinary=operation.kind=="file_move_recycle"
                    expected={**(operation.metadata.get("recycle_lifecycle" if ordinary else "recycle_link") or {}),"operation_id":operation.id}
                    if native["intent"].get("recycle_lifecycle" if ordinary else "copy_recycle")!=expected:
                        raise RuntimeError("Linked journal and native operation identities disagree.")
                if payload.get("history_move"):
                    if self.history_coordinator is None:
                        raise RuntimeError("Configure saved-history recovery before recovering this native move.")
                    outcome = self.history_coordinator.recover(
                        payload["history_move"], payload, self._engine, operation.id,
                    )
                else:
                    outcome = (
                        self._engine.recover_copy_undo(payload, operation.id)
                        if operation.kind == "file_copy_undo"
                        else self._engine.recover(payload, operation.id)
                    )
            except Exception as exc:
                self.recovery_issues.append(f"{operation.id}: recovery inspection failed: {exc}")
                continue
            if outcome.status=="attention":
                self.recovery_issues.append(f"{operation.id}: {outcome.message}")
                continue
            if outcome.status=="rolled_back":
                self.journal.finish(operation.id,"cancelled",message=outcome.message)
                decisions[operation.id]="rolled_back"
                continue
            if operation.kind in {"file_move", "file_copy", "file_move_linked", "file_move_recycle"}:
                record=self._record_from_operation(operation,payload)
                try:
                    self._append_record(record)
                except Exception as exc:
                    self.recovery_issues.append(
                        f"{operation.id}: verified files recovered but history could not be saved: {exc}"
                    )
                    continue
                self.journal.finish(
                    operation.id,result={
                        "record":self._record_value(record),"engine_version":operation.metadata["engine_version"],"recovered":True,
                    },
                )
                decisions[operation.id]="completed"
            else:
                if not self._mark_history_undone(
                    str(operation.metadata.get("restores_operation_id") or "")
                ):
                    self.recovery_issues.append(
                        f"{operation.id}: restored files were verified but their history record is unavailable."
                    )
                    continue
                self.journal.finish(
                    operation.id,result={"engine_version":2,"recovered":True},
                )
                original=str(operation.metadata.get("restores_operation_id") or "")
                if original:
                    self.journal.finish(original,"undone")
        return decisions

    def _recover_complete_history(self) -> None:
        for operation in self.journal.operations():
            if (
                operation.kind not in {"file_move", "file_copy", "file_move_linked", "file_move_recycle"}
                or operation.status != "complete"
            ):
                continue
            record_value=operation.result.get("record")
            if not isinstance(record_value,dict):
                continue
            try:
                self._append_record(MoveRecord(**record_value))
            except Exception as exc:
                self.recovery_issues.append(
                    f"{operation.id}: completed transfer history could not be restored: {exc}"
                )

    def can_undo(self) -> bool:
        return self.last_available() is not None

    def last_available(self) -> MoveRecord | None:
        with self._lock:
            try:history=self._load()
            except RuntimeError as exc:
                if str(exc) not in self.recovery_issues:self.recovery_issues.append(str(exc))
                return None
            for item in reversed(history):
                if not item.get("undone", False):
                    try:return MoveRecord(**item)
                    except TypeError:
                        issue="Latest move history has unsupported fields; open contextual history for its coverage."
                        if issue not in self.recovery_issues:self.recovery_issues.append(issue)
                        return None
        return None

    def undo_last(self) -> MoveRecord:
        with self._lock:
            history = self._load()
            index = next((i for i in range(len(history) - 1, -1, -1) if not history[i].get("undone", False)), None)
            if index is None:
                raise ValueError("There are no file moves available to undo.")
            return self._restore_index(history,index)

    def record_for_path(self, path: Path) -> MoveRecord | None:
        """Return the newest reversible move whose destination group contains path."""
        wanted=Path(path).resolve()
        with self._lock:
            for item in reversed(self._load()):
                if item.get("undone",False): continue
                destinations=[Path(entry.get("to","")).resolve() for entry in item.get("files",[]) if entry.get("to")]
                if wanted in destinations:
                    return MoveRecord(**item)
        return None

    def restore_path(
        self,path: Path,*,force_copy: bool=False,should_cancel=None,
        boundary_decision: LibraryBoundaryDecision|None=None,
        boundary_confirmed: bool=False,destination_profile_id: str="",
    ) -> MoveRecord:
        """Restore one specific moved image group instead of only the newest move."""
        if (
            boundary_decision is not None
            and boundary_decision.blocked_by_default
            and not boundary_confirmed
        ):
            raise PermissionError(
                "This restore crosses the Local Only privacy boundary and requires the exact preview confirmation."
            )
        wanted=Path(path).resolve()
        with self._lock:
            history=self._load()
            index=next((
                i for i in range(len(history)-1,-1,-1)
                if not history[i].get("undone",False) and wanted in {
                    Path(entry.get("to","")).resolve() for entry in history[i].get("files",[]) if entry.get("to")
                }
            ),None)
            if index is None: raise ValueError("No reversible move was found for this image.")
            return self._restore_index(
                history,index,force_copy=force_copy,should_cancel=should_cancel,
                boundary_decision=boundary_decision,
                boundary_confirmed=boundary_confirmed,
                destination_profile_id=destination_profile_id,
            )

    def _restore_index(
        self,history: list[dict],index: int,*,force_copy: bool=False,
        should_cancel=None,boundary_decision: LibraryBoundaryDecision|None=None,
        boundary_confirmed: bool=False,destination_profile_id: str="",
    ) -> MoveRecord:
        item = history[index]
        if item.get("recycle_link") or item.get("recycle_lifecycle") or item.get("operation_kind") in {"copy_recycle","recycle"}:
            raise ValueError("Use the exact linked Recycle lifecycle to restore this copied family.")
        recorded_targets = {
            Path(entry.get("to", "")).resolve()
            for entry in item.get("files", [])
            if entry.get("to")
        }
        if recorded_targets:
            image_target = Path(item["files"][0]["to"])
            additional = [
                path for path in self._engine.related_files(image_target)
                if path.resolve() not in recorded_targets
            ]
            if additional:
                raise RuntimeError(
                    "Related files were created after the transfer. Review or move the "
                    "current complete family before using Undo: "
                    + ", ".join(path.name for path in additional)
                )
        if str(item.get("operation_kind") or "move") == "copy":
            return self._undo_copy_index(history, index)
        pairs = [(Path(entry["from"]), Path(entry["to"])) for entry in item.get("files", [])]
        reverse_pairs=[(target,source) for source,target in pairs]
        plan=self._engine.plan_exact(
            reverse_pairs,
            image=reverse_pairs[0][0] if reverse_pairs else self.history_path,
            force_copy=force_copy,
        )
        hashes=self._engine.prepare_hashes(plan,should_cancel)
        for entry,(source,_) in zip(item.get("files",[]),reverse_pairs):
            expected=str(entry.get("sha256") or "")
            if expected and hashes[str(source)]!=expected:
                raise RuntimeError(
                    f"A moved file changed after the transfer: {source}"
                )
        if item.get("history_move") and self.history_coordinator is None:
            raise RuntimeError("Configure saved-history recovery before undoing this native move.")
        native = self.history_coordinator.admit(
            plan, hashes, prior=item.get("history_move"), should_cancel=should_cancel,
        ) if self.history_coordinator else None
        timestamp=datetime.now().isoformat(timespec="seconds")
        operation_id=self.journal.begin(
            "file_move_undo",expected=len(plan.members),
            reason=f"Undo: {item.get('reason') or 'file move'}",
            metadata={
                "engine_version":2,
                "timestamp":timestamp,
                "restores_operation_id":str(item.get("operation_id") or ""),
                "destination_profile_id":str(destination_profile_id or ""),
                "privacy_boundary_confirmed":bool(boundary_confirmed),
                "privacy_boundary_required":bool(
                    boundary_decision is not None
                    and boundary_decision.requires_explicit_confirmation
                ),
            },
        )
        payload=plan.journal_payload(hashes)
        if native is not None:
            payload["history_move"] = self.history_coordinator.pointer(native)
        self.journal.append_change(
            operation_id,item=str(plan.image),
            before={"files":[str(member.source) for member in plan.members]},
            after=payload,reason=f"Undo: {item.get('reason') or 'file move'}",
            metadata={"stage":"verified_transfer_plan"},
        )
        try:
            self._execute_transfer(plan, hashes, operation_id, native, should_cancel)
        except TransferRecoveryRequired as exc:
            self.recovery_issues.append(f"{operation_id}: {exc}")
            raise
        except InterruptedError as exc:
            self.journal.finish(operation_id,"cancelled",message=str(exc))
            raise
        except Exception as exc:
            self.journal.finish(operation_id,"failed",message=str(exc))
            raise

        item["undone"] = True
        history[index] = item
        try:
            self._save(history)
        except Exception as exc:
            message=(
                "The original files were restored, but durable undo history needs recovery "
                f"on restart: {exc}"
            )
            self.recovery_issues.append(f"{operation_id}: {message}")
            raise TransferRecoveryRequired(message) from exc
        self.journal.finish(operation_id,result={"engine_version":2})
        if item.get("operation_id"):
            self.journal.finish(str(item["operation_id"]),"undone")
        return MoveRecord(**item)

    def _undo_copy_index(self, history: list[dict], index: int) -> MoveRecord:
        item = history[index]
        files = [
            {
                "source": str(entry.get("from") or ""),
                "target": str(entry.get("to") or ""),
                "sha256": str(entry.get("sha256") or ""),
            }
            for entry in item.get("files", [])
            if entry.get("from") and entry.get("to") and entry.get("sha256")
        ]
        if not files or len(files) != len(item.get("files", [])):
            raise RuntimeError("Copy Undo is missing verified family evidence.")
        payload = {"mode":"copy_undo", "files":files}
        native = None
        if item.get("history_move"):
            if self.history_coordinator is None:
                raise RuntimeError("Configure saved-history recovery before undoing this native copy.")
            native = self.history_coordinator.admit_copy_undo(item["history_move"], payload)
        timestamp = datetime.now().isoformat(timespec="seconds")
        operation_id = self.journal.begin(
            "file_copy_undo",
            expected=len(files),
            reason=f"Undo: {item.get('reason') or 'file copy'}",
            metadata={
                "engine_version":2,
                "timestamp":timestamp,
                "restores_operation_id":str(item.get("operation_id") or ""),
            },
        )
        if native is not None:
            payload["history_move"] = self.history_coordinator.pointer(native)
        self.journal.append_change(
            operation_id,
            item=str(files[0]["target"]),
            before={"files":[value["target"] for value in files]},
            after=payload,
            reason=f"Undo: {item.get('reason') or 'file copy'}",
            metadata={"stage":"verified_copy_undo_plan"},
        )
        try:
            if native is None:
                self._engine.execute_copy_undo(payload, operation_id)
            else:
                self.history_coordinator.execute_copy_undo(native, self._engine, operation_id)
        except TransferRecoveryRequired as exc:
            self.recovery_issues.append(f"{operation_id}: {exc}")
            raise
        except InterruptedError as exc:
            self.journal.finish(operation_id, "cancelled", message=str(exc))
            raise
        except Exception as exc:
            self.journal.finish(operation_id, "failed", message=str(exc))
            raise

        item["undone"] = True
        history[index] = item
        try:
            self._save(history)
        except Exception as exc:
            message = (
                "The imported copy was removed, but durable Undo history needs "
                f"recovery on restart: {exc}"
            )
            self.recovery_issues.append(f"{operation_id}: {message}")
            raise TransferRecoveryRequired(message) from exc
        self.journal.finish(operation_id, result={"engine_version":2})
        if item.get("operation_id"):
            self.journal.finish(str(item["operation_id"]), "undone")
        return MoveRecord(**item)

    def _mark_history_undone(self, operation_id: str) -> bool:
        if not operation_id:
            return False
        with self._lock:
            history=self._load()
            index=next((
                index for index,item in enumerate(history)
                if str(item.get("operation_id") or "")==operation_id
            ),None)
            if index is None:
                return False
            history[index]["undone"]=True
            self._save(history)
            return True

    def _load(self) -> list[dict]:
        if not self.history_path.exists():
            return []
        from .history_versions import _reject_reparse
        _reject_reparse(self.history_path.absolute())
        if self.history_path.stat().st_size>MOVE_HISTORY_MAX_BYTES:
            raise RuntimeError("Move history exceeds its bounded reader; preserve it for recovery.")
        with self.history_path.open("rb") as stream:raw=stream.read(MOVE_HISTORY_MAX_BYTES+1)
        if len(raw)>MOVE_HISTORY_MAX_BYTES:raise RuntimeError("Move history grew beyond its supported bound.")
        try:value=json.loads(raw)
        except (ValueError,UnicodeError) as exc:
            raise RuntimeError("Move history is unreadable; retain it for exact journal recovery.") from exc
        if not isinstance(value,list) or len(value)>MOVE_HISTORY_MAX_RECORDS or any(not isinstance(item,dict) for item in value):
            raise RuntimeError("Move history has unsupported structure or exceeds its record bound.")
        return value

    def _save(self, history: list[dict]) -> None:
        raw=json.dumps(history,indent=2,ensure_ascii=False)+"\n"
        if len(history)>MOVE_HISTORY_MAX_RECORDS or len(raw.encode("utf-8"))>MOVE_HISTORY_MAX_BYTES:
            raise RuntimeError("Move history exceeds its supported publication bound; exact recovery is required.")
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.history_path.with_suffix(self.history_path.suffix + ".tmp")
        temporary.write_text(raw, encoding="utf-8")
        temporary.replace(self.history_path)
