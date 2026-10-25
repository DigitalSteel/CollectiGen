from __future__ import annotations

from collections.abc import Iterable, Iterator


def normalize_tag(tag: str) -> str:
    """Return the comparison form used throughout CollectiGen."""
    return " ".join(tag.strip().lower().replace("_", " ").split())


class TagCollection:
    """Ordered, case-insensitive collection of unique tags."""

    def __init__(self, tags: Iterable[str] | None = None) -> None:
        self._tags: list[str] = []
        self._keys: set[str] = set()

        if tags is not None:
            self.extend(tags)

    def add(self, tag: str) -> bool:
        value = tag.strip()
        key = normalize_tag(value)

        if not key or key in self._keys:
            return False

        self._tags.append(value)
        self._keys.add(key)
        return True

    def extend(self, tags: Iterable[str]) -> int:
        return sum(1 for tag in tags if self.add(tag))

    def remove(self, tag: str) -> bool:
        key = normalize_tag(tag)

        if key not in self._keys:
            return False

        for index, value in enumerate(self._tags):
            if normalize_tag(value) == key:
                del self._tags[index]
                self._keys.remove(key)
                return True

        return False

    def replace(self, old: str, new: str) -> bool:
        old_key = normalize_tag(old)
        new_value = new.strip()
        new_key = normalize_tag(new_value)

        if old_key not in self._keys or not new_key:
            return False

        index = next(
            i for i, value in enumerate(self._tags)
            if normalize_tag(value) == old_key
        )

        if new_key == old_key:
            if self._tags[index] == new_value:
                return False
            self._tags[index] = new_value
            return True

        self._tags.pop(index)
        self._keys.remove(old_key)

        if new_key not in self._keys:
            self._tags.insert(index, new_value)
            self._keys.add(new_key)

        return True

    def clear(self) -> bool:
        if not self._tags:
            return False

        self._tags.clear()
        self._keys.clear()
        return True

    def copy(self) -> TagCollection:
        return TagCollection(self._tags)

    def to_list(self) -> list[str]:
        return list(self._tags)

    def normalized_set(self) -> set[str]:
        return set(self._keys)

    def __contains__(self, tag: object) -> bool:
        return isinstance(tag, str) and normalize_tag(tag) in self._keys

    def __iter__(self) -> Iterator[str]:
        return iter(self._tags)

    def __len__(self) -> int:
        return len(self._tags)

    def __bool__(self) -> bool:
        return bool(self._tags)

    def __repr__(self) -> str:
        return f"TagCollection({self._tags!r})"
