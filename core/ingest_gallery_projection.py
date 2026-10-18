"""Disposable Ingest ordering; never an Image Knowledge or safety authority."""
from __future__ import annotations

from collections import Counter


class RevisionDict(dict):
    """Track replacements as well as size changes in an in-memory projection."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.revision = 0

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.revision += 1

    def __delitem__(self, key):
        super().__delitem__(key)
        self.revision += 1

    def clear(self):
        super().clear()
        self.revision += 1

    def pop(self, *args):
        value = super().pop(*args)
        self.revision += 1
        return value

    def popitem(self):
        value = super().popitem()
        self.revision += 1
        return value

    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        self.revision += 1

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def __ior__(self, other):
        self.update(other)
        return self


class IngestGalleryProjection:
    """One normalized ordering and exact stage/state groups per input revision.

    Callbacks read saved in-memory presentation state only. Rating preferences
    protect previews and do not change membership; the gallery still applies
    the live safety policy to each displayed card.
    """

    def __init__(self, paths, state_for, stage_for):
        self.positions = {path: position for position, path in enumerate(paths)}
        priority = {"problem": 0, "incomplete": 1, "ai": 2, "review": 3, "reviewed": 4}
        rows = [(path, state_for(path)[0], stage_for(path), str(path).casefold())
                for path in paths]
        rows.sort(key=lambda row: (priority.get(row[1], 9), row[3]))
        self.stage_counts = Counter(row[2] for row in rows)
        self.groups = {}
        for path, state, stage, _name in rows:
            for key in (("all", "all"), (stage, "all"), ("all", state), (stage, state)):
                self.groups.setdefault(key, []).append(path)

    def paths(self, stage, state):
        # Consumers treat these owned lists as read-only and slice only a page.
        return self.groups.get((stage, state), [])

    def selected(self, paths):
        return sorted((path for path in paths if path in self.positions),
                      key=self.positions.__getitem__)
