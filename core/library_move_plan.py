from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING,Callable, Iterable

from .character_database import CharacterDatabase
from .image_knowledge_reconciliation import ImageKnowledgeReconciliationService, ReconciliationPreview, knowledge_fingerprint
from .image_record import ImageRecord
from .image_workflow_state import ImageWorkflowState, ImageWorkflowStateService
from .organization import OrganizationProfile, OrganizationRouter
from .library_profiles import (
    LibraryBoundaryDecision,LibraryPrivacyPolicy,LibraryProfile,
    LibraryProfileRegistry,LibraryRole,
)
from .share_safety import DestinationSafetyDecision,DestinationSafetyPreflight
from .library_storage import LibraryStorageInspector
from .verified_transfer import (
    LIBRARY_NAMING_IMAGE_HASH,LIBRARY_NAMING_KEEP,VerifiedTransferEngine,
    VerifiedTransferPlan,normalize_library_naming_policy,
)

if TYPE_CHECKING:
    from .library_move_protection import LibraryMoveProtectionPlan


def _signature(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns
    except OSError:
        return -1, -1


@dataclass
class LibraryMovePlanItem:
    image: Path
    state: ImageWorkflowState
    destination: Path
    route_confidence: float
    route_reasons: tuple[str, ...] = ()
    missing_levels: tuple[str, ...] = ()
    signatures: tuple[tuple[int, int], ...] = ()
    reconciliation: ReconciliationPreview | None = None
    errors: list[str] = field(default_factory=list)
    source_profile: LibraryProfile | None = None
    transfer: VerifiedTransferPlan | None = None
    safety: DestinationSafetyDecision | None = None
    skipped_levels: tuple[str, ...] = ()

    @property
    def route_ready(self) -> bool:
        return not self.missing_levels

    @property
    def ready(self) -> bool:
        return (
            self.state.ready and self.route_ready and not self.errors
            and (self.transfer is None or self.transfer.ready)
            and (self.safety is None or self.safety.ready)
        )

    @property
    def exact_duplicate(self) -> bool:
        return bool(self.transfer and self.transfer.duplicate_target is not None)


@dataclass
class LibraryMovePlan:
    items: list[LibraryMovePlanItem] = field(default_factory=list)
    destination_profile: LibraryProfile | None = None
    boundary_decision: LibraryBoundaryDecision | None = None
    naming_policy: str = LIBRARY_NAMING_KEEP
    protection: "LibraryMoveProtectionPlan | None" = None

    @property
    def ready(self) -> list[LibraryMovePlanItem]:
        return [item for item in self.items if item.ready]

    @property
    def pending(self) -> list[LibraryMovePlanItem]:
        return [item for item in self.items if not item.ready]

    @property
    def fallback(self) -> list[LibraryMovePlanItem]:
        return [item for item in self.items if not item.route_ready]

    @property
    def skipped(self) -> list[LibraryMovePlanItem]:
        return [item for item in self.items if item.skipped_levels]

    @property
    def duplicates(self) -> list[LibraryMovePlanItem]:
        return [item for item in self.items if item.exact_duplicate]

    @property
    def safety_holds(self) -> list[LibraryMovePlanItem]:
        return [item for item in self.items if item.safety is not None and not item.safety.ready]

    @property
    def safety_blocked(self) -> list[LibraryMovePlanItem]:
        return [item for item in self.items if item.safety is not None and item.safety.blocked]

    @property
    def safety_warnings(self) -> list[LibraryMovePlanItem]:
        return [item for item in self.items if item.safety is not None and item.safety.warning]

    @property
    def local_review(self) -> list[LibraryMovePlanItem]:
        return [
            item for item in self.items
            if item.safety is not None and item.safety.status=="ready_local_review"
        ]

    @property
    def required_bytes(self) -> int:
        return sum(
            item.transfer.required_bytes
            for item in self.items
            if item.transfer is not None and item.transfer.ready
        )


class LibraryMovePlanningService:
    """Build a no-write move and projection preview from authoritative state."""

    def __init__(
        self, working_root: str | Path, characters: CharacterDatabase,
        profile: OrganizationProfile | dict | None = None, *, knowledge=None,
        reconciliation: ImageKnowledgeReconciliationService | None = None,
        ignored_plain_language=(),
        destination_profile: LibraryProfile | None = None,
        library_registry: LibraryProfileRegistry | None = None,
        transfers=None,
        naming_policy: str = LIBRARY_NAMING_KEEP,
        categories=None,
    ) -> None:
        self.router = OrganizationRouter(Path(working_root), characters, profile)
        self.states = ImageWorkflowStateService(
            knowledge, characters, ignored_plain_language=ignored_plain_language,
        )
        self.reconciliation = reconciliation or ImageKnowledgeReconciliationService(
            knowledge=knowledge, characters=characters,
        )
        self.destination_profile=destination_profile
        self.library_registry=library_registry or LibraryProfileRegistry()
        self.transfers=transfers
        engine=getattr(transfers,"_engine",None)
        self.storage=getattr(engine,"storage",None) or LibraryStorageInspector()
        self.naming_policy=normalize_library_naming_policy(naming_policy)
        self.categories=categories
        self.safety=DestinationSafetyPreflight(knowledge,categories)

    @staticmethod
    def _same_bytes(
        first: Path, second: Path,
        should_cancel: Callable[[], bool] | None = None,
    ) -> bool:
        if first.stat().st_size != second.stat().st_size:
            return False
        with first.open("rb") as left, second.open("rb") as right:
            while True:
                if should_cancel is not None and should_cancel():
                    raise InterruptedError(
                        "Move preview cancelled safely while checking an exact duplicate."
                    )
                left_chunk=left.read(1024*1024)
                right_chunk=right.read(1024*1024)
                if left_chunk!=right_chunk:
                    return False
                if not left_chunk:
                    return True

    @classmethod
    def _same_family(
        cls, first: Path, second: Path,
        should_cancel: Callable[[], bool] | None = None,
    ) -> bool:
        first_files=VerifiedTransferEngine.related_files(first)
        second_files=VerifiedTransferEngine.related_files(second)
        first_by_suffix={path.name[len(first.stem):].casefold():path for path in first_files}
        second_by_suffix={path.name[len(second.stem):].casefold():path for path in second_files}
        return (
            set(first_by_suffix)==set(second_by_suffix)
            and all(
                cls._same_bytes(first_by_suffix[key],second_by_suffix[key],should_cancel)
                for key in first_by_suffix
            )
        )

    @staticmethod
    def signatures(image: Path) -> tuple[tuple[int, int], ...]:
        return tuple(_signature(path) for path in (
            image, image.with_suffix(".tags.json"), image.with_suffix(".txt"),
            image.with_suffix(".xmp"),
        ))

    def preview(
        self, images: Iterable[str | Path], *, include_metadata: bool = True,
        progress: Callable[[int, int, str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        include_projection_preview: bool = True,
    ) -> LibraryMovePlan:
        paths = [Path(path) for path in images]
        boundary=(
            LibraryPrivacyPolicy.movement_for_paths(
                self.library_registry,paths,self.destination_profile,
            )
            if self.destination_profile is not None else None
        )
        plan = LibraryMovePlan(
            destination_profile=self.destination_profile,
            boundary_decision=boundary,
            naming_policy=self.naming_policy,
        )
        planned_hashes: dict[str, tuple[Path, Path]] = {}
        for index, path in enumerate(paths, start=1):
            if should_cancel is not None and should_cancel():
                raise InterruptedError("Move preview cancelled safely before any files changed.")
            errors: list[str] = []
            source_profile=self.library_registry.profile_for_path(path)
            conflicts=self.storage.conflict_artifacts((path,)).get(path,())
            availability=self.storage.inspect_image(path,conflict_paths=conflicts)
            if not availability.ready_for_write:
                errors.append(
                    f"{availability.label}: {availability.detail} {availability.recovery}".strip()
                )
                state=ImageWorkflowState(
                    "problem","orange",availability.label,
                    availability.detail,0,False,(str(availability.state.value),),
                )
                plan.items.append(LibraryMovePlanItem(
                    path,state,self.router.root,0.0,(),("storage availability",),
                    self.signatures(path),None,errors,
                    source_profile,None,
                ))
                if progress:progress(index,len(paths),path.name)
                continue
            state = self.states.assess(path)
            safety = None
            try:
                record = ImageRecord(path)
                suggestion = self.router.suggest(record)
            except Exception as exc:
                suggestion = None
                errors.append(str(exc))
            reconciliation = None
            if include_projection_preview and path.with_suffix(".tags.json").is_file():
                try:
                    reconciliation = self.reconciliation.preview(
                        path, include_metadata=include_metadata,
                    )
                except Exception as exc:
                    errors.append(f"Projection preview: {exc}")
            elif not path.with_suffix(".tags.json").is_file():
                errors.append("Authoritative Image Knowledge is required before moving.")
            if suggestion is None:
                destination = self.router.root
                confidence = 0.0
                reasons: tuple[str, ...] = ()
                missing = ("destination",)
                skipped: tuple[str, ...] = ()
            else:
                destination = suggestion.destination
                confidence = suggestion.confidence
                reasons = tuple(suggestion.reasons)
                missing = tuple(suggestion.missing)
                skipped = tuple(getattr(suggestion,"skipped",()))
            if self.destination_profile is not None:
                try:
                    safety=self.safety.evaluate(
                        path,self.destination_profile.share_safety_policy,
                        should_cancel=should_cancel,
                        local_review_destination=(
                            self.destination_profile.recognized_role is LibraryRole.LOCAL_ONLY
                        ),
                        library_move=True,
                    )
                except InterruptedError:
                    raise
                except Exception as exc:
                    errors.append(f"Destination safety preview: {exc}")
            transfer = None
            already_at_destination=(
                suggestion is not None
                and destination.resolve()==path.parent.resolve()
            )
            if (
                self.transfers is not None and suggestion is not None
                and (
                    not already_at_destination
                    or self.naming_policy==LIBRARY_NAMING_IMAGE_HASH
                )
            ):
                try:
                    transfer=self.transfers.preview(
                        path,destination,collision_policy="error",
                        force_copy=LibraryPrivacyPolicy.requires_verified_copy(
                            source_profile,self.destination_profile,
                        ),
                        naming_policy=self.naming_policy,
                        should_cancel=should_cancel,
                    )
                    # A correctly hash-named family in its routed folder is its
                    # own exact duplicate. Keep that as a no-op, while allowing
                    # stale same-folder names to receive a verified rename plan.
                    if (
                        transfer.duplicate_target is not None
                        and transfer.duplicate_target.resolve()==path.resolve()
                    ):
                        transfer=None
                    if (
                        transfer is not None
                        and self.naming_policy==LIBRARY_NAMING_IMAGE_HASH
                        and transfer.image_sha256
                        and not transfer.errors
                        and transfer.duplicate_target is None
                    ):
                        previous=planned_hashes.get(transfer.image_sha256)
                        if previous is not None:
                            previous_source,previous_target=previous
                            same_image=self._same_bytes(path,previous_source,should_cancel)
                            if same_image and self._same_family(
                                path,previous_source,should_cancel,
                            ):
                                transfer=replace(
                                    transfer,duplicate_target=previous_target,
                                )
                            elif same_image:
                                transfer=replace(
                                    transfer,
                                    errors=transfer.errors+(
                                        "Conflicting sidecar family among selected images: "
                                        "identical image bytes have different recognized related files.",
                                    ),
                                )
                            else:
                                transfer=replace(
                                    transfer,
                                    errors=transfer.errors+(
                                        "Image-hash collision among selected images: "
                                        "the same full SHA-256 name represents different bytes.",
                                    ),
                                )
                        elif transfer.members:
                            planned_hashes[transfer.image_sha256]=(
                                path,transfer.members[0].target,
                            )
                    if transfer is not None:
                        errors.extend(transfer.errors)
                except InterruptedError:
                    raise
                except Exception as exc:
                    errors.append(f"Transfer preview: {exc}")
            plan.items.append(LibraryMovePlanItem(
                path, state, destination, confidence, reasons, missing,
                self.signatures(path), reconciliation, errors,
                source_profile,transfer,safety,skipped,
            ))
            if progress:
                progress(index, len(paths), path.name)
        return plan

    def is_current(self, item: LibraryMovePlanItem) -> bool:
        conflicts=self.storage.conflict_artifacts((item.image,)).get(item.image,())
        if not self.storage.inspect_image(
            item.image,conflict_paths=conflicts,
        ).ready_for_write:
            return False
        if self.signatures(item.image) != item.signatures:
            return False
        try:
            record=ImageRecord(item.image)
            if item.reconciliation and knowledge_fingerprint(record)!=item.reconciliation.knowledge_fingerprint:
                return False
            suggestion = self.router.suggest(record)
        except Exception:
            return False
        transfer_current = True
        if item.transfer is not None:
            transfer_current=all(
                _signature(member.source)==member.signature()
                and not member.target.exists()
                for member in item.transfer.members
            )
        safety_current=(
            item.safety is None
            or self.destination_profile is not None
            and self.safety.is_current(
                item.image,item.safety,self.destination_profile.share_safety_policy,
                local_review_destination=(
                    self.destination_profile.recognized_role is LibraryRole.LOCAL_ONLY
                ),
                library_move=True,
            )
        )
        return (
            suggestion.destination == item.destination
            and tuple(suggestion.missing) == item.missing_levels
            and tuple(getattr(suggestion,"skipped",())) == item.skipped_levels
            and transfer_current
            and safety_current
            and (item.safety is None or item.safety.ready)
        )
