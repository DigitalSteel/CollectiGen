"""Shared record layout compatible with supported Python 3.10+ runtimes."""

from __future__ import annotations

import copy
from dataclasses import fields
from functools import lru_cache


class ImmutableRecordField:
    """Marker for immutable field values that need fresh mutable record copies."""

    __slots__ = ()

    def fresh_mutable_copy(self):
        raise NotImplementedError


@lru_cache(maxsize=None)
def _record_field_names(record_type: type) -> tuple[str, ...]:
    return tuple(item.name for item in fields(record_type))


class WeakReferenceableRecord:
    """Retain weak-reference support without an instance attribute dictionary."""

    __slots__ = ("__weakref__",)

    def __deepcopy__(self, memo):
        """Copy dataclass fields while thawing immutable field sentinels.

        An immutable sentinel is intentionally shared in the staged record. It
        must bypass deepcopy's normal memo for each field so a mutable record
        copy never aliases its empty containers. Ordinary values retain normal
        deepcopy memo semantics, including intentional aliases and cycles.
        """
        existing=memo.get(id(self))
        if existing is not None:
            return existing
        result=type(self).__new__(type(self))
        memo[id(self)]=result
        for name in _record_field_names(type(self)):
            value=getattr(self,name)
            copied=(
                value.fresh_mutable_copy()
                if isinstance(value,ImmutableRecordField)
                else copy.deepcopy(value,memo)
            )
            object.__setattr__(result,name,copied)
        return result
