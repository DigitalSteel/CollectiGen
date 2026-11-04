"""Execute exact public caller text with generated stores and real Qt modal returns.

No ordinary KnowledgePage construction, real store, writer or transaction.
"""
import ast,os
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget,QDialog
from duplicate_custody_test_support import qt_test_scope

FIELDS=('state','tags','categories','characters','editor_context')
SCENARIOS=['stable-changed','stable-unchanged','stable-tag','stable-links','stable-record',
    'constructor-error',*[f'return-{x}' for x in FIELDS],
    'reload','changed','tag-load','record-factory','record-return','record-load']

def method(path):
    tree=ast.parse(path.read_text(encoding='utf8'))
    cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='KnowledgePage')
    node=next(x for x in cls.body if isinstance(x,ast.FunctionDef) and x.name=='show_relationship_workspace')
    return ast.Module(body=[node],type_ignores=[])

@pytest.mark.parametrize('variant',['baseline','candidate'])
@pytest.mark.parametrize('scenario',SCENARIOS)
def test_original_owner_callback_routing(qt_test_scope,variant,scenario):
    root=Path(os.environ['CG_RELATIONSHIP_BASELINE_ROOT']) if variant=='baseline' else Path.cwd()
    source=root/'qt_app/knowledge_page.py'
    page=QWidget();events=[]
    def replaced(field):
        if field=='tags':setattr(page,field,NS(tags={'g':object()}))
        elif field=='characters':setattr(page,field,NS(load=character_load))
        else:setattr(page,field,NS())
    def changed():
        events.append('changed')
        if scenario=='changed':replaced('state')
    def reload():
        events.append('reload')
        if scenario=='reload':replaced('categories')
    def load(*args,**kwargs):
        events.append('tag-load')
        if scenario=='tag-load':replaced('editor_context')
    def character_load():
        events.append('character-load')
        if scenario=='record-load':replaced('characters')
    page.state=NS();page.tags=NS(tags={'g':object()});page.categories=NS()
    page.characters=NS(load=character_load);page.editor_context=NS()
    page.current_tag_id='g';page._tag_save_pending=False;page._flush_tag_save=lambda:None
    page._category_staging_active=lambda:False;page._reload_modifier_roles=reload
    page._changed=changed;page._load_tag_record_into_editor=load
    page._set_tag_save_status=lambda *a,**k:events.append('save-status')
    page.open_automation=NS(emit=lambda *a:events.append('conflict-route'))
    page._open_tag_details=lambda *a,**k:events.append('tag-route')
    page.show_character_tag_links=lambda:events.append('links-route')
    class Main(QDialog):
        def __init__(self,*args,**kwargs):
            if scenario=='constructor-error':raise ValueError('Generated unavailable store')
            super().__init__(page)
            self.changed=scenario not in ['stable-unchanged','stable-record','record-factory','record-return','record-load']
            self.review_tag_id='g' if scenario=='stable-tag' else ''
            self.review_character_links=scenario=='stable-links'
            self.review_character_kind='character' if scenario in ['stable-record','record-factory','record-return','record-load'] else ''
            self.review_character_id='generated' if self.review_character_kind else ''
            self.review_conflict_rule_id='' if self.review_tag_id or self.review_character_links or self.review_character_kind else 'generated-rule'
        def exec(self):
            def complete():
                if scenario.startswith('return-'):replaced(scenario[7:])
                self.accept()
            QTimer.singleShot(0,complete)
            return super().exec()
    class Record(QDialog):
        def __init__(self,*args,**kwargs):
            super().__init__(page);self.saved=True
            if scenario=='record-factory':replaced('categories')
        def exec(self):
            events.append('record-exec')
            def complete():
                if scenario=='record-return':replaced('tags')
                self.accept()
            QTimer.singleShot(0,complete)
            return super().exec()
    page._character_editor_factory=Record
    namespace=dict(RelationshipWorkspaceDialog=Main,CharacterRecordEditService=lambda *a:NS(),
        QMessageBox=NS(warning=lambda *a:events.append('construction-warning'),information=lambda *a:events.append('information')))
    exec(compile(method(source),str(source),'exec'),namespace)
    namespace['show_relationship_workspace'](page)
    stable=['reload','changed','tag-load']
    if scenario=='constructor-error':expected=['construction-warning']
    elif scenario.startswith('return-'):expected=[] if variant=='candidate' else stable+['conflict-route']
    elif scenario=='reload':expected=['reload'] if variant=='candidate' else stable+['conflict-route']
    elif scenario=='changed':expected=['reload','changed'] if variant=='candidate' else stable+['conflict-route']
    elif scenario=='tag-load':expected=stable if variant=='candidate' else stable+['conflict-route']
    elif scenario=='record-factory':expected=[] if variant=='candidate' else ['record-exec','character-load','changed']
    elif scenario=='record-return':expected=['record-exec'] if variant=='candidate' else ['record-exec','character-load','changed']
    elif scenario=='record-load':expected=['record-exec','character-load'] if variant=='candidate' else ['record-exec','character-load','changed']
    elif scenario=='stable-record':expected=['record-exec','character-load','changed']
    elif scenario=='stable-unchanged':expected=['conflict-route']
    else:expected=stable+[{'stable-tag':'tag-route','stable-links':'links-route'}.get(scenario,'conflict-route')]
    assert events==expected,(variant,scenario,events,expected)
    page.close();page.deleteLater();qt_test_scope.settle(2)
