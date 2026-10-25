from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .automation_repository import AutomationRuleRepository
from .caption_import import looks_like_metadata_prose_fragment,metadata_sources_only
from .character_vocabulary import CharacterVocabularyResolver
from .discovery_registry import DiscoveryRegistry
from .knowledge_engine import SuggestionEngine
from .review_queue import ReviewTaskDatabase
from .tag_collection import normalize_tag
from .tag_database import TagDatabase
from .tag_alias_relationships import (
    TagAliasChange,TagAliasPreflightError,TagAliasProposal,
    TagAliasRelationshipService,
)
from .paths import DEFAULTS_DIR


@dataclass(frozen=True)
class NewVocabularyItem:
    tag: str
    seen_count: int
    example_image: str = ""
    suggested_category: str = "uncategorized"
    confidence: float = 0.0
    suggestion: dict | None = None
    sources: tuple[str, ...] = ()
    alias_target_id: str = ""


class VocabularyIntakeService:
    """Acknowledge newly observed vocabulary before deeper semantic review."""

    def __init__(self, knowledge: TagDatabase, reviews: ReviewTaskDatabase,
                 discovery: DiscoveryRegistry, suggestions: SuggestionEngine,
                 automation: AutomationRuleRepository | None = None,
                 character_resolver: CharacterVocabularyResolver | None = None,
                 default_relationships_path: str | Path | None = None,
                 user_relationships_path: str | Path | None = None) -> None:
        self.knowledge = knowledge
        self.reviews = reviews
        self.discovery = discovery
        self.suggestions = suggestions
        self.automation = automation
        self.character_resolver = character_resolver
        self.default_relationships_path=Path(
            default_relationships_path or DEFAULTS_DIR/"relationships.json"
        )
        self.user_relationships_path=Path(
            user_relationships_path or knowledge.path.with_name("relationships.json")
        )

    def _ignored_tags(self) -> set[str]:
        if self.automation is None:
            return set()
        return {
            normalize_tag(str(record.get("tag") or ""))
            for record in self.automation.combined().get("ignore_rules", [])
            if normalize_tag(str(record.get("tag") or ""))
        }

    def find_new(self, tag_counts: dict[str, int],
                 indexed_records: Iterable[object] = (), *,
                 tag_sources: dict[str, Iterable[str]] | None = None,
                 tag_examples: dict[str, str] | None = None,
                 should_continue: Callable[[], bool] | None = None,
                 progress: Callable[[int, int, str], None] | None = None) -> list[NewVocabularyItem]:
        previously_seen = set(self.discovery.items)
        previously_queued = {
            normalize_tag(task.details.get("tag", ""))
            for task in self.reviews.tasks.values()
            if task.task_type == "new_tag"
        }
        globally_ignored = self._ignored_tags()
        source_map=tag_sources or {}
        unknown = {
            normalize_tag(tag): (str(tag).strip(), int(count or 0))
            for tag, count in tag_counts.items()
            if str(tag).strip()
            and self.knowledge.find(tag) is None
            and normalize_tag(tag) not in previously_seen
            and normalize_tag(tag) not in previously_queued
            and normalize_tag(tag) not in globally_ignored
            and not (
                metadata_sources_only(source_map.get(normalize_tag(tag),()))
                and looks_like_metadata_prose_fragment(tag)
            )
        }
        examples: dict[str, str] = {
            normalize_tag(key):str(value)
            for key,value in (tag_examples or {}).items() if str(value).strip()
        }
        if unknown:
            for index, record in enumerate(indexed_records, 1):
                if should_continue is not None and not should_continue():
                    raise InterruptedError("Vocabulary discovery cancelled safely")
                path = str(getattr(record, "path", "") or "")
                for raw_tag in getattr(record, "tags", ()):
                    key = normalize_tag(raw_tag)
                    if key in unknown and key not in examples:
                        examples[key] = path
                if len(examples) == len(unknown):
                    break

        result = []
        total = len(unknown)
        for index, (key, (display, count)) in enumerate(unknown.items(), 1):
            if should_continue is not None and not should_continue():
                raise InterruptedError("Vocabulary discovery cancelled safely")
            proposed = self.suggestions.suggest(display)
            payload = dict(proposed.__dict__)
            alias_target_id = ""
            if self.character_resolver is not None:
                character = self.character_resolver.resolve(display)
                if character.actionable and (
                    character.alias_target_id
                    or character.status in {"exact", "series_aware", "variant_review", "unlinked"}
                    or character.confidence >= float(payload.get("confidence") or 0.0)
                ):
                    payload.update({
                        "category": character.category,
                        "confidence": character.confidence,
                        "reason": character.reason,
                        "character_resolution": character.details(),
                    })
                    alias_target_id = character.alias_target_id
            result.append(NewVocabularyItem(
                tag=display, seen_count=count, example_image=examples.get(key, ""),
                suggested_category=str(payload.get("category") or "uncategorized"),
                confidence=float(payload.get("confidence") or 0.0), suggestion=payload,
                sources=tuple(dict.fromkeys(
                    str(source) for source in source_map.get(key,())
                    if str(source).strip()
                )),
                alias_target_id=alias_target_id,
            ))
            if progress is not None:
                progress(index, total, display)
        return sorted(result, key=lambda item: (-item.seen_count, item.tag.casefold()))

    def alias(self, items: Iterable[NewVocabularyItem]) -> list[str]:
        """Attach observed names to reviewed canonical System Knowledge records."""
        pending=list(items); saved=[]; now=datetime.now(timezone.utc).isoformat(timespec="seconds")
        service=TagAliasRelationshipService(
            self.knowledge,default_path=self.default_relationships_path,
            user_path=self.user_relationships_path,
        )
        plan=service.preflight(tuple(
            TagAliasProposal(item.tag,str(item.alias_target_id or ""))
            for item in pending
        ))
        if plan.blockers:raise TagAliasPreflightError(plan)
        changed_targets={}
        try:
            for item in pending:
                target=self.knowledge.tags.get(str(item.alias_target_id or ""))
                if target is None:
                    raise ValueError(f"Choose an existing SDXL Tag for {item.tag!r}.")
                if target.id not in changed_targets:
                    target.aliases=list(service.effective_for_tag(target.id).aliases)
                    changed_targets[target.id]=target
                key=normalize_tag(item.tag)
                if not key:
                    continue
                conflict=self.knowledge.conflicting_name([item.tag],exclude_id=target.id)
                if conflict is not None:
                    raise ValueError(
                        f"{item.tag!r} already identifies {conflict.display!r}; it cannot also identify {target.display!r}."
                    )
                if key==normalize_tag(target.display) or any(
                    normalize_tag(value)==key for value in target.aliases
                ):
                    continue
                target.aliases.append(item.tag.strip())
                target.seen_count=max(target.seen_count,int(item.seen_count or 0))
                target.last_seen=now
                provenance={
                    "source":"library_vocabulary_alias","alias":item.tag.strip(),
                    "seen_count":int(item.seen_count or 0),"sources":list(item.sources),
                }
                if provenance not in target.provenance:
                    target.provenance.append(provenance)
                saved.append(item.tag)
            if saved:
                service.apply(tuple(
                    TagAliasChange(
                        target.id,tuple(target.aliases),source="library_vocabulary_alias",
                        reason="Reviewed startup Use Existing Tag decision",
                    )
                    for target in changed_targets.values()
                    if any(
                        item.tag in saved and item.alias_target_id==target.id
                        for item in pending
                    )
                ))
        except BaseException:
            self.knowledge.load()
            raise
        if saved:
            reloaded=TagDatabase(self.knowledge.path)
            TagAliasRelationshipService(
                reloaded,default_path=self.default_relationships_path,
                user_path=self.user_relationships_path,
            )
            missing=[
                item.tag for item in pending
                if item.tag in saved and (
                    reloaded.find(item.tag) is None
                    or reloaded.find(item.tag).id!=item.alias_target_id
                )
            ]
            if missing:
                raise RuntimeError("The alias decision could not be verified after saving.")
        return saved

    def preflight_aliases(
        self,items: Iterable[NewVocabularyItem],*,indexed_records=None,
    ):
        """Preview startup alias decisions without saving or opening image files."""
        service=TagAliasRelationshipService(
            self.knowledge,default_path=self.default_relationships_path,
            user_path=self.user_relationships_path,
        )
        return service.preflight(tuple(
            TagAliasProposal(item.tag,str(item.alias_target_id or ""))
            for item in items
        ),records=indexed_records)

    def repair_acknowledged(self, tag_counts: dict[str, int]) -> list[str]:
        """Restore a missing meaning question after an interrupted two-file save."""
        repaired=[]
        counts_by_key={normalize_tag(value):int(count or 0) for value,count in tag_counts.items()}
        for record in self.knowledge.tags.values():
            if not any(
                item.get("source") == "library_vocabulary_intake"
                and item.get("meaning_reviewed") is False
                for item in record.provenance if isinstance(item, dict)
            ):
                continue
            source_key=f"new_tag:{normalize_tag(record.display)}"
            if self.reviews.find_by_source_key(source_key) is not None:
                continue
            self.reviews.add_task(
                task_type="new_tag", title=f"Review new tag: {record.display}",
                description="The name is recognized. Confirm what it means so CollectiGen can organize and reuse it safely.",
                details={
                    "tag":record.display,
                    "seen_count":counts_by_key.get(normalize_tag(record.display),record.seen_count),
                    "acknowledged":True,"recovered_after_interrupted_save":True,
                    "suggestion":{},
                },
                source_key=source_key,
            )
            repaired.append(record.display)
        if repaired:
            self.reviews.save()
        return repaired

    def ignore(self, items: Iterable[NewVocabularyItem]) -> list[str]:
        """Create durable global-ignore rules through the shared rule store."""
        if self.automation is None:
            raise RuntimeError("The shared Rules & Automation store is unavailable.")
        pending=list(items)
        return list(self.automation.add_ignore_tags_verified(
            (item.tag for item in pending),source="library_vocabulary_intake",
            seen_counts={item.tag:int(item.seen_count) for item in pending},
        ))

    def acknowledge(self, items: Iterable[NewVocabularyItem], *, checkpoint_size: int = 500,
                    progress: Callable[[int, int], None] | None = None) -> list[str]:
        pending = list(items)
        accepted = []
        seen_in_batch: set[str] = set()
        for index, item in enumerate(pending, start=1):
            key = normalize_tag(item.tag)
            if key in seen_in_batch or self.knowledge.find(item.tag) is not None:
                if progress:
                    progress(index, len(pending))
                continue
            seen_in_batch.add(key)
            category=str(item.suggested_category or "uncategorized")
            self.knowledge.add_known_tag(
                item.tag, category=category, seen_count=item.seen_count,
                provenance={"source": "library_vocabulary_intake", "meaning_reviewed": False},
                rebuild_lookup=False,
            )
            self.reviews.add_task(
                task_type="new_tag", title=f"Review new tag: {item.tag}",
                image_path=Path(item.example_image) if item.example_image else "",
                description="The name is recognized. Confirm what it means so CollectiGen can organize and reuse it safely.",
                details={"tag": item.tag, "seen_count": item.seen_count,
                         "acknowledged": True, "suggestion": {
                             **dict(item.suggestion or {}),"category":category,
                         }},
                source_key=f"new_tag:{normalize_tag(item.tag)}",
            )
            accepted.append(item.tag)
            if checkpoint_size > 0 and len(accepted) % checkpoint_size == 0:
                self.knowledge._rebuild_lookup()
                self.reviews.save(); self.knowledge.save()
            if progress:
                progress(index, len(pending))
        if accepted:
            self.knowledge._rebuild_lookup()
            self.reviews.save()
            self.knowledge.save()
            saved_knowledge=TagDatabase(self.knowledge.path)
            saved_reviews=ReviewTaskDatabase(self.reviews.path)
            if any(saved_knowledge.find(value) is None for value in accepted):
                raise RuntimeError("The vocabulary decision could not be verified after saving.")
            if any(
                saved_reviews.find_by_source_key(f"new_tag:{normalize_tag(value)}") is None
                for value in accepted
            ):
                raise RuntimeError("The queued meaning review could not be verified after saving.")
        return accepted
