"""Read-only host projections; no catalog fetching, package verification or activation."""
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Callable

from release_contract import SemVer
from suite_core.installation import (
    ComponentId, InstallationId, InstallationRegistry, RemoteDescriptor,
    ResolveRequest, ResolveState, removal_blockers, resolve,
)
from version import VERSION

PACKAGE_GATE = "No signed module catalog or packages are connected. Download, install, update and remove are unavailable."
MODULE_VIEW_INTERFACE_VERSION = 1


def _exact_version(value):
    if type(value) is not str or not 1 <= len(value) <= 96 or value != value.strip():
        raise ValueError("Bounded exact software version required")
    SemVer.parse(value)


class ModuleOrigin(str, Enum):
    CONFIGURED = "Configured inventory"
    DEVELOPMENT = "Development checkout"
    INSTALLED = "Verified installed package"
    AVAILABLE = "Published available release"
    PLANNED = "Planned / unpublished"
    REMOTE = "Unsupported remote"


@dataclass(frozen=True)
class ModuleDependency:
    component_id: ComponentId
    software_version: str

    def __post_init__(self):
        if type(self.component_id) is not ComponentId:
            raise ValueError("Explicit component identity required")
        _exact_version(self.software_version)


@dataclass(frozen=True)
class ModuleRecord:
    key: str
    name: str
    component_id: ComponentId
    software_version: str
    origin: ModuleOrigin
    released_on: str = ""
    notes: str = ""
    dependencies: tuple[ModuleDependency, ...] = ()
    compatible: bool | None = None
    inventory_state: str = ""
    interfaces: tuple[str, ...] = ()
    installation_id: InstallationId | None = None

    def __post_init__(self):
        if not isinstance(self.key, str) or not 1 <= len(self.key) <= 128:
            raise ValueError("Bounded record key required")
        if type(self.component_id) is not ComponentId or type(self.origin) is not ModuleOrigin:
            raise ValueError("Explicit module identity and origin required")
        _exact_version(self.software_version)
        if not isinstance(self.name, str) or not 1 <= len(self.name) <= 256:
            raise ValueError("Bounded display name required")
        if type(self.notes) is not str or len(self.notes) > 16000:
            raise ValueError("Bounded release notes required")
        if type(self.released_on) is not str or len(self.released_on) > 10:
            raise ValueError("Exact release date required")
        if self.released_on:
            if date.fromisoformat(self.released_on).isoformat() != self.released_on:
                raise ValueError("Canonical release date required")
        if type(self.dependencies) is not tuple or len(self.dependencies) > 16 or any(type(d) is not ModuleDependency for d in self.dependencies):
            raise ValueError("Bounded exact dependencies required")
        if self.compatible is not None and type(self.compatible) is not bool:
            raise ValueError("Compatibility must be known or unknown")
        if type(self.interfaces) is not tuple or len(self.interfaces) > 16 or any(type(v) is not str or len(v) > 128 for v in self.interfaces):
            raise ValueError("Bounded interface descriptions required")
        if type(self.inventory_state) is not str or len(self.inventory_state) > 128:
            raise ValueError("Bounded inventory state required")
        if self.installation_id is not None and type(self.installation_id) is not InstallationId:
            raise ValueError("Explicit installation identity required")

    @property
    def group(self):
        if self.origin is ModuleOrigin.REMOTE:
            return "Remote"
        if self.origin in {ModuleOrigin.AVAILABLE, ModuleOrigin.PLANNED}:
            return "Available"
        return "Installed"


@dataclass(frozen=True)
class ModuleSnapshot:
    revision: str
    records: tuple[ModuleRecord, ...] = ()
    synthetic: bool = False
    registry: InstallationRegistry | None = None

    def __post_init__(self):
        if type(self.revision) is not str or not 1 <= len(self.revision) <= 128:
            raise ValueError("Bounded inventory revision required")
        if type(self.records) is not tuple or len(self.records) > 64 or any(type(r) is not ModuleRecord for r in self.records):
            raise ValueError("Bounded immutable inventory required")
        if len({r.key for r in self.records}) != len(self.records):
            raise ValueError("Duplicate module record")
        if type(self.synthetic) is not bool or self.registry is not None and type(self.registry) is not InstallationRegistry:
            raise ValueError("Invalid inventory authority")
        if not self.synthetic and any(r.origin in {ModuleOrigin.INSTALLED, ModuleOrigin.AVAILABLE} for r in self.records):
            raise ValueError("Package verification is not implemented; package states require synthetic fixtures")


SnapshotProvider = Callable[[], ModuleSnapshot]


def empty_snapshot():
    """Honest production default: no live registry read or invented module rows."""
    return ModuleSnapshot("unconfigured")


def host_context():
    checkout = (Path(__file__).resolve().parents[1] / ".git").exists()
    return f"CollectiGen {VERSION} · " + ("Development checkout" if checkout else "Host package verification not supplied")


def configured_snapshot(registry, layout, consumer, *, revision):
    """Project the accepted explicit locator; located_local never becomes verified."""
    if type(registry) is not InstallationRegistry:
        raise ValueError("Explicit registry required")
    rows = []
    for entry in registry.entries:
        capability = sorted(entry.capabilities, key=lambda c: c.value)[0]
        result = resolve(layout, registry, ResolveRequest(
            consumer, entry.component_id, entry.software_version, capability,
            entry.interfaces[0], entry.installation_id,
        ))
        origin = ModuleOrigin.REMOTE if type(entry.location) is RemoteDescriptor else ModuleOrigin.CONFIGURED
        dependencies = tuple(ModuleDependency(next(e for e in registry.entries if e.installation_id == identity).component_id,
                                              next(e for e in registry.entries if e.installation_id == identity).software_version)
                             for identity in entry.dependencies)
        rows.append(ModuleRecord(entry.installation_id.value, entry.component_id.value, entry.component_id,
                                 entry.software_version, origin, dependencies=dependencies,
                                 inventory_state=result.state.value,
                                 interfaces=tuple(f"{i.manifest_key} = {i.version}" for i in entry.interfaces),
                                 installation_id=entry.installation_id))
    return ModuleSnapshot(revision, tuple(rows), registry=registry)


@dataclass(frozen=True)
class ModulePlan:
    title: str
    lines: tuple[str, ...]
    holds: tuple[str, ...]

    @property
    def executable(self):
        return False

    def text(self):
        return "\n".join((*self.lines, "", "Holds:", *("• " + h for h in self.holds), "", "Preview only. Nothing was downloaded, installed, changed or removed."))


def dependency_plan(snapshot, key, expected_revision):
    """Exact dependency inspection, bounded by the snapshot; never an install plan."""
    rows = {r.key: r for r in snapshot.records}
    holds = [PACKAGE_GATE]
    if snapshot.revision != expected_revision:
        return ModulePlan("Inventory changed — refresh and review again", (), tuple(holds + ["The selected inventory revision is stale."]))
    root = rows.get(key)
    if root is None:
        return ModulePlan("Module no longer present", (), tuple(holds + ["The selected module is missing. Refresh the inventory."]))
    lines, visited, active, versions = [], set(), set(), {}

    def visit(row):
        if row.key in active:
            holds.append(f"Dependency cycle: {row.name}.")
            return
        if row.key in visited:
            return
        active.add(row.key)
        prior = versions.setdefault(row.component_id, row.software_version)
        if prior != row.software_version:
            holds.append(f"Version conflict: {row.component_id.value} requires {prior} and {row.software_version}.")
        lines.append(f"{row.name} · {row.software_version} · {row.origin.value}")
        if row.origin not in {ModuleOrigin.INSTALLED, ModuleOrigin.AVAILABLE}:
            holds.append(f"{row.name}: {row.origin.value}; package eligibility is not established.")
        if row.inventory_state in {ResolveState.MISSING_LOCAL.value, ResolveState.HELD.value, ResolveState.INCOMPATIBLE.value}:
            holds.append(f"{row.name}: inventory {row.inventory_state}.")
        if row.compatible is not True:
            holds.append(f"{row.name}: " + ("incompatible with this host." if row.compatible is False else "compatibility is unknown."))
        for dep in row.dependencies:
            previous = versions.setdefault(dep.component_id, dep.software_version)
            if previous != dep.software_version:
                holds.append(f"Version conflict: {dep.component_id.value} requires {previous} and {dep.software_version}.")
            matches = [r for r in snapshot.records if r.component_id == dep.component_id and r.software_version == dep.software_version
                       and r.origin in {ModuleOrigin.INSTALLED, ModuleOrigin.AVAILABLE}]
            installed = [r for r in matches if r.origin is ModuleOrigin.INSTALLED]
            matches = installed or matches
            if len(matches) != 1:
                holds.append(f"Dependency {dep.component_id.value} {dep.software_version}: " + ("ambiguous exact matches." if matches else "no verified installed or published exact match."))
            else:
                visit(matches[0])
        active.remove(row.key)
        visited.add(row.key)

    visit(root)
    return ModulePlan("Dependency preview — " + root.name, tuple(lines), tuple(dict.fromkeys(holds)))


def removal_plan(snapshot, key, expected_revision):
    holds = [PACKAGE_GATE]
    if snapshot.revision != expected_revision:
        holds.append("The selected inventory revision is stale. Refresh and review again.")
    row = next((r for r in snapshot.records if r.key == key), None)
    if row is None:
        holds.append("The selected module is missing.")
    elif snapshot.registry is None or row.installation_id is None:
        holds.append("No bound installation registry is supplied; dependent checks are unknown.")
    else:
        bound = next((entry for entry in snapshot.registry.entries if entry.installation_id == row.installation_id), None)
        if bound is None or bound.component_id != row.component_id or bound.software_version != row.software_version:
            holds.append("The selected installation does not match the supplied registry.")
        else:
            for dependent, dependency in removal_blockers(snapshot.registry, frozenset({row.installation_id})):
                holds.append(f"Configured dependent {dependent.value} requires {dependency.value}.")
    return ModulePlan("Removal preview", (() if row is None else (f"{row.name} · {row.software_version}",)), tuple(holds))
