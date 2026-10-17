from __future__ import annotations

import hashlib
import json
import shutil
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .tag_database import CONTENT_RATINGS


TAG_APPEARANCE_SCHEMA_VERSION = 1
TAG_APPEARANCE_CHANNELS = ("accent", "border", "background", "text")
TAG_APPEARANCE_SCOPES = ("categories", "tags")
MINIMUM_TEXT_CONTRAST = 4.5


class TagAppearanceError(RuntimeError):
    pass


class TagAppearanceValidationError(ValueError):
    pass


@dataclass(frozen=True)
class TagAppearanceIssue:
    location: str
    message: str


@dataclass(frozen=True)
class ResolvedAppearanceChannel:
    color: str
    source: str


@dataclass(frozen=True)
class ResolvedTagAppearance:
    accent: ResolvedAppearanceChannel
    border: ResolvedAppearanceChannel
    background: ResolvedAppearanceChannel
    text: ResolvedAppearanceChannel
    contrast_ratio: float
    contrast_warning: bool
    safety_substituted: bool = False

    def channel(self, name: str) -> ResolvedAppearanceChannel:
        if name not in TAG_APPEARANCE_CHANNELS:
            raise KeyError(name)
        return getattr(self, name)


def normalize_appearance_color(value: Any, *, allow_auto: bool = False) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if allow_auto and text.casefold() == "auto":
        return "auto"
    if len(text) != 7 or not text.startswith("#"):
        raise TagAppearanceValidationError("Use an exact color in #RRGGBB form.")
    digits = text[1:]
    if any(character not in "0123456789abcdefABCDEF" for character in digits):
        raise TagAppearanceValidationError("Use an exact color in #RRGGBB form.")
    return "#" + digits.upper()


def _rgb(color: str) -> tuple[int, int, int]:
    normalized = normalize_appearance_color(color)
    return tuple(int(normalized[index:index + 2], 16) for index in (1, 3, 5))


def _relative_luminance(color: str) -> float:
    values = []
    for component in _rgb(color):
        value = component / 255.0
        values.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    return 0.2126 * values[0] + 0.7152 * values[1] + 0.0722 * values[2]


def contrast_ratio(first: str, second: str) -> float:
    first_luminance = _relative_luminance(first)
    second_luminance = _relative_luminance(second)
    lighter = max(first_luminance, second_luminance)
    darker = min(first_luminance, second_luminance)
    return (lighter + 0.05) / (darker + 0.05)


def safe_text_color(background: str) -> str:
    black_ratio = contrast_ratio("#000000", background)
    white_ratio = contrast_ratio("#FFFFFF", background)
    return "#000000" if black_ratio >= white_ratio else "#FFFFFF"


def _sha256(payload: bytes | None) -> str:
    return hashlib.sha256(payload or b"").hexdigest()


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_bytes(
        path,
        (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )


def default_tag_appearance_payload() -> dict[str, Any]:
    return {
        "schema_version": TAG_APPEARANCE_SCHEMA_VERSION,
        "revision": 0,
        "categories": {},
        "tags": {},
        "rules": [],
    }


def _validate_style(style: Any, location: str) -> None:
    if not isinstance(style, dict):
        raise TagAppearanceValidationError(f"{location} must be a color-choice object.")
    for channel in TAG_APPEARANCE_CHANNELS:
        if channel not in style:
            continue
        value = style[channel]
        if value in (None, ""):
            continue
        normalize_appearance_color(value, allow_auto=channel == "text")
        if channel != "text" and str(value).casefold() == "auto":
            raise TagAppearanceValidationError(
                f"{location}.{channel} cannot use automatic text color."
            )


def validate_tag_appearance_payload(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise TagAppearanceValidationError("The appearance profile must be a JSON object.")
    try:
        schema_version = int(payload.get("schema_version", TAG_APPEARANCE_SCHEMA_VERSION))
        revision = int(payload.get("revision", 0))
    except (TypeError, ValueError) as exc:
        raise TagAppearanceValidationError(
            "The appearance profile has an invalid schema version or revision."
        ) from exc
    if schema_version < 1 or revision < 0:
        raise TagAppearanceValidationError(
            "The appearance profile has an invalid schema version or revision."
        )
    if schema_version > TAG_APPEARANCE_SCHEMA_VERSION:
        raise TagAppearanceValidationError(
            "This appearance profile was written by a newer CollectiGen version."
        )
    for scope in TAG_APPEARANCE_SCOPES:
        records = payload.get(scope, {})
        if not isinstance(records, dict):
            raise TagAppearanceValidationError(f"{scope.title()} must be keyed by stable ID.")
        for stable_id, style in records.items():
            if not isinstance(stable_id, str) or not stable_id.strip():
                raise TagAppearanceValidationError(
                    f"{scope.title()} contains an invalid stable ID."
                )
            _validate_style(style, f"{scope}.{stable_id}")
    rules = payload.get("rules", [])
    if not isinstance(rules, list):
        raise TagAppearanceValidationError("Conditional rules must be a list.")
    seen_ids: set[str] = set()
    seen_ratings: set[str] = set()
    for index, rule in enumerate(rules):
        location = f"rules[{index}]"
        if not isinstance(rule, dict):
            raise TagAppearanceValidationError(f"{location} must be an object.")
        stable_id = str(rule.get("id") or "").strip()
        if not stable_id or stable_id in seen_ids:
            raise TagAppearanceValidationError(
                "Every conditional rule needs a unique stable ID."
            )
        seen_ids.add(stable_id)
        if not isinstance(rule.get("enabled", True), bool):
            raise TagAppearanceValidationError(
                f"{location}.enabled must be true or false."
            )
        condition = rule.get("condition")
        if not isinstance(condition, dict) or condition.get("kind") != "sensitivity":
            raise TagAppearanceValidationError(
                "Conditional rules currently support exact reviewed sensitivity only."
            )
        rating = str(condition.get("value") or "")
        if rating not in CONTENT_RATINGS:
            raise TagAppearanceValidationError(
                f"{location} has an unknown sensitivity condition."
            )
        if rating in seen_ratings:
            raise TagAppearanceValidationError(
                f"A conditional rule for {rating.title()} sensitivity already exists."
            )
        seen_ratings.add(rating)
        _validate_style(rule.get("style", {}), f"{location}.style")


def set_style_value(
    payload: Mapping[str, Any],
    scope: str,
    stable_id: str,
    channel: str,
    value: str | None,
) -> dict[str, Any]:
    if scope not in TAG_APPEARANCE_SCOPES:
        raise TagAppearanceValidationError("Choose a tag or category appearance scope.")
    if channel not in TAG_APPEARANCE_CHANNELS:
        raise TagAppearanceValidationError("Choose a supported appearance channel.")
    stable_id = str(stable_id or "").strip()
    if not stable_id:
        raise TagAppearanceValidationError("The appearance scope has no stable ID.")
    normalized = normalize_appearance_color(value, allow_auto=channel == "text")
    candidate = deepcopy(dict(payload))
    records = candidate.setdefault(scope, {})
    if not isinstance(records, dict):
        records = {}
        candidate[scope] = records
    style = deepcopy(records.get(stable_id, {}))
    if not isinstance(style, dict):
        style = {}
    if normalized:
        style[channel] = normalized
    else:
        style.pop(channel, None)
    if any(key in style for key in TAG_APPEARANCE_CHANNELS):
        records[stable_id] = style
    else:
        records.pop(stable_id, None)
    return candidate


def new_sensitivity_rule(rating: str) -> dict[str, Any]:
    rating = str(rating or "")
    if rating not in CONTENT_RATINGS:
        raise TagAppearanceValidationError("Choose an exact reviewed sensitivity rating.")
    return {
        "id": f"appearance-rule.{uuid4().hex}",
        "enabled": True,
        "condition": {"kind": "sensitivity", "value": rating},
        "style": {},
    }


class TagAppearanceProfileStore:
    """Versioned user-owned tag-chip appearance profile with verified writes."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.payload: dict[str, Any] = default_tag_appearance_payload()
        self.issues: list[TagAppearanceIssue] = []
        self._loaded_bytes: bytes | None = None
        self._load_error = ""
        self.recover_interrupted(self.path)
        self.load()

    @staticmethod
    def recover_interrupted(path: str | Path) -> tuple[Path, ...]:
        target = Path(path).resolve()
        root = target.parent / "backups" / "tag-appearance"
        if not root.exists():
            return ()
        recovered: list[Path] = []
        for manifest_path in sorted(root.glob("*/recovery.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if (
                    not isinstance(manifest, dict)
                    or manifest.get("operation") != "tag_appearance_profile_write"
                    or manifest.get("status") != "prepared"
                    or Path(str(manifest.get("target") or "")).resolve() != target
                ):
                    continue
                expected_sha = str(manifest.get("expected_sha256") or "")
                if not expected_sha:
                    continue
                before_name = str(manifest.get("before") or "")
                before_path = (manifest_path.parent / before_name).resolve() if before_name else None
                if before_path is not None and before_path.parent != manifest_path.parent.resolve():
                    continue
                current = target.read_bytes() if target.exists() else None
                current_sha = _sha256(current)
                if current_sha == expected_sha:
                    manifest["status"] = "complete"
                elif current_sha == str(manifest.get("before_sha256") or ""):
                    manifest["status"] = "rolled_back"
                elif before_path is not None and before_path.exists():
                    _atomic_bytes(target, before_path.read_bytes())
                    manifest["status"] = "rolled_back"
                    recovered.append(manifest_path)
                elif before_path is None and current is None:
                    manifest["status"] = "rolled_back"
                    recovered.append(manifest_path)
                else:
                    manifest["status"] = "recovery_failed"
                _atomic_json(manifest_path, manifest)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                continue
        return tuple(recovered)

    @property
    def revision(self) -> int:
        try:
            return int(self.payload.get("revision", 0))
        except (TypeError, ValueError):
            return 0

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self.payload)

    def load(self) -> None:
        self.issues = []
        raw = None
        try:
            raw = self.path.read_bytes() if self.path.exists() else None
            payload = json.loads(raw.decode("utf-8")) if raw is not None else default_tag_appearance_payload()
            validate_tag_appearance_payload(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, TagAppearanceValidationError) as exc:
            self._loaded_bytes = raw
            self._load_error = f"Saved tag appearance could not be read: {exc}"
            self.issues.append(TagAppearanceIssue("file", self._load_error))
            self.payload = default_tag_appearance_payload()
            return
        self._loaded_bytes = raw
        self._load_error = ""
        self.payload = deepcopy(payload)

    def _ensure_writable(self) -> None:
        if self._load_error:
            raise TagAppearanceError(
                f"{self._load_error} The existing file was left unchanged."
            )
        try:
            current = self.path.read_bytes() if self.path.exists() else None
        except OSError as exc:
            raise TagAppearanceError(
                f"Saved tag appearance could not be checked for changes: {exc}"
            ) from exc
        if current != self._loaded_bytes:
            raise TagAppearanceError(
                "Tag appearance changed after this editor opened. Reload and retry."
            )

    def save(
        self,
        candidate: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> Path | None:
        self._ensure_writable()
        if expected_revision is not None and expected_revision != self.revision:
            raise TagAppearanceError(
                "Tag appearance changed after editing began. Reload and retry."
            )
        expected = deepcopy(dict(candidate))
        expected["schema_version"] = TAG_APPEARANCE_SCHEMA_VERSION
        expected["revision"] = self.revision + 1
        validate_tag_appearance_payload(expected)
        comparable = deepcopy(expected)
        comparable["revision"] = self.revision
        if comparable == self.payload:
            return None
        previous = self._loaded_bytes
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = (
            self.path.parent
            / "backups"
            / "tag-appearance"
            / f"{stamp}_{uuid4().hex[:8]}"
        )
        backup.mkdir(parents=True, exist_ok=False)
        before_name = ""
        if previous is not None:
            before_name = self.path.name
            shutil.copy2(self.path, backup / before_name)
        manifest_path = backup / "recovery.json"
        manifest = {
            "schema_version": 1,
            "operation": "tag_appearance_profile_write",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "target": str(self.path.resolve()),
            "before": before_name,
            "before_sha256": _sha256(previous),
            "expected_sha256": _sha256(
                (json.dumps(expected, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            ),
            "after_sha256": "",
            "status": "prepared",
        }
        _atomic_json(manifest_path, manifest)
        try:
            _atomic_json(self.path, expected)
            actual = json.loads(self.path.read_text(encoding="utf-8"))
            if actual != expected:
                raise TagAppearanceError(
                    "Saved tag appearance failed read-back verification."
                )
            manifest["after_sha256"] = _sha256(self.path.read_bytes())
            manifest["status"] = "complete"
            _atomic_json(manifest_path, manifest)
        except BaseException:
            if previous is None:
                if self.path.exists():
                    self.path.unlink()
            else:
                _atomic_bytes(self.path, previous)
            manifest["status"] = "rolled_back"
            _atomic_json(manifest_path, manifest)
            self.load()
            raise
        self.load()
        return backup

    def import_payload(
        self,
        payload: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> Path | None:
        candidate = deepcopy(dict(payload))
        validate_tag_appearance_payload(candidate)
        candidate["schema_version"] = TAG_APPEARANCE_SCHEMA_VERSION
        candidate["revision"] = self.revision
        return self.save(candidate, expected_revision=expected_revision)

    def export_to(self, path: str | Path) -> Path:
        target = Path(path)
        payload = self.snapshot()
        validate_tag_appearance_payload(payload)
        _atomic_json(target, payload)
        actual = json.loads(target.read_text(encoding="utf-8"))
        if actual != payload:
            raise TagAppearanceError(
                "Exported tag appearance failed read-back verification."
            )
        return target

    def stale_references(
        self,
        *,
        tag_ids: set[str] | frozenset[str],
        category_ids: set[str] | frozenset[str],
    ) -> tuple[tuple[str, str], ...]:
        stale: list[tuple[str, str]] = []
        for stable_id in self.payload.get("tags", {}):
            if stable_id not in tag_ids:
                stale.append(("tag", stable_id))
        for stable_id in self.payload.get("categories", {}):
            if stable_id not in category_ids:
                stale.append(("category", stable_id))
        return tuple(stale)


class TagAppearanceResolver:
    """Resolve independent appearance channels with a bounded revision cache."""

    def __init__(self, store: TagAppearanceProfileStore, *, cache_limit: int = 100_000):
        self.store = store
        self.cache_limit = max(1, int(cache_limit))
        self._cache: dict[tuple[Any, ...], ResolvedTagAppearance] = {}
        self._context_cache: dict[tuple[Any, ...], tuple[dict[str, str], dict[str, str]]] = {}
        self._resolved_style_cache: dict[tuple[Any, ...], ResolvedTagAppearance] = {}
        self._cache_revision = store.revision

    def clear(self) -> None:
        self._cache.clear()
        self._context_cache.clear()
        self._resolved_style_cache.clear()
        self._cache_revision = self.store.revision

    def resolve(
        self,
        *,
        tag_id: str,
        category_id: str,
        semantic_accent: str,
        sensitivity: str,
        theme: Mapping[str, str],
        theme_key: str = "",
        high_contrast: bool = False,
    ) -> ResolvedTagAppearance:
        if self._cache_revision != self.store.revision:
            self.clear()
        context_key = (
            self.store.revision,
            str(category_id),
            str(sensitivity),
            str(theme_key),
            str(semantic_accent or ""),
            str(theme.get("border", "#808080")),
            str(theme.get("background", "#FFFFFF")),
            str(theme.get("text", "#000000")),
        )
        key = (context_key,str(tag_id),bool(high_contrast))
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        def apply_style(style: Any, source: str) -> None:
            if not isinstance(style, dict):
                return
            for channel in TAG_APPEARANCE_CHANNELS:
                value = style.get(channel)
                if value in (None, ""):
                    continue
                try:
                    normalized = normalize_appearance_color(
                        value, allow_auto=channel == "text"
                    )
                except TagAppearanceValidationError:
                    continue
                colors[channel] = normalized
                sources[channel] = source

        base=self._context_cache.get(context_key)
        if base is None:
            colors = {
                "accent": normalize_appearance_color(
                    semantic_accent or theme.get("border", "#808080")
                ),
                "border": normalize_appearance_color(theme.get("border", "#808080")),
                "background": normalize_appearance_color(
                    theme.get("background", "#FFFFFF")
                ),
                "text": normalize_appearance_color(theme.get("text", "#000000")),
            }
            sources = {
                channel: "Theme fallback" for channel in TAG_APPEARANCE_CHANNELS
            }
            if semantic_accent:
                sources["accent"] = "System Knowledge category accent"
            apply_style(
                self.store.payload.get("categories", {}).get(str(category_id), {}),
                "Category preference",
            )
            for rule in self.store.payload.get("rules", []):
                if not isinstance(rule, dict) or not rule.get("enabled", True):
                    continue
                condition = rule.get("condition", {})
                if (
                    isinstance(condition, dict)
                    and condition.get("kind") == "sensitivity"
                    and condition.get("value") == sensitivity
                ):
                    apply_style(
                        rule.get("style", {}),
                        f"Conditional rule: sensitivity is {str(sensitivity).title()}",
                    )
            base=(dict(colors),dict(sources))
            if len(self._context_cache) >= self.cache_limit:
                self._context_cache.clear()
            self._context_cache[context_key]=base
        colors=dict(base[0]); sources=dict(base[1])
        apply_style(
            self.store.payload.get("tags", {}).get(str(tag_id), {}),
            "SDXL Tag override",
        )
        style_key=(
            tuple(colors[channel] for channel in TAG_APPEARANCE_CHANNELS),
            tuple(sources[channel] for channel in TAG_APPEARANCE_CHANNELS),
            bool(high_contrast),
        )
        resolved=self._resolved_style_cache.get(style_key)
        if resolved is not None:
            if len(self._cache) >= self.cache_limit:
                self._cache.clear()
            self._cache[key]=resolved
            return resolved
        if colors["text"] == "auto":
            colors["text"] = safe_text_color(colors["background"])
            sources["text"] = f"{sources['text']} (safe automatic text)"
        ratio = contrast_ratio(colors["text"], colors["background"])
        substituted = False
        if high_contrast and ratio < MINIMUM_TEXT_CONTRAST:
            colors["text"] = safe_text_color(colors["background"])
            sources["text"] = "High-contrast safety substitute"
            ratio = contrast_ratio(colors["text"], colors["background"])
            substituted = True
        resolved = ResolvedTagAppearance(
            **{
                channel: ResolvedAppearanceChannel(colors[channel], sources[channel])
                for channel in TAG_APPEARANCE_CHANNELS
            },
            contrast_ratio=ratio,
            contrast_warning=ratio < MINIMUM_TEXT_CONTRAST,
            safety_substituted=substituted,
        )
        if len(self._resolved_style_cache) >= self.cache_limit:
            self._resolved_style_cache.clear()
        self._resolved_style_cache[style_key]=resolved
        if len(self._cache) >= self.cache_limit:
            self._cache.clear()
        self._cache[key] = resolved
        return resolved
