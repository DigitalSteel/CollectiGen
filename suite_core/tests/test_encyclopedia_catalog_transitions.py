"""Focused source delivery state tests; native read/write authority tested separately."""
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

from suite_core.tests.test_encyclopedia import app, fixture
from suite_core.tests.test_encyclopedia_catalog_ui import settle
from suite_core.encyclopedia.__main__ import create_window
from suite_core.encyclopedia import publication as codec


def publication(identity, revision='r1', generation=1):
    return replace(fixture(2, revision=revision, generation=generation), catalog_id=identity)


def test_accepted_export_refresh_wins_over_old_or_cancelled_native_cache(app, tmp_path, monkeypatch):
    window = create_window(settings_path=tmp_path / 'settings.json')
    host = window.catalog_host
    root = tmp_path / 'native'
    native = publication('native')
    def load(path, *args):
        view = SimpleNamespace(root=path, publication=native, stamp=(1, 2))
        host.service.view = view
        return view
    monkeypatch.setattr(host.service, 'open_catalog', load)
    host.open_database(root); settle(app, host)
    old_view = host.service.view
    exported = tmp_path / 'export.json'
    exported.write_bytes(codec.encode(publication('export')))
    assert window.open_snapshot(exported)
    host.service.view = old_view  # Simulate a read cache; it is never refresh authority.
    exported.write_bytes(codec.encode(publication('export', 'r2', 2)))
    assert host.refresh() and window.publication.catalog_id == 'export'
    entered, release = Event(), Event()
    def delayed(path, *args):
        entered.set(); assert release.wait(10)
        return load(path, *args)
    monkeypatch.setattr(host.service, 'open_catalog', delayed)
    host.open_database(root); assert entered.wait(2)
    exported.write_bytes(codec.encode(publication('export', 'r3', 3)))
    assert window.open_snapshot(exported, refreshing=True)
    accepted = host.accepted_source
    host.cancel(); release.set(); settle(app, host)
    assert host.accepted_source is accepted and window.publication.catalog_id == 'export'
    assert host.service.view is None
    exported.write_bytes(codec.encode(publication('export', 'r4', 4)))
    assert host.refresh() and window.publication.generation == 4
    window.close(); app.processEvents()


def test_native_delivery_refusal_keeps_accepted_source_and_error(app, tmp_path, monkeypatch):
    window = create_window(settings_path=tmp_path / 'settings.json')
    host = window.catalog_host; root = tmp_path / 'native'
    observed = [publication('native')]
    def load(path, *args):
        from core.encyclopedia_catalog import EncyclopediaCatalogView
        view = EncyclopediaCatalogView(path, None, observed[0], (1, 2))
        host.service.view = view
        return view
    monkeypatch.setattr(host.service, 'open_catalog', load)
    host.open_database(root); settle(app, host)
    assert window.publication is observed[0], window.status.text()
    assert host.accepted_source is not None
    assert host.service.view.adapter is None  # No writer authority in this fixture.
    accepted, before = host.accepted_source, host.service.view
    def refuse(*args, **kwargs):
        window.status.setText('Reader refused this native view')
        return False
    monkeypatch.setattr(window, 'open_publication', refuse)
    observed[0] = publication('native', 'r2', 2)
    host.refresh(); settle(app, host)
    assert host.accepted_source is accepted and host.service.view is before
    assert window.publication.snapshot.source.revision == 'r1'
    assert window.status.text() == 'Reader refused this native view'
    window.close(); app.processEvents()


def test_controller_delivery_refusal_never_relabels_export_as_native(app, tmp_path, monkeypatch):
    from suite_core.knowledge.tag_transactions import AppliedCatalog
    window = create_window(settings_path=tmp_path / 'settings.json'); host = window.catalog_host
    path = tmp_path / 'export.json'; path.write_bytes(codec.encode(publication('export')))
    window.open_snapshot(path); accepted = host.accepted_source
    host.controller.choices = (SimpleNamespace(native_adapter=SimpleNamespace(root=tmp_path / 'native', directory_pin=(1, 2)),
        transactions=SimpleNamespace(destination=tmp_path / 'native/snapshot.json')),)
    host.controller.index = 0
    host.controller.result = AppliedCatalog({}, publication('native'))
    host.controller.last_operation = 'apply'
    def refuse(*args, **kwargs):
        window.status.setText('Reader refused changed catalog')
        return False
    monkeypatch.setattr(window, 'open_publication', refuse)
    host.poll()
    assert host.accepted_source is accepted and window.publication.catalog_id == 'export'
    assert window.status.text() == 'Reader refused changed catalog'
    window.close(); app.processEvents()


def test_exact_alias_copy_uses_displayed_tag_and_unresolved_clears_action(app, tmp_path):
    from suite_core.knowledge import Lookup
    window = create_window(settings_path=tmp_path / 'settings.json')
    sample = fixture(2)
    sample = replace(sample, snapshot=replace(sample.snapshot, lookups=sample.snapshot.lookups +
        (Lookup('vulpes 1', 'canonical_alias', 'tag:1', ('tag:1',)),)))
    window.open_publication(sample)
    window.table.setCurrentIndex(window.model.index(0, 0))
    window.search.setText('Vulpes 1'); window.resolve_query()
    window.copy_tag()
    assert window._selected_id == 'tag:1' and app.clipboard().text() == 'Fox 0001'
    window.search.setText('missing-name'); window.resolve_query()
    assert not window._selected_id
    assert not next(c for c in window.commands if c.label == 'Copy Tag Name').available()
    window.copy_tag(); assert app.clipboard().text() == 'Fox 0001'
    window.close(); app.processEvents()


def test_identity_is_embedded_advanced_lookup_and_compact_empty(app, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QMessageBox
    shown = []
    monkeypatch.setattr(QMessageBox, 'information', lambda *args: shown.append(args[2]))
    window = create_window(settings_path=tmp_path / 'settings.json')
    window.show(); app.processEvents()
    assert not window.browse_panes.isVisible() and window.empty_hint.isVisible()
    assert 'IDs' not in window.search.placeholderText() and not window.resolve_button.isVisible()
    window.open_publication(fixture(2))
    QTest.mouseClick(window.advanced, Qt.LeftButton); app.processEvents()
    assert window.identity.window() is window and window.identity.isVisible()
    assert window.resolve_button.isVisible()
    window.help_commands.whats_new()
    assert '2026-10-12' in shown[0] and 'Dated version history' in shown[0]
    from pathlib import Path
    from suite_core.encyclopedia import __main__ as entry
    current_notes = (Path(entry.__file__).parent / "WHATS_NEW.md").read_text(encoding="utf-8").split("\n# ", 1)[0].strip()
    assert shown[0].split("\n\nDated version history\n", 1)[0] == current_notes
    assert "# Tag Encyclopedia a.0.2.2 - Alpha candidate" not in shown[0]
    window.close(); app.processEvents()
