"""Manager's single local-measurement worker owner; construction is inert."""
from concurrent.futures import Future


def _thread_factory(**options):
    from threading import Thread
    return Thread(**options)


class ResourceExecutorOwner:
    """One worker at a time, retained until public is_alive confirms teardown.

    Future completion is sample acknowledgement, not thread teardown. No joins,
    timers, automatic retries, process enumeration or shared job manager. A start
    error is ambiguous and keeps ownership held for independent supervision.
    """
    def __init__(self, *, thread_factory=_thread_factory):
        self._thread_factory = thread_factory
        self._thread = None
        self._future = None
        self._uncertain = False
        self.closed = False

    @property
    def close_held(self):
        if self._uncertain or self._future is not None and not self._future.done():
            return True
        if self._thread is not None:
            try:
                return bool(self._thread.is_alive())
            except Exception:
                return True
        return False

    def submit(self, function, *args):
        future = Future()
        if self.closed or self.close_held:
            # A definite refusal returns an acknowledged failed Future: nothing
            # was launched. Unknown submission cannot certify route drain.
            future.set_exception(RuntimeError("Local measurement worker unavailable"))
            return future

        def run():
            if not future.set_running_or_notify_cancel():
                return
            try:
                result = function(*args)
            except BaseException as error:
                future.set_exception(error)
            else:
                future.set_result(result)

        self._future = future
        try:
            self._thread = self._thread_factory(target=run,
                name="Manager-local-resources", daemon=False)
            self._thread.start()
        except Exception:
            self._uncertain = True
            if not future.done():
                future.set_exception(RuntimeError("Local measurement worker start unconfirmed"))
        return future

    def begin_close(self):
        self.closed = True
        if self._future is not None:
            self._future.cancel()  # queued only; running worker keeps ownership


def resource_close_held(route):
    return route is not None and (route.close_held or route.executor.close_held)


def begin_resource_close(route):
    if route is not None:
        route.begin_close()
        route.executor.begin_close()
