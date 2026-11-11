"""Separate contained SDXL validation worker; never entered by the trainer loop."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

FLAGS = ('-I', '-S', '-B', '-X', 'utf8')


def evaluate_values(request, evaluate_case):
    """Pure ordering/finite boundary around one native per-image evaluator."""
    values = []
    for case in request['cases']:
        measured = evaluate_case(case)
        if (type(measured) not in {list, tuple} or len(measured) != len(request['timesteps'])
                or any(type(value) not in {int, float} or not math.isfinite(value) or value < 0
                       for value in measured)):
            raise ValueError('validation_native_nonfinite')
        values.append({'stable_id': case['stable_id'],
                       'mse_by_timestep': [float(value) for value in measured]})
    return values


def _worker_request(path, work, process, validation):
    raw = process._read(path, limit=validation.MAX_REQUEST)
    request = json.loads(raw)
    if (type(request) is not dict or process._bytes(request) != raw
            or request.get('profile') != validation.PROFILE or request.get('kind') != 'sdxl_heldout_validation_request'
            or request.get('schema_version') != 1 or request.get('metric') != 'epsilon_denoising_mse'
            or request.get('qualification') not in {'product', 'candidate_only'}
            or request.get('quality_claim') != 'none' or request.get('timesteps') != list(validation.TIMESTEPS)
            or not request.get('cases') or len(request['cases']) > validation.MAX_CASES):
        raise ValueError('validation_worker_request_invalid')
    rows, _ = process.TrainingRunJournal(work.parent).read()
    if not rows or rows[0]['state'] != 'reserved':
        raise ValueError('validation_worker_reservation_missing')
    plan = rows[0]['details']['plan']
    process.TrainingProcessService._validate_receipt(plan)
    digest = hashlib.sha256(raw).hexdigest()
    if (plan['preview_fingerprint'] != digest
            or {'sha256': digest, 'size': len(raw)} not in plan['inputs']
            or plan['command_sha256'] != hashlib.sha256(process._bytes((
                str(Path(sys.executable).absolute()), *FLAGS, str(Path(__file__).absolute()), str(path)))).hexdigest()):
        raise ValueError('validation_worker_process_changed')
    return request, plan['process_fingerprint'], digest


def _recheck_inputs(request, process):
    from core.training_package import TrainingPackageService
    from core import training_validation as validation
    package = TrainingPackageService().verify(request['package_path'])
    if not package.valid or package.package_fingerprint != request['package_fingerprint']:
        raise ValueError('validation_worker_package_changed')
    if request['cases'] != list(validation._cases(package, request['training_process_fingerprint'])):
        raise ValueError('validation_worker_split_changed')
    selected = ((request['model_path'], request['model_sha256']),
                (request['base_path'], request['base_sha256']))
    for path, expected in selected:
        if process.ProcessInput.capture(path).sha256 != expected:
            raise ValueError('validation_worker_model_changed')
    for slot in request['tokenizers']:
        for item in slot['files']:
            if process.ProcessInput.capture(Path(slot['path']) / item['name']).sha256 != item['sha256']:
                raise ValueError('validation_worker_tokenizer_changed')
    for case in request['cases']:
        for key in ('image', 'caption'):
            if process.ProcessInput.capture(Path(request['package_path']) / case[key + '_path']).sha256 != case[key + '_sha256']:
                raise ValueError('validation_worker_artifact_changed')


def _native_values(request):
    """Pinned sd-scripts model/tokenizer path. Parent process owns hard stop."""
    native = request['native']
    from core import training_continuation_hooks
    training_continuation_hooks.prepare_environment()
    site = Path(native['environment_root']) / 'Lib' / 'site-packages'
    source = Path(native['source_root'])
    sys.path[:0] = [str(source), str(site)]
    os.environ.update(HF_HUB_OFFLINE='1', HF_DATASETS_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                      HF_HUB_DISABLE_TELEMETRY='1')
    import numpy as np
    import torch
    # Trainer dependencies can initialize CUDA while importing xformers. Match
    # the qualified training bootstrap: establish numerical policy first.
    training_continuation_hooks.configure_torch(torch)
    from PIL import Image
    import transformers
    from diffusers import DDPMScheduler
    from safetensors import torch as safetensors
    from library import sdxl_model_util, sdxl_train_util, strategy_sdxl
    from networks import lora
    from core import training_reference_loading, training_reference_tokenizers

    if not torch.cuda.is_available() or torch.cuda.current_device() != 0:
        raise ValueError('validation_worker_device_unavailable')
    dtype = {'bf16': torch.bfloat16, 'fp16': torch.float16}.get(native['weight_dtype'])
    if dtype is None:
        raise ValueError('validation_worker_precision_unsupported')
    catalog = json.loads(Path(native['model_catalog_path']).read_bytes())
    models, _ = training_reference_loading.load_reference_models(sdxl_model_util, torch,
        safetensors.load, request['base_path'], request['base_sha256'], catalog)
    for role, model in models.items():
        model.eval().requires_grad_(False).to('cuda', dtype=torch.float32 if role == 'vae' else dtype)
    models['unet'].set_use_sdpa(True)
    tokenizer_root = Path(request['tokenizers'][0]['path']).parent
    selected, report = training_reference_tokenizers.load_reference_tokenizers(
        transformers.CLIPTokenizer, transformers.AddedToken, tokenizer_root)
    if [slot['identity'] for slot in report['slots']] != [slot['identity'] for slot in request['tokenizers']]:
        raise ValueError('validation_worker_tokenizer_identity_changed')
    strategy = strategy_sdxl.SdxlTokenizeStrategy.__new__(strategy_sdxl.SdxlTokenizeStrategy)
    strategy.tokenizer1, strategy.tokenizer2 = selected
    strategy.max_length = native['max_caption_tokens'] + 2
    encoding = strategy_sdxl.SdxlTextEncodingStrategy()
    weights = safetensors.load_file(request['model_path'], device='cpu')
    if not weights or any(not name.startswith('lora_unet_') for name in weights):
        raise ValueError('validation_worker_lora_scope_changed')
    network, _ = lora.create_network_from_weights(1.0, request['model_path'], models['vae'],
        [models['encoder_1'], models['encoder_2']], models['unet'], weights_sd=weights)
    network.apply_to([models['encoder_1'], models['encoder_2']], models['unet'], False, True)
    network.load_state_dict(weights, strict=True)
    network.to('cuda', dtype=dtype).eval().requires_grad_(False)
    if network.text_encoder_loras or len(network.unet_loras) != 722:
        raise ValueError('validation_worker_lora_layout_changed')
    scheduler = DDPMScheduler(beta_start=0.00085, beta_end=0.012,
        beta_schedule='scaled_linear', num_train_timesteps=1000, clip_sample=False)
    torch.cuda.reset_peak_memory_stats(0)

    def one(case):
        image_path = Path(request['package_path']) / case['image_path']
        with Image.open(image_path) as image:
            if image.format != 'PNG' or image.mode != 'RGB' or image.size != (case['width'], case['height']):
                raise ValueError('validation_worker_image_changed')
            image.load()
            pixels = np.asarray(image, dtype=np.uint8).copy()
        if pixels.shape != (case['height'], case['width'], 3):
            raise ValueError('validation_worker_image_changed')
        image_tensor = torch.from_numpy(pixels).permute(2, 0, 1).unsqueeze(0).to(
            device='cuda', dtype=torch.float32).div(127.5).sub(1)
        caption = (Path(request['package_path']) / case['caption_path']).read_bytes().decode('utf-8')
        if not caption.endswith('\n') or '\n' in caption[:-1]:
            raise ValueError('validation_worker_caption_changed')
        ids = strategy.tokenize([caption[:-1]])
        ids = [part.to('cuda') for part in ids]
        generator = torch.Generator(device='cuda').manual_seed(case['seed'])
        with torch.inference_mode():
            hidden1, hidden2, pooled = encoding.encode_tokens(strategy,
                [models['encoder_1'], models['encoder_2']], ids)
            text = torch.cat([hidden1, hidden2], dim=2).to(dtype)
            sizes = sdxl_train_util.get_size_embeddings(
                torch.tensor([[case['height'], case['width']]]), torch.tensor([[0, 0]]),
                torch.tensor([[case['height'], case['width']]]), 'cuda').to(dtype)
            vector = torch.cat([pooled, sizes], dim=1).to(dtype)
            latents = models['vae'].encode(image_tensor).latent_dist.sample(generator=generator) * 0.13025
            if not torch.isfinite(latents).all().item():
                raise ValueError('validation_worker_latents_nonfinite')
            measured = []
            for timestep in case['timesteps']:
                noise = torch.randn(latents.shape, generator=generator, device='cuda', dtype=latents.dtype)
                steps = torch.tensor([timestep], device='cuda', dtype=torch.long)
                noisy = scheduler.add_noise(latents, noise, steps)
                prediction = models['unet'](noisy.to(dtype), steps, text, vector)
                mse = float((prediction.float() - noise.float()).square().mean().item())
                measured.append(mse)
            return measured

    values = evaluate_values(request, one)
    torch.cuda.synchronize(0)
    peak = int(torch.cuda.max_memory_allocated(0))
    if peak <= 0:
        raise ValueError('validation_worker_memory_unobserved')
    return values, peak


def run(path, *, native_runner=_native_values):
    root = Path(__file__).resolve().parents[1]
    # -S deliberately skips site initialization. Import application services only
    # after adding this exact process interpreter's already inventoried packages;
    # do not execute .pth files or fall back to another installed environment.
    site = Path(sys.executable).absolute().parent.parent / 'Lib' / 'site-packages'
    if not site.is_dir():
        raise ValueError('validation_worker_environment_unavailable')
    sys.path[:0] = [str(root), str(site)]
    from core import training_process as process
    from core import training_validation as validation
    path = Path(path).absolute()
    work = Path.cwd().absolute()
    if (work.name != 'work' or work.parent.parent != path.parent
            or tuple(sys.argv[1:]) != (str(path),)
            or not all((sys.flags.isolated, sys.flags.no_site,
                        sys.flags.dont_write_bytecode, sys.flags.utf8_mode))):
        raise ValueError('validation_worker_invocation_invalid')
    request, fingerprint, digest = _worker_request(path, work, process, validation)
    if request['qualification'] == 'product':
        profile = validation.qualified_profile()
        if profile is None or any(request['native'][key] != profile['native_evidence'][key]
                for key in ('source_revision', 'runtime_profile', 'source_inventory_sha256',
                            'environment_inventory_sha256', 'python_base_inventory_sha256')):
            raise ValueError('validation_worker_unqualified')
    _recheck_inputs(request, process)
    started = time.monotonic()
    values, peak = native_runner(request)
    mean = validation.validate_measurements(request, values)
    elapsed = time.monotonic() - started
    if not math.isfinite(elapsed) or elapsed <= 0 or type(peak) is not int or peak <= 0:
        raise ValueError('validation_worker_observation_invalid')
    _recheck_inputs(request, process)
    report = {'schema_version': 1, 'kind': 'sdxl_heldout_validation_result',
        'profile': validation.PROFILE, 'request_sha256': digest,
        'qualification': request['qualification'],
        'process_fingerprint': fingerprint, 'metric': 'epsilon_denoising_mse',
        'timesteps': list(validation.TIMESTEPS), 'values': values, 'mean_mse': mean,
        'elapsed_seconds': elapsed, 'peak_cuda_memory_bytes': peak}
    process._write_new(work / validation.RESULT_NAME, process._bytes(report))
    return report


if __name__ == '__main__':
    try:
        run(sys.argv[1])
    except BaseException:
        print('training_validation_failed', file=sys.stderr)
        raise SystemExit(2)
