"""Pure reviewed-analysis prompt drafts. No image reads, inference, stores or Qt.

Profiles are dated formatting advice for explicit checkpoints, not runtime admission.
Original metadata and reviewed inference remain separate, retained authorities.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re

SCHEMA_VERSION = 1
MAX_BYTES = 256 * 1024
MAX_PROMPT = 16384
KINDS = {"character", "outfit", "scene", "pose", "style", "other"}
SHA = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class PromptProfile:
    id: str
    version: int
    model_id: str
    variant: str
    layout: str
    negative: str
    adapter_scope: str
    guidance: tuple[str, ...]
    sources: tuple[str, ...]
    reviewed_on: str = "2026-10-11"
    override_checkpoint_sha256: str = ""


SDXL_CARD = "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0"
SDXL_API = "https://huggingface.co/docs/diffusers/en/api/pipelines/stable_diffusion/stable_diffusion_xl"
ANIMA = "https://huggingface.co/circlestone-labs/Anima"
KREA_PROMPT = "https://github.com/krea-ai/krea-2/blob/main/docs/prompting.md"
KREA_CODE = "https://github.com/krea-ai/krea-2/blob/main/inference.py"
KREA_DIFFUSERS = "https://github.com/huggingface/diffusers/blob/main/src/diffusers/pipelines/krea2/pipeline_krea2.py"
ZBASE = "https://huggingface.co/Tongyi-MAI/Z-Image"
ZTURBO = "https://huggingface.co/Tongyi-MAI/Z-Image-Turbo"

PROFILES = (
    PromptProfile("sdxl-base-1.0", 1, "stabilityai/stable-diffusion-xl-base-1.0", "base-1.0",
        "phrases", "conditional", "diffusers-sdxl",
        ("Preserve chosen phrases; no universal SDXL quality prefix is established.",
         "Negatives are ignored without effective guidance. Fine-tunes need their own profile."),
        (SDXL_CARD, SDXL_API)),
    PromptProfile("anima-base", 1, "circlestone-labs/Anima", "base", "phrases", "supported", "model-card",
        ("Base guidance recommends lowercase, spaced tags and grouped tag order; edit deliberately.",
         "Quality/score prefixes are optional author choices, never inserted by this renderer.",
         "This profile is variant guidance; the selected file hash must be bound by the caller."), (ANIMA,)),
    PromptProfile("anima-aesthetic", 1, "circlestone-labs/Anima", "aesthetic", "phrases", "supported", "model-card",
        ("Aesthetic guidance does not require quality tags and advises against score tags.",
         "Existing authored tags are retained exactly; changes require explicit editing.",
         "1.0 and 1.0b are distinct checkpoints; bind the exact selected file hash."), (ANIMA,)),
    PromptProfile("krea2-raw", 1, "krea/Krea-2-Raw", "raw", "paragraphs", "unknown", "unresolved",
        ("Official prompting guidance prefers detailed natural language; preserve quoted text.",
         "RAW uses guidance, but the official CLI has no negative argument.",
         "Diffusers has guidance-dependent negatives; choose an evidenced adapter-specific override."),
        (KREA_PROMPT, KREA_CODE, KREA_DIFFUSERS)),
    PromptProfile("krea2-turbo", 1, "krea/Krea-2-Turbo", "turbo", "paragraphs", "unknown", "unresolved",
        ("Use natural-language description; recommended Turbo inference disables guidance.",
         "Negative support differs by adapter; no family-wide capability is assumed."),
        (KREA_PROMPT, "https://github.com/krea-ai/krea-2/blob/main/README.md", KREA_DIFFUSERS)),
    PromptProfile("zimage-base", 1, "Tongyi-MAI/Z-Image", "base", "paragraphs", "conditional", "diffusers-zimage",
        ("Base examples use descriptive language and optional negative text with guidance.",
         "This is not a Turbo, Omni or Edit profile."), (ZBASE,)),
    PromptProfile("zimage-turbo", 1, "Tongyi-MAI/Z-Image-Turbo", "turbo", "paragraphs", "unsupported", "documented-turbo",
        ("Turbo guidance is disabled; the base model card marks Turbo negatives unsupported.",
         "Negative draft text stays retained; it is never folded into the positive prompt."), (ZBASE, ZTURBO)),
)


def _json_copy(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
        if len(raw) > MAX_BYTES:
            raise ValueError("Prompt draft exceeds its retained JSON limit.")
        return json.loads(raw)
    except (TypeError, OverflowError, RecursionError) as exc:
        raise ValueError("Prompt draft needs bounded JSON values.") from exc


def _text(value):
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_PROMPT or "\x00" in value:
        raise ValueError("Prompt text must be bounded Unicode without NUL.")
    return value


def _target(target):
    value = _json_copy(target)
    if not isinstance(value, dict):
        raise ValueError("Choose a target or explicit guide-only projection.")
    guide_only = value.get("mode") == "guide-only"
    if guide_only:
        if value.get("checkpoint_sha256") or value.get("adapter_id") != "manual-guide-only":
            raise ValueError("Guide-only advice has no checkpoint hash or executable adapter.")
    elif not SHA.fullmatch(str(value.get("checkpoint_sha256", ""))):
        raise ValueError("Bind an exact checkpoint SHA256; family names alone are insufficient.")
    for field in ("model_id", "variant", "adapter_id"):
        if not isinstance(value.get(field), str) or not value[field] or len(value[field]) > 256:
            raise ValueError("Target requires model, variant and adapter identities.")
    return value


def _profile(profile_id, target, override):
    profile = next((p for p in PROFILES if p.id == profile_id), None)
    if profile is None:
        raise ValueError("Unknown prompt profile; choose explicit checkpoint guidance.")
    if override is not None:
        if not isinstance(override, PromptProfile) or override.id != profile.id:
            raise ValueError("Override must retain its base profile identity.")
        if type(override.version) is not int or override.version <= profile.version or override.override_checkpoint_sha256 != target.get("checkpoint_sha256"):
            raise ValueError("Override needs a new version bound to this exact checkpoint.")
        profile = override
    if (profile.model_id, profile.variant) != (target["model_id"], target["variant"]):
        raise ValueError("Profile does not match this checkpoint; review an exact override.")
    if (profile.layout not in {"phrases", "paragraphs"} or profile.negative not in
            {"supported", "conditional", "unsupported", "unknown"} or not profile.sources
            or any(not isinstance(s, str) or not s.startswith("https://") for s in profile.sources)):
        raise ValueError("Invalid profile rules or missing evidence.")
    return profile


def _parts(analysis):
    if (not isinstance(analysis, dict) or type(analysis.get("schema_version")) is not int
            or analysis["schema_version"] != 1 or not SHA.fullmatch(str(analysis.get("image_sha256", "")))
            or not isinstance(analysis.get("analysis_model_id"), str) or not analysis["analysis_model_id"]):
        raise ValueError("Retained analysis needs a supported schema, exact image hash and analysis attribution.")
    parts = analysis.get("parts")
    if not isinstance(parts, list) or len(parts) > 32:
        raise ValueError("Retain up to 32 reviewed prompt parts.")
    selected = []
    ids = set()
    for part in parts:
        if not isinstance(part, dict) or not isinstance(part.get("id"), str) or not part["id"] or part["id"] in ids:
            raise ValueError("Prompt parts need distinct retained identities.")
        ids.add(part["id"])
        if not isinstance(part.get("kind"), str) or not isinstance(part.get("origin"), str) or part["kind"] not in KINDS or part["origin"] not in {"inferred", "reviewed_image_knowledge", "user_authored"}:
            raise ValueError("Prompt parts need explicit kind and evidence origin.")
        if type(part.get("selected")) is not bool or type(part.get("reviewed")) is not bool:
            raise ValueError("Selection and review must be explicit booleans.")
        _text(part.get("text"))
        if part["selected"]:
            if not part["reviewed"] or not part["text"].strip():
                raise ValueError("Only explicitly reviewed nonempty parts can enter a prompt.")
            selected.append(part["text"])
    return selected


def render_prompt(analysis, target, profile_id, *, edits=None, recorded_metadata=None,
                  guidance_active=None, override=None, profile_selections=None):
    """Pure target projection; inputs/edits/raw metadata are never mutated or analyzed."""
    analysis = _json_copy(analysis)
    parts = _parts(analysis)
    target = _target(target)
    selections = _json_copy({} if profile_selections is None else profile_selections)
    if not isinstance(selections, dict):
        raise ValueError("Retained profile selections must be a per-target map.")
    binding = target.get('checkpoint_sha256') or 'guide-only'
    selection_key = f"{profile_id}:{binding}:{target['adapter_id']}"
    retained = selections.get(selection_key)
    if override is None and retained is not None:
        try:
            override = PromptProfile(**retained["versions"][str(retained["selected_version"])])
        except (KeyError, TypeError) as exc:
            raise ValueError("Retained profile selection is invalid.") from exc
    profile = _profile(profile_id, target, override)
    if override is not None:
        retained = selections.setdefault(selection_key, {"selected_version": profile.version, "versions": {}})
        if not isinstance(retained, dict) or not isinstance(retained.get("versions"), dict):
            raise ValueError("Retained profile versions are invalid.")
        existing = retained["versions"].get(str(profile.version))
        profile_value = _json_copy(asdict(profile))
        if existing is not None and existing != profile_value:
            raise ValueError("A retained profile version cannot change rules; choose a new version.")
        retained["selected_version"] = profile.version
        retained["versions"][str(profile.version)] = profile_value
    if guidance_active is not None and type(guidance_active) is not bool:
        raise ValueError("Effective guidance must be explicitly assessed or unknown.")
    edits = _json_copy({} if edits is None else edits)
    if not isinstance(edits, dict):
        raise ValueError("Edits must be a per-target map.")
    for values in edits.values():
        if not isinstance(values, dict) or set(values) - {"positive", "negative"}:
            raise ValueError("Edits may contain only positive and negative text.")
        for value in values.values():
            _text(value)
    key = f"{profile.id}@{profile.version}:{binding}:{target['adapter_id']}"
    authored = edits.get(key, {})
    positive = _text(authored.get("positive", (", " if profile.layout == "phrases" else "\n\n").join(parts)))
    negative = _text(authored.get("negative", ""))
    # Upstream model documentation does not establish runtime adapter behavior.
    adapter_matches = (profile.adapter_scope not in {"model-card", "documented-turbo", "unresolved"}
                       and profile.adapter_scope == target["adapter_id"])
    capability = profile.negative if adapter_matches else "unknown"
    usable = bool(target.get("checkpoint_sha256")) and capability in {"supported", "conditional"} and guidance_active is True
    result = dict(schema_version=1, profile=asdict(profile), target=target, edit_key=key,
        analysis=analysis, analysis_digest=sha256(json.dumps(analysis, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        recorded_metadata=_json_copy(recorded_metadata), edits=edits, profile_selections=selections,
        outputs={"positive": positive, "negative": negative}, negative_capability=capability,
        negative_model_support=profile.negative, negative_execution_eligible=usable,
        negative_usable=usable, guidance_active=guidance_active, authority="inferred_prompt_draft_not_recorded_metadata")
    return _json_copy(result)


def _draft(draft):
    value = _json_copy(draft)
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != SCHEMA_VERSION
            or value.get("authority") != "inferred_prompt_draft_not_recorded_metadata"
            or not isinstance(value.get("edit_key"), str) or not value["edit_key"]
            or not isinstance(value.get("edits"), dict)
            or not isinstance(value.get("outputs"), dict)
            or type(value.get("negative_usable")) is not bool):
        raise ValueError("Choose a supported retained prompt draft.")
    for side in ("positive", "negative"):
        _text(value["outputs"].get(side))
    _parts(value.get("analysis"))
    _target(value.get("target"))
    return value


def edit_output(draft, side, text):
    """Return an edited copy; an empty string deliberately clears that side."""
    if side not in {"positive", "negative"}:
        raise ValueError("Choose positive or negative prompt text.")
    result = _draft(draft)
    result["edits"].setdefault(result["edit_key"], {})[side] = _text(text)
    result["outputs"][side] = text
    return result


def switch_target(draft, target, profile_id, *, guidance_active=None, override=None):
    draft = _draft(draft)
    return render_prompt(draft["analysis"], target, profile_id, edits=draft["edits"],
        recorded_metadata=draft["recorded_metadata"], guidance_active=guidance_active, override=override,
        profile_selections=draft.get("profile_selections", {}))


def text_for_copy(draft, side="positive"):
    draft = _draft(draft)
    if side not in {"positive", "negative"}:
        raise ValueError("Choose positive or negative prompt text.")
    return _text(draft["outputs"][side])


def plain_text_export(draft):
    """Return UTF8 bytes for a previewed text artifact; the existing writer owns save."""
    positive = text_for_copy(draft)
    negative = text_for_copy(draft, "negative")
    note = ("\nManual transfer warning: negative execution eligibility is unestablished or inactive.\n"
            if not draft["negative_execution_eligible"] else "")
    return ("Positive prompt:\n" + positive + "\n" +
            ("\nNegative prompt:\n" + negative + "\n" if negative else "") + note).encode("utf-8")
