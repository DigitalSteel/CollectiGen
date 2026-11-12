"""Fresh filesystem admission with bounded ancestor work; synthetic paths only."""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import pytest
from core.profile_paths import unredirected_directory
from core.dependencies import DependencyService


def test_existing_canonical_directory_checks_each_ancestor_once(tmp_path, monkeypatch):
    target=tmp_path/'one'/'two';target.mkdir(parents=True)
    original=Path.lstat;seen=[]
    def observe(path,*a,**kw):
        seen.append(path);return original(path,*a,**kw)
    monkeypatch.setattr(Path,'lstat',observe)
    assert unredirected_directory(target,must_exist=True)==target
    assert len(seen)==len(set(seen)),seen
    assert target in seen and target.anchor


@pytest.mark.parametrize('raw_root',[False,True])
def test_original_dot_dot_reparse_is_still_refused(tmp_path,monkeypatch,raw_root):
    allowed=tmp_path/'allowed';allowed.mkdir();file=allowed/'a.bin';file.write_bytes(b'owned')
    redirected=tmp_path/'redirected';redirected.mkdir()
    original=Path.lstat
    def stat(path,*a,**kw):
        info=original(path,*a,**kw)
        if path==redirected:return SimpleNamespace(st_mode=info.st_mode,st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path,'lstat',stat)
    raw=redirected/'..'/'allowed'
    with pytest.raises(RuntimeError):
        if raw_root:DependencyService._safe_component_file(file,raw)
        else:unredirected_directory(raw,must_exist=True)


def test_canonical_file_parent_covers_root_and_hashes_remain_fresh(tmp_path,monkeypatch):
    site=tmp_path/'site';parent=site/'nested';parent.mkdir(parents=True)
    file=parent/'a.bin';file.write_bytes(b'first')
    original=DependencyService._safe_component_directory;seen=[]
    def observe(path):seen.append(path);return original(path)
    monkeypatch.setattr(DependencyService,'_safe_component_directory',staticmethod(observe))
    assert DependencyService._safe_component_file(file,site)==file
    assert seen==[parent]
    first=DependencyService._verified_component_files(site)
    file.write_bytes(b'changed')
    second=DependencyService._verified_component_files(site)
    assert first['nested/a.bin']['sha256']==hashlib.sha256(b'first').hexdigest()
    assert second['nested/a.bin']['sha256']==hashlib.sha256(b'changed').hexdigest()


def test_outside_root_and_non_directory_still_refuse(tmp_path):
    root=tmp_path/'root';root.mkdir();outside=tmp_path/'outside.bin';outside.write_bytes(b'owned')
    with pytest.raises(RuntimeError,match='escapes'):
        DependencyService._safe_component_file(outside,root)
    with pytest.raises(RuntimeError,match='non-directory'):
        unredirected_directory(outside,must_exist=True)
    with pytest.raises(RuntimeError,match='does not exist'):
        unredirected_directory(root/'missing',must_exist=True)
