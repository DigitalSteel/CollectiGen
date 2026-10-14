"""Nonblocking bridge between the trainer chooser and canonical Setup queue."""
from dataclasses import dataclass
import json
from pathlib import Path

from .training_setup_dialog import TrainingSetupPreview, profile_overview


@dataclass(frozen=True)
class QueuedTrainerSetup:
    action: str
    operation_id: str
    reference: dict
    message: str


class TrainerSetupQueueController:
    def __init__(self,profile,executor,queue,*,managed_available=False):
        self.profile,self.executor,self.queue=profile,executor,queue
        self.managed_available=bool(managed_available)

    def supports(self,action):
        if self.profile is None:return False
        return self.managed_available if action=='managed' else action in {'existing','import','export','repair','activate','rollback'}

    def prepare(self,action,selection,*,should_cancel,progress):
        if not self.supports(action):raise ValueError('This trainer option has no qualified Setup profile.')
        reference,value=self.executor.prepare(action,selection,should_cancel=should_cancel,progress=progress)
        labels={'managed':('Review your setup plan','Install trainer software'),
            'existing':('Review Existing Trainer','Check this installation'),
            'import':('Review Offline Trainer Pack','Queue Pack Import'),
            'export':('Review Trainer Pack Export','Queue Pack Export'),
            'repair':('Review Trainer Repair','Queue Repair Generation'),
            'activate':('Review Trainer Activation','Queue Activation'),
            'rollback':('Review Trainer Rollback','Queue Rollback')}
        title,button=labels[action]
        summary=('Prepare a separate verified software generation. Activation and training each have their own review.'
            if action in {'managed','import','repair'} else
            'Verify this selected installation without changing or adopting its files.' if action=='existing' else
            'Export verified software and licenses to a new pack. Models and datasets are excluded.' if action=='export' else
            'Run fresh installation health checks, then select this generation. Earlier generations remain available.')
        details=profile_overview(self.profile).details+'\n\n'
        if action=='managed':
            plan=value['selection']
            summary=(f"Download: up to {plan['download_bytes']/1024**3:.2f} GiB of public trainer software\n"
                f"Installed software: {self.profile.byte_count/1024**3:.2f} GiB\n"
                f"Trainer storage allowance: {value['storage_budget_bytes']/1024**3:.1f} GiB (includes downloads and recovery files)\n"
                "Temporary space and download time: no separate estimate available\n"
                "Cost: no software purchase is made; your internet provider may charge for data\n"
                f"Location: {value['root']}\n\n"
                "Install and check a separate trainer environment through the shared Setup queue. "
                "You will review activation afterward. This does not start training.")
            details+=f"Trainer storage allowance: {value['storage_budget_bytes']/1024**3:.1f} GiB\n"
            details+='Exact download sources, hashes and licenses:\n'+json.dumps(plan,indent=2,ensure_ascii=False)
        else:
            selected=json.dumps(value['selection'],ensure_ascii=False)
            if action == 'export':
                generation, destination = value['selection']
                scope = (f'Retained generation: {generation}. Pack filename: {Path(destination).name}. '
                    'Full destination is in exact plan details.')
            elif action in {'activate', 'rollback'}:
                scope = f'Retained generation: {value["selection"]}.'
            else:
                label = {'existing': 'Installation folder', 'import': 'Pack filename',
                         'repair': 'Repair source folder'}[action]
                scope = (f'{label}: {Path(value["selection"]).name or "root folder"}. '
                    'Full path is in exact plan details.')
            summary+='\n\n'+scope
            details+='Selected scope: '+selected+'\n'
        details+='\nReview SHA-256: '+reference['review_sha256']+'\nManaged storage: '+value['root']
        return TrainingSetupPreview(action,title,summary,details,button,reference,True,self.profile.sha256)

    def apply(self,preview,*,should_cancel,progress):
        if should_cancel():raise InterruptedError('Trainer setup confirmation was cancelled.')
        if not self.supports(preview.action) or preview.profile_sha256!=self.profile.sha256:
            raise ValueError('The trainer profile changed. Review Setup again.')
        result=self.queue.enqueue_confirmed('setup_trainer_runtime',preview.confirmation_label,dict(preview.payload))
        return QueuedTrainerSetup(preview.action,result.operation.id,dict(preview.payload),result.message)

    def operation(self,identity):
        return next((row for row in self.queue.snapshot() if row.id==identity),None)

    def completed(self,pending,*,should_cancel,progress):
        return self.executor.completed_result(pending.reference,should_cancel=should_cancel)

    def cancel(self,pending):
        return self.queue.cancel_operation(pending.operation_id)


def open_trainer_setup(state,parent,*,return_review=None):
    """Load only reviewed trainer metadata off the GUI thread, then show Setup."""
    from pathlib import Path
    import os
    import re
    import weakref
    from shiboken6 import isValid
    from PySide6.QtCore import QTimer
    from core.paths import APP_ROOT,USER_DATA_DIR
    from core.trainer_runtime_catalog import RuntimeSoftwareCatalog
    from core.trainer_environment import _read_json
    from core.training_file_inventory import _check_path
    from .training_setup_dialog import TrainingSetupDialog
    from .training_setup_resources_dialog import TrainingSetupResourcesDialog
    from .workers import CoordinatedCallTask
    if getattr(parent,'_trainer_setup_load',None) is not None:return
    existing=getattr(parent,'_trainer_setup_dialog',None)
    if existing is not None and isValid(existing) and existing.isVisible():
        if return_review is not None:existing.set_return_to_preparation(return_review)
        existing.raise_();existing.activateWindow();return
    executor=state.setup_queue.trainer_executor
    owner=weakref.ref(parent)
    def load(job):
        if executor is None:return None,None,False,(),'The trainer Setup service is unavailable. Manual training packages remain available.'
        try:profile=executor.profile()
        except (OSError,RuntimeError,ValueError):
            return None,None,False,(),'The qualified trainer runtime is not included in this build. Manual training packages remain available.'
        overview=profile_overview(profile)
        try:RuntimeSoftwareCatalog.from_dict(_read_json(executor.catalog_path));managed=True
        except (OSError,RuntimeError,ValueError):managed=False
        choices=[];root=executor.managed_root/'generations'
        if root.exists():
            _check_path(root,directory=True)
            with os.scandir(root) as entries:
                for entry in entries:
                    if job.cancel_requested:raise InterruptedError('Opening trainer Setup was cancelled.')
                    if len(choices)>=1000:raise ValueError('Retained trainer generations exceed the bounded Setup view.')
                    if not re.fullmatch(r'[0-9a-f]{32}',entry.name):raise ValueError('Retained trainer storage needs review.')
                    _check_path(Path(entry.path),directory=True)
                    choices.append((entry.name, 'Generation ' + entry.name))
        return profile,overview,managed,tuple(sorted(choices)),''
    task=CoordinatedCallTask(state.jobs,'Opening local trainer Setup',load,resource='io')
    parent._trainer_setup_load=task
    def show(result):
        widget=owner()
        if widget is None or not isValid(widget):return
        widget._trainer_setup_load=None
        profile,overview,managed,generations,held=result
        controller=TrainerSetupQueueController(profile,executor,state.setup_queue,managed_available=managed) if profile is not None else None
        dialog=TrainingSetupDialog(profile,state.jobs,
            goal_summary='Prepare SDXL 1.0 LoRA training on this computer.',
            machine_fit_summary='Installation checks verify the local Python, trainer and GPU. The selected model and recipe receive separate readiness checks before training.',
            controller=controller,overview=overview,held_reason=held,generations=generations,parent=widget)
        if return_review is not None and isValid(return_review):
            dialog.set_return_to_preparation(return_review)
        widget._trainer_setup_dialog=dialog
        def selected(value):
            if value.get('mode')=='manual':return
            reference=value.get('review_reference')
            if not reference:return
            resources=TrainingSetupResourcesDialog(executor,reference,USER_DATA_DIR/'training-setup.json',state.jobs,
                default_tokenizers=str(APP_ROOT/'tools/trainer-tokenizers/sdxl'),
                default_output=str(USER_DATA_DIR/'training-runs'),parent=widget)
            resources.accepted.connect(
                lambda: QTimer.singleShot(0, lambda: dialog._return_to_preparation(setup_saved=True)))
            resources.rejected.connect(
                lambda: QTimer.singleShot(0,
                    lambda: dialog._return_to_preparation(setup_cancelled=True)))
            widget._trainer_resources_dialog=resources;resources.show()
        dialog.preparationSelected.connect(selected);dialog.show()
    task.signals.complete.connect(show)
    task.signals.failed.connect(lambda error:show((None,None,False,(),str(error)[:2000])))
    task.signals.cancelled.connect(lambda:show((None,None,False,(),'Opening trainer Setup was cancelled.')))
    try:task.start()
    except Exception as exc:show((None,None,False,(),str(exc)[:2000]))
