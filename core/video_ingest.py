"""Receipt-bound, source-preserving return of verified MP4 results to Ingest."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence

from .edit_service import MoveHistoryService
from .external_write_policy import ExternalWriteBoundary, ExternalWritePolicy
from .library_profiles import (
    LibraryBoundaryDecision, LibraryPrivacyPolicy, LibraryProfile,
    LibraryProfileRegistry, LibraryRole,
)
from .video_asset import VideoAssetRecord, _redirected, companion_path
from .video_generation import VideoSourceAsset
from .verified_transfer import TransferRecoveryRequired


_SHA = re.compile(r"[0-9a-f]{64}\Z")
VerifiedOutputs = Callable[[Path], Sequence[Mapping[str, Any]]]
CancelCallback = Callable[[], bool]


def _sha256(path: Path, cancel: CancelCallback) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            if cancel():
                raise InterruptedError("Video Ingest transfer cancelled during verification.")
            digest.update(chunk)
    if cancel():
        raise InterruptedError("Video Ingest transfer cancelled during verification.")
    return digest.hexdigest()


def _fingerprint(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _descriptor(
    verified_outputs: VerifiedOutputs, receipt: Path, source: Path,
    cancel: CancelCallback,
) -> dict[str, Any]:
    if not receipt.is_absolute() or _redirected(receipt) or not receipt.is_file():
        raise ValueError("The selected output receipt is missing or redirected.")
    values = verified_outputs(receipt)
    if cancel():
        raise InterruptedError("Video Ingest transfer cancelled after receipt verification.")
    matches = [dict(value) for value in values if Path(value.get("path", "")) == source]
    if len(matches) != 1:
        raise ValueError("The selected MP4 is not one exact verified output of this receipt.")
    value = deepcopy(matches[0])
    if value.get("media_type") != "video" or value.get("review_status") != "needs_review":
        raise ValueError("The selected output is not an unreviewed verified video.")
    if not source.is_absolute() or source.suffix.casefold() != ".mp4" or _redirected(source) or not source.is_file():
        raise ValueError("The selected output MP4 is missing, redirected, or not absolute.")
    sha = value.get("sha256")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise ValueError("The output descriptor needs a full file hash.")
    if type(value.get("byte_length")) is not int or value["byte_length"] <= 0:
        raise ValueError("The output descriptor needs an exact byte length.")
    asset = VideoSourceAsset.from_dict(value["source_asset"])
    if asset.sha256 != sha or asset.byte_length != value["byte_length"]:
        raise ValueError("The typed temporal asset differs from output bytes.")
    identity = value.get("output_receipt")
    if not isinstance(identity, dict) or Path(identity.get("path", "")) != receipt:
        raise ValueError("The output descriptor does not identify this durable receipt.")
    receipt_sha = identity.get("sha256")
    if not isinstance(receipt_sha, str) or not _SHA.fullmatch(receipt_sha):
        raise ValueError("The output receipt identity needs a full SHA-256 hash.")
    if _sha256(receipt, cancel) != receipt_sha:
        raise ValueError("The durable output receipt changed after verification.")
    generation = value.get("generation")
    if not isinstance(generation, dict) or not all(
        generation.get(key) for key in ("job_id", "prompt_id", "request_fingerprint", "graph_fingerprint")
    ):
        raise ValueError("The output descriptor lacks exact job and graph lineage.")
    if not all(isinstance(generation[key], str) and _SHA.fullmatch(generation[key])
               for key in ("request_fingerprint", "graph_fingerprint")):
        raise ValueError("The output descriptor has invalid request or graph identity.")
    for key in ("request", "resource_bindings", "raw_runtime_history"):
        if key not in generation:
            raise ValueError(f"The output descriptor lacks {key} evidence.")
    bindings = generation["resource_bindings"]
    if not isinstance(bindings, list) or not bindings or any(
        not isinstance(binding, dict) or not isinstance(binding.get("role"), str)
        or not binding["role"] or not isinstance(binding.get("sha256"), str)
        or not _SHA.fullmatch(binding["sha256"])
        for binding in bindings
    ):
        raise ValueError("The output descriptor needs full resource hashes and roles.")
    if "source_image" not in generation or (
        generation["source_image"] is not None
        and (not isinstance(generation["source_image"], dict)
             or "original_lineage" not in generation["source_image"])
    ):
        raise ValueError("The output descriptor lacks the source-image lineage snapshot.")
    VideoAssetRecord.from_descriptor(value)
    return value


@dataclass(frozen=True)
class VideoIngestPlan:
    receipt: Path
    source: Path
    destination: Path
    descriptor_fingerprint: str
    source_sha256: str
    byte_length: int
    boundary_decision: LibraryBoundaryDecision
    external_boundary: ExternalWriteBoundary
    source_profile_ids: tuple[str, ...]
    destination_profile_id: str
    issues: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class VideoIngestResult:
    status: str
    destination: Path
    issues: tuple[str, ...] = ()
    operation_id: str = ""

    @property
    def message(self) -> str:
        return " ".join(self.issues) or {
            "transferred": "The verified video family was copied into Ingest for review.",
            "already_present": "The exact verified video family is already in Ingest.",
            "recovery_required": "The video transfer needs recovery from its journal.",
        }.get(self.status, "The video transfer did not complete.")


class VideoIngestService:
    """Use canonical output verification plus the shared journaled family copy."""

    def __init__(
        self, verified_outputs: VerifiedOutputs, moves: MoveHistoryService, *,
        library_registry: LibraryProfileRegistry,
        external_write_policy: ExternalWritePolicy,
    ) -> None:
        if not callable(verified_outputs):
            raise ValueError("Video Ingest requires the canonical verified-output callback.")
        self.verified_outputs = verified_outputs
        self.moves = moves
        self.library_registry = library_registry
        self.external_write_policy = external_write_policy

    def preview(
        self, receipt_path: str | Path, source_path: str | Path,
        ingest_root: str | Path, *, should_cancel: CancelCallback | None = None,
    ) -> VideoIngestPlan:
        cancel = should_cancel or (lambda: False)
        receipt = Path(receipt_path).expanduser().absolute()
        source = Path(source_path).expanduser().absolute()
        root = Path(ingest_root).expanduser().absolute()
        target = root / source.name
        source_profile = self.library_registry.profile_for_path(source)
        destination_profile = (
            self.library_registry.profile_for_path(root)
            or LibraryProfile.create("Ingest folder", root, LibraryRole.LOCAL_ONLY,
                                     stable_id="ingest-unclassified-local")
        )
        boundary = LibraryPrivacyPolicy.movement_for_paths(
            self.library_registry, (source,), destination_profile,
        )
        external_boundary = self.external_write_policy.boundary_for_pairs(
            self.library_registry, ((source, target),), backup_enabled=False,
            artifact_label="verified video family", workflow_label="video Ingest transfer",
        )
        source_profile_ids = (source_profile.stable_id,) if source_profile is not None else ()
        try:
            if _redirected(root) or not root.is_dir():
                raise ValueError("The selected Ingest folder is missing or redirected.")
            if (_redirected(target) or _redirected(companion_path(target))
                    or os.path.lexists(target) and not target.is_file()
                    or os.path.lexists(companion_path(target)) and not companion_path(target).is_file()):
                raise ValueError("The Ingest video family has redirected or unsupported entries.")
            if source == target:
                raise ValueError("The retained output is already at the selected Ingest path.")
            value = _descriptor(self.verified_outputs, receipt, source, cancel)
            if source.stat().st_size != value["byte_length"] or _sha256(source, cancel) != value["sha256"]:
                raise ValueError("The output MP4 changed after its verified receipt.")
            expected = VideoAssetRecord.from_descriptor(value)
            if companion_path(source).exists() or companion_path(source).is_symlink():
                current = VideoAssetRecord.read(source)
                if not current.supported or any(
                    current.payload.get(key) != expected.payload.get(key)
                    for key in ("sha256", "byte_length", "source_asset", "generation", "output_receipt")
                ):
                    raise ValueError("The retained video companion differs from the output receipt.")
            if target.exists() or companion_path(target).exists():
                if not self._same_target(target, expected, value["sha256"], cancel):
                    raise FileExistsError("The Ingest filename is occupied by a different video family.")
            return VideoIngestPlan(receipt, source, target, _fingerprint(value),
                                   value["sha256"], value["byte_length"],
                                   boundary, external_boundary, source_profile_ids,
                                   destination_profile.stable_id)
        except InterruptedError:
            raise
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return VideoIngestPlan(receipt, source, target, "", "", 0,
                                   boundary, external_boundary, source_profile_ids,
                                   destination_profile.stable_id, (str(exc),))

    @staticmethod
    def _same_target(target: Path, expected: VideoAssetRecord, sha: str,
                     cancel: CancelCallback) -> bool:
        if not target.is_file() or target.is_symlink() or not companion_path(target).is_file():
            return False
        if _sha256(target, cancel) != sha:
            return False
        current = VideoAssetRecord.read(target)
        return current.supported and all(
            current.payload.get(key) == expected.payload.get(key)
            for key in ("sha256", "byte_length", "source_asset", "generation", "output_receipt")
        )

    def transfer(
        self, plan: VideoIngestPlan, *, boundary_confirmed: bool = False,
        should_cancel: CancelCallback | None = None,
    ) -> VideoIngestResult:
        cancel = should_cancel or (lambda: False)
        if not plan.ready:
            return VideoIngestResult("failed", plan.destination, plan.issues)
        if (plan.boundary_decision.blocked_by_default
                or plan.external_boundary.confirmation_required) and not boundary_confirmed:
            return VideoIngestResult(
                "failed", plan.destination,
                ("This exact video transfer crosses a Local Only boundary and needs confirmation.",),
            )
        current = self.preview(plan.receipt, plan.source, plan.destination.parent, should_cancel=cancel)
        if not current.ready or current != plan:
            return VideoIngestResult("failed", plan.destination,
                                     current.issues or ("The video transfer preview is stale; review it again.",))
        value = _descriptor(self.verified_outputs, plan.receipt, plan.source, cancel)
        expected = VideoAssetRecord.from_descriptor(value)
        if self._same_target(plan.destination, expected, plan.source_sha256, cancel):
            return VideoIngestResult("already_present", plan.destination)
        try:
            source_companion = companion_path(plan.source)
            if not source_companion.exists():
                expected.write_new(plan.source)
            current_companion = VideoAssetRecord.read(plan.source)
            if not current_companion.supported or any(
                current_companion.payload.get(key) != expected.payload.get(key)
                for key in ("sha256", "byte_length", "source_asset", "generation", "output_receipt")
            ):
                raise ValueError("The retained video companion changed after review.")
            if _sha256(plan.source, cancel) != plan.source_sha256:
                raise ValueError("The output MP4 changed after its verified receipt.")
            companion_sha = _sha256(source_companion, cancel)
            transfer = self.moves.preview(
                plan.source, plan.destination.parent,
                collision_policy="error", force_copy=True, preserve_source=True,
                should_cancel=cancel,
            )
            family = {member.source for member in transfer.members}
            if not transfer.ready or family != {plan.source, companion_path(plan.source)}:
                raise ValueError("The shared transfer cannot verify the complete video family: "
                                 + " ".join(transfer.errors))
            transfer = replace(transfer, expected_hashes=(
                (str(plan.source), plan.source_sha256),
                (str(source_companion), companion_sha),
            ))
            record = self.moves.copy(
                plan.source, plan.destination.parent,
                reason="verified video result to Ingest", preview=transfer,
                collision_policy="error", should_cancel=cancel,
                boundary_decision=plan.boundary_decision,
                boundary_confirmed=boundary_confirmed,
                source_profile_ids=plan.source_profile_ids,
                destination_profile_id=plan.destination_profile_id,
            )
            # The shared copy has committed. Finish reporting its real outcome
            # even if cancellation arrives while this final read-back runs.
            try:
                verified = self._same_target(
                    plan.destination, expected, plan.source_sha256, lambda: False,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                raise TransferRecoveryRequired(
                    "The Ingest video family needs recovery after copy verification."
                ) from exc
            if not verified:
                raise TransferRecoveryRequired("The Ingest video family needs recovery after copy verification.")
            return VideoIngestResult("transferred", plan.destination,
                                     operation_id=record.operation_id)
        except InterruptedError:
            raise
        except TransferRecoveryRequired as exc:
            return VideoIngestResult("recovery_required", plan.destination, (str(exc),))
        except (OSError, ValueError, RuntimeError) as exc:
            return VideoIngestResult("failed", plan.destination, (str(exc),))
