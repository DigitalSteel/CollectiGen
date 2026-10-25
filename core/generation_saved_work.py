"""Bounded, explicit access to saved generation groups and reusable recipes."""
from __future__ import annotations

import os
import json
import hashlib
import re
import stat
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from .generation_experiments import MAX_BYTES as GROUP_MAX_BYTES, load_experiment, validate_experiment
from .generation_recipes import MAX_BYTES as RECIPE_MAX_BYTES, _validate as _validate_recipe, load_recipe, recipe_storage_digest, save_recipe
from .generation_planning import MAX_PLAN_BYTES, load_plan, load_plan_with_digest, save_plan
from .training_source_io import _assert_unredirected
from .image_record import _atomic_create_text
from .external_write_transaction import atomic_verified_bytes
from .generation_prompt_templates import PromptPreset, MAX_PRESET_BYTES
from .generation_transport import strict_json

_KINDS = {"groups": ("experiments", GROUP_MAX_BYTES), "recipes": ("recipes", RECIPE_MAX_BYTES),
          "drafts": ("drafts", MAX_PLAN_BYTES), "prompt_presets": ("prompt_presets", MAX_PRESET_BYTES)}
MAX_IMPORT_EVIDENCE_BYTES = 2 * 1024 * 1024 + 65536 + 4096


def _directory(storage_root, kind):
    if kind not in _KINDS:
        raise ValueError("Choose saved groups, recipes or drafts.")
    root = Path(storage_root).absolute()
    directory = root / _KINDS[kind][0]
    _assert_unredirected(directory)
    return directory


def _checked_managed_path(storage_root, kind, path):
    directory = _directory(storage_root, kind)
    selected = Path(path).absolute()
    if selected.parent != directory or selected.suffix.lower() != ".json":
        raise ValueError("Choose one saved item from this generation workspace.")
    _assert_unredirected(selected)
    return selected


def _recipe_import_evidence_path(storage_root, item_path):
    recipe = _checked_managed_path(storage_root, "recipes", item_path)
    if not re.fullmatch(r"[0-9a-f]{32}", recipe.stem):
        raise ValueError("Only an exact managed setup save can have local import details.")
    directory = Path(storage_root).absolute() / "recipe_import_evidence"
    target = directory / recipe.name
    _assert_unredirected(target)
    return target


def has_recipe_import_evidence(storage_root, item_path):
    """Presence hint only; use read_recipe_import_evidence for trusted details."""
    try:
        return _recipe_import_evidence_path(storage_root, item_path).exists()
    except (OSError, ValueError):
        return False


def read_recipe_import_evidence(storage_root, item_path):
    """Read one local selected-preset record only if bound to exact saved bytes."""
    from .generation_recipes import _validate
    from .swarm_setup_file_import import validate_swarm_setup_evidence

    recipe = _checked_managed_path(storage_root, "recipes", item_path)
    target = _recipe_import_evidence_path(storage_root, recipe)
    if not target.exists():
        return None
    raw_recipe = _preset_bytes(recipe, maximum=RECIPE_MAX_BYTES, label="Saved setup")
    saved_recipe = _validate(strict_json(raw_recipe))[0]
    raw = _preset_bytes(target, maximum=MAX_IMPORT_EVIDENCE_BYTES,
                        label="Setup import evidence")
    receipt = strict_json(raw)
    if (not isinstance(receipt, dict) or set(receipt) != {
            "schema_version", "recipe_sha256", "import"}
            or receipt["schema_version"] != 1
            or receipt["recipe_sha256"] != hashlib.sha256(raw_recipe).hexdigest()):
        raise ValueError("Local import details do not match this saved setup.")
    return {"schema_version": 1, "recipe_sha256": receipt["recipe_sha256"],
            "import": validate_swarm_setup_evidence(receipt["import"], recipe=saved_recipe)}


def _load(kind, path):
    if kind == "prompt_presets":
        return PromptPreset.from_dict(strict_json(_preset_bytes(path)))
    if kind == "drafts":
        return load_plan(path)
    if kind == "groups":
        return load_experiment(path)[0]
    return load_recipe(path)


def _publish_group(path, value):
    encoded = json.dumps(validate_experiment(value), ensure_ascii=False,
                         sort_keys=True, indent=2, allow_nan=False) + "\n"
    raw = encoded.encode("utf-8")
    if len(raw) > GROUP_MAX_BYTES:
        raise ValueError("Saved group exceeds its size limit.")
    _assert_unredirected(path)
    try:
        _atomic_create_text(path, encoded)
    except FileExistsError as exc:
        raise ValueError("The destination already exists; its contents were preserved.") from exc
    _assert_unredirected(path)
    with path.open("rb") as stream:
        if stream.read(len(raw) + 1) != raw:
            raise ValueError("Saved group read-back verification failed.")


def prompt_preset_browser_metadata(preset):
    """Bounded plain presentation of an already validated local preset.

    No prompt expansion, artwork lookup or extra file access. Source labels are
    recorded attribution, never compatibility or execution authority.
    """
    if not isinstance(preset, PromptPreset):
        raise ValueError("Present a validated prompt preset.")
    preview = " ".join(preset.positive_prompt[:512].split())
    if len(preview) > 240 or len(preset.positive_prompt) > 512:
        preview = preview[:240].rstrip() + "…"
    labels = {"steps": "Steps", "cfg_scale": "Guidance", "width": "Width", "height": "Height",
              "sampler": "Sampler", "scheduler": "Scheduler", "seed": "Seed", "denoising_strength": "Change strength"}
    settings = " · ".join(f"{labels[key]} {str(preset.settings[key])[:48]}" for key in labels if key in preset.settings)
    claim = preset.extra_fields.get("swarm_import", {})
    recorded_swarm = isinstance(claim, dict) and type(claim.get("version")) is int and claim.get("version") == 1 and isinstance(claim.get("source"), dict)
    author = claim["source"].get("author", "") if recorded_swarm else ""
    return {"version": preset.schema_version, "prompt_preview": preview,
            "settings_summary": settings[:384] or "No scalar settings",
            "source": "SwarmUI (recorded)" if recorded_swarm else "Local preset",
            "author": author[:128] if isinstance(author, str) else ""}


def _saved_draft_model_label(plan):
    """Present only the saved checkpoint choice; browsing never consults live files or registry."""
    request = plan.request
    if request.model:
        return request.model
    digest = str(request.model_hash or "").lower().removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        return "Model reference needs review" if request.model_hash else "Model not chosen"
    checkpoint = next((item for item in reversed(request.resources) if item.role == "checkpoint"
        and item.id == f"sha256:{digest}" and item.hashes.get("sha256", "").lower() == digest), None)
    if checkpoint is None:
        return "Model reference needs review"
    family = checkpoint.compatibility[0] if len(checkpoint.compatibility) == 1 else ""
    labels = plan.target_context.get("resource_display_labels", ())
    if isinstance(labels, (list, tuple)):
        for entry in labels:
            if (isinstance(entry, dict) and entry.get("resource_id") == checkpoint.id
                    and entry.get("role") == checkpoint.role and entry.get("family") == family):
                label = entry.get("label")
                if (isinstance(label, str) and 0 < len(label) <= 160
                        and not any(char in label for char in ("/", "\\", "\r", "\n", "\t"))
                        and all(ord(char) >= 32 for char in label)):
                    return label
    return "Checkpoint selected"


def list_recent_saved_work(storage_root, kind, *, limit=40, scan_limit=256, offset=0, query="", cancelled=None):
    """Read one bounded folder page; next_offset names the first unread entry."""
    if type(limit) is not int or not 1 <= limit <= 100 or type(scan_limit) is not int or not limit <= scan_limit <= 1024:
        raise ValueError("Saved-work browsing needs bounded limits.")
    if type(offset) is not int or offset < 0 or not isinstance(query, str) or len(query) > 512:
        raise ValueError("Saved-work browsing needs a valid page and search.")
    directory = _directory(storage_root, kind)
    if not directory.exists():
        return {"items": [], "skipped": 0, "truncated": False, "next_offset": None}
    items = []
    skipped = 0
    truncated = False
    search = query.casefold()
    with os.scandir(directory) as entries:
        for index, entry in enumerate(entries):
            if callable(cancelled) and cancelled():
                raise InterruptedError("Saved-work browsing was cancelled.")
            if index < offset:
                continue
            if index >= offset + scan_limit or len(items) >= limit:
                truncated = True
                break
            try:
                if kind == "prompt_presets" and (entry.name.endswith(".pending.json")
                        or entry.name in {".collection.lock", "recovery"}):
                    continue
                if kind == "drafts" and (entry.name == "generation-plan-recovery" or entry.name.endswith(".lock")):
                    continue
                if not entry.name.lower().endswith(".json") or not entry.is_file(follow_symlinks=False):
                    skipped += 1
                    continue
                state = entry.stat(follow_symlinks=False)
            except OSError:
                skipped += 1
                continue
            path = directory / entry.name
            item = {"path": str(path), "name": path.stem, "summary": "", "modified_ns": state.st_mtime_ns,
                    "available": False, "error": ""}
            if kind == "prompt_presets":
                item["exportable"] = False
            try:
                _assert_unredirected(path)
                if state.st_size > _KINDS[kind][1]:
                    raise ValueError("Saved item exceeds its size limit.")
                if kind == "drafts":
                    value, item["content_digest"] = load_plan_with_digest(path)
                else:
                    value = _load(kind, path)
                if kind == "drafts":
                    item["name"] = value.goal or value.request.positive_prompt[:80] or "Image draft"
                    item["summary"] = "Resume request · " + _saved_draft_model_label(value)
                elif kind == "groups":
                    item["name"] = value.get("name") or "Saved alternatives"
                    counts = {}
                    for row in value["candidates"]:
                        counts[row["state"]] = counts.get(row["state"], 0) + 1
                    item["summary"] = f"{len(value['candidates'])} candidates · " + ", ".join(
                        f"{count} {state.replace('_', ' ')}" for state, count in sorted(counts.items()))
                elif kind == "prompt_presets":
                    item["name"] = value.name
                    held = value.extra_fields["import_semantics"]["held_fields"] if value.schema_version == 2 else []
                    item["summary"] = f"Prompt preset · {len(held)} unsupported fields" if held else "Prompt preset · review before applying"
                    item["preset_id"] = value.preset_id
                    item["browser"] = prompt_preset_browser_metadata(value)
                    item["size_bytes"] = state.st_size
                else:
                    item["name"] = value["name"]
                    item["summary"] = f"{len(value.get('resources', []))} model components" + (
                        " · prompt templates included" if "prompts" in value or
                        value.get("prompt_parts") or "negative_prompt" in value else "")
                    item["has_import_evidence"] = has_recipe_import_evidence(storage_root, path)
                item["available"] = True
                if kind == "prompt_presets":
                    item["exportable"] = True
                if kind == "prompt_presets" and value.schema_version not in {1, 2}:
                    item["available"] = False
                    item["summary"] = "Future prompt preset version · retained but unavailable"
                    item["error"] = "unsupported_preset_version"
            except (ValueError, RuntimeError, OSError, KeyError, TypeError, UnicodeError):
                item["name"] = "Unavailable saved item · " + path.name
                item["summary"] = "Invalid or unsupported saved file"
                item["error"] = "invalid_saved_item"
            preset_details = " ".join(str(value) for value in item.get("browser", {}).values()) if kind == "prompt_presets" else ""
            if not search or search in (item["name"] + " " + item["summary"] + " " + path.name + " " + preset_details).casefold():
                items.append(item)
    items.sort(key=lambda row: (row["modified_ns"], Path(row["path"]).name), reverse=True)
    return {"items": items, "skipped": skipped, "truncated": truncated,
            "next_offset": index if truncated else None}


def new_draft_path(storage_root):
    """A separate recovery slot per open workspace; loaded files are never overwritten."""
    return _directory(storage_root, "drafts") / (uuid4().hex + ".json")


def import_selected_saved_work(storage_root, kind, source_path, *, cancelled=None):
    """Validate one user-selected file and publish a separate no-clobber copy."""
    directory = _directory(storage_root, kind)
    source = Path(source_path).absolute()
    _assert_unredirected(source)
    if callable(cancelled) and cancelled():
        raise InterruptedError("Saved-work import was cancelled.")
    value = _load(kind, source)
    if kind == "prompt_presets":
        collection = PromptPresetCollection(storage_root)
        review = collection.preview_save(value, action="copy")
        outcome = collection.apply(review, confirmed=True, cancelled=cancelled)
        return str(collection._path(outcome["item_id"]))
    if callable(cancelled) and cancelled():
        raise InterruptedError("Saved-work import was cancelled.")
    directory.mkdir(parents=True, exist_ok=True)
    _assert_unredirected(directory)
    target = directory / ((value["id"] if kind == "groups" else uuid4().hex) + ".json")
    if source == target:
        return str(target)
    if target.exists():
        raise ValueError("This saved item already exists; open it instead of importing a duplicate.")
    if kind == "groups":
        _publish_group(target, value)
    elif kind == "drafts":
        save_plan(target, value)
    else:
        save_recipe(target, value)
    return str(target)


def save_named_recipe(storage_root, recipe, *, operation_id=None, import_evidence=None) -> Path:
    """Save one reviewed recipe in the managed local collection, no replacement."""
    directory = _directory(storage_root, "recipes")
    directory.mkdir(parents=True, exist_ok=True)
    _assert_unredirected(directory)
    if operation_id is not None and (not isinstance(operation_id, str) or
                                     len(operation_id) != 32 or
                                     any(char not in "0123456789abcdef" for char in operation_id)):
        raise ValueError("Invalid setup save operation ID.")
    target = directory / ((operation_id or uuid4().hex) + ".json")
    if import_evidence is not None:
        from .swarm_setup_file_import import validate_swarm_setup_evidence
        if operation_id is None:
            raise ValueError("An imported setup needs a stable save operation ID.")
        evidence = validate_swarm_setup_evidence(import_evidence, recipe=recipe)
        receipt = {"schema_version": 1, "recipe_sha256": recipe_storage_digest(recipe),
                   "import": evidence}
        encoded = _preset_text(receipt)
        if len(encoded.encode("utf-8")) > MAX_IMPORT_EVIDENCE_BYTES:
            raise ValueError("Selected import details exceed their local save limit.")
    else:
        receipt = None
    if target.exists():
        # A completion signal can be lost after publication. Retrying the same
        # reviewed operation must identify that exact saved setup, not make a copy.
        if operation_id is None or load_recipe(target) != recipe:
            raise ValueError("This setup save target already contains different data.")
    if receipt is not None:
        evidence_path = _recipe_import_evidence_path(storage_root, target)
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        _assert_unredirected(evidence_path.parent)
        if evidence_path.exists():
            if strict_json(_preset_bytes(evidence_path, maximum=MAX_IMPORT_EVIDENCE_BYTES,
                                         label="Setup import evidence")) != receipt:
                raise ValueError("This setup save operation has different local import details.")
        else:
            _atomic_create_text(evidence_path, encoded)
            if _preset_bytes(evidence_path, maximum=MAX_IMPORT_EVIDENCE_BYTES,
                             label="Setup import evidence") != encoded.encode("utf-8"):
                raise OSError("Setup import details failed read-back verification.")
    if not target.exists():
        save_recipe(target, recipe)
    if receipt is not None and read_recipe_import_evidence(storage_root, target) != receipt:
        raise OSError("Saved setup import details failed exact read-back verification.")
    return target


def export_selected_saved_work(storage_root, kind, item_path, destination, *, cancelled=None):
    """Export one managed item to an explicitly chosen new path without replacing it."""
    source = _checked_managed_path(storage_root, kind, item_path)
    destination = Path(destination).absolute()
    _assert_unredirected(destination)
    if callable(cancelled) and cancelled():
        raise InterruptedError("Saved-work export was cancelled.")
    value = _load(kind, source)
    if source == destination or destination.exists():
        raise ValueError("Choose a new export filename; the existing file is preserved.")
    if callable(cancelled) and cancelled():
        raise InterruptedError("Saved-work export was cancelled.")
    if kind == "groups":
        _publish_group(destination, value)
    elif kind == "drafts":
        save_plan(destination, value)
    elif kind == "prompt_presets":
        _atomic_create_text(destination, _preset_text(value.to_dict()))
        if _preset_bytes(destination) != _preset_text(value.to_dict()).encode("utf-8"):
            raise OSError("Prompt preset export read-back failed.")
    else:
        save_recipe(destination, value)
    return str(destination)


def review_setup_export(storage_root, item_path):
    """Capture one selected managed setup for an explicit export review."""
    source = _checked_managed_path(storage_root, "recipes", item_path)
    raw = _preset_bytes(source, maximum=RECIPE_MAX_BYTES, label="Selected setup")
    recipe = _validate_recipe(strict_json(raw))[0]
    return {"recipe": recipe, "digest": recipe_storage_digest(recipe)}


def export_reviewed_setup(storage_root, item_path, destination, review, *,
                          format="collectigen", include_prompts=False, cancelled=None):
    """Publish reviewed bytes to a new file; later saved edits are never exported."""
    if format not in {"collectigen", "swarmui"}:
        raise ValueError("Choose CollectiGen JSON or qualified SwarmUI JSON.")
    if callable(cancelled) and cancelled():
        raise InterruptedError("Setup export cancelled; nothing was saved.")
    current = review_setup_export(storage_root, item_path)
    if current != review:
        raise ValueError("The saved setup changed. Review Export Selected again before exporting.")
    destination = Path(destination).absolute()
    _assert_unredirected(destination)
    if destination.exists():
        raise ValueError("Choose a new export filename; the existing file is preserved.")
    recipe = current["recipe"]
    payload = setup_export_payload(recipe, format=format, include_prompts=include_prompts)
    encoded = _preset_text(payload)
    if callable(cancelled) and cancelled():
        raise InterruptedError("Setup export cancelled; nothing was saved.")
    _atomic_create_text(destination, encoded)
    try:
        if _preset_bytes(destination, maximum=RECIPE_MAX_BYTES + 4096, label="Setup export") != encoded.encode("utf-8"):
            raise ValueError("Exported bytes differ from the review.")
    except (OSError, ValueError) as exc:
        raise OSError("Setup export failed exact read-back verification. The export file may exist; "
                      "inspect it before retrying with a new filename.") from exc
    return str(destination)


def setup_export_payload(recipe, *, format="collectigen", include_prompts=False):
    """Preview exact portable content; prompt sharing is explicitly opt-in."""
    from .swarm_setup_file_import import translate_text_setup_to_swarm
    if format == "swarmui":
        return translate_text_setup_to_swarm(recipe, include_prompts=include_prompts)
    if format != "collectigen":
        raise ValueError("Choose CollectiGen JSON or qualified SwarmUI JSON.")
    payload = deepcopy(recipe)
    if not include_prompts:
        for field in ("prompts", "prompt_parts", "negative_prompt"):
            payload.pop(field, None)
    if not (payload.get("settings") or payload.get("resources") or payload.get("stages")
            or payload.get("prompts") or payload.get("prompt_parts")
            or payload.get("negative_prompt") is not None or payload.get("finishing")):
        raise ValueError("No reusable choices remain. Include prompt text or cancel this export.")
    return payload


def _preset_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


def _preset_digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else _preset_text(value).encode("utf-8")).hexdigest()


def _preset_bytes(path, *, maximum=MAX_PRESET_BYTES + 1, label="Prompt preset"):
    _assert_unredirected(path)
    with Path(path).open("rb") as stream:
        before = os.fstat(stream.fileno())
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    current = Path(path).stat(follow_symlinks=False)
    identity = lambda state: (state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns)
    if (len(raw) > maximum or not stat.S_ISREG(before.st_mode)
            or identity(before) != identity(after) or identity(before) != identity(current)):
        raise ValueError(f"{label} changed or exceeds its bounded size.")
    _assert_unredirected(path)
    return raw


class PromptPresetCollection:
    """Canonical managed presets with exact review tokens and retained recovery."""

    def __init__(self, storage_root):
        self.storage_root = Path(storage_root).absolute()
        self.directory = _directory(storage_root, "prompt_presets")
        self.recovery = self.directory / "recovery"

    def _path(self, item_id):
        if not isinstance(item_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", item_id):
            raise ValueError("Choose one managed prompt preset identifier.")
        if item_id.lower().endswith(".pending") or re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", item_id, re.I):
            raise ValueError("This prompt preset identifier is reserved.")
        return _checked_managed_path(self.storage_root, "prompt_presets", self.directory / (item_id + ".json"))

    def _journal(self, operation_id, suffix=""):
        if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise ValueError("Invalid prompt preset recovery identifier.")
        path = self.recovery / (operation_id + suffix + ".json")
        _assert_unredirected(path)
        return path

    @contextmanager
    def _writer(self):
        _assert_unredirected(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        lock = self.directory / ".collection.lock"
        _assert_unredirected(lock)
        with lock.open("a+b") as handle:
            state = os.fstat(handle.fileno())
            current = lock.stat(follow_symlinks=False)
            _assert_unredirected(lock)
            if not stat.S_ISREG(state.st_mode) or (state.st_dev, state.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError("The prompt collection lock changed.")
            if state.st_size == 0:
                handle.write(b"0"); handle.flush(); os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                _assert_unredirected(self.directory)
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def load(self, item_id):
        path = self._path(item_id)
        raw = _preset_bytes(path)
        preset = PromptPreset.from_dict(strict_json(raw))
        if preset.preset_id != item_id:
            raise ValueError("Prompt preset identity differs from its managed filename.")
        return {"preset": preset, "storage_digest": _preset_digest(raw)}

    def preview_save(self, preset, *, action="create", item_id=None, expected_digest=None):
        if action not in {"create", "update", "copy", "keep_local"}:
            raise ValueError("Choose create, update, copy or keep local.")
        preset = PromptPreset.from_dict(preset.to_dict())
        identifier = uuid4().hex if action == "copy" else item_id or preset.preset_id
        path = self._path(identifier)
        current = self.load(identifier) if path.exists() else None
        if action in {"update", "keep_local"} and (current is None or expected_digest != current["storage_digest"]):
            raise ValueError("The preset changed or lacks its exact loaded token; reload before review.")
        if action in {"create", "copy"} and current is not None:
            raise ValueError("The prompt preset already exists; review an update or separate copy.")
        after = {**preset.to_dict(), "preset_id": identifier}
        if action == "update":
            after = {**current["preset"].extra_fields, **after}
            after["settings"] = {**{key: value for key, value in current["preset"].settings.items()
                                   if key not in preset.settings and key not in {"seed", "steps", "cfg_scale", "width", "height", "sampler", "scheduler", "denoising_strength"}},
                                 **preset.settings}
            PromptPreset.from_dict(after)
        if action == "copy":
            after["copied_from_preset_id"] = preset.preset_id
            PromptPreset.from_dict(after)
        if action == "keep_local":
            after = current["preset"].to_dict()
        review = {"schema_version": 1, "item_id": identifier, "action": action,
                  "before_digest": current["storage_digest"] if current else "",
                  "after": after}
        return {**review, "review_digest": _preset_digest(review)}

    @staticmethod
    def _check_review(review):
        if not isinstance(review, dict) or set(review) != {"schema_version", "item_id", "action", "before_digest", "after", "review_digest"}:
            raise ValueError("Prompt preset review is invalid.")
        checked = {key: deepcopy(value) for key, value in review.items() if key != "review_digest"}
        if type(checked["schema_version"]) is not int or checked["schema_version"] != 1 or review["review_digest"] != _preset_digest(checked):
            raise ValueError("Prompt preset review changed; preview again.")
        if checked["action"] not in {"create", "update", "copy", "keep_local"}:
            raise ValueError("Prompt preset review action is unsupported.")
        token = checked["before_digest"]
        if (checked["action"] in {"update", "keep_local"} and
                (not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token))
                or checked["action"] in {"create", "copy"} and token != ""):
            raise ValueError("Prompt preset review lacks its exact destination token.")
        if PromptPreset.from_dict(checked["after"]).preset_id != checked["item_id"]:
            raise ValueError("Prompt preset review identity changed.")
        return checked

    def apply(self, review, *, confirmed=False, cancelled=None):
        if confirmed is not True:
            raise ValueError("Confirm the exact prompt preset collection change first.")
        checked = self._check_review(review)
        with self._writer():
            path = self._path(checked["item_id"])
            pending = path.with_suffix(".pending.json")
            _assert_unredirected(pending)
            if pending.exists():
                raise ValueError("Recover the interrupted prompt preset operation before writing again.")
            before = _preset_bytes(path) if path.exists() else None
            digest = _preset_digest(before) if before is not None else ""
            if digest != checked["before_digest"]:
                raise ValueError("The prompt preset changed after review; reload and preview again.")
            after = _preset_text(checked["after"]).encode("utf-8")
            if callable(cancelled) and cancelled():
                raise InterruptedError("Prompt preset publication cancelled before writing.")
            if before == after or checked["action"] == "keep_local":
                return {"state": "unchanged", "item_id": checked["item_id"], "storage_digest": digest, "operation_id": ""}
            operation_id = uuid4().hex
            self.recovery.mkdir(parents=True, exist_ok=True)
            _assert_unredirected(self.recovery)
            journal = {"schema_version": 1, "operation_id": operation_id, "review": review,
                       "before": before.decode("utf-8") if before is not None else None,
                       "after_digest": _preset_digest(after)}
            _atomic_create_text(self._journal(operation_id), _preset_text(journal))
            _atomic_create_text(pending, _preset_text({"operation_id": operation_id}))
            if before is None:
                _atomic_create_text(path, after.decode("utf-8"))
            else:
                # Lock and exact token protect cooperative writers; recheck
                # immediately before replacing an existing managed document.
                if _preset_digest(_preset_bytes(path)) != digest:
                    raise ValueError("The prompt preset changed during publication; recovery is held.")
                atomic_verified_bytes(path, after, label="Prompt preset collection")
            if _preset_bytes(path) != after:
                raise OSError("Prompt preset publication read-back failed; recover the retained operation.")
            return self._finish(journal, pending, "saved")

    def _finish(self, journal, pending, state):
        outcome = {"state": state, "item_id": journal["review"]["item_id"],
                   "storage_digest": journal["after_digest"], "operation_id": journal["operation_id"]}
        completion = self._journal(journal["operation_id"], ".complete")
        if not completion.exists():
            _atomic_create_text(completion, _preset_text(outcome))
        elif strict_json(_preset_bytes(completion)) != outcome:
            raise ValueError("Prompt preset completion evidence conflicts.")
        _assert_unredirected(pending)
        if pending.exists():
            if strict_json(_preset_bytes(pending)) != {"operation_id": journal["operation_id"]}:
                raise ValueError("Prompt preset recovery marker changed.")
            pending.unlink()
        return outcome

    def recover(self, operation_id, *, confirmed=False):
        if confirmed is not True:
            raise ValueError("Confirm review of this prompt preset recovery first.")
        with self._writer():
            journal = strict_json(_preset_bytes(self._journal(operation_id), maximum=4 * MAX_PRESET_BYTES))
            if (not isinstance(journal, dict) or set(journal) != {"schema_version", "operation_id", "review", "before", "after_digest"}
                    or journal["schema_version"] != 1 or journal["operation_id"] != operation_id):
                raise ValueError("Prompt preset recovery evidence is invalid.")
            checked = self._check_review(journal["review"])
            after = _preset_text(checked["after"]).encode("utf-8")
            before = journal["before"].encode("utf-8") if isinstance(journal["before"], str) else None
            if journal["after_digest"] != _preset_digest(after) or checked["before_digest"] != (_preset_digest(before) if before is not None else ""):
                raise ValueError("Prompt preset recovery content changed.")
            path = self._path(checked["item_id"])
            current = _preset_bytes(path) if path.exists() else None
            pending = path.with_suffix(".pending.json")
            if current == after:
                return self._finish(journal, pending, "saved")
            if current == before:
                # Nothing is replayed automatically; release only this exact
                # intent so the user can make a fresh source/destination review.
                _assert_unredirected(pending)
                if pending.exists():
                    if strict_json(_preset_bytes(pending)) != {"operation_id": operation_id}:
                        raise ValueError("Prompt preset recovery marker changed.")
                    pending.unlink()
                return {"state": "not_written", "item_id": checked["item_id"], "storage_digest": checked["before_digest"], "operation_id": operation_id}
            raise ValueError("The prompt preset has later edits; recovery will not overwrite them.")

    def pending_operations(self, item_id=None):
        """Bounded recovery projection; never expose prompt-bearing journals."""
        if item_id is not None:
            paths = [self._path(item_id).with_suffix(".pending.json")]
        else:
            if not self.directory.exists(): return []
            _assert_unredirected(self.directory)
            paths = []
            with os.scandir(self.directory) as entries:
                for index, entry in enumerate(entries):
                    if index >= 1024 or len(paths) >= 40:
                        raise ValueError("Choose one preset to review recovery; the collection-wide recovery scan is bounded.")
                    if entry.name.endswith(".pending.json"):
                        paths.append(self.directory / entry.name)
        rows = []
        for path in paths:
            if not path.exists(): continue
            marker = strict_json(_preset_bytes(path))
            if not isinstance(marker, dict) or set(marker) != {"operation_id"}:
                raise ValueError("Preset recovery marker is malformed; its contents were preserved.")
            journal = strict_json(_preset_bytes(self._journal(marker["operation_id"]), maximum=4 * MAX_PRESET_BYTES))
            if (not isinstance(journal, dict) or journal.get("operation_id") != marker["operation_id"]
                    or type(journal.get("schema_version")) is not int or journal["schema_version"] != 1):
                raise ValueError("Preset recovery evidence is malformed; its contents were preserved.")
            checked = self._check_review(journal.get("review"))
            if path != self._path(checked["item_id"]).with_suffix(".pending.json"):
                raise ValueError("Preset recovery marker names a different destination.")
            rows.append({"operation_id": marker["operation_id"], "item_id": checked["item_id"],
                         "state": "recovery_required", "summary": "Review interrupted preset save before writing again"})
        return rows
