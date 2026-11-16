"""The exact public catalog is software; all other data remains excluded."""
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
import stat
import pytest
from tools import build_component_package as builder


@pytest.fixture
def package(tmp_path, monkeypatch):
    canonical = (builder.ROOT / 'data/model_workflow_baseline.json').read_bytes()
    tools = tmp_path / 'reviewed-tools'
    target = tools / 'data/model_workflow_baseline.json'
    target.parent.mkdir(parents=True); target.write_bytes(canonical)
    monkeypatch.setattr(builder, 'ROOT', tools)
    producer = tmp_path / 'producer'; payload = producer / 'Payload'
    payload.mkdir(parents=True)
    files = {'Generate.exe': b'INERT, NEVER EXECUTED',
        'MODULE_MANIFEST.json': json.dumps({'schema_version': 1,
            'module_id': 'collectigen.generate', 'software_version': '0.1.0-alpha.1'}).encode(),
        '_internal/data/model_workflow_baseline.json': canonical}
    def save_files():
        for name, raw in files.items():
            path = payload / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
        runtime = dict(schema_version=1, component='generate', feature_version='0.1.0-alpha.1',
            os='windows', architecture='x86_64', inventory_excludes_itself='RUNTIME_MANIFEST.json',
            files=[dict(path=n, size_bytes=len(r), sha256=sha256(r).hexdigest()) for n,r in files.items()])
        (payload / 'RUNTIME_MANIFEST.json').write_text(json.dumps(runtime), encoding='utf-8')
    save_files()
    stage = producer / 'stage/STAGE.json'; stage.parent.mkdir()
    value = dict(schema=1, component='generate', software_version='0.1.0-alpha.1', source_rows=[
        dict(path='data/model_workflow_baseline.json', bytes=len(canonical), sha256=sha256(canonical).hexdigest())])
    def save_stage():
        stage.write_text(json.dumps(value), encoding='utf-8')
        return sha256(stage.read_bytes()).hexdigest()
    options = dict(payload_root=payload, output_root=tmp_path / 'output', runtime_manifest='RUNTIME_MANIFEST.json',
        module_manifest='MODULE_MANIFEST.json', component_id='collectigen.generate', software_version='0.1.0-alpha.1',
        os_name='windows', architecture='x86_64', artifact_name='inert.zip', supported_from='0.1.0-alpha.1',
        published_at='2026-11-16T00:00:00Z', release_notes='Synthetic packaging only.',
        frozen_producer=producer, frozen_producer_stage_sha256=save_stage())
    return options, value, save_stage, files, save_files, target


@pytest.mark.parametrize('component', ['generate', 'train'])
def test_explicit_exact_catalog_packages_without_qualification_claim(package, component):
    options, value, save_stage, files, save_files, target = package
    if component == 'train':
        options['component_id'] = 'collectigen.train'
        value['component'] = component
        options['frozen_producer_stage_sha256'] = save_stage()
        module = json.loads(files['MODULE_MANIFEST.json']); module['module_id'] = options['component_id']
        files['MODULE_MANIFEST.json'] = json.dumps(module).encode(); save_files()
        runtime_path = options['payload_root'] / 'RUNTIME_MANIFEST.json'
        runtime = json.loads(runtime_path.read_bytes()); runtime['component'] = component
        runtime_path.write_text(json.dumps(runtime), encoding='utf-8')
    receipt = builder.build_package(**options)
    assert receipt['immutable_model_catalog']['sha256'] == sha256(target.read_bytes()).hexdigest()
    assert receipt['immutable_model_catalog']['producer_stage_sha256'] == options['frozen_producer_stage_sha256']
    assert not receipt['signed'] and not receipt['native_health_verified'] and not receipt['production_install_authorized']


@pytest.mark.parametrize('defect', ['default', 'missing_pin', 'changed_pin', 'wrong_component', 'wrong_version',
    'missing_row', 'duplicate_row', 'forged_row', 'changed_payload', 'sibling', 'nested', 'casing', 'alias'])
def test_catalog_exception_stays_finite_and_correlated(package, monkeypatch, defect):
    options, value, save_stage, files, save_files, target = package
    if defect == 'default':
        options.pop('frozen_producer'); options.pop('frozen_producer_stage_sha256')
    elif defect == 'missing_pin': options['frozen_producer_stage_sha256'] = None
    elif defect == 'changed_pin': options['frozen_producer_stage_sha256'] = 'a' * 64
    elif defect == 'wrong_component': value['component'] = 'train'
    elif defect == 'wrong_version': value['software_version'] = '0.2.0-alpha.1'
    elif defect == 'missing_row': value['source_rows'] = []
    elif defect == 'duplicate_row': value['source_rows'] *= 2
    elif defect == 'forged_row': value['source_rows'][0]['sha256'] = 'f' * 64
    elif defect == 'changed_payload': files['_internal/data/model_workflow_baseline.json'] = b'changed'; save_files()
    elif defect in {'sibling', 'nested', 'casing'}:
        name = {'sibling': '_internal/data/user.json', 'nested': '_internal/data/nested/private.json',
                'casing': '_internal/Data/other.json'}[defect]
        files[name] = b'not software'; save_files()
    else:
        original = Path.lstat
        monkeypatch.setattr(Path, 'lstat', lambda path, *args, **kwargs:
            SimpleNamespace(st_mode=stat.S_IFREG, st_nlink=1, st_file_attributes=0x400)
            if path == target else original(path, *args, **kwargs))
    if defect in {'wrong_component', 'wrong_version', 'missing_row', 'duplicate_row', 'forged_row'}:
        options['frozen_producer_stage_sha256'] = save_stage()
    with pytest.raises(ValueError): builder.build_package(**options)
    assert not options['output_root'].exists()


def test_catalog_source_stale_guard_precedes_output(package, monkeypatch):
    options, _value, _save_stage, _files, _save_files, target = package
    original = builder.produce_inventory
    def mutate_after_inventory(*args, **kwargs):
        result = original(*args, **kwargs)
        target.write_bytes(b'changed public source')
        return result
    monkeypatch.setattr(builder, 'produce_inventory', mutate_after_inventory)
    with pytest.raises(ValueError, match='immutable_model_catalog_changed'): builder.build_package(**options)
    assert not options['output_root'].exists()
