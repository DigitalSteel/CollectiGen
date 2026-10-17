from __future__ import annotations

import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from release_contract import (
    ReleaseArtifact,
    ReleaseContractError,
    SemVer,
    VerifiedReleaseManifest,
    ensure_supported_upgrade,
    safe_zip_members,
    verify_artifact,
    verify_portable_package,
    verify_release_manifest,
)
from version import PUBLIC_REPOSITORY


GITHUB_REPOSITORY = PUBLIC_REPOSITORY
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPOSITORY}"
MANIFEST_ASSET = "collectigen-release.json"
SIGNATURE_ASSET = "collectigen-release.json.sig"
PORTABLE_KIND = "portable-windows-x64"
MAX_METADATA_BYTES = 1_000_000
MAX_ARCHIVE_FILES = 20_000
MAX_ARCHIVE_UNPACKED_BYTES = 5 * 1024 * 1024 * 1024


class UpdateServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseAssetSource:
    name: str
    url: str
    size_bytes: int
    api_digest: str = ""


@dataclass(frozen=True)
class UpdateCandidate:
    release: VerifiedReleaseManifest
    assets: dict[str, ReleaseAssetSource]
    release_url: str


@dataclass(frozen=True)
class PreparedUpdate:
    version: str
    channel: str
    artifact: Path
    pending_state: Path
    release_notes: str


def _signed_asset_pair(assets: dict[str, ReleaseAssetSource]) -> tuple[str, str]:
    if MANIFEST_ASSET in assets and SIGNATURE_ASSET in assets:
        return MANIFEST_ASSET, SIGNATURE_ASSET
    raise UpdateServiceError("The release is missing its signed CollectiGen update manifest.")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_bytes(path, (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    json.loads(path.read_text(encoding="utf-8"))


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid4().hex}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _json_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateServiceError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise UpdateServiceError(f"{label} is not a JSON object.")
    return payload


class ApplicationUpdateService:
    def __init__(
        self,
        app_root: str | Path,
        current_version: str,
        *,
        trust_store: str | Path | None = None,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.root = Path(app_root).resolve()
        self.current_version = current_version
        self.trust_store = Path(trust_store) if trust_store else self.root / "data" / "defaults" / "release-trust.json"
        self.state_root = self.root / "data" / "update-state"
        self.opener = opener or urllib.request.urlopen

    @property
    def installed_package(self) -> bool:
        try:
            payload = json.loads((self.root / "portable-package.json").read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, TypeError):
            return False
        return (
            isinstance(payload, dict)
            and payload.get("schema_version") in {1,2}
            and payload.get("artifact") == "collectigen-portable-windows-x64"
        )

    def _request(self, url: str, *, limit: int = MAX_METADATA_BYTES) -> bytes:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": f"CollectiGen/{self.current_version}",
            },
        )
        try:
            with self.opener(request, timeout=20) as response:
                raw = response.read(limit + 1)
        except (OSError, urllib.error.URLError) as exc:
            raise UpdateServiceError(f"The release service is unavailable: {exc}") from exc
        if len(raw) > limit:
            raise UpdateServiceError("Release metadata exceeds the safety limit.")
        return raw

    def _release_payload(self, channel: str) -> dict[str, Any]:
        if channel == "stable":
            payload = _json_object(self._request(f"{GITHUB_API}/releases/latest"), "GitHub release")
            if payload.get("prerelease"):
                raise UpdateServiceError("GitHub returned a prerelease for the stable channel.")
            return payload
        if channel != "beta":
            raise UpdateServiceError("Update channel must be stable or prerelease.")
        try:
            values = json.loads(self._request(f"{GITHUB_API}/releases?per_page=20").decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateServiceError(f"GitHub release list is invalid: {exc}") from exc
        if not isinstance(values, list):
            raise UpdateServiceError("GitHub release list is invalid.")
        selected: tuple[SemVer,dict[str,Any]] | None = None
        for payload in values:
            if not isinstance(payload,dict) or not payload.get("prerelease") or payload.get("draft"):
                continue
            tag=str(payload.get("tag_name") or "").removeprefix("v")
            try:
                version=SemVer.parse(tag)
            except ReleaseContractError:
                continue
            if not version.prerelease:
                continue
            if selected is None or version.compare(selected[0])>0:
                selected=(version,payload)
        if selected is not None:
            return selected[1]
        raise UpdateServiceError("No prerelease is currently available.")

    @staticmethod
    def _assets(payload: dict[str, Any]) -> dict[str, ReleaseAssetSource]:
        result: dict[str, ReleaseAssetSource] = {}
        for value in payload.get("assets", []):
            if not isinstance(value, dict):
                continue
            name = str(value.get("name") or "")
            url = str(value.get("browser_download_url") or "")
            if not name or Path(name).name != name or not url.startswith("https://github.com/"):
                continue
            result[name] = ReleaseAssetSource(
                name,
                url,
                int(value.get("size") or 0),
                str(value.get("digest") or "").casefold(),
            )
        return result

    def check(self, channel: str = "stable") -> UpdateCandidate | None:
        payload = self._release_payload(channel)
        assets = self._assets(payload)
        manifest_name, signature_name = _signed_asset_pair(assets)
        manifest_bytes = self._request(assets[manifest_name].url)
        signature_bytes = self._request(assets[signature_name].url)
        try:
            release = verify_release_manifest(
                manifest_bytes,
                signature_bytes,
                self.trust_store,
                expected_channel=channel,
            )
        except (OSError, ReleaseContractError) as exc:
            raise UpdateServiceError(str(exc)) from exc
        tag = str(payload.get("tag_name") or "").removeprefix("v")
        if tag != release.version:
            raise UpdateServiceError("GitHub release tag does not match the signed application version.")
        try:
            ensure_supported_upgrade(self.current_version, release)
        except ReleaseContractError as exc:
            if "not newer" in str(exc):
                return None
            raise UpdateServiceError(str(exc)) from exc
        artifact = release.artifact(PORTABLE_KIND)
        source = assets.get(artifact.name)
        if source is None:
            raise UpdateServiceError("The signed portable artifact is missing from the GitHub release.")
        if source.size_bytes and source.size_bytes != artifact.size_bytes:
            raise UpdateServiceError("GitHub asset size does not match the signed release manifest.")
        if source.api_digest and source.api_digest != f"sha256:{artifact.sha256}":
            raise UpdateServiceError("GitHub asset digest does not match the signed release manifest.")
        return UpdateCandidate(release, assets, str(payload.get("html_url") or ""))

    def _download(
        self, source: ReleaseAssetSource, artifact: ReleaseArtifact,
        progress=None, should_cancel=None, *, downloads_root=None,
    ) -> Path:
        downloads = self.state_root / "downloads" if downloads_root is None else Path(downloads_root)
        downloads.mkdir(parents=True, exist_ok=True)
        part = downloads / (artifact.name + ".part")
        if part.exists() and part.stat().st_size > artifact.size_bytes:
            part.unlink()
        offset = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": f"CollectiGen/{self.current_version}"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(source.url, headers=headers)
        try:
            if should_cancel and should_cancel():
                raise InterruptedError("The update download was cancelled safely.")
            response = self.opener(request, timeout=60)
            status = int(getattr(response, "status", response.getcode()))
            if offset and status != 206:
                response.close()
                offset = 0
                part.unlink(missing_ok=True)
                response = self.opener(urllib.request.Request(source.url, headers={"User-Agent": headers["User-Agent"]}), timeout=60)
            mode = "ab" if offset else "wb"
            with response, part.open(mode) as output:
                copied = offset
                while True:
                    if should_cancel and should_cancel():
                        raise InterruptedError("The update download was cancelled safely.")
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    output.write(block)
                    copied += len(block)
                    if copied > artifact.size_bytes:
                        raise UpdateServiceError("Downloaded release exceeds its signed size.")
                    if progress:
                        progress(copied, artifact.size_bytes, artifact.name)
        except InterruptedError:
            raise
        except (OSError, urllib.error.URLError) as exc:
            if should_cancel and should_cancel():
                raise InterruptedError("The update download was cancelled safely.") from exc
            raise UpdateServiceError(f"The update download was interrupted and can be resumed: {exc}") from exc
        try:
            verify_artifact(part, artifact)
        except ReleaseContractError as exc:
            rejected = part.with_suffix(part.suffix + ".rejected")
            rejected.unlink(missing_ok=True)
            part.replace(rejected)
            raise UpdateServiceError(f"Downloaded package was quarantined because verification failed: {exc}") from exc
        complete = downloads / artifact.name
        complete.unlink(missing_ok=True)
        part.replace(complete)
        return complete

    def fetch_metadata(self, url: str, *, limit: int = MAX_METADATA_BYTES) -> bytes:
        """Transport only; caller owns origin, trust and selection admission."""
        if type(limit) is not int or not 0 < limit <= 1024 * 1024:
            raise UpdateServiceError("Metadata limit must be bounded and positive.")
        return self._request(url, limit=limit)

    def download_artifact(self, source, artifact, *, cache_directory,
                          progress=None, should_cancel=None) -> Path:
        """Transport only into an explicit caller-owned cache, never queue/apply."""
        return self._download(source, artifact, progress, should_cancel,
                              downloads_root=cache_directory)

    @staticmethod
    def _validate_zip(archive: zipfile.ZipFile) -> None:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_FILES:
            raise UpdateServiceError("Update archive contains too many files.")
        safe_zip_members([item.filename for item in infos])
        total = 0
        for info in infos:
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise UpdateServiceError("Update archive contains a symbolic link.")
            total += info.file_size
            if total > MAX_ARCHIVE_UNPACKED_BYTES:
                raise UpdateServiceError("Update archive exceeds the unpacked-size safety limit.")

    def _verify_portable_archive(self, source: Path, release: VerifiedReleaseManifest) -> None:
        artifact = release.artifact(PORTABLE_KIND)
        self.state_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.state_root) as folder:
            destination = Path(folder)
            with zipfile.ZipFile(source, "r") as archive:
                self._validate_zip(archive)
                archive.extractall(destination)
            manifests = list(destination.rglob("portable-package.json"))
            if len(manifests) != 1:
                raise UpdateServiceError("Portable update must contain exactly one package manifest.")
            try:
                verify_portable_package(
                    manifests[0].parent,
                    expected_version=release.version,
                    expected_manifest_sha256=artifact.portable_manifest_sha256,
                )
            except (OSError, ReleaseContractError) as exc:
                raise UpdateServiceError(str(exc)) from exc

    def _queue(
        self,
        release: VerifiedReleaseManifest,
        manifest_bytes: bytes,
        signature_bytes: bytes,
        artifact_path: Path,
        should_cancel=None,
    ) -> PreparedUpdate:
        if not self.installed_package:
            raise UpdateServiceError(
                "Application replacement is available only in a verified portable installation; source checkouts update through version control."
            )
        if should_cancel and should_cancel():
            raise InterruptedError("The update preparation was cancelled safely.")
        pending_path = self.state_root / "pending-update.json"
        if pending_path.exists():
            pending = _json_object(pending_path.read_bytes(), "Pending update state")
            if pending.get("target_version") != release.version:
                raise UpdateServiceError("A different verified update is already waiting for restart.")
        folder = self.state_root / "pending" / release.version
        folder.mkdir(parents=True, exist_ok=True)
        manifest_path = folder / MANIFEST_ASSET
        signature_path = folder / SIGNATURE_ASSET
        queued_artifact = folder / artifact_path.name
        copied_to_queue=artifact_path.resolve()!=queued_artifact.resolve()
        if copied_to_queue:
            temporary_artifact = queued_artifact.with_name(queued_artifact.name + f".{uuid4().hex}.tmp")
            try:
                with artifact_path.open("rb") as source,temporary_artifact.open("wb") as output:
                    while True:
                        if should_cancel and should_cancel():
                            raise InterruptedError("The update preparation was cancelled safely.")
                        block=source.read(1024*1024)
                        if not block:
                            break
                        output.write(block)
                shutil.copystat(artifact_path,temporary_artifact)
                temporary_artifact.replace(queued_artifact)
            finally:
                temporary_artifact.unlink(missing_ok=True)
        artifact = release.artifact(PORTABLE_KIND)
        if should_cancel and should_cancel():
            if copied_to_queue:
                queued_artifact.unlink(missing_ok=True)
            raise InterruptedError("The update preparation was cancelled safely.")
        verify_artifact(queued_artifact, artifact)
        if should_cancel and should_cancel():
            if copied_to_queue:
                queued_artifact.unlink(missing_ok=True)
            raise InterruptedError("The update preparation was cancelled safely.")
        _atomic_bytes(manifest_path, manifest_bytes)
        _atomic_bytes(signature_path, signature_bytes)
        _atomic_json(pending_path, {
            "schema_version": 1,
            "status": "ready",
            "created_at": _utc_now(),
            "source_version": self.current_version,
            "target_version": release.version,
            "channel": release.channel,
            "key_id": release.key_id,
            "manifest": str(manifest_path.relative_to(self.root)).replace("\\", "/"),
            "signature": str(signature_path.relative_to(self.root)).replace("\\", "/"),
            "artifact": str(queued_artifact.relative_to(self.root)).replace("\\", "/"),
            "artifact_name": artifact.name,
        })
        return PreparedUpdate(release.version, release.channel, queued_artifact, pending_path, release.release_notes)

    def download_and_prepare(
        self, candidate: UpdateCandidate, progress=None, should_cancel=None,
    ) -> PreparedUpdate:
        release = candidate.release
        artifact = release.artifact(PORTABLE_KIND)
        source = candidate.assets[artifact.name]
        artifact_path = self._download(source, artifact, progress, should_cancel)
        if should_cancel and should_cancel():
            raise InterruptedError("The update preparation was cancelled safely.")
        self._verify_portable_archive(artifact_path, release)
        if should_cancel and should_cancel():
            raise InterruptedError("The update preparation was cancelled safely.")
        manifest_name, signature_name = _signed_asset_pair(candidate.assets)
        manifest = self._request(candidate.assets[manifest_name].url)
        if should_cancel and should_cancel():
            raise InterruptedError("The update preparation was cancelled safely.")
        signature = self._request(candidate.assets[signature_name].url)
        try:
            confirmed = verify_release_manifest(
                manifest,
                signature,
                self.trust_store,
                expected_channel=release.channel,
            )
        except (OSError, ReleaseContractError) as exc:
            raise UpdateServiceError(str(exc)) from exc
        if confirmed.manifest_sha256!=release.manifest_sha256:
            raise UpdateServiceError("The release manifest changed after the update check; check again before downloading.")
        if should_cancel and should_cancel():
            raise InterruptedError("The update preparation was cancelled safely.")
        return self._queue(
            release,manifest,signature,artifact_path,
            should_cancel=should_cancel,
        )

    def prepare_offline_bundle(self, bundle_path: str | Path) -> PreparedUpdate:
        bundle = Path(bundle_path)
        self.state_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.state_root) as folder:
            work = Path(folder)
            try:
                with zipfile.ZipFile(bundle, "r") as archive:
                    self._validate_zip(archive)
                    archive.extractall(work)
            except (OSError, zipfile.BadZipFile, ReleaseContractError) as exc:
                raise UpdateServiceError(f"Offline update bundle is invalid: {exc}") from exc
            manifests = list(work.rglob(MANIFEST_ASSET))
            signatures = list(work.rglob(SIGNATURE_ASSET))
            if len(manifests) != 1 or len(signatures) != 1:
                raise UpdateServiceError("Offline update bundle must contain one manifest and signature.")
            manifest_path, signature_path = manifests[0], signatures[0]
            manifest_bytes = manifest_path.read_bytes()
            signature_bytes = signature_path.read_bytes()
            try:
                release = verify_release_manifest(manifest_bytes, signature_bytes, self.trust_store)
                ensure_supported_upgrade(self.current_version, release)
                artifact = release.artifact(PORTABLE_KIND)
            except ReleaseContractError as exc:
                raise UpdateServiceError(str(exc)) from exc
            artifacts = list(work.rglob(artifact.name))
            if len(artifacts) != 1:
                raise UpdateServiceError("Offline update bundle is missing its signed portable artifact.")
            expected_files = {
                manifest_path.resolve(),
                signature_path.resolve(),
                artifacts[0].resolve(),
            }
            actual_files = {path.resolve() for path in work.rglob("*") if path.is_file()}
            if actual_files != expected_files:
                raise UpdateServiceError(
                    "Offline update bundle must contain exactly its signed manifest, signature, and portable artifact."
                )
            try:
                verify_artifact(artifacts[0], artifact)
            except ReleaseContractError as exc:
                raise UpdateServiceError(str(exc)) from exc
            self._verify_portable_archive(artifacts[0], release)
            return self._queue(release, manifest_bytes, signature_bytes, artifacts[0])


def mark_active_update_starting(
    app_root: str | Path,
    application_version: str,
    *,
    process_id: int | None = None,
) -> bool:
    path = Path(app_root) / "data" / "update-state" / "active-update.json"
    if not path.is_file():
        return False
    payload = _json_object(path.read_bytes(), "Active update state")
    if payload.get("status") != "launch_ready" or payload.get("target_version") != application_version:
        return False
    payload["status"] = "awaiting_health"
    payload["application_process_id"] = int(process_id if process_id is not None else os.getpid())
    payload["application_started_at"] = _utc_now()
    _atomic_json(path, payload)
    return True


def mark_active_update_healthy(app_root: str | Path, application_version: str) -> bool:
    path = Path(app_root) / "data" / "update-state" / "active-update.json"
    if not path.is_file():
        return False
    payload = _json_object(path.read_bytes(), "Active update state")
    if payload.get("status") != "awaiting_health" or payload.get("target_version") != application_version:
        return False
    payload["status"] = "healthy"
    payload.pop("application_process_id", None)
    payload["healthy_at"] = _utc_now()
    _atomic_json(path, payload)
    return True


def request_update_rollback(app_root: str | Path, application_version: str) -> str:
    path = Path(app_root) / "data" / "update-state" / "active-update.json"
    if not path.is_file():
        raise UpdateServiceError("No verified previous application version is available.")
    payload = _json_object(path.read_bytes(), "Active update state")
    if payload.get("status") != "healthy" or payload.get("target_version") != application_version:
        raise UpdateServiceError("The current update is not eligible for manual rollback.")
    payload["rollback_requested"] = True
    payload["rollback_requested_at"] = _utc_now()
    _atomic_json(path, payload)
    return str(payload.get("source_version") or "")
