from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass,field
from typing import Callable
from uuid import uuid4


RESOURCE_CLASSES=("cpu","io","gpu")


@dataclass
class JobProgress:
    id: str
    name: str
    state: str = "queued"
    current: int = 0
    total: int = 0
    message: str = ""
    error: str = ""
    resource: str = "cpu"
    key: str = ""
    external: bool = False
    recover_on_shutdown: bool = False
    priority: int = 0
    result: object = field(default=None,repr=False)
    reason: str = ""
    _pause: threading.Event = field(default_factory=threading.Event,repr=False)
    _cancel: threading.Event = field(default_factory=threading.Event,repr=False)
    _progress_callback: Callable[[int,int,str],None]|None = field(
        default=None,repr=False,
    )

    _checkpoint_callback: Callable[[],bool]|None = field(default=None,repr=False)
    _queued_at: float=field(default_factory=lambda:time.monotonic(),repr=False)
    _first_started_at: float|None=field(default=None,repr=False)
    _finished_at: float|None=field(default=None,repr=False)

    @property
    def queue_wait_seconds(self):
        endpoint=self._first_started_at if self._first_started_at is not None else self._finished_at
        return max(0,(time.monotonic() if endpoint is None else endpoint)-self._queued_at)

    @property
    def percent(self) -> float:
        return (self.current/self.total*100) if self.total else 0.0

    @property
    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    def checkpoint(self) -> bool:
        if self._checkpoint_callback is not None and not self._checkpoint_callback():
            return False
        while self._pause.is_set() and not self._cancel.is_set():
            if self._checkpoint_callback is not None and not self._checkpoint_callback():
                return False
            self._cancel.wait(0.1)
        return not self._cancel.is_set()

    def update(self,current: int,total: int=0,message: str="") -> None:
        if self._progress_callback is not None:
            self._progress_callback(current,total,message)
            return
        self._set_progress(current,total,message)

    def _set_progress(self,current: int,total: int=0,message: str="") -> None:
        self.current=max(0,int(current))
        if total:
            self.total=max(0,int(total))
        if message:
            self.message=str(message)


@dataclass(frozen=True)
class _QueuedWork:
    worker: Callable[[JobProgress],object]
    exclusive: bool
    priority: int
    sequence: int
    reclaim_ai_runtime: bool=False


class BackgroundExecutionScope:
    """Owner-issued admission and physical-drain handle; no cancellation authority."""
    def __init__(self,manager):
        self._manager=manager
        self._admitting=True
        self._jobs:dict[str,JobProgress]={}

    @property
    def admitting(self) -> bool:
        with self._manager._condition:
            return self._admitting and not self._manager._shutting_down

    def pause_admission(self) -> None:
        with self._manager._condition:
            self._admitting=False

    def resume_admission(self) -> None:
        with self._manager._condition:
            self._admitting=True

    def _prune_locked(self) -> None:
        self._jobs={key:job for key,job in self._jobs.items()
                    if not self._manager._execution_drained_locked(job)}

    def _retain_locked(self,job: JobProgress) -> None:
        if job.external:
            raise ValueError("Scoped execution requires an original internal job.")
        self._prune_locked()
        bound=self._manager._queue_limit+sum(self._manager.limits.values())
        if job.id not in self._jobs and len(self._jobs)>=bound:
            raise RuntimeError("Execution scope is at capacity; wait for original workers to drain.")
        self._jobs[job.id]=job

    def _require_room_locked(self) -> None:
        self._prune_locked()
        if len(self._jobs)>=self._manager._queue_limit+sum(self._manager.limits.values()):
            raise RuntimeError("Execution scope is at capacity; wait for original workers to drain.")

    def physically_drained(self) -> bool:
        with self._manager._condition:
            self._prune_locked()
            return not self._jobs


class BackgroundJobManager:
    """Shared resource-aware coordinator for background and externally run work.

    Core workers may be submitted directly.  Qt-owned workers reserve an
    external slot through :meth:`try_acquire`, then publish progress and release
    it when their own thread finishes.  Both paths therefore share resource
    limits, matching-request deduplication, cancellation state, and snapshots.
    """

    def __init__(
        self,on_change: Callable[[JobProgress],None]|None=None,*,
        limits: dict[str,int]|None=None,
        history_limit: int=256,
        queue_limit: int=512,
    ) -> None:
        cpu=max(1,min(4,(os.cpu_count() or 2)-1))
        selected={"cpu":cpu,"io":2,"gpu":1}
        for resource,value in (limits or {}).items():
            if resource in RESOURCE_CLASSES:
                selected[resource]=max(1,int(value))
        self.limits=selected
        self.jobs: dict[str,JobProgress]={}
        self.on_change=on_change
        self._condition=threading.Condition(threading.RLock())
        self._active={resource:0 for resource in RESOURCE_CLASSES}
        self._active_ids:set[str]=set()
        self._key_jobs:dict[str,str]={}
        self._listeners:dict[str,dict[str,Callable[[JobProgress],None]]]={}
        self._progress_listeners:dict[
            str,dict[str,Callable[[JobProgress],None]]
        ]={}
        self._exclusive_id=""
        self._threads:dict[str,threading.Thread]={}
        self._pending:dict[str,_QueuedWork]={}
        self._next_sequence=0
        self._history_limit=max(1,int(history_limit))
        self._queue_limit=max(1,int(queue_limit))
        self._counters={
            "requests":0,"deduplicated":0,"admitted":0,"completed":0,
            "rejected":0,"maximum_pending":0,"maximum_threads":0,
        }
        self._shutting_down=False
        self._yieldable_readers={}
        self._reader_yield_requests=set()
        self._yielded_readers=set()
        self._foreground_readers={}
        self._reader_handoff=False
        self._ai_runtime_reclaimer: Callable[[],object]|None=None

    def set_ai_runtime_reclaimer(self,callback: Callable[[],object]|None) -> None:
        """Install the worker-side release hook for non-AI GPU work."""
        if callback is not None and not callable(callback):
            raise ValueError("The AI runtime reclaimer must be callable.")
        with self._condition:
            self._ai_runtime_reclaimer=callback

    def prepare_ai_exclusive_work(self) -> bool:
        """Release retained AI runtimes before a native GPU worker starts."""
        with self._condition:
            callback=self._ai_runtime_reclaimer
        if callback is None:
            return False
        callback()
        return True

    @staticmethod
    def _resource(value: str) -> str:
        selected=str(value or "cpu").casefold()
        if selected not in RESOURCE_CLASSES:
            raise ValueError(f"Unknown job resource: {value}")
        return selected

    def _changed(self,job: JobProgress) -> None:
        if self.on_change:
            self.on_change(job)

    def _matching_locked(self,key: str) -> JobProgress|None:
        job_id=self._key_jobs.get(key) if key else None
        job=self.jobs.get(job_id) if job_id else None
        return (
            job
            if job
            and job.state in {"queued","running","paused"}
            and not job.cancel_requested
            else None
        )

    def _new_locked(
        self,name: str,resource: str,key: str,*,external: bool,
        recover_on_shutdown: bool=False,priority: int=0,
    ) -> JobProgress:
        if self._shutting_down:
            raise RuntimeError("Background work is shutting down.")
        job=JobProgress(
            uuid4().hex,str(name),resource=resource,key=str(key or ""),
            external=external,recover_on_shutdown=bool(recover_on_shutdown),
            priority=int(priority),
        )
        job._progress_callback=lambda current,total,message,job_id=job.id:self.update(
            job_id,current,total,message,
        )
        self.jobs[job.id]=job
        job._execution_owner=self
        job._execution_thread=None
        if job.key:
            self._key_jobs[job.key]=job.id
        return job

    def _prune_history_locked(self) -> None:
        terminal=[
            job_id for job_id,job in self.jobs.items()
            if job.state in {"complete","cancelled","failed"}
            and job_id not in self._threads
            and job_id not in self._listeners
            and job_id not in self._progress_listeners
        ]
        for job_id in terminal[:-self._history_limit]:
            self.jobs.pop(job_id,None)

    def _can_start_locked(self,resource: str,*,exclusive: bool=False) -> bool:
        if self._shutting_down or self._reader_handoff:
            return False
        if self._exclusive_id:
            return False
        if exclusive:
            return not self._active_ids
        return self._active[resource]<self.limits[resource]

    def _start_locked(self,job: JobProgress,*,exclusive: bool=False) -> None:
        self._active[job.resource]+=1; self._active_ids.add(job.id)
        if exclusive:
            self._exclusive_id=job.id
        job.state="running"
        if job._first_started_at is None:job._first_started_at=time.monotonic()
        self._counters["admitted"]+=1

    def _finish_locked(self,job: JobProgress,state: str,error: str="") -> None:
        if job.id in self._active_ids:
            self._active_ids.discard(job.id)
            self._active[job.resource]=max(0,self._active[job.resource]-1)
        if self._exclusive_id==job.id:
            self._exclusive_id=""
        self._yieldable_readers.pop(job.id,None)
        self._reader_yield_requests.discard(job.id)
        self._yielded_readers.discard(job.id)
        for reader_id in self._foreground_readers.pop(job.id,()):
            self._reader_yield_requests.discard(reader_id)
        job.state=state; job.error=str(error or "")
        job._finished_at=time.monotonic()
        self._resume_readers_locked()
        self._counters["completed"]+=1
        if job.key and self._key_jobs.get(job.key)==job.id:
            self._key_jobs.pop(job.key,None)
        self._condition.notify_all()

    def _pump_locked(self) -> None:
        """Admit bounded queued work without allocating one thread per request."""
        if self._reader_handoff:return
        self._resume_readers_locked()
        while self._pending and not self._shutting_down and not self._exclusive_id:
            queued=sorted(
                self._pending.items(),
                key=lambda item:(-item[1].priority,item[1].sequence),
            )
            selected=None
            for job_id,work in queued:
                job=self.jobs.get(job_id)
                if job is None or job.cancel_requested:
                    self._pending.pop(job_id,None)
                    continue
                if work.exclusive:
                    if self._can_start_locked(job.resource,exclusive=True):
                        selected=(job,work)
                    # An exclusive request is a priority barrier: let active
                    # work drain instead of continually admitting newer work.
                    break
                if self._can_start_locked(job.resource):
                    selected=(job,work)
                    break
            if selected is None:
                return
            job,work=selected
            self._pending.pop(job.id,None)
            self._start_locked(job,exclusive=work.exclusive)
            thread=threading.Thread(
                target=self._run_admitted,
                args=(job,work.worker,work.reclaim_ai_runtime),daemon=True,
            )
            self._threads[job.id]=thread
            job._execution_thread=thread  # Scope retains identity after scheduler cleanup.
            self._counters["maximum_threads"]=max(
                self._counters["maximum_threads"],len(self._threads),
            )
            thread.start()

    def _run_admitted(
        self,job: JobProgress,worker: Callable[[JobProgress],object],
        reclaim_ai_runtime: bool=False,
    ) -> None:
        self._changed(job)
        try:
            if reclaim_ai_runtime and job.resource=="gpu":
                self.prepare_ai_exclusive_work()
            result=worker(job)
            state="cancelled" if job.cancel_requested else "complete"; error=""
            if state=="complete":
                job.result=result
        except InterruptedError:
            state="cancelled"; error=""
        except Exception as exc:
            state="failed"; error=str(exc)
        with self._condition:
            self._finish_locked(job,state,error)
            listeners=tuple(self._listeners.pop(job.id,{}).values())
            self._progress_listeners.pop(job.id,None)
        self._changed(job)
        self._notify_listeners(job,listeners)
        with self._condition:
            # Results are delivered through one-shot observers. Completed job
            # history must not retain large gallery/path collections.
            job.result=None
            self._threads.pop(job.id,None)
            self._prune_history_locked()
            self._pump_locked()
            self._condition.notify_all()

    @staticmethod
    def _notify_listeners(
        job: JobProgress,listeners: tuple[Callable[[JobProgress],None],...],
    ) -> None:
        for listener in listeners:
            try:
                listener(job)
            except Exception:
                # Completion observers must not be able to corrupt coordinator
                # state or hide the result from other interested surfaces.
                continue

    def observe(
        self,job_id: str,listener: Callable[[JobProgress],None],
    ) -> str:
        """Observe an existing job's terminal state."""
        token=uuid4().hex; completed=None
        with self._condition:
            job=self.jobs[job_id]
            if job.state in {"complete","cancelled","failed"}:
                completed=job
            else:
                self._listeners.setdefault(job_id,{})[token]=listener
        if completed is not None:
            self._notify_listeners(completed,(listener,))
            return ""
        return token

    def unobserve(
        self,job_id: str,token: str,*,cancel_if_orphaned: bool=False,
    ) -> bool:
        """Detach one observer and optionally stop work nobody still needs."""
        cancelled=None
        with self._condition:
            listeners=self._listeners.get(job_id)
            progress_listeners=self._progress_listeners.get(job_id)
            found=bool(listeners is not None and token in listeners)
            found=bool(
                found or progress_listeners is not None
                and token in progress_listeners
            )
            if not found:
                return False
            if listeners is not None:
                listeners.pop(token,None)
            if progress_listeners is not None:
                progress_listeners.pop(token,None)
            if listeners is not None and not listeners:
                self._listeners.pop(job_id,None)
            if progress_listeners is not None and not progress_listeners:
                self._progress_listeners.pop(job_id,None)
            if not self._listeners.get(job_id) and not self._progress_listeners.get(job_id):
                job=self.jobs.get(job_id)
                if (
                    cancel_if_orphaned and job is not None
                    and job.state in {"queued","running","paused"}
                ):
                    job._cancel.set(); job._pause.clear()
                    if job.id in self._pending:
                        self._pending.pop(job.id,None)
                        self._finish_locked(job,"cancelled")
                        self._listeners.pop(job.id,None)
                        self._progress_listeners.pop(job.id,None)
                        cancelled=job
                        self._pump_locked()
                    self._condition.notify_all()
        if cancelled is not None:
            self._changed(cancelled)
            with self._condition:
                self._prune_history_locked()
        return True

    def _submit(
        self,name: str,worker: Callable[[JobProgress],object],*,
        resource: str,key: str,exclusive: bool=False,
        listener: Callable[[JobProgress],None]|None=None,
        progress_listener: Callable[[JobProgress],None]|None=None,
        recover_on_shutdown: bool=False,priority: int=0,
        reclaim_ai_runtime: bool=False,
        execution_scope: BackgroundExecutionScope|None=None,
    ) -> tuple[JobProgress,str]:
        resource=self._resource(resource); key=str(key or "")
        token=uuid4().hex if listener is not None or progress_listener is not None else ""
        with self._condition:
            if self._shutting_down:
                raise RuntimeError("Background work is shutting down.")
            if execution_scope is not None:
                if type(execution_scope) is not BackgroundExecutionScope or execution_scope._manager is not self:
                    raise ValueError("Original owner execution scope required.")
                if not execution_scope._admitting:
                    raise RuntimeError("Scoped background admission is paused for close.")
            self._counters["requests"]+=1
            matching=self._matching_locked(key)
            if matching is not None:
                if execution_scope is not None:execution_scope._retain_locked(matching)
                self._counters["deduplicated"]+=1
                if listener is not None:
                    self._listeners.setdefault(matching.id,{})[token]=listener
                if progress_listener is not None:
                    self._progress_listeners.setdefault(matching.id,{})[
                        token
                    ]=progress_listener
                return matching,token
            if len(self._pending)>=self._queue_limit:
                self._counters["rejected"]+=1
                raise RuntimeError(
                    "Background work queue is at capacity; retry after active work finishes."
                )
            if execution_scope is not None:execution_scope._require_room_locked()
            job=self._new_locked(
                name,resource,key,external=False,
                recover_on_shutdown=recover_on_shutdown,
                priority=priority,
            )
            if execution_scope is not None:execution_scope._retain_locked(job)
            if listener is not None:
                self._listeners.setdefault(job.id,{})[token]=listener
            if progress_listener is not None:
                self._progress_listeners.setdefault(job.id,{})[
                    token
                ]=progress_listener
            self._next_sequence+=1
            self._pending[job.id]=_QueuedWork(
                worker=worker,exclusive=bool(exclusive),priority=int(priority),
                sequence=self._next_sequence,
                reclaim_ai_runtime=bool(reclaim_ai_runtime),
            )
            self._counters["maximum_pending"]=max(
                self._counters["maximum_pending"],len(self._pending),
            )
            self._pump_locked()
        return job,token

    def submit(
        self,name: str,worker: Callable[[JobProgress],object],*,
        resource: str="cpu",key: str="",exclusive: bool=False,
        recover_on_shutdown: bool=False,priority: int=0,
        reclaim_ai_runtime: bool=False,
    ) -> JobProgress:
        return self._submit(
            name,worker,resource=resource,key=key,exclusive=exclusive,
            recover_on_shutdown=recover_on_shutdown,priority=priority,
            reclaim_ai_runtime=reclaim_ai_runtime,
        )[0]

    def submit_observed(
        self,name: str,worker: Callable[[JobProgress],object],
        listener: Callable[[JobProgress],None],*,
        resource: str="cpu",key: str="",exclusive: bool=False,
        progress_listener: Callable[[JobProgress],None]|None=None,
        recover_on_shutdown: bool=False,priority: int=0,
        reclaim_ai_runtime: bool=False,
        execution_scope: BackgroundExecutionScope|None=None,
    ) -> tuple[JobProgress,str]:
        """Submit or join work while attaching its observer atomically."""
        return self._submit(
            name,worker,resource=resource,key=key,exclusive=exclusive,
            listener=listener,progress_listener=progress_listener,
            recover_on_shutdown=recover_on_shutdown,priority=priority,
            reclaim_ai_runtime=reclaim_ai_runtime,
            execution_scope=execution_scope,
        )

    def create_execution_scope(self) -> BackgroundExecutionScope:
        """Issue a bounded scope over this coordinator's original internal work."""
        return BackgroundExecutionScope(self)

    def _execution_drained_locked(self,job: JobProgress) -> bool:
        if (type(job) is not JobProgress or getattr(job,"_execution_owner",None) is not self
                or job.external or job.state not in {"complete","cancelled","failed"}):
            return False
        thread=getattr(job,"_execution_thread",None)
        return thread is None or not thread.is_alive()

    def allow_foreground_yield(self,job_id: str,*,before_yield=None) -> bool:
        """Opt in only a read-only worker with an explicit safe checkpoint."""
        with self._condition:
            job=self.jobs.get(job_id)
            if job is None or job.state not in {"running","paused"} or self._exclusive_id==job_id:
                return False
            self._yieldable_readers[job_id]=before_yield
            job._checkpoint_callback=lambda:self._reader_checkpoint(job_id)
        return True

    def _resume_readers_locked(self):
        if self._reader_handoff or self._exclusive_id or self._shutting_down:return
        for job_id in tuple(self._yielded_readers):
            job=self.jobs.get(job_id)
            if job is None or job.cancel_requested:continue
            if job_id in self._reader_yield_requests:continue
            if not self._can_start_locked(job.resource):continue
            self._yielded_readers.discard(job_id)
            self._start_locked(job)
            if job._pause.is_set():
                job.state="paused";job.message="Paused by user"
            else:
                job.message="Resuming saved image-status work"
        self._condition.notify_all()

    def _reader_checkpoint(self,job_id):
        with self._condition:
            job=self.jobs.get(job_id)
            if job is None or job.cancel_requested:return False
            if job_id not in self._reader_yield_requests:return True
            flush=self._yieldable_readers.get(job_id)
        # Finish the current image/cache transaction before releasing capacity.
        if callable(flush):flush()
        with self._condition:
            if job_id not in self._reader_yield_requests:return not job.cancel_requested
            if job_id in self._active_ids:
                self._active_ids.remove(job_id)
                self._active[job.resource]=max(0,self._active[job.resource]-1)
            self._yielded_readers.add(job_id)
            job.state="paused";job.message="Paused for foreground work; completed status is saved"
            self._condition.notify_all()
        self._changed(job)
        with self._condition:
            while job_id in self._yielded_readers and not job.cancel_requested and not self._shutting_down:
                self._condition.wait(.1)
            return not job.cancel_requested and not self._shutting_down

    def try_acquire_after_readers(self,name,*,resource="io",key="",timeout=2.0):
        """Temporarily yield registered readers, never writes or AI jobs."""
        resource=self._resource(resource)
        with self._condition:
            matching=self._matching_locked(str(key or ""))
            if matching is not None:
                return matching,False
            active=set(self._active_ids)
            if (not active or not active.issubset(self._yieldable_readers)
                    or self._exclusive_id or self._reader_handoff or self._shutting_down):
                return self.try_acquire(name,resource=resource,key=key,exclusive=True)
            self._reader_handoff=True
            self._reader_yield_requests.update(active)
            self._condition.notify_all()
            deadline=time.monotonic()+max(0,float(timeout))
            while self._active_ids and not self._shutting_down:
                remaining=deadline-time.monotonic()
                if remaining<=0:break
                self._condition.wait(remaining)
            self._reader_handoff=False
            if self._active_ids or self._shutting_down:
                self._reader_yield_requests.difference_update(active)
                self._resume_readers_locked()
                self._pump_locked()
                return self.try_acquire(name,resource=resource,key=key,exclusive=True)
            job=self._new_locked(name,resource,key,external=True)
            self._start_locked(job,exclusive=True)
            self._foreground_readers[job.id]=active
        self._changed(job)
        return job,True

    def try_acquire(
        self,name: str,*,resource: str="io",key: str="",exclusive: bool=False,
    ) -> tuple[JobProgress,bool]:
        """Try to reserve capacity for a worker managed by another runtime."""
        resource=self._resource(resource); key=str(key or "")
        with self._condition:
            if self._shutting_down:
                raise RuntimeError("Background work is shutting down.")
            self._counters["requests"]+=1
            matching=self._matching_locked(key)
            if matching is not None:
                self._counters["deduplicated"]+=1
                return matching,False
            if not self._can_start_locked(resource,exclusive=exclusive):
                blocker=self.jobs.get(self._exclusive_id)
                if blocker is None and self._active_ids:
                    blocker=self.jobs.get(next(iter(self._active_ids)))
                if blocker is not None:
                    return blocker,False
            job=self._new_locked(name,resource,key,external=True)
            self._start_locked(job,exclusive=exclusive)
        self._changed(job)
        return job,True

    def update(self,job_id: str,current: int,total: int=0,message: str="") -> None:
        with self._condition:
            job=self.jobs.get(job_id)
            if job is None or job.state not in {"running","paused"}:
                return
            job._set_progress(current,total,message)
            listeners=tuple(
                self._progress_listeners.get(job_id,{}).values()
            )
        self._changed(job)
        self._notify_listeners(job,listeners)

    def release(self,job_id: str,*,state: str="complete",error: str="") -> None:
        selected=state if state in {"complete","cancelled","failed"} else "complete"
        with self._condition:
            job=self.jobs.get(job_id)
            if job is None or job.state not in {"queued","running","paused"}:
                return
            if selected=="complete" and job.cancel_requested:
                selected="cancelled"
            self._pending.pop(job.id,None)
            self._finish_locked(job,selected,error)
            listeners=tuple(self._listeners.pop(job.id,{}).values())
            self._progress_listeners.pop(job.id,None)
            self._pump_locked()
        self._changed(job)
        self._notify_listeners(job,listeners)
        with self._condition:
            self._prune_history_locked()

    def pause(self,job_id: str) -> None:
        with self._condition:
            job=self.jobs[job_id]
            if job.state!="running" and job_id not in self._yielded_readers:
                return
            job._pause.set(); job.state="paused"
        self._changed(job)

    def resume(self,job_id: str) -> None:
        with self._condition:
            job=self.jobs[job_id]
            if job.state!="paused":
                return
            job._pause.clear()
            if job_id not in self._yielded_readers:job.state="running"
        self._changed(job)

    def cancel(self,job_id: str) -> None:
        listeners=(); queued=False
        with self._condition:
            job=self.jobs[job_id]
            if job.state not in {"queued","running","paused"}:
                return
            job._cancel.set(); job._pause.clear()
            if job.id in self._pending:
                self._pending.pop(job.id,None)
                self._finish_locked(job,"cancelled")
                listeners=tuple(self._listeners.pop(job.id,{}).values())
                self._progress_listeners.pop(job.id,None)
                queued=True
                self._pump_locked()
            self._condition.notify_all()
        self._changed(job)
        if queued:
            self._notify_listeners(job,listeners)
            with self._condition:
                self._prune_history_locked()

    def get(self,job_id: str) -> JobProgress|None:
        with self._condition:
            return self.jobs.get(job_id)

    def holds_exclusive_visible_operation(self, job_id: str) -> bool:
        """Read-only proof of the current externally owned exclusive operation."""
        with self._condition:
            job = self.jobs.get(job_id)
            return bool(job is not None and job.external and job.state == "running"
                and job_id == self._exclusive_id and job_id in self._active_ids
                and not self._shutting_down)

    def active(self) -> list[JobProgress]:
        with self._condition:
            return [
                job for job in self.jobs.values()
                if job.state in {"queued","running","paused"}
            ]

    @property
    def shutting_down(self) -> bool:
        with self._condition:
            return self._shutting_down

    def shutdown(self,*,wait: bool=True,timeout: float=2.0) -> bool:
        """Stop new work and cooperatively drain ordinary coordinator jobs.

        Recoverable jobs still receive the cancellation request so their core
        transaction may stop safely, but their observers are detached. Their
        verified owning queue therefore remains waiting/running for next-start
        recovery instead of being rewritten as a deliberate user cancellation.
        """
        changed=[]
        completion_listeners=[]
        with self._condition:
            self._shutting_down=True
            for job in self.jobs.values():
                if job.state not in {"queued","running","paused"}:
                    continue
                job._cancel.set(); job._pause.clear(); changed.append(job)
                if job.recover_on_shutdown:
                    self._listeners.pop(job.id,None)
                    self._progress_listeners.pop(job.id,None)
                if job.id in self._pending:
                    self._pending.pop(job.id,None)
                    self._finish_locked(job,"cancelled")
                    listeners=tuple(self._listeners.pop(job.id,{}).values())
                    self._progress_listeners.pop(job.id,None)
                    if listeners:
                        completion_listeners.append((job,listeners))
            self._condition.notify_all()
            threads=tuple(
                thread for job_id,thread in self._threads.items()
                if self.jobs.get(job_id) is not None
                and not self.jobs[job_id].recover_on_shutdown
            )
        for job in changed:
            self._changed(job)
        for job,listeners in completion_listeners:
            self._notify_listeners(job,listeners)
        with self._condition:
            self._prune_history_locked()
        if not wait:
            return not any(thread.is_alive() for thread in threads)
        deadline=time.monotonic()+max(0.0,float(timeout))
        current=threading.current_thread()
        for thread in threads:
            if thread is current:
                continue
            remaining=deadline-time.monotonic()
            if remaining<=0:
                break
            thread.join(remaining)
        return not any(thread.is_alive() for thread in threads)

    def diagnostic_snapshot(self, limit: int = 30) -> list[dict[str, object]]:
        """Return bounded job state without paths, error text, results, or request keys."""
        with self._condition:
            selected = [
                job
                for job in self.jobs.values()
                if job.state in {"queued", "running", "paused", "failed"}
            ][-max(0, int(limit)) :]
            return [
                {
                    "name": job.name[:160],
                    "state": job.state,
                    "current": job.current,
                    "total": job.total,
                    "resource": job.resource,
                    "external": job.external,
                    "has_error": bool(job.error),
                }
                for job in selected
            ]

    def scheduler_snapshot(self) -> dict[str,object]:
        """Return privacy-safe bounded scheduler and admission counters."""
        with self._condition:
            return {
                **self._counters,
                "queued":len(self._pending),
                "active":dict(self._active),
                "active_threads":len(self._threads),
                "limits":dict(self.limits),
                "queue_limit":self._queue_limit,
                "terminal_history":sum(
                    job.state in {"complete","cancelled","failed"}
                    for job in self.jobs.values()
                ),
                "history_limit":self._history_limit,
            }
