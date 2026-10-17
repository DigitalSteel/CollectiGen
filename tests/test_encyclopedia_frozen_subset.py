"""Inert AST subset review helpers/tests; no imports of application modules."""
import ast
from hashlib import sha256
import json
from pathlib import Path

import pytest
from tools import build_manager_distribution as stock
from tools.manager_build.windows_build_isolation import local_graph_dependencies


def static_dependencies(name, raw, registered, *, unadmitted=None):
    """Conservative syntax closure; attributes are not mistaken for modules."""
    tree = ast.parse(raw.decode('utf-8-sig'))
    package = name.split('/')[:-1]
    targets, external, dynamic = set(), set(), []
    tops = {p.split('/')[0].removesuffix('.py') for p in registered if p.endswith('.py')}
    def resolve(parts, required):
        if not parts: return
        stem = '/'.join(parts)
        candidates = [stem + '.py', stem + '/__init__.py']
        found = next((p for p in candidates if p in registered), None)
        if found:
            targets.add(found)
            for i in range(1, len(parts)):
                parent = '/'.join(parts[:i]) + '/__init__.py'
                if parent in registered: targets.add(parent)
        elif required and parts[0] in tops:
            if unadmitted is None:
                raise ValueError('local_import_not_admitted:' + name + ':' + '.'.join(parts))
            unadmitted.append({'importer': name, 'module': '.'.join(parts)})
        elif required: external.add('.'.join(parts))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names: resolve(alias.name.split('.'), True)
        elif isinstance(node, ast.ImportFrom):
            parts = (package[:len(package) - node.level + 1] if node.level else [])
            parts += node.module.split('.') if node.module else []
            resolve(parts, True)
            for alias in node.names:
                if alias.name != '*': resolve(parts + alias.name.split('.'), False)
        elif isinstance(node, ast.Call):
            called = ast.unparse(node.func)
            if called.endswith(('import_module', '__import__', 'find_spec')):
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    literal = node.args[0].value
                    if not literal.startswith('.'): resolve(literal.split('.'), True)
                    else: dynamic.append({'line': node.lineno, 'call': ast.unparse(node)})
                else: dynamic.append({'line': node.lineno, 'call': ast.unparse(node)})
    return targets, external, dynamic


def derive_subset(source, seeds, resources):
    source = Path(source)
    inventory = stock.decode(stock.read(source / 'public-source.json'))['files']
    module = stock.decode(stock.read(source / 'suite_core/encyclopedia/MODULE_MANIFEST.json'))
    declared = set(module['runtime_files'])
    registered = set(inventory)
    selected, parents, external, dynamic = set(), {}, set(), {}
    unadmitted = []
    undeclared = set()
    pending = list(seeds)
    while pending:
        name = pending.pop()
        if name in selected: continue
        if name.endswith('.py') and name not in declared:
            undeclared.add(name)
            continue  # Retain the gap; never expand runtime authority.
        stock.require(name in inventory and (not name.endswith('.py') or name in declared),
            'public_runtime_source_required:' + name)
        public = inventory[name]
        raw = stock.check_row(source / name, {'path': name, 'bytes': public['size_bytes'], 'sha256': public['sha256']})
        selected.add(name)
        if name.endswith('.py'):
            dependencies, outside, calls = static_dependencies(name, raw, registered, unadmitted=unadmitted)
            external.update(outside)
            if calls: dynamic[name] = calls
            for dependency in dependencies:
                parents.setdefault(dependency, set()).add(name)
                pending.append(dependency)
    selected.update(resources)
    rows = []
    for name in sorted(selected):
        public = inventory[name]
        row = {'path': name, 'bytes': public['size_bytes'], 'sha256': public['sha256']}
        stock.check_row(source / name, row)
        rows.append(row)
    return {'schema': 1, 'source_rows': rows}, {
        'seeds': sorted(seeds), 'resource_rows': sorted(resources),
        'imported_by': {name: sorted(values) for name, values in sorted(parents.items())},
        'external_import_names': sorted(external), 'dynamic_import_calls': dynamic,
        'unadmitted_local_imports': unadmitted, 'runtime_undeclared_dependencies': sorted(undeclared)}


def validate_static_subset(source, descriptor, *, excluded_modules=(), evidence=None):
    rows = stock.rows(descriptor['source_rows'])
    source = Path(source)
    inventory = stock.decode(stock.read(source / 'public-source.json'))['files']
    module = stock.decode(stock.read(source / 'suite_core/encyclopedia/MODULE_MANIFEST.json'))
    declared = set(module['runtime_files'])
    missing = set()
    for name, row in rows.items():
        stock.require(inventory.get(name) == {'size_bytes': row['bytes'], 'sha256': row['sha256']},
            'subset_differs_from_accepted_export:' + name)
        data = stock.check_row(source / name, row)
        if name.endswith('.py'):
            stock.require(name in declared, 'subset_python_not_runtime_declared:' + name)
            unadmitted = []
            dependencies, _, _ = static_dependencies(name, data, set(inventory), unadmitted=unadmitted)
            missing.update(item['module'].replace('.', '/') + '.py' for item in unadmitted)
            missing.update(dependencies - rows.keys())
    class MissingGraph:
        def make_missing_toc(self):
            return [(repr(name[:-3].replace('/', '.').removesuffix('.__init__')), None, 'MISSING') for name in missing]
        def find_node(self, name):
            class ExcludedModule: pass
            return ExcludedModule() if name.strip("'\"") in excluded_modules else object()
    tops = {p.split('/')[0].removesuffix('.py') for p in inventory if p.endswith('.py')}
    absent, excluded = local_graph_dependencies(MissingGraph(), tops)
    result = {'source_rows': len(rows), 'missing_local_modules': absent, 'excluded_local_modules': excluded}
    if evidence is not None: evidence.update(result)
    stock.require(not absent and set(excluded) <= set(excluded_modules),
        'static_subset_local_dependency_missing:' + ','.join(absent))
    return result


def synthetic_source(tmp_path):
    source = tmp_path / 'source'; source.mkdir()
    files = {'suite_core/__init__.py': b'', 'suite_core/encyclopedia/__init__.py': b'',
        'suite_core/encyclopedia/entry.py': b'from . import leaf\nfrom release_contract import sha256_file\n',
        'suite_core/encyclopedia/leaf.py': b'import json\n', 'release_contract.py': b'def sha256_file(): pass\n'}
    module = {'runtime_files': list(files)}
    files['suite_core/encyclopedia/MODULE_MANIFEST.json'] = json.dumps(module).encode()
    for name, data in files.items():
        path = source / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
    inventory = {name: {'size_bytes': len(data), 'sha256': sha256(data).hexdigest()} for name, data in files.items()}
    (source / 'public-source.json').write_text(json.dumps({'files': inventory}))
    return source


def test_static_subset_derives_relative_standalone_and_package_dependencies(tmp_path):
    source = synthetic_source(tmp_path)
    subset, rationale = derive_subset(source, {'suite_core/encyclopedia/entry.py'},
        {'suite_core/encyclopedia/MODULE_MANIFEST.json'})
    assert {r['path'] for r in subset['source_rows']} == set(json.loads((source / 'public-source.json').read_bytes())['files'])
    assert validate_static_subset(source, subset)['missing_local_modules'] == []
    assert rationale['external_import_names'] == ['json']


def test_static_subset_holds_omitted_top_level_dependency(tmp_path):
    source = synthetic_source(tmp_path)
    subset, _ = derive_subset(source, {'suite_core/encyclopedia/entry.py'}, set())
    subset['source_rows'] = [r for r in subset['source_rows'] if r['path'] != 'release_contract.py']
    with pytest.raises(stock.BuildHold, match='release_contract'):
        validate_static_subset(source, subset)


def test_static_subset_holds_changed_admitted_bytes(tmp_path):
    source = synthetic_source(tmp_path)
    subset, _ = derive_subset(source, {'suite_core/encyclopedia/entry.py'}, set())
    (source / 'release_contract.py').write_bytes(b'CHANGED')
    with pytest.raises(stock.BuildHold, match='input_size_changed|input_digest_changed'):
        validate_static_subset(source, subset)


def test_static_subset_holds_unadmitted_local_import():
    with pytest.raises(ValueError, match='local_import_not_admitted'):
        static_dependencies('core/entry.py', b'import core.not_admitted\n', {'core/entry.py'})


def test_dynamic_imports_remain_explicit_review_obligations():
    dependencies, external, calls = static_dependencies('core/entry.py',
        b'import importlib\nimportlib.import_module(chosen_name)\n', {'core/entry.py'})
    assert not dependencies and external == {'importlib'} and calls[0]['line'] == 2


def test_derivation_retains_runtime_declaration_gap_without_enrolling_it(tmp_path):
    source = synthetic_source(tmp_path)
    path = source / 'suite_core/encyclopedia/MODULE_MANIFEST.json'
    module = json.loads(path.read_bytes()); module['runtime_files'].remove('release_contract.py')
    raw = json.dumps(module).encode(); path.write_bytes(raw)
    inventory = json.loads((source / 'public-source.json').read_bytes())
    inventory['files']['suite_core/encyclopedia/MODULE_MANIFEST.json'] = {'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()}
    (source / 'public-source.json').write_text(json.dumps(inventory))
    subset, rationale = derive_subset(source, {'suite_core/encyclopedia/entry.py'}, set())
    assert rationale['runtime_undeclared_dependencies'] == ['release_contract.py']
    assert 'release_contract.py' not in {r['path'] for r in subset['source_rows']}
    with pytest.raises(stock.BuildHold, match='release_contract'):
        validate_static_subset(source, subset)
