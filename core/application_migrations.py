from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from release_contract import SemVer

from .automation_repository import AutomationRuleRepository
from .automation_rule_conversion import RulePairAdmission
from .user_data_migration import LEGACY_FILES, _load as _load_user_payload, _merge_payloads


MIGRATION_STATE_SCHEMA = 1
MIGRATION_LEVEL = 3


class ApplicationMigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MigrationStep:
    id: str
    source: str
    target: str
    applies: Callable[[Path], bool]
    run: Callable[[Path], dict[str, Any]]


@dataclass
class ApplicationMigrationReport:
    application_version: str
    prior_application_version: str = ""
    applied: list[str] = field(default_factory=list)
    details: dict[str, dict[str, Any]] = field(default_factory=dict)
    backup_directory: Path | None = None
    recovery_manifest: Path | None = None


def _load(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        return dict(default)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        if path.name.casefold()=="tasks.json":
            invalid={"__invalid_review_queue__":True}
            payload=_load_user_payload(path,invalid)
            if "__invalid_review_queue__" not in payload:
                return payload
        raise ApplicationMigrationError(f"Cannot read migration input {path}: {exc}") from exc
    except (OSError, UnicodeDecodeError, TypeError) as exc:
        raise ApplicationMigrationError(f"Cannot read migration input {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ApplicationMigrationError(f"Migration input {path} is not a JSON object.")
    return payload


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with temporary.open("rb") as stream:
        stream.read(1)
    temporary.replace(path)
    _load(path, {})


def _normalize(value: object) -> str:
    return " ".join(str(value).strip().lower().replace("_", " ").split())


def _v040_applies(root: Path) -> bool:
    tags = root / "data" / "tags.json"
    if not tags.is_file():
        return False
    payload = _load(tags, {"tags": []})
    return any(
        isinstance(item, dict)
        and ("status" in item or item.get("category") in {"neckwear", "subject_count"})
        for item in payload.get("tags", [])
    )


def _migrate_v040(root: Path) -> dict[str, Any]:
    data = root / "data"
    tags_path = data / "tags.json"
    discovered_path = data / "discovered_tags.json"
    tags_payload = _load(tags_path, {"tags": [], "merge_rules": []})
    discovered = _load(discovered_path, {"discovered_tags": []})
    by_name = {
        str(item.get("display", "")).casefold(): dict(item)
        for item in discovered.get("discovered_tags", [])
        if isinstance(item, dict) and str(item.get("display", "")).strip()
    }
    report = {
        "known_preserved": 0,
        "moved_to_review": 0,
        "category_refs_updated": 0,
        "duplicate_categories_removed": 0,
    }
    remap = {"neckwear": "clothing_accessories_neckwear", "subject_count": "image_subject_count"}
    known: list[dict[str, Any]] = []
    for source in tags_payload.get("tags", []):
        if not isinstance(source, dict):
            continue
        item = dict(source)
        status = item.pop("status", None)
        category = str(item.get("category") or "uncategorized")
        if category in remap:
            item["category"] = remap[category]
            report["category_refs_updated"] += 1
        if status == "pending" and item.get("category", "uncategorized") == "uncategorized":
            name = str(item.get("display") or item.get("id") or "").strip()
            if name:
                prior = by_name.get(name.casefold())
                if prior:
                    prior["seen_count"] = max(int(prior.get("seen_count", 0)), int(item.get("seen_count", 0)))
                else:
                    by_name[name.casefold()] = {
                        "display": name,
                        "seen_count": int(item.get("seen_count", 0)),
                        "first_seen": item.get("first_seen"),
                        "last_seen": item.get("last_seen"),
                        "example_images": [],
                    }
                report["moved_to_review"] += 1
            continue
        item.setdefault("concept_id", None)
        item.setdefault("properties", {})
        item.setdefault("notes", "")
        item.setdefault("provenance", [])
        if not any(
            isinstance(value, dict) and value.get("source") == "v0.4_migration"
            for value in item["provenance"]
        ):
            item["provenance"].append({"source": "v0.4_migration"})
        known.append(item)
        report["known_preserved"] += 1
    _atomic_json(tags_path, {
        "schema_version": max(2, int(tags_payload.get("schema_version", 1) or 1)),
        "tags": known,
        "merge_rules": tags_payload.get("merge_rules", []),
    })
    _atomic_json(discovered_path, {
        "schema_version": 1,
        "discovered_tags": sorted(by_name.values(), key=lambda value: str(value.get("display", "")).casefold()),
    })
    categories = data / "user" / "categories.json"
    if categories.is_file():
        payload = _load(categories, {"categories": []})
        kept = []
        for category in payload.get("categories", []):
            if isinstance(category, dict) and category.get("id") in remap:
                report["duplicate_categories_removed"] += 1
            else:
                kept.append(category)
        payload["categories"] = kept
        _atomic_json(categories, payload)
    return report


def _v050_applies(root: Path) -> bool:
    data = root / "data"
    return any((data / name).is_file() for name in (*LEGACY_FILES, "settings.json"))


def _merge_foundation_tags(root: Path, payload: dict[str, Any]) -> dict[str, int]:
    foundation = _load(root / "data" / "defaults" / "foundation_tags.json", {"tags": []})
    tags = [dict(value) for value in payload.get("tags", []) if isinstance(value, dict)]
    by_name = {_normalize(value.get("display", "")): value for value in tags}
    added = merged = 0
    for source in foundation.get("tags", []):
        if not isinstance(source, dict):
            continue
        key = _normalize(source.get("display", ""))
        if not key:
            continue
        if key in by_name:
            current = by_name[key]
            for field_name in ("concept_id", "category"):
                if not current.get(field_name) or current.get(field_name) == "uncategorized":
                    current[field_name] = source.get(field_name, current.get(field_name))
            properties = current.setdefault("properties", {})
            if isinstance(properties, dict):
                properties.update({
                    key_name: value for key_name, value in source.get("properties", {}).items()
                    if key_name not in properties
                })
            merged += 1
        else:
            item = dict(source)
            tags.append(item)
            by_name[key] = item
            added += 1
    payload["schema_version"] = max(3, int(payload.get("schema_version", 1) or 1))
    payload["tags"] = tags
    return {"foundation_added": added, "foundation_merged": merged}


def _migrate_v050(root: Path) -> dict[str, Any]:
    data = root / "data"
    user = data / "user"
    user.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"files": {}}
    for filename, (list_key, id_field) in LEGACY_FILES.items():
        legacy_path = data / filename
        user_path = user / filename
        user_payload = _load(user_path, {"schema_version": 1, list_key: []})
        legacy_payload = _load(legacy_path, {"schema_version": 1, list_key: []})
        merged, added, updated = _merge_payloads(user_payload, legacy_payload, list_key, id_field)
        if filename == "tags.json":
            report.update(_merge_foundation_tags(root, merged))
        _atomic_json(user_path, merged)
        report["files"][filename] = {
            "added": added,
            "updated": updated,
            "total": len(merged.get(list_key, [])),
        }
    legacy_settings = _load(data / "settings.json", {})
    user_settings = _load(user / "settings.json", {})
    settings = {**user_settings, **legacy_settings}
    if not settings.get("ingest_directory") and settings.get("inbox_directory"):
        settings["ingest_directory"] = settings["inbox_directory"]
    if not settings.get("working_directory") and settings.get("dataset_directory"):
        settings["working_directory"] = settings["dataset_directory"]
    _atomic_json(user / "settings.json", settings)
    report["files"]["settings.json"] = {"keys": len(settings)}
    return report


def _automation_applies(root: Path) -> bool:
    return (root / "saved_rules.json").is_file() and not (root / "data" / "user" / ".saved_rules_imported_v1").exists()


def _migrate_automation(root: Path) -> dict[str, Any]:
    defaults = root / "data" / "defaults" / "automation_rules.json"
    user = root / "data" / "user" / "automation_rules.json"
    legacy = root / "saved_rules.json"
    repository = AutomationRuleRepository(
        defaults, user, rule_pair_admission=RulePairAdmission(root / "data" / "user"),
    )
    report = repository.upgrade_legacy(legacy)
    payload = _load(user, {})
    if not isinstance(payload.get("variables"), dict):
        raise ApplicationMigrationError("Legacy automation read-back verification failed.")
    marker = root / "data" / "user" / ".saved_rules_imported_v1"
    marker.write_text(datetime.now(timezone.utc).isoformat(timespec="seconds") + "\n", encoding="utf-8")
    return report


MIGRATIONS = (
    MigrationStep("legacy-knowledge-v040", "before 0.4.0", "0.4.0", _v040_applies, _migrate_v040),
    MigrationStep("consolidate-user-data-v050", "0.4.x", "0.5.0", _v050_applies, _migrate_v050),
    MigrationStep("legacy-automation-v1", "0.5.0-0.9.x", "0.10.0", _automation_applies, _migrate_automation),
)


def _transaction_paths(root: Path) -> list[Path]:
    data = root / "data"
    user = data / "user"
    paths = [data / "settings.json", user / "settings.json", root / "saved_rules.json"]
    for name in LEGACY_FILES:
        paths.extend((data / name, user / name))
    paths.extend((
        user / "categories.json",
        user / "automation_rules.json",
        user / ".saved_rules_imported_v1",
        user / "application-migrations.json",
    ))
    return paths


def _create_snapshot(root: Path, paths: list[Path]) -> tuple[Path, Path, list[dict[str, Any]]]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = root / "backups" / "application-migrations" / f"{stamp}_{uuid4().hex[:8]}"
    backup.mkdir(parents=True, exist_ok=False)
    entries: list[dict[str, Any]] = []
    for source in paths:
        try:
            relative = source.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise ApplicationMigrationError(f"Migration path escapes the application root: {source}") from exc
        entry: dict[str, Any] = {"path": relative.as_posix(), "existed": source.is_file()}
        if source.is_file():
            saved = backup / "files" / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, saved)
            entry["backup"] = saved.relative_to(backup).as_posix()
        entries.append(entry)
    manifest = backup / "recovery.json"
    _atomic_json(manifest, {
        "schema_version": 1,
        "status": "prepared",
        "root": str(root.resolve()),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": entries,
    })
    return backup, manifest, entries


def _restore_snapshot(root: Path, backup: Path, entries: list[dict[str, Any]]) -> None:
    for entry in entries:
        target = root.joinpath(*PurePath(entry["path"]).parts)
        if entry.get("existed"):
            source = backup.joinpath(*PurePath(entry["backup"]).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        elif target.exists():
            target.unlink()


class PurePath:
    """Convert manifest POSIX paths without accepting parent traversal."""

    def __init__(self, value: object) -> None:
        text = str(value)
        parts = text.split("/")
        if not text or any(part in {"", ".", ".."} for part in parts):
            raise ApplicationMigrationError("Migration recovery manifest contains an unsafe path.")
        self.parts = tuple(parts)


def _write_manifest(path: Path, payload: dict[str, Any], status: str, **extra: Any) -> None:
    payload = dict(payload)
    payload.update(extra)
    payload["status"] = status
    _atomic_json(path, payload)


def _recover_interrupted_migrations(root: Path) -> list[Path]:
    recovered: list[Path] = []
    backup_root = root / "backups" / "application-migrations"
    if not backup_root.is_dir():
        return recovered
    for manifest in sorted(backup_root.glob("*/recovery.json")):
        payload = _load(manifest, {})
        if payload.get("status") != "prepared":
            continue
        if Path(str(payload.get("root") or "")).resolve() != root.resolve():
            raise ApplicationMigrationError(f"Interrupted migration recovery root does not match: {manifest}")
        entries = payload.get("files")
        if not isinstance(entries, list):
            raise ApplicationMigrationError(f"Interrupted migration recovery manifest is invalid: {manifest}")
        _restore_snapshot(root, manifest.parent, entries)
        _write_manifest(manifest, payload, "recovered_after_interruption")
        recovered.append(manifest)
    return recovered


def run_application_migrations(root: str | Path, application_version: str) -> ApplicationMigrationReport:
    root = Path(root).resolve()
    SemVer.parse(application_version)
    interrupted = _recover_interrupted_migrations(root)
    state_path = root / "data" / "user" / "application-migrations.json"
    state = _load(state_path, {"schema_version": MIGRATION_STATE_SCHEMA, "completed": []})
    completed = [str(value) for value in state.get("completed", [])]
    # Profiles consolidated by the pre-ledger service are already past both
    # root-level knowledge steps. Recording that fact avoids re-merging stale
    # preserved source files into newer user-owned stores.
    if not state_path.exists() and (root / "data" / "user" / ".consolidated_v1").exists():
        completed.extend(("legacy-knowledge-v040", "consolidate-user-data-v050"))
    if not state_path.exists():
        automation = root / "data" / "user" / "automation_rules.json"
        try:
            automation_payload = _load(automation, {})
            if any(
                isinstance(item, dict) and item.get("source") == "legacy_upgrade"
                for kind in ("merge_rules", "wildcard_rules", "combine_rules", "ignore_rules")
                for item in automation_payload.get(kind, [])
            ):
                completed.append("legacy-automation-v1")
        except ApplicationMigrationError:
            pass
    completed = list(dict.fromkeys(completed))
    prior = str(state.get("application_version") or "")
    if prior and SemVer.parse(application_version).compare(SemVer.parse(prior)) < 0:
        raise ApplicationMigrationError(
            f"This profile was last opened by CollectiGen v{prior}; v{application_version} refuses an unsafe downgrade. "
            "Use the verified update rollback path, which restores the matching pre-update data snapshot."
        )
    report = ApplicationMigrationReport(application_version, prior_application_version=prior)
    if interrupted:
        report.details["interrupted_recovery"] = {"manifests": [str(value) for value in interrupted]}
        report.recovery_manifest = interrupted[-1]
        report.backup_directory = interrupted[-1].parent
    applicable = [step for step in MIGRATIONS if step.id not in completed and step.applies(root)]
    state_written = False
    if applicable:
        backup, manifest, entries = _create_snapshot(root, _transaction_paths(root))
        report.backup_directory = backup
        report.recovery_manifest = manifest
        manifest_payload = _load(manifest, {})
        try:
            for step in applicable:
                details = step.run(root)
                report.applied.append(step.id)
                report.details[step.id] = details
            for path in _transaction_paths(root):
                if path.suffix == ".json" and path.is_file():
                    _load(path, {})
            completed = list(dict.fromkeys([*completed, *report.applied]))
            _atomic_json(state_path, {
                "schema_version": MIGRATION_STATE_SCHEMA,
                "migration_level": MIGRATION_LEVEL,
                "application_version": application_version,
                "completed": completed,
                "last_migrated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "last_recovery_manifest": str(manifest),
            })
            _write_manifest(
                manifest, manifest_payload, "verified",
                applied=report.applied,
                application_version=application_version,
            )
            state_written = True
        except Exception as exc:
            _restore_snapshot(root, backup, entries)
            _write_manifest(manifest, manifest_payload, "rolled_back", error=str(exc), applied=report.applied)
            raise ApplicationMigrationError(f"Application migration failed and was rolled back: {exc}") from exc
    completed = list(dict.fromkeys([*completed, *report.applied]))
    state_changed = (
        state.get("schema_version") != MIGRATION_STATE_SCHEMA
        or state.get("migration_level") != MIGRATION_LEVEL
        or prior != application_version
        or [str(value) for value in state.get("completed", [])] != completed
        or bool(report.applied)
        or bool(interrupted)
    )
    if state_changed and not state_written:
        new_state = {
            "schema_version": MIGRATION_STATE_SCHEMA,
            "migration_level": MIGRATION_LEVEL,
            "application_version": application_version,
            "completed": completed,
            "last_migrated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "last_recovery_manifest": str(report.recovery_manifest or state.get("last_recovery_manifest") or ""),
        }
        _atomic_json(state_path, new_state)
    return report
