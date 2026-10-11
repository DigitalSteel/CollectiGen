from __future__ import annotations

import copy
import hashlib
import inspect
import json
from contextlib import contextmanager
from dataclasses import dataclass,field
from datetime import datetime,timezone
from pathlib import Path
from typing import Callable,Iterable

from .annotation import GenerationRecord,ImageAnnotation,SCHEMA_VERSION
from .automation_templates import concrete_sdxl_tags, has_automation_placeholder
from .batch_preparation import BatchPreparationService
from .caption_import import parse_generated_caption
from .confirmed_tag_identity import ConfirmedTagIdentityService
from .external_evidence_import import ExternalEvidenceImportCounts
from .image_record import ImageRecord,_atomic_write_text
from .history_versions import HistoryError
from .history_configuration import HistoryConfigurationError
from .image_knowledge_history import HistoryWriteInterrupted
from .metadata_sync import MetadataSyncService
from .operation_journal import OperationJournal
from .tag_collection import TagCollection,normalize_tag


MANAGED_SOURCE_NAMES=("TXT caption","Image metadata")
SOURCE_FINGERPRINT_VERSION=1
MANAGED_SCAN_CONTRACT_VERSION=2
DERIVED_RULE_RESULT_VERSION=1


def _signature(path: Path) -> dict[str,object]:
    try:
        stat=path.stat()
    except OSError:
        return {"exists":False}
    return {"exists":True,"size":stat.st_size,"mtime_ns":stat.st_mtime_ns}


def _valid_sha256(value: object) -> bool:
    return isinstance(value,str) and len(value)==64 and all(char in "0123456789abcdefABCDEF" for char in value)


def _signature_matches(fingerprint: object,path: Path) -> bool:
    if not isinstance(fingerprint,dict):
        return False
    current=_signature(path)
    return all(fingerprint.get(key)==value for key,value in current.items()) and all(
        key in current for key in ("exists",)
    )


def _stable_file_fingerprint(path: Path,expected: dict[str,object]|None=None) -> dict[str,object]:
    before=_signature(path)
    if not before.get("exists"):
        raise FileNotFoundError(path)
    if expected is not None and before!=expected:
        raise RuntimeError(f"{path.name} changed while it was being initialized.")
    digest=hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda:stream.read(1024*1024),b""):
            digest.update(block)
    after=_signature(path)
    if after!=before:
        raise RuntimeError(f"{path.name} changed while it was being initialized.")
    return {**after,"sha256":digest.hexdigest()}


def _system_knowledge_fingerprint(knowledge) -> str:
    if knowledge is None:
        return ""
    tags=getattr(knowledge,"tags",None)
    if isinstance(tags,dict):
        effective_aliases=getattr(knowledge,"effective_aliases",None)
        records=[]
        for stable_id,record in sorted(tags.items(),key=lambda item:str(item[0])):
            aliases=(
                effective_aliases(record) if callable(effective_aliases)
                else getattr(record,"aliases",())
            )
            records.append({
                "stable_id":str(stable_id),
                "display":str(getattr(record,"display","")),
                "aliases":list(aliases),
            })
        archives=[]
        for stable_id,archive in sorted(
            getattr(knowledge,"removed_tag_archives",{}).items(),
            key=lambda item:str(item[0]),
        ):
            serializer=getattr(archive,"to_dict",None)
            archives.append({
                "stable_id":str(stable_id),
                "archive":serializer() if callable(serializer) else dict(vars(archive)),
            })
        payload={
            "records":records,
            "removed_tag_ids":sorted(
                str(value) for value in getattr(knowledge,"removed_tag_ids",set())
            ),
            "merged_tag_ids":dict(sorted(
                (str(key),str(value))
                for key,value in getattr(knowledge,"merged_tag_ids",{}).items()
            )),
            "removed_tag_archives":archives,
            "represented_alias_keys":sorted(
                str(value)
                for value in getattr(knowledge,"_represented_alias_keys",set())
            ),
        }
    else:
        payload={
            "type":f"{type(knowledge).__module__}.{type(knowledge).__qualname__}",
        }
    encoded=json.dumps(
        payload,sort_keys=True,separators=(",",":"),ensure_ascii=False,default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _rules_fingerprint(engine,knowledge=None) -> str:
    from .conflict_policy import conflict_rule_cache_payload
    conflicts=getattr(engine,"conflicts",None)
    payload={
        "transform_rules":getattr(engine,"rules",{}),
        "conflict_rules":[conflict_rule_cache_payload(rule) for rule in getattr(conflicts,"rules",[])],
        "system_knowledge_fingerprint":_system_knowledge_fingerprint(
            knowledge if knowledge is not None else getattr(engine,"knowledge",None)
        ),
    }
    encoded=json.dumps(payload,sort_keys=True,separators=(",",":"),ensure_ascii=False,default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class ImageAuthorityInitializationIssue:
    image: Path
    code: str
    message: str
    recovery: str
    blocking: bool = True


@dataclass
class ImageAuthorityInitializationResult:
    total: int = 0
    processed: int = 0
    created: int = 0
    refreshed: int = 0
    reused: int = 0
    cache_refreshed: int = 0
    automated: int = 0
    conflicts: int = 0
    existing_legacy: int = 0
    cancelled: bool = False
    history_operation_ids: list[str] = field(default_factory=list)
    history_coverage: list[dict] = field(default_factory=list)
    history_interrupted: bool = False
    deferred_rule_refresh_paths: list[Path] = field(default_factory=list)
    issues: list[ImageAuthorityInitializationIssue] = field(default_factory=list)
    evidence_counts: ExternalEvidenceImportCounts = field(
        default_factory=ExternalEvidenceImportCounts,
    )

    @property
    def failed(self) -> int:
        return sum(1 for issue in self.issues if issue.blocking)

    @property
    def warnings(self) -> int:
        return sum(1 for issue in self.issues if not issue.blocking)


@dataclass
class _PendingImage:
    image: Path
    image_signature: dict[str,object]
    txt_signature: dict[str,object]
    record: ImageRecord
    existing: bool
    authority_fingerprint: dict[str,object]|None = None
    authority_text: str = ""
    needs_txt: bool = True
    needs_metadata: bool = True
    needs_identity: bool = True
    needs_metadata_validation: bool = False


@contextmanager
def _initialization_write_guard(context,image):
    if context is None:
        yield
        return
    with context.locked(image):
        if context.store.has_pending(context.entity):
            raise HistoryWriteInterrupted(
                "Saved history needs recovery. Reopen this exact image in its data editor before initialization continues."
            )
        yield


class _InitializationCaptureContext:
    """Retain commit receipts while the original write authority is still held."""
    def __init__(self,context):
        self.context=context
        self.version_ids=[]

    def __getattr__(self,name):
        return getattr(self.context,name)

    def write(self,*args,**kwargs):
        before=self.store.current_head(self.entity)
        result=self.context.write(*args,**kwargs)
        head=self.store.current_head(self.entity)
        if head is not None and head!=before and head.operation_id==self.operation_id:
            self.version_ids.append(head.version_id)
        return result


class ImageAuthorityInitializationService:
    """Materialize Image Knowledge once and refresh external evidence only on request."""

    def __init__(
        self,metadata: MetadataSyncService|None=None,*,knowledge=None,
        automation_engine=None,journal: OperationJournal|None=None,
        gather_source_evidence: bool=True,refresh_external_evidence: bool=False,
        external_evidence_mode: str="",external_evidence_sources: Iterable[str]=(
            "txt","metadata",
        ),
        trusted_existing: Iterable[str|Path]=(),batch_size: int=64,
    ) -> None:
        self.metadata=metadata or MetadataSyncService()
        self.knowledge=knowledge
        self.automation_engine=automation_engine
        self.journal=journal
        self.gather_source_evidence=bool(gather_source_evidence)
        requested_mode=str(external_evidence_mode or "").strip().casefold()
        self.external_evidence_mode=(
            requested_mode if requested_mode in {"cached","changed","all"}
            else "all" if refresh_external_evidence else "cached"
        )
        selected={str(value).strip().casefold() for value in external_evidence_sources}
        self.external_evidence_sources=tuple(
            value for value in ("txt","metadata") if value in selected
        )
        self.refresh_external_evidence=self.external_evidence_mode in {
            "changed","all",
        }
        self.trusted_existing={Path(value) for value in trusted_existing}
        self.batch_size=max(1,min(128,int(batch_size)))
        self._identity_service=(
            ConfirmedTagIdentityService(knowledge)
            if knowledge is not None else None
        )
        self._engine=None
        self._rule_fingerprint=""
        self._operation_id=""

    def current_rules_fingerprint(self) -> str:
        if self.automation_engine is None:
            from .automation_repository import active_automation_engine
            self._engine=active_automation_engine(self.knowledge)
        else:
            self._engine=self.automation_engine
        self._rule_fingerprint=_rules_fingerprint(self._engine,self.knowledge)
        return self._rule_fingerprint

    def current_job_fingerprint(self) -> str:
        """Identify the managed-scan contract whose completions may be reused."""
        payload={
            "contract_version":MANAGED_SCAN_CONTRACT_VERSION,
            "image_schema_version":SCHEMA_VERSION,
            "source_fingerprint_version":SOURCE_FINGERPRINT_VERSION,
            "source_evidence_mode":"automatic" if self.gather_source_evidence else "disabled",
        }
        encoded=json.dumps(payload,sort_keys=True,separators=(",",":"),ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def initialize_many(
        self,images: Iterable[str|Path],*,
        progress: Callable[[int,int,str],None]|None=None,
        status: Callable[[int,int,str],None]|None=None,
        should_cancel: Callable[[],bool]|None=None,
        on_complete: Callable[[Path,ImageRecord],None]|None=None,
        on_cache_complete: Callable[[Path,ImageRecord,dict[str,object]],None]|None=None,
        on_external_evidence_complete: Callable[
            [Path,ImageRecord,ExternalEvidenceImportCounts],None
        ]|None=None,
        history_operation=None,
        history_operation_factory=None,
    ) -> ImageAuthorityInitializationResult:
        if history_operation is not None and history_operation_factory is not None:
            raise ValueError("Supply one exact history operation or a bounded operation factory, not both.")
        paths=[Path(image) for image in images]
        result=ImageAuthorityInitializationResult(total=len(paths))
        self.current_rules_fingerprint()
        self._operation_id=""
        pending_paths=[]
        for image in paths:
            if image in self.trusted_existing:
                result.reused+=1; result.processed+=1
            else:
                pending_paths.append(image)
        if result.processed and progress:
            progress(result.processed,result.total,"Restored completed work")
        batch_total=(len(pending_paths)+self.batch_size-1)//self.batch_size
        for batch_index,start in enumerate(
            range(0,len(pending_paths),self.batch_size),start=1,
        ):
            if should_cancel and should_cancel():
                result.cancelled=True
                break
            batch=pending_paths[start:start+self.batch_size]
            try:
                operation=(history_operation_factory(tuple(batch))
                           if history_operation_factory is not None else history_operation)
            except InterruptedError:
                result.cancelled=True
                break
            except (HistoryError,HistoryConfigurationError,OSError) as exc:
                for image in batch:
                    self._issue(result,image,"history_held",str(exc),"Review History & Versions locations in Setup, then retry this exact scope.")
                    result.history_coverage.append({"image":str(image),"status":"held","version_ids":[],"error":str(exc)})
                result.processed+=len(batch)
                result.history_interrupted=True
                break
            if operation is not None and operation.operation_id not in result.history_operation_ids:
                result.history_operation_ids.append(operation.operation_id)
            if status:
                status(
                    result.processed,result.total,
                    f"Preparing Image Knowledge batch {batch_index:,} of {batch_total:,}",
                )
            pending=[]
            for image in batch:
                if should_cancel and should_cancel():
                    result.cancelled=True
                    break
                before_processed=result.processed
                context=None
                coverage=None
                if operation is not None:
                    coverage={"image":str(image),"operation_id":operation.operation_id,"status":"held","version_ids":[]}
                    result.history_coverage.append(coverage)
                    try:
                        admitted=operation.context_for(image)
                        if admitted is None or admitted.operation_id!=operation.operation_id:
                            raise HistoryError("Initialization requires the exact admitted history context.")
                        context=_InitializationCaptureContext(admitted)
                        coverage["entity_id"]=context.entity.entity_id
                    except InterruptedError:
                        result.cancelled=True
                        break
                    except (HistoryError,HistoryConfigurationError,OSError) as exc:
                        coverage["error"]=str(exc)
                        self._issue(result,image,"history_held",str(exc),"Review saved-history configuration before retrying this exact image.")
                        result.processed+=1
                        continue
                try:
                    item=self._prepare(image,result,history_context=context)
                except InterruptedError:
                    result.cancelled=True
                    break
                if coverage is not None:
                    coverage["status"]="pending" if item is not None else "held" if result.processed>before_processed and any(issue.image==image and issue.blocking for issue in result.issues) else "unchanged"
                if item is not None:
                    pending.append((item,coverage))
                elif result.processed>before_processed and progress:
                    progress(result.processed,result.total,image.name)
            if result.cancelled:
                break
            metadata_images=[item.image for item,_coverage in pending if item.needs_metadata]
            if metadata_images and status:
                status(
                    result.processed,result.total,
                    f"Reading Metadata batch {batch_index:,} of {batch_total:,} "
                    f"({len(metadata_images):,} image(s))",
                )
            metadata_values=self._read_metadata_batch(
                metadata_images,should_cancel=should_cancel,
            )
            if should_cancel and should_cancel():
                result.cancelled=True
                break
            if pending and status:
                writes_authority=any(
                    not item.existing or item.needs_txt
                    or item.needs_metadata or item.needs_identity or item.needs_metadata_validation
                    for item,_coverage in pending
                )
                status(
                    result.processed,result.total,
                    (
                        f"Saving Image Knowledge batch {batch_index:,} of {batch_total:,}"
                        if writes_authority else
                        f"Refreshing local image results batch {batch_index:,} of {batch_total:,}"
                    ),
                )
            for item,coverage in pending:
                if should_cancel and should_cancel():
                    result.cancelled=True
                    break
                reused_before=result.reused
                derived=self._initialize_one(
                    item,metadata_values.get(item.image),result,
                )
                if coverage is not None:
                    coverage["version_ids"]=list(item.record.history_context.version_ids)
                    coverage["status"]=("interrupted" if result.history_interrupted else "held" if derived is None else "complete" if coverage["version_ids"] else "unchanged")
                    problems=[issue.message for issue in result.issues if issue.image==item.image and issue.blocking]
                    if problems:coverage["error"]="; ".join(problems)
                evidence_counts=self._external_evidence_counts(
                    item,derived,
                    unchanged=bool(
                        derived is not None
                        and result.reused>reused_before
                        and (item.needs_txt or item.needs_metadata)
                    ),
                )
                result.evidence_counts+=evidence_counts
                if derived is not None and on_cache_complete:
                    on_cache_complete(item.image,item.record,derived)
                if derived is not None and on_external_evidence_complete:
                    on_external_evidence_complete(
                        item.image,item.record,evidence_counts,
                    )
                if derived is not None and on_complete:
                    on_complete(item.image,item.record)
                result.processed+=1
                if progress:
                    progress(result.processed,result.total,item.image.name)
                if result.history_interrupted:
                    break
            if result.cancelled or result.history_interrupted:
                break
        for coverage in result.history_coverage:
            if coverage["status"]=="pending":coverage["status"]="unprocessed"
        if self._operation_id and self.journal is not None:
            status="cancelled" if result.cancelled else "failed" if result.failed else "complete"
            self.journal.finish(
                self._operation_id,status,
                message="Managed scan stopped with partial failures." if status=="failed" else "",
            )
            self.journal.prune()
        close_metadata=getattr(self.metadata,"close",None)
        if callable(close_metadata):
            close_metadata()
        return result

    def _external_evidence_counts(
        self,item: _PendingImage,derived: dict[str,object]|None,*,
        unchanged: bool,
    ) -> ExternalEvidenceImportCounts:
        if not self.refresh_external_evidence or derived is None:
            return ExternalEvidenceImportCounts()
        source_reads=int(item.needs_txt)+int(item.needs_metadata)
        if not source_reads:
            return ExternalEvidenceImportCounts()
        findings=derived.get("candidate_findings",[])
        read_sources={
            name for key,name in (
                ("txt","TXT caption"),("metadata","Image metadata"),
            ) if (
                key=="txt" and item.needs_txt
                or key=="metadata" and item.needs_metadata
            )
        }
        findings=[
            value for value in findings
            if isinstance(value,dict)
            and read_sources.intersection(
                str(source) for source in value.get("sources",[])
            )
        ] if isinstance(findings,list) else []
        tag_findings=[
            value for value in findings if value.get("kind")=="tag"
        ]
        identities={}
        if self._identity_service is not None and tag_findings:
            identities={
                entry.normalized_value:entry
                for entry in self._identity_service.plan(
                    (str(value.get("value") or "") for value in tag_findings),
                    confirmation=False,
                ).entries
            }
        known=0; unknown=0; ambiguous=sum(
            value.get("kind")=="ambiguous" for value in findings
        )
        for finding in tag_findings:
            key=normalize_tag(str(finding.get("value") or ""))
            identity=identities.get(key)
            if identity is not None and identity.resolved:
                known+=1
            elif identity is not None and identity.status=="ambiguous":
                ambiguous+=1
            else:
                unknown+=1
        automation=derived.get("candidate_automation",{})
        suppressed=(
            automation.get("suppressed_evidence",[])
            if isinstance(automation,dict) else []
        )
        return ExternalEvidenceImportCounts(
            reread_images=int(bool(source_reads)),source_reads=source_reads,
            unchanged_images=int(bool(unchanged)),known_tags=known,
            unknown_tags=unknown,ambiguous_values=int(ambiguous),
            conflicted_values=sum(bool(value.get("conflicts")) for value in findings),
            descriptions=sum(
                value.get("kind")=="description" for value in findings
            ),
            suppressed_values=sum(
                isinstance(value,dict)
                and bool(read_sources.intersection(
                    str(source) for source in value.get("sources",[])
                ))
                for value in suppressed
            ),
        )

    def _prepare(
        self,image: Path,result: ImageAuthorityInitializationResult,
        *,history_context=None,
    ) -> _PendingImage|None:
        try:
            with _initialization_write_guard(history_context,image):
                item=self._prepare_unlocked(image,result)
                if item is not None:item.record.history_context=history_context
                return item
        except InterruptedError:
            raise
        except (HistoryError,HistoryConfigurationError,OSError) as exc:
            self._issue(result,image,"history_held",str(exc),"Reopen this exact image in its data editor to review saved-data recovery.")
            result.processed+=1
            return None

    def _prepare_unlocked(
        self,image: Path,result: ImageAuthorityInitializationResult,
    ) -> _PendingImage|None:
        image_signature=_signature(image)
        if not image_signature.get("exists"):
            self._issue(result,image,"image_unavailable","The image is no longer available.",
                        "Restore access to the image and refresh the library.")
            result.processed+=1
            return None
        sidecar=image.with_suffix(".tags.json")
        if (
            sidecar.exists() and image in self.trusted_existing
            and self.external_evidence_mode=="cached"
        ):
            result.reused+=1; result.processed+=1
            return None
        txt=image.with_suffix(".txt"); txt_signature=_signature(txt)
        if not sidecar.exists():
            record=ImageRecord(image,auto_load=False); record.loaded=True
            record.annotation=ImageAnnotation(image=image.name)
            return _PendingImage(
                image,image_signature,txt_signature,record,False,
                needs_txt=(
                    self.gather_source_evidence
                    and "txt" in self.external_evidence_sources
                ),
                needs_metadata=(
                    self.gather_source_evidence
                    and "metadata" in self.external_evidence_sources
                ),
                needs_identity=True,
            )
        try:
            before=_signature(sidecar)
            authority_bytes=sidecar.read_bytes()
            authority_text=authority_bytes.decode("utf-8")
            after=_signature(sidecar)
            if before!=after:
                raise RuntimeError("The sidecar changed while it was being read.")
            payload=json.loads(authority_text)
            if not isinstance(payload,dict):
                raise ValueError("The sidecar root is not a JSON object.")
            version=int(payload.get("schema_version",1))
            if version!=SCHEMA_VERSION:
                result.existing_legacy+=1; result.processed+=1
                return None
            record=ImageRecord(image,auto_load=False); record.loaded=True
            record.annotation=ImageAnnotation.from_dict(payload,image.name)
            authority_fingerprint={**after,"sha256":hashlib.sha256(authority_bytes).hexdigest()}
        except Exception as exc:
            self._issue(result,image,"invalid_sidecar",f"The existing Image Knowledge record was left unchanged: {exc}",
                        "Repair or restore the existing .tags.json, then refresh the library.")
            result.processed+=1
            return None
        identity=record.annotation.file_identity
        identity_current=_signature_matches(identity,image)
        fingerprints=record.annotation.source.get("source_fingerprints",{})
        if not isinstance(fingerprints,dict):
            fingerprints={}
        txt_current=_signature_matches(
            fingerprints.get("txt"),image.with_suffix(".txt"),
        )
        metadata_fingerprint=fingerprints.get("metadata",{})
        metadata_current=bool(
            isinstance(metadata_fingerprint,dict)
            and metadata_fingerprint.get("status")=="verified"
            and int(metadata_fingerprint.get("reader_version",0) or 0)
            ==int(getattr(self.metadata,"READER_VERSION",1))
            and isinstance(metadata_fingerprint.get("input"),dict)
            and _valid_sha256(metadata_fingerprint["input"].get("sha256"))
            and _signature_matches(metadata_fingerprint.get("input"),image)
        )
        needs_txt=bool(
            self.gather_source_evidence
            and "txt" in self.external_evidence_sources
            and (
                self.external_evidence_mode=="all"
                or self.external_evidence_mode=="changed" and not txt_current
            )
        )
        needs_metadata=bool(
            self.gather_source_evidence
            and "metadata" in self.external_evidence_sources
            and (
                self.external_evidence_mode=="all"
                or self.external_evidence_mode=="changed" and not metadata_current
            )
        )
        return _PendingImage(
            image,image_signature,txt_signature,record,True,authority_fingerprint,
            authority_text,needs_txt,needs_metadata,not identity_current,
            not metadata_current and bool(
                record.annotation.standard_metadata
                or any(value.source.casefold()=="image metadata" for value in record.annotation.generation_records)
                or any(isinstance(value,dict) and value.get("name")=="Image metadata"
                       for value in record.annotation.source.get("evidence",[]))
            ),
        )

    def _read_metadata_batch(
        self,images: list[Path],*,should_cancel: Callable[[],bool]|None=None,
    ) -> dict[Path,dict[str,object]|Exception]:
        if not images:
            return {}
        reader=getattr(self.metadata,"read_embedded_many",None)
        if callable(reader):
            try:
                try:
                    parameters=inspect.signature(reader).parameters.values()
                    accepts_cancel=any(
                        value.name=="should_cancel"
                        or value.kind==inspect.Parameter.VAR_KEYWORD
                        for value in parameters
                    )
                except (TypeError,ValueError):
                    accepts_cancel=False
                if accepts_cancel:
                    return dict(reader(images,should_cancel=should_cancel))
                return dict(reader(images))
            except Exception as exc:
                return {image:exc for image in images}
        values={}
        for image in images:
            try:
                values[image]=self.metadata.read_embedded(image)
            except Exception as exc:
                values[image]=exc
        return values

    def _initialize_one(
        self,item: _PendingImage,metadata_value: dict[str,object]|Exception|None,
        result: ImageAuthorityInitializationResult,
    ) -> dict[str,object]|None:
        image=item.image; record=item.record; source=record.annotation.source
        existing_evidence=[copy.deepcopy(value) for value in source.get("evidence",[]) if isinstance(value,dict)]
        evidence=[value for value in existing_evidence if str(value.get("name")) not in MANAGED_SOURCE_NAMES]
        fingerprints=copy.deepcopy(source.get("source_fingerprints",{}))
        if not isinstance(fingerprints,dict): fingerprints={}
        try:
            durable_change=bool(
                not item.existing or item.needs_txt
                or item.needs_metadata or item.needs_identity or item.needs_metadata_validation
            )
            if not durable_change:
                derived=self.derive_rule_result(
                    record,
                    authority_sha256=str((item.authority_fingerprint or {}).get("sha256") or ""),
                )
                result.reused+=1
                result.cache_refreshed+=1
                self._report_derived_issues(image,derived,result)
                return derived
            if item.needs_txt:
                txt_evidence,txt_fingerprint=self._read_txt(
                    image.with_suffix(".txt"), item.txt_signature, record,
                )
                fingerprints["txt"]=txt_fingerprint
            else:
                txt_evidence=next((value for value in existing_evidence if value.get("name")=="TXT caption"),None)
            if txt_evidence is not None:
                evidence.append(txt_evidence)

            image_fingerprint=(
                _stable_file_fingerprint(image,item.image_signature)
                if item.needs_metadata or item.needs_identity or item.needs_metadata_validation else copy.deepcopy(record.annotation.file_identity)
            )
            metadata_evidence=next((value for value in existing_evidence if value.get("name")=="Image metadata"),None)
            if item.needs_metadata:
                reader_version=int(getattr(self.metadata,"READER_VERSION",1))
                if isinstance(metadata_value,Exception) or metadata_value is None:
                    message=str(metadata_value or "Metadata returned no embedded result.")
                    previous_metadata=metadata_evidence
                    metadata_evidence={"name":"Image metadata","tags":[],"plain_language":[],
                                       "generation_reference":[],"generation_records":[],
                                       "standard_metadata":{},"error":message}
                    if previous_metadata:
                        stale=source.get("stale_metadata_observations",[])
                        if not isinstance(stale,list): stale=[]
                        stale.append({
                            "invalidated_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "reason":"The image changed and current embedded metadata could not be read.",
                            "evidence":previous_metadata,
                        })
                        source["stale_metadata_observations"]=stale[-3:]
                        record.annotation.generation_records=[
                            value for value in record.annotation.generation_records
                            if value.source.casefold()!="image metadata"
                        ]
                        record.annotation.standard_metadata={}
                    fingerprints["metadata"]={"status":"error","reader_version":reader_version,
                                               "input":image_fingerprint}
                    self._issue(result,image,"metadata_unavailable",
                                f"Image Knowledge was initialized, but Metadata could not be read: {message}",
                                "Restore ExifTool or image access, then refresh to retry Metadata extraction.",blocking=False)
                else:
                    metadata_evidence=self._metadata_evidence(metadata_value)
                    fingerprints["metadata"]={"status":"verified","reader_version":reader_version,
                                               "input":image_fingerprint}
                    record.annotation.generation_records=[
                        value for value in record.annotation.generation_records
                        if value.source.casefold()!="image metadata"
                    ]
                    for value in metadata_value.get("generation_records",[]):
                        if isinstance(value,dict):
                            record.annotation.add_generation_record(GenerationRecord.from_dict(value))
                    record.annotation.standard_metadata=dict(metadata_value.get("standard_metadata",{}))
            elif item.needs_identity or item.needs_metadata_validation:
                previous_metadata_fingerprint=fingerprints.get("metadata",{})
                if not isinstance(previous_metadata_fingerprint,dict):
                    previous_metadata_fingerprint={}
                previous_input=(
                    previous_metadata_fingerprint.get("input",{})
                )
                if not isinstance(previous_input,dict):
                    previous_input={}
                same_content=bool(
                    previous_metadata_fingerprint.get("status")=="verified"
                    and int(previous_metadata_fingerprint.get("reader_version",0) or 0)==int(getattr(self.metadata,"READER_VERSION",1))
                    and _valid_sha256(previous_input.get("sha256"))
                    and previous_input.get("sha256")==image_fingerprint.get("sha256")
                )
                if same_content:
                    previous_metadata_fingerprint["input"]=image_fingerprint
                    fingerprints["metadata"]=previous_metadata_fingerprint
                else:
                    embedded_records=[
                        value for value in record.annotation.generation_records
                        if value.source.casefold()=="image metadata"
                    ]
                    has_cached_metadata=bool(
                        metadata_evidence is not None or embedded_records
                        or record.annotation.standard_metadata
                        or previous_input.get("sha256")
                    )
                    if has_cached_metadata:
                        previous_content_known=bool(_valid_sha256(previous_input.get("sha256")) and previous_input.get("sha256")!=image_fingerprint.get("sha256"))
                        stale_reason=(
                            "The image content changed; cached embedded Metadata was not reused."
                            if previous_content_known else
                            "The source fingerprint or reader provenance is unavailable; cached embedded Metadata cannot be verified against this image."
                        )
                        stale_message=(
                            "The image content changed, so cached embedded Metadata was set aside without being reread."
                            if previous_content_known else
                            "The cached embedded Metadata has no current verified source fingerprint and reader provenance, so it was set aside without being reread."
                        )
                        stale_evidence=metadata_evidence
                        if stale_evidence is None and (embedded_records or record.annotation.standard_metadata):
                            stale_evidence={
                                "name":"Image metadata","tags":[],"plain_language":[],
                                "generation_reference":[],
                                "generation_records":[value.to_dict() for value in embedded_records],
                                "standard_metadata":copy.deepcopy(record.annotation.standard_metadata),
                                "error":"",
                            }
                        if stale_evidence is not None:
                            stale=source.get("stale_metadata_observations",[])
                            if not isinstance(stale,list): stale=[]
                            stale.append({
                                "invalidated_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                "reason":stale_reason,
                                "evidence":stale_evidence,
                            })
                            source["stale_metadata_observations"]=stale[-3:]
                        metadata_evidence=None
                        record.annotation.generation_records=[
                            value for value in record.annotation.generation_records
                            if value.source.casefold()!="image metadata"
                        ]
                        record.annotation.standard_metadata={}
                        fingerprints["metadata"]={
                            "status":"stale","reader_version":previous_metadata_fingerprint.get("reader_version",1),
                            "input":previous_input,"current_input":image_fingerprint,
                        }
                        self._issue(
                            result,image,"metadata_stale",
                            stale_message,
                            "Use Import External Evidence on the owning Ingest or Dataset page to reread the current image when it is available.",blocking=False,
                        )
            if metadata_evidence is not None:
                evidence.append(metadata_evidence)

            if _signature(image)!=item.image_signature or _signature(image.with_suffix(".txt"))!=item.txt_signature:
                raise RuntimeError("The image or TXT caption changed before authority could be saved.")
            record.annotation.file_identity=image_fingerprint
            fingerprints["version"]=SOURCE_FINGERPRINT_VERSION
            fingerprints["image"]=image_fingerprint
            source["source_fingerprints"]=fingerprints
            source["evidence"]=evidence
            portable=ImageRecord(image,auto_load=False); portable.loaded=True
            portable.annotation=copy.deepcopy(record.annotation)
            BatchPreparationService.update_candidate_findings(
                portable,evidence,self.knowledge,self._engine,
                replace_sources=MANAGED_SOURCE_NAMES,
                apply_rules=False,
            )
            source["candidate_findings"]=copy.deepcopy(
                portable.annotation.source.get("candidate_findings",[])
            )
            # Known legacy managed-scan fields are derived machine-local state.
            # Retire them only while another durable authority change already
            # requires a verified sidecar write; never churn an unchanged file
            # merely to remove the compatibility residue.
            source.pop("candidate_automation",None)
            source.pop("automatic_rule_state",None)
            source.pop("managed_scan",None)
            record.modified=True
            with _initialization_write_guard(record.history_context,image):
                if _signature(image)!=item.image_signature or _signature(image.with_suffix(".txt"))!=item.txt_signature:
                    raise RuntimeError("The image or TXT caption changed before authority could be saved.")
                if item.existing:
                    if self._update_existing(item,record):
                        result.refreshed+=1
                    else:
                        result.reused+=1
                else:
                    if not record.create_sidecar():
                        raise FileExistsError("A sidecar appeared while authority was being initialized.")
                    result.created+=1
            authority_fingerprint=_stable_file_fingerprint(record.sidecar_path)
            derived=self.derive_rule_result(
                record,authority_sha256=str(authority_fingerprint.get("sha256") or ""),
            )
            result.cache_refreshed+=1
            self._report_derived_issues(image,derived,result)
            return derived
        except InterruptedError:
            result.cancelled=True
        except HistoryWriteInterrupted as exc:
            result.history_interrupted=True
            self._issue(result,image,"history_interrupted",str(exc),"The current record was retained. Reopen this exact image in its data editor to review saved-data recovery.")
        except FileExistsError as exc:
            self._issue(result,image,"concurrent_sidecar",str(exc),
                        "Refresh the library; the existing sidecar was not overwritten.")
        except Exception as exc:
            self._issue(result,image,"initialization_failed",str(exc),
                        "Restore write access or wait for other file activity to finish, then refresh to resume.")
        return None

    def derived_semantic_proof(self):
        """Share the bounded per-image proof for disposable rule results."""
        from .workflow_semantic_proof import WorkflowSemanticProof
        if not self._rule_fingerprint or self._engine is None:
            self.current_rules_fingerprint()
        if getattr(self,"_derived_proof_fingerprint",None)!=self._rule_fingerprint:
            self._derived_proof=WorkflowSemanticProof(
                self.knowledge,None,getattr(self.knowledge,"_categories",None),(),
                engine=self._engine,
            )
            self._derived_proof_fingerprint=self._rule_fingerprint
        return self._derived_proof

    def reusable_derived_result(self,payload):
        """Rebind a proven unchanged computation; never certify another cache's work."""
        if not self.derived_semantic_proof().matches(payload.get("semantic_dependencies")):
            return None
        current=copy.deepcopy(payload)
        current["rules_fingerprint"]=self._rule_fingerprint
        state=current.get("automatic_rule_state")
        if not isinstance(state,dict):return None
        state["fingerprint"]=self._rule_fingerprint
        return current

    def derive_rule_result(
        self,record: ImageRecord,*,authority_sha256: str="",
    ) -> dict[str,object]:
        """Build disposable current-rule state without mutating portable authority."""
        if not self._rule_fingerprint or self._engine is None:
            self.current_rules_fingerprint()
        if not authority_sha256:
            authority_sha256=str(
                _stable_file_fingerprint(record.sidecar_path).get("sha256") or ""
            )
        derived_record=ImageRecord(record.image_path,auto_load=False)
        derived_record.loaded=True
        derived_record.annotation=copy.deepcopy(record.annotation)
        source=derived_record.annotation.source
        evidence=[
            copy.deepcopy(value) for value in source.get("evidence",[])
            if isinstance(value,dict)
        ]
        update=BatchPreparationService.update_candidate_findings(
            derived_record,evidence,self.knowledge,self._engine,
            replace_sources=MANAGED_SOURCE_NAMES,
        )
        projection_rules,confirmed_conflicts=self._inspect_confirmed_rules(
            derived_record,
        )
        automatic_rule_state={
            "fingerprint":self._rule_fingerprint,
            "transform_rules":{
                "mode":"training_projection_only",
                "matching_rules":list(projection_rules),
                "applied_rules":[],
            },
            "conflict_rules":{
                "mode":"review_only","matches":confirmed_conflicts,
            },
        }
        try:
            semantic_dependencies=self.derived_semantic_proof().build(
                record,candidate_findings=derived_record.annotation.source.get("candidate_findings",[]),
            )
        except (AttributeError,TypeError,ValueError,KeyError):
            semantic_dependencies=None
        return {
            "version":DERIVED_RULE_RESULT_VERSION,
            "semantic_dependencies":semantic_dependencies,
            "authority_sha256":str(authority_sha256),
            "rules_fingerprint":self._rule_fingerprint,
            "candidate_findings":copy.deepcopy(
                derived_record.annotation.source.get("candidate_findings",[])
            ),
            "candidate_automation":copy.deepcopy(
                derived_record.annotation.source.get("candidate_automation",{})
            ),
            "automatic_rule_state":automatic_rule_state,
            "candidate_conflicts":copy.deepcopy(update.conflicts),
        }

    def _report_derived_issues(
        self,image: Path,derived: dict[str,object],
        result: ImageAuthorityInitializationResult,
    ) -> None:
        state=derived.get("automatic_rule_state",{})
        conflict_state=(
            state.get("conflict_rules",{}) if isinstance(state,dict) else {}
        )
        confirmed_conflicts=(
            conflict_state.get("matches",[]) if isinstance(conflict_state,dict)
            else []
        )
        candidate_conflicts=derived.get("candidate_conflicts",[])
        if isinstance(confirmed_conflicts,list) and confirmed_conflicts:
            result.conflicts+=len(confirmed_conflicts)
            self._issue(
                result,image,"confirmed_conflict",
                f"{len(confirmed_conflicts)} confirmed-data conflict(s) need review; no conflict rule changed the image.",
                "Open Review findings in the image-data editor and choose which confirmed facts describe the image.",
                blocking=False,
            )
        if isinstance(candidate_conflicts,list) and candidate_conflicts:
            self._issue(
                result,image,"candidate_conflict",
                f"{len(candidate_conflicts)} Metadata conflict(s) need review.",
                "Open Review findings in the image-data editor and keep only the facts that describe the image.",
                blocking=False,
            )

    def _inspect_confirmed_rules(self,record: ImageRecord) -> tuple[list[str],list[dict]]:
        """Describe projection matches and conflicts without changing authority."""
        before=record.annotation.all_structured_tags()
        _after,projection_rules=self._engine.project_tags(before,automatic_only=True)
        conflicts=[]
        conflict_engine=getattr(self._engine,"conflicts",None)
        if conflict_engine is not None:
            for match in conflict_engine.evaluate(record):
                conflicts.append({
                    "rule_id":match.rule.id,"message":match.message,
                    "tags":list(match.found_tags),"ambiguous":bool(match.ambiguous),
                    "severity":match.rule.severity,"character_slot":match.character_slot,
                    "object_slot":getattr(match,"object_slot",None),
                    "owner_kind":str(getattr(match,"owner_kind","") or ""),
                    "owner_id":str(getattr(match,"owner_id","") or ""),
                    "owner_label":str(getattr(match,"owner_label","") or ""),
                    "relationship_id":match.rule.relationship_id or match.rule.id,
                })
        return list(projection_rules),conflicts

    def _update_existing(self,item: _PendingImage,record: ImageRecord) -> bool:
        sidecar=record.sidecar_path
        current=_stable_file_fingerprint(sidecar)
        if current!=item.authority_fingerprint:
            raise RuntimeError("The existing sidecar changed while source evidence was being refreshed.")
        expected=record.annotation.to_dict()
        expected_text=json.dumps(expected,indent=2,ensure_ascii=False)+"\n"
        if expected_text==item.authority_text:
            record.modified=False
            return False
        try:
            record.save_sidecar(force=True)
            written=json.loads(sidecar.read_text(encoding="utf-8"))
            if written!=expected:
                raise RuntimeError("The refreshed sidecar failed read-back verification.")
        except Exception:
            if record.history_context is not None:
                # A native intent owns recovery, including uncertain writes.
                # Do not inspect or roll back through the legacy fallback.
                raise
            # The write itself is atomic. Restore only when the current bytes
            # are exactly the bytes this operation published; a third-party
            # value must never be mistaken for our failed write.
            try:
                current_text=sidecar.read_text(encoding="utf-8")
            except OSError:
                current_text=""
            if item.authority_text and current_text==expected_text:
                _atomic_write_text(sidecar,item.authority_text)
            raise
        return True

    def _read_txt(
        self,path: Path,expected: dict[str,object],record: ImageRecord,
    ) -> tuple[dict|None,dict[str,object]]:
        if not expected.get("exists"):
            return None,{"exists":False}
        before=_signature(path)
        if before!=expected:
            raise RuntimeError("The TXT caption changed while it was being initialized.")
        raw=path.read_bytes(); after=_signature(path)
        if after!=before:
            raise RuntimeError("The TXT caption changed while it was being initialized.")
        raw_text=raw.decode("utf-8")
        typed_tag_keys=TagCollection(concrete_sdxl_tags([
            *record.annotation.all_structured_tags(),
            *record.annotation.confirmed_absent,
            *record.annotation.source.get("rejected_tags",[]),
        ])).normalized_set()
        rules=getattr(self._engine,"rules",{})
        def add_typed_rule_value(value) -> None:
            value=str(value or "")
            if value and not has_automation_placeholder(value):
                typed_tag_keys.add(normalize_tag(value))
        for rule in rules.get("ignore_rules",[]) if isinstance(rules,dict) else []:
            add_typed_rule_value(rule.get("tag"))
        for family in ("merge_rules","combine_rules","wildcard_rules"):
            for rule in rules.get(family,[]) if isinstance(rules,dict) else []:
                for field in ("remove","tags","source_tags","requires"):
                    values=rule.get(field,[]) if isinstance(rule,dict) else []
                    for value in values if isinstance(values,list) else [values]:
                        add_typed_rule_value(value)
                for field in ("replace","target_tag"):
                    if isinstance(rule,dict) and rule.get(field):
                        add_typed_rule_value(rule[field])
        conflict_engine=getattr(self._engine,"conflicts",None)
        for rule in getattr(conflict_engine,"rules",[]):
            for value in getattr(rule,"tags",[]):
                add_typed_rule_value(value)
        def known(value):
            return (
                normalize_tag(value) in typed_tag_keys
                or (self.knowledge is not None and self.knowledge.find(value) is not None)
            )
        parsed=parse_generated_caption(
            raw_text, known_tags=known,
            projection_state=record.annotation.projection_state.get("txt"),
        )
        fingerprint={**after,"sha256":hashlib.sha256(raw).hexdigest()}
        return ({"name":"TXT caption","tags":list(parsed.sdxl_tags),
                 "plain_language":list(parsed.plain_language),"detected_format":parsed.format,
                 "ambiguous_fragments":list(parsed.ambiguous_fragments),
                 "raw_text":parsed.raw_text,
                 "app_owned_projection":parsed.app_owned_projection,
                 "generation_reference":[],"generation_records":[],"standard_metadata":{},"error":""},
                fingerprint)

    @staticmethod
    def _metadata_evidence(values: dict[str,object]) -> dict:
        return {
            "name":"Image metadata","tags":list(values.get("tags",[])),
            "plain_language":list(values.get("plain_language",[])),
            "ambiguous_fragments":list(values.get("ambiguous_fragments",[])),
            "type_issues":list(values.get("type_issues",[])),
            "tag_fields":copy.deepcopy(values.get("tag_fields",{})),
            "description_fields":copy.deepcopy(values.get("description_fields",{})),
            "generation_reference":list(values.get("generation_info",[])),
            "generation_records":[dict(value) for value in values.get("generation_records",[]) if isinstance(value,dict)],
            "raw_generation_fields":copy.deepcopy(values.get("raw_generation_fields",{})),
            "standard_metadata":dict(values.get("standard_metadata",{})),"error":"",
        }

    @staticmethod
    def _issue(
        result: ImageAuthorityInitializationResult,image: Path,code: str,message: str,
        recovery: str,blocking: bool=True,
    ) -> None:
        result.issues.append(ImageAuthorityInitializationIssue(
            image,code,str(message),recovery,blocking,
        ))
