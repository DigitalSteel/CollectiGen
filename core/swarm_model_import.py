"""Reviewed, selected-model SwarmUI metadata import into the model registry.

Only the pinned DescribeModel schema is admitted. Metadata is source evidence,
never a file identity, user declaration, compatibility result, or image fact.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from .model_resource_registry import ModelResourceError, ResourceInspection, inspect_model_resource, _cancel, _current
from .model_cache_reference import CacheReferenceReview
from .model_workspace import ModelWorkspace, _path, _pinned_model_file
from .swarm_model_paths import SwarmModelPaths, ModelPathInventory


PINNED_SWARM_REVISION = "ec823c3f750ce9b5103e623f3ad9918d02b8ddde"
IMPORT_SCHEMA_VERSION = 1
MAX_METADATA_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_EVIDENCE_ROWS = 32
_RESOURCE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SEGMENT = re.compile(r"[^/\\\x00-\x1f\x7f:;]+\Z")
_SUBTYPES = {
    "Stable-Diffusion": "sdmodelfolder", "LoRA": "sdlorafolder",
    "VAE": "sdvaefolder", "Embedding": "sdembeddingfolder",
    "ControlNet": "sdcontrolnetsfolder", "Clip": "sdclipfolder",
    "ClipVision": "sdclipvisionfolder",
}
_TEXT_FIELDS = ("title", "author", "description", "architecture", "class", "compat_class",
                "license", "date", "usage_hint", "trigger_phrase", "merged_from", "special_format")
_BOOLEAN_FIELDS = ("is_supported_model_format", "is_negative_embedding")
_INTEGER_FIELDS = ("standard_width", "standard_height", "time_created", "time_modified")


class SwarmModelImportError(ModelResourceError):
    """The selected source, schema, correspondence, or review is held."""


@dataclass(frozen=True)
class CacheSourceSelection:
    """Plain requested source context; carries no verification/publication authority."""
    entry_id: str
    manual_pin_id: str
    role: str
    family: str
    media_type: str = "Images"


@dataclass(frozen=True)
class SwarmModelChoice:
    subtype: str
    model_name: str
    role_field: str
    selected_path: Path
    inventory_fingerprint: str


@dataclass(frozen=True)
class SwarmModelCorrespondence:
    """Current exact registered-path correspondence, not a model-byte proof."""
    resource_id: str
    selected_path: Path
    record: dict
    registry_token: str
    inventory: ModelPathInventory
    source_binding: dict
    folder_fingerprint: str
    choice: SwarmModelChoice


@dataclass(frozen=True)
class SwarmModelImportPreview:
    resource_id: str
    selected_path: Path
    subtype: str
    model_name: str
    inspection: ResourceInspection
    registry_token: str
    inventory_fingerprint: str
    source_binding: dict
    source_digest: str
    before: tuple[dict, ...]
    proposed: dict
    action: str
    cache_selection: CacheSourceSelection | None = None
    cache_review: CacheReferenceReview | None = None


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise SwarmModelImportError("SwarmUI returned unsupported model metadata.") from exc


def _model_name(value: object) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= 1024 or value.startswith("/"):
        raise SwarmModelImportError("The SwarmUI model name is unsupported.")
    parts = value.split("/")
    if any(part in ("", ".", "..") or not _SEGMENT.fullmatch(part) or part != part.strip()
           for part in parts) or not value.casefold().endswith(".safetensors"):
        raise SwarmModelImportError("The SwarmUI model name is unsafe or is not a supported single file.")
    return value


def _single_directory(value: str, install_root: str) -> Path:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or ";" in value:
        raise SwarmModelImportError("Compound or missing SwarmUI model folders need separate review.")
    text = value.replace("\\", "/")
    parts = text.split("/")
    if any(part in (".", "..") for part in parts):
        raise SwarmModelImportError("SwarmUI model folders with traversal are unsupported.")
    base = Path(value)
    if not base.is_absolute():
        if not install_root or not Path(install_root).is_absolute():
            raise SwarmModelImportError("The enrolled SwarmUI installation has no absolute folder root.")
        base = Path(install_root) / base
    return Path(os.path.abspath(base))


def _source_folders(inventory: ModelPathInventory, install_root: str) -> dict[str, Path]:
    root = _single_directory(inventory.model_root, install_root)
    result = {}
    for subtype, field in _SUBTYPES.items():
        value = inventory.values[field]
        if not isinstance(value, str) or not value.strip() or value != value.strip() or ";" in value:
            continue
        folder = Path(value)
        if folder.is_absolute():
            result[subtype] = _single_directory(value, install_root)
        else:
            result[subtype] = _single_directory(str(root / folder), install_root)
    return result


def _source_projection_fingerprint(inventory: ModelPathInventory, install_root: str) -> str:
    """Stable role-path identity; excludes enrollment file/retrieval timestamps."""
    return _digest({"install_root": install_root, "model_root": inventory.model_root,
                    "role_folders": inventory.values})


def parse_describe_model(payload: object, *, subtype: str, model_name: str) -> dict:
    """Pure bounded v1 parser. Unknown fields stay inert inside raw metadata."""
    if subtype not in _SUBTYPES or _model_name(model_name) != model_name:
        raise SwarmModelImportError("This SwarmUI model type or name is unsupported.")
    if not isinstance(payload, dict) or set(payload) != {"model"} or not isinstance(payload["model"], dict):
        raise SwarmModelImportError("SwarmUI did not return one selected model description.")
    if len(_json_bytes(payload)) > MAX_RESPONSE_BYTES:
        raise SwarmModelImportError("The selected SwarmUI response exceeds the import limit.")
    # DescribeModel may inline preview artwork. This category imports metadata
    # only; do not retain, hash, display, or follow that optional artwork field.
    model = {key: value for key, value in payload["model"].items() if key != "preview_image"}
    if len(_json_bytes(model)) > MAX_METADATA_BYTES:
        raise SwarmModelImportError("SwarmUI model metadata exceeds the selected-import limit.")
    if model.get("local") is not True:
        raise SwarmModelImportError("Remote or unconfirmed SwarmUI models cannot be imported as local files.")
    if model.get("name") != model_name:
        raise SwarmModelImportError("SwarmUI returned a different model than the selected one.")
    for key in _TEXT_FIELDS:
        if key in model and model[key] is not None and (not isinstance(model[key], str) or len(model[key]) > 20_000):
            raise SwarmModelImportError(f"SwarmUI {key} has an unsupported value.")
    for key in _BOOLEAN_FIELDS:
        if key in model and model[key] is not None and type(model[key]) is not bool:
            raise SwarmModelImportError(f"SwarmUI {key} has an unsupported value.")
    for key in _INTEGER_FIELDS:
        if key in model and model[key] is not None and (type(model[key]) is not int or abs(model[key]) > 2**63 - 1):
            raise SwarmModelImportError(f"SwarmUI {key} has an unsupported value.")
    tags = model.get("tags")
    if tags is not None and (not isinstance(tags, list) or len(tags) > 256 or any(
        not isinstance(tag, str) or len(tag) > 512 for tag in tags)):
        raise SwarmModelImportError("SwarmUI model tags have an unsupported value.")
    return deepcopy(model)


def validate_swarm_claim(claim: object, *, resource_id: str) -> None:
    if not isinstance(resource_id, str) or not _RESOURCE_ID.fullmatch(resource_id):
        raise SwarmModelImportError("The registered model identity is invalid.")
    if not isinstance(claim, dict) or set(claim) != {"schema_version", "source", "resource_id", "subtype", "model_name", "metadata", "source_field_omissions", "source_digest", "retrieved_at", "authority", "compatibility", "hash_scope"} or type(claim.get("schema_version")) is not int or claim.get("schema_version") != IMPORT_SCHEMA_VERSION or claim.get("authority") != "attributed_swarm_metadata" or claim.get("compatibility") != "unverified" or claim.get("resource_id") != resource_id or claim.get("hash_scope") != "swarm_tensor_only_unverified":
        raise SwarmModelImportError("The SwarmUI evidence claim is incomplete or unsupported.")
    source = claim.get("source")
    if not isinstance(source, dict) or set(source) != {"endpoint", "server_id", "source_revision", "folder_fingerprint"} or source.get("source_revision") != PINNED_SWARM_REVISION or any(
        not isinstance(source.get(key), str) or not source[key] or len(source[key]) > 4096
        for key in ("endpoint", "server_id", "folder_fingerprint")):
        raise SwarmModelImportError("The SwarmUI evidence source is incomplete.")
    if not isinstance(claim.get("retrieved_at"), str) or not isinstance(claim.get("source_digest"), str):
        raise SwarmModelImportError("The SwarmUI evidence identity is incomplete.")
    try:
        moment = datetime.fromisoformat(claim["retrieved_at"])
    except ValueError as exc:
        raise SwarmModelImportError("The SwarmUI evidence retrieval time is invalid.") from exc
    if moment.tzinfo is None:
        raise SwarmModelImportError("The SwarmUI evidence retrieval time needs a timezone.")
    model = parse_describe_model({"model": claim.get("metadata")}, subtype=claim.get("subtype"), model_name=claim.get("model_name"))
    if model != claim["metadata"]:
        raise SwarmModelImportError("SwarmUI preview artwork cannot be retained as model metadata.")
    if claim.get("source_field_omissions") != ["preview_image"]:
        raise SwarmModelImportError("The SwarmUI metadata-only scope is incomplete.")
    stable = {"schema_version": IMPORT_SCHEMA_VERSION, "source": source,
              "resource_id": resource_id, "subtype": claim["subtype"],
              "model_name": claim["model_name"], "metadata": model,
              "source_field_omissions": ["preview_image"]}
    if claim["source_digest"] != _digest(stable) or len(_json_bytes(claim)) > MAX_METADATA_BYTES + 4096:
        raise SwarmModelImportError("The SwarmUI evidence digest or size is invalid.")


def project_swarm_model_evidence(record: object) -> tuple[dict, ...]:
    """Bounded post-reopen detail projection; malformed rows remain visible as held."""
    if not isinstance(record, dict):
        return ()
    resource_id = record.get("identity", {}).get("resource_id") if isinstance(record.get("identity"), dict) else None
    rows = record.get("swarm_model_evidence", [])
    if not isinstance(rows, list):
        return ({"status": "held", "summary": "Unreadable SwarmUI evidence; inspect Raw Details."},)
    projected = []
    for row in rows[-MAX_EVIDENCE_ROWS:]:
        try:
            validate_swarm_claim(row, resource_id=resource_id)
        except (SwarmModelImportError, TypeError, ValueError):
            projected.append({"status": "held", "summary": "Unreadable SwarmUI evidence; inspect Raw Details."})
            continue
        model = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        known = {key: deepcopy(model[key]) for key in (*_TEXT_FIELDS, *_BOOLEAN_FIELDS, *_INTEGER_FIELDS, "tags") if key in model}
        unknown = sorted(set(model) - set(known) - {"name", "local"})
        projected.append({"status": "attributed",
            "source": deepcopy(row.get("source", {})), "retrieved_at": row.get("retrieved_at", ""),
            "subtype": row.get("subtype", ""), "model_name": row.get("model_name", ""),
            "source_digest": row.get("source_digest", ""), "known": known,
            "hash_scope": "swarm_tensor_only_unverified",
            "unknown_fields": tuple(unknown), "raw": deepcopy(row)})
    if len(rows) > MAX_EVIDENCE_ROWS:
        projected.insert(0, {"status": "held", "summary": "Older SwarmUI evidence is retained in Raw Details."})
    return tuple(projected)


class SwarmModelImport:
    def __init__(self, workspace: ModelWorkspace, paths: SwarmModelPaths, *, cache_references=None):
        self.workspace = workspace
        self.paths = paths
        if cache_references is not None and (workspace.cache_references is not cache_references
                or cache_references.cache.registry is not workspace.registry):
            raise SwarmModelImportError("Use the same trusted cache reference owner for source and publication.")
        self.cache_references = cache_references

    def _selected(self, resource_id, selected_path, expected_registry, cache_selection=None):
        if not isinstance(resource_id, str) or not _RESOURCE_ID.fullmatch(resource_id):
            raise SwarmModelImportError("Choose a registered single-file model; managed packs are unsupported.")
        snapshot, token = self.workspace.registry.snapshot()
        if token != expected_registry or resource_id not in snapshot["resources"]:
            raise SwarmModelImportError("The selected registry record changed; reload before importing.")
        if not isinstance(selected_path, (str, Path)) or not Path(selected_path).is_absolute():
            raise SwarmModelImportError("Choose an explicit registered model path.")
        selected = Path(os.path.abspath(selected_path))
        record = snapshot["resources"][resource_id]
        rows = [row for row in record["locations"] if os.path.normcase(os.path.abspath(row["path"])) == os.path.normcase(str(selected))]
        if len(rows) != 1:
            raise SwarmModelImportError("Choose one recorded location for this registered model.")
        if cache_selection is None:
            if "cache_entry_id" in rows[0]:
                raise SwarmModelImportError("Choose the explicit prepared Fast source; a permanent-file fallback is not allowed.")
            selected = _path(selected, self.workspace.protected_roots)
        else:
            if not isinstance(cache_selection, CacheSourceSelection) or self.cache_references is None:
                raise SwarmModelImportError("Choose a prepared Fast source with its trusted cache owner.")
            from .model_storage_policy import load_policy, fast_destination
            from .model_use_leases import cache_state
            state = cache_state(snapshot)
            entry = state["entries"].get(cache_selection.entry_id, {})
            expected = fast_destination(load_policy(snapshot.get("workspace", {})), resource_id=resource_id,
                role=cache_selection.role, family=cache_selection.family, media_type=cache_selection.media_type)
            if (rows[0].get("cache_entry_id") != cache_selection.entry_id or entry.get("resource_id") != resource_id
                    or os.path.normcase(str(expected)) != os.path.normcase(str(selected))
                    or os.path.normcase(entry.get("path", "")) != os.path.normcase(str(selected))):
                raise SwarmModelImportError("Choose the exact current Fast entry, path and role context.")
            selected = self.cache_references.cache._guard(selected)
        return record, token, selected

    def _choices(self, resource_id, selected_path, *, expected_registry, inventory, cache_selection=None):
        _, _, selected = self._selected(resource_id, selected_path, expected_registry, cache_selection)
        record = self.paths._record()
        if inventory.binding.get("source_revision") != PINNED_SWARM_REVISION or record.get("source_revision") != PINNED_SWARM_REVISION:
            raise SwarmModelImportError("This SwarmUI source revision has no qualified metadata importer.")
        if cache_selection is not None:
            return self._cache_choices(selected, cache_selection, inventory, expected_registry)
        result = []
        for subtype, folder in _source_folders(inventory, str(record.get("install_root") or "")).items():
            folder = _path(folder, self.workspace.protected_roots)
            if folder in selected.parents:
                relative = selected.relative_to(folder)
                name = relative.as_posix()
                _model_name(name)
                result.append(SwarmModelChoice(subtype, name, _SUBTYPES[subtype], selected, inventory.fingerprint))
        if len(result) != 1:
            raise SwarmModelImportError("The selected file has no unambiguous SwarmUI role-folder correspondence.")
        return tuple(result)

    def _cache_choices(self, selected, selection, inventory, expected_registry):
        from .model_engine_locations import SWARM_ROLES
        from .model_storage_policy import load_policy, root_generation, model_storage_roots, fast_destination
        from .model_use_leases import cache_state, pinned_entries
        snapshot, token = self.workspace.registry.snapshot()
        if token != expected_registry:
            raise SwarmModelImportError("The Fast folder registry projection changed.")
        state = cache_state(snapshot)
        pinned_entries(state)
        model_storage_roots(snapshot)
        policy = load_policy(snapshot.get("workspace", {}))
        generation = root_generation(policy.fast_root)
        pin = state["leases"].get(selection.manual_pin_id, {})
        if pin.get("kind") != "manual" or pin.get("state") != "active" or selection.entry_id not in pin.get("entry_ids", []):
            raise SwarmModelImportError("The selected Fast source needs its active manual-use pin.")
        if inventory.model_root != policy.fast_root or set(inventory.values) != set(_SUBTYPES.values()):
            raise SwarmModelImportError("SwarmUI needs the qualified current-Fast root and role-folder projection.")
        manual, uncertain = set(), set()
        for lease in state["leases"].values():
            if lease.get("state") == "released":
                continue
            operation = state["operations"].get(lease.get("operation_id"), {})
            if (lease.get("kind") == "manual" and lease.get("state") == "active"
                    and operation.get("kind") == "prepare" and operation.get("phase") == "complete"
                    and operation.get("root_generation") == generation):
                manual.update(lease["entry_ids"])
            else:
                uncertain.update(lease["entry_ids"])
                uncertain.update(lease.get("reserved_entry_ids", []))
        field = SWARM_ROLES.get(selection.role)
        matches, configured, folder_fields = [], {}, {}
        folder_count = 0
        for subtype, candidate_field in _SUBTYPES.items():
            raw = inventory.values[candidate_field]
            if not isinstance(raw, str) or len(raw) > 4096 or any(ord(c) < 32 for c in raw):
                raise SwarmModelImportError("SwarmUI has a malformed Fast role-folder list.")
            folders = raw.split(";")
            seen = set()
            for part in folders:
                folder_count += 1
                if (not part or part != part.strip() or not Path(part).is_absolute()
                        or Path(policy.fast_root) not in Path(part).parents or folder_count > 64
                        or os.path.normcase(part) in seen):
                    raise SwarmModelImportError("SwarmUI has an unqualified or ambiguous Fast role folder.")
                seen.add(os.path.normcase(part))
                folder = Path(part)
                key = os.path.normcase(str(folder))
                if key in folder_fields and folder_fields[key] != candidate_field:
                    raise SwarmModelImportError("SwarmUI shares a Fast folder across incompatible roles.")
                folder_fields[key] = candidate_field
                configured.setdefault(candidate_field, []).append(folder)
                exposed = [(entry_id, entry) for entry_id, entry in state["entries"].items()
                    if entry.get("state") == "available"
                    and os.path.normcase(str(Path(entry.get("path", "")).parent)) == key]
                empty_landing = Path(policy.fast_root) / ".collectigen-engine-empty" / candidate_field
                if not exposed and folder != empty_landing:
                    raise SwarmModelImportError("The enrolled Fast folder has no exact prepared role context.")
                for entry_id, entry in exposed:
                    owner = state["operations"].get(entry.get("operation_id"), {})
                    contexts = entry.get("contexts", [])
                    if (entry.get("root_generation") != generation or owner.get("phase") != "complete"
                            or entry_id not in manual or entry_id in uncertain or not isinstance(contexts, list) or not contexts):
                        raise SwarmModelImportError("Every exposed Fast copy needs completed current ownership and a manual pin.")
                    for context in contexts:
                        if (not isinstance(context, dict) or set(context) != {"role", "family", "media_type"}
                                or SWARM_ROLES.get(context.get("role")) != candidate_field
                                or os.path.normcase(str(fast_destination(policy, resource_id=entry["resource_id"], **context)))
                                != os.path.normcase(entry["path"])):
                            raise SwarmModelImportError("An exposed Fast copy has an incompatible role-folder context.")
                if folder in selected.parents:
                    if folder != selected.parent or candidate_field != field:
                        raise SwarmModelImportError("The selected Fast source has a broad or mismatched role folder.")
                    matches.append(SwarmModelChoice(subtype, _model_name(selected.relative_to(folder).as_posix()),
                        candidate_field, selected, inventory.fingerprint))
        if len(matches) != 1:
            raise SwarmModelImportError("The selected Fast file needs one exact enrolled role-folder match.")
        # DescribeModel resolves a subtype-relative name, not an absolute path.
        # Check only that exact name at alternate admitted roots; no enumeration
        # or hashing, and never trust a SHA-looking basename as correspondence.
        for folder in configured.get(field, []):
            other = folder / matches[0].model_name
            if other == selected:
                continue
            other = self.cache_references.cache._guard(other)
            try:
                other.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise SwarmModelImportError("The SwarmUI relative model name is shadowed by another Fast role folder.")
        return tuple(matches)

    def _review_cache(self, resource_id, selected, selection, token, *, should_cancel=None, progress=None):
        return self.cache_references.review(resource_id, selected, entry_id=selection.entry_id,
            manual_pin_id=selection.manual_pin_id, role=selection.role, family=selection.family,
            media_type=selection.media_type, expected_registry=token, should_cancel=should_cancel, progress=progress)

    def candidates(self, resource_id, selected_path, *, expected_registry, should_cancel=None, cache_selection=None) -> tuple[SwarmModelChoice, ...]:
        """Qualified source choices without reading model bytes or listing models."""
        correspondence = self.correspondence(resource_id, selected_path,
            expected_registry=expected_registry, should_cancel=should_cancel, cache_selection=cache_selection)
        return (correspondence.choice,)

    def correspondence(self, resource_id, selected_path, *, expected_registry,
                       should_cancel=None, cache_selection=None) -> SwarmModelCorrespondence:
        """Read enrolled folders and return one exact permanent-file match.

        This does not discover models, hash bytes, or map Archive files to Fast.
        Each consumer still proves its selected bytes and response separately.
        """
        _cancel(should_cancel)
        record, token, selected = self._selected(resource_id, selected_path, expected_registry, cache_selection)
        inventory = self.paths.read()
        result = self._choices(resource_id, selected, expected_registry=token, inventory=inventory, cache_selection=cache_selection)
        source_record = self.paths._record()
        binding = self.paths._binding(source_record)
        if inventory.binding != binding:
            raise SwarmModelImportError("The SwarmUI enrollment changed during source selection.")
        if self.paths.read() != inventory or self.paths._binding(self.paths._record()) != binding:
            raise SwarmModelImportError("SwarmUI folder settings changed during source selection.")
        self._selected(resource_id, selected, token, cache_selection)
        _cancel(should_cancel)
        return SwarmModelCorrespondence(resource_id, selected, deepcopy(record), token, inventory,
            deepcopy(binding), _source_projection_fingerprint(inventory, str(source_record.get("install_root") or "")), result[0])

    def preview(self, resource_id, selected_path, *, subtype, model_name, expected_registry,
                should_cancel=None, progress=None, cache_selection=None) -> SwarmModelImportPreview:
        return self._preview(resource_id, selected_path, subtype=subtype, model_name=model_name,
            expected_registry=expected_registry, should_cancel=should_cancel, progress=progress,
            cache_selection=cache_selection)

    def _preview(self, resource_id, selected_path, *, subtype, model_name, expected_registry,
                 should_cancel=None, progress=None, cache_selection=None, guarded_inspection=None, cache_review=None):
        """Read just the chosen DescribeModel response and hash one selected file."""
        _cancel(should_cancel)
        record, token, selected = self._selected(resource_id, selected_path, expected_registry, cache_selection)
        inventory = self.paths.read()
        choices = self._choices(resource_id, selected, expected_registry=token, inventory=inventory, cache_selection=cache_selection)
        choice = choices[0]
        if (subtype, model_name) != (choice.subtype, choice.model_name):
            raise SwarmModelImportError("The selected SwarmUI source choice changed; review it again.")
        source_record = self.paths._record()
        binding = self.paths._binding(source_record)
        if binding != inventory.binding:
            raise SwarmModelImportError("The SwarmUI enrollment changed; review it again.")
        try:
            transport, session_id = self.paths.manager._server_session(source_record)
            payload = transport.post("DescribeModel", {"session_id": session_id,
                "subtype": subtype, "modelName": model_name}, max_bytes=MAX_RESPONSE_BYTES)
        except Exception as exc:
            raise SwarmModelImportError("The selected SwarmUI model description is unavailable.") from exc
        metadata = parse_describe_model(payload, subtype=subtype, model_name=model_name)
        if self.paths.read() != inventory or self.paths._binding(self.paths._record()) != binding:
            raise SwarmModelImportError("The SwarmUI source changed during metadata read.")
        if cache_selection is None:
            inspection = inspect_model_resource(selected, should_cancel=should_cancel, progress=progress)
        elif guarded_inspection is None:
            verified = self._review_cache(resource_id, selected, cache_selection, token, should_cancel=should_cancel, progress=progress)
            inspection, cache_review = verified.inspection, verified.review
        else:
            inspection = guarded_inspection
        if inspection.resource_id != resource_id or inspection.stored_identity() != record["identity"]:
            raise SwarmModelImportError("The selected bytes do not match the registered full-file SHA-256 identity.")
        if self.workspace.registry.snapshot()[1] != token:
            raise SwarmModelImportError("The model registry changed during import preview.")
        _current(inspection)
        if self.paths.read() != inventory or self.paths._binding(self.paths._record()) != binding:
            raise SwarmModelImportError("The SwarmUI source changed during model verification.")
        if cache_selection is not None and self._choices(resource_id, selected, expected_registry=token,
                inventory=inventory, cache_selection=cache_selection) != choices:
            raise SwarmModelImportError("The exact Fast name correspondence changed during source read.")
        source = {"endpoint": binding["endpoint"], "server_id": binding["server_id"],
                  "source_revision": binding["source_revision"],
                  "folder_fingerprint": _source_projection_fingerprint(
                      inventory, str(source_record["install_root"]))}
        stable = {"schema_version": IMPORT_SCHEMA_VERSION, "source": source,
                  "resource_id": resource_id, "subtype": subtype, "model_name": model_name,
                  "metadata": metadata, "source_field_omissions": ["preview_image"]}
        claim = {**stable, "source_digest": _digest(stable),
                 "retrieved_at": datetime.now(timezone.utc).isoformat(),
                 "authority": "attributed_swarm_metadata", "compatibility": "unverified",
                 "hash_scope": "swarm_tensor_only_unverified"}
        validate_swarm_claim(claim, resource_id=resource_id)
        before = record.get("swarm_model_evidence", [])
        if not isinstance(before, list) or len(before) > MAX_EVIDENCE_ROWS:
            raise SwarmModelImportError("Existing SwarmUI evidence needs review before adding more.")
        for prior in before:
            validate_swarm_claim(prior, resource_id=resource_id)
        action = "unchanged" if any(isinstance(row, dict) and row.get("source_digest") == claim["source_digest"] for row in before) else "append"
        if action == "append" and len(before) >= MAX_EVIDENCE_ROWS:
            raise SwarmModelImportError("This model has reached its retained SwarmUI evidence limit.")
        return SwarmModelImportPreview(resource_id, selected, subtype, model_name, inspection,
            token, inventory.fingerprint, binding, claim["source_digest"], tuple(deepcopy(before)), claim, action,
            cache_selection, cache_review)

    def apply(self, preview: SwarmModelImportPreview, *, confirmed=False,
              should_cancel=None, progress=None) -> str:
        if not confirmed:
            raise SwarmModelImportError("Confirm the selected SwarmUI metadata review before importing.")
        if not isinstance(preview, SwarmModelImportPreview):
            raise SwarmModelImportError("Review selected SwarmUI metadata before importing.")
        validate_swarm_claim(preview.proposed, resource_id=preview.resource_id)
        reviewed_source = preview.proposed["source"]
        if (preview.proposed["source_digest"] != preview.source_digest
            or preview.proposed["subtype"] != preview.subtype
            or preview.proposed["model_name"] != preview.model_name
            or any(reviewed_source[key] != preview.source_binding[key]
                   for key in ("endpoint", "server_id", "source_revision"))):
            raise SwarmModelImportError("The displayed SwarmUI import review changed; preview it again.")
        def refreshed(inspection=None):
            fresh = self._preview(preview.resource_id, preview.selected_path,
                subtype=preview.subtype, model_name=preview.model_name,
                expected_registry=preview.registry_token,
                should_cancel=should_cancel, progress=progress, cache_selection=preview.cache_selection,
                guarded_inspection=inspection, cache_review=preview.cache_review)
            if (fresh.source_digest != preview.source_digest or fresh.inspection != preview.inspection
                or fresh.inventory_fingerprint != preview.inventory_fingerprint
                or fresh.source_binding != preview.source_binding or fresh.before != preview.before
                or fresh.action != preview.action
                or {key: value for key, value in fresh.proposed.items() if key != "retrieved_at"}
                   != {key: value for key, value in preview.proposed.items() if key != "retrieved_at"}):
                raise SwarmModelImportError("SwarmUI metadata or selected model changed after review.")
            return fresh
        if preview.cache_selection is not None:
            if (not isinstance(preview.cache_review, CacheReferenceReview)
                    or preview.cache_review.resource_id != preview.resource_id
                    or preview.cache_review.selected_path != preview.selected_path
                    or preview.cache_review.entry_id != preview.cache_selection.entry_id
                    or preview.cache_review.manual_pin_id != preview.cache_selection.manual_pin_id
                    or (preview.cache_review.role, preview.cache_review.family, preview.cache_review.media_type)
                    != (preview.cache_selection.role, preview.cache_selection.family, preview.cache_selection.media_type)):
                raise SwarmModelImportError("The Fast source review does not match the selected cache context.")
            return self.workspace.attach_swarm_model_evidence_from_cache(preview.cache_review,
                build_claim=lambda inspection: refreshed(inspection).proposed, expected_registry=preview.registry_token,
                should_cancel=should_cancel, progress=progress)
        with _pinned_model_file(preview.selected_path, self.workspace.protected_roots):
            fresh = refreshed()
            return self.workspace.attach_swarm_model_evidence(preview.resource_id, fresh.proposed,
                fresh.inspection, expected_registry=preview.registry_token,
                should_cancel=should_cancel)
