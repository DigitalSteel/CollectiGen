"""Concurrent optional dependency cache refresh; generated hooks only."""
import threading
import time
import pytest
from core import dependencies as dep


def test_optional_cache_refresh_is_serialized_without_skipping_requests(monkeypatch):
    gate=threading.Barrier(3);mutex=threading.Lock();active=0;maximum=0;calls=0;errors=[]
    def invalidation():
        nonlocal active,maximum,calls
        with mutex:
            active+=1;maximum=max(maximum,active);calls+=1
        time.sleep(0.04)
        with mutex:active-=1
    monkeypatch.setattr(dep.importlib,'invalidate_caches',invalidation)
    def worker():
        gate.wait()
        try:dep._invalidate_optional_import_caches()
        except BaseException as exc:errors.append(exc)
    threads=[threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:thread.start()
    gate.wait()
    for thread in threads:thread.join(timeout=3)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors and calls==2 and maximum==1


def test_optional_cache_refresh_keeps_errors_and_does_not_retry(monkeypatch):
    calls=[]
    def failed():
        calls.append(True);raise KeyError('synthetic invalidator failure')
    monkeypatch.setattr(dep.importlib,'invalidate_caches',failed)
    with pytest.raises(KeyError,match='synthetic invalidator failure'):
        dep._invalidate_optional_import_caches()
    assert calls==[True]
