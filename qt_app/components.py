from __future__ import annotations

from dataclasses import dataclass
from shiboken6 import isValid

import core
from PySide6.QtCore import QAbstractTableModel,QEvent,QModelIndex,QObject,QRect,QSize,QTimer,Qt,Signal
from PySide6.QtGui import QBrush,QColor,QKeySequence,QPainter,QPalette,QShortcut,QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,QAbstractScrollArea,QBoxLayout,QCheckBox,QComboBox,QCompleter,QDialog,QDialogButtonBox,QDoubleSpinBox,QHBoxLayout,QLabel,QListWidget,
    QFrame,QLineEdit,QMainWindow,QPlainTextEdit,QProgressBar,QPushButton,QScrollArea,QSizePolicy,QSpinBox,QStatusBar,QStyle,QTextBrowser,QTreeWidget,QVBoxLayout,QWidget,
)


def bounded_window_size(
    available: QRect,
    preferred: QSize,
    *,
    horizontal_margin: int = 48,
    vertical_margin: int = 96,
) -> QSize:
    """Keep a preferred top-level size inside the monitor's usable work area."""
    usable_width=max(1,available.width()-max(0,int(horizontal_margin)))
    usable_height=max(1,available.height()-max(0,int(vertical_margin)))
    return QSize(
        min(max(1,preferred.width()),usable_width),
        min(max(1,preferred.height()),usable_height),
    )


def counted_noun(count: int, singular: str, plural: str="") -> str:
    """Format a count with natural singular or plural wording."""

    value=max(0,int(count))
    noun=singular if value==1 else (plural or f"{singular}s")
    return f"{value:,} {noun}"


class ElidingLabel(QLabel):
    """Keep one-line shell text readable while preserving its complete value."""

    def __init__(self,text="",parent=None):
        super().__init__("",parent); self._complete_text=""
        self.setText(text)

    def completeText(self):
        return self._complete_text

    def setText(self,text):
        self._complete_text=str(text or "")
        self._refresh_elision()

    def resizeEvent(self,event):
        super().resizeEvent(event); self._refresh_elision()

    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange,QEvent.StyleChange):
            self._refresh_elision()

    def _refresh_elision(self):
        width=max(0,self.contentsRect().width())
        visible=self.fontMetrics().elidedText(
            self._complete_text,Qt.ElideRight,width,
        )
        super().setText(visible)


from .browse_components import IncrementalResultRow, IncrementalResultModel

class _SearchTermsCompleter(QCompleter):
    """Filter on hidden search terms while inserting the visible choice label."""

    def pathFromIndex(self,index):
        return str(index.data(Qt.DisplayRole) or "")


class BoundedSearchableComboBox(QComboBox):
    """Stable-ID choice selector with obvious, bounded contains-search."""

    SEARCH_ROLE=Qt.UserRole+32
    SEARCH_EXTRAS_ROLE=Qt.UserRole+33

    def __init__(
        self,parent=None,*,placeholder="Type to filter choices…",max_visible=12,
        allow_custom=False,
    ):
        super().__init__(parent)
        self._last_valid_index=-1
        self._allow_custom=bool(allow_custom)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.setMaxVisibleItems(max_visible)
        self.lineEdit().setPlaceholderText(placeholder)
        completer=_SearchTermsCompleter(self.model(),self)
        completer.setCompletionRole(self.SEARCH_ROLE)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        completer.setMaxVisibleItems(max_visible)
        self.setCompleter(completer)
        completer.activated[QModelIndex].connect(self._completion_activated)
        boundary=(
            "Supported custom text is validated before it can be saved."
            if self._allow_custom else
            "Unsupported text is not accepted."
        )
        self.setAccessibleDescription(
            "Type any part of a choice or supported alternate name to filter "
            f"the bounded result list. {boundary}"
        )
        self.currentIndexChanged.connect(self._remember_valid_index)
        self.lineEdit().editingFinished.connect(self._restore_valid_choice)

    def addItem(self,*args,search_terms=()):
        super().addItem(*args)
        index=self.count()-1
        self.setItemSearchTerms(index,search_terms)

    def addItems(self,texts):
        for value in texts:
            self.addItem(value)

    def setModel(self,model):
        """Replace the choice model while keeping contains-search in sync."""
        self._last_valid_index=-1
        super().setModel(model)
        completer=self.completer()
        if completer is not None:
            completer.setModel(model)

    def clear(self):
        self._last_valid_index=-1
        super().clear()

    def setItemText(self,index,text):
        super().setItemText(index,text)
        if 0<=index<self.count():
            extras=self.itemData(index,self.SEARCH_EXTRAS_ROLE) or ()
            self.setItemSearchTerms(index,extras)

    def setItemSearchTerms(self,index,search_terms=()):
        if not 0<=index<self.count():
            return
        if isinstance(search_terms,str):
            search_terms=(search_terms,)
        extras=tuple(
            str(value).strip() for value in search_terms
            if str(value).strip()
        )
        self.setItemData(index,extras,self.SEARCH_EXTRAS_ROLE)
        combined="\n".join(dict.fromkeys((self.itemText(index),*extras)))
        self.setItemData(index,combined,self.SEARCH_ROLE)

    def _completion_activated(self,index):
        model=self.completer().completionModel()
        source=model.mapToSource(index) if hasattr(model,"mapToSource") else QModelIndex()
        if source.isValid(): self.setCurrentIndex(source.row())

    def keyPressEvent(self,event):
        if event.key()==Qt.Key_Escape:
            self.completer().popup().hide()
            if 0<=self._last_valid_index<self.count():
                self.setCurrentIndex(self._last_valid_index)
                self.lineEdit().setText(self.itemText(self._last_valid_index))
            elif self.count():
                self.setCurrentIndex(0)
            else:
                self.clearEditText()
            event.accept()
            return
        super().keyPressEvent(event)

    def _remember_valid_index(self,index):
        if index>=0:self._last_valid_index=index

    def _restore_valid_choice(self):
        text=self.currentText().strip().casefold()
        exact=[
            index for index in range(self.count())
            if self.itemText(index).strip().casefold()==text
        ]
        current=self.currentIndex()
        if current in exact:
            self.setCurrentIndex(current)
        elif len(exact)==1:
            self.setCurrentIndex(exact[0])
        elif self._last_valid_index in exact:
            self.setCurrentIndex(self._last_valid_index)
        elif self._allow_custom:
            return
        elif 0<=self._last_valid_index<self.count():
            self.setCurrentIndex(self._last_valid_index)
            self.lineEdit().setText(self.itemText(self._last_valid_index))
        elif self.count():
            self.setCurrentIndex(0)


class _RangeCheckMixin:
    """Give checkable item views familiar visible-range Shift-click behavior."""

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self._range_anchor=None
        self._range_press=None
        self.setToolTip("Check one item, then Shift-click another checkbox to change the visible range.")

    def clear(self):
        self.reset_range_anchor()
        super().clear()

    def reset_range_anchor(self):
        self._range_anchor=None
        self._range_press=None

    def mousePressEvent(self,event):
        position=event.position().toPoint()
        item=self.itemAt(position)
        allowed=item is not None and self._range_click_allowed(position)
        before=self._range_state(item) if allowed else None
        self._range_press=(item,before,bool(event.modifiers() & Qt.ShiftModifier),self._range_anchor)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self,event):
        pressed=self._range_press
        super().mouseReleaseEvent(event)
        self._range_press=None
        if not pressed:
            return
        item,before,shift,anchor=pressed
        target_index=self._range_index(item) if item is not None else -1
        if target_index<0 or self._range_state(item)==before:
            return
        if not shift:
            self._range_anchor=item
            return
        anchor_index=self._range_index(anchor) if anchor is not None else -1
        if anchor_index<0:
            self._range_anchor=item
            return
        state=self._range_state(item)
        for index in range(min(anchor_index,target_index),max(anchor_index,target_index)+1):
            candidate=self._range_item(index)
            if not candidate.isHidden():
                self._range_set_state(candidate,state)


class RangeCheckTreeWidget(_RangeCheckMixin,QTreeWidget):
    """A tree whose checkboxes support Shift-click ranges in the first column."""

    def _range_click_allowed(self,position): return self.columnAt(position.x())==0
    def _range_index(self,item): return self.indexOfTopLevelItem(item)
    def _range_item(self,index): return self.topLevelItem(index)
    def _range_state(self,item): return item.checkState(0)
    def _range_set_state(self,item,state): item.setCheckState(0,state)


class ExtendedSelectionTreeWidget(QTreeWidget):
    """A row-oriented tree with the application's standard multi-selection behavior."""

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setAllColumnsShowFocus(True)
        guidance="Select rows with Click, Ctrl+Click, or Shift+Click."
        self.setToolTip(guidance)
        self.setAccessibleDescription(guidance)


class RangeCheckListWidget(_RangeCheckMixin,QListWidget):
    """A list whose checkboxes support Shift-click ranges."""

    def _range_click_allowed(self,_position): return True
    def _range_index(self,item): return self.row(item)
    def _range_item(self,index): return self.item(index)
    def _range_state(self,item): return item.checkState()
    def _range_set_state(self,item,state): item.setCheckState(state)


class _ScrollWheelGuard(QObject):
    """Keep a value control from stealing a scrollable form's wheel."""

    def eventFilter(self,watched,event):
        if event.type()!=QEvent.Wheel:
            return False
        parent=watched.parentWidget()
        while parent is not None and not isinstance(parent,QAbstractScrollArea):
            parent=parent.parentWidget()
        if parent is None:
            return False
        bar=parent.verticalScrollBar()
        pixel_y=event.pixelDelta().y()
        angle_y=event.angleDelta().y()
        if pixel_y:
            amount=pixel_y
        elif angle_y:
            amount=round((angle_y/120)*max(1,bar.singleStep())*3)
        else:
            return False
        bar.setValue(bar.value()-amount)
        event.accept()
        return True


def guard_scrollable_value_controls(root: QWidget) -> None:
    """Apply the shared wheel policy to selectable/numeric controls in ``root``."""
    guard=_ScrollWheelGuard(root)
    root._scroll_wheel_guard=guard
    for kind in (QComboBox,QSpinBox,QDoubleSpinBox):
        for control in root.findChildren(kind):
            control.installEventFilter(guard)


def compact_button(button: QPushButton, *, primary: bool | None = None) -> QPushButton:
    """Keep an action touch-friendly without letting sparse layouts stretch it."""
    button.setProperty("compact",True)
    if primary is not None: button.setProperty("primary",primary)
    button.setSizePolicy(QSizePolicy.Maximum,QSizePolicy.Fixed)
    return button


def show_operation_in_progress(widget: QWidget, active: str) -> None:
    """Report a harmless long-work collision without interrupting the user."""
    window=widget.window(); status_bar=getattr(window,"statusBar",lambda:None)()
    detail=str(active or "Another background task is still running").strip().rstrip(".")
    message=f"Already working: {detail}. You can keep reviewing while it finishes."
    if status_bar is not None:status_bar.showMessage(message,6000)
    else:window_status_footer(window).show_message(message)


class IndeterminateActivityBar(QWidget):
    """A theme-aware moving segment that never implies a fake percentage."""

    def __init__(self,parent=None):
        super().__init__(parent)
        self._active=False; self._phase=0.0
        self._timer=QTimer(self); self._timer.setInterval(40)
        self._timer.timeout.connect(self._advance)
        self.setMinimumHeight(18); self.setMaximumHeight(18)
        self.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
        self.setAccessibleName("Background activity indicator")

    @property
    def active(self):
        return self._active

    def _motion_enabled(self):
        return bool(self.style().styleHint(QStyle.SH_Widget_Animate,None,self))

    def set_active(self,active: bool,detail: str="") -> None:
        self._active=bool(active)
        self.setAccessibleDescription(str(detail or ""))
        if self._active and self._motion_enabled():
            if not self._timer.isActive():self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _advance(self):
        if not self._active:
            self._timer.stop(); return
        self._phase=(self._phase+0.04)%2.0
        self.update()

    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type()==QEvent.StyleChange and self._active:
            if self._motion_enabled():self._timer.start()
            else:self._timer.stop()

    def paintEvent(self,_event):
        painter=QPainter(self); painter.setRenderHint(QPainter.Antialiasing,True)
        track=self.rect().adjusted(1,3,-1,-3)
        if track.width()<=0 or track.height()<=0:return
        radius=track.height()/2
        palette=self.palette()
        painter.setPen(palette.color(QPalette.Midlight))
        painter.setBrush(palette.color(QPalette.Base))
        painter.drawRoundedRect(track,radius,radius)
        if not self._active:return
        segment=max(44,min(160,round(track.width()*0.22)))
        segment=min(segment,track.width())
        travel=max(0,track.width()-segment)
        position=self._phase if self._phase<=1.0 else 2.0-self._phase
        left=track.left()+round(travel*position)
        chunk=QRect(left,track.top(),segment,track.height())
        painter.setPen(Qt.NoPen); painter.setBrush(palette.color(QPalette.Highlight))
        painter.drawRoundedRect(chunk,radius,radius)


def set_progress_bar(
    bar: QProgressBar, current: int = 0, total: int = 0, *,
    detail: str = "", unknown_text: str = "Working…", unit: str = "",
    scope: str = "",
) -> None:
    """Show honest progress without the platform's misleading full busy bar."""
    current=max(0,int(current)); total=max(0,int(total))
    # QProgressBar.setValue may repaint synchronously. Apply one complete
    # state before scheduling paint, including when the job changes phase.
    updates_enabled=bar.updatesEnabled()
    if updates_enabled:
        bar.setUpdatesEnabled(False)
    try:
        bar.setTextVisible(True)
        if total:
            current=min(current,total); percent=round(current*100/total)
            bar.setRange(0,total); bar.setValue(current)
            unit_text=f" {str(unit).strip()}" if str(unit).strip() else ""
            scope_text=f"  •  {str(scope).strip()}" if str(scope).strip() else ""
            bar.setFormat(f"{percent}%  •  {current:,} of {total:,}{unit_text}{scope_text}")
        else:
            # Keep unknown progress empty rather than a misleading full busy bar.
            bar.setRange(0,100); bar.setValue(0); bar.setFormat(unknown_text)
        bar.setToolTip(str(detail or ""))
    finally:
        if updates_enabled:
            bar.setUpdatesEnabled(True)


def confirm_ai_batch(
    parent, count: int, *, auto_merge: bool = False, tagger_count: int = 1,
    minimum_agreement: int = 1,settings=None,model_ids=(),
) -> tuple[bool,bool,bool]:
    """One compact gate for analysis, optional merge, and deliberate reprocessing."""
    dialog=QDialog(parent); dialog.setWindowTitle("Analyze images"); dialog.setMinimumWidth(470)
    layout=QVBoxLayout(dialog)
    title=QLabel(f"Generate AI Tags for {counted_noun(count,'image')} with {counted_noun(tagger_count,'enabled model')}?")
    title.setObjectName("sectionTitle"); title.setWordWrap(True); layout.addWidget(title)
    automatic_minimum=max(2,int(minimum_agreement))
    status=QLabel(
        f"Automatic application needs support from {counted_noun(automatic_minimum,'independent source')}. "
        "Each enabled model counts once; matching pending TXT or Metadata can count once in total. "
        "One model alone is never enough. Current matching model results are skipped."
    )
    status.setWordWrap(True); status.setObjectName("mutedLabel"); layout.addWidget(status)
    if settings is not None and model_ids:
        timing=QLabel(ai_estimate_text(settings,count,model_ids)); timing.setWordWrap(True); timing.setObjectName("mutedLabel"); layout.addWidget(timing)
    merge=QCheckBox("Apply agreed, non-conflicting SDXL Tags automatically")
    merge.setChecked(auto_merge); layout.addWidget(merge)
    repeat=QCheckBox("Re-run unchanged images with matching AI results"); layout.addWidget(repeat)
    buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel)
    buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); layout.addWidget(buttons)
    accepted=dialog.exec()==QDialog.Accepted
    return accepted,repeat.isChecked(),merge.isChecked()


def _duration_text(seconds: float) -> str:
    seconds=max(0.0,float(seconds))
    if seconds<60:
        rounded=max(1,round(seconds)); return f"about {rounded:,} second"+("s" if rounded!=1 else "")
    minutes=seconds/60
    if minutes<60: return f"about {max(1,round(minutes)):,} minutes"
    hours=minutes/60; return f"about {hours:.1f} hours"


def ai_estimate_text(settings,count: int,model_ids) -> str:
    estimate=core.estimate_ai_duration(settings,count,model_ids)
    if not estimate.benchmarked_models:
        reason="Existing benchmark results are stale. " if estimate.stale_models else ""
        return reason+"Time estimate unavailable. Run the hardware benchmark in Setup to measure this configuration."
    text=f"Estimated warm processing time on this computer: {_duration_text(estimate.warm_seconds)}."
    if estimate.first_run_seconds>estimate.warm_seconds+5:
        text+=f" A cold first run may take {_duration_text(estimate.first_run_seconds)}."
    if estimate.missing_models:
        text+=f" {len(estimate.missing_models):,} enabled model(s) are not benchmarked, so actual time will be longer."
    if estimate.stale_models:
        text+=f" {len(estimate.stale_models):,} enabled model benchmark(s) are stale and are excluded from this estimate."
    return text


def ai_estimate_short_text(settings,count: int,model_ids) -> str:
    estimate=core.estimate_ai_duration(settings,count,model_ids)
    if not estimate.benchmarked_models:
        return "AI estimate unavailable • Benchmark again in Setup" if estimate.stale_models else "AI estimate unavailable • Benchmark in Setup"
    text="AI estimate: "+_duration_text(estimate.warm_seconds).removeprefix("about ")
    if estimate.first_run_seconds>estimate.warm_seconds+5: text+=" warm • "+_duration_text(estimate.first_run_seconds).removeprefix("about ")+" first run"
    if estimate.missing_models: text+=f" + {len(estimate.missing_models):,} unbenchmarked model"+("s" if len(estimate.missing_models)!=1 else "")
    if estimate.stale_models: text+=f" + {len(estimate.stale_models):,} stale benchmark"+("s" if len(estimate.stale_models)!=1 else "")
    return text


class EmptyStatePanel(QFrame):
    """Bounded, reusable presentation for a workspace with no current result."""

    def __init__(self,message: str,title: str="",parent=None,*,minimum_height: int=180):
        super().__init__(parent); self.setObjectName("emptyStatePanel")
        self.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Preferred)
        self.setMinimumHeight(max(1,int(minimum_height)))
        layout=QVBoxLayout(self); layout.setContentsMargins(24,20,24,20); layout.setSpacing(6)
        layout.addStretch(1)
        self.title_label=QLabel(title,self); self.title_label.setObjectName("emptyStateTitle")
        self.title_label.setAlignment(Qt.AlignCenter); self.title_label.setVisible(bool(title))
        self.message_label=QLabel(message); self.message_label.setObjectName("mutedLabel")
        self.message_label.setAlignment(Qt.AlignCenter); self.message_label.setWordWrap(True)
        layout.addWidget(self.title_label); layout.addWidget(self.message_label)
        layout.addStretch(1)
        self.setAccessibleName(title or "Empty workspace")
        self.setAccessibleDescription(message)

    def setText(self,message: str):
        self.message_label.setText(message); self.setAccessibleDescription(message)

    def text(self) -> str:
        return self.message_label.text()

    def setTitle(self,title: str):
        self.title_label.setText(title); self.title_label.setVisible(bool(title))
        self.setAccessibleName(title or "Empty workspace")

    def add_action(self,text: str,callback,*,primary: bool=True) -> QPushButton:
        """Put one contextual next action beside the empty-state explanation."""
        button=compact_button(QPushButton(text,self),primary=primary)
        button.clicked.connect(callback)
        self.layout().insertWidget(self.layout().count()-1,button,0,Qt.AlignHCenter)
        return button


class StatusActionPanel(QWidget):
    """A bounded name/state card with a consistently aligned external action rail."""

    def __init__(
        self,title: str,status: str,detail: str,action_text: str,callback,
        parent=None,*,primary: bool=False,action_width: int=260,
        fill_action: bool=False,
    ):
        super().__init__(parent)
        layout=QHBoxLayout(self); layout.setContentsMargins(0,0,0,0); layout.setSpacing(12)
        self.card=QFrame(); self.card.setObjectName("statusActionCard")
        card_layout=QVBoxLayout(self.card); card_layout.setContentsMargins(11,8,11,8); card_layout.setSpacing(4)
        heading=QHBoxLayout(); heading.setSpacing(10)
        self.title_label=QLabel(title); self.title_label.setObjectName("statusCardTitle")
        self.status_label=QLabel(status); self.status_label.setObjectName("statusCardStatus")
        self.status_label.setAlignment(Qt.AlignRight|Qt.AlignVCenter); self.status_label.setMinimumWidth(150)
        heading.addWidget(self.title_label); heading.addStretch(1); heading.addWidget(self.status_label,0,Qt.AlignRight|Qt.AlignVCenter)
        self.detail_label=QLabel(detail); self.detail_label.setObjectName("mutedLabel"); self.detail_label.setWordWrap(True)
        card_layout.addLayout(heading); card_layout.addWidget(self.detail_label)

        self.action_panel=QWidget(); self.action_panel.setObjectName("statusActionRail")
        action_width=max(1,int(action_width))
        if fill_action:
            self.action_panel.setFixedWidth(action_width)
        else:
            self.action_panel.setMinimumWidth(action_width)
        action_layout=QVBoxLayout(self.action_panel); action_layout.setContentsMargins(0,0,0,0)
        self.action_button=QPushButton(action_text)
        if fill_action:
            self.action_button.setProperty("primary",primary)
            self.action_button.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Expanding)
        else:
            action_layout.addStretch(1)
            compact_button(self.action_button,primary=primary)
        if callback is not None: self.action_button.clicked.connect(callback)
        if fill_action:
            action_layout.addWidget(self.action_button)
        else:
            action_layout.addWidget(self.action_button,0,Qt.AlignRight)
            action_layout.addStretch(1)
        layout.addWidget(self.card,1); layout.addWidget(self.action_panel)


class FooterActivityRow(QFrame):
    """Shared status, progress, action order with a narrow-window fallback."""

    def __init__(self,parent=None):
        super().__init__(parent)
        self.setObjectName("footerActivityRow")
        self.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Maximum)
        self.row=QBoxLayout(QBoxLayout.LeftToRight,self)
        self.row.setContentsMargins(0,0,0,0); self.row.setSpacing(8)
        self.text_layout=QHBoxLayout(); self.text_layout.setSpacing(6)
        self.controls=QHBoxLayout(); self.controls.setSpacing(8)
        self.progress_layout=QVBoxLayout(); self.progress_layout.setSpacing(4)
        self.indicators=[]
        self.button_layout=QHBoxLayout(); self.button_layout.setSpacing(6)
        self.controls.addStretch(1)
        self.controls.addLayout(self.progress_layout)
        self.controls.addLayout(self.button_layout)
        self.row.addLayout(self.text_layout,1); self.row.addLayout(self.controls)

    def add_feedback(self,widget):
        if isinstance(widget,QLabel):
            widget.setMinimumWidth(0); widget.setWordWrap(True)
            self.text_layout.addWidget(widget,1)
        elif isinstance(widget,(QProgressBar,IndeterminateActivityBar)):
            # Measured and unknown-duration indicators occupy the same column.
            widget.setFixedWidth(390)
            widget.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Fixed)
            self.progress_layout.addWidget(widget)
            self.indicators.append(widget)
        elif isinstance(widget,QPushButton):
            compact_button(widget)
            self.button_layout.addWidget(widget)
        else:
            self.controls.addWidget(widget,1)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        self._arrange()

    def event(self,event):
        result=super().event(event)
        if event.type()==QEvent.LayoutRequest and hasattr(self,"controls"):
            self._arrange()
        return result

    def _arrange(self):
        # Buttons live in a separate footer action host after attachment. That
        # host can die before this row receives its queued deletion.
        if not isValid(self.button_layout):return
        width=min(390,max(100,self.width()-self.button_layout.sizeHint().width()-16))
        for widget in self.indicators:
            if widget.width()!=width:widget.setFixedWidth(width)
        # Reserve enough text room before moving the controls to a second line.
        required=self.controls.sizeHint().width()+min(280,self.text_layout.sizeHint().width())+8
        direction=QBoxLayout.TopToBottom if self.width()<required else QBoxLayout.LeftToRight
        if self.row.direction()!=direction:self.row.setDirection(direction)


class StatusFooter(QFrame):
    """One window-owned home for operational feedback and activity controls."""

    notification_changed=Signal(str)

    def __init__(self,parent=None):
        super().__init__(parent)
        self.setObjectName("statusFooter")
        self.setAccessibleName("Window status and activity")
        self.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Maximum)
        self.rows=QVBoxLayout(self)
        self.rows.setContentsMargins(10,5,10,5); self.rows.setSpacing(4)
        self._bindings={}; self._global_activity=None; self._status_revision=0
        self._notice_has_extra_detail=False
        self._refresh_timer=QTimer(self); self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._refresh_activity)
        self.activity_slot=QVBoxLayout(); self.activity_slot.setSpacing(0)
        self.rows.addLayout(self.activity_slot)
        self.notice=ElidingLabel()
        self.notice.setObjectName("footerNotification")
        self.notice.setTextFormat(Qt.PlainText); self.notice.setMinimumWidth(120)
        self.notice.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
        self.notice.hide()
        self.notice.setAccessibleName("Status notification")
        self.notice.installEventFilter(self)
        self.notice_row=QHBoxLayout(); self.notice_row.setSpacing(8)
        self.notice_row.addWidget(self.notice,1)
        self.context_summary=ElidingLabel()
        self.context_summary.setMinimumWidth(0)
        self.context_summary.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
        self.context_summary.setAccessibleName("Additional window status")
        self.context_summary.hide(); self.notice_row.addWidget(self.context_summary,1)
        self.actions=QHBoxLayout(); self.actions.addStretch(1)
        self.actions.setSpacing(6)
        self.notice_row.addLayout(self.actions)
        self.rows.addLayout(self.notice_row)
        self.details=QPlainTextEdit(); self.details.setReadOnly(True)
        self.details.setMaximumHeight(140); self.details.hide()
        self.details.setAccessibleName("Full status notification")
        self.rows.addWidget(self.details)
        self.details_button=compact_button(QPushButton("More Status"))
        self.details_button.setCheckable(True); self.details_button.hide()
        self.details_button.setToolTip("Show the full status message and related activity details.")
        self.details_button.toggled.connect(self._show_details)
        self.actions.addWidget(self.details_button)
        self.context=QScrollArea(); self.context.setWidgetResizable(True)
        self.context.setFrameShape(QFrame.NoFrame); self.context.setMaximumHeight(160)
        self.context.setAccessibleName("Additional activity details")
        self.context_body=QWidget(); self.context_rows=QVBoxLayout(self.context_body)
        self.context_rows.setContentsMargins(0,0,0,0); self.context_rows.setSpacing(4)
        self.context.setWidget(self.context_body); self.context.hide()
        self.rows.addWidget(self.context)

    def _show_details(self,visible):
        self.details.setVisible(visible and bool(self.details.toPlainText()))
        self.context.setVisible(visible and any(
            value[0] and self.context_rows.indexOf(binding.row)>=0
            for binding,value in self._bindings.items()
        ))
        self.details_button.setText("Less Status" if visible else "More Status")

    def eventFilter(self,watched,event):
        # Children toggled while the activity strip is hidden emit
        # ShowToParent/HideToParent, not an effective Show/Hide event. Observe
        # those transitions so the footer can reveal the strip immediately.
        if event.type() in (QEvent.Show,QEvent.Hide,QEvent.ShowToParent,
                           QEvent.HideToParent,QEvent.LayoutRequest) or (
            watched is self.notice and event.type()==QEvent.Resize
        ):
            self._schedule_refresh()
        return False

    def _schedule_refresh(self):
        if isValid(self._refresh_timer) and not self._refresh_timer.isActive():
            self._refresh_timer.start(0)

    def _notice_needs_details(self):
        if not self.details.toPlainText():
            return False
        if self._notice_has_extra_detail:
            return True
        width=self.notice.contentsRect().width()
        if self.details_button.isVisible():
            width+=self.details_button.width()+self.actions.spacing()
        return self.notice.fontMetrics().horizontalAdvance(
            self.notice.completeText()
        )>width

    def set_global_activity(self,strip):
        self._global_activity=strip
        strip.controls.removeItem(strip.button_layout)
        strip.button_layout.setParent(None)
        self.actions.addLayout(strip.button_layout)
        self.activity_slot.addWidget(strip.overall_row)
        self.activity_slot.addWidget(strip)
        self.activity_slot.addWidget(strip.secondary_row)
        for widget in (strip,strip.progress,strip.activity_label):widget.installEventFilter(self)
        self._schedule_refresh()

    def update_binding(self,binding,meaningful,active):
        if binding not in self._bindings:
            # Keep original controls/signals. Only their presentation is shared.
            buttons=binding.row.button_layout
            binding.row.controls.removeItem(buttons); buttons.setParent(None)
            binding.action_host=QWidget(self); binding.action_host.setLayout(buttons)
            buttons.setContentsMargins(0,0,0,0)
            self.actions.insertWidget(1,binding.action_host)
            binding.destroyed.connect(self._prune_bindings)
            binding.owner.destroyed.connect(binding.action_host.deleteLater)
        # Visibility/layout changes are not new messages. Otherwise opening
        # Details can promote an older instruction above a newer error.
        fingerprint=tuple((widget.text(),widget.property("footerSummary"))
                          for widget in binding.widgets if isinstance(widget,QLabel))
        if fingerprint!=getattr(binding,"_status_fingerprint",None):
            binding._status_fingerprint=fingerprint
            self._status_revision+=1; binding._status_revision=self._status_revision
        self._bindings[binding]=(meaningful,active)
        binding.action_host.setVisible(meaningful and any(
            not widget.isHidden() and isinstance(widget,QPushButton)
            for widget in binding.widgets
        ))
        self._schedule_refresh()

    def _remove_binding(self,binding):
        self._bindings.pop(binding,None)
        self._schedule_refresh()

    def _prune_bindings(self):
        self._bindings={binding:value for binding,value in self._bindings.items() if isValid(binding)}
        self._schedule_refresh()

    def _refresh_activity(self):
        self._bindings={binding:value for binding,value in self._bindings.items() if isValid(binding)}
        eligible=[binding for binding,(visible,_) in self._bindings.items() if visible]
        active=[binding for binding in eligible if any(
            not widget.isHidden() and isinstance(widget,(QProgressBar,IndeterminateActivityBar))
            for widget in binding.widgets
        )]
        strip=self._global_activity
        global_active=strip is not None and not strip.progress.isHidden()
        global_pending=strip is not None and not strip.overall_row.isHidden()
        selected=None if global_active or global_pending else next(iter(reversed(active)),None)
        if strip is not None:strip.setVisible(global_active)
        for binding,(visible,_) in self._bindings.items():
            decision_controls=any(not isinstance(widget,(QLabel,QProgressBar,IndeterminateActivityBar,QPushButton))
                                  for widget in binding.widgets)
            target=self.rows if decision_controls else self.activity_slot if binding is selected else self.context_rows
            if target.indexOf(binding.row)<0:
                if decision_controls:self.add_status_widget(binding.row)
                else:target.addWidget(binding.row)
            binding.row.setVisible(visible)
        summaries=[]; details=[]
        for binding in sorted(eligible,key=lambda binding:binding._status_revision):
            labels=[widget for widget in binding.widgets
                    if isinstance(widget,QLabel) and not widget.isHidden() and widget.text()]
            if binding is selected or not labels:continue
            details.append(" • ".join(filter(None,(
                binding.title," · ".join(widget.text() for widget in labels),
            ))))
            summaries.append(" • ".join(filter(None,(
                binding.title," · ".join(
                    str(widget.property("footerSummary") or widget.text())
                    for widget in labels
                ),
            ))))
        self.context_summary.setText(" ".join(summaries[-1].split()) if summaries else "")
        self.context_summary.setToolTip("\n".join(details))
        self.context_summary.setAccessibleDescription("\n".join(details))
        self.context_summary.setVisible(bool(summaries) and not bool(self.details.toPlainText()))
        notice_needs_details=self._notice_needs_details()
        available=notice_needs_details or any(
            self.context_rows.indexOf(binding.row)>=0 for binding in eligible
        )
        self.details_button.setVisible(available)
        if not available:self.details_button.setChecked(False)
        self._show_details(self.details_button.isChecked())

    def add_status_widget(self,widget):
        self.rows.insertWidget(self.rows.indexOf(self.details),widget)

    def adopt_window_actions(self,root):
        """Keep a dialog's existing bottom actions inside its status area."""
        if getattr(self,"_actions_adopted",False) or not isinstance(root,QVBoxLayout):return
        self._actions_adopted=True
        bottom=next((root.itemAt(index) for index in reversed(range(root.count()))
                     if root.itemAt(index).widget() is not self),None)
        action_layout=bottom.layout() if bottom is not None else None
        action_widget=bottom.widget() if bottom is not None else None
        if isinstance(action_layout,QHBoxLayout) and any(
            isinstance(action_layout.itemAt(index).widget(),QDialogButtonBox)
            for index in range(action_layout.count())
        ):
            # Keep standard buttons parented to the dialog: moving their box
            # can replace Qt's child buttons and invalidate callers' handles.
            # Put status alongside the existing actions in the same bottom row.
            root.removeWidget(self)
            for index in reversed(range(action_layout.count())):
                if action_layout.itemAt(index).spacerItem() is not None:action_layout.takeAt(index)
            action_layout.insertWidget(0,self,1)
            return
        if isinstance(action_widget,QDialogButtonBox):
            visible=not action_widget.isHidden()
            root.removeWidget(action_widget); self.actions.addWidget(action_widget)
            action_widget.setVisible(visible)
        elif isinstance(action_layout,QHBoxLayout) and any(
            isinstance(action_layout.itemAt(index).widget(),(QPushButton,QDialogButtonBox))
            for index in range(action_layout.count())
        ):
            visible_widgets=[(item.widget(),not item.widget().isHidden())
                             for index in range(action_layout.count())
                             if (item:=action_layout.itemAt(index)).widget() is not None]
            root.removeItem(action_layout); action_layout.setParent(None)
            for index in reversed(range(action_layout.count())):
                if action_layout.itemAt(index).spacerItem() is not None:action_layout.takeAt(index)
            self.actions.addLayout(action_layout)
            for widget,visible in visible_widgets:widget.setVisible(visible)
        # A notice can create a footer before the rest of a dialog is built.
        root.removeWidget(self); root.addWidget(self)

    def show_message(self,text,*,severity="info",summary=None):
        text=str(text or "")
        visible=(str(summary).strip() if summary is not None else "") or next(
            (line.strip() for line in text.splitlines() if line.strip()),""
        )
        visible=" ".join(visible.split())
        self._notice_has_extra_detail=bool(text) and visible!=" ".join(text.split())
        self.notice.ensurePolished()
        self.notice.setMinimumWidth(max(120,self.notice.fontMetrics().horizontalAdvance("Attention: …")+16))
        self.notice.setText(
            ("Error: " if severity=="error" else "Attention: " if severity=="warning" else "")
            +visible
        )
        self.notice.setVisible(bool(text))
        self.notice.setToolTip(text)
        self.notice.setAccessibleDescription(text)
        self.details.setPlainText(text)
        self.details_button.setChecked(False)
        # Full text stays keyboard-accessible even when resizing elides a short notice.
        self.details_button.setVisible(self._notice_needs_details())
        self.context_summary.setVisible(not bool(text) and bool(self.context_summary.completeText()))
        self._schedule_refresh()
        self.notification_changed.emit(text)


class WindowStatusBar(QStatusBar):
    """Keep Qt's transient notifications inside the same full-width footer."""

    def __init__(self,parent=None):
        super().__init__(parent)
        self.footer=StatusFooter(self)
        self.addPermanentWidget(self.footer,1)
        self._message=""
        self._notice_timer=QTimer(self); self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self.clearMessage)
        self.footer.notification_changed.connect(self._footer_notice_changed)

    def _footer_notice_changed(self,message):
        self._notice_timer.stop(); self._message=str(message or "")
        self.messageChanged.emit(self._message)

    def showMessage(self,message,timeout=0,*,summary=None):
        self.footer.show_message(str(message or ""),summary=summary)
        if timeout>0:self._notice_timer.start(timeout)

    def clearMessage(self):
        self.showMessage("")

    def currentMessage(self):
        return self._message


def window_status_footer(window):
    """Resolve a footer on the actual window, including standalone dialogs."""
    if isinstance(window,QMainWindow):
        status=window.statusBar()
        if isinstance(status,WindowStatusBar):return status.footer
    footer=getattr(window,"_window_status_footer",None)
    if footer is None:
        footer=StatusFooter(window); window._window_status_footer=footer
        root=window.layout()
        if root is None:root=QVBoxLayout(window)
        root.addWidget(footer)
        footer.show()
    if isinstance(window,QDialog) and window.isVisible():
        footer.adopt_window_actions(window.layout())
    return footer


class FooterProgressDialog(QDialog):
    """Small progress window using the same footer and Qt progress API."""

    canceled=Signal()

    def __init__(self,label,cancel_text,minimum,maximum,parent=None):
        super().__init__(parent)
        self.setMinimumWidth(560)
        self._cancelled=False; self._complete=False
        self._minimum_duration=0
        self._auto_close=True
        root=QVBoxLayout(self); root.setContentsMargins(0,0,0,0)
        footer=window_status_footer(self)
        self.activity_row=FooterActivityRow()
        self.label=QLabel(str(label)); self.activity_row.add_feedback(self.label)
        self.progress=QProgressBar(); self.progress.setRange(minimum,maximum)
        self.activity_row.add_feedback(self.progress)
        footer.activity_slot.addWidget(self.activity_row)
        self.cancel_button=compact_button(QPushButton(str(cancel_text)))
        self.cancel_button.setVisible(bool(cancel_text))
        self.cancel_button.clicked.connect(self.cancel)
        self.activity_row.add_feedback(self.cancel_button)
        self.activity_row.controls.removeItem(self.activity_row.button_layout)
        self.activity_row.button_layout.setParent(None)
        footer.actions.addLayout(self.activity_row.button_layout)

    def setLabelText(self,text):self.label.setText(str(text))
    def setMinimumDuration(self,value):
        self._minimum_duration=max(0,int(value))
        if self._minimum_duration==0:self.show()
    def minimumDuration(self):return self._minimum_duration
    def setCancelButtonText(self,text):
        self.cancel_button.setText(str(text)); self.cancel_button.setVisible(bool(text))
    def setCancelButton(self,button):
        self.cancel_button.hide()
        if button is not None:
            self.cancel_button=button
            self.activity_row.add_feedback(button)
            button.clicked.connect(self.cancel); button.show()
    def setMaximum(self,value):self.progress.setMaximum(value)
    def setMinimum(self,value):self.progress.setMinimum(value)
    def setRange(self,minimum,maximum):self.progress.setRange(minimum,maximum)
    def setValue(self,value):
        self.progress.setValue(value)
        if self.progress.maximum()>self.progress.minimum() and value>=self.progress.maximum():
            self._complete=True
            if self._auto_close:self.close()
    def value(self):return self.progress.value()
    def wasCanceled(self):return self._cancelled
    def cancel(self):
        if not self._cancelled:
            self._cancelled=True; self.canceled.emit()
        self.hide()
    def reject(self):self.cancel()
    def closeEvent(self,event):
        if not self._complete:self.cancel()
        event.accept()


class _FooterBinding(QObject):
    """Move existing controls, preserving their signals and show/hide state."""

    def __init__(self,owner,widgets,title,follow_visibility):
        super().__init__(owner)
        self.owner=owner; self.widgets=tuple(widgets)
        self.title=str(title); self.follow_visibility=follow_visibility
        self.row=None; self.footer=None
        self.timer=QTimer(self); self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._attach)
        owner.installEventFilter(self)
        self.timer.start(0)

    def eventFilter(self,watched,event):
        if (watched is getattr(self,"owner",None) and event.type()==QEvent.ParentAboutToChange
                and self.footer is not None and self.row is not None):
            # The old window can be destroyed before the zero-delay attach.
            # Keep feedback and cancellation owned by the moving panel meanwhile.
            self._detach_from_footer()
        if event.type() in (
            QEvent.Show,QEvent.Hide,QEvent.ShowToParent,QEvent.HideToParent,
            QEvent.ParentChange,QEvent.LayoutRequest,
        ):
            timer=getattr(self,"timer",None)
            if timer is not None:
                try:
                    if not timer.isActive():timer.start(0)
                except RuntimeError:
                    # Qt may deliver a final layout event while its owner and
                    # child timer are being destroyed.
                    pass
        return False

    def _detach_from_footer(self):
        old_footer=self.footer
        old_footer._remove_binding(self)
        old_footer.actions.removeWidget(self.action_host)
        self.row.hide()
        self.row.setParent(self.owner)
        self.row.button_layout=QHBoxLayout()
        self.row.controls.addLayout(self.row.button_layout)
        for widget in self.widgets:
            if isinstance(widget,QPushButton):
                visible=not widget.isHidden()
                self.row.button_layout.addWidget(widget); widget.setVisible(visible)
        self.action_host.deleteLater()
        self.footer=None

    def _attach(self):
        # Construction often precedes insertion into the main stack or editor.
        # Resolve at show, never turn an embedded page into a second window.
        if self.row is None and not self.owner.isVisible():return
        window=self.owner.window()
        if self.footer is None or self.footer.window() is not window:
            old_footer=self.footer
            if old_footer is not None:
                self._detach_from_footer()
            self.footer=window_status_footer(window)
            if self.row is None:
                self.row=FooterActivityRow(self.owner)
                if self.title:
                    heading=QLabel(self.title); heading.setObjectName("footerActivityTitle")
                    self.row.text_layout.addWidget(heading)
                for widget in self.widgets:
                    visible=not widget.isHidden()
                    widget.setParent(self.row)
                    self.row.add_feedback(widget)
                    widget.setVisible(visible); widget.installEventFilter(self)
                self.row.installEventFilter(self)
                self.owner.destroyed.connect(self.row.deleteLater)
        meaningful=any(
            not widget.isHidden()
            and (not isinstance(widget,QLabel) or bool(widget.text()))
            for widget in self.widgets
        )
        active=any(
            not widget.isHidden() and (
                isinstance(widget,QProgressBar)
                or isinstance(widget,IndeterminateActivityBar) and widget.active
                or isinstance(widget,QPushButton) and widget.text().startswith(("Cancel","Stop"))
            ) for widget in self.widgets
        )
        eligible=meaningful and (
            self.owner.isVisible() or active and not self.follow_visibility
        )
        self.footer.update_binding(self,eligible,active)


def bind_status_footer(owner,*widgets,title="",follow_visibility=False):
    """Register one operation's existing feedback controls with its window."""
    binding=_FooterBinding(owner,widgets,title,follow_visibility)
    bindings=getattr(owner,"_status_footer_bindings",None)
    if bindings is None:bindings=[]; owner._status_footer_bindings=bindings
    bindings.append(binding)
    return binding


def refresh_status_footer(owner):
    """Refresh a bound footer after text changes that do not relayout a label."""
    for binding in getattr(owner, "_status_footer_bindings", ()):
        binding._attach()


class GlobalStatusStrip(FooterActivityRow):
    """Keep shared-shell context, activity, progress, and cancellation aligned."""

    def __init__(self,parent=None):
        super().__init__(parent); self.setObjectName("globalStatusStrip")
        self.overall_row=FooterActivityRow(self)
        self.overall_label=ElidingLabel("Overall work")
        self.overall_label.setMinimumWidth(120)
        self.overall_row.text_layout.addWidget(self.overall_label,1)
        self.overall_progress=QProgressBar()
        self.overall_progress.setAccessibleName("Overall work progress by task")
        self.overall_row.add_feedback(self.overall_progress)
        self.overall_row.hide()
        self.secondary_row=FooterActivityRow(self)
        self.secondary_label=ElidingLabel(); self.secondary_label.setMinimumWidth(120)
        self.secondary_row.text_layout.addWidget(self.secondary_label,1)
        self.secondary_progress=QProgressBar()
        self.secondary_progress.setAccessibleName("Concurrent task progress")
        self.secondary_row.add_feedback(self.secondary_progress)
        self.secondary_row.hide()
        self.ingest_attention_button=compact_button(QPushButton())
        self.ingest_attention_button.hide()
        self.library_scope_button=compact_button(QPushButton())
        self.activity_label=ElidingLabel(); self.activity_label.setObjectName("activityLabel")
        self.activity_label.setMinimumWidth(120)
        self.activity_label.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Preferred)
        self.activity_label.hide()
        self.progress=QProgressBar(); self.progress.hide()
        self.cancel_button=compact_button(QPushButton("Cancel Safely")); self.cancel_button.hide()
        self.text_layout.addWidget(self.activity_label,1)
        self.add_feedback(self.progress)
        for widget in (self.ingest_attention_button,self.library_scope_button,self.cancel_button):
            self.button_layout.addWidget(widget)


class ContextHelpDialog(QDialog):
    """One read-only guide with direct topic and literal-text navigation."""

    def __init__(self,title: str,help_text: str,parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{title} Help")
        self.setModal(False)
        self.setAttribute(Qt.WA_DeleteOnClose,True)
        self.resize(640,520)
        layout=QVBoxLayout(self); layout.setContentsMargins(20,18,20,18); layout.setSpacing(12)
        heading=QLabel(title); heading.setObjectName("pageTitle"); layout.addWidget(heading)
        self.guide=QTextBrowser(); self.guide.setOpenExternalLinks(False)
        self.guide.setMarkdown(str(help_text or "")); self.guide.setAccessibleName(f"{title} help")
        topic_row=QHBoxLayout()
        topic_label=QLabel("Topic")
        self.topics=QComboBox(); self.topics.setAccessibleName("Help topic")
        topic_label.setBuddy(self.topics)
        topic_row.addWidget(topic_label); topic_row.addWidget(self.topics,1)
        block=self.guide.document().begin()
        while block.isValid():
            if block.blockFormat().headingLevel()>0:
                self.topics.addItem(block.text(),block.position())
            block=block.next()
        topic_label.setVisible(self.topics.count()>1)
        self.topics.setVisible(self.topics.count()>1)
        self.topics.currentIndexChanged.connect(self._go_to_topic)
        layout.addLayout(topic_row)
        search_row=QHBoxLayout()
        self.find_text=QLineEdit(); self.find_text.setPlaceholderText("Find in this guide (Ctrl+F)")
        self.find_text.setAccessibleName("Find text in this help guide")
        self.find_previous=compact_button(QPushButton("Previous"))
        self.find_next=compact_button(QPushButton("Next"))
        self.find_previous.setAutoDefault(False); self.find_next.setAutoDefault(False)
        self.find_previous.setAccessibleName("Previous help match")
        self.find_next.setAccessibleName("Next help match")
        self.find_previous.clicked.connect(lambda:self._move_match(-1))
        self.find_next.clicked.connect(lambda:self._move_match(1))
        search_row.addWidget(self.find_text,1)
        search_row.addWidget(self.find_previous); search_row.addWidget(self.find_next)
        layout.addLayout(search_row)
        self.find_status=QLabel(); self.find_status.setAccessibleName("Help search result")
        self.find_status.setObjectName("mutedLabel"); layout.addWidget(self.find_status)
        self._matches=[]; self._match_index=-1
        self.find_text.textChanged.connect(self._find_in_guide)
        self.find_text.returnPressed.connect(lambda:self._move_match(1))
        self._find_shortcut=QShortcut(QKeySequence.Find,self)
        self._find_shortcut.activated.connect(self._focus_find)
        self._find_in_guide("")
        layout.addWidget(self.guide,1)
        buttons=QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setAutoDefault(False)
        buttons.rejected.connect(self.close); layout.addWidget(buttons)

    def _focus_find(self):
        self.find_text.setFocus(); self.find_text.selectAll()

    def _go_to_topic(self,index):
        position=self.topics.itemData(index)
        if position is None:return
        cursor=QTextCursor(self.guide.document()); cursor.setPosition(position)
        self.guide.setTextCursor(cursor); self.guide.ensureCursorVisible()
        scroll=self.guide.verticalScrollBar()
        scroll.setValue(scroll.value()+self.guide.cursorRect().top())

    def _find_in_guide(self,text):
        self._matches=[]; self._match_index=-1
        if text:
            cursor=QTextCursor(self.guide.document())
            while True:
                cursor=self.guide.document().find(text,cursor)
                if cursor.isNull():break
                self._matches.append((cursor.selectionStart(),cursor.selectionEnd()))
        available=bool(self._matches)
        self.find_previous.setEnabled(available); self.find_next.setEnabled(available)
        self.find_status.setVisible(bool(text))
        if available:self._move_match(1)
        else:
            cursor=self.guide.textCursor(); cursor.clearSelection(); self.guide.setTextCursor(cursor)
            self.find_status.setText("No matches in this guide." if text else "")

    def _move_match(self,direction):
        if not self._matches:return
        self._match_index=(self._match_index+direction)%len(self._matches)
        start,end=self._matches[self._match_index]
        cursor=QTextCursor(self.guide.document()); cursor.setPosition(start)
        cursor.setPosition(end,QTextCursor.KeepAnchor)
        self.guide.setTextCursor(cursor); self.guide.ensureCursorVisible()
        self.find_status.setText(f"Match {self._match_index+1} of {len(self._matches)}")


def _show_context_help(owner,title: str,help_text: str):
    """Reuse one guide per entry point, including menu-only page guidance."""
    if owner._help_dialog is not None and owner._help_dialog.isVisible():
        owner._help_dialog.raise_(); owner._help_dialog.activateWindow()
        return owner._help_dialog
    dialog=ContextHelpDialog(title,help_text,owner.window())
    owner._help_dialog=dialog

    def clear_dialog():
        if owner._help_dialog is dialog:
            owner._help_dialog=None

    dialog.destroyed.connect(clear_dialog)
    dialog.show()
    return dialog


class ContextHelpButton(QPushButton):
    """Visible, consistent access to guidance for a page or section."""

    def __init__(self,title: str,help_text: str,parent=None):
        super().__init__("Help",parent)
        self.help_title=str(title); self.help_text=str(help_text or "")
        self._help_dialog=None
        compact_button(self)
        self.setAccessibleName(f"Help for {self.help_title}")
        self.setAccessibleDescription(f"Open the guide for {self.help_title}.")
        self.setToolTip(f"Open the guide for {self.help_title}.")
        self.clicked.connect(self.show_help)

    def show_help(self,_checked=False):
        return _show_context_help(self,self.help_title,self.help_text)


def ui_setting_bool(settings,key: str,default: bool=True) -> bool:
    """Read a bool from optional QSettings without treating "false" as truthy."""
    if settings is None:
        return bool(default)
    value=settings.value(key,default)
    if isinstance(value,bool):
        return value
    normalized=str(value).strip().casefold()
    if normalized in {"1","true","yes","on"}:
        return True
    if normalized in {"0","false","no","off"}:
        return False
    return bool(default)


class PageHeader(QWidget):
    """Consistent page heading with context, status, and page-wide actions.

    Detailed guidance is available through F1 and the application Help menu,
    leaving page-wide actions free of duplicate Help buttons.
    """

    def __init__(self,title: str,subtitle: str="",help_text: str="",parent=None):
        super().__init__(parent); self.help_text=help_text; self._help_dialog=None
        layout=QVBoxLayout(self); layout.setContentsMargins(0,0,0,0); layout.setSpacing(4)
        row=QHBoxLayout(); self.title=QLabel(title); self.title.setObjectName("pageTitle")
        self.status=QLabel(); self.status.setObjectName("mutedLabel"); self.status.hide()
        self._layout=layout; self._header_row=row; self._compact_mode=False
        self.actions=QHBoxLayout(); self.actions.setSpacing(8)
        row.addWidget(self.title); row.addStretch(1); row.addWidget(self.status); row.addLayout(self.actions)
        layout.addLayout(row)
        self.subtitle=QLabel(subtitle,self); self.subtitle.setWordWrap(True); self.subtitle.setObjectName("mutedLabel")
        self.subtitle.setVisible(bool(subtitle)); layout.addWidget(self.subtitle)
        self._status_footer_binding=bind_status_footer(
            self,self.status,title=title,follow_visibility=True,
        )

    def add_action(self,text: str,callback,*,primary: bool=False):
        button=compact_button(QPushButton(text),primary=primary); button.clicked.connect(callback)
        button.setMinimumWidth(button.fontMetrics().horizontalAdvance(text)+24)
        self.actions.addWidget(button)
        return button

    def set_status(self,text: str):
        self.status.setText(text); self.status.setVisible(bool(text))
        self._status_footer_binding._attach()

    def set_context(self,text: str):
        self.subtitle.setText(text); self.subtitle.setVisible(bool(text))

    def set_title_visible(self,visible: bool) -> None:
        """Let shell navigation own the title without hiding context or actions."""
        self.title.setVisible(visible)
        self._layout.setSpacing(4 if visible else 0)
        self.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Preferred if visible else QSizePolicy.Maximum)
        self.updateGeometry()

    def set_compact_mode(self,compact: bool) -> None:
        """Keep header actions compact; operational feedback lives in the footer."""
        compact=bool(compact)
        if compact==self._compact_mode:
            return
        self._compact_mode=compact
        self.updateGeometry()

    def show_help(self):
        if self.help_text:
            return _show_context_help(self,self.title.text(),self.help_text)
        return None


class DisclosureButton(QPushButton):
    """One shared on-demand control for optional or specialist UI sections."""

    def __init__(self,show_text: str,hide_text: str,target: QWidget,parent=None):
        super().__init__(show_text,parent); self.show_text=show_text; self.hide_text=hide_text; self.target=target
        compact_button(self); self.setCheckable(True); self.target.hide(); self.toggled.connect(self._apply)

    def _apply(self,checked: bool):
        self.target.setVisible(checked); self.setText(self.hide_text if checked else self.show_text)
