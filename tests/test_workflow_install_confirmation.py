"""Actual nested Qt confirmation and shared lock regression; synthetic source only."""
from datetime import datetime, timezone
from types import SimpleNamespace as NS
import json

import pytest
from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QMessageBox, QPushButton

from suite_core.manager.window import create_window, unconfigured
from suite_core.manager.workflow_applications_ui import WorkflowAcquisitionPanel


@pytest.mark.parametrize('component', ('generate','train'))
def test_workflow_busy_progress_names_the_actual_application(component):
    app=QApplication.instance() or QApplication([])
    panel=WorkflowAcquisitionPanel('collectigen.'+component,
        NS(install_available=True,candidate_admission=object()),NS(close_held=False))
    panel.timer.stop()
    panel.future=NS(done=lambda: False)
    try:
        panel.progress.put(('phase','Verifying the downloaded reader'))
        panel.poll()
        assert panel.status.text()=='Verifying the downloaded '+component.title()+'. Please wait.'
        panel.progress.put(('phase','Saving the reader connection'))
        panel.poll()
        assert panel.status.text()=='Saving the '+component.title()+' connection. Please wait.'
        panel.progress.put((512*1024,1024*1024))
        panel.poll()
        assert panel.status.text()=='Downloading '+component.title()+': 50% of 1.0 MB. Installation has not finished.'
    finally:
        panel.future=None
        panel.deleteLater()
        app.processEvents()


@pytest.mark.parametrize('component', ['generate','train'])
@pytest.mark.parametrize('phase', ['activated','healthy'])
def test_completed_status_details_name_workflow_and_retain_safe_run_reason(tmp_path,component,phase):
    app=QApplication.instance() or QApplication([])
    owner=NS(reason='Workflow closed (exit 0); original process, Job and output are drained. Setup remains unconfirmed: no visible app window was observed during the original run.')
    service=NS(install_available=True,candidate_admission=object(),native_owner=owner,launch_available=True)
    panel=WorkflowAcquisitionPanel('collectigen.'+component,service,NS(close_held=False))
    panel.timer.stop()
    selected=NS(version='0.1.0-alpha.1',directory=tmp_path)
    installed=NS(selected=selected,phase=phase,pending=False,connection_pending=False)
    result=NS(installed=installed,connection_saved=True,reason='Reader setup is waiting for a successful reader run.')
    panel.operation='Checking app status'
    panel.future=NS(done=lambda:True,result=lambda:result)
    try:
        panel.show();app.processEvents();panel.poll();app.processEvents()
        text=panel.details.toPlainText()
        assert component.title() in text and 'reader' not in text.lower()
        assert ('Most recent workflow:' in text)==(phase=='activated')
        if phase=='activated':assert 'exit 0' in text and 'no visible app window' in text
        # Base Reader presentation is unchanged; no Reader application is run.
        from suite_core.manager.encyclopedia_acquisition_ui import EncyclopediaAcquisitionPanel
        assert EncyclopediaAcquisitionPanel.installation_details_status(panel,result)==result.reason
    finally:
        panel.deleteLater();app.processEvents()


@pytest.mark.parametrize('component', ('generate', 'train'))
def test_module_confirmation_stays_actionable_while_other_shared_edits_are_locked(monkeypatch, tmp_path, component):
    app = QApplication.instance() or QApplication([])
    manager = create_window(unconfigured(datetime.now(timezone.utc)))
    manager.workflow_applications.timer.stop()
    manager.show()
    owned = QDialog(manager)
    manager.workflow_applications.dialogs['collectigen.' + component] = owned
    service = NS(install_available=True, candidate_admission=object())
    panel = WorkflowAcquisitionPanel('collectigen.' + component, service, NS(close_held=False), parent=owned)
    owned.show()
    panel.show()
    other = QDialog(manager)
    edit = QLineEdit(other)
    apply = QPushButton('Apply', other)
    cancel = QPushButton('Cancel', other)
    other.show()
    app.processEvents()
    panel.confirming = True
    monkeypatch.setattr(type(manager.workflow_applications), 'close_held', property(lambda _self: True))
    reviewed = NS(offer=NS(release=NS(software_version='0.1.0-alpha.1',
        artifact=NS(size_bytes=1234, sha256='a' * 64))), destination=tmp_path / 'synthetic-package')
    seen = []
    timer = QTimer()
    def answer():
        box = app.activeModalWidget()
        if not isinstance(box, QMessageBox):
            return
        timer.stop()
        manager.poll_workflow_applications()
        yes, no = box.button(QMessageBox.Yes), box.button(QMessageBox.No)
        seen.append(dict(title=box.windowTitle(), text=box.text(), info=box.informativeText(),
            yes_enabled=yes.isEnabled(), no_enabled=no.isEnabled(), edit_enabled=edit.isEnabled(),
            apply_enabled=apply.isEnabled(), cancel_enabled=cancel.isEnabled(),
            owned_ancestor=owned.isAncestorOf(box), box_parent_is_panel=box.parentWidget() is panel))
        # Never bypass a disabled product control to make a test pass.
        QTest.mouseClick(yes if yes.isEnabled() else no, Qt.LeftButton)
    timer.timeout.connect(answer)
    timer.start(10)
    try:
        assert panel.refresh.text() == 'Find ' + component.title()
        assert 'Check Again' not in panel.status.text()
        assert panel.confirm_installation(reviewed), json.dumps(seen)
        assert len(seen) == 1
        observation = seen[0]
        assert observation['title'] == 'Install ' + component.title()
        assert observation['text'] == 'Install ' + component.title() + ' 0.1.0-alpha.1?'
        assert 'reader' not in observation['info'].lower()
        assert 'saved drafts, runs and images' in observation['info']
        assert observation['yes_enabled'] and observation['no_enabled']
        assert not observation['edit_enabled'] and not observation['apply_enabled']
        assert observation['cancel_enabled']
    finally:
        timer.stop()
        panel.confirming = False
        monkeypatch.undo()
        manager.poll_workflow_applications()
        assert edit.isEnabled() and apply.isEnabled()
        other.close()
        owned.close()
        manager.workflow_applications.dialogs.clear()
        manager.close()
        app.processEvents()


@pytest.mark.parametrize('component', ['generate', 'train', 'reader'])
def test_status_failure_names_the_affected_app_without_replaying(component):
    from suite_core.manager.encyclopedia_acquisition_ui import EncyclopediaAcquisitionPanel
    app=QApplication.instance() or QApplication([])
    service=NS(install_available=True,candidate_admission=object())
    executor=NS(close_held=False)
    panel=(EncyclopediaAcquisitionPanel(service,executor) if component=='reader' else
           WorkflowAcquisitionPanel('collectigen.'+component,service,executor))
    panel.timer.stop()
    def failure():raise ValueError('original_component_scope_required')
    panel.operation='Checking app status'
    panel.future=NS(done=lambda:True,result=failure)
    try:
        panel.show();app.processEvents();panel.poll();app.processEvents()
        name='reader' if component=='reader' else component.title()
        assert panel.status.text()=='App status is unavailable. Close '+name+' if it is running, then Check app status.'
        assert 'original_component_scope_required' in panel.details.toPlainText()
        assert panel.future is None
    finally:
        panel.deleteLater();app.processEvents()


def test_dashboard_exposes_module_entry_and_navigation_does_not_keep_other_section_hint():
    app = QApplication.instance() or QApplication([])
    manager = create_window(unconfigured(datetime.now(timezone.utc)))
    manager.show()
    app.processEvents()
    try:
        assert 'application' in manager.statusBar().currentMessage().lower()
        assert 'Databases' not in manager.statusBar().currentMessage()
        from PySide6.QtWidgets import QLabel
        labels = [label.text() for label in manager.findChildren(QLabel)]
        assert any('Generate · Manage or open in Applications' in value
            and 'Train · Manage or open in Applications' in value for value in labels)
        manager.navigation.setCurrentRow(manager.page_rows['Applications'])
        app.processEvents()
        assert 'Generate, Train and Media Organizer' in manager.statusBar().currentMessage()
        manager.navigation.setCurrentRow(manager.page_rows['Servers'])
        app.processEvents()
        assert 'Use Applications' not in manager.statusBar().currentMessage()
    finally:
        manager.close()
        app.processEvents()


@pytest.mark.parametrize('component', ('generate', 'train'))
def test_first_open_failure_keeps_matching_retry_and_inspectable_details(component):
    from time import monotonic
    from suite_core.manager.workflow_applications_ui import WorkflowApplications
    from suite_core.manager.resource_executor import ResourceExecutorOwner

    app = QApplication.instance() or QApplication([])
    surface = WorkflowApplications()
    surface.timer.stop()
    identity = 'collectigen.' + component
    # Component service boundary only; this grants no original/native admission.
    surface.owner = NS(close_held=False, pending_generate_draft=None,
        session=NS(workflow_contract_version=1,
                   workflow_selections={identity:NS(selected=NS(version='0.1.0-alpha.1'))}), reason='Idle')
    calls = []
    def fail_launch(**options):
        calls.append(options)
        raise RuntimeError('synthetic_workflow_start_failed')
    service = NS(install_available=True, candidate_admission=object(),
        launch_available=True, observe=lambda: None, launch=fail_launch)
    executor = ResourceExecutorOwner()
    dialog = QDialog(surface)
    panel = WorkflowAcquisitionPanel('collectigen.' + component, service, executor, parent=dialog)
    from PySide6.QtWidgets import QVBoxLayout
    QVBoxLayout(dialog).addWidget(panel)
    identity = 'collectigen.' + component
    surface.services[identity] = (service, executor)
    surface.panels[identity] = panel
    surface.dialogs[identity] = dialog
    surface.render()
    surface.show()
    app.processEvents()
    try:
        assert not panel.check_attempted
        QTest.mouseClick(surface.rows[identity][1], Qt.LeftButton)
        deadline = monotonic() + 3
        while (panel.future is not None or executor.close_held) and monotonic() < deadline:
            app.processEvents()
            QTest.qWait(10)
        assert panel.future is None and not executor.close_held
        assert len(calls) == 1
        QTest.mouseClick(surface.rows[identity][2], Qt.LeftButton)
        app.processEvents()
        assert panel.technical.isVisible()
        assert panel.refresh.isVisible() and panel.refresh.isEnabled()
        assert panel.refresh.text() == 'Check Again'
        assert 'Check Again' in panel.status.text()
        QTest.mouseClick(panel.technical, Qt.LeftButton)
        app.processEvents()
        assert panel.details.isVisible()
        assert 'Operation: Opening ' + component.title() in panel.details.toPlainText()
        assert 'synthetic_workflow_start_failed' in panel.details.toPlainText()
        assert len(calls) == 1  # Inspecting the failure must never replay launch.
    finally:
        panel.timer.stop()
        executor.begin_close()
        dialog.close()
        surface.close()
        app.processEvents()


@pytest.mark.parametrize('component', ('generate', 'train'))
@pytest.mark.parametrize('theme', ('dark', 'light'))
def test_initial_card_guidance_reaches_visible_find_without_lookup(component, theme, tmp_path, record_property):
    from PySide6.QtWidgets import QVBoxLayout
    from qt_app.theme import apply_theme
    from suite_core.manager.workflow_applications_ui import WorkflowApplications
    from suite_core.manager.resource_executor import ResourceExecutorOwner
    app = QApplication.instance() or QApplication([])
    apply_theme(app, theme)
    surface = WorkflowApplications()
    surface.timer.stop()
    identity = 'collectigen.' + component
    # Presentation seam only: no signed/native admission or network grant.
    surface.owner = NS(close_held=False, pending_generate_draft=None, reason='Idle',
        session=NS(workflow_contract_version=1, workflow_selections={identity:NS(selected=None)}))
    service = NS(install_available=True, candidate_admission=None, qualification_only=False, launch_available=False)
    executor = ResourceExecutorOwner()
    dialog = QDialog(surface)
    dialog.resize(1040, 700)
    panel = WorkflowAcquisitionPanel(identity, service, executor, parent=dialog)
    panel.timer.stop()
    QVBoxLayout(dialog).addWidget(panel)
    surface.services[identity] = (service, executor)
    surface.panels[identity] = panel
    surface.dialogs[identity] = dialog
    surface.resize(1920, 1080)
    surface.render();surface.show();app.processEvents()
    try:
        status, opening, manage, _group = surface.rows[identity]
        assert not opening.isEnabled() and manage.isVisible() and manage.isEnabled()
        assert manage.text() in status.text() and component.title() in status.text()
        assert panel.refresh.text() not in status.text()
        image = surface.grab()
        assert (image.width(), image.height()) == (1920, 1080)
        capture = tmp_path / (theme + '-' + component + '-initial.png')
        assert image.save(str(capture))
        record_property('source_component_capture', str(capture))
        QTest.mouseClick(manage, Qt.LeftButton);app.processEvents()
        assert dialog.isVisible() and panel.refresh.isVisible() and panel.refresh.isEnabled()
        assert panel.refresh.text() == 'Find ' + component.title()
        assert panel.refresh.text() in panel.status.text()
        assert not panel.check_attempted and panel.future is None and not executor.close_held
        capture = tmp_path / (theme + '-' + component + '-manage.png')
        assert dialog.grab().save(str(capture))
        record_property('source_component_dialog_capture', str(capture))
    finally:
        panel.timer.stop();executor.begin_close();dialog.close();surface.close();app.processEvents()
