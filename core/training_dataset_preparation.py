"""Bounded selected-package image admission and native preparation contract.

Standard library only until the contained child supplies its numeric modules.
The native child receives an owned verified replica, never the live package.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from functools import lru_cache
import json
import mmap
import os
from pathlib import Path, PurePosixPath
import re
from uuid import uuid4

from . import training_dataset_package_contract as generated
from . import training_package_effects as effects
from .training_configuration import TrainingConfiguration

canonical, sha = generated.canonical, generated.sha
PROFILE = 'sdxl-package-pixel-preserving-preparation-v1'
MAX_MANIFEST = 64 * 1024 * 1024
MAX_EXPOSURES = 50_000
MAX_BATCH = 4
MAX_PACKAGE_BYTES = 512 * 1024 * 1024
MAX_PIXEL_SAMPLES = 512 * 1024 * 1024
MAX_SELECTION = 16 * 1024
SHA = re.compile(r'[0-9a-f]{64}\Z')


def digest(value):
    return sha(canonical(value))


def validate_selection(value):
    fields = {'root', 'package_fingerprint', 'manifest_sha256', 'recipe'}
    if type(value) is not dict or set(value) != fields:
        raise ValueError('dataset_selection_invalid')
    path = value['root']
    if type(path) is not str or not path or len(path) > 32767 or '\0' in path or path.startswith(('\\\\', '//')) or not Path(path).is_absolute() or str(Path(os.path.abspath(path))) != path:
        raise ValueError('dataset_location_invalid')
    if any(type(value[key]) is not str or not SHA.fullmatch(value[key]) for key in ('package_fingerprint', 'manifest_sha256')):
        raise ValueError('dataset_identity_invalid')
    recipe = value['recipe']
    if type(recipe) is not dict or set(recipe) != {'rank', 'alpha', 'output_precision', 'gradient_checkpointing', 'schema_version'} or type(recipe['schema_version']) is not int or recipe['schema_version'] != 1 or type(recipe['rank']) is not int or not 4 <= recipe['rank'] <= 128 or type(recipe['alpha']) not in {int, float} or not 1 <= recipe['alpha'] <= recipe['rank'] or recipe['output_precision'] not in {'fp16', 'bf16', 'float'} or type(recipe['gradient_checkpointing']) is not bool:
        raise ValueError('dataset_recipe_invalid')
    return value


def binding(selection):
    validate_selection(selection)
    return {'profile': PROFILE, 'package_fingerprint': selection['package_fingerprint'],
        'manifest_sha256': selection['manifest_sha256'], 'location_sha256': digest(selection['root']),
        'recipe_sha256': digest(selection['recipe']), 'policy_sha256': effects.profile_sha256()}


def validate_descriptor(value):
    if type(value) is not dict or set(value) != {'mapping', 'length', 'sha256'} or type(value['mapping']) is not str or not re.fullmatch(r'Local\\CollectiGen\.Dataset\.[0-9a-f]{32}', value['mapping']) or type(value['length']) is not int or not 0 < value['length'] <= MAX_SELECTION or type(value['sha256']) is not str or not SHA.fullmatch(value['sha256']):
        raise ValueError('dataset_memory_invalid')
    return value


class SelectionMemory:
    def __init__(self, selection):
        raw = canonical(validate_selection(selection))
        self.descriptor = {'mapping': 'Local\\CollectiGen.Dataset.' + uuid4().hex,
            'length': len(raw), 'sha256': sha(raw)}
        validate_descriptor(self.descriptor)
        self.mapping = mmap.mmap(-1, len(raw), tagname=self.descriptor['mapping'], access=mmap.ACCESS_WRITE)
        self.mapping[:] = raw

    def close(self):
        self.mapping.close()


def read_selection(descriptor):
    validate_descriptor(descriptor)
    with mmap.mmap(-1, descriptor['length'], tagname=descriptor['mapping'], access=mmap.ACCESS_READ) as memory:
        raw = memory[:]
    if sha(raw) != descriptor['sha256']:
        raise ValueError('dataset_memory_changed')
    value = validate_selection(json.loads(raw))
    if canonical(value) != raw:
        raise ValueError('dataset_memory_noncanonical')
    return value


def blueprint(directory, config):
    value = generated.expected_blueprint(directory)
    dataset = value['dataset_group']['datasets'][0]
    dataset['params'].update(batch_size=config.batch_size)
    dataset['subsets'][0]['params']['num_repeats'] = config.repeats
    return value


def assess_manifest(manifest):
    """Pure admission after complete shared verification; bounded per-epoch work."""
    config = TrainingConfiguration.from_dict(manifest['training_configuration'])
    items = manifest['package_plan']['items']
    if len(items) > effects.MAX_ITEMS:
        raise ValueError('dataset_scope_exceeded')
    artifacts = {row['path']: row for row in manifest['artifacts']}
    holds = Counter(); pairs = []; buckets = Counter(); training = 0; pixel_samples = 0
    for item in items:
        image, caption = artifacts[item['image_path']], artifacts[item['caption_path']]
        assessment = effects.assess_image(image, caption, item['role'], config)
        if item['role'] != 'training':
            continue
        training += 1
        width, height = image['transform']['output_dimensions']
        pixel_samples += width * height * config.repeats
        holds.update(assessment.holds)
        if assessment.status != 'reference_match' and not assessment.holds:
            holds['image_effects_unmeasured'] += 1
        if assessment.resized_wh != assessment.input_wh or assessment.output_wh != assessment.input_wh:
            holds['pixel_preservation_unmeasured'] += 1
        if assessment.removed_ltrb != (0, 0, 0, 0) or assessment.conditioning_crop_hw != (0, 0):
            holds['pixel_geometry_unmeasured'] += 1
        buckets[tuple(image['transform']['output_dimensions'])] += config.repeats
        pairs.append([item['artifact_id'], image['sha256'], caption['sha256'], config.repeats])
    if config.target_model_family.casefold() != 'sdxl' or config.target_model_version not in {'', '1.0', 'base-1.0'} or config.output_kind != 'lora' or config.target_resolution != 1024 or config.bucket_policy != 'aspect_ratio' or config.allow_upscale or config.crop_policy != 'none':
        holds['dataset_recipe_unmeasured'] += 1
    if not 1 <= training * config.repeats <= MAX_EXPOSURES or not 1 <= config.batch_size <= MAX_BATCH:
        holds['dataset_scope_exceeded'] += 1
    if pixel_samples > MAX_PIXEL_SAMPLES or sum(row['size'] for row in manifest['artifacts']) > MAX_PACKAGE_BYTES:
        holds['dataset_work_budget_exceeded'] += 1
    return {'training_images': training, 'evaluation_images': len(items) - training,
        'repeats': config.repeats, 'epochs_planned': config.epochs,
        'epoch_exposures': training * config.repeats, 'batch_size': config.batch_size,
        'epoch_batches': sum((count + config.batch_size - 1) // config.batch_size for count in buckets.values()),
        'partial_batches': sum(bool(count % config.batch_size) for count in buckets.values()),
        'holds': dict(sorted(holds.items())), 'pixel_samples': pixel_samples, 'pair_counts_sha256': digest(sorted(pairs)),
        'configuration_sha256': digest(manifest['training_configuration']),
        'artifact_inventory_sha256': digest(manifest['artifacts']),
        'dataset_sha256': digest(effects.dataset_proposal('<selected-training>', config)),
        'blueprint_sha256': digest(blueprint('<selected-training>', config))}


def profile_report(selected_binding, summary=None, reason='package_unavailable_or_changed'):
    return {'schema_version': 1, 'kind': 'sdxl_dataset_admission', 'binding': selected_binding,
        'status': 'admitted' if reason == 'admitted' else 'held', 'reason': reason, 'summary': summary,
        'package_preparation_verified': False, 'selected_dataset_verified': False,
        'model_compatibility_verified': False, 'offline_execution_verified': False, 'execution_ready': False}


def expected_report(request):
    summary = request['summary']
    if summary['holds'] or not 1 <= summary['epoch_exposures'] <= MAX_EXPOSURES:
        raise ValueError('dataset_admission_held')
    return {'schema_version': 1, 'kind': 'sdxl_package_preparation', 'profile': PROFILE,
        'binding': request['binding'], 'summary': summary,
        'epochs_observed': 1, 'paired_exposures': summary['epoch_exposures'],
        'batches': summary['epoch_batches'], 'partial_batches': summary['partial_batches'],
        'pair_counts_sha256': summary['pair_counts_sha256'], 'source_pixels': 'decoded_PNG_RGB',
        'pixel_comparison': 'exact_float32_normalization_every_exposure',
        'geometry': 'input_equals_resized_equals_output_zero_crop',
        'pairing': 'per_batch_original_loader_paths_and_exact_captions',
        'tokenizer_scope': 'capture_pre_tokenizer_text_only', 'package_unchanged': True,
        'package_preparation_verified': True, 'selected_dataset_verified': False,
        'model_compatibility_verified': False, 'offline_execution_verified': False, 'execution_ready': False}


def validate_report(value, request):
    if canonical(value) != canonical(expected_report(request)):
        raise ValueError('dataset_preparation_report_changed')
    return value


def read_manifest(package, selected_binding):
    path = package / generated.MANIFEST
    if path.stat().st_size > MAX_MANIFEST:
        raise ValueError('dataset_scope_exceeded')
    manifest = json.loads(path.read_bytes())
    if digest(manifest) != selected_binding['manifest_sha256'] or manifest['package_fingerprint'] != selected_binding['package_fingerprint']:
        raise ValueError('dataset_package_changed')
    artifacts = manifest['artifacts']
    if type(artifacts) is not list or not 1 <= len(artifacts) <= effects.MAX_ITEMS * 2 + 32:
        raise ValueError('dataset_inventory_invalid')
    names = set()
    for artifact in artifacts:
        name = artifact['path']
        if type(name) is not str or not name or '\\' in name or ':' in name or PurePosixPath(name).is_absolute() or any(part in {'', '.', '..'} for part in name.split('/')) or name.casefold() in names or name == generated.MANIFEST:
            raise ValueError('dataset_path_invalid')
        names.add(name.casefold())
        if type(artifact['size']) is not int or not 0 <= artifact['size'] <= MAX_PACKAGE_BYTES:
            raise ValueError('dataset_scope_exceeded')
    return manifest


def probe_package(config_module, entry_module, dataset_module, torch, np, Image, request, work,
                  *, tokenize=None, observe_batch=None):
    """One synchronous native epoch from the verified immutable replica."""
    torch.set_num_threads(2)
    package = Path(request['package'])
    manifest = read_manifest(package, request['binding'])
    if canonical(assess_manifest(manifest)) != canonical(request['summary']):
        raise ValueError('dataset_admission_changed')
    expected_report(request)
    config = TrainingConfiguration.from_dict(manifest['training_configuration'])
    directory = package / 'images/training'
    artifacts = {row['path']: row for row in manifest['artifacts']}
    pairs = {str(package / item['image_path']): item for item in manifest['package_plan']['items'] if item['role'] == 'training'}
    expected_counts = Counter({path: config.repeats for path in pairs})
    with generated.readonly_package(package):
        path = work / 'selected-dataset.json'
        with path.open('xb') as handle:
            handle.write(canonical(effects.dataset_proposal(directory, config)))
        args = entry_module.setup_parser().parse_args(['--max_data_loader_n_workers', '0'])
        config_module.accelerator_setup.prepare_dataset_args(args, True)
        generator = config_module.BlueprintGenerator(config_module.ConfigSanitizer(True, True, False, True))
        built = generator.generate(config_module.load_user_config(str(path)), args)
        if canonical(asdict(built)) != canonical(blueprint(directory, config)):
            raise ValueError('dataset_mapping_changed')
        if dataset_module.LatentsCachingStrategy.get_strategy() is not None:
            raise ValueError('dataset_cache_strategy_changed')
        group, validation = config_module.generate_dataset_group_by_blueprint(built.dataset_group)
        if validation is not None or len(group.datasets) != 1:
            raise ValueError('dataset_roles_changed')
        dataset = group.datasets[0]
        dataset.verify_bucket_reso_steps(32)
        if set(dataset.image_data) != set(pairs) or dataset.num_train_images != request['summary']['epoch_exposures'] or dataset.num_reg_images != 0 or len(dataset) != request['summary']['epoch_batches']:
            raise ValueError('dataset_inventory_changed')
        class CaptureText:
            def __init__(self): self.captions = []
            def tokenize(self, text):
                self.captions.append(text)
                if tokenize is not None:
                    return tokenize(text)
                return [torch.tensor([[1, 2]], dtype=torch.int64)]
        dataset.tokenize_strategy = CaptureText()
        original_loader = dataset.load_image_with_face_info
        loaded = []
        def observe_loader(subset, path, alpha_mask=False):
            if path not in pairs or alpha_mask:
                raise ValueError('dataset_loader_scope_changed')
            loaded.append(path)
            return original_loader(subset, path, alpha_mask)
        dataset.load_image_with_face_info = observe_loader
        @lru_cache(maxsize=2)
        def expected_pixels(path):
            item = pairs[path]; artifact = artifacts[item['image_path']]
            with Image.open(path) as image:
                width, height = artifact['transform']['output_dimensions']
                if image.size != (width, height) or width * height > 1024 * 1024:
                    raise ValueError('dataset_source_geometry_changed')
                if image.mode != 'RGB' or image.format != 'PNG' or getattr(image, 'n_frames', 1) != 1 or image.info.get('icc_profile') or 'transparency' in image.info:
                    raise ValueError('dataset_pixels_unmeasured')
                image.load()
                pixels = np.array(image)
            width, height = artifact['transform']['output_dimensions']
            if pixels.shape != (height, width, 3) or pixels.dtype != np.uint8:
                raise ValueError('dataset_source_geometry_changed')
            return torch.from_numpy(pixels).permute(2, 0, 1).float().div(255).sub(.5).div(.5)
        observed = Counter(); sizes = []
        try:
            for batch_index in range(len(dataset)):
                loaded.clear(); dataset.tokenize_strategy.captions.clear()
                batch = dataset[batch_index]
                texts, images = batch['captions'], batch['images']
                if len(loaded) != len(texts) or dataset.tokenize_strategy.captions != texts or images.shape[0] != len(texts) or images.device.type != 'cpu' or images.dtype != torch.float32:
                    raise ValueError('dataset_pairing_changed')
                if batch['latents'] is not None or batch['text_encoder_outputs_list'] is not None or batch['alpha_masks'] is not None or batch['flippeds'] != [False] * len(texts) or batch['loss_weights'].tolist() != [1.0] * len(texts):
                    raise ValueError('dataset_processing_changed')
                sizes.append(len(texts))
                for position, (path, text) in enumerate(zip(loaded, texts)):
                    item = pairs[path]; image, caption = artifacts[item['image_path']], artifacts[item['caption_path']]
                    width, height = image['transform']['output_dimensions']; info = dataset.image_data[path]
                    if text != caption['package_caption'] or (package / item['caption_path']).read_bytes() != (text + '\n').encode('utf-8'):
                        raise ValueError('dataset_caption_changed')
                    if tuple(info.resized_size) != (width, height) or tuple(info.bucket_reso) != (width, height) or batch['original_sizes_hw'][position].tolist() != [height, width] or batch['target_sizes_hw'][position].tolist() != [height, width] or batch['crop_top_lefts'][position].tolist() != [0, 0]:
                        raise ValueError('dataset_pixel_geometry_changed')
                    if not torch.equal(images[position], expected_pixels(path)):
                        raise ValueError('dataset_pixels_changed')
                    observed[path] += 1
                if observe_batch is not None:
                    observe_batch(batch, tuple(loaded))
            if observed != expected_counts or sum(size < config.batch_size for size in sizes) != request['summary']['partial_batches']:
                raise ValueError('dataset_exposures_changed')
            actual_pairs = sorted([item['artifact_id'], artifacts[item['image_path']]['sha256'], artifacts[item['caption_path']]['sha256'], observed[path]] for path, item in pairs.items())
            if digest(actual_pairs) != request['summary']['pair_counts_sha256']:
                raise ValueError('dataset_pair_identity_changed')
            if digest(read_manifest(package, request['binding'])) != request['binding']['manifest_sha256']:
                raise ValueError('dataset_package_changed')
        finally:
            dataset.load_image_with_face_info = original_loader
            expected_pixels.cache_clear()
    return expected_report(request)
