from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .tag_collection import normalize_tag


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class DiscoveredTag:
    display: str
    seen_count: int = 0
    first_seen: str = field(default_factory=_now)
    last_seen: str = field(default_factory=_now)
    example_images: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "display": self.display,
            "seen_count": self.seen_count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "example_images": self.example_images[:12],
        }


class DiscoveryRegistry:
    """Temporary observed tags that have not yet entered permanent Knowledge."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.items: dict[str, DiscoveredTag] = {}
        self.load()

    def load(self) -> None:
        self.items.clear()
        if not self.path.exists(): return
        try: payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception: return
        for item in payload.get("discovered_tags", []):
            if not isinstance(item, dict): continue
            display = str(item.get("display") or "").strip()
            if not display: continue
            self.items[normalize_tag(display)] = DiscoveredTag(
                display=display,
                seen_count=int(item.get("seen_count", 0) or 0),
                first_seen=str(item.get("first_seen") or _now()),
                last_seen=str(item.get("last_seen") or _now()),
                example_images=[str(v) for v in item.get("example_images", [])],
            )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, "discovered_tags": [v.to_dict() for v in sorted(self.items.values(), key=lambda x: x.display.casefold())]}
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def observe(self, display: str, image_path: str = "") -> DiscoveredTag:
        key = normalize_tag(display)
        record = self.items.get(key)
        if record is None:
            record = DiscoveredTag(display=display.strip())
            self.items[key] = record
        record.seen_count += 1; record.last_seen = _now()
        if image_path and image_path not in record.example_images:
            record.example_images.append(image_path)
            record.example_images = record.example_images[-12:]
        return record

    def remove(self, display: str) -> None:
        self.items.pop(normalize_tag(display), None)

    def reconcile_known(self, values: Iterable[str]) -> int:
        """Remove discovery entries that already exist in permanent knowledge."""
        known = {normalize_tag(value) for value in values}
        stale = [key for key in self.items if key in known]
        for key in stale:
            self.items.pop(key, None)
        if stale:
            self.save()
        return len(stale)
