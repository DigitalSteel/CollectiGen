"""Receipt-qualified still-image history continuity for managed family moves.

Only a verified transfer creates a binding transition. No path search, content
deduplication, legacy journal inference, or implicit history enrollment is used.
"""
from __future__ import annotations

import base64
from contextlib import ExitStack,contextmanager
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

from .history_configuration import HistoryBindingResult,HistoryConfigurationError,path_key
from .history_versions import HistoryStore,HistoryEntityRef,HistoryError,MAX_RECORD_BYTES,MAX_SNAPSHOT_BYTES,_source_token,_reject_reparse,history_lock
from .image_record import _atomic_write_text
from .operation_journal import _canonical_bytes
from .verified_transfer import TransferRecoveryOutcome,TransferRecoveryRequired,VerifiedTransferEngine

MAX_MOVE_VERSIONS=10000
MAX_MOVE_BYTES=64*1024*1024
MAX_RECEIPT_BYTES=16*1024*1024


def _digest(value):return hashlib.sha256(_canonical_bytes(value)).hexdigest()


class HistoryMoveCoordinator:
    """One owner for dual-store admission, transfer, and exact restart recovery."""
    def __init__(self,configuration):
        self.configuration=configuration
        self._sha256=VerifiedTransferEngine()._sha256

    def _transit(self,path):
        path=path_key(path)
        for key in ("problems_directory","recycle_directory"):
            value=getattr(self.configuration.settings,key,"")
            if value and path.is_relative_to(path_key(value)):
                _reject_reparse(path)
                return True
        return False

    @staticmethod
    def _descriptor(raw):return HistoryBindingResult(**raw)

    def _store(self,raw):
        descriptor=self._descriptor(raw)
        return HistoryStore(descriptor.scope()) if descriptor.status=="configured" else None

    @contextmanager
    def _locked(self,intent,*,initial_copy=False):
        guarded=[*intent["bindings"],*(intent.get("copy_guard_bindings",()) if initial_copy else ())]
        bindings=[(self._descriptor(item["descriptor"]),Path(item["path"])) for item in guarded]
        with self.configuration.transfer_guard(bindings),ExitStack() as stack:
            stores={}
            for item in guarded:
                store=self._store(item["descriptor"])
                if store is not None:stores[store.scope.key]=store
            for key in sorted(stores):
                stack.enter_context(stores[key].locked())
                stores[key]._ready()
            # Store locks precede file locks, matching canonical saved writes.
            for path in sorted({path_key(intent["source"]),path_key(intent["target"]),
                    *(path_key(path) for path in (intent.get("copy_source_locks",()) if initial_copy else ()))},key=str):
                _reject_reparse(path)
                stack.enter_context(history_lock(path.with_suffix(".tags.json.history.lock")))
            yield stores

    def _receipt_path(self,pointer):
        if not re.fullmatch(r"[0-9a-f]{32}",str(pointer.get("receipt_id",""))):
            raise HistoryError("Invalid move receipt identity.")
        descriptor=self.configuration.resolve(path=pointer["owner_path"])
        if descriptor.status!="configured":raise HistoryConfigurationError(descriptor.reason)
        store=HistoryStore(descriptor.scope())
        path=store.scope.history_root/"moves"/(pointer["receipt_id"]+".json")
        _reject_reparse(path)
        return descriptor,store,path

    def _load(self,pointer):
        descriptor,store,path=self._receipt_path(pointer)
        with self.configuration.write_guard(descriptor,path=pointer["owner_path"]),store.locked():
            return self._read_receipt(pointer,path)

    @staticmethod
    def _read_receipt(pointer,path):
        _reject_reparse(path)
        with path.open("rb") as handle:raw=handle.read(MAX_RECEIPT_BYTES+1)
        if len(raw)>MAX_RECEIPT_BYTES:raise HistoryError("Move receipt exceeds its bounded size.")
        value=json.loads(raw);intent=value.get("intent",{})
        if value.get("status") not in {"prepared","complete","rolled_back"}:
            raise HistoryError("Unsupported native move receipt state; recovery is held.")
        schema=3 if intent.get("recycle_lifecycle") else 2 if intent.get("copy_recycle") else 1
        if value.get("schema_version")!=schema or _digest(intent)!=pointer.get("digest") or intent.get("receipt_id")!=pointer["receipt_id"]:
            raise HistoryError("Move receipt integrity or authority changed.")
        if schema==2:
            HistoryMoveCoordinator._validate_copy_link(intent,prepared=True)
        if schema==3:HistoryMoveCoordinator._validate_recycle_link(intent,prepared=True)
        if intent.get("owner_path")!=pointer["owner_path"]:
            raise HistoryError("Move receipt belongs to another source authority.")
        return value

    @staticmethod
    def _validate_recycle_link(intent,*,prepared=False):
        link=intent.get("recycle_lifecycle")
        keys={"schema_version","record_id","member_id","group_id","selected_operation_id","action"}
        if isinstance(link,dict) and "operation_id" in link:keys.add("operation_id")
        actions={"enter","restore_origin","route_ingest","route_library","return_to_recycle","restore_placement"}
        if (not isinstance(link,dict) or set(link)!=keys or link.get("schema_version")!=1
                or intent.get("copy_recycle") or intent["transfer"].get("preserve_source")
                or link.get("member_id")!=intent.get("entity_id") or link.get("action") not in actions
                or not re.fullmatch(r"recycle_[0-9a-f]{32}",str(link.get("record_id","")))
                or not isinstance(link.get("group_id"),str) or len(link["group_id"])>128
                or (link.get("action")!="enter" and not re.fullmatch(r"op_[0-9a-f]{32}",str(link.get("selected_operation_id",""))))
                or (link.get("action")=="enter" and link.get("selected_operation_id")!="")
                or (prepared and "operation_id" not in link)
                or ("operation_id" in link and not re.fullmatch(r"op_[0-9a-f]{32}",str(link["operation_id"])))):
            raise HistoryError("Unsupported ordinary Recycle lifecycle; execution and recovery are held.")

    @staticmethod
    def _validate_copy_link(intent,*,prepared=False):
        link=intent.get("copy_recycle")
        keys={"schema_version","record_id","copy_operation_id","member_id","group_id","selected_operation_id","direction"}
        if isinstance(link,dict) and "operation_id" in link:keys.add("operation_id")
        if (not isinstance(link,dict) or link.get("schema_version")!=1 or set(link)!=keys
                or (prepared and "operation_id" not in link) or link.get("direction") not in {"remove","restore"}
                or link.get("member_id")!=intent.get("entity_id") or not re.fullmatch("recycle_[0-9a-f]{32}",str(link.get("record_id","")))
                or not isinstance(link.get("group_id"),str) or len(link["group_id"])>128
                or any(not re.fullmatch("op_[0-9a-f]{32}",str(link.get(key,""))) for key in
                    ({"copy_operation_id","selected_operation_id","operation_id"} if "operation_id" in link else {"copy_operation_id","selected_operation_id"}))):
            raise HistoryError("Unsupported copied-family lifecycle; execution and recovery are held.")

    def _organization_original(self,intent,stores,*,key="organization_restore"):
        selected=intent[key]
        store=stores.get(selected["owner_scope"])
        if store is None:raise HistoryError("Selected organization history is outside the admitted stores.")
        pointer=selected["pointer"]
        if not re.fullmatch("[0-9a-f]{32}",str(pointer.get("receipt_id",""))):
            raise HistoryError("Invalid selected organization receipt identity.")
        path=store.scope.history_root/"moves"/(pointer["receipt_id"]+".json")
        value=self._read_receipt(pointer,path)
        if value["status"]!="complete":raise HistoryError("The selected relocation did not complete.")
        return value["intent"]

    def _validate_organization_placement(self,original,intent,stores,*,inverse=True):
        if (original["transfer"].get("preserve_source") or original.get("copy_undo")
                or original["entity_id"]!=intent["entity_id"]
                or path_key(original["target"])!=path_key(intent["source"])
                or (inverse and path_key(original["source"])!=path_key(intent["target"]))):
            raise HistoryError("The selected operation does not authorize this image identity or placement inverse.")
        store=stores[original["target_scope"] or original["source_scope"]]
        binding=store.binding(entity_id=original["entity_id"])
        locator=original["target_locator"] or original["source_locator"]
        state="transit" if original["transit"] else "active"
        if (not self._binding_matches(binding,original["entity_id"],state)
                or binding.get("locator")!=locator
                or binding.get("source_token")!=_source_token(Path(original["target"]))):
            raise HistoryError("Selected relocation identity or current placement changed; restore was held.")
        if str(binding.get("receipt_id","")).startswith(("copyrollback-","recyclerollback-")):
            self.linked_rollback(binding["receipt_id"],selected_pointer=self.pointer(original),
                owner_path=original["owner_path"],source=original["target"],entity_id=original["entity_id"],stores=stores)
            return
        # Data-only projection renewals may follow a move at the same locator.
        # Any intervening relocation, even out-and-back, is overlapping work.
        with store._db() as db:
            rows=db.execute("SELECT seq,offset,length,digest FROM events WHERE entity=? AND kind='binding' ORDER BY seq DESC LIMIT 101",
                (original["entity_id"],)).fetchall()
        for row in rows:
            event=store._event_at(row)
            if event.get("entity_id")!=original["entity_id"] or event.get("event")!="binding":
                raise HistoryError("Organization placement index disagrees with its native event.")
            if event.get("receipt_id")==original["receipt_id"]:
                if event.get("locator")!=locator or event.get("binding_state")!=state:
                    raise HistoryError("The original organization receipt has no completed current placement.")
                return
            if (state!="active" or event.get("locator")!=locator
                    or event.get("binding_state") not in {"active","moving"}
                    or not re.fullmatch(r"projection-[0-9a-f]{32}-[0-9a-f]{64}",str(event.get("receipt_id","")))):
                raise HistoryError("Later placement work overlaps this relocation; choose its exact current operation.")
            from .history_projection import _load_receipt
            projection,*_= _load_receipt(store,Path(original["target"]),event["receipt_id"])
            if projection["intent"].get("entity_id")!=original["entity_id"]:
                raise HistoryError("Later projection belongs to another image identity.")
        raise HistoryError("The selected placement exceeds bounded continuity coverage.")

    def admit_organization_restore(self,plan,hashes,*,selected_pointer,should_cancel=None):
        """Freeze a chosen relocation without weakening legacy move admission."""
        value=self._load(selected_pointer)
        if value["status"]!="complete":raise HistoryError("The selected relocation needs recovery first.")
        original=value["intent"]
        intent=self.admit(plan,hashes,prior=selected_pointer,should_cancel=should_cancel)
        if intent is None:raise HistoryError("Exact organization restore requires configured native history.")
        intent["organization_restore"]={"pointer":dict(selected_pointer),
            "owner_scope":original["source_scope"] or original["target_scope"]}
        if len(_canonical_bytes(intent))>MAX_RECEIPT_BYTES-1024:raise HistoryError("Organization restore exceeds its receipt bound.")
        with self._locked(intent) as stores:self._validate_before(intent,stores)
        return intent

    def admit_restored_copy_route(self,plan,hashes,*,selected_pointer,should_cancel=None):
        selected=self._load(selected_pointer);original=selected["intent"]
        if selected["status"]!="complete" or original.get("copy_recycle",{}).get("direction")!="restore":
            raise HistoryError("Only a completed linked copy restoration can enter ordinary routing.")
        intent=self.admit(plan,hashes,should_cancel=should_cancel)
        if intent is None:raise HistoryError("Restored copy routing requires its configured native identity.")
        owner=self.configuration.resolve(path=original["owner_path"])
        intent["organization_route"]={"pointer":dict(selected_pointer),"owner_scope":owner.scope().key}
        with self._locked(intent) as stores:self._validate_before(intent,stores)
        return intent

    def admit_recycle_route(self,plan,hashes,*,selected_pointer,should_cancel=None):
        selected=self._load(selected_pointer);original=selected["intent"]
        if selected["status"]!="complete":raise HistoryError("Complete exact Recycle recovery before routing.")
        intent=self.admit(plan,hashes,prior=selected_pointer,should_cancel=should_cancel)
        if intent is None:raise HistoryError("Recycle routing requires its exact native identity.")
        owner=self.configuration.resolve(path=original["owner_path"])
        # A routed family can now live in a different configured store while
        # its selected receipt still belongs to the previous owner. Include
        # that exact authority in the lock/configuration set before reading it.
        if owner.scope().key not in {self._descriptor(value["descriptor"]).scope().key for value in intent["bindings"] if value["descriptor"]["status"]=="configured"}:
            intent["bindings"].append({"path":original["owner_path"],"descriptor":asdict(owner)})
        intent["organization_route"]={"pointer":dict(selected_pointer),"owner_scope":owner.scope().key}
        with self._locked(intent) as stores:self._validate_before(intent,stores)
        return intent

    def _write(self,intent,status):
        pointer=self.pointer(intent)
        _descriptor,_store,path=self._receipt_path(pointer)
        value={"schema_version":3 if intent.get("recycle_lifecycle") else 2 if intent.get("copy_recycle") else 1,"intent":intent,"status":status}
        raw=_canonical_bytes(value)
        if len(raw)>MAX_RECEIPT_BYTES:raise HistoryError("Move receipt exceeds its bounded size.")
        path.parent.mkdir(parents=True,exist_ok=True)
        _atomic_write_text(path,raw.decode("utf-8")+"\n")
        if path.read_bytes()!=raw+b"\n":raise HistoryError("Move receipt failed read-back verification.")

    @staticmethod
    def pointer(intent):
        return {"receipt_id":intent["receipt_id"],"owner_path":intent["owner_path"],"digest":_digest(intent)}

    @staticmethod
    def _locator(store,path):return path_key(path).relative_to(store.scope.library_root).as_posix()

    @staticmethod
    def _binding_matches(event,entity,state=None):
        return bool(event and event.get("entity_id")==entity and (state is None or event.get("binding_state","active")==state))

    def admit(self,plan,hashes,*,prior=None,should_cancel=None):
        """Freeze identity and a bounded version inventory, before transfer IO.

        Returns None for unconfigured/non-image transfers. The caller journals
        pointer(intent) with the exact transfer plan before execute() begins.
        """
        if plan.image.suffix.casefold()==".mp4":return None
        if should_cancel and should_cancel():raise InterruptedError("History move admission cancelled.")
        if not plan.ready or not plan.members:raise HistoryError("Move preview is not ready.")
        source,target=path_key(plan.members[0].source),path_key(plan.members[0].target)
        if source==target:raise HistoryError("A move requires distinct source and destination locators.")
        source_descriptor=self.configuration.resolve(path=source)
        target_descriptor=self.configuration.resolve(path=target)
        for descriptor in (source_descriptor,target_descriptor):
            if descriptor.status not in {"configured","not_configured"}:
                raise HistoryConfigurationError(descriptor.reason)
        previous=None;return_receipt=""
        owner_path=source if source_descriptor.status=="configured" else target
        source_anchor=source
        if prior is not None and source_descriptor.status!="configured" and not plan.preserve_source:
            previous=self._load(prior)
            old=previous["intent"]
            if previous["status"]!="complete" or old["target"]!=str(source) or not old.get("transit") or not self._transit(source):
                raise HistoryError("The exact suspended move receipt does not authorize this source.")
            source_anchor=path_key(old["owner_path"])
            source_descriptor=self.configuration.resolve(path=source_anchor)
            owner_path=source_anchor
        if source_descriptor.status!="configured" and target_descriptor.status!="configured":return None
        bindings=[{"path":str(source_anchor),"descriptor":asdict(source_descriptor)},
                  {"path":str(target),"descriptor":asdict(target_descriptor)}]
        intent={"receipt_id":uuid4().hex,"owner_path":str(owner_path),"source":str(source),"target":str(target),
            "bindings":bindings,"source_scope":"","target_scope":"","source_locator":"","target_locator":"",
            "entity_id":"","new_entity":False,"transit":False,"source_token":_source_token(source),
            "source_tokens":{str(path_key(member.source)):_source_token(member.source) for member in plan.members},
            "versions":[],"head":"","prior":prior,"transfer":plan.journal_payload(hashes)}
        with self._locked(intent) as stores:
            source_store=self._store(asdict(source_descriptor)); target_store=self._store(asdict(target_descriptor))
            entity=None
            source_guard=None
            if source_store is not None and previous is None:
                source_entity=source_store.lookup(source)
                if source_entity is not None and source_store.has_pending(source_entity):
                    raise HistoryError("Resolve pending saved-data history before transferring this image.")
                source_guard=(source_store.scope.key,self._locator(source_store,source))
            if not plan.preserve_source and source_store is not None:
                intent["source_scope"]=source_store.scope.key
                if previous is not None:
                    old=previous["intent"]
                    event=source_store.binding(entity_id=old["entity_id"])
                    if (not self._binding_matches(event,old["entity_id"],"transit")
                            or event.get("transit_path")!=str(source) or event.get("source_token")!=_source_token(source)):
                        raise HistoryError("Suspended source identity changed; exact recovery is required.")
                    return_receipt=event.get("receipt_id")
                    if return_receipt!=old["receipt_id"]:
                        if not old.get("copy_recycle") and not str(return_receipt).startswith("recyclerollback-"):raise HistoryError("Suspended source receipt changed.")
                        self.linked_rollback(return_receipt,selected_pointer=prior,owner_path=old["owner_path"],
                            source=str(source),entity_id=old["entity_id"],stores=stores)
                    entity=HistoryEntityRef(source_store.scope.key,event["entity_id"],event["locator"],event["source_token"])
                else:
                    entity=source_store.lookup(source)
                    if entity is not None and source_store.has_pending(entity):raise HistoryError("Resolve pending saved-data history before moving this image.")
            if entity is None:
                if target_store is None:return None
                intent["entity_id"]=uuid4().hex; intent["new_entity"]=True
                intent["source_scope"]=""
            else:
                intent["entity_id"]=entity.entity_id; intent["source_locator"]=entity.locator
                head=source_store._find(kind="version",entity=entity.entity_id)
                intent["head"]=head.get("version_id","") if head else ""
            if target_store is None:
                if not self._transit(target):
                    raise HistoryError("Configure History & Versions for the destination library before moving an image with saved history.")
                intent["transit"]=True
            else:
                intent["target_scope"]=target_store.scope.key
                intent["target_locator"]=self._locator(target_store,target)
                occupant=target_store.binding(locator=intent["target_locator"])
                returning = bool(previous and occupant and occupant.get("entity_id")==intent["entity_id"]
                    and occupant.get("binding_state")=="transit" and occupant.get("receipt_id")==return_receipt)
                if occupant is not None and occupant.get("binding_state")!="retired" and not returning:
                    raise HistoryError("The destination locator already has a history identity; choose another destination.")
            if entity is not None and target_store is not None and source_store.scope.key!=target_store.scope.key:
                intent["versions"]=self._inventory(source_store,entity,live=previous is None,should_cancel=should_cancel)
            guarded={(intent["source_scope"],intent["source_locator"]),(intent["target_scope"],intent["target_locator"])}
            if source_guard:guarded.add(source_guard)
            intent["before_bindings"] = [
                {"scope":scope,"locator":locator,"event":stores[scope].binding(locator=locator)}
                for scope,locator in sorted(guarded) if scope
            ]
            if len(_canonical_bytes(intent))>MAX_RECEIPT_BYTES-1024:raise HistoryError("Move history exceeds its receipt budget.")
        return intent

    def _inventory(self,store,entity,*,live,should_cancel=None):
        with store._db() as db:
            rows=db.execute("SELECT seq,offset,length,digest FROM events WHERE entity=? AND kind IN ('version','imported-version') ORDER BY seq LIMIT ?",(entity.entity_id,MAX_MOVE_VERSIONS+1)).fetchall()
        if len(rows)>MAX_MOVE_VERSIONS:raise HistoryError("Move history exceeds the bounded version budget.")
        result=[]; total=0
        for row in rows:
            if should_cancel and should_cancel():raise InterruptedError("History inventory cancelled before transfer.")
            event=store._event_at(row)
            raw=store.payload(entity,event["snapshot"]) if live else self._archived_payload(store,entity.entity_id,event)
            total+=len(raw or b"")
            if total>MAX_MOVE_BYTES:raise HistoryError("Move history exceeds the 64 MiB transfer budget.")
            result.append({key:value for key,value in event.items() if key not in {"sequence","scope","history_sequence","history_schema","schema_version"}})
        return result

    @staticmethod
    def _archived_payload(store,entity_id,event):
        authoritative=store._find(kind=event["event"],entity=entity_id,version=event["version_id"])
        if authoritative is None or authoritative.get("snapshot")!=event["snapshot"]:
            raise HistoryError("Historical move inventory changed.")
        value=store.journal.load_verified_snapshot(event["snapshot"],max_bytes=MAX_SNAPSHOT_BYTES)
        if value.get("history_schema")!=1 or value.get("scope")!=store.scope.key or value.get("entity_id")!=entity_id:
            raise HistoryError("Move snapshot authority mismatch.")
        if value.get("exists") is False and value.get("raw") is None:return None
        if value.get("exists") is not True or not isinstance(value.get("raw"),str):raise HistoryError("Invalid moved snapshot.")
        raw=base64.b64decode(value["raw"],validate=True)
        if len(raw)>MAX_RECORD_BYTES:raise HistoryError("Moved record exceeds its byte bound.")
        return raw

    def _validate_before(self,intent,stores):
        if "copy_recycle" in intent:self._validate_copy_link(intent)
        if "recycle_lifecycle" in intent:self._validate_recycle_link(intent)
        if intent.get("copy_recycle_initial"):
            self._validate_copy_recycle_source(intent,stores)
        if intent.get("organization_restore"):
            self._validate_organization_placement(self._organization_original(intent,stores),intent,stores)
        if intent.get("organization_route"):
            original=self._organization_original(intent,stores,key="organization_route")
            source_intent={**intent,"entity_id":original["entity_id"]} if intent["transfer"].get("preserve_source") else intent
            self._validate_organization_placement(original,source_intent,stores,inverse=False)
        if _source_token(Path(intent["source"]))!=intent["source_token"]:
            raise HistoryError("The admitted source instance changed before the move.")
        for member in intent["transfer"]["files"]:
            source,target=Path(member["source"]),Path(member["target"])
            _reject_reparse(source); _reject_reparse(target)
            if _source_token(source)!=intent["source_tokens"][str(path_key(source))]:
                raise HistoryError("The admitted source family instance changed before the move.")
            if target.exists() or self._sha256(source)!=member["sha256"]:
                raise HistoryError("The exact admitted move family changed before transfer.")
        if intent["source_scope"]:
            store=stores[intent["source_scope"]]
            event=store.binding(entity_id=intent["entity_id"])
            expected_state="transit" if intent["prior"] is not None and self._transit(intent["source"]) else "active"
            if not self._binding_matches(event,intent["entity_id"],expected_state) or event.get("source_token")!=intent["source_token"]:
                raise HistoryError("The source history binding changed before the move.")
            head=store._find(kind="version",entity=intent["entity_id"])
            if (head.get("version_id","") if head else "")!=intent["head"]:
                raise HistoryError("Saved history changed after move admission.")
            if expected_state=="active":
                entity=HistoryEntityRef(store.scope.key,intent["entity_id"],intent["source_locator"],intent["source_token"])
                if store.has_pending(entity):raise HistoryError("Pending saved-data recovery holds this move.")
        for binding in intent["before_bindings"]:
            if stores[binding["scope"]].binding(locator=binding["locator"])!=binding["event"]:
                raise HistoryError("A history binding changed after move admission.")

    def admit_copy_recycle(self,plan,hashes,*,selected_pointer,should_cancel=None):
        """Admit retention of the copied UUID, with original proof only initially."""
        selected=self._load(selected_pointer)
        original=selected["intent"]
        if selected["status"]!="complete" or not original["transfer"].get("preserve_source"):
            raise HistoryError("Choose the exact completed native copy operation.")
        intent=self.admit(plan,hashes,should_cancel=should_cancel)
        if intent is None or intent["entity_id"]!=original["entity_id"]:
            raise HistoryError("The copied image no longer has its recorded native identity.")
        descriptor=self.configuration.resolve(path=original["owner_path"])
        intent["copy_recycle_initial"]={"pointer":dict(selected_pointer),"owner_scope":descriptor.scope().key}
        # Include original authority and locks only in this initial admission.
        intent["copy_guard_bindings"]=[item for item in original["bindings"] if item not in intent["bindings"]]
        intent["copy_source_locks"]=[original["source"]]
        with self._locked(intent,initial_copy=True) as stores:self._validate_before(intent,stores)
        return intent

    def _validate_copy_recycle_source(self,intent,stores):
        selected=intent["copy_recycle_initial"];pointer=selected["pointer"]
        store=stores[selected["owner_scope"]]
        value=self._read_receipt(pointer,store.scope.history_root/"moves"/(pointer["receipt_id"]+".json"))
        original=value["intent"]
        if (value["status"]!="complete" or not original["transfer"].get("preserve_source")
                or original["entity_id"]!=intent["entity_id"] or original["target"]!=intent["source"]):
            raise HistoryError("The selected copy does not authorize this retained family.")
        binding=stores[original["target_scope"]].binding(entity_id=original["entity_id"])
        if not binding or binding.get("binding_state")!="active":
            raise HistoryError("Later copied-image work superseded the selected copy.")
        rollback=None
        if binding.get("receipt_id")!=original["receipt_id"]:
            rollback=self.linked_rollback(binding.get("receipt_id"),selected_pointer=selected["pointer"],
                owner_path=original["target"],source=original["target"],entity_id=original["entity_id"],stores=stores)
        for member in original["transfer"]["files"]:
            for endpoint in ("source","target"):
                path=Path(member[endpoint]);_reject_reparse(path)
                expected=self._expected_token(rollback,stores,"source",path) if rollback and endpoint=="target" else self._expected_token(original,stores,endpoint,path)
                if _source_token(path)!=expected:raise HistoryError("The original source or copied file instance changed.")
        if not all(self._family_state(original)[:2]):
            raise HistoryError("The original source or imported copy changed; removal is held.")
        for endpoint in ("source","target"):
            if {path_key(path) for path in VerifiedTransferEngine.related_files(Path(original[endpoint]))}!={path_key(member[endpoint]) for member in original["transfer"]["files"]}:
                raise HistoryError("The original source or imported copy family changed.")
        descriptor=self.configuration.resolve(path=original["source"])
        if descriptor.status=="configured":
            source_store=stores[descriptor.scope().key]
            entity=source_store.lookup(Path(original["source"]))
            if entity is not None and source_store.has_pending(entity):
                raise HistoryError("The original source has pending saved-data recovery.")

    def linked_rollback(self,receipt_id,*,selected_pointer,owner_path,source,entity_id,stores):
        intent=self.linked_rollback_receipt(receipt_id,selected_pointer=selected_pointer,owner_path=owner_path,
            source=source,entity_id=entity_id,stores=stores)
        self._verify_instances(intent,stores)
        source_ok,_,_,target_absent=self._family_state(intent)
        if not source_ok or not target_absent:raise HistoryError("The exact copied-family rollback was superseded.")
        return intent

    def linked_rollback_receipt(self,receipt_id,*,selected_pointer,owner_path,source,entity_id,stores):
        """Qualify immutable rollback evidence; does not authorize file mutation."""
        match=re.fullmatch(r"(?:copyrollback|recyclerollback)-([0-9a-f]{32})-([0-9a-f]{64})",str(receipt_id))
        if not match:raise HistoryError("Later copied-image work superseded the selected placement.")
        descriptor=self.configuration.resolve(path=owner_path)
        if descriptor.scope().key not in stores:raise HistoryError("The selected rollback owner is outside admitted history.")
        # A subsequent attempted route can originate in the selected move's
        # destination store. Its exact rollback receipt belongs there, while
        # the selected successful receipt remains in the former source store.
        # Inspect this receipt ID only within the already guarded store union.
        candidates=[]
        for store in stores.values():
            path=store.scope.history_root/"moves"/(match[1]+".json");_reject_reparse(path)
            if not path.exists():continue
            with path.open("rb") as handle:raw=handle.read(MAX_RECEIPT_BYTES+1)
            if len(raw)>MAX_RECEIPT_BYTES:raise HistoryError("Rollback receipt exceeds its bound.")
            owner=json.loads(raw).get("intent",{}).get("owner_path","")
            pointer={"receipt_id":match[1],"digest":match[2],"owner_path":owner}
            value=self._read_receipt(pointer,path)
            if self.configuration.resolve(path=owner).scope().key!=store.scope.key:
                raise HistoryError("Rollback receipt store and exact owner disagree.")
            candidates.append(value)
        if len(candidates)!=1:raise HistoryError("The exact rollback receipt is missing or ambiguous.")
        value=candidates[0];intent=value["intent"]
        selected=intent.get("organization_restore") or intent.get("copy_recycle_initial") or intent.get("organization_route") or {}
        linked=bool(intent.get("copy_recycle") or intent.get("recycle_lifecycle") or (intent.get("organization_route") and not intent["transfer"].get("preserve_source")))
        if (value["status"]!="rolled_back" or not linked or selected.get("pointer")!=selected_pointer
                or intent["entity_id"]!=entity_id or intent["source"]!=source):
            raise HistoryError("The copied-family rollback does not qualify the selected placement.")
        return intent

    def validate_linked(self,intent):
        if not (intent.get("copy_recycle") or intent.get("recycle_lifecycle")):raise HistoryError("Linked admission requires a typed Recycle lifecycle.")
        with self._locked(intent,initial_copy=True) as stores:self._validate_before(intent,stores)

    def prepare_linked(self,intent):
        """Durable no-file-mutation receipt for the linked Recycle publisher."""
        if not (intent.get("copy_recycle") or intent.get("recycle_lifecycle")):raise HistoryError("Linked preparation requires a typed Recycle lifecycle.")
        with self._locked(intent,initial_copy=True) as stores:
            self._validate_before(intent,stores)
            self._write(intent,"prepared")

    def _transition(self,store,intent,locator,state,token,*,transit_path=""):
        current=store.binding(locator=locator)
        if (current is not None and current.get("receipt_id")==intent["receipt_id"] and current.get("binding_state")==state
                and current.get("source_token")==token and current.get("transit_path","")==transit_path):return
        store.transition_binding(intent["entity_id"],locator,token,state=state,receipt_id=intent["receipt_id"],transit_path=transit_path)

    def _reserve(self,intent,stores):
        if intent["source_scope"]:self._transition(stores[intent["source_scope"]],intent,intent["source_locator"],"moving",intent["source_token"])
        if intent["target_scope"]:self._transition(stores[intent["target_scope"]],intent,intent["target_locator"],"moving","")

    def _import_versions(self,intent,stores):
        if not intent["versions"]:return
        source,target=stores[intent["source_scope"]],stores[intent["target_scope"]]
        for event in intent["versions"]:
            raw=self._archived_payload(source,intent["entity_id"],event)
            existing=target._find(kind=event["event"],entity=intent["entity_id"],version=event["version_id"])
            if existing is not None:
                if self._archived_payload(target,intent["entity_id"],existing)!=raw:raise HistoryError("Destination history version collision.")
                ignored={"snapshot","scope","sequence","history_schema","history_sequence","schema_version","transfer_receipt","transfer_origin_scope"}
                if {k:v for k,v in existing.items() if k not in ignored}!={k:v for k,v in event.items() if k not in ignored}:
                    raise HistoryError("Destination history version provenance collision.")
                continue
            envelope={"history_schema":1,"scope":target.scope.key,"entity_id":intent["entity_id"],"exists":raw is not None,
                "raw":base64.b64encode(raw).decode("ascii") if raw is not None else None}
            digest=target.journal.store_verified_snapshot(envelope,max_bytes=MAX_SNAPSHOT_BYTES)
            target._append({**event,"snapshot":digest,"transfer_receipt":intent["receipt_id"],"transfer_origin_scope":source.scope.key})

    def _family_state(self,intent):
        source_ok=True; target_ok=True; source_absent=True; target_absent=True
        for member in intent["transfer"]["files"]:
            source,target=Path(member["source"]),Path(member["target"])
            _reject_reparse(source); _reject_reparse(target)
            source_exists=source.exists(); target_exists=target.exists()
            source_absent&=not source_exists; target_absent&=not target_exists
            source_ok&=source_exists and self._sha256(source)==member["sha256"]
            target_ok&=target_exists and self._sha256(target)==member["sha256"]
        return source_ok,target_ok,source_absent,target_absent

    def _instance_store(self,intent,stores):
        descriptor=self.configuration.resolve(path=intent["owner_path"])
        return stores[descriptor.scope().key]

    def _observer(self,intent,stores):
        store=self._instance_store(intent,stores)
        def observe(event,intended,staged,copied_identity=None):
            cleanup_phases={"discard":"target","remove-source":"source","discard-staged":"staging","discard-restore":"source",
                "undo-stage":"target","undo-restore":"target","undo-discard":"target","undo-source":"source"}
            if event in cleanup_phases:
                phase=cleanup_phases[event]; endpoint="target" if phase=="staging" else phase
                member=next((member for member in intent["transfer"]["files"] if path_key(member[endpoint])==path_key(intended)),None)
                if member is None:raise HistoryError("Unexpected move cleanup endpoint.")
                path=Path(staged); _reject_reparse(path)
                if _source_token(path)!=self._expected_token(intent,stores,phase,intended) or self._sha256(path)!=member["sha256"]:
                    raise HistoryError("Move cleanup instance changed; preserve it for exact recovery.")
                return
            phase="staging" if event=="staged" else "target" if event=="publish" else "source"
            endpoint="target" if phase=="staging" else phase
            member=next((member for member in intent["transfer"]["files"] if path_key(member[endpoint])==path_key(intended)),None)
            if member is None:raise HistoryError("Unexpected move publication endpoint.")
            _reject_reparse(Path(staged))
            if event=="staged" and copied_identity is None:
                raise HistoryError("Verified copy omitted its created-file instance witness.")
            if copied_identity is not None and _digest(copied_identity)!=_source_token(Path(staged)):
                raise HistoryError("Verified copy staging was replaced after file creation.")
            origin_phase="source" if phase in {"target","staging"} else "target"
            origin=Path(member[origin_phase]); _reject_reparse(origin)
            if _source_token(origin)!=self._expected_token(intent,stores,origin_phase,origin):
                raise HistoryError("Move publication origin instance was replaced.")
            if self._sha256(Path(staged))!=member["sha256"]:
                raise HistoryError("Move publication bytes differ from the admitted family.")
            if event=="publish" and intent["transfer"]["mode"]!="rename" and _source_token(Path(staged))!=self._expected_token(intent,stores,"staging",intended):
                raise HistoryError("Verified staging instance was replaced before publication.")
            store._append({"event":"move-instance","entity_id":intent["entity_id"],
                "version_id":intent["receipt_id"],"transaction_id":phase+":"+_digest(str(path_key(intended))),
                "source_token":_source_token(Path(staged))})
        return observe

    def _expected_token(self,intent,stores,phase,path):
        event=self._instance_store(intent,stores)._find(kind="move-instance",entity=intent["entity_id"],version=intent["receipt_id"],tx=phase+":"+_digest(str(path_key(path))))
        return event["source_token"] if event is not None else intent.get(phase+"_tokens",{}).get(str(path_key(path)))

    def _verify_instances(self,intent,stores):
        for member in intent["transfer"]["files"]:
            for phase in ("source","target"):
                path=Path(member[phase]); _reject_reparse(path)
                if path.exists() and _source_token(path)!=self._expected_token(intent,stores,phase,path):
                    raise HistoryError("A move endpoint was replaced; equal bytes do not prove image identity.")

    def _verify_reserved(self,intent,stores):
        for before in intent["before_bindings"]:
            current=stores[before["scope"]].binding(locator=before["locator"])
            if current!=before["event"] and (not current or current.get("receipt_id")!=intent["receipt_id"]):
                raise HistoryError("Managed move reservation was superseded; recovery is held.")

    def _finish(self,intent,stores,outcome):
        self._verify_reserved(intent,stores)
        self._verify_instances(intent,stores)
        source_ok,target_ok,source_absent,target_absent=self._family_state(intent)
        copied=bool(intent["transfer"].get("preserve_source"))
        if outcome=="completed":
            if not target_ok or not (source_ok if copied else source_absent):raise HistoryError("Move completion does not match the exact verified family.")
            self._import_versions(intent,stores)
            token=_source_token(Path(intent["target"]))
            if intent["source_scope"]:
                self._transition(stores[intent["source_scope"]],intent,intent["source_locator"],
                    "transit" if intent["transit"] else "retired",token if intent["transit"] else intent["source_token"],
                    transit_path=intent["target"] if intent["transit"] else "")
            if intent["target_scope"]:
                self._transition(stores[intent["target_scope"]],intent,intent["target_locator"],"active",token)
            self._write(intent,"complete")
        elif outcome=="rolled_back":
            if not source_ok or not target_absent:raise HistoryError("Move rollback does not match the original family.")
            if intent["target_scope"] and (intent["target_scope"],intent["target_locator"])!=(intent["source_scope"],intent["source_locator"]):
                self._transition(stores[intent["target_scope"]],intent,intent["target_locator"],"retired","")
            if intent["source_scope"]:
                transit=bool(intent["prior"] and self._transit(intent["source"]))
                restored=intent if not transit else {**intent,"receipt_id":intent["prior"]["receipt_id"]}
                if intent.get("copy_recycle") or intent.get("recycle_lifecycle") or intent.get("organization_route"):
                    prefix="recyclerollback-" if intent.get("recycle_lifecycle") else "copyrollback-"
                    restored={**intent,"receipt_id":prefix+intent["receipt_id"]+"-"+self.pointer(intent)["digest"]}
                elif intent.get("organization_restore"):
                    # A verified failed inverse did not change placement. Keep
                    # its selected operation retryable, renewing only the exact
                    # rollback instance already proved above.
                    restored={**intent,"receipt_id":intent["organization_restore"]["pointer"]["receipt_id"]}
                self._transition(stores[intent["source_scope"]],restored,intent["source_locator"],"transit" if transit else "active",
                    _source_token(Path(intent["source"])),transit_path=intent["source"] if transit else "")
            self._write(intent,"rolled_back")
        else:raise HistoryError("Move recovery requires attention before binding continuity can resume.")

    def execute(self,intent,transfer):
        """Execute a callable verified transfer while admitted bindings are held."""
        with self._locked(intent,initial_copy=True) as stores:
            self._validate_before(intent,stores)
            self._write(intent,"prepared")
            try:
                self._reserve(intent,stores)
                transfer(self._observer(intent,stores))
            except Exception as exc:
                if isinstance(exc,TransferRecoveryRequired):raise
                try:
                    if self._family_state(intent)[0] and self._family_state(intent)[3]:
                        self._finish(intent,stores,"rolled_back")
                    else:raise HistoryError("The exact family is not fully rolled back.")
                except Exception as recovery:
                    raise TransferRecoveryRequired("Saved-history move recovery is pending: "+str(recovery)) from exc
                raise
            try:self._finish(intent,stores,"completed")
            except Exception as exc:
                raise TransferRecoveryRequired("Files moved; exact saved-history continuity needs restart recovery: "+str(exc)) from exc

    def recover(self,pointer,payload,engine,operation_id):
        """Only explicit recorded native intents can renew instance bindings."""
        try:value=self._load(pointer)
        except FileNotFoundError as exc:
            raise HistoryError("Missing native move receipt; retain both locations for exact recovery.") from exc
        intent=value["intent"]
        if intent.get("copy_recycle") and intent["copy_recycle"].get("operation_id")!=operation_id:
            raise HistoryError("Linked recovery requires its exact native operation identity.")
        if intent.get("recycle_lifecycle") and intent["recycle_lifecycle"].get("operation_id")!=operation_id:
            raise HistoryError("Recycle recovery requires its exact native operation identity.")
        expected={key:item for key,item in payload.items() if key!="history_move"}
        if expected!=intent["transfer"]:raise HistoryError("Native receipt differs from the exact journaled transfer.")
        if intent.get("copy_undo"):
            return self._recover_copy_undo(value,engine,operation_id)
        with self._locked(intent) as stores:
            self._verify_instances(intent,stores)
            if value["status"]=="rolled_back":
                if intent["source_scope"]:
                    binding=stores[intent["source_scope"]].binding(locator=intent["source_locator"])
                    receipt=intent["prior"]["receipt_id"] if intent["prior"] and self._transit(intent["source"]) else intent["receipt_id"]
                    if intent.get("copy_recycle") or intent.get("recycle_lifecycle") or intent.get("organization_route"):
                        prefix="recyclerollback-" if intent.get("recycle_lifecycle") else "copyrollback-"
                        receipt=prefix+intent["receipt_id"]+"-"+self.pointer(intent)["digest"]
                    elif intent.get("organization_restore"):
                        receipt=intent["organization_restore"]["pointer"]["receipt_id"]
                    if not binding or binding.get("receipt_id")!=receipt:raise HistoryError("Rolled-back move receipt was superseded.")
                if not self._family_state(intent)[0] or not self._family_state(intent)[3]:raise HistoryError("Rolled-back move family changed.")
                if intent["target_scope"] and (intent["target_scope"],intent["target_locator"])!=(intent["source_scope"],intent["source_locator"]):
                    binding=stores[intent["target_scope"]].binding(locator=intent["target_locator"])
                    if not binding or binding.get("receipt_id")!=intent["receipt_id"] or binding.get("binding_state")!="retired":
                        raise HistoryError("Rolled-back destination reservation changed.")
                return TransferRecoveryOutcome("rolled_back","Native move rollback already completed.")
            if value["status"]=="complete":
                # Replay is a no-op only while this receipt still owns the
                # binding and exact current destination family.
                target_store=stores.get(intent["target_scope"] or intent["source_scope"])
                locator=intent["target_locator"] or intent["source_locator"]
                binding=target_store.binding(locator=locator)
                if not binding or binding.get("receipt_id")!=intent["receipt_id"]:raise HistoryError("Move receipt was superseded by later identity work.")
                if not self._family_state(intent)[1] or binding.get("source_token")!=_source_token(Path(intent["target"])):
                    raise HistoryError("Completed move identity changed before replay.")
                return TransferRecoveryOutcome("completed","Saved-history move already completed.")
            self._verify_reserved(intent,stores)
            for member in intent["transfer"]["files"]:
                target=Path(member["target"])
                temporary=engine._temporary_target(target,operation_id)
                _reject_reparse(temporary)
                if temporary.exists() and (_source_token(temporary)!=self._expected_token(intent,stores,"staging",target)
                        or engine._sha256(temporary)!=member["sha256"]):
                    raise HistoryError("Unqualified staged move file; preserve it for exact recovery.")
                source=Path(member["source"])
                restore=source.with_name(f".{source.name}.collectigen-restore-{operation_id}.tmp")
                _reject_reparse(restore)
                if restore.exists() and (_source_token(restore)!=self._expected_token(intent,stores,"source",source)
                        or engine._sha256(restore)!=member["sha256"]):
                    raise HistoryError("Unqualified rollback staging; preserve it for exact recovery.")
            outcome=engine.recover(intent["transfer"],operation_id,identity_observer=self._observer(intent,stores))
            if outcome.status!="attention":self._finish(intent,stores,outcome.status)
            return outcome

    def admit_copy_undo(self,pointer,payload):
        value=self._load(pointer); previous=value["intent"]
        if value["status"]!="complete" or not previous["transfer"].get("preserve_source"):
            raise HistoryError("Copy Undo requires its exact completed native receipt.")
        files=[{key:member[key] for key in ("source","target","sha256")} for member in previous["transfer"]["files"]]
        if payload!={"mode":"copy_undo","files":files}:
            raise HistoryError("Copy Undo family differs from its native receipt.")
        with self._locked(previous) as stores:
            self._verify_instances(previous,stores)
            store=stores[previous["target_scope"]]
            entity=store.lookup(Path(previous["target"]))
            if entity is None or entity.entity_id!=previous["entity_id"] or store.has_pending(entity):
                raise HistoryError("Copy identity changed or has pending saved-data recovery.")
            binding=store.binding(locator=entity.locator)
            if binding.get("receipt_id")!=previous["receipt_id"]:
                raise HistoryError("Copy Undo receipt has been superseded.")
            return {**previous,"receipt_id":uuid4().hex,"copy_undo":True,
                "transfer":{"mode":"copy_undo","files":[dict(member) for member in payload["files"]]},
                "prior":pointer,"target_tokens":{str(path_key(member["target"])):_source_token(Path(member["target"])) for member in payload["files"]},
                "before_bindings":[{"scope":store.scope.key,"locator":entity.locator,"event":binding}]}

    def _copy_undo_finish(self,intent,stores,outcome,operation_id):
        self._verify_reserved(intent,stores)
        self._verify_instances(intent,stores)
        source_ok,target_ok,_source_absent,target_absent=self._family_state(intent)
        if not source_ok or not (target_absent if outcome=="completed" else target_ok):
            raise HistoryError("Copy Undo does not match its exact family.")
        for member in intent["transfer"]["files"]:
            if VerifiedTransferEngine._copy_undo_temporary(Path(member["target"]),operation_id).exists():
                raise HistoryError("Copy Undo still has staged family members.")
        restored=intent if outcome=="completed" else {**intent,"receipt_id":intent["prior"]["receipt_id"]}
        self._transition(stores[intent["target_scope"]],restored,intent["target_locator"],
            "retired" if outcome=="completed" else "active",intent["target_tokens"][str(path_key(intent["target"]))])
        self._write(intent,"complete" if outcome=="completed" else "rolled_back")

    def execute_copy_undo(self,intent,engine,operation_id):
        with self._locked(intent) as stores:
            self._verify_reserved(intent,stores); self._verify_instances(intent,stores)
            for member in intent["transfer"]["files"]:
                temporary=engine._copy_undo_temporary(Path(member["target"]),operation_id)
                _reject_reparse(temporary)
                if temporary.exists():raise HistoryError("Copy Undo staging is occupied; retain it for exact recovery.")
            self._write(intent,"prepared")
            try:
                self._reserve(intent,stores)
                engine.execute_copy_undo(intent["transfer"],operation_id,identity_observer=self._observer(intent,stores))
            except Exception as exc:
                try:self._copy_undo_finish(intent,stores,"rolled_back",operation_id)
                except Exception as recovery:
                    raise TransferRecoveryRequired("Native Copy Undo requires exact recovery: "+str(recovery)) from exc
                raise
            try:self._copy_undo_finish(intent,stores,"completed",operation_id)
            except Exception as exc:raise TransferRecoveryRequired("Native Copy Undo history remains pending: "+str(exc)) from exc

    def _recover_copy_undo(self,value,engine,operation_id):
        intent=value["intent"]
        with self._locked(intent) as stores:
            self._verify_instances(intent,stores)
            if value["status"]=="rolled_back":
                current=stores[intent["target_scope"]].binding(locator=intent["target_locator"])
                if not current or current.get("receipt_id")!=intent["prior"]["receipt_id"] or not self._family_state(intent)[1]:
                    raise HistoryError("Copy Undo rollback was superseded.")
                return TransferRecoveryOutcome("rolled_back","Native Copy Undo rollback already completed.")
            self._verify_reserved(intent,stores)
            for member in intent["transfer"]["files"]:
                target=Path(member["target"]); temporary=engine._copy_undo_temporary(target,operation_id)
                _reject_reparse(temporary)
                if temporary.exists() and (_source_token(temporary)!=intent["target_tokens"][str(path_key(target))]
                        or engine._sha256(temporary)!=member["sha256"]):
                    raise HistoryError("The staged copy instance changed; Copy Undo recovery is held.")
            if value["status"] in {"complete","rolled_back"}:
                outcome="completed" if value["status"]=="complete" else "rolled_back"
            else:outcome=engine.recover_copy_undo(intent["transfer"],operation_id,identity_observer=self._observer(intent,stores)).status
            if outcome!="attention":self._copy_undo_finish(intent,stores,outcome,operation_id)
            return TransferRecoveryOutcome(outcome,"Native Copy Undo recovery: "+outcome)
