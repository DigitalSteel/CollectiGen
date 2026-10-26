"""Local affected-check evidence; never a release approval or automatic retry."""
from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import platform
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from tools import test_suites

SOURCE_ROOTS = ('core', 'qt_app', 'tests', 'tools', 'docs', 'assets', 'licenses', '.github')
SOURCE_CATALOG_FILES = ('data/model_workflow_baseline.json',)
AGENT_SKILL_FILES = tuple(
    f'.agents/skills/{name}/SKILL.md'
    for name in ('collectigen-verify', 'collectigen-ui-review')
)
STATIC_EXTERNAL_IMPORTS = frozenset({
    '__future__', 'collections', 'copy', 'dataclasses', 'datetime', 'decimal',
    'enum', 'functools', 'hashlib', 'itertools', 'json', 'math', 'operator',
    'os', 'pathlib', 'pytest', 're', 'shutil', 'statistics', 'string', 'sys',
    'tempfile', 'textwrap', 'time', 'types', 'typing', 'unittest', 'uuid',
})
STATIC_EXTERNAL_FROM = {
    '__future__': {'annotations'},
    'pathlib': {'Path', 'PurePath', 'PureWindowsPath'},
    'pytest': {'fixture', 'mark', 'raises'},
}
STATIC_EXTERNAL_ATTRS = {
    'json': {'dumps', 'loads'},
    'os': {'environ', 'getpid', 'path'},
    'pathlib': {'Path', 'PurePath', 'PureWindowsPath', 'resolve', 'parent', 'mkdir',
                'name', 'stem', 'suffix', 'joinpath', 'as_posix', 'with_name',
                'with_suffix', 'parts', 'is_absolute'},
    'pytest': {'fixture', 'mark', 'parametrize', 'raises', 'skip', 'fail'},
    'shutil': {'rmtree'},
    'sys': {'version', 'executable'},
}
PRIVATE_DEFAULTS = {'promotion_exclusions.json', 'promotion_manifest.json'}
ROOT_SOURCE_FILES = frozenset({
    'AGENTS.md','README.md','CHANGELOG.md','CONTRIBUTING.md','FUNDING.md','LICENSE','LICENSES.md',
    'PRIVACY.md','SECURITY.md','SUPPORT.md','THIRD_PARTY_NOTICES.md','WINDOWS_PREREQUISITES.md',
    '.gitignore','.gitattributes','pytest.ini','conftest.py','version.py','qt_main.py','qt_visual_test.py',
    'runtime_contract.py','release_contract.py','migration_audit.py','Start_CollectiGen.bat',
    'requirements.txt','requirements-runtime.lock','requirements-dev.txt',
    'test_core.py','test_generation_adapters.py','test_image_knowledge.py',
})


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root).decode('utf-8').strip()


def source_path(root: Path, relative: str) -> Path:
    parts = Path(relative).parts
    if not parts:
        raise ValueError('Empty source path.')
    allowed = (relative in ROOT_SOURCE_FILES or relative in SOURCE_CATALOG_FILES
               or relative in AGENT_SKILL_FILES or parts[0] in SOURCE_ROOTS)
    allowed |= len(parts) >= 3 and parts[:2] == ('data', 'defaults') and parts[-1] not in PRIVATE_DEFAULTS
    allowed |= relative in {'.gitignore', '.gitattributes'}
    if not allowed or '..' in parts or Path(relative).is_absolute():
        raise ValueError('Checkpoint scope includes a protected or unsupported source boundary.')
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise ValueError('Checkpoint source must not traverse a symlink or junction.')
    return path


def changed_paths(root: Path, base: str) -> tuple[str, ...]:
    raw = subprocess.check_output(['git', 'diff', '--name-only', '-z', base, '--'], cwd=root)
    return tuple(sorted(p.decode('utf-8') for p in raw.split(b'\0') if p))


def snapshot(root: Path, explicit: tuple[str, ...]) -> dict:
    # Query source pathspecs only; never enumerate ignored or working user data.
    raw = subprocess.check_output(['git', 'ls-files', '-z', '--', *SOURCE_ROOTS, *SOURCE_CATALOG_FILES,
                                  *AGENT_SKILL_FILES, *ROOT_SOURCE_FILES,
                                  'data/defaults', ':!data/defaults/promotion_exclusions.json',
                                  ':!data/defaults/promotion_manifest.json'], cwd=root)
    paths = set(explicit)
    for item in raw.split(b'\0'):
        if item:
            relative = item.decode('utf-8')
            try:
                source_path(root, relative)
            except ValueError as exc:
                if 'symlink' in str(exc) or 'junction' in str(exc):
                    raise
                continue
            paths.add(relative)
    hashes = {}
    for relative in sorted(paths):
        path = source_path(root, relative)
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else 'absent'
    encoded = json.dumps(hashes, sort_keys=True).encode()
    return {'head': git(root, 'rev-parse', 'HEAD'),
            'source_sha256': hashlib.sha256(encoded).hexdigest(), 'file_count': len(hashes), 'files': hashes}


def execute(command: list[str], root: Path, log: Path, timeout: float) -> dict:
    env = test_environment()
    started = time.monotonic()
    options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
    with log.open('wb') as stream:
        process = subprocess.Popen(command, cwd=root, env=env, stdout=stream, stderr=subprocess.STDOUT, **options)
        try:
            code = process.wait(timeout=timeout)
            stream.flush()
            clean_new_file = (code == 1 and command[0] == 'git' and '--no-index' in command
                              and '--check' in command and all(
                                  line.startswith(b'warning:') for line in log.read_bytes().splitlines()))
            status = 'passed' if code == 0 or clean_new_file else 'failed'
        except subprocess.TimeoutExpired:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], stdout=stream, stderr=subprocess.STDOUT, check=True)
            else:
                os.killpg(process.pid, signal.SIGKILL)
            code = process.wait(timeout=10)
            status = 'timed_out'
        except BaseException:
            if process.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], stdout=stream, stderr=subprocess.STDOUT, check=True)
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
            raise
    return {'command': command, 'status': status, 'exit_code': code,
            'duration_seconds': round(time.monotonic()-started, 3), 'log': log.name}


def run(paths=(), *, root=test_suites.ROOT, base='HEAD', timeout=300, fresh=False,
        knowledge_only=False, knowledge_reviews=None, owned_only=False) -> tuple[int, Path]:
    root = Path(root).resolve()
    base = git(root, 'rev-parse', '--verify', '--end-of-options', base+'^{commit}')
    owned = test_suites.normalize_requested_paths(paths, root)
    if owned_only and not owned:
        raise ValueError('Owned-only checkpoint needs at least one explicit source path.')
    explicit = test_suites.normalize_requested_paths([*owned, *([knowledge_reviews] if knowledge_reviews else [])], root)
    changed = changed_paths(root, base)
    requested = explicit if owned_only else tuple(sorted(set(changed) | set(explicit)))
    for relative in requested:
        source_path(root, relative)
    omitted = []
    protected_omitted = 0
    if owned_only:
        for relative in sorted(set(changed) - set(requested)):
            try:
                source_path(root, relative)
            except ValueError:
                protected_omitted += 1
            else:
                omitted.append(relative)
    for directory in (root/'.tmp', root/'.tmp/test-checkpoints'):
        if directory.is_symlink() or getattr(directory, 'is_junction', lambda: False)():
            raise ValueError('Checkpoint evidence must stay in the repository.')
    previous = reusable_checks(root)
    report_dir = root / '.tmp' / 'test-checkpoints' / uuid.uuid4().hex[:12]
    report_dir.mkdir(parents=True, exist_ok=False)
    report_path = report_dir / 'report.json'
    report = {'schema_version': 1, 'base_commit': base, 'status': 'running',
              'release_qualified': False, 'automatic_retries': False, 'checks': [],
              'scope': {'mode': 'owned_only' if owned_only else 'all_tracked',
                        'requested_paths': list(requested), 'omitted_tracked_source_paths': omitted,
                        'omitted_protected_path_count': protected_omitted},
              'untracked_policy': 'New source/test files must be named explicitly; ignored/user data is not discovered.'}
    def save():
        report_path.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    save()
    try:
        report['before'] = snapshot(root, requested)
        plan = test_suites.plan_for_paths(requested, root)
        report['plan'] = plan
        errors = list(test_suites.validate_suite_manifest(root))
        errors += ['Unmapped changed paths: '+', '.join(plan['unknown_paths'])] if plan['unknown_paths'] else []
        if errors:
            report.update(status='blocked', errors=errors)
            save()
            return 2, report_path
        report['runtime'] = runtime_identity()
        knowledge_changed = knowledge_only or knowledge_reviews or any(
            p.startswith(('data/defaults/', 'tools/tag-semantic-audits/', 'tools/tag-identity-reviews/')) for p in requested)
        if knowledge_changed:
            from tools.checkpoint_knowledge import review_changes
            review_key = test_suites.normalize_requested_paths([knowledge_reviews],root)[0] if knowledge_reviews else None
            knowledge_key = knowledge_fingerprint(base, report['before']['files'], review_key)
            saved_knowledge = None if fresh else reusable_knowledge(root, knowledge_key)
            if saved_knowledge:
                knowledge, reused_knowledge_path = saved_knowledge
                print('Reusing unchanged knowledge review: '+str(reused_knowledge_path), flush=True)
            else:
                knowledge = review_changes(root, base, report_dir/'knowledge-inputs', knowledge_reviews, requested)
            knowledge_path = report_dir/'knowledge-review.json'
            knowledge_path.write_text(json.dumps(knowledge,indent=2)+'\n',encoding='utf-8')
            report['knowledge_review'] = {'report':str(knowledge_path),
                'fingerprint':knowledge_key, 'reused_from':str(saved_knowledge[1]) if saved_knowledge else None,
                'artifact_sha256':hashlib.sha256(knowledge_path.read_bytes()).hexdigest(),
                'changed':len(knowledge['changed']), 'affected':len(knowledge['affected']),
                'reused':len(knowledge['reused']), 'pending':len(knowledge['pending']),
                'new_issues':len(knowledge['new_issues']), 'current_issues':len(knowledge['issues'])}
        commands = [['git', 'diff', '--check', base, '--', *requested] if owned_only
                    else ['git', 'diff', '--check', base, '--']]
        for relative in explicit:
            try:
                git(root,'ls-files','--error-unmatch','--',relative)
            except subprocess.CalledProcessError:
                if (root/relative).is_file() and (root/relative).suffix.lower() in {'.py','.md','.json','.toml','.yml','.yaml','.ps1','.bat','.txt','.lock','.ini','.svg'}:
                    empty = report_dir/'empty-file'
                    empty.write_bytes(b'')
                    commands.append(['git','diff','--text','--no-index','--check','--',str(empty),relative])
        modules = [] if knowledge_only else plan['test_modules']
        # A fresh interpreter per module bounds accumulated Qt state. No retries.
        for index, module in enumerate(modules):
            commands.append([sys.executable, '-B', '-X', 'utf8', '-m', 'pytest', '-q', module,
                             '-o', 'faulthandler_timeout=120',
                             '--basetemp', str(report_dir / f't{index:03d}')])
        docs = [p for p in plan['documentation_paths'] if p.endswith('.md') and (root/p).is_file()]
        if docs and not knowledge_only:
            commands.append([sys.executable, '-B', '-X', 'utf8', 'tools/documentation_audit.py',
                             *[arg for path in docs for arg in ('--file', path)]])
        for index, command in enumerate(commands):
            print(subprocess.list2cmdline(command), flush=True)
            dependency = check_dependencies(command, report['before']['files'], root)
            key = check_fingerprint(command, report['before']['files'], report['runtime'], root=root,
                                    dependency=dependency)
            failure_key = failure_fingerprint(command, dependency, report['runtime']) if dependency else None
            cached = previous.get(key) if key else None
            prior_uncertain = previous.get('failure:' + failure_key) if failure_key else None
            if not fresh and cached and cached['status'] == 'passed':
                result = {**cached, 'reused_from': cached.get('origin_report',cached['report']), 'command': command}
                print('Reusing unchanged passing evidence: '+cached['report'], flush=True)
            else:
                result = execute(command, root, report_dir/f'{index:03d}.log', timeout)
            result['fingerprint'] = key
            if failure_key:
                result['failure_fingerprint'] = failure_key
            if dependency:
                result['dependency_mode'] = dependency['mode']
                result['dependency_reasons'] = dependency['reasons']
                result['dependency_cacheable'] = dependency['cacheable']
                result['dependency_input_count'] = len(dependency['inputs'])
                if dependency['mode'] == 'static_local_imports':
                    result['dependency_paths'] = sorted(dependency['inputs'])
                else:
                    result['dependency_input_scope'] = 'tracked_source_snapshot'
            result.setdefault('origin_report', str(report_path))
            prior_failure = next((item for item in (cached, prior_uncertain)
                                  if item and item['status'] != 'passed'), None)
            if prior_failure and result['status'] == 'passed':
                result['status'] = 'needs_failure_review'
                field = ('prior_failure_same_tracked_inputs' if prior_failure is prior_uncertain
                         else 'prior_failure_same_inputs')
                result[field] = prior_failure['report']
            report['checks'].append(result)
            save()
            if result['status'] != 'passed':
                report['status'] = result['status']
                break
        else:
            report['status'] = 'affected_checks_passed' if requested else 'no_changes'
        if report['status'] in {'affected_checks_passed','no_changes'}:
            if knowledge_only:
                report['status'] = 'knowledge_checks_passed'
            if report.get('knowledge_review',{}).get('pending') or report.get('knowledge_review',{}).get('current_issues'):
                report['status'] = 'knowledge_review_required'
        report['after'] = snapshot(root, requested)
        if report['before'] != report['after'] or changed != changed_paths(root, base):
            report['status'] = 'stale'
        report['remaining_gates'] = {'visual_scopes': plan['visual_scopes'],
                                   'release': 'Full release, bug triage, knowledge review and package gates remain separate.'}
        save()
        return (0 if report['status'] in {'affected_checks_passed', 'knowledge_checks_passed', 'no_changes'} else 1), report_path
    except BaseException as exc:
        report.update(status='interrupted' if isinstance(exc, KeyboardInterrupt) else 'error', error=str(exc))
        save()
        raise


def runtime_identity():
    packages = {}
    for name in ('pytest', 'PySide6', 'PySide6-Essentials', 'shiboken6', 'Pillow'):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = 'absent'
    return {'python': sys.version, 'executable': sys.executable,
            'platform': platform.platform(), 'packages': packages,
            'environment_sha256': hashlib.sha256(json.dumps(test_environment(), sort_keys=True).encode()).hexdigest()}


def check_dependencies(command, hashes, root=None):
    """Describe the exact bounded evidence inputs or explain a conservative fallback.

    Static imports alone are insufficient when code can discover source files at
    runtime. Those modules retain the complete source snapshot as their input.
    """
    if '-m' not in command or 'pytest' not in command:
        return None  # Cheap Git/documentation checks always run.
    module = command[command.index('-q') + 1]
    repository_audit = any(test_suites._matches(module, test_suites.SUITES[name].test_patterns)
                           for name in ('release', 'tooling'))
    if repository_audit:
        if root is not None:
            root = Path(root)
            fixtures = [module, 'conftest.py']
            parts = Path(module).parts[:-1]
            fixtures.extend(Path(*parts[:length], 'conftest.py').as_posix()
                            for length in range(1, len(parts) + 1))
            for fixture in fixtures:
                try:
                    present = source_path(root, fixture).is_file()
                except ValueError:
                    return {'mode': 'non_reusable_fallback',
                            'reasons': [f'unsafe_audit_input:{fixture}'],
                            'inputs': dict(hashes), 'cacheable': False}
                if (fixture == module or present) and fixture not in hashes:
                    return {'mode': 'non_reusable_fallback',
                            'reasons': [f'untracked_or_absent_dependency:{fixture}'],
                            'inputs': dict(hashes), 'cacheable': False}
        return {'mode': 'broad_fallback',
                'reasons': ['repository_audit', 'static_imports_not_analyzed'],
                'inputs': dict(hashes), 'cacheable': True}
    if root is None:
        # Keep existing standalone check_fingerprint callers compatible.
        inputs = {path: digest for path, digest in hashes.items()
                  if not (path.endswith('.md') or path.startswith(('docs/', 'licenses/', '.github/'))
                          or (Path(path).name.startswith('test_') and path.endswith('.py') and path != module))}
        return {'mode': 'legacy_broad', 'reasons': ['no_repository_root'],
                'inputs': inputs, 'cacheable': True}

    root = Path(root)
    # Pytest config and checkpoint implementation change test execution or its
    # interpretation. Shipped non-Python sources can be read by application code.
    selected = {path for path in hashes if path in {'pytest.ini', 'conftest.py',
                                                     'tools/test_checkpoint.py', 'tools/test_suites.py'}
                or path.startswith(('data/defaults/', 'assets/'))
                or path in SOURCE_CATALOG_FILES or path.startswith('requirements')}
    pending = [module]
    if module.startswith('tests/'):
        parts = Path(module).parts[:-1]
        for length in range(1, len(parts) + 1):
            fixture = Path(*parts[:length], 'conftest.py').as_posix()
            try:
                present = source_path(root, fixture).is_file()
            except ValueError:
                present = True  # An unsafe fixture must not authorize reuse.
            if fixture in hashes or present:
                pending.append(fixture)
    try:
        root_fixture = source_path(root, 'conftest.py').is_file()
    except ValueError:
        root_fixture = True
    if 'conftest.py' in hashes or root_fixture:
        pending.append('conftest.py')
    visited = set()
    reasons = []
    local_roots = {'core', 'qt_app', 'tests', 'tools'} | {
        Path(path).stem for path in hashes if '/' not in path and path.endswith('.py')}

    def local_files(name):
        parts = name.split('.')
        if not parts or parts[0] not in local_roots:
            return None
        prefix = Path()
        files = []
        for part in parts[:-1]:
            prefix /= part
            init = (prefix / '__init__.py').as_posix()
            if init in hashes:
                files.append(init)
            else:
                try:
                    untracked_init = source_path(root, init).is_file()
                except ValueError:
                    untracked_init = True
                if untracked_init:
                    return []
        stem = Path(*parts)
        candidates = (stem.with_suffix('.py').as_posix(), (stem / '__init__.py').as_posix())
        found = next((candidate for candidate in candidates if candidate in hashes), None)
        return files + [found] if found else []

    while pending and not reasons:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        if path not in hashes:
            reasons.append(f'untracked_or_absent_dependency:{path}')
            break
        selected.add(path)
        try:
            tree = ast.parse(source_path(root, path).read_text(encoding='utf-8'), filename=path)
        except (OSError, SyntaxError, UnicodeError, ValueError):
            reasons.append(f'unreadable_or_invalid_python:{path}')
            break
        external_aliases = {}
        for imported in ast.walk(tree):
            if isinstance(imported, ast.Import):
                for alias in imported.names:
                    external_aliases[alias.asname or alias.name.split('.')[0]] = alias.name.split('.')[0]
            elif isinstance(imported, ast.ImportFrom) and imported.module:
                for alias in imported.names:
                    external_aliases[alias.asname or alias.name] = imported.module.split('.')[0]
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                    level = 0
                else:
                    package = path.removesuffix('.py').replace('/', '.').split('.')
                    if package[-1] == '__init__':
                        package.pop()
                    else:
                        package.pop()
                    level = node.level
                    if level > len(package) + 1:
                        reasons.append(f'unresolved_relative_import:{path}')
                        break
                    base = '.'.join(package[:len(package) - level + 1] +
                                    ([node.module] if node.module else [])) if level else (node.module or '')
                    names = [base] if base else []
                    if node.module is None:
                        names.extend(base + '.' + alias.name for alias in node.names)
                for name in names:
                    files = local_files(name)
                    if files is None:
                        external = name.split('.')[0]
                        if external not in STATIC_EXTERNAL_IMPORTS:
                            reasons.append(f'unreviewed_external_import:{path}:{name}')
                        elif isinstance(node, ast.ImportFrom) and any(
                                alias.name not in STATIC_EXTERNAL_FROM.get(external, set())
                                for alias in node.names):
                            reasons.append(f'aliased_or_unreviewed_external_symbol:{path}:{name}')
                        continue
                    if not files:
                        reasons.append(f'unresolved_local_import:{path}:{name}')
                        break
                    pending.extend(files)
                if reasons:
                    break
                if isinstance(node, ast.ImportFrom) and node.module is not None:
                    base_files = local_files(base)
                    if base_files and base_files[-1].endswith('/__init__.py'):
                        for alias in node.names:
                            if alias.name == '*':
                                reasons.append(f'package_star_import:{path}:{base}')
                                break
                            candidate = local_files(base + '.' + alias.name)
                            if candidate:
                                pending.extend(candidate)
                            else:
                                # A package export may be a variable or a late
                                # imported submodule; do not guess which.
                                reasons.append(f'ambiguous_package_export:{path}:{base}.{alias.name}')
                                break
            elif isinstance(node, ast.Call):
                function = node.func
                name = function.id if isinstance(function, ast.Name) else (
                    function.attr if isinstance(function, ast.Attribute) else '')
                if name in {'__import__', 'eval', 'exec', 'compile', 'getattr', 'globals',
                            'locals', 'vars', 'dir', 'open', 'read',
                            'readall', 'readAll', 'load', 'imread', 'read_csv', 'read_json',
                            'loadtxt', 'genfromtxt', 'syspath_prepend', 'open_resource',
                            'get_data', 'read_text',
                            'read_bytes', 'glob', 'rglob', 'iterdir', 'listdir', 'scandir',
                            'walk', 'stat', 'exists', 'isfile', 'isdir', 'is_file', 'is_dir',
                            'import_module',
                            'spec_from_file_location', 'run_path', 'run_module'}:
                    reasons.append(f'dynamic_source_access:{path}:{name}')
                    break
            elif isinstance(node, ast.Attribute):
                if (node.attr in {'open', 'read', 'readall', 'readAll', 'load', 'glob',
                                  'rglob', 'iterdir', 'listdir', 'scandir', 'walk',
                                  'stat', 'exists', 'isfile', 'isdir', 'is_file',
                                  'is_dir', 'modules', 'syspath_prepend'}
                        or node.attr.startswith(('read_', 'load_', 'imread'))):
                    reasons.append(f'dynamic_source_attribute:{path}:{node.attr}')
                    break
                owner = node.value
                while isinstance(owner, (ast.Attribute, ast.Call)):
                    owner = owner.value if isinstance(owner, ast.Attribute) else owner.func
                if isinstance(owner, ast.Name):
                    external = external_aliases.get(owner.id)
                    if external in STATIC_EXTERNAL_IMPORTS and external not in local_roots:
                        if node.attr not in STATIC_EXTERNAL_ATTRS.get(external, set()):
                            reasons.append(f'unreviewed_external_attribute:{path}:{external}.{node.attr}')
                            break
            elif isinstance(node, ast.Name) and node.id in {
                    'open', '__import__', 'eval', 'exec', 'compile', 'input',
                    'globals', 'locals', 'vars', 'dir', '__builtins__', 'breakpoint'}:
                reasons.append(f'dynamic_builtin_reference:{path}:{node.id}')
                break
            elif isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == 'pytest_plugins'
                    for target in node.targets):
                reasons.append(f'pytest_plugins:{path}')
                break
    if reasons:
        missing = any(reason.startswith((
            'untracked_or_absent_dependency:', 'unresolved_local_import:',
            'unresolved_relative_import:', 'ambiguous_package_export:',
            'unreadable_or_invalid_python:')) for reason in reasons)
        return {'mode': 'non_reusable_fallback' if missing else 'broad_fallback',
                'reasons': reasons, 'inputs': dict(hashes), 'cacheable': not missing}
    return {'mode': 'static_local_imports', 'reasons': ['resolved_local_import_closure'],
            'inputs': {path: hashes[path] for path in selected}, 'cacheable': True}


def check_fingerprint(command, hashes, runtime, root=None, dependency=None):
    dependency = dependency if dependency is not None else check_dependencies(command, hashes, root)
    if dependency is None:
        return None
    if not dependency['cacheable']:
        return None
    module = command[command.index('-q') + 1]
    return hashlib.sha256(json.dumps({'module': module, 'inputs': dependency['inputs'],
                                      'runtime': runtime, 'dependency_mode': dependency['mode']},
                                     sort_keys=True).encode()).hexdigest()


def failure_fingerprint(command, dependency, runtime):
    if dependency['cacheable']:
        return None
    module = command[command.index('-q') + 1]
    return hashlib.sha256(json.dumps({'module': module, 'tracked_inputs': dependency['inputs'],
                                      'runtime': runtime, 'reasons': dependency['reasons']},
                                     sort_keys=True).encode()).hexdigest()


def reusable_checks(root):
    parent = root / '.tmp' / 'test-checkpoints'
    if not parent.is_dir():
        return {}
    candidates = sorted(parent.glob('*/report.json'), key=lambda p:p.stat().st_mtime_ns, reverse=True)[:100]
    newest = {}
    for path in candidates:
        if path.is_symlink() or path.parent.is_symlink():
            continue
        try:
            report = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if report.get('before') != report.get('after') or not report.get('after'):
            continue
        for check in report.get('checks', []):
            key = check.get('fingerprint')
            if key and key not in newest:
                newest[key] = {**check, 'report':str(path)}
            uncertain = check.get('failure_fingerprint')
            if uncertain and 'failure:' + uncertain not in newest:
                newest['failure:' + uncertain] = {**check, 'report':str(path)}
    return newest


def knowledge_fingerprint(base, hashes, review_path=None):
    inputs={path:digest for path,digest in hashes.items() if path.startswith((
        'core/','data/defaults/','tools/tag-semantic-audits/','tools/tag-identity-reviews/',
        'tools/knowledge-change-reviews')) or path in {
            'tools/knowledge_change_review.py','tools/checkpoint_knowledge.py','tools/test_checkpoint.py'}}
    if review_path:
        inputs[review_path]=hashes[review_path]
    return hashlib.sha256(json.dumps({'base':base,'inputs':inputs,'review_path':review_path,'python':sys.version},sort_keys=True).encode()).hexdigest()


def reusable_knowledge(root, fingerprint):
    parent=root/'.tmp/test-checkpoints'
    if not parent.is_dir(): return None
    for path in sorted(parent.glob('*/report.json'),key=lambda p:p.stat().st_mtime_ns,reverse=True)[:100]:
        artifact=path.parent/'knowledge-review.json'
        if any(p.is_symlink() or getattr(p,'is_junction',lambda:False)() for p in (path.parent,path,artifact)):
            continue
        try:
            report=json.loads(path.read_text(encoding='utf-8'))
            meta=report.get('knowledge_review',{})
            if not report.get('after') or report.get('before')!=report['after'] or meta.get('fingerprint')!=fingerprint:
                continue
            data=artifact.read_bytes()
            if hashlib.sha256(data).hexdigest()!=meta.get('artifact_sha256'): continue
            return json.loads(data),artifact
        except (OSError,ValueError,TypeError):
            continue
    return None


def test_environment():
    # Host pytest filters/retry plugins must not silently shrink or retry a gate.
    return dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1',
                QT_QPA_PLATFORM='offscreen', PYTEST_ADDOPTS='', PYTEST_PLUGINS='',
                PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
