"""Trusted contained software copier; never imports the staged trainer."""
from __future__ import annotations
import importlib
import mmap
from pathlib import Path
import re
import sys
from types import ModuleType

package = ModuleType('_collectigen_restricted_stage')
package.__path__ = [str(Path(__file__).resolve().parents[1]/'core')]
sys.modules[package.__name__] = package
preflight = importlib.import_module(package.__name__+'.training_preflight')
staging = importlib.import_module(package.__name__+'.training_runtime_staging')
process = importlib.import_module(package.__name__+'.training_process')


def main():
    name,length,digest = sys.argv[1:]
    if not re.fullmatch(r'Local\\CollectiGen\.Preflight\.[0-9a-f]{32}',name) or not length.isdecimal() or not 0 < int(length) <= preflight.MAX_REQUEST:
        return 2
    size = int(length)
    with mmap.mmap(-1,size+4+preflight.MAX_RESPONSE+4+preflight.MAX_PROGRESS,tagname=name,access=mmap.ACCESS_WRITE) as memory:
        raw = memory[:size]
        if process._sha(raw) != digest: return 2
        request = preflight.runtime._json(raw)
        staging.validate_stage_request(request)
        if process._bytes(request) != raw or request['nonce'] != name.rsplit('.',1)[1]:
            return 2
        response = {'schema_version':1,'nonce':request['nonce'],'request_sha256':digest,'status':'held','result':None}
        def progress(done,total,message):
            payload = process._bytes({'phase':'bytes','done':done,'total':total})
            offset = size+4+preflight.MAX_RESPONSE
            memory[offset:offset+4] = b'\0'*4
            memory[offset+4:offset+4+len(payload)] = payload
            memory[offset:offset+4] = len(payload).to_bytes(4,'little')
        try:
            if request['operation'] == 'restricted_stage': result = staging.stage(request,progress=progress)
            elif request['operation'] == 'restricted_recheck': result = staging.recheck(request,progress=progress)
            else: return 2
            response.update(status='complete',result=result)
        except BaseException:
            pass  # No paths, embedded strings or selected-software exception text.
        reply = process._bytes(response)
        if len(reply) > preflight.MAX_RESPONSE: return 2
        memory[size+4:size+4+len(reply)] = reply
        memory[size:size+4] = len(reply).to_bytes(4,'little')
    return 0


if __name__ == '__main__':
    try: raise SystemExit(main())
    except SystemExit: raise
    except BaseException: raise SystemExit(2) from None
