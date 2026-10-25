"""One explicit reference review/load/chat route; existing worker and Job own it."""
from threading import Event
from datetime import datetime,timezone

from core.manager_llamacpp_provider import inspect_gguf_reference,inspect_runtime,LlamaCppProvider,DIRECT_CHAT_MAX_OUTPUT_TOKENS
from suite_core.backend_contracts import ApplicationId,HostId
from suite_core.local_resources import sample_local_resources
from .resource_executor import ResourceExecutorOwner
from .model_chat_controller import ModelChatController,ChatTarget


class DirectModelHost:
    def __init__(self,*,executor=None,provider_factory=LlamaCppProvider,sampler=None,
                 direct_runtime_selection=None,normal_entry_session=None,
                 output_tokens=DIRECT_CHAT_MAX_OUTPUT_TOKENS):
        if type(output_tokens) is not int or not 1<=output_tokens<=DIRECT_CHAT_MAX_OUTPUT_TOKENS:
            raise ValueError('bounded_direct_chat_output_required')
        self.output_tokens=output_tokens
        self.direct_runtime_selection=direct_runtime_selection
        self.normal_entry_session=normal_entry_session
        if direct_runtime_selection is not None:
            from core.manager_direct_admission import DirectRuntimeSelection
            from .bootstrap_entry import NormalEntrySession,EntryHelperAdapter
            if (type(direct_runtime_selection) is not DirectRuntimeSelection
                    or type(normal_entry_session) is not NormalEntrySession
                    or direct_runtime_selection.root!=normal_entry_session.admitted.layout.code_root):
                raise ValueError('typed_original_direct_runtime_session_required')
            direct_runtime_selection.validate()
            def provider_factory(selection):
                if selection!=direct_runtime_selection.selection:
                    raise ValueError('reviewed_direct_runtime_changed')
                return LlamaCppProvider(selection,helper_factory=lambda native,command:
                    EntryHelperAdapter.for_direct_runtime(normal_entry_session,native,
                        direct_runtime_selection,command),qualified_modules=True,
                        expected_version=direct_runtime_selection.runtime_version)
        elif normal_entry_session is not None:
            raise ValueError('root_direct_runtime_selection_required')
        self.executor=executor or ResourceExecutorOwner();self.provider_factory=provider_factory
        self.sampler=sampler or (lambda:sample_local_resources(ApplicationId('suite-manager'),HostId('local')))
        self.provider=None;self.version_text='';self.panel=None;self.chat_panel=None
        self.controller=ModelChatController(self,self.executor,output_tokens=self.output_tokens)
        self.future=None;self.cancelled=None;self.action=None;self.closing=False
        self._closing_stop_dispatched=False
        self.status=('Review the installed server and GGUF fixed by this profile. Files stay in place.'
            if direct_runtime_selection is not None else
            'Choose the installed server and an existing GGUF. Files stay in place.')

    @property
    def busy(self):return self.future is not None or self.controller.close_held or self.executor.close_held

    @property
    def close_held(self):return self.busy or self.provider is not None and self.provider.close_held

    def targets(self):
        if self.provider is None:return ()
        ready=self.provider.capabilities['ready'] and not self.provider._unknown and not self.closing
        return (ChatTarget('Manager-owned llama.cpp',self.provider.selection.alias,
            self.provider.selection.model.resource_id,ready,
            'Load this owned model first. After an interrupted request, Stop model and load again.',4096),)

    def request(self,action,*,runtime=None,model=None):
        if self.busy:raise ValueError('Wait for the original worker to finish.')
        if action=='review' and self.provider is not None and self.provider.close_held:
            raise ValueError('Stop the original model before changing files.')
        if action not in ('review','load','stop'):raise ValueError('Unsupported direct model action.')
        if action!='review' and self.provider is None:raise ValueError('Review the exact files first.')
        self.cancelled=Event();self.action=action
        self.status={'review':'Verifying selected files and stock version; no model loading.',
            'load':'Loading the referenced file on CPU only. Cancellation retains original cleanup.',
            'stop':'Stopping only the original owned model process.'}[action]
        def work():
            if action=='review':
                if self.direct_runtime_selection is not None:
                    from pathlib import Path
                    selected=self.direct_runtime_selection.selection
                    if Path(runtime)!=selected.executable or Path(model)!=selected.model.path:
                        raise ValueError('root_selected_direct_files_required')
                reference=inspect_gguf_reference(model,cancelled=self.cancelled.is_set)
                selection=inspect_runtime(runtime,reference,cancelled=self.cancelled.is_set)
                provider=self.provider_factory(selection)
                try:version=provider.version(cancelled=self.cancelled.is_set)
                except BaseException:
                    # Retain any original version-only actor if Stop is uncertain.
                    self.provider=provider
                    raise
                return provider,version
            if action=='load':return self.provider.start(resources=self.sampler(),cancelled=self.cancelled.is_set)
            return self.provider.stop(cancelled=lambda:False)
        self.future=self.executor.submit(work)

    def poll(self):
        self.controller.poll()
        if self.future is not None and self.future.done() and not self.executor.close_held:
            action=self.action
            try:
                value=self.future.result()
                if action=='review':self.provider,self.version_text=value
                self.status={'review':'Files and stock version verified. Review CPU-only plan, then Load model.',
                    'load':'Owned model ready. Open Chat to send a bounded message.',
                    'stop':'Owned model stopped; original process and Job are closed.'}[action]
            except BaseException as error:
                # Program/model errors remain local; do not expose arbitrary logs.
                self.status='Action did not complete. '+('Original process remains held; choose Stop model.'
                    if self.provider is not None and self.provider.close_held else 'No owned model is running. Review files and resources before retry.')
            self.future=None;self.action=None
        if self.chat_panel is not None and not self.controller.closed:
            existing=tuple(self.chat_panel.models.itemData(i) for i in range(1,self.chat_panel.models.count()))
            if self.targets()!=existing:self.chat_panel.set_targets(self.targets())
        if self.closing and not self._closing_stop_dispatched and not self.busy and self.provider is not None and self.provider.close_held:
            self._closing_stop_dispatched=True
            self.request('stop')

    def stream_chat(self,request,cancelled):
        if self.provider is None or request.target not in self.targets() or not request.target.ready:
            raise ValueError('Exact owned model is not ready.')
        yield from self.provider.stream_chat(request,cancelled)

    def open_chat(self,parent):
        from .model_chat_ui import ModelChatPanel
        if self.closing:raise ValueError('Manager is closing.')
        if self.chat_panel is None or self.controller.closed:
            if self.busy:raise ValueError('Wait for chat completion.')
            self.controller=ModelChatController(self,self.executor,output_tokens=self.output_tokens)
            self.chat_panel=ModelChatPanel(self.controller,self.targets(),parent)
        self.chat_panel.show();self.chat_panel.raise_();self.chat_panel.activateWindow()

    def begin_close(self):
        if not self.closing:self._closing_stop_dispatched=False
        self.closing=True
        if self.cancelled is not None:self.cancelled.set()
        if self.chat_panel is not None:self.chat_panel.close()
        else:self.controller.begin_close()
        self.poll()

    def finish_close(self):
        self.poll()
        if self.close_held:return False
        self.executor.begin_close()
        if self.panel is not None:self.panel.timer.stop();self.panel.hide()
        if self.chat_panel is not None:self.chat_panel.timer.stop()
        return not self.executor.close_held


def create_direct_model_panel(host,parent):
    from PySide6.QtCore import QTimer,Qt
    from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QLabel,QLineEdit,QPushButton,QFileDialog
    from .ui_cards import compact_action,details_panel
    class Panel(QDialog):
        def __init__(self):
            super().__init__(parent);self.setWindowTitle('Use an existing GGUF model');self.resize(800,520)
            layout=QVBoxLayout(self)
            note=QLabel('Reference the existing model file directly. No Ollama import, copy or download. '
                'Review launches only the selected server for its version; Load starts one CPU-only owned model.')
            note.setWordWrap(True);layout.addWidget(note)
            self.runtime=QLineEdit();self.model=QLineEdit()
            for title,field,pattern in (('Installed llama-server',self.runtime,'llama-server.exe'),('Existing model',self.model,'GGUF models (*.gguf)')):
                layout.addWidget(QLabel(title));row=QHBoxLayout();row.addWidget(field)
                browse=compact_action(QPushButton('Choose file'))
                def choose(checked=False,field=field,pattern=pattern):
                    value,_=QFileDialog.getOpenFileName(self,'Choose exact existing file','',pattern)
                    if value:field.setText(value)
                browse.clicked.connect(choose);row.addWidget(browse);layout.addLayout(row)
                if host.direct_runtime_selection is not None:
                    selected=host.direct_runtime_selection.selection
                    field.setText(str(selected.executable if field is self.runtime else selected.model.path))
                    field.setReadOnly(True);browse.setEnabled(False)
            plan=QLabel('CPU only · 2 threads · 4096 context · 1 slot · up to 32 reply tokens. '
                'Fresh RAM is checked before Load. Stop affects only this owned process.')
            plan.setWordWrap(True);layout.addWidget(plan)
            self.status=QLabel();self.status.setTextFormat(Qt.PlainText);self.status.setWordWrap(True);layout.addWidget(self.status)
            self.details=details_panel('Selected identity and stock version','')
            layout.addWidget(self.details)
            row=QHBoxLayout()
            self.review=compact_action(QPushButton('Review files'));self.load=compact_action(QPushButton('Load model'))
            self.stop=compact_action(QPushButton('Stop model'));self.chat=compact_action(QPushButton('Open Chat'))
            self.cancel=compact_action(QPushButton('Cancel action'))
            for button in (self.review,self.load,self.stop,self.chat,self.cancel):row.addWidget(button)
            layout.addLayout(row)
            self.review.clicked.connect(lambda:self.request('review'))
            self.load.clicked.connect(lambda:self.request('load'));self.stop.clicked.connect(lambda:self.request('stop'))
            self.chat.clicked.connect(lambda:host.open_chat(self));self.cancel.clicked.connect(lambda:host.cancelled.set() if host.cancelled is not None else None)
            self.timer=QTimer(self);self.timer.setInterval(100);self.timer.timeout.connect(self.poll);self.timer.start();self.render()
        def request(self,action):
            try:host.request(action,runtime=self.runtime.text(),model=self.model.text())
            except ValueError as error:host.status=str(error)
            self.render()
        def poll(self):
            host.poll();self.render()
            if host.closing and not host.close_held:self.timer.stop();self.close()
        def render(self):
            self.status.setText(host.status)
            self.runtime.setEnabled(not host.busy and not host.close_held)
            self.model.setEnabled(not host.busy and not host.close_held)
            self.review.setEnabled(not host.busy and not host.close_held and bool(self.runtime.text() and self.model.text()))
            self.load.setEnabled(not host.busy and host.provider is not None and not host.provider.close_held and not host.closing)
            self.stop.setEnabled(not host.busy and host.provider is not None and host.provider.close_held)
            self.chat.setEnabled(not host.busy and bool(host.targets() and host.targets()[0].ready))
            self.cancel.setEnabled(host.future is not None and host.action!='stop')
            if host.provider is not None:
                from PySide6.QtWidgets import QTextBrowser
                s=host.provider.selection
                self.details.findChild(QTextBrowser).setPlainText(str(s.model.path)+'\n'+s.model.resource_id+'\n'+host.version_text[:512])
        def closeEvent(self,event):
            host.begin_close()
            if host.close_held:event.ignore()
            else:event.accept()
        def reject(self):self.close()
    panel=Panel();panel.runtime.textChanged.connect(panel.render);panel.model.textChanged.connect(panel.render)
    host.panel=panel;return panel


def supervise_direct_model_recovery(held):
    from PySide6.QtCore import QEventLoop,QTimer,Qt
    from PySide6.QtWidgets import QLabel
    from .ui_cards import details_panel
    owner=held.owner;host=owner.direct_model_host;original=owner.window
    if original is None:raise RuntimeError('original_direct_model_window_required')
    original.setEnabled(True);original.show()
    panel=host.panel or create_direct_model_panel(host,original)
    reason=QLabel('Manager needs attention. This owned model is stopping. Keep this window open until its original process and worker are closed.')
    reason.setTextFormat(Qt.PlainText);reason.setWordWrap(True);panel.layout().insertWidget(0,reason)
    panel.layout().insertWidget(1,details_panel('Failure details',type(held.error).__name__+': '+str(held.error)[:512]))
    panel.show();host.begin_close()
    if held.admission is not None:
        original.owned_runtime_panel.enter_recovery(type(held.error).__name__+': '+str(held.error)[:512])
    loop=QEventLoop()
    def refresh():
        host.poll()
        if not original.isVisible() and owner.poll_cleanup():loop.quit()
    timer=QTimer(original);timer.setInterval(100);timer.timeout.connect(refresh);timer.start()
    held.dialog=original;held.app=owner.app
    loop.exec();timer.stop()
    if not owner.closed:raise RuntimeError('direct_model_recovery_still_held')
    held.dialog=None;held.app=None;return 1
