"""Bounded, IO-free review of existing model-folder authorities.

These are recorded preferences, not a filesystem inventory. Only the existing
explicit ModelWorkspace preview may inspect a selected folder's contents.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
import ntpath
import re

from .swarm_model_paths import ModelPathInventory, ROLE_FIELDS

MAX_ORIGINS = 512
MAX_PATH = 4096
PAGE_SIZE = 50
SWARM_LABELS = dict(zip(ROLE_FIELDS, (
    "Checkpoints", "LoRAs", "VAEs", "Embeddings", "ControlNets", "Text Encoders", "Vision Encoders",
)))


@dataclass(frozen=True)
class FolderOrigin:
    label: str
    recorded_path: str
    hint: str = ""
    swarm: bool = False


@dataclass(frozen=True)
class KnownModelFolder:
    path: str
    origins: tuple[FolderOrigin, ...]
    issue: str = ""

    @property
    def actionable(self):
        return not self.issue

    @property
    def uses_swarm(self):
        return any(origin.swarm for origin in self.origins)

    def detail(self):
        lines = [self.path, self.issue or "Recorded folder only; availability and contents have not been checked."]
        lines.extend(f"{origin.label}: {origin.recorded_path}" + (f" · Hint: {origin.hint}" if origin.hint else "")
                     for origin in self.origins)
        if len({origin.hint for origin in self.origins if origin.hint}) > 1:
            lines.append("Different role/family hints share this folder. Review the files and declare their role/family explicitly.")
        lines.append("Folder hints do not establish model identity, compatibility or permission to register.")
        return "\n".join(lines)


@dataclass(frozen=True)
class KnownFolderReview:
    candidates: tuple[KnownModelFolder, ...]
    warnings: tuple[str, ...]

    def page(self, page=0):
        start = max(0, int(page)) * PAGE_SIZE
        return self.candidates[start:start + PAGE_SIZE]


def _scalar(value):
    if not isinstance(value, str):
        return f"<unsupported {type(value).__name__} value>"
    return value if len(value) <= MAX_PATH else value[:MAX_PATH] + "… [overlong recorded path]"


def _absolute_folder(value):
    """Normalize Windows spelling lexically; never expand, resolve or stat."""
    if not isinstance(value, str) or not value or len(value) > MAX_PATH:
        return "", "Missing, overlong or unsupported folder value; choose a folder explicitly."
    if (value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(char in value for char in ';%$*?{}<>|"') or value.startswith(("\\\\?\\", "\\\\.\\"))):
        return "", "Unsupported path expression or multiple folders; choose one folder explicitly."
    drive, tail = ntpath.splitdrive(value)
    if not drive or not ntpath.isabs(value) or (not drive.startswith(("\\\\", "//")) and not re.fullmatch("[A-Za-z]:", drive)):
        return "", "Relative or incomplete folder; choose an absolute folder explicitly."
    if drive.startswith(("\\\\", "//")):
        network_parts = drive.replace("/", "\\")[2:].split("\\")
        if len(network_parts) != 2 or any(not part or ":" in part or part.endswith((".", " ")) for part in network_parts):
            return "", "Incomplete or ambiguous network folder; choose an absolute folder explicitly."
    if ":" in tail:
        return "", "Unsupported folder syntax; choose a folder explicitly."
    parts = re.split(r"[\\/]", tail)
    if any(part not in ("", ".", "..") and (part.endswith((".", " ")) or
        re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)) for part in parts):
        return "", "Ambiguous or reserved Windows folder name; choose a folder explicitly."
    return ntpath.normpath(value), ""


def review_known_model_folders(snapshot, *, swarm_inventory=None, protected_roots=()):
    """Project saved Setup/index roots and an explicitly obtained Swarm read.

    Unknown path syntax remains visible, never guessed. Distinct origins for a
    case-insensitive lexical path are retained; no physical alias is inferred.
    """
    workspace = snapshot.get("workspace", {}) if isinstance(snapshot, dict) else None
    if not isinstance(workspace, dict):
        raise ValueError("Saved model-folder settings are unsupported. Review Setup Folders.")
    origins, warnings = [], []
    def add(label, value, hint="", swarm=False):
        if len(origins) >= MAX_ORIGINS:
            if not warnings: warnings.append("Folder review limit reached; only the first 512 recorded origins are shown. Narrow saved folders in Setup.")
            return
        origins.append(FolderOrigin(label, _scalar(value), hint, swarm))
    if "default_destination" in workspace:
        add("Setup default destination", workspace["default_destination"])
    for key, label in (("destination_overrides", "Setup role destination"), ("family_destinations", "Setup family destination")):
        values = workspace.get(key, {})
        if not isinstance(values, dict):
            add(label, values); continue
        for hint, value in islice(values.items(), MAX_ORIGINS + 1):
            add(label, value, _scalar(hint)[:200])
    indexed = workspace.get("indexed_folders", [])
    if isinstance(indexed, list):
        for value in islice(indexed, MAX_ORIGINS + 1): add("Previously indexed folder", value)
    else:
        add("Previously indexed folders", indexed)
    if swarm_inventory is not None:
        if (not isinstance(swarm_inventory, ModelPathInventory) or not isinstance(swarm_inventory.values, dict)
                or set(swarm_inventory.values) != set(ROLE_FIELDS)
                or not isinstance(swarm_inventory.binding, dict) or not swarm_inventory.binding
                or not isinstance(swarm_inventory.fingerprint, str) or not swarm_inventory.fingerprint):
            raise ValueError("Connected SwarmUI returned unsupported model-folder evidence. Read its folders again.")
        add("Connected SwarmUI model root", swarm_inventory.model_root, swarm=True)
        for field in ROLE_FIELDS:
            add("Connected SwarmUI role folder", swarm_inventory.values[field], SWARM_LABELS[field], True)
    protected = []
    for value in protected_roots:
        path, issue = _absolute_folder(str(value))
        if issue:
            raise ValueError("Protected folder scope is unsupported; folder preview is held.")
        protected.append(ntpath.normcase(path).rstrip("\\") + "\\")
    grouped = {}
    for origin in origins:
        path, issue = _absolute_folder(origin.recorded_path)
        if path:
            key = ntpath.normcase(path)
            scoped = key.rstrip("\\") + "\\"
            if any(scoped.startswith(root) or root.startswith(scoped) for root in protected):
                issue = "Protected folder overlap; choose a model-only folder separate from libraries and backups."
        else:
            key = (origin.label, origin.recorded_path, origin.hint)
        if key not in grouped: grouped[key] = [path or origin.recorded_path, [], issue]
        grouped[key][1].append(origin)
    return KnownFolderReview(tuple(KnownModelFolder(path, tuple(items), issue)
        for path, items, issue in grouped.values()), tuple(warnings))
