"""Software selections in canonical Manager settings, separate from workflow preferences."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from uuid import uuid4

from core.backend_connection_settings import BackendConnectionSettings, MAX_SETTINGS_BYTES
from core.settings import AppSettings, _settings_writer
from core.workflow_connection import WorkflowConnection, WorkflowConnectionHold, workflow_component


KEYS = {'collectigen.generate': 'manager_generate_connection',
        'collectigen.train': 'manager_train_connection'}
FIELDS = ('component_id', 'directory', 'version', 'inventory_sha256', 'manifest_sha256', 'binding_revision')


class WorkflowConnectionStore:
    def __init__(self, path, component_id):
        workflow_component(component_id)
        self.component_id, self.key = component_id, KEYS[component_id]
        self.settings = BackendConnectionSettings(path)

    def observe(self):
        return self.settings.observe()

    def selected(self, snapshot):
        row = snapshot._payload.get(self.key)
        if row is None:
            return None
        if type(row) is not dict:
            raise WorkflowConnectionHold('saved_workflow_requires_review')
        # Explicit first-install recovery can leave future extension fields intact.
        if not any(field in row for field in ('schema_version', *FIELDS)):
            return None
        if (type(row.get('schema_version')) is not int or row['schema_version'] != 1
                or row.get('component_id') != self.component_id
                or any(type(row.get(field)) is not str for field in FIELDS)):
            raise WorkflowConnectionHold('saved_workflow_requires_review')
        return WorkflowConnection(row['component_id'], Path(row['directory']), row['version'],
            row['inventory_sha256'], row['manifest_sha256'], row['binding_revision'])

    def save(self, selected, *, expected):
        if type(selected) is not WorkflowConnection or selected.component_id != self.component_id:
            raise WorkflowConnectionHold('same_workflow_connection_required')
        with _settings_writer(self.settings.path):
            current = self.observe()
            if current.digest != expected.digest:
                raise WorkflowConnectionHold('settings_changed_review_again')
            previous = current._payload.get(self.key, {})
            if type(previous) is not dict:
                raise WorkflowConnectionHold('saved_workflow_requires_review')
            self.selected(current)  # Never silently overwrite a foreign/invalid selection.
            saved = replace(selected, binding_revision=uuid4().hex)
            payload = deepcopy(current._payload)
            payload[self.key] = {**previous, 'schema_version': 1,
                **{field: str(getattr(saved, field)) for field in FIELDS}}
            if len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) > MAX_SETTINGS_BYTES:
                raise WorkflowConnectionHold('settings_size_limit')
            AppSettings._from_payload(payload)._save_locked(self.settings.path, _patch_payload=payload)
        result = self.observe()
        if self.selected(result) != saved:
            raise WorkflowConnectionHold('saved_workflow_readback_changed')
        return result

    def clear(self, *, expected):
        with _settings_writer(self.settings.path):
            current = self.observe()
            if current.digest != expected.digest:
                raise WorkflowConnectionHold('settings_changed_review_again')
            self.selected(current)
            if self.key not in current._payload:
                return current
            payload = deepcopy(current._payload)
            remaining = {key: value for key, value in payload[self.key].items()
                         if key not in ('schema_version', *FIELDS)}
            if remaining:
                payload[self.key] = remaining
            else:
                payload.pop(self.key)
            AppSettings._from_payload(payload)._save_locked(self.settings.path, _patch_payload=payload)
        result = self.observe()
        if self.selected(result) is not None:
            raise WorkflowConnectionHold('workflow_clear_readback_changed')
        return result
