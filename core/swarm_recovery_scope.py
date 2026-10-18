"""Declarative recovery coverage, never installation or mutation authority.

This module performs no I/O. Privacy, paths and ownership supplied by a caller
remain declarations. Only the canonical archive service adds observed evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re


PROTECTED = ("models", "outputs", "libraries", "local-only")
ROLES = ("host-code", "host-dependency", "extension", "settings", "backends",
         "account-database", "external-reference", "unknown-state")
PRECONDITIONS = (
    "complete-critical-state-inventory", "active-path-and-file-identity",
    "installed-baseline-and-ownership", "consistent-private-state",
    "all-writers-exclusive", "private-storage-and-space",
    "qualified-publication-and-rollback", "fresh-review-and-mutation-authority",
)
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")
_FIELDS = {"item_id", "role", "source", "ownership", "treatment", "root", "relative_path", "classification", "inspection"}


class ScopeError(ValueError):
    """Always generic: never echo supplied private values in diagnostics."""


@dataclass(frozen=True)
class ScopeItem:
    item_id: str
    role: str
    source: str
    ownership: str
    treatment: str
    root: str
    relative_path: str
    classification: str
    inspection: str


@dataclass(frozen=True)
class ScopeDeclaration:
    scope_id: str
    installation_ref: str
    components: tuple[ScopeItem, ...]
    excluded_categories: tuple[str, ...]
    open_questions: tuple[str, ...]


@dataclass(frozen=True)
class FileObservation:
    role: str
    size: int
    sha256: str


@dataclass(frozen=True)
class CoverageObservation:
    kind: str
    reference: str
    status: str
    files: tuple[FileObservation, ...] = ()
    metadata_hashes: tuple[str, ...] = ()
    observed_at: str = ""


@dataclass(frozen=True)
class RecoveryScopeReview:
    declaration: ScopeDeclaration
    observations: tuple[CoverageObservation, ...] = ()

    @property
    def coverage_basis(self):
        return "declared-components-only"

    @property
    def installation_inventory_exhaustive(self):
        return False

    @property
    def installation_recovery_ready(self):
        return False

    @property
    def mutation_allowed(self):
        return False

    @property
    def gaps(self):
        represented = {item.role for item in self.declaration.components}
        return tuple(dict.fromkeys((*PRECONDITIONS, "privacy-classification-not-attested", "current-enrollment-not-checked",
                "declaration-artifact-binding-unverified",
                *(f"undeclared-{role}" for role in ROLES if role not in represented),
                *(f"unresolved-{item.role}" for item in self.declaration.components if item.treatment != "include"),
                *self.declaration.open_questions)))

    @property
    def limitations(self):
        return (
            "Declarations are not an exhaustive installation inventory or privacy attestation.",
            "Configuration archives may contain credentials even when declared ordinary.",
            "Archive integrity does not prove current originals, active paths or historical authenticity.",
            "Observed artifacts are not bound to the declaration's installation reference or logical targets.",
            "Recorded detached ownership does not verify current package bytes or installed ownership.",
            "No original files, package directories, enrollment or runtime were inspected; no replacement is authorized.",
        )


def _token(value):
    return type(value) is str and _TOKEN.fullmatch(value) is not None


def _relative(value):
    if type(value) is not str or len(value) > 2048:
        raise ScopeError("Invalid declared relative target.")
    if not value:
        return ""
    parts = value.replace("\\", "/").split("/")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if any(not part or part in {".", ".."} or part != part.rstrip(" .")
           or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF or c in '<>:"|?*' for c in part)
           or part.split(".")[0].upper() in reserved for part in parts):
        raise ScopeError("Invalid declared relative target.")
    return "/".join(parts)


def validate_scope(value) -> ScopeDeclaration:
    """Validate bounded declarations without following paths or accepting proof."""
    if (type(value) is not dict or set(value) != {"schema_version", "scope_id", "installation_ref", "components", "excluded_categories", "open_questions"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not _token(value["scope_id"]) or not _token(value["installation_ref"])):
        raise ScopeError("Invalid recovery scope identity or schema.")
    items, exclusions, questions = value["components"], value["excluded_categories"], value["open_questions"]
    if (type(items) is not list or len(items) > 64 or type(exclusions) is not list or len(exclusions) != len(PROTECTED)
            or any(type(item) is not str for item in exclusions) or set(exclusions) != set(PROTECTED)
            or type(questions) is not list or len(questions) > len(PRECONDITIONS)
            or any(type(item) is not str or item not in PRECONDITIONS for item in questions)
            or len(set(questions)) != len(questions)):
        raise ScopeError("Protected exclusions, component count or unresolved conditions are invalid.")
    parsed, identities, targets = [], set(), set()
    choices = dict(role=ROLES, source=("declared", "configured", "runtime-observed"),
                   ownership=("managed", "user-owned", "unknown"), treatment=("include", "preserve", "exclude", "unresolved"),
                   classification=("ordinary", "credential-bearing", "unknown"), inspection=("none", "selected-artifact"))
    metadata_bytes = 0
    for item in items:
        if (type(item) is not dict or set(item) != _FIELDS or not _token(item["item_id"]) or not _token(item["root"])
                or item["root"].lower() in PROTECTED
                or any(type(item[key]) is not str or item[key] not in allowed for key, allowed in choices.items())):
            raise ScopeError("Invalid or protected scope component; values are not reported.")
        relative = _relative(item["relative_path"])
        metadata_bytes += sum(len(part.encode("utf-8")) for part in item.values())
        if metadata_bytes > 128 * 1024:
            raise ScopeError("Declared scope metadata exceeds its bound.")
        identity = item["item_id"].casefold()
        target = (item["root"].casefold(), relative.casefold())
        if (identity in identities or (relative and any(root == target[0] and
                (path == target[1] or path.startswith(target[1] + "/") or target[1].startswith(path + "/")) for root, path in targets))):
            raise ScopeError("Declared identities or targets overlap.")
        if item["treatment"] == "include" and not relative:
            raise ScopeError("Included components need an exact declared relative target.")
        if item["inspection"] != "none" and item["role"] not in {"settings", "backends", "extension"}:
            raise ScopeError("This component has no supported selected-artifact observation.")
        identities.add(identity)
        if relative:
            targets.add(target)
        parsed.append(ScopeItem(**{**item, "relative_path": relative}))
    if any(sum(item.role == role for item in parsed) > 1 for role in ("settings", "backends")):
        raise ScopeError("Configuration roles may only be declared once.")
    if len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 128 * 1024:
        raise ScopeError("Declared scope metadata exceeds its bound.")
    return ScopeDeclaration(value["scope_id"], value["installation_ref"], tuple(parsed), tuple(PROTECTED), tuple(questions))


def validate_selections(scope: ScopeDeclaration, backup_id, companion_ids):
    if (backup_id is not None and (type(backup_id) is not str or not _ID.fullmatch(backup_id))
            or type(companion_ids) not in (list, tuple) or len(companion_ids) > 4
            or any(type(item) is not str or not _ID.fullmatch(item) for item in companion_ids)
            or len(set(companion_ids)) != len(companion_ids)):
        raise ScopeError("Invalid or excessive canonical evidence selection.")
    admitted = {item.role for item in scope.components if item.inspection == "selected-artifact"
                and item.treatment in {"include", "preserve"} and item.classification != "unknown"}
    if ((backup_id is not None and not {"settings", "backends"} <= admitted)
            or (companion_ids and "extension" not in admitted)):
        raise ScopeError("Explicit classified artifact selection is required for every selected role; classification remains a declaration.")
    return tuple(companion_ids)
