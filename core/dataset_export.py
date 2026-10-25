from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass,field
from datetime import datetime,timezone
from pathlib import Path,PurePath
from typing import Callable,Iterable
from uuid import uuid4

from .character_database import CharacterDatabase
from .export_profile import ExportProfile
from .external_write_transaction import (
    ExternalWriteDecision,atomic_verified_bytes,external_write_validation_issue,
)
from .image_record import ImageRecord,annotation_knowledge_fingerprint
from .operation_journal import OperationJournal
from .tag_database import TagDatabase


ProgressCallback=Callable[[int,int,str],None]
CancelCallback=Callable[[],bool]


def _write_verified_bytes(path: Path,value: bytes) -> None:
    atomic_verified_bytes(path,value,label=f"Training TXT {path.name}")


def _write_verified_text(path: Path,value: str) -> None:
    _write_verified_bytes(path,value.encode("utf-8"))


def _restore_verified_backup(
    backup: Path,target: Path,fingerprint: dict[str,object],
) -> None:
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name(f".{target.name}.{uuid4().hex}.restore.tmp")
    try:
        shutil.copy2(backup,temporary)
        if _file_fingerprint(temporary)!=fingerprint:
            raise RuntimeError(
                f"Training TXT restore backup failed verification: {target.name}"
            )
        temporary.replace(target)
        if _file_fingerprint(target)!=fingerprint:
            raise RuntimeError(
                f"Training TXT restore failed read-back verification: {target.name}"
            )
    finally:
        if temporary.exists():
            temporary.unlink()


def _file_fingerprint(path: Path) -> dict[str,object]:
    try:
        if not path.exists():
            return {"exists":False}
        if not path.is_file():
            return {"exists":True,"kind":"not_file"}
        value=path.read_bytes()
        return {
            "exists":True,
            "kind":"file",
            "size":len(value),
            "sha256":hashlib.sha256(value).hexdigest(),
        }
    except OSError as exc:
        return {"exists":True,"kind":"unavailable","error":str(exc)}


def _content_fingerprint(value: bytes) -> dict[str,object]:
    return {
        "exists":True,"kind":"file","size":len(value),
        "sha256":hashlib.sha256(value).hexdigest(),
    }


def _safe_relative(value: str|Path) -> Path:
    if not str(value).strip():
        raise ValueError("A relative export path is required.")
    relative=Path(value)
    if relative==Path(".") or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Image identity contains an unsafe relative path.")
    return relative


@dataclass
class DatasetCaptionExportPlanItem:
    image: Path
    target: Path|None
    disposition: str
    caption: str=""
    error: str=""
    authority_fingerprint: dict[str,object]=field(default_factory=dict)
    knowledge_fingerprint: str=""
    target_fingerprint: dict[str,object]=field(default_factory=dict)
    manifest_entry: dict[str,object]=field(default_factory=dict)

    @property
    def will_write(self) -> bool:
        return self.disposition in {"create","replace"}


@dataclass
class DatasetCaptionExportPlan:
    output_directory: Path
    profile: ExportProfile
    items: list[DatasetCaptionExportPlanItem]=field(default_factory=list)
    manifest_path: Path|None=None
    manifest_fingerprint: dict[str,object]=field(default_factory=dict)
    manifest_disposition: str="create"
    required_free_bytes: int=0
    available_free_bytes: int|None=None
    cancelled: bool=False
    not_processed: list[Path]=field(default_factory=list)
    destination_mode: str="separate"
    backup_enabled: bool=True
    backup_directory: Path|None=None
    external_policy_fingerprint: str=""
    privacy_confirmation_required: bool=False
    privacy_message: str=""
    scope_fingerprint: str=""

    def count(self,disposition: str) -> int:
        return sum(
            1 for item in self.items if item.disposition==disposition
        )

    @property
    def write_count(self) -> int:
        return sum(1 for item in self.items if item.will_write)

    @property
    def available_count(self) -> int:
        return sum(
            1 for item in self.items
            if item.disposition in {"create","replace","current"}
        )

    @property
    def estimated_write_bytes(self) -> int:
        return sum(
            len(item.caption.encode("utf-8"))
            for item in self.items if item.will_write
        )

    @property
    def estimated_backup_bytes(self) -> int:
        total=sum(
            int(item.target_fingerprint.get("size") or 0)
            for item in self.items if item.disposition=="replace"
        )
        if self.manifest_disposition=="replace":
            total+=int(self.manifest_fingerprint.get("size") or 0)
        return total

    @property
    def space_sufficient(self) -> bool:
        if self.required_free_bytes<=0:
            return True
        return (
            self.available_free_bytes is not None
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
class DatasetCaptionExportResult:
    output_directory: Path
    exported: list[Path]=field(default_factory=list)
    created: list[Path]=field(default_factory=list)
    replaced: list[Path]=field(default_factory=list)
    current: list[Path]=field(default_factory=list)
    excluded_by_rating: list[Path]=field(default_factory=list)
    empty: list[Path]=field(default_factory=list)
    stale: list[Path]=field(default_factory=list)
    collisions: list[Path]=field(default_factory=list)
    not_processed: list[Path]=field(default_factory=list)
    failed: list[tuple[Path,str]]=field(default_factory=list)
    profile: ExportProfile|None=None
    manifest_path: Path|None=None
    manifest_updated: bool=False
    recovery_manifest: Path|None=None
    operation_id: str=""
    cancelled: bool=False


@dataclass
class DatasetCaptionExportRestoreResult:
    recovery_manifest: Path|None=None
    restored: list[Path]=field(default_factory=list)
    already_restored: list[Path]=field(default_factory=list)
    held: list[Path]=field(default_factory=list)


class DatasetCaptionExportService:
    """Preview and recoverably export captions to one separate tree."""

    MANIFEST_NAME="collectigen-training-export.json"
    RECOVERY_KIND="collectigen_training_txt_export_recovery"

    def __init__(
        self,knowledge: TagDatabase,
        characters: CharacterDatabase|None=None,categories=None,
    ) -> None:
        self.knowledge=knowledge
        self.characters=characters
        self.categories=categories

    @staticmethod
    def backup_root(output_directory: str|Path) -> Path:
        output=Path(output_directory).resolve()
        label=output.name or "training-export"
        return output.parent/f".{label}.collectigen-backups"

    @classmethod
    def _validated_backup_root(cls,output_directory: str|Path) -> Path:
        output=Path(output_directory).resolve()
        root=cls.backup_root(output)
        resolved=root.resolve()
        if root.is_symlink() or resolved.parent!=output.parent.resolve():
            raise RuntimeError(
                "The Training TXT backup folder redirects outside the chosen "
                "export storage zone. Nothing was written."
            )
        return resolved

    @classmethod
    def _journal(cls,output_directory: str|Path) -> OperationJournal:
        return OperationJournal(
            cls._validated_backup_root(
                output_directory
            )/"operation_journal.jsonl"
        )

    @staticmethod
    def _scope_fingerprint(
        mode: str,items: Iterable[DatasetCaptionExportPlanItem],
    ) -> str:
        payload={
            "destination_mode":str(mode),
            "targets":sorted(
                str(item.target.resolve())
                for item in items if item.target is not None
            ),
        }
        return hashlib.sha256(json.dumps(
            payload,sort_keys=True,separators=(",",":"),ensure_ascii=False,
        ).encode("utf-8")).hexdigest()

    @classmethod
    def _plan_backup_root(cls,plan: DatasetCaptionExportPlan) -> Path:
        if plan.backup_directory is None:
            if plan.destination_mode=="adjacent":
                raise RuntimeError(
                    "Adjacent Training TXT protection needs the shared backup "
                    "folder configured in Rules & Automation."
                )
            return cls._validated_backup_root(plan.output_directory)
        base=plan.backup_directory.expanduser()
        resolved_base=base.resolve()
        if base.is_symlink():
            raise RuntimeError(
                "The shared external-write backup folder redirects to another "
                "storage location. Nothing was written."
            )
        root=resolved_base/"training-txt-export"
        resolved=root.resolve()
        if root.is_symlink() or resolved.parent!=resolved_base:
            raise RuntimeError(
                "The Training TXT recovery folder redirects outside the "
                "configured backup destination. Nothing was written."
            )
        return resolved

    @classmethod
    def _journal_for_plan(
        cls,plan: DatasetCaptionExportPlan,
    ) -> OperationJournal:
        return OperationJournal(
            cls._plan_backup_root(plan)/"operation_journal.jsonl"
        )

    @staticmethod
    def _entry_target(
        plan: DatasetCaptionExportPlan,entry: dict[str,object],
        *,allowed_targets: set[Path]|None=None,
    ) -> Path:
        if plan.destination_mode=="adjacent":
            target=Path(str(entry.get("target") or "")).resolve()
            allowed=allowed_targets or {
                item.target.resolve() for item in plan.items
                if item.target is not None
            }
            if target not in allowed:
                raise RuntimeError(
                    "Training TXT recovery contains a target outside the "
                    "frozen adjacent scope."
                )
            return target
        relative=_safe_relative(str(entry.get("target") or ""))
        target=plan.output_directory.joinpath(
            *PurePath(relative.as_posix()).parts
        ).resolve()
        try:
            target.relative_to(plan.output_directory)
        except ValueError as exc:
            raise RuntimeError(
                "Training TXT recovery contains an unsafe target."
            ) from exc
        return target

    @staticmethod
    def _available_space(path: Path) -> int|None:
        existing=path
        while not existing.exists() and existing!=existing.parent:
            existing=existing.parent
        try:
            return shutil.disk_usage(existing).free
        except OSError:
            return None

    @staticmethod
    def _identity_lookup(image_identities: dict|None) -> dict[str,object]:
        return {
            str(Path(key).resolve()):value
            for key,value in (image_identities or {}).items()
        }

    @staticmethod
    def _target_for(
        image: Path,output: Path,root: Path|None,identity,
        *,mixed_libraries: bool,destination_mode: str="separate",
    ) -> tuple[Path,dict[str,str]]:
        library_id=str(getattr(identity,"library_id","") or "")
        identity_relative=str(getattr(identity,"relative_path","") or "")
        source={}
        if destination_mode=="adjacent":
            target=image.resolve().with_suffix(".txt")
            if identity is not None and library_id and identity_relative:
                source={
                    "library_id":library_id,
                    "relative_image_path":Path(identity_relative).as_posix(),
                }
            return target,source
        if identity is not None and library_id and identity_relative:
            relative=_safe_relative(identity_relative)
            if mixed_libraries:
                safe_library="library-"+hashlib.sha256(
                    library_id.encode("utf-8")
                ).hexdigest()[:16]
                relative=Path(safe_library)/relative
            source={
                "library_id":library_id,
                "relative_image_path":Path(identity_relative).as_posix(),
            }
        else:
            try:
                relative=(
                    image.resolve().relative_to(root)
                    if root is not None else Path(image.name)
                )
            except ValueError:
                relative=Path(image.name)
        target=(output/relative).with_suffix(".txt").resolve()
        try:
            target.relative_to(output)
        except ValueError as exc:
            raise ValueError(
                "Training TXT target escapes the chosen export folder."
            ) from exc
        return target,source

    def _project_item(
        self,image: Path,target: Path,profile: ExportProfile,
        source: dict[str,str],
    ) -> DatasetCaptionExportPlanItem:
        record=ImageRecord(image)
        authority_fingerprint=_file_fingerprint(record.sidecar_path)
        knowledge_fingerprint=annotation_knowledge_fingerprint(
            record.annotation
        )
        if not profile.includes(
            record.annotation,self.knowledge,self.categories,
        ):
            return DatasetCaptionExportPlanItem(
                image,target,"excluded",
                authority_fingerprint=authority_fingerprint,
                knowledge_fingerprint=knowledge_fingerprint,
                target_fingerprint=_file_fingerprint(target),
            )
        projection=profile.project(
            record.annotation,self.characters,self.knowledge
        )
        caption=projection.render(profile.separator)
        if not caption.strip():
            return DatasetCaptionExportPlanItem(
                image,target,"empty",caption=caption,
                authority_fingerprint=authority_fingerprint,
                knowledge_fingerprint=knowledge_fingerprint,
                target_fingerprint=_file_fingerprint(target),
            )
        target_fingerprint=_file_fingerprint(target)
        if target_fingerprint.get("kind") in {"not_file","unavailable"}:
            raise RuntimeError(
                "The planned Training TXT target is not a readable file."
            )
        current=False
        if target_fingerprint.get("exists"):
            try:
                current=target.read_text(encoding="utf-8")==caption
            except (OSError,UnicodeError) as exc:
                raise RuntimeError(
                    f"Existing Training TXT could not be inspected: {exc}"
                ) from exc
        disposition=(
            "current" if current else
            "replace" if target_fingerprint.get("exists") else "create"
        )
        entry={
            "caption":target.name,
            "content_sha256":hashlib.sha256(
                caption.encode("utf-8")
            ).hexdigest(),
            "decision_counts":projection.decision_counts(),
            "projection_decisions":[
                decision.to_dict() for decision in projection.decisions
            ],
            "relationship_fingerprint":projection.relationship_fingerprint,
        }
        if source:
            entry["source"]=source
        return DatasetCaptionExportPlanItem(
            image,target,disposition,caption=caption,
            authority_fingerprint=authority_fingerprint,
            knowledge_fingerprint=knowledge_fingerprint,
            target_fingerprint=target_fingerprint,
            manifest_entry=entry,
        )

    def preview(
        self,images: Iterable[str|Path],output_directory: str|Path|None,*,
        dataset_root: str|Path|None=None,
        profile: ExportProfile|None=None,
        image_identities: dict|None=None,
        destination_mode: str="separate",
        backup_enabled: bool=True,
        backup_directory: str|Path|None=None,
        external_policy_fingerprint: str="",
        privacy_confirmation_required: bool=False,
        privacy_message: str="",
        progress: ProgressCallback|None=None,
        should_cancel: CancelCallback|None=None,
    ) -> DatasetCaptionExportPlan:
        paths=tuple(dict.fromkeys(
            Path(value) for value in images if value
        ))
        mode=str(destination_mode or "separate")
        if mode not in {"separate","adjacent"}:
            raise ValueError(
                "Choose separate-folder or adjacent Training TXT output."
            )
        if mode=="separate" and output_directory is None:
            raise ValueError("Choose a separate Training TXT export folder.")
        output=(
            Path(output_directory).resolve()
            if output_directory is not None else
            paths[0].resolve().parent if paths else Path.cwd().resolve()
        )
        root=Path(dataset_root).resolve() if dataset_root else None
        selected=profile or ExportProfile()
        identity_lookup=self._identity_lookup(image_identities)
        selected_identity_keys={str(path.resolve()) for path in paths}
        library_ids={
            str(getattr(value,"library_id","") or "")
            for key,value in identity_lookup.items()
            if key in selected_identity_keys
            if str(getattr(value,"library_id","") or "")
        }
        mixed_libraries=len(library_ids)>1
        plan=DatasetCaptionExportPlan(
            output,selected,destination_mode=mode,
            backup_enabled=bool(backup_enabled),
            backup_directory=(
                Path(backup_directory).expanduser().absolute()
                if backup_directory is not None else None
            ),
            external_policy_fingerprint=str(external_policy_fingerprint or ""),
            privacy_confirmation_required=bool(privacy_confirmation_required),
            privacy_message=str(privacy_message or ""),
            manifest_path=(
                output/self.MANIFEST_NAME if mode=="separate" else None
            ),
        )
        cancel=should_cancel or (lambda:False)
        notify=progress or (lambda *_:None)
        for index,image in enumerate(paths,1):
            if cancel():
                plan.cancelled=True
                plan.not_processed.extend(paths[index-1:])
                break
            target=None
            try:
                identity=identity_lookup.get(str(image.resolve()))
                target,source=self._target_for(
                    image,output,root,identity,
                    mixed_libraries=mixed_libraries,destination_mode=mode,
                )
                item=self._project_item(image,target,selected,source)
                if item.manifest_entry:
                    item.manifest_entry["caption"]=(
                        target.relative_to(output).as_posix()
                        if mode=="separate" else str(target)
                    )
                plan.items.append(item)
            except Exception as exc:
                plan.items.append(DatasetCaptionExportPlanItem(
                    image,target,"failed",error=str(exc),
                    target_fingerprint=(
                        _file_fingerprint(target) if target is not None else {}
                    ),
                ))
            notify(index,len(paths),image.name)
        by_target: dict[str,list[DatasetCaptionExportPlanItem]]={}
        for item in plan.items:
            if item.target is None or item.disposition=="failed":
                continue
            by_target.setdefault(str(item.target).casefold(),[]).append(item)
        for matches in by_target.values():
            if len(matches)<2:
                continue
            names=", ".join(item.image.name for item in matches)
            for item in matches:
                item.disposition="collision"
                item.error=(
                    "More than one selected image resolves to this Training "
                    f"TXT target: {names}"
                )
        plan.scope_fingerprint=self._scope_fingerprint(mode,plan.items)
        stable=self._manifest_payload(plan,plan.items)
        if plan.manifest_path is not None:
            plan.manifest_fingerprint=_file_fingerprint(plan.manifest_path)
            plan.manifest_disposition=self._manifest_disposition(
                plan.manifest_path,stable,
            )
            manifest_bytes=len(json.dumps({
                **stable,"generated_at":"0000-00-00T00:00:00+00:00",
            },indent=2,ensure_ascii=False).encode("utf-8"))+1
        else:
            plan.manifest_fingerprint={"exists":False}
            plan.manifest_disposition="none"
            manifest_bytes=0
        operation_needed=(
            bool(plan.write_count)
            or plan.manifest_disposition not in {"current","none"}
        )
        recovery_reserve=(
            1_048_576+4_096*(plan.write_count+1)
            if operation_needed and plan.backup_enabled else 0
        )
        plan.required_free_bytes=(
            plan.estimated_write_bytes
            +(plan.estimated_backup_bytes if plan.backup_enabled else 0)
            +(
                manifest_bytes
                if plan.manifest_disposition not in {"current","none"} else 0
            )
            +recovery_reserve
        )
        space_paths={
            item.target.parent for item in plan.items if item.target is not None
        }
        if plan.backup_enabled and plan.backup_directory is not None:
            space_paths.add(plan.backup_directory)
        available=[
            self._available_space(path) for path in space_paths or {output}
        ]
        plan.available_free_bytes=(
            min(value for value in available if value is not None)
            if available and all(value is not None for value in available)
            else None
        )
        return plan

    @staticmethod
    def _available_items(items):
        return [
            item for item in items
            if item.disposition in {"create","replace","current"}
        ]

    def _manifest_payload(
        self,plan: DatasetCaptionExportPlan,items,*,failed: int|None=None,
    ) -> dict[str,object]:
        available=self._available_items(items)
        failed_count=(
            failed if failed is not None else
            sum(item.disposition in {"failed","collision"} for item in items)
        )
        return {
            "schema_version":1,
            "kind":"collectigen_training_txt_export",
            "profile":plan.profile.to_dict(),
            **plan.profile.provenance(),
            "relationship_policy":plan.profile.hierarchy_policy,
            "counts":{
                "exported":len(available),
                "excluded_by_rating":sum(
                    item.disposition=="excluded" for item in items
                ),
                "empty":sum(item.disposition=="empty" for item in items),
                "failed":int(failed_count),
            },
            "captions":sorted(
                (dict(item.manifest_entry) for item in available),
                key=lambda item:str(item["caption"]),
            ),
        }

    @staticmethod
    def _manifest_disposition(path: Path,payload: dict[str,object]) -> str:
        if not path.exists():
            return "create"
        try:
            current=json.loads(path.read_text(encoding="utf-8"))
        except (OSError,UnicodeError,json.JSONDecodeError):
            return "replace"
        current.pop("generated_at",None)
        return "current" if current==payload else "replace"

    @staticmethod
    def _recovery_text(payload: dict[str,object]) -> str:
        return json.dumps(payload,indent=2,ensure_ascii=False)+"\n"

    @staticmethod
    def _backup_name(relative: Path) -> str:
        digest=hashlib.sha256(relative.as_posix().encode("utf-8")).hexdigest()
        return f"{digest[:20]}_{relative.name}"

    def _create_recovery(
        self,plan: DatasetCaptionExportPlan,entries: list[dict[str,object]],
    ) -> tuple[str,Path,Path,dict[str,object],OperationJournal]:
        root=self._plan_backup_root(plan)
        operation_id=f"export_{uuid4().hex}"
        directory=root/operation_id
        directory.mkdir(parents=True,exist_ok=False)
        try:
            for entry in entries:
                target=self._entry_target(plan,entry)
                before=dict(entry["before"])
                if before.get("exists"):
                    backup=directory/self._backup_name(
                        Path(str(entry["target"]))
                    )
                    shutil.copy2(target,backup)
                    if _file_fingerprint(backup)!=before:
                        raise RuntimeError(
                            "Training TXT backup failed verification: "
                            f"{target.name}"
                        )
                    entry["backup"]=backup.name
        except Exception:
            # No output write is allowed until every replacement backup has
            # been copied and verified. This UUID directory is owned solely by
            # this failed preflight, so remove it instead of advertising a
            # recovery operation that never reached its durable checkpoint.
            shutil.rmtree(directory,ignore_errors=True)
            raise
        recovery={
            "schema_version":1,
            "kind":self.RECOVERY_KIND,
            "status":"applying",
            "operation_id":operation_id,
            "destination_mode":plan.destination_mode,
            "scope_fingerprint":plan.scope_fingerprint,
            "output_directory":str(plan.output_directory),
            "backup_directory":str(plan.backup_directory or ""),
            "privacy_confirmation_required":plan.privacy_confirmation_required,
            "profile_id":plan.profile.profile_id,
            "profile_version":plan.profile.version,
            "profile_fingerprint":plan.profile.fingerprint(),
            "entries":entries,
        }
        recovery_path=directory/"recovery.json"
        _write_verified_text(recovery_path,self._recovery_text(recovery))
        journal=self._journal_for_plan(plan)
        journal_id=journal.begin(
            "training_txt_export",expected=len(entries),
            reason=(
                "Export profile-projected Training TXT beside source images"
                if plan.destination_mode=="adjacent" else
                "Export profile-projected Training TXT to a separate folder"
            ),
            metadata={
                "recovery":str(recovery_path),
                "destination_mode":plan.destination_mode,
                "scope_fingerprint":plan.scope_fingerprint,
                "profile_id":plan.profile.profile_id,
                "profile_version":plan.profile.version,
            },
        )
        recovery["journal_operation_id"]=journal_id
        _write_verified_text(recovery_path,self._recovery_text(recovery))
        return operation_id,directory,recovery_path,recovery,journal

    def _validate_item(
        self,item: DatasetCaptionExportPlanItem,plan: DatasetCaptionExportPlan,
    ) -> bool:
        try:
            if item.target is None:
                return False
            record=ImageRecord(item.image)
            if _file_fingerprint(record.sidecar_path)!=item.authority_fingerprint:
                return False
            if annotation_knowledge_fingerprint(
                record.annotation
            )!=item.knowledge_fingerprint:
                return False
            if _file_fingerprint(item.target)!=item.target_fingerprint:
                return False
            source=dict(item.manifest_entry.get("source") or {})
            current=self._project_item(
                item.image,item.target,plan.profile,source,
            )
            if current.disposition!=item.disposition:
                return False
            if current.caption!=item.caption:
                return False
            if current.manifest_entry:
                current.manifest_entry["caption"]=item.manifest_entry.get(
                    "caption",item.target.name
                )
            return current.manifest_entry==item.manifest_entry
        except Exception:
            return False

    def apply(
        self,plan: DatasetCaptionExportPlan,*,
        progress: ProgressCallback|None=None,
        should_cancel: CancelCallback|None=None,
        privacy_confirmed: bool=False,
        external_policy_fingerprint: str="",
    ) -> DatasetCaptionExportResult:
        if plan.cancelled:
            raise RuntimeError(
                "A cancelled Training TXT preview cannot be applied. Preview again."
            )
        result=DatasetCaptionExportResult(
            plan.output_directory,profile=plan.profile,
            manifest_path=plan.manifest_path,
        )
        result.excluded_by_rating=[
            item.image for item in plan.items if item.disposition=="excluded"
        ]
        result.empty=[
            item.image for item in plan.items if item.disposition=="empty"
        ]
        result.collisions=[
            item.image for item in plan.items if item.disposition=="collision"
        ]
        result.failed=[
            (item.image,item.error) for item in plan.items
            if item.disposition=="failed"
        ]
        writes_planned=[
            item for item in plan.items if item.will_write
        ]
        decision=plan.write_decision
        validation_issue=external_write_validation_issue(
            decision,decision.execution(
                current_policy_fingerprint=external_policy_fingerprint,
                privacy_confirmed=privacy_confirmed,
            ),
        )
        if validation_issue in {"stale_decision","stale_policy"}:
            result.failed.append((
                plan.output_directory,
                "The shared external-write backup policy changed after preview.",
            ))
            result.not_processed=[item.image for item in writes_planned]
            return result
        if validation_issue=="privacy_confirmation_required":
            result.failed.append((
                plan.output_directory,
                "This exact cross-zone export was not confirmed.",
            ))
            result.not_processed=[item.image for item in writes_planned]
            return result
        space_paths={
            item.target.parent for item in plan.items if item.target is not None
        }
        if plan.backup_enabled and plan.backup_directory is not None:
            space_paths.add(plan.backup_directory)
        current_values=[
            self._available_space(path)
            for path in space_paths or {plan.output_directory}
        ]
        current_free=(
            min(value for value in current_values if value is not None)
            if current_values
            and all(value is not None for value in current_values) else None
        )
        if (
            not plan.space_sufficient
            or plan.required_free_bytes>0 and current_free is None
            or current_free is not None
            and current_free<plan.required_free_bytes
        ):
            result.failed.append((
                plan.output_directory,
                "The export destination no longer has the required free space.",
            ))
            result.not_processed=[
                item.image for item in plan.items if item.will_write
            ]
            return result
        available=self._available_items(plan.items)
        validated=[
            item for item in plan.items
            if item.disposition in {
                "create","replace","current","excluded","empty",
            }
        ]
        manifest_stale=(
            plan.manifest_path is not None
            and _file_fingerprint(plan.manifest_path)!=plan.manifest_fingerprint
        )
        stale_items=[
            item for item in validated if not self._validate_item(item,plan)
        ]
        if manifest_stale or stale_items:
            result.stale=[item.image for item in stale_items]
            if manifest_stale:
                result.stale.append(plan.manifest_path)
            result.not_processed=[item.image for item in available]
            return result
        writes=[item for item in available if item.will_write]
        result.current=[
            item.target for item in available
            if item.disposition=="current" and item.target is not None
        ]
        stable_manifest=self._manifest_payload(plan,plan.items)
        manifest_disposition=(
            self._manifest_disposition(plan.manifest_path,stable_manifest)
            if plan.manifest_path is not None else "none"
        )
        if not writes and manifest_disposition in {"current","none"}:
            return result
        entries=[]
        for item in writes:
            target_key=(
                str(item.target.resolve())
                if plan.destination_mode=="adjacent" else
                item.target.relative_to(plan.output_directory).as_posix()
            )
            entries.append({
                "target":target_key,
                "before":item.target_fingerprint,
                "after":_content_fingerprint(item.caption.encode("utf-8")),
                "state":"planned",
            })
        manifest_entry=None
        if plan.manifest_path is not None:
            manifest_relative=plan.manifest_path.relative_to(
                plan.output_directory
            )
            manifest_entry={
                "target":manifest_relative.as_posix(),
                "before":plan.manifest_fingerprint,
                "after":plan.manifest_fingerprint,
                "state":"planned",
                "manifest":True,
            }
            if writes or manifest_disposition!="current":
                entries.append(manifest_entry)
        recovery=None
        journal=None
        journal_id=""
        if plan.backup_enabled:
            try:
                (
                    result.operation_id,_directory,result.recovery_manifest,
                    recovery,journal,
                )=self._create_recovery(plan,entries)
            except Exception as exc:
                result.failed.append((
                    plan.manifest_path or plan.output_directory,str(exc),
                ))
                result.not_processed=[item.image for item in writes]
                return result
            journal_id=str(recovery["journal_operation_id"])
        notify=progress or (lambda *_:None)
        cancel=should_cancel or (lambda:False)
        written_items=[]
        total=len(writes)
        for index,item in enumerate(writes,1):
            if cancel():
                result.cancelled=True
                result.not_processed.extend(
                    candidate.image for candidate in writes[index-1:]
                )
                break
            if (
                not self._validate_item(item,plan)
                or item.target is None
            ):
                result.stale.append(item.image)
                result.not_processed.extend(
                    candidate.image for candidate in writes[index-1:]
                )
                break
            entry=next(
                value for value in entries
                if value["target"]==(
                    str(item.target.resolve())
                    if plan.destination_mode=="adjacent" else
                    item.target.relative_to(plan.output_directory).as_posix()
                )
            )
            try:
                if journal is not None:
                    journal.append_change(
                        journal_id,item=str(entry["target"]),
                        before=entry["before"],after=entry["after"],
                        reason=(
                            "Verified adjacent Training TXT export"
                            if plan.destination_mode=="adjacent" else
                            "Verified separate-folder Training TXT export"
                        ),
                    )
                    entry["journaled"]=True
                _write_verified_text(item.target,item.caption)
                result.exported.append(item.target)
                (result.created if item.disposition=="create" else result.replaced).append(item.target)
                written_items.append(item)
                entry["state"]="written"
            except Exception as exc:
                recovery_error=""
                if result.recovery_manifest is not None:
                    try:
                        self._restore_recovery_entry(
                            plan,result.recovery_manifest,entry,
                        )
                        entry["state"]="restored_after_failure"
                    except Exception as restore_exc:
                        recovery_error=(
                            f"; automatic restore failed: {restore_exc}"
                        )
                result.failed.append((item.image,str(exc)+recovery_error))
                result.not_processed.extend(
                    candidate.image for candidate in writes[index:]
                )
                break
            notify(index,total,item.image.name)
        manifest_items=[
            item for item in plan.items
            if item.disposition=="current" or item in written_items
        ]
        if (
            not result.cancelled and not result.stale and cancel()
        ):
            result.cancelled=True
        if (
            plan.manifest_path is not None
            and not result.cancelled and not result.stale
        ):
            final_payload=self._manifest_payload(
                plan,
                [
                    *manifest_items,
                    *[item for item in plan.items if item.disposition in {
                        "excluded","empty","failed","collision",
                    }],
                ],
                failed=len(result.failed)+len(result.collisions),
            )
            final_disposition=self._manifest_disposition(
                plan.manifest_path,final_payload,
            )
            if final_disposition!="current" and manifest_entry is not None:
                final_payload={
                    **final_payload,
                    "generated_at":datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                }
                value=json.dumps(
                    final_payload,indent=2,ensure_ascii=False
                )+"\n"
                if (
                    _file_fingerprint(plan.manifest_path)
                    !=manifest_entry["before"]
                ):
                    result.stale.append(plan.manifest_path)
                else:
                    try:
                        manifest_entry["after"]=_content_fingerprint(
                            value.encode("utf-8")
                        )
                        if journal is not None:
                            journal.append_change(
                                journal_id,item=str(manifest_entry["target"]),
                                before=manifest_entry["before"],
                                after=manifest_entry["after"],
                                reason="Verified Training TXT export manifest",
                            )
                            manifest_entry["journaled"]=True
                        if recovery is not None:
                            recovery["entries"]=entries
                            _write_verified_text(
                                result.recovery_manifest,
                                self._recovery_text(recovery),
                            )
                        _write_verified_text(plan.manifest_path,value)
                        result.manifest_updated=True
                        manifest_entry["state"]="written"
                    except Exception as exc:
                        recovery_error=""
                        if result.recovery_manifest is not None:
                            try:
                                self._restore_recovery_entry(
                                    plan,result.recovery_manifest,
                                    manifest_entry,
                                )
                                manifest_entry["state"]="restored_after_failure"
                            except Exception as restore_exc:
                                recovery_error=(
                                    f"; automatic restore failed: {restore_exc}"
                                )
                        result.failed.append((
                            plan.manifest_path,str(exc)+recovery_error,
                        ))
        if recovery is not None and journal is not None:
            recovery["status"]=(
                "cancelled" if result.cancelled else
                "failed" if result.failed or result.stale else "complete"
            )
            recovery["entries"]=entries
            recovery["result"]={
                "created":len(result.created),"replaced":len(result.replaced),
                "current":len(result.current),"stale":len(result.stale),
                "failed":len(result.failed),
                "not_processed":len(result.not_processed),
                "manifest_updated":result.manifest_updated,
            }
            try:
                _write_verified_text(
                    result.recovery_manifest,self._recovery_text(recovery)
                )
                journal.finish(
                    journal_id,recovery["status"],result=recovery["result"],
                )
            except Exception as exc:
                result.failed.append((result.recovery_manifest,str(exc)))
        return result

    def export(
        self,images: Iterable[str|Path],output_directory: str|Path,*,
        dataset_root: str|Path|None=None,
        profile: ExportProfile|None=None,
        image_identities: dict|None=None,
    ) -> DatasetCaptionExportResult:
        plan=self.preview(
            images,output_directory,dataset_root=dataset_root,
            profile=profile,image_identities=image_identities,
        )
        return self.apply(plan)

    def recovery_manifests(
        self,output_directory: str|Path,*,statuses=(),
    ) -> list[Path]:
        try:
            root=self._validated_backup_root(output_directory)
        except RuntimeError:
            return []
        if not root.is_dir():
            return []
        wanted={str(value) for value in statuses}
        result=[]
        resolved_root=root.resolve()
        try:
            paths=sorted(
                root.glob("export_*/recovery.json"),
                key=lambda path:path.stat().st_mtime_ns,
            )
        except OSError:
            return []
        for path in paths:
            try:
                path.resolve().relative_to(resolved_root)
            except ValueError:
                continue
            try:
                payload=json.loads(path.read_text(encoding="utf-8"))
            except (OSError,UnicodeError,json.JSONDecodeError):
                continue
            if payload.get("kind")!=self.RECOVERY_KIND:
                continue
            if wanted and str(payload.get("status")) not in wanted:
                continue
            result.append(path)
        return result

    def recovery_manifests_for_plan(
        self,plan: DatasetCaptionExportPlan,*,statuses=(),
    ) -> list[Path]:
        roots=[]
        try:
            roots.append(self._plan_backup_root(plan))
        except RuntimeError:
            pass
        if plan.destination_mode=="separate":
            try:
                legacy=self._validated_backup_root(plan.output_directory)
                if legacy not in roots:
                    roots.append(legacy)
            except RuntimeError:
                pass
        wanted={str(value) for value in statuses}
        result=[]
        for root in roots:
            if not root.is_dir():
                continue
            try:
                paths=tuple(root.glob("export_*/recovery.json"))
            except OSError as exc:
                raise RuntimeError(
                    "Training TXT recovery records could not be inspected."
                ) from exc
            resolved_root=root.resolve()
            for path in paths:
                try:
                    path.resolve().relative_to(resolved_root)
                except (OSError,ValueError) as exc:
                    raise RuntimeError(
                        "A Training TXT recovery record redirects outside its "
                        "protected backup folder."
                    ) from exc
                try:
                    payload=json.loads(path.read_text(encoding="utf-8"))
                except (OSError,UnicodeError,json.JSONDecodeError) as exc:
                    raise RuntimeError(
                        "A Training TXT recovery record could not be verified."
                    ) from exc
                if payload.get("kind")!=self.RECOVERY_KIND:
                    continue
                if str(payload.get("destination_mode") or "separate")!=(
                    plan.destination_mode
                ):
                    continue
                payload_scope=str(payload.get("scope_fingerprint") or "")
                if payload_scope and payload_scope!=plan.scope_fingerprint:
                    continue
                if not payload_scope and (
                    plan.destination_mode!="separate"
                    or Path(str(
                        payload.get("output_directory") or ""
                    )).resolve()!=plan.output_directory
                ):
                    continue
                if wanted and str(payload.get("status")) not in wanted:
                    continue
                result.append(path)
        try:
            return sorted(
                dict.fromkeys(result),key=lambda path:path.stat().st_mtime_ns,
            )
        except OSError as exc:
            raise RuntimeError(
                "Training TXT recovery timestamps could not be verified."
            ) from exc

    def latest_recovery_for_plan(
        self,plan: DatasetCaptionExportPlan,*,statuses=(),
    ) -> Path|None:
        values=self.recovery_manifests_for_plan(plan,statuses=statuses)
        return values[-1] if values else None

    def latest_recovery(
        self,output_directory: str|Path,*,statuses=(),
    ) -> Path|None:
        values=self.recovery_manifests(
            output_directory,statuses=statuses,
        )
        return values[-1] if values else None

    def _restore_recovery_entry(
        self,plan: DatasetCaptionExportPlan,
        recovery_manifest: Path,entry: dict[str,object],
    ) -> None:
        target=self._entry_target(plan,entry)
        before=dict(entry.get("before") or {})
        backup_name=str(entry.get("backup") or "")
        if before.get("exists"):
            recovery_directory=recovery_manifest.parent.resolve()
            backup=(recovery_directory/backup_name).resolve()
            if (
                not backup_name or backup.parent!=recovery_directory
                or _file_fingerprint(backup)!=before
            ):
                raise RuntimeError("Verified pre-export backup is unavailable.")
            _restore_verified_backup(backup,target,before)
        elif target.exists():
            target.unlink()
            if target.exists():
                raise RuntimeError("Created export target could not be removed.")

    def restore_latest(
        self,output_directory: str|Path,*,
        progress: ProgressCallback|None=None,
    ) -> DatasetCaptionExportRestoreResult:
        output=Path(output_directory).resolve()
        manifest_path=self.latest_recovery(
            output,statuses=("applying","complete","cancelled","failed"),
        )
        plan=DatasetCaptionExportPlan(
            output,ExportProfile(),destination_mode="separate",
            manifest_path=output/self.MANIFEST_NAME,
        )
        return self._restore_manifest(plan,manifest_path,progress=progress)

    def restore_latest_for_plan(
        self,plan: DatasetCaptionExportPlan,*,
        progress: ProgressCallback|None=None,
    ) -> DatasetCaptionExportRestoreResult:
        manifest_path=self.latest_recovery_for_plan(
            plan,statuses=("applying","complete","cancelled","failed"),
        )
        return self._restore_manifest(plan,manifest_path,progress=progress)

    def _restore_manifest(
        self,plan: DatasetCaptionExportPlan,manifest_path: Path|None,*,
        progress: ProgressCallback|None=None,
    ) -> DatasetCaptionExportRestoreResult:
        result=DatasetCaptionExportRestoreResult(manifest_path)
        if manifest_path is None:
            return result
        payload=json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            str(payload.get("destination_mode") or "separate")
            !=plan.destination_mode
        ):
            raise RuntimeError(
                "Training TXT recovery belongs to another destination mode."
            )
        if (
            plan.destination_mode=="separate"
            and Path(str(payload.get("output_directory") or "")).resolve()
            !=plan.output_directory
        ):
            raise RuntimeError(
                "Training TXT recovery belongs to another export folder."
            )
        payload_scope=str(payload.get("scope_fingerprint") or "")
        if (
            plan.scope_fingerprint and (
                payload_scope and payload_scope!=plan.scope_fingerprint
                or not payload_scope and plan.destination_mode!="separate"
            )
        ):
            raise RuntimeError(
                "Training TXT recovery belongs to another frozen image scope."
            )
        resolved=[]
        seen=set()
        allowed_targets={
            item.target.resolve() for item in plan.items
            if item.target is not None
        }
        for entry in payload.get("entries",()):
            target=self._entry_target(
                plan,entry,allowed_targets=allowed_targets or None,
            )
            target_key=str(target).casefold()
            if target_key in seen:
                raise RuntimeError(
                    "Training TXT recovery repeats an export target."
                )
            seen.add(target_key)
            before=dict(entry.get("before") or {})
            after=dict(entry.get("after") or {})
            current=_file_fingerprint(target)
            if current==before:
                result.already_restored.append(target)
                continue
            if current!=after:
                result.held.append(target)
                continue
            backup_name=str(entry.get("backup") or "")
            recovery_directory=manifest_path.parent.resolve()
            backup=(
                (recovery_directory/backup_name).resolve()
                if backup_name else None
            )
            if before.get("exists"):
                if (
                    backup is None or backup.parent!=recovery_directory
                    or _file_fingerprint(backup)!=before
                ):
                    raise RuntimeError(
                        "Training TXT recovery backup is missing or invalid."
                    )
            resolved.append((target,before,backup))
        if result.held:
            return result
        notify=progress or (lambda *_:None)
        for index,(target,before,backup) in enumerate(resolved,1):
            if before.get("exists"):
                _restore_verified_backup(backup,target,before)
            elif target.exists():
                target.unlink()
                if target.exists():
                    raise RuntimeError(
                        f"Created Training TXT could not be removed: {target.name}"
                    )
            result.restored.append(target)
            notify(index,len(resolved),target.name)
        payload["status"]="restored"
        payload["restored_at"]=datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        _write_verified_text(manifest_path,self._recovery_text(payload))
        journal_id=str(payload.get("journal_operation_id") or "")
        if journal_id:
            OperationJournal(
                manifest_path.parent.parent/"operation_journal.jsonl"
            ).finish(
                journal_id,"undone",result={"restored":len(result.restored)},
            )
        return result
