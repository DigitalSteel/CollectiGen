"""Exact registered-model guidance shared by UI and saved cross-module drafts."""
from copy import deepcopy
from pathlib import Path
import re
from core.annotation import GenerationResource

def _assert_portable_provenance(value, *, location="registered resource", depth=0, budget=None):
    """Fail closed before machine-local registry detail enters a portable plan."""
    if budget is None:
        budget = [20_000]
    budget[0] -= 1
    if budget[0] < 0 or depth > 64:
        raise ValueError("The registered model provenance is too complex for a portable generation plan.")
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_portable_provenance(key, location=f"{location} field name", depth=depth + 1, budget=budget)
            _assert_portable_provenance(item, location=f"{location}.{key}", depth=depth + 1, budget=budget)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_portable_provenance(item, location=f"{location}[{index}]", depth=depth + 1, budget=budget)
    elif isinstance(value, str):
        without_urls = re.sub(r"https?://\S+", "", value, flags=re.IGNORECASE)
        if (re.search(r"(?<![a-zA-Z0-9])[a-zA-Z]:[\\/]", value)
                or re.search(r"\\\\[^\\\s]+[\\/]", value)
                or re.search(r"(?<![a-zA-Z0-9])/(?:[^/\s]+)(?:/[^\s]*)?", without_urls)):
            raise ValueError(
                f"The registered model provenance contains a machine-local path at {location}. "
                "Review its lineage in Models before creating a portable generation plan."
            )


def _registered_resource_with_label(registry, resource_id, *, selected_role="", selected_family="", resource_guard=None):
    """Read one exact resource and a path-free presentation label together."""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(resource_id)):
        raise ValueError("This generation adapter requires a single-file SHA-256 resource; preparation packs stay in Setup.")
    value, _revision = registry.snapshot()
    record = value["resources"].get(resource_id)
    if record is None:
        raise ValueError("The selected registered model is no longer in the model registry.")
    if resource_guard is not None:
        resource_guard(record)
    declarations = deepcopy(record.get("declarations", []))
    if selected_role:
        declarations = [item for item in declarations if item.get("role") == selected_role
            and (not selected_family or item.get("family", "").casefold() == selected_family.casefold())]
    roles = {str(item.get("role", "")).strip() for item in declarations if item.get("role")}
    if len(roles) != 1:
        raise ValueError("The selected registered model does not have one unambiguous resource role.")
    role = roles.pop()
    families = sorted({str(item.get("family", "")).strip() for item in declarations
                       if item.get("role") == role and str(item.get("family", "")).strip()})
    lineage = deepcopy(record.get("lineage", []))
    _assert_portable_provenance({"declarations": declarations, "lineage": lineage})
    sha256 = resource_id.removeprefix("sha256:")
    resource = GenerationResource(
        id=resource_id,
        role=role,
        name=f"Registered {role.replace('_', ' ').title()}",
        hashes={"sha256": sha256},
        source_id=resource_id,
        compatibility=families,
        provenance={
            "authority": "model_resource_registry",
            "resource_id": resource_id,
            "declarations": declarations,
            "lineage": lineage,
        },
    )
    user = record.get("user", {})
    custom = str(user.get("display_name", "") or "").strip() if isinstance(user, dict) else ""
    locations = record.get("locations", ())
    basename = next((Path(item["path"]).name for item in locations
                     if isinstance(item, dict) and item.get("path")), "")
    label = custom if _safe_model_label(custom) else basename if _safe_model_label(basename) else resource_id
    return resource, label


def _safe_model_label(value):
    return isinstance(value, str) and bool(value) and len(value) <= 160 and not any(
        char in value for char in ("/", "\\", "\r", "\n", "\t")) and all(ord(char) >= 32 for char in value)


def _resource_label_key(resource):
    compatibility = resource.get("compatibility", ())
    family = compatibility[0] if isinstance(compatibility, (list, tuple)) and len(compatibility) == 1 else ""
    return resource.get("id", ""), resource.get("role", ""), family


def _updated_resource_labels(context, resources, *, selected=None, label=""):
    """Retain only labels tied to current exact declarations; never alter request resources."""
    target = dict(context)
    current = {_resource_label_key(item) for item in resources}
    previous = target.get("resource_display_labels", ())
    entries = [item for item in previous if isinstance(item, dict) and
               (item.get("resource_id"), item.get("role"), item.get("family")) in current and
               _safe_model_label(item.get("label"))] if isinstance(previous, (list, tuple)) else []
    if selected is not None and _safe_model_label(label):
        key = _resource_label_key(selected)
        entries = [item for item in entries if
                   (item["resource_id"], item["role"], item["family"]) != key]
        entries.append(dict(resource_id=key[0], role=key[1], family=key[2], label=label))
    if entries:
        target["resource_display_labels"] = entries
    else:
        target.pop("resource_display_labels", None)
    return target


def _resource_display_label(resource, context):
    key = _resource_label_key(resource.to_dict())
    values = context.get("resource_display_labels", ())
    if isinstance(values, (list, tuple)):
        for item in values:
            if isinstance(item, dict) and (item.get("resource_id"), item.get("role"), item.get("family")) == key:
                label = item.get("label")
                if isinstance(label, str) and _safe_model_label(label):
                    return label
    return resource.name


def apply_reviewed_model_guidance(plan, registry, guidance, configuration, *, expected_registry_revision=None, selected_resources=()):
    if configuration is None or guidance is None or guidance.request.workflow_kind != 'generation':
        return None
    resources = [item.to_dict() for item in plan.request.resources]
    changes = configuration.request_changes()
    if expected_registry_revision is not None and (registry is None or registry.snapshot()[1] != expected_registry_revision):
        raise ValueError('Registered models changed. Review a fresh recommendation before applying it.')
    chosen = [(guidance.request.base_model_id, 'checkpoint')] if guidance.request.base_model_id else []
    for identity, role in selected_resources:
        if role != 'lora':
            raise ValueError('The selected model role changed; reopen Models and review it again.')
        chosen.append((identity, role))
    requirements = {item.slot: item for item in guidance.companion_requirements}
    for binding in guidance.request.resource_bindings:
        requirement = requirements.get(binding.slot)
        if requirement is None:
            raise ValueError('A companion requirement changed; reopen Model Guidance.')
        chosen.append((binding.resource_id, requirement.role))
    if chosen and registry is None:
        raise ValueError('Open this planner from Models or Dataset to apply exact registered model choices.')
    selected_labels = []
    for identity, role in chosen:
        resource, label = _registered_resource_with_label(registry, identity, selected_role=role)
        resources = [item for item in resources if not (item.get('role') == role and (role in {'checkpoint', 'vae', 'controlnet'} or item.get('id') == resource.id))]
        resources.append(resource.to_dict())
        selected_labels.append((resource.to_dict(), label))
        if role == 'checkpoint':
            changes.update(model='', model_hash=resource.hashes['sha256'])
    changes['resources'] = resources
    context = _updated_resource_labels(plan.target_context, resources)
    for resource, label in selected_labels:
        context = _updated_resource_labels(context, resources, selected=resource, label=label)
    context['model_guidance'] = {'recipe_id': configuration.recipe_id, 'recipe_version': configuration.recipe_version, 'family': configuration.target_model_family, 'model_version': configuration.target_model_version, 'record_id': getattr(guidance, 'record_id', ''), 'source_urls': list(getattr(guidance, 'source_urls', ())), 'authority': 'reviewed_configuration_advice', 'execution_readiness': 'preflight_required', 'resource_bindings': [{'slot': item.slot, 'resource_id': item.resource_id} for item in guidance.request.resource_bindings]}
    if expected_registry_revision is not None and registry.snapshot()[1] != expected_registry_revision:
        raise ValueError('Registered models changed. Review a fresh recommendation before applying it.')
    plan = plan.revise({'request': changes, 'target_context': context, 'goal': guidance.request.goal}, source='model_guidance', consequence='Applied reviewed model-specific generation settings and selected resources; exact adapter preflight remains required')
    return plan
