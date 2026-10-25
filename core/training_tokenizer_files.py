"""Shared bounded four-file CLIP evidence reader; no loader or directory scan."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat

from .training_source_io import _assert_unredirected, _identity

TOKENIZER_FILES = (('vocab.json', 8 * 1024 * 1024), ('merges.txt', 4 * 1024 * 1024),
                   ('tokenizer_config.json', 256 * 1024), ('special_tokens_map.json', 256 * 1024))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate_tokenizer_key')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('nonfinite_tokenizer_value')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def read_file(path, limit):
    _assert_unredirected(path)
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit or int(getattr(before, 'st_file_attributes', 0)) & (0x1000 | 0x40000 | 0x400000):
        raise ValueError('unsupported_tokenizer_file')
    with path.open('rb') as handle:
        if _identity(os.fstat(handle.fileno())) != _identity(before):
            raise ValueError('tokenizer_changed_before_read')
        raw = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
    _assert_unredirected(path)
    if len(raw) != before.st_size or _identity(before) != _identity(after) or _identity(path.stat(follow_symlinks=False)) != _identity(before):
        raise ValueError('tokenizer_changed_during_read')
    return raw, _identity(before)


def layout(blobs):
    vocab = strict_json(blobs['vocab.json'])
    if type(vocab) is not dict or not 1 <= len(vocab) <= 100000 or any(not key or len(key) > 512 or type(value) is not int or not 0 <= value < 2 ** 31 for key, value in vocab.items()) or len(set(vocab.values())) != len(vocab):
        raise ValueError('unsupported_tokenizer_vocabulary')
    merges = blobs['merges.txt'].decode('utf-8').strip().splitlines()
    if not merges or merges[0] != '#version: 0.2' or len(merges) > 100001 or any(len(line.split()) != 2 for line in merges[1:]):
        raise ValueError('unsupported_tokenizer_merges')
    for name in ('tokenizer_config.json', 'special_tokens_map.json'):
        if type(strict_json(blobs[name])) is not dict:
            raise ValueError('unsupported_tokenizer_metadata')


def files_identity(blobs):
    rows = [{'name': name, 'bytes': len(blobs[name]), 'sha256': hashlib.sha256(blobs[name]).hexdigest()}
            for name, _ in TOKENIZER_FILES]
    return 'clip-files-v1:' + hashlib.sha256(canonical({'format': 'clip-files-v1', 'files': rows})).hexdigest()


def read_slot(folder):
    folder = Path(folder)
    _assert_unredirected(folder)
    state = folder.stat(follow_symlinks=False)
    if not stat.S_ISDIR(state.st_mode) or int(getattr(state, 'st_file_attributes', 0)) & (0x1000 | 0x40000 | 0x400000):
        raise ValueError('unsupported_tokenizer_directory')
    blobs = {name: read_file(folder / name, limit)[0] for name, limit in TOKENIZER_FILES}
    layout(blobs)
    return blobs
