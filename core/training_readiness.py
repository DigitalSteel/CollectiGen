from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .character_database import CharacterDatabase
from .export_profile import CaptionProjection, ExportProfile
from .image_record import ImageRecord
from .tag_database import TagDatabase
from .content_sensitivity import rating_conflicts
from .image_knowledge_completeness import (
    CompletenessComponent,
    ImageKnowledgeCompleteness,
)
from .image_knowledge_health import (
    ImageKnowledgeHealthService,
    MetadataBackupHealth,
    TrainingExportHealth,
)


@dataclass
class ReadinessIssue:
    code: str
    label: str
    severity: str
    suggested_action: str
    deduction: float = 0.0
    component: str = ""


@dataclass
class TrainingReadiness:
    image: Path
    ready: bool
    score: int
    issues: list[ReadinessIssue] = field(default_factory=list)
    export_preview: str = ""
    components: list[CompletenessComponent] = field(default_factory=list)
    quickest_action: str = ""
    completeness: ImageKnowledgeCompleteness | None = None
    training_export_health: TrainingExportHealth | None = None
    metadata_backup_health: MetadataBackupHealth | None = None


class TrainingReadinessService:
    """One readiness definition shared by ingest, cleanup, search, and export."""

    def __init__(self, characters:CharacterDatabase|None=None, knowledge:TagDatabase|None=None, *, metadata=None, categories=None, ignored_plain_language=()) -> None:
        self.characters=characters; self.knowledge=knowledge; self.categories=categories
        self.health=ImageKnowledgeHealthService(
            metadata=metadata,knowledge=knowledge,characters=characters,
            ignored_plain_language=ignored_plain_language,
        )
        self.completeness=self.health.completeness

    def assess(
        self,image:str|Path,profile:ExportProfile|None=None,*,
        include_metadata:bool=True,include_output_health:bool=True,
    ) -> TrainingReadiness:
        return self.assess_record(
            ImageRecord(Path(image)),profile,
            include_metadata=include_metadata,
            include_output_health=include_output_health,
        )

    def assess_record(
        self,record:ImageRecord,profile:ExportProfile|None=None,*,
        include_metadata:bool=True,include_output_health:bool=True,
        candidate_findings:list|None=None,
        caption_projection:CaptionProjection|None=None,
    ) -> TrainingReadiness:
        """Assess a loaded authority record without redundant sidecar reads.

        A caller may supply the request-local projection it just derived from
        this exact record, profile, character database, and knowledge database.
        The service never retains it across calls.
        """
        path=record.image_path
        selected_profile=profile or ExportProfile()
        health=self.health.assess_record(
            record,profile=selected_profile,include_metadata=include_metadata,
            include_output_health=include_output_health,
            candidate_findings=candidate_findings,
            caption_projection=caption_projection,
        )
        completeness=health.completeness
        issues=[ReadinessIssue(
            item.code,item.label,item.severity,item.suggested_action,item.deduction,item.component,
        ) for item in completeness.issues]
        for slot in record.annotation.characters:
            for outfit_id in slot.outfit_ids:
                if self.characters and outfit_id not in self.characters.outfits:
                    issues.append(ReadinessIssue("unknown_outfit",f"Character {slot.slot} references an unknown outfit","medium","Choose or create the outfit in learned knowledge.",0,"review_resolution"))
        if self.knowledge is not None:
            conflicts=rating_conflicts(record.annotation,self.knowledge,self.categories)
            if conflicts:
                issues.append(ReadinessIssue("rating_conflict","; ".join(conflicts),"high","Review and correct the confirmed image rating or the conflicting structured tags.",0,"review_resolution"))
        export=""
        if include_output_health:
            rating_excluded=(
                self.knowledge is not None
                and not selected_profile.includes(
                    record.annotation,self.knowledge,self.categories,
                )
            )
            export=(
                caption_projection.render(selected_profile.separator)
                if caption_projection is not None else
                selected_profile.render(
                    record.annotation,self.characters,self.knowledge,
                )
            )
            if rating_excluded:
                issues.append(ReadinessIssue("rating_excluded","Excluded by the selected export rating limit","high","Choose a broader export rating profile or leave this image out of the export.",0,"selected_export"))
            elif not export.strip():
                issues.append(ReadinessIssue("empty_export","The selected training profile would export an empty caption","high","Confirm at least one included tag or description.",0,"selected_export"))
        score=completeness.score
        quickest=(sorted(issues,key=lambda item:(-item.deduction,item.code))[0].suggested_action
                  if issues else "No completeness repair is needed.")
        return TrainingReadiness(
            path,completeness.ready and not any(item.severity=="high" for item in issues),score,issues,export,
            completeness.components,quickest,completeness,
            health.training_export,health.metadata_backup,
        )
