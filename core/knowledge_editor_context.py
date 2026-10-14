"""Explicit native paths for the original editor; never rebind global profiles."""
from dataclasses import dataclass
from pathlib import Path
from .character_database import CharacterDatabase
from .character_record_edit import CharacterRecordEditService
from .tag_identity_merge import TagIdentityMergeService


class SelectedTagIdentityMergeService(TagIdentityMergeService):
    """Original preview/transaction with selected pre-write admission."""
    def __init__(self, session, tags, categories, characters):
        session.require_character_authority()
        if tags is not session.tags or categories is not session.categories or characters is not session.characters:
            raise ValueError("Merge service must retain the exact selected native authorities.")
        self.editor_session = session
        super().__init__(tags, categories, characters)

    def apply(self, *args, **kwargs):
        return self.editor_session.run_native_operation(
            lambda: super(SelectedTagIdentityMergeService,self).apply(*args,**kwargs),
            kind="identity_merge")


def adopt_live_tag_authority(live, categories, foundation, database, services):
    """Original GUI adoption's model portion, shared with Category-only hosting."""
    categories.__dict__.update(database._categories.__dict__)
    foundation.__dict__.update(services["foundation_snapshot"].__dict__)
    database._categories=categories
    revision=getattr(live,"_relationship_signature_revision",0)
    live.__dict__.update(database.__dict__)
    live._relationship_signature_revision=max(revision,getattr(database,"_relationship_signature_revision",0))+1
    for name,value in services.items():
        if name=="relationship_workspace":value.tags=live
        if name.endswith("_service") and value is not None:
            value.tags=live
            value.workspace.tags=live
            if hasattr(value,"categories"):value.categories=categories
            if hasattr(value,"foundation"):value.foundation=foundation
    live._app_tag_alias_service=services["tag_alias_service"]
    live._app_tag_semantic_service=services["tag_semantic_service"]
    live._app_relationship_workspace=services["relationship_workspace"]


def _selected_character_write(method):
    def write(self, *args, **kwargs):
        return self.editor_session.run_native_operation(
            lambda: method(self, *args, **kwargs), kind="character_edit")
    return write


class SelectedCharacterRecordEditService(CharacterRecordEditService):
    """Original public transactions admitted by one selected owner."""
    def __init__(self, session, database, tag_database=None, categories=None):
        if database is not session.characters or tag_database is not session.tags or categories is not session.categories:
            raise ValueError("Character service must retain the exact selected native authorities.")
        self.editor_session = session
        super().__init__(database, tag_database, categories)

    create_source_from_name = _selected_character_write(CharacterRecordEditService.create_source_from_name)
    create_series = _selected_character_write(CharacterRecordEditService.create_series)
    create_character = _selected_character_write(CharacterRecordEditService.create_character)
    create_outfit = _selected_character_write(CharacterRecordEditService.create_outfit)
    update_series = _selected_character_write(CharacterRecordEditService.update_series)
    update_character = _selected_character_write(CharacterRecordEditService.update_character)
    update_outfit = _selected_character_write(CharacterRecordEditService.update_outfit)


class PreparedCharacterDatabase(CharacterDatabase):
    """Stock loader without its missing-file initialization side effect.

    The stock recover_interrupted=False flag prevents recovery but still calls
    save for absent files. Suppress only that constructor call; missing native
    files remain unavailable for editing until initialized by their own owner.
    """

    def __init__(self, directory):
        self._preparing = True
        try:
            super().__init__(directory, recover_interrupted=False)
        finally:
            self._preparing = False

    def save(self):
        if self._preparing:
            return
        if any(not p.exists() for p in (self.series_path, self.characters_path, self.outfits_path)):
            raise RuntimeError("Character Knowledge has not been initialized by its native owner.")
        return super().save()


@dataclass(frozen=True)
class KnowledgeEditorContext:
    defaults: Path
    user: Path
    categories: Path
    patterns: Path
    relationships: Path
    consumer_policies: Path
    standalone: bool = False
    app_root: Path | None = None

    @classmethod
    def original(cls):
        from .paths import (APP_ROOT, DEFAULTS_DIR, USER_DATA_DIR, CATEGORIES_PATH,
                            PATTERNS_PATH, RELATIONSHIPS_PATH, CONSUMER_POLICIES_PATH)
        return cls(DEFAULTS_DIR, USER_DATA_DIR, CATEGORIES_PATH, PATTERNS_PATH,
                   RELATIONSHIPS_PATH, CONSUMER_POLICIES_PATH, app_root=APP_ROOT)

    @classmethod
    def selected(cls, session):
        session.require_editor_ready()
        defaults, user = session.root / "data/defaults", session.root / "data/user"
        return cls(defaults, user, user / "categories.json", user / "patterns.json",
                   user / "relationships.json", user / "consumer_policies.json", True, session.root)
