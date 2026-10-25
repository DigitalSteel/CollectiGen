"""Pinned generated continuation policy and non-executable compatibility records.

The qualification names code and numerical policy, not arbitrary learned-resource
or device support. Current admission binds selected software independently.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from . import training_checkpoint as checkpoint
from .training_process import _bytes, _read, _write_new
from .training_resume_adapter import PROFILE as ADAPTER_PROFILE, REVISION

PROFILE = 'collectigen.sdxl-continuation-binding-v1'
QUALIFICATION_SHA256 = '72bf4967a6527d63d36286d72bbb8f4aaf34746de1750431a6ea64c071d7db24'
SOURCE_SHA256 = '0ea59d6da63b67c84a7d3a929af1b19ffe8893629416b4b743b57b33c85d2ffe'
PATCH_SHA256 = 'c247dd4efdc79c2b504ee027f7a2f5bd45561740c9bc639abdcd6843220969d4'
# Exact reviewed source variants, including the documented host-orchestration
# compatibility extension. No code is executed from the qualification artifact;
# its generated compute results remain historical.
CODE_HASHES = {
    'training_source_loader.py': [
        'b6c91ee1eaac1ea1a70800723e126452f5545efc8b6789ccc2f2a6a3f7bcc1c4',
        '15ebc6afbf81d5c11f8859d1f8358588f59d80e463c590e4d95373e26724fb0e',
    ],
    'training_windows_file_identity.py': [
        '3ec95c2119e2208c8685d616570dd106025b5d328520d621b127dde6843b7cb4',
        '2dc39b6f2a9d008c55b448fcbf4442807f0e55ec10ffb18a948078dcaf220d08',
    ],
    'training_model_loading_contract.py': [
        '0dfa359e0c38da83f11a1c406feb6207743d2b27e6a759e637908ff4c4890040',
        'a126895d76709e91abce9017109621bb58d8ebd5e7c4702ffd44545b33106321',
    ],
    'training_sdxl_loading.py': [
        '04dafeb03ad596e743ed6483a2f831c4099c4f8d7ad501a40ae4faf8a141f817',
        '1a2d5cc7503f2e325f40a17f7f06cac38e488312445508fec415c97047ef76b5',
    ],
    'training_model_contract.py': [
        '4ddd7d0786bfeb1522c5590ca0a454ecc772a197a98c62c27e47b5fc9a86b973',
        'cbe42324b4ba8a56518ebb8a231840d988a93b7a7b1536171996a776bfef3819',
    ],
    'model_resource_registry.py': [
        # BUG284 reviewed source; fresh native baseline and trial proof retained.
        '3be5a9d83da085c2bfdfb35d29e2c2acbdbebc0479dbf8277f991b754d64d61c',
        '3f09f7726563b93312fb727b7fa6d7b56360623212c390dc7beedd5ea0770b0d',

        'f4c3b8e9af26944abd58ac5880e41e614bb560e1e3047726f1ec81a42eb25d7b',
        'fe69b8ee79e3f4568de27142bb6645bbd8616ef9e65ab6a57d5f77796667bf31',
        # Reviewed pack identities; existing single-file training resolution is retained.
        '217d3326812b396b2118d5149a28b599db038e3368c01ba27c5992c11247f49b',
        'bf538a1a4d935f72fb40cc7684b74c369c43f47c5dbe64bd99e44661bb02ad96',
    ],
    'managed_model_identity.py': [
        '02e3144b1d6ac97050813b611825f5b2d231f763b9ca2a1ce192d9ea4e91cf24',
        'e007eb51feb4ee7de1d31c156620d36c70b874ec5a92f8dfef151076baf16bd7',
    ],
    'training_source_io.py': [
        # BUG284 reviewed source; fresh native baseline and trial proof retained.
        '4ba0433a7fe3dd1faa146fd16d5ccebbeb53c8d1ce864193a4dc2b24fbe5dfed',
        '7d091490b0218537e9f30751c60c06b20f5ae11f749cd84de858229b59b66040',

        '2996955c98cdf6f94fdd96aae860f6343fa2fa04afb41ff7356043dabb262097',
        '8a7daf3d2e74e96359baec95bb587f92cf4d907ebb265cfb23e4c09cacaad4a9',
    ],
    'training_restricted_loading_contract.py': [
        '0032817951aba8261def4981c91d2265176d2a266738ceb107180d772eb08801',
        'ab45c8660fbd0e073a61d6bc02448587b5872ccb00bd696092e82567e699463b',
    ],
    'training_dataset_package_contract.py': [
        '1aff0241f5094fb302a3b201faf932429c29d4901e56cdbd1635f139325ed8ae',
        'b09090abdc7e800811497cae12b614d7531bb8600c0dcf4041ac1e63fd57dd7b',
    ],
    'training_package_effects.py': [
        '4e8f4cfcbae8b8aed7ee7e6c04a1a0ae852492787fe42102990392506743deb2',
        '793198bffa68aaaeb9ba71d1bca1f2135e18e63ef2d7c01d17d5ccd3aa478e85',
    ],
    'training_image_effects_contract.py': [
        '02a3aee6cdfbcf4ce0bbf3aba886acae0f2fcbcea9ef2e38780cb95dbeaa32df',
        '1cfcb6fd6f60328dcd47fd4787dc04854d8c351b0cac93c34fa621ab0aeed38d',
    ],
    'training_model_pairing_contract.py': [
        '274da389f600b3f3e9f37c7b67d425c7c52587dab8ad559ea576c2140050a398',
        '4b93fe4b1aa03da2f71f7d637963923565e38411b6d1521e613ed884b9085bf1',
    ],
    'training_dataset_preparation.py': [
        '75181cc40a5effc6b180573ad02fad696ec4f51dcb1667b37dbe8e55f1110601',
        '97156e9a0d793b1ca8c46ed0b3e0c8ad6f11f69642ef4e88777daabfa1cfb0ac',
    ],
    'training_configuration.py': [
        # BUG284 reviewed source; fresh native baseline and trial proof retained.
        'c5c1ad3dc1d310abdc7797ed70264c0a67764168a5da6ab1cdf0e6b2b68107fb',
        'd91405be0fa3a2498aa7549ec904357426ccc35c191718b1525958dcf7a2613a',

        '5d225285f1dd1131b1cbf7fe0303da7ca6355d7176b95263da8d038c4622c7ed',
        '97770df12820886886fd476e8712351ca89df97ab6ea97e45d24b205e135aff0',
        # Portable optional companion evidence; executable consumers hold unsupported sets.
        '3f62fd113429f7262882f0c1d3259a95cb351a8ea7b14d1a83633e9cb9959cc7',
        'eff659a6fcf78f0c77cf912e3e5ffb9521b8f354ff5d23e18fc4e88d1fa85f17',
    ],
    'training_tokenizer_contract.py': [
        '66cb6e33648bde6e5308ca720cc485d3cd41230518c46be78a712bd823d3d6cc',
        'd14f665b9df4b85f29f0dfb5e537887f489b99585aabf9fc0775360e3d95cdbe',
    ],
    'training_conditioning_contract.py': [
        '12b37e2fadfe9a4675d5b05de8d8e0ad62216bfffd41b574c1cab87268c192a5',
        '27aa3261ccb18f31f7872caad2edb3d0a2412d9aeefb719a9126ac8194f80aaa',
    ],
    'training_conditioning_reference.py': [
        '10f1c5f540c65bc4ae2ce14141364e3d1b3e46034b13de4b7b8baa74d3c72bb1',
        '86caf5efe9bb10dc978f20b2457e2e0a79165c5779eef40fce8f52c5d70db6b1',
    ],
    'training_unet_step_contract.py': [
        '659d6b8fad9e36a53f2cd65c599abe5bfc042bb717cf473685e8f5a532fa01ae',
        'dbfeeffc1cf650488bbce1dae3bf00a20c9bbf6955c33081e43626a0c872fcd1',
    ],
    'training_loop_contract.py': [
        '0cf5c76f15e7935fe6a88c33de656d53e8a3fe388049bd90a5d74163837a53dd',
        'd7db10f51e1f6ceddc2575e9958f9b5550409a14431680a6c52ddbd1dc9e9177',
    ],
    'training_checkpoint.py': [
        'b487a598b9bc3fbf54d901e2b904900c3088793cf300faaa842181e8be9b5ef7',
        'febbeef6dfb39d5e30fd97f0e6944369b19bca2c6bd754ba3d9180eacee4ab84',
    ],
    'training_process.py': [
        'ddb042ed655bab2c555e927ab7a2ff8a5666867c72acb677ed3e35ec86bd14f9',
        '78c41222c5b36384b3e266baafa89732a577624035202e44be590e75b1b9f951',
        # Reviewed host-side AI memory release before native process creation.
        'de683de03a62b016356277f3e99437cff5acd061c8d11c0c9ab0ad0758de187a',
        '4dba3328a7621818c697028f6f0612708b50887601767a423a352deaeb084f93',
    ],
    'training_process_windows.py': [
        '19827309ca86138f9af0e258575f6771a110362e2e9a6a365b0d7c1b6c51606e',
        '48b77ac43d6c7f71d8c3cf3f90126b73793f0d70f644101a83daf61f1bd7f9aa',
    ],
    'training_file_inventory.py': [
        'ac9ac04ba2acdf7c7df1312651d4b2d06abd50c064a9f7250db3a7c00dfa9593',
        '18e02adda84959e7ea6ce6141b179d8fab21139065cb77e9bbbeab9a7d1707b3',
    ],
    'training_verification.py': [
        '0b2d3563d3bb696f23fca80423a9ee43eb7d312575f859245d8dfe931bbca00a',
        '9243c9a5c2663b6fa0da90e3c31a1cd0a8dca1bc582deb783857063896fc5029',
    ],
    'background_jobs.py': [
        '1c5c6877fa2f25e689f46bf4e8b8628d083912672426725288667838bcc40ed1',
        '1e153bce69735ff3a98817e0b13f3e84a4fa971a7026df3d406fd9754e0af1c7',
        # Opt-in image readers; trainers retain ordinary non-yieldable admission.
        '357cf61f15fb6d06ec6abc51a70f8feb4b63b5c314ea86d8cbdddb21ffec77e1',
        '45946a7350821eaea666f556d382a221203b9b37af5368dce3378916cdf3d9f4',
        # Opt-in AI cleanup retains GPU admission and existing recovery ownership.
        '82dd27fb1961f04cec9c05e4c0d3dd1e0cf2e20d64ccf66e33a465feb153bd77',
        'e68b50a1e124c3606076045710966c28bfa6d2dc143b6cbde58ea7b169e083e1',
    ],
    'training_resume_adapter.py': [
        '2bbed799135641cb01a25903f8bf5749029368a196ea39e162985acf2a3e26a6',
        'ea341ea27fa6922d55c2a09c3d2e311c207827159294fd59ab0a8f3171234928',
    ],
    'training_resume_experiment.py': [
        '2d7bc215bf093511c608c2ddaf42312133b1a68addd082ae2a2032aa66d0dacb',
        '88e1b2de859ba9e08e97bbdd1e78d10c10170d92b98a7b297eda78c5364ca546',
    ],
}
SETTINGS = {'processes': 1, 'loader_batch_size': 1, 'workers': 0, 'persistent_workers': False,
    'sampler': 'epoch-seeded-permutation-v1', 'dataset_epoch': 'before-fetch-preserve-rng-v1',
    'loader_rng': 'private-cpu-generator-v1', 'console_log_simple': True,
    'deterministic_algorithms': True, 'cudnn_deterministic': True,
    'cudnn_benchmark': False, 'cublas_workspace_config': ':4096:8',
    'cuda_matmul_tf32': False, 'cudnn_tf32': False,
    'torch_threads': 2, 'random_augmentation': False, 'caption_dropout': False,
    'feature_caching': False, 'validation_sampling': False, 'distributed': False,
    'initial_step_override': None, 'initial_epoch_override': None,
    'resume_position': 'optimizer-steps-v1'}


def digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def profile_contract():
    return {'schema_version': 1, 'profile': ADAPTER_PROFILE,
        'source_revision': REVISION, 'original_source_sha256': SOURCE_SHA256,
        'patched_source_sha256': PATCH_SHA256,
        'checkpoint_profile': checkpoint.PROFILE,
        'compute_code_sha256': digest(CODE_HASHES), 'settings': deepcopy(SETTINGS),
        'qualification': {'result_sha256': QUALIFICATION_SHA256,
            'scope': 'generated-sdxl-64px-bf16', 'runtime_profile': 'kohya-uv-45088f04',
            'images': 4, 'accumulation': 2, 'optimizer_steps': 3, 'seed': 42,
            'rank': 32, 'alpha': 16, 'caption_limit': 75, 'device_capability': [12, 0],
            'native_resume_verified': False, 'selected_recipe_verified': False,
            'application_writer_verified': False, 'device_portability_verified': False}}


def check_profile_code(should_cancel=None):
    """Only owned application source; never a selected image/software path."""
    for name, allowed in CODE_HASHES.items():
        checkpoint._cancel(should_cancel)
        if hashlib.sha256(_read(Path(__file__).with_name(name), limit=2 * 1024**2)).hexdigest() not in allowed:
            raise checkpoint.CheckpointError('continuation_profile_code_changed')
    return profile_contract()


def _inventory(value):
    keys = {'schema_version', 'inventory_sha256', 'files', 'directories', 'bytes', 'scope'}
    if (type(value) is not dict or set(value) != keys or type(value['schema_version']) is not int
            or value['schema_version'] != 1 or not checkpoint._hash(value['inventory_sha256'])
            or value['scope'] != 'explicit_software_directory'
            or type(value['files']) is not int or not 1 <= value['files'] <= 200_000
            or type(value['directories']) is not int or not 1 <= value['directories'] <= 200_000
            or type(value['bytes']) is not int or not 1 <= value['bytes'] <= 64 * 1024**3):
        raise checkpoint.CheckpointError('continuation_software_identity_invalid')


SELECTION_KEYS = {'adapter_id', 'adapter_version', 'source_revision', 'runtime_profile',
    'package', 'configuration_sha256', 'component_binding_sha256', 'registry_revision',
    'base_id', 'local_vae_override_id', 'caption_limit'}


def validate_contract(value):
    """Strict, path-free semantics; this record never grants launch/load authority."""
    from .training_runtime_contract import ADAPTER_ID, ADAPTER_VERSION, RUNTIME_PROFILE_NAMES
    from .training_dataset_preparation import PROFILE as PACKAGE_PROFILE
    try:
        if (type(value) is not dict or set(value) != {'schema_version', 'profile', 'continuation_profile', 'selection', 'runtime'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1 or value['profile'] != PROFILE
                or _bytes(value['continuation_profile']) != _bytes(profile_contract())):
            raise ValueError
        selected = value['selection']
        if (type(selected) is not dict or set(selected) != SELECTION_KEYS
                or selected['adapter_id'] != ADAPTER_ID or type(selected['adapter_version']) is not int
                or selected['adapter_version'] != ADAPTER_VERSION or selected['source_revision'] != REVISION
                or selected['runtime_profile'] not in RUNTIME_PROFILE_NAMES
                or type(selected['caption_limit']) is not int or selected['caption_limit'] not in (75, 150, 225)
                or any(not checkpoint._hash(selected[key]) for key in ('configuration_sha256', 'component_binding_sha256', 'registry_revision'))
                or type(selected['base_id']) is not str or not selected['base_id'].startswith('sha256:')
                or not checkpoint._hash(selected['base_id'][7:])
                or (selected['local_vae_override_id'] is not None and (type(selected['local_vae_override_id']) is not str
                    or not selected['local_vae_override_id'].startswith('sha256:') or not checkpoint._hash(selected['local_vae_override_id'][7:])))):
            raise ValueError
        package = selected['package']
        package_keys = {'profile', 'package_fingerprint', 'manifest_sha256', 'location_sha256', 'recipe_sha256', 'policy_sha256'}
        if (type(package) is not dict or set(package) != package_keys or package['profile'] != PACKAGE_PROFILE
                or any(not checkpoint._hash(package[key]) for key in package_keys - {'profile'})):
            raise ValueError
        runtime = value['runtime']
        if (type(runtime) is not dict or set(runtime) != {'inspection_sha256', 'software_inventories', 'environment_profile'}
                or runtime['environment_profile'] != 'local-processing-v1' or not checkpoint._hash(runtime['inspection_sha256'])
                or type(runtime['software_inventories']) is not dict or set(runtime['software_inventories']) != {'environment', 'python_base'}):
            raise ValueError
        for row in runtime['software_inventories'].values():
            _inventory(row)
        if runtime['software_inventories']['environment']['inventory_sha256'] == runtime['software_inventories']['python_base']['inventory_sha256']:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise checkpoint.CheckpointError('continuation_contract_invalid') from None
    return value


def bind_admission(bound, runtime):
    """Join the current validated prerequisite result; exclude per-probe nonces.

    Inventory and inspection digests retain local filesystem identities. This
    deliberately does not establish portability to another installation/device.
    """
    if (bound.get('continuation_profile_sha256') != digest(profile_contract())
            or runtime.get('status') != 'parser_checked' or runtime.get('software_inventory_verified') is not True
            or runtime.get('native_selected_tokenizer_behavior_verified') is not True
            or runtime.get('source_revision') != REVISION or runtime.get('profile') != bound['runtime_profile']
            or runtime.get('execution_ready') is not False or runtime.get('training_started') is not False):
        raise checkpoint.CheckpointError('continuation_admission_unverified')
    value = {'schema_version': 1, 'profile': PROFILE, 'continuation_profile': profile_contract(),
        'selection': {key: deepcopy(bound[key]) for key in SELECTION_KEYS},
        'runtime': {key: deepcopy(runtime.get(key)) for key in ('inspection_sha256', 'software_inventories', 'environment_profile')}}
    return deepcopy(validate_contract(value))


def _companion(work, name):
    _, receipt = checkpoint._paths(work, name)
    return receipt.with_suffix('.continuation.json')


@checkpoint._safe_error
def seal_checkpoint(work, name, binding, contract, *, step, epoch, completion_sha256, should_cancel=None):
    """Owned save hook only. Existing layout receipts cannot be retrofitted."""
    frozen = deepcopy(validate_contract(contract))
    binding = deepcopy(checkpoint._binding(binding))
    check_profile_code(should_cancel)
    companion = _companion(work, name)
    if companion.exists():
        raise checkpoint.CheckpointError('continuation_receipt_exists')
    receipt_sha256 = checkpoint.seal_checkpoint(work, name, binding, step=step, epoch=epoch,
        completion_sha256=completion_sha256, should_cancel=should_cancel)
    value = {'schema_version': 1, 'kind': 'checkpoint_continuation_contract', 'binding': deepcopy(binding),
        'checkpoint_name': name, 'checkpoint_profile': checkpoint.PROFILE, 'step': step, 'epoch': epoch,
        'checkpoint_receipt_sha256': receipt_sha256, 'contract': frozen, 'contract_sha256': digest(frozen),
        'load_verified': False, 'execution_ready': False}
    checkpoint._cancel(should_cancel)
    raw = _bytes(value)
    _write_new(companion, raw)
    proof = {'checkpoint_receipt_sha256': receipt_sha256, 'continuation_receipt_sha256': hashlib.sha256(raw).hexdigest()}
    inspect_checkpoint(work, name, binding, frozen, **proof, should_cancel=should_cancel)
    return proof


def _checked_companion(work, name, binding, contract, checkpoint_receipt_sha256, continuation_receipt_sha256):
    validate_contract(contract)
    checkpoint._binding(binding)
    if not all(checkpoint._hash(item) for item in (checkpoint_receipt_sha256, continuation_receipt_sha256)):
        raise checkpoint.CheckpointError('continuation_receipt_unbound')
    companion = _companion(work, name)
    raw = _read(companion, limit=32 * 1024)
    if hashlib.sha256(raw).hexdigest() != continuation_receipt_sha256:
        raise checkpoint.CheckpointError('continuation_receipt_changed')
    value = json.loads(raw)
    expected = {'schema_version', 'kind', 'binding', 'checkpoint_name', 'checkpoint_profile', 'step', 'epoch',
        'checkpoint_receipt_sha256', 'contract', 'contract_sha256', 'load_verified', 'execution_ready'}
    if (type(value) is not dict or set(value) != expected or _bytes(value) != raw
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['kind'] != 'checkpoint_continuation_contract' or value['binding'] != binding
            or value['checkpoint_name'] != name or value['checkpoint_profile'] != checkpoint.PROFILE
            or value['checkpoint_receipt_sha256'] != checkpoint_receipt_sha256
            or value['load_verified'] is not False or value['execution_ready'] is not False
            or value['contract_sha256'] != digest(contract) or _bytes(value['contract']) != _bytes(contract)):
        raise checkpoint.CheckpointError('continuation_contract_changed')
    checkpoint._position(value['step'], value['epoch'])
    return value, raw


@checkpoint._safe_error
def inspect_checkpoint(work, name, binding, contract, *, checkpoint_receipt_sha256,
        continuation_receipt_sha256, should_cancel=None):
    """Check semantic compatibility BEFORE hashing opaque optimizer/RNG files."""
    frozen = deepcopy(validate_contract(contract))
    check_profile_code(should_cancel)
    value, raw = _checked_companion(work, name, binding, frozen, checkpoint_receipt_sha256, continuation_receipt_sha256)
    checked = checkpoint.inspect_checkpoint(work, name, binding,
        expected_receipt_sha256=checkpoint_receipt_sha256, should_cancel=should_cancel)
    return _finish(work, name, frozen, value, raw, checked, should_cancel)


def _finish(work, name, frozen, value, raw, checked, should_cancel):
    if (checked['step'], checked['epoch']) != (value['step'], value['epoch']):
        raise checkpoint.CheckpointError('continuation_position_changed')
    check_profile_code(should_cancel)
    if _read(_companion(work, name), limit=32 * 1024) != raw:
        raise checkpoint.CheckpointError('continuation_receipt_changed')
    return {**checked, 'continuation_contract_sha256': digest(frozen),
        'continuation_contract_compatible': True, 'selected_recipe_resume_verified': False, 'resume_ready': False}


@checkpoint._safe_error
def assess_recovery(plan, expected_tip, name, contract, *, checkpoint_receipt_sha256,
        continuation_receipt_sha256, process_service=None, should_cancel=None):
    frozen = deepcopy(validate_contract(contract))
    binding = checkpoint.binding_for(plan)
    check_profile_code(should_cancel)
    value, raw = _checked_companion(plan.destination / 'work', name, binding, frozen,
        checkpoint_receipt_sha256, continuation_receipt_sha256)
    recovered = checkpoint.assess_recovery(plan, expected_tip, name,
        expected_receipt_sha256=checkpoint_receipt_sha256, process_service=process_service, should_cancel=should_cancel)
    return _finish(plan.destination / 'work', name, frozen, value, raw, recovered, should_cancel)
