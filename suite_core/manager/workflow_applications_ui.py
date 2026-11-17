"""Applications-only workflow launch and stock signed installation decisions."""
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget, QLabel, QPushButton, QDialog, QVBoxLayout, QHBoxLayout, QGroupBox

from core.workflow_connection import workflow_component
from core.workflow_installation import WorkflowInstallation
from core.workflow_native_owner import WorkflowWorkspaceOwner
from suite_core.manager.encyclopedia_acquisition_ui import EncyclopediaAcquisitionPanel
from suite_core.manager.resource_executor import ResourceExecutorOwner


WORKFLOW_NAMES = {'collectigen.generate': 'Generate', 'collectigen.train': 'Train'}


class WorkflowAcquisitionPanel(EncyclopediaAcquisitionPanel):
    def __init__(self, component_id, service, executor, **kwargs):
        self.component_id = component_id
        self.label = WORKFLOW_NAMES[component_id]
        super().__init__(service, executor, **kwargs)
        for label in self.findChildren(QLabel):
            if label.text() == 'Tag Encyclopedia':
                label.setText(self.label)
        self.activity.setAccessibleName(self.label + ' setup progress')
        self.health_accept.setToolTip('Open ' + self.label + ', then close it normally. Its original process and work must drain before setup is confirmed.')
        self.recovery_confirm.setText('Restore the previous app state and connection. Keep saved drafts, runs and images.')

    def scope_text(self):
        if getattr(self.service, 'qualification_only', False):
            return 'Qualification build. Review this signed local ' + self.label + ' fixture. This is not a public update; saved workflow data stays separate.'
        if getattr(self.service, 'candidate_admission', None) is not None:
            return 'Evaluate this exact ' + self.label + ' candidate in its isolated suite. This is not a public update; saved workflow data stays separate.'
        return 'Install or update ' + self.label + ' from a verified GitHub release. Review and confirm changes; saved workflow data stays separate.'

    def initial_status_text(self):
        if getattr(self.service, 'qualification_only', False):
            return 'Qualification fixture. Find ' + self.label + ' checks sealed local release evidence. No GitHub download is enabled.'
        if getattr(self.service, 'candidate_admission', None) is not None:
            return 'Evaluation candidate. Find ' + self.label + ' checks the sealed local release evidence. No GitHub download is enabled.'
        return 'Choose Find ' + self.label + ' to review signed releases. Opening this window downloads nothing.'

    def application_name(self):
        return self.label

    def progress_phase_text(self, phase):
        return {
            'Saving the reader connection': 'Saving the ' + self.label + ' connection',
            'Checking the reader download': 'Checking the ' + self.label + ' download',
            'Verifying the downloaded reader': 'Verifying the downloaded ' + self.label,
            'Preparing reader files': 'Preparing ' + self.label + ' files',
            'Installing reader files': 'Installing ' + self.label + ' files',
            'Checking installed reader files': 'Checking installed ' + self.label + ' files',
            'Rechecking reader files before activation': 'Rechecking ' + self.label + ' files before activation',
        }.get(phase, phase)

    def installation_title(self, verb):
        return verb + ' ' + self.label

    def first_check_label(self):
        return 'Find ' + self.label

    def preserved_data_text(self):
        return 'Your saved drafts, runs and images will not be changed.'

    def package_review_name(self):
        return self.label

    def available_version_label(self):
        return 'Available ' + self.label + ': '

    def available_status_text(self, offer):
        return self.label + ' ' + offer.release.software_version + ' is available. Review before confirming Install or Update.'

    def empty_release_status_text(self):
        return 'No compatible newer ' + self.label + ' release was found on this channel.'

    def installation_status_reason(self, result):
        if not result.connection_saved:
            return 'App files are retained. Retry save connection; installation will not repeat.'
        if result.installed.pending or result.installed.connection_pending:
            return 'Setup needs attention. Review its status or the available recovery action.'
        if result.installed.selected is not None and result.installed.phase == 'activated':
            return self.pending_setup_guidance()
        return self.label + ' setup status: ' + result.installed.phase + '. Saved workflow data was preserved.'

    def show_installed_details(self):
        selected = self.installed.selected
        self.details.setPlainText('No managed app is active. Saved workflow data was preserved.' if selected is None else
            self.label + ' ' + selected.version + '\nDirectory: ' + str(selected.directory) + '\nSetup: ' + self.installed.phase)

    def observation_text(self):
        selected = None if self.installed is None else self.installed.selected
        return ('Installation has not been checked.' if self.installed is None else
                'No managed app is installed.' if selected is None else 'Installed ' + self.label + ': ' + selected.version)

    def pending_setup_guidance(self):
        return ('Open ' + self.label + ' from Applications, then close its window normally.'
                if self.service.launch_available else 'App files are saved. Restart Manager to admit the saved app before opening it.')


class WorkflowReturnBar(QGroupBox):
    """Contextual navigation back to the saved app, without starting it."""
    def __init__(self, return_to_applications, parent=None):
        super().__init__('Saved Generate work', parent)
        layout = QHBoxLayout(self)
        note = QLabel('Your Generate work is saved. After reviewing settings, resume it from Applications.')
        note.setWordWrap(True)
        layout.addWidget(note, 1)
        action = QPushButton('Return to Generate in Applications')
        action.clicked.connect(return_to_applications)
        layout.addWidget(action)
        self.hide()


class WorkflowApplications(QWidget):
    def owns_dialog(self, dialog):
        # QWidget.isAncestorOf stops at top-level window boundaries. Native
        # confirmations still retain their exact acquisition parent chain.
        current = dialog
        while current is not None:
            if any(current is owned for owned in self.dialogs.values()):
                return True
            current = current.parentWidget()
        return False

    def __init__(self, *, workspace_owner=None, services=(), launch_allowed=lambda: True, parent=None):
        super().__init__(parent)
        if workspace_owner is not None and type(workspace_owner) is not WorkflowWorkspaceOwner:
            raise ValueError('original_workflow_workspace_owner_required')
        self.owner = workspace_owner
        self.launch_allowed = launch_allowed
        self.rows = {}
        self.services = {}
        self.panels = {}
        self.dialogs = {}
        for component_id, service, executor in services:
            workflow_component(component_id)
            if (component_id in self.services or type(service) is not WorkflowInstallation
                    or service.component.value != component_id
                    or type(executor) is not ResourceExecutorOwner
                    or workspace_owner is None or service.native_owner is not workspace_owner):
                raise ValueError('same_component_original_workflow_service_required')
            self.services[component_id] = service, executor
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for component_id, name in WORKFLOW_NAMES.items():
            group = QGroupBox(name)
            body = QVBoxLayout(group)
            description = QLabel('Create images and videos with saved local setups.' if name == 'Generate' else
                                 'Review training packages, prepare a trainer and resume saved runs.')
            description.setWordWrap(True)
            body.addWidget(description)
            status = QLabel()
            status.setWordWrap(True)
            body.addWidget(status)
            actions = QHBoxLayout()
            open_button = QPushButton('Open ' + name)
            open_button.setProperty('primary', True)
            manage = QPushButton('Manage install and updates…')
            actions.addWidget(open_button)
            actions.addWidget(manage)
            actions.addStretch(1)
            body.addLayout(actions)
            open_button.clicked.connect(lambda _checked=False, selected=component_id: self.open_application(selected))
            manage.clicked.connect(lambda _checked=False, selected=component_id: self.manage(selected))
            layout.addWidget(group)
            self.rows[component_id] = status, open_button, manage, group
            if component_id in self.services:
                service, executor = self.services[component_id]
                dialog = QDialog(self)
                dialog.setWindowTitle('Manage ' + name)
                dialog.resize(1040, 700)
                panel = WorkflowAcquisitionPanel(component_id, service, executor, parent=dialog,
                    return_to_applications=dialog.hide)
                QVBoxLayout(dialog).addWidget(panel)
                self.panels[component_id], self.dialogs[component_id] = panel, dialog
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.render()

    @property
    def close_held(self):
        return ((self.owner is not None and self.owner.close_held)
                or any(panel.close_held for panel in self.panels.values())
                or any(executor.close_held for _service, executor in self.services.values()))

    def open_application(self, component_id):
        workflow_component(component_id)
        if component_id not in self.services or self.owner.close_held or not self.launch_allowed():
            return
        service, _executor = self.services[component_id]
        panel = self.panels[component_id]
        if service.launch_available and not panel.close_held and not panel.closing:
            def work():
                from core.encyclopedia_installation import InstallationResult
                observed = service.observe()
                service.launch(confirmed=True, should_cancel=panel.cancel.is_set)
                return InstallationResult(observed, True, WORKFLOW_NAMES[component_id] +
                    ' is opening. Close its window normally to return to shared management.')
            panel.check_attempted = True
            panel._start('Opening ' + WORKFLOW_NAMES[component_id], work)
        self.render()

    def manage(self, component_id):
        workflow_component(component_id)
        dialog = self.dialogs.get(component_id)
        if dialog is not None and not (self.owner is not None and self.owner.close_held):
            dialog.show()
            dialog.raise_()

    def set_filter(self, query, status, category):
        for component_id, name in WORKFLOW_NAMES.items():
            own_category = 'AI Generation' if component_id == 'collectigen.generate' else 'AI Training'
            available = component_id in self.services
            panel = self.panels.get(component_id)
            installed = panel is not None and panel.installed is not None and panel.installed.selected is not None
            update = panel is not None and panel.offer is not None and panel.offer.current_version is not None
            show_status = (status == 'All entries' or status == 'Installed' and installed
                or status == 'Available' and available and not installed
                or status == 'Updates' and update or status == 'In development' and not available)
            self.rows[component_id][3].setVisible(show_status
                and category in ('All categories', own_category)
                and (not query or query in (name + ' collectigen ' + own_category).casefold()))

    def render(self):
        held = self.owner is not None and self.owner.close_held
        for component_id, (status, opening, manage, _group) in self.rows.items():
            reference = getattr(self.owner, 'failure_reference', None)
            status.setToolTip('Workflow diagnostic reference: ' + reference if reference else '')
            resume = (component_id == 'collectigen.generate' and self.owner is not None
                      and self.owner.pending_generate_draft is not None)
            opening.setText('Resume Generate' if resume else 'Open ' + WORKFLOW_NAMES[component_id])
            pair = self.services.get(component_id)
            if pair is None:
                old_launcher = (self.owner is not None
                    and getattr(getattr(self.owner, 'session', None), 'workflow_contract_version', 0) != 1)
                status.setText('This app needs the matching Suite launcher. Use a complete Suite update when available.'
                               if old_launcher else 'No installable release is available in this Manager build.')
                opening.setEnabled(False)
                manage.setEnabled(False)
                continue
            service, _executor = pair
            panel = self.panels[component_id]
            try:
                available = service.launch_available
            except (ValueError, RuntimeError, OSError):
                available = False
            opening.setEnabled(available and not held and not panel.close_held and not panel.closing
                              and self.launch_allowed())
            opening.setToolTip('Finish or close other management reviews before opening this app.'
                if not self.launch_allowed() else 'Open the installed app.'
                if available else 'Install the app, then restart Manager before opening it.')
            manage.setEnabled(not held and not panel.closing)
            self.dialogs[component_id].setEnabled(not held)
            if held:
                text = self.owner.reason
            elif resume:
                text = 'Your saved Generate work is ready to resume.'
            elif available:
                selected = self.owner.session.workflow_selections[component_id].selected
                text = WORKFLOW_NAMES[component_id] + ' ' + selected.version + ' is installed.'
                detail = panel.status.text()
                text += ' Ready to open.' if detail == panel.initial_status_text() else ' ' + detail
            else:
                text = panel.status.text()
            status.setText(text)

    def poll(self):
        if self.owner is not None:
            try:
                self.owner.poll()
            except (ValueError, RuntimeError, OSError):
                self.owner.reason = 'Workflow cleanup needs attention. Original ownership remains retained.'
        self.render()

    def close_ready(self):
        ready = True
        for panel in self.panels.values():
            ready = panel.close_ready() and ready
        if self.owner is not None:
            ready = self.owner.begin_close() and ready
        return ready and not self.close_held

    def resume_after_cancelled_close(self):
        for panel in self.panels.values():
            panel.resume_after_cancelled_close()
