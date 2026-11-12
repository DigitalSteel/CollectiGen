"""Organizer owns one new-plan launcher alongside its existing saved drafts."""
from types import SimpleNamespace
from unittest.mock import Mock
from PySide6.QtWidgets import QApplication,QWidget,QPushButton
from suite_core.media_organizer.organizer_recovery_entry import with_dataset_recovery


def test_organizer_dataset_plan_reuses_source_and_disabled_guard():
    app=QApplication.instance() or QApplication([])
    page=QWidget();goal=QWidget(page);button=QPushButton('Plan Training Dataset…',goal)
    page.training_plan_button=button
    calls=[];button.clicked.connect(lambda:calls.append('plan'))
    owner=SimpleNamespace(dataset_page=page,review_dataset_package_receipt=Mock())
    cls=with_dataset_recovery(type('Base',(),{}))
    commands=cls.workspace_extra_commands(owner,2)
    plans=[c for c in commands if c.key=='organizer:training-plan']
    assert len(plans)==1 and plans[0].source is button and plans[0].group=='Dataset Plans'
    assert button.property('commandBarOwned')
    button.setEnabled(False);assert not plans[0].invoke();assert not calls
    button.setEnabled(True);assert plans[0].invoke();assert calls==['plan']
    assert sum(c.source is button for c in cls.workspace_extra_commands(owner,2))==1
    assert any(c.key=='organizer:package-recovery' for c in commands)
    page.close();app.processEvents()


def test_organizer_plan_command_does_not_create_dataset_or_change_library_home():
    app=QApplication.instance() or QApplication([])
    cls=with_dataset_recovery(type('Base',(),{}))
    owner=SimpleNamespace(review_dataset_package_receipt=Mock())
    assert not any(c.key=='organizer:training-plan' for c in cls.workspace_extra_commands(owner,2))
    assert cls.workspace_extra_commands(owner,0)==[]
    assert cls.workspace_extra_commands(owner,6)==[]
    assert not hasattr(owner,'dataset_page')


def test_late_dataset_binding_refreshes_organizer_saved_draft_label():
    from qt_app.command_bar import UiCommand
    app=QApplication.instance() or QApplication([])
    page=QWidget();page.training_resume_button=QPushButton('Open Training',page)
    command=UiCommand('saved-source','Open Training','Training',source=page.training_resume_button)
    controller=SimpleNamespace(_page_commands={2:(page,[command])})
    owner=SimpleNamespace(dataset_page=page,command_controller=controller,review_dataset_package_receipt=Mock())
    cls=with_dataset_recovery(type('Base',(),{}))
    cls.workspace_extra_commands(owner,2)
    assert page.training_resume_button.text()=='Saved Dataset Drafts.'
    assert command.label=='Saved Dataset Drafts.'
    assert command.source is page.training_resume_button
    assert 'execution is unavailable' in page.training_resume_button.toolTip()
    cls.workspace_extra_commands(owner,2)
    assert command.label=='Saved Dataset Drafts.'
    page.close();app.processEvents()
