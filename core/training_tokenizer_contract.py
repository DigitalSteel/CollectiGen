"""Generated-only SDXL caption experiment, shared with the isolated runtime.

Standard library only. No selected paths or user captions are accepted. The
oracle uses a deliberately merge-free byte vocabulary; it cannot qualify a
real checkpoint's vocabulary, encoder pairing, weighted captions or quality.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


BOS, EOS, WIDTH = 49406, 49407, 77
LIMITS = (75, 150, 225)
FILES = ('vocab.json', 'merges.txt', 'tokenizer_config.json', 'special_tokens_map.json')
PROFILE = 'sdxl-generated-clip-bytes-v1'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def sha(value):
    return hashlib.sha256(value).hexdigest()


def byte_alphabet():
    # CLIP's reversible byte spelling, independent of the selected library.
    visible = list(range(33, 127)) + list(range(161, 173)) + list(range(174, 256))
    result = {number: chr(number) for number in visible}
    for number in range(256):
        if number not in result:
            result[number] = chr(256 + sum(key < number and key not in visible for key in range(256)))
    return result


def fixture_files(role):
    if type(role) is not int or role not in (1, 2):
        raise ValueError('unsupported_tokenizer_role')
    alphabet = byte_alphabet()
    # Different IDs make accidentally using encoder 1 for both roles visible.
    offset = (role - 1) * 1000 + 1
    vocab = {text + suffix: offset + byte * 2 + ending
             for byte, text in alphabet.items() for ending, suffix in enumerate(('', '</w>'))}
    vocab.update({'<synthetic-pad>': 0, '<|startoftext|>': BOS, '<|endoftext|>': EOS})
    specials = {'bos_token': '<|startoftext|>', 'eos_token': '<|endoftext|>',
                'unk_token': '<|endoftext|>', 'pad_token': '<|endoftext|>'}
    return {'vocab.json': canonical(vocab), 'merges.txt': b'#version: 0.2\n',
            'tokenizer_config.json': canonical({'tokenizer_class': 'CLIPTokenizer',
                                                'model_max_length': WIDTH, **specials}),
            'special_tokens_map.json': canonical(specials)}


def fixture_identity(blobs):
    return 'clip-files-v1:' + sha(canonical({'format': 'clip-files-v1', 'files': [
        {'name': name, 'bytes': len(blobs[name]), 'sha256': sha(blobs[name])} for name in FILES]}))


def cases():
    # Expected lexical pieces are declared, not inferred by another tokenizer.
    values = [('empty', '', ()), ('whitespace', ' \t\n ', ()),
              ('case_and_spacing', 'A  a\tA', ('a',) * 3),
              ('unicode', 'café', ('café',)),
              ('literal_parentheses', '(a)', ('(', 'a', ')'))]
    values.extend((f'words_{count}', ' '.join(['a'] * count), ('a',) * count)
                  for count in (74, 75, 76, 149, 150, 151, 224, 225, 226))
    return tuple(values)


def expected_payload(role, pieces):
    offset = (role - 1) * 1000 + 1
    return [offset + byte * 2 + int(index == len(encoded) - 1)
            for piece in pieces for encoded in (piece.encode('utf-8'),)
            for index, byte in enumerate(encoded)]


def expected_chunks(role, payload, limit):
    """Model complete caption windows, including the pinned encoder-2 tail.

    Short captions put EOS inside a window. A full window needs an appended
    EOS; unused encoder-2 windows start BOS/EOS and otherwise contain pad 0.
    The pinned strategy copies the global terminal token to each window, so an
    exactly full overall caption can also leave EOS at unused window tails.
    """
    if role not in (1, 2) or limit not in LIMITS:
        raise ValueError('unsupported_caption_window')
    retained = payload[:limit]
    pad = EOS if role == 1 else 0
    padded = retained + [EOS] + [pad] * (limit - len(retained))
    windows = []
    for start in range(0, limit, WIDTH - 2):
        middle = padded[start:start + WIDTH - 2]
        tail = padded[-1]
        if role == 2 and limit > 75:
            if middle[-1] not in (EOS, pad):
                tail = EOS
            if middle[0] == pad:
                middle[0] = EOS
        windows.append([BOS, *middle, tail])
    return windows


def expected_report():
    result = {'schema_version': 1, 'kind': 'sdxl_synthetic_tokenizer_check',
              'profile': PROFILE, 'case_set_sha256': sha(canonical(cases())),
              'scope': 'generated_local_files_and_unweighted_captions',
              'fixtures': [], 'checks': [], 'selected_components_verified': False,
              'offline_execution_verified': False, 'execution_ready': False}
    for role in (1, 2):
        result['fixtures'].append({'role': role, 'identity': fixture_identity(fixture_files(role))})
        for limit in LIMITS:
            rows = []
            for name, _, pieces in cases():
                payload = expected_payload(role, pieces)
                rows.append({'case': name, 'payload_tokens': len(payload),
                             'truncated_tokens': max(0, len(payload) - limit),
                             'output_sha256': sha(canonical(expected_chunks(role, payload, limit)))})
            result['checks'].append({'role': role, 'payload_limit': limit,
                                     'shape': [len(rows), limit // 75, WIDTH], 'cases': rows})
    return result


def validate_report(value):
    # Compare canonical bytes as well as values to reject bool/int substitutions,
    # unknown fields, role swaps and plausible but changed output digests.
    if canonical(value) != canonical(expected_report()):
        raise ValueError('tokenizer_evidence_invalid')
    return value


def probe_generated_tokenizers(tokenizer_class, strategy_class, work):
    """Exercise the real known classes without their remote/cache loader.

    Every input is generated below. Do not add a selected path or caption
    parameter here: real component selection needs its own authority/stale gate.
    """
    root = Path(work) / 'synthetic-tokenizers'
    root.mkdir()
    tokenizers, identities = [], []
    for role in (1, 2):
        folder = root / str(role)
        folder.mkdir()
        blobs = fixture_files(role)
        for name, raw in blobs.items():
            with (folder / name).open('xb') as handle:
                handle.write(raw)
        # Constructor-only loading: no AutoTokenizer, from_pretrained, hub IDs,
        # directory discovery, dynamic config imports or cache fallback.
        tokenizer = tokenizer_class(vocab_file=str(folder / 'vocab.json'),
                                    merges_file=str(folder / 'merges.txt'),
                                    model_max_length=WIDTH,
                                    **json.loads(blobs['special_tokens_map.json']))
        if (tokenizer.bos_token_id, tokenizer.eos_token_id,
                tokenizer.model_max_length, tokenizer.pad_token_id) != (BOS, EOS, WIDTH, EOS):
            raise ValueError('tokenizer_special_tokens_changed')
        if role == 2:
            tokenizer.pad_token_id = 0  # Exact pinned SDXL role, not a user override.
        tokenizers.append(tokenizer)
        identities.append(fixture_identity(blobs))
    observed = expected_report()
    captions = [caption for _, caption, _ in cases()]
    for limit in LIMITS:
        # Bypass only __init__'s known remote-capable _load_tokenizer. Execute
        # the unchanged pinned tokenize / _get_input_ids implementations.
        strategy = strategy_class.__new__(strategy_class)
        strategy.tokenizer1, strategy.tokenizer2 = tokenizers
        strategy.max_length = limit + 2
        actual = strategy.tokenize(captions)
        if type(actual) is not tuple or len(actual) != 2:
            raise ValueError('tokenizer_roles_changed')
        for role, tensor in enumerate(actual, 1):
            row = next(row for row in observed['checks'] if row['role'] == role and row['payload_limit'] == limit)
            if list(tensor.shape) != row['shape']:
                raise ValueError('tokenizer_shape_changed')
            values = tensor.tolist()
            for measured, (name, caption, pieces), entry in zip(values, cases(), row['cases']):
                raw_ids = tokenizers[role - 1].encode(caption, add_special_tokens=False, truncation=False)
                if raw_ids != expected_payload(role, pieces) or canonical(measured) != canonical(expected_chunks(role, raw_ids, limit)):
                    raise ValueError('tokenizer_caption_changed')
                entry['output_sha256'] = sha(canonical(measured))
    for role in (1, 2):
        # Exact read-back after all uses; generated fixture mutation is a hold.
        blobs = {}
        for name, original in fixture_files(role).items():
            with (root / str(role) / name).open('rb') as handle:
                blobs[name] = handle.read(len(original) + 1)
        if fixture_identity(blobs) != identities[role - 1]:
            raise ValueError('tokenizer_fixture_changed')
    return validate_report(observed)
