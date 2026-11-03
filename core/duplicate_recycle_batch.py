"""Exact duplicate-recycle accounting; canonical services still own each write."""
from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
from typing import Callable

from .verified_transfer import TransferRecoveryRequired


@dataclass(frozen=True)
class RecycleFamilyIdentity:
    identity: str
    path: str


@dataclass(frozen=True)
class UnconfirmedRecycleFamily:
    family: RecycleFamilyIdentity
    error: str
    recovery_required: bool = False


@dataclass(frozen=True)
class DuplicateRecycleOutcome:
    moved: tuple[RecycleFamilyIdentity, ...] = ()
    unconfirmed: tuple[UnconfirmedRecycleFamily, ...] = ()
    unattempted: tuple[RecycleFamilyIdentity, ...] = ()
    stop_reason: str = ""

    @property
    def recovery_required(self) -> bool:
        return any(item.recovery_required for item in self.unconfirmed)


def family_identities(plans) -> tuple[RecycleFamilyIdentity, ...]:
    return tuple(RecycleFamilyIdentity(str(record.identity),str(record.path))
                 for record,_plan in plans)


def missing_duplicate_recycle_outcome(families, *, invoked: bool, message: str):
    """Do not manufacture no-change evidence when an invoked receipt is absent."""
    if not invoked:
        return DuplicateRecycleOutcome(unattempted=tuple(families),stop_reason=message)
    return DuplicateRecycleOutcome(
        unconfirmed=tuple(UnconfirmedRecycleFamily(family,message) for family in families),
        stop_reason=message,
    )


def apply_duplicate_recycle_batch(
    plans, *, apply: Callable, checkpoint: Callable[[],bool],
    authority_current: Callable[[],bool], progress: Callable,
    final_outcome: Future,
) -> DuplicateRecycleOutcome:
    """Publish immutable final accounting even when the stock job loses its result.

    The caller owns a fresh Future and reads it only after terminal delivery.
    It is not cancelled or populated by the UI. Exceptions from an invoked writer
    remain unconfirmed;only a returned success becomes known moved. This does not
    replace per-family verification,journals,recovery or process-crash persistence.
    """
    plans=tuple(plans);families=family_identities(plans)
    moved=[];unconfirmed=[];attempted=set();stop_reason=""
    try:
        if len({family.identity for family in families})!=len(families):
            raise ValueError("The accepted batch contains a repeated family identity.")
        for index,((record,plan),family) in enumerate(zip(plans,families)):
            if not checkpoint():
                stop_reason="Stopped before another family was attempted."
                break
            if not authority_current():
                stop_reason="Frozen scope changed;no further family was attempted."
                break
            attempted.add(index)
            try:
                apply(plan)
            except TransferRecoveryRequired as error:
                unconfirmed.append(UnconfirmedRecycleFamily(family,str(error),True))
                stop_reason="Recovery requires attention before another family."
                break
            except InterruptedError as error:
                unconfirmed.append(UnconfirmedRecycleFamily(family,str(error) or "Interrupted inside the family operation."))
                stop_reason="Interrupted family outcome requires review."
                break
            except Exception as error:
                unconfirmed.append(UnconfirmedRecycleFamily(family,str(error)))
            else:
                moved.append(family)
            progress(index+1,len(plans),f"Reviewed Recycle Bin outcome for {record.path.name}")
    except Exception as error:
        # Includes admission/currentness/progress failures outside an invocation.
        # Earlier known successes stay in the final accounting.
        stop_reason=str(error) or type(error).__name__
    finally:
        outcome=DuplicateRecycleOutcome(
            tuple(moved),tuple(unconfirmed),
            tuple(family for index,family in enumerate(families) if index not in attempted),
            stop_reason,
        )
        final_outcome.set_result(outcome)
    return outcome
