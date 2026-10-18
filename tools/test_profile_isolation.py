"""Standard-library-only guards; importing this module performs no writes."""
from pathlib import Path
import inspect
import sys
from core.profile_paths import unredirected_directory

VARIABLES = ("TAG_MANAGER_PYTEST_USER_DATA_DIR", "TAG_MANAGER_UI_TEST_USER_DATA_DIR",
             "TAG_MANAGER_PYTEST_CACHE_DIR", "TAG_MANAGER_UI_TEST_CACHE_DIR")

def validated_profile(root, app_root, *, must_exist=False):
    root = unredirected_directory(root, must_exist=must_exist)
    app = unredirected_directory(app_root, must_exist=True)
    live = unredirected_directory(app / "data")
    user = unredirected_directory(root / "user")
    cache = unredirected_directory(root / "cache")
    for target in (root, user, cache):
        if target == live or target in live.parents or live in target.parents:
            raise RuntimeError("Test profile overlaps application data")
    for target in (user, cache):
        if target.parent != root:
            raise RuntimeError("Writable child escapes owned test root")
    return root, user, cache

def check_environment(env, root, user, cache):
    if env.get("TAG_MANAGER_TEST_OWNED_ROOT") != str(root):
        raise RuntimeError("Owned root environment mismatch")
    if env.get("TAG_MANAGER_PYTEST") != "1" or env.get("TAG_MANAGER_UI_TEST") != "1":
        raise RuntimeError("Isolation mode missing")
    for name in VARIABLES:
        if env.get(name) != str(cache if "CACHE" in name else user):
            raise RuntimeError("Writable environment mismatch")

def check_bound_modules(user, cache):
    paths = sys.modules.get("core.paths")
    if paths is not None:
        for name, expected in (("USER_DATA_DIR", user), ("CACHE_DIR", cache),
                               ("SETTINGS_PATH", user / "settings.json")):
            if Path(getattr(paths, name)).absolute() != expected:
                raise RuntimeError("Application path bound outside owned profile")
    settings = sys.modules.get("core.settings")
    if settings is not None:
        expected = user / "settings.json"
        if Path(settings.DEFAULT_SETTINGS_PATH).absolute() != expected:
            raise RuntimeError("Settings constant bound outside owned profile")
        for method in (settings.AppSettings.load, settings.AppSettings.save):
            if Path(inspect.signature(method).parameters["path"].default).absolute() != expected:
                raise RuntimeError("Settings method default bound outside owned profile")
