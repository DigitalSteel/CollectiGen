"""Streaming checks for a verified SDXL package used by the owned trainer.

The package remains immutable. Validate one bounded batch at a time instead of
imposing the earlier diagnostic's whole-dataset pixel or byte budget.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path, PurePosixPath

from .training_configuration import TrainingConfiguration
from .training_process import _read

MAX_IMAGES = 50000
MAX_PIXELS = 1024 * 1024
MAX_CAPTION_BYTES = 1024 * 1024


def package_pairs(package, manifest):
    package = Path(package).absolute()
    items = manifest['package_plan']['items']
    if type(items) is not list or not 1 <= len(items) <= MAX_IMAGES:
        raise ValueError('training_dataset_scope_invalid')
    artifacts = {row['path']: row for row in manifest['artifacts']}
    pairs = {}
    for item in items:
        if item['role'] != 'training':
            continue
        for field in ('image_path', 'caption_path'):
            name = item[field]
            if (type(name) is not str or not name or '\\' in name or ':' in name
                    or PurePosixPath(name).is_absolute()
                    or any(part in ('', '.', '..') for part in name.split('/'))):
                raise ValueError('training_dataset_path_invalid')
        image, caption = artifacts[item['image_path']], artifacts[item['caption_path']]
        width, height = image['transform']['output_dimensions']
        if (type(width) is not int or type(height) is not int
                or min(width, height) < 64 or width % 64 or height % 64
                or width * height > MAX_PIXELS):
            raise ValueError('training_dataset_geometry_unsupported')
        path = str(package / item['image_path'])
        if path in pairs:
            raise ValueError('training_dataset_duplicate_path')
        pairs[path] = {'item': item, 'image': image, 'caption': caption}
    if not pairs:
        raise ValueError('training_dataset_empty')
    return pairs


class DatasetAudit:
    """One zero-worker dataset group, preserving loader order and exact captions."""
    def __init__(self, group, package, manifest, torch, np, Image):
        self.group, self.package = group, Path(package).absolute()
        self.torch, self.np, self.Image = torch, np, Image
        self.pairs = package_pairs(package, manifest)
        self.config = TrainingConfiguration.from_dict(manifest['training_configuration'])
        self.loaded = []
        if len(group.datasets) != 1:
            raise ValueError('training_dataset_groups_changed')
        self.dataset = group.datasets[0]
        if (set(self.dataset.image_data) != set(self.pairs)
                or self.dataset.num_train_images != len(self.pairs) * self.config.repeats
                or self.dataset.num_reg_images != 0):
            raise ValueError('training_dataset_membership_changed')
        original_loader = self.dataset.load_image_with_face_info
        def load(subset, path, alpha_mask=False):
            if path not in self.pairs or alpha_mask:
                raise ValueError('training_dataset_loader_scope_changed')
            self.loaded.append(path)
            return original_loader(subset, path, alpha_mask)
        self.dataset.load_image_with_face_info = load
        self.expected_pixels = lru_cache(maxsize=2)(self._pixels)

    def __len__(self): return len(self.group)
    def __getattr__(self, name): return getattr(self.group, name)

    def _pixels(self, path):
        width, height = self.pairs[path]['image']['transform']['output_dimensions']
        with self.Image.open(path) as image:
            if (image.size != (width, height) or image.mode != 'RGB' or image.format != 'PNG'
                    or getattr(image, 'n_frames', 1) != 1 or image.info.get('icc_profile')
                    or 'transparency' in image.info):
                raise ValueError('training_dataset_pixels_unsupported')
            image.load()
            pixels = self.np.array(image)
        if pixels.shape != (height, width, 3) or pixels.dtype != self.np.uint8:
            raise ValueError('training_dataset_pixels_changed')
        return self.torch.from_numpy(pixels).permute(2, 0, 1).float().div(255).sub(.5).div(.5)

    def __getitem__(self, index):
        self.loaded.clear()
        batch = self.group[index]
        texts, images = batch['captions'], batch['images']
        if (len(self.loaded) != len(texts) or not 1 <= len(texts) <= self.config.batch_size
                or images.shape[0] != len(texts) or images.device.type != 'cpu'
                or images.dtype != self.torch.float32
                or batch['latents'] is not None or batch['text_encoder_outputs_list'] is not None
                or batch['alpha_masks'] is not None or batch['flippeds'] != [False] * len(texts)
                or batch['loss_weights'].tolist() != [1.0] * len(texts)):
            raise ValueError('training_dataset_processing_changed')
        for position, (path, text) in enumerate(zip(self.loaded, texts)):
            pair = self.pairs[path]
            width, height = pair['image']['transform']['output_dimensions']
            info = self.dataset.image_data[path]
            if (text != pair['caption']['package_caption']
                    or _read(self.package / pair['item']['caption_path'], limit=MAX_CAPTION_BYTES)
                        != (text + '\n').encode('utf-8')):
                raise ValueError('training_dataset_caption_changed')
            if (tuple(info.resized_size) != (width, height) or tuple(info.bucket_reso) != (width, height)
                    or batch['original_sizes_hw'][position].tolist() != [height, width]
                    or batch['target_sizes_hw'][position].tolist() != [height, width]
                    or batch['crop_top_lefts'][position].tolist() != [0, 0]
                    or not self.torch.equal(images[position], self.expected_pixels(path))):
                raise ValueError('training_dataset_pixels_changed')
        return batch
