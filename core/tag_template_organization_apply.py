from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime,timezone
from hashlib import sha256
from pathlib import Path
from typing import Any,Callable
from uuid import uuid4

from .category_registry import CategoryRegistry
from .knowledge_engine import FoundationKnowledge
from .operation_journal import JournalOperation,OperationJournal
from .relationship_store import RelationshipStore
from .tag_database import TagDatabase
from .automation_rule_conversion import RulePairAdmission
from .tag_order_workspace import CategoryWorkspaceSession
from .tag_template_organization import (
    TAG_TEMPLATE_ORGANIZE_CREATE,TAG_TEMPLATE_ORGANIZE_MOVE,
    TAG_TEMPLATE_ORGANIZE_REUSE,TagTemplateOrganizationPlanningService,
    TagTemplateOrganizationReport,TagTemplateOrganizationStaleError,
)
from .tag_template_store import ReviewedTagTemplate


TAG_TEMPLATE_ORGANIZATION_APPLY_KIND="tag_template_organization"
TAG_TEMPLATE_ORGANIZATION_UNDO_KIND="tag_template_organization_undo"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def derived_tag_template_tag_id(result_id: str) -> str:
    stable_id=str(result_id or "").strip()
    if not stable_id.startswith(("tag_template_expansion.","tag_template_result.")):
        raise ValueError("A generated Tag needs one stable template result identity.")
    digest=sha256(stable_id.encode("utf-8")).hexdigest()[:20]
    return f"tag_template_tag.{digest}"


@dataclass(frozen=True)
class TagTemplateOrganizationApplyResult:
    operation_id: str
    template_id: str
    template_version: int
    plan_fingerprint: str
    created_tag_ids: tuple[str,...]
    moved_tag_ids: tuple[str,...]
    reused_tag_ids: tuple[str,...]
    relationship_ids: tuple[str,...]
    category_id: str
    category_created: bool
    backup_directory: Path | None
    changed: bool


class TagTemplateOrganizationApplyService:
    """Apply and exactly restore one reviewed template organization operation."""

    def __init__(
        self, *, tags_path: str|Path,category_default_path: str|Path,
        category_user_path: str|Path,modifier_path: str|Path,
        concept_path: str|Path,default_relationship_path: str|Path,
        user_relationship_path: str|Path,journal_path: str|Path,
        rule_pair_admission: RulePairAdmission|None=None,
    ) -> None:
        self.tags_path=Path(tags_path)
        self.category_default_path=Path(category_default_path)
        self.category_user_path=Path(category_user_path)
        self.modifier_path=Path(modifier_path)
        self.concept_path=Path(concept_path)
        self.default_relationship_path=Path(default_relationship_path)
        self.user_relationship_path=Path(user_relationship_path)
        self.journal=OperationJournal(journal_path)
        if rule_pair_admission is not None and not rule_pair_admission.owns(self.tags_path,"knowledge"):
            raise ValueError("Template organization must bind its exact Knowledge participant.")
        self.rule_pair_admission=rule_pair_admission

    def _admission(self):
        return self.rule_pair_admission.admit() if self.rule_pair_admission else nullcontext()

    def _check_supplied_binding(self,tags):
        supplied_admission=getattr(tags,"rule_pair_admission",None)
        if supplied_admission is not None and (
            self.rule_pair_admission is None
            or supplied_admission.authority_key!=self.rule_pair_admission.authority_key
            or not supplied_admission.owns(self.tags_path,"knowledge")
        ):
            raise ValueError("Use template organization bound to this exact Knowledge pair.")

    @property
    def allowed_paths(self) -> set[Path]:
        return {
            self.tags_path.resolve(),self.category_user_path.resolve(),
            self.user_relationship_path.resolve(),
        }

    @staticmethod
    def _disk_payload(path: Path) -> tuple[dict[str,Any],bool]:
        if not path.exists():return {},False
        try:value=json.loads(path.read_text(encoding="utf-8"))
        except (OSError,json.JSONDecodeError) as exc:
            raise RuntimeError(f"System Knowledge storage is unavailable or invalid: {path}") from exc
        if not isinstance(value,dict):
            raise RuntimeError(f"System Knowledge storage must contain an object: {path}")
        return value,True

    @staticmethod
    def _write_payload(path: Path,payload: dict[str,Any]|None) -> None:
        if payload is None:
            path.unlink(missing_ok=True); return
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix(path.suffix+f".{uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(payload,indent=2,ensure_ascii=False)+"\n",encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _write_bytes(path: Path,payload: bytes|None) -> None:
        if payload is None:
            path.unlink(missing_ok=True); return
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix(path.suffix+f".{uuid4().hex}.restore.tmp")
        temporary.write_bytes(payload); temporary.replace(path)

    @staticmethod
    def _current_matches(
        current: dict[str,Any]|None,expected: dict[str,Any],exists: bool,
    ) -> bool:
        return current==expected if exists else current is None

    def _mutable_snapshot(self) -> dict[Path,bytes|None]:
        return {
            path:(path.read_bytes() if path.exists() else None)
            for path in self.allowed_paths
        }

    def apply(
        self, record: ReviewedTagTemplate, expansion: Any,
        expected_plan: TagTemplateOrganizationReport, *,
        tags: TagDatabase|None=None,categories: CategoryRegistry|None=None,
        foundation: FoundationKnowledge|None=None,
        progress: Callable[[int,int,str],None]|None=None,
        cancelled: Callable[[],bool]|None=None,
    ) -> TagTemplateOrganizationApplyResult:
        self._check_supplied_binding(tags)
        with self._admission():
            return self._apply_admitted(record,expansion,expected_plan,tags=tags,
                categories=categories,foundation=foundation,progress=progress,cancelled=cancelled)

    def _apply_admitted(
        self, record: ReviewedTagTemplate, expansion: Any,
        expected_plan: TagTemplateOrganizationReport, *,
        tags: TagDatabase|None=None,categories: CategoryRegistry|None=None,
        foundation: FoundationKnowledge|None=None,
        progress: Callable[[int,int,str],None]|None=None,
        cancelled: Callable[[],bool]|None=None,
    ) -> TagTemplateOrganizationApplyResult:
        cancelled=cancelled or (lambda:False)
        if (
            tags is not None and tags.path.resolve()!=self.tags_path.resolve()
            or categories is not None and (
                categories.user_path.resolve()!=self.category_user_path.resolve()
                or categories.default_path.resolve()!=self.category_default_path.resolve()
            )
        ):
            raise RuntimeError("The revalidated System Knowledge snapshot does not match this operation's stores.")
        before_load=self._mutable_snapshot()
        tags=TagDatabase(self.tags_path,rule_pair_admission=self.rule_pair_admission)
        categories=CategoryRegistry(
            self.category_default_path,self.category_user_path,
        )
        effective_relationships=dict(
            RelationshipStore(self.default_relationship_path).relationships,
        )
        effective_relationships.update(
            RelationshipStore(self.user_relationship_path).relationships,
        )
        categories.configure_category_base_authority(
            effective_relationships.values(),
        )
        foundation=foundation or FoundationKnowledge(
            self.modifier_path,self.concept_path,
        )
        loaded_snapshot=self._mutable_snapshot()
        if loaded_snapshot!=before_load:
            raise TagTemplateOrganizationStaleError(
                "System Knowledge changed while the organization scope was loading. Review the refreshed plan before applying it."
            )
        current_plan=TagTemplateOrganizationPlanningService(tags,categories).preview(
            record,expansion,progress,cancelled,
        )
        if current_plan.plan_fingerprint!=expected_plan.plan_fingerprint:
            raise TagTemplateOrganizationStaleError(
                "The Category or Tag organization plan changed after confirmation. Review the refreshed plan before applying it."
            )
        if not current_plan.items:
            raise ValueError("Choose at least one ready template result before creating Tags.")
        if current_plan.held_count or not current_plan.destination.ready:
            raise ValueError("Every saved selected result must be ready before one atomic Create and organize operation.")
        if self._mutable_snapshot()!=loaded_snapshot:
            raise TagTemplateOrganizationStaleError(
                "System Knowledge changed while the organization plan was being checked. Review the refreshed plan before applying it."
            )
        if cancelled():raise InterruptedError()
        session=CategoryWorkspaceSession(
            tags,categories,default_relationship_path=self.default_relationship_path,
            user_relationship_path=self.user_relationship_path,foundation=foundation,
        )
        if self._mutable_snapshot()!=loaded_snapshot:
            raise TagTemplateOrganizationStaleError(
                "System Knowledge changed before the organization transaction was staged. Review the refreshed plan before applying it."
            )

        planned_created_ids=tuple(
            derived_tag_template_tag_id(item.result_id)
            for item in current_plan.items
            if item.action==TAG_TEMPLATE_ORGANIZE_CREATE
        )
        planned_moved_ids=tuple(
            item.existing_tag_ids[0]
            for item in current_plan.items
            if item.action==TAG_TEMPLATE_ORGANIZE_MOVE
        )
        planned_reused_ids=tuple(
            item.existing_tag_ids[0]
            for item in current_plan.items
            if item.action==TAG_TEMPLATE_ORGANIZE_REUSE
        )
        operation_id=self.journal.begin(
            TAG_TEMPLATE_ORGANIZATION_APPLY_KIND,
            expected=3,
            reason="Create and organize reviewed reusable System Knowledge Tags",
            metadata={
                "template_id":record.id,"template_version":record.version,
                "plan_fingerprint":current_plan.plan_fingerprint,
                "selected_results":len(current_plan.items),
                "category_id":current_plan.destination.category_id,
                "category_created":categories.get(
                    current_plan.destination.category_id,
                ) is None,
                "created_tag_ids":list(planned_created_ids),
                "moved_tag_ids":list(planned_moved_ids),
                "reused_tag_ids":list(planned_reused_ids),
            },
        )
        created_at=_utc_now()
        created_category=False
        created=[]; moved=[]; reused=[]; relationship_ids=[]
        try:
            destination_id=current_plan.destination.category_id
            roles=record.anchor_roles or tuple(
                "base" if index==0 else "component"
                for index in range(len(record.anchor_ids))
            )
            base_ids=tuple(
                stable_id for stable_id,role in zip(record.anchor_ids,roles)
                if role=="base"
            )
            if len(base_ids)!=1:
                raise TagTemplateOrganizationStaleError(
                    "Create and organize needs exactly one reviewed primary Base SDXL Tag."
                )
            anchor_id=base_ids[0]
            additional_anchors=tuple(
                {"tag_id":stable_id,"role":role}
                for stable_id,role in zip(record.anchor_ids,roles)
                if role!="base"
            )
            if current_plan.destination.proposed_base_relationship:
                if not current_plan.destination.reviewed_base_approval:
                    raise TagTemplateOrganizationStaleError(
                        "Review the exact proposed Category Base before applying this organization plan."
                    )
                base_plan=session.preview_category_base_application(
                    current_plan.destination.parent_category_id,anchor_id,
                    expected_match_basis=current_plan.destination.base_match_basis,
                    allow_broader_review=bool(
                        record.destination_policy.get("allow_broader_base_scope",False)
                    ),
                )
                session.apply_category_base_application(base_plan)
            if session.get(destination_id) is None:
                parent=session.get(current_plan.destination.parent_category_id)
                if parent is None:
                    raise TagTemplateOrganizationStaleError("The reviewed destination parent Category is unavailable.")
                slot_labels=tuple(dict.fromkeys(
                    str(item.get("family_label") or item.get("family_id") or "")
                    for item in record.slot_records
                ))
                default_label=(
                    slot_labels[0] if len(slot_labels)==1 else
                    " + ".join(slot_labels)+" Combinations"
                )
                category_name=(
                    str(record.destination_policy.get("label") or "").strip()
                    or f"{parent.name} — {default_label}"
                )
                session.create_category(
                    category_name,
                    parent_id=parent.id,stable_id=destination_id,
                    extras={"tag_template_destination":{
                        "schema_version":1,"parent_category_id":parent.id,
                        "modifier_family_ids":list(record.modifier_family_ids),
                        "template_id":record.id,"template_version":record.version,
                        "operation_id":operation_id,"created_at":created_at,
                    }},
                )
                created_category=True
            expansion_by_id={item.id:item for item in expansion.items}
            move_ids=[]; staged_entries=[]; staged_actions=[]
            total=len(current_plan.items)
            for index,item in enumerate(current_plan.items,1):
                if cancelled():raise InterruptedError()
                expanded=expansion_by_id.get(item.result_id)
                expanded_value_ids=tuple(getattr(
                    expanded,"modifier_value_ids",(getattr(expanded,"value_id",""),)
                )) if expanded is not None else ()
                if expanded is None or expanded_value_ids!=item.modifier_value_ids:
                    raise TagTemplateOrganizationStaleError("A selected expansion result changed before application.")
                if item.action==TAG_TEMPLATE_ORGANIZE_CREATE:
                    stable_id=derived_tag_template_tag_id(item.result_id)
                elif item.action in {TAG_TEMPLATE_ORGANIZE_MOVE,TAG_TEMPLATE_ORGANIZE_REUSE} and len(item.existing_tag_ids)==1:
                    stable_id=item.existing_tag_ids[0]
                else:
                    raise TagTemplateOrganizationStaleError("A selected result is no longer deterministically applicable.")
                staged_entries.append({
                    "tag_id":stable_id,"display":item.tag,
                    "category_id":destination_id,"anchor_id":anchor_id,
                    "additional_anchors":list(additional_anchors),
                    "modifier_values":[
                        {"family_id":family_id,"value_id":value_id}
                        for family_id,value_id in zip(
                            item.modifier_family_ids,item.modifier_value_ids,
                        )
                    ],
                    "selected_slot_values":[
                        {"slot_id":slot_id,"family_id":family_id,"value_id":value_id}
                        for slot_id,family_id,value_id,_label in getattr(expanded,"slot_values",())
                    ],
                    "template_id":record.id,
                    "template_version":record.version,"result_id":item.result_id,
                    "operation_id":operation_id,"created_at":created_at,
                })
                staged_actions.append((stable_id,item.action))
                if item.action==TAG_TEMPLATE_ORGANIZE_MOVE:move_ids.append(stable_id)
                if progress:progress(index,total,f"Preparing {item.tag}")
            created_ids,changed_relationships=session.stage_reviewed_template_tags(
                staged_entries,
            )
            created.extend(created_ids); relationship_ids.extend(changed_relationships)
            created_set=set(created_ids)
            for stable_id,action in staged_actions:
                if stable_id in created_set:continue
                if action==TAG_TEMPLATE_ORGANIZE_MOVE:moved.append(stable_id)
                else:reused.append(stable_id)
            if move_ids:session.change_tag_categories(move_ids,destination_id)
            if not session.has_changes:
                self.journal.finish(
                    operation_id,"cancelled",message="The reviewed template organization was already current.",
                )
                return TagTemplateOrganizationApplyResult(
                    operation_id,record.id,record.version,current_plan.plan_fingerprint,
                    (),(),tuple(reused),(),destination_id,False,None,False,
                )
            payloads=session.transaction_payloads()
            resolved_payloads={path.resolve():payload for path,payload in payloads.items()}
            required_paths={self.tags_path.resolve(),self.category_user_path.resolve()}
            if (
                not required_paths.issubset(resolved_payloads)
                or not set(resolved_payloads).issubset(self.allowed_paths)
            ):
                raise RuntimeError("The template transaction did not resolve the approved System Knowledge stores.")
            expected_after={}
            for path in sorted(self.allowed_paths,key=str):
                before,existed=self._disk_payload(path)
                after=resolved_payloads.get(path,before)
                exists_after=True if path in resolved_payloads else existed
                expected_after[path]=(after,exists_after)
                self.journal.append_change(
                    operation_id,item=str(path.resolve()),before=before,
                    after=after,reason="Apply reviewed template System Knowledge",
                    metadata={
                        "existed_before":existed,"exists_after":exists_after,
                    },
                )
            if cancelled():raise InterruptedError()
            saved=session.save(recovery_id=operation_id)
            for path,(expected,expected_exists) in expected_after.items():
                actual,exists=self._disk_payload(path)
                observed=actual if exists else None
                if not self._current_matches(observed,expected,expected_exists):
                    raise RuntimeError("Template organization failed final operation read-back verification.")
        except BaseException as exc:
            try:self.journal.finish(
                operation_id,"cancelled" if isinstance(exc,InterruptedError) else "failed",
                message=str(exc),
            )
            except Exception:pass
            raise
        result={
            "template_id":record.id,"template_version":record.version,
            "plan_fingerprint":current_plan.plan_fingerprint,
            "created_tag_ids":created,"moved_tag_ids":moved,"reused_tag_ids":reused,
            "relationship_ids":list(dict.fromkeys(relationship_ids)),
            "category_id":current_plan.destination.category_id,
            "category_created":created_category,
            "backup_directory":str(saved.backup_directory),
        }
        self.journal.finish(operation_id,"complete",result=result)
        self.journal.prune()
        return TagTemplateOrganizationApplyResult(
            operation_id,record.id,record.version,current_plan.plan_fingerprint,
            tuple(created),tuple(moved),tuple(reused),
            tuple(dict.fromkeys(relationship_ids)),current_plan.destination.category_id,
            created_category,saved.backup_directory,True,
        )

    def undoable_operation(self) -> JournalOperation|None:
        return self.journal.latest(
            kind=TAG_TEMPLATE_ORGANIZATION_APPLY_KIND,statuses=("complete",),
        )

    def _operation_changes(self,operation: JournalOperation):
        changes=[]
        for change in operation.changes:
            path=Path(change["item"]).resolve()
            if path not in self.allowed_paths:
                raise RuntimeError("A template operation contains an unexpected System Knowledge file.")
            before=self.journal.load_snapshot(change["before"])
            after=self.journal.load_snapshot(change["after"])
            if not isinstance(before,dict) or not isinstance(after,dict):
                raise RuntimeError("A template operation is missing a valid recovery snapshot.")
            metadata=dict(change.get("metadata") or {})
            before_exists=bool(metadata.get("existed_before",True))
            after_exists=bool(metadata.get("exists_after",True))
            changes.append((path,before,before_exists,after,after_exists))
        if len(changes)!=len(self.allowed_paths) or {item[0] for item in changes}!=self.allowed_paths:
            raise RuntimeError("A template operation does not cover all three System Knowledge stores.")
        return changes

    def _backup_entries(
        self,operation: JournalOperation,
    ) -> tuple[Path,dict[Path,tuple[bytes|None,bool]]]:
        backup_directory=Path(str(
            operation.result.get("backup_directory")
            or self.tags_path.parent/"backups"/"category-workspace"/operation.id
        )).resolve()
        expected_backup_root=(
            self.tags_path.parent/"backups"/"category-workspace"
        ).resolve()
        if backup_directory.parent!=expected_backup_root:
            raise RuntimeError("The recorded template recovery directory is outside the approved System Knowledge backup root.")
        manifest_path=backup_directory/"recovery.json"
        try:manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError,json.JSONDecodeError) as exc:
            raise RuntimeError("The completed template operation is missing its verified recovery manifest.") from exc
        if manifest.get("status")!="complete":
            raise RuntimeError("The template operation recovery manifest is not complete.")
        entries=tuple(manifest.get("files",()))
        backup_entries={}
        for entry in entries:
            path=Path(str(entry.get("target") or "")).resolve()
            backup=(backup_directory/str(entry.get("backup") or "")).resolve()
            if path not in self.allowed_paths or backup.parent!=backup_directory:
                raise RuntimeError("The template recovery manifest contains an unsafe file.")
            raw=(backup.read_bytes() if entry.get("existed") else None)
            backup_entries[path]=(raw,bool(entry.get("existed")))
        if len(backup_entries)!=len(entries):
            raise RuntimeError("The template recovery manifest repeats a System Knowledge store.")
        return backup_directory,backup_entries

    def undo_last(self) -> str:
        with self._admission():
            return self._undo_last_admitted()

    def _undo_last_admitted(self) -> str:
        operation=self.undoable_operation()
        if operation is None:
            raise ValueError("There is no completed reusable Tag organization to restore.")
        changes=self._operation_changes(operation)
        _backup_directory,backup_entries=self._backup_entries(operation)
        current=[]
        for path,before,before_exists,after,after_exists in changes:
            value,exists=self._disk_payload(path)
            observed=value if exists else None
            if not self._current_matches(observed,after,after_exists):
                raise RuntimeError(
                    "System Knowledge changed after this template operation. Review current Tags, Categories, and relationships before restoring it."
                )
            backup_entry=backup_entries.get(path)
            if backup_entry is None:
                if before_exists!=after_exists or before!=after:
                    raise RuntimeError("The template recovery manifest omits a changed System Knowledge store.")
                raw=None; restore=False
            else:
                raw,raw_exists=backup_entry
                if raw_exists!=before_exists:
                    raise RuntimeError("The template recovery manifest contradicts its operation snapshot.")
                restore=True
            current.append((
                path,value,exists,path.read_bytes() if exists else None,
                before,before_exists,raw,restore,
            ))
        undo_id=self.journal.begin(
            TAG_TEMPLATE_ORGANIZATION_UNDO_KIND,expected=len(current),
            reason="Restore exact System Knowledge from before reusable Tag organization",
            metadata={"restores":operation.id},
        )
        for path,value,exists,_current_raw,before,before_exists,_before_raw,_restore in current:
            self.journal.append_change(
                undo_id,item=str(path),before=value,after=before,
                reason="Restore exact pre-template System Knowledge",
                metadata={"existed_before":exists,"exists_after":before_exists},
            )
        written=[]
        try:
            for path,_value,_exists,current_raw,_before,before_exists,before_raw,restore in current:
                if not restore:continue
                self._write_bytes(path,before_raw if before_exists else None)
                written.append((path,current_raw))
            for path,_value,_exists,_current_raw,_before,before_exists,before_raw,restore in current:
                if not restore:continue
                if (
                    path.exists()!=before_exists
                    or (before_exists and path.read_bytes()!=before_raw)
                ):
                    raise RuntimeError("Restored template System Knowledge failed read-back verification.")
        except BaseException as exc:
            for path,current_raw in reversed(written):
                self._write_bytes(path,current_raw)
            self.journal.finish(undo_id,"failed",message=str(exc)); raise
        self.journal.finish(undo_id,"complete",result={"restores":operation.id})
        self.journal.finish(operation.id,"undone")
        self.journal.prune()
        return operation.id

    def _complete_interrupted_undo(
        self,operation: JournalOperation,original: JournalOperation,
        changes,states,
    ) -> None:
        _backup_directory,backup_entries=self._backup_entries(original)
        for state,(path,_before,_before_exists,after,after_exists) in zip(states,changes):
            if state!="before":continue
            backup_entry=backup_entries.get(path)
            if backup_entry is None:
                raise RuntimeError(
                    "The original template recovery manifest omits a store needed to finish Undo."
                )
            raw,raw_exists=backup_entry
            if raw_exists!=after_exists:
                raise RuntimeError(
                    "The original template recovery manifest contradicts the interrupted Undo."
                )
            self._write_bytes(path,raw if raw_exists else None)
        for path,_before,_before_exists,after,after_exists in changes:
            value,exists=self._disk_payload(path)
            observed=value if exists else None
            if not self._current_matches(observed,after,after_exists):
                raise RuntimeError("The interrupted template Undo failed final recovery verification.")
            backup_entry=backup_entries.get(path)
            if backup_entry is not None:
                raw,raw_exists=backup_entry
                if path.exists()!=raw_exists or (
                    raw_exists and path.read_bytes()!=raw
                ):
                    raise RuntimeError("The interrupted template Undo failed exact-byte verification.")
        self.journal.finish(
            operation.id,"complete",
            message="Finished the exact interrupted reusable Tag organization restore.",
            result={"restores":original.id},
        )
        self.journal.finish(original.id,"undone")

    def recover_interrupted(self) -> tuple[str,...]:
        with self._admission():
            return self._recover_interrupted_admitted()

    def _recover_interrupted_admitted(self) -> tuple[str,...]:
        recovered=[]
        operations=self.journal.operations()
        operation_by_id={operation.id:operation for operation in operations}
        for operation in operations:
            if operation.status!="applying" or operation.kind not in {
                TAG_TEMPLATE_ORGANIZATION_APPLY_KIND,TAG_TEMPLATE_ORGANIZATION_UNDO_KIND,
            }:continue
            if len(operation.changes)<3:
                self.journal.finish(
                    operation.id,"failed",
                    message="Interrupted before the complete System Knowledge recovery scope was journaled; no knowledge write had begun.",
                )
                recovered.append(operation.id); continue
            changes=self._operation_changes(operation)
            states=[]
            for path,before,before_exists,after,after_exists in changes:
                value,exists=self._disk_payload(path); observed=value if exists else None
                matches_before=self._current_matches(observed,before,before_exists)
                matches_after=self._current_matches(observed,after,after_exists)
                if matches_before and matches_after:states.append("same")
                elif matches_before:states.append("before")
                elif matches_after:states.append("after")
                else:
                    raise RuntimeError(
                        "System Knowledge changed after an interrupted reusable Tag operation. Recovery refused to overwrite it."
                    )
            if any(state=="after" for state in states) and all(
                state in {"after","same"} for state in states
            ):
                result={}
                if operation.kind==TAG_TEMPLATE_ORGANIZATION_APPLY_KIND:
                    backup_directory,_backup_entries=self._backup_entries(operation)
                    result={
                        "template_id":str(operation.metadata.get("template_id") or ""),
                        "template_version":int(operation.metadata.get("template_version") or 0),
                        "plan_fingerprint":str(operation.metadata.get("plan_fingerprint") or ""),
                        "category_id":str(operation.metadata.get("category_id") or ""),
                        "category_created":bool(operation.metadata.get("category_created")),
                        "created_tag_ids":list(operation.metadata.get("created_tag_ids") or ()),
                        "moved_tag_ids":list(operation.metadata.get("moved_tag_ids") or ()),
                        "reused_tag_ids":list(operation.metadata.get("reused_tag_ids") or ()),
                        "backup_directory":str(backup_directory),
                    }
                self.journal.finish(
                    operation.id,"complete",
                    message="Verified the completed interrupted reusable Tag operation.",
                    result=result,
                )
                if operation.kind==TAG_TEMPLATE_ORGANIZATION_UNDO_KIND:
                    restored=str(operation.metadata.get("restores") or "")
                    if restored:self.journal.finish(restored,"undone")
                recovered.append(operation.id); continue
            if operation.kind==TAG_TEMPLATE_ORGANIZATION_UNDO_KIND and any(
                state=="after" for state in states
            ):
                original_id=str(operation.metadata.get("restores") or "")
                original=operation_by_id.get(original_id)
                if (
                    original is None
                    or original.kind!=TAG_TEMPLATE_ORGANIZATION_APPLY_KIND
                ):
                    raise RuntimeError(
                        "The interrupted template Undo is missing its original operation history."
                    )
                self._complete_interrupted_undo(
                    operation,original,changes,states,
                )
                recovered.append(operation.id); continue
            if any(state=="after" for state in states):
                restored=[]
                try:
                    for state,(path,before,before_exists,after,after_exists) in zip(states,changes):
                        if state!="after":continue
                        self._write_payload(path,before if before_exists else None)
                        restored.append((path,after,after_exists))
                except BaseException:
                    for path,after,after_exists in reversed(restored):
                        self._write_payload(path,after if after_exists else None)
                    raise
            self.journal.finish(
                operation.id,"failed",
                message="Recovered exact pre-operation System Knowledge after interruption.",
            )
            recovered.append(operation.id)
        if recovered:self.journal.prune()
        return tuple(recovered)


__all__=[
    "TAG_TEMPLATE_ORGANIZATION_APPLY_KIND",
    "TAG_TEMPLATE_ORGANIZATION_UNDO_KIND",
    "TagTemplateOrganizationApplyResult",
    "TagTemplateOrganizationApplyService",
    "derived_tag_template_tag_id",
]
