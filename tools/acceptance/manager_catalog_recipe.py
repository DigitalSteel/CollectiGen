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
# Exact reviewed read-only feedback projection cohort; unknown native changes still refuse.
FEEDBACK_SOURCE_BRIDGE = {'suite_core/manager/window.py': ['ea7343fcdc3d4d8929aef1ac5e7d3d82a1853a3d390389bc231db9f40663c7ac', '32db432e47c09e1da23038909d78cfe193794447862d033d25394d5f36371358'], 'suite_core/manager/tag_database_controller.py': ['56a51081ad9e2fe2b7ba4843a97fdb55babfe288d4e469b01a13945a81fd4841', 'ea9b50468164cc04d18714b12dae8cda31fcc717940b825badc89bdecd58747a'], 'suite_core/encyclopedia/catalog_ui.py': ['894ab55c2e25509cd785adda1da03a775fcaaf515ce729304d9cb9499de3cf7f', 'cdd9cc3e7fdd289fab8741a0ce49dcb8689d271aa08608dc4a2b0d9445860c51'], 'suite_core/tests/test_tag_database_apply_delivery.py': ['8ba1cfa521f037f15d149446967560c172324d2f0b0568d8f69cc15eb0fde482', 'e7aefe647f0145d566653f34807a6d7bd082f5618faadbdea09dbbf7f4e619dc']}
EXPECTED_OBSERVER_FAILURE = 'RuntimeError: Synthetic observer failure before admission'
CLOSE_SCENARIOS = frozenset({'close_before_admission','close_after_deadline'})
FEEDBACK_SCENARIOS = frozenset({'cancel_before_admission','cancel_after_admission'})
BRIDGED_SCENARIOS = frozenset({'context','observer_failure'}) | CLOSE_SCENARIOS | FEEDBACK_SCENARIOS
SAVED_SCENARIOS = frozenset({'reopen'}) | BRIDGED_SCENARIOS


@lru_cache(maxsize=4)
def _stock(name, file):
    # The shared bootstrap guards are stdlib-only. Do not import a checkout's
    # tools/core packages into the selected application's fresh worker process.
    return runpy.run_path(str(Path(__file__).with_name(file)), run_name='_guard')[name]


def guarded(repo, value):
    return _stock('workspace_path', 'organizer_package_probe.py')(repo, value)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def accepted_result(record, *, require_retirement=False):
    """A process exit or screenshot alone never establishes recipe acceptance."""
    if not isinstance(record, dict) or not isinstance(record.get('captures'), dict) or not isinstance(record.get('application_origins'), dict):
        return False
    scenario = record.get('scenario')
    if scenario in FEEDBACK_SCENARIOS:
        return feedback_result(record,require_retirement=require_retirement)
    if scenario in CLOSE_SCENARIOS:
        return close_recovery_result(record,require_retirement=require_retirement)
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



def feedback_result(record, *, require_retirement=True):
    if not isinstance(record,dict) or record.get('scenario') not in FEEDBACK_SCENARIOS:return False
    facts=record.get('close_facts',{})
    if not isinstance(facts,dict) or not isinstance(facts.get('final'),dict):return False
    final=facts['final'];policy=final.get('policy',{})
    if not isinstance(policy,dict) or not isinstance(policy.get('decision'),dict):return False
    rows=record.get('feedback_facts',{});captures=record.get('feedback_captures',{})
    expected={n+'-'+t for n in ['waiting','cancel-requested','settled'] for t in ['dark','light']}
    if not isinstance(rows,dict) or set(rows)!=expected or not isinstance(captures,dict) or set(captures)!=expected:return False
    waiting=[rows[n+'-'+t] for n in ['waiting','cancel-requested'] for t in ['dark','light']]
    return bool(record.get('schema_version')==1 and record.get('level')=='source_component' and record.get('platform')=='windows'
        and record.get('application_origins') and record.get('unknown_settings_preserved') is True
        and record.get('ordinary_branch') is True and record.get('exit_code')==0 and record.get('errors')==[]
        and record.get('blocked_events')==[] and record.get('saved_catalog_unchanged') is True
        and record.get('saved_settings_unchanged') is True and record.get('input_bytes_preserved') is True
        and record.get('draft_preserved') is True and record.get('no_false_result') is True
        and record.get('close_accepted') is True and record.get('events')==['visible_open_saved_once','visible_cancel_once','normal_close_once']
        and all(isinstance(r,dict) and r.get('pane_enabled') is True and r.get('cancel_enabled') is True
            and r.get('pending') is True and r.get('indeterminate') is True and r.get('elapsed_visible') is True
            and isinstance(r.get('mutation_controls'),dict) and len(r['mutation_controls'])==8
            and all(v is False for v in r['mutation_controls'].values()) for r in waiting)
        and all(final.get(k) is False for k in ['pending','controller_held','executor_held','thread_alive','host_busy'])
        and final.get('future_done') is True and final.get('host_jobs')==0
        and facts.get('before',{}).get('admitted') is (record['scenario']=='cancel_after_admission')
        and facts.get('after_cancel',{}).get('cancelled') is True
        and all(isinstance(rows[n+'-'+t].get('elapsed_seconds'),int) for n in ['waiting','cancel-requested'] for t in ['dark','light'])
        and all(rows['cancel-requested-'+t]['elapsed_seconds']>rows['waiting-'+t]['elapsed_seconds'] for t in ['dark','light'])
        and policy.get('closed') is True and policy.get('decision',{}).get('allow_close') is True
        and not record.get('close_ack_error')
        and (not require_retirement or record.get('original_process_retired') is True and record.get('worker_exit_code')==0))

def valid_personal_witness(value):
    """Reject empty, omitted, changed or merely exit-based preservation claims."""
    if not isinstance(value, dict) or not value.get('tag_id') or not value.get('category_id'):
        return False
    rows = value.get('files')
    return bool(value.get('decoded_verified') is True and isinstance(rows, dict)
        and set(rows) == {'data/user/tags.json', 'data/user/categories.json'}
        and all(isinstance(row, dict) and re.fullmatch('[0-9a-f]{64}', row.get('before', ''))
            and row.get('before') == row.get('after') for row in rows.values()))


def layout_result(record):
    """Finite action visibility/keyboard evidence supplements native saved authority."""
    if not isinstance(record,dict) or record.get('scenario') not in {'preserve','reopen'}:return False
    states=['entry','decision','saved'] if record['scenario']=='preserve' else ['entry','reopened']
    expected={state+'-'+theme for state in states for theme in ['dark','light']}
    rows=record.get('layout_facts');captures=record.get('layout_captures')
    return bool(isinstance(rows,dict) and set(rows)==expected and isinstance(captures,dict)
        and set(captures)==expected and all(isinstance(row,dict)
            and row.get('keyboard_disclosure') is True and row.get('draft_approval_preserved') is True
            and isinstance(row.get('actions_in_view'),dict)
            and set(row['actions_in_view'])=={'preview','confirm','apply','receipts','recover','observe','open_encyclopedia'}
            and all(value is True for value in row['actions_in_view'].values())
            and row.get('detail_selector_keyboard_reached',True) is True for row in rows.values()))


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


def close_recovery_result(record, *, require_retirement=True):
    """Admission, drained owners and final policy acknowledgement are distinct."""
    if not isinstance(record,dict) or record.get('scenario') not in CLOSE_SCENARIOS:
        return False
    facts=record.get('close_facts')
    if not isinstance(facts,dict) or not isinstance(record.get('captures'),dict):return False
    if not all(isinstance(facts.get(k,{}),dict) for k in ['before','after_busy_close','final','deadline','drained','after_cancel']):return False
    if not all(isinstance(f.get('policy',{}),dict) for f in facts.values() if isinstance(f,dict)):return False
    before=facts.get('before',{});cancelled=facts.get('after_busy_close',{});final=facts.get('final',{})
    policy=final.get('policy',{});decision=policy.get('decision',{})
    if not isinstance(decision,dict):return False
    common=bool(record.get('schema_version')==1 and record.get('level')=='source_component'
        and record.get('platform')=='windows' and record.get('exit_code')==0
        and record.get('ordinary_branch') is True and record.get('errors')==[] and record.get('blocked_events')==[]
        and record.get('busy_close_accepted') is False and before.get('pending') is True
        and cancelled.get('cancelled') is True and final.get('future_done') is True
        and all(final.get(k) is False for k in ['pending','controller_held','executor_held','thread_alive','host_busy'])
        and final.get('host_jobs')==0 and policy.get('closed') is True
        and decision.get('state')=='closed' and decision.get('allow_close') is True
        and record.get('saved_catalog_unchanged') is True and record.get('saved_settings_unchanged') is True
        and record.get('input_bytes_preserved') is True and record.get('unknown_settings_preserved') is True
        and isinstance(record.get('application_origins'),dict) and record.get('application_origins')
        and policy.get('expired') is False and cancelled.get('policy',{}).get('pending') is True
        and not record.get('close_ack_error'))
    if not common or (require_retirement and not (record.get('original_process_retired') is True and record.get('worker_exit_code')==0)):
        return False
    captures=record.get('captures',{})
    if record['scenario']=='close_before_admission':
        return bool(before.get('admitted') is False and final.get('admitted') is False
            and policy.get('request_id')==cancelled.get('policy',{}).get('request_id')
            and record.get('events')==['visible_open_saved_once','busy_close_once']
            and set(captures)=={'entry','closing'})
    deadline=facts.get('deadline',{});drained=facts.get('drained',{});renewed=facts.get('after_cancel',{})
    return bool(before.get('admitted') is True and final.get('admitted') is True and deadline.get('policy',{}).get('expired') is True
        and deadline.get('pending') is True and drained.get('policy',{}).get('expired') is True
        and all(drained.get(k) is False for k in ['pending','executor_held','host_busy'])
        and renewed.get('policy',{}).get('expired') is False
        and isinstance(policy.get('request_id'),int) and isinstance(renewed.get('policy',{}).get('request_id'),int)
        and policy['request_id']>renewed['policy']['request_id']>deadline.get('policy',{}).get('request_id',policy['request_id'])
        and record.get('fresh_close_accepted') is True
        and record.get('events')==['visible_open_saved_once','busy_close_once','visible_cancel_close_once','fresh_close_once']
        and set(captures)=={'entry','closing','deadline-held','drained-held','cancelled-intent'})


def close_snapshot(window,host,controller,operation,future):
    """Observe existing owners and policy; never request Close or poll delivery."""
    from dataclasses import asdict
    from enum import Enum
    def plain(value):
        if isinstance(value,Enum):return value.value
        if isinstance(value,dict):return {k:plain(v) for k,v in value.items()}
        if isinstance(value,(tuple,list)):return [plain(v) for v in value]
        return value
    thread=host.executor._thread
    return {'admitted':operation.admitted,'cancelled':operation.cancelled.is_set(),
        'future_done':future.done(),'pending':controller.pending,'controller_held':controller.close_held,
        'executor_held':host.executor.close_held,'thread_alive':None if thread is None else thread.is_alive(),
        'host_busy':host.busy,'host_jobs':len(host._operation_jobs),
        'context':plain(asdict(window.close_context())),
        'policy':{'pending':window.close_policy.pending,'closed':window.close_policy.closed,
            'expired':window.close_policy.expired,'request_id':window.close_policy.request_id,
            'decision':plain(asdict(window.close_policy.decision))}}


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
    if value.get('scenario') in SAVED_SCENARIOS:
        required |= {'saved_result', 'saved_result_sha256'}
    if value.get('scenario') in BRIDGED_SCENARIOS:
        required |= {'saved_source_root','saved_source_inventory_sha256'}
    if set(value) != required or value['schema_version'] != 1 or value['scenario'] not in SAVED_SCENARIOS | {'stale','preserve'}:
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
    if value['scenario'] in SAVED_SCENARIOS:
        saved_source_pin = value['source_inventory_sha256']
        if value['scenario'] in BRIDGED_SCENARIOS:
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
                        pair=[old_inventory['files'][name]['sha256'],rows.get(name,{}).get('sha256')]
                        if value['scenario'] not in FEEDBACK_SCENARIOS | CLOSE_SCENARIOS or FEEDBACK_SOURCE_BRIDGE.get(name)!=pair:
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
    if scenario in SAVED_SCENARIOS:
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
                if scenario in SAVED_SCENARIOS and _existing_saved_lock(event, path, args, catalog):
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
            close_operation=[None];close_future=[None]
            if scenario in BRIDGED_SCENARIOS:
                if scenario in {'close_after_deadline','cancel_after_admission'}:
                    from core import tag_catalog_connection as connection
                    stock_open=connection.open_saved_connection
                    def delayed_saved_open(*args):
                        assert open_release.wait(20), 'Labelled admitted-read delay expired'
                        return stock_open(*args)
                    connection.open_saved_connection=delayed_saved_open
                    record['fixture_hook']='Delay saved read after original operation admission; original read unchanged'
                else:
                    import suite_core.manager.tag_database_controller as delivery
                    stock_connect=delivery._connect
                    def delayed_connect(*args):
                        assert open_release.wait(15 if scenario in CLOSE_SCENARIOS | FEEDBACK_SCENARIOS else 5), 'Labelled fixture saved-open delay expired'
                        return stock_connect(*args)
                    delivery._connect=delayed_connect
                    record['fixture_hook']='Delay only; calls unchanged original connection worker'
                draft=('synthetic-draft-source','synthetic-draft-catalog',True)
                def assert_draft():
                    assert (pane.source_directory.text(),pane.catalog_directory.text(),pane.create_catalog.isChecked())==draft
                saved_settings=settings.read_bytes()

            def keep_close_fact(name):
                record.setdefault('close_facts',{})[name]=close_snapshot(window,host,controller,close_operation[0],close_future[0])

            if scenario in CLOSE_SCENARIOS | FEEDBACK_SCENARIOS:
                def final_acknowledgement():
                    try:keep_close_fact('final')
                    except BaseException as error:record['close_ack_error']=type(error).__name__+': '+str(error)
                    retain()
                app.aboutToQuit.connect(final_acknowledgement)

            def capture(name):
                path = output / (name + '.png')
                assert window.grab().save(str(path))
                record['captures'][name] = {'path': str(path), 'sha256': digest(path)}
                record['logical_size'] = [window.width(), window.height()]
                record['dpr'] = window.devicePixelRatioF()
                if scenario in {'preserve','reopen'} and hasattr(pane,'detail_options'):
                    review_layout(name)

            def review_layout(name):
                """Finite visible disclosure/focus and theme observations; no data actions."""
                from qt_app.theme import apply_theme
                original=(controller.review,pane.confirm.isChecked(),pane.catalog.currentIndex(),
                    pane.source_directory.text(),pane.catalog_directory.text(),pane.create_catalog.isChecked())
                was_open=pane.detail_options.isChecked()
                rows=record.setdefault('layout_facts',{})
                for theme in ['dark','light']:
                    apply_theme(app,theme);app.processEvents()
                    facts={'actions_in_view':{},'keyboard_disclosure':False,'draft_approval_preserved':False}
                    for control in ['preview','confirm','apply','receipts','recover','observe','open_encyclopedia']:
                        widget=getattr(pane,control)
                        rect=widget.rect();rect.moveTopLeft(widget.mapTo(window,rect.topLeft()))
                        facts['actions_in_view'][control]=widget.isVisible() and window.rect().contains(rect)
                    assert all(facts['actions_in_view'].values()),facts
                    # Reach the existing disclosure through its visible home, then use keyboard.
                    ancestor=pane.detail_options.parentWidget()
                    while ancestor is not None:
                        if isinstance(ancestor,QScrollArea):ancestor.ensureWidgetVisible(pane.detail_options);break
                        ancestor=ancestor.parentWidget()
                    pane.detail_options.setFocus();app.processEvents()
                    prior=pane.detail_options.isChecked()
                    QTest.keyClick(pane.detail_options,Qt.Key_Space);app.processEvents()
                    assert pane.detail_options.isChecked() is not prior
                    QTest.keyClick(pane.detail_options,Qt.Key_Space);app.processEvents()
                    assert pane.detail_options.isChecked() is prior
                    facts['keyboard_disclosure']=True
                    assert original==(controller.review,pane.confirm.isChecked(),pane.catalog.currentIndex(),
                        pane.source_directory.text(),pane.catalog_directory.text(),pane.create_catalog.isChecked())
                    facts['draft_approval_preserved']=True
                    if pane.detail_options.isChecked():
                        pane.detail_options.setFocus();app.processEvents()
                        QTest.keyClick(pane.detail_options,Qt.Key_Tab);app.processEvents()
                        facts['detail_selector_keyboard_reached']=pane.sections.hasFocus()
                        facts['focus_after_tab']=type(app.focusWidget()).__name__
                        rows[name+'-'+theme]=facts
                        assert facts['detail_selector_keyboard_reached']
                    path=output/(name+'-'+theme+'-actions.png');assert window.grab().save(str(path))
                    record.setdefault('layout_captures',{})[name+'-'+theme]={'path':str(path),'sha256':digest(path)}
                    rows[name+'-'+theme]=facts
                apply_theme(app,'dark');app.processEvents()
                assert pane.detail_options.isChecked() is was_open

            def draft_layout_capture(name):
                from qt_app.theme import apply_theme
                ancestor=pane.catalog.parentWidget()
                while ancestor is not None and not isinstance(ancestor,QScrollArea):ancestor=ancestor.parentWidget()
                assert ancestor is not None
                ancestor.verticalScrollBar().setValue(0);app.processEvents()
                for theme in ['dark','light']:
                    apply_theme(app,theme);app.processEvents()
                    facts={'scroll':ancestor.verticalScrollBar().value(),
                        'expanded':pane.connection_options.isChecked(),'form_visible':pane.connection_box.isVisible(),
                        'context_before_draft':pane.catalog.mapTo(window,pane.catalog.rect().topLeft()).y()<pane.connection_options.mapTo(window,pane.connection_options.rect().topLeft()).y(),
                        'draft':(pane.source_directory.text(),pane.catalog_directory.text(),pane.create_catalog.isChecked()),
                        'selected':pane.catalog.currentIndex(),'status':pane.status.text()}
                    assert facts['scroll']==0 and facts['context_before_draft']
                    p=output/('draft-'+name+'-'+theme+'.png');assert window.grab().save(str(p))
                    record.setdefault('draft_layout',{})[name+'-'+theme]={'path':str(p),'sha256':digest(p),'facts':facts}
                apply_theme(app,'dark');app.processEvents()

            def draft_layout_roundtrip():
                original=(controller.review,pane.confirm.isChecked(),pane.catalog.currentIndex(),
                    pane.source_directory.text(),pane.catalog_directory.text(),pane.create_catalog.isChecked(),pane.confirm_connection.isChecked())
                draft_layout_capture('connected-expanded')
                window.activateWindow();app.setActiveWindow(window);pane.connection_options.setFocus();app.processEvents()
                assert pane.connection_options.hasFocus();QTest.keyClick(pane.connection_options,Qt.Key_Space);app.processEvents()
                assert not pane.connection_options.isChecked() and not pane.connection_box.isVisible()
                draft_layout_capture('connected-collapsed')
                pane.connection_options.setFocus();QTest.keyClick(pane.connection_options,Qt.Key_Space);app.processEvents()
                assert pane.connection_options.isChecked() and pane.connection_box.isVisible()
                draft_layout_capture('returned')
                assert original==(controller.review,pane.confirm.isChecked(),pane.catalog.currentIndex(),
                    pane.source_directory.text(),pane.catalog_directory.text(),pane.create_catalog.isChecked(),pane.confirm_connection.isChecked())
                record['draft_disclosure_preserved']=True

            def feedback_capture(name):
                from qt_app.theme import apply_theme
                for theme in ['dark','light']:
                    apply_theme(app,theme);app.processEvents()
                    waiting=name!='settled'
                    flags={key:getattr(pane,key).isEnabled() for key in ['preview','apply','reload','catalog','source_directory','catalog_directory','connect_catalog','review_connection']}
                    facts={'pane_enabled':pane.isEnabled(),'cancel_enabled':pane.cancel.isEnabled(),
                        'pending':controller.pending,'phase':pane.status.text(),'elapsed':pane.elapsed.text(),
                        'elapsed_visible':pane.elapsed.isVisible(),'mutation_controls':flags,
                        'indeterminate':pane.progress.isVisible() and pane.progress.minimum()==pane.progress.maximum()==0}
                    record.setdefault('feedback_facts',{})[name+'-'+theme]=facts
                    record['feedback_guard']={'eligible':host.saved_open_feedback_available(),
                        'same_controller':host.controller is controller,'same_executor':controller.executor is host.executor,
                        'same_future':host.executor._future is controller._future,'foreign_work':host._other_work_busy(saved_feedback=True),
                        'controller_faulted':controller.faulted,'unconfirmed':controller._unconfirmed,
                        'executor_uncertain':host.executor._uncertain,'executor_closed':host.executor.closed,
                        'host_closing':host._closing,'close_pending':window.close_policy.pending,
                        'kind':controller.pending_feedback[1] if controller.pending_feedback else None}
                    retain()
                    if waiting:facts['elapsed_seconds']=max(0,int(pane._feedback_clock()-pane._feedback_started))
                    if waiting:
                        assert facts['pane_enabled'] and facts['cancel_enabled'] and facts['indeterminate']
                        assert facts['elapsed_visible'] and not any(flags.values())
                    path=output/(name+'-'+theme+'.png');assert window.grab().save(str(path))
                    record.setdefault('feedback_facts',{})[name+'-'+theme]=facts
                    record.setdefault('feedback_captures',{})[name+'-'+theme]={'path':str(path),'sha256':digest(path)}
                apply_theme(app,'dark');app.processEvents()

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
                    if scenario in FEEDBACK_SCENARIOS and stage[0]=='context_wait' and controller.pending:
                        if time.monotonic()-record['open_started']<1.3:return
                        operation=controller._operation
                        assert operation.admitted is (scenario=='cancel_after_admission')
                        close_operation[0]=operation;close_future[0]=controller._future
                        keep_close_fact('before');feedback_capture('waiting')
                        assert pane.isEnabled() and pane.cancel.isEnabled()
                        window.activateWindow();app.setActiveWindow(window);pane.cancel.setFocus();app.processEvents()
                        record['cancel_keyboard_focus']=pane.cancel.hasFocus();assert record['cancel_keyboard_focus']
                        QTest.keyClick(pane.cancel,Qt.Key_Space)
                        record['events'].append('visible_cancel_once');keep_close_fact('after_cancel')
                        record['cancel_started']=time.monotonic();stage[0]='feedback_cancel_wait';retain();return
                    if scenario in FEEDBACK_SCENARIOS and stage[0]=='feedback_cancel_wait':
                        if time.monotonic()-record['cancel_started']<1.3:return
                        assert controller._future is close_future[0] and controller.pending and controller.cancel_requested
                        feedback_capture('cancel-requested');open_release.set();stage[0]='feedback_settle';retain();return
                    if scenario in FEEDBACK_SCENARIOS and stage[0]=='feedback_settle':
                        if controller.pending or host.executor.close_held or host.busy:return
                        assert not pane.elapsed.isVisible() and not pane.cancel.isVisible()
                        record['draft_preserved']=(pane.source_directory.text(),pane.catalog_directory.text(),pane.create_catalog.isChecked())==draft
                        record['no_false_result']=controller.result is None if scenario=='cancel_before_admission' else not controller.error
                        draft_layout_roundtrip()
                        keep_close_fact('settled');feedback_capture('settled')
                        record['events'].append('normal_close_once');record['close_accepted']=bool(window.close());timer.stop();retain();return
                    if scenario in CLOSE_SCENARIOS and stage[0]=='context_wait' and controller.pending:
                        operation=controller._operation
                        if time.monotonic()-record['open_started']<.35:return
                        if scenario=='close_after_deadline' and not operation.admitted:return
                        assert operation.admitted is (scenario=='close_after_deadline')
                        close_operation[0]=operation;close_future[0]=controller._future
                        keep_close_fact('before')
                        record['busy_close_accepted']=bool(window.close())
                        record['events'].append('busy_close_once');keep_close_fact('after_busy_close');capture('closing')
                        record['close_started']=time.monotonic();stage[0]='close_settle'
                        if scenario=='close_before_admission':open_release.set()
                        retain();return
                    if scenario in CLOSE_SCENARIOS and stage[0]=='close_settle':
                        if scenario=='close_after_deadline':
                            if not open_release.is_set() and time.monotonic()-record['close_started']>6:
                                assert window.close_policy.expired
                                keep_close_fact('deadline');capture('deadline-held');open_release.set();retain()
                            if not controller.pending and not host.executor.close_held and not host.busy:
                                stage[0]='close_recover'
                                keep_close_fact('drained');capture('drained-held')
                                assert window.close_policy.expired
                                click(window.cancel_close_button);record['events'].append('visible_cancel_close_once')
                                keep_close_fact('after_cancel');capture('cancelled-intent')
                                stage[0]='close_requested';record['events'].append('fresh_close_once')
                                record['fresh_close_accepted']=bool(window.close());retain()
                                if record['fresh_close_accepted']:timer.stop()
                                else:
                                    record['errors'].append('Fresh Close held; retain original owner for supervision')
                                    record['failure_cleanup_status']='held_for_supervision'
                                    stage[0]='failure_settled';timer.stop();retain()
                        return
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
                        if scenario in FEEDBACK_SCENARIOS:draft_layout_capture('initial-empty')
                        if scenario in BRIDGED_SCENARIOS:
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
        if scenario == 'observer_failure' or scenario in CLOSE_SCENARIOS | FEEDBACK_SCENARIOS:
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
            result['passed'] = bool(child.returncode == 0 and accepted_result(result,require_retirement=True))
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
