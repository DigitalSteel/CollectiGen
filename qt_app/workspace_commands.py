"""Explicit command homes over existing workspace controls and settings routes."""
from __future__ import annotations

from PySide6.QtCore import QObject,QTimer,Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QAbstractButton,QMenu,QToolButton,QWidget

from .command_bar import CommandBar,CommandSearchDialog,UiCommand,bind_source
from .components import PageHeader
from .secondary_workspace_commands import secondary_commands


class WorkspaceCommandController(QObject):
    def __init__(self,window):
        super().__init__(window)
        self.window=window;self._page_commands={};self._search_dialog=None
        self.bar=CommandBar(window);window.command_bar=self.bar
        window.centralWidget().layout().insertWidget(0,self.bar)
        self.file_actions=[];self.help_actions=[]
        for action in window.menuBar().actions():
            menu=action.menu()
            if menu is None:continue
            bucket=self.file_actions if action.text()=="File" else self.help_actions
            for child in menu.actions():
                if not child.isSeparator():bucket.append(child);window.addAction(child)
        window.menuBar().hide()
        self.bar.searchRequested.connect(self.open_search)
        window.stack.currentChanged.connect(self.refresh)
        self.refresh()

    def _global_commands(self,index):
        commands=[]
        for number,action in enumerate(self.file_actions):
            label=action.text();group="File"
            if label=="Refresh Libraries":
                if index in (1,2):continue
                group="Home"
            if label=="Plan Image Generation…":
                continue # Generate owns the primary start; contextual Dataset handoffs remain.
            commands.append(UiCommand(f"app:file:{number}",label,group,source=action,
                                      keywords=("libraries","folders") if number==0 else (),context="Application"))
        for number,action in enumerate(self.help_actions):
            label=action.text()
            if label.startswith("What's New in "):label="What's New"
            commands.append(UiCommand(f"app:help:{number}",label,"Help",source=action,
                                      keywords=("guide","instructions","support") if number==0 else (),context="Application"))
        return commands

    def _page_bindings(self,page,index):
        cached=self._page_commands.get(index)
        if cached is not None and cached[0] is page:return cached[1]
        header=page.findChild(PageHeader)
        if header is None:return [] # Preserve lazy loading and its named shell.
        context=self.window.PAGE_NAMES[index];commands=[]

        def add(attr,group,*,label=None,keywords=(),move=True):
            source=getattr(page,attr,None)
            if source is not None:
                commands.append(bind_source(page,f"{index}:{attr}",source,group,
                    label=label,move=move,keywords=(source.text(),*keywords) if label else keywords,context=context))

        def add_summary_toggle():
            source=page.summary_visibility_button
            command=bind_source(page,f"{index}:summary_visibility_button",source,"View",
                move=True,keywords=("show summary","hide summary","inspector","sidebar"),context=context)
            def update_label(hidden):
                command.label="Show Summary" if hidden else "Hide Summary"
            update_label(source.isChecked())
            source.toggled.connect(update_label)
            commands.append(command)

        if index==1:
            add("add_images_button","Home",keywords=("add images","import","files","folder"))
            add("video_ingest_button","View",keywords=("video","clip","playback","lineage","media","mixed","unsupported"))
            add("refresh_button","Home",keywords=("refresh","scan","incoming"))
            add("import_external_evidence_button","Import",keywords=("metadata","txt","sidecar","evidence"))
            add_summary_toggle()
        elif index==2:
            add("media_library_button","View",keywords=("media","images","video","mixed","unsupported"))
            # Retain the existing refresh signal and exact library scope.
            for position in range(header.actions.count()):
                source=header.actions.itemAt(position).widget()
                if isinstance(source,QAbstractButton) and source.text()=="Refresh":
                    commands.append(bind_source(page,"2:refresh",source,"Home",label="Refresh Libraries",move=True,context=context))
                    break
            add_summary_toggle()
            library=page.library_actions
            for number,action in enumerate(library.menu().actions()):
                if action.isSeparator():continue
                label=action.text()
                group="Import" if action is page.import_external_evidence_action else "Organize"
                keywords=("duplicates","duplicate","similarity","similar") if action is page.find_duplicates_action else ()
                if action is page.import_external_evidence_action:keywords=("metadata","txt","sidecar","evidence")
                title={"Find Duplicate and Similar Images…":"Find Duplicates…",
                       "Audit Folder Organization…":"Audit Folders…",
                       "Open Problems Folder…":"Problems Folder…"}.get(label,label)
                commands.append(UiCommand(f"2:library:{number}",title,group,source=action,
                                           description=action.toolTip() or label,
                                           context=context,keywords=(label,*keywords),
                                           enabled=lambda source=library:source.isEnabled()))
            # The dropdown's members now have named command homes.
            bind_source(page,"2:library_source",library,"Organize",move=True)
            for attr,label in (("concept_groups_button","Training Collections…"),("concept_add_button","Add to Collection…")):
                add(attr,"Training",label=label,keywords=("concept","collection","training","create"))
            for attr in ("training_resume_button","training_rules_button"):
                add(attr,"Training",
                    keywords=("training","caption","dataset","prepare"))
            page.training_rules_button.setToolTip("Review how caption rules affect selected images or current matching results before exporting training captions.")
            page.training_resume_button.setToolTip("Open Training to continue saved plans, review resources and inspect runs. Dataset keeps your selection and filters.")
            sections={"training_resume_button":"Workspace",
                      "training_rules_button":"Captions","concept_groups_button":"Collections","concept_add_button":"Collections"}
            for command in commands:
                for attr,section in sections.items():
                    if command.source is getattr(page,attr):command.section=section
            for attr in ("generation_selection_button","generation_new_button"):
                add(attr,"Generate",keywords=("generate","generation","plan","images"))
            page.generation_new_button.setToolTip("Open Generate to continue image or video drafts and review results. Dataset keeps your selection and filters.")
            page.concept_groups_button.setToolTip("Create a collection, add Dataset images, then build training data for your goal and model. Each plan freezes its image list.")
            for command in commands:
                if command.source is page.generation_selection_button:
                    command.reason="Select 1–100 images and wait for current work or search to finish."
            # Planning entry points now have one ribbon home. Keep the original
            # controls and scope labels alive for their existing state updates.
            tabs=page.workflow_tabs
            if tabs.currentWidget() in (page.training_goal_page,page.generation_goal_page):
                tabs.setCurrentWidget(page.browse_workflow)
            for goal in (page.training_goal_page,page.generation_goal_page):
                tabs.removeTab(tabs.indexOf(goal))
        elif index==8:
            for attr,group in (("overview_button","Training"),("models_button","Resources"),
                               ("setup_button","Resources"),("package_button","Training"),
                               ("package_recovery_button","Training"),
                               ("history_button","History")):
                add(attr,group)
        else:
            commands.extend(secondary_commands(self.window,page,index))
            if index==4:
                for attr,group,keywords in (
                    ("knowledge_history_button","View",("history","back","forward","recent views")),
                ):
                    source=getattr(page,attr,None)
                    if source is not None:
                        source.setProperty("commandSearchMenuOnly",True)
                        add(attr,group,keywords=keywords)
        if index in (1,2):
            add("history_button","History",label="History && Versions…",
                keywords=("saved changes","versions","restore","undo","history"))
            resolution=page.resolution_filter
            review=bind_source(page,f"{index}:resolution_review",resolution.review_button,
                "Resolution",move=True,context=context,
                keywords=("resolution","resize","upscale","downscale","dimensions","policy"),
                description="Review selected images, or current filtered results, and choose resolution or resizing options.")
            commands.append(review)
        navigator=getattr(page,"settings_navigation",None)
        if navigator is not None:
            # The section list stays beside its editors. Search has one home.
            navigator.search.hide()
        header.set_title_visible(False)
        page.setAccessibleName(context)
        self._page_commands[index]=(page,commands)
        return commands

    def refresh(self,*_args):
        index=self.window.stack.currentIndex()
        if index<0:return
        page=self.window.stack.currentWidget()
        commands=self._global_commands(index)+self._page_bindings(page,index)
        extra=getattr(self.window,"workspace_extra_commands",None)
        if callable(extra):commands+=extra(index)
        commands=self._apply_window_policy(commands)
        order=getattr(self.window, "WORKSPACE_COMMAND_GROUPS", None)
        groups=None if order is None else [group for group in order if any(c.group==group for c in commands)]
        self.bar.set_commands(self.window.PAGE_NAMES[index],commands,groups=groups)

    def _apply_window_policy(self, commands):
        policy = getattr(self.window, "workspace_command_policy", None)
        if policy is None:
            return commands
        return [result for command in commands if (result := policy(command)) is not None]

    def _setting_commands(self):
        commands=[]
        for index,attribute in ((5,"automation_page"),(6,"setup_page")):
            page=getattr(self.window,attribute,None)
            navigator=getattr(page,"settings_navigation",None)
            if navigator is None:continue
            for number,destination in enumerate(navigator.destinations()):
                if destination.control is not None and destination.control.property("commandBarOwned"):
                    continue # Its command registration is now the sole search home.
                section=navigator._sections[destination.section].label
                commands.append(UiCommand(f"settings:{index}:{number}",destination.label,section,
                    callback=lambda i=index,n=navigator,d=destination:self._reveal_setting(i,n,d),
                    keywords=destination.keywords,description=f"Show this setting in {self.window.PAGE_NAMES[index]} › {section}. Its value will not change.",
                    context=self.window.PAGE_NAMES[index],reveal=True))
        return commands

    def _reveal_setting(self,index,navigator,destination):
        self.window.navigation.setCurrentRow(index)
        if self.window.stack.currentIndex()!=index:return # Respect pending-edit leave decisions.
        navigator.search.clear()
        navigator.activate_destination(destination)

    def reveal_group(self,index,group):
        """Route to an owning page and group, respecting pending-edit decisions."""
        self.window.navigation.setCurrentRow(index)
        if self.window.stack.currentIndex()!=index:return False
        self.refresh()
        return self.bar.select_group(group)

    def _find_settings(self,index,query):
        self.window.navigation.setCurrentRow(index)
        if self.window.stack.currentIndex()!=index:return
        page=self.window.stack.currentWidget()
        navigator=getattr(page,"settings_navigation",None)
        if navigator is not None:
            # Only user activation materializes the settings page, then expose
            # exact targets in the same palette instead of searching profiles.
            self.open_search(query=query)

    def search_commands(self):
        commands=list(self.bar._commands)
        expanded=[]
        def visit(command,depth=0):
            if depth>3:return
            menu=command.menu()
            if menu is None or (command.source is not None and command.source.property("commandSearchMenuOnly")):
                expanded.append(command);return
            actions=[action for action in menu.actions() if not action.isSeparator()]
            if not actions:expanded.append(command)
            for number,action in enumerate(actions):
                child=UiCommand(f"{command.key}:menu:{number}",action.text(),f"{command.group} › {command.label}",
                    source=action,context=command.context,keywords=command.keywords,
                    enabled=command.available,reason=command.reason)
                visit(child,depth+1)
        for command in commands:visit(command)
        index=self.window.stack.currentIndex();page=self.window.stack.currentWidget()
        # Primary workflow entry/finish controls stay on the canvas. Discovery
        # reveals their exact existing location instead of adding a second home.
        local_primary={1:("organize_button",),3:("conflict_button","safe_button"),4:("knowledge_new_button",)}
        for attr in local_primary.get(index,()):
            source=getattr(page,attr,None)
            if source is None:continue
            expanded.append(UiCommand(f"primary:{index}:{attr}",source.text(),"Main Workspace",
                callback=lambda s=source:self._reveal_primary(s),context=self.window.PAGE_NAMES[index],
                description="Show this primary action in its normal workflow location.",
                keywords=("workflow",),
                enabled=lambda s=source:s.isVisible() and s.isEnabled(),
                reason="Choose the required library, images or workflow stage to use this action.",reveal=True))
        preparation=getattr(page,"preparation_controls",None)
        if preparation is not None:
            expanded.append(UiCommand(f"primary:{index}:prepare","Prepare Images…","Main Workspace",
                callback=lambda:self._reveal_primary(preparation.action_button),
                description="Show preparation beside the image selection. Review its scope before starting.",
                keywords=("prepare","images","analyze","tags","descriptions"),context=self.window.PAGE_NAMES[index],
                enabled=lambda:preparation.action_button.isEnabled() and preparation.action_button.isVisible(),
                reason="Select images in a preparation stage to use this action.",reveal=True))
        if index!=8:
            expanded.append(UiCommand("primary:training","Training","Main Workspace",
                callback=lambda:self.window.open_training_workspace(),context="Training",keywords=("train","dataset","captions","draft","checkpoint"),
                description="Open collections, saved training drafts and run history without starting a job.",reveal=True))
        if index!=7:
            expanded.append(UiCommand("primary:generate","Generate Images","Main Workspace",
                callback=lambda:self.window.navigation.setCurrentRow(7),context="Generate",
                keywords=("generation","prompt","new image","remix"),
                description="Open Generate and continue saved request drafts.",reveal=True))
        expanded.extend(self._setting_commands())
        for index,attribute in ((5,"automation_page"),(6,"setup_page")):
            if getattr(self.window,attribute,None) is None:
                context=self.window.PAGE_NAMES[index]
                expanded.append(UiCommand(f"settings:open:{index}",f"Find Settings in {context}…","Settings",
                    callback=lambda i=index:self._find_settings(i,getattr(self,"_search_query","")),
                    context=context,description=f"Open {context} and continue this search across its settings. No setting values will change.",search_scope=True))
        return self._apply_window_policy(expanded)

    def _reveal_primary(self,source):
        from .settings_navigation import SettingsNavigator
        SettingsNavigator._reveal(source)

    def _show_dataset_goal(self,kind):
        self.window.navigation.setCurrentRow(2)
        if self.window.stack.currentIndex()!=2:return
        page=self.window.dataset_page
        group="Training" if kind=="training" else "Generate"
        index=next((i for i in range(self.bar.tabs.count()) if self.bar.tabs.tabText(i)==group),-1)
        if index>=0:self.bar.tabs.setCurrentIndex(index)

    def open_search(self,*_args,query=""):
        if self._search_dialog is not None and self._search_dialog.isVisible():
            self._search_dialog.raise_();self._search_dialog.search.setFocus();return
        context=self.window.PAGE_NAMES[self.window.stack.currentIndex()]
        dialog=CommandSearchDialog(self.search_commands(),context,self.window,query=query)
        self._search_dialog=dialog
        self._search_query=query
        dialog.search.textChanged.connect(lambda text:setattr(self,"_search_query",text))
        dialog.setAttribute(Qt.WA_DeleteOnClose,True)
        dialog.destroyed.connect(lambda:self._clear_search(dialog))
        dialog.show()

    def _clear_search(self,dialog):
        if self._search_dialog is dialog:self._search_dialog=None
