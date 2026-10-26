"""Tiny inert packaging outputs, canonical inventory and signed pair admission."""
from hashlib import sha256
from pathlib import Path
import json
import subprocess
import sys
import zipfile

import pytest
from tools.build_component_package import build_package
from suite_core.discovery.component_staging import create_disposable_root, INVENTORY
from suite_core.discovery.offline_component_producer import PackageInput, qualify_pair
from suite_core.discovery.component_release_signature import verify_component_release
from suite_core.installation import ComponentId
from suite_core.tests.test_component_staging import COMPONENT, TRUST, sign


def producer_identity_packet(tmp_path, stage='alpha', baseline_schema=1):
    from suite_core.tests.test_component_release_signature import manifest, raw, signature, trust
    version = '0.2.0'; root = payload(tmp_path, version); source = tmp_path / 'source'
    source.mkdir(); wrapped = root / 'App'; module_path = wrapped / '_internal/MODULE_MANIFEST.json'
    module = json.loads(module_path.read_bytes())
    module.update(numeric_version=version, maturity=stage, release_date='2026-10-06', release_timezone='UTC')
    module_path.write_bytes(raw(module))
    runtime_path = wrapped / 'RUNTIME_MANIFEST.json'; runtime = json.loads(runtime_path.read_bytes())
    runtime['maturity'] = stage
    for row in runtime['files']:
        data = (wrapped / row['path']).read_bytes(); row.update(size_bytes=len(data), sha256=sha256(data).hexdigest())
    runtime_path.write_bytes(raw(runtime))
    assignments = dict(SOFTWARE_VERSION=version, NUMERIC_VERSION=version, MATURITY=stage,
        RELEASE_DATE='2026-10-06', RELEASE_TIMEZONE='UTC')
    (source / 'version.py').write_bytes(('\n'.join(k+' = '+repr(v) for k,v in assignments.items())+'\n').encode())
    (source / 'MODULE_MANIFEST.json').write_bytes(raw(module))
    (source / 'history.json').write_bytes(raw(dict(schema_version=1, releases=[dict(
        software_version=version, numeric_version=version, maturity=stage, date='2026-10-06', timezone='UTC')])))
    display = ('a.' if stage=='alpha' else 'b.' if stage=='beta' else '') + version
    notes = f'# {display} - 2026-10-06\r\n\r\nINERT approved fixture notes.\r\n'
    (source / 'notes.md').write_bytes(notes.encode())
    old = manifest(); old.update(component_id=COMPONENT.value, version='0.1.0-alpha.9', supported_from='0.1.0-alpha.9',
        platform=dict(os='windows', architecture='x86_64'))
    if baseline_schema==2: old.update(schema_version=2, version='0.1.0', supported_from='0.1.0', maturity='alpha')
    for name, data in [('baseline.json', raw(old)), ('baseline.sig', raw(signature(raw(old)))), ('trust.json', raw(trust()))]:
        (tmp_path / name).write_bytes(data)
    def pin(p): return dict(path=str(p), sha256=sha256(p.read_bytes()).hexdigest())
    descriptor = dict(schema_version=1, purpose='COMPONENT_SCHEMA2_PRODUCER_INPUTS', component_id=COMPONENT.value,
        version=version, maturity=stage, source_version=pin(source/'version.py'), source_module=pin(source/'MODULE_MANIFEST.json'),
        release_history=pin(source/'history.json'), release_notes=pin(source/'notes.md'),
        baseline=dict(manifest=pin(tmp_path/'baseline.json'), signature=pin(tmp_path/'baseline.sig'), trust=pin(tmp_path/'trust.json')))
    (source/'public-source.json').write_bytes(raw(dict(schema_version=1, export_scope='component-source',
        fresh_history_required=True, manifest_excludes_self=True, files={p.name:dict(size_bytes=p.stat().st_size,
        sha256=sha256(p.read_bytes()).hexdigest()) for p in source.iterdir()})))
    descriptor['source_inventory'] = pin(source/'public-source.json')
    inputs = tmp_path/'identity.json'; inputs.write_bytes(raw(descriptor))
    args = options(root, tmp_path/'output', version=version)
    args.update(supported_from='0.1.0', release_notes=notes, identity_inputs=inputs)
    return args, descriptor


def rebind_identity_packet(args, descriptor, *, source=True):
    """Only synthetic fixtures may refresh approved pins after deliberate changes."""
    from suite_core.tests.test_component_release_signature import raw
    if source:
        inventory_path = Path(descriptor['source_inventory']['path']); inventory = json.loads(inventory_path.read_bytes())
        for role in ('source_version', 'source_module', 'release_history', 'release_notes'):
            p=Path(descriptor[role]['path']); data=p.read_bytes(); descriptor[role]['sha256']=sha256(data).hexdigest()
            inventory['files'][p.name]=dict(size_bytes=len(data), sha256=sha256(data).hexdigest())
        inventory_path.write_bytes(raw(inventory)); descriptor['source_inventory']['sha256']=sha256(inventory_path.read_bytes()).hexdigest()
    Path(args['identity_inputs']).write_bytes(raw(descriptor))


@pytest.mark.parametrize('stage', ['alpha','beta','stable'])
@pytest.mark.parametrize('baseline_schema', [1,2])
def test_schema2_producer_binds_static_identity_and_b1_admission(tmp_path, stage, baseline_schema):
    args, descriptor=producer_identity_packet(tmp_path, stage, baseline_schema)
    before=snapshot(Path(args['payload_root'])); receipt=build_package(**args)
    envelope=(args['output_root']/'release-manifest.input.json').read_bytes()
    verified=verify_component_release(envelope, sign(envelope), TRUST, expected_component=COMPONENT,
        expected_version='0.2.0', expected_maturity=stage, expected_channel='stable' if stage=='stable' else 'beta',
        expected_os='windows', expected_architecture='x86_64')
    assert verified.schema_version==2 and verified.maturity==stage
    assert receipt['release_identity']['maturity']==stage and receipt['release_schema_version']==2
    assert all(not receipt[k] for k in ('signed','native_health_verified','production_install_authorized'))
    assert snapshot(Path(args['payload_root']))==before
    assert json.loads(envelope)['release_notes']==args['release_notes'] and '\r\n' in args['release_notes']
    assert not any(str(tmp_path) in key for key in receipt['release_identity']['input_sha256'])


@pytest.mark.parametrize('mutation', ['missing_constant','computed_constant','duplicate_constant','dynamic_target',
    'module_maturity','source_module_bytes','runtime_maturity','history_duplicate','history_date','source_numeric',
    'notes_heading','notes_second_heading','notes_bom','notes_content','source_hash','inventory_hash','unknown_key',
    'relative_path','alias','path_alias','source_escape','timezone','naive_stamp','equal_baseline','lower_target','floor','signature','private_trust'])
def test_schema2_producer_refuses_mismatch_before_output(tmp_path, mutation):
    from suite_core.tests.test_component_release_signature import raw, signature
    args, d=producer_identity_packet(tmp_path)
    version=Path(d['source_version']['path']); notes=Path(d['release_notes']['path']); history=Path(d['release_history']['path'])
    if mutation=='missing_constant': version.write_bytes(version.read_bytes().replace(b"MATURITY = 'alpha'\n",b''))
    elif mutation=='computed_constant': version.write_bytes(version.read_bytes().replace(b"MATURITY = 'alpha'",b"MATURITY = str('alpha')"))
    elif mutation=='duplicate_constant': version.write_bytes(version.read_bytes()+b"MATURITY = 'alpha'\n")
    elif mutation=='dynamic_target': version.write_bytes(version.read_bytes()+b"globals()['MATURITY'] = 'beta'\n")
    elif mutation=='source_numeric': version.write_bytes(version.read_bytes().replace(b"NUMERIC_VERSION = '0.2.0'",b"NUMERIC_VERSION = '0.2.1'"))
    elif mutation in ('module_maturity','source_module_bytes'):
        p=Path(d['source_module']['path']); m=json.loads(p.read_bytes()); m['maturity']='beta'; p.write_bytes(raw(m))
        if mutation=='module_maturity':
            staged=Path(args['payload_root'])/'App/_internal/MODULE_MANIFEST.json'; staged.write_bytes(p.read_bytes())
            runtime_path=Path(args['payload_root'])/'App/RUNTIME_MANIFEST.json'; runtime=json.loads(runtime_path.read_bytes())
            for row in runtime['files']:
                data=(Path(args['payload_root'])/'App'/row['path']).read_bytes();row.update(size_bytes=len(data),sha256=sha256(data).hexdigest())
            runtime_path.write_bytes(raw(runtime))
    elif mutation=='runtime_maturity':
        p=Path(args['payload_root'])/'App/RUNTIME_MANIFEST.json'; m=json.loads(p.read_bytes()); m.pop('maturity');p.write_bytes(raw(m))
    elif mutation in ('history_duplicate','history_date'):
        h=json.loads(history.read_bytes())
        if mutation=='history_duplicate':h['releases'].append(h['releases'][0])
        else:h['releases'][0]['date']='2026-10-07'
        history.write_bytes(raw(h))
    elif mutation in ('notes_heading','notes_second_heading','notes_bom'):
        content=notes.read_bytes()
        content=(content.replace(b'a.0.2.0',b'b.0.2.0') if mutation=='notes_heading' else
            content+b'# Another heading\n' if mutation=='notes_second_heading' else b'\xef\xbb\xbf'+content)
        notes.write_bytes(content); args['release_notes']=content.decode('utf-8')
    elif mutation=='notes_content': args['release_notes']='Different free text'
    elif mutation=='unknown_key':d['unknown']=1
    elif mutation=='relative_path':d['source_version']['path']='version.py'
    elif mutation=='path_alias':d['source_version']['path']=str(version.parent/'..'/'source'/'version.py')
    elif mutation=='alias':d['release_notes']=dict(d['source_version'])
    elif mutation=='source_escape':
        outside=tmp_path/'outside.py';outside.write_bytes(version.read_bytes());d['source_version']['path']=str(outside)
    elif mutation=='timezone':version.write_bytes(version.read_bytes().replace(b"'UTC'",b"'Missing/FixtureZone'"))
    elif mutation=='naive_stamp':args['published_at']='2026-10-06T00:00:00'
    elif mutation=='floor':args['supported_from']='0.1.1'
    elif mutation in ('equal_baseline','lower_target'):
        p=Path(d['baseline']['manifest']['path']);m=json.loads(p.read_bytes());m.update(schema_version=2,version='0.2.0' if mutation=='equal_baseline' else '0.3.0',supported_from='0.1.0',maturity='alpha');p.write_bytes(raw(m))
        s=Path(d['baseline']['signature']['path']);s.write_bytes(raw(signature(raw(m))))
        for role in ('manifest','signature'):d['baseline'][role]['sha256']=sha256(Path(d['baseline'][role]['path']).read_bytes()).hexdigest()
    elif mutation=='signature':
        p=Path(d['baseline']['signature']['path']);p.write_bytes(b'{}');d['baseline']['signature']['sha256']=sha256(p.read_bytes()).hexdigest()
    elif mutation=='private_trust':
        p=Path(d['baseline']['trust']['path']);t=json.loads(p.read_bytes());t['keys'][0]['private_exponent']='NOT A KEY';p.write_bytes(raw(t));d['baseline']['trust']['sha256']=sha256(p.read_bytes()).hexdigest()
    if mutation not in ('relative_path','alias','path_alias','source_escape'):rebind_identity_packet(args,d)
    else:rebind_identity_packet(args,d,source=False)
    if mutation=='source_hash':d['source_version']['sha256']='0'*64;rebind_identity_packet(args,d,source=False)
    if mutation=='inventory_hash':d['source_inventory']['sha256']='0'*64;rebind_identity_packet(args,d,source=False)
    with pytest.raises((ValueError,KeyError,UnicodeError)):
        build_package(**args)
    assert not args['output_root'].exists()


def test_schema2_identity_mutation_before_output_refuses_without_marker(tmp_path, monkeypatch):
    import tools.build_component_package as producer
    args,d=producer_identity_packet(tmp_path);original=producer.produce_inventory
    def change(*a,**k):
        result=original(*a,**k);p=Path(d['source_version']['path']);p.write_bytes(p.read_bytes()+b'# changed\n');return result
    monkeypatch.setattr(producer,'produce_inventory',change)
    with pytest.raises(ValueError,match='producer_input_changed'):build_package(**args)
    assert not args['output_root'].exists()


def test_schema2_identity_drift_after_archive_retains_incomplete_output(tmp_path, monkeypatch):
    import tools.build_component_package as producer
    args,d=producer_identity_packet(tmp_path);original=producer.zipfile.ZipFile
    def changed(file,*a,**k):
        result=original(file,*a,**k)
        if isinstance(file,Path) and file.suffix=='.zip' and not a and not k:
            p=Path(d['source_version']['path']);p.write_bytes(p.read_bytes()+b'# drift\n')
        return result
    monkeypatch.setattr(producer.zipfile,'ZipFile',changed)
    with pytest.raises(ValueError,match='producer_input_changed'):build_package(**args)
    assert (args['output_root']/'INCOMPLETE.json').exists()
    assert len(list(args['output_root'].glob('*.zip')))==1


@pytest.mark.parametrize('stamp,allowed', [('2026-10-05T23:30:00-01:00',True),('2026-10-06T23:30:00-01:00',False)])
def test_schema2_publication_offset_uses_declared_date(tmp_path, stamp, allowed):
    args,d=producer_identity_packet(tmp_path);args['published_at']=stamp
    if allowed:assert build_package(**args)['release_schema_version']==2
    else:
        with pytest.raises(ValueError,match='date'):build_package(**args)
        assert not args['output_root'].exists()


def test_schema2_missing_iana_data_holds_without_install_fallback(tmp_path, monkeypatch):
    import zoneinfo
    args,d=producer_identity_packet(tmp_path)
    def missing(*a,**k):raise zoneinfo.ZoneInfoNotFoundError('Synthetic missing IANA data')
    monkeypatch.setattr(zoneinfo,'ZoneInfo',missing)
    version=Path(d['source_version']['path']);version.write_bytes(version.read_bytes().replace(b"'UTC'",b"'America/New_York'"))
    rebind_identity_packet(args,d)
    with pytest.raises(ValueError):build_package(**args)
    assert not args['output_root'].exists()


def test_schema2_cli_keeps_canonical_crlf_notes(tmp_path):
    args,d=producer_identity_packet(tmp_path);argv=[]
    for key,value in args.items():
        if key!='release_notes':argv.extend(('--'+key.replace('_','-'),str(value)))
    argv.extend(('--release-notes-file',d['release_notes']['path']))
    result=subprocess.run([sys.executable,'-B','tools/build_component_package.py',*argv],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    envelope=json.loads((args['output_root']/'release-manifest.input.json').read_bytes())
    assert envelope['release_notes']==args['release_notes'] and '\r\n' in envelope['release_notes']


@pytest.mark.parametrize('wire', ['duplicate','oversized','nonfinite'])
def test_schema2_descriptor_wire_limits_leave_no_output(tmp_path, wire):
    args,d=producer_identity_packet(tmp_path);p=Path(args['identity_inputs'])
    p.write_bytes(b'{"schema_version":1,"schema_version":1}' if wire=='duplicate' else
        b' '*(128*1024+1) if wire=='oversized' else b'{"schema_version":NaN}')
    with pytest.raises(ValueError):build_package(**args)
    assert not args['output_root'].exists()


def payload(tmp_path, version='0.1.0-alpha.9', component='collectigen.suite-manager'):
    root = tmp_path / ('payload-' + version)
    wrapped = root / 'App'
    wrapped.mkdir(parents=True)
    files = {'bin/inert.txt': ('INERT ' + version).encode(), '_internal/MODULE_MANIFEST.json': json.dumps({
        'schema_version': 1, 'module_id': component, 'software_version': version}).encode()}
    for name, content in files.items():
        file = wrapped / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)
    runtime = {'schema_version': 1, 'component': 'manager' if component == COMPONENT.value else 'encyclopedia',
        'feature_version': version, 'os': 'windows', 'architecture': 'x86_64',
        'inventory_excludes_itself': 'RUNTIME_MANIFEST.json', 'GUI_qualified': False,
        'files': [{'path': n, 'size_bytes': len(v), 'sha256': sha256(v).hexdigest()} for n, v in files.items()]}
    (wrapped / 'RUNTIME_MANIFEST.json').write_text(json.dumps(runtime), encoding='utf-8')
    return root


def options(root, output, version='0.1.0-alpha.9', component=COMPONENT.value):
    return dict(payload_root=root, output_root=output, runtime_manifest='App/RUNTIME_MANIFEST.json',
        module_manifest='App/_internal/MODULE_MANIFEST.json', component_id=component, software_version=version,
        os_name='windows', architecture='x86_64', artifact_name='fixture-' + version + '.zip',
        supported_from='0.1.0-alpha.9', published_at='2026-10-06T00:00:00Z', release_notes='INERT fixture only.')


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('component', [COMPONENT.value, 'collectigen.tag-encyclopedia'])
def test_output_has_exact_inventory_and_unsigned_envelope_with_deterministic_bytes(tmp_path, component):
    root = payload(tmp_path, component=component)
    before = snapshot(root)
    first, second = tmp_path / 'one', tmp_path / 'two'
    receipt = build_package(**options(root, first, component=component))
    build_package(**options(root, second, component=component))
    assert snapshot(root) == before
    assert snapshot(first) == snapshot(second)
    assert not (first / 'INCOMPLETE.json').exists()
    assert not receipt['signed'] and not receipt['production_install_authorized'] and not receipt['native_health_verified']
    raw = (first / INVENTORY).read_bytes()
    inventory = json.loads(raw)
    assert [r['path'] for r in inventory['files']] == sorted(before)
    assert INVENTORY not in [r['path'] for r in inventory['files']]
    with zipfile.ZipFile(next(first.glob('*.zip'))) as z:
        assert z.namelist() == sorted((*before, INVENTORY))
        assert z.read(INVENTORY) == raw
        for row in inventory['files']:
            actual = z.read(row['path'])
            assert actual == before[row['path']]
            assert len(actual) == row['size_bytes'] and sha256(actual).hexdigest() == row['sha256']
    envelope = (first / 'release-manifest.input.json').read_bytes()
    verified = verify_component_release(envelope, sign(envelope), TRUST, expected_component=ComponentId(component),
        expected_version='0.1.0-alpha.9', expected_channel='beta', expected_os='windows', expected_architecture='x86_64')
    assert verified.artifact.portable_manifest_sha256 == sha256(raw).hexdigest()
    assert verified.artifact.sha256 == receipt['archive_sha256']


def test_two_generated_inert_versions_qualify_through_canonical_validator(tmp_path):
    inputs = []
    for version in ('0.1.0-alpha.9', '0.1.0-alpha.10'):
        root = payload(tmp_path, version)
        out = tmp_path / ('out-' + version)
        build_package(**options(root, out, version))
        manifest = (out / 'release-manifest.input.json').read_bytes()
        inputs.append(PackageInput(next(out.glob('*.zip')), manifest, sign(manifest)))
    scratch = create_disposable_root(tmp_path / 'synthetic-pair')
    proof = qualify_pair(*inputs, scratch_root=scratch, trust_bytes=TRUST, component_id=COMPONENT,
        prior_version='0.1.0-alpha.9', target_version='0.1.0-alpha.10', os_name='windows', architecture='x86_64')
    assert not proof.production_install_authorized and not proof.native_health_verified
    assert len(proof.prior_member_pins) == len(proof.target_member_pins) == 3
    assert not list(scratch.rglob('offline-qualification-*'))


@pytest.mark.parametrize('problem', ['platform', 'architecture', 'version', 'component', 'wrong_hash', 'extra',
    'missing', 'nested_logs', 'inventory_present', 'duplicate', 'unknown_component', 'builder_platform', 'boolean_schema'])
def test_rejects_mismatched_and_excluded_inputs_before_output_write(tmp_path, problem):
    root = payload(tmp_path)
    runtime_path = root / 'App/RUNTIME_MANIFEST.json'
    runtime = json.loads(runtime_path.read_bytes())
    args = options(root, tmp_path / 'out')
    if problem in ('platform', 'architecture', 'version', 'component'):
        field = {'platform': 'os', 'architecture': 'architecture', 'version': 'feature_version', 'component': 'component'}[problem]
        runtime[field] = {'platform': 'linux', 'architecture': 'aarch64', 'version': '0.1.0-alpha.10', 'component': 'encyclopedia'}[problem]
    elif problem == 'wrong_hash':
        runtime['files'][0]['sha256'] = '0' * 64
    elif problem == 'duplicate':
        runtime['files'].append(runtime['files'][0].copy())
    elif problem in ('extra', 'nested_logs', 'inventory_present'):
        name = {'extra': 'extra.txt', 'nested_logs': 'App/logs/accidental.txt', 'inventory_present': INVENTORY}[problem]
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'INERT')
    elif problem == 'missing':
        (root / 'App/bin/inert.txt').unlink()
    elif problem == 'unknown_component':
        args['component_id'] = 'collectigen.unknown'
    elif problem == 'builder_platform':
        args['os_name'] = 'linux'
    elif problem == 'boolean_schema':
        runtime['schema_version'] = True
    runtime_path.write_text(json.dumps(runtime), encoding='utf-8')
    before = snapshot(root)
    with pytest.raises(ValueError):
        build_package(**args)
    assert snapshot(root) == before and not (tmp_path / 'out').exists()


def test_never_overwrites_output_or_places_output_in_input(tmp_path):
    root = payload(tmp_path)
    existing = tmp_path / 'exists'
    existing.mkdir()
    (existing / 'keep.txt').write_bytes(b'KEEP')
    with pytest.raises(ValueError):
        build_package(**options(root, existing))
    assert (existing / 'keep.txt').read_bytes() == b'KEEP'
    with pytest.raises(ValueError):
        build_package(**options(root, root / 'new-output'))
    assert not (root / 'new-output').exists()


def test_cli_consumes_explicit_fixture_and_emits_unsigned_input(tmp_path):
    root = payload(tmp_path)
    args = options(root, tmp_path / 'cli-out')
    notes = tmp_path / 'fixture-notes.txt'
    notes.write_text(args.pop('release_notes'), encoding='utf-8')
    command = [sys.executable, '-B', 'tools/build_component_package.py']
    for name, value in args.items():
        command.extend(['--' + name.replace('_', '-'), str(value)])
    command.extend(['--release-notes-file', str(notes)])
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)['signed'] is False
    assert (tmp_path / 'cli-out/release-manifest.input.json').exists()


def test_stale_input_addition_is_rejected_before_any_output(tmp_path, monkeypatch):
    import tools.build_component_package as builder
    root = payload(tmp_path)
    original = builder.produce_inventory
    def changed(*args, **kwargs):
        inventory = original(*args, **kwargs)
        (root / 'added-during-build.txt').write_bytes(b'INERT')
        return inventory
    monkeypatch.setattr(builder, 'produce_inventory', changed)
    with pytest.raises(ValueError, match='payload_closure_changed'):
        build_package(**options(root, tmp_path / 'out'))
    assert not (tmp_path / 'out').exists()


def test_failed_archive_readback_retains_incomplete_output_without_touching_input(tmp_path, monkeypatch):
    root = payload(tmp_path)
    before = snapshot(root)
    original = zipfile.ZipFile.writestr
    def corrupted(self, info, content, **kwargs):
        return original(self, info, content + b'CORRUPT', **kwargs)
    monkeypatch.setattr(zipfile.ZipFile, 'writestr', corrupted)
    with pytest.raises(ValueError, match='output_readback'):
        build_package(**options(root, tmp_path / 'out'))
    assert snapshot(root) == before
    assert (tmp_path / 'out/INCOMPLETE.json').exists()
    assert not (tmp_path / 'out/packaging-receipt.json').exists()


@pytest.mark.parametrize('os_name,architecture,suffix,reason', [
    ('linux', 'x86_64', '.tar.gz', 'builder_windows_x64_only'),
    ('linux', 'aarch64', '.tar.gz', 'builder_windows_x64_only'),
    ('linux', 'x86_64', '.zip', 'builder_windows_x64_only'),
    ('windows', 'x86_64', '.tar.gz', 'artifact_basename'),
])
def test_unadmitted_linux_and_tar_formats_stay_disabled_even_with_matching_metadata(
        tmp_path, os_name, architecture, suffix, reason):
    root = payload(tmp_path)
    runtime_path = root / 'App/RUNTIME_MANIFEST.json'
    runtime = json.loads(runtime_path.read_bytes())
    runtime.update(os=os_name, architecture=architecture)
    runtime_path.write_text(json.dumps(runtime), encoding='utf-8')
    before = snapshot(root)
    args = options(root, tmp_path / 'unsupported-output')
    args.update(os_name=os_name, architecture=architecture, artifact_name='inert' + suffix)
    with pytest.raises(ValueError, match=reason):
        build_package(**args)
    assert snapshot(root) == before
    assert not (tmp_path / 'unsupported-output').exists()


def test_dot_dot_output_alias_inside_payload_is_rejected_without_writes(tmp_path):
    root = payload(tmp_path)
    before = snapshot(root)
    anchor = tmp_path / 'sibling'
    anchor.mkdir()
    alias = anchor / '..' / root.name / 'generated-output'
    assert alias.resolve().is_relative_to(root)
    with pytest.raises(ValueError, match='separate_output_required'):
        build_package(**options(root, alias))
    assert snapshot(root) == before
    assert not (root / 'generated-output').exists()


def test_padded_raw_notes_over_canonical_limit_rejected_before_output(tmp_path):
    root = payload(tmp_path)
    before = snapshot(root)
    args = options(root, tmp_path / 'notes-output')
    args['release_notes'] = ' ' * 100000 + 'x'
    with pytest.raises(ValueError, match='release_notes'):
        build_package(**args)
    assert snapshot(root) == before
    assert not (tmp_path / 'notes-output').exists()
