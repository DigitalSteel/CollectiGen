"""Explicit, isolated Comfy source preparation and owned process lifecycle.

Construction is inert. Preview reads only caller-selected interpreter, source
files and three model files. No model discovery, installation or HTTP calls.
An exact binding is still not proof of graph/model compatibility or GPU fit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from core.model_resource_registry import inspect_model_resource
from core.training_source_io import _assert_unredirected


SOURCE_FILES = ("main.py", "server.py", "nodes.py", "comfy_extras/nodes_wan.py",
                "comfy_extras/nodes_video.py", "comfyui_version.py",
                "folder_paths.py", "utils/extra_config.py",
                "comfy/text_encoders/wan.py", "comfy/text_encoders/spiece_tokenizer.py",
                "comfy/cli_args.py", "app/database/db.py")
# Public v0.36.0 tag resolved to this immutable commit on 2026-09-18.
# https://github.com/Comfy-Org/ComfyUI/tree/ee71d5c4993f29086b27fde1629a945ae48425bf
# Source is GPL-3.0; no upstream implementation is bundled by this catalog.
LOADER_REVISION = "ee71d5c4993f29086b27fde1629a945ae48425bf"
LOADER_CONTRACT = "collectigen.comfy-exact-wan-loaders.v1"
LOADER_SOURCE_HASHES = {
    "main.py": "8fc2cafc8e19e6e4b9134a2f6eba70b0e93d9d341dde01a40534dca89ca11727",
    "server.py": "2118a18b95583869491fbf1ae33b3a279d92d64af23a244ec254ee5f2150957c",
    "nodes.py": "34a483b59eed6bc407e74cd860cba7cae542f494fdff29bb15138369f78387a7",
    "folder_paths.py": "b748be5d1e068ad673f6c4de069fb6b7b723405c40ae3c3c44465d416f7df2db",
    "utils/extra_config.py": "ebb923e58956587acf5196e3a76ae3c576196f2f498e5933ce9416fca1ddaf9e",
    "comfy_extras/nodes_wan.py": "dcd8b81d1225d84e8b0c6743675d59772449899018e9f50bb27267dbb8962002",
    "comfy_extras/nodes_video.py": "5879633287808ba2dac6753bcbc90acf8d73c72e8df1d0a9755a4ab70e712a0d",
    "comfyui_version.py": "d17a9d16cbc0c7a1cfd8101608db43b4038f224d9e36c0cddb1d238f5c8bfdf3",
    "comfy/text_encoders/wan.py": "e9a6ca356790c76f7a36550842fc8a93c8c584cd175d69615eaddb7b4cd276bc",
    "comfy/text_encoders/spiece_tokenizer.py": "300de9904c109171b2270524a35c372e6b8c145b88b3f9d242613d45c2bb559f",
    "comfy/cli_args.py": "13cafedf0b724c3b04918a18a06507b86287fb66af8147d49b343d67c5c85c0a",
    "app/database/db.py": "3fd0ee8ae7d6d6f67eff117168f52e90c95825c4ef5319c4dd53a6bde16c4035",
    "comfy/sd.py": "3eba634a7311bc51cf19c54b12c91149306fe71ae28fa37744e36cfb9290f00a",
    "comfy/utils.py": "f3ee59baf72f6910b7662224854e372c713ba3ad01c89739ba092e5ee22460e6",
    "comfy/options.py": "a9d0bb41f5b8d393efc264e83e5f3003ce52c87aca585c0e8ab97bbaea87c8bb",
}
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_OWNED_ENV_PATHS = {
    'HOME': 'user', 'USERPROFILE': 'user',
    'APPDATA': 'user/AppData/Roaming', 'LOCALAPPDATA': 'user/AppData/Local',
    'TMPDIR': 'temp', 'TMP': 'temp', 'TEMP': 'temp',
    'HF_HOME': 'cache/huggingface', 'HF_HUB_CACHE': 'cache/huggingface/hub',
    'HUGGINGFACE_HUB_CACHE': 'cache/huggingface/hub',
    'HF_ASSETS_CACHE': 'cache/huggingface/assets',
    'HF_DATASETS_CACHE': 'cache/huggingface/datasets',
    'TRANSFORMERS_CACHE': 'cache/transformers',
    'SENTENCE_TRANSFORMERS_HOME': 'cache/sentence-transformers',
    'TORCH_HOME': 'cache/torch', 'TORCH_EXTENSIONS_DIR': 'cache/torch/extensions',
    'TORCHINDUCTOR_CACHE_DIR': 'cache/inductor',
    'TRITON_CACHE_DIR': 'cache/triton', 'CUDA_CACHE_PATH': 'cache/cuda',
    'XDG_CACHE_HOME': 'cache/xdg', 'XDG_CONFIG_HOME': 'user/config',
    'XDG_DATA_HOME': 'user/data', 'XDG_STATE_HOME': 'user/state',
    'MPLCONFIGDIR': 'cache/matplotlib', 'NUMBA_CACHE_DIR': 'cache/numba',
    'PYTHONPYCACHEPREFIX': 'cache/pycache', 'PYTHONUSERBASE': 'user/python',
}
_SCRUB_ENV = {'PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'HOMEDRIVE',
              'HOMEPATH', 'PIP_CONFIG_FILE', 'PIP_INDEX_URL', 'PIP_EXTRA_INDEX_URL',
              'VIRTUAL_ENV', 'CONDA_PREFIX'}
_SCRUB_PREFIXES = ('HF_', 'HUGGINGFACE_', 'XDG_', 'COMFYUI_', 'TRANSFORMERS_',
                   'PIP_', 'WANDB_')
_PRESERVE_ENV = {'HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE'}


class RuntimeBoundaryError(ValueError):
    """An isolated runtime cannot be prepared or is no longer the reviewed one."""


class RuntimeStartRecoveryError(RuntimeBoundaryError):
    """Startup failed after spawning; ``receipt`` retains the exact child stop handle."""

    def __init__(self, message: str, receipt: "RuntimeReceipt") -> None:
        super().__init__(message)
        self.receipt = receipt


_runtime_lock = threading.Lock()
_runtime_starting = object()
_active_process: Any | None = None
_recovery_receipt: Any | None = None


def _process_exited(process: Any) -> bool:
    try:
        return process.poll() is not None
    except Exception:
        return False  # An uncertain process state never frees the GPU ownership claim.


def _check(test: bool, message: str) -> None:
    if not test:
        raise RuntimeBoundaryError(message)


def _path(value: str | Path) -> Path:
    _check(isinstance(value, (str, Path)) and bool(str(value)), "An explicit path is required.")
    _check(Path(value).is_absolute(), "An absolute path is required.")
    path = Path(os.path.abspath(str(value)))
    _assert_unredirected(path)
    return path


def _same_or_child(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((os.path.normcase(str(path)), os.path.normcase(str(root)))) == os.path.normcase(str(root))
    except ValueError:
        return False


def _hash_file(path: Path) -> str:
    _assert_unredirected(path)
    before = path.stat(follow_symlinks=False)
    _check(stat.S_ISREG(before.st_mode), f"{path.name} must be a regular file.")
    _check(not (int(getattr(before, "st_file_attributes", 0)) & (0x1000 | 0x40000 | 0x400000)),
           f"{path.name} is not fully local.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    digest = hashlib.sha256()
    with os.fdopen(os.open(path, flags), "rb") as handle:
        opened = os.fstat(handle.fileno())
        _check(stat.S_ISREG(opened.st_mode) and _identity(before) == _identity(opened),
               f"{path.name} changed before inspection.")
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
        closed = os.fstat(handle.fileno())
    _assert_unredirected(path)
    _check(_identity(opened) == _identity(closed) == _identity(path.stat(follow_symlinks=False)),
           f"{path.name} changed during inspection.")
    return digest.hexdigest()


def _read_owned_file(path: Path, limit: int) -> bytes:
    _assert_unredirected(path)
    before = path.stat(follow_symlinks=False)
    _check(stat.S_ISREG(before.st_mode) and before.st_size <= limit,
           f"{path.name} is not a bounded regular file.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        opened = os.fstat(handle.fileno())
        _check(_identity(before) == _identity(opened), f"{path.name} changed before read.")
        raw = handle.read(limit + 1)
        closed = os.fstat(handle.fileno())
    _assert_unredirected(path)
    _check(len(raw) <= limit and _identity(opened) == _identity(closed)
           == _identity(path.stat(follow_symlinks=False)),
           f"{path.name} changed during read.")
    return raw


def _identity(state: os.stat_result) -> tuple[int, ...]:
    return (int(state.st_dev), int(state.st_ino), int(state.st_size),
            int(state.st_mtime_ns), int(getattr(state, "st_file_attributes", 0)))


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def runtime_environment(workspace):
    """Explicit owned home, cache and temporary roots; never change connectivity."""
    root = Path(workspace)
    return {**{key: str(root / relative) for key, relative in _OWNED_ENV_PATHS.items()},
            'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'}


def runtime_process_environment(workspace, inherited=None):
    """Preserve OS/GPU essentials while removing inherited user/cache routes."""
    source = os.environ if inherited is None else inherited
    kept = {key: value for key, value in source.items()
            if key.upper() in _PRESERVE_ENV or (
                key.upper() not in _SCRUB_ENV
                and not key.upper().startswith(_SCRUB_PREFIXES)
                and key.upper() not in _OWNED_ENV_PATHS)}
    return {**kept, **runtime_environment(workspace)}


def _bootstrap_bytes(source: Path, config_path: Path, config_sha256: str, *, protected=None) -> bytes:
    """Guard Comfy's source-root implicit YAML before the installed loader runs."""
    source_literal = json.dumps(str(source), ensure_ascii=True)
    config_literal = json.dumps(str(config_path), ensure_ascii=True)
    digest_literal = json.dumps(config_sha256)
    script = f'''# CollectiGen owned Comfy bootstrap; exact bytes are in the runtime receipt.
import builtins
import hashlib
import io
import os
import runpy
import sys

SOURCE_ROOT = {source_literal}
MAIN_PATH = os.path.join(SOURCE_ROOT, "main.py")
SOURCE_EXTRA = os.path.join(SOURCE_ROOT, "extra_model_paths.yaml")
OWNED_EXTRA = {config_literal}
EXPECTED_EXTRA_SHA256 = {digest_literal}
_original_open = builtins.open
_original_isfile = os.path.isfile
_original_argv = sys.argv
_original_sys_path = list(sys.path)

def _same(path, expected):
    try:
        return os.path.normcase(os.path.abspath(os.fspath(path))) == os.path.normcase(os.path.abspath(expected))
    except (TypeError, ValueError):
        return False

def _guarded_isfile(path):
    if _same(path, SOURCE_EXTRA):
        if os.path.lexists(SOURCE_EXTRA):
            raise RuntimeError("Unreviewed source-root model paths are blocked")
        return False
    return _original_isfile(path)

def _guarded_open(file, mode="r", *args, **kwargs):
    if _same(file, SOURCE_EXTRA):
        raise RuntimeError("Unreviewed source-root model paths are blocked")
    if _same(file, OWNED_EXTRA):
        if mode not in ("r", "rt"):
            raise RuntimeError("Owned model path config is read-only")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(OWNED_EXTRA, flags), "rb") as handle:
            raw = handle.read(16385)
        if len(raw) > 16384 or hashlib.sha256(raw).hexdigest() != EXPECTED_EXTRA_SHA256:
            raise RuntimeError("Owned model path config changed")
        return io.StringIO(raw.decode("utf-8"))
    return _original_open(file, mode, *args, **kwargs)

if os.path.lexists(SOURCE_EXTRA):
    raise RuntimeError("Unreviewed source-root model paths are blocked")
sys.argv = [MAIN_PATH, *sys.argv[2:]]
sys.path.insert(0, SOURCE_ROOT)
builtins.open = _guarded_open
os.path.isfile = _guarded_isfile
try:
    runpy.run_path(MAIN_PATH, run_name="__main__")
finally:
    builtins.open = _original_open
    os.path.isfile = _original_isfile
    sys.argv = _original_argv
    sys.path[:] = _original_sys_path
'''
    if protected is not None:
        setup = "\n".join("    " + line for line in _protected_loader_setup(protected).splitlines())
        script = script.replace("_original_open = builtins.open", "_original_import = builtins.__import__\n_original_open = builtins.open")
        script = script.replace("try:\n    runpy.run_path", "try:\n" + setup + "\n    runpy.run_path")
        script = script.replace("    builtins.open = _original_open", "    builtins.__import__ = _original_import\n    builtins.open = _original_open")
    return script.encode('utf-8')


def _protected_loader_setup(protected):
    """Original guard code for the pinned public stock-loader calling convention."""
    return '''
import json
import stat
from pathlib import Path
_protected = json.loads(PROTECTED_LITERAL)
_original_import = builtins.__import__
_aliases = {"unet": "diffusion_models", "clip": "text_encoders"}
_categories = {"transformer": "diffusion_models", "text_encoder": "text_encoders", "vae": "vae"}
_selected = {_categories[role]: row for role, row in _protected["models"].items()}
_patched = set()
_nonce = os.environ.get("COLLECTIGEN_RUNTIME_NONCE", "")
if len(_nonce) != 32 or any(c not in "0123456789abcdef" for c in _nonce):
    raise RuntimeError("Protected runtime requires its exact supervisor nonce")

def _safe_file(path, expected_hash, maximum):
    path = Path(path)
    for part in (path, *path.parents):
        st = part.lstat()
        if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
            raise RuntimeError("Redirected protected source or model")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
        raise RuntimeError("Protected source or model exceeds exact bounds")
    sha = hashlib.sha256()
    with _original_open(path, "rb") as stream:
        opened = os.fstat(stream.fileno())
        remaining = maximum + 1
        while remaining:
            block = stream.read(min(4 * 1024 * 1024, remaining))
            if not block: break
            remaining -= len(block)
            sha.update(block)
        after = os.fstat(stream.fileno())
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    if not remaining or sha.hexdigest() != expected_hash or not identity(before) == identity(opened) == identity(after) == identity(path.stat()):
        raise RuntimeError("Protected source or model identity changed")

for _relative, _sha in _protected["source_hashes"].items():
    _safe_file(os.path.join(SOURCE_ROOT, _relative), _sha, 4 * 1024 * 1024)

def _exact_path(category, filename):
    category = _aliases.get(category, category)
    row = _selected.get(category)
    if row is None or filename != row["file_name"]:
        raise RuntimeError("This runtime admits only the reviewed exact three model files")
    _safe_file(row["path"], row["sha256"], row["size"])
    return row["path"]

def _exact_names(category):
    row = _selected.get(_aliases.get(category, category))
    return [row["file_name"]] if row else []

def _exact_folders(category):
    row = _selected.get(_aliases.get(category, category))
    return [os.path.dirname(row["path"])] if row else []

def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    result = _original_import(name, globals, locals, fromlist, level)
    folders = sys.modules.get("folder_paths")
    if folders is not None and hasattr(folders, "get_full_path_or_raise") and "folders" not in _patched:
        folders.get_full_path = folders.get_full_path_or_raise = _exact_path
        folders.get_filename_list = _exact_names
        folders.get_folder_paths = _exact_folders
        _patched.add("folders")
    server = sys.modules.get("server")
    if server is not None and hasattr(server, "PromptServer") and "server" not in _patched:
        original_add = server.PromptServer.add_routes
        def add_routes(instance):
            async def identity(request):
                if not (folders.get_full_path_or_raise is _exact_path and folders.get_filename_list is _exact_names):
                    raise RuntimeError("Protected loader functions changed")
                return server.web.json_response({"contract": _protected["contract"],
                    "binding_digest": _protected["binding_digest"], "nonce": _nonce, "pid": os.getpid()})
            instance.routes.get("/collectigen/runtime/" + _nonce)(identity)
            return original_add(instance)
        server.PromptServer.add_routes = add_routes
        _patched.add("server")
    return result

builtins.__import__ = _guarded_import
'''.replace("PROTECTED_LITERAL", repr(_json(protected).decode("utf-8")))


def protected_binding(preview):
    _check(preview.config.protected_loader and preview.source_hashes == LOADER_SOURCE_HASHES,
           "Protected preparation requires the pinned exact Comfy loader contract.")
    facts = {"contract": LOADER_CONTRACT, "revision": LOADER_REVISION,
             "models": preview.model_manifest, "source_hashes": preview.source_hashes}
    return {**facts, "binding_digest": hashlib.sha256(_json(facts)).hexdigest()}


@dataclass(frozen=True)
class RuntimeModel:
    role: str
    path: str | Path
    sha256: str

    def __post_init__(self) -> None:
        _check(self.role in {"transformer", "text_encoder", "vae"}, "Unknown runtime model role.")
        _check(isinstance(self.sha256, str) and bool(_SHA.fullmatch(self.sha256)),
               "Runtime model needs a full SHA-256 hash.")


@dataclass(frozen=True)
class RuntimeConfig:
    source_root: str | Path
    python_executable: str | Path
    python_runtime_root: str | Path
    transformer: RuntimeModel
    text_encoder: RuntimeModel
    vae: RuntimeModel
    workspace_root: str | Path
    port: int
    required_free_bytes: int
    protected_loader: bool = False

    def __post_init__(self) -> None:
        _check(type(self.protected_loader) is bool, "Protected loader mode must be explicit.")
        _check(type(self.port) is int and 1024 <= self.port <= 65535,
               "Choose a bounded unprivileged loopback port.")
        _check(type(self.required_free_bytes) is int and self.required_free_bytes > 0,
               "An explicit positive free-space budget is required.")
        _check((self.transformer.role, self.text_encoder.role, self.vae.role)
               == ("transformer", "text_encoder", "vae"), "Runtime model roles are mismatched.")


@dataclass(frozen=True)
class RuntimePreview:
    config: RuntimeConfig
    source_hashes: dict[str, str]
    python_sha256: str
    model_manifest: dict[str, dict[str, Any]]
    config_bytes: bytes
    bootstrap_bytes: bytes
    command: tuple[str, ...]
    fingerprint: str
    free_bytes: int
    protected_roots: tuple[Path, ...]

    @property
    def model_bindings(self) -> dict[str, dict[str, str]]:
        return {role: {"resource_id": "sha256:" + row["sha256"],
                       "file_name": row["file_name"]}
                for role, row in self.model_manifest.items()}


@dataclass
class RuntimeReceipt:
    preview: RuntimePreview
    pid: int
    started_ns: int
    nonce: str
    receipt_path: Path
    process: Any = field(repr=False)
    protected_verifier: Callable | None = field(default=None, repr=False)


def get_held_runtime_recovery(preview: RuntimePreview) -> RuntimeReceipt | None:
    """Return only this reviewed draft's held post-spawn failure handle."""
    _check(isinstance(preview, RuntimePreview), "A reviewed runtime preview is required for recovery.")
    global _active_process, _recovery_receipt
    with _runtime_lock:
        receipt = _recovery_receipt
        if receipt is None:
            return None
        if _active_process is not receipt.process or _process_exited(receipt.process):
            if _active_process is receipt.process:
                _active_process = None
            _recovery_receipt = None
            return None
        # This comparison must remain usable even if the owned workspace changed
        # after launch; stopping the held child does not read that workspace.
        if (receipt.preview.fingerprint != preview.fingerprint or
                os.path.normcase(os.path.abspath(str(receipt.preview.config.workspace_root))) !=
                os.path.normcase(os.path.abspath(str(preview.config.workspace_root)))):
            return None
        return receipt


def preview_runtime(config: RuntimeConfig, *, protected_roots: tuple[str | Path, ...] = (),
                    inspect: Callable[..., Any] = inspect_model_resource,
                    disk_free: Callable[[Path], int] | None = None,
                    allow_existing_workspace: bool = False) -> RuntimePreview:
    """Rehash the explicitly selected bytes and build a no-side-effect launch plan."""
    _check(isinstance(config, RuntimeConfig), "Runtime configuration is required.")
    source, executable, runtime_root, workspace = map(_path, (
        config.source_root, config.python_executable, config.python_runtime_root,
        config.workspace_root))
    _check(source.is_dir() and runtime_root.is_dir(), "Configured source/runtime root is missing.")
    _check(executable.is_file() and executable.suffix.casefold() == ".exe"
           and _same_or_child(executable, runtime_root),
           "Python executable must exist inside the explicit runtime root.")
    _check(source != workspace and not _same_or_child(workspace, source)
           and not _same_or_child(source, workspace)
           and not _same_or_child(workspace, runtime_root)
           and not _same_or_child(runtime_root, workspace),
           "Owned workspace must be separate from source and Python runtime.")
    protected = tuple(_path(raw) for raw in protected_roots)
    for root in protected:
        _check(not _same_or_child(workspace, root) and not _same_or_child(root, workspace),
               "Owned runtime workspace overlaps a protected root.")
    _check(workspace.parent.is_dir(), "The owned workspace parent must already exist.")
    if workspace.exists():
        _check(allow_existing_workspace and workspace.is_dir(),
               "Owned runtime workspace already exists; choose a new one.")
    source_extra = source / "extra_model_paths.yaml"
    _check(not os.path.lexists(source_extra),
           "Source-root extra_model_paths.yaml would add unreviewed model directories.")
    source_hashes = {}
    for rel in (LOADER_SOURCE_HASHES if config.protected_loader else SOURCE_FILES):
        file = _path(source / rel)
        _check(_same_or_child(file, source), "Source manifest escaped its root.")
        source_hashes[rel] = _hash_file(file)
    if config.protected_loader:
        _check(source_hashes == LOADER_SOURCE_HASHES,
               "This source does not match the pinned qualified Comfy loader contract; keep manual mode.")
    python_sha256 = _hash_file(executable)
    model_manifest: dict[str, dict[str, Any]] = {}
    names: set[str] = set()
    files: set[str] = set()
    extra_paths: dict[str, dict[str, str]] = {}
    for model in (config.transformer, config.text_encoder, config.vae):
        path = _path(model.path)
        _check(path.suffix.casefold() == ".safetensors" and path.is_file(),
               "Each selected model must be one existing safetensors file.")
        _check(not _same_or_child(path, workspace) and not _same_or_child(path, source)
               and not _same_or_child(path, runtime_root),
               "Selected models must be separate from runtime and owned workspace.")
        for root in protected:
            _check(not _same_or_child(path, root),
                   "Selected model falls under a protected root.")
        folded = path.name.casefold()
        _check(folded not in names, "Duplicate selected model basenames are ambiguous.")
        names.add(folded)
        normalized = os.path.normcase(str(path))
        _check(normalized not in files, "The same model file cannot fill two roles.")
        files.add(normalized)
        inspection = inspect(path)
        _check(inspection.resource_id == "sha256:" + model.sha256,
               f"Selected {model.role} model hash differs from the reviewed identity.")
        _check(inspection.path == path and inspection.size > 0,
               "Selected model inspection changed path or size.")
        model_manifest[model.role] = {"path": str(path), "sha256": model.sha256,
                                      "size": inspection.size, "file_name": path.name,
                                      "file_state": list(inspection.file_state)}
        category = {"transformer": "diffusion_models", "text_encoder": "text_encoders",
                    "vae": "vae"}[model.role]
        extra_paths[f"collectigen_{model.role}"] = {
            "is_default": True, category: str(path.parent)}
    config_bytes = _json(extra_paths) + b"\n"  # JSON is accepted by Comfy's yaml.safe_load.
    config_path = workspace / "extra_model_paths.yaml"
    protection = None
    if config.protected_loader:
        facts = {"contract": LOADER_CONTRACT, "revision": LOADER_REVISION,
                 "models": model_manifest, "source_hashes": source_hashes}
        protection = {**facts, "binding_digest": hashlib.sha256(_json(facts)).hexdigest()}
    bootstrap_bytes = _bootstrap_bytes(source, config_path,
        hashlib.sha256(config_bytes).hexdigest(), protected=protection)
    command = (str(executable), str(workspace / "bootstrap.py"), str(source / "main.py"),
               "--base-directory", str(workspace),
               "--user-directory", str(workspace / "user"),
               "--input-directory", str(workspace / "input"),
               "--output-directory", str(workspace / "output"),
               "--temp-directory", str(workspace / "temp"),
               "--database-url", "sqlite:///" + (workspace / "user" / "comfyui.db").as_posix(),
               "--extra-model-paths-config", str(config_path),
               "--disable-all-custom-nodes", "--disable-auto-launch",
               "--disable-metadata", "--listen", "127.0.0.1", "--port", str(config.port))
    if disk_free is None:
        disk_free = lambda path: shutil.disk_usage(path).free
    free_bytes = disk_free(workspace.parent)
    _check(type(free_bytes) is int and free_bytes >= config.required_free_bytes,
           "Insufficient free space for the explicit runtime budget.")
    manifest = {"source_root": str(source), "source_hashes": source_hashes,
                "python_executable": str(executable), "python_sha256": python_sha256,
                "models": model_manifest, "workspace_root": str(workspace),
                "port": config.port, "required_free_bytes": config.required_free_bytes,
                "extra_config_sha256": hashlib.sha256(config_bytes).hexdigest(),
                "bootstrap_sha256": hashlib.sha256(bootstrap_bytes).hexdigest(),
                "runtime_environment": runtime_environment(workspace),
                "command": command}
    return RuntimePreview(config, source_hashes, python_sha256, model_manifest,
                          config_bytes, bootstrap_bytes, command,
                          hashlib.sha256(_json(manifest)).hexdigest(),
                          free_bytes, protected)


def start_runtime(preview: RuntimePreview, *, confirmed: bool = False,
                  popen: Callable[..., Any] = subprocess.Popen,
                  inspect: Callable[..., Any] = inspect_model_resource,
                  disk_free: Callable[[Path], int] | None = None) -> RuntimeReceipt:
    """Start at most one owned video runtime in this app process at a time."""
    _check(confirmed, "Explicit runtime start confirmation is required.")
    _check(not preview.config.protected_loader or popen is not subprocess.Popen,
           "Protected runtime startup requires its contained lifecycle owner.")
    global _active_process, _recovery_receipt
    with _runtime_lock:
        if _active_process is not None and _active_process is not _runtime_starting:
            if _process_exited(_active_process):
                _active_process = None
                _recovery_receipt = None
        _check(_active_process is None,
               "Another owned video runtime is starting or running. Stop the previous Video draft runtime first.")
        _active_process = _runtime_starting
    try:
        return _start_reserved_runtime(preview, popen=popen, inspect=inspect,
                                       disk_free=disk_free)
    except BaseException:
        with _runtime_lock:
            if _active_process is _runtime_starting or (
                    _active_process is not None and _process_exited(_active_process)):
                _active_process = None
                _recovery_receipt = None
        raise


def _start_reserved_runtime(preview: RuntimePreview, *, popen: Callable[..., Any],
                            inspect: Callable[..., Any],
                            disk_free: Callable[[Path], int] | None) -> RuntimeReceipt:
    """Revalidate and launch while the process-wide start slot is reserved."""
    global _active_process, _recovery_receipt
    fresh = preview_runtime(preview.config, protected_roots=preview.protected_roots,
                            inspect=inspect, disk_free=disk_free)
    _check(fresh.fingerprint == preview.fingerprint, "Runtime bytes or plan changed since preview.")
    workspace = _path(preview.config.workspace_root)
    workspace.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name in ("user", "input", "output", "temp"):
        (workspace / name).mkdir(mode=0o700, exist_ok=False)
    overrides = runtime_environment(workspace)
    for key in _OWNED_ENV_PATHS:
        Path(overrides[key]).mkdir(mode=0o700, parents=True, exist_ok=True)
    config_path = workspace / "extra_model_paths.yaml"
    with open(config_path, "xb") as handle:
        handle.write(preview.config_bytes)
        handle.flush()
        os.fsync(handle.fileno())
    bootstrap_path = workspace / "bootstrap.py"
    with open(bootstrap_path, "xb") as handle:
        handle.write(preview.bootstrap_bytes)
        handle.flush()
        os.fsync(handle.fileno())
    nonce = uuid4().hex
    pending = workspace / "runtime.pending.json"
    with open(pending, "xb") as handle:
        handle.write(_json({"nonce": nonce, "fingerprint": preview.fingerprint,
                            "state": "starting"}))
        handle.flush()
        os.fsync(handle.fileno())
    log = open(workspace / "runtime.log", "xb")
    process = None
    provisional = None
    try:
        try:
            process = popen(preview.command, cwd=str(_path(preview.config.source_root)),
                            env=runtime_process_environment(workspace),
                            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            # Claim the returned object before even checking its PID. A failed
            # receipt write must never discard a child that may still be alive.
            with _runtime_lock:
                _active_process = process if process is not None else _runtime_starting
            provisional = RuntimeReceipt(preview, getattr(process, "pid", None),
                time.time_ns(), nonce, workspace / "runtime.receipt.json", process)
        finally:
            log.close()
        _check(type(provisional.pid) is int and provisional.pid > 0,
               "Runtime did not return an owned PID.")
        with open(provisional.receipt_path, "xb") as handle:
            handle.write(_json({"nonce": nonce, "fingerprint": preview.fingerprint,
                                "pid": provisional.pid, "started_ns": provisional.started_ns,
                                "workspace_root": str(workspace), "command": preview.command,
                                "runtime_environment": overrides,
                                "bootstrap_sha256": hashlib.sha256(preview.bootstrap_bytes).hexdigest(),
                                "source_hashes": preview.source_hashes,
                                "python_sha256": preview.python_sha256,
                                "models": preview.model_manifest}))
            handle.flush()
            os.fsync(handle.fileno())
        pending.unlink()
    except BaseException:
        if process is not None:
            try:
                if not _process_exited(process):
                    process.terminate()
                    process.wait(timeout=10)
            except BaseException as cleanup_error:
                if not _process_exited(process):
                    with _runtime_lock:
                        _recovery_receipt = provisional
                    raise RuntimeStartRecoveryError(
                        "The owned video runtime may still be running after startup failed. "
                        "Stop this held Video draft runtime before retrying.", provisional,
                    ) from cleanup_error
            if not _process_exited(process):
                with _runtime_lock:
                    _recovery_receipt = provisional
                raise RuntimeStartRecoveryError(
                    "The owned video runtime has not confirmed exit after startup failed. "
                    "Stop this held Video draft runtime before retrying.", provisional)
        raise
    return provisional


def verify_runtime(receipt: RuntimeReceipt, *, inspect: Callable[..., Any] = inspect_model_resource,
                   disk_free: Callable[[Path], int] | None = None) -> RuntimePreview:
    """Require the same held process and unchanged selected bytes before execution."""
    with _runtime_lock:
        _check(receipt is not _recovery_receipt,
               "Stop the runtime from the failed startup before preparing another request.")
    _check(isinstance(receipt, RuntimeReceipt) and receipt.process.pid == receipt.pid
           and receipt.process.poll() is None, "Owned Comfy process is not running.")
    receipt_path = _path(receipt.receipt_path)
    _check(receipt_path == _path(receipt.preview.config.workspace_root) / "runtime.receipt.json",
           "Runtime receipt path changed.")
    saved = json.loads(_read_owned_file(receipt_path, 16_384))
    _check(saved.get("nonce") == receipt.nonce and saved.get("pid") == receipt.pid
           and saved.get("started_ns") == receipt.started_ns
           and saved.get("fingerprint") == receipt.preview.fingerprint
           and saved.get("command") == list(receipt.preview.command)
           and saved.get("bootstrap_sha256") == hashlib.sha256(receipt.preview.bootstrap_bytes).hexdigest()
           and saved.get("runtime_environment") == runtime_environment(receipt.preview.config.workspace_root),
           "Owned runtime receipt differs from the held process.")
    config_path = _path(receipt.preview.config.workspace_root) / "extra_model_paths.yaml"
    _check(_read_owned_file(config_path, 16_384) == receipt.preview.config_bytes,
           "Runtime model-path config changed.")
    bootstrap_path = _path(receipt.preview.config.workspace_root) / "bootstrap.py"
    _check(_read_owned_file(bootstrap_path, 16_384) == receipt.preview.bootstrap_bytes,
           "Owned runtime bootstrap changed.")
    fresh = preview_runtime(receipt.preview.config,
                            protected_roots=receipt.preview.protected_roots,
                            inspect=inspect, disk_free=disk_free,
                            allow_existing_workspace=True)
    _check(fresh.fingerprint == receipt.preview.fingerprint,
           "Runtime source, interpreter, model or config identity changed.")
    if receipt.preview.config.protected_loader:
        _check(callable(receipt.protected_verifier), "Protected runtime ownership is unavailable; retain its models.")
        receipt.protected_verifier(receipt)
    return fresh


def stop_runtime(receipt: RuntimeReceipt, *, confirmed: bool = False,
                 timeout_seconds: float = 10.0) -> None:
    """Stop only the held child process; never discover or interrupt a shared PID."""
    _check(confirmed, "Explicit owned-runtime stop confirmation is required.")
    _check(isinstance(receipt, RuntimeReceipt) and receipt.process.pid == receipt.pid,
           "No matching owned process handle is available.")
    if receipt.process.poll() is None:
        receipt.process.terminate()
        try:
            receipt.process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            receipt.process.kill()
            receipt.process.wait(timeout=timeout_seconds)
    _check(_process_exited(receipt.process),
           "Owned video runtime stop has not confirmed process exit; its GPU ownership remains held.")
    global _active_process, _recovery_receipt
    with _runtime_lock:
        if _active_process is receipt.process:
            _active_process = None
            _recovery_receipt = None
