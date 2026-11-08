"""Finite generated-model result and bounded owned-file native barrier."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

from .training_source_io import _assert_unredirected, _identity
from .training_model_contract import canonical, SOURCE_REVISION
from .training_model_loading_contract import expected_report as model_report

PROFILE = 'generated-sdxl-appcontainer-loading-v1'
LPAC_PROFILE = 'generated-sdxl-lpac-loading-v1'
REGISTRY_PROFILE = 'generated-sdxl-lpac-registry-read-loading-v1'
WFP_PROFILE = 'generated-sdxl-lpac-wfp-loading-v1'
MAX_PACKET = 32 * 1024
FALSE_FLAGS = ('selected_model_weights_loaded','model_compatibility_verified','forward_pass_verified',
               'offline_execution_verified','runtime_code_verified','execution_ready')


def read_packet(path):
    path = Path(path)
    _assert_unredirected(path)
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= MAX_PACKET:
        raise ValueError('restricted_packet_bound')
    with path.open('rb') as handle:
        if _identity(os.fstat(handle.fileno())) != _identity(before):
            raise ValueError('restricted_packet_changed')
        raw = handle.read(MAX_PACKET + 1)
        if _identity(os.fstat(handle.fileno())) != _identity(before):
            raise ValueError('restricted_packet_changed')
    _assert_unredirected(path)
    if _identity(path.stat(follow_symlinks=False)) != _identity(before):
        raise ValueError('restricted_packet_changed')
    value = json.loads(raw)
    if canonical(value) != raw:
        raise ValueError('restricted_packet_noncanonical')
    return value


def write_packet(path, value):
    path = Path(path); raw = canonical(value)
    if len(raw) > MAX_PACKET:
        raise ValueError('restricted_packet_bound')
    _assert_unredirected(path.parent)
    pending = path.with_name(path.name + '.pending')
    with pending.open('xb') as handle:
        if handle.write(raw) != len(raw): raise ValueError('restricted_packet_short_write')
        handle.flush(); os.fsync(handle.fileno())
    # Windows rename refuses an existing target; never replace a packet.
    if os.name != 'nt' or path.exists(): raise ValueError('restricted_packet_exists_or_unsupported')
    os.rename(pending, path)
    if read_packet(path) != value: raise ValueError('restricted_packet_readback')


class FileBarrier:
    """Owned work files only; no cross-token mapping or broker handle grant."""
    def __init__(self, work): self.work = Path(work)
    def path(self, index):
        if type(index) is not int or index not in (0, 1): raise ValueError('restricted_barrier_slot')
        return self.work / ('native-ready.json' if index == 0 else 'native-ack.json')
    def read(self, index):
        try: return read_packet(self.path(index))
        except FileNotFoundError: return None
    def write(self, index, value): write_packet(self.path(index), value)
    def close(self): pass


def validate_request(value):
    fields = {'schema_version','profile','nonce','source_revision','source','runtime_roots',
              'model_inputs','code_bundle','catalog','guard','staging_binding_sha256'}
    if type(value) is not dict or set(value) != fields or type(value['schema_version']) is not int or value['schema_version'] != 1 or value['profile'] != PROFILE:
        raise ValueError('restricted_request_schema')
    if type(value['nonce']) is not str or not re.fullmatch('[0-9a-f]{32}', value['nonce']): raise ValueError('restricted_nonce')
    if type(value['staging_binding_sha256']) is not str or not re.fullmatch('[0-9a-f]{64}', value['staging_binding_sha256']): raise ValueError('restricted_staging_binding')
    if value['source_revision'] != SOURCE_REVISION: raise ValueError('restricted_source_revision')
    roots = value['runtime_roots']
    if type(roots) is not dict or set(roots) != {'environment','python_base'}: raise ValueError('restricted_runtime_scope')
    for path in [*(value[name] for name in ('source','model_inputs','code_bundle','catalog','guard')), *roots.values()]:
        if type(path) is not str or not Path(path).is_absolute() or len(path) > 32767: raise ValueError('restricted_path')
    return value


def expected_checks():
    return {'model_loading':model_report(), 'staged_interpreter_verified':True,
            'read_only_model_write_denied':True, 'runtime_write_denied':True}


def validate_checks(value):
    if canonical(value) != canonical(expected_checks()): raise ValueError('restricted_loading_checks')
    return value


def validate_report(value, *, expected, token):
    """Require independently verified parent evidence, never a self-issued hash.

    The caller obtains expected from its process, helper, native and profile
    receipts. This finite result has no selected-resource or launch authority.
    """
    fields = {'schema_version','profile','status','generated_model_loading_in_appcontainer_verified',
        'staging','native_observation','native_observation_sha256','checks','process_fingerprint',
        'journal_tip','input_verification_sha256','appcontainer_created_sha256',
        'appcontainer_deleted_sha256','tokens','root_token_checks','helper_receipt_sha256',*FALSE_FLAGS}
    registry = type(value) is dict and type(value.get('schema_version')) is int and value['schema_version'] == 3
    wfp = type(value) is dict and type(value.get('schema_version')) is int and value['schema_version'] == 4
    lpac = wfp or registry or (type(value) is dict and type(value.get('schema_version')) is int and value['schema_version'] == 2)
    if lpac:
        fields.add('least_privileged_policy')
    if type(value) is not dict or set(value) != fields or canonical(value) != canonical(expected):
        raise ValueError('restricted_report_binding')
    if type(value['schema_version']) is not int or value['schema_version'] != (4 if wfp else 3 if registry else 2 if lpac else 1) or value['profile'] != (WFP_PROFILE if wfp else REGISTRY_PROFILE if registry else LPAC_PROFILE if lpac else PROFILE) or value['status'] != 'generated_restricted_loading_checked' or value['generated_model_loading_in_appcontainer_verified'] is not True or any(value[name] is not False for name in FALSE_FLAGS):
        raise ValueError('restricted_report_authority')
    validate_checks(value['checks'])
    for key in ('native_observation_sha256','process_fingerprint','journal_tip',
                'appcontainer_created_sha256','appcontainer_deleted_sha256'):
        if type(value[key]) is not str or not re.fullmatch('[0-9a-f]{64}',value[key]):
            raise ValueError('restricted_report_hash')
    for key,count in (('input_verification_sha256',3),('helper_receipt_sha256',4)):
        rows = value[key]
        if type(rows) is not list or len(rows) != count or len(set(rows)) != count or any(type(row) is not str or not re.fullmatch('[0-9a-f]{64}',row) for row in rows):
            raise ValueError('restricted_report_receipts')
    tokens, roots = value['tokens'], value['root_token_checks']
    if type(tokens) is not list or not 1 <= len(tokens) <= 3 or len(tokens) != value['native_observation']['process_count'] or type(roots) is not list or len(roots) != 2 or any(canonical(row) != canonical(token) for row in [*tokens,*roots]):
        raise ValueError('restricted_report_tokens')
    if lpac:
        if registry or wfp:
            if wfp:
                from .training_wfp_policy import validate_loading_receipt as validate_receipt, validate_token
            else:
                from .training_registry_policy import validate_receipt, validate_token
            validate_token(token,token.get('sid_sha256') if type(token) is dict else None)
            policy = value['least_privileged_policy']
            if type(policy) is not dict or canonical(policy.get('root_tokens')) != canonical(roots) or canonical(policy.get('member_tokens')) != canonical(tokens):
                raise ValueError('restricted_registry_token_binding')
            if wfp and policy.get('intent', {}).get('created_sha256') != value['appcontainer_created_sha256']:
                raise ValueError('restricted_wfp_profile_binding')
        else:
            from .training_offline_policy import validate_receipt
        validate_receipt(value['least_privileged_policy'],value['process_fingerprint'],token['sid_sha256'],len(tokens))
    return value
