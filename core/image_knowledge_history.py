"""Opt-in saved Image Knowledge capture and typed selective restoration."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
from threading import local
from typing import Callable, Mapping
from uuid import uuid4

from .annotation import ImageAnnotation, SCHEMA_VERSION
from .image_contexts import validate_image_contexts
from .tag_collection import normalize_tag
from .history_versions import (
    HistoryEntityRef, HistoryError, HistoryStore, VersionRef, read_record, record_token,
)


MAX_PREVIEW_BYTES = 64 * 1024 * 1024
MAX_AUTHORITY_ITEMS = 4096
MAX_AUTHORITY_BYTES = 512 * 1024
SUPPORTED_RESTORE_UNITS = ("descriptions", "tags", "absence", "exclusion", "characters", "objects", "contexts", "decisions", "review", "record", "record_absence")
_ACTIVE_GUARDS = local()


@contextmanager
def _admitted_guard(factory):
    if factory is None:
        yield
        return
    held = getattr(_ACTIVE_GUARDS, "factories", None)
    if held is None:
        held = _ACTIVE_GUARDS.factories = set()
    key = id(factory)
    if key in held:
        yield
        return
    with factory():
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)


class HistoryPreviewCancelled(HistoryError):
    """No partial selection is authorized after cancelling a comparison."""


class HistoryWriteInterrupted(HistoryError):
    """The canonical record may have changed; reconcile before another write."""


@dataclass(frozen=True)
class HistoryWriteContext:
    store: HistoryStore
    entity: HistoryEntityRef
    operation_id: str
    restored_from: str = ""
    operation_kind: str = ""
    reason: str = ""
    producer: str = ""
    authority_guard: Callable | None = field(default=None, compare=False, repr=False)
    restored_units: tuple[str, ...] = ()

    @contextmanager
    def locked(self, image: Path):
        with _admitted_guard(self.authority_guard), self.store.locked(image):
            self.store._ready()
            if self.store.validate_entity(self.entity) != self.store.scope.image(image):
                raise HistoryError("Write context does not authorize this image.")
            yield

    def write(self, image: Path, intended: bytes | None, writer: Callable[[], object], *, with_intent: bool = False):
        with self.locked(image):
            path = image.with_suffix(".tags.json")
            before = read_record(path)
            intent = self.store.prepare(self.entity, before, intended,
                                        operation_id=self.operation_id, restored_from=self.restored_from,
                                        operation_kind=self.operation_kind, reason=self.reason, producer=self.producer, restored_units=self.restored_units)
            if intent is None:
                return False
            # If the writer raises, retain the intent. Restart can distinguish
            # unchanged, successfully written, and divergent authority exactly.
            try:
                result = writer(intent) if with_intent else writer()
            except Exception as exc:
                from .image_record import ImageRecordStaleWriteError
                if isinstance(exc, ImageRecordStaleWriteError):
                    # Existing editor callers use this precise signal to open
                    # stale-save recovery. No canonical write was attempted.
                    raise
                if read_record(path) != before:
                    raise HistoryWriteInterrupted(
                        "Image Knowledge changed during an interrupted write. Preserve current data "
                        "and reconcile history before another save."
                    ) from exc
                raise
            try:
                self.store.commit(intent)
            except Exception as exc:
                raise HistoryWriteInterrupted(
                    "Image Knowledge was written, but history completion is interrupted. "
                    "Keep the current record and reconcile history before another save."
                ) from exc
            return result


@dataclass(frozen=True)
class RestoreUnit:
    kind: str
    key: str = ""

    @property
    def id(self) -> str:
        return self.kind + (":" + self.key if self.key else "")


@dataclass(frozen=True)
class RestoreSelection:
    entity: HistoryEntityRef
    target_version: str
    base_version: str = ""
    units: tuple[RestoreUnit, ...] = (RestoreUnit("descriptions"),)
    mode: str = "version"


@dataclass(frozen=True)
class RestoreUnitChange:
    unit: RestoreUnit
    changed: bool
    later_overlap: bool
    required_units: tuple[str, ...] = ()


@dataclass(frozen=True)
class RestoreItem:
    entity: HistoryEntityRef
    target: VersionRef
    current_token: str
    historical: bytes | None
    current: bytes | None
    proposed: bytes | None
    requires_choice: bool
    base: VersionRef
    history_head: VersionRef
    later_overlap: bool
    held: tuple[str, ...] = ()
    selection: RestoreSelection | None = None
    changes: tuple[RestoreUnitChange, ...] = ()
    authority_token: str = ""


@dataclass(frozen=True)
class RestorePreview:
    scope_key: str
    items: tuple[RestoreItem, ...]
    units: tuple[str, ...] = ("descriptions",)


@dataclass(frozen=True)
class RestoreOutcome:
    entity_id: str
    status: str
    message: str = ""
    version_id: str = ""


@dataclass(frozen=True)
class RestoreUnitComparison:
    unit: RestoreUnit
    current: str
    historical: str
    proposed: str
    changed: bool
    later_overlap: bool
    required_units: tuple[str, ...] = ()


@dataclass(frozen=True)
class RestoreComparison:
    entity_id: str
    units: tuple[RestoreUnitComparison, ...]
    proposed: bytes | None
    held: tuple[str, ...] = ()


def _annotation(raw: bytes) -> dict:
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise HistoryError("Duplicate JSON facts require explicit repair before restoration.")
            value[key] = item
        return value

    value = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value.get("schema_version") != SCHEMA_VERSION:
        raise HistoryError("This saved schema is not eligible for selective restoration.")
    return value


def _description_proposal(historical: dict, current: dict) -> tuple[dict, tuple[str, ...]]:
    """A closed Description unit; ambiguous linked authorities are held.

    Evidence payloads, Tags, review state, projection state, and unknown fields
    remain current. Only recognized description decisions may accompany text.
    """
    proposed = deepcopy(current)
    held = []
    for key in ("characters", "objects", "contexts"):
        if historical.get(key) != current.get(key):
            held.append(f"{key} dependencies changed; review a valid linked unit first.")
    if "plain_language" in historical:
        proposed["plain_language"] = deepcopy(historical["plain_language"])
    else:
        proposed.pop("plain_language", None)
    old_source, new_source = historical.get("source", {}), current.get("source", {})
    if not isinstance(old_source, dict) or not isinstance(new_source, dict):
        return proposed, ("Description source authority has an unsupported shape.",)
    source = proposed.setdefault("source", {})
    for key in ("rejected_descriptions",):
        if key in old_source:
            source[key] = deepcopy(old_source[key])
        else:
            source.pop(key, None)
    for key in ("finding_decisions", "dismissed_findings"):
        old, new = old_source.get(key, []), new_source.get(key, [])
        if not isinstance(old, list) or not isinstance(new, list):
            held.append(f"Unsupported {key} dependency.")
            continue
        selected = [deepcopy(item) for item in old if isinstance(item, dict) and item.get("kind") == "description"]
        preserved = [deepcopy(item) for item in new if not isinstance(item, dict) or item.get("kind") != "description"]
        if selected or preserved or key in new_source or key in old_source:
            source[key] = preserved + selected
        if selected and any(old_source.get(name) != new_source.get(name)
                            for name in ("source_fingerprints", "evidence")):
            held.append(f"{key} source attribution changed; review the linked Description unit first.")
    known = {"rejected_descriptions", "finding_decisions", "dismissed_findings"}
    for key in set(old_source) | set(new_source):
        if key not in known and any(word in key.casefold() for word in ("description", "identity", "context", "policy")):
            if (key in old_source) != (key in new_source) or old_source.get(key) != new_source.get(key):
                held.append(f"Linked {key} authority changed; selective restoration is held.")
    # Do not attach historical decisions to different raw/model evidence. A
    # future adapter may resolve these explicitly; H1 never infers that link.
    old_evidence = historical.get("ai_evidence", [])
    new_evidence = current.get("ai_evidence", [])
    if old_evidence != new_evidence:
        held.append("AI evidence or its decisions changed; review their linked Description unit first.")
    if not source and "source" not in current:
        proposed.pop("source", None)
    return proposed, tuple(held)


def _member(values, key):
    return [value for value in values if isinstance(value, str) and normalize_tag(value) == key]


def _unit_value(value, unit):
    from .image_record_recovery import _tag_locations
    if unit.kind in {"record", "record_absence"}:
        return value
    value = value or {}
    if unit.kind == "tags":
        source = value.get("source", {})
        return {
            "locations": _tag_locations(value).get(unit.key, ()),
            "identity": value.get("tag_identities", {}).get(unit.key),
            "absence": _member(value.get("confirmed_absent", []), unit.key),
            "exclusion": _member(value.get("export_excluded", []), unit.key),
            "rejected": _member(source.get("rejected_tags", []), unit.key),
        }
    if unit.kind in {"absence", "exclusion"}:
        return _member(value.get("confirmed_absent" if unit.kind == "absence" else "export_excluded", []), unit.key)
    if unit.kind in {"characters", "objects", "contexts"}:
        return next((item for item in value.get(unit.kind, []) if item.get("id") == unit.key), None)
    if unit.kind == "review":
        return ("review_status" in value, value.get("review_status"))
    if unit.kind == "decisions":
        source = value.get("source", {})
        return ({key: (key in source, source.get(key)) for key in ("rejected_tags", "rejected_descriptions", "finding_decisions", "dismissed_findings", "ownership_acknowledgements")},
                {item["id"]: item.get("decisions", {}) for item in value.get("ai_evidence", [])})
    if unit.kind == "descriptions":
        # Reuse the closed Description projection, excluding unchanged fields.
        proposal, _ = _description_proposal(value, {})
        return proposal
    raise HistoryError("Unsupported typed restore unit.")


def _expanded_units(selection, historical, base):
    from .image_record_recovery import _tag_locations
    result = []
    for unit in selection.units:
        if unit.kind not in SUPPORTED_RESTORE_UNITS:
            raise HistoryError("Unsupported typed restore unit.")
        keys = [unit.key]
        if not unit.key and unit.kind in {"tags", "absence", "exclusion"}:
            keys = set()
            for value in (historical or {}, base or {}):
                if unit.kind == "tags":
                    keys.update(_tag_locations(value))
                    keys.update(value.get("tag_identities", {}))
                fields = {"absence": ("confirmed_absent",), "exclusion": ("export_excluded",), "tags": ("confirmed_absent", "export_excluded")}[unit.kind]
                for field in fields:
                    keys.update(normalize_tag(item) for item in value.get(field, []))
            keys = sorted(keys)
        elif not unit.key and unit.kind in {"characters", "objects", "contexts"}:
            keys = sorted({item["id"] for value in (historical or {}, base or {}) for item in value.get(unit.kind, [])})
        elif unit.key and unit.kind not in {"tags", "absence", "exclusion", "characters", "objects", "contexts"}:
            raise HistoryError("This restore category does not accept a key.")
        for key in keys:
            item = RestoreUnit(unit.kind, key)
            if item not in result:
                result.append(item)
    if any(unit.kind in {"record", "record_absence"} for unit in result) and len(result) != 1:
        raise HistoryError("Whole-record/absence restoration cannot be combined with selective units.")
    # Restore stable owners before placing their tags.
    priority = {"characters": 0, "objects": 0, "contexts": 1, "tags": 2}
    return tuple(sorted(result, key=lambda item: priority.get(item.kind, 3)))


def _apply_unit(proposed, historical, unit):
    from .image_record_recovery import _replace_tag_locations, _container_at
    if unit.kind in {"record", "record_absence"}:
        return deepcopy(historical)
    if proposed is None or historical is None:
        raise HistoryError("Selective units require both records; choose whole-record/absence recovery.")
    state = _unit_value(historical, unit)
    if unit.kind == "tags":
        if any(_container_at(proposed, path) is None for path, _ in state["locations"]):
            raise HistoryError("Tag placement requires its character/object/context unit.")
        for path, _ in state["locations"]:
            if path[0] in {"characters", "objects", "contexts"}:
                owner_id = path[1][1:-1]
                old_owner = next(item for item in historical[path[0]] if item["id"] == owner_id)
                owner = next(item for item in proposed[path[0]] if item["id"] == owner_id)
                identity_keys = {"characters": ("character_id", "outfit_ids"), "objects": ("identity_tag_id",), "contexts": ("kind", "owner_kind", "owner_id")}[path[0]]
                if any(owner.get(key) != old_owner.get(key) for key in identity_keys):
                    raise HistoryError(f"Tag placement requires the historical {path[0]}:{owner_id} owner unit.")
        _replace_tag_locations(proposed, unit.key, state["locations"])
        identities = proposed.setdefault("tag_identities", {})
        if state["identity"] is not None:
            identities[unit.key] = state["identity"]
        else:
            identities.pop(unit.key, None)
        for field, key in (("confirmed_absent", "absence"), ("export_excluded", "exclusion")):
            proposed[field] = [item for item in proposed.get(field, []) if normalize_tag(item) != unit.key] + deepcopy(state[key])
        source = proposed.setdefault("source", {})
        if "rejected_tags" in source or "rejected_tags" in historical.get("source", {}):
            source["rejected_tags"] = [item for item in source.get("rejected_tags", []) if normalize_tag(item) != unit.key] + deepcopy(state["rejected"])
    elif unit.kind in {"absence", "exclusion"}:
        field = "confirmed_absent" if unit.kind == "absence" else "export_excluded"
        proposed[field] = [item for item in proposed.get(field, []) if normalize_tag(item) != unit.key] + deepcopy(state)
    elif unit.kind in {"characters", "objects", "contexts"}:
        values = proposed.setdefault(unit.kind, [])
        index = next((i for i, item in enumerate(values) if item.get("id") == unit.key), len(values))
        values[:] = [item for item in values if item.get("id") != unit.key]
        if state is not None:
            values.insert(index, deepcopy(state))
    elif unit.kind == "descriptions":
        proposed, _ = _description_proposal(historical, proposed)
    elif unit.kind == "review":
        if state[0]:
            proposed["review_status"] = state[1]
        else:
            proposed.pop("review_status", None)
    elif unit.kind == "decisions":
        source = proposed.setdefault("source", {})
        for key, (present, value) in state[0].items():
            if present:
                source[key] = deepcopy(value)
            else:
                source.pop(key, None)
        for item in proposed.get("ai_evidence", []):
            if item["id"] in state[1]:
                item["decisions"] = deepcopy(state[1][item["id"]])
    return proposed


def _validate_candidate(candidate, current, historical, selected, *, image, knowledge=None, characters=None):
    if candidate is None:
        return ()
    held = []
    annotation = ImageAnnotation.from_dict(candidate, image.name)
    if annotation.to_dict() != candidate:
        held.append("Canonical parsing would change unselected or unsupported facts.")
    try:
        for field in ("characters", "objects", "contexts", "ai_evidence", "generation_records"):
            ids = [item.get("id") for item in candidate.get(field, [])]
            if any(not item for item in ids) or len(ids) != len(set(ids)):
                raise HistoryError(f"{field} has missing or duplicate stable identities.")
        validate_image_contexts(annotation)
        present = {normalize_tag(item) for item in annotation.all_structured_tags()}
        absent = {normalize_tag(item) for item in annotation.confirmed_absent}
        excluded = {normalize_tag(item) for item in annotation.export_excluded}
        if present & absent or not excluded.issubset(present):
            raise HistoryError("Tags, confirmed absence and export exclusion require a valid linked unit.")
        from .image_record_recovery import _tag_locations
        old_tags = _tag_locations(current or {})
        new_tags = _tag_locations(candidate)
        old_annotation = ImageAnnotation.from_dict(current or {}, image.name)
        old_values = {normalize_tag(value) for value in old_annotation.all_structured_tags()}
        changed_values = [value for value in annotation.all_structured_tags()
                          if normalize_tag(value) not in old_values
                          or new_tags.get(normalize_tag(value)) != old_tags.get(normalize_tag(value))
                          or candidate.get("tag_identities", {}).get(normalize_tag(value)) != (current or {}).get("tag_identities", {}).get(normalize_tag(value))]
        from .confirmed_tag_identity import ConfirmedTagIdentityService
        if ConfirmedTagIdentityService.prune_annotation(deepcopy(annotation)):
            raise HistoryError("Removed facts require their Tag identity closure; select the affected Tags unit.")
        if changed_values:
            if knowledge is None:
                raise HistoryError("Changed Tags require explicit read-only System Knowledge authority.")
            resolution = ConfirmedTagIdentityService(knowledge).require_existing(changed_values, stored_ids=annotation.tag_identities, confirmation=False)
            if any(annotation.tag_identities.get(key) != identity for key, identity in resolution.identity_by_value.items()):
                raise HistoryError("Tags require the exact reviewed identity binding as a linked unit.")
            from .image_fact_decisions import tag_addition_decision_issues
            if tag_addition_decision_issues(annotation, changed_values, knowledge=knowledge):
                raise HistoryError("Restored Tags conflict with current image-level decisions; include their valid decision unit.")
        current_characters = {item.get("id"): item for item in (current or {}).get("characters", [])}
        for item in annotation.characters:
            prior = current_characters.get(item.id, {})
            if item.character_id and (item.character_id != prior.get("character_id") or item.outfit_ids != prior.get("outfit_ids", [])):
                if characters is None or item.character_id not in characters.characters:
                    raise HistoryError("Character assignment needs explicit current character authority.")
                if any(value not in characters.outfits for value in item.outfit_ids):
                    raise HistoryError("Character outfit identity is unavailable.")
                for outfit_id in item.outfit_ids:
                    owners = characters.associated_character_ids(outfit_id)
                    if owners and item.character_id not in owners:
                        raise HistoryError("Outfit is not associated with this character; shared/generic semantics must remain valid.")
        kinds = {unit.kind for unit in selected}
        if kinds & {"characters", "objects", "contexts"} and "descriptions" not in kinds and "record" not in kinds:
            if (current or {}).get("plain_language") != (historical or {}).get("plain_language"):
                raise HistoryError("Changed subject/context bindings require the linked Descriptions unit or an explicit current choice.")
        if "descriptions" in kinds and "record" not in kinds:
            _, reasons = _description_proposal(historical or {}, current or {})
            for reason in reasons:
                if any(reason.startswith(name + " dependencies") and candidate.get(name) == (historical or {}).get(name)
                       for name in ("characters", "objects", "contexts")):
                    continue
                if "decisions" in kinds and ("AI evidence" in reason or "source attribution" in reason):
                    continue
                held.append(reason)
        if "decisions" in kinds:
            def evidence(value):
                return [{key: item for key, item in row.items() if key != "decisions"} for row in value.get("ai_evidence", [])]
            if evidence(historical or {}) != evidence(current or {}):
                raise HistoryError("Raw AI evidence differs; decision attribution cannot be transplanted.")
            for key in ("source_fingerprints", "evidence"):
                if (historical or {}).get("source", {}).get(key) != (current or {}).get("source", {}).get(key):
                    raise HistoryError("Source attribution differs; decision restoration is held.")
    except (ValueError, RuntimeError) as exc:
        held.append(str(exc))
    return tuple(dict.fromkeys(held))


class ImageKnowledgeHistory:
    def __init__(self, store: HistoryStore, *, knowledge=None, characters=None, authority_guard=None):
        self.store = store
        self.knowledge = knowledge
        self.characters = characters
        self.authority_guard = authority_guard

    def context(self, image, *, operation_id=None, operation_kind="", reason="", producer=""):
        with _admitted_guard(self.authority_guard):
            entity = self.store.bind(image)
        return HistoryWriteContext(self.store, entity, operation_id or uuid4().hex,
                                   operation_kind=operation_kind, reason=reason, producer=producer,
                                   authority_guard=self.authority_guard)

    def reconcile(self, entity):
        with _admitted_guard(self.authority_guard):
            return self.store.reconcile(entity)

    def _authority_token(self, current, historical, units, cancelled):
        import hashlib
        kinds = {unit.kind for unit in units}
        needs_tags = bool(kinds & {"tags", "absence", "exclusion", "objects", "contexts", "characters", "record"})
        needs_characters = bool(kinds & {"characters", "record"})
        value = {"needs_tags": needs_tags, "needs_characters": needs_characters, "tags": [], "characters": [], "outfits": [], "source_stats": []}
        names, bindings, character_ids, outfit_ids = set(), set(), set(), set()
        if needs_tags or needs_characters:
            for raw in (current, historical):
                if raw is None:
                    continue
                annotation = ImageAnnotation.from_dict(raw, raw.get("image", ""))
                if needs_tags:
                    names.update(annotation.all_structured_tags())
                    names.update(annotation.confirmed_absent)
                    bindings.update(annotation.tag_identities.values())
                if needs_characters:
                    character_ids.update(item.character_id for item in annotation.characters if item.character_id)
                    outfit_ids.update(key for item in annotation.characters for key in item.outfit_ids)
        if sum(map(len, (names, bindings, character_ids, outfit_ids))) > MAX_AUTHORITY_ITEMS:
            raise HistoryError("Relevant authority exceeds its item budget; narrow the restore selection.")
        if needs_tags and self.knowledge is not None:
            for name in sorted(names):
                if cancelled():
                    raise HistoryPreviewCancelled("Authority comparison cancelled; no partial authorization.")
                record = self.knowledge.find(name)
                value["tags"].append((name, record.to_dict() if record else None))
            for key in sorted(bindings):
                record = self.knowledge.tags.get(key)
                value["tags"].append((key, record.to_dict() if record else None,
                                      key in self.knowledge.removed_tag_ids, self.knowledge.merged_tag_ids.get(key)))
        if needs_characters and self.characters is not None:
            for key in sorted(character_ids):
                record = self.characters.characters.get(key)
                value["characters"].append((key, record.to_dict() if record else None))
            for key in sorted(outfit_ids):
                record = self.characters.outfits.get(key)
                value["outfits"].append((key, record.to_dict() if record else None,
                                         self.characters.associated_character_ids(key)))
        sources = []
        if needs_tags and self.knowledge is not None:
            sources.append(self.knowledge.path)
        if needs_characters and self.characters is not None:
            sources.extend((self.characters.characters_path, self.characters.outfits_path))
        for path in sources:
            try:
                info = Path(path).stat()
                value["source_stats"].append((str(path), info.st_size, info.st_mtime_ns, info.st_ino))
            except FileNotFoundError:
                value["source_stats"].append((str(path), "missing"))
        digest, size = hashlib.sha256(), 0
        for chunk in json.JSONEncoder(sort_keys=True, ensure_ascii=False, separators=(",", ":")).iterencode(value):
            if cancelled():
                raise HistoryPreviewCancelled("Authority comparison cancelled; no partial authorization.")
            encoded = chunk.encode("utf-8")
            size += len(encoded)
            if size > MAX_AUTHORITY_BYTES:
                raise HistoryError("Relevant authority exceeds its byte budget; narrow the restore selection.")
            digest.update(encoded)
        return digest.hexdigest()

    @staticmethod
    def restore_capabilities(version):
        if "record-absence" in version.coverage:
            return ("record_absence",)
        if version.record_schema != SCHEMA_VERSION:
            return ()
        if "image-knowledge-record" in version.coverage:
            return tuple(kind for kind in SUPPORTED_RESTORE_UNITS if kind != "record_absence")
        return tuple(kind for kind in SUPPORTED_RESTORE_UNITS if kind in version.coverage)

    def _operation_pair(self, entity, target, base):
        if target.version_id == base.version_id:
            raise HistoryError("An operation restore requires distinct exact before and after versions.")
        current = base
        for _ in range(64):
            if current.parent_id == target.version_id:
                return
            if not current.parent_id:
                break
            current = self.store.version(entity, current.parent_id)
            if current.operation_id != base.operation_id:
                break
        raise HistoryError("The selected before/after pair is not one proven operation boundary.")

    def operation_selection(self, entity, operation_id, *, units=(RestoreUnit("descriptions"),)):
        """Select a proven operation's first before and final committed after.

        At most 64 exact operation versions are admitted; interrupted or
        interleaved captures remain unavailable instead of guessing a boundary.
        """
        if not isinstance(operation_id, str) or not operation_id:
            raise HistoryError("Select an exact operation identifier.")
        with self.store.locked():
            if self.store.has_pending(entity):
                raise HistoryError("History recovery is required before selecting an operation.")
            page = self.store.query(entity, operation_id=operation_id, limit=64)
            if page.next_cursor is not None:
                raise HistoryError("Operation exceeds the 64-version comparison bound.")
            versions = tuple(reversed(page.versions))
            if not versions:
                raise HistoryError("No committed operation versions are available for this image.")
            if versions[0].origin == "imported":
                if len(versions) != 2 or versions[0].parent_id or versions[1].parent_id != versions[0].version_id:
                    raise HistoryError("Imported operation boundary is incomplete or ambiguous.")
                target, base = versions
            else:
                saves = tuple(version for version in versions if version.origin not in {"baseline", "observed-current"})
                if not saves or not saves[0].parent_id:
                    raise HistoryError("Operation has no proven committed mutation.")
                target = self.store.version(entity, saves[0].parent_id)
                base = saves[-1]
                allowed = (target,) + saves if target.operation_id == operation_id else saves
                if versions != allowed or any(after.parent_id != before.version_id for before, after in zip((target,) + saves, saves)):
                    raise HistoryError("Operation contains interleaved or ambiguous saved boundaries.")
            self._operation_pair(entity, target, base)
            return RestoreSelection(entity, target.version_id, base.version_id, tuple(units), "operation")

    def compare_restore(self, preview, *, choices=None, cancelled=lambda: False):
        """Render frozen typed values through the same choice rules as apply.

        JSON strings keep comparison values immutable. This performs no history
        publication or source record reads; apply still revalidates the exact
        preview and all current authorities before writing.
        """
        if preview.scope_key != self.store.scope.key or not 1 <= len(preview.items) <= 1000:
            raise HistoryError("Comparison preview is outside its scope or item bound.")
        result, retained = [], 0
        for item in preview.items:
            if cancelled():
                raise HistoryPreviewCancelled("Comparison cancelled.")
            retained += len(item.current or b"") + len(item.historical or b"")
            if retained > MAX_PREVIEW_BYTES:
                raise HistoryError("Comparison exceeds its aggregate byte budget.")
            current = _annotation(item.current) if item.current is not None else None
            historical = _annotation(item.historical) if item.historical is not None else None
            if choices is not None and any(choices.get(item.entity.entity_id + ":" + change.unit.id, choices.get(item.entity.entity_id, "current")) not in {"current", "historical"} for change in item.changes):
                raise HistoryError("Comparison choices must be current or historical.")
            try:
                proposed, selected = self._proposal(current, historical, item.changes, choices=choices, entity_id=item.entity.entity_id)
                image = self.store.scope.library_root / item.entity.locator
                held = _validate_candidate(proposed, current, historical, selected, image=image, knowledge=self.knowledge, characters=self.characters) if selected else ()
            except HistoryError as exc:
                proposed, selected, held = deepcopy(current), (), (str(exc),)
            whole = any(unit.kind in {"record", "record_absence"} for unit in selected)
            raw = item.historical if whole else (item.current if proposed == current else (
                json.dumps(proposed, indent=2, ensure_ascii=False).encode("utf-8") + b"\n" if proposed is not None else None))
            comparisons = []
            for change in item.changes:
                values = tuple(json.dumps(_unit_value(value, change.unit), ensure_ascii=False, sort_keys=True) for value in (current, historical, proposed))
                retained += sum(len(value.encode("utf-8")) for value in values)
                if retained + len(raw or b"") > MAX_PREVIEW_BYTES:
                    raise HistoryError("Typed comparison exceeds its aggregate byte budget.")
                comparisons.append(RestoreUnitComparison(change.unit, *values, change.changed, change.later_overlap, change.required_units))
            retained += len(raw or b"")
            result.append(RestoreComparison(item.entity.entity_id, tuple(comparisons), raw, tuple(held)))
        return tuple(result)

    def _proposal(self, current, historical, changes, *, choices=None, entity_id=""):
        proposed = deepcopy(current)
        selected = []
        for change in changes:
            if not change.changed:
                continue
            choice = "historical" if choices is None else choices.get(entity_id + ":" + change.unit.id, choices.get(entity_id))
            if choice == "historical":
                proposed = _apply_unit(proposed, historical, change.unit)
                selected.append(change.unit)
        return proposed, tuple(selected)

    def preview_restore(self, selections, *, cancelled=lambda: False):
        if not selections or len(selections) > 1000:
            raise HistoryError("Select between one and 1000 exact image versions.")
        normalized = tuple(item if isinstance(item, RestoreSelection) else RestoreSelection(item[0], item[1]) for item in selections)
        if len({item.entity.entity_id for item in normalized}) != len(normalized):
            raise HistoryError("Select each image only once.")
        items, retained_bytes = [], 0

        def admit(size):
            if cancelled():
                raise HistoryPreviewCancelled("Comparison cancelled; no partial restore selection was authorized.")
            if retained_bytes + size > MAX_PREVIEW_BYTES:
                raise HistoryError("Comparison exceeds the aggregate byte budget; select fewer exact images.")

        with self.store.locked():
            self.store._ready()
            for selection in normalized:
                admit(0)
                entity = selection.entity
                image = self.store.validate_entity(entity)
                if self.store.has_pending(entity):
                    raise HistoryError("History recovery is pending; reconcile through admitted recovery before comparison.")
                target = self.store.version(entity, selection.target_version)
                head = self.store.current_head(entity) or self.store.query(entity, limit=1).versions[0]
                if selection.mode not in {"version", "operation"} or not selection.units:
                    raise HistoryError("Choose a supported restore mode and typed units.")
                if selection.mode == "operation" and not selection.base_version:
                    raise HistoryError("Saved operation Undo requires its exact after/base version.")
                base = self.store.version(entity, selection.base_version) if selection.base_version else head
                if selection.mode == "operation":
                    self._operation_pair(entity, target, base)
                selection = RestoreSelection(entity, target.version_id, base.version_id, selection.units, selection.mode)
                capabilities = self.restore_capabilities(target)
                if any(unit.kind not in capabilities for unit in selection.units):
                    raise HistoryError("This version's capture coverage does not authorize the selected restore units.")
                historical_raw = self.store.payload(entity, target.snapshot)
                admit(len(historical_raw or b""))
                current_raw = read_record(image.with_suffix(".tags.json"))
                base_raw = self.store.payload(entity, base.snapshot)
                admit(len(historical_raw or b"") + len(current_raw or b"") + len(base_raw or b""))
                historical = _annotation(historical_raw) if historical_raw is not None else None
                current = _annotation(current_raw) if current_raw is not None else None
                base_value = _annotation(base_raw) if base_raw is not None else None
                units = _expanded_units(selection, historical, base_value)
                authority = self._authority_token(current, historical, units, cancelled)
                changes = []
                for unit in units:
                    wanted, original, present = (_unit_value(value, unit) for value in (historical, base_value, current))
                    affected = selection.mode != "operation" or wanted != original
                    changed = affected and wanted != present
                    overlap = changed and present != original
                    required = ("tag identity/placement, absence/exclusion and image decisions",) if unit.kind == "tags" else ()
                    changes.append(RestoreUnitChange(unit, changed, overlap, required))
                held = ()
                try:
                    proposed, selected = self._proposal(current, historical, changes)
                    held = _validate_candidate(proposed, current, historical, selected, image=image, knowledge=self.knowledge, characters=self.characters)
                except HistoryError as exc:
                    proposed, held = deepcopy(current), (str(exc),)
                whole = any(unit.kind in {"record", "record_absence"} for unit in units)
                proposed_raw = historical_raw if whole and any(change.changed for change in changes) else (
                    json.dumps(proposed, indent=2, ensure_ascii=False).encode("utf-8") + b"\n" if proposed is not None else None)
                size = sum(len(value or b"") for value in (historical_raw, current_raw, proposed_raw))
                admit(size)
                items.append(RestoreItem(entity, target, record_token(current_raw), historical_raw, current_raw,
                                         proposed_raw, any(change.changed for change in changes), base, head,
                                         any(change.later_overlap for change in changes), held, selection, tuple(changes), authority))
                retained_bytes += size
        kinds = tuple(dict.fromkeys(unit.kind for item in items for unit in item.selection.units))
        return RestorePreview(self.store.scope.key, tuple(items), kinds)

    def apply_restore(self, preview, *, choices, cancelled=lambda: False):
        if preview.scope_key != self.store.scope.key or any(kind not in SUPPORTED_RESTORE_UNITS for kind in preview.units):
            raise HistoryError("Restore preview is outside this scope or uses unsupported units.")
        ids = [item.entity.entity_id for item in preview.items]
        if not ids or len(ids) != len(set(ids)) or len(ids) > 1000:
            raise HistoryError("Invalid restore item selection.")
        for item in preview.items:
            for change in item.changes:
                if change.changed and choices.get(item.entity.entity_id + ":" + change.unit.id, choices.get(item.entity.entity_id)) not in {"current", "historical"}:
                    raise HistoryError("Choose historical or current for every changed typed unit.")
        outcomes, operation_id = [], uuid4().hex
        for item in preview.items:
            if cancelled():
                outcomes.append(RestoreOutcome(item.entity.entity_id, "pending", "Cancelled before this image."))
                continue
            try:
                image = self.store.scope.image(self.store.scope.library_root / item.entity.locator)
                with _admitted_guard(self.authority_guard), self.store.locked(image):
                    if item.selection is None:
                        raise HistoryError("Restore selection lacks exact scope/base authority.")
                    fresh = self.preview_restore((item.selection,)).items[0]
                    if fresh != item:
                        raise HistoryError("Current/history/scope changed since preview; compare again.")
                    current = _annotation(item.current) if item.current is not None else None
                    historical = _annotation(item.historical) if item.historical is not None else None
                    proposed, selected = self._proposal(current, historical, item.changes, choices=choices, entity_id=item.entity.entity_id)
                    if not selected or proposed == current:
                        outcomes.append(RestoreOutcome(item.entity.entity_id, "kept-current"))
                        continue
                    held = _validate_candidate(proposed, current, historical, selected, image=image, knowledge=self.knowledge, characters=self.characters)
                    if held:
                        raise HistoryError("; ".join(held))
                    from .image_record import ImageRecord, ImageRecordEditSnapshot
                    from .image_record_recovery import ImageRecordRecoveryService
                    context = HistoryWriteContext(self.store, item.entity, operation_id, item.target.version_id,
                                                  operation_kind="history_restore", reason="Restore selected saved Image Knowledge", producer="collectigen",
                                                  restored_units=tuple(unit.id for unit in selected))
                    recovery = ImageRecordRecoveryService(backup_root=self.store.scope.history_root / "recovery", history_context=context)
                    if any(unit.kind in {"record", "record_absence"} for unit in selected):
                        recovery.restore_saved_record(image, expected=item.current, intended=item.historical)
                    else:
                        record = ImageRecord(image)
                        opening = ImageRecordEditSnapshot.capture(record)
                        record.annotation = ImageAnnotation.from_dict(proposed, image.name)
                        record.modified = True
                        plan = recovery.plan(opening, record)
                        if plan.conflicts or plan.materialize() != proposed:
                            raise HistoryError("Canonical recovery would change the approved proposal.")
                        recovery.apply(plan)
                    latest = self.store.query(item.entity, limit=1).versions[0]
                    outcomes.append(RestoreOutcome(item.entity.entity_id, "complete", version_id=latest.version_id))
            except HistoryWriteInterrupted as exc:
                outcomes.append(RestoreOutcome(item.entity.entity_id, "interrupted", str(exc)))
            except Exception as exc:
                outcomes.append(RestoreOutcome(item.entity.entity_id, "held", str(exc)))
        return tuple(outcomes)
