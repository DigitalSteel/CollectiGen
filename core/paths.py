from __future__ import annotations

import os
from pathlib import Path


PROGRAM_ROOT = Path(__file__).resolve().parents[1]
from .application_profile import selected_profile_root
APP_ROOT = selected_profile_root() or PROGRAM_ROOT
DATA_DIR = APP_ROOT / "data"
DEFAULTS_DIR = DATA_DIR / "defaults"


def _ui_test_path(variable: str, fallback: Path) -> Path:
    """Allow the visual harness to isolate writable state, never production runs."""
    value=os.environ.get(variable,"").strip()
    if os.environ.get("TAG_MANAGER_UI_TEST")!="1" or not value:
        return fallback
    return Path(value).expanduser().resolve()


def _pytest_path(variable: str, fallback: Path) -> Path:
    """Keep repository tests out of the live development profile."""
    value=os.environ.get(variable,"").strip()
    if os.environ.get("TAG_MANAGER_PYTEST")!="1" or not value:
        return fallback
    return Path(value).expanduser().resolve()


def _isolated_path(ui_variable: str, pytest_variable: str, fallback: Path) -> Path:
    ui_path=_ui_test_path(ui_variable,fallback)
    if ui_path!=fallback:
        return ui_path
    return _pytest_path(pytest_variable,fallback)


USER_DATA_DIR = _isolated_path(
    "TAG_MANAGER_UI_TEST_USER_DATA_DIR",
    "TAG_MANAGER_PYTEST_USER_DATA_DIR",
    DATA_DIR / "user",
)
CACHE_DIR = _isolated_path(
    "TAG_MANAGER_UI_TEST_CACHE_DIR",
    "TAG_MANAGER_PYTEST_CACHE_DIR",
    DATA_DIR / "cache",
)

SETTINGS_PATH = USER_DATA_DIR / "settings.json"
TAGS_PATH = USER_DATA_DIR / "tags.json"
TASKS_PATH = USER_DATA_DIR / "tasks.json"
DISCOVERED_TAGS_PATH = USER_DATA_DIR / "discovered_tags.json"
CHARACTERS_PATH = USER_DATA_DIR / "characters.json"
SERIES_PATH = USER_DATA_DIR / "series.json"
CATEGORIES_PATH = USER_DATA_DIR / "categories.json"
PATTERNS_PATH = USER_DATA_DIR / "patterns.json"
MISSING_TAG_EXCEPTIONS_PATH = USER_DATA_DIR / "missing_tag_exceptions.json"
AI_WORK_LEASE_PATH = USER_DATA_DIR / "active_ai_work.json"
SETUP_OPERATION_QUEUE_PATH = USER_DATA_DIR / "setup_operation_queue.json"
EXPORT_PROFILES_PATH = USER_DATA_DIR / "export_profiles.json"
TRAINING_PLAN_DRAFTS_PATH = USER_DATA_DIR / "training_plan_drafts.json"
MODEL_RESOURCES_PATH = USER_DATA_DIR / "model_resources.json"
SWARM_RUNTIME_PATH = USER_DATA_DIR / "swarm_runtime.json"
RELATIONSHIPS_PATH = USER_DATA_DIR / "relationships.json"
CONSUMER_POLICIES_PATH = USER_DATA_DIR / "consumer_policies.json"
TAG_MEANING_EVIDENCE_PATH = USER_DATA_DIR / "tag_meaning_evidence.json"


def ensure_user_data_dir() -> Path:
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return USER_DATA_DIR
