"""Shared, read-only resolution filtering for primary image galleries."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QMenu, QPushButton, QWidget

from core.paths import CACHE_DIR
from core.resolution_policy import DimensionCache, ResolutionInspector, ResolutionPolicy, ResolutionState
from core.resolution_workflow import load_resolution_settings
from .workers import CoordinatedCallTask


class ResolutionFilter(QWidget):
    """Filter an existing projection using background decoded-dimension evidence."""
    changed = Signal()
    status_changed = Signal(str)

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state=state; self.records={}; self._known_paths=set(); self._pending_paths=set()
        self._task=None; self._generation=0; self._scope_epoch=0; self._policy_generation=0; self._shutdown=False
        self._review_scope_provider=None; self._policy_error=""; self.policy=ResolutionPolicy()
        layout=QHBoxLayout(self); layout.setContentsMargins(0,0,0,0); layout.setSpacing(5)
        self.label=QLabel("Resolution"); layout.addWidget(self.label)
        self.combo=QComboBox(); self.combo.setAccessibleName("Resolution filter")
        self.combo.setAccessibleDescription("Refine current results by decoded dimensions. Dimensions unavailable is separate from image problems.")
        self.combo.currentIndexChanged.connect(self.changed); layout.addWidget(self.combo)
        self.review_button=QPushButton("Review Resolution…"); self.review_button.setProperty("compact",True)
        self.review_button.clicked.connect(self._review); layout.addWidget(self.review_button)
        self._refresh_combo()
        self._recheck_timer=QTimer(self); self._recheck_timer.setInterval(30_000)
        self._recheck_timer.timeout.connect(self.recheck_known); self._recheck_timer.start()

    def set_review_scope_provider(self,provider): self._review_scope_provider=provider

    def use_command_surface(self):
        """Expose the existing filter choices while keeping active scope in view."""
        if hasattr(self,"command_menu"):return self.command_menu
        self.command_menu=QMenu(self)
        for index in range(self.combo.count()):
            value=self.combo.itemData(index)
            action=self.command_menu.addAction(self.combo.itemText(index))
            action.setData(value); action.setCheckable(True)
            action.triggered.connect(lambda _checked=False,value=value:self.combo.setCurrentIndex(self.combo.findData(value)))
        self.combo.hide()
        self.label.setObjectName("mutedLabel")
        self.changed.connect(self._sync_command_surface)
        self._sync_command_surface()
        return self.command_menu

    def _sync_command_surface(self):
        if not hasattr(self,"command_menu"):return
        selected=self.selected_state()
        for action in self.command_menu.actions():
            index=self.combo.findData(action.data())
            action.setText(self.combo.itemText(index))
            action.setChecked(action.data()==selected)
        self.label.setText(f"Resolution: {selected}")
        self.label.setAccessibleName(self.label.text())
        self.setVisible(bool(selected))

    def _review(self):
        paths=tuple(self._review_scope_provider() if callable(self._review_scope_provider) else ())
        if paths:
            from .resolution_dialog import open_resolution_workspace
            open_resolution_workspace(self.parentWidget(),paths=paths)

    def selected_state(self): return str(self.combo.currentData() or "")

    def _refresh_combo(self):
        selected=self.selected_state() if hasattr(self,"combo") else ""
        counts={state.value:0 for state in ResolutionState}
        for record in self.records.values():
            value=str(getattr(getattr(record,"state",""),"value",getattr(record,"state","")))
            counts[value]=counts.get(value,0)+1
        self.combo.blockSignals(True); self.combo.clear(); self.combo.addItem("All resolutions","")
        for state in ResolutionState:self.combo.addItem(f"{state.value} ({counts[state.value]:,})",state.value)
        self.combo.setCurrentIndex(max(0,self.combo.findData(selected))); self.combo.blockSignals(False)
        self._sync_command_surface()

    def filter_paths(self,paths):
        wanted=self.selected_state(); values=[Path(path) for path in paths]
        if not wanted:return values
        unavailable=ResolutionState.DIMENSIONS_UNAVAILABLE.value
        return [path for path in values if str(getattr(getattr(self.records.get(path),"state",unavailable),"value",getattr(self.records.get(path),"state",unavailable)))==wanted]

    def _safe_named_paths(self,paths):
        getter=getattr(getattr(self.state,"settings",None),"library_registry",None)
        if not callable(getter):return []
        try: registry=getter()
        except Exception:return []
        result=[]
        for value in paths:
            path=Path(value)
            try: profile=registry.profile_for_path(path)
            except Exception: profile=None
            if profile is not None and str(getattr(profile,"role",""))!="local_only":result.append(path)
        return result

    def observe_population(self,paths):
        """Update membership only. It never stats or decodes on the UI thread."""
        if self._shutdown:return
        current=set(self._safe_named_paths(paths)); removed=self._known_paths-current
        if removed:self.records={path:record for path,record in self.records.items() if path not in removed}
        self._pending_paths.update(current-self._known_paths); self._known_paths=current; self._scope_epoch+=1
        self._start_pending()

    def recheck_known(self):
        """Coalesced background freshness check; never invoked from rendering."""
        if not self._shutdown and self._task is None:
            self._pending_paths.update(self._known_paths); self._scope_epoch+=1; self._start_pending()

    def invalidate_policy(self):
        self.records.clear(); self._pending_paths.update(self._known_paths); self._scope_epoch+=1; self._policy_generation+=1
        self._refresh_combo(); self._start_pending()

    def _start_pending(self):
        if self._shutdown or self._task is not None or not self._pending_paths:return False
        jobs=getattr(self.state,"jobs",None)
        if jobs is None or getattr(jobs,"shutting_down",False):return False
        paths=tuple(sorted(self._pending_paths,key=lambda path:str(path).casefold()))
        self._pending_paths.difference_update(paths); self._generation+=1; generation,epoch=self._generation,self._scope_epoch
        policy_generation=self._policy_generation
        def work(job):
            try: settings=load_resolution_settings(); policy=ResolutionPolicy(**settings["policy"])
            except (OSError,ValueError) as exc:return generation,epoch,policy_generation,(),str(exc)
            # Policy privacy can change while this scope waits in the queue.
            safe_paths=tuple(self._safe_named_paths(paths))
            if not safe_paths:
                return generation,epoch,policy_generation,(),""
            # SQLite opening is deliberately inside the worker.
            with DimensionCache(CACHE_DIR/"resolution.sqlite3") as cache:
                inspector=ResolutionInspector(policy,cache)
                records=inspector.inspect(safe_paths,cancelled=lambda:not job.checkpoint(),progress=lambda current,total,name:job.update(current,total,name))
            return generation,epoch,policy_generation,records,""
        task=CoordinatedCallTask(jobs,"Inspect image resolutions",work,resource="io",key="resolution-inspect:"+"\n".join(str(path).casefold() for path in paths))
        self._task=task
        task.signals.progress.connect(lambda current,total,name:self.status_changed.emit(f"Checking image dimensions: {current:,} of {total:,} • {name}"))
        task.signals.complete.connect(lambda result,owner=task:self._complete(owner,result))
        task.signals.cancelled.connect(lambda owner=task:self._cancelled(owner))
        task.signals.failed.connect(lambda message,owner=task:self._failed(owner,message))
        try:task.start()
        except RuntimeError as exc:self._failed(task,str(exc)); return False
        return True

    def inspect(self,paths):
        """Explicitly queue a bounded safe scope for background refresh."""
        self._pending_paths.update(self._safe_named_paths(paths))
        self._scope_epoch+=1
        return self._start_pending()

    def _complete(self,task,result):
        if task is not self._task:return
        self._task=None; generation,_epoch,policy_generation,records,error=result
        if self._shutdown or generation!=self._generation:return
        if policy_generation!=self._policy_generation:
            self._pending_paths.update(self._known_paths)
            self._start_pending()
            return
        if error:
            self._policy_error=error; self.status_changed.emit(f"Resolution settings unavailable: {error}")
            self._pending_paths.update(self._known_paths); return
        self._policy_error=""; current=set(self._known_paths)
        published=[record for record in records if Path(record.path) in current]
        self.records.update({Path(record.path):record for record in published})
        self._refresh_combo(); self.status_changed.emit("")
        if published:
            try:
                from .resolution_dialog import schedule_automatic_resizing
                schedule_automatic_resizing(self.parentWidget(),published)
            except ImportError:pass
        self.changed.emit()
        if self._pending_paths:self._start_pending()

    def _cancelled(self,task):
        if task is self._task:self._task=None; self.status_changed.emit(""); self._start_pending()

    def _failed(self,task,message):
        if task is self._task:
            self._task=None; self.status_changed.emit(f"Resolution check unavailable: {message}")
            self._pending_paths.update(self._known_paths)

    def shutdown(self):
        self._shutdown=True; self._recheck_timer.stop(); task=self._task; self._task=None
        if task is not None:task.cancel()
