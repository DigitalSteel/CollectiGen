"""Exact copied-family retention and inverse publication through configured Recycle.

Initial removal proves original source and copy. All later inverses use only the
retained copied identity and its selected native placement receipt.
"""
from __future__ import annotations

from dataclasses import asdict,replace
from contextlib import contextmanager,ExitStack
import json
from pathlib import Path

from .history_configuration import path_key
from .history_versions import HistoryStore,_source_token,history_lock,_reject_reparse
from .organization_history import (
    OrganizationHistoryService,OrganizationRestorePreview,OrganizationRestoreOutcome,
    OrganizationFileChange,_canonical,_digest,
)
from .recycle_bin import RecycleRecord,RecycleFile
from .verified_transfer import TransferRecoveryRequired
from .history_recycle_support import NativeRecycleSupport


class OrganizationCopyHistoryProvider(NativeRecycleSupport):
    def __init__(self,recycle):
        self.recycle=recycle
        self.moves=recycle.moves

    def _history(self):return OrganizationHistoryService(self.moves,copy_provider=self)

    @staticmethod
    def _link(record):
        link=record._extra.get("copy_lifecycle")
        if not isinstance(link,dict) or link.get("schema_version")!=1:
            raise ValueError("Unsupported copied-family Recycle lifecycle.")
        required={"schema_version","copy_operation_id","member_id","group_id","copy_pointer",
            "copy_path","recycle_path","current_operation_id","current_pointer","pending"}
        if set(link)!=required:
            raise ValueError("Unsupported copied-family lifecycle fields; preserve its exact evidence.")
        if not link["copy_operation_id"] or not link["member_id"] or not link["current_operation_id"]:
            raise ValueError("Copied-family lifecycle identity is incomplete.")
        return link

    def _current(self,record):
        link=self._link(record)
        if record.status=="purged":raise ValueError("This retained copy was explicitly purged; restore coverage has expired.")
        if record.status not in {"active","restored"} or link["pending"]:
            raise ValueError("Complete exact copied-family recovery before another action.")
        root=self.recycle.validate_root()
        if not path_key(link["recycle_path"]).is_relative_to(path_key(root)):
            raise ValueError("The exact configured Recycle location changed.")
        current=self._history()._record(link["current_operation_id"])
        native=self.moves.history_coordinator._load(link["current_pointer"])
        intent=native["intent"]
        expected=current.get("recycle_link")
        if (native["status"]!="complete" or current.get("history_move")!=link["current_pointer"]
                or intent.get("copy_recycle")!=expected or not expected
                or expected.get("record_id")!=record.id or expected.get("member_id")!=link["member_id"]
                or expected.get("operation_id")!=link["current_operation_id"]
                or expected.get("copy_operation_id")!=link["copy_operation_id"]
                or intent["entity_id"]!=link["member_id"]):
            raise ValueError("Recycle, operation and native copied-image identities disagree.")
        destination=link["recycle_path"] if record.status=="active" else link["copy_path"]
        if path_key(intent["target"])!=path_key(destination) or path_key(record.current_path)!=path_key(destination):
            raise ValueError("Copied-family current placement changed.")
        self._verify_placement(intent)
        return link,current,intent

    def validate_active(self,record):
        self._current(record)

    def admit_restored_route(self,previous,plan,hashes,*,should_cancel=None):
        """Route a restored family; later saved data follows its native identity."""
        reference=previous.recycle_link or {}
        record=self.recycle.store.load_strict(reference.get("record_id",""));link=self._link(record)
        root=self.recycle.validate_root()
        selected=self._history()._record(previous.operation_id)
        if (record.status!="restored" or link["pending"] or reference.get("direction")!="restore"
                or link["current_operation_id"]!=previous.operation_id or link["current_pointer"]!=previous.history_move
                or selected!=self.moves._record_value(previous)
                or path_key(record.current_path)!=path_key(plan.image) or path_key(link["copy_path"])!=path_key(plan.image)
                or path_key(plan.image).is_relative_to(path_key(root))
                or not path_key(link["recycle_path"]).is_relative_to(path_key(root))):
            raise ValueError("The linked copied family is not proven fully restored outside Recycle.")
        receipt=self.moves.history_coordinator._load(previous.history_move);intent=receipt["intent"]
        if (receipt["status"]!="complete" or intent.get("copy_recycle")!=reference
                or intent["entity_id"]!=link["member_id"] or reference.get("record_id")!=record.id
                or reference.get("operation_id")!=previous.operation_id or reference.get("copy_operation_id")!=link["copy_operation_id"]):
            raise ValueError("The restored copy's exact Recycle and native identities disagree.")
        return self.moves.history_coordinator.admit_restored_copy_route(plan,hashes,
            selected_pointer=previous.history_move,should_cancel=should_cancel)

    def preview_restore(self,record,*,member_ids=None,cancelled=lambda:False):
        operation_id=record.get("operation_id","")
        try:
            if cancelled():raise InterruptedError("Copied-family preview cancelled.")
            history=self._history();coordinator=self.moves.history_coordinator
            if coordinator is None:raise ValueError("Configure native history before reversible copy removal.")
            if record.get("operation_kind")=="copy":
                if not record.get("history_move"):
                    raise ValueError("This copy has no verified still-image identity. Legacy and video copies require their qualified recovery route.")
                receipt=coordinator._load(record["history_move"]);original=receipt["intent"]
                if record["files"]!=[{"from":item["source"],"to":item["target"],"sha256":item["sha256"]} for item in original["transfer"]["files"]]:
                    raise ValueError("Copy operation and native receipt disagree.")
                image=Path(original["target"])
                if image.suffix.casefold()==".mp4":raise ValueError("Video copies require a qualified temporal lifecycle provider.")
                plan=self.recycle.preview_recycle(image,should_cancel=cancelled)
                retained=plan.record;transfer=plan.transfer
                decision,boundary_json,source_profile,target_profile=history._boundary(image,transfer.members[0].target)
                # Rebuild exact transfer with the real storage roles, including
                # unclassified sources; folder location is not privacy proof.
                from .library_profiles import LibraryPrivacyPolicy
                transfer=self.moves._engine.plan_exact([(m.source,m.target) for m in transfer.members],image=image,
                    force_copy=LibraryPrivacyPolicy.requires_verified_copy(source_profile,target_profile))
                hashes=self.moves._engine.prepare_hashes(transfer,cancelled)
                intent=coordinator.admit_copy_recycle(transfer,hashes,selected_pointer=record["history_move"],should_cancel=cancelled)
                member_id=intent["entity_id"]
                retained._extra["copy_lifecycle"]={"schema_version":1,"copy_operation_id":operation_id,
                    "member_id":member_id,"group_id":record.get("group_id",""),"copy_pointer":record["history_move"],
                    "copy_path":str(image),"recycle_path":str(transfer.members[0].target),
                    "current_operation_id":operation_id,"current_pointer":record["history_move"],"pending":None}
                retained.source_profile_id=source_profile.stable_id if source_profile.recognized_role else ""
                family=tuple(OrganizationFileChange(m.source,m.target,hashes[str(m.source)],hashes[str(m.source)],
                    intent["source_tokens"][str(path_key(m.source))],m.size) for m in transfer.members)
                preview=OrganizationRestorePreview(operation_id,member_id,record.get("group_id",""),family,
                    source_profile.role,target_profile.role,decision,provider="copy_recycle",record_digest=_digest(record),
                    transfer=transfer,native_intent_json=_canonical(intent),boundary_json=boundary_json)
                state={"initial":True,"record":retained.to_dict(),"record_digest":"","direction":"remove"}
            else:
                reference=record.get("recycle_link") or {}
                retained=self.recycle.store.load_strict(reference.get("record_id",""))
                link,current,_=self._current(retained)
                if current!=record:raise ValueError("Choose the exact current copied-family operation; this one was superseded.")
                preview=history._preview_restore(operation_id,member_ids,cancelled=cancelled,linked=True)
                if not preview.ready:raise ValueError("; ".join(preview.held))
                intent=json.loads(preview.native_intent_json)
                state={"initial":False,"record":retained.to_dict(),"record_digest":_digest(retained.to_dict()),
                    "direction":"restore" if retained.status=="active" else "remove"}
            if member_ids is not None and tuple(member_ids)!=(preview.member_id,):
                raise ValueError("Choose the exact complete copied image family; companions cannot be split.")
            link=self._link(retained)
            intent["copy_recycle"]={"schema_version":1,"record_id":retained.id,"copy_operation_id":link["copy_operation_id"],
                "member_id":preview.member_id,"group_id":preview.group_id,"selected_operation_id":operation_id,
                "direction":state["direction"]}
            preview=replace(preview,provider="copy_recycle",native_intent_json=_canonical(intent),provider_state_json=_canonical(state))
            return replace(preview,fingerprint=history._preview_fingerprint(preview))
        except Exception as exc:
            return OrganizationRestorePreview(operation_id,provider="copy_recycle",held=(str(exc),))

    def apply_restore(self,preview,*,boundary_confirmed=False,cancelled=lambda:False):
        inverse_id="";record_id=""
        try:
            history=self._history()
            if cancelled():raise InterruptedError("Cancelled before copied-family removal or restore.")
            if not preview.ready or preview.fingerprint!=history._preview_fingerprint(preview):
                raise ValueError("The exact copied-family preview is held or changed.")
            with self.moves._lock:
                selected=history._record(preview.operation_id)
                if _digest(selected)!=preview.record_digest:raise ValueError("The selected copy history changed after preview.")
                state=json.loads(preview.provider_state_json)
                record=RecycleRecord.from_dict(state["record"]);record_id=record.id
                link=self._link(record);transfer=preview.transfer
                root=self.recycle.validate_root()
                if not path_key(link["recycle_path"]).is_relative_to(path_key(root)):
                    raise ValueError("The configured Recycle location changed after preview.")
                if state["initial"]:
                    if self.recycle.store.path_for(record.id).exists():raise ValueError("Recycle identity is already occupied.")
                else:
                    current=self.recycle.store.load_strict(record.id)
                    if _digest(current.to_dict())!=state["record_digest"]:raise ValueError("Recycle state changed after preview.")
                    self._current(current)
                decision,boundary_json,source_profile,target_profile=history._boundary(transfer.image,transfer.members[0].target)
                if boundary_json!=preview.boundary_json:raise ValueError("Storage roles or privacy authority changed after preview.")
                if decision.requires_explicit_confirmation and not boundary_confirmed:raise PermissionError("Explicitly confirm the exact copied-family storage boundary.")
                if tuple(self.moves._engine.related_files(transfer.image))!=tuple(m.source for m in transfer.members):
                    raise ValueError("Copied-family membership changed after preview.")
                intent=json.loads(preview.native_intent_json)
                if intent["copy_recycle"]["selected_operation_id"]!=preview.operation_id or intent["entity_id"]!=preview.member_id:
                    raise ValueError("Selected copied-family native identity changed.")
                def publish(prepared):
                    nonlocal inverse_id
                    inverse_id=prepared.operation_id
                    link["pending"]={"operation_id":prepared.operation_id,"pointer":prepared.history_move,
                        "direction":state["direction"],"previous_status":record.status,"files":prepared.files}
                    record.status="moving" if state["direction"]=="remove" else "restoring"
                    record.recovery="Exact copied-family transfer is prepared."
                    self.recycle.store.save(record)
                moved=self.moves.move(transfer.image,transfer.destination,preview=transfer,collision_policy="error",
                    reason="Restore selected copy placement: "+selected["reason"],should_cancel=cancelled,
                    boundary_decision=decision,boundary_confirmed=boundary_confirmed,
                    source_profile_ids=(source_profile.stable_id,),destination_profile_id=target_profile.stable_id,
                    group_id=preview.group_id,restores_operation_id=preview.operation_id,
                    admitted_history=intent,prepared_operation=publish)
                self._complete(record,moved)
            return (OrganizationRestoreOutcome(preview.operation_id,preview.member_id,"complete",
                "The complete copied family reached its reviewed location.",moved.operation_id,
                Path(moved.files[0]["to"]),record.id,"copy_recycle"),)
        except InterruptedError as exc:status="cancelled";message=str(exc)
        except TransferRecoveryRequired as exc:
            status="recovery_required";message=str(exc);inverse_id=getattr(exc,"operation_id",inverse_id)
        except Exception as exc:status="recovery_required" if inverse_id else "held";message=str(exc)
        return (OrganizationRestoreOutcome(preview.operation_id,preview.member_id,status,message,inverse_id,
            recycle_record_id=record_id,provider="copy_recycle"),)

    def _complete(self,record,moved):
        link=self._link(record);pending=link["pending"]
        if not pending or pending["operation_id"]!=moved.operation_id or pending["pointer"]!=moved.history_move or pending["files"]!=moved.files:
            raise ValueError("The exact prepared Recycle operation differs from completion.")
        native=self.moves.history_coordinator._load(moved.history_move)
        if native["status"]!="complete" or native["intent"].get("copy_recycle")!=moved.recycle_link:
            raise ValueError("The exact copied-family native completion is unavailable.")
        self._verify_placement(native["intent"])
        direction=pending["direction"]
        record.status="active" if direction=="remove" else "restored"
        record.current_path=moved.files[0]["to"]
        if direction=="remove":
            record.move_operation_id=moved.operation_id
            record.files=tuple(RecycleFile(item["from"],item["to"],item["sha256"]) for item in moved.files)
        else:record.routed_operation_id=moved.operation_id
        link["current_operation_id"]=moved.operation_id;link["current_pointer"]=moved.history_move;link["pending"]=None
        record.recovery="";self.recycle.store.save(record)

    def restore_record(self,record_id,*,boundary_confirmed=False,should_cancel=None):
        record=self.recycle.store.load_strict(record_id);link,selected,_=self._current(record)
        if record.status!="active":raise ValueError("This retained family is not currently in Recycle.")
        preview=self.preview_restore(selected,member_ids=(link["member_id"],),cancelled=should_cancel or (lambda:False))
        result=self.apply_restore(preview,boundary_confirmed=boundary_confirmed,cancelled=should_cancel or (lambda:False))[0]
        if result.status=="recovery_required":raise TransferRecoveryRequired(result.message)
        if result.status!="complete":raise ValueError(result.message)
        return self.recycle.store.load_strict(record_id)

    def recover_pending(self):
        """Run after canonical transfer and purge recovery; never infer by path."""
        for item in self.recycle.store.records():
            if "copy_lifecycle" not in item._extra:continue
            try:
                record=self.recycle.store.load_strict(item.id);link=self._link(record);pending=link["pending"]
                if not pending or record.status=="purged":continue
                native=self.moves.history_coordinator._load(pending["pointer"])
                intent=native["intent"];reference=intent.get("copy_recycle") or {}
                if (reference.get("record_id")!=record.id or reference.get("operation_id")!=pending["operation_id"]
                        or reference.get("member_id")!=link["member_id"] or reference.get("copy_operation_id")!=link["copy_operation_id"]):
                    raise ValueError("Pending copied-family Recycle linkage disagrees with its native operation.")
                if native["status"]=="complete":
                    moved=self._history()._record(pending["operation_id"])
                    from .edit_service import MoveRecord
                    self._complete(record,MoveRecord(**moved))
                elif native["status"]=="rolled_back":
                    payload={**intent["transfer"],"history_move":pending["pointer"]}
                    self.moves.history_coordinator.recover(pending["pointer"],payload,self.moves._engine,pending["operation_id"])
                    record.status="cancelled" if pending["previous_status"]=="planned" else pending["previous_status"]
                    link["pending"]=None;record.recovery="Exact copied-family transfer rolled back."
                    self.recycle.store.save(record)
                else:raise ValueError("Canonical copied-family transfer recovery is still pending.")
            except Exception as exc:self.recycle.recovery_issues.append(f"{item.id}: {exc}")
