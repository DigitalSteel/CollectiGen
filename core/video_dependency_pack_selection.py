"""Read-only selection of reviewed Comfy video runtime bytes for an offline pack.

The runtime preview pins the launch-critical files, but does not inventory a
complete Python dependency closure or supply redistribution licenses. This
builder records those gaps; it never starts, copies, or discovers a runtime.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Callable

from .comfy_video_runtime import RuntimePreview, SOURCE_FILES
from .video_dependency_pack import MAX_FILES, MAX_TOTAL_BYTES, PackFile, PackSelection, VideoPackError


MAX_VISITED_ENTRIES = 80_000
HASH_CHUNK_BYTES = 1024 * 1024
SOURCE_TOP_LEVEL = frozenset({
    "requirements.txt", "LICENSE", "LICENSE.txt", "pyproject.toml",
    "setup.py", "execution.py", "latent_preview.py", "cuda_malloc.py",
    "model_filemanager.py", "samplers.py", "protocol.py",
    "node_helpers.py", "hook_breaker_ac10a0.py", "alembic.ini",
} | {name for name in SOURCE_FILES if "/" not in name})
SOURCE_CODE_TREES = ("comfy", "comfy_extras", "app", "api_server", "utils", "comfyui_api_nodes",
                     "comfy_api", "comfy_execution", "comfy_config", "middleware", "alembic_db")
SOURCE_DATA_FILES = frozenset({"comfy/text_encoders/umt5_config_xxl.json",
                                "alembic_db/script.py.mako", "alembic_db/README.md"})
RUNTIME_DATA_FILES = frozenset({"Lib/site-packages/certifi/cacert.pem"})
RUNTIME_DOCS_PREFIX = "Lib/site-packages/comfyui_embedded_docs/docs/"
RUNTIME_DOCS_SUFFIXES = frozenset({".md", ".jpg", ".png", ".webp"})
RUNTIME_TEMPLATE_MEDIA_PREFIX = "Lib/site-packages/comfyui_workflow_templates_media_"
RUNTIME_TEMPLATE_MEDIA_SUFFIXES = frozenset({".webp", ".png", ".jpg", ".jpeg", ".mp3"})
RUNTIME_TREES = ("DLLs", "Lib")
ROOT_USER_DIRS = frozenset({".git", ".venv", ".pytest_cache", "cache", "caches",
    "custom_nodes", "data", "database", "input", "output", "temp", "tmp", "user",
    "users", "models", "backups", "logs", "private", "secrets", "credentials",
    "local_only", "local only", "local-only", "node_modules", "training", "downloads"})
GENERATED_DIRS = frozenset({".git", ".venv", ".pytest_cache", "__pycache__", ".cache", "node_modules"})
PRIVATE_DIRS = frozenset({"private", "secrets", "credentials", "local_only", "cache",
    "caches", "input", "output", "temp", "tmp", "user", "users", "backups",
    "logs", "custom_nodes", "downloads", "training", "local only", "local-only"})
SOURCE_SUFFIXES = frozenset({".py", ".toml", ".yaml", ".yml"})
RUNTIME_SUFFIXES = frozenset({
    ".py", ".pyc", ".pyd", ".dll", ".exe", ".zip", ".pth", "._pth",
    ".cfg", ".txt", ".json", ".h", ".lib",
})
FRONTEND_SUFFIXES = frozenset({".js", ".css", ".html", ".svg", ".png", ".ico", ".woff", ".woff2", ".eot", ".ttf", ".webm"})
RUNTIME_METADATA_NAMES = frozenset({"metadata", "record", "wheel", "entry_points.txt", "top_level.txt", "license", "copying", "notice"})
_SENSITIVE_NAMES = frozenset({".env", "credentials.json", "secrets.json", "secret.json",
    "token.json", "id_rsa", "id_ed25519", "private_key", "private_key.pem"})
_SHA = re.compile(r"[0-9a-f]{64}\Z")
Cancel = Callable[[], bool]


def _cancel(should_cancel: Cancel | None) -> None:
    if should_cancel is not None and should_cancel():
        raise InterruptedError("Video dependency pack selection cancelled.")


def _contained(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((os.path.normcase(str(path)), os.path.normcase(str(root)))) == os.path.normcase(str(root))
    except ValueError:
        return False


def _safe_path(path: Path, *, directory: bool) -> os.stat_result:
    if not path.is_absolute() or ".." in path.parts or str(path).startswith("\\\\"):
        raise VideoPackError("Runtime pack selection needs explicit absolute paths.")
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError as exc:
            raise VideoPackError("A selected runtime path disappeared.") from exc
        if (stat.S_ISLNK(info.st_mode) or int(getattr(info, "st_file_attributes", 0)) &
                (0x400 | 0x1000 | 0x40000 | 0x400000)):
            raise VideoPackError("Runtime pack selection refuses redirected or cloud paths.")
    info = path.lstat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise VideoPackError("Runtime pack selection needs ordinary local files and directories.")
    return info


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            int(getattr(info, "st_file_attributes", 0)))


def _hash_file(path: Path, *, should_cancel: Cancel | None) -> tuple[str, int, tuple[int, int, int, int, int]]:
    _cancel(should_cancel)
    before = _safe_path(path, directory=False)
    if not 0 <= before.st_size <= MAX_TOTAL_BYTES:
        raise VideoPackError("Selected runtime file exceeds the bounded pack size.")
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0)) | int(getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    total = 0
    with os.fdopen(os.open(path, flags), "rb") as handle:
        if _identity(os.fstat(handle.fileno())) != _identity(before):
            raise VideoPackError("Selected runtime file changed before reading.")
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            _cancel(should_cancel)
            total += len(chunk)
            if total > before.st_size or total > MAX_TOTAL_BYTES:
                raise VideoPackError("Selected runtime file grew beyond its reviewed bound.")
            digest.update(chunk)
        if total != before.st_size:
            raise VideoPackError("Selected runtime file changed length during reading.")
        if _identity(os.fstat(handle.fileno())) != _identity(before):
            raise VideoPackError("Selected runtime file changed while reading.")
    if _identity(_safe_path(path, directory=False)) != _identity(before):
        raise VideoPackError("Selected runtime file changed after reading.")
    return digest.hexdigest(), before.st_size, _identity(before)


def _walk_allowed(root: Path, subtree: str, *, should_cancel: Cancel | None,
                  selected_suffixes: frozenset[str], visited: list[int],
                  omissions: set[str], protected: tuple[Path, ...]) -> list[Path]:
    start = root / subtree
    if not os.path.lexists(start):
        return []
    if any(_contained(start, boundary) for boundary in protected):
        omissions.add("explicit_protected_subtree_excluded")
        return []
    _safe_path(start, directory=True)
    pending = [start]
    selected: list[Path] = []
    while pending:
        _cancel(should_cancel)
        folder = pending.pop()
        _safe_path(folder, directory=True)
        entries = []
        with os.scandir(folder) as iterator:
            for entry in iterator:
                _cancel(should_cancel)
                visited[0] += 1
                if visited[0] > MAX_VISITED_ENTRIES:
                    raise VideoPackError("Runtime source tree exceeds the bounded inspection count.")
                entries.append(entry)
        entries.sort(key=lambda item: (item.name.casefold(), item.name))
        for entry in entries:
            name = entry.name
            folded = name.casefold()
            # This exact installed Comfy API source package is named "input";
            # ordinary user/input trees remain excluded everywhere else.
            public_api_input = (subtree == "comfy_api" and folder == start and name == "input"
                                and entry.is_dir(follow_symlinks=False))
            if (folded in GENERATED_DIRS or (folded in PRIVATE_DIRS and not public_api_input) or name.startswith(".")
                    or folded in _SENSITIVE_NAMES):
                omissions.add("excluded_private_or_generated_subtree_or_file")
                continue  # Do not stat or descend into excluded/private locations.
            child = Path(entry.path)
            if any(_contained(child, boundary) for boundary in protected):
                omissions.add("explicit_protected_subtree_excluded")
                continue
            info = _safe_path(child, directory=entry.is_dir(follow_symlinks=False))
            if stat.S_ISDIR(info.st_mode):
                pending.append(child)
            elif (child.suffix.casefold() in selected_suffixes or folded in RUNTIME_METADATA_NAMES
                    or child.relative_to(root).as_posix() in SOURCE_DATA_FILES
                    or child.relative_to(root).as_posix() in RUNTIME_DATA_FILES
                    or (child.relative_to(root).as_posix().startswith(RUNTIME_DOCS_PREFIX)
                        and child.suffix.casefold() in RUNTIME_DOCS_SUFFIXES)
                    or (child.relative_to(root).as_posix().startswith(RUNTIME_TEMPLATE_MEDIA_PREFIX)
                        and child.suffix.casefold() in RUNTIME_TEMPLATE_MEDIA_SUFFIXES)
                    or (child.relative_to(root).as_posix().casefold().startswith(
                        "lib/site-packages/comfyui_frontend_package/")
                        and child.suffix.casefold() in FRONTEND_SUFFIXES)):
                selected.append(child)
            else:
                omissions.add("unlisted_runtime_or_source_file_types_omitted")
    return selected


def _source_revision(source_hashes: dict[str, str]) -> str:
    raw = json.dumps(source_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "content-sha256:" + hashlib.sha256(raw).hexdigest()


def build_runtime_pack_selection(preview: RuntimePreview, *,
                                 should_cancel: Cancel | None = None) -> PackSelection:
    """Select only explicit reviewed roots and retain unqualified closure/license gates."""
    if not isinstance(preview, RuntimePreview) or not isinstance(preview.fingerprint, str) or not _SHA.fullmatch(preview.fingerprint):
        raise VideoPackError("An exact reviewed RuntimePreview is required.")
    config = preview.config
    source = Path(config.source_root)
    runtime = Path(config.python_runtime_root)
    executable = Path(config.python_executable)
    if (not source.is_absolute() or not runtime.is_absolute() or not executable.is_absolute()
            or not _contained(executable, runtime) or _contained(source, runtime)
            or _contained(runtime, source)):
        raise VideoPackError("Reviewed source, Python runtime and executable paths are not separate and explicit.")
    _safe_path(source, directory=True)
    _safe_path(runtime, directory=True)
    _safe_path(executable, directory=False)
    protected = tuple(Path(path) for path in preview.protected_roots)
    for root in protected:
        if _contained(source, root) or _contained(runtime, root):
            raise VideoPackError("A selected runtime root overlaps a protected location.")
    if set(preview.source_hashes) != set(SOURCE_FILES) or any(
        not isinstance(value, str) or not _SHA.fullmatch(value)
        for value in preview.source_hashes.values()
    ):
        raise VideoPackError("Reviewed source hashes do not match the pinned Comfy source list.")
    if not isinstance(preview.python_sha256, str) or not _SHA.fullmatch(preview.python_sha256):
        raise VideoPackError("Reviewed Python executable has no full SHA-256 pin.")
    if set(preview.model_manifest) != {"transformer", "text_encoder", "vae"}:
        raise VideoPackError("Reviewed runtime must have exactly the three selected model roles.")
    _cancel(should_cancel)
    visited = [0]
    omissions: set[str] = set()
    source_paths = {source / rel for rel in SOURCE_FILES}
    source_paths.update(source / rel for rel in SOURCE_DATA_FILES if os.path.lexists(source / rel))
    for name in SOURCE_TOP_LEVEL:
        candidate = source / name
        if os.path.lexists(candidate):
            source_paths.add(candidate)
    for tree in SOURCE_CODE_TREES:
        source_paths.update(_walk_allowed(source, tree, should_cancel=should_cancel,
            selected_suffixes=SOURCE_SUFFIXES, visited=visited, omissions=omissions,
            protected=protected))
    requirements = source / "requirements.txt"
    if requirements not in source_paths:
        raise VideoPackError("Reviewed Comfy source has no requirements.txt dependency manifest.")
    files: list[PackFile] = []
    total = 0

    def add(role: str, path: Path, relative: str, *, pinned: str | None = None,
            pinned_size: int | None = None, pinned_state: tuple[int, ...] | None = None,
            license_id: str = "NOASSERTION", evidence: dict | None = None) -> None:
        nonlocal total
        if len(files) >= MAX_FILES:
            raise VideoPackError("Selected runtime exceeds the pack file-count bound.")
        if any(_contained(path, boundary) for boundary in protected):
            raise VideoPackError("A selected file overlaps an explicit protected location.")
        digest, size, identity = _hash_file(path, should_cancel=should_cancel)
        if (pinned is not None and digest != pinned) or (pinned_size is not None and size != pinned_size) or (
            pinned_state is not None and identity != pinned_state
        ):
            raise VideoPackError(f"Reviewed runtime byte or file identity changed: {relative}.")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise VideoPackError("Selected runtime exceeds the bounded pack byte count.")
        record = {"authority": "reviewed RuntimePreview", "preview_fingerprint": preview.fingerprint,
                  "license_source": "not_supplied", **(evidence or {})}
        files.append(PackFile(role, path, relative, digest, size, license_id, record))

    for path in sorted(source_paths, key=lambda item: item.relative_to(source).as_posix().casefold()):
        _cancel(should_cancel)
        rel = path.relative_to(source).as_posix()
        if path.relative_to(source).parts[0].casefold() in ROOT_USER_DIRS:
            raise VideoPackError("Pinned source path crossed an excluded user subtree.")
        if rel == "requirements.txt":
            role = "dependency_manifest"
        else:
            prefix = "license:f" if rel.casefold() in {"license", "license.txt"} else "source:f"
            role = prefix + hashlib.sha256(rel.encode("utf-8")).hexdigest()[:32]
        add(role, path, "source/" + rel, pinned=preview.source_hashes.get(rel),
            evidence={"reviewed_source_sha256": preview.source_hashes[rel]} if rel in preview.source_hashes else
                     {"additional_source_file_not_pinned_by_preview": True})

    add("interpreter", executable, "runtime/" + executable.relative_to(runtime).as_posix(),
        pinned=preview.python_sha256, evidence={"reviewed_python_sha256": preview.python_sha256})
    top_entries: list[Path] = []
    with os.scandir(runtime) as iterator:
        for entry in iterator:
            _cancel(should_cancel)
            visited[0] += 1
            if visited[0] > MAX_VISITED_ENTRIES:
                raise VideoPackError("Runtime source tree exceeds the bounded inspection count.")
            top_entries.append(Path(entry.path))
    embedded = executable.parent == runtime and any(
        path.name.casefold().startswith("python") and path.name.casefold().endswith("._pth")
        for path in top_entries
    ) and any(path.name.casefold().startswith("python") and path.suffix.casefold() == ".dll"
              for path in top_entries)
    if embedded:
        top_suffixes = RUNTIME_SUFFIXES
        for path in sorted(top_entries, key=lambda item: item.name.casefold()):
            if (path == executable or path.name.casefold() in ROOT_USER_DIRS
                    or path.name.startswith(".") or path.name.casefold() in _SENSITIVE_NAMES):
                continue
            if (path.suffix.casefold() in top_suffixes or path.name.casefold() in RUNTIME_METADATA_NAMES):
                add("dependency:f" + hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:32],
                    path, "runtime/" + path.name)
            elif not path.is_dir():
                omissions.add("unlisted_runtime_or_source_file_types_omitted")
        for tree in RUNTIME_TREES:
            for path in _walk_allowed(runtime, tree, should_cancel=should_cancel,
                                      selected_suffixes=RUNTIME_SUFFIXES,
                                      visited=visited, omissions=omissions,
                                      protected=protected):
                rel = path.relative_to(runtime).as_posix()
                add("dependency:f" + hashlib.sha256(rel.encode("utf-8")).hexdigest()[:32],
                    path, "runtime/" + rel)
    else:
        omissions.add("python_runtime_layout_unrecognized")

    for role in ("transformer", "text_encoder", "vae"):
        row = preview.model_manifest[role]
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise VideoPackError("Reviewed model manifest is malformed.")
        path = Path(row["path"])
        if not path.is_absolute() or path.suffix.casefold() != ".safetensors" or _contained(path, source) or _contained(path, runtime) or any(
            _contained(path, root) for root in protected
        ):
            raise VideoPackError("Reviewed model path is not a separate explicit resource.")
        expected = getattr(config, role)
        if Path(expected.path) != path or expected.sha256 != row.get("sha256") or row.get("file_name") != path.name:
            raise VideoPackError("Reviewed model binding differs from the runtime preview.")
        sha = row.get("sha256")
        size = row.get("size")
        state = row.get("file_state")
        if (not isinstance(sha, str) or not _SHA.fullmatch(sha) or type(size) is not int
                or size <= 0 or not isinstance(state, list) or len(state) != 5
                or any(type(value) is not int for value in state)):
            raise VideoPackError("Reviewed model lacks full hash, size, or file state.")
        license_id = row.get("license_id", "NOASSERTION")
        if not isinstance(license_id, str) or not license_id.strip():
            raise VideoPackError("Reviewed model license identifier is malformed.")
        evidence = {"reviewed_resource_id": "sha256:" + sha,
                    "reviewed_file_state": state}
        for key in ("source_revision", "license_source", "source_record"):
            if key in row:
                evidence[key] = row[key]
        add("model:" + role, path, "models/" + role + "/" + path.name,
            pinned=sha, pinned_size=size, pinned_state=tuple(state),
            license_id=license_id, evidence=evidence)
        if license_id == "NOASSERTION" or "license_source" not in row:
            omissions.add("model_license_evidence_not_supplied")

    source_revision = _source_revision({item.relative_path: item.sha256 for item in files
        if item.relative_path.startswith("source/")})
    gates = {"source_vcs_revision_not_supplied", "source_license_evidence_not_supplied",
             "installed_dependency_closure_not_verified", "native_codec_attribution_not_verified"} | omissions
    pins = {"runtime_preview_fingerprint": preview.fingerprint,
            "source_content_revision": source_revision,
            "python_executable_sha256": preview.python_sha256,
            **{f"model_{role}_sha256": preview.model_manifest[role]["sha256"]
               for role in ("transformer", "text_encoder", "vae")}}
    return PackSelection(tuple(files), ("transformer", "text_encoder", "vae"), source_revision,
        "runtime/" + executable.relative_to(runtime).as_posix(), "source/requirements.txt",
        pins, runtime_closure_supplied=False, attribution_gates=tuple(sorted(gates)),
        extra_fields={"reviewed_runtime_fingerprint": preview.fingerprint,
                      "reviewed_source_hashes": dict(preview.source_hashes),
                      "python_runtime_scope": "embedded_allowlist_unqualified" if embedded else "executable_only_unknown_layout",
                      "visited_entry_bound": MAX_VISITED_ENTRIES})
