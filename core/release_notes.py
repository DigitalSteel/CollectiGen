from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cmp_to_key
from pathlib import Path

from release_contract import ReleaseContractError,SemVer


_RELEASE_HEADING = re.compile(
    r"^## v(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?) — "
    r"(?P<date>\d{4}-\d{2}-\d{2})$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ReleaseNoteEntry:
    version: str
    released_at: str
    notes: str

    @property
    def label(self) -> str:
        return f"v{self.version} — {self.released_at}" if self.released_at else f"v{self.version}"


def canonical_release_notes_path(app_root: str | Path, version: str) -> Path:
    """Return the packaged canonical release-notes path for an application version."""
    try:
        SemVer.parse(version)
    except ReleaseContractError as exc:
        raise ValueError("Release notes require a semantic application version.") from exc
    return Path(app_root) / "docs" / "releases" / f"v{version}.md"


def load_release_notes(app_root: str | Path, version: str) -> str:
    """Load the offline canonical notes for the installed application version."""
    path = canonical_release_notes_path(app_root, version)
    try:
        notes = path.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        raise FileNotFoundError(f"Release notes for CollectiGen v{version} are unavailable.") from exc
    if not notes:
        raise ValueError(f"Release notes for CollectiGen v{version} are empty.")
    return notes


def release_dates(app_root: str | Path) -> dict[str, str]:
    """Load version dates from the canonical changelog without exposing its bulk UI."""
    path=Path(app_root)/"CHANGELOG.md"
    try:
        changelog=path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise FileNotFoundError("The installed CollectiGen release history is unavailable.") from exc
    return {
        match.group("version"):match.group("date")
        for match in _RELEASE_HEADING.finditer(changelog)
    }


def load_release_history(
    app_root: str | Path,
    current_version: str,
    *,
    limit: int = 12,
) -> tuple[ReleaseNoteEntry, ...]:
    """Load a bounded, newest-first offline release history through the installed version."""
    try:
        current_key=SemVer.parse(current_version)
    except ReleaseContractError as exc:
        raise ValueError("Release history requires a semantic application version.") from exc
    if limit < 1 or limit > 50:
        raise ValueError("Release history limit must be between 1 and 50.")
    root=Path(app_root)
    versions=[]
    for path in (root/"docs"/"releases").glob("v*.md"):
        match=re.fullmatch(
            r"v(\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)\.md",
            path.name,
        )
        if not match:
            continue
        version=match.group(1)
        try:
            key=SemVer.parse(version)
        except ReleaseContractError:
            continue
        if key.compare(current_key)<=0:
            versions.append((key,version))
    versions.sort(key=cmp_to_key(lambda left,right: left[0].compare(right[0])),reverse=True)
    if current_version not in {version for _,version in versions}:
        load_release_notes(root,current_version)
        versions.insert(0,(current_key,current_version))
    dates=release_dates(root)
    entries=[]
    for _,version in versions[:limit]:
        entries.append(ReleaseNoteEntry(
            version=version,
            released_at=dates.get(version,""),
            notes=load_release_notes(root,version),
        ))
    return tuple(entries)
