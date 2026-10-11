"""Default-free component export uses the stock scanner/inventory, never native catalogs."""
import json
from pathlib import Path
import pytest
from tools import public_source as exporter


def candidate(tmp_path):
    root=tmp_path/'input';root.mkdir()
    file='suite_core/encyclopedia/MODULE_MANIFEST.json'
    path=root/file;path.parent.mkdir(parents=True);path.write_text(json.dumps({'module_id':'collectigen.tag-encyclopedia','software_version':'0.1.0-alpha.8'}))
    manifest=root/'manifest.json';manifest.write_text(json.dumps({'schema_version':1,'export_scope':'component-source','component_ids':['collectigen.tag-encyclopedia'],'files':[file],'reviewed_defaults':[],'required_provenance':[]}))
    return root,manifest


def test_component_cli_has_no_native_default_authority_and_exact_inventory(tmp_path,monkeypatch):
    root,manifest=candidate(tmp_path)
    def denied(*a,**k):raise AssertionError('code export must not inspect native catalog authority')
    monkeypatch.setattr(exporter,'audit_private_distribution_completeness',denied)
    model=exporter.load_manifest(manifest)
    _,_,digest=exporter.prepare_files(model,exporter._tree_reader(root))
    output=tmp_path/'export'
    assert exporter.main(['--repository',str(root),'--manifest',str(manifest),'--working-tree','--output',str(output),'--approve-defaults-digest',digest])==0
    assert exporter.verify_public_inventory(output)==[] and exporter.scan_public_tree(output)==[]
    assert (output/'.gitattributes').read_bytes().endswith(b'* -text\n')
    assert json.loads((output/'public-source.json').read_bytes())['export_scope']=='component-source'
    assert json.loads((output/'data/defaults/public-defaults.json').read_bytes())['files']=={}
    (output/'surprise.txt').write_text('unreviewed')
    assert exporter.verify_public_inventory(output)==['unlisted exported file: surprise.txt']


@pytest.mark.parametrize('change',({'reviewed_defaults':['data/defaults/categories.json']},{'files':['data/defaults/categories.json','suite_core/encyclopedia/MODULE_MANIFEST.json']},{'component_ids':['collectigen.tag-database']},{'component_ids':[{}]},{'component_ids':['collectigen.tag-encyclopedia','collectigen.tag-encyclopedia']},{'required_provenance':['LICENSE']},{'files':['README.md']}))
def test_component_scope_cannot_include_native_defaults_or_ambiguous_identity(tmp_path,change):
    root,manifest=candidate(tmp_path);value=json.loads(manifest.read_bytes());value.update(change);manifest.write_text(json.dumps(value))
    with pytest.raises(exporter.PublicSourceError):exporter.load_manifest(manifest)


def test_component_export_preserves_privacy_scan_and_signature_public_only_rule(tmp_path):
    root,manifest=candidate(tmp_path);model=exporter.load_manifest(manifest)
    prepared,_,_=exporter.prepare_files(model,exporter._tree_reader(root))
    prepared['data/defaults/release-trust.json']=b'{"schema_version":1,"keys":[{"private":"forbidden"}]}'
    with pytest.raises(ValueError):exporter.validate_default_references(prepared,model)
    module='suite_core/encyclopedia/MODULE_MANIFEST.json'
    prepared[module]=b'{"module_id":"collectigen.suite-manager","software_version":"0.1.0-alpha.8"}'
    assert exporter.validate_default_references(prepared,model)==['Component source manifest identity mismatch.']


def test_ordinary_export_keeps_explicit_completeness_hold(tmp_path):
    root,manifest=candidate(tmp_path);output=tmp_path/'export'
    with pytest.raises(exporter.PublicSourceError,match='Distribution knowledge is incomplete'):
        exporter.build_public_source(root,output,manifest,'inert',completeness_check=lambda:['unreviewed native defaults'])
    assert not output.exists()
