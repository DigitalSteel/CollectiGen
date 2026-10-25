"""Pure bounded resource presentation and versioned browser preferences.

No model/preset discovery, preview loading, compatibility admission or settings
publication lives here. Owning services supply already authorized descriptors.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from itertools import islice
import ntpath

MODES = ("cards", "thumbnails", "list", "details")
SORT_KEYS = ("recommended", "name", "family", "recent", "author", "size")
VIEW_KEYS = ("models", "presets")
PREFERENCE_KEYS = (*VIEW_KEYS, "models_management")
MAX_ROWS = 20_000


@dataclass(frozen=True)
class BrowserViewPreferences:
    mode: str = "cards"
    density: int = 2
    sort_key: str = "recommended"
    reverse: bool = False
    include_descendants: bool = True
    max_depth: int = 0
    navigation_visible: bool = True


def normalize_view_preferences(value):
    if not isinstance(value, dict): raise ValueError("Browser view preferences must be a mapping.")
    fields = {**asdict(BrowserViewPreferences()), **{key: value[key] for key in asdict(BrowserViewPreferences()) if key in value}}
    if fields["mode"] not in MODES or fields["sort_key"] not in SORT_KEYS:
        raise ValueError("This browser mode or ordering is unsupported; saved preferences were preserved.")
    if type(fields["density"]) is not int or not 0 <= fields["density"] <= 5:
        raise ValueError("Choose a browser density from 0 to 5.")
    if any(type(fields[key]) is not bool for key in ("reverse", "include_descendants", "navigation_visible")):
        raise ValueError("Browser ordering and descendant preferences must be explicit booleans.")
    if type(fields["max_depth"]) is not int or not 0 <= fields["max_depth"] <= 32:
        raise ValueError("Browser folder depth must be from 0 to 32; zero includes all depths.")
    return BrowserViewPreferences(**fields)


def _views(saved, key):
    if key not in PREFERENCE_KEYS: raise ValueError("Choose a supported resource browser preference scope.")
    if not isinstance(saved, dict) or (saved and (type(saved.get("schema_version")) is not int or saved.get("schema_version") != 1)):
        raise ValueError("Browser preference version is unsupported; saved preferences were preserved.")
    views = saved.get("views", {})
    if not isinstance(views, dict) or len(views) > 100:
        raise ValueError("Browser view preferences are invalid or exceed their supported bound.")
    return views


def load_view_preferences(saved, view_key="models"):
    return normalize_view_preferences(_views(saved, view_key).get(view_key, {}))


def update_view_preferences(saved, view_key, changes):
    views = _views(saved, view_key)
    load_view_preferences(saved, view_key)
    if not isinstance(changes, dict) or any(key not in asdict(BrowserViewPreferences()) for key in changes):
        raise ValueError("Update only supported browser presentation preferences.")
    selected = {**views.get(view_key, {}), **changes}
    normalize_view_preferences(selected)
    result = deepcopy(saved)
    result["schema_version"] = 1
    result.setdefault("views", {})[view_key] = deepcopy(selected)
    return result


def folder_key(value):
    text = str(value or "").replace("/", "\\")
    return ntpath.normcase(ntpath.normpath(text)) if text else ""


def preset_name_group(name):
    """Logical display group from slash-separated names, never a disk path."""
    if not isinstance(name, str):
        raise ValueError("Preset name groups require plain names.")
    parts = [part.strip() for part in name.split("/")[:-1]]
    if not parts or len(parts) > 16 or any(not part or part in {".", ".."} or "\\" in part or ":" in part for part in parts):
        return "Ungrouped"
    return "\\".join(parts)


def resource_key(row):
    explicit = getattr(row, "browser_key", None)
    if explicit is not None:
        if not isinstance(explicit, tuple) or not explicit or any(not isinstance(part, str) for part in explicit):
            raise ValueError("Resource browser identity must be an immutable string tuple.")
        if explicit[0] not in VIEW_KEYS:
            raise ValueError("Resource browser identity needs a models or presets namespace.")
        return explicit
    kind = str(getattr(row, "resource_kind", "models"))
    if kind not in VIEW_KEYS: raise ValueError("Resource browser namespace is unsupported.")
    return (kind, str(row.resource_id), str(row.role), str(row.family).casefold())


@dataclass(frozen=True)
class ResourceView:
    key: tuple[str, ...]
    title: str
    role: str
    family: str
    folders: tuple[str, ...]
    author: str = ""
    version: str = ""
    source: str = ""
    favorite: bool = False
    recent_selection: str = ""
    size_bytes: int = 0
    storage_state: str = "Unknown"
    search_text: str = ""


def project_rows(rows):
    rows = tuple(islice(iter(rows), MAX_ROWS + 1))
    if len(rows) > MAX_ROWS: raise ValueError("Resource browsing supports at most 20,000 projected choices.")
    result, identities = [], set()
    for row in rows:
        key = resource_key(row)
        if key in identities: raise ValueError("Resource browser choices must have distinct exact identities.")
        identities.add(key)
        categories = getattr(row, "browser_folders", None)
        paths = tuple(islice(iter(categories if categories is not None else getattr(row, "locations", ())), 257))
        if len(paths) > 256: raise ValueError("A resource exceeds the bounded registered-folder presentation.")
        folders = tuple(sorted({folder_key(str(path) if categories is not None else ntpath.dirname(str(path))) for path in paths if path}))
        size = getattr(row, "size_bytes", 0)
        result.append(ResourceView(key, str(row.display_name), str(row.role), str(row.family), folders,
            str(getattr(row, "author", "")), str(getattr(row, "version_label", "")), str(getattr(row, "source", "")),
            bool(row.favorite), str(getattr(row, "last_used_at", "")), size if type(size) is int and size >= 0 else 0,
            str(getattr(row, "storage_state", "Unknown")), str(getattr(row, "search_text", ""))))
    return tuple(result)


def folder_matches(folder, root, *, include_descendants=True, max_depth=0):
    if not root: return True
    if folder == root: return True
    prefix = root.rstrip("\\") + "\\"
    if not include_descendants or not folder.startswith(prefix): return False
    depth = len(folder[len(prefix):].split("\\"))
    return not max_depth or depth <= max_depth


def order_visible(views, preferences, *, folder="", query=""):
    """Return exact row keys; stable ties never invent freshness or readiness."""
    query = str(query).strip().casefold()
    folder = folder_key(folder)
    selected = [view for view in views if (not folder or any(folder_matches(path, folder,
        include_descendants=preferences.include_descendants, max_depth=preferences.max_depth) for path in view.folders))
        and (not query or query in " ".join((view.title, view.role, view.family, view.author, view.version, view.source, view.search_text)).casefold())]
    sort = preferences.sort_key
    if sort != "recommended":
        selected.sort(key=lambda view: (view.title.casefold(), view.key))
        if sort == "family": selected.sort(key=lambda view: view.family.casefold())
        elif sort == "author": selected.sort(key=lambda view: view.author.casefold())
        elif sort == "recent": selected.sort(key=lambda view: view.recent_selection, reverse=True)
        elif sort == "size": selected.sort(key=lambda view: view.size_bytes, reverse=True)
    if preferences.reverse: selected.reverse()
    return tuple(view.key for view in selected)
