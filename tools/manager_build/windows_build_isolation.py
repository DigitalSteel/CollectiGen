"""Builder-only Windows dependency isolation; does not import or launch either app."""
import os
from pathlib import Path, PureWindowsPath
import sys


def local_graph_dependencies(graph, local_top_level):
    """Read PyInstaller's warning TOC and normalize its quoted identifiers."""
    missing, excluded = set(), set()
    for raw_name, origin, kind in graph.make_missing_toc():
        name = raw_name
        if len(name) >= 2 and name[0] in "'\"" and name[-1] == name[0]:
            name = name[1:-1]
        if name.partition('.')[0] not in local_top_level:
            continue
        node = graph.find_node(raw_name)
        target = excluded if type(node).__name__ == 'ExcludedModule' else missing
        target.add(name)
    return sorted(missing), sorted(excluded)


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
