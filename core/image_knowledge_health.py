from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from .export_profile import CaptionProjection, DEFAULT_EXPORT_PROFILE, ExportProfile
from .image_knowledge_completeness import (
    ImageKnowledgeCompleteness,
    ImageKnowledgeCompletenessService,
)
from .image_knowledge_reconciliation import (
    _fingerprint,
    _managed_xmp_subject,
    knowledge_fingerprint,
)
from .image_record import ImageRecord
from .metadata_sync import MetadataSyncService
from .tag_collection import TagCollection, normalize_tag


@dataclass(frozen=True)
class OutputHealthIssue:
    code: str
    label: str
    severity: str
    suggested_action: str


@dataclass(frozen=True)
class TrainingExportHealth:
    image: Path
    state: str
    label: str
    detail: str
    suggested_action: str
    profile_id: str
    profile_version: int
    profile_name: str
    profile_fingerprint: str
    readable: bool = False
    verified: bool = False
    reproducible: bool = False
    current: bool = False
    issues: tuple[OutputHealthIssue, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MetadataBackupHealth:
    image: Path
    state: str
    label: str
    detail: str
    suggested_action: str
    container: str
    expected_tags: int = 0
    backed_up_tags: int = 0
    missing_tags: int = 0
    extra_tags: int = 0
    supported: bool = False
    readable: bool = False
    complete: bool = False
    verified: bool = False
    current: bool = False
    issues: tuple[OutputHealthIssue, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ImageKnowledgeHealth:
    completeness: ImageKnowledgeCompleteness
    training_export: TrainingExportHealth | None
    metadata_backup: MetadataBackupHealth | None


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ImageKnowledgeHealthService:
    """Assess Image Knowledge and optional outputs as three separate states."""

    def __init__(
        self,
        *,
        metadata: MetadataSyncService | None = None,
        knowledge=None,
        characters=None,
        ignored_plain_language=(),
    ) -> None:
        self.metadata = metadata
        self.knowledge = knowledge
        self.characters = characters
        self.completeness = ImageKnowledgeCompletenessService(
            knowledge=knowledge,ignored_plain_language=ignored_plain_language,
        )

    def assess(
        self,
        image: str | Path,
        *,
        profile: ExportProfile | None = None,
        case_type: str | None = None,
        include_metadata: bool = True,
        include_output_health: bool = True,
    ) -> ImageKnowledgeHealth:
        return self.assess_record(
            ImageRecord(Path(image)),profile=profile,case_type=case_type,
            include_metadata=include_metadata,
            include_output_health=include_output_health,
        )

    def assess_record(
        self,
        record: ImageRecord,
        *,
        profile: ExportProfile | None = None,
        case_type: str | None = None,
        include_metadata: bool = True,
        include_output_health: bool = True,
        candidate_findings: list | None = None,
        caption_projection: CaptionProjection | None = None,
    ) -> ImageKnowledgeHealth:
        """Assess one loaded record and optionally skip unrelated output files."""
        selected_profile = profile or DEFAULT_EXPORT_PROFILE
        return ImageKnowledgeHealth(
            completeness=self.completeness.assess_record(
                record,case_type=case_type,
                candidate_findings=candidate_findings,
            ),
            training_export=(
                self.assess_training_export(
                    record,selected_profile,
                    caption_projection=caption_projection,
                )
                if include_output_health else None
            ),
            metadata_backup=(
                self.assess_metadata_backup(
                    record,include_metadata=include_metadata,
                )
                if include_output_health else None
            ),
        )

    def assess_training_export(
        self,
        record: ImageRecord,
        profile: ExportProfile | None = None,
        *,
        caption_projection: CaptionProjection | None = None,
    ) -> TrainingExportHealth:
        """Assess TXT produced for the selected profile.

        ``caption_projection`` is request-local reuse for the exact record,
        profile, character database, and knowledge database in this synchronous
        call. This service does not retain it.
        """
        selected = profile or DEFAULT_EXPORT_PROFILE
        path = record.image_path
        provenance = selected.provenance()
        common = {
            "image": path,
            "profile_id": selected.profile_id,
            "profile_version": selected.version,
            "profile_name": selected.name,
            "profile_fingerprint": selected.fingerprint(),
        }
        if not record.caption_path.exists():
            action = (
                f"Export Training TXT with {selected.name} v{selected.version}, "
                "or leave this optional output absent."
            )
            issue = OutputHealthIssue(
                "missing_training_export",
                "No Training TXT exists for the selected profile",
                "medium",
                action,
            )
            return TrainingExportHealth(
                **common,
                state="missing",
                label="Missing",
                detail=(
                    "The selected profile has no adjacent Training TXT. "
                    "Image Knowledge completeness is unchanged."
                ),
                suggested_action=action,
                issues=(issue,),
            )
        try:
            current_text = record.caption_path.read_bytes().decode("utf-8")
        except Exception as exc:
            action = "Repair or replace the unreadable Training TXT through a previewed export."
            issue = OutputHealthIssue(
                "unreadable_training_export",
                f"Training TXT could not be read: {exc}",
                "high",
                action,
            )
            return TrainingExportHealth(
                **common,
                state="unreadable",
                label="Unreadable",
                detail=str(exc),
                suggested_action=action,
                issues=(issue,),
            )

        projection = (
            caption_projection
            if caption_projection is not None else
            record.output_projection(selected,self.characters,self.knowledge)
        )
        expected = projection.render(selected.separator)

        state = record.annotation.projection_state.get("txt", {})
        state = state if isinstance(state, dict) else {}
        file_fingerprint = _fingerprint(record.caption_path)
        verified = bool(
            state.get("status") == "verified"
            and state.get("projection_fingerprint") == file_fingerprint
            and state.get("content_sha256") == _text_sha256(current_text)
        )
        profile_matches = bool(
            state.get("profile_id") == provenance["profile_id"]
            and state.get("profile_version") == provenance["profile_version"]
            and state.get("profile_fingerprint") == provenance["profile_fingerprint"]
        )
        source_matches = state.get("knowledge_fingerprint") == knowledge_fingerprint(record)
        snapshot = state.get("profile")
        snapshot_reproducible = False
        if isinstance(snapshot, dict):
            try:
                restored = ExportProfile.from_dict(snapshot)
                snapshot_reproducible = bool(
                    state.get("profile_fingerprint")
                    and restored.fingerprint() == state.get("profile_fingerprint")
                )
            except Exception:
                snapshot_reproducible = False
        exact_content = current_text == expected
        current = bool(
            exact_content
            and verified
            and profile_matches
            and source_matches
            and snapshot_reproducible
        )
        if current:
            return TrainingExportHealth(
                **common,
                state="current",
                label="Current and Verified",
                detail=(
                    f"The Training TXT matches {selected.name} v{selected.version}, "
                    "its exact profile snapshot, current Image Knowledge, and read-back fingerprint."
                ),
                suggested_action="No Training TXT action is needed.",
                readable=True,
                verified=True,
                reproducible=True,
                current=True,
            )

        if not exact_content:
            state_name = "stale"
            label = "Stale or Changed"
            detail = (
                f"The adjacent Training TXT does not match the output currently derived "
                f"by {selected.name} v{selected.version}."
            )
            code = "stale_training_export"
        elif not profile_matches:
            state_name = "different_profile"
            label = "Different Profile"
            detail = (
                "The TXT content happens to match, but its recorded profile identity "
                f"is not {selected.profile_id} v{selected.version}."
            )
            code = "training_profile_mismatch"
        elif not source_matches:
            state_name = "stale"
            label = "Stale"
            detail = (
                "The TXT content matches, but its recorded Image Knowledge fingerprint "
                "does not describe the current authoritative record."
            )
            code = "stale_training_export"
        elif not snapshot_reproducible:
            state_name = "unreproducible"
            label = "Profile Snapshot Unverified"
            detail = (
                "The TXT content matches, but its saved profile snapshot cannot reproduce "
                "the recorded profile fingerprint."
            )
            code = "unreproducible_training_export"
        else:
            state_name = "unverified"
            label = "Read-back Unverified"
            detail = (
                "The TXT content and profile match, but no current verified read-back "
                "fingerprint proves the adjacent file is the recorded output."
            )
            code = "unverified_training_export"
        action = (
            f"Preview and export Training TXT with {selected.name} v{selected.version} "
            "to record current reproducible output."
        )
        issue = OutputHealthIssue(code, detail, "medium", action)
        return TrainingExportHealth(
            **common,
            state=state_name,
            label=label,
            detail=detail,
            suggested_action=action,
            readable=True,
            verified=verified,
            reproducible=snapshot_reproducible,
            current=False,
            issues=(issue,),
        )

    def assess_metadata_backup(
        self,
        record: ImageRecord,
        *,
        include_metadata: bool = True,
    ) -> MetadataBackupHealth:
        path = record.image_path
        container = path.suffix.lstrip(".").upper() or "IMAGE"
        present = TagCollection(record.annotation.all_structured_tags()).to_list()
        absent = TagCollection(record.annotation.confirmed_absent).normalized_set()
        expected = [tag for tag in present if normalize_tag(tag) not in absent]
        common = {
            "image": path,
            "container": container,
            "expected_tags": len(expected),
        }
        if not include_metadata:
            return MetadataBackupHealth(
                **common,
                state="not_assessed",
                label="Not Assessed",
                detail=(
                    "Metadata was not read for this bounded assessment. "
                    "Image Knowledge completeness is unchanged."
                ),
                suggested_action="Run the explicit Metadata Backup Health check when needed.",
            )
        if not expected:
            return MetadataBackupHealth(
                **common,
                state="not_needed",
                label="No Tag Backup Needed",
                detail="This image has no confirmed SDXL Tags to back up.",
                suggested_action="No Metadata tag-backup action is needed.",
                supported=True,
                readable=True,
                complete=True,
                current=True,
            )

        metadata = self.metadata or MetadataSyncService()
        if not bool(getattr(metadata, "is_available", lambda: True)()):
            action = "Set up Metadata support before attempting a verified tag backup."
            issue = OutputHealthIssue(
                "metadata_backup_unavailable",
                "Metadata support is unavailable",
                "low",
                action,
            )
            return MetadataBackupHealth(
                **common,
                state="unavailable",
                label="Unavailable",
                detail=(
                    f"{container} Metadata could not be assessed because Metadata support "
                    "is unavailable. Image Knowledge completeness is unchanged."
                ),
                suggested_action=action,
                issues=(issue,),
            )
        try:
            values = metadata.read_embedded(path)
        except Exception as exc:
            action = "Repair Metadata support or leave the authoritative Image Knowledge unchanged."
            issue = OutputHealthIssue(
                "metadata_backup_unreadable",
                f"Metadata could not be read: {exc}",
                "medium",
                action,
            )
            return MetadataBackupHealth(
                **common,
                state="unreadable",
                label="Unreadable",
                detail=str(exc),
                suggested_action=action,
                supported=True,
                issues=(issue,),
            )

        actual = TagCollection(_managed_xmp_subject(values)).to_list()
        expected_by_key = {normalize_tag(value): value for value in expected}
        actual_by_key = {normalize_tag(value): value for value in actual}
        missing = [value for key, value in expected_by_key.items() if key not in actual_by_key]
        extra = [value for key, value in actual_by_key.items() if key not in expected_by_key]
        complete = not missing and not extra
        state = record.annotation.projection_state.get("metadata", {})
        state = state if isinstance(state, dict) else {}
        verified = bool(
            state.get("status") == "verified"
            and state.get("projection_fingerprint") == _fingerprint(path)
        )
        common.update(
            {
                "backed_up_tags": len(actual),
                "missing_tags": len(missing),
                "extra_tags": len(extra),
                "supported": True,
                "readable": True,
                "complete": complete,
                "verified": verified,
            }
        )
        if complete and verified:
            return MetadataBackupHealth(
                **common,
                state="current",
                label="Complete and Verified",
                detail=(
                    f"The managed {container} Metadata tag field contains all "
                    f"{len(expected):,} confirmed SDXL Tags and has a current read-back fingerprint."
                ),
                suggested_action="No Metadata tag-backup action is needed.",
                current=True,
            )
        if complete:
            action = "Preview and verify Back Up Tags to Metadata before relying on this recovery copy."
            issue = OutputHealthIssue(
                "metadata_backup_unverified",
                "The complete managed tag field has no current read-back verification",
                "low",
                action,
            )
            return MetadataBackupHealth(
                **common,
                state="unverified",
                label="Complete but Unverified",
                detail=issue.label + ".",
                suggested_action=action,
                issues=(issue,),
            )
        action = "Preview Back Up Tags to Metadata and verify the managed field after writing."
        if not actual:
            state_name = "missing"
            label = "Missing"
            detail = f"No managed {container} Metadata tag backup was found."
            code = "missing_metadata_backup"
        else:
            state_name = "partial"
            label = "Partial or Stale"
            detail = (
                f"The managed {container} Metadata field is missing {len(missing):,} "
                f"confirmed tag(s) and contains {len(extra):,} extra tag(s)."
            )
            code = "partial_metadata_backup"
        issue = OutputHealthIssue(code, detail, "medium", action)
        return MetadataBackupHealth(
            **common,
            state=state_name,
            label=label,
            detail=detail,
            suggested_action=action,
            issues=(issue,),
        )
