"""Independent sealed source identities; no freezer, interpreter or app runs."""
import json
from pathlib import Path

import pytest

from tools import build_manager_distribution as build
from tools.prepare_manager_production_stage import prepare_public_stage
from test_manager_distribution_builder import inputs, fake_freeze


def stage_input(tmp_path, component, *, include_readme=True, readme=None, include_notes=True, notes=None):
    source = tmp_path / 'export'
    paths = [f'suite_core/{component}/__main__.py',
             'suite_core/workflow_modules/entry.py', 'suite_core/workflow_modules/session.py']
    files = {name: b'# generated inert source; no program startup\n' for name in paths}
    if include_readme:
        files[f'suite_core/{component}/README.md'] = (readme if readme is not None else
            f'# CollectiGen {component.title()} 0.1.0-alpha.1\n\nSynthetic package guide.\n'.encode())
    if include_notes:
        files[f'suite_core/{component}/WHATS_NEW.md'] = (notes if notes is not None else
            f'# CollectiGen {component.title()} 0.1.0-alpha.1\n\nSynthetic release notes.\n'.encode())
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


def workflow_arguments(inputs, tmp_path, component, **guide):
    args, calls = inputs
    args = dict(args)
    args.pop('root_renderer')
    stage, subset, _inventory = stage_input(tmp_path / 'workflow', component, **guide)
    manifest = tmp_path / 'workflow-subset.json'
    manifest.write_bytes(subset)
    args.update(component=component, source_stage=stage,
        stage_sha256=build.digest((stage/'STAGE.json').read_bytes()),
        public_inventory=tmp_path/'workflow/export/public-source.json', source_manifest=manifest)
    return args, calls


@pytest.mark.parametrize('component', ['generate', 'train'])
def test_workflow_guide_is_sealed_and_inventoried_in_stock_package(inputs, tmp_path, monkeypatch, component):
    import zipfile
    from tools.build_component_package import build_package
    args, calls = workflow_arguments(inputs, tmp_path, component)
    fake_freeze(monkeypatch, calls)
    result = build.build_workflow_distribution(**args)
    payload=Path(result['payload'])
    original=(args['source_stage']/f'source/suite_core/{component}/README.md').read_bytes()
    assert (payload/'README.md').read_bytes()==original
    notes=(args['source_stage']/f'source/suite_core/{component}/WHATS_NEW.md').read_bytes()
    assert (payload/'WHATS_NEW.md').read_bytes()==notes
    runtime=json.loads((payload/'RUNTIME_MANIFEST.json').read_bytes())
    guide=next(row for row in runtime['files'] if row['path']=='README.md')
    assert guide==dict(path='README.md',size_bytes=len(original),sha256=build.digest(original))
    assert calls==['tools-inspected-synthetically',component.title()]
    assert result['apps_launched'] is False and result['native_qualified'] is False
    archive=tmp_path/'package'
    build_package(payload,archive,runtime_manifest='RUNTIME_MANIFEST.json',module_manifest='MODULE_MANIFEST.json',
        component_id='collectigen.'+component,software_version='0.1.0-alpha.1',os_name='windows',architecture='x86_64',
        artifact_name='synthetic.zip',supported_from='0.1.0-alpha.1',published_at='2026-11-17T00:00:00Z',
        release_notes='Synthetic package assembly; no application was run.')
    with zipfile.ZipFile(archive/'synthetic.zip') as z:
        assert z.read('README.md')==original
        assert z.read('WHATS_NEW.md')==notes
        inventory=json.loads(z.read('BOOTSTRAP_INVENTORY.json'))
        assert any(row['path']=='README.md' and row['sha256']==build.digest(original)
                   for row in inventory['files'])
        assert any(row['path']=='WHATS_NEW.md' and row['sha256']==build.digest(notes)
                   for row in inventory['files'])


@pytest.mark.parametrize('component', ['generate', 'train'])
@pytest.mark.parametrize('fault', ['missing','wrong-version','wrong-component','stale', 'missing-notes','wrong-notes'])
def test_unqualified_guide_refuses_before_freeze_or_output(inputs, tmp_path, component, fault):
    wrong=(f'# CollectiGen {component.title()} 0.0.1-alpha.1\n' if fault=='wrong-version' else
           '# CollectiGen Other 0.1.0-alpha.1\n').encode()
    args,calls=workflow_arguments(inputs,tmp_path,component,include_readme=fault!='missing',
        readme=wrong if fault in ('wrong-version','wrong-component') else None,
        include_notes=fault!='missing-notes',notes=wrong if fault=='wrong-notes' else None)
    if fault=='stale':
        (args['source_stage']/f'source/suite_core/{component}/README.md').write_bytes(b'changed after sealing')
    with pytest.raises(build.BuildHold):
        build.build_workflow_distribution(**args)
    assert not args['output'].exists() and calls==[]
