from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Iterable

from .tag_collection import normalize_tag


_STORE_LOCK = RLock()


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        str(value).strip() for value in values if str(value).strip()
    ))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class IdentityCleanupNotice:
    """Rebuildable cached state for reviewed image-identity maintenance."""

    active: bool = False
    recovery_available: bool = False
    stable_ids: tuple[str, ...] = ()
    source_names: tuple[str, ...] = ()
    change_labels: tuple[str, ...] = ()
    audit_state: str = "needs_audit"
    cached_possible_count: int | None = None
    library_image_count: int = 0
    candidate_count: int = 0
    remaining_count: int = 0
    safe_count: int = 0
    held_count: int = 0
    overlapping_warning_count: int = 0
    updated_at: str = ""

    def handoff_scope(self) -> dict:
        label=(
            self.change_labels[0]
            if len(self.change_labels)==1 else
            "Review pending System Knowledge image cleanup"
        )
        return {
            "kind":"alias_cleanup",
            "origin":"dashboard",
            "stable_ids":self.stable_ids,
            "source_names":self.source_names,
            "label":label,
        }


@dataclass(frozen=True)
class ProjectionUpdateNotice:
    """Rebuildable scope for derived outputs affected by Knowledge changes."""

    active: bool = False
    affected_tag_ids: tuple[str, ...] = ()
    changed_category_ids: tuple[str, ...] = ()
    change_labels: tuple[str, ...] = ()
    cached_affected_image_count: int | None = None
    captions_pending: bool = False
    metadata_pending: bool = False
    organization_pending: bool = False
    updated_at: str = ""

    @property
    def pending_targets(self) -> tuple[str, ...]:
        return tuple(
            target for target,pending in (
                ("captions",self.captions_pending),
                ("metadata",self.metadata_pending),
                ("organization",self.organization_pending),
            ) if pending
        )

    def handoff_scope(self,target: str) -> dict:
        target=str(target or "")
        if target not in self.pending_targets:
            raise ValueError("The requested downstream projection is not pending.")
        return {
            "kind":"knowledge_projection_update",
            "origin":"dashboard",
            "target":target,
            "stable_ids":self.affected_tag_ids,
            "category_ids":self.changed_category_ids,
            "scope_label":"images affected by saved System Knowledge projection changes",
            "notice_updated_at":self.updated_at,
        }


@dataclass(frozen=True)
class DownstreamChangeClassification:
    """Path-free consequences of one reviewed System Knowledge change."""

    change_kind: str
    label: str = ""
    affected_tag_ids: tuple[str, ...] = ()
    source_names: tuple[str, ...] = ()
    changed_category_ids: tuple[str, ...] = ()
    blocking_issues: tuple[str, ...] = ()
    automatic_refreshes: tuple[str, ...] = ()
    matching_images_pending: bool = False
    cached_matching_image_count: int | None = None
    captions_pending: bool = False
    metadata_pending: bool = False
    organization_pending: bool = False
    cached_projection_image_count: int | None = None

    @property
    def pending_targets(self) -> tuple[str, ...]:
        return tuple(
            target for target,pending in (
                ("matching_images",self.matching_images_pending),
                ("captions",self.captions_pending),
                ("metadata",self.metadata_pending),
                ("organization",self.organization_pending),
            ) if pending
        )

    @property
    def classification_kinds(self) -> tuple[str, ...]:
        values=[]
        if self.blocking_issues:values.append("blocking")
        if self.automatic_refreshes:values.append("automatic_refresh")
        if self.matching_images_pending:values.append("matching_images")
        if any((
            self.captions_pending,self.metadata_pending,
            self.organization_pending,
        )):
            values.append("derived_outputs")
        return tuple(values) or ("current",)

    def completion_summary(self) -> str:
        """Explain unchanged authority, automatic work, blocks, and optional jobs."""

        current=["confirmed Image Knowledge","source images"]
        if not self.captions_pending:current.append("Training TXT")
        if not self.metadata_pending:current.append("managed Metadata")
        if not self.organization_pending:current.append("folder organization")
        parts=["Current: "+", ".join(current)+"."]
        if self.automatic_refreshes:
            parts.append(
                "Refreshes automatically: "+", ".join(self.automatic_refreshes)+"."
            )
        if self.blocking_issues:
            parts.append("Blocked for review: "+"; ".join(self.blocking_issues)+".")
        labels={
            "matching_images":"Update Matching Images",
            "captions":"Update Captions",
            "metadata":"Update Metadata",
            "organization":"Review Organization",
        }
        if self.pending_targets:
            parts.append(
                "Review Related Updates on Dashboard for optional follow-ups: "+", ".join(
                    labels[target] for target in self.pending_targets
                )+"."
            )
        else:
            parts.append("No optional image or file update is pending.")
        return " ".join(parts)


def classify_system_knowledge_change(
    change_kind: str,*,label: str="",
    affected_tag_ids: Iterable[str]=(),source_names: Iterable[str]=(),
    changed_category_ids: Iterable[str]=(),blocking_issues: Iterable[str]=(),
    automatic_refreshes: Iterable[str]|None=None,
    matching_images: bool=False,cached_matching_image_count: int|None=None,
    impact=None,captions: bool|None=None,metadata: bool|None=None,
    organization: bool|None=None,
    cached_projection_image_count: int|None=None,
) -> DownstreamChangeClassification:
    """Classify one System Knowledge mutation without reading image authority."""

    kind=str(change_kind or "routine_change").strip()
    impact_tags=tuple(getattr(impact,"affected_tag_ids",()) or ())
    impact_categories=tuple(getattr(impact,"changed_categories",()) or ())
    captions=(
        bool(getattr(impact,"captions_may_be_stale",False))
        if captions is None else bool(captions)
    )
    metadata=(
        bool(getattr(impact,"metadata_may_be_stale",False))
        if metadata is None else bool(metadata)
    )
    organization=(
        bool(getattr(impact,"organization_affected",False))
        if organization is None else bool(organization)
    )
    if cached_projection_image_count is None and impact is not None:
        cached_projection_image_count=getattr(impact,"affected_images",None)
    if automatic_refreshes is None:
        automatic=["open presentation","rebuildable library indexes"]
        if kind in {
            "tag_created","tag_details","tag_renamed","tag_aliases",
            "tag_membership","tag_removal","tag_identity_merge",
            "tag_relationships","category_details","category_structure",
            "modifier_policy","caption_order","routine_change",
        }:
            automatic.append("derived results and Review questions")
        if kind in {"content_sensitivity_only","category_sensitivity"}:
            automatic.append("content-safety presentation")
        automatic_refreshes=automatic
    def count(value):
        if value is None:return None
        try:return max(0,int(value))
        except (TypeError,ValueError):return None
    resolved_tag_ids=_unique((*affected_tag_ids,*impact_tags))
    has_stable_scope=bool(resolved_tag_ids)
    return DownstreamChangeClassification(
        change_kind=kind,label=str(label or "").strip(),
        affected_tag_ids=resolved_tag_ids,
        source_names=_unique(source_names),
        changed_category_ids=_unique((
            *changed_category_ids,*impact_categories,
        )),
        blocking_issues=_unique(blocking_issues),
        automatic_refreshes=_unique(automatic_refreshes),
        matching_images_pending=bool(matching_images and has_stable_scope),
        cached_matching_image_count=count(cached_matching_image_count),
        captions_pending=bool(captions and has_stable_scope),
        metadata_pending=bool(metadata and has_stable_scope),
        organization_pending=bool(organization and has_stable_scope),
        cached_projection_image_count=count(cached_projection_image_count),
    )


class DownstreamUpdateStore:
    """Persist non-authoritative downstream-work notices without scanning.

    The file is a rebuildable coordination cache. It contains only reusable
    System Knowledge scope and aggregate counts, never image paths or evidence.
    """

    SCHEMA_VERSION = 2

    def __init__(self, path: str | Path) -> None:
        self.path=Path(path)

    @staticmethod
    def _nonnegative(value) -> int:
        try:return max(0,int(value))
        except (TypeError,ValueError):return 0

    @classmethod
    def _decode_identity(cls,payload) -> IdentityCleanupNotice:
        if not isinstance(payload,dict):return IdentityCleanupNotice()
        item=payload.get("identity_cleanup")
        if not isinstance(item,dict):
            return IdentityCleanupNotice()
        audit_state=str(item.get("audit_state") or "needs_audit")
        if audit_state not in {"needs_audit","audited","verification_needed"}:
            audit_state="needs_audit"
        possible=item.get("cached_possible_count")
        possible=(None if possible is None else cls._nonnegative(possible))
        return IdentityCleanupNotice(
            active=bool(item.get("active",False)),
            recovery_available=bool(item.get("recovery_available",False)),
            stable_ids=_unique(item.get("stable_ids") or ()),
            source_names=_unique(item.get("source_names") or ()),
            change_labels=_unique(item.get("change_labels") or ()),
            audit_state=audit_state,
            cached_possible_count=possible,
            library_image_count=cls._nonnegative(item.get("library_image_count")),
            candidate_count=cls._nonnegative(item.get("candidate_count")),
            remaining_count=cls._nonnegative(item.get("remaining_count")),
            safe_count=cls._nonnegative(item.get("safe_count")),
            held_count=cls._nonnegative(item.get("held_count")),
            overlapping_warning_count=cls._nonnegative(
                item.get("overlapping_warning_count")
            ),
            updated_at=str(item.get("updated_at") or ""),
        )

    @classmethod
    def _decode_projection(cls,payload) -> ProjectionUpdateNotice:
        if not isinstance(payload,dict):return ProjectionUpdateNotice()
        item=payload.get("projection_update")
        if not isinstance(item,dict) or not bool(item.get("active",False)):
            return ProjectionUpdateNotice()
        captions=bool(item.get("captions_pending",False))
        metadata=bool(item.get("metadata_pending",False))
        organization=bool(item.get("organization_pending",False))
        if not (captions or metadata or organization):
            return ProjectionUpdateNotice()
        possible=item.get("cached_affected_image_count")
        possible=(None if possible is None else cls._nonnegative(possible))
        return ProjectionUpdateNotice(
            active=True,
            affected_tag_ids=_unique(item.get("affected_tag_ids") or ()),
            changed_category_ids=_unique(
                item.get("changed_category_ids") or ()
            ),
            change_labels=_unique(item.get("change_labels") or ()),
            cached_affected_image_count=possible,
            captions_pending=captions,
            metadata_pending=metadata,
            organization_pending=organization,
            updated_at=str(item.get("updated_at") or ""),
        )

    def _read_payload(self) -> dict:
        try:
            payload=json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError,ValueError,TypeError):
            return {}
        return payload if isinstance(payload,dict) else {}

    def load(self) -> IdentityCleanupNotice:
        with _STORE_LOCK:
            return self._decode_identity(self._read_payload())

    def load_projection(self) -> ProjectionUpdateNotice:
        with _STORE_LOCK:
            return self._decode_projection(self._read_payload())

    def _write_states(
        self,identity: IdentityCleanupNotice,
        projection: ProjectionUpdateNotice,
    ) -> None:
        payload={
            "schema_version":self.SCHEMA_VERSION,
            "identity_cleanup":{
                "active":identity.active,
                "recovery_available":identity.recovery_available,
                "stable_ids":list(identity.stable_ids),
                "source_names":list(identity.source_names),
                "change_labels":list(identity.change_labels),
                "audit_state":identity.audit_state,
                "cached_possible_count":identity.cached_possible_count,
                "library_image_count":identity.library_image_count,
                "candidate_count":identity.candidate_count,
                "remaining_count":identity.remaining_count,
                "safe_count":identity.safe_count,
                "held_count":identity.held_count,
                "overlapping_warning_count":identity.overlapping_warning_count,
                "updated_at":identity.updated_at,
            },
            "projection_update":{
                "active":projection.active,
                "affected_tag_ids":list(projection.affected_tag_ids),
                "changed_category_ids":list(
                    projection.changed_category_ids
                ),
                "change_labels":list(projection.change_labels),
                "cached_affected_image_count":(
                    projection.cached_affected_image_count
                ),
                "captions_pending":projection.captions_pending,
                "metadata_pending":projection.metadata_pending,
                "organization_pending":projection.organization_pending,
                "updated_at":projection.updated_at,
            },
        }
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_suffix(self.path.suffix+".tmp")
        temporary.write_text(
            json.dumps(payload,indent=2,ensure_ascii=False)+"\n",
            encoding="utf-8",newline="\n",
        )
        temporary.replace(self.path)

    def _write(self,state: IdentityCleanupNotice) -> IdentityCleanupNotice:
        with _STORE_LOCK:
            projection=self._decode_projection(self._read_payload())
            self._write_states(state,projection)
        return state

    def _write_projection(
        self,state: ProjectionUpdateNotice,
    ) -> ProjectionUpdateNotice:
        with _STORE_LOCK:
            identity=self._decode_identity(self._read_payload())
            self._write_states(identity,state)
        return state

    def clear(self) -> IdentityCleanupNotice:
        current=self.load()
        return self._write(IdentityCleanupNotice(
            recovery_available=current.recovery_available,
            stable_ids=current.stable_ids if current.recovery_available else (),
            source_names=current.source_names if current.recovery_available else (),
            updated_at=_now(),
        ))

    def record_identity_recovery(self,available: bool) -> IdentityCleanupNotice:
        """Cache only availability; the correction journal still authorizes restore."""
        current=self.load()
        if current.recovery_available==bool(available):return current
        return self._write(replace(
            current,recovery_available=bool(available),updated_at=_now(),
        ))

    def clear_projection(self) -> ProjectionUpdateNotice:
        return self._write_projection(ProjectionUpdateNotice(updated_at=_now()))

    def record_classification(
        self,classification: DownstreamChangeClassification,
    ) -> DownstreamChangeClassification:
        """Persist only explicit optional jobs from one path-free classification."""

        if not isinstance(classification,DownstreamChangeClassification):
            raise TypeError("A typed downstream change classification is required.")
        if classification.matching_images_pending:
            self.record_identity_change(
                stable_ids=classification.affected_tag_ids,
                source_names=classification.source_names,
                label=classification.label,
                cached_possible_count=classification.cached_matching_image_count,
            )
        if any((
            classification.captions_pending,
            classification.metadata_pending,
            classification.organization_pending,
        )):
            self.record_projection_change(
                affected_tag_ids=classification.affected_tag_ids,
                changed_category_ids=classification.changed_category_ids,
                label=classification.label,
                cached_affected_image_count=(
                    classification.cached_projection_image_count
                ),
                captions=classification.captions_pending,
                metadata=classification.metadata_pending,
                organization=classification.organization_pending,
            )
        return classification

    def record_projection_change(
        self,*,affected_tag_ids: Iterable[str]=(),
        changed_category_ids: Iterable[str]=(),label: str="",
        cached_affected_image_count: int | None=None,
        captions: bool=True,metadata: bool=True,organization: bool=False,
    ) -> ProjectionUpdateNotice:
        current=self.load_projection()
        incoming_tags=_unique(affected_tag_ids)
        incoming_categories=_unique(changed_category_ids)
        incoming_labels=_unique((label,))
        incoming_count=(
            None if cached_affected_image_count is None
            else self._nonnegative(cached_affected_image_count)
        )
        if not current.active:
            possible=incoming_count
        elif (
            set(current.affected_tag_ids).issubset(incoming_tags)
            and set(current.changed_category_ids).issubset(incoming_categories)
        ):
            possible=incoming_count
        else:
            # Counts for overlapping saved scopes cannot be safely added.
            possible=None
        state=ProjectionUpdateNotice(
            active=bool(
                captions or metadata or organization
                or current.pending_targets
            ),
            affected_tag_ids=_unique((
                *current.affected_tag_ids,*incoming_tags,
            )),
            changed_category_ids=_unique((
                *current.changed_category_ids,*incoming_categories,
            )),
            change_labels=_unique((
                *current.change_labels,*incoming_labels,
            )),
            cached_affected_image_count=possible,
            captions_pending=current.captions_pending or bool(captions),
            metadata_pending=current.metadata_pending or bool(metadata),
            organization_pending=(
                current.organization_pending or bool(organization)
            ),
            updated_at=_now(),
        )
        return self._write_projection(state)

    def complete_projection_target(
        self,target: str,*,expected_updated_at: str="",
    ) -> ProjectionUpdateNotice:
        current=self.load_projection()
        target=str(target or "")
        if (
            not current.active or target not in current.pending_targets
            or (
                expected_updated_at
                and expected_updated_at!=current.updated_at
            )
        ):
            return current
        state=replace(
            current,
            captions_pending=(
                False if target=="captions" else current.captions_pending
            ),
            metadata_pending=(
                False if target=="metadata" else current.metadata_pending
            ),
            organization_pending=(
                False if target=="organization"
                else current.organization_pending
            ),
            updated_at=_now(),
        )
        if not state.pending_targets:
            state=ProjectionUpdateNotice(updated_at=state.updated_at)
        return self._write_projection(state)

    def record_identity_change(
        self,*,stable_ids: Iterable[str]=(),source_names: Iterable[str]=(),
        label: str="",cached_possible_count: int | None=None,
    ) -> IdentityCleanupNotice:
        current=self.load(); incoming_ids=_unique(stable_ids)
        incoming_names=_unique(source_names); incoming_labels=_unique((label,))
        same_scope=(
            current.active
            and set(incoming_ids).issubset(current.stable_ids)
            and {
                normalize_tag(value) for value in incoming_names
            }.issubset({normalize_tag(value) for value in current.source_names})
        )
        possible=(
            self._nonnegative(cached_possible_count)
            if cached_possible_count is not None and (not current.active or same_scope)
            else None
        )
        return self._write(IdentityCleanupNotice(
            active=True,
            recovery_available=current.recovery_available,
            stable_ids=_unique((*(current.stable_ids if current.active else ()),*incoming_ids)),
            source_names=_unique((*(current.source_names if current.active else ()),*incoming_names)),
            change_labels=_unique((*current.change_labels,*incoming_labels)),
            audit_state="needs_audit",
            cached_possible_count=possible,
            updated_at=_now(),
        ))

    @staticmethod
    def _scope_covers(
        current: IdentityCleanupNotice,stable_ids: Iterable[str],
        source_names: Iterable[str],
    ) -> bool:
        checked_ids=set(_unique(stable_ids))
        checked_names={normalize_tag(value) for value in _unique(source_names)}
        if not checked_ids and not checked_names:
            return True
        return (
            set(current.stable_ids).issubset(checked_ids)
            and {
                normalize_tag(value) for value in current.source_names
            }.issubset(checked_names)
        )

    def record_identity_audit(self,preview) -> IdentityCleanupNotice:
        if bool(getattr(preview,"cancelled",False)):
            return self.load()
        current=self.load()
        target_ids=tuple(getattr(preview,"target_ids",()) or ())
        source_names=tuple(getattr(preview,"source_names",()) or ())
        if current.active and not self._scope_covers(
            current,target_ids,source_names,
        ):
            return current
        items=tuple(getattr(preview,"items",()) or ())
        remaining=len(items)
        if remaining==0:
            return self.clear() if current.active else current
        safe=sum(getattr(item,"status","")=="will_change" for item in items)
        held=sum(
            getattr(item,"status","") in {
                "unresolved","unavailable","read_only","failed",
            }
            for item in items
        )
        overlaps=sum(
            getattr(item,"status","")=="will_change"
            and bool(getattr(item,"warnings",()))
            for item in items
        )
        labels=current.change_labels or (
            "Review current System Knowledge image cleanup",
        )
        return self._write(IdentityCleanupNotice(
            active=True,
            recovery_available=current.recovery_available,
            stable_ids=current.stable_ids if current.active else _unique(target_ids),
            source_names=(
                current.source_names if current.active else _unique(source_names)
            ),
            change_labels=labels,
            audit_state="audited",
            cached_possible_count=remaining,
            library_image_count=self._nonnegative(
                getattr(preview,"image_count",0)
            ),
            candidate_count=self._nonnegative(
                getattr(preview,"candidate_count",0)
            ),
            remaining_count=remaining,
            safe_count=safe,
            held_count=held,
            overlapping_warning_count=overlaps,
            updated_at=_now(),
        ))

    def record_identity_apply(
        self,changed: int,*,stable_ids: Iterable[str]=(),
        source_names: Iterable[str]=(),
    ) -> IdentityCleanupNotice:
        current=self.load()
        if not current.active or not self._scope_covers(
            current,stable_ids,source_names,
        ):
            return current
        changed=self._nonnegative(changed)
        remaining=max(0,current.remaining_count-changed)
        return self._write(replace(
            current,
            audit_state="verification_needed",
            cached_possible_count=remaining,
            remaining_count=remaining,
            safe_count=max(0,current.safe_count-changed),
            updated_at=_now(),
        ))

    def require_identity_recheck(self) -> IdentityCleanupNotice:
        current=self.load()
        if not (current.active or current.recovery_available):return current
        return self._write(replace(
            current,active=True,audit_state="verification_needed",updated_at=_now(),
        ))
