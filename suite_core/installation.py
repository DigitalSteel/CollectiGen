"""Explicit immutable installation configuration, never backend state or activation."""
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlsplit

from .backend_contracts import ApplicationId, CapabilityId, StableId
from .installation_version import INSTALLATION_INTERFACE_VERSION

MAX_INSTALLATIONS = 64
MAX_MANIFEST_BYTES = 64 * 1024
MAX_INVENTORY_FILES = 128
_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*)?\Z")
_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]{0,127}\Z")
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


class InstallationHold(ValueError):
    pass


def _require(value, reason):
    if not value:
        raise InstallationHold(reason)


def _version(value):
    _require(type(value) is str and len(value) <= 96 and _VERSION.fullmatch(value), "explicit_exact_version_required")
    if "-" in value:
        _require(all(not (part.isdigit() and len(part) > 1 and part.startswith("0")) for part in value.split("-", 1)[1].split(".")), "invalid_prerelease_version")


def _relative(value):
    _require(type(value) is str and len(value) <= 1024, "invalid_relative_path")
    path = PurePosixPath(value)
    _require(not path.is_absolute() and path.as_posix() == value and 1 <= len(path.parts) <= 16, "invalid_relative_path")
    for part in path.parts:
        _require(_PART.fullmatch(part) and part not in {".", ".."} and not part.endswith(".")
                 and part.split(".", 1)[0].casefold() not in _RESERVED, "invalid_relative_path")
    return path.parts


def _path(value):
    _require(isinstance(value, Path) and value.is_absolute() and len(str(value)) <= 4096 and len(value.parts) <= 128, "absolute_explicit_root_required")
    _require(os.name != "nt" or hasattr(value, "is_junction"), "filesystem_alias_checks_unavailable")
    _require(not str(value).startswith(("//", "\\\\")), "remote_filesystem_root_unsupported")
    for part in (value, *value.parents):
        _require(not part.is_symlink() and not getattr(part, "is_junction", lambda: False)(), "redirected_path_refused")
    return value.resolve(strict=False)


def _within(path, root):
    return path == root or root in path.parents


class InstallMode(str, Enum):
    PORTABLE = "portable"
    INSTALLED = "installed"


class ComponentKind(str, Enum):
    APP = "Apps"
    MODULE = "Modules"
    RUNTIME = "Runtimes"


@dataclass(frozen=True, slots=True)
class ComponentId(StableId):
    pass


@dataclass(frozen=True, slots=True)
class InstallationId(StableId):
    pass


@dataclass(frozen=True, slots=True)
class SuiteLayout:
    code_root: Path
    data_root: Path
    mode: InstallMode
    model_roots: tuple[Path, ...] = ()

    def __post_init__(self):
        code, data = _path(self.code_root), _path(self.data_root)
        _require(type(self.mode) is InstallMode and type(self.model_roots) is tuple and len(self.model_roots) <= 16, "invalid_layout")
        _require(data != code and not any(_within(data, code / kind.value) for kind in ComponentKind), "data_overlaps_component_code")
        if self.mode is InstallMode.INSTALLED:
            _require(not _within(data, code) and not _within(code, data), "installed_data_must_be_separate")
        else:
            _require(data == code / "Data", "portable_data_root_must_be_explicit_Data")
        for root in self.model_roots:
            model = _path(root)
            _require((not _within(model, code) and not _within(code, model)) or _within(model, data), "models_overlap_component_code")


@dataclass(frozen=True, slots=True)
class InterfaceBinding:
    manifest_key: str
    version: int

    def __post_init__(self):
        _require(type(self.manifest_key) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.manifest_key)
                 and self.manifest_key.endswith("_version") and self.manifest_key not in {"schema_version", "software_version"}, "invalid_interface_key")
        _require(type(self.version) is int and 1 <= self.version <= 65535, "invalid_interface_version")


@dataclass(frozen=True, slots=True)
class LocalInventory:
    relative_directory: str
    manifest: str
    manifest_sha256: str

    def __post_init__(self):
        _relative(self.relative_directory)
        _relative(self.manifest)
        _require(type(self.manifest_sha256) is str and re.fullmatch(r"[0-9a-f]{64}", self.manifest_sha256), "exact_manifest_pin_required")


@dataclass(frozen=True, slots=True)
class RemoteDescriptor:
    endpoint: str
    read_interface_version: int

    def __post_init__(self):
        _require(type(self.endpoint) is str and len(self.endpoint) <= 2048 and not any(c.isspace() or ord(c) < 32 for c in self.endpoint), "invalid_remote_descriptor")
        try:
            parsed = urlsplit(self.endpoint)
            port = parsed.port
        except ValueError as failure:
            raise InstallationHold("invalid_remote_descriptor") from failure
        _require(parsed.scheme in {"http", "https"} and parsed.hostname and parsed.username is None
                 and parsed.password is None and not parsed.query and not parsed.fragment
                 and (port is None or 1 <= port <= 65535), "invalid_remote_descriptor")
        _require(type(self.read_interface_version) is int and self.read_interface_version == 1, "unsupported_remote_descriptor_interface")


@dataclass(frozen=True, slots=True)
class InstalledComponent:
    installation_id: InstallationId
    component_id: ComponentId
    software_version: str
    kind: ComponentKind
    capabilities: frozenset[CapabilityId]
    interfaces: tuple[InterfaceBinding, ...]
    location: LocalInventory | RemoteDescriptor
    dependencies: tuple[InstallationId, ...] = ()

    def __post_init__(self):
        _require(type(self.installation_id) is InstallationId and type(self.component_id) is ComponentId and type(self.kind) is ComponentKind, "invalid_component_identity")
        _version(self.software_version)
        _require(type(self.capabilities) is frozenset and 1 <= len(self.capabilities) <= 16
                 and all(type(v) is CapabilityId for v in self.capabilities), "invalid_capabilities")
        _require(type(self.interfaces) is tuple and 1 <= len(self.interfaces) <= 16
                 and all(type(v) is InterfaceBinding for v in self.interfaces)
                 and len({v.manifest_key for v in self.interfaces}) == len(self.interfaces), "invalid_interfaces")
        _require(type(self.location) in {LocalInventory, RemoteDescriptor}, "unsupported_location")
        if type(self.location) is LocalInventory:
            _require(len(_relative(self.location.relative_directory)) >= 2 and _relative(self.location.relative_directory)[0] == self.kind.value, "component_kind_directory_mismatch")
        _require(type(self.dependencies) is tuple and len(self.dependencies) <= 16
                 and all(type(v) is InstallationId for v in self.dependencies)
                 and len(set(self.dependencies)) == len(self.dependencies)
                 and self.installation_id not in self.dependencies, "invalid_dependencies")


@dataclass(frozen=True, slots=True)
class InstallationRegistry:
    entries: tuple[InstalledComponent, ...]

    def __post_init__(self):
        _require(type(self.entries) is tuple and len(self.entries) <= MAX_INSTALLATIONS and all(type(v) is InstalledComponent for v in self.entries), "invalid_registry")
        identities = {v.installation_id for v in self.entries}
        _require(len(identities) == len(self.entries), "duplicate_installation_identity")
        _require(all(set(v.dependencies) <= identities for v in self.entries), "unbound_installation_dependency")


@dataclass(frozen=True, slots=True)
class ResolveRequest:
    consumer: ApplicationId
    component_id: ComponentId
    software_version: str
    capability: CapabilityId
    interface: InterfaceBinding
    installation_id: InstallationId | None = None

    def __post_init__(self):
        _require(type(self.consumer) is ApplicationId and type(self.component_id) is ComponentId
                 and type(self.capability) is CapabilityId and type(self.interface) is InterfaceBinding, "invalid_request")
        _version(self.software_version)
        _require(self.installation_id is None or type(self.installation_id) is InstallationId, "invalid_requested_installation")


class ResolveState(str, Enum):
    NOT_CONFIGURED = "not_configured"
    INCOMPATIBLE = "incompatible"
    AMBIGUOUS = "ambiguous"
    MISSING_LOCAL = "missing_local"
    HELD = "held"
    LOCATED_LOCAL = "located_local"
    REMOTE_UNSUPPORTED = "remote_unsupported"


@dataclass(frozen=True, slots=True)
class Resolution:
    consumer: ApplicationId
    state: ResolveState
    installation_id: InstallationId | None = None
    local_root: Path | None = None
    reason: str | None = None
    interface_version: int = INSTALLATION_INTERFACE_VERSION

    @property
    def execution_authorized(self):
        return False


def _pairs(pairs):
    output = {}
    for key, value in pairs:
        _require(key not in output, "duplicate_manifest_key")
        output[key] = value
    return output


def _manifest(raw):
    # Reject excessive JSON nesting before invoking the recursive parser.
    depth = 0
    quoted = escaped = False
    for c in raw:
        if quoted:
            if escaped: escaped = False
            elif c == 92: escaped = True
            elif c == 34: quoted = False
        elif c == 34: quoted = True
        elif c in (91, 123):
            depth += 1
            _require(depth <= 16, "manifest_depth_bound")
        elif c in (93, 125): depth -= 1
    return json.loads(raw, object_pairs_hook=_pairs)


def resolve(layout, registry, request):
    """Locate one explicit inventory; never load code, launch, poll or write."""
    _require(type(layout) is SuiteLayout and type(registry) is InstallationRegistry and type(request) is ResolveRequest, "invalid_resolver_input")
    entries = [v for v in registry.entries if v.component_id == request.component_id
               and (request.installation_id is None or v.installation_id == request.installation_id)]
    if not entries:
        return Resolution(request.consumer, ResolveState.NOT_CONFIGURED)
    entries = [v for v in entries if v.software_version == request.software_version
               and request.capability in v.capabilities and request.interface in v.interfaces]
    if not entries:
        return Resolution(request.consumer, ResolveState.INCOMPATIBLE)
    if len(entries) != 1:
        return Resolution(request.consumer, ResolveState.AMBIGUOUS)
    entry = entries[0]
    if type(entry.location) is RemoteDescriptor:
        return Resolution(request.consumer, ResolveState.REMOTE_UNSUPPORTED, entry.installation_id)
    root = None
    try:
        # Revalidate mutable filesystem aliases at every read, not only at DTO creation.
        code = _path(layout.code_root)
        root = _path(code.joinpath(*_relative(entry.location.relative_directory)))
        _require(_within(root, code / entry.kind.value), "component_root_escape")
        manifest_path = _path(root.joinpath(*_relative(entry.location.manifest)))
        _require(_within(manifest_path, root), "manifest_root_escape")
        if not manifest_path.is_file():
            return Resolution(request.consumer, ResolveState.MISSING_LOCAL, entry.installation_id)
        with manifest_path.open("rb") as stream:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        _require(len(raw) <= MAX_MANIFEST_BYTES, "manifest_byte_bound")
        _require(sha256(raw).hexdigest() == entry.location.manifest_sha256, "manifest_pin_changed")
        manifest = _manifest(raw)
        _require(type(manifest) is dict and type(manifest.get("schema_version")) is int and manifest["schema_version"] == 1, "unsupported_inventory_schema")
        _require(manifest.get("module_id") == entry.component_id.value and manifest.get("software_version") == entry.software_version, "inventory_identity_mismatch")
        _require(all(type(manifest.get(v.manifest_key)) is int and manifest[v.manifest_key] == v.version for v in entry.interfaces), "inventory_interface_mismatch")
        files = manifest.get("runtime_files")
        _require(type(files) is list and 1 <= len(files) <= MAX_INVENTORY_FILES and all(type(v) is str for v in files), "invalid_runtime_inventory")
        _require(len({name.casefold() for name in files}) == len(files), "duplicate_runtime_inventory")
        for name in files:
            target = _path(root.joinpath(*_relative(name)))
            _require(_within(target, root) and target.is_file(), "runtime_inventory_missing_or_redirected")
        # No support/data tree is copied, enumerated or interpreted.
        return Resolution(request.consumer, ResolveState.LOCATED_LOCAL, entry.installation_id, root)
    except (InstallationHold, OSError, ValueError, TypeError, RecursionError):
        return Resolution(request.consumer, ResolveState.HELD, entry.installation_id, reason="inventory_validation_held")


def removal_blockers(registry, removing):
    """Pure preview: configured surviving dependents block removal, even if offline."""
    _require(type(registry) is InstallationRegistry and type(removing) is frozenset
             and all(type(v) is InstallationId for v in removing), "invalid_removal_preview")
    _require(removing <= {v.installation_id for v in registry.entries}, "unknown_removal_identity")
    return tuple(sorted(((v.installation_id, dependency) for v in registry.entries if v.installation_id not in removing
                         for dependency in v.dependencies if dependency in removing), key=lambda pair: (pair[0].value, pair[1].value)))
