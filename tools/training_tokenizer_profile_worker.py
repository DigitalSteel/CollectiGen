"""Project-interpreter helper for exact local CLIP profile checks; no ML imports."""
from __future__ import annotations

import hashlib
import importlib
import mmap
from pathlib import Path
import re
import sys
from types import ModuleType

package = ModuleType('_collectigen_tokenizer_profile')
package.__path__ = [str(Path(__file__).resolve().parents[1] / 'core')]
sys.modules[package.__name__] = package
# No application initializer or registry is needed in this worker.
selected = importlib.import_module(package.__name__ + '.training_selected_tokenizers')


def main():
    name, length, digest = sys.argv[1:]
    if not re.fullmatch(r'Local\\CollectiGen\.Preflight\.[0-9a-f]{32}', name) or not length.isdecimal() or not 0 < int(length) <= 256 * 1024 or not re.fullmatch(r'[0-9a-f]{64}', digest):
        return 2
    length = int(length)
    with mmap.mmap(-1, length + 4 + 512 * 1024 + 4 + 1024, tagname=name, access=mmap.ACCESS_WRITE) as memory:
        raw = memory[:length]
        if hashlib.sha256(raw).hexdigest() != digest:
            return 2
        request = selected.strict_json(raw)
        if type(request) is not dict or set(request) != {'schema_version', 'nonce', 'operation', 'selection'} or type(request['schema_version']) is not int or request['schema_version'] != 1 or request['nonce'] != name.rsplit('.', 1)[1] or request['operation'] != 'tokenizer_profile' or selected.canonical(request) != raw:
            return 2
        selection = selected.validate_selection(request['selection'])
        if any(Path.cwd().is_relative_to(Path(row['path'])) for row in selection['slots']):
            return 2
        reason = 'profile_checked'
        try:
            selected.inspect_selection(selection)
        except BaseException as exc:
            allowed = {'unsupported_tokenizer_configuration', 'unsupported_tokenizer_special_ids', 'selected_tokenizer_changed'}
            reason = str(exc) if type(exc) is ValueError and str(exc) in allowed else 'selected_tokenizer_unavailable_or_invalid'
        value = {'schema_version': 1, 'kind': 'sdxl_tokenizer_profile_check',
            'status': 'profile_checked' if reason == 'profile_checked' else 'held',
            'profile': selected.PROFILE, 'reason': reason, 'binding': selected.binding(selection),
            'tokenizer_behavior_verified': False, 'model_compatibility_verified': False,
            'offline_execution_verified': False, 'execution_ready': False}
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
