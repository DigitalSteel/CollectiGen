"""Pure plain-text projection of supplied DatabaseReview; no preview/writer/Qt calls."""
from dataclasses import dataclass
from itertools import chain, islice
from suite_core.database_actions import (
    DatabaseActionsController, DatabasePreviewStatus, DatabaseReview, PRESENTATION_INTERFACE_VERSION,
)

MAX_DISPLAY_ROWS = 100
MAX_DISPLAY_TEXT = 1024


def project_connected_catalog(choice):
    """Project supplied accepted context only; no filesystem/settings reads."""
    from pathlib import Path
    if choice is None:
        return "No catalog connected. Choose a connection draft or open a saved connection."
    if choice.native_adapter is None:
        return "Selected catalog folders are unavailable. Open a saved connection to verify them."
    root, source = Path(choice.transactions.host.root), Path(choice.pack).parent
    if not root.is_absolute() or not source.is_absolute() or root != choice.native_adapter.root:
        return "Selected catalog folders are unavailable. Open a saved connection to verify them."
    return "Connected catalog: " + str(root) + "\nApproved source: " + str(source)

FIELD_LABELS = {'display': 'Name', 'category_id': 'Category', 'aliases': 'Aliases',
                'minimum_rating': 'Minimum rating', 'classification_status': 'Classification'}
APPLY_DISABLED = "Preview only. Apply is unavailable: native writing, backup, read-back and recovery are not connected or qualified."
PREVIEW_DISABLED = "Catalog preview is not connected. You can review changes when a compatible catalog and preview service are available."
REASONS = {
    "catalog_name_collision": "Different stable IDs share a canonical or alias name; review them separately.",
    "default_conflicts_with_user_override": "Existing user edits are protected; the default-only proposal needs review.",
    "default_conflicts_with_local_retirement": "A local removal or refinement is retained; the default-only proposal needs review.",
    "default_would_exclude_local_record": "The proposal excludes this reusable catalog record; original image assignments are retained.",
    "missing_local_baseline": "A prior-default record is absent locally; review its removal before proposing defaults.",
    "untracked_identity_collision": "An existing identity lacks a matching prior-default baseline; keep it separate for review.",
    "specific_refinement_requires_review": "A supplied refinement offers specific targets; choose deliberately during later review.",
    "refinement_targets_unavailable": "The supplied refinement targets are unavailable in this proposal.",
    "exact_identity_choice_requires_review": "A specific identity is offered for review; it is not an automatic reassignment.",
    "ambiguous_identity_requires_review": "More than one identity is possible; keep the original until a specific choice is reviewed.",
    "assignment_retained_without_catalog_target": "No proposed catalog target is available; the original assignment is retained.",
    "local_retirement_retained": "The local removal/refinement and original assignment are retained.",
}

@dataclass(frozen=True)
class DatabaseCatalogChoice:
    label: str
    controller: DatabaseActionsController

    def __post_init__(self):
        if type(self.label) is not str or not self.label.strip() or type(self.controller) is not DatabaseActionsController:
            raise ValueError("Supply an explicit approved catalog label and its existing preview controller")

@dataclass(frozen=True)
class ReviewSection:
    title: str
    rows: tuple[str, ...]
    total: int | None

    def text(self):
        if self.total is None: return "Details unknown until a current supplied preview is accepted."
        count = f"Showing {len(self.rows)} of {self.total} rows."
        if self.total > len(self.rows): count += " Full evidence remains in the supplied review."
        return count + "\n\n" + ("\n\n".join(self.rows) if self.rows else "None in this proposal.")

@dataclass(frozen=True)
class DatabasePaneState:
    selection: str
    status: str
    versions: tuple[str, ...]
    sections: tuple[ReviewSection, ...]
    review_accepted: bool = False
    apply_enabled: bool = False
    apply_reason: str = APPLY_DISABLED

def project_connection_error(error, operation):
    """Explain only verified source-size holds; retain the native diagnostic."""
    if operation not in {"connection_review", "connection_confirm", "connection_open"} or error != "payload_size":
        return None
    message = "The selected Tag package changed or is incomplete."
    action = "Choose the complete approved source folder with Browse, then select Review connection and confirm the new review."
    return message + " " + action, message + "\n\n" + action + "\n\nDiagnostic code: " + error


def _text(value):
    if value is None: return "not supplied"
    if type(value) is not str: return "unknown"
    return value if len(value) <= MAX_DISPLAY_TEXT else value[:MAX_DISPLAY_TEXT] + " [display shortened; full value retained]"

def _field(value):
    if type(value) is tuple:
        values = ", ".join(_text(item) for item in value[:8])
        return values + (f" [+{len(value)-8} values retained]" if len(value)>8 else "")
    return _text(value)

def _explain(reason):
    kind, _, scope = reason.partition(":")
    explanation = REASONS.get(kind, "The catalog preview requires review; no change has been applied.")
    return explanation + (" Scope: " + _text(scope) if scope else "")

def _choice(value):
    if value == "keep_original": return "Keep original"
    if value == "skip_review": return "Skip review"
    if value.startswith("review_specific:"): return "Review specific target: " + _text(value.partition(":")[2])
    return "Choice needs review"

def _section(title, values, render):
    return ReviewSection(title, tuple(render(row) for row in values[:MAX_DISPLAY_ROWS]), len(values))

def project_database_review(controller=None, review=None):
    """Freshness comes only from the supplied controller; never compute a merge or apply."""
    titles = ("Additions", "Untouched-field updates", "Protected user edits", "Conflicts",
              "Retained and excluded vocabulary", "Original image assignment choices", "Content release notes")
    unknown_sections = tuple(ReviewSection(title, (), None) for title in titles)
    unknown_versions = ("Installed default content: unknown", "Available content: unknown")
    if PRESENTATION_INTERFACE_VERSION != 1 or type(controller) is not DatabaseActionsController:
        return DatabasePaneState("No catalog connected", "No catalog selected. Installation and content review are not available yet.",
                                 unknown_versions, unknown_sections)
    selection = "Selected catalog: " + _text(controller.session.binding.catalog_id)
    if review is None:
        return DatabasePaneState(selection, "No current review supplied. Versions remain unknown until an approved preview is delivered.",
                                 unknown_versions, unknown_sections)
    if type(review) is not DatabaseReview or type(review.status) is not DatabasePreviewStatus or review.status is DatabasePreviewStatus.STALE or not controller.accepts(review):
        return DatabasePaneState(selection, "Review is out of date or belongs to another selection. Request a fresh scoped preview.",
                                 unknown_versions, unknown_sections)
    statuses = {
        DatabasePreviewStatus.REVIEW_READY: "Ready to review; Apply remains unavailable.",
        DatabasePreviewStatus.REVIEW_REQUIRED: "Decisions need review; original vocabulary and image assignments are unchanged.",
        DatabasePreviewStatus.UNAVAILABLE: "Available catalog content was not supplied; change counts are unknown.",
        DatabasePreviewStatus.STALE: "Review is out of date; request a fresh scoped preview.",
        DatabasePreviewStatus.CANCELLED: "Preview cancelled; no catalog or image assignment was changed.",
        DatabasePreviewStatus.ERROR: "Supplied preview inputs need correction; no change counts are inferred.",
    }
    versions = (
        "Installed default content: " + (_text(review.installed_version) if review.installed_version is not None else "not installed (supplied observation; database engine status is separate)"),
        "Supplied available content: " + (_text(review.available_version) if review.available_version is not None else "unavailable"),
        "Available release date: " + _text(review.available_date),
        "Installed content source: " + _text(review.installed_source),
        "Available content source: " + _text(review.available_source),
        "Available is supplied content, not a claim of the latest release. Pack versions are separate from software/schema/lookup versions.",
    )
    if review.proposal is None:
        release_notes = unknown_sections[-1] if review.available_version is None else _section(titles[-1], review.whats_new, _text)
        sections = unknown_sections[:-1] + (release_notes,)
    else:
        def change(row):
            fields = ("display", "category_id", "aliases", "minimum_rating", "classification_status")
            values = []
            for name in fields:
                before, proposed = getattr(row.before, name, None), getattr(row.proposed, name, None)
                if row.before is None:
                    if proposed is None or name == 'display':
                        continue  # Unknown optional facts are not invented.
                    value = _field(proposed) or '(none)'
                else:
                    value = 'before ' + (_field(before) or '(none)') + '; proposed ' + (_field(proposed) or '(none)')
                values.append(FIELD_LABELS[name] + ': ' + value)
            title = getattr(row.proposed, 'display', None) or getattr(row.before, 'display', None) or row.tag_id
            return 'Tag: ' + _text(title) + '\n' + '\n'.join(values) + '\nStable ID: ' + _text(row.tag_id)
        def protected(row):
            fields = tuple(FIELD_LABELS.get(name, _text(name).replace('_', ' ')) + ": local " + _field(getattr(row.before, name, None)) + "; proposed result " + _field(getattr(row.proposed, name, None)) for name in row.protected_fields)
            title = getattr(row.before, 'display', None) or row.tag_id
            return 'Tag: ' + _text(title) + "\nProtected local fields retained:\n" + "\n".join(fields) + '\nStable ID: ' + _text(row.tag_id)
        def conflict(row): return _explain(row.reason) + "\nExact IDs: " + (_field(row.tag_ids) or "not supplied")
        def assignment(row):
            original = row.original
            choices = "; ".join(_choice(value) for value in row.choices[:8])
            if len(row.choices)>8: choices += f" [+{len(row.choices)-8} offered choices retained]"
            return "Occurrence: " + _text(original.occurrence_id) + "\nOriginal ID: " + _text(original.tag_id) + "\nOriginal value: " + _text(original.source_value) + "\nEvidence: " + _text(original.evidence_ref) + "\n" + _explain(row.reason) + "\nOffered choices: " + choices + "\nRelationship evidence: " + (_field(row.relationship_refs) or "not supplied")
        vocabulary = chain(("Retained catalog ID: " + _text(i) for i in review.retained_local_ids),
                           ("Excluded from proposal: " + _text(i) + " (original image assignments retained)" for i in review.excluded_catalog_ids))
        vocabulary_section = ReviewSection(titles[4], tuple(islice(vocabulary, MAX_DISPLAY_ROWS)),
                                           len(review.retained_local_ids)+len(review.excluded_catalog_ids))
        sections = (
            _section(titles[0], review.additions, change), _section(titles[1], review.updates, change),
            _section(titles[2], review.protected_edits, protected), _section(titles[3], review.conflicts, conflict),
            vocabulary_section, _section(titles[5], review.assignment_choices, assignment),
            _section(titles[6], review.whats_new, _text),
        )
    modes={"blank":"Keep catalog blank","own":"Keep own catalog","default":"Use default catalog","merged":"Merge/update existing"}
    status=review.action_label + ": " + statuses[review.status] + " Proposal: " + modes.get(review.mode,"unknown")
    return DatabasePaneState(selection, status, versions, sections, True)
