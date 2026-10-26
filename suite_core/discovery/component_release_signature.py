"""Component admission extension of the existing exact-byte release verifier.

Standard library only. No default trust, key enrollment, network, installation
or filesystem lookup. Caller-supplied public trust uses the main app's schema.
"""
import base64
from dataclasses import dataclass
import json
import re

from release_contract import (
    ReleaseContractError, ReleaseArtifact, SIGNATURE_ALGORITHM, SemVer,
    verify_release_manifest, verify_artifact,
)
from suite_core.installation import ComponentId

ADAPTER_VERSION = "0.1.0-alpha.1"
MAX_METADATA_BYTES = 128 * 1024
ARTIFACT_KIND = "component-package"  # never the main app portable kind
HASH = re.compile(r"[0-9a-f]{64}\Z")


def _require(ok, reason):
    if not ok:
        raise ReleaseContractError(reason)


def _json(raw, label):
    return _bounded_json(raw, label, MAX_METADATA_BYTES)


def inventory_json(raw, *, max_bytes=MAX_METADATA_BYTES):
    """Inventory-only enlarged bound; retain the strict shared parser."""
    _require(type(max_bytes) is int and max_bytes in (MAX_METADATA_BYTES, 1024 * 1024), "inventory bound")
    return _bounded_json(raw, "component inventory", max_bytes)


def _bounded_json(raw, label, max_bytes):
    _require(type(raw) is bytes and 0 < len(raw) <= max_bytes, label + " size")
    # Bound nesting before json.loads, including tiny deeply nested inputs.
    depth = 0
    quoted = escaped = False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            _require(depth <= 16, label + " nesting")
        elif byte in (93, 125):
            depth -= 1
            _require(depth >= 0, label + " syntax")
    def pairs(rows):
        result = {}
        for key, value in rows:
            _require(key not in result, label + " duplicate key")
            result[key] = value
        return result
    def constant(_):
        raise ReleaseContractError(label + " nonfinite value")
    try:
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs, parse_constant=constant)
    except ReleaseContractError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ReleaseContractError(label + " syntax") from exc
    _require(type(value) is dict, label + " object required")
    return value


def _version(value):
    _require(type(value) is str and value.isascii() and len(value) <= 96 and value == value.strip() and "+" not in value, "exact version")
    return SemVer.parse(value)


def _text(value, label, maximum=255):
    _require(type(value) is str and 0 < len(value) <= maximum
             and all(32 <= ord(c) < 127 for c in value), label)


def _trust(raw):
    trust = _json(raw, "trust")
    _require(set(trust) == {"schema_version", "keys"} and type(trust["schema_version"]) is int
             and trust["schema_version"] == 1, "trust schema")
    keys = trust["keys"]
    _require(type(keys) is list and len(keys) <= 16, "trust keys")
    seen = set()
    for key in keys:
        _require(type(key) is dict and set(key) in (
            {"key_id", "algorithm", "modulus", "exponent"},
            {"key_id", "algorithm", "modulus", "exponent", "status"}), "trust key schema")
        _text(key["key_id"], "key id", 128)
        _require(key["key_id"] not in seen, "duplicate trust key")
        seen.add(key["key_id"])
        _require(key["algorithm"] == SIGNATURE_ALGORITHM, "trust algorithm")
        _require(type(key["exponent"]) is int and 3 <= key["exponent"] <= 2**32 - 1
                 and key["exponent"] % 2 == 1, "public exponent")
        _require(type(key.get("status", "active")) is str, "key status")
        modulus = key["modulus"]
        _require(type(modulus) is str and 342 <= len(modulus) <= 1366
                 and re.fullmatch(r"[A-Za-z0-9_-]+", modulus), "public modulus")
        try:
            decoded = base64.b64decode(modulus + "=" * (-len(modulus) % 4), altchars=b"-_", validate=True)
        except ValueError as exc:
            raise ReleaseContractError("public modulus") from exc
        _require(base64.urlsafe_b64encode(decoded).decode().rstrip("=") == modulus
                 and 2048 <= int.from_bytes(decoded, "big").bit_length() <= 8192, "public modulus")
    return trust


@dataclass(frozen=True)
class VerifiedComponentRelease:
    component_id: ComponentId
    software_version: str
    channel: str
    os: str
    architecture: str
    artifact: ReleaseArtifact
    key_id: str
    manifest_sha256: str

    @property
    def production_install_authorized(self):
        return False


def verify_component_release(manifest_bytes, signature_bytes, trust_bytes, *,
                             expected_component, expected_version, expected_channel,
                             expected_os, expected_architecture):
    """Verify exact signed identity/platform/artifact metadata, not installation.

    The signature envelope, trust schema, algorithm and raw-byte hashing are
    unchanged from release_contract/Sign-ReleaseManifest.ps1. Strict structural
    admission precedes that verifier. No fixture-only algorithm or test bypass.
    Caller must bind expectations to the selected offer and actual target.
    """
    _require(type(expected_component) is ComponentId, "component expectation")
    _version(expected_version)
    _require(expected_channel in ("stable", "beta"), "channel expectation")
    _require(expected_os in ("linux", "windows") and expected_architecture in ("x86_64", "aarch64"), "platform expectation")
    manifest = _json(manifest_bytes, "manifest")
    signature = _json(signature_bytes, "signature")
    _require(set(manifest) == {"schema_version", "product", "version", "channel", "published_at",
        "supported_from", "release_notes", "artifacts", "component_id", "platform"}, "component manifest schema")
    _require(type(manifest["schema_version"]) is int and manifest["schema_version"] == 1
             and manifest["product"] == "collectigen", "component manifest schema")
    _require(manifest["component_id"] == expected_component.value and type(manifest["component_id"]) is str, "component binding")
    version = _version(manifest["version"])
    supported = _version(manifest["supported_from"])
    _require(manifest["version"] == expected_version and supported.compare(version) <= 0, "version binding")
    _require(manifest["channel"] == expected_channel and type(manifest["channel"]) is str
             and bool(version.prerelease) == (expected_channel == "beta"), "channel binding")
    platform = manifest["platform"]
    _require(type(platform) is dict and set(platform) == {"os", "architecture"}
             and platform == {"os": expected_os, "architecture": expected_architecture}, "platform binding")
    _text(manifest["published_at"], "publication date", 64)
    _require(type(manifest["release_notes"]) is str and 0 < len(manifest["release_notes"].strip()) <= 100000, "release notes")
    artifacts = manifest["artifacts"]
    _require(type(artifacts) is list and len(artifacts) == 1 and type(artifacts[0]) is dict, "one artifact required")
    a = artifacts[0]
    _require(set(a) == {"name", "kind", "sha256", "size_bytes", "portable_manifest_sha256"}, "artifact schema")
    _text(a["name"], "artifact name")
    _require(a["name"] not in (".", "..") and not any(c in a["name"] for c in "/\\"), "artifact basename")
    _require(a["kind"] == ARTIFACT_KIND, "component artifact kind")
    _require(type(a["size_bytes"]) is int and 0 < a["size_bytes"] <= 2**40, "artifact size")
    _require(all(type(a[k]) is str and HASH.fullmatch(a[k]) for k in ("sha256", "portable_manifest_sha256")), "artifact digest")
    _require(set(signature) == {"schema_version", "algorithm", "key_id", "manifest_sha256", "signature"}
             and type(signature["schema_version"]) is int and signature["schema_version"] == 1, "signature schema")
    _text(signature["key_id"], "signature key id", 128)
    _require(type(signature["manifest_sha256"]) is str and HASH.fullmatch(signature["manifest_sha256"]), "signature digest")
    _require(type(signature["signature"]) is str and len(signature["signature"]) <= 1400, "signature size")
    trust = _trust(trust_bytes)
    verified = verify_release_manifest(manifest_bytes, signature_bytes, trust, expected_channel=expected_channel)
    return VerifiedComponentRelease(expected_component, verified.version, verified.channel,
        expected_os, expected_architecture, verified.artifacts[0], verified.key_id, verified.manifest_sha256)


def verify_component_artifact(path, release):
    """Read/hash chosen artifact via existing verifier; no extraction or writes.

    A caller-constructed DTO is not signature evidence; call this only with the
    verifier result. Durable pinning/revalidation remains an activation gate.
    """
    _require(type(release) is VerifiedComponentRelease, "verified component result required")
    verify_artifact(path, release.artifact)
