"""Canonical, non-destructive management record for a loopback SwarmUI runtime."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import threading
from typing import Any, Callable
from urllib.parse import urlsplit

from .external_write_transaction import file_fingerprint, write_verified_json
from .generation_execution import SwarmCompatibilityError, SwarmLocalAdapter
from .generation_transport import LocalTransport, local_endpoint
from .swarm_companion_inventory import review_companion_inventory
from .paths import SWARM_RUNTIME_PATH
from .swarm_source_inventory import (
    capture_source_inventory, compare_source_inventory, format_source_inventory,
    read_git_revision,
)


SCHEMA_VERSION = 1
DEFAULT_ENDPOINT = "http://127.0.0.1:7801"
SWARM_SOURCE = "https://github.com/mcmonkeyprojects/SwarmUI.git"
MANAGED_RELEASE = "0.9.8-Beta"
MANAGED_COMMIT = "ec823c3f750ce9b5103e623f3ad9918d02b8ddde"
SOURCE_UPDATE_BLOCK_REASON = (
    "SwarmUI source updates are paused: a verified full-installation backup, pinned update target, "
    "and rollback path are not available. Saved configuration copies and source observations "
    "do not provide that recovery."
)
_MAX_RESOURCE_METRIC = (1 << 64) - 1
_STARTUP_LOCK = threading.RLock()
_STARTUP_BY_RECORD: dict[str, "_StartupHandle"] = {}
_STARTUP_BY_ROOT: dict[str, "_StartupHandle"] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_message(error: BaseException) -> str:
    text = " ".join(str(error).split())
    return text[:300] or type(error).__name__


@dataclass(frozen=True)
class SwarmInspection:
    install_root: str
    endpoint: str
    recognized: bool
    launcher: str = ""
    source_revision: str = ""
    runtime_version: str = ""
    server_id: str = ""
    backend_ids: tuple[str, ...] = ()
    model_root: str = ""
    output_root: str = ""
    settings_path: str = ""
    online: bool = False
    control_plane_online: bool = False
    backend_state: str = "offline"
    issue: str = ""
    fingerprint: str = ""
    tracked_source_state: str = "unavailable"
    source_inventory: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SwarmSnapshot:
    state: str
    headline: str
    detail: str
    configured: bool
    endpoint: str = DEFAULT_ENDPOINT
    install_root: str = ""
    ownership: str = ""
    runtime_version: str = ""
    backend_ids: tuple[str, ...] = ()
    last_health_check: str = ""
    update_available: bool = False
    update_summary: str = ""
    active_jobs: int = 0
    control_plane_online: bool = False
    backend_state: str = "offline"
    work_status_known: bool = False
    lifecycle_pending: str = ""
    compatibility_state: str = "unknown"
    compatibility_issue: str = ""
    resource_summary: str = ""
    resource_checked_at: str = ""


@dataclass(frozen=True)
class SwarmStartupStatus:
    running: bool
    pid: int | None
    returncode: int | None
    detail: str


@dataclass
class _StartupHandle:
    record_key: str
    install_key: str
    endpoint: str
    process: Any


def _canonical_key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve(strict=False)))


def _startup_status(handle: _StartupHandle | None, endpoint: str = "") -> SwarmStartupStatus:
    if handle is None:
        return SwarmStartupStatus(False, None, None, "No SwarmUI launcher is retained in this CollectiGen process.")
    pid = getattr(handle.process, "pid", None)
    pid = pid if type(pid) is int and pid > 0 else None
    try:
        returncode = handle.process.poll()
    except Exception:
        return SwarmStartupStatus(True, pid, None,
                                  "The retained launcher state is uncertain; review it before another start.")
    if returncode is None:
        if endpoint and handle.endpoint != endpoint:
            return SwarmStartupStatus(True, pid, None,
                                      f"A launcher for the previous endpoint {handle.endpoint} remains active; review that connection before another start.")
        return SwarmStartupStatus(True, pid, None,
                                  "A SwarmUI launcher started by CollectiGen is still active; check the local connection.")
    return SwarmStartupStatus(False, pid, int(returncode),
                              "The retained SwarmUI launcher exited; a new start may be reviewed.")


def _loopback_port_available(endpoint: str) -> None:
    address = urlsplit(local_endpoint(endpoint))
    ipv6 = address.hostname == "::1"
    family = socket.AF_INET6 if ipv6 else socket.AF_INET
    host = "::1" if ipv6 else "127.0.0.1"
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            if os.name == "nt":
                exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
                if exclusive is None:
                    raise OSError("Exclusive loopback binding is unavailable")
                probe.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            probe.bind((host, address.port, 0, 0) if ipv6 else (host, address.port))
    except OSError as exc:
        raise RuntimeError("The selected loopback port is occupied or unavailable; review the existing service before starting SwarmUI.") from exc


@dataclass(frozen=True)
class DeploymentPreview:
    destination: str
    ready: bool
    source: str
    release: str
    commit: str
    prerequisites: tuple[str, ...]
    issues: tuple[str, ...]
    model_root: str
    free_bytes: int
    fingerprint: str


class SwarmRuntimeManager:
    """Own CollectiGen's record; never claim ownership of Swarm user data."""

    def __init__(
        self,
        record_path: str | Path = SWARM_RUNTIME_PATH,
        *,
        adapter_factory: Callable[[str], Any] = SwarmLocalAdapter,
        transport_factory: Callable[[str], Any] = LocalTransport,
    ):
        self.record_path = Path(record_path)
        self.adapter_factory = adapter_factory
        self.transport_factory = transport_factory

    def _load(self) -> tuple[dict[str, Any], str]:
        try:
            payload = json.loads(self.record_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}, ""
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {}, f"The saved SwarmUI connection could not be read: {_safe_message(exc)}"
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            return {}, "The saved SwarmUI connection uses an unsupported format."
        if payload.get("ownership") not in {"adopted", "managed"}:
            return {}, "The saved SwarmUI ownership mode is invalid."
        try:
            payload["endpoint"] = local_endpoint(str(payload.get("endpoint") or ""))
        except (TypeError, ValueError) as exc:
            return {}, _safe_message(exc)
        return payload, ""

    @staticmethod
    def _global_work(transport, session_id: str) -> int:
        """Fail closed: a new session's status cannot describe other sessions."""
        result = transport.post("GetGlobalStatus", {"session_id": session_id})
        status = result.get("status")
        keys = ("waiting_gens", "loading_models", "waiting_backends", "live_gens")
        if not isinstance(status, dict) or any(
            type(status.get(key)) is not int or status[key] < 0 for key in keys
        ):
            raise RuntimeError("Server-wide work status is unavailable; check SwarmUI before continuing.")
        return sum(status[key] for key in keys)

    def _server_session_payload(self, record):
        transport = self.transport_factory(record["endpoint"])
        session = transport.post("GetNewSession", {})
        if not session.get("session_id") or not session.get("server_id"):
            raise RuntimeError("SwarmUI identity is unavailable; check the connection again.")
        if not record.get("server_id") or session["server_id"] != record["server_id"]:
            raise RuntimeError("The SwarmUI server changed; check the connection and review the action again.")
        return transport, session

    def _server_session(self, record):
        transport, session = self._server_session_payload(record)
        return transport, session["session_id"]

    @staticmethod
    def _backend_rows(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        """Normalize the bounded ListBackends response without trusting its keys."""
        rows: list[tuple[str, dict[str, Any]]] = []
        for key, value in payload.items():
            if isinstance(value, dict) and value.get("type"):
                rows.append((str(key), value))
        return rows

    def _control_plane_probe(self, endpoint: str) -> dict[str, Any]:
        """Identify a live Swarm server separately from generation-backend readiness."""
        transport = self.transport_factory(endpoint)
        session = transport.post("GetNewSession", {})
        session_id = session.get("session_id")
        if not session_id:
            raise ValueError("SwarmUI did not return a session identifier.")
        sid = {"session_id": session_id}
        current = transport.post("GetCurrentStatus", sid)
        backends = transport.post("ListBackends", {**sid, "full_data": True})
        rows = self._backend_rows(backends)
        local = [
            (key, row) for key, row in rows
            if row.get("type") == "comfyui_selfstart"
        ]
        enabled = [(key, row) for key, row in local if row.get("enabled") is True]
        ready = [
            str(row.get("id", key)) for key, row in enabled
            if str(row.get("status") or "").casefold() in {"running", "idle"}
        ]
        work = current.get("status") if isinstance(current.get("status"), dict) else {}
        backend_status = (
            current.get("backend_status")
            if isinstance(current.get("backend_status"), dict) else {}
        )
        active_jobs = 0
        for key in ("waiting_gens", "loading_models", "waiting_backends", "live_gens"):
            try:
                active_jobs += max(0, int(work.get(key) or 0))
            except (TypeError, ValueError, OverflowError):
                pass
        statuses = {str(row.get("status") or "").casefold() for _key, row in enabled}
        try:
            waiting_backends = max(0, int(work.get("waiting_backends") or 0))
        except (TypeError, ValueError, OverflowError):
            waiting_backends = 0
        starting = bool(
            backend_status.get("any_loading")
            or waiting_backends > 0
            or statuses.intersection({"loading", "starting", "initializing", "queued"})
        )
        return {
            "version": str(session.get("version") or ""),
            "server_id": str(session.get("server_id") or ""),
            "backend_ids": ready or [str(row.get("id", key)) for key, row in enabled],
            "ready_backend_ids": ready,
            "has_local_backend": bool(local),
            "has_enabled_backend": bool(enabled),
            "starting": starting,
            "backend_status": str(backend_status.get("status") or "unknown"),
            "active_jobs": active_jobs,
        }

    @staticmethod
    def _control_plane_issue(status: dict[str, Any], adapter_issue: str) -> tuple[str, str]:
        if status.get("starting"):
            return "starting", "SwarmUI is online; its local generation backend is still starting."
        if not status.get("has_local_backend"):
            return (
                "needs_attention",
                "SwarmUI is online, but no local managed Comfy backend is configured. "
                "Open SwarmUI to finish backend setup.",
            )
        if not status.get("has_enabled_backend"):
            return (
                "needs_attention",
                "SwarmUI is online, but its local managed Comfy backend is disabled. "
                "Open SwarmUI to review backend settings.",
            )
        if status.get("ready_backend_ids"):
            return (
                "needs_attention",
                "Compatibility could not be checked; retry or review the SwarmUI installation.",
            )
        return (
            "needs_attention",
            "SwarmUI is online, but its local generation backend is not ready"
            + (f" ({status.get('backend_status')})." if status.get("backend_status") else ".")
            + " Review backend status in SwarmUI.",
        )

    @staticmethod
    def _git_revision(root: Path) -> str:
        return read_git_revision(root)

    @staticmethod
    def _tracked_source_state(root: Path) -> str:
        """Return only an aggregate tracked-source state for this exact checkout."""
        git_marker = root / ".git"
        if (
            not root.is_dir()
            or not git_marker.is_dir()
            or not SwarmRuntimeManager._git_revision(root)
            or not shutil.which("git")
        ):
            return "unavailable"
        command_prefix = [
            "git", "-C", str(root), f"--work-tree={root}", "-c", "core.fsmonitor=false",
            "-c", "core.untrackedCache=false",
            "-c", f"core.worktree={root}", "-c", "core.bare=false",
        ]
        git_environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        git_environment.update({
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        })
        options = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "timeout": 5,
            "check": False,
            "env": git_environment,
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        }
        try:
            conversion_filter = subprocess.run(
                [*command_prefix, "config", "--includes", "--local", "--get-regexp", r"^filter\..*\.(clean|process)$"],
                **options,
            )
            if conversion_filter.returncode == 0:
                return "unavailable"
            if conversion_filter.returncode != 1:
                return "unavailable"
            for index_option in ("--cached", None):
                command = [
                    *command_prefix, "diff", "--quiet", "--no-ext-diff", "--no-textconv",
                    "--ignore-submodules=untracked",
                ]
                if index_option:
                    command.append(index_option)
                result = subprocess.run(command, **options)
                if result.returncode == 1:
                    return "modified"
                if result.returncode != 0:
                    return "unavailable"
        except (OSError, subprocess.SubprocessError):
            return "unavailable"
        return "clean"

    @staticmethod
    def _fingerprint(payload: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()

    def inspect_existing(
        self, install_root: str | Path, endpoint: str, *, probe: bool = True,
    ) -> SwarmInspection:
        root = Path(install_root).expanduser().resolve(strict=False)
        endpoint = local_endpoint(endpoint)
        launchers = (root / "launch-windows.bat", root / "launch-windows.ps1")
        launcher = next((path for path in launchers if path.is_file()), None)
        recognized = root.is_dir() and launcher is not None and (
            (root / "src" / "SwarmUI.csproj").is_file()
            or (root / "SwarmUI.sln").is_file()
        )
        issue = "" if recognized else (
            "Choose the SwarmUI folder containing its Windows launcher and source project."
        )
        tracked_source_state = self._tracked_source_state(root) if recognized else "unavailable"
        source_inventory = capture_source_inventory(root) if recognized else {
            "schema_version": 1, "status": "unavailable", "root_revision": "", "extensions": [],
        }
        capabilities: dict[str, Any] = {}
        control: dict[str, Any] = {}
        backend_state = "offline"
        if recognized and probe:
            try:
                capabilities = dict(self.adapter_factory(endpoint).probe())
                control = {
                    "version": capabilities.get("version"),
                    "server_id": capabilities.get("server_id"),
                    "backend_ids": capabilities.get("backend_ids", ()),
                }
                backend_state = "ready"
            except Exception as exc:  # A stopped or partly configured local runtime is adoptable.
                adapter_issue = _safe_message(exc)
                try:
                    control = self._control_plane_probe(endpoint)
                    backend_state, issue = self._control_plane_issue(control, adapter_issue)
                except Exception:
                    issue = "SwarmUI was recognized but is not running: " + adapter_issue
        identity = {
            "install_root": str(root), "endpoint": endpoint,
            "launcher": str(launcher or ""),
            "source_revision": source_inventory["root_revision"],
            "source_inventory": source_inventory,
            "launcher_fingerprint": file_fingerprint(launcher) if launcher else {"exists": False},
            "recognized": recognized,
        }
        if recognized:
            identity["tracked_source_state"] = tracked_source_state
        payload = {
            "install_root": identity["install_root"], "endpoint": endpoint,
            "launcher": identity["launcher"],
            "source_revision": identity["source_revision"],
            "runtime_version": str(control.get("version") or ""),
            "server_id": str(control.get("server_id") or ""),
            "backend_ids": tuple(str(value) for value in control.get("backend_ids", ())),
            "model_root": str(root / "Models"),
            "output_root": str(root / "Output"),
            "settings_path": str(root / "Data" / "Settings.fds"),
            "online": bool(capabilities.get("healthy")),
            "control_plane_online": bool(control),
            "backend_state": backend_state,
            "tracked_source_state": tracked_source_state,
            "source_inventory": source_inventory,
            "recognized": recognized,
        }
        return SwarmInspection(
            **payload, issue=issue, fingerprint=self._fingerprint(identity),
        )

    def adopt(self, inspection: SwarmInspection, *, managed: bool = False) -> SwarmSnapshot:
        if self.record_path.exists():
            _record, existing_issue = self._load()
            if existing_issue:
                raise RuntimeError(
                    "The saved SwarmUI record needs attention. Disconnect it explicitly "
                    "before creating a replacement."
                )
        current = self.inspect_existing(
            inspection.install_root, inspection.endpoint, probe=False,
        )
        if not current.recognized:
            raise ValueError(current.issue)
        if (current.fingerprint != inspection.fingerprint
                or current.source_inventory != inspection.source_inventory):
            raise RuntimeError("The selected SwarmUI installation changed; inspect it again.")
        record = {
            "schema_version": SCHEMA_VERSION,
            "ownership": "managed" if managed else "adopted",
            "install_root": current.install_root,
            "endpoint": current.endpoint,
            "launcher": current.launcher,
            "source_revision": current.source_revision,
            "tracked_source_state": current.tracked_source_state,
            "source_inventory": current.source_inventory,
            "runtime_version": inspection.runtime_version,
            "server_id": inspection.server_id,
            "backend_ids": list(inspection.backend_ids),
            "model_root": current.model_root,
            "output_root": current.output_root,
            "settings_path": current.settings_path,
            "adopted_at": _now(),
            "last_health_check": _now() if inspection.control_plane_online else "",
            "last_health_issue": inspection.issue if not inspection.online else "",
            "control_plane_online": inspection.control_plane_online,
            "backend_state": inspection.backend_state,
            "launcher_fingerprint": file_fingerprint(current.launcher),
            "update_available": False,
            "update_summary": "",
            "managed_fields": [
                "endpoint", "launcher", "backend connection", "model path integration preview",
                "health checks", "lifecycle requests", "generation submission",
            ],
            "user_owned": [
                "models", "outputs", "extensions", "credentials", "unknown files",
                "custom settings outside CollectiGen's managed fields",
            ],
        }
        write_verified_json(self.record_path, record, label="SwarmUI management record")
        return self.snapshot()

    def disconnect(self) -> None:
        try:
            self.record_path.unlink()
        except FileNotFoundError:
            return

    def snapshot(self) -> SwarmSnapshot:
        record, issue = self._load()
        if issue:
            return SwarmSnapshot("needs_attention", "Needs attention", issue, True)
        if not record:
            return SwarmSnapshot(
                "not_configured", "Not configured",
                "Connect an existing SwarmUI installation or let CollectiGen prepare a managed one.",
                False,
            )
        last_issue = str(record.get("last_health_issue") or "")
        try:
            active_jobs = max(0, int(record.get("active_jobs") or 0))
        except (TypeError, ValueError, OverflowError):
            active_jobs = 0
        update_available = bool(record.get("update_available"))
        control_plane_online = bool(record.get("control_plane_online", not last_issue))
        backend_state = str(record.get("backend_state") or ("offline" if last_issue else "ready"))
        if not control_plane_online:
            state, headline = "offline", "Offline"
        elif backend_state == "starting":
            state, headline = "starting", "Starting"
        elif backend_state in {"needs_attention", "incompatible"}:
            state, headline = backend_state, "Needs attention" if backend_state == "needs_attention" else "Incompatible"
        elif active_jobs:
            state, headline = "busy", "Busy"
        elif update_available:
            state, headline = "update_available", "Update available"
        else:
            state, headline = "ready", "Ready"
        detail = last_issue or "SwarmUI is enrolled for local generation through CollectiGen."
        lifecycle_pending = str(record.get("lifecycle_pending") or "")
        if lifecycle_pending:
            state, headline = "needs_attention", "Needs attention"
            detail = f"SwarmUI {lifecycle_pending} was requested; check the connection before another action."
        elif not control_plane_online:
            startup = self.startup_status()
            if startup.running:
                handle = self._retained_handle(record)
                if handle is not None and handle.endpoint != record["endpoint"]:
                    state, headline, detail = "needs_attention", "Needs attention", startup.detail
                else:
                    state, headline = "starting", "Starting"
                    detail = (f"CollectiGen launcher PID {startup.pid} retained. {startup.detail}"
                              if startup.pid is not None else startup.detail)
        return SwarmSnapshot(
            state, headline, detail, True,
            endpoint=str(record["endpoint"]),
            install_root=str(record.get("install_root") or ""),
            ownership=str(record.get("ownership") or ""),
            runtime_version=str(record.get("runtime_version") or ""),
            backend_ids=tuple(str(value) for value in record.get("backend_ids", ())),
            last_health_check=str(record.get("last_health_check") or ""),
            update_available=update_available,
            update_summary=str(record.get("update_summary") or ""),
            active_jobs=active_jobs,
            control_plane_online=control_plane_online,
            backend_state=backend_state,
            work_status_known=bool(record.get("work_status_known")),
            lifecycle_pending=lifecycle_pending,
            compatibility_state=str(record.get("compatibility_state") or "unknown"),
            compatibility_issue=str(record.get("compatibility_issue") or ""),
            resource_summary=str(record.get("resource_summary") or ""),
            resource_checked_at=str(record.get("resource_checked_at") or ""),
        )

    def refresh_health(self) -> SwarmSnapshot:
        record, issue = self._load()
        if issue or not record:
            return self.snapshot()
        original = dict(record)
        try:
            original_fingerprint = file_fingerprint(self.record_path)
        except OSError:
            return self.snapshot()
        if not original_fingerprint.get("exists") or self._load() != (original, ""):
            return self.snapshot()
        record["active_jobs"] = 0
        record["work_status_known"] = False
        record["compatibility_state"] = "unknown"
        record["compatibility_issue"] = ""
        record["resource_summary"] = ""
        record["resource_checked_at"] = ""
        enrolled_server_id = record.get("server_id")
        enrolled_version = record.get("runtime_version", "")
        enrolled_backends = record.get("backend_ids", [])
        try:
            capabilities = dict(self.adapter_factory(record["endpoint"]).probe())
            record.update({
                "runtime_version": str(capabilities.get("version") or ""),
                "server_id": str(capabilities.get("server_id") or ""),
                "backend_ids": list(capabilities.get("backend_ids") or ()),
                "last_health_check": _now(), "last_health_issue": "",
                "control_plane_online": True, "backend_state": "ready",
                "compatibility_state": "supported", "compatibility_issue": "",
            })
        except Exception as exc:
            adapter_issue = _safe_message(exc)
            try:
                control = self._control_plane_probe(record["endpoint"])
                backend_state, detail = self._control_plane_issue(control, adapter_issue)
                same_server = bool(
                    enrolled_server_id and control.get("server_id") == enrolled_server_id
                )
                if same_server and control.get("ready_backend_ids"):
                    if isinstance(exc, SwarmCompatibilityError):
                        backend_state = "incompatible"
                        detail = str(exc)[:300]
                        record["compatibility_state"] = "incompatible"
                        record["compatibility_issue"] = detail
                    else:
                        record["compatibility_state"] = "unavailable"
                        record["compatibility_issue"] = detail
                record.update({
                    "runtime_version": str(control.get("version") or ""),
                    "server_id": str(control.get("server_id") or ""),
                    "backend_ids": list(control.get("backend_ids") or ()),
                    "active_jobs": 0,
                    "last_health_check": _now(), "last_health_issue": detail,
                    "control_plane_online": True, "backend_state": backend_state,
                })
            except Exception:
                record.update({
                    "last_health_check": _now(), "last_health_issue": adapter_issue,
                    "control_plane_online": False, "backend_state": "offline",
                })
        identity_changed = bool(enrolled_server_id and record.get("server_id") != enrolled_server_id)
        if identity_changed:
            record.update(
                server_id=enrolled_server_id, backend_state="needs_attention",
                runtime_version=enrolled_version, backend_ids=enrolled_backends,
                last_health_issue="The SwarmUI server identity changed. Use Existing SwarmUI to review the connection again.",
                compatibility_state="unknown", compatibility_issue="",
            )
        if record.get("control_plane_online") and not identity_changed:
            try:
                transport, session_id = self._server_session(record)
                try:
                    record["active_jobs"] = self._global_work(transport, session_id)
                    record["work_status_known"] = True
                except Exception:
                    record["active_jobs"] = 0
                    # Global work and resource observations are independent.
                try:
                    response = transport.post("GetServerResourceInfo", {"session_id": session_id})
                    record["resource_summary"] = self._compact_resource_summary(response)
                except Exception:
                    record["resource_summary"] = "RAM unavailable; GPU VRAM unavailable."
                record["resource_checked_at"] = _now()
            except Exception as exc:
                record["resource_summary"] = "RAM unavailable; GPU VRAM unavailable."
                if isinstance(exc, RuntimeError) and (
                    "server changed" in str(exc) or "identity is unavailable" in str(exc)
                ):
                    record["backend_state"] = "needs_attention"
                    record["last_health_issue"] = (
                        "The SwarmUI server identity changed during health review. "
                        "Use Existing SwarmUI to review the connection again."
                    )
                    record["compatibility_state"] = "unknown"
                    record["compatibility_issue"] = ""
                # Health remains useful on older servers; lifecycle fails closed.
        record.pop("lifecycle_pending", None)
        try:
            unchanged = (
                file_fingerprint(self.record_path) == original_fingerprint
                and self._load() == (original, "")
            )
        except OSError:
            unchanged = False
        if not unchanged:
            return self.snapshot()
        write_verified_json(self.record_path, record, label="SwarmUI health record")
        return self.snapshot()

    def change_endpoint(self, endpoint: str) -> SwarmSnapshot:
        record, issue = self._load()
        if issue or not record:
            raise RuntimeError(issue or "Connect SwarmUI before changing its endpoint.")
        record["endpoint"] = local_endpoint(endpoint)
        record["last_health_issue"] = "Connection settings changed; check the connection."
        record["last_health_check"] = ""
        record["control_plane_online"] = False
        record["backend_state"] = "offline"
        record["work_status_known"] = False
        record["active_jobs"] = 0
        record["compatibility_state"] = "unknown"
        record["compatibility_issue"] = ""
        record["resource_summary"] = ""
        record["resource_checked_at"] = ""
        write_verified_json(self.record_path, record, label="SwarmUI connection record")
        return self.snapshot()

    def check_updates(self) -> SwarmSnapshot:
        record, issue = self._load()
        if issue or not record:
            raise RuntimeError(issue or "Connect SwarmUI before checking for updates.")
        original = dict(record)
        original_fingerprint = file_fingerprint(self.record_path)
        if not original_fingerprint.get("exists") or self._load() != (original, ""):
            raise RuntimeError("The saved connection changed; check updates again for the current connection.")
        transport = self.transport_factory(record["endpoint"])
        session = transport.post("GetNewSession", {})
        result = transport.post("CheckForUpdates", {"session_id": session["session_id"]})
        record["work_status_known"] = False
        record["active_jobs"] = 0
        try:
            record["active_jobs"] = self._global_work(transport, session["session_id"])
            record["work_status_known"] = True
        except Exception:
            pass
        server = result.get("server") if isinstance(result.get("server"), dict) else {}
        try:
            counts = int(server.get("count", result.get("server_updates_count")) or 0)
        except (TypeError, ValueError, OverflowError):
            counts = 0
        raw_preview = server.get("preview", result.get("server_updates_preview", ()))
        preview = tuple(str(value) for value in raw_preview if value) if isinstance(raw_preview, (list, tuple)) else ()
        extension_updates = result.get("extensions") if isinstance(result.get("extensions"), dict) else {}
        backend_updates = result.get("backends") if isinstance(result.get("backends"), dict) else {}
        additional = []
        if extension_updates:
            additional.append(
                f"{len(extension_updates)} extension update{'s' if len(extension_updates) != 1 else ''} require SwarmUI review"
            )
        if backend_updates:
            additional.append(
                f"{len(backend_updates)} backend update{'s' if len(backend_updates) != 1 else ''} require SwarmUI review"
            )
        record["update_available"] = counts > 0
        record["update_summary"] = (
            f"{counts} SwarmUI update change{'s' if counts != 1 else ''} available"
            + (": " + ", ".join(preview[:6]) if preview else "")
            if counts else "SwarmUI reports no server update available."
        )
        if additional:
            record["update_summary"] += " " + "; ".join(additional) + "."
        record["update_checked_at"] = _now()
        if file_fingerprint(self.record_path) != original_fingerprint or self._load() != (original, ""):
            raise RuntimeError("The saved connection changed; check updates again for the current connection.")
        write_verified_json(self.record_path, record, label="SwarmUI update record")
        return self.snapshot()

    @staticmethod
    def _review_scalar(value: Any) -> str:
        if type(value) is bool:
            return "yes" if value else "no"
        if type(value) in {int, float}:
            return str(value)
        if type(value) is str:
            return " ".join(value.split())[:512] or "unavailable"
        return "unavailable"

    @classmethod
    def _review_backend_scalar(cls, value: Any) -> str:
        """Backend labels are display-only and deliberately shorter than report fields."""
        text = cls._review_scalar(value)
        return text[:100] + (" (truncated)" if len(text) > 100 else "")

    @classmethod
    def _review_configured_path(cls, settings: Any, field: str) -> str:
        if not isinstance(settings, dict):
            return "unavailable"
        paths = settings.get("paths")
        if not isinstance(paths, dict) or not isinstance(paths.get("value"), dict):
            return "unavailable"
        entry = paths["value"].get(field)
        if not isinstance(entry, dict) or type(entry.get("value")) is not str:
            return "unavailable"
        value = entry["value"]
        return value[:512] + (" (truncated)" if len(value) > 512 else "") if value else "unavailable"

    @staticmethod
    def _review_number(value: Any, *, percent: bool = False) -> str:
        """Return only finite, non-negative metrics from the documented resource route."""
        if type(value) is int:
            numeric = value
        elif type(value) is float and math.isfinite(value):
            numeric = value
        else:
            return "unavailable"
        if numeric < 0 or numeric > _MAX_RESOURCE_METRIC or (percent and numeric > 100):
            return "unavailable"
        return str(numeric)

    @staticmethod
    def _review_bytes(value: Any) -> int | None:
        """The source emits RAM and VRAM byte counts, not generic numeric units."""
        if type(value) is not int or value < 0 or value > _MAX_RESOURCE_METRIC:
            return None
        return value

    @staticmethod
    def _format_bytes(value: int) -> str:
        units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")
        amount = float(value)
        unit = 0
        while amount >= 1024 and unit < len(units) - 1:
            amount /= 1024
            unit += 1
        return f"{amount:.1f} {units[unit]}" if unit else f"{value} B"

    @classmethod
    def _review_memory(cls, value: Any) -> tuple[str, str, str] | None:
        if not isinstance(value, dict):
            return None
        total, used, free = (cls._review_bytes(value.get(key)) for key in ("total", "used", "free"))
        if None in {total, used, free}:
            return None
        if used > total or free > total or used + free != total:
            return None
        return tuple(cls._format_bytes(metric) for metric in (total, used, free))

    @classmethod
    def _compact_resource_summary(cls, payload: Any) -> str:
        """Keep only bounded memory numbers, never GPU names or raw API data."""
        unavailable = "RAM unavailable; GPU VRAM unavailable."
        if not isinstance(payload, dict):
            return unavailable

        def memory(value: Any, *, gpu: bool = False) -> tuple[int, int] | None:
            if not isinstance(value, dict):
                return None
            keys = (
                ("total_memory", "used_memory", "free_memory")
                if gpu else ("total", "used", "free")
            )
            total, used, free = (cls._review_bytes(value.get(key)) for key in keys)
            if None in {total, used, free} or total == 0:
                return None
            if used > total or free > total or used + free != total:
                return None
            return used, total

        def compact(label: str, values: tuple[int, int] | None) -> str:
            if values is None:
                return f"{label} unavailable"
            used, total = values
            unit, divisor = "B", 1
            for candidate, size in (
                ("KiB", 1 << 10), ("MiB", 1 << 20), ("GiB", 1 << 30),
                ("TiB", 1 << 40), ("PiB", 1 << 50), ("EiB", 1 << 60),
            ):
                if total >= size:
                    unit, divisor = candidate, size
            pair = (
                f"{used}/{total} B" if divisor == 1
                else f"{used / divisor:.1f}/{total / divisor:.1f} {unit}"
            )
            percent = (used * 100 + total // 2) // total
            return f"{label} {pair} used ({percent}%)"

        rows = [compact("RAM", memory(payload.get("system_ram")))]
        gpus = payload.get("gpus")
        if not isinstance(gpus, dict) or not gpus:
            rows.append("GPU VRAM unavailable")
        else:
            for position, value in enumerate(gpus.values()):
                if position >= 2:
                    break
                rows.append(compact(f"GPU {position + 1} VRAM", memory(value, gpu=True)))
            if len(gpus) > 2:
                rows.append(f"+{len(gpus) - 2} more GPUs")
        return "; ".join(rows) + "."

    @classmethod
    def _review_resource_lines(cls, payload: Any) -> list[str]:
        """Project the small documented resource response without retaining raw data."""
        if not isinstance(payload, dict):
            return (
                "Process CPU and host cores: unavailable.",
                "Host RAM: unavailable.",
                "Nvidia GPU resources (up to 20): unavailable.",
            )
        cpu = payload.get("cpu")
        usage = cls._review_number(cpu.get("usage") if isinstance(cpu, dict) else None)
        cores = cls._review_number(cpu.get("cores") if isinstance(cpu, dict) else None)
        cpu_line = (
            f"Process CPU usage ratio: {usage}; host cores: {cores}."
            if usage != "unavailable" and cores != "unavailable"
            else "Process CPU and host cores: unavailable."
        )
        ram = cls._review_memory(payload.get("system_ram"))
        ram_line = (
            f"Host RAM: total {ram[0]}; used {ram[1]}; free {ram[2]}."
            if ram else "Host RAM: unavailable."
        )
        gpus = payload.get("gpus")
        if not isinstance(gpus, dict):
            return [cpu_line, ram_line, "Nvidia GPU resources (up to 20): unavailable."]
        rows = []
        for position, value in enumerate(gpus.values()):
            if position >= 20:
                break
            if not isinstance(value, dict):
                continue
            memory = cls._review_memory({
                "total": value.get("total_memory"), "used": value.get("used_memory"),
                "free": value.get("free_memory"),
            })
            temperature = cls._review_number(value.get("temperature"))
            gpu_use = cls._review_number(value.get("utilization_gpu"), percent=True)
            memory_use = cls._review_number(value.get("utilization_memory"), percent=True)
            if not memory or "unavailable" in {temperature, gpu_use, memory_use}:
                rows.append(f"- GPU {position + 1}: unavailable.")
                continue
            rows.append(
                f"- GPU {position + 1}: VRAM total {memory[0]}; used {memory[1]}; free {memory[2]}; "
                f"temperature {temperature} C; GPU use {gpu_use}%; memory use {memory_use}%."
            )
        suffix = "\n(GPU list truncated at 20 entries)" if len(gpus) > 20 else ""
        return [cpu_line, ram_line, "Nvidia GPU resources (up to 20): " + ("\n".join(rows) if rows else "unavailable.") + suffix]

    @staticmethod
    def _review_global_work_lines(payload: Any) -> list[str]:
        if not isinstance(payload, dict) or not isinstance(payload.get("status"), dict):
            return ["Fresh server-wide work: unavailable."]
        status = payload["status"]
        keys = ("waiting_gens", "loading_models", "waiting_backends", "live_gens")
        if any(type(status.get(key)) is not int or status[key] < 0 for key in keys):
            return ["Fresh server-wide work: unavailable."]
        return [
            "Fresh server-wide work: "
            + "; ".join(f"{key} {status[key]}" for key in keys)
            + "."
        ]

    @staticmethod
    def _review_extension_folders(root: Path, inventory: dict[str, Any] | None = None) -> str:
        """Render the same bounded candidate observation used for source comparison."""
        observed = inventory if inventory is not None else capture_source_inventory(root)
        status = observed.get("status")
        entries = [row["name"] for row in observed.get("extensions", ())]
        if status == "unavailable":
            return "unavailable (review the enrolled installation folder)."
        if status == "partial" and not entries:
            return "unavailable (directory scan capped at 1000 entries or incomplete; review the enrolled installation folder)."
        if not entries:
            return "none found (activation and version unverified)."
        suffix = " (truncated)" if status == "partial" else ""
        return "local folders; activation and version unverified: " + ", ".join(entries) + suffix

    def review_installation(self) -> str:
        """Return a bounded, non-persistent installation report for an enrolled runtime."""
        record, issue = self._load()
        if issue or not record:
            raise RuntimeError(issue or "Connect SwarmUI before reviewing its installation.")
        original = dict(record)
        source_inventory = capture_source_inventory(Path(str(record["install_root"])))
        lines = [
            "SwarmUI installation review",
            f"Enrolled installation: {self._review_scalar(record.get('install_root'))}",
            f"Endpoint: {self._review_scalar(record.get('endpoint'))}",
            f"Saved source state: {self._review_scalar(record.get('tracked_source_state'))}",
            f"Saved runtime version: {self._review_scalar(record.get('runtime_version'))}",
            f"Current source state: {self._tracked_source_state(Path(str(record['install_root'])))}",
            compare_source_inventory(record.get("source_inventory"), source_inventory),
            "Current source candidate observations:\n" + format_source_inventory(source_inventory),
            "Stock API process ID: unavailable (the stock read-only APIs do not expose it).",
            "Stock API source fingerprint: unavailable (the stock read-only APIs do not expose it).",
        ]
        try:
            transport, live_session = self._server_session_payload(record)
            session = {"session_id": live_session["session_id"]}
            lines.append("Live server: connected")
            lines.append(f"Live runtime version: {self._review_scalar(live_session.get('version'))}")
            lines.extend(review_companion_inventory(transport, live_session))
            try:
                lines.extend(self._review_resource_lines(transport.post("GetServerResourceInfo", session)))
            except Exception:
                lines.extend(self._review_resource_lines(None))
            try:
                lines.extend(self._review_global_work_lines(transport.post("GetGlobalStatus", session)))
            except Exception:
                lines.extend(self._review_global_work_lines(None))
            try:
                settings_response = transport.post("ListServerSettings", session)
                settings = settings_response.get("settings") if isinstance(settings_response, dict) else None
                configured_paths = tuple(self._review_configured_path(settings, field) for field in ("modelroot", "outputpath", "datapath"))
                lines.append(f"Configured model path: {configured_paths[0]}")
                lines.append(f"Configured output path: {configured_paths[1]}")
                lines.append(f"Configured DataPath setting: {configured_paths[2]} (not proof of active startup paths)")
                if "unavailable" in configured_paths:
                    lines.append("Settings next action: review SwarmUI server-settings permission or supported path values.")
            except Exception:
                lines.extend((
                    "Configured model path: unavailable.", "Configured output path: unavailable.",
                    "Configured DataPath setting: unavailable (not proof of active startup paths).",
                    "Settings next action: review SwarmUI server-settings permission or availability.",
                ))
            try:
                backends = transport.post("ListBackends", {**session, "nonreal": False, "full_data": False})
                if not isinstance(backends, dict):
                    raise ValueError("backend inventory is unavailable")
                rows = []
                for position, (key, value) in enumerate(backends.items()):
                    if position >= 100:
                        break
                    if not isinstance(value, dict):
                        continue
                    backend_id = self._review_backend_scalar(value.get("id", key))
                    backend_type = self._review_backend_scalar(value.get("type"))
                    status = self._review_backend_scalar(value.get("status"))
                    enabled = self._review_backend_scalar(value.get("enabled"))
                    load_models = self._review_backend_scalar(value.get("can_load_models"))
                    features = value.get("features")
                    feature_text = ", ".join(self._review_backend_scalar(item) for item in features[:20]) if isinstance(features, list) else "unavailable"
                    if isinstance(features, list) and len(features) > 20:
                        feature_text += " (truncated)"
                    rows.append(f"- Backend {backend_id}: type {backend_type}; status {status}; enabled {enabled}; can load models {load_models}; features {feature_text}.")
                suffix = "\n(backend list truncated at 100 entries)" if len(backends) > 100 else ""
                lines.append("Backends (up to 100): " + ("\n".join(rows) if rows else "unavailable.") + suffix)
            except Exception:
                lines.extend(("Backends: unavailable.", "Backend next action: review SwarmUI backend-inventory availability."))
            lines.append("Backend versions: unavailable (the supported inventory has no version field).")
            lines.append("Stock API extension activation and source revisions: unavailable (the stock inventory does not expose them).")
        except Exception:
            lines.extend((
                "Live server: unavailable.",
                "Process CPU and host cores: unavailable.",
                "Host RAM: unavailable.",
                "Nvidia GPU resources (up to 20): unavailable.",
                "Fresh server-wide work: unavailable.",
                "Configured model path: unavailable.",
                "Configured output path: unavailable.",
                "Configured DataPath setting: unavailable (not proof of active startup paths).",
                "Backends: unavailable.",
                "Backend versions: unavailable (the supported inventory has no version field).",
                "Stock API extension activation and source revisions: unavailable (the stock inventory does not expose them).",
                "Next action: review the SwarmUI connection, then try again.",
            ))
        lines.append("Original launch arguments and initial path resolution: unverified. Companion effective runtime paths are current observations, not proof of the original invocation.")
        lines.append("Extension inventory (up to 100 folders): " + self._review_extension_folders(Path(str(record["install_root"])), source_inventory))
        if self._load() != (original, ""):
            return "Installation review unavailable. The saved connection changed; review the connection again."
        report = "\n".join(lines)
        marker = "\n[Report truncated at 80000 characters.]"
        return report if len(report) <= 80_000 else report[:80_000 - len(marker)] + marker

    def apply_reported_update(self) -> str:
        raise RuntimeError(SOURCE_UPDATE_BLOCK_REASON)

    def request_lifecycle(self, action: str) -> str:
        if action not in {"stop", "restart"}:
            raise ValueError("Choose Stop or Restart.")
        record, issue = self._load()
        if issue or not record:
            raise RuntimeError(issue or "Connect SwarmUI before controlling it.")
        return self._request_lifecycle(record, action)

    def _request_lifecycle(self, record, action: str) -> str:
        if action == "update":
            raise RuntimeError(SOURCE_UPDATE_BLOCK_REASON)
        if action not in {"stop", "restart"}:
            raise ValueError("Choose Stop or Restart.")
        original = dict(record)
        if record.get("lifecycle_pending"):
            raise RuntimeError("Check the connection before repeating a runtime action.")
        if int(record.get("active_jobs") or 0) > 0:
            raise RuntimeError("SwarmUI has active or waiting work; check again after it becomes idle.")
        transport, session_id = self._server_session(record)
        active_jobs = self._global_work(transport, session_id)
        if self._load() != (original, ""):
            raise RuntimeError("The saved connection changed; review the action again.")
        record.update(active_jobs=active_jobs, work_status_known=True)
        if active_jobs:
            write_verified_json(self.record_path, record, label="SwarmUI work status")
            raise RuntimeError("SwarmUI has active or waiting work; check again after it becomes idle.")
        if self._load() != (original, ""):
            raise RuntimeError("The saved connection changed; review the action again.")
        # Retain intent before sending. A dropped reply must never trigger a retry.
        record["lifecycle_pending"] = action
        write_verified_json(self.record_path, record, label="SwarmUI lifecycle intent")
        intent_fingerprint = file_fingerprint(self.record_path)
        route = "ShutdownServer" if action == "stop" else "UpdateAndRestart"
        payload = {"session_id": session_id}
        if action == "restart":
            payload.update(doUpdateServer=False, extensionsToUpdate=[],
                           backendsToUpdate=[], aggressive=False, force=True)
        try:
            result = transport.post(route, payload)
        except Exception as exc:
            raise RuntimeError(
                "The runtime action's outcome is unknown. Check the connection before retrying."
            ) from exc
        if result.get("success") is not True:
            raise RuntimeError("SwarmUI did not confirm the action. Check the connection before retrying.")
        if file_fingerprint(self.record_path) != intent_fingerprint:
            raise RuntimeError("The runtime action was sent, but the saved connection changed. Check the connection.")
        record["last_health_issue"] = (
            "Stop requested; check the connection to confirm shutdown."
            if action == "stop" else "Restart requested; check the connection when SwarmUI returns."
        )
        record["control_plane_online"] = True
        record["backend_state"] = "needs_attention" if action == "stop" else "starting"
        write_verified_json(self.record_path, record, label="SwarmUI lifecycle record")
        return str(result.get("result") or record["last_health_issue"])

    def recent_logs(self) -> str:
        """Explicit local, bounded display only. Never persist or upload log content."""
        record, issue = self._load()
        if issue or not record:
            raise RuntimeError(issue or "Connect SwarmUI before viewing its logs.")
        transport, session_id = self._server_session(record)
        result = transport.post("ListRecentLogMessages", {
            "session_id": session_id, "types": ["info", "error", "warning"],
        })
        data = result.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("SwarmUI did not provide readable logs.")
        lines = []
        for kind in ("info", "error", "warning"):
            rows = data.get(kind, [])
            if not isinstance(rows, list):
                raise RuntimeError("SwarmUI returned an unsupported log format.")
            for row in rows[-100:]:
                if not isinstance(row, dict) or not isinstance(row.get("message"), str):
                    raise RuntimeError("SwarmUI returned an unsupported log entry.")
                lines.append(f"[{kind}] {str(row.get('timestamp', ''))[:40]} {row['message'][:2000]}")
        return "\n".join(lines)[-120000:] or "No recent information, warning or error messages."

    def deployment_preview(self, destination: str | Path, model_root: str | Path) -> DeploymentPreview:
        target = Path(destination).expanduser().resolve(strict=False)
        issues: list[str] = []
        if os.name != "nt":
            issues.append("Fresh managed deployment is currently supported on Windows only.")
        if target.exists() and any(target.iterdir()):
            issues.append("The destination must be a new or empty folder.")
        git = shutil.which("git")
        dotnet = shutil.which("dotnet")
        prerequisites = (
            f"Git: {git or 'missing'}", f".NET: {dotnet or 'missing'}",
            "Python/backend: selected during SwarmUI first-run setup",
        )
        if not git:
            issues.append("Git is required before CollectiGen can prepare SwarmUI.")
        if not dotnet:
            issues.append("The .NET SDK is required before CollectiGen can prepare SwarmUI.")
        probe = target if target.exists() else target.parent
        try:
            free_bytes = shutil.disk_usage(probe).free
        except OSError:
            free_bytes = 0
            issues.append("Available storage could not be measured.")
        if 0 < free_bytes < 10 * 1024**3:
            issues.append("At least 10 GiB free is required for the runtime before models.")
        values = {
            "destination": str(target), "source": SWARM_SOURCE,
            "release": MANAGED_RELEASE, "commit": MANAGED_COMMIT,
            "model_root": str(Path(model_root).expanduser().resolve(strict=False)),
            "prerequisites": prerequisites, "issues": tuple(issues),
        }
        return DeploymentPreview(
            str(target), not issues, SWARM_SOURCE, MANAGED_RELEASE, MANAGED_COMMIT,
            prerequisites, tuple(issues), values["model_root"], free_bytes,
            self._fingerprint(values),
        )

    def prepare_managed_install(self, preview: DeploymentPreview) -> SwarmInspection:
        current = self.deployment_preview(preview.destination, preview.model_root)
        if current.fingerprint != preview.fingerprint:
            raise RuntimeError("Deployment readiness changed; review the plan again.")
        if not current.ready:
            raise RuntimeError("Resolve the listed deployment issues before continuing.")
        target = Path(current.destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--branch", current.release, "--depth", "1", current.source, str(target)],
            check=True, capture_output=True, text=True, timeout=600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        revision = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip().lower()
        if revision != current.commit:
            raise RuntimeError("The prepared SwarmUI source did not match the reviewed commit.")
        inspection = self.inspect_existing(target, DEFAULT_ENDPOINT)
        if not inspection.recognized:
            raise RuntimeError("The verified source checkout lacks the expected SwarmUI launcher.")
        return inspection

    def launch_command(self) -> tuple[str, tuple[str, ...], str]:
        record, issue = self._load()
        if issue or not record:
            raise RuntimeError(issue or "Connect SwarmUI before starting it.")
        launcher = Path(str(record.get("launcher") or "")).resolve(strict=False)
        root = Path(str(record.get("install_root") or "")).resolve(strict=False)
        if launcher.parent != root or launcher.name.casefold() != "launch-windows.bat" or not launcher.is_file():
            raise RuntimeError("The saved SwarmUI Windows launcher must be reviewed again.")
        if file_fingerprint(launcher) != record.get("launcher_fingerprint"):
            raise RuntimeError("The SwarmUI launcher changed after enrollment; review the installation again.")
        address = urlsplit(local_endpoint(record["endpoint"]))
        host = "::1" if address.hostname == "::1" else "127.0.0.1"
        return str(launcher), ("--launch_mode", "none", "--host", host, "--port", str(address.port)), str(root)

    def _retained_handle(self, record: dict[str, Any]) -> _StartupHandle | None:
        if not record:
            return None
        record_key = _canonical_key(self.record_path)
        install_key = _canonical_key(record["install_root"]) if record.get("install_root") else ""
        # The root entry is the latest attempt across every record for that
        # installation. An older record may still point at an exited launcher.
        handle = _STARTUP_BY_ROOT.get(install_key) if install_key else None
        if handle is None:
            handle = _STARTUP_BY_RECORD.get(record_key)
            if handle is not None and handle.install_key != install_key:
                handle = None
        return handle

    def startup_status(self) -> SwarmStartupStatus:
        """Observe only the launcher handle retained by this CollectiGen process."""
        record, _issue = self._load()
        return _startup_status(self._retained_handle(record), str(record.get("endpoint") or ""))

    @staticmethod
    def _startup_binding(record: dict[str, Any]) -> dict[str, Any]:
        return {key: record.get(key) for key in (
            "ownership", "adopted_at", "endpoint", "server_id", "install_root",
            "launcher", "launcher_fingerprint", "source_revision",
        )}

    def start_runtime(self) -> SwarmStartupStatus:
        """Freshly check enrollment and loopback availability before one launch."""
        if os.name != "nt":
            raise RuntimeError("The reviewed SwarmUI Windows launcher is supported on Windows only.")
        with _STARTUP_LOCK:
            record, issue = self._load()
            if issue or not record:
                raise RuntimeError(issue or "Connect SwarmUI before starting it.")
            record_key = _canonical_key(self.record_path)
            install_key = _canonical_key(record["install_root"])
            retained = self._retained_handle(record)
            status = _startup_status(retained, record["endpoint"])
            if status.running:
                return status
            original_binding = self._startup_binding(record)
            health = self.refresh_health()
            current, current_issue = self._load()
            current_binding = self._startup_binding(current)
            if not original_binding["server_id"] and health.control_plane_online:
                # A fresh successful probe may discover the previously unknown
                # server identity. It returns without launching anything.
                current_binding["server_id"] = original_binding["server_id"]
            if current_issue or current_binding != original_binding:
                raise RuntimeError("The SwarmUI enrollment changed during startup review; review it again.")
            if health.control_plane_online:
                return SwarmStartupStatus(False, None, None,
                                          "The enrolled SwarmUI control plane is already reachable; no launcher was started.")
            launcher, arguments, working = self.launch_command()
            if any(char in launcher for char in '&|<>()^%!"'):
                raise RuntimeError("The SwarmUI installation path cannot be safely passed to the Windows batch launcher.")
            before = file_fingerprint(self.record_path)
            if not before.get("exists") or self._load() != (current, ""):
                raise RuntimeError("The SwarmUI enrollment changed before port review; review it again.")
            _loopback_port_available(current["endpoint"])
            if file_fingerprint(self.record_path) != before or self._load() != (current, ""):
                raise RuntimeError("The SwarmUI enrollment changed during port review; review it again.")
            if self.launch_command() != (launcher, arguments, working):
                raise RuntimeError("The SwarmUI launcher changed during port review; review the installation again.")
            command = ["cmd.exe", "/d", "/c", launcher, *arguments]
            system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
            process = subprocess.Popen(
                command, executable=str(system_root / "System32" / "cmd.exe"), cwd=working,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            handle = _StartupHandle(record_key, install_key, current["endpoint"], process)
            _STARTUP_BY_RECORD[record_key] = handle
            _STARTUP_BY_ROOT[install_key] = handle
            return _startup_status(handle)
