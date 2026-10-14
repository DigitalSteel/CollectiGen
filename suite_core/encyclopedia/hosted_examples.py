"""Hosted translation of an existing library session; no new index/cache owner.

The host supplies its already-running AppState-shaped library session, admitted
Publication and existing CoordinatedCallTask class plus GUI dispatcher. Importing
this module starts nothing and imports neither core nor Qt. Standalone discovery
and the inactive byte-pinned cache reader are deliberately not used.
"""
from dataclasses import dataclass
from hashlib import sha256
from threading import RLock
from uuid import uuid4

from suite_core.media_examples import ExampleResult, ExampleStatus, MediaReference


class _Hold(Exception):
    def __init__(self, status): self.status = status


@dataclass
class _Delivery:
    result: ExampleResult
    memory: tuple


class HostedLibraryBinding:
    """Ephemeral consumer lifecycle around native evidence queries and jobs.

    No library admission is manufactured here. The host must already own the
    session's native cache query operations and explicitly admit the Publication
    from the matching native Knowledge owner before binding it. The Publication
    must already be selected in the window. Changing catalogs requires rebinding.
    """
    EVENTS = ('library_scope_changed', 'image_data_changed', 'knowledge_changed')

    def __init__(self, window, session, *, admitted_publication, task_factory, post_to_gui):
        if (window.publication is None or admitted_publication != window.publication
                or window.catalog_session is None):
            raise ValueError('Select the host-admitted Publication before binding its library session')
        if not callable(task_factory) or not callable(post_to_gui):
            raise ValueError('The existing coordinated task class and GUI dispatcher are required')
        for name in ('dataset', 'evidence_queries', 'knowledge', 'events', 'jobs'):
            if getattr(session, name, None) is None:
                raise ValueError('An existing hosted library session is required')
        self.window, self.session = window, session
        self.dataset, self.queries, self.knowledge = session.dataset, session.evidence_queries, session.knowledge
        self.catalog_token = window.example_catalog_token()
        self.task_factory, self.post_to_gui = task_factory, post_to_gui
        self._lock = RLock()
        self._epoch = 0
        self._closed = False
        self._closing = False
        scope_factory = getattr(session.jobs, 'create_execution_scope', None)
        self.execution_scope = scope_factory() if callable(scope_factory) else None
        self._tasks = {}
        self._deliveries = {}
        self._event_callback = self._changed
        for event in self.EVENTS:
            session.events.subscribe(event, self._event_callback)
        try:
            self.refresh_scope()
        except Exception:
            self.close()
            raise

    def refresh_scope(self):
        """GUI route after the owning shell changes scope or native evidence."""
        if self._closed:
            return
        # EventBus callbacks may run on a worker: GUI mutation is dispatched,
        # while the epoch invalidates delivery immediately on that calling thread.
        self.window.configure_examples(provider=self,
            library_ids=tuple(self.dataset.scope_library_ids), submit=self.submit)

    def example_queries_admitted(self):
        with self._lock:
            return (not self._closed and not self._closing and
                    (self.execution_scope is None or self.execution_scope.admitting))

    def pause_example_queries(self):
        if self.execution_scope is not None:self.execution_scope.pause_admission()
        with self._lock:
            self._closing = True
            self._epoch += 1
            tasks = tuple(self._tasks.values())
        for task in tasks:task.cancel()

    def example_queries_drained(self):
        return self.execution_scope is not None and self.execution_scope.physically_drained()

    def resume_example_queries(self):
        if not self.example_queries_drained():return False
        with self._lock:
            if self._closed:return False
            self._closing = False
            self._epoch += 1
            self._tasks.clear()
            self._deliveries.clear()
        self.execution_scope.resume_admission()
        return self.example_queries_admitted()

    def _changed(self, **payload):
        with self._lock:
            if self._closed:
                return
            self._epoch += 1
        self.post_to_gui(self.refresh_scope)

    def _memory(self, request, *, require_available=True):
        with self._lock:
            epoch, closed = self._epoch, self._closed or self._closing
        if (closed or self.session.dataset is not self.dataset
                or self.session.evidence_queries is not self.queries
                or self.session.knowledge is not self.knowledge
                or request.selection_token != self.window.example_selection_token()
                or request.knowledge_revision != self.window.example_catalog_token()
                or request.knowledge_revision != self.catalog_token):
            raise _Hold(ExampleStatus.STALE)
        scope = tuple(self.dataset.scope_library_ids)
        if not request.library_ids:
            raise _Hold(ExampleStatus.UNAVAILABLE)
        # The native federation owns its whole included scope. Never mutate its
        # scope or construct a scoped index to service a consumer subset.
        if request.library_ids != scope:
            raise _Hold(ExampleStatus.STALE)
        participants = tuple(self.dataset.scoped_profile_managers())
        if require_available and (len(participants) != len(scope)
                or {profile.stable_id for profile, _ in participants} != set(scope)
                or any(manager.index is None for _, manager in participants)
                or getattr(self.dataset, 'index', None) is None):
            raise _Hold(ExampleStatus.UNAVAILABLE)
        native_names = []
        for identity in request.identities:
            record = self.knowledge.tags.get(identity.tag_id)
            names = tuple(self.knowledge.identity_names(identity.tag_id))
            if (record is None or names != identity.names
                    or record.category != identity.category_id):
                raise _Hold(ExampleStatus.STALE)
            native_names.append((identity.tag_id, names, record.category))
        return (epoch, scope, id(self.dataset.index),
                tuple((profile.stable_id, id(manager), id(manager.index)) for profile, manager in participants),
                tuple(native_names))

    def _source_token(self):
        # Existing native EvidenceQueryService owns normalization/cache semantics;
        # this is its source-token reader, not a new cache or SQLite connection.
        token = self.queries._source_token(self.dataset.index)
        if (token and token[0] == 'federated'
                and any(type(row[-1]) is not int or row[-1] < 0 for row in token[1])):
            raise _Hold(ExampleStatus.UNAVAILABLE)
        return token

    @staticmethod
    def _digest(memory, source):
        return sha256(repr((memory, source)).encode('utf-8')).hexdigest()

    def query(self, request, *, should_continue=None):
        def checkpoint():
            self._memory(request)
            if should_continue is not None and not should_continue():
                raise _Hold(ExampleStatus.CANCELLED)
            return True
        try:
            checkpoint()
            memory, before = self._memory(request), self._source_token()
            normalized = tuple(self.queries._groups((identity.names,)) for identity in request.identities)
            groups = (() if request.match_all and any(not group for group in normalized)
                      else tuple(group[0] for group in normalized if group))
            paths = self.queries.query(self.dataset.index, groups, request.match_all,
                                       should_continue=checkpoint) if groups else ()
            references, seen = [], set()
            for path in paths:
                checkpoint()
                identity = self.dataset.identity_for_path(path)
                if identity is None or identity.library_id not in request.library_ids:
                    raise _Hold(ExampleStatus.STALE)
                reference = MediaReference(identity.library_id, identity.relative_path)
                if reference not in seen:
                    seen.add(reference); references.append(reference)
                    if len(references) > 100_000:
                        raise _Hold(ExampleStatus.UNAVAILABLE)
            checkpoint()
            after_memory, after = self._memory(request), self._source_token()
            if memory != after_memory or before != after:
                raise _Hold(ExampleStatus.STALE)
            checkpoint()
            return ExampleResult(request, ExampleStatus.SUCCESS if references else ExampleStatus.EMPTY,
                                 tuple(references), self._digest(memory, after))
        except _Hold as error:
            return ExampleResult(request, error.status)
        except InterruptedError:
            return ExampleResult(request, ExampleStatus.CANCELLED)
        except Exception:
            return ExampleResult(request, ExampleStatus.ERROR)

    def accepts(self, result):
        """GUI delivery uses memory only; native source validation runs in the job."""
        try:
            with self._lock:
                receipt = self._deliveries.get(id(result))
            return (receipt is not None and receipt.result is result
                    and receipt.memory == self._memory(result.request,
                        require_available=result.status in (ExampleStatus.SUCCESS, ExampleStatus.EMPTY)))
        except Exception:
            return False

    def submit(self, work, deliver):
        """Reuse native coordinated jobs, preserving job cancellation checkpoints."""
        if not self.example_queries_admitted():
            raise ValueError('Original example query admission is paused for close')
        request = self.window.example_current_request()
        job_key = uuid4().hex  # selection-specific; do not coalesce consumer jobs

        def run(job):
            result = work(job)
            try:
                memory = self._memory(request,
                    require_available=result.status in (ExampleStatus.SUCCESS, ExampleStatus.EMPTY))
                if result.status in (ExampleStatus.SUCCESS, ExampleStatus.EMPTY):
                    if result.catalog_token != self._digest(memory, self._source_token()):
                        result = ExampleResult(request, ExampleStatus.STALE)
                with self._lock:
                    if not self._closed and job_key in self._tasks:
                        self._deliveries[id(result)] = _Delivery(result, memory)
            except Exception:
                result = ExampleResult(request, ExampleStatus.STALE)
            return result

        def complete(result):
            def on_gui():
                try:
                    deliver(result)
                finally:
                    with self._lock:
                        self._deliveries.pop(id(result), None)
                        self._tasks.pop(job_key, None)
            self.post_to_gui(on_gui)

        def stopped(status):
            # Native coordinator failure/cancel must leave the visible query state.
            result = ExampleResult(request, status)
            try:
                with self._lock:
                    self._deliveries[id(result)] = _Delivery(result, self._memory(request, require_available=False))
            except Exception:
                pass
            complete(result)

        scope_options = {} if self.execution_scope is None else {'execution_scope': self.execution_scope}
        task = self.task_factory(self.session.jobs, 'Search Encyclopedia library examples', run,
                                 resource='cpu', key='encyclopedia-examples:' + job_key, **scope_options)
        if self.execution_scope is not None and (getattr(task, 'execution_scope', None) is not self.execution_scope
                or getattr(task, 'manager', None) is not self.session.jobs):
            raise ValueError('Original task must retain the coordinator-issued execution scope')
        self._tasks[job_key] = task
        task.signals.complete.connect(complete)
        task.signals.failed.connect(lambda _message: stopped(ExampleStatus.ERROR))
        task.signals.cancelled.connect(lambda: stopped(ExampleStatus.CANCELLED))
        try:
            task.start()
        except Exception:
            self._tasks.pop(job_key, None)
            task.cancel()
            raise

    def close(self):
        """Detach subscriptions/observers; the existing job manager owns drain."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._epoch += 1
            self._deliveries.clear()
        if self.execution_scope is not None:self.execution_scope.pause_admission()
        for event in self.EVENTS:
            self.session.events.unsubscribe(event, self._event_callback)
        for task in tuple(self._tasks.values()):
            task.cancel()
        self._tasks.clear()
