"""Prepare canonical stock freezer inputs only; never freeze or launch products.

The retained private stage contributes pinned SDK/notices/spec policy and the
previous analysis-bound source names, never its application source bytes.
"""
import argparse,ast,hashlib,json,re,shutil
from pathlib import Path

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def pinned_bytes(path,expected,reason):
    raw=path.read_bytes()
    if not re.fullmatch('[0-9a-f]{64}',expected) or hashlib.sha256(raw).hexdigest()!=expected:
        raise ValueError(reason)
    return raw

def stock_path(baseline,name):
    relative=Path(name)
    if relative.is_absolute() or '..' in relative.parts or ':' in name or '\\' in name:
        raise ValueError('exact_relative_stock_metadata_required')
    path=baseline/relative
    if not path.is_file() or path.resolve()!=path.absolute() or any(p.is_symlink() or p.is_junction() for p in [path,*path.parents]):
        raise ValueError('unredirected_stock_metadata_required')
    return path

def root_entry(package_pin=None, *, compiled_update_profile=False, compiled_profile_input=None,
               qualification_test_build=False, qualification_mapping_sha256=None):
    if package_pin is not None and not re.fullmatch('[0-9a-f]{64}',package_pin):
        raise ValueError('exact_manager_package_digest_required')
    if compiled_update_profile and compiled_profile_input is None:
        raise ValueError('exact_compiled_root_profile_input_required')
    if compiled_update_profile and package_pin is None:
        raise ValueError('compiled_root_package_pin_required')
    if not compiled_update_profile and compiled_profile_input is not None:
        raise ValueError('explicit_compiled_root_profile_binding_required')
    if qualification_test_build and (not compiled_update_profile or qualification_mapping_sha256 is None):
        raise ValueError('sealed_test_root_profile_required')
    if not qualification_test_build and qualification_mapping_sha256 is not None:
        raise ValueError('explicit_test_build_only_mapping_required')
    require_profile=''
    root_profile=(render_qualification_update_config(compiled_profile_input,include_prior=True,
        mapping_sha256=qualification_mapping_sha256) if qualification_test_build else
        render_compiled_update_config(compiled_profile_input,include_prior=True) if compiled_update_profile else '')
    profile_option=", compiled_update_profile=PROFILE" if compiled_update_profile else ""
    return (root_profile+"EXPECTED_MANAGER_PACKAGE_SHA256 = "+repr(package_pin)+"\n"
        "if __name__ == '__main__':\n"
        "    if EXPECTED_MANAGER_PACKAGE_SHA256 is None:\n"
        "        raise RuntimeError('manager_package_pin_not_finalized')\n"
        "    from suite_core.manager.launcher_main import main\n"+require_profile+
        "    raise SystemExit(main(expected_package_sha256=EXPECTED_MANAGER_PACKAGE_SHA256"+profile_option+"))\n")

def render_compiled_update_config(value, *, include_prior=False):
    """Render only reviewed public JSON data into the fixed trusted template."""
    required={'schema','trust_hex','trust_sha256','channel','transport_id','initial_prior',
        'updater_sha256','stock_review_hex','stock_review_sha256'}
    if type(value) is not dict or set(value)!=required or value['schema']!=1:
        raise ValueError('exact_compiled_update_input_schema_required')
    for data,pin in (('trust_hex','trust_sha256'),('stock_review_hex','stock_review_sha256')):
        if type(value[data]) is not str or len(value[data])>2*1024*1024:
            raise ValueError('compiled_public_input_bounds')
        raw=bytes.fromhex(value[data])
        if hashlib.sha256(raw).hexdigest()!=value[pin]:raise ValueError('compiled_public_input_pin_changed')
    if (value['channel'] not in {'stable','beta'} or value['transport_id']!='github-release-assets-https'
            or not re.fullmatch('[0-9a-f]{64}',value['updater_sha256'])
            or (value['initial_prior'] is None and include_prior)
            or value['initial_prior'] is not None and type(value['initial_prior']) is not dict):
        raise ValueError('reviewed_stock_update_profile_required')
    prior=value['initial_prior']
    if prior is not None and (set(prior)!={'version','channel','platform','files'} or set(prior['files'])!={'archive','manifest','signature','trust','proof','inventory','executable'}):
        raise ValueError('exact_compiled_prior_release_required')
    for spec in prior['files'].values() if prior is not None else ():
        if type(spec) is not dict or set(spec)!={'path','size','sha256'}:raise ValueError('exact_compiled_prior_pin_required')
        path=Path(spec['path'])
        if (not path.parts or path.is_absolute() or '..' in path.parts or ':' in spec['path'] or path.parts[0]!='Recovery'
                or type(spec['size']) is not int or spec['size']<=0 or not re.fullmatch('[0-9a-f]{64}',spec['sha256'])):
            raise ValueError('compiled_prior_recovery_scope_required')
    # No CDN origins are qualified by this schema; stock redirects remain held.
    return ('# Reviewed build-bound public inputs; no runtime enrollment.\n'
        'from core.component_release_transport import ComponentReleaseTransport, ReleaseTransportPolicy\n'
        'from core.manager_update_host import CompiledManagerUpdateProfile, QualifiedStockReview\n'
        'PROFILE = CompiledManagerUpdateProfile(\n'
        '    trust_bytes=bytes.fromhex('+repr(value['trust_hex'])+'),\n'
        '    trust_sha256='+repr(value['trust_sha256'])+',\n'
        '    channel='+repr(value['channel'])+', transport_id='+repr(value['transport_id'])+', opener=ComponentReleaseTransport(ReleaseTransportPolicy()),\n'
        '    initial_prior='+repr(prior if include_prior else None)+',\n'
        '    updater_sha256='+repr(value['updater_sha256'])+',\n'
        '    native_review=QualifiedStockReview(bytes.fromhex('+repr(value['stock_review_hex'])+'), '+repr(value['stock_review_sha256'])+'))\n')

def render_qualification_update_config(value, *, include_prior=False, mapping_sha256=None):
    """Explicit isolated test build; production renderer remains HTTPS-only."""
    if type(value) is not dict or value.get('transport_id')!='sealed-local-qualification':
        raise ValueError('explicit_sealed_local_test_profile_required')
    if ((include_prior and mapping_sha256 is None) or
            mapping_sha256 is not None and not re.fullmatch('[0-9a-f]{64}',mapping_sha256)):
        raise ValueError('sealed_qualification_mapping_pin_required')
    validated=dict(value,transport_id='github-release-assets-https')
    template=render_compiled_update_config(validated,include_prior=include_prior)
    return template.replace('from core.component_release_transport import ComponentReleaseTransport, ReleaseTransportPolicy\n','').replace(
        'CompiledManagerUpdateProfile, QualifiedStockReview\n',
        'CompiledManagerUpdateProfile, QualifiedStockReview, SealedLocalQualificationOpener\n').replace(
        "transport_id='github-release-assets-https', opener=ComponentReleaseTransport(ReleaseTransportPolicy()),",
        "transport_id='sealed-local-qualification', opener=SealedLocalQualificationOpener("+repr(mapping_sha256)+"),")

def safe_source(workspace,name):
    relative=Path(name)
    if relative.is_absolute() or '..' in relative.parts or ':' in name or '\\' in name:
        raise ValueError('exact_relative_source_required')
    if any(part.startswith('private_') for part in relative.parts):
        raise ValueError('private_QA_source_excluded')
    if not (name.startswith(('core/','suite_core/','qt_app/','licenses/')) or
            name in {'LICENSE','LICENSES.md','THIRD_PARTY_NOTICES.md','release_contract.py','runtime_contract.py','version.py'}):
        raise ValueError('outside_stock_application_source_scope')
    target=workspace/relative
    if not target.is_file() or target.resolve()!=target.absolute() or any(p.is_symlink() or p.is_junction() for p in [target,*target.parents]):
        raise ValueError('unredirected_canonical_source_required')
    return target

def prepare_public_stage(source_root,output,*,public_inventory_sha256,
                         frozen_sources,frozen_sources_sha256,manifest_sha256):
    """Stage a reviewed exported subset; no historical stage or SDK enrollment.

    frozen_sources is a Path to pinned JSON with schema=1 and source_rows.
    Rows use the existing path/bytes/sha256 representation, without origins.
    The caller owns native recipes and external tool qualification separately.
    """
    source_root=Path(source_root).absolute();output=Path(output).absolute()
    if (source_root.resolve()!=source_root or output.resolve()!=output or
            output.exists() or output.is_relative_to(source_root) or
            any(p.is_symlink() or p.is_junction() for p in [source_root,*source_root.parents,*output.parents])):
        raise ValueError('new_unredirected_public_stage_required')
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('duplicate_public_stage_input_key')
            result[key]=value
        return result
    def document(raw):
        return json.loads(raw,object_pairs_hook=pairs)
    inventory_path=stock_path(source_root,'public-source.json')
    inventory=document(pinned_bytes(inventory_path,public_inventory_sha256,'public_inventory_pin_changed'))
    if (type(inventory) is not dict or type(inventory.get('schema_version')) is not int or inventory.get('schema_version')!=1 or
            inventory.get('manifest_excludes_self') is not True or type(inventory.get('files')) is not dict):
        raise ValueError('accepted_public_inventory_required')
    subset_path=Path(frozen_sources).absolute()
    if (not subset_path.is_file() or subset_path.resolve()!=subset_path or
            any(p.is_symlink() or p.is_junction() for p in [subset_path,*subset_path.parents])):
        raise ValueError('unredirected_frozen_subset_required')
    subset=document(pinned_bytes(subset_path,frozen_sources_sha256,'frozen_subset_pin_changed'))
    if (type(subset) is not dict or set(subset)!={'schema','source_rows'} or
            type(subset['schema']) is not int or subset['schema']!=1 or
            type(subset['source_rows']) is not list or not subset['source_rows']):
        raise ValueError('exact_frozen_subset_required')
    required={'core/manager_launch_owner.py','suite_core/manager/bootstrap_entry.py',
        'suite_core/manager/entry_binding.py','suite_core/manager/frozen_entry.py','suite_core/manager/launcher_main.py'}
    manifest_name='suite_core/manager/MODULE_MANIFEST.json'
    manifest_path=safe_source(source_root,manifest_name)
    manifest_raw=pinned_bytes(manifest_path,manifest_sha256,'public_runtime_manifest_pin_changed')
    if inventory['files'].get(manifest_name)!={'size_bytes':len(manifest_raw),'sha256':manifest_sha256}:
        raise ValueError('runtime_manifest_not_in_public_inventory')
    manifest=document(manifest_raw)
    if (type(manifest) is not dict or type(manifest.get('runtime_files')) is not list or
            any(type(n) is not str for n in manifest['runtime_files']) or
            type(manifest.get('software_version')) is not str or not manifest['software_version']):
        raise ValueError('public_runtime_manifest_required')
    declared=set(manifest['runtime_files'])
    sources={};aliases=set()
    for row in subset['source_rows']:
        if (type(row) is not dict or set(row)!={'path','bytes','sha256'} or type(row['path']) is not str or
                type(row['bytes']) is not int or row['bytes']<0 or type(row['sha256']) is not str or
                re.fullmatch('[0-9a-f]{64}',row['sha256']) is None):
            raise ValueError('exact_public_source_row_required')
        name=row['path']
        if Path(name).as_posix()!=name or not name or name.endswith('/'):
            raise ValueError('exact_relative_source_required')
        if name.casefold() in aliases:raise ValueError('duplicate_frozen_source_path')
        aliases.add(name.casefold())
        origin=safe_source(source_root,name)
        if (inventory['files'].get(name)!={'size_bytes':row['bytes'],'sha256':row['sha256']} or
                name.endswith('.py') and name not in declared):
            raise ValueError('frozen_subset_not_declared_public_source')
        raw=pinned_bytes(origin,row['sha256'],'reviewed_public_source_changed')
        if len(raw)!=row['bytes']:raise ValueError('reviewed_public_source_size_changed')
        if name.endswith('.py'):
            tree=ast.parse(raw.decode('utf-8-sig'))
            imported=[]
            for node in ast.walk(tree):
                if isinstance(node,ast.Import):imported.extend(a.name for a in node.names)
                elif isinstance(node,ast.ImportFrom) and node.module:imported.append(node.module)
            if any(n.startswith('private_') or n.startswith('PySide6.QtTest') for n in imported):
                raise ValueError('private_QA_import_in_public_source:'+name)
        sources[name]=(origin,raw)
    if not required<=sources.keys():raise ValueError('canonical_public_entries_required')
    # All participant bytes are admitted before creating any output.
    output.parent.mkdir(parents=True,exist_ok=True)
    if any(p.is_symlink() or p.is_junction() for p in output.parents):
        raise ValueError('new_unredirected_public_stage_required')
    output.mkdir();stage=output/'stage';(stage/'source').mkdir(parents=True)
    rows=[]
    for name,(origin,raw) in sorted(sources.items()):
        expected=hashlib.sha256(raw).hexdigest()
        if digest(origin)!=expected:raise ValueError('public_source_changed_during_copy')
        target=stage/'source'/name;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(origin,target)
        if digest(origin)!=expected or digest(target)!=expected:
            raise ValueError('public_source_changed_during_copy')
        rows.append({'path':name,'bytes':len(raw),'sha256':expected,'origin':str(origin)})
    # Recheck early participants after the final copy; a changing checkout cannot
    # be presented as the accepted immutable snapshot.
    for name,(origin,raw) in sources.items():
        expected=hashlib.sha256(raw).hexdigest()
        if digest(origin)!=expected or digest(stage/'source'/name)!=expected:
            raise ValueError('public_source_changed_during_copy')
    pinned_bytes(inventory_path,public_inventory_sha256,'public_inventory_pin_changed')
    pinned_bytes(subset_path,frozen_sources_sha256,'frozen_subset_pin_changed')
    pinned_bytes(manifest_path,manifest_sha256,'public_runtime_manifest_pin_changed')
    wrapper=stage/'source/public_root_entry.py';wrapper.write_bytes(root_entry().encode('utf-8'))
    rows.append({'path':'public_root_entry.py','bytes':wrapper.stat().st_size,'sha256':digest(wrapper),
        'origin':'generated canonical root wrapper; package pin intentionally unbound'})
    config={'schema':1,'software_version':manifest['software_version'],'source_rows':rows,
        'public_inventory_sha256':public_inventory_sha256,'frozen_sources_sha256':frozen_sources_sha256,
        'manifest_sha256':manifest_sha256,'manager_entry':'suite_core/manager/frozen_entry.py',
        'root_entry':'public_root_entry.py','root_package_pin_bound':False}
    (stage/'STAGE.json').write_text(json.dumps(config,indent=2)+'\n',encoding='utf-8')
    proof=dict(config,status='public_source_stage_only_not_frozen',freeze_or_product_execution=False,
        historical_stage_required=False,external_tools_or_sdk_enrolled=False)
    (output/'PRODUCTION_STAGE.json').write_text(json.dumps(proof,indent=2)+'\n',encoding='utf-8')
    return stage

def prepare(workspace,baseline,output,*,allowlist_sha256,stage_sha256,manifest_sha256,metadata_pins,metadata_pins_sha256):
    workspace=workspace.resolve();baseline=baseline.resolve();output=output.absolute()
    if output.exists() or not output.is_relative_to(workspace/'.tmp'):
        raise ValueError('new_owned_source_stage_required')
    if any(p.is_symlink() or p.is_junction() for p in output.parents):
        raise ValueError('unredirected_output_parent_required')
    public=workspace/'tools/public-source-manifest.json'
    manifest_path=workspace/'suite_core/manager/MODULE_MANIFEST.json'
    approved=set(json.loads(pinned_bytes(public,allowlist_sha256,'pinned_source_or_stock_stage_changed'))['files'])
    manifest=json.loads(pinned_bytes(manifest_path,manifest_sha256,'pinned_source_or_stock_stage_changed'));declared=set(manifest['runtime_files'])
    if any(any(p.startswith('private_') for p in Path(n).parts) for n in declared):
        raise ValueError('private_QA_manifest_declaration')
    if not declared<=approved:raise ValueError('runtime_manifest_has_nonpublic_sources')
    config=json.loads(pinned_bytes(baseline/'STAGE.json',stage_sha256,'pinned_source_or_stock_stage_changed'))
    if 'software_version' in manifest:
        config['software_version']=manifest['software_version']
    pins=json.loads(pinned_bytes(Path(metadata_pins),metadata_pins_sha256,'approved_metadata_pins_changed'))
    expected_metadata={r['path'] for r in config['notice_rows']}|{'sdk_binary_origin.py','windows_build_isolation.py','CollectiGenManager.spec','CollectiGen.spec'}
    if (set(pins)!={'schema','stage_sha256','files'} or pins['schema']!=1 or pins['stage_sha256']!=stage_sha256 or
            type(pins['files']) is not dict or set(pins['files'])!=expected_metadata):
        raise ValueError('exact_approved_stock_metadata_scope_required')
    # Admit the exact reviewed bytes before any output parent or file is created.
    metadata_bytes={name:pinned_bytes(stock_path(baseline,name),pin,'approved_stock_metadata_changed') for name,pin in pins['files'].items()}
    sdk_bytes={row['path']:pinned_bytes(stock_path(baseline,'sdk/'+row['path']),row['sha256'],'retained_stock_sdk_changed') for row in config['sdk_rows']}
    names={r['path'] for r in config['source_rows'] if not r['path'].startswith(('private_','public_'))}
    # Actual canonical entry analysis requires this public knowledge-pack model;
    # the historical private wrapper's narrower closure did not declare it.
    names.add('core/default_catalog_release.py')
    # Native Foundation review now captures the staged effective snapshot. These
    # shared owners are absent from the historical analysis-bound source list.
    names.update({'core/foundation_dependencies.py','core/shared_tag_snapshot.py',
        'core/effective_tag_snapshot_reader.py'})
    # core.paths imports this shared binding at module initialization. It is
    # required Manager infrastructure, not the excluded full application UI.
    names.add('core/application_profile.py')
    # Ordinary Databases connection reuses native profile/admission/settings.
    names.add('core/tag_catalog_connection.py')
    # Registered Encyclopedia connection/acquisition owners must enter ordinary
    # Manager freezes even when the historical stock source list predates them.
    names.update({'core/encyclopedia_connection.py','core/encyclopedia_installation.py',
        'core/component_release_transport.py','suite_core/manager/encyclopedia_connection_ui.py',
        'suite_core/manager/encyclopedia_acquisition_ui.py',
        'suite_core/manager/compiled_encyclopedia_config.py',
        'suite_core/manager/encyclopedia_acquisition_binding.py'})
    names.add('core/component_release_identity.py')
    names.update({'suite_core/manager/RELEASE_PRESENTATION.json',
        'suite_core/encyclopedia/RELEASE_PRESENTATION.json',
        'suite_core/media_organizer/RELEASE_PRESENTATION.json'})
    names.add('suite_core/manager/update_review_ui.py')
    names.add('suite_core/manager/compiled_update_config.py')
    names.update({'suite_core/manager/tag_database_controller.py','suite_core/manager/tag_database_ui.py',
        'suite_core/manager/tag_entry_recovery.py',
        'suite_core/manager/model_chat_binding.py','suite_core/manager/model_chat_controller.py','suite_core/manager/model_chat_ui.py',
        'core/manager_llamacpp_provider.py','core/manager_direct_admission.py','suite_core/manager/direct_model_ui.py',
        'core/model_resource_registry.py','core/training_source_io.py',
        'suite_core/encyclopedia/browser.py','suite_core/encyclopedia/hosted_examples.py',
        'suite_core/knowledge/tag_transactions.py','suite_core/knowledge/native_tag_catalog_host.py',
        'suite_core/media_organizer/__init__.py','suite_core/media_organizer/version.py'})
    names.update({'core/manager_stock_hook_pipe.py','core/manager_stock_hook_admission.py',
        'core/manager_stock_hook_provider.py','core/manager_native_member.py','core/manager_successor_admission.py',
        'core/manager_update_host.py'})
    required={'core/manager_launch_owner.py','suite_core/manager/bootstrap_entry.py',
        'suite_core/manager/entry_binding.py','suite_core/manager/frozen_entry.py','suite_core/manager/launcher_main.py'}
    if not required<=names or not names<=approved or any(n.endswith('.py') and n not in declared for n in names):
        raise ValueError('canonical_declared_public_closure_required')
    sources={name:safe_source(workspace,name) for name in names}
    imports=[]
    for name,path in sources.items():
        if not name.endswith('.py'):continue
        tree=ast.parse(path.read_text(encoding='utf-8-sig'))
        imported=[]
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):imported.extend(a.name for a in node.names)
            elif isinstance(node,ast.ImportFrom) and node.module:imported.append(node.module)
        if any(n.startswith('private_') or n.startswith('PySide6.QtTest') for n in imported):
            raise ValueError('private_QA_import_in_canonical_source:'+name)
        if name in required:imports.append({'path':name,'direct_imports':sorted(set(imported))})
    output.parent.mkdir(parents=True,exist_ok=True)
    if any(p.is_symlink() or p.is_junction() for p in output.parents):raise ValueError('unredirected_output_parent_required')
    output.mkdir();stage=output/'stage';(stage/'source').mkdir(parents=True)
    rows=[]
    for name,origin in sorted(sources.items()):
        before=digest(origin);target=stage/'source'/name;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(origin,target)
        if digest(origin)!=before or digest(target)!=before:raise ValueError('canonical_source_changed_during_copy')
        rows.append({'path':name,'bytes':target.stat().st_size,'sha256':before,'origin':str(origin)})
    wrapper=stage/'source/public_root_entry.py';wrapper.write_text(root_entry(),encoding='utf-8')
    rows.append({'path':'public_root_entry.py','bytes':wrapper.stat().st_size,'sha256':digest(wrapper),'origin':'generated canonical root wrapper; package pin intentionally unbound'})
    metadata=[]
    for row in config['sdk_rows']:
        target=stage/'sdk'/row['path'];target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(sdk_bytes[row['path']])
        if digest(target)!=row['sha256']:raise ValueError('stock_sdk_readback_failed')
    for name in [r['path'] for r in config['notice_rows']]+['sdk_binary_origin.py','windows_build_isolation.py']:
        before=pins['files'][name];target=stage/name;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(metadata_bytes[name])
        if digest(target)!=before:raise ValueError('stock_metadata_changed')
        metadata.append({'path':name,'sha256':before})
    for name in ('CollectiGenManager','CollectiGen'):
        text=metadata_bytes[name+'.spec'].decode('utf-8')
        if name=='CollectiGenManager':
            anchor="source/'public_child_entry.py'"
            canonical_anchor="source/'suite_core/manager/frozen_entry.py'"
            if text.count(anchor)==1 and text.count(canonical_anchor)==0:
                text=text.replace(anchor,canonical_anchor)
            elif text.count(anchor)!=0 or text.count(canonical_anchor)!=1:
                raise ValueError('stock_child_recipe_anchor_changed')
        if text.count("'PySide6.QtTest',")>1:raise ValueError('stock_QA_dependency_anchor_changed')
        text=text.replace("'PySide6.QtTest',",'')
        (stage/(name+'.spec')).write_bytes(text.encode('utf-8'))
        metadata.append({'path':name+'.spec','preimage_sha256':pins['files'][name+'.spec'],'postimage_sha256':digest(stage/(name+'.spec'))})
    config['source_rows']=rows
    (stage/'STAGE.json').write_text(json.dumps(config,indent=2)+'\n')
    proof={'status':'canonical_source_stage_only_not_frozen','software_version':config['software_version'],'source_rows':len(rows),'entry_sources':sorted(required),
        'manager_entry':'suite_core/manager/frozen_entry.py','root_entry':'public_root_entry.py',
        'root_package_pin_bound':False,'canonical_source_bytes_only':True,'private_QA_sources_or_dispatch':False,
        'stock_metadata':metadata,'entry_direct_imports':imports,'allowlist_sha256':allowlist_sha256,
        'manifest_sha256':manifest_sha256,'baseline_stage_sha256':stage_sha256,'approved_metadata_pins_sha256':metadata_pins_sha256,'freeze_or_product_execution':False,
        'holds':['Bind root package digest after stock Manager freeze','Fresh production frozen closure/import verification',
            'Actual production default-entry/runtime qualification not claimed','Production owned-helper qualification disabled','Release/update/repair gates unchanged']}
    (output/'PRODUCTION_STAGE.json').write_text(json.dumps(proof,indent=2)+'\n')
    return stage

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ('workspace','baseline','output','metadata-pins'):parser.add_argument('--'+name,type=Path,required=True)
    for name in ('allowlist-sha256','stage-sha256','manifest-sha256','metadata-pins-sha256'):parser.add_argument('--'+name,required=True)
    args=parser.parse_args();print(prepare(**vars(args)))
