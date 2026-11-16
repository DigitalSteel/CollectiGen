"""Explicit synthetic module-channel fixtures; never native/package qualification."""
from pathlib import Path
from threading import RLock
from types import SimpleNamespace as NS

from suite_core.installation import SuiteLayout, InstallMode
from suite_core.workflow_modules.contracts import WorkflowWorkspaceOffer
from suite_core.workflow_modules.entry_binding import ModuleEntryBinding
from suite_core.workflow_modules.session import ModuleEntrySession, _SESSION_TOKEN
from core.workflow_connection import WorkflowConnection


class SyntheticModuleWire:
    """Inert exact replies exercise the real child session state machine only."""
    def __init__(self, offer):
        self.lock = RLock()
        self.sent = []
        self.document = {'entry': {'synthetic': 'child'}, 'launcher': {'synthetic': 'parent'}}
        self.channel = NS(read_fault=None, write_fault=None, epoch=offer.channel_epoch,
            peer_handle='synthetic-never-native', api=NS(WaitForSingleObject=lambda *_: 258),
            local_witness=lambda: self.document['entry'], peer_witness=lambda: self.document['launcher'])

    def send(self, kind, payload):
        self.sent.append((kind, payload))

    def receive(self, reply_kind):
        kind, payload = self.sent[-1]
        expected = {'workflow-check': ('workflow-check-ack', 'ready'),
            'workflow-closing': ('workflow-closing-ack', 'closing'),
            'workflow-drained-report': ('workflow-drained-ack', 'recorded'),
            'workflow-return': ('workflow-return-ack', 'recorded')}
        reply, state = expected[kind]
        assert reply_kind == reply
        scope = {key: payload[key] for key in ('component_id', 'attempt_id', 'grant_id')}
        if kind == 'workflow-return':
            scope['target'] = payload['target']
            if 'resume_draft' in payload:
                scope['resume_draft'] = dict(payload['resume_draft'])
        return {'payload': {**scope, 'state': state}}


def synthetic_module_session(root, component='generate'):
    root = Path(root)
    component_id = 'collectigen.' + component
    code = root / 'Suite'
    data = code / 'Data'
    package = code / 'Apps' / component_id / '0.1.0-alpha.1'
    selected = WorkflowConnection(component_id, package, '0.1.0-alpha.1', 'a' * 64, 'b' * 64, 'synthetic-ui')
    layout = SuiteLayout(code, data, InstallMode.PORTABLE)
    executable = package / ('Generate.exe' if component == 'generate' else 'Train.exe')
    # No signature, filesystem inventory, native pin or process is qualified.
    binding = ModuleEntryBinding(layout, selected, b'inert', b'inert', b'inert', 'c' * 64,
                                 'd' * 64, (), executable)
    offer = WorkflowWorkspaceOffer(1, 1, component_id, selected.version, code, data,
        package, data / 'Manager', 'a' * 64, 'b' * 64, '1' * 32, '2' * 32,
        '3' * 32, '4' * 32, 'e' * 64, 'exclusive-writer')
    session = ModuleEntrySession(_SESSION_TOKEN, binding, offer, SyntheticModuleWire(offer))
    session.state = 'ready'
    return session


def workflow_window(root, component='generate', *, theme=None, initial_draft=None,
                    configure_session=None, **kwargs):
    from PySide6.QtWidgets import QApplication
    from importlib import import_module
    app = QApplication.instance() or QApplication([])
    if theme is not None:
        from qt_app.theme import apply_theme
        apply_theme(app, theme)
    session = synthetic_module_session(root, component)
    if initial_draft is not None:
        from core.generation_module_handoff import draft_descriptor
        assert component == 'generate'
        session.initial_draft = draft_descriptor(initial_draft)
    if configure_session is not None:
        configure_session(session)
    window = import_module('suite_core.' + component + '.window').create_window(session, **kwargs)
    window.resize(1920, 1040)
    window.show()
    app.processEvents()
    return app, session, window


def trigger_more(window, label):
    """Use the actual surface menu and its QAction signal, including modal routes."""
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    menu = window.surface.more.menu()
    action, = [action for action in menu.actions() if action.text() == label]
    selected = []
    def choose():
        # Dynamic retained-work actions are refreshed by aboutToShow.
        selected.append(action.isEnabled() and action.isVisible())
        menu.hide()
        if selected[-1]:
            action.trigger()
    QTimer.singleShot(0, choose)
    QTest.mouseClick(window.surface.more, Qt.LeftButton)
    assert selected == [True], label
    return action
