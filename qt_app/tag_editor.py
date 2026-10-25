from __future__ import annotations

import csv
import io
import json
import weakref
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QMimeData,QPoint,QRect,QSize,QTimer,Qt,Signal
from PySide6.QtGui import QAccessible,QAccessibleEvent,QColor,QDrag,QPalette
from PySide6.QtWidgets import (
    QApplication,QComboBox,QFrame,QHBoxLayout,QLabel,QLayout,QLayoutItem,QLineEdit,QMenu,QPushButton,QScrollArea,
    QSizePolicy,QVBoxLayout,QWidget,
)

from core.content_sensitivity import effective_tag_sensitivity
from core.paths import USER_DATA_DIR
from core.tag_appearance import (
    TagAppearanceProfileStore,TagAppearanceResolver,
)
from core.tag_collection import normalize_tag
from core.tag_palette_index import PreparedTagPaletteIndex,prepare_tag_palette_index


_ORDER_GROUP_RANK={name:index for index,name in enumerate(("rating","image_style","subject_count","identity","subject_traits","outfit","action","scene","other"))}
TAG_MIME="application/x-collectigen-sdxl-tag"
TAG_IDS_MIME="application/x-collectigen-sdxl-tag-ids"
TAG_VALUES_MIME="application/x-collectigen-sdxl-tag-values"

_APPEARANCE_CHIPS=weakref.WeakSet()
_DEFAULT_APPEARANCE_PATH=None
_DEFAULT_APPEARANCE_STORE=None
_DEFAULT_APPEARANCE_RESOLVER=None
_DEFAULT_APPEARANCE_PINNED=False
_PLACEHOLDER_MEANING_NOTES=frozenset((
    "",
    "Community-confirmed reusable vocabulary.",
    "Community-confirmed reusable character identity.",
))


def default_tag_appearance_store() -> TagAppearanceProfileStore:
    global _DEFAULT_APPEARANCE_PATH,_DEFAULT_APPEARANCE_STORE,_DEFAULT_APPEARANCE_RESOLVER
    path=_DEFAULT_APPEARANCE_PATH if _DEFAULT_APPEARANCE_PINNED else USER_DATA_DIR/"tag_appearance.json"
    if _DEFAULT_APPEARANCE_STORE is None or _DEFAULT_APPEARANCE_PATH!=path:
        _DEFAULT_APPEARANCE_PATH=path
        _DEFAULT_APPEARANCE_STORE=TagAppearanceProfileStore(path)
        _DEFAULT_APPEARANCE_RESOLVER=TagAppearanceResolver(_DEFAULT_APPEARANCE_STORE)
    return _DEFAULT_APPEARANCE_STORE


def default_tag_appearance_resolver() -> TagAppearanceResolver:
    default_tag_appearance_store()
    return _DEFAULT_APPEARANCE_RESOLVER


def refresh_all_tag_appearance_chips(*,reload_profile=False):
    store=default_tag_appearance_store()
    if reload_profile:store.load()
    default_tag_appearance_resolver().clear()
    for chip in tuple(_APPEARANCE_CHIPS):
        try:chip.refresh_appearance()
        except RuntimeError:_APPEARANCE_CHIPS.discard(chip)


def reset_default_tag_appearance_service(path=None):
    """Test/support hook for switching the user-owned profile location."""
    global _DEFAULT_APPEARANCE_PATH,_DEFAULT_APPEARANCE_STORE,_DEFAULT_APPEARANCE_RESOLVER,_DEFAULT_APPEARANCE_PINNED
    _DEFAULT_APPEARANCE_PINNED=path is not None
    _DEFAULT_APPEARANCE_PATH=Path(path) if path is not None else None
    _DEFAULT_APPEARANCE_STORE=(
        TagAppearanceProfileStore(_DEFAULT_APPEARANCE_PATH)
        if _DEFAULT_APPEARANCE_PATH is not None else None
    )
    _DEFAULT_APPEARANCE_RESOLVER=(
        TagAppearanceResolver(_DEFAULT_APPEARANCE_STORE)
        if _DEFAULT_APPEARANCE_STORE is not None else None
    )


def decode_tag_drag_ids(mime_data,knowledge) -> tuple[str, ...]:
    """Resolve current and legacy tag-chip drag payloads to stable IDs."""
    if mime_data.hasFormat(TAG_IDS_MIME):
        try:values=json.loads(bytes(mime_data.data(TAG_IDS_MIME)).decode("utf-8"))
        except (UnicodeDecodeError,json.JSONDecodeError,TypeError):values=[]
        if isinstance(values,list):
            result=[]
            for value in values:
                stable_id=str(value).strip()
                if stable_id and stable_id not in result:result.append(stable_id)
            if result:return tuple(result)
    if mime_data.hasFormat(TAG_MIME):
        try:value=bytes(mime_data.data(TAG_MIME)).decode("utf-8")
        except UnicodeDecodeError:return ()
        record=knowledge.find(value) if knowledge is not None else None
        if record is not None:return (record.id,)
    return ()


def decode_tag_drag_values(mime_data) -> tuple[str, ...]:
    """Return the complete displayed-value selection carried by tag chips."""
    if mime_data.hasFormat(TAG_VALUES_MIME):
        try:values=json.loads(bytes(mime_data.data(TAG_VALUES_MIME)).decode("utf-8"))
        except (UnicodeDecodeError,json.JSONDecodeError,TypeError):values=[]
        if isinstance(values,list):
            result=[]; seen=set()
            for value in values:
                display=str(value).strip(); key=normalize_tag(display)
                if key and key not in seen:
                    result.append(display); seen.add(key)
            if result:return tuple(result)
    if mime_data.hasFormat(TAG_MIME):
        try:value=bytes(mime_data.data(TAG_MIME)).decode("utf-8")
        except UnicodeDecodeError:return ()
        return (value,) if normalize_tag(value) else ()
    return ()


def parse_tag_paste(text: str) -> list[str]:
    """Parse a CSV-style tag paste while retaining quoted commas and Unicode."""
    try:
        rows=csv.reader(io.StringIO(str(text or "")),skipinitialspace=True)
        values=[cell.strip() for row in rows for cell in row if cell.strip()]
    except csv.Error:
        values=[str(text or "").strip()]
    result=[]; seen=set()
    for value in values:
        key=normalize_tag(value)
        if key and key not in seen:
            result.append(value); seen.add(key)
    return result


class FlowLayout(QLayout):
    """Small reusable wrapping layout for chips and bounded palette choices."""

    def __init__(self,parent=None,margin: int=0,h_spacing: int=6,v_spacing: int=6):
        super().__init__(parent); self._items=[]; self._h=h_spacing; self._v=v_spacing
        self.setContentsMargins(margin,margin,margin,margin)

    def addItem(self,item: QLayoutItem): self._items.append(item)
    def count(self): return len(self._items)
    def itemAt(self,index): return self._items[index] if 0<=index<len(self._items) else None
    def takeAt(self,index): return self._items.pop(index) if 0<=index<len(self._items) else None
    def expandingDirections(self): return Qt.Orientations(Qt.Orientation(0))
    def hasHeightForWidth(self): return True
    def heightForWidth(self,width): return self._layout(QRect(0,0,width,0),True)
    def setGeometry(self,rect): super().setGeometry(rect); self._layout(rect,False)
    def sizeHint(self): return self.minimumSize()
    def minimumSize(self):
        size=QSize()
        for item in self._items: size=size.expandedTo(item.minimumSize())
        margins=self.contentsMargins(); return size+QSize(margins.left()+margins.right(),margins.top()+margins.bottom())

    def clear(self):
        while self.count():
            item=self.takeAt(0); widget=item.widget()
            if widget is not None:
                # A visible child becomes a temporary top-level window when it
                # is unparented before deleteLater() runs.  Keep it owned by the
                # surrounding surface for the rest of its deferred lifetime.
                self._retire_widget(widget)

    def _retire_widget(self,widget):
        widget.hide()
        owner=self.parentWidget()
        retirement_parent=owner.parentWidget() if owner is not None else None
        if retirement_parent is not None:
            widget.setParent(retirement_parent)
        widget.deleteLater()

    def replace_widgets(self,widgets):
        """Reorder/reuse children while retiring removed widgets in place."""
        widgets=list(widgets); retained={id(widget) for widget in widgets}
        for index in range(self.count()-1,-1,-1):
            item=self.itemAt(index); widget=item.widget()
            if widget is not None and id(widget) not in retained:
                self.takeAt(index); self._retire_widget(widget)
        old_items={
            id(item.widget()):item for item in self._items
            if item.widget() is not None
        }
        self._items=[]
        for widget in widgets:
            item=old_items.get(id(widget))
            if item is not None:
                self._items.append(item)
            else:
                self.addWidget(widget)
            if self.parentWidget() is not None and self.parentWidget().isVisible():
                widget.show()

    def _layout(self,rect: QRect,test_only: bool):
        margins=self.contentsMargins(); area=rect.adjusted(margins.left(),margins.top(),-margins.right(),-margins.bottom())
        x=area.x(); y=area.y(); line_height=0
        for item in self._items:
            hint=item.sizeHint(); next_x=x+hint.width()+self._h
            if next_x-self._h>area.right() and line_height>0:
                x=area.x(); y+=line_height+self._v; next_x=x+hint.width()+self._h; line_height=0
            if not test_only: item.setGeometry(QRect(QPoint(x,y),hint))
            x=next_x; line_height=max(line_height,hint.height())
        return y+line_height-rect.y()+margins.bottom()


def category_details(value,knowledge,categories) -> tuple[str,str,str]:
    record=knowledge.find(value) if knowledge is not None else None
    category_id=str(getattr(record,"category","") or "uncategorized")
    category=categories.get(category_id) if categories is not None else None
    label=categories.path_label(category_id) if categories is not None else category_id
    if label==category_id: label=category_id.replace("_"," ").title()
    color=""
    if categories is not None:
        if hasattr(categories,"effective_color"):
            color=categories.effective_color(category_id)[0]
        else:
            for path_id in reversed(categories.path_ids(category_id)):
                candidate=categories.get(path_id)
                if candidate is not None and candidate.color:
                    color=candidate.color; break
    description=str(getattr(category,"description","") or "").strip()
    meaning=str(getattr(record,"notes","") or "").strip()
    meaning=(
        meaning
        if meaning not in _PLACEHOLDER_MEANING_NOTES else
        "Not yet reviewed."
    )
    lines=[value,f"Meaning & Usage: {meaning}",f"Category: {label}"]
    if description:
        lines.append(f"Category description: {description}")
    tooltip="\n".join(lines)
    return label,color,tooltip


class TagChip(QFrame):
    remove_requested=Signal(str)
    remove_ignore_requested=Signal(str)
    favorite_requested=Signal(str,bool)
    chosen=Signal(str)
    selection_requested=Signal(str,object)
    context_targeted=Signal(str)
    edit_requested=Signal(str)
    change_category_requested=Signal(str)
    arrange_requested=Signal(str)
    remove_knowledge_requested=Signal(str)
    merge_knowledge_requested=Signal(str)
    always_ignore_requested=Signal(str)
    promote_requested=Signal(str)

    def __init__(self,value: str,*,category_label: str,color: str="",tooltip: str="",favorite: bool=False,editable: bool=True,draggable: bool=False,manage: bool=False,selectable: bool=False,details_enabled: bool=False,stable_id: str="",category_id: str="",sensitivity: str="",drag_ids_provider=None,drag_values_provider=None,relationship_label: str="",accessible_role: str="",automation_actions_provider=None,context_menu_provider=None,state_indicator=(),parent=None):
        super().__init__(parent); self.value=value; self.stable_id=str(stable_id); self.favorite=favorite; self.editable=editable; self.draggable=bool(draggable); self.manage=bool(manage); self.selectable=bool(selectable); self.details_enabled=bool(details_enabled); self.relationship_label=str(relationship_label); self.accessible_role=str(accessible_role).strip(); self.drag_ids_provider=drag_ids_provider; self.drag_values_provider=drag_values_provider; self.automation_actions_provider=automation_actions_provider; self.context_menu_provider=context_menu_provider; self._selected=False; self._press_position=None; self._drag_started=False; self._appearance_context=(str(category_id),str(sensitivity)); self._state_indicator=("", "", "solid", ""); self._knowledge=None; self._categories=None
        self.setObjectName("sdxlTagChip"); self.setFocusPolicy(Qt.StrongFocus if editable or manage or self.selectable or self.details_enabled or self.relationship_label else Qt.TabFocus)
        self._state_indicator=self._indicator_state(*state_indicator)
        layout=QHBoxLayout(self); layout.setContentsMargins(2,2,2,2); layout.setSpacing(0)
        self.inner=QFrame(); self.inner.setObjectName("sdxlTagChipInner"); layout.addWidget(self.inner)
        inner_layout=QHBoxLayout(self.inner); inner_layout.setContentsMargins(8,2,3,2); inner_layout.setSpacing(3)
        self._state_mark=None
        self._remove_button=None
        self.selection_mark=QLabel(self.inner)
        self.selection_mark.setObjectName("tagChipSelectionMark")
        self.selection_mark.setAlignment(Qt.AlignCenter)
        self.selection_mark.setFixedWidth(14)
        self.selection_mark.setAttribute(Qt.WA_TransparentForMouseEvents,True)
        self.selection_mark.setVisible(self.manage or self.selectable)
        inner_layout.addWidget(self.selection_mark)
        self.label=QLabel(value)
        self.label.setTextInteractionFlags(Qt.NoTextInteraction if self.draggable else Qt.TextSelectableByMouse)
        # The chip owns selection and its context menu.  Let pointer events over
        # the text reach the chip instead of being swallowed by the QLabel.
        self.label.setAttribute(Qt.WA_TransparentForMouseEvents,bool(editable or manage or self.selectable or self.details_enabled or self.relationship_label or self.draggable))
        inner_layout.addWidget(self.label)
        if (editable and not manage) or self.relationship_label:
            self.remove_button
        _APPEARANCE_CHIPS.add(self)
        self.set_category_presentation(category_label,color,tooltip)
        if editable or manage or self.details_enabled or self.relationship_label or self.context_menu_provider:
            self.setContextMenuPolicy(Qt.CustomContextMenu)
            self.customContextMenuRequested.connect(
                self._provided_menu if self.context_menu_provider else self._relationship_menu if self.relationship_label
                else self._manage_menu if manage
                else self._details_menu if self.details_enabled
                else self._menu
            )

    @property
    def state_mark(self):
        """Allocate a state marker only when a state or explicit caller needs it."""
        if self._state_mark is None:
            mark=QLabel(self.inner)
            mark.setObjectName("tagChipStateMark")
            mark.setAlignment(Qt.AlignCenter)
            mark.setAttribute(Qt.WA_TransparentForMouseEvents,True)
            mark.hide()
            self.inner.layout().insertWidget(0,mark)
            self._state_mark=mark
        return self._state_mark

    @property
    def remove_button(self):
        """Keep inactive chips light while retaining the existing control API."""
        if self._remove_button is None:
            # Parent before configuring visibility so no temporary window appears.
            button=QPushButton("×",self.inner)
            button.setObjectName("tagChipRemove"); button.setFlat(True)
            button.setFixedSize(26,26)
            button.setAccessibleName(
                f"Remove {self.relationship_label} link to {self.value}"
                if self.relationship_label else f"Remove SDXL Tag {self.value}"
            )
            button.setToolTip(
                f"Remove {self.relationship_label} link to {self.value}"
                if self.relationship_label else f"Remove {self.value} from this image"
            )
            button.clicked.connect(lambda:self.remove_requested.emit(self.value))
            button.setVisible((self.editable and not self.manage) or bool(self.relationship_label))
            self.inner.layout().addWidget(button)
            self._remove_button=button
        return self._remove_button

    def set_category_presentation(self,category_label: str,color: str | None="",tooltip: str="",*,category_id=None,sensitivity=None):
        self._presentation=(category_label,color,tooltip)
        if category_id is not None or sensitivity is not None:
            current_category,current_sensitivity=self._appearance_context
            self._appearance_context=(
                current_category if category_id is None else str(category_id),
                current_sensitivity if sensitivity is None else str(sensitivity),
            )
        state=" Selected." if self._selected else ""
        if self.relationship_label:accessible=f"{self.relationship_label} SDXL Tag {self.value}. Category {category_label}. Open and remove-link actions available."
        elif self.manage:accessible=f"SDXL Tag {self.value}. Category {category_label}.{state} Edit, category, ignore, and System Knowledge removal actions available."
        elif self.selectable:
            role=self.accessible_role or "Related SDXL Tag"
            action=(
                "Select for confirmed-tag actions."
                if role=="Confirmed SDXL Tag" else
                "Select to stage it for this image."
            )
            if self.context_menu_provider:
                action+=" Right-click or press Menu/Shift+F10 for review actions."
            if self.draggable:
                action+=" Drag to confirmed tags to stage the current selection."
            accessible=(
                f"{role} {self.value}. Category {category_label}.{state} "
                f"{action}"
            )
        elif self.editable and self.stable_id:accessible=f"SDXL Tag {self.value}. Category {category_label}. Remove button and actions menu available, including Tag Details and per-image ignore."
        elif self.editable:accessible=f"Confirmed SDXL Tag {self.value}. Category {category_label}. System Knowledge identity repair is required before this image can be saved."
        elif self.details_enabled and self.stable_id:accessible=f"Confirmed SDXL Tag {self.value}. Category {category_label}. Open Tag Details with Enter, double-click, or the actions menu."
        else:accessible=f"Confirmed SDXL Tag {self.value}. Category {category_label}."
        state_label=self._state_indicator[0]
        if state_label:accessible+=f" Application state: {state_label}."
        self.setAccessibleName(accessible)
        detail=tooltip or f"{self.value}\nCategory: {category_label}"
        self._base_tooltip=detail
        self._apply_appearance()

    def _appearance_theme(self):
        palette=self.palette()
        border=palette.color(QPalette.Midlight).name().upper()
        background=palette.color(QPalette.Base).name().upper()
        text=palette.color(QPalette.Text).name().upper()
        return {
            "accent":border,"border":border,
            "background":background,"text":text,
        }

    def _apply_appearance(self):
        category_id,sensitivity=self._appearance_context
        semantic=QColor(self._presentation[1] or "")
        semantic_color=semantic.name().upper() if semantic.isValid() else ""
        theme=self._appearance_theme()
        app=QApplication.instance()
        high_contrast=bool(app.property("collectigenHighContrast")) if app is not None else False
        resolved=default_tag_appearance_resolver().resolve(
            tag_id=self.stable_id,category_id=category_id,
            semantic_accent=semantic_color,sensitivity=sensitivity,
            theme=theme,theme_key="|".join(theme.values()),
            high_contrast=high_contrast,
        )
        state_label,state_color,state_pattern,state_mark=self._state_indicator
        self.selection_mark.setText("✓" if self._selected else "")
        if state_label or self._state_mark is not None:
            self.state_mark.setText(state_mark if state_label else "")
            self.state_mark.setVisible(bool(state_label))
        if self.hasFocus():
            outer_color=self.palette().color(QPalette.Highlight).name()
            outer_width=3; outer_pattern="dashed"
        elif self._selected:
            outer_color=self.palette().color(QPalette.Highlight).name()
            outer_width=3; outer_pattern="solid"
        elif state_color:
            outer_color=state_color; outer_width=2; outer_pattern=state_pattern
        else:
            outer_color="transparent"; outer_width=2; outer_pattern="solid"
        self.setStyleSheet(
            f"/* semantic-accent:{semantic.name() if semantic.isValid() else ''} */"
            f"QFrame#sdxlTagChip {{ background:transparent; border:{outer_width}px {outer_pattern} {outer_color}; border-radius:11px; }}"
            f"QFrame#sdxlTagChipInner {{ background:{resolved.background.color}; color:{resolved.text.color}; "
            f"border:1px solid {resolved.border.color}; border-left:4px solid {resolved.accent.color}; border-radius:8px; }}"
            f"QFrame#sdxlTagChipInner QLabel {{ color:{resolved.text.color}; background:transparent; border:0; }}"
            f"QLabel#tagChipSelectionMark {{ color:{self.palette().color(QPalette.Highlight).name()}; font-weight:800; }}"
            f"QLabel#tagChipStateMark {{ color:{state_color or resolved.text.color}; font-weight:800; }}"
            "QFrame#sdxlTagChipInner QPushButton#tagChipRemove { border:0; border-radius:7px; padding:0; min-height:20px; font-weight:700; }"
            "QFrame#sdxlTagChipInner QPushButton#tagChipRemove:hover { background:palette(midlight); }"
        )
        notices=[]
        if resolved.contrast_warning:
            notices.append(
                f"Appearance warning: text contrast is {resolved.contrast_ratio:.2f}:1, "
                "below the 4.5:1 readable-text target. Review this Tag's Colors settings."
            )
        if resolved.safety_substituted:
            notices.append(
                "High-contrast safety substitute: readable theme colors are active."
            )
        # Category-wide guidance stays in accessible detail and Category details;
        # hover help prioritizes the exact Tag's reviewed meaning.
        detail="\n".join(line for line in self._base_tooltip.splitlines()
                         if not line.startswith("Category description:"))
        if notices:
            detail+="\n\n"+"\n".join(notices)
        self.setAccessibleDescription(self._base_tooltip); self.setToolTip(detail); self.label.setToolTip(detail)

    def refresh_appearance(self):
        if hasattr(self,"inner"):
            record=(
                self._knowledge.tags.get(self.stable_id)
                if self._knowledge is not None and self.stable_id else None
            )
            if record is not None:
                category_id=str(getattr(record,"category","") or "")
                sensitivity=effective_tag_sensitivity(record,self._categories)[0]
                self._appearance_context=(category_id,sensitivity)
                if self._categories is not None:
                    label,color,_tooltip=category_details(record.display,self._knowledge,self._categories)
                    self._presentation=(label,color,self._presentation[2])
            self._apply_appearance()

    def set_knowledge_context(self,knowledge,categories):
        self._knowledge=knowledge; self._categories=categories

    @staticmethod
    def _indicator_state(label="",color="",pattern="solid",mark="⚠"):
        return str(label),str(color),str(pattern or "solid"),str(mark)

    def set_state_indicator(self,label: str="",color: str="",pattern: str="solid",mark: str="⚠"):
        state=self._indicator_state(label,color,pattern,mark)
        if self._state_indicator==state:
            return
        self._state_indicator=state
        self.set_category_presentation(*self._presentation)

    def focusInEvent(self,event):
        self._apply_appearance(); super().focusInEvent(event)

    def focusOutEvent(self,event):
        self._apply_appearance(); super().focusOutEvent(event)
    def mousePressEvent(self,event):
        if event.button()==Qt.LeftButton:self._press_position=event.position().toPoint(); self._drag_started=False
        super().mousePressEvent(event)

    def mouseMoveEvent(self,event):
        if self.draggable and self._press_position is not None and event.buttons()&Qt.LeftButton and (event.position().toPoint()-self._press_position).manhattanLength()>=QApplication.startDragDistance():
            payload=self._drag_mime_data()
            drag=QDrag(self); drag.setMimeData(payload); self._drag_started=True; drag.exec(Qt.MoveAction|Qt.LinkAction,Qt.MoveAction); event.accept(); return
        super().mouseMoveEvent(event)

    def _drag_mime_data(self):
        stable_ids=tuple(self.drag_ids_provider(self.value)) if callable(self.drag_ids_provider) else ((self.stable_id,) if self.stable_id else ())
        payload=QMimeData(); payload.setData(TAG_MIME,self.value.encode("utf-8"))
        if stable_ids:payload.setData(TAG_IDS_MIME,json.dumps(list(stable_ids)).encode("utf-8"))
        values=tuple(self.drag_values_provider(self.value)) if callable(self.drag_values_provider) else (self.value,)
        if values:payload.setData(TAG_VALUES_MIME,json.dumps(list(values)).encode("utf-8"))
        return payload

    def mouseReleaseEvent(self,event):
        if event.button()==Qt.LeftButton and not self._drag_started:
            if self.manage or self.selectable:self.selection_requested.emit(self.value,event.modifiers())
            else:self.chosen.emit(self.value)
        self._press_position=None; self._drag_started=False; super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self,event):
        if (
            self.manage or self.relationship_label
            or self.details_enabled or self.editable
        ) and event.button()==Qt.LeftButton:
            target=self.stable_id or (
                self.value if self.manage or self.relationship_label else ""
            )
            if target:self.edit_requested.emit(target)
            event.accept(); return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self,event):
        if (
            self.manage or self.relationship_label
            or self.details_enabled or self.editable
        ) and event.key() in (Qt.Key_Return,Qt.Key_Enter):
            target=self.stable_id or (
                self.value if self.manage or self.relationship_label else ""
            )
            if target:self.edit_requested.emit(target)
            event.accept(); return
        if (self.manage or self.selectable) and event.key()==Qt.Key_Space:
            self.selection_requested.emit(self.value,event.modifiers()); event.accept(); return
        if event.key() in (Qt.Key_Return,Qt.Key_Enter,Qt.Key_Space):
            self.chosen.emit(self.value); event.accept(); return
        if (self.editable or self.relationship_label) and event.key() in (Qt.Key_Delete,Qt.Key_Backspace):
            self.remove_requested.emit(self.value); event.accept(); return
        if (self.editable or self.manage or self.details_enabled or self.relationship_label or self.context_menu_provider) and (event.key()==Qt.Key_Menu
                or (event.key()==Qt.Key_F10 and event.modifiers()&Qt.ShiftModifier)):
            (
                self._provided_menu if self.context_menu_provider else self._relationship_menu if self.relationship_label
                else self._manage_menu if self.manage
                else self._details_menu if self.details_enabled
                else self._menu
            )(self.rect().center()); event.accept(); return
        super().keyPressEvent(event)

    def _provided_menu(self,position):
        """Build a caller-owned QMenu after synchronizing the target selection."""
        self.context_targeted.emit(self.value)
        menu=self.context_menu_provider(self)
        try:
            menu.exec(self.mapToGlobal(position))
        finally:
            menu.deleteLater()

    def image_actions_menu(self):
        menu=QMenu(self)
        if self.stable_id:
            edit=menu.addAction("Open Tag Details\u2026")
            edit.triggered.connect(lambda:self.edit_requested.emit(self.stable_id))
        else:
            edit=menu.addAction("Repair System Knowledge Identity\u2026")
            edit.setToolTip(
                "Review the missing, ambiguous, ignored, or removed reusable "
                "identity before this image can be saved."
            )
            edit.triggered.connect(
                lambda:self.promote_requested.emit(self.value)
            )
        menu.addSeparator()
        remove=menu.addAction("Remove from This Image")
        remove_ignore=menu.addAction("Remove from This Image and Add to Its Ignore List")
        menu.addSeparator()
        favorite=menu.addAction("Remove from Favorites" if self.favorite else "Add to Favorites")
        remove.triggered.connect(lambda:self.remove_requested.emit(self.value))
        remove_ignore.triggered.connect(lambda:self.remove_ignore_requested.emit(self.value))
        favorite.triggered.connect(lambda:self.favorite_requested.emit(self.value,not self.favorite))
        return menu

    def _menu(self,position):
        self.image_actions_menu().exec(self.mapToGlobal(position))

    def details_actions_menu(self):
        menu=QMenu(self)
        edit=menu.addAction("Open Tag Details\u2026")
        edit.setEnabled(bool(self.stable_id))
        if not self.stable_id:
            edit.setToolTip("This image tag is not yet in System Knowledge.")
        edit.triggered.connect(lambda:self.edit_requested.emit(self.stable_id))
        return menu

    def _details_menu(self,position):
        self.details_actions_menu().exec(self.mapToGlobal(position))

    def manage_actions_menu(self):
        menu=QMenu(self)
        edit=menu.addAction("Edit Tag…"); category=menu.addAction("Change Category…"); placement=menu.addAction("Change Caption Placement…")
        selection=self._manage_selection_label()
        selected_count=len(tuple(self.drag_ids_provider(self.value))) if callable(self.drag_ids_provider) else 1
        menu.addSeparator(); merge=menu.addAction("Merge Duplicate Tags…"); merge.setEnabled(selected_count>=2)
        merge.setToolTip("Choose one canonical survivor for the complete selection." if selected_count>=2 else "Select two or more SDXL Tags to merge duplicate identities.")
        ignore=menu.addAction(f"Always Ignore {selection}…"); remove=menu.addAction(f"Remove {selection} from System Knowledge…")
        automation_available=bool(self.automation_actions_provider()) if callable(self.automation_actions_provider) else True
        ignore.setEnabled(automation_available); ignore.setToolTip("Create an exact global ignore rule." if automation_available else "Save or reset staged Category Workspace changes before writing Rules & Automation.")
        edit.triggered.connect(lambda:self.edit_requested.emit(self.value))
        category.triggered.connect(lambda:self.change_category_requested.emit(self.value))
        placement.triggered.connect(lambda:self.arrange_requested.emit(self.value))
        merge.triggered.connect(lambda:self.merge_knowledge_requested.emit(self.value))
        ignore.triggered.connect(lambda:self.always_ignore_requested.emit(self.value))
        remove.triggered.connect(lambda:self.remove_knowledge_requested.emit(self.value))
        return menu

    def _manage_menu(self,position):
        self.context_targeted.emit(self.value)
        self.manage_actions_menu().exec(self.mapToGlobal(position))

    def _manage_selection_label(self):
        count=len(tuple(self.drag_ids_provider(self.value))) if callable(self.drag_ids_provider) else 1
        return f"{count:,} Selected Tag{'s' if count!=1 else ''}"

    def _relationship_menu(self,position):
        menu=QMenu(self); open_tag=menu.addAction("Open Tag Details…"); remove=menu.addAction(f"Remove {self.relationship_label} Link")
        chosen=menu.exec(self.mapToGlobal(position))
        if chosen is open_tag:self.edit_requested.emit(self.stable_id or self.value)
        elif chosen is remove:self.remove_requested.emit(self.value)

    def set_selected(self,selected: bool):
        if self._selected==bool(selected):return
        self._selected=bool(selected); self.set_category_presentation(*self._presentation)


class TagChipEditor(QWidget):
    remove_requested=Signal(str)
    remove_ignore_requested=Signal(str)
    favorite_requested=Signal(str,bool)
    chosen=Signal(str)
    edit_requested=Signal(str)
    change_category_requested=Signal(object)
    arrange_requested=Signal(str)
    remove_knowledge_requested=Signal(object)
    merge_knowledge_requested=Signal(object)
    always_ignore_requested=Signal(object)
    promote_requested=Signal(str)
    selection_changed=Signal(object)
    drop_requested=Signal(object)

    def __init__(self,parent=None,*,editable: bool=True,show_status: bool=True,draggable: bool=False,manage: bool=False,selectable: bool=False,details_enabled: bool=False,accept_drops: bool=False,accessible_role: str="",drag_values_provider=None,automation_actions_provider=None,context_menu_provider=None):
        super().__init__(parent); self.editable=editable; self.draggable=bool(draggable); self.manage=bool(manage); self.selectable=bool(selectable); self.details_enabled=bool(details_enabled); self.accept_drops=bool(accept_drops); self.accessible_role=str(accessible_role).strip(); self.drag_values_provider=drag_values_provider; self.automation_actions_provider=automation_actions_provider; self.context_menu_provider=context_menu_provider; self._chips=[]; self._selected=[]; self._anchor=-1; self._stable_ids={}
        self._restore_timer=QTimer(self); self._restore_timer.setSingleShot(True)
        self._restore_timer.timeout.connect(self._restore_view)
        self._restore_intent=None
        self.setAcceptDrops(self.accept_drops)
        layout=QVBoxLayout(self); layout.setContentsMargins(0,0,0,0)
        self.status=QLabel(self); self.status.setObjectName("mutedLabel"); self.status.setVisible(show_status); layout.addWidget(self.status)
        self.scroll=QScrollArea(); self.scroll.setWidgetResizable(True); self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setMinimumHeight(100); self.scroll.setAccessibleName("Confirmed SDXL Tags")
        self.container=QWidget(); self.flow=FlowLayout(self.container); self.scroll.setWidget(self.container); layout.addWidget(self.scroll,1)

    def set_tags(self,values,knowledge,categories,favorites=(),*,shared_category=None,stable_ids=None,tooltips=None,enabled=None,state_indicators=None):
        values=list(values); favorite_keys={normalize_tag(value) for value in favorites}; tooltip_values={normalize_tag(value):str(text) for value,text in dict(tooltips or {}).items()}; enabled_values={normalize_tag(value):bool(state) for value,state in dict(enabled or {}).items()}
        indicator_values={
            normalize_tag(value):tuple(indicator)
            for value,indicator in dict(state_indicators or {}).items()
        }
        old_chips={
            normalize_tag(chip.value):chip for chip in self._chips
        }
        focus=QApplication.focusWidget()
        focused_chip=next((
            chip for chip in self._chips
            if focus is chip or (focus is not None and chip.isAncestorOf(focus))
        ),None)
        scroll=self.scroll.verticalScrollBar().value()
        selected_keys={normalize_tag(value) for value in self._selected}
        anchor_key=(
            normalize_tag(self._chips[self._anchor].value)
            if 0<=self._anchor<len(self._chips) else ""
        )
        self._stable_ids={normalize_tag(value):str(stable_id) for value,stable_id in dict(stable_ids or {}).items() if str(stable_id)}
        status=f"{len(values):,} confirmed SDXL Tag"+("s" if len(values)!=1 else "")
        self.status.setText(status); self.scroll.setAccessibleDescription(status)
        QAccessible.updateAccessibility(QAccessibleEvent(self.scroll,QAccessible.Event.DescriptionChanged))
        if shared_category is not None:
            shared_label,shared_color,shared_description=shared_category[:3]
            shared_category_id=str(shared_category[3]) if len(shared_category)>3 else ""
        chips=[]
        for value in values:
            key=normalize_tag(value)
            stable_id=self._stable_ids.get(key,"")
            record=(
                knowledge.tags.get(stable_id)
                if knowledge is not None and stable_id else None
            )
            if record is None:
                record=knowledge.find(value) if knowledge is not None else None
            if shared_category is None:label,color,tooltip=category_details(record.display if record is not None else value,knowledge,categories)
            else:
                label,color=shared_label,shared_color
                meaning=str(getattr(record,"notes","") or "").strip()
                if meaning in _PLACEHOLDER_MEANING_NOTES:
                    meaning="Not yet reviewed."
                lines=[
                    value,
                    f"Meaning & Usage: {meaning}",
                    f"Category: {shared_label}",
                ]
                if shared_description:
                    lines.append(f"Category description: {shared_description}")
                tooltip="\n".join(lines)
            if record is not None:self._stable_ids[normalize_tag(value)]=record.id
            stable_id=self._stable_ids.get(normalize_tag(value),"")
            category_id=(
                str(getattr(record,"category","") or "")
                if record is not None else
                shared_category_id if shared_category is not None else ""
            )
            sensitivity=(
                effective_tag_sensitivity(record,categories)[0]
                if record is not None else ""
            )
            context_tooltip=tooltip_values.get(normalize_tag(value),"").strip()
            if context_tooltip:
                tooltip=f"{tooltip}\n\n{context_tooltip}"
            chip=old_chips.get(key)
            indicator=indicator_values.get(normalize_tag(value),())
            if chip is None or chip.value!=value:
                chip=TagChip(value,category_label=label,color=color,tooltip=tooltip,
                             favorite=key in favorite_keys,editable=self.editable,draggable=self.draggable,manage=self.manage,selectable=self.selectable,
                             details_enabled=self.details_enabled,stable_id=stable_id,category_id=category_id,sensitivity=sensitivity,drag_ids_provider=self._drag_ids,drag_values_provider=self._drag_values,
                             accessible_role=self.accessible_role,automation_actions_provider=self.automation_actions_provider,context_menu_provider=self.context_menu_provider,state_indicator=indicator,parent=self.container)
                chip.remove_requested.connect(self.remove_requested); chip.remove_ignore_requested.connect(self.remove_ignore_requested)
                chip.favorite_requested.connect(self.favorite_requested); chip.chosen.connect(self.chosen); chip.selection_requested.connect(self._select)
                chip.context_targeted.connect(self._context_target); chip.edit_requested.connect(self.edit_requested); chip.change_category_requested.connect(lambda _value:self.change_category_requested.emit(tuple(self._selected)))
                chip.arrange_requested.connect(self.arrange_requested)
                chip.remove_knowledge_requested.connect(lambda _value:self.remove_knowledge_requested.emit(tuple(self._selected)))
                chip.merge_knowledge_requested.connect(lambda _value:self.merge_knowledge_requested.emit(tuple(self._selected)))
                chip.always_ignore_requested.connect(lambda _value:self.always_ignore_requested.emit(tuple(self._selected)))
                chip.promote_requested.connect(self.promote_requested)
            else:
                chip.favorite=key in favorite_keys
                stable_changed=chip.stable_id!=stable_id
                chip.stable_id=stable_id
                context_changed=chip._appearance_context!=(category_id,sensitivity)
                if stable_changed or context_changed or chip._presentation!=(label,color,tooltip):
                    chip.set_category_presentation(label,color,tooltip,category_id=category_id,sensitivity=sensitivity)
            chip.set_knowledge_context(knowledge,categories)
            chip.set_state_indicator(*indicator) if indicator else chip.set_state_indicator()
            desired_enabled=enabled_values.get(normalize_tag(value),True)
            if chip.isEnabled()!=desired_enabled:
                chip.setEnabled(desired_enabled)
            chips.append(chip)
        widgets=list(chips)
        if not values:
            empty=QLabel("No confirmed SDXL Tags",self.container); empty.setObjectName("mutedLabel"); widgets.append(empty)
        self.flow.replace_widgets(widgets); self._chips=chips
        self._selected=[
            chip.value for chip in chips
            if chip.isEnabled() and normalize_tag(chip.value) in selected_keys
        ]
        self._anchor=next((
            index for index,chip in enumerate(chips)
            if normalize_tag(chip.value)==anchor_key
        ),-1)
        self._refresh_selection(); self.container.updateGeometry()
        if focused_chip in chips:
            focused_chip.setFocus(Qt.OtherFocusReason)
        self._schedule_view_restore(scroll,focused_chip if focused_chip in chips else None)

    def _schedule_view_restore(self,scroll,focus=None):
        """Coalesce deferred geometry recovery into this editor's lifetime."""
        self._restore_intent=(int(scroll),focus)
        self._restore_timer.start(0)

    def _restore_view(self):
        intent=self._restore_intent; self._restore_intent=None
        if intent is None:return
        scroll,focus=intent
        self.scroll.verticalScrollBar().setValue(scroll)
        if focus is not None and focus in self._chips:
            focus.setFocus(Qt.OtherFocusReason)
            self.scroll.ensureWidgetVisible(focus)

    def refresh_category_details(self,category_label: str,color: str="",description: str="",category_id: str=""):
        for chip in self._chips:
            tooltip=f"{chip.value}\nCategory: {category_label}"+(f"\n{description}" if description else "")
            chip.set_category_presentation(category_label,color,tooltip,category_id=category_id or None)

    def _context_target(self,value):
        if value not in self._selected:self._selected=[value]; self._anchor=self.index_of(value); self._refresh_selection()

    def _select(self,value,modifiers):
        index=self.index_of(value); modifiers=Qt.KeyboardModifiers(modifiers)
        if index<0 or not self._chips[index].isEnabled():return
        if modifiers&Qt.ShiftModifier and self._anchor>=0:
            first,last=sorted((self._anchor,index)); self._selected=[chip.value for chip in self._chips[first:last+1]]
        elif modifiers&Qt.ControlModifier:
            if value in self._selected:self._selected.remove(value)
            else:self._selected.append(value)
            self._anchor=index
        else:self._selected=[value]; self._anchor=index
        self._refresh_selection(); self.chosen.emit(value)

    def _refresh_selection(self):
        selected={normalize_tag(value) for value in self._selected}
        for chip in self._chips:chip.set_selected(normalize_tag(chip.value) in selected)
        self.selection_changed.emit(tuple(self._selected))

    def selected_values(self):return tuple(self._selected)

    def select_values(self,values):
        """Restore a stable selection without depending on chip positions."""
        requested={normalize_tag(value) for value in values}
        self._selected=[chip.value for chip in self._chips if chip.isEnabled() and normalize_tag(chip.value) in requested]
        self._anchor=self.index_of(self._selected[-1]) if self._selected else -1
        self._refresh_selection()

    def _drag_ids(self,value):
        values=self._selected if value in self._selected else [value]
        return tuple(self._stable_ids[key] for key in (normalize_tag(item) for item in values) if key in self._stable_ids)

    def _drag_values(self,value):
        if callable(self.drag_values_provider):
            return tuple(self.drag_values_provider(value))
        return tuple(self._selected if value in self._selected else (value,))

    def dragEnterEvent(self,event):
        if self.accept_drops and decode_tag_drag_values(event.mimeData()):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self,event):
        self.dragEnterEvent(event)

    def dropEvent(self,event):
        values=decode_tag_drag_values(event.mimeData()) if self.accept_drops else ()
        if not values:
            event.ignore()
            return
        self.drop_requested.emit(values)
        event.acceptProposedAction()

    def index_of(self,value) -> int:
        key=normalize_tag(value)
        return next((index for index,chip in enumerate(self._chips) if normalize_tag(chip.value)==key),-1)

    def focus_index(self,index: int):
        if not self._chips:return
        chip=self._chips[min(max(0,index),len(self._chips)-1)]
        chip.setFocus(Qt.OtherFocusReason); self.scroll.ensureWidgetVisible(chip)

    def focus_tag(self,value):
        index=self.index_of(value)
        if index>=0:self.focus_index(index)


class TagChoiceChip(TagChip):
    """Standard tag chip used as an add choice in the known-tag palette."""

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.label.setAttribute(Qt.WA_TransparentForMouseEvents,True)

    def text(self):
        """Compatibility text for callers that previously inspected a button."""
        return f"{self.value} - {self._presentation[0]}"

    def keyPressEvent(self,event):
        if event.key() in (Qt.Key_Return,Qt.Key_Enter,Qt.Key_Space,Qt.Key_Menu) or (
            event.key()==Qt.Key_F10 and event.modifiers()&Qt.ShiftModifier
        ):
            self.customContextMenuRequested.emit(self.rect().center()); event.accept(); return
        super().keyPressEvent(event)


class TagPalette(QWidget):
    filters_changed=Signal()
    tag_requested=Signal(str)
    favorite_requested=Signal(str,bool)
    show_present_requested=Signal(str)
    create_requested=Signal(str)
    selection_changed=Signal(object)
    PAGE_SIZE=80

    def __init__(self,knowledge,categories,parent=None,*,prepared_index=None,shared_root_category=None,shared_conflict_filter=None,defer_initial_render=False,target_label="image"):
        self.target_label = str(target_label)
        self.target_location = "on this image" if target_label == "image" else f"in this {target_label}"
        super().__init__(parent); self.knowledge=knowledge; self.categories=categories; self.present=set(); self.conflicts={}; self.favorites=[]; self.recents=[]; self.limit=self.PAGE_SIZE
        self._defer_initial_render=defer_initial_render; self._buttons=[]
        self._restore_timer=QTimer(self); self._restore_timer.setSingleShot(True)
        self._restore_timer.timeout.connect(self._restore_view)
        self._restore_intent=None
        self.shared_root_category=shared_root_category
        self._favorite_keys=set(); self._recent_keys=set()
        self._selected=[]; self._anchor=-1
        if prepared_index is None:
            self._rebuild_knowledge_index()
        else:
            self._apply_prepared_index(prepared_index)
        layout=QVBoxLayout(self); layout.setContentsMargins(0,0,0,0)
        layout.setSizeConstraint(QLayout.SetMinimumSize)
        self.search=QLineEdit(); self.search.setPlaceholderText("Search selected category and aliases...")
        self.search.setAccessibleName("Search known SDXL Tags and aliases in the selected category")
        self.search.setAccessibleDescription("Search stays inside the selected main and child category. Choose All to search every category.")
        self._search_timer=QTimer(self); self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(180); self._search_timer.timeout.connect(self._refresh_filters)
        self.search.textChanged.connect(self._search_changed); layout.addWidget(self.search)
        outcome_row=QHBoxLayout()
        self.query_outcome=QLabel()
        self.query_outcome.setObjectName("mutedLabel")
        self.query_outcome.setWordWrap(True)
        self.query_outcome.setAccessibleName("Exact searched SDXL Tag status")
        outcome_row.addWidget(self.query_outcome,1)
        self.query_outcome_action=QPushButton()
        self.query_outcome_action.setProperty("compact",True)
        self.query_outcome_action.clicked.connect(self._activate_query_outcome)
        outcome_row.addWidget(self.query_outcome_action)
        layout.addLayout(outcome_row)
        self._query_outcome_kind=""; self._query_outcome_value=""
        self.query_outcome.hide(); self.query_outcome_action.hide()
        filter_header=QHBoxLayout()
        self.filter_toggle=QPushButton("Filters…")
        self.filter_toggle.setCheckable(True)
        self.filter_toggle.setProperty("compact",True)
        self.filter_toggle.setAccessibleName("Show Add Tags filters")
        self.filter_summary=QLabel("Eligible tags • All categories")
        self.filter_summary.setObjectName("mutedLabel")
        filter_header.addWidget(self.filter_toggle)
        filter_header.addWidget(self.filter_summary,1)
        layout.addLayout(filter_header)
        self.filters_panel=QWidget()
        filters_layout=QHBoxLayout(self.filters_panel)
        filters_layout.setContentsMargins(0,0,0,0)
        filters_layout.setSpacing(8)
        filters_layout.addWidget(QLabel("Show"))
        self.availability=shared_conflict_filter if shared_conflict_filter is not None else QComboBox()
        if shared_conflict_filter is None:
            self.availability.setAccessibleName("Known SDXL Tag availability filter")
            self.availability.addItem("Eligible to add","eligible")
            self.availability.addItem(f"Already {self.target_location}","present")
            self.availability.addItem("Conflicts needing review","conflicts")
        self.availability.currentIndexChanged.connect(self._selection_changed)
        filters_layout.addWidget(self.availability,1)
        filters_layout.addWidget(QLabel("Browse" if shared_root_category is not None else "Category"))
        self.main=QComboBox(); self.main.setAccessibleName("Main SDXL Tag category"); self.main.setAccessibleDescription("Choose Favorites, Recent, All, or a main System Knowledge category."); self.main.setMaxVisibleItems(16); self.main.currentIndexChanged.connect(self._selection_changed)
        if shared_root_category is not None:
            self.main.setAccessibleName("Collection for all displayed Tags")
            self.main.setAccessibleDescription("Filter both Tag panes to All, Favorites, or Recent within the shared category.")
            shared_root_category.currentIndexChanged.connect(self._selection_changed)
        filters_layout.addWidget(self.main,1)
        filters_layout.addWidget(QLabel("Within"))
        self.child=QComboBox(); self.child.setAccessibleName("Child SDXL Tag category"); self.child.setAccessibleDescription("Optionally narrow the selected main category."); self.child.currentIndexChanged.connect(self._selection_changed)
        if shared_root_category is not None:
            for control in (self.main,self.child,self.availability):
                control.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
                control.setMinimumContentsLength(14)
            self.child.setAccessibleName("Subcategory for both Tag panes")
        filters_layout.addWidget(self.child,1)
        layout.addWidget(self.filters_panel)
        self.filters_panel.hide()
        self.filter_toggle.toggled.connect(self.filters_panel.setVisible)
        result_row=QHBoxLayout()
        self.count=QLabel(); self.count.setObjectName("mutedLabel"); self.count.setAccessibleName("Known SDXL Tag results"); result_row.addWidget(self.count)
        result_row.addStretch(1)
        self.more=QPushButton("Show More"); self.more.setProperty("compact",True); self.more.clicked.connect(self._show_more); self.more.hide(); result_row.addWidget(self.more)
        layout.addLayout(result_row)
        self.scroll=QScrollArea(); self.scroll.setWidgetResizable(True); self.scroll.setFrameShape(QFrame.NoFrame); self.scroll.setMinimumHeight(170)
        self.scroll.setAccessibleName("Known SDXL Tags available to add")
        self.container=QWidget(); self.flow=FlowLayout(self.container); self.scroll.setWidget(self.container); layout.addWidget(self.scroll,1)
        self._build_categories()
        self._defer_initial_render=False

    def _rebuild_knowledge_index(self):
        self._apply_prepared_index(
            prepare_tag_palette_index(self.knowledge,self.categories)
        )

    def _apply_prepared_index(self,index):
        self._known_keys=index.known_keys
        self._ordered_records=index.ordered_records
        self._ordered_entries=index.ordered_entries
        self._exact_identity_index=index.exact_identity_index
        self._descendant_counts=index.descendant_counts

    def refresh_knowledge(self,*,prepared_index=None,defer_refresh=False):
        if prepared_index is None:self._rebuild_knowledge_index()
        else:self._apply_prepared_index(prepared_index)
        self._build_categories(refresh=not defer_refresh)

    def _roots(self): return list(self.categories.children_of(None)) if self.categories is not None else []
    def _build_categories(self,*,refresh=True):
        selected=self.main.currentData() or "__all__"
        self.main.blockSignals(True); self.main.clear()
        self.main.addItem("Favorites (0)","__favorites__"); self.main.addItem("Recent (0)","__recent__")
        self.main.addItem(f"All ({len(self.knowledge.tags) if self.knowledge is not None else 0:,})", "__all__")
        for record in (() if self.shared_root_category is not None else self._roots()):
            count=self._descendant_counts.get(record.id,0); self.main.addItem(f"{record.name} ({count:,})",record.id)
            self.main.setItemData(self.main.count()-1,record.description or self.categories.path_label(record.id),Qt.ToolTipRole)
        self.main.setCurrentIndex(max(0,self.main.findData(selected))); self.main.blockSignals(False); self._rebuild_children()
        if refresh:self.refresh()

    def set_state(self,*,present=(),conflicts=None,favorites=(),recents=()):
        self.present={normalize_tag(value) for value in present}; self.conflicts={normalize_tag(value):tuple(messages) for value,messages in dict(conflicts or {}).items()}; self.favorites=list(favorites); self.recents=list(recents)
        self._favorite_keys={normalize_tag(value) for value in self.favorites}
        self._recent_keys={normalize_tag(value) for value in self.recents}
        known=self._known_keys
        present_count=len(known&self.present)
        conflict_count=len((known-set(self.present))&set(self.conflicts))
        eligible_count=max(0,len(known)-present_count-conflict_count)
        if self.shared_root_category is None:
            self.availability.setItemText(0,f"Eligible to add ({eligible_count:,})")
            self.availability.setItemText(1,f"Already {self.target_location} ({present_count:,})")
            self.availability.setItemText(2,f"Conflicts needing review ({conflict_count:,})")
        self.main.setItemText(0,f"Favorites ({sum(normalize_tag(value) in known for value in self.favorites):,})")
        self.main.setItemText(1,f"Recent ({sum(normalize_tag(value) in known for value in self.recents):,})")
        self.main.setItemText(2,f"All ({len(known):,})"); self.refresh()

    def _selected_root(self):
        control=self.shared_root_category if self.shared_root_category is not None else self.main
        return control.currentData() or "__all__"
    def _selection_changed(self,*_):
        self._search_timer.stop(); self.limit=self.PAGE_SIZE
        self._rebuild_children(); self._refresh_filters()

    def _refresh_filters(self):
        self.refresh()
        self.filters_changed.emit()

    def _search_changed(self,*_):
        self.limit=self.PAGE_SIZE
        if self.search.text().strip():self._search_timer.start()
        else:
            self._search_timer.stop(); self._refresh_filters()
    def _rebuild_children(self):
        selected=self.child.currentData() if self.child.count() else ""
        root=self._selected_root(); self.child.blockSignals(True); self.child.clear(); self.child.addItem("All child categories","")
        if not str(root).startswith("__"):
            root_path=self.categories.path_label(str(root))
            descendants=sorted(
                (
                    record for record in self.categories.categories.values()
                    if record.id!=root and root in self.categories.path_ids(record.id)
                ),
                key=lambda record:self.categories.path_label(record.id).casefold(),
            )
            for record in descendants:
                path=self.categories.path_label(record.id)
                relative=path[len(root_path):].lstrip(" →") if path.startswith(root_path) else path
                self.child.addItem(f"{relative} ({self._descendant_counts.get(record.id,0):,})",record.id)
        index=self.child.findData(selected); self.child.setCurrentIndex(max(0,index)); self.child.setEnabled(self.child.count()>1); self.child.blockSignals(False)

    def _descends_from(self,category_id: str,ancestor: str) -> bool: return ancestor in self.categories.path_ids(category_id)

    def matches_view(self,value,*,conflict=False,stable_id=""):
        record=(self.knowledge.tags.get(stable_id) if stable_id else self.knowledge.find(value)) if self.knowledge is not None else None
        names=self.knowledge.identity_names(record) if record is not None else (value,)
        path_ids=self.categories.path_ids(record.category) if record is not None else ()
        category=self.categories.path_label(record.category) if record is not None else ""
        haystack=" ".join((*names,category)).casefold()
        return self._matches_view_entry(normalize_tag(value),haystack,path_ids,conflict)

    def _view_filter_state(self):
        return (self._selected_root(),self.child.currentData(),self.search.text().strip().casefold(),
                self.main.currentData(),self.availability.currentData())

    def _matches_view_entry(self,key,haystack,path_ids,conflict,*,ignore_conflict=False,state=None):
        root,child,query,collection,mode=state or self._view_filter_state()
        if query and query not in haystack:return False
        if not str(root).startswith("__") and root not in path_ids:return False
        if child and child not in path_ids:return False
        if collection=="__favorites__" and key not in self._favorite_keys:return False
        if collection=="__recent__" and key not in self._recent_keys:return False
        return ignore_conflict or mode=="all" or (mode=="conflicts" and conflict) or (mode=="clear" and not conflict)
    def _records(self,availability_override=None):
        if self.knowledge is None: return []
        root=self._selected_root(); child=str(self.child.currentData() or ""); query=self.search.text().strip().casefold()
        collection=self.main.currentData()
        favorite_keys={normalize_tag(value) for value in self.favorites}; recent_order={normalize_tag(value):i for i,value in enumerate(self.recents)}
        availability=(
            str(availability_override)
            if availability_override is not None
            else str(self.availability.currentData() or "eligible")
        )
        records=[]
        view_state=self._view_filter_state() if self.shared_root_category is not None else None
        if (view_state is not None and str(root).startswith("__") and not child
                and not query and collection not in ("__favorites__","__recent__")
                and (view_state[-1]=="all" or availability_override=="all")):
            # The ordinary shared All view has no per-record filter decisions.
            # Keep exact order/count and current present membership without
            # calling the general filter predicate for the entire vocabulary.
            if availability_override is not None:
                return list(self._ordered_records)
            return [record for record,key,_,_ in self._ordered_entries
                    if key not in self.present]
        for record,key,haystack,path_ids in self._ordered_entries:
            if self.shared_root_category is not None:
                if availability_override is None and key in self.present:continue
                if self._matches_view_entry(key,haystack,path_ids,bool(self.conflicts.get(key)),ignore_conflict=availability_override=="all",state=view_state):
                    records.append(record)
                continue
            if query and query not in haystack: continue
            if availability=="eligible" and (key in self.present or key in self.conflicts): continue
            if availability=="present" and key not in self.present: continue
            if availability=="conflicts" and (key in self.present or key not in self.conflicts): continue
            if collection=="__favorites__" and key not in favorite_keys: continue
            if collection=="__recent__" and key not in recent_order: continue
            if not str(root).startswith("__") and str(root) not in path_ids: continue
            if child and child not in path_ids: continue
            records.append(record)
        if collection=="__recent__": records.sort(key=lambda item:recent_order.get(normalize_tag(item.display),9999))
        return records

    def refresh(self):
        if self._defer_initial_render:return
        records=self._records(); shown=records[:self.limit]
        selected_keys={normalize_tag(value) for value in self._selected}
        anchor_key=(
            normalize_tag(self._buttons[self._anchor].value)
            if hasattr(self,"_buttons") and 0<=self._anchor<len(self._buttons)
            else ""
        )
        old_buttons={
            (str(chip.stable_id),chip.value):chip
            for chip in getattr(self,"_buttons",())
        }
        focus=QApplication.focusWidget()
        focused_key=next((
            (str(chip.stable_id),chip.value)
            for chip in old_buttons.values()
            if focus is chip or (focus is not None and chip.isAncestorOf(focus))
        ),None)
        scroll=self.scroll.verticalScrollBar().value()
        buttons=[]
        for record in shown:
            label,color,tooltip=category_details(record.display,self.knowledge,self.categories)
            key=(str(record.id),record.display); chip=old_buttons.get(key)
            if chip is None:
                chip=TagChoiceChip(record.display,category_label=label,color=color,tooltip=tooltip,editable=False,draggable=True,selectable=True,stable_id=record.id,category_id=record.category,sensitivity=effective_tag_sensitivity(record,self.categories)[0],drag_values_provider=self._drag_values,parent=self.container)
                chip.setProperty("tagValue",record.display); chip.setSizePolicy(QSizePolicy.Maximum,QSizePolicy.Fixed); chip.setFocusPolicy(Qt.StrongFocus); chip.setCursor(Qt.OpenHandCursor)
                chip.selection_requested.connect(self._select)
                chip.setContextMenuPolicy(Qt.CustomContextMenu)
                chip.customContextMenuRequested.connect(
                    lambda point,value=record.display,control=chip:
                    self._favorite_menu(control,point,value)
                )
            else:
                context=(record.category,effective_tag_sensitivity(record,self.categories)[0])
                if chip._presentation!=(label,color,tooltip) or chip._appearance_context!=context:
                    chip.set_category_presentation(label,color,tooltip,category_id=context[0],sensitivity=context[1])
            chip.set_knowledge_context(self.knowledge,self.categories)
            desired_enabled=normalize_tag(record.display) not in self.present
            if chip.isEnabled()!=desired_enabled:
                chip.setEnabled(desired_enabled)
            conflict_messages=self.conflicts.get(normalize_tag(record.display),())
            chip.setAccessibleName(
                f"SDXL Tag choice {record.display}. Category {label}. "
                +(
                    "Diagnostic conflict review required. Drag to confirmed tags "
                    "or open the Actions menu to add."
                    if conflict_messages else
                    "Drag to confirmed tags or open the Actions menu to add."
                )
                if chip.isEnabled() else f"SDXL Tag {record.display}. Category {label}. Already {self.target_location}."
            )
            suffix=(
                f"\nAlready {self.target_location}" if not chip.isEnabled()
                else "\nConflict review required before adding:\n"
                +"\n".join(conflict_messages)
                if conflict_messages
                else f"\nDrag to {'confirmed tags' if self.target_label == 'image' else 'the Tag list'} or use Add to This {self.target_label.title()} from the Actions menu"
            )
            effective_aliases=self.knowledge.effective_aliases(record)
            aliases=f"\nOther Names: {', '.join(effective_aliases)}" if effective_aliases else ""; chip.setToolTip(tooltip+aliases+suffix); chip.label.setToolTip(chip.toolTip())
            buttons.append(chip)
        widgets=list(buttons)
        if not shown:
            message="No matching known SDXL Tags"
            if str(self.availability.currentData() or "eligible")=="eligible":
                hidden=self._records("all")
                present=sum(
                    normalize_tag(record.display) in self.present
                    for record in hidden
                )
                conflicts=sum(
                    normalize_tag(record.display) in self.conflicts
                    and normalize_tag(record.display) not in self.present
                    for record in hidden
                )
                if present or conflicts:
                    reasons=[]
                    if present:
                        reasons.append(
                            f"{present:,} already {self.target_location}"
                        )
                    if conflicts:
                        reasons.append(
                            f"{conflicts:,} conflict{'s' if conflicts!=1 else ''} "
                            "needing review"
                        )
                    message=(
                        "No eligible matches. "
                        +"; ".join(reasons)
                        +". The exact searched tag status and next action are "
                        "shown above; Availability views browse other results "
                        "in the same state."
                    )
            self._empty_label=QLabel(message,self.container)
            self._empty_label.setObjectName("mutedLabel")
            self._empty_label.setWordWrap(True)
            widgets.append(self._empty_label)
        else:
            self._empty_label=None
        self.flow.replace_widgets(widgets); self._buttons=buttons
        self._selected=[
            chip.value for chip in buttons
            if chip.isEnabled() and normalize_tag(chip.value) in selected_keys
        ]
        self._anchor=next((
            index for index,chip in enumerate(buttons)
            if normalize_tag(chip.value)==anchor_key
        ),-1)
        self._refresh_selection()
        status=f"Showing {len(shown):,} of {len(records):,}"
        self.count.setText(status); self.count.setAccessibleDescription(status)
        QAccessible.updateAccessibility(QAccessibleEvent(self.count,QAccessible.Event.DescriptionChanged))
        self.more.setVisible(len(shown)<len(records)); self.container.updateGeometry()
        availability=str(self.availability.currentText()).split(" (",1)[0]
        main=str(self.main.currentText()).split(" (",1)[0]
        if self.shared_root_category is not None:
            main=f"{main} • {self.shared_root_category.currentText()}"
        child=str(self.child.currentText()).split(" (",1)[0]
        category=main if child=="All child categories" else f"{main} / {child}"
        self.filter_summary.setText(f"{availability} • {category}")
        if self.shared_root_category is not None:
            active=sum((self.availability.currentData()!="all",self.main.currentData()!="__all__",bool(self.child.currentData())))
            self.filter_toggle.setText(f"Filters ({active})" if active else "Filters…")
        self._refresh_query_outcome()
        focused=None
        if focused_key is not None:
            focused=next((
                chip for chip in buttons
                if (str(chip.stable_id),chip.value)==focused_key
            ),None)
            if focused is not None:
                focused.setFocus(Qt.OtherFocusReason)
        self._schedule_view_restore(scroll,focused)

    def _schedule_view_restore(self,scroll,focus=None):
        """Coalesce deferred geometry recovery into this palette's lifetime."""
        self._restore_intent=(int(scroll),focus)
        self._restore_timer.start(0)

    def _restore_view(self):
        intent=self._restore_intent; self._restore_intent=None
        if intent is None:return
        scroll,focus=intent
        self.scroll.verticalScrollBar().setValue(scroll)
        if focus is not None and focus in self._buttons:
            focus.setFocus(Qt.OtherFocusReason)
            self.scroll.ensureWidgetVisible(focus)

    def _exact_query_records(self):
        query=normalize_tag(self.search.text())
        if not query or self.knowledge is None:
            return []
        return list(self._exact_identity_index.get(query,()))

    def _refresh_query_outcome(self):
        raw=self.search.text().strip()
        self._query_outcome_kind=""; self._query_outcome_value=""
        if not raw:
            self.query_outcome.hide(); self.query_outcome_action.hide()
            self.updateGeometry(); return
        matches=self._exact_query_records()
        if len(matches)>1:
            names=", ".join(sorted(record.display for record in matches)[:3])
            self.query_outcome.setText(
                f'Ambiguous exact alias “{raw}” matches {len(matches):,} reusable '
                f'tags: {names}. Refine the name before adding.'
            )
            self.query_outcome.setAccessibleDescription(self.query_outcome.text())
            self.query_outcome.show(); self.query_outcome_action.hide()
            self.updateGeometry(); return
        if not matches:
            self.query_outcome.setText(
                f'No reusable exact match for “{raw}”. Creation will still '
                'review aliases, ignored names, and removed identities.'
            )
            self._query_outcome_kind="create"; self._query_outcome_value=raw
            self.query_outcome_action.setText("Create New Tag…")
        else:
            record=matches[0]; value=record.display; key=normalize_tag(value)
            category=self.categories.path_label(record.category)
            if key in self.present:
                state="Already on This Image" if self.target_label == "image" else f"Already in This {self.target_label.title()}"
                self._query_outcome_kind="present"
                self.query_outcome_action.setText("Show Selected Tag" if self.target_label != "image" else "Show Confirmed Tag")
            elif key in self.conflicts:
                state="Conflict Review Required"
                self._query_outcome_kind="conflict"
                self.query_outcome_action.setText("Show Conflict")
            else:
                state="Eligible to Add"
                self._query_outcome_kind="add"
                self.query_outcome_action.setText(f"Add to This {self.target_label.title()}")
            self._query_outcome_value=value
            self.query_outcome.setText(f"{state} — {value} · {category}")
        description=self.query_outcome.text()+" "+self.query_outcome_action.text()
        self.query_outcome.setAccessibleDescription(description)
        self.query_outcome_action.setAccessibleName(description)
        self.query_outcome.show(); self.query_outcome_action.show()
        self.updateGeometry()

    def _activate_query_outcome(self):
        kind=self._query_outcome_kind; value=self._query_outcome_value
        if not kind or not value:return
        if kind=="present":
            self.show_present_requested.emit(value)
        elif kind=="conflict":
            index=self.availability.findData("conflicts")
            if index>=0:self.availability.setCurrentIndex(index)
            QTimer.singleShot(0,lambda:self.focus_value(value))
        elif kind=="add":
            self.tag_requested.emit(value)
        elif kind=="create":
            self.create_requested.emit(value)

    def focus_value(self,value):
        key=normalize_tag(value)
        button=next((item for item in self._buttons if normalize_tag(item.property("tagValue"))==key),None)
        if button is not None and button.isEnabled():
            button.setFocus(Qt.OtherFocusReason); self.scroll.ensureWidgetVisible(button)

    def focus_after_added(self,value):
        key=normalize_tag(value); index=next((i for i,item in enumerate(self._buttons) if normalize_tag(item.property("tagValue"))==key),-1)
        candidates=(self._buttons[index+1:]+list(reversed(self._buttons[:index]))) if index>=0 else self._buttons
        target=next((item for item in candidates if item.isEnabled()),None)
        if target is None:self.search.setFocus(Qt.OtherFocusReason)
        else:target.setFocus(Qt.OtherFocusReason); self.scroll.ensureWidgetVisible(target)

    def _select(self,value,modifiers):
        index=next((
            position for position,chip in enumerate(self._buttons)
            if normalize_tag(chip.value)==normalize_tag(value)
        ),-1)
        modifiers=Qt.KeyboardModifiers(modifiers)
        if index<0 or not self._buttons[index].isEnabled():return
        if modifiers&Qt.ShiftModifier and self._anchor>=0:
            first,last=sorted((self._anchor,index))
            self._selected=[
                chip.value for chip in self._buttons[first:last+1]
                if chip.isEnabled()
            ]
        elif modifiers&Qt.ControlModifier:
            if value in self._selected:self._selected.remove(value)
            else:self._selected.append(value)
            self._anchor=index
        else:
            self._selected=[value]; self._anchor=index
        self._refresh_selection()

    def _refresh_selection(self):
        selected={normalize_tag(value) for value in self._selected}
        for chip in getattr(self,"_buttons",()):
            chip.set_selected(normalize_tag(chip.value) in selected)
        self.selection_changed.emit(tuple(self._selected))

    def selected_values(self):return tuple(self._selected)

    def clear_selection(self):
        self._selected=[]; self._anchor=-1; self._refresh_selection()

    def _drag_values(self,value):
        return tuple(self._selected if value in self._selected else (value,))

    def _show_more(self): self.limit+=self.PAGE_SIZE; self.refresh()
    def choice_actions_menu(self,control,value,favorite=None):
        if favorite is None:
            favorite=normalize_tag(value) in {
                normalize_tag(item) for item in self.favorites
            }
        menu=QMenu(control)
        add=menu.addAction(f"Add to This {self.target_label.title()}")
        add.setEnabled(control.isEnabled())
        add.triggered.connect(lambda:self.tag_requested.emit(value))
        menu.addSeparator()
        favorite_action=menu.addAction(
            "Remove from Favorites" if favorite else "Add to Favorites"
        )
        favorite_action.triggered.connect(
            lambda:self.favorite_requested.emit(value,not favorite)
        )
        return menu

    def _favorite_menu(self,control,position,value,favorite=None):
        self.choice_actions_menu(control,value,favorite).exec(
            control.mapToGlobal(position)
        )
