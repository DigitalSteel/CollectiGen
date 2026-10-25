from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .ai_interrogation import hash_image, split_plain_language, _preparation_record, _preparation_write_guard
from .history_versions import HistoryError
from .history_configuration import HistoryConfigurationError
from .image_knowledge_history import HistoryWriteInterrupted
from .annotation import AIEvidence, ImageAnnotation
from .confirmed_tag_identity import ConfirmedTagIdentityService
from .image_record import ImageRecord
from .image_fact_decisions import ImageFindingDecisionService
from .operation_journal import JournalOperation, OperationJournal
from .settings import normalize_plain_language_exclusion


class PreferredCaptionPreparationStaleError(RuntimeError):
    """The Image Knowledge used by a preferred-caption preview changed."""


@dataclass
class PreferredCaptionPreparationItem:
    image: Path
    fingerprint: str
    status: str
    detail: str = ""
    preferred_evidence_id: str = ""
    preferred_model_id: str = ""
    preferred_model_revision: str = ""
    preferred_sentences: tuple[str, ...] = ()
    prior_evidence_id: str = ""
    prior_model_id: str = ""
    prior_model_revision: str = ""
    error: str = ""


@dataclass
class PreferredCaptionPreparationPreview:
    preferred_model_id: str = ""
    preferred_model_revision: str = ""
    items: list[PreferredCaptionPreparationItem] = field(default_factory=list)
    cancelled: bool = False

    def count(self, status: str) -> int:
        return sum(item.status == status for item in self.items)

    @property
    def fill_empty(self) -> int:
        return self.count("fill_empty")

    @property
    def upgrade_prior(self) -> int:
        return self.count("upgrade_prior")

    @property
    def already_preferred(self) -> int:
        return self.count("already_preferred")

    @property
    def needs_review(self) -> int:
        return self.count("needs_review")

    @property
    def unavailable(self) -> int:
        return self.count("unavailable")

    @property
    def failed(self) -> int:
        return self.count("failed")

    @property
    def actionable(self) -> int:
        return self.fill_empty + self.upgrade_prior


@dataclass(frozen=True)
class PreferredCaptionPreparationApplyItem:
    image: Path
    status: str
    mode: str = ""
    error: str = ""


@dataclass
class PreferredCaptionPreparationApplyResult:
    operation_id: str = ""
    items: list[PreferredCaptionPreparationApplyItem] = field(default_factory=list)
    cancelled: bool = False
    history_operation_id: str = ""
    history_interrupted: bool = False

    def count(self, status: str) -> int:
        return sum(item.status == status for item in self.items)

    @property
    def changed(self) -> int:
        return self.count("changed")

    @property
    def held(self) -> int:
        return self.count("held")

    @property
    def already_current(self) -> int:
        return self.count("already_current")

    @property
    def stale(self) -> int:
        return self.count("stale")

    @property
    def failed(self) -> int:
        return self.count("failed")

    @property
    def not_processed(self) -> int:
        return self.count("not_processed")


def preferred_caption_preparation_fingerprint(
    image: str | Path, *, image_digest: str = ""
) -> str:
    """Fingerprint every input that can define the loaded image record."""
    image = Path(image)
    digest = hashlib.sha256()
    digest.update(f"image:{image}:".encode("utf-8"))
    try:
        digest.update((image_digest or hash_image(image)).encode("ascii"))
    except OSError:
        digest.update(b"missing")
    for path, label in (
        (image.with_suffix(".tags.json"), "sidecar"),
        (image.with_suffix(".txt"), "caption"),
    ):
        digest.update(f"{label}:{path}:".encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"missing")
    return digest.hexdigest()


class PreferredCaptionPreparationService:
    """Safely prepare confirmed prose from one explicitly preferred captioner.

    Only portable ``.tags.json`` Image Knowledge is changed. Raw AI evidence,
    training TXT, embedded Metadata, and image files remain untouched.
    """

    OPERATION_KIND = "preferred_caption_preparation"
    UNDO_KIND = "preferred_caption_preparation_undo"
    POLICY_ID = "preferred_captioner_exact_attribution"
    POLICY_VERSION = 1

    def __init__(
        self,
        journal: OperationJournal,
        preferred_identity_provider: Callable[[ImageRecord], dict],
        *,
        ignored_plain_language: Iterable[str] = (),
    ) -> None:
        self.journal = journal
        self.preferred_identity_provider = preferred_identity_provider
        self.ignored_plain_language = {
            normalize_plain_language_exclusion(value)
            for value in ignored_plain_language
            if normalize_plain_language_exclusion(value)
        }

    def preview(
        self,
        images: Iterable[str | Path],
        *,
        should_cancel: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
        history_operation=None,
    ) -> PreferredCaptionPreparationPreview:
        paths = tuple(dict.fromkeys(Path(value) for value in images))
        should_cancel = should_cancel or (lambda: False)
        result = PreferredCaptionPreparationPreview()
        for index, image in enumerate(paths, 1):
            if should_cancel():
                result.cancelled = True
                break
            if history_operation is not None:
                _preparation_record(image, history_operation)
            item = self._preview_image(image)
            result.items.append(item)
            if item.preferred_model_id and not result.preferred_model_id:
                result.preferred_model_id = item.preferred_model_id
                result.preferred_model_revision = item.preferred_model_revision
            if progress is not None:
                progress(index, len(paths), image.name)
        return result

    def apply(
        self,
        preview: PreferredCaptionPreparationPreview,
        *,
        should_cancel: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
        mutation_callback: Callable[[Path,dict,dict,bool,str],None] | None = None,
        history_operation=None,
    ) -> PreferredCaptionPreparationApplyResult:
        should_cancel = should_cancel or (lambda: False)
        candidates = [
            item
            for item in preview.items
            if item.status in {"fill_empty", "upgrade_prior"}
        ]
        result = PreferredCaptionPreparationApplyResult()
        if history_operation is not None:
            result.history_operation_id = history_operation.operation_id
        if not candidates:
            result.items.extend(self._non_action_result(item) for item in preview.items)
            return result

        operation_id = history_operation.operation_id if history_operation is not None else self.journal.begin(
            self.OPERATION_KIND,
            expected=len(candidates),
            reason="Prepare selected images with the preferred captioner",
            metadata={
                "preferred_model_id": preview.preferred_model_id,
                "preferred_model_revision": preview.preferred_model_revision,
                "policy_id": self.POLICY_ID,
                "policy_version": self.POLICY_VERSION,
                "writes": "image_knowledge_only",
            },
        )
        if history_operation is None:
            result.operation_id = operation_id
        terminal_status = "complete"

        def native_stop(position, item, exc):
            result.cancelled = isinstance(exc, InterruptedError)
            result.history_interrupted = not result.cancelled
            result.items.append(PreferredCaptionPreparationApplyItem(
                item.image, "not_processed" if result.cancelled else "interrupted", item.status, str(exc)))
            for remaining in preview.items[position + 1:]:
                result.items.append(PreferredCaptionPreparationApplyItem(remaining.image, "not_processed")
                    if remaining.status in {"fill_empty", "upgrade_prior"} else self._non_action_result(remaining))
            return result

        for position, item in enumerate(preview.items):
            if item.status not in {"fill_empty", "upgrade_prior"}:
                result.items.append(self._non_action_result(item))
                continue
            if should_cancel():
                result.cancelled = True
                terminal_status = "cancelled"
                for remaining in preview.items[position:]:
                    if remaining.status in {"fill_empty", "upgrade_prior"}:
                        result.items.append(
                            PreferredCaptionPreparationApplyItem(
                                remaining.image,
                                "not_processed",
                                error="The batch was cancelled before this image was changed.",
                            )
                        )
                    else:
                        result.items.append(self._non_action_result(remaining))
                break
            if history_operation is not None:
                try:
                    _preparation_record(item.image, history_operation)
                except (HistoryError, HistoryConfigurationError, InterruptedError) as exc:
                    return native_stop(position, item, exc)
            if (
                preferred_caption_preparation_fingerprint(item.image)
                != item.fingerprint
            ):
                result.items.append(
                    PreferredCaptionPreparationApplyItem(
                        item.image,
                        "stale",
                        error="Image Knowledge changed after the preview.",
                    )
                )
                continue
            try:
                self._apply_one(
                    operation_id,item,mutation_callback=mutation_callback,
                    **({"history_operation": history_operation} if history_operation is not None else {}),
                )
            except PreferredCaptionPreparationStaleError as exc:
                result.items.append(
                    PreferredCaptionPreparationApplyItem(
                        item.image, "stale", item.status, str(exc)
                    )
                )
            except Exception as exc:
                if history_operation is not None and isinstance(exc, (HistoryError, HistoryConfigurationError, InterruptedError)):
                    return native_stop(position, item, exc)
                result.items.append(
                    PreferredCaptionPreparationApplyItem(
                        item.image, "failed", item.status, str(exc)
                    )
                )
            else:
                result.items.append(
                    PreferredCaptionPreparationApplyItem(
                        item.image, "changed", item.status
                    )
                )
            if progress is not None:
                progress(len(result.items), len(preview.items), item.image.name)

        if history_operation is None:
            self.journal.finish(operation_id, terminal_status)
            self.journal.prune()
        return result

    def undo(self, operation_id: str) -> PreferredCaptionPreparationApplyResult:
        operation = self._operation(operation_id)
        if (
            operation.kind != self.OPERATION_KIND
            or operation.status not in {"complete", "cancelled"}
            or not operation.changes
        ):
            raise ValueError("That preferred-description batch is not available to undo.")

        prepared = []
        for change in operation.changes:
            image = Path(change["item"])
            before = self.journal.load_snapshot(change["before"])
            after = self.journal.load_snapshot(change["after"])
            try:
                current = ImageRecord(image).annotation.to_dict()
            except Exception as exc:
                raise PreferredCaptionPreparationStaleError(
                    f"{image.name} could not be checked before undo: {exc}"
                ) from exc
            if current == before:
                continue
            if current != after:
                raise PreferredCaptionPreparationStaleError(
                    f"{image.name} changed after this batch, so it cannot be undone automatically."
                )
            prepared.append(
                (
                    image,
                    before,
                    after,
                    bool(change["metadata"].get("sidecar_existed", True)),
                )
            )

        undo_id = self.journal.begin(
            self.UNDO_KIND,
            expected=len(prepared),
            reason=f"Undo preferred-description batch {operation_id}",
            metadata={"restores": operation_id, "writes": "image_knowledge_only"},
        )
        restored = []
        result = PreferredCaptionPreparationApplyResult(operation_id=undo_id)
        try:
            for image, before, after, sidecar_existed in prepared:
                self._restore(image, before, sidecar_existed=sidecar_existed)
                self.journal.append_change(
                    undo_id,
                    item=str(image),
                    before=after,
                    after=before,
                    reason="Undo preferred-caption preparation",
                    metadata={"sidecar_existed": True},
                )
                restored.append((image, after))
                result.items.append(
                    PreferredCaptionPreparationApplyItem(image, "changed")
                )
        except Exception as exc:
            rollback_errors = []
            for image, after in reversed(restored):
                try:
                    self._restore(image, after, sidecar_existed=True)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{image.name}: {rollback_exc}")
            message = str(exc)
            if rollback_errors:
                message += "; automatic recovery also failed: " + "; ".join(
                    rollback_errors
                )
            self.journal.finish(undo_id, "failed", message=message)
            raise RuntimeError(message) from exc
        self.journal.finish(undo_id, "complete")
        self.journal.finish(operation_id, "undone")
        self.journal.prune()
        return result

    def recover_interrupted(self) -> list[str]:
        """Close interrupted operations without overwriting divergent records."""
        recovered = []
        for operation in self.journal.operations():
            if operation.kind != self.OPERATION_KIND or operation.status != "applying":
                continue
            divergent = []
            for change in operation.changes:
                image = Path(change["item"])
                before = self.journal.load_snapshot(change["before"])
                after = self.journal.load_snapshot(change["after"])
                try:
                    current = ImageRecord(image).annotation.to_dict()
                except Exception:
                    divergent.append(image.name)
                    continue
                if current not in (before, after):
                    divergent.append(image.name)
            if divergent:
                self.journal.finish(
                    operation.id,
                    "failed",
                    message=(
                        "Recovery stopped because Image Knowledge diverged for: "
                        + ", ".join(divergent)
                    ),
                )
            else:
                self.journal.finish(
                    operation.id,
                    "cancelled",
                    message=(
                        "Recovered an interrupted batch. Completed writes were "
                        "kept and remain available to undo."
                    ),
                )
            recovered.append(operation.id)
        return recovered

    def _preview_image(self, image: Path) -> PreferredCaptionPreparationItem:
        if not image.is_file():
            return PreferredCaptionPreparationItem(
                image,
                preferred_caption_preparation_fingerprint(image),
                "unavailable",
                error="The image is unavailable.",
            )
        try:
            record = ImageRecord(image)
            image_digest = hash_image(image)
            fingerprint = preferred_caption_preparation_fingerprint(
                image, image_digest=image_digest
            )
            identity = dict(self.preferred_identity_provider(record) or {})
        except Exception as exc:
            return PreferredCaptionPreparationItem(
                image,
                preferred_caption_preparation_fingerprint(image),
                "failed",
                error=str(exc),
            )
        model_id = str(identity.get("model_id") or "")
        revision = str(identity.get("model_revision") or "")
        base = {
            "image": image,
            "fingerprint": fingerprint,
            "preferred_model_id": model_id,
            "preferred_model_revision": revision,
        }
        if not model_id or identity.get("kind") != "plain_language":
            return PreferredCaptionPreparationItem(
                **base,
                status="failed",
                error="The selected preferred captioner has no valid Description identity.",
            )

        matching = [
            evidence
            for evidence in record.annotation.ai_evidence
            if self._matches_identity(evidence, identity, image_digest)
        ]
        if not matching:
            return PreferredCaptionPreparationItem(
                **base,
                status="needs_review",
                detail="Run the currently selected captioner for this image first.",
            )
        preferred = matching[-1]
        sentences = tuple(split_plain_language(preferred.typed_plain_language()))
        base.update(
            preferred_evidence_id=preferred.id,
            preferred_sentences=sentences,
        )
        if preferred.type_issues() or not sentences:
            return PreferredCaptionPreparationItem(
                **base,
                status="needs_review",
                detail="The preferred result is incomplete or has ambiguous data types.",
            )
        if preferred.decisions.get("plain_language") == "rejected":
            return PreferredCaptionPreparationItem(
                **base,
                status="needs_review",
                detail="The preferred result was previously rejected.",
            )
        if self._is_excluded(record, sentences):
            return PreferredCaptionPreparationItem(
                **base,
                status="needs_review",
                detail="Part of the preferred result was previously rejected or ignored.",
            )

        confirmed = tuple(record.annotation.plain_language)
        if confirmed == sentences:
            return PreferredCaptionPreparationItem(
                **base,
                status="already_preferred",
                detail="Confirmed Descriptions already match the preferred result.",
            )
        if not confirmed:
            if preferred.decisions.get("plain_language") == "accepted":
                return PreferredCaptionPreparationItem(
                    **base,
                    status="needs_review",
                    detail=(
                        "The preferred result was accepted before, but its confirmed "
                        "description was later removed."
                    ),
                )
            return PreferredCaptionPreparationItem(
                **base,
                status="fill_empty",
                detail="No confirmed Descriptions exist.",
            )

        prior_matches = [
            evidence
            for evidence in record.annotation.ai_evidence
            if (
                evidence.id != preferred.id
                and evidence.model_id != model_id
                and evidence.kind == "plain_language"
                and evidence.image_hash == image_digest
                and not evidence.type_issues()
                and evidence.decisions.get("plain_language") == "accepted"
                and tuple(split_plain_language(evidence.typed_plain_language()))
                == confirmed
            )
        ]
        if len(prior_matches) != 1:
            detail = (
                "Confirmed Descriptions are edited, mixed-source, or cannot be "
                "attributed to exactly one prior accepted captioner result."
            )
            return PreferredCaptionPreparationItem(
                **base, status="needs_review", detail=detail
            )
        prior = prior_matches[0]
        if preferred.decisions.get("plain_language") == "accepted":
            return PreferredCaptionPreparationItem(
                **base,
                status="needs_review",
                detail=(
                    "The preferred result was accepted before, but confirmed Plain "
                    "Language now matches a different source."
                ),
            )
        return PreferredCaptionPreparationItem(
            **base,
            status="upgrade_prior",
            detail="Confirmed Descriptions exactly match one prior captioner result.",
            prior_evidence_id=prior.id,
            prior_model_id=prior.model_id,
            prior_model_revision=prior.model_revision,
        )

    def _apply_one(
        self, operation_id: str, item: PreferredCaptionPreparationItem, *,
        mutation_callback: Callable[[Path,dict,dict,bool,str],None] | None=None,
        history_operation=None,
    ) -> None:
        record = _preparation_record(item.image, history_operation)
        before = copy.deepcopy(record.annotation.to_dict())
        sidecar_existed = record.sidecar_path.exists()
        evidence = next(
            (
                value
                for value in record.annotation.ai_evidence
                if value.id == item.preferred_evidence_id
            ),
            None,
        )
        if evidence is None:
            raise PreferredCaptionPreparationStaleError(
                "The preferred captioner result is no longer available."
            )
        if tuple(split_plain_language(evidence.typed_plain_language())) != tuple(
            item.preferred_sentences
        ):
            raise PreferredCaptionPreparationStaleError(
                "The preferred captioner result changed after preview."
            )

        record.annotation.plain_language = list(item.preferred_sentences)
        ImageFindingDecisionService(
            record.annotation,
        ).accept_confirmed_descriptions(record.annotation.plain_language)
        applied_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        record.annotation.source["preferred_caption_preparation"] = {
            "state": "provisionally_prepared",
            "policy_id": self.POLICY_ID,
            "policy_version": self.POLICY_VERSION,
            "mode": item.status,
            "operation_id": operation_id,
            "preferred_evidence_id": evidence.id,
            "preferred_model_id": evidence.model_id,
            "preferred_model_revision": evidence.model_revision,
            "prior_evidence_id": item.prior_evidence_id,
            "prior_model_id": item.prior_model_id,
            "prior_model_revision": item.prior_model_revision,
            "applied_at": applied_at,
        }
        record.annotation.source["latest_operation"] = {
            "id": operation_id,
            "kind": self.OPERATION_KIND,
        }
        ConfirmedTagIdentityService.prune_annotation(record.annotation)
        record.modified = True
        after = copy.deepcopy(record.annotation.to_dict())
        if mutation_callback is not None and history_operation is None:
            mutation_callback(
                item.image,before,after,sidecar_existed,
                "Prepare confirmed Descriptions from preferred captioner",
            )
        if history_operation is not None:
            with _preparation_write_guard(record.history_context, item.image):
                record.save_sidecar(force=True)
                try:
                    if ImageRecord(item.image).annotation.to_dict() != after:
                        raise RuntimeError("Saved preferred Description differs from its intended state.")
                except Exception as exc:
                    raise HistoryWriteInterrupted("Saved preferred Description needs explicit history recovery; its current record was retained.") from exc
            return
        self.journal.append_change(
            operation_id,
            item=str(item.image),
            before=before,
            after=after,
            reason="Prepare confirmed Descriptions from preferred captioner",
            metadata={
                "mode": item.status,
                "preferred_evidence_id": evidence.id,
                "sidecar_existed": sidecar_existed,
            },
        )
        wrote = False
        try:
            wrote = True
            record.save_sidecar(force=True)
            if ImageRecord(item.image).annotation.to_dict() != after:
                raise RuntimeError(
                    "Saved Image Knowledge did not match the preferred description."
                )
        except Exception:
            if wrote:
                try:
                    current = ImageRecord(item.image).annotation.to_dict()
                except Exception:
                    current = None
                if current != before:
                    self._restore(
                        item.image, before, sidecar_existed=sidecar_existed
                    )
            raise

    def _is_excluded(
        self, record: ImageRecord, sentences: tuple[str, ...]
    ) -> bool:
        excluded = set(self.ignored_plain_language)
        excluded.update(
            normalize_plain_language_exclusion(sentence)
            for raw in record.annotation.source.get("rejected_descriptions", [])
            for sentence in split_plain_language(raw)
            if normalize_plain_language_exclusion(sentence)
        )
        return any(
            normalize_plain_language_exclusion(value) in excluded
            for value in sentences
        )

    @staticmethod
    def _matching_settings(settings) -> dict:
        values = dict(settings or {})
        values.pop("execution_provider", None)
        return values

    @classmethod
    def _matches_identity(
        cls, evidence: AIEvidence, identity: dict, image_digest: str
    ) -> bool:
        return (
            evidence.image_hash == image_digest
            and evidence.model_id == identity.get("model_id")
            and evidence.model_revision == identity.get("model_revision", "")
            and evidence.kind == identity.get("kind")
            and evidence.prompt == identity.get("prompt", "")
            and cls._matching_settings(evidence.settings)
            == cls._matching_settings(identity.get("settings", {}))
        )

    @staticmethod
    def _non_action_result(
        item: PreferredCaptionPreparationItem,
    ) -> PreferredCaptionPreparationApplyItem:
        status = {
            "already_preferred": "already_current",
            "needs_review": "held",
            "unavailable": "stale",
            "failed": "failed",
        }.get(item.status, "held")
        return PreferredCaptionPreparationApplyItem(
            item.image, status, item.status, item.error or item.detail
        )

    def _operation(self, operation_id: str) -> JournalOperation:
        operation = next(
            (item for item in self.journal.operations() if item.id == operation_id),
            None,
        )
        if operation is None:
            raise ValueError("The requested Image Knowledge operation is unavailable.")
        return operation

    @staticmethod
    def _restore(
        image: Path,
        snapshot: dict,
        *,
        sidecar_existed: bool,
    ) -> None:
        sidecar = image.with_suffix(".tags.json")
        if not sidecar_existed:
            sidecar.unlink(missing_ok=True)
            return
        record = ImageRecord(image, auto_load=False)
        record.annotation = ImageAnnotation.from_dict(
            copy.deepcopy(snapshot), image.name
        )
        record.loaded = True
        record.modified = True
        record.save_sidecar(force=True)
        if ImageRecord(image).annotation.to_dict() != snapshot:
            raise RuntimeError(
                f"{image.name} could not be restored to its exact prior Image Knowledge."
            )
