from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import Callable, Iterable


CAPTION_PREFIXES = (
    "a ", "an ", "the ", "she ", "he ", "they ", "it ", "this ", "there ",
    "digital ", "illustration ", "drawing ", "image ", "anime ", "cartoon ",
    "painting ", "render ", "3d ", "cgi ", "photo ", "photograph ",
)

FOLLOWING_SENTENCE_PREFIXES = CAPTION_PREFIXES + (
    "her ", "his ", "its ", "we ", "you ", "i ",
)

OPENING_CLAUSE_PREFIXES = (
    "to ", "in ", "on ", "at ", "behind ", "beside ", "near ", "under ",
    "above ", "inside ", "outside ", "across ", "along ", "through ",
    "with ", "without ", "from ",
)

_SENTENCE_END = re.compile(r"[.!?。！？][\"')\]]*$")
_STRONG_TAG_SYNTAX = re.compile(
    r"(?:^\S+:[^\s]+$|\([^\r\n()]{2,}\)$|\\[()]|\b\w+_\w+\b)", re.UNICODE
)
_MODEL_CHATTER = re.compile(
    r"^(?:negative\s+prompt|prompt|steps?|sampler|seed|cfg\s+scale|model|workflow|"
    r"here(?:'s|\s+is)|sure[,!]|i\s+(?:can|see|would)|generated\s+by)\b",
    re.IGNORECASE,
)
_METADATA_SENTENCE_VERB = re.compile(
    r"\b(?:am|is|are|was|were|has|have|had|does|do|did|shows?|depicts?|"
    r"appears?|looks?|wears?|holds?|sits?|stands?|kneels?|lies|lying|"
    r"glistens?|contains?|includes?|features?|streams?|filters?|remains?|"
    r"seems?|conveys?|floats?|glows?|suggests?|visible)\b",
    re.IGNORECASE,
)
_METADATA_PRONOUN_SUBJECTS = (
    "she ", "he ", "they ", "it ", "this ", "there ",
    "her ", "his ", "their ", "its ",
)
_METADATA_URL_FRAGMENT = re.compile(
    r"^(?:(?:https?://|www\.)\S+|(?:com|net|org|io|ai|co|dev|art)/[^\s/]+)$",
    re.IGNORECASE,
)
_METADATA_COUNTED_SUBJECTS = {
    "woman", "women", "man", "men", "girl", "girls", "boy", "boys",
    "person", "people", "character", "characters", "figure", "figures",
    "subject", "subjects", "illustration", "illustrations",
}
_METADATA_COUNT_PREFIXES = {
    "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "several", "multiple", "many",
}


def text_sha256(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


@dataclass
class CaptionImportResult:
    """Typed, source-aware interpretation of a TXT caption.

    TXT is a tag-oriented interchange format in Tag Manager. Non-prose values
    therefore remain typed SDXL Tags even when System Knowledge has not seen
    their names yet. ``ambiguous_fragments`` is reserved for prompt chatter or
    other content whose source does not establish an image-tag assertion.
    ``raw_text`` retains the exact decoded external text.
    """

    sdxl_tags: list[str] = field(default_factory=list)
    plain_language: list[str] = field(default_factory=list)
    ambiguous_fragments: list[str] = field(default_factory=list)
    format: str = "empty"
    raw_text: str = ""
    app_owned_projection: bool = False


def looks_like_clear_prose(value: str) -> bool:
    clean = value.strip()
    if not clean or _MODEL_CHATTER.search(clean):
        return False
    words = clean.split()
    lowered = clean.casefold()
    return (
        len(words) >= 12
        or (len(words) >= 4 and clean[:1].isupper() and lowered.startswith(CAPTION_PREFIXES))
        # Preserve a capitalized prepositional opening as prose while the
        # comma-delimited parser collects the rest of its sentence. This does
        # not classify lower-case tag phrases merely because they share a word.
        or (len(words) >= 4 and clean[:1].isupper() and lowered.startswith(OPENING_CLAUSE_PREFIXES))
        or (len(words) >= 7 and clean[:1].isupper())
        or (len(words) >= 5 and lowered.startswith(CAPTION_PREFIXES))
        or (len(words) >= 4 and bool(_SENTENCE_END.search(clean)))
        # CJK and other scripts may not use spaces. Sentence punctuation is a
        # stronger signal than length or casing, neither of which is portable.
        or (len(words) == 1 and len(clean) >= 8 and bool(_SENTENCE_END.search(clean)))
    )


def _looks_like_following_sentence(value: str) -> bool:
    clean = value.strip()
    words = clean.split()
    if not clean or _MODEL_CHATTER.search(clean):
        return False
    lowered = clean.casefold()
    return (
        (len(words) >= 2 and clean[:1].isupper() and lowered.startswith(FOLLOWING_SENTENCE_PREFIXES))
        or (len(words) >= 3 and bool(_SENTENCE_END.search(clean)))
        or (len(words) == 1 and len(clean) >= 8 and bool(_SENTENCE_END.search(clean)))
    )


def looks_like_clear_tag(value: str, known_tag: Callable[[str], bool] | None = None) -> bool:
    clean = value.strip()
    if not clean or _MODEL_CHATTER.search(clean) or looks_like_clear_prose(clean):
        return False
    if known_tag is not None:
        try:
            if bool(known_tag(clean)):
                return True
        except Exception:
            pass
    return bool(_STRONG_TAG_SYNTAX.search(clean))


def has_broken_metadata_quotes(value: str) -> bool:
    """Return whether a Metadata value is visibly split inside a quotation."""
    clean=str(value or "").strip()
    return (
        not any(character.isalnum() for character in clean)
        or clean.count('"')%2==1
        or clean.count("“")!=clean.count("”")
    )


def looks_like_metadata_prose_fragment(value: str) -> bool:
    """Recognize obvious prose accidentally stored or split into a tag field.

    Metadata tag fields remain tag-oriented, so this deliberately requires
    sentence grammar, a broken quotation, or a clear clause boundary. It does
    not reject a phrase merely because the phrase contains several words.
    """
    clean=str(value or "").strip()
    if not clean:
        return False
    if (
        has_broken_metadata_quotes(clean)
        or _MODEL_CHATTER.search(clean)
        or _METADATA_URL_FRAGMENT.search(clean)
        or looks_like_clear_prose(clean)
    ):
        return True
    words=clean.split(); lowered=clean.casefold()
    if (
        len(words)>=3
        and lowered.startswith(_METADATA_PRONOUN_SUBJECTS)
        and _METADATA_SENTENCE_VERB.search(clean)
    ):
        return True
    if len(words)>=4 and _METADATA_SENTENCE_VERB.search(clean):
        return True
    normalized_words={re.sub(r"^\W+|\W+$","",word.casefold()) for word in words[1:]}
    if (
        len(words)>=4
        and words[0].casefold() in _METADATA_COUNT_PREFIXES
        and normalized_words.intersection(_METADATA_COUNTED_SUBJECTS)
    ):
        return True
    if len(words)>=5 and lowered.startswith(OPENING_CLAUSE_PREFIXES):
        return True
    return lowered.startswith(("the artist's watermark ","the artist’s watermark "))


def metadata_sources_only(sources) -> bool:
    """Return whether every named source is the embedded image Metadata reader."""
    names=[str(source or "").strip().casefold() for source in sources or () if str(source or "").strip()]
    return bool(names) and all(name in {"image metadata","metadata"} for name in names)


def _known_tag_resolver(
    known_tags: Iterable[str] | Callable[[str], bool] | None,
) -> Callable[[str], bool] | None:
    if callable(known_tags):
        return known_tags
    if known_tags is None:
        return None
    normalized = {" ".join(str(value).casefold().replace("_", " ").split()) for value in known_tags}
    return lambda value: " ".join(str(value).casefold().replace("_", " ").split()) in normalized


def _from_projection(text: str, projection_state: dict | None) -> CaptionImportResult | None:
    state = projection_state if isinstance(projection_state, dict) else {}
    tags = state.get("sdxl_tags")
    prose = state.get("plain_language")
    if (
        state.get("writer") not in {"collectigen","tag_manager"}
        or state.get("status") != "verified"
        or state.get("content_sha256") != text_sha256(text)
        or not isinstance(tags, list)
        or not isinstance(prose, list)
    ):
        return None
    return CaptionImportResult(
        [str(value) for value in tags if str(value).strip()],
        [str(value) for value in prose if str(value).strip()],
        [], "app_owned_projection", text, True,
    )


def parse_generated_caption(
    text: str,
    *,
    known_tags: Iterable[str] | Callable[[str], bool] | None = None,
    projection_state: dict | None = None,
) -> CaptionImportResult:
    """Classify external TXT without silently turning ambiguity into tags.

    A matching app-owned projection identity bypasses heuristics and restores
    the exact typed segments recorded when Tag Manager wrote the TXT file.
    External non-prose values remain tags so new vocabulary, aliases, character
    recognition, and automation can treat TXT consistently with Metadata tag
    fields. Clear prose stays separate and prompt/provenance chatter remains
    ambiguous rather than entering tag behavior.
    """

    raw_text = str(text)
    projected = _from_projection(raw_text, projection_state)
    if projected is not None:
        return projected

    parts = [part.strip() for part in raw_text.replace("\r\n", "\n").split(",")]
    parts = [part for part in parts if part]
    if not parts:
        return CaptionImportResult(raw_text=raw_text)

    tags: list[str] = []
    prose: list[str] = []
    ambiguous: list[str] = []
    prose_parts: list[str] = []

    def classify_fragment(part: str, *, following_prose: bool = False) -> None:
        # Newlines are source boundaries, not evidence that a fragment is a tag.
        fragments = [value.strip() for value in re.split(r"\n+", part) if value.strip()]
        for fragment in fragments:
            if looks_like_clear_prose(fragment) or (following_prose and _looks_like_following_sentence(fragment)):
                prose.append(fragment)
            elif _MODEL_CHATTER.search(fragment):
                ambiguous.append(fragment)
            else:
                # A TXT caption is primarily a tag projection. Unknown short,
                # multilingual, punctuationless, or phrase-shaped values are
                # still typed tags; System Knowledge discovery can then ask
                # what they mean instead of losing them as untyped fragments.
                tags.append(fragment)

    for part in parts:
        if not prose_parts:
            if "\n" in part:
                classify_fragment(part, following_prose=bool(prose))
                continue
            if looks_like_clear_prose(part) or (prose and _looks_like_following_sentence(part)):
                prose_parts.append(part)
            else:
                classify_fragment(part, following_prose=bool(prose))
            continue

        # Commas inside clear prose are retained until the sentence ends.
        if _SENTENCE_END.search(prose_parts[-1]):
            prose.append(", ".join(prose_parts).strip())
            prose_parts = []
            if looks_like_clear_prose(part) or _looks_like_following_sentence(part):
                prose_parts.append(part)
            else:
                classify_fragment(part, following_prose=True)
        else:
            prose_parts.append(part)

    if prose_parts:
        prose.append(", ".join(prose_parts).strip())

    if tags and prose:
        detected_format = "mixed_tags_and_prose"
    elif tags:
        detected_format = "tag_list"
    elif prose:
        detected_format = "plain_language"
    else:
        detected_format = "ambiguous"
    if ambiguous and (tags or prose):
        detected_format = "mixed_with_ambiguity"
    return CaptionImportResult(tags, prose, ambiguous, detected_format, raw_text, False)
