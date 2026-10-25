from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Iterable

from .annotation import CONTEXT_KINDS, ImageAnnotation, ImageContextAnnotation
from .tag_collection import TagCollection, normalize_tag


class ImageContextError(ValueError):
    """A secondary-context edit would create ambiguous Image Knowledge."""


@dataclass(frozen=True)
class ImageContextPlacement:
    context_id: str
    label: str


def _main_tag_containers(annotation: ImageAnnotation):
    for groups in (
        annotation.global_tags,
        annotation.scene,
        annotation.custom_groups,
    ):
        yield from groups.values()
    for character in annotation.characters:
        yield from character.tags.values()
    for item in annotation.objects:
        yield item.tags
    yield annotation.visual_style.style_tags
    yield annotation.sdxl_tags
    yield annotation.uncategorized


def context_placements(annotation: ImageAnnotation) -> dict[str, ImageContextPlacement]:
    placements: dict[str, ImageContextPlacement] = {}
    for context in annotation.contexts:
        for value in context.tags:
            key = normalize_tag(value)
            if key:
                placements[key] = ImageContextPlacement(
                    context.id, context.display_label,
                )
    return placements


def validate_image_contexts(
    annotation: ImageAnnotation,
    contexts: Iterable[ImageContextAnnotation] | None = None,
) -> None:
    """Fail closed when a secondary-context partition is not unambiguous."""

    values = list(annotation.contexts if contexts is None else contexts)
    context_ids: set[str] = set()
    placed: dict[str, str] = {}
    object_ids = {item.id for item in annotation.objects}
    main_keys = TagCollection(
        annotation.all_structured_tags(include_contexts=False)
    ).normalized_set()
    structural_identities = {
        normalize_tag(item.identity_tag)
        for item in annotation.objects
        if item.identity_tag
    }
    for context in values:
        if not context.id or context.id in context_ids:
            raise ImageContextError(
                "Every secondary image context must have one unique stable ID."
            )
        context_ids.add(context.id)
        if context.kind not in CONTEXT_KINDS:
            raise ImageContextError(
                f"{context.display_label!r} uses an unsupported context kind."
            )
        if context.owner_kind not in {"", "tag", "object"}:
            raise ImageContextError(
                f"{context.display_label!r} uses an unsupported owner kind."
            )
        if context.owner_kind and not (
            context.owner_id.strip() and context.owner_label.strip()
        ):
            raise ImageContextError(
                f"{context.display_label!r} has an incomplete owning Tag or object."
            )
        if context.owner_kind == "object" and context.owner_id not in object_ids:
            raise ImageContextError(
                f"{context.display_label!r} refers to a missing object assignment."
            )
        if context.owner_kind == "tag":
            owner_key = normalize_tag(context.owner_label)
            if (
                not owner_key
                or annotation.tag_identities.get(owner_key) != context.owner_id
                or owner_key not in main_keys
            ):
                raise ImageContextError(
                    f"{context.display_label!r} must refer to one confirmed Main "
                    "Scene Tag with its stable System Knowledge identity."
                )
        for tag in context.tags:
            key = normalize_tag(tag)
            if not key:
                continue
            if context.owner_kind == "tag" and key == normalize_tag(
                context.owner_label
            ):
                raise ImageContextError(
                    f"{tag!r} owns {context.display_label!r} and must remain in "
                    "the Main Scene."
                )
            if key in structural_identities:
                raise ImageContextError(
                    f"{tag!r} identifies an object assignment and must remain in the "
                    "Main Scene. Use that object as the context owner instead."
                )
            previous = placed.get(key)
            if previous and previous != context.id:
                raise ImageContextError(
                    f"{tag!r} is assigned to more than one secondary image context."
                )
            placed[key] = context.id


def apply_context_partition(
    annotation: ImageAnnotation,
    contexts: Iterable[ImageContextAnnotation],
    *,
    confirmed_values: Iterable[str] | None = None,
) -> None:
    """Apply one staged context partition without flattening its facts."""

    candidate = deepcopy(annotation)
    desired = (
        TagCollection(confirmed_values).normalized_set()
        if confirmed_values is not None else None
    )
    candidate.contexts = deepcopy(list(contexts))
    for context in candidate.contexts:
        context.tags = TagCollection(
            value
            for value in context.tags
            if desired is None or normalize_tag(value) in desired
        ).to_list()
    candidate.contexts = [
        context for context in candidate.contexts if context.tags
    ]
    validate_image_contexts(candidate)
    placed = set(context_placements(candidate))
    for container in _main_tag_containers(candidate):
        container[:] = [
            value for value in container if normalize_tag(value) not in placed
        ]
    validate_image_contexts(candidate)
    annotation.contexts = candidate.contexts
    for target, source in zip(
        _main_tag_containers(annotation),
        _main_tag_containers(candidate),
    ):
        target[:] = source


def move_image_facts(
    annotation: ImageAnnotation,
    values: Iterable[str],
    *,
    context_id: str | None,
) -> bool:
    """Move confirmed facts between Main Scene and one stable secondary context."""

    candidate = deepcopy(annotation)
    requested = TagCollection(values).to_list()
    requested_keys = TagCollection(requested).normalized_set()
    if not requested_keys:
        return False
    existing = {
        normalize_tag(value): value for value in candidate.all_structured_tags()
    }
    missing = [value for value in requested if normalize_tag(value) not in existing]
    if missing:
        raise ImageContextError(
            "Only confirmed image facts can be assigned to a context: "
            + ", ".join(missing)
        )
    destination = candidate.get_context(context_id or "") if context_id else None
    if context_id and destination is None:
        raise ImageContextError("The selected secondary image context no longer exists.")

    before = [context.to_dict() for context in candidate.contexts]
    for context in candidate.contexts:
        context.tags = [
            value for value in context.tags
            if normalize_tag(value) not in requested_keys
        ]
    if destination is not None:
        destination.tags = TagCollection([
            *destination.tags,
            *(existing[normalize_tag(value)] for value in requested),
        ]).to_list()
    candidate.contexts = [
        context for context in candidate.contexts if context.tags
    ]
    apply_context_partition(candidate, candidate.contexts)
    if destination is None:
        main_keys = TagCollection(
            candidate.all_structured_tags(include_contexts=False)
        ).normalized_set()
        candidate.sdxl_tags = TagCollection([
            *candidate.sdxl_tags,
            *(existing[normalize_tag(value)] for value in requested
              if normalize_tag(value) not in main_keys),
        ]).to_list()
    changed = before != [context.to_dict() for context in candidate.contexts]
    if not changed:
        return False
    annotation.contexts = candidate.contexts
    for target, source in zip(
        _main_tag_containers(annotation),
        _main_tag_containers(candidate),
    ):
        target[:] = source
    return True


def context_relation_description(context: ImageContextAnnotation) -> str:
    """Render a deterministic, evidence-preserving relationship sentence."""

    facts = context.all_tags()
    if not facts:
        return ""
    subject = ", ".join(facts[:-1]) + (
        f" and {facts[-1]}" if len(facts) > 1 else facts[0]
    )
    owner = context.owner_label.strip()
    relation = {
        "seen_through": "seen through",
        "reflected_in": "reflected in",
        "shown_on_screen": "shown on screen",
        "depicted_in_picture": "depicted in a picture",
    }[context.kind]
    suffix = f" {owner}" if owner else ""
    subject = subject[:1].upper() + subject[1:]
    return f"{subject} are {relation}{suffix}." if len(facts) > 1 else (
        f"{subject} is {relation}{suffix}."
    )
