from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .paths import SETTINGS_PATH, USER_DATA_DIR
from .library_profiles import LibraryProfile,LibraryProfileRegistry
from .settings import AppSettings


ONBOARDING_VERSION = 1
ONBOARDING_STEPS = ("welcome", "locations", "behavior", "components", "review")
ONBOARDING_STATUSES = {"not_started", "in_progress", "skipped", "completed", "legacy"}
LOW_MODEL_SPACE_WARNING = 4 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class LocationAssessment:
    key: str
    path: str
    valid: bool
    writable: bool
    available: bool
    network: bool
    free_bytes: int | None
    detail: str


@dataclass(frozen=True)
class LocationValidation:
    locations: tuple[LocationAssessment, ...]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.errors


def is_genuinely_new_profile(
    user_data_dir: str | Path = USER_DATA_DIR,
    settings_path: str | Path = SETTINGS_PATH,
) -> bool:
    """Detect a clean profile before normal startup creates shipped defaults.

    Any existing profile file is treated as upgrade evidence.  This deliberately
    fails closed: an old or partially damaged profile must never be mistaken for
    a new user merely because settings.json is missing.
    """
    settings_path = Path(settings_path)
    if settings_path.exists():
        return False
    root = Path(user_data_dir)
    if not root.exists():
        return True
    try:
        return not any(candidate.is_file() for candidate in root.rglob("*"))
    except OSError:
        return False


def should_offer_onboarding(settings: AppSettings, *, genuinely_new: bool) -> bool:
    status = str(getattr(settings, "onboarding_status", "legacy"))
    return status == "in_progress" or (status == "not_started" and genuinely_new)


def _normalized_path(value: str | Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(os.fspath(Path(value).expanduser()))))


def _nearest_existing(path: Path) -> Path | None:
    candidate = path
    while True:
        try:
            if candidate.exists():
                return candidate
        except OSError:
            return None
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_locations(
    values: dict[str, str],
    *,
    library_profiles: Iterable[LibraryProfile] | None = None,
) -> LocationValidation:
    machine_labels = {
        "ingest_directory": "Incoming images",
        "problems_directory": "Problems",
        "ai_models_directory": "AI model files",
    }
    if "recycle_directory" in values:
        machine_labels["recycle_directory"] = "Recycle Bin"
    if "external_write_backup_directory" in values:
        machine_labels["external_write_backup_directory"] = "Protected image backups"
    # Retain the four-field contract for older callers while Guided Setup uses
    # the named-library registry as the image-library authority.
    labels = dict(machine_labels)
    if library_profiles is None:
        labels = {
            "ingest_directory": "Incoming images",
            "working_directory": "Default image library",
            "problems_directory": "Problems",
            "ai_models_directory": "AI model files",
        }
        if "recycle_directory" in values:
            labels["recycle_directory"] = "Recycle Bin"
        if "external_write_backup_directory" in values:
            labels["external_write_backup_directory"] = "Protected image backups"
    assessments: list[LocationAssessment] = []
    errors: list[str] = []
    warnings: list[str] = []
    normalized: dict[str, Path] = {}

    for key, label in labels.items():
        raw = str(values.get(key, "") or "").strip()
        if not raw:
            assessments.append(LocationAssessment(
                key, "", True, False, False, False, None,
                f"{label} is not configured yet.",
            ))
            continue
        try:
            path = _normalized_path(raw)
        except (OSError, ValueError, TypeError) as exc:
            errors.append(f"{label} is not a valid folder path: {exc}")
            assessments.append(LocationAssessment(key, raw, False, False, False, False, None, errors[-1]))
            continue
        normalized[key] = path
        existing = _nearest_existing(path)
        available = existing is not None
        writable = bool(existing and existing.is_dir() and os.access(existing, os.R_OK | os.W_OK))
        network = str(path).startswith("\\\\")
        free_bytes = None
        if existing is not None:
            try:
                free_bytes = int(shutil.disk_usage(existing).free)
            except OSError:
                pass
        if key == "recycle_directory" and network:
            detail = "Recycle Bin must use machine-local storage, not a network path."
            errors.append(detail)
        elif not available:
            detail = f"{label} is unavailable and has no reachable parent folder."
            errors.append(detail)
        elif not existing.is_dir():
            detail = f"{label} resolves beneath a file instead of a folder."
            errors.append(detail)
        elif not writable:
            detail = f"{label} is read-only or cannot be written by this account."
            errors.append(detail)
        else:
            location_kind = "Network/shared location" if network else "Local or removable location"
            try: configured_exists=path.is_dir()
            except OSError: configured_exists=False
            creation = "Folder is available." if configured_exists else "The folder will need to be created before it is used."
            detail = f"{location_kind}. {creation}"
            if network:
                warnings.append(
                    f"{label} is on a network path. CollectiGen keeps its rebuildable index and job state on this computer."
                )
            if key == "ai_models_directory" and free_bytes is not None and free_bytes < LOW_MODEL_SPACE_WARNING:
                warnings.append(
                    f"{label} has less than 4 GiB free. Optional model setup will verify its exact required space before changing an installed model."
                )
        assessments.append(LocationAssessment(
            key, str(path), not any(message == detail for message in errors),
            writable, available, network, free_bytes, detail,
        ))

    profiles=tuple(library_profiles or ())
    if library_profiles is not None:
        registry=LibraryProfileRegistry(profiles)
        for profile in profiles:
            label=f'Image library "{profile.name or "Unnamed"}"'
            key=f"library:{profile.stable_id}"
            labels[key]=label
            structural=registry.validate(
                profile,replacing_id=profile.stable_id,
            )
            errors.extend(
                f"{label}: {issue.message}" for issue in structural
            )
            raw=str(profile.root or "").strip()
            if not raw:
                assessments.append(LocationAssessment(
                    key,"",False,False,False,False,None,
                    f"{label} has no folder.",
                ))
                continue
            try:
                path=_normalized_path(raw)
            except (OSError,ValueError,TypeError) as exc:
                message=f"{label} is not a valid folder path: {exc}"
                errors.append(message)
                assessments.append(LocationAssessment(
                    key,raw,False,False,False,False,None,message,
                ))
                continue
            normalized[key]=path
            existing=_nearest_existing(path)
            available=existing is not None
            writable=bool(
                existing and existing.is_dir()
                and os.access(existing,os.R_OK|os.W_OK)
            )
            network=str(path).startswith("\\\\")
            free_bytes=None
            if existing is not None:
                try:
                    free_bytes=int(shutil.disk_usage(existing).free)
                except OSError:
                    pass
            exact_exists=False
            try:
                exact_exists=path.is_dir()
            except OSError:
                pass
            if existing is not None and not existing.is_dir():
                detail=f"{label} resolves beneath a file instead of a folder."
                errors.append(detail)
                valid=False
            elif not available:
                detail=(
                    f"{profile.display_role}. The folder is currently unavailable; "
                    "CollectiGen will check it again before use."
                )
                warnings.append(f"{label} is currently unavailable.")
                valid=not structural
            elif not writable:
                detail=(
                    f"{profile.display_role}. The folder is currently read-only; "
                    "browsing may remain available, but writes will require a fresh check."
                )
                warnings.append(f"{label} is currently read-only.")
                valid=not structural
            else:
                availability=(
                    "Folder is available."
                    if exact_exists else
                    "The folder will need to be created before it is used."
                )
                detail=f"{profile.display_role}. {availability}"
                valid=not structural
            assessments.append(LocationAssessment(
                key,str(path),valid,writable,available,network,free_bytes,detail,
            ))

    items = list(normalized.items())
    for index, (left_key, left) in enumerate(items):
        for right_key, right in items[index + 1:]:
            if left_key.startswith("library:") and right_key.startswith("library:"):
                # The registry reports these with the exact profile names.
                continue
            left_case = Path(str(left).casefold())
            right_case = Path(str(right).casefold())
            if _is_relative_to(left_case, right_case) or _is_relative_to(right_case, left_case):
                errors.append(
                    f"{labels[left_key]} and {labels[right_key]} must not be the same folder or nested inside one another."
                )

    return LocationValidation(tuple(assessments), tuple(dict.fromkeys(errors)), tuple(dict.fromkeys(warnings)))


def apply_starting_preset(
    settings: AppSettings,
    preset: str,
    *,
    wd_ready: bool = False,
    joy_ready: bool = False,
    consensus_ready: bool = False,
) -> None:
    """Apply a named starting point only through existing settings fields."""
    preset = preset if preset in {"conservative", "balanced", "hands_on"} else "balanced"
    settings.onboarding_preset = preset
    settings.validate_library_on_startup = True
    settings.check_dependencies_on_startup = True
    settings.restore_last_dataset = True
    # Readiness is capability, not consent: starting points preserve AI choices.
    settings.gallery_blur_unknown = True

    if preset == "conservative":
        settings.auto_import_source_evidence = False
        settings.include_subfolders = False
        settings.gallery_content_level = "general"
        settings.gallery_hidden_ratings = ["sensitive", "questionable", "explicit"]
    elif preset == "hands_on":
        settings.auto_import_source_evidence = True
        settings.include_subfolders = True
        settings.gallery_content_level = "explicit"
        settings.gallery_hidden_ratings = []
    else:
        settings.auto_import_source_evidence = True
        settings.include_subfolders = True
        settings.gallery_content_level = "sensitive"
        settings.gallery_hidden_ratings = ["questionable", "explicit"]


def preset_consequences(preset: str, *, wd_ready: bool, joy_ready: bool, consensus_ready: bool) -> tuple[str, ...]:
    if preset == "conservative":
        return (
            "Creates minimal Image Knowledge without importing TXT or Metadata until you request it.",
            "Preserves individual AI choices and includes only the selected folder, not its subfolders.",
            "Blurs sensitive, questionable, explicit, and not-yet-dependably-rated previews.",
            "Checks an opened library for changes at startup.",
        )
    if preset == "hands_on":
        return (
            "Imports current TXT and Metadata as reviewable evidence and includes subfolders.",
            "Automatic AI and consensus confirmation remain your individual checkbox choices.",
            "Shows rated previews without preset blur and keeps unknown previews protected.",
        )
    return (
        "Imports current TXT and Metadata as reviewable evidence and includes subfolders.",
        "Preserves individual AI and confirmation choices; imported findings remain reviewable.",
        "Blurs questionable, explicit, and not-yet-dependably-rated previews.",
        "Checks an opened library for changes at startup.",
    )


def save_settings_verified(settings: AppSettings, path: str | Path = SETTINGS_PATH) -> AppSettings:
    """Atomically save onboarding/settings state and verify the durable read-back."""
    path = Path(path)
    before = path.read_bytes() if path.is_file() else None
    settings.save(path)
    restored = AppSettings.load(path)
    checked = (
        "ingest_directory", "working_directory", "problems_directory",
        "recycle_directory", "ai_models_directory",
        "auto_import_source_evidence", "auto_run_wd_on_managed_images",
        "auto_run_joy_on_managed_images", "auto_merge_ai_tags", "include_subfolders",
        "restore_last_dataset", "validate_library_on_startup", "check_dependencies_on_startup",
        "update_channel", "gallery_content_level", "gallery_hidden_ratings", "gallery_blur_unknown",
        "enabled_wd_taggers", "wd_execution_provider", "wd_batch_size",
        "wd_consensus_minimum", "ai_model_benchmarks", "plain_language_model",
        "joycaption_gpu_index", "joycaption_quantization",
        "joycaption_quantization_explicit", "joycaption_max_new_tokens",
        "joycaption_num_beams", "joycaption_do_sample",
        "suggest_wd_for_missing_tags",
        "suggest_joycaption_for_missing_descriptions",
        "onboarding_status", "onboarding_version", "onboarding_step",
        "onboarding_completed_steps", "onboarding_preset", "onboarding_goal",
        "onboarding_model_usage_choice",
    )
    if all(getattr(restored, name) == getattr(settings, name) for name in checked):
        return restored
    try:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            temporary = path.with_suffix(path.suffix + ".restore")
            temporary.write_bytes(before)
            temporary.replace(path)
    except OSError:
        pass
    raise RuntimeError("Guided setup could not verify the saved settings. The previous settings were restored when possible.")


def checkpoint_onboarding(
    settings: AppSettings,
    *,
    status: str,
    step: str,
    completed_steps: Iterable[str] = (),
    path: str | Path = SETTINGS_PATH,
) -> AppSettings:
    settings.onboarding_status = status if status in ONBOARDING_STATUSES else "in_progress"
    settings.onboarding_version = ONBOARDING_VERSION
    settings.onboarding_step = step if step in ONBOARDING_STEPS else "welcome"
    settings.onboarding_completed_steps = [
        value for value in ONBOARDING_STEPS
        if value in {str(item) for item in completed_steps}
    ]
    return save_settings_verified(settings, path)


def reset_onboarding_progress(settings: AppSettings) -> None:
    """Reset only the checklist cursor; never delete or reset configured data."""
    settings.onboarding_status = "in_progress"
    settings.onboarding_version = ONBOARDING_VERSION
    settings.onboarding_step = "welcome"
    settings.onboarding_completed_steps = []
