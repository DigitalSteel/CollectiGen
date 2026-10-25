"""Pure exact batch scope shared by owning-page completion controls."""
from dataclasses import dataclass, replace
from pathlib import Path
import re


@dataclass(frozen=True)
class InterruptedAddReceipt:
    """Recorded, read-only transfer evidence; not a fresh filesystem verdict."""

    operation_id: str
    group_id: str
    mode: str
    files: tuple[tuple[Path, Path, str], ...]


@dataclass(frozen=True)
class AddRecoveryResult:
    """One exact Add family examined by recovery, not a batch resume."""

    operation_id: str
    group_id: str
    mode: str
    status: str  # completed, rolled_back, or held
    receipt: InterruptedAddReceipt | None = None
    reason: str = ""  # Recorded recovery verdict; not a fresh filesystem check.


@dataclass(frozen=True)
class AddRecoveryInspection:
    """Fresh exact-family observations, admitted again before reconciliation."""

    operation_id: str
    group_id: str
    receipt: InterruptedAddReceipt
    fingerprint: str
    source_states: tuple[str, ...]
    target_states: tuple[str, ...]
    ready: bool
    reason: str


def read_interrupted_add_receipt(journal, scope) -> InterruptedAddReceipt:
    """Read only the exact held Add Images operation and verified snapshot."""

    operation_id = scope.recovery_operation_id
    if not re.fullmatch(r"op_[0-9a-f]{32}", operation_id) or not scope.group_id.startswith("ingest-add-"):
        raise ValueError("The exact Add Images recovery identity is unavailable.")
    operation = next((item for item in journal.operations() if item.id == operation_id), None)
    if operation is None or operation.status != "applying":
        raise ValueError("This transfer is no longer held at its recorded state. Reopen CollectiGen to inspect recovery.")
    if operation.kind not in {"file_copy", "file_move"} or operation.metadata.get("group_id") != scope.group_id:
        raise ValueError("The held transfer does not match this Add Images batch.")
    if not operation.changes:
        raise ValueError("The held transfer has no recorded file-family snapshot.")
    try:
        payload = journal.load_verified_snapshot(operation.changes[-1]["after"], max_bytes=8 * 1024 * 1024)
        values = payload["files"]
        if not isinstance(values, list) or not values or len(values) > 10000:
            raise ValueError("The recorded file family is unavailable.")
        files = []
        for value in values:
            source, target, sha256 = value["source"], value["target"], value["sha256"]
            if (not isinstance(source, str) or not isinstance(target, str)
                    or not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256)
                    or not Path(source).is_absolute() or not Path(target).is_absolute()):
                raise ValueError("The recorded file family is invalid.")
            files.append((Path(source), Path(target), sha256))
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"The exact recorded transfer details cannot be verified: {exc}") from exc
    return InterruptedAddReceipt(operation_id, scope.group_id,
        "copy" if operation.kind == "file_copy" else "move", tuple(files))


def _ids(values):
    result = tuple(dict.fromkeys(values))
    if len(result) > 10000 or any(not isinstance(value, str) or not value or len(value) > 128 for value in result):
        raise ValueError("The recorded organization scope is unsupported; choose exact operations in History.")
    return result


@dataclass(frozen=True)
class OrganizationHistoryScope:
    group_id: str = ""
    operation_ids: tuple[str, ...] = ()
    paths: tuple[Path, ...] = ()
    recovery_operation_id: str = ""

    @classmethod
    def from_result(cls, result):
        operations = _ids(result.get("organization_operation_ids", ()))
        group = result.get("organization_group_id", "")
        recovery = result.get("recovery_operation_id", "")
        if not isinstance(group, str) or len(group) > 128 or not isinstance(recovery, str) or len(recovery) > 128:
            raise ValueError("The recorded organization batch identity is invalid.")
        if len(operations) > 1000 and not group:
            raise ValueError("A large organization result needs its exact recorded batch identity.")
        paths = tuple(dict.fromkeys(Path(value) for value in result.get("organization_paths", ())))
        if len(paths) > 20000 or any(not path.is_absolute() for path in paths):
            raise ValueError("Organization completion contains unsupported image paths.")
        return cls(group, operations, paths, recovery)

    @property
    def available(self):
        return bool(self.operation_ids)

    def browser_kwargs(self):
        if not self.available:
            raise ValueError("This result has no completed recorded organization operation.")
        # Native inverses retain their original group. Large batches are paged by
        # that exact group, never by a truncated list or a global latest lookup.
        return {"operation_targets": self.operation_ids if len(self.operation_ids) <= 1000 else (),
            "group_id": self.group_id}

    def advanced(self, outcomes):
        replacements = {}
        paths = list(self.paths)
        recovery = self.recovery_operation_id
        for outcome in outcomes:
            if outcome.operation_id not in self.operation_ids:
                continue
            if outcome.status == "complete":
                if outcome.inverse_operation_id:
                    replacements[outcome.operation_id] = outcome.inverse_operation_id
                if outcome.current_path is not None:
                    paths.append(Path(outcome.current_path))
            elif outcome.status == "recovery_required":
                recovery = outcome.operation_id
        return replace(self, operation_ids=_ids(replacements.get(value, value) for value in self.operation_ids),
            paths=tuple(dict.fromkeys(paths)), recovery_operation_id=recovery)
