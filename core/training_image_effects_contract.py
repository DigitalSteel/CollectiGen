"""Generated-only qualification of pinned DreamBooth dataset pixel/text effects.

Imports stay standard-library-only; the contained child supplies public runtime
modules. This experiment neither accepts user data nor authorizes training.
"""
from __future__ import annotations

from collections import Counter
from functools import lru_cache
import hashlib
import json
import struct
import zlib

PROFILE = 'generated-dreambooth-image-effects-v1'
SUBSET = dict(alpha_mask=False, num_repeats=1, shuffle_caption=False,
    caption_separator=',', keep_tokens=0, keep_tokens_separator=None,
    secondary_separator=None, enable_wildcard=False, color_aug=False, flip_aug=False,
    face_crop_aug_range=None, random_crop=False, caption_dropout_rate=0.0,
    caption_dropout_every_n_epochs=0, caption_tag_dropout_rate=0.0,
    caption_prefix=None, caption_suffix=None, token_warmup_min=1, token_warmup_step=0,
    custom_attributes=None, validation_seed=0, validation_split=0.0,
    resize_interpolation=None, is_reg=False, class_tokens=None,
    caption_extension='.txt', cache_info=False)
DATASET = dict(is_training_dataset=True, batch_size=1, resolution=(1024, 1024),
    network_multiplier=1.0, enable_bucket=True, min_bucket_reso=256,
    max_bucket_reso=1024, bucket_reso_steps=64, bucket_no_upscale=True,
    prior_loss_weight=1.0, train_inpainting=False, debug_dataset=True,
    validation_split=0.0, validation_seed=0, resize_interpolation=None)
# name, input WH, resized WH, output WH, actual removed LTRB, SDXL conditioning HW
GEOMETRY = (
    ('aligned', (1024, 1024), (1024, 1024), (1024, 1024), (0, 0, 0, 0), (0, 0)),
    ('small', (512, 768), (512, 768), (512, 768), (0, 0, 0, 0), (0, 0)),
    ('oddlandscape', (777, 513), (777, 513), (768, 512), (4, 0, 5, 1), (2, 0)),
    ('oddportrait', (513, 777), (513, 777), (512, 768), (0, 4, 1, 5), (0, 2)),
    ('downscale', (2048, 2048), (1024, 1024), (1024, 1024), (0, 0, 0, 0), (0, 0)),
    ('upscale', (512, 512), (1024, 1024), (1024, 1024), (0, 0, 0, 0), (0, 0)),
    ('square', (1024, 1024), (1024, 1024), (1024, 1024), (0, 0, 0, 0), (0, 0)),
)
CAPTIONS = (
    ('literal', 'tag, café, (detail:1.2)', 'tag, café, (detail:1.2)'),
    ('spaces', '  café, tag  \n', 'café, tag'),
    ('multiline', 'first\nsecond', 'first'),
    ('crlf', 'first\r\nsecond\r\n', 'first'),
    ('bom', '\ufefffirst', '\ufefffirst'),
    ('wildcard', '{red|blue}, {{literal}}', '{red|blue}, {{literal}}'),
    ('blank', '\n', ''),
    ('missing', None, ''),
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':')).encode()


def sha(value): return hashlib.sha256(value).hexdigest()


def pixels(width, height, *, solid=False):
    """A coordinate pattern constant in 2x2 blocks gives an exact area oracle."""
    if solid: return bytes((64, 128, 192)) * width * height
    rows = [bytes(channel for x in range(width) for channel in
        ((x // 2) % 251, y % 251, ((x // 2) // 7 + y // 11) % 251)) for y in range((height + 1) // 2)]
    return b''.join(rows[y // 2] for y in range(height))


def png(width, height, raw, *, alpha=False):
    def chunk(kind, value):
        return struct.pack('>I', len(value)) + kind + value + struct.pack('>I', zlib.crc32(kind + value))
    stride = width * (4 if alpha else 3)
    scan = b''.join(b'\0' + raw[y * stride:(y + 1) * stride] for y in range(height))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6 if alpha else 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(scan)) + chunk(b'IEND', b'')


@lru_cache(maxsize=1)
def _expected():
    rows = [{'case': name, 'input_wh': size, 'resized_wh': resized, 'output_wh': output,
             'removed_ltrb': crop, 'conditioning_crop_hw': conditioning}
            for name, size, resized, output, crop, conditioning in GEOMETRY]
    return {'schema_version': 1, 'profile': PROFILE, 'geometry_cases': len(GEOMETRY),
        'geometry_sha256': sha(canonical(rows)), 'caption_cases': len(CAPTIONS),
        'caption_sha256': sha(canonical([(name, effective) for name, _, effective in CAPTIONS])),
        'negative_cases': ['empty_caption', 'square_small', 'square_large'],
        'training_images': 3, 'repeats': 3, 'batch_size': 2, 'batches': 5,
        'exposures': 9, 'partial_batch_size': 1, 'excluded_role_images': 2,
        'pixel_scope': 'all_RGB_values_and_float32_normalization',
        'pairing_scope': 'per_image_key_RGB_and_caption_in_every_batch',
        'alpha_case': 'RGBA_discarded_no_mask',
        'tokenizer_scope': 'capture_pre_tokenizer_text_only',
        'generated_dataset_effects_verified': True, 'selected_dataset_verified': False,
        'model_compatibility_verified': False, 'offline_execution_verified': False,
        'execution_ready': False}


def expected_report(): return json.loads(canonical(_expected()))


def validate_report(value):
    if canonical(value) != canonical(expected_report()):
        raise ValueError('image_effects_report_changed')
    return value


def probe_generated_images(dataset_module, subset_module, dreambooth_module, torch, np, work):
    """Actual dataset constructor/buckets/batch path; generated RGB PNG/TXT only."""
    torch.set_num_threads(2)
    root = work / 'generated-image-effects'
    root.mkdir()
    snapshots = {}
    def write(path, value):
        with path.open('xb') as handle: handle.write(value)
        snapshots[path] = sha(value)
    def pair(directory, name, size, caption, *, solid=False, shift=0):
        directory.mkdir(exist_ok=True)
        raw = pixels(*size, solid=solid)
        if shift: raw = raw.translate(bytes((value + shift) % 256 for value in range(256)))
        path = directory / (name + '.png')
        write(path, png(*size, raw))
        if caption is not None: write(path.with_suffix('.txt'), caption.encode('utf-8'))
        return path, np.frombuffer(raw, dtype=np.uint8).reshape(size[1], size[0], 3)

    class CaptureText:
        def __init__(self): self.captions = []
        def tokenize(self, caption):
            self.captions.append(caption)
            # A sentinel only. No tokenizer vocabulary or encoding is tested.
            return [torch.tensor([[1, 2]], dtype=torch.int64)]

    def dataset(directory, *, repeats=1, batch=1, bucket=True, no_upscale=True):
        if dataset_module.LatentsCachingStrategy.get_strategy() is not None:
            raise ValueError('image_effects_unexpected_cache_strategy')
        subset = subset_module.DreamBoothSubset(image_dir=str(directory), **{**SUBSET, 'num_repeats': repeats})
        value = dreambooth_module.DreamBoothDataset([subset], **{**DATASET, 'batch_size': batch,
            'enable_bucket': bucket, 'bucket_no_upscale': no_upscale})
        value.tokenize_strategy = CaptureText()
        value.make_buckets()
        value.verify_bucket_reso_steps(32)
        return value

    def normalized(expected):
        return torch.from_numpy(expected.copy()).permute(2, 0, 1).float().div(255).sub(.5).div(.5)

    def check_batch(value, expected):
        image = value['images']
        wanted = normalized(expected)
        if image.device.type != 'cpu' or image.dtype != torch.float32 or not torch.equal(image, wanted.unsqueeze(0)):
            raise ValueError('image_effects_pixels_changed')
        if value['latents'] is not None or value['text_encoder_outputs_list'] is not None or value['alpha_masks'] is not None or value['flippeds'] != [False]:
            raise ValueError('image_effects_unexpected_processing')

    observed = []
    for name, size, resized, output, crop, conditioning in GEOMETRY:
        directory = root / name
        path, raw = pair(directory, 'image', size, 'literal, café', solid=name == 'upscale')
        value = dataset(directory, bucket=name != 'square', no_upscale=name != 'upscale')
        info = value.image_data[str(path)]
        if tuple(info.resized_size) != resized or tuple(info.bucket_reso) != output:
            raise ValueError('image_effects_geometry_changed')
        batch = value[0]
        if name == 'downscale': expected = raw[::2, ::2]
        elif name == 'upscale': expected = np.full((1024, 1024, 3), (64, 128, 192), dtype=np.uint8)
        else: expected = raw[crop[1]:size[1] - crop[3], crop[0]:size[0] - crop[2]]
        check_batch(batch, expected)
        if batch['captions'] != ['literal, café'] or value.tokenize_strategy.captions != ['literal, café']:
            raise ValueError('image_effects_caption_pair_changed')
        if batch['original_sizes_hw'].tolist() != [[size[1], size[0]]] or batch['target_sizes_hw'].tolist() != [[output[1], output[0]]] or batch['crop_top_lefts'].tolist() != [list(conditioning)]:
            raise ValueError('image_effects_conditioning_changed')
        observed.append({'case': name, 'input_wh': size, 'resized_wh': resized, 'output_wh': output,
            'removed_ltrb': crop, 'conditioning_crop_hw': conditioning})
    if sha(canonical(observed)) != expected_report()['geometry_sha256']:
        raise ValueError('image_effects_geometry_report_changed')

    directory = root / 'alpha'
    directory.mkdir()
    rgb = np.frombuffer(pixels(256, 256), dtype=np.uint8).reshape(256, 256, 3)
    rgba = np.concatenate((rgb, np.arange(256, dtype=np.uint8).reshape(256, 1, 1).repeat(256, axis=1)), axis=2)
    write(directory / 'image.png', png(256, 256, rgba.tobytes(), alpha=True))
    write(directory / 'image.txt', b'alpha\n')
    check_batch(dataset(directory)[0], rgb)

    captions = []
    for name, original, effective in CAPTIONS:
        directory = root / ('caption-' + name)
        _, raw = pair(directory, 'image', (256, 256), original)
        value = dataset(directory)
        batch = value[0]
        check_batch(batch, raw)
        if batch['captions'] != [effective] or value.tokenize_strategy.captions != [effective]:
            raise ValueError('image_effects_text_changed')
        captions.append((name, effective))
    if sha(canonical(captions)) != expected_report()['caption_sha256']:
        raise ValueError('image_effects_caption_report_changed')

    for name, size, caption, bucket, reason in (('empty_caption', (256, 256), '', True, 'caption file is empty'),
        ('square_small', (512, 512), 'literal', False, 'image size is small'),
        ('square_large', (2048, 1024), 'literal', False, 'image too large')):
        directory = root / name
        pair(directory, 'image', size, caption)
        try: dataset(directory, bucket=bucket)[0]
        except AssertionError as error:
            if not str(error).startswith(reason):
                raise ValueError('image_effects_unexpected_refusal') from None
        else: raise ValueError('image_effects_expected_refusal_missing')

    directory = root / 'roles-training'
    pairs = {}
    for index in range(3):
        caption = 'training ' + str(index)
        path, raw = pair(directory, 'image' + str(index), (256, 256), caption, shift=index * 17)
        pairs[str(path)] = (caption, normalized(raw))
    for role in ('validation', 'holdout'): pair(root / ('roles-' + role), 'image', (256, 256), role)
    value = dataset(directory, repeats=3, batch=2)
    exposures, sizes = [], []
    for index in range(len(value)):
        batch = value[index]
        exposures.extend(batch['captions'])
        sizes.append(len(batch['captions']))
        if batch['images'].shape[0] != len(batch['captions']) or batch['loss_weights'].tolist() != [1.0] * len(batch['captions']):
            raise ValueError('image_effects_batch_changed')
        if len(batch['image_keys']) != len(batch['captions']):
            raise ValueError('image_effects_batch_keys_changed')
        for key, caption, tensor in zip(batch['image_keys'], batch['captions'], batch['images']):
            if key not in pairs or caption != pairs[key][0] or not torch.equal(tensor, pairs[key][1]):
                raise ValueError('image_effects_batch_pair_changed')
    if Counter(exposures) != Counter({'training ' + str(index): 3 for index in range(3)}) or sorted(sizes) != [1, 2, 2, 2, 2]:
        raise ValueError('image_effects_exposures_changed')
    if len(value.image_data) != 3 or value.num_train_images != 9 or value.num_reg_images != 0:
        raise ValueError('image_effects_role_changed')
    if value.tokenize_strategy.captions != exposures:
        raise ValueError('image_effects_tokenizer_pair_changed')
    # Generated source pixels/captions remain exact; trainer caches and extra
    # artifacts are not accepted even if the dataset otherwise returns a batch.
    if {p for p in root.rglob('*') if p.is_file()} != set(snapshots):
        raise ValueError('image_effects_unexpected_artifact')
    if any(sha(path.read_bytes()) != fingerprint for path, fingerprint in snapshots.items()):
        raise ValueError('image_effects_source_changed')
    return expected_report()
