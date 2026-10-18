"""One reviewed Encyclopedia acquisition over the existing staging transaction.

Original-root admission and publisher trust are explicit dependencies. No root
lease, installer, native health assertion, default feed refresh or auto-launch.
"""
from dataclasses import dataclass
from pathlib import Path
from hashlib import sha256
from uuid import uuid4

from core.component_acquisition import ComponentOffer
from core.encyclopedia_connection import COMPONENT, ReaderConnection, review_directory
from suite_core.discovery.component_staging import (
    ComponentStageAdmission, active, active_token, canonical, read_json,
    validate_journal, prior_journal_bytes, stage_and_activate_admitted, finish_or_recover_admitted,
    MAX_ARCHIVE, require, atomic, guard,
)


@dataclass(frozen=True)
class InstalledEncyclopedia:
    selected: ReaderConnection | None
    phase: str
    active_sha256: str
    journal_sha256: str
    pending: bool
    connection_pending: bool = False


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
    def __init__(self, acquisition, connection_store, *, admission=None, native_health=None, standalone_compatibility=False):
        require(acquisition.maximum <= MAX_ARCHIVE, "staging_archive_bound_required")
        require(admission is None or type(admission) is ComponentStageAdmission, "original_root_admission_required")
        self.acquisition, self.store = acquisition, connection_store
        self.admission, self.native_health = admission, native_health
        require(type(standalone_compatibility) is bool, "explicit_standalone_compatibility_mode_required")
        self.standalone_compatibility = standalone_compatibility
        self._review = None
        self.local_reader = None

    def select_local_reader(self, reader):
        from core.encyclopedia_local_reader import SignedLocalReader
        require(type(reader) is SignedLocalReader and reader.acquisition is self.acquisition
                and self.local_reader is None and self.standalone_compatibility, "original_local_reader_service_required")
        require(self.admission is not None, "original_root_admission_required")
        self.local_reader = reader

    def _fresh_local(self, installed):
        require(installed.selected is None and installed.phase == "absent"
                and not installed.pending and not installed.connection_pending, "local_reader_first_install_only")

    @property
    def install_available(self):
        return self.admission is not None

    def _scope(self):
        require(self.admission is not None, "Installation awaits the original Manager root's qualified transaction admission.")
        return self.admission.transaction(COMPONENT)

    def _observe(self, root, base, owner):
        platform = {"os": "windows", "architecture": "x86_64"}
        reference = active(root, base, COMPONENT, platform)
        journal_path = base / "transaction.json"
        pending = (base / "pending-transaction.json").exists()
        journal = read_json(journal_path) if journal_path.exists() else None
        if journal is not None:
            validate_journal(journal, COMPONENT, owner, self.acquisition.trust)
            prior_journal_bytes(base, journal, COMPONENT, owner, self.acquisition.trust)
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
        selected = None if reference is None else ReaderConnection(
            root / reference["directory"], reference["inventory_sha256"], reference["version"])
        if selected is not None:
            review_directory(selected.directory, expected=selected)
        return InstalledEncyclopedia(selected, phase, active_token(reference),
            sha256(canonical(journal)).hexdigest(), pending, (base / "connection-pending.json").exists())

    def observe(self):
        self.acquisition._trust()
        with self._scope() as (root, base, owner, check):
            check()
            return self._observe(root, base, owner)

    def discover(self, *, channel, should_cancel=None):
        self._review = None
        installed = self.observe()
        require(not installed.pending, "Recover the pending installation before refreshing releases.")
        if self.local_reader is not None:
            self._fresh_local(installed)
            self._cancel(should_cancel)
            result = self.local_reader.discover(channel)
            self._cancel(should_cancel)
            return result
        versions = {} if installed.selected is None else {COMPONENT.value: installed.selected.version}
        return tuple(offer for offer in self.acquisition.discover(channel=channel,
            installed_versions=versions, should_cancel=should_cancel, component_id=COMPONENT)
            if offer.release.component_id == COMPONENT)

    def review(self, offer):
        self._review = None  # failed re-admission cannot retain an earlier confirmation
        require(type(offer) is ComponentOffer and offer.release.component_id == COMPONENT,
                "exact_encyclopedia_offer_required")
        if self.local_reader is None:
            require(self.acquisition._offers.get(offer.release.manifest_sha256) is offer, "current_signed_offer_required")
            self.acquisition.revalidate_offer(offer)
        else:
            self.local_reader.revalidate(offer)
        with self._scope() as (root, base, owner, check):
            installed = self._observe(root, base, owner)
            if self.local_reader is not None:
                self._fresh_local(installed)
            require(not installed.pending and installed.phase != "activated", "Finish launch checking or recover the current installation first.")
            require(not (base / "connection-pending.json").exists(), "Retry the pending connection publication before another install.")
            require(offer.current_version == (None if installed.selected is None else installed.selected.version), "stale_installed_offer")
            settings = self.store.observe()
            saved = self.store.selected(settings)
            require(saved is None or saved == installed.selected, "The saved reader is externally managed. Review its managed origin before installing.")
            destination = root / "Apps" / COMPONENT.value / offer.release.software_version
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

    def install(self, reviewed, *, confirmed=False, progress=None, should_cancel=None):
        require(confirmed is True and reviewed is self._review, "explicit_current_installation_confirmation_required")
        self._review = None  # one-shot; any failure needs fresh Review/confirmation
        with self._scope() as (root, base, owner, check):
            self._unchanged(reviewed, root, base, owner)
            check()
        self._cancel(should_cancel)
        if progress and self.standalone_compatibility:
            progress("phase", "Checking the selected app package")
        source = self.acquisition if self.local_reader is None else self.local_reader
        acquired = source.acquire(reviewed.offer, progress=progress, should_cancel=should_cancel)
        self._cancel(should_cancel)
        # Network/cache work does not hold root maintenance exclusion. Re-admit
        # exactly the same original owners and stale tokens before any mutation.
        with self._scope() as (root, base, owner, check):
            self._unchanged(reviewed, root, base, owner)
            require(root / "Apps" / COMPONENT.value / reviewed.offer.release.software_version == reviewed.destination,
                    "changed_install_destination")
            check()
        def handoff(phase):
            if progress and self.standalone_compatibility:
                label = {"staging_created": "Installing app files", "member_extracted": "Installing app files",
                    "before_version_publish": "Finishing installation"}.get(phase)
                if label:
                    progress("phase", label)
            if phase in ("staging_created", "member_extracted", "before_version_publish"):
                self._cancel(should_cancel)
            # After version publication the transaction settles even if Stop was
            # clicked. The retained UI worker reports that settlement truthfully.
        offer = acquired.offer
        if self.standalone_compatibility:
            if progress:
                progress("phase", "Checking package compatibility")
            from core.encyclopedia_update_binding import require_standalone_install_compatibility
            require_standalone_install_compatibility(reviewed.installed.selected, acquired)
            self._cancel(should_cancel)
        stage_and_activate_admitted(self.admission, acquired.artifact, component=COMPONENT,
            version=offer.release.software_version, os_name="windows", architecture="x86_64",
            manifest_bytes=offer.manifest_bytes, signature_bytes=offer.signature_bytes,
            trust_bytes=self.acquisition.trust, expected_active_token=reviewed.installed.active_sha256,
            failpoint=handoff, preflight=lambda root, base, owner: self._unchanged(reviewed, root, base, owner))
        if progress and self.standalone_compatibility:
            progress("phase", "Saving the app connection")
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
                    prior = None if before is None else ReaderConnection(root / before["directory"],
                        before["inventory_sha256"], before["version"])
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
        finish_or_recover_admitted(self.admission, component=COMPONENT,
            trust_bytes=self.acquisition.trust, health_verified=True,
            preflight=lambda root, base, owner: require(self._observe(root, base, owner) == installed, "changed_health_selection"))
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
        if installed.phase == "activated" and connected:
            if callable(self.native_health) and self.native_health(installed.selected) is True:
                installed = self.confirm_health()
                return InstallationResult(installed, True, "Reader setup is complete. Updates may now be reviewed; Launch remains your choice.")
            return InstallationResult(installed, True,
                "Setup is waiting for a successful reader run. Launch the reader, close its window, then return here. Portable Manager may first need an ordinary restart.")
        return InstallationResult(installed, connected,
            "Reader setup is complete. Launch remains your choice." if installed.phase == "healthy" else
            "No managed reader is active. Refresh releases to review an installation.")

    def recover(self, *, confirmed=False):
        require(confirmed is True, "explicit_recovery_confirmation_required")
        self._review = None
        expected = self.store.observe()
        old_publication = [None]
        def preflight(root, base, owner):
            require(self.store.observe().digest == expected.digest, "Settings changed before recovery.")
            path = base / "pending-transaction.json"
            journal = read_json(path if path.exists() else base / "transaction.json")
            validate_journal(journal, COMPONENT, owner, self.acquisition.trust)
            allowed = [None]
            for reference in (journal["before"], journal["target"]):
                if reference is not None:
                    allowed.append(ReaderConnection(root / reference["directory"], reference["inventory_sha256"], reference["version"]))
            require(self.store.selected(expected) in allowed,
                    "Saved reader changed independently. Review its connection before recovery.")
            pending = base / "connection-pending.json"
            guard(pending)
            if pending.exists():
                intent = read_json(pending)
                current = read_json(base / "transaction.json")
                validate_journal(current, COMPONENT, owner, self.acquisition.trust)
                require(set(intent) == {"schema", "journal_sha256", "target", "prior_selection"}
                        and intent["schema"] == 1 and intent["journal_sha256"] == sha256(canonical(current)).hexdigest()
                        and intent["target"] in [None if value is None else value.mapping() for value in allowed],
                        "unknown_connection_recovery_intent")
                old_publication[0] = pending.read_bytes()
        reference = finish_or_recover_admitted(self.admission, component=COMPONENT,
            trust_bytes=self.acquisition.trust, rollback_approved=True, preflight=preflight)
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
