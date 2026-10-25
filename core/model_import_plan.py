"""Typed, receipt-bound orchestration for an explicitly approved Models import.

Fixed production ports own all file/API operations and recovery proofs. This
module never follows model paths, downloads URLs or deserializes callables.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
import re
from typing import Mapping, Protocol
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .model_import_receipts import ImportReceiptStore, digest, encoded
from .model_resource_registry import ModelResourceError, RESOURCE_ROLES

KINDS = ("registration", "move", "acquisition", "companions", "manual_cache", "paths")
MAX_STEPS = 128
MAX_RESOURCES = 64
MAX_REVISIONS = 16
MAX_REGISTRATION_FILES = 20_000
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_RESOURCE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SECRET_KEYS = {"password", "api_key", "apikey", "access_token", "refresh_token", "authorization", "cookie", "client_secret", "credentials"}
_OUTCOMES = {"completed", "recoverable", "held", "uncertain", "cancelled", "not_started"}


@dataclass(frozen=True)
class ImportStep:
    step_id: str
    kind: str
    depends_on: tuple[str, ...]
    choices: dict
    expected_effects: dict


@dataclass(frozen=True)
class ImportPlan:
    source_binding: dict[str, str]
    registry_token: str
    steps: tuple[ImportStep, ...]


@dataclass(frozen=True)
class OwnerReceipt:
    owner: str
    operation_id: str
    locator: str
    fingerprint: str
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StepResult:
    step_id: str
    status: str
    effects: dict = field(default_factory=dict)
    registry_token: str = ""
    receipt: OwnerReceipt | None = None
    message: str = ""


@dataclass(frozen=True)
class ActionContext:
    source_binding: dict[str, str]
    registry_token: str
    completed: tuple[StepResult, ...] = ()
    prior_receipt: OwnerReceipt | None = None


@dataclass(frozen=True)
class ActionPreview:
    status: str
    effects: dict
    observed_registry_token: str
    observed_source_binding: dict[str, str]
    handle: object = None
    message: str = ""


@dataclass(frozen=True)
class ActionOutcome:
    status: str
    effects: dict
    registry_token: str
    receipt: OwnerReceipt | None = None
    message: str = ""


class ActionPort(Protocol):
    def preview(self, step: ImportStep, context: ActionContext) -> ActionPreview: ...
    def execute(self, step, preview, context, *, on_started, should_cancel=None, progress=None) -> ActionOutcome: ...
    def reconcile(self, step, receipt, context, *, should_cancel=None, progress=None) -> ActionOutcome: ...


@dataclass(frozen=True)
class ImportReview:
    review_id: str
    plan: ImportPlan
    steps: tuple[StepResult, ...]
    fingerprint: str


@dataclass(frozen=True)
class RevisionReview:
    review_id: str
    approval_id: str
    prior_revision: str
    parent_head: str
    prefix_length: int
    registry_token: str
    plan: ImportPlan
    steps: tuple[StepResult, ...]
    fingerprint: str


@dataclass(frozen=True)
class ImportRun:
    status: str
    steps: tuple[StepResult, ...]
    registry_token: str
    message: str


def _text(value, *, limit=4096):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ModelResourceError("Invalid import text or identifier.")
    return value


def _hash(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ModelResourceError("Import evidence needs an exact SHA-256 fingerprint.")
    return value


def _path_text(value):
    path = Path(_text(value))
    if not path.is_absolute() or ".." in path.parts or str(path).startswith(("\\\\?\\", "\\\\.\\")):
        raise ModelResourceError("Import locations must be explicit absolute paths without traversal.")
    return value


def _metadata(value, *, depth=0):
    if depth > 12:
        raise ModelResourceError("Import metadata nesting exceeds its supported bound.")
    if value is None or type(value) is bool or type(value) is int:
        return
    if isinstance(value, str):
        if len(value) > 64_000 or "\x00" in value:
            raise ModelResourceError("Import metadata text exceeds its supported bound.")
        if value.startswith(("https://", "http://")):
            parsed = urlsplit(value)
            if parsed.username or parsed.password or any(key.casefold() in _SECRET_KEYS | {"token", "key", "signature", "sig"} for key in parse_qs(parsed.query)):
                raise ModelResourceError("Credentials and signed URLs cannot be retained in an import plan.")
        return
    if isinstance(value, list):
        if len(value) > MAX_REGISTRATION_FILES:
            raise ModelResourceError("Import metadata list exceeds its supported bound.")
        for item in value: _metadata(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 128:
            raise ModelResourceError("Import metadata object exceeds its supported bound.")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128 or key.casefold() in _SECRET_KEYS:
                raise ModelResourceError("Unsupported or secret import metadata field.")
            if key in {"resource_id", "main_resource_id"} and (not isinstance(item, str) or not _RESOURCE.fullmatch(item)):
                raise ModelResourceError("Import resources need exact standalone model hashes.")
            _metadata(item, depth=depth + 1)
        return
    raise ModelResourceError("Import metadata must be bounded JSON, never executable objects.")


def _choices(step):
    value = step.choices
    fields = {"registration": {"review"}, "move": {"selection", "destination"},
        "acquisition": {"plan", "role", "family"}, "companions": {"request", "choices"},
        "manual_cache": {"requirements", "approved_cleanup"},
        "paths": {"mode", "inventory", "values", "model_root", "prepared_steps"}}
    if not isinstance(value, dict) or set(value) != fields[step.kind]:
        raise ModelResourceError(f"Unsupported {step.kind} choice envelope.")
    _metadata(value); _metadata(step.expected_effects)
    if not isinstance(step.expected_effects, dict) or not step.expected_effects:
        raise ModelResourceError("Every import step needs explicit expected effects.")
    if step.kind == "registration":
        from .model_scan_review import _decode
        draft = _decode(value["review"])
        if not 1 <= len(draft.candidates) <= MAX_REGISTRATION_FILES:
            raise ModelResourceError("Register between one and 20,000 explicitly reviewed files per step.")
    elif step.kind == "move":
        selection = value["selection"]
        if not isinstance(selection, dict) or set(selection) != {"resource_id", "source", "role", "family", "media_type"}:
            raise ModelResourceError("A move needs one exact model, source and declaration.")
        _path_text(selection["source"]); _path_text(value["destination"])
        if selection["role"] not in RESOURCE_ROLES or selection["media_type"] not in {"Images", "Video"}:
            raise ModelResourceError("Unsupported move declaration.")
        _text(selection["family"], limit=160)
    elif step.kind == "acquisition":
        plan = value["plan"]
        if not isinstance(plan, dict) or set(plan) != {"schema_version", "provider", "version", "url", "sha256", "size", "destination", "terms"}:
            raise ModelResourceError("Acquisition needs an exact provider receipt.")
        if type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
            raise ModelResourceError("Unsupported acquisition receipt schema.")
        _hash(plan["sha256"]); _path_text(plan["destination"]); _text(plan["terms"], limit=64_000)
        if type(plan["size"]) is not int or not 0 < plan["size"] <= 1024**4 or value["role"] not in RESOURCE_ROLES:
            raise ModelResourceError("Acquisition needs exact size and model role.")
        _text(value["family"], limit=160)
        from .model_workspace import validate_download_url
        validate_download_url(plan["url"])
        parsed = urlsplit(plan["url"])
        version = _text(plan["version"], limit=128)
        if parsed.fragment:
            raise ModelResourceError("Acquisition URLs cannot retain fragments.")
        if plan["provider"] == "civitai":
            if (parsed.hostname != "civitai.com" or not version.isdigit()
                    or parsed.path.rstrip("/") != f"/api/download/models/{version}"
                    or set(parse_qs(parsed.query)) - {"type", "format", "size"}):
                raise ModelResourceError("Acquisition URL differs from the exact reviewed provider version.")
        elif plan["provider"] == "hugging_face":
            parts = parsed.path.strip("/").split("/")
            if parsed.hostname != "huggingface.co" or parsed.query or len(parts) < 5 or parts[2] != "resolve" or parts[3] != version or not re.fullmatch(r"[0-9a-f]{40}", version):
                raise ModelResourceError("Acquisition needs an exact Hugging Face commit URL.")
        else:
            raise ModelResourceError("Unsupported acquisition provider.")
    elif step.kind == "companions":
        if not isinstance(value["request"], dict) or not isinstance(value["choices"], list) or len(value["choices"]) > MAX_RESOURCES:
            raise ModelResourceError("Companion verification needs a bounded exact request and slot choices.")
        slots = set()
        for row in value["choices"]:
            if not isinstance(row, dict) or not {"slot", "resource_id", "path"} <= set(row) or not _RESOURCE.fullmatch(str(row["resource_id"])):
                raise ModelResourceError("Companion choices need exact slots, resources and paths.")
            slot = _text(row["slot"], limit=128); _path_text(row["path"])
            if slot in slots: raise ModelResourceError("A companion slot appears more than once.")
            slots.add(slot)
    elif step.kind == "manual_cache":
        requirements = value["requirements"]
        if not isinstance(requirements, dict) or not isinstance(requirements.get("resources"), list) or not 1 <= len(requirements["resources"]) <= MAX_RESOURCES:
            raise ModelResourceError("Manual preparation needs a bounded exact resource set.")
        if not isinstance(value["approved_cleanup"], list) or len(value["approved_cleanup"]) > MAX_RESOURCES:
            raise ModelResourceError("Cleanup victims must be explicitly bounded in the plan.")
    elif step.kind == "paths":
        from .swarm_model_paths import ROLE_FIELDS
        if not isinstance(value["mode"], str) or value["mode"] not in {"single", "tiered"} or not isinstance(value["inventory"], dict) or not isinstance(value["values"], dict) or set(value["values"]) != set(ROLE_FIELDS):
            raise ModelResourceError("Path updates need a complete exact before/after role review.")
        for path in value["values"].values(): _text(path)
        _text(value["model_root"])
        if value["mode"] == "tiered": _path_text(value["model_root"])
        prepared = value["prepared_steps"]
        if not isinstance(prepared, list) or len(prepared) > MAX_RESOURCES or any(not isinstance(item, str) for item in prepared) or len(set(prepared)) != len(prepared) or any(item not in step.depends_on for item in prepared):
            raise ModelResourceError("Prepared path inputs must name distinct direct preparation dependencies.")
        if value["mode"] == "tiered" and not prepared:
            raise ModelResourceError("Tiered paths require manual preparation dependencies.")


def plan_payload(plan):
    if not isinstance(plan, ImportPlan): raise ModelResourceError("Choose a typed import plan.")
    return {"source_binding": deepcopy(plan.source_binding), "registry_token": plan.registry_token,
        "steps": [{"step_id": step.step_id, "kind": step.kind, "depends_on": list(step.depends_on),
            "choices": deepcopy(step.choices), "expected_effects": deepcopy(step.expected_effects)} for step in plan.steps]}


def parse_plan(value):
    if not isinstance(value, dict) or set(value) != {"source_binding", "registry_token", "steps"}:
        raise ModelResourceError("Unsupported import plan schema.")
    _metadata(value); _hash(value["registry_token"])
    binding = value["source_binding"]
    if not isinstance(binding, dict) or not binding or len(binding) > 32 or any(not isinstance(v, str) or not v or len(v) > 4096 for v in binding.values()):
        raise ModelResourceError("Import requires exact enrolled source binding.")
    if not isinstance(value["steps"], list) or not 1 <= len(value["steps"]) <= MAX_STEPS:
        raise ModelResourceError("Choose between one and 128 supported import steps.")
    seen, steps = {}, []
    for row in value["steps"]:
        if not isinstance(row, dict) or set(row) != {"step_id", "kind", "depends_on", "choices", "expected_effects"}:
            raise ModelResourceError("Unsupported import step schema.")
        identity, kind, dependencies = row["step_id"], row["kind"], row["depends_on"]
        if not isinstance(identity, str) or not _ID.fullmatch(identity) or identity in seen or kind not in KINDS:
            raise ModelResourceError("Import step identity or kind is invalid or repeated.")
        if not isinstance(dependencies, list) or len(dependencies) > MAX_STEPS or any(not isinstance(v, str) for v in dependencies) or len(set(dependencies)) != len(dependencies) or any(v not in seen for v in dependencies):
            raise ModelResourceError("Import dependencies must name distinct earlier steps; cycles are not supported.")
        step = ImportStep(identity, kind, tuple(dependencies), deepcopy(row["choices"]), deepcopy(row["expected_effects"]))
        _choices(step)
        if kind == "paths" and any(seen[item].kind != "manual_cache" for item in step.choices["prepared_steps"]):
            raise ModelResourceError("Swarm prepared paths require manual-cache owners.")
        steps.append(step); seen[identity] = step
    encoded(value)
    return ImportPlan(deepcopy(binding), value["registry_token"], tuple(steps))


def _receipt(value, kind):
    if isinstance(value, OwnerReceipt): value = asdict(value)
    if (not isinstance(value, dict) or set(value) != {"owner", "operation_id", "locator", "fingerprint", "evidence"}
            or value["owner"] != kind or not isinstance(value["operation_id"], str)
            or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", value["operation_id"])):
        raise ModelResourceError("Import owner receipt does not identify the exact supported operation.")
    _path_text(value["locator"]); _hash(value["fingerprint"]); _metadata(value["evidence"])
    evidence = value["evidence"]
    canonical = evidence.get("canonical_reference") if isinstance(evidence, dict) else None
    fields = {"schema_version", "reference_kind", "owner", "operation_id", "locator", "fingerprint", "evidence"}
    owners = {"registration": "model_registration", "move": "model_relocation", "acquisition": "model_acquisition",
        "companions": "model_registration", "manual_cache": "model_cache", "paths": "swarm_model_paths"}
    if (not isinstance(canonical, dict) or set(canonical) != fields
            or type(canonical["schema_version"]) is not int or canonical["schema_version"] != 1
            or canonical["owner"] != owners[kind] or not isinstance(canonical["evidence"], dict)
            or not isinstance(canonical.get("reference_kind"), str)
            or canonical["reference_kind"] not in {"immutable_receipt", "registry_transition"}
            or any(canonical[key] != value[key] for key in ("operation_id", "locator", "fingerprint"))):
        raise ModelResourceError("Import owner evidence must be an immutable receipt or exact registry transition.")
    return OwnerReceipt(**deepcopy(value))


def _outcome(value, step, bound=None):
    if not isinstance(value, ActionOutcome) or not isinstance(value.status, str) or value.status not in _OUTCOMES or not isinstance(value.effects, dict):
        raise ModelResourceError("The import owner returned an unsupported outcome.")
    _hash(value.registry_token); _metadata(value.effects)
    if not isinstance(value.message, str) or len(value.message) > 4096:
        raise ModelResourceError("The import owner returned an invalid message.")
    receipt = _receipt(value.receipt, step.kind) if value.receipt is not None else bound
    if bound is not None and receipt != bound:
        raise ModelResourceError("The import owner changed its bound recovery receipt.")
    if value.status in {"completed", "recoverable"}:
        if receipt is None or value.effects != step.expected_effects:
            raise ModelResourceError("Owner outcome lacks the exact approved effects and receipt.")
    if value.status == "not_started" and (receipt is not None or value.effects):
        raise ModelResourceError("A not-started outcome cannot carry effects or an owner receipt.")
    return replace(value, receipt=receipt)


class ModelImportPlanService:
    def __init__(self, receipts: ImportReceiptStore, ports: Mapping[str, ActionPort]):
        if set(ports) - set(KINDS): raise ModelResourceError("Only fixed supported import action ports are allowed.")
        self.receipts, self.ports = receipts, dict(ports)
        self._reviews = {}
        self._revision_reviews = {}

    def _preview(self, step, context):
        if step.kind not in self.ports:
            raise ModelResourceError(f"The {step.kind} action is unavailable; retain this step for review.")
        preview = self.ports[step.kind].preview(step, context)
        if not isinstance(preview, ActionPreview) or not isinstance(preview.status, str) or preview.status not in {"ready", "waiting", "held"}:
            raise ModelResourceError("The import owner returned an unsupported preview.")
        if preview.observed_registry_token != context.registry_token or preview.observed_source_binding != context.source_binding:
            raise ModelResourceError("Registry or enrolled source changed outside this plan. Review the combined plan again.")
        if preview.status != "held" and preview.effects != step.expected_effects:
            raise ModelResourceError("Preview consequences differ from the approved choices. Review the combined plan again.")
        return preview

    def review(self, plan):
        plan = parse_plan(plan_payload(plan))
        context, results = ActionContext(plan.source_binding, plan.registry_token), []
        for step in plan.steps:
            try:
                preview = self._preview(step, context)
                status = "waiting" if step.depends_on and preview.status == "ready" else preview.status
                results.append(StepResult(step.step_id, status, deepcopy(preview.effects), context.registry_token, message=preview.message))
            except (OSError, RuntimeError, ValueError) as exc:
                results.append(StepResult(step.step_id, "held", message=str(exc)[:4096]))
        identity = uuid4().hex
        fingerprint = digest({"plan": plan_payload(plan), "steps": [asdict(row) for row in results]})
        review = ImportReview(identity, plan, tuple(results), fingerprint)
        if len(self._reviews) >= 32: self._reviews.pop(next(iter(self._reviews)))
        self._reviews[identity] = digest(asdict(review))
        return review

    def approve(self, review, *, confirmed=False):
        if not confirmed: raise ModelResourceError("Confirm the complete displayed import plan before applying it.")
        if not isinstance(review, ImportReview) or self._reviews.get(review.review_id) != digest(asdict(review)):
            raise ModelResourceError("Review the unchanged import plan in this session before approval.")
        if any(row.status == "held" for row in review.steps):
            raise ModelResourceError("Resolve held choices and review the combined plan before approval.")
        parse_plan(plan_payload(review.plan))
        return self.receipts.create({"schema_version": 1, "plan": plan_payload(review.plan), "review_fingerprint": review.fingerprint})

    def _ledger(self, approval_id):
        value = self.receipts.read(approval_id)
        if not isinstance(value, dict) or set(value) != {"schema_version", "plan", "review_fingerprint"} or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ModelResourceError("Unsupported saved import approval.")
        _hash(value["review_fingerprint"])
        events = self.receipts.events(approval_id)
        plan, history, revision = self._replay(parse_plan(value["plan"]), events, approval_id)
        return plan, history, revision, events

    def read(self, approval_id):
        """Resolve only named ledger metadata; owner locators remain inert."""
        return self._ledger(approval_id)[0]

    def read_with_revision(self, approval_id):
        """Return one coherent displayed plan/token pair for later execution."""
        plan, _, revision, _ = self._ledger(approval_id)
        return plan, revision

    def read_recorded_import(self, approval_id):
        """Read saved step states only; no live owner, source, or registry check."""
        plan, history, revision, _ = self._ledger(approval_id)
        states = {}
        for step in plan.steps:
            state = history.get(step.step_id)
            states[step.step_id] = (
                state["outcome"].status if state and state["outcome"] is not None else
                "started" if state and state["receipt"] is not None else
                "intent" if state else "not_started"
            )
        return plan, revision, states

    def recorded_fast_recovery(self, approval_id, *, expected_revision):
        """Locate one bound unfinished Fast owner in the displayed saved revision.

        This is ledger metadata, not proof that its native operation is current or
        recoverable. The Fast service must independently review the exact owner.
        """
        plan, history, revision, _ = self._ledger(approval_id)
        if expected_revision != revision:
            raise ModelResourceError("The displayed import revision is stale. Reopen it before reviewing Fast recovery.")
        candidates = []
        for step in plan.steps:
            if step.kind != "manual_cache":
                continue
            state = history.get(step.step_id)
            if state is None or state["receipt"] is None:
                continue
            outcome = state["outcome"]
            if outcome is not None and outcome.status == "completed":
                continue
            receipt = state["receipt"]
            if receipt.owner != "manual_cache":
                raise ModelResourceError("The saved Fast owner differs from its exact import step.")
            candidates.append(receipt)
        if len(candidates) != 1:
            raise ModelResourceError("No unique interrupted Fast owner is recorded for this saved import. Review its exact steps and receipts before recovery.")
        return candidates[0]

    def recent_saved_imports(self, *, limit=50):
        """Metadata-only summaries for a bounded picker; no owner or source read."""
        summaries = []
        for approval_id, saved_ns in self.receipts.recent_approvals(limit=limit):
            try:
                plan, history, _, _ = self._ledger(approval_id)
            except (ModelResourceError, OSError, ValueError) as exc:
                summaries.append({"approval_id": approval_id, "saved_ns": saved_ns,
                    "source": "Saved import", "completed": 0, "total": 0,
                    "status": "Needs review", "error": str(exc)[:240]})
                continue
            completed = sum(
                state is not None and state["outcome"] is not None
                and state["outcome"].status == "completed"
                for step in plan.steps for state in (history.get(step.step_id),)
            )
            total = len(plan.steps)
            source = str(plan.source_binding.get("server_id") or "Enrolled SwarmUI source")[:120]
            status = ("Recorded complete · needs recheck" if completed == total else
                "Approved · not started" if not history else
                f"{completed} of {total} recorded complete")
            summaries.append({"approval_id": approval_id, "saved_ns": saved_ns,
                "source": source, "completed": completed, "total": total,
                "status": status, "error": ""})
        return tuple(summaries)

    def revision_token(self, approval_id):
        return self._ledger(approval_id)[2]

    def _history(self, approval_id, plan):
        actual, history, _, _ = self._ledger(approval_id)
        if actual != plan:
            raise ModelResourceError("The displayed import revision changed; reopen it before continuing.")
        return history

    @staticmethod
    def _revision_binding(review):
        return {key: value for key, value in asdict(review).items() if key not in {"review_id", "fingerprint"}}

    def _remaining_state_locked(self, approval_id, *, expected_revision=None, should_cancel=None, progress=None):
        plan, history, revision, events = self._ledger(approval_id)
        if expected_revision is not None and revision != expected_revision:
            raise ModelResourceError("The displayed import revision changed; reopen remaining work before proposing choices.")
        if sum(event["event"] == "revision" for event in events) >= MAX_REVISIONS:
            raise ModelResourceError("Import revision history is full; preserve its completed operation records.")
        # Reconcile only the old exact owners. A changed proposal cannot redirect
        # their recovery, and the completed prefix is never sent to preview again.
        proven, token, failure = self._reconcile_history(approval_id, plan, history, should_cancel, progress)
        if failure is not None:
            raise ModelResourceError("Reprove existing import operations before revising: " + failure.message)
        actual, history, current_revision, events = self._ledger(approval_id)
        if actual != plan or current_revision != revision:
            raise ModelResourceError("The import revision changed during review.")
        prefix = self._revision_prefix(plan, plan, history)
        if any(step.step_id not in proven or proven[step.step_id].status != "completed" for step in plan.steps[:prefix]):
            raise ModelResourceError("Every retained prefix operation needs fresh canonical completion proof.")
        results = tuple(proven[step.step_id] for step in plan.steps[:prefix])
        return plan, history, revision, events, prefix, ActionContext(deepcopy(plan.source_binding), token, results)

    def propose_remaining(self, approval_id, proposer, *, expected_revision, should_cancel=None, progress=None):
        """Call the trusted metadata proposer with a freshly proven prefix.

        The callback may preview exact remaining consequences, but must not
        execute owners or approve work. Its returned plan is still unapproved.
        """
        _hash(expected_revision)
        with self.receipts.execution_lock(approval_id):
            plan, history, _, events, prefix, context = self._remaining_state_locked(approval_id,
                expected_revision=expected_revision, should_cancel=should_cancel, progress=progress)
            if should_cancel and should_cancel():
                raise InterruptedError("Remaining-work proposal cancelled; no choices were approved.")
            proposed = parse_plan(plan_payload(proposer(deepcopy(plan), prefix, deepcopy(context))))
            self._revision_prefix(plan, proposed, history)
            if self.receipts.events(approval_id) != events:
                raise ModelResourceError("The import history changed during proposal; review remaining work again.")
            return proposed

    def _review_remaining_locked(self, approval_id, proposed_full_plan, *, issue=True, should_cancel=None, progress=None):
        proposed = parse_plan(plan_payload(proposed_full_plan))
        plan, history, revision, events, prefix, context = self._remaining_state_locked(approval_id,
            should_cancel=should_cancel, progress=progress)
        self._revision_prefix(plan, proposed, history)
        if proposed == plan:
            raise ModelResourceError("Choose a changed remaining plan before requesting a revision.")
        token, results = context.registry_token, list(context.completed)
        for step in proposed.steps[prefix:]:
            if should_cancel and should_cancel():
                raise InterruptedError("Remaining-work review cancelled; no revision was approved.")
            try:
                preview = self._preview(step, context)
                results.append(StepResult(step.step_id, preview.status, deepcopy(preview.effects), token, message=preview.message))
            except (OSError, RuntimeError, ValueError) as exc:
                results.append(StepResult(step.step_id, "held", message=str(exc)[:4096]))
        # Preview must not mutate this ledger. Reconciliation's own metadata
        # updates above are included in the captured review head.
        after_events = self.receipts.events(approval_id)
        if after_events != events:
            raise ModelResourceError("The import history changed during remaining-work preview.")
        head = digest(events[-1]) if events else approval_id
        review = RevisionReview(uuid4().hex, approval_id, revision, head, prefix, token,
            proposed, tuple(results), "")
        review = replace(review, fingerprint=digest(self._revision_binding(review)))
        if issue:
            if len(self._revision_reviews) >= 32: self._revision_reviews.pop(next(iter(self._revision_reviews)))
            self._revision_reviews[review.review_id] = digest(asdict(review))
        return review

    def review_remaining(self, approval_id, proposed_full_plan, *, should_cancel=None, progress=None):
        """Explicit review may reprove owners; merely opening an approval never does."""
        with self.receipts.execution_lock(approval_id):
            return self._review_remaining_locked(approval_id, proposed_full_plan, should_cancel=should_cancel, progress=progress)

    def approve_revision(self, review, *, confirmed=False, should_cancel=None, progress=None):
        if not confirmed:
            raise ModelResourceError("Confirm the complete revised remaining work before applying its new choices.")
        if (not isinstance(review, RevisionReview)
                or self._revision_reviews.get(review.review_id) != digest(asdict(review))):
            raise ModelResourceError("Review the unchanged remaining work in this session before confirming it.")
        if any(row.status == "held" for row in review.steps):
            raise ModelResourceError("Resolve held remaining-work choices before confirming a revision.")
        with self.receipts.execution_lock(review.approval_id):
            _, _, revision, events = self._ledger(review.approval_id)
            head = digest(events[-1]) if events else review.approval_id
            if revision != review.prior_revision or head != review.parent_head:
                raise ModelResourceError("The reviewed revision or import history changed; review remaining work again.")
            fresh = self._review_remaining_locked(review.approval_id, review.plan, issue=False,
                should_cancel=should_cancel, progress=progress)
            if fresh.fingerprint != review.fingerprint:
                raise ModelResourceError("The remaining-work proof, registry or consequences changed; review again.")
            if should_cancel and should_cancel():
                raise InterruptedError("Remaining-work approval cancelled; no revision was approved.")
            event = self.receipts.append(review.approval_id, "revision", "revision", {
                "plan": plan_payload(review.plan), "prefix_length": review.prefix_length,
                "parent_head": review.parent_head, "prior_revision": review.prior_revision,
                "review_registry_token": review.registry_token, "review_fingerprint": review.fingerprint},
                expected_head=review.parent_head)
            del self._revision_reviews[review.review_id]
            return digest(event)

    @staticmethod
    def _revision_prefix(plan, proposed, history):
        if proposed.source_binding != plan.source_binding or proposed.registry_token != plan.registry_token:
            raise ModelResourceError("A revision must preserve the original enrolled source and initial registry token.")
        prefix = 0
        for step in plan.steps:
            state = history.get(step.step_id)
            if not state or state["outcome"] is None or state["outcome"].status != "completed" or not state["started"] or state["receipt"] is None:
                break
            prefix += 1
        if set(history) != {step.step_id for step in plan.steps[:prefix]}:
            raise ModelResourceError("Only a wholly unstarted suffix can be revised; resolve every recorded intent or bound owner first.")
        if prefix == len(plan.steps):
            raise ModelResourceError("This import has no unstarted remaining work to revise.")
        if len(proposed.steps) < prefix or encoded(plan_payload(plan)["steps"][:prefix]) != encoded(plan_payload(proposed)["steps"][:prefix]):
            raise ModelResourceError("Completed import steps, choices and effects must remain byte-identical.")
        return prefix

    def _replay(self, plan, events, approval_id):
        steps = {step.step_id: step for step in plan.steps}
        history = {}
        token = plan.registry_token
        revision, revisions = approval_id, 0
        for event in events:
            identity, payload = event["step_id"], event["payload"]
            if event["event"] == "revision":
                revisions += 1
                if (revisions > MAX_REVISIONS or identity != "revision" or set(payload) != {
                        "plan", "prefix_length", "parent_head", "prior_revision", "review_registry_token", "review_fingerprint"}
                        or type(payload["prefix_length"]) is not int or payload["parent_head"] != event["previous"]
                        or payload["prior_revision"] != revision or payload["review_registry_token"] != token):
                    raise ModelResourceError("Import revision history is changed, stale or exceeds its supported bound.")
                _hash(payload["review_fingerprint"])
                proposed = parse_plan(payload["plan"])
                if self._revision_prefix(plan, proposed, history) != payload["prefix_length"]:
                    raise ModelResourceError("The revision's completed prefix differs from its exact preceding history.")
                plan, revision = proposed, digest(event)
                steps = {step.step_id: step for step in plan.steps}
                continue
            if identity not in steps or not isinstance(payload.get("attempt_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", payload["attempt_id"]):
                raise ModelResourceError("Import event names an unknown step or attempt.")
            step = steps[identity]
            previous = history.get(identity)
            if event["event"] == "intent":
                if set(payload) != {"attempt_id", "registry_token", "expected_effects", "prior_receipt"} or payload["expected_effects"] != step.expected_effects:
                    raise ModelResourceError("Import intent differs from its approved effects.")
                _hash(payload["registry_token"])
                if payload["registry_token"] != token:
                    raise ModelResourceError("Recorded import input revision does not continue its prior exact outcome.")
                if any(history.get(item, {}).get("outcome") is None or history[item]["outcome"].status != "completed" for item in step.depends_on):
                    raise ModelResourceError("Recorded import action started before its prerequisites completed.")
                if any(key != identity and (state["outcome"] is None or state["outcome"].status not in {"completed", "not_started"}) for key, state in history.items()):
                    raise ModelResourceError("Recorded import action bypassed an unresolved operation.")
                prior = _receipt(payload["prior_receipt"], step.kind) if payload["prior_receipt"] is not None else None
                if prior is not None and (previous is None or previous["outcome"] is None or previous["outcome"].status != "recoverable"):
                    raise ModelResourceError("A recovery receipt cannot be adopted without its prior proven operation.")
                if previous and (previous["outcome"] is None or previous["outcome"].status not in {"not_started", "recoverable"}):
                    raise ModelResourceError("An unresolved or completed import operation was retried without recovery.")
                if previous and previous["outcome"].status == "recoverable" and prior != previous["receipt"]:
                    raise ModelResourceError("Import retry changed its exact recovery owner.")
                history[identity] = {"attempt_id": payload["attempt_id"], "sequence": event["sequence"], "registry_token": payload["registry_token"], "receipt": prior, "started": False, "outcome": None}
            else:
                if (previous is None or previous["attempt_id"] != payload["attempt_id"]
                        or (previous["outcome"] is not None and event["event"] != "reconciled")):
                    raise ModelResourceError("Import event has no matching unresolved intent.")
                if event["event"] == "started":
                    if set(payload) != {"attempt_id", "receipt"} or previous["started"]:
                        raise ModelResourceError("Import owner start is duplicated or invalid.")
                    receipt = _receipt(payload["receipt"], step.kind)
                    if previous["receipt"] is not None and receipt != previous["receipt"]:
                        raise ModelResourceError("Import recovery changed its owner.")
                    previous.update(receipt=receipt, started=True)
                else:
                    if set(payload) != {"attempt_id", "outcome"} or not isinstance(payload["outcome"], dict) or set(payload["outcome"]) != {"status", "effects", "registry_token", "receipt", "message"}:
                        raise ModelResourceError("Import outcome record is malformed.")
                    raw = payload["outcome"]
                    receipt = _receipt(raw["receipt"], step.kind) if raw["receipt"] is not None else None
                    if event["event"] == "reconciled" and not previous["started"]:
                        raise ModelResourceError("Reconciliation needs an exact recorded owner start.")
                    outcome = _outcome(ActionOutcome(**{**raw, "receipt": receipt}), step, previous["receipt"])
                    if outcome.status in {"completed", "recoverable"} and not previous["started"]:
                        raise ModelResourceError("An import completion has no recorded owner start.")
                    if outcome.status == "not_started" and outcome.registry_token != previous["registry_token"]:
                        raise ModelResourceError("A not-started operation cannot advance registry authority.")
                    previous["outcome"] = outcome
                    if outcome.status in {"completed", "recoverable"}:
                        token = outcome.registry_token
        return plan, history, revision

    def _reconcile_history(self, approval_id, plan, history, should_cancel, progress):
        """Prove the latest owner, then earlier effects using only proven suffixes."""
        steps = {step.step_id: step for step in plan.steps}
        ordered = sorted(history.items(), key=lambda item: item[1]["sequence"], reverse=True)
        proven, pending_records = {}, []
        token = None
        for identity, state in ordered:
            step = steps[identity]
            if state["receipt"] is None:
                if state["outcome"] is not None and state["outcome"].status == "not_started":
                    continue
                failure = StepResult(identity, "held", message="Owner receipt missing after recorded intent. Preserve evidence; do not retry blindly.")
                return {}, plan.registry_token, failure
            if should_cancel and should_cancel():
                return {}, token or plan.registry_token, StepResult(identity, "cancelled", receipt=state["receipt"], message="Reconciliation cancelled; exact receipts remain retained.")
            observed = token or (state["outcome"].registry_token if state["outcome"] else state["registry_token"])
            context = ActionContext(deepcopy(plan.source_binding), observed, tuple(proven.values()), state["receipt"])
            try:
                if step.kind not in self.ports: raise ModelResourceError("The recorded action owner is unavailable.")
                outcome = _outcome(self.ports[step.kind].reconcile(step, state["receipt"], context,
                    should_cancel=should_cancel, progress=progress), step, state["receipt"])
                if outcome.status not in {"completed", "recoverable"}:
                    return {}, observed, self._result(step, outcome)
                if token is not None and outcome.registry_token != token:
                    raise ModelResourceError("Registry changed during reverse owner reconciliation; retry the complete review.")
                if proven and outcome.status != "completed":
                    raise ModelResourceError("An earlier owner is unfinished despite later recorded effects; preserve recovery evidence.")
            except (OSError, RuntimeError, ValueError) as exc:
                return {}, observed, StepResult(identity, "cancelled" if isinstance(exc, InterruptedError) else "held", receipt=state["receipt"], message=str(exc)[:4096])
            token = outcome.registry_token
            proven[identity] = self._result(step, outcome)
            if state["outcome"] != outcome:
                pending_records.append((identity, state["attempt_id"], outcome))
        # No operation starts until every referenced owner has proven the same
        # current snapshot. Never pass unverified ledger claims to a port.
        for identity, attempt, outcome in pending_records:
            if not history[identity]["started"]:
                # A crash may separate a recovery intent from its repeated
                # start callback. Its inherited owner was already bound by the
                # previous attempt and has just been canonically re-proven.
                self.receipts.append(approval_id, "started", identity,
                    {"attempt_id": attempt, "receipt": asdict(outcome.receipt)})
            self.receipts.append(approval_id, "reconciled", identity, {"attempt_id": attempt, "outcome": asdict(outcome)})
        return proven, token or plan.registry_token, None

    @staticmethod
    def _result(step, outcome):
        return StepResult(step.step_id, outcome.status, deepcopy(outcome.effects), outcome.registry_token, outcome.receipt, outcome.message)

    def execute(self, approval_id, *, confirmed=False, should_cancel=None, progress=None, expected_revision=None):
        return self.resume(approval_id, confirmed=confirmed, should_cancel=should_cancel, progress=progress,
            expected_revision=expected_revision)

    def resume(self, approval_id, *, confirmed=False, should_cancel=None, progress=None, expected_revision=None):
        if not confirmed: raise ModelResourceError("Explicitly Apply or Resume the displayed import plan; reopening starts nothing.")
        with self.receipts.execution_lock(approval_id):
            plan, history, revision, _ = self._ledger(approval_id)
            if (expected_revision is not None and expected_revision != revision
                    or revision != approval_id and expected_revision is None):
                raise ModelResourceError("The displayed import revision is missing or stale. Reopen and explicitly confirm the current remaining work.")
            proven, token, failure = self._reconcile_history(approval_id, plan, history, should_cancel, progress)
            if failure is not None:
                return self._run_result(plan, [failure], token, failure.status, failure.message)
            results = []
            for index, step in enumerate(plan.steps):
                if should_cancel and should_cancel():
                    return self._run_result(plan, results, token, "cancelled", "Import paused by cancellation; exact owner receipts and pins are retained.")
                if progress: progress(index, len(plan.steps), f"Import: {step.kind}")
                if step.step_id in proven and proven[step.step_id].status == "completed":
                    results.append(proven[step.step_id])
                    continue
                if not set(step.depends_on) <= {row.step_id for row in results if row.status == "completed"}:
                    return self._run_result(plan, results, token, "held", "A prerequisite remains unfinished; no dependent action started.")
                current = history.get(step.step_id)
                context = ActionContext(deepcopy(plan.source_binding), token, tuple(results), current["receipt"] if current else None)
                try:
                    preview = self._preview(step, context)
                except (OSError, RuntimeError, ValueError) as exc:
                    results.append(StepResult(step.step_id, "held", receipt=context.prior_receipt, message=str(exc)[:4096]))
                    return self._run_result(plan, results, token, "held", results[-1].message)
                if preview.status != "ready":
                    results.append(StepResult(step.step_id, preview.status, message=preview.message, receipt=context.prior_receipt))
                    return self._run_result(plan, results, token, preview.status, preview.message)
                attempt = uuid4().hex
                self.receipts.append(approval_id, "intent", step.step_id, {"attempt_id": attempt, "registry_token": token,
                    "expected_effects": step.expected_effects, "prior_receipt": asdict(context.prior_receipt) if context.prior_receipt else None})
                bound = None
                def started(reference):
                    nonlocal bound
                    receipt = _receipt(reference, step.kind)
                    if bound is not None or (context.prior_receipt is not None and receipt != context.prior_receipt):
                        raise ModelResourceError("Import owner start changed or repeated its exact receipt.")
                    self.receipts.append(approval_id, "started", step.step_id, {"attempt_id": attempt, "receipt": asdict(receipt)})
                    bound = receipt
                try:
                    outcome = _outcome(self.ports[step.kind].execute(step, preview, context, on_started=started,
                        should_cancel=should_cancel, progress=progress), step, bound)
                    if outcome.receipt is not None and bound is None:
                        raise ModelResourceError("The action returned an owner reference without first binding it.")
                    if outcome.status == "not_started" and outcome.registry_token != token:
                        raise ModelResourceError("A not-started operation cannot advance registry authority.")
                    if outcome.status in {"completed", "recoverable"} and bound is None:
                        raise ModelResourceError("The action returned effects without first binding its receipt.")
                except (OSError, RuntimeError, ValueError) as exc:
                    outcome = ActionOutcome("cancelled" if isinstance(exc, InterruptedError) else "uncertain", {}, token, bound, str(exc)[:4096])
                self.receipts.append(approval_id, "outcome", step.step_id, {"attempt_id": attempt, "outcome": asdict(outcome)})
                results.append(self._result(step, outcome))
                if outcome.status != "completed":
                    return self._run_result(plan, results, token, outcome.status, outcome.message)
                token = outcome.registry_token
            if progress: progress(len(plan.steps), len(plan.steps), "Import completed; owner receipts retained")
            return ImportRun("completed", tuple(results), token, "All approved supported import steps completed. No global rollback is implied.")

    @staticmethod
    def _run_result(plan, results, token, status, message):
        seen = {row.step_id for row in results}
        remaining = tuple(StepResult(step.step_id, "waiting", message="Not started; previous work remains held or unfinished.") for step in plan.steps if step.step_id not in seen)
        return ImportRun(status, tuple(results) + remaining, token, message)
