"""Saved, deliberate comparisons over the canonical Generate workflow."""
from __future__ import annotations

from pathlib import Path
import hashlib
import io

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QMessageBox, QComboBox,
    QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.generation_planning import load_plan, save_plan, recover_plan
from core.training_evaluation import (KEY, create_evaluation, evaluation_cells, matched_results,
    review_cell, template_summary, prepare_missing_batch)
from core.training_process import _read
from .gallery import ProtectedImageView
from .modal_feedback import FeedbackEnabledDialog
from .workers import CoordinatedCallTask


class TrainingEvaluationDialog(FeedbackEnabledDialog):
    generationRequested = Signal(object)
    templateRequested = Signal()
    batchRequested = Signal(object)
    editorCloseReady = Signal()

    def __init__(self, jobs, service, template_provider, *, settings=None, run_id="", parent=None):
        super().__init__(parent)
        self.jobs, self.service, self.template_provider = jobs, service, template_provider
        self.run_id = run_id
        self.session = None
        self.path = None
        self.digest = None
        self.cells = ()
        self.results = {}
        self._task = None
        self._closing = False
        self._serial = 0
        self._editor_selection = []
        self._observation_baseline = (False, '')
        self._restoring_selection = False
        self._template_plan = None
        self.setWindowTitle("Training Evaluation")
        self.resize(1180, 790)
        layout = QVBoxLayout(self)
        intro = QLabel("Compare fixed prompts and seeds across the base model and registered LoRAs. Each image uses Generate's normal review and submission. Favorites are personal observations.")
        intro.setWordWrap(True); layout.addWidget(intro)
        top = QHBoxLayout()
        self.open_generate = QPushButton("Set Up Template in Generate…")
        self.open_generate.clicked.connect(self.templateRequested)
        self.open_saved = QPushButton("Open Saved Evaluation…")
        self.open_saved.clicked.connect(self._open)
        self.recover_saved = QPushButton("Recover Interrupted Save…")
        self.recover_saved.clicked.connect(self._recover)
        top.addWidget(self.open_generate); top.addWidget(self.open_saved); top.addWidget(self.recover_saved); top.addStretch()
        layout.addLayout(top)
        self.spec = QWidget(); form = QFormLayout(self.spec)
        self.prompts = QPlainTextEdit(); self.prompts.setPlaceholderText("One evaluation prompt per line, up to eight")
        self.prompts.setMaximumHeight(82)
        self.prompts.setAccessibleName("Fixed evaluation prompts")
        self.seeds = QLineEdit("42"); self.seeds.setAccessibleName("Fixed evaluation seeds")
        self.strengths = QLineEdit("0.5, 1.0"); self.strengths.setAccessibleName("LoRA strengths")
        form.addRow("Prompts", self.prompts); form.addRow("Seeds", self.seeds); form.addRow("LoRA strengths", self.strengths)
        self.template_details = QLabel('No template selected. Set up Generate, then use its settings here.')
        self.template_details.setWordWrap(True); self.template_details.setTextFormat(Qt.PlainText)
        self.use_template = QPushButton('Use Reviewed Generate Settings')
        self.use_template.clicked.connect(self._use_template)
        form.addRow(self.use_template); form.addRow(self.template_details)
        self.create = QPushButton("Save Fixed Comparison…")
        self.create.clicked.connect(self._create)
        form.addRow(self.create); layout.addWidget(self.spec)
        self.summary = QLabel("Set up the base model, LoRAs, size, sampler and other fixed settings in Generate first.")
        self.summary.setWordWrap(True); self.summary.setTextFormat(Qt.PlainText); layout.addWidget(self.summary)
        self.no_results = QLabel(
            "No verified comparison images yet. Queue missing samples in Generate, "
            "review and start that queue there, then refresh verified results here."
        )
        self.no_results.setWordWrap(True)
        self.no_results.setTextFormat(Qt.PlainText)
        layout.addWidget(self.no_results)
        splitter = QSplitter(Qt.Horizontal)
        self.results_splitter = splitter
        self.items = QTreeWidget(); self.items.setRootIsDecorated(False)
        self.items.setHeaderLabels(["Variant", "Prompt / seed", "Result"])
        self.items.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.items.header().setSectionResizeMode(QHeaderView.Stretch)
        self.items.setAccessibleName("Evaluation images; select two rows to compare")
        self.items.itemSelectionChanged.connect(self._selected)
        splitter.addWidget(self.items)
        previews = QWidget(); previews_layout = QHBoxLayout(previews)
        self.preview_panel = previews
        self.previews = [ProtectedImageView(settings=settings), ProtectedImageView(settings=settings)]
        self.preview_titles = []
        for view in self.previews:
            column=QWidget();column_layout=QVBoxLayout(column);column_layout.setContentsMargins(0,0,0,0)
            title=QLabel('Select a verified result');title.setWordWrap(True);title.setTextFormat(Qt.PlainText)
            self.preview_titles.append(title);column_layout.addWidget(title);column_layout.addWidget(view,1)
            previews_layout.addWidget(column)
        splitter.addWidget(previews); splitter.setSizes([440,700]); layout.addWidget(splitter,1)
        note_panel = QWidget()
        note_row = QHBoxLayout(note_panel)
        note_row.setContentsMargins(0, 0, 0, 0)
        self.note_panel = note_panel
        self.favorite = QCheckBox("Favorite")
        self.notes = QLineEdit(); self.notes.setMaxLength(4000); self.notes.setPlaceholderText("Personal note about the selected verified result")
        self.save_note = QPushButton("Save Observation"); self.save_note.clicked.connect(self._save_note)
        note_row.addWidget(self.favorite); note_row.addWidget(self.notes,1); note_row.addWidget(self.save_note)
        layout.addWidget(note_panel)
        actions = QHBoxLayout()
        self.refresh = QPushButton("Refresh Verified Results"); self.refresh.clicked.connect(self.refresh_results)
        self.generate = QPushButton("Review Selected Image in Generate…")
        self.generate.setProperty("primary",True); self.generate.clicked.connect(self._generate)
        self.batch = QComboBox(); self.batch.setAccessibleName('Comparison queue batch')
        self.batch.setMinimumContentsLength(14)
        self.batch.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.queue_missing = QPushButton('Queue Missing Samples…')
        self.queue_missing.setToolTip('Prepare or reopen this batch in Generate. Review readiness and explicitly start its sequential queue there.')
        self.queue_missing.clicked.connect(self._queue_missing)
        self.cancel = QPushButton("Cancel Check"); self.cancel.clicked.connect(self._cancel)
        close = QDialogButtonBox(QDialogButtonBox.Close); close.rejected.connect(self.reject)
        for control in (self.refresh,self.generate,self.batch,self.queue_missing,self.cancel,close): actions.addWidget(control)
        layout.addLayout(actions)
        self.status = QLabel("No evaluation loaded."); self.status.setWordWrap(True); self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        self._update()

    def _launch(self, label, work, done):
        if self._task is not None or self._closing: return
        task = CoordinatedCallTask(self.jobs,label,work,resource="io")
        self._task = task; self.status.setText(label); self._update()
        task.signals.complete.connect(lambda value:self._finished(task,done,value))
        task.signals.failed.connect(lambda error:self._finished(task,done,None,str(error)))
        task.signals.cancelled.connect(lambda:self._finished(task,done,None,"Check cancelled."))
        try: task.start()
        except Exception as exc: self._finished(task,done,None,str(exc))

    def _finished(self,task,done,value,error=""):
        if self._task is not task:return
        self._task=None
        if self._closing: super().reject(); return
        if error:self.status.setText(error[:2000])
        else:
            try: done(value)
            except Exception as exc:self.status.setText(str(exc)[:2000])
        self._update()

    def _use_template(self):
        try:
            template=self.template_provider()
            if template is None:raise ValueError("Open Generate and review a template with exact registered resources first.")
            summary=template_summary(template)
            self._template_plan=template.fork()
            self.template_details.setText(summary)
            self.summary.setText('Enter fixed prompts, seeds and LoRA strengths, then save this comparison.')
            self.status.setText('Template selected. These settings stay fixed even if Generate changes.')
        except Exception as exc:self.status.setText(str(exc)[:2000])
        self._update()

    def _create(self):
        if not self._allow_observation_change(self._create):return
        try:
            template=self._template_plan
            if template is None:raise ValueError('Use Reviewed Generate Settings before saving the comparison.')
            session=create_evaluation(template,prompts=self.prompts.toPlainText().splitlines(),
                seeds=[int(s.strip()) for s in self.seeds.text().split(',')],
                strengths=[float(s.strip()) for s in self.strengths.text().split(',')],
                loras=[r for r in template.request.resources if r.role=='lora'],run_id=self.run_id)
        except Exception as exc:self.status.setText(str(exc)[:2000]); return
        destination,_=QFileDialog.getSaveFileName(self,"Save Training Evaluation","","Evaluation plan (*.json)")
        if not destination:return
        path=Path(destination)
        # Creation never overwrites another evaluation or ordinary generation plan.
        def work(job):
            if job.cancel_requested:raise InterruptedError("Saving evaluation cancelled.")
            if path.exists():raise ValueError("Choose a new evaluation filename; existing plans are preserved.")
            digest=save_plan(path,session,expected_digest=hashlib.sha256(b'<absent>').hexdigest())
            return session,path,digest
        self._launch("Save evaluation specification",work,self._loaded)

    def _open(self):
        if not self._allow_observation_change(self._open):return
        path,_=QFileDialog.getOpenFileName(self,"Open Training Evaluation","","Evaluation plan (*.json)")
        if not path:return
        def work(job):
            if job.cancel_requested:raise InterruptedError("Opening evaluation cancelled.")
            session=load_plan(path); evaluation_cells(session)
            self._check_saved_scope(session)
            return session,Path(path),session.storage_digest
        self._launch("Open saved evaluation",work,self._loaded)

    def _loaded(self,value):
        self._check_saved_scope(value[0])
        self.session,self.path,self.digest=value
        self._template_plan=None
        self.cells=evaluation_cells(self.session); self.results={}
        self.batch.clear()
        for start in range(0,len(self.cells),32):
            self.batch.addItem(f'Images {start+1}–{min(start+32,len(self.cells))}',start//32)
        self.spec.hide()
        self.summary.setText(f"{len(self.cells)} fixed images · {self.path.name}\n{template_summary(self.session)}\nSelect two verified rows to compare. Queue batches contain at most 32 images and require review in Generate.")
        self._render()
        self.status.setText(
            "Saved comparison ready. Queue missing samples in Generate, then refresh verified results here."
        )
        self._update()

    def _recover(self):
        if not self._allow_observation_change(self._recover):return
        path,_=QFileDialog.getOpenFileName(self,"Select Evaluation Plan to Recover","","Evaluation plan (*.json)")
        if not path:return
        def work(job):
            if job.cancel_requested:raise InterruptedError('Recovery cancelled before changes.')
            if self.run_id or getattr(self, 'required_resource', ''):
                try:
                    prior=load_plan(path); evaluation_cells(prior)
                except (OSError, ValueError, RuntimeError) as exc:
                    raise ValueError('This scoped review cannot establish the saved plan identity before recovery. Use the general Training evaluation recovery to inspect it.') from exc
                self._check_saved_scope(prior)
            recover_plan(path)
            session=load_plan(path);evaluation_cells(session)
            self._check_saved_scope(session)
            return session,Path(path),session.storage_digest
        self._launch('Recover interrupted evaluation save',work,self._loaded)

    def _check_saved_scope(self, session):
        spec = session.target_context.get(KEY, {})
        if self.run_id and spec.get('run_id') != self.run_id:
            raise ValueError('This saved evaluation belongs to another training run. Open it from the general Training evaluation workspace.')
        required = getattr(self, 'required_resource', '')
        if required and required not in {row.get('resource', {}).get('id')
                for row in spec.get('variants', ()) if isinstance(row, dict)
                and isinstance(row.get('resource'), dict)}:
            raise ValueError('This saved evaluation does not include the selected training LoRA. Choose its exact comparison or use the general Training evaluation workspace.')

    def _render(self):
        self._editor_selection=[]; self._observation_baseline=(False,'')
        self.items.clear()
        for cell in self.cells:
            rows=self.results.get(cell.key,[])
            ready=[r for r in rows if r['path']]
            state=("Verified" if ready else "Settings changed" if any(not r['comparable'] for r in rows)
                   else rows[-1]['state'] if rows else "Not generated")
            item=QTreeWidgetItem([cell.label,f"{cell.prompt[:80]} / {cell.seed}",state])
            item.setToolTip(1,cell.prompt); item.setData(0,Qt.UserRole,cell.key); self.items.addTopLevelItem(item)

    def _selection(self):
        return [item.data(0,Qt.UserRole) for item in self.items.selectedItems()]

    def _result(self,key):
        ready=[r for r in self.results.get(key,[]) if r['path']]
        review=self.session.target_context[KEY]['reviews'].get(key,{}) if self.session else {}
        return next((r for r in ready if r['job_id']==review.get('job_id')),ready[-1] if ready else None)

    def _selected(self):
        if self._restoring_selection:return
        selected=self._selection()
        if not self._allow_observation_change(lambda:self._select_keys(selected)):
            self._select_keys(self._editor_selection,notify=False)
            return
        self._editor_selection=selected
        self._serial+=1
        for view in self.previews:view.hide()
        for title in self.preview_titles:title.setText('Select a verified result')
        selected=self._selection()
        review=self.session.target_context[KEY]['reviews'].get(selected[0],{}) if len(selected)==1 and self.session else {}
        self.favorite.setChecked(review.get('favorite',False)); self.notes.setText(review.get('notes',''))
        self._observation_baseline=(self.favorite.isChecked(),self.notes.text())
        self._update()

    def refresh_results(self):
        if not self._allow_observation_change(self.refresh_results):return
        if self.session is None:return
        session=self.session
        def work(job):return matched_results(session,self.service,should_cancel=lambda:job.cancel_requested)
        def show(rows):
            self.results=rows; self._render(); self.status.setText("Verified results refreshed. Select one or two rows, then Preview Comparison.")
        self._launch("Verify evaluation results",work,show)

    def preview_selected(self):
        selected=self._selection()
        if not 1<=len(selected)<=2:return
        rows=[self._result(key) for key in selected]
        if any(row is None for row in rows):return
        serial=self._serial
        def work(job):
            images=[]
            # Recheck receipts and bytes immediately before exposing preview pixels.
            current=matched_results(self.session,self.service,should_cancel=lambda:job.cancel_requested)
            for key,row in zip(selected,rows):
                if row not in current[key]:raise ValueError("The selected result changed; refresh before previewing.")
                raw=_read(Path(row['path']),limit=64*1024**2)
                if hashlib.sha256(raw).hexdigest()!=row['sha256']:raise ValueError("The evaluation image changed.")
                from PIL import Image
                if len(raw)>64*1024**2:raise ValueError("The evaluation preview exceeds its size limit.")
                with Image.open(io.BytesIO(raw)) as probe:
                    if probe.width*probe.height>32_000_000:
                        raise ValueError("The evaluation preview exceeds its pixel limit.")
                images.append((raw,row['path']))
            return images
        def show(images):
            if serial!=self._serial:return
            for title,view,key,(raw,path) in zip(self.preview_titles,self.previews,selected,images):
                image=QImage.fromData(raw)
                if image.isNull():raise ValueError("A verified evaluation image cannot be decoded.")
                cell=next(cell for cell in self.cells if cell.key==key)
                title.setText(f'{cell.label} · Seed {cell.seed}');title.setToolTip(cell.prompt)
                view.set_image(QPixmap.fromImage(image),path); view.show()
            self.status.setText("Fixed-setting comparison. Use temporary reveal for protected images.")
        self._launch("Load verified comparison",work,show)

    def _generate(self):
        selected=self._selection()
        if len(selected)!=1:return
        cell=next(c for c in self.cells if c.key==selected[0])
        self.generationRequested.emit(cell.plan)

    def _save_note(self, _checked=False, *, after_save=None):
        selected=list(self._editor_selection)
        if len(selected)!=1:return
        result=self._result(selected[0])
        if result is None:return
        try:
            updated=review_cell(self.session,selected[0],job_id=result['job_id'],output_sha256=result['sha256'],
                favorite=self.favorite.isChecked(),notes=self.notes.text())
        except Exception as exc:self.status.setText(str(exc));return
        expected=self.digest
        def work(job):
            if job.cancel_requested:raise InterruptedError("Saving observation cancelled.")
            current=matched_results(self.session,self.service,should_cancel=lambda:job.cancel_requested)
            if result not in current[selected[0]]:raise ValueError("The reviewed result changed.")
            return save_plan(self.path,updated,expected_digest=expected)
        def saved(digest):
            self.session,self.digest=updated,digest; self.status.setText("Personal observation saved.")
            self._observation_baseline=(self.favorite.isChecked(),self.notes.text())
            if after_save is not None:after_save()
        self._launch("Save evaluation observation",work,saved)

    def _update(self):
        idle=self._task is None and not self._closing
        selected=self._selection()
        has_verified_result=any(row.get('path') for rows in self.results.values() for row in rows)
        selected_result=(self._result(selected[0]) if self.session is not None and len(selected)==1
                         else None)
        self.open_generate.setVisible(self.session is None)
        self.open_generate.setEnabled(idle and self.session is None)
        self.open_saved.setText("Open Another Comparison…" if self.session is not None
                                else "Open Saved Evaluation…")
        for button in (self.open_saved,self.recover_saved):button.setEnabled(idle)
        self.use_template.setEnabled(idle and self.session is None)
        self.create.setEnabled(idle and self.session is None and self._template_plan is not None)
        self.queue_missing.setEnabled(idle and self.session is not None)
        self.batch.setEnabled(idle and self.session is not None)
        self.batch.setVisible(self.session is not None and self.batch.count()>1)
        self.results_splitter.setVisible(self.session is not None)
        self.preview_panel.setVisible(self.session is not None and has_verified_result)
        self.no_results.setVisible(self.session is not None and not has_verified_result)
        self.note_panel.setVisible(selected_result is not None)
        self.items.setEnabled(idle)
        self.refresh.setEnabled(idle and self.session is not None)
        self.generate.setEnabled(idle and len(selected)==1)
        self.generate.setVisible(self.session is not None and len(selected)==1)
        for control in (self.refresh, self.queue_missing):
            control.setVisible(self.session is not None)
        self.save_note.setEnabled(idle and selected_result is not None)
        self.favorite.setEnabled(self.save_note.isEnabled());self.notes.setEnabled(self.save_note.isEnabled())
        self.cancel.setVisible(not idle and not self._closing)
        if not hasattr(self,'preview_button'):
            self.preview_button=QPushButton("Preview Comparison")
            self.preview_button.clicked.connect(self.preview_selected)
            self.layout().insertWidget(self.layout().count()-2,self.preview_button)
        self.preview_button.setVisible(self.session is not None and has_verified_result)
        self.preview_button.setEnabled(idle and 1<=len(selected)<=2 and all(self._result(k) for k in selected))

    def _cancel(self):
        # Keep the completion observer until the worker has actually stopped.
        # CoordinatedCallTask.cancel detaches it and is unsuitable for owned close.
        if self._task is not None and self._task.job_id:
            self.jobs.cancel(self._task.job_id)

    def reject(self):
        if not self._allow_observation_change(self.reject):return
        self._closing=True
        if self._task is not None:self._cancel();self._update();return
        super().reject()

    def request_shell_close(self):
        def saved_close():
            self.reject()
            self.editorCloseReady.emit()
        if self._allow_observation_change(saved_close):self.reject()

    def closeEvent(self,event):
        event.ignore();self.reject()

    def _select_keys(self,selected,*,notify=True):
        self._restoring_selection=True
        for index in range(self.items.topLevelItemCount()):
            item=self.items.topLevelItem(index)
            item.setSelected(item.data(0,Qt.UserRole) in selected)
        self._restoring_selection=False
        if notify:self._selected()

    def _allow_observation_change(self,after_save=None):
        if not self.has_unsaved_changes():
            return True
        if self._task is not None:return False
        answer=QMessageBox.question(self,'Unsaved observation',
            'Save this observation before leaving it?',
            QMessageBox.Save|QMessageBox.Discard|QMessageBox.Cancel,QMessageBox.Cancel)
        if answer==QMessageBox.Save:
            self._save_note(after_save=after_save)
            return False
        if answer!=QMessageBox.Discard:return False
        self.favorite.setChecked(self._observation_baseline[0]);self.notes.setText(self._observation_baseline[1])
        return True

    def has_unsaved_changes(self):
        return len(self._editor_selection)==1 and (self.favorite.isChecked(),self.notes.text())!=self._observation_baseline

    def _queue_missing(self):
        if self.session is None or not self._allow_observation_change(self._queue_missing):return
        session=self.session;batch=self.batch.currentData()
        def work(job):
            return prepare_missing_batch(session,self.service,self.service.root,
                batch=batch,should_cancel=lambda:job.cancel_requested)
        def ready(value):
            self.status.setText('Comparison queue retained. Review readiness and start it in Generate; reopening uses this same batch.')
            self.batchRequested.emit(value)
        self._launch('Prepare missing comparison samples',work,ready)
