"""Explicit standalone composition of existing native Tag connection/transactions.

Opening an enrolled catalog reads its effective view. Import, connection saving,
initialization and publication require separate explicit reviewed operations.
No Manager process, global profile, source discovery or network is required.
"""
from dataclasses import dataclass
from pathlib import Path

from core import tag_catalog_connection as connection
from suite_core.catalog_identity_candidate import decode_identity
from suite_core.database_actions import DatabaseAction
from suite_core.knowledge.native_tag_catalog_host import NativeTagCatalogAdapter
from suite_core.encyclopedia.publication import read
from suite_core.manager.tag_database_controller import empty_default_layer


@dataclass(frozen=True)
class EncyclopediaCatalogView:
    root: Path
    identity: object
    publication: object
    stamp: tuple
    adapter: object | None = None
    read_binding: object | None = None
    read_only: bool = False
    candidate_only: bool = False

    def __post_init__(self):
        from core.encyclopedia_read_admission import validate_read_binding
        if type(self.read_only) is not bool or type(self.candidate_only) is not bool:
            raise connection.TagConnectionHold("Exact read delivery flags required")
        if self.candidate_only and not self.read_only:
            raise connection.TagConnectionHold("Candidate delivery requires read-only admission")
        if self.read_only:
            if self.adapter is not None:
                raise connection.TagConnectionHold("Read-only delivery cannot expose a native writer")
            binding = validate_read_binding(self.read_binding, root=self.root,
                identity=self.identity, publication=self.publication, stamp=self.stamp)
            if self.candidate_only != binding.candidate_only:
                raise connection.TagConnectionHold("Candidate binding cannot qualify production delivery")
        elif self.read_binding is not None:
            raise connection.TagConnectionHold("Read binding cannot grant writer authority")


@dataclass(frozen=True)
class PreparedDefaultConnectionReview:
    package: object
    connection_review: object


def prepare_downloaded_default_review(acquired, *, destination, trust_bytes, component,
        acquisition_cache_root, settings_path, catalog_root, should_cancel=None):
    """Worker-only stock package verification followed by read-only review."""
    from core.tag_default_package import prepare_tag_default_package
    from suite_core.discovery.component_staging import verify_directory
    target = connection._path(Path(catalog_root))
    cache = connection._path(Path(destination))
    settings = connection._path(Path(settings_path))
    if cache.is_relative_to(target) or target.is_relative_to(cache) or settings.is_relative_to(cache):
        raise connection.TagConnectionHold('Keep disposable source cache separate from the catalog and settings')
    package = prepare_tag_default_package(acquired, destination, should_cancel,
        trust_bytes=trust_bytes, component=component, acquisition_cache_root=acquisition_cache_root)
    review = connection.prepare_connection(settings, package.source_directory, target, False)
    if (review.source_pin[1] != component.location.manifest_sha256
            or review.source_pin[3] != package.release.fingerprint):
        raise connection.TagConnectionHold('Downloaded source changed before connection review')
    verify_directory(package.source_directory, component.location.manifest_sha256,
        component.component_id, component.software_version)
    if should_cancel is not None and should_cancel():
        raise InterruptedError('Downloaded source review cancelled; retain the prepared cache')
    return PreparedDefaultConnectionReview(package, review)


def require_selected_editor_binding(adapter, publication, directory_pin):
    """Revalidate the accepted reader binding; never adopt a replacement."""
    from suite_core.encyclopedia.publication import Publication
    if (type(adapter) is not NativeTagCatalogAdapter or type(publication) is not Publication
            or directory_pin is None or adapter.catalog_id!=publication.catalog_id
            or publication.snapshot.source.kind!='effective_native'
            or publication.snapshot.source.completeness!='complete'):
        raise connection.TagConnectionHold('Accepted complete native reader binding required')
    with adapter.guard():
        adapter.validate()
        if adapter.directory_pin!=directory_pin or adapter.revision()!=publication.snapshot.source.revision:
            raise connection.TagConnectionHold('Selected catalog changed; reopen before preparing editors')


def prepare_selected_editor(root, owner, *, settings_path, publication, directory_pin, job=None):
    """Worker-only original selected session and Tag host preparation."""
    from core import knowledge_pack
    from core.encyclopedia_editing import EncyclopediaEditingSession
    from core.settings import AppSettings
    from qt_app.tag_details_host import prepare_tag_details_host
    root = connection._path(Path(root))
    adapter = NativeTagCatalogAdapter(root,native=knowledge_pack,
        catalog_id=publication.catalog_id,source_reference='Selected standalone Tag Database')
    adapter.directory_pin=directory_pin
    require_selected_editor_binding(adapter,publication,directory_pin)
    session = EncyclopediaEditingSession.open(adapter)
    previous_knowledge=owner.knowledge
    try:
        owner.knowledge = session.tags
        settings = AppSettings.load(settings_path)
        preparation = prepare_tag_details_host(session,owner,job)
        require_selected_editor_binding(adapter,publication,directory_pin)
        return session,preparation,settings
    except Exception:
        session.close()
        raise
    finally:
        owner.knowledge=previous_knowledge


def prepare_organizer_handoff(adapter, publication, stable_id, *, request_id, previous_display=None):
    """Worker-only fresh selected revision; receiver still admits native authority.

    A former-name choice is an explicit correction suggestion, never a historical
    rename witness. No image query/write or catalog adoption occurs here.
    """
    from core.organizer_encyclopedia_handoffs import MatchingImagesRequest,CanonicalRenameRequest,HandoffHold
    from core.tag_collection import normalize_tag
    from suite_core.encyclopedia.publication import Publication
    if type(adapter) is not NativeTagCatalogAdapter or type(publication) is not Publication:
        raise HandoffHold('original_selected_native_authority_required')
    snapshot=publication.snapshot
    if (publication.catalog_id!=adapter.catalog_id or snapshot.source.kind!='effective_native'
            or snapshot.source.completeness!='complete'):
        raise HandoffHold('selected_complete_native_publication_required')
    with adapter.guard():
        adapter.validate()
        revision=adapter.revision()
        if snapshot.source.revision!=revision:
            raise HandoffHold('refresh_selected_native_catalog_before_handoff')
        current=adapter.capture(publication.generation)
        if current.catalog_id!=publication.catalog_id or current.snapshot.source.revision!=revision or adapter.revision()!=revision:
            raise HandoffHold('selected_native_catalog_changed_during_handoff')
        tag=next((tag for tag in current.snapshot.tags if tag.id==stable_id),None)
        if tag is None:raise HandoffHold('unresolved_selected_stable_identity')
        if previous_display is None:
            return MatchingImagesRequest(request_id,current.catalog_id,revision,tag.id)
        key=normalize_tag(previous_display)
        row=next((row for row in current.snapshot.lookups if row.key==key),None)
        if (key==normalize_tag(tag.display) or key not in {normalize_tag(alias) for alias in tag.aliases}
                or row is None or row.tag_id!=tag.id or tuple(row.owner_ids)!=(tag.id,)
                or row.retired_ids or row.unavailable_ids or row.status in {'ambiguous','retired','unavailable','missing'}):
            raise HandoffHold('uniquely_owned_current_alias_required')
        return CanonicalRenameRequest(request_id,current.catalog_id,'',revision,tag.id,previous_display,tag.display)


class EncyclopediaCatalog:
    """Serial-worker service; its entry retains worker ownership through Close."""
    def __init__(self, settings_path):
        self.settings_path = connection._path(settings_path)
        self.view = None
        self.connection_review = None
        self.choice = None
        self.import_review = None
        self._read_adapter = None
        self._candidate_view = None
        self._read_request_generation = 0
        self._candidate_request_generation = None

    def open_catalog(self, root, baseline=None, expected_directory=None, *,
            read_candidate=None, should_cancel=None):
        """Current enrolled format only; no source pack or publication write."""
        self._read_request_generation += 1
        if read_candidate is not None:
            return self._open_read_candidate(root, baseline, expected_directory,
                read_candidate, should_cancel, self._read_request_generation)
        root = connection._path(root)
        before = connection._catalog_stamp(root)
        if expected_directory is not None and before[:2] != expected_directory:
            raise connection.TagConnectionHold("Tag Database folder changed. Close and reopen the reader before reviewing this folder.")
        if self.view is not None and self.view.root == root and self.view.stamp[:2] != before[:2]:
            raise connection.TagConnectionHold("Tag Database folder changed. Close and reopen the reader before reviewing this folder.")
        identity = decode_identity((root / "data/user/catalog_identity.json").read_bytes())
        retained=self.view.adapter if self.view is not None and self.view.root==root and self.view.identity==identity else None
        adapter = retained if type(retained) is NativeTagCatalogAdapter else NativeTagCatalogAdapter(root, native=connection.native,
            catalog_id=identity.publication_catalog_id,
            source_reference="Selected personal Tag Database")
        with adapter.guard():
            adapter.validate()
            if connection._catalog_stamp(root) != before:
                raise connection.TagConnectionHold("Tag Database changed while opening. Open it again.")
            if self.view is not None and self.view.root == root and self.view.identity != identity:
                raise connection.TagConnectionHold("Tag Database identity changed. Review its folder again.")
            previous = baseline if baseline is not None else (self.view.publication if self.view is not None and self.view.identity == identity else None)
            if previous is not None and previous.catalog_id != identity.publication_catalog_id:
                raise connection.TagConnectionHold("The current reader belongs to a different database identity.")
            saved = root / "tag_catalog_snapshot.json"
            if previous is None and saved.exists():
                previous = read(saved)
                if previous.catalog_id != identity.publication_catalog_id:
                    raise connection.TagConnectionHold("Saved catalog belongs to a different database identity.")
            generation = previous.generation if previous is not None else 1
            publication = adapter.capture(generation)
            if previous is not None:
                if publication.snapshot != previous.snapshot:
                    publication = type(publication)(publication.catalog_id, generation + 1, publication.snapshot)
            after = connection._catalog_stamp(root)
            if before != after:
                raise connection.TagConnectionHold("Tag Database changed while reading. Previous view is retained.")
        observed = EncyclopediaCatalogView(root, identity, publication, after, adapter)
        self.view = observed
        return observed

    def _open_read_candidate(self, root, baseline, expected_directory, evidence,
            should_cancel, request_generation):
        """Serial-worker test route; successful result is never the accepted view."""
        from core.encyclopedia_read_admission import (
            observe_selected_read, check_cancel, ReadAdmissionBinding)
        from suite_core.encyclopedia.publication import Publication
        first = observe_selected_read(root, evidence, should_cancel=should_cancel)
        root = first.root
        prior = self._candidate_view
        # Metadata has already held unknown semantics before creating any adapter.
        # Existing RulePairAdmission owns cooperative lock bookkeeping/exclusion.
        with connection.native.RulePairAdmission(root / "data/user").admit(
                existing_directory=True, expected_directory=first.directory_pins[2][1:]):
            current = observe_selected_read(root, evidence, should_cancel=should_cancel)
            if current != first:
                raise connection.TagConnectionHold("Selected metadata changed before native exclusion")
            before = connection._catalog_stamp(root)
            if expected_directory is not None and before[:2] != expected_directory:
                raise connection.TagConnectionHold("Tag Database folder changed; reopen explicitly")
            for accepted in (self.view, prior):
                if accepted is not None and accepted.root == root:
                    if accepted.stamp[:2] != before[:2] or accepted.identity != current.identity:
                        raise connection.TagConnectionHold("Selected catalog identity or folder changed")
            reusable = (prior is not None and prior.root == root
                and prior.read_binding.observation == current)
            adapter = self._read_adapter if reusable else NativeTagCatalogAdapter(root,
                native=connection.native, catalog_id=current.identity.publication_catalog_id,
                source_reference="Selected candidate read-only Tag Database")
            with adapter.guard():
                adapter.validate()
                previous = baseline if baseline is not None else (
                    prior.publication if reusable else None)
                if previous is not None and (type(previous) is not Publication
                        or previous.catalog_id != current.identity.publication_catalog_id):
                    raise connection.TagConnectionHold("Reader baseline belongs to another identity")
                generation = previous.generation if previous is not None else 1
                publication = adapter.capture(generation)
                if previous is not None and publication.snapshot != previous.snapshot:
                    publication = type(publication)(publication.catalog_id, generation + 1,
                        publication.snapshot)
                after = connection._catalog_stamp(root)
                final = observe_selected_read(root, evidence, should_cancel=should_cancel)
                if after != before or final != first:
                    raise connection.TagConnectionHold("Selected read evidence drifted; previous view retained")
                check_cancel(should_cancel)
                if request_generation != self._read_request_generation:
                    raise connection.TagConnectionHold("Selected read request was superseded")
                binding = ReadAdmissionBinding(final, evidence, after, publication.catalog_id,
                    publication.snapshot.fingerprint, publication.generation)
                result = EncyclopediaCatalogView(root, final.identity, publication, after,
                    None, binding, True, True)
        self._read_adapter = adapter
        self._candidate_view = result
        self._candidate_request_generation = request_generation
        return result

    def validate_read_delivery(self, view, *, should_cancel=None):
        """Pure staged-result/generation check; no GUI-thread catalog I/O."""
        from core.encyclopedia_read_admission import check_cancel, validate_read_binding
        check_cancel(should_cancel)
        if (view is not self._candidate_view or not view.read_only or not view.candidate_only
                or self._candidate_request_generation != self._read_request_generation):
            raise connection.TagConnectionHold("Read result is unstaged or superseded")
        return validate_read_binding(view.read_binding, root=view.root, identity=view.identity,
            publication=view.publication, stamp=view.stamp)

    def revalidate_read_view(self, view, *, should_cancel=None):
        """Worker-only deferred-delivery recheck, released before UI waiting."""
        from core.encyclopedia_read_admission import observe_selected_read
        binding = self.validate_read_delivery(view, should_cancel=should_cancel)
        first = observe_selected_read(view.root, binding.evidence, should_cancel=should_cancel)
        if first != binding.observation:
            raise connection.TagConnectionHold("Deferred selected metadata changed")
        with connection.native.RulePairAdmission(view.root / "data/user").admit(
                existing_directory=True, expected_directory=binding.observation.directory_pins[2][1:]):
            if observe_selected_read(view.root, binding.evidence,
                    should_cancel=should_cancel) != binding.observation:
                raise connection.TagConnectionHold("Deferred metadata changed before record reads")
            self._read_adapter.validate()
            if (connection._catalog_stamp(view.root) != binding.stamp
                    or observe_selected_read(view.root, binding.evidence,
                        should_cancel=should_cancel) != binding.observation):
                raise connection.TagConnectionHold("Deferred selected catalog changed")
            self.validate_read_delivery(view, should_cancel=should_cancel)
        return view

    def review_source(self, source, root, *, create_new=False):
        self.connection_review = self.import_review = None
        self.connection_review = connection.prepare_connection(
            self.settings_path, source, root, create_new)
        return self.connection_review

    def confirm_source(self, review, *, confirmed_token):
        if review is not self.connection_review:
            raise connection.TagConnectionHold("Review this exact source and destination again.")
        self.connection_review = None
        result = connection.confirm_connection(review, confirmed_token)
        self.choice = result.choice
        self.import_review = None
        return result

    def reopen_source(self):
        """Explicit saved-source reopening; not called by ordinary catalog Open."""
        self.connection_review = self.import_review = None
        result = connection.open_saved_connection(self.settings_path)
        self.choice = result.choice
        return result

    def preview_import(self):
        self.import_review = None
        if self.choice is None:
            raise connection.TagConnectionHold("Review and connect the selected source first.")
        # Same observation/typed release binding used by Manager; no fake release.
        self.choice = connection._observe(self.choice)
        choice = self.choice
        if choice.observation_status in {"unknown", "recovery_pending"}:
            raise connection.TagConnectionHold("Review installed evidence or finish exact recovery before importing.")
        action = DatabaseAction.MERGE_UPDATE if choice.installed else DatabaseAction.INSTALL_DEFAULT
        mode = "merged" if not choice.installed and empty_default_layer(choice.native_adapter) else None
        review = choice.transactions.preview(action, mode=mode, base_release=choice.base_release,
            available_release=choice.available_release, pack=choice.pack,
            installed=choice.installed, assignments=choice.assignments, offers=choice.offers)
        self.import_review = review
        return review

    def apply(self, review, *, confirmed_token):
        if self.choice is None or review is not self.import_review:
            raise connection.TagConnectionHold("Preview and confirm this exact import.")
        self.import_review = None
        result = self.choice.transactions.apply(review, confirmed_token=confirmed_token)
        self.choice = connection._observe(self.choice)
        return result

    def reload(self):
        if self.choice is None:
            if self.view is None:
                raise connection.TagConnectionHold("Open a Tag Database first.")
            return self.open_catalog(self.view.root)
        publication = self.choice.transactions.reload()
        self.choice = connection._observe(self.choice)
        self.open_catalog(self.choice.native_adapter.root)
        return publication

    def recover(self, receipt):
        if self.choice is None:
            raise connection.TagConnectionHold("Reopen the reviewed connection before selecting recovery.")
        self.import_review = None
        result = self.choice.transactions.recover(receipt)
        self.choice = connection._observe(self.choice)
        return result
