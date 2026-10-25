"""Native Category retirement plans, exact receipts and private publication.

These receipts stay in the existing Category backup directory. They deliberately
do not use legacy ``pending``: old recovery does not understand byte identities.
Unresolved receipts require explicit reconciliation, including when every write
finished. The private publisher requires its native owner's retained save
exclusion and fresh reviewed plan. No UI retry/replay command is enabled.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .knowledge_write_admission import (
    MAX_PARTICIPANT_BYTES, MAX_RECEIPT_BYTES, _checked_path, _read,
)
from .model_resource_registry import _json


KIND = "category_tag_retirement"
PARTICIPANTS = ("categories.json", "tags.json", "patterns.json", "relationships.json")
STATUSES = frozenset(("retirement_pending", "retirement_complete", "retirement_restored"))
MAX_RECEIPTS = 4096
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_OP = re.compile(r"op_[0-9a-f]{32}\Z")


class CategoryRetirementHold(RuntimeError):
    """Preserve receipts/backups and do not start another recovery writer."""


@dataclass(frozen=True)
class CategoryRetirementState:
    operation_id: str
    plan_sha256: str
    receipt_sha256: str
    outcome: str
    participants: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class CategoryRetirementPlan:
    """Exact detached preview, not permission to save or replay an attempt."""

    authority_key: str
    source_sha256: str
    target_ids: tuple[str, ...]
    participants: tuple[tuple[str, str | None, str | None], ...]
    payloads: tuple[tuple[str, bytes | None], ...]

    def declaration(self) -> dict:
        return {"authority_key": self.authority_key, "source_sha256": self.source_sha256,
            "target_ids": list(self.target_ids), "participants": [
                {"name": name, "before": before, "after": after}
                for name, before, after in self.participants]}

    @property
    def plan_sha256(self) -> str:
        return _digest(self.declaration())


def source_pin(paths) -> str:
    """Bounded named source observation; no publication or enrollment."""
    return _source_pin(paths, {})


def _source_pin(paths, overrides) -> str:
    """One codec for current observations and exact before-byte reconstruction."""
    declaration = []
    for path in paths:
        if len(declaration) >= 64:
            raise CategoryRetirementHold("The retirement source declaration exceeds the supported bound.")
        path = Path(path)
        if not path.is_absolute() or ".." in path.parts:
            raise CategoryRetirementHold("A retirement source must have an exact absolute path.")
        name = os.path.normcase(str(path))
        # None is a recorded absence, not an instruction to read current bytes.
        token = overrides[name] if name in overrides else _sha(_read(path, MAX_PARTICIPANT_BYTES))
        declaration.append((name, token))
    return _digest(declaration)


def _reconstruct_retirement_source_pin(directory, paths, participants) -> str:
    """Reconstruct the original pin using a decoded receipt's exact participants.

    The current code-owned source recipe supplies path order and duplicates.
    The receipt never supplies source paths. Equality with its original pin
    proves that recipe and every unwritten source still match the reviewed save.
    This is an observation, not permission to release a pending reference.
    """
    root = _root(directory)
    overrides = {os.path.normcase(str(root / row["name"])): row["before"]
                 for row in participants}
    paths = tuple(paths)
    if not set(overrides).issubset(os.path.normcase(str(Path(path))) for path in paths):
        raise CategoryRetirementHold("The current source recipe omits a retirement participant.")
    return _source_pin(paths, overrides)


def _native_payload_bytes(payload) -> bytes | None:
    if payload is None:
        return None
    # Existing native serializers use write_text(newline=None); their persisted
    # Windows bytes contain CRLF, not the unmodified LF preview string.
    text = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    return text.replace("\n", os.linesep).encode("utf-8")


def _plan_native_retirement(directory, target_ids, source_sha256, payloads) -> CategoryRetirementPlan:
    root = _root(directory)
    targets = _retirement_target_ids(target_ids)
    if not isinstance(source_sha256, str) or not _SHA.fullmatch(source_sha256):
        raise CategoryRetirementHold("Choose exact valid Tag identities and a current retirement source.")
    by_name = {}
    for path, payload in payloads.items():
        path = Path(path)
        if path.parent != root or path.name not in PARTICIPANTS or path.name in by_name:
            raise CategoryRetirementHold("Native retirement participants must stay in the bound Category authority.")
        after = _native_payload_bytes(payload)
        if after is not None and len(after) > MAX_PARTICIPANT_BYTES:
            raise CategoryRetirementHold("A native retirement participant exceeds the supported size limit.")
        by_name[path.name] = (_sha(_read(path, MAX_PARTICIPANT_BYTES)), after)
    if not {"categories.json", "tags.json"}.issubset(by_name):
        raise CategoryRetirementHold("Native retirement must include Categories and Tags.")
    rows, saved = [], []
    for name in PARTICIPANTS:
        if name in by_name:
            before, after = by_name[name]
            rows.append((name, before, _sha(after)))
            saved.append((name, after))
    if not any(before != after for _, before, after in rows):
        raise CategoryRetirementHold("The native retirement preview contains no saved change.")
    plan = CategoryRetirementPlan(retirement_authority_key(root), source_sha256, targets, tuple(rows), tuple(saved))
    # Prevent constructing an intent too large for its eventual bounded receipt.
    if len(json.dumps(plan.declaration(), ensure_ascii=False).encode("utf-8")) > MAX_RECEIPT_BYTES // 2:
        raise CategoryRetirementHold("The retirement plan exceeds the bounded receipt limit.")
    return plan


def _sha(raw: bytes | None) -> str | None:
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def _root(directory: str | Path) -> Path:
    root = Path(directory)
    if not root.is_absolute() or ".." in root.parts:
        raise CategoryRetirementHold("Bind the exact absolute Category authority folder.")
    _checked_path(root, directory=True, required=True)
    return root


def retirement_authority_key(directory: str | Path) -> str:
    """Path binding only, not admission or entity incarnation."""
    root = _root(directory)
    declaration = (os.path.normcase(str(root)), PARTICIPANTS)
    return hashlib.sha256(json.dumps(declaration, separators=(",", ":")).encode("utf-8")).hexdigest()


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _fingerprint(value) -> bool:
    return value is None or (isinstance(value, str) and _SHA.fullmatch(value) is not None)


def _stable_id(value) -> bool:
    # Tag identity is opaque (UUIDs, generated template IDs, and retained IDs),
    # never a filename. Match continuation bounds rather than guessing a codec.
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= 512
    except UnicodeError:
        return False


def _retirement_target_ids(values) -> tuple[str, ...]:
    targets = []
    for value in values:
        if len(targets) >= 1000 or not _stable_id(value):
            raise CategoryRetirementHold("Choose a bounded set of exact valid retirement Tag identities.")
        targets.append(value)
    if not targets or len(set(targets)) != len(targets):
        raise CategoryRetirementHold("Choose unique exact retirement Tag identities.")
    return tuple(sorted(targets))


def _decode(root: Path, path: Path, raw: bytes) -> dict:
    value = _json(raw)
    keys = {"schema_version", "kind", "status", "operation_id", "plan", "plan_sha256", "backups"}
    if (not isinstance(value, dict) or set(value) != keys
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["kind"] != KIND or value["status"] not in STATUSES
            or not isinstance(value["operation_id"], str)
            or not _OP.fullmatch(value["operation_id"])
            or path.parent.name != value["operation_id"]):
        raise CategoryRetirementHold("The Category retirement receipt has an unsupported identity or format.")
    plan = value["plan"]
    if (not isinstance(plan, dict)
            or set(plan) != {"authority_key", "source_sha256", "target_ids", "participants"}
            or plan["authority_key"] != retirement_authority_key(root)
            or not isinstance(plan["source_sha256"], str) or not _SHA.fullmatch(plan["source_sha256"])
            or not isinstance(plan["target_ids"], list) or not 1 <= len(plan["target_ids"]) <= 1000
            or any(not _stable_id(item) for item in plan["target_ids"])
            or plan["target_ids"] != sorted(set(plan["target_ids"]))):
        raise CategoryRetirementHold("The Category retirement plan is unavailable or belongs to another authority.")
    rows = plan["participants"]
    if not isinstance(rows, list) or not 2 <= len(rows) <= len(PARTICIPANTS):
        raise CategoryRetirementHold("The Category retirement participants are unsupported.")
    names = []
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"name", "before", "after"}
                or row["name"] not in PARTICIPANTS
                or not _fingerprint(row["before"]) or not _fingerprint(row["after"])):
            raise CategoryRetirementHold("The Category retirement byte identities are unsupported.")
        names.append(row["name"])
    if (len(set(names)) != len(names)
            or names != [name for name in PARTICIPANTS if name in names]
            or not {"categories.json", "tags.json"}.issubset(names)
            or not any(row["before"] != row["after"] for row in rows)
            or not isinstance(value["plan_sha256"], str)
            or value["plan_sha256"] != _digest(plan)):
        raise CategoryRetirementHold("The Category retirement plan does not match its exact participant declaration.")
    backups = value["backups"]
    if not isinstance(backups, list) or len(backups) != len(rows):
        raise CategoryRetirementHold("The Category retirement backup declaration is incomplete.")
    for index, (row, backup) in enumerate(zip(rows, backups)):
        expected_name = f"{index}_{row['name']}" if row["before"] is not None else None
        if (not isinstance(backup, dict) or set(backup) != {"name", "file", "sha256"}
                or backup["name"] != row["name"] or backup["file"] != expected_name
                or backup["sha256"] != row["before"]):
            raise CategoryRetirementHold("The Category retirement backup identity is invalid.")
    return value


def _load(root: Path, operation_id: str) -> tuple[Path, bytes, dict]:
    if not isinstance(operation_id, str) or not _OP.fullmatch(operation_id):
        raise CategoryRetirementHold("Choose an exact Category retirement attempt.")
    path = root / "backups" / "category-workspace" / operation_id / "recovery.json"
    raw = _read(path, MAX_RECEIPT_BYTES)
    if raw is None:
        raise CategoryRetirementHold("The selected Category retirement receipt is missing; do not replay it.")
    return path, raw, _decode(root, path, raw)


def classify_category_retirement(
    directory: str | Path, operation_id: str, *, expected_plan_sha256: str | None = None,
) -> CategoryRetirementState:
    """Observe exact selected receipt, backups and bytes without restoring them.

    Caller retains operation exclusion. Returned observations cannot authorize a
    later write; current state must be rechecked under the eventual save lease.
    """
    try:
        root = _root(directory)
        path, raw, value = _load(root, operation_id)
        if expected_plan_sha256 is not None and value["plan_sha256"] != expected_plan_sha256:
            raise CategoryRetirementHold("The selected Category retirement plan changed; review the current attempt.")
        states = []
        observed = {}
        for row, backup in zip(value["plan"]["participants"], value["backups"]):
            if backup["file"] is not None:
                backup_raw = _read(path.parent / backup["file"], MAX_PARTICIPANT_BYTES)
                if backup_raw is None or _sha(backup_raw) != backup["sha256"]:
                    raise CategoryRetirementHold("A Category retirement backup is missing or changed; preserve the remaining evidence.")
                observed[path.parent / backup["file"]] = _sha(backup_raw)
            target = root / row["name"]
            current = _read(target, MAX_PARTICIPANT_BYTES)
            observed[target] = _sha(current)
            before, after = observed[target] == row["before"], observed[target] == row["after"]
            state = "both" if before and after else "before" if before else "after" if after else "other"
            states.append((row["name"], state))
        # Detect participant or receipt changes during this bounded inspection.
        if (_read(path, MAX_RECEIPT_BYTES) != raw
                or any(_sha(_read(target, MAX_PARTICIPANT_BYTES)) != token for target, token in observed.items())):
            raise CategoryRetirementHold("Category retirement evidence changed during inspection; review it again.")
        all_before = all(state in {"before", "both"} for _, state in states)
        all_after = all(state in {"after", "both"} for _, state in states)
        status = value["status"]
        if any(state == "other" for _, state in states):
            outcome = "diverged"
        elif all_after and status in {"retirement_pending", "retirement_complete"}:
            outcome = "published" if status == "retirement_pending" else "complete"
        elif all_before and status in {"retirement_pending", "retirement_restored"}:
            outcome = "unchanged" if status == "retirement_pending" else "restored"
        else:
            outcome = "interrupted"
        return CategoryRetirementState(operation_id, value["plan_sha256"], _sha(raw), outcome, tuple(states))
    except CategoryRetirementHold:
        raise
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, RecursionError) as exc:
        raise CategoryRetirementHold("Category retirement evidence is unavailable or invalid. Keep receipts and backups unchanged.") from exc


def preflight_category_retirements(directory: str | Path) -> None:
    """Before any recovery writer, hold unresolved typed Category attempts.

    Valid terminal declarations are historical: ordinary later edits need not
    match their old bytes. This is not terminal re-verification or downgrade-safe
    recovery. Legacy receipts are left to their original recovery owner.
    """
    try:
        root = _root(directory)
        backups = root / "backups" / "category-workspace"
        if _checked_path(backups, directory=True) is None:
            return
        count = 0
        with os.scandir(backups) as entries:
            for entry in entries:
                count += 1
                if count > MAX_RECEIPTS:
                    raise CategoryRetirementHold("Category recovery inventory exceeds the bounded startup check; review retained backups.")
                folder = Path(entry.path)
                _checked_path(folder, directory=True, required=True)
                path = folder / "recovery.json"
                raw = _read(path, MAX_RECEIPT_BYTES)
                if raw is None:
                    continue  # Existing pre-manifest backups confer no typed authority.
                value = _json(raw)
                if not isinstance(value, dict):
                    raise CategoryRetirementHold("A Category recovery receipt has an invalid format.")
                status = value.get("status")
                typed_fields = {"plan", "plan_sha256", "backups", "operation_id"}
                if (value.get("kind") != KIND
                        and not (isinstance(status, str) and status.startswith("retirement_"))
                        and not typed_fields.intersection(value)):
                    continue
                value = _decode(root, path, raw)
                if value["status"] in {"retirement_complete", "retirement_restored"}:
                    continue
                state = classify_category_retirement(root, value["operation_id"], expected_plan_sha256=value["plan_sha256"])
                reason = {
                    "published": "All planned files were written, but completion still needs reconciliation. Do not replay the save.",
                    "unchanged": "The attempt is unresolved even though its files match the starting state.",
                    "interrupted": "Only part of the save matches the plan. Do not overwrite it automatically.",
                    "diverged": "Files contain newer or unrecognized data. Preserve them for review.",
                }[state.outcome]
                raise CategoryRetirementHold(
                    "Category retirement recovery needs attention. " + reason
                    + " Keep Category backups and receipts unchanged before retrying."
                )
    except CategoryRetirementHold:
        raise
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, RecursionError) as exc:
        raise CategoryRetirementHold("Category recovery evidence is unavailable or invalid. Keep receipts and backups unchanged.") from exc


def _json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def _make_directory(path: Path) -> None:
    if _checked_path(path, directory=True) is None:
        path.mkdir()
    _checked_path(path, directory=True, required=True)


def _create_file(path: Path, raw: bytes) -> None:
    """Exclusive evidence publication; uncertain partial files remain held."""
    _checked_path(path.parent, directory=True, required=True)
    _checked_path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    if _read(path, max(MAX_RECEIPT_BYTES, len(raw))) != raw:
        raise CategoryRetirementHold("Category recovery evidence failed publication read-back.")


def _replace_file(path: Path, raw: bytes, expected: str | None) -> None:
    """Replace only exact expected bytes under the native owner's exclusion."""
    _checked_path(path.parent, directory=True, required=True)
    if _sha(_read(path, MAX_PARTICIPANT_BYTES)) != expected:
        raise CategoryRetirementHold("A Category participant changed before publication; preserve newer data.")
    descriptor, name = tempfile.mkstemp(prefix=".category-retirement-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if (_read(temporary, MAX_PARTICIPANT_BYTES) != raw
                or _sha(_read(path, MAX_PARTICIPANT_BYTES)) != expected):
            raise CategoryRetirementHold("Category publication evidence changed before replacement.")
        os.replace(temporary, path)
        if _read(path, MAX_PARTICIPANT_BYTES) != raw:
            raise CategoryRetirementHold("Category publication failed exact read-back.")
    finally:
        if temporary.exists():
            _checked_path(temporary, required=True)
            temporary.unlink()


def _delete_file(path: Path, expected: str) -> None:
    if _sha(_read(path, MAX_PARTICIPANT_BYTES)) != expected:
        raise CategoryRetirementHold("A Category participant changed before removal; preserve newer data.")
    path.unlink()
    if _read(path, MAX_PARTICIPANT_BYTES) is not None:
        raise CategoryRetirementHold("Category participant absence failed read-back.")


def _clear_reference(path: Path, expected_raw: bytes) -> None:
    if _read(path, MAX_RECEIPT_BYTES) != expected_raw:
        raise CategoryRetirementHold("The Category pending reference changed; do not clear unrelated recovery work.")
    _delete_file(path, _sha(expected_raw))


def _publish_native_retirement(directory, pending_root, plan, operation_id, reload_authority, verify_sources) -> Path:
    """Private Category-owned command; caller retains native save exclusion.

    ``reload_authority(after)`` verifies detached authority, not shared adoption.
    Successful publication retains the reference for an application-owned handoff.
    Never invoke ordinary nested writers after installing the reference.
    """
    root = _root(directory)
    if (not isinstance(operation_id, str) or not _OP.fullmatch(operation_id)
            or not isinstance(plan, CategoryRetirementPlan)
            or plan.authority_key != retirement_authority_key(root)
            or Path(pending_root) != root / ".automation-rule-conversion-pending"):
        raise CategoryRetirementHold("Choose an exact native retirement plan and a fresh attempt identity.")
    pending_root = Path(pending_root)
    backups_root = root / "backups"
    owner_root = backups_root / "category-workspace"
    folder = owner_root / operation_id
    entries = [{"name": name, "file": f"{index}_{name}" if before is not None else None,
        "sha256": before} for index, (name, before, _) in enumerate(plan.participants)]
    manifest = {"schema_version": 1, "kind": KIND, "status": "retirement_pending",
        "operation_id": operation_id, "plan": plan.declaration(),
        "plan_sha256": plan.plan_sha256, "backups": entries}
    path = folder / "recovery.json"
    pending_raw = _json_bytes(manifest)
    _decode(root, path, pending_raw)
    if len(pending_raw) > MAX_RECEIPT_BYTES:
        raise CategoryRetirementHold("The Category retirement receipt exceeds its supported bound.")
    if (not isinstance(plan.payloads, tuple)
            or tuple(name for name, _ in plan.payloads) != tuple(name for name, _, _ in plan.participants)
            or any(raw is not None and (not isinstance(raw, bytes) or len(raw) > MAX_PARTICIPANT_BYTES)
                for _, raw in plan.payloads)
            or any(_sha(raw) != after for (_, raw), (_, _, after) in zip(plan.payloads, plan.participants))):
        raise CategoryRetirementHold("The native payload declaration differs from its fixed retirement plan.")
    _make_directory(backups_root)
    _make_directory(owner_root)
    _checked_path(folder, directory=True)
    folder.mkdir()  # No replay or replacement of an existing attempt.
    for (name, before, _), backup in zip(plan.participants, entries):
        saved = _read(root / name, MAX_PARTICIPANT_BYTES)
        if _sha(saved) != before:
            raise CategoryRetirementHold("The reviewed retirement source changed before backup.")
        if backup["file"] is not None:
            _create_file(folder / backup["file"], saved)
    _make_directory(pending_root)
    reference = pending_root / f"category-retirement.{operation_id}.json"
    reference_raw = _json_bytes({"schema_version": 1, "kind": "category_retirement_reference",
        "operation_id": operation_id, "plan_sha256": plan.plan_sha256})
    # Reference first: a crash before receipt publication is an explicit orphan
    # hold, not permission for another writer to use an unrecognized attempt.
    _create_file(reference, reference_raw)
    _create_file(path, pending_raw)
    after_raw = dict(plan.payloads)
    try:
        for name, before, after in plan.participants:
            verify_sources()
            raw = after_raw[name]
            if _sha(raw) != after:
                raise CategoryRetirementHold("The retirement payload does not match its frozen plan.")
            if before == after:
                continue
            if raw is None:
                _delete_file(root / name, before)
            else:
                _replace_file(root / name, raw, before)
        state = classify_category_retirement(root, operation_id, expected_plan_sha256=plan.plan_sha256)
        if state.outcome != "published":
            raise CategoryRetirementHold("Retirement publication did not match every reviewed participant.")
        reload_authority(True)
        verify_sources()
        manifest["status"] = "retirement_complete"
        complete_raw = _json_bytes(manifest)
        _replace_file(path, complete_raw, _sha(pending_raw))
        if classify_category_retirement(root, operation_id, expected_plan_sha256=plan.plan_sha256).outcome != "complete":
            raise CategoryRetirementHold("Retirement completion could not be verified.")
        verify_sources()
        if _read(reference, MAX_RECEIPT_BYTES) != reference_raw:
            raise CategoryRetirementHold("Retirement handoff reference changed; preserve the completed attempt.")
        # Detached read-back cannot refresh retained application consumers. Keep
        # the fast hold across worker return and the next thread's acquisition.
        # Shared-authority adoption/finalization is a separate qualified command.
        return folder
    except BaseException as failure:
        # Never undo a terminal publication, or infer pending from a status alone.
        # A changed reference/receipt or divergent participant stays held.
        try:
            # If removal succeeded but its read-back/return failed, reinstate our
            # exact hold before surfacing uncertainty. Never overwrite another
            # reference or use absence as permission to replay a completed save.
            if _read(reference, MAX_RECEIPT_BYTES) is None:
                _create_file(reference, reference_raw)
            if (_read(reference, MAX_RECEIPT_BYTES) != reference_raw
                    or _read(path, MAX_RECEIPT_BYTES) != pending_raw):
                raise CategoryRetirementHold("Retirement completion is uncertain; preserve the current attempt.")
            state = classify_category_retirement(root, operation_id, expected_plan_sha256=plan.plan_sha256)
            if state.outcome == "diverged":
                raise CategoryRetirementHold("Newer Category data prevents automatic retirement restoration.")
            # All-before-any preflight above checked *all* backups and participants.
            states = dict(state.participants)
            for row, backup in zip(plan.participants, entries):
                name, before, after = row
                if states[name] != "after":
                    continue
                if before is None:
                    _delete_file(root / name, after)
                else:
                    saved = _read(folder / backup["file"], MAX_PARTICIPANT_BYTES)
                    if _sha(saved) != before:
                        raise CategoryRetirementHold("A Category backup changed during restoration.")
                    _replace_file(root / name, saved, after)
            if classify_category_retirement(root, operation_id, expected_plan_sha256=plan.plan_sha256).outcome != "unchanged":
                raise CategoryRetirementHold("Retirement restoration could not be verified.")
            reload_authority(False)
            manifest["status"] = "retirement_restored"
            restored_raw = _json_bytes(manifest)
            _replace_file(path, restored_raw, _sha(pending_raw))
            if classify_category_retirement(root, operation_id, expected_plan_sha256=plan.plan_sha256).outcome != "restored":
                raise CategoryRetirementHold("Restored retirement completion could not be verified.")
            _clear_reference(reference, reference_raw)
        except BaseException as held:
            raise CategoryRetirementHold(
                "Category retirement needs recovery review. Keep its pending reference, receipt and backups unchanged; do not replay the save."
            ) from held
        raise failure
