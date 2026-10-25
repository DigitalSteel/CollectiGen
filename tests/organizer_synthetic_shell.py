"""Admitted synthetic shell fixture; no guard/function substitutions."""
import ast, json, sys
from pathlib import Path
import pytest

REPO=Path(__file__).resolve().parents[1]

@pytest.fixture
def synthetic_shell_admission(monkeypatch,tmp_path):
    # Reuse exact serialized schema helper rather than invent a catalog payload.
    source=(REPO/'tests/test_default_overrides.py').read_text(encoding='utf-8-sig')
    tree=ast.parse(source)
    selected=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in {'_root','_write'}]
    assert len(selected)==2
    namespace={'Path':Path,'json':json}
    exec(compile(ast.Module(body=selected,type_ignores=[]),'stock-synthetic-default-schema','exec'),namespace)
    root=namespace['_root'](tmp_path)
    defaults=root/'data/defaults'; user=root/'data/user'; cache=root/'data/cache'; cache.mkdir()
    # Existing fixture omits optional empty leaves. Use stock native empty schemas.
    for filename,payload in {
        'relationships.json':{'schema_version':1,'relationships':[]},
        'modifiers.json':{'schema_version':1,'modifiers':[]},
        'concepts.json':{'schema_version':1,'concepts':[]},
        'conflicts.json':{'schema_version':1,'relationships':[]},
        'automation_rules.json':{'schema_version':1,'rules':[]},
    }.items(): namespace['_write'](defaults/filename,payload)
    from core import paths
    old_defaults=paths.DEFAULTS_DIR; old_user=paths.USER_DATA_DIR; old_cache=paths.CACHE_DIR
    old_data=paths.DATA_DIR; old_app=paths.APP_ROOT
    # Include all modules that the shell bootstrap has already copied constants
    # into. Import code only after pytest redirected user/cache into owned paths.
    import core.app_state
    import qt_app.main_window
    changes=[]
    for name,module in tuple(sys.modules.items()):
        if module is None or not (name=='core.paths' or name.startswith(('core.','qt_app.'))): continue
        for attr,value in tuple(vars(module).items()):
            if not attr.isupper() or not isinstance(value,Path): continue
            replacement=None
            if value==old_app: replacement=root
            elif value==old_data: replacement=root/'data'
            elif value==old_defaults or old_defaults in value.parents: replacement=defaults/value.relative_to(old_defaults)
            elif value==old_user or old_user in value.parents: replacement=user/value.relative_to(old_user)
            elif value==old_cache or old_cache in value.parents: replacement=cache/value.relative_to(old_cache)
            elif value==old_data/'optional-components' or (old_data/'optional-components') in value.parents:
                replacement=root/'data/optional-components'/value.relative_to(old_data/'optional-components')
            if replacement is not None:
                assert root==replacement or root in replacement.parents
                monkeypatch.setattr(module,attr,replacement)
                changes.append(name+'.'+attr)
    # Production knowledge factory must sync/admit actual fixture bytes and bind
    # its RulePairAdmission context. No injecting an unadmitted TagDatabase.
    knowledge=core.app_state._load_knowledge()
    assert knowledge.rule_pair_admission.owns(knowledge.path,'knowledge')
    assert knowledge.find('blue') is not None
    assert getattr(knowledge,'_default_identity_context',None) is not None
    assert (user/'default_knowledge_state.json').is_file()
    (tmp_path/'synthetic-admission-receipt.json').write_text(json.dumps({'schema_reuse':'tests/test_default_overrides.py::_root',
        'knowledge_path':str(knowledge.path),'redirected_constants':changes,'guarded_loader':'core.app_state._load_knowledge',
        'default_identity_context_bound':True},indent=2),encoding='utf-8')
    return root
