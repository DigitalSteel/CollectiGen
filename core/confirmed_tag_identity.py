from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Callable, Iterable, Mapping

from .tag_collection import TagCollection, normalize_tag


@dataclass(frozen=True)
class ConfirmedTagIdentityEntry:
    raw_value: str
    normalized_value: str
    status: str
    stable_id: str = ""
    canonical_display: str = ""
    candidate_ids: tuple[str, ...] = ()
    candidate_displays: tuple[str, ...] = ()
    message: str = ""

    @property
    def resolved(self) -> bool:
        return self.status in {"stored", "stored_redirect", "known", "created"}

    @property
    def needs_creation(self) -> bool:
        return self.status == "new"

    @property
    def blocked(self) -> bool:
        return self.status in {
            "ambiguous", "disabled_alias", "ignored", "tombstone",
            "missing_identity",
        }


@dataclass(frozen=True)
class ConfirmedTagIdentityPlan:
    entries: tuple[ConfirmedTagIdentityEntry, ...]

    @property
    def blockers(self) -> tuple[ConfirmedTagIdentityEntry, ...]:
        return tuple(entry for entry in self.entries if entry.blocked)

    @property
    def new_entries(self) -> tuple[ConfirmedTagIdentityEntry, ...]:
        return tuple(entry for entry in self.entries if entry.needs_creation)

    @property
    def resolved(self) -> bool:
        return bool(self.entries) and not self.blockers and not self.new_entries


@dataclass(frozen=True)
class ConfirmedTagIdentityResolution:
    values: tuple[str, ...]
    stable_ids: tuple[str, ...]
    identity_by_value: dict[str, str]
    created_ids: tuple[str, ...] = ()
    backup_path: Path | None = None


@dataclass(frozen=True)
class ConfirmedTagIdentityAudit:
    image_count: int
    confirmed_occurrences: int
    unique_values: int
    plan: ConfirmedTagIdentityPlan
    truncated: bool = False


@dataclass(frozen=True)
class ConfirmedTagUseAudit:
    image_count: int
    uses_by_tag_id: dict[str, tuple[str, ...]]
    uncertain_by_tag_id: dict[str, tuple[str, ...]]

    @property
    def used_paths(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            path
            for paths in self.uses_by_tag_id.values()
            for path in paths
        ))

    @property
    def uncertain_paths(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            path
            for paths in self.uncertain_by_tag_id.values()
            for path in paths
        ))


def removed_tag_guidance(knowledge, entry, recovery_record):
    """Read review guidance without treating choice labels as replacement facts."""
    archive=knowledge.removed_archive_for_tag(entry.stable_id)
    metadata=(getattr(recovery_record,"extras",{}) or {}).get("retirement_guidance",{})
    supported=(isinstance(metadata,dict) and metadata.get("schema_version")==1
               and metadata.get("reviewed") is True)
    archived_reason=str(getattr(archive,"reason","") or "").strip()
    reviewed_reason=str(metadata.get("reason") or "").strip() if supported else ""
    if archived_reason and reviewed_reason and archived_reason!=reviewed_reason:
        reason=f"Recorded removal: {archived_reason}\nReviewed retirement: {reviewed_reason}"
    else:
        reason=(archived_reason or reviewed_reason
                or str(getattr(recovery_record,"notes","") or "").strip()
                or "No removal reason was recorded. Review the image before choosing a replacement.")
    alternatives=[]
    rows=metadata.get("alternatives",[]) if supported else []
    if isinstance(rows,list):
        for row in rows[:60]:
            if not isinstance(row,dict) or not isinstance(row.get("label"),str):continue
            label=row["label"].strip()
            stable_id=row.get("tag_id","")
            if not label or not isinstance(stable_id,str):continue
            if stable_id:
                resolved=knowledge.resolve_tag_id(stable_id)
                record=knowledge.tags.get(resolved or "")
            else:
                plan=ConfirmedTagIdentityService.shared(knowledge).plan((label,),confirmation=False)
                record=knowledge.tags.get(plan.entries[0].stable_id) if plan.resolved else None
            if record is not None and record.id==entry.stable_id:continue
            alternatives.append((record.id if record else "",record.display if record else label))
    return reason,tuple(dict.fromkeys(alternatives))


def replace_retired_image_tag(annotation, stable_id, raw_values, replacements):
    """Apply a reviewed image-only replacement in its original structured scopes.

    Replacements are already-validated (stable ID, display) pairs. This function
    has no persistence or vocabulary-creation authority and leaves raw evidence.
    """
    raw_keys={normalize_tag(value) for value in raw_values}
    def matches(value):
        key=normalize_tag(value)
        return key in raw_keys or annotation.tag_identities.get(key)==stable_id
    owners=[item for item in annotation.objects if item.identity_tag_id==stable_id]
    contexts=[item for item in annotation.contexts if item.owner_kind=="tag" and item.owner_id==stable_id]
    if (owners or contexts) and len(replacements)!=1:
        raise ValueError("Choose one replacement for this Tag because it owns image details. Its existing details will be preserved.")
    containers=[annotation.sdxl_tags,annotation.uncategorized,annotation.visual_style.style_tags,annotation.excluded]
    containers.extend(annotation.global_tags.values()); containers.extend(annotation.scene.values())
    containers.extend(annotation.custom_groups.values())
    for character in annotation.characters:containers.extend(character.tags.values())
    for item in annotation.objects:containers.append(item.tags)
    for item in annotation.contexts:containers.append(item.tags)
    for values in containers:
        result=TagCollection()
        for value in values:
            result.extend(display for _id,display in replacements) if matches(value) else result.add(value)
        values[:]=result.to_list()
    for item in owners:item.identity_tag_id,item.identity_tag=replacements[0]
    for item in contexts:item.owner_id,item.owner_label=replacements[0]
    annotation.tag_identities={key:value for key,value in annotation.tag_identities.items()
                               if value!=stable_id and key not in raw_keys}
    annotation.tag_identities.update({normalize_tag(display):tag_id for tag_id,display in replacements})


class ConfirmedTagIdentityError(ValueError):
    def __init__(self, plan: ConfirmedTagIdentityPlan):
        self.plan = plan
        messages = [entry.message for entry in plan.blockers if entry.message]
        if not messages:
            messages = [
                f'"{entry.raw_value}" does not yet have a reusable System Knowledge identity.'
                for entry in plan.new_entries
            ]
        super().__init__(" ".join(messages) or "Confirmed SDXL Tag identity could not be resolved.")


class ConfirmedTagIdentityService:
    """Resolve confirmed tag strings to one durable System Knowledge identity."""

    def __init__(
        self,
        knowledge,
        *,
        evidence_rejected: Callable[[str], bool] | None = None,
    ) -> None:
        if knowledge is None:
            raise ValueError("System Knowledge is required to confirm SDXL Tags.")
        self.knowledge = knowledge
        self.evidence_rejected = evidence_rejected
        self._name_index_signature=None
        self._cached_name_index={}
        self._index_lock=RLock()

    @classmethod
    def shared(cls,knowledge):
        """Reuse one identity index for repeated saves against one authority."""
        service=getattr(knowledge,"_confirmed_tag_identity_service",None)
        if not isinstance(service,cls) or service.knowledge is not knowledge:
            service=cls(knowledge)
            knowledge._confirmed_tag_identity_service=service
        return service

    def _name_index(self) -> dict[str, tuple]:
        # Tag records remain directly editable while a nested Knowledge editor
        # is open. A cheap content signature keeps this cache current without
        # renormalizing and resorting every name for each image-editor refresh.
        with self._index_lock:
            signature=tuple(
                (record.id,record.display,tuple(record.aliases))
                for record in self.knowledge.tags.values()
            )+(
                bool(getattr(self.knowledge,"_alias_authority_enabled",False)),
                tuple(sorted(getattr(self.knowledge,"_canonical_alias_owners",{}).items())),
                tuple(sorted(getattr(self.knowledge,"_represented_alias_keys",set()))),
            )
            if signature==self._name_index_signature:
                return self._cached_name_index
            values: dict[str, dict[str, object]] = {}
            keys=(
                self.knowledge.identity_name_keys()
                if hasattr(self.knowledge,"identity_name_keys")
                else tuple({
                    normalize_tag(name)
                    for record in self.knowledge.tags.values()
                    for name in (record.display,*record.aliases)
                    if normalize_tag(name)
                })
            )
            for key in keys:
                owner_ids=(
                    self.knowledge.identity_owner_ids(key)
                    if hasattr(self.knowledge,"identity_owner_ids")
                    else tuple(
                        record.id for record in self.knowledge.tags.values()
                        if any(
                            normalize_tag(name)==key
                            for name in (record.display,*record.aliases)
                        )
                    )
                )
                for owner_id in owner_ids:
                    record=self.knowledge.tags.get(owner_id)
                    if record is not None:values.setdefault(key,{})[record.id]=record
            result={
                key: tuple(sorted(records.values(), key=lambda item: item.id.casefold()))
                for key, records in values.items()
            }
            self._name_index_signature=signature
            self._cached_name_index=result
            return result

    def _matches_by_key(self, keys: Iterable[str]) -> dict[str, tuple]:
        """Resolve a bounded set of names through the live Tag identity index.

        TagDatabase already maintains normalized display and Other Name owner
        maps. Rebuilding a second complete map for every selected image made a
        routine click scale with the entire vocabulary. Keep the complete scan
        only for older/fake knowledge implementations and for requested names
        whose maintained index may be stale after an in-memory direct edit.
        """
        requested = tuple(dict.fromkeys(str(key) for key in keys if str(key)))
        owner_ids = getattr(self.knowledge, "identity_owner_ids", None)
        identity_names = getattr(self.knowledge, "identity_names", None)
        if not callable(owner_ids):
            complete = self._name_index()
            return {key: complete.get(key, ()) for key in requested}

        result: dict[str, tuple] = {}
        unresolved: set[str] = set()
        for key in requested:
            records = {}
            for owner_id in owner_ids(key):
                record = self.knowledge.tags.get(owner_id)
                if record is None:
                    continue
                current_names = (
                    identity_names(record)
                    if callable(identity_names)
                    else (record.display, *record.aliases)
                )
                if any(normalize_tag(name) == key for name in current_names):
                    records[record.id] = record
            if records:
                result[key] = tuple(sorted(
                    records.values(), key=lambda item: item.id.casefold(),
                ))
            else:
                unresolved.add(key)

        if unresolved:
            scanned = {key: {} for key in unresolved}
            for record in self.knowledge.tags.values():
                current_names = (
                    identity_names(record)
                    if callable(identity_names)
                    else (record.display, *record.aliases)
                )
                for name in current_names:
                    key = normalize_tag(name)
                    if key in scanned:
                        scanned[key][record.id] = record
            for key, records in scanned.items():
                result[key] = tuple(sorted(
                    records.values(), key=lambda item: item.id.casefold(),
                ))
        return result

    def plan(
        self,
        values: Iterable[str],
        *,
        stored_ids: Mapping[str, str] | None = None,
        confirmation: bool = True,
    ) -> ConfirmedTagIdentityPlan:
        stored = {
            normalize_tag(value): str(stable_id)
            for value, stable_id in dict(stored_ids or {}).items()
            if normalize_tag(value) and str(stable_id).strip()
        }
        values = tuple(TagCollection(values))
        # A stored stable ID is authoritative for its value. Resolving every
        # such value through the live name index first made an image-only save
        # scan the complete vocabulary even though the result could not affect
        # the stored-ID decision (including a missing/removed-ID blocker).
        names = self._matches_by_key(
            key for value in values
            if (key:=normalize_tag(value)) and key not in stored
        )
        entries = []
        seen = set()
        for raw in values:
            value = str(raw).strip()
            key = normalize_tag(value)
            if not key or key in seen:
                continue
            seen.add(key)
            stored_id = stored.get(key, "")
            if stored_id:
                resolved_id = (
                    self.knowledge.resolve_tag_id(stored_id)
                    if hasattr(self.knowledge, "resolve_tag_id")
                    else stored_id if stored_id in self.knowledge.tags else None
                )
                record = self.knowledge.tags.get(resolved_id or "")
                if record is not None:
                    entries.append(ConfirmedTagIdentityEntry(
                        value, key,
                        "stored_redirect" if resolved_id != stored_id else "stored",
                        record.id, record.display,
                    ))
                    continue
                entries.append(ConfirmedTagIdentityEntry(
                    value, key, "missing_identity", stable_id=stored_id,
                    message=(
                        f'"{value}" points to removed System Knowledge identity '
                        f'"{stored_id}". Review the lifecycle change before saving.'
                    ),
                ))
                continue

            matches = names.get(key, ())
            if len(matches) > 1:
                entries.append(ConfirmedTagIdentityEntry(
                    value, key, "ambiguous",
                    candidate_ids=tuple(record.id for record in matches),
                    candidate_displays=tuple(record.display for record in matches),
                    message=(
                        f'"{value}" matches more than one canonical name or alias: '
                        + ", ".join(
                            f'{record.display} ({record.id})' for record in matches
                        )
                        + ". Choose the intended System Knowledge identity."
                    ),
                ))
                continue
            if (
                confirmation
                and self.evidence_rejected is not None
                and self.evidence_rejected(value)
            ):
                entries.append(ConfirmedTagIdentityEntry(
                    value, key, "ignored",
                    message=(
                        f'"{value}" is covered by an active incoming-evidence '
                        "rejection rule. Review that Automation rule before confirming it."
                    ),
                ))
                continue
            if len(matches) == 1:
                record = matches[0]
                entries.append(ConfirmedTagIdentityEntry(
                    value, key, "known", record.id, record.display,
                ))
                continue
            if key in getattr(self.knowledge,"_represented_alias_keys",set()):
                entries.append(ConfirmedTagIdentityEntry(
                    value,key,"disabled_alias",
                    message=(
                        f'"{value}" is represented by disabled, unreviewed, or '
                        "unavailable canonical Other Name history. Review that "
                        "System Knowledge identity before confirming or recreating it."
                    ),
                ))
                continue
            tombstones=(
                self.knowledge.removed_identity_ids(value)
                if hasattr(self.knowledge,"removed_identity_ids") else
                tuple(filter(None,(self.knowledge.removed_identity_id(value),)))
            )
            if len(tombstones)>1:
                displays=[]
                for stable_id in tombstones:
                    archive=(
                        self.knowledge.removed_archive_for_tag(stable_id)
                        if hasattr(self.knowledge,"removed_archive_for_tag") else None
                    )
                    record=archive.tag_record(stable_id) if archive is not None else None
                    displays.append(record.display if record is not None else stable_id)
                entries.append(ConfirmedTagIdentityEntry(
                    value,key,"ambiguous",
                    candidate_ids=tuple(tombstones),
                    candidate_displays=tuple(displays),
                    message=(
                        f'“{value}” matches more than one removed System Knowledge '
                        "identity: "+", ".join(
                            f"{display} ({stable_id})"
                            for display,stable_id in zip(displays,tombstones)
                        )+". Review removed vocabulary and choose the intended record."
                    ),
                ))
                continue
            tombstone=tombstones[0] if tombstones else None
            if tombstone:
                entries.append(ConfirmedTagIdentityEntry(
                    value, key, "tombstone", stable_id=tombstone,
                    message=(
                        f'"{value}" matches removed System Knowledge identity '
                        f'"{tombstone}". Review removed vocabulary instead of recreating it.'
                    ),
                ))
                continue
            entries.append(ConfirmedTagIdentityEntry(value, key, "new"))
        return ConfirmedTagIdentityPlan(tuple(entries))

    @staticmethod
    def _resolution(
        entries: Iterable[ConfirmedTagIdentityEntry],
        *,
        created_ids: Iterable[str] = (),
        backup_path: Path | None = None,
    ) -> ConfirmedTagIdentityResolution:
        values = []
        stable_ids = []
        identity_by_value = {}
        seen_ids = set()
        for entry in entries:
            if not entry.resolved or not entry.stable_id:
                continue
            identity_by_value[entry.normalized_value] = entry.stable_id
            canonical_key = normalize_tag(
                entry.canonical_display or entry.raw_value
            )
            if canonical_key:
                identity_by_value[canonical_key] = entry.stable_id
            if entry.stable_id in seen_ids:
                continue
            seen_ids.add(entry.stable_id)
            values.append(entry.canonical_display or entry.raw_value)
            stable_ids.append(entry.stable_id)
        return ConfirmedTagIdentityResolution(
            tuple(values),
            tuple(stable_ids),
            identity_by_value,
            tuple(created_ids),
            backup_path,
        )

    def require_existing(
        self,
        values: Iterable[str],
        *,
        stored_ids: Mapping[str, str] | None = None,
        confirmation: bool = True,
    ) -> ConfirmedTagIdentityResolution:
        plan = self.plan(
            values, stored_ids=stored_ids, confirmation=confirmation,
        )
        if plan.blockers or plan.new_entries:
            raise ConfirmedTagIdentityError(plan)
        return self._resolution(plan.entries)

    def canonical_displays(
        self,
        values: Iterable[str],
        *,
        stored_ids: Mapping[str, str] | None = None,
    ) -> tuple[str, ...]:
        """Return live preferred displays without mutating Image Knowledge.

        Stored stable IDs take precedence over the historical spelling saved in
        an image record.  Unresolved values remain visible exactly as stored so
        a read-only inspector never hides an identity problem.  Multiple
        spellings that resolve to one stable identity collapse to one display.
        """

        plan = self.plan(values, stored_ids=stored_ids, confirmation=False)
        displays: list[str] = []
        seen: set[tuple[str, str]] = set()
        for entry in plan.entries:
            if entry.resolved and entry.stable_id:
                identity = ("id", entry.stable_id)
                display = entry.canonical_display or entry.raw_value
            else:
                identity = ("value", entry.normalized_value)
                display = entry.raw_value
            if identity in seen:
                continue
            seen.add(identity)
            displays.append(display)
        return tuple(displays)

    def commit(
        self,
        plan: ConfirmedTagIdentityPlan,
        *,
        source: str,
        backup_root: str | Path | None = None,
        category: str = "uncategorized",
    ) -> ConfirmedTagIdentityResolution:
        if plan.blockers:
            raise ConfirmedTagIdentityError(plan)
        created = []
        backup = None
        try:
            for entry in plan.new_entries:
                record = self.knowledge.add_known_tag(
                    entry.raw_value,
                    category=str(category or "uncategorized"),
                    provenance={
                        "source": str(source),
                        "raw_value": entry.raw_value,
                        "action": "create_confirmed_tag_identity",
                    },
                )
                created.append(record.id)
            if created:
                backup = self.knowledge.save_verified(backup_root=backup_root)
        except BaseException:
            for stable_id in created:
                self.knowledge.tags.pop(stable_id, None)
            self.knowledge._rebuild_lookup()
            raise
        refreshed = self.plan(
            (entry.raw_value for entry in plan.entries),
            confirmation=False,
        )
        if refreshed.blockers or refreshed.new_entries:
            raise ConfirmedTagIdentityError(refreshed)
        return self._resolution(
            refreshed.entries, created_ids=created, backup_path=backup,
        )

    def restore_tombstone(
        self,
        entry: ConfirmedTagIdentityEntry,
        *,
        source: str,
        recovery_record,
        backup_root: str | Path | None = None,
    ) -> ConfirmedTagIdentityResolution:
        """Restore one removed identity from complete reviewed recovery data."""
        return self.restore_tombstones(
            (entry,),source=source,
            recovery_records={entry.stable_id:recovery_record},
            backup_root=backup_root,
        )

    def restore_tombstones(
        self,
        entries: Iterable[ConfirmedTagIdentityEntry],
        *,
        source: str,
        recovery_records: Mapping[str, object],
        backup_root: str | Path | None = None,
    ) -> ConfirmedTagIdentityResolution:
        """Restore reviewed removed identities in one verified transaction."""
        requested=tuple({
            entry.stable_id:entry for entry in entries
        }.values())
        if not requested or any(
            entry.status not in {"tombstone","missing_identity"}
            or not entry.stable_id
            for entry in requested
        ):
            raise ConfirmedTagIdentityError(ConfirmedTagIdentityPlan(requested))
        archived=[
            entry for entry in requested
            if getattr(self.knowledge,"removed_archive_for_tag",lambda _value:None)(
                entry.stable_id
            ) is not None
        ]
        if archived:
            if len(archived)!=len(requested):
                raise ValueError(
                    "Archived and legacy ID-only removals cannot be restored in one "
                    "transaction. Review each recovery source separately."
                )
            categories=getattr(self.knowledge,"_categories",None)
            if categories is None or not all(
                hasattr(categories,attribute)
                for attribute in ("default_path","user_path","categories")
            ):
                from .category_registry import CategoryRegistry
                parent=self.knowledge.path.parent
                defaults=(parent.parent/"defaults" if parent.name.casefold()=="user" else parent/"defaults")
                categories=CategoryRegistry(
                    defaults/"categories.json",parent/"categories.json",
                )
            from .tag_order_workspace import CategoryWorkspaceSession
            session=CategoryWorkspaceSession(
                self.knowledge,categories,
                default_relationship_path=categories.default_path.with_name(
                    "relationships.json"
                ),
                user_relationship_path=self.knowledge.path.with_name(
                    "relationships.json"
                ),
            )
            session.restore_tags(
                (entry.stable_id for entry in archived),source=source,
            )
            result=session.save()
            self.knowledge.load()
            refreshed=self.plan(
                (entry.raw_value for entry in requested),confirmation=False,
            )
            if refreshed.blockers or refreshed.new_entries:
                raise ConfirmedTagIdentityError(refreshed)
            return self._resolution(
                refreshed.entries,backup_path=result.backup_directory,
            )
        restored=[]; backup=None
        try:
            for entry in requested:
                record=self.knowledge.restore_removed_tag(
                    entry.raw_value,stable_id=entry.stable_id,
                    recovery_record=recovery_records.get(entry.stable_id),
                    provenance={
                        "source":str(source),
                        "raw_value":entry.raw_value,
                        "action":"restore_confirmed_tag_identity",
                    },
                )
                restored.append(record)
            refreshed=self.plan(
                (entry.raw_value for entry in requested),confirmation=False,
            )
            if refreshed.blockers or refreshed.new_entries:
                raise ConfirmedTagIdentityError(refreshed)
            backup=self.knowledge.save_verified(backup_root=backup_root)
        except BaseException:
            for record in restored:
                self.knowledge.tags.pop(record.id,None)
                self.knowledge.removed_tag_ids.add(record.id)
            self.knowledge._rebuild_lookup()
            raise
        return self._resolution(refreshed.entries,backup_path=backup)

    def reconcile_annotation(
        self,
        annotation,
        *,
        source: str,
        backup_root: str | Path | None = None,
    ) -> ConfirmedTagIdentityResolution:
        values = annotation.all_structured_tags()
        plan = self.plan(
            values,
            stored_ids=getattr(annotation, "tag_identities", {}),
            confirmation=True,
        )
        resolution = self.commit(
            plan, source=source, backup_root=backup_root,
        )
        self.bind_annotation(annotation, resolution, values=values)
        return resolution

    def audit_annotations(
        self,
        annotations: Iterable,
        *,
        max_images: int | None = None,
    ) -> ConfirmedTagIdentityAudit:
        values = []
        stored = {}
        conflicting_stored: dict[str, set[str]] = {}
        image_count = 0
        occurrences = 0
        truncated = False
        for annotation in annotations:
            if max_images is not None and image_count >= max(0, int(max_images)):
                truncated = True
                break
            image_count += 1
            confirmed = annotation.all_structured_tags()
            occurrences += len(confirmed)
            values.extend(confirmed)
            confirmed_keys = {
                normalize_tag(value)
                for value in confirmed
                if normalize_tag(value)
            }
            for raw_value, stable_id in dict(
                getattr(annotation, "tag_identities", {})
            ).items():
                key = normalize_tag(raw_value)
                stable_id = str(stable_id).strip()
                if not key or key not in confirmed_keys or not stable_id:
                    continue
                if hasattr(self.knowledge, "resolve_tag_id"):
                    stable_id = self.knowledge.resolve_tag_id(stable_id) or stable_id
                prior = stored.get(key)
                if prior and prior != stable_id:
                    conflicting_stored.setdefault(key, {prior}).add(stable_id)
                else:
                    stored[key] = stable_id
        plan = self.plan(values, stored_ids=stored, confirmation=True)
        if conflicting_stored:
            replacements = []
            for entry in plan.entries:
                conflict = conflicting_stored.get(entry.normalized_value)
                if conflict:
                    replacements.append(ConfirmedTagIdentityEntry(
                        entry.raw_value,
                        entry.normalized_value,
                        "ambiguous",
                        candidate_ids=tuple(sorted(conflict)),
                        message=(
                            f'"{entry.raw_value}" is linked to more than one stored '
                            "System Knowledge identity across image records: "
                            + ", ".join(sorted(conflict))
                            + ". Review the affected records before migration."
                        ),
                    ))
                else:
                    replacements.append(entry)
            plan = ConfirmedTagIdentityPlan(tuple(replacements))
        return ConfirmedTagIdentityAudit(
            image_count,
            occurrences,
            len(plan.entries),
            plan,
            truncated,
        )

    @staticmethod
    def audit_cached_uses(
        tag_records: Iterable,
        image_records: Iterable,
        *,
        names_for: Callable[[object], Iterable[str]] | None = None,
    ) -> ConfirmedTagUseAudit:
        """Find indexed confirmed uses before a reusable identity is removed."""

        names: dict[str, set[str]] = {}
        tag_ids = []
        for record in tag_records:
            stable_id = str(record.id)
            tag_ids.append(stable_id)
            values=(
                names_for(record) if names_for is not None
                else (record.display,*record.aliases)
            )
            for value in values:
                key = normalize_tag(value)
                if key:
                    names.setdefault(key, set()).add(stable_id)

        uses = {stable_id: [] for stable_id in tag_ids}
        uncertain = {stable_id: [] for stable_id in tag_ids}
        image_count = 0
        for image in image_records:
            image_count += 1
            path = str(image.path)
            source_map = {
                normalize_tag(value): tuple(
                    str(source) for source in sources
                )
                for value, sources in dict(
                    getattr(image, "tag_sources", {}) or {}
                ).items()
                if normalize_tag(value)
            }
            for value in getattr(image, "tags", ()) or ():
                key = normalize_tag(value)
                matching_ids = names.get(key, ())
                if not matching_ids:
                    continue
                sources = source_map.get(key, ())
                for stable_id in matching_ids:
                    if "Confirmed Image Knowledge" in sources:
                        uses[stable_id].append(path)
                    elif (
                        not sources
                        and bool(getattr(image, "sidecar_exists", True))
                    ):
                        uncertain[stable_id].append(path)

        return ConfirmedTagUseAudit(
            image_count,
            {
                stable_id: tuple(dict.fromkeys(paths))
                for stable_id, paths in uses.items()
                if paths
            },
            {
                stable_id: tuple(dict.fromkeys(paths))
                for stable_id, paths in uncertain.items()
                if paths
            },
        )

    def synchronize_annotation(self, annotation) -> bool:
        values = annotation.all_structured_tags()
        resolution = self.require_existing(
            values,
            stored_ids=getattr(annotation, "tag_identities", {}),
            confirmation=False,
        )
        return self.bind_annotation(annotation, resolution, values=values)

    @staticmethod
    def bind_annotation(annotation, resolution, *, values: Iterable[str]) -> bool:
        current = {
            normalize_tag(value): str(stable_id)
            for value, stable_id in dict(
                getattr(annotation, "tag_identities", {})
            ).items()
            if normalize_tag(value) and str(stable_id).strip()
        }
        confirmed_keys = {
            normalize_tag(value)
            for value in values
            if normalize_tag(value)
        }
        updated = {
            key: stable_id
            for key, stable_id in current.items()
            if key in confirmed_keys
        }
        updated.update({
            key: stable_id
            for key, stable_id in resolution.identity_by_value.items()
            if key in confirmed_keys
        })
        if updated == current:
            return False
        annotation.tag_identities = updated
        return True

    @staticmethod
    def prune_annotation(annotation) -> bool:
        """Discard identity links for facts that are no longer confirmed."""

        confirmed_keys = {
            normalize_tag(value)
            for value in annotation.all_structured_tags()
            if normalize_tag(value)
        }
        current = {
            normalize_tag(value): str(stable_id)
            for value, stable_id in dict(
                getattr(annotation, "tag_identities", {})
            ).items()
            if normalize_tag(value) and str(stable_id).strip()
        }
        updated = {
            key: stable_id
            for key, stable_id in current.items()
            if key in confirmed_keys
        }
        if updated == current:
            return False
        annotation.tag_identities = updated
        return True


@dataclass(frozen=True)
class ImageTagOccurrence:
    owner_kind: str
    owner_id: str
    owner_group: str
    label: str
    values: tuple[str, ...]

    @property
    def scope_ref(self):
        return f"{self.owner_kind}:{self.owner_id}:{self.owner_group}"


def image_tag_occurrences(annotation, value):
    """Read exact native containers; never flatten equal facts on different owners."""
    key=normalize_tag(value); result=[]
    def add(kind,owner,group,label,values):
        if any(normalize_tag(v)==key for v in values):
            result.append(ImageTagOccurrence(kind,owner,group,label,tuple(values)))
    add('image','','sdxl_tags','Image-wide',annotation.sdxl_tags)
    # Uncategorized is retained but cannot silently be moved into another scope.
    for kind,groups in (('global',annotation.global_tags),('scene',annotation.scene),
                        ('custom_group',annotation.custom_groups)):
        for group,values in groups.items():add(kind,'',group,f'{kind}: {group}',values)
    add('visual_style','','style_tags','Visual style',annotation.visual_style.style_tags)
    for owner in annotation.characters:
        for group,values in owner.tags.items():
            add('character',owner.id,group,f'Character {owner.slot}: {group}',values)
    for owner in annotation.objects:add('object',owner.id,'tags',f'Object {owner.slot}',owner.tags)
    for owner in annotation.contexts:add('context',owner.id,'tags',owner.label or f'Context {owner.kind}',owner.tags)
    return tuple(result)


def active_refinement_inputs(knowledge, categories, relationships, source_id):
    """Bounded projection from native live records/slots, without text inference or IO."""
    from .knowledge_relationships import ADVISORY_ASSOCIATION, SPECIFICITY, MODIFIER_VALUE
    from .modifier_composition import modifier_composition_resolutions, EffectiveModifierComposition
    records=knowledge.tags
    def available(tid):
        return (tid in records and tid not in getattr(knowledge,'removed_tag_ids',())
                and getattr(knowledge,'resolve_tag_id',lambda value:value)(tid)==tid)
    if not available(source_id):raise ValueError('source_identity_missing')
    relationships=tuple(relationships)
    source_rows=tuple(r for r in relationships if r.kind==MODIFIER_VALUE
                      and r.endpoints and r.endpoints[0].id==source_id)
    source_slots=modifier_composition_resolutions(source_rows,tags=knowledge,categories=categories)
    bases={row.base_tag_id for row in source_slots.get(source_id,{}).values() if row.is_set}
    sources={source_id}|(bases if len(bases)==1 else set())
    links=tuple(r for r in relationships if r.reviewed and not r.disabled and r.version==1
                and r.directed and len(r.endpoints)==2 and all(e.kind=='tag' for e in r.endpoints)
                and ((r.kind==ADVISORY_ASSOCIATION and r.endpoints[0].id in sources)
                     or (r.kind==SPECIFICITY and r.endpoints[1].id in sources)))
    candidates=tuple(dict.fromkeys(r.endpoints[1 if r.kind==ADVISORY_ASSOCIATION else 0].id for r in links))
    wanted=sources|set(candidates)
    # Existing compounds only: exact base ID, not spelling/category similarity.
    wanted.update(r.endpoints[0].id for r in relationships if r.kind==MODIFIER_VALUE and r.endpoints
                  and r.details.get('base_tag_id') in candidates)
    if len(wanted)>256 or len(candidates)>64:raise ValueError('refinement_input_bound')
    slots=tuple(r for r in relationships if r.kind==MODIFIER_VALUE and r.endpoints and r.endpoints[0].id in wanted)
    if len(slots)+len(links)>1024:raise ValueError('refinement_input_bound')
    resolved=modifier_composition_resolutions(slots,tags=knowledge,categories=categories)
    displays={tid:records[tid].display for tid in sorted(wanted) if available(tid)}
    compositions=[]
    for tid in displays:
        canonical=tuple(resolved.get(tid,{}).values())
        values=tuple(row.value_id for row in canonical if row.is_set)
        compatibility=tuple(str(v) for v in records[tid].attributes if str(v) not in values)
        compositions.append(EffectiveModifierComposition(tid,(*values,*compatibility),canonical,compatibility))
    return dict(displays=displays,relationships=links,compositions=tuple(compositions),candidate_ids=candidates)


def retired_refinement_hold(knowledge, stable_id):
    """Recovery is not complete historical composition authority.

    Current native archives omit default-only slots and carry no contract for
    snapshot version, provenance or completeness. Do not accept extras claims,
    current-default fallback records, or disabled live slots as historical proof.
    """
    archive=knowledge.removed_archive_for_tag(stable_id)
    if archive is None:return 'Historical source archive is unavailable.'
    return ('Historical replacement needs a complete, versioned original modifier '
            'snapshot with verified provenance. This recovery archive does not establish '
            'that authority. Keep or skip this review; no replacement was staged.')
