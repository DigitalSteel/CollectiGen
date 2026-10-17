from __future__ import annotations

from .catalog_admission import native_store_api

import colorsys
from hashlib import sha256
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from .tag_ordering import (
    MAX_TAG_PRIORITY,MIN_TAG_PRIORITY,ORDER_GROUP_DEFAULT_PRIORITY,ORDER_GROUP_RANK,
    default_order_group,
)
from .tag_collection import normalize_tag

if TYPE_CHECKING:
    from .category_base_relationships import DirectCategoryBaseAuthority


def normalize_category_color(value: str | None) -> str:
    color=str(value or "").strip()
    return color.upper() if re.fullmatch(r"#[0-9A-Fa-f]{6}",color) else ""


CATEGORY_COLOR_POLICIES = ("own", "parent", "parent_lighter", "none")
CATEGORY_COLOR_POLICY_LABELS = {
    "own": "Own color",
    "parent": "Use parent color",
    "parent_lighter": "Parent color, lighter by depth",
    "none": "No color",
}

CATEGORY_REFERENCE_POLICIES = ("inherit", "own", "none")
CATEGORY_BASE_TAG_POLICY_LABELS = {
    "inherit": "Inherit parent base tag",
    "own": "Use selected base tag",
    "none": "No base tag",
}
CATEGORY_SENSITIVITY_POLICY_LABELS = {
    "inherit": "Inherit parent sensitivity",
    "own": "Use selected sensitivity",
    "none": "Not classified",
}


def exact_category_anchor_ids(categories: Iterable[Any], tags: Iterable[Any]) -> dict[str, str]:
    """Return unique direct tags whose canonical names match their categories.

    The result is derived presentation/relationship state.  It does not write a
    category base or promote aliases, AI evidence, or lexical similarity into
    reviewed System Knowledge.
    """
    category_names={
        str(record.id):normalize_tag(str(record.name))
        for record in categories
        if str(getattr(record,"id","") or "") and normalize_tag(str(getattr(record,"name","") or ""))
    }
    candidates: dict[str,list[str]]={}
    for tag in tags:
        category_id=str(getattr(tag,"category","") or "")
        if category_id not in category_names:
            continue
        if normalize_tag(str(getattr(tag,"display","") or ""))!=category_names[category_id]:
            continue
        tag_id=str(getattr(tag,"id","") or "")
        if tag_id:
            candidates.setdefault(category_id,[]).append(tag_id)
    return {
        category_id:values[0]
        for category_id,values in candidates.items()
        if len(values)==1
    }
CATEGORY_SEMANTIC_ROLE_LABELS = {
    "character_source": "Character Source",
    "character_identity": "Character Identity",
    "character_outfit": "Character Outfit",
    "reusable_modifier": "Reusable Modifier",
    "modifier_color": "Color Modifier",
    "modifier_material": "Material Modifier",
    "modifier_pattern": "Pattern Modifier",
}

REUSABLE_MODIFIER_FAMILY_EXTRA = "reusable_modifier_family"


class _ReusableModifierFamilySnapshot:
    """Read-through category view with one stable family inventory.

    Bulk relationship compilers can ask for the same reviewed family list once
    per Tag.  Re-sorting the complete category tree for every Tag made opening
    the Tag Encyclopedia quadratic in ordinary System Knowledge size.  This
    view is intentionally short-lived: callers create it only for one bounded,
    read-only compilation after their candidate category state is complete.
    """

    def __init__(self, categories: Any, family_ids: Iterable[str]) -> None:
        self._categories = categories
        self._family_ids = tuple(family_ids)

    def reusable_modifier_family_ids(self) -> tuple[str, ...]:
        return self._family_ids

    def __getattr__(self, name: str) -> Any:
        return getattr(self._categories, name)


def reusable_modifier_family_snapshot(categories: Any) -> Any:
    """Freeze one category family's lookup for a read-only bulk operation."""

    resolver = getattr(categories, "reusable_modifier_family_ids", None)
    if not callable(resolver):
        return categories
    return _ReusableModifierFamilySnapshot(categories, resolver())


def category_own_semantic_role(record: "CategoryRecord | None") -> str:
    if record is None:
        return ""
    value = str(record.extras.get("semantic_role") or "").strip().casefold()
    if value == "ordinary":
        return "ordinary"
    return value if value in CATEGORY_SEMANTIC_ROLE_LABELS else ""


def category_is_protected(record: "CategoryRecord | None") -> bool:
    return bool(record is not None and record.extras.get("protected"))


def normalize_category_reference_policy(value: str | None, *, parent_id: str | None = None,
                                        own_value: str = "") -> str:
    policy = str(value or "").strip().casefold()
    if policy in CATEGORY_REFERENCE_POLICIES:
        return "none" if policy == "inherit" and not parent_id else policy
    if own_value:
        return "own"
    return "inherit" if parent_id else "none"


def normalize_category_color_policy(value: str | None, *, color: str = "", parent_id: str | None = None) -> str:
    policy = str(value or "").strip().casefold()
    if policy in CATEGORY_COLOR_POLICIES:
        return policy
    if color:
        return "own"
    return "parent_lighter" if parent_id else "none"


def _lighten_category_color(color: str, depth: int) -> str:
    """Return a deterministic bounded lightness variant without changing hue."""
    normalized = normalize_category_color(color)
    if not normalized or depth <= 0:
        return normalized
    red, green, blue = (int(normalized[index:index + 2], 16) / 255 for index in (1, 3, 5))
    hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    lightness = min(0.82, lightness + min(0.28, int(depth) * 0.07))
    red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
    return f"#{round(red * 255):02X}{round(green * 255):02X}{round(blue * 255):02X}"


@dataclass
class CategoryRecord:
    id: str
    name: str
    parent_id: str | None = None
    description: str = ""
    source: str = "user"
    ordering_group: str = ""
    ordering_priority: int | None = None
    ordering_reviewed: bool = False
    ordering_provenance: list[dict[str, Any]] = field(default_factory=list)
    color: str = ""
    color_policy: str = "none"
    base_tag_id: str = ""
    base_tag_policy: str = "none"
    sensitivity_rating: str = ""
    sensitivity_policy: str = "none"
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(
        self, *, include_category_base_projection: bool = True,
    ) -> dict[str, Any]:
        result = dict(self.extras)
        result.update({
            "id": self.id,
            "name": self.name,
            "parent_id": self.parent_id,
            "description": self.description,
            "color_policy": self.color_policy,
            "sensitivity_policy": self.sensitivity_policy,
        })
        if include_category_base_projection:
            result["base_tag_policy"] = self.base_tag_policy
        else:
            result.pop("base_tag_policy", None)
        if self.color:
            result["color"] = self.color
        else:
            result.pop("color", None)
        if include_category_base_projection and self.base_tag_id:
            result["base_tag_id"] = self.base_tag_id
        else:
            result.pop("base_tag_id", None)
        if self.sensitivity_rating:
            result["sensitivity_rating"] = self.sensitivity_rating
        else:
            result.pop("sensitivity_rating", None)
        if self.ordering_group:
            result["ordering"] = {
                "group": self.ordering_group,
                "priority": self.ordering_priority,
                "reviewed": self.ordering_reviewed,
                "provenance": list(self.ordering_provenance),
            }
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, source: str) -> "CategoryRecord":
        ordering = data.get("ordering") if isinstance(data.get("ordering"), dict) else {}
        group = str(ordering.get("group") or "")
        if group not in ORDER_GROUP_RANK: group = ""
        try: priority = int(ordering.get("priority")) if ordering.get("priority") is not None else None
        except (TypeError,ValueError): priority = None
        if priority is not None: priority=min(MAX_TAG_PRIORITY,max(MIN_TAG_PRIORITY,priority))
        color = normalize_category_color(data.get("color"))
        parent_id = str(data["parent_id"]) if data.get("parent_id") else None
        base_tag_id = str(data.get("base_tag_id") or "")
        sensitivity_rating = str(data.get("sensitivity_rating") or "").casefold()
        if sensitivity_rating not in {"general", "sensitive", "questionable", "explicit"}:
            sensitivity_rating = ""
        known = {"id", "name", "parent_id", "description", "ordering", "color", "color_policy", "base_tag_id", "base_tag_policy", "sensitivity_rating", "sensitivity_policy"}
        return cls(
            id=str(data.get("id") or "other"),
            name=str(data.get("name") or data.get("id") or "Other"),
            parent_id=parent_id,
            description=str(data.get("description") or ""),
            source=source,
            ordering_group=group,
            ordering_priority=priority,
            ordering_reviewed=bool(ordering.get("reviewed",False)),
            ordering_provenance=[item for item in ordering.get("provenance",[]) if isinstance(item,dict)],
            color=color,
            color_policy=normalize_category_color_policy(data.get("color_policy"), color=color, parent_id=parent_id),
            base_tag_id=base_tag_id,
            base_tag_policy=normalize_category_reference_policy(data.get("base_tag_policy"),parent_id=parent_id,own_value=base_tag_id),
            sensitivity_rating=sensitivity_rating,
            sensitivity_policy=normalize_category_reference_policy(data.get("sensitivity_policy"),parent_id=parent_id,own_value=sensitivity_rating),
            extras={key: value for key, value in data.items() if key not in known},
        )


class CategoryRegistry:
    """Merged default + user category knowledge.

    User records override defaults with the same stable ID.
    """

    SCHEMA_VERSION = 6
    ENROLLED_SCHEMA_VERSION = 7

    def __init__(self, default_path: str | Path, user_path: str | Path) -> None:
        self.default_path = Path(default_path)
        self.user_path = Path(user_path)
        self.categories: dict[str, CategoryRecord] = {}
        self.default_categories: dict[str, CategoryRecord] = {}
        self.disabled_default_ids: set[str] = set()
        self.user_extras: dict[str, Any] = {}
        self._category_base_source_index: dict[str, tuple[Any, ...]] = {}
        self.load()

    @staticmethod
    def slug(value: str) -> str:
        chars = []
        for char in value.strip().lower():
            chars.append(char if char.isalnum() else "_")
        slug = "_".join(part for part in "".join(chars).split("_") if part)
        return slug or "category"

    def _read_payload(self, path: Path) -> dict[str, Any]:
        raw=path.read_bytes() if path.exists() else None
        self._loaded_source_fingerprints[path]=sha256(raw).digest() if raw is not None else ()
        if path == self.user_path:
            from .tag_storage_decode import KnowledgeLifecycleCodec, decode_tag_storage
            codec = KnowledgeLifecycleCodec("category", self.SCHEMA_VERSION, self.ENROLLED_SCHEMA_VERSION)
            data = decode_tag_storage(raw.decode("utf-8"), None, lifecycle_codec=codec)[0] if raw is not None else {}
            self._lifecycle_codec = codec
            self._loaded_user_bytes = raw
        else:
            if raw is None:return {}
            data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}

    def _read(self, path: Path, source: str) -> list[CategoryRecord]:
        data = self._read_payload(path)
        return [
            CategoryRecord.from_dict(item, source=source)
            for item in data.get("categories", [])
            if isinstance(item, dict)
        ]

    @native_store_api("default_path", "user_path")
    def load(self) -> None:
        self._loaded_source_fingerprints={}
        self.categories.clear()
        self._category_base_source_index = {}
        self.default_categories={record.id:record for record in self._read(self.default_path,"default")}
        user_payload = self._read_payload(self.user_path)
        self.disabled_default_ids = {
            str(value) for value in user_payload.get("disabled_default_ids", [])
            if str(value).strip()
        }
        # Deterministic system-role categories cannot remain disabled by an
        # older local tombstone. A normal user override may still customize
        # their presentation without changing the protected semantic identity.
        self.disabled_default_ids.difference_update(
            record.id for record in self.default_categories.values()
            if category_is_protected(record)
        )
        self.user_extras = {
            key: value for key, value in user_payload.items()
            if key not in {"schema_version", "categories", "disabled_default_ids", "knowledge_lifecycle"}
        }
        for record in self.default_categories.values():
            if record.id not in self.disabled_default_ids:
                self.categories[record.id] = record
        for record in (
            CategoryRecord.from_dict(item, source="user")
            for item in user_payload.get("categories", []) if isinstance(item, dict)
        ):
            default=self.default_categories.get(record.id)
            if category_is_protected(default):
                record.parent_id=default.parent_id
                record.extras.update({
                    "semantic_role":default.extras["semantic_role"],
                    "protected":True,
                })
            self.categories[record.id] = record
        self._lifecycle_codec.bind_current(tuple(self.categories))

    @property
    def lifecycle_envelope(self):
        return self._lifecycle_codec.envelope

    @property
    def lifecycle_write_hold(self):
        return self._lifecycle_codec.write_hold

    def _lifecycle_current_ids(self):
        """Exact effective IDs, including defaults with no local record."""
        from .knowledge_lifecycle import KnowledgeLifecycleError
        effective = (set(self.default_categories) - set(self.disabled_default_ids)) | {
            record.id for record in self.categories.values() if record.source == "user"
        }
        if self.lifecycle_envelope is not None and (
            effective != set(self.categories) or any(key != record.id for key, record in self.categories.items())
        ):
            raise KnowledgeLifecycleError("Effective Categories disagree with current defaults or local identity decisions.")
        return tuple(effective)

    def stage_lifecycle_transitions(self, transitions, *, operation_id, expected_fingerprint):
        """Stage exact already-enrolled identity decisions, without write authority."""
        self._lifecycle_codec.stage(transitions, operation_id=operation_id,
            expected_fingerprint=expected_fingerprint, record_ids=self._lifecycle_current_ids())

    def user_payload(
        self, *, category_base_relationships: Iterable[Any] | None = None,
    ) -> dict[str, Any]:
        from .category_base_relationships import category_base_relationship_index
        from .knowledge_lifecycle import KnowledgeLifecycleError

        if "knowledge_lifecycle" in self.user_extras:
            raise KnowledgeLifecycleError("Lifecycle authority cannot be introduced through unknown fields.")
        lifecycle_fields = self._lifecycle_codec.fields_for_write(self._lifecycle_current_ids())

        records = [r for r in self.categories.values() if r.source == "user"]
        category_base_index = (
            self._category_base_source_index
            if category_base_relationships is None
            else category_base_relationship_index(category_base_relationships)
        )
        payload = dict(self.user_extras)
        payload.update({
            **lifecycle_fields,
            "categories": [
                r.to_dict(
                    include_category_base_projection=not bool(
                        category_base_index.get(r.id)
                    ),
                )
                for r in sorted(
                    records, key=lambda x: self.path_label(x.id).lower()
                )
            ],
            "disabled_default_ids": sorted(self.disabled_default_ids),
        })
        return payload

    @native_store_api("default_path", "user_path")
    def save_user(
        self, *, category_base_relationships: Iterable[Any] | None = None,
    ) -> None:
        payload = self.user_payload(
            category_base_relationships=category_base_relationships,
        )
        self.user_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.user_path.with_suffix(self.user_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        saved_fingerprint=sha256(temporary.read_bytes()).digest()
        temporary.replace(self.user_path)
        self._loaded_source_fingerprints[self.user_path]=saved_fingerprint

    def get(self, category_id: str | None) -> CategoryRecord | None:
        return self.categories.get(category_id or "")

    def configure_category_base_authority(self, relationships: Iterable[Any]) -> None:
        """Publish canonical authority without changing Category records.

        Schema-v6 user storage omits represented Category Base projections. The
        registry resolves canonical authority directly; unrepresented legacy
        fields remain compatibility input until they are reviewed.
        """

        from .category_base_relationships import category_base_relationship_index

        self._category_base_source_index = category_base_relationship_index(
            relationships,
        )

    def direct_category_base(
        self, category_id: str | None,
    ) -> DirectCategoryBaseAuthority | None:
        """Return direct canonical-or-legacy Category Base authority."""

        from .category_base_relationships import direct_category_base_authority

        record = self.categories.get(str(category_id or ""))
        if record is None:
            return None
        return direct_category_base_authority(
            record, self._category_base_source_index,
        )

    def effective_semantic_role(self, category_id: str | None) -> str:
        """Return the inherited deterministic role for one category."""
        seen: set[str] = set()
        current = self.categories.get(str(category_id or ""))
        while current is not None and current.id not in seen:
            seen.add(current.id)
            role = category_own_semantic_role(current)
            if role == "ordinary":
                return ""
            if role:
                return role
            current = self.categories.get(current.parent_id or "")
        return ""

    def semantic_role_source(self, category_id: str | None) -> str:
        seen: set[str] = set()
        current = self.categories.get(str(category_id or ""))
        while current is not None and current.id not in seen:
            seen.add(current.id)
            if category_own_semantic_role(current):
                return current.id
            current = self.categories.get(current.parent_id or "")
        return ""

    def category_ids_for_role(self, role: str) -> tuple[str, ...]:
        wanted = str(role or "").strip().casefold()
        return tuple(
            sorted(
                (
                    record.id for record in self.categories.values()
                    if self.effective_semantic_role(record.id) == wanted
                ),
                key=lambda value: self.path_label(value).casefold(),
            )
        )

    def is_reusable_modifier_family(self, category_id: str | None) -> bool:
        """Return whether one category is the reviewed source of a value family.

        A user may designate any category without moving it under the shipped
        Reusable Modifiers branch.  Shipped modifier-role categories and direct
        children of the protected reusable-modifier root remain compatible with
        older category knowledge.
        """
        record=self.categories.get(str(category_id or ""))
        if record is None:
            return False
        if record.extras.get(REUSABLE_MODIFIER_FAMILY_EXTRA) is True:
            return True
        if category_own_semantic_role(record) in {
            "modifier_color","modifier_material","modifier_pattern",
        }:
            return True
        parent=self.categories.get(record.parent_id or "")
        return category_own_semantic_role(parent)=="reusable_modifier"

    def modifier_family_source(self, category_id: str | None) -> str:
        """Return the nearest category that supplies this modifier family."""
        seen: set[str]=set()
        current=self.categories.get(str(category_id or ""))
        while current is not None and current.id not in seen:
            seen.add(current.id)
            if self.is_reusable_modifier_family(current.id):
                return current.id
            current=self.categories.get(current.parent_id or "")
        return ""

    def reusable_modifier_family_ids(self) -> tuple[str, ...]:
        return tuple(sorted(
            (
                record.id for record in self.categories.values()
                if self.is_reusable_modifier_family(record.id)
            ),
            key=lambda value:self.path_label(value).casefold(),
        ))

    def is_protected(self, category_id: str | None) -> bool:
        return category_is_protected(self.categories.get(str(category_id or "")))

    def find_sibling(self, name: str, parent_id: str | None) -> CategoryRecord | None:
        wanted = " ".join(name.strip().casefold().split())
        for record in self.categories.values():
            if record.parent_id == parent_id and " ".join(record.name.casefold().split()) == wanted:
                return record
        return None

    def duplicate_groups(self) -> list[list[CategoryRecord]]:
        groups: dict[tuple[str | None, str], list[CategoryRecord]] = {}
        for record in self.categories.values():
            key = (record.parent_id, " ".join(record.name.casefold().split()))
            groups.setdefault(key, []).append(record)
        return [items for items in groups.values() if len(items) > 1]

    def merge_categories(self, keep_id: str, remove_ids: Iterable[str]) -> dict[str, str]:
        replacements: dict[str, str] = {}
        for remove_id in remove_ids:
            if remove_id == keep_id or remove_id not in self.categories:
                continue
            if self.is_protected(remove_id):
                raise ValueError(
                    f"{self.categories[remove_id].name} has a protected system role and cannot be merged away."
                )
            for record in self.categories.values():
                if record.parent_id == remove_id:
                    record.parent_id = keep_id
                    if record.source == "default": record.source = "user"
            self.categories.pop(remove_id, None)
            if remove_id in self.default_categories:
                self.disabled_default_ids.add(remove_id)
            replacements[remove_id] = keep_id
        self.save_user()
        return replacements

    def add(self, name: str, *, parent_id: str | None = None, description: str = "",
            ordering_group: str = "", ordering_priority: int | None = None,
            ordering_reviewed: bool = False, color: str = "", color_policy: str | None = None,
            base_tag_id: str = "", base_tag_policy: str | None = None,
            sensitivity_rating: str = "", sensitivity_policy: str | None = None) -> CategoryRecord:
        existing_sibling = self.find_sibling(name, parent_id)
        if existing_sibling:
            return existing_sibling
        base = self.slug(name)
        category_id = base
        counter = 2
        while category_id in self.categories:
            existing = self.categories[category_id]
            if existing.name.casefold() == name.strip().casefold() and existing.parent_id == parent_id:
                return existing
            category_id = f"{base}_{counter}"
            counter += 1
        if color_policy is None and parent_id and not normalize_category_color(color):color_policy="parent_lighter"
        normalized_sensitivity=str(sensitivity_rating or "").casefold()
        if normalized_sensitivity not in {"general","sensitive","questionable","explicit"}:normalized_sensitivity=""
        record = CategoryRecord(
            id=category_id,name=name.strip(),parent_id=parent_id,description=description,source="user",
            ordering_group=ordering_group,ordering_priority=ordering_priority,
            ordering_reviewed=ordering_reviewed,ordering_provenance=[],color=normalize_category_color(color),
            color_policy=normalize_category_color_policy(color_policy,color=normalize_category_color(color),parent_id=parent_id),
            base_tag_id=str(base_tag_id or ""),
            base_tag_policy=normalize_category_reference_policy(base_tag_policy,parent_id=parent_id,own_value=base_tag_id),
            sensitivity_rating=normalized_sensitivity,
            sensitivity_policy=normalize_category_reference_policy(sensitivity_policy,parent_id=parent_id,own_value=normalized_sensitivity),
        )
        self.categories[category_id] = record
        self.save_user()
        return record

    def effective_ordering(self, category_id: str) -> tuple[str,int,bool,str]:
        """Return group, baseline priority, reviewed state, and source category."""
        seen: set[str] = set(); current=self.categories.get(category_id)
        reviewed_default = False
        fallback_source = category_id
        inferred_fallback: tuple[str,int,bool,str] | None = None
        while current and current.id not in seen:
            seen.add(current.id)
            is_default = current.source == "default" or current.id in self.default_categories
            reviewed_default = reviewed_default or is_default
            fallback_source = current.id
            candidate=current
            if not candidate.ordering_group and current.id in self.default_categories:
                candidate=self.default_categories[current.id]
            if candidate.ordering_group:
                group=candidate.ordering_group
                priority=candidate.ordering_priority if candidate.ordering_priority is not None else ORDER_GROUP_DEFAULT_PRIORITY[group]
                return group,priority,candidate.ordering_reviewed,current.id
            inferred=default_order_group(current.id)
            if inferred!="other" and inferred_fallback is None:
                inferred_fallback=(inferred,ORDER_GROUP_DEFAULT_PRIORITY[inferred],is_default,current.id)
            current=self.categories.get(current.parent_id or "")
        if inferred_fallback is not None:
            return inferred_fallback
        return "other",ORDER_GROUP_DEFAULT_PRIORITY["other"],reviewed_default,fallback_source

    def update(self, category_id: str, *, name: str, parent_id: str | None, description: str = "",
               ordering_group: str = "", ordering_priority: int | None = None,
               ordering_reviewed: bool = False, color: str = "", color_policy: str | None = None,
               base_tag_id: str | None = None, base_tag_policy: str | None = None,
               sensitivity_rating: str | None = None, sensitivity_policy: str | None = None) -> CategoryRecord:
        old = self.categories[category_id]
        if category_is_protected(old) and old.parent_id != parent_id:
            raise ValueError(
                f"{old.name} has a protected system role. Its semantic parent cannot be changed."
            )
        normalized_color=normalize_category_color(color)
        selected_policy=color_policy
        if old.parent_id is None and parent_id and not normalized_color and old.color_policy=="none" and selected_policy in {None,"none"}:
            selected_policy="parent_lighter"
        if selected_policy is None:
            if normalized_color and normalized_color!=old.color:selected_policy="own"
            elif not normalized_color and old.color:selected_policy=None
            else:selected_policy=old.color_policy
        selected_base_tag=old.base_tag_id if base_tag_id is None else str(base_tag_id or "")
        selected_base_policy=old.base_tag_policy if base_tag_policy is None else base_tag_policy
        selected_sensitivity=old.sensitivity_rating if sensitivity_rating is None else str(sensitivity_rating or "").casefold()
        if selected_sensitivity not in {"general","sensitive","questionable","explicit"}:selected_sensitivity=""
        selected_sensitivity_policy=old.sensitivity_policy if sensitivity_policy is None else sensitivity_policy
        if old.parent_id is None and parent_id:
            if base_tag_policy in {None,"none"} and old.base_tag_policy=="none" and not selected_base_tag:selected_base_policy="inherit"
            if sensitivity_policy in {None,"none"} and old.sensitivity_policy=="none" and not selected_sensitivity:selected_sensitivity_policy="inherit"
        # Editing a default creates a user override instead of modifying shipped knowledge.
        updated = CategoryRecord(
            id=category_id,name=name.strip(),parent_id=parent_id,description=description,source="user",
            ordering_group=ordering_group,ordering_priority=ordering_priority,
            ordering_reviewed=ordering_reviewed,ordering_provenance=list(old.ordering_provenance),color=normalized_color,
            color_policy=normalize_category_color_policy(
                selected_policy,color=normalized_color,parent_id=parent_id,
            ),
            base_tag_id=selected_base_tag,
            base_tag_policy=normalize_category_reference_policy(selected_base_policy,parent_id=parent_id,own_value=selected_base_tag),
            sensitivity_rating=selected_sensitivity if selected_sensitivity in {"general","sensitive","questionable","explicit"} else "",
            sensitivity_policy=normalize_category_reference_policy(selected_sensitivity_policy,parent_id=parent_id,own_value=selected_sensitivity),
            extras=dict(old.extras),
        )
        self.categories[category_id] = updated
        self.save_user()
        return updated

    def path_ids(self, category_id: str) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        current = self.categories.get(category_id)
        while current and current.id not in seen:
            result.append(current.id)
            seen.add(current.id)
            current = self.categories.get(current.parent_id or "")
        return list(reversed(result))

    def path_label(self, category_id: str) -> str:
        ids = self.path_ids(category_id)
        if not ids:
            return category_id
        return " → ".join(self.categories[item].name for item in ids)

    def choices(self) -> list[tuple[str, str]]:
        return sorted(
            ((category_id, self.path_label(category_id)) for category_id in self.categories),
            key=lambda item: item[1].casefold(),
        )

    def find_id_by_label(self, value: str) -> str | None:
        wanted = value.strip().casefold()
        for category_id, label in self.choices():
            if label.casefold() == wanted or self.categories[category_id].name.casefold() == wanted:
                return category_id
        return None

    def children_of(self, parent_id: str | None) -> Iterable[CategoryRecord]:
        return sorted(
            (record for record in self.categories.values() if record.parent_id == parent_id),
            key=lambda record: record.name.casefold(),
        )

    def effective_color(self, category_id: str) -> tuple[str, str | None, int, str]:
        """Return effective color, owning ancestor, depth, and selected policy."""
        return self._effective_color(category_id,set())

    def effective_base_tag(
        self,
        category_id: str,
        *,
        derived_anchors: dict[str,str] | None = None,
        relationship_index: dict[str, tuple[Any, ...]] | None = None,
    ) -> tuple[str, str | None, str]:
        """Return stable tag ID, owning category, and selected policy."""
        from .category_base_relationships import direct_category_base_authority

        authority = (
            self._category_base_source_index
            if relationship_index is None else relationship_index
        )
        seen=set(); current=self.categories.get(category_id); selected_policy="none"
        while current is not None and current.id not in seen:
            seen.add(current.id)
            direct = direct_category_base_authority(current, authority)
            policy = direct.policy
            if direct.ambiguous:
                return "", None, "ambiguous"
            if current.id==category_id:selected_policy=policy
            if policy=="own":
                if (
                    current.id==category_id
                    or direct.member_scope=="descendants"
                ):
                    return direct.base_tag_id,current.id,selected_policy
                # An explicit direct-only Base owns this Category's derived
                # compatibility projection, but does not apply below it. Keep
                # walking so a broader ancestor may still opt into descendants.
                current=self.categories.get(current.parent_id or "")
                continue
            derived_id=str((derived_anchors or {}).get(current.id) or "")
            if derived_id:return derived_id,current.id,selected_policy
            if policy=="none":
                return "",None,selected_policy
            current=self.categories.get(current.parent_id or "")
        return "",None,selected_policy

    def effective_sensitivity(self, category_id: str) -> tuple[str, str | None, str]:
        """Return inherited rating, owning category, and selected policy."""
        seen=set(); current=self.categories.get(category_id); selected_policy="none"
        while current is not None and current.id not in seen:
            seen.add(current.id)
            policy=normalize_category_reference_policy(current.sensitivity_policy,parent_id=current.parent_id,own_value=current.sensitivity_rating)
            if current.id==category_id:selected_policy=policy
            if policy=="own":return current.sensitivity_rating,current.id,selected_policy
            if policy=="none":return "",None,selected_policy
            current=self.categories.get(current.parent_id or "")
        return "",None,selected_policy

    def _effective_color(self,category_id: str,seen: set[str]) -> tuple[str,str | None,int,str]:
        record = self.categories.get(category_id)
        if record is None:
            return "", None, 0, "none"
        if record.id in seen:return "",None,0,"none"
        seen={*seen,record.id}
        policy = normalize_category_color_policy(record.color_policy,color=record.color,parent_id=record.parent_id)
        if policy == "own":
            return record.color, record.id if record.color else None, 0, policy
        if policy == "none":
            return "", None, 0, policy
        if policy == "parent":
            parent=self.categories.get(record.parent_id or "")
            if parent is None:return "",None,0,policy
            color,source,depth,_=self._effective_color(parent.id,seen)
            return color,source,depth+1,policy
        depth = 0
        current = self.categories.get(record.parent_id or "")
        while current is not None and current.id not in seen:
            seen.add(current.id)
            depth += 1
            current_policy = normalize_category_color_policy(current.color_policy,color=current.color,parent_id=current.parent_id)
            if current_policy == "own" and current.color:
                return _lighten_category_color(current.color,depth),current.id,depth,policy
            current = self.categories.get(current.parent_id or "")
        return "", None, depth, policy
