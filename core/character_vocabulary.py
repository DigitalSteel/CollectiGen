from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Literal

from .character_database import CharacterDatabase
from .character_tag_bridge import CharacterTagBridge
from .tag_collection import normalize_tag
from .tag_database import TagDatabase


CharacterVocabularyStatus = Literal[
    "none",
    "exact",
    "series_aware",
    "variant_review",
    "ambiguous",
    "unlinked",
    "source_conflict",
]


@dataclass(frozen=True)
class CharacterVocabularySuggestion:
    """One non-mutating character-vocabulary interpretation for intake review."""

    status: CharacterVocabularyStatus = "none"
    category: str = ""
    confidence: float = 0.0
    reason: str = ""
    alias_target_id: str = ""
    character_id: str = ""
    source_id: str = ""
    source_tag_id: str = ""
    base_character_tag_id: str = ""
    variant_groups: tuple[str, ...] = ()

    @property
    def actionable(self) -> bool:
        return bool(self.category)

    def details(self) -> dict:
        payload = asdict(self)
        payload["variant_groups"] = list(self.variant_groups)
        return payload


def trailing_parenthetical_groups(value: str) -> tuple[str, tuple[str, ...], bool]:
    """Return the unqualified stem and balanced trailing groups.

    The parser deliberately assigns no meaning to the groups. Callers still
    need exact Character Source and typed-record evidence before using them.
    """

    text = str(value).strip()
    groups: list[str] = []
    nested = False
    while text.endswith(")"):
        depth = 0
        opening = -1
        for index in range(len(text) - 1, -1, -1):
            character = text[index]
            if character == ")":
                depth += 1
            elif character == "(":
                depth -= 1
                if depth == 0:
                    opening = index
                    break
                if depth < 0:
                    return str(value).strip(), (), True
        if opening < 0 or depth != 0:
            return str(value).strip(), (), True
        group = text[opening + 1 : -1].strip()
        if not group:
            return str(value).strip(), (), True
        if "(" in group or ")" in group:
            nested = True
        groups.insert(0, group)
        text = text[:opening].rstrip()
    return text, tuple(groups), nested


class CharacterVocabularyResolver:
    """Resolve startup vocabulary against reviewed tag-rooted identities.

    Results are proposals only. The resolver never writes System Knowledge,
    creates a typed record, or confirms a tag on an image.
    """

    CHARACTER_IDENTITY_CATEGORY = "character_identity"
    CHARACTER_SOURCE_CATEGORY = "copyright_series"
    CHARACTER_OUTFIT_CATEGORY = "character_outfit"
    _SHORT_NAME_STOPWORDS = {
        "alter",
        "adult",
        "child",
        "lily",
        "rider",
        "saber",
        "small",
        "summer",
        "young",
    }

    def __init__(
        self,
        tags: TagDatabase,
        characters: CharacterDatabase,
        categories=None,
    ) -> None:
        self.tags = tags
        self.characters = characters
        self.categories = categories
        self._character_names: dict[str, set[str]] = {}
        self._source_names: dict[str, set[str]] = {}
        self._short_names: dict[tuple[str, str], set[str]] = {}
        self._character_tag_ids: dict[str, str] = {}
        self._source_tag_ids: dict[str, str] = {}
        self._build_indexes()

    def _role_for_tag(self, tag) -> str:
        if tag is None:
            return ""
        if self.categories is not None:
            return str(self.categories.effective_semantic_role(tag.category) or "")
        return {
            self.CHARACTER_IDENTITY_CATEGORY: "character_identity",
            self.CHARACTER_SOURCE_CATEGORY: "character_source",
            self.CHARACTER_OUTFIT_CATEGORY: "character_outfit",
        }.get(str(tag.category), "")

    @staticmethod
    def _add(index: dict[str, set[str]], value: str, stable_id: str) -> None:
        key = normalize_tag(value)
        if key:
            index.setdefault(key, set()).add(stable_id)

    @classmethod
    def _short_name(cls, value: str) -> str:
        stem, groups, nested = trailing_parenthetical_groups(value)
        if nested:
            return ""
        if groups:
            stem = stem.strip()
        tokens = re.findall(r"[a-z0-9]+", normalize_tag(stem))
        if len(tokens) < 2:
            return ""
        candidate = tokens[-1]
        if len(candidate) < 4 or candidate in cls._SHORT_NAME_STOPWORDS:
            return ""
        return candidate

    def _build_indexes(self) -> None:
        bridge = CharacterTagBridge(self.tags, self.characters, self.categories).report()
        for row in bridge.rows:
            if row.status not in {"linked", "ready"} or len(row.tag_ids) != 1:
                continue
            tag = self.tags.tags.get(row.tag_ids[0])
            if row.entity_kind == "character" and self._role_for_tag(tag) == "character_identity":
                self._character_tag_ids[row.entity_id] = row.tag_ids[0]
            elif row.entity_kind == "series" and self._role_for_tag(tag) == "character_source":
                self._source_tag_ids[row.entity_id] = row.tag_ids[0]

        for source in self.characters.series.values():
            for value in (source.name, *source.aliases):
                self._add(self._source_names, value, source.id)
            tag = self.tags.tags.get(self._source_tag_ids.get(source.id, ""))
            if tag is not None:
                for value in self.tags.identity_names(tag):
                    self._add(self._source_names, value, source.id)

        for character in self.characters.characters.values():
            for value in (character.name, *character.aliases):
                self._add(self._character_names, value, character.id)
            short = self._short_name(character.name)
            source_id = self.characters.character_source_id("character", character.id)
            if short and source_id:
                self._short_names.setdefault((source_id, short), set()).add(character.id)

    def _character_result(
        self,
        character_id: str,
        *,
        status: CharacterVocabularyStatus,
        confidence: float,
        source_id: str = "",
        reason: str,
    ) -> CharacterVocabularySuggestion:
        target = self._character_tag_ids.get(character_id, "")
        if not target:
            return CharacterVocabularySuggestion(
                status="unlinked",
                category=self.CHARACTER_IDENTITY_CATEGORY,
                confidence=min(confidence, 0.84),
                reason=(
                    reason
                    + " The typed character has no single compatible reviewed Character Identity Tag, so no alias target is preselected."
                ),
                character_id=character_id,
                source_id=source_id,
                source_tag_id=self._source_tag_ids.get(source_id, ""),
            )
        target_tag = self.tags.tags.get(target)
        target_category = (
            str(target_tag.category)
            if self._role_for_tag(target_tag) == "character_identity"
            else self.CHARACTER_IDENTITY_CATEGORY
        )
        return CharacterVocabularySuggestion(
            status=status,
            category=target_category,
            confidence=confidence,
            reason=reason,
            alias_target_id=target,
            character_id=character_id,
            source_id=source_id,
            source_tag_id=self._source_tag_ids.get(source_id, ""),
            base_character_tag_id=target,
        )

    def resolve(self, value: str) -> CharacterVocabularySuggestion:
        key = normalize_tag(value)
        if not key:
            return CharacterVocabularySuggestion()

        direct = sorted(self._character_names.get(key, ()))
        if len(direct) == 1:
            character = self.characters.characters[direct[0]]
            return self._character_result(
                character.id,
                status="exact",
                confidence=0.99,
                source_id=self.characters.character_source_id("character", character.id),
                reason="The observed name exactly matches one typed character preferred name or declared alias.",
            )
        if len(direct) > 1:
            return CharacterVocabularySuggestion(
                status="ambiguous",
                category=self.CHARACTER_IDENTITY_CATEGORY,
                confidence=0.65,
                reason="The observed name matches more than one typed character. Review Character Source before choosing an identity.",
            )

        stem, groups, nested = trailing_parenthetical_groups(value)
        if not groups or nested:
            return CharacterVocabularySuggestion()
        source_ids = sorted(self._source_names.get(normalize_tag(groups[-1]), ()))
        if len(source_ids) != 1:
            if len(source_ids) > 1:
                return CharacterVocabularySuggestion(
                    status="ambiguous",
                    category=self.CHARACTER_IDENTITY_CATEGORY,
                    confidence=0.55,
                    reason="The trailing name matches more than one Character Source. No identity or alias target was chosen.",
                )
            return CharacterVocabularySuggestion()

        source_id = source_ids[0]
        stem_key = normalize_tag(stem)
        candidates = {
            character_id
            for character_id in self._character_names.get(stem_key, ())
            if self.characters.character_source_id("character", character_id) == source_id
        }
        candidates.update(self._short_names.get((source_id, stem_key), ()))
        if not candidates:
            cross_source = sorted(self._character_names.get(stem_key, ()))
            if cross_source:
                return CharacterVocabularySuggestion(
                    status="source_conflict",
                    category=self.CHARACTER_IDENTITY_CATEGORY,
                    confidence=0.6,
                    reason="The name matches typed character knowledge, but not within the exact Character Source suffix. Review the source conflict.",
                    source_id=source_id,
                    source_tag_id=self._source_tag_ids.get(source_id, ""),
                )
            return CharacterVocabularySuggestion()
        if len(candidates) > 1:
            return CharacterVocabularySuggestion(
                status="ambiguous",
                category=self.CHARACTER_IDENTITY_CATEGORY,
                confidence=0.7,
                reason="The name and Character Source still match more than one typed character. No alias target was chosen.",
                source_id=source_id,
                source_tag_id=self._source_tag_ids.get(source_id, ""),
            )

        character_id = next(iter(candidates))
        target = self._character_tag_ids.get(character_id, "")
        variant_groups = tuple(groups[:-1])
        if variant_groups:
            return CharacterVocabularySuggestion(
                status="variant_review",
                category="uncategorized",
                confidence=0.96,
                reason=(
                    "The stem and Character Source resolve to one reviewed base identity, while additional qualifiers remain. "
                    "Keep the exact composite Tag and choose whether it represents a Character Identity variation, "
                    "an outfit collection, or another meaning. Parentheses do not decide that role, and the Tag must "
                    "not be aliased to the base identity."
                ),
                character_id=character_id,
                source_id=source_id,
                source_tag_id=self._source_tag_ids.get(source_id, ""),
                base_character_tag_id=target,
                variant_groups=variant_groups,
            )
        return self._character_result(
            character_id,
            status="series_aware",
            confidence=0.98,
            source_id=source_id,
            reason="The short name is unique within the exact reviewed Character Source, so the canonical Character Identity Tag can be proposed for alias review.",
        )
