from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict,deque
from contextlib import nullcontext
from pathlib import Path
from threading import Event,RLock
from typing import Iterable

from .annotation import ImageAnnotation
from .background_jobs import BackgroundJobManager,JobProgress
from .events import EventBus
from .image_authority_initialization import ImageAuthorityInitializationService
from .image_questions import ImageQuestionService
from .image_record import ImageRecord
from .typed_boundary import typed_tag_sources
from .refresh_diagnostics import RefreshTrace


class DerivedRefreshCoordinator:
    """Coalesce authority events into safe, machine-local cache refreshes."""

    def __init__(
        self,events:EventBus,jobs:BackgroundJobManager,dataset,*,settings=None,
        initializer_factory=None,reviews=None,review_service_factory=None,
    ) -> None:
        self.events,self.jobs,self.dataset=events,jobs,dataset
        self.settings=settings
        self.initializer_factory=initializer_factory
        self.reviews=reviews
        self.review_service_factory=review_service_factory
        self._lock=RLock()
        self._pending:set[Path]=set()
        self._pending_all=False
        self._pending_dependency_tags:set[str]=set()
        self._pending_dependency_relationships:set[str]=set()
        self._pending_causes:set[str]=set()
        self._wake=Event()
        self._job:JobProgress|None=None
        self._token=""
        self._inflight=None
        self._inflight_generation=0
        self._deferred_for_visible_work=False
        self._preempted_jobs:dict[str,str]={}
        self._diagnostic_history=deque(maxlen=32)
        self._pending_trace=None
        self._started=False
        self.start()

    def start(self) -> None:
        if self._started:
            return
        self._started=True
        self.events.subscribe("image_data_changed",self._image_changed)
        self.events.subscribe("library_data_changed",self._library_changed)
        self.events.subscribe("knowledge_changed",self._knowledge_changed)
        self.events.subscribe("tag_membership_changed",self._tag_policy_changed)
        self.events.subscribe("modifier_policy_changed",self._tag_policy_changed)

    def stop(self) -> None:
        if not self._started:
            return
        self.events.unsubscribe("image_data_changed",self._image_changed)
        self.events.unsubscribe("library_data_changed",self._library_changed)
        self.events.unsubscribe("knowledge_changed",self._knowledge_changed)
        self.events.unsubscribe("tag_membership_changed",self._tag_policy_changed)
        self.events.unsubscribe("modifier_policy_changed",self._tag_policy_changed)
        with self._lock:
            job=self._job
            self._pending.clear(); self._pending_all=False
            self._pending_dependency_tags.clear()
            self._pending_dependency_relationships.clear()
            self._pending_causes.clear()
            self._inflight=None
            self._deferred_for_visible_work=False
            self._preempted_jobs.clear()
            self._pending_trace=None
            self._wake.set()
        if job is not None and job.state in {"queued","running","paused"}:
            self.jobs.cancel(job.id)
        self._started=False

    def _image_changed(self,**payload) -> None:
        values=payload.get("images") or payload.get("paths") or payload.get("image")
        if isinstance(values,(str,Path)):
            values=(values,)
        self.request(values or (),cause="Image Knowledge changed",
            action_code="image_data_changed",change_class="image_evidence")

    def _library_changed(self,**payload) -> None:
        """Refresh changed library rows or repair incomplete dependency coverage."""
        values=payload.get("images") or payload.get("paths") or payload.get("image")
        if isinstance(values,(str,Path)):
            values=(values,)
        if payload.get("exact_changes") is True:
            # A completed move supplies its exact source/destination paths.
            # Missing coverage elsewhere belongs to normal library recovery,
            # not to this single-family operation.
            self.request(values or (),cause="Moved images changed library contents",
                action_code="library_data_changed",change_class="library_membership")
            return
        readable=self._readable_images()
        coverage=self._dependency_images(
            readable,tag_ids=set(),relationship_ids=set(),
        )
        cause="Library contents or derived dependency coverage changed"
        changed={Path(value) for value in (values or ()) if value}
        changed.update(coverage)
        if changed:
            self.request(changed,cause=cause,action_code="library_data_changed",
                change_class="library_membership",expansion_reason="library_coverage_repair")

    @staticmethod
    def _stable_ids(value) -> tuple[str,...]:
        if isinstance(value,(str,bytes)):
            value=(value,)
        return tuple(dict.fromkeys(
            str(item).strip() for item in (value or ())
            if item is not None and str(item).strip()
        ))

    def _knowledge_changed(self,**payload) -> None:
        tag_ids=self._stable_ids(payload.get("tag_ids"))
        relationship_ids=self._stable_ids(payload.get("relationship_ids"))
        exact=(
            payload.get("complete") is True
            and str(payload.get("dependency_kind") or "") in {
                "relationship","tag_policy","tag_identity",
            }
            and bool(tag_ids)
        )
        if not exact:
            reason=("incomplete_dependency_contract" if payload.get("complete") is not True
                else "unsupported_dependency_kind" if str(payload.get("dependency_kind") or "") not in
                {"relationship","tag_policy","tag_identity"} else "missing_dependency_ids")
            self.request(
                None,cause="System Knowledge or Automation rules changed",
                action_code="knowledge_changed",change_class=payload.get("dependency_kind"),
                expansion_reason=reason,
            )
            return
        images=payload.get("images",()) if payload.get("dependency_kind")=="tag_identity" else ()
        if isinstance(images,(str,Path)):
            images=(images,)
        self.request(
            images,cause="Reviewed System Knowledge dependencies changed",
            dependency_tag_ids=tag_ids,
            dependency_relationship_ids=relationship_ids,
            action_code="knowledge_changed",change_class=payload.get("dependency_kind"),
        )

    def _tag_policy_changed(self,**payload) -> None:
        tag_ids=self._stable_ids(payload.get("tag_ids"))
        if payload.get("complete") is not True or not tag_ids:
            self.request(None,cause="System Knowledge policy changed",
                action_code="tag_policy_changed",change_class="tag_policy",
                expansion_reason="incomplete_dependency_contract" if payload.get("complete") is not True else "missing_dependency_ids")
            return
        self.request(
            (),cause="System Knowledge policy changed",
            dependency_tag_ids=tag_ids,
            action_code="tag_policy_changed",change_class="tag_policy",
        )

    def request(
        self,images:Iterable[str|Path]|None=None,*,cause:str="Derived data changed",
        dependency_tag_ids:Iterable[str]=(),
        dependency_relationship_ids:Iterable[str]=(),
        action_code:str="refresh_request",change_class:str="unknown",
        expansion_reason:str="none",
    ) -> JobProgress|None:
        if not self._started or self.jobs.shutting_down:
            return None
        cause=str(cause or "Derived data changed").strip()
        with self._lock:
            requested=None if images is None else {Path(value) for value in images if value}
            active=self._job is not None and self._job.state in {"queued","running","paused"}
            trace=(getattr(self._job,"_derived_refresh_trace",None) if active else self._pending_trace)
            if trace is None:
                trace=RefreshTrace()
                self._pending_trace=trace
            trace.add_request(action_code=action_code,change_class=change_class,
                requested_count=None if requested is None else len(requested),
                expansion_reason="explicit_all" if requested is None and expansion_reason=="none" else expansion_reason)
            if images is None:
                self._pending_all=True; self._pending.clear()
                self._pending_dependency_tags.clear()
                self._pending_dependency_relationships.clear()
            elif not self._pending_all:
                self._pending.update(requested)
                self._pending_dependency_tags.update(
                    self._stable_ids(dependency_tag_ids)
                )
                self._pending_dependency_relationships.update(
                    self._stable_ids(dependency_relationship_ids)
                )
            self._pending_causes.add(cause)
            self._wake.set()
            if self._job is not None and self._job.state in {
                "queued","running","paused",
            }:
                return self._job
            if self._deferred_for_visible_work:
                return None
            try:
                self._job,self._token=self.jobs.submit_observed(
                    "Refresh current image results",self._run,self._finished,
                    resource="io",key="aut04:derived-refresh",
                )
            except RuntimeError:
                self._job=None; return None
            job=self._job
            job._derived_refresh_trace=trace
            trace.bind_job(job.id)
            self._diagnostic_history.append(trace)
            self._pending_trace=None
            scope="all" if self._pending_all else "images"
        self._publish("queued",cause=cause,scope=scope,current=0,total=0)
        return job

    def diagnostic_snapshot(self):
        """Bounded aggregate diagnostics; never paths, content or exception text."""
        with self._lock:
            traces=tuple(self._diagnostic_history)
        return [trace.snapshot() for trace in traces]

    @staticmethod
    def _phase(job,name):
        trace=getattr(job,"_derived_refresh_trace",None)
        return trace.phase(name) if trace is not None else nullcontext()

    @staticmethod
    def _count(job,name,amount=1):
        trace=getattr(job,"_derived_refresh_trace",None)
        if trace is not None:
            trace.count(name,amount)

    @classmethod
    def _phase_images(cls,job,name,callback,images,*args,**kwargs):
        cls._count(job,name+"_images",len(images))
        with cls._phase(job,name):
            return callback(images,*args,**kwargs)

    @staticmethod
    def _values(value) -> list[Path]:
        selected=value() if callable(value) else value
        return [Path(path) for path in (selected or ())]

    def _readable_images(self) -> list[Path]:
        return self._values(getattr(self.dataset,"readable_images",()))

    def _manager_for(self,image:Path):
        resolver=getattr(self.dataset,"manager_for_path",None)
        return resolver(image) if callable(resolver) else self.dataset

    def _next_request(self):
        with self._lock:
            broad=self._pending_all
            requested=None if broad else set(self._pending)
            causes=set(self._pending_causes)
            dependency_tags=set(self._pending_dependency_tags)
            dependency_relationships=set(
                self._pending_dependency_relationships
            )
            self._pending_all=False; self._pending.clear()
            self._pending_dependency_tags.clear()
            self._pending_dependency_relationships.clear()
            self._pending_causes.clear()
            self._inflight_generation+=1
            generation=self._inflight_generation
            has_work=bool(
                broad or requested or dependency_tags
                or dependency_relationships
            )
            if has_work:
                if self._inflight is None:
                    self._inflight=(
                        generation,broad,requested,causes,dependency_tags,
                        dependency_relationships,
                    )
                else:
                    (
                        _prior_generation,prior_broad,prior_requested,
                        prior_causes,prior_dependency_tags,
                        prior_dependency_relationships,
                    )=self._inflight
                    combined_broad=bool(prior_broad or broad)
                    self._inflight=(
                        generation,combined_broad,
                        (
                            None if combined_broad else
                            set(prior_requested or ())|set(requested or ())
                        ),
                        set(prior_causes)|set(causes),
                        set(prior_dependency_tags)|set(dependency_tags),
                        set(prior_dependency_relationships)
                        |set(dependency_relationships),
                    )
        return (
            generation,broad,requested,causes,dependency_tags,
            dependency_relationships,
        )

    def _merge_request_locked(
        self,broad,requested,causes,dependency_tags,
        dependency_relationships,
    ) -> None:
        if broad:
            self._pending_all=True; self._pending.clear()
            self._pending_dependency_tags.clear()
            self._pending_dependency_relationships.clear()
        elif not self._pending_all:
            self._pending.update(requested or ())
            self._pending_dependency_tags.update(dependency_tags or ())
            self._pending_dependency_relationships.update(
                dependency_relationships or ()
            )
        self._pending_causes.update(causes or ())
        self._wake.set()

    def defer_for_visible_work(
        self,job_id:str,*,label:str="Visible work",timeout:float=2.0,
    ) -> bool:
        """Yield only this coordinator's safe job and retain its exact scope."""
        done=Event()
        with self._lock:
            job=self._job
            if (
                not self._started or job is None or job.id!=str(job_id)
                or job.state not in {"queued","running","paused"}
            ):
                return False
            self._deferred_for_visible_work=True
            self._preempted_jobs[job.id]=str(label or "Visible work")
            if self._inflight is not None:
                (
                    _generation,broad,requested,causes,dependency_tags,
                    dependency_relationships,
                )=self._inflight
                self._merge_request_locked(
                    broad,requested,causes,dependency_tags,
                    dependency_relationships,
                )
            cause=self._cause_text(set(self._pending_causes))
            scope="all" if self._pending_all else "images"
        token=self.jobs.observe(job.id,lambda _job:done.set())
        self.jobs.cancel(job.id)
        completed=done.wait(max(0.0,float(timeout)))
        if token and not completed:
            self.jobs.unobserve(job.id,token)
        if completed:
            return True
        with self._lock:
            self._deferred_for_visible_work=False
            pending=self._has_pending_locked()
        self._publish(
            "deferred",cause=cause,scope=scope,current=job.current,
            total=job.total,message=(
                f"Automatic refresh is yielding to {label}; the visible "
                "operation can be retried after the current image checkpoint."
            ),
        )
        if pending and self._started and not self.jobs.shutting_down:
            self.request((),cause=cause)
        return False

    def resume_after_visible_work(self) -> None:
        """Resume coalesced derived work after its visible blocker releases."""
        with self._lock:
            if not self._deferred_for_visible_work:
                return
            self._deferred_for_visible_work=False
            pending=self._has_pending_locked()
            cause=self._cause_text(set(self._pending_causes))
        if pending and self._started and not self.jobs.shutting_down:
            self.request((),cause=cause)

    def _has_pending_locked(self) -> bool:
        return bool(
            self._pending_all or self._pending
            or self._pending_dependency_tags
            or self._pending_dependency_relationships
        )

    def _dependency_images(
        self,readable,*,tag_ids:set[str],relationship_ids:set[str],trace=None,
    ) -> set[Path]:
        grouped=defaultdict(list); selected=set()
        for image in readable:
            manager=self._manager_for(image)
            if manager is None or getattr(manager,"index",None) is None:
                if trace is not None:trace.expansion_reason("dependency_cache_unavailable")
                selected.add(image)
                continue
            grouped[id(manager)].append((manager,image))
        for values in grouped.values():
            manager=values[0][0]
            paths={item[1] for item in values}
            resolver=getattr(manager.index,"managed_dependency_refresh_images",None)
            if not callable(resolver):
                resolver=getattr(manager.index,"managed_dependency_images",None)
            try:
                matches=(resolver(
                    paths,tag_ids=tag_ids,relationship_ids=relationship_ids,
                ) if callable(resolver) else None)
            except Exception:
                matches=None
            # An unavailable cache needs repair only within its own library;
            # it cannot invalidate known nonmatches in another library.
            selected.update(paths if matches is None else matches)
            if trace is not None:
                trace.expansion_reason("dependency_cache_unavailable" if matches is None else "dependency_matches")
        return selected

    def _run(self,job:JobProgress):
        # request() binds the trace while holding this lock, before the worker
        # can begin. Observers may read only its aggregate snapshot.
        with self._lock:
            trace=getattr(job,"_derived_refresh_trace",None)
        if trace is not None:trace.start()
        job.update(0,0,"Preparing automatic image refresh")
        refreshed=[]; affected=[]; reused=0; skipped=0; failed=0; messages=[]
        review_created=review_refreshed=review_retired=0
        review_legacy_retired=review_failed=0
        broad_result=False; all_causes=set(); processed=0
        workflow_proofs={}
        while job.checkpoint():
            # Let a short synchronous burst settle before snapshotting its
            # union. This prevents one editor save from becoming several jobs
            # when multiple consumers publish the same exact authority event.
            settle_deadline=time.monotonic()+0.25
            while job.checkpoint() and time.monotonic()<settle_deadline:
                self._wake.clear()
                if not self._wake.wait(0.025):
                    break
            (
                generation,broad,requested,causes,dependency_tags,
                dependency_relationships,
            )=self._next_request()
            if (
                not broad and not requested and not dependency_tags
                and not dependency_relationships
            ):
                break
            requested_count=None if broad else len(requested)
            with self._phase(job,"scope"):
                readable_values=self._readable_images(); readable=set(readable_values)
                if not broad and (dependency_tags or dependency_relationships):
                    dependency_images=self._dependency_images(
                        readable_values,tag_ids=dependency_tags,
                        relationship_ids=dependency_relationships,trace=trace,
                    )
                    if dependency_images is None:
                        broad=True; requested=None
                    else:
                        requested.update(dependency_images)
            broad_result=bool(broad_result or broad); all_causes.update(causes)
            paths=list(readable_values) if broad else [
                path for path in requested if path in readable
            ]
            if trace is not None:
                trace.scope(requested_count=requested_count,final_count=len(paths),broad=broad)
            skipped+=len(readable if broad else requested)-len(paths)
            cause=self._cause_text(all_causes)
            job.reason=cause
            if broad and trace is not None and any(
                item.get("expansion_reason") in {"incomplete_dependency_contract","unsupported_dependency_kind","missing_dependency_ids"}
                for item in trace.snapshot().get("requests",())
            ):
                job.reason=cause+". Full refresh is required because the changed items are not fully identified."
            job.update(5*processed,5*(processed+len(paths)),"Refreshing current image results")
            self._publish(
                "running",cause=cause,scope="all" if broad else "images",
                current=processed,total=processed+len(paths),
            )
            for path in paths:
                workflow_proofs.pop(str(path),None)
            result=self._refresh_paths(
                paths,job,processed,processed+len(paths),cause=cause,
            )
            self._count(job,"derivation_reused",result["reused"])
            self._count(job,"failed_images",result["failed"])
            self._count(job,"skipped_images",result["skipped"])
            refreshed.extend(result["refreshed"]); reused+=result["reused"]
            # Failed cache work still invalidates its exact attempted status;
            # an unavailable library outside the readable scope stays held.
            affected.extend(str(path) for path in paths)
            workflow_proofs.update(result.get("workflow_proofs",{}))
            skipped+=result["skipped"]; failed+=result["failed"]
            review_created+=result.get("review_created",0)
            review_refreshed+=result.get("review_refreshed",0)
            review_retired+=result.get("review_retired",0)
            review_legacy_retired+=result.get("review_legacy_retired",0)
            review_failed+=result.get("review_failed",0)
            messages.extend(result["messages"]); processed+=len(paths)
            if result.get("cancelled"):
                job.result={
                    "refreshed":tuple(dict.fromkeys(refreshed)),"reused":reused,
                    "affected":tuple(dict.fromkeys(affected)),
                    "skipped":skipped,"failed":failed,
                    "review_created":review_created,
                    "review_refreshed":review_refreshed,
                    "review_retired":review_retired,
                    "review_legacy_retired":review_legacy_retired,
                    "review_failed":review_failed,
                    "messages":tuple(messages),
                    "scope":"all" if broad_result else "images",
                    "cause":self._cause_text(all_causes),
                }
                raise InterruptedError()
        return {
            "refreshed":tuple(dict.fromkeys(refreshed)),"reused":reused,
            "affected":tuple(dict.fromkeys(affected)),
            "workflow_proofs":workflow_proofs,
            "skipped":skipped,"failed":failed,"messages":tuple(messages),
            "review_created":review_created,
            "review_refreshed":review_refreshed,
            "review_retired":review_retired,
            "review_legacy_retired":review_legacy_retired,
            "review_failed":review_failed,
            "scope":"all" if broad_result else "images",
            "cause":self._cause_text(all_causes),
        }

    def _refresh_paths(self,paths,job,offset,total,*,cause):
        refreshed=[]; affected=[]; reused=0; skipped=0; failed=0; messages=[]
        review_created=review_refreshed=review_retired=0
        review_legacy_retired=review_failed=0
        if not paths:
            return {
                "refreshed":refreshed,"reused":0,"skipped":0,"failed":0,
                "affected":affected,
                "messages":messages,"cancelled":False,
                "review_created":0,"review_refreshed":0,
                "review_retired":0,"review_legacy_retired":0,
                "review_failed":0,
            }
        knowledge=getattr(self.dataset,"knowledge",None)
        gather_source_evidence=bool(
            getattr(self.settings,"auto_import_source_evidence",True)
        )
        service=(
            self.initializer_factory(knowledge,gather_source_evidence)
            if self.initializer_factory is not None else
            ImageAuthorityInitializationService(
                knowledge=knowledge,
                gather_source_evidence=gather_source_evidence,
            )
        )
        rules=service.current_rules_fingerprint()
        job_fingerprint=service.current_job_fingerprint()
        grouped=defaultdict(list)
        for path in paths:
            manager=self._manager_for(path)
            if manager is None or getattr(manager,"index",None) is None:
                skipped+=1
            else:
                grouped[id(manager)].append((manager,path))
        position=offset
        workflow_proofs={}
        for values in grouped.values():
            manager=values[0][0]; manager_paths=[item[1] for item in values]
            index_store=manager.index
            derived_by_image={}
            group_start=position
            group_count=len(manager_paths)
            def phase_progress(phase,current,phase_total,message):
                if current==0 or current==phase_total or current%64==0:
                    job.update(5*group_start+phase*group_count+current,5*total,
                        f"{message}: {current:,}/{phase_total:,}")
            try:
                with self._phase(job,"cache_read"):
                    trusted=index_store.trusted_managed_images(
                        manager_paths,rules_fingerprint=rules,
                        job_fingerprint=job_fingerprint,require_rule_results=True,
                    )
                    connection=index_store.connect()
            except Exception as exc:
                failed+=len(manager_paths); position+=len(manager_paths)
                messages.append(
                    "One library's rebuildable local cache is unavailable; "
                    f"its images were held for Recheck: {exc}"
                )
                job.update(
                    5*position,5*total,
                    f"Held {len(manager_paths):,} image(s) with unavailable local cache",
                )
                continue
            pending_updates=[]
            group_refreshed=len(refreshed); group_reused=reused
            group_failed=failed; group_start=position

            def commit_pending() -> None:
                if not pending_updates:
                    return
                # Publish live aggregates only after the matching disposable
                # cache transaction is durable. A cancellation may therefore
                # roll back the current batch without leaving memory ahead of
                # the local index.
                with self._phase(job,"checkpoint"):
                    connection.commit()
                self._count(job,"checkpoint_commits")
                self._count(job,"cache_rows_written",len(pending_updates))
                manager.replace_cached_batch(tuple(
                    (cached,previous)
                    for _pending_image,cached,previous in pending_updates
                ))
                for pending_image,_cached,_previous in pending_updates:
                    refreshed.append(str(pending_image))
                pending_updates.clear()

            try:
                # Trusted derivations never consume an old row. Already
                # validated rows are held by the manager; fetch only the
                # remaining previous values needed for aggregate replacement.
                previous_needed=[
                    image for image in manager_paths
                    if image not in trusted
                    and image not in manager.validated_records
                    and image in manager.indexed_images
                ]
                with self._phase(job,"cache_read"):
                    cached_by_path=(
                        index_store.records_for_paths(previous_needed,connection)
                        if previous_needed else {}
                    )
                self._count(job,"previous_rows_loaded",len(cached_by_path))
                for _manager,image in values:
                    if not job.checkpoint():
                        raise InterruptedError()
                    position+=1
                    if image in trusted:
                        with self._phase(job,"cache_read"):
                            derived_by_image[image]=index_store.managed_rule_result(
                                image,rules_fingerprint=rules,db=connection,
                            ) or {}
                        reused+=1
                        job.update(5*group_start+position-group_start,5*total,"Refreshing image results")
                        if position%16==0:
                            # This rebuild is intentionally low-priority. A
                            # tiny periodic sleep releases the GIL so gallery
                            # input and painting stay responsive on large
                            # startup scopes without materially changing total
                            # refresh time.
                            time.sleep(0.001)
                        continue
                    previous=manager.validated_records.get(image)
                    if previous is None and image in manager.indexed_images:
                        previous=cached_by_path.get(image)
                    sidecar=image.with_suffix(".tags.json")
                    savepoint_active=False
                    try:
                        if not connection.in_transaction:
                            connection.execute("BEGIN")
                        connection.execute("SAVEPOINT derived_refresh_image")
                        savepoint_active=True
                        if not sidecar.is_file():
                            cached=index_store.put(
                                image,[],db=connection,tag_sources={},
                                plain_language=[],
                            )
                            index_store.invalidate_managed_image(image,db=connection)
                        else:
                            stat=sidecar.stat()
                            before_signature=(stat.st_size,stat.st_mtime_ns)
                            before=sidecar.read_bytes()
                            stat=sidecar.stat()
                            if (stat.st_size,stat.st_mtime_ns)!=before_signature:
                                raise RuntimeError(
                                    "Image Knowledge changed during its derived refresh."
                                )
                            payload=json.loads(before.decode("utf-8"))
                            if not isinstance(payload,dict):
                                raise ValueError(
                                    "The Image Knowledge sidecar root is not an object."
                                )
                            record=ImageRecord(image,auto_load=False)
                            record.loaded=True
                            record.annotation=ImageAnnotation.from_dict(
                                payload,image.name,
                            )
                            authority_sha=hashlib.sha256(before).hexdigest()
                            with self._phase(job,"derive"):
                                derived=service.derive_rule_result(
                                    record,authority_sha256=authority_sha,
                                )
                            self._count(job,"derivation_computed")
                            derived_by_image[image]=derived
                            tag_sources=typed_tag_sources(record,knowledge)
                            cached=index_store.put(
                                image,list(tag_sources),db=connection,
                                tag_sources=tag_sources,
                                plain_language=record.annotation.plain_language,
                                preserve_current_workflow=True,
                            )
                            index_store.record_managed_completion(
                                image,rules_fingerprint=rules,
                                job_fingerprint=job_fingerprint,
                                derived_result=derived,db=connection,
                            )
                            stat=sidecar.stat()
                            if (
                                (stat.st_size,stat.st_mtime_ns)!=before_signature
                                or sidecar.read_bytes()!=before
                            ):
                                raise RuntimeError(
                                    "Image Knowledge changed during its derived refresh."
                                )
                        connection.execute("RELEASE derived_refresh_image")
                        savepoint_active=False
                        pending_updates.append((image,cached,previous))
                        if len(pending_updates)>=64:
                            commit_pending()
                    except Exception as exc:
                        if savepoint_active:
                            try:
                                connection.execute("ROLLBACK TO derived_refresh_image")
                                connection.execute("RELEASE derived_refresh_image")
                            except Exception:
                                connection.rollback(); pending_updates.clear()
                        failed+=1; messages.append(str(exc))
                    finally:
                        job.update(5*group_start+position-group_start,5*total,"Refreshing image results")
                        if position%16==0:
                            time.sleep(0.001)
                commit_pending()
            except InterruptedError:
                connection.rollback()
                return {
                    "refreshed":refreshed,"reused":reused,"skipped":skipped,
                    "failed":failed,"messages":messages,"cancelled":True,
                    "review_created":review_created,
                    "review_refreshed":review_refreshed,
                    "review_retired":review_retired,
                    "review_legacy_retired":review_legacy_retired,
                    "review_failed":review_failed,
                }
            except Exception as exc:
                try:
                    connection.rollback()
                except Exception:
                    pass
                pending_updates.clear()
                accounted=(
                    len(refreshed)-group_refreshed
                    +reused-group_reused+failed-group_failed
                )
                held=max(0,len(manager_paths)-accounted)
                failed+=held
                position=group_start+len(manager_paths)
                messages.append(
                    "One library's rebuildable local cache stopped during "
                    f"refresh; {held:,} image(s) were held for Recheck: {exc}"
                )
                job.update(
                    5*position,5*total,
                    f"Held {held:,} image(s) after a local-cache failure",
                )
                continue
            finally:
                try:
                    connection.close()
                except Exception as exc:
                    messages.append(
                        "One library's local cache did not close cleanly; "
                        f"its committed checkpoints remain rebuildable: {exc}"
                    )
            if self.reviews is not None:
                try:
                    identity_resolver=getattr(self.dataset,"identity_for_path",None)
                    identity=(
                        identity_resolver(manager_paths[0])
                        if callable(identity_resolver) and manager_paths else None
                    )
                    library_id=str(getattr(identity,"library_id","") or "")
                    review_service=(
                        self.review_service_factory(
                            manager,knowledge,library_id,
                        )
                        if self.review_service_factory is not None else
                        ImageQuestionService(
                            Path(manager.root),inspect_metadata=False,
                            knowledge=knowledge,library_id=library_id,
                            external_evidence=False,
                        )
                    )
                    review_result=self._phase_images(job,"review",review_service.queue_for_images,
                        manager_paths,self.reviews,
                        derived_results=derived_by_image,cause=cause,
                        should_cancel=lambda:not job.checkpoint(),
                        progress=lambda current,total,message:phase_progress(1,current,total,message),
                    )
                    review_created+=int(review_result.get("created",0))
                    review_refreshed+=int(review_result.get("refreshed",0))
                    review_retired+=int(review_result.get("retired",0))
                    review_legacy_retired+=int(
                        review_result.get("legacy_retired",0)
                    )
                    review_failed+=int(review_result.get("errors",0))
                    messages.extend(review_result.get("messages",()))
                    if review_result.get("cancelled"):
                        self._phase_images(job,"dependency",index_store.update_managed_dependency_batch,manager_paths)
                        return {
                            "refreshed":refreshed,"reused":reused,
                            "affected":affected,
                            "skipped":skipped,"failed":failed,
                            "messages":messages,"cancelled":True,
                            "review_created":review_created,
                            "review_refreshed":review_refreshed,
                            "review_retired":review_retired,
                            "review_legacy_retired":review_legacy_retired,
                            "review_failed":review_failed,
                        }
                    dependency_details=review_result.get("dependencies",{})
                    self._phase_images(job,"dependency",index_store.update_managed_dependency_batch,
                        manager_paths,dependency_details,should_continue=job.checkpoint,
                        progress=lambda current,total:phase_progress(3,current,total,"Saving dependency coverage"),
                    )
                except InterruptedError:
                    return {
                        "refreshed":refreshed,"reused":reused,"affected":affected,
                        "skipped":skipped,"failed":failed,"messages":messages,"cancelled":True,
                        "review_created":review_created,"review_refreshed":review_refreshed,
                        "review_retired":review_retired,"review_legacy_retired":review_legacy_retired,
                        "review_failed":review_failed,
                    }
                except Exception as exc:
                    self._phase_images(job,"dependency",index_store.update_managed_dependency_batch,manager_paths)
                    review_failed+=len(manager_paths)
                    messages.append(f"Review queue refresh failed: {exc}")
            else:
                self._phase_images(job,"dependency",index_store.update_managed_dependency_batch,manager_paths)
            proof_reader=getattr(index_store,"current_workflow_proofs",None)
            if callable(proof_reader):
                workflow_proofs.update(self._phase_images(job,"workflow",proof_reader,
                    manager_paths,should_cancel=lambda:not job.checkpoint(),
                    progress=lambda current,total:phase_progress(4,current,total,"Validating saved image status"),
                ))
            job.update(5*position,5*total,"Current image results ready")
            affected.extend(str(path) for path in manager_paths)
        return {
            "refreshed":refreshed,"reused":reused,"skipped":skipped,
            "affected":affected,
            "workflow_proofs":workflow_proofs,
            "failed":failed,"messages":messages,"cancelled":False,
            "review_created":review_created,
            "review_refreshed":review_refreshed,
            "review_retired":review_retired,
            "review_legacy_retired":review_legacy_retired,
            "review_failed":review_failed,
        }

    @staticmethod
    def _cause_text(causes:set[str]) -> str:
        values=sorted(value for value in causes if value)
        if not values:
            return "Derived data changed"
        if len(values)==1:
            return values[0]
        return "Image Knowledge, System Knowledge, or Automation rules changed"

    def _finished(self,job:JobProgress) -> None:
        trace=getattr(job,"_derived_refresh_trace",None)
        if trace is not None:trace.finish(job.state)
        result=job.result if isinstance(job.result,dict) else {}
        paths=tuple(result.get("affected",result.get("refreshed",())))
        scope=str(result.get("scope") or "images")
        message="; ".join(dict.fromkeys(result.get("messages",())))
        with self._lock:
            preempted_label=self._preempted_jobs.pop(job.id,"")
            if self._job is job:
                self._job=None; self._token=""; self._inflight=None
            pending=self._has_pending_locked()
            deferred=self._deferred_for_visible_work
            cause=self._cause_text(set(self._pending_causes))
        state=(
            "deferred"
            if preempted_label and job.state in {"cancelled","complete"}
            else job.state
        )
        self._publish(
            state,cause=str(result.get("cause") or "Derived data changed"),
            scope=scope,current=job.current//5,total=job.total//5,
            refreshed=len(result.get("refreshed",())),
            reused=int(result.get("reused",0)),
            skipped=int(result.get("skipped",0)),
            failed=int(result.get("failed",bool(job.error))),
            review_created=int(result.get("review_created",0)),
            review_refreshed=int(result.get("review_refreshed",0)),
            review_retired=int(result.get("review_retired",0)),
            review_legacy_retired=int(result.get("review_legacy_retired",0)),
            review_failed=int(result.get("review_failed",0)),
            images=paths,workflow_proofs=result.get("workflow_proofs",{}),message=(
                f"Automatic refresh yielded to {preempted_label} and will resume "
                "after that visible operation finishes."
                if state=="deferred" else message or job.error
            ),
        )
        if (
            pending and not deferred and self._started
            and not self.jobs.shutting_down
        ):
            self.request((),cause=cause)

    def _publish(
        self,state:str,*,cause:str,scope:str,current:int,total:int,
        refreshed:int=0,reused:int=0,skipped:int=0,failed:int=0,
        review_created:int=0,review_refreshed:int=0,review_retired:int=0,
        review_legacy_retired:int=0,review_failed:int=0,
        images=(),message:str="",workflow_proofs=None,
    ) -> None:
        self.events.publish(
            "derived_refresh_changed",state=str(state),cause=str(cause),
            scope=str(scope),current=max(0,int(current)),total=max(0,int(total)),
            refreshed=max(0,int(refreshed)),reused=max(0,int(reused)),
            skipped=max(0,int(skipped)),failed=max(0,int(failed)),
            review_created=max(0,int(review_created)),
            review_refreshed=max(0,int(review_refreshed)),
            review_retired=max(0,int(review_retired)),
            review_legacy_retired=max(0,int(review_legacy_retired)),
            review_failed=max(0,int(review_failed)),
            images=tuple(images),message=str(message or ""),
            workflow_proofs=dict(workflow_proofs or {}),
        )
