"""Repository-wide pytest configuration."""

from __future__ import annotations

import os
from pathlib import Path
import shutil

import pytest


# Validate every original path chain before mkdir or application collection.
import sys
from tools.test_profile_isolation import (
    validated_profile, unredirected_directory, check_environment, check_bound_modules,
)
_app_root = Path(__file__).absolute().parent
_REPOSITORY_TEMP_ROOT = unredirected_directory(_app_root / "logs" / "pytest-temp")
_owned_root = os.environ.get("TAG_MANAGER_TEST_OWNED_ROOT", "").strip()
_PYTEST_PROFILE_ROOT, _expected_user, _expected_cache = validated_profile(
    _owned_root or _REPOSITORY_TEMP_ROOT/f"isolated-profile-{os.getpid()}",
    _app_root, must_exist=bool(_owned_root),
)
if _owned_root:
    check_environment(os.environ, _PYTEST_PROFILE_ROOT, _expected_user, _expected_cache)
check_bound_modules(_expected_user, _expected_cache)
if "PYTEST_DEBUG_TEMPROOT" not in os.environ:
    _REPOSITORY_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    unredirected_directory(_REPOSITORY_TEMP_ROOT, must_exist=True)
    os.environ["PYTEST_DEBUG_TEMPROOT"] = str(_REPOSITORY_TEMP_ROOT)
    os.environ["TAG_MANAGER_PYTEST_TEMP_USING_DEFAULT"] = "1"
else:
    unredirected_directory(os.environ["PYTEST_DEBUG_TEMPROOT"])
for variable, expected in (
    ("TAG_MANAGER_PYTEST_USER_DATA_DIR", _expected_user),
    ("TAG_MANAGER_UI_TEST_USER_DATA_DIR", _expected_user),
    ("TAG_MANAGER_PYTEST_CACHE_DIR", _expected_cache),
    ("TAG_MANAGER_UI_TEST_CACHE_DIR", _expected_cache),
):
    os.environ[variable] = str(expected)
os.environ["TAG_MANAGER_PYTEST"] = "1"
os.environ["TAG_MANAGER_UI_TEST"] = "1"

# QApplication chooses its platform backend once per process. Select the
# headless backend before test collection can import PySide6 so module order
# cannot make later widget geometry depend on whichever test created the first
# application instance.
os.environ["QT_QPA_PLATFORM"]="offscreen"


def pytest_sessionfinish(session, exitstatus) -> None:
    """Remove only the exact synthetic profile allocated to this pytest process."""
    del session,exitstatus
    try:
        target=_PYTEST_PROFILE_ROOT.resolve()
        parent=_REPOSITORY_TEMP_ROOT.resolve()
        if target.parent==parent and target.name.startswith("isolated-profile-"):
            shutil.rmtree(target,ignore_errors=True)
    except OSError:
        pass


@pytest.fixture
def qt_test_scope():
    """Provide opt-in ownership and restoration for a Qt widget test."""
    from tools.qt_test_support import QtTestScope

    with QtTestScope() as scope:
        yield scope
