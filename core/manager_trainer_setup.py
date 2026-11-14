"""Manager-local host for the stock confirmed trainer software queue.

The canonical executor owns acquisition, health, retained generations and recovery.
Opening this host never resumes an operation or installs software.
"""
from pathlib import Path
import os
import re

from .events import EventBus
from .training_process import TrainingProcessService
from .setup_operation_queue import SetupOperationQueue
from .trainer_setup_executor import TrainerSetupExecutor
from .trainer_runtime_catalog import RuntimeSoftwareCatalog
from .trainer_environment import _read_json
from .training_source_io import _assert_unredirected
from .training_setup_selection import _require_disjoint_protected_paths


class ManagerTrainerSetupProcess(TrainingProcessService):
    """Observe retained stock ownership without changing the qualified runner."""
    @property
    def has_retained_owners(self):
        with self._held_lock:
            return bool(self._held)


class ManagerTrainerSetup:
    def __init__(self, jobs, root, settings, *, profile_path, catalog_path, source_root,
                 session_allowed, protected_roots, executor=None, queue_factory=SetupOperationQueue):
        self.jobs = jobs
        self.root = Path(root)
        self.session_allowed = session_allowed
        self.protected_roots = protected_roots
        self.closing = False
        self.executor = executor or TrainerSetupExecutor(jobs, self.root,
            profile_path=profile_path, catalog_path=catalog_path, source_root=source_root, process=ManagerTrainerSetupProcess())
        self.validate_paths(self.root)
        self.events = EventBus()
        self.queue = queue_factory(settings, jobs, self.events, self.root / 'setup-queue.json',
                                   trainer_executor=self.execute)
        if self.queue.load_error:
            raise ValueError(self.queue.load_error)
        if any(row.kind != 'setup_trainer_runtime' for row in self.queue.snapshot()):
            raise ValueError('This Manager queue contains another capability. It was left unchanged.')

    def cancelled(self, should_cancel=lambda: False):
        return lambda: should_cancel() or self.closing or not self.session_allowed()

    def check(self, should_cancel=lambda: False):
        if self.cancelled(should_cancel)():
            raise InterruptedError('The original Manager Trainer Setup session ended.')
        self.validate_paths(self.root)

    def validate_paths(self, *paths):
        if not self.session_allowed():
            raise InterruptedError('The original Manager Trainer Setup session ended.')
        _require_disjoint_protected_paths(paths, self.protected_roots())
        for path in paths:
            _assert_unredirected(Path(path))

    def validate_selection(self, action, selection):
        paths = [self.root]
        if action in {'existing', 'import', 'repair'}:
            paths.append(Path(selection))
        elif action == 'export':
            paths.append(Path(selection[1]))
        self.validate_paths(*paths)

    def describe(self, *, should_cancel=lambda: False):
        self.check(should_cancel)
        profile = self.executor.profile()
        managed = False
        issue = ''
        try:
            RuntimeSoftwareCatalog.from_dict(_read_json(self.executor.catalog_path))
            managed = True
        except (OSError, RuntimeError, ValueError) as error:
            issue = 'Managed acquisition is unavailable: ' + str(error)[:1000]
        choices = []
        root = self.executor.managed_root / 'generations'
        _assert_unredirected(root)
        if root.exists():
            with os.scandir(root) as entries:
                for entry in entries:
                    self.check(should_cancel)
                    if len(choices) >= 1000 or not re.fullmatch(r'[0-9a-f]{32}', entry.name):
                        raise ValueError('Retained trainer generations need review.')
                    path = Path(entry.path)
                    _assert_unredirected(path)
                    if not path.is_dir():
                        raise ValueError('A retained trainer generation is not a directory.')
                    choices.append((entry.name, 'Generation ' + entry.name))
        return profile, managed, tuple(sorted(choices)), issue

    def execute(self, operation, *, should_cancel, progress):
        cancel = self.cancelled(should_cancel)
        self.check(cancel)
        value, _ = self.executor._review(dict(operation.params), cancel)
        self.validate_selection(value['action'], value['selection'])
        return self.executor(operation, should_cancel=cancel, progress=progress)

    def completed(self, reference, *, should_cancel):
        cancel = self.cancelled(should_cancel)
        self.check(cancel)
        value, _ = self.executor._review(dict(reference), cancel)
        self.validate_selection(value['action'], value['selection'])
        return self.executor.completed_result(reference, should_cancel=cancel)

    def save_resources(self, reference, path, tokens, output, revision, *, should_cancel):
        cancel = self.cancelled(should_cancel)
        result = self.completed(reference, should_cancel=cancel)
        generation = getattr(result, 'generation', result)
        self.validate_paths(self.root, path, tokens, output, generation.software_root)
        return self.executor.save_resources(reference, path, tokens, output, revision, should_cancel=cancel)

    def resume_review(self):
        if not self.session_allowed():
            raise InterruptedError('The original Manager Trainer Setup session ended.')
        # Revoke only the shell close intent. Existing job cancellation and
        # retained native ownership are unchanged; no operation is dispatched.
        self.closing = False

    def begin_close(self):
        self.closing = True
        for row in self.queue.snapshot():
            if row.state in {'waiting', 'running'}:
                self.queue.cancel_operation(row.id)
        return not self.close_held

    @property
    def close_held(self):
        return (any(row.state in {'waiting', 'running'} for row in self.queue.snapshot())
                or self.executor.process.has_retained_owners)
