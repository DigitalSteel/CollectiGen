"""Exact registered inputs for the pinned Wan video runtime.

Issuance is an explicit, cancellable file verification operation, never a
display projection. It neither writes the registry nor starts a runtime.
Cache publication retains its independent whole-registry freshness check.
"""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from . import comfy_video_adapter as adapter
from . import comfy_video_runtime as runtime
from .model_cache import CacheResource, ModelRequirementSet
from .model_organization import normalize_model_family
from .model_resource_registry import ModelResourceError, _cancel, inspect_model_resource
from .model_use_leases import digest
from .model_workspace import _path, _pinned_model_file

ADAPTER_ID = "collectigen.wan22-ti2v5b.comfy"
# Portable identities retained by tools/qualify_video_offline.py. Machine paths
# and the qualification script itself are deliberately not imported here.
QUALIFIED_MODELS = (
    ("transformer", "456f901338bd9eadbded3828b819109a9b68e8a525ca5cf8d0049a69fcfeca1e"),
    ("text_encoder", "c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68"),
    ("vae", "e40321bd36b9709991dae2530eb4ac303dd168276980d3e9bc4b6e2b75fed156"),
)
_ROLES = {"transformer": ("unet", "checkpoint"), "text_encoder": ("text_encoder",), "vae": ("vae",)}
_MAX_REVIEWS = 32


@dataclass(frozen=True)
class WanResourceReview:
    review_id: str
    source_preview: runtime.RuntimePreview
    requirements: ModelRequirementSet
    registry_token: str
    recipe_digest: str


def _recipe():
    return digest({"adapter_id": ADAPTER_ID, "adapter_version": adapter.ADAPTER_VERSION,
        "template_sha256": adapter.TEMPLATE_SHA256, "models": QUALIFIED_MODELS,
        "loader_contract": runtime.LOADER_CONTRACT, "loader_revision": runtime.LOADER_REVISION,
        "source_hashes": runtime.LOADER_SOURCE_HASHES, "family": "Wan2.2", "media_type": "Video"})


def _key(path):
    return str(Path(path)).casefold()


class WanRuntimeResourceIssuer:
    """Mint session-bound requirements from verified files and the fixed recipe.

Use issue() with the user's exact RuntimeConfig, then pass resolver() and the
returned requirements to ComfyVideoLifecycle. A saved flag, family declaration,
or caller-built ModelRequirementSet cannot mint an issuer capability.
"""
    def __init__(self, registry, *, protected_roots=()):
        self.registry = registry
        self.protected_roots = tuple(protected_roots)
        self._issued = {}

    def _selected(self, config, snapshot):
        if not isinstance(config, runtime.RuntimeConfig):
            raise ModelResourceError("Choose the exact Wan runtime and its three registered files.")
        resources, evidence = [], []
        for slot, sha256 in QUALIFIED_MODELS:
            model = getattr(config, slot)
            if model.role != slot or model.sha256 != sha256:
                raise ModelResourceError("This protected runtime requires the exact qualified Wan model, text encoder and VAE.")
            raw_path = Path(model.path)
            if not raw_path.is_absolute() or ".." in raw_path.parts:
                raise ModelResourceError("Choose an explicit absolute registered model location.")
            resource_id = "sha256:" + sha256
            record = snapshot["resources"].get(resource_id)
            locations = [] if record is None else [loc for loc in record["locations"]
                if _key(loc["path"]) == _key(raw_path)]
            if len(locations) != 1 or locations[0].get("cache_entry_id"):
                raise ModelResourceError("Choose one exact registered Archive original, not a Fast cache copy.")
            # Fixed role order is independent of registration/location order.
            # Multiple spellings of the selected role's family need correction,
            # rather than silently picking an arbitrary stored declaration.
            selected_role, families = "", set()
            for role in _ROLES[slot]:
                families = {d["family"] for d in record["declarations"]
                    if d["role"] == role and normalize_model_family(d["family"]) == "Wan2.2"}
                if families:
                    selected_role = role
                    break
            if len(families) != 1:
                raise ModelResourceError("Review this file's exact Wan2.2 role and family declaration.")
            family = next(iter(families))
            resources.append(CacheResource(resource_id, raw_path, selected_role, family, "Video"))
            evidence.append({"identity": deepcopy(record["identity"]), "location": deepcopy(locations[0]),
                             "role": selected_role, "family": family})
        if len({r.resource_id for r in resources}) != 3 or len({_key(r.archive_path) for r in resources}) != 3:
            raise ModelResourceError("The Wan recipe needs three distinct registered files.")
        return tuple(resources), tuple(evidence)

    def _verify(self, config, *, expected_registry=None, should_cancel=None, progress=None,
                allow_existing_workspace=False):
        _cancel(should_cancel)
        snapshot, token = self.registry.snapshot()
        if expected_registry is not None and expected_registry != token:
            raise ModelResourceError("The model registry changed; review the selected Wan files again.")
        resources, evidence = self._selected(config, snapshot)
        # Check protected paths before runtime.preview_runtime can probe them.
        for name in ("source_root", "python_executable", "python_runtime_root", "workspace_root"):
            _path(getattr(config, name), self.protected_roots)
        inspections = {}
        with ExitStack() as stack:
            for resource in resources:
                path = _path(resource.archive_path, self.protected_roots)
                stack.enter_context(_pinned_model_file(path, self.protected_roots))

            def inspect(path):
                value = inspect_model_resource(path, should_cancel=should_cancel, progress=progress)
                inspections[_key(path)] = value
                return value

            preview = runtime.preview_runtime(replace(config, protected_loader=True),
                protected_roots=self.protected_roots, inspect=inspect,
                allow_existing_workspace=allow_existing_workspace)
            runtime.protected_binding(preview)
            for resource, facts in zip(resources, evidence):
                inspection = inspections[_key(resource.archive_path)]
                if (inspection.stored_identity() != facts["identity"]
                        or list(inspection.file_state) != facts["location"].get("file_state")):
                    raise ModelResourceError("A selected registered file changed; verify its location again.")
            _cancel(should_cancel)
            if self.registry.snapshot()[1] != token:
                raise ModelResourceError("The model registry changed during Wan verification; review again.")
        return preview, resources, evidence, token

    def issue(self, config, *, expected_registry, should_cancel=None, progress=None):
        if not isinstance(expected_registry, str) or not expected_registry:
            raise ModelResourceError("Review the current model registry before verifying Wan inputs.")
        recipe = _recipe()
        preview, resources, evidence, token = self._verify(config, expected_registry=expected_registry,
            should_cancel=should_cancel, progress=progress)
        if _recipe() != recipe:
            raise ModelResourceError("The supported Wan recipe changed during verification.")
        review_id = uuid4().hex
        key = digest({"review_id": review_id, "recipe": recipe,
                      "source_fingerprint": preview.fingerprint, "selected": evidence})
        requirements = ModelRequirementSet(resources, "adapter_qualified", ADAPTER_ID, adapter.ADAPTER_VERSION, key)
        review = WanResourceReview(review_id, preview, requirements, token, recipe)
        if len(self._issued) >= _MAX_REVIEWS:
            self._issued.pop(next(iter(self._issued)))
        # RuntimePreview contains dictionaries: retain a private copy so edits
        # to a displayed report cannot change the resolver's original evidence.
        self._issued[key] = (deepcopy(preview), requirements, deepcopy(evidence), recipe)
        return review

    def resolver(self, *, should_cancel=None, progress=None):
        def resolve(adapter_id, adapter_version, request_fingerprint):
            held = self._issued.get(request_fingerprint)
            if held is None:
                raise ModelResourceError("Verify the exact Wan resource request in this session first.")
            preview, required, evidence, recipe = held
            if ((adapter_id, adapter_version) != (required.adapter_id, required.adapter_version)
                    or _recipe() != recipe):
                raise ModelResourceError("The reviewed Wan adapter or recipe changed.")
            # Added Fast locations or unrelated presentation changes do not
            # replace the explicitly reviewed Archive source. Each invocation
            # still requires a stable current registry snapshot during reads;
            # the cache/lifecycle owner retains its separate strict write token.
            fresh, resources, facts, _ = self._verify(preview.config,
                should_cancel=should_cancel, progress=progress, allow_existing_workspace=True)
            if (fresh.fingerprint != preview.fingerprint or resources != required.resources
                    or facts != evidence or _recipe() != recipe):
                raise ModelResourceError("The reviewed Wan source, selected files or declarations changed.")
            return required.resources
        return resolve
