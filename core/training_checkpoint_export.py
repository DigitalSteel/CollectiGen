"""Export an anchored SDXL recovery snapshot as an explicit evaluation LoRA.

Only the safe-tensor network member is interpreted. Optimizer/RNG members are
integrity checked by the existing envelope service and never deserialized here.
Tensor payload bytes and FP32 precision are preserved; this is not a resume or
the original run's final-result registration.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import struct

from .model_resource_registry import _header, inspect_model_resource
from .training_process import TrainingProcessService, TrainingRunJournal, _bytes, _read, _write_new, _state, descriptor_identity
from .training_source_io import _assert_unredirected

MAX_BYTES = 512 * 1024**2
PROFILE = "collectigen.sdxl-checkpoint-evaluation-export-v1"


def _write_network(path, raw):
    """No-clobber model publication uses its model bound, not a JSON receipt bound."""
    if type(raw) is not bytes or len(raw)>MAX_BYTES:
        raise ValueError('The exported network exceeds its size bound.')
    _assert_unredirected(path)
    flags=os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os,'O_BINARY',0) | getattr(os,'O_NOFOLLOW',0)
    with os.fdopen(os.open(path,flags,0o600),'wb') as handle:
        handle.write(raw);handle.flush();os.fsync(handle.fileno())
        expected=descriptor_identity(handle.fileno())
    if _state(path)!=expected or _read(path,limit=MAX_BYTES)!=raw:
        raise ValueError('The export failed read-back verification; retain it for review.')


def encode_checkpoint_lora(raw, tensor_specs, metadata):
    """Preserve every finite FP32 network tensor under exact admitted shapes."""
    if type(raw) is not bytes or not 10 <= len(raw) <= MAX_BYTES:
        raise ValueError("The checkpoint network exceeds the export bound.")
    length = int.from_bytes(raw[:8], 'little')
    if not 2 <= length <= 16*1024**2 or length + 8 > len(raw):
        raise ValueError("The checkpoint network header is invalid.")
    _header(raw[8:8+length], len(raw)-8-length)
    header = json.loads(raw[8:8+length])
    tensors = {k:v for k,v in header.items() if k != '__metadata__'}
    if set(tensors) != set(tensor_specs):
        raise ValueError("The checkpoint network does not match the admitted LoRA tensor set.")
    payload = raw[8+length:]
    for name, tensor in tensors.items():
        if (not name.startswith('lora_unet_') or tensor['shape'] != tensor_specs[name]['shape']
                or tensor['dtype'] != 'F32'):
            raise ValueError("The checkpoint is not the exact FP32 UNet LoRA layout.")
        start,end = tensor['data_offsets']
        if any(not math.isfinite(value) for (value,) in struct.iter_unpack('<f', payload[start:end])):
            raise ValueError("The checkpoint contains nonfinite network weights.")
    if any(type(k) is not str or type(v) is not str for k,v in metadata.items()):
        raise ValueError("Export metadata must be plain text.")
    # Preserve unknown original metadata; reviewed ownership fields take precedence.
    header['__metadata__'] = {**header.get('__metadata__', {}), **metadata}
    encoded = json.dumps(header,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
    encoded += b' ' * (-len(encoded) % 8)
    result = struct.pack('<Q',len(encoded)) + encoded + payload
    if len(result)>MAX_BYTES:raise ValueError('The exported network exceeds its size bound.')
    _header(encoded,len(payload))
    return result


@dataclass(frozen=True)
class CheckpointExport:
    path: Path
    sha256: str
    receipt_sha256: str
    step: int
    source_sha256: str


def _checked_source(plan, contract, previous, *, process_service=None, should_cancel=None):
    from .training_execution_plan import validate_execution_contract
    from .training_execution import recovery_choices
    from . import training_continuation as continuation
    validate_execution_contract(contract,plan)
    if previous not in [r['previous'] for r in recovery_choices(plan,contract,previous['anchor_sha256'],should_cancel=should_cancel)]:
        raise ValueError("Choose the exact checkpoint from this stopped run's retained review.")
    rows,tip = TrainingRunJournal(plan.destination).read()
    if not rows:raise ValueError("The training process journal is unavailable.")
    continuation.assess_recovery(plan,tip,previous['name'],contract['continuation']['contract']['prerequisite'],
        checkpoint_receipt_sha256=previous['checkpoint_receipt_sha256'],
        continuation_receipt_sha256=previous['continuation_receipt_sha256'],
        process_service=process_service or TrainingProcessService(),should_cancel=should_cancel)
    path=plan.destination/'work'/previous['name']/'model.safetensors'
    raw=_read(path,limit=MAX_BYTES)
    # Bind the actual model bytes to the already authenticated envelope, not a
    # second unanchored observation of a file with the same name.
    from .training_checkpoint import _paths
    _,receipt=_paths(plan.destination/'work',previous['name'])
    proof=_read(receipt,limit=16*1024)
    if hashlib.sha256(proof).hexdigest()!=previous['checkpoint_receipt_sha256']:
        raise ValueError("The selected checkpoint receipt changed.")
    if hashlib.sha256(raw).hexdigest()!=json.loads(proof)['files']['model.safetensors']['sha256']:
        raise ValueError("The checkpoint network changed after verification.")
    return raw


def export_checkpoint(plan,contract,previous,destination,*,confirmed_selection,process_service=None,should_cancel=None):
    """Publish a new safe-tensor file plus recoverable, exact export receipt."""
    selection=hashlib.sha256(_bytes(previous)).hexdigest()
    if confirmed_selection!=selection:raise ValueError("Review the exact checkpoint before exporting.")
    raw=_checked_source(plan,contract,previous,process_service=process_service,should_cancel=should_cancel)
    source_sha=hashlib.sha256(raw).hexdigest()
    metadata={**contract['result_binding']['required_metadata'],
        'collectigen_export_profile':PROFILE,'collectigen_training_run':plan.run_id,
        'collectigen_training_step':str(previous['step']),
        'collectigen_checkpoint_sha256':previous['checkpoint_receipt_sha256'],
        'collectigen_source_network_sha256':source_sha}
    output=encode_checkpoint_lora(raw,contract['result_binding']['tensor_specs'],metadata)
    sha=hashlib.sha256(output).hexdigest()
    destination=Path(destination).absolute()
    if destination.suffix.casefold()!='.safetensors' or destination.is_relative_to(plan.destination):
        raise ValueError("Choose a new .safetensors destination outside the retained run.")
    _assert_unredirected(destination)
    receipt=destination.with_suffix('.evaluation-export.json')
    payload=_bytes({'schema_version':1,'profile':PROFILE,'plan_fingerprint':plan.fingerprint,
        'contract_sha256':plan.preview_fingerprint,'selection_sha256':selection,
        'source_sha256':source_sha,'output_sha256':sha,'output_bytes':len(output),
        'step':previous['step'],'precision':'F32','quality':'unmeasured'})
    if should_cancel and should_cancel():raise InterruptedError("Checkpoint export cancelled before publication.")
    # An existing exact receipt permits idempotent recovery after a stopped write.
    # A partial output is retained for inspection and never overwritten.
    destination.parent.mkdir(parents=True,exist_ok=True)
    if receipt.exists():
        if _read(receipt)!=payload:raise ValueError("Another export owns this destination receipt.")
    else:
        if destination.exists():raise ValueError("The export destination already exists; choose a new name.")
        _write_new(receipt,payload)
    if destination.exists():
        if _read(destination,limit=MAX_BYTES)!=output:
            raise ValueError("An incomplete or changed export occupies this name. Preserve it and choose a new name.")
    else:_write_network(destination,output)
    inspection=inspect_model_resource(destination,should_cancel=should_cancel)
    if inspection.resource_id!='sha256:'+sha:raise ValueError("Checkpoint export read-back failed.")
    return CheckpointExport(destination,sha,hashlib.sha256(payload).hexdigest(),previous['step'],source_sha)


def register_checkpoint_export(export,plan,contract,previous,registry,*,expected_registry,should_cancel=None):
    """Register separately declared evaluation lineage after exact reinspection."""
    fresh=export_checkpoint(plan,contract,previous,export.path,
        confirmed_selection=hashlib.sha256(_bytes(previous)).hexdigest(),should_cancel=should_cancel)
    if fresh!=export:raise ValueError("The checkpoint export changed after review.")
    binding=contract['result_binding']
    return registry.register_training_result(inspect_model_resource(export.path,should_cancel=should_cancel),
        package_path=binding['package_path'],expected_package_fingerprint=binding['package_fingerprint'],
        expected_registry=expected_registry,parent_ids=tuple(binding['parent_ids']),trainer_id=PROFILE,
        trainer_version='1',settings={'optimizer_step':export.step,'precision':'F32',
            'checkpoint_receipt_sha256':previous['checkpoint_receipt_sha256'],
            'export_receipt_sha256':export.receipt_sha256,'source_run':plan.run_id},
        role='lora',family='SDXL',should_cancel=should_cancel)
