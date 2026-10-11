"""Pure GitHub metadata selection; locators never establish publisher trust.

No network, file writes, install, signature bypass or channel preference storage.
The caller must obtain a complete bounded release list and verify the selected
component envelope using a separately qualified production admission adapter.
"""
from dataclasses import dataclass
import json
from urllib.parse import quote

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


@dataclass(frozen=True)
class ReleaseLocator:
    component_id: ComponentId
    software_version: str
    channel: str
    tag: str
    assets: tuple[AssetLocator, ...]

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
        assets = row.get("assets")
        _require(type(assets) is list and len(assets) <= MAX_ASSETS, "asset_count")
        locators = []
        names = set()
        for asset in assets:
            _require(type(asset) is dict, "asset_row")
            name = asset.get("name")
            _require(type(name) is str and 0 < len(name) <= 255
                     and name not in (".", "..") and not any(c in name for c in "/\\")
                     and all(32 <= ord(c) < 127 for c in name), "asset_basename")
            _require(name not in names, "duplicate_asset")
            names.add(name)
            expected = f"https://github.com/{REPOSITORY}/releases/download/{quote(tag, safe='')}/{quote(name, safe='')}"
            _require(asset.get("browser_download_url") == expected, "asset_origin_binding")
            size = asset.get("size")
            _require(type(size) is int and 0 < size <= 2**40, "asset_size")
            locators.append(AssetLocator(name, expected, size))
        _require(bool(locators), "release_assets_missing")
        candidates.append((parsed, ReleaseLocator(component_id, version, channel, tag,
                          tuple(sorted(locators, key=lambda a: a.name)))))
    if not candidates:
        state = "waiting_for_stable_catch_up" if channel == "stable" and current is not None and current.prerelease else "no_newer_release"
        return DiscoveryResult(state, None)
    selected = candidates[0]
    for candidate in candidates[1:]:
        if candidate[0].compare(selected[0]) > 0:
            selected = candidate
    return DiscoveryResult("verification_required", selected[1])
