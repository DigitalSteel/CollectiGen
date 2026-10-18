from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from .image_authority_initialization import ImageAuthorityInitializationIssue


_ISSUE_LABELS = {
    "invalid_sidecar": "Invalid Image Knowledge",
    "image_unavailable": "Unavailable incoming image",
    "metadata_unavailable": "Metadata could not be read",
    "metadata_stale": "Metadata needs a deliberate refresh",
    "concurrent_sidecar": "Image Knowledge changed during the refresh",
    "initialization_failed": "Image Knowledge could not be initialized",
    "confirmed_conflict": "Confirmed image facts conflict",
    "candidate_conflict": "Imported evidence conflicts",
    "automatic_ai": "Automatic AI analysis could not finish",
}

_INGEST_RECOVERY = {
    "invalid_sidecar": (
        "Repair or restore the existing .tags.json, then choose Recheck Ingest Folder."
    ),
    "image_unavailable": (
        "Restore access to the image, then choose Recheck Ingest Folder."
    ),
    "metadata_unavailable": (
        "Restore ExifTool or image access, then choose Recheck Ingest Folder."
    ),
    "concurrent_sidecar": (
        "Wait for the other file activity to finish, then choose Recheck Ingest Folder."
    ),
    "initialization_failed": (
        "Restore write access or wait for other file activity to finish, then choose "
        "Recheck Ingest Folder."
    ),
}


@dataclass(frozen=True)
class IngestAttentionItem:
    image: Path | None
    code: str
    message: str
    recovery: str
    blocking: bool = True


@dataclass(frozen=True)
class IngestAttentionGroup:
    code: str
    label: str
    recovery: str
    items: tuple[IngestAttentionItem, ...]


@dataclass(frozen=True)
class IngestAttentionReport:
    root: Path
    items: tuple[IngestAttentionItem, ...] = ()

    @classmethod
    def build(
        cls,
        root: str | Path,
        issues: Iterable[ImageAuthorityInitializationIssue] = (),
        *,
        automatic_errors: Iterable[str] = (),
    ) -> "IngestAttentionReport":
        items = [
            IngestAttentionItem(
                Path(issue.image),
                str(issue.code or "initialization_failed"),
                str(issue.message or "The incoming image could not be prepared."),
                _INGEST_RECOVERY.get(
                    str(issue.code or ""),
                    str(
                        issue.recovery
                        or "Restore access, then choose Recheck Ingest Folder."
                    ),
                ),
                bool(issue.blocking),
            )
            for issue in issues
        ]
        items.extend(
            IngestAttentionItem(
                None,
                "automatic_ai",
                str(message),
                "Review optional AI components in Setup, then use Recheck Data when ready.",
                False,
            )
            for message in automatic_errors
            if str(message).strip()
        )
        report = cls(Path(root), tuple(items))
        prepared = cls(
            report.root, tuple(sorted(report.items, key=report._sort_key))
        )
        return prepared.prepare()

    def prepare(self) -> "IngestAttentionReport":
        # Managed Ingest builds reports in its worker. Materialize every
        # immutable lookup there so publishing the result never transfers
        # whole-report grouping or path work to the UI thread.
        _=(
            self.fingerprint,
            self.affected_images,
            self.image_paths,
            self.groups,
            self._display_name_index,
            self._item_path_index,
        )
        return self

    @cached_property
    def _absolute_root(self) -> Path:
        return Path(os.path.abspath(os.fspath(self.root)))

    def _relative_identity(self, image: Path | None) -> str:
        if image is None:
            return ""
        try:
            relative = Path(
                os.path.abspath(os.fspath(image))
            ).relative_to(self._absolute_root)
        except ValueError:
            relative = Path(image.name)
        return relative.as_posix()

    @staticmethod
    def _absolute_key(image: str | Path) -> str:
        return os.path.normcase(os.path.abspath(os.fspath(image)))

    def _sort_key(self, item: IngestAttentionItem) -> tuple[str, str, str]:
        return (
            self._relative_identity(item.image).casefold(),
            item.code.casefold(),
            item.message.casefold(),
        )

    @cached_property
    def fingerprint(self) -> str:
        payload = [
            {
                "image": self._relative_identity(item.image),
                "code": item.code,
                "message": item.message,
                "recovery": item.recovery,
                "blocking": item.blocking,
            }
            for item in self.items
        ]
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest() if payload else ""

    @cached_property
    def affected_images(self) -> int:
        return len(
            {
                self._relative_identity(item.image).casefold()
                for item in self.items
                if item.image is not None
            }
        )

    @cached_property
    def image_paths(self) -> tuple[Path, ...]:
        paths: dict[str, Path] = {}
        for item in self.items:
            if item.image is None:
                continue
            paths.setdefault(self._absolute_key(item.image),item.image)
        return tuple(paths.values())

    @cached_property
    def groups(self) -> tuple[IngestAttentionGroup, ...]:
        grouped: dict[tuple[str, str], list[IngestAttentionItem]] = {}
        for item in self.items:
            # Parser details may differ by line or token, but the code and safe
            # recovery action define one user decision.
            grouped.setdefault((item.code, item.recovery), []).append(item)
        groups=[
            IngestAttentionGroup(
                code,
                _ISSUE_LABELS.get(code, code.replace("_", " ").title()),
                recovery,
                tuple(values),
            )
            for (code, recovery), values in sorted(
                grouped.items(), key=lambda entry: (entry[0][0], entry[0][1])
            )
        ]
        groups.sort(
            key=lambda group:(
                not any(item.blocking for item in group.items),
                group.code,
                group.recovery,
            )
        )
        return tuple(groups)

    @cached_property
    def _display_name_index(self) -> Mapping[str, str]:
        relative_paths = {
            self._relative_identity(item.image)
            for item in self.items
            if item.image is not None
        }
        by_name: dict[str, list[str]] = {}
        for value in relative_paths:
            by_name.setdefault(Path(value).name.casefold(), []).append(value)
        names: dict[str, str] = {}
        for values in by_name.values():
            if len(values) == 1:
                names[values[0]] = Path(values[0]).name
                continue
            parts_by_value = {value: Path(value).parts for value in values}
            suffix_counts = Counter(
                tuple(part.casefold() for part in parts[-depth:])
                for parts in parts_by_value.values()
                for depth in range(2, len(parts) + 1)
            )
            for value, parts in parts_by_value.items():
                display = Path(value).as_posix()
                folded = tuple(part.casefold() for part in parts)
                for depth in range(2, len(parts) + 1):
                    suffix = "/".join(parts[-depth:])
                    if suffix_counts[folded[-depth:]] == 1:
                        display = suffix
                        break
                names[value] = display
        return MappingProxyType(names)

    def display_names(self) -> Mapping[str, str]:
        return self._display_name_index

    def display_name_for(self, image: str | Path) -> str:
        identity=self._relative_identity(Path(image))
        return self._display_name_index.get(identity,Path(identity).name)

    @cached_property
    def _item_path_index(
        self,
    ) -> Mapping[str, tuple[IngestAttentionItem, ...]]:
        grouped: dict[str, list[IngestAttentionItem]] = {}
        for item in self.items:
            if item.image is None:
                continue
            key=self._absolute_key(item.image)
            grouped.setdefault(key,[]).append(item)
        return MappingProxyType(
            {key:tuple(values) for key,values in grouped.items()}
        )

    def items_for(
        self,image: str | Path,
    ) -> tuple[IngestAttentionItem, ...]:
        key=self._absolute_key(image)
        return self._item_path_index.get(key,())

    def merged(self, other: "IngestAttentionReport") -> "IngestAttentionReport":
        combined=[]
        seen=set()
        for item in (*self.items,*other.items):
            key=(
                self._relative_identity(item.image).casefold(),
                item.code,item.message,item.recovery,item.blocking,
            )
            if key in seen:
                continue
            seen.add(key)
            combined.append(item)
        report=IngestAttentionReport(self.root,tuple(combined))
        return IngestAttentionReport(
            report.root,tuple(sorted(report.items,key=report._sort_key)),
        )

    def short_summary(self) -> str:
        image_count = self.affected_images
        other_count = sum(1 for item in self.items if item.image is None)
        subjects = []
        if image_count:
            subjects.append(
                f"{image_count:,} incoming image{'s' if image_count != 1 else ''}"
            )
        if other_count:
            subjects.append(
                f"{other_count:,} automatic task{'s' if other_count != 1 else ''}"
            )
        subject = " and ".join(subjects) or "No incoming images"
        group_count = len(self.groups)
        verb="needs" if image_count+other_count==1 else "need"
        return (
            f"{subject} {verb} attention across {group_count:,} issue "
            f"group{'s' if group_count != 1 else ''}. Affected records and raw evidence were preserved."
        )

    def detailed_summary(
        self, *, max_groups: int = 4, max_examples: int = 4
    ) -> str:
        if not self.items:
            return "No Ingest issues remain."
        names = self.display_names()
        lines = [self.short_summary()]
        shown_groups = self.groups[: max(1, int(max_groups))]
        for group in shown_groups:
            group_paths = []
            seen = set()
            for item in group.items:
                if item.image is None:
                    continue
                identity = self._relative_identity(item.image)
                if identity.casefold() in seen:
                    continue
                seen.add(identity.casefold())
                group_paths.append(names.get(identity, Path(identity).name))
            examples = group_paths[: max(1, int(max_examples))]
            hidden = max(0, len(group_paths) - len(examples))
            line = f"{group.label}: {len(group.items):,}"
            if examples:
                line += f". Examples: {', '.join(examples)}"
            if hidden:
                line += f". {hidden:,} more not shown"
            line += f". Next: {group.recovery}"
            lines.append(line)
        hidden_groups = self.groups[len(shown_groups) :]
        if hidden_groups:
            hidden_items = sum(len(group.items) for group in hidden_groups)
            lines.append(
                f"{hidden_items:,} more issue(s) in {len(hidden_groups):,} additional "
                f"group(s) are not shown."
            )
        return "\n".join(lines)
