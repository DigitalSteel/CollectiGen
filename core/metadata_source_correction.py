from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .external_write_transaction import (
    ExternalWriteDecision, ExternalWriteExecution, atomic_verified_bytes,
    copy_verified, external_write_validation_issue,
    file_fingerprint, same_content, validated_recovery_root,
)
from .image_record import ImageRecord
from .image_fact_decisions import image_tag_identity
from .image_record_recovery import ImageRecordRecoveryPlan
from .operation_journal import OperationJournal
from .settings import normalize_plain_language_exclusion
from .source_correction import (
    PAIRED_IMAGE_KNOWLEDGE,SOURCE_ONLY,SourceCorrectionChoice,
    SourceCorrectionService,
    SourceCorrectionHistory,history_preview,history_apply,history_restore,
)
from .tag_collection import normalize_tag


def _json_hash(value: object) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _description_values(value: object) -> tuple[list[str], str]:
    """Return exact blank-line-delimited descriptions and their separator."""
    raw = str(value or "")
    separator = "\r\n\r\n" if "\r\n\r\n" in raw else "\n\n"
    if not raw:
        return [], separator
    values = [part.strip() for part in raw.replace("\r\n", "\n").split("\n\n")]
    if any(not part for part in values):
        raise ValueError("Managed XMP:Description has ambiguous blank-line structure.")
    if separator.join(values)!=raw:
        raise ValueError(
            "Managed XMP:Description has formatting outside the supported exact "
            "blank-line structure."
        )
    return values, separator


def _subject_values(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value,str):
        return [value] if value.strip() else []
    if isinstance(value,(list,tuple)) and all(
        isinstance(item,str) and item.strip() for item in value
    ):
        return list(value)
    raise ValueError("Managed XMP:Subject is unavailable or ambiguous.")


def _readonly(path: Path) -> bool:
    return bool(path.exists() and not path.stat().st_mode & stat.S_IWRITE)


@dataclass(frozen=True)
class ManagedMetadataCorrectionPlan:
    image: Path
    source_path: Path
    source_kind: str
    source_field: str
    value_kind: str
    value: str
    disposition: str
    detail: str
    original_text: str = ""
    updated_text: str = ""
    original_values: tuple[str, ...] = ()
    updated_values: tuple[str, ...] = ()
    occurrence_count: int = 0
    confirmed_in_image_knowledge: bool = False
    source_fingerprint: dict[str, object] = field(default_factory=dict)
    authority_fingerprint: dict[str, object] = field(default_factory=dict)
    write_decision: ExternalWriteDecision = field(default_factory=ExternalWriteDecision)
    plan_fingerprint: str = ""
    preservation_fingerprint: str = ""
    pixel_fingerprint: str = ""
    choices: tuple[SourceCorrectionChoice, ...] = ()
    paired_disposition: str = "unsupported"
    paired_detail: str = ""
    paired_present_values: tuple[str, ...] = ()
    paired_absent_value: str = ""
    paired_recovery_plan: ImageRecordRecoveryPlan | None = None
    history_operation: object = field(default=None,compare=False,repr=False)
    history_source_evidence: dict = field(default_factory=dict,compare=False,repr=False)
    history_head: str = ""

    @property
    def will_write(self) -> bool:
        return self.disposition == "change"

    @property
    def backup_enabled(self) -> bool:
        return self.write_decision.backup_enabled

    @property
    def backup_directory(self) -> Path | None:
        return self.write_decision.backup_directory

    @property
    def privacy_confirmation_required(self) -> bool:
        return self.write_decision.privacy_confirmation_required


@dataclass(frozen=True)
class ManagedMetadataCorrectionResult:
    status: str
    detail: str
    source_path: Path
    operation_id: str = ""
    recovery_manifest: Path | None = None
    source_kind: str = "managed_metadata"
    value_kind: str = ""
    source_field: str = ""
    meaning: str = "source_only"
    image_knowledge_recovery_manifest: Path | None = None


@dataclass(frozen=True)
class ManagedMetadataCorrectionRestoreResult:
    status: str
    detail: str
    source_path: Path
    source_kind: str = "managed_metadata"


class ManagedMetadataCorrectionService:
    """One exact, source-only correction of a CollectiGen-managed XMP field."""

    RECOVERY_KIND = "collectigen_metadata_source_correction_recovery"
    JOURNAL_KIND = "metadata_source_correction"

    def __init__(self, metadata=None, *, knowledge=None, recovery_service=None,history_operation_factory=None) -> None:
        self.metadata = metadata
        self.knowledge=knowledge
        self._paired=SourceCorrectionService(
            knowledge=knowledge,recovery_service=recovery_service,history_operation_factory=history_operation_factory,
        )
        self._history=SourceCorrectionHistory(self,history_operation_factory)

    @staticmethod
    def _root(directory: Path | None) -> Path:
        return validated_recovery_root(
            directory, "source-corrections", workflow_label="Metadata source correction",
        )

    def _metadata(self):
        if self.metadata is None:
            from .metadata_sync import MetadataSyncService

            self.metadata = MetadataSyncService()
        return self.metadata

    def _pixel_fingerprint(self,path: Path) -> str:
        hook=getattr(self._metadata(),"pixel_fingerprint",None)
        if callable(hook):
            return str(hook(path) or "")
        import hashlib
        from PIL import Image,ImageSequence

        digest=hashlib.sha256()
        with Image.open(path) as image:
            for frame in ImageSequence.Iterator(image):
                digest.update(str(frame.mode).encode("utf-8"))
                digest.update(f"{frame.width}x{frame.height}".encode("ascii"))
                digest.update(frame.tobytes())
        return digest.hexdigest()

    @staticmethod
    def _result(
        plan: ManagedMetadataCorrectionPlan,status: str,detail: str,*,
        operation_id: str="",recovery_manifest: Path|None=None,
        meaning: str=SOURCE_ONLY,
        image_knowledge_recovery_manifest: Path|None=None,
    ) -> ManagedMetadataCorrectionResult:
        return ManagedMetadataCorrectionResult(
            status,detail,plan.source_path,operation_id,recovery_manifest,
            value_kind=plan.value_kind,source_field=plan.source_field,
            meaning=meaning,
            image_knowledge_recovery_manifest=image_knowledge_recovery_manifest,
        )

    @staticmethod
    def _preserved(
        values: dict[str, object],source_field: str,
    ) -> dict[str, object]:
        return {
            key: value for key, value in values.items()
            if key not in {
                "tag_fields","description_fields","tags","plain_language",
                "ambiguous_fragments","type_issues","generation_records",
            }
            # Parser-generated record IDs are fresh on each read. Exact generator
            # source payloads remain protected by raw_generation_fields instead.
        } | {
            "tag_fields": {
                key: value for key, value in dict(values.get("tag_fields") or {}).items()
                if source_field!="XMP:Subject" or key!="Subject"
            },
            "description_fields": {
                key: value for key, value in dict(values.get("description_fields") or {}).items()
                if source_field!="XMP:Description" or key!="Description"
            },
        }

    def _confirmed(self,image: Path, value: str, value_kind: str) -> bool:
        try:
            annotation = ImageRecord(image).annotation
        except Exception:
            return False
        if value_kind == "tag":
            wanted = image_tag_identity(value,self.knowledge)
            return bool(wanted) and any(
                image_tag_identity(item,self.knowledge)==wanted
                for item in annotation.all_structured_tags()
            )
        wanted = normalize_plain_language_exclusion(value)
        return any(
            normalize_plain_language_exclusion(item) == wanted
            for item in annotation.plain_language
        )

    @history_preview
    def plan(
        self, image, value, *, value_kind: str, source_field: str,
        write_decision: ExternalWriteDecision | None = None,
        _history_operation=None,
    ) -> ManagedMetadataCorrectionPlan:
        image_path = Path(image).resolve()
        decision = write_decision or ExternalWriteDecision()
        clean = str(value or "").strip()
        sidecar = image_path.with_suffix(".tags.json")
        recovery_error=""
        if _history_operation is None and decision.backup_enabled and decision.backup_directory is not None:
            try:
                self.recover_pending(image_path,decision.backup_directory)
                self._paired.recover_pending_paired(
                    image_path,decision.backup_directory,
                )
            except Exception as exc:
                recovery_error=str(exc)
        source_fingerprint = file_fingerprint(image_path)
        authority_fingerprint = file_fingerprint(sidecar)
        common = dict(
            image=image_path, source_path=image_path, source_kind="managed_metadata",
            source_field=str(source_field or ""), value_kind=str(value_kind or ""),
            value=clean, source_fingerprint=source_fingerprint,
            authority_fingerprint=authority_fingerprint, write_decision=decision,
        )
        if source_field not in {"XMP:Subject", "XMP:Description"}:
            return ManagedMetadataCorrectionPlan(
                disposition="unsupported", detail="Only app-managed XMP:Subject and XMP:Description can be corrected.", **common,
            )
        if (source_field == "XMP:Subject") != (value_kind == "tag") or (
            source_field == "XMP:Description"
        ) != (value_kind == "description"):
            return ManagedMetadataCorrectionPlan(
                disposition="unsupported", detail="The requested value kind does not match this managed Metadata field.", **common,
            )
        if not clean:
            return ManagedMetadataCorrectionPlan(
                disposition="blocked", detail="Choose one exact Metadata value to correct.", **common,
            )
        if recovery_error:
            return ManagedMetadataCorrectionPlan(
                disposition="blocked",
                detail="Interrupted Metadata correction needs recovery: "+recovery_error,
                **common,
            )
        try:
            if not source_fingerprint.get("exists"):
                raise FileNotFoundError("The source image is unavailable.")
            if image_path.is_symlink():
                raise RuntimeError("The source image is a symbolic link and was not staged.")
            if _readonly(image_path):
                raise PermissionError("The source image is read only and was not staged.")
            values = self._metadata().read_embedded(image_path)
            pixel_fingerprint=self._pixel_fingerprint(image_path)
            if not pixel_fingerprint:
                raise RuntimeError("Image pixels could not be fingerprinted safely.")
            preserved = self._preserved(values,source_field)
            if source_field == "XMP:Subject":
                original = _subject_values(
                    dict(values.get("tag_fields") or {}).get("Subject")
                )
                wanted = normalize_tag(clean)
                matches = [item for item in original if normalize_tag(item) == wanted]
                updated = [item for item in original if normalize_tag(item) != wanted]
                original_text = ", ".join(original)
                updated_text = ", ".join(updated)
            else:
                raw = dict(values.get("description_fields") or {}).get("Description")
                if not isinstance(raw, str) or not raw.strip():
                    raise ValueError("Managed XMP:Description is unavailable.")
                original, separator = _description_values(raw)
                wanted = normalize_plain_language_exclusion(clean)
                matches = [item for item in original if normalize_plain_language_exclusion(item) == wanted]
                updated = [item for item in original if normalize_plain_language_exclusion(item) != wanted]
                original_text = raw
                updated_text = separator.join(updated)
            if not matches:
                disposition, detail = "current", "The exact value is no longer present in this managed Metadata field."
            elif len(matches) != 1:
                disposition, detail = "held", "The exact value occurs more than once; no ambiguous Metadata removal was staged."
            elif decision.backup_enabled and decision.backup_directory is None:
                disposition, detail = "blocked", "Protected Metadata correction requires the shared external-write backup folder."
            else:
                disposition, detail = "change", f"Remove one exact value from {source_field} only. Image Knowledge and other Metadata fields remain unchanged."
            confirmed=self._confirmed(image_path,clean,value_kind)
            paired_disposition="unsupported"; paired_present_values=()
            paired_absent_value=""; paired_recovery_plan=None
            paired_detail=(
                "This value is not a confirmed Image Knowledge fact. Remove it "
                "from managed Metadata only, or review the image data first."
            )
            if disposition=="change" and confirmed:
                if not decision.backup_enabled:
                    paired_disposition="blocked"
                    paired_detail=(
                        "This two-authority correction requires a verified recovery "
                        "copy of both managed Metadata and Image Knowledge. Turn on "
                        "external-write protection, then preview it again."
                    )
                else:
                    record=ImageRecord(image_path)
                    (
                        paired_disposition,paired_detail,paired_present_values,
                        paired_absent_value,paired_recovery_plan,
                    )=self._paired._paired_image_knowledge_plan(
                        record,clean,value_kind=value_kind,
                    )
                    if paired_disposition=="change":
                        paired_detail=(
                            f"Remove the exact {source_field} value, then "
                            +paired_detail
                        )
            elif confirmed and disposition!="change":
                paired_disposition=disposition
                paired_detail=(
                    "The paired correction is held because this exact managed "
                    "Metadata source is not writable from the frozen preview."
                )
            false_label=(
                "The description is false for this image"
                if value_kind=="description" else
                "The fact is false for this image"
            )
            choices=(
                SourceCorrectionChoice(
                    SOURCE_ONLY,"Remove only from this managed Metadata field",
                    detail,"dataset.source_correction.preview",
                    "available" if disposition=="change" else "unavailable",
                ),
                SourceCorrectionChoice(
                    PAIRED_IMAGE_KNOWLEDGE,false_label,paired_detail,
                    "dataset.source_correction.preview",
                    "available" if paired_disposition=="change" else "unavailable",
                ),
            )
            payload = {
                "image": str(image_path), "field": source_field, "kind": value_kind,
                "value": clean, "source": source_fingerprint, "authority": authority_fingerprint,
                "original": original, "updated": updated, "decision": decision.fingerprint(),
                "preserved": _json_hash(preserved),"pixels":pixel_fingerprint,
                "disposition": disposition,
                "paired_disposition":paired_disposition,
                "paired_annotation_sha256":(
                    _json_hash(paired_recovery_plan.materialize())
                    if paired_recovery_plan is not None else ""
                ),
            }
            return ManagedMetadataCorrectionPlan(
                disposition=disposition, detail=detail, original_text=original_text,
                updated_text=updated_text, original_values=tuple(original),
                updated_values=tuple(updated), occurrence_count=len(matches),
                confirmed_in_image_knowledge=confirmed,
                plan_fingerprint=_json_hash(payload),
                preservation_fingerprint=_json_hash(preserved),
                pixel_fingerprint=pixel_fingerprint,choices=choices,
                paired_disposition=paired_disposition,
                paired_detail=paired_detail,
                paired_present_values=paired_present_values,
                paired_absent_value=paired_absent_value,
                paired_recovery_plan=paired_recovery_plan,**common,
            )
        except Exception as exc:
            return ManagedMetadataCorrectionPlan(
                disposition="blocked", detail=str(exc), **common,
            )

    def _validate(self, plan: ManagedMetadataCorrectionPlan) -> bool:
        current = self.plan(
            plan.image, plan.value, value_kind=plan.value_kind,
            source_field=plan.source_field, write_decision=plan.write_decision,
        )
        return current.plan_fingerprint == plan.plan_fingerprint and current.will_write == plan.will_write

    def _semantic_matches(self, path: Path, plan: ManagedMetadataCorrectionPlan) -> bool:
        if self._pixel_fingerprint(path)!=plan.pixel_fingerprint:
            return False
        values = self._metadata().read_embedded(path)
        if _json_hash(
            self._preserved(values,plan.source_field)
        ) != plan.preservation_fingerprint:
            return False
        if plan.source_field == "XMP:Subject":
            try:
                actual=tuple(_subject_values(
                    dict(values.get("tag_fields") or {}).get("Subject")
                ))
            except ValueError:
                return False
        else:
            raw = dict(values.get("description_fields") or {}).get("Description")
            if raw is not None and not isinstance(raw, str):
                return False
            actual, _separator = _description_values(raw)
            actual = tuple(actual)
        return tuple(actual) == plan.updated_values

    @staticmethod
    def _manifest_bytes(payload: dict[str, object]) -> bytes:
        return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    def _native_prepare(self,context,plan,backup):
        """Use the shared proof/publication adapter, changing only one XMP field."""
        from types import SimpleNamespace
        from .history_projection import prepare_projection
        metadata=self._metadata();before=metadata.read_embedded(plan.image)
        tags=plan.updated_values if plan.source_field=="XMP:Subject" else _subject_values(before.get("tag_fields",{}).get("Subject"))
        description=plan.updated_text if plan.source_field=="XMP:Description" else str(before.get("description_fields",{}).get("Description","") or "")
        class OneFieldWriter:
            def read_embedded(_self,path):return metadata.read_embedded(path)
            def write_xmp(_self,path,_tags,_description):
                if plan.source_field=="XMP:Subject":metadata.write_managed_xmp(path,tags=list(plan.updated_values))
                else:metadata.write_managed_xmp(path,description=plan.updated_text)
        preview=SimpleNamespace(embedded_standard_metadata=before.get("standard_metadata",{}),
            embedded_generation_fields=before.get("raw_generation_fields",{}),
            expected_metadata_tags=tags,expected_metadata_descriptions=(description,))
        prepared=prepare_projection(context,plan.image,OneFieldWriter(),preview,backup)
        if not self._semantic_matches(Path(prepared.intent["staged"]),plan):
            raise RuntimeError("The staged correction changed another managed field or failed exact read-back.")
        return prepared

    @history_apply
    def apply(
        self, plan: ManagedMetadataCorrectionPlan,
        execution: ExternalWriteExecution | None = None,
        *,meaning: str=SOURCE_ONLY,
    ) -> ManagedMetadataCorrectionResult:
        if meaning==PAIRED_IMAGE_KNOWLEDGE:
            return self.apply_paired(plan,execution=execution)
        if meaning!=SOURCE_ONLY:
            return self._result(
                plan,"blocked","This source-correction meaning is unsupported.",
                meaning=meaning,
            )
        if not plan.will_write:
            return self._result(plan,plan.disposition,plan.detail)
        execution = execution or plan.write_decision.execution()
        issue = external_write_validation_issue(plan.write_decision, execution)
        if issue:
            detail = "This exact Local Only boundary crossing was not confirmed." if issue == "privacy_confirmation_required" else "The frozen external-write protection changed after preview."
            return self._result(
                plan,"blocked" if issue=="privacy_confirmation_required" else "stale",
                detail,
            )
        if not self._validate(plan):
            return self._result(
                plan,"stale","Managed Metadata or Image Knowledge changed after preview.",
            )
        token=uuid4().hex
        staged=plan.image.with_name(
            f".{plan.image.stem}.{token}.metadata-source-stage{plan.image.suffix}"
        )
        rollback=plan.image.with_name(
            f".{plan.image.stem}.{token}.metadata-source-rollback{plan.image.suffix}"
        )
        operation_id = f"metadata_source_correction_{uuid4().hex}"
        directory = manifest = None
        journal = None
        after: dict[str, object] = {}
        try:
            copy_verified(
                plan.image,rollback,expected=plan.source_fingerprint,
                label="Managed Metadata operation-local rollback",
            )
            copy_verified(
                plan.image,staged,expected=plan.source_fingerprint,
                label="Managed Metadata correction staging",
            )
            kwargs = {"tags": list(plan.updated_values)} if plan.source_field == "XMP:Subject" else {"description": plan.updated_text}
            self._metadata().write_managed_xmp(staged, **kwargs)
            if not self._semantic_matches(staged, plan):
                raise RuntimeError("Managed Metadata semantic read-back verification failed on the staged image.")
            after = file_fingerprint(staged)
            if not same_content(file_fingerprint(plan.image), plan.source_fingerprint) or not same_content(file_fingerprint(plan.image.with_suffix(".tags.json")), plan.authority_fingerprint):
                return self._result(
                    plan,"stale","The source image or Image Knowledge changed "
                    "before the managed Metadata write.",
                )
            if plan.backup_enabled:
                root = self._root(plan.backup_directory); root.mkdir(parents=True, exist_ok=True)
                directory = root / operation_id; directory.mkdir(exist_ok=False)
                backup = directory / "image.before"; copy_verified(plan.image, backup, expected=plan.source_fingerprint, label="Managed Metadata image backup")
                manifest = directory / "recovery.json"
                payload = {"kind": self.RECOVERY_KIND, "status": "applying", "operation_id": operation_id, "plan_fingerprint": plan.plan_fingerprint, "image": str(plan.image), "backup": backup.name, "before": plan.source_fingerprint, "after": after}
                atomic_verified_bytes(manifest, self._manifest_bytes(payload), label="Managed Metadata recovery record")
                journal = OperationJournal(root / "operation_journal.jsonl")
                journal_id = journal.begin(self.JOURNAL_KIND, expected=1, reason="Remove one exact managed Metadata source value", metadata={"recovery": str(manifest)})
                payload["journal_operation_id"] = journal_id
                atomic_verified_bytes(manifest, self._manifest_bytes(payload), label="Managed Metadata recovery record")
            os.replace(staged, plan.image)
            if not self._semantic_matches(plan.image, plan) or not same_content(file_fingerprint(plan.image), after):
                raise RuntimeError("Managed Metadata correction failed semantic read-back verification.")
            if manifest is not None:
                payload["status"] = "complete"
                atomic_verified_bytes(manifest, self._manifest_bytes(payload), label="Managed Metadata recovery record")
                journal.append_change(payload["journal_operation_id"], item=str(plan.image), before=plan.source_fingerprint, after=after, reason="Verified exact managed Metadata source correction")
                journal.finish(payload["journal_operation_id"], result={"recovery": str(manifest)})
            return self._result(
                plan,"complete",plan.detail,operation_id=operation_id,
                recovery_manifest=manifest,
            )
        except Exception as exc:
            current = file_fingerprint(plan.image)
            rollback_error=""
            restored=same_content(current,plan.source_fingerprint)
            if after and same_content(current,after):
                try:
                    if not same_content(
                        file_fingerprint(rollback),plan.source_fingerprint,
                    ):
                        raise RuntimeError(
                            "The operation-local Metadata rollback copy is invalid."
                        )
                    os.replace(rollback,plan.image)
                    restored=same_content(
                        file_fingerprint(plan.image),plan.source_fingerprint,
                    )
                except Exception as rollback_exc:
                    rollback_error=str(rollback_exc)
            elif not restored:
                rollback_error=(
                    "The source image changed again; automatic rollback did not "
                    "overwrite the newer bytes."
                )
            if manifest is not None:
                try:
                    payload["status"]=(
                        "restored_after_failure" if restored and not rollback_error
                        else "recovery_required"
                    )
                    atomic_verified_bytes(manifest, self._manifest_bytes(payload), label="Managed Metadata recovery record")
                    if journal is not None:
                        journal.finish(
                            payload["journal_operation_id"],"failed",
                            message=str(exc)+(
                                " Automatic rollback failed: "+rollback_error
                                if rollback_error else ""
                            ),
                        )
                except Exception:
                    pass
            return self._result(
                plan,"failed",str(exc)+(
                    " Automatic rollback failed: "+rollback_error
                    if rollback_error else
                    " Original image bytes were restored." if restored else ""
                ),operation_id=operation_id,
                recovery_manifest=manifest,
            )
        finally:
            if staged.exists():
                staged.unlink()
            if rollback.exists():
                rollback.unlink()

    @history_apply
    def apply_paired(
        self,plan: ManagedMetadataCorrectionPlan,
        execution: ExternalWriteExecution|None=None,
    ) -> ManagedMetadataCorrectionResult:
        """Apply one protected managed-Metadata and Image Knowledge correction."""

        if plan.paired_disposition!="change" or plan.paired_recovery_plan is None:
            return self._result(
                plan,plan.paired_disposition,plan.paired_detail,
                meaning=PAIRED_IMAGE_KNOWLEDGE,
            )
        if not plan.backup_enabled or plan.backup_directory is None:
            return self._result(
                plan,"blocked","A paired correction requires verified recovery "
                "for both managed Metadata and Image Knowledge.",
                meaning=PAIRED_IMAGE_KNOWLEDGE,
            )
        execution=execution or plan.write_decision.execution()
        issue=external_write_validation_issue(plan.write_decision,execution)
        if issue:
            return self._result(
                plan,
                "blocked" if issue=="privacy_confirmation_required" else "stale",
                (
                    "This exact Local Only boundary crossing was not confirmed."
                    if issue=="privacy_confirmation_required" else
                    "The frozen external-write protection changed after preview."
                ),
                meaning=PAIRED_IMAGE_KNOWLEDGE,
            )
        if not self._validate(plan):
            return self._result(
                plan,"stale","Managed Metadata or Image Knowledge changed after preview.",
                meaning=PAIRED_IMAGE_KNOWLEDGE,
            )

        token=uuid4().hex
        staged=plan.image.with_name(
            f".{plan.image.stem}.{token}.metadata-paired-stage{plan.image.suffix}"
        )
        operation_id=f"source_correction_{uuid4().hex}"
        directory: Path|None=None
        manifest: Path|None=None
        journal=None; journal_id=""
        payload: dict[str,object]={}
        image_recovery_manifest=None
        source_before=plan.source_fingerprint
        source_after: dict[str,object]={}
        sidecar=plan.image.with_suffix(".tags.json")
        sidecar_before=plan.authority_fingerprint
        intended=self._paired._paired_intended_annotation(plan)
        intended_fingerprint=_json_hash(intended)
        try:
            copy_verified(
                plan.image,staged,expected=source_before,
                label="Managed Metadata paired correction staging",
            )
            kwargs=(
                {"tags":list(plan.updated_values)}
                if plan.source_field=="XMP:Subject" else
                {"description":plan.updated_text}
            )
            self._metadata().write_managed_xmp(staged,**kwargs)
            if not self._semantic_matches(staged,plan):
                raise RuntimeError(
                    "Managed Metadata semantic read-back verification failed "
                    "on the paired staged image."
                )
            source_after=file_fingerprint(staged)
            if (
                not same_content(file_fingerprint(plan.image),source_before)
                or not same_content(file_fingerprint(sidecar),sidecar_before)
            ):
                staged.unlink()
                return self._result(
                    plan,"stale","Managed Metadata or Image Knowledge changed "
                    "before protected recovery was created.",
                    meaning=PAIRED_IMAGE_KNOWLEDGE,
                )

            root=self._root(plan.backup_directory)
            directory=root/operation_id
            directory.mkdir(parents=True,exist_ok=False)
            source_backup=directory/"managed-metadata-image.before"
            copy_verified(
                plan.image,source_backup,expected=source_before,
                label="Managed Metadata paired backup",
            )
            sidecar_backup=directory/"image-knowledge.tags.json.before"
            sidecar_existed=bool(sidecar_before.get("exists"))
            if sidecar_existed:
                copy_verified(
                    sidecar,sidecar_backup,expected=sidecar_before,
                    label="Image Knowledge paired backup",
                )
            manifest=directory/"recovery.json"
            payload={
                "schema_version":2,
                "kind":SourceCorrectionService.RECOVERY_KIND,
                "status":"applying","operation_id":operation_id,
                "plan_fingerprint":plan.plan_fingerprint,
                "meaning":PAIRED_IMAGE_KNOWLEDGE,
                "source_kind":"managed_metadata",
                "source_field":plan.source_field,
                "value_kind":plan.value_kind,
                "image_path":str(plan.image),
                "source_path":str(plan.image),
                "backup":source_backup.name,
                "before":source_before,"after":source_after,
                "image_knowledge_path":str(sidecar),
                "image_knowledge_backup":sidecar_backup.name,
                "image_knowledge_existed":sidecar_existed,
                "image_knowledge_before":sidecar_before,
                "image_knowledge_after_json_sha256":intended_fingerprint,
                "manifest_path":str(manifest),
            }
            atomic_verified_bytes(
                manifest,self._manifest_bytes(payload),
                label="Paired source-correction recovery record",
            )
            journal_path=root/"operation_journal.jsonl"
            if journal_path.is_symlink():
                raise RuntimeError(
                    "The source-correction journal cannot be a symbolic link."
                )
            journal=OperationJournal(journal_path)
            journal_id=journal.begin(
                "source_correction",expected=2,
                reason="Correct one false image fact and its managed Metadata value",
                metadata={
                    "recovery":str(manifest),
                    "plan_fingerprint":plan.plan_fingerprint,
                    "source_kind":"managed_metadata",
                    "source_field":plan.source_field,
                    "value_kind":plan.value_kind,
                    "meaning":PAIRED_IMAGE_KNOWLEDGE,
                },
            )
            payload["journal_operation_id"]=journal_id
            atomic_verified_bytes(
                manifest,self._manifest_bytes(payload),
                label="Paired source-correction recovery record",
            )
        except Exception as exc:
            if journal is not None and journal_id:
                try:
                    journal.finish(journal_id,"failed",message=str(exc))
                except Exception:
                    pass
            if directory is not None:
                shutil.rmtree(directory,ignore_errors=True)
            if staged.exists():
                staged.unlink()
            return self._result(
                plan,"failed",str(exc),operation_id=operation_id,
                meaning=PAIRED_IMAGE_KNOWLEDGE,
            )

        try:
            journal.append_change(
                journal_id,item=str(sidecar),before=sidecar_before,
                after={"json_sha256":intended_fingerprint},
                reason="Record exact negative decision in Image Knowledge",
            )
            journal.append_change(
                journal_id,item=str(plan.image),before=source_before,
                after=source_after,
                reason=f"Remove the exact false value from {plan.source_field}",
            )
            image_result=self._paired.recovery_service.apply(
                plan.paired_recovery_plan,knowledge=self.knowledge,
            )
            image_recovery_manifest=image_result.recovery_manifest
            if not self._paired._paired_semantic_matches(image_result.record,plan):
                raise RuntimeError(
                    "The paired Image Knowledge correction failed semantic "
                    "read-back verification."
                )
            payload["image_knowledge_recovery"]=str(image_recovery_manifest)
            payload["image_knowledge_after"]=file_fingerprint(sidecar)
            atomic_verified_bytes(
                manifest,self._manifest_bytes(payload),
                label="Paired source-correction recovery record",
            )
            if not same_content(file_fingerprint(plan.image),source_before):
                raise RuntimeError(
                    "Managed Metadata changed again before the paired write. "
                    "The newer source image was not overwritten."
                )
            if (
                not same_content(
                    file_fingerprint(sidecar),
                    dict(payload["image_knowledge_after"]),
                )
                or not self._paired._sidecar_matches_intended(sidecar,intended)
            ):
                raise RuntimeError(
                    "Image Knowledge changed again before the paired Metadata "
                    "write. The newer record was not overwritten."
                )
            os.replace(staged,plan.image)
            if (
                not self._semantic_matches(plan.image,plan)
                or not same_content(file_fingerprint(plan.image),source_after)
            ):
                raise RuntimeError(
                    "Paired managed Metadata correction failed semantic read-back "
                    "verification."
                )
            if (
                not same_content(
                    file_fingerprint(sidecar),
                    dict(payload["image_knowledge_after"]),
                )
                or not self._paired._sidecar_matches_intended(sidecar,intended)
            ):
                raise RuntimeError(
                    "Image Knowledge changed again after the paired Metadata "
                    "write. The transaction was not marked complete."
                )
            payload["status"]="complete"
            payload["after"]=file_fingerprint(plan.image)
            atomic_verified_bytes(
                manifest,self._manifest_bytes(payload),
                label="Paired source-correction recovery record",
            )
            journal.finish(journal_id,result={"recovery":str(manifest)})
            return self._result(
                plan,"complete",plan.paired_detail,operation_id=operation_id,
                recovery_manifest=manifest,meaning=PAIRED_IMAGE_KNOWLEDGE,
                image_knowledge_recovery_manifest=image_recovery_manifest,
            )
        except Exception as exc:
            rollback_errors=[]
            try:
                backup=(manifest.parent/str(payload["backup"])).resolve()
                current=file_fingerprint(plan.image)
                if not (
                    same_content(current,source_before)
                    or same_content(current,source_after)
                ):
                    raise RuntimeError(
                        "Managed Metadata changed again; paired rollback did not "
                        "overwrite the newer image."
                    )
                atomic_verified_bytes(
                    plan.image,backup.read_bytes(),
                    label="Paired managed Metadata rollback",
                )
            except Exception as restore_exc:
                rollback_errors.append(str(restore_exc))
            try:
                self._paired._restore_paired_sidecar(plan,payload)
            except Exception as restore_exc:
                rollback_errors.append(str(restore_exc))
            payload["status"]=(
                "restored_after_failure" if not rollback_errors
                else "recovery_required"
            )
            payload["rollback_errors"]=rollback_errors
            try:
                atomic_verified_bytes(
                    manifest,self._manifest_bytes(payload),
                    label="Paired source-correction recovery record",
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
                detail+=" Managed Metadata and Image Knowledge were restored."
            try:
                journal.finish(journal_id,"failed",message=detail)
            except Exception as journal_exc:
                detail+=f" Journal failure could not be recorded: {journal_exc}"
            return self._result(
                plan,"failed",detail,operation_id=operation_id,
                recovery_manifest=manifest,meaning=PAIRED_IMAGE_KNOWLEDGE,
                image_knowledge_recovery_manifest=image_recovery_manifest,
            )
        finally:
            if staged.exists():
                staged.unlink()

    @history_restore
    def restore_paired(self,plan: ManagedMetadataCorrectionPlan,manifest):
        return self._paired.restore_paired(plan,manifest)

    def recover_pending(self, image, backup_directory) -> tuple[Path, ...]:
        operation=self._history.operation(image)
        if operation is not None:return self._history.recover(operation,image)
        image_path = Path(image).resolve(); root = self._root(Path(backup_directory))
        if not root.exists():
            return ()
        recovered = []
        for manifest in root.glob("metadata_source_correction_*/recovery.json"):
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            if payload.get("kind") != self.RECOVERY_KIND or payload.get("status") not in {"applying", "recovery_required"} or Path(str(payload.get("image") or "")).resolve() != image_path:
                continue
            if manifest.parent.name != str(payload.get("operation_id") or ""):
                raise RuntimeError("Interrupted Metadata correction has an invalid recovery folder.")
            backup = (manifest.parent / str(payload.get("backup") or "")).resolve()
            if backup.parent != manifest.parent.resolve():
                raise RuntimeError("Interrupted Metadata correction backup escapes its recovery folder.")
            before = dict(payload.get("before") or {}); after = dict(payload.get("after") or {})
            current = file_fingerprint(image_path)
            if same_content(current, after):
                payload["status"] = "complete_after_interruption"
            elif same_content(current, before):
                payload["status"] = "restored_after_interruption"
            else:
                payload["status"] = "recovery_required"
                atomic_verified_bytes(manifest, self._manifest_bytes(payload), label="Managed Metadata recovery record")
                raise RuntimeError("Managed Metadata changed after interruption; automatic recovery was held.")
            if not same_content(file_fingerprint(backup), before):
                raise RuntimeError("Interrupted Metadata correction backup failed verification.")
            atomic_verified_bytes(manifest, self._manifest_bytes(payload), label="Managed Metadata recovery record")
            journal_id = str(payload.get("journal_operation_id") or "")
            if journal_id:
                journal = OperationJournal(root / "operation_journal.jsonl")
                if payload["status"] == "complete_after_interruption":
                    journal.finish(journal_id, result={"recovery": str(manifest), "interrupted": True})
                else:
                    journal.finish(journal_id, "failed", message="Image remained at pre-write bytes after interruption")
            recovered.append(manifest)
        return tuple(recovered)

    @history_restore
    def restore(self, plan: ManagedMetadataCorrectionPlan, manifest) -> ManagedMetadataCorrectionRestoreResult:
        path = Path(manifest).resolve()
        staged=plan.image.with_name(
            f".{plan.image.stem}.{uuid4().hex}.metadata-restore-stage"
            f"{plan.image.suffix}"
        )
        try:
            root = self._root(plan.backup_directory)
            if path.parent.parent != root.resolve():
                raise RuntimeError("The recovery manifest is outside the configured protected backup root.")
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("kind") != self.RECOVERY_KIND or payload.get("plan_fingerprint") != plan.plan_fingerprint:
                raise RuntimeError("The recovery does not match this frozen Metadata correction plan.")
            if path.parent.name != str(payload.get("operation_id") or "") or Path(str(payload.get("image") or "")).resolve() != plan.image:
                raise RuntimeError("The recovery manifest does not belong to this image operation.")
            if payload.get("status") == "restored":
                return ManagedMetadataCorrectionRestoreResult("already_restored", "Managed Metadata was already restored.", plan.source_path)
            if payload.get("status") not in {"complete", "complete_after_interruption"}:
                raise RuntimeError("The Metadata correction did not reach a restorable state.")
            if not same_content(file_fingerprint(plan.image), dict(payload.get("after") or {})):
                return ManagedMetadataCorrectionRestoreResult("held", "Managed Metadata changed again; restore was held.", plan.source_path)
            backup = (path.parent / str(payload.get("backup") or "")).resolve()
            before = dict(payload.get("before") or {})
            if backup.parent != path.parent.resolve() or not same_content(file_fingerprint(backup), before):
                raise RuntimeError("The Metadata correction backup is missing or invalid.")
            copy_verified(
                backup,staged,expected=before,
                label="Managed Metadata correction restore staging",
            )
            if not same_content(
                file_fingerprint(plan.image),dict(payload.get("after") or {}),
            ):
                return ManagedMetadataCorrectionRestoreResult(
                    "held","Managed Metadata changed again; restore was held.",
                    plan.source_path,
                )
            os.replace(staged,plan.image)
            if not same_content(file_fingerprint(plan.image), before):
                raise RuntimeError("Managed Metadata restore failed exact read-back verification.")
            payload["status"] = "restored"
            atomic_verified_bytes(path, self._manifest_bytes(payload), label="Managed Metadata recovery record")
            journal_id = str(payload.get("journal_operation_id") or "")
            if journal_id:
                OperationJournal(root / "operation_journal.jsonl").finish(journal_id, "undone", message="Restored exact managed Metadata bytes")
            return ManagedMetadataCorrectionRestoreResult("restored", "Restored the exact pre-correction managed Metadata image.", plan.source_path)
        except Exception as exc:
            return ManagedMetadataCorrectionRestoreResult("failed", str(exc), plan.source_path)
        finally:
            if staged.exists():
                staged.unlink()
