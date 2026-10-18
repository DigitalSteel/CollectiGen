"""Synthetic published-authority and standalone Qt workflow qualification."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import hashlib
from threading import RLock
import pytest

from suite_core.knowledge import Category, EffectiveTag, Lookup, Snapshot, Source, TagLookup
from suite_core.encyclopedia import publication as codec
from suite_core.encyclopedia.native_writer import capture_and_publish


def fixture(count=1, *, revision="r1", generation=1):
    tags = tuple(EffectiveTag(f"tag:{i}", f"Fox {i:04}", "species", (f"Vulpes {i}",)) for i in range(count))
    rows = tuple(Lookup(f"fox {i:04}", "canonical_name", f"tag:{i}", (f"tag:{i}",)) for i in range(count))
    rows += (Lookup("shared", "ambiguous", owner_ids=("tag:0",)),)
    return codec.Publication("fixture-catalog", generation,
        Snapshot(Source("synthetic approved authority", revision, "fixture", ("species",)),
                 tags, (Category("species", "Species", "Life / Species"),), rows,
                 removed_ids=("retired:1",)))


def test_codec_two_consumers_and_no_unknown_fields(tmp_path):
    publication = fixture()
    path = tmp_path / "published.json"
    path.write_bytes(codec.encode(publication))
    before = path.read_bytes()
    first, second = codec.read(path), codec.read(path)
    assert first == second == publication
    assert first.token == second.token
    assert TagLookup(first.snapshot).resolve("shared").status == "ambiguous"
    assert TagLookup(second.snapshot).get("retired:1").status == "retired"
    assert path.read_bytes() == before
    invalid = json.loads(before)
    invalid["payload"]["snapshot"]["tags"][0]["private_notes"] = "no"
    invalid["payload_sha256"] = hashlib.sha256(codec._json(invalid["payload"])).hexdigest()
    with pytest.raises(codec.PublicationError):
        codec.decode(json.dumps(invalid).encode())
    with pytest.raises(codec.PublicationError):
        codec.decode(b'{"interface_version":1,"interface_version":1}')


def test_atomic_publish_failure_and_stale_native_revision(tmp_path, monkeypatch):
    path = tmp_path / "published.json"
    old = fixture()
    token = codec.publish(old, path, expected_token=None, revision=lambda: "r1", guard=RLock())
    before = path.read_bytes()
    new = fixture(revision="r2", generation=2)
    with pytest.raises(codec.StalePublication):
        codec.publish(new, path, expected_token=token, revision=lambda: "r3", guard=RLock())
    monkeypatch.setattr(codec.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("synthetic replace failure")))
    with pytest.raises(OSError):
        codec.publish(new, path, expected_token=token, revision=lambda: "r2", guard=RLock())
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_publication_tokens_generations_and_changed_during_staging(tmp_path):
    path = tmp_path / "published.json"
    old = fixture()
    token = codec.publish(old, path, expected_token=None, revision=lambda: "r1", guard=RLock())
    new = fixture(revision="r2", generation=2)
    with pytest.raises(codec.StalePublication):
        codec.publish(new, path, expected_token="wrong", revision=lambda: "r2", guard=RLock())
    revisions = iter(("r2", "r3"))
    with pytest.raises(codec.StalePublication):
        codec.publish(new, path, expected_token=token, revision=lambda: next(revisions), guard=RLock())
    assert codec.read(path) == old
    token = codec.publish(new, path, expected_token=token, revision=lambda: "r2", guard=RLock())
    assert codec.read(path) == new
    with pytest.raises(codec.StalePublication):
        codec.require_successor(new, old)
    with pytest.raises(codec.StalePublication):
        codec.require_successor(new, replace(new, catalog_id="different"))
    with pytest.raises(codec.StalePublication):
        codec.require_successor(new, fixture(count=2, revision="r2", generation=2))


def test_explicit_native_host_adapter(tmp_path):
    publication = fixture()
    calls = []
    def capture(**kwargs):
        calls.append(kwargs)
        assert kwargs["revision"]() == kwargs["source"].revision
        return publication.snapshot
    token = capture_and_publish(capture=capture, tags=object(), categories=object(), aliases=object(),
        relationships=(), source=publication.snapshot.source, revision=lambda: "r1", guard=RLock(),
        catalog_id=publication.catalog_id, generation=1, destination=tmp_path / "published.json", expected_token=None)
    assert len(calls) == 1
    assert codec.read(tmp_path / "published.json").token == token


def test_existing_native_capture_adapter_with_synthetic_loaded_authority(tmp_path, monkeypatch):
    import importlib.util
    from types import ModuleType, SimpleNamespace
    root = Path(__file__).resolve().parents[2]
    # Reuse the existing native-fixture namespace pattern: relative leaf imports
    # need a package, while the eager application initializer must stay inactive.
    package_name = "_encyclopedia_synthetic_native"
    package = ModuleType(package_name)
    package.__path__ = [str(root / "core")]
    monkeypatch.setitem(sys.modules, package_name, package)
    name = package_name + ".shared_tag_snapshot"
    spec = importlib.util.spec_from_file_location(name, root / "core/shared_tag_snapshot.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    absent = object()
    app_state_before = sys.modules.get("core.app_state", absent)
    spec.loader.exec_module(module)
    # Undo sibling import registrations too, keeping this loader test isolated.
    for loaded in tuple(sys.modules):
        if loaded.startswith(package_name + ".") and loaded != name:
            sys.modules.pop(loaded)
    # Other public tests may already have loaded the application. This leaf
    # import must neither load it nor replace an existing module.
    assert sys.modules.get("core.app_state", absent) is app_state_before
    record = SimpleNamespace(id="tag:fox", display="Red Fox", category="species", aliases=("Vulpes",))
    tags = SimpleNamespace(tags={record.id: record}, merged_tag_ids={}, removed_tag_ids=set(),
                           effective_identity_projection=lambda: ((), ()))
    categories = SimpleNamespace(categories={"species": SimpleNamespace(id="species", name="Species")}, path_label=lambda _: "Life / Species")
    def resolve(term):
        key = " ".join(term.strip().lower().split())
        return SimpleNamespace(status="canonical_name" if key == "red fox" else "canonical_alias",
                               tag_id=record.id, owner_ids=(record.id,), relationship_ids=(),
                               retired_ids=(), unavailable_ids=())
    aliases = SimpleNamespace(tags=tags, effective_for_tag=lambda _: SimpleNamespace(aliases=("Vulpes",)), resolve=resolve)
    capture_and_publish(capture=module.capture_tag_snapshot, tags=tags, categories=categories,
        aliases=aliases, relationships=(), source=Source("synthetic", "r1", "fixture", ("species",)),
        revision=lambda: "r1", guard=RLock(), catalog_id="fixture", generation=1,
        destination=tmp_path / "published.json", expected_token=None)
    assert TagLookup(codec.read(tmp_path / "published.json").snapshot).resolve("vulpes").tag.id == "tag:fox"


def test_independent_consumer_import():
    root = Path(__file__).resolve().parents[2]
    code = "import sys; sys.path.insert(0,sys.argv[1]); import suite_core.encyclopedia.publication; import suite_core.encyclopedia.native_writer; import suite_core.encyclopedia.browser; allowed={'core','core.component_release_identity'}; assert not any((x == 'core' or x.startswith('core.') or x == 'torch') and x not in allowed for x in sys.modules)"
    subprocess.run([sys.executable, "-I", "-B", "-c", code, str(root)], check=True, capture_output=True)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_visible_browser_workflow_refresh_and_zero_writes(app, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from suite_core.encyclopedia.browser import EncyclopediaWindow
    path = tmp_path / "published.json"
    path.write_bytes(codec.encode(fixture(450)))
    original = path.read_bytes()
    monkeypatch.setattr("suite_core.encyclopedia.browser.QFileDialog.getOpenFileName", lambda *_: (str(path), ""))
    window = EncyclopediaWindow()
    window.resize(1280, 720)
    window.show()
    QTest.mouseClick(window.open_button, Qt.LeftButton)
    assert window.model.rowCount() == 200 and window.model.total_count == 450
    QTest.mouseClick(window.more_button, Qt.LeftButton)
    assert window.model.rowCount() == 400
    window.search.setText("lpes 44")
    assert window.model.total_count == 11
    window.search.clear()
    window.table.setCurrentIndex(window.model.index(0, 0))
    QTest.mouseClick(window.advanced, Qt.LeftButton)
    assert "Stable ID: tag:0" in window.details.toPlainText()
    window.search.setText("shared")
    QTest.mouseClick(window.resolve_button, Qt.LeftButton)
    assert "ambiguous" in window.details.toPlainText()
    window.search.setText("retired:1")
    QTest.keyClick(window.search, Qt.Key_Return)
    assert "retired" in window.details.toPlainText()
    window.search.setText("absent")
    QTest.mouseClick(window.resolve_button, Qt.LeftButton)
    assert "missing" in window.details.toPlainText()
    window.search.clear()
    assert path.read_bytes() == original
    path.write_bytes(codec.encode(fixture(450, revision="r2", generation=2)))
    QTest.mouseClick(window.refresh_button, Qt.LeftButton)
    assert "revision r2" in window.identity.text()
    path.write_bytes(original)
    QTest.mouseClick(window.refresh_button, Qt.LeftButton)
    assert window.stale and window.publication.generation == 2
    assert "Older snapshot" in window.status.text()
    path.unlink()
    QTest.mouseClick(window.refresh_button, Qt.LeftButton)
    assert window.stale and window.publication.generation == 2
    assert not list(tmp_path.iterdir())
    window.close()


def test_partial_scope_and_category_filter(app, tmp_path):
    from suite_core.encyclopedia.browser import EncyclopediaWindow
    publication = fixture()
    snapshot = replace(publication.snapshot, source=replace(publication.snapshot.source, completeness="partial"))
    path = tmp_path / "published.json"
    path.write_bytes(codec.encode(replace(publication, snapshot=snapshot)))
    window = EncyclopediaWindow()
    assert window.open_snapshot(path)
    window.category.setCurrentIndex(1)
    assert window.model.total_count == 1
    window.search.setText("absent")
    window.resolve_query()
    assert "unavailable" in window.details.toPlainText()
    window.close()


def test_two_browser_consumers_and_20k_scope(app, tmp_path, monkeypatch):
    from suite_core.encyclopedia.browser import EncyclopediaWindow
    publication = fixture(20_000)
    path = tmp_path / "published.json"
    path.write_bytes(codec.encode(publication))
    original = path.read_bytes()
    opened = []
    original_open = Path.open
    def read_only_open(self, mode="r", *args, **kwargs):
        assert self == path and mode == "rb"
        opened.append(self)
        return original_open(self, mode, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", read_only_open)
        first, second = EncyclopediaWindow(), EncyclopediaWindow()
        assert first.open_snapshot(path) and second.open_snapshot(path)
        assert first.publication.token == second.publication.token == publication.token
        assert first.model.rowCount() == 200 and first.model.total_count == 20_000
        first.search.setText("Fox 19999")
        assert first.model.total_count == 1
        first.resolve_query()
        assert "Preferred label: Fox 19999" in first.details.toPlainText()
        assert "tag:19999" not in first.details.toPlainText()
        first.advanced.setChecked(True)
        assert "tag:19999" in first.details.toPlainText()
        assert second.model.total_count == 20_000
        first.close()
        second.close()
    assert len(opened) == 2 and path.read_bytes() == original


def test_extracted_component_preserves_existing_implementation():
    import ast
    root = Path(__file__).resolve().parents[2]
    leaf = ast.parse((root / "qt_app/browse_components.py").read_text(encoding="utf-8-sig"))
    components = (root / "qt_app/components.py").read_text(encoding="utf-8-sig")
    assert "from .browse_components import IncrementalResultRow, IncrementalResultModel" in components
    assert {node.name for node in leaf.body if isinstance(node, ast.ClassDef)} == {"IncrementalResultRow", "IncrementalResultModel"}


def test_developer_entrypoint_version_and_demo_refuses_overwrite(tmp_path):
    from suite_core.encyclopedia.version import SOFTWARE_VERSION, RELEASE_DATE
    root = Path(__file__).resolve().parents[2]
    notes = (root / "suite_core/encyclopedia/WHATS_NEW.md").read_text()
    assert SOFTWARE_VERSION in notes and RELEASE_DATE in notes
    result = subprocess.run([sys.executable, "-B", "-m", "suite_core.encyclopedia", "--help"],
                            cwd=root, capture_output=True, text=True, check=True)
    assert "--snapshot" in result.stdout
    path = tmp_path / "demo.json"
    command = [sys.executable, "-B", "-m", "suite_core.encyclopedia.demo", str(path)]
    subprocess.run(command, cwd=root, check=True, capture_output=True)
    before = path.read_bytes()
    assert codec.read(path).catalog_id == "synthetic-demo"
    assert subprocess.run(command, cwd=root, capture_output=True).returncode != 0
    assert path.read_bytes() == before
