from __future__ import annotations

"""Read-only, scoped consequences for draft preparation choices.

This module deliberately describes only current, deterministic preferred-caption
outcomes.  AI Tag and Description generation is future work: the impact lists
the requested step but never claims to know a model result before it runs.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .preparation_workflow import PreparationOptions


_NOT_ASSESSED_DETAIL = (
    "Current Description impact was not assessed because Fill or upgrade "
    "Descriptions is not selected."
)
_RETAINED_FACTS_STEP = (
    "Confirmed Image Knowledge and raw source/AI evidence are retained; "
    "this impact check does not write files."
)


@dataclass(frozen=True)
class PreparationImpactItem:
    image: Path
    status: str
    detail: str


@dataclass(frozen=True)
class PreparationPolicyImpact:
    images: tuple[Path, ...]
    steps: tuple[str, ...]
    items: tuple[PreparationImpactItem, ...]
    cancelled: bool = False

    def count(self, status: str) -> int:
        return sum(item.status == status for item in self.items)

    def summary(self) -> str:
        if self.cancelled:
            return "Policy impact check cancelled; no partial outcome is shown."
        counts = ", ".join(
            f"{self.count(status):,} {status}"
            for status in (
                "eligible", "review", "unchanged", "unavailable", "not_assessed",
            )
            if self.count(status)
        )
        return counts or "No images are in scope."


class PreparationPolicyImpactService:
    """Describe one frozen scope without saving, applying, or running AI."""

    def __init__(
        self,
        preferred_service=None,
        availability: Callable[[Path], str] | None = None,
    ) -> None:
        self.preferred_service = preferred_service
        self.availability = availability or (lambda _path: "")

    def preview(
        self,
        images: Iterable[str | Path],
        options: PreparationOptions,
        *,
        progress: Callable[[int, int, str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> PreparationPolicyImpact:
        paths = tuple(dict.fromkeys(Path(image) for image in images))
        should_cancel = should_cancel or (lambda: False)
        steps = self._steps(options)
        items: list[PreparationImpactItem] = []

        for index, path in enumerate(paths, 1):
            if should_cancel():
                return self._cancelled(paths, steps)
            if not options.prepares_preferred_descriptions:
                item = PreparationImpactItem(path, "not_assessed", _NOT_ASSESSED_DETAIL)
            else:
                item = self._preferred_item(path, should_cancel)
                if item is None:
                    return self._cancelled(paths, steps)
            items.append(item)
            if progress is not None and (index==1 or index%64==0 or index==len(paths)):
                progress(index, len(paths), path.name)
            if should_cancel():
                return self._cancelled(paths, steps)

        return PreparationPolicyImpact(paths, steps, tuple(items))

    @staticmethod
    def _cancelled(
        paths: tuple[Path, ...], steps: tuple[str, ...],
    ) -> PreparationPolicyImpact:
        # Never publish a mix of completed and absent preview outcomes.
        return PreparationPolicyImpact(paths, steps, (), cancelled=True)

    def _preferred_item(
        self,
        path: Path,
        should_cancel: Callable[[], bool],
    ) -> PreparationImpactItem | None:
        reason = str(self.availability(path) or "").strip()
        if reason:
            return PreparationImpactItem(path, "unavailable", reason)
        if should_cancel():
            return None
        if self.preferred_service is None:
            return PreparationImpactItem(
                path,
                "unavailable",
                "No preferred Description preparation service is available.",
            )
        before = self._input_stat_signature(path)
        preview = self.preferred_service.preview(
            (path,), should_cancel=should_cancel,
        )
        if should_cancel() or bool(getattr(preview, "cancelled", False)):
            return None
        if before != self._input_stat_signature(path):
            return PreparationImpactItem(
                path,
                "unavailable",
                "The image, Image Knowledge, or current TXT changed while its impact was being checked. Check again.",
            )
        preview_items = tuple(getattr(preview, "items", ()))
        if len(preview_items) != 1:
            return PreparationImpactItem(
                path,
                "unavailable",
                "Current Description impact could not be checked.",
            )
        source = preview_items[0]
        status = str(getattr(source, "status", "failed"))
        detail = str(
            getattr(source, "error", "") or getattr(source, "detail", "")
        ).strip()
        if status in {"fill_empty", "upgrade_prior"}:
            return PreparationImpactItem(
                path,
                "eligible",
                "Would fill or upgrade the current preferred Description if it remains current."
                +(f" {detail}" if detail else ""),
            )
        if status == "already_preferred":
            return PreparationImpactItem(
                path,
                "unchanged",
                "Current confirmed Descriptions already match the preferred result."
                +(f" {detail}" if detail else ""),
            )
        if status == "needs_review":
            return PreparationImpactItem(
                path,
                "review",
                "Current Description evidence needs review before automatic preparation."
                +(f" {detail}" if detail else ""),
            )
        return PreparationImpactItem(
            path,
            "unavailable",
            detail or "Current Description impact cannot run for this image.",
        )

    @staticmethod
    def _input_stat_signature(path: Path) -> tuple[tuple[str, int, int] | None, ...]:
        """Cheap drift check around a preview that already hashes its inputs."""
        signatures = []
        for candidate in (path, path.with_suffix(".tags.json"), path.with_suffix(".txt")):
            try:
                stat = candidate.stat()
            except OSError:
                signatures.append(None)
            else:
                signatures.append((str(candidate), int(stat.st_size), int(stat.st_mtime_ns)))
        return tuple(signatures)

    @staticmethod
    def _steps(options: PreparationOptions) -> tuple[str, ...]:
        steps: list[str] = []
        if options.read_external_evidence:
            steps.append(
                "Read current TXT/Metadata as attributed, reviewable evidence."
            )
        if options.suggest_sdxl_tags:
            steps.append(
                "Request AI Tag analysis; future generated Tags remain unknown until the run."
            )
            if options.confirms_safe_tags:
                votes = options.tag_agreement_requirement()
                external = (
                    " Matching typed TXT/Metadata may add one vote only with a current AI Tag result."
                    if options.uses_external_evidence_vote else
                    " TXT/Metadata alone cannot confirm a Tag."
                )
                steps.append(
                    f"Confirm only safe current AI Tags with at least {votes} independent vote(s)."
                    + external
                )
        if options.suggest_plain_language:
            steps.append(
                "Request AI Description analysis; future generated Descriptions remain unknown until the run."
            )
        if options.prepares_preferred_descriptions:
            steps.append(
                "Check current preferred Description evidence for fill, upgrade, review, or no-change outcomes."
            )
        if options.suggest_sdxl_tags or options.suggest_plain_language:
            steps.append(
                "Force all AI scans." if options.force_all_ai else
                f"Reuse matching AI evidence up to {options.skip_ai_value} {options.skip_ai_unit}."
            )
        if not steps:
            steps.append("No preparation steps are selected.")
        steps.append(_RETAINED_FACTS_STEP)
        return tuple(steps)
