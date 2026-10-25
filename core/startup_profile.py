"""Bind startup writes to the already admitted synthetic profile.

Ordinary application/deployment authority is unchanged. Test startup requires
the migration service's existing root/data/{user,cache} layout; ambiguous layouts
fail before recovery, logging, migrations or update health can touch disk.
"""
from pathlib import Path
import os


def startup_write_root(application_root: str | Path) -> Path:
    application_root = Path(application_root).resolve()
    if not any(os.environ.get(name) == "1" for name in
               ("TAG_MANAGER_UI_TEST", "TAG_MANAGER_PYTEST")):
        return application_root
    from core.paths import USER_DATA_DIR, CACHE_DIR
    from tools.test_profile_isolation import check_environment, unredirected_directory

    user = unredirected_directory(USER_DATA_DIR)
    cache = unredirected_directory(CACHE_DIR)
    if user.name != "user" or cache.name != "cache" or user.parent != cache.parent:
        raise RuntimeError("Synthetic startup requires sibling user/cache directories")
    if user.parent.name != "data":
        raise RuntimeError("Synthetic startup requires an owned root/data profile layout")
    root = unredirected_directory(user.parent.parent, must_exist=True)
    live = application_root / "data"
    if root == application_root or root == live or live in root.parents or root in live.parents:
        raise RuntimeError("Synthetic startup overlaps application data")
    check_environment(os.environ, user.parent, user, cache)
    return root
