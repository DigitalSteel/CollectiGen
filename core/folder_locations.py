"""Persistent folder roles presented by Setup; existing stores remain authoritative.

Adding a persistent location requires a role here, its existing owner/validation,
and a Setup editor. Operation-specific file selections do not belong here.
"""
from pathlib import Path

from .external_write_policy import ExternalWritePolicy
from .onboarding import validate_locations
from .resolution_workflow import load_resolution_settings, save_resolution_settings
from .training_source_io import _assert_unredirected


APP_FOLDERS = (
    ("ingest_directory", "Incoming images"),
    ("problems_directory", "Problems"),
    ("recycle_directory", "Recycle Bin"),
    ("ai_models_directory", "AI analysis models"),
    ("external_write_backup_directory", "Protected image backups"),
)
IMAGE_APP_FOLDERS = tuple(item for item in APP_FOLDERS if item[0] != "ai_models_directory")
ANALYSIS_APP_FOLDERS = tuple(item for item in APP_FOLDERS if item[0] == "ai_models_directory")
RESIZE_FOLDERS = (
    ("output_root", "Manual resize versions"),
    ("upscale_output_root", "Automatic upscale versions"),
    ("downscale_output_root", "Automatic downscale versions"),
)


def protected_roots(settings):
    if settings is None:
        return ()
    from .history_configuration import history_roots
    from .structured_history_configuration import structured_history_roots
    return history_roots(getattr(settings, "history_storage", {})) + structured_history_roots(getattr(settings, "structured_history_storage", {})) + tuple(p.root for p in settings.library_registry().profiles) + tuple(
        str(getattr(settings, key, "") or "")
        for key, _ in APP_FOLDERS if getattr(settings, key, "")
    )


def validate_version_folders(values, roots=()):
    for value in values.values():
        if not value:
            continue
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("Choose an absolute folder for protected resize versions.")
        path = path.absolute()
        # Check scope before accessing the selected location.
        for root in roots:
            base = Path(root).absolute()
            if path == base or path in base.parents or base in path.parents:
                raise ValueError("Keep resize versions separate from libraries, models and other storage roles.")
        _assert_unredirected(path)
        if path.exists() and not path.is_dir():
            raise ValueError("Choose a folder for protected resize versions, not a file.")


def validate_model_folder_separation(app_values, profiles, workspace, *, snapshot=None):
    """Check the reverse direction when changing a protected app folder."""
    from .model_workspace import _path
    roots = tuple(p.root for p in profiles) + tuple(
        value for key, value in app_values.items()
        if value and key != "ai_models_directory"
    )
    from .model_storage_policy import model_storage_roots
    for value in model_storage_roots(snapshot if snapshot is not None else {"workspace": workspace}):
        if value:
            _path(value, roots)


def save_version_folders(values, *, path=None, expected=None, roots=()):
    previous = load_resolution_settings(path) if expected is None else expected
    selected = {key: str(values.get(key, "") or "").strip() for key, _ in RESIZE_FOLDERS}
    validate_version_folders(selected, roots)
    updated = dict(previous)
    updated.update(selected)
    # A destination is part of automatic-write consent. Never carry permission
    # to a different folder merely because a storage preference changed.
    for direction in ("upscale", "downscale"):
        key = direction + "_output_root"
        if selected[key] != previous.get(key, ""):
            updated["automatic_" + direction] = False
    save_resolution_settings(updated, path, expected=previous)
    return load_resolution_settings(path)


def save_app_folders(settings, values, *, expected):
    keys = tuple(key for key, _ in APP_FOLDERS)
    previous = {key: str(getattr(settings, key, "") or "") for key in keys}
    if set(values) - set(keys):
        raise ValueError("Unsupported folder setting.")
    if any(key not in expected or previous[key] != expected[key] for key in values):
        raise ValueError("Folder settings changed elsewhere. Reload folders before saving.")
    selected = dict(previous)
    selected.update({key: str(value).strip() for key, value in values.items()})
    from .history_configuration import history_roots, overlaps
    from .structured_history_configuration import structured_history_roots
    retained = history_roots(getattr(settings, "history_storage", {})) + structured_history_roots(getattr(settings, "structured_history_storage", {}))
    if any(overlaps(root, value) for root in retained
            for value in selected.values() if value):
        raise ValueError("Keep storage roles separate from retained History & Versions folders.")
    validation = validate_locations(selected, library_profiles=settings.library_registry().profiles)
    if not validation.valid:
        raise ValueError(" ".join(validation.errors))
    policy = ExternalWritePolicy(settings.external_write_backup_mode, selected["external_write_backup_directory"])
    issues = policy.backup_destination_issues(settings.library_registry()) if selected["external_write_backup_directory"] else ()
    if issues:
        raise ValueError(" ".join(issues))
    try:
        for key, value in selected.items():
            setattr(settings, key, value)
        settings.save()
    except Exception:
        for key, value in previous.items():
            setattr(settings, key, value)
        raise
    return validation


def save_history_folders(settings, value, *, expected, expected_sources, registry, resolution_path=None, settings_path=None):
    from .history_configuration import validate_configuration, check_storage
    from .settings import DEFAULT_SETTINGS_PATH
    def validate(selected, current):
        snapshot = registry.snapshot()[0]
        from .model_storage_policy import model_storage_roots
        resize = load_resolution_settings(resolution_path)
        extra = tuple(item for item in (*model_storage_roots(snapshot),
            *(resize.get(key, "") for key, _ in RESIZE_FOLDERS)) if item)
        validate_configuration(selected, current, extra_roots=extra)
        old = expected.get("bindings", {})
        for key, binding in selected["bindings"].items():
            if binding["enabled"] and not binding["retired"] and binding != old.get(key):
                check_storage(selected["zones"][binding["zone_id"]]["zone_root"], binding["history_root"])
    settings.update_history_storage(value, expected=expected, expected_sources=expected_sources,
        path=settings_path or DEFAULT_SETTINGS_PATH, validate=validate)
    return settings.history_storage


def save_structured_history_folders(settings, value, *, expected, expected_authority, registry,
                                    resolution_path=None, settings_path=None):
    from .structured_history_configuration import validate_configuration, check_storage
    from .settings import DEFAULT_SETTINGS_PATH
    path = settings_path or DEFAULT_SETTINGS_PATH
    def validate(selected, current):
        snapshot = registry.snapshot()[0]
        from .model_storage_policy import model_storage_roots
        resize = load_resolution_settings(resolution_path)
        extra = tuple(item for item in (*model_storage_roots(snapshot),
            *(resize.get(key, "") for key, _ in RESIZE_FOLDERS)) if item)
        validate_configuration(selected, current, settings_path=path, extra_roots=extra)
        old = expected.get("bindings", {})
        for key, binding in selected["bindings"].items():
            if binding["enabled"] and not binding["retired"] and binding != old.get(key):
                check_storage(selected["zones"][binding["zone_id"]]["zone_root"], binding["history_root"])
    settings.update_structured_history_storage(value, expected=expected, expected_authority=expected_authority,
        path=path, validate=validate)
    return settings.structured_history_storage
