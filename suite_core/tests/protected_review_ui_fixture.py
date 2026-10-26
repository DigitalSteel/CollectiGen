"""Read-only supplied-review component qualification; never a native journey.

Run with an explicit immutable source export and fresh synthetic output folder.
Qt's Windows platform supplies readable native fonts; offscreen is not visual proof.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import subprocess
import sys


def qualify(source, output, theme, scenario='protected'):
    source, output = Path(source).resolve(), Path(output).resolve()
    assert source.is_dir() and not output.exists(), "Explicit source and fresh output required"
    assert output != source and not source.is_relative_to(output)
    output.mkdir(parents=True)
    sys.path.insert(0, str(source))
    from contextlib import nullcontext
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from suite_core.database_actions import CatalogRelease, DatabaseAction, DatabaseActionsController
    from suite_core.encyclopedia.publication import Publication
    from suite_core.knowledge import Category, EffectiveTag, Lookup, Snapshot, Source, normalize_term
    from suite_core.knowledge.catalog_migration_preview import PackRevision
    from suite_core.knowledge.tag_transactions import TagCatalogHost, TagCatalogTransactions, TransactionReview
    from suite_core.manager.tag_database_controller import TagDatabaseChoice, TagDatabaseController
    from suite_core.manager.tag_database_ui import TagDatabasePane
    from suite_core.manager.database_presentation import project_database_review
    from suite_core.selected_catalog import SelectedCatalogSession
    # Same typed fox fixture as test_database_actions; keep the tiny factories
    # here because the public component export intentionally omits that test file.
    # No native record parser, enrollment or writer is introduced.
    def snapshot(tags=(), *, revision='r1'):
        names = {}
        for tag in tags:
            for name in (tag.display, *tag.aliases):
                names.setdefault(normalize_term(name), set()).add(tag.id)
        rows = []
        for name, owners in names.items():
            owners = tuple(sorted(owners))
            rows.append(Lookup(name, 'canonical_name' if len(owners) == 1 else 'ambiguous',
                               owners[0] if len(owners) == 1 else '', owners))
        return Snapshot(Source('synthetic approved fixture', revision, 'fixture-authority', ('animals',)),
                        tuple(tags), (Category('animals', 'Animals', 'Animals'),), tuple(rows))

    def release(value, version):
        return CatalogRelease(PackRevision('optional-animals', version, value), '2026-10-06',
                              (f'Synthetic vocabulary {version}.',), 'approved fixture release')
    from qt_app.theme import apply_theme

    loaded = {}
    for name in ('suite_core.database_actions', 'suite_core.manager.database_presentation',
                 'suite_core.manager.tag_database_ui', 'suite_core.manager.tag_database_controller',
                 'suite_core.knowledge.tag_transactions'):
        path = Path(sys.modules[name].__file__).resolve()
        assert path.is_relative_to(source), (name, path)
        loaded[path.relative_to(source).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()

    calls = []
    def deny(*args, **kwargs):
        calls.append('forbidden_host_call')
        raise AssertionError('Read-only component fixture cannot invoke host operations')

    original = snapshot((EffectiveTag('fox', 'Red Fox', 'animals', ('Vulpes',)),))
    local = snapshot((EffectiveTag('fox', 'My Fox', 'animals', ('Mine',)),))
    proposed = snapshot((*original.tags, EffectiveTag('wolf', 'Wolf', 'animals')), revision='r2')
    base, available = release(original, '1'), release(proposed, '2')
    session = SelectedCatalogSession(Publication('selected-catalog-a', 1, local))
    db = DatabaseActionsController(session, base_release=base, available_release=available)
    review = db.preview(DatabaseAction.MERGE_UPDATE)
    assert db.accepts(review) and len(review.protected_edits) == 1
    row = review.protected_edits[0]
    assert row.tag_id == 'fox' and row.protected_fields == ('display', 'aliases')
    assert row.proposed.display == 'My Fox' and row.proposed.aliases == ('Mine',)
    catalog = output / 'synthetic-catalog'
    catalog.mkdir()
    host = TagCatalogHost(catalog, nullcontext, deny, deny, deny, deny, deny, deny)
    service = TagCatalogTransactions(host, session, destination=catalog / 'publication.json')
    service.controller = db
    supplied = TransactionReview(review, None, output / 'unused-pack.zip', 'component-only', 'supplied-review', ())
    service.current = supplied
    receipts = tuple(catalog / 'backups' / run / 'recovery.json' for run in ('run-A', 'run-B'))
    submissions = []
    class DenyingExecutor:
        def submit(self, *args, **kwargs):
            submissions.append('forbidden_submission')
            raise AssertionError('Receipt selection cannot start an operation')
    choice = TagDatabaseChoice('Synthetic selected catalog', service, base, available,
                               output / 'unused-pack.zip', True,
                               recovery_receipts=receipts if scenario == 'receipts' else ())
    controller = TagDatabaseController(choices=(choice,), apply_authorized=False,
                                      executor=DenyingExecutor() if scenario == 'receipts' else None)
    # Explicit supplied-state setup, not application entry or a native preview.
    controller.review = supplied
    app = QApplication.instance() or QApplication([])
    assert app.platformName() == 'windows', 'Native Windows font environment required'
    apply_theme(app, theme)
    pane = TagDatabasePane(controller)
    pane.resize(1280, 1000)
    pane.show()
    def settle():
        for _ in range(8):
            app.processEvents()
            QTest.qWait(10)
    settle()
    pane.source_directory.setText('Synthetic draft source')
    pane.catalog_directory.setText('Synthetic draft catalog')
    before = (session.binding, controller.review, pane.catalog.currentIndex(),
              pane.source_directory.text(), pane.catalog_directory.text(), pane.confirm.isChecked())
    assert not before[-1] and not pane.apply.isEnabled()

    def reveal(widget):
        pane._review_scroll.ensureWidgetVisible(widget)
        widget.setFocus()
        settle()

    if not pane.detail_options.isChecked():
        reveal(pane.detail_options)
        QTest.mouseClick(pane.detail_options, Qt.LeftButton)
        settle()
    reveal(pane.sections)
    # Keyboard selection uses the visible control, never an injected index.
    QTest.keyClick(pane.sections, Qt.Key_Home)
    for _ in range(pane.sections.count()):
        if pane.sections.currentText() == 'Protected user edits':
            break
        QTest.keyClick(pane.sections, Qt.Key_Down)
    assert pane.sections.currentText() == 'Protected user edits'
    reveal(pane.details)
    text = pane.details.toPlainText()
    assert 'Showing 1 of 1 rows' in text and 'fox' in text
    for value in ('My Fox', 'Mine', 'Protected local fields retained', 'Stable ID'):
        assert value in text, (value, text)
    section = next(s for s in project_database_review(db, review).sections if s.title == 'Protected user edits')
    assert section.total == 1 and text == section.text()
    assert '1 protected local edit' in pane.review_summary.text()
    viewport = pane._review_scroll.viewport()
    from PySide6.QtCore import QPoint, QRect
    rect = QRect(pane.details.mapTo(viewport, QPoint()), pane.details.size())
    visible = rect.intersected(viewport.rect())
    assert visible.height() == rect.height() and visible.width() == rect.width()
    capture = output / ('protected-' + theme + '.png')
    assert pane.grab().save(str(capture))
    # A return to the selected advanced section must retain the supplied authority.
    reveal(pane.sections)
    QTest.keyClick(pane.sections, Qt.Key_Up)
    QTest.keyClick(pane.sections, Qt.Key_Down)
    settle()
    assert pane.sections.currentText() == 'Protected user edits'
    assert pane.details.toPlainText() == text
    after = (session.binding, controller.review, pane.catalog.currentIndex(),
             pane.source_directory.text(), pane.catalog_directory.text(), pane.confirm.isChecked())
    assert before == after and not pane.apply.isEnabled() and not calls
    # Canonical no-edits versus unaccepted review contrast; component semantics.
    empty_session = SelectedCatalogSession(Publication('empty-edits', 1, original))
    empty_db = DatabaseActionsController(empty_session, base_release=base, available_release=available)
    empty_review = empty_db.preview(DatabaseAction.MERGE_UPDATE)
    empty = next(s for s in project_database_review(empty_db, empty_review).sections if s.title == 'Protected user edits')
    unknown = next(s for s in project_database_review().sections if s.title == 'Protected user edits')
    assert empty.total == 0 and 'None in this proposal' in empty.text()
    assert unknown.total is None and unknown.text() != empty.text()
    receipt_facts = {}
    extra_captures = {}
    if scenario == 'receipts':
        from dataclasses import replace
        assert pane.receipts.isEnabled() and pane.receipts.count() == 2
        assert not pane.recover.isEnabled() and not pane.apply.isEnabled()
        reveal(pane.receipts)
        QTest.keyClick(pane.receipts, Qt.Key_Home)
        QTest.keyClick(pane.receipts, Qt.Key_Down)
        settle()
        assert pane.receipts.currentData() == receipts[1]
        assert pane.receipts.currentText() == 'Import receipt: run-B'
        # Reprojection is a component assertion, not a native-entry claim.
        pane.render()
        settle()
        assert pane.receipts.currentData() == receipts[1]
        reveal(pane.sections)
        QTest.keyClick(pane.sections, Qt.Key_Up)
        QTest.keyClick(pane.sections, Qt.Key_Down)
        reveal(pane.receipts)
        assert pane.receipts.currentData() == receipts[1]
        selected = output / ('receipts-' + theme + '.png')
        assert pane.grab().save(str(selected))
        extra_captures[selected.name] = hashlib.sha256(selected.read_bytes()).hexdigest()
        # Supply an explicit empty component state; never parse/invoke a receipt.
        controller.choices = (replace(choice, recovery_receipts=()),)
        pane.render()
        settle()
        assert not pane.receipts.isEnabled() and pane.receipts.currentData() is None
        assert pane.receipts.currentText() == 'No approved recovery receipt supplied'
        assert not pane.recover.isEnabled()
        absent = output / ('no-receipts-' + theme + '.png')
        assert pane.grab().save(str(absent))
        extra_captures[absent.name] = hashlib.sha256(absent.read_bytes()).hexdigest()
        rejected = []
        for invalid in (catalog / 'outside.json', catalog / 'backups' / '..' / 'outside.json'):
            try:
                replace(choice, recovery_receipts=(invalid,))
            except ValueError:
                rejected.append(str(invalid))
            else:
                raise AssertionError('Strict receipt Path admission must reject ' + str(invalid))
        assert len(rejected) == 2 and not calls and not submissions
        assert controller.review is supplied and service.current is supplied and db.accepts(review)
        assert before == (session.binding, controller.review, pane.catalog.currentIndex(),
                          pane.source_directory.text(), pane.catalog_directory.text(), pane.confirm.isChecked())
        receipt_facts = {'selected': str(receipts[1]), 'selected_label': 'Import receipt: run-B',
                         'render_return_retained': True, 'empty_disabled': True,
                         'strict_rejections': rejected, 'payload_files_created_or_read': False}
    pane.close()
    settle()
    assert not pane.isVisible() and not controller.close_held and not calls and not submissions
    assert not list(catalog.iterdir()), 'No saved catalog writes allowed'
    report = {'passed': True, 'level': 'source_component', 'theme': theme, 'scenario': scenario,
              'entry': 'explicit supplied-review Pane component precondition',
              'source': str(source), 'source_pins': loaded,
              'platform': app.platformName(), 'size': [pane.width(), pane.height()],
              'dpr': pane.devicePixelRatioF(), 'protected_count': section.total,
              'detail_text': text, 'host_calls': calls, 'unchanged_state': before == after,
              'empty_unknown_distinct': True, 'closed': True,
              'receipt_selection': receipt_facts, 'submissions': submissions, 'extra_captures': extra_captures,
              'capture': capture.name, 'capture_sha256': hashlib.sha256(capture.read_bytes()).hexdigest()}
    (output / 'RESULT.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'passed': True, 'theme': theme, 'report': str(output / 'RESULT.json')}))


def test_supplied_protected_review(tmp_path):
    """Opt-in native-font component check over an explicitly qualified export."""
    _run_fixture(tmp_path, 'protected')


def test_supplied_receipt_selection(tmp_path):
    """Choose exact supplied identities with no receipt payload or recovery."""
    _run_fixture(tmp_path, 'receipts')


def _run_fixture(tmp_path, scenario):
    import pytest
    source = os.environ.get('COLLECTIGEN_PROTECTED_REVIEW_SOURCE')
    if not source:
        pytest.skip('Explicit immutable protected-review source export required')
    for theme in ('dark', 'light'):
        result = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                                 '--source', source, '--output', str(tmp_path / theme), '--theme', theme,
                                 '--scenario', scenario],
                                capture_output=True, encoding='utf-8', timeout=45,
                                env={**os.environ, 'QT_QPA_PLATFORM': 'windows'})
        assert result.returncode == 0, result.stdout + result.stderr


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--theme', choices=('dark', 'light'), required=True)
    parser.add_argument('--scenario', choices=('protected', 'receipts'), default='protected')
    args = parser.parse_args()
    qualify(args.source, args.output, args.theme, args.scenario)
