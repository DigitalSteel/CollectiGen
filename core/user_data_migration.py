from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .paths import APP_ROOT


LEGACY_FILES: dict[str, tuple[str, str | None]] = {
    "tags.json": ("tags", "id"),
    "tasks.json": ("tasks", "id"),
    "characters.json": ("characters", "id"),
    "series.json": ("series", "id"),
    "discovered_tags.json": ("discovered_tags", None),
}


def _load(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default.copy()
    try:
        text=path.read_text(encoding="utf-8")
        payload=json.loads(text)
    except (OSError,TypeError):
        return default.copy()
    except json.JSONDecodeError:
        if path.name.casefold()!="tasks.json":
            return default.copy()
        tasks={}
        try:
            lines=text.splitlines()
            header=json.loads(lines[0]) if lines else {}
            if header.get("format")!="review_task_events":
                return default.copy()
            for index,line in enumerate(lines[1:],start=1):
                try:
                    event=json.loads(line)
                except json.JSONDecodeError:
                    # An interrupted append can only damage the final event.
                    if index==len(lines)-1:
                        break
                    raise
                if event.get("event")=="task" and isinstance(event.get("task"),dict):
                    task=dict(event["task"]); task_id=str(task.get("id") or "")
                    if task_id:tasks[task_id]=task
                elif event.get("event")=="outcome":
                    task=tasks.get(str(event.get("task_id") or ""))
                    if task is None:continue
                    if event.get("status"):task["status"]=str(event["status"])
                    if event.get("updated_at"):task["updated_at"]=str(event["updated_at"])
                    if event.get("has_resolution") is True:
                        details=task.get("details")
                        details=dict(details) if isinstance(details,dict) else {}
                        resolution=event.get("resolution")
                        details["resolution"]=(dict(resolution) if isinstance(resolution,dict) else {})
                        task["details"]=details
        except (json.JSONDecodeError,TypeError,AttributeError):
            return default.copy()
        payload={"schema_version":int(header.get("schema_version") or 2),"tasks":list(tasks.values())}
    return payload if isinstance(payload, dict) else default.copy()


def _record_key(record: dict[str, Any], id_field: str | None) -> str:
    if id_field:
        value = record.get(id_field)
        if value:
            return str(value).strip().casefold()
    for field in ("display", "name", "normalized", "tag"):
        value = record.get(field)
        if value:
            return " ".join(str(value).replace("_", " ").split()).casefold()
    return ""


def _merge_payloads(
    user_payload: dict[str, Any],
    legacy_payload: dict[str, Any],
    list_key: str,
    id_field: str | None,
) -> tuple[dict[str, Any], int, int]:
    result = dict(user_payload)
    user_items = [item for item in user_payload.get(list_key, []) if isinstance(item, dict)]
    legacy_items = [item for item in legacy_payload.get(list_key, []) if isinstance(item, dict)]
    by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for item in user_items:
        key = _record_key(item, id_field)
        if key and key not in by_key:
            order.append(key)
            by_key[key] = dict(item)

    added = updated = 0
    for item in legacy_items:
        key = _record_key(item, id_field)
        if not key:
            continue
        if key not in by_key:
            order.append(key)
            by_key[key] = dict(item)
            added += 1
        elif by_key[key] != item:
            # Root-level files were used by v0.5.0, so their current values win
            # while fields that only existed in the preserved user copy survive.
            # Migration-added empty placeholders are not authoritative values
            # and must not erase a meaningful local note/property/identity.
            for field_name, value in item.items():
                if value in (None, "", [], {}) and by_key[key].get(field_name) not in (
                    None,
                    "",
                    [],
                    {},
                ):
                    continue
                by_key[key][field_name] = value
            updated += 1

    result[list_key] = [by_key[key] for key in order]
    result["schema_version"] = max(
        int(user_payload.get("schema_version", 1) or 1),
        int(legacy_payload.get("schema_version", 1) or 1),
    )

    # Tag databases also contain merge rules alongside tag records.
    if list_key == "tags":
        rules, rule_order = {}, []
        for source in (user_payload, legacy_payload):
            for rule in source.get("merge_rules", []):
                if not isinstance(rule, dict):
                    continue
                key = _record_key(rule, "id")
                if key and key not in rules:
                    rule_order.append(key)
                if key:
                    rules[key] = {**rules.get(key, {}), **rule}
        result["merge_rules"] = [rules[key] for key in rule_order]
    return result, added, updated


def migrate_user_data(app_root: Path = APP_ROOT) -> dict[str, Any]:
    """Compatibility facade over the automatic application migration ledger."""
    app_root = Path(app_root)
    state_path = app_root / "data" / "user" / "application-migrations.json"
    if state_path.exists():
        state = _load(state_path, {})
        if "consolidate-user-data-v050" in state.get("completed", []):
            return {"already_consolidated": True, "marker": str(state_path)}

    from version import VERSION
    from .application_migrations import run_application_migrations

    result = run_application_migrations(app_root, VERSION)
    details = result.details.get("consolidate-user-data-v050", {})
    return {
        **details,
        "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "backup": str(result.backup_directory or ""),
        "recovery_manifest": str(result.recovery_manifest or ""),
    }
