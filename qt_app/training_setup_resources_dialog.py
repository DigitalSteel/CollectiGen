"""Save the user's verified trainer and chosen local training-resource folders."""
from pathlib import Path

from PySide6.QtWidgets import QDialogButtonBox,QFileDialog,QFormLayout,QHBoxLayout,QLabel,QLineEdit,QPushButton,QVBoxLayout,QWidget

from core.training_setup_selection import TrainingSetupSelectionService
from .modal_feedback import FeedbackEnabledDialog
from .workers import CoordinatedCallTask


class TrainingSetupResourcesDialog(FeedbackEnabledDialog):
    feedback_workflow='Saved local training resources'

    def __init__(self,executor,reference,selection_path,jobs,*,default_tokenizers='',default_output='',parent=None):
        super().__init__(parent)
        self.executor,self.reference,self.selection_path,self.jobs=executor,dict(reference),Path(selection_path),jobs
        self._task=None;self._revision=None;self._closing=False
        self.setWindowTitle('Save Local Training Setup');self.resize(780,350)
        layout=QVBoxLayout(self)
        note=QLabel('Choose the original SDXL text-encoding files and where to save training runs. The selected trainer is already verified; each training run still requires its own review.')
        note.setWordWrap(True);layout.addWidget(note)
        form=QFormLayout();layout.addLayout(form)
        self.fields={};self.choose_buttons=[]
        for key,label,initial in [('tokenizers','SDXL tokenizer folder',default_tokenizers),('output','Save training runs in',default_output)]:
            row=QWidget();line=QHBoxLayout(row);line.setContentsMargins(0,0,0,0)
            text=QLineEdit(str(initial));text.setAccessibleName(label);self.fields[key]=text
            choose=QPushButton('Choose…');self.choose_buttons.append(choose);choose.clicked.connect(lambda checked=False,k=key:self._choose(k))
            line.addWidget(text,1);line.addWidget(choose);form.addRow(label,row)
        help_text=QLabel('The tokenizer folder contains tokenizer and tokenizer_2. Advanced users can select another verified copy. Existing outputs are preserved; new runs use separate destinations.')
        help_text.setWordWrap(True);layout.addWidget(help_text)
        self.status=QLabel('Loading the saved Setup selection…');self.status.setWordWrap(True);layout.addWidget(self.status)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel);layout.addWidget(buttons)
        self.save_button=buttons.button(QDialogButtonBox.Save);self.save_button.setText('Verify and Save Setup')
        buttons.accepted.connect(self._save);buttons.rejected.connect(self.reject)
        self._start('Loading saved trainer resources',lambda job:TrainingSetupSelectionService(self.selection_path).snapshot(),self._loaded)

    def _choose(self,key):
        selected=QFileDialog.getExistingDirectory(self,'Choose '+('SDXL tokenizer folder' if key=='tokenizers' else 'training output folder'),self.fields[key].text())
        if selected:self.fields[key].setText(selected)

    def _start(self,label,work,complete):
        if self._task is not None:return
        task=CoordinatedCallTask(self.jobs,label,work,resource='io');self._task=task
        self.save_button.setEnabled(False)
        for field in (*self.fields.values(),*self.choose_buttons):field.setEnabled(False)
        task.signals.complete.connect(lambda result:self._finish(task,complete,result))
        task.signals.failed.connect(lambda error:self._finish(task,self._failed,error))
        task.signals.cancelled.connect(lambda:self._finish(task,self._failed,'Setup saving cancelled. The verified trainer is retained.'))
        try:task.start()
        except Exception as exc:self._finish(task,self._failed,str(exc))

    def _finish(self,task,complete,result):
        if self._task is not task:return
        self._task=None
        if self._closing:super().reject();return
        complete(result)
        self.save_button.setEnabled(self._revision is not None)
        for field in (*self.fields.values(),*self.choose_buttons):field.setEnabled(True)

    def _loaded(self,result):
        selected,self._revision=result
        if selected is not None:
            self.fields['tokenizers'].setText(selected.tokenizers_root)
            self.fields['output'].setText(selected.output_root)
        self.status.setText('Review these folders, then save this verified trainer for future training runs.')

    def _failed(self,error):self.status.setText(str(error)[:2000])

    def _save(self):
        if self._task is not None or self._revision is None:return
        tokens=self.fields['tokenizers'].text().strip();output=self.fields['output'].text().strip();revision=self._revision
        def work(job):
            if not tokens or not output or not Path(tokens).is_absolute() or not Path(output).is_absolute():
                raise ValueError('Choose absolute tokenizer and training output folders.')
            if job.cancel_requested:raise InterruptedError('Setup saving cancelled.')
            from core.training_source_io import _assert_unredirected
            _assert_unredirected(Path(output));Path(output).mkdir(parents=True,exist_ok=True)
            return self.executor.save_resources(self.reference,self.selection_path,tokens,output,revision,
                should_cancel=lambda:job.cancel_requested or self.jobs.shutting_down)
        self.status.setText('Verifying the selected trainer, text-encoding files, and output location…')
        self._start('Verifying and saving local training Setup',work,lambda _:super(TrainingSetupResourcesDialog,self).accept())

    def reject(self):
        self._closing=True
        if self._task is not None:
            self.jobs.cancel(self._task.job_id);self.status.setText('Stopping at the current verification boundary…');return
        super().reject()

    def closeEvent(self,event):
        if self._task is not None:event.ignore();self.reject()
        else:super().closeEvent(event)
