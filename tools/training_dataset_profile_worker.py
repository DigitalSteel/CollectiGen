"""Contained shared package verifier; selected locations stay in memory."""
from __future__ import annotations

import hashlib
import importlib
import json
import mmap
from pathlib import Path
import re
import sys
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
package = ModuleType('_collectigen_dataset_profile')
package.__path__ = [str(ROOT / 'core')]
sys.modules[package.__name__] = package
selected = importlib.import_module(package.__name__ + '.training_dataset_preparation')


def _worker_site(executable):
    """Select the single software site implied by the admitted Windows layout."""
    executable = Path(executable)
    if not executable.is_absolute() or ".." in executable.parts or executable.name.casefold() != "python.exe":
        raise ValueError("An exact Windows worker Python executable is required.")
    folder = executable.parent
    prefix = folder.parent if folder.name.casefold() == "scripts" else folder
    site = prefix / "Lib" / "site-packages"
    source_io = importlib.import_module(package.__name__ + ".training_source_io")
    source_io._assert_unredirected(site)
    if not site.is_dir():
        raise ValueError("The reviewed worker Python package site is missing.")
    return site


def main():
    name, length, digest = sys.argv[1:]
    if not re.fullmatch(r'Local\\CollectiGen\.Preflight\.[0-9a-f]{32}', name) or not length.isdecimal() or not 0 < int(length) <= 256 * 1024 or not re.fullmatch(r'[0-9a-f]{64}', digest):
        return 2
    length = int(length)
    with mmap.mmap(-1, length + 4 + 512 * 1024 + 4 + 1024, tagname=name, access=mmap.ACCESS_WRITE) as memory:
        raw = memory[:length]
        request = json.loads(raw)
        if hashlib.sha256(raw).hexdigest() != digest or type(request) is not dict or set(request) != {'schema_version', 'nonce', 'operation', 'selection', 'summary', 'verification_bundle'} or request['schema_version'] != 1 or type(request['schema_version']) is not int or request['nonce'] != name.rsplit('.', 1)[1] or request['operation'] != 'dataset_admission' or selected.canonical(request) != raw:
            return 2
        selection = selected.validate_selection(request['selection'])
        if Path.cwd().is_relative_to(Path(selection['root'])):
            return 2
        bundle = request['verification_bundle']
        expected_path = Path.cwd().parents[1] / ('verification-code-' + request['nonce'] + '.zip')
        if type(bundle) is not dict or set(bundle) != {'path', 'sha256'} or bundle['path'] != str(expected_path) or selected.sha(expected_path.read_bytes()) != bundle['sha256']:
            return 2
        package.__path__ = [str(expected_path) + '/_collectigen_package_code']
        sys.path.insert(0, str(expected_path))
        # The full shared verifier requires Pillow, not the selected trainer.
        # Avoid the application initializer and forbid live profile/cache reads.
        site = _worker_site(sys.executable)
        sys.path.insert(0, str(site))
        sys.path.insert(0, str(ROOT / 'tools'))
        from training_runtime_probe import install_guard
        denials = install_guard(Path.cwd(), (site, sys.base_prefix, selection['root']),
            blocked_roots=(ROOT / 'core', ROOT / 'data/user', ROOT / 'data/cache'), read_files=(ROOT / 'version.py', expected_path))
        value = selected.profile_report(selected.binding(selection))
        try:
            service = importlib.import_module(package.__name__ + '.training_dataset_binding')
            candidate = service.inspect_package(selection)
            if not denials:
                value = candidate
        except BaseException:
            pass  # Never persist imported exceptions, captions or selected paths.
        reply = selected.canonical({'schema_version': 1, 'nonce': request['nonce'], 'request_sha256': digest,
            'status': 'complete', 'result': value})
        if len(reply) > 512 * 1024:
            return 2
        memory[length + 4:length + 4 + len(reply)] = reply
        memory[length:length + 4] = len(reply).to_bytes(4, 'little')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        raise SystemExit(2) from None
