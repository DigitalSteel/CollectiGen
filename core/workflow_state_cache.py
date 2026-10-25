"""Bounded checkpoints for disposable image-status scans, never image authority."""

from pathlib import Path
import hashlib
import json
import sqlite3


def workflow_state_digest(payload):
    """Compare a saved workflow projection without passing its full data to Qt."""
    if isinstance(payload,dict) and payload.get("version")==2:
        from .image_workflow_state import ImageWorkflowState
        adapted=ImageWorkflowState.from_cache_dict(payload)
        if adapted is not None:
            payload=adapted.to_cache_dict()
    return hashlib.sha256(json.dumps(
        payload,sort_keys=True,separators=(",",":"),ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


class WorkflowStateCheckpoint:
    """Retain completed status work even when a later image fails or is cancelled."""

    BATCH_SIZE = 64

    def __init__(self, *, context_fingerprint, index=None, targets=None, cache_scope="dataset", paths=()):
        self.context_fingerprint = str(context_fingerprint)
        self.cache_scope = str(cache_scope)
        self.index = index
        self.targets = targets or {}
        self.pending = {}
        self.recorded = set()
        self.cache_warning = False
        self.revisions = {}
        self.unavailable_revisions = set()
        grouped = {}
        for path in (paths or self.targets):
            path=Path(path)
            target=self.targets.get(path,self.index)
            if target is not None:grouped.setdefault(target,[]).append(path)
        for target,images in grouped.items():
            reader=getattr(target,"workflow_revisions",None)
            if callable(reader):
                try:self.revisions[target]=reader(images)
                except (OSError,sqlite3.DatabaseError):
                    self.cache_warning=True
                    self.unavailable_revisions.add(target)

    def add(self, path, payload, expected):
        self.pending[Path(path)] = (payload, expected)
        if len(self.pending) >= self.BATCH_SIZE:
            self.flush()

    def current_recorded(self):
        """Exclude rows invalidated after an earlier batch was checkpointed.

        A yielded reader may have already saved a path that foreground work
        invalidates. Check generations once before publishing the final result,
        without turning every 64-image checkpoint into an accumulating reread.
        """
        current=set(self.recorded);grouped={}
        for path in current:
            target=self.targets.get(path,self.index)
            if target in self.revisions:grouped.setdefault(target,[]).append(path)
        for target,paths in grouped.items():
            try:revisions=target.workflow_revisions(paths)
            except (OSError,sqlite3.DatabaseError):
                self.cache_warning=True;current.difference_update(paths);continue
            current.difference_update(path for path in paths
                if revisions.get(path,0)!=self.revisions[target].get(path,0))
        return current

    def flush(self):
        grouped = {}
        for path, (payload, expected) in self.pending.items():
            target = self.targets.get(path, self.index)
            if target is None or not self.context_fingerprint:
                self.recorded.add(path)
            else:
                grouped.setdefault(target, {})[path] = (payload, expected)
        self.pending.clear()
        for target, values in grouped.items():
            if target in self.unavailable_revisions:
                # Unknown invalidation generations cannot certify results from
                # before a concurrent knowledge change.
                continue
            try:
                guards=({"expected_revisions":self.revisions[target]}
                        if target in self.revisions else {})
                self.recorded.update(target.record_workflow_states(
                    {path: value[0] for path, value in values.items()},
                    context_fingerprint=self.context_fingerprint,
                    cache_scope=self.cache_scope,
                    expected_fingerprints={path: value[1] for path, value in values.items()},
                    **guards,
                ))
            except (OSError, sqlite3.DatabaseError):
                # Cache failure must not discard usable in-memory results or
                # mask the original worker error during its final checkpoint.
                self.recorded.update(values)
                self.cache_warning = True
