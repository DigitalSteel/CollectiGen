"""Ingest preparation uses the shared frozen request with its own fingerprint root."""
from pathlib import Path
from dataclasses import replace
import json
from .dataset_preparation_request import DatasetPreparationRequestWork


class IngestPreparationRequestWork(DatasetPreparationRequestWork):
    def __init__(self,*args,ingest_root=None,**kwargs):
        self.ingest_root=Path(ingest_root).absolute() if ingest_root is not None else None
        super().__init__(*args,**kwargs)
        authority=self.request.snapshot("authority")
        authority["ingest_fingerprint_root"]=str(self.ingest_root) if self.ingest_root is not None else None
        self.request=replace(self.request,authority_json=json.dumps(authority,sort_keys=True,separators=(",",":")))

    def _warm_fingerprints(self,job,cancelled,registry):
        from .duplicate_preflight import warm_duplicate_fingerprints
        return warm_duplicate_fingerprints(self.state.duplicates,self.paths,self.ingest_root,
            progress=job.update,should_cancel=cancelled)

    def validate(self,request):
        root=str(self.ingest_root) if self.ingest_root is not None else None
        if root!=request.snapshot("authority").get("ingest_fingerprint_root"):
            return "Incoming fingerprint scope changed. Review and submit a new request."
        return super().validate(request)
