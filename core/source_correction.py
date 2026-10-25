from __future__ import annotations

import codecs
import hashlib
import json
import re
import shutil
import stat
from dataclasses import dataclass,field,replace
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from .caption_import import parse_generated_caption
from .external_write_transaction import (
    ExternalWriteDecision,ExternalWriteExecution,
    atomic_verified_bytes as _atomic_verified_bytes,
    content_fingerprint as _content_fingerprint,
    copy_verified as _copy_verified,
    external_write_validation_issue,file_fingerprint as _fingerprint,
    same_content as _same_content,validated_recovery_root,
)
from .confirmed_tag_identity import ConfirmedTagIdentityService
from .image_record import ImageRecord,ImageRecordEditSnapshot
from .image_fact_decisions import ImageFindingDecisionService,image_tag_identity
from .image_record_recovery import (
    ImageRecordRecoveryPlan,ImageRecordRecoveryService,
)
from .operation_journal import OperationJournal
from .settings import normalize_plain_language_exclusion
from .tag_collection import TagCollection,normalize_tag


SOURCE_ONLY="source_only"
PAIRED_IMAGE_KNOWLEDGE="paired_image_knowledge"
PROJECTION_EXCLUSION="projection_exclusion"


class SourceCorrectionHistory:
    """Exact-image native receipts; never scan external backup directories.

    Source publication precedes the canonical saved-fact transaction. Restart
    cancels untouched work or finishes the approved missing half. Divergence
    retains both authorities for review, with no correction-level rollback.
    """
    KIND="collectigen_native_source_correction"
    LIMIT=16*1024*1024

    def __init__(self,owner,factory):self.owner=owner;self.factory=factory

    def operation(self,image):
        return self.factory((Path(image),)) if self.factory is not None else None

    def admit(self,plan,*,restore=False):
        current=self.operation(plan.image)
        frozen=plan.history_operation
        if (current is None)!=(frozen is None):
            raise RuntimeError("Saved-history participation changed after preview. Preview this correction again.")
        if current is None:return None
        if current.members!=frozen.members:
            raise RuntimeError("Saved-history configuration changed after preview. Preview again.")
        # The frozen operation also guards the exact pre-edit source instance.
        if not restore:
            frozen.context_for(plan.image).store.validate_entity(frozen.context_for(plan.image).entity)
            return frozen
        return current

    @staticmethod
    def _raw(path):
        from .history_versions import read_record
        return read_record(Path(path))

    @staticmethod
    def _encode(raw):return None if raw is None else raw.hex()

    @staticmethod
    def _decode(raw):return None if raw is None else bytes.fromhex(raw)

    @staticmethod
    def _head(context):
        head=context.store.current_head(context.entity)
        return head.version_id if head else ""

    @staticmethod
    def _check_path(path,root):
        from .history_versions import _reject_reparse
        path=Path(path).absolute();root=Path(root).absolute()
        if not path.is_relative_to(root):raise RuntimeError("Native correction evidence escapes its private scope.")
        for item in (path,*path.parents):
            _reject_reparse(item)
            if item==root:break
        return path

    def _directory(self,context):
        return self._check_path(context.store.scope.history_root/"source-corrections"/context.entity.entity_id,
            context.store.scope.history_root)

    def _write(self,path,payload):
        data=SourceCorrectionService._recovery_text({"schema_version":1,"kind":self.KIND,
            "digest":SourceCorrectionService._json_fingerprint(payload),"payload":payload})
        if len(data)>self.LIMIT:raise RuntimeError("Native source correction exceeds its bounded receipt size.")
        from .history_versions import _reject_reparse
        _reject_reparse(path)
        _atomic_verified_bytes(path,data)

    def _load(self,path):
        from .history_versions import _reject_reparse
        _reject_reparse(path)
        if path.stat().st_size>self.LIMIT:raise RuntimeError("Native correction receipt exceeds its supported size.")
        with path.open("rb") as stream:raw=stream.read(self.LIMIT+1)
        if len(raw)>self.LIMIT:raise RuntimeError("Native correction receipt grew beyond its supported size.")
        value=json.loads(raw)
        if (value.get("schema_version")!=1 or value.get("kind")!=self.KIND
                or value.get("digest")!=SourceCorrectionService._json_fingerprint(value.get("payload"))):
            raise RuntimeError("Unsupported or damaged native source-correction receipt.")
        return value["payload"]

    @staticmethod
    def _evidence(path):
        from .history_versions import _source_token,_reject_reparse
        _reject_reparse(path)
        token=_source_token(path) if path.exists() else None
        value=_fingerprint(path)
        if value.get("exists"):value["instance"]=_source_token(path)
        if value.get("instance")!=token:raise RuntimeError("The source instance changed during verification.")
        return value

    def _exact(self,path,evidence):
        current=self._evidence(path)
        return _same_content(current,evidence) and current.get("instance")==evidence.get("instance")

    def _metadata(self):
        if hasattr(self.owner,"_metadata"):return self.owner._metadata()
        from .metadata_sync import MetadataSyncService
        return MetadataSyncService()

    @staticmethod
    def recovery_service(context):
        root=SourceCorrectionHistory._check_path(
            context.store.scope.history_root/"source-correction-records"/context.entity.entity_id/context.operation_id,
            context.store.scope.history_root)
        return ImageRecordRecoveryService(history_context=context,
            backup_root=root)

    def _receipt(self,context,path):
        directory=self._directory(context)
        path=self._check_path(path,directory)
        if path.parent.parent!=directory or path.name!="receipt.json" or not re.fullmatch("[0-9a-f]{32}",path.parent.name):
            raise RuntimeError("Native correction receipt is outside this exact entity.")
        value=self._load(path)
        if (value.get("entity_id")!=context.entity.entity_id or value.get("scope")!=context.store.scope.key
                or value.get("locator")!=context.entity.locator or value.get("id")!=path.parent.name):
            raise RuntimeError("Native correction receipt does not authorize this image.")
        if (value.get("source_kind") not in {"training_txt","managed_metadata"}
                or value.get("meaning") not in {SOURCE_ONLY,PAIRED_IMAGE_KNOWLEDGE}
                or value.get("direction") not in {"apply","restore"}
                or not re.fullmatch("[0-9a-f]{32}",str(value.get("operation_id","")))):
            raise RuntimeError("Unsupported native correction transaction.")
        return value

    def _qualify_pending(self,context,value):
        """Read only this entity's bounded pending events before canonical recovery."""
        store=context.store
        if not store.has_pending(context.entity):return
        with store._db() as db:
            rows=db.execute("SELECT p.seq,p.offset,p.length,p.digest FROM events p WHERE p.kind='prepare' AND p.entity=? AND NOT EXISTS (SELECT 1 FROM events f WHERE f.tx=p.tx AND f.kind IN ('version','abort')) ORDER BY p.seq LIMIT 101",
                (context.entity.entity_id,)).fetchall()
        if len(rows)!=1:raise RuntimeError("Resolve unrelated pending saved history before this correction.")
        event=store._event_at(rows[0])
        if (event.get("event")!="prepare" or event.get("entity_id")!=context.entity.entity_id
                or event.get("operation_id")!=value["operation_id"]
                or store.payload(context.entity,event["before"])!=self._decode(value["record_before"])
                or store.payload(context.entity,event["after"])!=self._decode(value["record_after"])
                or event.get("restored_from","")!=value["restore_version"]):
            raise RuntimeError("Pending saved history is not qualified by this exact correction receipt.")

    def _intended(self,plan):
        from .annotation import ImageAnnotation
        annotation=ImageAnnotation.from_dict(plan.paired_recovery_plan.materialize(),plan.image.name)
        if self.owner.knowledge is not None:
            ConfirmedTagIdentityService(self.owner.knowledge).synchronize_annotation(annotation)
        return (json.dumps(annotation.to_dict(),indent=2,ensure_ascii=False)+"\n").encode("utf-8")

    def _prepare_source(self,context,plan,directory,*,candidate=None):
        source=plan.source_path
        before=self._evidence(source)
        if before.get("size",0)>(256*1024*1024 if plan.source_kind=="managed_metadata" else self.LIMIT//2):
            raise RuntimeError("Source correction exceeds its supported size bound.")
        backup=directory/"source.before"
        _copy_verified(source,backup,expected=before,label="Private original source backup")
        if plan.source_kind=="managed_metadata":
            from .history_projection import prepare_projection_from_backup
            if candidate is None:
                prepared=self.owner._native_prepare(context,plan,backup)
            else:
                prepared=prepare_projection_from_backup(context,plan.image,self._metadata(),candidate_backup=candidate,
                    expected_candidate_sha256=_fingerprint(candidate)["sha256"],current_backup=backup)
            intent=prepared.intent
            after={"exists":True,"sha256":intent["after_digest"],"size":Path(intent["staged"]).stat().st_size,
                "instance":intent["new_token"]}
            return before,after,{"identifier":prepared.identifier,"intent":intent},prepared
        from .verified_transfer import VerifiedTransferEngine
        staged=source.with_name(f".{source.name}.{directory.name}.source-stage.tmp")
        parked=source.with_name(f".{source.name}.{directory.name}.source-original.tmp")
        from .history_versions import _reject_reparse
        _reject_reparse(staged);_reject_reparse(parked)
        if staged.exists() or parked.exists():raise RuntimeError("Native TXT publication paths are occupied.")
        data=candidate.read_bytes() if candidate is not None else plan.updated_bytes
        import os
        with staged.open("xb") as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        after=self._evidence(staged)
        if not _same_content(after,_content_fingerprint(data)):raise RuntimeError("Staged TXT failed verification.")
        return before,after,{"staged":str(staged),"parked":str(parked)},None

    def _publish_txt(self,source,value):
        from .verified_transfer import VerifiedTransferEngine
        staged=Path(value["publication"]["staged"]);parked=Path(value["publication"]["parked"])
        if not self._exact(source,value["before"]) or not self._exact(staged,value["after"]) or parked.exists():
            raise RuntimeError("TXT publication identity changed. All instances were retained.")
        VerifiedTransferEngine._rename_no_replace(source,parked)
        if not self._exact(parked,value["before"]) or not self._exact(staged,value["after"]):
            raise RuntimeError("TXT instance changed during publication; recovery is required.")
        VerifiedTransferEngine._rename_no_replace(staged,source)
        if not self._exact(source,value["after"]):raise RuntimeError("TXT publication failed exact instance verification.")

    def _finish_facts(self,context,image,value):
        from .annotation import ImageAnnotation
        expected=self._decode(value["record_before"]);intended=self._decode(value["record_after"])
        context=replace(context,operation_id=value["operation_id"])
        service=self.recovery_service(context)
        self._qualify_pending(context,value)
        service.recover_pending(image)
        current=self._raw(image.with_suffix(".tags.json"))
        if current not in (expected,intended):raise RuntimeError("Image Knowledge changed independently; paired correction was held.")
        if value["meaning"]==PAIRED_IMAGE_KNOWLEDGE:
            if current==expected and current!=intended:
                if self._head(context)!=value["head_before"]:
                    head=context.store.current_head(context.entity)
                    if (head is None or head.operation_id!=value["operation_id"]
                            or head.origin not in {"baseline","observed-current"}
                            or head.parent_id!=value["head_before"]
                            or context.store.payload(context.entity,head.snapshot)!=expected):
                        raise RuntimeError("Saved history changed before the approved paired correction.")
                if value["direction"]=="restore":
                    service=self.recovery_service(replace(context,restored_from=value["restore_version"]))
                    service.restore_saved_record(image,expected=expected,intended=intended)
                else:
                    record=ImageRecord(image,known_tags=self.owner.knowledge)
                    snapshot=ImageRecordEditSnapshot.capture(record)
                    record.annotation=ImageAnnotation.from_dict(json.loads(intended),image.name)
                    record.modified=True
                    plan=service.plan(snapshot,record)
                    if plan.conflicts:raise RuntimeError("Paired saved facts need a new explicit conflict decision.")
                    service.apply(plan,knowledge=self.owner.knowledge)
            if self._raw(image.with_suffix(".tags.json"))!=intended:
                raise RuntimeError("Paired saved-fact verification failed.")
            head=context.store.current_head(context.entity)
            if head is None or head.operation_id!=value["operation_id"]:
                raise RuntimeError("The paired saved-fact version is not receipt-qualified.")
            if value["direction"]=="apply":
                previous=context.store.version(context.entity,head.parent_id)
                if context.store.payload(context.entity,previous.snapshot)!=expected:
                    raise RuntimeError("The native before-version does not match the approved saved facts.")
                value["before_version"]=previous.version_id
            value["head_after"]=head.version_id

    def _finish(self,context,image,path,value):
        source=image if value["source_kind"]=="managed_metadata" else image.with_suffix(".txt")
        if not self._exact(source,value["after"]):raise RuntimeError("The approved corrected source instance is unavailable.")
        self._finish_facts(context,image,value)
        if not self._exact(source,value["after"]):raise RuntimeError("The corrected source changed during the paired saved-fact write.")
        if value["source_kind"]=="training_txt":
            parked=Path(value["publication"]["parked"])
            if parked.exists():
                if not self._exact(parked,value["before"]) or not _same_content(_fingerprint(path.parent/"source.before"),value["before"]):
                    raise RuntimeError("Original TXT or verified backup changed; cleanup was held.")
                parked.unlink()
        value["status"]="complete"
        self._write(path,value)
        if value["direction"]=="restore":
            original_path=self._directory(context)/value["original_id"]/"receipt.json"
            original=self._receipt(context,original_path)
            original["status"]="restored";original["restored_by"]=value["id"]
            self._write(original_path,original)
        self._clear_pending(context,value["id"])

    def _clear_pending(self,context,identifier):
        pointer=self._directory(context)/"pending.json"
        if self._load(pointer)!={"id":identifier}:raise RuntimeError("The pending correction pointer changed.")
        pointer.unlink()

    def recover(self,operation,image):
        image=Path(image).resolve();context=operation.context_for(image)
        with context.locked(image):
            directory=self._directory(context);pointer=directory/"pending.json"
            if not pointer.exists():
                if context.store.has_pending(context.entity):raise RuntimeError("Resolve interrupted saved history before previewing source corrections.")
                return ()
            identifier=self._load(pointer).get("id","")
            if not isinstance(identifier,str) or not re.fullmatch("[0-9a-f]{32}",identifier):
                raise RuntimeError("Invalid exact-image pending correction pointer.")
            path=directory/identifier/"receipt.json";value=self._receipt(context,path)
            if value["status"]=="cancelled":self._clear_pending(context,identifier);return (path,)
            source=image if value["source_kind"]=="managed_metadata" else image.with_suffix(".txt")
            if value["source_kind"]=="training_txt":
                # Derived sibling names are checked before touching retained instances.
                for key,suffix in (("staged","source-stage"),("parked","source-original")):
                    expected=source.with_name(f".{source.name}.{identifier}.{suffix}.tmp")
                    if Path(value["publication"][key])!=expected:raise RuntimeError("Unsafe TXT recovery path.")
                parked=Path(value["publication"]["parked"])
                if not source.exists() and self._exact(parked,value["before"]):
                    from .verified_transfer import VerifiedTransferEngine
                    VerifiedTransferEngine._rename_no_replace(parked,source)
            if self._exact(source,value["after"]):
                self._finish(context,image,path,value)
            elif self._exact(source,value["before"]) and self._raw(image.with_suffix(".tags.json"))==self._decode(value["record_before"]):
                # No approved authority was changed: preserve scratch for inspection.
                value["status"]="cancelled";self._write(path,value);self._clear_pending(context,identifier)
            else:raise RuntimeError("Interrupted source correction differs from its exact receipt. Preserve files and review recovery.")
            return (path,)

    def _begin(self,operation,plan,meaning,*,original=None,context=None):
        image=plan.image
        if context is None:context=operation.context_for(image)
        with context.locked(image):
            if context.store.has_pending(context.entity):raise RuntimeError("Resolve pending saved history before correcting a source.")
            directory=self._directory(context);pointer=directory/"pending.json"
            if pointer.exists():raise RuntimeError("Resolve this image's pending source correction first.")
            identifier=uuid4().hex;folder=directory/identifier
            folder.mkdir(parents=True,exist_ok=False)
            record_before=self._raw(image.with_suffix(".tags.json"))
            candidate=None
            if original is not None:
                candidate=directory/original["id"]/"source.before"
                self._check_path(candidate,directory)
                if not _same_content(_fingerprint(candidate),original["before"]):raise RuntimeError("The original private source backup changed.")
                record_after=self._decode(original["record_before"])
            else:record_after=self._intended(plan) if meaning==PAIRED_IMAGE_KNOWLEDGE else record_before
            if meaning==PAIRED_IMAGE_KNOWLEDGE:
                from .annotation import ImageAnnotation,SCHEMA_VERSION
                for raw in (record_before,record_after):
                    if raw is None:continue
                    parsed=json.loads(raw)
                    if (not isinstance(parsed,dict) or parsed.get("schema_version")!=SCHEMA_VERSION
                            or ImageAnnotation.from_dict(parsed,image.name).to_dict()!=parsed):
                        raise RuntimeError("Paired correction requires saved facts that can be restored without schema conversion.")
                if original is not None:
                    version=context.store.version(context.entity,original["before_version"])
                    if context.store.payload(context.entity,version.snapshot)!=record_after:
                        raise RuntimeError("The selected before-version no longer matches the approved exact restore.")
            before,after,publication,prepared=self._prepare_source(context,plan,folder,candidate=candidate)
            value={"id":identifier,"operation_id":operation.operation_id,"scope":context.store.scope.key,
                "entity_id":context.entity.entity_id,"locator":context.entity.locator,"source_kind":plan.source_kind,
                "meaning":meaning,"plan_fingerprint":plan.plan_fingerprint,"status":"pending",
                "direction":"restore" if original else "apply","before":before,"after":after,"publication":publication,
                "record_before":self._encode(record_before),"record_after":self._encode(record_after),
                "head_before":self._head(context),"before_version":"","head_after":"",
                "restore_version":original.get("before_version","") if original else "",
                "original_id":original["id"] if original else ""}
            path=folder/"receipt.json"
            self._write(path,value)
            if original is None and plan.backup_enabled:
                external=SourceCorrectionService._backup_root(plan.backup_directory)/("native_source_correction_"+identifier)
                external.mkdir(parents=True,exist_ok=False)
                _copy_verified(folder/"source.before",external/"source.before",expected=before,label="Selected external source backup")
                if meaning==PAIRED_IMAGE_KNOWLEDGE and record_before is not None:
                    _atomic_verified_bytes(external/"image-knowledge.before",record_before)
                # A distinct kind prevents old readers from applying raw restores.
                self._write(external/"recovery.json",{"native_receipt":str(path),"id":identifier})
            self._write(pointer,{"id":identifier})
            if not self._exact(plan.source_path,before) or self._raw(image.with_suffix(".tags.json"))!=record_before:
                raise RuntimeError("Source or saved facts changed before publication. No overwrite was attempted.")
            if prepared is not None:
                from .history_projection import publish_projection
                renewed=publish_projection(context,image,prepared)
                operation.replace_context(image,context,renewed);context=renewed
            else:self._publish_txt(plan.source_path,value)
            self._finish(context,image,path,value)
            return path,value

    def apply(self,operation,plan,execution,meaning):
        if meaning not in {SOURCE_ONLY,PAIRED_IMAGE_KNOWLEDGE}:raise RuntimeError("Unsupported correction meaning.")
        if not plan.will_write:raise RuntimeError(plan.detail)
        issue=external_write_validation_issue(plan.write_decision,execution or plan.write_decision.execution())
        if issue:raise RuntimeError("External-write authorization changed or its exact privacy confirmation is missing: "+issue)
        if meaning==PAIRED_IMAGE_KNOWLEDGE and (plan.paired_disposition!="change" or plan.paired_recovery_plan is None
                or not plan.backup_enabled or plan.backup_directory is None):
            raise RuntimeError("A paired correction needs its confirmed fact and verified external-write protection.")
        context=operation.context_for(plan.image)
        with context.locked(plan.image):
            if context.store.has_pending(context.entity) or (self._directory(context)/"pending.json").exists():
                raise RuntimeError("Resolve interrupted saved history or source correction before applying this preview.")
            if hasattr(self.owner,"_metadata"):
                current=self.owner.plan.__wrapped__(self.owner,plan.image,plan.value,value_kind=plan.value_kind,
                    source_field=plan.source_field,write_decision=plan.write_decision,_history_operation=operation)
            else:
                planner=self.owner.plan_txt_description_removal if plan.value_kind=="description" else self.owner.plan_txt_tag_removal
                current=planner.__wrapped__(self.owner,plan.image,plan.value,write_decision=plan.write_decision,_history_operation=operation)
            valid=current.plan_fingerprint==plan.plan_fingerprint and current.disposition==plan.disposition
            if not valid:raise RuntimeError("Source or Image Knowledge changed after preview. Preview again.")
            if not self._exact(plan.source_path,plan.history_source_evidence) or self._head(context)!=plan.history_head:
                raise RuntimeError("Source instance or saved-history head changed after preview.")
            path,value=self._begin(operation,plan,meaning,context=context)
        return SourceCorrectionResult("complete",plan.detail,plan.source_path,value["operation_id"],path,
            meaning=meaning,source_kind=plan.source_kind,value_kind=plan.value_kind)

    def restore(self,operation,plan,manifest):
        context=operation.context_for(plan.image)
        with context.locked(plan.image):
            original=self._receipt(context,Path(manifest))
            if original["plan_fingerprint"]!=plan.plan_fingerprint:raise RuntimeError("Restore does not match this correction preview.")
            if original["status"]=="restored":return SourceCorrectionRestoreResult("already_restored","Source correction was already restored.",plan.source_path)
            if original["status"]!="complete":raise RuntimeError("Recover the interrupted correction before restoring.")
            if not self._exact(plan.source_path,original["after"]):raise RuntimeError("The corrected source instance changed; restore was held.")
            if self._raw(plan.image.with_suffix(".tags.json"))!=self._decode(original["record_after"]):
                raise RuntimeError("Saved facts changed after correction; restore was held.")
            if original["meaning"]==PAIRED_IMAGE_KNOWLEDGE and self._head(context)!=original["head_after"]:
                raise RuntimeError("Saved history changed after correction; restore was held.")
            self._begin(operation,plan,original["meaning"],original=original,context=context)
        return SourceCorrectionRestoreResult("restored","Restored the exact source and selected saved-data version.",plan.source_path,original["meaning"])


def history_preview(method):
    """Admit capture before the legacy planner can attempt automatic recovery."""
    from functools import wraps
    @wraps(method)
    def wrapped(self,image,value,*args,**kwargs):
        history=self._history
        try:
            operation=history.operation(image)
            if operation is not None:history.recover(operation,image)
            context=operation.context_for(image) if operation is not None else None
            with context.locked(Path(image)) if context is not None else nullcontext():
                if context is not None and context.store.has_pending(context.entity):
                    raise RuntimeError("Resolve interrupted saved history before previewing source corrections.")
                plan=method(self,image,value,*args,_history_operation=operation,**kwargs)
                return replace(plan,history_operation=operation,
                    history_source_evidence=history._evidence(plan.source_path) if operation is not None and plan.will_write else {},
                    history_head=history._head(context) if context is not None else "")
        except Exception as exc:
            common=dict(image=Path(image).resolve(),value=str(value),disposition="held",detail=str(exc),
                write_decision=kwargs.get("write_decision") or ExternalWriteDecision())
            if hasattr(self,"_metadata"):
                from .metadata_source_correction import ManagedMetadataCorrectionPlan
                return ManagedMetadataCorrectionPlan(source_path=common["image"],source_kind="managed_metadata",
                    source_field=kwargs.get("source_field",""),value_kind=kwargs.get("value_kind",""),**common)
            return SourceCorrectionPlan(source_path=common["image"].with_suffix(".txt"),source_kind="training_txt",
                value_kind="description" if "description" in method.__name__ else "tag",**common)
    return wrapped


def history_apply(method):
    from functools import wraps
    @wraps(method)
    def wrapped(self,plan,*args,**kwargs):
        meaning=PAIRED_IMAGE_KNOWLEDGE if method.__name__=="apply_paired" else kwargs.get("meaning",SOURCE_ONLY)
        try:
            operation=self._history.admit(plan)
            if operation is None:return method(self,plan,*args,**kwargs)
            execution=kwargs.get("execution",args[0] if args else None)
            result=self._history.apply(operation,plan,execution,meaning)
        except Exception as exc:
            result=SourceCorrectionResult("held",str(exc)+" An interrupted correction may have retained a completed source or saved-fact change. Reopen this exact correction to recover before retrying.",plan.source_path,meaning=meaning,
                source_kind=plan.source_kind,value_kind=plan.value_kind)
        if hasattr(self,"_result"):
            return self._result(plan,result.status,result.detail,operation_id=result.operation_id,
                recovery_manifest=result.recovery_manifest,meaning=meaning,
                image_knowledge_recovery_manifest=result.image_knowledge_recovery_manifest)
        return result
    return wrapped


def history_restore(method):
    from functools import wraps
    @wraps(method)
    def wrapped(self,plan,manifest):
        try:
            operation=self._history.admit(plan,restore=True)
            if operation is None:return method(self,plan,manifest)
            result=self._history.restore(operation,plan,manifest)
        except Exception as exc:
            result=SourceCorrectionRestoreResult("held",str(exc)+" Reopen this exact correction to recover any interrupted restore before retrying.",plan.source_path)
        if hasattr(self,"_metadata"):
            from .metadata_source_correction import ManagedMetadataCorrectionRestoreResult
            return ManagedMetadataCorrectionRestoreResult(result.status,result.detail,result.source_path)
        return result
    return wrapped


def _remove_exact_tag_list_value(raw: str,value: str) -> tuple[str,int]:
    """Remove exact comma/newline-delimited tag fields without reformatting peers."""
    parts=re.split(r"(,[ \t]*|\r\n|\r|\n)",raw)
    wanted=normalize_tag(value)
    matches=[
        index for index in range(0,len(parts),2)
        if normalize_tag(parts[index].strip())==wanted
    ]
    for index in reversed(matches):
        if index+1<len(parts):
            del parts[index:index+2]
        elif index:
            del parts[index-1:index+1]
        else:
            parts[index]=""
    return "".join(parts),len(matches)


def _remove_app_owned_plain_language_value(
    raw: str,value: str,projection_state: dict[str,object],
) -> tuple[str,int,str]:
    """Remove one typed prose projection only when its exact structure is frozen."""

    tags=projection_state.get("sdxl_tags")
    prose=projection_state.get("plain_language")
    profile=projection_state.get("profile")
    separator=(profile.get("separator") if isinstance(profile,dict) else None)
    if (
        not isinstance(tags,list) or not isinstance(prose,list)
        or not isinstance(separator,str) or not separator
    ):
        return raw,0,"The app-owned TXT projection lacks an exact separator contract."
    values=[str(item) for item in prose]
    wanted=normalize_plain_language_exclusion(value)
    matches=[
        index for index,item in enumerate(values)
        if normalize_plain_language_exclusion(item)==wanted
    ]
    if len(matches)>1:
        return raw,0,"The selected description occurs more than once in this TXT projection."
    if not matches:
        return raw,0,"The exact Plain Language Description is no longer present in Training TXT."
    rendered=separator.join([*(str(item) for item in tags),*values])
    if raw!=rendered:
        return raw,0,"The app-owned TXT no longer matches its exact typed projection structure."
    del values[matches[0]]
    return separator.join([*(str(item) for item in tags),*values]),1,""


@dataclass(frozen=True)
class SourceCorrectionChoice:
    key: str
    label: str
    detail: str
    function_id: str
    availability: str="available"


@dataclass(frozen=True)
class SourceCorrectionPlan:
    image: Path
    source_path: Path
    source_kind: str
    value_kind: str
    value: str
    disposition: str
    detail: str
    original_text: str=""
    updated_text: str=""
    original_bytes: bytes=b""
    updated_bytes: bytes=b""
    occurrence_count: int=0
    confirmed_in_image_knowledge: bool=False
    app_owned_projection: bool=False
    utf8_bom: bool=False
    choices: tuple[SourceCorrectionChoice,...]=()
    source_fingerprint: dict[str,object]=field(default_factory=dict)
    authority_fingerprint: dict[str,object]=field(default_factory=dict)
    write_decision: ExternalWriteDecision=field(default_factory=ExternalWriteDecision)
    plan_fingerprint: str=""
    paired_disposition: str="unsupported"
    paired_detail: str=""
    paired_present_values: tuple[str,...]=()
    paired_absent_value: str=""
    paired_recovery_plan: ImageRecordRecoveryPlan|None=None
    history_operation: object=field(default=None,compare=False,repr=False)
    history_source_evidence: dict=field(default_factory=dict,compare=False,repr=False)
    history_head: str=""

    @property
    def will_write(self) -> bool:
        return self.disposition=="change"

    @property
    def external_policy_fingerprint(self) -> str:
        return self.write_decision.policy_fingerprint

    @property
    def backup_enabled(self) -> bool:
        return self.write_decision.backup_enabled

    @property
    def backup_directory(self) -> Path|None:
        return self.write_decision.backup_directory

    @property
    def privacy_confirmation_required(self) -> bool:
        return self.write_decision.privacy_confirmation_required


@dataclass
class SourceCorrectionResult:
    status: str
    detail: str
    source_path: Path
    operation_id: str=""
    recovery_manifest: Path|None=None
    meaning: str=SOURCE_ONLY
    image_knowledge_recovery_manifest: Path|None=None
    source_kind: str="training_txt"
    value_kind: str="tag"


@dataclass
class SourceCorrectionRestoreResult:
    status: str
    detail: str
    source_path: Path
    meaning: str=SOURCE_ONLY


class SourceCorrectionService:
    """Plan, apply, and restore one exact external source-value correction."""

    RECOVERY_KIND="collectigen_source_correction_recovery"

    def __init__(self,*,knowledge=None,recovery_service=None,history_operation_factory=None) -> None:
        self.knowledge=knowledge
        self.recovery_service=(
            recovery_service or ImageRecordRecoveryService()
        )
        self._history=SourceCorrectionHistory(self,history_operation_factory)

    def _known_tag(self,value: str) -> bool:
        return bool(self.knowledge is not None and self.knowledge.find(value))

    @staticmethod
    def _plan_fingerprint(payload: dict[str,object]) -> str:
        return hashlib.sha256(json.dumps(
            payload,sort_keys=True,separators=(",",":"),ensure_ascii=False,
        ).encode("utf-8")).hexdigest()

    @staticmethod
    def _backup_root(directory: Path|None) -> Path:
        return validated_recovery_root(
            directory,"source-corrections",workflow_label="Source correction",
        )

    @staticmethod
    def _choices(
        *,confirmed: bool,app_owned: bool,paired_disposition: str,
        paired_detail: str,
    ) -> tuple[SourceCorrectionChoice,...]:
        source_detail=(
            "Change only this TXT. The confirmed image fact remains valid and a "
            "future caption export may emit it again."
            if confirmed else
            "Change only this TXT without changing Image Knowledge."
        )
        if app_owned:
            source_detail+=(
                " This makes the current app-owned projection intentionally out of date."
            )
        return (
            SourceCorrectionChoice(
                SOURCE_ONLY,"Remove only from this Training TXT",source_detail,
                "dataset.source_correction.preview",
            ),
            SourceCorrectionChoice(
                PAIRED_IMAGE_KNOWLEDGE,"The fact is false for this image",
                paired_detail or
                "Remove the source value and explicitly correct Image Knowledge together.",
                "dataset.source_correction.preview",
                "available" if paired_disposition=="change" else "unavailable",
            ),
            SourceCorrectionChoice(
                PROJECTION_EXCLUSION,"Keep the fact but omit it from training",
                "Open the training-caption projection workflow. The confirmed "
                "fact remains true; only this image's versioned training "
                "exclusion decision changes.",
                "dataset.training_captions.preview",
            ),
        )

    @staticmethod
    def _json_fingerprint(value: object) -> str:
        return hashlib.sha256(json.dumps(
            value,sort_keys=True,separators=(",",":"),ensure_ascii=False,
        ).encode("utf-8")).hexdigest()

    def _paired_image_knowledge_plan(
        self,record: ImageRecord,value: str,*,value_kind: str="tag",
    ) -> tuple[str,str,tuple[str,...],str,ImageRecordRecoveryPlan|None]:
        if value_kind=="description":
            wanted=normalize_plain_language_exclusion(value)
            present=tuple(dict.fromkeys(
                item for item in record.annotation.plain_language
                if normalize_plain_language_exclusion(item)==wanted
            ))
            if not wanted or not present:
                return (
                    "unsupported",
                    "This value is not a confirmed Image Knowledge description. "
                    "Remove it from the external source only, or review the image "
                    "description first.",(),"",None,
                )
            if record.sidecar_path.is_symlink():
                return (
                    "held","Image Knowledge is a symbolic link and was not staged.",
                    present,present[0],None,
                )
            if (
                record.sidecar_path.exists()
                and not record.sidecar_path.stat().st_mode&stat.S_IWRITE
            ):
                return (
                    "held","Image Knowledge is read only and was not staged.",
                    present,present[0],None,
                )
            snapshot=ImageRecordEditSnapshot.capture(record)
            edited=ImageRecord(
                record.image_path,auto_load=False,known_tags=self.knowledge,
            )
            edited.annotation=deepcopy(record.annotation)
            edited.loaded=True
            edited.annotation.plain_language=[
                item for item in edited.annotation.plain_language
                if normalize_plain_language_exclusion(item)!=wanted
            ]
            ImageFindingDecisionService(
                edited.annotation,knowledge=self.knowledge,
            ).ignore_for_image(descriptions=present)
            edited.modified=True
            try:
                recovery_plan=self.recovery_service.plan(snapshot,edited)
            except Exception as exc:
                return (
                    "held",f"Image Knowledge could not be staged safely: {exc}",
                    present,present[0],None,
                )
            if recovery_plan.conflicts:
                return (
                    "held","Image Knowledge changed while the paired correction "
                    "was being prepared. Review the newest image data and try again.",
                    present,present[0],None,
                )
            occurrence=(
                "matching confirmed description"
                if len(present)==1 else "matching confirmed descriptions"
            )
            return (
                "change",
                f"remove {len(present):,} {occurrence} and record “{present[0]}” "
                "as not on this image while preserving raw evidence, unrelated "
                "descriptions, Tags, Metadata, and pixels.",
                present,present[0],recovery_plan,
            )
        if value_kind!="tag":
            return (
                "unsupported","This Image Knowledge value kind is unsupported.",
                (),"",None,
            )
        wanted=image_tag_identity(value,self.knowledge)
        present=tuple(dict.fromkeys(
            item for item in record.annotation.all_structured_tags()
            if image_tag_identity(item,self.knowledge)==wanted
        ))
        if not wanted or not present:
            return (
                "unsupported",
                "This value is not a confirmed Image Knowledge fact. Remove it "
                "from Training TXT only, or review the image fact first.",
                (),"",None,
            )
        if record.sidecar_path.is_symlink():
            return (
                "held","Image Knowledge is a symbolic link and was not staged.",
                present,present[0],None,
            )
        if (
            record.sidecar_path.exists()
            and not record.sidecar_path.stat().st_mode&stat.S_IWRITE
        ):
            return (
                "held","Image Knowledge is read only and was not staged.",
                present,present[0],None,
            )
        snapshot=ImageRecordEditSnapshot.capture(record)
        edited=ImageRecord(record.image_path,auto_load=False,known_tags=self.knowledge)
        edited.annotation=deepcopy(record.annotation)
        edited.loaded=True
        for item in present:
            edited.remove_tag(item)
        absent_identity={
            image_tag_identity(item,self.knowledge)
            for item in edited.annotation.confirmed_absent
        }
        absent_value=present[0]
        if wanted not in absent_identity:
            edited.annotation.confirmed_absent=TagCollection([
                *edited.annotation.confirmed_absent,absent_value,
            ]).to_list()
        if self.knowledge is not None:
            ConfirmedTagIdentityService(
                self.knowledge,
            ).synchronize_annotation(edited.annotation)
        edited.modified=True
        try:
            recovery_plan=self.recovery_service.plan(snapshot,edited)
        except Exception as exc:
            return (
                "held",f"Image Knowledge could not be staged safely: {exc}",
                present,absent_value,None,
            )
        if recovery_plan.conflicts:
            return (
                "held","Image Knowledge changed while the paired correction "
                "was being prepared. Review the newest image data and try again.",
                present,absent_value,None,
            )
        occurrence=(
            "matching confirmed fact occurrence"
            if len(present)==1 else "matching confirmed fact occurrences"
        )
        return (
            "change",
            f"remove {len(present):,} {occurrence} "
            f"and record “{absent_value}” as confirmed absent while preserving "
            "raw evidence, unrelated facts, descriptions, Metadata, and pixels.",
            present,absent_value,recovery_plan,
        )

    def recover_pending_paired(
        self,image: str|Path,backup_directory: str|Path,
    ) -> tuple[Path,...]:
        """Resolve interrupted paired writes without overwriting newer authority."""

        image_path=Path(image).resolve()
        operation=self._history.operation(image_path)
        if operation is not None:return self._history.recover(operation,image_path)
        root=self._backup_root(Path(backup_directory))
        if not root.exists():
            return ()
        recovered=[]
        for manifest in sorted(root.glob("source_correction_*/recovery.json")):
            try:
                payload=json.loads(manifest.read_text(encoding="utf-8"))
            except Exception:
                continue
            if (
                payload.get("kind")!=self.RECOVERY_KIND
                or payload.get("meaning")!=PAIRED_IMAGE_KNOWLEDGE
                or str(payload.get("status") or "") not in {
                    "applying","restoring","recovery_required",
                }
                or Path(str(payload.get("image_path") or "")).resolve()!=image_path
            ):
                continue
            if manifest.parent.name!=str(payload.get("operation_id") or ""):
                raise RuntimeError(
                    "An interrupted paired correction has an invalid recovery folder."
                )
            source=Path(str(payload.get("source_path") or "")).resolve()
            sidecar=Path(str(payload.get("image_knowledge_path") or "")).resolve()
            source_kind=str(payload.get("source_kind") or "training_txt")
            source_label=(
                "managed Metadata" if source_kind=="managed_metadata"
                else "Training TXT"
            )
            expected_source=(
                image_path if source_kind=="managed_metadata"
                else image_path.with_suffix(".txt").resolve()
            )
            if (
                source!=expected_source
                or sidecar!=image_path.with_suffix(".tags.json").resolve()
            ):
                raise RuntimeError(
                    "An interrupted paired correction points outside its exact image scope."
                )
            source_before=dict(payload.get("before") or {})
            source_after=dict(payload.get("after") or {})
            source_current=_fingerprint(source)
            source_is_before=_same_content(source_current,source_before)
            source_is_after=_same_content(source_current,source_after)
            sidecar_before=dict(payload.get("image_knowledge_before") or {})
            sidecar_current=_fingerprint(sidecar)
            sidecar_is_before=_same_content(sidecar_current,sidecar_before)
            sidecar_is_after=False
            if sidecar.is_file():
                try:
                    raw=json.loads(sidecar.read_text(encoding="utf-8"))
                except Exception:
                    raw=None
                sidecar_is_after=(
                    isinstance(raw,dict)
                    and self._json_fingerprint(raw)==str(
                        payload.get("image_knowledge_after_json_sha256") or ""
                    )
                )
            status=str(payload.get("status") or "")
            restoring=(
                status=="restoring"
                or (
                    status=="recovery_required"
                    and payload.get("recovery_direction")=="restore"
                )
            )
            if not restoring and source_is_after and sidecar_is_after:
                payload["status"]="complete_after_interruption"
            elif (
                (source_is_before or source_is_after)
                and (sidecar_is_before or sidecar_is_after)
                ):
                if source_is_after:
                    source_backup=(
                        manifest.parent/str(payload.get("backup") or "")
                    ).resolve()
                    if source_backup.parent!=manifest.parent.resolve():
                        raise RuntimeError(
                            "The interrupted Training TXT backup path is unsafe."
                        )
                    if not _same_content(_fingerprint(source_backup),source_before):
                        raise RuntimeError(
                            f"The interrupted {source_label} backup failed verification."
                        )
                    _atomic_verified_bytes(
                        source,source_backup.read_bytes(),
                        label=f"Interrupted paired {source_label} rollback",
                    )
                if sidecar_is_after:
                    # The top-level recovery owns an exact sidecar backup, so it
                    # can recover even when interruption happened before the
                    # nested ImageRecord recovery path was recorded.
                    intended_sha=str(
                        payload.get("image_knowledge_after_json_sha256") or ""
                    )
                    if not sidecar.is_file():
                        raise RuntimeError(
                            "Interrupted paired Image Knowledge is unavailable."
                        )
                    current_raw=json.loads(sidecar.read_text(encoding="utf-8"))
                    if self._json_fingerprint(current_raw)!=intended_sha:
                        raise RuntimeError(
                            "Image Knowledge changed after the interrupted correction."
                        )
                    backup=(
                        manifest.parent
                        /str(payload.get("image_knowledge_backup") or "")
                    ).resolve()
                    if bool(payload.get("image_knowledge_existed")):
                        if backup.parent!=manifest.parent.resolve() or not _same_content(
                            _fingerprint(backup),sidecar_before,
                        ):
                            raise RuntimeError(
                                "The interrupted Image Knowledge backup failed verification."
                            )
                        _atomic_verified_bytes(
                            sidecar,backup.read_bytes(),
                            label="Interrupted paired Image Knowledge rollback",
                        )
                    else:
                        sidecar.unlink()
                    if not _same_content(_fingerprint(sidecar),sidecar_before):
                        raise RuntimeError(
                            "Interrupted Image Knowledge rollback failed verification."
                        )
                payload["status"]="restored_after_interruption"
            else:
                payload["status"]="recovery_required"
                _atomic_verified_bytes(manifest,self._recovery_text(payload))
                raise RuntimeError(
                    f"{source_label} or Image Knowledge changed after an interrupted "
                    "paired correction. Automatic recovery was held."
                )
            _atomic_verified_bytes(manifest,self._recovery_text(payload))
            journal_id=str(payload.get("journal_operation_id") or "")
            if journal_id:
                journal=OperationJournal(root/"operation_journal.jsonl")
                if payload["status"]=="complete_after_interruption":
                    journal.finish(
                        journal_id,result={"recovery":str(manifest)},
                    )
                else:
                    journal.finish(
                        journal_id,"undone" if restoring else "failed",
                        message=(
                            "Finished interrupted paired restore before preview."
                            if restoring else
                            "Recovered interrupted paired correction before preview."
                        ),
                    )
            recovered.append(manifest)
        return tuple(recovered)

    @history_preview
    def plan_txt_tag_removal(
        self,image: str|Path,value: str,*,
        write_decision: ExternalWriteDecision|None=None,
        _history_operation=None,
    ) -> SourceCorrectionPlan:
        decision=write_decision or ExternalWriteDecision()
        image_path=Path(image).resolve()
        source_path=image_path.with_suffix(".txt")
        cleaned=str(value or "").strip()
        sidecar=image_path.with_suffix(".tags.json")
        source_error=""; authority_error=""
        if _history_operation is None and decision.backup_enabled and decision.backup_directory is not None:
            try:
                self.recovery_service.recover_pending(image_path)
                self.recover_pending_paired(
                    image_path,decision.backup_directory,
                )
            except Exception as exc:
                authority_error=(
                    "Interrupted paired correction needs recovery: "+str(exc)
                )
        try:
            source_fingerprint=_fingerprint(source_path)
        except OSError as exc:
            source_fingerprint={"exists":True,"unavailable":True}
            source_error=str(exc)
        try:
            authority_fingerprint=_fingerprint(sidecar)
        except OSError as exc:
            authority_fingerprint={"exists":True,"unavailable":True}
            authority_error=str(exc)
        disposition="blocked"; detail="Enter one exact SDXL Tag value."
        original=""; updated=""; original_bytes=b""; updated_bytes=b""
        count=0; confirmed=False; app_owned=False; has_bom=False
        paired_disposition="unsupported"
        paired_detail=(
            "A paired correction is available only when this exact TXT value is "
            "also a confirmed Image Knowledge fact."
        )
        paired_present_values=()
        paired_absent_value=""
        paired_recovery_plan=None
        record=None
        if cleaned and not authority_error:
            try:
                record=ImageRecord(image_path)
                wanted_identity=image_tag_identity(cleaned,self.knowledge)
                confirmed=bool(wanted_identity) and any(
                    image_tag_identity(item,self.knowledge)==wanted_identity
                    for item in record.annotation.all_structured_tags()
                )
            except (OSError,UnicodeError,ValueError) as exc:
                authority_error=str(exc)
        if cleaned and source_error:
            detail=f"Training TXT is unavailable: {source_error}"
        elif cleaned and authority_error:
            detail=f"Image Knowledge is unavailable: {authority_error}"
        elif cleaned and not source_path.exists():
            detail="The adjacent Training TXT does not exist."
        elif cleaned and source_path.is_symlink():
            detail=(
                "The adjacent Training TXT is a symbolic link. Replace it with "
                "a regular file before correcting this exact source."
            )
        elif cleaned:
            try:
                if not (source_path.stat().st_mode&stat.S_IWRITE):
                    raise PermissionError(
                        "The adjacent Training TXT is read only. Make this exact "
                        "file writable before correcting it."
                    )
                original_bytes=source_path.read_bytes()
                has_bom=original_bytes.startswith(codecs.BOM_UTF8)
                body=(
                    original_bytes[len(codecs.BOM_UTF8):]
                    if has_bom else original_bytes
                )
                original=body.decode("utf-8")
                parsed=parse_generated_caption(
                    original,known_tags=self._known_tag,
                    projection_state=(
                        record.annotation.projection_state.get("txt")
                        if record is not None else None
                    ),
                )
                app_owned=parsed.app_owned_projection
                if normalize_tag(cleaned) not in {
                    normalize_tag(item) for item in parsed.sdxl_tags
                }:
                    disposition="current"
                    detail="The exact SDXL Tag is no longer present in Training TXT."
                elif not (
                    parsed.format=="tag_list"
                    or (
                        parsed.app_owned_projection
                        and parsed.sdxl_tags
                        and not parsed.plain_language
                        and not parsed.ambiguous_fragments
                    )
                ):
                    disposition="unsupported"
                    detail=(
                        "This TXT mixes tags with prose or ambiguous content. "
                        "Use a profile-based regeneration preview instead of guessing at an in-place edit."
                    )
                else:
                    updated,count=_remove_exact_tag_list_value(original,cleaned)
                    updated_bytes=(codecs.BOM_UTF8 if has_bom else b"")+updated.encode("utf-8")
                    if not count:
                        disposition="current"
                        detail="The exact SDXL Tag is no longer present in Training TXT."
                    else:
                        disposition="change"
                        detail=(
                            f"Remove {count:,} exact occurrence(s) from Training TXT only. "
                            +(
                                "The separately confirmed Image Knowledge fact will remain unchanged; "
                                "a future caption export may emit it again."
                                if confirmed else
                                "Image Knowledge will remain unchanged."
                            )
                        )
            except (OSError,UnicodeError,ValueError) as exc:
                disposition="blocked"; detail=str(exc)
        if (
            disposition=="change" and decision.backup_enabled
            and decision.backup_directory is None
        ):
            disposition="blocked"
            detail=(
                "Protected source correction requires the shared external-write backup folder."
            )
        if disposition=="change" and confirmed and record is not None:
            if not decision.backup_enabled:
                paired_disposition="blocked"
                paired_detail=(
                    "This two-authority correction requires a verified recovery "
                    "copy of both Training TXT and Image Knowledge. Turn on "
                    "external-write protection, then preview it again."
                )
            else:
                (
                    paired_disposition,paired_detail,paired_present_values,
                    paired_absent_value,paired_recovery_plan,
                )=self._paired_image_knowledge_plan(record,cleaned)
                if paired_disposition=="change":
                    paired_detail=(
                        f"Remove the exact Training TXT value, then {paired_detail}"
                    )
        elif confirmed and disposition!="change":
            paired_disposition=disposition
            paired_detail=(
                "The paired correction is held because this exact Training TXT "
                "source is not currently writable from the frozen preview."
            )
        payload={
            "image":str(image_path),"source":str(source_path),
            "source_kind":"training_txt","value_kind":"tag","value":cleaned,
            "disposition":disposition,"occurrences":count,
            "source_fingerprint":source_fingerprint,
            "authority_fingerprint":authority_fingerprint,
            "updated_sha256":hashlib.sha256(updated_bytes).hexdigest(),
            "write_decision_fingerprint":decision.fingerprint(),
            "paired_disposition":paired_disposition,
            "paired_annotation_sha256":(
                self._json_fingerprint(paired_recovery_plan.materialize())
                if paired_recovery_plan is not None else ""
            ),
        }
        return SourceCorrectionPlan(
            image=image_path,source_path=source_path,source_kind="training_txt",
            value_kind="tag",value=cleaned,disposition=disposition,detail=detail,
            original_text=original,updated_text=updated,
            original_bytes=original_bytes,updated_bytes=updated_bytes,
            occurrence_count=count,confirmed_in_image_knowledge=confirmed,
            app_owned_projection=app_owned,utf8_bom=has_bom,
            choices=self._choices(
                confirmed=confirmed,app_owned=app_owned,
                paired_disposition=paired_disposition,
                paired_detail=paired_detail,
            ),
            source_fingerprint=source_fingerprint,
            authority_fingerprint=authority_fingerprint,
            write_decision=decision,plan_fingerprint=self._plan_fingerprint(payload),
            paired_disposition=paired_disposition,paired_detail=paired_detail,
            paired_present_values=paired_present_values,
            paired_absent_value=paired_absent_value,
            paired_recovery_plan=paired_recovery_plan,
        )

    @history_preview
    def plan_txt_description_removal(
        self,image: str|Path,value: str,*,
        write_decision: ExternalWriteDecision|None=None,
        _history_operation=None,
    ) -> SourceCorrectionPlan:
        """Plan one exact, structurally proven TXT description removal."""

        decision=write_decision or ExternalWriteDecision()
        image_path=Path(image).resolve()
        source_path=image_path.with_suffix(".txt")
        sidecar=image_path.with_suffix(".tags.json")
        cleaned=str(value or "").strip()
        source_error=""; authority_error=""
        if _history_operation is None and decision.backup_enabled and decision.backup_directory is not None:
            try:
                self.recovery_service.recover_pending(image_path)
                self.recover_pending_paired(
                    image_path,decision.backup_directory,
                )
            except Exception as exc:
                authority_error=(
                    "Interrupted paired correction needs recovery: "+str(exc)
                )
        try:
            source_fingerprint=_fingerprint(source_path)
        except OSError as exc:
            source_fingerprint={"exists":True,"unavailable":True}
            source_error=str(exc)
        try:
            authority_fingerprint=_fingerprint(sidecar)
        except OSError as exc:
            authority_fingerprint={"exists":True,"unavailable":True}
            authority_error=str(exc)
        disposition="blocked"
        detail="Enter one exact Plain Language Description."
        original=""; updated=""; original_bytes=b""; updated_bytes=b""
        count=0; confirmed=False; app_owned=False; has_bom=False
        record=None
        if cleaned and not authority_error:
            try:
                record=ImageRecord(image_path)
                wanted=normalize_plain_language_exclusion(cleaned)
                confirmed=bool(wanted) and any(
                    normalize_plain_language_exclusion(item)==wanted
                    for item in record.annotation.plain_language
                )
            except (OSError,UnicodeError,ValueError) as exc:
                authority_error=str(exc)
        if cleaned and source_error:
            detail=f"Training TXT is unavailable: {source_error}"
        elif cleaned and authority_error:
            detail=f"Image Knowledge is unavailable: {authority_error}"
        elif cleaned and not source_path.exists():
            detail="The adjacent Training TXT does not exist."
        elif cleaned and source_path.is_symlink():
            detail=(
                "The adjacent Training TXT is a symbolic link. Replace it with "
                "a regular file before correcting this exact source."
            )
        elif cleaned:
            try:
                if not (source_path.stat().st_mode&stat.S_IWRITE):
                    raise PermissionError(
                        "The adjacent Training TXT is read only. Make this exact "
                        "file writable before correcting it."
                    )
                original_bytes=source_path.read_bytes()
                has_bom=original_bytes.startswith(codecs.BOM_UTF8)
                body=(
                    original_bytes[len(codecs.BOM_UTF8):]
                    if has_bom else original_bytes
                )
                original=body.decode("utf-8")
                state=(
                    record.annotation.projection_state.get("txt")
                    if record is not None else None
                )
                parsed=parse_generated_caption(
                    original,known_tags=self._known_tag,
                    projection_state=state,
                )
                app_owned=parsed.app_owned_projection
                wanted=normalize_plain_language_exclusion(cleaned)
                matches=[
                    item for item in parsed.plain_language
                    if normalize_plain_language_exclusion(item)==wanted
                ]
                if not matches:
                    disposition="current"
                    detail=(
                        "The exact Plain Language Description is no longer "
                        "present in Training TXT."
                    )
                elif len(matches)>1:
                    disposition="unsupported"
                    detail=(
                        "The selected description occurs more than once. Review "
                        "a profile-based regeneration instead of guessing which "
                        "occurrence to remove."
                    )
                elif parsed.app_owned_projection and isinstance(state,dict):
                    updated,count,issue=_remove_app_owned_plain_language_value(
                        original,cleaned,state,
                    )
                    if issue:
                        disposition="unsupported"; detail=issue
                    else:
                        disposition="change"
                elif (
                    parsed.format=="plain_language"
                    and len(parsed.plain_language)==1
                    and not parsed.sdxl_tags
                    and not parsed.ambiguous_fragments
                    and normalize_plain_language_exclusion(original.strip())==wanted
                ):
                    # A single whole-file description has no neighboring source
                    # structure to reinterpret or reformat.
                    updated=""; count=1; disposition="change"
                else:
                    disposition="unsupported"
                    detail=(
                        "This TXT does not expose one safely separable description. "
                        "Review a profile-based regeneration instead of guessing at "
                        "an in-place prose edit."
                    )
                if disposition=="change":
                    updated_bytes=(
                        (codecs.BOM_UTF8 if has_bom else b"")
                        +updated.encode("utf-8")
                    )
                    detail=(
                        "Remove one exact Plain Language Description from Training "
                        "TXT only. "
                        +(
                            "The separately confirmed Image Knowledge description "
                            "will remain unchanged and a future caption export may "
                            "emit it again."
                            if confirmed else
                            "Image Knowledge will remain unchanged."
                        )
                    )
            except (OSError,UnicodeError,ValueError) as exc:
                disposition="blocked"; detail=str(exc)
        if (
            disposition=="change" and decision.backup_enabled
            and decision.backup_directory is None
        ):
            disposition="blocked"
            detail=(
                "Protected source correction requires the shared external-write "
                "backup folder."
            )
        paired_disposition="unsupported"; paired_present_values=()
        paired_absent_value=""; paired_recovery_plan=None
        paired_detail=(
            "This value is not a confirmed Image Knowledge description. Remove "
            "it from Training TXT only, or review the image description first."
        )
        if disposition=="change" and confirmed and record is not None:
            if not decision.backup_enabled:
                paired_disposition="blocked"
                paired_detail=(
                    "This two-authority correction requires a verified recovery "
                    "copy of both Training TXT and Image Knowledge. Turn on "
                    "external-write protection, then preview it again."
                )
            else:
                (
                    paired_disposition,paired_detail,paired_present_values,
                    paired_absent_value,paired_recovery_plan,
                )=self._paired_image_knowledge_plan(
                    record,cleaned,value_kind="description",
                )
                if paired_disposition=="change":
                    paired_detail=(
                        "Remove the exact Training TXT description, then "
                        +paired_detail
                    )
        elif confirmed and disposition!="change":
            paired_disposition=disposition
            paired_detail=(
                "The paired correction is held because this exact Training TXT "
                "source is not currently writable from the frozen preview."
            )
        choices=(
            SourceCorrectionChoice(
                SOURCE_ONLY,"Remove only from this Training TXT",detail,
                "dataset.source_correction.preview",
                "available" if disposition=="change" else "unavailable",
            ),
            SourceCorrectionChoice(
                PAIRED_IMAGE_KNOWLEDGE,
                "The description is false for this image",paired_detail,
                "dataset.source_correction.preview",
                "available" if paired_disposition=="change" else "unavailable",
            ),
        )
        payload={
            "image":str(image_path),"source":str(source_path),
            "source_kind":"training_txt","value_kind":"description",
            "value":cleaned,"disposition":disposition,"occurrences":count,
            "source_fingerprint":source_fingerprint,
            "authority_fingerprint":authority_fingerprint,
            "updated_sha256":hashlib.sha256(updated_bytes).hexdigest(),
            "write_decision_fingerprint":decision.fingerprint(),
            "paired_disposition":paired_disposition,
            "paired_annotation_sha256":(
                self._json_fingerprint(paired_recovery_plan.materialize())
                if paired_recovery_plan is not None else ""
            ),
        }
        return SourceCorrectionPlan(
            image=image_path,source_path=source_path,source_kind="training_txt",
            value_kind="description",value=cleaned,disposition=disposition,
            detail=detail,original_text=original,updated_text=updated,
            original_bytes=original_bytes,updated_bytes=updated_bytes,
            occurrence_count=count,confirmed_in_image_knowledge=confirmed,
            app_owned_projection=app_owned,utf8_bom=has_bom,choices=choices,
            source_fingerprint=source_fingerprint,
            authority_fingerprint=authority_fingerprint,
            write_decision=decision,plan_fingerprint=self._plan_fingerprint(payload),
            paired_disposition=paired_disposition,paired_detail=paired_detail,
            paired_present_values=paired_present_values,
            paired_absent_value=paired_absent_value,
            paired_recovery_plan=paired_recovery_plan,
        )

    def _validate_plan(self,plan: SourceCorrectionPlan) -> bool:
        if plan.source_kind!="training_txt":
            return False
        planner=(
            self.plan_txt_description_removal
            if plan.value_kind=="description" else self.plan_txt_tag_removal
        )
        current=planner(plan.image,plan.value,write_decision=plan.write_decision)
        return (
            current.plan_fingerprint==plan.plan_fingerprint
            and current.updated_text==plan.updated_text
            and current.disposition==plan.disposition
        )

    @staticmethod
    def _recovery_text(payload: dict[str,object]) -> bytes:
        return (json.dumps(payload,indent=2,ensure_ascii=False)+"\n").encode("utf-8")

    @history_apply
    def apply(
        self,plan: SourceCorrectionPlan,*,
        execution: ExternalWriteExecution|None=None,
        meaning: str=SOURCE_ONLY,
    ) -> SourceCorrectionResult:
        if meaning==PAIRED_IMAGE_KNOWLEDGE:
            return self.apply_paired(plan,execution=execution)
        if meaning!=SOURCE_ONLY:
            return SourceCorrectionResult(
                "blocked","This source-correction meaning is unsupported.",
                plan.source_path,meaning=meaning,
                source_kind=plan.source_kind,value_kind=plan.value_kind,
            )
        if not plan.will_write:
            return SourceCorrectionResult(
                plan.disposition,plan.detail,plan.source_path,
                source_kind=plan.source_kind,value_kind=plan.value_kind,
            )
        execution=execution or plan.write_decision.execution()
        validation_issue=external_write_validation_issue(
            plan.write_decision,execution,
        )
        if validation_issue in {"stale_decision","stale_policy"}:
            return SourceCorrectionResult(
                "stale",(
                    "The frozen external-write decision no longer matches this preview."
                    if validation_issue=="stale_decision" else
                    "The shared external-write policy changed after preview."
                ),
                plan.source_path,
                source_kind=plan.source_kind,value_kind=plan.value_kind,
            )
        if validation_issue=="privacy_confirmation_required":
            return SourceCorrectionResult(
                "blocked","This exact Local Only boundary crossing was not confirmed.",
                plan.source_path,source_kind=plan.source_kind,
                value_kind=plan.value_kind,
            )
        if not self._validate_plan(plan):
            return SourceCorrectionResult(
                "stale","Training TXT or Image Knowledge changed after preview.",
                plan.source_path,source_kind=plan.source_kind,
                value_kind=plan.value_kind,
            )
        operation_id=f"source_correction_{uuid4().hex}"
        recovery_path=None; recovery=None; journal=None; journal_id=""
        directory: Path|None=None
        before=plan.source_fingerprint
        after_expected=_content_fingerprint(plan.updated_bytes)
        if plan.backup_enabled:
            try:
                root=self._backup_root(plan.backup_directory)
                directory=root/operation_id
                directory.mkdir(parents=True,exist_ok=False)
                backup=directory/"training.txt.before"
                _copy_verified(
                    plan.source_path,backup,expected=before,
                    label="Training TXT backup",
                )
                recovery_path=directory/"recovery.json"
                recovery={
                    "schema_version":1,"kind":self.RECOVERY_KIND,
                    "status":"applying","operation_id":operation_id,
                    "plan_fingerprint":plan.plan_fingerprint,
                    "source_path":str(plan.source_path),
                    "backup":"training.txt.before","before":before,
                    "after":after_expected,
                }
                _atomic_verified_bytes(
                    recovery_path,self._recovery_text(recovery),
                )
                journal_path=root/"operation_journal.jsonl"
                if journal_path.is_symlink():
                    raise RuntimeError(
                        "The source-correction journal cannot be a symbolic link."
                    )
                journal=OperationJournal(journal_path)
                journal_id=journal.begin(
                    "source_correction",expected=1,
                    reason="Remove one exact SDXL Tag from adjacent Training TXT",
                    metadata={
                        "recovery":str(recovery_path),
                        "plan_fingerprint":plan.plan_fingerprint,
                        "source_kind":plan.source_kind,
                        "value_kind":plan.value_kind,
                    },
                )
                recovery["journal_operation_id"]=journal_id
                _atomic_verified_bytes(
                    recovery_path,self._recovery_text(recovery),
                )
            except Exception as exc:
                if journal is not None and journal_id:
                    try:
                        journal.finish(journal_id,"failed",message=str(exc))
                    except Exception:
                        pass
                if directory is not None:
                    shutil.rmtree(directory,ignore_errors=True)
                return SourceCorrectionResult(
                    "failed",str(exc),plan.source_path,operation_id,
                    source_kind=plan.source_kind,value_kind=plan.value_kind,
                )
        try:
            if journal is not None:
                journal.append_change(
                    journal_id,item=str(plan.source_path),before=before,
                    after=after_expected,
                    reason="Verified exact Training TXT source correction",
                )
            _atomic_verified_bytes(
                plan.source_path,plan.updated_bytes,label="Training TXT correction",
            )
            actual_after=_fingerprint(plan.source_path)
            if recovery is not None:
                recovery["status"]="complete"; recovery["after"]=actual_after
                _atomic_verified_bytes(
                    recovery_path,self._recovery_text(recovery),
                )
            if journal is not None:
                journal.finish(
                    journal_id,result={"recovery":str(recovery_path)},
                )
            return SourceCorrectionResult(
                "complete",plan.detail,plan.source_path,operation_id,
                recovery_path,source_kind=plan.source_kind,
                value_kind=plan.value_kind,
            )
        except Exception as exc:
            recovery_error=""
            try:
                if recovery_path is not None:
                    backup=recovery_path.parent/str(recovery["backup"])
                    original_bytes=backup.read_bytes()
                else:
                    original_bytes=plan.original_bytes
                _atomic_verified_bytes(
                    plan.source_path,original_bytes,label="Training TXT rollback",
                )
                if recovery_path is not None:
                    recovery["status"]="restored_after_failure"
                    _atomic_verified_bytes(
                        recovery_path,self._recovery_text(recovery),
                    )
            except Exception as restore_exc:
                recovery_error=f" Automatic restore failed: {restore_exc}"
            if journal is not None:
                try:
                    journal.finish(
                        journal_id,"failed",message=str(exc)+recovery_error,
                    )
                except Exception as journal_exc:
                    recovery_error+=(
                        f" Journal failure could not be recorded: {journal_exc}"
                    )
            return SourceCorrectionResult(
                "failed",str(exc)+recovery_error,plan.source_path,
                operation_id,recovery_path,
                source_kind=plan.source_kind,value_kind=plan.value_kind,
            )

    def _paired_intended_annotation(
        self,plan: SourceCorrectionPlan,
    ) -> dict[str,object]:
        if plan.paired_recovery_plan is None:
            raise RuntimeError("No paired Image Knowledge change was staged.")
        intended=plan.paired_recovery_plan.materialize()
        if not isinstance(intended,dict):
            raise RuntimeError("The paired Image Knowledge result is invalid.")
        return intended

    def _sidecar_matches_intended(
        self,path: Path,intended: dict[str,object],
    ) -> bool:
        if not path.is_file():
            return False
        try:
            raw=json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        return isinstance(raw,dict) and (
            self._json_fingerprint(raw)==self._json_fingerprint(intended)
        )

    def _paired_semantic_matches(
        self,record: ImageRecord,plan: SourceCorrectionPlan,
    ) -> bool:
        if plan.value_kind=="description":
            wanted=normalize_plain_language_exclusion(plan.value)
            present=any(
                normalize_plain_language_exclusion(item)==wanted
                for item in record.annotation.plain_language
            )
            rejected=(
                wanted in ImageFindingDecisionService(
                    record.annotation,knowledge=self.knowledge,
                ).ignored_description_keys()
            )
            return bool(wanted) and not present and rejected
        wanted=image_tag_identity(plan.value,self.knowledge)
        return bool(wanted) and not any(
            image_tag_identity(item,self.knowledge)==wanted
            for item in record.annotation.all_structured_tags()
        ) and any(
            image_tag_identity(item,self.knowledge)==wanted
            for item in record.annotation.confirmed_absent
        )

    def _restore_paired_sidecar(
        self,plan: SourceCorrectionPlan,recovery: dict[str,object],
    ) -> None:
        sidecar=plan.image.with_suffix(".tags.json")
        before=dict(recovery.get("image_knowledge_before") or {})
        intended=self._paired_intended_annotation(plan)
        current=_fingerprint(sidecar)
        if _same_content(current,before):
            return
        if not self._sidecar_matches_intended(sidecar,intended):
            raise RuntimeError(
                "Image Knowledge changed again; paired rollback did not overwrite it."
            )
        if bool(recovery.get("image_knowledge_existed")):
            backup=(
                Path(str(recovery["manifest_path"])).parent
                /str(recovery.get("image_knowledge_backup") or "")
            ).resolve()
            if backup.parent!=Path(str(recovery["manifest_path"])).parent.resolve():
                raise RuntimeError("The paired Image Knowledge backup path is unsafe.")
            if not _same_content(_fingerprint(backup),before):
                raise RuntimeError("The paired Image Knowledge backup failed verification.")
            _atomic_verified_bytes(
                sidecar,backup.read_bytes(),label="Paired Image Knowledge rollback",
            )
        elif sidecar.exists():
            sidecar.unlink()
        if not _same_content(_fingerprint(sidecar),before):
            raise RuntimeError("Paired Image Knowledge rollback failed verification.")

    @history_apply
    def apply_paired(
        self,plan: SourceCorrectionPlan,*,
        execution: ExternalWriteExecution|None=None,
    ) -> SourceCorrectionResult:
        """Apply one protected TXT removal and confirmed-absence decision together."""

        if plan.paired_disposition!="change" or plan.paired_recovery_plan is None:
            return SourceCorrectionResult(
                plan.paired_disposition,plan.paired_detail,plan.source_path,
                meaning=PAIRED_IMAGE_KNOWLEDGE,
                source_kind=plan.source_kind,value_kind=plan.value_kind,
            )
        if not plan.backup_enabled or plan.backup_directory is None:
            return SourceCorrectionResult(
                "blocked","A paired correction requires verified recovery for "
                "both Training TXT and Image Knowledge.",plan.source_path,
                meaning=PAIRED_IMAGE_KNOWLEDGE,
                source_kind=plan.source_kind,value_kind=plan.value_kind,
            )
        execution=execution or plan.write_decision.execution()
        validation_issue=external_write_validation_issue(
            plan.write_decision,execution,
        )
        if validation_issue in {"stale_decision","stale_policy"}:
            return SourceCorrectionResult(
                "stale",(
                    "The frozen external-write decision no longer matches this preview."
                    if validation_issue=="stale_decision" else
                    "The shared external-write policy changed after preview."
                ),plan.source_path,meaning=PAIRED_IMAGE_KNOWLEDGE,
                source_kind=plan.source_kind,value_kind=plan.value_kind,
            )
        if validation_issue=="privacy_confirmation_required":
            return SourceCorrectionResult(
                "blocked","This exact Local Only boundary crossing was not confirmed.",
                plan.source_path,meaning=PAIRED_IMAGE_KNOWLEDGE,
                source_kind=plan.source_kind,value_kind=plan.value_kind,
            )
        if not self._validate_plan(plan):
            return SourceCorrectionResult(
                "stale","Training TXT or Image Knowledge changed after preview.",
                plan.source_path,meaning=PAIRED_IMAGE_KNOWLEDGE,
                source_kind=plan.source_kind,value_kind=plan.value_kind,
            )

        operation_id=f"source_correction_{uuid4().hex}"
        recovery_path=None; recovery=None; journal=None; journal_id=""
        image_recovery_manifest=None
        directory: Path|None=None
        source_before=plan.source_fingerprint
        source_after=_content_fingerprint(plan.updated_bytes)
        sidecar=plan.image.with_suffix(".tags.json")
        sidecar_before=plan.authority_fingerprint
        intended=self._paired_intended_annotation(plan)
        intended_fingerprint=self._json_fingerprint(intended)
        try:
            root=self._backup_root(plan.backup_directory)
            directory=root/operation_id
            directory.mkdir(parents=True,exist_ok=False)
            source_backup=directory/"training.txt.before"
            _copy_verified(
                plan.source_path,source_backup,expected=source_before,
                label="Training TXT backup",
            )
            sidecar_backup=directory/"image-knowledge.tags.json.before"
            sidecar_existed=bool(sidecar_before.get("exists"))
            if sidecar_existed:
                _copy_verified(
                    sidecar,sidecar_backup,expected=sidecar_before,
                    label="Image Knowledge backup",
                )
            recovery_path=directory/"recovery.json"
            recovery={
                "schema_version":2,"kind":self.RECOVERY_KIND,
                "status":"applying","operation_id":operation_id,
                "plan_fingerprint":plan.plan_fingerprint,
                "meaning":PAIRED_IMAGE_KNOWLEDGE,
                "image_path":str(plan.image),
                "source_path":str(plan.source_path),
                "backup":"training.txt.before","before":source_before,
                "after":source_after,
                "image_knowledge_path":str(sidecar),
                "image_knowledge_backup":sidecar_backup.name,
                "image_knowledge_existed":sidecar_existed,
                "image_knowledge_before":sidecar_before,
                "image_knowledge_after_json_sha256":intended_fingerprint,
            }
            _atomic_verified_bytes(
                recovery_path,self._recovery_text(recovery),
            )
            journal_path=root/"operation_journal.jsonl"
            if journal_path.is_symlink():
                raise RuntimeError(
                    "The source-correction journal cannot be a symbolic link."
                )
            journal=OperationJournal(journal_path)
            journal_id=journal.begin(
                "source_correction",expected=2,
                reason="Correct one false image fact and its Training TXT value",
                metadata={
                    "recovery":str(recovery_path),
                    "plan_fingerprint":plan.plan_fingerprint,
                    "source_kind":plan.source_kind,
                    "value_kind":plan.value_kind,
                    "meaning":PAIRED_IMAGE_KNOWLEDGE,
                },
            )
            recovery["journal_operation_id"]=journal_id
            recovery["manifest_path"]=str(recovery_path)
            _atomic_verified_bytes(
                recovery_path,self._recovery_text(recovery),
            )
        except Exception as exc:
            if journal is not None and journal_id:
                try:
                    journal.finish(journal_id,"failed",message=str(exc))
                except Exception:
                    pass
            if directory is not None:
                shutil.rmtree(directory,ignore_errors=True)
            return SourceCorrectionResult(
                "failed",str(exc),plan.source_path,operation_id,
                meaning=PAIRED_IMAGE_KNOWLEDGE,
                source_kind=plan.source_kind,value_kind=plan.value_kind,
            )

        try:
            if journal is not None:
                journal.append_change(
                    journal_id,item=str(sidecar),before=sidecar_before,
                    after={"json_sha256":intended_fingerprint},
                    reason="Record exact confirmed absence in Image Knowledge",
                )
                journal.append_change(
                    journal_id,item=str(plan.source_path),before=source_before,
                    after=source_after,
                    reason="Remove the exact false value from Training TXT",
                )
            image_result=self.recovery_service.apply(
                plan.paired_recovery_plan,knowledge=self.knowledge,
            )
            image_recovery_manifest=image_result.recovery_manifest
            saved=image_result.record
            if not self._paired_semantic_matches(saved,plan):
                raise RuntimeError(
                    "The paired Image Knowledge correction failed semantic read-back verification."
                )
            recovery["image_knowledge_recovery"]=str(image_recovery_manifest)
            recovery["image_knowledge_after"]=_fingerprint(sidecar)
            _atomic_verified_bytes(
                recovery_path,self._recovery_text(recovery),
            )
            if not _same_content(_fingerprint(plan.source_path),source_before):
                raise RuntimeError(
                    "Training TXT changed again before the paired write. The "
                    "newer source was not overwritten."
                )
            if (
                not _same_content(
                    _fingerprint(sidecar),dict(recovery["image_knowledge_after"]),
                )
                or not self._sidecar_matches_intended(sidecar,intended)
            ):
                raise RuntimeError(
                    "Image Knowledge changed again before the paired TXT write. "
                    "The newer record was not overwritten."
                )
            _atomic_verified_bytes(
                plan.source_path,plan.updated_bytes,
                label="Paired Training TXT correction",
            )
            if (
                not _same_content(
                    _fingerprint(sidecar),dict(recovery["image_knowledge_after"]),
                )
                or not self._sidecar_matches_intended(sidecar,intended)
            ):
                raise RuntimeError(
                    "Image Knowledge changed again after the paired TXT write. "
                    "The transaction was not marked complete."
                )
            recovery["status"]="complete"
            recovery["after"]=_fingerprint(plan.source_path)
            _atomic_verified_bytes(
                recovery_path,self._recovery_text(recovery),
            )
            if journal is not None:
                journal.finish(
                    journal_id,result={"recovery":str(recovery_path)},
                )
            return SourceCorrectionResult(
                "complete",plan.paired_detail,plan.source_path,operation_id,
                recovery_path,PAIRED_IMAGE_KNOWLEDGE,image_recovery_manifest,
                plan.source_kind,plan.value_kind,
            )
        except Exception as exc:
            rollback_errors=[]
            try:
                source_backup=recovery_path.parent/str(recovery["backup"])
                current=_fingerprint(plan.source_path)
                if not (
                    _same_content(current,source_before)
                    or _same_content(current,source_after)
                ):
                    raise RuntimeError(
                        "Training TXT changed again; paired rollback did not overwrite it."
                    )
                _atomic_verified_bytes(
                    plan.source_path,source_backup.read_bytes(),
                    label="Paired Training TXT rollback",
                )
            except Exception as restore_exc:
                rollback_errors.append(str(restore_exc))
            try:
                recovery["manifest_path"]=str(recovery_path)
                self._restore_paired_sidecar(plan,recovery)
            except Exception as restore_exc:
                rollback_errors.append(str(restore_exc))
            recovery["status"]=(
                "restored_after_failure" if not rollback_errors
                else "recovery_required"
            )
            recovery["rollback_errors"]=rollback_errors
            try:
                _atomic_verified_bytes(
                    recovery_path,self._recovery_text(recovery),
                )
            except Exception as restore_exc:
                rollback_errors.append(
                    f"Recovery status could not be recorded: {restore_exc}"
                )
            detail=str(exc)
            if rollback_errors:
                detail+=" Automatic paired recovery needs attention: "+"; ".join(
                    rollback_errors
                )
            else:
                detail+=" Both Training TXT and Image Knowledge were restored."
            if journal is not None:
                try:
                    journal.finish(journal_id,"failed",message=detail)
                except Exception as journal_exc:
                    detail+=f" Journal failure could not be recorded: {journal_exc}"
            return SourceCorrectionResult(
                "failed",detail,plan.source_path,operation_id,recovery_path,
                PAIRED_IMAGE_KNOWLEDGE,image_recovery_manifest,
                plan.source_kind,plan.value_kind,
            )

    @history_restore
    def restore_paired(
        self,plan: SourceCorrectionPlan,manifest: str|Path,
    ) -> SourceCorrectionRestoreResult:
        path=Path(manifest).resolve()
        restore_started=False
        source_label=(
            "managed Metadata"
            if plan.source_kind=="managed_metadata" else "Training TXT"
        )
        try:
            expected_root=self._backup_root(plan.backup_directory)
            if path.parent.parent!=expected_root:
                raise RuntimeError(
                    "The recovery manifest is outside the configured protected backup root."
                )
            payload=json.loads(path.read_text(encoding="utf-8"))
            if (
                payload.get("kind")!=self.RECOVERY_KIND
                or payload.get("meaning")!=PAIRED_IMAGE_KNOWLEDGE
            ):
                raise RuntimeError("This is not a paired source-correction recovery.")
            if payload.get("plan_fingerprint")!=plan.plan_fingerprint:
                raise RuntimeError("The recovery does not match this frozen correction plan.")
            if path.parent.name!=str(payload.get("operation_id") or ""):
                raise RuntimeError("The recovery operation folder does not match its manifest.")
            source=Path(str(payload.get("source_path") or "")).resolve()
            sidecar=Path(str(payload.get("image_knowledge_path") or "")).resolve()
            if source!=plan.source_path.resolve() or sidecar!=plan.image.with_suffix(
                ".tags.json",
            ).resolve():
                raise RuntimeError("The paired recovery targets are outside frozen scope.")
            status=str(payload.get("status") or "")
            if status.startswith("restored"):
                return SourceCorrectionRestoreResult(
                    "already_restored",f"{source_label} and Image Knowledge were "
                    "already restored.",source,PAIRED_IMAGE_KNOWLEDGE,
                )
            if status in {"restoring","recovery_required"} and (
                status=="restoring"
                or payload.get("recovery_direction")=="restore"
            ):
                try:
                    self.recover_pending_paired(plan.image,plan.backup_directory)
                except RuntimeError as exc:
                    return SourceCorrectionRestoreResult(
                        "held",str(exc),source,PAIRED_IMAGE_KNOWLEDGE,
                    )
                payload=json.loads(path.read_text(encoding="utf-8"))
                if str(payload.get("status") or "").startswith("restored"):
                    return SourceCorrectionRestoreResult(
                        "restored","Finished the interrupted restore of the exact "
                        f"pre-correction {source_label} and Image Knowledge.",source,
                        PAIRED_IMAGE_KNOWLEDGE,
                    )
                status=str(payload.get("status") or "")
            if status not in {"complete","complete_after_interruption"}:
                raise RuntimeError("The paired correction did not reach a restorable state.")
            if not _same_content(_fingerprint(source),dict(payload["after"])):
                return SourceCorrectionRestoreResult(
                    "held",f"{source_label} changed again; paired restore was held.",
                    source,PAIRED_IMAGE_KNOWLEDGE,
                )
            intended=self._paired_intended_annotation(plan)
            if not self._sidecar_matches_intended(sidecar,intended):
                return SourceCorrectionRestoreResult(
                    "held","Image Knowledge changed again; paired restore was held.",
                    source,PAIRED_IMAGE_KNOWLEDGE,
                )
            source_backup=(path.parent/str(payload.get("backup") or "")).resolve()
            if source_backup.parent!=path.parent.resolve():
                raise RuntimeError("The source-correction backup path is unsafe.")
            before=dict(payload["before"])
            if not _same_content(_fingerprint(source_backup),before):
                raise RuntimeError("The source-correction backup failed verification.")
            sidecar_before=dict(payload.get("image_knowledge_before") or {})
            sidecar_existed=bool(payload.get("image_knowledge_existed"))
            if sidecar_existed!=bool(sidecar_before.get("exists")):
                raise RuntimeError(
                    "The paired Image Knowledge recovery state is inconsistent."
                )
            if sidecar_existed:
                sidecar_backup=(
                    path.parent/str(payload.get("image_knowledge_backup") or "")
                ).resolve()
                if sidecar_backup.parent!=path.parent.resolve():
                    raise RuntimeError(
                        "The paired Image Knowledge backup path is unsafe."
                    )
                if not _same_content(_fingerprint(sidecar_backup),sidecar_before):
                    raise RuntimeError(
                        "The paired Image Knowledge backup failed verification."
                    )

            # Persist the inverse direction before either authority changes. A
            # fresh preview can then finish every valid before/after combination
            # after a process interruption without guessing which state won.
            payload["status"]="restoring"
            payload["recovery_direction"]="restore"
            payload["manifest_path"]=str(path)
            restore_started=True
            _atomic_verified_bytes(path,self._recovery_text(payload))
            _atomic_verified_bytes(
                source,source_backup.read_bytes(),label=f"Paired {source_label} restore",
            )
            self._restore_paired_sidecar(plan,payload)
            if not _same_content(_fingerprint(source),before):
                raise RuntimeError(f"Restored {source_label} failed verification.")
            if not _same_content(_fingerprint(sidecar),sidecar_before):
                raise RuntimeError("Restored Image Knowledge failed verification.")
            payload["status"]="restored"
            payload.pop("recovery_direction",None)
            _atomic_verified_bytes(path,self._recovery_text(payload))
            journal=OperationJournal(expected_root/"operation_journal.jsonl")
            journal_id=str(payload.get("journal_operation_id") or "")
            if journal_id:
                journal.finish(
                    journal_id,"undone",
                    message=f"Restored exact {source_label} and Image Knowledge",
                )
            return SourceCorrectionRestoreResult(
                "restored",f"Restored the exact pre-correction {source_label} and "
                "Image Knowledge.",source,PAIRED_IMAGE_KNOWLEDGE,
            )
        except Exception as exc:
            if restore_started:
                try:
                    self.recover_pending_paired(plan.image,plan.backup_directory)
                    latest=json.loads(path.read_text(encoding="utf-8"))
                    if str(latest.get("status") or "").startswith("restored"):
                        return SourceCorrectionRestoreResult(
                            "restored","Restored the exact pre-correction "
                            f"{source_label} and Image Knowledge after recovering an interrupted "
                            f"inverse write: {exc}",plan.source_path,
                            PAIRED_IMAGE_KNOWLEDGE,
                        )
                except RuntimeError as recovery_exc:
                    return SourceCorrectionRestoreResult(
                        "held",f"{exc} Automatic paired restore recovery was held: "
                        f"{recovery_exc}",plan.source_path,
                        PAIRED_IMAGE_KNOWLEDGE,
                    )
                except Exception as recovery_exc:
                    return SourceCorrectionRestoreResult(
                        "failed",f"{exc} Automatic paired restore recovery failed: "
                        f"{recovery_exc}",plan.source_path,
                        PAIRED_IMAGE_KNOWLEDGE,
                    )
            return SourceCorrectionRestoreResult(
                "failed",str(exc),plan.source_path,PAIRED_IMAGE_KNOWLEDGE,
            )

    @history_restore
    def restore(
        self,plan: SourceCorrectionPlan,manifest: str|Path,
    ) -> SourceCorrectionRestoreResult:
        path=Path(manifest).resolve()
        try:
            expected_root=self._backup_root(plan.backup_directory)
            if path.parent.parent!=expected_root:
                raise RuntimeError(
                    "The recovery manifest is outside the configured protected backup root."
                )
            payload=json.loads(path.read_text(encoding="utf-8"))
            if payload.get("kind")!=self.RECOVERY_KIND:
                raise RuntimeError("This is not a source-correction recovery manifest.")
            if payload.get("meaning") not in {None,"",SOURCE_ONLY}:
                raise RuntimeError(
                    "A paired recovery must restore Training TXT and Image "
                    "Knowledge together."
                )
            if payload.get("plan_fingerprint")!=plan.plan_fingerprint:
                raise RuntimeError("The recovery does not match this frozen correction plan.")
            if path.parent.name!=str(payload.get("operation_id") or ""):
                raise RuntimeError("The recovery operation folder does not match its manifest.")
            target=Path(str(payload.get("source_path") or "")).resolve()
            if target!=plan.source_path.resolve():
                raise RuntimeError("The recovery target is outside the frozen source scope.")
            if payload.get("status")=="restored":
                return SourceCorrectionRestoreResult(
                    "already_restored","Training TXT was already restored.",target,
                )
            if payload.get("status")!="complete":
                raise RuntimeError("The source correction did not reach a restorable state.")
            if not _same_content(_fingerprint(target),dict(payload["after"])):
                return SourceCorrectionRestoreResult(
                    "held","Training TXT changed again; restore was held.",target,
                )
            backup=(path.parent/str(payload.get("backup") or "")).resolve()
            if backup.parent!=path.parent.resolve():
                raise RuntimeError("The source-correction backup path is unsafe.")
            before=dict(payload["before"])
            if not _same_content(_fingerprint(backup),before):
                raise RuntimeError("The source-correction backup failed verification.")
            _atomic_verified_bytes(
                target,backup.read_bytes(),label="Training TXT restore",
            )
            if not _same_content(_fingerprint(target),before):
                raise RuntimeError("Restored Training TXT failed verification.")
            payload["status"]="restored"
            _atomic_verified_bytes(path,self._recovery_text(payload))
            journal=OperationJournal(
                self._backup_root(plan.backup_directory)/"operation_journal.jsonl"
            )
            journal_id=str(payload.get("journal_operation_id") or "")
            if journal_id:
                journal.finish(journal_id,"undone",message="Restored exact Training TXT bytes")
            return SourceCorrectionRestoreResult(
                "restored","Restored the exact pre-correction Training TXT.",target,
            )
        except Exception as exc:
            return SourceCorrectionRestoreResult("failed",str(exc),plan.source_path)
