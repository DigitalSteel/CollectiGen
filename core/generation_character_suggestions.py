"""Detached Character/Outfit setup suggestions from confirmed Image Knowledge.

This module ranks evidence supplied by a caller. It does not read a library,
edit Image/System Knowledge, or save a Generation Setup. The UI must obtain a
current, explicitly scoped snapshot and recheck it before publication.
"""
from __future__ import annotations

from collections import defaultdict
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import ExitStack, closing
from dataclasses import dataclass
from itertools import combinations, islice
import json
import re
from pathlib import Path
from threading import Event
from typing import Callable, Iterable

from .annotation import ImageAnnotation
from .dataset_index import DatasetIndex
from .image_record import ImageRecord, _path_fingerprint
from .library_storage import LibraryStorageInspector
from .conflict_policy import ConflictAllowPolicy
from .knowledge_relationships import SCOPED_CONSTRAINT
from .tag_collection import normalize_tag


@dataclass(frozen=True)
class SuggestionImage:
    image_id: str
    library_id: str
    label: str
    annotation: ImageAnnotation | None
    state: str = "current"  # current, stale, or unavailable
    source_ref: SuggestionSourceRef | None = None


@dataclass(frozen=True)
class SuggestionSourceRef:
    """One exact current library copy for optional, local-only visual review."""

    library_id: str
    identity_key: str
    path: str
    label: str
    image_fingerprint: tuple[int, ...]
    sidecar_fingerprint: dict


@dataclass(frozen=True)
class SetupSuggestion:
    kind: str
    text: str
    source_type: str  # owner_tag, image_tag, or description
    supporting_images: int
    eligible_images: int
    examples: tuple[str, ...]
    source_examples: tuple[str, ...] = ()
    source_refs: tuple[SuggestionSourceRef, ...] = ()


@dataclass(frozen=True)
class SavedOwnerSetupDetail:
    """Authored owner-record text, not an image-supported occurrence."""

    kind: str
    text: str
    source: str


def saved_owner_setup_details(owner_kind, record) -> tuple[SavedOwnerSetupDetail, ...]:
    """Offer literal Character/Outfit components without inventing image support."""
    if owner_kind not in {"character", "outfit"}:
        raise ValueError("Choose a Character or Outfit for saved setup details.")
    destination = "character" if owner_kind == "character" else "outfit"
    tags = (record.sdxl_tags or record.default_tags) if owner_kind == "character" else record.sdxl_tags
    groups = ((tags, destination, "Saved tag"),
              (record.plain_language, "other", "Saved description"),
              (record.original_style_tags if owner_kind == "character" else record.style_tags,
               "style", "Saved style tag"))
    result = []
    seen = set()
    for values, kind, source in groups:
        for value in values:
            text = str(value).strip()
            key = (kind, " ".join(text.split()).casefold())
            if not text or key in seen:
                continue
            seen.add(key)
            result.append(SavedOwnerSetupDetail(kind, text, source))
    return tuple(result)


@dataclass(frozen=True)
class RecurringClothingGroup:
    """Co-assigned clothing, either an exact set or a shared subset."""

    details: tuple[str, ...]
    supporting_images: int
    eligible_images: int
    examples: tuple[str, ...]
    complete_set: bool = True


@dataclass(frozen=True)
class StarterSetupProposal:
    """A small set observed together on the selected owner, not inferred facts."""

    kind: str
    details: tuple[str, ...]
    supporting_images: int
    eligible_images: int


@dataclass(frozen=True)
class ImageContextIdea:
    """Recurring image-wide prompt text, with no selected-owner attribution."""

    text: str
    source_type: str
    supporting_images: int
    eligible_images: int
    examples: tuple[str, ...] = ()
    source_examples: tuple[str, ...] = ()
    source_refs: tuple[SuggestionSourceRef, ...] = ()


@dataclass(frozen=True)
class SetupSuggestionReport:
    owner_kind: str
    owner_id: str
    eligible_images: int
    owner_assigned_images: int
    ambiguous_images: int
    stale_images: int
    unavailable_images: int
    duplicate_copies: int
    suggestions: tuple[SetupSuggestion, ...]
    omitted_suggestions: int = 0
    clothing_groups: tuple[RecurringClothingGroup, ...] = ()
    omitted_clothing_groups: int = 0
    long_clothing_images: int = 0
    clothing_vocabulary_limited: bool = False
    starter_proposal: StarterSetupProposal | None = None
    image_context_ideas: tuple[ImageContextIdea, ...] = ()
    context_excluded_images: int = 0


def _starter_from_owner_groups(owner_kind, groups_by_image, displays, denominator,
                               *, should_cancel=None):
    """Choose one bounded co-observed set; marginal tag counts are insufficient."""
    group_support = defaultdict(set)
    for image_id, groups in groups_by_image.items():
        if len(groups) == 1:
            group = next(iter(groups))
            if group:
                group_support[group].add(image_id)
    # Compare only the 16 most-observed complete owner sets. The source count
    # can be 100,000 images, so pairwise intersections must not grow with it.
    ranked = sorted(group_support, key=lambda group: (-len(group_support[group]), group))[:16]
    candidates = {group for group in ranked if 1 <= len(group) <= 8}
    for left, right in combinations(ranked, 2):
        if should_cancel is not None and should_cancel():
            raise InterruptedError("Starter setup review stopped.")
        shared = tuple(sorted(set(left).intersection(right)))
        if 1 <= len(shared) <= 8:
            candidates.add(shared)
    group_sets = [(set(group), len(images)) for group, images in group_support.items()]
    choices = []
    for candidate in candidates:
        candidate_set = set(candidate)
        count = sum(support for group, support in group_sets
                    if candidate_set.issubset(group))
        if count >= 2:
            choices.append((candidate, count))
    if not choices:
        return None
    # Favor useful multi-detail sets, but do not bury a much better-supported
    # smaller set. Every returned combination was assigned together in >=2 images.
    details, count = min(choices, key=lambda choice: (
        -choice[1] * min(len(choice[0]), 4), -choice[1], -len(choice[0]), choice[0],
    ))
    kind = "character" if owner_kind == "character" else "outfit"
    return StarterSetupProposal(kind, tuple(displays[detail] for detail in details),
                                count, denominator)


@dataclass(frozen=True)
class SuggestionFocus:
    """An explicit, owner-assigned evidence cohort, not an inferred conflict."""

    mode: str  # character_outfit, owner_detail, or reviewed_branch
    value: str
    group: str = ""  # owner_detail group, or reviewed_branch relationship ID


@dataclass(frozen=True)
class SuggestionFocusOption:
    focus: SuggestionFocus
    text: str
    supporting_images: int


@dataclass(frozen=True)
class SuggestionFocusCatalog:
    options: tuple[SuggestionFocusOption, ...]
    omitted_options: int = 0


@dataclass(frozen=True)
class LookAlternativeBranch:
    root_id: str
    label: str
    supporting_images: int
    examples: tuple[str, ...]


@dataclass(frozen=True)
class LookAlternativeGroup:
    label: str
    relationship_id: str
    branches: tuple[LookAlternativeBranch, ...]
    unresolved_images: int


@dataclass(frozen=True)
class SuggestionSourceCapture:
    included_library_ids: tuple[str, ...]
    library_states: tuple[tuple[str, str], ...]
    index_token: tuple
    # Every indexed path in scope is represented, including excluded rows.
    # Readable local sidecars have exact hashes. Unavailable families retain
    # metadata/availability observations, not a fabricated content hash; only
    # observable changes can be held until that family is readable again.
    source_fingerprints: tuple[tuple[str, tuple[int, ...], dict], ...]


_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_DESCRIPTION_WORD = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)
_DESCRIPTION_CLAUSE = re.compile(r"[.!?;,:\n]+")
_UNCERTAIN_WORDS = frozenset({
    "no", "not", "never", "without", "lack", "lacks", "lacking", "maybe",
    "perhaps", "possibly", "might", "could", "seems", "appears", "unclear",
})
_EXCERPT_EDGE_WORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for",
    "from", "with", "by", "is", "are", "has", "have", "as", "its", "their",
})
_FINGERPRINT_READ_WINDOW = 4
_SOURCE_AVAILABILITY_BATCH = 256


def _guarded_sidecar_fingerprint(path: Path):
    """Do not hydrate a changed provider placeholder while hashing evidence."""
    inspector = LibraryStorageInspector()
    if (inspector.requires_remote_content(path)
            or inspector.requires_remote_content(path.with_suffix(".tags.json"))):
        return None
    return _path_fingerprint(path.with_suffix(".tags.json"))


def _availability_key(availability):
    state = getattr(availability, "state", None)
    return (getattr(state, "value", str(state)) if state is not None else "unknown",
            bool(availability.readable))


def _unavailable_observation(cached, fresh):
    return {"observation": "unavailable", "cached": _availability_key(cached),
            "fresh": _availability_key(fresh)}


def _source_fingerprint(path: Path, cancel_event: Event):
    if cancel_event.is_set():
        raise InterruptedError("Character/Outfit suggestion review cancelled.")
    signature = DatasetIndex.fingerprint(path)
    if cancel_event.is_set():
        raise InterruptedError("Character/Outfit suggestion review cancelled.")
    return signature, _guarded_sidecar_fingerprint(path)


def _prefetched_sources(rows, dataset, scope: tuple[str, ...], max_images: int,
                        should_cancel: Callable[[], bool] | None,
                        *, start_position: int = 0):
    """Hash at most four scoped rows ahead; keep all decisions on the caller."""
    source = iter(rows)
    selected_count = start_position
    cancel_event = Event()
    inspector = LibraryStorageInspector()

    def check_cancel():
        if should_cancel is not None and should_cancel():
            cancel_event.set()
            raise InterruptedError("Character/Outfit suggestion review cancelled.")

    with ThreadPoolExecutor(max_workers=_FINGERPRINT_READ_WINDOW,
                            thread_name_prefix="setup-evidence-read") as executor:
        while True:
            batch = []
            for _ in range(_SOURCE_AVAILABILITY_BATCH):
                check_cancel()
                try:
                    row = next(source)
                except StopIteration:
                    break
                path = Path(row.path)
                identity = dataset.identity_for_path(path)
                if identity is None or identity.library_id not in scope:
                    continue
                selected_count += 1
                if selected_count > max_images:
                    raise ValueError("The selected scope exceeds the bounded analysis limit.")
                batch.append((row, path, identity, selected_count))
            if not batch:
                break
            availability = inspector.inspect_images(item[1] for item in batch)
            check_cancel()
            pending = deque()
            selected = iter(batch)

            def fill():
                while len(pending) < _FINGERPRINT_READ_WINDOW:
                    try:
                        row, path, identity, position = next(selected)
                    except StopIteration:
                        break
                    cached = dataset.storage_availability(path)
                    fresh = availability[path]
                    readable = cached.readable and fresh.readable
                    future = (executor.submit(_source_fingerprint, path, cancel_event)
                              if readable else None)
                    pending.append((row, path, identity, position, cached, fresh, future))

            try:
                fill()
                while pending:
                    check_cancel()
                    row, path, identity, position, cached, fresh, future = pending.popleft()
                    if future is None:
                        signature, sidecar_fingerprint = DatasetIndex.fingerprint(path), None
                    else:
                        while True:
                            try:
                                signature, sidecar_fingerprint = future.result(timeout=.1)
                                break
                            except FutureTimeout:
                                check_cancel()
                    yield (row, path, identity, position, signature,
                           sidecar_fingerprint, cached, fresh)
                    fill()
            finally:
                if pending:
                    cancel_event.set()
                    for _row, _path, _identity, _position, _cached, _fresh, future in pending:
                        if future is not None:
                            future.cancel()
    cancel_event.set()


def _description_excerpts(text: str) -> tuple[str, ...]:
    """Bound exact, unqualified spans; never infer an owner from prose."""
    if len(text) > 512:
        return ()
    excerpts = []
    seen = set()
    for clause in _DESCRIPTION_CLAUSE.split(text)[:8]:
        words = [match.group() for match in _DESCRIPTION_WORD.finditer(clause)]
        if len(words) < 2 or len(words) > 24:
            continue
        normalized = [word.casefold() for word in words]
        if any(word in _UNCERTAIN_WORDS for word in normalized):
            continue
        for width in (2, 3, 4):
            for start in range(len(words) - width + 1):
                chunk = words[start:start + width]
                if (normalized[start] in _EXCERPT_EDGE_WORDS or
                        normalized[start + width - 1] in _EXCERPT_EDGE_WORDS):
                    continue
                value = " ".join(chunk)
                key = normalize_tag(value)
                if key not in seen:
                    excerpts.append(value)
                    seen.add(key)
                if len(excerpts) >= 32:
                    return tuple(excerpts)
    return tuple(excerpts)


def load_suggestion_sources(
    dataset,
    included_library_ids: Iterable[str],
    *,
    should_cancel: Callable[[], bool] | None = None,
    progress: Callable[[int, int], None] | None = None,
    max_images: int = 100_000,
) -> tuple[tuple[SuggestionImage, ...], SuggestionSourceCapture]:
    """Read an explicit included-library snapshot through normal local authority.

    Only a current structured sidecar supplies confirmed suggestions. Legacy
    caption imports, stale index rows and unavailable files are coverage gaps,
    not silently upgraded image facts. This runs on a cancellable background
    worker; it never opens image pixels or writes source records.
    """
    scope = tuple(dict.fromkeys(str(value) for value in included_library_ids if value))
    if not scope:
        raise ValueError("Choose at least one included image library.")
    active = set(getattr(dataset, "scope_library_ids", ()))
    if set(scope) - active:
        raise ValueError("The selected library scope changed; review it again.")
    index = getattr(dataset, "index", None)
    if index is None or not callable(getattr(index, "all_records", None)):
        raise ValueError("Refresh the included libraries before reviewing image evidence.")
    scoped_stream = getattr(index, "iter_records_for_libraries", None)
    scoped_connect = getattr(index, "connect_for_libraries", None)
    scoped_token = getattr(index, "evidence_snapshot_token_for_libraries", None)
    selected_stream = all(callable(value) for value in (
        scoped_stream, scoped_connect, scoped_token,
    ))
    before = (scoped_token(scope) if selected_stream
              else index.evidence_snapshot_token())
    library_states = tuple((library_id, dataset.library_availability(library_id))
                           for library_id in scope)
    if selected_stream:
        library_order = tuple(library_id for library_id in dataset.scope_library_ids
                              if library_id in scope)
        row_groups = (scoped_stream((library_id,), should_cancel=should_cancel)
                      for library_id in library_order)
        connection = scoped_connect(scope)
    else:
        stream = getattr(index, "iter_records", None)
        row_groups = ((stream(should_cancel=should_cancel) if callable(stream)
                       else iter(index.all_records())),)
        connection = index.connect()
    images = []
    fingerprints = []
    with ExitStack() as resources:
        db = resources.enter_context(closing(connection))
        def ordered_prefetched():
            for rows in row_groups:
                with ExitStack() as row_resources:
                    close_rows = getattr(rows, "close", None)
                    if callable(close_rows):
                        row_resources.callback(close_rows)
                    batch = _prefetched_sources(
                        rows, dataset, scope, max_images, should_cancel,
                        start_position=len(images),
                    )
                    row_resources.callback(batch.close)
                    yield from batch

        prefetched = ordered_prefetched()
        resources.callback(prefetched.close)
        for (row, path, identity, position, signature, sidecar_fingerprint,
             cached, fresh) in prefetched:
            if should_cancel is not None and should_cancel():
                raise InterruptedError("Character/Outfit suggestion review cancelled.")
            if progress is not None and (position == 1 or position % 250 == 0):
                progress(position, 0)
            if sidecar_fingerprint is None:
                sidecar_fingerprint = _unavailable_observation(cached, fresh)
            fingerprints.append((str(path), signature, sidecar_fingerprint))
            image_id = identity.key
            if sidecar_fingerprint.get("observation") == "unavailable":
                images.append(SuggestionImage(image_id, identity.library_id,
                                               path.name, None, "unavailable"))
                continue
            if index.get_if_fresh(path, db) is None:
                images.append(SuggestionImage(image_id, identity.library_id,
                                               path.name, None, "stale"))
                continue
            if not row.sidecar_exists:
                images.append(SuggestionImage(image_id, identity.library_id,
                                               path.name, None, "unavailable"))
                continue
            try:
                record = ImageRecord(path)
            except (OSError, RuntimeError, ValueError):
                images.append(SuggestionImage(image_id, identity.library_id,
                                               path.name, None, "unavailable"))
                continue
            # The sidecar must be the exact version that supplied annotation.
            if record._loaded_sidecar_fingerprint != sidecar_fingerprint:
                images.append(SuggestionImage(image_id, identity.library_id,
                                               path.name, None, "stale"))
                continue
            file_hash = record.annotation.file_identity.get("sha256", "")
            if isinstance(file_hash, str) and _SHA256.fullmatch(file_hash):
                image_id = file_hash.lower()
            source_ref = SuggestionSourceRef(
                identity.library_id, identity.key, str(path), path.name,
                signature, sidecar_fingerprint,
            )
            images.append(SuggestionImage(image_id, identity.library_id,
                                           path.name, record.annotation,
                                           source_ref=source_ref))
    if progress is not None and images:
        progress(len(images), 0)
    after = (scoped_token(scope) if selected_stream
             else index.evidence_snapshot_token())
    if (before != after or set(scope) - set(getattr(dataset, "scope_library_ids", ()))
            or library_states != tuple((library_id, dataset.library_availability(library_id))
                                       for library_id in scope)):
        raise ValueError("Indexed Image Knowledge changed during review; refresh suggestions.")
    return tuple(images), SuggestionSourceCapture(scope, library_states, tuple(after),
                                                  tuple(fingerprints))


def recheck_suggestion_sources(dataset, capture: SuggestionSourceCapture,
                               *, should_cancel: Callable[[], bool] | None = None) -> None:
    """Hold setup publication if the library scope or any source changed."""
    if not isinstance(capture, SuggestionSourceCapture):
        raise ValueError("Review image evidence before saving this setup.")
    if set(capture.included_library_ids) - set(getattr(dataset, "scope_library_ids", ())):
        raise ValueError("The included library scope changed; refresh suggestions.")
    if capture.library_states != tuple((library_id, dataset.library_availability(library_id))
                                       for library_id in capture.included_library_ids):
        raise ValueError("An included library changed availability; refresh suggestions.")
    index = getattr(dataset, "index", None)
    if index is None:
        raise ValueError("Indexed Image Knowledge changed; refresh suggestions.")
    scoped_token = getattr(index, "evidence_snapshot_token_for_libraries", None)
    current_token = (scoped_token(capture.included_library_ids)
                     if callable(scoped_token) else index.evidence_snapshot_token())
    if current_token != capture.index_token:
        raise ValueError("Indexed Image Knowledge changed; refresh suggestions.")
    inspector = LibraryStorageInspector()
    captured = iter(capture.source_fingerprints)
    while batch := tuple(islice(captured, _SOURCE_AVAILABILITY_BATCH)):
        if should_cancel is not None and should_cancel():
            raise InterruptedError("Setup source recheck cancelled.")
        availability = inspector.inspect_images(Path(item[0]) for item in batch)
        for path_string, signature, sidecar_fingerprint in batch:
            if should_cancel is not None and should_cancel():
                raise InterruptedError("Setup source recheck cancelled.")
            path = Path(path_string)
            if DatasetIndex.fingerprint(path) != signature:
                raise ValueError("An image or its Image Knowledge changed; refresh suggestions.")
            cached = dataset.storage_availability(path)
            fresh = availability[path]
            if sidecar_fingerprint.get("observation") == "unavailable":
                if _unavailable_observation(cached, fresh) != sidecar_fingerprint:
                    raise ValueError("Source availability changed; refresh suggestions.")
                continue
            if not cached.readable or not fresh.readable:
                raise ValueError("A source became unavailable; refresh suggestions.")
            if _guarded_sidecar_fingerprint(path) != sidecar_fingerprint:
                raise ValueError("An image or its Image Knowledge changed; refresh suggestions.")


def recheck_suggestion_preview_source(
    dataset, capture: SuggestionSourceCapture, source: SuggestionSourceRef,
    *, selected_library_ids: Iterable[str],
) -> Path:
    """Admit one retained example for local preview, without rescanning a library.

    This is deliberately narrower than the all-source guard required for Save.
    It checks the exact captured copy before the UI decodes any image pixels.
    """
    if not isinstance(capture, SuggestionSourceCapture) or not isinstance(source, SuggestionSourceRef):
        raise ValueError("Find Suggested Details again before viewing a source image.")
    selected = frozenset(str(value) for value in selected_library_ids if value)
    if (source.library_id not in selected
            or source.library_id not in capture.included_library_ids
            or source.library_id not in getattr(dataset, "scope_library_ids", ())):
        raise ValueError("This source is outside the checked image libraries. Find Suggested Details again.")
    captured_state = dict(capture.library_states).get(source.library_id)
    if (captured_state is None
            or dataset.library_availability(source.library_id) != captured_state):
        raise ValueError("This source library changed availability. Find Suggested Details again.")
    path = Path(source.path)
    identity = dataset.identity_for_path(path)
    if (identity is None or identity.library_id != source.library_id
            or identity.key != source.identity_key):
        raise ValueError("This source no longer belongs to the reviewed library. Find Suggested Details again.")
    exact_capture = (source.path, source.image_fingerprint, source.sidecar_fingerprint)
    if exact_capture not in capture.source_fingerprints:
        raise ValueError("This source is not in the reviewed image scope. Find Suggested Details again.")
    if not dataset.storage_availability(path).readable:
        raise ValueError("This source image is unavailable. Find Suggested Details again when it is readable.")
    if not LibraryStorageInspector().inspect_images((path,))[path].readable:
        raise ValueError("This source image became unavailable. Find Suggested Details again.")
    if (DatasetIndex.fingerprint(path) != source.image_fingerprint
            or _guarded_sidecar_fingerprint(path) != source.sidecar_fingerprint):
        raise ValueError("This image or its Image Knowledge changed. Find Suggested Details again.")
    return path


def _image_wide_identity(annotation: ImageAnnotation, *, tag_id: str,
                         names: frozenset[str]) -> bool:
    # Image-wide confirmed facts establish presence, not which subject owns
    # other tags or description text. AI/metadata proposals are separate.
    for value in annotation.all_structured_tags():
        if annotation.tag_identities.get(value) == tag_id:
            return True
        if normalize_tag(value) in names:
            return True
    return False


def _validate_focus(owner_kind: str, focus: SuggestionFocus | None) -> None:
    if focus is None:
        return
    if not isinstance(focus, SuggestionFocus) or not focus.value:
        raise ValueError("Choose an exact observed look focus.")
    if focus.mode == "character_outfit":
        if owner_kind != "character" or focus.group:
            raise ValueError("An Outfit focus needs a selected Character.")
    elif focus.mode == "owner_detail":
        if focus.group not in ({"appearance", "clothing"} if owner_kind == "character"
                               else {"clothing"}) or not normalize_tag(focus.value):
            raise ValueError("Choose an assigned appearance or clothing detail.")
    elif focus.mode == "reviewed_branch":
        if not focus.group:
            raise ValueError("Choose one reviewed relationship and its branch.")
    else:
        raise ValueError("Choose an available look focus.")


def _qualified_look_constraints(evaluator, qualified_relationship_ids):
    """Intersect caller qualification with current, unconditional owner rules.

    The caller also qualifies conflict-rule policies/aliases and captures their
    authorities. This helper cannot infer that qualification from labels.
    """
    qualified = frozenset(qualified_relationship_ids)
    if evaluator is None:
        if qualified:
            raise ValueError("Reviewed look choices need the captured relationship evaluator.")
        return {}
    result = {}
    for relationship in evaluator.registry.relationships:
        if (relationship.id not in qualified or relationship.kind != SCOPED_CONSTRAINT
                or relationship.scope != "same_character" or relationship.directed
                or not relationship.reviewed or relationship.disabled):
            continue
        try:
            if ConflictAllowPolicy.from_payload(relationship.extras.get("allow_when")).conditions:
                continue
        except (ValueError, TypeError):
            continue
        roots = tuple(dict.fromkeys(endpoint.id for endpoint in relationship.endpoints
                                    if endpoint.kind == "tag" and endpoint.id))
        if len(roots) < 2 or len(roots) != len(relationship.endpoints):
            continue
        # An ancestor and its refinement cannot establish alternative branches.
        if any(evaluator.is_same_branch(first, second)
               for index, first in enumerate(roots) for second in roots[index + 1:]):
            continue
        result[relationship.id] = (relationship, roots)
    return result


def qualified_look_relationship_ids(registry, conflicts_path):
    """Admit only unconditional owner-scoped branch rules we can explain."""
    payload = json.loads(Path(conflicts_path).read_text(encoding="utf-8"))
    records = payload.get("rules", ()) if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise ValueError("Reviewed look rules are unavailable.")
    relationships = {item.id: item for item in registry.relationships}
    allowed_fields = {"id", "label", "scope", "relation", "severity", "notes",
                      "relationship_id"}
    qualified = set()
    unsafe = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        relationship_id = str(record.get("relationship_id") or "")
        if set(record) - allowed_fields:
            unsafe.add(relationship_id)
            continue
        if (record.get("scope") != "same_character" or
                record.get("relation") != "mutually_exclusive"):
            unsafe.add(relationship_id)
            continue
        relationship = relationships.get(relationship_id)
        if (relationship is None or relationship.kind != SCOPED_CONSTRAINT
                or relationship.scope != "same_character" or relationship.directed
                or not relationship.reviewed or relationship.disabled
                or relationship.extras or len(relationship.endpoints) < 2
                or any(endpoint.kind != "tag" for endpoint in relationship.endpoints)):
            continue
        qualified.add(relationship_id)
    return tuple(sorted(qualified - unsafe))


def _look_owners(owner_kind, owner_id, annotation):
    return tuple(owner for owner in annotation.characters
                 if (owner.character_id == owner_id if owner_kind == "character"
                     else owner_id in owner.outfit_ids))


def _accepted_look_exception(annotation):
    # The evaluator does not carry legacy conflict-rule aliases. Conservatively
    # exclude any image with a recorded accepted exception instead of missing
    # an alias and turning an explicitly accepted combination into alternatives.
    return bool(annotation.source.get("accepted_conflict_rules"))


def _owner_look_branches(owner_kind, owner, roots, evaluator):
    groups = ("appearance", "clothing") if owner_kind == "character" else ("clothing",)
    identities = {identity for group in groups for value in owner.tags.get(group, ())
                  if (identity := evaluator.registry.resolve_tag_id(value))}
    return frozenset(root for root in roots if any(
        identity == root or root in evaluator.broader_paths(identity)
        for identity in identities
    ))


def _reviewed_branch_owners(owner_kind, owners, annotation, roots, root_id, evaluator):
    if _accepted_look_exception(annotation):
        return ()
    return tuple(owner for owner in owners
                 if (owner_kind != "outfit" or len(owner.outfit_ids) == 1)
                 and _owner_look_branches(owner_kind, owner, roots, evaluator) == {root_id})


def reviewed_look_alternatives(
    owner_kind: str, owner_id: str, images: Iterable[SuggestionImage], *,
    included_library_ids: Iterable[str], evaluator, qualified_relationship_ids,
    should_cancel: Callable[[], bool] | None = None,
    max_images: int = 100_000, max_groups: int = 8,
) -> tuple[LookAlternativeGroup, ...]:
    """Group positively observed owner facts by reviewed incompatible branches.

    Counts describe distinct supporting images, not contradictory source images
    or confidence. Contradictory/ambiguous occurrences and accepted exceptions
    remain unresolved evidence. No text or missing fact establishes membership.
    """
    if owner_kind not in {"character", "outfit"} or not owner_id:
        raise ValueError("Choose one canonical Character or Outfit.")
    scope = frozenset(str(value) for value in included_library_ids if value)
    if not scope or max_images < 1 or max_groups < 1:
        raise ValueError("Choose included libraries and positive look review limits.")
    constraints = _qualified_look_constraints(evaluator, qualified_relationship_ids)
    support = defaultdict(set)
    examples = defaultdict(dict)
    unresolved = defaultdict(set)
    first_look_facts = {}
    divergent_identities = set()
    for position, image in enumerate(images, 1):
        if should_cancel is not None and should_cancel():
            raise InterruptedError("Reviewed look analysis cancelled.")
        if position > max_images:
            raise ValueError("The selected scope exceeds the bounded analysis limit.")
        if (image.library_id not in scope or image.state != "current"
                or image.annotation is None):
            continue
        if not image.image_id or not isinstance(image.annotation, ImageAnnotation):
            raise ValueError("Look sources need current Image Knowledge and exact image identities.")
        annotation = image.annotation
        owners = _look_owners(owner_kind, owner_id, annotation)
        look_facts = (
            tuple((owner.slot, owner.character_id, tuple(owner.outfit_ids),
                   tuple((group, tuple(owner.tags.get(group, ())))
                         for group in ("appearance", "clothing"))) for owner in owners),
            _accepted_look_exception(annotation),
        )
        previous = first_look_facts.setdefault(image.image_id, look_facts)
        if previous != look_facts:
            divergent_identities.add(image.image_id)
        for relationship_id, (_relationship, roots) in constraints.items():
            for owner in owners:
                branches = _owner_look_branches(owner_kind, owner, roots, evaluator)
                if not branches:
                    continue
                if (len(branches) != 1 or _accepted_look_exception(annotation)
                        or (owner_kind == "outfit" and len(owner.outfit_ids) != 1)):
                    unresolved[relationship_id].add(image.image_id)
                    continue
                key = relationship_id, next(iter(branches))
                support[key].add(image.image_id)
                if image.label and len(examples[key]) < 3:
                    examples[key].setdefault(image.image_id, image.label)
    groups = []
    for relationship_id, (relationship, roots) in constraints.items():
        # Copies with one exact identity but divergent sidecars cannot establish
        # clean alternatives. Multiple owner occurrences in one source can.
        for image_id in divergent_identities:
            observed_roots = tuple(root for root in roots
                                   if image_id in support[relationship_id, root])
            if observed_roots:
                unresolved[relationship_id].add(image_id)
                for root in observed_roots:
                    support[relationship_id, root].discard(image_id)
                    examples[relationship_id, root].pop(image_id, None)
        branches = tuple(LookAlternativeBranch(
            root, evaluator.registry.tag_display(root), len(support[relationship_id, root]),
            tuple(examples[relationship_id, root].values()),
        ) for root in roots if support[relationship_id, root])
        if len(branches) >= 2:
            groups.append(LookAlternativeGroup(
                relationship.label or "Alternative observed details", relationship_id,
                branches, len(unresolved[relationship_id]),
            ))
    return tuple(sorted(groups, key=lambda group: (
        -sum(branch.supporting_images for branch in group.branches), group.relationship_id,
    ))[:max_groups])


def suggestion_focus_options(
    owner_kind: str, owner_id: str, images: Iterable[SuggestionImage], *,
    included_library_ids: Iterable[str], should_cancel: Callable[[], bool] | None = None,
    max_outfits: int = 12, max_details: int = 24,
) -> SuggestionFocusCatalog:
    """Shortlist only exact per-owner assignments as optional evidence foci."""
    if owner_kind not in {"character", "outfit"} or not owner_id:
        raise ValueError("Choose one canonical Character or Outfit.")
    if max_outfits < 0 or max_details < 0:
        raise ValueError("Choose nonnegative focus limits.")
    scope = frozenset(str(value) for value in included_library_ids if value)
    outfits: dict[str, set[str]] = defaultdict(set)
    details: dict[tuple[str, str], set[str]] = defaultdict(set)
    display: dict[tuple[str, str], str] = {}
    for image in images:
        if should_cancel is not None and should_cancel():
            raise InterruptedError("Look focus review cancelled.")
        if (image.library_id not in scope or image.state != "current"
                or image.annotation is None):
            continue
        for occurrence in image.annotation.characters:
            if owner_kind == "character":
                if occurrence.character_id != owner_id:
                    continue
                if len(occurrence.outfit_ids) == 1:
                    outfits[occurrence.outfit_ids[0]].add(image.image_id)
                groups = ("appearance", "clothing")
            else:
                if owner_id not in occurrence.outfit_ids or len(occurrence.outfit_ids) != 1:
                    continue
                groups = ("clothing",)
            for group in groups:
                for value in occurrence.tags.get(group, ()):
                    normalized = normalize_tag(value)
                    if normalized:
                        key = group, normalized
                        details[key].add(image.image_id)
                        display.setdefault(key, value)
    ordered_outfits = sorted(outfits, key=lambda value: (-len(outfits[value]), value))
    ordered_details = sorted(details, key=lambda key: (-len(details[key]), key))
    options = [SuggestionFocusOption(SuggestionFocus("character_outfit", outfit_id),
                                     outfit_id, len(outfits[outfit_id]))
               for outfit_id in ordered_outfits[:max_outfits]]
    options.extend(SuggestionFocusOption(
        SuggestionFocus("owner_detail", key[1], key[0]), display[key], len(details[key])
    ) for key in ordered_details[:max_details])
    return SuggestionFocusCatalog(tuple(options),
                                  max(0, len(ordered_outfits) - max_outfits) +
                                  max(0, len(ordered_details) - max_details))


def suggest_character_outfit_setup(
    owner_kind: str,
    owner_id: str,
    images: Iterable[SuggestionImage],
    *,
    included_library_ids: Iterable[str],
    representative_tag_id: str = "",
    representative_names: Iterable[str] = (),
    focus: SuggestionFocus | None = None,
    evaluator=None,
    qualified_relationship_ids: Iterable[str] = (),
    should_cancel: Callable[[], bool] | None = None,
    max_images: int = 100_000,
    max_suggestions: int = 80,
    max_clothing_groups: int = 8,
) -> SetupSuggestionReport:
    """Count distinct scoped images without promoting co-occurrence to ownership.

    ``image_id`` should be a trustworthy known image identity when available;
    callers may use a scoped path identity when bytes are not verified. Copies
    with the same supplied identity contribute at most once to every count.
    """
    if owner_kind not in {"character", "outfit"} or not owner_id:
        raise ValueError("Choose one canonical Character or Outfit.")
    _validate_focus(owner_kind, focus)
    branch_roots = ()
    if focus is not None and focus.mode == "reviewed_branch":
        constraint = _qualified_look_constraints(evaluator, qualified_relationship_ids).get(focus.group)
        if constraint is None or focus.value not in constraint[1]:
            raise ValueError("The reviewed look branch is unavailable; refresh suggestions.")
        branch_roots = constraint[1]
    scope = frozenset(str(value) for value in included_library_ids if value)
    if not scope:
        raise ValueError("Choose at least one included image library.")
    if max_images < 1 or max_suggestions < 1 or max_clothing_groups < 0:
        raise ValueError("Choose positive analysis limits.")
    names = frozenset(normalize_tag(value) for value in representative_names if value)
    support: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    examples: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    source_examples: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    source_refs: dict[tuple[str, str, str], list[SuggestionSourceRef]] = defaultdict(list)
    image_clothing_groups: dict[str, set[tuple[str, ...]]] = defaultdict(set)
    image_starter_groups: dict[str, set[tuple[str, ...]]] = defaultdict(set)
    starter_display: dict[str, str] = {}
    clothing_display: dict[tuple[str, ...], tuple[str, ...]] = {}
    clothing_examples: dict[tuple[str, ...], dict[str, str]] = defaultdict(dict)
    long_clothing_images: set[str] = set()
    eligible: set[str] = set()
    assigned: set[str] = set()
    ambiguous: set[str] = set()
    stale: set[str] = set()
    unavailable: set[str] = set()
    seen_copies: set[str] = set()
    duplicate_ids: set[str] = set()
    known_multi_owner: set[str] = set()
    context_sources: dict[str, tuple[str, SuggestionSourceRef | None, tuple[str, ...]]] = {}
    duplicate_copies = 0

    def add(kind: str, text: str, source_type: str, image: SuggestionImage,
            *, source_text: str = "") -> None:
        value = text.strip()
        if not value or len(value) > 4096:
            return
        key = kind, normalize_tag(value), source_type
        if not key[1]:
            return
        new_support = image.image_id not in support[key]
        support[key].add(image.image_id)
        sample = examples[key]
        if new_support and image.label and image.label not in sample and len(sample) < 3:
            sample.append(image.label)
        if new_support and image.source_ref is not None and len(source_refs[key]) < 3:
            source_refs[key].append(image.source_ref)
        if source_text and new_support:
            source_sample = source_examples[key]
            evidence = f"{image.label}: {source_text.strip()}"
            if evidence not in source_sample and len(source_sample) < 3:
                source_sample.append(evidence)
        display.setdefault(key, value)

    display: dict[tuple[str, str, str], str] = {}
    for position, image in enumerate(images, 1):
        if position > max_images:
            raise ValueError("The selected scope exceeds the bounded analysis limit.")
        if should_cancel is not None and should_cancel():
            raise InterruptedError("Character/Outfit suggestion review cancelled.")
        if image.library_id not in scope:
            continue
        if not image.image_id:
            raise ValueError("Each scoped image needs an exact or path-scoped identity.")
        if image.image_id in seen_copies:
            duplicate_copies += 1
            duplicate_ids.add(image.image_id)
        else:
            seen_copies.add(image.image_id)
        if image.state == "stale":
            stale.add(image.image_id)
            image_clothing_groups[image.image_id].add(())
            image_starter_groups[image.image_id].add(())
            continue
        if image.state == "unavailable" or image.annotation is None:
            unavailable.add(image.image_id)
            image_clothing_groups[image.image_id].add(())
            image_starter_groups[image.image_id].add(())
            continue
        if image.state != "current" or not isinstance(image.annotation, ImageAnnotation):
            raise ValueError("Suggestion sources must be current reviewed Image Knowledge.")
        annotation = image.annotation
        if len(annotation.characters) > 1:
            known_multi_owner.add(image.image_id)
        owners = (tuple(character for character in annotation.characters
                        if character.character_id == owner_id) if owner_kind == "character" else
                  tuple(character for character in annotation.characters
                        if owner_id in character.outfit_ids))
        if focus is not None:
            if focus.mode == "character_outfit":
                owners = tuple(character for character in owners
                               if character.outfit_ids == [focus.value])
            elif focus.mode == "reviewed_branch":
                owners = _reviewed_branch_owners(
                    owner_kind, owners, annotation, branch_roots, focus.value, evaluator,
                )
            else:
                owners = tuple(character for character in owners
                               if (owner_kind != "outfit" or len(character.outfit_ids) == 1)
                               and normalize_tag(focus.value) in {
                                   normalize_tag(value)
                                   for value in character.tags.get(focus.group, ())
                               })
            if not owners:
                continue
        image_wide = _image_wide_identity(annotation, tag_id=representative_tag_id,
                                          names=names)
        if not owners and not image_wide:
            image_clothing_groups[image.image_id].add(())
            image_starter_groups[image.image_id].add(())
            continue
        eligible.add(image.image_id)
        # Retain one bounded-scope source per distinct identity until final
        # duplicate/multi-owner exclusions are known. Generic suggestion
        # samples cannot be reused: their first three may all be excluded.
        context_sources.setdefault(
            image.image_id, (image.label, image.source_ref, tuple(annotation.plain_language))
        )
        if not owners:
            image_clothing_groups[image.image_id].add(())
            image_starter_groups[image.image_id].add(())
        if owners:
            # One character may have several outfit links. In that case the
            # clothing cannot be attributed to a particular outfit.
            precise = owner_kind == "character" or all(
                len(character.outfit_ids) == 1 for character in owners
            )
            if precise:
                assigned.add(image.image_id)
                for character in owners:
                    clothing = {}
                    invalid_clothing = False
                    for value in character.tags.get("clothing", ()):
                        stripped = value.strip()
                        if not stripped:
                            continue
                        normalized = normalize_tag(value)
                        if not normalized or len(stripped) > 256:
                            invalid_clothing = True
                            continue
                        clothing.setdefault(normalized, stripped)
                    key = tuple(sorted(clothing))
                    image_clothing_groups[image.image_id].add(() if invalid_clothing else key)
                    starter_values = (character.tags.get("appearance", ())
                                      if owner_kind == "character" else
                                      character.tags.get("clothing", ()))
                    starter = {}
                    invalid_starter = False
                    for value in starter_values:
                        stripped = value.strip()
                        normalized = normalize_tag(value)
                        if stripped and (not normalized or len(stripped) > 256):
                            invalid_starter = True
                        elif normalized:
                            starter.setdefault(normalized, stripped)
                    starter_key = tuple(sorted(starter))
                    image_starter_groups[image.image_id].add(
                        () if invalid_starter or len(starter_key) > 12 else starter_key
                    )
                    if not invalid_starter and len(starter_key) <= 12:
                        for normalized, text in starter.items():
                            starter_display.setdefault(normalized, text)
                    if invalid_clothing or len(key) > 12:
                        long_clothing_images.add(image.image_id)
                    elif len(key) >= 2:
                        clothing_display.setdefault(key, tuple(clothing[part] for part in key))
                        if image.label:
                            clothing_examples[key].setdefault(image.image_id, image.label)
                    groups = (("appearance", "character"), ("clothing", "outfit"),
                              ("pose", "pose"), ("expression", "pose"),
                              ("actions", "pose"))
                    for group, kind in groups:
                        if owner_kind == "outfit" and group != "clothing":
                            continue
                        for value in character.tags.get(group, ()):
                            add(kind, value, "owner_tag", image)
            else:
                ambiguous.add(image.image_id)
                image_clothing_groups[image.image_id].add(())
                image_starter_groups[image.image_id].add(())
        else:
            ambiguous.add(image.image_id)
        # These are useful optional context, never owner-specific appearance.
        for values, kind in ((annotation.global_tags.get("style", ()), "style"),
                             (annotation.visual_style.style_tags, "style")):
            for value in values:
                add(kind, value, "image_tag", image)
        for values in annotation.scene.values():
            for value in values:
                add("scene", value, "image_tag", image)
        excerpt_budget = 32
        for description_index, value in enumerate(annotation.plain_language):
            add("other", value, "description", image, source_text=value)
            if description_index >= 8:
                continue
            for excerpt in _description_excerpts(value):
                if excerpt_budget <= 0:
                    break
                if normalize_tag(excerpt) != normalize_tag(value):
                    add("other", excerpt, "description_excerpt", image,
                        source_text=value)
                    excerpt_budget -= 1
        # Only explicit image-wide confirmed tags are presented as
        # co-occurrence. They cannot become selected-owner traits, even when
        # that owner has other precisely assigned facts in the same image.
        image_wide_tags = [*annotation.sdxl_tags, *annotation.uncategorized]
        for values in annotation.global_tags.values():
            image_wide_tags.extend(values)
        for values in annotation.custom_groups.values():
            image_wide_tags.extend(values)
        for value in image_wide_tags:
            if normalize_tag(value) not in names:
                add("other", value, "image_tag", image)

    denominator = len(eligible)
    source_order = {"owner_tag": 0, "image_tag": 1, "description": 2,
                    "description_excerpt": 3}
    recurring_excerpts = sorted(
        (key for key in support if key[2] == "description_excerpt" and
         len(support[key]) >= 2),
        key=lambda key: (-len(support[key]), -len(key[1].split()), key[1]),
    )
    owner_support_by_text: dict[str, set[str]] = defaultdict(set)
    for key, image_ids in support.items():
        if key[2] == "owner_tag":
            owner_support_by_text[key[1]].update(image_ids)
    retained_excerpts = []
    for key in recurring_excerpts:
        if support[key].issubset(owner_support_by_text.get(key[1], ())):
            continue
        if any(support[key] == support[other] and
               f" {key[1]} " in f" {other[1]} " for other in retained_excerpts):
            continue
        retained_excerpts.append(key)
        if len(retained_excerpts) >= 12:
            break
    retained = set(retained_excerpts)
    ranked = sorted((key for key in support if key[2] != "description_excerpt" or
                     key in retained), key=lambda key: (
        -len(support[key]), source_order[key[2]], key[0], key[1],
    ))
    # A large set of one-off traits must not hide repeatedly described details
    # (or vice versa). Reserve a small, recurrence-ranked share for each source
    # type, then fill the remaining shortlist by distinct-image support.
    reserve = max(1, max_suggestions // 5)
    chosen: set[tuple[str, str, str]] = set()
    if max_suggestions >= len(source_order):
        for source_type in source_order:
            source_count = 0
            for key in ranked:
                if key[2] != source_type:
                    continue
                chosen.add(key)
                source_count += 1
                if source_count >= reserve:
                    break
    for key in ranked:
        if len(chosen) >= max_suggestions:
            break
        chosen.add(key)
    shortlist = [key for key in ranked if key in chosen]
    suggestions = tuple(
        SetupSuggestion(key[0], display[key], key[2], len(support[key]),
                        denominator, tuple(examples[key]),
                        tuple(source_examples[key]), tuple(source_refs[key]))
        for key in shortlist
    )
    # Offer a compact, explicitly image-wide fallback when no owner-assigned
    # starter exists. Counts for this shortlist omit known multi-person and
    # duplicate-copy images; unknown subject ownership remains unknown.
    context_excluded = eligible.intersection(
        known_multi_owner | duplicate_ids | stale | unavailable
    )
    context_supporters = eligible - context_excluded
    context_ranked = []
    for key in shortlist:
        if key[2] not in {"image_tag", "description", "description_excerpt"}:
            continue
        if key[1] in owner_support_by_text or len(display[key]) > 120:
            continue
        count = len(support[key] & context_supporters)
        if count >= 2:
            context_ranked.append((key, count))
    context_ranked.sort(key=lambda item: (
        -item[1], {"image_tag": 0, "description_excerpt": 1, "description": 2}[item[0][2]],
        item[0][1],
    ))
    context_ideas = []
    seen_context_text = set()
    for key, count in context_ranked:
        if key[1] in seen_context_text:
            continue
        seen_context_text.add(key[1])
        supporters = support[key] & context_supporters
        labels = []
        sentences = []
        refs = []
        for image_id, (label, ref, descriptions) in context_sources.items():
            if image_id not in supporters:
                continue
            if label and len(labels) < 3:
                labels.append(label)
            if ref is not None and len(refs) < 3:
                refs.append(ref)
            if key[2] in {"description", "description_excerpt"} and len(sentences) < 3:
                original = next((value for value in descriptions
                                 if key[1] in normalize_tag(value)), None)
                if original is not None:
                    sentences.append(f"{label}: {original}")
            if len(labels) >= 3 and len(refs) >= 3 and (
                    key[2] == "image_tag" or len(sentences) >= 3):
                break
        context_ideas.append(ImageContextIdea(
            display[key], key[2], count, denominator,
            tuple(labels), tuple(sentences), tuple(refs),
        ))
        if len(context_ideas) >= 6:
            break
    # An image with divergent duplicate sidecars or several different owner
    # occurrences cannot establish which complete clothing set it supports.
    group_support: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for image_id, groups in image_clothing_groups.items():
        if len(groups) == 1:
            group_support[next(iter(groups))].add(image_id)
    ranked_groups = sorted(
        (key for key, ids in group_support.items()
         if 2 <= len(key) <= 12 and len(ids) >= 2),
        key=lambda key: (-len(group_support[key]), key),
    )
    # A complete set is often tagged unevenly: {coat, skirt} and
    # {coat, skirt, boots} still support the shared pair. Limit the vocabulary
    # before making pairs so this remains bounded for very large libraries.
    clothing_frequency: dict[str, int] = defaultdict(int)
    for key, image_ids in group_support.items():
        if 2 <= len(key) <= 12:
            for detail in key:
                clothing_frequency[detail] += len(image_ids)
    top_details = sorted(clothing_frequency,
                         key=lambda detail: (-clothing_frequency[detail], detail))[:48]
    allowed_details = set(top_details)
    pair_support: dict[tuple[str, str], int] = defaultdict(int)
    pair_examples: dict[tuple[str, str], list[str]] = defaultdict(list)
    detail_display: dict[str, str] = {}
    for key in sorted(group_support):
        if should_cancel is not None and should_cancel():
            raise InterruptedError("Clothing suggestion review stopped.")
        if not 2 <= len(key) <= 12:
            continue
        detail_display.update({detail: text for detail, text in
                               zip(key, clothing_display.get(key, key))
                               if detail not in detail_display})
        image_ids = group_support[key]
        for pair in combinations((detail for detail in key if detail in allowed_details), 2):
            pair_support[pair] += len(image_ids)
            if len(pair_examples[pair]) < 3:
                pair_examples[pair].extend(
                    clothing_examples[key][image_id]
                    for image_id in sorted(image_ids)
                    if image_id in clothing_examples[key]
                )
                del pair_examples[pair][3:]
    redundant_pairs: set[tuple[str, str]] = set()
    for exact in ranked_groups:
        count = len(group_support[exact])
        for pair in combinations((detail for detail in exact if detail in allowed_details), 2):
            if pair_support[pair] == count:
                redundant_pairs.add(pair)
    shared_pairs = [pair for pair, count in pair_support.items()
                    if count >= 2 and pair not in redundant_pairs]
    ranked_choices = sorted(
        [(key, True, len(group_support[key])) for key in ranked_groups] +
        [(key, False, pair_support[key]) for key in shared_pairs],
        key=lambda choice: (-choice[2], not choice[1], -len(choice[0]), choice[0]),
    )
    clothing_groups = tuple(
        RecurringClothingGroup(
            clothing_display[key] if complete else tuple(detail_display[part] for part in key),
            count, denominator,
            tuple(clothing_examples[key][image_id]
                  for image_id in sorted(group_support[key])
                  if image_id in clothing_examples[key])[:3]
            if complete else tuple(pair_examples[key]),
            complete,
        )
        for key, complete, count in ranked_choices[:max_clothing_groups]
    )
    starter_proposal = _starter_from_owner_groups(
        owner_kind, image_starter_groups, starter_display, denominator,
        should_cancel=should_cancel,
    )
    return SetupSuggestionReport(
        owner_kind, owner_id, denominator, len(assigned), len(ambiguous),
        len(stale), len(unavailable), duplicate_copies, suggestions,
        max(0, len(ranked) - max_suggestions),
        clothing_groups, max(0, len(ranked_choices) - max_clothing_groups),
        len(long_clothing_images), len(clothing_frequency) > len(top_details),
        starter_proposal, tuple(context_ideas), len(context_excluded),
    )
