"""Durable pull outcome guard; a disconnect never means server cancellation."""
from hashlib import sha256
import os
from core.ollama_owned_executor import read_json, require
from suite_core.discovery.component_staging import guard, atomic_bytes, canonical


class PullOutcomeRecord:
    def __init__(self, path):
        guard(path);self.path=path
        path.parent.mkdir(parents=True,exist_ok=True)

    def begin(self, plan):
        # One exclusively created record per reviewed pull. An existing record
        # (including completed) is never reused as implicit retry authority.
        guard(self.path)
        value={'schema':1,'phase':'submitted_outcome_unknown','fingerprint':plan.fingerprint,
            'endpoint_pin':plan.endpoint_pin,'model':plan.model.__dict__}
        with self.path.open('xb') as stream:
            stream.write(canonical(value));stream.flush();os.fsync(stream.fileno())
        require(read_json(self.path)==value,'pull_outcome_readback_failed')

    def complete(self, plan):
        value=read_json(self.path)
        require(value['fingerprint']==plan.fingerprint and value['phase']=='submitted_outcome_unknown','pull_outcome_record_changed')
        value['phase']='stream_success_and_inventory_verified'
        atomic_bytes(self.path,canonical(value))
        require(read_json(self.path)==value,'pull_outcome_readback_failed')

    def reconcile(self, plan, *, installed, reviewed_fingerprint):
        """Record content reconciliation, not proof of server quiescence/retry."""
        require(reviewed_fingerprint==plan.fingerprint,'pull_reconciliation_review_required')
        value=read_json(self.path)
        require(value['fingerprint']==plan.fingerprint and value['phase']=='submitted_outcome_unknown','pull_outcome_record_changed')
        require(plan.model in installed,'pull_content_not_reconciled')
        value['phase']='exact_content_present_server_activity_unknown'
        atomic_bytes(self.path,canonical(value))
        require(read_json(self.path)==value,'pull_outcome_readback_failed')
        return value
