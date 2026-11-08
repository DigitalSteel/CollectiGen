"""Private-memory, read-only input verifier, always owned by a Windows Job."""
from __future__ import annotations

import hashlib
import importlib
import json
import mmap
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace


CORE = Path(__file__).resolve().parents[1] / 'core'
SOURCE_LOADER_HASHES = ('b6c91ee1eaac1ea1a70800723e126452f5545efc8b6789ccc2f2a6a3f7bcc1c4', '15ebc6afbf81d5c11f8859d1f8358588f59d80e463c590e4d95373e26724fb0e')
_SOURCE_LOADER = None


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':')).encode('utf-8')


def source_loader():
    """Compile the initial helper from its reviewed bytes, never its cache."""
    global _SOURCE_LOADER
    if _SOURCE_LOADER is None:
        path = CORE / 'training_source_loader.py'
        with path.open('rb') as handle:
            raw = handle.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024 or hashlib.sha256(raw).hexdigest() not in SOURCE_LOADER_HASHES:
            raise ValueError('training_execution_source_loader_changed')
        name = '_collectigen_source_loader_' + hashlib.sha256(__name__.encode()).hexdigest()[:16]
        if name in sys.modules:
            raise ValueError('training_execution_source_loader_occupied')
        module = ModuleType(name)
        module.__file__ = str(path)
        sys.modules[name] = module
        try:
            exec(compile(raw, str(path), 'exec', dont_inherit=True), module.__dict__)
        except BaseException:
            del sys.modules[name]
            raise
        _SOURCE_LOADER = module
    return _SOURCE_LOADER


def main():
    mapping_name, length, expected_hash = sys.argv[1:]
    if not re.fullmatch(r"Local\\CollectiGen\.Verify\.[0-9a-f]{32}", mapping_name) or not length.isdecimal() or not 0 < int(length) <= 256 * 1024 or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        return 2
    with mmap.mmap(-1, int(length), tagname=mapping_name, access=mmap.ACCESS_READ) as mapping:
        raw = mapping[:]
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        return 2
    value = json.loads(raw)
    if type(value) is not dict or set(value) != {"schema_version", "nonce", "phase", "process_fingerprint", "inputs", "inventories"} or type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["nonce"] != mapping_name.rsplit(".", 1)[1] or value["phase"] not in {"before-create", "before-resume", "after-exit"} or not re.fullmatch(r"[0-9a-f]{64}", value["process_fingerprint"]) or canonical(value) != raw:
        return 2
    if type(value["inputs"]) is not list or not 1 <= len(value["inputs"]) <= 128 or type(value["inventories"]) is not list or len(value["inventories"]) > 4:
        return 2
    loader = source_loader()
    package = loader.worker_package('_collectigen_verifier', CORE, value['inputs'], 'verifier')
    process = package.import_module('training_process')
    report = {key: value[key] for key in ("schema_version", "nonce", "phase", "process_fingerprint")}
    report.update(request_sha256=expected_hash, status="held")
    try:
        plan = SimpleNamespace(inputs=tuple(process.ProcessInput(row["path"], tuple(row["state"]), row["sha256"]) for row in value["inputs"]),
            inventories=tuple(process.ProcessInventory(**row) for row in value["inventories"]))
        process.TrainingProcessService._verify_inputs(plan)
        report["status"] = "verified"
    except BaseException:
        pass  # Never expose selected paths or arbitrary I/O exception text.
    reply = process._bytes(report)
    if len(reply) > 4096:
        return 2
    with mmap.mmap(-1, int(length) + 4 + 4096, tagname=mapping_name, access=mmap.ACCESS_WRITE) as mapping:
        mapping[int(length) + 4:int(length) + 4 + len(reply)] = reply
        mapping[int(length):int(length) + 4] = len(reply).to_bytes(4, "little")
    return 0 if report["status"] == "verified" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        raise SystemExit(2) from None
