from __future__ import annotations

from dataclasses import dataclass,field
from datetime import datetime,timezone
import threading
import time
from typing import Any,Callable


RUNTIME_STATES=frozenset({"Loading","Ready in memory","In use","Not loaded"})


class RuntimeMemoryAdmissionError(RuntimeError):
    """A run-wide safe hold before unqualified GPU work."""


def _default_hardware_probe(gpu_index: int):
    # Keep process discovery out of construction/configuration and inside the
    # worker acquisition path. The dependency helper caches its bounded probe.
    from .dependencies import nvidia_gpu_profile
    return nvidia_gpu_profile(gpu_index)


def _default_free_memory_probe(key: "LocalAIRuntimeKey") -> int|None:
    """Fresh free dedicated memory only; never import/load an AI runtime."""
    provider=str(key.provider).casefold()
    if "cuda" not in provider:
        return None
    import shutil,subprocess
    executable=shutil.which("nvidia-smi")
    if not executable:
        return None
    try:
        result=subprocess.run(
            [executable,f"--id={int(key.device_index)}",
             "--query-gpu=memory.free","--format=csv,noheader,nounits"],
            capture_output=True,text=True,check=True,timeout=3,
            creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0),
        )
        value=int(float(result.stdout.strip().splitlines()[0]))
        return value if value>=0 else None
    except (OSError,ValueError,IndexError,subprocess.SubprocessError):
        return None


@dataclass(frozen=True,order=True)
class LocalAIRuntimeKey:
    model_id: str
    model_revision: str
    model_fingerprint: str
    adapter_profile: str
    provider: str
    device_index: int
    dtype: str
    quantization: str
    load_settings_fingerprint: str


@dataclass(frozen=True)
class LocalAIRuntimeSpec:
    key: LocalAIRuntimeKey
    display_name: str
    capability: str
    estimated_memory_mib: int
    loader: Callable[[],Any]=field(compare=False,repr=False)
    releaser: Callable[[Any],None]=field(compare=False,repr=False)
    health_check: Callable[[Any],bool]=field(
        default=lambda _resource:True,compare=False,repr=False,
    )
    inference_headroom_mib: int=field(default=0,compare=False)


@dataclass(frozen=True)
class LocalAIRuntimeStatus:
    key: LocalAIRuntimeKey
    display_name: str
    state: str
    device_label: str
    memory_mode: str
    idle_expires_at: str=""
    last_error: str=""


@dataclass
class _Entry:
    spec: LocalAIRuntimeSpec
    resource: Any=None
    state: str="Not loaded"
    last_used: float=0.0
    idle_expires_at: float=0.0
    last_error: str=""
    users: int=0
    retire_requested: bool=False
    use_lock: threading.Lock=field(default_factory=threading.Lock,repr=False)


class RuntimeLease:
    """One serialized use of a heavyweight runtime resource."""

    def __init__(
        self,resource: Any,close: Callable[[],None],*,
        key: LocalAIRuntimeKey|None=None,
    ) -> None:
        self.resource=resource
        self.key=key
        self._close=close
        self._closed=False
        self._lock=threading.Lock()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed=True
            close=self._close
            self.resource=None
            self._close=lambda:None
        # The reference and callback were detached before allocator cleanup,
        # including when a backend releaser raises unexpectedly.
        close()

    def __enter__(self) -> Any:
        return self.resource

    def __exit__(self,*_args) -> None:
        self.close()


class LocalAIRuntimeManager:
    """Application-owned, reclaimable cache of heavyweight local-AI runtimes.

    Entries retain only runtime resources and load/status metadata. Per-image
    prompts, facts, thresholds, cancellation callbacks, results, and evidence
    remain owned by short-lived backend adapters.
    """

    def __init__(
        self,jobs=None,events=None,*,hardware_probe: Callable[[int],Any]|None=None,
        clock: Callable[[],float]=time.monotonic,
        free_memory_probe: Callable[[LocalAIRuntimeKey],int|None]|None=None,
    ) -> None:
        self.jobs=jobs
        self.events=events
        self.hardware_probe=hardware_probe or _default_hardware_probe
        self.free_memory_probe=free_memory_probe or _default_free_memory_probe
        self.clock=clock
        self._lock=threading.RLock()
        self._condition=threading.Condition(self._lock)
        self._entries: dict[LocalAIRuntimeKey,_Entry]={}
        self._release_events: dict[LocalAIRuntimeKey,threading.Event]={}
        self._mode="on_demand"
        self._idle_seconds=600.0
        self._configured_budget_mib=0
        self._budget_mib=0
        self._resolved_budgets: dict[tuple[str,int],int]={}
        self._load_configuration=None
        self._closed=False

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    @property
    def budget_mib(self) -> int:
        with self._lock:
            return self._budget_mib

    def configure(self,settings) -> None:
        mode=str(getattr(settings,"ai_runtime_mode","on_demand") or "on_demand")
        if mode not in {"on_demand","keep_warm","preload"}:
            mode="on_demand"
        idle_minutes=max(1,min(120,int(getattr(
            settings,"ai_runtime_idle_minutes",10,
        ) or 10)))
        configured=max(0,int(getattr(
            settings,"ai_runtime_memory_budget_mb",0,
        ) or 0))
        load_configuration=(
            str(getattr(settings,"ai_models_directory","") or ""),
            tuple(sorted(
                str(value) for value in
                (getattr(settings,"enabled_wd_taggers",()) or ())
            )),
            str(getattr(settings,"wd_execution_provider","auto") or "auto"),
            max(0,int(getattr(settings,"joycaption_gpu_index",0) or 0)),
            str(getattr(settings,"plain_language_model","") or ""),
            str(getattr(settings,"joycaption_quantization","bf16") or "bf16"),
        )
        with self._lock:
            mode_changed=mode!=self._mode
            idle_changed=idle_minutes*60!=self._idle_seconds
            budget_changed=configured!=self._configured_budget_mib
            load_changed=load_configuration!=self._load_configuration
            changed=mode_changed or idle_changed or budget_changed or load_changed
            previous_mode=self._mode
            self._mode=mode
            self._idle_seconds=float(idle_minutes*60)
            self._configured_budget_mib=configured
            if configured:
                self._budget_mib=max(256,configured)
            elif budget_changed or load_changed:
                self._budget_mib=0
            self._load_configuration=load_configuration
            if budget_changed or load_changed:
                self._resolved_budgets.clear()
            retire=(
                mode=="on_demand" or previous_mode!=mode
                or budget_changed or load_changed
            )
            if retire:
                for entry in self._entries.values():
                    entry.retire_requested=True
            elif idle_changed:
                for entry in self._entries.values():
                    if entry.resource is not None and not entry.users:
                        entry.idle_expires_at=entry.last_used+self._idle_seconds
            if changed:
                self._condition.notify_all()
        if retire:
            self._retire_idle_async("AI runtime settings changed")
        if changed:
            self._publish()

    @staticmethod
    def _resource_group(key: LocalAIRuntimeKey) -> tuple[str,int]:
        provider=str(key.provider or "automatic").casefold()
        if "cuda" in provider:
            return "cuda",max(0,int(key.device_index))
        if "cpu" in provider:
            return "cpu",0
        return provider,max(0,int(key.device_index))

    def _automatic_budget_mib(self,spec: LocalAIRuntimeSpec) -> int:
        group=self._resource_group(spec.key)
        if group[0]=="cuda":
            profile=None
            try:
                profile=self.hardware_probe(group[1])
            except Exception:
                profile=None
            total=max(0,int(getattr(profile,"total_memory_mib",0) or 0))
            if total:
                # Leave headroom for the UI, decode buffers, and allocator variance.
                return max(1024,min(total-1024,int(total*.75)))
        elif group[0]=="cpu":
            try:
                import psutil
                memory=psutil.virtual_memory()
                total=max(0,int(memory.total//(1024*1024)))
                available=max(0,int(memory.available//(1024*1024)))
                if total and available:
                    return max(1024,min(int(total*.5),int(available*.75)))
            except (ImportError,OSError,TypeError,ValueError):
                pass
        return 4096

    def _budget_for(self,spec: LocalAIRuntimeSpec) -> int:
        group=self._resource_group(spec.key)
        with self._lock:
            if self._configured_budget_mib:
                return self._budget_mib
            cached=self._resolved_budgets.get(group)
        if cached is not None:
            return cached
        budget=max(256,int(self._automatic_budget_mib(spec)))
        with self._lock:
            if self._configured_budget_mib:
                return self._budget_mib
            budget=self._resolved_budgets.setdefault(group,budget)
            self._budget_mib=budget
            return budget

    @staticmethod
    def _device_label(key: LocalAIRuntimeKey) -> str:
        provider=str(key.provider or "").casefold()
        if "cuda" in provider:
            return f"NVIDIA GPU {key.device_index}"
        return "CPU" if "cpu" in provider else str(key.provider or "Automatic")

    @staticmethod
    def _memory_mode(key: LocalAIRuntimeKey) -> str:
        return str(key.quantization or key.dtype or "default").upper()

    def _status_locked(self,entry: _Entry) -> LocalAIRuntimeStatus:
        expiry=""
        if entry.state=="Ready in memory" and entry.idle_expires_at:
            remaining=max(0.0,entry.idle_expires_at-self.clock())
            expiry=(datetime.now(timezone.utc).timestamp()+remaining)
            expiry=datetime.fromtimestamp(expiry,timezone.utc).isoformat()
        return LocalAIRuntimeStatus(
            entry.spec.key,entry.spec.display_name,entry.state,
            self._device_label(entry.spec.key),self._memory_mode(entry.spec.key),
            expiry,entry.last_error,
        )

    def snapshot(self) -> tuple[LocalAIRuntimeStatus,...]:
        with self._lock:
            values=[self._status_locked(entry) for entry in self._entries.values()]
        return tuple(sorted(values,key=lambda item:item.key))

    def needs_maintenance(self) -> bool:
        """Return whether a bounded reap/release pass has actionable work."""
        with self._lock:
            now=self.clock()
            return bool(self._release_events) or any(
                entry.retire_requested
                or (
                    entry.resource is not None and not entry.users
                    and entry.idle_expires_at and entry.idle_expires_at<=now
                )
                for entry in self._entries.values()
            )

    def _publish(self) -> None:
        publish=getattr(self.events,"publish",None)
        if callable(publish):
            try:
                publish("ai_runtime_changed",statuses=self.snapshot())
            except Exception:
                # Status observers cannot take ownership of runtime safety.
                pass

    @staticmethod
    def _release_one(spec: LocalAIRuntimeSpec,resource: Any) -> None:
        if resource is None:
            return
        try:
            spec.releaser(resource)
        except Exception:
            pass

    def _release_many(self,values: list[tuple[LocalAIRuntimeSpec,Any]]) -> None:
        for spec,resource in values:
            try:
                self._release_one(spec,resource)
            finally:
                with self._lock:
                    event=self._release_events.pop(spec.key,None)
                    if event is not None:
                        event.set()
                    self._condition.notify_all()

    def _release_async(self,values: list[tuple[LocalAIRuntimeSpec,Any]]) -> None:
        if not values:
            return
        threading.Thread(
            target=self._release_many,args=(values,),
            name="CollectiGen AI runtime release",daemon=True,
        ).start()

    def _detach_idle_locked(
        self,predicate: Callable[[LocalAIRuntimeKey],bool]|None=None,
    ) -> list[tuple[LocalAIRuntimeSpec,Any]]:
        released=[]
        for key,entry in tuple(self._entries.items()):
            if predicate is not None and not predicate(key):
                continue
            entry.retire_requested=True
            if entry.users or entry.state=="Loading":
                continue
            if entry.resource is None:
                self._entries.pop(key,None)
                continue
            released.append((entry.spec,entry.resource))
            self._release_events.setdefault(key,threading.Event()).clear()
            entry.resource=None
            entry.state="Not loaded"
            entry.idle_expires_at=0.0
            self._entries.pop(key,None)
        return released

    def _retire_idle_async(self,reason: str) -> None:
        del reason
        with self._lock:
            released=self._detach_idle_locked()
            self._condition.notify_all()
        self._release_async(released)

    def invalidate(
        self,predicate: Callable[[LocalAIRuntimeKey],bool]|None=None,*,
        reason: str="configuration changed",
    ) -> None:
        del reason
        with self._lock:
            released=self._detach_idle_locked(predicate)
            self._condition.notify_all()
        self._release_async(released)
        self._publish()

    def _retained_mib_locked(self,group: tuple[str,int]) -> int:
        return sum(
            max(0,int(entry.spec.estimated_memory_mib))
            for entry in self._entries.values()
            if entry.resource is not None
            and self._resource_group(entry.spec.key)==group
        )

    def _evict_for(self,spec: LocalAIRuntimeSpec) -> None:
        released=[]; group=self._resource_group(spec.key)
        budget=self._budget_for(spec)
        with self._lock:
            wanted=max(0,int(spec.estimated_memory_mib))
            candidates=sorted(
                (
                    entry for entry in self._entries.values()
                    if entry.resource is not None and not entry.users
                    and entry.spec.key!=spec.key
                    and self._resource_group(entry.spec.key)==group
                ),
                key=lambda item:(item.last_used,item.spec.key),
            )
            while (
                candidates
                and self._retained_mib_locked(group)+wanted>budget
            ):
                victim=candidates.pop(0)
                self._entries.pop(victim.spec.key,None)
                released.append((victim.spec,victim.resource))
                self._release_events.setdefault(
                    victim.spec.key,threading.Event(),
                ).clear()
                victim.resource=None; victim.state="Not loaded"
            self._condition.notify_all()
        self._release_many(released)
        if released:
            self._publish()

    def _evict_all_idle_except(self,spec: LocalAIRuntimeSpec) -> None:
        released=[]; key=spec.key; group=self._resource_group(key)
        with self._lock:
            for candidate,entry in tuple(self._entries.items()):
                if candidate==key or entry.users or entry.resource is None:
                    continue
                if self._resource_group(candidate)!=group:
                    continue
                self._entries.pop(candidate,None)
                released.append((entry.spec,entry.resource))
                self._release_events.setdefault(candidate,threading.Event()).clear()
                entry.resource=None; entry.state="Not loaded"
            self._condition.notify_all()
        self._release_many(released)
        if released:
            self._publish()

    def _admit_measured_memory(self,spec: LocalAIRuntimeSpec,*,loading=True,should_cancel=None,allow_memory_pressure=False) -> None:
        """Fresh headroom gate, reclaiming only idle resources owned by this manager."""
        if self._resource_group(spec.key)[0]!="cuda":
            return
        needed=max(0,int(spec.estimated_memory_mib if loading else spec.inference_headroom_mib))+1024
        def measure():
            if self._cancelled(should_cancel):
                raise InterruptedError("AI memory admission cancelled safely")
            try:
                value=self.free_memory_probe(spec.key)
                return None if value is None else max(0,int(value))
            except (OSError,ValueError,TypeError):
                return None
        free=measure()
        if free is None:
            if allow_memory_pressure:
                return
            raise RuntimeMemoryAdmissionError(
                "Available dedicated GPU memory could not be verified. "
                "No new AI model was admitted. Check GPU status and retry; "
                "other applications and active work are untouched."
            )
        if free<needed:
            self._evict_all_idle_except(spec)
            free=measure()
        if free is None or free<needed:
            if allow_memory_pressure:
                return
            available="unavailable" if free is None else f"{free:,} MiB"
            raise RuntimeMemoryAdmissionError(
                f"Insufficient verified dedicated GPU headroom for {spec.display_name}: "
                f"{available} free; {needed:,} MiB required "
                "(task peak/headroom estimate plus 1,024 MiB reserve). "
                "CollectiGen idle resources were released where possible and may reload next time; active leases "
                "and other applications are untouched. Finish other GPU work, choose "
                "a smaller supported model, or retry after freeing memory."
            )

    @staticmethod
    def _allocation_failure(exc: Exception) -> bool:
        detail=str(exc).casefold()
        return isinstance(exc,MemoryError) or any(value in detail for value in (
            "out of memory","failed to allocate","memory allocation",
        ))

    @staticmethod
    def _cancelled(should_cancel: Callable[[],bool]|None) -> bool:
        return bool(should_cancel is not None and should_cancel())

    def _cold_lease(
        self,spec: LocalAIRuntimeSpec,should_cancel: Callable[[],bool]|None,
        *,allow_memory_pressure: bool=False,
    ) -> RuntimeLease:
        if self._cancelled(should_cancel):
            raise InterruptedError("AI runtime loading cancelled safely")
        resource=None
        try:
            resource=spec.loader()
            if self._cancelled(should_cancel):
                raise InterruptedError("AI runtime loading cancelled safely")
            if not spec.health_check(resource):
                raise RuntimeError(f"{spec.display_name} failed its runtime health check.")
            self._admit_measured_memory(spec,loading=False,should_cancel=should_cancel,allow_memory_pressure=allow_memory_pressure)
        except Exception:
            self._release_one(spec,resource)
            raise
        return RuntimeLease(
            resource,lambda:self._release_one(spec,resource),key=spec.key,
        )

    def acquire(
        self,spec: LocalAIRuntimeSpec,*,should_cancel: Callable[[],bool]|None=None,
        purpose: str="analysis",bypass_cache: bool=False,
        allow_memory_pressure: bool=False,
    ) -> RuntimeLease:
        del purpose
        if bypass_cache:
            self._admit_measured_memory(spec,should_cancel=should_cancel,allow_memory_pressure=allow_memory_pressure)
            return self._cold_lease(spec,should_cancel,allow_memory_pressure=allow_memory_pressure)
        while True:
            with self._lock:
                if self._closed:
                    raise RuntimeError("Local AI runtimes are shutting down.")
                pending_release=self._release_events.get(spec.key)
            if pending_release is not None:
                if pending_release.wait(.05):
                    continue
                if self._cancelled(should_cancel):
                    raise InterruptedError("AI runtime acquisition cancelled safely")
                continue
            with self._lock:
                if self._closed:
                    raise RuntimeError("Local AI runtimes are shutting down.")
                entry=self._entries.get(spec.key)
                if entry is None:
                    entry=_Entry(spec=spec,last_used=self.clock())
                    self._entries[spec.key]=entry
                else:
                    entry.spec=spec
            while not entry.use_lock.acquire(timeout=.05):
                if self._cancelled(should_cancel):
                    raise InterruptedError("AI runtime acquisition cancelled safely")
                with self._lock:
                    if self._closed:
                        raise RuntimeError("Local AI runtimes are shutting down.")
            with self._lock:
                if self._entries.get(spec.key) is entry:
                    break
            entry.use_lock.release()
        resource=None
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError("Local AI runtimes are shutting down.")
                entry.users=1
                entry.retire_requested=False
                if entry.resource is not None:
                    try:
                        healthy=bool(spec.health_check(entry.resource))
                    except Exception:
                        healthy=False
                    if not healthy:
                        resource=entry.resource
                        entry.resource=None
                        entry.state="Not loaded"
                if entry.resource is None:
                    entry.state="Loading"; entry.last_error=""
                else:
                    entry.state="In use"
                self._condition.notify_all()
            if resource is not None:
                self._release_one(spec,resource); resource=None
            self._publish()
            if entry.resource is None:
                self._evict_for(spec)
                self._admit_measured_memory(spec,should_cancel=should_cancel,allow_memory_pressure=allow_memory_pressure)
                if self._cancelled(should_cancel):
                    raise InterruptedError("AI runtime loading cancelled safely")
                try:
                    try:
                        resource=spec.loader()
                    except Exception as first:
                        if not self._allocation_failure(first):
                            raise
                        self._release_one(spec,resource); resource=None
                        self._evict_all_idle_except(spec)
                        if self._cancelled(should_cancel):
                            raise InterruptedError(
                                "AI runtime loading cancelled safely"
                            ) from first
                        self._admit_measured_memory(spec,should_cancel=should_cancel,allow_memory_pressure=allow_memory_pressure)
                        resource=spec.loader()
                    if self._cancelled(should_cancel):
                        raise InterruptedError("AI runtime loading cancelled safely")
                    if not spec.health_check(resource):
                        raise RuntimeError(
                            f"{spec.display_name} failed its runtime health check."
                        )
                except Exception as exc:
                    self._release_one(spec,resource); resource=None
                    with self._lock:
                        entry.users=0; entry.state="Not loaded"
                        entry.last_error=str(exc).strip() or type(exc).__name__
                        self._entries.pop(spec.key,None)
                        self._condition.notify_all()
                    self._publish()
                    raise
                with self._lock:
                    entry.resource=resource; entry.state="In use"
                    entry.last_used=self.clock(); entry.last_error=""
                    self._condition.notify_all()
            if entry.resource is not None:
                self._admit_measured_memory(spec,loading=False,should_cancel=should_cancel,allow_memory_pressure=allow_memory_pressure)
            return RuntimeLease(
                entry.resource,lambda:self._return(entry),key=spec.key,
            )
        except Exception:
            # Admission failures retain/release owned resources by the same policy
            # as an ordinary lease return; never leave an unleased In use entry.
            entry.retire_requested=True
            self._return(entry)
            raise

    def _return(self,entry: _Entry) -> None:
        release=None; release_event=None
        try:
            with self._lock:
                entry.users=0; entry.last_used=self.clock()
                group=self._resource_group(entry.spec.key)
                budget=(self._budget_mib if self._configured_budget_mib else
                        self._resolved_budgets.get(group,4096))
                if (
                    self._closed or self._mode=="on_demand"
                    or entry.retire_requested
                    or int(entry.spec.estimated_memory_mib)>budget
                ):
                    release=entry.resource; entry.resource=None
                    entry.state="Not loaded"; entry.idle_expires_at=0.0
                    if release is not None:
                        if self._entries.get(entry.spec.key) is entry:
                            self._entries.pop(entry.spec.key,None)
                        release_event=self._release_events.setdefault(
                            entry.spec.key,threading.Event(),
                        )
                        release_event.clear()
                else:
                    entry.state="Ready in memory"
                    entry.idle_expires_at=entry.last_used+self._idle_seconds
                self._condition.notify_all()
            # Retain the per-entry serialization boundary through allocator and
            # CUDA cleanup; a following load must not overlap empty_cache().
            self._release_one(entry.spec,release)
        finally:
            if release_event is not None:
                with self._lock:
                    current=self._release_events.get(entry.spec.key)
                    if current is release_event:
                        self._release_events.pop(entry.spec.key,None)
                    release_event.set()
                    self._condition.notify_all()
            entry.use_lock.release()
        self._publish()

    def reap_expired(self) -> int:
        now=self.clock(); released=[]
        with self._lock:
            for key,entry in tuple(self._entries.items()):
                if (
                    not entry.users and entry.state!="Loading"
                    and entry.resource is None
                ):
                    self._entries.pop(key,None)
                    continue
                if (
                    entry.resource is not None and not entry.users
                    and entry.idle_expires_at and entry.idle_expires_at<=now
                ):
                    self._entries.pop(key,None)
                    released.append((entry.spec,entry.resource))
                    self._release_events.setdefault(key,threading.Event()).clear()
                    entry.resource=None; entry.state="Not loaded"
            self._condition.notify_all()
        self._release_many(released)
        if released:
            self._publish()
        return len(released)

    def release_all(self,reason: str,*,wait: bool=True) -> bool:
        del reason
        with self._lock:
            for entry in self._entries.values():
                entry.retire_requested=True
            released=self._detach_idle_locked()
            if wait:
                while any(entry.users or entry.state=="Loading" for entry in self._entries.values()):
                    self._condition.wait(.05)
                released.extend(self._detach_idle_locked())
            self._condition.notify_all()
        if wait:
            self._release_many(released)
            while True:
                with self._lock:
                    pending=tuple(self._release_events.values())
                if not pending:
                    break
                for event in pending:
                    event.wait(.05)
        else:
            self._release_async(released)
        self._publish()
        with self._lock:
            return not self._entries and not self._release_events

    def shutdown(self,*,wait: bool=True) -> bool:
        with self._lock:
            self._closed=True
        return self.release_all("application shutdown",wait=wait)

    def preload(
        self,specs,*,progress: Callable[[int,int,str],None]|None=None,
        should_cancel: Callable[[],bool]|None=None,
    ) -> tuple[LocalAIRuntimeStatus,...]:
        values=tuple(specs); report=progress or (lambda *_args:None)
        for index,spec in enumerate(values,1):
            if self._cancelled(should_cancel):
                raise InterruptedError("AI runtime preload cancelled safely")
            report(index-1,len(values),f"Loading {spec.display_name}")
            lease=self.acquire(spec,should_cancel=should_cancel,purpose="preload")
            lease.close()
            report(index,len(values),f"{spec.display_name} is ready in memory")
        return self.snapshot()


def preparation_memory_review(settings,options,*,probe=None) -> str:
    """Content-free read-only observation; actual admission always remeasures."""
    from .ai_interrogation import configured_runtime_specs
    capabilities=[]
    if options.suggest_sdxl_tags:capabilities.append("wd")
    if options.suggest_plain_language:capabilities.append("joy")
    specs=configured_runtime_specs(settings,capabilities=capabilities)
    measure=probe or _default_free_memory_probe
    lines=[]
    for spec in specs:
        if "cuda" not in spec.key.provider.casefold():
            lines.append(f"{spec.display_name}: CPU selected; no dedicated VRAM admission.")
            continue
        try:value=measure(spec.key)
        except (OSError,ValueError,TypeError):value=None
        free="unavailable / not verified" if value is None else f"{int(value):,} MiB measured free"
        lines.append(f"{spec.display_name}: {free}; {spec.estimated_memory_mib:,} MiB estimated task peak + 1,024 MiB reserve = {spec.estimated_memory_mib+1024:,} MiB admission requirement.")
    if not lines:
        lines.append("Selected AI model memory could not be assessed. Model/runtime readiness is still required.")
    return "\n".join(lines)+"\n\nEstimates are uncertain, not guaranteed peak measurements. Memory is rechecked at each model transition. Idle CollectiGen resources may be released; active leases and other apps stay untouched. Parallel execution can exhaust VRAM, spill into shared memory, slow down or fail. This choice does not bypass model readiness, the existing CollectiGen task queue or active runtime leases. It never reduces image scope or quality, cancels generation or unloads SwarmUI. External queue/cancel/unload choices are unavailable here."
