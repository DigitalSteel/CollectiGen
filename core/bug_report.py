from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .support_bundle import redact_diagnostic_text, write_verified_zip


BUG_REPORT_SCHEMA_VERSION = 2
SUPPORTED_BUG_REPORT_SCHEMA_VERSIONS = {1, BUG_REPORT_SCHEMA_VERSION}
BUG_REPORT_TRACKING_SCHEMA_VERSION = 1
REPORT_ID_PATTERN = re.compile(r"^CBR-[A-F0-9]{16}$")
REPORT_DIRECTORY_PATTERN = re.compile(
    r"^CBR-[A-F0-9]{16}(?:_[A-Za-z0-9-]{1,48})?$"
)
BUG_REPORT_FEEDBACK_TYPES = {
    "bug": "Something isn't working",
    "workflow": "A workflow is difficult or confusing",
    "feature": "Feature or improvement request",
}
BUG_REPORT_FIELD_HEADINGS = {
    "bug": (
        "Observed behavior",
        "Expected behavior",
        "Steps to reproduce",
    ),
    "workflow": (
        "Workflow or goal",
        "What would make it easier",
        "Where it becomes difficult",
    ),
    "feature": (
        "Problem or goal",
        "Feature or improvement",
        "Example use",
    ),
}
MAX_REPORT_FIELD = 20_000
MAX_REPORT_SUMMARY = 120
MAX_REPORT_NAME_SUMMARY = 48
MAX_CONTEXT_EVENTS = 50
MAX_CONTEXT_JOBS = 30
MAX_ATTACHMENT_BYTES = 12 * 1024 * 1024
MAX_SHAREABLE_REPORT_ZIP_BYTES = 20 * 1024 * 1024


class BugReportError(ValueError):
    pass


@dataclass(frozen=True)
class BugReportDraft:
    summary: str
    current_context: str
    desired_result: str
    steps_or_example: str
    feedback_type: str = "bug"


@dataclass(frozen=True)
class BugReportAttachment:
    name: str
    media_type: str
    payload: bytes
    description: str
    editable_text: bool = False


@dataclass(frozen=True)
class PreparedBugReport:
    report_id: str
    created_at: str
    content_fingerprint: str
    report: dict[str, Any]
    attachments: tuple[BugReportAttachment, ...] = ()
    privacy_manifest: dict[str, Any] = field(default_factory=dict)

    def human_readable(self) -> str:
        fields = self.report["report"]
        context = self.report["context"]
        headings = BUG_REPORT_FIELD_HEADINGS[fields["feedback_type"]]
        lines = [
            f"# CollectiGen report {self.report_id}",
            "",
            f"**Summary:** {fields['summary']}",
            (
                "**Feedback type:** "
                f"{BUG_REPORT_FEEDBACK_TYPES.get(fields['feedback_type'], fields['feedback_type'])}"
            ),
            f"**Created:** {self.created_at}",
            f"**CollectiGen:** {context.get('application_version', 'Unknown')}",
            f"**Page/workflow:** {context.get('page', 'Unknown')}",
            "",
            f"## {headings[0]}",
            "",
            fields["current_context"],
            "",
            f"## {headings[1]}",
            "",
            fields["desired_result"],
            "",
            f"## {headings[2]}",
            "",
            fields["steps_or_example"] or "Not provided.",
            "",
            "## Included diagnostic context",
            "",
            f"- Recent application events: {len(context.get('recent_events', []))}",
            f"- Active or failed jobs: {len(context.get('jobs', []))}",
            f"- Optional attachments: {len(self.attachments)}",
            f"- Content fingerprint: `{self.content_fingerprint}`",
            "",
            "This report was saved locally. CollectiGen did not upload or transmit it.",
            "",
        ]
        return "\n".join(lines)


@dataclass(frozen=True)
class SavedBugReport:
    directory: Path
    duplicate_of_report_id: str = ""


@dataclass(frozen=True)
class FeedbackReportSummary:
    directory: Path
    report_id: str
    created_at: str
    feedback_type: str
    summary: str
    attachment_count: int
    export_count: int = 0
    last_exported_at: str = ""


@dataclass(frozen=True)
class FeedbackArchiveExport:
    path: Path
    archive_id: str
    sha256: str


def _clean_field(
    value: Any,
    name: str,
    *,
    required: bool,
    maximum: int = MAX_REPORT_FIELD,
) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if required and not text:
        raise BugReportError(f"{name} is required.")
    if len(text) > maximum:
        raise BugReportError(f"{name} must be {maximum} characters or fewer.")
    return text


def report_directory_name(report_id: str, summary: str) -> str:
    """Return a stable-ID-first, portable, bounded report folder name."""
    if not REPORT_ID_PATTERN.fullmatch(str(report_id)):
        raise BugReportError("The report ID is invalid.")
    normalized = unicodedata.normalize("NFKD", str(summary or ""))
    ascii_summary = normalized.encode("ascii", errors="ignore").decode("ascii")
    suffix = re.sub(r"[^A-Za-z0-9]+", "-", ascii_summary).strip("-")
    suffix = suffix[:MAX_REPORT_NAME_SUMMARY].rstrip("-") or "Feedback"
    return f"{report_id}_{suffix}"


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _pretty_json(payload: Any) -> bytes:
    return (
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")


def _safe_context(value: Any, *, limit: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not isinstance(value, (list, tuple)):
        return result
    for raw in list(value)[-limit:]:
        if not isinstance(raw, Mapping):
            continue
        item: dict[str, Any] = {}
        for key, candidate in raw.items():
            name = str(key)
            if name not in {
                "at",
                "event",
                "active",
                "busy",
                "current",
                "external",
                "has_error",
                "label",
                "name",
                "owner",
                "resource",
                "state",
                "total",
                "updates",
            }:
                continue
            if isinstance(candidate, bool) or candidate is None:
                item[name] = candidate
            elif isinstance(candidate, int):
                item[name] = candidate
            elif isinstance(candidate, str):
                item[name] = redact_diagnostic_text(candidate)[:160]
        if item:
            result.append(item)
    return result


def _compact_recent_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse consecutive progress updates without losing event transitions."""
    compacted: list[dict[str, Any]] = []
    identity_fields = (
        "event",
        "owner",
        "resource",
        "label",
        "state",
        "active",
        "busy",
        "external",
        "has_error",
    )
    for event in events:
        identity = tuple(event.get(key) for key in identity_fields)
        if compacted and tuple(compacted[-1].get(key) for key in identity_fields) == identity:
            previous = compacted[-1]
            previous["updates"] = int(previous.get("updates") or 1) + int(
                event.get("updates") or 1
            )
            for key in ("at", "current", "total"):
                if key in event:
                    previous[key] = event[key]
            continue
        compacted.append(dict(event))
    return compacted


def _clean_runtime_context(context: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("application_version", "knowledge_version"):
        result[key] = redact_diagnostic_text(str(context.get(key) or ""))[:160]
    try:
        result["data_schema_version"] = int(context.get("data_schema_version") or 0)
    except (TypeError, ValueError):
        result["data_schema_version"] = 0
    result["page"] = redact_diagnostic_text(str(context.get("page") or "Unknown"))[:160]
    platform_value = context.get("platform")
    result["platform"] = {
        key: redact_diagnostic_text(str(platform_value.get(key) or ""))[:240]
        for key in ("system", "release", "version", "machine")
    } if isinstance(platform_value, Mapping) else {}
    result["recent_events"] = _compact_recent_events(
        _safe_context(context.get("recent_events"), limit=MAX_CONTEXT_EVENTS)
    )
    result["jobs"] = _safe_context(context.get("jobs"), limit=MAX_CONTEXT_JOBS)
    selected_value = context.get("selected_image_identity")
    if isinstance(selected_value, Mapping):
        file_name = str(selected_value.get("file_name") or "").strip()[:512]
        fingerprint = str(selected_value.get("path_fingerprint") or "").strip().casefold()
        selected: dict[str, str] = {}
        if file_name:
            selected["file_name"] = Path(file_name).name
        if re.fullmatch(r"[a-f0-9]{24}", fingerprint):
            selected["path_fingerprint"] = fingerprint
        if selected:
            result["selected_image_identity"] = selected
    return result


def runtime_context(
    *,
    application_version: str,
    knowledge_version: str,
    data_schema_version: int,
    page: str,
    recent_events: Any = (),
    jobs: Any = (),
    selected_image_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "application_version": str(application_version),
        "knowledge_version": str(knowledge_version),
        "data_schema_version": int(data_schema_version),
        "page": str(page or "Unknown")[:160],
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
        },
        "recent_events": _safe_context(recent_events, limit=MAX_CONTEXT_EVENTS),
        "jobs": _safe_context(jobs, limit=MAX_CONTEXT_JOBS),
    }
    if selected_image_identity:
        selected: dict[str, str] = {}
        for key in ("file_name", "path_fingerprint"):
            value = str(selected_image_identity.get(key) or "").strip()
            if value:
                selected[key] = value[:512]
        if selected:
            context["selected_image_identity"] = selected
    return _clean_runtime_context(context)


def selected_image_identity(path: str | Path) -> dict[str, str]:
    value = Path(path)
    return {
        "file_name": value.name,
        "path_fingerprint": hashlib.sha256(
            str(value).encode("utf-8", errors="surrogatepass")
        ).hexdigest()[:24],
    }


def prepare_bug_report(
    draft: BugReportDraft,
    context: Mapping[str, Any],
    attachments: tuple[BugReportAttachment, ...] = (),
    *,
    report_id: str | None = None,
    created_at: str | None = None,
) -> PreparedBugReport:
    identifier = str(report_id or f"CBR-{uuid4().hex[:16].upper()}")
    if not REPORT_ID_PATTERN.fullmatch(identifier):
        raise BugReportError("The report ID is invalid.")
    created = str(
        created_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    report_fields = {
        "summary": _clean_field(
            draft.summary,
            "Summary",
            required=True,
            maximum=MAX_REPORT_SUMMARY,
        ),
        "current_context": _clean_field(
            draft.current_context, "Current behavior or context", required=True
        ),
        "desired_result": _clean_field(
            draft.desired_result, "Desired result", required=True
        ),
        "steps_or_example": _clean_field(
            draft.steps_or_example, "Steps or example workflow", required=False
        ),
        "feedback_type": _clean_field(
            draft.feedback_type, "Feedback type", required=True
        ).casefold(),
    }
    if report_fields["feedback_type"] not in BUG_REPORT_FEEDBACK_TYPES:
        raise BugReportError("Choose a valid feedback type.")
    safe_attachments: list[BugReportAttachment] = []
    seen: set[str] = set()
    for attachment in attachments:
        name = str(attachment.name).replace("\\", "/").strip("/")
        if (
            not name
            or name in seen
            or Path(name).is_absolute()
            or ".." in Path(name).parts
        ):
            raise BugReportError(f"Attachment name is unsafe or duplicated: {attachment.name}")
        payload = bytes(attachment.payload)
        if len(payload) > MAX_ATTACHMENT_BYTES:
            raise BugReportError(f"Attachment is too large: {name}")
        safe_attachments.append(
            BugReportAttachment(
                name=name,
                media_type=str(attachment.media_type)[:120],
                payload=payload,
                description=str(attachment.description)[:400],
                editable_text=bool(attachment.editable_text),
            )
        )
        seen.add(name)
    attachment_manifest = [
        {
            "name": item.name,
            "media_type": item.media_type,
            "bytes": len(item.payload),
            "sha256": hashlib.sha256(item.payload).hexdigest(),
            "description": item.description,
            "editable_text": item.editable_text,
        }
        for item in safe_attachments
    ]
    manifest = {
        "schema_version": 1,
        "report_id": identifier,
        "created_at": created,
        "network_activity": "none",
        "included": [
            "user-written report fields",
            "application/build version",
            "current page/workflow",
            "operating-system version and architecture",
            "bounded event names and active/failed job state",
        ],
        "excluded_by_default": [
            "image pixels and thumbnails",
            "captions, SDXL Tags, and Plain Language Descriptions",
            "generator payloads and Image/System Knowledge",
            "library paths, account identity, and machine name",
            "settings, logs, and selected-image identity unless explicitly included",
        ],
        "optional_attachments": attachment_manifest,
        "selected_image_identity_included": "selected_image_identity" in context,
        "notice": (
            "This report is saved locally and is never uploaded automatically. "
            "Review every included field and attachment before sharing it."
        ),
    }
    manifest_sha256 = hashlib.sha256(_pretty_json(manifest)).hexdigest()
    manifest_content = {
        key: value
        for key, value in manifest.items()
        if key not in {"report_id", "created_at"}
    }
    manifest_content_fingerprint = hashlib.sha256(
        _canonical_json(manifest_content)
    ).hexdigest()
    safe_context = _clean_runtime_context(context)
    fingerprint_payload = {
        "report": report_fields,
        "context": safe_context,
        "attachments": attachment_manifest,
        "privacy_manifest_content_fingerprint": manifest_content_fingerprint,
    }
    fingerprint = hashlib.sha256(_canonical_json(fingerprint_payload)).hexdigest()
    report = {
        "schema_version": BUG_REPORT_SCHEMA_VERSION,
        "report_id": identifier,
        "created_at": created,
        "content_fingerprint": fingerprint,
        "privacy_manifest_sha256": manifest_sha256,
        "privacy_manifest_content_fingerprint": manifest_content_fingerprint,
        "report": report_fields,
        "context": safe_context,
        "attachments": attachment_manifest,
    }
    prepared = PreparedBugReport(
        report_id=identifier,
        created_at=created,
        content_fingerprint=fingerprint,
        report=report,
        attachments=tuple(safe_attachments),
        privacy_manifest=manifest,
    )
    report["report_md_sha256"] = hashlib.sha256(
        prepared.human_readable().encode("utf-8")
    ).hexdigest()
    return prepared


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise BugReportError(f"Bug report file is unavailable or malformed: {path.name}") from exc
    if not isinstance(payload, dict):
        raise BugReportError(f"Bug report file is malformed: {path.name}")
    return payload


def load_bug_report(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    directory = source if source.is_dir() else source.parent
    payload = _load_json(directory / "report.json" if source.is_dir() else source)
    version = payload.get("schema_version")
    if version not in SUPPORTED_BUG_REPORT_SCHEMA_VERSIONS:
        relation = "newer" if isinstance(version, int) and version > BUG_REPORT_SCHEMA_VERSION else "unsupported"
        raise BugReportError(f"This bug report uses a {relation} schema version ({version}).")
    identifier = str(payload.get("report_id") or "")
    if not REPORT_ID_PATTERN.fullmatch(identifier):
        raise BugReportError("The bug report ID is invalid.")
    expected = str(payload.get("content_fingerprint") or "")
    fingerprint_payload = {
        "report": payload.get("report"),
        "context": payload.get("context"),
        "attachments": payload.get("attachments"),
        "privacy_manifest_content_fingerprint": payload.get(
            "privacy_manifest_content_fingerprint"
        ),
    }
    actual = hashlib.sha256(_canonical_json(fingerprint_payload)).hexdigest()
    if expected != actual:
        raise BugReportError("The bug report fingerprint does not match its contents.")
    report_fields = payload.get("report")
    if not isinstance(report_fields, Mapping):
        raise BugReportError("The bug report fields are malformed.")
    if version == BUG_REPORT_SCHEMA_VERSION:
        if report_fields.get("feedback_type") not in BUG_REPORT_FEEDBACK_TYPES:
            raise BugReportError("The bug report feedback type is invalid.")
        if not all(
            isinstance(report_fields.get(key), str)
            for key in (
                "summary",
                "current_context",
                "desired_result",
                "steps_or_example",
            )
        ):
            raise BugReportError("The bug report fields are malformed.")
    manifest_path = directory / "privacy-manifest.json"
    if manifest_path.is_file():
        manifest_bytes = manifest_path.read_bytes()
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
        if manifest_digest != payload.get("privacy_manifest_sha256"):
            raise BugReportError("The bug report privacy manifest has changed.")
        manifest_payload = _load_json(manifest_path)
        manifest_content = {
            key: value
            for key, value in manifest_payload.items()
            if key not in {"report_id", "created_at"}
        }
        content_digest = hashlib.sha256(_canonical_json(manifest_content)).hexdigest()
        if content_digest != payload.get("privacy_manifest_content_fingerprint"):
            raise BugReportError("The bug report privacy manifest content does not match.")
    elif source.is_dir():
        raise BugReportError("The bug report privacy manifest is missing.")
    attachments = payload.get("attachments")
    if not isinstance(attachments, list):
        raise BugReportError("The bug report attachment manifest is malformed.")
    if source.is_dir():
        if version == BUG_REPORT_SCHEMA_VERSION:
            human_path = directory / "report.md"
            try:
                human_payload = human_path.read_bytes()
            except OSError as exc:
                raise BugReportError("The human-readable report file is missing.") from exc
            if hashlib.sha256(human_payload).hexdigest() != payload.get(
                "report_md_sha256"
            ):
                raise BugReportError("The human-readable report file has changed.")
        attachment_root = directory / "attachments"
        for item in attachments:
            if not isinstance(item, Mapping):
                raise BugReportError("The bug report attachment manifest is malformed.")
            name = str(item.get("name") or "")
            attachment_path = attachment_root / name
            try:
                attachment_path.resolve().relative_to(attachment_root.resolve())
            except (OSError, ValueError) as exc:
                raise BugReportError("The bug report contains an unsafe attachment path.") from exc
            try:
                attachment_payload = attachment_path.read_bytes()
            except OSError as exc:
                raise BugReportError(f"Bug report attachment is missing: {name}") from exc
            if hashlib.sha256(attachment_payload).hexdigest() != item.get("sha256"):
                raise BugReportError(f"Bug report attachment has changed: {name}")
    return payload


def _find_duplicate(destination: Path, fingerprint: str) -> str:
    if not destination.is_dir():
        return ""
    checked = 0
    for child in destination.iterdir():
        if checked >= 1000:
            break
        if not child.is_dir() or not REPORT_DIRECTORY_PATTERN.fullmatch(child.name):
            continue
        checked += 1
        try:
            payload = load_bug_report(child)
        except BugReportError:
            continue
        if payload.get("content_fingerprint") == fingerprint:
            return str(payload["report_id"])
    return ""


def save_bug_report(prepared: PreparedBugReport, destination: str | Path) -> SavedBugReport:
    parent = Path(destination)
    parent.mkdir(parents=True, exist_ok=True)
    final = parent / report_directory_name(
        prepared.report_id,
        str(prepared.report["report"]["summary"]),
    )
    if final.exists():
        raise BugReportError(f"A report with ID {prepared.report_id} already exists.")
    duplicate = _find_duplicate(parent, prepared.content_fingerprint)
    staging = parent / f".{prepared.report_id}.{uuid4().hex}.tmp"
    try:
        staging.mkdir()
        report_bytes = _pretty_json(prepared.report)
        manifest_bytes = _pretty_json(prepared.privacy_manifest)
        (staging / "report.json").write_bytes(report_bytes)
        (staging / "report.md").write_bytes(
            prepared.human_readable().encode("utf-8")
        )
        (staging / "privacy-manifest.json").write_bytes(manifest_bytes)
        attachment_root = staging / "attachments"
        if prepared.attachments:
            attachment_root.mkdir()
        for item in prepared.attachments:
            target = attachment_root / item.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item.payload)
        tracking = {
            "schema_version": BUG_REPORT_TRACKING_SCHEMA_VERSION,
            "report_id": prepared.report_id,
            "content_fingerprint": prepared.content_fingerprint,
            "imported_at": "",
            "disposition": "unreviewed",
            "bug_id": "",
            "roadmap_id": "",
            "duplicate_of_report_id": duplicate,
        }
        (staging / "tracking.json").write_text(
            json.dumps(tracking, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        loaded = load_bug_report(staging)
        if loaded["content_fingerprint"] != prepared.content_fingerprint:
            raise OSError("Bug report read-back verification failed.")
        for item in prepared.attachments:
            payload = (attachment_root / item.name).read_bytes()
            expected = hashlib.sha256(item.payload).hexdigest()
            if hashlib.sha256(payload).hexdigest() != expected:
                raise OSError(f"Attachment read-back verification failed: {item.name}")
        os.replace(staging, final)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise
    return SavedBugReport(final, duplicate)


def update_report_tracking(
    report_directory: str | Path,
    *,
    disposition: str,
    bug_id: str = "",
    roadmap_id: str = "",
    imported_at: str | None = None,
) -> dict[str, Any]:
    directory = Path(report_directory)
    report = load_bug_report(directory)
    path = directory / "tracking.json"
    current = _load_json(path)
    if current.get("schema_version") != BUG_REPORT_TRACKING_SCHEMA_VERSION:
        raise BugReportError("The report tracking file uses an unsupported schema.")
    if current.get("report_id") != report.get("report_id"):
        raise BugReportError("The report tracking file belongs to a different report.")
    if current.get("content_fingerprint") != report.get("content_fingerprint"):
        raise BugReportError("The report tracking fingerprint does not match the report.")
    updated = dict(current)
    updated.update(
        {
            "imported_at": str(
                imported_at
                if imported_at is not None
                else datetime.now(timezone.utc).isoformat(timespec="seconds")
            ),
            "disposition": _clean_field(disposition, "Disposition", required=True)[:120],
            "bug_id": str(bug_id or "")[:80],
            "roadmap_id": str(roadmap_id or "")[:80],
        }
    )
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    verified = _load_json(temporary)
    if verified != updated:
        temporary.unlink(missing_ok=True)
        raise OSError("Report tracking read-back verification failed.")
    os.replace(temporary, path)
    return updated


def record_report_export(
    directory: str | Path,
    *,
    package_id: str,
    package_sha256: str,
    package_kind: str,
    exported_at: str | None = None,
) -> dict[str, Any]:
    """Record local packaging separately from immutable report evidence."""
    report_directory = Path(directory)
    load_bug_report(report_directory)
    path = report_directory / "tracking.json"
    current = _load_json(path)
    history = current.get("export_history")
    if not isinstance(history, list):
        history = []
    if any(
        isinstance(item, Mapping) and item.get("package_id") == package_id
        for item in history
    ):
        return current
    entry = {
        "package_id": str(package_id)[:80],
        "package_sha256": str(package_sha256)[:64],
        "package_kind": str(package_kind)[:40],
        "exported_at": str(
            exported_at
            or datetime.now(timezone.utc).isoformat(timespec="seconds")
        ),
    }
    updated = dict(current)
    updated["export_history"] = [*history[-99:], entry]
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if _load_json(temporary) != updated:
        temporary.unlink(missing_ok=True)
        raise OSError("Report export tracking read-back verification failed.")
    os.replace(temporary, path)
    return updated


def export_bug_report_zip(
    report_directory: str | Path,
    output: str | Path,
) -> Path:
    directory = Path(report_directory)
    report = load_bug_report(directory)
    allowed = ("report.json", "report.md", "privacy-manifest.json")
    files: list[tuple[str, bytes]] = []
    for name in allowed:
        path = directory / name
        if path.is_file():
            files.append((name, path.read_bytes()))
    for item in report.get("attachments", []):
        if not isinstance(item, dict):
            raise BugReportError("The bug report attachment manifest is malformed.")
        name = str(item.get("name") or "")
        path = directory / "attachments" / name
        try:
            path.resolve().relative_to((directory / "attachments").resolve())
        except (OSError, ValueError) as exc:
            raise BugReportError("The bug report contains an unsafe attachment path.") from exc
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != item.get("sha256"):
            raise BugReportError(f"Attachment verification failed: {name}")
        files.append((f"attachments/{name}", payload))
    destination = Path(output)
    write_verified_zip(destination, files)
    if destination.stat().st_size > MAX_SHAREABLE_REPORT_ZIP_BYTES:
        destination.unlink(missing_ok=True)
        raise BugReportError(
            "The shareable report package would exceed CollectiGen's 20 MB "
            "upload-compatibility limit. Remove one or more optional attachments "
            "and review the report again."
        )
    return destination


def discover_feedback_reports(
    feedback_directory: str | Path,
) -> tuple[tuple[FeedbackReportSummary, ...], tuple[str, ...]]:
    """List report metadata without reading attachment payloads."""
    root = Path(feedback_directory)
    if not root.exists():
        return (), ()
    if not root.is_dir():
        return (), (f"{root.name}: not a folder",)
    reports: list[FeedbackReportSummary] = []
    invalid: list[str] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        if not child.is_dir() or not REPORT_DIRECTORY_PATTERN.fullmatch(child.name):
            continue
        try:
            payload = load_bug_report(child / "report.json")
            fields = payload.get("report")
            attachments = payload.get("attachments")
            if not isinstance(fields, Mapping) or not isinstance(attachments, list):
                raise BugReportError("The report fields are malformed.")
            feedback_type = str(fields.get("feedback_type") or "bug")
            if feedback_type not in BUG_REPORT_FEEDBACK_TYPES:
                feedback_type = "bug"
            export_count = 0
            last_exported_at = ""
            try:
                tracking = _load_json(child / "tracking.json")
                history = tracking.get("export_history")
                if isinstance(history, list):
                    valid_history = [
                        item for item in history if isinstance(item, Mapping)
                    ]
                    export_count = len(valid_history)
                    if valid_history:
                        last_exported_at = str(
                            valid_history[-1].get("exported_at") or ""
                        )
            except BugReportError:
                pass
            reports.append(
                FeedbackReportSummary(
                    directory=child,
                    report_id=str(payload["report_id"]),
                    created_at=str(payload.get("created_at") or ""),
                    feedback_type=feedback_type,
                    summary=str(fields.get("summary") or "")[:240],
                    attachment_count=len(attachments),
                    export_count=export_count,
                    last_exported_at=last_exported_at,
                )
            )
        except (BugReportError, OSError) as exc:
            invalid.append(f"{child.name}: {exc}")
    reports.sort(key=lambda item: (item.created_at, item.report_id))
    return tuple(reports), tuple(invalid)


def export_feedback_archive(
    report_directories: tuple[str | Path, ...] | list[str | Path],
    output: str | Path,
    *,
    created_at: str | None = None,
) -> FeedbackArchiveExport:
    """Create one verified uploadable archive without changing report originals."""
    if not report_directories:
        raise BugReportError("Select at least one feedback report.")
    files: list[tuple[str, bytes]] = []
    report_manifest: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for value in report_directories:
        directory = Path(value)
        payload = load_bug_report(directory)
        report_id = str(payload["report_id"])
        if report_id in seen_ids:
            raise BugReportError(f"Feedback report is selected more than once: {report_id}")
        seen_ids.add(report_id)
        fields = payload.get("report")
        feedback_type = (
            str(fields.get("feedback_type") or "bug")
            if isinstance(fields, Mapping)
            else "bug"
        )
        included_files: list[dict[str, Any]] = []
        names = ("report.json", "report.md", "privacy-manifest.json")
        for name in names:
            path = directory / name
            if not path.is_file():
                raise BugReportError(f"Feedback report file is missing: {report_id}/{name}")
            content = path.read_bytes()
            archive_name = f"{report_id}/{name}"
            files.append((archive_name, content))
            included_files.append(
                {
                    "name": archive_name,
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        for attachment in payload.get("attachments", []):
            if not isinstance(attachment, Mapping):
                raise BugReportError("The bug report attachment manifest is malformed.")
            name = str(attachment.get("name") or "")
            path = directory / "attachments" / name
            content = path.read_bytes()
            archive_name = f"{report_id}/attachments/{name}"
            files.append((archive_name, content))
            included_files.append(
                {
                    "name": archive_name,
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        report_manifest.append(
            {
                "report_id": report_id,
                "created_at": str(payload.get("created_at") or ""),
                "schema_version": payload.get("schema_version"),
                "feedback_type": (
                    feedback_type
                    if feedback_type in BUG_REPORT_FEEDBACK_TYPES
                    else "bug"
                ),
                "content_fingerprint": str(
                    payload.get("content_fingerprint") or ""
                ),
                "files": included_files,
            }
        )
    archive_id = f"CFA-{uuid4().hex[:16].upper()}"
    archive_manifest = {
        "schema_version": 1,
        "archive_id": archive_id,
        "created_at": str(
            created_at
            or datetime.now(timezone.utc).isoformat(timespec="seconds")
        ),
        "report_count": len(report_manifest),
        "reports": report_manifest,
        "excluded": [
            "maintainer tracking.json files",
            "existing individual or bulk ZIP exports",
            "anything outside the selected immutable report evidence",
        ],
        "notice": (
            "This archive was created locally and was not uploaded automatically. "
            "Its report folders contain only content retained in each privacy review."
        ),
    }
    files.insert(0, ("feedback-archive-manifest.json", _pretty_json(archive_manifest)))
    destination = Path(output)
    write_verified_zip(destination, files)
    archive_sha256 = hashlib.sha256(destination.read_bytes()).hexdigest()
    return FeedbackArchiveExport(destination, archive_id, archive_sha256)
