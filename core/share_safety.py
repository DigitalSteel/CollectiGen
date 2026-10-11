from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .content_sensitivity import RATING_ORDER, effective_tag_sensitivity
from .image_record import ImageRecord
from .tag_collection import normalize_tag


SHARE_SAFETY_SCHEMA_VERSION = 1
SHARE_SAFETY_REVIEWED_ONLY = "reviewed_only"
SHARE_SAFETY_THROUGH_QUESTIONABLE = "through_questionable"
SHARE_SAFETY_THROUGH_SENSITIVE = "through_sensitive"
SHARE_SAFETY_GENERAL_ONLY = "general_only"

SHARE_SAFETY_POLICY_LABELS = {
    SHARE_SAFETY_REVIEWED_ONLY: "No ordinary rating limit",
    SHARE_SAFETY_THROUGH_QUESTIONABLE: "Allow through Questionable (recommended)",
    SHARE_SAFETY_THROUGH_SENSITIVE: "Allow General and Sensitive",
    SHARE_SAFETY_GENERAL_ONLY: "Allow General only",
}

_POLICY_MAXIMUM_RATING = {
    SHARE_SAFETY_REVIEWED_ONLY: "explicit",
    SHARE_SAFETY_THROUGH_QUESTIONABLE: "questionable",
    SHARE_SAFETY_THROUGH_SENSITIVE: "sensitive",
    SHARE_SAFETY_GENERAL_ONLY: "general",
}

PROHIBITED_MINOR_CONTENT_FLAG = "sexual content involving minors"


@dataclass(frozen=True)
class ProhibitedContentRule:
    """One reviewed operational rule; this is not a legal adjudication."""

    rule_id: str
    label: str
    content_flags: frozenset[str]
    destination_action: str = "block_non_local"


PROHIBITED_CONTENT_RULES = (
    ProhibitedContentRule(
        "minor_sexual_exploitation",
        "Sexual exploitation or prohibited sexual depictions involving minors",
        frozenset({
            PROHIBITED_MINOR_CONTENT_FLAG,
            "minor sexual content",  # schema-v4 compatibility spelling
            "child sexual abuse",
            "child sexual abuse material",
        }),
    ),
)
_PROHIBITED_MINOR_CONTENT_FLAGS = frozenset().union(
    *(rule.content_flags for rule in PROHIBITED_CONTENT_RULES)
)

SAFETY_READY = "ready"
SAFETY_WARNING = "ready_with_warning"
SAFETY_LOCAL_REVIEW = "ready_local_review"
SAFETY_REVIEW = "review_required"
SAFETY_BLOCKED = "blocked"


def prohibited_content_signal(tag_sources, knowledge) -> tuple[str, tuple[str, ...]]:
    """Classify reviewed prohibited tag rules without promoting observations."""
    confirmed: set[str] = set()
    suspected: set[str] = set()
    for tag, raw_sources in dict(tag_sources or {}).items():
        reusable = knowledge.find(tag) if knowledge is not None else None
        if (
            reusable is None
            or reusable.content_sensitivity.classification_status != "reviewed"
        ):
            continue
        flags = {
            normalize_tag(value)
            for value in reusable.content_sensitivity.content_flags
        }
        if not flags.intersection(_PROHIBITED_MINOR_CONTENT_FLAGS):
            continue
        sources = {
            str(value).strip()
            for value in (
                raw_sources
                if isinstance(raw_sources, (list, tuple, set))
                else (raw_sources,)
            )
            if str(value).strip()
        }
        if "Confirmed Image Knowledge" in sources:
            confirmed.add(str(tag))
        else:
            suspected.add(str(tag))
    if confirmed:
        return "confirmed", tuple(sorted(confirmed, key=str.casefold))
    if suspected:
        return "suspected", tuple(sorted(suspected, key=str.casefold))
    return "", ()


def normalize_share_safety_policy(value: object) -> str:
    candidate = str(value or "").strip().casefold()
    return (
        candidate if candidate in _POLICY_MAXIMUM_RATING
        else SHARE_SAFETY_THROUGH_QUESTIONABLE
    )


def share_safety_policy_fingerprint(
    policy_id: str,require_reviewed_image_knowledge: bool = True,
    *,library_move: bool = False,
) -> str:
    payload = json.dumps({
        "schema_version": SHARE_SAFETY_SCHEMA_VERSION,
        "policy_id": normalize_share_safety_policy(policy_id),
        "require_reviewed_image_knowledge":bool(require_reviewed_image_knowledge and not library_move),
        "library_move":library_move,
        "prohibited_flags": sorted(_PROHIBITED_MINOR_CONTENT_FLAGS),
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class DestinationSafetyDecision:
    status: str
    rating: str
    rating_basis: str
    reasons: tuple[str, ...]
    policy_id: str
    policy_fingerprint: str
    image_sha256: str

    @property
    def ready(self) -> bool:
        return self.status in {SAFETY_READY,SAFETY_LOCAL_REVIEW,SAFETY_WARNING}

    @property
    def warning(self) -> bool:
        return self.status == SAFETY_WARNING

    @property
    def blocked(self) -> bool:
        return self.status == SAFETY_BLOCKED

    @property
    def label(self) -> str:
        return {
            SAFETY_READY: "Ready to share",
            SAFETY_WARNING: "Destination policy warning",
            SAFETY_LOCAL_REVIEW: "Ready for Local Only review",
            SAFETY_REVIEW: "Safety review required",
            SAFETY_BLOCKED: "Blocked by destination safety",
        }.get(self.status, "Safety review required")


class DestinationSafetyPreflight:
    """Read-only, hash-bound safety decision for one outward file operation.

    Only confirmed Image Knowledge and reviewed System Knowledge can authorize a
    destination. AI observations and display-only provisional ratings are never
    promoted through this path.
    """

    def __init__(self, knowledge, categories=None) -> None:
        self.knowledge = knowledge
        self.categories = categories

    @staticmethod
    def _sha256(
        path: Path, should_cancel: Callable[[], bool] | None = None,
    ) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                if should_cancel is not None and should_cancel():
                    raise InterruptedError(
                        "Destination safety preview cancelled safely while verifying file identity."
                    )
                digest.update(chunk)
        return digest.hexdigest()

    def _rating_and_flags(self, record: ImageRecord) -> tuple[str, str, set[str]]:
        rating = ""
        basis = ""
        flags: set[str] = set()
        for tag in record.annotation.all_structured_tags():
            normalized = normalize_tag(tag)
            if normalized in RATING_ORDER:
                candidate = normalized
                candidate_basis = "Confirmed Image Knowledge rating"
            else:
                reusable = self.knowledge.find(tag) if self.knowledge is not None else None
                if reusable is None:
                    continue
                sensitivity = reusable.content_sensitivity
                if sensitivity.classification_status == "reviewed":
                    flags.update(
                        normalize_tag(value)
                        for value in sensitivity.content_flags
                        if normalize_tag(value)
                    )
                candidate = effective_tag_sensitivity(
                    reusable, self.categories,
                )[0]
                candidate_basis = "Confirmed Image Knowledge with reviewed System Knowledge"
            if candidate in RATING_ORDER and (
                not rating or RATING_ORDER[candidate] > RATING_ORDER[rating]
            ):
                rating = candidate
                basis = candidate_basis
        return rating, basis, flags

    def _provisional_prohibited_tags(self, record: ImageRecord) -> tuple[str, ...]:
        """Return unconfirmed tag findings that need review, never confirmation."""
        candidates: set[str] = set()
        for evidence in record.annotation.ai_evidence:
            if evidence.kind != "sdxl_tags":
                continue
            for prediction in evidence.typed_tags():
                tag=str(prediction.get("tag") or "")
                decision=str(evidence.decisions.get(normalize_tag(tag)) or "")
                # An accepted model decision is safe to omit here only after
                # the tag actually appears in confirmed Image Knowledge. If a
                # historical or interrupted save retained the decision without
                # the fact, keep the conservative review hold.
                if tag and decision not in {"rejected","dismissed","ignored"}:
                    candidates.add(tag)
        findings=record.annotation.source.get("candidate_findings",[])
        for finding in findings if isinstance(findings,list) else ():
            if isinstance(finding,dict) and finding.get("kind")=="tag":
                value=str(finding.get("value") or "")
                if value:candidates.add(value)
        held=[]
        for tag in sorted(candidates,key=str.casefold):
            reusable=self.knowledge.find(tag) if self.knowledge is not None else None
            if reusable is None or reusable.content_sensitivity.classification_status!="reviewed":
                continue
            flags={normalize_tag(value) for value in reusable.content_sensitivity.content_flags}
            if flags.intersection(_PROHIBITED_MINOR_CONTENT_FLAGS):held.append(tag)
        return tuple(held)

    def evaluate(
        self, image: str | Path, policy_id: str, *,
        should_cancel: Callable[[], bool] | None = None,
        local_review_destination: bool = False,
        require_reviewed_image_knowledge: bool = True,
        library_move: bool = False,
    ) -> DestinationSafetyDecision:
        path = Path(image)
        normalized_policy = normalize_share_safety_policy(policy_id)
        fingerprint = share_safety_policy_fingerprint(
            normalized_policy,require_reviewed_image_knowledge,library_move=library_move,
        )
        image_sha256 = self._sha256(path, should_cancel)
        sidecar = path.with_suffix(".tags.json")
        if not sidecar.is_file():
            return DestinationSafetyDecision(
                SAFETY_LOCAL_REVIEW if local_review_destination else SAFETY_REVIEW,
                "unknown", "No confirmed Image Knowledge",
                (
                    "Confirm Image Knowledge and its content rating before sharing. "
                    "Keep this image family in a Local Only library until review is complete.",
                ),
                normalized_policy, fingerprint, image_sha256,
            )
        try:
            record = ImageRecord(path)
        except Exception as exc:
            return DestinationSafetyDecision(
                SAFETY_LOCAL_REVIEW if local_review_destination else SAFETY_REVIEW,
                "unknown", "Image Knowledge unavailable",
                (
                    f"Image Knowledge could not be read safely: {exc} Keep this image family "
                    "in a Local Only library until review is complete.",
                ),
                normalized_policy, fingerprint, image_sha256,
            )
        if (
            require_reviewed_image_knowledge and not library_move
            and record.annotation.review_status != "reviewed"
        ):
            return DestinationSafetyDecision(
                SAFETY_LOCAL_REVIEW if local_review_destination else SAFETY_REVIEW,
                "unknown", "Image Knowledge needs review",
                (
                    "Mark the current Image Knowledge reviewed before sharing, or change "
                    "this library's saved whole-image review preference. Keep this image "
                    "family in a Local Only library until one of those decisions is complete.",
                ),
                normalized_policy, fingerprint, image_sha256,
            )
        rating, basis, flags = self._rating_and_flags(record)
        if flags.intersection(_PROHIBITED_MINOR_CONTENT_FLAGS):
            if local_review_destination:
                return DestinationSafetyDecision(
                    SAFETY_LOCAL_REVIEW,rating or "unknown",basis or "Reviewed System Knowledge",
                    (
                        "A confirmed prohibited-content signal may be relocated into this Local Only "
                        "library for authorized review. It remains held from shared or cloud destinations.",
                    ),
                    normalized_policy,fingerprint,image_sha256,
                )
            return DestinationSafetyDecision(
                SAFETY_BLOCKED, rating or "unknown", basis or "Reviewed System Knowledge",
                (
                    "Confirmed Image Knowledge includes a reviewed prohibited-content signal involving minors. "
                    "Correct a false classification in the image-data editor; this destination operation cannot override it.",
                ),
                normalized_policy, fingerprint, image_sha256,
            )
        provisional=self._provisional_prohibited_tags(record)
        if provisional:
            if local_review_destination:
                return DestinationSafetyDecision(
                    SAFETY_LOCAL_REVIEW,rating or "unknown",basis or "Unconfirmed safety finding",
                    (
                        "An unconfirmed AI or imported safety finding may be relocated into this "
                        "Local Only library for review. It is not a confirmed image fact.",
                    ),
                    normalized_policy,fingerprint,image_sha256,
                )
            return DestinationSafetyDecision(
                SAFETY_REVIEW, rating or "unknown", basis or "Unconfirmed safety finding",
                (
                    "An AI or imported tag finding may indicate prohibited sexual content involving "
                    "minors. It remains unconfirmed and must be reviewed in the image-data editor. "
                    "Keep this image family in a Local Only library until review is complete.",
                ),
                normalized_policy, fingerprint, image_sha256,
            )
        if not rating:
            return DestinationSafetyDecision(
                SAFETY_LOCAL_REVIEW if local_review_destination else
                SAFETY_WARNING if library_move else SAFETY_REVIEW,
                "unknown", "No confirmed content rating",
                (
                    "The destination rating policy cannot be checked without a confirmed rating. "
                    "You may continue with this move or cancel to correct the rating."
                    if library_move else
                    "Confirm an exact content rating before sharing. Keep this image family in a "
                    "Local Only library until review is complete.",
                ),
                normalized_policy, fingerprint, image_sha256,
            )
        maximum = _POLICY_MAXIMUM_RATING[normalized_policy]
        if RATING_ORDER[rating] > RATING_ORDER[maximum]:
            return DestinationSafetyDecision(
                SAFETY_WARNING if library_move else SAFETY_REVIEW, rating, basis,
                (
                    f"The confirmed {rating.title()} rating exceeds this destination's "
                    f"{maximum.title()} limit. You may continue with this move or cancel "
                    "to change the selection or destination policy."
                    if library_move else
                    f"The confirmed {rating.title()} rating exceeds this destination's "
                    f"{maximum.title()} safety limit. Keep this image family in a Local Only "
                    "library until it is reviewed or the destination policy is deliberately changed.",
                ),
                normalized_policy, fingerprint, image_sha256,
            )
        return DestinationSafetyDecision(
            SAFETY_READY, rating, basis,
            (f"Confirmed {rating.title()} content is within the destination safety limit.",),
            normalized_policy, fingerprint, image_sha256,
        )

    def is_current(
        self, image: str | Path, decision: DestinationSafetyDecision,
        policy_id: str,*,local_review_destination: bool = False,
        require_reviewed_image_knowledge: bool = True,
        library_move: bool = False,
    ) -> bool:
        normalized_policy = normalize_share_safety_policy(policy_id)
        if decision.policy_fingerprint != share_safety_policy_fingerprint(
            normalized_policy,require_reviewed_image_knowledge,library_move=library_move,
        ):
            return False
        try:
            current = self.evaluate(
                image,normalized_policy,
                local_review_destination=local_review_destination,
                require_reviewed_image_knowledge=require_reviewed_image_knowledge,
                library_move=library_move,
            )
        except (OSError, InterruptedError):
            return False
        return current == decision
