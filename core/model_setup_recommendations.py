"""Read-only, explainable model setup suggestions for a selected exact identity.

This module never loads model bytes, binds a generator, or establishes adapter
compatibility. Callers must deliberately review and apply proposed changes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .model_organization import configured_family_hints, normalize_model_family
from .model_workflow_guidance import _requirements_for


_SETTING_FIELDS = ("width", "height", "steps", "cfg_scale", "sampler", "scheduler")


@dataclass(frozen=True)
class SetupRecommendation:
    resource_id: str
    family: str
    family_source: str
    declaration_families: tuple[str, ...]
    folder_hints: tuple[tuple[str, str], ...]
    provider_family_claims: tuple[str, ...]
    embedded_claims: dict[str, str]
    catalog_record_id: str
    recipe_id: str
    recipe_version: int
    catalog_choices: tuple[dict[str, Any], ...]
    suggested_settings: dict[str, Any]
    setting_sources: dict[str, str]
    preserved_fields: tuple[str, ...]
    companion_slots: tuple[dict[str, Any], ...]
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _embedded_claims(record):
    identity = record.get("identity", {})
    container = identity.get("container", {}) if isinstance(identity, Mapping) else {}
    metadata = container.get("metadata", {}) if isinstance(container, Mapping) else {}
    if not isinstance(metadata, Mapping):
        return {}
    return {key: str(metadata["modelspec." + key])[:8000]
            for key in ("title", "author", "description", "resolution")
            if isinstance(metadata.get("modelspec." + key), str) and metadata["modelspec." + key]}


def _provider_claims(record, resource_id):
    families, catalog_ids = set(), set()
    for claim in record.get("provider_evidence", ()):
        if not isinstance(claim, Mapping):
            continue
        item = claim.get("item", {})
        if not isinstance(item, Mapping):
            continue
        # Attached evidence is still a provider claim. The exact full-file
        # match only ties that claim to these bytes, not to tested support.
        files = item.get("files", ())
        if not any(isinstance(row, Mapping) and row.get("hash_scope") == "full_file"
                   and "sha256:" + str(row.get("sha256", "")).lower() == resource_id.lower()
                   for row in files):
            continue
        family = str(item.get("base_model", "") or "").strip()
        if family:
            families.add(family)
        if claim.get("provider") == "hugging_face" and item.get("repo_id"):
            catalog_ids.add(str(item["repo_id"]))
    return tuple(sorted(families)), tuple(sorted(catalog_ids))


def _catalog_records(catalog):
    if catalog is None:
        return ()
    source = catalog.catalog if hasattr(catalog, "catalog") else catalog
    if not isinstance(source, Mapping):
        raise ValueError("The guidance catalog must be a mapping or loaded guidance service.")
    records = source.get("records", ())
    if not isinstance(records, (list, tuple)):
        raise ValueError("The guidance catalog records are unavailable.")
    return tuple(row for row in records if isinstance(row, Mapping))


def _companion_slots(record, snapshot, selected, verified):
    result = []
    resources = snapshot.get("resources", {})
    verified = set(verified)
    for requirement in _requirements_for(record, "generation"):
        candidate_ids = []
        for resource_id, resource in resources.items():
            if any(isinstance(row, Mapping) and row.get("role") == requirement.role
                   and normalize_model_family(row.get("family", "")).casefold()
                   == normalize_model_family(requirement.family).casefold()
                   and (not requirement.variant or not row.get("variant")
                        or str(row["variant"]).casefold() == requirement.variant.casefold())
                   and (not requirement.revision or not (row.get("revision") or row.get("version"))
                        or str(row.get("revision") or row.get("version")).casefold()
                        == requirement.revision.casefold())
                   for row in resource.get("declarations", ())):
                candidate_ids.append(resource_id)
        candidate_ids.sort()
        chosen = str(selected.get(requirement.slot, "") or "")
        if chosen:
            state = "selected"
        elif not candidate_ids:
            state = "missing"
        elif len(candidate_ids) > 1:
            state = "ambiguous"
        elif requirement.compatibility_status == "not_assessed" or not requirement.accepted_sha256:
            state = "not_assessed"
        else:
            state = "unambiguous"
        accepted = {"sha256:"+value.removeprefix("sha256:") for value in requirement.accepted_sha256}
        suggested = candidate_ids[0] if state == "unambiguous" and requirement.compatibility_status == "tested" and candidate_ids[0] in verified \
            and candidate_ids[0] in accepted else ""
        if state == "unambiguous" and not suggested:
            state = "not_assessed"
        if chosen and chosen not in candidate_ids:
            selected_status = "declaration mismatch"
        elif chosen and chosen not in verified:
            selected_status = "recheck required"
        elif chosen and (requirement.compatibility_status == "not_assessed" or not accepted):
            selected_status = "compatibility not assessed"
        elif chosen and chosen not in accepted:
            selected_status = "catalog hash mismatch"
        elif chosen and requirement.compatibility_status != "tested":
            selected_status = "documented only; adapter compatibility not tested"
        elif chosen:
            selected_status = "catalog match"
        else:
            selected_status = "not selected"
        result.append({
            "slot": requirement.slot, "role": requirement.role, "family": requirement.family,
            "variant": requirement.variant, "revision": requirement.revision,
            "necessity": requirement.necessity,
            "compatibility_status": requirement.compatibility_status,
            "state": state, "selected_resource_id": chosen,
            "selected_status": selected_status,
            "candidate_ids": tuple(candidate_ids), "suggested_resource_id": suggested,
            "verification": "verified" if chosen in verified else "recheck required" if chosen else "not assessed",
            "note": requirement.revision_note,
        })
    if not result:
        for slot, role in (("vae", "vae"), ("text_encoder", "text_encoder")):
            result.append({"slot": slot, "role": role, "family": "", "variant": "", "revision": "",
                           "necessity": "not specified", "compatibility_status": "not_assessed",
                           "state": "selected" if selected.get(slot) else "not_assessed",
                           "selected_resource_id": str(selected.get(slot, "") or ""),
                           "candidate_ids": (), "suggested_resource_id": "",
                           "verification": "recheck required" if selected.get(slot) else "not assessed",
                           "note": "The catalog does not establish whether this component is bundled or separately required."})
    return tuple(result)


def recommend_model_setup(snapshot: Mapping[str, Any], resource_id: str, *, catalog=None,
                          current_request: Mapping[str, Any] | None = None,
                          selected_companions: Mapping[str, str] | None = None,
                          verified_resource_ids=()) -> SetupRecommendation:
    """Propose a sourced setup for a registered model without changing a draft.

    `current_request` and `selected_companions` represent deliberate caller
    choices. Numeric settings require an exact catalog binding; a family folder
    alone supplies only a labeled family hint and reviewable catalog options.
    """
    resources = snapshot.get("resources", {})
    record = resources.get(resource_id)
    if not isinstance(record, Mapping):
        raise ValueError("Choose a registered exact model identity.")
    current = current_request or {}
    selected = selected_companions or {}
    if not isinstance(current, Mapping) or not isinstance(selected, Mapping):
        raise ValueError("Current settings and companion choices must be mappings.")
    records = _catalog_records(catalog)
    by_id = {str(row.get("id", "")): row for row in records}
    declarations = tuple(row for row in record.get("declarations", ()) if isinstance(row, Mapping))
    declared = tuple(sorted({normalize_model_family(row.get("family", "")) for row in declarations
                             if row.get("role") in ("checkpoint", "unet") and row.get("family")
                             and str(row["family"]).strip().casefold() not in {"unknown", "unclassified"}}))
    paths = tuple(str(row.get("path", "")) for row in record.get("locations", ())
                  if isinstance(row, Mapping) and row.get("path"))
    hints = configured_family_hints(snapshot.get("workspace", {}), paths)
    provider_claims, provider_ids = _provider_claims(record, resource_id)
    exact_ids = {str(row.get("catalog_record_id")) for row in declarations if row.get("catalog_record_id")}
    user = record.get("user", {})
    if isinstance(user, Mapping) and user.get("catalog_record_id"):
        exact_ids.add(str(user["catalog_record_id"]))
    # A repository match identifies a model line, not an exact recipe revision.
    # Keep it a catalog option unless the user explicitly binds the recipe.
    blockers = []
    if len(declared) > 1:
        blockers.append("Conflicting main-model family declarations need review.")
    if len({family for _, family in hints}) > 1:
        blockers.append("The same model is indexed under conflicting configured family destinations.")
    if len({media_type for media_type, _ in hints}) > 1:
        blockers.append("The same model is indexed under both Images and Video family destinations; resolve its intended media.")
    if len(exact_ids) > 1:
        blockers.append("Attached exact-model catalog identities conflict.")
    exact_id = next(iter(exact_ids)) if len(exact_ids) == 1 and next(iter(exact_ids)) in by_id else ""
    exact_record = by_id.get(exact_id)
    if exact_ids and not exact_record and len(exact_ids) == 1:
        blockers.append("The selected exact catalog identity has no loaded recipe record.")
    family = ""
    source = "unresolved"
    if len(declared) == 1:
        family, source = declared[0], "user declaration"
    elif not declared and exact_record:
        family, source = normalize_model_family(exact_record.get("family", "")), "selected-model catalog record"
    elif not declared and len(provider_claims) == 1:
        family, source = normalize_model_family(provider_claims[0]), "attributed provider claim"
    elif not declared and not provider_claims and len(hints) == 1:
        family, source = hints[0][1], "Suggested from folder location"
    if family and exact_record and normalize_model_family(exact_record.get("family", "")) != family:
        blockers.append("The exact catalog family conflicts with the user's declaration.")
    if family and hints and any(item != family for _, item in hints):
        blockers.append("The configured folder family conflicts with stronger selected-model evidence.")
    if family and provider_claims and any(normalize_model_family(item) != family for item in provider_claims):
        blockers.append("The provider base-model claim differs from the selected family; review it.")
    choices = []
    for row in records:
        if (family and normalize_model_family(row.get("family", "")) == family) or str(row.get("id", "")) in provider_ids:
            recipe = row.get("generation_recipe") or {}
            choices.append({"catalog_record_id": str(row.get("id", "")),
                            "family": str(row.get("family", "")), "version": str(row.get("version", "")),
                            "variant": str(row.get("variant", "")), "catalog_status": str(row.get("catalog_status", "")),
                            "summary": str(row.get("summary", "")), "recipe_id": str(recipe.get("id", "")),
                            "recipe_version": int(recipe.get("version", 0)),
                            "source": "exact catalog record" if row is exact_record else "unverified family option"})
    suggested, sources, preserved = {}, {}, []
    if family:
        if current.get("target_model_family") in (None, ""):
            suggested["target_model_family"] = family
            sources["target_model_family"] = source
        else:
            preserved.append("target_model_family")
    recipe = (exact_record.get("generation_recipe") if exact_record and not blockers
              and exact_record.get("catalog_status") == "current" else None)
    if recipe:
        for field in _SETTING_FIELDS:
            value = recipe.get("configuration", {}).get(field)
            if field in current and current[field] not in (None, ""):
                preserved.append(field)
            elif value not in (None, ""):
                suggested[field] = value
                sources[field] = str(recipe.get("configuration_evidence", {}).get(field, "catalog recipe"))
    slots = _companion_slots(exact_record, snapshot, selected, verified_resource_ids) if exact_record else ()
    for slot in slots:
        if slot["necessity"] == "required" and slot.get("selected_resource_id") and slot.get("selected_status") != "catalog match":
            blockers.append(f"Selected {slot['slot'].replace('_', ' ')} needs resolution: {slot['selected_status']}.")
    return SetupRecommendation(str(resource_id), family, source, declared, hints, provider_claims,
        _embedded_claims(record), exact_id, str(recipe.get("id", "")) if recipe else "",
        int(recipe.get("version", 0)) if recipe else 0, tuple(choices), suggested, sources,
        tuple(preserved), slots, tuple(dict.fromkeys(blockers)))
