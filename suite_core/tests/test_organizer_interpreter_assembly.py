"""Synthetic source projection and inert interpreter package assembly cases."""
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools import organizer_freeze_inputs as source_inputs
from tools.build_component_package import assemble_organizer_interpreter, ORGANIZER_PTH, build_package, _mutable_parts


def source_fixture(tmp_path, monkeypatch, *, private=False):
    root = tmp_path / "software"
    (root / "core").mkdir(parents=True)
    (root / "tools").mkdir()
    raw = b"synthetic_marker = True\n"
    (root / "core/fixture.py").write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    rows = [{"path": "core/fixture.py", "sha256": digest, "public_registered": False}]
    if private:
        rows.append({"path": "core/development_bug_inbox.py", "sha256": "a" * 64})
    (root / "tools/public-source-manifest.json").write_text(
        json.dumps({"files": ["core/fixture.py"]}), encoding="utf-8")
    assessment = tmp_path / "reviewed.json"
    assessment.write_text(json.dumps({"rows": rows}), encoding="utf-8")
    monkeypatch.setattr(source_inputs, "EXPLICIT_STARTUP_LEAVES", ())
    return root, assessment


def test_reviewed_actual_source_rows_reuse_public_preparer(tmp_path, monkeypatch):
    root, assessment = source_fixture(tmp_path, monkeypatch)
    output = tmp_path / "prepared"
    receipt = source_inputs.prepare(root, assessment, output)
    assert len(receipt["source_rows"]) == 1
    assert not receipt["source_rows"][0]["changed_since_assessment"]
    assert receipt["retained_dynamic_imports"] is None
    assert receipt["external_names"] is None
    assert not receipt["standalone_distribution_qualified"]
    assert (output / "source/core/fixture.py").read_bytes() == (root / "core/fixture.py").read_bytes()


def test_private_leaf_excluded_without_reading_its_nonexistent_bytes(tmp_path, monkeypatch):
    root, assessment = source_fixture(tmp_path, monkeypatch, private=True)
    receipt = source_inputs.prepare(root, assessment, tmp_path / "prepared")
    assert receipt["excluded_private_runtime_leaves"] == ["core/development_bug_inbox.py"]
    assert len(receipt["source_rows"]) == 1


@pytest.mark.parametrize("defect", ["hash", "escape", "alias", "unregistered"])
def test_reviewed_source_evidence_refuses_bad_rows_before_output(tmp_path, monkeypatch, defect):
    root, assessment = source_fixture(tmp_path, monkeypatch)
    rows = json.loads(assessment.read_text(encoding="utf-8"))["rows"]
    if defect == "hash": rows[0]["sha256"] = "not-a-pin"
    elif defect == "escape": rows[0]["path"] = "../outside.py"
    elif defect == "alias": rows.append({"path": "CORE/fixture.py", "sha256": "a" * 64})
    else: rows[0]["path"] = "core/unregistered.py"
    assessment.write_text(json.dumps({"rows": rows}), encoding="utf-8")
    output = tmp_path / "prepared"
    with pytest.raises(ValueError): source_inputs.prepare(root, assessment, output)
    assert not output.exists()


def assembly_fixture(tmp_path):
    runtime=tmp_path/'runtime'
    prepared=tmp_path/'prepared'
    assets=tmp_path/'admitted-assets'
    files={'python/python.exe':b'synthetic interpreter','python/python313._pth':b'python313.zip\n.\n../../checkout\n',
           'python/module.py':b'original source','python/__pycache__/module.cpython-313.pyc':b'generated cache',
           'python/__pycache__/orphan.cpython-313.pyc':b'bytecode only',
           'python/Lib/site-packages/provider/RECORD':b'original wheel provenance',
           'python/Lib/site-packages/provider/LICENSE':b'upstream notice',
           'python/Lib/site-packages/provider/header.h':b'upstream header',
           'python/Lib/site-packages/provider/plugins/plugin.dll':b'upstream plugin',
           'python/Lib/site-packages/provider/codec.pyd':b'upstream codec',
           'exiftool/exiftool.exe':b'synthetic metadata tool'}
    for name,raw in files.items():
        path=runtime/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    manifest={'schema_version':2,'profile':'organizer-image','python_version':'3.13.15','architecture':'amd64',
              'critical_files':{'python/python.exe':hashlib.sha256(files['python/python.exe']).hexdigest()}}
    stock=runtime/'python/collectigen-runtime.json';stock.write_text(json.dumps(manifest),encoding='utf-8')
    source=prepared/'source/suite_core/media_organizer/__main__.py'
    source.parent.mkdir(parents=True);source.write_bytes(b'# inert entry; never execute\n')
    rows=[{'path':'suite_core/media_organizer/__main__.py','sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'size':source.stat().st_size}]
    receipt=prepared/'INPUTS.json';receipt.write_text(json.dumps({'runtime_scope':'public','source_rows':rows}),encoding='utf-8')
    asset_files={'suite_core/media_organizer/MODULE_MANIFEST.json':json.dumps({'schema_version':1,
        'module_id':'collectigen.media-organizer','software_version':'0.4.0-alpha.1'}).encode(),
        'tools/runtime-pins.json':b'{}','tools/optional-ai-profiles.json':b'{}',
        'requirements-organizer-image-runtime.lock':b'# synthetic lock',
        'LICENSE':b'notice','LICENSES.md':b'notice','THIRD_PARTY_NOTICES.md':b'notice'}
    inputs=[]
    for name,raw in asset_files.items():
        path=assets/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        inputs.append((path,name,hashlib.sha256(raw).hexdigest()))
    output=tmp_path/'Apps/collectigen.media-organizer/0.4.0-alpha.1'
    output.parent.mkdir(parents=True)
    return dict(runtime_root=runtime,source_inputs_root=prepared,output_root=output,
                software_version=output.name,runtime_kind='interpreter-portable',
                expected_runtime_manifest_sha256=hashlib.sha256(stock.read_bytes()).hexdigest(),
                expected_source_receipt_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(),asset_inputs=tuple(inputs))


def test_cache_projection_relocates_pth_and_preserves_other_software(tmp_path):
    options=assembly_fixture(tmp_path)
    source_record=(options['runtime_root']/'python/Lib/site-packages/provider/RECORD').read_bytes()
    result=assemble_organizer_interpreter(**options)
    root=options['output_root']
    assert result['assembly_complete'] and not result['Manager_admitted']
    assert result['excluded_cache_files']==1
    assert (root/'Runtime/python/python313._pth').read_bytes()==ORGANIZER_PTH
    assert not (root/'Runtime/python/__pycache__/module.cpython-313.pyc').exists()
    assert (root/'Runtime/python/__pycache__/orphan.cpython-313.pyc').read_bytes()==b'bytecode only'
    assert (root/'Runtime/python/Lib/site-packages/provider/RECORD').read_bytes()==source_record
    for name in ('header.h','plugins/plugin.dll','codec.pyd','LICENSE'):
        assert (root/'Runtime/python/Lib/site-packages/provider'/name).exists()
    assert (options['runtime_root']/'python/__pycache__/module.cpython-313.pyc').exists()
    assert not result['same_version_installed_repair_supported']


@pytest.mark.parametrize('defect',['kind','stale_runtime','stale_source','source_extra','missing_assets','existing','raw_seed'])
def test_assembly_refuses_before_output_write(tmp_path,defect):
    options=assembly_fixture(tmp_path)
    if defect=='kind': options['runtime_kind']='frozen-portable'
    elif defect=='stale_runtime': options['expected_runtime_manifest_sha256']='0'*64
    elif defect=='stale_source': options['expected_source_receipt_sha256']='0'*64
    elif defect=='source_extra': (options['source_inputs_root']/'source/unlisted.py').write_bytes(b'extra')
    elif defect=='missing_assets': options['asset_inputs']=()
    elif defect=='existing': options['output_root'].mkdir()
    else:
        raw=tmp_path/'public-seed-inputs/tags.json';raw.parent.mkdir();raw.write_bytes(b'held raw fixture')
        options['asset_inputs']+=((raw,'assets/tags.json',hashlib.sha256(raw.read_bytes()).hexdigest()),)
    with pytest.raises((ValueError,RuntimeError)): assemble_organizer_interpreter(**options)
    assert not (options['output_root']/'Runtime').exists()


def test_assembled_inert_interpreter_consumes_original_unsigned_packager(tmp_path):
    options=assembly_fixture(tmp_path)
    assemble_organizer_interpreter(**options)
    packaged=tmp_path/'unsigned'
    result=build_package(options['output_root'],packaged,runtime_manifest='RUNTIME_MANIFEST.json',
        module_manifest='suite_core/media_organizer/MODULE_MANIFEST.json',component_id='collectigen.media-organizer',
        software_version='0.4.0-alpha.1',os_name='windows',architecture='x86_64',artifact_name='inert.zip',
        supported_from='0.4.0-alpha.1',published_at='2026-10-13T00:00:00Z',release_notes='INERT fixture',
        runtime_kind='interpreter-portable')
    inventory=json.loads((packaged/'BOOTSTRAP_INVENTORY.json').read_text(encoding='utf-8'))
    assert inventory['runtime_kind']=='interpreter-portable'
    assert result['production_install_authorized'] is False


@pytest.mark.parametrize('name',['INCOMPLETE.json','incomplete.JSON','RUNTIME_MANIFEST.json',
                               'Runtime_Manifest.JSON','BOOTSTRAP_INVENTORY.json','bootstrap_inventory.JSON',
                               'assets/Incomplete.JSON','assets/runtime_manifest.json','assets/BOOTSTRAP_INVENTORY.json',
                               'assets/Bootstrap_Inventory.JSON/payload.json'])
def test_reserved_asset_destinations_refuse_without_writes(tmp_path,name):
    options=assembly_fixture(tmp_path)
    supplied=tmp_path/'reserved-asset.json'
    supplied.write_bytes(b'explicit synthetic asset bytes')
    options['asset_inputs']+=((supplied,name,hashlib.sha256(supplied.read_bytes()).hexdigest()),)
    before={str(p.relative_to(tmp_path)):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises((ValueError,RuntimeError),match='reserved_assembly_destination'):
        assemble_organizer_interpreter(**options)
    assert not options['output_root'].exists()
    after={str(p.relative_to(tmp_path)):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert before==after


def test_existing_output_stays_exact_on_reserved_input_refusal(tmp_path):
    options=assembly_fixture(tmp_path)
    output=options['output_root'];output.mkdir()
    sentinel=output/'original.json';sentinel.write_bytes(b'original accepted candidate')
    supplied=tmp_path/'reserved.json';supplied.write_bytes(b'unused input')
    options['asset_inputs']+=((supplied,'RUNTIME_MANIFEST.json',hashlib.sha256(supplied.read_bytes()).hexdigest()),)
    before={str(p.relative_to(output)):p.read_bytes() for p in output.rglob('*') if p.is_file()}
    with pytest.raises((ValueError,RuntimeError),match='fresh_assembly_destination'):
        assemble_organizer_interpreter(**options)
    assert before=={str(p.relative_to(output)):p.read_bytes() for p in output.rglob('*') if p.is_file()}


@pytest.mark.parametrize('name',['Runtime/python/Lib/site-packages/pip/_internal/models/link.py',
                               'Runtime/python/Lib/site-packages/PySide6/qml/QtQml/Models/modelsplugin.dll'])
def test_exact_vendor_models_namespaces_preserved_for_interpreter_only(name):
    assert not _mutable_parts(name,'interpreter-portable')
    assert _mutable_parts(name,'frozen-portable')=={'models'}


@pytest.mark.parametrize('name',['Runtime/models/user.safetensors',
                               'Runtime/python/Lib/site-packages/other/models/user.bin',
                               'Runtime/python/Lib/site-packages/pip/_internal/models/data/user.json',
                               'Runtime/python/Lib/site-packages/pip/_internal/models-extra/models/user.bin'])
def test_vendor_exception_cannot_admit_other_mutable_paths(name):
    assert _mutable_parts(name,'interpreter-portable')


@pytest.mark.parametrize('name',['Runtime/exiftool/exiftool_files/lib/Data/Dumper.pm',
                               'Runtime/exiftool/exiftool_files/lib/auto/Data/Dumper/Dumper.xs.dll'])
def test_exact_perl_data_code_namespace_is_not_user_data(name):
    assert not _mutable_parts(name,'interpreter-portable')
    assert _mutable_parts(name,'frozen-portable')=={'data'}
    assert _mutable_parts(name.rsplit('/',1)[0]+'/models/user.bin','interpreter-portable')=={'models'}


def test_shared_payload_path_helper_carries_vendor_names_through_packaging(tmp_path):
    options=assembly_fixture(tmp_path)
    paths=('python/Lib/site-packages/PySide6/qml/QtQuick/Pdf/+Material/PdfStyle.qml',
           'python/Lib/site-packages/pip/_vendor/requests/requirements@' + 'vendor.txt')
    for name in paths:
        path=options['runtime_root']/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'inert vendor file')
    assemble_organizer_interpreter(**options)
    packaged=tmp_path/'vendor-package'
    build_package(options['output_root'],packaged,runtime_manifest='RUNTIME_MANIFEST.json',
        module_manifest='suite_core/media_organizer/MODULE_MANIFEST.json',component_id='collectigen.media-organizer',
        software_version='0.4.0-alpha.1',os_name='windows',architecture='x86_64',artifact_name='inert.zip',
        supported_from='0.4.0-alpha.1',published_at='LOCAL-INERT-TEST',release_notes='INERT',runtime_kind='interpreter-portable')
    inventory=json.loads((packaged/'BOOTSTRAP_INVENTORY.json').read_text(encoding='utf-8'))
    assert {'Runtime/'+p for p in paths}<={row['path'] for row in inventory['files']}


def test_payload_helper_does_not_relax_artifact_locator(tmp_path):
    options=assembly_fixture(tmp_path);assemble_organizer_interpreter(**options)
    output=tmp_path/'bad-artifact'
    with pytest.raises((ValueError,RuntimeError)):
        build_package(options['output_root'],output,runtime_manifest='RUNTIME_MANIFEST.json',
            module_manifest='suite_core/media_organizer/MODULE_MANIFEST.json',component_id='collectigen.media-organizer',
            software_version='0.4.0-alpha.1',os_name='windows',architecture='x86_64',artifact_name='+bad.zip',
            supported_from='0.4.0-alpha.1',published_at='LOCAL-INERT-TEST',release_notes='INERT',runtime_kind='interpreter-portable')
    assert not output.exists()


def test_actual_embedded_windows_path_reaches_package_root(tmp_path):
    # Exact task-owned approved stock artifact only; CI without it skips native evidence.
    stock=Path(__file__).resolve().parents[2]/'data/runtime/organizer-image-proof-20261013/python'
    if not (stock/'python.exe').is_file(): pytest.skip('Task-owned embedded Windows interpreter absent')
    root=tmp_path/'package';python=root/'Runtime/python';python.mkdir(parents=True)
    for source in stock.iterdir():
        if source.is_file(): shutil.copyfile(source,python/source.name)
    (python/'python313._pth').write_bytes(ORGANIZER_PTH)
    (root/'root_marker.py').write_bytes(b'synthetic_marker=True\n')
    result=subprocess.run([str(python/'python.exe'),'-I','-B','-c',
        'import sys,json,importlib.util,site;print(json.dumps({"path":sys.path,"origin":importlib.util.find_spec("root_marker").origin,"user_site":site.ENABLE_USER_SITE}))'],
        cwd=root,check=True,capture_output=True,text=True,timeout=20)
    data=json.loads(result.stdout)
    assert Path(data['origin'])==root/'root_marker.py'
    assert str(root) in data['path'] and data['user_site'] is False
    assert not (root/'__pycache__').exists()


@pytest.mark.parametrize('prefix', [
    'Runtime/python/Lib/site-packages/pip/_internal/models',
    'Runtime/python/Lib/site-packages/PySide6/qml/QtQml/Models',
    'Runtime/exiftool/exiftool_files/lib/Data',
    'Runtime/exiftool/exiftool_files/lib/auto/Data',
])
@pytest.mark.parametrize('nested', ['models', 'Models', 'data', 'Data'])
def test_vendor_exception_only_exempts_exact_prefix_segment(prefix, nested):
    assert _mutable_parts(prefix+'/'+nested+'/user.bin', 'interpreter-portable') == {nested.casefold()}


def shipping_fixture(tmp_path):
    from tools.build_component_package import SHIPPING_REQUIRED_ASSETS
    options=assembly_fixture(tmp_path)
    inputs=list(options['asset_inputs']);by_name={name:path for path,name,_ in inputs}
    for name in SHIPPING_REQUIRED_ASSETS-set(by_name):
        path=tmp_path/'shipping'/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'reviewed synthetic shipping asset');by_name[name]=path
    module=by_name['suite_core/media_organizer/MODULE_MANIFEST.json']
    value=json.loads(module.read_text());value.update(interface_version=1,
        entrypoint='Start_MediaOrganizer.bat',runtime_kind='interpreter-portable',runtime_profile='organizer-image')
    module.write_text(json.dumps(value))
    options['asset_inputs']=tuple((path,name,hashlib.sha256(path.read_bytes()).hexdigest())
                                 for name,path in sorted(by_name.items()))
    options['shipping_descriptor']={'schema_version':1,'component_id':'collectigen.media-organizer',
        'software_version':options['software_version'],'runtime_kind':'interpreter-portable',
        'runtime_profile':'organizer-image','entrypoint':'Start_MediaOrganizer.bat',
        'assets':[{'source':str(path),'destination':name,'size_bytes':path.stat().st_size,'sha256':digest}
                  for path,name,digest in options['asset_inputs']]}
    return options


def test_exact_shipping_descriptor_admitted(tmp_path):
    options=shipping_fixture(tmp_path);result=assemble_organizer_interpreter(**options)
    assert result['shipping_inputs_admitted'] and len(result['shipping_descriptor_sha256'])==64
    assert not result['Manager_admitted'] and not result['native_health_verified']


from tools.build_component_package import SHIPPING_REQUIRED_ASSETS

@pytest.mark.parametrize('missing', sorted(SHIPPING_REQUIRED_ASSETS))
def test_missing_shipping_input_refuses_before_output(tmp_path, missing):
    options=shipping_fixture(tmp_path)
    options['shipping_descriptor']['assets']=[r for r in options['shipping_descriptor']['assets']
                                             if r['destination']!=missing]
    options['asset_inputs']=tuple(r for r in options['asset_inputs'] if r[1]!=missing)
    with pytest.raises(ValueError, match='shipping_required_inputs'):
        assemble_organizer_interpreter(**options)
    assert not options['output_root'].exists()


@pytest.mark.parametrize('fault', ['pin','size','extra','duplicate','escape','tuple_pin'])
def test_changed_shipping_closure_refuses_before_output(tmp_path, fault):
    options=shipping_fixture(tmp_path);rows=options['shipping_descriptor']['assets']
    if fault=='pin': rows[0]['sha256']='0'*64
    elif fault=='size': rows[0]['size_bytes']+=1
    elif fault=='extra': options['asset_inputs']+=((options['asset_inputs'][0][0],'extra.md',options['asset_inputs'][0][2]),)
    elif fault=='duplicate': rows.append(dict(rows[0],destination=rows[0]['destination'].swapcase()))
    elif fault=='escape': rows[0]['destination']='../README.md'
    else: options['asset_inputs']=((options['asset_inputs'][0][0],options['asset_inputs'][0][1],'0'*64),)+options['asset_inputs'][1:]
    with pytest.raises(ValueError): assemble_organizer_interpreter(**options)
    assert not options['output_root'].exists()


@pytest.mark.parametrize('field,value', [('module_id','other'),('software_version','0.9.0'),
    ('entrypoint','MediaOrganizer.exe'),('runtime_kind','frozen-portable'),
    ('runtime_profile','collectigen-core'),('interface_version',True)])
def test_shipping_module_binding_refuses_before_output(tmp_path, field, value):
    options=shipping_fixture(tmp_path)
    row=next(r for r in options['shipping_descriptor']['assets'] if r['destination'].endswith('MODULE_MANIFEST.json'))
    path=Path(row['source']);module=json.loads(path.read_text());module[field]=value;path.write_text(json.dumps(module))
    row.update(size_bytes=path.stat().st_size,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    options['asset_inputs']=tuple((p,n,row['sha256'] if n==row['destination'] else d) for p,n,d in options['asset_inputs'])
    with pytest.raises(ValueError,match='shipping_module_binding'): assemble_organizer_interpreter(**options)
    assert not options['output_root'].exists()


@pytest.mark.parametrize('field,value', [('schema_version',True),('component_id','other'),
    ('software_version','0.9.0'),('runtime_kind','frozen-portable'),
    ('runtime_profile','collectigen-core'),('entrypoint','MediaOrganizer.exe'),('assets',{})])
def test_shipping_descriptor_identity_refuses_before_output(tmp_path, field, value):
    options=shipping_fixture(tmp_path);options['shipping_descriptor'][field]=value
    with pytest.raises(ValueError): assemble_organizer_interpreter(**options)
    assert not options['output_root'].exists()


@pytest.mark.parametrize('name', [
    'readme.md/extra.txt',
    'Suite_Core/media_organizer/__MAIN__.py/extra.txt',
    'suite_CORE',
    'runtime/python/PYTHON.exe/extra.txt',
    'RUNTIME/python',
])
@pytest.mark.parametrize('shipping', [False, True])
def test_complete_destination_file_ancestor_conflicts_refuse_before_output(tmp_path, name, shipping):
    options=shipping_fixture(tmp_path) if shipping else assembly_fixture(tmp_path)
    additions=[]
    if not shipping and name=='readme.md/extra.txt':
        first=tmp_path/'asset-readme';first.write_bytes(b'readme')
        additions.append((first,'README.md',hashlib.sha256(first.read_bytes()).hexdigest()))
    extra=tmp_path/'asset-extra';extra.write_bytes(b'conflicting file')
    additions.append((extra,name,hashlib.sha256(extra.read_bytes()).hexdigest()))
    options['asset_inputs']+=tuple(additions)
    if shipping:
        options['shipping_descriptor']['assets'] += [
            {'source':str(path),'destination':destination,'size_bytes':path.stat().st_size,'sha256':digest}
            for path,destination,digest in additions]
    with pytest.raises(ValueError,match='assembly_file_directory_conflict'):
        assemble_organizer_interpreter(**options)
    assert not options['output_root'].exists()


@pytest.mark.parametrize("reverse", [False, True])
def test_asset_parent_child_order_conflict_refuses_before_output(tmp_path, reverse):
    options=assembly_fixture(tmp_path)
    extra=tmp_path/'asset-extra';extra.write_bytes(b'conflicting file')
    digest=hashlib.sha256(extra.read_bytes()).hexdigest()
    pairs=((extra,'README.md',digest),(extra,'readme.md/extra.txt',digest))
    options['asset_inputs']+=tuple(reversed(pairs)) if reverse else pairs
    with pytest.raises(ValueError,match='assembly_file_directory_conflict'):
        assemble_organizer_interpreter(**options)
    assert not options['output_root'].exists()
