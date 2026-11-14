"""Filename-driven Training workers must survive the component and frozen export."""
from pathlib import Path
import hashlib,json,os
from core import training_source_loader
from core.training_anima import WORKER

ROOT=Path(os.environ.get("COLLECTIGEN_MANAGER_SOURCE_ROOT",Path(__file__).resolve().parents[1]))

def required_sources():
    names=set().union(*training_source_loader.WORKER_MODULES.values())
    return {"core/"+name+".py" for name in names}|{"core/training_anima_worker.py"}

def test_component_exports_retain_fixed_training_source_closure():
    required=required_sources()
    for manifest in ["tools/component-source-manifest.json","tools/public-source-manifest.json"]:
        files=set(json.loads((ROOT/manifest).read_bytes())["files"])
        assert required<=files,sorted(required-files)

def test_frozen_training_sources_match_the_reviewed_public_bytes():
    rows={r["path"]:r for r in json.loads((ROOT/"tools/manager_build/source-manifest.json").read_bytes())["source_rows"]}
    public=json.loads((ROOT/"public-source.json").read_bytes())["files"]
    required=required_sources()
    declared=set(json.loads((ROOT/"suite_core/manager/MODULE_MANIFEST.json").read_bytes())["runtime_files"])
    assert required<=declared,sorted(required-declared)
    assert required<=rows.keys(),sorted(required-rows.keys())
    for name in required:
        data=(ROOT/name).read_bytes();digest=hashlib.sha256(data).hexdigest()
        assert rows[name]["sha256"]==digest==public[name]["sha256"]
        assert rows[name]["bytes"]==len(data)==public[name]["size_bytes"]

def test_reachable_anima_service_worker_uses_the_shipped_source_path():
    assert WORKER.resolve()==(ROOT/"core/training_anima_worker.py").resolve()
    assert WORKER.is_file()
    assert WORKER.relative_to(ROOT).as_posix() in required_sources()
