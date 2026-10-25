"""Join selected software, native readiness and the final training review.

Preparing this workflow checks selected local software. It never starts training,
acquires resources, changes installed software or registers an output.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from . import training_execution_plan as authority
from .training_configuration import TrainingConfiguration
from .training_execution_preparation import (QualifiedExecutionEnvironment,
    TrainingExecutionPreparationService, _runtime_configuration)
from .training_execution_readiness import TrainingExecutionReadinessService
from .training_execution_selection import PreparedTrainingSource, TrainingExecutionSelectionService
from .training_local_components import LocalComponentSelection, RegisteredModelSelection
from .training_package import TrainingPackageService, TrainingPackageVerification
from .training_process import TrainingRunJournal, _bytes, _read, _write_new, _directory
from .training_resume_adapter import patch_source
from .training_source_io import _assert_unredirected

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'tools/training_execution_qualification.json'
TENSOR_SPECS = ROOT / 'tools/training_execution_tensor_specs.json'
SOURCE_CATALOG = ROOT / 'tools/training_runtime_sources.json'
FIELDS = {'source_root', 'environment_root', 'python_base_root', 'python', 'tokenizers_root',
          'output_root', 'run_name', 'checkpoint_interval'}


def _assets(package, recipe):
    profile = authority.qualified_profile()
    if profile is None:
        raise ValueError('Local training is unavailable until a reviewed execution profile is installed. The manual package remains usable.')
    evidence = _read(EVIDENCE, limit=4 * 1024**2)
    raw = _read(TENSOR_SPECS, limit=1024**2)
    specs = authority._strict_json(raw)
    if (raw != _bytes(specs) or hashlib.sha256(evidence).hexdigest() != profile['qualification']['evidence_sha256']):
        raise ValueError('The installed training qualification evidence changed. Review trainer setup.')
    config = TrainingConfiguration.from_dict(package.manifest['training_configuration'])
    authority._qualification(profile['qualification'])
    authority._profile({'qualification': profile['qualification'],
        'resources': {'base': {'id': config.base_model_id}},
        'recipe': {'proposal': asdict(recipe), 'configuration': {'mixed_precision': config.precision}},
        'result_binding': {'tensor_specs': specs}})
    return profile, EVIDENCE, specs


def _request(value):
    if type(value) is not dict or set(value) != FIELDS:
        raise ValueError('Choose the exact trainer, tokenizer and output locations.')
    result = dict(value)
    for key in FIELDS - {'checkpoint_interval', 'run_name'}:
        path = value[key]
        if type(path) is not str or not path or '\0' in path or not Path(path).is_absolute():
            raise ValueError('Choose explicit absolute local software and output locations.')
        result[key] = Path(path).absolute()
        _assert_unredirected(result[key])
    name = value['run_name']
    if (type(name) is not str or not name or len(name) > 80
            or re.search(r'[\\/:*?"<>|\x00-\x1f]', name) or name in {'.', '..'}
            or name.endswith((' ', '.')) or name.split('.')[0].upper() in
                {'CON','PRN','AUX','NUL',*(f'COM{n}' for n in range(1,10)),*(f'LPT{n}' for n in range(1,10))}):
        raise ValueError('Choose an unused run name of up to 80 characters without reserved filename characters.')
    interval = value['checkpoint_interval']
    if type(interval) is not int or not 0 <= interval <= 1_000_000_000:
        raise ValueError('Choose an automatic or positive checkpoint interval.')
    _directory(result['output_root'])
    return result


class TrainingWorkflowService:
    def __init__(self, jobs, *, readiness=None, selection=None, preparation=None, preparation_root=None):
        from .paths import USER_DATA_DIR
        self.jobs = jobs
        self.preparation_root = Path(preparation_root if preparation_root is not None
            else USER_DATA_DIR / 'training-preparation').absolute()
        self.readiness = readiness or TrainingExecutionReadinessService()
        self.selection = selection or TrainingExecutionSelectionService()
        self.preparation = preparation or TrainingExecutionPreparationService()
        self.last_readiness = None
        self.recovery_required = False
        self.last_preparation_root = None

    def prepare(self, package, registry, recipe, request, *, should_cancel=None, progress=None,
                previous=None, resume_contract=None, native_receipt=None):
        def cancel():
            if should_cancel and should_cancel():
                raise InterruptedError('Training preparation cancelled; retained checks remain available.')
        cancel()
        if (previous is None) != (resume_contract is None) or (previous is not None and native_receipt is None):
            raise ValueError('Resume requires the exact prior contract, checkpoint and retained native readiness evidence.')
        if self.recovery_required:
            raise ValueError('Confirm the previous readiness process has stopped before preparing again.')
        if not isinstance(package, TrainingPackageVerification) or not package.valid:
            raise ValueError('Review a verified training package before preparing local training.')
        recipe.validate()
        profile, evidence, specs = _assets(package, recipe)
        selected = _request(request)
        fresh = TrainingPackageService().verify(package.destination, should_cancel=should_cancel)
        if not fresh.valid or fresh.manifest != package.manifest or fresh.package_fingerprint != package.package_fingerprint:
            raise ValueError('The training package changed. Review it again.')
        _, revision = registry.snapshot()
        config = TrainingConfiguration.from_dict(fresh.manifest['training_configuration'])
        base = registry.resolve(config.base_model_id, role='checkpoint', family='SDXL',
                                should_cancel=should_cancel, progress=progress)
        if base.status != 'identity_verified' or base.inspection is None:
            raise ValueError('The exact base model is unavailable. Select it in the shared model registry.')
        token_root = selected['tokenizers_root']
        components = LocalComponentSelection(str(registry.path), revision,
            RegisteredModelSelection(config.base_model_id, str(base.inspection.path)),
            str(token_root / 'tokenizer'), str(token_root / 'tokenizer_2'))
        components.validate()
        output = selected['output_root']
        inputs = [selected[name] for name in ('source_root', 'environment_root', 'python_base_root', 'tokenizers_root')]
        inputs += [fresh.destination, base.inspection.path]
        if any(output == path or output.is_relative_to(path) or path.is_relative_to(output) for path in inputs):
            raise ValueError('Choose an output folder separate from the selected package, model and software locations.')
        if os.path.lexists(output / selected['run_name']):
            raise ValueError('This run destination is occupied. Choose a new run name.')
        cancel()
        folder = self.preparation_root / uuid4().hex
        if any(folder.is_relative_to(path) or path.is_relative_to(folder) for path in (*inputs, output)):
            raise ValueError('Choose training output and inputs separate from the app preparation area.')
        _assert_unredirected(folder)
        folder.mkdir(parents=True, exist_ok=False)
        self.last_preparation_root = folder
        _write_new(folder / 'selection.json', _bytes(request))
        original = selected['source_root'] / 'train_network.py'
        raw_source = _read(original, limit=2 * 1024**2)
        catalog = authority._strict_json(_read(SOURCE_CATALOG, limit=2 * 1024**2))
        patch = folder / 'prepared-source' / 'train_network.py'
        patch.parent.mkdir(exist_ok=False)
        _write_new(patch, patch_source(raw_source, catalog).encode('utf-8'))
        runtime_config = _runtime_configuration(fresh.manifest, recipe, str(base.inspection.path))
        if native_receipt is None:
            if previous is not None or resume_contract is not None:
                raise ValueError('Resume requires the original retained native readiness evidence.')
            prepared = self.readiness.prepare(selected['source_root'], selected['environment_root'],
                selected['python_base_root'], selected['python'], token_root, runtime_config,
                {key: profile['qualification'][key] for key in ('profile', 'evidence_sha256', 'runtime_profile')},
                config.base_model_id, folder / 'readiness-inputs', folder, 'readiness',
                should_cancel=should_cancel, progress=progress)
            self.last_readiness = prepared
            self.recovery_required = True
            try:
                outcome, native_receipt = self.readiness.execute(prepared, prepared.plan.fingerprint,
                                                               self.jobs, should_cancel=should_cancel)
            except BaseException:
                self.recovery_required = bool(getattr(self.readiness, 'process_invoked', True))
                raise
            self.recovery_required = outcome.status == 'recovery_required' or not outcome.tree_stopped and outcome.status != 'held'
            if native_receipt is None:
                raise ValueError('Trainer readiness did not pass. ' + str(outcome.message)[:1500])
        cancel()
        joined = self.selection.select(fresh, registry, recipe, components, native_receipt,
            PreparedTrainingSource(original, SOURCE_CATALOG, patch), expected_registry=revision,
            save_every_n_steps=selected['checkpoint_interval'] or None, resume_contract=resume_contract,
            should_cancel=should_cancel, progress=progress)
        environment = QualifiedExecutionEnvironment(selected['python'], selected['source_root'],
            selected['environment_root'], selected['python_base_root'], patch, evidence)
        return self.preparation.prepare(fresh, registry, recipe, components, joined.admission,
            joined.resolved, environment, joined.device_observation, folder / 'execution-inputs',
            output, selected['run_name'], tensor_specs=specs, previous=previous,
            expected_registry=revision, should_cancel=should_cancel, progress=progress)

    def recover_preparation(self, *, should_cancel=None, progress=None):
        if should_cancel and should_cancel(): raise InterruptedError('Recovery check cancelled.')
        if not self.recovery_required: return True
        if self.last_readiness is None: return False
        plan = self.last_readiness.plan
        _, tip = TrainingRunJournal(plan.destination).read()
        result = self.readiness.process.recover(plan.destination, tip)
        self.recovery_required = not result.tree_stopped or getattr(result, 'status', '') == 'recovery_required'
        return not self.recovery_required
