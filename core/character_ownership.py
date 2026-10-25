from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .annotation import CHARACTER_TAG_GROUPS, CharacterAnnotation
from .tag_collection import TagCollection, normalize_tag


CHARACTER_OWNERSHIP_GROUP_LABELS = {
    "appearance": "Appearance",
    "clothing": "Clothing",
    "pose": "Pose",
    "expression": "Expression",
    "actions": "Actions",
    "other": "Other",
}


class CharacterOwnershipError(ValueError):
    """Raised when an image-specific Character owner choice is not exact."""


@dataclass(frozen=True)
class CharacterOwnerChoice:
    key: str
    tag_id: str
    character_id: str
    assignment_id: str
    label: str
    existing: bool


@dataclass(frozen=True)
class CharacterOwnershipFact:
    tag_id: str
    display: str
    role: str
    linked_id: str
    owner_keys: tuple[str, ...]
    owner_groups: tuple[str, ...]
    preserved_owner_count: int = 0

    @property
    def role_label(self) -> str:
        if self.role == "character_outfit":
            return "Character Outfit"
        return "Confirmed Tag"


@dataclass(frozen=True)
class CharacterOwnershipView:
    owners: tuple[CharacterOwnerChoice, ...]
    facts: tuple[CharacterOwnershipFact, ...]


class CharacterOwnershipService:
    """Review exact optional per-image ownership without duplicating Tag facts."""

    def __init__(self, tags, characters, categories=None) -> None:
        self.tags = tags
        self.characters = characters
        self.categories = categories

    def _role(self, tag) -> str:
        if tag is None:
            return ""
        role = (
            self.categories.effective_semantic_role(tag.category)
            if self.categories is not None else tag.category
        )
        if role == "copyright_series":
            role = "character_source"
        return str(role or "")

    def _reviewed_link_index(self):
        links = {
            "character_identity": {},
            "character_outfit": {},
        }
        for role, records, kind in (
            (
                "character_identity",
                self.characters.characters.values(),
                "character",
            ),
            (
                "character_outfit",
                self.characters.outfits.values(),
                "outfit",
            ),
        ):
            for record in records:
                link = self.characters.effective_knowledge_tag(kind, record.id)
                if not isinstance(link, dict) or link.get("status") != "reviewed":
                    continue
                tag_id = str(link.get("tag_id") or "")
                if tag_id:
                    links[role].setdefault(tag_id, []).append(record)
        return links

    def _stable_id(self, annotation, value: str) -> str:
        key = normalize_tag(value)
        return str(annotation.tag_identities.get(key) or "")

    def _matches_tag(self, annotation, value: str, tag_id: str) -> bool:
        return bool(tag_id) and self._stable_id(annotation, value) == tag_id

    @staticmethod
    def _image_wide_containers(annotation):
        for values in annotation.global_tags.values():
            yield values
        for values in annotation.scene.values():
            yield values
        for values in annotation.custom_groups.values():
            yield values
        yield annotation.sdxl_tags
        yield annotation.uncategorized
        yield annotation.visual_style.style_tags

    def _is_image_wide(self, annotation, tag_id: str) -> bool:
        return any(
            self._matches_tag(annotation, value, tag_id)
            for values in self._image_wide_containers(annotation)
            for value in values
        )

    def _remove_image_wide(self, annotation, tag_id: str) -> None:
        for values in self._image_wide_containers(annotation):
            values[:] = [
                value for value in values
                if not self._matches_tag(annotation, value, tag_id)
            ]

    def _owner_choices(self, annotation, values: Iterable[str], links):
        identities = []
        seen_characters = set()
        for value in values:
            tag = self.tags.find(value) if self.tags is not None else None
            if tag is None or self._role(tag) != "character_identity":
                continue
            if self._stable_id(annotation, value) != tag.id:
                continue
            linked = tuple(links["character_identity"].get(tag.id, ()))
            if len(linked) != 1 or linked[0].id in seen_characters:
                continue
            character = linked[0]
            seen_characters.add(character.id)
            matches = sorted(
                (
                    assignment for assignment in annotation.characters
                    if assignment.character_id == character.id
                ),
                key=lambda item: (item.slot, item.id),
            )
            if matches:
                for assignment in matches:
                    identities.append(CharacterOwnerChoice(
                        key=assignment.id,
                        tag_id=tag.id,
                        character_id=character.id,
                        assignment_id=assignment.id,
                        label=f"{character.name} — Character {assignment.slot}",
                        existing=True,
                    ))
            else:
                identities.append(CharacterOwnerChoice(
                    key=f"new:{character.id}",
                    tag_id=tag.id,
                    character_id=character.id,
                    assignment_id="",
                    label=character.name,
                    existing=False,
                ))
        return tuple(identities)

    def analyze(
        self, annotation, values: Iterable[str],
    ) -> CharacterOwnershipView:
        values = tuple(TagCollection(values))
        links = self._reviewed_link_index()
        owners = self._owner_choices(annotation, values, links)
        choices_by_assignment = {
            choice.assignment_id: choice
            for choice in owners if choice.assignment_id
        }
        facts = []
        seen_ids = set()
        for value in values:
            tag = self.tags.find(value) if self.tags is not None else None
            if tag is None or tag.id in seen_ids:
                continue
            if self._stable_id(annotation, value) != tag.id:
                continue
            role = self._role(tag)
            if role in {"character_identity", "character_source"}:
                continue
            linked_id = ""
            if role == "character_outfit":
                linked = tuple(links[role].get(tag.id, ()))
                if len(linked) != 1:
                    continue
                linked_id = linked[0].id
            owner_keys = []
            owner_groups = []
            preserved_owner_count = 0
            for assignment in annotation.characters:
                groups = []
                if role == "character_outfit":
                    if linked_id in assignment.outfit_ids:
                        groups.append("clothing")
                else:
                    groups.extend(
                        group
                        for group, group_values in assignment.tags.items()
                        if any(
                            self._matches_tag(annotation, raw, tag.id)
                            for raw in group_values
                        )
                    )
                if not groups:
                    continue
                choice = choices_by_assignment.get(assignment.id)
                if choice is None:
                    preserved_owner_count += 1
                    continue
                owner_keys.extend([choice.key] * len(groups))
                owner_groups.extend(groups)
            if not (
                role == "character_outfit"
                or self._is_image_wide(annotation, tag.id)
                or owner_keys
            ):
                continue
            seen_ids.add(tag.id)
            facts.append(CharacterOwnershipFact(
                tag_id=tag.id,
                display=value,
                role=role,
                linked_id=linked_id,
                owner_keys=tuple(dict.fromkeys(owner_keys)),
                owner_groups=tuple(dict.fromkeys(owner_groups)),
                preserved_owner_count=preserved_owner_count,
            ))
        facts.sort(key=lambda item: (
            0 if item.role == "character_outfit" else 1,
            item.display.casefold(), item.tag_id,
        ))
        return CharacterOwnershipView(owners=owners, facts=tuple(facts))

    @staticmethod
    def _bare_assignment(assignment: CharacterAnnotation) -> bool:
        return bool(assignment.character_id) and not (
            assignment.role
            or assignment.outfit_ids
            or assignment.all_tags()
            or assignment.extra_fields
        )

    def _remove_fact_from_assignment(
        self, annotation, assignment, fact: CharacterOwnershipFact,
    ) -> None:
        if fact.role == "character_outfit":
            assignment.outfit_ids = [
                value for value in assignment.outfit_ids
                if value != fact.linked_id
            ]
            return
        for group, values in assignment.tags.items():
            assignment.tags[group] = [
                value for value in values
                if not self._matches_tag(annotation, value, fact.tag_id)
            ]

    def _choice_assignment(self, annotation, choice: CharacterOwnerChoice):
        if choice.assignment_id:
            assignment = next((
                item for item in annotation.characters
                if item.id == choice.assignment_id
            ), None)
            if assignment is None or assignment.character_id != choice.character_id:
                raise CharacterOwnershipError(
                    "The selected Character ownership context is no longer current."
                )
            return assignment
        return annotation.add_character(character_id=choice.character_id)

    def set_fact_owners(
        self, annotation, values: Iterable[str], *, fact_tag_id: str,
        owner_keys: Iterable[str], group: str = "",
    ) -> bool:
        view = self.analyze(annotation, values)
        fact = next(
            (item for item in view.facts if item.tag_id == fact_tag_id), None,
        )
        if fact is None:
            raise CharacterOwnershipError(
                "The selected confirmed Tag is no longer eligible for Character ownership."
            )
        choices = {choice.key: choice for choice in view.owners}
        selected = tuple(dict.fromkeys(str(value) for value in owner_keys if str(value)))
        if any(key not in choices for key in selected):
            raise CharacterOwnershipError(
                "A selected Character identity is no longer confirmed on this image."
            )
        if fact.role != "character_outfit" and selected:
            if group not in CHARACTER_TAG_GROUPS:
                raise CharacterOwnershipError(
                    "Choose the Character fact group before applying ownership."
                )
        before = annotation.to_dict()
        touched = set()
        for choice in view.owners:
            if not choice.assignment_id:
                continue
            assignment = next((
                item for item in annotation.characters
                if item.id == choice.assignment_id
            ), None)
            if assignment is None:
                continue
            self._remove_fact_from_assignment(annotation, assignment, fact)
            touched.add(assignment.id)
        for key in selected:
            assignment = self._choice_assignment(annotation, choices[key])
            touched.add(assignment.id)
            if fact.role == "character_outfit":
                if fact.linked_id not in assignment.outfit_ids:
                    assignment.outfit_ids.append(fact.linked_id)
            else:
                assignment.tags[group] = TagCollection((
                    *assignment.tags.get(group, ()), fact.display,
                )).to_list()
        annotation.characters = [
            assignment for assignment in annotation.characters
            if assignment.id not in touched or not self._bare_assignment(assignment)
        ]
        annotation.sort_characters()
        self.place_fact(
            annotation, fact_tag_id=fact.tag_id, display=fact.display,
            owned=bool(selected), role=fact.role,
        )
        return annotation.to_dict() != before

    def place_fact(
        self, annotation, *, fact_tag_id: str, display: str,
        owned: bool, role: str = "",
    ) -> None:
        tag = self.tags.tags.get(fact_tag_id) if self.tags is not None else None
        role = self._role(tag) or role
        if role == "character_outfit":
            return
        if owned:
            self._remove_image_wide(annotation, fact_tag_id)
            return
        if not self._is_image_wide(annotation, fact_tag_id):
            annotation.sdxl_tags = TagCollection((
                *annotation.sdxl_tags, display,
            )).to_list()
