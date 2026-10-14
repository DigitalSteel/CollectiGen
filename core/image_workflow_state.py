from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict,dataclass,field
import hashlib
import json
import re
from pathlib import Path

from .automation_repository import active_automation_engine
from .character_database import CharacterDatabase
from .image_record import ImageRecord
from .image_fact_decisions import ImageFindingDecisionService
from .image_knowledge_completeness import ImageKnowledgeCompletenessService
from .settings import normalize_plain_language_exclusion
from .training_readiness import TrainingReadinessService


def concise_image_status_detail(detail: str) -> str:
    """Present old cached status prose without rescanning or changing readiness."""
    detail=str(detail or "").strip()
    # Only normalize the service's old generated prefix; arbitrary issue text
    # (including quoted numbers or warnings) must remain intact.
    match=re.match(r"^Image Knowledge completeness is \d+(?:\.\d+)?/100[.;]\s*",detail)
    if not match:
        return detail
    detail=detail[match.end():]
    for suffix in (
        "Human review is not recorded; destination policy warnings and privacy confirmations remain separate.",
        "Human review is recorded.",
    ):
        if detail.endswith(suffix):
            detail=detail[:-len(suffix)].strip()
    if detail=="no blocking issue was found.":
        return "Image Knowledge complete."
    return detail


def _knowledge_workflow_digest(knowledge,checkpoint=None) -> str:
    """Exclude observation counters and independently cached content ratings."""
    raw=getattr(knowledge,"_loaded_bytes",None)
    if not isinstance(raw,bytes):
        return ""
    cached=getattr(knowledge,"_workflow_digest_cache",None)
    if isinstance(cached,tuple) and len(cached)==2 and cached[0] is raw:
        return cached[1]
    try:
        payload=json.loads(raw)
        if checkpoint is not None:checkpoint()
        if isinstance(payload,dict) and isinstance(payload.get("tags"),list):
            for tag in payload["tags"]:
                if isinstance(tag,dict):
                    for key in ("seen_count","first_seen","last_seen","content_sensitivity"):
                        tag.pop(key,None)
        encoded=json.dumps(payload,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8")
    except (ValueError,UnicodeError):
        encoded=raw
    digest=hashlib.sha256(encoded).hexdigest()
    knowledge._workflow_digest_cache=(raw,digest)
    return digest


@dataclass(frozen=True)
class ImageWorkflowScoreComponent:
    key: str
    score: float
    maximum: float


@dataclass(frozen=True)
class ImageWorkflowScoreIssue:
    code: str
    label: str
    deduction: float
    component: str
    suggested_action: str


@dataclass(frozen=True)
class ImageWorkflowState:
    key: str
    color: str
    label: str
    detail: str
    score: int
    ready: bool
    issue_codes: tuple[str, ...] = ()
    last_sdxl_ai_scan: str = ""
    last_plain_language_ai_scan: str = ""
    score_components: tuple[ImageWorkflowScoreComponent, ...] = ()
    score_issues: tuple[ImageWorkflowScoreIssue, ...] = ()
    next_image_knowledge_action: str = ""
    training_export_health_state: str = ""
    training_export_health_label: str = ""
    training_export_health_detail: str = ""
    training_export_profile_id: str = ""
    training_export_issue_codes: tuple[str, ...] = ()
    metadata_backup_health_state: str = ""
    metadata_backup_health_label: str = ""
    metadata_backup_health_detail: str = ""
    has_saved_tags: bool | None = None
    has_saved_descriptions: bool | None = None
    has_pending_tags: bool | None = None
    has_pending_descriptions: bool | None = None
    rating_tags: tuple[str,...] | None = None
    workflow_dependencies: dict | None = field(default=None,compare=False)

    CACHE_VERSION = 3

    def to_cache_dict(self) -> dict[str,object]:
        """Serialize this rebuildable projection without changing authority."""
        payload={"version":self.CACHE_VERSION,**asdict(self)}
        if self.rating_tags is None:payload.pop("rating_tags",None)
        if self.workflow_dependencies is None:payload.pop("workflow_dependencies",None)
        return payload

    @classmethod
    def from_cache_dict(cls,payload: Mapping) -> "ImageWorkflowState"|None:
        """Decode one versioned projection, failing closed on malformed rows."""
        try:
            version=int(payload.get("version") or 0)
            # Old aggregate findings can be false positives for recognized
            # titles and cannot identify the affected half. Reassess just
            # these disposable rows through the ordinary bounded reader.
            if "typed_boundary_review" in payload.get("issue_codes",()):
                return None
            if version==2 and cls.CACHE_VERSION==3:
                # V3 changes readiness for missing useful Descriptions. V2
                # already stores this exact finding and confirmed-content flags;
                # adapt the disposable result without reopening image authority.
                if not isinstance(payload.get("issue_codes"),(list,tuple)) or any(
                    not isinstance(payload.get(key),bool) for key in (
                        "has_saved_tags","has_saved_descriptions",
                        "has_pending_tags","has_pending_descriptions",
                    )
                ):
                    return None
                payload=dict(payload)
                missing="missing_description" in payload["issue_codes"]
                if payload.get("ready") is True and (
                    not payload["has_saved_tags"] or "missing_tags" in payload["issue_codes"]
                    or (not payload["has_saved_descriptions"] and not missing)
                ):
                    return None
                if missing and (payload.get("ready") is True or payload.get("key")=="incomplete"):
                    payload.update(
                        key="incomplete",color="orange",label="Description needed",
                        detail="Image Knowledge is partially prepared: a useful confirmed Description is missing.",
                        ready=False,
                        next_image_knowledge_action="Add a useful confirmed Description of the visible subjects, actions, or relationships.",
                    )
                payload["version"]=cls.CACHE_VERSION
            elif version!=cls.CACHE_VERSION:
                return None
            # Older projections gave ordinary missing Tags the same red state
            # as damaged data. Adapt only the simple, fully known content gap
            # in place; other findings need a fresh assessment so a real
            # conflict or pending decision cannot be hidden by cache reuse.
            if (payload.get("key")=="problem"
                    and payload.get("detail")=="No confirmed-present SDXL tags"
                    and "missing_tags" in payload.get("issue_codes",())):
                codes=payload.get("issue_codes")
                simple=(isinstance(codes,(list,tuple))
                        and set(codes) <= {"missing_tags","missing_description",
                                           "thin_description","missing_rating"}
                        and payload.get("has_saved_tags") is False
                        and payload.get("has_pending_tags") is False
                        and payload.get("has_pending_descriptions") is False
                        and isinstance(payload.get("has_saved_descriptions"),bool))
                if not simple:
                    return None
                missing_description="missing_description" in codes
                payload=dict(payload)
                payload.update(
                    key="incomplete",color="orange",
                    label="Tags and Description needed" if missing_description else "Tags needed",
                    detail="No confirmed Tags or useful Description yet." if missing_description
                    else "No confirmed Tags yet.",
                    next_image_knowledge_action=(
                        "Review the image, then add or accept Tags it supports; add a useful Description."
                        if missing_description else
                        "Review the image, then add or accept Tags it supports."
                    ),
                )
            ready=payload["ready"]
            raw_components=payload.get("score_components",())
            raw_issues=payload.get("score_issues",())
            if (
                not isinstance(ready,bool)
                or not isinstance(raw_components,(list,tuple))
                or not all(isinstance(item,Mapping) for item in raw_components)
                or not isinstance(raw_issues,(list,tuple))
                or not all(isinstance(item,Mapping) for item in raw_issues)
            ):
                return None
            def strings(key):
                values=payload.get(key,())
                if not isinstance(values,(list,tuple)):
                    raise TypeError(key)
                return tuple(str(value) for value in values)
            def optional_bool(key):
                value=payload.get(key)
                if value is not None and not isinstance(value,bool):
                    raise TypeError(key)
                return value
            components=tuple(
                ImageWorkflowScoreComponent(
                    str(item["key"]),float(item["score"]),float(item["maximum"]),
                )
                for item in raw_components
            )
            issues=tuple(
                ImageWorkflowScoreIssue(
                    str(item["code"]),str(item["label"]),
                    float(item["deduction"]),str(item["component"]),
                    str(item["suggested_action"]),
                )
                for item in raw_issues
            )
            return cls(
                key=str(payload["key"]),color=str(payload["color"]),
                label=str(payload["label"]),detail=str(payload["detail"]),
                score=int(payload["score"]),ready=ready,
                issue_codes=strings("issue_codes"),
                last_sdxl_ai_scan=str(payload.get("last_sdxl_ai_scan") or ""),
                last_plain_language_ai_scan=str(payload.get("last_plain_language_ai_scan") or ""),
                score_components=components,score_issues=issues,
                next_image_knowledge_action=str(payload.get("next_image_knowledge_action") or ""),
                training_export_health_state=str(payload.get("training_export_health_state") or ""),
                training_export_health_label=str(payload.get("training_export_health_label") or ""),
                training_export_health_detail=str(payload.get("training_export_health_detail") or ""),
                training_export_profile_id=str(payload.get("training_export_profile_id") or ""),
                training_export_issue_codes=strings("training_export_issue_codes"),
                metadata_backup_health_state=str(payload.get("metadata_backup_health_state") or ""),
                metadata_backup_health_label=str(payload.get("metadata_backup_health_label") or ""),
                metadata_backup_health_detail=str(payload.get("metadata_backup_health_detail") or ""),
                has_saved_tags=optional_bool("has_saved_tags"),
                has_saved_descriptions=optional_bool("has_saved_descriptions"),
                has_pending_tags=optional_bool("has_pending_tags"),
                has_pending_descriptions=optional_bool("has_pending_descriptions"),
                rating_tags=strings("rating_tags") if payload.get("rating_tags") is not None else None,
                workflow_dependencies=payload.get("workflow_dependencies") if isinstance(payload.get("workflow_dependencies"),dict) else None,
            )
        except (KeyError,TypeError,ValueError,OverflowError):
            return None


def sensitivity_cached_paths(groups,inputs):
    """Find possibly affected cached rows without storage or a library rescan.

    Used only when the indexed dependency query failed or was cancelled. Missing
    inputs remain unknown; callers hold their displayed status until checked.
    """
    from .tag_collection import normalize_tag
    names={normalize_tag(value) for group in groups for value in group}
    affected=set()
    for path,tags in inputs.items():
        if (not isinstance(tags,(tuple,list)) or any(not isinstance(tag,str) for tag in tags)
                or any(normalize_tag(tag) in names for tag in tags)):
            affected.add(path)
    return affected


def image_preparation_stage(state: ImageWorkflowState | None, *, has_record: bool) -> str:
    """Project preparation progress without treating an empty sidecar as work.

    Unknown content retains its partial/checking treatment. Only an assessed
    absence of saved and pending content can return to Unprepared; diagnostics
    beyond missing content retain their attention route.
    """
    if not has_record:
        return "unprepared"
    if state is not None and bool(getattr(state, "ready", False)):
        return "complete"
    content = (
        getattr(state, name, None) for name in (
            "has_saved_tags", "has_saved_descriptions",
            "has_pending_tags", "has_pending_descriptions",
        )
    )
    empty_gaps = {"missing_tags", "missing_description", "missing_rating"}
    if (
        all(value is False for value in content)
        and not (set(getattr(state, "issue_codes", ())) - empty_gaps)
    ):
        return "unprepared"
    return "partial"


@dataclass(frozen=True)
class ImageKnowledgeCoverage:
    tags: bool | None = None
    descriptions: bool | None = None
    tags_detail: str = "Tags: coverage has not been assessed yet."
    descriptions_detail: str = "Descriptions: coverage has not been assessed yet."
    review_pending: bool = False
    review_detail: str = ""
    problem: bool = False
    tags_stored: bool | None = None
    descriptions_stored: bool | None = None
    tags_pending: bool | None = None
    descriptions_pending: bool | None = None
    tags_problem: bool = False
    descriptions_problem: bool = False


def image_knowledge_coverage(state: ImageWorkflowState | None) -> ImageKnowledgeCoverage:
    """Project saved coverage from the existing assessment, never AI run dates."""
    if state is None:
        return ImageKnowledgeCoverage()
    codes=set(getattr(state,"issue_codes",()))
    review_codes={
        "unresolved_evidence","typed_boundary_review","tag_contradiction",
        "typed_tag_boundary_review","typed_description_boundary_review",
        "canonical_tag_conflict","missing_rating","incomplete_style_relationship",
        "unknown_outfit",
    }
    review_pending=bool(codes & review_codes or getattr(state,"key","") in {"ai","review"})
    review_detail=("Review pending: "+str(getattr(state,"detail","") or "Image findings need a decision.")) if review_pending else ""
    status_kwargs=dict(
        review_pending=review_pending,review_detail=review_detail,
        problem=getattr(state,"color","")=="red" or getattr(state,"key","")=="problem",
        tags_stored=getattr(state,"has_saved_tags",None),
        descriptions_stored=getattr(state,"has_saved_descriptions",None),
        tags_pending=True if "typed_tag_boundary_review" in codes else getattr(state,"has_pending_tags",None),
        descriptions_pending=True if "typed_description_boundary_review" in codes else getattr(state,"has_pending_descriptions",None),
        tags_problem=bool(codes & {"tag_contradiction", "canonical_tag_conflict"}),
    )
    if not getattr(state,"score_components",()):
        if getattr(state,"key","")=="incomplete":
            return ImageKnowledgeCoverage(
                False,False,"Tags: no assessed saved Image Knowledge.",
                "Descriptions: no assessed saved Image Knowledge.",
                **status_kwargs,
            )
        return ImageKnowledgeCoverage(**status_kwargs)
    shared={"missing_record","typed_boundary_review"}
    tag_codes=shared | {
        "missing_tags","thin_tag_coverage","low_tag_diversity",
        "tag_contradiction","canonical_tag_conflict","typed_tag_boundary_review",
    }
    description_codes=shared | {"missing_description","thin_description","typed_description_boundary_review"}
    issues=getattr(state,"score_issues",())

    def part(label,relevant):
        gaps=codes & relevant
        if not gaps:
            return True,f"{label}: saved coverage looks complete (contextual estimate)."
        reasons=[item.label for item in issues if item.code in gaps]
        return False,f"{label}: "+("; ".join(dict.fromkeys(reasons)) or "coverage needs attention.")

    tags,tags_detail=part("Tags",tag_codes)
    descriptions,descriptions_detail=part("Descriptions",description_codes)
    return ImageKnowledgeCoverage(tags,descriptions,tags_detail,descriptions_detail,**status_kwargs)


class ImageWorkflowStateService:
    """One read-only state used by galleries, filters, and move decisions."""

    def __init__(
        self, knowledge=None, characters: CharacterDatabase | None = None,
        *, ignored_plain_language=(), categories=None, inspect_sources: bool=False,
    ) -> None:
        self.knowledge = knowledge
        self.characters = characters
        self.categories = categories
        self.inspect_sources = bool(inspect_sources)
        self._source_inspector = None
        self.ignored_plain_language = {
            normalize_plain_language_exclusion(value)
            for value in ignored_plain_language
            if normalize_plain_language_exclusion(value)
        }

    def semantic_validator(self):
        from .workflow_semantic_proof import WorkflowSemanticProof,active_automation_engine
        engine=active_automation_engine(self.knowledge)
        revision=(id(engine),getattr(self.knowledge,"_relationship_signature_revision",0),
            id(getattr(self.characters,"outfits",None)),frozenset(self.ignored_plain_language))
        if getattr(self,"_semantic_proof",None) is None or getattr(self,"_semantic_proof_revision",None)!=revision:
            self._semantic_proof_revision=revision
            self._semantic_proof=WorkflowSemanticProof(self.knowledge,self.characters,self.categories,
                self.ignored_plain_language,inspect_sources=self.inspect_sources,engine=engine)
        proof_service=self._semantic_proof
        return lambda payload:(proof_service.matches(payload.get("workflow_dependencies"))
                               and self.cached_rating_matches(payload))

    def cached_rating_matches(self,payload,confirmed_tags=None) -> bool:
        """Check only rating conflicts from saved confirmed inputs, never image IO."""
        from types import SimpleNamespace
        from .content_sensitivity import rating_conflicts
        tags=payload.get("rating_tags",confirmed_tags)
        if tags is None:
            # Unknown inputs cannot certify readiness across a rating change.
            # Legacy Ingest callers recover saved JSON inputs in their worker.
            return False
        if not isinstance(tags,(list,tuple)) or any(not isinstance(tag,str) for tag in tags):
            return False
        current="; ".join(rating_conflicts(
            SimpleNamespace(all_structured_tags=lambda:tags),self.knowledge,self.categories,
        )) if self.knowledge is not None else ""
        previous=[str(issue.get("label") or "") for issue in payload.get("score_issues",())
                  if isinstance(issue,Mapping) and issue.get("code")=="rating_conflict"]
        return previous==([current] if current else [])

    def compatible_context_fingerprints(self) -> tuple[str,...]:
        """Recognize old category-rating contexts and the guarded V2 adapter."""
        current=self.context_fingerprint()
        candidates=[self.context_fingerprint(legacy_category_sensitivity=True)]
        if ImageWorkflowState.CACHE_VERSION==3:
            candidates.extend((self.context_fingerprint(cache_version=2),
                               self.context_fingerprint(cache_version=2,legacy_category_sensitivity=True)))
        candidates.extend(getattr(self,"_rating_compatible_contexts",{}).get(current,()))
        return tuple(dict.fromkeys(value for value in candidates if value!=current))

    def context_fingerprint(self,*,cache_version: int|None=None,legacy_category_sensitivity: bool=False,checkpoint=None) -> str:
        """Identify every shared input that can change a readiness projection."""
        from .image_authority_initialization import (
            ImageAuthorityInitializationService,
        )

        knowledge_digest=_knowledge_workflow_digest(self.knowledge,checkpoint)
        if checkpoint is not None:checkpoint()
        categories={
            str(key):record.to_dict()
            for key,record in sorted(
                getattr(self.categories,"categories",{}).items(),
                key=lambda item:str(item[0]),
            )
        }
        if not legacy_category_sensitivity:
            for category in categories.values():
                category.pop("sensitivity_rating",None)
                category.pop("sensitivity_policy",None)
        character_files=[]
        for path,value in sorted(
            getattr(self.characters,"_loaded_files",{}).items(),
            key=lambda item:str(item[0]),
        ):
            character_files.append((
                Path(path).name,
                hashlib.sha256(value).hexdigest()
                if isinstance(value,bytes) else "missing",
            ))
        # CharacterDatabase also hydrates source/outfit identities from shipped
        # relationships. Reuse that loaded authority, including user overrides;
        # its default layer is absent from the four user-file snapshots above.
        character_relationships={
            record.id:record.to_dict()
            for record in (
                *getattr(self.characters,"_default_relationships",()),
                *getattr(self.characters,"_user_relationships",()),
            )
        }
        if checkpoint is not None:checkpoint()
        rules=ImageAuthorityInitializationService(
            knowledge=self.knowledge,gather_source_evidence=False,
        ).current_rules_fingerprint()
        if checkpoint is not None:checkpoint()
        payload={
            "cache_version":ImageWorkflowState.CACHE_VERSION if cache_version is None else cache_version,
            "rules":rules,"knowledge":knowledge_digest,
            "categories":categories,"characters":character_files,
            "character_relationships":character_relationships,
            "ignored_plain_language":sorted(self.ignored_plain_language),
        }
        if self.inspect_sources:
            # Distinguish current-source projections from older saved-only rows.
            # Unrelated consumers retain their existing cache context.
            payload["current_source_projection"] = 1
        encoded=json.dumps(
            payload,sort_keys=True,separators=(",",":"),ensure_ascii=False,
            default=str,
        )
        fingerprint=hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        if cache_version is None and not legacy_category_sensitivity:
            if getattr(self,"_semantic_proof_context",None)!=fingerprint:
                self._semantic_proof=None
                self._semantic_proof_context=fingerprint
        if legacy_category_sensitivity and (cache_version is None or cache_version==ImageWorkflowState.CACHE_VERSION):
            modern=self.context_fingerprint(cache_version=cache_version)
            history=getattr(self,"_rating_compatible_contexts",{})
            history.setdefault(modern,set()).add(fingerprint)
            self._rating_compatible_contexts={modern:history[modern]}
        return fingerprint

    def _pending_findings(
        self,record: ImageRecord,*,candidate_findings=None,by_kind=None,
    ) -> tuple[bool, bool]:
        """Use the same actionable observations as the completeness score."""
        counts = ImageKnowledgeCompletenessService(
            knowledge=self.knowledge, ignored_plain_language=self.ignored_plain_language,
        ).unresolved_findings(record, candidate_findings=candidate_findings, by_kind=by_kind)
        return bool(counts[0]), bool(counts[1])

    @staticmethod
    def _latest_ai_scan_times(record: ImageRecord) -> tuple[str, str]:
        """Return the newest durable completion time for each typed AI scan."""
        latest = {"sdxl_tags": "", "plain_language": ""}
        completed = {"sdxl_tags": False, "plain_language": False}

        def consider(kind, completed_at) -> None:
            kind = str(kind or "")
            completed_at = str(completed_at or "")
            if kind not in latest:
                return
            completed[kind] = True
            if completed_at and completed_at > latest[kind]:
                latest[kind] = completed_at

        runs = record.annotation.source.get("ai_analysis_runs", {})
        for run in runs.values() if isinstance(runs, dict) else ():
            if isinstance(run, dict) and run.get("status") == "complete":
                consider(run.get("kind"), run.get("completed_at"))
        # Older authoritative records may have typed evidence but predate the
        # completed-run index. Its creation time is still durable scan history.
        for evidence in record.annotation.ai_evidence:
            consider(evidence.kind, evidence.created_at)
        return tuple(
            latest[kind] or ("time unavailable" if completed[kind] else "")
            for kind in ("sdxl_tags","plain_language")
        )

    def has_review_findings(
        self,image: str | Path,*,include_deferred: bool = False,
    ) -> bool:
        """Return exact editor-review work without changing any authority."""
        record=ImageRecord(Path(image))
        pending_ai,pending_source=self._pending_findings(record)
        engine=active_automation_engine(self.knowledge)
        conflict_resolver=getattr(engine,"conflicts_for_tags",None)
        conflicts=(
            tuple(conflict_resolver(
                record,record.annotation.all_structured_tags(),
            ))
            if callable(conflict_resolver) else ()
        )
        if pending_ai or pending_source or conflicts:
            return True
        if not include_deferred:
            return False
        decisions=ImageFindingDecisionService(
            record.annotation,knowledge=self.knowledge,
        )
        return bool(
            decisions.deferred_findings()
            or decisions.deferred_tag_identities()
            or decisions.deferred_description_keys()
        )

    def assess(
        self,image: str | Path,*,include_output_health: bool=True,
    ) -> ImageWorkflowState:
        path=Path(image)
        try:
            record = ImageRecord(path)
            derived_result = None
            if self.inspect_sources:
                from .evidence_freshness import current_candidate_findings
                from .source_inspection import ImageEvidenceInspector

                if self._source_inspector is None:
                    self._source_inspector = ImageEvidenceInspector(knowledge=self.knowledge)
                try:
                    report = self._source_inspector.inspect(path)
                except Exception:
                    # Unavailable sources cannot retire saved pending findings.
                    report = None
                if report is not None:
                    derived_result = {"candidate_findings": current_candidate_findings(
                        record.annotation.source.get("candidate_findings", []),
                        report, record=record,
                    )}
                    from .source_inspection import workflow_source_lookup_names
                    derived_result["workflow_source_lookup_names"]=workflow_source_lookup_names(report)
            return self.assess_record(
                record,include_output_health=include_output_health,
                derived_result=derived_result,
            )
        except Exception as exc:
            return ImageWorkflowState(
                "problem", "red", "Image data could not be read",
                f"Image data could not be read: {exc}", 0, False,
                ("unreadable_record",),
            )

    def close(self) -> None:
        """Release the reusable source reader after a bounded status scan."""
        inspector = self._source_inspector
        self._source_inspector = None
        if inspector is not None:
            close = getattr(getattr(inspector, "metadata", None), "close", None)
            if callable(close):
                close()

    def assess_record(
        self,record: ImageRecord,*,include_output_health: bool=True,
        derived_result: Mapping | None=None,
    ) -> ImageWorkflowState:
        """Classify an already loaded record without reopening its sidecar."""
        path=record.image_path
        try:
            last_sdxl_ai_scan, last_plain_language_ai_scan = self._latest_ai_scan_times(record)
            pending_by_kind = None
            readiness = None

            def workflow_state(*args, **kwargs) -> ImageWorkflowState:
                try:
                    self.semantic_validator()
                    proof=self._semantic_proof.build(record,
                        candidate_findings=derived_result.get("candidate_findings") if isinstance(derived_result,Mapping) else None,
                        source_lookup_names=derived_result.get("workflow_source_lookup_names") if isinstance(derived_result,Mapping) else None,
                        include_output_health=include_output_health and readiness is not None,
                        output_preview=readiness.export_preview if readiness is not None else None)
                except (AttributeError,TypeError,ValueError,KeyError):
                    proof=None
                return ImageWorkflowState(
                    *args, **kwargs,
                    last_sdxl_ai_scan=last_sdxl_ai_scan,
                    last_plain_language_ai_scan=last_plain_language_ai_scan,
                    rating_tags=tuple(record.annotation.all_structured_tags()),
                    workflow_dependencies=proof,
                    has_saved_tags=any(str(value).strip() for value in record.annotation.all_structured_tags()),
                    has_saved_descriptions=any(str(value).strip() for value in record.annotation.plain_language),
                    has_pending_tags=bool(pending_by_kind.get("tag")) if pending_by_kind is not None else None,
                    has_pending_descriptions=bool(pending_by_kind.get("description")) if pending_by_kind is not None else None,
                )

            if not record.sidecar_path.is_file():
                if record.caption_path.is_file():
                    return workflow_state(
                        "incomplete", "orange", "Existing caption to import",
                        "Existing caption information has not been prepared and reviewed.", 0, False,
                        ("missing_record",),
                    )
                return workflow_state(
                    "incomplete", "orange", "No prepared image data",
                    "No authoritative Image Knowledge record is saved yet.", 0, False,
                    ("missing_record",),
                )

            current_candidates=(
                derived_result.get("candidate_findings")
                if isinstance(derived_result,Mapping) else None
            )
            pending_by_kind = {}
            pending_ai,pending_source=self._pending_findings(
                record,candidate_findings=current_candidates,by_kind=pending_by_kind,
            )

            derived_state=(
                derived_result.get("automatic_rule_state",{})
                if isinstance(derived_result,Mapping) else {}
            )
            derived_conflict_state=(
                derived_state.get("conflict_rules",{})
                if isinstance(derived_state,Mapping) else {}
            )
            derived_conflicts=(
                derived_conflict_state.get("matches")
                if isinstance(derived_conflict_state,Mapping) else None
            )
            if isinstance(derived_conflicts,list):
                conflict_messages=tuple(dict.fromkeys(
                    str(match.get("message") or "Confirmed Tags conflict.")
                    for match in derived_conflicts if isinstance(match,Mapping)
                ))
            else:
                engine=active_automation_engine(self.knowledge)
                conflict_resolver=getattr(engine,"conflicts_for_tags",None)
                conflicts=(
                    tuple(conflict_resolver(
                        record,record.annotation.all_structured_tags(),
                    ))
                    if callable(conflict_resolver) else ()
                )
                conflict_messages=tuple(dict.fromkeys(
                    str(match.message) for match in conflicts
                ))
            readiness = TrainingReadinessService(
                self.characters, self.knowledge,categories=self.categories,
                ignored_plain_language=self.ignored_plain_language,
            ).assess_record(
                record,include_metadata=False,
                include_output_health=include_output_health,
                candidate_findings=current_candidates,
            )
            knowledge_issues = [
                issue for issue in readiness.issues
                if issue.component != "selected_export"
            ]
            codes = tuple(dict.fromkeys(issue.code for issue in knowledge_issues))
            score_components = tuple(
                ImageWorkflowScoreComponent(component.name, component.score, component.maximum)
                for component in readiness.components
            )
            score_issues = tuple(
                ImageWorkflowScoreIssue(
                    issue.code, issue.label, issue.deduction,
                    issue.component, issue.suggested_action,
                )
                for issue in sorted(
                    knowledge_issues,
                    key=lambda item: (-item.deduction, item.component, item.code),
                )
            )
            if conflict_messages:
                conflict_detail=" ".join(conflict_messages)
                codes=tuple(dict.fromkeys((*codes,"canonical_tag_conflict")))
                score_issues=(*score_issues,ImageWorkflowScoreIssue(
                    "canonical_tag_conflict",conflict_detail,0,
                    "review_resolution",
                    "Review the involved SDXL Tags in the Image Knowledge editor.",
                ))
            image_knowledge_issue = next(
                (
                    issue for issue in score_issues
                    if issue.component in {"authoritative_record", "review_resolution"}
                    and (issue.deduction > 0 or issue.suggested_action)
                ),
                None,
            )
            next_image_knowledge_action = (
                image_knowledge_issue.suggested_action
                if image_knowledge_issue is not None
                else (
                    readiness.completeness.quickest_action
                    if readiness.completeness is not None
                    else readiness.quickest_action
                )
            )
            training_health = readiness.training_export_health
            metadata_health = readiness.metadata_backup_health
            health_kwargs = {
                "training_export_health_state": (
                    training_health.state if training_health is not None else ""
                ),
                "training_export_health_label": (
                    training_health.label if training_health is not None else ""
                ),
                "training_export_health_detail": (
                    training_health.detail if training_health is not None else ""
                ),
                "training_export_profile_id": (
                    training_health.profile_id if training_health is not None else ""
                ),
                "training_export_issue_codes": tuple(
                    issue.code for issue in (
                        training_health.issues if training_health is not None else ()
                    )
                ),
                "metadata_backup_health_state": (
                    metadata_health.state if metadata_health is not None else ""
                ),
                "metadata_backup_health_label": (
                    metadata_health.label if metadata_health is not None else ""
                ),
                "metadata_backup_health_detail": (
                    metadata_health.detail if metadata_health is not None else ""
                ),
            }
            # Missing useful Tags remains a high-severity completeness gap,
            # but it is not corrupt or contradictory image data. Keep true
            # integrity problems red and route the empty-content case below.
            high = next((issue for issue in knowledge_issues
                         if issue.severity == "high" and issue.code != "missing_tags"), None)
            if high:
                return workflow_state(
                    "problem", "red", "Image data problem", high.label,
                    readiness.score, False, codes,
                    score_components=score_components,
                    score_issues=score_issues,
                    next_image_knowledge_action=high.suggested_action,
                    **health_kwargs,
                )
            review_blocking_codes={
                "unresolved_evidence","typed_boundary_review",
                "typed_tag_boundary_review","typed_description_boundary_review",
                "missing_rating","incomplete_style_relationship",
                "unknown_outfit","canonical_tag_conflict",
            }
            review_codes=review_blocking_codes.intersection(codes)
            if "missing_tags" in codes:
                # With no confirmed Tags, the missing rating is part of the
                # same initial preparation gap, not a separate reviewed fact.
                # Other pending/contradictory findings still take priority.
                review_codes.discard("missing_rating")
            needs_fact_review=bool(pending_ai or pending_source or review_codes)
            if needs_fact_review:
                if conflict_messages:
                    next_image_knowledge_action=(
                        "Review the involved SDXL Tags in the Image Knowledge editor."
                    )
                    review_detail=conflict_detail
                elif pending_ai:
                    next_image_knowledge_action=(
                        "Review the pending AI Tags or Descriptions."
                    )
                    review_detail="AI findings still need a review decision."
                elif pending_source:
                    next_image_knowledge_action=(
                        "Review the pending source Tags or Descriptions."
                    )
                    review_detail="Imported source findings still need a review decision."
                else:
                    review_detail=(
                        image_knowledge_issue.label
                        if image_knowledge_issue is not None and image_knowledge_issue.code in {
                            "typed_tag_boundary_review","typed_description_boundary_review",
                        } else next_image_knowledge_action
                    )
                return workflow_state(
                    "ai" if pending_ai else "review", "yellow",
                    "Needs fact review",
                    review_detail,
                    readiness.score, False, codes,
                    score_components=score_components,
                    score_issues=score_issues,
                    next_image_knowledge_action=next_image_knowledge_action,
                    **health_kwargs,
                )
            if "missing_tags" in codes:
                missing_description="missing_description" in codes
                action=(
                    "Review the image, then add or accept Tags it supports; add a useful Description."
                    if missing_description else
                    "Review the image, then add or accept Tags it supports."
                )
                return workflow_state(
                    "incomplete", "orange",
                    "Tags and Description needed" if missing_description else "Tags needed",
                    "No confirmed Tags or useful Description yet." if missing_description
                    else "No confirmed Tags yet.",
                    readiness.score, False, codes,
                    score_components=score_components,
                    score_issues=score_issues,
                    next_image_knowledge_action=action,
                    **health_kwargs,
                )
            if "missing_description" in codes:
                action = "Add a useful confirmed Description of the visible subjects, actions, or relationships."
                return workflow_state(
                    "incomplete", "orange", "Description needed",
                    "Image Knowledge is partially prepared: a useful confirmed Description is missing.",
                    readiness.score, False, codes,
                    score_components=score_components,
                    score_issues=score_issues,
                    next_image_knowledge_action=action,
                    **health_kwargs,
                )
            if readiness.completeness is not None and readiness.completeness.ready:
                return workflow_state(
                    "reviewed", "green", "Image Knowledge complete",
                    "Image Knowledge complete.",
                    readiness.score, True, codes,
                    score_components=score_components,
                    score_issues=score_issues,
                    next_image_knowledge_action=next_image_knowledge_action,
                    **health_kwargs,
                )
            if readiness.score >= 70:
                return workflow_state(
                    "review", "yellow", "Needs fact review",
                    next_image_knowledge_action,
                    readiness.score, False, codes,
                    score_components=score_components,
                    score_issues=score_issues,
                    next_image_knowledge_action=next_image_knowledge_action,
                    **health_kwargs,
                )
            return workflow_state(
                "incomplete", "orange", "Image Knowledge incomplete",
                next_image_knowledge_action,
                readiness.score, False, codes,
                score_components=score_components,
                score_issues=score_issues,
                next_image_knowledge_action=next_image_knowledge_action,
                **health_kwargs,
            )
        except Exception as exc:
            return ImageWorkflowState(
                "problem", "red", "Image data could not be read",
                f"Image data could not be read: {exc}", 0, False,
                ("unreadable_record",),
            )
