from __future__ import annotations

import json
from copy import deepcopy
import re
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable


@dataclass
class PatternRecord:
    id: str
    name: str
    regex: str
    category: str
    enabled: bool = True
    auto_apply: bool = False
    confidence: float = 1.0
    examples: list[str] = field(default_factory=list)
    concept_id: str | None = None
    source: str = "user"
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = dict(self.extras)
        result.update({
            "id": self.id,
            "name": self.name,
            "regex": self.regex,
            "category": self.category,
            "enabled": self.enabled,
            "auto_apply": self.auto_apply,
            "confidence": self.confidence,
            "examples": list(self.examples),
            "concept_id": self.concept_id,
        })
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, source: str) -> "PatternRecord":
        known = {"id", "name", "regex", "category", "enabled", "auto_apply", "confidence", "examples", "concept_id"}
        return cls(
            id=str(data.get("id") or "pattern"),
            name=str(data.get("name") or data.get("id") or "Pattern"),
            regex=str(data.get("regex") or ""),
            category=str(data.get("category") or "uncategorized"),
            enabled=bool(data.get("enabled", True)),
            auto_apply=bool(data.get("auto_apply", False)),
            confidence=float(data.get("confidence", 1.0)),
            examples=[str(v) for v in data.get("examples", []) if str(v).strip()],
            concept_id=str(data.get("concept_id")) if data.get("concept_id") else None,
            source=source,
            extras={key: value for key, value in data.items() if key not in known},
        )


class PatternRegistry:
    """Merged reusable tag grammar. User patterns override defaults by stable ID."""

    def __init__(self, default_path: str | Path, user_path: str | Path) -> None:
        self.default_path = Path(default_path)
        self.user_path = Path(user_path)
        self.patterns: dict[str, PatternRecord] = {}
        self.default_patterns: dict[str, PatternRecord] = {}
        self.disabled_default_ids: set[str] = set()
        self.user_extras: dict[str, Any] = {}
        self._compiled: dict[str, re.Pattern[str]] = {}
        self.load()

    def _read_payload(self,path: Path) -> dict[str,Any]:
        raw=path.read_bytes() if path.exists() else None
        self._loaded_source_fingerprints[path]=(
            sha256(raw).digest() if raw is not None else ()
        )
        if raw is None:return {}
        payload=json.loads(raw.decode("utf-8"))
        return payload if isinstance(payload,dict) else {}

    def _read(self, path: Path, source: str) -> list[PatternRecord]:
        payload=self._read_payload(path)
        return [PatternRecord.from_dict(item, source=source) for item in payload.get("patterns", []) if isinstance(item, dict)]

    def load(self) -> None:
        self._loaded_source_fingerprints={}
        self.patterns.clear()
        self.default_patterns={record.id:record for record in self._read(self.default_path,"default")}
        user_payload=self._read_payload(self.user_path)
        self.disabled_default_ids={
            str(value) for value in user_payload.get("disabled_default_ids",[])
            if str(value).strip()
        }
        self.user_extras={
            key:value for key,value in user_payload.items()
            if key not in {"schema_version","patterns","disabled_default_ids"}
        }
        for record in self.default_patterns.values():
            if record.id not in self.disabled_default_ids:
                self.patterns[record.id] = record
        for record in (PatternRecord.from_dict(item,source="user") for item in user_payload.get("patterns",[]) if isinstance(item,dict)):
            self.patterns[record.id] = record
        self._compiled.clear()
        for record in self.patterns.values():
            if record.enabled and record.regex:
                try:
                    self._compiled[record.id] = re.compile(record.regex, re.IGNORECASE)
                except re.error:
                    continue

    def user_payload(self) -> dict[str, Any]:
        """Return detached storage data shared by preview and the native writer."""
        records = [item for item in self.patterns.values() if item.source == "user"]
        payload = dict(self.user_extras); payload.update({
            "schema_version": 1,
            "patterns": [item.to_dict() for item in sorted(records, key=lambda x: x.name.casefold())],
            "disabled_default_ids": sorted(self.disabled_default_ids),
        })
        return deepcopy(payload)

    def save_user(self) -> None:
        self.user_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.user_payload()
        temporary=self.user_path.with_suffix(self.user_path.suffix+".tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        saved_fingerprint=sha256(temporary.read_bytes()).digest()
        temporary.replace(self.user_path)
        self._loaded_source_fingerprints[self.user_path]=saved_fingerprint

    def match(self, tag: str) -> PatternRecord | None:
        value = tag.strip()
        for pattern_id, compiled in self._compiled.items():
            if compiled.fullmatch(value):
                return self.patterns[pattern_id]
        return None

    def classify(self, tags: Iterable[str]) -> tuple[dict[str, PatternRecord], list[str]]:
        matched: dict[str, PatternRecord] = {}
        unmatched: list[str] = []
        for tag in tags:
            pattern = self.match(tag)
            if pattern:
                matched[tag] = pattern
            else:
                unmatched.append(tag)
        return matched, unmatched
