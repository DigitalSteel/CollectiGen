import hashlib
import json
from pathlib import Path
from threading import Event
import pytest
from core.encyclopedia_connection import (review_directory, ReaderConnectionStore,
    OwnedEncyclopedia, EncyclopediaHold, KEY, root_reader_selection)


def package(root):
    file = root / "TagEncyclopedia/TagEncyclopedia.exe"
    file.parent.mkdir(parents=True); file.write_bytes(b"inert reader fixture")
    row = {"path": "TagEncyclopedia/TagEncyclopedia.exe", "size_bytes": file.stat().st_size,
           "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
    (root / "BOOTSTRAP_INVENTORY.json").write_text(json.dumps({"schema_version":1,
        "component_id":"collectigen.tag-encyclopedia","software_version":"0.1.0-alpha.8",
        "platform":{"os":"windows","architecture":"x86_64"},"runtime_kind":"frozen-portable",
        "files":[row]}),encoding="utf-8")
    return review_directory(root)


def test_connection_saved_with_unknown_fields_and_stale_guard(tmp_path):
    selected=package(tmp_path/'package'); path=tmp_path/'settings.json'
    path.write_text(json.dumps({'future':{'keep':7},KEY:{'schema_version':1,
        'directory':str(selected.directory),'inventory_sha256':selected.inventory_sha256,
        'version':selected.version,'future_reader':8}}),encoding='utf-8')
    store=ReaderConnectionStore(path); before=store.observe()
    saved=store.save(selected,expected=before)
    assert store.selected(saved)==selected
    assert saved._payload['future']=={'keep':7} and saved._payload[KEY]['future_reader']==8
    path.write_text(json.dumps({'future':9}),encoding='utf-8')
    with pytest.raises(EncyclopediaHold,match='Settings changed'):store.save(selected,expected=saved)
    assert json.loads(path.read_text())=={'future':9}


@pytest.mark.parametrize('drift',['extra','payload','inventory'])
def test_changed_package_held_before_factory(tmp_path,drift):
    selected=package(tmp_path/'package')
    if drift=='extra':(selected.directory/'surprise.exe').write_bytes(b'unlisted')
    if drift=='payload':selected.program.write_bytes(b'changed')
    if drift=='inventory':(selected.directory/'BOOTSTRAP_INVENTORY.json').write_bytes(b'{}')
    calls=[];owner=OwnedEncyclopedia(factory=lambda:calls.append(True))
    with pytest.raises(Exception):owner.launch(selected)
    assert calls==[] and not owner.close_held


class Native:
    def __init__(self):
        self.pid=0
        self.job=self;self.child=self;self.code=None;self.members=1;self.closed=False
        self.drained=Event();self.drained.set();self.drain=None
    def spawn_command(self,command,cwd,environment,**kwargs):
        self.command=command;self.cwd=cwd;return self
    def identity(self,child,program):return {'original':1}
    def check_identity(self,*args):pass
    def poll(self):return self.code
    def active(self):return self.members
    def terminate_owned(self,child):self.code=1;self.members=0
    def close(self,child):self.closed=True


def test_original_owner_retained_until_exit_members_and_drain(tmp_path):
    selected=package(tmp_path/'package');native=Native();owner=OwnedEncyclopedia(factory=lambda:native)
    owner.launch(selected)
    assert native.command==(str(selected.program),) and owner.close_held
    with pytest.raises(EncyclopediaHold):owner.launch(selected)
    native.code=0
    assert not owner.poll() and not native.closed
    native.members=0;native.drained.clear()
    assert not owner.poll() and not native.closed
    native.drained.set()
    assert owner.poll() and native.closed and owner.last_exit==0 and not owner.close_held


def test_frozen_root_not_bypassed(tmp_path):
    selected=package(tmp_path/'package');calls=[]
    owner=OwnedEncyclopedia(factory=lambda:calls.append(1),launch_allowed=False)
    with pytest.raises(EncyclopediaHold,match='root owner'):owner.launch(selected)
    assert calls==[]


def test_failed_start_retains_original_for_explicit_stop(tmp_path):
    selected=package(tmp_path/'package');native=Native()
    def fail(*args):raise OSError('identity unavailable')
    native.identity=fail;owner=OwnedEncyclopedia(factory=lambda:native)
    with pytest.raises(OSError):owner.launch(selected)
    assert owner.close_held and owner.child is native
    owner.stop_failed_start();assert owner.poll() and native.closed


def test_root_selection_rechecks_saved_identity_and_clear_preserves_settings(tmp_path):
    root=tmp_path/'deployment';selected=package(root/'reader')
    data=root/'Data';data.mkdir();path=data/'settings.json'
    path.write_text(json.dumps({'future':3}),encoding='utf-8')
    store=ReaderConnectionStore(path)
    assert root_reader_selection(root,path) is None
    saved=store.save(selected,expected=store.observe())
    admission=root_reader_selection(root,path)
    assert admission.program_sha256==hashlib.sha256(selected.program.read_bytes()).hexdigest()
    assert admission.context()['directory']==str(selected.directory)
    cleared=store.clear(expected=saved)
    assert cleared._payload=={'future':3}
    before=path.read_bytes();store.clear(expected=cleared);assert path.read_bytes()==before
    with pytest.raises(EncyclopediaHold,match='saved reader changed'):admission.validate()


def test_stage_capability_retains_lost_release_and_refuses_dev_entry(tmp_path):
    from suite_core.installation import ComponentId
    from suite_core.discovery.component_staging import ComponentStageAdmission
    owner=OwnedEncyclopedia()
    with pytest.raises(EncyclopediaHold,match='original portable'):
        with owner.component_stage_hold(ComponentId('collectigen.tag-encyclopedia')):pass
    root=tmp_path/'root';root.mkdir();data=root/'Data';data.mkdir()
    class Session:
        state='ready';helper_outstanding=None
        def request_component_stage(self,operation,scope_id=None):
            if operation=='release':
                self.state='held';raise OSError('lost original ACK')
            return {'scope_id':'a'*32,'owner_generation':'b'*32,'code_root':str(root),'data_root':str(data)}
    owner=OwnedEncyclopedia(normal_entry_session=Session())
    with pytest.raises(OSError,match='original ACK'):
        with ComponentStageAdmission(owner.component_stage_hold).transaction(ComponentId('collectigen.tag-encyclopedia')) as binding:
            assert owner.component_stage_pending and owner.close_held
            binding[3]()
    assert owner.close_held and owner.component_stage_pending


@pytest.mark.parametrize("visible,stopped,accepted", [(False,False,False),(True,False,True),(True,True,False)])
def test_health_requires_original_visible_normal_exit_and_drain(tmp_path,monkeypatch,visible,stopped,accepted):
    monkeypatch.setattr('core.encyclopedia_connection._visible_reader_window',lambda pid:visible)
    selected=package(tmp_path/'package');native=Native();owner=OwnedEncyclopedia(factory=lambda:native)
    owner.launch(selected);owner.poll()
    assert not owner.native_health(selected)
    if stopped:owner.stop_failed_start()
    native.code=0;native.members=0;native.drained.clear()
    assert not owner.native_health(selected)
    native.drained.set();assert owner.native_health(selected) is accepted
    selected.program.write_bytes(b'changed after drain')
    if accepted:
        with pytest.raises(ValueError):owner.native_health(selected)


def stage_selection(root, maturity="alpha"):
    from dataclasses import replace
    package(root)
    path=root/"BOOTSTRAP_INVENTORY.json";value=json.loads(path.read_bytes())
    value["software_version"]="0.1.0";path.write_text(json.dumps(value),encoding="utf-8")
    legacy=review_directory(root)
    return replace(legacy,release_schema=2,maturity=maturity,release_sha256="c"*64)


@pytest.mark.parametrize("maturity",["alpha","beta","stable"])
def test_stage_selection_saved_readback_and_reopened_identity(tmp_path,maturity):
    selected=stage_selection(tmp_path/"package",maturity);path=tmp_path/"settings.json"
    path.write_text(json.dumps({"future":{"keep":[1,2]}}),encoding="utf-8")
    store=ReaderConnectionStore(path);saved=store.save(selected,expected=store.observe())
    assert store.selected(saved)==selected
    assert ReaderConnectionStore(path).selected(ReaderConnectionStore(path).observe())==selected
    assert saved._payload["future"]=={"keep":[1,2]}
    assert saved._payload[KEY]=={"schema_version":2,"release_schema":2,"directory":str(selected.directory),
        "inventory_sha256":selected.inventory_sha256,"version":"0.1.0","maturity":maturity,"release_sha256":"c"*64}
    assert review_directory(selected.directory,expected=selected)==selected
    # The same inventory, without explicit expectation, does not establish stage.
    assert review_directory(selected.directory).release_schema==1


def test_stage_selection_maturity_is_part_of_equality_and_stale_guard(tmp_path):
    from dataclasses import replace
    selected=stage_selection(tmp_path/"package");store=ReaderConnectionStore(tmp_path/"settings.json")
    saved=store.save(selected,expected=store.observe());promoted=replace(selected,maturity="beta",release_sha256="d"*64)
    assert promoted!=selected
    next_saved=store.save(promoted,expected=saved)
    assert store.selected(next_saved)==promoted
    with pytest.raises(EncyclopediaHold,match="Settings changed"):
        store.save(selected,expected=saved)
    assert store.selected(store.observe())==promoted


@pytest.mark.parametrize("field,value",[("schema_version",True),("schema_version",3),("release_schema",True),
    ("release_schema",1),("maturity",None),("maturity","Alpha"),("maturity",1),
    ("version","0.1.0-alpha.1"),("version","01.1.0"),("inventory_sha256","A"*64),
    ("inventory_sha256",None),("release_sha256","C"*64),("release_sha256",""),
    ("directory","relative/package"),("directory",None)])
def test_saved_stage_selection_rejects_malformed_identity(tmp_path,field,value):
    selected=stage_selection(tmp_path/"package");row=selected.mapping();row[field]=value
    path=tmp_path/"settings.json";path.write_text(json.dumps({KEY:row}),encoding="utf-8")
    store=ReaderConnectionStore(path);before=path.read_bytes()
    with pytest.raises(ValueError):store.selected(store.observe())
    assert path.read_bytes()==before


def test_saved_stage_selection_exact_fields_and_no_silent_unknown_pruning(tmp_path):
    selected=stage_selection(tmp_path/"package");path=tmp_path/"settings.json";row=selected.mapping()
    for key in row:
        invalid=dict(row);invalid.pop(key);path.write_text(json.dumps({KEY:invalid}),encoding="utf-8")
        store=ReaderConnectionStore(path)
        with pytest.raises(ValueError):store.selected(store.observe())
    invalid=dict(row,future_correction={"keep":7});path.write_text(json.dumps({KEY:invalid}),encoding="utf-8")
    store=ReaderConnectionStore(path);before=path.read_bytes()
    with pytest.raises(ValueError):store.save(selected,expected=store.observe())
    assert path.read_bytes()==before


def test_legacy_unknown_selection_fields_hold_format_promotion(tmp_path):
    selected=stage_selection(tmp_path/"package");legacy=review_directory(selected.directory)
    path=tmp_path/"settings.json";path.write_text(json.dumps({KEY:{**legacy.mapping(),"future_correction":7}}),encoding="utf-8")
    store=ReaderConnectionStore(path);before=path.read_bytes()
    with pytest.raises(EncyclopediaHold,match="Unknown saved reader fields"):
        store.save(selected,expected=store.observe())
    assert path.read_bytes()==before


def test_explicit_stage_to_legacy_selection_retires_only_known_identity_fields(tmp_path):
    selected=stage_selection(tmp_path/"package");path=tmp_path/"settings.json"
    path.write_text(json.dumps({"future":{"keep":7}}),encoding="utf-8")
    store=ReaderConnectionStore(path);saved=store.save(selected,expected=store.observe())
    legacy=review_directory(selected.directory);restored=store.save(legacy,expected=saved)
    assert restored._payload[KEY]==legacy.mapping() and restored._payload["future"]=={"keep":7}
    assert store.selected(restored)==legacy
    assert store.clear(expected=restored)._payload.get(KEY) is None


def test_stage_helper_context_cannot_silently_drop_signed_stage_identity(tmp_path):
    from core.encyclopedia_connection import RootReaderSelection
    root=tmp_path/"root";selected=stage_selection(root/"package");path=root/"Data/settings.json"
    path.parent.mkdir(parents=True);store=ReaderConnectionStore(path);store.save(selected,expected=store.observe())
    selection=RootReaderSelection(root,path,selected)
    with pytest.raises(EncyclopediaHold,match="awaits original-host integration"):
        selection.context()


@pytest.mark.parametrize("changes",[{"release_schema":True},{"release_schema":3},{"release_schema":1},
    {"directory":Path("relative")},{"directory":Path("C:/one/../two")},
    {"maturity":"a"},{"release_sha256":None},{"inventory_sha256":"bad"}])
def test_stage_selection_constructor_refuses_partial_or_aliased_identity(tmp_path,changes):
    from dataclasses import replace
    selected=stage_selection(tmp_path/"package")
    with pytest.raises(ValueError):replace(selected,**changes)


def test_stage_selection_inventory_drift_cannot_save_or_update_expected_identity(tmp_path):
    selected=stage_selection(tmp_path/"package");path=tmp_path/"settings.json";store=ReaderConnectionStore(path)
    saved=store.save(selected,expected=store.observe());before=path.read_bytes()
    inventory=selected.directory/"BOOTSTRAP_INVENTORY.json";value=json.loads(inventory.read_bytes())
    value["software_version"]="0.2.0";inventory.write_text(json.dumps(value),encoding="utf-8")
    with pytest.raises(ValueError):store.save(selected,expected=saved)
    assert path.read_bytes()==before


@pytest.mark.parametrize("bad",[[],[1],"unknown",{"schema_version":3}])
def test_stage_save_refuses_malformed_previous_selection_without_writes(tmp_path,bad):
    selected=stage_selection(tmp_path/"package");path=tmp_path/"settings.json"
    path.write_text(json.dumps({KEY:bad}),encoding="utf-8");before=path.read_bytes();store=ReaderConnectionStore(path)
    # Empty lists are malformed too, even though Python treats them as false.
    with pytest.raises(ValueError):store.save(selected,expected=store.observe())
    assert path.read_bytes()==before
