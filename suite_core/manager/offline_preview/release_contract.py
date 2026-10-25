from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


RELEASE_MANIFEST_SCHEMA = 1
RELEASE_SIGNATURE_SCHEMA = 1
PORTABLE_PACKAGE_SCHEMA = 2
PRODUCT_ID = "collectigen"
SIGNATURE_ALGORITHM = "rsa-pkcs1v15-sha256"
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MUTABLE_PREFIXES = (
    "data/user/", "data/cache/", "data/models/", "data/optional-components/",
    "data/update-state/",
    "training/", "output/", "logs/", "backups/", ".git/", ".venv/",
)


class ReleaseContractError(ValueError):
    pass


@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: str) -> "SemVer":
        match = _SEMVER.fullmatch(str(value).strip())
        if not match:
            raise ReleaseContractError(f"Invalid semantic version: {value!r}")
        prerelease = tuple((match.group(4) or "").split(".")) if match.group(4) else ()
        if any(part.isdigit() and len(part) > 1 and part.startswith("0") for part in prerelease):
            raise ReleaseContractError(f"Invalid semantic version: {value!r}")
        return cls(int(match.group(1)), int(match.group(2)), int(match.group(3)), prerelease)

    def _compare_prerelease(self, other: "SemVer") -> int:
        if not self.prerelease and not other.prerelease:
            return 0
        if not self.prerelease:
            return 1
        if not other.prerelease:
            return -1
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            left_number = left.isdigit()
            right_number = right.isdigit()
            if left_number and right_number:
                return -1 if int(left) < int(right) else 1
            if left_number != right_number:
                return -1 if left_number else 1
            return -1 if left < right else 1
        if len(self.prerelease) == len(other.prerelease):
            return 0
        return -1 if len(self.prerelease) < len(other.prerelease) else 1

    def compare(self, other: "SemVer") -> int:
        left = (self.major, self.minor, self.patch)
        right = (other.major, other.minor, other.patch)
        if left != right:
            return -1 if left < right else 1
        return self._compare_prerelease(other)


@dataclass(frozen=True)
class ReleaseArtifact:
    name: str
    kind: str
    sha256: str
    size_bytes: int
    portable_manifest_sha256: str


@dataclass(frozen=True)
class VerifiedReleaseManifest:
    version: str
    channel: str
    published_at: str
    release_notes: str
    supported_from: str
    artifacts: tuple[ReleaseArtifact, ...]
    key_id: str
    manifest_sha256: str
    payload: dict[str, Any]

    def artifact(self, kind: str) -> ReleaseArtifact:
        matches = [item for item in self.artifacts if item.kind == kind]
        if len(matches) != 1:
            raise ReleaseContractError(f"Release must contain one {kind!r} artifact.")
        return matches[0]


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _b64decode(value: object, label: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ReleaseContractError(f"{label} is missing.")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ReleaseContractError(f"{label} is not valid base64.") from exc


def _b64url_uint(value: object, label: str) -> int:
    if not isinstance(value, str) or not value:
        raise ReleaseContractError(f"{label} is missing.")
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(value + padding)
    except (ValueError, TypeError) as exc:
        raise ReleaseContractError(f"{label} is not valid base64url.") from exc
    if not decoded:
        raise ReleaseContractError(f"{label} is empty.")
    return int.from_bytes(decoded, "big")


def verify_rsa_pkcs1v15_sha256(message: bytes, signature: bytes, *, modulus: int, exponent: int) -> bool:
    size = (modulus.bit_length() + 7) // 8
    if size < 256 or len(signature) != size or exponent < 3 or exponent % 2 == 0:
        return False
    signature_number = int.from_bytes(signature, "big")
    if signature_number >= modulus:
        return False
    encoded = pow(signature_number, exponent, modulus).to_bytes(size, "big")
    digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    padding_length = size - len(digest_info) - 3
    if padding_length < 8:
        return False
    expected = b"\x00\x01" + (b"\xff" * padding_length) + b"\x00" + digest_info
    return hmac.compare_digest(encoded, expected)


def _load_json_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseContractError(f"{label} is not valid UTF-8 JSON.") from exc
    if not isinstance(payload, dict):
        raise ReleaseContractError(f"{label} must contain a JSON object.")
    return payload


def _safe_payload_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ReleaseContractError("Package inventory contains an invalid path.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseContractError(f"Package inventory path is unsafe: {value!r}")
    normalized = path.as_posix()
    lowered = normalized.casefold() + ("/" if normalized.endswith("/") else "")
    if any(lowered == prefix.rstrip("/") or lowered.startswith(prefix) for prefix in _MUTABLE_PREFIXES):
        raise ReleaseContractError(f"Package inventory includes mutable or development state: {normalized}")
    return normalized


def _validate_artifacts(values: object) -> tuple[ReleaseArtifact, ...]:
    if not isinstance(values, list) or not values:
        raise ReleaseContractError("Release manifest contains no artifacts.")
    artifacts: list[ReleaseArtifact] = []
    names: set[str] = set()
    kinds: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            raise ReleaseContractError("Release artifact entries must be objects.")
        name = str(value.get("name") or "")
        if not name or Path(name).name != name or "/" in name or "\\" in name:
            raise ReleaseContractError("Release artifact name is unsafe.")
        kind = str(value.get("kind") or "")
        digest = str(value.get("sha256") or "").casefold()
        portable_digest = str(value.get("portable_manifest_sha256") or "").casefold()
        try:
            size = int(value.get("size_bytes"))
        except (TypeError, ValueError) as exc:
            raise ReleaseContractError(f"Release artifact {name!r} has an invalid size.") from exc
        if name in names or not kind or kind in kinds:
            raise ReleaseContractError("Release artifact names and kinds must be unique.")
        if not _HEX_SHA256.fullmatch(digest) or not _HEX_SHA256.fullmatch(portable_digest):
            raise ReleaseContractError(f"Release artifact {name!r} has an invalid SHA-256 value.")
        if size < 1:
            raise ReleaseContractError(f"Release artifact {name!r} is empty.")
        names.add(name)
        kinds.add(kind)
        artifacts.append(ReleaseArtifact(name, kind, digest, size, portable_digest))
    return tuple(artifacts)


def verify_release_manifest(
    manifest_bytes: bytes,
    signature_bytes: bytes,
    trust_store: str | Path | dict[str, Any],
    *,
    expected_channel: str | None = None,
) -> VerifiedReleaseManifest:
    manifest = _load_json_object(manifest_bytes, "Release manifest")
    signature = _load_json_object(signature_bytes, "Release signature")
    if isinstance(trust_store, (str, Path)):
        trust = _load_json_object(Path(trust_store).read_bytes(), "Release trust store")
    elif isinstance(trust_store, dict):
        trust = trust_store
    else:
        raise ReleaseContractError("Release trust store is invalid.")
    if trust.get("schema_version") != 1:
        raise ReleaseContractError("Release trust store schema is unsupported.")
    if manifest.get("schema_version") != RELEASE_MANIFEST_SCHEMA or manifest.get("product") != PRODUCT_ID:
        raise ReleaseContractError("Release manifest schema or product is unsupported.")
    if signature.get("schema_version") != RELEASE_SIGNATURE_SCHEMA:
        raise ReleaseContractError("Release signature schema is unsupported.")
    if signature.get("algorithm") != SIGNATURE_ALGORITHM:
        raise ReleaseContractError("Release signature algorithm is unsupported.")
    key_id = str(signature.get("key_id") or "")
    keys = [value for value in trust.get("keys", []) if isinstance(value, dict)]
    keys = [value for value in keys if value.get("key_id") == key_id and value.get("status", "active") == "active"]
    if len(keys) != 1 or keys[0].get("algorithm") != SIGNATURE_ALGORITHM:
        raise ReleaseContractError("Release signature does not use an active trusted key.")
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    if not hmac.compare_digest(str(signature.get("manifest_sha256") or "").casefold(), digest):
        raise ReleaseContractError("Release manifest digest does not match its signature envelope.")
    key = keys[0]
    try:
        exponent = int(key.get("exponent", 0))
    except (TypeError, ValueError) as exc:
        raise ReleaseContractError("Release public exponent is invalid.") from exc
    if not verify_rsa_pkcs1v15_sha256(
        manifest_bytes,
        _b64decode(signature.get("signature"), "Release signature"),
        modulus=_b64url_uint(key.get("modulus"), "Release public modulus"),
        exponent=exponent,
    ):
        raise ReleaseContractError("Release manifest signature verification failed.")
    version = str(manifest.get("version") or "")
    SemVer.parse(version)
    channel = str(manifest.get("channel") or "")
    if channel not in {"stable", "beta"} or (expected_channel and channel != expected_channel):
        raise ReleaseContractError("Release manifest channel does not match the requested channel.")
    supported_from = str(manifest.get("supported_from") or "")
    SemVer.parse(supported_from)
    notes = str(manifest.get("release_notes") or "")
    if not notes.strip() or len(notes) > 100_000:
        raise ReleaseContractError("Release notes are missing or too large.")
    artifacts = _validate_artifacts(manifest.get("artifacts"))
    return VerifiedReleaseManifest(
        version=version,
        channel=channel,
        published_at=str(manifest.get("published_at") or ""),
        release_notes=notes,
        supported_from=supported_from,
        artifacts=artifacts,
        key_id=key_id,
        manifest_sha256=digest,
        payload=manifest,
    )


def ensure_supported_upgrade(current_version: str, release: VerifiedReleaseManifest) -> None:
    current = SemVer.parse(current_version)
    target = SemVer.parse(release.version)
    if target.compare(current) <= 0:
        raise ReleaseContractError(
            f"CollectiGen v{release.version} is not newer than the installed v{current_version}."
        )
    if current.compare(SemVer.parse(release.supported_from)) < 0:
        raise ReleaseContractError(
            f"This package supports automatic upgrades from v{release.supported_from} or newer."
        )


def verify_artifact(path: str | Path, artifact: ReleaseArtifact) -> None:
    source = Path(path)
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise ReleaseContractError(f"Release artifact is unavailable: {exc}") from exc
    if size != artifact.size_bytes:
        raise ReleaseContractError("Release artifact size does not match the signed manifest.")
    if not hmac.compare_digest(sha256_file(source), artifact.sha256):
        raise ReleaseContractError("Release artifact SHA-256 does not match the signed manifest.")


def verify_portable_package(
    package_root: str | Path,
    *,
    expected_version: str,
    expected_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    root = Path(package_root)
    manifest_path = root / "portable-package.json"
    raw = manifest_path.read_bytes()
    if expected_manifest_sha256 and not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(), expected_manifest_sha256.casefold()
    ):
        raise ReleaseContractError("Portable package manifest digest does not match the signed release.")
    payload = _load_json_object(raw, "Portable package manifest")
    if payload.get("schema_version") != PORTABLE_PACKAGE_SCHEMA:
        raise ReleaseContractError("Portable package schema is unsupported.")
    if payload.get("artifact") != "collectigen-portable-windows-x64":
        raise ReleaseContractError("Portable package target is unsupported.")
    if payload.get("application_version") != expected_version:
        raise ReleaseContractError("Portable package application version does not match the release.")
    inventory = payload.get("files")
    if not isinstance(inventory, list) or not inventory:
        raise ReleaseContractError("Portable package has no verified file inventory.")
    expected: set[str] = set()
    total = 0
    for item in inventory:
        if not isinstance(item, dict):
            raise ReleaseContractError("Portable package file entry is invalid.")
        relative = _safe_payload_path(item.get("path"))
        if relative in expected:
            raise ReleaseContractError(f"Portable package repeats {relative!r}.")
        digest = str(item.get("sha256") or "").casefold()
        try:
            size = int(item.get("size_bytes"))
        except (TypeError, ValueError) as exc:
            raise ReleaseContractError(f"Portable package size is invalid for {relative!r}.") from exc
        if not _HEX_SHA256.fullmatch(digest) or size < 0:
            raise ReleaseContractError(f"Portable package inventory is invalid for {relative!r}.")
        target = root.joinpath(*PurePosixPath(relative).parts)
        if not target.is_file() or target.stat().st_size != size or not hmac.compare_digest(sha256_file(target), digest):
            raise ReleaseContractError(f"Portable package file failed verification: {relative}")
        expected.add(relative)
        total += size
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path != manifest_path
    }
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ReleaseContractError(f"Portable package inventory mismatch; missing={missing}, extra={extra}")
    if int(payload.get("payload_file_count", -1)) != len(expected) or int(payload.get("payload_size_bytes", -1)) != total:
        raise ReleaseContractError("Portable package inventory totals do not match its files.")
    return payload


def safe_zip_members(names: Iterable[str]) -> list[PurePosixPath]:
    result: list[PurePosixPath] = []
    folded: set[str] = set()
    for name in names:
        if not name or "\\" in name:
            raise ReleaseContractError("Update archive contains an invalid member name.")
        path = PurePosixPath(name)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ReleaseContractError(f"Update archive member is unsafe: {name!r}")
        key = path.as_posix().rstrip("/").casefold()
        if key in folded:
            raise ReleaseContractError(f"Update archive repeats a case-insensitive path: {name!r}")
        folded.add(key)
        result.append(path)
    return result
