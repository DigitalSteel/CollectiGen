"""Fixed generated-package dataset-parser qualification; standard library only.

The installed child supplies public trainer/numeric modules. Only the pinned
synthetic catalog is accepted; this does not qualify selected data or training.
"""
from __future__ import annotations

import base64
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import os
from pathlib import Path, PurePosixPath
import struct
import sys
from types import SimpleNamespace
import zlib

from . import training_image_effects_contract as reference
from . import training_package_effects as policy

canonical,sha=reference.canonical,reference.sha
PROFILE='generated-dataset-package-parser-v1'
CATALOG_SHA256='b4f8e7a7c3bc2d82189ea8e3b2e77bcbabab9f67c7404863549b24bf20d610eb'
PACKAGE_FINGERPRINT='68ffb9f6cad7e5ec6c30cc587395c82d455eef03b52cd167fb4ee535b6c4143c'
MANIFEST='collectigen-training-manifest.json'
SIZES=((1024,1024),(512,768),(777,513),(513,777),(2048,2048),(512,768),(256,256),(256,256))
CONFIG=SimpleNamespace(repeats=3,batch_size=2,target_resolution=1024,
    bucket_policy='aspect_ratio',allow_upscale=False,crop_policy='none')
SCHEMA_REFUSALS=('unknown_root','unknown_subset','constructor_only','resolution_type','batch_type')
MAPPING_REFUSALS=('caption_prefix','random_crop','cache_info','alpha_mask','is_reg',
    'validation_split','missing_repeats','wrong_directory','argparse_default')


def generated_image(index):
    size=SIZES[index]
    raw=reference.pixels(*size).translate(bytes((v+index*17)%256 for v in range(256)))
    if index==5:
        raw=b''.join(raw[offset:offset+3]+bytes(((offset//3)%256,)) for offset in range(0,len(raw),3))
    # Stored DEFLATE blocks avoid zlib-version-dependent compression choices.
    # The checksum algorithms are fixed; every runtime emits identical PNG bytes.
    width,height=size;stride=width*(4 if index==5 else 3)
    scan=b''.join(b'\0'+raw[y*stride:(y+1)*stride] for y in range(height))
    blocks=[]
    for start in range(0,len(scan),65535):
        block=scan[start:start+65535]
        blocks.append(bytes((int(start+len(block)==len(scan)),))+struct.pack('<HH',len(block),len(block)^65535)+block)
    compressed=b'\x78\x01'+b''.join(blocks)+struct.pack('>I',zlib.adler32(scan))
    def chunk(kind,value):
        return struct.pack('>I',len(value))+kind+value+struct.pack('>I',zlib.crc32(kind+value))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,6 if index==5 else 2,0,0,0))+chunk(b'IDAT',compressed)+chunk(b'IEND',b'')


def validate_catalog(value):
    if sha(canonical(value))!=CATALOG_SHA256:
        raise ValueError('dataset_package_catalog_changed')
    return value


def materialize(value,work):
    """Reproduce the exact fixed package, whose full verifier is a source gate."""
    import json
    value=validate_catalog(value)
    entries={entry['path']:base64.b64decode(entry['base64'],validate=True) for entry in value['files']}
    entries.update({entry['path']:generated_image(entry['pattern_index']) for entry in value['images']})
    if len(entries)!=22:
        raise ValueError('dataset_package_inventory_changed')
    manifest=json.loads(entries[MANIFEST])
    if manifest['package_fingerprint']!=PACKAGE_FINGERPRINT:
        raise ValueError('dataset_package_identity_changed')
    expected={item['path']:(item['sha256'],item['size']) for item in manifest['artifacts']}
    if set(expected)!=(set(entries)-{MANIFEST}):
        raise ValueError('dataset_package_inventory_changed')
    for name,raw in entries.items():
        path=PurePosixPath(name)
        if path.is_absolute() or any(part in {'','.','..'} for part in path.parts) or '\\' in name or ':' in name:
            raise ValueError('dataset_package_path_changed')
        if name!=MANIFEST and (sha(raw),len(raw))!=expected[name]:
            raise ValueError('dataset_package_artifact_changed')
    package=work/'generated-dataset-package'
    package.mkdir()
    for name,raw in entries.items():
        path=package/name
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('xb') as handle:handle.write(raw)
    return package,manifest,{name:sha(raw) for name,raw in entries.items()}


def verify_unchanged(package,snapshots):
    expected_dirs={str(parent) for name in snapshots for parent in PurePosixPath(name).parents if str(parent)!='.'}
    files=set(); directories=set()
    for path in package.rglob('*'):
        name=path.relative_to(package).as_posix()
        if path.is_symlink():raise ValueError('dataset_package_link_created')
        if path.is_dir():directories.add(name)
        elif path.is_file():files.add(name)
        else:raise ValueError('dataset_package_artifact_changed')
    if files!=set(snapshots) or directories!=expected_dirs:
        raise ValueError('dataset_package_inventory_changed')
    if any(sha((package/name).read_bytes())!=digest for name,digest in snapshots.items()):
        raise ValueError('dataset_package_artifact_changed')


@contextmanager
def readonly_package(package):
    """Audit defense only: caught Python write attempts still invalidate proof."""
    active=True; denied=[]
    root=os.path.normcase(os.path.abspath(package))
    def inside(value):
        if not isinstance(value,(str,bytes,os.PathLike)):return False
        path=os.path.normcase(os.path.abspath(os.fsdecode(value)))
        return path==root or path.startswith(root+os.sep)
    def audit(event,args):
        if not active:return
        writing=False
        if event=='open':
            mode,flags=args[1:3]
            writing=inside(args[0]) and ((isinstance(mode,str) and any(c in mode for c in 'wax+')) or
                (isinstance(flags,int) and flags&(os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND)))
        elif event in {'os.remove','os.rmdir','os.mkdir','os.chmod','os.utime','os.truncate','os.rename','os.link','os.symlink'}:
            writing=any(inside(value) for value in args[:2])
        if writing:
            denied.append(event)
            raise PermissionError('generated_package_write_denied')
    sys.addaudithook(audit)
    try:
        yield
        if denied:raise ValueError('dataset_package_write_attempted')
    finally:active=False


def expected_blueprint(directory):
    dataset={key:value for key,value in reference.DATASET.items() if key!='is_training_dataset'}
    dataset.update(batch_size=2,debug_dataset=False,skip_image_resolution=None)
    # The native argparse default is an empty string (disabled), whereas the
    # earlier direct-constructor experiment supplied None. Bind the actual
    # parser value explicitly without changing the package's caption policy.
    subset={**reference.SUBSET,'image_dir':str(directory),'num_repeats':3,'keep_tokens_separator':''}
    return {'dataset_group':{'datasets':[{'is_dreambooth':True,'is_controlnet':False,
        'params':dataset,'subsets':[{'params':subset}]}]}}


def validate_blueprint(value,directory):
    if canonical(value)!=canonical(expected_blueprint(directory)):
        raise ValueError('dataset_package_mapping_changed')
    return value


def expected_report():
    return {'schema_version':1,'profile':PROFILE,'catalog_sha256':CATALOG_SHA256,
        'package_fingerprint':PACKAGE_FINGERPRINT,'package_files':22,
        'configuration_cases':2,'argument_preparation':'native_prepare_dataset_args',
        'blueprint_sha256':sha(canonical(expected_blueprint('<generated-training>'))),
        'schema_refusals':list(SCHEMA_REFUSALS),'mapping_refusals':list(MAPPING_REFUSALS),
        'training_images':6,'evaluation_images':2,'repeats':3,'batch_size':2,
        'exposures':18,'batches':10,'partial_batches':2,
        'pixels':'all_generated_RGB_values_and_float32_normalization',
        'pairing':'exact_package_caption_and_image_in_every_batch',
        'crop_conflict_images':2,'upscale_conflict_images':0,'alpha_loss_images':1,
        'package_policy_holds_preserved':True,'package_unchanged':True,
        'tokenizer_scope':'capture_pre_tokenizer_text_only',
        'generated_package_parser_verified':True,'selected_dataset_verified':False,
        'model_compatibility_verified':False,'offline_execution_verified':False,'execution_ready':False}


def validate_report(value):
    if canonical(value)!=canonical(expected_report()):
        raise ValueError('dataset_package_report_changed')
    return value


def probe_generated_package(config_module,entry_module,dataset_module,torch,np,catalog,work):
    """Run load_user_config -> blueprint -> actual batches on a pinned package."""
    torch.set_num_threads(2)
    package,manifest,snapshots=materialize(catalog,work)
    directory=package/'images/training'
    config=policy.dataset_proposal(directory,CONFIG)
    generator=config_module.BlueprintGenerator(config_module.ConfigSanitizer(True,True,False,True))
    def parse(value,args,name):
        path=work/(name+'.json')
        with path.open('xb') as handle:handle.write(canonical(value))
        loaded=config_module.load_user_config(str(path))
        if canonical(loaded)!=canonical(value):raise ValueError('dataset_package_json_changed')
        namespace=entry_module.setup_parser().parse_args(args)
        config_module.accelerator_setup.prepare_dataset_args(namespace,True)
        return generator.generate(loaded,namespace)
    with readonly_package(package):
        blueprint=parse(config,[],'dataset-baseline')
        validate_blueprint(asdict(blueprint),directory)
        contradictory=['--resolution','512,512','--train_batch_size','7','--dataset_repeats','9',
            '--shuffle_caption','--flip_aug','--color_aug','--caption_extension','.caption']
        validate_blueprint(asdict(parse(config,contradictory,'dataset-explicit-precedence')),directory)
        for case in SCHEMA_REFUSALS:
            changed=deepcopy(config); ds=changed['datasets'][0]; subset=ds['subsets'][0]
            if case=='unknown_root':changed['unknown']=True
            elif case=='unknown_subset':subset['unknown']=True
            elif case=='constructor_only':ds['is_training_dataset']=True
            elif case=='resolution_type':ds['resolution']='invalid'
            elif case=='batch_type':ds['batch_size']='invalid'
            try:parse(changed,[],'schema-'+case)
            except config_module.MultipleInvalid:pass
            else:raise ValueError('dataset_package_invalid_schema_accepted')
        for case in MAPPING_REFUSALS:
            changed=deepcopy(config); ds=changed['datasets'][0]; subset=ds['subsets'][0]; args=[]
            if case=='caption_prefix':subset['caption_prefix']='changed'
            elif case in {'random_crop','cache_info','alpha_mask','is_reg'}:subset[case]=True
            elif case=='validation_split':ds['validation_split']=0.25
            elif case=='missing_repeats':subset.pop('num_repeats')
            elif case=='wrong_directory':subset['image_dir']=str(package/'images/holdout')
            elif case=='argparse_default':args=['--caption_suffix','changed']
            changed_blueprint=parse(changed,args,'mapping-'+case)
            try:validate_blueprint(asdict(changed_blueprint),directory)
            except ValueError:pass
            else:raise ValueError('dataset_package_changed_mapping_accepted')
        if dataset_module.LatentsCachingStrategy.get_strategy() is not None:
            raise ValueError('dataset_package_unexpected_cache_strategy')
        group,validation=config_module.generate_dataset_group_by_blueprint(blueprint.dataset_group)
        if validation is not None or len(group.datasets)!=1:
            raise ValueError('dataset_package_role_changed')
        dataset=group.datasets[0]
        dataset.verify_bucket_reso_steps(32)
        class CaptureText:
            def __init__(self):self.captions=[]
            def tokenize(self,caption):
                self.captions.append(caption)
                return [torch.tensor([[1,2]],dtype=torch.int64)]
        dataset.tokenize_strategy=CaptureText()
        training=[item for item in manifest['artifacts'] if item['kind']=='image' and item['role']=='training']
        captions={item['artifact_id']:item for item in manifest['artifacts'] if item['kind']=='caption'}
        expected={}; paths=set(); holds=Counter()
        for item in training:
            index=int(item['stable_id'].split('image-')[1].split('.')[0])
            caption=captions[item['artifact_id']]
            text='café, hero, pose_'+str(index)
            if caption['package_caption']!=text:raise ValueError('dataset_package_caption_changed')
            assessed=policy.assess_image(item,caption,'training',CONFIG)
            holds.update(assessed.holds)
            row=reference.GEOMETRY[index if index<5 else 1]
            _name,size,resized,output,crop,conditioning=row
            path=str(package/item['path']); paths.add(path)
            info=dataset.image_data[path]
            if tuple(info.resized_size)!=resized or tuple(info.bucket_reso)!=output:
                raise ValueError('dataset_package_geometry_changed')
            rgb=reference.pixels(*size).translate(bytes((v+index*17)%256 for v in range(256)))
            pixels=np.frombuffer(rgb,dtype=np.uint8).reshape(size[1],size[0],3)
            pixels=pixels[::2,::2] if index==4 else pixels[crop[1]:size[1]-crop[3],crop[0]:size[0]-crop[2]]
            tensor=torch.from_numpy(pixels.copy()).permute(2,0,1).float().div(255).sub(.5).div(.5)
            expected[text]=(tensor,size,output,conditioning)
        if set(dataset.image_data)!=paths or dataset.num_train_images!=18 or dataset.num_reg_images!=0:
            raise ValueError('dataset_package_role_changed')
        if (holds['crop_conflict'],holds['upscale_conflict'],holds['alpha_loss'])!=(2,0,1):
            raise ValueError('dataset_package_policy_changed')
        exposures=[]; sizes=[]
        for batch_index in range(len(dataset)):
            batch=dataset[batch_index]; texts=batch['captions']; images=batch['images']
            exposures.extend(texts); sizes.append(len(texts))
            if images.device.type!='cpu' or images.dtype!=torch.float32 or images.shape[0]!=len(texts):
                raise ValueError('dataset_package_tensor_changed')
            if batch['latents'] is not None or batch['text_encoder_outputs_list'] is not None or batch['alpha_masks'] is not None or batch['flippeds']!=[False]*len(texts) or batch['loss_weights'].tolist()!=[1.0]*len(texts):
                raise ValueError('dataset_package_unexpected_processing')
            for position,text in enumerate(texts):
                if text not in expected:raise ValueError('dataset_package_caption_changed')
                tensor,size,output,conditioning=expected[text]
                if not torch.equal(images[position],tensor):raise ValueError('dataset_package_pixels_changed')
                if batch['original_sizes_hw'][position].tolist()!=[size[1],size[0]] or batch['target_sizes_hw'][position].tolist()!=[output[1],output[0]] or batch['crop_top_lefts'][position].tolist()!=list(conditioning):
                    raise ValueError('dataset_package_conditioning_changed')
        if Counter(exposures)!=Counter({text:3 for text in expected}) or sorted(sizes)!=[1,1]+[2]*8:
            raise ValueError('dataset_package_exposures_changed')
        if dataset.tokenize_strategy.captions!=exposures:raise ValueError('dataset_package_tokenizer_pair_changed')
        verify_unchanged(package,snapshots)
    return expected_report()
