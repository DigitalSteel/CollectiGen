"""Component source admits the Manager's exact immutable catalog, never user/default data."""
import json
import pytest
from tools.public_source import load_manifest,PublicSourceError

@pytest.mark.parametrize('component,path,allowed',[
 ('collectigen.suite-manager','data/model_workflow_baseline.json',True),
 ('collectigen.tag-encyclopedia','data/model_workflow_baseline.json',False),
 ('collectigen.suite-manager','data/not-approved.json',False),
 ('collectigen.suite-manager','data/defaults/tags.json',False),
 ('collectigen.suite-manager','data/defaults/release-trust.json',True),
])
def test_component_source_catalog_scope(tmp_path,component,path,allowed):
    manifests={'collectigen.suite-manager':'suite_core/manager/MODULE_MANIFEST.json',
               'collectigen.tag-encyclopedia':'suite_core/encyclopedia/MODULE_MANIFEST.json'}
    source=tmp_path/'manifest.json';source.write_text(json.dumps({'schema_version':1,
        'export_scope':'component-source','component_ids':[component],
        'files':[manifests[component],path],'reviewed_defaults':[]}))
    if allowed:assert path in load_manifest(source)['files']
    else:
        with pytest.raises(PublicSourceError):load_manifest(source)
