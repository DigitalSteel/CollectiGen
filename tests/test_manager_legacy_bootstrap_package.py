"""Inert package mapping and pin checks; crypto verifier is explicitly doubled."""
from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace
import json,zipfile
import pytest
from release_contract import ReleaseArtifact
from tools.build_manager_distribution import render_legacy_reader_config, legacy_reader_files, BuildHold
from core.encyclopedia_legacy_host import CompiledLegacyReaderProfile
from suite_core.manager.compiled_encyclopedia_config import PROFILE as READER_PROFILE


@pytest.fixture
def package(tmp_path,monkeypatch):
    files={'TagEncyclopedia.exe':b'INERT ROOT, NEVER EXECUTE','_internal/runtime.dll':b'INERT LIBRARY',
           'README.md':b'Original Root instructions','LICENSE':b'Original Root license'}
    inventory=json.dumps(dict(software_version='0.2.2-alpha.1',platform={'os':'windows','architecture':'x86_64'},
        files=[dict(path=n,size_bytes=len(raw),sha256=sha256(raw).hexdigest()) for n,raw in files.items()])).encode()
    output=BytesIO()
    with zipfile.ZipFile(output,'w') as bundle:
        bundle.writestr('BOOTSTRAP_INVENTORY.json',inventory)
        for name,raw in files.items():bundle.writestr(name,raw)
    archive=tmp_path/'inert-root.zip';archive.write_bytes(output.getvalue())
    artifact=ReleaseArtifact(archive.name,'portable-windows-x64',sha256(archive.read_bytes()).hexdigest(),
        archive.stat().st_size,sha256(inventory).hexdigest())
    monkeypatch.setattr('release_contract.verify_release_manifest',lambda *args,**kwargs:
        SimpleNamespace(version='0.2.2-alpha.1',artifacts=(artifact,)))
    trust=READER_PROFILE.trust_bytes
    value=dict(schema=1,trust_hex=trust.hex(),trust_sha256=sha256(trust).hexdigest(),
        manifest_hex=b'UNSIGNED INERT MANIFEST'.hex(),signature_hex=b'UNSIGNED INERT SIGNATURE'.hex(),
        archive_sha256=artifact.sha256,inventory_sha256=sha256(inventory).hexdigest())
    return archive,value,files


def test_original_launcher_layout_and_docs_are_preserved_without_collision(package,tmp_path):
    archive,value,original=package
    mapped,receipt=legacy_reader_files(render_legacy_reader_config(value).encode(),archive)
    root=tmp_path/'suite';root.mkdir()
    (root/'README.md').write_bytes(b'Manager instructions')
    (root/'LICENSE').write_bytes(b'Manager license')
    for name,raw in mapped.items():
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    profile=CompiledLegacyReaderProfile(READER_PROFILE.trust_bytes,READER_PROFILE.trust_sha256,value['inventory_sha256'])
    assert profile.verify_bootstrap(root)==sha256(original['TagEncyclopedia.exe']).hexdigest()
    assert (root/'README.md').read_bytes()==b'Manager instructions'
    assert (root/'_legacy-reader/README.md').read_bytes()==original['README.md']
    assert receipt['apps_launched'] is False and receipt['detached_release_verified'] is True
    (root/'_internal/unlisted.dll').write_bytes(b'not admitted')
    with pytest.raises(ValueError,match='runtime_members_changed'):profile.verify_bootstrap(root)


def test_active_profile_requires_exact_archive(package):
    archive,value,_=package
    with pytest.raises(BuildHold,match='archive_required'):
        legacy_reader_files(render_legacy_reader_config(value).encode(),None)
    archive.write_bytes(archive.read_bytes()+b'changed')
    with pytest.raises(Exception):legacy_reader_files(render_legacy_reader_config(value).encode(),archive)


def test_profile_is_fixed_data_and_cannot_append_an_instruction(package):
    archive,value,_=package
    with pytest.raises(BuildHold,match='canonical_legacy'):
        legacy_reader_files(render_legacy_reader_config(value).encode()+b'print("extra execution")\n',archive)
    value['program']='C:/arbitrary.exe'
    with pytest.raises(BuildHold,match='closed_legacy'):render_legacy_reader_config(value)
