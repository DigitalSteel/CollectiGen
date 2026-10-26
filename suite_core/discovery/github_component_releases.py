"""Pure GitHub metadata selection; locators never establish publisher trust.

No network, file writes, install, signature bypass or channel preference storage.
The caller must obtain a complete bounded release list and verify the selected
component envelope using a separately qualified production admission adapter.
"""
from dataclasses import dataclass
import json
from urllib.parse import quote, unquote, urlsplit

from release_contract import SemVer, ReleaseContractError
from suite_core.installation import ComponentId

ADAPTER_VERSION = "0.1.0-alpha.3"
REPOSITORY = "DigitalSteel/CollectiGen"
MAX_BYTES = 1024 * 1024
MAX_RELEASES = 100
MAX_ASSETS = 32


class DiscoveryHold(ValueError):
    pass


@dataclass(frozen=True)
class AssetLocator:
    name: str
    url: str
    declared_size: int  # GitHub metadata, not verified package bytes
    asset_id: int | None = None


@dataclass(frozen=True)
class ReleaseLocator:
    component_id: ComponentId
    software_version: str
    channel: str
    tag: str
    assets: tuple[AssetLocator, ...]
    release_id: int | None = None
    schema_version: int = 1  # untrusted locator hints, never signed admission
    maturity: str | None = None

    @property
    def production_install_authorized(self):
        return False


@dataclass(frozen=True)
class DiscoveryResult:
    state: str
    candidate: ReleaseLocator | None


def _require(condition, reason):
    if not condition:
        raise DiscoveryHold(reason)


def _version(value):
    _require(type(value) is str and value.isascii() and len(value) <= 96 and value == value.strip(), "exact_version")
    try:
        parsed = SemVer.parse(value)
    except ReleaseContractError as exc:
        raise DiscoveryHold("exact_version") from exc
    # Build metadata has equal precedence: prohibit it in pilot release identity.
    _require("+" not in value, "build_metadata_not_supported")
    return parsed


def release_asset_identity(url, *, include_schema2=False):
    """Exact first-party component tag/basename, with one slash spelling variant.

    Decode once to identify fields, then require an explicitly reconstructed URL.
    Never normalize arbitrary paths, encoded basename separators or redirect hosts.
    """
    _require(type(include_schema2) is bool, "explicit_schema2_optin")
    _require(type(url) is str and 0 < len(url) <= 2048, "asset_origin_binding")
    try:
        parsed = urlsplit(url)
        prefix = f"/{REPOSITORY}/releases/download/"
        _require(parsed.scheme == "https" and parsed.netloc == "github.com"
                 and not parsed.query and not parsed.fragment and parsed.path.startswith(prefix),
                 "asset_origin_binding")
        parts = parsed.path[len(prefix):].split("/")
        _require(len(parts) in (2, 3) and all(parts), "asset_origin_binding")
        tag = unquote("/".join(parts[:-1]), errors="strict")
        name = unquote(parts[-1], errors="strict")
        schema2 = include_schema2 and "/releases/" in tag
        if schema2:
            _require(len(parts) == 2, "asset_origin_binding")
            _schema2_tag(tag)
        else:
            _require(tag.count("/v") == 1, "asset_origin_binding")
            component, version = tag.split("/v")
            ComponentId(component)
            _version(version)
        _require(0 < len(name) <= 255 and name not in (".", "..")
                 and not any(c in name for c in "/\\%")
                 and all(32 <= ord(c) < 127 for c in name), "asset_origin_binding")
        encoded = f"https://github.com/{REPOSITORY}/releases/download/{quote(tag, safe='')}/{quote(name, safe='')}"
        literal = f"https://github.com/{REPOSITORY}/releases/download/{quote(tag, safe='/')}/{quote(name, safe='')}"
        # Percent-escape hex case is equivalent only for this validated delimiter.
        _require(url == encoded if schema2 else
            url in (encoded, encoded.replace("%2F", "%2f", 1), literal), "asset_origin_binding")
        return tag, name
    except (ValueError, UnicodeError) as exc:
        raise DiscoveryHold("asset_origin_binding") from exc


def _pairs(rows):
    result = {}
    for key, value in rows:
        _require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def _constant(_):
    raise DiscoveryHold("nonfinite_json")


def _listing(raw, complete):
    _require(complete is True, "complete_release_listing_required")
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_BYTES, "metadata_size")
    try:
        releases = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except DiscoveryHold:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise DiscoveryHold("metadata_syntax") from exc
    _require(type(releases) is list and len(releases) <= MAX_RELEASES, "release_count")
    return releases


def discover_component_ids(raw, *, complete):
    """Unsigned bounded candidate IDs only; each selected offer still needs trust."""
    identifiers, seen = set(), set()
    for row in _listing(raw, complete):
        _require(type(row) is dict, "release_row")
        tag = row.get("tag_name")
        if type(tag) is not str or "/v" not in tag:
            continue
        name, version = tag.rsplit("/v", 1)
        try:
            component = ComponentId(name)
        except ValueError as exc:
            raise DiscoveryHold("component_tag_identity") from exc
        parsed = _version(version)
        _require(type(row.get("draft")) is bool and type(row.get("prerelease")) is bool,
                 "release_flags")
        _require(bool(parsed.prerelease) == row["prerelease"], "tag_channel_mismatch")
        _require(tag not in seen, "duplicate_component_tag")
        seen.add(tag)
        if not row["draft"]:
            identifiers.add(component.value)
    _require(len(identifiers) <= 64, "component_count")
    return tuple(ComponentId(name) for name in sorted(identifiers))


def _schema2_tag(tag):
    """Parse hints only. These fields must later match signed schema2 metadata."""
    from core.component_release_identity import ComponentReleaseIdentity
    _require(type(tag) is str and tag.isascii(), "schema2_tag_identity")
    parts = tag.split("/")
    _require(len(parts) == 5 and parts[1:3] == ["releases", "schema2"], "schema2_tag_identity")
    try:
        component = ComponentId(parts[0])
        identity = ComponentReleaseIdentity(parts[3], parts[4])
    except ValueError as exc:
        raise DiscoveryHold("schema2_tag_identity") from exc
    return component, identity


def _release_assets(row, tag, *, schema2=False):
    assets = row.get("assets")
    _require(type(assets) is list and len(assets) <= MAX_ASSETS, "asset_count")
    release_id = row.get("id")
    _require(release_id is None or type(release_id) is int and 0 < release_id < 2**63, "release_id")
    _require(not schema2 or type(release_id) is int and 0 < release_id < 2**63, "release_id")
    locators = []
    names = set()
    asset_ids = set()
    for asset in assets:
        _require(type(asset) is dict, "asset_row")
        name = asset.get("name")
        _require(type(name) is str and 0 < len(name) <= 255
                 and name not in (".", "..") and not any(c in name for c in "/\\%")
                 and all(32 <= ord(c) < 127 for c in name), "asset_basename")
        _require(name not in names, "duplicate_asset")
        names.add(name)
        url = asset.get("browser_download_url")
        _require(release_asset_identity(url, include_schema2=schema2) == (tag, name), "asset_origin_binding")
        size = asset.get("size")
        _require(type(size) is int and 0 < size <= 2**40, "asset_size")
        asset_id = asset.get("id")
        _require(asset_id is None or type(asset_id) is int and 0 < asset_id < 2**63, "asset_id")
        _require(asset_id is None or asset_id not in asset_ids, "duplicate_asset_id")
        _require(not schema2 or type(asset_id) is int and 0 < asset_id < 2**63, "asset_id")
        if asset_id is not None: asset_ids.add(asset_id)
        locators.append(AssetLocator(name, url, size, asset_id))
    _require(bool(locators), "release_assets_missing")
    return release_id, tuple(sorted(locators, key=lambda a: a.name))


def discover_candidates(raw, *, component_id, channel, complete, include_schema2=False):
    """Bounded untrusted worklist, with no installed-version filter or winner.

    Existing discover_releases remains the legacy selector. Explicit schema2
    opt-in retains same-number stages for later original-byte authentication.
    No I/O, crypto, transport activation, artifact fetch or installation.
    """
    _require(type(component_id) is ComponentId, "exact_component_id")
    _require(type(channel) is str and channel in ("stable", "beta"), "channel")
    _require(type(include_schema2) is bool, "explicit_schema2_optin")
    old_prefix, new_prefix = component_id.value + "/v", component_id.value + "/releases/"
    candidates = []; seen = set(); release_ids = set(); asset_ids = set()
    for row in _listing(raw, complete):
        _require(type(row) is dict, "release_row")
        tag = row.get("tag_name")
        if type(tag) is not str:
            continue
        schema2 = include_schema2 and tag.startswith(new_prefix)
        if not schema2 and not tag.startswith(old_prefix):
            continue
        _require(type(row.get("draft")) is bool and type(row.get("prerelease")) is bool, "release_flags")
        if row["draft"]:
            continue
        if schema2:
            component, identity = _schema2_tag(tag)
            _require(component == component_id, "schema2_tag_identity")
            version, maturity = identity.version, identity.maturity
            prerelease = maturity != "stable"
        else:
            version, maturity = tag[len(old_prefix):], None
            prerelease = bool(_version(version).prerelease)
        _require(prerelease == row["prerelease"], "tag_channel_mismatch")
        _require(tag not in seen, "duplicate_component_tag"); seen.add(tag)
        if prerelease != (channel == "beta"):
            continue
        release_id, assets = _release_assets(row, tag, schema2=schema2)
        _require(release_id is None or release_id not in release_ids, "duplicate_release_id")
        if release_id is not None: release_ids.add(release_id)
        for asset in assets:
            _require(asset.asset_id is None or asset.asset_id not in asset_ids, "duplicate_asset_id")
            if asset.asset_id is not None: asset_ids.add(asset.asset_id)
        candidates.append(ReleaseLocator(component_id, version, channel, tag, assets, release_id,
            2 if schema2 else 1, maturity))
    return tuple(candidates)


def discover_releases(raw, *, component_id, channel, current_version, complete):
    """Select a newer component release from supplied bytes, with no I/O.

    None current_version means first install, not a fabricated old version.
    Tags are exactly '<component-id>/v<semver>'. Beta selects prereleases
    (including alpha); Stable selects ordinary releases. A beta-to-stable
    switch waits for stable to catch up and never offers automatic downgrade.
    'complete' is a caller claim about pagination, not transport qualification.
    Asset URLs must identify this exact repository, tag and basename. All OS
    assets remain locators; signed platform admission is a separate gate.
    """
    _require(type(component_id) is ComponentId, "exact_component_id")
    _require(channel in ("stable", "beta") and type(channel) is str, "channel")
    current = None if current_version is None else _version(current_version)
    releases = _listing(raw, complete)
    prefix = component_id.value + "/v"
    candidates = []
    seen = set()
    for row in releases:
        _require(type(row) is dict, "release_row")
        tag = row.get("tag_name")
        if type(tag) is not str or not tag.startswith(prefix):
            continue
        _require(type(row.get("draft")) is bool and type(row.get("prerelease")) is bool, "release_flags")
        if row["draft"]:
            continue
        version = tag[len(prefix):]
        parsed = _version(version)
        _require(bool(parsed.prerelease) == row["prerelease"], "tag_channel_mismatch")
        _require(tag not in seen, "duplicate_component_tag")
        seen.add(tag)
        if row["prerelease"] != (channel == "beta"):
            continue
        if current is not None and parsed.compare(current) <= 0:
            continue
        release_id, assets = _release_assets(row, tag)
        candidates.append((parsed, ReleaseLocator(component_id, version, channel, tag, assets, release_id)))
    if not candidates:
        state = "waiting_for_stable_catch_up" if channel == "stable" and current is not None and current.prerelease else "no_newer_release"
        return DiscoveryResult(state, None)
    selected = candidates[0]
    for candidate in candidates[1:]:
        if candidate[0].compare(selected[0]) > 0:
            selected = candidate
    return DiscoveryResult("verification_required", selected[1])
