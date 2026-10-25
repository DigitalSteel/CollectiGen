"""Read-only Applications categories over the supplied Manager catalog.

These labels classify application records, never model files or install support.
"""
from dataclasses import dataclass, replace

APPLICATION_GROUPS = (
    ("All categories", "All applications", "Browse the supplied catalog and components in development."),
    ("AI Agents", "AI agents", "Applications explicitly described as agents by the supplied catalog."),
    ("LLM", "Chat & language assistants", "Applications for working with language models; model files remain under backend metadata."),
    ("Model Runners", "Model runners", "Applications explicitly described as model runners; a model file is not an application."),
    ("AI Generation", "Generation tools", "Create images, audio, video or other content with AI."),
    ("AI Training", "Training tools", "Train or refine AI models and prepare training data."),
    ("AI Analysis", "Analysis tools", "Understand existing content: tagging, captioning, OCR and transcription."),
    ("Productivity", "Productivity", "Organize, edit and manage everyday work."),
    ("Games", "Games", "Games retain their own gameplay interface."),
)
APP_CATEGORIES = tuple(key for key, _, _ in APPLICATION_GROUPS if key != "All categories")
CATEGORY_DESCRIPTIONS = {key: description for key, _, description in APPLICATION_GROUPS}
ROLE_LABELS = {
    "AI Agents": "AI agent", "LLM": "Language-model application",
    "Model Runners": "Model runner", "AI Generation": "Generation tool",
    "AI Training": "Training tool", "AI Analysis": "Analysis tool",
    "Productivity": "Productivity application", "Games": "Game",
}


@dataclass(frozen=True)
class ApplicationFilterState:
    category: str = "All categories"
    status: str = "All entries"
    query: str = ""


def select_category(state, category):
    """Navigation changes scope only; retain the existing status and search."""
    if category not in CATEGORY_DESCRIPTIONS:
        raise ValueError("unknown_application_category")
    return replace(state, category=category)


def matches_category(categories, category):
    return category == "All categories" or category in categories


def matching_catalog(entries, state):
    """A supplied catalog entry alone never proves an Installed/Available state."""
    if state.status != "All entries":
        return ()
    query = state.query.strip().casefold()
    return tuple(entry for entry in entries
        if matches_category(entry.categories, state.category)
        and (not query or query in " ".join((entry.title, entry.description, *entry.capabilities)).casefold()))


def application_roles(categories):
    roles = tuple(dict.fromkeys(ROLE_LABELS[key] for key in categories if key in ROLE_LABELS))
    return " / ".join(roles) if roles else "Application role not supplied"


def related_application_entries(entry, registry, catalog):
    """Resolve declared optional add-ons to unambiguous existing catalog details.

    Generic runtime/model requirements never become compatibility relationships.
    A configured registry is not proof of installation or executable support.
    """
    if registry is None:
        return ()
    result = []
    seen = set()
    for identity in entry.optional_addon_instances:
        instances = [item for item in registry.entries if item.installation_id == identity]
        if len(instances) != 1 or getattr(instances[0].kind, "value", instances[0].kind) != "Modules":
            continue
        targets = [item for item in catalog if item.component_id == instances[0].component_id]
        if len(targets) == 1 and targets[0].component_id != entry.component_id and targets[0].component_id not in seen:
            seen.add(targets[0].component_id)
            result.append(targets[0])
    return tuple(result)
