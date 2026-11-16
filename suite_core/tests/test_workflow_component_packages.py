"""Generate/Train reuse inert frozen packages and the default-free source exporter."""
from hashlib import sha256
import json
import zipfile

import pytest

from tools import public_source as exporter
from tools.build_component_package import build_package
from suite_core.discovery.component_payload_policy import WORKFLOW_FROZEN_PAYLOAD_POLICY, select_payload_policy
from suite_core.discovery.component_release_signature import verify_component_release
from suite_core.installation import ComponentId
from suite_core.tests.test_component_staging import TRUST, sign


@pytest.fixture(params=['generate', 'train'])
def workflow(request):
    return request.param


def frozen_input(tmp_path, workflow, *, module_id=None, module_version='0.1.0-alpha.1', runtime_name=None):
    root = tmp_path / 'payload'
    root.mkdir()
    module = json.dumps({'schema_version': 1, 'module_id': module_id or 'collectigen.' + workflow,
                         'software_version': module_version}).encode()
    (root / 'MODULE_MANIFEST.json').write_bytes(module)
    runtime = {'schema_version': 1, 'component': runtime_name or workflow,
               'feature_version': '0.1.0-alpha.1', 'os': 'windows', 'architecture': 'x86_64',
               'inventory_excludes_itself': 'RUNTIME_MANIFEST.json', 'GUI_qualified': False,
               'files': [{'path': 'MODULE_MANIFEST.json', 'size_bytes': len(module),
                          'sha256': sha256(module).hexdigest()}]}
    (root / 'RUNTIME_MANIFEST.json').write_text(json.dumps(runtime), encoding='utf-8')
    return dict(payload_root=root, output_root=tmp_path / 'package',
                runtime_manifest='RUNTIME_MANIFEST.json', module_manifest='MODULE_MANIFEST.json',
                component_id='collectigen.' + workflow, software_version='0.1.0-alpha.1',
                os_name='windows', architecture='x86_64', artifact_name=workflow + '.zip',
                supported_from='0.1.0-alpha.1', published_at='2026-11-15T00:00:00Z',
                release_notes='Synthetic package; no native qualification.')


def test_workflow_package_uses_existing_frozen_inventory_and_signed_envelope(tmp_path, workflow):
    args = frozen_input(tmp_path, workflow)
    component = ComponentId(args['component_id'])
    assert select_payload_policy(component, 'windows', 'x86_64') is WORKFLOW_FROZEN_PAYLOAD_POLICY
    receipt = build_package(**args)
    output = args['output_root']
    raw = (output / 'release-manifest.input.json').read_bytes()
    release = verify_component_release(raw, sign(raw), TRUST, expected_component=component,
        expected_version='0.1.0-alpha.1', expected_channel='beta', expected_os='windows',
        expected_architecture='x86_64')
    archive = output / args['artifact_name']
    assert release.artifact.sha256 == sha256(archive.read_bytes()).hexdigest()
    with zipfile.ZipFile(archive) as package:
        assert json.loads(package.read('MODULE_MANIFEST.json'))['module_id'] == component.value
        assert json.loads(package.read('RUNTIME_MANIFEST.json'))['component'] == workflow
    assert not receipt['signed'] and not receipt['production_install_authorized']
    assert not receipt['native_health_verified']
    with pytest.raises(ValueError, match='unsupported_payload_policy'):
        select_payload_policy(component, 'windows', 'x86_64', 'interpreter-portable')


@pytest.mark.parametrize('defect', ['module_id', 'module_version', 'runtime_name', 'unknown_id'])
def test_workflow_package_refuses_wrong_identity_before_output(tmp_path, workflow, defect):
    replacements = {'module_id': {'module_id': 'collectigen.suite-manager'},
                    'module_version': {'module_version': '0.1.0-alpha.2'},
                    'runtime_name': {'runtime_name': 'manager'}, 'unknown_id': {}}
    args = frozen_input(tmp_path, workflow, **replacements[defect])
    if defect == 'unknown_id':
        args['component_id'] = 'collectigen.unknown'
    with pytest.raises(ValueError):
        build_package(**args)
    assert not args['output_root'].exists()


def source_input(tmp_path, workflow, *, identity=None):
    root = tmp_path / 'source'
    path = root / f'suite_core/{workflow}/MODULE_MANIFEST.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(identity if identity is not None else {
        'schema_version': 1, 'module_id': 'collectigen.' + workflow,
        'software_version': '0.1.0-alpha.1'}), encoding='utf-8')
    manifest = tmp_path / 'source-manifest.json'
    manifest.write_text(json.dumps({'schema_version': 1, 'export_scope': 'component-source',
        'component_ids': ['collectigen.' + workflow], 'files': [path.relative_to(root).as_posix()],
        'reviewed_defaults': [], 'required_provenance': []}), encoding='utf-8')
    return root, manifest


def test_workflow_source_export_preserves_stock_scan_inventory_and_default_exclusion(tmp_path, workflow):
    root, manifest = source_input(tmp_path, workflow)
    model = exporter.load_manifest(manifest)
    _, _, digest = exporter.prepare_files(model, exporter._tree_reader(root))
    output = tmp_path / 'public'
    exporter.build_public_source(root, output, manifest, digest,
                                completeness_check=lambda: [], working_tree=True)
    assert exporter.verify_public_inventory(output) == []
    assert exporter.scan_public_tree(output) == []
    assert json.loads((output / 'public-source.json').read_bytes())['component_ids'] == ['collectigen.' + workflow]
    assert json.loads((output / 'data/defaults/public-defaults.json').read_bytes())['files'] == {}


@pytest.mark.parametrize('defect', ['module_id', 'software_version', 'boolean_version', 'schema_version'])
def test_workflow_source_refuses_malformed_identity_before_export(tmp_path, workflow, defect):
    identity = {'schema_version': 1, 'module_id': 'collectigen.' + workflow,
                'software_version': '0.1.0-alpha.1'}
    field = 'software_version' if defect == 'boolean_version' else defect
    identity[field] = {'module_id': 'collectigen.suite-manager', 'software_version': 'a.0.1.0',
                       'boolean_version': True, 'schema_version': True}[defect]
    root, manifest = source_input(tmp_path, workflow, identity=identity)
    model = exporter.load_manifest(manifest)
    with pytest.raises(exporter.PublicSourceError, match='identity mismatch'):
        exporter.prepare_files(model, exporter._tree_reader(root))


def test_workflow_source_requires_exact_manifest_directory(tmp_path, workflow):
    _, manifest = source_input(tmp_path, workflow)
    value = json.loads(manifest.read_bytes())
    value['files'] = ['suite_core/manager/MODULE_MANIFEST.json']
    manifest.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(exporter.PublicSourceError, match='exact component manifests'):
        exporter.load_manifest(manifest)



def test_module_closure_contains_every_bound_dataset_worker_source(workflow):
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    def declared_names(file, assignment, function=None):
        tree = ast.parse((root / file).read_text(encoding='utf-8'))
        if function is not None:
            tree = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function)
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == assignment for t in n.targets))
        generator = node.value.args[0]
        return ast.literal_eval(generator.generators[0].iter)
    names = set(declared_names('core/training_preflight.py', 'code', '_run'))
    names.update(declared_names('core/training_dataset_binding.py', 'PACKAGE_CODE'))
    tree = ast.parse((root / 'core/training_source_loader.py').read_text(encoding='utf-8'))
    declaration = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == 'VERIFIER_MODULES' for t in n.targets))
    names.update(name + '.py' for name in ast.literal_eval(declaration.value))
    required = {'core/' + name for name in names}
    required.update({'tools/training_dataset_profile_worker.py', 'tools/training_verification_worker.py',
        'tools/training_runtime_probe.py', 'tools/training_runtime_sources.json',
        'tools/training_dataset_package.json', 'version.py'})
    # The restricted-loading contract also imports policy modules. Freezer
    # analysis follows those imports even when this worker captures source bytes.
    # Walk this concrete local root without importing or exercising native APIs.
    pending = ['core/training_restricted_loading_contract.py']
    seen = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        required.add(name)
        for node in ast.walk(ast.parse((root / name).read_text(encoding='utf-8'))):
            if isinstance(node, ast.ImportFrom):
                if node.level == 1:
                    module = 'core.' + (node.module or '')
                elif node.level == 0:
                    module = node.module or ''
                else:
                    continue
                paths = ([module.replace('.', '/') + '.py'] if module != 'core.' else
                         ['core/' + alias.name + '.py' for alias in node.names])
            elif isinstance(node, ast.Import):
                paths = [alias.name.replace('.', '/') + '.py' for alias in node.names]
            else:
                continue
            pending.extend(path for path in paths if path.startswith('core/')
                           and (root / path).is_file() and path not in seen)
    assert len(required) > 40  # Covers the original declared closure and local dependencies.
    manifest = json.loads((root / ('tools/workflow_build/' + workflow + '-source-manifest.json')).read_bytes())
    runtime = json.loads((root / ('suite_core/' + workflow + '/MODULE_MANIFEST.json')).read_bytes())
    assert not required.difference(runtime['runtime_files']), sorted(required.difference(runtime['runtime_files']))
    rows = {row['path']: row for row in manifest['source_rows']}
    assert not required.difference(rows), sorted(required.difference(rows))
    for name in required:
        raw = (root / name).read_bytes()
        assert rows[name]['bytes'] == len(raw) and rows[name]['sha256'] == sha256(raw).hexdigest(), name
