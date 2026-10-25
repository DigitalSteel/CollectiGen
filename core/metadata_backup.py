from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass,field
from datetime import datetime,timezone
from pathlib import Path
from typing import Callable,Iterable
from uuid import uuid4

from .image_knowledge_reconciliation import (
    ImageKnowledgeReconciliationService,ReconciliationPreview,
)
from .external_write_transaction import (
    ExternalWriteDecision,copy_verified,external_write_validation_issue,
    file_fingerprint,write_verified_json,
)
from .operation_journal import OperationJournal


def _file_fingerprint(path: str|Path) -> dict[str,object]:
    return file_fingerprint(path)


def _scope_fingerprint(images: Iterable[Path]) -> str:
    payload=[str(Path(image).resolve()) for image in images]
    return hashlib.sha256(json.dumps(
        payload,ensure_ascii=False,separators=(",",":"),
    ).encode("utf-8")).hexdigest()


def _write_manifest(path: Path,payload: dict[str,object]) -> None:
    write_verified_json(
        path,payload,label="Metadata backup recovery record",
    )


def _copy_verified(source: Path,destination: Path,expected: dict[str,object]) -> None:
    copy_verified(
        source,destination,expected=expected,label="Metadata recovery",
    )


def _available_bytes(path: Path) -> int|None:
    probe=path
    while not probe.exists() and probe!=probe.parent:
        probe=probe.parent
    try:
        return int(shutil.disk_usage(probe).free)
    except OSError:
        return None


@dataclass
class MetadataBackupPlanItem:
    image: Path
    disposition: str
    detail: str
    preview: ReconciliationPreview|None=None


@dataclass
class MetadataBackupPlan:
    items: list[MetadataBackupPlanItem]=field(default_factory=list)
    scope_fingerprint: str=""
    backup_enabled: bool=True
    backup_directory: Path|None=None
    external_policy_fingerprint: str=""
    privacy_confirmation_required: bool=False
    privacy_message: str=""
    required_free_bytes: int=0
    available_free_bytes: int|None=None
    cancelled: bool=False
    history_operations: dict|None=field(default=None,repr=False)

    def count(self,disposition: str) -> int:
        return sum(item.disposition==disposition for item in self.items)

    @property
    def write_count(self) -> int:
        return self.count("change")

    @property
    def space_sufficient(self) -> bool:
        return (
            not self.backup_enabled
            or self.available_free_bytes is not None
            and self.available_free_bytes>=self.required_free_bytes
        )

    @property
    def write_decision(self) -> ExternalWriteDecision:
        return ExternalWriteDecision.from_values(
            policy_fingerprint=self.external_policy_fingerprint,
            backup_enabled=self.backup_enabled,
            backup_directory=self.backup_directory,
            privacy_confirmation_required=self.privacy_confirmation_required,
            privacy_message=self.privacy_message,
        )


@dataclass
class MetadataBackupResultItem:
    image: Path
    disposition: str
    detail: str=""
    recovered: bool=False


@dataclass
class MetadataBackupResult:
    items: list[MetadataBackupResultItem]=field(default_factory=list)
    cancelled: bool=False
    recovery_manifest: Path|None=None
    operation_id: str=""
    native_history: bool=False

    def count(self,disposition: str) -> int:
        return sum(item.disposition==disposition for item in self.items)

    @property
    def changed(self) -> int:
        return self.count("verified")

    @property
    def current(self) -> int:
        return self.count("current")

    @property
    def failed(self) -> int:
        return self.count("failed")

    @property
    def recovered(self) -> int:
        return sum(item.recovered for item in self.items)


@dataclass
class MetadataBackupRestoreResult:
    recovery_manifest: Path|None=None
    restored: list[Path]=field(default_factory=list)
    already_restored: list[Path]=field(default_factory=list)
    held: list[Path]=field(default_factory=list)
    errors: list[str]=field(default_factory=list)


class MetadataBackupService:
    """Plan and execute the Dataset-owned managed Metadata backup job."""

    SCHEMA_VERSION=1
    RECOVERY_KIND="collectigen_metadata_backup_recovery"
    JOURNAL_KIND="metadata_backup"
    MANAGED_FIELDS=("XMP:Subject","XMP:Description")

    def __init__(
        self,reconciliation: ImageKnowledgeReconciliationService,
        *,journal_path: str|Path|None=None,
        history_operation_factory=None,
    ):
        self.reconciliation=reconciliation
        self.history_operation_factory=history_operation_factory
        self.operation_journal=(
            OperationJournal(journal_path) if journal_path is not None else None
        )

    @staticmethod
    def recovery_root(backup_directory: str|Path) -> Path:
        return Path(backup_directory).absolute()/"metadata-backup"

    @classmethod
    def _journal(cls,backup_directory: str|Path) -> OperationJournal:
        return OperationJournal(cls.recovery_root(backup_directory)/"operations.jsonl")

    def preview(
        self,images: Iterable[str|Path],*,backup_enabled: bool,
        backup_directory: str|Path|None=None,
        external_policy_fingerprint: str="",
        progress: Callable[[int,int,str],None]|None=None,
        should_cancel: Callable[[],bool]|None=None,
    ) -> MetadataBackupPlan:
        ordered=[]; seen=set()
        for value in images:
            image=Path(value)
            key=os.path.normcase(str(image.resolve()))
            if key in seen:
                continue
            seen.add(key); ordered.append(image)
        plan=MetadataBackupPlan(
            scope_fingerprint=_scope_fingerprint(ordered),
            backup_enabled=bool(backup_enabled),
            backup_directory=(
                Path(backup_directory).absolute()
                if backup_enabled and backup_directory is not None else None
            ),
            external_policy_fingerprint=str(external_policy_fingerprint or ""),
            history_operations=self._capture_operations(ordered),
        )
        for index,image in enumerate(ordered,1):
            if should_cancel and should_cancel():
                plan.cancelled=True
                break
            sidecar=image.with_suffix(".tags.json")
            if not sidecar.is_file():
                item=MetadataBackupPlanItem(
                    image,"missing","No saved Image Knowledge record is available.",
                )
            else:
                try:
                    preview=self.reconciliation.preview(image,include_metadata=True)
                    difference=preview.difference("metadata")
                    if preview.errors:
                        disposition="blocked"; detail="; ".join(preview.errors)
                    elif difference is None or not difference.available or difference.error:
                        disposition="unsupported"
                        detail=(
                            difference.error if difference is not None and difference.error
                            else "Managed Metadata is unavailable for this image."
                        )
                    elif difference.will_write:
                        disposition="change"
                        detail=(
                            f"XMP:Subject +{len(difference.added_tags)}/-{len(difference.removed_tags)} • "
                            f"XMP:Description +{len(difference.added_descriptions)}/-{len(difference.removed_descriptions)}"
                        )
                    else:
                        disposition="current"
                        detail="Managed XMP:Subject and XMP:Description are already current."
                    item=MetadataBackupPlanItem(image,disposition,detail,preview)
                except Exception as exc:
                    item=MetadataBackupPlanItem(image,"blocked",str(exc))
            plan.items.append(item)
            if progress:
                progress(index,len(ordered),image.name)
        if plan.backup_enabled:
            for item in plan.items:
                if item.disposition!="change" or item.preview is None:
                    continue
                plan.required_free_bytes+=(
                    int(item.preview.image_fingerprint.get("size",0) or 0)
                    +int(item.preview.authority_fingerprint.get("size",0) or 0)
                )
            plan.required_free_bytes+=max(64*1024,len(plan.items)*2048)
            if plan.backup_directory is not None:
                plan.available_free_bytes=_available_bytes(plan.backup_directory)
        return plan

    def _create_recovery(
        self,plan: MetadataBackupPlan,*,native: bool=False,
    ) -> tuple[Path,dict[str,object],OperationJournal|None,str]:
        if plan.backup_directory is None:
            raise ValueError("A verified Metadata backup destination is required.")
        root=self.recovery_root(plan.backup_directory)
        if root.is_symlink():
            raise ValueError("The Metadata recovery folder cannot be a symbolic link.")
        root.mkdir(parents=True,exist_ok=True)
        stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        directory=root/f"operation_{stamp}_{uuid4().hex[:8]}"
        directory.mkdir(parents=False,exist_ok=False)
        entries=[]
        for index,item in enumerate(
            value for value in plan.items if value.disposition=="change"
        ):
            if item.preview is None:
                raise ValueError("A changed Metadata target has no frozen preview.")
            image=item.image.resolve()
            sidecar=image.with_suffix(".tags.json")
            if _file_fingerprint(image)!=item.preview.image_fingerprint:
                raise ValueError(
                    f"{image.name} changed after preview; nothing was written."
                )
            if _file_fingerprint(sidecar)!=item.preview.authority_fingerprint:
                raise ValueError(
                    f"{sidecar.name} changed after preview; nothing was written."
                )
            image_backup=f"{index:05d}_image{image.suffix}"
            sidecar_backup=f"{index:05d}_authority.tags.json"
            _copy_verified(image,directory/image_backup,item.preview.image_fingerprint)
            _copy_verified(
                sidecar,directory/sidecar_backup,item.preview.authority_fingerprint,
            )
            entries.append({
                "image":str(image),"sidecar":str(sidecar),
                "image_backup":image_backup,"sidecar_backup":sidecar_backup,
                "before_image":item.preview.image_fingerprint,
                "before_sidecar":item.preview.authority_fingerprint,
                "after_image":{},"after_sidecar":{},"state":"pending",
            })
        journal=None if native else self._journal(plan.backup_directory)
        operation_id=uuid4().hex if native else journal.begin(
            self.JOURNAL_KIND,expected=len(entries),
            reason="Back up complete confirmed Tags to managed image Metadata",
            metadata={"scope_fingerprint":plan.scope_fingerprint},
        )
        payload={
            "schema_version":2 if native else self.SCHEMA_VERSION,"kind":self.RECOVERY_KIND,
            "status":"applying","created_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "scope_fingerprint":plan.scope_fingerprint,
            "external_policy_fingerprint":plan.external_policy_fingerprint,
            "operation_id":operation_id,"managed_fields":list(self.MANAGED_FIELDS),
            "entries":entries,
            "native_history":native,
        }
        manifest=directory/"recovery.json"
        _write_manifest(manifest,payload)
        return manifest,payload,journal,operation_id

    def apply(
        self,plan: MetadataBackupPlan,*,privacy_confirmed: bool=False,
        external_policy_fingerprint: str="",
        progress: Callable[[int,int,str],None]|None=None,
        should_cancel: Callable[[],bool]|None=None,
    ) -> MetadataBackupResult:
        decision=plan.write_decision
        validation_issue=external_write_validation_issue(
            decision,decision.execution(
                current_policy_fingerprint=external_policy_fingerprint,
                privacy_confirmed=privacy_confirmed,
            ),
        )
        if validation_issue in {"stale_decision","stale_policy"}:
            raise ValueError(
                "The shared external-write protection changed after preview."
            )
        if validation_issue=="privacy_confirmation_required":
            raise ValueError("The exact cross-zone Metadata backup was not confirmed.")
        if not plan.space_sufficient:
            raise OSError("The configured backup destination has insufficient verified free space.")
        result=MetadataBackupResult()
        recovery=None; journal=None
        changed=[item for item in plan.items if item.disposition=="change"]
        operations=self._plan_operations(plan,[item.image for item in changed])
        native=bool(operations)
        result.native_history=native
        # Admit every frozen member before external backups or mutable loads.
        # A pending image cannot fall through to an untracked legacy write.
        for image,operation in operations.items():
            context=operation.context_for(image)
            with context.locked(image):
                if context.store.has_pending(context.entity):
                    raise ValueError("An image has interrupted saved history; recover that exact image before Metadata backup.")
        if plan.backup_enabled and changed:
            result.recovery_manifest,recovery,journal,result.operation_id=(
                self._create_recovery(plan,native=native)
            )
        elif changed and self.operation_journal is not None and not native:
            journal=self.operation_journal
            result.operation_id=journal.begin(
                self.JOURNAL_KIND,expected=len(changed),
                reason="Back up complete confirmed Tags to managed image Metadata",
                metadata={
                    "scope_fingerprint":plan.scope_fingerprint,
                    "durable_recovery":False,
                },
            )
        recovery_by_image={
            str(Path(entry["image"]).resolve()):entry
            for entry in (recovery or {}).get("entries",[])
        }
        total=len(plan.items)
        for index,item in enumerate(plan.items,1):
            if should_cancel and should_cancel():
                result.cancelled=True
                for pending in plan.items[index-1:]:
                    result.items.append(MetadataBackupResultItem(
                        pending.image,"not_processed","Cancelled before this image.",
                    ))
                break
            if item.disposition!="change" or item.preview is None:
                result.items.append(MetadataBackupResultItem(
                    item.image,item.disposition,item.detail,
                ))
            else:
                temporary=None; temporary_entry=None; temporary_manifest=None
                if not plan.backup_enabled and journal is not None:
                    temporary,temporary_manifest,temporary_entry=(
                        self._create_operation_local_recovery(item)
                    )
                try:
                    applied=self.reconciliation.apply(
                        item.preview,targets=("metadata",),durable_backup=False,
                        **({"history_operation":operations[item.image]} if native else {}),
                    )
                except Exception:
                    if temporary is not None:
                        temporary.cleanup()
                    raise
                if applied.errors:
                    result.items.append(MetadataBackupResultItem(
                        item.image,"failed","; ".join(applied.errors),applied.recovered,
                    ))
                    entry=recovery_by_image.get(str(item.image.resolve()))
                    if entry is not None:
                        entry["state"]="failed"
                elif "metadata" in applied.verified_targets:
                    after_image=_file_fingerprint(item.image)
                    after_sidecar=_file_fingerprint(item.image.with_suffix(".tags.json"))
                    result.items.append(MetadataBackupResultItem(
                        item.image,"verified",
                        "XMP:Subject and XMP:Description changed and passed read-back verification.",
                    ))
                    entry=recovery_by_image.get(str(item.image.resolve()))
                    if entry is not None:
                        entry["state"]="written"
                        entry["after_image"]=after_image
                        entry["after_sidecar"]=after_sidecar
                        try:
                            if native:
                                context=operations[item.image].context_for(item.image)
                                head=context.store.current_head(context.entity)
                                if head is None or head.operation_id!=context.operation_id:
                                    raise ValueError("Metadata completion lacks its exact saved-history receipt.")
                                entry["history"]={"entity_id":context.entity.entity_id,
                                    "scope_key":context.entity.scope_key,"head":head.version_id,
                                    "before_version":head.parent_id,"source_token":context.entity.source_token}
                            recovery["entries"]=list(recovery_by_image.values())
                            _write_manifest(result.recovery_manifest,recovery)
                            if journal is not None: journal.append_change(
                                result.operation_id,item=str(item.image.resolve()),
                                before={
                                    "image":entry["before_image"],
                                    "sidecar":entry["before_sidecar"],
                                },
                                after={"image":after_image,"sidecar":after_sidecar},
                                reason="Verified managed Metadata backup",
                                metadata={"fields":list(self.MANAGED_FIELDS)},
                            )
                        except Exception as bookkeeping_exc:
                            recovered=False; recovery_error=""
                            if native:
                                recovery_error=" Current data and private saved-history evidence were retained; no whole-record rollback was attempted."
                            else:
                                try:
                                    self._restore_entry(result.recovery_manifest,entry)
                                    recovered=True; entry["state"]="failed"
                                except Exception as restore_exc:
                                    recovery_error=(f" Automatic recovery failed: {restore_exc}")
                            try:
                                recovery["entries"]=list(recovery_by_image.values())
                                _write_manifest(result.recovery_manifest,recovery)
                            except Exception:
                                pass
                            result.items[-1]=MetadataBackupResultItem(
                                item.image,"failed",
                                "Metadata changed, but its durable recovery record "
                                f"could not be completed: {bookkeeping_exc}."
                                +recovery_error,recovered,
                            )
                    elif journal is not None:
                        try:
                            journal.append_change(
                                result.operation_id,
                                item=hashlib.sha256(str(
                                    item.image.resolve()
                                ).encode("utf-8")).hexdigest(),
                                before={
                                    "image":item.preview.image_fingerprint,
                                    "sidecar":item.preview.authority_fingerprint,
                                },
                                after={"image":after_image,"sidecar":after_sidecar},
                                reason="Verified managed Metadata backup",
                                metadata={
                                    "fields":list(self.MANAGED_FIELDS),
                                    "durable_recovery":False,
                                },
                            )
                        except Exception as bookkeeping_exc:
                            recovered=False; recovery_error=""
                            try:
                                self._restore_entry(
                                    temporary_manifest,temporary_entry,
                                )
                                recovered=True
                            except Exception as restore_exc:
                                recovery_error=(
                                    f" Automatic recovery failed: {restore_exc}"
                                )
                            result.items[-1]=MetadataBackupResultItem(
                                item.image,"failed",
                                "Metadata changed, but its operation journal "
                                f"could not be completed: {bookkeeping_exc}."
                                +recovery_error,recovered,
                            )
                else:
                    result.items.append(MetadataBackupResultItem(
                        item.image,"current","Managed Metadata became current before writing.",
                    ))
                if temporary is not None:
                    temporary.cleanup()
                if native and (applied.errors or result.items[-1].disposition=="failed"):
                    for pending in plan.items[index:]:
                        result.items.append(MetadataBackupResultItem(pending.image,"not_processed",
                            "Stopped after an incomplete Metadata image; review its saved history before continuing."))
                    break
            if progress:
                progress(index,total,item.image.name)
        status=(
            "cancelled" if result.cancelled else
            "failed" if result.failed else "complete"
        )
        if recovery is not None and result.recovery_manifest is not None:
            recovery["status"]=status
            recovery["result"]={
                "verified":result.changed,"current":result.current,
                "failed":result.failed,"cancelled":result.cancelled,
            }
            _write_manifest(result.recovery_manifest,recovery)
            if journal is not None: journal.finish(
                result.operation_id,recovery["status"],result=recovery["result"],
            )
        elif journal is not None and result.operation_id:
            journal.finish(
                result.operation_id,status,result={
                    "verified":result.changed,"current":result.current,
                    "failed":result.failed,"cancelled":result.cancelled,
                    "durable_recovery":False,
                },
            )
        return result

    def _capture_operations(self,images):
        if self.history_operation_factory is None or not images:
            return {}
        operations={}; legacy=False
        for offset in range(0,len(images),64):
            members=tuple(images[offset:offset+64])
            operation=self.history_operation_factory(members)
            if operation is None:
                legacy=True
            else:
                operations.update((image,operation) for image in members)
        if operations and legacy:
            raise ValueError("Select one group with the same saved-history participation before Metadata backup.")
        return operations

    def _plan_operations(self,plan,images):
        if self.history_operation_factory is None:
            return {}
        if plan.history_operations is None:
            raise ValueError("Preview this exact Metadata scope again to freeze saved-history authority.")
        if not plan.history_operations:
            # Recheck opt-out participation; enabling capture after preview must
            # not silently perform a legacy write under the new configuration.
            if self._capture_operations(images):
                raise ValueError("Saved-history participation changed; preview Metadata again.")
            return {}
        if any(image not in plan.history_operations for image in images):
            raise ValueError("A Metadata image is outside its frozen history scope.")
        return {image:plan.history_operations[image] for image in images}

    @staticmethod
    def _create_operation_local_recovery(
        item: MetadataBackupPlanItem,
    ) -> tuple[tempfile.TemporaryDirectory,Path,dict[str,object]]:
        if item.preview is None:
            raise ValueError("A changed Metadata target has no frozen preview.")
        temporary=tempfile.TemporaryDirectory(
            prefix="collectigen-metadata-operation-rollback-"
        )
        directory=Path(temporary.name)
        image=item.image.resolve(); sidecar=image.with_suffix(".tags.json")
        image_backup="image"+image.suffix
        sidecar_backup="authority.tags.json"
        _copy_verified(image,directory/image_backup,item.preview.image_fingerprint)
        _copy_verified(
            sidecar,directory/sidecar_backup,item.preview.authority_fingerprint,
        )
        entry={
            "image":str(image),"sidecar":str(sidecar),
            "image_backup":image_backup,"sidecar_backup":sidecar_backup,
            "before_image":item.preview.image_fingerprint,
            "before_sidecar":item.preview.authority_fingerprint,
        }
        return temporary,directory/"operation-local.json",entry

    @staticmethod
    def _restore_entry(manifest: Path,entry: dict[str,object]) -> None:
        for backup_key,before_key,target_key in (
            ("image_backup","before_image","image"),
            ("sidecar_backup","before_sidecar","sidecar"),
        ):
            backup=(manifest.parent/str(entry[backup_key])).resolve()
            target=Path(str(entry[target_key])).resolve()
            expected=dict(entry.get(before_key) or {})
            temporary=target.with_name(
                f".{target.name}.{uuid4().hex}.metadata-rollback.tmp"
            )
            try:
                shutil.copy2(backup,temporary)
                if _file_fingerprint(temporary).get("sha256")!=expected.get("sha256"):
                    raise OSError("A staged Metadata rollback copy failed verification.")
                os.replace(temporary,target)
                if _file_fingerprint(target).get("sha256")!=expected.get("sha256"):
                    raise OSError("A Metadata rollback target failed verification.")
            finally:
                if temporary.exists():
                    temporary.unlink()

    def recovery_manifests_for_plan(
        self,plan: MetadataBackupPlan,*,statuses: Iterable[str]=(),
    ) -> list[Path]:
        if plan.backup_directory is None:
            return []
        root=self.recovery_root(plan.backup_directory)
        if not root.exists():
            return []
        if root.is_symlink():
            raise ValueError("The Metadata recovery folder redirects elsewhere.")
        wanted=set(statuses)
        values=[]
        for manifest in root.glob("operation_*/recovery.json"):
            if manifest.is_symlink() or manifest.parent.parent.resolve()!=root.resolve():
                raise ValueError("A Metadata recovery record redirects outside its recovery folder.")
            payload=self._recovery_payload(manifest)
            if (
                payload.get("scope_fingerprint")!=plan.scope_fingerprint
                or wanted and payload.get("status") not in wanted
                or not any(entry.get("state")=="written" for entry in payload.get("entries",[]))
            ):
                continue
            values.append(manifest)
        return sorted(values,key=lambda value:value.stat().st_mtime_ns)

    def latest_recovery_for_plan(
        self,plan: MetadataBackupPlan,*,statuses: Iterable[str]=(),
    ) -> Path|None:
        values=self.recovery_manifests_for_plan(plan,statuses=statuses)
        return values[-1] if values else None

    def _recovery_payload(
        self,manifest: Path,plan: MetadataBackupPlan|None=None,
    ) -> dict[str,object]:
        try:
            payload=json.loads(manifest.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError("A Metadata recovery record could not be verified.") from exc
        if (
            not isinstance(payload,dict)
            or payload.get("schema_version") not in {self.SCHEMA_VERSION,2}
            or (payload.get("schema_version")==2)!=bool(payload.get("native_history"))
            or payload.get("kind")!=self.RECOVERY_KIND
            or plan is not None
            and payload.get("scope_fingerprint")!=plan.scope_fingerprint
            or not isinstance(payload.get("entries"),list)
            or not all(isinstance(entry,dict) for entry in payload["entries"])
        ):
            raise ValueError("A Metadata recovery record has an invalid or mismatched contract.")
        return payload

    def restore_latest_for_plan(
        self,plan: MetadataBackupPlan,
        *,progress: Callable[[int,int,str],None]|None=None,
    ) -> MetadataBackupRestoreResult:
        manifest=self.latest_recovery_for_plan(
            plan,statuses=("applying","complete","cancelled","failed"),
        )
        if manifest is None:
            raise ValueError("No protected Metadata operation matches this exact image scope.")
        payload=self._recovery_payload(manifest,plan)
        if payload.get("native_history"):
            return self._restore_native(plan,manifest,payload,progress=progress)
        # A legacy receipt cannot authorize a raw replacement of an image that
        # now participates in identity-qualified history.
        if self._plan_operations(plan,[item.image for item in plan.items]):
            raise ValueError("This older Metadata backup has no qualified image identity receipt. Preserve it and use the exact image's saved-history recovery.")
        expected={str(item.image.resolve()) for item in plan.items}
        entries=[entry for entry in payload.get("entries",[]) if entry.get("state")=="written"]
        seen=set(); prepared=[]
        result=MetadataBackupRestoreResult(recovery_manifest=manifest)
        for entry in entries:
            image=Path(str(entry.get("image") or "")).resolve()
            sidecar=Path(str(entry.get("sidecar") or "")).resolve()
            if str(image) not in expected or sidecar!=image.with_suffix(".tags.json"):
                raise ValueError("Metadata recovery contains a target outside the frozen plan.")
            if str(image) in seen:
                raise ValueError("Metadata recovery repeats an image target.")
            seen.add(str(image))
            backups=[]
            for key,before_key,target in (
                ("image_backup","before_image",image),
                ("sidecar_backup","before_sidecar",sidecar),
            ):
                name=str(entry.get(key) or "")
                backup=(manifest.parent/name).resolve()
                if (
                    not name or backup.parent!=manifest.parent.resolve()
                    or backup.is_symlink() or not backup.is_file()
                    or _file_fingerprint(backup).get("sha256")
                    !=dict(entry.get(before_key) or {}).get("sha256")
                ):
                    raise ValueError("Metadata recovery backup is missing or invalid.")
                after_key=(
                    "after_image" if target==image else "after_sidecar"
                )
                backups.append((
                    backup,target,dict(entry.get(before_key) or {}),
                    dict(entry.get(after_key) or {}),
                ))
            current_image=_file_fingerprint(image)
            current_sidecar=_file_fingerprint(sidecar)
            before_image=dict(entry.get("before_image") or {})
            before_sidecar=dict(entry.get("before_sidecar") or {})
            if current_image==before_image and current_sidecar==before_sidecar:
                result.already_restored.append(image)
                continue
            if (
                current_image!=dict(entry.get("after_image") or {})
                or current_sidecar!=dict(entry.get("after_sidecar") or {})
            ):
                result.held.append(image)
                continue
            prepared.append((entry,image,backups))
        if result.held:
            return result
        staged=[]; rollback_staged=[]; replaced=[]
        try:
            for _entry,_image,backups in prepared:
                for backup,target,before,after in backups:
                    temporary=target.with_name(
                        f".{target.name}.{uuid4().hex}.metadata-restore.tmp"
                    )
                    shutil.copy2(backup,temporary)
                    if _file_fingerprint(temporary).get("sha256")!=before.get("sha256"):
                        raise OSError("A staged Metadata restore copy failed verification.")
                    staged.append((temporary,target,before,after))
                    rollback=target.with_name(
                        f".{target.name}.{uuid4().hex}.metadata-restore-current.tmp"
                    )
                    shutil.copy2(target,rollback)
                    if _file_fingerprint(rollback).get("sha256")!=after.get("sha256"):
                        raise OSError("A staged current Metadata copy failed verification.")
                    rollback_staged.append((rollback,target,after))
            rollback_by_target={target:value for value,target,_after in rollback_staged}
            for index,(entry,image,_backups) in enumerate(prepared,1):
                pairs=staged[(index-1)*2:index*2]
                for temporary,target,before,after in pairs:
                    os.replace(temporary,target)
                    replaced.append((rollback_by_target[target],target,after))
                    if _file_fingerprint(target).get("sha256")!=before.get("sha256"):
                        raise OSError("A restored Metadata target failed verification.")
                entry["state"]="restored"
                result.restored.append(image)
                if progress:
                    progress(index,len(prepared),image.name)
        except Exception as exc:
            rollback_errors=[]
            for rollback,target,after in reversed(replaced):
                try:
                    os.replace(rollback,target)
                    if _file_fingerprint(target).get("sha256")!=after.get("sha256"):
                        raise OSError("rollback read-back did not match")
                except Exception as rollback_exc:
                    rollback_errors.append(f"{target.name}: {rollback_exc}")
            result.restored.clear()
            detail=(
                " Metadata restore rollback also failed: "+"; ".join(rollback_errors)
                if rollback_errors else " All staged changes were rolled back."
            )
            raise RuntimeError(str(exc)+detail) from exc
        finally:
            for temporary,_target,_before,_after in staged:
                try:
                    if temporary.exists():
                        temporary.unlink()
                except OSError:
                    pass
            for rollback,_target,_after in rollback_staged:
                try:
                    if rollback.exists():
                        rollback.unlink()
                except OSError:
                    pass
        payload["status"]="restored"
        payload["entries"]=payload.get("entries",[])
        _write_manifest(manifest,payload)
        operation_id=str(payload.get("operation_id") or "")
        if operation_id and plan.backup_directory is not None:
            self._journal(plan.backup_directory).finish(
                operation_id,"undone",result={"restored":len(result.restored)},
            )
        return result

    def _restore_native(self,plan,manifest,payload,*,progress=None):
        from dataclasses import replace
        from .annotation import ImageAnnotation,SCHEMA_VERSION
        from .history_projection import prepare_projection_from_backup,publish_projection
        from .history_versions import read_record,_reject_reparse
        from .image_record_recovery import ImageRecordRecoveryService

        result=MetadataBackupRestoreResult(recovery_manifest=manifest)
        expected={str(item.image.resolve()) for item in plan.items}
        entries=[entry for entry in payload["entries"] if entry.get("state")=="written"]
        images=[Path(str(entry.get("image") or "")).resolve() for entry in entries]
        if len(set(images))!=len(images) or any(str(image) not in expected for image in images):
            raise ValueError("Metadata recovery contains repeated or out-of-scope images.")
        operations=self._plan_operations(plan,images)
        if images and len(operations)!=len(images):
            raise ValueError("Restore requires the original configured saved-history authority.")
        from .history_workflow import HistoryCaptureOperation
        fresh={}
        for image,operation in operations.items():
            # The frozen plan must remain valid before a distinct restore
            # operation is admitted; reuse no apply operation identity.
            from .history_configuration import path_key
            descriptor=operation.members[path_key(image)]
            with operation.configuration.write_guard(descriptor,path=image):
                key=id(operation)
                if key not in fresh:
                    members=[value for value in images if operations[value] is operation]
                    fresh[key]=HistoryCaptureOperation(operation.configuration,members,
                        operation_kind="managed_metadata_restore",reason="Restore the exact protected Metadata operation")
        operations={image:fresh[id(operation)] for image,operation in operations.items()}
        prepared=[]
        for entry,image in zip(entries,images):
            try:
                if Path(str(entry.get("sidecar") or "")).resolve()!=image.with_suffix(".tags.json"):
                    raise ValueError("Metadata recovery sidecar is outside its exact image.")
                context=operations[image].context_for(image)
                with context.locked(image):
                    if context.store.has_pending(context.entity):
                        raise ValueError("Recover this image's interrupted saved history before Metadata restore.")
                    authority=entry.get("history") or {}
                    if (authority.get("entity_id")!=context.entity.entity_id
                            or authority.get("scope_key")!=context.entity.scope_key):
                        raise ValueError("Metadata recovery belongs to a different saved image identity.")
                    backups={}
                    for key,before in (("image_backup","before_image"),("sidecar_backup","before_sidecar")):
                        candidate=(manifest.parent/str(entry.get(key) or "")).resolve()
                        if (candidate.parent!=manifest.parent.resolve() or candidate.is_symlink()
                                or not candidate.is_file()
                                or _file_fingerprint(candidate).get("sha256")!=entry[before].get("sha256")):
                            raise ValueError("Metadata recovery backup is missing or changed.")
                        backups[key]=candidate
                    baseline=context.store.version(context.entity,authority.get("before_version",""))
                    baseline_raw=context.store.payload(context.entity,baseline.snapshot)
                    if baseline_raw!=backups["sidecar_backup"].read_bytes():
                        raise ValueError("Metadata recovery record differs from its saved-history baseline.")
                    baseline_value=json.loads(baseline_raw)
                    if (baseline_value.get("schema_version")!=SCHEMA_VERSION
                            or ImageAnnotation.from_dict(baseline_value,image.name).to_dict()!=baseline_value):
                        raise ValueError("Metadata restore cannot coerce unsupported saved Image Knowledge.")
                    head=context.store.current_head(context.entity)
                    marker=entry.get("restore") or {}
                    # Restart after canonical completion only finalizes the
                    # manifest when its exact operation and current bytes agree.
                    if (marker and head and head.operation_id==marker.get("operation_id")
                            and head.parent_id==authority.get("head")
                            and context.store.payload(context.entity,head.snapshot)==read_record(image.with_suffix(".tags.json"))
                            and context.entity.source_token==marker.get("new_token")
                            and _file_fingerprint(image).get("sha256")==entry["before_image"].get("sha256")):
                        prepared.append((entry,image,context,backups,baseline_raw,"complete"))
                        continue
                    if not head or head.version_id!=authority.get("head"):
                        raise ValueError("Saved image history changed after the Metadata operation.")
                    if _file_fingerprint(image.with_suffix(".tags.json"))!=entry.get("after_sidecar"):
                        raise ValueError("Newer Image Knowledge must be preserved.")
                    state="new"
                    if marker:
                        binding=context.store.binding(entity_id=context.entity.entity_id)
                        if (context.entity.source_token==marker.get("new_token")
                                and binding.get("receipt_id")==marker.get("receipt_id")
                                and _file_fingerprint(image).get("sha256")==entry["before_image"].get("sha256")):
                            state="published"
                        elif context.entity.source_token!=authority.get("source_token"):
                            raise ValueError("Metadata restore no longer matches its exact publication receipt.")
                    if state=="new" and (_file_fingerprint(image)!=entry.get("after_image")
                            or context.entity.source_token!=authority.get("source_token")):
                        raise ValueError("Newer or replaced image data must be preserved.")
                    prepared.append((entry,image,context,backups,baseline_raw,state))
            except Exception as exc:
                result.held.append(image);result.errors.append(str(exc))
        if result.held:
            return result
        for index,(entry,image,context,backups,baseline_raw,state) in enumerate(prepared,1):
            try:
                with context.locked(image):
                    authority=entry["history"]
                    if state!="complete":
                        head=context.store.current_head(context.entity)
                        if (not head or head.version_id!=authority["head"]
                                or _file_fingerprint(image.with_suffix(".tags.json"))!=entry["after_sidecar"]):
                            raise ValueError("Saved authority changed before Metadata restore publication.")
                        expected_record=read_record(image.with_suffix(".tags.json"))
                        if state=="new":
                            directory=context.store.scope.history_root/"metadata-restores"/uuid4().hex
                            _reject_reparse(directory)
                            if not directory.resolve().is_relative_to(context.store.scope.history_root):
                                raise ValueError("Metadata restoration recovery escaped configured private history.")
                            directory.mkdir(parents=True,exist_ok=False)
                            current=directory/("current"+image.suffix)
                            candidate=directory/("historical"+image.suffix)
                            _copy_verified(image,current,entry["after_image"])
                            _copy_verified(backups["image_backup"],candidate,entry["before_image"])
                            projection=prepare_projection_from_backup(context,image,self.reconciliation.metadata,
                                candidate_backup=candidate,expected_candidate_sha256=entry["before_image"]["sha256"],
                                current_backup=current)
                            entry["restore"]={"operation_id":context.operation_id,
                                "receipt_id":projection.identifier,"new_token":projection.intent["new_token"]}
                            _write_manifest(manifest,payload)
                            renewed=publish_projection(context,image,projection)
                            operations[image].replace_context(image,context,renewed)
                            context=renewed
                        else:
                            context=replace(context,operation_id=entry["restore"]["operation_id"])
                        context=replace(context,restored_from=authority["before_version"],restored_units=("record",))
                        ImageRecordRecoveryService(history_context=context,
                            backup_root=context.store.scope.history_root/"metadata-record-restores").restore_saved_record(
                                image,expected=expected_record,intended=baseline_raw)
                    entry["state"]="restored"
                    _write_manifest(manifest,payload)
                    result.restored.append(image)
                if progress:
                    progress(index,len(prepared),image.name)
            except Exception as exc:
                result.held.extend(value[1] for value in prepared[index-1:])
                result.errors.append("Metadata restore stopped; completed images and exact recovery evidence were retained. "+str(exc))
                break
        payload["status"]="restored" if not result.held else "failed"
        _write_manifest(manifest,payload)
        return result
