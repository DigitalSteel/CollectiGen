"""Shared native placement and purge mechanics; authority stays with typed providers."""
from contextlib import contextmanager, ExitStack
from dataclasses import asdict
from pathlib import Path

from .history_configuration import path_key
from .history_versions import _source_token, history_lock, _reject_reparse
from .organization_history import _digest


class NativeRecycleSupport:
    _intent_key = "copy_recycle"
    _purge_prefix = "copy-purge-"

    def _purge_record_link(self,record,witness=None):return self._link(record)

    def _purge_reference_matches(self,native,record,witness):
        reference=native.get(self._intent_key) or {}
        return reference.get("record_id")==record.id and reference.get("operation_id")==witness["operation_id"]
    def _current_bindings(self,intent):
        coordinator=self.moves.history_coordinator
        # Completed receipts retain their original admission token. Refresh only
        # the ambient configuration fingerprint; never change enrolled storage,
        # source role, zone, or identity to make an old placement fit.
        bindings=[]
        for item in intent["bindings"]:
            old=coordinator._descriptor(item["descriptor"])
            current=coordinator.configuration.resolve(path=item["path"])
            if (old.status,old.binding_json,old.zone_json)!=(current.status,current.binding_json,current.zone_json):
                raise ValueError("The retained copy's storage authority changed; review its exact configuration.")
            bindings.append({"path":item["path"],"descriptor":asdict(current)})
        return bindings

    def _verify_placement(self,intent):
        coordinator=self.moves.history_coordinator
        with coordinator._locked({**intent,"bindings":self._current_bindings(intent)}) as stores:
            # Do not call _verify_instances on the original copy or re-read its
            # source. Here intent describes only the retained family's move.
            owner=stores[intent["target_scope"] or intent["source_scope"]]
            binding=owner.binding(entity_id=intent["entity_id"])
            if (not binding
                    or binding.get("binding_state")!=("transit" if intent["transit"] else "active")
                    or binding.get("source_token")!=_source_token(Path(intent["target"]))):
                raise ValueError("A newer placement or replacement superseded this retained copy.")
            if owner.binding(locator=binding["locator"])!=binding:
                raise ValueError("Another native image now owns the retained copy's locator.")
            rollback=None
            if binding.get("receipt_id")!=intent["receipt_id"]:
                rollback=coordinator.linked_rollback(binding.get("receipt_id"),selected_pointer=coordinator.pointer(intent),
                    owner_path=intent["owner_path"],source=intent["target"],entity_id=intent["entity_id"],stores=stores)
            family=self.moves._engine.related_files(Path(intent["target"]))
            if {path_key(path) for path in family}!={path_key(item["target"]) for item in intent["transfer"]["files"]}:
                raise ValueError("The retained copied family changed.")
            for item in intent["transfer"]["files"]:
                path=Path(item["target"])
                if (coordinator._sha256(path)!=item["sha256"] or _source_token(path)!=
                        coordinator._expected_token(rollback or intent,stores,"source" if rollback else "target",path)):
                    raise ValueError("The retained copied family changed or was replaced.")

    def purge_witness(self,record):
        """Freeze the exact native binding which explicit purge may retire."""
        link,_,intent=self._current(record)
        bindings=self._current_bindings(intent)
        with self.moves.history_coordinator._locked({**intent,"bindings":bindings}) as stores:
            store=stores[intent["target_scope"] or intent["source_scope"]]
            binding=store.binding(entity_id=link["member_id"])
            if (not binding or binding.get("source_token")!=_source_token(Path(intent["target"]))
                    or store.binding(locator=binding["locator"])!=binding):
                raise ValueError("The retained-family binding changed before purge admission.")
            if binding.get("receipt_id")!=intent["receipt_id"]:
                self.moves.history_coordinator.linked_rollback(binding.get("receipt_id"),selected_pointer=link["current_pointer"],
                    owner_path=intent["owner_path"],source=intent["target"],entity_id=intent["entity_id"],stores=stores)
            return {"schema_version":1,"record_id":record.id,"record_digest":_digest(record.to_dict()),
                "member_id":link["member_id"],"operation_id":link["current_operation_id"],
                "pointer":link["current_pointer"],"bindings":bindings,"scope":store.scope.key,"binding":binding,
                "lock_paths":[intent["source"],intent["target"]],
                "files":[{"path":item["target"],"sha256":item["sha256"],"source_token":_source_token(Path(item["target"]))}
                    for item in intent["transfer"]["files"]]}

    def purge_guard(self,witnesses,*,operation_id="",recovery=False):
        return self.purge_guard_pairs([(self,witness) for witness in witnesses],operation_id=operation_id,recovery=recovery)
    @contextmanager
    def purge_guard_pairs(self,pairs,*,operation_id="",recovery=False):
        """Lock configured native bindings until purge commit/retirement finishes."""
        if not pairs:
            yield {};return
        coordinator=self.moves.history_coordinator
        bindings=[];paths=set()
        for provider,witness in pairs:
            if (not isinstance(witness,dict) or witness.get("schema_version")!=1 or set(witness)!={"schema_version",
                    "record_id","record_digest","member_id","operation_id","pointer","bindings","scope","binding","lock_paths","files"}):
                raise ValueError("Unsupported retained-family purge witness.")
            for item in provider._current_bindings(witness):
                if item not in bindings:bindings.append(item)
            paths.update(path_key(path) for path in witness["lock_paths"])
        with coordinator.configuration.transfer_guard([(coordinator._descriptor(item["descriptor"]),Path(item["path"])) for item in bindings]),ExitStack() as stack:
            stores={}
            for item in bindings:
                store=coordinator._store(item["descriptor"])
                if store is not None:stores[store.scope.key]=store
            for key in sorted(stores):
                stack.enter_context(stores[key].locked());stores[key]._ready()
            for path in sorted(paths,key=str):
                _reject_reparse(path);stack.enter_context(history_lock(path.with_suffix(".tags.json.history.lock")))
            for provider,witness in pairs:
                record=self.recycle.store.load_strict(witness["record_id"]);link=provider._purge_record_link(record,witness)
                if (link["member_id"]!=witness["member_id"] or link["current_operation_id"]!=witness["operation_id"]
                        or link["current_pointer"]!=witness["pointer"] or link["pending"]
                        or record.status not in ({"active","purged"} if recovery else {"active"})):
                    raise ValueError("Copied-family purge no longer owns its exact Recycle record.")
                if not recovery and _digest(record.to_dict())!=witness["record_digest"]:
                    raise ValueError("Recycle record changed after purge preview.")
                store=stores[witness["scope"]];current=store.binding(entity_id=witness["member_id"])
                retired=provider._is_purge_retirement(current,witness,operation_id)
                if not (recovery and retired) and (current!=witness["binding"] or store.binding(locator=witness["binding"]["locator"])!=current):
                    raise ValueError("A foreign or newer native binding holds retained-family purge retirement.")
                pointer=witness["pointer"]
                value=coordinator._read_receipt(pointer,store.scope.history_root/"moves"/(pointer["receipt_id"]+".json"))
                native=value["intent"]
                if (value["status"]!="complete" or not provider._purge_reference_matches(native,record,witness)
                        or native["entity_id"]!=witness["member_id"]
                        or {path_key(item["target"]):item["sha256"] for item in native["transfer"]["files"]}!=
                           {path_key(item["path"]):item["sha256"] for item in witness["files"]}):
                    raise ValueError("Purge witness differs from the exact native retained-family receipt.")
                instance_intent=native;phase="target"
                binding=witness["binding"]
                if (binding.get("entity_id")!=native["entity_id"] or binding.get("binding_state")!="transit"
                        or binding.get("locator")!=native["source_locator"] or binding.get("transit_path")!=native["target"]):
                    raise ValueError("Purge witness is not the exact retained native placement.")
                if binding.get("receipt_id")!=native["receipt_id"]:
                    instance_intent=coordinator.linked_rollback_receipt(binding.get("receipt_id"),selected_pointer=pointer,
                        owner_path=native["owner_path"],source=native["target"],entity_id=native["entity_id"],stores=stores)
                    phase="source"
                for item in witness["files"]:
                    if coordinator._expected_token(instance_intent,stores,phase,Path(item["path"]))!=item["source_token"]:
                        raise ValueError("Purge file instance differs from its qualified native witness.")
            yield stores

    def _is_purge_retirement(self,binding,witness,operation_id):
        return bool(operation_id and binding and binding.get("entity_id")==witness["member_id"]
            and binding.get("locator")==witness["binding"]["locator"] and binding.get("binding_state")=="retired"
            and binding.get("receipt_id")==self._purge_prefix+operation_id
            and binding.get("source_token")==witness["binding"]["source_token"])

    def retire_purged(self,witnesses,operation_id,stores):
        for witness in witnesses:
            record=self.recycle.store.load_strict(witness["record_id"])
            link=self._purge_record_link(record,witness)
            if record.status!="purged" or record.routed_operation_id!=operation_id:
                raise ValueError("Only the exact committed purge may retire this retained identity.")
            if link["member_id"]!=witness["member_id"] or link["current_pointer"]!=witness["pointer"] or link["current_operation_id"]!=witness["operation_id"]:
                raise ValueError("The retained-family purge record changed before retirement.")
            if any(Path(item["path"]).exists() for item in witness["files"]):
                raise ValueError("Retained files still exist; native retirement is held.")
            store=stores[witness["scope"]];current=store.binding(entity_id=witness["member_id"])
            if self._is_purge_retirement(current,witness,operation_id):continue
            if current!=witness["binding"] or store.binding(locator=current["locator"])!=current:
                raise ValueError("A newer native binding prevents retained-family retirement.")
            store.transition_binding(witness["member_id"],current["locator"],current["source_token"],
                state="retired",receipt_id=self._purge_prefix+operation_id)


class NativeRecyclePurge:
    """One lock union for mixed typed witnesses; validators remain provider-owned."""
    def __init__(self,recycle):self.recycle=recycle

    def _provider(self,name):
        if name=="recycle":return self.recycle.organization_history
        if name=="copy_recycle":return self.recycle.copy_history
        raise ValueError("Unsupported native Recycle purge provider.")

    def pairs(self,witnesses):
        pairs=[];seen=set()
        for wrapper in witnesses:
            if not isinstance(wrapper,dict) or set(wrapper)!={"schema_version","provider","authority"} or wrapper["schema_version"]!=1:
                raise ValueError("Unsupported typed Recycle purge witness.")
            provider=self._provider(wrapper["provider"]);witness=wrapper["authority"]
            identifier=witness["record_id"]
            if identifier in seen:raise ValueError("Duplicated native Recycle purge authority.")
            seen.add(identifier);record=self.recycle.store.load_strict(identifier)
            expected="copy_recycle" if "copy_lifecycle" in record._extra else "recycle"
            if wrapper["provider"]!=expected:raise ValueError("Purge witness belongs to another lifecycle provider.")
            pairs.append((provider,witness))
        return pairs

    def guard(self,witnesses,*,operation_id="",recovery=False):
        return self.recycle.copy_history.purge_guard_pairs(self.pairs(witnesses),operation_id=operation_id,recovery=recovery)

    def retire(self,witnesses,operation_id,stores):
        for provider,witness in self.pairs(witnesses):provider.retire_purged((witness,),operation_id,stores)
