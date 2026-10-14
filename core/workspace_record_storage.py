"""Memory-bounded empty containers for detached workspace Tag records."""
from __future__ import annotations

from .record_layout import ImmutableRecordField


class SharedReadOnlyEmptyList(ImmutableRecordField,list):
    """One list-compatible empty value that rejects content changes."""

    @staticmethod
    def _reject():
        raise TypeError("Shared workspace empty lists are read-only.")

    def __deepcopy__(self, memo):
        return self

    def fresh_mutable_copy(self):
        return []

    def append(self, value):
        self._reject()

    def extend(self, values):
        if tuple(values):
            self._reject()

    def insert(self, index, value):
        self._reject()

    def __setitem__(self, key, value):
        self._reject()

    def __delitem__(self, key):
        self._reject()

    def __iadd__(self, values):
        if tuple(values):
            self._reject()
        return self

    def __imul__(self, value):
        return self

    def clear(self):
        return None


class SharedReadOnlyEmptyDict(ImmutableRecordField,dict):
    """One dict-compatible empty value that rejects content changes."""

    @staticmethod
    def _reject():
        raise TypeError("Shared workspace empty dictionaries are read-only.")

    def __deepcopy__(self, memo):
        return self

    def fresh_mutable_copy(self):
        return {}

    def __setitem__(self, key, value):
        self._reject()

    def __delitem__(self, key):
        self._reject()

    def update(self, *args, **kwargs):
        if dict(*args, **kwargs):
            self._reject()

    def setdefault(self, key, default=None):
        self._reject()

    def pop(self, key, *default):
        if len(default)>1:
            raise TypeError(f"pop expected at most 2 arguments, got {1+len(default)}")
        if default:
            return default[0]
        raise KeyError(key)

    def popitem(self):
        raise KeyError("popitem(): dictionary is empty")

    def __ior__(self, other):
        if dict(other):
            self._reject()
        return self

    def clear(self):
        return None


SHARED_EMPTY_LIST=SharedReadOnlyEmptyList()
SHARED_EMPTY_DICT=SharedReadOnlyEmptyDict()

_TAG_LIST_FIELDS=(
    "aliases","attributes","implies","conflicts","provenance",
)
_TAG_DICT_FIELDS=("properties","extras","relation_extras")
_SENSITIVITY_LIST_FIELDS=("content_flags","provenance")
_ORDERING_LIST_FIELDS=("provenance",)
_NESTED_DICT_FIELDS=("extras",)


def _compact(owner, list_fields, dict_fields) -> None:
    for field in list_fields:
        value=getattr(owner,field)
        if isinstance(value,list) and not value:
            setattr(owner,field,SHARED_EMPTY_LIST)
    for field in dict_fields:
        value=getattr(owner,field)
        if isinstance(value,dict) and not value:
            setattr(owner,field,SHARED_EMPTY_DICT)


def _materialize(owner, list_fields, dict_fields) -> None:
    for field in list_fields:
        if getattr(owner,field) is SHARED_EMPTY_LIST:
            setattr(owner,field,[])
    for field in dict_fields:
        if getattr(owner,field) is SHARED_EMPTY_DICT:
            setattr(owner,field,{})


def compact_staged_tag_record(record, *, keep_relation_extras_mutable=False):
    """Share empty detached fields, optionally leaving authority edits writable."""
    dict_fields=(
        ("properties","extras") if keep_relation_extras_mutable else _TAG_DICT_FIELDS
    )
    _compact(record,_TAG_LIST_FIELDS,dict_fields)
    if record.allowed_modifier_groups == []:
        record.allowed_modifier_groups=SHARED_EMPTY_LIST
    _compact(
        record.content_sensitivity,
        _SENSITIVITY_LIST_FIELDS,
        _NESTED_DICT_FIELDS,
    )
    _compact(record.ordering,_ORDERING_LIST_FIELDS,_NESTED_DICT_FIELDS)
    return record


def materialize_staged_tag_record(record):
    """Thaw one staged record before an in-place workspace mutation."""
    _materialize(record,_TAG_LIST_FIELDS,_TAG_DICT_FIELDS)
    if record.allowed_modifier_groups is SHARED_EMPTY_LIST:
        record.allowed_modifier_groups=[]
    _materialize(
        record.content_sensitivity,
        _SENSITIVITY_LIST_FIELDS,
        _NESTED_DICT_FIELDS,
    )
    _materialize(record.ordering,_ORDERING_LIST_FIELDS,_NESTED_DICT_FIELDS)
    return record
