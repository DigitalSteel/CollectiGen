"""Selected-run experiment review over the canonical index and package store."""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import hashlib
from pathlib import Path
from types import SimpleNamespace

from .training_package import TrainingPackageService
from .training_plan_drafts import TrainingPlanDraftStore, validate_training_plan_manifest
from .training_run_index import (PHASES, TrainingRunAnnotation, TrainingRunIndexError,
    TrainingRunPage)
from .training_run_recovery import IndexedTrainingReview, restore_indexed_review
from .training_process import TrainingProcessService, TrainingRunJournal, _bytes
from .training_trial import load_receipt as load_trial_receipt, receipt_matches_process
from .training_execution import TrainingExecutionService


@dataclass(frozen=True)
class TrainingExperimentReview:
    indexed: IndexedTrainingReview
    candidate_manifest: dict
    configuration: dict
    package_fingerprint: str

    @property
    def record(self):
        return self.indexed.record


@dataclass(frozen=True)
class TrainingExperimentComparison:
    first: TrainingExperimentReview
    second: TrainingExperimentReview
    differences: tuple[str, ...]
    first_outcome: "TrainingExperimentOutcome"
    second_outcome: "TrainingExperimentOutcome"


@dataclass(frozen=True)
class TrainingExperimentOutcome:
    """Read-only run evidence; this does not verify or rate a model output."""
    indexed_phase: str
    process_state: str
    exit_code: int | None
    observed_step: int | None
    observed_loss: float | None
    observed_gaps: bool | None
    trial_status: str
    evidence: str


def _outcome(review: TrainingExperimentReview, should_cancel=None) -> TrainingExperimentOutcome:
    record, plan = review.record, review.indexed.plan
    def cancel():
        if should_cancel is not None and should_cancel():
            raise InterruptedError("Experiment outcome check cancelled.")
    cancel()
    if not record.latest_journal_tip:
        state = "not started" if record.phase in {"prepared", "ready"} else "unavailable"
        return TrainingExperimentOutcome(record.phase, state, None, None, None, None,
            "unavailable", "No matching retained process receipt; model result is not checked.")
    try:
        rows, tip = TrainingRunJournal(plan.destination).read()
        receipt = TrainingProcessService._receipt(plan)
        expected = {"plan": receipt, "fingerprint": hashlib.sha256(_bytes(receipt)).hexdigest()}
        if (not rows or tip != record.latest_journal_tip or rows[0]["state"] != "reserved"
                or _bytes(rows[0]["details"]) != _bytes(expected)):
            raise ValueError("The indexed process receipt changed.")
    except (OSError, ValueError, RuntimeError):
        return TrainingExperimentOutcome(record.phase, "unavailable", None, None, None,
            None, "unavailable", "The indexed process receipt is unavailable or changed; model result is not checked.")
    cancel()
    last = rows[-1]
    code = last["details"].get("exit_code")
    if type(code) is not int:
        code = None
    observed_step, observed_loss, observed_gaps = None, None, None
    evidence = "Exact reviewed run and process journal; model bytes and image quality are not checked here."
    try:
        metrics = TrainingExecutionService.metrics(plan, review.indexed.value)
        if metrics is not None:
            observed_gaps = metrics.observed_gaps
            if metrics.records:
                last_observed = metrics.records[-1]
                observed_step = last_observed.optimizer_step
                observed_loss = next((row.loss for row in reversed(metrics.records)
                    if row.loss is not None), None)
    except (OSError, ValueError, RuntimeError, KeyError):
        evidence += " Observed measurements are unavailable or changed."
    cancel()
    trial_status = "none"
    try:
        trial = load_trial_receipt(plan, review.indexed.value)
        if trial is not None:
            stopped = SimpleNamespace(tree_stopped=last["state"] in
                {"cancelled", "failed", "interrupted", "exited_unverified"},
                journal_tip=tip, status=last["state"], exit_code=code)
            trial_status = trial.status if receipt_matches_process(trial, stopped) else "unavailable"
    except (OSError, ValueError, RuntimeError, KeyError):
        trial_status = "unavailable"
    return TrainingExperimentOutcome(record.phase, last["state"], code,
        observed_step, observed_loss, observed_gaps, trial_status, evidence)


class TrainingExperiments:
    """No discovery or launch: open only selected, hash-bound retained reviews."""

    def __init__(self, index, draft_store_path, *, packages=None):
        self.index = index
        self.draft_store_path = Path(draft_store_path)
        self.packages = packages or TrainingPackageService()

    def page(self, *, offset=0, limit=100, query="", favorite_only=False, status="",
             should_cancel=None):
        if (type(offset) is not int or offset < 0 or type(limit) is not int
                or not 1 <= limit <= 100 or type(query) is not str or len(query) > 200
                or type(favorite_only) is not bool or type(status) is not str
                or status not in PHASES | {""}):
            raise TrainingRunIndexError("Choose valid bounded training experiment filters.")
        needle = query.strip().casefold()
        if not (needle or favorite_only or status):
            return self.index.page(offset=offset, limit=limit)
        records, revision = self.index.snapshot()
        matched = []
        for position, record in enumerate(records):
            if should_cancel is not None and position % 128 == 0 and should_cancel():
                raise InterruptedError("Experiment search cancelled.")
            if favorite_only and not record.annotation.favorite:
                continue
            if status and record.phase != status:
                continue
            if needle and not any(needle in text.casefold() for text in (
                    record.annotation.name, record.annotation.notes, record.run_id,
                    Path(record.destination).name)):
                continue
            matched.append(record)
        if should_cancel is not None and should_cancel():
            raise InterruptedError("Experiment search cancelled.")
        selected = tuple(matched[offset:offset + limit])
        next_offset = offset + len(selected) if offset + len(selected) < len(matched) else None
        return TrainingRunPage(selected, len(matched), offset, next_offset, revision)

    def annotate(self, run_id, expected_revision, *, name, notes, favorite):
        annotation = TrainingRunAnnotation(name.strip(), notes.strip(), favorite)
        return self.index.update_annotation(run_id, annotation, expected_revision)

    def review(self, run_id, expected_revision, *, should_cancel=None):
        def checkpoint():
            if should_cancel is not None and should_cancel():
                raise InterruptedError("Experiment review cancelled.")
        checkpoint()
        indexed = restore_indexed_review(self.index, run_id, expected_revision)
        if indexed.record.kind != "execution":
            raise TrainingRunIndexError("Only retained execution runs can be compared or cloned.")
        value = indexed.value
        binding = value.get("result_binding")
        package = value.get("package")
        if type(binding) is not dict or type(package) is not dict:
            raise TrainingRunIndexError("The execution review has no exact package binding.")
        root = binding.get("package_path")
        if type(root) is not str or not Path(root).is_absolute():
            raise TrainingRunIndexError("The execution review has no exact package location.")
        checkpoint()
        verified = self.packages.verify(root, should_cancel=should_cancel)
        if (not verified.valid or verified.package_fingerprint != package.get("package_fingerprint")
                or verified.package_fingerprint != binding.get("package_fingerprint")):
            raise TrainingRunIndexError("The selected run's frozen package is unavailable or changed.")
        # The package verifier authenticates the full manifest and every artifact.
        # The execution binding additionally pins its manifest bytes and location.
        from .training_process import _read
        from .training_package import MANIFEST_NAME
        from .training_execution_plan import MAX_PACKAGE_MANIFEST
        from .training_dataset_preparation import digest
        import hashlib
        if (hashlib.sha256(_read(Path(root) / MANIFEST_NAME,
                limit=MAX_PACKAGE_MANIFEST)).hexdigest() != package.get("manifest_sha256")
                or package.get("manifest_sha256") != binding.get("manifest_sha256")
                or digest(root) != package.get("location_sha256")):
            raise TrainingRunIndexError("The selected run's package binding changed.")
        candidate = validate_training_plan_manifest(verified.manifest["candidate_plan"],
            should_cancel=should_cancel)
        configuration = deepcopy(verified.manifest["training_configuration"])
        if configuration != candidate.get("training_configuration"):
            raise TrainingRunIndexError("The selected run's package configuration changed.")
        checkpoint()
        return TrainingExperimentReview(indexed, candidate, configuration,
            verified.package_fingerprint)

    def compare(self, run_ids, expected_revision, *, should_cancel=None):
        if type(run_ids) not in (tuple, list) or len(run_ids) != 2 or run_ids[0] == run_ids[1]:
            raise ValueError("Select exactly two distinct execution runs.")
        first = self.review(run_ids[0], expected_revision, should_cancel=should_cancel)
        second = self.review(run_ids[1], expected_revision, should_cancel=should_cancel)
        first_outcome = _outcome(first, should_cancel)
        second_outcome = _outcome(second, should_cancel)
        if self.index.snapshot()[1] != expected_revision:
            raise TrainingRunIndexError("The selected runs changed while comparing outcomes. Refresh them.")
        left, right = first.candidate_manifest, second.candidate_manifest
        fields = (
            ("Frozen image scope", left.get("selection_scope"), right.get("selection_scope")),
            ("Goal", left.get("goal"), right.get("goal")),
            ("Caption profile", left.get("profile"), right.get("profile")),
            ("Package identity", first.package_fingerprint, second.package_fingerprint),
            ("Training configuration", first.configuration, second.configuration),
            ("Base model", first.configuration.get("target"), second.configuration.get("target")),
            ("Seed", first.configuration.get("optimization_request", {}).get("seed"),
                     second.configuration.get("optimization_request", {}).get("seed")),
        )
        differences = tuple(label for label, a, b in fields if a != b)
        if (first_outcome.indexed_phase, first_outcome.process_state,
                first_outcome.exit_code, first_outcome.trial_status) != (
                second_outcome.indexed_phase, second_outcome.process_state,
                second_outcome.exit_code, second_outcome.trial_status):
            differences += ("Recorded outcome",)
        return TrainingExperimentComparison(first, second, differences,
            first_outcome, second_outcome)

    def clone(self, run_id, expected_revision, name, *, should_cancel=None):
        reviewed = self.review(run_id, expected_revision, should_cancel=should_cancel)
        if should_cancel is not None and should_cancel():
            raise InterruptedError("Experiment draft creation cancelled.")
        # Recheck the index immediately before the canonical draft write. The
        # frozen manifest is a starting point; the candidate planner rechecks
        # live sources before any later package or training preparation.
        if self.index.snapshot()[1] != expected_revision:
            raise TrainingRunIndexError("The selected run changed. Refresh it before cloning.")
        store = TrainingPlanDraftStore(self.draft_store_path)
        return store.save_manifest(reviewed.candidate_manifest, name).draft
