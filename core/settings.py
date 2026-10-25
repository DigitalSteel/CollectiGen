from __future__ import annotations

import hashlib
import json
import math
import os
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from threading import RLock
from uuid import uuid4

from version import VERSION

from .paths import SETTINGS_PATH
from .paths import DATA_DIR
from .organization import default_organization_profile
from .fact_expansion_policy import (
    FACT_EXPANSION_MIN_DEPTH,
    FACT_EXPANSION_MAX_DEPTH,
    FACT_EXPANSION_RELATIONSHIPS,
    clean_fact_expansion_relationships,
)
from .external_write_policy import normalize_external_write_backup_mode
from .library_profiles import (
    LibraryProfile,LibraryProfileRegistry,LibraryRole,
)
from .verified_transfer import (
    LIBRARY_NAMING_IMAGE_HASH,LIBRARY_NAMING_KEEP,normalize_library_naming_policy,
)


DEFAULT_SETTINGS_PATH = SETTINGS_PATH
SETTINGS_HISTORY_LIMIT = 10
SETTINGS_RECOVERY_LIMIT = 10
_SETTINGS_LOCKS: dict[str, RLock] = {}
_SETTINGS_LOCKS_GUARD = RLock()


@contextmanager
def _settings_writer(path):
    """Short shared publication lock; no dialogs or source inspection inside."""
    from .training_source_io import _assert_unredirected
    path = Path(path).absolute()
    key = os.path.normcase(str(path))
    with _SETTINGS_LOCKS_GUARD:
        thread_lock = _SETTINGS_LOCKS.setdefault(key, RLock())
    with thread_lock:
        _assert_unredirected(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = path.with_name(path.name + ".lock")
        _assert_unredirected(lock)
        with lock.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0: handle.write(b"0"); handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try: yield
            finally:
                handle.seek(0)
                if os.name == "nt": msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

DEFAULT_UPDATE_CHANNEL = "beta" if "-" in VERSION else "stable"
JOYCAPTION_MODEL_ID = "fancyfeast/llama-joycaption-beta-one-hf-llava"
FLORENCE2_MODEL_ID = "florence-community/Florence-2-base-ft"
PLAIN_LANGUAGE_MODEL_IDS = (JOYCAPTION_MODEL_ID,FLORENCE2_MODEL_ID)
PLAIN_LANGUAGE_MAX_NEW_TOKENS_FLOOR = 256
LEGACY_JOYCAPTION_PROMPT = (
    "Write one precise, detailed training caption describing only what is visibly present. "
    "Include subjects, appearance, clothing, action, composition, setting, lighting, and visual style. "
    "Do not invent character names or hidden details."
)
PREVIOUS_DEFAULT_JOYCAPTION_PROMPT = (
    "Write one concise visual description as a single paragraph of 3 to 6 complete sentences. "
    "Describe only what is visibly present: medium and style; subjects and notable appearance; "
    "clothing or accessories; pose and action; composition; setting; lighting; and expression. "
    "Use clear natural language suitable for image training data. Do not invent character names, "
    "identity, intent, or hidden details. Do not discuss missing or unknown artist names, signatures, "
    "watermarks, prompts, sources, or provenance."
)
JOYCAPTION_IDENTITY_GUIDANCE = (
    "Identify a known character and their franchise or series only when distinctive visual evidence "
    "makes the match highly confident. Otherwise describe visible traits and leave the identity "
    "unspecified. Do not infer identity from art style alone or invent names, affiliations, intent, "
    "or hidden details."
)
DEFAULT_JOYCAPTION_PROMPT = (
    "Write one concise visual description as a single paragraph of 3 to 6 complete sentences. "
    "Describe only what is visibly present: medium and style; subjects and notable appearance; "
    "clothing or accessories; pose and action; composition; setting; lighting; and expression. "
    "Use clear natural language suitable for image training data. "
    f"{JOYCAPTION_IDENTITY_GUIDANCE} "
    "Do not discuss missing or unknown artist names, signatures, watermarks, prompts, sources, "
    "or provenance."
)


def normalized_plain_language_max_new_tokens(value: object) -> int:
    """Keep ordinary description generation above the reviewed truncation floor."""
    return max(PLAIN_LANGUAGE_MAX_NEW_TOKENS_FLOOR,int(value))


class SettingsRecoveryState(str, Enum):
    CLEAN = "clean"
    RECOVERED = "recovered"
    DECISION_REQUIRED = "decision_required"
    UNAVAILABLE = "unavailable"
    SAFE_DEFAULTS = "safe_defaults"


@dataclass(frozen=True)
class SettingsRecoveryReport:
    state: SettingsRecoveryState = SettingsRecoveryState.CLEAN
    detail: str = ""
    invalid_sha256: str = ""
    invalid_size: int = 0
    quarantine_path: str = ""
    recovered_from: str = ""
    safe_defaults_allowed: bool = False

    @property
    def requires_decision(self) -> bool:
        return self.state in {
            SettingsRecoveryState.DECISION_REQUIRED,
            SettingsRecoveryState.UNAVAILABLE,
        }

    @property
    def can_use_safe_defaults(self) -> bool:
        return (
            self.state is SettingsRecoveryState.DECISION_REQUIRED
            and bool(self.invalid_sha256)
            and self.safe_defaults_allowed
        )


class SettingsRecoveryRequiredError(OSError):
    """Raised when an in-memory fallback must not overwrite unread settings."""


def _settings_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_replace_bytes(path: Path, value: bytes, *, label: str) -> None:
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(f".{path.name}.{label}.{uuid4().hex}.tmp")
    try:
        temp.write_bytes(value)
        if temp.read_bytes()!=value:
            raise OSError(f"The staged {label} file failed read-back verification.")
        temp.replace(path)
        if path.read_bytes()!=value:
            raise OSError(f"The replaced {label} file failed read-back verification.")
    finally:
        temp.unlink(missing_ok=True)


def _write_settings_recovery_record(
    directory: Path,
    *,
    invalid_bytes: bytes,
    status: str,
    recovered_bytes: bytes,
    recovered_from: Path | None,
) -> tuple[Path,Path]:
    directory=Path(directory); directory.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    stem=f"{stamp}_{uuid4().hex[:8]}"
    quarantine=directory/f"{stem}.invalid.json"
    quarantine_temp=directory/f".{stem}.invalid.tmp"
    record=directory/f"{stem}.recovery.json"
    try:
        quarantine_temp.write_bytes(invalid_bytes)
        if quarantine_temp.read_bytes()!=invalid_bytes:
            raise OSError("The invalid settings quarantine failed read-back verification.")
        quarantine_temp.replace(quarantine)
        if quarantine.read_bytes()!=invalid_bytes:
            raise OSError("The invalid settings quarantine failed read-back verification.")
        payload={
            "schema_version":1,
            "kind":"collectigen_settings_recovery",
            "status":str(status),
            "created_at":datetime.now(timezone.utc).isoformat(),
            "invalid_file":quarantine.name,
            "invalid_sha256":_settings_digest(invalid_bytes),
            "invalid_size":len(invalid_bytes),
            "recovered_sha256":_settings_digest(recovered_bytes),
            "recovered_size":len(recovered_bytes),
            "recovered_from":recovered_from.name if recovered_from is not None else "safe_defaults",
        }
        raw=(json.dumps(payload,indent=2,ensure_ascii=False)+"\n").encode("utf-8")
        _atomic_replace_bytes(record,raw,label="settings-recovery-record")
        if json.loads(record.read_text(encoding="utf-8"))!=payload:
            raise OSError("The settings recovery record failed semantic read-back verification.")
        return quarantine,record
    except Exception:
        quarantine_temp.unlink(missing_ok=True)
        quarantine.unlink(missing_ok=True)
        record.unlink(missing_ok=True)
        raise


def _finalize_settings_recovery_record(record: Path, status: str) -> None:
    payload=json.loads(Path(record).read_text(encoding="utf-8"))
    if not isinstance(payload,dict) or payload.get("kind")!="collectigen_settings_recovery":
        raise OSError("The prepared settings recovery record is not recognized.")
    payload["status"]=str(status)
    payload["finalized_at"]=datetime.now(timezone.utc).isoformat()
    raw=(json.dumps(payload,indent=2,ensure_ascii=False)+"\n").encode("utf-8")
    _atomic_replace_bytes(Path(record),raw,label="settings-recovery-finalize")
    if json.loads(Path(record).read_text(encoding="utf-8"))!=payload:
        raise OSError("The finalized settings recovery record failed read-back verification.")


def _prune_settings_recovery(directory: Path) -> None:
    try:
        records=sorted(
            Path(directory).glob("*.recovery.json"),
            key=lambda value:(value.stat().st_mtime_ns,value.name),
            reverse=True,
        )
        for record in records[SETTINGS_RECOVERY_LIMIT:]:
            try:
                payload=json.loads(record.read_text(encoding="utf-8"))
                if payload.get("status") not in {
                    "recovered_from_history","safe_defaults",
                }:
                    continue
                name=str(payload.get("invalid_file") or "")
                quarantine=record.parent/name
                if not (
                    name and Path(name).name==name
                    and quarantine.parent==record.parent
                ):
                    continue
                record.unlink(missing_ok=True)
                quarantine.unlink(missing_ok=True)
            except (OSError,json.JSONDecodeError,TypeError,AttributeError):
                # Never remove an unrecognized recovery record merely to enforce
                # retention. A later explicit maintenance path can review it.
                continue
    except OSError:
        # Retaining an extra verified recovery record is safer than converting a
        # successful settings recovery into a startup failure.
        pass


def normalize_plain_language_exclusion(value: str) -> str:
    """Normalize harmless presentation differences without matching partial prose."""
    text=str(value or "").translate(str.maketrans({"’":"'","‘":"'","“":"\"","”":"\""}))
    return " ".join(text.split()).rstrip(" .!?").casefold()


def _clean_plain_language_exclusions(values) -> list[str]:
    cleaned=[]; seen=set()
    for raw in values if isinstance(values,list) else []:
        value=" ".join(str(raw or "").split())
        key=normalize_plain_language_exclusion(value)
        if key and key not in seen:
            cleaned.append(value); seen.add(key)
    return cleaned


def _clean_tag_convenience_values(values, *, limit: int) -> list[str]:
    cleaned=[]; seen=set()
    for raw in values if isinstance(values,list) else []:
        value=" ".join(str(raw or "").split())
        key=" ".join(value.casefold().replace("_"," ").split())
        if value and key not in seen:
            cleaned.append(value); seen.add(key)
        if len(cleaned)>=limit: break
    return cleaned


def _validate_settings_payload_shape(payload: dict[str,Any]) -> None:
    """Reject known fields whose JSON shape cannot preserve current semantics."""
    string_fields={
        "dataset_directory","ingest_directory","inbox_directory","working_directory",
        "active_library_id","library_move_naming_policy","problems_directory",
        "recycle_directory",
        "exiftool_path","ai_models_directory","wd_execution_provider",
        "plain_language_model","joycaption_prompt","joycaption_quantization","ai_runtime_mode",
        "prepare_ai_skip_unit","prepare_confidence_preset","ui_theme","update_channel",
        "gallery_content_level","export_content_level","onboarding_status",
        "onboarding_step","onboarding_preset","onboarding_goal",
        "last_ingest_attention_fingerprint","window_geometry",
        "external_write_backup_mode","external_write_backup_directory",
    }
    bool_fields={
        "auto_merge_ai_tags","auto_run_wd_on_managed_images",
        "auto_run_joy_on_managed_images","joycaption_do_sample",
        "joycaption_quantization_explicit","suggest_wd_for_missing_tags",
        "suggest_joycaption_for_missing_descriptions","prepare_read_external_evidence",
        "ingest_create_duplicate_fingerprints",
        "prepare_external_evidence_vote","prepare_confirm_safe_tags",
        "prepare_preferred_descriptions","include_subfolders","show_tooltips",
        "color_blind_mode","restore_last_dataset","validate_library_on_startup",
        "check_dependencies_on_startup","auto_import_source_evidence",
        "gallery_blur_unknown","remember_content_reveals","fact_expansion_enabled",
        "check_updates_on_startup",
    }
    integer_fields={
        "wd_consensus_minimum","wd_batch_size","joycaption_max_new_tokens",
        "ai_runtime_idle_minutes","ai_runtime_memory_budget_mb",
        "joycaption_min_new_tokens","joycaption_num_beams","joycaption_top_k",
        "joycaption_no_repeat_ngram_size","joycaption_gpu_index",
        "prepare_ai_skip_value","prepare_tag_vote_minimum","gallery_page_size",
        "gallery_card_width","fact_expansion_max_depth","onboarding_version",
    }
    number_fields={
        "wd_general_threshold","wd_character_threshold","wd_rating_threshold",
        "joycaption_length_penalty","joycaption_temperature","joycaption_top_p",
        "joycaption_repetition_penalty",
    }
    string_list_fields={
        "library_scope_ids","enabled_wd_taggers",
        "gallery_hidden_ratings","ignored_plain_language_sentences",
        "recent_directories","favorite_sdxl_tags","recent_sdxl_tags",
        "fact_expansion_relationships","onboarding_completed_steps",
    }
    dict_fields={"ai_model_benchmarks","organization_profile","resource_browser_views"}
    usage_choice = payload.get("onboarding_model_usage_choice")
    if usage_choice is not None and type(usage_choice) is not bool:
        raise TypeError("onboarding_model_usage_choice must be true, false, or null.")
    for field_name in string_fields:
        if field_name in payload and not isinstance(payload[field_name],str):
            raise TypeError(f"{field_name} must be a string.")
    for field_name in bool_fields:
        if field_name in payload and not isinstance(payload[field_name],bool):
            raise TypeError(f"{field_name} must be true or false.")
    for field_name in integer_fields:
        value=payload.get(field_name)
        if field_name in payload and (
            isinstance(value,bool) or not isinstance(value,int)
        ):
            raise TypeError(f"{field_name} must be an integer.")
    for field_name in number_fields:
        value=payload.get(field_name)
        if field_name in payload and (
            isinstance(value,bool) or not isinstance(value,(int,float))
            or not math.isfinite(value)
        ):
            raise TypeError(f"{field_name} must be a finite number.")
    for field_name in string_list_fields:
        value=payload.get(field_name)
        if field_name in payload and (
            not isinstance(value,list)
            or any(not isinstance(item,str) for item in value)
        ):
            raise TypeError(f"{field_name} must be a list of strings.")
    profiles=payload.get("library_profiles",[])
    if "library_profiles" in payload and not isinstance(profiles,list):
        raise TypeError("library_profiles must be a list.")
    for field_name in dict_fields:
        if field_name in payload and not isinstance(payload[field_name],dict):
            raise TypeError(f"{field_name} must be an object.")
    for profile in profiles:
        if not isinstance(profile,dict):
            raise TypeError("Every named library setting must be an object.")
        for field_name in ("stable_id","id","name","root","role"):
            if field_name in profile and not isinstance(profile[field_name],str):
                raise TypeError(
                    f"Every named library {field_name} setting must be a string."
                )
        profile_version=profile.get("schema_version")
        if "schema_version" in profile and (
            isinstance(profile_version,bool) or not isinstance(profile_version,int)
        ):
            raise TypeError("Every named library schema_version must be an integer.")


@dataclass
class AppSettings:
    dataset_directory: str = ""
    ingest_directory: str = ""
    working_directory: str = ""
    library_profiles: list[dict[str, Any]] = field(default_factory=list)
    history_storage: dict[str, Any] = field(default_factory=dict)
    structured_history_storage: dict[str, Any] = field(default_factory=dict)
    active_library_id: str = ""
    library_scope_ids: list[str] = field(default_factory=list)
    library_move_naming_policy: str = LIBRARY_NAMING_IMAGE_HASH
    problems_directory: str = ""
    recycle_directory: str = ""
    exiftool_path: str = ""
    ai_models_directory: str = str(DATA_DIR / "models")
    ai_runtime_mode: str = "on_demand"
    ai_runtime_idle_minutes: int = 10
    ai_runtime_memory_budget_mb: int = 0
    wd_general_threshold: float = 0.35
    wd_character_threshold: float = 0.85
    wd_rating_threshold: float = 0.35
    enabled_wd_taggers: list[str] = field(default_factory=lambda:["SmilingWolf/wd-eva02-large-tagger-v3"])
    wd_consensus_minimum: int = 1
    auto_merge_ai_tags: bool = False
    auto_run_wd_on_managed_images: bool = False
    auto_run_joy_on_managed_images: bool = False
    wd_execution_provider: str = "auto"
    wd_batch_size: int = 1
    ai_model_benchmarks: dict[str,Any] = field(default_factory=dict)
    plain_language_model: str = JOYCAPTION_MODEL_ID
    joycaption_prompt: str = DEFAULT_JOYCAPTION_PROMPT
    joycaption_max_new_tokens: int = 256
    joycaption_min_new_tokens: int = 30
    joycaption_num_beams: int = 4
    joycaption_length_penalty: float = 1.0
    joycaption_do_sample: bool = False
    joycaption_temperature: float = 0.5
    joycaption_top_k: int = 50
    joycaption_top_p: float = 0.9
    joycaption_repetition_penalty: float = 1.0
    joycaption_no_repeat_ngram_size: int = 3
    joycaption_gpu_index: int = 0
    joycaption_quantization: str = "bf16"
    joycaption_quantization_explicit: bool = False
    suggest_wd_for_missing_tags: bool = True
    suggest_joycaption_for_missing_descriptions: bool = True
    prepare_read_external_evidence: bool = True
    ingest_create_duplicate_fingerprints: bool = True
    ingest_check_duplicates: bool = False
    ingest_include_similar_duplicates: bool = False
    ingest_duplicate_similarity_minimum: int = 95
    ingest_duplicate_similarity_maximum: int = 100
    prepare_ai_skip_value: int = 7
    prepare_ai_skip_unit: str = "days"
    prepare_confidence_preset: str = "balanced"
    prepare_tag_vote_minimum: int = 2
    prepare_external_evidence_vote: bool = True
    prepare_confirm_safe_tags: bool = True
    prepare_preferred_descriptions: bool = True
    include_subfolders: bool = True
    show_tooltips: bool = True
    ui_theme: str = "system"
    color_blind_mode: bool = False
    organization_profile: dict[str,Any] = field(default_factory=default_organization_profile)
    restore_last_dataset: bool = True
    validate_library_on_startup: bool = True
    check_dependencies_on_startup: bool = True
    check_updates_on_startup: bool = False
    update_channel: str = DEFAULT_UPDATE_CHANNEL
    auto_import_source_evidence: bool = True
    gallery_page_size: int = 24
    gallery_card_width: int = 155
    resource_browser_views: dict[str,Any] = field(default_factory=dict)
    gallery_content_level: str = "explicit"
    gallery_hidden_ratings: list[str] = field(default_factory=list)
    gallery_blur_unknown: bool = True
    ignored_plain_language_sentences: list[str] = field(default_factory=list)
    remember_content_reveals: bool = True
    export_content_level: str = "explicit"
    recent_directories: list[str] = field(default_factory=list)
    favorite_sdxl_tags: list[str] = field(default_factory=list)
    recent_sdxl_tags: list[str] = field(default_factory=list)
    fact_expansion_enabled: bool = False
    fact_expansion_relationships: list[str] = field(
        default_factory=lambda: list(FACT_EXPANSION_RELATIONSHIPS)
    )
    fact_expansion_max_depth: int = FACT_EXPANSION_MIN_DEPTH
    external_write_backup_mode: str = "always"
    external_write_backup_directory: str = ""
    onboarding_status: str = "not_started"
    onboarding_version: int = 0
    onboarding_step: str = "welcome"
    onboarding_completed_steps: list[str] = field(default_factory=list)
    onboarding_preset: str = "balanced"
    onboarding_goal: str = "library"
    onboarding_model_usage_choice: bool | None = None
    last_ingest_attention_fingerprint: str = ""
    window_geometry: str = "980x650"
    _extra: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    _load_report: SettingsRecoveryReport = field(
        default_factory=SettingsRecoveryReport,repr=False,compare=False,
    )

    @classmethod
    def load(cls, path: Path = DEFAULT_SETTINGS_PATH) -> "AppSettings":
        path = Path(path)
        try:
            current_bytes=path.read_bytes()
        except FileNotFoundError:
            return cls()
        except OSError as exc:
            settings=cls()
            settings._load_report=SettingsRecoveryReport(
                SettingsRecoveryState.UNAVAILABLE,
                "The settings file could not be read. It was left unchanged; "
                f"restore access and restart CollectiGen. ({exc})",
            )
            return settings
        try:
            return cls._from_bytes(current_bytes)
        except (UnicodeDecodeError,json.JSONDecodeError,TypeError,ValueError,OverflowError,AttributeError) as exc:
            return cls._recover_invalid(path,current_bytes,exc)

    @classmethod
    def _from_bytes(cls, value: bytes) -> "AppSettings":
        payload=json.loads(value.decode("utf-8"))
        return cls._from_payload(payload)

    @classmethod
    def _recover_invalid(
        cls,path: Path,current_bytes: bytes,error: Exception,
    ) -> "AppSettings":
        history=path.parent/"settings-history"
        try:
            candidates=sorted(
                history.glob("*.json"),
                key=lambda value:(value.stat().st_mtime_ns,value.name),
                reverse=True,
            )
        except OSError:
            candidates=[]
        for candidate in candidates[:SETTINGS_HISTORY_LIMIT]:
            try:
                candidate_bytes=candidate.read_bytes()
                restored=cls._from_bytes(candidate_bytes)
            except (OSError,UnicodeDecodeError,json.JSONDecodeError,TypeError,ValueError,OverflowError,AttributeError):
                continue
            recovery_dir=path.parent/"settings-recovery"
            replaced=False
            quarantine: Path | None=None
            try:
                # Preserve the unread authority before changing the active path,
                # then recheck its exact bytes immediately before replacement.
                quarantine,record=_write_settings_recovery_record(
                    recovery_dir,
                    invalid_bytes=current_bytes,
                    status="prepared",
                    recovered_bytes=candidate_bytes,
                    recovered_from=candidate,
                )
                if path.read_bytes()!=current_bytes:
                    raise OSError(
                        "The settings file changed again while recovery was being prepared."
                    )
                _atomic_replace_bytes(path,candidate_bytes,label="settings-recovery")
                replaced=True
                verified_bytes=path.read_bytes()
                verified=cls._from_bytes(verified_bytes)
                if verified_bytes!=candidate_bytes or verified!=restored:
                    raise OSError("Recovered settings failed semantic read-back verification.")
                _finalize_settings_recovery_record(record,"recovered_from_history")
                _prune_settings_recovery(recovery_dir)
                restored._load_report=SettingsRecoveryReport(
                    SettingsRecoveryState.RECOVERED,
                    "The active settings were invalid. CollectiGen preserved their "
                    "exact bytes and restored the newest completely valid settings history.",
                    _settings_digest(current_bytes),len(current_bytes),
                    str(quarantine),str(candidate),
                )
                return restored
            except Exception as recovery_exc:
                rollback_error=""
                if replaced:
                    try:
                        _atomic_replace_bytes(path,current_bytes,label="settings-recovery-rollback")
                    except Exception as rollback_exc:
                        rollback_error=f" Exact rollback also failed: {rollback_exc}"
                try:
                    safe_defaults_allowed=path.read_bytes()==current_bytes
                except OSError:
                    safe_defaults_allowed=False
                settings=cls()
                settings._load_report=SettingsRecoveryReport(
                    SettingsRecoveryState.DECISION_REQUIRED,
                    "Automatic settings recovery stopped safely. The in-memory defaults "
                    "cannot be saved until recovery is resolved. "
                    f"Recovery error: {type(recovery_exc).__name__}.{rollback_error}".strip(),
                    _settings_digest(current_bytes),len(current_bytes),
                    str(quarantine or ""),str(candidate),safe_defaults_allowed,
                )
                return settings

        settings=cls()
        settings._load_report=SettingsRecoveryReport(
            SettingsRecoveryState.DECISION_REQUIRED,
            "The active settings are invalid and no completely valid settings-history "
            "snapshot is available. The file was left unchanged. "
            f"({type(error).__name__})",
            _settings_digest(current_bytes),len(current_bytes),
            "","",True,
        )
        return settings

    @classmethod
    def _from_payload(cls, payload: Any) -> "AppSettings":
        if not isinstance(payload,dict):
            raise TypeError("The settings root must be a JSON object.")
        _validate_settings_payload_shape(payload)

        gallery_page_size=max(12,min(96,int(payload.get("gallery_page_size",24))))
        gallery_card_width=int(payload.get("gallery_card_width",155))
        if gallery_page_size==24 and gallery_card_width==180:
            gallery_card_width=168
        gallery_card_width=max(112,min(240,gallery_card_width))
        joycaption_prompt=str(payload.get("joycaption_prompt") or DEFAULT_JOYCAPTION_PROMPT)
        if joycaption_prompt in {
            LEGACY_JOYCAPTION_PROMPT,PREVIOUS_DEFAULT_JOYCAPTION_PROMPT,
        }:
            joycaption_prompt=DEFAULT_JOYCAPTION_PROMPT
        joycaption_quantization=str(payload.get("joycaption_quantization","bf16"))
        joycaption_quantization_valid=joycaption_quantization in {"bf16","nf4"}
        if not joycaption_quantization_valid:
            joycaption_quantization="bf16"
        joycaption_quantization_explicit=bool(
            payload.get(
                "joycaption_quantization_explicit",
                "joycaption_quantization" in payload and joycaption_quantization_valid,
            )
        )
        plain_language_model=str(
            payload.get("plain_language_model") or JOYCAPTION_MODEL_ID
        )
        if plain_language_model not in PLAIN_LANGUAGE_MODEL_IDS:
            plain_language_model=JOYCAPTION_MODEL_ID
        onboarding_status=str(payload.get("onboarding_status") or "legacy")
        if onboarding_status not in {"not_started","in_progress","skipped","completed","legacy"}:
            onboarding_status="legacy"
        existing_blur_policy=bool(payload.get("gallery_hidden_ratings", [])) or str(
            payload.get("gallery_content_level","explicit")
        )!="explicit"
        settings = cls(
            dataset_directory=str(payload.get("dataset_directory", "")),
            ingest_directory=str(
                payload.get("ingest_directory") or payload.get("inbox_directory", "")
            ),
            working_directory=str(
                payload.get("working_directory") or payload.get("dataset_directory", "")
            ),
            library_profiles=[
                dict(value) for value in payload.get("library_profiles", [])
                if isinstance(value, dict)
            ],
            history_storage=deepcopy(payload.get("history_storage", {})),
            structured_history_storage=deepcopy(payload.get("structured_history_storage", {})),
            active_library_id=str(payload.get("active_library_id") or ""),
            library_scope_ids=[
                str(value) for value in payload.get("library_scope_ids",[])
                if str(value)
            ],
            library_move_naming_policy=normalize_library_naming_policy(
                payload.get("library_move_naming_policy")
            ),
            problems_directory=str(payload.get("problems_directory", "")),
            recycle_directory=str(payload.get("recycle_directory", "")),
            exiftool_path=str(payload.get("exiftool_path", "")),
            ai_models_directory=str(payload.get("ai_models_directory") or DATA_DIR / "models"),
            ai_runtime_mode=(str(payload.get("ai_runtime_mode","on_demand"))
                if payload.get("ai_runtime_mode","on_demand") in {"on_demand","keep_warm","preload"}
                else "on_demand"),
            ai_runtime_idle_minutes=max(1,min(120,int(payload.get("ai_runtime_idle_minutes",10)))),
            ai_runtime_memory_budget_mb=max(0,min(131072,int(payload.get("ai_runtime_memory_budget_mb",0)))),
            wd_general_threshold=float(payload.get("wd_general_threshold",0.35)),
            wd_character_threshold=float(payload.get("wd_character_threshold",0.85)),
            wd_rating_threshold=float(payload.get("wd_rating_threshold",0.35)),
            enabled_wd_taggers=[str(value) for value in payload.get("enabled_wd_taggers",["SmilingWolf/wd-eva02-large-tagger-v3"]) if str(value)],
            wd_consensus_minimum=max(1,int(payload.get("wd_consensus_minimum",1))),
            auto_merge_ai_tags=bool(payload.get("auto_merge_ai_tags",False)),
            auto_run_wd_on_managed_images=bool(payload.get("auto_run_wd_on_managed_images",False)),
            auto_run_joy_on_managed_images=bool(payload.get("auto_run_joy_on_managed_images",False)),
            wd_execution_provider=str(payload.get("wd_execution_provider","auto")) if str(payload.get("wd_execution_provider","auto")) in {"auto","cuda","cpu"} else "auto",
            wd_batch_size=max(1,min(16,int(payload.get("wd_batch_size",1)))),
            ai_model_benchmarks=dict(payload.get("ai_model_benchmarks",{})) if isinstance(payload.get("ai_model_benchmarks"),dict) else {},
            plain_language_model=plain_language_model,
            joycaption_prompt=joycaption_prompt,
            joycaption_max_new_tokens=normalized_plain_language_max_new_tokens(
                payload.get("joycaption_max_new_tokens",256)
            ),
            joycaption_min_new_tokens=int(payload.get("joycaption_min_new_tokens",30)),
            joycaption_num_beams=int(payload.get("joycaption_num_beams",4)),
            joycaption_length_penalty=float(payload.get("joycaption_length_penalty",1.0)),
            joycaption_do_sample=bool(payload.get("joycaption_do_sample",False)),
            joycaption_temperature=float(payload.get("joycaption_temperature",0.5)),
            joycaption_top_k=int(payload.get("joycaption_top_k",50)),
            joycaption_top_p=float(payload.get("joycaption_top_p",0.9)),
            joycaption_repetition_penalty=float(payload.get("joycaption_repetition_penalty",1.0)),
            joycaption_no_repeat_ngram_size=int(payload.get("joycaption_no_repeat_ngram_size",3)),
            joycaption_gpu_index=int(payload.get("joycaption_gpu_index",0)),
            joycaption_quantization=joycaption_quantization,
            joycaption_quantization_explicit=joycaption_quantization_explicit,
            suggest_wd_for_missing_tags=bool(payload.get("suggest_wd_for_missing_tags",True)),
            suggest_joycaption_for_missing_descriptions=bool(payload.get("suggest_joycaption_for_missing_descriptions",True)),
            prepare_read_external_evidence=bool(payload.get(
                "prepare_read_external_evidence",True,
            )),
            ingest_create_duplicate_fingerprints=bool(payload.get(
                "ingest_create_duplicate_fingerprints",True,
            )),
            ingest_check_duplicates=bool(payload.get("ingest_check_duplicates",False)),
            ingest_include_similar_duplicates=bool(payload.get("ingest_include_similar_duplicates",False)),
            ingest_duplicate_similarity_minimum=_duplicate_percentage(payload.get("ingest_duplicate_similarity_minimum"),95),
            ingest_duplicate_similarity_maximum=max(_duplicate_percentage(payload.get("ingest_duplicate_similarity_minimum"),95),_duplicate_percentage(payload.get("ingest_duplicate_similarity_maximum"),100)),
            prepare_ai_skip_value=max(1,min(999,int(payload.get("prepare_ai_skip_value",7)))),
            prepare_ai_skip_unit=(
                str(payload.get("prepare_ai_skip_unit","days"))
                if str(payload.get("prepare_ai_skip_unit","days")) in {"hours","days","months"}
                else "days"
            ),
            prepare_confidence_preset=(
                str(payload.get("prepare_confidence_preset","balanced"))
                if str(payload.get("prepare_confidence_preset","balanced"))
                in {"conservative","balanced","permissive","custom"}
                else "balanced"
            ),
            prepare_tag_vote_minimum=max(1,min(99,int(
                payload.get(
                    "prepare_tag_vote_minimum",
                    (
                        1 if str(payload.get("prepare_confidence_preset","balanced"))=="permissive"
                        else max(
                            3 if str(payload.get("prepare_confidence_preset","balanced"))=="conservative" else 2,
                            int(payload.get("wd_consensus_minimum",1)),
                        )
                    ),
                )
            ))),
            prepare_external_evidence_vote=bool(payload.get(
                "prepare_external_evidence_vote",
                str(payload.get("prepare_confidence_preset","balanced"))!="permissive",
            )),
            prepare_confirm_safe_tags=bool(payload.get(
                "prepare_confirm_safe_tags",True,
            )),
            prepare_preferred_descriptions=bool(payload.get(
                "prepare_preferred_descriptions",True,
            )),
            include_subfolders=bool(payload.get("include_subfolders", True)),
            show_tooltips=bool(payload.get("show_tooltips",True)),
            ui_theme=str(payload.get("ui_theme") or "system"),
            color_blind_mode=bool(payload.get("color_blind_mode",False)),
            organization_profile=dict(payload["organization_profile"])
            if isinstance(payload.get("organization_profile"),dict)
            else default_organization_profile(),
            restore_last_dataset=bool(payload.get("restore_last_dataset",True)),
            validate_library_on_startup=bool(payload.get("validate_library_on_startup",True)),
            check_dependencies_on_startup=bool(payload.get("check_dependencies_on_startup",True)),
            check_updates_on_startup=bool(payload.get("check_updates_on_startup",False)),
            update_channel=(
                str(payload.get("update_channel",DEFAULT_UPDATE_CHANNEL))
                if str(payload.get("update_channel",DEFAULT_UPDATE_CHANNEL)) in {"stable","beta"}
                else ("stable" if "update_channel" in payload else DEFAULT_UPDATE_CHANNEL)
            ),
            auto_import_source_evidence=bool(payload.get("auto_import_source_evidence",True)),
            gallery_page_size=gallery_page_size,
            gallery_card_width=gallery_card_width,
            resource_browser_views=deepcopy(payload.get("resource_browser_views", {})),
            gallery_content_level=str(payload.get("gallery_content_level","explicit")) if str(payload.get("gallery_content_level","explicit")) in {"general","sensitive","questionable","explicit"} else "explicit",
            gallery_hidden_ratings=[str(value) for value in payload.get("gallery_hidden_ratings", []) if str(value) in {"general","sensitive","questionable","explicit"}],
            gallery_blur_unknown=bool(payload.get("gallery_blur_unknown",existing_blur_policy)),
            ignored_plain_language_sentences=_clean_plain_language_exclusions(payload.get("ignored_plain_language_sentences",[])),
            remember_content_reveals=bool(payload.get("remember_content_reveals",True)),
            export_content_level=str(payload.get("export_content_level","explicit")) if str(payload.get("export_content_level","explicit")) in {"general","sensitive","questionable","explicit"} else "explicit",
            recent_directories=[str(value) for value in payload.get("recent_directories", []) if value],
            favorite_sdxl_tags=_clean_tag_convenience_values(payload.get("favorite_sdxl_tags", []),limit=100),
            recent_sdxl_tags=_clean_tag_convenience_values(payload.get("recent_sdxl_tags", []),limit=30),
            fact_expansion_enabled=bool(payload.get("fact_expansion_enabled",False)),
            fact_expansion_relationships=clean_fact_expansion_relationships(
                payload.get(
                    "fact_expansion_relationships",
                    list(FACT_EXPANSION_RELATIONSHIPS),
                )
            ),
            fact_expansion_max_depth=max(
                FACT_EXPANSION_MIN_DEPTH,
                min(
                    FACT_EXPANSION_MAX_DEPTH,
                    int(payload.get("fact_expansion_max_depth",FACT_EXPANSION_MIN_DEPTH)),
                ),
            ),
            external_write_backup_mode=normalize_external_write_backup_mode(
                payload.get("external_write_backup_mode","always")
            ),
            external_write_backup_directory=str(
                payload.get("external_write_backup_directory") or ""
            ),
            onboarding_status=onboarding_status,
            onboarding_version=max(0,int(payload.get("onboarding_version",0))),
            onboarding_step=str(payload.get("onboarding_step") or "welcome"),
            onboarding_completed_steps=[
                str(value) for value in payload.get("onboarding_completed_steps",[])
                if str(value).strip()
            ],
            onboarding_preset=str(payload.get("onboarding_preset") or "balanced")
            if str(payload.get("onboarding_preset") or "balanced") in {"conservative","balanced","hands_on"}
            else "balanced",
            onboarding_goal=str(payload.get("onboarding_goal") or "library")
            if str(payload.get("onboarding_goal") or "library") in {"library","ingest","explore"}
            else "library",
            onboarding_model_usage_choice=payload.get("onboarding_model_usage_choice"),
            last_ingest_attention_fingerprint=str(
                payload.get("last_ingest_attention_fingerprint") or ""
            )[:64],
            window_geometry=str(payload.get("window_geometry", "980x650")),
        )
        known_fields = {
            name for name in cls.__dataclass_fields__
            if name not in {"_extra","_load_report"}
        }
        settings._extra = {
            key: value
            for key, value in payload.items()
            if key not in known_fields and key != "inbox_directory"
        }
        settings._clean_recent_directories()
        settings._reconcile_library_profiles()
        from .managed_settings_protection import capture_baseline
        capture_baseline(settings)
        return settings

    @property
    def load_report(self) -> SettingsRecoveryReport:
        return self._load_report

    def save(
        self,
        path: Path = DEFAULT_SETTINGS_PATH,
        *,
        _allow_recovery_replacement: bool = False,
        _skip_history_snapshot: bool = False,
    ) -> None:
        if self._load_report.requires_decision and not _allow_recovery_replacement:
            raise SettingsRecoveryRequiredError(
                "Settings were loaded from safe in-memory defaults because the active "
                "file could not be accepted. Resolve the startup recovery before saving."
            )
        with _settings_writer(path):
            self._save_locked(path, _allow_recovery_replacement=_allow_recovery_replacement,
                _skip_history_snapshot=_skip_history_snapshot)

    def appearance_import_snapshot(self, path=DEFAULT_SETTINGS_PATH):
        """Read only the appearance destination and its bounded import markers."""
        from .history_configuration import MAX_SETTINGS_BYTES
        from .training_source_io import _assert_unredirected
        from .swarm_onboarding_receipts import digest, validate_markers
        path = Path(path)
        _assert_unredirected(path)
        if self._load_report.requires_decision:
            raise SettingsRecoveryRequiredError("Resolve settings recovery before importing appearance.")
        payload = {}
        if path.exists():
            with path.open("rb") as stream: raw = stream.read(MAX_SETTINGS_BYTES + 1)
            if len(raw) > MAX_SETTINGS_BYTES: raise ValueError("Settings exceed the supported size limit.")
            AppSettings._from_bytes(raw)
            payload = json.loads(raw)
        theme = payload.get("ui_theme", "system")
        if theme not in {"system", "dark", "light", "midnight"}:
            raise ValueError("Review unsupported appearance settings before importing.")
        value = {"theme": theme, "imports": validate_markers(payload.get("swarm_appearance_imports", []))}
        return {**value, "token": digest(value)}

    def import_appearance(self, theme, *, expected, marker, path=DEFAULT_SETTINGS_PATH):
        """Patch appearance only; never publish another editor's pending settings."""
        from .swarm_onboarding_receipts import digest, validate_marker, validate_markers
        from .history_configuration import MAX_SETTINGS_BYTES
        marker = validate_marker(marker)
        if (theme not in {"dark", "light"} or marker["after"] != theme or marker["category"] != "appearance"
                or marker["destination"] != "ui_theme" or marker["before"] != expected.get("theme")):
            raise ValueError("Review a supported source appearance before importing.")
        if self.ui_theme != expected.get("theme"):
            raise ValueError("Appearance changed in this editor; review the import again.")
        with _settings_writer(path):
            current = self.appearance_import_snapshot(path)
            if current != expected: raise ValueError("Appearance changed on disk; review the import again.")
            previous = next((row for row in current["imports"] if row["operation_id"] == marker["operation_id"]), None)
            if previous is not None:
                if previous != marker: raise ValueError("The appearance import marker changed.")
                if current["theme"] != theme: raise ValueError("A later appearance correction is preserved; make a fresh review.")
                return current
            if len(current["imports"]) >= 128: raise ValueError("Appearance import history needs review.")
            path = Path(path)
            if path.exists():
                with path.open("rb") as stream: raw = stream.read(MAX_SETTINGS_BYTES + 1)
                if len(raw) > MAX_SETTINGS_BYTES: raise ValueError("Settings exceed the supported size limit.")
                AppSettings._from_bytes(raw)
                payload = json.loads(raw)
                reread = {"theme": payload.get("ui_theme", "system"), "imports": validate_markers(payload.get("swarm_appearance_imports", []))}
                if digest(reread) != expected["token"]: raise ValueError("Appearance changed during publication review.")
                payload["ui_theme"] = theme
                payload["swarm_appearance_imports"] = current["imports"] + [marker]
                self._save_locked(path, _patch_payload=payload)
            else:
                # A new store starts from defaults, not this editor's pending fields.
                clean = AppSettings()
                clean.ui_theme = theme
                clean._extra["swarm_appearance_imports"] = current["imports"] + [marker]
                clean._save_locked(path)
            self.ui_theme = theme
            self._extra["swarm_appearance_imports"] = current["imports"] + [marker]
        return self.appearance_import_snapshot(path)

    def update_resource_browser_view(self, view_key, changes, *, expected, path=DEFAULT_SETTINGS_PATH):
        """Patch presentation preferences without publishing another editor's drafts."""
        from .resource_browser_view import update_view_preferences
        from .history_configuration import MAX_SETTINGS_BYTES
        if self._load_report.requires_decision:
            raise SettingsRecoveryRequiredError("Resolve settings recovery before saving browser preferences.")
        if self.resource_browser_views != expected:
            raise ValueError("Browser preferences changed elsewhere. Reopen the browser before saving its view.")
        selected = update_view_preferences(expected, view_key, changes)
        with _settings_writer(path):
            path = Path(path)
            if path.exists():
                with path.open("rb") as stream:
                    raw = stream.read(MAX_SETTINGS_BYTES + 1)
                if len(raw) > MAX_SETTINGS_BYTES:
                    raise ValueError("Settings exceed the supported size limit.")
                current = AppSettings._from_bytes(raw)
                if current.resource_browser_views != expected:
                    raise ValueError("Browser preferences changed elsewhere. Reopen the browser before saving its view.")
                payload = json.loads(raw.decode("utf-8"))
                payload["resource_browser_views"] = selected
                self._save_locked(path, _patch_payload=payload)
            else:
                previous = deepcopy(self.resource_browser_views)
                try:
                    self.resource_browser_views = selected
                    self._save_locked(path)
                except Exception:
                    self.resource_browser_views = previous
                    raise
            self.resource_browser_views = deepcopy(selected)
        return deepcopy(selected)

    def update_history_storage(self, value, *, expected, expected_sources, path=DEFAULT_SETTINGS_PATH, validate=None):
        """Compare and patch only history configuration in the current settings payload."""
        from .history_configuration import configuration, source_signature, validate_configuration
        if self._load_report.requires_decision:
            raise SettingsRecoveryRequiredError("Resolve settings recovery before saving history locations.")
        if self.history_storage != expected or source_signature(self) != expected_sources:
            raise ValueError("History locations or source folders changed elsewhere. Reload this group before saving.")
        with _settings_writer(path):
            path = Path(path)
            if not path.exists():
                raise ValueError("Save the configured source folders before registering history locations.")
            raw = path.read_bytes()
            current = AppSettings._from_bytes(raw)
            configuration(current.history_storage)  # unknown schemas cannot be replaced
            if current.history_storage != expected or source_signature(current) != expected_sources:
                raise ValueError("History locations or source folders changed elsewhere. Reload this group before saving.")
            selected = validate_configuration(value, current)
            if validate is not None: validate(selected, current)
            payload = json.loads(raw.decode("utf-8"))
            payload["history_storage"] = selected
            self._save_locked(path, _patch_payload=payload)
            self.history_storage = deepcopy(selected)

    def reload_history_storage(self, path=DEFAULT_SETTINGS_PATH):
        """Refresh this group's authority without replacing other in-memory drafts."""
        from .history_configuration import source_signature
        with _settings_writer(path):
            path = Path(path)
            if not path.exists() and not self.history_storage:
                return
            current = AppSettings._from_bytes(path.read_bytes())
            if source_signature(current) != source_signature(self):
                raise ValueError("Saved source folders changed in another process. Reopen CollectiGen before editing history locations.")
            self.history_storage = deepcopy(current.history_storage)

    def update_structured_history_storage(self, value, *, expected, expected_authority,
                                          path=DEFAULT_SETTINGS_PATH, authorities=None, validate=None):
        """Patch explicit global-history enrollment without replacing preference drafts."""
        from .structured_history_configuration import configuration, authority_signature, validate_configuration
        if self._load_report.requires_decision:
            raise SettingsRecoveryRequiredError("Resolve settings recovery before saving global-history locations.")
        if (self.structured_history_storage != expected
                or authority_signature(self, path, authorities) != expected_authority):
            raise ValueError("Global-history locations or authority changed. Reload this group before saving.")
        with _settings_writer(path):
            path = Path(path)
            if not path.exists():
                raise ValueError("Save the profile before explicitly registering global-history locations.")
            from .history_configuration import MAX_SETTINGS_BYTES
            with path.open("rb") as stream: raw = stream.read(MAX_SETTINGS_BYTES + 1)
            if len(raw) > MAX_SETTINGS_BYTES:
                raise ValueError("Settings exceed the supported global-history authority limit.")
            current = AppSettings._from_bytes(raw)
            configuration(current.structured_history_storage)
            if (current.structured_history_storage != expected
                    or authority_signature(current, path, authorities) != expected_authority):
                raise ValueError("Saved global-history locations or authority changed. Reload this group before saving.")
            selected = validate_configuration(value, current, settings_path=path)
            if validate is not None: validate(selected, current)
            payload = json.loads(raw.decode("utf-8"))
            payload["structured_history_storage"] = selected
            self._save_locked(path, _patch_payload=payload)

    def reload_structured_history_storage(self, path=DEFAULT_SETTINGS_PATH, *, authorities=None):
        from .structured_history_configuration import authority_signature
        from .history_configuration import MAX_SETTINGS_BYTES
        with _settings_writer(path):
            path = Path(path)
            if not path.exists() and not self.structured_history_storage: return
            with path.open("rb") as stream: raw = stream.read(MAX_SETTINGS_BYTES + 1)
            if len(raw) > MAX_SETTINGS_BYTES:
                raise ValueError("Settings exceed the supported global-history authority limit.")
            current = AppSettings._from_bytes(raw)
            if authority_signature(current, path, authorities) != authority_signature(self, path, authorities):
                raise ValueError("Saved source or storage authority changed. Reopen Setup before changing global history.")
            self.structured_history_storage = deepcopy(current.structured_history_storage)

    def _save_locked(self, path, *, _allow_recovery_replacement=False, _skip_history_snapshot=False, _patch_payload=None, _suite_managed_intent=False):
        path = Path(path)
        if self._load_report.requires_decision and not _allow_recovery_replacement:
            raise SettingsRecoveryRequiredError(
                "Settings were loaded from safe in-memory defaults because the active "
                "file could not be accepted. Resolve the startup recovery before saving."
            )
        # Managed intent rejection happens before history or canonical data writes.
        if path.exists() and not _allow_recovery_replacement:
            from .managed_settings_protection import preserve_managed_save
            from .managed_settings_protection import GROUPS
            preview = _patch_payload if _patch_payload is not None else {
                **self._extra,
                **{key: deepcopy(getattr(self, key)) for keys in
                   GROUPS.values() for key in keys},
            }
            preserve_managed_save(self, preview, json.loads(path.read_bytes().decode("utf-8")),
                explicit=_suite_managed_intent, raw_patch=_patch_payload is not None)
        self._reconcile_library_profiles()
        path.parent.mkdir(parents=True, exist_ok=True)
        previous_bytes = path.read_bytes() if path.exists() else None
        history: Path | None=None
        if previous_bytes is not None and not _skip_history_snapshot:
            history=path.parent/"settings-history"
            history.mkdir(parents=True,exist_ok=True)
            stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            snapshot=history/f"{stamp}_{uuid4().hex[:8]}.json"
            snapshot_temp=snapshot.with_suffix(".json.tmp")
            snapshot_temp.write_bytes(previous_bytes)
            snapshot_temp.replace(snapshot)
            if snapshot.read_bytes()!=previous_bytes:
                raise OSError("The previous settings snapshot failed read-back verification.")
        self._clean_recent_directories()
        payload: dict[str, Any] = {
            **self._extra,
            "dataset_directory": self.dataset_directory,
            "ingest_directory": self.ingest_directory,
            "working_directory": self.working_directory,
            "library_profiles": [dict(value) for value in self.library_profiles],
            "history_storage": deepcopy(self.history_storage),
            "structured_history_storage": deepcopy(self.structured_history_storage),
            "active_library_id": self.active_library_id,
            "library_scope_ids": list(self.library_scope_ids),
            "library_move_naming_policy": normalize_library_naming_policy(
                self.library_move_naming_policy
            ),
            "problems_directory": self.problems_directory,
            "recycle_directory": self.recycle_directory,
            "exiftool_path": self.exiftool_path,
            "ai_models_directory": self.ai_models_directory,
            "ai_runtime_mode": self.ai_runtime_mode if self.ai_runtime_mode in {"on_demand","keep_warm","preload"} else "on_demand",
            "ai_runtime_idle_minutes": max(1,min(120,int(self.ai_runtime_idle_minutes))),
            "ai_runtime_memory_budget_mb": max(0,min(131072,int(self.ai_runtime_memory_budget_mb))),
            "wd_general_threshold": self.wd_general_threshold,
            "wd_character_threshold": self.wd_character_threshold,
            "wd_rating_threshold": self.wd_rating_threshold,
            "enabled_wd_taggers": list(self.enabled_wd_taggers),
            "wd_consensus_minimum": self.wd_consensus_minimum,
            "auto_merge_ai_tags": self.auto_merge_ai_tags,
            "auto_run_wd_on_managed_images": self.auto_run_wd_on_managed_images,
            "auto_run_joy_on_managed_images": self.auto_run_joy_on_managed_images,
            "wd_execution_provider": self.wd_execution_provider,
            "wd_batch_size": self.wd_batch_size,
            "ai_model_benchmarks": dict(self.ai_model_benchmarks),
            "plain_language_model": self.plain_language_model,
            "joycaption_prompt": self.joycaption_prompt,
            "joycaption_max_new_tokens": normalized_plain_language_max_new_tokens(
                self.joycaption_max_new_tokens
            ),
            "joycaption_min_new_tokens": self.joycaption_min_new_tokens,
            "joycaption_num_beams": self.joycaption_num_beams,
            "joycaption_length_penalty": self.joycaption_length_penalty,
            "joycaption_do_sample": self.joycaption_do_sample,
            "joycaption_temperature": self.joycaption_temperature,
            "joycaption_top_k": self.joycaption_top_k,
            "joycaption_top_p": self.joycaption_top_p,
            "joycaption_repetition_penalty": self.joycaption_repetition_penalty,
            "joycaption_no_repeat_ngram_size": self.joycaption_no_repeat_ngram_size,
            "joycaption_gpu_index": self.joycaption_gpu_index,
            "joycaption_quantization": self.joycaption_quantization,
            "joycaption_quantization_explicit": bool(
                self.joycaption_quantization_explicit
            ),
            "suggest_wd_for_missing_tags": self.suggest_wd_for_missing_tags,
            "suggest_joycaption_for_missing_descriptions": self.suggest_joycaption_for_missing_descriptions,
            "prepare_read_external_evidence": bool(
                self.prepare_read_external_evidence
            ),
            "ingest_create_duplicate_fingerprints": bool(self.ingest_create_duplicate_fingerprints),
            "ingest_check_duplicates": bool(self.ingest_check_duplicates),
            "ingest_include_similar_duplicates": bool(self.ingest_include_similar_duplicates),
            "ingest_duplicate_similarity_minimum": _duplicate_percentage(self.ingest_duplicate_similarity_minimum,95),
            "ingest_duplicate_similarity_maximum": max(_duplicate_percentage(self.ingest_duplicate_similarity_minimum,95),_duplicate_percentage(self.ingest_duplicate_similarity_maximum,100)),
            "prepare_ai_skip_value": max(1,min(999,int(self.prepare_ai_skip_value))),
            "prepare_ai_skip_unit": self.prepare_ai_skip_unit if self.prepare_ai_skip_unit in {"hours","days","months"} else "days",
            "prepare_confidence_preset": (
                self.prepare_confidence_preset
                if self.prepare_confidence_preset
                in {"conservative","balanced","permissive","custom"}
                else "balanced"
            ),
            "prepare_tag_vote_minimum": max(
                1,min(99,int(self.prepare_tag_vote_minimum))
            ),
            "prepare_external_evidence_vote": bool(
                self.prepare_external_evidence_vote
            ),
            "prepare_confirm_safe_tags": bool(
                self.prepare_confirm_safe_tags
            ),
            "prepare_preferred_descriptions": bool(
                self.prepare_preferred_descriptions
            ),
            "include_subfolders": self.include_subfolders,
            "show_tooltips": self.show_tooltips,
            "ui_theme": self.ui_theme,
            "color_blind_mode": self.color_blind_mode,
            "organization_profile": dict(self.organization_profile),
            "restore_last_dataset": self.restore_last_dataset,
            "validate_library_on_startup": self.validate_library_on_startup,
            "check_dependencies_on_startup": self.check_dependencies_on_startup,
            "check_updates_on_startup": self.check_updates_on_startup,
            "update_channel": self.update_channel,
            "auto_import_source_evidence": self.auto_import_source_evidence,
            "gallery_page_size": self.gallery_page_size,
            "gallery_card_width": self.gallery_card_width,
            "resource_browser_views": deepcopy(self.resource_browser_views),
            "gallery_content_level": self.gallery_content_level,
            "gallery_hidden_ratings": list(self.gallery_hidden_ratings),
            "gallery_blur_unknown": self.gallery_blur_unknown,
            "ignored_plain_language_sentences": _clean_plain_language_exclusions(self.ignored_plain_language_sentences),
            "remember_content_reveals": self.remember_content_reveals,
            "export_content_level": self.export_content_level,
            "recent_directories": self.recent_directories,
            "favorite_sdxl_tags": _clean_tag_convenience_values(self.favorite_sdxl_tags,limit=100),
            "recent_sdxl_tags": _clean_tag_convenience_values(self.recent_sdxl_tags,limit=30),
            "fact_expansion_enabled": bool(self.fact_expansion_enabled),
            "fact_expansion_relationships": clean_fact_expansion_relationships(
                self.fact_expansion_relationships
            ),
            "fact_expansion_max_depth": max(
                FACT_EXPANSION_MIN_DEPTH,
                min(FACT_EXPANSION_MAX_DEPTH,int(self.fact_expansion_max_depth)),
            ),
            "external_write_backup_mode":normalize_external_write_backup_mode(
                self.external_write_backup_mode
            ),
            "external_write_backup_directory":str(
                self.external_write_backup_directory or ""
            ),
            "onboarding_status": self.onboarding_status,
            "onboarding_version": self.onboarding_version,
            "onboarding_step": self.onboarding_step,
            "onboarding_completed_steps": list(dict.fromkeys(self.onboarding_completed_steps)),
            "onboarding_preset": self.onboarding_preset,
            "onboarding_goal": self.onboarding_goal,
            "onboarding_model_usage_choice": self.onboarding_model_usage_choice,
            "last_ingest_attention_fingerprint": str(
                self.last_ingest_attention_fingerprint or ""
            )[:64],
            "window_geometry": self.window_geometry,
        }
        if _patch_payload is not None:
            payload = deepcopy(_patch_payload)
        elif previous_bytes is not None and not _allow_recovery_replacement:
            # Unrelated/stale preference editors never own history enrollment.
            current_payload = json.loads(previous_bytes.decode("utf-8"))
            payload["history_storage"] = deepcopy(current_payload.get("history_storage", {}))
            payload["structured_history_storage"] = deepcopy(current_payload.get("structured_history_storage", {}))
            payload["resource_browser_views"] = deepcopy(current_payload.get("resource_browser_views", {}))
            if "swarm_appearance_imports" in current_payload:
                payload["swarm_appearance_imports"] = deepcopy(current_payload["swarm_appearance_imports"])
            # Only the explicit grouped backend writer owns this extension.
            if "ollama_metadata_connection" in current_payload:
                payload["ollama_metadata_connection"] = deepcopy(current_payload["ollama_metadata_connection"])
            else:
                payload.pop("ollama_metadata_connection", None)
            # Explicit Tag connection confirmation owns this additive extension.
            if "tag_database_connection" in current_payload:
                payload["tag_database_connection"] = deepcopy(current_payload["tag_database_connection"])
            else:
                payload.pop("tag_database_connection", None)
        if previous_bytes is not None and not _allow_recovery_replacement:
            from .managed_settings_protection import preserve_managed_save
            payload = preserve_managed_save(self, payload, json.loads(previous_bytes.decode("utf-8")),
                explicit=_suite_managed_intent, raw_patch=_patch_payload is not None)
        elif not _suite_managed_intent and "suite_managed_settings" in payload:
            raise ValueError("Managed settings require an explicit initialized receiver transaction.")
        if _patch_payload is None and (payload.get("history_storage") or payload.get("structured_history_storage")):
            # A newly changed library/app role cannot take over retained history.
            from .history_configuration import history_roots, overlaps
            from .structured_history_configuration import structured_history_roots
            previous = json.loads(previous_bytes.decode("utf-8")) if previous_bytes else {}
            changed_roots = []
            for profile in payload["library_profiles"]:
                old = next((item for item in previous.get("library_profiles", []) if item.get("stable_id") == profile.get("stable_id")), {})
                if profile.get("root") and profile.get("root") != old.get("root"):
                    changed_roots.append(profile["root"])
            for key in ("ingest_directory", "problems_directory", "recycle_directory", "ai_models_directory", "external_write_backup_directory"):
                if payload.get(key) and payload.get(key) != previous.get(key):
                    changed_roots.append(payload[key])
            try:
                retained = history_roots(payload.get("history_storage", {})) + structured_history_roots(payload.get("structured_history_storage", {}))
            except ValueError:
                if changed_roots:
                    raise ValueError("History configuration is unsupported. Keep source and storage folders unchanged until it can be reviewed.")
                retained = ()
            if any(overlaps(root, store) for root in changed_roots for store in retained):
                raise ValueError("Keep library and storage folders separate from retained History & Versions storage.")
        temp = path.with_suffix(path.suffix + ".tmp")
        attempted_bytes = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            temp.write_bytes(attempted_bytes)
            temp.replace(path)
            verified = json.loads(path.read_text(encoding="utf-8"))
            if verified != payload:
                raise OSError("Settings read-back did not match the values written.")
        except Exception as exc:
            try:
                observed = path.read_bytes() if path.exists() else None
            except OSError as observed_exc:
                raise SettingsRecoveryRequiredError("Settings publication was interrupted and the current target cannot be verified. Recovery is required; the prior snapshot was retained.") from observed_exc
            if observed == previous_bytes:
                raise OSError("Settings publication was interrupted; the previous settings state remains unchanged.") from exc
            if observed != attempted_bytes:
                raise SettingsRecoveryRequiredError(
                    "Settings publication was interrupted by a changed target; recovery is required. "
                    "The current file and any exact prior snapshot were retained. Reload and review before saving again."
                ) from exc
            try:
                if previous_bytes is None:
                    path.unlink(missing_ok=True)
                else:
                    restore = path.with_suffix(path.suffix + ".restore.tmp")
                    restore.write_bytes(previous_bytes)
                    restore.replace(path)
                    if path.read_bytes() != previous_bytes:
                        raise OSError("Restored settings did not match their exact prior snapshot.")
            except Exception as restore_exc:
                raise OSError(
                    "Settings verification failed and the previous file could not be restored: "
                    f"{restore_exc}"
                ) from exc
            raise OSError(
                "Settings verification failed; the previous settings file was restored."
            ) from exc
        from .managed_settings_protection import accept_saved_managed
        accept_saved_managed(self, payload)
        self.history_storage = deepcopy(payload.get("history_storage", {}))
        self.structured_history_storage = deepcopy(payload.get("structured_history_storage", {}))
        self.resource_browser_views = deepcopy(payload.get("resource_browser_views", {}))
        if previous_bytes is not None and history is not None:
            try:
                snapshots=sorted(
                    history.glob("*.json"),
                    key=lambda value:(value.stat().st_mtime_ns,value.name),
                    reverse=True,
                )
                for expired in snapshots[SETTINGS_HISTORY_LIMIT:]:
                    expired.unlink()
            except OSError:
                # A retained extra recovery copy is safer than reporting a
                # successful settings write as failed after read-back passed.
                pass

    def use_safe_defaults(self, path: Path = DEFAULT_SETTINGS_PATH) -> SettingsRecoveryReport:
        """Explicitly replace one unchanged invalid settings file with safe defaults."""
        path=Path(path); report=self._load_report
        if not report.can_use_safe_defaults:
            raise SettingsRecoveryRequiredError(
                "Safe defaults cannot replace settings that were unavailable or have no "
                "verified invalid-file identity. Restore access and restart CollectiGen."
            )
        try:
            invalid_bytes=path.read_bytes()
        except OSError as exc:
            raise SettingsRecoveryRequiredError(
                f"The invalid settings can no longer be read: {exc}"
            ) from exc
        if (
            len(invalid_bytes)!=report.invalid_size
            or _settings_digest(invalid_bytes)!=report.invalid_sha256
        ):
            raise SettingsRecoveryRequiredError(
                "The settings file changed again after startup. It was left unchanged; "
                "restart CollectiGen to evaluate the current file."
            )

        recovery_dir=path.parent/"settings-recovery"
        quarantine,record=_write_settings_recovery_record(
            recovery_dir,
            invalid_bytes=invalid_bytes,
            status="prepared",
            recovered_bytes=b"",
            recovered_from=None,
        )
        if path.read_bytes()!=invalid_bytes:
            raise SettingsRecoveryRequiredError(
                "The settings file changed again while safe defaults were being prepared. "
                "It was left unchanged; restart CollectiGen to evaluate the current file."
            )
        try:
            safe=type(self)()
            safe.save(
                path,
                _allow_recovery_replacement=True,
                _skip_history_snapshot=True,
            )
            saved_bytes=path.read_bytes()
            verified=self._from_bytes(saved_bytes)
            if verified!=safe or verified._extra!=safe._extra:
                raise OSError("Safe-default settings failed semantic read-back verification.")
            # Refresh the recorded result hash now that the exact serialized
            # default payload is known.
            payload=json.loads(record.read_text(encoding="utf-8"))
            payload["recovered_sha256"]=_settings_digest(saved_bytes)
            payload["recovered_size"]=len(saved_bytes)
            raw=(json.dumps(payload,indent=2,ensure_ascii=False)+"\n").encode("utf-8")
            _atomic_replace_bytes(record,raw,label="settings-recovery-result")
            _finalize_settings_recovery_record(record,"safe_defaults")
        except Exception as exc:
            try:
                _atomic_replace_bytes(path,invalid_bytes,label="settings-defaults-rollback")
            except Exception as rollback_exc:
                raise OSError(
                    "Safe-default recovery failed and the invalid settings could not be "
                    f"restored exactly: {rollback_exc}"
                ) from exc
            raise OSError(
                "Safe-default recovery failed; the original invalid settings were restored."
            ) from exc
        _prune_settings_recovery(recovery_dir)
        self.__dict__.update(verified.__dict__)
        self._load_report=SettingsRecoveryReport(
            SettingsRecoveryState.SAFE_DEFAULTS,
            "Safe defaults were saved only after the exact invalid settings were "
            "preserved and verified.",
            report.invalid_sha256,report.invalid_size,str(quarantine),"safe_defaults",
        )
        return self._load_report

    def remember_directory(self, directory: str | Path) -> None:
        value = str(Path(directory).expanduser().resolve())
        self.dataset_directory = value
        self.recent_directories = [
            item for item in self.recent_directories
            if item.lower() != value.lower()
        ]
        self.recent_directories.insert(0, value)
        self._clean_recent_directories()

    def library_registry(self) -> LibraryProfileRegistry:
        """Return the recognized named libraries without probing their contents."""
        return LibraryProfileRegistry.from_dicts(self.library_profiles)

    def active_library_profile(self) -> LibraryProfile | None:
        return self.library_registry().get(self.active_library_id)

    def library_profile_for_path(self, path: str | Path) -> LibraryProfile | None:
        return self.library_registry().profile_for_path(path)

    def set_library_registry(
        self,
        registry: LibraryProfileRegistry,
        *,
        active_library_id: str | None = None,
    ) -> None:
        requested = self.active_library_id if active_library_id is None else str(active_library_id)
        active = registry.get(requested)
        if active is None and registry.profiles:
            active = registry.profiles[0]
        self.library_profiles = registry.to_dicts()
        self.active_library_id = active.stable_id if active is not None else ""
        valid_ids={profile.stable_id for profile in registry.profiles}
        self.library_scope_ids=[
            value for value in dict.fromkeys(self.library_scope_ids)
            if value in valid_ids
        ]
        self.working_directory = active.root if active is not None else ""
        if active is not None:
            self.remember_directory(active.root)

    def activate_library(self, stable_id: str) -> LibraryProfile:
        registry = self.library_registry()
        profile = registry.get(stable_id)
        if profile is None:
            raise KeyError(stable_id)
        issues = registry.validate(profile, replacing_id=profile.stable_id)
        if issues:
            raise ValueError(" ".join(issue.message for issue in issues))
        self.set_library_registry(registry, active_library_id=profile.stable_id)
        return profile

    def select_library_root(
        self, root: str | Path, *, preferred_name: str = "",
    ) -> LibraryProfile:
        """Activate an existing root or register a new Local Only root safely."""
        registry = self.library_registry()
        profile = registry.find_root(root)
        if profile is None:
            name = preferred_name or Path(root).name or "Working Library"
            profile = LibraryProfile.create(
                registry.unique_name(name),root,LibraryRole.LOCAL_ONLY,
            )
            registry.add(profile)
        self.set_library_registry(registry, active_library_id=profile.stable_id)
        return profile

    def _reconcile_library_profiles(self) -> None:
        """Preserve the legacy active-folder fields while profiles take ownership."""
        registry = self.library_registry()
        wanted = str(self.working_directory or self.dataset_directory or "").strip()
        active = registry.get(self.active_library_id)

        if wanted:
            matching = registry.find_root(wanted)
            if matching is not None:
                active = matching
            elif active is None or active.root != wanted:
                preferred = Path(wanted).name or "Working Library"
                profile = LibraryProfile.create(
                    registry.unique_name(preferred),wanted,LibraryRole.LOCAL_ONLY,
                )
                try:
                    registry.add(profile)
                except ValueError:
                    # A legacy overlapping path cannot be silently activated. Keep the
                    # existing named registry and let Setup surface the conflict.
                    profile = None
                if profile is not None:
                    active = profile

        if active is None and registry.profiles:
            active = registry.profiles[0]
        self.library_profiles = registry.to_dicts()
        self.active_library_id = active.stable_id if active is not None else ""
        valid_ids={profile.stable_id for profile in registry.profiles}
        self.library_scope_ids=[
            value for value in dict.fromkeys(self.library_scope_ids)
            if value in valid_ids
        ]
        if active is not None:
            self.working_directory = active.root

    def _clean_recent_directories(self) -> None:
        seen: set[str] = set()
        cleaned: list[str] = []
        for value in self.recent_directories:
            normalized = str(Path(value).expanduser())
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(normalized)
            if len(cleaned) >= 10:
                break
        self.recent_directories = cleaned


def _duplicate_percentage(value,default: int) -> int:
    try:return max(70,min(100,int(value)))
    except (TypeError,ValueError,OverflowError):return default
