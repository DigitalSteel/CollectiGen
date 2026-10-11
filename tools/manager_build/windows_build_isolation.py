"""Builder-only Windows dependency isolation; does not import or launch either app."""
import os
from pathlib import Path, PureWindowsPath
import sys


def build_environment(output):
    if sys.platform != "win32":
        return None  # Preserve independently qualified Linux recipe.
    system = Path(os.environ["SystemRoot"]).resolve()
    environment = {key: os.environ[key] for key in
                   ("SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA")
                   if key in os.environ}
    # Discard inherited native-tool/Qt/Python injection paths. Wheels are located
    # through this interpreter; PyInstaller's Qt hooks add their own wheel dirs.
    environment["PATH"] = os.pathsep.join(str(path) for path in
        (system / "System32", system, Path(sys.executable).resolve().parent, Path(sys.base_prefix).resolve()))
    environment["PYINSTALLER_CONFIG_DIR"] = str(Path(output).resolve() / "isolated-pyinstaller-cache")
    return environment


def validate_binary_origins(binaries, *, roots=None):
    if sys.platform != "win32":
        return
    approved = tuple(Path(path).resolve() for path in (roots if roots is not None else
                     (sys.prefix, sys.base_prefix, os.environ["SystemRoot"])))
    for destination, source, kind in binaries:
        if kind not in ("BINARY", "EXTENSION"):
            continue
        source_path = Path(source)
        if not source_path.is_file() or source_path.is_symlink() or not any(source_path.resolve().is_relative_to(root) for root in approved):
            raise ValueError("unapproved_binary_origin: " + destination)
        # This wheel uses the Windows system ICU API. Do not shadow it with a
        # similarly named third-party ICU. No blanket stripping of dependencies.
        if source_path.name.casefold() == "icuuc.dll" or PureWindowsPath(destination).name.casefold() == "icuuc.dll":
            raise ValueError("windows_system_icu_must_not_be_bundled")
