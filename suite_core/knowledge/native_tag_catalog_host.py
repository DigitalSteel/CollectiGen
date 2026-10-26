"""Selected native catalog host; no application initialization or migration.

Bind an already-loaded canonical knowledge_pack leaf explicitly. Native identity
observation and exact selected recovery remain under canonical admission.
"""
from dataclasses import dataclass
from hashlib import sha256
from importlib import import_module
import json
from pathlib import Path
import sys

from suite_core.encyclopedia.publication import Publication
from suite_core.knowledge import Source, Snapshot
from suite_core.database_actions import CatalogRelease
from suite_core.knowledge.catalog_migration_preview import PackRevision
from .tag_transactions import TagCatalogHost

ADAPTER_INTERFACE_VERSION = 1


def bind_foundation_source_release(decoded, approved_source_snapshot: Snapshot) -> CatalogRelease:
    """Bind an independently approved source DTO to exact decoded wire metadata.

    The wire v1 proposal does not contain this DTO or its reconstruction recipe.
    The producer must separately supply qualified source evidence; a recipient
    publication is never its substitute. Native transaction review still checks
    source tags/categories against the exact default proposal before apply.
    This pure binding grants no enrollment, installation or closure qualification.
    """
    owner = sys.modules.get(type(decoded).__module__)
    release_owner = sys.modules.get(type(decoded).__module__.rpartition(".")[0] + ".default_catalog_release")
    if (owner is None or not owner.__name__.endswith(".knowledge_pack")
            or type(decoded) is not getattr(owner, "FoundationPack", None)
            or type(decoded.proposal) is not getattr(owner, "FoundationProposal", None)
            or release_owner is None
            or type(decoded.approved_release) is not getattr(release_owner, "ApprovedDefaultPackRelease", None)
            or type(approved_source_snapshot) is not Snapshot):
        raise TypeError("Exact decoded Foundation and independently approved source Snapshot required")
    decoded.proposal.__post_init__()
    approved = decoded.approved_release
    approved.__post_init__()
    if approved_source_snapshot.source.kind != "reviewed_portable":
        raise ValueError("Foundation source requires a reviewed_portable Snapshot, not recipient publication")
    if approved_source_snapshot.fingerprint != approved.snapshot_fingerprint:
        raise ValueError("Approved source Snapshot differs from Foundation wire fingerprint")
    release = CatalogRelease(PackRevision(approved.pack_id, approved.content_version, approved_source_snapshot),
                             approved.released_on, approved.whats_new, approved.source_ref)
    if release.fingerprint != approved.release_fingerprint:
        raise ValueError("Approved source release differs from Foundation wire fingerprint")
    return release


class NativeTagHostHold(RuntimeError):
    """A supported native owner interface or selected authority is unavailable."""


@dataclass(frozen=True)
class DefaultContentObservation:
    """Exact native evidence; fingerprints do not imply a release version."""
    installed_version: str | None
    tracking_schema: int | None
    tracking_fingerprint: str
    default_fingerprints: tuple
    version_reason: str
    approved_release: dict | None = None


class NativeTagCatalogAdapter:
    """Bind existing native APIs to an explicit selected catalog.

    No constructor reads a catalog or initializes core. Each operation requires
    existing regular native participants and canonical filesystem/admission guards.
    catalog_id and source_reference are explicitly approved host observations.
    Scope must cover the complete effective Category set for safe full-Tag merging.
    No callbacks supplied by untrusted plugins are executed here.
    """
    def __init__(self, root, *, native, catalog_id, source_reference, foundation_proposal=None):
        self.root = Path(root)
        if not self.root.is_absolute() or ".." in self.root.parts:
            raise ValueError("Select an exact absolute native catalog root")
        name = getattr(native, "__name__", "")
        package = name.rpartition(".")[0]
        if not package or name != package + ".knowledge_pack" or sys.modules.get(name) is not native:
            raise TypeError("Supply the already-loaded canonical knowledge_pack leaf")
        for value in (catalog_id, source_reference):
            if type(value) is not str or not value.strip():
                raise ValueError("Explicit approved catalog identity/source required")
        if foundation_proposal is not None and type(foundation_proposal) is not native.FoundationProposal:
            raise TypeError("Supply the native typed Foundation proposal")
        self.foundation_proposal = foundation_proposal
        self.native, self.package = native, package
        self.catalog_id, self.source_reference = catalog_id, source_reference
        self.directory_pin = None
        self.identity_binding = None

    def guard(self):
        # Check before RulePairAdmission could create an absent authority directory.
        self._paths()
        return self.native.RulePairAdmission(self.root / "data/user").admit()

    def _paths(self):
        for path in (self.root, self.root / "data/defaults", self.root / "data/user"):
            self.native._check_export_path(path)
            if not path.is_dir():
                raise NativeTagHostHold("Selected native catalog directory is absent")
        user = self.root / "data/user"
        stat = user.stat()
        pin = (stat.st_dev, stat.st_ino)
        if self.directory_pin is None:
            self.directory_pin = pin
        elif pin != self.directory_pin:
            raise NativeTagHostHold("Selected native catalog directory changed; explicitly bind again")
        for path in (*self.native._pack_authority_paths(self.root),
                     self.root / "data/defaults/tag_aliases.json"):
            self.native._check_export_path(path)
        if not (user / "tags.json").is_file():
            raise NativeTagHostHold("Select an existing native Tags participant; no automatic catalog creation")

    def _state(self):
        self._paths()
        admission = import_module(self.package + ".catalog_admission")
        state = admission.CatalogAdmissionState(self.root, self.native)
        observed = tuple(sorted(state.binding.items())) if state.enrolled else ()
        if state.enrolled and state.catalog_id != self.catalog_id:
            raise NativeTagHostHold("Selected native catalog identity differs from the approved reader")
        if self.identity_binding is None:
            self.identity_binding = observed
        elif self.identity_binding != observed:
            raise NativeTagHostHold("Selected native catalog incarnation changed; explicitly bind again")
        return state

    def validate(self):
        self._state()
        admission = import_module(self.package + ".catalog_admission")
        with admission.catalog_admission(self.root, self.native, role="host-validation"):
            if self.native._pending_pack_receipts(self.root):
                raise NativeTagHostHold("Native Pack recovery is pending; select the exact receipt before ordinary work")

    def _revision_parts(self):
        with self.guard():
            self.validate()
            read_paths = set(self.native._foundation_plan_paths(self.root, self.foundation_proposal))
            read_paths.add(self.root / "data/defaults/tag_aliases.json")
            authority = tuple((str(path.resolve()), self.native._file_fingerprint(path))
                for path in sorted(read_paths, key=lambda value: str(value).casefold()))
            receipts = []
            for path in sorted((self.root / "backups/knowledge-pack-import").glob("*/recovery.json")):
                _value, stamp = self.native._read_pack_receipt(path, self.root)
                receipts.append((path.relative_to(self.root).as_posix(), stamp[-1]))
            values = (self.catalog_id, self.directory_pin, authority, receipts)
            authority_revision = sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()
            recipe_revision = import_module(self.package + ".effective_tag_snapshot_reader").snapshot_reader_revision
            return authority_revision, recipe_revision(authority_revision)

    def revision(self):
        return self._revision_parts()[1]

    def capture(self, generation):
        with self.guard():
            self.validate()
            native_tags = import_module(self.package + ".tag_database").TagDatabase
            native_categories = import_module(self.package + ".category_registry").CategoryRegistry
            native_aliases = import_module(self.package + ".tag_alias_relationships").TagAliasRelationshipService
            capture = import_module(self.package + ".shared_tag_snapshot").capture_tag_snapshot
            prepare = import_module(self.package + ".effective_tag_snapshot_reader").prepare_effective_tag_snapshot_reader
            authority_revision, observed = self._revision_parts()  # pin before loading any participating reader
            tags = native_tags(self.root / "data/user/tags.json",
                               rule_pair_admission=self.native.RulePairAdmission(self.root / "data/user"))
            categories = native_categories(self.root / "data/defaults/categories.json",
                                           self.root / "data/user/categories.json")
            # Qualify persisted interpretation independently of mutation policy.
            # A supported read can coexist with a store's general write hold.
            storage = import_module(self.package + ".tag_storage_decode")
            documents = [
                (self.root / "data/user/tags.json", "tag", native_tags, tags._loaded_bytes),
                (self.root / "data/user/categories.json", "category", native_categories,
                 categories._loaded_user_bytes),
            ]
            for name, kind, owner in (
                    ("foundation_tags.json", "tag", native_tags),
                    ("community_tags.json", "tag", native_tags),
                    ("promoted_tags.json", "tag", native_tags),
                    ("categories.json", "category", native_categories)):
                path = self.root / "data/defaults" / name
                documents.append((path, kind, owner, path.read_bytes() if path.exists() else None))
            for _path, kind, owner, raw in documents:
                if raw is None:
                    continue
                # A new codec's finish evaluates format interpretation only:
                # header, envelope, duplicates and record inventory. It has no
                # edits, store write policy, lifecycle transition or new authority.
                codec = storage.KnowledgeLifecycleCodec(kind, owner.SCHEMA_VERSION, owner.ENROLLED_SCHEMA_VERSION)
                payload, _records = storage.decode_tag_storage(raw.decode("utf-8"), None, lifecycle_codec=codec)
                if type(payload) is not dict:
                    raise NativeTagHostHold("Selected Tag or Category document must be an object; retain the original data.")
                if codec.envelope is not None:
                    if kind == "category" and _path == self.root / "data/user/categories.json":
                        ids = tuple(categories.categories)
                    else:
                        key = "tags" if kind == "tag" else "categories"
                        ids = tuple(row["id"] for row in payload.get(key, ()) if isinstance(row, dict))
                    codec._validate_current(codec.envelope, ids,
                        removed_ids=payload.get("removed_tag_ids", ()), redirects=payload.get("merged_tag_ids", {}))
                if codec.write_hold:
                    raise NativeTagHostHold("Selected Tag or Category format is unsupported or unqualified for publication; retain the original data.")
            tracking_path = self.root / "data/user/default_knowledge_state.json"
            if tracking_path.exists():
                tracking = self.native._read_payload_checked(tracking_path)
                state_schema = tracking.get("schema_version", 1)
                current_state_schema = import_module(self.package + ".default_sync")._STATE_SCHEMA_VERSION
                if type(state_schema) is not int or not 1 <= state_schema <= current_state_schema:
                    raise NativeTagHostHold("Selected synchronization tracking format is unsupported; retain the original data.")
            reader = prepare(tags=tags, defaults_dir=self.root / "data/defaults",
                             revision=self.revision, expected_revision=observed)
            aliases = native_aliases(reader.tags, default_path=self.root / "data/defaults/relationships.json",
                                     user_path=self.root / "data/user/relationships.json")
            source = Source(self.source_reference, observed, authority_revision, tuple(sorted(categories.categories)))
            snapshot = capture(tags=tags, categories=categories, aliases=aliases,
                               relationships=aliases.workspace.effective, source=source, revision=self.revision,
                               effective_reader=reader)
            return Publication(self.catalog_id, generation, snapshot)

    def plan(self, pack, *, foundation_proposal=None, approved_default_release=None):
        with self.guard():
            self.validate()
            plan = self.native.plan_knowledge_pack_import(self.root, pack,
                foundation_proposal=foundation_proposal if foundation_proposal is not None else self.foundation_proposal,
                approved_default_release=approved_default_release)
            return self.native.review_foundation_import_plan(self.root, plan)

    def apply(self, pack, plan):
        with self.guard():
            self.validate()
            return self.native.import_knowledge_pack(self.root, pack, plan=plan)

    def review_completed_import(self, receipt):
        """Review one explicitly supplied enrolled user import; never discover data."""
        with self.guard():
            state = self._state()
            if not state.enrolled:
                raise NativeTagHostHold("Completed import requires the selected enrolled catalog")
            return self.native.review_completed_knowledge_pack_import(self.root, receipt,
                catalog_identity_binding=self.identity_binding)

    def undo_completed_import(self, review):
        with self.guard():
            self._state()
            if review.catalog_identity_binding != self.identity_binding:
                raise NativeTagHostHold("Completed review belongs to another selected catalog")
            return self.native.undo_completed_knowledge_pack_import(self.root, review)

    def observe_completed_inverse(self, receipt):
        """Validate a selected durable B1 receipt without executing recovery."""
        with self.guard():
            self._state()
            admission = import_module(self.package + ".catalog_admission")
            with admission.selected_completed_pack_inverse(self.root, self.native,
                    binding=self.identity_binding, receipt=receipt):
                return self.native._read_pack_receipt(Path(receipt), self.root)[1][-1]

    def recover_completed_inverse(self, receipt):
        with self.guard():
            self._state()
            return self.native.recover_completed_knowledge_pack_inverse(self.root, receipt,
                catalog_identity_binding=self.identity_binding)

    def recover(self, receipt):
        """Recover one exact selected legacy or identity-bound enrolled import.

        Native preflight, backup checks, before/after stale guards and writes stay
        in knowledge_pack. Multiple pending operations hold rather than broadening
        the caller's selected recovery scope.
        """
        with self.guard():
            state = self._state()  # Recovery may enter while ordinary work holds.
            if state.enrolled:
                return self.native.recover_selected_knowledge_pack_import(
                    self.root, receipt, catalog_identity_binding=self.identity_binding)
            receipt = Path(receipt)
            value, _stamp = self.native._read_pack_receipt(receipt, self.root)
            if type(value.get("schema_version")) is not int or value["schema_version"] != 2:
                raise NativeTagHostHold("Select a supported native schema-2 import recovery receipt")
            if value["status"] == "complete":
                raise NativeTagHostHold("Completed import requires explicit Undo, not interrupted recovery")
            pending = self.native._pending_pack_receipts(self.root)
            if value["status"] == "restored":
                if pending:
                    raise NativeTagHostHold("Another pending recovery requires its own selection")
                prepared = self.native._prepare_pack_restore(receipt, value, self.root)
                if any((row[4][-1] if row[4] is not None else "missing") != row[1] for row in prepared):
                    raise NativeTagHostHold("Previously restored native content changed; recovery is not a new Undo")
                return False
            if len(pending) != 1 or pending[0][0].absolute() != receipt.absolute():
                raise NativeTagHostHold("Select the sole pending native import; multiple operations need native owner review")
            result = self.native.recover_interrupted_knowledge_pack_imports(self.root)
            if tuple(Path(p).absolute() for p in result) != (receipt.absolute(),):
                raise NativeTagHostHold("Native recovery outcome differs from the selected receipt")
            return True

    def plan_default_release(self, pack, base_release, available_release, installed, *, foundation_proposal=None):
        with self.guard():
            self.validate()
            release_type = import_module(self.package + ".default_catalog_release").ApprovedDefaultPackRelease
            approved = release_type(available_release.revision.pack_id,
                available_release.revision.content_version, available_release.released_on,
                available_release.whats_new, available_release.source_ref,
                available_release.revision.snapshot.fingerprint, available_release.fingerprint,
                base_release.fingerprint if installed else None)
            plan = self.native.plan_knowledge_pack_import(self.root, pack, approved_default_release=approved,
                foundation_proposal=foundation_proposal if foundation_proposal is not None else self.foundation_proposal)
            return self.native.review_foundation_import_plan(self.root, plan)

    def default_content(self, pack_id=None):
        with self.guard():
            self.validate()
            fingerprints = self.native._authority_fingerprints(self.root)
            defaults = tuple((Path(path).relative_to(self.root).as_posix(), digest)
                             for path, digest in fingerprints if Path(path).is_relative_to(self.root / "data/defaults"))
            tracking = self.root / "data/user/default_knowledge_state.json"
            digest = dict(fingerprints).get(str(tracking.resolve()), "missing")
            value = self.native._read_payload_checked(tracking) if tracking.exists() else {}
            schema = value.get("schema_version")
            release = self.native.observe_approved_default_pack_release(self.root, pack_id) if pack_id else None
            return DefaultContentObservation(release.get("content_version") if release else None,
                schema if type(schema) is int else None, digest, defaults,
                "Committed approved native Pack state" if release else
                "No approved Pack ID/content-version commit binding for the explicit selected Pack", release)

    def host(self):
        return TagCatalogHost(self.root, self.guard, self.validate, self.plan, self.apply,
                              self.recover, self.capture, self.revision, self.native._check_export_path, self.plan_default_release)
