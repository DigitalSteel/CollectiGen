"""Independent sealed source identities; no freezer, interpreter or app runs."""
import json
from pathlib import Path

import pytest

from tools import build_manager_distribution as build
from tools.prepare_manager_production_stage import prepare_public_stage


def stage_input(tmp_path, component):
    source = tmp_path / 'export'
    paths = [f'suite_core/{component}/__main__.py',
             'suite_core/workflow_modules/entry.py', 'suite_core/workflow_modules/session.py']
    files = {name: b'# generated inert source; no program startup\n' for name in paths}
    manifest_name = f'suite_core/{component}/MODULE_MANIFEST.json'
    files[manifest_name] = build.json_bytes(dict(schema_version=1, module_id='collectigen.' + component,
        software_version='0.1.0-alpha.1', runtime_files=paths))
    for name, raw in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    inventory = build.json_bytes(dict(schema_version=1, manifest_excludes_self=True,
        files={name: dict(size_bytes=len(raw), sha256=build.digest(raw)) for name, raw in files.items()}))
    (source / 'public-source.json').write_bytes(inventory)
    subset = tmp_path / 'subset.json'
    subset.write_bytes(build.json_bytes(dict(schema=1, source_rows=[dict(path=name, bytes=len(raw),
                        sha256=build.digest(raw)) for name, raw in files.items()])))
    stage = prepare_public_stage(source, tmp_path / 'prepared', component=component,
        public_inventory_sha256=build.digest(inventory), frozen_sources=subset,
        frozen_sources_sha256=build.digest(subset.read_bytes()), manifest_sha256=build.digest(files[manifest_name]))
    return stage, subset.read_bytes(), inventory


@pytest.mark.parametrize('component', ['generate', 'train'])
def test_module_stage_has_exact_entry_and_no_root_or_manager_wrapper(tmp_path, component):
    stage, manifest, inventory = stage_input(tmp_path, component)
    receipt, _subset, sources = build.validate_sources(stage, build.digest((stage / 'STAGE.json').read_bytes()),
                                                        manifest, inventory, component=component)
    assert receipt['software_version'] == '0.1.0-alpha.1'
    assert receipt['component'] == component and receipt['root_entry'] is None
    assert receipt['manager_entry'] == f'suite_core/{component}/__main__.py'
    assert not (stage / 'source/public_root_entry.py').exists()
    assert not any('suite_core/manager/' in name for name in sources)
    foreign = 'train' if component == 'generate' else 'generate'
    with pytest.raises(build.BuildHold, match='same_frozen_component_required'):
        build.validate_sources(stage, build.digest((stage / 'STAGE.json').read_bytes()),
                               manifest, inventory, component=foreign)
    entry = stage / f'source/suite_core/{component}/__main__.py'
    entry.write_bytes(b'changed after sealing')
    with pytest.raises(build.BuildHold):
        build.validate_sources(stage, build.digest((stage / 'STAGE.json').read_bytes()),
                               manifest, inventory, component=component)


def test_unknown_component_refuses_before_output(tmp_path):
    with pytest.raises(ValueError, match='finite_frozen_component_required'):
        prepare_public_stage(tmp_path / 'absent', tmp_path / 'output', component='unknown',
            public_inventory_sha256='0' * 64, frozen_sources=tmp_path / 'none',
            frozen_sources_sha256='0' * 64, manifest_sha256='0' * 64)
    assert not (tmp_path / 'output').exists()
