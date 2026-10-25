"""Bounded offline suggestions; exact identity never implies compatibility."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json

from .model_organization import family_options, normalize_model_family
from .model_resource_registry import MAX_HEADER_BYTES, RESOURCE_ROLES, ModelResourceError, _cancel

MAX_FILES = 20_000
MAX_CLAIMS = 200_000
MAX_RELATED = 64
FAMILIES = frozenset((*family_options("Images"), *family_options("Video")))
# Exact identifiers qualified against primary specifications, 2026-09-18.
# https://github.com/Stability-AI/ModelSpec (1.0.1, Architecture ID Listing)
MODELSPEC_SOURCE = "https://github.com/Stability-AI/ModelSpec"
SDXL_ARCHITECTURES = frozenset(("stable-diffusion-xl-v1", "stable-diffusion-xl-v1-base",
    "stable-diffusion-xl-v1-refiner", "stable-diffusion-xl-v1-edit", "stable-diffusion-xl-turbo-v1"))
MODELSPEC_COMPONENTS = {"lora": "lora", "vae": "vae", "textual-inversion": "embedding",
    "controlnet": "control_model", "control-lora": "control_model"}
ANIMA_LLLITE_SOURCE = "https://github.com/kohya-ss/sd-scripts/blob/main/docs/anima_train_control_net_lllite.md"


@dataclass(frozen=True)
class HeaderSuggestions:
    header_sha256: str
    roles: tuple[str, ...]
    families: tuple[str, ...]
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class ModelDetection:
    resource_id: str
    role: str
    family: str
    status: str
    evidence: tuple[str, ...]
    scanned_copies: tuple[str, ...]
    recorded_copies: tuple[str, ...]
    related_versions: tuple[str, ...]
    version_scope_complete: bool = True

    @property
    def duplicate(self):
        return len({path.casefold() for path in (*self.scanned_copies, *self.recorded_copies)}) > 1


def _family(value):
    normalized = normalize_model_family(value)
    return normalized if normalized in FAMILIES else ""


def _role(value):
    value = str(value or "").strip().casefold()
    return value if value in RESOURCE_ROLES else ""


def _architecture(value):
    base, _, component = value.strip().casefold().partition("/")
    if base in SDXL_ARCHITECTURES:
        # An unfamiliar component still establishes the declared base family,
        # but cannot establish a type. A bare base ID does not prove a full pack.
        return "SDXL", MODELSPEC_COMPONENTS.get(component, ""), MODELSPEC_SOURCE
    if value.strip().casefold() == "anima-preview/control-net-lllite":
        return "Anima", "control_model", ANIMA_LLLITE_SOURCE
    return _family(value), "", ""


def inspect_header_suggestions(raw: bytes) -> HeaderSuggestions:
    """Consume only the existing inspector's validated bounded header observer.

    Metadata claims are attributed, not architectural proof. The only tensor
    role signature used is a complete matching LoRA up/down matrix pair.
    Family is never guessed from tensor dimensions, filenames or folders.
    """
    if not isinstance(raw, bytes) or len(raw) > MAX_HEADER_BYTES:
        raise ModelResourceError("Detection requires a bounded validated tensor header.")
    value = json.loads(raw)
    if not isinstance(value, dict): raise ModelResourceError("Invalid tensor header for detection.")
    metadata = value.get("__metadata__", {})
    roles, families, evidence = set(), set(), []
    for key in ("modelspec.architecture", "ss_base_model_version", "modelspec.base_model"):
        claim = metadata.get(key)
        if isinstance(claim, str) and claim:
            family, role, source = _architecture(claim) if key == "modelspec.architecture" else (_family(claim), "", "")
            if family: families.add(family)
            if role: roles.add(role)
            evidence.append(f"Embedded {key}: {claim[:300]}" + (" (recognized family claim)" if family else " (unrecognized family; review manually)"))
            if source: evidence.append(f"Identifier mapping: {source}; embedded claims require review and do not prove executable compatibility.")
    for key in ("modelspec.role", "model_type"):
        claim = metadata.get(key)
        if isinstance(claim, str) and claim:
            role = _role(claim)
            if role: roles.add(role)
            evidence.append(f"Embedded {key}: {claim[:160]}" + (" (recognized role claim)" if role else " (unrecognized role)"))
    pairs = 0
    for name, tensor in value.items():
        if not name.endswith(".lora_down.weight") or not isinstance(tensor, dict): continue
        up = value.get(name[:-len(".lora_down.weight")] + ".lora_up.weight")
        down_shape = tensor.get("shape")
        up_shape = up.get("shape") if isinstance(up, dict) else None
        if isinstance(down_shape, list) and isinstance(up_shape, list) and len(down_shape) == len(up_shape) == 2 and down_shape[0] == up_shape[1] and down_shape[0] > 0:
            pairs += 1
    if pairs:
        roles.add("lora")
        evidence.append(f"Validated header contains {pairs:,} matching LoRA up/down matrix pairs; base family and backend compatibility remain unverified.")
    return HeaderSuggestions(hashlib.sha256(raw).hexdigest(), tuple(sorted(roles)), tuple(sorted(families)), tuple(evidence))


def _claims(record, identity):
    claims = record.get("provider_evidence", ())
    if not isinstance(claims, (list, tuple)): return
    for claim in claims[:100]:
        if not isinstance(claim, dict) or claim.get("authority") != "attributed_provider_evidence": continue
        item = claim.get("item", {})
        if not isinstance(item, dict) or not isinstance(claim.get("source"), dict) or not claim["source"].get("retrieved_at"): continue
        files = item.get("files", ())
        if not isinstance(files, (list, tuple)): continue
        if not any(isinstance(file, dict) and file.get("hash_scope") == "full_file" and "sha256:" + str(file.get("sha256", "")).lower() == identity for file in files[:1000]): continue
        if claim.get("provider") not in {"civitai", "hugging_face"}: continue
        yield claim


def _date(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else None
    except (ValueError, TypeError): return None


def review_model_files(inspections, snapshot, *, headers=None, should_cancel=None):
    """Project suggestions, exact copies and qualified provider relationships.

    Uses the already captured registry snapshot. Recorded paths are evidence
    only: this function never opens them or assumes they are still available.
    """
    inspections = tuple(inspections)
    records = snapshot.get("resources", {})
    if len(inspections) > MAX_FILES or len(records) > MAX_FILES:
        raise ModelResourceError("Detection scope exceeds 20,000 files or registered models.")
    headers = headers or {}
    scanned = defaultdict(list)
    for item in inspections:
        _cancel(should_cancel)
        scanned[item.resource_id].append(str(item.path))
    provider_groups, claims_by_identity = defaultdict(list), {}
    claims_seen, complete = 0, True
    for identity, record in records.items():
        _cancel(should_cancel)
        retained = record.get("provider_evidence", ())
        if isinstance(retained, (list, tuple)) and len(retained) > 100: complete = False
        claims = []
        for claim in _claims(record, identity):
            claims_seen += 1
            if claims_seen > MAX_CLAIMS:
                complete = False; break
            claims.append(claim)
            item = claim["item"]
            model_id = item.get("provider_model_id")
            version_id = item.get("provider_version_id", item.get("version_id"))
            if model_id is not None and version_id is not None:
                provider_groups[(claim["provider"], str(model_id))].append((identity, str(version_id), _date(item.get("created_at"))))
        claims_by_identity[identity] = claims
        if not complete: break
    results = {}
    for inspection in inspections:
        _cancel(should_cancel)
        identity = inspection.resource_id
        if identity in results: continue
        record = records.get(identity, {})
        roles, families, evidence, related = set(), set(), [], []
        header = headers.get(identity)
        if header:
            if header.header_sha256 != inspection.container.get("header_sha256"):
                raise ModelResourceError("Detection header no longer matches the exact inspected model.")
            roles.update(header.roles); families.update(header.families); evidence.extend(header.evidence)
        for declaration in record.get("declarations", ())[:100]:
            role, family = _role(declaration.get("role")), _family(declaration.get("family"))
            if role: roles.add(role)
            if family: families.add(family)
            evidence.append(f"Existing user declaration: {declaration.get('role', 'unknown')} / {declaration.get('family', 'unknown')} (not compatibility proof)")
        related_complete = complete
        for claim in claims_by_identity.get(identity, ()):
            item = claim["item"]
            role, family = _role(item.get("role")), _family(item.get("base_model"))
            if role: roles.add(role)
            if family: families.add(family)
            evidence.append(f"{claim['provider']} full-file match: {item.get('role') or 'unknown role'} / {item.get('base_model') or 'unknown family'}; retrieved {claim['source']['retrieved_at']}; {claim.get('currentness', 'unknown freshness')}")
            model_id = item.get("provider_model_id")
            version_id = str(item.get("provider_version_id", item.get("version_id")))
            when = _date(item.get("created_at"))
            for other, version, date in provider_groups.get((claim["provider"], str(model_id)), ()):
                if other == identity: continue
                if len(related) >= MAX_RELATED:
                    related_complete = False; break
                if version == version_id:
                    relation = "Different file or variant in the same provider version"
                elif when and date and when != date:
                    relation = "Earlier recorded provider version" if date < when else "Later recorded provider version"
                else: relation = "Related provider version; chronology unknown"
                related.append(f"{relation}: {claim['provider']} model {model_id}, version {version}, {other}. Precision, quantization and training may differ; this is not an upgrade recommendation.")
        status = "conflict" if len(roles) > 1 or len(families) > 1 else "suggested" if len(roles) == len(families) == 1 else "unknown"
        if not complete:
            status = "unknown"
            evidence.append("The saved-evidence budget was reached; partial evidence cannot authorize a detected assignment.")
        if not evidence: evidence.append("No recognized family or role evidence. Filenames and folder names are not identification authority.")
        recorded = tuple(str(item["path"]) for item in record.get("locations", ()) if isinstance(item, dict) and isinstance(item.get("path"), str))
        results[identity] = ModelDetection(identity, next(iter(roles)) if len(roles) == 1 else "",
            next(iter(families)) if len(families) == 1 else "", status, tuple(evidence),
            tuple(scanned[identity]), recorded, tuple(dict.fromkeys(related)), related_complete)
    _cancel(should_cancel)
    return results
