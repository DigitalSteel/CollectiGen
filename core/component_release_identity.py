"""Structured component identities; signed legacy wire versions remain unchanged.

Callers supply an audited legacy mapping. No prerelease counter is implicitly
converted into a patch number, and these values do not authorize an update.
"""
from dataclasses import dataclass
from functools import cmp_to_key
from typing import Iterable

from release_contract import ReleaseContractError, SemVer


_MATURITY_RANK = {"alpha": 0, "beta": 1, "stable": 2}
_PREFIX = {"alpha": "a.", "beta": "b.", "stable": ""}


@dataclass(frozen=True)
class ComponentReleaseIdentity:
    version: str
    maturity: str

    def __post_init__(self) -> None:
        if type(self.version) is not str or type(self.maturity) is not str:
            raise ReleaseContractError("Release identity requires string fields.")
        parsed = SemVer.parse(self.version)
        numeric = f"{parsed.major}.{parsed.minor}.{parsed.patch}"
        if self.version != numeric or self.maturity not in _MATURITY_RANK:
            raise ReleaseContractError("Require canonical numeric version and alpha/beta/stable maturity.")

    @property
    def display_version(self) -> str:
        return _PREFIX[self.maturity] + self.version

    @property
    def sort_key(self) -> tuple[int, int, int, int]:
        parsed = SemVer.parse(self.version)
        return parsed.major, parsed.minor, parsed.patch, _MATURITY_RANK[self.maturity]

    def compare(self, other: "ComponentReleaseIdentity") -> int:
        if type(other) is not ComponentReleaseIdentity:
            raise TypeError("Require a component release identity.")
        return (self.sort_key > other.sort_key) - (self.sort_key < other.sort_key)


def require_component_maturity_upgrade(current_version: str, target: ComponentReleaseIdentity,
        *, current_schema: int, current_maturity: str | None = None, supported_from: str) -> None:
    """Pure schema-2 ordering gate, not signature, feed or install authority.

    The numeric floor includes all stages. Legacy same-base transitions hold;
    no legacy display mapping or build ID establishes a newer signed identity.
    """
    if (type(target) is not ComponentReleaseIdentity
            or type(current_schema) is not int or current_schema not in (1, 2)):
        raise ReleaseContractError("Require explicit component identity schemas.")
    floor = ComponentReleaseIdentity(supported_from, "alpha").sort_key[:3]
    if target.sort_key[:3] < floor:
        raise ReleaseContractError("Target precedes supported numeric floor.")
    if current_schema == 1:
        if (current_maturity is not None or type(current_version) is not str
                or not current_version.isascii() or len(current_version) > 96
                or current_version != current_version.strip() or "+" in current_version):
            raise ReleaseContractError("Require exact legacy identity without inferred maturity.")
        parsed = SemVer.parse(current_version)
        current_number = (parsed.major, parsed.minor, parsed.patch)
        newer = target.sort_key[:3] > current_number
    else:
        current = ComponentReleaseIdentity(current_version, current_maturity)
        current_number = current.sort_key[:3]
        newer = target.compare(current) > 0
    if current_number < floor:
        raise ReleaseContractError("Installed version precedes supported numeric floor.")
    if not newer:
        raise ReleaseContractError("Require a newer component identity; equal or downgrade held.")


def validate_producer_source_identity(version_source: bytes, module: dict, history: dict,
        runtime: dict, *, component_id: str, version: str, maturity: str,
        published_at: str, notes: bytes) -> dict:
    """Pure literal-source/date/notes agreement; no source execution or I/O."""
    import ast
    from datetime import date, datetime, timezone
    from zoneinfo import ZoneInfo

    identity = ComponentReleaseIdentity(version, maturity)
    required = {"SOFTWARE_VERSION", "NUMERIC_VERSION", "MATURITY", "RELEASE_DATE", "RELEASE_TIMEZONE"}
    try:
        tree = ast.parse(version_source.decode("utf-8"))
        for statement in tree.body:
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and type(statement.value.value) is str:
                continue
            if (not isinstance(statement, (ast.Assign, ast.AnnAssign))
                    or not isinstance(statement.value, ast.Constant)
                    or (isinstance(statement, ast.AnnAssign)
                        and not isinstance(statement.annotation, ast.Name))):
                raise ReleaseContractError("Literal source declarations only.")
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            if len(targets) != 1 or not isinstance(targets[0], ast.Name):
                raise ReleaseContractError("Simple literal source assignments required.")
        declarations = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)} & required
                for name in names:
                    if (name in declarations or node not in tree.body
                            or not isinstance(node, (ast.Assign, ast.AnnAssign))
                            or len(targets) != 1 or not isinstance(targets[0], ast.Name)
                            or not isinstance(node.value, ast.Constant) or type(node.value.value) is not str):
                        raise ReleaseContractError("Unique literal source identity required.")
                    declarations[name] = node.value.value
        if set(declarations) != required:
            raise ReleaseContractError("Missing literal source identity.")
        expected = dict(SOFTWARE_VERSION=version, NUMERIC_VERSION=version, MATURITY=maturity)
        if any(declarations[k] != v for k, v in expected.items()):
            raise ReleaseContractError("Source identity mismatch.")
        day, zone = declarations["RELEASE_DATE"], declarations["RELEASE_TIMEZONE"]
        if date.fromisoformat(day).isoformat() != day:
            raise ReleaseContractError("Canonical release date required.")
        stamp = datetime.fromisoformat(published_at)
        resolved_zone = timezone.utc if zone == "UTC" else ZoneInfo(zone)
        if stamp.tzinfo is None or stamp.utcoffset() is None or stamp.astimezone(resolved_zone).date().isoformat() != day:
            raise ReleaseContractError("Publication timezone/date mismatch.")
        if (module.get("module_id") != component_id or module.get("software_version") != version
                or module.get("numeric_version") != version or module.get("maturity") != maturity
                or module.get("release_date") != day or module.get("release_timezone") != zone
                or runtime.get("feature_version") != version or runtime.get("maturity") != maturity):
            raise ReleaseContractError("Module/runtime/source identity mismatch.")
        if (type(history) is not dict or type(history.get("schema_version")) is not int
                or history["schema_version"] != 1 or type(history.get("releases")) is not list
                or len(history["releases"]) > 256 or not all(type(r) is dict for r in history["releases"])):
            raise ReleaseContractError("Release history schema.")
        matches = [r for r in history["releases"] if r.get("software_version") == version and r.get("maturity") == maturity]
        if len(matches) != 1 or any(matches[0].get(k) != v for k, v in
                dict(numeric_version=version, date=day, timezone=zone).items()):
            raise ReleaseContractError("Unique matching release history required.")
        text = notes.decode("utf-8")
        lines = text.splitlines(); nonempty = [line for line in lines if line.strip()]
        if (text.startswith("\ufeff") or not nonempty
                or nonempty[0] != f"# {identity.display_version} - {day}"
                or sum(line.startswith("# ") for line in lines) != 1):
            raise ReleaseContractError("Canonical notes identity/date heading required.")
        return dict(version=version, maturity=maturity, release_date=day, release_timezone=zone)
    except (SyntaxError, UnicodeError, ValueError, KeyError, TypeError, RecursionError) as exc:
        if isinstance(exc, ReleaseContractError):
            raise
        raise ReleaseContractError("Invalid producer source/date/notes input.") from exc


def validate_legacy_display_mapping(
    entries: Iterable[tuple[str, str, ComponentReleaseIdentity]],
) -> dict[tuple[str, str], ComponentReleaseIdentity]:
    """Validate explicit component/wire/display mappings without changing feeds.

    Every distinct legacy release needs a distinct display identity, preserving
    SemVer precedence within its component. Equal-precedence build variants
    belong beneath one release and cannot be renumbered as different releases.
    """
    result = {}
    components = {}
    for component, legacy, identity in entries:
        if type(component) is not str or not component.strip() or type(legacy) is not str:
            raise ReleaseContractError("Require component and legacy version strings.")
        if type(identity) is not ComponentReleaseIdentity:
            raise ReleaseContractError("Require a structured display identity.")
        old = SemVer.parse(legacy)
        legacy_maturity = old.prerelease[0] if old.prerelease else "stable"
        if legacy_maturity not in _MATURITY_RANK or identity.maturity != legacy_maturity:
            raise ReleaseContractError("Display mapping must preserve known legacy maturity.")
        if legacy != legacy.strip():
            raise ReleaseContractError("Legacy version cannot contain surrounding whitespace.")
        key = component, legacy
        if key in result:
            raise ReleaseContractError("Duplicate component/legacy identity.")
        result[key] = identity
        components.setdefault(component, []).append((old, identity))
    for releases in components.values():
        releases.sort(key=cmp_to_key(lambda left, right: left[0].compare(right[0])))
        for (old_left, left), (old_right, right) in zip(releases, releases[1:]):
            if old_left.compare(old_right) >= 0 or left.compare(right) >= 0:
                raise ReleaseContractError("Display mappings must be unique and preserve release order.")
    return result


# Explicit accepted local history labels. These are presentation mappings only;
# unknown external versions are displayed verbatim and never guessed.
_LOCAL_LABEL_ROWS = (
    ('CollectiGen', '0.88.0-alpha.1', '0.88.1', 'alpha'),
    ('CollectiGen', '0.88.0-alpha.2', '0.88.2', 'alpha'),
    ('CollectiGen', '0.89.0-alpha.1', '0.89.1', 'alpha'),
    ('CollectiGen', '0.90.0-alpha.1', '0.90.1', 'alpha'),
    ('CollectiGen', '0.91.0-alpha.1', '0.91.1', 'alpha'),
    ('CollectiGen', '0.92.0-alpha.1', '0.92.1', 'alpha'),
    ('CollectiGen', '0.93.0-alpha.1', '0.93.1', 'alpha'),
    ('CollectiGen', '0.94.0-alpha.1', '0.94.1', 'alpha'),
    ('CollectiGen', '0.95.0-alpha.1', '0.95.1', 'alpha'),
    ('CollectiGen', '0.96.0-alpha.1', '0.96.1', 'alpha'),
    ('CollectiGen', '0.97.0-alpha.1', '0.97.1', 'alpha'),
    ('CollectiGen', '0.98.0-alpha.1', '0.98.1', 'alpha'),
    ('CollectiGen', '0.99.0-alpha.1', '0.99.1', 'alpha'),
    ('CollectiGen', '0.100.0-alpha.1', '0.100.1', 'alpha'),
    ('MediaOrganizer', '0.4.0-alpha.1', '0.4.0', 'alpha'),
    ('TagEncyclopedia', '0.1.9-alpha.1', '0.1.9', 'alpha'),
    ('TagEncyclopedia', '0.2.0-alpha.1', '0.2.0', 'alpha'),
    ('MediaOrganizer', '0.1.0-alpha.1', '0.1.1', 'alpha'),
    ('MediaOrganizer', '0.1.0-alpha.2', '0.1.2', 'alpha'),
    ('MediaOrganizer', '0.1.0-alpha.3', '0.1.3', 'alpha'),
    ('MediaOrganizer', '0.1.0-alpha.4', '0.1.4', 'alpha'),
    ('MediaOrganizer', '0.2.0-alpha.1', '0.2.0', 'alpha'),
    ('SuiteManager', '0.1.0-alpha.9', '0.1.9', 'alpha'),
    ('SuiteManager', '0.1.0-alpha.17', '0.1.17', 'alpha'),
    ('SuiteManager', '0.1.0-alpha.18', '0.1.18', 'alpha'),
    ('SuiteManager', '0.1.0-alpha.19', '0.1.19', 'alpha'),
    ('SuiteManager', '0.1.0-alpha.20', '0.1.20', 'alpha'),
    ('SuiteManager', '0.1.0-alpha.21', '0.1.21', 'alpha'),
    ('SuiteManager', '0.1.0-alpha.22', '0.1.22', 'alpha'),
    ('SuiteManager', '0.2.0-alpha.1', '0.2.0', 'alpha'),
    ('SuiteManager', '0.2.1-alpha.1', '0.2.1', 'alpha'),
    ('SuiteManager', '0.2.1-alpha.2', '0.2.2', 'alpha'),
    ('SuiteManager', '0.2.1-alpha.3', '0.2.3', 'alpha'),
    ('SuiteManager', '0.2.1-alpha.4', '0.2.4', 'alpha'),
    ('SuiteManager', '0.2.1-alpha.5', '0.2.5', 'alpha'),
    ('SuiteManager', '0.2.1-alpha.6', '0.2.6', 'alpha'),
    ('SuiteManager', '0.2.1-alpha.7', '0.2.7', 'alpha'),
    ('SuiteManager', '0.2.1-alpha.8', '0.2.8', 'alpha'),
    ('SuiteManager', '0.2.1-alpha.9', '0.2.9', 'alpha'),
    ('SuiteManager', '0.2.1-alpha.10', '0.2.10', 'alpha'),
    ('SuiteManager', '0.2.1-alpha.11', '0.2.11', 'alpha'),
    ('SuiteManager', '0.2.1-alpha.12', '0.2.12', 'alpha'),
    ('SuiteManager', '0.2.1-alpha.13', '0.2.13', 'alpha'),
    ('SuiteManager', '0.2.1-alpha.14', '0.2.14', 'alpha'),
    ('SuiteManager', '0.2.1-alpha.15', '0.2.15', 'alpha'),
    ('SuiteManager', '0.2.1-alpha.16', '0.2.16', 'alpha'),
    ('SuiteManager', '0.3.0-alpha.1', '0.3.0', 'alpha'),
    ('SuiteManager', '0.3.1-alpha.1', '0.3.1', 'alpha'),
    ('SuiteManager', '0.3.2-alpha.1', '0.3.2', 'alpha'),
    ('SuiteManager', '0.4.0-alpha.1', '0.4.0', 'alpha'),
    ('SuiteManager', '0.4.1-alpha.1', '0.4.1', 'alpha'),
    ('SuiteManager', '0.5.0-alpha.1', '0.5.0', 'alpha'),
    ('SuiteManager', '0.5.1-alpha.1', '0.5.1', 'alpha'),
    ('SuiteManager', '0.6.0-alpha.1', '0.6.0', 'alpha'),
    ('TagEncyclopedia', '0.2.2-alpha.1', '0.2.2', 'alpha'),
    ('MediaOrganizer', '0.4.6-alpha.1', '0.4.6', 'alpha'),
    ('TagEncyclopedia', '0.1.0-alpha.1', '0.1.1', 'alpha'),
    ('TagEncyclopedia', '0.1.0-alpha.2', '0.1.2', 'alpha'),
    ('TagEncyclopedia', '0.1.0-alpha.3', '0.1.3', 'alpha'),
    ('TagEncyclopedia', '0.1.0-alpha.4', '0.1.4', 'alpha'),
    ('TagEncyclopedia', '0.1.0-alpha.5', '0.1.5', 'alpha'),
    ('TagEncyclopedia', '0.1.0-alpha.6', '0.1.6', 'alpha'),
    ('TagEncyclopedia', '0.1.0-alpha.7', '0.1.7', 'alpha'),
    ('TagEncyclopedia', '0.1.0-alpha.8', '0.1.8', 'alpha'),
)
_LOCAL_LABELS = validate_legacy_display_mapping(
    (component, legacy, ComponentReleaseIdentity(numeric, maturity))
    for component, legacy, numeric, maturity in _LOCAL_LABEL_ROWS
)
_COMPONENT_ALIASES = {
    "collectigen": "CollectiGen", "CollectiGen": "CollectiGen",
    "suite-manager": "SuiteManager", "collectigen.suite-manager": "SuiteManager", "SuiteManager": "SuiteManager",
    "tag-encyclopedia": "TagEncyclopedia", "collectigen.tag-encyclopedia": "TagEncyclopedia", "TagEncyclopedia": "TagEncyclopedia",
    "media-organizer": "MediaOrganizer", "collectigen.media-organizer": "MediaOrganizer", "MediaOrganizer": "MediaOrganizer",
}


def display_component_version(component: str, legacy_version: str, *, include_legacy: bool = False) -> str:
    """Format a known label without altering DTOs, installed identity or trust."""
    identity = _LOCAL_LABELS.get((_COMPONENT_ALIASES.get(component, component), legacy_version))
    if identity is None:
        return legacy_version
    label = identity.display_version
    return f"{label} (embedded {legacy_version})" if include_legacy else label
