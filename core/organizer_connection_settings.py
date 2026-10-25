"""Candidate extension of canonical settings; explicit save only, no defaults."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from uuid import uuid4
from core.backend_connection_settings import BackendConnectionSettings, MAX_SETTINGS_BYTES
from core.settings import AppSettings, _settings_writer
from core.organizer_connection import OrganizerConnection, OrganizerConnectionHold

KEY = 'manager_organizer_connection'

class OrganizerConnectionStore:
    def __init__(self, path):
        self.settings = BackendConnectionSettings(path)

    def observe(self):
        return self.settings.observe()

    def selected(self, snapshot):
        row = snapshot._payload.get(KEY)
        if row is None: return None
        fields = ('directory','version','inventory_sha256','manifest_sha256','binding_revision')
        if type(row) is not dict or row.get('schema_version') != 1 or any(type(row.get(k)) is not str or not row[k] for k in fields):
            raise OrganizerConnectionHold('saved_connection_requires_review')
        return OrganizerConnection(Path(row['directory']), *(row[k] for k in fields[1:]))

    def save(self, selected, *, expected):
        if type(selected) is not OrganizerConnection:
            raise OrganizerConnectionHold('reviewed_connection_required')
        with _settings_writer(self.settings.path):
            current = self.observe()
            if current.digest != expected.digest:
                raise OrganizerConnectionHold('settings_changed_review_again')
            saved = replace(selected, binding_revision=str(uuid4()))
            payload = deepcopy(current._payload)
            previous = payload.get(KEY) or {}
            if type(previous) is not dict: raise OrganizerConnectionHold('saved_connection_requires_review')
            payload[KEY] = {**previous, 'schema_version':1, 'directory':str(saved.directory),
                'version':saved.version, 'inventory_sha256':saved.inventory_sha256,
                'manifest_sha256':saved.manifest_sha256,'binding_revision':saved.binding_revision}
            if len(json.dumps(payload,ensure_ascii=False).encode('utf-8')) > MAX_SETTINGS_BYTES:
                raise OrganizerConnectionHold('settings_size_limit')
            AppSettings._from_payload(payload)._save_locked(self.settings.path,_patch_payload=payload)
        result = self.observe()
        if self.selected(result) != saved: raise OrganizerConnectionHold('saved_connection_readback_changed')
        return result
