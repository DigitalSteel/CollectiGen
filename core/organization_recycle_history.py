"""Exact ordinary Recycle placement, publication and restart authority."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re

from .dataset_scope import IMG_EXTENSIONS
from .history_configuration import path_key
from .history_recycle_support import NativeRecycleSupport
from .history_moves import MAX_RECEIPT_BYTES
from .history_versions import HistoryStore, _reject_reparse
from .library_profiles import LibraryPrivacyPolicy
from .organization_history import (OrganizationHistoryService, OrganizationRestorePreview,
    OrganizationRestoreOutcome, OrganizationFileChange, _canonical, _digest)
from .recycle_bin import RecycleRecord, RecycleFile
from .verified_transfer import TransferRecoveryRequired


class OrganizationRecycleHistoryProvider(NativeRecycleSupport):
    _intent_key = "recycle_lifecycle"
    _purge_prefix = "recycle-purge-"

    def __init__(self,recycle):
        self.recycle=recycle;self.moves=recycle.moves

    def _history(self):return OrganizationHistoryService(self.moves,recycle_provider=self)

    def _source_record(self,image):
        """Resolve an enrolled identity's receipt, never a newest path match."""
        coordinator=self.moves.history_coordinator;descriptor=coordinator.configuration.resolve(path=image)
        store=HistoryStore(descriptor.scope());entity=store.lookup(image)
        if entity is None:raise ValueError("The source has no enrolled native image identity.")
        with store.locked(),store._db() as db:
            rows=db.execute("SELECT seq,offset,length,digest FROM events WHERE entity=? AND kind='binding' ORDER BY seq DESC LIMIT 101",(entity.entity_id,)).fetchall()
            events=[store._event_at(row) for row in rows]
        # Initial enrolled images have no move log yet. Native binding events
        # decide whether a concrete placement receipt requires that log.
        if not events:return None
        records=None;pointer=None
        for event in events:
            identifier=str(event.get("receipt_id", ""))
            if identifier.startswith("projection-"):continue
            match=re.fullmatch(r"(?:copyrollback|recyclerollback)-([0-9a-f]{32})-([0-9a-f]{64})",identifier)
            receipt_id=match[1] if match else identifier
            if not receipt_id:return None
            if not re.fullmatch(r"[0-9a-f]{32}",receipt_id):raise ValueError("The source placement receipt is unsupported.")
            if match:
                # A failed move from this configured source owns its rollback
                # receipt in this store. Completed cross-store moves instead
                # retain their exact owner pointer in the operation below.
                path=store.scope.history_root/"moves"/(receipt_id+".json");_reject_reparse(path)
                with path.open("rb") as handle:raw=handle.read(MAX_RECEIPT_BYTES+1)
                if len(raw)>MAX_RECEIPT_BYTES:raise ValueError("The source placement receipt exceeds its bound.")
                value=json.loads(raw);intent=value["intent"];pointer=coordinator.pointer(intent)
                coordinator._read_receipt(pointer,path)
                if intent["receipt_id"]!=receipt_id or intent["entity_id"]!=entity.entity_id or pointer["digest"]!=match[2] or value["status"]!="rolled_back":
                    raise ValueError("The source rollback receipt changed.")
                selected=intent.get("organization_route") or intent.get("organization_restore") or intent.get("copy_recycle_initial")
                if not selected:return None
                pointer=selected["pointer"]
            else:
                records=self._history()._records()[0]
                matches=[item for item in records if (item.get("history_move") or {}).get("receipt_id")==receipt_id]
                if len(matches)!=1:raise ValueError("The exact source placement operation is missing or ambiguous.")
                pointer=matches[0]["history_move"]
            break
        if pointer is None:raise ValueError("The source exceeds bounded native placement coverage.")
        native=coordinator._load(pointer)
        if native["status"]!="complete" or native["intent"]["entity_id"]!=entity.entity_id or path_key(native["intent"]["target"])!=path_key(image):
            raise ValueError("The source's exact native placement disagrees with its current identity.")
        if records is None:records=self._history()._records()[0]
        matches=[item for item in records if item.get("history_move")==pointer]
        if len(matches)!=1:raise ValueError("The source's exact native placement operation is missing or ambiguous.")
        return self._history()._record(matches[0]["operation_id"])

    @staticmethod
    def _link(record):
        link=record._extra.get("recycle_lifecycle")
        keys={"schema_version","member_id","group_id","origin_path","recycle_path","outside_path",
            "current_operation_id","current_pointer","pending"}
        if (not isinstance(link,dict) or set(link)!=keys or link.get("schema_version")!=1
                or "copy_lifecycle" in record._extra or not isinstance(link.get("member_id"),str)
                or not re.fullmatch(r"[0-9a-f]{32}",link["member_id"])
                or not isinstance(link.get("group_id"),str) or len(link["group_id"])>128
                or any(not isinstance(link.get(k),str) or not Path(link[k]).is_absolute()
                    for k in ("origin_path","recycle_path","outside_path"))
                or path_key(link["origin_path"])!=path_key(record.original_path)):
            raise ValueError("Unsupported ordinary Recycle lifecycle authority.")
        if record.status not in {"planned","cancelled"} and (not link["current_operation_id"] or not link["current_pointer"]) and not link["pending"]:
            raise ValueError("Recycle lifecycle has no exact current operation.")
        pending=link["pending"]
        if pending is not None and (not isinstance(pending,dict) or set(pending)!={"operation_id","pointer","action","previous_status","previous_outside_path","files"}):
            raise ValueError("Unsupported pending Recycle authority.")
        return link

    def _qualified(self,record):
        """Read-only exact legacy join; never infer a receipt from a path."""
        if "recycle_lifecycle" in record._extra:return record
        if "copy_lifecycle" in record._extra or record.status!="active" or record.kind!="image":
            raise ValueError("This legacy Recycle state has no qualified native current operation.")
        selected=self._history()._record(record.move_operation_id)
        coordinator=self.moves.history_coordinator
        if coordinator is None or not selected.get("history_move"):
            raise ValueError("This Recycle record has no verified native image identity.")
        receipt=coordinator._load(selected["history_move"]);intent=receipt["intent"]
        expected=[{"from":f.original,"to":f.current,"sha256":f.sha256} for f in record.files]
        if (selected.get("operation_kind","move")!="move" or selected.get("undone")
                or receipt["status"]!="complete" or not intent["transit"] or selected["files"]!=expected
                or path_key(intent["source"])!=path_key(record.original_path)
                or path_key(intent["target"])!=path_key(record.current_path)):
            raise ValueError("Legacy Recycle record and exact native entry disagree.")
        record=RecycleRecord.from_dict(record.to_dict())
        record._extra["recycle_lifecycle"]={"schema_version":1,"member_id":intent["entity_id"],
            "group_id":selected.get("group_id",""),"origin_path":record.original_path,
            "recycle_path":record.current_path,"outside_path":record.original_path,
            "current_operation_id":selected["operation_id"],"current_pointer":selected["history_move"],"pending":None}
        return record

    def _current(self,record):
        record=self._qualified(record);link=self._link(record)
        if record.status=="purged":raise ValueError("This exact Recycle recovery was explicitly purged; coverage expired.")
        if record.status not in {"active","restored","routed_ingest","routed_library"} or link["pending"]:
            raise ValueError("Complete this exact Recycle recovery before another action.")
        root=path_key(self.recycle.validate_root())
        if not path_key(link["recycle_path"]).is_relative_to(root) or path_key(link["outside_path"]).is_relative_to(root):
            raise ValueError("The configured Recycle boundary or outside placement changed.")
        current=self._history()._record(link["current_operation_id"])
        receipt=self.moves.history_coordinator._load(link["current_pointer"]);intent=receipt["intent"]
        expected=[{"from":f["source"],"to":f["target"],"sha256":f["sha256"]} for f in intent["transfer"]["files"]]
        reference=current.get("recycle_lifecycle")
        legacy=(current.get("operation_kind","move")=="move" and reference is None
            and record.status=="active" and record.move_operation_id==current["operation_id"])
        if (receipt["status"]!="complete" or current.get("undone") or current.get("history_move")!=link["current_pointer"]
                or current["files"]!=expected or intent["entity_id"]!=link["member_id"]
                or (not legacy and (not reference or intent.get("recycle_lifecycle")!=reference
                    or reference.get("record_id")!=record.id or reference.get("operation_id")!=current["operation_id"]))):
            raise ValueError("Recycle record, selected operation and native image identities disagree.")
        target=link["recycle_path"] if record.status=="active" else link["outside_path"]
        if path_key(record.current_path)!=path_key(target) or path_key(intent["target"])!=path_key(target):
            raise ValueError("Recycle current placement changed.")
        if record.status=="active":
            if {path_key(f.current):f.sha256 for f in record.files}!={path_key(f["target"]):f["sha256"] for f in intent["transfer"]["files"]}:
                raise ValueError("The retained manifest differs from the exact current receipt.")
            self._verify_placement(intent)
        else:
            coordinator=self.moves.history_coordinator
            with coordinator._locked({**intent,"bindings":self._current_bindings(intent)}) as stores:
                coordinator._validate_organization_placement(intent,{"entity_id":intent["entity_id"],"source":intent["target"],"target":intent["source"]},stores)
        return link,current,intent

    def validate_active(self,record):
        if record.status!="active":raise ValueError("This family is not currently retained in Recycle.")
        return self._current(record)

    def _purge_record_link(self,record,witness=None):
        if "recycle_lifecycle" in record._extra:return self._link(record)
        # Native receipt validation happens under the shared lock union below;
        # do not recursively acquire the Windows settings writer here.
        if witness is None or record.status not in {"active","purged"} or record.kind!="image":
            raise ValueError("Legacy purge has no exact admitted native witness.")
        selected=self._history()._record(record.move_operation_id)
        if (selected.get("operation_kind","move")!="move" or selected.get("undone")
                or selected.get("history_move")!=witness["pointer"] or selected["operation_id"]!=witness["operation_id"]
                or selected["files"]!=[{"from":f.original,"to":f.current,"sha256":f.sha256} for f in record.files]):
            raise ValueError("Legacy Recycle record changed from its exact purge admission.")
        return {"member_id":witness["member_id"],"current_operation_id":selected["operation_id"],
            "current_pointer":selected["history_move"],"pending":None}

    def _purge_reference_matches(self,native,record,witness):
        if "recycle_lifecycle" in record._extra:return super()._purge_reference_matches(native,record,witness)
        link=self._purge_record_link(record,witness)
        return (not native.get("recycle_lifecycle") and not native.get("copy_recycle")
            and link["current_operation_id"]==witness["operation_id"] and link["current_pointer"]==witness["pointer"]
            and path_key(native["source"])==path_key(record.original_path) and path_key(native["target"])==path_key(record.current_path))

    def _build(self,record,transfer,*,action,selected=None,initial=False,record_digest="",cancelled=lambda:False):
        if cancelled():raise InterruptedError("Recycle comparison cancelled before writing.")
        history=self._history();coordinator=self.moves.history_coordinator
        image=transfer.image
        if coordinator is None or record.kind!="image" or image.suffix.casefold() not in IMG_EXTENSIONS:
            raise ValueError("This media requires its separately qualified identity provider.")
        decision,boundary_json,source_profile,target_profile=history._boundary(image,transfer.members[0].target)
        transfer=self.moves._engine.plan_exact([(m.source,m.target) for m in transfer.members],image=image,
            force_copy=LibraryPrivacyPolicy.requires_verified_copy(source_profile,target_profile))
        hashes=self.moves._engine.prepare_hashes(transfer,cancelled)
        if initial:
            # Preserve the linked source's current lifecycle proof if present.
            previous=self._source_record(image)
            if previous and (previous.get("recycle_link") or previous.get("recycle_lifecycle")):
                from .edit_service import MoveRecord
                provider=self.recycle.organization_history if previous.get("recycle_lifecycle") else self.recycle.copy_history
                intent=provider.admit_restored_route(MoveRecord(**previous),transfer,hashes,should_cancel=cancelled)
            elif previous and previous.get("operation_kind","move")=="move":
                intent=coordinator.admit_recycle_route(transfer,hashes,selected_pointer=previous["history_move"],should_cancel=cancelled)
            else:intent=coordinator.admit(transfer,hashes,should_cancel=cancelled)
            if intent is None or intent.get("new_entity") or not intent.get("transit"):
                raise ValueError("Recycle entry requires the image's configured native identity.")
            record._extra["recycle_lifecycle"]={"schema_version":1,"member_id":intent["entity_id"],"group_id":"",
                "origin_path":record.original_path,"recycle_path":str(transfer.members[0].target),"outside_path":record.original_path,
                "current_operation_id":"","current_pointer":None,"pending":None}
        else:
            intent=coordinator.admit_recycle_route(transfer,hashes,selected_pointer=selected["history_move"],should_cancel=cancelled)
        link=self._link(record);operation_id=selected["operation_id"] if selected else ""
        intent["recycle_lifecycle"]={"schema_version":1,"record_id":record.id,"member_id":link["member_id"],
            "group_id":link["group_id"],"selected_operation_id":operation_id,"action":action}
        coordinator._validate_recycle_link(intent)
        historical={path_key(f["to"]):f["sha256"] for f in (selected or {}).get("files",())}
        family=tuple(OrganizationFileChange(m.source,m.target,hashes[str(m.source)],historical.get(path_key(m.source),hashes[str(m.source)]),
            intent["source_tokens"][str(path_key(m.source))],m.size) for m in transfer.members)
        state={"record":record.to_dict(),"record_digest":record_digest,"initial":initial,"action":action}
        preview=OrganizationRestorePreview(operation_id,link["member_id"],link["group_id"],family,source_profile.role,target_profile.role,decision,
            provider="recycle",record_digest=_digest(selected) if selected else "",transfer=transfer,
            native_intent_json=_canonical(intent),boundary_json=boundary_json,provider_state_json=_canonical(state))
        return replace(preview,fingerprint=history._preview_fingerprint(preview))

    def preview_entry(self,plan,*,group_id="",cancelled=lambda:False):
        try:
            preview=self._build(RecycleRecord.from_dict(plan.record.to_dict()),plan.transfer,action="enter",initial=True,cancelled=cancelled)
            if group_id:
                state=json.loads(preview.provider_state_json);intent=json.loads(preview.native_intent_json)
                state["record"]["recycle_lifecycle"]["group_id"]=group_id;intent["recycle_lifecycle"]["group_id"]=group_id
                preview=replace(preview,group_id=group_id,provider_state_json=_canonical(state),native_intent_json=_canonical(intent))
                preview=replace(preview,fingerprint=self._history()._preview_fingerprint(preview))
            return preview
        except Exception as exc:return OrganizationRestorePreview("",provider="recycle",held=(str(exc),))

    def preview_record(self,record_id,*,action="restore_origin",destination=None,destination_profile=None,cancelled=lambda:False):
        operation_id="";member_id="";group_id=""
        try:
            original=self.recycle.store.load_strict(record_id);record=self._qualified(original)
            link,selected,intent=self._current(record);operation_id=selected["operation_id"]
            member_id=link["member_id"];group_id=link["group_id"]
            image=Path(record.current_path)
            if action=="return_to_recycle":
                if record.status=="active":raise ValueError("This exact family is already in Recycle.")
                target=Path(link["recycle_path"])
            else:
                if record.status!="active":raise ValueError("This exact family is not currently in Recycle.")
                if action=="restore_origin":target=Path(link["origin_path"])
                elif action=="restore_placement":target=Path(link["outside_path"])
                elif action in {"route_ingest","route_library"}:
                    destination=Path(destination) if destination is not None else None
                    if destination is None or not destination.is_absolute():raise ValueError("Choose an exact configured destination.")
                    if action=="route_ingest" and path_key(destination)!=path_key(self.recycle.settings.ingest_directory):
                        raise ValueError("The selected Ingest destination changed.")
                    if action=="route_library":
                        registry=self.recycle.settings.library_registry();current=registry.get(destination_profile.stable_id) if destination_profile else None
                        if current!=destination_profile or current is None or path_key(current.root)!=path_key(destination) or current.recognized_role is None:
                            raise ValueError("The exact named-library destination is unavailable or changed.")
                    target=destination/image.name
                else:raise ValueError("Unsupported Recycle history action.")
            root=path_key(self.recycle.validate_root())
            if action!="return_to_recycle" and path_key(target).is_relative_to(root):raise ValueError("Choose a destination outside Recycle.")
            current=self.moves._engine.related_files(image)
            pairs=[(path,target if path==image else target.with_suffix(path.suffix if path.suffix!=".json" else ".tags.json")) for path in current]
            transfer=self.moves._engine.plan_exact(pairs,image=image)
            return self._build(record,transfer,action=action,selected=selected,record_digest=_digest(original.to_dict()),cancelled=cancelled)
        except Exception as exc:return OrganizationRestorePreview(operation_id,member_id,group_id,provider="recycle",held=(str(exc),))

    def preview_restore(self,move_record,*,member_ids=None,cancelled=lambda:False):
        operation_id=move_record.get("operation_id","");member_id="";group_id=""
        try:
            reference=move_record.get("recycle_lifecycle")
            if not reference:raise ValueError("Choose this legacy entry through its exact Recycle record.")
            record=self.recycle.store.load_strict(reference["record_id"])
            link,selected,_=self._current(record)
            member_id=link["member_id"];group_id=link["group_id"]
            if selected!=move_record or (member_ids is not None and tuple(member_ids)!=(link["member_id"],)):
                raise ValueError("Choose the exact current Recycle operation and complete family.")
            action="restore_placement" if record.status=="active" else "return_to_recycle"
            return self.preview_record(record.id,action=action,cancelled=cancelled)
        except Exception as exc:return OrganizationRestorePreview(operation_id,member_id,group_id,provider="recycle",held=(str(exc),))

    def apply_restore(self,preview,*,boundary_confirmed=False,cancelled=lambda:False):
        inverse_id="";record_id=""
        try:
            history=self._history()
            if cancelled():raise InterruptedError("Cancelled before the exact Recycle transfer.")
            if preview.provider!="recycle" or not preview.ready or preview.fingerprint!=history._preview_fingerprint(preview):
                raise ValueError("The immutable Recycle comparison is held or changed.")
            state=json.loads(preview.provider_state_json);record=RecycleRecord.from_dict(state["record"]);record_id=record.id
            link=self._link(record);transfer=preview.transfer;action=state["action"]
            with self.moves._lock:
                root=path_key(self.recycle.validate_root())
                if not path_key(link["recycle_path"]).is_relative_to(root):raise ValueError("The exact Recycle boundary changed.")
                if state["initial"]:
                    if self.recycle.store.path_for(record.id).exists():raise ValueError("This Recycle identity is occupied.")
                else:
                    current=self.recycle.store.load_strict(record.id)
                    if _digest(current.to_dict())!=state["record_digest"]:raise ValueError("Recycle state changed after comparison.")
                    _,selected,_=self._current(current)
                    if _digest(selected)!=preview.record_digest:raise ValueError("The exact operation changed after comparison.")
                decision,boundary_json,source_profile,target_profile=history._boundary(transfer.image,transfer.members[0].target)
                if boundary_json!=preview.boundary_json:raise ValueError("Storage roles or privacy authority changed.")
                if decision.requires_explicit_confirmation and not boundary_confirmed:raise PermissionError("Confirm the exact displayed Recycle privacy boundary.")
                if tuple(self.moves._engine.related_files(transfer.image))!=tuple(m.source for m in transfer.members):
                    raise ValueError("The exact Recycle family changed after comparison.")
                intent=json.loads(preview.native_intent_json);reference=intent["recycle_lifecycle"]
                if reference["record_id"]!=record.id or reference["member_id"]!=link["member_id"] or reference["selected_operation_id"]!=preview.operation_id or reference["action"]!=action:
                    raise ValueError("The compared Recycle/native identities disagree.")
                def publish(prepared):
                    nonlocal inverse_id
                    inverse_id=prepared.operation_id
                    link["pending"]={"operation_id":prepared.operation_id,"pointer":prepared.history_move,"action":action,
                        "previous_status":record.status,"previous_outside_path":link["outside_path"],"files":prepared.files}
                    record.status="moving" if action in {"enter","return_to_recycle"} else "restoring"
                    record.recovery="Exact Recycle transfer is prepared."
                    self.recycle.store.save(record)
                inverse=preview.operation_id if action in {"restore_origin","restore_placement","return_to_recycle"} else ""
                moved=self.moves.move(transfer.image,transfer.destination,preview=transfer,collision_policy="error",reason="Recycle: "+action,
                    should_cancel=cancelled,boundary_decision=decision,boundary_confirmed=boundary_confirmed,
                    source_profile_ids=(source_profile.stable_id,),destination_profile_id=target_profile.stable_id,
                    group_id=preview.group_id,restores_operation_id=inverse,admitted_history=intent,prepared_operation=publish)
                self._complete(record,moved)
            return (OrganizationRestoreOutcome(preview.operation_id,preview.member_id,"complete","The exact family reached its reviewed placement.",
                moved.operation_id,Path(record.current_path),record.id,"recycle",transfer.image),)
        except InterruptedError as exc:status="cancelled";message=str(exc)
        except TransferRecoveryRequired as exc:status="recovery_required";message=str(exc);inverse_id=getattr(exc,"operation_id",inverse_id)
        except Exception as exc:status="recovery_required" if inverse_id else "held";message=str(exc)
        return (OrganizationRestoreOutcome(getattr(preview,"operation_id",""),getattr(preview,"member_id",""),status,message,inverse_id,
            recycle_record_id=record_id,provider="recycle"),)

    def _complete(self,record,moved):
        link=self._link(record);pending=link["pending"]
        if not pending or (pending["operation_id"],pending["pointer"],pending["files"])!=(moved.operation_id,moved.history_move,moved.files):
            raise ValueError("Recycle completion differs from its exact pending transfer.")
        native=self.moves.history_coordinator._load(moved.history_move)
        if native["status"]!="complete" or native["intent"].get("recycle_lifecycle")!=moved.recycle_lifecycle:
            raise ValueError("The exact native Recycle completion is unavailable.")
        self._verify_placement(native["intent"])
        action=pending["action"];record.current_path=moved.files[0]["to"]
        if action in {"enter","return_to_recycle"}:
            record.status="active";record.move_operation_id=moved.operation_id
            origin=Path(link["origin_path"]);image=Path(record.current_path)
            record.files=tuple(RecycleFile(str(origin if Path(f["to"])==image else origin.with_suffix(".tags.json" if Path(f["to"]).suffix==".json" else Path(f["to"]).suffix)),f["to"],f["sha256"]) for f in moved.files)
        else:
            record.status="routed_ingest" if action=="route_ingest" else "routed_library" if action=="route_library" else "restored"
            record.routed_operation_id=moved.operation_id;link["outside_path"]=record.current_path
        link["current_operation_id"]=moved.operation_id;link["current_pointer"]=moved.history_move;link["pending"]=None
        record.recovery="";self.recycle.store.save(record)

    def admit_restored_route(self,previous,plan,hashes,*,should_cancel=None):
        reference=previous.recycle_lifecycle or {};record=self.recycle.store.load_strict(reference.get("record_id",""))
        _,selected,_=self._current(record)
        if record.status=="active" or selected!=self.moves._record_value(previous):
            raise ValueError("Route this retained family through its exact Recycle lifecycle.")
        return self.moves.history_coordinator.admit_recycle_route(plan,hashes,selected_pointer=previous.history_move,should_cancel=should_cancel)

    def recover_pending(self):
        for item in self.recycle.store.records():
            if "recycle_lifecycle" not in item._extra:continue
            try:
                record=self.recycle.store.load_strict(item.id);link=self._link(record);pending=link["pending"]
                if not pending or record.status=="purged":continue
                native=self.moves.history_coordinator._load(pending["pointer"]);intent=native["intent"]
                reference=intent.get("recycle_lifecycle") or {}
                if reference.get("record_id")!=record.id or reference.get("operation_id")!=pending["operation_id"] or reference.get("member_id")!=link["member_id"] or reference.get("action")!=pending["action"]:
                    raise ValueError("Pending Recycle/native identities disagree.")
                if native["status"]=="complete":
                    from .edit_service import MoveRecord
                    self._complete(record,MoveRecord(**self._history()._record(pending["operation_id"])))
                elif native["status"]=="rolled_back":
                    self.moves.history_coordinator.recover(pending["pointer"],{**intent["transfer"],"history_move":pending["pointer"]},self.moves._engine,pending["operation_id"])
                    record.status="cancelled" if pending["previous_status"]=="planned" else pending["previous_status"]
                    link["outside_path"]=pending["previous_outside_path"];link["pending"]=None
                    record.recovery="Exact Recycle transfer rolled back.";self.recycle.store.save(record)
                else:raise ValueError("Canonical Recycle transfer recovery remains pending.")
            except Exception as exc:self.recycle.recovery_issues.append(f"{item.id}: {exc}")
