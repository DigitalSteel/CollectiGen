"""Frozen Dataset preparation work; no Qt objects or page fields in workers."""
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from enum import Enum
import hashlib
from pathlib import Path

from .preparation_requests import PreparationRequest
from .preparation_workflow import PreparationOptions


def serializable(value):
    if is_dataclass(value):return serializable(asdict(value))
    if isinstance(value,dict):return {str(k):serializable(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):return [serializable(v) for v in value]
    if isinstance(value,Path):return str(value)
    if isinstance(value,Enum):return serializable(value.value)
    if isinstance(value,bytes):return {"sha256":hashlib.sha256(value).hexdigest()}
    if value is None or isinstance(value,(str,int,float,bool)):return value
    raise TypeError("Preparation cannot snapshot a live object.")


def preparation_settings(settings):
    prefixes=("wd_","joycaption_","florence_","ai_runtime_","local_ai_")
    names={"enabled_wd_taggers","ai_models_directory","plain_language_model",
           "ignored_plain_language_sentences","auto_merge_ai_tags","history_storage","library_profiles","dataset_directory","ingest_directory","working_directory"}
    return serializable({name:value for name,value in vars(settings).items()
                         if name in names or name.startswith(prefixes)})


def family_signatures(paths):
    values={}
    for path in paths:
        path=Path(path)
        family=[]
        for member in (path,path.with_suffix(".tags.json"),path.with_suffix(".txt")):
            try:
                stat=member.stat()
                family.append([stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns,stat.st_ino,stat.st_dev])
            except FileNotFoundError:family.append(None)
        if family[0] is None:raise ValueError("A selected image is no longer available.")
        values[str(path)]=family
    return values


class DatasetPreparationRequestWork:
    """Own one accepted scope/configuration/history identity across queue wait.

    The original capture operation is retained, never recreated on admission.
    File stat signatures are a conservative preflight; canonical source hashes,
    locks, write receipts and history authority guards remain in the service.
    """
    def __init__(self, state, settings, paths, options, *, history_operation,
                 history_configuration, authority_token, journal_path,
                 create_fingerprints, service_factory, tagger_factory,
                 captioner_factory, preferred_factory, journal_factory):
        self.delivery=None
        self.continuation=None
        self.state=state
        self.settings=deepcopy(settings)
        self.current_settings=settings
        self.paths=tuple(Path(p).absolute() for p in paths)
        self.options=options
        self.history=history_operation
        self.configuration=history_configuration
        self.authority_token=authority_token
        self.journal_path=Path(journal_path)
        self.create_fingerprints=bool(create_fingerprints)
        self.service_factory=service_factory
        self.tagger_factory=tagger_factory
        self.captioner_factory=captioner_factory
        self.preferred_factory=preferred_factory
        self.journal_factory=journal_factory
        self.expected_history=self._history_snapshot()
        self.expected_authority=serializable(authority_token())
        self.expected_models=self._model_snapshot(self.settings)
        self.request=PreparationRequest.capture(self.paths,
            settings=preparation_settings(self.settings),options=asdict(options),
            history={"operation_id":getattr(self.history,"operation_id",""),
                     "descriptors":self.expected_history},
            authority={"families":family_signatures(self.paths),
                       "knowledge":self.expected_authority,"models":self.expected_models,
                       "fingerprints":self.create_fingerprints},
            resource="gpu" if options.suggest_sdxl_tags or options.suggest_plain_language else "io",
            allow_memory_pressure=options.allow_memory_pressure)

    def _history_snapshot(self):
        values={}
        for path in self.paths:
            descriptor=self.configuration.resolve(path=path)
            row=serializable(descriptor)
            if self.history is not None:
                # Freeze actual admitted head as well as lazy configuration.
                context=self.history.context_for(path)
                head=context.store.current_head(context.entity)
                row={"descriptor":row,"head":getattr(head,"version_id",None),
                     "entity":str(context.entity)}
            values[str(path)]=row
        return values

    def _model_snapshot(self,settings):
        backends=list(self.tagger_factory(settings)) if self.options.suggest_sdxl_tags else []
        if self.options.suggest_plain_language or self.options.prepares_preferred_descriptions:
            captioner=self.captioner_factory(settings)
            if captioner is not None:backends.append(captioner)
        values=[]
        for backend in backends:
            spec=getattr(backend,"runtime_spec",None)
            identity=spec().key if callable(spec) else backend.evidence_identity()
            values.append(serializable(identity))
        return values

    def validate(self, request):
        try:
            if preparation_settings(getattr(self.state,"settings",self.current_settings))!=request.snapshot("settings"):
                return "Preparation settings changed while waiting. Review and submit a new request."
            expected=request.snapshot("authority")
            if family_signatures(self.paths)!=expected["families"]:
                return "Selected image or sidecar changed while waiting. Review and submit a new request; history was not rebased."
            if self._history_snapshot()!=self.expected_history:
                return "Saved-history authority or head changed while waiting. Review and submit a new request."
            if serializable(self.authority_token())!=self.expected_authority:
                return "System Knowledge changed while waiting. Review and submit a new request."
            if self._model_snapshot(self.settings)!=self.expected_models:
                return "Selected model files or runtime identity changed while waiting. Review and submit a new request."
        except Exception:
            return "Preparation authority could not be verified. No work started; review this request again."
        return ""

    def factory(self,request):
        # New mutable adapters per request; no page/editor callbacks or settings.
        taggers=self.tagger_factory(self.settings) if self.options.suggest_sdxl_tags else []
        captioner=self.captioner_factory(self.settings) if (
            self.options.suggest_plain_language or self.options.prepares_preferred_descriptions) else None
        journal=self.journal_factory(self.journal_path)
        preferred=(self.preferred_factory(journal,lambda record:captioner.evidence_identity(),
                   ignored_plain_language=self.settings.ignored_plain_language_sentences)
                   if self.options.prepares_preferred_descriptions else None)
        service=self.service_factory(self.state.knowledge,self.state.ai,
            preferred_caption_service=preferred,journal=journal)
        registry=self.settings.library_registry() if self.create_fingerprints else None
        has_steps=any((self.options.read_external_evidence,self.options.suggest_sdxl_tags,
                       self.options.suggest_plain_language,self.options.has_automated_application))
        def run(job):
            cancelled=lambda:not job.checkpoint()
            if self.history is not None:self.history.cancelled=cancelled
            lines=[];errors=[]
            if self.create_fingerprints:
                warm=self._warm_fingerprints(job,cancelled,registry)
                if warm.cancelled or cancelled():
                    return {"processed":0,"cancelled":True,"skip_followup_state_scan":True,
                            "summary_lines":["Fingerprint creation cancelled safely; completed cache entries remain reusable."]}
                lines.append(f"Duplicate fingerprints: {warm.indexed:,} new or refreshed, {warm.reused:,} reused, {warm.issue_count:,} held.")
                errors=[str(issue.detail) for issue in warm.issues]
            if has_steps:
                kwargs=dict(tag_backends=taggers,description_backend=captioner,
                    minimum_tag_agreement=self.settings.wd_consensus_minimum,
                    progress=job.update,should_cancel=cancelled,
                    **({"history_operation":self.history} if self.history is not None else {}))
                create=getattr(service,"create_continuation",None)
                if callable(create):
                    self.continuation=create(self.paths,self.options,
                        request_id=request.id,**kwargs)
                    result=self.continuation.drain().as_page_result()
                else:
                    # Compatibility for existing run-only service adapters.
                    result=service.run(self.paths,self.options,**kwargs).as_page_result()
            else:result={"processed":len(self.paths),"errors":[],"summary_lines":[],"skip_followup_state_scan":True}
            result["summary_lines"]=lines+list(result.get("summary_lines",()))
            result["errors"]=errors+list(result.get("errors",()))
            return result
        return run

    def _warm_fingerprints(self,job,cancelled,registry):
        from .duplicate_preflight import warm_dataset_duplicate_fingerprints
        return warm_dataset_duplicate_fingerprints(self.state.duplicates,self.paths,registry,
            progress=job.update,should_cancel=cancelled)
