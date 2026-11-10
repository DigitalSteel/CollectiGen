"""Explicit official SDXL extras around the previously qualified F16 loader."""
from __future__ import annotations

from . import training_sdxl_loading as loading
from .training_unet_step_contract import state_hash


POSITION_IDS = 'conditioner.embedders.0.transformer.text_model.embeddings.position_ids'
LOGIT_SCALE = 'conditioner.embedders.1.model.logit_scale'
EXTRA_SHAPES = {POSITION_IDS: [1, 77], LOGIT_SCALE: []}


def load_reference_models(module, torch, decoder, base, base_sha256, catalog):
    shapes = {**catalog['tensors'], **EXTRA_SHAPES}
    with loading.verified_file(base, base_sha256, shapes) as verified:
        state = loading.decode_state(verified, decoder)
        extras = {name: state.pop(name) for name in EXTRA_SHAPES}
        positions, scale = extras[POSITION_IDS], extras[LOGIT_SCALE]
        if (not torch.equal(positions, torch.arange(77, dtype=torch.float16).reshape(1, 77))
                or scale.ndim != 0 or not torch.isfinite(scale).item()):
            raise ValueError('reference_encoder_auxiliary_tensor_invalid')
        models = loading.assemble(module, torch, state)
        # Position IDs are a nonpersistent CLIP buffer in the pinned runtime.
        # The checkpoint stores F16 integers; preserve their exact index values.
        actual = models['encoder_1'].text_model.embeddings.position_ids
        if actual.dtype != torch.int64 or not torch.equal(actual.cpu(), positions.to(torch.int64)):
            raise ValueError('reference_encoder_position_ids_changed')
        evidence = {'profile': 'sdxl-reference-encoder-extras-v1',
            'tensor_state_sha256': state_hash(extras, torch),
            'position_ids_verified': True,
            'logit_scale': float(scale.item()),
            'logit_scale_use': 'retained_base_identity_not_used_by_sdxl_conditioning'}
    return models, evidence
