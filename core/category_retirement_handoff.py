"""Private terminal retirement adoption; not a replay or crash-recovery UI.

The application owner must retain its exclusive visible-operation gate. Native
exclusion is acquired here on the adopting thread, never moved from a worker.
"""
from __future__ import annotations

import os

from .category_retirement import (
    CategoryRetirementHold, _clear_reference, _create_file, _digest, _json_bytes,
    classify_category_retirement, source_pin,
)
from .knowledge_write_admission import KnowledgeWriteAdmission, MAX_RECEIPT_BYTES, _checked_path, _read


class CategoryRetirementNotificationError(RuntimeError):
    """Authority is adopted; retry refresh, never repeat the completed disk save."""

    def __init__(self, session, result):
        super().__init__("Retirement was saved and adopted, but a view could not refresh. Refresh the view; do not repeat the save.")
        self.session, self.result = session, result


def live_authority_pin(session, *, include_revision=True):
    """Detect competing in-memory native edits without merging or saving them."""
    tags, categories, patterns, characters = (
        session.live_tags, session.live_categories, session.live_patterns, session.characters,
    )
    value = {
        "tags": tags._payload(), "categories": categories.user_payload(),
        "all_categories": {key: row.to_dict() for key, row in categories.categories.items()},
        "paths": [str(tags.path), str(categories.default_path), str(categories.user_path),
            str(session.default_relationship_path), str(session.user_relationship_path)],
        "relationships": [{key: row.to_dict() for key, row in store.relationships.items()}
            for store in (session.default_relationships, session.user_relationships)],
    }
    if include_revision:
        value["revision"] = getattr(tags, "_relationship_signature_revision", None)
    if patterns is not None:
        value["patterns"] = patterns.user_payload()
        value["all_patterns"] = {key: row.to_dict() for key, row in patterns.patterns.items()}
        value["paths"].extend((str(patterns.default_path), str(patterns.user_path)))
    if characters is not None:
        value["characters"] = {name: {key: row.to_dict() for key, row in getattr(characters, name).items()}
            for name in ("series", "characters", "outfits")}
        value["character_extras"] = [getattr(characters, name) for name in
            ("_series_extras", "_characters_extras", "_outfits_extras")]
        value["paths"].append(str(characters.data_dir))
    identities = tuple(id(owner) for owner in (
        tags, categories, patterns, characters, tags.rule_pair_admission,
        getattr(tags, "_app_relationship_workspace", None),
        getattr(tags, "_app_tag_alias_service", None), getattr(tags, "_app_tag_semantic_service", None),
    ))
    return identities, _digest(value)


def _install_authority_state(owner, state):
    """No constructors, reads or callbacks in the gated multi-object installation."""
    owner.__dict__ = state


def _configure_application_bundle(candidate, fresh):
    from .relationship_workspace import RelationshipWorkspaceService
    from .tag_alias_relationships import TagAliasRelationshipService
    from .tag_semantic_relationships import TagSemanticRelationshipService
    tags, categories = fresh.live_tags, fresh.live_categories
    workspace = RelationshipWorkspaceService(candidate.default_relationship_path,
        candidate.user_relationship_path, tags=tags)
    categories.configure_category_base_authority(workspace.effective)
    tags.apply_category_ordering(categories)
    alias = TagAliasRelationshipService(tags, default_path=candidate.default_relationship_path,
        user_path=candidate.user_relationship_path, workspace=workspace)
    semantic = TagSemanticRelationshipService(tags, default_path=candidate.default_relationship_path,
        user_path=candidate.user_relationship_path, workspace=workspace)
    tags._app_relationship_workspace = workspace
    tags._app_tag_alias_service = alias
    tags._app_tag_semantic_service = semantic
    # Native envelopes above, projected staged maps below. They are not interchangeable.
    fresh._refresh_category_base_authority(publish_live=True)
    fresh._refresh_tag_semantic_authority(publish_live=True)
    fresh._refresh_reusable_modifier_authority(publish_live=True)
    fresh._refresh_scoped_constraint_authority(publish_live=True)
    return fresh


def _prepared_application_bundle(candidate):
    prepared = candidate.prepare_detached_reload()
    if prepared is None:
        raise CategoryRetirementHold("Retirement sources changed while preparing application authority.")
    _, fresh = prepared
    fresh = _configure_application_bundle(candidate, fresh)
    if fresh._full_signature(fresh._state) != candidate._full_signature(candidate._state):
        raise CategoryRetirementHold("Prepared application authority differs from the completed retirement.")
    return fresh


def adopt_completed_retirement(candidate):
    """Private owner-thread handoff, called only with the app operation gate held."""
    candidate._require_active_ownership("adopt completed retirement")
    completed = getattr(candidate, "_completed_retirement", None)
    predecessor = getattr(candidate, "_retirement_predecessor", None)
    if completed is None or predecessor is None:
        raise CategoryRetirementHold("Choose this worker's exact completed retirement handoff.")
    operation_id, plan, saved_pin, receipt_pin, result = completed
    if plan is not getattr(candidate, "_prepared_retirement_plan", None):
        raise CategoryRetirementHold("Retirement handoff does not belong to the reviewed plan.")
    root = candidate.live_tags.path.parent
    admission = candidate.live_tags.rule_pair_admission
    if (admission is None or predecessor.live_tags.rule_pair_admission is not admission
            or not admission.owns(candidate.live_tags.path, "knowledge")):
        raise CategoryRetirementHold("Retirement handoff binding changed; keep the saved attempt held.")
    reference = admission.pending_root / f"category-retirement.{operation_id}.json"
    expected_reference = _json_bytes({"schema_version": 1, "kind": "category_retirement_reference",
        "operation_id": operation_id, "plan_sha256": plan.plan_sha256})
    sources = candidate._reload_source_paths()

    def verify():
        predecessor._require_active_ownership("adopt its completed retirement")
        if (predecessor.has_changes or predecessor._undo or predecessor._redo
                or predecessor._state.actions
                or predecessor._full_signature(predecessor._state) != predecessor._full_signature(predecessor._initial)
                or live_authority_pin(predecessor) != candidate._retirement_live_pin):
            raise CategoryRetirementHold("Shared Knowledge acquired edits; keep them and review the held retirement.")
        _checked_path(admission.pending_root, directory=True, required=True)
        with os.scandir(admission.pending_root) as entries:
            names = []
            for entry in entries:
                names.append(entry.name)
                if len(names) > 1:
                    raise CategoryRetirementHold("Other pending work prevents retirement handoff.")
        if names != [reference.name] or _read(reference, MAX_RECEIPT_BYTES) != expected_reference:
            raise CategoryRetirementHold("Retirement handoff reference changed; preserve the saved attempt.")
        state = classify_category_retirement(root, operation_id, expected_plan_sha256=plan.plan_sha256)
        if state.outcome != "complete" or state.receipt_sha256 != receipt_pin or source_pin(sources) != saved_pin:
            raise CategoryRetirementHold("Saved retirement evidence changed before shared adoption.")

    with KnowledgeWriteAdmission(root).locked():
        if KnowledgeWriteAdmission(root)._inspect_pending_locked():
            raise CategoryRetirementHold("Linked Knowledge recovery prevents retirement handoff.")
        verify()
        fresh = _prepared_application_bundle(candidate)
        verify()
        shared_tags, shared_categories, shared_patterns = (
            predecessor.live_tags, predecessor.live_categories, predecessor.live_patterns,
        )
        workspace = fresh.live_tags._app_relationship_workspace
        alias = fresh.live_tags._app_tag_alias_service
        semantic = fresh.live_tags._app_tag_semantic_service
        workspace.tags = shared_tags
        alias.tags = semantic.tags = shared_tags
        states = [(shared_categories, dict(fresh.live_categories.__dict__))]
        if shared_patterns is not None:
            states.append((shared_patterns, dict(fresh.live_patterns.__dict__)))
        tag_state = dict(fresh.live_tags.__dict__)
        tag_state["_categories"] = shared_categories
        tag_state["_relationship_signature_revision"] = max(
            getattr(shared_tags, "_relationship_signature_revision", 0),
            tag_state.get("_relationship_signature_revision", 0)) + 1
        states.append((shared_tags, tag_state))
        fresh.live_tags, fresh.live_categories, fresh.live_patterns = shared_tags, shared_categories, shared_patterns
        # Character records are not participants in retirement. Preserve their
        # actual shared object; preparation loaded them with recovery disabled.
        fresh.characters = predecessor.characters
        before_states = [(owner, owner.__dict__) for owner, _ in states]
        predecessor_state, candidate_state = predecessor.__dict__.copy(), candidate.__dict__.copy()
        installation_started = False
        try:
            verify()
            installation_started = True
            for owner, state in states:
                _install_authority_state(owner, state)
            if (shared_tags._categories is not shared_categories or workspace.tags is not shared_tags
                    or alias.tags is not shared_tags or semantic.tags is not shared_tags):
                raise CategoryRetirementHold("Shared retirement graph failed installation verification.")
            # Do not use verify(): the reviewed live before-state is intentionally
            # replaced now. Recheck saved authority/reference before releasing it.
            state = classify_category_retirement(root, operation_id, expected_plan_sha256=plan.plan_sha256)
            if state.outcome != "complete" or state.receipt_sha256 != receipt_pin or source_pin(sources) != saved_pin:
                raise CategoryRetirementHold("Retirement evidence changed during shared adoption.")
            _clear_reference(reference, expected_reference)
            if source_pin(sources) != saved_pin:
                raise CategoryRetirementHold("Retirement sources changed after handoff verification.")
            predecessor._retire_ownership()
            candidate._retire_ownership()
            return fresh, result
        except BaseException as failure:
            # Confirm/reinstate the durable hold BEFORE restoring old memory.
            # If reinstatement itself is impossible, do not expose stale shared
            # authority with a cleared reference after a completed disk save.
            try:
                if _read(reference, MAX_RECEIPT_BYTES) is None:
                    _create_file(reference, expected_reference)
            except BaseException as unavailable:
                if installation_started:
                    # Complete the prepared graph without the fault hook. A
                    # partial graph is unsafe when durable recovery is unreadable.
                    # Before installation, preserve any competing draft instead.
                    for owner, prepared_state in states:
                        owner.__dict__ = prepared_state
                    predecessor._retire_ownership()
                    candidate._retire_ownership()
                raise CategoryRetirementHold("The disk save is complete. Shared adoption recovery is unavailable; do not replay the save.") from unavailable
            for owner, previous in before_states:
                owner.__dict__ = previous
            predecessor.__dict__, candidate.__dict__ = predecessor_state, candidate_state
            # Terminal disk completion is never rolled back by adoption failure.
            raise CategoryRetirementHold("Shared adoption needs review; the completed disk save remains held.") from failure
