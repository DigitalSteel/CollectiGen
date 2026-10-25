"""Build an explicit import from choices collected by the ordinary Models UI."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path

from .model_import_actions import registration_effects, acquisition_effects, companion_effects
from .model_import_plan import ImportPlan, ImportStep, parse_plan, plan_payload
from .model_import_storage_actions import move_effects, manual_cache_effects, paths_effects
from .model_resource_registry import ModelResourceError
from .model_scan_review import ScanReviewCandidate, ScanReviewDraft, _decode, _encode
from .model_workspace import IndexDeclaration


def split_registration_choice(choice, new_choice_paths):
    """Partition one reviewed selection without reading files or granting approval."""
    if choice.get("kind") != "registration":
        raise ModelResourceError("Only a reviewed file choice can be split.")
    raw = choice.get("review")
    draft = _decode(raw) if isinstance(raw, dict) else _decode(json.loads(_encode(raw)))
    selected = {str(path).casefold(): path for path in draft.selected_paths}
    moved = [str(Path(path)).casefold() for path in new_choice_paths]
    if len(moved) != len(set(moved)) or not moved or len(moved) >= len(selected) or not set(moved) <= set(selected):
        raise ModelResourceError("Select at least one, but not every, exact file for the new choice.")
    moved_keys = set(moved)
    def part(keys, *, held):
        subset = replace(draft,
            candidates=tuple(row for row in draft.candidates if str(row.path).casefold() in keys),
            declarations=tuple(row for row in draft.declarations if str(row.path).casefold() in keys),
            selected_paths=tuple(path for path in draft.selected_paths if str(path).casefold() in keys),
            held=held)
        return _decode(json.loads(_encode(subset)))
    first = deepcopy(choice); second = deepcopy(choice)
    first["review"] = part(set(selected) - moved_keys, held=draft.held)
    second["review"] = part(moved_keys, held=())
    first.pop("media_type", None); second.pop("media_type", None)
    return first, second


def saved_file_correction_moves(plan, step_id, *, allow_omit_terminal_paths=False):
    """Inspect graph capability, not receipt authority or permission to revise."""
    plan = _file_correction_plan(plan, allow_omit_terminal_paths)
    return _saved_file_correction_dependencies(plan, step_id)[0]


def _file_correction_plan(plan, allow_omit_terminal_paths, *, prefix_length=0):
    if type(allow_omit_terminal_paths) is not bool:
        raise ModelResourceError("Explicitly choose whether to omit optional folder sharing.")
    if not allow_omit_terminal_paths:
        return plan
    # Validate the original graph before dropping anything: omission must not
    # conceal malformed choices, duplicate IDs or broken dependencies.
    plan = parse_plan(plan_payload(plan))
    paths = [step for step in plan.steps if step.kind == "paths"]
    if not paths:
        return plan
    if (len(paths) != 1 or paths[0] != plan.steps[-1]
            or any(paths[0].step_id in step.depends_on for step in plan.steps)):
        raise ModelResourceError("Only one terminal optional folder-sharing step can be omitted.")
    if len(plan.steps) - 1 < prefix_length:
        raise ModelResourceError("Started folder sharing keeps its exact recovery owner; it cannot be omitted.")
    if paths[0].expected_effects.get("action") != "share_model_paths":
        raise ModelResourceError("The terminal step does not describe optional model-folder sharing.")
    return parse_plan(plan_payload(replace(plan, steps=plan.steps[:-1])))


def _saved_file_correction_dependencies(plan, step_id):
    """Map plain Fast members to exact origins, never merely their content ID."""
    registrations = {step.step_id: step for step in plan.steps if step.kind == "registration"}
    if step_id not in registrations or any(step.depends_on for step in registrations.values()):
        raise ModelResourceError("Choose an unstarted direct registration to correct.")
    moves, caches = {}, []
    for step in plan.steps:
        if step.kind == "registration":
            continue
        if step.kind == "manual_cache":
            caches.append(step)
            continue
        if step.kind != "move" or len(step.depends_on) != 1 or step.depends_on[0] not in registrations:
            raise ModelResourceError("This saved import has other dependent work; preserve its receipts and review the full dependency chain.")
        owner = step.depends_on[0]
        selection = step.choices.get("selection")
        files = registrations[owner].expected_effects.get("files", ())
        if not isinstance(selection, dict) or set(selection) != {"resource_id", "source", "role", "family", "media_type"}:
            raise ModelResourceError("A saved move does not identify one exact registered file.")
        matching = [row for row in files if row.get("resource_id") == selection["resource_id"]
            and row.get("path") == selection["source"] and row.get("role") == selection["role"]
            and row.get("family") == selection["family"]]
        key = (owner, selection["source"])
        if len(matching) != 1 or key in moves or selection["media_type"] not in ("Images", "Video"):
            raise ModelResourceError("A saved move cannot be matched uniquely to its registered file.")
        if (step.expected_effects.get("selection") != selection
                or step.expected_effects.get("destination") != step.choices.get("destination")):
            raise ModelResourceError("A saved move's reviewed consequences no longer match its exact choice.")
        moves[key] = step
    if not caches:
        return moves, None, ()
    if len(caches) != 1 or caches[0] != plan.steps[-1]:
        raise ModelResourceError("Saved file correction supports only one final unstarted plain Fast preparation.")
    cache = caches[0]
    raw = cache.choices.get("requirements")
    if (set(cache.choices) != {"requirements", "approved_cleanup"}
            or cache.choices["approved_cleanup"] != [] or not isinstance(raw, dict)
            or set(raw) - {"resources", "qualification"}
            or raw.get("qualification", "unknown") != "unknown"):
        raise ModelResourceError("Qualified Fast sets or cleanup need separate dependency review; their authority cannot be reduced here.")
    resources = raw.get("resources")
    if not isinstance(resources, list) or not 1 <= len(resources) <= 64:
        raise ModelResourceError("Review a nonempty bounded exact Fast preparation set.")
    if (cache.expected_effects.get("resources") != resources
            or cache.expected_effects.get("qualification") != "unknown"
            or cache.expected_effects.get("cleanup") != []
            or cache.expected_effects.get("archive_originals") != "retained"
            or cache.expected_effects.get("manual_external_use_pin") is not True):
        raise ModelResourceError("The saved Fast consequences do not match its plain exact choices.")
    for registration in registrations.values():
        if registration_effects(registration.choices) != registration.expected_effects:
            raise ModelResourceError("The saved registration consequences differ from its exact files.")
    members = []
    for resource in resources:
        if (not isinstance(resource, dict)
                or set(resource) != {"resource_id", "archive_path", "role", "family", "media_type"}
                or resource["media_type"] not in ("Images", "Video")):
            raise ModelResourceError("Every Fast member needs its exact identity, Archive path and declaration.")
        matches = []
        for owner, registration in registrations.items():
            for row in registration.expected_effects["files"]:
                move = moves.get((owner, row["path"]))
                archive = move.choices["destination"] if move else row["path"]
                if (resource["resource_id"] == row["resource_id"] and resource["archive_path"] == archive
                        and resource["role"] == row["role"] and resource["family"] == row["family"]
                        and (move is None or resource["media_type"] == move.choices["selection"]["media_type"])):
                    matches.append((owner, row["path"], move.step_id if move else owner))
        if len(matches) != 1 or matches[0] in members:
            raise ModelResourceError("A Fast member cannot be matched uniquely to its exact registered Archive origin.")
        members.append(matches[0])
    if set(cache.depends_on) != {member[2] for member in members}:
        raise ModelResourceError("The saved Fast dependencies do not match its exact Archive origins.")
    return moves, cache, tuple(members)


def correct_saved_registration(plan, prefix_length, original, selection, snapshot, *,
                               allow_omit_terminal_paths=False):
    """Propose only; the caller must obtain fresh service prefix proof/reapproval."""
    if type(prefix_length) is not int or not 0 <= prefix_length <= len(plan.steps):
        raise ModelResourceError("Saved correction requires a verified completed-prefix length.")
    plan = _file_correction_plan(plan, allow_omit_terminal_paths, prefix_length=prefix_length)
    _moves, cache, members = _saved_file_correction_dependencies(plan, original.step_id)
    index = next((i for i, step in enumerate(plan.steps) if step.step_id == original.step_id), -1)
    if index < prefix_length or index < 0 or plan.steps[index] != original:
        raise ModelResourceError("The selected saved step changed or has already started.")
    effects = registration_effects(selection)
    old = {row["path"]: row for row in original.expected_effects["files"]}
    new = effects["files"]
    if not new or len(new) >= len(old) or any(row != old.get(row["path"]) for row in new):
        raise ModelResourceError("Saved recovery can only exclude changed files while keeping a nonempty unchanged subset. New bytes or declarations need a new import.")
    removed = set(old) - {row["path"] for row in new}
    if cache is not None and plan.steps.index(cache) < prefix_length:
        raise ModelResourceError("The saved Fast preparation has already started; preserve its exact recovery owner.")
    steps = []
    for i, step in enumerate(plan.steps):
        if i == index:
            steps.append(replace(step, choices=deepcopy(selection), expected_effects=effects))
        elif i >= prefix_length and step.kind == "move":
            if step.depends_on[0] == original.step_id and step.choices["selection"]["source"] in removed:
                continue
            steps.append(replace(step, expected_effects=move_effects(step.choices, snapshot)))
        elif cache is not None and step.step_id == cache.step_id:
            retained = [(resource, member) for resource, member in zip(cache.choices["requirements"]["resources"], members)
                if not (member[0] == original.step_id and member[1] in removed)]
            if not retained:
                raise ModelResourceError("Saved correction must retain a nonempty exact Fast set; review removal of the whole preparation separately.")
            choices = deepcopy(cache.choices)
            choices["requirements"]["resources"] = [deepcopy(resource) for resource, _member in retained]
            dependencies = {member[2] for _resource, member in retained}
            steps.append(replace(step, choices=choices,
                depends_on=tuple(dependency for dependency in step.depends_on if dependency in dependencies),
                expected_effects=manual_cache_effects(choices, snapshot)))
        else:
            steps.append(step)
    return parse_plan(plan_payload(replace(plan, steps=tuple(steps))))


def planned_fast_paths(snapshot, inventory, resources, *, excluded_paths=()):
    """Describe the future namespace; this is never hot-folder qualification.

    The paths port will qualify the actual complete namespace after preparation.
    Extra files, changed contexts or lost manual pins then hold the write.
    """
    from .model_engine_locations import SWARM_ROLES, _path_key
    from .model_storage_policy import load_policy, fast_destination
    from .model_use_leases import cache_state
    from .swarm_model_paths import ROLE_FIELDS
    policy = load_policy(snapshot.get("workspace", {}))
    if policy.mode != "tiered":
        raise ModelResourceError("Choose Fast storage before planning managed SwarmUI paths.")
    folders, exposure = {}, {}
    excluded = {_path_key(str(path)) for path in excluded_paths}
    entries = tuple(entry for entry in cache_state(snapshot)["entries"].values()
        if _path_key(entry.get("path", "")) not in excluded)
    for row in resources:
        role = row["role"]
        if role not in SWARM_ROLES:
            raise ModelResourceError(f"The selected {role} resource has no supported SwarmUI model-folder setting.")
        context = {key: row[key] for key in ("role", "family", "media_type")}
        destination = fast_destination(policy, resource_id=row["resource_id"], **context)
        folders.setdefault(SWARM_ROLES[role], set()).add(str(destination.parent))
        existing = [entry for entry in entries if _path_key(entry.get("path", "")) == _path_key(str(destination))]
        if len(existing) > 1:
            raise ModelResourceError("More than one cache entry claims a planned Fast path; review cache recovery.")
        contexts = deepcopy(existing[0].get("contexts", [])) if existing else []
        if context not in contexts: contexts.append(context)
        exposure[_path_key(str(destination))] = {"resource_id": row["resource_id"], "path": str(destination), "contexts": contexts}
    role_folders = {_path_key(path) for paths in folders.values() for path in paths}
    for entry in entries:
        path = entry.get("path", "")
        if entry.get("state") == "available" and _path_key(str(Path(path).parent)) in role_folders:
            exposure.setdefault(_path_key(path), {"resource_id": entry["resource_id"], "path": path,
                "contexts": deepcopy(entry.get("contexts", []))})
    values = {field: ";".join(sorted(folders.get(field, {str(Path(policy.fast_root) / ".collectigen-engine-empty" / field)}), key=_path_key))
        for field in ROLE_FIELDS}
    choices = {"mode": "tiered", "inventory": asdict(inventory), "values": values,
        "model_root": policy.fast_root, "prepared_steps": ["prepare_fast"]}
    return choices, tuple(exposure.values())


def build_import_plan(environment, source_binding, choices, *, organize=False, prepare_fast=False,
                      companion_requests=(), path_choice=None, exposed_resources=(), share_fast_inventory=None):
    """Project desired effects without creating files or a prospective registry.

    Canonical service previews subsequently verify the real prerequisites. Any
    changed destination, policy, exposure or cleanup set needs renewed review.
    """
    snapshot, token = environment.registry.snapshot()
    if environment.source_binding_reader() != source_binding:
        raise ModelResourceError("Read the enrolled SwarmUI source again before building an import.")
    steps, resources, registration_ids = [], [], []
    moved_paths = {}
    seen_paths = set()
    media_by_registration = {}
    for index, choice in enumerate(choices):
        dependency = ()
        if (organize or prepare_fast or share_fast_inventory is not None) and choice.get("media_type") is None:
            raise ModelResourceError("Choose Images or Video for each import choice before organizing or preparing its files.")
        if choice.get("kind") == "acquisition":
            plan = choice["plan"]
            receipt = plan.receipt() if hasattr(plan, "receipt") else deepcopy(plan)
            if organize:
                original_destination = str(receipt["destination"])
                media_type = choice["media_type"]
                folder, reason = environment.workspace.destination_details(snapshot, role=choice["role"], family=choice["family"], media_type=media_type)
                if not folder or "conflicting" in reason:
                    raise ModelResourceError("Set and review Folder Locations for each selected model family and type first.")
                # Publish directly to the reviewed organized location. Acquisition
                # retains a recovery hardlink, so it must not then be relocated
                # as though it were an independent standalone source file.
                receipt["destination"] = str(Path(folder) / Path(receipt["destination"]).name)
                moved_paths[("sha256:" + receipt["sha256"], original_destination)] = receipt["destination"]
            acquisition = {"plan": receipt, "role": choice["role"], "family": choice["family"]}
            identity = f"download_{index + 1}"
            steps.append(ImportStep(identity, "acquisition", (), acquisition, acquisition_effects(acquisition)))
            dependency = (identity,)
            path = Path(receipt["destination"])
            draft = ScanReviewDraft(path.parent, token,
                (ScanReviewCandidate(path, "sha256:" + receipt["sha256"], receipt["size"]),),
                (IndexDeclaration(path, choice["role"], choice["family"]),), (path,), ())
        elif choice.get("kind") == "registration":
            draft = choice["review"]
            if isinstance(draft, dict): draft = _decode(draft)
            # The new plan retains exact chosen metadata; the old scan token is
            # not executable authority. Explicit review rehashes selected files.
            draft = replace(draft, registry_token=token)
        else:
            raise ModelResourceError("Choose reviewed model files or an exact provider download.")
        selection = {"review": json.loads(_encode(draft))}
        effect = registration_effects(selection)
        if any(str(Path(row["path"])).casefold() in seen_paths for row in effect["files"]):
            raise ModelResourceError("The same file appears in more than one import choice. Keep one explicit selection.")
        seen_paths.update(str(Path(row["path"])).casefold() for row in effect["files"])
        identity = f"register_{index + 1}"
        from .model_organization import family_options, normalize_model_family
        families = {normalize_model_family(row["family"]) for row in effect["files"]}
        media_type = choice.get("media_type")
        if media_type not in (None, "Images", "Video"):
            raise ModelResourceError("Choose Images or Video for the selected model files.")
        if (organize or prepare_fast or share_fast_inventory is not None) and media_type is None:
            raise ModelResourceError("Choose Images or Video for each import choice before organizing or preparing its files.")
        if media_type == "Images" and families & set(family_options("Video")):
            raise ModelResourceError("A video model family cannot use Images folder or Fast preparation. Choose Video.")
        media_by_registration[identity] = media_type
        steps.append(ImportStep(identity, "registration", dependency, selection, effect))
        registration_ids.append(identity)
        resources.extend((identity, row) for row in effect["files"])
    if not steps:
        raise ModelResourceError("Choose at least one file or exact download for this import.")

    prepared_resources, preparation_dependencies = [], []
    for index, (registration_id, row) in enumerate(resources):
        path = Path(row["path"])
        dependency = registration_id
        media_type = media_by_registration[registration_id] or "Images"
        if organize:
            folder, reason = environment.workspace.destination_details(snapshot, role=row["role"], family=row["family"], media_type=media_type)
            if not folder or "conflicting" in reason:
                raise ModelResourceError("Set and review Folder Locations for each selected model family and type first.")
            destination = Path(folder) / path.name
            if destination != path:
                selection = {"resource_id": row["resource_id"], "source": str(path), "role": row["role"],
                    "family": row["family"], "media_type": media_type}
                move = {"selection": selection, "destination": str(destination)}
                dependency = f"move_{index + 1}"
                steps.append(ImportStep(dependency, "move", (registration_id,), move, move_effects(move, snapshot)))
                moved_paths[(row["resource_id"], str(path))] = str(destination)
                path = destination
        prepared_resources.append({"resource_id": row["resource_id"], "archive_path": str(path),
            "role": row["role"], "family": row["family"], "media_type": media_type})
        preparation_dependencies.append(dependency)

    companion_ids, reviewed_companions = [], []
    for index, request in enumerate(companion_requests):
        value = deepcopy(request)
        for choice in value["choices"]:
            choice["path"] = moved_paths.get((choice["resource_id"], choice["path"]), choice["path"])
            matching = [row for row in prepared_resources if row["resource_id"] == choice["resource_id"]]
            if not matching and (organize or prepare_fast):
                raise ModelResourceError("Add each required model as an import choice before organizing or preparing it.")
            resource = {"resource_id": choice["resource_id"], "archive_path": choice["path"],
                "role": choice["role"], "family": choice["family"],
                "media_type": matching[0]["media_type"] if matching else "Images"}
            if resource not in prepared_resources: prepared_resources.append(resource)
        identity = f"companions_{index + 1}"
        steps.append(ImportStep(identity, "companions", tuple(dict.fromkeys(preparation_dependencies)), value, companion_effects(value)))
        companion_ids.append(identity)
        reviewed_companions.append(value)
    preparation_id = None
    if prepare_fast:
        by_resource = {}
        for row in prepared_resources:
            previous = by_resource.setdefault(row["resource_id"], row)
            if previous != row:
                raise ModelResourceError("Choose one exact archive path and type for each model before preparing Fast storage.")
        cache = {"requirements": {"resources": list(by_resource.values()), "qualification": "unknown"}, "approved_cleanup": []}
        if len(reviewed_companions) == 1:
            from .model_companion_readiness import CompanionReadinessRequest, CompanionReadinessService
            companion = reviewed_companions[0]
            ordered = [{"resource_id": row["resource_id"], "archive_path": row["path"], "role": row["role"],
                "family": row["family"], "media_type": by_resource[row["resource_id"]]["media_type"]}
                for row in sorted(companion["choices"], key=lambda row: row["slot"]) if row["resource_id"] in by_resource]
            owner = CompanionReadinessService._owner(CompanionReadinessRequest(**companion["request"]))
            if owner is not None and len(ordered) == len(by_resource) and all(row in ordered for row in by_resource.values()):
                # This is a proposed complete set, not qualification. The port
                # re-verifies the current canonical owner and exact files.
                cache["requirements"] = {"resources": ordered, "companion_request": companion}
        preparation_id = "prepare_fast"
        dependencies = tuple(dict.fromkeys((*preparation_dependencies, *companion_ids)))
        steps.append(ImportStep(preparation_id, "manual_cache", dependencies, cache, manual_cache_effects(cache, snapshot)))
        if share_fast_inventory is not None:
            if path_choice is not None:
                raise ModelResourceError("Choose one exact engine-folder proposal.")
            path_choice, exposed_resources = planned_fast_paths(snapshot, share_fast_inventory, list(by_resource.values()))
    elif share_fast_inventory is not None:
        raise ModelResourceError("Include Fast preparation before sharing planned Fast model folders.")
    if path_choice is not None:
        path_choice = deepcopy(path_choice)
        if path_choice["mode"] == "tiered":
            if preparation_id is None:
                raise ModelResourceError("Include manual Fast preparation before sharing tiered folders.")
            path_choice["prepared_steps"] = [preparation_id]
            dependencies = (preparation_id,)
        else:
            path_choice["prepared_steps"] = []
            dependencies = tuple(dict.fromkeys((*preparation_dependencies, *companion_ids)))
        steps.append(ImportStep("share_paths", "paths", dependencies, path_choice,
            paths_effects(path_choice, snapshot, exposed_resources=exposed_resources)))
    return parse_plan(plan_payload(ImportPlan(deepcopy(source_binding), token, tuple(steps))))


def propose_remaining_import_plan(environment, service, approval_id, *, expected_revision,
                                  should_cancel=None, progress=None):
    """Refresh only unstarted consequences after canonical prefix proof.

    This issues no approval and performs no model/API writes. In particular,
    cleanup candidates come only from a real complete-set capacity preview.
    The caller must display and separately approve the returned proposal.
    """
    from .model_import_storage_actions import ManualCachePort
    from .model_resource_registry import _cancel

    def propose(plan, prefix_length, context):
        snapshot, token = environment.observe(context)
        completed = {row.step_id for row in context.completed if row.status == "completed"}
        steps = list(plan.steps[:prefix_length])
        for original in plan.steps[prefix_length:]:
            _cancel(should_cancel)
            choices = deepcopy(original.choices)
            effects = deepcopy(original.expected_effects)
            if original.kind == "move":
                # Keep the exact selected destination. A changed folder layout
                # may invalidate this choice rather than silently redirect it.
                effects = move_effects(choices, snapshot)
            elif original.kind == "manual_cache":
                choices["approved_cleanup"] = []
                if set(original.depends_on) <= completed:
                    port = ManualCachePort(environment)
                    candidate = replace(original, choices=choices)
                    requirements = port._requirements(candidate, token,
                        should_cancel=should_cancel, progress=progress)
                    with port._resolver(should_cancel=should_cancel, progress=progress):
                        preview = environment.cache.preview_prepare(requirements, expected_registry=token,
                            should_cancel=should_cancel, progress=progress)
                    if not preview.can_prepare:
                        raise ModelResourceError(preview.message)
                    choices["approved_cleanup"] = json.loads(json.dumps(
                        [asdict(row) for row in preview.capacity.victims] if preview.capacity else [],
                        default=str))
                effects = manual_cache_effects(choices, snapshot)
            elif original.kind == "paths":
                inventory = environment.paths.read()
                if inventory.binding != plan.source_binding:
                    raise ModelResourceError("The enrolled source changed. Read and review a new import.")
                choices["inventory"] = asdict(inventory)
                exposure = effects.get("exposed_resources", [])
                if choices["mode"] == "tiered":
                    prepared = [step for step in steps if step.step_id in choices["prepared_steps"]]
                    if len(prepared) != 1 or prepared[0].kind != "manual_cache":
                        raise ModelResourceError("Review the exact Fast preparation before its folder changes.")
                    cache = prepared[0].choices
                    choices, exposure = planned_fast_paths(snapshot, inventory,
                        cache["requirements"]["resources"],
                        excluded_paths=[row["path"] for row in cache["approved_cleanup"]])
                    choices["prepared_steps"] = [prepared[0].step_id]
                effects = paths_effects(choices, snapshot, exposed_resources=exposure)
            steps.append(replace(original, choices=choices, expected_effects=effects))
        if environment.registry.snapshot()[1] != token:
            raise ModelResourceError("The registry changed during remaining-work review.")
        return parse_plan(plan_payload(replace(plan, steps=tuple(steps))))

    with environment.operation(should_cancel=should_cancel, progress=progress):
        return service.propose_remaining(approval_id, propose, expected_revision=expected_revision,
            should_cancel=should_cancel, progress=progress)
