from __future__ import annotations

import hashlib
import importlib
import json
import platform
import re
import shutil
import stat
import sys
import sysconfig
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


RUNTIME_MANIFEST_SCHEMA = 2
SUPPORTED_RUNTIME_MANIFEST_SCHEMAS = (1, RUNTIME_MANIFEST_SCHEMA)
CORE_IMPORTS = (
    ("PySide6", "PySide6", "__version__"),
    ("Pillow", "PIL", "__version__"),
    ("PyAV", "av", "__version__"),
)
CORE_LOAD_MODULES = (
    ("PySide6 Qt Core", "PySide6.QtCore"),
    ("PySide6 Qt GUI", "PySide6.QtGui"),
    ("PySide6 Qt Widgets", "PySide6.QtWidgets"),
    ("Pillow imaging", "PIL.Image"),
    ("PyAV video codecs", "av"),
)


ORGANIZER_IMAGE_IMPORTS = (
    ("PySide6", "PySide6", "__version__"),
    ("Pillow", "PIL", "__version__"),
)
ORGANIZER_IMAGE_LOAD_MODULES = (
    ("PySide6 Qt Core", "PySide6.QtCore"),
    ("PySide6 Qt GUI", "PySide6.QtGui"),
    ("PySide6 Qt Widgets", "PySide6.QtWidgets"),
    ("Pillow imaging", "PIL.Image"),
)


def runtime_profile_modules(capability_profile: str = "collectigen-core"):
    """Select a finite caller-admitted contract; never infer it from payload data."""
    if capability_profile == "collectigen-core":
        return CORE_IMPORTS, CORE_LOAD_MODULES
    if capability_profile == "organizer-image":
        return ORGANIZER_IMAGE_IMPORTS, ORGANIZER_IMAGE_LOAD_MODULES
    raise ValueError("Unsupported runtime capability profile.")


@dataclass(frozen=True)
class RuntimeCheck:
    ready: bool
    issues: tuple[str, ...]
    manifest: dict[str, Any] | None = None


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("The runtime manifest must contain a JSON object.")
    return payload


def install_pip_wheel(
    wheel_path: str | Path,
    *,
    expected_version: str,
    expected_sha256: str,
    purelib_path: str | Path | None = None,
) -> Path:
    """Install one reviewed, pure-Python pip wheel with rollback on failure."""
    wheel = Path(wheel_path).resolve()
    if sha256_file(wheel).casefold() != expected_sha256.casefold():
        raise ValueError("The pip wheel does not match its reviewed SHA-256.")
    purelib = Path(purelib_path or sysconfig.get_path("purelib")).resolve()
    dist_info_name = f"pip-{expected_version}.dist-info"
    required = {
        f"{dist_info_name}/METADATA",
        f"{dist_info_name}/WHEEL",
        f"{dist_info_name}/RECORD",
    }
    with zipfile.ZipFile(wheel) as archive:
        names: set[str] = set()
        for info in archive.infolist():
            name = info.filename.replace("\\", "/").rstrip("/")
            if not name:
                continue
            parts = Path(name).parts
            if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in parts:
                raise ValueError(f"The pip wheel contains an unsafe path: {info.filename}")
            if stat.S_ISLNK((info.external_attr >> 16) & 0o170000):
                raise ValueError(f"The pip wheel contains a symbolic link: {info.filename}")
            if parts[0] not in {"pip", dist_info_name}:
                raise ValueError(f"The pip wheel contains an unexpected root: {parts[0]}")
            names.add(name)
        missing = sorted(required - names)
        if missing:
            raise ValueError(f"The pip wheel is missing required metadata: {', '.join(missing)}")
        metadata = archive.read(f"{dist_info_name}/METADATA").decode("utf-8")
        wheel_metadata = archive.read(f"{dist_info_name}/WHEEL").decode("utf-8")
        if not re.search(r"(?mi)^Name:\s*pip\s*$", metadata):
            raise ValueError("The reviewed wheel does not identify pip.")
        if not re.search(rf"(?mi)^Version:\s*{re.escape(expected_version)}\s*$", metadata):
            raise ValueError("The pip wheel version does not match its reviewed pin.")
        if not re.search(r"(?mi)^Root-Is-Purelib:\s*true\s*$", wheel_metadata):
            raise ValueError("The pip wheel is not a pure-library wheel.")
        if not re.search(r"(?mi)^Tag:\s*py3-none-any\s*$", wheel_metadata):
            raise ValueError("The pip wheel is not compatible with the reviewed Python target.")

        purelib.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".collectigen-pip-", dir=purelib.parent) as folder:
            transaction = Path(folder)
            stage = transaction / "stage"
            backup = transaction / "backup"
            stage.mkdir()
            backup.mkdir()
            archive.extractall(stage)
            purelib.mkdir(parents=True, exist_ok=True)
            previous = [purelib / "pip", *sorted(purelib.glob("pip-*.dist-info"))]
            moved_previous: list[tuple[Path, Path]] = []
            installed: list[Path] = []
            try:
                for target in previous:
                    if not target.exists():
                        continue
                    saved = backup / target.name
                    target.replace(saved)
                    moved_previous.append((target, saved))
                for name in ("pip", dist_info_name):
                    source = stage / name
                    target = purelib / name
                    installed.append(target)
                    shutil.copytree(source, target)
                importlib.invalidate_caches()
            except Exception:
                for target in reversed(installed):
                    if target.is_dir():
                        shutil.rmtree(target)
                    elif target.exists():
                        target.unlink()
                for target, saved in reversed(moved_previous):
                    saved.replace(target)
                importlib.invalidate_caches()
                raise
    return purelib / dist_info_name


def load_expected_runtime_contract(
    pins_path: str | Path,
    lock_path: str | Path,
    *, capability_profile: str = "collectigen-core",
) -> dict[str, Any]:
    """Load the checked-in dependency contract that determines runtime reuse."""
    runtime_profile_modules(capability_profile)
    pins_file = Path(pins_path)
    lock_file = Path(lock_path)
    pins = load_manifest(pins_file)
    if pins.get("schema_version") not in (1, 2):
        raise ValueError("The runtime pin contract has an unsupported schema.")

    locked_versions: dict[str, str] = {}
    for raw_line in lock_file.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        requirement = line.split(maxsplit=1)[0]
        if "==" not in requirement:
            raise ValueError(f"The core runtime lock contains an unsupported requirement: {requirement}")
        name, version = requirement.split("==", 1)
        locked_versions[name.strip().casefold().replace("_", "-")] = version.strip()

    try:
        core_packages = {
            "PySide6": locked_versions["pyside6-essentials"],
            "Pillow": locked_versions["pillow"],
        }
        if capability_profile == "collectigen-core":
            core_packages["PyAV"] = locked_versions["av"]
        elif set(locked_versions) != {"pillow", "pyside6-essentials", "shiboken6"}:
            raise ValueError("The Organizer image lock has an unsupported package closure.")
        return {
            "python_version": str(pins["python"]["version"]),
            "architecture": str(pins["python"]["architecture"]),
            "exiftool_version": str(pins["exiftool"]["version"]),
            "core_packages": core_packages,
            "runtime_pins_sha256": sha256_file(pins_file),
            "runtime_lock_sha256": sha256_file(lock_file),
        }
    except KeyError as exc:
        raise ValueError(f"The runtime dependency contract is incomplete: {exc}") from exc


def core_import_issues(*, capability_profile: str = "collectigen-core") -> list[str]:
    imports,load_modules=runtime_profile_modules(capability_profile)
    issues: list[str] = []
    if sys.version_info < (3, 11):
        issues.append(f"Python {platform.python_version()} is older than the supported 3.11 minimum required by video playback.")
    for display, module_name, version_attribute in imports:
        try:
            module = importlib.import_module(module_name)
            version = str(getattr(module, version_attribute, "")).strip()
            if not version:
                issues.append(f"{display} did not report a version.")
        except (ImportError, OSError, RuntimeError) as exc:
            issues.append(f"{display} could not load: {type(exc).__name__}: {exc}")
    for display,module_name in load_modules:
        try:
            importlib.import_module(module_name)
        except (ImportError,OSError,RuntimeError) as exc:
            issues.append(f"{display} could not load: {type(exc).__name__}: {exc}")
    return issues


def validate_runtime(
    manifest_path: str | Path,
    *,
    application_version: str,
    runtime_root: str | Path | None = None,
    verify_hashes: bool = True,
    expected_contract: dict[str, Any] | None = None,
    capability_profile: str = "collectigen-core",
) -> RuntimeCheck:
    """Validate a built portable runtime without coupling it to an app patch."""
    del application_version  # Retained for callers; app and runtime versions advance independently.
    imports,_=runtime_profile_modules(capability_profile)
    issues = core_import_issues(capability_profile=capability_profile)
    manifest_file = Path(manifest_path)
    try:
        manifest = load_manifest(manifest_file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        issues.append(f"Runtime manifest could not be read: {exc}")
        return RuntimeCheck(False, tuple(issues), None)

    schema = manifest.get("schema_version")
    if schema not in SUPPORTED_RUNTIME_MANIFEST_SCHEMAS:
        issues.append("Runtime manifest schema is missing or unsupported.")
    if manifest.get("profile") != capability_profile:
        issues.append("Runtime manifest does not identify the caller-admitted runtime profile.")
    if capability_profile == "organizer-image" and schema != RUNTIME_MANIFEST_SCHEMA:
        issues.append("Organizer image runtime requires the current reviewed manifest schema.")
    if manifest.get("architecture") != "amd64":
        issues.append("Runtime architecture is not the supported amd64 build.")
    if manifest.get("python_version") != platform.python_version():
        issues.append(
            "Runtime Python version does not match its manifest "
            f"({platform.python_version()} != {manifest.get('python_version')})."
        )

    actual_versions: dict[str, str] = {}
    for display, module_name, version_attribute in imports:
        try:
            actual_versions[display] = str(
                getattr(importlib.import_module(module_name), version_attribute, "")
            )
        except (ImportError, OSError, RuntimeError):
            continue
    expected_versions = manifest.get("core_packages")
    if not isinstance(expected_versions, dict):
        issues.append("Runtime manifest has no resolved core-package inventory.")
    else:
        if capability_profile == "organizer-image":
            if set(expected_versions) != {"PySide6","Pillow"}:
                issues.append("Organizer image core-package inventory has an unsupported closure.")
            if any(str(row.get("name","")).casefold()=="av"
                   for row in manifest.get("resolved_packages",[]) if isinstance(row,dict)):
                issues.append("Organizer image base must not contain PyAV.")
        for display, actual in actual_versions.items():
            expected = str(expected_versions.get(display, ""))
            if not expected or expected != actual:
                issues.append(f"{display} version does not match its manifest ({actual} != {expected or 'missing'}).")

    if expected_contract is not None:
        for key, label in (
            ("python_version", "Python version"),
            ("architecture", "architecture"),
            ("exiftool_version", "ExifTool version"),
        ):
            expected = str(expected_contract.get(key, ""))
            actual = str(manifest.get(key, ""))
            if not expected or actual != expected:
                issues.append(
                    f"Runtime {label} does not match the checked-in dependency contract "
                    f"({actual or 'missing'} != {expected or 'missing'})."
                )
        contract_packages = expected_contract.get("core_packages")
        if not isinstance(contract_packages, dict):
            issues.append("The checked-in runtime contract has no core-package inventory.")
        elif isinstance(expected_versions, dict):
            for display, expected in contract_packages.items():
                actual = str(expected_versions.get(display, ""))
                if actual != str(expected):
                    issues.append(
                        f"Runtime {display} does not match the checked-in dependency contract "
                        f"({actual or 'missing'} != {expected})."
                    )
        if schema == RUNTIME_MANIFEST_SCHEMA:
            for key, label in (
                ("runtime_pins_sha256", "source-pin"),
                ("runtime_lock_sha256", "core-lock"),
            ):
                expected = str(expected_contract.get(key, "")).casefold()
                actual = str(manifest.get(key, "")).casefold()
                if not expected or actual != expected:
                    issues.append(
                        f"Runtime {label} fingerprint does not match the checked-in dependency contract."
                    )

    critical_files = manifest.get("critical_files")
    if not isinstance(critical_files, dict) or not critical_files:
        issues.append("Runtime manifest has no critical-file inventory.")
    elif verify_hashes:
        root = Path(runtime_root) if runtime_root is not None else manifest_file.parent.parent
        for relative, expected in critical_files.items():
            candidate = root / Path(str(relative))
            if not candidate.is_file():
                issues.append(f"Critical runtime file is missing: {relative}")
                continue
            try:
                actual = sha256_file(candidate)
            except OSError as exc:
                issues.append(f"Critical runtime file could not be read: {relative}: {exc}")
                continue
            if actual.casefold() != str(expected).casefold():
                issues.append(f"Critical runtime file is damaged: {relative}")

    return RuntimeCheck(not issues, tuple(issues), manifest)
