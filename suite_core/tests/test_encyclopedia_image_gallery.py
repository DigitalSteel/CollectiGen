"""Actual source widgets with synthetic PNGs and borrowed memory-only services."""
from pathlib import Path
from types import SimpleNamespace
import os

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QColor, QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget, QHBoxLayout

from core.federated_dataset import LibraryImageIdentity
from suite_core.media_organizer.contracts import ExistingContext, SERVICE_NAMES
from suite_core.media_organizer.facade import MediaOrganizer
from suite_core.tests.test_media_examples import query_service, SyntheticIndex
from suite_core.tests.test_encyclopedia_hosted_examples_candidate import Bus, Task, Federation
from suite_core.tests.test_selected_catalog import publication
from qt_app.theme import apply_theme


@pytest.fixture
def app():
    from tools.qt_test_support import QtTestScope
    from qt_app.confidence import STATUS_COLORS
    colors = dict(STATUS_COLORS)
    with QtTestScope() as scope:
        if not QFontDatabase.families() and os.name == 'nt':
            assert QFontDatabase.addApplicationFont(str(Path(os.environ['WINDIR']) / 'Fonts/segoeui.ttf')) >= 0
        scope.app.setFont(QFont('Segoe UI', 10))
        try:
            yield scope.app
        finally:
            STATUS_COLORS.clear()
            STATUS_COLORS.update(colors)


@pytest.fixture
def surface(app, tmp_path, query_service, monkeypatch):
    from suite_core.encyclopedia import browser as module
    from suite_core.media_organizer.encyclopedia_image_gallery import HostedEncyclopediaImageGallery
    monkeypatch.setattr(module, 'read', lambda path: publication())
    browser = module.EncyclopediaWindow()
    browser.open_snapshot('synthetic-publication.json')
    paths = {}
    for library, count in [('a', 13), ('b', 1)]:
        for n in range(count):
            name = 'same.png' if n == 0 else f'example-{n:02}.png'
            path = tmp_path / library / name
            path.parent.mkdir(exist_ok=True)
            image = QImage(96, 96, QImage.Format_RGB32)
            image.fill(QColor('#3b82f6' if library == 'a' else '#f59e0b'))
            assert image.save(str(path))
            paths[LibraryImageIdentity(library, name)] = path
    participants = [(SimpleNamespace(stable_id=library), SimpleNamespace(index=SyntheticIndex(library,
        [(identity.relative_path, ('Red Fox',)) for identity in paths if identity.library_id == library])))
        for library in ('a', 'b')]
    dataset = SimpleNamespace(scope_library_ids=('a', 'b'), images=list(paths.values()),
        scoped_profile_managers=lambda: tuple(p for p in participants if p[0].stable_id in dataset.scope_library_ids),
        path_for_identity=lambda identity: paths[identity], storage_availability=lambda path: SimpleNamespace(readable=True))
    def identity(path):
        text = str(path)
        if text.startswith('/synthetic/'):
            parts = text.split('/')
            return LibraryImageIdentity(parts[2], parts[3])
        return next((key for key, file in paths.items() if file == Path(path)), None)
    dataset.identity_for_path = identity
    dataset.index = Federation(dataset, query_service)
    tag = SimpleNamespace(id='shared-id', display='Red Fox', aliases=('Vulpes',), category='animals')
    knowledge = SimpleNamespace(tags={tag.id: tag}, identity_names=lambda key: ('Red Fox', 'Vulpes'))
    settings = object()
    state = SimpleNamespace(settings=settings, dataset=dataset, evidence_queries=query_service,
        knowledge=knowledge, events=Bus(), jobs=SimpleNamespace(tasks=[]))
    for name in SERVICE_NAMES:
        if not hasattr(state, name):
            setattr(state, name, object())
    opened, gui = [], []
    index = [2]
    host = SimpleNamespace(state=state, settings=settings, startup_cancelled=False,
        navigation=SimpleNamespace(setCurrentRow=lambda value: index.__setitem__(0, value)),
        stack=SimpleNamespace(currentIndex=lambda: index[0]), choose_library=lambda: None,
        open_completed_dataset_image=lambda path: opened.append(path) or True,
        edit_image_data=lambda path: pytest.fail('No image edit is permitted'), close=lambda: None)
    organizer = MediaOrganizer(ExistingContext(settings, state), host)
    binding = browser.attach_hosted_library_session(state, admitted_publication=browser.publication,
        task_factory=Task, post_to_gui=gui.append)
    gallery = HostedEncyclopediaImageGallery(browser, binding, organizer)
    browser.search.setText('shared-id')
    QTest.keyClick(browser.search, Qt.Key_Return)
    root = QWidget()
    root.resize(1920, 1080)
    body = QHBoxLayout(root)
    body.addWidget(browser, 1)
    body.addWidget(gallery, 1)
    root.show()
    app.processEvents()
    value = SimpleNamespace(root=root, browser=browser, gallery=gallery, state=state,
        binding=binding, gui=gui, paths=paths, opened=opened)
    yield value
    gallery.close()
    browser.close()
    root.close()
    app.processEvents()


def find(surface, app):
    QTest.mouseClick(surface.browser.examples_button, Qt.LeftButton)
    task = surface.state.jobs.tasks[-1]
    task.finish()
    while surface.gui:
        surface.gui.pop(0)()
    app.processEvents()


def test_find_selection_open_and_paging_use_original_owner(surface, app):
    s = surface
    assert not s.gallery.gallery.cards and not s.opened
    find(s, app)
    assert len(s.gallery.gallery.visible_paths()) == 12
    first = next(iter(s.gallery.gallery.cards.values()))
    assert not first.allow_data_edit and not first.allow_ai_actions and not first.allow_duplicate_check
    assert not first.allow_generation_actions and not first._file_actions
    assert not s.gallery.gallery.open_dataset.isEnabled()
    QTest.mouseClick(first.preview, Qt.LeftButton)
    assert s.gallery.gallery.open_dataset.isEnabled()
    assert not s.opened
    QTest.mouseClick(s.gallery.gallery.open_dataset, Qt.LeftButton)
    assert s.opened == [str(next(iter(s.paths.values())))]
    QTest.mouseClick(s.gallery.next_page, Qt.LeftButton)
    assert list(s.gallery.gallery.visible_paths()) == list(s.paths.values())[12:]
    assert not s.gallery.next_page.isEnabled()


def capture_outcome(surface,app,tmp_path,name):
    app.processEvents()
    path=tmp_path/(name+'.png')
    assert surface.root.grab().save(str(path))
    print('HOSTED_OUTCOME_CAPTURE='+str(path))


def test_zero_matches_has_honest_empty_state(surface, app,tmp_path):
    for profile,manager in surface.state.dataset.scoped_profile_managers():
        manager.index = SyntheticIndex(profile.stable_id, [])
    find(surface, app)
    assert not surface.gallery.gallery.visible_paths()
    # Original browser owns EMPTY outcomes; observers receive nonempty SUCCESS.
    assert 'No cached examples match' in surface.browser.examples_status.text()
    assert '1-0' not in surface.gallery.page_status.text()
    assert not surface.gallery.next_page.isEnabled()
    assert not surface.gallery.gallery.open_dataset.isEnabled()
    assert not surface.opened
    capture_outcome(surface,app,tmp_path,'empty-query')


def test_zero_page_renderer_has_clear_empty_state(surface,app,monkeypatch):
    # Component-only presentation; no result/authority is fabricated or admitted.
    monkeypatch.setattr(surface.gallery.bridge,'page',lambda offset:SimpleNamespace(
        paths=(),references=(),offset=0,next_offset=0,total=0,unavailable=0))
    surface.gallery.show_next_page()
    assert surface.gallery.page_status.text() == 'No matching examples.'
    assert 'No matching example images' in surface.gallery.gallery.status.text()
    assert not surface.gallery.next_page.isEnabled()
    assert not surface.gallery.gallery.open_dataset.isEnabled()


def test_unavailable_page_preserves_pagination_and_does_not_open(surface, app,tmp_path):
    surface.state.dataset.storage_availability = lambda path: SimpleNamespace(readable=False)
    find(surface, app)
    outcome='14 cached example references. File availability and image facts are unverified.'
    assert surface.browser.examples_status.text() == outcome
    assert not surface.gallery.gallery.visible_paths()
    assert surface.gallery.page_status.text() == '12 unavailable in this page; 14 matches in total.'
    assert surface.gallery.next_page.isEnabled()
    assert not surface.gallery.gallery.open_dataset.isEnabled()
    capture_outcome(surface,app,tmp_path,'unavailable-page')
    QTest.mouseClick(surface.gallery.next_page, Qt.LeftButton)
    assert surface.browser.examples_status.text() == outcome
    assert surface.gallery.page_status.text() == '2 unavailable in this page; 14 matches in total.'
    assert not surface.gallery.next_page.isEnabled()
    assert not surface.opened


@pytest.mark.parametrize('provider',['ready','disconnected','unavailable'])
def test_held_guidance_requires_existing_retry_capability(surface,app,provider,tmp_path):
    ready = provider == 'ready'
    if provider == 'disconnected':
        surface.browser.detach_hosted_library_session()
        assert 'Choose Find Examples' not in surface.gallery.gallery.status.text()
    elif provider == 'unavailable':surface.binding.close()
    else:assert surface.browser.examples_button.isEnabled()
    surface.gallery._held()
    message=surface.gallery.gallery.status.text()
    assert 'Reconnect' not in message
    assert ('Choose Find Examples' in message) is ready
    assert not surface.gallery.next_page.isEnabled()
    assert not surface.gallery.gallery.open_dataset.isEnabled()
    assert not surface.opened
    capture_outcome(surface,app,tmp_path,'held-'+provider)
    if ready:
        find(surface,app)
        assert len(surface.gallery.gallery.visible_paths()) == 12


def test_detached_gallery_does_not_offer_retry(surface,app):
    surface.gallery.detach()
    surface.gallery._held()
    assert 'Choose Find Examples' not in surface.gallery.gallery.status.text()
    assert not surface.gallery.gallery.open_dataset.isEnabled()


def test_repeated_cancel_retry_preserves_outcomes_and_rejects_late_results(surface,app,tmp_path):
    for _ in range(2):
        QTest.mouseClick(surface.browser.examples_button,Qt.LeftButton)
        old=surface.state.jobs.tasks[-1]
        assert not surface.browser.examples_button.isEnabled()
        QTest.mouseClick(surface.browser.examples_cancel,Qt.LeftButton)
        outcome=surface.browser.examples_status.text()
        assert outcome.startswith('Cancelled.')
        assert surface.browser.examples_button.isEnabled()
        assert 'Choose Find Examples' in surface.gallery.gallery.status.text()
        old.finish()
        while surface.gui:surface.gui.pop(0)()
        assert surface.browser.examples_status.text() == outcome
        assert not surface.gallery.gallery.visible_paths()
        assert not surface.gallery.gallery.open_dataset.isEnabled()
        capture_outcome(surface,app,tmp_path,'cancelled-retry')
        find(surface,app)
        assert len(surface.gallery.gallery.visible_paths()) == 12
        assert 'File availability and image facts are unverified.' in surface.browser.examples_status.text()
    assert not surface.opened


@pytest.mark.parametrize('state',['pending','stale','unbound','disconnected'])
def test_capability_invalidation_ignores_stale_button_state(surface,app,state):
    # Component matrix supplements the real Cancel notification-order test.
    surface.browser.examples_button.setEnabled(True)
    if state=='pending':surface.browser._example_pending=object()
    elif state=='stale':surface.browser.stale=True
    elif state=='unbound':surface.binding.catalog_token='changed-catalog'
    else:surface.browser.detach_hosted_library_session()
    outcome=surface.browser.examples_status.text()
    surface.gallery.invalidate()
    assert 'Choose Find Examples' not in surface.gallery.gallery.status.text()
    assert surface.browser.examples_status.text() == outcome
    surface.browser._example_pending=None


def test_cancel_late_delivery_and_detach_clear_images(surface, app):
    s = surface
    QTest.mouseClick(s.browser.examples_button, Qt.LeftButton)
    old = s.state.jobs.tasks[-1]
    QTest.mouseClick(s.browser.examples_cancel, Qt.LeftButton)
    old.finish()
    while s.gui:
        s.gui.pop(0)()
    assert not s.gallery.gallery.visible_paths()
    find(s, app)
    old_path = str(next(iter(s.paths.values())))
    s.gallery.detach()
    assert s.gallery.open_path(old_path) is False and not s.opened
    assert not s.gallery.gallery.visible_paths()


def test_images_do_not_enter_publication_or_change_tag_order(surface, app):
    from suite_core.encyclopedia.publication import encode
    before = encode(surface.browser.publication)
    model = surface.browser.model
    tag_order = [model.stable_id(model.index(n, 0)) for n in range(model.rowCount())]
    find(surface, app)
    QTest.mouseClick(surface.gallery.next_page, Qt.LeftButton)
    assert encode(surface.browser.publication) == before
    assert [model.stable_id(model.index(n, 0)) for n in range(model.rowCount())] == tag_order
    assert b'same.png' not in before and str(next(iter(surface.paths.values()))).encode() not in before


def test_closed_browser_rejects_late_gallery_delivery(surface, app):
    QTest.mouseClick(surface.browser.examples_button, Qt.LeftButton)
    old = surface.state.jobs.tasks[-1]
    surface.browser.close()
    old.finish()
    while surface.gui:
        surface.gui.pop(0)()
    assert not surface.gallery.gallery.visible_paths()
    assert not surface.opened


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_source_1080p_gallery_capture(surface, app, tmp_path, theme):
    apply_theme(app, theme, color_blind_mode=True)
    find(surface, app)
    assert surface.gallery.gallery.card_grid.pool.waitForDone(5000)
    app.processEvents()
    for card in surface.gallery.gallery.cards.values():
        assert card.width() >= 136
        assert card.path.name  # Synthetic filename only.
        assert card.preview.pixmap() is not None and not card.preview.pixmap().isNull()
    capture = tmp_path / ('hosted-examples-' + theme + '.png')
    assert surface.root.grab().save(str(capture))
    print('HOSTED_EXAMPLES_CAPTURE=' + str(capture))


@pytest.mark.parametrize("named",[False,True])
def test_source_badge_uses_registry_name_or_honest_label_and_keeps_full_id(surface,app,named):
    from core.library_profiles import LibraryProfile,LibraryProfileRegistry
    if named:
        surface.state.dataset.registry=LibraryProfileRegistry([
            LibraryProfile(stable_id=key,name="Synthetic Library "+key.upper(),root=str(next(path.parent for identity,path in surface.paths.items() if identity.library_id==key)))
            for key in ("a","b")])
    find(surface,app)
    assert surface.gallery.gallery.card_grid.pool.waitForDone(5000)
    app.processEvents()
    for card in surface.gallery.gallery.cards.values():
        reference=surface.gallery._references[str(card.path)]
        expected="Synthetic Library "+reference.library_id.upper() if named else "Included library"
        assert card.library_label.text()==expected
        assert "Library ID: "+reference.library_id in card.library_label.toolTip()
        assert reference.library_id==surface.state.dataset.identity_for_path(card.path).library_id
