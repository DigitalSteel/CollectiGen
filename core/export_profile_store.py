from __future__ import annotations

import hashlib
import json
import re
import shutil
from copy import deepcopy
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .content_sensitivity import RATING_ORDER
from .export_profile import (
    HIERARCHY_POLICIES,
    ExportProfile,
    builtin_export_profiles,
)


EXPORT_PROFILE_STORE_SCHEMA_VERSION = 1
EXPORT_PROFILE_FORMAT_VERSION = 6
USER_PROFILE_ID_PATTERN = re.compile(r"^user\.[a-z0-9][a-z0-9.-]{5,95}$")


class ExportProfileStoreError(RuntimeError):
    pass


class ExportProfileValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ExportProfileStoreIssue:
    location: str
    message: str


@dataclass(frozen=True)
class ExportProfileSaveResult:
    profile: ExportProfile
    changed: bool
    backup_directory: Path | None = None


def _sha256(payload: bytes | None) -> str:
    return hashlib.sha256(payload or b"").hexdigest()


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_bytes(
        path,
        (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )


def _clean_text(value: Any, *, field_name: str, maximum: int) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ExportProfileValidationError(f"Enter {field_name}.")
    if len(text) > maximum:
        raise ExportProfileValidationError(
            f"{field_name.title()} must be {maximum:,} characters or fewer."
        )
    return text


def validate_export_profile(
    profile: ExportProfile,
    *,
    require_user_id: bool = False,
) -> None:
    if require_user_id and (
        not isinstance(profile.profile_id, str)
        or not USER_PROFILE_ID_PATTERN.fullmatch(profile.profile_id)
    ):
        raise ExportProfileValidationError(
            "This training profile does not have a valid stable user-profile ID."
        )
    text_fields = (
        ("name", "a profile name", 80, False),
        ("description", "a profile description", 600, False),
        ("target_model_family", "a target model family", 80, False),
        ("target_model_version", "a target model version", 80, True),
        ("training_objective", "a training objective", 160, False),
        ("caption_strategy", "a caption strategy", 120, False),
        ("trigger_policy", "a trigger policy", 200, False),
    )
    for attribute, field_name, maximum, allow_empty in text_fields:
        value = getattr(profile, attribute)
        if not isinstance(value, str):
            raise ExportProfileValidationError(
                f"The profile has an invalid {attribute.replace('_', ' ')}."
            )
        if allow_empty and not value:
            continue
        _clean_text(value, field_name=field_name, maximum=maximum)
    if not isinstance(profile.version, int) or isinstance(profile.version, bool) or profile.version < 1:
        raise ExportProfileValidationError("Profile versions must be positive.")
    if (
        not isinstance(profile.effective_format_version, int)
        or isinstance(profile.effective_format_version, bool)
        or profile.effective_format_version < 1
    ):
        raise ExportProfileValidationError("Profile format versions must be positive.")
    if profile.maximum_content_rating not in RATING_ORDER:
        raise ExportProfileValidationError(
            "The profile has an unsupported maximum content rating."
        )
    if profile.hierarchy_policy not in HIERARCHY_POLICIES:
        raise ExportProfileValidationError(
            "The profile has an unsupported Tag hierarchy policy."
        )
    if not isinstance(profile.separator, str) or profile.separator not in {", ", "\n", ",\n"}:
        raise ExportProfileValidationError(
            "The profile uses an unsupported training-caption separator."
        )
    if profile.token_budget is not None and (
        not isinstance(profile.token_budget, int)
        or isinstance(profile.token_budget, bool)
        or not 1 <= profile.token_budget <= 1_000_000
    ):
        raise ExportProfileValidationError(
            "The recorded token budget must be between 1 and 1,000,000."
        )
    inclusion_fields = (
        "include_character_names",
        "include_character_tags",
        "include_outfit_names",
        "include_outfit_tags",
        "include_outfit_prose",
        "include_object_identities",
        "include_object_tags",
        "include_global",
        "include_scene",
        "include_contexts",
        "include_sdxl",
        "include_uncategorized",
        "include_custom_groups",
        "include_plain_language",
    )
    if not any(bool(getattr(profile, name)) for name in inclusion_fields):
        raise ExportProfileValidationError(
            "Include at least one Image Knowledge source in the training profile."
        )
    boolean_fields = (
        *inclusion_fields,
        "apply_system_ordering",
        "canonicalize_aliases",
        "apply_automation_rules",
    )
    if any(not isinstance(getattr(profile, name), bool) for name in boolean_fields):
        raise ExportProfileValidationError(
            "The profile has an invalid enabled or disabled setting."
        )
    if profile.include_outfit_prose and not profile.include_plain_language:
        raise ExportProfileValidationError(
            "Outfit descriptions require Plain Language Descriptions."
        )
    for group_field in (
        "character_groups",
        "global_groups",
        "scene_groups",
        "custom_group_names",
    ):
        values = getattr(profile, group_field)
        if values is not None and (
            not isinstance(values, set)
            or any(not isinstance(value, str) or not value.strip() for value in values)
        ):
            raise ExportProfileValidationError(
                f"The profile has invalid values in {group_field}."
            )
    for assumption_field in ("vocabulary_assumptions", "adapter_assumptions"):
        values = getattr(profile, assumption_field)
        if not isinstance(values, tuple) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise ExportProfileValidationError(
                f"The profile has invalid values in {assumption_field}."
            )


class ExportProfileStore:
    """Forward-compatible user projection profiles with verified file writes."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.profiles_by_id: dict[str, ExportProfile] = {}
        self.issues: list[ExportProfileStoreIssue] = []
        self.load()

    @property
    def writable(self) -> bool:
        return not self._load_error and self._schema_version <= EXPORT_PROFILE_STORE_SCHEMA_VERSION

    @property
    def load_error(self) -> str:
        return self._load_error

    def load(self) -> None:
        self.profiles_by_id = {}
        self.issues = []
        self._raw_by_id: dict[str, dict[str, Any]] = {}
        self._invalid_records: list[Any] = []
        self._payload_extras: dict[str, Any] = {}
        self._schema_version = EXPORT_PROFILE_STORE_SCHEMA_VERSION
        self._load_error = ""
        raw = None
        try:
            raw = self.path.read_bytes() if self.path.exists() else None
            payload = json.loads(raw.decode("utf-8")) if raw is not None else {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            self._loaded_bytes = raw
            self._load_error = f"Saved training profiles could not be read: {exc}"
            self.issues.append(ExportProfileStoreIssue("file", self._load_error))
            return
        self._loaded_bytes = raw
        if not isinstance(payload, dict):
            self._load_error = "Saved training profiles must use a JSON object."
            self.issues.append(ExportProfileStoreIssue("file", self._load_error))
            return
        self._payload_extras = {
            key: deepcopy(value)
            for key, value in payload.items()
            if key not in {"schema_version", "profiles"}
        }
        try:
            self._schema_version = max(
                1, int(payload.get("schema_version", EXPORT_PROFILE_STORE_SCHEMA_VERSION))
            )
        except (TypeError, ValueError):
            self._load_error = "Saved training profiles have an invalid schema version."
            self.issues.append(ExportProfileStoreIssue("schema_version", self._load_error))
            return
        if self._schema_version > EXPORT_PROFILE_STORE_SCHEMA_VERSION:
            self.issues.append(
                ExportProfileStoreIssue(
                    "schema_version",
                    "These training profiles were written by a newer CollectiGen version. "
                    "They remain available only after updating this application.",
                )
            )
        records = payload.get("profiles", [])
        if not isinstance(records, list):
            self._load_error = "Saved training profiles must contain a profile list."
            self.issues.append(ExportProfileStoreIssue("profiles", self._load_error))
            return
        builtin_ids = {profile.profile_id for profile in builtin_export_profiles()}
        for index, raw_profile in enumerate(records):
            location = f"profiles[{index}]"
            if not isinstance(raw_profile, dict):
                self._invalid_records.append(deepcopy(raw_profile))
                self.issues.append(
                    ExportProfileStoreIssue(location, "This profile is not a JSON object.")
                )
                continue
            try:
                profile = ExportProfile.from_dict(raw_profile)
                validate_export_profile(profile, require_user_id=True)
                if profile.effective_format_version > EXPORT_PROFILE_FORMAT_VERSION:
                    raise ExportProfileValidationError(
                        "This profile uses a newer projection format and was left unchanged."
                    )
                if profile.profile_id in builtin_ids:
                    raise ExportProfileValidationError(
                        "A user profile cannot replace a built-in profile."
                    )
                if profile.profile_id in self.profiles_by_id:
                    raise ExportProfileValidationError(
                        "This stable profile ID is duplicated."
                    )
            except (TypeError, ValueError, ExportProfileValidationError) as exc:
                self._invalid_records.append(deepcopy(raw_profile))
                self.issues.append(ExportProfileStoreIssue(location, str(exc)))
                continue
            self.profiles_by_id[profile.profile_id] = profile
            self._raw_by_id[profile.profile_id] = deepcopy(raw_profile)

    def all_profiles(self) -> tuple[ExportProfile, ...]:
        users = sorted(
            self.profiles_by_id.values(),
            key=lambda profile: (profile.name.casefold(), profile.profile_id),
        )
        return (*builtin_export_profiles(), *users)

    def resolve(self, profile_id: str | None) -> ExportProfile:
        if profile_id in self.profiles_by_id:
            return self.profiles_by_id[str(profile_id)]
        for profile in builtin_export_profiles():
            if profile.profile_id == profile_id:
                return profile
        return builtin_export_profiles()[0]

    @staticmethod
    def new_profile_id() -> str:
        return f"user.{uuid4().hex}"

    def duplicate(self, source: ExportProfile, name: str) -> ExportProfileSaveResult:
        clean_name = _clean_text(name, field_name="a profile name", maximum=80)
        draft = replace(
            source,
            profile_id=self.new_profile_id(),
            version=1,
            format_version=max(
                EXPORT_PROFILE_FORMAT_VERSION, source.effective_format_version
            ),
            name=clean_name,
        )
        return self.save_profile(draft)

    def save_profile(
        self,
        profile: ExportProfile,
        *,
        expected_fingerprint: str | None = None,
    ) -> ExportProfileSaveResult:
        self._ensure_writable()
        validate_export_profile(profile, require_user_id=True)
        current = self.profiles_by_id.get(profile.profile_id)
        if current is not None:
            if expected_fingerprint is None or current.fingerprint() != expected_fingerprint:
                raise ExportProfileStoreError(
                    "This training profile changed after editing began. Reload it and retry."
                )
            candidate = replace(
                profile,
                profile_id=current.profile_id,
                version=current.version,
                format_version=(
                    profile.format_version
                    or current.format_version
                    or max(
                        EXPORT_PROFILE_FORMAT_VERSION,
                        current.effective_format_version,
                    )
                ),
            )
            if candidate.fingerprint() == current.fingerprint():
                return ExportProfileSaveResult(current, False)
            saved_profile = replace(candidate, version=current.version + 1)
        else:
            if expected_fingerprint is not None:
                raise ExportProfileStoreError(
                    "The training profile no longer exists. Reload the profile list."
                )
            saved_profile = replace(
                profile,
                version=1,
                format_version=(
                    profile.format_version
                    or max(
                        EXPORT_PROFILE_FORMAT_VERSION,
                        profile.effective_format_version,
                    )
                ),
            )
        validate_export_profile(saved_profile, require_user_id=True)
        next_profiles = dict(self.profiles_by_id)
        next_profiles[saved_profile.profile_id] = saved_profile
        backup = self._write_profiles(next_profiles)
        return ExportProfileSaveResult(
            self.profiles_by_id[saved_profile.profile_id], True, backup
        )

    def delete_profile(
        self,
        profile_id: str,
        *,
        expected_fingerprint: str,
    ) -> Path:
        self._ensure_writable()
        current = self.profiles_by_id.get(profile_id)
        if current is None:
            raise ExportProfileStoreError(
                "The training profile no longer exists. Reload the profile list."
            )
        if current.fingerprint() != expected_fingerprint:
            raise ExportProfileStoreError(
                "This training profile changed after editing began. Reload it and retry."
            )
        next_profiles = dict(self.profiles_by_id)
        del next_profiles[profile_id]
        return self._write_profiles(next_profiles)

    def _ensure_writable(self) -> None:
        if self._load_error:
            raise ExportProfileStoreError(
                f"{self._load_error} The existing file was left unchanged."
            )
        if self._schema_version > EXPORT_PROFILE_STORE_SCHEMA_VERSION:
            raise ExportProfileStoreError(
                "Saved training profiles use a newer format and were left unchanged."
            )
        try:
            current = self.path.read_bytes() if self.path.exists() else None
        except OSError as exc:
            raise ExportProfileStoreError(
                f"Saved training profiles could not be checked for changes: {exc}"
            ) from exc
        if current != self._loaded_bytes:
            raise ExportProfileStoreError(
                "Saved training profiles changed after this view opened. Reload and retry."
            )

    def _profile_payload(
        self,
        profiles: dict[str, ExportProfile],
    ) -> dict[str, Any]:
        known_fields = {definition.name for definition in fields(ExportProfile)}
        records: list[Any] = []
        for profile in sorted(
            profiles.values(),
            key=lambda item: (item.name.casefold(), item.profile_id),
        ):
            raw = self._raw_by_id.get(profile.profile_id, {})
            record = {
                key: deepcopy(value)
                for key, value in raw.items()
                if key not in known_fields
            }
            record.update(profile.to_dict())
            records.append(record)
        records.extend(deepcopy(self._invalid_records))
        payload = deepcopy(self._payload_extras)
        payload.update(
            {
                "schema_version": EXPORT_PROFILE_STORE_SCHEMA_VERSION,
                "profiles": records,
            }
        )
        return payload

    def _write_profiles(self, profiles: dict[str, ExportProfile]) -> Path:
        self._ensure_writable()
        previous = self._loaded_bytes
        expected = self._profile_payload(profiles)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = (
            self.path.parent
            / "backups"
            / "export-profiles"
            / f"{stamp}_{uuid4().hex[:8]}"
        )
        backup.mkdir(parents=True, exist_ok=False)
        before_name = ""
        if previous is not None:
            before_name = self.path.name
            shutil.copy2(self.path, backup / before_name)
        manifest_path = backup / "recovery.json"
        manifest = {
            "schema_version": 1,
            "operation": "export_profile_store_write",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "target": str(self.path.resolve()),
            "before": before_name,
            "before_sha256": _sha256(previous),
            "after_sha256": "",
            "status": "prepared",
        }
        _atomic_json(manifest_path, manifest)
        try:
            _atomic_json(self.path, expected)
            actual = json.loads(self.path.read_text(encoding="utf-8"))
            if actual != expected:
                raise ExportProfileStoreError(
                    "Saved training profiles failed read-back verification."
                )
            after = self.path.read_bytes()
            manifest["after_sha256"] = _sha256(after)
            manifest["status"] = "complete"
            _atomic_json(manifest_path, manifest)
        except BaseException:
            if previous is None:
                if self.path.exists():
                    self.path.unlink()
            else:
                _atomic_bytes(self.path, previous)
            manifest["status"] = "rolled_back"
            _atomic_json(manifest_path, manifest)
            self.load()
            raise
        self.load()
        return backup
