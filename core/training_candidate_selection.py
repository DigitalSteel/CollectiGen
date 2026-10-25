from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from typing import Callable, Iterable, Mapping

from PIL import Image

from .character_database import CharacterDatabase
from .export_profile import ExportProfile
from .federated_dataset import LibraryImageIdentity
from .image_record import ImageRecord, annotation_knowledge_fingerprint
from .tag_database import TagDatabase
from .training_dataset_analysis import (
    TrainingDatasetAnalysis,analyze_training_candidates,
    with_duplicate_findings,with_effective_split_findings,
)
from .training_readiness import TrainingReadinessService
from .training_configuration import (
    TrainingConfiguration,package_caption,recommended_training_configuration,
    recontextualize_training_configuration,validate_training_configuration,
)
from .training_source_io import read_stable_training_source
from .video_generation import DerivedFrameReference, FRAME_SCHEMA_ID, SCHEMA_VERSION


ProgressCallback = Callable[[int, int, str], None]
CancelCallback = Callable[[], bool]

PERMISSION_FIELD_LABELS = (
    ("creator", "Creator"),
    ("source", "Source"),
    ("license", "License"),
    ("consent_or_use_restrictions", "Consent / use restrictions"),
    ("real_person_or_privacy", "Real-person / privacy"),
)


def _stable_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_source_snapshot(path: Path) -> tuple[bytes, dict[str, object]]:
    return read_stable_training_source(path)


def _words(value: str) -> tuple[str, ...]:
    return tuple(
        word for word in re.findall(r"[a-z0-9]+", value.casefold()) if len(word) >= 3
    )


def _looks_machine_local(value: str) -> bool:
    raw=str(value or "")
    if (
        re.search(r"(?<![a-zA-Z0-9])[a-zA-Z]:[\\/]",raw)
        or re.search(r"\\\\[^\\\s]+[\\/]",raw)
    ):
        return True
    text=re.sub(r"https?://\S+","",raw,flags=re.IGNORECASE)
    return bool(re.search(r"(?<![a-zA-Z0-9])/(?:[^/\s]+/)+",text))


@dataclass(frozen=True)
class TrainingGoal:
    """Plain-language intent kept separate from trainer-specific settings."""

    text: str
    kind: str = "concept"
    target_model_family: str = "SDXL"
    target_model_version: str = ""
    base_model_id: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.strip() or "concept",
            "text": self.text.strip(),
            "target_model_family": self.target_model_family.strip(),
            "target_model_version": self.target_model_version.strip(),
            "base_model_id": self.base_model_id.strip(),
        }


@dataclass(frozen=True)
class CandidateOverride:
    decision: str
    reason: str
    role: str = "training"


@dataclass(frozen=True)
class TrainingPermissionField:
    """One explicitly reported rights/privacy fact; absence remains unknown."""

    status: str = "unknown"
    value: str = ""
    evidence_source: str = ""

    def to_dict(self) -> dict[str,str]:
        return {
            "status":self.status,
            "value":self.value,
            "evidence_source":self.evidence_source,
        }


@dataclass(frozen=True)
class TrainingPermissionEvidence:
    """Strict plan-owned snapshot, separate from Image Knowledge and ratings."""

    creator: TrainingPermissionField = TrainingPermissionField()
    source: TrainingPermissionField = TrainingPermissionField()
    license: TrainingPermissionField = TrainingPermissionField()
    consent_or_use_restrictions: TrainingPermissionField = TrainingPermissionField()
    real_person_or_privacy: TrainingPermissionField = TrainingPermissionField()
    issues: tuple[str,...] = ()
    schema_version: int = 1

    def field_items(self):
        return tuple(
            (name,label,getattr(self,name)) for name,label in PERMISSION_FIELD_LABELS
        )

    @property
    def unknown_count(self) -> int:
        return sum(field.status=="unknown" for _,_,field in self.field_items())

    def to_dict(self) -> dict[str,object]:
        return {
            "schema_version":self.schema_version,
            **{name:field.to_dict() for name,_,field in self.field_items()},
            "issues":list(self.issues),
        }

    @classmethod
    def from_annotation_source(cls,source: object) -> "TrainingPermissionEvidence":
        """Read only the named contract; never infer rights from other metadata."""
        container=source if isinstance(source,dict) else {}
        raw=container.get("training_permission_evidence")
        if raw is None:
            return cls()
        issues=[]
        if (
            not isinstance(raw,dict)
            or type(raw.get("schema_version")) is not int
            or raw.get("schema_version")!=1
        ):
            return cls(issues=(
                "Recorded training permission evidence has an unsupported structure; all fields remain unknown.",
            ))
        values={}
        for name,label in PERMISSION_FIELD_LABELS:
            item=raw.get(name)
            if not isinstance(item,dict) or item.get("status")!="reported":
                values[name]=TrainingPermissionField()
                if item not in (None,{"status":"unknown","value":"","evidence_source":""}):
                    issues.append(f"{label} evidence is invalid and remains unknown.")
                continue
            value=" ".join(str(item.get("value") or "").split())
            evidence_source=" ".join(
                str(item.get("evidence_source") or "").split()
            )
            if (
                not value or not evidence_source or len(value)>2_000
                or len(evidence_source)>500 or _looks_machine_local(value)
                or _looks_machine_local(evidence_source)
            ):
                values[name]=TrainingPermissionField()
                issues.append(
                    f"{label} evidence is incomplete, too long, or machine-local and remains unknown."
                )
                continue
            values[name]=TrainingPermissionField(
                "reported",value,evidence_source,
            )
        return cls(**values,issues=tuple(issues))


@dataclass(frozen=True)
class TrainingPermissionDisposition:
    """User decision for one exact included evidence scope, not legal clearance."""

    status: str = "pending"
    reason: str = ""
    unknown_evidence_acknowledged: bool = False
    evidence_scope_fingerprint: str = ""
    policy_version: str = "collectigen.training-permission-review-v1"

    def to_dict(self) -> dict[str,object]:
        return {
            "status":self.status,
            "reason":self.reason,
            "unknown_evidence_acknowledged":self.unknown_evidence_acknowledged,
            "evidence_scope_fingerprint":self.evidence_scope_fingerprint,
            "policy_version":self.policy_version,
            "legal_permission_adjudicated":False,
        }


@dataclass(frozen=True)
class TrainingPlanIssue:
    code: str
    level: str
    summary: str
    reason: str
    direct_fix: str = ""
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "level": self.level,
            "summary": self.summary,
            "reason": self.reason,
            "direct_fix": self.direct_fix,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class TrainingImageEvidence:
    size: int
    mtime_ns: int
    sha256: str
    format: str
    mode: str
    width: int
    height: int
    frame_count: int
    exif_orientation: int
    has_alpha: bool
    has_color_profile: bool
    conversion_requirements: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "file": {
                "size": self.size,
                "mtime_ns": self.mtime_ns,
                "sha256": self.sha256,
            },
            "pixels": {
                "format": self.format,
                "mode": self.mode,
                "width": self.width,
                "height": self.height,
                "frame_count": self.frame_count,
                "exif_orientation": self.exif_orientation,
                "has_alpha": self.has_alpha,
                "has_color_profile": self.has_color_profile,
                "conversion_requirements": list(self.conversion_requirements),
            },
        }


@dataclass(frozen=True)
class TrainingCandidate:
    image: Path
    stable_id: str
    source_identity: dict[str, object]
    evidence: TrainingImageEvidence | None
    decision: str
    role: str
    decision_reason: str
    user_override: bool = False
    caption: str = ""
    caption_sha256: str = ""
    knowledge_fingerprint: str = ""
    projection_decisions: tuple[dict[str, object], ...] = ()
    exact_duplicate_group: str = ""
    split_group: str = ""
    recommended_decision: str = "include"
    recommended_role: str = "training"
    recommendation_reason: str = ""
    permission_evidence: TrainingPermissionEvidence = TrainingPermissionEvidence()
    issues: tuple[TrainingPlanIssue, ...] = ()
    derived_frame: DerivedFrameReference | None = None

    @property
    def blocking(self) -> tuple[TrainingPlanIssue, ...]:
        return tuple(issue for issue in self.issues if issue.level == "blocking")

    def to_manifest_dict(self) -> dict[str, object]:
        return {
            "stable_id": self.stable_id,
            "derived_frame": self.derived_frame.to_dict() if self.derived_frame else None,
            "source": dict(self.source_identity),
            "source_state": self.evidence.to_dict() if self.evidence else None,
            "decision": self.decision,
            "role": self.role,
            "decision_reason": self.decision_reason,
            "user_override": self.user_override,
            "caption": self.caption,
            "caption_sha256": self.caption_sha256,
            "knowledge_fingerprint": self.knowledge_fingerprint,
            "projection_decisions": [dict(value) for value in self.projection_decisions],
            "exact_duplicate_group": self.exact_duplicate_group,
            "split_group": self.split_group,
            "recommendation": {
                "decision": self.recommended_decision,
                "role": self.recommended_role,
                "reason": self.recommendation_reason,
            },
            "permission_evidence":self.permission_evidence.to_dict(),
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class TrainingCandidatePlan:
    goal: TrainingGoal
    source_label: str
    scope_fingerprint: str
    profile: ExportProfile
    candidates: tuple[TrainingCandidate, ...]
    issues: tuple[TrainingPlanIssue, ...] = ()
    limitations: tuple[str, ...] = ()
    frozen_scope: tuple[dict[str, object], ...] = ()
    unprocessed_scope: tuple[dict[str, object], ...] = ()
    cancelled: bool = False
    not_processed: tuple[Path, ...] = ()
    analysis: TrainingDatasetAnalysis | None = None
    permission_disposition: TrainingPermissionDisposition = TrainingPermissionDisposition()
    configuration: TrainingConfiguration | None = None
    schema_version: int = 6
    assessment_context_fingerprint: str = ""

    @property
    def ready(self) -> bool:
        return any(
            candidate.decision == "include" and candidate.role == "training"
            for candidate in self.candidates
        ) and not self.cancelled and not any(
            issue.level == "blocking" for issue in self.issues
        ) and not any(candidate.blocking for candidate in self.candidates)

    def count(self, decision: str) -> int:
        return sum(candidate.decision == decision for candidate in self.candidates)

    @property
    def permission_scope_fingerprint(self) -> str:
        return _stable_hash([
            {
                "stable_id":candidate.stable_id,
                "source_sha256":(
                    candidate.evidence.sha256 if candidate.evidence is not None else ""
                ),
                "permission_evidence":candidate.permission_evidence.to_dict(),
            }
            for candidate in self.candidates if candidate.decision=="include"
        ])

    @property
    def permission_unknown_count(self) -> int:
        return sum(
            candidate.permission_evidence.unknown_count
            for candidate in self.candidates if candidate.decision=="include"
        )

    @property
    def permission_ready(self) -> bool:
        disposition=self.permission_disposition
        return bool(
            self.count("include")
            and disposition.status=="proceed"
            and disposition.reason.strip()
            and disposition.evidence_scope_fingerprint==self.permission_scope_fingerprint
            and (
                not self.permission_unknown_count
                or disposition.unknown_evidence_acknowledged
            )
        )

    @property
    def configuration_issues(self):
        if self.configuration is None:
            return ()
        return validate_training_configuration(
            self.configuration,profile_family=self.profile.target_model_family,
            profile_version=self.profile.target_model_version,
        )

    @property
    def package_ready(self) -> bool:
        if self.configuration is None:
            return False
        strict=validate_training_configuration(
            self.configuration,profile_family=self.profile.target_model_family,
            profile_version=self.profile.target_model_version,
            require_base_model=True,
        )
        return bool(
            self.ready and self.permission_ready
            and not any(issue.level=="blocking" for issue in strict)
            and all(
                package_caption(candidate.caption,self.configuration).strip()
                for candidate in self.candidates if candidate.decision=="include"
            )
        )

    def to_manifest_dict(self) -> dict[str, object]:
        payload = {
            "schema_version": self.schema_version,
            "kind": "collectigen_training_candidate_plan",
            "goal": self.goal.to_dict(),
            "selection_scope": {
                "source_label": self.source_label,
                "scope_fingerprint": self.scope_fingerprint,
                "candidate_count": len(self.frozen_scope),
                "processed_count": len(self.candidates),
                "frozen_selection": [dict(value) for value in self.frozen_scope],
                "not_processed": [dict(value) for value in self.unprocessed_scope],
            },
            "profile": self.profile.to_dict(),
            "profile_provenance": self.profile.provenance(),
            "counts": {
                "included": self.count("include"),
                "excluded": self.count("exclude"),
                "training": sum(
                    candidate.decision == "include" and candidate.role == "training"
                    for candidate in self.candidates
                ),
                "validation": sum(
                    candidate.decision == "include" and candidate.role == "validation"
                    for candidate in self.candidates
                ),
                "holdout": sum(
                    candidate.decision == "include" and candidate.role == "holdout"
                    for candidate in self.candidates
                ),
                "blocking": sum(bool(candidate.blocking) for candidate in self.candidates),
                "not_processed": len(self.not_processed),
            },
            "candidates": [
                candidate.to_manifest_dict() for candidate in self.candidates
            ],
            "issues": [issue.to_dict() for issue in self.issues],
            "dataset_analysis": (
                self.analysis.to_dict() if self.analysis is not None else None
            ),
            "permission_review":self.permission_disposition.to_dict(),
            "training_configuration":(
                self.configuration.to_dict() if self.configuration is not None else None
            ),
            "training_configuration_issues":[
                issue.to_dict() for issue in self.configuration_issues
            ],
            "package_ready":self.package_ready,
            "limitations": list(self.limitations),
            "cancelled": self.cancelled,
        }
        return {**payload, "plan_fingerprint": _stable_hash(payload)}


class TrainingCandidateSelectionService:
    """Build an exact, non-mutating candidate plan from caller-owned scope."""

    LIMITATIONS = (
        "Current byte-identical evidence and canonical source-video frame lineage are grouped transitively. "
        "Other derivative relationships remain unchecked.",
        "Visual relationship evidence uses only matching current cached fingerprints when available; coverage is reported separately.",
        "Creator/source, license, consent or use restrictions, and real-person/privacy "
        "remain unknown unless the named structured evidence contract reports them. "
        "CollectiGen records evidence and the user's disposition; it does not adjudicate legal permission.",
    )
    OVERRIDE_ISSUE_CODES = {
        "invalid_override_decision",
        "invalid_override_role",
        "override_reason_required",
    }
    PERMISSION_ISSUE_CODES={
        "permission_disposition_invalid","permission_reason_required",
        "permission_unknown_acknowledgement_required","permission_scope_changed",
    }

    def __init__(
        self,
        knowledge: TagDatabase | None = None,
        characters: CharacterDatabase | None = None,
        categories=None,
        *,duplicate_evidence_provider=None,
    ) -> None:
        self.knowledge = knowledge
        self.characters = characters
        self.categories = categories
        self.duplicate_evidence_provider=duplicate_evidence_provider
        self.readiness = TrainingReadinessService(
            characters, knowledge, categories=categories
        )

    def assessment_context_fingerprint(self) -> str:
        """Identify shared assessment inputs without exposing them in manifests."""
        from .image_workflow_state import ImageWorkflowStateService

        return ImageWorkflowStateService(
            self.knowledge,self.characters,categories=self.categories,
        ).context_fingerprint()

    @staticmethod
    def _identity_lookup(
        identities: Mapping[object, LibraryImageIdentity] | None,
    ) -> dict[str, LibraryImageIdentity]:
        values: dict[str, LibraryImageIdentity] = {}
        for key, identity in (identities or {}).items():
            try:
                values[str(Path(key).resolve()).casefold()] = identity
            except (OSError, TypeError, ValueError):
                continue
        return values

    @staticmethod
    def _frozen_paths(images: Iterable[str | Path]) -> tuple[Path, ...]:
        paths: list[Path] = []
        seen: set[str] = set()
        for value in images:
            if not value:
                continue
            path = Path(value).resolve()
            key = str(path).casefold()
            if key in seen:
                continue
            seen.add(key)
            paths.append(path)
        return tuple(paths)

    @staticmethod
    def _source_identity(
        path: Path,
        identity: LibraryImageIdentity | None,
        source_sha256: str,
    ) -> tuple[str, dict[str, object], TrainingPlanIssue | None]:
        if identity is not None:
            source = {
                "kind": "library_image",
                "library_id": identity.library_id,
                "relative_image_path": identity.relative_path,
            }
            return identity.key, source, None
        fallback = f"content:{source_sha256}"
        return fallback, {
            "kind": "content_fingerprint",
            "filename_hint": path.name,
            "sha256": source_sha256,
        }, TrainingPlanIssue(
            "portable_identity_unavailable",
            "caution",
            "This image has no stable library identity in the selected scope.",
            "The plan can match its exact bytes, but another machine cannot resolve a "
            "library-relative path automatically.",
            "Add the image through a named library before writing a portable package.",
            (path.name, source_sha256),
        )

    @staticmethod
    def _inspect_image(path: Path) -> TrainingImageEvidence:
        raw, source = _read_source_snapshot(path)
        with Image.open(BytesIO(raw)) as opened:
            image_format = str(opened.format or "unknown")
            mode = str(opened.mode or "unknown")
            width, height = opened.size
            frame_count = int(getattr(opened, "n_frames", 1) or 1)
            orientation = int(opened.getexif().get(274, 1) or 1)
            has_alpha = "A" in opened.getbands() or "transparency" in opened.info
            has_color_profile = bool(opened.info.get("icc_profile"))
            for frame in range(frame_count):
                opened.seek(frame)
                opened.load()
        _current_raw, current = _read_source_snapshot(path)
        if current != source:
            raise RuntimeError(f"{path.name} changed while its pixels were decoded.")
        requirements: list[str] = []
        if orientation != 1:
            requirements.append("apply_exif_orientation")
        if frame_count > 1:
            requirements.append("select_static_frame")
        if has_alpha:
            requirements.append("preserve_or_flatten_alpha")
        if mode not in {"RGB", "RGBA", "L"}:
            requirements.append("convert_color_mode")
        return TrainingImageEvidence(
            int(source["size"]),
            int(source["mtime_ns"]),
            str(source["sha256"]),
            image_format,
            mode,
            int(width),
            int(height),
            frame_count,
            orientation,
            has_alpha,
            has_color_profile,
            tuple(requirements),
        )

    def _candidate(
        self,
        path: Path,
        goal: TrainingGoal,
        profile: ExportProfile,
        identity: LibraryImageIdentity | None,
        override: CandidateOverride | None,
    ) -> TrainingCandidate:
        issues: list[TrainingPlanIssue] = []
        try:
            evidence = self._inspect_image(path)
        except Exception as exc:
            if identity is not None:
                stable_id=identity.key
                source={
                    "kind":"library_image",
                    "library_id":identity.library_id,
                    "relative_image_path":identity.relative_path,
                }
            else:
                stable_id=f"unavailable:{_stable_hash(str(path))}"
                source={
                    "kind": "unavailable",
                    "filename_hint": path.name,
                }
            issue = TrainingPlanIssue(
                "source_unavailable",
                "blocking",
                "The selected image cannot be decoded from its current bytes.",
                (
                    "The file is missing or cannot be read."
                    if isinstance(exc, (FileNotFoundError, PermissionError)) else
                    f"The frozen source could not be inspected ({type(exc).__name__})."
                ),
                "Restore or replace the image, then rebuild this exact selection.",
                (path.name,),
            )
            return self._apply_candidate_override(
                TrainingCandidate(
                    path,
                    stable_id,
                    source,
                    None,
                    "exclude",
                    "training",
                    "Excluded because the frozen source image is unavailable or undecodable.",
                    recommended_decision="exclude",
                    recommendation_reason=(
                        "Excluded because the frozen source image is unavailable or undecodable."
                    ),
                    issues=(issue,),
                ),
                override,
            )
        stable_id, source, identity_issue = self._source_identity(
            path, identity, evidence.sha256
        )
        if identity_issue is not None:
            issues.append(identity_issue)
        if evidence.conversion_requirements:
            issues.append(TrainingPlanIssue(
                "conversion_review",
                "caution",
                "This image needs an explicit conversion decision before packaging.",
                "The actual decoded pixels expose orientation, animation, alpha, or color-mode behavior that an adapter must handle loss-consciously.",
                "Review the named conversion requirements before package export.",
                evidence.conversion_requirements,
            ))
        if min(evidence.width, evidence.height) < 512:
            issues.append(TrainingPlanIssue(
                "low_resolution",
                "caution",
                "The shorter image edge is below 512 pixels.",
                "Upscaling or heavy cropping may reduce useful detail for common modern training targets.",
                "Review the target resolution and crop policy; keep or exclude this image deliberately.",
                (f"{evidence.width}x{evidence.height}",),
            ))
        ratio = max(evidence.width, evidence.height) / max(
            1, min(evidence.width, evidence.height)
        )
        if ratio > 3:
            issues.append(TrainingPlanIssue(
                "extreme_aspect_ratio",
                "caution",
                "The image has an extreme aspect ratio.",
                "A target bucket or crop may discard important goal evidence.",
                "Preview the adapter-specific bucket and crop before package export.",
                (f"{evidence.width}x{evidence.height}", f"ratio={ratio:.2f}"),
            ))
        record = ImageRecord(path)
        derived_frame = None
        declared_frame = record.annotation.extra_fields.get("derived_frame")
        frame_source = record.annotation.source
        if "derived_frame" in record.annotation.extra_fields or frame_source.get("kind") == "verified_video_frame":
            try:
                if (not isinstance(declared_frame, dict)
                        or declared_frame.get("schema_id") != FRAME_SCHEMA_ID
                        or type(declared_frame.get("schema_version")) is not int
                        or declared_frame.get("schema_version") != SCHEMA_VERSION):
                    raise ValueError("The frame reference schema is missing or unsupported.")
                reference = DerivedFrameReference.from_dict(declared_frame)
                if (frame_source.get("kind") != "verified_video_frame"
                        or frame_source.get("source_asset_id") != reference.source_asset_id
                        or frame_source.get("source_sha256") != reference.source_sha256
                        or evidence is None or evidence.sha256 != reference.output_sha256):
                    raise ValueError("The frame reference does not match this image and its source declaration.")
                derived_frame = reference
            except (ValueError, TypeError, KeyError):
                issues.append(TrainingPlanIssue(
                    "derived_frame_lineage_invalid", "blocking",
                    "Recorded video-frame lineage needs review.",
                    "Missing, unsupported or stale frame evidence cannot establish a safe source-video split group.",
                    "Review the source declaration or re-extract the frame before including it in training.",
                ))
        permission_evidence=TrainingPermissionEvidence.from_annotation_source(
            record.annotation.source
        )
        if permission_evidence.issues:
            issues.append(TrainingPlanIssue(
                "permission_evidence_invalid","caution",
                "Some recorded permission evidence could not be carried into the plan.",
                "Only the strict source-neutral training_permission_evidence contract is accepted; raw Metadata and generator records are never interpreted as permission.",
                "Review the named fields and record a plan disposition before packaging.",
                permission_evidence.issues,
            ))
        projection = profile.project(
            record.annotation, self.characters, self.knowledge
        )
        assessment = self.readiness.assess_record(
            record, profile, include_metadata=False, include_output_health=True,
            caption_projection=projection,
        )
        caption = projection.render(profile.separator)
        for item in assessment.issues:
            level = (
                "blocking" if item.severity == "high" else
                "caution" if item.severity == "medium" else
                "recommendation"
            )
            issues.append(TrainingPlanIssue(
                f"profile_{item.code}",
                level,
                item.label,
                "This is the selected profile's exact per-image eligibility or Image Knowledge evidence, not a universal dataset score.",
                item.suggested_action,
                (item.component,) if item.component else (),
            ))
        goal_words = set(_words(goal.text))
        caption_words = set(_words(caption))
        if goal_words and not goal_words.intersection(caption_words):
            issues.append(TrainingPlanIssue(
                "literal_goal_coverage_unconfirmed",
                "caution",
                "The exact caption has no literal term from the stated goal.",
                "Aliases and visual evidence may still support the goal, so this is a review prompt rather than an automatic exclusion.",
                "Review the image and exact projection against the stated goal.",
                tuple(sorted(goal_words)),
            ))
        stem_words = set(_words(path.stem))
        leaked = tuple(sorted(stem_words.intersection(caption_words)))
        if leaked:
            issues.append(TrainingPlanIssue(
                "possible_filename_leakage",
                "caution",
                "The exact caption repeats terms found in the source filename.",
                "The terms may be valid confirmed facts, but filename-derived correlations require explicit review.",
                "Inspect provenance for the matching terms before package export.",
                leaked,
            ))
        blocking = any(issue.level == "blocking" for issue in issues)
        decision = "exclude" if blocking else "include"
        reason = (
            "Excluded because one or more concrete package requirements are blocked."
            if blocking else
            "Included because the source decodes and the selected profile emits an eligible non-empty caption."
        )
        candidate = TrainingCandidate(
            path,
            stable_id,
            source,
            evidence,
            decision,
            "training",
            reason,
            recommended_decision=decision,
            recommendation_reason=reason,
            permission_evidence=permission_evidence,
            derived_frame=derived_frame,
            caption=caption,
            caption_sha256=hashlib.sha256(caption.encode("utf-8")).hexdigest(),
            knowledge_fingerprint=annotation_knowledge_fingerprint(record.annotation),
            projection_decisions=tuple(
                decision.to_dict() for decision in projection.decisions
            ),
            issues=tuple(issues),
        )
        return self._apply_candidate_override(candidate,override)

    @classmethod
    def _recommended_candidate(cls,candidate: TrainingCandidate) -> TrainingCandidate:
        issues=tuple(
            issue for issue in candidate.issues
            if issue.code not in cls.OVERRIDE_ISSUE_CODES
        )
        blocking=any(issue.level=="blocking" for issue in issues)
        decision=(
            "exclude" if blocking else
            candidate.recommended_decision or "include"
        )
        reason=(
            "Excluded because one or more concrete package requirements are blocked."
            if blocking else
            candidate.recommendation_reason
            or "Included because the source decodes and the selected profile emits an eligible non-empty caption."
        )
        return replace(
            candidate,
            decision=decision,
            role=candidate.recommended_role or "training",
            decision_reason=reason,
            user_override=False,
            issues=issues,
        )

    @classmethod
    def _apply_candidate_override(
        cls,candidate: TrainingCandidate,override: CandidateOverride | None,
    ) -> TrainingCandidate:
        candidate=cls._recommended_candidate(candidate)
        if override is None:
            return candidate
        issues=list(candidate.issues)
        requested=override.decision.strip().casefold()
        requested_role=override.role.strip().casefold() or "training"
        if requested not in {"include","exclude"}:
            issues.append(TrainingPlanIssue(
                "invalid_override_decision","blocking",
                "The candidate override is invalid.",
                "Only include or exclude is supported in this planning slice.",
                "Choose Include or Exclude and record the reason.",
                (override.decision,),
            ))
        elif requested_role not in {"training","validation","holdout"}:
            issues.append(TrainingPlanIssue(
                "invalid_override_role","blocking",
                "The candidate role override is invalid.",
                "Only training, validation, or holdout are portable role names.",
                "Choose a named role and record why it differs from the default.",
                (override.role,),
            ))
        elif not override.reason.strip():
            issues.append(TrainingPlanIssue(
                "override_reason_required","blocking",
                "The candidate override has no recorded reason.",
                "Every user change to a recommendation must remain explainable and reproducible.",
                "Enter why this image should be included or excluded.",
            ))
        else:
            return replace(
                candidate,decision=requested,role=requested_role,
                decision_reason=override.reason.strip(),user_override=True,
            )
        return replace(candidate,user_override=True,issues=tuple(issues))

    @classmethod
    def _base_candidate(
        cls,candidate: TrainingCandidate,
        previous_split_suffix: str="",
    ) -> TrainingCandidate:
        """Remove plan-wide and user-decision projections before recomputing them."""
        base=cls._recommended_candidate(candidate)
        reason=base.recommendation_reason
        if previous_split_suffix and reason.endswith(previous_split_suffix):
            reason=reason[:-len(previous_split_suffix)]
        issues=tuple(
            issue for issue in base.issues
            if issue.code not in {*cls.OVERRIDE_ISSUE_CODES,"exact_duplicate"}
        )
        return replace(
            base,role="training",recommended_role="training",
            decision_reason=reason,recommendation_reason=reason,
            user_override=False,exact_duplicate_group="",split_group="",
            issues=issues,
        )

    @staticmethod
    def _overrides_from_plan(
        plan: TrainingCandidatePlan,
    ) -> dict[str,CandidateOverride]:
        overrides={}
        for candidate in plan.candidates:
            if not candidate.user_override:
                continue
            override=CandidateOverride(
                candidate.decision,candidate.decision_reason,candidate.role,
            )
            # Invalid requests retain recommended values on the candidate;
            # those values must not turn the rejected request into approval.
            for issue in candidate.issues:
                if issue.code=="invalid_override_decision":
                    override=replace(override,decision=issue.evidence[0] if issue.evidence else "")
                elif issue.code=="invalid_override_role":
                    override=replace(override,role=issue.evidence[0] if issue.evidence else "invalid")
                elif issue.code=="override_reason_required":
                    override=replace(override,reason="")
            overrides[candidate.stable_id]=override
        return overrides

    @classmethod
    def _base_candidates_from_plan(
        cls,plan: TrainingCandidatePlan,
    ) -> Iterable[TrainingCandidate]:
        groups_by_id={
            stable_id:group for group in plan.analysis.groups
            for stable_id in group.member_stable_ids
        } if plan.analysis is not None else {}
        for candidate in plan.candidates:
            group=groups_by_id.get(candidate.stable_id)
            suffix=(
                f" The {plan.analysis.split_rule_version} rule recommends "
                f"{group.recommended_role} for split group "
                f"{group.group_id} using seed {plan.analysis.split_seed}."
                if candidate.split_group and group is not None else ""
            )
            yield cls._base_candidate(candidate,suffix)

    def _duplicate_analysis(self,analysis,candidates,progress,should_cancel):
        if self.duplicate_evidence_provider is None:
            return analysis
        from .training_duplicate_evidence import TrainingDuplicateEvidence
        from .training_dataset_analysis import with_duplicate_findings

        snapshot=self.duplicate_evidence_provider(
            candidates,progress=progress,should_cancel=should_cancel,
        )
        snapshot=TrainingDuplicateEvidence.from_dict(
            snapshot.to_dict(),source_hashes={
                item.stable_id:item.evidence.sha256 if item.evidence else ""
                for item in candidates
            },
        )
        return with_duplicate_findings(
            replace(analysis,duplicate_evidence=snapshot),candidates,
        )

    @classmethod
    def _finalize_candidates(
        cls,candidates: Iterable[TrainingCandidate],*,goal: TrainingGoal,
        profile: ExportProfile,scope_fingerprint: str,
        overrides: Mapping[str,CandidateOverride] | None=None,
    ) -> tuple[tuple[TrainingCandidate,...],TrainingDatasetAnalysis]:
        """Recompute plan-wide evidence, then restore explicit user decisions."""
        values=list(candidates)
        duplicate_indexes: dict[str,list[int]]={}
        for index,candidate in enumerate(values):
            if candidate.evidence is not None:
                duplicate_indexes.setdefault(candidate.evidence.sha256,[]).append(index)
        for digest,indexes in duplicate_indexes.items():
            if len(indexes)<2:
                continue
            group=f"exact:{digest[:16]}"
            names=tuple(values[index].image.name for index in indexes)
            issue=TrainingPlanIssue(
                "exact_duplicate","caution",
                "This candidate is byte-identical to another selected image.",
                "Exact duplicates can unintentionally increase effective training weight and must remain in one future split role.",
                "Keep one copy, or record why repeated weighting is intentional.",
                names,
            )
            for index in indexes:
                candidate=values[index]
                values[index]=replace(
                    candidate,exact_duplicate_group=group,
                    issues=(*candidate.issues,issue),
                )
        analysis=analyze_training_candidates(
            values,goal=goal.to_dict(),scope_fingerprint=scope_fingerprint,
            profile_provenance=profile.provenance(),
        )
        groups_by_id={
            stable_id:group for group in analysis.groups
            for stable_id in group.member_stable_ids
        }
        recommended=[]
        for candidate in values:
            group=groups_by_id.get(candidate.stable_id)
            if group is None:
                recommended.append(candidate)
                continue
            role=group.recommended_role
            reason=(
                candidate.recommendation_reason
                +f" The {analysis.split_rule_version} rule recommends {role} for "
                f"split group {group.group_id} using seed {analysis.split_seed}."
            )
            recommended.append(replace(
                candidate,role=role,recommended_role=role,
                recommendation_reason=reason,decision_reason=reason,
                split_group=group.group_id,
            ))
        selected=tuple(
            cls._apply_candidate_override(
                candidate,(overrides or {}).get(candidate.stable_id),
            )
            for candidate in recommended
        )
        return selected,with_effective_split_findings(
            analysis,selected,goal=goal.to_dict(),
            scope_fingerprint=scope_fingerprint,
        )

    @classmethod
    def apply_overrides(
        cls,
        plan: TrainingCandidatePlan,
        overrides: Mapping[str,CandidateOverride],
    ) -> TrainingCandidatePlan:
        """Apply explicit decisions to inspected candidates without rereading sources."""
        candidates=tuple(
            cls._apply_candidate_override(
                candidate,overrides.get(candidate.stable_id)
            )
            for candidate in plan.candidates
        )
        analysis=(
            with_effective_split_findings(
                plan.analysis,candidates,goal=plan.goal.to_dict(),
                scope_fingerprint=plan.scope_fingerprint,
            )
            if plan.analysis is not None else None
        )
        permission=plan.permission_disposition
        configuration=(
            recontextualize_training_configuration(
                plan.configuration,
                included_count=sum(item.decision=="include" for item in candidates),
                goal_kind=plan.goal.kind,
            )
            if plan.configuration is not None else None
        )
        next_plan=replace(
            plan,candidates=candidates,analysis=analysis,
            configuration=configuration,
        )
        if permission.status=="pending":
            return replace(
                next_plan,
                permission_disposition=replace(
                    permission,
                    evidence_scope_fingerprint=next_plan.permission_scope_fingerprint,
                ),
            )
        if (
            permission.evidence_scope_fingerprint
            !=next_plan.permission_scope_fingerprint
        ):
            issues=tuple(
                issue for issue in next_plan.issues
                if issue.code not in cls.PERMISSION_ISSUE_CODES
            )+(TrainingPlanIssue(
                "permission_scope_changed","caution",
                "The recorded permission disposition no longer matches the included scope.",
                "An inclusion change altered the exact images or permission evidence covered by the prior decision.",
                "Review the current Source & Permission evidence and record a new disposition.",
            ),)
            return replace(
                next_plan,issues=issues,
                permission_disposition=TrainingPermissionDisposition(
                    evidence_scope_fingerprint=next_plan.permission_scope_fingerprint,
                ),
            )
        return next_plan

    @classmethod
    def apply_configuration(
        cls,plan: TrainingCandidatePlan,configuration: TrainingConfiguration,
    ) -> TrainingCandidatePlan:
        """Apply one explicit neutral settings snapshot without touching sources."""
        current=recontextualize_training_configuration(
            configuration,included_count=plan.count("include"),
            goal_kind=plan.goal.kind,
        )
        goal=replace(
            plan.goal,target_model_family=current.target_model_family,
            target_model_version=current.target_model_version,
            base_model_id=current.base_model_id,
        )
        if goal==plan.goal:
            return replace(plan,configuration=current)
        # The split seed includes model identity. Reproject the inspected scope
        # before packaging can compare it with a fresh source inspection.
        candidates,analysis=cls._finalize_candidates(
            cls._base_candidates_from_plan(plan),goal=goal,profile=plan.profile,
            scope_fingerprint=plan.scope_fingerprint,
            overrides=cls._overrides_from_plan(plan),
        )
        if plan.analysis is not None and plan.analysis.duplicate_evidence is not None:
            analysis=with_duplicate_findings(
                replace(analysis,duplicate_evidence=plan.analysis.duplicate_evidence),
                candidates,
            )
        updated=replace(
            plan,goal=goal,candidates=candidates,analysis=analysis,
            configuration=recontextualize_training_configuration(
                current,included_count=sum(item.decision=="include" for item in candidates),
                goal_kind=goal.kind,
            ),
        )
        return cls.apply_permission_disposition(updated,plan.permission_disposition)

    @classmethod
    def apply_permission_disposition(
        cls,plan: TrainingCandidatePlan,disposition: TrainingPermissionDisposition,
    ) -> TrainingCandidatePlan:
        """Bind one explicit user review to the exact currently included evidence."""
        issues=tuple(
            issue for issue in plan.issues
            if issue.code not in cls.PERMISSION_ISSUE_CODES
        )
        status=str(disposition.status or "pending").strip().casefold()
        reason=" ".join(str(disposition.reason or "").split())
        added=[]
        if (
            status!="pending" and disposition.evidence_scope_fingerprint
            and disposition.evidence_scope_fingerprint
            !=plan.permission_scope_fingerprint
        ):
            return replace(
                plan,
                issues=(*issues,TrainingPlanIssue(
                    "permission_scope_changed","caution",
                    "The saved permission disposition does not match current evidence.",
                    "The included image set or its structured permission evidence changed after the disposition was recorded.",
                    "Review the current Source & Permission evidence and record a new disposition.",
                )),
                permission_disposition=TrainingPermissionDisposition(
                    evidence_scope_fingerprint=plan.permission_scope_fingerprint,
                ),
            )
        if status not in {"pending","proceed","needs_review"}:
            added.append(TrainingPlanIssue(
                "permission_disposition_invalid","blocking",
                "The source and permission disposition is invalid.",
                "Only Pending, Needs review, or Proceed for this planned use is supported.",
                "Choose a named disposition and record its reason.",
            ))
            status="pending"; reason=""
        elif status!="pending" and not reason:
            added.append(TrainingPlanIssue(
                "permission_reason_required","blocking",
                "The source and permission disposition has no recorded reason.",
                "A package decision must remain explainable without implying legal adjudication.",
                "Record why this exact scope should proceed or remain on hold.",
            ))
        elif (
            status=="proceed" and plan.permission_unknown_count
            and not disposition.unknown_evidence_acknowledged
        ):
            added.append(TrainingPlanIssue(
                "permission_unknown_acknowledgement_required","blocking",
                "Unknown source or permission evidence was not acknowledged.",
                "At least one included field is unknown; CollectiGen cannot infer creator, license, consent, restrictions, or privacy permission.",
                "Acknowledge the unknown evidence or keep this plan on hold.",
            ))
        bound=TrainingPermissionDisposition(
            status,reason,bool(disposition.unknown_evidence_acknowledged),
            plan.permission_scope_fingerprint,
        )
        return replace(plan,issues=(*issues,*added),permission_disposition=bound)

    def refresh_candidates(
        self,plan: TrainingCandidatePlan,stable_ids: Iterable[str],*,
        expected_plan_fingerprint: str,
        progress: ProgressCallback | None=None,
        should_cancel: CancelCallback | None=None,
    ) -> TrainingCandidatePlan:
        """Refresh exact candidates while retaining the caller-owned plan scope."""
        current_fingerprint=plan.to_manifest_dict()["plan_fingerprint"]
        if expected_plan_fingerprint!=current_fingerprint:
            raise RuntimeError(
                "The training plan changed before its candidate refresh started."
            )
        if (
            plan.cancelled or plan.not_processed or plan.unprocessed_scope
            or len(plan.candidates)!=len(plan.frozen_scope)
            or plan.analysis is None
        ):
            raise RuntimeError(
                "Only a complete current candidate plan can be refreshed incrementally."
            )
        requested=tuple(str(value) for value in stable_ids)
        if not requested or any(not value for value in requested):
            raise ValueError("Choose at least one exact candidate to refresh.")
        if len(set(requested))!=len(requested):
            raise ValueError("A candidate refresh cannot contain duplicate stable IDs.")
        by_id={candidate.stable_id:candidate for candidate in plan.candidates}
        if len(by_id)!=len(plan.candidates):
            raise RuntimeError("The current training plan has duplicate candidate identities.")
        unknown=tuple(value for value in requested if value not in by_id)
        if unknown:
            raise ValueError(
                "The candidate refresh is outside the frozen plan scope: "
                +", ".join(unknown)
            )
        cancel=should_cancel or (lambda:False)
        notify=progress or (lambda *_:None)
        if cancel():
            raise InterruptedError("Training candidate refresh was cancelled safely.")
        overrides=self._overrides_from_plan(plan)
        identities={}
        for candidate in plan.candidates:
            source=candidate.source_identity
            if source.get("kind")=="library_image":
                identities[candidate.image]=LibraryImageIdentity(
                    str(source["library_id"]),str(source["relative_image_path"]),
                )
        context_before=self.assessment_context_fingerprint()
        if (
            not plan.assessment_context_fingerprint
            or plan.assessment_context_fingerprint!=context_before
        ):
            rebuilt=self.plan(
                (candidate.image for candidate in plan.candidates),
                plan.goal,plan.profile,identities=identities,overrides=overrides,
                source_label=plan.source_label,progress=progress,
                should_cancel=should_cancel,
                permission_disposition=plan.permission_disposition,
                configuration=plan.configuration,
            )
            if rebuilt.cancelled or cancel():
                raise InterruptedError("Training candidate refresh was cancelled safely.")
            if (
                rebuilt.frozen_scope!=plan.frozen_scope
                or rebuilt.scope_fingerprint!=plan.scope_fingerprint
                or tuple(item.stable_id for item in rebuilt.candidates)
                !=tuple(item.stable_id for item in plan.candidates)
            ):
                raise RuntimeError(
                    "Current evidence changed a candidate identity in the frozen plan scope."
                )
            if (
                self.assessment_context_fingerprint()
                !=rebuilt.assessment_context_fingerprint
            ):
                raise RuntimeError(
                    "Training assessment context changed during candidate refresh."
                )
            if cancel():
                raise InterruptedError("Training candidate refresh was cancelled safely.")
            return rebuilt

        target_ids=set(requested)
        base_candidates=[]; completed=0
        for candidate in self._base_candidates_from_plan(plan):
            if candidate.stable_id not in target_ids:
                base_candidates.append(candidate)
                continue
            if cancel():
                raise InterruptedError("Training candidate refresh was cancelled safely.")
            refreshed=self._candidate(
                candidate.image,plan.goal,plan.profile,
                identities.get(candidate.image),None,
            )
            if refreshed.stable_id!=candidate.stable_id:
                raise RuntimeError(
                    "Current evidence changed an anonymous candidate identity; rebuild the exact selection."
                )
            base_candidates.append(refreshed)
            completed+=1
            notify(completed,len(requested),candidate.image.name)
        if cancel():
            raise InterruptedError("Training candidate refresh was cancelled safely.")
        candidates,analysis=self._finalize_candidates(
            base_candidates,goal=plan.goal,profile=plan.profile,
            scope_fingerprint=plan.scope_fingerprint,overrides=overrides,
        )
        analysis=self._duplicate_analysis(analysis,candidates,notify,cancel)
        configuration=(
            recontextualize_training_configuration(
                plan.configuration,
                included_count=sum(item.decision=="include" for item in candidates),
                goal_kind=plan.goal.kind,
            )
            if plan.configuration is not None else None
        )
        issues=tuple(
            issue for issue in plan.issues
            if issue.code not in self.PERMISSION_ISSUE_CODES
        )
        refreshed_plan=replace(
            plan,candidates=candidates,analysis=analysis,issues=issues,
            configuration=configuration,
            permission_disposition=TrainingPermissionDisposition(),
            assessment_context_fingerprint=context_before,
        )
        refreshed_plan=self.apply_permission_disposition(
            refreshed_plan,plan.permission_disposition,
        )
        context_after=self.assessment_context_fingerprint()
        if context_after!=context_before:
            raise RuntimeError(
                "Training assessment context changed during candidate refresh."
            )
        if cancel():
            raise InterruptedError("Training candidate refresh was cancelled safely.")
        return replace(
            refreshed_plan,assessment_context_fingerprint=context_after,
        )

    def plan(
        self,
        images: Iterable[str | Path],
        goal: TrainingGoal,
        profile: ExportProfile | None = None,
        *,
        identities: Mapping[object, LibraryImageIdentity] | None = None,
        overrides: Mapping[str, CandidateOverride] | None = None,
        source_label: str = "Explicit image selection",
        progress: ProgressCallback | None = None,
        should_cancel: CancelCallback | None = None,
        permission_disposition: TrainingPermissionDisposition | None = None,
        configuration: TrainingConfiguration | None = None,
    ) -> TrainingCandidatePlan:
        paths = self._frozen_paths(images)
        selected = profile or ExportProfile()
        context_before=self.assessment_context_fingerprint()
        lookup = self._identity_lookup(identities)
        frozen_scope = tuple(
            (
                {
                    "kind": "library_image",
                    "library_id": identity.library_id,
                    "relative_image_path": identity.relative_path,
                }
                if (identity := lookup.get(str(path).casefold())) is not None else
                {
                    "kind": "local_selection",
                    "filename_hint": path.name,
                    "selection_key": hashlib.sha256(
                        str(path).casefold().encode("utf-8")
                    ).hexdigest(),
                }
            )
            for path in paths
        )
        override_lookup = dict(overrides or {})
        notify = progress or (lambda *_: None)
        cancel = should_cancel or (lambda: False)
        candidates: list[TrainingCandidate] = []
        not_processed: tuple[Path, ...] = ()
        for index, path in enumerate(paths, 1):
            if cancel():
                not_processed = paths[index - 1:]
                break
            identity = lookup.get(str(path).casefold())
            candidate = self._candidate(path, goal, selected, identity, None)
            candidates.append(candidate)
            notify(index, len(paths), path.name)
        scope_fingerprint=_stable_hash(frozen_scope)
        candidates,analysis=self._finalize_candidates(
            candidates,goal=goal,profile=selected,
            scope_fingerprint=scope_fingerprint,overrides=override_lookup,
        )
        if not not_processed and not cancel():
            analysis=self._duplicate_analysis(analysis,candidates,notify,cancel)
        plan_issues: list[TrainingPlanIssue] = []
        if not goal.text.strip():
            plan_issues.append(TrainingPlanIssue(
                "goal_required",
                "blocking",
                "A plain-language training goal is required.",
                "Recommendations cannot be interpreted without knowing what the dataset is intended to teach.",
                "Describe the intended character, style, product, or broader concept.",
            ))
        if not paths:
            plan_issues.append(TrainingPlanIssue(
                "selection_required",
                "blocking",
                "At least one explicitly selected image is required.",
                "The planner never broadens an empty or invalid scope to the current library.",
                "Select the exact candidate images in Dataset.",
            ))
        selected_configuration=(
            recontextualize_training_configuration(
                configuration,included_count=sum(
                    item.decision=="include" for item in candidates
                ),goal_kind=goal.kind,
            )
            if configuration is not None else
            recommended_training_configuration(
                target_model_family=(
                    goal.target_model_family or selected.target_model_family
                ),
                target_model_version=(
                    goal.target_model_version or selected.target_model_version
                ),
                base_model_id=goal.base_model_id,goal_kind=goal.kind,
                included_count=sum(item.decision=="include" for item in candidates),
                profile_token_budget=selected.token_budget,
            )
        )
        plan=TrainingCandidatePlan(
            goal,
            source_label.strip() or "Explicit image selection",
            scope_fingerprint,
            selected,
            tuple(candidates),
            tuple(plan_issues),
            self.LIMITATIONS,
            frozen_scope,
            frozen_scope[len(candidates):],
            cancelled=bool(not_processed),
            not_processed=not_processed,
            analysis=analysis,
            configuration=selected_configuration,
            assessment_context_fingerprint=context_before,
        )
        context_after=self.assessment_context_fingerprint()
        if context_after!=context_before:
            raise RuntimeError(
                "Training assessment context changed while the plan was being built."
            )
        plan=replace(plan,assessment_context_fingerprint=context_after)
        return self.apply_permission_disposition(
            plan,permission_disposition or TrainingPermissionDisposition(),
        )
