"""Generated component journeys for Train's shared model/method presentation.

Package verification and preparation are explicit inert test doubles here.
Original Qt controls, executor ownership and settlement remain real. Native
package/worker/saved-reopen evidence is recorded separately by the release gate.
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from threading import Event
from time import monotonic
from types import SimpleNamespace as NS

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTimer
from PySide6.QtGui import QAccessible
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialogButtonBox, QLabel, QLineEdit, QProgressBar, QPushButton, QToolButton

from core.background_jobs import BackgroundJobManager
from core.training_configuration import TrainingConfiguration
from core.training_dataset_binding import ReviewedDatasetVerifier, TrainingPreparationRetentionError
from core.training_package import TrainingPackageVerification
from core.training_process import ProcessInventory
from suite_core.manager.krea_training_review import KreaTrainingReviewDialog
from test_manager_generator_setup_ui import application, wait_until


def capture(dialog, name):
    destination = os.environ.get('COLLECTIGEN_TRAIN_CLARITY_CAPTURES')
    if destination:
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        assert dialog.grab().save(str(root / (name + '.png')))


def assert_visible_controls_unclipped(dialog):
    assert dialog.width() >= 720
    for control in [*dialog.findChildren(QPushButton), *dialog.findChildren(QLineEdit)]:
        if control.isVisible():
            assert control.visibleRegion().contains(control.rect().adjusted(2, 2, -2, -2)), (
                control.accessibleName(), getattr(control, 'text', lambda: '')(), control.geometry())


def review_fixture(tmp_path, monkeypatch, theme, *, family='SDXL', method='lora', version='', release=None, review_error=None):
    app = application()
    from qt_app.theme import apply_theme
    apply_theme(app, theme)
    config = TrainingConfiguration(target_model_family=family, target_model_version=version, output_kind=method)
    package = tmp_path / 'Synthetic package'
    package.mkdir()
    manifest = {'candidate_plan': {'goal': {'text': 'Teach a small still-life concept'}},
        'counts': {'included_images': 2, 'excluded_images': 1, 'training': 1, 'validation': 1, 'holdout': 0},
        'training_configuration': config.to_dict(), 'excluded': [],
        'package_plan': {'items': []}, 'artifacts': []}
    verification = TrainingPackageVerification(package, True, 'a' * 64, manifest=manifest)
    report = 'Registered Anima base is unavailable. Review the exact model registration.' if family == 'Anima' else None
    def reviewed(*_args):
        if release is not None:
            assert release.wait(12), 'Generated package check was not released'
        if review_error is not None:
            raise review_error
        return verification, config, report
    monkeypatch.setattr(KreaTrainingReviewDialog, '_review', reviewed)
    inventory = ProcessInventory(str(tmp_path / 'inert-worker'), str(tmp_path / 'inert-inventory.json'), 'b' * 64, 0, 0)
    verifier = ReviewedDatasetVerifier(inventory, 'python.exe', tmp_path / 'prepared')
    dialog = KreaTrainingReviewDialog(package, tmp_path / 'models.json', None,
        dataset_verifier=verifier, preview_jobs=BackgroundJobManager(),
        anima_settings_path=tmp_path / 'generated-settings.json', anima_review_allowed=lambda: True)
    dialog.resize(1920, 1080)
    dialog.show()
    app.processEvents()
    return app, dialog, verifier, verification, config


def finish(app, dialog, release=None):
    if release is not None:
        release.set()
    dialog.reject()
    wait_until(app, lambda: not dialog.isVisible())
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


@pytest.mark.parametrize('theme', ['dark', 'light'])
@pytest.mark.parametrize('family,method,method_label', [
    ('SDXL', 'lora', 'LoRA'), ('SDXL', 'full_fine_tune', 'Full fine-tune'),
    ('SDXL', 'embedding', 'Embedding'), ('Anima', 'lora', 'LoRA'), ('Krea 2', 'lora', 'LoRA')])
def test_model_and_method_scope_keep_exact_expert_details(tmp_path, monkeypatch, theme, family, method, method_label):
    app, dialog, verifier, verification, config = review_fixture(tmp_path, monkeypatch, theme, family=family, method=method)
    try:
        wait_until(app, lambda: not dialog._timer.isActive())
        assert family in dialog.package_scope.text() and method_label in dialog.package_scope.text()
        assert '2 included images' in dialog.package_scope.text()
        assert 'Teach a small still-life concept' in dialog.package_goal.text()
        for label in (dialog.package_goal, dialog.package_scope, dialog.readiness_notes, dialog.next_step):
            accessible = QAccessible.queryAccessibleInterface(label)
            assert accessible.text(QAccessible.Text.Name) == label.text()
            assert accessible.text(QAccessible.Text.Description) == label.accessibleDescription()
        assert not dialog.details_panel.isVisible()
        assert 'Training has not started' in dialog.readiness_notes.text() or family == 'Anima'
        if family == 'SDXL' and method == 'lora':
            assert dialog.prepare_button.isVisible() and dialog.prepare_button.property('primary')
            assert 0 <= dialog.prepare_button.geometry().top() - dialog.status.geometry().bottom() <= 40
        else:
            assert not dialog.prepare_button.isVisible()
            assert dialog.details_button.property('primary')
        if family == 'Krea 2':
            assert 'cannot use this folder directly' in dialog.next_step.text()
        if family == 'Anima':
            assert 'Registered Anima base is unavailable' in dialog.readiness_notes.text()
        capture(dialog, f'package-{family}-{method}-{theme}')
        original = tuple(section.toPlainText() for section in dialog.sections)
        if family == 'SDXL' and method == 'lora':
            dialog.prepare_button.setFocus(Qt.TabFocusReason)
            QTest.keyClick(dialog.prepare_button, Qt.Key_Tab)
            assert dialog.return_button.hasFocus()
            QTest.keyClick(dialog.return_button, Qt.Key_Tab)
            assert dialog.details_button.hasFocus()
        else:
            dialog.details_button.setFocus()
        QTest.keyClick(dialog.details_button, Qt.Key_Space)
        assert dialog.details_panel.isVisible()
        if family == 'SDXL' and method == 'lora':
            capture(dialog, 'package-expert-details-' + theme)
        assert verification.package_fingerprint in dialog.sections[0].toPlainText()
        assert f'Expected output: {method}' in dialog.sections[1].toPlainText()
        QTest.mouseClick(dialog.details_button, Qt.LeftButton)
        assert not dialog.details_panel.isVisible()
        assert tuple(section.toPlainText() for section in dialog.sections) == original
        if family == 'SDXL' and method == 'lora':
            capture(dialog, 'package-expert-return-' + theme)
    finally:
        finish(app, dialog)


@pytest.mark.parametrize('theme', ['dark', 'light'])
def test_delayed_package_check_has_activity_elapsed_and_original_cancel(tmp_path, monkeypatch, theme):
    release = Event()
    app, dialog, *_ = review_fixture(tmp_path, monkeypatch, theme, release=release)
    try:
        wait_until(app, lambda: 'Elapsed 1s' in dialog.elapsed.text(), timeout=5)
        assert dialog.progress.isVisible() and dialog.progress.minimum() == dialog.progress.maximum() == 0
        assert dialog.return_button.isVisible() and dialog.return_button.isEnabled()
        capture(dialog, 'package-delayed-' + theme)
        QTest.mouseClick(dialog.return_button, Qt.LeftButton)
        assert dialog._cancel.is_set() and dialog.isVisible()
        assert not dialog.return_button.isEnabled()
        release.set()
        wait_until(app, lambda: not dialog.isVisible())
        assert not dialog.prepare_button.isVisible()
    finally:
        finish(app, dialog, release)


@pytest.mark.parametrize('theme', ['dark', 'light'])
@pytest.mark.parametrize('retained', [False, True])
def test_prepare_result_distinguishes_retained_workspace_from_saved_list(tmp_path, monkeypatch, theme, retained):
    app, dialog, verifier, *_ = review_fixture(tmp_path, monkeypatch, theme)
    prepared = verifier.work_root / 'synthetic-copy'
    prepared.mkdir(parents=True)
    calls, errors = [], []
    def prepare(*args, **kwargs):
        calls.append((args, kwargs))
        if retained:
            raise TrainingPreparationRetentionError(prepared)
        return {}, NS(root=prepared), {}, {}
    monkeypatch.setattr(ReviewedDatasetVerifier, 'prepare', prepare)
    def choose():
        try:
            popup = dialog._preparation_setup
            if popup is None:
                QTimer.singleShot(10, choose)
                return
            toggle, = [b for b in popup.findChildren(QToolButton) if b.text() == 'Trainer folders']
            QTest.mouseClick(toggle, Qt.LeftButton)
            for field in popup.findChildren(QLineEdit):
                assert field.isVisible()
                field.setText(str(tmp_path / field.accessibleName().replace(' ', '-')))
            app.processEvents()
            assert_visible_controls_unclipped(popup)
            button = popup.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)
            assert button.isEnabled() and button.property('primary')
            QTest.mouseClick(button, Qt.LeftButton)
        except BaseException as exc:
            errors.append(repr(exc))
            if dialog._preparation_setup is not None:
                dialog._preparation_setup.reject()
    try:
        wait_until(app, lambda: not dialog._timer.isActive())
        QTimer.singleShot(10, choose)
        QTest.mouseClick(dialog.prepare_button, Qt.LeftButton)
        assert not errors, errors
        wait_until(app, lambda: dialog._preparation_complete and not dialog._timer.isActive())
        assert len(calls) == 1
        assert str(prepared) in dialog.saved_location.text() and dialog.saved_location.isVisible()
        assert 'a' * 64 in dialog.sections[0].toPlainText()
        assert dialog.return_button.property('primary')
        if retained:
            assert not dialog._preparation_saved
            assert 'do not assume it appears' in dialog.next_step.text()
        else:
            assert dialog._preparation_saved and 'Saved Preparations' in dialog.next_step.text()
        capture(dialog, ('retained-without-list-' if retained else 'preparation-complete-') + theme)
    finally:
        finish(app, dialog)


@pytest.mark.parametrize('theme', ['dark', 'light'])
def test_held_then_admitted_dataset_check_replaces_current_requirements(tmp_path, monkeypatch, theme):
    app, dialog, verifier, verification, config = review_fixture(tmp_path, monkeypatch, theme)
    replies = [dict(status='held', reason='policy_held', summary={'holds': {'pixel_geometry_unmeasured': 1}}),
               dict(status='admitted', summary={'training_images': 1, 'evaluation_images': 1, 'epoch_exposures': config.repeats})]
    def check(*_args, **_kwargs):
        return dict(replies.pop(0), binding={'package_fingerprint': verification.package_fingerprint}, preflight_receipt_sha256='d' * 64)
    monkeypatch.setattr(ReviewedDatasetVerifier, '__call__', check)
    try:
        wait_until(app, lambda: not dialog._timer.isActive())
        QTest.mouseClick(dialog.details_button, Qt.LeftButton)
        QTest.mouseClick(dialog.dataset_button, Qt.LeftButton)
        wait_until(app, lambda: not dialog._timer.isActive())
        assert 'could not verify the image dimensions' in dialog.readiness_notes.text()
        assert not dialog.prepare_button.isEnabled() and not dialog.prepare_button.property('primary')
        assert 'Choose Verify SDXL Dataset or' not in dialog.sections[0].toPlainText()
        capture(dialog, 'dataset-held-' + theme)
        QTest.mouseClick(dialog.dataset_button, Qt.LeftButton)
        wait_until(app, lambda: not dialog._timer.isActive())
        assert 'Dataset inspection passed' in dialog.readiness_notes.text()
        assert not replies, 'Both original explicit dataset checks must actually settle'
        assert 'could not verify the image dimensions' not in dialog.readiness_notes.text()
        assert dialog.prepare_button.isEnabled() and dialog.prepare_button.property('primary')
        dialog._invalidate_package_review('The selected package changed. Choose it again.')
        dialog._refresh_presentation()
        assert not dialog.prepare_button.isEnabled() and dialog.return_button.property('primary')
        assert 'no longer establish current readiness' in dialog.readiness_notes.text()
        assert dialog.sections[0].toPlainText().startswith('Earlier review evidence — not current')
        capture(dialog, 'package-stale-' + theme)
    finally:
        finish(app, dialog)


@pytest.mark.parametrize('theme', ['dark', 'light'])
@pytest.mark.parametrize('family,version', [('SDXL', '1.0'), ('Anima', 'Base v1.0')])
def test_explicit_model_version_stays_visible(tmp_path, monkeypatch, theme, family, version):
    app, dialog, *_ = review_fixture(tmp_path, monkeypatch, theme, family=family, version=version)
    try:
        wait_until(app, lambda: not dialog._timer.isActive())
        assert f'{family} {version}' in dialog.package_scope.text()
        assert 'LoRA' in dialog.package_scope.text()
        capture(dialog, 'explicit-version-' + family + '-' + theme)
    finally:
        finish(app, dialog)


@pytest.mark.parametrize('theme', ['dark', 'light'])
@pytest.mark.parametrize('outcome', ['complete', 'cancel', 'failure'])
def test_saved_trainer_loading_keeps_progress_and_manual_choices(tmp_path, monkeypatch, theme, outcome):
    app, dialog, verifier, *_ = review_fixture(tmp_path, monkeypatch, theme)
    release, entered = Event(), Event()
    calls, errors = [], []
    def resolve(_self, cancelled):
        calls.append(True)
        entered.set()
        assert release.wait(10)
        if outcome == 'failure':
            raise ValueError('Generated trainer unavailable')
        return tuple(str(tmp_path / part) for part in ('source', 'environment', 'runtime'))
    monkeypatch.setattr(KreaTrainingReviewDialog, '_resolve_saved_sdxl_setup', resolve)
    state = {'clicked': False, 'captured': False, 'done': False}
    deadline = monotonic() + 12
    def drive():
        if state['done']:
            return
        try:
            assert monotonic() < deadline
            popup = dialog._preparation_setup
            if popup is None:
                QTimer.singleShot(10, drive)
                return
            load, = [b for b in popup.findChildren(QPushButton) if b.text() == 'Load Saved Trainer']
            buttons = popup.findChild(QDialogButtonBox)
            if not state['clicked']:
                assert load.isVisible() and load.property('primary')
                assert not any(f.isVisible() for f in popup.findChildren(QLineEdit))
                state['clicked'] = True
                QTest.mouseClick(load, Qt.LeftButton)
            elapsed, = [w for w in popup.findChildren(QLabel)
                        if w.accessibleDescription() == 'Saved trainer check elapsed time']
            if entered.is_set() and elapsed.isVisible() and 'Elapsed 1s' in elapsed.text() and not state['captured']:
                assert QAccessible.queryAccessibleInterface(elapsed).text(QAccessible.Text.Name) == elapsed.text()
                state['captured'] = True
                progress, = popup.findChildren(QProgressBar)
                assert progress.isVisible() and progress.minimum() == progress.maximum() == 0
                assert not buttons.button(QDialogButtonBox.Ok).isEnabled()
                assert_visible_controls_unclipped(popup)
                capture(popup, 'saved-trainer-delay-' + outcome + '-' + theme)
                if outcome == 'cancel':
                    QTest.mouseClick(buttons.button(QDialogButtonBox.Cancel), Qt.LeftButton)
                    assert popup.isVisible()
                release.set()
            if state['captured'] and release.is_set() and outcome != 'cancel' and load.isEnabled():
                fields = popup.findChildren(QLineEdit)
                if outcome == 'complete':
                    assert all(f.text() for f in fields)
                    assert buttons.button(QDialogButtonBox.Ok).isEnabled()
                    assert 'Trainer: source' in next(w.text() for w in popup.findChildren(QLabel)
                        if w.accessibleDescription() == 'Selected saved trainer')
                else:
                    assert all(f.isVisible() and not f.text() for f in fields)
                app.processEvents()
                assert_visible_controls_unclipped(popup)
                capture(popup, 'saved-trainer-' + outcome + '-' + theme)
                state['done'] = True
                QTest.mouseClick(buttons.button(QDialogButtonBox.Cancel), Qt.LeftButton)
                return
            if outcome == 'cancel' and state['captured'] and dialog._preparation_setup is None:
                state['done'] = True
                return
            QTimer.singleShot(20, drive)
        except BaseException as exc:
            errors.append(repr(exc))
            release.set()
            state['done'] = True
            if dialog._preparation_setup is not None:
                dialog._preparation_setup.reject()
    try:
        wait_until(app, lambda: not dialog._timer.isActive())
        QTimer.singleShot(10, drive)
        QTest.mouseClick(dialog.prepare_button, Qt.LeftButton)
        state['done'] = True
        assert not errors, errors
        assert state['captured'] and len(calls) == 1
        assert not dialog._preparation_complete
    finally:
        state['done'] = True
        release.set()
        finish(app, dialog)


@pytest.mark.parametrize('theme', ['dark', 'light'])
def test_saved_preparation_disclosure_preserves_exact_selection_and_reopen(tmp_path, theme):
    from qt_app.training_history_dialog import TrainingHistoryDialog
    from test_manager_training_history_management import indexed
    from qt_app.theme import apply_theme
    app = application()
    apply_theme(app, theme)
    index, row, revision, receipt, output, _registry = indexed(tmp_path / 'TrainingReview', preparations=True)
    before = (index.path.read_bytes(), receipt.read_bytes(), output.read_bytes())
    for reopening in (False, True):
        dialog = TrainingHistoryDialog(index, BackgroundJobManager(), preparations=True)
        dialog.resize(1920, 1080)
        dialog.show()
        try:
            wait_until(app, lambda: dialog.runs.topLevelItemCount() == 1 and dialog.runs.isEnabled())
            selected = dialog.runs.topLevelItem(0)
            QTest.mouseClick(dialog.runs.viewport(), Qt.LeftButton, pos=dialog.runs.visualItemRect(selected).center())
            assert dialog._selected().run_id == row.run_id
            assert selected.text(0) == 'SDXL preparation 1'
            assert selected.text(2) == 'Prepared · training not started'
            assert dialog.runs.isColumnHidden(1) and dialog.runs.isColumnHidden(3)
            assert dialog.open_button.isVisible() and dialog.open_button.property('primary')
            assert not dialog.detail.isVisible() and not dialog.forget_button.isVisible()
            assert 'live inputs' in dialog.selected_summary.text()
            capture(dialog, ('saved-reopened-' if reopening else 'saved-preparation-') + theme)
            dialog.details_button.setFocus()
            QTest.keyClick(dialog.details_button, Qt.Key_Space)
            assert dialog.detail.isVisible() and str(row.destination) in dialog.detail.text()
            assert row.run_id in dialog.detail.text() and dialog.forget_button.isVisible()
            assert dialog._selected().run_id == row.run_id
            if not reopening:
                capture(dialog, 'saved-preparation-details-' + theme)
            QTest.keyClick(dialog.details_button, Qt.Key_Space)
            assert not dialog.detail.isVisible() and dialog._selected().run_id == row.run_id
        finally:
            dialog.reject()
            wait_until(app, lambda: not dialog.isVisible())
            dialog.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            app.processEvents()
        assert (index.path.read_bytes(), receipt.read_bytes(), output.read_bytes()) == before


@pytest.mark.parametrize('theme', ['dark', 'light'])
@pytest.mark.parametrize('outcome', ['complete', 'cancel', 'failure'])
def test_delayed_preparation_progress_cancel_and_explicit_retry(tmp_path, monkeypatch, theme, outcome):
    app, dialog, verifier, *_ = review_fixture(tmp_path, monkeypatch, theme)
    entered, release = Event(), Event()
    calls, errors = [], []
    prepared = verifier.work_root / 'generated-copy'
    prepared.mkdir(parents=True)
    prior = verifier.work_root / 'prior-saved-work.txt'
    prior.write_bytes(b'Existing generated work must survive cancellation and retry.\n')
    before = prior.read_bytes()
    def prepare(*_args, **kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(10)
        if outcome == 'failure' and len(calls) == 1:
            raise RuntimeError('Generated preparation failure before publication')
        return {}, NS(root=prepared), {}, {}
    monkeypatch.setattr(ReviewedDatasetVerifier, 'prepare', prepare)
    def choose():
        try:
            popup = dialog._preparation_setup
            if popup is None:
                QTimer.singleShot(10, choose)
                return
            toggle, = [b for b in popup.findChildren(QToolButton) if b.text() == 'Trainer folders']
            QTest.mouseClick(toggle, Qt.LeftButton)
            for field in popup.findChildren(QLineEdit):
                field.setText(str(tmp_path / field.accessibleName().replace(' ', '-')))
            QTest.mouseClick(popup.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok), Qt.LeftButton)
        except BaseException as exc:
            errors.append(repr(exc))
            if dialog._preparation_setup is not None:
                dialog._preparation_setup.reject()
    try:
        wait_until(app, lambda: not dialog._timer.isActive())
        QTimer.singleShot(10, choose)
        QTest.mouseClick(dialog.prepare_button, Qt.LeftButton)
        assert not errors, errors
        wait_until(app, entered.is_set)
        wait_until(app, lambda: 'Elapsed 1s' in dialog.status.text(), timeout=5)
        assert dialog.progress.isVisible() and dialog.progress.minimum() == dialog.progress.maximum() == 0
        assert not dialog.prepare_button.isEnabled() and dialog.return_button.isEnabled()
        assert 'Cancel' in dialog.next_step.text()
        capture(dialog, 'preparation-delayed-' + outcome + '-' + theme)
        if outcome == 'cancel':
            QTest.mouseClick(dialog.return_button, Qt.LeftButton)
            assert dialog.isVisible() and dialog._cancel.is_set()
            release.set()
            wait_until(app, lambda: not dialog.isVisible())
            assert not dialog._preparation_complete
        else:
            release.set()
            wait_until(app, lambda: not dialog._timer.isActive())
            if outcome == 'failure':
                assert 'Preparation did not finish' in dialog.status.text()
                assert dialog.prepare_button.isEnabled() and dialog.prepare_button.property('primary')
                capture(dialog, 'preparation-failure-' + theme)
                QTimer.singleShot(10, choose)
                QTest.mouseClick(dialog.prepare_button, Qt.LeftButton)
                assert not errors, errors
                wait_until(app, lambda: dialog._preparation_complete and not dialog._timer.isActive())
                assert len(calls) == 2, 'Only the explicit second click may retry'
            else:
                assert dialog._preparation_complete and len(calls) == 1
        assert prior.read_bytes() == before
    finally:
        release.set()
        finish(app, dialog)


@pytest.mark.parametrize('theme', ['dark', 'light'])
def test_unavailable_package_returns_and_fresh_review_can_open(tmp_path, monkeypatch, theme):
    failed_root = tmp_path / 'first'
    failed_root.mkdir()
    app, dialog, *_ = review_fixture(failed_root, monkeypatch, theme,
        review_error=ValueError('Generated package is unavailable or changed'))
    try:
        wait_until(app, lambda: not dialog._timer.isActive())
        assert 'Compatibility review unavailable' in dialog.status.text()
        assert not dialog.progress.isVisible() and dialog.return_button.property('primary')
        assert 'could not be verified' in dialog.package_goal.text()
        assert not dialog.prepare_button.isVisible()
        capture(dialog, 'package-unavailable-' + theme)
    finally:
        finish(app, dialog)
    fresh_root = tmp_path / 'second'
    fresh_root.mkdir()
    app, reopened, *_ = review_fixture(fresh_root, monkeypatch, theme)
    try:
        wait_until(app, lambda: not reopened._timer.isActive())
        assert reopened._package_valid and reopened.prepare_button.isEnabled()
    finally:
        finish(app, reopened)
