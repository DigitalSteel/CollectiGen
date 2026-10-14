from __future__ import annotations

from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject,QRunnable,Signal,Slot


def start_qt_delivery_task(pool,task,priority=None):
    """Start a bounded Qt delivery adapter through one shared call surface."""
    if priority is None:
        pool.start(task)
    else:
        pool.start(task,priority)


class AsyncCallSignals(QObject):
    complete=Signal(object)
    failed=Signal(str)


class AsyncCallTask(QRunnable):
    """Run one potentially expensive callable without blocking the interface."""

    def __init__(self,callable_):
        super().__init__(); self.callable=callable_; self.signals=AsyncCallSignals()
        self._cancelled=Event(); self._finished=Event()
        self.state="queued"; self.error=""

    def cancel(self):
        """Discard a queued or running result without interrupting the callable."""
        self._cancelled.set()
        if self.state=="queued":
            self.state="cancelled"

    def is_cancelled(self):
        return self._cancelled.is_set()

    def is_finished(self):
        return self._finished.is_set()

    def wait(self,timeout: float|None=None):
        return self._finished.wait(timeout)

    @staticmethod
    def _signal_owner_was_deleted(exc: RuntimeError) -> bool:
        message=str(exc).casefold()
        return (
            "signal source has been deleted" in message
            or "internal c++ object (asynccallsignals) already deleted" in message
        )

    def _emit(self,signal,*args):
        try:
            signal.emit(*args)
        except RuntimeError as exc:
            # Teardown can delete the Qt signal owner after the callable has
            # safely finished. That is not a work failure and must not trigger
            # a second emission through the same deleted object.
            if not self._signal_owner_was_deleted(exc):
                raise

    @Slot()
    def run(self):
        try:
            if self.is_cancelled():
                self.state="cancelled"
                return
            self.state="running"
            try:
                result=self.callable()
            except Exception as exc:
                self.error=str(exc)
                self.state="cancelled" if self.is_cancelled() else "failed"
                if not self.is_cancelled():
                    self._emit(self.signals.failed,self.error)
                return
            if self.is_cancelled():
                self.state="cancelled"
                return
            self.state="complete"
            self._emit(self.signals.complete,result)
        finally:
            self._finished.set()


class CoordinatedCallTask(QObject):
    """Publish one shared coordinator result to Qt without owning another pool."""

    def __init__(self,manager,name: str,callable_,*,resource: str="cpu",key: str="",execution_scope=None):
        super().__init__(); self.manager=manager; self.name=name; self.callable=callable_
        self.resource=resource; self.key=key; self.signals=ProgressCallSignals()
        self.job_id=""; self._observer=""; self._cancelled=False
        self.execution_scope=execution_scope

    def start(self):
        if self.job_id:
            return
        scope_options={} if self.execution_scope is None else {"execution_scope":self.execution_scope}
        job,observer=self.manager.submit_observed(
            self.name,self.callable,self._finished,
            resource=self.resource,key=self.key,
            progress_listener=self._progress,
            **scope_options,
        )
        self.job_id=job.id
        self._observer=observer
        if job.state in {"complete","cancelled","failed"}:
            self._observer=""

    def cancel(self):
        self._cancelled=True
        if self.job_id and self._observer:
            self.manager.unobserve(
                self.job_id,self._observer,cancel_if_orphaned=True,
            )
            self._observer=""

    def _finished(self,job):
        self._observer=""
        if self._cancelled:
            return
        if job.state=="complete":
            self.signals.complete.emit(job.result)
        elif job.state=="cancelled":
            self.signals.cancelled.emit()
        else:
            self.signals.failed.emit(job.error or "Background work failed")

    def _progress(self,job):
        if not self._cancelled:
            self.signals.progress.emit(job.current,job.total,job.message)


class ProgressCallSignals(QObject):
    progress=Signal(int,int,str)
    complete=Signal(object)
    cancelled=Signal()
    failed=Signal(str)


class ProgressCallTask(QRunnable):
    """Run a callable that reports ``(current, total, label)`` progress."""

    def __init__(self,callable_):
        super().__init__(); self.callable=callable_; self.signals=ProgressCallSignals()

    @Slot()
    def run(self):
        try: self.signals.complete.emit(self.callable(self.signals.progress.emit))
        except Exception as exc: self.signals.failed.emit(str(exc))


class CancelableProgressCallTask(QRunnable):
    """Progress worker whose callable receives a cooperative cancellation predicate."""

    def __init__(self,callable_):
        super().__init__(); self.callable=callable_; self.signals=ProgressCallSignals(); self._cancelled=Event()

    def cancel(self):
        self._cancelled.set()

    def is_cancelled(self):
        return self._cancelled.is_set()

    @Slot()
    def run(self):
        try: self.signals.complete.emit(self.callable(self.signals.progress.emit,self.is_cancelled))
        except InterruptedError: self.signals.cancelled.emit()
        except Exception as exc: self.signals.failed.emit(str(exc))


class DatasetRefreshWorker(QObject):
    cached_ready=Signal(object)
    inventory_ready=Signal(object)
    phase=Signal(str)
    schema_migration_complete=Signal(object)
    authority_initialization_complete=Signal(object)
    automatic_ai_complete=Signal(object)
    progress=Signal(int,int,str)
    complete=Signal(object)
    cancelled=Signal(object)
    failed=Signal(str)
    finished=Signal()

    def __init__(
        self,dataset,path: Path,include_subfolders: bool,excluded=(),*,
        restore_cached: bool=False,validate: bool=True,migrate_legacy: bool=False,
        knowledge=None,operation_journal=None,settings=None,ai_service=None,
        ai_work_lease=None,
        refresh_external_evidence: bool=False,
        external_evidence_mode: str="",external_evidence_sources=("txt","metadata"),
        managed_paths=None,run_automatic_ai: bool=True,changed_paths=None,
        external_evidence_checkpoint_service=None,
        external_evidence_checkpoint_session=None,
        history_configuration=None,
    ):
        super().__init__()
        self.dataset=dataset; self.path=Path(path)
        self.include_subfolders=include_subfolders
        self.excluded=tuple(Path(value) for value in excluded if value)
        self.restore_cached=restore_cached; self.validate=validate
        self.migrate_legacy=migrate_legacy
        self.knowledge=knowledge; self.operation_journal=operation_journal
        self.settings=settings; self.ai_service=ai_service
        self.ai_work_lease=ai_work_lease
        self.refresh_external_evidence=bool(refresh_external_evidence)
        self.external_evidence_mode=str(external_evidence_mode or "")
        self.external_evidence_sources=tuple(external_evidence_sources)
        self.managed_paths=(
            None if managed_paths is None else
            tuple(dict.fromkeys(Path(value) for value in managed_paths))
        )
        self.run_automatic_ai=bool(run_automatic_ai)
        self.changed_paths=(
            None if changed_paths is None else {Path(value) for value in changed_paths}
        )
        self.external_evidence_checkpoint_service=(
            external_evidence_checkpoint_service
        )
        self.external_evidence_checkpoint_session=(
            external_evidence_checkpoint_session
        )
        self.history_configuration=history_configuration
        self._managed_rules_fingerprint=""
        self._managed_job_fingerprint=""
        self._managed_initializer=None
        self._cancelled=Event()

    @Slot()
    def cancel(self):
        self._cancelled.set()

    def is_cancelled(self):
        return self._cancelled.is_set()

    @Slot()
    def run(self):
        try:
            cached=None
            if self.restore_cached:
                self.phase.emit("Opening the remembered library")
                cached=self.dataset.restore_cached(
                    self.path,include_subfolders=self.include_subfolders,
                )
                if cached:
                    self.cached_ready.emit(cached)
                    if not self.validate:
                        if self.migrate_legacy:
                            if not self._migrate_legacy():
                                self.cancelled.emit(cached)
                                return
                        self.complete.emit(cached)
                        return
            self.phase.emit("Checking the library for changes")
            last=[0]
            def report(index,total,name):
                if index==total or index-last[0]>=25:
                    last[0]=index; self.progress.emit(index,total,name)
            previously_validated=(
                dict(getattr(self.dataset,"validated_records",{}))
                if self.changed_paths is not None else {}
            )
            summary=self.dataset.inventory(
                self.path,include_subfolders=self.include_subfolders,
                excluded_directories=self.excluded,
                progress=report,should_cancel=self.is_cancelled,
            )
            last[0]=0
            inventory_index=getattr(self.dataset,"index",None)
            if inventory_index is not None:
                inventory_index.remove_missing(self.dataset.images)
            if self.changed_paths is not None:
                self.dataset.reuse_unchanged_cached(
                    self.changed_paths,previously_validated=previously_validated,
                )
            readable_images=list(getattr(self.dataset,"readable_images",self.dataset.images))
            writable_images=list(getattr(self.dataset,"writable_images",readable_images))
            if self.changed_paths is not None:
                readable_images=[p for p in readable_images if p in self.changed_paths]
                writable_images=[p for p in writable_images if p in self.changed_paths]
            if self.managed_paths is not None:
                current={str(Path(value)).casefold():Path(value) for value in self.dataset.images}
                writable={str(Path(value)).casefold():Path(value) for value in writable_images}
                missing=[
                    path for path in self.managed_paths
                    if str(path).casefold() not in current
                ]
                if missing:
                    raise RuntimeError(
                        "The frozen external-evidence scope changed before work "
                        "started. Nothing was imported; build the preview again."
                    )
                held=[
                    path for path in self.managed_paths
                    if str(path).casefold() not in writable
                ]
                if held:
                    raise RuntimeError(
                        "One or more images in the frozen external-evidence scope "
                        "became unavailable, read-only, or conflicted. No evidence "
                        "was imported for this library; restore access and build the preview again."
                    )
                writable_images=[
                    writable[str(path).casefold()] for path in self.managed_paths
                    if str(path).casefold() in writable
                ]
                readable_images=list(writable_images)
            def report_status(index,total,label):
                self.phase.emit(label)
                self.progress.emit(index,total,label)
            if cached is not None:
                self.phase.emit("Validating saved image data")
                self.dataset.validate_cached(
                    readable_images,progress=report,
                    should_cancel=self.is_cancelled,
                )
                if self.is_cancelled():
                    self.cancelled.emit(self.dataset.summary)
                    return
            # A remembered view remains usable while this inexpensive exact
            # signature pass runs, but Dataset must not label its workflow
            # projections current until changed families have been retired.
            self.inventory_ready.emit(summary)
            from core.image_authority_initialization import ImageAuthorityInitializationService
            self.phase.emit(
                "Importing external evidence"
                if self.managed_paths is not None else "Initializing image data"
            )
            initializer=ImageAuthorityInitializationService(
                knowledge=self.knowledge,journal=self.operation_journal,
                gather_source_evidence=(
                    True if self.refresh_external_evidence else
                    bool(getattr(self.settings,"auto_import_source_evidence",True))
                ),
                refresh_external_evidence=self.refresh_external_evidence,
                external_evidence_mode=self.external_evidence_mode,
                external_evidence_sources=self.external_evidence_sources,
            )
            rules_fingerprint=initializer.current_rules_fingerprint()
            job_fingerprint=initializer.current_job_fingerprint()
            self._managed_initializer=initializer
            self._managed_rules_fingerprint=rules_fingerprint
            self._managed_job_fingerprint=job_fingerprint
            if self.history_configuration is not None and self.external_evidence_checkpoint_session is not None:
                # Native saved history must not reuse a legacy import session
                # that can silently skip images outside its captured boundary.
                from core.history_workflow import configured_capture
                for start in range(0,len(writable_images),initializer.batch_size):
                    if configured_capture(self.history_configuration,
                            writable_images[start:start+initializer.batch_size],
                            operation_kind="external_evidence_import") is not None:
                        raise ValueError("Saved-history imports cannot reuse a legacy checkpoint. Reopen the exact import preview.")
            index=getattr(self.dataset,"index",None)
            deferred_rule_refresh=[]
            if (
                self.refresh_external_evidence
                and self.external_evidence_checkpoint_session is not None
            ):
                initializer.trusted_existing={
                    path for path in writable_images
                    if path in {
                        *self.external_evidence_checkpoint_session.completed_paths,
                        *self.external_evidence_checkpoint_session.current_paths,
                    }
                }
            elif index is not None and not self.refresh_external_evidence:
                validated=set(getattr(self.dataset,"validated_records",{}))
                current_results=index.trusted_managed_images(
                    writable_images,rules_fingerprint=rules_fingerprint,
                    job_fingerprint=job_fingerprint,
                    validated_images=validated,
                    require_rule_results=True,
                )
                authority_results=index.trusted_managed_authority_images(
                    writable_images,job_fingerprint=job_fingerprint,
                    validated_images=validated,
                )
                current_results.update(index.rebind_managed_rule_results(
                    authority_results-current_results,rules_fingerprint=rules_fingerprint,
                    job_fingerprint=job_fingerprint,validated_images=validated,
                    rebind=initializer.reusable_derived_result,
                    should_continue=lambda:not self.is_cancelled(),
                ))
                initializer.trusted_existing=authority_results
                deferred_rule_refresh=sorted(
                    authority_results-current_results,
                    key=lambda path:str(path).casefold(),
                )
            checkpoint_db=index.connect() if index is not None else None
            checkpoint_count=[0]
            def cache_checkpoint(image,record,derived_result):
                if index is not None and checkpoint_db is not None:
                    self.dataset.cache_managed_record(image,record,db=checkpoint_db)
                    index.record_managed_completion(
                        image,rules_fingerprint=rules_fingerprint,
                        job_fingerprint=job_fingerprint,
                        derived_result=derived_result,db=checkpoint_db,
                    )
                checkpoint_count[0]+=1
                if (
                    checkpoint_db is not None
                    and self.external_evidence_checkpoint_session is None
                    and checkpoint_count[0]%initializer.batch_size==0
                ):
                    checkpoint_db.commit()
            def evidence_checkpoint(image,_record,counts):
                if checkpoint_db is not None:
                    checkpoint_db.commit()
                self.external_evidence_checkpoint_service.checkpoint(
                    self.external_evidence_checkpoint_session,image,
                    counts=counts,
                )
            external_checkpoint=(
                evidence_checkpoint
                if self.external_evidence_checkpoint_service is not None
                and self.external_evidence_checkpoint_session is not None
                else None
            )
            try:
                from core.history_workflow import configured_capture
                history_factory=(lambda images:configured_capture(
                    self.history_configuration,images,
                    operation_kind="external_evidence_import" if self.refresh_external_evidence else "image_authority_initialization",
                    cancelled=self.is_cancelled,
                )) if self.history_configuration is not None else None
                initialized=initializer.initialize_many(
                    writable_images,progress=report,status=report_status,
                    should_cancel=self.is_cancelled,
                    on_cache_complete=cache_checkpoint,
                    on_external_evidence_complete=external_checkpoint,
                    **({"history_operation_factory":history_factory} if history_factory is not None else {}),
                )
                initialized.deferred_rule_refresh_paths=list(
                    deferred_rule_refresh
                )
                if checkpoint_db is not None:
                    checkpoint_db.commit()
            finally:
                if checkpoint_db is not None:
                    try:
                        checkpoint_db.commit()
                    finally:
                        checkpoint_db.close()
            if (
                self.changed_paths is None and index is not None and not initialized.failed
                and not initialized.cancelled and not initialized.existing_legacy
                and len(writable_images)==len(self.dataset.images)
                and not initialized.deferred_rule_refresh_paths
            ):
                index.set_metadata("managed_image_rules_fingerprint",rules_fingerprint)
            self.authority_initialization_complete.emit(initialized)
            if initialized.cancelled or self.is_cancelled():
                self.cancelled.emit(summary)
                return
            if self.settings is not None and self.run_automatic_ai and not initialized.history_interrupted:
                from core.automatic_ai_analysis import AutomaticAIAnalysisService
                automatic_ai=AutomaticAIAnalysisService(
                    self.settings,knowledge=self.knowledge,ai=self.ai_service,
                    ai_work_lease=self.ai_work_lease,
                )
                if automatic_ai.enabled():
                    self.phase.emit("Running configured AI analysis")
                    last[0]=0
                    ai_result=automatic_ai.analyze_many(
                        [path for path in writable_images if path not in {
                            issue.image for issue in initialized.issues if issue.blocking
                        }],progress=report,should_cancel=self.is_cancelled,
                        **({"history_operation_factory":lambda images:configured_capture(
                            self.history_configuration,images,operation_kind="automatic_image_ai",cancelled=self.is_cancelled,
                        )} if self.history_configuration is not None else {}),
                    )
                    self.automatic_ai_complete.emit(ai_result)
                    if ai_result.cancelled or self.is_cancelled():
                        self.cancelled.emit(summary)
                        return
            self.phase.emit("Reading image data")
            last[0]=0
            summary=self.dataset.analyze(
                readable_images,progress=report,should_cancel=self.is_cancelled,
            )
            if self.is_cancelled():
                self.cancelled.emit(summary)
                return
            if self.migrate_legacy:
                if not self._migrate_legacy():
                    self.cancelled.emit(summary)
                    return
            self.complete.emit(summary)
        except InterruptedError:
            self.cancelled.emit(getattr(self.dataset,"summary",None))
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()

    def _migrate_legacy(self) -> bool:
        from core.annotation import SCHEMA_VERSION
        from core.image_schema_migration import ImageSchemaMigrationService
        from core.history_workflow import configured_capture
        migration=ImageSchemaMigrationService()
        self.phase.emit("Checking image record formats")
        index=getattr(self.dataset,"index",None)
        try: checked=int(index.metadata("image_schema_checked_version","0") or 0) if index else 0
        except (TypeError,ValueError): checked=0
        writable=set(getattr(self.dataset,"writable_images",self.dataset.images))
        images=(
            list(getattr(self.dataset,"schema_migration_candidates",()))
            if checked>=SCHEMA_VERSION else self.dataset.images
        )
        images=[image for image in images if image in writable]
        trusted_migrations=(
            getattr(index,"trusted_schema_migration_images",None)
            if index is not None else None
        )
        retained=(
            trusted_migrations(images,target_version=SCHEMA_VERSION)
            if callable(trusted_migrations) else set()
        )
        if retained:
            self.progress.emit(
                len(retained),len(images),"Restored completed upgrades",
            )
        pending=[image for image in images if image not in retained]
        def preview_progress(current,total,name):
            self.progress.emit(len(retained)+current,len(images),name)
        plan=migration.preview_many(
            pending,progress=preview_progress,should_cancel=self.is_cancelled,
        )
        connect=getattr(index,"connect",None) if index is not None else None
        checkpoint_db=(
            connect() if plan.previews and callable(connect) else None
        )
        def checkpoint(preview,record):
            if index is None or checkpoint_db is None:
                return
            try:
                self.dataset.cache_managed_record(
                    preview.image,record,db=checkpoint_db,
                )
                if (
                    self._managed_rules_fingerprint
                    and self._managed_job_fingerprint
                ):
                    derived_result=(
                        self._managed_initializer.derive_rule_result(record)
                        if self._managed_initializer is not None else None
                    )
                    index.record_managed_completion(
                        preview.image,
                        rules_fingerprint=self._managed_rules_fingerprint,
                        job_fingerprint=self._managed_job_fingerprint,
                        derived_result=derived_result,
                        db=checkpoint_db,
                    )
                index.record_schema_migration_completion(
                    preview.image,target_version=SCHEMA_VERSION,db=checkpoint_db,
                )
                # Every verified Image Knowledge upgrade is independently durable.
                # Losing the process after this point must retain its exact progress.
                checkpoint_db.commit()
            except Exception:
                checkpoint_db.rollback()
                raise
        def apply_progress(current,total,name):
            self.progress.emit(
                len(retained)+current,len(retained)+total,name,
            )
        if plan.previews:
            self.phase.emit("Upgrading legacy image records")
            try:
                result=migration.apply_many(
                    plan,root=self.path,progress=apply_progress,
                    should_cancel=self.is_cancelled,on_complete=checkpoint,
                    retained=len(retained),
                    **({"history_operation_factory":lambda images:configured_capture(
                            self.history_configuration,images,operation_kind="image_schema_migration",cancelled=self.is_cancelled,
                        )} if self.history_configuration is not None else {}),
                )
            finally:
                if checkpoint_db is not None:
                    checkpoint_db.close()
        else:
            if checkpoint_db is not None:
                checkpoint_db.close()
            result=migration.apply_many(
                plan,root=self.path,retained=len(retained),
            )
        if (
            index and not result.failed and not result.cancelled
            and len(writable)==len(self.dataset.images)
        ):
            index.set_metadata("image_schema_checked_version",str(SCHEMA_VERSION))
            self.dataset.schema_migration_candidates.clear()
        self.schema_migration_complete.emit(result)
        if result.history_interrupted or result.history_held:
            raise RuntimeError("Legacy image upgrade stopped for saved-history recovery or renewed source admission. Review the affected image before continuing.")
        return not result.cancelled
