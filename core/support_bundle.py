from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING
from suite_core.diagnostic_io import write_verified_zip

if TYPE_CHECKING:
    from .dataset_manager import DatasetManager


SAFE_SETTING_FIELDS = (
    "auto_import_source_evidence",
    "auto_merge_ai_tags",
    "auto_run_joy_on_managed_images",
    "auto_run_wd_on_managed_images",
    "check_dependencies_on_startup",
    "color_blind_mode",
    "gallery_card_width",
    "gallery_page_size",
    "restore_last_dataset",
    "show_tooltips",
    "suggest_joycaption_for_missing_descriptions",
    "suggest_wd_for_missing_tags",
    "ui_theme",
    "update_channel",
    "validate_library_on_startup",
    "wd_batch_size",
    "wd_consensus_minimum",
    "wd_execution_provider",
)
MAX_LOG_FILES = 5
MAX_LOG_LINES = 250
MAX_LOG_CHARACTERS = 80_000

_WINDOWS_PATH = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:[\\/]|\\\\)[^\r\n|\"'<>]*"
)
_POSIX_HOME_PATH = re.compile(r"(?<![A-Za-z0-9])/(?:home|Users)/[^/\s]+[^\r\n|\"'<>]*")
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_IMAGE_OR_SIDECAR_NAME = re.compile(
    r"(?i)(?<![\w])[\w .()\[\]{}-]{1,200}\."
    r"(?:png|jpe?g|webp|bmp|gif|tiff?|avif|jxl|txt|tags\.json)(?![\w])"
)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def redact_diagnostic_text(value: str, *, app_root: Path | None = None) -> str:
    """Remove common machine, account, and filesystem identity from diagnostics."""
    text = str(value or "").replace("\x00", "")
    text = _WINDOWS_PATH.sub("<redacted path>", text)
    text = _POSIX_HOME_PATH.sub("<redacted path>", text)
    text = _IMAGE_OR_SIDECAR_NAME.sub("<redacted file>", text)
    replacements: list[tuple[str, str]] = []
    if app_root is not None:
        try:
            replacements.append((str(Path(app_root).resolve()), "<application folder>"))
        except OSError:
            pass
    for candidate, label in (
        (os.environ.get("USERPROFILE", ""), "<user folder>"),
        (str(Path.home()), "<user folder>"),
    ):
        if candidate:
            replacements.append((candidate, label))
    for candidate, label in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        text = re.sub(re.escape(candidate), label, text, flags=re.IGNORECASE)
    text = _EMAIL.sub("<redacted email>", text)
    return text


def sanitized_settings(payload: Any) -> dict[str, Any]:
    """Return a small allowlist that cannot contain libraries, prompts, tags, or paths."""
    if not isinstance(payload, dict):
        return {}
    result: dict[str, Any] = {}
    for key in SAFE_SETTING_FIELDS:
        if key not in payload:
            continue
        value = payload.get(key)
        if isinstance(value, (bool, int, float)) or value is None:
            result[key] = value
        elif isinstance(value, str):
            result[key] = value[:120]
    enabled = payload.get("enabled_wd_taggers")
    if isinstance(enabled, list):
        # Adapter names are capabilities, not user content. Bound both count and length.
        result["enabled_wd_taggers"] = [str(item)[:160] for item in enabled[:8]]
    return result


def sanitized_settings_file(path: Path) -> str:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        payload = {}
    return json.dumps(sanitized_settings(payload), indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def sanitized_log_excerpt(
    log_dir: Path,
    *,
    app_root: Path | None = None,
    max_files: int = MAX_LOG_FILES,
    max_lines: int = MAX_LOG_LINES,
    max_characters: int = MAX_LOG_CHARACTERS,
) -> str:
    """Return a bounded, path-redacted tail from recent plain-text log files."""
    directory = Path(log_dir)
    if not directory.is_dir():
        return ""
    candidates = []
    for path in directory.iterdir():
        if not path.is_file() or path.suffix.casefold() not in {".log", ".txt"}:
            continue
        try:
            candidates.append((path.stat().st_mtime_ns, path))
        except OSError:
            continue
    selected = [item[1] for item in sorted(candidates, reverse=True)[: max(0, int(max_files))]]
    remaining = max(0, int(max_characters))
    sections: list[str] = []
    for path in selected:
        if remaining <= 0:
            break
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        tail = "\n".join(lines[-max(0, int(max_lines)) :])
        cleaned = redact_diagnostic_text(tail, app_root=app_root)
        cleaned = cleaned[-remaining:]
        sections.append(f"--- {path.name}\n{cleaned}".rstrip())
        remaining -= len(cleaned)
    return "\n\n".join(sections).rstrip() + ("\n" if sections else "")


def _diagnostic_files(app_root: Path, dataset: DatasetManager | None) -> list[tuple[str, bytes]]:
    root = Path(app_root)
    files: list[tuple[str, bytes]] = []
    system = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
        },
    }
    files.append(
        (
            "system.json",
            (json.dumps(system, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
    )
    if dataset is not None:
        summary = getattr(dataset, "summary", None)
        summary_payload = {
            key: int(value)
            for key, value in vars(summary).items()
            if isinstance(value, int) and not isinstance(value, bool)
        } if summary is not None else {}
        files.append(
            (
                "dataset_counts.json",
                (json.dumps(summary_payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
        )
    settings_text = sanitized_settings_file(root / "data" / "user" / "settings.json")
    files.append(("settings_sanitized.json", settings_text.encode("utf-8")))
    logs = sanitized_log_excerpt(root / "logs", app_root=root)
    if logs:
        files.append(("logs/recent_sanitized.txt", logs.encode("utf-8")))
    manifest = {
        "schema_version": 2,
        "privacy": {
            "excluded": [
                "image pixels and thumbnails",
                "image names and paths",
                "Image Knowledge and captions",
                "SDXL Tags and Plain Language Descriptions",
                "generator payloads",
                "System Knowledge and review tasks",
                "account identity and executable paths",
            ],
            "notes": "Logs are bounded and path-redacted. Review before sharing.",
        },
        "files": [
            {
                "name": name,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for name, payload in sorted(files)
        ],
    }
    files.append(
        (
            "manifest.json",
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
    )
    return files


def create_diagnostic_bundle(app_root: Path, dataset: DatasetManager | None = None) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    output = Path(app_root) / f"CollectiGen_Diagnostics_{stamp}.zip"
    write_verified_zip(output, _diagnostic_files(Path(app_root), dataset))
    return output


def create_rules_support_bundle(app_root: Path, dataset: DatasetManager | None = None) -> Path:
    """Create the separately requested, knowledge-bearing rules-review bundle."""
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    output = Path(app_root) / f"CollectiGen_RulesSupport_{stamp}.zip"
    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp)
        manifest = {
            "created": datetime.now().isoformat(timespec="seconds"),
            "files": [],
            "privacy": "Contains user-authored System Knowledge. Review before sharing.",
        }
        for name in ("tags.json", "characters.json", "series.json", "tasks.json"):
            source = Path(app_root) / "data" / "user" / name
            if source.exists():
                shutil.copy2(source, work / name)
                manifest["files"].append(name)
        if dataset and dataset.root:
            _write_json(
                work / "dataset_statistics.json",
                {
                    "dataset_name": dataset.root.name,
                    "summary": dataset.summary.__dict__,
                    "top_tags": sorted(
                        dataset.tag_counts.items(), key=lambda item: (-item[1], item[0])
                    )[:1000],
                },
            )
            manifest["files"].append("dataset_statistics.json")
        _write_json(work / "manifest.json", manifest)
        write_verified_zip(
            output,
            (
                (file.relative_to(work).as_posix(), file.read_bytes())
                for file in work.rglob("*")
                if file.is_file()
            ),
        )
    return output
