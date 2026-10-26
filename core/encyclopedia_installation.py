"""One reviewed Encyclopedia acquisition over the existing staging transaction.

Original-root admission and publisher trust are explicit dependencies. No root
lease, installer, native health assertion, default feed refresh or auto-launch.
"""
from dataclasses import dataclass, replace
from pathlib import Path
from hashlib import sha256
from uuid import uuid4

from core.component_acquisition import ComponentOffer
from suite_core.discovery.component_payload_policy import FROZEN_PAYLOAD_POLICY, require_payload_policy
from core.encyclopedia_connection import COMPONENT, ReaderConnection, review_directory, connection_from_reference, connection_from_release
from suite_core.discovery.component_staging import (
    ComponentStageAdmission, active, active_token, canonical, read_json,
    validate_journal, prior_journal_bytes, stage_and_activate_admitted, finish_or_recover_admitted,
    MAX_ARCHIVE, require, atomic, guard, _signed_reference_release,
)


@dataclass(frozen=True)
class InstalledEncyclopedia:
    selected: ReaderConnection | None
    phase: str
    active_sha256: str
    journal_sha256: str
    pending: bool
    connection_pending: bool = False
    recovery_available: bool = False
    recovery_version: str | None = None
    recovery_journal_sha256: str = ""


@dataclass(frozen=True)
class InstallationReview:
    offer: ComponentOffer
    installed: InstalledEncyclopedia
    settings: object
    destination: Path


@dataclass(frozen=True)
class InstallationResult:
    installed: InstalledEncyclopedia
    connection_saved: bool
    reason: str


class EncyclopediaInstallation:
    """Serial worker service; the UI retains its worker through settlement.

    Admission's hold callback must belong to the existing root and reader owner,
    exclude update/maintenance, and retain native path/root ownership. None keeps
    Install held. A verified offer is never converted into such a capability.
    """
    phase_progress_supported = True
    component = COMPONENT
    payload_policy = FROZEN_PAYLOAD_POLICY
    connection_type = ReaderConnection
    review_selected = staticmethod(review_directory)

    def __init__(self, acquisition, connection_store, *, admission=None, native_health=None, standalone_compatibility=False, schema2_identity=False):
        require_payload_policy(self.payload_policy, self.component)
        require(acquisition.maximum <= self.payload_policy.max_archive_bytes, "staging_archive_bound_required")
        require(admission is None or type(admission) is ComponentStageAdmission, "original_root_admission_required")
        self.acquisition, self.store = acquisition, connection_store
        self.admission, self.native_health = admission, native_health
        require(type(standalone_compatibility) is bool, "explicit_standalone_compatibility_mode_required")
        self.standalone_compatibility = standalone_compatibility
        require(type(schema2_identity) is bool, "explicit_reader_identity_mode")
        require(not schema2_identity or (self.component == COMPONENT and self.connection_type is ReaderConnection),
                "reader_identity_scope_required")
        self.schema2_identity = schema2_identity
        self._review = None

    @property
    def install_available(self):
        return self.admission is not None

    def _scope(self):
        require(self.admission is not None, "Installation awaits the original Manager root's qualified transaction admission.")
        return self.admission.transaction(self.component)

    def _connection(self, root, reference):
        if reference is None:
            return None
        if self.schema2_identity:
            return connection_from_reference(root, reference)
        return self.connection_type(root / reference["directory"], reference["inventory_sha256"], reference["version"])

    def _destination(self, root, release):
        if release.schema_version == 2:
            require(self.schema2_identity, "reader_identity_mode_required")
            return connection_from_release(root, release).directory
        return root / "Apps" / self.component.value / release.software_version

    def _observe(self, root, base, owner):
        platform = {"os": "windows", "architecture": "x86_64"}
        options = {"allow_schema2": True} if self.schema2_identity else {}
        reference = (read_json(base / "active.json") if (base / "active.json").exists() else None) if self.schema2_identity else active(root, base, self.component, platform, payload_policy=self.payload_policy)
        journal_path = base / "transaction.json"
        pending = (base / "pending-transaction.json").exists()
        journal = read_json(journal_path) if journal_path.exists() else None
        if journal is not None:
            validate_journal(journal, self.component, owner, self.acquisition.trust, **options)
            prior_journal_bytes(base, journal, self.component, owner, self.acquisition.trust, **options)
            phase = journal["phase"]
            expected = journal["target"] if phase in ("healthy", "activated") else journal["before"]
            # Interrupted handoffs require explicit recovery, never adoption.
            if phase not in ("healthy", "activated", "rolled_back", "abandoned"):
                pending = True
            else:
                require(reference == expected, "installed_origin_reference_mismatch")
        else:
            require(reference is None, "authenticated_installed_origin_required")
            phase = "absent"
        if self.schema2_identity and reference is not None:
            release = _signed_reference_release(reference, journal, base, self.component, owner, self.acquisition.trust)
            active(root, base, self.component, platform, payload_policy=self.payload_policy, release=release)
        selected = self._connection(root, reference)
        if selected is not None:
            self.review_selected(selected.directory, expected=selected)
        # Preview the exact authenticated journal recovery will consume, including
        # an interrupted transaction rather than an unrelated installed version.
        recovery = journal
        pending_path = base / "pending-transaction.json"
        if pending_path.exists():
            recovery = read_json(pending_path)
            validate_journal(recovery, self.component, owner, self.acquisition.trust, **options)
            prior_journal_bytes(base, recovery, self.component, owner, self.acquisition.trust, **options)
        available = recovery is not None and (pending or recovery["phase"] not in ("rolled_back", "abandoned"))
        before = None if recovery is None else recovery["before"]
        return InstalledEncyclopedia(selected, phase, active_token(reference),
            sha256(canonical(journal)).hexdigest(), pending, (base / "connection-pending.json").exists(),
            available, None if before is None else before["version"],
            "" if recovery is None else sha256(canonical(recovery)).hexdigest())

    def observe(self):
        self.acquisition._trust()
        with self._scope() as (root, base, owner, check):
            check()
            return self._observe(root, base, owner)

    def discover(self, *, channel, should_cancel=None):
        self._review = None
        installed = self.observe()
        require(not installed.pending, "Recover the pending installation before refreshing releases.")
        versions = {} if installed.selected is None else {self.component.value: installed.selected.version}
        return tuple(offer for offer in self.acquisition.discover(channel=channel,
            installed_versions=versions, should_cancel=should_cancel, component_id=self.component)
            if offer.release.component_id == self.component)

    def review(self, offer):
        self._review = None  # failed re-admission cannot retain an earlier confirmation
        require(type(offer) is ComponentOffer and offer.release.component_id == self.component,
                "exact_encyclopedia_offer_required")
        require(self.acquisition._offers.get(offer.release.manifest_sha256) is offer, "current_signed_offer_required")
        self.acquisition.revalidate_offer(offer)
        with self._scope() as (root, base, owner, check):
            installed = self._observe(root, base, owner)
            require(not installed.pending and installed.phase != "activated", "Finish launch checking or recover the current installation first.")
            require(not (base / "connection-pending.json").exists(), "Retry the pending connection publication before another install.")
            require(offer.current_version == (None if installed.selected is None else installed.selected.version), "stale_installed_offer")
            if offer.release.schema_version == 2:
                require(self.acquisition.schema2_metadata and offer.installed_context is not None,
                        "original_metadata_observation_required")
                context = offer.installed_context
                selected = installed.selected
                require((context.version, context.schema_version, context.maturity) ==
                        ((None, None, None) if selected is None else
                         (selected.version, selected.release_schema, selected.maturity)), "stale_installed_identity")
            settings = self.store.observe()
            saved = self.store.selected(settings)
            require(saved is None or saved == installed.selected, "The saved reader is externally managed. Review its managed origin before installing.")
            destination = self._destination(root, offer.release)
            check()
            self._review = InstallationReview(offer, installed, settings, destination)
            return self._review

    @staticmethod
    def _cancel(cancel):
        if cancel is not None and cancel():
            raise InterruptedError("Acquisition cancelled. Any pending staging must be explicitly recovered.")

    def _unchanged(self, reviewed, root, base, owner):
        require(self._observe(root, base, owner) == reviewed.installed, "stale_installation_review")
        require(self.store.observe().digest == reviewed.settings.digest, "Settings changed. Review installation again.")

    def install(self, reviewed, *, confirmed=False, progress=None, should_cancel=None, phase_progress=None):
        require(phase_progress is None or callable(phase_progress), 'phase_progress_callback_required')
        def phase(value):
            if phase_progress is not None:
                phase_progress(value)
        require(confirmed is True and reviewed is self._review, "explicit_current_installation_confirmation_required")
        self._review = None  # one-shot; any failure needs fresh Review/confirmation
        phase('Checking installation permissions')
        with self._scope() as (root, base, owner, check):
            self._unchanged(reviewed, root, base, owner)
            check()
        self._cancel(should_cancel)
        phase('Checking the reader download')
        acquired = self.acquisition.acquire(reviewed.offer, progress=progress, should_cancel=should_cancel)
        phase('Verifying the downloaded reader')
        self._cancel(should_cancel)
        # Network/cache work does not hold root maintenance exclusion. Re-admit
        # exactly the same original owners and stale tokens before any mutation.
        with self._scope() as (root, base, owner, check):
            self._unchanged(reviewed, root, base, owner)
            require(self._destination(root, reviewed.offer.release) == reviewed.destination,
                    "changed_install_destination")
            check()
        def handoff(phase):
            if phase_progress is not None:
                message = {'staging_created': 'Preparing reader files',
                    'member_extracted': 'Installing reader files',
                    'before_version_publish': 'Finishing reader installation'}.get(phase)
                if message is not None:
                    phase_progress(message)
            if phase in ("staging_created", "member_extracted", "before_version_publish"):
                self._cancel(should_cancel)
            # After version publication the transaction settles even if Stop was
            # clicked. The retained UI worker reports that settlement truthfully.
        offer = acquired.offer
        if self.standalone_compatibility:
            from core.encyclopedia_update_binding import require_standalone_install_compatibility
            require_standalone_install_compatibility(reviewed.installed.selected, acquired)
            self._cancel(should_cancel)
        stage_and_activate_admitted(self.admission, acquired.artifact, component=self.component,
            version=offer.release.software_version, os_name="windows", architecture="x86_64",
            manifest_bytes=offer.manifest_bytes, signature_bytes=offer.signature_bytes,
            trust_bytes=self.acquisition.trust, expected_active_token=reviewed.installed.active_sha256,
            failpoint=handoff, preflight=lambda root, base, owner: self._unchanged(reviewed, root, base, owner), payload_policy=self.payload_policy,
            **({"release_schema": offer.release.schema_version, "maturity": offer.release.maturity} if self.schema2_identity else {}))
        phase('Saving the reader connection')
        return self._publish_connection(expected=reviewed.settings)

    def _publish_connection(self, *, expected, restoring=False):
        with self._scope() as (root, base, owner, check):
            installed = self._observe(root, base, owner)
            require(not installed.pending and installed.phase in
                    (("activated", "healthy", "rolled_back", "abandoned") if restoring else ("activated", "healthy")),
                    "committed_installation_required")
            path = base / "connection-pending.json"
            guard(path)
            target = None if installed.selected is None else installed.selected.mapping()
            if path.exists():
                intent = read_json(path)
                require(set(intent) == {"schema", "journal_sha256", "target", "prior_selection"}
                        and intent["schema"] == 1 and intent["journal_sha256"] == installed.journal_sha256
                        and intent["target"] == target, "connection_publication_evidence_changed")
            else:
                previous = self.store.selected(expected)
                if not restoring:
                    journal = read_json(base / "transaction.json")
                    before = journal["before"]
                    prior = self._connection(root, before)
                    require(previous in (None, prior, installed.selected), "Saved reader changed independently; publication held.")
                intent = {"schema": 1, "journal_sha256": installed.journal_sha256,
                          "target": target, "prior_selection": None if previous is None else previous.mapping()}
                check()
                atomic(path, intent)
                require(read_json(path) == intent, "connection_intent_readback")
            try:
                saved = self.store.selected(self.store.observe())
                saved_mapping = None if saved is None else saved.mapping()
                require(saved_mapping in (target, intent["prior_selection"]),
                        "Saved reader changed independently. Review its connection before Retry save.")
                if saved_mapping != target:
                    check()
                    if installed.selected is None:
                        self.store.clear(expected=expected)
                    else:
                        self.store.save(installed.selected, expected=expected)
                require(self.store.selected(self.store.observe()) == installed.selected, "connection_publication_readback")
                check()
                path.unlink()
                require(not path.exists(), "connection_intent_clear_readback")
                installed = replace(installed, connection_pending=False)
            except Exception as error:
                return InstallationResult(installed, False,
                    f"Executable transaction saved; connection publication needs Retry save. {error}")
        if restoring:
            return InstallationResult(installed, True,
                "Recovery settled. Retained package history and user data were preserved. Review the saved reader before Launch.")
        return InstallationResult(installed, True,
            f"Reader {installed.selected.version} installed and connected. Launch the reader, then close its window to finish setup. Portable Manager may need an ordinary restart before Launch.")

    def retry_connection(self):
        """Explicit user Retry save, including after restart; never restage."""
        self._review = None
        with self._scope() as (root, base, owner, check):
            restoring = (base / "connection-pending.json").exists()
        return self._publish_connection(expected=self.store.observe(), restoring=restoring)

    def confirm_health(self):
        require(callable(self.native_health), "Qualified native launch/drain evidence is required before accepting health.")
        installed = self.observe()
        require(not installed.pending and installed.phase == "activated", "activated_health_phase_required")
        with self._scope() as (root, base, owner, check):
            require(not (base / "connection-pending.json").exists(), "Save the pending reader connection before accepting health.")
        # Original native owner supplies fresh exact-package outcome; no UI bool.
        require(self.native_health(installed.selected) is True, "native_reader_health_held")
        finish_or_recover_admitted(self.admission, component=self.component,
            trust_bytes=self.acquisition.trust, health_verified=True,
            preflight=lambda root, base, owner: require(self._observe(root, base, owner) == installed, "changed_health_selection"), payload_policy=self.payload_policy,
            **({"allow_schema2": True} if self.schema2_identity else {}))
        return self.observe()

    def reconcile_launch(self):
        """Local status reconciliation; no network, launch or recovery action.

        A retained owner's conclusive exact-package receipt can finish setup
        automatically. Missing evidence is a waiting state, never an override.
        """
        installed = self.observe()
        saved = self.store.selected(self.store.observe())
        connected = saved == installed.selected and not installed.connection_pending
        if installed.pending:
            return InstallationResult(installed, connected,
                "Installation needs recovery. Review Recover installation before continuing setup.")
        if installed.connection_pending:
            return InstallationResult(installed, False,
                "The executable transaction is saved. Retry save connection before continuing setup.")
        if self.schema2_identity and installed.selected is not None and not connected:
            return InstallationResult(installed, False,
                "Saved reader identity differs from the installed reader. Review its original owner before launch.")
        if installed.phase == "activated" and connected:
            if callable(self.native_health) and self.native_health(installed.selected) is True:
                installed = self.confirm_health()
                return InstallationResult(installed, True, "Reader setup is complete. Updates may now be reviewed; Launch remains your choice.")
            return InstallationResult(installed, True,
                "Setup is waiting for a successful reader run. Launch the reader, close its window, then return here. Portable Manager may first need an ordinary restart.")
        return InstallationResult(installed, connected,
            "Reader setup is complete. Launch remains your choice." if installed.phase == "healthy" else
            "No managed reader is active. Refresh releases to review an installation.")

    def recover(self, *, confirmed=False, expected_installed=None):
        require(confirmed is True, "explicit_recovery_confirmation_required")
        self._review = None
        expected = self.store.observe()
        old_publication = [None]
        def preflight(root, base, owner):
            if expected_installed is not None:
                require(type(expected_installed) is InstalledEncyclopedia
                    and self._observe(root, base, owner) == expected_installed,
                    "Installation changed. Check Again and review recovery before confirming.")
            require(self.store.observe().digest == expected.digest, "Settings changed before recovery.")
            path = base / "pending-transaction.json"
            journal = read_json(path if path.exists() else base / "transaction.json")
            validate_journal(journal, self.component, owner, self.acquisition.trust, **({"allow_schema2": True} if self.schema2_identity else {}))
            allowed = [None]
            for reference in (journal["before"], journal["target"]):
                if reference is not None:
                    allowed.append(self._connection(root, reference))
            require(self.store.selected(expected) in allowed,
                    "Saved reader changed independently. Review its connection before recovery.")
            pending = base / "connection-pending.json"
            guard(pending)
            if pending.exists():
                intent = read_json(pending)
                current = read_json(base / "transaction.json")
                validate_journal(current, self.component, owner, self.acquisition.trust, **({"allow_schema2": True} if self.schema2_identity else {}))
                require(set(intent) == {"schema", "journal_sha256", "target", "prior_selection"}
                        and intent["schema"] == 1 and intent["journal_sha256"] == sha256(canonical(current)).hexdigest()
                        and intent["target"] in [None if value is None else value.mapping() for value in allowed],
                        "unknown_connection_recovery_intent")
                old_publication[0] = pending.read_bytes()
        reference = finish_or_recover_admitted(self.admission, component=self.component,
            trust_bytes=self.acquisition.trust, rollback_approved=True, preflight=preflight, payload_policy=self.payload_policy, **({"allow_schema2": True} if self.schema2_identity else {}))
        # A failed recovery save/clear is also resumable without repeating the
        # executable rollback. Persist its exact publication intent in State.
        with self._scope() as (root, base, owner, check):
            pending = base / "connection-pending.json"
            guard(pending)
            if pending.exists():
                # Supersede only the exact previous journal's intent after this
                # explicitly reviewed rollback, never an unknown state record.
                require(old_publication[0] is not None and pending.read_bytes() == old_publication[0],
                        "changed_connection_recovery_intent")
                from suite_core.discovery.component_staging import atomic_bytes
                atomic_bytes(base / ("connection-history-" + uuid4().hex + ".json"), old_publication[0])
                pending.unlink()
        return self._publish_connection(expected=expected, restoring=True)
