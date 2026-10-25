from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .image_record import ImageRecord
from .tag_collection import TagCollection, normalize_tag
from .tag_database import MergeRule


@dataclass
class MergeSuggestion:
    record: ImageRecord
    rule: MergeRule
    location: str
    before_tags: list[str]

    @property
    def image_path(self):
        return self.record.image_path

    @property
    def source_tags(self) -> list[str]:
        return list(self.rule.source_tags)

    @property
    def target_tag(self) -> str:
        return self.rule.target_tag

    def preview_tags(self) -> list[str]:
        return merged_tags(self.before_tags, self.source_tags, self.target_tag)


def merged_tags(values: Iterable[str], source_tags: Iterable[str], target_tag: str) -> list[str]:
    sources = {normalize_tag(tag) for tag in source_tags}
    result = TagCollection(tag for tag in values if normalize_tag(tag) not in sources)
    result.add(target_tag)
    return result.to_list()


def _iter_locations(record: ImageRecord):
    annotation = record.annotation
    for group, values in annotation.global_tags.items():
        yield f"global.{group}", values, lambda new, g=group: annotation.global_tags.__setitem__(g, new)
    for group, values in annotation.scene.items():
        yield f"scene.{group}", values, lambda new, g=group: annotation.scene.__setitem__(g, new)
    for character in annotation.characters:
        for group, values in character.tags.items():
            yield (
                f"character.{character.slot}.{group}",
                values,
                lambda new, c=character, g=group: c.tags.__setitem__(g, new),
            )
    for group, values in annotation.custom_groups.items():
        yield f"custom.{group}", values, lambda new, g=group: annotation.custom_groups.__setitem__(g, new)
    yield "sdxl_tags", annotation.sdxl_tags, lambda new: setattr(annotation, "sdxl_tags", new)
    yield "uncategorized", annotation.uncategorized, lambda new: setattr(annotation, "uncategorized", new)


def find_suggestions(record: ImageRecord, rules: Iterable[MergeRule]) -> list[MergeSuggestion]:
    suggestions: list[MergeSuggestion] = []
    for location, values, _setter in _iter_locations(record):
        for rule in rules:
            if rule.matches(values):
                suggestions.append(MergeSuggestion(record, rule, location, list(values)))
    return suggestions


def apply_suggestion(suggestion: MergeSuggestion) -> bool:
    raise RuntimeError(
        "Merge suggestions are training-projection previews and cannot remove "
        "confirmed Image Knowledge."
    )


@dataclass(frozen=True)
class MergeBatchItem:
    image_path: Path
    rule_id: str
    location: str
    task_id: str = ""


@dataclass
class MergeBatchResult:
    applied: int = 0
    unchanged: int = 0
    images_changed: int = 0
    backup_directory: Path | None = None
    recovery_manifest: Path | None = None
    resolutions: dict[str, dict] = field(default_factory=dict)


class MergeBatchApplyService:
    """Restore historical merge batches; new confirmed-data merges are blocked."""

    def __init__(self, knowledge, journal=None) -> None:
        self.knowledge = knowledge
        self.journal = journal

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def restore(manifest_path: str | Path) -> None:
        manifest = Path(manifest_path)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        for item in payload.get("files", []):
            original = Path(item["original"])
            if item.get("existed"):
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(Path(item["backup"]), original)
            elif original.exists():
                original.unlink()

    def apply(
        self,
        items: Iterable[MergeBatchItem],
        *,
        export_txt: bool = True,
        backup_root: str | Path | None = None,
    ) -> MergeBatchResult:
        unique: list[MergeBatchItem] = []
        seen: set[tuple[str, str, str]] = set()
        for item in items:
            normalized = MergeBatchItem(Path(item.image_path), str(item.rule_id), str(item.location), str(item.task_id))
            key = (str(normalized.image_path.resolve()), normalized.rule_id, normalized.location)
            if key not in seen:
                seen.add(key); unique.append(normalized)

        result = MergeBatchResult()
        if not unique:
            return result
        raise RuntimeError(
            "Merge rules now belong to training projections and cannot be "
            "applied to confirmed Image Knowledge."
        )
