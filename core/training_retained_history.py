"""Index only exact, service-retained training reviews named by their owners."""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from .training_run_index import TrainingRunIndexError, TrainingRunRecord, _read_optional


def _review(prepared, kind):
    if kind == "anima":
        from .training_anima import AnimaPrepared, AnimaTrainingService
        if type(prepared) is not AnimaPrepared:
            raise TrainingRunIndexError("Choose an exact retained Anima preparation.")
        path = prepared.preparation_root / "anima-review.json"
        raw = _read_optional(path, limit=256 * 1024)
        if raw is None:
            raise TrainingRunIndexError("The retained Anima review is unavailable.")
        restored, process, _ = AnimaTrainingService.open_review(path)
        if restored != prepared or _read_optional(path, limit=256 * 1024) != raw:
            raise TrainingRunIndexError("The retained Anima review changed during indexing.")
        return path, sha256(raw).hexdigest(), process
    if kind == "validation":
        from .training_validation import PreparedValidation, restore_prepared
        if (type(prepared) is not PreparedValidation or prepared.review_path is None
                or not prepared.review_sha256):
            raise TrainingRunIndexError("Choose an exact retained validation preparation.")
        restored = restore_prepared(prepared.review_path, prepared.review_sha256)
        if restored != prepared:
            raise TrainingRunIndexError("The retained validation review changed during indexing.")
        return prepared.review_path, prepared.review_sha256, None
    raise TrainingRunIndexError("The retained review kind is unsupported.")


def upsert_retained_review(index, kind, payload):
    """Restore the supplied exact review off UI, then CAS its pointer into history.

    The index is navigation state. Native result verification still belongs to the
    respective service when the user opens the saved review.
    """
    if type(payload) is not dict or set(payload) != {"prepared", "outcome", "registered"}:
        raise TrainingRunIndexError("The retained review update is incomplete.")
    prepared, outcome, registered = (payload[key] for key in ("prepared", "outcome", "registered"))
    if type(registered) is not bool or registered and kind != "anima":
        raise TrainingRunIndexError("The retained review registration state is invalid.")
    if registered and outcome is None:
        raise TrainingRunIndexError("A registered Anima review needs its stopped process evidence.")
    path, digest, restored_process = _review(prepared, kind)
    plan = prepared.plan
    plan.validate()
    if kind == "anima":
        from .training_process import TrainingProcessResult
        if outcome is not None and type(outcome) is not TrainingProcessResult:
            raise TrainingRunIndexError("The Anima process outcome is invalid.")
        if outcome is not None and (restored_process is None
                or outcome.destination != plan.destination
                or outcome.journal_tip != restored_process.journal_tip):
            raise TrainingRunIndexError("The retained Anima process changed before indexing.")
        effective = restored_process
        phase = "completed" if registered else (
            "verification_required" if effective and effective.status == "exited_unverified"
            else effective.status if effective else "prepared")
        tip = effective.journal_tip if effective else ""
    else:
        from .training_validation import TrainingValidationOutcome
        if outcome is not None and (type(outcome) is not TrainingValidationOutcome
                or outcome.process is None or outcome.process.destination != plan.destination):
            raise TrainingRunIndexError("The validation process outcome is invalid.")
        phase = (("completed" if outcome.status == "complete" else
                  "verification_required" if outcome.status == "exited_unverified" else outcome.status)
                 if outcome else "prepared")
        tip = outcome.process.journal_tip if outcome else ""
    record = TrainingRunRecord(plan.run_id, kind, str(Path(path).absolute()), digest,
        plan.fingerprint, str(plan.destination), phase=phase, latest_journal_tip=tip)
    record.validate()
    for _attempt in range(3):
        records, revision = index.snapshot()
        prior = next((row for row in records if row.run_id == plan.run_id), None)
        if prior is not None:
            if (prior.kind, prior.review_path, prior.review_sha256,
                    prior.plan_fingerprint, prior.destination) != (record.kind, record.review_path,
                    record.review_sha256, record.plan_fingerprint, record.destination):
                raise TrainingRunIndexError("The run identity already points to another retained review.")
            if prior.phase == phase and prior.latest_journal_tip == tip:
                return prior
            # A delayed prepared notification must never downgrade a newer outcome.
            if outcome is None and prior.phase != "prepared":
                return prior
            try:
                index.update_phase(plan.run_id, phase, revision, latest_journal_tip=tip)
            except TrainingRunIndexError as exc:
                if "changed after review" in str(exc):
                    continue
                raise
            return replace(prior, phase=phase, latest_journal_tip=tip)
        try:
            index.register(record, revision)
        except TrainingRunIndexError as exc:
            if "changed after review" in str(exc):
                continue
            raise
        return record
    raise TrainingRunIndexError("Training history changed repeatedly. Open History and retry indexing.")


def check_retained_pointer(index, record, revision):
    """Recheck one selected index revision and its exact review bytes off UI."""
    rows, current = index.snapshot()
    if current != revision or record not in rows:
        raise TrainingRunIndexError("Training history changed. Refresh and select the review again.")
    record.validate(stored=True)
    limit = 256 * 1024
    raw = _read_optional(Path(record.review_path), limit=limit)
    if raw is None or sha256(raw).hexdigest() != record.review_sha256:
        raise TrainingRunIndexError("The selected retained review changed or is unavailable.")
    if record.kind == "anima":
        from .training_anima import AnimaTrainingService
        prepared, _process, _qualification = AnimaTrainingService.open_review(record.review_path)
    elif record.kind == "validation":
        from .training_validation import restore_prepared
        prepared = restore_prepared(record.review_path, record.review_sha256)
    else:
        raise TrainingRunIndexError("The selected retained review kind is unsupported.")
    if (prepared.plan.run_id != record.run_id
            or prepared.plan.fingerprint != record.plan_fingerprint
            or str(prepared.plan.destination) != record.destination
            or _read_optional(Path(record.review_path), limit=limit) != raw):
        raise TrainingRunIndexError("The selected retained review does not match its saved run identity.")
    return record
