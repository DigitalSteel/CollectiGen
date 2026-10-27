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


@dataclass
class Policy:
    horizontal: object = None
    vertical: object = None
    def setVerticalPolicy(self, value): self.vertical = value


class Control:
    def __init__(self, *args, **kwargs):
        self.value = args[0] if args and isinstance(args[0], str) else ''
        self.enabled = True
        self.items = []
        self.position = 0
        self._parent = next((arg for arg in reversed(args) if isinstance(arg, Control)), None)
        self._central = self._layout = None
        self._hidden = False
        self._policy = Policy()
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
    def hide(self): self._hidden = True
    def show(self): self._hidden = False
    def setVisible(self, visible): self._hidden = not visible
    def isHidden(self): return self._hidden
    def setParent(self, parent): self._parent = parent
    def parentWidget(self): return self._parent
    def setCentralWidget(self, widget): self._central = widget
    def centralWidget(self): return self._central
    def layout(self): return self._layout
    def sizePolicy(self): return replace(self._policy)
    def setSizePolicy(self, *args):
        self._policy = replace(args[0]) if len(args) == 1 else Policy(*args)
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


class Layout(Control):
    """Ownership/order protocol only; no native geometry or event loop."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.owner, self.widgets, self.layouts = parent, [], []
        if parent is not None: parent._layout = self
    def addWidget(self, widget, *args):
        self.insertWidget(len(self.widgets), widget)
    def insertWidget(self, index, widget):
        self.removeWidget(widget)
        self.widgets.insert(len(self.widgets) if index < 0 else index, widget)
        if self.owner is not None: widget.setParent(self.owner)
    def removeWidget(self, widget):
        if widget in self.widgets: self.widgets.remove(widget)
    def indexOf(self, widget):
        return self.widgets.index(widget) if widget in self.widgets else -1
    def addLayout(self, layout, *args):
        self.layouts.append(layout)
        layout.owner = self.owner
        for widget in layout.widgets:
            if self.owner is not None: widget.setParent(self.owner)


class CommandBarControl(Control):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.visible_limits = {}


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
    qtcore = ModuleType('PySide6.QtCore'); qtcore.Qt = SimpleNamespace(PlainText=0, AlignLeft=1)
    widgets = ModuleType('PySide6.QtWidgets')
    for name in ('QComboBox QFileDialog QLabel QLineEdit QMainWindow QPushButton QSplitter QTableView QTextEdit QWidget QListWidget QApplication QCheckBox QFrame').split():
        setattr(widgets, name, Control)
    widgets.QVBoxLayout = widgets.QHBoxLayout = Layout
    widgets.QSizePolicy = SimpleNamespace(Maximum=1, Preferred=2, Expanding=3, Minimum=4)
    widgets.QAbstractItemView = SimpleNamespace(SelectRows=1, SingleSelection=2, NoEditTriggers=3)
    # The browser reuses a native activity widget; this pure-Control fixture
    # must double that dependency too, even when real Qt was loaded earlier.
    components = ModuleType('qt_app.components')
    components.IndeterminateActivityBar = Control
    shared = ModuleType('qt_app.browse_components')
    shared.IncrementalResultModel, shared.IncrementalResultRow = Model, Row
    commandbar = ModuleType('suite_core.manager.command_bar')
    commandbar.CommandBar = CommandBarControl
    commandbar.CommandSearchDialog = Control
    commandbar.UiCommand = lambda *args, **kwargs: SimpleNamespace(key=args[0], label=args[1], group=args[2], **kwargs)
    cards = ModuleType('suite_core.manager.ui_cards')
    cards.card_style = lambda widget, *, compact=False: None  # Presentation only, never import native Qt.
    for name, module in {'PySide6': ModuleType('PySide6'), 'PySide6.QtCore': qtcore,
                         'PySide6.QtWidgets': widgets, 'qt_app.browse_components': shared,
                         'suite_core.manager.command_bar': commandbar,
                         'suite_core.manager.ui_cards': cards,
                         'qt_app.components': components}.items():
        monkeypatch.setitem(sys.modules, name, module)
    path = Path(__file__).with_name('browser.py')
    if not path.is_file():
        path = Path(__file__).resolve().parents[1] / 'encyclopedia/browser.py'
    spec = importlib.util.spec_from_file_location('suite_core.encyclopedia._media_candidate', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'read', lambda _: publication())
    window = module.EncyclopediaWindow()
    assert type(window.database_activity) is Control
    window.open_snapshot('synthetic-publication.json')  # reader replaced with DTO
    return window, module


def test_fake_constructor_delivery_preserves_parent_visibility_and_policy(browser):
    loaded, module = browser
    loaded._sync_start_choices()  # Hostless fixture: explicitly model the host's next render sync.
    empty = module.EncyclopediaWindow()
    assert type(empty.start_card) is Control and type(empty.start_body) is Layout
    assert empty.bar.visible_limits == {'Home': 2}
    assert loaded.bar.visible_limits == {}  # Mutable policy is per instance.
    root = empty.centralWidget()
    assert root.layout().indexOf(empty.start_card) >= 0
    for widget in (empty.empty_hint, empty.status, empty.database_activity, empty.database_elapsed, empty.cancel_work_button):
        assert widget.parentWidget() is empty.start_card
    assert empty.database_activity.isHidden() and empty.database_elapsed.isHidden()
    assert empty.cancel_work_button.isHidden() and not empty.status.isHidden()
    assert empty.status.sizePolicy().vertical == 4
    assert empty.open_publication(publication())
    empty._sync_start_choices()
    assert empty.bar.visible_limits == {} and empty.start_card.isHidden()
    for widget in (empty.empty_hint, empty.status, empty.database_activity, empty.database_elapsed, empty.cancel_work_button):
        assert widget.parentWidget() is root
    assert empty.database_activity.isHidden() and empty.cancel_work_button.isHidden()
    assert not empty.status.isHidden()


@pytest.mark.parametrize('preloaded', [False, True])
def test_fake_module_substitution_restores_exact_previous_modules(monkeypatch, preloaded):
    names = ('PySide6', 'PySide6.QtCore', 'PySide6.QtWidgets', 'qt_app.browse_components',
             'suite_core.manager.command_bar', 'suite_core.manager.ui_cards', 'qt_app.components')
    missing = object()
    for name in names:
        if preloaded:
            monkeypatch.setitem(sys.modules, name, ModuleType(name))
        else:
            monkeypatch.delitem(sys.modules, name, raising=False)
    previous = {name: sys.modules.get(name, missing) for name in names}
    with monkeypatch.context() as scoped:
        window, _module = browser.__wrapped__(scoped)
        assert type(window.database_activity) is Control
        assert sys.modules['PySide6.QtWidgets'].QApplication is Control
        assert sys.modules['suite_core.manager.ui_cards'].card_style.__module__ == __name__
    for name, original in previous.items():
        assert sys.modules.get(name, missing) is original, name


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
