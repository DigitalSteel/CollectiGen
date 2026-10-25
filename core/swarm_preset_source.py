"""Consent-only preset projection through the canonical enrolled Swarm session."""
from copy import deepcopy
import json

from .model_resource_registry import ModelResourceError, _cancel
from .swarm_model_import import PINNED_SWARM_REVISION
from .swarm_onboarding import MAX_RESPONSE_BYTES, _bounded_payload, _short
from .swarm_onboarding_receipts import digest


class SwarmPresetSource:
    """No reads on construction; returned rows remain untrusted import evidence."""

    def __init__(self, paths):
        self.paths = paths

    def __call__(self, *, consent=False, cancelled=None):
        if consent is not True:
            raise ModelResourceError("Allow the disclosed combined SwarmUI account and preset read first.")
        _cancel(cancelled)
        record = self.paths._record()
        binding = self.paths._binding(record)
        if record.get("source_revision") != PINNED_SWARM_REVISION or binding.get("source_revision") != PINNED_SWARM_REVISION:
            raise ModelResourceError("This SwarmUI revision has no qualified preset importer.")
        try:
            transport, session = self.paths.manager._server_session(record)
            payload = transport.post("GetMyUserData", {"session_id": session}, max_bytes=MAX_RESPONSE_BYTES)
        except Exception as exc:
            raise ModelResourceError("The enrolled SwarmUI presets are unavailable; no collection items were imported.") from exc
        _bounded_payload(payload)
        principal = payload.get("user_name")
        if not _short(principal, limit=256):
            raise ModelResourceError("SwarmUI did not provide a qualified source account identity.")
        rows = payload.get("presets")
        if not isinstance(rows, list) or len(rows) > 256 or any(not isinstance(row, dict) for row in rows):
            raise ModelResourceError("SwarmUI presets are malformed or exceed the supported category bound.")
        # Collection/translation owns individual row validation and exclusions.
        # This projection never retains unrelated categories or raw account data.
        result = {"schema_version": 1, "source_revision": PINNED_SWARM_REVISION,
            "server_fingerprint": digest({key: binding.get(key, "") for key in
                ("endpoint", "server_id", "enrollment_fingerprint", "source_revision")}),
            "account_fingerprint": digest({"server_id": binding["server_id"],
                "endpoint": binding["endpoint"], "account": principal}),
            "presets": deepcopy(rows)}
        del rows, payload, principal
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 1024 * 1024:
            raise ModelResourceError("The preset category exceeds the supported size limit.")
        _cancel(cancelled)
        if self.paths._binding(self.paths._record()) != binding:
            raise ModelResourceError("SwarmUI enrollment changed during the preset read; review it again.")
        return result


def make_swarm_preset_import_service(storage_root, *, paths=None, manager=None):
    """Build a passive importer; actual source access requires its explicit read."""
    from .generation_saved_work import PromptPresetCollection
    from .swarm_preset_import import SwarmPresetImportService
    if paths is None:
        from .swarm_model_paths import SwarmModelPaths
        from .swarm_runtime import SwarmRuntimeManager
        paths = SwarmModelPaths(manager if manager is not None else SwarmRuntimeManager())
    return SwarmPresetImportService(PromptPresetCollection(storage_root), SwarmPresetSource(paths))
