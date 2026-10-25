"""Host-worker coordination only; no Qt, catalog writes or executor ownership."""
from concurrent.futures import Future
from threading import Event, get_ident

from core.tag_default_acquisition import TagDefaultAcquisition, TagDefaultBaseline


class DefaultAcquisitionController:
    """Host supplies a cheap monotonic baseline_generation callback.

    Increment that in-memory integer before publishing any catalog/root/revision,
    approved-release, recovery or selected-connection change to consumers. Poll
    must run after those host invalidations. The callback performs no disk I/O;
    observe_baseline remains worker-only. A generation never resets or rebinds
    during this controller's lifetime, including changes away and back.
    """
    def __init__(self, *, service, executor, observe_baseline, baseline_generation):
        if (type(service) is not TagDefaultAcquisition or not callable(observe_baseline)
                or not callable(baseline_generation)):
            raise TypeError("Explicit acquisition and native baseline observer required")
        if not callable(getattr(executor, "submit", None)):
            raise TypeError("Retained host executor required")
        self.service, self.executor, self.observe_baseline = service, executor, observe_baseline
        self.baseline_generation = baseline_generation
        self.discovery = self.result = None
        self.error = ""
        self._future = None
        self._cancel = Event()
        self._closing = self._uncertain = False
        self._owner = get_ident()
        self._generation = None

    def _current_generation(self):
        value = self.baseline_generation()
        if type(value) is not int or value < 0:
            raise ValueError("Host freshness requires a nonnegative monotonic generation")
        return value

    def _check_owner(self):
        if get_ident() != self._owner:
            raise RuntimeError("Controller delivery belongs to the original host thread")

    @property
    def close_held(self):
        return self._future is not None or self._uncertain

    def _start(self, operation, offer=None, channel=None):
        self._check_owner()
        if self._closing or self.close_held:
            raise RuntimeError("Wait for retained acquisition work to settle")
        generation = self._current_generation()
        reviewed = self.discovery
        if operation == "acquire" and generation != self._generation:
            self.discovery = self.result = None
            raise RuntimeError("Native baseline generation changed; refresh before download")
        self.discovery = self.result = None
        self.error = ""
        self._cancel = cancel = Event()
        self._generation = generation
        def work():
            if get_ident() == self._owner:
                raise RuntimeError("Acquisition requires the original background executor")
            if cancel.is_set():
                raise InterruptedError("Cancelled before observation")
            baseline = self.observe_baseline()
            if type(baseline) is not TagDefaultBaseline:
                raise TypeError("Native baseline observer returned unsupported evidence")
            if operation == "refresh":
                result = self.service.discover(baseline=baseline, channel=channel,
                    should_cancel=cancel.is_set)
            else:
                if reviewed is None or baseline != reviewed.baseline:
                    raise RuntimeError("Native baseline changed; refresh before download")
                result = self.service.acquire(offer, baseline=baseline, confirmed=True,
                    should_cancel=cancel.is_set)
            if cancel.is_set():
                raise InterruptedError("Cancelled; retained work has settled")
            if self.observe_baseline() != baseline:
                raise RuntimeError("Native baseline changed during acquisition; refresh before review")
            return result
        try:
            future = self.executor.submit(work)
            if not isinstance(future, Future):
                raise RuntimeError("Submission returned no retained Future")
            self._future = future
        except Exception as error:
            self._uncertain = True  # Submission may have started; never fabricate cleanup.
            self.error = str(error)
            raise

    def refresh(self, *, channel):
        self._start("refresh", channel=channel)

    def acquire(self, offer, *, confirmed=False):
        self._check_owner()
        if confirmed is not True or self.discovery is None or not any(
                row is offer for row in self.discovery.offers):
            raise ValueError("Confirm the exact displayed offer before download")
        self._start("acquire", offer=offer)

    def poll(self):
        self._check_owner()
        future = self._future
        if future is None or not future.done():
            return
        self._future = None
        try:
            result = future.result()
            if self._cancel.is_set() or self._closing:
                return
            if self._current_generation() != self._generation:
                raise RuntimeError("Native baseline changed before delivery; refresh before review")
            from core.tag_default_acquisition import TagDefaultDiscovery
            if type(result) is TagDefaultDiscovery:
                self.discovery = result
            else:
                self.result = result
        except Exception as error:
            self.error = str(error)

    def cancel(self):
        self._check_owner()
        self.discovery = self.result = None
        self._cancel.set()
        if self._future is not None:
            self._future.cancel()

    def begin_close(self):
        self._check_owner()
        self._closing = True
        self.cancel()
