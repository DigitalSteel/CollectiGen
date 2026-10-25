"""Ephemeral local examples bridge; borrows the original host and query receipt."""
from dataclasses import dataclass
from pathlib import Path

from core.federated_dataset import LibraryImageIdentity
from suite_core.media_examples import ExampleResult, ExampleStatus, MediaReference


class ExampleImageHold(ValueError):
    pass


@dataclass(frozen=True)
class ExampleImagePage:
    references: tuple
    paths: tuple
    offset: int
    next_offset: int
    total: int
    unavailable: int


class OrganizerExampleImages:
    def __init__(self, binding, organizer):
        from suite_core.encyclopedia.hosted_examples import HostedLibraryBinding
        from suite_core.media_organizer.facade import MediaOrganizer
        if not isinstance(binding, HostedLibraryBinding) or not isinstance(organizer, MediaOrganizer):
            raise ExampleImageHold('Original hosted binding and Organizer facade required')
        self.binding, self.organizer = binding, organizer
        self.result = self._memory = None
        self._closed = False
        self._check_host()

    def _check_host(self):
        if self._closed:
            raise ExampleImageHold('Example image bridge is detached')
        self.organizer._check_context()
        window = self.binding.window
        if (window._hosted_binding is not self.binding or window._example_provider is not self.binding
                or self.organizer.context.state is not self.binding.session
                or self.binding.session.dataset is not self.binding.dataset):
            raise ExampleImageHold('Original hosted library authority changed')

    def clear(self):
        self.result = self._memory = None

    def close(self):
        self.clear()
        self._closed = True

    def capture(self, result):
        """Called synchronously during accepted delivery, before receipt expires."""
        self.clear()
        self._check_host()
        if (type(result) is not ExampleResult or result.status is not ExampleStatus.SUCCESS
                or type(result.references) is not tuple or not 0 < len(result.references) <= 100_000
                or any(type(ref) is not MediaReference for ref in result.references)
                or len(set(result.references)) != len(result.references)
                or not self.binding.accepts(result)):
            raise ExampleImageHold('Original accepted example delivery required')
        self._memory = self.binding._memory(result.request)
        self.result = result

    def _current(self):
        self._check_host()
        if self.result is None or self.binding._memory(self.result.request) != self._memory:
            self.clear()
            raise ExampleImageHold('Example images became stale; find examples again')
        return self.result

    def _resolve(self, reference, included):
        if type(reference) is not MediaReference or reference.library_id not in self.result.request.library_ids:
            raise ExampleImageHold('Reference outside the accepted library scope')
        dataset = self.binding.dataset
        identity = LibraryImageIdentity(reference.library_id, reference.relative_path)
        path = Path(dataset.path_for_identity(identity))
        # Native identity/path conversion and existing storage availability own
        # root/sync-provider semantics. No image bytes, sidecars or scans here.
        if path not in included or dataset.identity_for_path(path) != identity:
            return None
        if not dataset.storage_availability(path).readable or not path.is_file():
            return None
        return path

    def page(self, offset=0, limit=12):
        result = self._current()
        if type(offset) is not int or type(limit) is not int or not 0 <= offset <= len(result.references) or not 1 <= limit <= 48:
            raise ExampleImageHold('Bounded example page required')
        included = frozenset(Path(path) for path in self.binding.dataset.images)
        stop = min(offset + limit, len(result.references))
        references, paths = [], []
        unavailable = 0
        for reference in result.references[offset:stop]:
            self._current()
            try:
                path = self._resolve(reference, included)
            except (OSError, KeyError):
                path = None
            if path is None:
                unavailable += 1
            else:
                references.append(reference)
                paths.append(path)
        self._current()
        return ExampleImagePage(tuple(references), tuple(paths), offset, stop,
                                len(result.references), unavailable)

    def open_image(self, reference):
        result = self._current()
        if reference not in result.references:
            raise ExampleImageHold('Only an accepted example can be opened')
        included = frozenset(Path(path) for path in self.binding.dataset.images)
        try:
            path = self._resolve(reference, included)
        except (OSError, KeyError):
            path = None
        if path is None:
            return False
        self._current()
        return self.organizer.view_library_image(str(path))
