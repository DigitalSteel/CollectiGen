"""Opt-in, pinned original Manager source-entry catalog recipes.

No controller injection, desktop input, dependency installation or live catalogs.
Check admission by default; --run owns one fresh audited child until normal exit.
"""
from __future__ import annotations

import argparse
import ast
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import subprocess
import sys
import time
import traceback

REPO = Path(__file__).resolve().parents[2]
EXPECTED_OBSERVER_FAILURE = 'RuntimeError: Synthetic observer failure before admission'


@lru_cache(maxsize=4)
def _stock(name, file):
    # The shared bootstrap guards are stdlib-only. Do not import a checkout's
    # tools/core packages into the selected application's fresh worker process.
    return runpy.run_path(str(Path(__file__).with_name(file)), run_name='_guard')[name]


def guarded(repo, value):
    return _stock('workspace_path', 'organizer_package_probe.py')(repo, value)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def accepted_result(record):
    """A process exit or screenshot alone never establishes recipe acceptance."""
    if not isinstance(record, dict) or not isinstance(record.get('captures'), dict) or not isinstance(record.get('application_origins'), dict):
        return False
    scenario = record.get('scenario')
    if scenario == 'context':
        branch = (record.get('context_matches_accepted') is True and record.get('draft_preserved') is True
            and record.get('held_context_preserved') is True and record.get('saved_catalog_unchanged') is True
            and record.get('saved_settings_unchanged') is True and record.get('delayed_open_reviewed') is True
            and record.get('events') == ['visible_open_saved_once','visible_draft_return','visible_failed_open_once']
            and set(record['captures']) == {'entry','opening','context','held'})
    elif scenario == 'preserve':
        branch = (record.get('personal_preserved') is True and valid_personal_witness(record.get('personal'))
            and record.get('native_saved') is True and record.get('participants_verified') is True
            and record.get('events') == ['visible_reload_seed', 'visible_apply_once']
            and set(record['captures']) == {'entry', 'decision', 'saved'})
    elif scenario == 'reopen':
        branch = (record.get('personal_preserved') is True and valid_personal_witness(record.get('personal'))
            and record.get('saved_catalog_unchanged') is True and record.get('saved_reopened') is True
            and record.get('events') == ['visible_open_saved_once']
            and set(record['captures']) == {'entry', 'reopened'})
    else:
        branch = (scenario == 'stale' and record.get('stale_rejected') is True
            and record.get('no_receipt_created') is True and record.get('events') == ['visible_apply_once']
            and set(record['captures']) == {'entry', 'decision', 'stale-held'})
    return bool(branch and record.get('schema_version') == 1
        and record.get('level') == 'source_component' and record.get('platform') == 'windows'
        and record.get('exit_code') == 0 and record.get('ordinary_branch') is True
        and record.get('connection_review_read_only') is True and record.get('close_accepted') is True
        and record.get('unknown_settings_preserved') is True and record.get('input_bytes_preserved') is True
        and record.get('host_busy') is False and record.get('controller_close_held') is False
        and record.get('executor_close_held') is False and record.get('errors') == []
        and record.get('blocked_events') == []
        and record.get('application_origins'))


def valid_personal_witness(value):
    """Reject empty, omitted, changed or merely exit-based preservation claims."""
    if not isinstance(value, dict) or not value.get('tag_id') or not value.get('category_id'):
        return False
    rows = value.get('files')
    return bool(value.get('decoded_verified') is True and isinstance(rows, dict)
        and set(rows) == {'data/user/tags.json', 'data/user/categories.json'}
        and all(isinstance(row, dict) and re.fullmatch('[0-9a-f]{64}', row.get('before', ''))
            and row.get('before') == row.get('after') for row in rows.values()))


def expected_failure_cleanup_result(record):
    """Qualify one explicit negative test; never turn a failed recipe into a pass."""
    return bool(isinstance(record, dict) and record.get('schema_version') == 1
        and record.get('scenario') == 'observer_failure' and record.get('passed') is False
        and record.get('errors') == [EXPECTED_OBSERVER_FAILURE] and record.get('blocked_events') == []
        and record.get('exit_code') == 0 and record.get('worker_exit_code') == 1
        and record.get('ordinary_branch') is True and record.get('original_process_retired') is True
        and record.get('close_accepted') is True and record.get('failure_close_attempts') == 1
        and record.get('failure_cleanup_status') == 'closed_after_drain'
        and record.get('host_busy') is False and record.get('controller_close_held') is False
        and record.get('executor_close_held') is False and record.get('saved_catalog_unchanged') is True
        and record.get('saved_settings_unchanged') is True and record.get('input_bytes_preserved') is True
        and record.get('unknown_settings_preserved') is True and record.get('application_origins')
        and isinstance(record.get('captures'), dict) and set(record['captures']) == {'entry','failure'})


def _personal(catalog, witness):
    """Independent raw decode plus exact native-file preservation, no serializer."""
    for name in ('tags', 'categories'):
        value = _json(catalog / ('data/user/' + name + '.json'))
        assert value['recipe_future_root'] == {'opaque': [17, {'keep': True}]}
        field = 'tag_id' if name == 'tags' else 'category_id'
        row = next(r for r in value[name] if r['id'] == witness[field])
        assert row['recipe_future_record'] == {'opaque': ['preserve', 23]}
        if name == 'tags':
            assert row['category'] == witness['category_id'] and row['properties']['note'] == 'preserve personal native record'
        relative = 'data/user/' + name + '.json'
        witness['files'][relative]['after'] = digest(catalog / relative)
    witness['decoded_verified'] = True
    assert valid_personal_witness(witness)
    return witness


def _existing_saved_lock(event, path, args, catalog):
    """Stock read admission takes the already-created native advisory lock.

    Allow its existing-parent mkdir no-op and append-open only. No participant,
    new lock, truncation, rename, unlink or directory creation is authorized.
    """
    user = catalog / 'data/user'; lock = user / '.knowledge-write.lock'
    if event == 'os.mkdir':
        return path == user and user.is_dir()
    return bool(event == 'open' and path == lock and lock.is_file()
        and lock.read_bytes() == b'\0' and len(args) > 2 and args[1] in {'a+', 'a+b'}
        and isinstance(args[2], int) and args[2] & os.O_APPEND and not args[2] & os.O_TRUNC)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate receipt/recipe key')
        value[key] = item
    return value


def _json(path, expected=None):
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('Evidence exceeds bound')
    raw = path.read_bytes()
    if expected is not None and (not re.fullmatch('[0-9a-f]{64}', expected or '')
                                 or hashlib.sha256(raw).hexdigest() != expected):
        raise ValueError('Pinned evidence changed')
    return json.loads(raw, object_pairs_hook=_unique)


def _relative(value):
    if not isinstance(value, str) or '\\' in value or ':' in value:
        raise ValueError('Canonical relative evidence path required')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or not path.parts or path.as_posix() != value:
        raise ValueError('Unsafe evidence path')
    if any(p.casefold() in {'local only', 'local_only', '.git'} for p in path.parts):
        raise ValueError('Protected evidence path')
    return path


def admit(repo, recipe_path, recipe_sha256):
    """Read/check only; a refused recipe creates no profile or output directory."""
    repo = Path(repo).absolute()
    recipe_path = guarded(repo, recipe_path)
    value = _json(recipe_path, recipe_sha256)
    required = {'schema_version', 'scenario', 'source_root', 'source_inventory_sha256',
                'input_receipt', 'input_receipt_sha256', 'fixture_root',
                'manager_version', 'interpreter_sha256'}
    if value.get('scenario') in {'reopen','context','observer_failure'}:
        required |= {'saved_result', 'saved_result_sha256'}
    if value.get('scenario') in {'context','observer_failure'}:
        required |= {'saved_source_root','saved_source_inventory_sha256'}
    if set(value) != required or value['schema_version'] != 1 or value['scenario'] not in {'stale', 'preserve', 'reopen','context','observer_failure'}:
        raise ValueError('Exact finite catalog recipe required')
    code = guarded(repo, value['source_root'])
    receipt = guarded(repo, value['input_receipt'])
    fixture = guarded(repo, value['fixture_root'])
    if (fixture.exists() or fixture == code or code in fixture.parents or fixture in code.parents
            or fixture == receipt.parent or receipt.parent in fixture.parents
            or fixture in receipt.parents):
        raise ValueError('Fresh disjoint fixture required')
    if digest(Path(sys.executable)) != value['interpreter_sha256']:
        raise ValueError('Selected interpreter changed')
    inventory_path = guarded(repo, code / 'public-source.json')
    inventory = _json(inventory_path, value['source_inventory_sha256'])
    if (inventory.get('export_scope') != 'component-source'
            or 'collectigen.suite-manager' not in inventory.get('component_ids', [])
            or not inventory.get('manifest_excludes_self')):
        raise ValueError('Explicit default-free Manager source inventory required')
    rows = inventory.get('files')
    if not isinstance(rows, dict) or not 1 <= len(rows) <= 5000:
        raise ValueError('Finite source inventory required')
    required_source = {'suite_core/manager/MODULE_MANIFEST.json', 'suite_core/manager/version.py',
        'suite_core/manager/__main__.py', 'suite_core/manager/tag_database_controller.py',
        'core/knowledge_pack.py'}
    if not required_source.issubset(rows):
        raise ValueError('Required original-entry source owners missing')
    # Validate ALL names before opening any declared leaf.
    names = [_relative(n) for n in rows]
    for relative in names:
        path = guarded(repo, code / str(relative))
        row = rows[str(relative)]
        if not isinstance(row, dict) or digest(path) != row.get('sha256'):
            raise ValueError('Selected source input changed')
    stock = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'public_source.py'), run_name='_inventory')
    if stock['verify_public_inventory'](code, allow_git_metadata=True):
        raise ValueError('Selected public source inventory differs')
    manifest = _json(guarded(repo, code / 'suite_core/manager/MODULE_MANIFEST.json'))
    constants = {}
    for node in ast.parse((code / 'suite_core/manager/version.py').read_text(encoding='utf-8-sig')).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value
    if (manifest.get('module_id') != 'collectigen.suite-manager'
            or manifest.get('software_version') != value['manager_version']
            or constants.get('VERSION') != value['manager_version']):
        raise ValueError('Manager source identity mismatch')
    prior = _json(receipt, value['input_receipt_sha256'])
    if prior.get('schema_version') != 1 or prior.get('synthetic') is not True:
        raise ValueError('Explicit pinned generated-input receipt required')
    data = guarded(repo, prior['source'])
    if not data.is_relative_to(receipt.parent):
        raise ValueError('Generated source escapes its receipt zone')
    prefix = data.relative_to(receipt.parent).as_posix() + '/'
    input_rows = prior.get('initial_files')
    if not isinstance(input_rows, list) or not 1 <= len(input_rows) <= 5000:
        raise ValueError('Finite generated input pins required')
    selected = []; seen = set()
    for row in input_rows:
        if not isinstance(row, dict) or set(row) != {'path', 'sha256'} or row['path'] in seen:
            raise ValueError('Unambiguous generated input pins required')
        seen.add(row['path'])
        relative = _relative(row['path'])
        if str(relative).startswith(prefix):
            selected.append((guarded(repo, receipt.parent / str(relative)), row['sha256']))
    if not selected or data / 'TagDatabase.zip' not in [p for p, _ in selected]:
        raise ValueError('Generated Pack pins required')
    for path, expected in selected:
        if digest(path) != expected:
            raise ValueError('Generated source changed')
    saved = None
    if value['scenario'] in {'reopen','context','observer_failure'}:
        saved_source_pin = value['source_inventory_sha256']
        if value['scenario'] in {'context','observer_failure'}:
            baseline = guarded(repo,value['saved_source_root'])
            if baseline == fixture or baseline in fixture.parents or fixture in baseline.parents:
                raise ValueError('Context output must be separate from saved source')
            old_inventory = _json(guarded(repo,baseline/'public-source.json'),value['saved_source_inventory_sha256'])
            old_names = [_relative(n) for n in old_inventory['files']]
            if stock['verify_public_inventory'](baseline,allow_git_metadata=True):
                raise ValueError('Saved source inventory differs')
            allowed = {'suite_core/manager/tag_database_ui.py','suite_core/manager/database_presentation.py','suite_core/manager/version.py'}
            for n in old_names:
                name=str(n)
                if name.startswith(('core/','suite_core/','qt_app/')) and name.endswith('.py') and name not in allowed:
                    if name not in rows or rows[name]['sha256'] != old_inventory['files'][name]['sha256']:
                        raise ValueError('Context recipe cannot bridge native source changes')
            saved_source_pin = value['saved_source_inventory_sha256']
        prior_path = guarded(repo, value['saved_result'])
        saved = _json(prior_path, value['saved_result_sha256'])
        if (not accepted_result(saved) or saved.get('scenario') != 'preserve'
                or saved.get('original_process_retired') is not True or saved.get('worker_exit_code') != 0
                or saved.get('source_inventory_sha256') != saved_source_pin
                or saved.get('input_receipt_sha256') != value['input_receipt_sha256']
                or not saved.get('saved_files') or prior_path.name != 'RESULT.json'
                or fixture == prior_path.parent or prior_path.parent in fixture.parents or fixture in prior_path.parents):
            raise ValueError('Pinned retired preservation result required')
        saved_catalog = guarded(repo, prior_path.parent / 'catalog')
        if not isinstance(saved['saved_files'], dict) or not 1 <= len(saved['saved_files']) <= 3000:
            raise ValueError('Finite saved native participants required')
        names = [_relative(n) for n in saved['saved_files']]
        for relative in names:
            name = str(relative); pin = saved['saved_files'][name]
            if digest(guarded(repo, saved_catalog / name)) != pin:
                raise ValueError('Saved catalog changed before reopening')
        saved_settings = guarded(repo, prior_path.parent / 'profile/user/ManagerAppSettings.json')
        if digest(saved_settings) != saved.get('saved_settings_sha256'):
            raise ValueError('Saved settings changed before reopening')
        saved = dict(saved, catalog=saved_catalog, settings=saved_settings)
    return {'recipe': value, 'recipe_path': recipe_path, 'code': code, 'data': data, 'saved': saved,
            'fixture': fixture, 'input_pins': selected, 'repo': repo}


def _worker(admitted, recipe_sha256):
    code, data, fixture, repo = (admitted[k] for k in ('code', 'data', 'fixture', 'repo'))
    if sys.platform != 'win32' or any(k in sys.modules for k in ('core', 'suite_core', 'qt_app')):
        raise ValueError('Fresh Windows source worker required')
    guarded(repo, fixture).mkdir(parents=True, exist_ok=False)
    profile, temp, output = fixture / 'profile', fixture / 'temp', fixture / 'captures'
    for path in (profile / 'user', profile / 'cache', temp, output):
        path.mkdir(parents=True)
    os.environ.update(TAG_MANAGER_TEST_OWNED_ROOT=str(profile), TAG_MANAGER_UI_TEST='1',
        TAG_MANAGER_PYTEST='1', TEMP=str(temp), TMP=str(temp), TMPDIR=str(temp),
        QT_QPA_PLATFORM='windows', PYTHONDONTWRITEBYTECODE='1')
    for key in ('TAG_MANAGER_PYTEST_USER_DATA_DIR', 'TAG_MANAGER_UI_TEST_USER_DATA_DIR'):
        os.environ[key] = str(profile / 'user')
    for key in ('TAG_MANAGER_PYTEST_CACHE_DIR', 'TAG_MANAGER_UI_TEST_CACHE_DIR'):
        os.environ[key] = str(profile / 'cache')
    settings, catalog = profile / 'user/ManagerAppSettings.json', fixture / 'catalog'
    scenario = admitted['recipe']['scenario']
    if scenario in {'reopen','context','observer_failure'}:
        settings.write_bytes(admitted['saved']['settings'].read_bytes())
        catalog = admitted['saved']['catalog']
    else:
        settings.write_text('{"recipe_unknown":{"keep":7}}', encoding='utf-8')
    record = {'schema_version': 1, 'scenario': scenario, 'pid': os.getpid(), 'events': [],
              'errors': [], 'blocked_events': [], 'recipe_sha256': recipe_sha256,
              'source_inventory_sha256': admitted['recipe']['source_inventory_sha256'],
              'input_receipt_sha256': admitted['recipe']['input_receipt_sha256'],
              'level': 'source_component', 'captures': {}, 'passed': False}

    def retain():
        (fixture / 'RESULT.json').write_text(json.dumps(record, indent=2), encoding='utf-8')

    def audit(event, args):
        if event.startswith(('socket.connect', 'socket.bind', 'subprocess.Popen',
                             'os.system', 'os.posix_spawn', 'os.symlink', 'os.link')):
            record['blocked_events'].append(event)
            raise RuntimeError('Recipe refuses network/process/redirect creation')
        events = {'open', 'os.listdir', 'os.scandir', 'os.mkdir', 'os.remove', 'os.rmdir',
                  'os.rename', 'os.replace', 'shutil.rmtree'}
        if event not in events or not args:
            return
        raw_paths = args[:2] if event in {'os.rename', 'os.replace'} else args[:1]
        for raw in raw_paths:
            if isinstance(raw, int) or not isinstance(raw, (str, bytes, os.PathLike)):
                continue
            path = Path(os.path.abspath(os.fsdecode(raw)))
            if any(part.casefold() in {'local only', 'local_only'} for part in path.parts):
                record['blocked_events'].append('protected-zone')
                raise RuntimeError('Protected zone refused')
            if any(path == base / 'data' or base / 'data' in path.parents for base in (repo, code)):
                record['blocked_events'].append('protected-project-data')
                raise RuntimeError('Project data refused')
            if Path.home() in path.parents and not path.is_relative_to(Path(sys.base_prefix)):
                record['blocked_events'].append('external-user-read')
                raise RuntimeError('External user data refused')
            writing = event not in {'open', 'os.listdir', 'os.scandir'}
            if event == 'open':
                writing = (isinstance(args[1], str) and any(c in args[1] for c in 'wax+')
                    or len(args) > 2 and isinstance(args[2], int) and args[2] &
                    (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writing and not path.is_relative_to(fixture):
                if scenario in {'reopen','context','observer_failure'} and _existing_saved_lock(event, path, args, catalog):
                    record.setdefault('existing_lock_accesses', []).append(event)
                    continue
                record['blocked_events'].append('external-write')
                record.setdefault('blocked_paths', []).append(str(path))
                raise RuntimeError('Write outside fresh recipe fixture refused')

    sys.addaudithook(audit)
    refs = []
    try:
        sys.path.insert(0, str(code))
        from tools.test_profile_isolation import check_environment, check_bound_modules
        check_environment(os.environ, profile, profile / 'user', profile / 'cache')
        from PySide6.QtCore import QTimer, Qt, QPoint
        from PySide6.QtGui import QFont, QFontDatabase
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QScrollArea, QCheckBox
        from suite_core.manager.__main__ import main as original_main

        def observe(window, app):
            font = QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf')
            assert font >= 0 and app.platformName() == 'windows'
            app.setFont(QFont(QFontDatabase.applicationFontFamilies(font)[0], 10))
            pane, host = window.tag_database_pane, window.tag_content_host
            controller = pane.controller
            assert host.controller is controller and host.executor is controller.executor
            assert controller.settings_path == settings
            check_bound_modules(profile / 'user', profile / 'cache')
            refs.append((host, controller))
            record['original_host'] = type(host).__name__
            record['platform'] = app.platformName()
            record['ordinary_branch'] = True
            window.resize(1920, 1080)
            stage, started = ['navigate'], time.monotonic()
            from threading import Event
            open_release=Event()
            if scenario in {'context','observer_failure'}:
                import suite_core.manager.tag_database_controller as delivery
                stock_connect=delivery._connect
                def delayed_connect(*args):
                    assert open_release.wait(5), 'Labelled fixture saved-open delay expired'
                    return stock_connect(*args)
                delivery._connect=delayed_connect
                record['fixture_hook']='Delay only; calls unchanged original connection worker'
                draft=('synthetic-draft-source','synthetic-draft-catalog',True)
                def assert_draft():
                    assert (pane.source_directory.text(),pane.catalog_directory.text(),pane.create_catalog.isChecked())==draft
                saved_settings=settings.read_bytes()

            def capture(name):
                path = output / (name + '.png')
                assert window.grab().save(str(path))
                record['captures'][name] = {'path': str(path), 'sha256': digest(path)}
                record['logical_size'] = [window.width(), window.height()]
                record['dpr'] = window.devicePixelRatioF()

            def click(widget):
                ancestor = widget.parentWidget()
                while ancestor is not None:
                    if isinstance(ancestor, QScrollArea):
                        ancestor.ensureWidgetVisible(widget)
                        break
                    ancestor = ancestor.parentWidget()
                app.processEvents()
                assert widget.isVisible() and widget.isEnabled()
                if isinstance(widget, QCheckBox):
                    QTest.mouseClick(widget, Qt.LeftButton, pos=QPoint(8, widget.height() // 2))
                else:
                    QTest.mouseClick(widget, Qt.LeftButton)

            def tick():
                try:
                    # Retained failures never re-enter route deadlines/actions.
                    if stage[0] == 'failure_close':
                        if controller.pending or controller.close_held or host.busy or host.executor.close_held:
                            return
                        timer.stop();stage[0]='failure_settled'
                        record['failure_close_attempts']=1
                        record['close_accepted']=bool(window.close())
                        record['failure_cleanup_status']=('closed_after_drain' if record['close_accepted'] else 'held_for_supervision')
                        retain();return
                    if stage[0] == 'failure_settled':
                        return
                    if time.monotonic() - started > 120:
                        raise RuntimeError('Recipe deadline; retain original work, never replay')
                    if scenario == 'observer_failure' and stage[0] == 'context_wait':
                        raise RuntimeError('Synthetic observer failure before admission')
                    if scenario == 'context' and stage[0] == 'context_wait' and controller.pending:
                        if time.monotonic()-record['open_started'] >= .35 and not open_release.is_set():
                            assert pane.progress.isVisible() and pane.progress.minimum()==pane.progress.maximum()==0
                            assert pane.status.text() and controller.close_held
                            record['cancel_available_in_manager_pane']=pane.cancel.isEnabled()
                            assert_draft();capture('opening')
                            record['delay_elapsed_seconds']=time.monotonic()-record['open_started']
                            record['delayed_open_reviewed']=True;open_release.set()
                        return
                    if controller.pending:
                        return
                    current = stage[0]
                    if current == 'navigate':
                        item = window.navigation.item(window.page_rows['Databases'])
                        QTest.mouseClick(window.navigation.viewport(), Qt.LeftButton,
                            pos=window.navigation.visualItemRect(item).center())
                        app.processEvents()
                        assert pane.isVisible()
                        capture('entry')
                        if scenario in {'context','observer_failure'}:
                            pane.source_directory.setFocus();QTest.keyClicks(pane.source_directory,draft[0])
                            pane.catalog_directory.setFocus();QTest.keyClicks(pane.catalog_directory,draft[1])
                            if not pane.create_catalog.isChecked():click(pane.create_catalog)
                            record['open_started']=time.monotonic()
                            click(pane.open_connection);record['events'].append('visible_open_saved_once')
                            stage[0]='context_wait';return
                        if scenario == 'reopen':
                            click(pane.open_connection); record['events'].append('visible_open_saved_once')
                            stage[0] = 'reopened'; return
                        pane.source_directory.setFocus(); QTest.keyClicks(pane.source_directory, str(data))
                        pane.catalog_directory.setFocus(); QTest.keyClicks(pane.catalog_directory, str(catalog))
                        if not pane.create_catalog.isChecked():
                            click(pane.create_catalog)
                        assert pane.create_catalog.isChecked()
                        click(pane.review_connection); stage[0] = 'review'
                    elif current == 'review':
                        assert controller.connection_review is not None and not catalog.exists(), controller.error
                        record['connection_review_read_only'] = True
                        click(pane.confirm_connection); click(pane.connect_catalog); stage[0] = 'connected'
                    elif current == 'connected':
                        assert controller.choices and controller.choice.native_adapter is not None, controller.error
                        if scenario == 'preserve':
                            from core.category_registry import CategoryRegistry
                            from core.tag_database import TagDatabase
                            from core import knowledge_pack as native
                            categories = CategoryRegistry(catalog / 'data/defaults/categories.json', catalog / 'data/user/categories.json')
                            category = categories.add('Synthetic personal preservation'); categories.save_user()
                            tags = TagDatabase(catalog / 'data/user/tags.json')
                            tag = tags.add_known_tag('synthetic_personal_preservation', category=category.id,
                                properties={'note': 'preserve personal native record'})
                            tags.save_verified()
                            witness = {'tag_id': tag.id, 'category_id': category.id, 'files': {}}
                            for name in ('tags', 'categories'):
                                p = catalog / ('data/user/' + name + '.json'); value = _json(p)
                                value['recipe_future_root'] = {'opaque': [17, {'keep': True}]}
                                field = 'tag_id' if name == 'tags' else 'category_id'
                                next(r for r in value[name] if r['id'] == witness[field])['recipe_future_record'] = {'opaque': ['preserve', 23]}
                                native._atomic_json(p, value)
                                witness['files']['data/user/' + name + '.json'] = {'before': digest(p)}
                            record['personal'] = _personal(catalog, witness)
                            record['before_catalog'] = {p.relative_to(catalog).as_posix(): digest(p) for p in catalog.rglob('*') if p.is_file()}
                            click(pane.reload); record['events'].append('visible_reload_seed'); stage[0] = 'reloaded'; return
                        click(pane.preview); stage[0] = 'preview'
                    elif current == 'reloaded':
                        assert not controller.error, controller.error
                        _personal(catalog, record['personal'])
                        click(pane.preview); stage[0] = 'preview'
                    elif current == 'preview':
                        assert controller.review is not None and controller.review.can_apply, controller.error
                        capture('decision')
                        click(pane.confirm)
                        if scenario == 'preserve':
                            click(pane.apply); record['events'].append('visible_apply_once'); stage[0] = 'saved'; return
                        from core import knowledge_pack as native
                        user = catalog / 'data/user/tags.json'
                        (fixture / 'user.before').write_bytes(user.read_bytes())
                        value = json.loads(user.read_bytes()); value['recipe_later_correction'] = {'keep': 29}
                        native._atomic_json(user, value)
                        record['later_correction_sha256'] = digest(user)
                        click(pane.apply); record['events'].append('visible_apply_once'); stage[0] = 'held'
                    elif current == 'held':
                        assert controller.error and digest(catalog / 'data/user/tags.json') == record['later_correction_sha256']
                        assert not tuple((catalog / 'backups/knowledge-pack-import').glob('*/recovery.json'))
                        assert 'fresh review' in pane.status.text() and 'recover' not in pane.status.text().lower()
                        record['displayed_status'] = pane.status.text()
                        record['stale_rejected'] = True; record['no_receipt_created'] = True
                        capture('stale-held')
                        timer.stop(); record['close_accepted'] = bool(window.close())
                    elif current == 'saved':
                        assert controller.result is not None and not controller.result.publication_error, controller.error
                        assert controller.choice.installed and not controller.error, controller.error
                        record['personal'] = _personal(catalog, record['personal']); record['personal_preserved'] = True
                        receipts = list((catalog / 'backups/knowledge-pack-import').glob('*/recovery.json'))
                        assert len(receipts) == 1
                        receipt = _json(receipts[0]); assert receipt['status'] == 'complete'
                        from core import knowledge_pack as native
                        pack = native.load_foundation_pack(data / 'TagDatabase.zip')
                        for name, raw in pack.proposal.payloads:
                            assert (catalog / name).read_bytes() == raw
                        tracking = _json(catalog / 'data/user/default_knowledge_state.json')
                        assert tracking['approved_pack_releases']
                        from suite_core.encyclopedia.publication import read
                        publication = read(catalog / 'tag_catalog_snapshot.json')
                        assert controller.choice.transactions.session.binding.snapshot_fingerprint == publication.snapshot.fingerprint
                        record['participants_verified'] = True; record['native_saved'] = True
                        record['displayed_status'] = pane.status.text(); capture('saved')
                        timer.stop(); record['close_accepted'] = bool(window.close())
                    elif current == 'reopened':
                        assert controller.choices and controller.choice.installed and not controller.error, controller.error
                        record['connection_review_read_only'] = True
                        record['personal'] = _personal(catalog, admitted['saved']['personal']); record['personal_preserved'] = True
                        record['saved_catalog_unchanged'] = all(digest(catalog / n) == pin for n, pin in admitted['saved']['saved_files'].items())
                        assert record['saved_catalog_unchanged']
                        record['saved_reopened'] = True; capture('reopened')
                        timer.stop(); record['close_accepted'] = bool(window.close())
                    elif current == 'context_wait':
                        assert controller.choices and not controller.error,controller.error
                        context=pane.connected_context.text()
                        assert str(catalog) in context and str(data) in context
                        assert_draft();assert pane.connection_options.isChecked()
                        click(pane.connection_options);click(pane.connection_options);assert_draft()
                        record['events'].append('visible_draft_return');record['draft_preserved']=True
                        record['context_matches_accepted']=True;record['accepted_context']=context
                        record['connection_review_read_only']=True;capture('context')
                        settings.unlink()  # Labelled failure in this fresh profile only.
                        click(pane.open_connection);record['events'].append('visible_failed_open_once');stage[0]='context_held'
                    elif current == 'context_held':
                        assert controller.error and pane.connected_context.text()==record['accepted_context']
                        assert_draft();record['held_context_preserved']=True;capture('held')
                        settings.write_bytes(saved_settings)
                        record['saved_settings_unchanged']=digest(settings)==admitted['saved']['saved_settings_sha256']
                        record['saved_catalog_unchanged']=all(digest(catalog/n)==pin for n,pin in admitted['saved']['saved_files'].items())
                        assert record['saved_catalog_unchanged'] and record['saved_settings_unchanged']
                        timer.stop();record['close_accepted']=bool(window.close())
                except BaseException as error:
                    if stage[0] == 'failure_settled':
                        record['failure_cleanup_status']='held_for_supervision'
                        record['failure_cleanup_error']=type(error).__name__+': '+str(error)
                        retain();return
                    open_release.set()
                    record['errors'].append(type(error).__name__ + ': ' + str(error))
                    record['traceback'] = traceback.format_exc()
                    stage[0]='failure_close'
                    record['failure_cleanup_status']='waiting_for_drain'
                    try:capture('failure')
                    except BaseException as capture_error:
                        record['failure_capture_error']=type(capture_error).__name__+': '+str(capture_error)
                retain()

            timer = QTimer(window); timer.setInterval(100); timer.timeout.connect(tick); timer.start()

        record['exit_code'] = original_main(['--theme', 'dark'], settings_path=settings,
            diagnostics_root=fixture / 'diagnostics', entry_probe=observe)
        host, controller = refs[0]
        record.update(host_busy=host.busy, controller_close_held=controller.close_held,
                      executor_close_held=host.executor.close_held)
        record['unknown_settings_preserved'] = json.loads(settings.read_bytes()).get('recipe_unknown') == {'keep': 7}
        origins = {}
        for name, module in tuple(sys.modules.items()):
            if name.split('.')[0] in {'core', 'suite_core', 'qt_app', 'runtime_contract'} and getattr(module, '__file__', None):
                path = Path(module.__file__).resolve()
                assert path.is_relative_to(code), 'Application source borrowed outside admitted export'
                origins[name] = path.relative_to(code).as_posix()
        record['application_origins'] = origins
        record['input_bytes_preserved'] = all(digest(p) == pin for p, pin in admitted['input_pins'])
        if scenario == 'observer_failure':
            record['saved_catalog_unchanged']=all(digest(catalog/n)==pin for n,pin in admitted['saved']['saved_files'].items())
            record['saved_settings_unchanged']=digest(settings)==admitted['saved']['saved_settings_sha256']
        if scenario == 'preserve':
            record['saved_files'] = {p.relative_to(catalog).as_posix(): digest(p) for p in catalog.rglob('*') if p.is_file()}
            record['saved_settings_sha256'] = digest(settings)
        record['passed'] = accepted_result(record)
    except BaseException as error:
        record['errors'].append(type(error).__name__ + ': ' + str(error))
        record['traceback'] = traceback.format_exc()
    finally:
        retain()
    return 0 if record['passed'] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recipe', required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        admitted = admit(REPO, args.recipe, args.sha256)
        if args.worker:
            return _worker(admitted, args.sha256)
        if not args.run:
            print(json.dumps({'admitted': True, 'scenario': admitted['recipe']['scenario'], 'writes': False}))
            return 0
        if sys.platform != 'win32':
            raise ValueError('Windows native execution required')
        env = dict(os.environ); env.pop('PYTHONPATH', None)
        command = [sys.executable, '-I', '-B', str(Path(__file__).resolve()), '--worker',
                   '--recipe', str(admitted['recipe_path']), '--sha256', args.sha256]
        # No timeout kill: the child records deadlines and requests guarded Close.
        with subprocess.Popen(command, cwd=admitted['code'], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as child:
            stdout, _ = child.communicate()
            fixture = admitted['fixture']
            if not fixture.exists():
                print(stdout.decode('utf-8', errors='replace'), file=sys.stderr)
                raise ValueError('Worker produced no admitted result; no success inferred')
            guarded(REPO, fixture / 'worker.log').write_bytes(stdout)
            result = _json(guarded(REPO, fixture / 'RESULT.json'))
            if (result.get('pid') != child.pid or result.get('recipe_sha256') != args.sha256
                    or result.get('source_inventory_sha256') != admitted['recipe']['source_inventory_sha256']):
                raise ValueError('Original child/input identity differs')
            result['original_process_retired'] = child.returncode is not None
            result['worker_exit_code'] = child.returncode
            result['passed'] = bool(child.returncode == 0 and accepted_result(result))
            (fixture / 'RESULT.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
            print(json.dumps({'passed': result['passed'], 'result': str(fixture / 'RESULT.json'),
                              'original_process_retired': result['original_process_retired']}))
            if not result['passed']:
                return child.returncode or 1
            return child.returncode
    except (ValueError, OSError, KeyError, TypeError) as error:
        print('Manager recipe held: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
