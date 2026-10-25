from __future__ import annotations

from dataclasses import dataclass,field
from pathlib import Path
from typing import Callable,Iterable

from .ai_interrogation import (
    AIInterrogationService,configured_plain_language_backend,
    configured_wd_backends,
)
from .dependencies import DependencyService
from .history_configuration import HistoryConfigurationError
from .history_versions import HistoryError


@dataclass
class AutomaticAIAnalysisResult:
    total_images: int = 0
    processed: int = 0
    skipped: int = 0
    auto_merged: int = 0
    conflicts_held: int = 0
    cancelled: bool = False
    errors: list[str] = field(default_factory=list)
    changed_paths: list[Path] = field(default_factory=list)
    history_operation_ids: list[str] = field(default_factory=list)
    history_interrupted: bool = False


class AutomaticAIAnalysisService:
    """Run resumable local AI, retaining review evidence and optional safe tag consensus."""

    def __init__(
        self,settings,*,knowledge=None,ai: AIInterrogationService|None=None,
        tag_backends=None,description_backend=None,batch_size: int=16,
        ai_work_lease=None,
    ) -> None:
        self.settings=settings; self.knowledge=knowledge
        self.ai=ai or AIInterrogationService()
        self._tag_backends=tag_backends; self._description_backend=description_backend
        self.batch_size=max(1,min(32,int(batch_size)))
        self.ai_work_lease=ai_work_lease

    def enabled(self) -> bool:
        return bool(
            self.settings.auto_run_wd_on_managed_images
            or self.settings.auto_run_joy_on_managed_images
        )

    def analyze_many(
        self,images: Iterable[str|Path],*,
        progress: Callable[[int,int,str],None]|None=None,
        should_cancel: Callable[[],bool]|None=None,
        history_operation=None,
        history_operation_factory=None,
    ) -> AutomaticAIAnalysisResult:
        if history_operation is not None and history_operation_factory is not None:
            raise ValueError("Supply one exact history operation or a bounded operation factory, not both.")
        paths=[Path(value) for value in images if Path(value).with_suffix(".tags.json").is_file()]
        result=AutomaticAIAnalysisResult(total_images=len(paths))
        if not paths or not self.enabled(): return result
        if self.ai_work_lease is None:
            return self._analyze_paths(
                paths,result,progress=progress,should_cancel=should_cancel,
                history_operation=history_operation,history_operation_factory=history_operation_factory,
            )
        with self.ai_work_lease.tracked("Automatic AI analysis"):
            return self._analyze_paths(
                paths,result,progress=progress,should_cancel=should_cancel,
                history_operation=history_operation,history_operation_factory=history_operation_factory,
            )

    def _analyze_paths(
        self,paths: list[Path],result: AutomaticAIAnalysisResult,*,
        progress: Callable[[int,int,str],None]|None,
        should_cancel: Callable[[],bool]|None,
        history_operation=None,
        history_operation_factory=None,
    ) -> AutomaticAIAnalysisResult:
        def capture(images):
            operation=(history_operation_factory(tuple(images))
                       if history_operation_factory is not None else history_operation)
            if operation is not None and operation.operation_id not in result.history_operation_ids:
                result.history_operation_ids.append(operation.operation_id)
            return {"history_operation":operation} if operation is not None else {}

        tag_backends=[]; description_backend=None
        dependencies=DependencyService(self.settings)
        if self.settings.auto_run_wd_on_managed_images:
            if self._tag_backends is not None:
                tag_backends.extend(self._tag_backends)
            elif dependencies.capability_ready("wd"):
                tag_backends.extend(configured_wd_backends(self.settings))
            else:
                result.errors.append("Automatic SDXL Tag analysis is enabled but its configured components are unavailable.")
        if self.settings.auto_run_joy_on_managed_images:
            if self._description_backend is not None:
                description_backend=self._description_backend
            elif dependencies.capability_ready("joy"):
                description_backend=configured_plain_language_backend(
                    self.settings,
                )
            else:
                result.errors.append("Automatic Plain Language Description analysis is enabled but its configured components are unavailable.")
        all_backends=[*tag_backends,*([description_backend] if description_backend else [])]
        auto_merge=bool(getattr(self.settings,"auto_merge_ai_tags",False))
        managed_count=sum(
            1 for backend in all_backends
            if callable(getattr(backend,"release",None))
        )
        tag_units=(
            len(paths)*len(tag_backends)
            + (len(paths) if tag_backends else 0)
            + (len(paths) if tag_backends and auto_merge else 0)
        )
        description_units=len(paths) if description_backend is not None else 0
        total=max(1,tag_units+description_units+(2*managed_count)); completed=0
        changed_paths=set(result.changed_paths)

        managed_tags=[backend for backend in tag_backends if callable(getattr(backend,"release",None))]
        if managed_tags:
            completed+=len(managed_tags)
            if progress: progress(completed,total,"Preparing SDXL Tag models…")
        try:
            for start in (range(0,len(paths),self.batch_size) if tag_backends else ()):
                if should_cancel and should_cancel():
                    result.cancelled=True; break
                chunk=paths[start:start+self.batch_size]
                history=capture(chunk)
                def report(index,_total,name,base=completed):
                    if progress: progress(min(total,base+index),total,name)
                batch=self.ai.analyze_taggers(
                    chunk,tag_backends,self.knowledge,report,force=False,
                    auto_merge=auto_merge,
                    minimum_agreement=max(2,int(getattr(self.settings,"wd_consensus_minimum",2))),
                    run_mode="automatic",_manage_lifecycle=False,
                    should_cancel=should_cancel,
                    **history,
                )
                result.processed+=batch.processed; result.skipped+=batch.skipped_existing
                result.auto_merged+=batch.auto_merged; result.conflicts_held+=batch.conflicts_held
                result.errors.extend(batch.errors)
                for path,item in batch.image_results.items():
                    if item.processed and path not in changed_paths:
                        changed_paths.add(path); result.changed_paths.append(path)
                completed+=len(chunk)*(len(tag_backends)+1+int(auto_merge))
                if getattr(batch,"history_interrupted",False):
                    result.history_interrupted=True
                    break
                if batch.cancelled:
                    result.cancelled=True
                    break
        except InterruptedError:
            result.cancelled=True
        except (HistoryError,HistoryConfigurationError,OSError) as exc:
            if history_operation is None and history_operation_factory is None:raise
            result.history_interrupted=True
            result.errors.append(str(exc))
        finally:
            for backend in managed_tags:
                try: backend.release()
                except Exception: pass
            if managed_tags:
                completed+=len(managed_tags)
                if progress: progress(min(total,completed),total,"Finished SDXL Tag models")

        if not result.cancelled and not result.history_interrupted and description_backend is not None:
            managed=callable(getattr(description_backend,"release",None))
            if managed:
                completed+=1
                if progress: progress(min(total,completed),total,"Preparing the Plain Language Description model…")
            try:
                for path in paths:
                    if should_cancel and should_cancel():
                        result.cancelled=True; break
                    history=capture((path,))
                    def report(index,_total,name,base=completed):
                        if progress: progress(min(total,base+index),total,name)
                    batch=self.ai.analyze_batch(
                        [path],description_backend,self.knowledge,report,force=False,
                        auto_merge=False,run_mode="automatic",_manage_lifecycle=False,
                        should_cancel=should_cancel,
                        **history,
                    )
                    result.processed+=batch.processed; result.skipped+=batch.skipped_existing
                    result.errors.extend(batch.errors)
                    for changed,item in batch.image_results.items():
                        if item.processed and changed not in changed_paths:
                            changed_paths.add(changed)
                            result.changed_paths.append(changed)
                    completed+=1
                    if getattr(batch,"history_interrupted",False):
                        result.history_interrupted=True
                        break
                    if batch.cancelled:
                        result.cancelled=True
                        break
            except InterruptedError:
                result.cancelled=True
            except (HistoryError,HistoryConfigurationError,OSError) as exc:
                if history_operation is None and history_operation_factory is None:raise
                result.history_interrupted=True
                result.errors.append(str(exc))
            finally:
                if managed:
                    try: description_backend.release()
                    except Exception: pass
                    completed+=1
                    if progress: progress(min(total,completed),total,"Finished the Plain Language Description model")
        return result
