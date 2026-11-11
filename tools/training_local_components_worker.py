"""Isolated local-input reader; no trainer, tokenizer or model imports."""
from __future__ import annotations

import importlib
import mmap
from pathlib import Path
import re
import sys
from types import ModuleType

package = ModuleType('_collectigen_components')
package.__path__ = [str(Path(__file__).resolve().parents[1] / 'core')]
sys.modules[package.__name__] = package
components = importlib.import_module(package.__name__ + '.training_local_components')
preflight = importlib.import_module(package.__name__ + '.training_preflight')
process = importlib.import_module(package.__name__ + '.training_process')


def main():
    name, length, digest = sys.argv[1:]
    if not re.fullmatch(r'Local\\CollectiGen\.Preflight\.[0-9a-f]{32}', name) or not length.isdecimal() or not 0 < int(length) <= preflight.MAX_REQUEST or not preflight.SHA.fullmatch(digest):
        return 2
    size = int(length)
    with mmap.mmap(-1, size + 4 + preflight.MAX_RESPONSE + 4 + preflight.MAX_PROGRESS, tagname=name, access=mmap.ACCESS_WRITE) as mapping:
        raw = mapping[:size]
        if process._sha(raw) != digest:
            return 2
        request = preflight.runtime._json(raw)
        if type(request) is not dict or set(request) != {'schema_version', 'nonce', 'operation', 'selection', 'expected_binding'} or process._bytes(request) != raw or type(request['schema_version']) is not int or request['schema_version'] != 1 or request['nonce'] != name.rsplit('.', 1)[1] or request['operation'] not in {'components', 'model_layout', 'component_profile'}:
            return 2
        selection = components.LocalComponentSelection.from_dict(request['selection'])
        if any(Path.cwd().is_relative_to(path) for path in selection.roots()):
            return 2
        report = {'schema_version': 1, 'nonce': request['nonce'], 'request_sha256': digest, 'status': 'held', 'result': None}
        def progress(done, total, message):
            raw_progress = process._bytes({'phase': 'bytes', 'done': done, 'total': total})
            offset = size + 4 + preflight.MAX_RESPONSE
            mapping[offset:offset + 4] = b'\0' * 4
            mapping[offset + 4:offset + 4 + len(raw_progress)] = raw_progress
            mapping[offset:offset + 4] = len(raw_progress).to_bytes(4, 'little')
        try:
            if request['operation'] == 'component_profile':
                profile = importlib.import_module(package.__name__ + '.training_component_profile')
                result = profile.inspect_component_profile(selection, expected_binding=request['expected_binding'], progress=progress)
            elif request['operation'] == 'model_layout':
                layout = importlib.import_module(package.__name__ + '.training_model_layout')
                result = layout.inspect_model_layout(selection, expected_binding=request['expected_binding'], progress=progress)
            else:
                result = components.inspect_local_components(selection, expected_binding=request['expected_binding'], progress=progress)
                components.validate_report(result, selection, request['expected_binding'])
            report.update(status='complete', result=result)
        except BaseException:
            pass  # Never emit selected paths, embedded strings or exceptions.
        reply = process._bytes(report)
        if len(reply) > preflight.MAX_RESPONSE:
            return 2
        mapping[size + 4:size + 4 + len(reply)] = reply
        mapping[size:size + 4] = len(reply).to_bytes(4, 'little')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        raise SystemExit(2) from None
