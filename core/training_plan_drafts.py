from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import re
import shutil
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime,timezone
from pathlib import Path,PurePosixPath
from threading import RLock
from typing import Any,Callable
from uuid import uuid4

from .export_profile import ExportProfile
from .export_profile_store import validate_export_profile
from .federated_dataset import LibraryImageIdentity
from .training_candidate_selection import (
    CandidateOverride,PERMISSION_FIELD_LABELS,TrainingCandidatePlan,TrainingGoal,
    TrainingPermissionDisposition,
)
from .training_configuration import TrainingConfiguration,validate_training_configuration


TRAINING_PLAN_DRAFT_STORE_SCHEMA_VERSION=1
_DRAFT_ID=re.compile(r"^draft\.[a-f0-9]{32}$")
_DRAFT_LOCKS_GUARD=RLock()
_DRAFT_LOCKS: dict[str,RLock]={}


def _draft_lock(path: Path) -> RLock:
    key=str(path.resolve(strict=False)).casefold()
    with _DRAFT_LOCKS_GUARD:
        return _DRAFT_LOCKS.setdefault(key,RLock())


class TrainingPlanDraftError(RuntimeError):
    pass


@dataclass(frozen=True)
class TrainingPlanDraftIssue:
    location: str
    message: str


@dataclass(frozen=True)
class TrainingDraftResolution:
    paths: tuple[Path,...]
    identities: dict[Path,LibraryImageIdentity]
    unresolved: tuple[str,...]

    @property
    def ready(self) -> bool:
        return bool(self.paths) and not self.unresolved


def _stable_hash(value: object) -> str:
    encoded=json.dumps(
        value,ensure_ascii=False,sort_keys=True,separators=(",",":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _clean_text(value: Any,field: str,maximum: int) -> str:
    text=" ".join(str(value or "").split())
    if not text:
        raise TrainingPlanDraftError(f"Enter {field}.")
    if len(text)>maximum:
        raise TrainingPlanDraftError(
            f"{field.title()} must be {maximum:,} characters or fewer."
        )
    return text


def _safe_relative_path(value: Any) -> str:
    text=str(value or "").replace("\\","/").strip()
    relative=PurePosixPath(text)
    if (
        not text or text=="." or relative.is_absolute() or ".." in relative.parts
        or ":" in relative.parts[0]
    ):
        raise TrainingPlanDraftError(
            "A saved library image has an unsafe relative path."
        )
    return relative.as_posix()


def _looks_absolute_path(value: str) -> bool:
    raw=str(value or "")
    if (
        re.search(r"(?<![a-zA-Z0-9])[a-zA-Z]:[\\/]",raw)
        or re.search(r"\\\\[^\\\s]+[\\/]",raw)
    ):
        return True
    text=re.sub(r"https?://\S+","",raw,flags=re.IGNORECASE)
    return bool(re.search(r"(?<![a-zA-Z0-9])/(?:[^/\s]+/)+",text))


def _assert_source_neutral(
    value: Any,location: str="plan",*,should_cancel: Callable[[],bool]|None=None,
) -> None:
    visited=0
    def visit(item: Any,position: str) -> None:
        nonlocal visited
        if visited%128==0 and should_cancel is not None and should_cancel():
            raise InterruptedError("Training plan validation was cancelled.")
        visited+=1
        if isinstance(item,dict):
            for key,child in item.items():
                visit(child,f"{position}.{key}")
        elif isinstance(item,list):
            for index,child in enumerate(item):
                visit(child,f"{position}[{index}]")
        elif isinstance(item,str) and _looks_absolute_path(item):
            raise TrainingPlanDraftError(
                f"A saved draft contains a machine-local absolute path at {position}."
            )
    visit(value,location)


def _validate_source_record(record: Any,*,frozen: bool) -> dict[str,object]:
    if not isinstance(record,dict):
        raise TrainingPlanDraftError("A saved candidate identity is not an object.")
    kind=str(record.get("kind") or "")
    if kind=="library_image":
        if set(record)!={"kind","library_id","relative_image_path"}:
            raise TrainingPlanDraftError(
                "A saved library identity contains unsupported fields."
            )
        library_id=_clean_text(record.get("library_id"),"a library ID",160)
        return {
            "kind":kind,
            "library_id":library_id,
            "relative_image_path":_safe_relative_path(
                record.get("relative_image_path")
            ),
        }
    if frozen and kind=="local_selection":
        if set(record)!={"kind","filename_hint","selection_key"}:
            raise TrainingPlanDraftError(
                "A local selection identity contains unsupported fields."
            )
        selection_key=str(record.get("selection_key") or "")
        if not re.fullmatch(r"[a-f0-9]{64}",selection_key):
            raise TrainingPlanDraftError(
                "A local selection does not have a valid opaque identity."
            )
        return {
            "kind":kind,
            "filename_hint":Path(str(record.get("filename_hint") or "image")).name,
            "selection_key":selection_key,
        }
    if not frozen and kind=="content_fingerprint":
        if set(record)!={"kind","filename_hint","sha256"}:
            raise TrainingPlanDraftError(
                "A content identity contains unsupported fields."
            )
        digest=str(record.get("sha256") or "")
        if not re.fullmatch(r"[a-f0-9]{64}",digest):
            raise TrainingPlanDraftError(
                "A content-identified candidate has an invalid fingerprint."
            )
        return {
            "kind":kind,
            "filename_hint":Path(str(record.get("filename_hint") or "image")).name,
            "sha256":digest,
        }
    if not frozen and kind=="unavailable":
        if set(record)!={"kind","filename_hint"}:
            raise TrainingPlanDraftError(
                "An unavailable identity contains unsupported fields."
            )
        return {
            "kind":kind,
            "filename_hint":Path(str(record.get("filename_hint") or "image")).name,
        }
    raise TrainingPlanDraftError(f"Unsupported saved candidate identity: {kind or 'empty'}.")


def _validate_dataset_analysis(
    value: Any,*,stable_ids: set[str],recommended_included: set[str],
    schema_version: int,
) -> None:
    if not isinstance(value,dict):
        raise TrainingPlanDraftError("A current draft has no valid dataset analysis.")
    rule=value.get("split_rule")
    if not isinstance(rule,dict):
        raise TrainingPlanDraftError("A draft has no valid split rule.")
    _clean_text(rule.get("version"),"a split rule version",120)
    seed=str(rule.get("seed") or "")
    if not re.fullmatch(r"[a-f0-9]{24}",seed):
        raise TrainingPlanDraftError("A draft has an invalid split seed.")
    targets=rule.get("role_targets_percent")
    if not isinstance(targets,dict) or set(targets)!={
        "training","validation","holdout",
    }:
        raise TrainingPlanDraftError("A draft has invalid split role targets.")
    try:
        percentages={key:int(value) for key,value in targets.items()}
    except (TypeError,ValueError) as exc:
        raise TrainingPlanDraftError("A draft split target is invalid.") from exc
    if any(value<0 or value>100 for value in percentages.values()) or sum(
        percentages.values()
    )!=100:
        raise TrainingPlanDraftError("Draft split targets must total 100 percent.")
    groups=value.get("groups")
    if not isinstance(groups,list):
        raise TrainingPlanDraftError("A draft has no valid split groups.")
    assigned=set(); group_ids=set()
    for group in groups:
        if not isinstance(group,dict):
            raise TrainingPlanDraftError("A draft split group is not an object.")
        group_id=_clean_text(group.get("group_id"),"a split group ID",120)
        if group_id in group_ids:
            raise TrainingPlanDraftError("A draft split group ID is duplicated.")
        group_ids.add(group_id)
        allowed_evidence={"independent","byte_identical"}
        if schema_version>=6:
            allowed_evidence.add("source_video")
        if group.get("evidence_kind") not in allowed_evidence:
            raise TrainingPlanDraftError("A draft split group has invalid evidence.")
        if group.get("recommended_role") not in {
            "training","validation","holdout",
        }:
            raise TrainingPlanDraftError("A draft split recommendation is invalid.")
        members=group.get("member_stable_ids")
        if not isinstance(members,list) or not members:
            raise TrainingPlanDraftError("A draft split group has no members.")
        member_ids={str(item) for item in members}
        if len(member_ids)!=len(members) or not member_ids<=stable_ids:
            raise TrainingPlanDraftError("A draft split group has invalid members.")
        if assigned.intersection(member_ids):
            raise TrainingPlanDraftError("A candidate belongs to multiple split groups.")
        assigned.update(member_ids)
    if assigned!=recommended_included:
        raise TrainingPlanDraftError(
            "Draft split groups do not match the recommended included candidates."
        )
    findings=value.get("findings")
    if not isinstance(findings,list):
        raise TrainingPlanDraftError("A draft has no valid dataset findings.")
    finding_codes=set()
    for finding in findings:
        if not isinstance(finding,dict):
            raise TrainingPlanDraftError("A draft dataset finding is not an object.")
        code=_clean_text(finding.get("code"),"a dataset finding code",160)
        if code in finding_codes:
            raise TrainingPlanDraftError("A draft dataset finding is duplicated.")
        finding_codes.add(code)
        if finding.get("category") not in {
            "Split integrity","Balance","Diversity","Correlation","Leakage",
        }:
            raise TrainingPlanDraftError("A draft dataset finding category is invalid.")
        if finding.get("level") not in {"observed","caution","recommendation"}:
            raise TrainingPlanDraftError("A draft dataset finding level is invalid.")
        try:
            affected=int(finding.get("affected_count"))
            denominator=int(finding.get("denominator"))
        except (TypeError,ValueError) as exc:
            raise TrainingPlanDraftError("A draft dataset finding count is invalid.") from exc
        if affected<0 or denominator<0 or affected>denominator:
            raise TrainingPlanDraftError("A draft dataset finding count is invalid.")
        affected_ids=finding.get("stable_ids",[])
        if not isinstance(affected_ids,list) or not {
            str(item) for item in affected_ids
        }<=stable_ids:
            raise TrainingPlanDraftError("A draft dataset finding has invalid members.")
    limitations=value.get("limitations")
    if not isinstance(limitations,list) or any(
        not isinstance(item,str) or not item.strip() for item in limitations
    ):
        raise TrainingPlanDraftError("A draft dataset-analysis limitation is invalid.")


def _validate_video_split_components(
    analysis: dict[str,object],candidates: list[dict[str,object]],
    recommended_included: set[str],
) -> None:
    """Recompute schema-six split components from retained exact SHA evidence."""
    from .training_dataset_analysis import (
        SPLIT_RULE_VERSION,VIDEO_SPLIT_RULE_VERSION,source_video_components,
    )

    included=tuple(candidate for candidate in candidates
                   if candidate["stable_id"] in recommended_included)
    has_video=any(candidate.get("derived_frame") is not None for candidate in included)
    expected_rule=VIDEO_SPLIT_RULE_VERSION if has_video else SPLIT_RULE_VERSION
    if analysis["split_rule"].get("version")!=expected_rule:
        raise TrainingPlanDraftError("A saved video split rule does not match its retained frame evidence.")
    if not has_video:
        # No-frame schema-six plans keep the pre-video grouped-hash semantics.
        if any(group["evidence_kind"]=="source_video" for group in analysis["groups"]):
            raise TrainingPlanDraftError(
                "A saved source-video split group has no retained frame evidence."
            )
        return
    expected=set(source_video_components(included))
    actual={frozenset(group["member_stable_ids"]):group
            for group in analysis["groups"]}
    if set(actual)!=expected:
        raise TrainingPlanDraftError(
            "Saved split groups do not match the retained source-video and exact-byte components."
        )
    frames={candidate["stable_id"]:candidate.get("derived_frame") for candidate in included}
    for members,group in actual.items():
        kind=group["evidence_kind"]
        expected_kind=("source_video" if any(frames[item] is not None for item in members)
                       else "byte_identical" if len(members)>1 else "independent")
        if kind!=expected_kind:
            raise TrainingPlanDraftError(
                "A saved source-video split group has inconsistent lineage evidence."
            )
        if kind=="source_video":
            clips=sorted({frames[item]["source_sha256"] for item in members
                          if frames[item] is not None})
            if group.get("evidence")!=["source_sha256="+digest for digest in clips]:
                raise TrainingPlanDraftError(
                    "A saved source-video split group has inconsistent clip evidence."
                )


def _validate_permission_evidence(value: Any) -> dict[str,object]:
    if (
        not isinstance(value,dict)
        or type(value.get("schema_version")) is not int
        or value.get("schema_version")!=1
    ):
        raise TrainingPlanDraftError(
            "A current draft candidate has no valid permission evidence snapshot."
        )
    expected={"schema_version","issues",*(name for name,_ in PERMISSION_FIELD_LABELS)}
    if set(value)!=expected:
        raise TrainingPlanDraftError(
            "A draft permission evidence snapshot contains unsupported fields."
        )
    normalized={"schema_version":1}
    for name,label in PERMISSION_FIELD_LABELS:
        field=value.get(name)
        if not isinstance(field,dict) or set(field)!={
            "status","value","evidence_source",
        }:
            raise TrainingPlanDraftError(
                f"A draft {label.casefold()} evidence field is invalid."
            )
        status=str(field.get("status") or "")
        text=str(field.get("value") or "")
        source=str(field.get("evidence_source") or "")
        if status not in {"unknown","reported"}:
            raise TrainingPlanDraftError(
                f"A draft {label.casefold()} evidence status is invalid."
            )
        if status=="unknown" and (text or source):
            raise TrainingPlanDraftError(
                f"Unknown {label.casefold()} evidence cannot claim a value or source."
            )
        if status=="reported" and (
            not text.strip() or not source.strip() or len(text)>2_000
            or len(source)>500
        ):
            raise TrainingPlanDraftError(
                f"Reported {label.casefold()} evidence is incomplete or too long."
            )
        normalized[name]={
            "status":status,"value":text,"evidence_source":source,
        }
    raw_issues=value.get("issues")
    if not isinstance(raw_issues,list) or len(raw_issues)>10 or any(
        not isinstance(item,str) or not item.strip() or len(item)>1_000
        for item in raw_issues
    ):
        raise TrainingPlanDraftError("A draft permission evidence issue is invalid.")
    normalized["issues"]=list(raw_issues)
    return normalized


def _validate_permission_review(
    value: Any,*,included_permission_scope: list[dict[str,object]],
) -> None:
    if not isinstance(value,dict) or set(value)!={
        "status","reason","unknown_evidence_acknowledged",
        "evidence_scope_fingerprint","policy_version",
        "legal_permission_adjudicated",
    }:
        raise TrainingPlanDraftError("A current draft has no valid permission review.")
    if value.get("status") not in {"pending","proceed","needs_review"}:
        raise TrainingPlanDraftError("A draft permission disposition is invalid.")
    reason=str(value.get("reason") or "")
    if len(reason)>4_000 or (
        value.get("status")!="pending" and not reason.strip()
    ):
        raise TrainingPlanDraftError("A draft permission disposition reason is invalid.")
    if not isinstance(value.get("unknown_evidence_acknowledged"),bool):
        raise TrainingPlanDraftError("A draft permission acknowledgement is invalid.")
    if value.get("policy_version")!="collectigen.training-permission-review-v1":
        raise TrainingPlanDraftError("A draft permission policy version is invalid.")
    if value.get("legal_permission_adjudicated") is not False:
        raise TrainingPlanDraftError(
            "A draft cannot claim that CollectiGen adjudicated legal permission."
        )
    expected=_stable_hash(included_permission_scope)
    if value.get("evidence_scope_fingerprint")!=expected:
        raise TrainingPlanDraftError(
            "A draft permission disposition does not match its included evidence scope."
        )
    has_unknown=any(
        field["status"]=="unknown"
        for item in included_permission_scope
        for name,_label in PERMISSION_FIELD_LABELS
        for field in (item["permission_evidence"][name],)
    )
    if (
        value.get("status")=="proceed" and has_unknown
        and not value.get("unknown_evidence_acknowledged")
    ):
        raise TrainingPlanDraftError(
            "A proceed disposition must acknowledge unknown permission evidence."
        )


def _validated_manifest(
    value: Any,*,should_cancel: Callable[[],bool]|None=None,
) -> dict[str,object]:
    def checkpoint() -> None:
        if should_cancel is not None and should_cancel():
            raise InterruptedError("Training plan validation was cancelled.")
    checkpoint()
    if not isinstance(value,dict):
        raise TrainingPlanDraftError("A saved training plan is not an object.")
    manifest=deepcopy(value)
    checkpoint()
    if manifest.get("kind")!="collectigen_training_candidate_plan":
        raise TrainingPlanDraftError("A draft does not contain a training candidate plan.")
    try:
        schema_version=int(manifest.get("schema_version") or 0)
    except (TypeError,ValueError) as exc:
        raise TrainingPlanDraftError("A draft plan has an invalid schema version.") from exc
    if schema_version not in {1,2,3,4,5,6}:
        raise TrainingPlanDraftError(
            "A draft plan uses an unsupported schema version."
        )
    goal=manifest.get("goal")
    if not isinstance(goal,dict):
        raise TrainingPlanDraftError("A draft has no valid training goal.")
    _clean_text(goal.get("text"),"a plain-language training goal",2_000)
    if str(goal.get("kind") or "") not in {
        "character","style","product","concept",
    }:
        raise TrainingPlanDraftError("A draft has an unsupported training goal type.")
    profile_payload=manifest.get("profile")
    if not isinstance(profile_payload,dict):
        raise TrainingPlanDraftError("A draft has no valid projection profile.")
    try:
        validate_export_profile(ExportProfile.from_dict(profile_payload))
    except (TypeError,ValueError) as exc:
        raise TrainingPlanDraftError(
            f"A draft has an invalid projection profile: {exc}"
        ) from exc
    scope=manifest.get("selection_scope")
    if not isinstance(scope,dict):
        raise TrainingPlanDraftError("A draft has no exact selection scope.")
    frozen=scope.get("frozen_selection")
    candidates=manifest.get("candidates")
    if not isinstance(frozen,list) or not frozen:
        raise TrainingPlanDraftError("A draft has no saved candidate selection.")
    if not isinstance(candidates,list) or len(candidates)!=len(frozen):
        raise TrainingPlanDraftError(
            "A draft does not preserve one inspected candidate per selected image."
        )
    if scope.get("not_processed") not in ([],()):
        raise TrainingPlanDraftError(
            "A persisted draft must contain a fully inspected selection."
        )
    try:
        candidate_count=int(scope.get("candidate_count") or 0)
        processed_count=int(scope.get("processed_count") or 0)
    except (TypeError,ValueError) as exc:
        raise TrainingPlanDraftError("A draft scope count is invalid.") from exc
    if candidate_count!=len(frozen):
        raise TrainingPlanDraftError("A draft candidate count does not match its scope.")
    if processed_count!=len(candidates):
        raise TrainingPlanDraftError("A draft processed count does not match its scope.")
    _clean_text(scope.get("source_label"),"a selection source label",200)
    normalized_frozen=[]
    for item in frozen:
        checkpoint()
        normalized_frozen.append(_validate_source_record(item,frozen=True))
    if scope.get("scope_fingerprint")!=_stable_hash(normalized_frozen):
        raise TrainingPlanDraftError("A draft selection fingerprint does not match.")
    checkpoint()
    stable_ids=set(); recommended_included=set(); included_permission_scope=[]
    for index,candidate in enumerate(candidates):
        checkpoint()
        if not isinstance(candidate,dict):
            raise TrainingPlanDraftError("A saved candidate is not an object.")
        stable_id=_clean_text(candidate.get("stable_id"),"a candidate stable ID",500)
        if stable_id in stable_ids:
            raise TrainingPlanDraftError("A saved candidate stable ID is duplicated.")
        stable_ids.add(stable_id)
        candidate_source=_validate_source_record(
            candidate.get("source"),frozen=False
        )
        frozen_source=normalized_frozen[index]
        if frozen_source["kind"]=="library_image":
            if candidate_source!=frozen_source:
                raise TrainingPlanDraftError(
                    "A saved candidate does not match its ordered library selection."
                )
            expected_id=(
                f"{frozen_source['library_id']}:"
                f"{frozen_source['relative_image_path']}"
            )
            if stable_id!=expected_id:
                raise TrainingPlanDraftError(
                    "A saved candidate stable ID does not match its library identity."
                )
        elif candidate_source["kind"] not in {
            "content_fingerprint","unavailable",
        }:
            raise TrainingPlanDraftError(
                "A local draft candidate has an incompatible source identity."
            )
        if candidate.get("decision") not in {"include","exclude"}:
            raise TrainingPlanDraftError("A saved candidate decision is invalid.")
        if candidate.get("role") not in {"training","validation","holdout"}:
            raise TrainingPlanDraftError("A saved candidate role is invalid.")
        if candidate.get("user_override") and not str(
            candidate.get("decision_reason") or ""
        ).strip():
            raise TrainingPlanDraftError(
                "A saved candidate override has no recorded reason."
            )
        if schema_version>=6:
            if "derived_frame" not in candidate:
                raise TrainingPlanDraftError("A current draft candidate has no derived-frame snapshot.")
            frame=candidate["derived_frame"]
            if frame is not None:
                from .video_generation import (
                    DerivedFrameReference, FRAME_SCHEMA_ID, SCHEMA_VERSION,
                )
                source_state=candidate.get("source_state")
                source_file=source_state.get("file") if isinstance(source_state,dict) else None
                image_sha=source_file.get("sha256") if isinstance(source_file,dict) else None
                try:
                    if (not isinstance(frame,dict)
                            or type(frame.get("schema_version")) is not int
                            or frame["schema_version"]!=SCHEMA_VERSION
                            or frame.get("schema_id")!=FRAME_SCHEMA_ID):
                        raise ValueError("derived_frame_schema_invalid")
                    reference=DerivedFrameReference.from_dict(frame)
                    if (reference.to_dict()!=frame
                            or reference.output_sha256!=image_sha):
                        raise ValueError("derived_frame_image_mismatch")
                except (TypeError,ValueError,KeyError) as exc:
                    raise TrainingPlanDraftError(
                        "A saved derived frame does not match its exact image evidence."
                    ) from exc
        if schema_version>=2:
            recommendation=candidate.get("recommendation")
            if not isinstance(recommendation,dict):
                raise TrainingPlanDraftError(
                    "A current draft candidate has no valid recommendation."
                )
            if recommendation.get("decision") not in {"include","exclude"}:
                raise TrainingPlanDraftError(
                    "A draft candidate recommendation is invalid."
                )
            if recommendation.get("role") not in {
                "training","validation","holdout",
            }:
                raise TrainingPlanDraftError(
                    "A draft candidate recommended role is invalid."
                )
            _clean_text(recommendation.get("reason"),"a recommendation reason",4_000)
            if recommendation.get("decision")=="include":
                recommended_included.add(stable_id)
        if schema_version>=3:
            permission_evidence=_validate_permission_evidence(
                candidate.get("permission_evidence")
            )
            if candidate.get("decision")=="include":
                source_state=candidate.get("source_state")
                source_file=(
                    source_state.get("file") if isinstance(source_state,dict)
                    else None
                )
                source_sha256=(
                    str(source_file.get("sha256") or "")
                    if isinstance(source_file,dict) else ""
                )
                unavailable=(
                    candidate_source.get("kind")=="unavailable"
                    and source_state is None and not source_sha256
                )
                if not unavailable and not re.fullmatch(
                    r"[a-f0-9]{64}",source_sha256
                ):
                    raise TrainingPlanDraftError(
                        "An included draft candidate has no valid source-state fingerprint."
                    )
                included_permission_scope.append({
                    "stable_id":stable_id,
                    "source_sha256":source_sha256,
                    "permission_evidence":permission_evidence,
                })
    checkpoint()
    if schema_version>=2:
        _validate_dataset_analysis(
            manifest.get("dataset_analysis"),stable_ids=stable_ids,
            recommended_included=recommended_included,schema_version=schema_version,
        )
        if schema_version>=6:
            _validate_video_split_components(manifest["dataset_analysis"],candidates,
                                             recommended_included)
    checkpoint()
    if schema_version>=3:
        _validate_permission_review(
            manifest.get("permission_review"),
            included_permission_scope=included_permission_scope,
        )
    checkpoint()
    if schema_version>=5:
        from .training_duplicate_evidence import TrainingDuplicateEvidence
        analysis=manifest.get("dataset_analysis",{})
        if "duplicate_evidence" not in analysis:
            raise TrainingPlanDraftError("A current draft is missing its duplicate-evidence state.")
        if analysis["duplicate_evidence"] is not None:
            try:
                TrainingDuplicateEvidence.from_dict(
                    analysis["duplicate_evidence"],source_hashes={
                        item["stable_id"]:((item.get("source_state") or {}).get("file") or {}).get("sha256","")
                        for item in candidates
                    },
                )
            except (TypeError,ValueError) as exc:
                raise TrainingPlanDraftError("A draft duplicate-evidence snapshot is invalid.") from exc
    if schema_version>=4:
        try:
            configuration=TrainingConfiguration.from_dict(
                manifest.get("training_configuration")
            )
        except (TypeError,ValueError) as exc:
            raise TrainingPlanDraftError(
                f"A draft has an invalid training configuration: {exc}"
            ) from exc
        expected_issues=[
            issue.to_dict() for issue in validate_training_configuration(
                configuration,
                profile_family=str(profile_payload.get("target_model_family") or ""),
                profile_version=str(profile_payload.get("target_model_version") or ""),
            )
        ]
        if manifest.get("training_configuration_issues")!=expected_issues:
            raise TrainingPlanDraftError(
                "A draft training-configuration issue snapshot is inconsistent."
            )
        if type(manifest.get("package_ready")) is not bool:
            raise TrainingPlanDraftError("A draft package-ready state is invalid.")
    checkpoint()
    recorded=manifest.pop("plan_fingerprint",None)
    if recorded!=_stable_hash(manifest):
        raise TrainingPlanDraftError("A draft plan fingerprint does not match.")
    manifest["plan_fingerprint"]=recorded
    checkpoint()
    _assert_source_neutral(manifest,should_cancel=should_cancel)
    checkpoint()
    return manifest


def validate_training_plan_manifest(
    value: Any,*,should_cancel: Callable[[],bool]|None=None,
) -> dict[str,object]:
    """Validate and copy one source-neutral candidate-plan manifest."""
    return _validated_manifest(value,should_cancel=should_cancel)


def _validated_collection_version(value,manifest):
    if value is None:
        return None
    from .collection_training_version import CollectionTrainingVersion
    try:
        version=CollectionTrainingVersion.from_bytes(json.dumps(value,ensure_ascii=False,allow_nan=False).encode("utf-8"))
        clean=version.to_dict()
        if clean["candidate_plan"]!=manifest:
            raise ValueError("Collection draft version differs from its exact saved plan.")
        return json.loads(version.to_bytes())
    except (ValueError,TypeError) as exc:
        raise TrainingPlanDraftError(str(exc)) from exc


@dataclass(frozen=True)
class TrainingPlanDraft:
    draft_id: str
    name: str
    plan_manifest: dict[str,object]
    created_at: str
    updated_at: str
    collection_version: dict[str,object] | None = None

    @property
    def fingerprint(self) -> str:
        return self.to_dict()["draft_fingerprint"]

    @property
    def goal(self) -> TrainingGoal:
        value=self.plan_manifest["goal"]
        return TrainingGoal(
            str(value.get("text") or ""),
            kind=str(value.get("kind") or "concept"),
            target_model_family=str(value.get("target_model_family") or ""),
            target_model_version=str(value.get("target_model_version") or ""),
            base_model_id=str(value.get("base_model_id") or ""),
        )

    @property
    def profile(self) -> ExportProfile:
        return ExportProfile.from_dict(dict(self.plan_manifest["profile"]))

    @property
    def candidate_count(self) -> int:
        return len(self.plan_manifest["selection_scope"]["frozen_selection"])

    def overrides(self) -> dict[str,CandidateOverride]:
        return {
            str(candidate["stable_id"]):CandidateOverride(
                str(candidate["decision"]),
                str(candidate["decision_reason"]),
                str(candidate["role"]),
            )
            for candidate in self.plan_manifest["candidates"]
            if candidate.get("user_override")
        }

    def permission_disposition(self) -> TrainingPermissionDisposition:
        value=self.plan_manifest.get("permission_review")
        if not isinstance(value,dict):
            return TrainingPermissionDisposition()
        return TrainingPermissionDisposition(
            str(value.get("status") or "pending"),
            str(value.get("reason") or ""),
            bool(value.get("unknown_evidence_acknowledged")),
            str(value.get("evidence_scope_fingerprint") or ""),
            str(value.get("policy_version") or "collectigen.training-permission-review-v1"),
        )

    def configuration(self) -> TrainingConfiguration | None:
        value=self.plan_manifest.get("training_configuration")
        if not isinstance(value,dict):
            return None
        return TrainingConfiguration.from_dict(value)

    def comparison_changes(
        self,plan: TrainingCandidatePlan,
    ) -> tuple[str,...]:
        """Name exact saved/current differences after the current source recheck."""
        current=plan.to_manifest_dict()
        changes=[]
        saved_scope=self.plan_manifest["selection_scope"]
        current_scope=current["selection_scope"]
        if saved_scope.get("scope_fingerprint")!=current_scope.get("scope_fingerprint"):
            changes.append("The ordered selection fingerprint changed.")
        if self.plan_manifest.get("profile_provenance")!=current.get(
            "profile_provenance"
        ):
            changes.append("The selected profile provenance changed.")
        if self.plan_manifest.get("dataset_analysis")!=current.get(
            "dataset_analysis"
        ):
            changes.append(
                "The split recommendation or bounded dataset evidence changed."
            )
        if self.plan_manifest.get("permission_review")!=current.get(
            "permission_review"
        ):
            changes.append(
                "The source and permission evidence scope or disposition changed."
            )
        if self.plan_manifest.get("training_configuration")!=current.get(
            "training_configuration"
        ):
            changes.append("The target, package, or advanced training settings changed.")
        saved_by_id={
            str(item.get("stable_id")):item
            for item in self.plan_manifest["candidates"]
        }
        current_by_id={
            str(item.get("stable_id")):item for item in current["candidates"]
        }
        for stable_id in sorted(set(saved_by_id)|set(current_by_id)):
            before=saved_by_id.get(stable_id); after=current_by_id.get(stable_id)
            label=stable_id
            if before is None or after is None:
                changes.append(f"{label} is not present in both plan snapshots.")
                continue
            if before.get("source_state")!=after.get("source_state"):
                changes.append(f"{label} has different current source-file evidence.")
            if before.get("knowledge_fingerprint")!=after.get("knowledge_fingerprint"):
                changes.append(f"{label} has different current Image Knowledge.")
            if before.get("derived_frame")!=after.get("derived_frame"):
                changes.append(f"{label} has different current video-frame lineage.")
            if before.get("caption_sha256")!=after.get("caption_sha256"):
                changes.append(f"{label} has a different current caption projection.")
        return tuple(changes)

    def resolve_selection(self,dataset) -> TrainingDraftResolution:
        paths=[]; identities={}; unresolved=[]
        frozen=self.plan_manifest["selection_scope"]["frozen_selection"]
        for index,source in enumerate(frozen,1):
            if source["kind"]!="library_image":
                unresolved.append(
                    f"Candidate {index:,} was not saved with a portable library identity."
                )
                continue
            identity=LibraryImageIdentity(
                str(source["library_id"]),str(source["relative_image_path"]),
            )
            try:
                path=Path(dataset.path_for_identity(identity))
            except (AttributeError,KeyError,TypeError,ValueError,OSError):
                unresolved.append(
                    f"{identity.library_id}:{identity.relative_path} is not available in the current library configuration."
                )
                continue
            try:
                if not path.is_file():
                    raise FileNotFoundError(path.name)
                with path.open("rb") as handle:
                    handle.read(1)
            except OSError:
                unresolved.append(
                    f"{identity.library_id}:{identity.relative_path} is not a readable image file in the current library."
                )
                continue
            paths.append(path); identities[path]=identity
        return TrainingDraftResolution(tuple(paths),identities,tuple(unresolved))

    def to_dict(self) -> dict[str,object]:
        payload={
            "draft_id":self.draft_id,
            "name":self.name,
            "plan":deepcopy(self.plan_manifest),
            "created_at":self.created_at,
            "updated_at":self.updated_at,
        }
        if self.collection_version is not None:
            payload["collection_version"]=deepcopy(self.collection_version)
        return {**payload,"draft_fingerprint":_stable_hash(payload)}

    @classmethod
    def from_dict(cls,value: Any) -> "TrainingPlanDraft":
        if not isinstance(value,dict):
            raise TrainingPlanDraftError("A saved draft is not an object.")
        draft_id=str(value.get("draft_id") or "")
        if not _DRAFT_ID.fullmatch(draft_id):
            raise TrainingPlanDraftError("A saved draft has an invalid stable ID.")
        name=_clean_text(value.get("name"),"a draft name",100)
        created_at=_clean_text(value.get("created_at"),"a creation time",80)
        updated_at=_clean_text(value.get("updated_at"),"an update time",80)
        plan=_validated_manifest(value.get("plan"))
        binding=_validated_collection_version(value.get("collection_version"),plan)
        draft=cls(draft_id,name,plan,created_at,updated_at,binding)
        if value.get("draft_fingerprint")!=draft.fingerprint:
            raise TrainingPlanDraftError("A saved draft fingerprint does not match.")
        return draft


@dataclass(frozen=True)
class TrainingPlanDraftSaveResult:
    draft: TrainingPlanDraft
    changed: bool
    backup_directory: Path | None = None


class TrainingPlanDraftStore:
    """Durable source-neutral coordination state for unfinished training plans."""

    def __init__(self,path: str | Path) -> None:
        self.path=Path(path)
        self._lock=_draft_lock(self.path)
        self.load()

    @contextmanager
    def locked(self):
        with self._lock:
            yield

    def require_current(self):
        self._ensure_writable()

    @property
    def writable(self) -> bool:
        return (
            not self.load_error and not self._invalid_records
            and self._schema_version<=TRAINING_PLAN_DRAFT_STORE_SCHEMA_VERSION
        )

    def load(self) -> None:
        with self._lock:
            self._load_unlocked()

    def _load_unlocked(self) -> None:
        self.drafts_by_id={}; self.issues=[]; self.load_error=""
        self._invalid_records=[]; self._payload_extras={}
        self._schema_version=TRAINING_PLAN_DRAFT_STORE_SCHEMA_VERSION
        raw=None
        try:
            raw=self.path.read_bytes() if self.path.exists() else None
            payload=json.loads(raw.decode("utf-8")) if raw is not None else {}
        except (OSError,UnicodeError,json.JSONDecodeError) as exc:
            self._loaded_bytes=raw
            self.load_error=f"Saved training drafts could not be read: {exc}"
            self.issues.append(TrainingPlanDraftIssue("file",self.load_error))
            return
        self._loaded_bytes=raw
        if not isinstance(payload,dict):
            self.load_error="Saved training drafts must use a JSON object."
            self.issues.append(TrainingPlanDraftIssue("file",self.load_error))
            return
        try:
            self._schema_version=max(1,int(payload.get(
                "schema_version",TRAINING_PLAN_DRAFT_STORE_SCHEMA_VERSION,
            )))
        except (TypeError,ValueError):
            self.load_error="Saved training drafts have an invalid schema version."
            self.issues.append(TrainingPlanDraftIssue("schema_version",self.load_error))
            return
        self._payload_extras={
            key:deepcopy(value) for key,value in payload.items()
            if key not in {"schema_version","drafts"}
        }
        if self._schema_version>TRAINING_PLAN_DRAFT_STORE_SCHEMA_VERSION:
            self.issues.append(TrainingPlanDraftIssue(
                "schema_version",
                "Saved training drafts were written by a newer CollectiGen version.",
            ))
        records=payload.get("drafts",[])
        if not isinstance(records,list):
            self.load_error="Saved training drafts must contain a draft list."
            self.issues.append(TrainingPlanDraftIssue("drafts",self.load_error))
            return
        for index,record in enumerate(records):
            try:
                draft=TrainingPlanDraft.from_dict(record)
                if draft.draft_id in self.drafts_by_id:
                    raise TrainingPlanDraftError("A saved draft ID is duplicated.")
            except TrainingPlanDraftError as exc:
                self._invalid_records.append(deepcopy(record))
                self.issues.append(TrainingPlanDraftIssue(f"drafts[{index}]",str(exc)))
                continue
            self.drafts_by_id[draft.draft_id]=draft

    def all_drafts(self) -> tuple[TrainingPlanDraft,...]:
        with self._lock:
            return tuple(sorted(
                self.drafts_by_id.values(),
                key=lambda draft:(draft.updated_at,draft.draft_id),reverse=True,
            ))

    def save_plan(
        self,
        plan: TrainingCandidatePlan,
        name: str,
        *,
        draft_id: str="",
        expected_fingerprint: str | None=None,
        collection_version: dict | None=None,
    ) -> TrainingPlanDraftSaveResult:
        with self._lock:
            return self._save_plan_unlocked(
                plan,name,draft_id=draft_id,
                expected_fingerprint=expected_fingerprint,collection_version=collection_version,
            )

    def save_manifest(self,manifest: dict[str,object],name: str, *, companion: dict | None=None) -> TrainingPlanDraftSaveResult:
        """Create a new draft from an exact validated, source-neutral frozen plan.

        The caller owns provenance verification; this never reuses a draft ID or
        treats frozen source evidence as current launch authority.
        """
        with self._lock:
            self._ensure_writable()
            clean_name=_clean_text(name,"a draft name",100)
            frozen=_validated_manifest(manifest)
            now=datetime.now(timezone.utc).isoformat(timespec="seconds")
            draft=TrainingPlanDraft(f"draft.{uuid4().hex}",clean_name,frozen,now,now)
            next_drafts=dict(self.drafts_by_id)
            next_drafts[draft.draft_id]=draft
            if companion is not None:
                from .collection_training_profiles import validate_profile_companion
                try:
                    companion=validate_profile_companion(companion,candidate_plan_fingerprint=frozen["plan_fingerprint"])
                except ValueError as exc:
                    raise TrainingPlanDraftError(str(exc)) from exc
                slot="collection_profile_companions_candidate_v1"
                rows=deepcopy(self._payload_extras.get(slot,{}))
                if not isinstance(rows,dict):
                    raise TrainingPlanDraftError("Companion namespace is malformed.")
                rows[draft.draft_id]=deepcopy(companion)
                self._payload_extras[slot]=rows
            try:
                backup=self._write(next_drafts)
            except BaseException:
                self.load()
                raise
            return TrainingPlanDraftSaveResult(self.drafts_by_id[draft.draft_id],True,backup)

    def _save_plan_unlocked(
        self,
        plan: TrainingCandidatePlan,
        name: str,
        *,
        draft_id: str="",
        expected_fingerprint: str | None=None,
        collection_version: dict | None=None,
    ) -> TrainingPlanDraftSaveResult:
        self._ensure_writable()
        if plan.cancelled or len(plan.candidates)!=len(plan.frozen_scope):
            raise TrainingPlanDraftError(
                "Finish inspecting the exact selection before saving this draft."
            )
        clean_name=_clean_text(name,"a draft name",100)
        manifest=_validated_manifest(plan.to_manifest_dict())
        binding=_validated_collection_version(collection_version,manifest)
        now=datetime.now(timezone.utc).isoformat(timespec="seconds")
        current=self.drafts_by_id.get(draft_id) if draft_id else None
        if current is not None:
            if current.collection_version is not None and binding is None:
                raise TrainingPlanDraftError("A bound Collection draft requires its reviewed Collection version; it cannot be silently unbound.")
            if expected_fingerprint!=current.fingerprint:
                raise TrainingPlanDraftError(
                    "This draft changed after editing began. Reload it and retry."
                )
            draft=TrainingPlanDraft(
                current.draft_id,clean_name,manifest,current.created_at,now,binding,
            )
            if (
                current.name==draft.name
                and current.plan_manifest==draft.plan_manifest
                and current.collection_version==draft.collection_version
            ):
                return TrainingPlanDraftSaveResult(current,False)
        else:
            if draft_id or expected_fingerprint is not None:
                raise TrainingPlanDraftError(
                    "This draft no longer exists. Reload the draft list."
                )
            draft=TrainingPlanDraft(
                f"draft.{uuid4().hex}",clean_name,manifest,now,now,binding,
            )
        next_drafts=dict(self.drafts_by_id); next_drafts[draft.draft_id]=draft
        backup=self._write(next_drafts)
        return TrainingPlanDraftSaveResult(
            self.drafts_by_id[draft.draft_id],True,backup,
        )

    def _ensure_writable(self) -> None:
        if self.load_error:
            raise TrainingPlanDraftError(
                f"{self.load_error} The existing file was left unchanged."
            )
        if self._schema_version>TRAINING_PLAN_DRAFT_STORE_SCHEMA_VERSION:
            raise TrainingPlanDraftError(
                "Saved training drafts use a newer format and were left unchanged."
            )
        if self._invalid_records:
            raise TrainingPlanDraftError(
                "Saved training drafts contain invalid records and were left unchanged."
            )
        try:
            current=self.path.read_bytes() if self.path.exists() else None
        except OSError as exc:
            raise TrainingPlanDraftError(
                f"Saved training drafts could not be checked for changes: {exc}"
            ) from exc
        if current!=self._loaded_bytes:
            raise TrainingPlanDraftError(
                "Saved training drafts changed after this view opened. Reload and retry."
            )

    def _payload(self,drafts: dict[str,TrainingPlanDraft]) -> dict[str,object]:
        payload=deepcopy(self._payload_extras)
        payload.update({
            "schema_version":TRAINING_PLAN_DRAFT_STORE_SCHEMA_VERSION,
            "drafts":[draft.to_dict() for draft in sorted(
                drafts.values(),key=lambda item:(item.name.casefold(),item.draft_id),
            )],
        })
        payload["drafts"].extend(deepcopy(self._invalid_records))
        if "collection_profile_companions_candidate_v1" in payload:
            from .collection_training_version import _structural_bounds
            _structural_bounds(payload)
            if len((json.dumps(payload,indent=2,ensure_ascii=False)+"\n").encode("utf-8"))>16*1024*1024:
                raise TrainingPlanDraftError("Companion snapshot exceeds supported byte limit.")
        return payload

    @staticmethod
    def _atomic_bytes(path: Path,payload: bytes) -> None:
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(payload); handle.flush(); os.fsync(handle.fileno())
            temporary.replace(path)
            if path.read_bytes()!=payload:
                raise TrainingPlanDraftError(
                    "Saved training drafts failed byte-level read-back verification."
                )
        finally:
            if temporary.exists():
                temporary.unlink()

    @classmethod
    def _atomic_json(cls,path: Path,payload: dict[str,object]) -> None:
        cls._atomic_bytes(
            path,(json.dumps(payload,indent=2,ensure_ascii=False)+"\n").encode("utf-8"),
        )

    def _write(self,drafts: dict[str,TrainingPlanDraft]) -> Path:
        self._ensure_writable()
        previous=self._loaded_bytes
        expected=self._payload(drafts)
        stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup=self.path.parent/"backups"/"training-plan-drafts"/(
            f"{stamp}_{uuid4().hex[:8]}"
        )
        backup.mkdir(parents=True,exist_ok=False)
        before_name=""
        if previous is not None:
            before_name=self.path.name
            shutil.copy2(self.path,backup/before_name)
        recovery=backup/"recovery.json"
        recovery_payload={
            "schema_version":1,
            "operation":"training_plan_draft_store_write",
            "target_name":self.path.name,
            "before":before_name,
            "before_sha256":hashlib.sha256(previous or b"").hexdigest(),
            "after_sha256":"",
            "status":"prepared",
        }
        self._atomic_json(recovery,recovery_payload)
        try:
            self._atomic_json(self.path,expected)
            actual=json.loads(self.path.read_text(encoding="utf-8"))
            if actual!=expected:
                raise TrainingPlanDraftError(
                    "Saved training drafts failed read-back verification."
                )
            after=self.path.read_bytes()
            recovery_payload["after_sha256"]=hashlib.sha256(after).hexdigest()
            recovery_payload["status"]="complete"
            self._atomic_json(recovery,recovery_payload)
        except BaseException:
            if previous is None:
                if self.path.exists():
                    self.path.unlink()
            else:
                self._atomic_bytes(self.path,previous)
            recovery_payload["status"]="rolled_back"
            self._atomic_json(recovery,recovery_payload)
            self.load()
            raise
        self.load()
        return backup
