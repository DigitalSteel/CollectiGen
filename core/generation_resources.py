"""Read-only, exact cached-tensor discovery for a local SwarmUI model list.

Discovery never hashes a Swarm model, reads model files, or selects a model for
the user. The execution adapter still binds and rechecks one chosen candidate.
"""
from __future__ import annotations

import re


SWARM_SUBTYPES = {
    "checkpoint": "Stable-Diffusion",
    "lora": "LoRA",
    "vae": "VAE",
    "text_encoder": "Clip",
    "controlnet": "ControlNet",
    "control_model": "ControlNet",
}
MAX_MODEL_ROWS = 20_000
MAX_NAME_LENGTH = 1024
_SHA256 = re.compile(r"(?:0x)?[0-9a-fA-F]{64}\Z")


def _sha256(value):
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        return ""
    return value.lower().removeprefix("0x")


def _name(value):
    if (not isinstance(value, str) or not value or len(value) > MAX_NAME_LENGTH
            or value.startswith(("/", "\\")) or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("SwarmUI returned an invalid model name.")
    return value


def discover_swarm_models(transport, session_id: str, selected_tensor_sha256: str,
                          *, role: str = "checkpoint") -> tuple[dict[str, object], ...]:
    """Find local model names whose *cached* tensor SHA equals the selected SHA.

    Distinct matching names remain distinct candidates. No filename, title, or
    compatibility-class heuristic can substitute for the cached tensor hash.
    """
    if role not in SWARM_SUBTYPES:
        raise ValueError("Choose a supported Swarm model category.")
    target = _sha256(selected_tensor_sha256)
    if not target:
        raise ValueError("Provide the selected model's exact tensor SHA-256.")
    if (not isinstance(session_id, str) or not session_id or len(session_id) > 512
            or any(ord(char) < 32 or ord(char) == 127 for char in session_id)):
        raise ValueError("Check the local generator session first.")
    subtype = SWARM_SUBTYPES[role]
    result = transport.post("ListModels", {
        "session_id": session_id, "path": "", "depth": 20,
        "subtype": subtype, "allowRemote": False, "dataImages": False,
    })
    if not isinstance(result, dict) or result.get("error") or result.get("error_id"):
        raise ValueError("SwarmUI could not list local models for this category.")
    files = result.get("files")
    folders = result.get("folders")
    if (not isinstance(files, list) or len(files) > MAX_MODEL_ROWS
            or not isinstance(folders, list) or len(folders) > MAX_MODEL_ROWS):
        raise ValueError("SwarmUI returned an unsupported model list size or shape.")
    found = {}
    for row in files:
        if not isinstance(row, dict):
            raise ValueError("SwarmUI returned a malformed model entry.")
        name = _name(row.get("name"))
        if row.get("local") is not True:
            continue
        if row.get("is_supported_model_format") is not True:
            continue
        first = _sha256(row.get("hash_sha256"))
        second = _sha256(row.get("hash"))
        if ((row.get("hash_sha256") not in (None, "") and not first)
                or (row.get("hash") not in (None, "") and not second)):
            continue
        if first and second and first != second:
            raise ValueError("SwarmUI returned conflicting cached tensor hashes.")
        cached = first or second
        if cached != target:
            continue
        family = row.get("compat_class")
        if family is not None and (not isinstance(family, str) or len(family) > 256):
            raise ValueError("SwarmUI returned malformed model compatibility data.")
        candidate = {"name": name, "role": role, "subtype": subtype,
                     "cached_tensor_sha256": cached, "compat_class": family,
                     "local": True, "is_supported_model_format": True}
        if name in found and found[name] != candidate:
            raise ValueError("SwarmUI returned conflicting identities for one model name.")
        found[name] = candidate
    return tuple(found[name] for name in sorted(found))
