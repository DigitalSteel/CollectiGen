"""Separate, explicit QtTest recipe policy; ordinary stock recipes stay unchanged."""
from hashlib import sha256
from pathlib import Path
import ast
import importlib.util
import json
import textwrap

PURPOSE='ENCYCLOPEDIA_APPLICATION_OWNED_QT_QUALIFICATION_ONLY'


def derive_qt_stage(baseline,baseline_pin,output,*,a_version,b_version,scenario='baseline'):
    """Exclusive derived stage, keeping ordinary baseline/source/tool bytes intact."""
    from tools.prepare_encyclopedia_qualification_stage import verify_prepared_stage
    from tools import build_manager_distribution as stock
    baseline=stock.unredirected(Path(baseline));output=stock.unredirected(Path(output))
    config=verify_prepared_stage(baseline,baseline_pin)
    if (not config.get('profile_bound') or config.get('include_python_sources') is not False
            or output.exists() or output.is_relative_to(baseline) or baseline.is_relative_to(output)):
        raise ValueError('fresh_separate_bound_qt_stage_required')
    # Fail before writes when primary's optional hooks are not in accepted source.
    ui=ast.parse((baseline/'source/suite_core/encyclopedia/root_recovery_ui.py').read_bytes())
    for name in ('present_original_enrollment','present_original_root_hold'):
        definitions=[n for n in ui.body if isinstance(n,ast.FunctionDef) and n.name==name]
        if len(definitions)!=1 or 'dialog_ready' not in [a.arg for a in definitions[0].args.kwonlyargs]:
            raise ValueError('accepted_primary_dialog_ready_seam_required:'+name)
    source={}
    for key,prefix in (('source_rows','source/'),('generated_rows','source/'),('build_rows','')):
        for row in config[key]: source[prefix+row['path']]=stock.check_row(baseline/prefix/row['path'],row)
    driver_path=Path(__file__).with_name('qt_qualification_driver.py')
    driver_source=driver_path.read_bytes()
    for role in ('root','reader'):
        name='encyclopedia_'+role+'_entry.py'
        raw=render_qt_entry(role,driver_source,a_version=a_version,b_version=b_version,scenario=scenario)
        source['source/'+name]=raw
        row=next(r for r in config['generated_rows'] if r['path']==name)
        row.update(bytes=len(raw),sha256=sha256(raw).hexdigest())
    for name in ('qt_qualification_recipe.py','TagEncyclopediaQtRoot.spec','TagEncyclopediaQtReader.spec'):
        raw=Path(__file__).with_name(name).read_bytes()
        source[name]=raw
        config['build_rows'].append(dict(path=name,bytes=len(raw),sha256=sha256(raw).hexdigest()))
    base_recipe=next(r for r in config['build_rows'] if r['path']=='recipe_support.py')
    config['qualification_qt']=dict(purpose=PURPOSE,driver_sha256=sha256(driver_source).hexdigest(),
        recipe_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),
        base_recipe_sha256=base_recipe['sha256'],scenario=scenario,a_version=a_version,b_version=b_version)
    for name,raw in source.items():stock.write_new(output/name,raw)
    stock.write_new(output/'TOOLCHAIN.json',(baseline/'TOOLCHAIN.json').read_bytes())
    raw=stock.json_bytes(config);stock.write_new(output/'STAGE.json',raw)
    verify_prepared_stage(output,sha256(raw).hexdigest())
    verify_prepared_stage(baseline,baseline_pin)
    return dict(stage=str(output),stage_sha256=sha256(raw).hexdigest(),
        qualification_qt=config['qualification_qt'],built=False,native_launched=False,signed=False)


def render_qt_entry(role,driver_source,*,a_version,b_version,scenario='baseline'):
    from tools.encyclopedia_build.profile_renderer import render_entry
    from suite_core.discovery.component_release_signature import _version
    if role not in ('root','reader') or scenario not in ('baseline','enrollment-recover','recover-root','baseline-ordinary-close-retry'):
        raise ValueError('closed_qt_qualification_role_required')
    _version(a_version);_version(b_version)
    if type(driver_source) is not bytes or len(driver_source)>128*1024:
        raise ValueError('bounded_owned_qt_driver_source_required')
    driver=driver_source.decode('utf-8');ast.parse(driver)
    base=render_entry(role,bound=True).decode('utf-8')
    marker="if __name__ == '__main__':\n    raise SystemExit(run())\n"
    if not base.endswith(marker):raise ValueError('exact_stock_entry_tail_required')
    tail="""if __name__ == '__main__':
    # Custody keeps its stock role and never imports/attaches a UI driver.
    if tuple(sys.argv[1:]) == ('--encyclopedia-custody',):
        raise SystemExit(run())
    def _owned_driver_namespace():
DRIVER_SOURCE
        return locals()
    _drivers = _owned_driver_namespace()
    from functools import partial
    from pathlib import Path
    from encyclopedia_fixture_profile import INPUT
    from suite_core.encyclopedia import __main__ as application, root_recovery_ui
    _retained_drivers=[]
    _output=Path(INPUT['fixture_root'])/'Data/QualificationQt'
    def _reader_ready(window, app):
        # Readonly signed admission decides the reviewed first/target Close route.
        control=window.catalog_host.entry_check.control
        update=_drivers['reader_update_mode'](control,bootstrap_entry.PROFILE,
            sys.argv[1:],a_version=A_VERSION)
        _retained_drivers.append(_drivers['reader_driver'](window,_output,
            update=update,expected_version=B_VERSION,require_native_ui=True))
    def _enrollment_ready(dialog):
        _retained_drivers.append(_drivers['enrollment_driver'](dialog,_output,
            recover=SCENARIO=='enrollment-recover',require_native_ui=True))
    def _recovery_ready(dialog):
        if SCENARIO=='recover-root':
            _retained_drivers.append(_drivers['root_recovery_driver'](dialog,_output,require_native_ui=True))
        elif QUALIFICATION_ROLE=='root' and SCENARIO=='baseline-ordinary-close-retry':
            _retained_drivers.append(_drivers['root_ordinary_close_retry_driver'](dialog,_output,require_native_ui=True))
        else:
            # Unexpected failure is retained for review, never silently recovered.
            _retained_drivers.append(_drivers['QtQualificationDriver'](dialog,
                [_drivers['UiStep']('unexpected-recovery','wait',observed=lambda:False)],
                _output,scenario='unexpected-recovery',timeout=.1,require_native_ui=True))
    application.main=partial(application.main,entry_probe=_reader_ready)
    root_recovery_ui.present_original_enrollment=partial(
        root_recovery_ui.present_original_enrollment,dialog_ready=_enrollment_ready)
    root_recovery_ui.present_original_root_hold=partial(
        root_recovery_ui.present_original_root_hold,dialog_ready=_recovery_ready)
    raise SystemExit(run())
"""
    tail=tail.replace('DRIVER_SOURCE',textwrap.indent(driver,'        '))
    tail=tail.replace('A_VERSION',repr(a_version)).replace('B_VERSION',repr(b_version)).replace('SCENARIO',repr(scenario))
    tail=tail.replace('QUALIFICATION_ROLE',repr(role))
    result=(base[:-len(marker)]+tail).encode('utf-8')
    ast.parse(result)
    return result


def _load_isolated_stock(stage,config):
    path=stage/'recipe_support.py'
    rows=[r for r in config['build_rows'] if r['path']=='recipe_support.py']
    if len(rows)!=1 or sha256(path.read_bytes()).hexdigest()!=rows[0]['sha256']:
        raise ValueError('pinned_ordinary_recipe_required')
    spec=importlib.util.spec_from_file_location('_isolated_qt_qualification_stock',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    if 'PySide6.QtTest' not in module.FORBIDDEN:
        raise ValueError('known_ordinary_qttest_exclusion_required')
    # Separate module instance/policy: every other source/binary/startup guard stays.
    module.FORBIDDEN=module.FORBIDDEN-{'PySide6.QtTest'}
    return module


def build_qt_recipe(role,namespace):
    stage=Path(namespace['SPECPATH']).resolve()
    config=json.loads((stage/'STAGE.json').read_bytes())
    qualification=config.get('qualification_qt')
    fields={'purpose','driver_sha256','recipe_sha256','base_recipe_sha256','scenario','a_version','b_version'}
    if (type(qualification) is not dict or set(qualification)!=fields or qualification.get('purpose')!=PURPOSE
            or qualification['recipe_sha256']!=sha256(Path(__file__).read_bytes()).hexdigest()):
        raise ValueError('explicit_qt_qualification_stage_required')
    row=next((r for r in config['build_rows'] if r['path']=='recipe_support.py'),None)
    if row is None or row['sha256']!=qualification['base_recipe_sha256']:
        raise ValueError('original_qt_base_recipe_pin_required')
    stock=_load_isolated_stock(stage,config)
    analysis=namespace['Analysis']
    def declared_analysis(*args,**kwargs):
        kwargs['hiddenimports']=sorted(set(kwargs['hiddenimports'])|{'PySide6.QtTest'})
        if 'PySide6.QtTest' in kwargs['excludes']:
            raise ValueError('qttest_collection_still_excluded')
        return analysis(*args,**kwargs)
    owned_namespace=dict(namespace,Analysis=declared_analysis)
    return stock.build_recipe(role,owned_namespace)
