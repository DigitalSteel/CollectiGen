"""Selected native-catalog editor seam using original writers, never a profile.

This module deliberately supplies no replacement Tag Details UI. Its request
API accepts the original editor's validated immutable save requests. Category
drafts use the original CategoryWorkspaceSession and can be handed to its Qt UI.
Exported publications are observations, never editing authority.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace

from .catalog_admission import catalog_admission
from .pattern_registry import PatternRegistry
from .tag_database import TagRecord
from .tag_order_workspace import CategoryWorkspaceSession
from .tag_record_save import (
    TagDetailsSaveRequest, TagSaveAuthorities, load_tag_save_authority,
    save_tag_details, source_stamp,
)
from .tag_collection import normalize_tag
from .knowledge_editor_context import PreparedCharacterDatabase,adopt_live_tag_authority
from suite_core.knowledge.native_tag_catalog_host import NativeTagCatalogAdapter
from suite_core.knowledge.tag_lookup import TagLookup


class EncyclopediaEditingHold(RuntimeError):
    """The selected saved authority must be reviewed before editing continues."""


class _SelectedCategorySession(CategoryWorkspaceSession):
    """Keep selected identity pinned through the original native writer."""

    def __init__(self, owner, *args, **kwargs):
        self.editor_owner = owner
        self.editor_adapter = owner.adapter
        super().__init__(*args, **kwargs)

    def save(self, **kwargs):
        with self.editor_adapter.guard():
            with catalog_admission(self.editor_adapter.root, self.editor_adapter.native,
                                   role="encyclopedia-category-save"):
                self.editor_adapter.validate()
                self.editor_owner._require_ready(category_writer=self)
                try:
                    return super().save(**kwargs)
                except Exception:
                    self.editor_owner.reload_required = True
                    raise

    def prepare_tag_retirement(self, *args, **kwargs):
        self.editor_owner.require_character_authority()
        self.editor_owner.require_editor_ready()
        return super().prepare_tag_retirement(*args, **kwargs)

    def preview_tag_removal(self, *args, **kwargs):
        self.editor_owner.require_character_authority()
        return super().preview_tag_removal(*args, **kwargs)

    def remove_tags(self, *args, **kwargs):
        self.editor_owner.require_character_authority()
        return super().remove_tags(*args, **kwargs)


class EncyclopediaEditingSession:
    """Explicit one-catalog owner; construction reads, never recovers or enrolls.

    Call on one serial owner. The shell supplies worker/GUI dispatch and close
    holds. No AppState, global path rebinding, executor or publication is created.
    A save error is not permission to retry: reload saved authority explicitly.
    """

    @classmethod
    def open(cls, adapter):
        if type(adapter) is not NativeTagCatalogAdapter:
            raise EncyclopediaEditingHold(
                "Editing requires an explicitly selected native Tag Database; "
                "exported snapshots and memory views are read-only."
            )
        from . import knowledge_pack
        if adapter.native is not knowledge_pack:
            raise EncyclopediaEditingHold("Use the original native catalog owner.")
        self = cls.__new__(cls)
        self.adapter = adapter
        self.root = adapter.root
        self.busy = False
        self.reload_required = False
        self._closed = False
        with self._admission():
            if not adapter._state().enrolled:
                raise EncyclopediaEditingHold("Connect and enroll this catalog before editing.")
            self._load()
        return self

    @contextmanager
    def _admission(self):
        with self.adapter.guard():
            with catalog_admission(self.root, self.adapter.native,
                                   role="encyclopedia-editing"):
                self.adapter.validate()
                yield

    def _source_paths(self):
        paths = set(self.adapter.native._pack_authority_paths(self.root))
        paths.update((self.root / "data/defaults/tag_aliases.json",
                      self.root / "data/user/catalog_identity.json"))
        paths.update(self.root / "data/defaults" / name for name in (
            "categories.json", "patterns.json", "modifiers.json", "concepts.json",
            "relationships.json", "consumer_policies.json", "automation_rules.json"))
        paths.update(self.root / "data/user" / name for name in (
            "categories.json", "patterns.json", "relationships.json",
            "consumer_policies.json", "automation_rules.json"))
        return tuple(sorted(paths, key=str))

    def _stamp(self):
        return tuple((p, source_stamp(p)) for p in self._source_paths())

    def _load(self):
        self._require_recovery_clear()
        before = self._stamp()
        defaults, user = self.root / "data/defaults", self.root / "data/user"
        for path in self._source_paths():
            self.adapter.native._check_export_path(path)
        self.authorities = TagSaveAuthorities(
            defaults / "categories.json", user / "categories.json",
            defaults / "modifiers.json", defaults / "concepts.json",
            defaults / "relationships.json", user / "relationships.json",
            self._bytes(defaults / "relationships.json"),
            self._bytes(user / "relationships.json"),
            tuple((p, source_stamp(p)) for p in (
                defaults / "categories.json", user / "categories.json",
                defaults / "modifiers.json", defaults / "concepts.json")),
        )
        self.tags, self.services = load_tag_save_authority(
            user / "tags.json", self.authorities,
            rule_pair_admission=self.adapter.native.RulePairAdmission(user),
        )
        self.categories = self.tags._categories
        self._rule_pair_admission = self.tags.rule_pair_admission
        self.patterns = PatternRegistry(defaults / "patterns.json", user / "patterns.json")
        self.characters = PreparedCharacterDatabase(user)
        self.category_session = _SelectedCategorySession(
            self, self.tags, self.categories, self.patterns, self.characters,
            default_relationship_path=defaults / "relationships.json",
            user_relationship_path=user / "relationships.json",
            foundation=self.services["foundation_snapshot"],
            publish_live_authority=False,
        )
        if before != self._stamp():
            raise EncyclopediaEditingHold("Tag Database changed while loading editor authority.")
        self._loaded_stamp = before
        self._pending_native_operation = None
        self._pending_tag_result = None
        self._category_adoption_pending = False
        self.reload_required = False

    @staticmethod
    def _bytes(path):
        return path.read_bytes() if path.exists() else None

    @property
    def has_changes(self):
        return self.category_session.has_changes

    @property
    def closed(self):
        return self._closed

    @property
    def write_hold(self):
        return self.tags.lifecycle_write_hold or self.categories.lifecycle_write_hold

    def require_editor_ready(self):
        with self._admission():
            self._require_ready()

    def _require_recovery_clear(self):
        import json
        for namespace in ("category-workspace", "tag-order", "tag-autosave",
                "relationship-allowed-dimensions", "character-record-edit",
                "character-database-save", "character-tag-bridge", "tag-identity-merge"):
            root = self.root / "data/user/backups" / namespace
            self.adapter.native._check_export_path(root)
            for receipt in sorted(root.glob("*/recovery.json")):
                self.adapter.native._check_export_path(receipt)
                try:
                    payload = json.loads(receipt.read_text(encoding="utf-8"))
                except (OSError, ValueError) as error:
                    raise EncyclopediaEditingHold("Category recovery receipt needs review.") from error
                terminal = {"complete", "restored"}
                if namespace in {"character-record-edit", "character-database-save"}:
                    terminal.add("stale")  # native rejection before any write
                if not isinstance(payload, dict) or payload.get("status") not in terminal:
                    raise EncyclopediaEditingHold("Pending or unknown Category recovery holds editing; review recovery explicitly.")

    def _require_ready(self, *, category_writer=None):
        self._require_recovery_clear()
        if category_writer is not None and category_writer is not self.category_session:
            raise EncyclopediaEditingHold("Category writer belongs to an obsolete selected session.")
        own_category_save = (category_writer is self.category_session
                             and getattr(self, "_category_save_running", False))
        if self._closed or (self.busy and not own_category_save) or self.reload_required:
            raise EncyclopediaEditingHold("Close, saving or recovery holds this editor. Reload saved authority.")
        if self._stamp() != self._loaded_stamp:
            raise EncyclopediaEditingHold("Tag Database changed after opening. Reload and review it again.")
        if self.write_hold:
            raise EncyclopediaEditingHold("Native lifecycle authority holds edits; review its saved evidence.")

    def require_character_authority(self):
        self.require_editor_ready()
        if any(not p.exists() for p in (self.characters.series_path,
                self.characters.characters_path, self.characters.outfits_path)):
            raise EncyclopediaEditingHold("Review and initialize Character Knowledge before reference-sensitive removal or merge.")

    def run_native_operation(self, operation, *, kind, initialization_approved=False):
        """Admit an existing native writer; retain its result for exact GUI adoption."""
        from threading import get_ident
        if kind not in {"modifier_policy", "character_setup", "character_edit", "identity_merge", "identity_restore"}:
            raise ValueError("Unknown native selected operation.")
        if self.busy and getattr(self, "_native_operation_thread", None) == get_ident():
            if kind != self._native_operation_kind:
                raise EncyclopediaEditingHold("A different native writer cannot join this transaction.")
            return operation()
        with self._admission():
            self._require_ready()
            if self.has_changes:
                raise EncyclopediaEditingHold("Finish the Category draft before another native Knowledge write.")
            if kind in {"character_edit", "identity_merge"} or kind == "character_setup" and not initialization_approved:
                self.require_character_authority()
            allowed = {self.tags.path, self.authorities.relationship_user}
            if kind in {"identity_merge", "identity_restore"}:
                allowed.add(self.authorities.category_user)
            if kind in {"character_setup", "character_edit", "identity_merge"}:
                allowed.update((self.characters.series_path, self.characters.characters_path,
                                self.characters.outfits_path))
            before = self._loaded_stamp
            self.busy = True
            self._native_operation_thread = get_ident()
            self._native_operation_kind = kind
            try:
                result = operation()
                after = self._stamp()
                if any(source_stamp(p) != stamp for p, stamp in before if p not in allowed):
                    raise EncyclopediaEditingHold("Unrelated catalog authority changed during the native operation.")
                self._pending_native_operation = (kind, result, after)
                self.reload_required = True
                return result
            except Exception:
                self.reload_required = True
                raise
            finally:
                self.busy = False
                self._native_operation_thread = None

    def adopt_native_operation(self, page, result):
        with self._admission():
            self._require_recovery_clear()
            pending = getattr(self, "_pending_native_operation", None)
            if self.closed or pending is None or pending[1] is not result or self._stamp() != pending[2]:
                raise EncyclopediaEditingHold("The exact native saved result is no longer current.")
            if page is not None and (page.tags is not self.tags or page.categories is not self.categories):
                raise EncyclopediaEditingHold("The selected live authority changed before native adoption.")
            authorities = replace(self.authorities,
                relationship_default_bytes=self._bytes(self.authorities.relationship_default),
                relationship_user_bytes=self._bytes(self.authorities.relationship_user),
                source_stats=tuple((p, source_stamp(p)) for p, _ in self.authorities.source_stats))
            database, services = load_tag_save_authority(self.tags.path, authorities,
                rule_pair_admission=self._rule_pair_admission)
            foundation=page.foundation if page is not None else self.services["foundation_snapshot"]
            if page is not None:page._adopt_tag_save_authority(database,services)
            else:adopt_live_tag_authority(self.tags,self.categories,foundation,database,services)
            if all(p.exists() for p in (self.characters.series_path,
                    self.characters.characters_path, self.characters.outfits_path)):
                self.characters.load()
            self.services = services
            self.services["foundation_snapshot"] = foundation
            self.authorities = authorities
            fresh = _SelectedCategorySession(self, self.tags, self.categories, self.patterns,
                self.characters, default_relationship_path=authorities.relationship_default,
                user_relationship_path=authorities.relationship_user,
                foundation=foundation, publish_live_authority=False)
            self.category_session.__dict__.update(fresh.__dict__)
            if self._stamp() != pending[2]:
                raise EncyclopediaEditingHold("Catalog authority changed during native adoption.")
            self._loaded_stamp = pending[2]
            self._pending_native_operation = None
            self.reload_required = False

    def reload(self, *, discard=False):
        if self._closed or self.busy:
            raise EncyclopediaEditingHold("Wait for the editor operation before reloading.")
        if self.has_changes and not discard:
            raise EncyclopediaEditingHold("Discard the pending Category draft explicitly before reloading.")
        with self._admission():
            self._load()
        return self

    def discard(self):
        """Discard only this session's unsaved Category draft; never undo saves."""
        if self.busy:
            raise EncyclopediaEditingHold("Wait for the verified save before discarding.")
        self.category_session.reset()

    def close(self, *, discard=False):
        if self.busy or (self.has_changes and not discard):
            return False
        if discard:
            self.discard()
        self._closed = True
        return True

    def create_category(self, name, *, parent_id=None, **details):
        """Stage only; explicit save_categories owns durable completion."""
        with self._admission():
            self._require_ready()
            return self.category_session.create_category(name, parent_id=parent_id, **details)

    def update_category(self, category_id, **details):
        with self._admission():
            self._require_ready()
            return self.category_session.update_category(category_id, **details)

    def save_categories(self):
        with self._admission():
            self._require_ready()
            self.busy = True
            self._category_save_running = True
            try:
                result = self.category_session.save(preserve_history=True)
                self._load()
                return result
            except Exception:
                self.reload_required = True
                raise
            finally:
                self.busy = False
                self._category_save_running = False

    def adopt_category_workspace_save(self, workspace, *, defer_ready=False):
        """Observe an original widget's verified save; never replay its writer.

        Called by the shell after the original saved signal, on its GUI owner.
        The widget retains its session and native objects; no hidden recovery or
        object replacement can invalidate that widget's completion callbacks.
        """
        with self._admission():
            if self.closed or self.reload_required:
                raise EncyclopediaEditingHold("Prior adoption or recovery holds this selected editor.")
            if (workspace.tags is not self.tags or workspace.categories is not self.categories
                    or workspace.patterns is not self.patterns
                    or workspace.session.live_tags is not self.tags
                    or getattr(workspace.session, "editor_adapter", None) is not self.adapter
                    or workspace.has_unsaved_changes()):
                raise EncyclopediaEditingHold("Wait for the exact Category Workspace saved outcome.")
            if self.tags.path.read_bytes() != self.tags._loaded_bytes:
                raise EncyclopediaEditingHold("Saved Tags changed before Category delivery.")
            if source_stamp(self.categories.user_path) != self.categories._loaded_source_fingerprints[self.categories.user_path]:
                raise EncyclopediaEditingHold("Saved Categories changed before delivery.")
            writer_paths = {self.tags.path, self.categories.user_path,
                            self.patterns.user_path, self.authorities.relationship_user}
            if any(source_stamp(p) != stamp for p, stamp in self._loaded_stamp
                   if p not in writer_paths):
                raise EncyclopediaEditingHold("Other catalog sources changed before Category delivery.")
            self.category_session = workspace.session
            self.authorities = replace(
                self.authorities,
                relationship_default_bytes=self._bytes(self.authorities.relationship_default),
                relationship_user_bytes=self._bytes(self.authorities.relationship_user),
                source_stats=tuple((p, source_stamp(p)) for p, _ in self.authorities.source_stats),
            )
            _, self.services = load_tag_save_authority(
                self.tags.path, self.authorities, prepared_database=self.tags,
                rule_pair_admission=self.tags.rule_pair_admission,
            )
            self.tags._categories = self.categories
            self._loaded_stamp = self._stamp()
            self.reload_required = bool(defer_ready)
            self._category_adoption_pending = bool(defer_ready)

    def finish_category_adoption(self):
        with self._admission():
            self._require_recovery_clear()
            if self.closed or not getattr(self, "_category_adoption_pending", False) or self._stamp() != self._loaded_stamp:
                raise EncyclopediaEditingHold("Category adoption is no longer current.")
            self._category_adoption_pending = False
            self.reload_required = False

    def save_tag_request(self, request, *, defer_adoption=False):
        """Save an original Tag Details request bound to this exact authority.

        Name/category checks supplement the native writer. Rename and relationship
        decisions still belong to the original editor and its canonical services.
        No Tag form or parallel validation/serialization engine lives here.
        """
        if type(request) is not TagDetailsSaveRequest:
            raise TypeError("Original immutable Tag Details save request required.")
        with self._admission():
            self._require_ready()
            if self.has_changes:
                raise EncyclopediaEditingHold("Save or discard Category changes before editing a Tag.")
            expected_path = self.root / "data/user/tags.json"
            if (request.record.path != expected_path
                    or request.record.expected_bytes != self.tags._loaded_bytes
                    or request.authorities != self.authorities
                    or request.record.rule_pair_admission != self.tags.rule_pair_admission):
                raise EncyclopediaEditingHold("Tag request belongs to different or stale editor authority.")
            import json
            raw_record = json.loads(request.record.record_json)
            if not isinstance(raw_record, dict) or not str(raw_record.get("display") or "").strip():
                raise ValueError("Enter the preferred SDXL Tag name.")
            record = TagRecord.from_dict(raw_record)
            if self.categories.get(record.category) is None:
                raise ValueError("Choose an existing category before saving.")
            if request.record.created and self.tags.lifecycle_envelope is not None:
                # Single-record requests do not carry a reviewed lifecycle
                # transition. Do not write an ID outside the enrolled envelope.
                raise EncyclopediaEditingHold("New Tag identity requires the original reviewed lifecycle transition.")
            conflict = self.tags.conflicting_name((record.display,), exclude_id=record.id)
            alias_service = self.services["tag_alias_service"]
            previous = self.tags.tags.get(record.id)
            renamed = bool(previous and normalize_tag(previous.display) != normalize_tag(record.display))
            if request.record.created or renamed:
                # Native defaults may own names absent from the mutable user
                # rows. Reuse the canonical effective reader for this check;
                # its immutable output is never used as writable Tag payload.
                effective = TagLookup(self.adapter.capture(1).snapshot).resolve(record.display)
                if (any(owner != record.id for owner in effective.owner_ids)
                        or effective.tag is not None and effective.tag.id != record.id):
                    raise ValueError("This name already identifies another stable SDXL Tag.")
                if effective.status in {"unavailable", "retired", "ambiguous"}:
                    raise EncyclopediaEditingHold("Effective Tag identity is unavailable, retired or ambiguous; review it before saving this name.")
            if alias_service is None and (request.record.created or renamed):
                raise EncyclopediaEditingHold("Canonical Other Names authority is unavailable; review it before creating or renaming a Tag.")
            if alias_service is not None:
                resolution = alias_service.resolve(record.display)
                if any(owner != record.id for owner in resolution.owner_ids):
                    raise ValueError("This name already identifies another stable SDXL Tag.")
                if (request.record.created or renamed) and (resolution.retired_ids or resolution.unavailable_ids):
                    raise EncyclopediaEditingHold("Review the retired or unavailable Tag identity before using this name.")
            if conflict:
                raise ValueError("This name already identifies another stable SDXL Tag.")
            if renamed:
                # The full original UI retains the old name through canonical
                # aliases and reviewed image handoff. Refuse a raw record rename.
                changes = json.loads(request.changes_json)
                if (request.mode != "aliases" or not any(
                        normalize_tag(a) == normalize_tag(previous.display)
                        for a in changes.get("aliases", ()))):
                    raise EncyclopediaEditingHold("Canonical rename requires the original Other Names review.")
            if request.record.created and self.tags.removed_identity_id(record.display):
                raise EncyclopediaEditingHold("Review the removed Tag identity before creating it again.")
            self.busy = True
            try:
                # A full result is required before editor authority can refresh.
                result = save_tag_details(replace(request, prepare_live_delta=False))
                if defer_adoption:
                    self._pending_tag_result = result
                    self.reload_required = True
                else:
                    self._load()
                return result
            except Exception:
                self.reload_required = True
                raise
            finally:
                self.busy = False

    def adopt_tag_editor_save(self, page, result):
        """Finish selected save after original GUI adoption, retaining live identities."""
        with self._admission():
            self._require_recovery_clear()
            if self.closed or getattr(self, "_pending_tag_result", None) is not result:
                raise EncyclopediaEditingHold("No matching selected Tag save awaits adoption.")
            if (page.tags is not self.tags or page.categories is not self.categories
                    or page.category_workspace.session is not self.category_session):
                raise EncyclopediaEditingHold("The original editor no longer owns this selected authority.")
            writer_paths = {self.tags.path, self.authorities.relationship_user}
            if any(source_stamp(p) != stamp for p, stamp in self._loaded_stamp if p not in writer_paths):
                raise EncyclopediaEditingHold("Other catalog sources changed before Tag adoption.")
            if self.tags.path.read_bytes() != result.database._loaded_bytes:
                raise EncyclopediaEditingHold("Saved Tag bytes changed before adoption.")
            if self._bytes(self.authorities.relationship_user) != result.relationship_bytes:
                raise EncyclopediaEditingHold("Saved relationships changed before adoption.")
            self.services = result.services
            self.services["foundation_snapshot"] = page.foundation
            self.authorities = replace(self.authorities,
                relationship_default_bytes=self._bytes(self.authorities.relationship_default),
                relationship_user_bytes=result.relationship_bytes,
                source_stats=tuple((p,source_stamp(p)) for p,_ in self.authorities.source_stats))
            fresh = _SelectedCategorySession(self, self.tags, self.categories, self.patterns, self.characters,
                default_relationship_path=self.authorities.relationship_default,
                user_relationship_path=self.authorities.relationship_user,
                foundation=page.foundation, publish_live_authority=False)
            self.category_session.__dict__.update(fresh.__dict__)
            self._loaded_stamp = self._stamp()
            self._pending_tag_result = None
            self.reload_required = False
