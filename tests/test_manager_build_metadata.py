"""Synthetic frozen-tree metadata producer; no interpreter,freezer or app execution."""
from copy import deepcopy
import json
import pytest
from tools import build_manager_distribution as build
from suite_core.manager.current_build import load_current_build


def frozen(tmp_path):
    folder = tmp_path / "frozen"
    (folder / "_internal/suite_core/manager").mkdir(parents=True)
    (folder / "CollectiGenManager.exe").write_bytes(b"synthetic-not-an-executable")
    (folder / "_internal/python314.dll").write_bytes(b"synthetic-runtime")
    return folder


def profile(): return {"python": {"version": "3.14.7"}, "distributions": {"PySide6-Essentials": "6.11.2"}}


def test_producer_records_exact_pre_metadata_inventory_and_parser_loads_it(tmp_path):
    folder = frozen(tmp_path); original = build.tree_inventory(folder)
    receipt = build.write_current_build_metadata(folder, software_version="0.7.5-alpha.1",
        source_plan_sha256="a"*64, tool_profile=profile())
    resource = folder / receipt["resource"]; raw = resource.read_bytes()
    assert build.digest(raw) == receipt["sha256"] and len(raw) == receipt["bytes"]
    assert receipt["runtime_inventory"] == original
    assert receipt["metadata"]["provenance"]["runtime_inventory_sha256"] == build.digest(build.json_bytes(original))
    actual = build.tree_inventory(folder); actual.pop(receipt["resource"])
    assert actual == original
    status = load_current_build(resource.parent, "0.7.5-alpha.1")
    assert status.kind == "portable-runtime" and (status.python,status.qt) == ("3.14.7","6.11.2")
    assert status.unsigned is True and status.clean_machine_qualified is False
    text = " ".join(status.lines())
    assert "Python 3.14.7 and Qt 6.11.2 included" in text and "not Windows code-signed" in text
    assert "Unsigned developer build" not in text and "clean computer is not established" in text
    assert load_current_build(resource.parent, "0.7.4-alpha.1").kind == "unknown"


def test_producer_refuses_to_replace_existing_resource(tmp_path):
    folder = frozen(tmp_path)
    args = dict(software_version="0.7.5-alpha.1", source_plan_sha256="a"*64, tool_profile=profile())
    receipt = build.write_current_build_metadata(folder, **args)
    path = folder / receipt["resource"]; raw = path.read_bytes()
    with pytest.raises(build.BuildHold): build.write_current_build_metadata(folder, **args)
    assert path.read_bytes() == raw


@pytest.mark.parametrize("change", ["source", "python", "qt", "version"])
def test_invalid_inputs_never_publish_metadata(tmp_path, change):
    folder = frozen(tmp_path); values = profile(); pin="a"*64; version="0.7.5-alpha.1"
    if change == "source": pin="not-a-hash"
    elif change == "python": values["python"]["version"]="unknown"
    elif change == "qt": values["distributions"].pop("PySide6-Essentials")
    else: version="not-a-version"
    with pytest.raises(build.BuildHold):
        build.write_current_build_metadata(folder, software_version=version, source_plan_sha256=pin, tool_profile=values)
    assert not (folder / "_internal/suite_core/manager/CURRENT_BUILD.json").exists()
