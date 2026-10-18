"""Pure proposals for sharing configured model locations with an engine.

No directory discovery, resource inspection, connection or configuration write.
The canonical Swarm model-path transaction still owns external application.
"""
from dataclasses import dataclass
import ntpath
from pathlib import Path

from .model_organization import destination_path
from .swarm_model_paths import ROLE_FIELDS, ModelPathError

SWARM_PATH_REVISION = "ec823c3f750ce9b5103e623f3ad9918d02b8ddde"
IMPLICIT_MODEL_FOLDERS = (
    "tensorrt", "diffusion_models", "unet", "VAE", "Lora", "LyCORIS", "ESRGAN", "RealESRGAN",
    "SwinIR", "upscale-models", "upscale_models", "embeddings", "hypernetworks", "ControlNet",
    "model_patches", "clip", "clip_vision", "gligen", "ipadapter", "yolov8", "clipseg",
    "style_models", "latent_upscale_models",
)


SWARM_ROLES = {
    "checkpoint": "sdmodelfolder", "unet": "sdmodelfolder",
    "lora": "sdlorafolder", "vae": "sdvaefolder",
    "embedding": "sdembeddingfolder", "control_model": "sdcontrolnetsfolder",
    "text_encoder": "sdclipfolder", "vision_encoder": "sdclipvisionfolder",
}


@dataclass(frozen=True)
class EngineFolderProposal:
    values: dict[str, str]
    additions: tuple[tuple[str, str, str], ...]
    held: tuple[str, ...]


def _path_key(value):
    return ntpath.normcase(ntpath.normpath(value))


def propose_swarm_folders(workspace, current_values):
    """Append only explicit type-specific destinations, preserving old entries.

    A generic legacy default can contain mixed resource types, so it is never
    proposed under every Swarm role. Unknown/unclassified families are held.
    """
    if not isinstance(workspace, dict) or not isinstance(current_values, dict) or set(current_values) != set(ROLE_FIELDS):
        raise ModelPathError("Read complete SwarmUI folders and saved CollectiGen folders first.")
    from .model_storage_policy import load_policy
    if load_policy(workspace).mode == "tiered":
        raise ModelPathError("Tiered storage requires Prepare for Use and a reviewed current-Fast root transition; permanent folders cannot be appended.")
    if any(not isinstance(v,str) or len(v)>4096 or any(ord(c)<32 for c in v) for v in current_values.values()):
        raise ModelPathError("Current SwarmUI folder values are unsupported.")
    families=workspace.get("family_destinations",{})
    overrides=workspace.get("destination_overrides",{})
    if not isinstance(families,dict) or not isinstance(overrides,dict) or len(families)>100 or len(overrides)>100:
        raise ModelPathError("Saved model destination settings are unsupported.")
    if any(not isinstance(k,str) or not isinstance(v,str) for table in (families,overrides) for k,v in table.items()):
        raise ModelPathError("Saved model folder keys and paths must be text.")
    values=dict(current_values); additions=[]; held=[]; requests=[]
    for key in sorted(families):
        if not isinstance(key,str):raise ModelPathError("Model folder keys must be text.")
        if key=="Shared":
            media,family="Images","Shared"
        elif ":" in key and key.split(":",1)[0] in {"Images","Video"}:
            media,family=key.split(":",1)
            if not family.strip():raise ModelPathError("A model family folder needs its family name.")
        else:
            # Analysis/upscalers lack one of the qualified Swarm role settings;
            # Unclassified is not enough evidence to route arbitrary resources.
            if key=="Unclassified" and families[key]:
                held.append("Unclassified model folders need reviewed type/family assignments before sharing.")
            continue
        requests.extend((role,family,media) for role in SWARM_ROLES)
    for key in sorted(overrides):
        if not isinstance(key,str):raise ModelPathError("Model folder keys must be text.")
        role,_,family=key.partition(":")
        if role in SWARM_ROLES:requests.append((role,family,"Images"))
    if not requests and workspace.get("default_destination"):
        held.append("Configure family/type folders first; the general model folder may contain mixed model types.")
    for role,family,media in dict.fromkeys(requests):
        folder,reason=destination_path(workspace,role=role,family=family,media_type=media)
        if "conflicting" in reason:
            held.append(f"{role} / {family}: {reason}.")
            continue
        if not folder or reason in {"legacy default destination","configured unclassified destination"}:
            continue
        if not isinstance(folder,str) or not Path(folder).is_absolute() or ";" in folder or any(ord(c)<32 for c in folder):
            held.append(f"{role} / {family or 'all families'}: choose one absolute folder without semicolons.")
            continue
        field=SWARM_ROLES[role]
        known={_path_key(v.strip()) for v in values[field].split(";") if v.strip()}
        if _path_key(folder) in known:continue
        proposed=values[field]+(";" if values[field] and not values[field].endswith(";") else "")+folder
        if len(proposed)>4096:
            held.append(f"{role}: proposed folder list exceeds SwarmUI's supported bound.")
            continue
        values[field]=proposed
        additions.append((field,folder,reason))
    return EngineFolderProposal(values,tuple(additions),tuple(dict.fromkeys(held)))


@dataclass(frozen=True)
class HotFolderProjection:
    registry_token: str
    policy_digest: str
    inventory_fingerprint: str
    inventory_binding: tuple[tuple[str, str], ...]
    operation_ids: tuple[str, ...]
    entry_ids: tuple[str, ...]
    before_model_root: str
    after_model_root: str
    before: tuple[tuple[str, str], ...]
    after: tuple[tuple[str, str], ...]
    directory_entries: tuple[tuple[str, tuple[str, ...]], ...]
    empty_directories: tuple[str, ...]
    implicit_directories: tuple[str, ...]
    additions: tuple[tuple[str, str, str], ...]
    removals: tuple[tuple[str, str, str], ...]
    qualification: str
    held: tuple[str, ...]
    review_text: str

    @property
    def values(self):
        return dict(self.after)


def project_swarm_hot_folders(snapshot, inventory, prepared_operation_ids, *, registry_token):
    """Pure exact prepared-cache projection, never a file-readiness assertion."""
    from itertools import islice
    from .model_storage_policy import load_policy, model_storage_roots, root_generation, fast_destination
    from .model_use_leases import cache_state, digest
    from .swarm_model_paths import ModelPathInventory
    if not isinstance(inventory, ModelPathInventory) or set(inventory.values) != set(ROLE_FIELDS):
        raise ModelPathError("Read the complete enrolled Swarm root and role folders first.")
    if not isinstance(registry_token, str) or not registry_token:
        raise ModelPathError("Project the exact registry revision with the prepared set.")
    if any(not isinstance(v, str) or len(v) > 4096 or any(ord(c) < 32 for c in v) for v in (*inventory.values.values(), inventory.model_root)):
        raise ModelPathError("Swarm root or role values are malformed.")
    ids = tuple(islice(iter(prepared_operation_ids), 65))
    if not ids or len(ids) > 64 or any(not isinstance(v, str) for v in ids) or len(set(ids)) != len(ids):
        raise ModelPathError("Choose distinct exact completed preparations, up to 64.")
    model_storage_roots(snapshot)
    policy = load_policy(snapshot.get("workspace", {}))
    if policy.mode != "tiered":
        raise ModelPathError("Current-Fast managed paths require tiered storage.")
    state = cache_state(snapshot)
    current_generation = root_generation(policy.fast_root)
    held, selected, qualifications = [], {}, []
    if inventory.binding.get("source_revision") != SWARM_PATH_REVISION:
        held.append("This enrolled Swarm source revision has no qualified implicit-path contract. Review its version before changing managed paths.")
    manual = set()
    uncertain = set()
    for lease in state["leases"].values():
        if lease.get("state") == "active":
            # Another runtime can release its own lease while Swarm still
            # exposes these folders. Only explicit external-use pins cover
            # this manual handoff; do not adopt another consumer's lifetime.
            if lease.get("kind") == "manual":
                manual.update(lease.get("entry_ids", []))
        elif lease.get("state") != "released":
            uncertain.update(lease.get("entry_ids", []))
    for operation_id in ids:
        operation = state["operations"].get(operation_id)
        if not operation or operation.get("kind") != "prepare" or operation.get("phase") != "complete" or operation.get("root_generation") != current_generation:
            held.append("A selected preparation is incomplete, missing or belongs to a retired Fast folder.")
            continue
        qualification = operation.get("qualification", "unknown")
        qualifications.append(qualification)
        for entry_id in operation.get("entry_ids", []):
            entry = state["entries"].get(entry_id)
            context = operation.get("binding_contexts", {}).get(entry_id)
            if (not entry or entry.get("state") != "available" or entry.get("root_generation") != current_generation
                    or not isinstance(context, dict) or set(context) != {"role", "family", "media_type"}):
                held.append("A prepared file or its exact role context is missing; prepare the set again.")
                continue
            role = context.get("role")
            if role not in SWARM_ROLES:
                held.append(f"Prepared role {role!r} has no qualified Swarm folder setting; required companions cannot be silently omitted.")
                continue
            expected = fast_destination(policy, resource_id=entry["resource_id"], **context)
            if _path_key(str(expected)) != _path_key(entry["path"]):
                held.append("A prepared path differs from the current family/type destination. Prepare it again.")
                continue
            if entry_id not in manual or entry_id in uncertain:
                held.append("Every exposed model needs an active manual external-use pin. Use Models > Prepare for Use for these models; reconcile uncertain use first.")
            field = SWARM_ROLES[role]
            folder = str(expected.parent)
            selected.setdefault((_path_key(folder), field), (folder, set()))[1].add(entry_id)
    if not selected:
        held.append("Prepare and pin at least one supported model set in the current Fast folder.")
    # A directory exposes other cached files alongside the selected set. Include
    # all of them in namespace qualification and pin admission, not just the
    # requested filenames. No directory is read in this pure projection.
    for (_, field), (folder, entries) in selected.items():
        for entry_id, entry in state["entries"].items():
            if entry.get("state") != "available" or _path_key(str(Path(entry.get("path", "")).parent)) != _path_key(folder):
                continue
            if entry.get("root_generation") != current_generation:
                held.append("A role directory includes an older cache generation.")
                continue
            contexts = entry.get("contexts", [])
            if not contexts or any(not isinstance(ctx, dict) or SWARM_ROLES.get(ctx.get("role")) != field for ctx in contexts):
                held.append("A shared role directory contains an incompatible or unclassified model context.")
            if entry_id not in manual or entry_id in uncertain:
                held.append("A model sharing an exposed role folder needs an active manual external-use pin. Use Models > Prepare for Use for that model; reconcile uncertain use first.")
            entries.add(entry_id)
    after, empty, directory_entries = {}, [], []
    for field in ROLE_FIELDS:
        folders = sorted({folder for (_, candidate), (folder, _) in selected.items() if candidate == field}, key=_path_key)
        if not folders:
            folder = str(Path(policy.fast_root) / ".collectigen-engine-empty" / field)
            folders = [folder]; empty.append(folder)
        after[field] = ";".join(folders)
        if len(after[field]) > 4096 or any(";" in folder or any(ord(c) < 32 for c in folder) for folder in folders):
            held.append("A proposed role path is malformed or exceeds the engine's supported bound.")
    for (_, _), (folder, entries) in sorted(selected.items()):
        directory_entries.append((folder, tuple(sorted(entries))))
    additions, removals = [], []
    for field in ROLE_FIELDS:
        old = [part.strip() for part in inventory.values[field].split(";") if part.strip()]
        new = after[field].split(";")
        additions.extend((field, path, "qualified current-Fast role folder" if path not in empty else "empty inactive-role landing folder")
            for path in new if _path_key(path) not in {_path_key(item) for item in old})
        removals.extend((field, path, "not an exact qualified current-Fast role folder; no cold, relative or broad fallback retained")
            for path in old if _path_key(path) not in {_path_key(item) for item in new})
    if inventory.model_root != policy.fast_root:
        removals.append(("modelroot", inventory.model_root, "replace all previous roots, including implicit root-relative fallbacks"))
        additions.append(("modelroot", policy.fast_root, "single current Fast root"))
    qualification = "adapter_qualified" if qualifications and all(q == "adapter_qualified" for q in qualifications) else "unknown"
    text = ("Review managed Swarm paths only. The root and seven role lists replace previous cold, retired, relative and broad paths. "
        "Every owned model sharing these folders must retain a manual external-use pin until external use finishes. Other runtimes' leases do not protect Swarm use. "
        "The root change also changes default download placement and may reload enabled self-start Comfy backends. "
        "Engine downloads are not automatically cache-owned. External backends, native defaults and custom-node paths remain unverified; this is not filesystem isolation. "
        + ("The selected preparation has adapter-qualified companion evidence." if qualification == "adapter_qualified" else
           "Companion completeness and adapter readiness remain Unknown; this is a manual prepared-file handoff."))
    implicit = tuple(str(Path(policy.fast_root) / name) for name in IMPLICIT_MODEL_FOLDERS)
    return HotFolderProjection(registry_token, digest(snapshot.get("workspace", {})), inventory.fingerprint, tuple(sorted(inventory.binding.items())), ids,
        tuple(sorted({entry for _, entries in directory_entries for entry in entries})), inventory.model_root, policy.fast_root,
        tuple((field, inventory.values[field]) for field in ROLE_FIELDS), tuple((field, after[field]) for field in ROLE_FIELDS),
        tuple(directory_entries), tuple(empty), implicit, tuple(additions), tuple(removals), qualification,
        tuple(dict.fromkeys(held)), text)
