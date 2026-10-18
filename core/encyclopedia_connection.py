"""Explicit existing portable reader connection; no acquisition or installation.

Reuse component inventory readback, canonical settings transactions and retained
Windows Job ownership. Inventory consistency is not publisher authentication.
The stable frozen root's additional helper admission is deliberately not bypassed.
"""
from dataclasses import dataclass
from copy import deepcopy
from functools import partial
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path

from core.backend_connection_settings import BackendConnectionSettings, MAX_SETTINGS_BYTES
from core.settings import AppSettings, _settings_writer
from suite_core.discovery.component_staging import verify_directory, guard
from suite_core.discovery.component_release_signature import _json
from suite_core.installation import ComponentId

KEY = "manager_encyclopedia_connection"
COMPONENT = ComponentId("collectigen.tag-encyclopedia")


class EncyclopediaHold(ValueError):
    pass


@dataclass(frozen=True)
class ReaderConnection:
    directory: Path
    inventory_sha256: str
    version: str

    @property
    def program(self):
        return self.directory / "TagEncyclopedia/TagEncyclopedia.exe"

    def mapping(self):
        return {"schema_version": 1, "directory": str(self.directory),
                "inventory_sha256": self.inventory_sha256, "version": self.version}


def review_directory(directory, *, expected=None):
    directory = Path(directory)
    if not directory.is_absolute():
        raise EncyclopediaHold("Select an absolute extracted package directory.")
    guard(directory)
    inventory = directory / "BOOTSTRAP_INVENTORY.json"
    guard(inventory)
    if inventory.stat().st_size > 1024 * 1024:
        raise EncyclopediaHold("The package inventory is too large.")
    raw = inventory.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    profile = _json(raw, "component inventory")
    selected = ReaderConnection(directory, digest, profile.get("software_version"))
    if expected is not None and selected != expected:
        raise EncyclopediaHold("The connected package changed. Review and save it again.")
    profile = verify_directory(directory, digest, COMPONENT, selected.version)
    if profile["platform"] != {"os": "windows", "architecture": "x86_64"}:
        raise EncyclopediaHold("A Windows x64 Encyclopedia package is required.")
    if "TagEncyclopedia/TagEncyclopedia.exe" not in {row["path"] for row in profile["files"]}:
        raise EncyclopediaHold("The declared Encyclopedia entry is missing.")
    return selected


class ReaderConnectionStore:
    def __init__(self, settings_path):
        self.settings = BackendConnectionSettings(settings_path)

    def observe(self):
        return self.settings.observe()

    def selected(self, snapshot):
        row = snapshot._payload.get(KEY)
        if row is None:
            return None
        if (type(row) is not dict or row.get("schema_version") != 1
                or any(type(row.get(k)) is not str for k in ("directory", "inventory_sha256", "version"))):
            raise EncyclopediaHold("The saved reader connection is unavailable. Review a package again.")
        return ReaderConnection(Path(row["directory"]), row["inventory_sha256"], row["version"])

    def save(self, selected, *, expected):
        selected = review_directory(selected.directory, expected=selected)
        with _settings_writer(self.settings.path):
            current = self.observe()
            if current.digest != expected.digest:
                raise EncyclopediaHold("Settings changed. Reload the connection before saving.")
            payload = deepcopy(current._payload)
            previous = payload.get(KEY) or {}
            payload[KEY] = {**previous, **selected.mapping()}
            raw = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            if len(raw) > MAX_SETTINGS_BYTES:
                raise EncyclopediaHold("Settings exceed their supported size.")
            # Canonical writer owns backup, publication, readback and restoration.
            AppSettings._from_payload(payload)._save_locked(self.settings.path, _patch_payload=payload)
        return self.observe()

    def clear(self, *, expected):
        """Explicit recovered first-install removal, same stale guard and writer."""
        with _settings_writer(self.settings.path):
            current=self.observe()
            if current.digest != expected.digest:
                raise EncyclopediaHold("Settings changed. Reload the connection before clearing.")
            if KEY not in current._payload:return current
            payload=deepcopy(current._payload);payload.pop(KEY)
            AppSettings._from_payload(payload)._save_locked(self.settings.path,_patch_payload=payload)
        return self.observe()


@dataclass(frozen=True)
class RootReaderSelection:
    root: Path
    settings_path: Path
    selected: ReaderConnection

    def validate(self):
        if not self.settings_path.is_absolute() or not self.settings_path.is_relative_to(self.root / "Data"):
            raise EncyclopediaHold("Reader settings must belong to the original Manager deployment.")
        store = ReaderConnectionStore(self.settings_path)
        if store.selected(store.observe()) != self.selected:
            raise EncyclopediaHold("The saved reader changed. Restart Manager to admit its new connection.")
        return review_directory(self.selected.directory, expected=self.selected)

    def context(self):
        self.validate()
        return {"directory": str(self.selected.directory), "inventory_sha256": self.selected.inventory_sha256,
                "version": self.selected.version, "program": str(self.selected.program),
                "settings_path": str(self.settings_path)}

    @property
    def program_sha256(self):
        self.validate()
        profile = _json((self.selected.directory / "BOOTSTRAP_INVENTORY.json").read_bytes(), "component inventory")
        return next(row["sha256"] for row in profile["files"] if row["path"] == "TagEncyclopedia/TagEncyclopedia.exe")


def root_reader_selection(root, admitted_settings_path):
    settings_path = Path(admitted_settings_path)
    if settings_path.suffix.casefold() == ".ini":
        settings_path = settings_path.with_suffix(".json")
    store = ReaderConnectionStore(settings_path)
    selected = store.selected(store.observe())
    if selected is None:return None
    selection = RootReaderSelection(Path(root),settings_path,selected)
    selection.validate()
    return selection


def native_reader():
    from core.ollama_owned_windows import WindowsOwnedProcess
    from core.training_process_windows import WindowsTrainingJob
    native = WindowsOwnedProcess()
    native.job_factory = partial(WindowsTrainingJob, interactive=True)
    return native


def _visible_reader_window(pid):
    """Observe a visible window belonging only to the retained native process."""
    import ctypes
    from ctypes import wintypes
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows.restype = wintypes.BOOL
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsWindowVisible.restype = wintypes.BOOL
    seen = []
    @callback_type
    def observe(hwnd, unused):
        current = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(current))
        if current.value == pid and user.IsWindowVisible(hwnd):
            seen.append(hwnd)
        return True
    if not user.EnumWindows(observe, 0):
        raise EncyclopediaHold("The retained reader's visible window could not be checked.")
    return bool(seen)


class OwnedEncyclopedia:
    """One retained native reader; Manager Close waits for ordinary reader Close.

    The source entry can own a local child. Frozen entry must supply a separately
    qualified stable-root admission before enabling this route. No fake lifecycle
    provider or updater permit is repurposed here.
    """
    def __init__(self, *, factory=native_reader, launch_allowed=True, normal_entry_session=None):
        self.factory = factory
        self.launch_allowed = launch_allowed is True
        self.normal_entry_session = normal_entry_session
        self.component_stage_pending = False
        self.native = self.child = self.selected = self.identity = None
        self.reason = "No reader is running."
        self.last_exit = None
        self._visible_seen = False
        self._health = None
        self._stopped = False

    @property
    def close_held(self):
        return self.native is not None or self.component_stage_pending

    def launch(self, selected):
        if not self.launch_allowed:
            raise EncyclopediaHold("Launch awaits Encyclopedia admission by this portable Manager's root owner.")
        if self.close_held:
            raise EncyclopediaHold("Close the retained Encyclopedia window before launching again.")
        selected = review_directory(selected.directory, expected=selected)
        self._health = None
        self._visible_seen = self._stopped = False
        native = self.factory()
        self.native, self.selected = native, selected
        try:
            context = {}
            if self.normal_entry_session is not None:
                from suite_core.manager.bootstrap_entry import EntryHelperAdapter
                root_selection = getattr(self.normal_entry_session,"reader_selection",None)
                if root_selection is None or root_selection.selected != selected:
                    raise EncyclopediaHold("Restart Manager to admit this saved reader connection.")
                adapter = EntryHelperAdapter.for_reader(self.normal_entry_session,native,root_selection)
                native.bind_helper_capability(adapter)
                context = root_selection.context()
            self.child = native.spawn_command((str(selected.program),), selected.program.parent,
                dict(os.environ), helper_context=context)
            self.identity = native.identity(self.child, selected.program)
            self.reason = "Reader process started. Close its window when finished; Manager retains its owner."
        except BaseException:
            # A partial suspended birth remains retained for explicit cleanup.
            self.reason = "Reader startup failed; its original owner is retained until cleanup is verified."
            raise

    def assert_idle(self):
        self.poll()
        if self.close_held:
            raise EncyclopediaHold("Close the owned reader before changing its installation.")
        session = self.normal_entry_session
        if session is not None and (session.state != 'ready' or session.helper_outstanding is not None):
            raise EncyclopediaHold("The original Manager session is not idle.")
        return True

    @contextmanager
    def component_stage_hold(self, component):
        """Original root grant, not a supplied boolean or another deployment lease."""
        from suite_core.discovery.component_staging import ComponentStageBinding, component_stage_owner
        if component != COMPONENT or self.normal_entry_session is None:
            raise EncyclopediaHold("Installation needs the original portable Manager root admission.")
        self.assert_idle()
        session=self.normal_entry_session
        self.component_stage_pending=True
        scope=None
        try:
            scope=session.request_component_stage('begin')
            root=Path(scope['code_root']);data=Path(scope['data_root'])
            def check():
                if self.native is not None or not self.component_stage_pending:
                    raise EncyclopediaHold("The original reader/transaction owner changed.")
                observed=session.request_component_stage('check',scope['scope_id'])
                if observed != scope:
                    raise EncyclopediaHold("The original root transaction scope changed.")
            yield ComponentStageBinding(root,component_stage_owner(root,data,component),check,data)
        finally:
            # Lost ACK/session or lease retains the hold. Never claim release.
            if scope is not None and session.state=='ready':
                session.request_component_stage('release',scope['scope_id'])
                self.component_stage_pending=False
            elif scope is None and session.state=='ready':
                self.component_stage_pending=False

    def poll(self):
        if self.native is None:
            return True
        if self.child is None:
            job = self.native.job
            if job is not None:
                if job.process is not None:
                    if job.poll() is None or job.active():
                        self.reason = "Partial reader startup needs explicit cleanup."
                        return False
                    drain = self.native.drain
                    if drain is not None and drain.is_alive():
                        return False
                    if job.stdout is not None:
                        job.stdout.close()
                job.close()
            self.native = None
            return True
        if self.identity is not None:
            self.native.check_identity(self.child, self.identity, self.selected.program)
        code = self.child.poll()
        if code is None and self.identity is not None and not self._visible_seen:
            seen = _visible_reader_window(self.child.pid)
            self.native.check_identity(self.child, self.identity, self.selected.program)
            self._visible_seen = seen and self.child.poll() is None
        if code is None or self.native.job.active() or not self.native.drained.is_set():
            return False
        self.native.close(self.child)
        self.last_exit = code
        if code == 0 and self.identity is not None and self._visible_seen and not self._stopped:
            self._health = self.selected
        self.native = self.child = self.identity = None
        self.reason = f"Reader closed (exit {code}); its process and output worker are drained."
        return True

    def native_health(self, selected):
        """Exact original visible birth, normal exit and verified drain; no UI bool."""
        self.poll()
        if self.close_held or self._health != selected:
            return False
        review_directory(selected.directory, expected=selected)
        if self.normal_entry_session is not None:
            startup = getattr(self.normal_entry_session, "reader_selection", None)
            if startup is None or startup.selected != selected:
                return False
            startup.validate()
        return True

    def stop_failed_start(self):
        self._health = None
        self._stopped = True
        if self.native is None:
            return
        if self.child is not None:
            self.native.terminate_owned(self.child)
        elif self.native.job is not None:
            self.native.job.terminate()
        self.reason = "Stopping only the retained reader Job. Waiting for verified drain."
