"""Explicit original-reference CLIP construction, without metadata dispatch.

Only the four named public-reference files in each fixed slot are read. The
caller owns their approved resource provenance and the injected native classes.
This module checks structural and construction behavior, never learned-model
compatibility, acquisition policy, or permission to launch training.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .training_tokenizer_files import (
    TOKENIZER_FILES, canonical, files_identity, layout, read_slot, strict_json,
)
from .training_tokenizer_contract import BOS, EOS, WIDTH, byte_alphabet


PROFILE = 'sdxl-reference-clip-added-token-v1'
VOCABULARY_SIZE = 49_408
MERGE_COUNT = VOCABULARY_SIZE - 512 - 2
SLOTS = ('tokenizer', 'tokenizer_2')
CONTENTS = {'bos_token': '<|startoftext|>', 'eos_token': '<|endoftext|>',
            'unk_token': '<|endoftext|>'}
TOKEN_OPTIONS = {'normalized': True, 'lstrip': False, 'rstrip': False, 'single_word': False}
OPTIONS = {'add_prefix_space': False, 'clean_up_tokenization_spaces': True,
           'do_lower_case': True, 'errors': 'replace', 'model_max_length': WIDTH}


def _expected_metadata(role):
    if type(role) is not int or role not in (1, 2):
        raise ValueError('reference_tokenizer_role_invalid')
    pad = '<|endoftext|>' if role == 1 else '!'
    specials = {name: {'content': content, **TOKEN_OPTIONS} for name, content in CONTENTS.items()}
    specials['pad_token'] = pad
    config = {'tokenizer_class': 'CLIPTokenizer', **OPTIONS, 'pad_token': pad,
              **{name: {'__type': 'AddedToken', **specials[name]} for name in CONTENTS}}
    return config, specials


def inspect_reference_slot(blobs, role):
    """Validate the original structured metadata and the complete CLIP closure.

    The finite vocabulary/merge algorithm mirrors the frozen component profile;
    keeping it here avoids importing that controller's Qt/preflight dependency
    graph and does not alter its narrower scalar-metadata admission contract.
    """
    layout(blobs)
    expected_config, expected_specials = _expected_metadata(role)
    if (canonical(strict_json(blobs['tokenizer_config.json'])) != canonical(expected_config)
            or canonical(strict_json(blobs['special_tokens_map.json'])) != canonical(expected_specials)):
        raise ValueError('reference_tokenizer_configuration_unsupported')
    vocab = strict_json(blobs['vocab.json'])
    alphabet = set(byte_alphabet().values())
    initial = alphabet | {symbol + '</w>' for symbol in alphabet}
    specials = {'<|startoftext|>', '<|endoftext|>'}
    if (len(vocab) != VOCABULARY_SIZE or set(vocab.values()) != set(range(VOCABULARY_SIZE))
            or not initial | specials <= vocab.keys()
            or (vocab.get('<|startoftext|>'), vocab.get('<|endoftext|>'), vocab.get('!')) != (BOS, EOS, 0)):
        raise ValueError('reference_tokenizer_vocabulary_unsupported')
    rows = blobs['merges.txt'].decode('utf-8').splitlines()
    if len(rows) != MERGE_COUNT + 1 or rows[0] != '#version: 0.2':
        raise ValueError('reference_tokenizer_merges_unsupported')
    available, ranks = set(initial), {}
    for rank, row in enumerate(rows[1:]):
        operands = row.split()
        if len(operands) != 2:
            raise ValueError('reference_tokenizer_merges_unsupported')
        left, right = operands
        result = left + right
        if left not in available or right not in available or result not in vocab or result in available:
            raise ValueError('reference_tokenizer_merges_unsupported')
        available.add(result)
        ranks[(left, right)] = rank
    if available | specials != vocab.keys():
        raise ValueError('reference_tokenizer_vocabulary_unsupported')
    return vocab, ranks


def _token_state(token):
    return {'content': getattr(token, 'content', None),
            **{key: getattr(token, key, None) for key in TOKEN_OPTIONS}}


def _check_constructed(tokenizer, vocab, ranks, role):
    expected_ids = (BOS, EOS, EOS, EOS if role == 1 else 0)
    ids = tuple(getattr(tokenizer, name, None) for name in
                ('bos_token_id', 'eos_token_id', 'unk_token_id', 'pad_token_id'))
    if (canonical(ids) != canonical(expected_ids)
            or type(tokenizer.model_max_length) is not int or tokenizer.model_max_length != WIDTH
            or type(tokenizer.vocab_size) is not int or tokenizer.vocab_size != VOCABULARY_SIZE
            or len(tokenizer) != VOCABULARY_SIZE
            or canonical(tokenizer.get_vocab()) != canonical(vocab)
            or tokenizer.bpe_ranks != ranks):
        raise ValueError('reference_tokenizer_constructed_inventory_changed')
    if tokenizer.errors != 'replace' or tokenizer.clean_up_tokenization_spaces is not True:
        raise ValueError('reference_tokenizer_constructed_options_changed')
    kwargs = getattr(tokenizer, 'init_kwargs', {})
    if type(kwargs) is not dict or any(canonical(kwargs.get(key)) != canonical(value) for key, value in OPTIONS.items()):
        raise ValueError('reference_tokenizer_constructed_options_changed')
    extended = tokenizer.special_tokens_map_extended
    if type(extended) is not dict or set(extended) != {*CONTENTS, 'pad_token'}:
        raise ValueError('reference_tokenizer_constructed_specials_changed')
    for name, content in CONTENTS.items():
        if canonical(_token_state(extended[name])) != canonical({'content': content, **TOKEN_OPTIONS}):
            raise ValueError('reference_tokenizer_constructed_specials_changed')
    if str(extended['pad_token']) != ('<|endoftext|>' if role == 1 else '!'):
        raise ValueError('reference_tokenizer_constructed_specials_changed')


def load_reference_tokenizers(tokenizer_class, added_token_class, resources_root):
    """Return ``((encoder_1_tokenizer, encoder_2_tokenizer), canonical_report)``.

    No from_pretrained/AutoTokenizer call, downloads, file rewrites, extra-file
    inspection, or tokenizer_class dispatch from metadata is permitted. Raw
    identities refer to original files, including their structured AddedTokens.
    The caller supplies trusted native classes in its separately owned runtime.
    """
    root = Path(resources_root)
    captured = []
    for role, slot in enumerate(SLOTS, 1):
        blobs = read_slot(root / slot)
        vocab, ranks = inspect_reference_slot(blobs, role)
        captured.append((blobs, vocab, ranks))
    tokenizers, rows = [], []
    for role, (slot, (blobs, vocab, ranks)) in enumerate(zip(SLOTS, captured), 1):
        tokens = {name: added_token_class(content, **TOKEN_OPTIONS) for name, content in CONTENTS.items()}
        if any(canonical(_token_state(tokens[name])) != canonical({'content': content, **TOKEN_OPTIONS})
               for name, content in CONTENTS.items()):
            raise ValueError('reference_added_token_behavior_changed')
        folder = root / slot
        tokenizer = tokenizer_class(vocab_file=str(folder / 'vocab.json'),
            merges_file=str(folder / 'merges.txt'), **tokens, **OPTIONS,
            pad_token='<|endoftext|>' if role == 1 else '!')
        _check_constructed(tokenizer, vocab, ranks, role)
        tokenizers.append(tokenizer)
        rows.append({'role': role, 'identity': files_identity(blobs),
            'files': [{'name': name, 'bytes': len(blobs[name]), 'sha256': hashlib.sha256(blobs[name]).hexdigest()}
                      for name, _ in TOKENIZER_FILES],
            'vocabulary_size': len(vocab), 'merge_count': len(ranks), 'model_max_length': WIDTH,
            'bos_token_id': BOS, 'eos_token_id': EOS, 'unk_token_id': EOS,
            'pad_token_id': EOS if role == 1 else 0})
    # Re-read BOTH roles after both constructors, including a constructor that
    # might alter the other slot. Reports never substitute normalized JSON bytes.
    for slot, (blobs, _, _) in zip(SLOTS, captured):
        if read_slot(root / slot) != blobs:
            raise ValueError('reference_tokenizer_files_changed')
    report = {'schema_version': 1, 'kind': 'sdxl_reference_tokenizer_construction',
              'profile': PROFILE, 'slots': rows}
    return tuple(tokenizers), strict_json(canonical(report))
