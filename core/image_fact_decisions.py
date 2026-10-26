from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Iterable

from .annotation import ImageAnnotation
from .automation_templates import has_automation_placeholder
from .tag_collection import TagCollection, normalize_tag


@dataclass(frozen=True)
class TagDecisionIssue:
    """One existing decision that blocks silently confirming an SDXL Tag."""

    kind: str
    value: str
    message: str


@dataclass(frozen=True)
class FindingDecisionTarget:
    """One typed observation selected for an explicit image-level decision."""

    kind: str
    value: str
    sources: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    source_fingerprint: str = ""


_DESCRIPTION_WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)
_DESCRIPTION_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "he", "her", "his", "in", "is", "it", "of", "on", "she",
    "the", "their", "they", "this", "to", "was", "wears", "with",
})


def description_review_classification(
    value: str, confirmed_values: Iterable[str],
) -> str:
    """Classify only strong lexical overlap; never infer or merge meaning.

    This deliberately conservative signal exists to route likely paraphrases to
    the existing side-by-side comparison. It is not confirmation authority.
    """

    candidate = {
        token.casefold().replace("’", "'")
        for token in _DESCRIPTION_WORD_RE.findall(str(value or ""))
        if token.casefold() not in _DESCRIPTION_STOP_WORDS
    }
    if len(candidate) < 3:
        return "New description"
    for confirmed in confirmed_values:
        current = {
            token.casefold().replace("’", "'")
            for token in _DESCRIPTION_WORD_RE.findall(str(confirmed or ""))
            if token.casefold() not in _DESCRIPTION_STOP_WORDS
        }
        shared = candidate & current
        if len(shared) >= 3 and len(shared) / min(len(candidate), len(current)) >= 0.5:
            return "Alternative wording"
    return "New description"


def _identity(value: str, knowledge=None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    known = knowledge.find(text) if knowledge is not None else None
    if known is not None:
        return f"id:{known.id}"
    key = normalize_tag(text)
    return f"name:{key}" if key else ""


def image_tag_identity(value: str, knowledge=None) -> str:
    """Return the stable identity used by image-level tag decisions.

    Reviewed aliases and renamed preferred labels resolve to the same tag ID.
    Unknown values retain a normalized-name identity so decisions still work
    before the vocabulary has reviewed the tag.
    """

    return _identity(value, knowledge)


def confirmed_absent_tag_identities(
    annotation: ImageAnnotation, knowledge=None,
) -> set[str]:
    return {
        identity
        for value in annotation.confirmed_absent
        if (identity := _identity(value, knowledge))
    }


def per_image_rejected_tag_identities(
    annotation: ImageAnnotation, knowledge=None,
) -> set[str]:
    identities = {
        identity
        for value in annotation.source.get("rejected_tags", [])
        if (identity := _identity(value, knowledge))
    }
    identities.update(
        identity
        for evidence in annotation.ai_evidence
        if evidence.kind == "sdxl_tags"
        for value, decision in evidence.decisions.items()
        if decision == "rejected"
        if (identity := _identity(value, knowledge))
    )
    return identities


def unavailable_confirmed_tag_identities(
    annotation: ImageAnnotation, knowledge=None,
) -> set[str]:
    """Return image-level negative decisions that suppress ordinary ingress."""

    return (
        confirmed_absent_tag_identities(annotation, knowledge)
        | per_image_rejected_tag_identities(annotation, knowledge)
    )


def tag_addition_decision_issues(
    annotation: ImageAnnotation,
    values: Iterable[str],
    *,
    knowledge=None,
    automation_engine=None,
) -> tuple[TagDecisionIssue, ...]:
    """Explain why an ordinary add path must hold a tag for explicit review."""

    absent = confirmed_absent_tag_identities(annotation, knowledge)
    rejected = per_image_rejected_tag_identities(annotation, knowledge)
    issues = []
    seen = set()
    for raw_value in TagCollection(values):
        value = str(raw_value).strip()
        if has_automation_placeholder(value):
            issues.append(TagDecisionIssue(
                "unresolved_automation_template",
                value,
                f'“{value}” contains an unresolved Automation template and cannot be confirmed as an SDXL Tag.',
            ))
            continue
        identity = _identity(value, knowledge)
        if not identity or identity in seen:
            continue
        seen.add(identity)
        if identity in absent:
            issues.append(TagDecisionIssue(
                "confirmed_absent",
                value,
                f'“{value}” is explicitly confirmed absent from this image. '
                "Restore that reviewed decision before adding the tag.",
            ))
        if identity in rejected:
            issues.append(TagDecisionIssue(
                "per_image_rejection",
                value,
                f'“{value}” is rejected on this image. Restore that per-image '
                "decision before adding the tag.",
            ))
        if (
            automation_engine is not None
            and automation_engine.is_evidence_rejected(value)
        ):
            issues.append(TagDecisionIssue(
                "global_evidence_rejection",
                value,
                f'An active Automation rule rejects “{value}” as incoming '
                "evidence. Review that rule before confirming the tag.",
            ))
    return tuple(issues)


def accept_confirmed_tag_decisions(
    annotation: ImageAnnotation,
    values: Iterable[str],
    *,
    knowledge=None,
) -> bool:
    """Make an explicit confirmed-present decision coherent without losing evidence.

    Export exclusions remain untouched because they suppress a valid fact only
    from selected training projections. Raw candidate and AI observations also
    remain intact; only their reversible per-image decision changes.
    """

    accepted = {
        identity
        for value in values
        if (identity := _identity(value, knowledge))
    }
    if not accepted:
        return False
    changed = False

    absent = [
        value
        for value in annotation.confirmed_absent
        if _identity(value, knowledge) not in accepted
    ]
    if absent != annotation.confirmed_absent:
        annotation.confirmed_absent = TagCollection(absent).to_list()
        changed = True

    rejected = annotation.source.get("rejected_tags", [])
    retained_rejected = [
        value
        for value in rejected
        if _identity(value, knowledge) not in accepted
    ]
    if retained_rejected != rejected:
        annotation.source["rejected_tags"] = TagCollection(
            retained_rejected
        ).to_list()
        changed = True

    for evidence in annotation.ai_evidence:
        if evidence.kind != "sdxl_tags":
            continue
        observed_identities = {
            _identity(str(item.get("tag") or ""), knowledge)
            for item in evidence.typed_observed_tags()
        }
        for decision_key in tuple(evidence.decisions):
            if _identity(decision_key, knowledge) not in accepted:
                continue
            if evidence.decisions.get(decision_key) != "accepted":
                evidence.decisions[decision_key] = "accepted"
                changed = True
        for item in evidence.typed_tags():
            value = str(item.get("tag") or "")
            identity = _identity(value, knowledge)
            key = normalize_tag(value)
            if (
                identity in accepted
                and identity in observed_identities
                and key
                and evidence.decisions.get(key) != "accepted"
            ):
                evidence.decisions[key] = "accepted"
                changed = True

    dismissed = annotation.source.get("dismissed_findings", [])
    dismissed_values = dismissed if isinstance(dismissed, list) else []
    retained_dismissed = [
        item
        for item in dismissed_values
        if not (
            isinstance(item, dict)
            and item.get("kind") == "tag"
            and _identity(str(item.get("value") or ""), knowledge) in accepted
        )
    ]
    if retained_dismissed != dismissed_values:
        if retained_dismissed:
            annotation.source["dismissed_findings"] = retained_dismissed
        else:
            annotation.source.pop("dismissed_findings", None)
        changed = True
    return changed


class ImageFindingDecisionService:
    """One typed decision boundary for source and AI image findings.

    The persisted ``rejected`` and ``dismissed`` values predate this service and
    remain compatibility wire values.  In the interface they mean **Not on This
    Image** and **Decide Later** respectively.  Neither outcome removes raw
    source/AI observations or writes TXT/Metadata projections.
    """

    DEFERRED_VALUES = frozenset({"dismissed", "deferred"})
    USED_AS_EDITED = "used_as_edited"

    def __init__(self, annotation: ImageAnnotation, *, knowledge=None) -> None:
        self.annotation = annotation
        self.knowledge = knowledge

    @staticmethod
    def _kind(kind: str) -> str:
        value = str(kind or "").strip().casefold().replace("-", "_")
        if value in {"tag", "tags", "sdxl_tag", "sdxl_tags"}:
            return "tag"
        if value in {
            "description", "descriptions", "plain_language",
            "plain_language_description", "plain_language_descriptions",
        }:
            return "description"
        raise ValueError(f"Unsupported image finding kind: {kind!r}")

    @staticmethod
    def _description_sentences(value: str) -> list[str]:
        # Imported lazily because ai_interrogation also consumes this decision
        # service while its model/runtime definitions are loading.
        from .ai_interrogation import split_plain_language

        return split_plain_language(str(value or ""))

    @staticmethod
    def _description_key(value: str) -> str:
        from .settings import normalize_plain_language_exclusion

        return normalize_plain_language_exclusion(str(value or ""))

    @staticmethod
    def _sources(values: Iterable[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            clean
            for value in values
            if (clean := str(value or "").strip())
        ))

    def _tag_identity(self, value: str) -> str:
        return _identity(value, self.knowledge)

    @staticmethod
    def _canonical_fingerprint(value: object) -> str:
        if value in (None, "", {}, []):
            return ""
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def source_fingerprint(self, source: str) -> str:
        """Return the current durable identity for one attributed source."""

        source_name = str(source or "Unknown").strip() or "Unknown"
        source_key = source_name.casefold()
        fingerprints = self.annotation.source.get("source_fingerprints", {})
        fingerprints = fingerprints if isinstance(fingerprints, dict) else {}
        if source_key == "txt caption":
            value = fingerprints.get("txt", {})
            if isinstance(value, dict) and value.get("sha256"):
                return f"txt:{value['sha256']}"
        if source_key == "image metadata":
            value = fingerprints.get("metadata", {})
            value = value if isinstance(value, dict) else {}
            image = value.get("input", {})
            image = image if isinstance(image, dict) else {}
            if image.get("sha256"):
                return (
                    f"metadata:{value.get('reader_version', '')}:"
                    f"{image['sha256']}"
                )
        evidence = self.annotation.source.get("evidence", [])
        for item in evidence if isinstance(evidence, list) else ():
            if (
                isinstance(item, dict)
                and str(item.get("name") or "").strip().casefold() == source_key
            ):
                return f"source:{self._canonical_fingerprint(item)}"
        return ""

    def used_as_edited_findings(self) -> list[dict]:
        values = self.annotation.source.get("finding_decisions", [])
        return [
            dict(item)
            for item in values if isinstance(item, dict)
            and item.get("decision") == self.USED_AS_EDITED
            and item.get("kind") == "description"
            and self._description_key(str(item.get("value") or ""))
        ] if isinstance(values, list) else []

    def source_was_used_as_edited(
        self, kind: str, value: str, source: str, *, evidence_id: str = "",
        source_fingerprint: str = "",
    ) -> bool:
        if self._kind(kind) != "description":
            return False
        value_key = self._description_key(value)
        source_key = str(source or "Unknown").strip().casefold() or "unknown"
        current_fingerprint = (
            str(source_fingerprint or "").strip()
            or self.source_fingerprint(source)
        )
        evidence_id = str(evidence_id or "").strip()
        for item in self.used_as_edited_findings():
            if self._description_key(item.get("value", "")) != value_key:
                continue
            if str(item.get("source") or "Unknown").strip().casefold() != source_key:
                continue
            stored_evidence = str(item.get("evidence_id") or "").strip()
            stored_fingerprint = str(item.get("source_fingerprint") or "").strip()
            if evidence_id or stored_evidence:
                if evidence_id and stored_evidence == evidence_id:
                    return True
                continue
            if stored_fingerprint == current_fingerprint:
                return True
        return False

    def mark_used_as_edited(
        self, targets: Iterable[FindingDecisionTarget],
    ) -> bool:
        """Resolve exact selected description attributions without losing evidence."""

        current = self.annotation.source.get("finding_decisions", [])
        retained = [
            dict(item) for item in current if isinstance(item, dict)
        ] if isinstance(current, list) else []
        seen = {
            (
                str(item.get("decision") or ""),
                str(item.get("kind") or ""),
                self._description_key(str(item.get("value") or "")),
                str(item.get("source") or "Unknown").strip().casefold(),
                str(item.get("evidence_id") or ""),
                str(item.get("source_fingerprint") or ""),
            )
            for item in retained
        }
        changed = False
        for target in targets:
            if self._kind(target.kind) != "description":
                continue
            sources = self._sources(target.sources) or ("Unknown",)
            evidence_ids = self._sources(target.evidence_ids)
            attributions = (
                [(source, evidence_id) for source in sources for evidence_id in evidence_ids]
                if evidence_ids else [(source, "") for source in sources]
            )
            for sentence in self._description_sentences(target.value):
                if not self._description_key(sentence):
                    continue
                for source, evidence_id in attributions:
                    fingerprint = (
                        str(target.source_fingerprint or "").strip()
                        or ("" if evidence_id else self.source_fingerprint(source))
                    )
                    key = (
                        self.USED_AS_EDITED, "description",
                        self._description_key(sentence), source.casefold(),
                        evidence_id, fingerprint,
                    )
                    if key in seen:
                        continue
                    retained.append({
                        "kind": "description",
                        "value": sentence,
                        "source": source,
                        "evidence_id": evidence_id,
                        "source_fingerprint": fingerprint,
                        "decision": self.USED_AS_EDITED,
                    })
                    seen.add(key)
                    changed = True
        if changed:
            self.annotation.source["finding_decisions"] = retained
        return changed

    def ignored_tag_identities(self) -> set[str]:
        return per_image_rejected_tag_identities(
            self.annotation, self.knowledge,
        )

    def ignored_description_keys(self) -> set[str]:
        keys = {
            key
            for raw in self.annotation.source.get("rejected_descriptions", [])
            for value in self._description_sentences(raw)
            if (key := self._description_key(value))
        }
        keys.update(
            key
            for evidence in self.annotation.ai_evidence
            if evidence.typed_plain_language()
            and evidence.decisions.get("plain_language") == "rejected"
            for value in self._description_sentences(
                evidence.typed_plain_language()
            )
            if (key := self._description_key(value))
        )
        return keys

    def deferred_findings(self) -> list[dict]:
        values = self.annotation.source.get("dismissed_findings", [])
        return [
            dict(item)
            for item in values
            if isinstance(item, dict)
            and item.get("kind") in {"tag", "description"}
            and str(item.get("value") or "").strip()
        ] if isinstance(values, list) else []

    def deferred_tag_identities(self) -> set[str]:
        identities = {
            identity
            for item in self.deferred_findings()
            if item["kind"] == "tag"
            if (identity := self._tag_identity(item["value"]))
        }
        identities.update(
            identity
            for evidence in self.annotation.ai_evidence
            if evidence.kind == "sdxl_tags"
            for value, decision in evidence.decisions.items()
            if decision in self.DEFERRED_VALUES
            if (identity := self._tag_identity(value))
        )
        return identities

    def deferred_description_keys(self) -> set[str]:
        keys = {
            key
            for item in self.deferred_findings()
            if item["kind"] == "description"
            for value in self._description_sentences(item["value"])
            if (key := self._description_key(value))
        }
        keys.update(
            key
            for evidence in self.annotation.ai_evidence
            if evidence.typed_plain_language()
            and evidence.decisions.get("plain_language") in self.DEFERRED_VALUES
            for value in self._description_sentences(
                evidence.typed_plain_language()
            )
            if (key := self._description_key(value))
        )
        return keys

    def source_is_deferred(
        self, kind: str, value: str, source: str, *, evidence_id: str = "",
    ) -> bool:
        kind = self._kind(kind)
        source_key = str(source or "Unknown").strip().casefold() or "unknown"
        value_key = (
            self._tag_identity(value) if kind == "tag"
            else self._description_key(value)
        )
        for item in self.deferred_findings():
            if item["kind"] != kind:
                continue
            item_keys = (
                {self._tag_identity(item["value"])}
                if kind == "tag" else {
                    self._description_key(sentence)
                    for sentence in self._description_sentences(item["value"])
                    if self._description_key(sentence)
                }
            )
            sources = {
                str(raw or "").strip().casefold()
                for raw in item.get("sources", [])
                if str(raw or "").strip()
            } or {"unknown"}
            evidence_ids = {
                str(raw or "").strip()
                for raw in item.get("evidence_ids", [])
                if str(raw or "").strip()
            }
            if (
                value_key in item_keys and source_key in sources
                and (
                    not evidence_id
                    or not evidence_ids
                    or evidence_id in evidence_ids
                )
            ):
                return True
        for evidence in self.annotation.ai_evidence:
            if evidence_id and evidence.id != evidence_id:
                continue
            model_key = str(
                evidence.model_id or "Unknown model"
            ).strip().casefold()
            if model_key != source_key:
                continue
            if kind == "tag":
                for decision_key, decision in evidence.decisions.items():
                    if (
                        decision in self.DEFERRED_VALUES
                        and self._tag_identity(decision_key) == value_key
                    ):
                        return True
            elif (
                evidence.decisions.get("plain_language")
                in self.DEFERRED_VALUES
                and any(
                    self._description_key(sentence) == value_key
                    for sentence in self._description_sentences(
                        evidence.typed_plain_language()
                    )
                )
            ):
                return True
        return False

    def accept_confirmed_tags(self, values: Iterable[str]) -> bool:
        return accept_confirmed_tag_decisions(
            self.annotation, values, knowledge=self.knowledge,
        )

    def accept_confirmed_descriptions(self, values: Iterable[str]) -> bool:
        accepted = {
            key
            for raw in values
            for value in self._description_sentences(raw)
            if (key := self._description_key(value))
        }
        if not accepted:
            return False
        changed = False
        rejected = {
            self._description_key(value): value
            for raw in self.annotation.source.get("rejected_descriptions", [])
            for value in self._description_sentences(raw)
            if self._description_key(value)
        }
        for key in accepted:
            if rejected.pop(key, None) is not None:
                changed = True

        deferred = []
        for item in self.deferred_findings():
            if item["kind"] != "description":
                deferred.append(item)
                continue
            sentences = self._description_sentences(item["value"])
            remaining = [
                value for value in sentences
                if self._description_key(value) not in accepted
            ]
            if len(remaining) != len(sentences):
                changed = True
            for value in remaining:
                updated = dict(item)
                updated["value"] = value
                deferred.append(updated)

        for evidence in self.annotation.ai_evidence:
            sentences = self._description_sentences(
                evidence.typed_plain_language()
            )
            keys = {
                self._description_key(value)
                for value in sentences if self._description_key(value)
            }
            if not keys or not keys & accepted:
                continue
            previous = evidence.decisions.get("plain_language")
            if previous == "rejected":
                for value in sentences:
                    key = self._description_key(value)
                    if key and key not in accepted:
                        rejected.setdefault(key, value)
            elif previous in self.DEFERRED_VALUES:
                remaining = [
                    value for value in sentences
                    if self._description_key(value) not in accepted
                ]
                if remaining:
                    deferred.extend({
                        "kind": "description", "value": value,
                        "sources": [evidence.model_id or "Unknown model"],
                        "evidence_ids": [evidence.id],
                    } for value in remaining)
            if keys <= accepted:
                if previous != "accepted":
                    evidence.decisions["plain_language"] = "accepted"
                    changed = True
            elif previous in {"rejected", *self.DEFERRED_VALUES}:
                evidence.decisions.pop("plain_language", None)
                changed = True

        changed = self._write_description_rejections(rejected) or changed
        changed = self._write_deferred(deferred) or changed
        return changed

    def ignore_for_image(
        self, *, tags: Iterable[str] = (), descriptions: Iterable[str] = (),
    ) -> bool:
        tag_values = TagCollection(tags).to_list()
        tag_ids = {
            identity for value in tag_values
            if (identity := self._tag_identity(value))
        }
        description_values = [
            value
            for raw in descriptions
            for value in self._description_sentences(raw)
            if self._description_key(value)
        ]
        description_keys = {
            self._description_key(value) for value in description_values
        }
        if not tag_ids and not description_keys:
            return False
        changed = False
        rejected_tags = TagCollection(
            self.annotation.source.get("rejected_tags", [])
        )
        existing_tag_ids = {
            self._tag_identity(value) for value in rejected_tags
        }
        for value in tag_values:
            if self._tag_identity(value) not in existing_tag_ids:
                rejected_tags.add(value)
                existing_tag_ids.add(self._tag_identity(value))
                changed = True
        normalized_tags = rejected_tags.to_list()
        if normalized_tags != self.annotation.source.get("rejected_tags", []):
            self.annotation.source["rejected_tags"] = normalized_tags
            changed = True

        rejected_descriptions = {
            self._description_key(value): value
            for raw in self.annotation.source.get("rejected_descriptions", [])
            for value in self._description_sentences(raw)
            if self._description_key(value)
        }
        for value in description_values:
            key = self._description_key(value)
            if key not in rejected_descriptions:
                rejected_descriptions[key] = value
                changed = True

        for evidence in self.annotation.ai_evidence:
            if evidence.kind == "sdxl_tags":
                for prediction in evidence.typed_tags():
                    value = str(prediction.get("tag") or "")
                    key = normalize_tag(value)
                    if (
                        self._tag_identity(value) in tag_ids and key
                        and evidence.decisions.get(key) != "rejected"
                    ):
                        evidence.decisions[key] = "rejected"
                        changed = True
            else:
                sentences = self._description_sentences(
                    evidence.typed_plain_language()
                )
                keys = {
                    self._description_key(value)
                    for value in sentences if self._description_key(value)
                }
                if keys and keys <= description_keys:
                    if evidence.decisions.get("plain_language") != "rejected":
                        evidence.decisions["plain_language"] = "rejected"
                        changed = True
                elif keys & description_keys and evidence.decisions.get(
                    "plain_language"
                ) in {"accepted", *self.DEFERRED_VALUES}:
                    evidence.decisions.pop("plain_language", None)
                    changed = True

        changed = self._write_description_rejections(
            rejected_descriptions,
        ) or changed
        changed = self.restore_deferred(
            tags=tag_values, descriptions=description_values,
        ) or changed
        return changed

    def restore_image_ignores(
        self, *, tags: Iterable[str] = (), descriptions: Iterable[str] = (),
    ) -> bool:
        tag_ids = {
            identity for value in tags
            if (identity := self._tag_identity(value))
        }
        description_keys = {
            key
            for raw in descriptions
            for value in self._description_sentences(raw)
            if (key := self._description_key(value))
        }
        changed = False
        rejected_tags = self.annotation.source.get("rejected_tags", [])
        retained_tags = [
            value for value in rejected_tags
            if self._tag_identity(value) not in tag_ids
        ]
        if retained_tags != rejected_tags:
            self.annotation.source["rejected_tags"] = retained_tags
            changed = True

        rejected_descriptions = {
            self._description_key(value): value
            for raw in self.annotation.source.get("rejected_descriptions", [])
            for value in self._description_sentences(raw)
            if self._description_key(value) not in description_keys
        }
        changed = self._write_description_rejections(
            rejected_descriptions,
        ) or changed

        for evidence in self.annotation.ai_evidence:
            if evidence.kind == "sdxl_tags":
                for key in tuple(evidence.decisions):
                    if (
                        evidence.decisions.get(key) == "rejected"
                        and self._tag_identity(key) in tag_ids
                    ):
                        evidence.decisions.pop(key, None)
                        changed = True
            elif evidence.decisions.get("plain_language") == "rejected":
                sentences = self._description_sentences(
                    evidence.typed_plain_language()
                )
                keys = {
                    self._description_key(value)
                    for value in sentences if self._description_key(value)
                }
                if keys & description_keys:
                    evidence.decisions.pop("plain_language", None)
                    changed = True
                    for value in sentences:
                        key = self._description_key(value)
                        if key and key not in description_keys:
                            rejected_descriptions.setdefault(key, value)
        changed = self._write_description_rejections(
            rejected_descriptions,
        ) or changed
        return changed

    def defer(self, targets: Iterable[FindingDecisionTarget]) -> bool:
        targets = tuple(
            FindingDecisionTarget(
                self._kind(item.kind), str(item.value or "").strip(),
                self._sources(item.sources), self._sources(item.evidence_ids),
            )
            for item in targets
            if str(item.value or "").strip()
        )
        if not targets:
            return False
        ai_sources = {
            str(evidence.model_id or "Unknown model").strip().casefold()
            for evidence in self.annotation.ai_evidence
        }
        deferred = self.deferred_findings()
        by_key = {
            (
                item["kind"],
                self._tag_identity(item["value"])
                if item["kind"] == "tag"
                else self._description_key(item["value"]),
            ): dict(item)
            for item in deferred
        }
        changed = False
        target_sources: dict[tuple[str, str], set[str]] = {}
        target_evidence_ids: dict[tuple[str, str, str], set[str]] = {}
        for item in targets:
            key = (
                item.kind,
                self._tag_identity(item.value)
                if item.kind == "tag" else self._description_key(item.value),
            )
            sources = item.sources or ("Unknown",)
            target_sources.setdefault(key, set()).update(
                source.casefold() for source in sources
            )
            for source in sources:
                target_evidence_ids.setdefault(
                    (item.kind, key[1], source.casefold()), set(),
                ).update(item.evidence_ids)
            non_ai_sources = [
                source for source in sources
                if source.casefold() not in ai_sources
            ]
            if not non_ai_sources:
                continue
            existing = by_key.get(key, {
                "kind": item.kind, "value": item.value, "sources": [],
            })
            merged_sources = list(dict.fromkeys([
                *existing.get("sources", []), *non_ai_sources,
            ]))
            if merged_sources != existing.get("sources", []):
                changed = True
            existing["sources"] = merged_sources
            by_key[key] = existing

        for evidence in self.annotation.ai_evidence:
            source = str(
                evidence.model_id or "Unknown model"
            ).strip().casefold()
            if evidence.kind == "sdxl_tags":
                for prediction in evidence.typed_tags():
                    value = str(prediction.get("tag") or "")
                    key = ("tag", self._tag_identity(value))
                    if source not in target_sources.get(key, set()):
                        continue
                    evidence_ids = target_evidence_ids.get(
                        ("tag", key[1], source), set(),
                    )
                    if evidence_ids and evidence.id not in evidence_ids:
                        continue
                    decision_key = normalize_tag(value)
                    if evidence.decisions.get(decision_key) != "dismissed":
                        evidence.decisions[decision_key] = "dismissed"
                        changed = True
            else:
                sentences = self._description_sentences(
                    evidence.typed_plain_language()
                )
                keys = {
                    self._description_key(value)
                    for value in sentences if self._description_key(value)
                }
                selected = {
                    key for kind, key in target_sources
                    if kind == "description"
                    and source in target_sources[(kind, key)]
                    and (
                        not target_evidence_ids.get(
                            (kind, key, source), set(),
                        )
                        or evidence.id in target_evidence_ids[
                            (kind, key, source)
                        ]
                    )
                }
                if keys and keys <= selected and evidence.decisions.get(
                    "plain_language"
                ) != "dismissed":
                    evidence.decisions["plain_language"] = "dismissed"
                    changed = True
                elif keys & selected:
                    for value in sentences:
                        value_key = self._description_key(value)
                        if value_key not in selected:
                            continue
                        evidence_ids = target_evidence_ids.get(
                            ("description", value_key, source), set(),
                        )
                        if evidence_ids and evidence.id not in evidence_ids:
                            continue
                        key = ("description", value_key)
                        existing = by_key.get(key, {
                            "kind": "description", "value": value,
                            "sources": [], "evidence_ids": [],
                        })
                        existing["sources"] = list(dict.fromkeys([
                            *existing.get("sources", []),
                            evidence.model_id or "Unknown model",
                        ]))
                        existing["evidence_ids"] = list(dict.fromkeys([
                            *existing.get("evidence_ids", []), evidence.id,
                        ]))
                        by_key[key] = existing
                        changed = True

        changed = self._write_deferred(list(by_key.values())) or changed
        return changed

    def restore_deferred(
        self, *, tags: Iterable[str] = (), descriptions: Iterable[str] = (),
    ) -> bool:
        tag_ids = {
            identity for value in tags
            if (identity := self._tag_identity(value))
        }
        description_keys = {
            key
            for raw in descriptions
            for value in self._description_sentences(raw)
            if (key := self._description_key(value))
        }
        changed = False
        retained = []
        for item in self.deferred_findings():
            if item["kind"] == "tag":
                if self._tag_identity(item["value"]) in tag_ids:
                    changed = True
                    continue
                retained.append(item)
                continue
            sentences = self._description_sentences(item["value"])
            remaining = [
                value for value in sentences
                if self._description_key(value) not in description_keys
            ]
            if len(remaining) != len(sentences):
                changed = True
            for value in remaining:
                updated = dict(item)
                updated["value"] = value
                retained.append(updated)
        changed = self._write_deferred(retained) or changed
        for evidence in self.annotation.ai_evidence:
            if evidence.kind == "sdxl_tags":
                for key in tuple(evidence.decisions):
                    if (
                        evidence.decisions.get(key) in self.DEFERRED_VALUES
                        and self._tag_identity(key) in tag_ids
                    ):
                        evidence.decisions.pop(key, None)
                        changed = True
            elif evidence.decisions.get(
                "plain_language"
            ) in self.DEFERRED_VALUES and any(
                self._description_key(value) in description_keys
                for value in self._description_sentences(
                    evidence.typed_plain_language()
                )
            ):
                evidence.decisions.pop("plain_language", None)
                changed = True
        return changed

    def _write_description_rejections(self, values: dict[str, str]) -> bool:
        current = self.annotation.source.get("rejected_descriptions", [])
        updated = sorted(values.values(), key=str.casefold)
        if updated == current:
            return False
        if updated:
            self.annotation.source["rejected_descriptions"] = updated
        else:
            self.annotation.source.pop("rejected_descriptions", None)
        return True

    def _write_deferred(self, values: list[dict]) -> bool:
        current = self.annotation.source.get("dismissed_findings", [])
        updated = sorted(
            values,
            key=lambda item: (
                str(item.get("kind") or ""),
                str(item.get("value") or "").casefold(),
            ),
        )
        if updated == current:
            return False
        if updated:
            self.annotation.source["dismissed_findings"] = updated
        else:
            self.annotation.source.pop("dismissed_findings", None)
        return True
