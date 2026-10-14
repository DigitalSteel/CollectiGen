"""Offline component Help projection; canonical public notes remain host-owned."""
from dataclasses import dataclass
import json
from pathlib import Path

from core.release_notes import ReleaseNoteEntry
from release_contract import SemVer
from suite_core.encyclopedia.version import SOFTWARE_VERSION, DISPLAY_VERSION


@dataclass(frozen=True)
class EncyclopediaHelpContent:
    display_version: str
    software_version: str
    history: tuple[ReleaseNoteEntry, ...]
    notes_reference: Path
    public_notes: str

    @property
    def about(self):
        return f'Tag Encyclopedia {self.display_version}\nSoftware version: {self.software_version}'

    @property
    def notes_text(self):
        return self.public_notes or 'Public release notes are unavailable in this package. The dated version history remains available offline.'


def load_encyclopedia_help(component_root, *, public_notes=''):
    """Read only packaged history, never runtime/user/installation state.

    History's internal scope/status/testing fields are deliberately not projected.
    The host supplies current public prose from canonical WHATS_NEW.md after its
    editorial review; this adapter neither copies nor invents a release history.
    Empty public prose is an honest unavailable state, not raw developer notes.
    """
    root = Path(component_root)
    history_path = root / 'RELEASE_HISTORY.json'
    if history_path.stat().st_size > 256 * 1024:
        raise ValueError('bounded_packaged_release_history_required')
    raw = history_path.read_bytes()
    if len(raw) > 256 * 1024:
        raise ValueError('bounded_packaged_release_history_required')
    rows = json.loads(raw)['releases']
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError('bounded_packaged_release_history_required')
    current = SemVer.parse(SOFTWARE_VERSION)
    entries, seen = [], set()
    for row in rows:
        version = row['software_version']
        parsed = SemVer.parse(version)
        if version in seen:
            raise ValueError('duplicate_packaged_release_version')
        seen.add(version)
        if parsed.compare(current) > 0:
            continue
        entries.append(ReleaseNoteEntry(version, row['date'], ''))
    # SemVer ordering remains the source authority, including same-day releases.
    from functools import cmp_to_key
    entries.sort(key=cmp_to_key(lambda a, b: SemVer.parse(b.version).compare(SemVer.parse(a.version))))
    if not isinstance(public_notes, str) or len(public_notes) > 256 * 1024:
        raise ValueError('bounded_public_notes_required')
    return EncyclopediaHelpContent(DISPLAY_VERSION, SOFTWARE_VERSION, tuple(entries),
        root / 'WHATS_NEW.md', public_notes)
