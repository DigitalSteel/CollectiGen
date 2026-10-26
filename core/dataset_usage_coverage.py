"""Frozen observations from the original Dataset worker, never write authority.

This retains scan evidence through original QThread drain. It does not provide
cross-writer exclusion, authorization, or a global usage-completeness receipt.
"""
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, eq=False)
class OriginalAuthority:
    value: object = field(repr=False)

    def __eq__(self, other):
        return type(other) is OriginalAuthority and self.value is other.value


@dataclass(frozen=True)
class DatasetScanPolicy:
    root: str = field(repr=False)
    include_subfolders: bool
    excluded: tuple[str, ...] = field(repr=False)
    validate: bool
    restore_cached: bool
    managed_paths: tuple[str, ...] | None = field(repr=False)
    changed_paths: tuple[str, ...] | None = field(repr=False)
    external_evidence: bool
    external_mode: str
    external_sources: tuple[str, ...]
    migrate_legacy: bool
    run_automatic_ai: bool


@dataclass(frozen=True)
class DatasetUsageCoverageTerminal:
    run: object = field(repr=False)
    manager: OriginalAuthority = field(repr=False)
    index: OriginalAuthority = field(repr=False)
    policy: DatasetScanPolicy
    scope: tuple = field(repr=False)
    outcome: str
    inventory: tuple[str, ...] = field(repr=False)
    validated: tuple[str, ...] = field(repr=False)
    indexed: tuple[str, ...] = field(repr=False)
    failures: tuple[tuple[str, str], ...] = field(repr=False)
    counts: tuple[tuple[str, int], ...]
    index_token: tuple | None = field(repr=False)
    error: str = field(repr=False)

    @property
    def complete_scan(self):
        token = self.index_token
        return (bool(self.scope) and self.outcome == 'complete' and not self.error and not self.failures
            and self.policy.validate is True and self.policy.managed_paths is None
            and self.policy.changed_paths is None
            and self.index.value is not None and type(token) is tuple and len(token) == 3
            and type(token[0]) is str and bool(token[0])
            and type(token[1]) is int and token[1] > 0
            and type(token[2]) is int and token[2] >= 0
            and not any(dict(self.counts).get(key, 0) for key in
                        ('errors', 'online_only', 'conflicts', 'read_only', 'unavailable'))
            and set(self.inventory) == set(self.validated) == set(self.indexed))


def freeze_worker_terminal(worker, outcome, error=''):
    """Called only by the actual worker after its original terminal outcome."""
    manager = worker.dataset
    index = getattr(manager, 'index', None)
    provider = getattr(index, 'evidence_snapshot_token', None)
    try:
        token = provider() if callable(provider) else None
    except Exception:
        token = None
    if type(token) is not tuple:
        token = None
    records = dict(getattr(manager, 'validated_records', {}) or {})
    paths = lambda values: tuple(sorted(str(Path(value)) for value in values))
    summary = getattr(manager, 'summary', None)
    counts = tuple((key, int(getattr(summary, key, 0))) for key in
        ('images', 'indexed', 'errors', 'online_only', 'conflicts', 'read_only', 'unavailable', 'reused_from_cache'))
    return DatasetUsageCoverageTerminal(worker.coverage_run,
        OriginalAuthority(manager), OriginalAuthority(index), worker.coverage_policy,
        worker.coverage_scope, outcome, paths(getattr(manager, 'images', ())),
        paths(records), paths(getattr(manager, 'indexed_images', ())),
        tuple(sorted((str(path), str(row.error)) for path, row in records.items()
                     if getattr(row, 'error', '')))+tuple(worker._coverage_stage_failures),
        counts, token, str(error))


@dataclass
class DatasetScanRun:
    """Original owner retains one real worker/thread and its frozen result."""
    run: object
    worker: object = field(repr=False)
    thread: object = field(repr=False)
    scope: tuple = field(repr=False)
    terminal: DatasetUsageCoverageTerminal | None = None
    invalidated: bool = False
    drained: bool = False

    def accept(self, terminal):
        if (self.invalidated or type(terminal) is not DatasetUsageCoverageTerminal
                or terminal.run is not self.run or terminal.manager.value is not self.worker.dataset
                or terminal.scope != self.scope or terminal.policy != self.worker.coverage_policy):
            return False
        # Contradictory/duplicate terminal publication must not replace evidence.
        if self.terminal is not None:
            if terminal is self.terminal:
                return True
            self.invalidated=True
            return False
        self.terminal = terminal
        return True

    def current_terminal(self, scope):
        terminal = self.terminal
        if (self.invalidated or not self.drained or terminal is None
                or not terminal.complete_scan or scope != self.scope):
            return None
        return current_scan_terminal(terminal,scope)


def current_scan_terminal(terminal,scope):
    if (type(terminal) is not DatasetUsageCoverageTerminal or not terminal.complete_scan
            or terminal.scope != scope):
        return None
    manager = terminal.manager.value
    index = getattr(manager, 'index', None)
    root = getattr(manager, 'root', None)
    if index is not terminal.index.value or root is None:
        return None
    try:
        if (str(Path(root)) != terminal.policy.root
                or index.evidence_snapshot_token() != terminal.index_token):
            return None
    except Exception:
        return None
    return terminal
