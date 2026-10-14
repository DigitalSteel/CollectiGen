"""Narrow, recoverable edits to an enrolled SwarmUI server's model-role paths.

SwarmUI's ChangeServerSettings has no compare-and-swap operation. A fresh
identity, global-idle and value check narrows the race, but an independent
administrator can still change settings between that check and the API call.
The API may create the configured role directories and refresh model lists.
This module never walks those directories or reads SwarmUI's settings file.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping
from uuid import uuid4

from .external_write_transaction import file_fingerprint, write_verified_json


ROLE_FIELDS = (
    "sdmodelfolder", "sdlorafolder", "sdvaefolder", "sdembeddingfolder",
    "sdcontrolnetsfolder", "sdclipfolder", "sdclipvisionfolder",
)
_ROOT_FIELD = "modelroot"
_MAX_VALUE = 4096
_SCHEMA = 1
_MAX_BASELINES = 512


class ModelPathError(RuntimeError):
    """An unsafe or uncertain operation; inspect recovery before retrying."""


@dataclass(frozen=True)
class ModelPathInventory:
    model_root: str
    values: dict[str, str]
    binding: dict[str, str]
    fingerprint: str


@dataclass(frozen=True)
class ModelPathPreview:
    before: dict[str, str]
    after: dict[str, str]
    changes: tuple[str, ...]
    model_root: str
    binding: dict[str, str]
    inventory_fingerprint: str
    preview_id: str
    fingerprint: str
    kind: str = "apply"
    parent_id: str = ""
    after_model_root: str | None = None

    @property
    def root_changed(self):
        return self.after_model_root is not None and self.after_model_root != self.model_root


@dataclass(frozen=True)
class ModelPathRecovery:
    operation_id: str
    status: str
    message: str
    before: dict[str, str]
    after: dict[str, str]
    changes: tuple[str, ...]
    model_root: str
    kind: str
    after_model_root: str | None = None

    @property
    def root_changed(self):
        return self.after_model_root is not None and self.after_model_root != self.model_root


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


def _valid_value(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) > _MAX_VALUE or any(
        ord(char) < 32 or ord(char) == 127 for char in value
    ):
        raise ModelPathError(f"SwarmUI {label} is missing or has an unsupported value.")
    return value


def _values(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(ROLE_FIELDS):
        raise ModelPathError("The model-path recovery record has invalid role fields.")
    return {key: _valid_value(value[key], key) for key in ROLE_FIELDS}


def _preview_data(preview: ModelPathPreview) -> dict[str, object]:
    return {
        "before": preview.before, "after": preview.after,
        "changes": preview.changes, "model_root": preview.model_root,
        "binding": preview.binding,
        "inventory_fingerprint": preview.inventory_fingerprint,
        "preview_id": preview.preview_id, "kind": preview.kind,
        "parent_id": preview.parent_id,
        "after_model_root": preview.after_model_root,
    }


class SwarmModelPaths:
    """Use the canonical enrollment, and persist only projected path settings."""

    def __init__(self, manager):
        self.manager = manager
        record_path = Path(manager.record_path)
        self.recovery_dir = record_path.with_name(f".{record_path.stem}.model-paths")
        self._issued: dict[str, str] = {}

    def _record(self) -> dict:
        record, issue = self.manager._load()
        if issue:
            raise ModelPathError(issue)
        if not record or not record.get("server_id") or not record.get("endpoint"):
            raise ModelPathError("Enroll a running local SwarmUI server first.")
        return record

    def _binding(self, record: dict) -> dict[str, str]:
        enrollment = {key: str(record.get(key) or "") for key in (
            "endpoint", "server_id", "install_root", "model_root",
            "ownership", "adopted_at", "source_revision",
        )}
        fingerprint = file_fingerprint(self.manager.record_path)
        if not fingerprint.get("exists"):
            raise ModelPathError("The SwarmUI enrollment record disappeared.")
        return {
            "endpoint": enrollment["endpoint"],
            "server_id": enrollment["server_id"],
            "enrollment_fingerprint": _digest(enrollment),
            "record_sha256": str(fingerprint["sha256"]),
            "source_revision": enrollment["source_revision"],
        }

    @staticmethod
    def _project(payload: object) -> tuple[str, dict[str, str]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("settings"), dict):
            raise ModelPathError("SwarmUI did not return server settings.")
        paths = payload["settings"].get("paths")
        if not isinstance(paths, dict) or paths.get("type") != "group" or paths.get("is_secret") is not False:
            raise ModelPathError("SwarmUI's model-path settings are unavailable.")
        group = paths.get("value")
        if not isinstance(group, dict):
            raise ModelPathError("SwarmUI's model-path settings are unavailable.")
        selected = {}
        for key in (*ROLE_FIELDS, _ROOT_FIELD):
            item = group.get(key)
            if (
                not isinstance(item, dict)
                or item.get("type") not in {"text", "string"}
                or item.get("is_secret") is not False
            ):
                raise ModelPathError(f"SwarmUI's paths.{key} is missing, secret or unsupported.")
            selected[key] = _valid_value(item.get("value"), f"paths.{key}")
        return selected.pop(_ROOT_FIELD), selected

    def _live(self, record: dict | None = None):
        record = record or self._record()
        binding = self._binding(record)
        try:
            transport, sid = self.manager._server_session(record)
            root, values = self._project(transport.post("ListServerSettings", {"session_id": sid}))
        except ModelPathError:
            raise
        except Exception as exc:
            raise ModelPathError(
                "Cannot read verified SwarmUI model paths; check the connection and settings permission."
            ) from exc
        if self._binding(self._record()) != binding:
            raise ModelPathError("The SwarmUI enrollment changed during the settings read.")
        inventory = ModelPathInventory(
            root, values, binding,
            _digest({"model_root": root, "values": values, "binding": binding}),
        )
        return inventory, transport, sid

    def read(self) -> ModelPathInventory:
        return self._live()[0]

    def _make_preview(
        self, inventory: ModelPathInventory, after: dict[str, str], *,
        kind: str = "apply", parent_id: str = "",
        after_model_root: str | None = None,
    ) -> ModelPathPreview:
        changes = tuple(key for key in ROLE_FIELDS if inventory.values[key] != after[key])
        preview = ModelPathPreview(
            dict(inventory.values), dict(after), changes, inventory.model_root,
            dict(inventory.binding), inventory.fingerprint, uuid4().hex, "", kind, parent_id, after_model_root,
        )
        preview = ModelPathPreview(**{**preview.__dict__, "fingerprint": _digest(_preview_data(preview))})
        self._issued[preview.preview_id] = preview.fingerprint
        return preview

    def preview(self, values: Mapping[str, str], *, expected: ModelPathInventory) -> ModelPathPreview:
        if not isinstance(expected, ModelPathInventory):
            raise ModelPathError("Read model paths again before reviewing changes.")
        if not isinstance(values, Mapping) or not values or set(values) - set(ROLE_FIELDS):
            raise ModelPathError("Only the seven model-role folders may be changed.")
        if any(key not in expected.values for key in ROLE_FIELDS):
            raise ModelPathError("The displayed model paths are incomplete; read again.")
        live = self.read()
        if live != expected:
            raise ModelPathError("SwarmUI paths or enrollment changed; read them again before editing.")
        after = dict(live.values)
        for key, value in values.items():
            after[key] = _valid_value(value, f"paths.{key}")
        return self._make_preview(live, after)

    def preview_managed_paths(self, values: Mapping[str, str], *, model_root: str,
                              expected: ModelPathInventory) -> ModelPathPreview:
        """Explicit root + seven-role review; filesystem qualification is separate.

        Callers claiming prepared Fast-only paths must hold ModelHotPathService's
        qualification guard around preview and apply. This transaction owns only
        exact enrolled-server settings, readback and selective recovery.
        """
        if not isinstance(values, Mapping) or set(values) != set(ROLE_FIELDS):
            raise ModelPathError("Review all seven roles with the managed model root.")
        model_root = _valid_value(model_root, "paths.modelroot")
        if not model_root.strip() or any(not _valid_value(v, k).strip() for k, v in values.items()):
            raise ModelPathError("Managed root and role folders must be nonempty.")
        if not isinstance(expected, ModelPathInventory) or self.read() != expected:
            raise ModelPathError("SwarmUI paths or enrollment changed; read them again before editing.")
        return self._make_preview(expected, dict(values), after_model_root=model_root)

    def _directory(self, *, create: bool) -> Path:
        path = self.recovery_dir
        # A redirected ancestor could silently move these receipts outside the
        # canonical enrollment's private local storage area.
        if any(
            candidate.is_symlink() or (
                getattr(candidate, "is_junction", lambda: False)()
            )
            for candidate in (path, *path.parents)
        ) or (path.exists() and not path.is_dir()):
            raise ModelPathError("The model-path recovery location is unsafe.")
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    @contextmanager
    def _exclusive(self):
        """Serialize writers with an OS lock released if the process stops."""
        lock = self._directory(create=True) / "operation.lock"
        if lock.is_symlink() or (lock.exists() and lock.stat().st_nlink != 1):
            raise ModelPathError("The model-path recovery lock is unsafe.")
        try:
            with lock.open("a+b") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                    os.fsync(handle.fileno())
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    except OSError as exc:
                        raise ModelPathError("Another model-path operation is active; retry after it finishes.") from exc
                    try:
                        yield
                    finally:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError as exc:
                        raise ModelPathError("Another model-path operation is active; retry after it finishes.") from exc
                    try:
                        yield
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise ModelPathError(f"Cannot lock the model-path recovery journal: {exc}") from exc

    def _journal(self):
        directory = self._directory(create=False)
        latest = directory / "latest.json"
        if latest.is_symlink():
            raise ModelPathError("The model-path recovery journal is unsafe.")
        try:
            state = json.loads(latest.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValueError) as exc:
            raise ModelPathError("The model-path recovery journal is damaged; inspect it before another write.") from exc
        if not isinstance(state, dict) or type(state.get("schema_version")) is not int or state.get("schema_version") not in (1, 2):
            raise ModelPathError("The model-path recovery journal has an unsupported format.")
        op_id = state.get("operation_id")
        if not isinstance(op_id, str) or len(op_id) != 32 or any(c not in "0123456789abcdef" for c in op_id):
            raise ModelPathError("The model-path recovery operation identifier is invalid.")
        baseline_path = directory / f"{op_id}.json"
        if baseline_path.is_symlink():
            raise ModelPathError("The model-path recovery baseline is unsafe.")
        try:
            raw = baseline_path.read_bytes()
            baseline = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ModelPathError("The model-path recovery baseline is missing or damaged.") from exc
        if hashlib.sha256(raw).hexdigest() != state.get("baseline_sha256"):
            raise ModelPathError("The model-path recovery baseline changed; another write is blocked.")
        if not isinstance(baseline, dict) or type(baseline.get("schema_version")) is not int or baseline.get("schema_version") != state["schema_version"] or baseline.get("operation_id") != op_id:
            raise ModelPathError("The model-path recovery baseline is invalid.")
        if state.get("status") not in {"uncertain", "applied", "not_applied", "conflict"} or state.get("phase") not in {"send_intent", "verified", "reconciled"}:
            raise ModelPathError("The model-path recovery status is invalid.")
        if not isinstance(state.get("message"), str) or len(state["message"]) > 500:
            raise ModelPathError("The model-path recovery message is invalid.")
        before, after = _values(baseline.get("before")), _values(baseline.get("after"))
        root = _valid_value(baseline.get("model_root"), "recovery model root")
        if state["schema_version"] == 2:
            after_root = _valid_value(baseline.get("after_model_root"), "recovery after model root")
        else:
            if "after_model_root" in baseline:
                raise ModelPathError("Legacy recovery cannot contain a root transition.")
            after_root = root
        changes = tuple(key for key in ROLE_FIELDS if before[key] != after[key])
        if list(changes) != baseline.get("changes") or (not changes and root == after_root):
            raise ModelPathError("The model-path recovery changes are invalid.")
        if (
            not isinstance(baseline.get("binding"), dict)
            or not isinstance(baseline.get("model_root"), str)
            or baseline.get("kind") not in {"apply", "restore"}
        ):
            raise ModelPathError("The model-path recovery binding is invalid.")
        return state, baseline

    @staticmethod
    def _recovery(state: dict, baseline: dict) -> ModelPathRecovery:
        return ModelPathRecovery(
            state["operation_id"], state["status"], state.get("message", ""),
            dict(baseline["before"]), dict(baseline["after"]),
            tuple(baseline["changes"]), baseline["model_root"], baseline["kind"],
            baseline.get("after_model_root"),
        )

    def recovery(self) -> ModelPathRecovery | None:
        journal = self._journal()
        return self._recovery(*journal) if journal else None

    def _write_state(self, state: dict):
        write_verified_json(self._directory(create=True) / "latest.json", state, label="Swarm model-path journal")

    def _new_journal(self, preview: ModelPathPreview) -> tuple[dict, dict]:
        directory = self._directory(create=True)
        if sum(1 for item in directory.iterdir() if item.suffix == ".json" and item.name != "latest.json") >= _MAX_BASELINES:
            raise ModelPathError(
                "The model-path recovery archive reached its local limit; preserve and review it before more writes."
            )
        operation_id = uuid4().hex
        schema = 2 if preview.after_model_root is not None else _SCHEMA
        baseline = {
            "schema_version": schema, "operation_id": operation_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "kind": preview.kind, "parent_id": preview.parent_id,
            "binding": preview.binding, "model_root": preview.model_root,
            "before": preview.before, "after": preview.after,
            "changes": list(preview.changes),
        }
        if schema == 2:
            baseline["after_model_root"] = preview.after_model_root
        # A verified temporary file is hard-linked into its final name, so an
        # old operation baseline can never be replaced, even across processes.
        target = directory / f"{operation_id}.json"
        temp = directory / f".{operation_id}.{uuid4().hex}.tmp"
        raw = (json.dumps(baseline, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        try:
            with temp.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            if temp.read_bytes() != raw:
                raise ModelPathError("Could not verify the model-path recovery baseline.")
            os.link(temp, target)
            if target.read_bytes() != raw:
                raise ModelPathError("Could not verify the model-path recovery baseline.")
        finally:
            temp.unlink(missing_ok=True)
        state = {
            "schema_version": schema, "operation_id": operation_id,
            "baseline_sha256": hashlib.sha256(raw).hexdigest(),
            "status": "uncertain", "phase": "send_intent",
            "message": "Write outcome unknown; reconcile before another change.",
        }
        self._write_state(state)
        return state, baseline

    @staticmethod
    def _status(inventory: ModelPathInventory, baseline: dict) -> str:
        changes = baseline["changes"]
        if inventory.model_root == baseline.get("after_model_root", baseline["model_root"]) and all(inventory.values[key] == baseline["after"][key] for key in changes):
            return "applied"
        if inventory.model_root == baseline["model_root"] and all(inventory.values[key] == baseline["before"][key] for key in changes):
            return "not_applied"
        return "conflict"

    def reconcile(self) -> ModelPathRecovery | None:
        with self._exclusive():
            return self._reconcile_locked()

    def _reconcile_locked(self) -> ModelPathRecovery | None:
        journal = self._journal()
        if not journal:
            return None
        state, baseline = journal
        try:
            live = self.read()
            if live.binding["enrollment_fingerprint"] != baseline["binding"].get("enrollment_fingerprint") or (baseline["schema_version"] == 1 and live.model_root != baseline["model_root"]):
                raise ModelPathError("The enrolled server or model root changed; inspect the original SwarmUI runtime.")
            status = self._status(live, baseline)
        except Exception as exc:
            status = "uncertain"
            message = "Cannot verify this write; check the enrolled server and retry reconciliation."
        else:
            message = {
                "applied": "Reviewed paths match the requested values. A selective restore is available.",
                "not_applied": "Reviewed paths still match their original values; no restore is needed.",
                "conflict": "Reviewed paths differ from both complete states; review them in SwarmUI.",
            }[status]
        state = {**state, "status": status, "phase": "reconciled", "message": message}
        self._write_state(state)
        return self._recovery(state, baseline)

    def preview_restore(self) -> ModelPathPreview:
        journal = self._journal()
        if not journal or journal[0]["status"] != "applied":
            raise ModelPathError("Reconcile a verified applied model-path operation before restoring.")
        state, baseline = journal
        live = self.read()
        if live.binding["enrollment_fingerprint"] != baseline["binding"].get("enrollment_fingerprint") or live.model_root != baseline.get("after_model_root", baseline["model_root"]):
            raise ModelPathError("The enrolled SwarmUI runtime changed; restore is blocked.")
        if any(live.values[key] != baseline["after"][key] for key in baseline["changes"]):
            raise ModelPathError("A changed role was edited externally; reconcile and review it first.")
        after = dict(live.values)
        for key in baseline["changes"]:
            after[key] = baseline["before"][key]
        return self._make_preview(live, after, kind="restore", parent_id=state["operation_id"],
            after_model_root=baseline["model_root"] if baseline["schema_version"] == 2 else None)

    def _execute(self, preview: ModelPathPreview, *, kind: str, operation_started=None) -> ModelPathRecovery:
        with self._exclusive():
            return self._execute_locked(preview, kind=kind, operation_started=operation_started)

    def _execute_locked(self, preview: ModelPathPreview, *, kind: str, operation_started=None) -> ModelPathRecovery:
        if (
            not isinstance(preview, ModelPathPreview) or preview.kind != kind
            or self._issued.pop(preview.preview_id, None) != preview.fingerprint
            or _digest(_preview_data(preview)) != preview.fingerprint
            or (not preview.changes and not preview.root_changed) or tuple(key for key in ROLE_FIELDS if preview.before[key] != preview.after[key]) != preview.changes
        ):
            raise ModelPathError("The model-path preview changed or expired; review it again.")
        journal = self._journal()
        if journal and journal[0]["status"] in {"uncertain", "conflict"}:
            raise ModelPathError("Resolve the pending model-path recovery before another write.")
        if kind == "restore" and (not journal or journal[0]["status"] != "applied" or journal[0]["operation_id"] != preview.parent_id):
            raise ModelPathError("The restore source changed; review recovery again.")
        record = self._record()
        if self._binding(record) != preview.binding:
            raise ModelPathError("The SwarmUI enrollment changed; review model paths again.")
        try:
            transport, sid = self.manager._server_session(record)
            if self.manager._global_work(transport, sid) != 0:
                raise ModelPathError("SwarmUI has active server-wide work; wait until it is idle.")
            root, values = self._project(transport.post("ListServerSettings", {"session_id": sid}))
            if root != preview.model_root or values != preview.before:
                raise ModelPathError("SwarmUI model paths changed; read and review them again.")
            # Check work a second time directly before recording the send intent.
            if self.manager._global_work(transport, sid) != 0:
                raise ModelPathError("SwarmUI has active server-wide work; wait until it is idle.")
        except ModelPathError:
            raise
        except Exception as exc:
            raise ModelPathError(f"Cannot verify SwarmUI before changing paths: {exc}") from exc
        state, baseline = self._new_journal(preview)
        if operation_started is not None:
            try:
                # The callback records only its own journal: owner reentry is
                # forbidden under this lock. Payload contains no mutable owner data.
                if self._journal() != (state, baseline):
                    raise ModelPathError("Model-path recovery evidence changed before binding.")
                operation_started({"schema_version": 1, "reference_kind": "immutable_receipt",
                    "owner": "swarm_model_paths", "operation_id": state["operation_id"],
                    "locator": str(self._directory(create=False) / (state["operation_id"] + ".json")),
                    "fingerprint": state["baseline_sha256"],
                    "evidence": {"kind": kind, "parent_id": preview.parent_id}})
                if self._journal() != (state, baseline):
                    raise ModelPathError("Model-path recovery evidence changed while binding.")
            except Exception as exc:
                self._write_state({**state, "status": "not_applied", "phase": "reconciled",
                    "message": "No change was sent; operation binding failed. Review the retained operation."})
                raise ModelPathError("No change was sent; operation binding failed. Inspect recovery.") from exc
        try:
            # Journal creation can take time. Make the final server and local
            # checks after intent is durable, immediately before the API send.
            current_record = self._record()
            if self._binding(current_record) != preview.binding:
                raise ModelPathError("The SwarmUI enrollment changed before the write.")
            transport, sid = self.manager._server_session(current_record)
            if self.manager._global_work(transport, sid) != 0:
                raise ModelPathError("SwarmUI started server-wide work before the write.")
            final_root, final_values = self._project(
                transport.post("ListServerSettings", {"session_id": sid})
            )
            if final_root != preview.model_root or final_values != preview.before:
                raise ModelPathError("SwarmUI model paths changed before the write.")
            if self._binding(self._record()) != preview.binding:
                raise ModelPathError("The SwarmUI enrollment changed before the write.")
        except Exception as exc:
            state = {
                **state, "status": "not_applied", "phase": "reconciled",
                "message": "No change was sent; enrollment, server work or model paths changed. Review again.",
            }
            self._write_state(state)
            raise ModelPathError(state["message"]) from exc
        payload = {
            "session_id": sid,
            "settings": {f"paths.{key}": preview.after[key] for key in preview.changes},
        }
        if preview.root_changed:
            payload["settings"]["paths.modelroot"] = preview.after_model_root
        try:
            result = transport.post("ChangeServerSettings", payload)
            if not isinstance(result, dict) or result.get("success") is not True:
                raise ModelPathError("SwarmUI did not confirm the setting change.")
            readback_root, readback_values = self._project(
                transport.post("ListServerSettings", {"session_id": sid})
            )
            verified_roles = ROLE_FIELDS if preview.after_model_root is not None else preview.changes
            if readback_root != (preview.after_model_root if preview.after_model_root is not None else preview.model_root) or any(
                readback_values[key] != preview.after[key] for key in verified_roles
            ):
                raise ModelPathError("SwarmUI readback did not match every changed path.")
        except Exception as exc:
            # Never resend or automatically roll back after the intent is durable.
            raise ModelPathError(
                "The SwarmUI write outcome is uncertain. Reconcile the saved operation before another change."
            ) from exc
        state = {
            **state, "status": "applied", "phase": "verified",
            "message": "Reviewed paths matched exact SwarmUI readback.",
        }
        try:
            self._write_state(state)
        except Exception as exc:
            raise ModelPathError(
                "SwarmUI readback matched, but the local receipt could not be finalized; inspect recovery before another change."
            ) from exc
        return self._recovery(state, baseline)

    def apply(self, preview: ModelPathPreview, *, operation_started=None) -> ModelPathRecovery:
        return self._execute(preview, kind="apply", operation_started=operation_started)

    def restore(self, preview: ModelPathPreview, *, operation_started=None) -> ModelPathRecovery:
        return self._execute(preview, kind="restore", operation_started=operation_started)
