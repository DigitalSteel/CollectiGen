"""Compact shared commands and local, context-ranked command discovery."""
from __future__ import annotations

from dataclasses import dataclass,field
from typing import Callable

from shiboken6 import isValid
from PySide6.QtCore import QEvent,QObject,QPoint,QSize,Qt,QTimer,Signal
from PySide6.QtGui import QAction,QCursor,QKeySequence,QShortcut
from PySide6.QtWidgets import (
    QAbstractButton,QDialog,QDialogButtonBox,QFrame,QHBoxLayout,QLabel,QLineEdit,
    QListWidget,QListWidgetItem,QMenu,QScrollArea,QSizePolicy,QTabBar,QToolButton,
    QVBoxLayout,QWidget,
)

from core.command_search import SearchEntry,rank_commands
from .command_icons import command_icon,icon_for_label


@dataclass
class UiCommand:
    key: str
    label: str
    group: str
    callback: Callable[[],None] | None=None
    source: QAction | QAbstractButton | None=None
    keywords: tuple[str,...]=()
    description: str=""
    context: str=""
    enabled: Callable[[],bool] | None=None
    reason: str="Unavailable in the current workspace or selection."
    reveal: bool=False
    search_scope: bool=False
    children: list["UiCommand"]=field(default_factory=list)
    section: str=""

    def available(self):
        if self.enabled is not None and not self.enabled():return False
        if self.source is not None:
            if not isValid(self.source) or not self.source.isEnabled():return False
            if isinstance(self.source,QAction):return self.source.isVisible()
            return not self.source.isHidden()
        return True

    def menu(self):
        return self.source.menu() if self.source is not None and hasattr(self.source,"menu") else None

    def detail(self):
        text=self.description
        if not text and self.source is not None and isValid(self.source):
            text=self.source.toolTip()
        return text or self.label

    def invoke(self):
        # Revalidate immediately before dispatch; search is never write authority.
        if not self.available():return False
        if self.callback is not None:self.callback()
        elif self.menu() is not None:
            self._popup=_guarded_menu(self,self.menu(),self.menu().parentWidget())
            self._popup.aboutToHide.connect(self._popup.deleteLater)
            self._popup.popup(QCursor.pos())
        elif isinstance(self.source,QAction):self.source.trigger()
        elif self.source is not None:self.source.click()
        return True


def _guarded_menu(command,source_menu,parent):
    """Presentation-only menu; every leaf dispatch rechecks original authority."""
    menu=QMenu(parent)

    def populate():
        menu.clear()
        if not command.available():
            QTimer.singleShot(0,menu.hide);return
        source_menu.aboutToShow.emit()
        for number,source in enumerate(source_menu.actions()):
            if not source.isVisible():continue
            if source.isSeparator():menu.addSeparator();continue
            child=UiCommand(f"{command.key}:popup:{number}",source.text(),command.group,
                source=source,context=command.context,enabled=command.available)
            action=menu.addAction(source.icon() if not source.icon().isNull() else icon_for_label(source.text()),source.text())
            action.setEnabled(child.available());action.setToolTip(source.toolTip())
            action.setCheckable(source.isCheckable());action.setChecked(source.isChecked())
            if source.menu() is not None:action.setMenu(_guarded_menu(child,source.menu(),menu))
            else:action.triggered.connect(lambda _checked=False,c=child:c.invoke())
    menu.aboutToShow.connect(populate)
    return menu


def bind_source(page,key,source,group,*,label=None,move=False,keywords=(),description="",context=""):
    """Route to the original control; optionally retire its former visible home."""
    if label is None:
        text=source.text() or source.accessibleName()
        label=text.replace("&&","\0").replace("&","").replace("\0","&")
    if move and isinstance(source,QAbstractButton) and not source.property("commandBarOwned"):
        source._command_original_parent=source.parentWidget()
        holder=getattr(page,"_command_source_holder",None)
        if holder is None:
            holder=QWidget(page);holder.setObjectName("retainedCommandSources");holder.hide()
            page._command_source_holder=holder
        explicitly_visible=not source.isHidden()
        source.setParent(holder);source.setVisible(explicitly_visible)
        source.setProperty("commandBarOwned",True)
    original_parent=getattr(source,"_command_original_parent",None)
    enabled=(lambda p=original_parent:isValid(p) and p.isEnabled()) if original_parent is not None else None
    return UiCommand(key,label,group,source=source,keywords=tuple(keywords),
                     description=description,context=context,enabled=enabled)


class CommandSearchDialog(QDialog):
    """A bounded palette indexes registered UI labels, never library content."""
    def __init__(self,commands,context,parent=None,query=""):
        super().__init__(parent)
        self.setWindowTitle("Find Commands and Settings")
        self.setWindowModality(Qt.WindowModal)
        self.resize(700,480)
        self.commands={command.key:command for command in commands}
        self.context=context
        self.entries=[SearchEntry(c.key,c.label,c.group,c.keywords,c.context)
                      for c in self.commands.values()]
        layout=QVBoxLayout(self)
        self.search=QLineEdit();self.search.setPlaceholderText("Find commands or settings…")
        self.search.setAccessibleName("Search commands and settings")
        self.search.setClearButtonEnabled(True);layout.addWidget(self.search)
        self.scope=QLabel(f"Prioritizing {context} · commands and settings only")
        self.scope.setObjectName("mutedLabel");layout.addWidget(self.scope)
        self.results=QListWidget();self.results.setWordWrap(True)
        self.results.setAccessibleName("Matching commands and settings")
        layout.addWidget(self.results,1)
        self.detail=QLabel();self.detail.setWordWrap(True);layout.addWidget(self.detail)
        self.buttons=QDialogButtonBox(QDialogButtonBox.Close)
        self.open_button=self.buttons.addButton("Open",QDialogButtonBox.ActionRole)
        self.open_button.clicked.connect(self.activate_current)
        self.buttons.rejected.connect(self.reject);layout.addWidget(self.buttons)
        self.search.textChanged.connect(self.refresh_results)
        self.search.returnPressed.connect(self.activate_current)
        self.results.itemActivated.connect(self.activate_current)
        self.results.currentItemChanged.connect(self._selected)
        self.search.installEventFilter(self)
        self.search.setText(query);self.refresh_results();self.search.setFocus()

    def eventFilter(self,watched,event):
        if watched is self.search and event.type()==QEvent.KeyPress and event.key() in (Qt.Key_Down,Qt.Key_Up):
            count=self.results.count()
            if count:self.results.setCurrentRow(max(0,min(count-1,self.results.currentRow()+(1 if event.key()==Qt.Key_Down else -1))))
            return True
        return super().eventFilter(watched,event)

    def refresh_results(self,*_args):
        self.results.clear()
        scopes=[c for c in self.commands.values() if c.search_scope] if self.search.text().strip() else []
        matches=rank_commands([e for e in self.entries if not self.commands[e.key].search_scope],self.search.text(),context=self.context,limit=30-len(scopes))
        matches.extend(SearchEntry(c.key,c.label) for c in scopes)
        for entry in matches:
            command=self.commands[entry.key]
            location=" › ".join(part for part in (command.context,command.group) if part)
            kind="Search workspace" if command.search_scope else "Location" if command.reveal else "Action"
            text=f"{command.label}\n{kind} · {location}"
            if not command.available():text+=" · Unavailable"
            item=QListWidgetItem(icon_for_label(command.label),text)
            item.setData(Qt.UserRole,command.key)
            item.setToolTip(command.detail() if command.available() else command.reason)
            self.results.addItem(item)
        if self.results.count():self.results.setCurrentRow(0)
        else:
            self.detail.setText("Type a command or setting name to begin." if not self.search.text().strip() else "No matches. Try a shorter phrase or a related word.")
            self.open_button.setEnabled(False)

    def _selected(self,item,*_args):
        command=self.commands.get(item.data(Qt.UserRole)) if item else None
        enabled=bool(command and command.available())
        self.open_button.setEnabled(enabled)
        if command:
            self.open_button.setText("Show Location" if command.reveal else "Open")
            self.detail.setText(command.detail() if enabled else command.reason)

    def activate_current(self,*_args):
        item=self.results.currentItem()
        command=self.commands.get(item.data(Qt.UserRole)) if item else None
        if command is None or not command.available():
            self._selected(item);return
        # Close first so an existing modal action/reveal handler retains focus.
        self.accept()
        QTimer.singleShot(0,command.invoke)


class CommandBar(QWidget):
    """One tab strip and one command row; excess actions share a named menu."""
    searchRequested=Signal()
    groupChanged=Signal(str)

    def __init__(self,parent=None):
        super().__init__(parent)
        self.setObjectName("commandBar")
        self.setAccessibleName("Workspace command bar")
        self._commands=[];self._buttons=[];self._section_separators={};self._remembered={};self._context=""
        self._group="";self._signature=();self.visible_limits={}
        layout=QVBoxLayout(self);layout.setContentsMargins(10,2,10,6);layout.setSpacing(2)
        top=QHBoxLayout();top.setSpacing(10)
        self.tabs=QTabBar();self.tabs.setExpanding(False);self.tabs.setDrawBase(False)
        self.tabs.setUsesScrollButtons(True);self.tabs.setAccessibleName("Command groups")
        top.addWidget(self.tabs,1)
        self.search_button=QToolButton();self.search_button.setText("Find Commands…")
        self.search_button.setIcon(command_icon("search"));self.search_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.search_button.setToolTip("Find commands and settings (Ctrl+K). Results prioritize this workspace.")
        self.search_button.setAccessibleName("Find commands and settings")
        self.search_button.clicked.connect(self.searchRequested);top.addWidget(self.search_button)
        layout.addLayout(top)
        self.row_widget=QWidget();self.row_widget.setObjectName("commandRow")
        # The visible command set must not become the dialog's minimum width:
        # _fit moves commands into More Commands after the parent is sized.
        self.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        self.row_widget.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        self.row=QHBoxLayout(self.row_widget);self.row.setContentsMargins(5,2,5,2);self.row.setSpacing(4)
        self.scope=QLabel();self.scope.setObjectName("commandContext");self.scope.setAccessibleName("Command context")
        self.scope.setContentsMargins(8,0,8,0)
        scope_font=self.scope.font();scope_font.setBold(True);self.scope.setFont(scope_font)
        self.scope_separator=QFrame();self.scope_separator.setObjectName("commandContextSeparator")
        self.scope_separator.setFixedSize(1,22)
        self.overflow=QToolButton();self.overflow.setText("More Commands")
        self.overflow.setIcon(command_icon("more"));self.overflow.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.overflow.setPopupMode(QToolButton.InstantPopup);self.overflow_menu=QMenu(self.overflow);self.overflow.setMenu(self.overflow_menu)
        layout.addWidget(self.row_widget)
        self.tabs.currentChanged.connect(self._change_group)
        self._state_timer=QTimer(self);self._state_timer.setInterval(200)
        self._state_timer.timeout.connect(self.refresh_state);self._state_timer.start()
        shortcut=QShortcut(QKeySequence("Ctrl+K"),self);shortcut.activated.connect(self.searchRequested)
        self._search_shortcut=shortcut

    def set_commands(self,context,commands,*,groups=None):
        self._context=context;self._commands=list(commands)
        explicit_groups=groups is not None
        groups=list(dict.fromkeys(groups if explicit_groups else (command.group for command in self._commands)))
        for command in self._commands:
            if command.group not in groups:groups.append(command.group)
        preferred=["File","Home","Import","Organize","Prepare","Training","Generate","Review","Resolution","Knowledge","Rules","Settings","View","Advanced","Help"]
        if not explicit_groups:
            groups.sort(key=lambda group:preferred.index(group) if group in preferred else len(preferred))
        active=self._remembered.get(context,"Home")
        if active not in groups:active=groups[0] if groups else ""
        self.tabs.blockSignals(True)
        while self.tabs.count():self.tabs.removeTab(0)
        for group in groups:
            index=self.tabs.addTab(group.replace("&","&&"))
            self.tabs.setTabData(index,group)
        self.tabs.setCurrentIndex(groups.index(active) if active else -1)
        self.tabs.blockSignals(False)
        self._group=active;self._build_row()

    def _change_group(self,index):
        if index<0:return
        self._group=self.tabs.tabData(index);self._remembered[self._context]=self._group;self._build_row()
        self.groupChanged.emit(self._group)

    def select_group(self,group):
        """Select a registered group without invoking any command."""
        if not self.tabs.isEnabled():return False
        for index in range(self.tabs.count()):
            if self.tabs.tabData(index)==group:
                self.tabs.setCurrentIndex(index)
                return True
        return False

    def _button(self,command):
        button=QToolButton();button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        button.setIconSize(QSize(18,18));button.setIcon(icon_for_label(command.label))
        button.setText(command.label);button.setAccessibleName(command.label)
        button.setMinimumHeight(34)
        button.setEnabled(command.available())
        menu=command.menu()
        if menu is not None:
            button.setMenu(_guarded_menu(command,menu,button));button.setPopupMode(QToolButton.InstantPopup)
        else:button.clicked.connect(command.invoke)
        if command.source is not None and hasattr(command.source,"isCheckable"):
            button.setCheckable(command.source.isCheckable())
            button.setChecked(command.source.isChecked())
        return button

    def _build_row(self):
        while self.row.count():
            item=self.row.takeAt(0);widget=item.widget()
            if widget is not None:
                widget.hide()
                if widget not in (self.scope,self.scope_separator,self.overflow):widget.deleteLater()
        self._buttons=[]
        self._section_separators={}
        self.row.addWidget(self.scope);self.row.addWidget(self.scope_separator)
        self.row.addSpacing(6)
        self.scope.setText(self._context);self.scope.show();self.scope_separator.show()
        for command in self._commands:
            if command.group!=self._group:continue
            if command.section:
                divider=QFrame();divider.setObjectName("commandSectionSeparator");divider.setFixedSize(1,22)
                self.row.addWidget(divider);self._section_separators[command.key]=divider
            button=self._button(command);self.row.addWidget(button);self._buttons.append((command,button))
        self.row.addWidget(self.overflow);self.row.addStretch(1)
        self.row_widget.setVisible(bool(self._buttons))
        self._signature=();self.refresh_state();self._fit()

    def refresh_state(self):
        if not self.isVisible():return
        for command,button in self._buttons:
            if button.text()!=command.label:
                button.setText(command.label);button.setAccessibleName(command.label)
                button.setIcon(icon_for_label(command.label))
            enabled=command.available();button.setEnabled(enabled)
            button.setToolTip(command.detail() if enabled else command.reason)
            button.setAccessibleDescription(button.toolTip())
            if button.isCheckable() and command.source is not None and isValid(command.source):
                button.setChecked(command.source.isChecked())
        self._fit()

    def _fit(self):
        # Bounded width accounting; never squeeze labels or grow a second row.
        available=max(0,self.row_widget.width()-self.scope.sizeHint().width()-self.scope_separator.width()-46)
        widths=[button.sizeHint().width()+self.row.spacing() for _,button in self._buttons]
        section_width=1+self.row.spacing()
        last_section="";total=sum(widths)
        for command,_button in self._buttons:
            if command.section and last_section and command.section!=last_section:total+=section_width
            last_section=command.section
        limit=self.visible_limits.get(self._group)
        overflow=total>available or (limit is not None and len(self._buttons)>limit)
        if overflow:available-=self.overflow.sizeHint().width()+self.row.spacing()
        hidden=[];used=0;last_section="";visible_count=0
        for (command,button),width in zip(self._buttons,widths):
            separated=bool(command.section and last_section and command.section!=last_section)
            required=width+(section_width if separated else 0)
            fits=used+required<=available and (limit is None or visible_count<limit)
            button.setVisible(fits)
            divider=self._section_separators.get(command.key)
            if divider is not None:divider.setVisible(fits and separated)
            if fits:used+=required;last_section=command.section;visible_count+=1
            else:hidden.append(command)
        self.overflow.setVisible(bool(hidden))
        signature=tuple((c.key,c.label,c.section,c.available()) for c in hidden)
        if signature!=self._signature:
            self.overflow_menu.clear()
            previous_section=""
            for command in hidden:
                if command.section and previous_section and command.section!=previous_section:self.overflow_menu.addSeparator()
                action=self.overflow_menu.addAction(icon_for_label(command.label),command.label)
                previous_section=command.section
                action.setEnabled(command.available());action.setToolTip(command.detail())
                if command.menu() is not None:action.setMenu(_guarded_menu(command,command.menu(),self.overflow_menu))
                else:action.triggered.connect(command.invoke)
            self._signature=signature
        self.overflow.setAccessibleName(f"More {self._group} commands")

    def resizeEvent(self,event):
        super().resizeEvent(event);self._fit()


class DialogCommandController(QObject):
    """Dialog-owned ribbon; search reveals local commands and never executes them.

    The owning dialog supplies existing commands, section order and a section
    reveal callback. It remains responsible for drafts and busy/selection gates.
    No main-window registry, settings search or library content is consulted.
    """
    def __init__(self,dialog,context,commands,*,groups=None,on_group_changed=None,initial_group=None,
                 visible_limits=None):
        super().__init__(dialog)
        self.dialog=dialog;self.context=context;self.commands=list(commands)
        self._search_dialog=None;self._on_group_changed=on_group_changed
        self.bar=CommandBar(dialog)
        self.bar.visible_limits=dict(visible_limits or {})
        dialog.layout().insertWidget(0,self.bar)
        for command in self.commands:
            prior_gate=command.enabled
            command.enabled=lambda gate=prior_gate:self.bar.tabs.isEnabled() and (gate is None or gate())
        self.bar.set_commands(context,self.commands,groups=groups)
        if initial_group is not None:self.bar.select_group(initial_group)
        self.bar.groupChanged.connect(self._reveal_group)
        self.bar.searchRequested.connect(self.open_search)
        self.bar.search_button.setToolTip("Find commands in this window (Ctrl+K). Open reveals the command without running it.")
        self._reveal_group(self.bar._group)

    def _reveal_group(self,group):
        if self._on_group_changed is not None:self._on_group_changed(group)

    def select_group(self,group):
        return self.bar.select_group(group)

    def _reveal_command(self,command):
        if not self.select_group(command.group):return
        # Recheck after section activation: a reveal may update selection gates.
        self.bar.refresh_state()
        for candidate,button in self.bar._buttons:
            if candidate.key==command.key:
                target=button if button.isVisible() else self.bar.overflow
                if not target.isEnabled():target=self.bar.tabs
                target.setFocus(Qt.ShortcutFocusReason)
                return

    def search_commands(self):
        available=lambda:self.bar.tabs.isEnabled()
        sections=[UiCommand(f"dialog:section:{index}",group,"Sections",
            callback=lambda g=group:self.select_group(g),context=self.context,
            description=f"Show {group} in this window. No work will start.",
            enabled=available,reason="Wait for current work to finish.",reveal=True)
            for index in range(self.bar.tabs.count())
            for group in (self.bar.tabs.tabData(index),)]
        return sections+[UiCommand(f"dialog:find:{command.key}",command.label,command.group,
            callback=lambda c=command:self._reveal_command(c),context=self.context,
            keywords=command.keywords,description="Show this command in its section without running it. "+command.detail()+
                (" "+command.reason if not command.available() else ""),
            enabled=lambda c=command:available() and (c.source is None or isValid(c.source)),
            reason="Wait for current work to finish or reopen this window.",reveal=True)
            for command in self.commands]

    def open_search(self,*_args,query=""):
        if self._search_dialog is not None and isValid(self._search_dialog) and self._search_dialog.isVisible():
            self._search_dialog.raise_();self._search_dialog.search.setFocus();return
        dialog=CommandSearchDialog(self.search_commands(),self.context,self.dialog,query=query)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.scope.setText(f"{self.context} only · Open reveals a command; it does not run it")
        self._search_dialog=dialog
        dialog.show();dialog.search.setFocus()
