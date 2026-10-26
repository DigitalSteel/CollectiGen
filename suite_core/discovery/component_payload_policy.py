"""Finite caller-selected package ceilings; archive metadata grants no policy."""
from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from suite_core.installation import ComponentId, _relative, _RESERVED

@dataclass(frozen=True, slots=True)
class ComponentPayloadPolicy:
    runtime_kind: str
    max_inventory_members: int
    max_zip_entries: int
    max_unpacked_bytes: int
    max_archive_bytes: int
    max_inventory_json_bytes: int
    max_assembly_tree_entries: int | None
    max_relative_path_characters: int
    zip_regular_files_only: bool

FROZEN_PAYLOAD_POLICY = ComponentPayloadPolicy(
    "frozen-portable", 512, 1024, 256 * 1024 * 1024, 64 * 1024 * 1024,
    128 * 1024, None, 1024, False)
ORGANIZER_INTERPRETER_PAYLOAD_POLICY = ComponentPayloadPolicy(
    "interpreter-portable", 5120, 5121, 384 * 1024 * 1024, 384 * 1024 * 1024,
    1024 * 1024, 6144, 512, True)

def select_payload_policy(component_id, os_name, architecture, runtime_kind="frozen-portable"):
    if (type(component_id) is not ComponentId or os_name not in ("windows", "linux")
            or architecture not in ("x86_64", "aarch64")):
        raise ValueError("payload_policy_expectations")
    if runtime_kind == "frozen-portable":
        return FROZEN_PAYLOAD_POLICY
    if (runtime_kind == "interpreter-portable" and component_id.value == "collectigen.media-organizer"
            and os_name == "windows" and architecture == "x86_64"):
        return ORGANIZER_INTERPRETER_PAYLOAD_POLICY
    raise ValueError("unsupported_payload_policy")

def require_payload_policy(policy, component_id=None, platform=None):
    if policy is None:
        return FROZEN_PAYLOAD_POLICY
    if policy is not FROZEN_PAYLOAD_POLICY and policy is not ORGANIZER_INTERPRETER_PAYLOAD_POLICY:
        raise ValueError("finite_payload_policy_required")
    if component_id is not None and policy is ORGANIZER_INTERPRETER_PAYLOAD_POLICY:
        if component_id.value != "collectigen.media-organizer":
            raise ValueError("payload_policy_component")
        if platform is not None and platform != {"os": "windows", "architecture": "x86_64"}:
            raise ValueError("payload_policy_platform")
    return policy


_QT_RESOURCE_PREFIXES = frozenset(
    f"Runtime/python/Lib/site-packages/PySide6/qml/Qt/labs/assetdownloader/{build}/QmlAssetDownloaderPrivate_resources_1/.qt"
    for build in ("objects-Debug", "objects-RelWithDebInfo"))

_INTERPRETER_PART = re.compile(r"[A-Za-z0-9_+@][A-Za-z0-9_.+@-]{0,127}\Z")


def payload_relative(value, *, payload_policy=None):
    """Validate immutable payload members, never remote URLs or locators.

    Only explicitly admitted interpreter payloads gain stock Qt '+' segment
    prefixes, pip '@' names and two exact upstream Qt resource prefixes.
    Frozen callers retain the original validator.
    """
    policy = require_payload_policy(payload_policy)
    if policy is FROZEN_PAYLOAD_POLICY:
        return _relative(value)
    if type(value) is not str or not value:
        raise ValueError("invalid_payload_relative_path")
    if len(value) > policy.max_relative_path_characters:
        raise ValueError("payload_path_bounds")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or not 1 <= len(path.parts) <= 16:
        raise ValueError("invalid_payload_relative_path")
    for index, part in enumerate(path.parts):
        vendor_qt = part == ".qt" and "/".join(path.parts[:index + 1]) in _QT_RESOURCE_PREFIXES
        if ((not _INTERPRETER_PART.fullmatch(part) and not vendor_qt) or part in {".", ".."}
                or part.endswith(".") or part.split(".", 1)[0].casefold() in _RESERVED):
            raise ValueError("invalid_payload_relative_path")
    return path.parts
