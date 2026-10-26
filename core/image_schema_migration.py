from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass,field
from datetime import datetime,timezone
from pathlib import Path
from typing import Callable,Iterable
from uuid import uuid4

from .annotation import SCHEMA_VERSION,ImageAnnotation
from .image_knowledge_reconciliation import _fingerprint
from .image_record import ImageRecord,_atomic_write_text
from .history_versions import HistoryError,_reject_reparse
from .image_knowledge_history import HistoryWriteInterrupted


_SCHEMA_PATTERN=re.compile(rb'"schema_version"\s*:\s*(\d+)')


@dataclass(frozen=True)
class ImageSchemaMigrationPreview:
    image: Path
    sidecar: Path
    source_version: int
    fingerprint: dict[str,object]


@dataclass
class ImageSchemaMigrationIssue:
    sidecar: Path
    error: str


@dataclass
class ImageSchemaMigrationPlan:
    previews: list[ImageSchemaMigrationPreview]=field(default_factory=list)
    issues: list[ImageSchemaMigrationIssue]=field(default_factory=list)
    cancelled: bool=False


@dataclass
class ImageSchemaMigrationResult:
    detected: int=0
    upgraded: int=0
    retained: int=0
    failed: int=0
    checkpoint_failed: int=0
    cancelled: bool=False
    issues: list[ImageSchemaMigrationIssue]=field(default_factory=list)
    backup_directory: Path|None=None
    recovery_manifest: Path|None=None
    history_operation_ids: list[str]=field(default_factory=list)
    history_coverage: list[dict]=field(default_factory=list)
    history_interrupted: bool=False
    history_held: bool=False
    recovery_manifests: list[Path]=field(default_factory=list)


class ImageSchemaMigrationService:
    """Losslessly upgrade old per-image sidecars with backup and read-back checks."""

    def preview_many(
        self,images: Iterable[str|Path],*,
        progress: Callable[[int,int,str],None]|None=None,
        should_cancel: Callable[[],bool]|None=None,
    ) -> ImageSchemaMigrationPlan:
        items=[Path(value) for value in images]
        plan=ImageSchemaMigrationPlan()
        for index,image in enumerate(items,start=1):
            if should_cancel and should_cancel():
                plan.cancelled=True
                break
            sidecar=image.with_suffix(".tags.json")
            if sidecar.is_file():
                try:
                    with sidecar.open("rb") as stream:
                        head=stream.read(4096)
                    match=_SCHEMA_PATTERN.search(head)
                    source_version=int(match.group(1)) if match else 1
                    if source_version < SCHEMA_VERSION:
                        payload=json.loads(sidecar.read_text(encoding="utf-8"))
                        if not isinstance(payload,dict):
                            raise ValueError("the JSON root is not an object")
                        ImageAnnotation.from_dict(payload,image.name)
                        plan.previews.append(ImageSchemaMigrationPreview(
                            image,sidecar,source_version,_fingerprint(sidecar),
                        ))
                    elif source_version > SCHEMA_VERSION:
                        plan.issues.append(ImageSchemaMigrationIssue(
                            sidecar,
                            f"schema version {source_version} is newer than this application",
                        ))
                except Exception as exc:
                    plan.issues.append(ImageSchemaMigrationIssue(sidecar,str(exc)))
            if progress and (index==len(items) or index%100==0):
                progress(index,len(items),image.name)
        return plan

    def apply_many(
        self,plan: ImageSchemaMigrationPlan,*,root: str|Path,
        progress: Callable[[int,int,str],None]|None=None,
        should_cancel: Callable[[],bool]|None=None,
        on_complete: Callable[[ImageSchemaMigrationPreview,ImageRecord],None]|None=None,
        retained: int=0,
        history_operation=None,
        history_operation_factory=None,
    ) -> ImageSchemaMigrationResult:
        if history_operation is not None and history_operation_factory is not None:
            raise ValueError("Supply one exact history operation or a bounded factory.")
        if history_operation is not None or history_operation_factory is not None:
            return self._apply_configured_many(plan,root=Path(root),progress=progress,
                should_cancel=should_cancel,on_complete=on_complete,retained=retained,
                history_operation=history_operation,history_operation_factory=history_operation_factory)
        result=ImageSchemaMigrationResult(
            detected=len(plan.previews),retained=max(0,int(retained)),
            failed=len(plan.issues),cancelled=plan.cancelled,
            issues=list(plan.issues),
        )
        if plan.cancelled or not plan.previews:
            return result
        root=Path(root)
        stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup=root/".tagmanager-backups"/f"schema-v{SCHEMA_VERSION}_{stamp}_{uuid4().hex[:8]}"
        backup.mkdir(parents=True,exist_ok=False)
        result.backup_directory=backup
        entries=[]
        manifest=backup/"recovery.json"
        result.recovery_manifest=manifest
        for index,preview in enumerate(plan.previews,start=1):
            if should_cancel and should_cancel():
                result.cancelled=True
                break
            relative=self._relative_name(preview.sidecar,root,index)
            saved=backup/relative
            saved.parent.mkdir(parents=True,exist_ok=True)
            entry={
                "original":str(preview.sidecar.resolve()),
                "backup":str(saved.resolve()),
                "source_version":preview.source_version,
                "status":"pending",
            }
            entries.append(entry)
            try:
                if _fingerprint(preview.sidecar)!=preview.fingerprint:
                    raise RuntimeError("record changed after the migration preview")
                shutil.copy2(preview.sidecar,saved)
                record=ImageRecord(preview.image)
                expected=record.annotation.to_dict()
                record.save_sidecar(force=True)
                written=json.loads(preview.sidecar.read_text(encoding="utf-8"))
                if int(written.get("schema_version",0)) < SCHEMA_VERSION:
                    raise RuntimeError("schema version failed read-back verification")
                verified_record=ImageRecord(preview.image)
                if verified_record.annotation.to_dict()!=expected:
                    raise RuntimeError("migrated knowledge failed semantic read-back verification")
                entry["status"]="verified"; result.upgraded+=1
            except Exception as exc:
                result.failed+=1
                result.issues.append(ImageSchemaMigrationIssue(preview.sidecar,str(exc)))
                if saved.exists():
                    shutil.copy2(saved,preview.sidecar)
                    entry["status"]="recovered"
                else:
                    entry["status"]="skipped"
            else:
                if on_complete:
                    try:
                        on_complete(preview,verified_record)
                    except Exception as exc:
                        result.failed+=1; result.checkpoint_failed+=1
                        result.issues.append(ImageSchemaMigrationIssue(
                            preview.sidecar,
                            "The schema upgrade was verified, but its rebuildable "
                            f"local completion checkpoint could not be saved: {exc}",
                        ))
            self._write_manifest(manifest,root,entries)
            if progress:
                progress(index,len(plan.previews),preview.image.name)
        return result

    def _apply_configured_many(self,plan,*,root,progress,should_cancel,on_complete,
                               retained,history_operation,history_operation_factory):
        result=ImageSchemaMigrationResult(detected=len(plan.previews),retained=max(0,int(retained)),
            failed=len(plan.issues),cancelled=plan.cancelled,issues=list(plan.issues))
        if plan.cancelled:return result
        for start in range(0,len(plan.previews),64):
            if should_cancel and should_cancel():result.cancelled=True; break
            chunk=plan.previews[start:start+64]
            try:
                operation=(history_operation_factory(tuple(item.image for item in chunk))
                    if history_operation_factory is not None else history_operation)
            except Exception as exc:
                result.cancelled=isinstance(exc,InterruptedError)
                result.history_held=not result.cancelled
                if not result.cancelled:
                    result.failed+=len(chunk)
                    result.issues.extend(ImageSchemaMigrationIssue(item.sidecar,f"Saved history held migration: {exc}") for item in chunk)
                break
            if operation is None:
                legacy=self.apply_many(ImageSchemaMigrationPlan(list(chunk)),root=root,
                    progress=(lambda index,total,name:progress(start+index,len(plan.previews),name)) if progress else None,
                    should_cancel=should_cancel,on_complete=on_complete)
                for key in ("upgraded","failed","checkpoint_failed"):setattr(result,key,getattr(result,key)+getattr(legacy,key))
                result.issues.extend(legacy.issues)
                result.backup_directory=legacy.backup_directory
                result.recovery_manifest=legacy.recovery_manifest
                if legacy.recovery_manifest:result.recovery_manifests.append(legacy.recovery_manifest)
                if legacy.cancelled:result.cancelled=True; break
                continue
            result.history_operation_ids.append(operation.operation_id)
            for offset,preview in enumerate(chunk,1):
                if should_cancel and should_cancel():result.cancelled=True; break
                coverage={"image":str(preview.image),"operation_id":operation.operation_id,"status":"held","version_ids":[]}
                result.history_coverage.append(coverage)
                write_started=False
                try:
                    context=operation.context_for(preview.image)
                    if context is None or context.operation_id!=operation.operation_id:
                        raise HistoryError("Migration lacks its exact admitted history context.")
                    with context.locked(preview.image):
                        if context.store.has_pending(context.entity):
                            raise HistoryWriteInterrupted("Reopen this image in its data editor for saved-history recovery before migration.")
                        if _fingerprint(preview.sidecar)!=preview.fingerprint:
                            raise HistoryError("Image Knowledge changed after the migration preview.")
                        backup=context.store.scope.history_root/"migration-recovery"/operation.operation_id/context.entity.entity_id
                        _reject_reparse(backup)
                        if not backup.resolve().is_relative_to(context.store.scope.history_root):
                            raise HistoryError("Migration recovery escaped its private history storage.")
                        backup.mkdir(parents=True,exist_ok=False)
                        saved=backup/preview.sidecar.name
                        shutil.copy2(preview.sidecar,saved)
                        if _fingerprint(saved)["sha256"]!=preview.fingerprint["sha256"]:
                            raise HistoryError("Migration recovery copy failed exact verification.")
                        manifest=backup/"recovery.json"
                        entry={"original":str(preview.sidecar),"backup":str(saved),
                            "source_version":preview.source_version,"status":"native_history_prepared"}
                        self._write_manifest(manifest,root,[entry])
                        result.backup_directory=backup;result.recovery_manifest=manifest
                        result.recovery_manifests.append(manifest)
                        record=ImageRecord(preview.image,history_context=context)
                        expected=record.annotation.to_dict()
                        write_started=True
                        record.save_sidecar(force=True)
                        verified_record=ImageRecord(preview.image,history_context=context)
                        if verified_record.annotation.to_dict()!=expected:
                            raise HistoryWriteInterrupted("Migration was saved but semantic verification failed; review exact history recovery.")
                        head=context.store.current_head(context.entity)
                        if head is None or head.operation_id!=operation.operation_id:
                            raise HistoryWriteInterrupted("The saved migration history could not be verified.")
                        coverage.update(status="complete",version_ids=[head.version_id],entity_id=context.entity.entity_id)
                        result.upgraded+=1
                        entry["status"]="verified"
                        self._write_manifest(manifest,root,[entry])
                except Exception as exc:
                    result.cancelled=isinstance(exc,InterruptedError)
                    result.history_held=not result.cancelled
                    result.history_interrupted=write_started or isinstance(exc,HistoryWriteInterrupted)
                    coverage.update(status="interrupted" if result.history_interrupted else "held",error=str(exc))
                    if not result.cancelled:
                        result.failed+=1
                        result.issues.append(ImageSchemaMigrationIssue(preview.sidecar,
                            f"{exc} Current data was retained; reopen this exact image for saved-history recovery before retrying."))
                    return result
                if on_complete:
                    try:on_complete(preview,verified_record)
                    except Exception as exc:
                        result.failed+=1;result.checkpoint_failed+=1
                        result.issues.append(ImageSchemaMigrationIssue(preview.sidecar,f"Upgrade verified; its rebuildable completion checkpoint failed: {exc}"))
                if progress:progress(start+offset,len(plan.previews),preview.image.name)
            if result.cancelled:break
        return result

    @staticmethod
    def _relative_name(sidecar: Path,root: Path,index: int) -> Path:
        try: return sidecar.resolve().relative_to(root.resolve())
        except ValueError: return Path(f"{index:06d}_{sidecar.name}")

    @staticmethod
    def _write_manifest(manifest: Path,root: Path,entries: list[dict]) -> None:
        _atomic_write_text(manifest,json.dumps({
            "schema_version":1,
            "migration_target":SCHEMA_VERSION,
            "root":str(root.resolve()),
            "created_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "files":entries,
        },indent=2,ensure_ascii=False)+"\n")
