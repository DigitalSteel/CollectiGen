"""Reusable role-aware model selection over the shared private registry.

Projection reads registry metadata only. Exact local bytes are inspected only
after an explicit selection, and adapter compatibility remains a consumer-owned
preflight result.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Iterable, Mapping

from .model_resource_registry import (
    ModelResourceError,
    ModelResourceRegistry,
    RESOURCE_ROLES,
    _cancel,
    _current,
)
from .model_workspace import _path


@dataclass(frozen=True)
class ModelSelectionRow:
    resource_id: str
    role: str
    family: str
    display_name: str
    default_name: str
    locations: tuple[str, ...]
    tags: tuple[str, ...]
    notes: str
    favorite: bool
    last_used_at: str
    use_count: int
    cached_status: str
    verification_status: str
    compatibility: str
    recommendation_status: str
    ready_to_run: bool
    search_text: str
    title: str = ""
    author: str = ""
    description: str = ""
    resolution: str = ""
    source: str = ""
    license_note: str = ""
    size_bytes: int = 0
    provider_details: tuple[str, ...] = ()
    provider_title: str = ""
    folder_hints: tuple[tuple[str, str], ...] = ()
    provider_triggers: tuple[str, ...] = ()
    architecture_claim: str = ""
    derivative_label: str = ""
    context_compatibility: str = "unknown"
    context_evidence: str = ""
    artwork: dict | None = None
    artwork_status: str = "absent"


@dataclass(frozen=True)
class VerifiedModelSelection:
    resource_id: str
    role: str
    family: str
    path: str
    component_model_id: str = ""


class ModelLocationUnavailable(ModelResourceError):
    """The chosen exact registry identity has no currently verified local copy."""


@dataclass(frozen=True)
class ModelSelectionSummary:
    selections: tuple[tuple[str, str, str], ...]
    required_roles: tuple[str, ...]
    missing_roles: tuple[str, ...]
    conflicting_families: tuple[str, ...]
    compatibility: str
    ready_to_run: bool
    message: str


def protected_model_roots(settings: object | None) -> tuple[str, ...]:
    """Return existing image-library and backup roots without reading them."""
    if settings is None:
        return ()
    values: list[str] = []
    registry_factory = getattr(settings, "library_registry", None)
    if callable(registry_factory):
        registry = registry_factory()
        for profile in getattr(registry, "profiles", ()):
            root = str(getattr(profile, "root", "") or "").strip()
            if root:
                values.append(root)
    for key in (
        "ingest_directory",
        "problems_directory",
        "recycle_directory",
        "external_write_backup_directory",
    ):
        value = str(getattr(settings, key, "") or "").strip()
        if value:
            values.append(value)
    return tuple(dict.fromkeys(values))


def _user(record: Mapping[str, object]) -> Mapping[str, object]:
    value = record.get("user", {})
    if not isinstance(value, Mapping):
        raise ModelResourceError("The model's user preferences are invalid; repair the registry before selecting it.")
    return value


def _provider_display(evidence, resource_id):
    """Project exact attached claims without giving them selection authority."""
    details, titles, triggers = [], [], []
    for claim in evidence[:100]:
        if not isinstance(claim, Mapping):
            continue
        item = claim.get("item", {})
        if not isinstance(item, Mapping):
            continue
        files = item.get("files", [])
        if not isinstance(files, list) or not any(isinstance(file, Mapping)
            and file.get("hash_scope") == "full_file"
            and "sha256:" + str(file.get("sha256", "")) == resource_id for file in files):
            continue
        def text(key):
            value = item.get(key, "")
            return value[:4000] if isinstance(value, str) else ""
        title = text("model_name")
        if title:
            titles.append(title)
        source = claim.get("source", {})
        retrieved = source.get("retrieved_at", "Unknown date") if isinstance(source, Mapping) else "Unknown date"
        lines = [f"{claim.get('provider', 'Provider')} · {claim.get('currentness', 'unknown')} · {retrieved}"]
        for key, label in (("model_name", "Title"), ("version_name", "Version"),
                           ("base_model", "Reported base"), ("creator", "Creator"),
                           ("description", "Description"), ("page_url", "Source")):
            if text(key):
                lines.append(f"{label}: {text(key)}")
        trigger_claims = item.get("trigger_words", [])
        if isinstance(trigger_claims, list):
            words = [word[:200] for word in trigger_claims[:100] if isinstance(word, str)]
            if words:
                lines.append("Suggested trigger words: " + ", ".join(words))
                triggers.extend(words)
        lines.append("Attributed metadata; compatibility remains unverified.")
        details.append("\n".join(lines))
    unique_titles = tuple(dict.fromkeys(titles))
    return (tuple(details), unique_titles[0] if len(unique_titles) == 1 else "",
            tuple(dict.fromkeys(triggers))[:100])


def swarm_evidence_details(record):
    """Bounded plaintext attribution; never selection or compatibility authority."""
    from .swarm_model_import import project_swarm_model_evidence
    details = []
    for row in tuple(reversed(project_swarm_model_evidence(record)))[:10]:
        if row.get("status") != "attributed":
            details.append(str(row.get("summary", "SwarmUI evidence needs review in Raw Details."))[:1000])
            continue
        source = row.get("source", {})
        lines = [f"SwarmUI imported metadata · {row.get('retrieved_at', 'Date unknown')}",
            f"Source model: {row.get('model_name', '')} · {row.get('subtype', '')}",
            f"Source revision: {source.get('source_revision', 'Unknown')}"]
        for key, value in row.get("known", {}).items():
            if key in {"preview_image", "preview", "thumbnail"}: continue
            text = ", ".join(str(item) for item in value[:100]) if isinstance(value, list) else str(value)
            lines.append(f"{key.replace('_', ' ').capitalize()}: {text[:4000]}")
        unknown = row.get("unknown_fields", ())
        if unknown: lines.append(f"{len(unknown)} unsupported source fields retained in Raw Details.")
        lines.append("Attributed source claims; compatibility and current availability require a fresh check.")
        details.append("\n".join(lines)[:16000])
    return tuple(details)


_EXACT_RESOURCE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_QUALIFIED_AUTHORITIES = {"tested_adapter", "documented_catalog"}


def _context_compatibility(main_id, candidate_id, role, evidence):
    """Only exact, role-specific qualified pairs can narrow a contextual view.

    Family declarations and provider base-model claims are never pair evidence.
    Conflicting qualified claims remain unknown rather than silently resolving.
    """
    if not (_EXACT_RESOURCE.fullmatch(str(main_id)) and _EXACT_RESOURCE.fullmatch(str(candidate_id))):
        return "unknown", ""
    matches = [item for item in evidence if isinstance(item, Mapping)
               and item.get("main_model_resource_id") == main_id
               and item.get("resource_id") == candidate_id
               and item.get("role") == role
               and item.get("authority") in _QUALIFIED_AUTHORITIES
               and item.get("status") in {"compatible", "incompatible"}]
    statuses = {item["status"] for item in matches}
    if len(statuses) != 1:
        return "unknown", ""
    return statuses.pop(), ", ".join(sorted({str(item["authority"]) for item in matches}))


def _selection(value: object) -> tuple[str, str, str]:
    if isinstance(value, VerifiedModelSelection):
        return value.resource_id, value.role, value.family
    if isinstance(value, ModelSelectionRow):
        return value.resource_id, value.role, value.family
    if isinstance(value, str):
        return value, "", ""
    if isinstance(value, Mapping):
        return str(value.get("resource_id", "")), str(value.get("role", "")), str(value.get("family", ""))
    if all(hasattr(value, name) for name in ("role", "family")) and (hasattr(value, "resource_id") or hasattr(value, "id")):
        return (
            str(getattr(value, "resource_id", "") or getattr(value, "id", "")),
            str(getattr(value, "role", "")),
            str(getattr(value, "family", "")),
        )
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        return str(value[0]), str(value[1]), str(value[2])
    raise ModelResourceError("A selected resource needs an exact identity, role and family.")


class ModelSelectionService:
    """Project, verify and remember explicit choices without duplicating inventory."""

    def __init__(self, registry: ModelResourceRegistry, *, protected_roots=(), component_source=None):
        self.registry = registry
        self.protected_roots = tuple(protected_roots)
        self.component_source = component_source

    def project(
        self,
        *,
        query: str = "",
        role: str = "",
        family: str = "",
        favorite_only: bool = False,
        recent_only: bool = False,
        main_model_resource_id: str = "",
        compatibility_scope: str = "auto",
        qualified_compatibility: Iterable[Mapping[str, object]] = (),
    ) -> tuple[tuple[ModelSelectionRow, ...], str]:
        if role and role not in RESOURCE_ROLES:
            raise ModelResourceError("Choose a supported model resource role.")
        if compatibility_scope not in {"auto", "compatible", "unknown", "all"}:
            raise ModelResourceError("Choose a supported compatibility view.")
        if main_model_resource_id and not _EXACT_RESOURCE.fullmatch(main_model_resource_id):
            raise ModelResourceError("The selected main model needs an exact full-file identity.")
        scope = ("compatible" if main_model_resource_id and role in {"lora", "vae"}
                 else "all") if compatibility_scope == "auto" else compatibility_scope
        qualified = tuple(qualified_compatibility)
        query_key = " ".join(str(query or "").split()).casefold()
        from .model_organization import normalize_model_family, configured_family_hints
        family_key = normalize_model_family(family).casefold()
        snapshot, revision = self.registry.snapshot()
        rows: list[ModelSelectionRow] = []
        for resource_id, record in snapshot["resources"].items():
            user = _user(record)
            tags_value = user.get("tags", ())
            tags = tuple(str(item) for item in tags_value) if isinstance(tags_value, (list, tuple)) else ()
            notes = str(user.get("notes", "") or "")
            locations = tuple(str(item.get("path", "")) for item in record["locations"] if item.get("path"))
            folder_hints = configured_family_hints(snapshot.get("workspace", {}), locations)
            default_name = Path(locations[0]).name if locations else resource_id
            display_name = str(user.get("display_name", "") or "").strip() or default_name
            favorite = user.get("favorite", False) is True
            last_used_at = str(user.get("last_used_at", "") or "")
            use_count_value = user.get("use_count", 0)
            use_count = use_count_value if type(use_count_value) is int and use_count_value >= 0 else 0
            evidence = record.get("provider_evidence", ())
            searchable_evidence = evidence if isinstance(evidence, list) else ()
            provider_details, provider_title, provider_triggers = _provider_display(searchable_evidence, resource_id)
            provider_details = (*provider_details, *swarm_evidence_details(record))
            identity = record.get("identity", {})
            container = identity.get("container", {})
            metadata = container.get("metadata", {}) if isinstance(container, Mapping) else {}
            metadata = metadata if isinstance(metadata, Mapping) else {}
            size = identity.get("size", 0)
            size = size if type(size) is int and size >= 0 else 0
            # Embedded claims are display evidence, never role/compatibility authority.
            def claim(key):
                value = metadata.get("modelspec." + key, "")
                return value[:8000] if isinstance(value, str) else ""
            title, author, description, resolution = (claim(key) for key in ("title", "author", "description", "resolution"))
            architecture_claim = claim("architecture")
            embedded_derivative = claim("derivative") or claim("variant")
            seen_declarations: set[tuple[str, str]] = set()
            for declaration in record["declarations"]:
                declared_role = str(declaration.get("role", ""))
                declared_family = str(declaration.get("family", ""))
                if declared_role not in RESOURCE_ROLES or not declared_family:
                    continue
                declaration_key = (declared_role, declared_family.casefold())
                if declaration_key in seen_declarations:
                    continue
                seen_declarations.add(declaration_key)
                if role and declared_role != role:
                    continue
                if family_key and normalize_model_family(declared_family).casefold() != family_key:
                    continue
                compatibility = str(declaration.get("compatibility", "unverified") or "unverified")
                context_status, context_evidence = _context_compatibility(
                    main_model_resource_id, resource_id, declared_role, qualified)
                if main_model_resource_id and (scope == "compatible" and context_status != "compatible"
                        or scope == "unknown" and context_status != "unknown"):
                    continue
                search_parts = (
                    display_name,
                    default_name,
                    resource_id,
                    declared_role,
                    declared_family,
                    " ".join(tags),
                    notes,
                    str(declaration.get("source", "") or ""),
                    repr(searchable_evidence),
                    "\n".join(provider_details),
                    "favorite" if favorite else "",
                    title, author, description, resolution, " ".join(locations),
                )
                search_text = "\n".join(search_parts).casefold()
                if query_key and query_key not in search_text:
                    continue
                if favorite_only and not favorite:
                    continue
                if recent_only and not last_used_at:
                    continue
                from .model_artwork import project_artwork
                artwork, artwork_status = project_artwork(record, resource_id)
                rows.append(ModelSelectionRow(
                    resource_id=resource_id,
                    role=declared_role,
                    family=declared_family,
                    display_name=display_name,
                    default_name=default_name,
                    locations=locations,
                    tags=tags,
                    notes=notes,
                    favorite=favorite,
                    last_used_at=last_used_at,
                    use_count=use_count,
                    cached_status="stored" if locations else "no_location",
                    verification_status="recheck_required",
                    compatibility=compatibility,
                    recommendation_status="not_assessed",
                    ready_to_run=False,
                    search_text=search_text,
                    title=title,
                    author=author,
                    description=description,
                    resolution=resolution,
                    source=str(declaration.get("source", "") or ""),
                    license_note=str(declaration.get("license_note", "") or ""),
                    size_bytes=size,
                    provider_details=provider_details,
                    provider_title=provider_title,
                    folder_hints=folder_hints,
                    provider_triggers=provider_triggers,
                    artwork=artwork,
                    artwork_status=artwork_status,
                    architecture_claim=architecture_claim,
                    derivative_label=str(declaration.get("derivative", "") or declaration.get("variant", "") or embedded_derivative)[:300],
                    context_compatibility=context_status,
                    context_evidence=context_evidence,
                ))
        rows.sort(key=lambda item: (item.display_name.casefold(), item.role, item.family.casefold(), item.resource_id))
        rows.sort(key=lambda item: item.last_used_at, reverse=True)
        rows.sort(key=lambda item: item.favorite, reverse=True)
        return tuple(rows), revision

    def save_preferences(
        self,
        resource_id: str,
        *,
        display_name: str,
        favorite: bool,
        expected_registry: str,
    ) -> str:
        name = " ".join(str(display_name or "").split())
        if len(name) > 160 or type(favorite) is not bool:
            raise ModelResourceError("Use a display name of at most 160 characters and a valid favorite choice.")
        with self.registry._writer():
            value, revision = self.registry.snapshot()
            if revision != expected_registry or resource_id not in value["resources"]:
                raise ModelResourceError("The model registry changed; reload before saving preferences.")
            record = value["resources"][resource_id]
            current = record.get("user", {})
            if not isinstance(current, dict):
                raise ModelResourceError("The model's user preferences are invalid; repair the registry first.")
            user = record.setdefault("user", {})
            if name:
                user["display_name"] = name
            else:
                user.pop("display_name", None)
            user["favorite"] = favorite
            self.registry._save(value, revision)
        return self.registry.snapshot()[1]

    def import_favorite(self, resource_id, *, expected_registry, expected_favorite, marker):
        """Apply one explicitly reviewed favorite, preserving all other user fields."""
        from .swarm_onboarding_receipts import validate_marker, validate_markers
        marker = validate_marker(marker)
        if (marker["after"] is not True or expected_favorite is not None and type(expected_favorite) is not bool
                or marker["category"] != "favorites" or marker["destination"] != resource_id or marker["before"] is not expected_favorite):
            raise ModelResourceError("Review an explicit source favorite before importing.")
        with self.registry._writer():
            value, revision = self.registry.snapshot()
            if revision != expected_registry or resource_id not in value["resources"]:
                raise ModelResourceError("The model registry changed; review favorites again.")
            record = value["resources"][resource_id]
            user = record.get("user", {})
            if not isinstance(user, dict) or user.get("favorite") != expected_favorite:
                raise ModelResourceError("The local favorite changed after review.")
            history = validate_markers(record.get("swarm_favorite_imports", []))
            prior = next((row for row in history if row["operation_id"] == marker["operation_id"]), None)
            if prior is not None:
                if prior != marker: raise ModelResourceError("The favorite import marker changed.")
                if user.get("favorite") is not True: raise ModelResourceError("A later local correction is preserved; make a fresh import review.")
                return revision
            if len(history) >= 128: raise ModelResourceError("Favorite import history needs review before adding another entry.")
            record.setdefault("user", {})["favorite"] = True
            record["swarm_favorite_imports"] = history + [marker]
            self.registry._save(value, revision)
        return self.registry.snapshot()[1]

    def verify_and_record(
        self,
        resource_id: str,
        role: str,
        family: str,
        *,
        expected_registry: str,
        progress=None,
        should_cancel=None,
    ) -> VerifiedModelSelection:
        if role not in RESOURCE_ROLES or not str(family or "").strip():
            raise ModelResourceError("Choose an explicit resource role and family.")
        snapshot, revision = self.registry.snapshot()
        if revision != expected_registry:
            raise ModelResourceError("The model registry changed; reload before using this selection.")
        record = snapshot["resources"].get(resource_id)
        if record is None:
            raise ModelResourceError("The selected model is no longer registered.")
        if not any(
            item.get("role") == role
            and str(item.get("family", "")).casefold() == family.casefold()
            for item in record["declarations"]
        ):
            raise ModelResourceError("The selected role and family declaration changed; reload the selector.")
        # Reject every protected overlap before registry.resolve can inspect any
        # registered location. Selection never falls through to another path.
        for location in record["locations"]:
            _path(location.get("path", ""), self.protected_roots)
        component_id = ""
        resolution_options = {}
        if record["identity"].get("kind") == "managed_model_pack":
            component_id = record["identity"]["model_id"]
            if self.component_source is not None:
                target = self.component_source.target(component_id)
                if not any(Path(row["path"]) == target for row in record["locations"]):
                    raise ModelResourceError("The managed component location changed; inspect its current installation.")
                resolution_options["location_path"] = target
        resolution = self.registry.resolve(
            resource_id,
            role=role,
            family=family,
            progress=progress,
            should_cancel=should_cancel,
            **resolution_options,
        )
        if resolution.status == "unavailable" and resource_id.startswith("sha256:"):
            raise ModelLocationUnavailable(resolution.reason or "No exact local copy is available.")
        if resolution.status != "identity_verified" or resolution.inspection is None:
            raise ModelResourceError(resolution.reason or "The selected model identity could not be verified.")
        with self.registry._writer():
            value, current = self.registry.snapshot()
            if current != expected_registry or resource_id not in value["resources"]:
                raise ModelResourceError("The model registry changed during verification; reload and choose again.")
            user = value["resources"][resource_id].setdefault("user", {})
            if not isinstance(user, dict):
                raise ModelResourceError("The model's user preferences are invalid; repair the registry first.")
            count = user.get("use_count", 0)
            user.update(
                last_used_at=datetime.now(timezone.utc).isoformat(),
                last_used_role=role,
                last_used_family=family,
                use_count=(count if type(count) is int and count >= 0 else 0) + 1,
            )
            _cancel(should_cancel)
            _current(resolution.inspection)
            if component_id and self.component_source is not None and self.component_source.target(component_id) != resolution.inspection.path:
                raise ModelResourceError("The managed component location changed during verification.")
            self.registry._save(value, current)
        return VerifiedModelSelection(resource_id, role, family, str(resolution.inspection.path), component_id)

    @staticmethod
    def summarize(
        selected_resources: Iterable[object] = (),
        *,
        required_roles: Iterable[str] = (),
        candidate: object | None = None,
    ) -> ModelSelectionSummary:
        selections = [_selection(item) for item in selected_resources]
        if candidate is not None:
            current = _selection(candidate)
            selections = [item for item in selections if item[0] != current[0]]
            selections.append(current)
        selections = list(dict.fromkeys(selections))
        required = tuple(dict.fromkeys(str(item) for item in required_roles if str(item)))
        unknown = tuple(item for item in required if item not in RESOURCE_ROLES)
        if unknown:
            raise ModelResourceError("Required companion roles must use supported resource roles.")
        present = {item[1] for item in selections}
        missing = tuple(item for item in required if item not in present)
        families = tuple(sorted({item[2] for item in selections if item[2]}, key=str.casefold))
        conflicts = families if len(families) > 1 else ()
        if missing:
            message = "Missing selected companion roles: " + ", ".join(
                item.replace("_", " ") for item in missing) + "."
        elif conflicts:
            message = "Selected resources declare different families; the exact adapter must resolve compatibility."
        elif selections:
            message = "Selected identities are stored choices; exact adapter compatibility and run readiness still require preflight."
        else:
            message = "No model resources are selected."
        return ModelSelectionSummary(
            selections=tuple(selections),
            required_roles=required,
            missing_roles=missing,
            conflicting_families=conflicts,
            compatibility="unknown",
            ready_to_run=False,
            message=message,
        )
