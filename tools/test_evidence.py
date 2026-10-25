from __future__ import annotations

import hashlib
import json
import importlib.metadata
import os
import platform
import struct
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path,PurePosixPath


EVIDENCE_SCHEMA_VERSION = 2
IDENTITY_FIELDS = (
    "evidence_schema_version",
    "application_version",
    "source_fingerprint_sha256",
    "runner_sha256",
    "environment_identity",
)
_SOURCE_DIRECTORIES = ("core", "qt_app", "assets/brand")
_PRIVATE_DEFAULTS = frozenset({"promotion_exclusions.json","promotion_manifest.json"})
_SOURCE_FILES = (
    "version.py", "tools/test_evidence.py", "requirements-runtime.lock",
    "tools/runtime-pins.json", "runtime_contract.py",
    "tools/public-source-manifest.json", "data/model_workflow_baseline.json",
)


def runtime_environment_identity() -> dict:
    dependencies={}
    for distribution in ("PySide6","PySide6-Essentials","shiboken6","Pillow","av"):
        try: dependencies[distribution]=importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            dependencies[distribution]="not-installed"
    return {
        "python_implementation":platform.python_implementation(),
        "python_version":platform.python_version(),
        "operating_system":platform.system(),
        "platform_architecture":platform.machine() or "unknown",
        "pointer_bits":struct.calcsize("P")*8,
        "dependencies":dependencies,
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_fingerprint(root: Path, runner: Path | None = None) -> str:
    root = root.resolve()
    candidates = [root / relative for relative in _SOURCE_FILES]
    if runner is not None:
        candidates.append(runner)
    # Default source files must be explicitly public. Never enumerate or inspect
    # the working defaults directory: it can also hold private runtime records.
    manifest=root/"tools/public-source-manifest.json"
    if manifest.is_file():
        source_list=json.loads(manifest.read_text(encoding="utf-8"))["files"]
        for relative in source_list:
            if not isinstance(relative,str):continue
            parts=PurePosixPath(relative).parts
            if (len(parts)<3 or parts[:2]!=("data","defaults")
                or ".." in parts or "\\" in relative or ":" in relative
                or parts[-1].casefold() in _PRIVATE_DEFAULTS):continue
            path=root
            for part in parts:
                path=path/part
                if path.is_symlink() or getattr(path,"is_junction",lambda:False)():
                    raise ValueError("Public default source must not traverse redirected paths.")
            candidates.append(path)
    for relative in _SOURCE_DIRECTORIES:
        directory = root / relative
        if directory.is_dir():
            candidates.extend(
                path for path in directory.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts
            )
    digest = hashlib.sha256()
    for path in sorted({path.resolve() for path in candidates if path.is_file()}):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def git_head(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True,
            capture_output=True, text=True, encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"
    return completed.stdout.strip() or "unavailable"


def build_test_evidence(root: Path, application_version: str, runner: Path) -> dict:
    return {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "application_version": application_version,
        "git_head": git_head(root),
        "source_fingerprint_sha256": source_fingerprint(root, runner),
        "runner_sha256": file_sha256(runner),
        "environment_identity": runtime_environment_identity(),
    }


def evidence_identity(evidence: dict) -> tuple:
    return tuple(evidence.get(field) for field in IDENTITY_FIELDS)


def default_visual_output(
    root: Path,
    *,
    now: datetime | None = None,
    process_id: int | None = None,
    token: str | None = None,
) -> Path:
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S%fZ")
    pid = os.getpid() if process_id is None else process_id
    suffix = token or uuid.uuid4().hex[:8]
    return root / "logs" / "qt_ui_visuals" / f"{timestamp}-{pid}-{suffix}"


def claim_output_directory(path: Path) -> Path:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir(exist_ok=False)
    return path
