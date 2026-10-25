"""Parent-owned launch, recovery, and verified result handoff for local training."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
import time

from . import training_checkpoint as checkpoint
from . import training_continuation_execution as continuation
from .training_execution_plan import validate_execution_contract
from .training_process import (ProcessInput, ProcessInventory, TrainingProcessPlan,
    TrainingProcessService, TrainingRunJournal, _bytes, _read, _state)
from .training_progress import read_progress
from .training_metrics import NAME as METRICS_NAME, TrainingMetricsHistory, TrainingMetricsSnapshot
from .training_result import SdxlTrainingResultService, VerifiedTrainingResult
from .training_source_io import _assert_unredirected, _identity
from .training_trial import (TrainingTrialLimits, TrainingTrialReceipt, load_intent as load_trial_intent,
    retain_intent as retain_trial_intent, retain_receipt as retain_trial_receipt)
from .training_resource_guidance import TrialMemoryObservation, TrialMemorySampler, retain_observation


MAX_REVIEW_BYTES = 4 * 1024**2
FAILURE_CODES = {'out_of_memory', 'nonfinite', 'resource_unavailable',
    'workspace_unavailable', 'input_changed', 'runtime_failed'}


@dataclass(frozen=True)
class TrainingExecutionOutcome:
    status: str
    destination: Path
    message: str
    process: object = field(default=None, repr=False)
    resource_id: str = ''
    checkpoint_anchor_sha256: str = ''
    checkpoints: tuple[dict, ...] = ()
    verified: VerifiedTrainingResult | None = field(default=None, repr=False)
    failure_code: str = ''
    trial: TrainingTrialReceipt | None = None
    metrics: TrainingMetricsSnapshot | None = field(default=None, repr=False)
    metrics_error: str = ''
    memory: TrialMemoryObservation | None = None


def _write_exact(path, value):
    return _write_exact_bytes(path, _bytes(value))


def _write_exact_bytes(path, raw):
    """Exclusive bounded parent record with exact identity/read-back checks."""
    if type(raw) is not bytes or len(raw) > MAX_REVIEW_BYTES:
        raise ValueError('training_execution_review_size_bound')
    _assert_unredirected(path)
    with path.open('xb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
        expected = _identity(os.fstat(handle.fileno()))
    if _state(path) != expected or _read(path, limit=MAX_REVIEW_BYTES) != raw:
        raise ValueError('training_execution_review_readback_failed')
    return hashlib.sha256(raw).hexdigest()


def retain_review(plan, contract):
    """Keep the exact reviewed private plan beside its new run, never in logs."""
    if continuation.digest(contract) != plan.preview_fingerprint:
        raise ValueError('training_execution_review_unbound')
    path = Path(plan.root) / (plan.name + '.execution-review.json')
    value = {'schema_version': 1, 'kind': 'sdxl_training_execution_review',
        'plan': asdict(plan), 'contract': contract, 'fingerprint': plan.fingerprint}
    if os.path.lexists(path):
        restored, current = restore_review(path, hashlib.sha256(_bytes(value)).hexdigest())
        if restored != plan or current != contract:
            raise ValueError('training_execution_review_changed')
        return path, hashlib.sha256(_bytes(value)).hexdigest()
    return path, _write_exact(path, value)


def restore_review(path, expected_sha256):
    """Restore a selected stopped-run review; this never starts or resumes it."""
    raw = _read(Path(path), limit=MAX_REVIEW_BYTES)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('training_execution_review_changed')
    value = json.loads(raw)
    if (type(value) is not dict or set(value) != {'schema_version', 'kind', 'plan', 'contract', 'fingerprint'}
            or value['schema_version'] != 1 or type(value['schema_version']) is not int
            or value['kind'] != 'sdxl_training_execution_review' or _bytes(value) != raw):
        raise ValueError('training_execution_review_invalid')
    fields = deepcopy(value['plan'])
    fields['inputs'] = tuple(ProcessInput(**{**row, 'state': tuple(row['state'])}) for row in fields['inputs'])
    fields['inventories'] = tuple(ProcessInventory(**row) for row in fields['inventories'])
    fields['command'], fields['root_identity'] = tuple(fields['command']), tuple(fields['root_identity'])
    plan = TrainingProcessPlan(**fields)
    plan.validate()
    if plan.fingerprint != value['fingerprint'] or continuation.digest(value['contract']) != plan.preview_fingerprint:
        raise ValueError('training_execution_review_changed')
    return plan, value['contract']


def observed_failure(plan):
    """Only stable worker categories are displayed; a receipt is not authority."""
    try:
        raw = _read(plan.destination / 'work/training-failure.json', limit=4096)
        value = json.loads(raw)
        if (type(value) is not dict or set(value) != {'schema_version', 'kind',
                'process_fingerprint', 'execution_contract_sha256', 'code', 'error_type'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['kind'] != 'sdxl_training_failure' or _bytes(value) != raw
                or value['process_fingerprint'] != plan.fingerprint
                or value['execution_contract_sha256'] != plan.preview_fingerprint
                or value['code'] not in FAILURE_CODES
                or type(value['error_type']) is not str or not value['error_type'].isidentifier()
                or len(value['error_type']) > 100):
            return 'runtime_failed'
        return value['code']
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return 'runtime_failed'


def recovery_choices(plan, contract, anchor_sha256, *, should_cancel=None):
    """List anchored recovery candidates; selected files are reverified before use."""
    binding = checkpoint.binding_for(plan)
    rows = continuation.anchored_choices(str(plan.destination), binding,
        contract['continuation']['contract'], anchor_sha256, should_cancel=should_cancel)
    return tuple({'sequence': row['sequence'], 'step': row['step'], 'epoch': row['epoch'],
        'previous': {'destination': str(plan.destination), 'binding': deepcopy(binding),
            'anchor_sha256': anchor_sha256, 'sequence': row['sequence'],
            'name': row['name'], 'step': row['step'], 'epoch': row['epoch'], **row['proof']}}
        for row in rows)


class TrainingExecutionService:
    reports_process_invocation = True
    supports_metrics = True

    def __init__(self, *, process=None, results=None, run_index=None, clock=time.monotonic,
                 memory_sampler_factory=TrialMemorySampler):
        self.process = process or TrainingProcessService()
        self.results = results or SdxlTrainingResultService()
        self.run_index = run_index
        self.clock = clock
        self.memory_sampler_factory = memory_sampler_factory

    def execute(self, plan, contract, confirmed_fingerprint, jobs, registry, *,
                 expected_registry, should_cancel=None, running_observer=None, progress_callback=None,
                 invocation_observer=None, metrics_callback=None, trial: TrainingTrialLimits | None = None):
        if confirmed_fingerprint != plan.fingerprint:
            raise ValueError('training_execution_confirmation_changed')
        validate_execution_contract(contract, plan)
        if registry.snapshot()[1] != expected_registry or contract['resources']['registry_revision'] != expected_registry:
            raise ValueError('training_execution_registry_changed')
        review_path, review_sha = retain_review(plan, contract)
        if trial is not None:
            retain_trial_intent(plan, contract, trial)
        elif load_trial_intent(plan, contract) is not None:
            raise ValueError('training_trial_intent_requires_trial_review')
        if self.run_index is not None:
            from .training_run_recovery import register_review
            register_review(self.run_index, plan, 'execution', review_path, review_sha,
                registry_path=registry.path, registry_revision=expected_registry)
        last_poll, last_digest = 0.0, ''
        metrics_history = None
        metrics_error = ''
        metrics_disabled = False
        invoked_at = None
        trial_stop_reason = ''
        memory_sampler = None
        if trial is not None:
            try:
                memory_sampler = self.memory_sampler_factory(plan, contract)
            except (OSError, ValueError, RuntimeError, TypeError):
                pass  # Missing memory telemetry must not block the trial.
        def notify(value):
            if progress_callback is not None:
                try:
                    progress_callback(value)
                except Exception:
                    pass  # An observation consumer must never interrupt process ownership.
        def metric_notice(snapshot, error=''):
            if metrics_callback is not None:
                try:
                    metrics_callback(snapshot, error)
                except Exception:
                    pass  # Presentation must not interrupt the process owner.
        def poll_progress(*, force=False):
            nonlocal last_poll, last_digest, metrics_history, metrics_error, metrics_disabled
            now = self.clock()
            if not force and now - last_poll < .5:
                return None
            last_poll = now
            path = plan.destination / 'work' / 'training-progress.json'
            try:
                value, digest = read_progress(path.parent, plan.fingerprint, plan.preview_fingerprint)
            except (OSError, ValueError, RuntimeError):
                if force and os.path.lexists(path) and not metrics_error:
                    metrics_error = 'Training measurements could not read verified worker progress; the run continues.'
                    metric_notice(None, metrics_error)
                return None  # A not-yet-written observation is not a process failure.
            if digest == last_digest:
                return value
            last_digest = digest
            if not metrics_disabled:
                try:
                    if metrics_history is None:
                        metrics_history = TrainingMetricsHistory(plan.destination, plan.fingerprint,
                            plan.preview_fingerprint, contract['result_binding']['expected_final_step'])
                    snapshot = metrics_history.observe(value,
                        elapsed_seconds=max(0.0, now - invoked_at) if invoked_at is not None else None)
                    metric_notice(snapshot)
                except (OSError, ValueError, RuntimeError):
                    metrics_disabled = True
                    metrics_error = 'Training measurements could not be retained; process and result evidence remain separate.'
                    metric_notice(None, metrics_error)
            notify(value)
            return value
        def control():
            nonlocal trial_stop_reason
            if should_cancel and should_cancel():
                trial_stop_reason = 'user_cancel' if trial is not None else ''
                return True
            value = poll_progress()
            if trial is not None and invoked_at is not None:
                if value is not None and value['optimizer_step'] >= trial.max_optimizer_steps:
                    trial_stop_reason = 'step_limit'
                    return True
                if self.clock() - invoked_at >= trial.max_elapsed_seconds:
                    trial_stop_reason = 'elapsed_limit'
                    return True
            return False
        notify({'state': 'starting', 'optimizer_step': 0,
                'max_train_steps': contract['result_binding']['expected_final_step']})
        invoked = False
        def entering_owner():
            nonlocal invoked, invoked_at
            invoked = True
            invoked_at = self.clock()
            if memory_sampler is not None:
                try: memory_sampler.start()
                except (OSError, ValueError, RuntimeError): pass
            if invocation_observer is not None:
                try:
                    invocation_observer()
                except Exception:
                    pass  # Observation must never interrupt process ownership.
        options = {}
        if getattr(self.process, "reports_process_invocation", False):
            options["invocation_observer"] = entering_owner
        else:
            entering_owner()  # Legacy injected adapters remain conservative.
        memory_observation = None
        try:
            outcome = self.process.execute(plan, confirmed_fingerprint, jobs,
                should_cancel=control, running_observer=running_observer, **options)
        except BaseException:
            if self.run_index is not None:
                from .training_run_recovery import note_outcome
                note_outcome(self.run_index, plan, 'recovery_required' if invoked else 'failed')
            raise
        finally:
            if memory_sampler is not None:
                try: memory_observation = memory_sampler.stop()
                except (OSError, ValueError, RuntimeError): pass
        poll_progress(force=True)
        snapshot = None
        if metrics_history is not None:
            try:
                snapshot = metrics_history.snapshot()
            except (OSError, ValueError, RuntimeError):
                metrics_error = 'Training measurements changed; process and result evidence remain separate.'
                metric_notice(None, metrics_error)
        if trial is None and progress_callback is not None and outcome.tree_stopped and outcome.exit_code == 0:
            notify({'state': 'verifying',
                'optimizer_step': contract['result_binding']['expected_final_step'],
                'max_train_steps': contract['result_binding']['expected_final_step']})
        result = self.finish(plan, contract, outcome, registry, expected_registry=expected_registry,
                             should_cancel=should_cancel, trial=trial, trial_stop_reason=trial_stop_reason,
                             metrics=snapshot)
        if (trial is not None and outcome.tree_stopped and memory_observation is not None
                and result.status in {'trial_stopped', 'trial_cancelled', 'trial_failed',
                    'trial_interrupted', 'trial_ended_before_limit'}):
            try:
                retain_observation(plan, contract, memory_observation)
                result = replace(result, memory=memory_observation)
            except (OSError, ValueError, RuntimeError):
                pass  # The canonical process and timing receipts remain authoritative.
        return replace(result, metrics=snapshot, metrics_error=metrics_error)

    def finish(self, plan, contract, outcome, registry, *, expected_registry, should_cancel=None,
               trial: TrainingTrialLimits | None = None, trial_stop_reason='', metrics=None):
        result = (self._finish_trial(plan, contract, outcome, trial, trial_stop_reason, metrics)
                  if trial is not None else self._finish(plan, contract, outcome, registry,
                      expected_registry=expected_registry, should_cancel=should_cancel))
        if self.run_index is not None:
            from .training_run_recovery import note_outcome
            if trial is not None:
                phase = ('recovery_required' if result.status in {'stopping', 'trial_recovery_required'}
                    else 'cancelled' if result.status in {'trial_stopped', 'trial_cancelled'}
                    else 'failed' if result.status == 'trial_failed'
                    else 'interrupted' if result.status == 'trial_interrupted' else 'stopped')
            else:
                phase = 'recovery_required' if outcome.status == 'recovery_required' else result.status
            if not note_outcome(self.run_index, plan, phase, getattr(outcome, 'journal_tip', '')):
                result = replace(result, message=result.message +
                    ' The saved run list needs recovery; keep this run workspace and review.')
        return result

    def _finish_trial(self, plan, contract, outcome, limits, stop_reason, metrics):
        limits.validate(contract['result_binding']['expected_final_step'])
        if outcome.status == 'held':
            return TrainingExecutionOutcome('held', plan.destination,
                'GPU work is active. The unchanged measured trial can be retried.', outcome)
        if not outcome.tree_stopped:
            return TrainingExecutionOutcome('stopping', plan.destination,
                'The trial process has not been confirmed stopped. Keep its workspace and check recovery.', outcome)
        if outcome.status == 'recovery_required':
            return TrainingExecutionOutcome('trial_recovery_required', plan.destination,
                'The trial process still needs receipt recovery. No model was verified or registered.', outcome)
        anchor, choices = '', ()
        try:
            anchor = continuation.anchor_stopped(plan, outcome.journal_tip,
                contract['continuation']['contract'], process_service=self.process)
            choices = recovery_choices(plan, contract, anchor)
        except (OSError, ValueError, RuntimeError):
            pass
        code = observed_failure(plan) if outcome.status == 'failed' else ''
        if outcome.status == 'cancelled':
            if stop_reason in {'step_limit', 'elapsed_limit'}:
                status, reason = 'trial_stopped', stop_reason
            else:
                status, reason = 'trial_cancelled', 'user_cancel'
        elif outcome.status == 'failed':
            status, reason = 'trial_failed', 'process_failed'
        elif outcome.status == 'interrupted':
            status, reason = 'trial_interrupted', 'process_interrupted'
        else:
            status, reason = 'trial_ended_before_limit', 'ended_before_limit'
        try:
            receipt = retain_trial_receipt(plan, contract, limits, outcome,
                status=status, stop_reason=reason, metrics=metrics, failure_code=code)
        except (OSError, ValueError, RuntimeError):
            return TrainingExecutionOutcome('trial_recovery_required', plan.destination,
                'The trial process stopped, but its measurement receipt needs recovery. No model was registered.',
                outcome, checkpoint_anchor_sha256=anchor, checkpoints=choices, failure_code=code)
        if status == 'trial_stopped':
            message = ('Measured trial stopped at its reviewed step or time limit. '
                'Its rate estimate excludes warmup; no model was verified or registered.')
        elif status == 'trial_ended_before_limit':
            message = ('The trainer exited before the trial stop could take effect. '
                'No model was verified or registered; review this retained run.')
        else:
            message = ('The measured trial was cancelled. Retained timing may be incomplete; no model was registered.'
                if status == 'trial_cancelled' else
                'The measured trial failed or was interrupted. Review process recovery; no model was registered.')
        return TrainingExecutionOutcome(status, plan.destination, message, outcome,
            checkpoint_anchor_sha256=anchor, checkpoints=choices, failure_code=code, trial=receipt)

    def _finish(self, plan, contract, outcome, registry, *, expected_registry, should_cancel=None):
        if outcome.status == 'held':
            return TrainingExecutionOutcome('held', plan.destination,
                'GPU work is already active. This training run has not started; retry the unchanged review when capacity is available.', outcome)
        if not outcome.tree_stopped:
            return TrainingExecutionOutcome('stopping', plan.destination,
                'The trainer has not stopped. Keep its workspace until process recovery confirms it has stopped.', outcome)
        anchor, choices = '', ()
        try:
            anchor = continuation.anchor_stopped(plan, outcome.journal_tip,
                contract['continuation']['contract'], process_service=self.process)
            choices = recovery_choices(plan, contract, anchor)
        except (OSError, ValueError, RuntimeError):
            pass
        if outcome.status != 'exited_unverified' or outcome.exit_code != 0:
            status = outcome.status if outcome.status in {'cancelled', 'failed', 'interrupted'} else 'failed'
            code = '' if status == 'cancelled' else observed_failure(plan)
            message = ('Training was cancelled. Retained checkpoints require an explicit resume choice.' if status == 'cancelled'
                else 'Training stopped before a verified result was available. Review the failure and retained checkpoints.')
            return TrainingExecutionOutcome(status, plan.destination, message, outcome,
                checkpoint_anchor_sha256=anchor, checkpoints=choices, failure_code=code)
        try:
            result_anchor = self.results.anchor_stopped_result(plan, outcome, contract, should_cancel=should_cancel)
            verified = self.results.verify(plan, outcome, contract, result_anchor, should_cancel=should_cancel)
        except (OSError, ValueError, RuntimeError, InterruptedError):
            return TrainingExecutionOutcome('verification_required', plan.destination,
                'The trainer stopped. Its output still needs independent verification before it can be registered.', outcome,
                checkpoint_anchor_sha256=anchor, checkpoints=choices)
        try:
            resource_id = self.results.register(registry, verified, expected_registry=expected_registry,
                                                should_cancel=should_cancel)
        except (OSError, ValueError, RuntimeError, InterruptedError):
            return TrainingExecutionOutcome('registration_required', plan.destination,
                'The output is verified. Review the current resource registry before registering it.', outcome,
                checkpoint_anchor_sha256=anchor, checkpoints=choices, verified=verified)
        return TrainingExecutionOutcome('completed', plan.destination,
            'The LoRA is verified and registered with its dataset, base resources, settings and recovery evidence. Quality has not been assessed.',
            outcome, resource_id, anchor, choices, verified)

    @staticmethod
    def progress(plan):
        return read_progress(plan.destination / 'work', plan.fingerprint, plan.preview_fingerprint)[0]

    @staticmethod
    def metrics(plan, contract):
        """Background-only read of an existing sidecar; never creates one on reopen."""
        path = plan.destination / METRICS_NAME
        _assert_unredirected(path)
        if not os.path.lexists(path):
            return None
        return TrainingMetricsHistory(plan.destination, plan.fingerprint, plan.preview_fingerprint,
            contract['result_binding']['expected_final_step']).snapshot()

    def recover(self, plan, contract, expected_process_tip):
        """Confirm process death and checkpoint integrity, without launching."""
        from .training_run_recovery import recover_plan_process, note_outcome
        outcome = recover_plan_process(plan, self.process, expected_process_tip)
        if self.run_index is not None:
            phase = ('recovery_required' if not outcome.tree_stopped or outcome.status == 'recovery_required'
                     else 'verification_required' if outcome.status == 'exited_unverified' and outcome.exit_code == 0
                     else outcome.status if outcome.status in {'cancelled', 'failed', 'interrupted'} else 'stopped')
            if not note_outcome(self.run_index, plan, phase, outcome.journal_tip):
                outcome = replace(outcome, message=outcome.message +
                    ' The saved run list needs recovery; keep this run workspace and review.')
        if not outcome.tree_stopped or outcome.status == 'recovery_required':
            return outcome, '', ()
        try:
            anchor = continuation.anchor_stopped(plan, outcome.journal_tip,
                contract['continuation']['contract'], process_service=self.process)
            return outcome, anchor, recovery_choices(plan, contract, anchor)
        except (OSError, ValueError, RuntimeError):
            # Process death is useful evidence even when training stopped
            # before the first checkpoint, or its optional journal is damaged.
            return outcome, '', ()
