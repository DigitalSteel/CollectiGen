from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .image_record import ImageRecord
from .tag_collection import normalize_tag
from .tag_database import MergeRule


RATINGS = {"general", "sensitive", "questionable", "explicit"}
RATING_ALIASES = {
    "general": {"general", "safe"},
    "sensitive": {"sensitive"},
    "questionable": {"questionable"},
    "explicit": {"explicit"},
}
STYLE_ALIASES = {
    "photorealistic": {"photorealistic", "photo realistic"},
    "realistic": {"realistic"},
    "semi-realistic": {"semi-realistic", "semi realistic", "semirealistic"},
}


@dataclass(frozen=True)
class FolderContext:
    rating: str | None = None
    style: str | None = None
    series: str | None = None
    character: str | None = None


@dataclass(frozen=True)
class FolderAuditIssue:
    code: str
    message: str
    severity: str = "review"


def folder_context(image: Path, root: Path) -> FolderContext:
    try:
        parts = list(image.resolve().relative_to(root.resolve()).parts[:-1])
    except ValueError:
        return FolderContext()
    lowered = [part.casefold() for part in parts]
    rating = next((part for part in lowered if part in RATINGS), None)
    style = next((part for part in lowered if part in STYLE_ALIASES), None)
    series = character = None
    try:
        marker = lowered.index("specific character")
        remainder = parts[marker + 1:]
        if remainder:
            series = remainder[0]
        if len(remainder) >= 2:
            character = remainder[-1]
    except ValueError:
        pass
    return FolderContext(rating, style, series, character)


def _matches_name(tags: set[str], name: str) -> bool:
    expected = normalize_tag(name)
    return any(
        tag == expected
        or tag.startswith(expected + " (")
        or tag.endswith(" (" + expected + ")")
        for tag in tags
    )


class FolderAuditAnalyzer:
    def __init__(self, root: Path, merge_rules: Iterable[MergeRule] = ()) -> None:
        self.root = Path(root).resolve()
        self.merge_rules = list(merge_rules)

    def audit(self, image: Path) -> list[FolderAuditIssue]:
        context = folder_context(image, self.root)
        issues: list[FolderAuditIssue] = []
        if not image.with_suffix(".txt").exists():
            issues.append(
                FolderAuditIssue("missing_caption", "No TXT caption exists for this image.")
            )
        try:
            known_tags=set().union(*RATING_ALIASES.values(),*STYLE_ALIASES.values())
            for rule in self.merge_rules:
                known_tags.update(rule.source_tags); known_tags.add(rule.target_tag)
            record = ImageRecord(image,known_tags=known_tags)
            tags = {normalize_tag(value) for value in record.annotation.all_structured_tags()}
        except Exception as exc:
            return issues + [
                FolderAuditIssue("unreadable_annotation", f"Caption data could not be read: {exc}", "error")
            ]

        if context.rating:
            present_ratings = {
                rating
                for rating, aliases in RATING_ALIASES.items()
                if tags.intersection(aliases)
            }
            if present_ratings and context.rating not in present_ratings:
                issues.append(
                    FolderAuditIssue(
                        "rating_mismatch",
                        f"Folder rating is {context.rating}, but tags indicate "
                        f"{', '.join(sorted(present_ratings))}.",
                    )
                )

        if context.style:
            expected_styles = STYLE_ALIASES[context.style]
            if not tags.intersection(expected_styles):
                issues.append(
                    FolderAuditIssue(
                        "missing_style",
                        f'Folder expects art style "{context.style}", but no matching style tag was found.',
                    )
                )

        if context.character:
            expected_characters = [
                value.strip() for value in context.character.split(" and ") if value.strip()
            ]
            missing = [name for name in expected_characters if not _matches_name(tags, name)]
            if missing:
                issues.append(
                    FolderAuditIssue(
                        "missing_character",
                        "Folder expects character tag(s): " + ", ".join(missing) + ".",
                    )
                )

        return issues
