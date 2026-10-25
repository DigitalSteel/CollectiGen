"""Ingest presentation hooks on the shared serial preparation request UI."""
from .dataset_preparation_queue import DatasetPreparationQueue


class IngestPreparationQueue(DatasetPreparationQueue):
    surface="Ingest"
    registry_name="ingest_preparation_work"
    busy_name="_batch_task"
    prepare_name="batch_prepare"

    def _set_status(self,message):
        self.owner.batch_summary.setText(message)
        self.owner.batch_summary.show()
    def _controls_changed(self):self.owner._sync_preparation_queue_controls()
    def _hide_page_cancel(self):self.owner.cancel_batch_button.hide()
    def _history_action(self,paths,operation):self.owner._open_image_history(paths,operation)

    def _completion_updates(self,paths):
        for path in paths:
            self.owner._state_cache.pop(path,None)
            self.owner._editor_prefetch_cache.pop(path,None)
        self.owner._trusted_workflow_paths.difference_update(paths)
        if not getattr(self.owner,"_shutdown_requested",False):
            self.owner.render_page()
            self.owner._start_state_scan(paths)
            self.owner.library_paths_changed.emit(tuple(paths))
