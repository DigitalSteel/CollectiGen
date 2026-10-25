"""Onedir recipe policy using stock source/graph/binary guards; no app launch."""
from pathlib import Path
import json
import ast
import importlib.metadata
import platform
import struct
import sys
import marshal
import re
from types import CodeType

try:
    import build_validators as stock
    from windows_build_isolation import local_graph_dependencies, validate_binary_origins
except ModuleNotFoundError:
    from tools import build_manager_distribution as stock
    from tools.manager_build.windows_build_isolation import local_graph_dependencies, validate_binary_origins

ROLES = {'reader': ('encyclopedia_reader_entry.py', 'TagEncyclopediaReader'),
         'root': ('encyclopedia_root_entry.py', 'TagEncyclopediaRoot')}
FORBIDDEN = {'core.app_state', 'qt_app.main_window', 'suite_core.tests', 'tests', 'PySide6.QtTest'}


def compiled_constant_equal(left, right):
    """Exact typed recursive constants, including IEEE float/complex bit patterns."""
    if type(left) is not type(right): return False
    if isinstance(left, CodeType): return compiled_code_equal(left, right)
    if type(left) is float: return struct.pack('>d', left) == struct.pack('>d', right)
    if type(left) is complex:
        return struct.pack('>dd', left.real, left.imag) == struct.pack('>dd', right.real, right.imag)
    if type(left) is slice:
        return compiled_constant_equal((left.start, left.stop, left.step), (right.start, right.stop, right.step))
    if type(left) in (tuple, list):
        return len(left) == len(right) and all(compiled_constant_equal(a, b) for a, b in zip(left, right))
    if type(left) in (frozenset, set, dict):
        if len(left) != len(right): return False
        remaining = list(right.items() if type(right) is dict else right)
        for item in (left.items() if type(left) is dict else left):
            match = next((index for index, candidate in enumerate(remaining)
                if compiled_constant_equal(item, candidate)), None)
            if match is None: return False
            remaining.pop(match)
        return True
    if type(left) in (type(None), type(Ellipsis), bool, int, str, bytes): return left == right
    return False  # Never invoke custom equality on an unsupported constant object.


def compiled_code_equal(left, right):
    """Compare pinned compilation exactly except the relocated source filename."""
    fields = ('co_argcount', 'co_posonlyargcount', 'co_kwonlyargcount', 'co_nlocals', 'co_stacksize',
        'co_flags', 'co_code', 'co_names', 'co_varnames', 'co_freevars', 'co_cellvars', 'co_name',
        'co_qualname', 'co_firstlineno', 'co_linetable', 'co_exceptiontable')
    if not isinstance(left, CodeType) or not isinstance(right, CodeType): return False
    if any(getattr(left, field) != getattr(right, field) for field in fields): return False
    if len(left.co_consts) != len(right.co_consts): return False
    return compiled_constant_equal(left.co_consts, right.co_consts)


def verify_compiled_sources(image, source, rows, *, entry_source=None):
    """Static PYZ AND actual CArchive startup extraction; never executes code."""
    from PyInstaller.archive.readers import CArchiveReader
    stock.pe_x64(stock.read(image))
    container = CArchiveReader(str(image))
    archive = container.open_embedded_archive('PYZ.pyz')
    admitted_rows = stock.rows(rows)
    scripts = [name for name, info in container.toc.items() if info[-1] == 's']
    candidates = [name for name, bundle in ROLES.values()
        if name in admitted_rows and Path(name).stem in scripts]
    if entry_source is None:
        stock.require(len(candidates) == 1, 'exact_carchive_startup_source_required')
        entry_source = candidates[0]
    stock.require(entry_source in admitted_rows and entry_source in {name for name, bundle in ROLES.values()}
        and scripts and scripts[-1] == Path(entry_source).stem
        and candidates == [entry_source], 'exact_carchive_startup_source_required')
    entry_raw = stock.check_row(source / entry_source, admitted_rows[entry_source])
    expected_entry = compile(entry_raw, entry_source, 'exec', dont_inherit=True, optimize=0)
    actual_entry = marshal.loads(container.extract(Path(entry_source).stem))
    stock.require(compiled_code_equal(actual_entry, expected_entry), 'compiled_carchive_startup_source_mismatch')
    matched = []
    for name, row in admitted_rows.items():
        if not name.endswith('.py'): continue
        raw = stock.check_row(source / name, row)
        module = name[:-3].replace('/', '.').removesuffix('.__init__')
        expected = compile(raw, name, 'exec', dont_inherit=True, optimize=0)
        stock.require(compiled_code_equal(archive.extract(module), expected),
            'compiled_pinned_source_mismatch:' + module)
        matched.append(module)
    return {'status': 'PINNED_COMPILED_SOURCE_READBACK_PASSED', 'modules': sorted(matched),
        'startup_source': entry_source, 'actual_carchive_startup_verified': True, 'archive_code_executed': False}


def admit_local_namespace(name, origin, kind, graph, source, module_rows):
    """Stock namespace nodes have no source file; admit only one pinned directory."""
    node = graph.find_node(name)
    directory = source.joinpath(*name.split('.'))
    descendants = [row for module, row in module_rows.items() if module.startswith(name + '.')]
    stock.require(origin == '-' and kind == 'PYMODULE' and type(node).__name__ == 'NamespacePackage'
        and getattr(node, 'filename', None) == '-' and descendants,
        'admitted_local_namespace_node_required:' + name)
    paths = getattr(node, 'packagepath', None)
    stock.require(type(paths) in (list, tuple) and len(paths) == 1 and type(paths[0]) is str
        and Path(paths[0]).absolute() == directory.absolute(),
        'local_namespace_resolved_outside_admitted_stage:' + name)
    stock.unredirected(directory)
    stock.require(directory.is_dir() and not (directory / '__init__.py').exists(),
        'exact_admitted_namespace_directory_required:' + name)
    for row in descendants: stock.check_row(source / row['path'], row)


def classify_pinned_facade_attributes(missing, source, module_rows, collected):
    """Resolve exact lazy facade symbols only to collected pinned source owners."""
    unresolved, resolved = [], []
    facade = module_rows.get('core')
    if facade is None or 'core' not in collected:
        return list(missing), resolved
    tree = ast.parse(stock.check_row(source / facade['path'], facade))
    declaration = next((node.value for node in tree.body if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == '_EXPORT_GROUPS' for target in node.targets)), None)
    if declaration is None:
        return list(missing), resolved
    groups = ast.literal_eval(declaration)  # Literal metadata only; never executes the facade.
    exports = {public: ('core' + owner, symbol) for owner, entries in groups for public, symbol in entries}
    def global_symbols(statements):
        symbols = set()
        for node in statements:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbols.add(node.name)  # Do not descend into local/class scopes.
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                symbols.update(item.id for target in targets for item in ast.walk(target) if isinstance(item, ast.Name))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                symbols.update(alias.asname or alias.name.split('.')[0] for alias in node.names)
            else:
                for field in ('body', 'orelse', 'finalbody'):
                    symbols.update(global_symbols(getattr(node, field, [])))
                for handler in getattr(node, 'handlers', []): symbols.update(global_symbols(handler.body))
        return symbols
    for name in missing:
        exported = exports.get(name[5:]) if name.startswith('core.') and name.count('.') == 1 else None
        owner, symbol = exported if exported else (None, None)
        row = module_rows.get(owner)
        if owner not in collected or row is None:
            unresolved.append(name)
            continue
        owner_tree = ast.parse(stock.check_row(source / row['path'], row))
        if symbol not in global_symbols(owner_tree.body):
            unresolved.append(name)
            continue
        resolved.append({'warning': name, 'owner': owner, 'symbol': symbol, 'owner_sha256': row['sha256']})
    return unresolved, resolved


def admit_current_toolchain(root, config):
    raw = stock.pinned(root / 'TOOLCHAIN.json', config['toolchain_sha256'])
    value = stock.decode(raw)
    stock.require(value.get('purpose') == config['purpose'], 'toolchain_stage_purpose_mismatch')
    python = value['python']
    image = stock.pinned(Path(sys.executable), python['sha256'])
    stock.require(len(image) == python['bytes'] and sys.platform == 'win32'
        and struct.calcsize('P') == 8 and platform.machine().casefold() in {'amd64', 'x86_64'}
        and platform.python_version() == python['version'], 'actual_toolchain_interpreter_drift')
    for name, version in value['distributions'].items():
        stock.require(importlib.metadata.version(name) == version, 'actual_toolchain_wheel_drift:' + name)
    return value


def build_recipe(role, namespace):
    stock.require(role in ROLES, 'fixed_encyclopedia_recipe_role_required')
    original = Path(namespace['SPECPATH']).absolute()
    stock.unredirected(original)
    root = original.resolve()
    stock.require(root == original, 'original_stage_path_required')
    config = stock.decode(stock.read(root / 'STAGE.json'))
    normal = config.get('purpose') == 'ENCYCLOPEDIA_NORMAL_PORTABLE_BUILD_ONLY'
    stock.require(config.get('purpose') in ('ENCYCLOPEDIA_QUALIFICATION_BUILD_ONLY',
        'ENCYCLOPEDIA_NORMAL_PORTABLE_BUILD_ONLY')
        and config.get('component_id') == 'collectigen.tag-encyclopedia', 'exact_encyclopedia_stage_required')
    stock.require((normal and config.get('profile_kind') == 'normal-portable'
        and config.get('profile_bound') is True and config.get('fixture_profile_sha256') is None
        and type(config.get('normal_profile_sha256')) is str
        and re.fullmatch('[0-9a-f]{64}', config['normal_profile_sha256'])
        and config.get('include_python_sources') is False)
        or (not normal and 'profile_kind' not in config and 'normal_profile_sha256' not in config),
        'exact_stage_profile_mode_required')
    admit_current_toolchain(root, config)
    source = root / 'source'
    rows = stock.rows(config['source_rows'])
    generated = stock.rows(config['generated_rows'])
    expected_generated = {'encyclopedia_root_entry.py', 'encyclopedia_reader_entry.py'}
    if config['profile_bound']:
        expected_generated.add('encyclopedia_compiled_profile.py' if normal else 'encyclopedia_fixture_profile.py')
    stock.require(set(generated) == expected_generated, 'exact_generated_entry_membership_required')
    stock.require(not set(rows) & set(generated), 'generated_source_row_collision')
    # Pinned rows cannot hide an undeclared stage file or swapped profile mode.
    expected_files = {'STAGE.json', 'TOOLCHAIN.json'} | {'source/' + n for n in (*rows, *generated)}
    expected_files |= set(stock.rows(config['build_rows']))
    actual_files = set()
    for path in root.rglob('*'):
        stock.unredirected(path)
        if path.is_file(): actual_files.add(path.relative_to(root).as_posix())
    stock.require(actual_files == expected_files, 'undeclared_or_missing_recipe_stage_member')
    for name, row in {**rows, **generated}.items():
        stock.unredirected(source / name)
        stock.check_row(source / name, row)
    for name, row in stock.rows(config['build_rows']).items():
        stock.check_row(root / name, row)
    all_rows = {**rows, **generated}
    allowed = {name[:-3].replace('/', '.').removesuffix('.__init__') for name in all_rows if name.endswith('.py')}
    allowed |= {'.'.join(name.split('.')[:i]) for name in tuple(allowed) for i in range(1, len(name.split('.')))}
    stock.require(not any(any(name == f or name.startswith(f + '.') for f in FORBIDDEN) for name in allowed),
        'private_or_whole_application_source_refused')
    # The admitted stager records repository namespaces from the accepted public
    # inventory, before runtime/subset filtering. Runtime-declaration omissions
    # such as runtime_contract must remain local in PyInstaller warnings too.
    repository_tops = config.get('repository_local_top_levels')
    stock.require(type(repository_tops) is list and repository_tops
        and all(type(name) is str and name.isidentifier() for name in repository_tops)
        and repository_tops == sorted(set(repository_tops)),
        'accepted_repository_namespace_registry_required')
    module = stock.decode(stock.read(source / 'suite_core/encyclopedia/MODULE_MANIFEST.json'))
    declared_tops = {name.split('/')[0].removesuffix('.py') for name in module['runtime_files'] if name.endswith('.py')}
    tops = set(repository_tops) | declared_tops | {name.partition('.')[0] for name in allowed}
    module_rows = {name[:-3].replace('/', '.').removesuffix('.__init__'): row
        for name, row in all_rows.items() if name.endswith('.py')}
    entry, bundle = ROLES[role]
    include_sources = config.get('include_python_sources', True)
    stock.require(type(include_sources) is bool, 'explicit_python_source_payload_policy_required')
    datas = [(str(source / name), str(Path(name).parent)) for name in all_rows
        if include_sources or not name.endswith('.py')]
    analysis = namespace['Analysis']([str(source / entry)], pathex=[str(source)], binaries=[], datas=datas,
        hiddenimports=sorted(allowed | {'PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets', 'shiboken6'}),
        hookspath=[], runtime_hooks=[], excludes=sorted(FORBIDDEN | {'PyQt5', 'PyQt6', 'PySide2'}), noarchive=False)
    namespaces = []
    for name, origin, kind in analysis.pure:
        if any(name == f or name.startswith(f + '.') for f in FORBIDDEN):
            raise stock.BuildHold('excluded_application_or_test_module_collected:' + name)
        if name.partition('.')[0] in tops and name not in allowed:
            raise stock.BuildHold('undeclared_local_module_collected:' + name)
        if name.partition('.')[0] in tops:
            if name not in module_rows:
                admit_local_namespace(name, origin, kind, analysis.graph, source, module_rows)
                namespaces.append(name)
            else:
                stock.require(type(origin) is str, 'admitted_local_module_origin_required:' + name)
                row = module_rows[name]
                admitted = source / row['path']
                stock.unredirected(admitted)
                stock.require(Path(origin).absolute() == admitted.absolute(),
                    'local_module_resolved_outside_admitted_stage:' + name)
                stock.check_row(Path(origin), row)
        if origin and Path(origin).is_file() and Path(origin).resolve().is_relative_to(source):
            stock.require(Path(origin).resolve().relative_to(source).as_posix() in all_rows,
                'undeclared_source_origin')
    missing, excluded = local_graph_dependencies(analysis.graph, tops)
    unexpected = set(excluded) - FORBIDDEN
    stock.require(not unexpected, 'unclassified_local_exclusions')
    actual = {name for name, origin, kind in analysis.pure}
    missing, facade_attributes = classify_pinned_facade_attributes(missing, source, module_rows, actual)
    missing_declared = sorted(allowed - actual)
    report = dict(purpose=config['purpose'], role=role,
        status='ANALYSIS_MODULE_BOUND_PASSED' if not missing and not missing_declared else 'ANALYSIS_MODULE_BOUND_INSUFFICIENT',
        source_rows=len(rows), generated_rows=len(generated), missing_local_modules=missing,
        missing_declared_modules=missing_declared, actual_pure_modules=sorted(actual),
        excluded_local_modules=excluded, product_apps_started=False)
    report['admitted_local_namespace_modules'] = sorted(namespaces)
    report['admitted_pinned_facade_attributes'] = facade_attributes
    report['include_python_sources'] = include_sources
    stock.write_new(root.parent / (bundle + '_ANALYSIS_CLOSURE.json'), stock.json_bytes(report))
    stock.require(not missing and not missing_declared, 'encyclopedia_analysis_closure_insufficient')
    validate_binary_origins(analysis.binaries)  # same interpreter/base/system roots, no relaxed SDK origin
    # Preserve stock existing-system MSVC policy; no dependency installation.
    msvc = {'msvcp140_1.dll', 'msvcp140_2.dll', 'msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'}
    removed = [row for row in analysis.binaries if Path(row[0]).name.casefold() in msvc]
    analysis.binaries = [row for row in analysis.binaries if Path(row[0]).name.casefold() not in msvc]
    stock.write_new(root.parent / (bundle + '_SYSTEM_MSVC_POLICY.json'), stock.json_bytes(dict(
        policy='existing-system-prerequisite', minimum_version='14.44.35211.0', removed=removed,
        static_dependency_review_only=True)))
    pyz = namespace['PYZ'](analysis.pure)
    executable = namespace['EXE'](pyz, analysis.scripts, [], exclude_binaries=True,
        name='TagEncyclopedia', debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
        console=False, disable_windowed_traceback=True, contents_directory='_internal')
    collection = namespace['COLLECT'](executable, analysis.binaries, analysis.datas,
        strip=False, upx=False, name=bundle)
    if not include_sources:
        compiled = verify_compiled_sources(Path(namespace['DISTPATH']) / bundle / 'TagEncyclopedia.exe',
            source, list(all_rows.values()), entry_source=entry)
        stock.write_new(root.parent / (bundle + '_COMPILED_SOURCE_READBACK.json'), stock.json_bytes(compiled))
    return collection
