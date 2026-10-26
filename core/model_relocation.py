"""Explicit standalone-model organization with exact registry/file recovery.

Product fit: reuse model identity and saved folder policy to organize selected
copies, preserving other locations, engine configuration and recovery authority.
No discovery, model loading, network IO or constructor filesystem work.
"""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from uuid import uuid4

from .external_write_transaction import write_verified_json
from .model_file_retirement import pinned_model_retirement
from .model_organization import destination_path
from .model_resource_registry import (
    ModelResourceError, ResourceInspection, RESOURCE_ROLES, _bounded_read, _bytes, _cancel,
    _identity, _json, inspect_model_resource,
)
from .model_workspace import (_path, _pinned_model_file, _pinned_parent,
    _immutable_operation_reference, _notify_operation_started)

MAX_SELECTION = 256
MAX_OPERATIONS = 2000
COMPANIONS_NOTICE = (
    "Move only this standalone model file. Adjacent artwork, licenses and provider "
    "sidecars stay in place. VAEs, encoders and other models need their own selection."
)
ENGINE_NOTICE = (
    "Moving this location may break engine paths or saved workflows. Engine settings "
    "are not changed; review their model folders separately. Other consumers are unknown."
)


def _key(path):
    return str(Path(path).absolute()).casefold()


def _digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _fingerprint(value):
    return hashlib.sha256(json.dumps(asdict(value), sort_keys=True, default=str).encode()).hexdigest()


def _settings(value):
    workspace = value.get("workspace", {})
    return _digest({key: workspace.get(key) for key in
        ("default_destination", "destination_overrides", "family_destinations")})


@dataclass(frozen=True)
class RelocationSelection:
    resource_id: str
    source: Path
    role: str
    family: str
    media_type: str = "Images"


@dataclass(frozen=True)
class RelocationRow:
    row_id: str
    selection: RelocationSelection
    destination: Path | None
    destination_reason: str
    status: str
    reason: str
    companions_notice: str = COMPANIONS_NOTICE
    engine_notice: str = ENGINE_NOTICE
    inspection: ResourceInspection | None = None
    ancestor: Path | None = None
    ancestor_identity: tuple = ()


@dataclass(frozen=True)
class RelocationPreview:
    preview_id: str
    registry_token: str
    settings_digest: str
    rows: tuple[RelocationRow, ...]


@dataclass(frozen=True)
class RelocationOutcome:
    row_id: str
    operation_id: str
    status: str
    message: str
    source: Path
    destination: Path | None


@dataclass(frozen=True)
class RelocationRecovery:
    preview_id: str
    operation_id: str
    registry_token: str
    baseline_digest: str
    status: str
    source: Path
    destination: Path
    message: str
    layout_changed: bool = False
    settings_digest: str = ""


class ModelRelocationService:
    def __init__(self, registry, *, protected_roots=(), managed_roots=(), recorded_engine_roots=()):
        self.registry = registry
        self.protected_roots = tuple(protected_roots)
        self.managed_roots = tuple(Path(p).absolute() for p in managed_roots)
        self.recorded_engine_roots = tuple(Path(p).absolute() for p in recorded_engine_roots)
        self._issued = {}

    def _issue(self, preview):
        if len(self._issued) >= 32:
            self._issued.pop(next(iter(self._issued)))
        self._issued[preview.preview_id] = _fingerprint(preview)
        return preview

    def _accept(self, preview, kind):
        if not isinstance(preview, kind) or self._issued.get(preview.preview_id) != _fingerprint(preview):
            raise ModelResourceError("Use an unchanged preview from this Models session; preview again.")

    def _guard(self, path):
        path = _path(path, self.protected_roots)
        if any(path == root or root in path.parents or path in root.parents for root in self.managed_roots):
            raise ModelResourceError("This is a managed installation. Use its Component Manager or a standalone copy.")
        # Inspect only exact known manifest names, never enumerate ancestors.
        for parent in path.parents:
            if (parent / "collectigen-training-manifest.json").exists():
                raise ModelResourceError("This location belongs to a training package. Choose a standalone copy outside the package.")
        return path

    @staticmethod
    def _operations(value):
        operations = value.get("model_relocations", {})
        phases = {"prepared", "verified_staged", "admitted", "retiring_source", "source_retired", "complete", "abandoned"}
        if not isinstance(operations, dict) or len(operations) > MAX_OPERATIONS:
            raise ModelResourceError("Relocation history is invalid or unsupported. Preserve the registry for review.")
        for key, marker in operations.items():
            if (not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key)
                    or not isinstance(marker, dict) or marker.get("schema_version") != 1
                    or marker.get("phase") not in phases or not isinstance(marker.get("resource_id"), str)
                    or not isinstance(marker.get("baseline_digest"), str)):
                raise ModelResourceError("Relocation history is invalid or unsupported. Preserve the registry for review.")
        return operations

    def _row(self, selection, value, *, should_cancel=None, progress=None):
        if not isinstance(selection, RelocationSelection) or not isinstance(selection.source, Path):
            raise ModelResourceError("Choose an exact registered file location and declaration.")
        if (not isinstance(selection.resource_id, str) or selection.role not in RESOURCE_ROLES
                or not isinstance(selection.family, str) or not selection.family.strip() or len(selection.family) > 160):
            raise ModelResourceError("Choose a supported role and one recorded model family.")
        row_id = _digest({**asdict(selection), "source": str(selection.source)})[:24]
        target, rule, inspection, ancestor, parent_id = None, "", None, None, ()
        try:
            if os.name != "nt":
                raise ModelResourceError("Model relocation requires the qualified Windows file boundary.")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", selection.resource_id):
                raise ModelResourceError("Managed packs are not standalone files. Use their Component Manager.")
            source = self._guard(selection.source)
            record = value["resources"].get(selection.resource_id)
            if record is None or not any(_key(row["path"]) == _key(source) for row in record["locations"]):
                raise ModelResourceError("The exact source location is not registered. Locate an exact copy first.")
            if not any(d.get("role") == selection.role and d.get("family") == selection.family for d in record["declarations"]):
                raise ModelResourceError("Choose one recorded role and family for this model.")
            if selection.media_type not in ("Images", "Video"):
                raise ModelResourceError("Choose Images or Video explicitly.")
            if re.search(r"-\d{5}-of-\d{5}\.safetensors$", source.name, re.I):
                raise ModelResourceError("This appears to be a weight shard. Keep its complete managed bundle together.")
            for name in ("model.safetensors.index.json", "diffusion_pytorch_model.safetensors.index.json", "model_index.json"):
                if (source.parent / name).exists():
                    raise ModelResourceError("This folder contains a model bundle. Use a standalone model copy.")
            if source.stat(follow_symlinks=False).st_nlink != 1:
                raise ModelResourceError("This file has linked copies. Choose an independent standalone copy; aliases are not moved.")
            destination, rule = destination_path(value.get("workspace", {}), role=selection.role,
                family=selection.family, media_type=selection.media_type)
            if not destination:
                raise ModelResourceError("Configure this model's family/type destination in Setup Folders, then preview again.")
            target = self._guard(Path(destination) / source.name)
            if _key(source) == _key(target):
                return RelocationRow(row_id, selection, target, rule, "noop", "Already in its configured destination.")
            if target.exists():
                raise ModelResourceError("Destination occupied. Nothing is replaced or renamed; use Locate Exact Copy for duplicates.")
            ancestor = target.parent
            while not ancestor.exists() and ancestor != ancestor.parent:
                ancestor = ancestor.parent
            _path(ancestor, self.protected_roots)
            state = ancestor.stat(follow_symlinks=False)
            if not ancestor.is_dir() or int(getattr(state, "st_file_attributes", 0)) & (0x1000 | 0x40000 | 0x400000):
                raise ModelResourceError("Choose an available local destination folder.")
            parent_id = (state.st_dev, state.st_ino)
            inspection = inspect_model_resource(source, should_cancel=should_cancel, progress=progress)
            if inspection.resource_id != selection.resource_id or inspection.stored_identity() != record["identity"]:
                raise ModelResourceError("This location's bytes no longer match its registered identity. Locate an exact copy.")
            if shutil.disk_usage(ancestor).free < inspection.size + 16 * 1024**2:
                raise ModelResourceError("Not enough destination storage for the verified copy. Choose another folder.")
            for marker in self._operations(value).values():
                if marker.get("resource_id") == selection.resource_id and marker.get("phase") not in ("complete", "abandoned"):
                    raise ModelResourceError("This model has an unfinished relocation. Inspect its recorded recovery first.")
            # A DELETE-capable pin is a read-only availability probe here.
            with pinned_model_retirement(source, inspection.file_state, protected_roots=self.protected_roots):
                pass
            origins = [str(root) for root in self.recorded_engine_roots if source == root or root in source.parents]
            notice = ENGINE_NOTICE + (" Recorded engine folders: " + "; ".join(origins) if origins else "")
            return RelocationRow(row_id, selection, target, rule, "ready", "Exact registered file ready for reviewed relocation.",
                engine_notice=notice, inspection=inspection, ancestor=ancestor, ancestor_identity=parent_id)
        except InterruptedError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            return RelocationRow(row_id, selection, target, rule, "held", str(exc))

    def preview(self, selections, *, expected_registry, should_cancel=None, progress=None):
        from itertools import islice
        selections = tuple(islice(iter(selections), MAX_SELECTION + 1))
        if not 1 <= len(selections) <= MAX_SELECTION:
            raise ModelResourceError(f"Choose between 1 and {MAX_SELECTION} exact model locations.")
        value, token = self.registry.snapshot()
        if token != expected_registry:
            raise ModelResourceError("The registry changed. Reload before previewing relocation.")
        rows = []
        for selection in selections:
            _cancel(should_cancel)
            rows.append(self._row(selection, value, should_cancel=should_cancel, progress=progress))
        sources = [_key(row.selection.source) for row in rows]
        targets = [_key(row.destination) if row.destination else None for row in rows]
        if len(set(sources)) != len(sources) or len({row.row_id for row in rows}) != len(rows):
            raise ModelResourceError("Choose each exact source location once.")
        ready_targets = [target for row, target in zip(rows, targets) if row.status == "ready"]
        if len(set(ready_targets)) != len(ready_targets) or any(target in sources for target in ready_targets):
            raise ModelResourceError("Selected destinations collide or overlap selected sources. Review separate locations.")
        if self.registry.snapshot()[1] != token:
            raise ModelResourceError("The registry changed during preview. Preview again.")
        return self._issue(RelocationPreview(uuid4().hex, token, _settings(value), tuple(rows)))

    def apply(self, preview, selected_row_ids, *, confirmed=False, should_cancel=None, progress=None, operation_started=None):
        if not confirmed:
            raise ModelResourceError("Confirm the exact selected source-to-destination preview first.")
        self._accept(preview, RelocationPreview)
        from itertools import islice
        selected = tuple(islice(iter(selected_row_ids), MAX_SELECTION + 1))
        available = {row.row_id: row for row in preview.rows}
        if not selected or len(selected) > MAX_SELECTION or any(not isinstance(key, str) for key in selected) or len(set(selected)) != len(selected) or any(key not in available or available[key].status != "ready" for key in selected):
            raise ModelResourceError("Select only distinct ready rows from this preview.")
        if self.registry.snapshot()[1] != preview.registry_token:
            raise ModelResourceError("The registry or saved folder layout changed. Preview again.")
        del self._issued[preview.preview_id]
        outcomes, token, created_directories = [], preview.registry_token, {}
        for index, key in enumerate(selected):
            row = available[key]
            if should_cancel and should_cancel():
                outcomes.extend(self._outcome(available[k], "", "cancelled", "Not started; source unchanged.") for k in selected[index:])
                break
            try:
                outcome, token = self._apply_one(row, token, preview.settings_digest, should_cancel, progress, created_directories, operation_started)
            except (OSError, RuntimeError, ValueError) as exc:
                outcome = self._outcome(row, "", "cancelled" if isinstance(exc, InterruptedError) else "held", str(exc))
            outcomes.append(outcome)
            if outcome.status != "complete":
                outcomes.extend(self._outcome(available[k], "", "held", "Not started. Resolve the previous result and preview again.") for k in selected[index + 1:])
                break
        return tuple(outcomes)

    @staticmethod
    def _outcome(row, operation, status, message):
        return RelocationOutcome(row.row_id, operation, status, message, row.selection.source, row.destination)

    def _directory(self, operation):
        if not isinstance(operation, str) or not re.fullmatch(r"[0-9a-f]{32}", operation):
            raise ModelResourceError("Choose an exact recorded relocation operation.")
        return _path(self.registry.path.parent / "model-relocation-recovery" / operation, self.protected_roots)

    def _save_phase(self, value, token, operation, phase, **fields):
        value["model_relocations"][operation].update(phase=phase, **fields)
        self.registry._save(value, token)
        saved, token = self.registry.snapshot()
        if saved != value:
            raise ModelResourceError("The registry changed during relocation. Inspect recovery.")
        return token

    def _bind_operation(self, value, token, operation, callback):
        if callback is None:
            return
        baseline, _ = self._load_operation(value, operation)
        path = self._directory(operation) / "baseline.json"
        raw = _bounded_read(path, 512 * 1024)
        if raw is None or _json(raw) != baseline:
            raise ModelResourceError("Relocation receipt changed before binding; inspect recovery.")
        _notify_operation_started(callback, _immutable_operation_reference(
            "model_relocation", operation, path, raw, kind="relocate"))
        if self.registry.snapshot()[1] != token:
            raise ModelResourceError("The registry changed while binding relocation; inspect recovery.")
        if _bounded_read(path, 512 * 1024) != raw:
            raise ModelResourceError("Relocation receipt changed while binding; inspect recovery.")

    def _apply_one(self, row, token, settings_digest, should_cancel, progress, created_directories, operation_started=None):
        operation = ""
        journal_started = False
        try:
            with self.registry._writer():
                value, current = self.registry.snapshot()
                if current != token or _settings(value) != settings_digest:
                    raise ModelResourceError("The registry or saved folder layout changed; preview again.")
                refreshed = self._row(row.selection, value, should_cancel=should_cancel, progress=progress)
                if refreshed != row:
                    from dataclasses import replace
                    # Earlier selected members can create a shared destination
                    # folder. Only this batch's recorded exact directories may
                    # explain a changed nearest ancestor.
                    owned_parent = refreshed.ancestor is not None and created_directories.get(_key(refreshed.ancestor)) == refreshed.ancestor_identity
                    comparable = replace(refreshed, ancestor=row.ancestor, ancestor_identity=row.ancestor_identity)
                    if not owned_parent or comparable != row:
                        raise ModelResourceError("The selected source or destination changed; preview again.")
                    row = refreshed
                if len(self._operations(value)) >= MAX_OPERATIONS:
                    raise ModelResourceError("The retained relocation history is full. Review it before more moves.")
                with _pinned_parent(row.ancestor, self.protected_roots):
                    state = row.ancestor.stat(follow_symlinks=False)
                    if (state.st_dev, state.st_ino) != row.ancestor_identity:
                        raise ModelResourceError("Destination ancestor changed; preview again.")
                    # No file mutation before all preview guards. Empty created
                    # destination folders are retained on cancellation.
                    pending, cursor = [], row.destination.parent
                    while cursor != row.ancestor:
                        pending.append(cursor)
                        cursor = cursor.parent
                    for folder in reversed(pending):
                        _cancel(should_cancel)
                        self._guard(folder)
                        folder.mkdir(exist_ok=False)
                        self._guard(folder)
                        created = folder.stat(follow_symlinks=False)
                        created_directories[_key(folder)] = (created.st_dev, created.st_ino)
                    with _pinned_parent(row.destination.parent, self.protected_roots), pinned_model_retirement(
                        row.selection.source, row.inspection.file_state, protected_roots=self.protected_roots) as source:
                        for folder in (row.destination.parent, *row.destination.parent.parents):
                            expected = created_directories.get(_key(folder))
                            if expected is not None:
                                observed = folder.stat(follow_symlinks=False)
                                if (observed.st_dev, observed.st_ino) != expected:
                                    raise ModelResourceError("A newly created destination folder changed. Preview again.")
                        operation = uuid4().hex
                        directory = self._directory(operation)
                        directory.mkdir(parents=True, exist_ok=False)
                        stage_dir = row.destination.parent / (".collectigen-relocation-" + operation)
                        stage = stage_dir / row.destination.name
                        source_row = next(deepcopy(loc) for loc in value["resources"][row.selection.resource_id]["locations"]
                            if _key(loc["path"]) == _key(row.selection.source))
                        baseline = {"schema_version": 1, "operation_id": operation, "row_id": row.row_id,
                            "selection": {**asdict(row.selection), "source": str(row.selection.source)},
                            "source_state": list(row.inspection.file_state), "source_row": source_row,
                            "identity": row.inspection.stored_identity(), "destination": str(row.destination),
                            "stage": str(stage), "settings_digest": settings_digest,
                            "destination_parent": list((row.destination.parent.stat().st_dev, row.destination.parent.stat().st_ino))}
                        if len(_bytes(baseline)) > 512 * 1024:
                            raise ModelResourceError("This model's recovery evidence exceeds the supported size. Preserve its source.")
                        write_verified_json(directory / "baseline.json", baseline)
                        value.setdefault("model_relocations", {})[operation] = {"schema_version": 1, "baseline_digest": _digest(baseline),
                            "resource_id": row.selection.resource_id, "phase": "prepared"}
                        journal_started = True
                        token = self._save_phase(value, token, operation, "prepared")
                        # Earlier empty destination/recovery folders are retained
                        # scaffolding, never authority to discover an operation.
                        self._bind_operation(value, token, operation, operation_started)
                        _cancel(should_cancel)
                        stage_dir.mkdir(exist_ok=False)
                        with _pinned_parent(stage_dir, self.protected_roots):
                            with stage.open("xb") as writer:
                                source.verify(row.selection.resource_id[7:], row.inspection.size,
                                    should_cancel=should_cancel, progress=progress, destination=writer)
                                writer.flush()
                                os.fsync(writer.fileno())
                            with _pinned_model_file(stage, self.protected_roots):
                                inspection = inspect_model_resource(stage, should_cancel=should_cancel, progress=progress)
                                if inspection.stored_identity() != baseline["identity"]:
                                    raise ModelResourceError("Staged model differs from its registered identity. Inspect recovery.")
                                token = self._save_phase(value, token, operation, "verified_staged", stage_state=list(inspection.file_state))
                                _cancel(should_cancel)
                                os.link(stage, row.destination)  # atomic no-clobber publication
                                # Finalization is deliberately noncancellable. The
                                # next cancellation boundary is after this member.
                                with _pinned_model_file(row.destination, self.protected_roots):
                                    result = inspect_model_resource(row.destination)
                                    if result.file_state != inspection.file_state:
                                        raise ModelResourceError("Published file identity differs. Inspect recovery.")
                                    token = self._admit(value, token, operation, baseline, result)
                                    token = self._save_phase(value, token, operation, "retiring_source")
                                    source.retire()
                                    token = self._remove_source_row(value, token, operation, baseline)
                        # The published location remains; retire only our exact
                        # retained staging link, after destination pins close.
                        token = self._cleanup_stage(value, token, operation, baseline)
                        token = self._complete(value, token, operation, baseline)
                        return self._outcome(row, operation, "complete", "Model moved; the registry now records its verified destination."), token
        except (OSError, RuntimeError, ValueError) as exc:
            if not journal_started:
                raise
            return self._outcome(row, operation, "recovery_required", "Relocation stopped; inspect its recorded recovery. " + str(exc)), token

    def _admit(self, value, token, operation, baseline, inspection):
        record = value["resources"][baseline["selection"]["resource_id"]]
        existing = [loc for loc in record["locations"] if _key(loc["path"]) == _key(inspection.path)]
        destination_row = {**deepcopy(baseline["source_row"]), "path": str(inspection.path), "file_state": list(inspection.file_state)}
        if existing:
            if len(existing) != 1 or existing[0] != value["model_relocations"][operation].get("destination_row"):
                raise ModelResourceError("Destination registry location changed; preserve it for review.")
        else:
            record["locations"].append(destination_row)
        phase = value["model_relocations"][operation]["phase"]
        if phase not in ("retiring_source", "source_retired"):
            phase = "admitted"
        return self._save_phase(value, token, operation, phase, destination_row=destination_row)

    def _remove_source_row(self, value, token, operation, baseline):
        record = value["resources"][baseline["selection"]["resource_id"]]
        source_key = _key(baseline["selection"]["source"])
        matching = [loc for loc in record["locations"] if _key(loc["path"]) == source_key]
        if matching and matching != [baseline["source_row"]]:
            raise ModelResourceError("Source registry location changed; preserve it for review.")
        record["locations"] = [loc for loc in record["locations"] if _key(loc["path"]) != source_key]
        return self._save_phase(value, token, operation, "source_retired")

    def _cleanup_stage(self, value, token, operation, baseline):
        stage, destination = Path(baseline["stage"]), Path(baseline["destination"])
        marker = value["model_relocations"][operation]
        if stage.exists():
            with pinned_model_retirement(stage, marker["stage_state"], protected_roots=self.protected_roots, expected_links=2) as owned:
                if _identity(destination.stat(follow_symlinks=False)) != tuple(marker["stage_state"]):
                    raise ModelResourceError("Published destination changed; retained staging link preserved.")
                owned.retire()
        return token

    def _complete(self, value, token, operation, baseline):
        destination = Path(baseline["destination"])
        with _pinned_model_file(destination, self.protected_roots):
            inspected = inspect_model_resource(destination)
            if (list(inspected.file_state) != value["model_relocations"][operation]["stage_state"]
                    or inspected.stored_identity() != baseline["identity"] or destination.stat().st_nlink != 1):
                raise ModelResourceError("Final destination identity or links changed. Inspect recovery.")
            return self._save_phase(value, token, operation, "complete")

    def _load_operation(self, value, operation):
        marker = self._operations(value).get(operation)
        if not isinstance(marker, dict):
            raise ModelResourceError("This relocation is not recorded in the selected registry.")
        raw = _bounded_read(self._directory(operation) / "baseline.json", 512 * 1024)
        baseline = _json(raw or b"null")
        if not isinstance(baseline, dict) or baseline.get("schema_version") != 1 or baseline.get("operation_id") != operation or _digest(baseline) != marker.get("baseline_digest"):
            raise ModelResourceError("Relocation recovery evidence is missing or changed. Preserve it for review.")
        selection = baseline["selection"]
        source, destination = self._guard(selection["source"]), self._guard(baseline["destination"])
        stage = _path(baseline["stage"], self.protected_roots)
        if stage != destination.parent / (".collectigen-relocation-" + operation) / destination.name:
            raise ModelResourceError("The recovery stage is outside its recorded destination.")
        record = value["resources"].get(selection["resource_id"])
        if record is None or record["identity"] != baseline["identity"]:
            raise ModelResourceError("The registered model identity changed. Preserve recovery evidence.")
        if not any(d.get("role") == selection["role"] and d.get("family") == selection["family"] for d in record["declarations"]):
            raise ModelResourceError("The selected model declaration changed. Preserve recovery evidence.")
        for path, expected in ((source, baseline["source_row"]), (destination, marker.get("destination_row"))):
            matches = [loc for loc in record["locations"] if _key(loc["path"]) == _key(path)]
            if matches and matches != [expected]:
                raise ModelResourceError("A relocation-owned registry location changed. Preserve it for review.")
            if path == destination and expected is not None and not matches:
                raise ModelResourceError("The admitted destination registry location disappeared. Preserve it for review.")
            if path == source and not matches and marker["phase"] not in ("source_retired", "complete"):
                raise ModelResourceError("The original registered source location disappeared. Preserve it for review.")
        parent = destination.parent.stat(follow_symlinks=False)
        if [parent.st_dev, parent.st_ino] != baseline["destination_parent"]:
            raise ModelResourceError("The original destination folder changed. Preserve recovery evidence.")
        return baseline, marker

    def preview_recovery(self, operation_id, *, should_cancel=None, progress=None):
        value, token = self.registry.snapshot()
        baseline, marker = self._load_operation(value, operation_id)
        destination, stage = (Path(baseline[key]) for key in ("destination", "stage"))
        source = Path(baseline["selection"]["source"])
        if marker["phase"] != "abandoned":
            if source.exists():
                inspected = inspect_model_resource(source, should_cancel=should_cancel, progress=progress)
                if list(inspected.file_state) != baseline["source_state"] or inspected.stored_identity() != baseline["identity"]:
                    raise ModelResourceError("The source was replaced or changed. Recovery will not remove it.")
            elif marker["phase"] not in ("retiring_source", "source_retired", "complete"):
                raise ModelResourceError("Source disappeared before recorded retirement. Preserve recovery evidence.")
            for path in (stage, destination):
                if path.exists() and marker.get("stage_state"):
                    inspected = inspect_model_resource(path, should_cancel=should_cancel, progress=progress)
                    if list(inspected.file_state) != marker["stage_state"] or inspected.stored_identity() != baseline["identity"]:
                        raise ModelResourceError("A staging or destination file was replaced. Recovery will not overwrite it.")
                elif path == destination and path.exists():
                    raise ModelResourceError("An unowned destination exists. Recovery will not overwrite it.")
            if marker["phase"] not in ("prepared", "abandoned") and not stage.exists() and not destination.exists():
                raise ModelResourceError("Both verified destination copies are unavailable. Preserve the source and receipt.")
        if self.registry.snapshot()[1] != token:
            raise ModelResourceError("The registry changed during recovery preview. Inspect recovery again.")
        message = ("Source remains; abandon the incomplete attempt and retain its staging files." if marker["phase"] == "prepared"
            else "Verify the exact recorded placement and finish its registry/file transition.")
        changed = _settings(value) != baseline["settings_digest"]
        if changed:
            message += " Saved folder layout changed. This recovery keeps the original destination shown here; it does not use the new layout."
        return self._issue(RelocationRecovery(uuid4().hex, operation_id, token, marker["baseline_digest"], marker["phase"], source, destination,
            message, changed, _settings(value)))

    def recover(self, preview, *, confirmed=False, should_cancel=None, progress=None, operation_started=None):
        if not confirmed:
            raise ModelResourceError("Confirm the exact recovery preview first.")
        self._accept(preview, RelocationRecovery)
        del self._issued[preview.preview_id]
        operation = preview.operation_id
        with self.registry._writer():
            value, token = self.registry.snapshot()
            if token != preview.registry_token or _settings(value) != preview.settings_digest:
                raise ModelResourceError("The registry changed after recovery preview; inspect recovery again.")
            baseline, marker = self._load_operation(value, operation)
            if marker["baseline_digest"] != preview.baseline_digest or marker["phase"] != preview.status:
                raise ModelResourceError("Recovery state changed. Inspect recovery again.")
            selection = RelocationSelection(**{**baseline["selection"], "source": Path(baseline["selection"]["source"])})
            row = RelocationRow(baseline["row_id"], selection, Path(baseline["destination"]), "", "ready", "")
            if marker["phase"] in ("complete", "abandoned"):
                return self._outcome(row, operation, marker["phase"], "This recorded operation is already settled.")
            try:
                _cancel(should_cancel)
                self._bind_operation(value, token, operation, operation_started)
                with _pinned_parent(row.destination.parent, self.protected_roots), ExitStack() as pins:
                    _cancel(should_cancel)
                    source = None
                    if row.selection.source.exists():
                        source = pins.enter_context(pinned_model_retirement(row.selection.source, baseline["source_state"], protected_roots=self.protected_roots))
                        source.verify(selection.resource_id[7:], baseline["identity"]["size"], should_cancel=should_cancel, progress=progress)
                    elif marker["phase"] not in ("retiring_source", "source_retired"):
                        raise ModelResourceError("Source disappeared before recorded retirement. Preserve evidence.")
                    if marker["phase"] == "prepared":
                        if source is None or row.destination.exists():
                            raise ModelResourceError("Incomplete attempt placement changed. Preserve evidence.")
                        self._save_phase(value, token, operation, "abandoned")
                        return self._outcome(row, operation, "abandoned", "Source unchanged; incomplete staging retained. Start a fresh preview.")
                    stage = Path(baseline["stage"])
                    with ExitStack() as destination_pins:
                        if stage.exists():
                            destination_pins.enter_context(_pinned_model_file(stage, self.protected_roots))
                            inspected = inspect_model_resource(stage, should_cancel=should_cancel, progress=progress)
                            if list(inspected.file_state) != marker["stage_state"] or inspected.stored_identity() != baseline["identity"]:
                                raise ModelResourceError("Staging file changed. Preserve it for review.")
                        if not row.destination.exists():
                            if not stage.exists() or marker["phase"] != "verified_staged":
                                raise ModelResourceError("An admitted destination is missing. Preserve evidence.")
                            _cancel(should_cancel)
                            os.link(stage, row.destination)
                        destination_pins.enter_context(_pinned_model_file(row.destination, self.protected_roots))
                        inspected = inspect_model_resource(row.destination)
                        if list(inspected.file_state) != marker["stage_state"] or inspected.stored_identity() != baseline["identity"]:
                            raise ModelResourceError("Destination was replaced. Preserve it for review.")
                        token = self._admit(value, token, operation, baseline, inspected)
                        if source is not None:
                            token = self._save_phase(value, token, operation, "retiring_source")
                        if source is not None:
                            source.retire()
                        token = self._remove_source_row(value, token, operation, baseline)
                    token = self._cleanup_stage(value, token, operation, baseline)
                    self._complete(value, token, operation, baseline)
                    return self._outcome(row, operation, "complete", "Relocation recovered; exact destination verified and registry reconciled.")
            except (OSError, RuntimeError, ValueError) as exc:
                return self._outcome(row, operation, "recovery_required", "Recovery remains held. " + str(exc))
