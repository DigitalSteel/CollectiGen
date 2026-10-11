# Manager-only candidate recipe; native execution requires separate frozen-plan approval.
from pathlib import Path
import hashlib, json, sys
root = Path(SPECPATH).resolve()
config = json.loads((root/'STAGE.json').read_bytes())
source = root/'source'
rows = config['source_rows']
for row in rows:
    path = source/row['path']
    if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
        raise ValueError('staged source drift: '+row['path'])
datas = [(str(source/r['path']), str(Path(r['path']).parent)) for r in rows]
allowed = {r['path'][:-3].replace('/', '.').removesuffix('.__init__') for r in rows if r['path'].endswith('.py')}
allowed |= {'.'.join(n.split('.')[:i]) for n in tuple(allowed) for i in range(1,len(n.split('.')))}
local_top_level = {module.partition('.')[0] for module in allowed} | {'runtime_contract'}
hidden = sorted(allowed | {'PySide6.QtCore','PySide6.QtGui','PySide6.QtWidgets','shiboken6','velopack','velopack.velopack'})
reviewed_exclusions = set(['core.ai_interrogation', 'core.automation_repository', 'core.automation_rules', 'core.category_base_relationships', 'core.conflict_policy', 'core.default_sync', 'core.default_tag_identity', 'core.dependencies', 'core.external_write_transaction', 'core.image_knowledge_reconciliation', 'core.knowledge_lifecycle', 'core.knowledge_pack', 'core.managed_model_identity', 'core.modifier_composition', 'core.relationship_evaluator', 'core.resource_browser_view', 'core.reviewed_tag_semantics', 'core.rule_intents', 'core.structured_history_configuration', 'core.swarm_onboarding_receipts', 'core.tag_alias_relationships', 'core.tag_order_workspace', 'core.tag_storage_decode', 'core.tag_template_projection', 'core.training_package'])
reviewed_exclusions -= allowed  # Current explicitly required closure supersedes old optional exclusions.
sdk = root/'sdk'
sdk_extension = sdk/'velopack/velopack.pyd'
sdk_datas = [(str(sdk/r['path']), str(Path(r['path']).parent)) for r in config['sdk_rows'] if not r['path'].endswith(('.py','.pyd'))]
notice_datas = [(str(root/r['path']), 'licenses') for r in config['notice_rows']]
a = Analysis([str(source/'public_root_entry.py')], pathex=[str(source),str(sdk)], binaries=[(str(sdk_extension),'velopack')], datas=datas+sdk_datas+notice_datas,
    hiddenimports=hidden, hookspath=[], runtime_hooks=[],
    excludes=sorted(reviewed_exclusions | {'qt_app.main_window','core.app_state','PyQt5','PyQt6','PySide2','PIL','av','suite_core.tests'}), noarchive=False)
for name, origin, kind in a.pure:
    if name == 'core.app_state' or name.startswith(('core.app_state.','suite_core.tests','qt_app.main_window')):
        raise ValueError('excluded application/test module collected: '+name)
    if (name == 'core' or name.startswith(('core.','qt_app.','suite_core.')) or name.partition('.')[0] in local_top_level) and name not in allowed:
        raise ValueError('undeclared local module collected: '+name)
    if origin and Path(origin).is_file() and Path(origin).resolve().is_relative_to(source):
        relative=Path(origin).resolve().relative_to(source).as_posix()
        if relative not in {r['path'] for r in rows}:
            raise ValueError('undeclared source origin: '+relative)
missing_local = sorted({node.identifier for node in a.graph.flatten()
    if type(node).__name__ == 'MissingModule' and (node.identifier.partition('.')[0] in local_top_level)})
excluded_local = sorted({node.identifier for node in a.graph.flatten() if type(node).__name__ == 'ExcludedModule' and (node.identifier.partition('.')[0] in local_top_level)})
unclassified_excluded = sorted(set(excluded_local) - reviewed_exclusions - {'core.app_state','qt_app.main_window','suite_core.tests'})
if unclassified_excluded:
    raise ValueError('unclassified local exclusions: '+', '.join(unclassified_excluded))
closure_report = dict(reviewed_exclusions=sorted(reviewed_exclusions), excluded_local_modules=excluded_local, status='ANALYSIS_MODULE_BOUND_PASSED' if not missing_local else 'ANALYSIS_MODULE_BOUND_INSUFFICIENT',
    allowed_local_modules=sorted(allowed), actual_pure_modules=sorted(name for name, origin, kind in a.pure),
    missing_local_modules=missing_local, source_rows=len(rows), product_apps_started=False)
(root.parent/'CollectiGen_ANALYSIS_CLOSURE.json').write_text(json.dumps(closure_report,indent=2)+'\n',encoding='utf-8')
if missing_local:
    raise ValueError('declared-module bound insufficient: missing local dependencies: '+', '.join(missing_local))
sys.path.insert(0,str(root))
from sdk_binary_origin import validate_with_sdk
validate_with_sdk(a.binaries, sdk_extension, config["sdk_extension_sha256"])
excluded_system_msvc={'msvcp140_1.dll', 'msvcp140_2.dll', 'msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'}
removed_system_msvc=[row for row in a.binaries if Path(row[0]).name.lower() in excluded_system_msvc]
a.binaries=[row for row in a.binaries if Path(row[0]).name.lower() not in excluded_system_msvc]
(root.parent/'CollectiGen_SYSTEM_MSVCP_POLICY.json').write_text(json.dumps(dict(policy='existing-system-prerequisite',minimum_version='14.44.35211.0',removed=removed_system_msvc,static_dependency_review_only=True),indent=2)+'\n',encoding='utf-8')
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='CollectiGen',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=True, contents_directory='_bootstrap')
bundle = COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,name='CollectiGen')
