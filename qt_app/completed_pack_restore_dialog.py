"""Exact native user-import restore preview; the retained host owns all work."""
from pathlib import Path
from time import monotonic
import json

from PySide6.QtCore import Qt, QSize, QTimer
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QTreeWidget, QTreeWidgetItem, QPlainTextEdit, QCheckBox, QProgressBar,
    QSplitter, QGroupBox, QScrollArea, QWidget, QToolButton, QSizePolicy)
from .components import bounded_window_size, IndeterminateActivityBar
from suite_core.manager.ui_cards import disclosure_panel


class CompletedPackRestoreDialog(QDialog):
    def __init__(self, host):
        super().__init__(host.window)
        self.host, self.controller = host, host.controller
        self._listing = self._review = self._result = None
        self._rows = ()
        self._saved = None
        self._scope_key = None
        self._phase = None
        self._started = None
        self.setWindowTitle("Restore state before selected user import")
        self.resize(bounded_window_size(host.window.screen().availableGeometry(), QSize(1000, 740)))
        outer = QVBoxLayout(self)
        layout = outer
        title = QLabel("Review a completed user import")
        title.setObjectName("workspaceHeading");layout.addWidget(title)
        self.scope = self.label("");layout.addWidget(self.scope)
        scroll=QScrollArea();scroll.setWidgetResizable(True)
        body=QWidget();layout=QVBoxLayout(body);scroll.setWidget(body)
        outer.addWidget(scroll,1)
        self.scroll=scroll
        layout.addWidget(self.label("Compare the current data with the saved state before one import. Restoring affects every listed area together. Later changes prevent restoration."))
        controls = QHBoxLayout()
        self.open_saved = QPushButton("Open saved catalog connection")
        self.refresh = QPushButton("Choose completed import…")
        controls.addWidget(self.open_saved);controls.addWidget(self.refresh);controls.addStretch()
        layout.addLayout(controls)
        self.imports = QComboBox();self.imports.setAccessibleName("Completed user import to review")
        self.imports.addItem("Choose an import; nothing is selected", None);layout.addWidget(self.imports)
        self.review_button = QPushButton("Compare selected import");layout.addWidget(self.review_button)
        self.areas = QTreeWidget();self.areas.setHeaderLabels(["Affected area", "Restoration consequence"])
        self.areas.setWordWrap(True);self.areas.setRootIsDecorated(False);self.areas.setAccessibleName("Areas restored together")
        self.areas.setMinimumHeight(65);self.areas.setMaximumHeight(160);self.areas.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Maximum);layout.addWidget(self.areas)
        layout.addWidget(self.label("Names summarize the changes. Compare exact saved values to review every stored field."))
        compare_body = QGroupBox();compare_layout=QVBoxLayout(compare_body)
        headers=QHBoxLayout();headers.addWidget(self.label("Current saved values"));headers.addWidget(self.label("Before import / after restoration"));compare_layout.addLayout(headers)
        splitter=QSplitter(Qt.Horizontal)
        self.current=QPlainTextEdit();self.previous=QPlainTextEdit()
        for box in (self.current,self.previous):box.setReadOnly(True);splitter.addWidget(box)
        self.current.setAccessibleName("Current saved data comparison");self.previous.setAccessibleName("Proposed previous saved data")
        compare_layout.addWidget(splitter)
        self.comparison=disclosure_panel("Compare exact saved values",compare_body)
        self.comparison.findChild(QToolButton).toggled.connect(
            lambda opened: QTimer.singleShot(0,lambda:scroll.ensureWidgetVisible(compare_body)) if opened else None)
        compare_body.setMinimumHeight(100);compare_body.setMaximumHeight(190);layout.addWidget(self.comparison)
        details_body=QGroupBox();details_layout=QVBoxLayout(details_body)
        self.details=QPlainTextEdit();self.details.setReadOnly(True);self.details.setMaximumHeight(80)
        details_layout.addWidget(self.details);layout.addWidget(disclosure_panel("Receipt and catalog details",details_body))
        layout.addStretch()
        layout=outer
        self.confirm=QCheckBox("Restore all listed areas to the saved state before this exact import")
        layout.addWidget(self.confirm)
        self.progress=IndeterminateActivityBar(self);self.progress.hide();layout.addWidget(self.progress)
        self.status=self.label("Open the saved catalog connection, then choose an import.")
        self.status.setAccessibleName("Completed import restoration status");layout.addWidget(self.status)
        actions=QHBoxLayout()
        self.restore=QPushButton("Restore reviewed import")
        self.recover=QPushButton("Recover selected inverse")
        self.reload=QPushButton("Reload saved catalog")
        self.cancel=QPushButton("Cancel")
        self.cancel.setDefault(True)
        for b in (self.restore,self.recover,self.reload,self.cancel):actions.addWidget(b)
        layout.addLayout(actions)
        self.open_saved.clicked.connect(self.open_connection)
        self.refresh.clicked.connect(self.load_imports)
        self.imports.currentIndexChanged.connect(self.selection_changed)
        self.review_button.clicked.connect(self.compare_selected)
        self.restore.clicked.connect(self.restore_selected)
        self.recover.clicked.connect(self.recover_selected)
        self.reload.clicked.connect(lambda:self.request_ordinary('reload'))
        self.cancel.clicked.connect(self.cancel_work)
        self.confirm.toggled.connect(lambda:self.render())
        self.areas.currentItemChanged.connect(self.show_part)
        self.render()

    @staticmethod
    def label(text):
        label=QLabel(text);label.setWordWrap(True);label.setTextFormat(Qt.PlainText);return label

    def open_connection(self):
        if self.host._write_actions_allowed():self.controller.connect('connection_open')
        self.render()

    def load_imports(self):
        self._saved=None
        self.selection_changed()
        self.controller.request_completed('list');self.render()

    def selection_changed(self,*_):
        if not self.controller.pending:
            self.controller.cancel()
            self._review=None;self.areas.clear();self.current.clear();self.previous.clear()
            self.confirm.setChecked(False)
        self.render()

    def compare_selected(self):
        relative=self.imports.currentData()
        if relative is not None and self.controller.choice is not None:
            self.controller.request_completed('compare',receipt=self.controller.choice.native_adapter.root/relative)
        self.render()

    def restore_selected(self):
        if self.confirm.isChecked() and self._review is self.controller.completed_review:
            self.controller.request_completed('undo',review=self._review,confirmed=True)
        self.render()

    def recover_selected(self):
        choice=self.controller.choice
        if choice and len(choice.completed_inverse_receipts)==1:
            self.controller.request_completed('recover',receipt=choice.completed_inverse_receipts[0])
        self.render()

    def request_ordinary(self,kind):
        self.controller.request(kind);self.render()

    def cancel_work(self):
        self.controller.cancel()
        self.confirm.setChecked(False)
        if not self.controller.pending:
            self._review=None;self.areas.clear();self.current.clear();self.previous.clear()
        self.render()

    def show_part(self,item,*_):
        if item is not None:
            part=item.data(0,Qt.UserRole)
            self.current.setPlainText(part.current);self.previous.setPlainText(part.proposed)

    def render(self,delivered=False):
        c=self.controller;choice=c.choice
        binding=choice.transactions.session.binding if choice else None
        key=(id(c),choice.native_adapter.root if choice and choice.native_adapter else None,
            choice.native_adapter.identity_binding if choice and choice.native_adapter else None,binding)
        if key!=self._scope_key:
            keep_saved=(self._scope_key is not None and key[:3]==self._scope_key[:3]
                and c.last_operation=='reload' and c.result is not None)
            saved=self._saved if keep_saved else None
            self._scope_key=key;self._listing=self._review=self._result=None;self._rows=();self._saved=saved
            self.imports.blockSignals(True);self.imports.clear();self.imports.addItem("Choose an import; nothing is selected",None);self.imports.blockSignals(False)
            self.areas.clear();self.current.clear();self.previous.clear();self.confirm.setChecked(False);self.details.clear()
        writable=self.host._write_actions_allowed() and c.apply_authorized
        self.scope.setText("Selected catalog: "+(choice.native_adapter.root.name+" · saved connection" if choice and choice.native_adapter else "not connected"))
        self.open_saved.setVisible(choice is None)
        self.open_saved.setEnabled(writable and c.can_connect)
        busy=c.close_held or c.executor.close_held
        self.progress.setVisible(busy and not c.faulted)
        feedback=c.pending_feedback
        owner=feedback[0] if feedback else None
        if owner is not None and owner is not self._phase:
            self._phase=owner;self._started=monotonic()
        elif not busy:
            self._phase=self._started=None
        if c.completed_listing is not None and c.completed_listing is not self._listing and c.completed_listing.session_binding==binding:
            self._listing=c.completed_listing;self._rows=self._listing.rows
            self.imports.blockSignals(True);self.imports.clear();self.imports.addItem("Choose an import; nothing is selected",None)
            for n,row in enumerate(self._rows,1):self.imports.addItem("User import "+str(n)+" · "+", ".join(row.areas),row.receipt_relative)
            self.imports.blockSignals(False)
        if c.completed_review is not None and c.completed_review is not self._review:
            self._review=c.completed_review;self.areas.clear();self.confirm.setChecked(False)
            for part in self._review.comparison:
                item=QTreeWidgetItem([Path(part.relative).stem.replace('_',' ').title(),part.consequence+"\n"+part.summary]);item.setData(0,Qt.UserRole,part);self.areas.addTopLevelItem(item)
            if self.areas.topLevelItemCount():self.areas.setCurrentItem(self.areas.topLevelItem(0))
            self.details.setPlainText("Catalog: "+self._review.native_review.catalog_root+"\nReceipt: "+self._review.native_review.receipt_relative+"\nReceipt SHA256: "+self._review.native_review.receipt_sha256)
            self.areas.resizeColumnToContents(0)
            self.areas.setMaximumHeight(min(180,self.areas.header().height()+max(1,self.areas.topLevelItemCount())*48+4))
        if getattr(c.result,"native_saved",False):
            self._saved=c.result
            if c.result is not self._result:
                self._result=c.result;self.confirm.setChecked(False)
                self.details.setPlainText("Catalog: "+str(choice.native_adapter.root)+"\nRestoration receipt: "+str(c.result.receipt))
        if self._review is not None and c.completed_review is None and not busy:
            self._review=None;self.confirm.setChecked(False);self.areas.clear();self.current.clear();self.previous.clear()
        valid=bool(self._review is not None and self._review is c.completed_review and self._review.comparison)
        pending_inverse=bool(choice and choice.completed_inverse_receipts and choice.observation_status=='completed_inverse_pending')
        self.refresh.setText("Refresh imports" if self._listing is not None else "Choose completed import…")
        self.review_button.setVisible(not valid)
        self.refresh.setEnabled(writable and c.can_request and not busy and not pending_inverse)
        self.imports.setEnabled(writable and not busy and not pending_inverse)
        self.review_button.setEnabled(writable and c.can_request and not busy and self.imports.currentData() is not None and not pending_inverse)
        self.confirm.setEnabled(valid and not busy and writable)
        self.restore.setEnabled(valid and self.confirm.isChecked() and not busy and writable)
        self.recover.setVisible(pending_inverse);self.recover.setEnabled(writable and not busy and c.can_request)
        refreshed=bool(self.host.reader_delivery_result is c.result and self.host.reader_delivery_ok
            and self.host.window.publication == self.host.reader_delivery_publication)
        failed_refresh=bool(self._saved is not None and (not refreshed or c.error))
        self.reload.setVisible(failed_refresh);self.reload.setEnabled(writable and c.can_request and not busy)
        if c.faulted and c.close_held:
            message="Completion is unconfirmed. Automatic work is stopped; Close stays held. Keep this window and the saved receipt evidence for recovery review."
        elif busy:
            elapsed=int(monotonic()-self._started) if self._started is not None else 0
            queued={"connection_open":"Opening saved catalog connection",
                "completed_list":"Finding completed user imports",
                "completed_compare":"Comparing saved values",
                "completed_undo":"Restoring reviewed user import",
                "completed_recover":"Recovering interrupted restoration",
                "reload":"Reloading saved catalog"}
            phase=c.phase if c.pending else "Settling the retained worker"
            if phase=="Waiting for background admission" and feedback:
                phase=queued.get(feedback[1],"Preparing the selected work")+"; waiting to start"
            phase=("Cancellation requested; waiting for retained work" if c.cancel_requested else
                phase)
            message=phase+f" · elapsed: {elapsed} seconds. Keep this window open until work settles."
        elif pending_inverse:message="An interrupted restoration needs review. Recover the selected inverse for this catalog; ordinary changes stay held."
        elif self._saved is not None:
            message="Restoration saved. The original receipt and its backups are retained. "+("Reader refreshed." if refreshed else "Reload saved catalog to refresh the reader.")
            if c.error:message+=" Host verification needs review; see Receipt and catalog details. Do not restore again."
        elif c.error:message="Review held. Keep the saved evidence; nothing further will run automatically. Open Receipt and catalog details for the exact reason."
        elif valid:message="Comparison ready. Review every listed area, then confirm the complete restoration."
        elif c.reason.startswith("Cancelled before admission"):message="Cancelled before work started. No restoration was requested."
        elif "discard" in c.reason.lower():message="Review cleared. No restoration was requested. Compare an import again to continue."
        elif choice and self._listing is not None and not self._rows:message="No completed user imports are available to review in this catalog."
        elif choice:message="Choose a completed user import, then compare its saved changes."
        else:message="Open the saved writable catalog connection. No data will be imported or restored."
        if c.error:
            self.details.setPlainText("Catalog: "+str(choice.native_adapter.root if choice and choice.native_adapter else "not connected")+"\nDiagnostic: "+c.error)
        if self.host.reader_delivery_error:
            self.details.setPlainText("Catalog: "+str(choice.native_adapter.root if choice and choice.native_adapter else "not connected")+"\nReader delivery: "+self.host.reader_delivery_error)
        self.progress.set_active(busy and not c.faulted,message)
        self.status.setText(message)
        self.cancel.setText("Close held" if c.faulted else "Cancel work" if busy else "Clear review")
        self.cancel.setEnabled(not c.faulted)

    def invalidate_context(self):
        self._scope_key=None
        self.render()

    def reject(self):
        if self.controller.close_held or self.controller.executor.close_held:
            if not self.controller.faulted:self.controller.cancel()
            self.render();return
        self.controller.cancel();super().reject()

    def closeEvent(self,event):
        if self.controller.close_held or self.controller.executor.close_held:
            if not self.controller.faulted:self.controller.cancel()
            self.render();event.ignore();return
        self.controller.cancel();event.accept()
