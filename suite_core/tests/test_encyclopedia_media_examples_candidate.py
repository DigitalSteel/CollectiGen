"""Candidate interaction checks using fake controls only; never import Qt."""
from dataclasses import dataclass, replace
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import pytest

from suite_core.tests.test_selected_catalog import publication
from suite_core.media_examples import ExampleResult, ExampleStatus, MediaReference
from suite_core.tests.test_media_examples import host, query_service


class Signal:
    def __init__(self): self.callbacks = []
    def connect(self, callback): self.callbacks.append(callback)
    def emit(self, *args):
        for callback in self.callbacks: callback(*args)


class Control:
    def __init__(self, *args, **kwargs):
        self.value = args[0] if args and isinstance(args[0], str) else ''
        self.enabled = True
        self.items = []
        self.position = 0
        for name in ('clicked', 'textChanged', 'returnPressed', 'currentIndexChanged', 'currentChanged',
                     'toggled', 'currentRowChanged', 'searchRequested'):
            setattr(self, name, Signal())
    def __getattr__(self, name):
        if name.startswith(('set', 'add', 'resize', 'block')): return lambda *args, **kwargs: None
        raise AttributeError(name)
    def setEnabled(self, value): self.enabled = value
    def click(self):
        if self.enabled: self.clicked.emit()
    def setText(self, value): self.value = value; self.textChanged.emit(value)
    def text(self): return self.value
    def setPlainText(self, value): self.value = value
    def hide(self): pass
    def show(self): pass
    def clearSelection(self): pass
    def isChecked(self): return False
    def clear(self): self.items = []; self.value = ''
    def font(self): return Control()
    def horizontalHeader(self): return Control()
    def selectionModel(self): return self
    def setModel(self, model): self.model = model
    def setCurrentIndex(self, index):
        if isinstance(index, int): self.position = index
        else: self.currentChanged.emit(index, Index())
    def addItem(self, label, data=None): self.items.append((label, data))
    def count(self): return len(self.items)
    def itemText(self, index): return self.items[index][0]
    def currentData(self): return self.items[self.position][1]
    def currentIndex(self): return self.position
    def findData(self, data): return next((i for i, item in enumerate(self.items) if item[1] == data), -1)
    def closeEvent(self, event): pass


class Index:
    def __init__(self, identity=''): self.identity = identity
    def isValid(self): return bool(self.identity)


@dataclass
class Row:
    stable_id: str
    columns: tuple
    tooltip: str = ''


class Model:
    def __init__(self, *args, page_size=200):
        self.rows = []; self.counts_changed = Signal(); self.page_size = page_size; self.loaded = 0
    def set_rows(self, rows, **kwargs):
        self.rows = list(rows); self.loaded = min(len(self.rows), self.page_size)
        self.counts_changed.emit(self.loaded, len(self.rows))
    def fetch_more(self):
        self.loaded = min(len(self.rows), self.loaded + self.page_size)
        self.counts_changed.emit(self.loaded, len(self.rows))
    def index_for_id(self, identity): return Index(identity if any(row.stable_id == identity for row in self.rows) else '')
    def stable_id(self, index): return index.identity


@pytest.fixture
def browser(monkeypatch):
    # All Qt and shared-model imports replaced before candidate import. No native
    # widget, QApplication, thumbnail, reader or file-dialog is ever constructed.
    qtcore = ModuleType('PySide6.QtCore'); qtcore.Qt = SimpleNamespace(PlainText=0)
    widgets = ModuleType('PySide6.QtWidgets')
    for name in ('QComboBox QFileDialog QHBoxLayout QLabel QLineEdit QMainWindow QPushButton QSplitter QTableView QTextEdit QVBoxLayout QWidget QListWidget QApplication QCheckBox').split():
        setattr(widgets, name, Control)
    widgets.QSizePolicy = SimpleNamespace(Maximum=1, Preferred=2, Expanding=3)
    widgets.QAbstractItemView = SimpleNamespace(SelectRows=1, SingleSelection=2, NoEditTriggers=3)
    shared = ModuleType('qt_app.browse_components')
    shared.IncrementalResultModel, shared.IncrementalResultRow = Model, Row
    commandbar = ModuleType('suite_core.manager.command_bar')
    commandbar.CommandBar = commandbar.CommandSearchDialog = Control
    commandbar.UiCommand = lambda *args, **kwargs: SimpleNamespace(key=args[0], label=args[1], group=args[2], **kwargs)
    for name, module in {'PySide6': ModuleType('PySide6'), 'PySide6.QtCore': qtcore,
                         'PySide6.QtWidgets': widgets, 'qt_app.browse_components': shared,
                         'suite_core.manager.command_bar': commandbar}.items():
        monkeypatch.setitem(sys.modules, name, module)
    path = Path(__file__).with_name('browser.py')
    if not path.is_file():
        path = Path(__file__).resolve().parents[1] / 'encyclopedia/browser.py'
    spec = importlib.util.spec_from_file_location('suite_core.encyclopedia._media_candidate', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'read', lambda _: publication())
    window = module.EncyclopediaWindow()
    window.open_snapshot('synthetic-publication.json')  # reader replaced with DTO
    return window, module


class Provider:
    def __init__(self): self.calls = []; self.valid = True; self.status = ExampleStatus.SUCCESS
    def query(self, request, *, should_continue):
        self.calls.append(request)
        assert should_continue()
        return ExampleResult(request, self.status, (MediaReference('a', 'same.png'), MediaReference('b', 'same.png')))
    def accepts(self, result): return self.valid


def ready(browser):
    window, module = browser
    provider, jobs = Provider(), []
    window.configure_examples(provider=provider, library_ids=('a', 'b'), submit=lambda work, deliver: jobs.append((work, deliver)))
    window.table.setCurrentIndex(Index('shared-id'))
    return window, module, provider, jobs


def finish(jobs):
    work, deliver = jobs[-1]; result = work(); deliver(result); return result


def test_default_entry_has_honest_no_library_state(browser):
    window, _ = browser
    assert 'No library connected' in window.examples_status.text()
    assert not window.examples_button.enabled
    assert not window.examples_cancel.enabled


def test_visible_selection_query_delivery_retains_library_identity(browser):
    window, _, provider, jobs = ready(browser)
    window.examples_button.click()
    assert window.examples_cancel.enabled and 'Finding' in window.examples_status.text()
    result = finish(jobs)
    assert provider.calls == [result.request]
    assert result.request.knowledge_revision == window.example_catalog_token()
    assert result.request.identities[0].names == ('Red Fox', 'Vulpes')
    assert [row.columns for row in window.examples_model.rows] == [('a', 'same.png'), ('b', 'same.png')]
    assert 'unverified' in window.examples_status.text()


def test_exact_alias_resolution_is_real_example_entry(browser):
    window, _, _, jobs = ready(browser)
    window.search.setText('Vulpes')
    window.resolve_button.click(); window.examples_button.click(); finish(jobs)
    assert len(window.examples_model.rows) == 2


@pytest.mark.parametrize('status, text', [(ExampleStatus.EMPTY, 'No cached examples'),
    (ExampleStatus.UNAVAILABLE, 'unavailable'), (ExampleStatus.ERROR, 'could not be queried'),
    (ExampleStatus.CANCELLED, 'Cancelled'), (ExampleStatus.STALE, 'stale')])
def test_distinct_non_success_states_discard_even_provider_partial_references(browser, status, text):
    window, _, provider, jobs = ready(browser); provider.status = status
    window.examples_button.click(); finish(jobs)
    assert text in window.examples_status.text() and not window.examples_model.rows
    assert window.examples_button.enabled and not window.examples_cancel.enabled


def test_cancel_control_discards_late_delivery_and_allows_retry(browser):
    window, _, _, jobs = ready(browser); window.examples_button.click()
    work, deliver = jobs[-1]; result = work()
    window.examples_cancel.click(); deliver(result)
    assert not window.examples_model.rows and 'Cancelled' in window.examples_status.text()
    window.examples_button.click(); finish(jobs); assert len(window.examples_model.rows) == 2


@pytest.mark.parametrize('change', ['tag', 'scope', 'catalog', 'filter', 'close', 'refresh_failure'])
def test_late_delivery_rejected_after_context_changes(browser, change):
    window, module, provider, jobs = ready(browser); window.examples_button.click()
    work, deliver = jobs[-1]; result = work(); token = window.example_selection_token()
    if change == 'tag': window.table.setCurrentIndex(Index('shared-id'))
    elif change == 'scope': window.configure_examples(provider=provider, library_ids=('b',), submit=lambda w, d: jobs.append((w, d)))
    elif change == 'catalog':
        module.read = lambda _: publication('catalog-b'); window.open_snapshot('other-synthetic.json')
    elif change == 'filter': window.search.setText('no matching tag')
    elif change == 'close': window.closeEvent(object())
    else:
        module.read = lambda _: (_ for _ in ()).throw(ValueError('synthetic refusal'))
        assert not window.refresh()
    deliver(result)
    assert token != window.example_selection_token() and not window.examples_model.rows


def test_delivery_time_provider_freshness_and_foreign_request_hold(browser):
    window, _, provider, jobs = ready(browser); window.examples_button.click()
    work, deliver = jobs[-1]; result = work(); provider.valid = False; deliver(result)
    assert 'stale' in window.examples_status.text() and not window.examples_model.rows
    provider.valid = True; window.examples_button.click()
    work, deliver = jobs[-1]; result = work()
    deliver(replace(result, request=replace(result.request, knowledge_revision='foreign')))
    assert 'stale' in window.examples_status.text() and not window.examples_model.rows


def test_query_and_scheduler_errors_are_private_and_recoverable(browser):
    window, _, provider, jobs = ready(browser)
    provider.query = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('PRIVATE'))
    window.examples_button.click(); finish(jobs)
    assert 'PRIVATE' not in window.examples_status.text() and 'could not be queried' in window.examples_status.text()
    window.configure_examples(provider=provider, library_ids=('a',), submit=lambda *a: (_ for _ in ()).throw(RuntimeError('PRIVATE')))
    window.examples_button.click()
    assert 'could not be scheduled' in window.examples_status.text() and window.examples_button.enabled


def test_reentrant_older_delivery_cannot_clear_new_job(browser):
    window, _, _, jobs = ready(browser); window.examples_button.click()
    work, deliver = jobs[-1]; old = work(); window.examples_cancel.click(); window.examples_button.click()
    deliver(old); assert window.examples_cancel.enabled
    finish(jobs); assert len(window.examples_model.rows) == 2


def test_paging_uses_existing_shared_model_contract(browser):
    window, _, provider, jobs = ready(browser)
    provider.query = lambda request, **kw: ExampleResult(request, ExampleStatus.SUCCESS,
        tuple(MediaReference('a', f'{i}.png') for i in range(401)))
    window.examples_button.click(); finish(jobs)
    assert window.examples_model.loaded == 200
    window.examples_more.click(); assert window.examples_model.loaded == 400
    window.examples_more.click(); assert window.examples_model.loaded == 401 and not window.examples_more.enabled


def test_browser_routes_real_accepted_provider_to_unchanged_native_query_leaf(browser, host):
    window, _ = browser
    provider, _, _, _ = host
    provider.current_selection = window.example_selection_token
    provider.current_knowledge_revision = window.example_catalog_token
    jobs = []
    window.configure_examples(provider=provider, library_ids=('a', 'b'), submit=lambda w, d: jobs.append((w, d)))
    window.table.setCurrentIndex(Index('shared-id'))
    window.examples_button.click(); result = finish(jobs)
    assert result.status == ExampleStatus.SUCCESS
    assert [(row.columns[0], row.columns[1]) for row in window.examples_model.rows] == [
        (ref.library_id, ref.relative_path) for ref in result.references]
    assert provider.accepts(result)


@pytest.mark.parametrize('references', [(MediaReference('foreign', 'x.png'),),
    (MediaReference('a', 'x.png'), MediaReference('a', 'x.png')), ()])
def test_success_delivery_rejects_foreign_duplicate_or_missing_references(browser, references):
    window, _, provider, jobs = ready(browser)
    provider.query = lambda request, **kw: ExampleResult(request, ExampleStatus.SUCCESS, references)
    window.examples_button.click(); finish(jobs)
    assert 'stale' in window.examples_status.text() and not window.examples_model.rows


def test_filter_without_selection_disables_query_and_clears_results(browser):
    window, _, _, jobs = ready(browser); window.examples_button.click(); finish(jobs)
    window.search.setText('no matching tag')
    assert not window.examples_button.enabled and not window.examples_model.rows
    assert 'Select or resolve' in window.examples_status.text()
