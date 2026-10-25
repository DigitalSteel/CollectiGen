"""Generated SDXL dataset/tokenizer/encoder pairing experiment.

This owned native-child contract joins only generated fixtures.  It verifies
that the exact captions admitted by the generated package reach both SDXL
tokenizer roles and their matching input-embedding tables.  It never accepts a
selected model or tokenizer, runs a transformer forward pass, or grants a
training/offline/readiness claim.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
import re

from . import training_dataset_preparation as preparation
from . import training_model_loading_contract as model_loading
from . import training_tokenizer_contract as tokens
from .training_configuration import TrainingConfiguration


PROFILE = 'generated-sdxl-dataset-model-pairing-v1'
SHA = re.compile(r'[0-9a-f]{64}\Z')
CAPTIONS = {
    'café, hero': ('café', ',', 'hero'),
    'café, villain': ('café', ',', 'villain'),
}
ENCODERS = (
    ('encoder_1', 1, 768, 12, 12, 'quick_gelu',
     'conditioner.embedders.0.transformer.text_model.embeddings.token_embedding.weight'),
    ('encoder_2', 2, 1280, 32, 20, 'gelu',
     'conditioner.embedders.1.model.token_embedding.weight'),
)


def _hash(value):
    return tokens.sha(tokens.canonical(value))


def _dataset_request(value, *, native=False):
    durable = {'package', 'binding', 'summary', 'code_bundle'}
    envelopes = (durable,) if native else (durable, {'binding', 'summary'}, {'memory', 'binding', 'summary'})
    if type(value) is not dict or set(value) not in envelopes:
        raise ValueError('model_pairing_dataset_request_invalid')
    if set(value) == durable and any(type(value[name]) is not str or not value[name] for name in ('package', 'code_bundle')):
        raise ValueError('model_pairing_dataset_path_invalid')
    binding = value['binding']
    if type(binding) is not dict or set(binding) != {'profile', 'package_fingerprint', 'manifest_sha256', 'location_sha256', 'recipe_sha256', 'policy_sha256'} or binding['profile'] != preparation.PROFILE or any(type(binding[name]) is not str or not SHA.fullmatch(binding[name]) for name in binding if name != 'profile'):
        raise ValueError('model_pairing_dataset_binding_invalid')
    summary = value['summary']
    required = {'training_images', 'evaluation_images', 'repeats', 'epochs_planned', 'epoch_exposures',
                'batch_size', 'epoch_batches', 'partial_batches', 'holds', 'pixel_samples',
                'pair_counts_sha256', 'configuration_sha256', 'artifact_inventory_sha256',
                'dataset_sha256', 'blueprint_sha256'}
    if type(summary) is not dict or set(summary) != required or type(summary['holds']) is not dict:
        raise ValueError('model_pairing_dataset_summary_invalid')
    for name in ('training_images', 'evaluation_images', 'repeats', 'epochs_planned', 'epoch_exposures',
                 'batch_size', 'epoch_batches', 'partial_batches', 'pixel_samples'):
        if type(summary[name]) is not int or summary[name] < 0:
            raise ValueError('model_pairing_dataset_summary_invalid')
    if any(type(summary[name]) is not str or not SHA.fullmatch(summary[name])
           for name in ('pair_counts_sha256', 'configuration_sha256', 'artifact_inventory_sha256',
                        'dataset_sha256', 'blueprint_sha256')):
        raise ValueError('model_pairing_dataset_summary_invalid')
    # Reuse the schema-11 report constructor so admission/no-holds remains the
    # authority for this contract rather than a copied predicate.
    preparation.expected_report(value)
    return value


def _pairing_request(value):
    if type(value) is not dict or set(value) != {'caption_limit'} or type(value['caption_limit']) is not int or value['caption_limit'] not in tokens.LIMITS:
        raise ValueError('model_pairing_request_invalid')
    return value


def _encoder_report():
    return [
        {'name': name, 'role': role, 'vocab_rows': 49408, 'hidden_size': hidden,
         'layers': layers, 'heads': heads, 'activation': activation,
         'context': 77, 'projection_dim': hidden, 'dtype': 'F32'}
        for name, role, hidden, layers, heads, activation, _ in ENCODERS
    ]


def expected_report(dataset_request, pairing_request):
    """Return the path-free finite aggregate expected from a successful probe."""
    dataset_request = _dataset_request(dataset_request)
    pairing_request = _pairing_request(pairing_request)
    dataset_report = preparation.expected_report(dataset_request)
    model_report = model_loading.expected_report()
    exposures = dataset_request['summary']['epoch_exposures'] * 2
    return {
        'schema_version': 1, 'kind': 'generated_sdxl_model_pairing', 'profile': PROFILE,
        'dataset_binding': dict(dataset_request['binding']),
        'dataset_fingerprints': {
            name: dataset_request['summary'][name]
            for name in ('configuration_sha256', 'dataset_sha256', 'blueprint_sha256', 'pair_counts_sha256')
        },
        'caption_limit': pairing_request['caption_limit'],
        'accepted_caption_set_sha256': _hash(sorted(CAPTIONS)),
        'generated_model': {'base_sha256': model_loading.FIXTURE_SHA256['base'],
                            'catalog_sha256': model_loading.CATALOG_SHA256},
        'generated_tokenizers': [
            {'role': role, 'identity': tokens.fixture_identity(tokens.fixture_files(role))}
            for role in (1, 2)
        ],
        'encoders': _encoder_report(),
        'paired_token_exposures': exposures,
        'paired_embedding_exposures': exposures,
        'dataset_preparation_checks': deepcopy(dataset_report),
        'model_loading_checks': deepcopy(model_report),
        'generated_model_pairing_verified': True,
        'selected_dataset_verified': False, 'selected_model_weights_loaded': False,
        'model_compatibility_verified': False, 'forward_pass_verified': False,
        'offline_execution_verified': False, 'execution_ready': False,
    }


def validate_report(value, dataset_request, pairing_request):
    if tokens.canonical(value) != tokens.canonical(expected_report(dataset_request, pairing_request)):
        raise ValueError('model_pairing_report_changed')
    return value


def _verify_tokenizers(root):
    for role in (1, 2):
        folder = root / str(role)
        blobs = {name: (folder / name).read_bytes() for name in tokens.FILES}
        if tokens.fixture_identity(blobs) != tokens.fixture_identity(tokens.fixture_files(role)):
            raise ValueError('model_pairing_tokenizer_fixture_changed')


def _write_tokenizers(tokenizer_class, work):
    root = Path(work) / 'generated-pairing-tokenizers'
    root.mkdir()
    result = []
    for role in (1, 2):
        folder = root / str(role)
        folder.mkdir()
        blobs = tokens.fixture_files(role)
        for name, raw in blobs.items():
            with (folder / name).open('xb') as handle:
                handle.write(raw)
        special = json.loads(blobs['special_tokens_map.json'])
        tokenizer = tokenizer_class(vocab_file=str(folder / 'vocab.json'),
                                    merges_file=str(folder / 'merges.txt'),
                                    model_max_length=tokens.WIDTH, **special)
        if (tokenizer.bos_token_id, tokenizer.eos_token_id, tokenizer.pad_token_id,
                tokenizer.model_max_length) != (tokens.BOS, tokens.EOS, tokens.EOS, tokens.WIDTH):
            raise ValueError('model_pairing_tokenizer_specials_changed')
        if role == 2:
            tokenizer.pad_token_id = 0
        result.append(tokenizer)
    _verify_tokenizers(root)
    return result, root


def _strategy(strategy_class, tokenizers, limit):
    strategy = strategy_class.__new__(strategy_class)
    strategy.tokenizer1, strategy.tokenizer2 = tokenizers
    strategy.max_length = limit + 2
    return strategy


def _values(tensor, shape, torch, code):
    if list(tensor.shape) != shape or tensor.device.type != 'cpu' or tensor.dtype != torch.int64:
        raise ValueError(code)
    value = tensor.tolist()
    tokens.canonical(value)  # rejects non-finite/unsupported JSON values
    return value


def _check_model(models, torch, model_catalog):
    model_loading.checked_catalog(model_catalog)
    constants = {name: value for name, _, _, _, value in model_loading.fixture_rows(model_catalog, 'base')}
    result = {}
    for name, role, hidden, layers, heads, activation, checkpoint_key in ENCODERS:
        model = models.get(name)
        if model is None:
            raise ValueError('model_pairing_encoder_missing')
        config = model.config
        if (config.vocab_size, config.hidden_size, config.num_hidden_layers,
                config.num_attention_heads, config.hidden_act, config.max_position_embeddings,
                config.projection_dim) != (49408, hidden, layers, heads, activation, 77, hidden):
            raise ValueError('model_pairing_encoder_configuration_changed')
        embedding = model.get_input_embeddings().weight
        if list(embedding.shape) != [49408, hidden] or embedding.device.type != 'cpu' or embedding.dtype != torch.float32:
            raise ValueError('model_pairing_embedding_layout_changed')
        if checkpoint_key not in constants:
            raise ValueError('model_pairing_embedding_fixture_missing')
        result[role] = (model, constants[checkpoint_key])
    return result


def validate_manifest_scope(manifest, dataset_request, pairing_request):
    """Refuse known scope mismatches early; cached manifests grant no authority."""
    _dataset_request(dataset_request)
    _pairing_request(pairing_request)
    config = TrainingConfiguration.from_dict(manifest['training_configuration'])
    if config.base_model_id != 'sha256:' + model_loading.FIXTURE_SHA256['base']:
        raise ValueError('model_pairing_base_identity_changed')
    if config.max_caption_tokens != pairing_request['caption_limit']:
        raise ValueError('model_pairing_caption_limit_changed')
    if tokens.canonical(preparation.assess_manifest(manifest)) != tokens.canonical(dataset_request['summary']):
        raise ValueError('dataset_admission_changed')
    artifacts = {row['path']: row for row in manifest['artifacts']}
    for item in manifest['package_plan']['items']:
        if item['role'] == 'training' and artifacts[item['caption_path']]['package_caption'] not in CAPTIONS:
            raise ValueError('model_pairing_caption_scope_changed')


def probe_pairing(model_module, torch, safetensors, model_catalog, tokenizer_class, strategy_class,
                  config_module, entry_module, dataset_module, np, Image, dataset_request,
                  pairing_request, work, *, observe=None):
    """Run the finite generated pairing check through the real native dataset path."""
    dataset_request = _dataset_request(dataset_request, native=True)
    pairing_request = _pairing_request(pairing_request)
    limit = pairing_request['caption_limit']
    package = Path(dataset_request['package'])
    manifest = preparation.read_manifest(package, dataset_request['binding'])
    validate_manifest_scope(manifest, dataset_request, pairing_request)
    tokenizers, tokenizer_root = _write_tokenizers(tokenizer_class, work)
    strategy = _strategy(strategy_class, tokenizers, limit)
    models = {}
    exposures = {'tokens': 0, 'embeddings': 0}
    captured = []

    def inspect_models(assembled):
        models.update(_check_model(assembled, torch, model_catalog))

    model_report = model_loading.probe_generated_models(model_module, torch, safetensors,
                                                        model_catalog, work,
                                                        inspect_models=inspect_models)
    if model_report != model_loading.expected_report() or set(models) != {1, 2}:
        raise ValueError('model_pairing_model_loading_changed')

    def tokenize(text):
        pieces = CAPTIONS.get(text)
        if pieces is None:
            raise ValueError('model_pairing_caption_scope_changed')
        actual = strategy.tokenize([text])
        if type(actual) is not tuple or len(actual) != 2:
            raise ValueError('model_pairing_tokenizer_roles_changed')
        for role, tensor in enumerate(actual, 1):
            payload = tokens.expected_payload(role, pieces)
            expected = tokens.expected_chunks(role, payload, limit)
            if tokenizers[role - 1].encode(text, add_special_tokens=False, truncation=False) != payload:
                raise ValueError('model_pairing_tokenizer_payload_changed')
            if _values(tensor, [1, limit // 75, tokens.WIDTH], torch, 'model_pairing_token_shape_changed') != [expected]:
                raise ValueError('model_pairing_token_values_changed')
        return actual

    def observe_batch(batch, loaded_paths):
        captions = batch.get('captions')
        ids_list = batch.get('input_ids_list')
        if type(captions) is not list or type(ids_list) not in {list, tuple} or len(ids_list) != 2 or len(loaded_paths) != len(captions):
            raise ValueError('model_pairing_batch_scope_changed')
        for role, ids in enumerate(ids_list, 1):
            expected = []
            for text in captions:
                pieces = CAPTIONS.get(text)
                if pieces is None:
                    raise ValueError('model_pairing_caption_scope_changed')
                expected.append(tokens.expected_chunks(role, tokens.expected_payload(role, pieces), limit))
            if _values(ids, [len(captions), limit // 75, tokens.WIDTH], torch, 'model_pairing_batch_token_shape_changed') != expected:
                raise ValueError('model_pairing_batch_token_values_changed')
            model, constant = models[role]
            with torch.no_grad():
                embedded = model.get_input_embeddings()(ids)
            hidden = 768 if role == 1 else 1280
            if (list(embedded.shape) != [len(captions), limit // 75, tokens.WIDTH, hidden]
                    or embedded.device.type != 'cpu' or embedded.dtype != torch.float32
                    or not torch.isfinite(embedded).all().item()
                    or not torch.all(embedded == constant).item()):
                raise ValueError('model_pairing_embedding_values_changed')
            exposures['tokens'] += len(captions)
            exposures['embeddings'] += len(captions)
        if observe is not None:
            captured.append({'input_ids_list': tuple(ids.detach().clone() for ids in ids_list),
                **{name: batch[name].detach().clone() for name in
                   ('original_sizes_hw', 'crop_top_lefts', 'target_sizes_hw')}})

    dataset_report = preparation.probe_package(config_module, entry_module, dataset_module, torch, np,
                                                Image, dataset_request, work, tokenize=tokenize,
                                                observe_batch=observe_batch)
    if dataset_report != preparation.expected_report(dataset_request):
        raise ValueError('model_pairing_dataset_preparation_changed')
    expected = dataset_request['summary']['epoch_exposures'] * 2
    if exposures != {'tokens': expected, 'embeddings': expected}:
        raise ValueError('model_pairing_exposures_changed')
    _verify_tokenizers(tokenizer_root)
    if observe is not None:
        observe({role: model for role, (model, _) in models.items()}, strategy, tuple(captured))
    return validate_report(expected_report(dataset_request, pairing_request), dataset_request, pairing_request)
