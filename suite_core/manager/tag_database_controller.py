"""Host-owned background coordination for the existing native Tag transaction service.

No catalog discovery, executor creation, enrollment, native writes or restoration engine.
The host owns the executor lifetime and must drain close_held before closing it.
"""
from concurrent.futures import Future
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from importlib import import_module
from threading import Event, Lock, get_ident

from suite_core.database_actions import DatabaseAction, CatalogRelease
from suite_core.knowledge.tag_transactions import TagCatalogTransactions
from suite_core.knowledge.native_tag_catalog_host import NativeTagCatalogAdapter, bind_foundation_source_release
from suite_core.installation import InstalledComponent, LocalInventory, ComponentKind
from core.connected_version_observation import VersionBinding, VersionObservation
from core.connected_version_projection import observe_default_content


@dataclass(frozen=True)
class TagDatabaseChoice:
    label: str
    transactions: TagCatalogTransactions
    base_release: CatalogRelease
    available_release: CatalogRelease | None
    pack: Path
    installed: bool
    assignments: tuple = ()
    offers: tuple = ()
    recovery_receipts: tuple = ()
    native_adapter: NativeTagCatalogAdapter | None = None
    approved_releases: tuple = ()
    receipt_stamps: tuple = ()
    observation_status: str = "supplied"
    foundation_component: InstalledComponent | None = None
    foundation_code_root: Path | None = None
    capability_report: object = None
    capability_inventory: tuple = ()
    capability_revision: str = ""
    capability_error: str = ""
    version_observation: VersionObservation | None = None

    def with_foundation_component(self, component, code_root):
        """Select an approved installed inventory; acquisition stays on the worker."""
        return replace(self, available_release=None, foundation_component=component,
                       foundation_code_root=Path(code_root))

    def with_foundation_source(self, decoded, approved_source_snapshot):
        """Bind supplied approved source evidence for the existing entry seam.

        Passive: no companion discovery/loading, recipient capture, enrollment,
        executor creation or Apply authority. Native preview still checks the
        exact archive and default Tags/Categories before confirmation.
        """
        adapter = self.native_adapter
        if (adapter is None or type(decoded) is not getattr(adapter.native, "FoundationPack", None)
                or adapter.foundation_proposal != decoded.proposal):
            raise ValueError("Bind the decoded Foundation proposal to the exact selected native adapter")
        release = bind_foundation_source_release(decoded, approved_source_snapshot)
        if release.revision.pack_id != self.base_release.revision.pack_id:
            raise ValueError("Foundation source belongs to a different approved Tag catalog")
        return replace(self, available_release=release)

    def __post_init__(self):
        if self.version_observation is not None and type(self.version_observation) is not VersionObservation:
            raise TypeError("Immutable supplied content version observation required")
        if (type(self.label) is not str or not self.label.strip()
                or type(self.transactions) is not TagCatalogTransactions
                or type(self.base_release) is not CatalogRelease
                or self.available_release is not None and type(self.available_release) is not CatalogRelease
                or type(self.installed) is not bool
                or any(type(v) is not tuple for v in (self.assignments, self.offers, self.recovery_receipts))):
            raise ValueError("Explicit approved Tag catalog/release inputs required")
        object.__setattr__(self, "pack", Path(self.pack))
        object.__setattr__(self, "recovery_receipts", tuple(Path(p) for p in self.recovery_receipts))
        if self.native_adapter is not None:
            if (type(self.native_adapter) is not NativeTagCatalogAdapter
                    or self.native_adapter.root != self.transactions.host.root
                    or getattr(self.transactions.host.capture, "__self__", None) is not self.native_adapter):
                raise ValueError("Bind the exact native adapter already owning this Tag service")
        if type(self.approved_releases) is not tuple or any(type(r) is not CatalogRelease for r in self.approved_releases):
            raise ValueError("Supply immutable approved releases; never reconstruct snapshots from version labels")
        if self.foundation_component is not None:
            item = self.foundation_component
            if (type(item) is not InstalledComponent or item.kind is not ComponentKind.APP
                    or type(item.location) is not LocalInventory
                    or item.location.manifest != "BOOTSTRAP_INVENTORY.json"
                    or self.native_adapter is None or not isinstance(self.foundation_code_root, Path)
                    or not self.foundation_code_root.is_absolute() or ".." in self.foundation_code_root.parts):
                raise ValueError("Explicit selected local component inventory and absolute code root required")
        elif self.foundation_code_root is not None:
            raise ValueError("Code root requires an explicit selected Foundation component")
        if self.capability_report is not None:
            if (self.native_adapter is None or type(self.capability_report) is not
                    import_module(self.native_adapter.package + ".foundation_dependencies").FoundationCapabilityReport):
                raise TypeError("Exact native selected-recipient capability report required")
        for receipt in self.recovery_receipts:
            path = Path(receipt)
            if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(self.transactions.host.root / "backups"):
                raise ValueError("Supply exact approved selected native recovery receipts")


def _acquire_foundation_choice(choice):
    """Reuse exact stock inventory, bounded DTO read and native source binding."""
    item = choice.foundation_component
    if item is None:
        return choice
    from suite_core.discovery.component_staging import verify_directory, guard
    from suite_core.encyclopedia.publication import read
    try:
        # LocalInventory already binds the explicit relative payload root. A
        # content candidate need not use an executable Apps/<id>/<version> home.
        directory = choice.foundation_code_root / item.location.relative_directory
        guard(directory)
        companion = directory / "metadata/TagDatabase.source-snapshot.json"
        guard(companion)
        if not companion.is_file():
            raise ValueError("Required source snapshot companion is missing. Restore the reviewed component, then Preview again.")
        if (not choice.pack.is_absolute() or ".." in choice.pack.parts
                or not choice.pack.is_relative_to(directory) or not choice.pack.is_file()):
            raise ValueError("Exact Foundation archive and source companion must belong to the selected inventory")
        def verify():
            try:
                verify_directory(directory, item.location.manifest_sha256, item.component_id, item.software_version)
            except (ValueError, OSError) as error:
                raise ValueError("Selected component cannot match its pinned inventory. Restore the reviewed files, then Preview again. "
                    "Inventory check: " + str(error)) from error
        verify()
        decoded = choice.native_adapter.native.load_foundation_pack(choice.pack)
        bound = choice.with_foundation_source(decoded, read(companion).snapshot)
        # Revalidate the same immutable inventory after decoding, including exact
        # membership/size/hash and link guards. Never adopt a replacement package.
        verify()
        return bound
    except (ValueError, TypeError, OSError) as error:
        raise ValueError("Approved Foundation component held: " + str(error)) from error


def empty_default_layer(adapter):
    """Prove an enrolled empty default layer under original native admission.

    Personal records do not imply default content. Absence of a version alone
    does not establish emptiness; inspect every directory entry and tracking.
    """
    if type(adapter) is not NativeTagCatalogAdapter:
        return False
    with adapter.guard():
        state = adapter._state()
        if not state.enrolled:
            return False
        admission = import_module(adapter.package + ".catalog_admission")
        if any(value["status"] == "pending" for _path, value, _stamp in
               admission._receipts(adapter.root, adapter.native)):
            return False
        adapter.validate()
        defaults = adapter.root / "data/defaults"
        tracking = adapter.root / "data/user/default_knowledge_state.json"
        adapter.native._check_export_path(defaults)
        adapter.native._check_export_path(tracking)
        before = defaults.stat()
        members = tuple(sorted(path.name for path in defaults.iterdir()))
        if members or tracking.exists():
            return False
        observed = adapter.default_content()
        adapter.native._check_export_path(defaults)
        adapter.native._check_export_path(tracking)
        after = defaults.stat()
        fresh = tuple(sorted(path.name for path in defaults.iterdir()))
        adapter.validate()
        if (before.st_dev, before.st_ino, members) != (after.st_dev, after.st_ino, fresh):
            raise RuntimeError("Default-directory membership changed during observation")
        return not fresh and not tracking.exists() and observed.tracking_fingerprint == "missing"


def _observe(choice, *, include_capabilities=True):
    """Reuse native observation/receipt validation under the selected exclusion."""
    if not include_capabilities:
        choice = replace(choice, capability_report=None, capability_inventory=(),
            capability_revision="", capability_error="Catalog changed; refresh observations to check its current behavior.")
    adapter = choice.native_adapter
    if adapter is None:
        return choice
    with adapter.guard():
        adapter._state()  # Pinned identity check also permits selected pending recovery.
        receipt_root = adapter.root / "backups/knowledge-pack-import"
        adapter.native._check_export_path(receipt_root)
        if receipt_root.exists() and not receipt_root.is_dir():
            raise RuntimeError("Native recovery folder is unavailable; retain evidence for owner review")
        admission = import_module(adapter.package + ".catalog_admission")
        # Read native receipts first; Foundation restore preflight below needs
        # exact selected recovery admission rather than ordinary admission.
        pending = tuple(row for row in admission._receipts(adapter.root, adapter.native) if row[1]["status"] == "pending")
        releases = choice.approved_releases + (choice.base_release,) + ((choice.available_release,) if choice.available_release else ())
        release_type = import_module(adapter.package + ".default_catalog_release").ApprovedDefaultPackRelease
        for path, value, _digest in pending:
            approved = release_type.decode(value.get("approved_default_release_json", ""))
            if (value.get("schema_version") not in {2, 5}
                    or approved.release_fingerprint not in {r.fingerprint for r in releases}
                    or approved.pack_id != choice.base_release.revision.pack_id):
                raise RuntimeError("Pending operation is outside the approved Tag import; retain it for its native owner")
            allowed = {adapter.root / "data/user/tags.json", adapter.root / "data/user/default_knowledge_state.json"}
            if "foundation_transition" in value:
                # A declared default path is not authority. Reuse the exact native
                # selected lease and full backup/stale preflight before offering
                # recovery; this observation never restores or admits ordinary work.
                with admission.selected_native_pack_recovery(adapter.root, adapter.native,
                        receipt=path, catalog_identity_binding=adapter.identity_binding):
                    allowed = adapter.native._require_foundation_authority(adapter.root, value)
                    allowed.add(adapter.root / "data/user/default_knowledge_state.json")
                    adapter.native._prepare_pack_restore(path, value, adapter.root)
            if any(Path(row.get("target", "")) not in allowed for row in value.get("files", ())):
                raise RuntimeError("Pending operation is outside the approved Tag import; retain it for its native owner")
        receipts = tuple(Path(row[0]).absolute() for row in pending)
        stamps = tuple((Path(row[0]).absolute(), row[2]) for row in pending)
        if receipts:
            return replace(choice, recovery_receipts=receipts, receipt_stamps=stamps,
                           observation_status="recovery_pending", capability_report=None,
                           capability_inventory=(), capability_revision="",
                           capability_error="Finish exact native recovery before checking selected-catalog behavior.")
        if include_capabilities:
            choice = _observe_capabilities(choice)
        observed = adapter.default_content(choice.base_release.revision.pack_id)
        # Retain the result of this existing worker-owned read, never read again
        # while presenting it. Admission, base-release selection and recovery
        # remain with the original native owner below.
        version_binding = VersionBinding(choice.base_release.revision.pack_id,
            adapter.catalog_id, observed.tracking_fingerprint, version_kind="content",
            source_identity=str(adapter.root))
        version_observation = observe_default_content(version_binding, observed,
            observed_at=datetime.now(timezone.utc))
        choice = replace(choice, version_observation=version_observation)
        if observed.approved_release is not None:
            fingerprint = observed.approved_release["release_fingerprint"]
            approved = next((r for r in releases if r.fingerprint == fingerprint), None)
            if approved is None:
                return replace(choice, recovery_receipts=(), receipt_stamps=(), observation_status="unknown",
                    version_observation=replace(version_observation, installation_condition="changed_requires_review"))
            return replace(choice, base_release=approved, installed=True, recovery_receipts=(),
                           receipt_stamps=(), observation_status="current")
        # A genuine empty default layer may already contain personal records.
        blank = not choice.installed and empty_default_layer(adapter)
        return replace(choice, recovery_receipts=(), receipt_stamps=(),
                       observation_status="current" if blank else "unknown")


def _observe_capabilities(choice):
    adapter = choice.native_adapter
    if adapter.foundation_proposal is None:
        return choice
    try:
        # Called under the selected host guard, never on the Qt owner. The report
        # describes this recipient including overrides, not universal pack counts.
        expected = adapter.revision()
        inventory = tuple((path.relative_to(adapter.root).as_posix(), adapter.native._export_path_state(path)[-1])
            for path in sorted((adapter.root / "data/defaults").glob("*.json")))
        report = import_module(adapter.package + ".foundation_dependencies").inspect_foundation_capabilities(
            adapter.root, source_inventory=inventory, revision=adapter.revision, expected_revision=expected)
        return replace(choice, capability_report=report, capability_inventory=inventory,
                       capability_revision=expected, capability_error="")
    except Exception as error:
        return replace(choice, capability_report=None, capability_inventory=(), capability_revision="",
                       capability_error=str(error))


@dataclass(frozen=True)
class _Completion:
    choice: TagDatabaseChoice
    result: object = None
    error: str = ""


class _Operation:
    def __init__(self, kind):
        self.kind = kind
        self.cancelled = Event()
        self.lock = Lock()
        self.admitted = False
        self.phase = "Waiting for background admission"

    def enter(self):
        with self.lock:
            if self.cancelled.is_set():
                self.phase = "Cancelled before admission"
                return False
            self.admitted = True
            self.phase = {
                "preview": "Preparing native plan and protected-edit review",
                "apply": "Applying native transaction; then verifying and publishing snapshot",
                "recover": "Verifying selected receipt and restoring native transaction",
                "reload": "Verifying saved catalog and publishing snapshot",
                "observe": "Refreshing installed release and exact recovery receipts",
                "connection_review": "Verifying selected source and catalog for connection review",
                "connection_confirm": "Connecting the exact reviewed source and catalog",
                "connection_open": "Verifying the saved catalog connection",
            }[self.kind]
            return True

    def cancel(self):
        with self.lock:
            self.cancelled.set()
            return not self.admitted


def _execute(operation, choice, action, review, receipt, ui_owner):
    if get_ident() == ui_owner:
        raise RuntimeError("Native Tag operations require the host background executor")
    if not operation.enter():
        return None
    service = choice.transactions
    saved_result = None
    try:
        if operation.kind in {"preview", "apply"}:
            choice = _acquire_foundation_choice(choice)
        if operation.kind == "observe":
            return _Completion(_observe(choice), True)
        if operation.kind == "preview":
            choice = _observe(choice)
            if choice.observation_status in {"unknown", "recovery_pending"}:
                raise RuntimeError("Installed release needs approved evidence or exact pending recovery before preview")
            action = DatabaseAction.MERGE_UPDATE if choice.installed else DatabaseAction.INSTALL_DEFAULT
            if choice.installed and choice.available_release and choice.base_release.fingerprint == choice.available_release.fingerprint:
                raise RuntimeError("This approved release is already installed. Supply the next approved release before update review")
        if operation.kind == "recover" and choice.native_adapter is not None:
            with choice.native_adapter.guard():
                fresh = _observe(choice)
                if dict(fresh.receipt_stamps).get(receipt) != dict(choice.receipt_stamps).get(receipt) or receipt not in fresh.recovery_receipts:
                    raise RuntimeError("Selected native receipt changed; refresh host observations before recovery")
                service.recover(receipt)
            return _Completion(_observe(choice), True)
        if operation.kind == "preview":
            mode = "merged" if action is DatabaseAction.INSTALL_DEFAULT and empty_default_layer(choice.native_adapter) else None
            result = service.preview(action, mode=mode, base_release=choice.base_release,
                available_release=choice.available_release, pack=choice.pack,
                installed=choice.installed, assignments=choice.assignments, offers=choice.offers)
        elif operation.kind == "apply":
            result = service.apply(review, confirmed_token=review.token)
            saved_result = result
        elif operation.kind == "recover":
            service.recover(receipt); result = True
        else:
            result = service.reload()
        return _Completion(_observe(choice, include_capabilities=operation.kind != "apply")
            if operation.kind != "preview" else choice, result)
    except Exception as error:
        try:
            refreshed = _observe(choice, include_capabilities=operation.kind != "apply")
        except Exception as observation_error:
            return _Completion(replace(choice, observation_status="unknown", recovery_receipts=(), receipt_stamps=(),
                capability_report=None, capability_inventory=(), capability_revision="",
                capability_error="Host observation failed; refresh observations after resolving its native hold."),
                result=saved_result, error=str(error) + "\nHost observation held: " + str(observation_error))
        return _Completion(refreshed, result=saved_result, error=str(error))


def _connect(operation, settings_path, values, ui_owner):
    if get_ident() == ui_owner:
        raise RuntimeError("Native connection requires the host background executor")
    if not operation.enter():
        return None
    from core.tag_catalog_connection import prepare_connection, confirm_connection, open_saved_connection
    if operation.kind == "connection_review":
        return prepare_connection(settings_path, *values)
    if operation.kind == "connection_confirm":
        review, = values
        return confirm_connection(review, review.token)
    return open_saved_connection(settings_path)


class TagDatabaseController:
    """One serialized host Future; native service retains all transaction authority."""
    def __init__(self, *, choices=(), executor=None, apply_authorized=False, settings_path=None, owns_executor=False, before_baseline_change=None):
        if (type(choices) is not tuple or len(choices) > 32
                or any(type(c) is not TagDatabaseChoice for c in choices)
                or type(apply_authorized) is not bool
                or executor is not None and not callable(getattr(executor, "submit", None))):
            raise ValueError("Explicit choices, host executor and operation authority required")
        self.choices, self.executor, self.apply_authorized = choices, executor, apply_authorized
        self.settings_path = Path(settings_path) if settings_path is not None else None
        self.owns_executor = owns_executor
        if before_baseline_change is not None and not callable(before_baseline_change):
            raise TypeError('Host baseline invalidation callback required')
        self.before_baseline_change = before_baseline_change
        self.connection_review = None
        self.index = 0 if choices else -1
        self.review = None
        self.result = None
        self.error = ""
        self.reason = "Choose a catalog and preview approved content."
        self.closed = self.faulted = False
        self._owner = get_ident()
        self._future = self._operation = self._binding = None
        self._unconfirmed = False
        self.last_operation = ""

    def _check_owner(self):
        if get_ident() != self._owner:
            raise RuntimeError("Tag UI admission/delivery belongs to its UI owner")

    def _invalidate_baseline(self):
        if self.before_baseline_change is not None:
            self.before_baseline_change()

    @property
    def choice(self):
        return self.choices[self.index] if self.index >= 0 else None

    @property
    def pending(self):
        return self._future is not None

    @property
    def close_held(self):
        return self.pending or self._unconfirmed or bool(self.owns_executor and self.executor.close_held)

    @property
    def can_connect(self):
        return bool(self.settings_path and self.executor and not self.close_held and not self.closed and not self.faulted)

    def connect(self, kind, *, source="", catalog="", create_new=False, confirmed=False):
        self._check_owner()
        if kind not in {"connection_review", "connection_confirm", "connection_open"}:
            raise ValueError("Unknown connection operation")
        if not self.can_connect:
            return False
        if kind == "connection_confirm" and (not confirmed or self.connection_review is None):
            self.reason = "Review and confirm this exact source and catalog connection."
            return False
        values = ((source, catalog, create_new) if kind == "connection_review" else
                  (self.connection_review,) if kind == "connection_confirm" else ())
        self._invalidate_baseline()
        self.connection_review = self.review = self.result = None
        self.error = ""
        operation = _Operation(kind)
        self._operation = operation
        try:
            future = self.executor.submit(_connect, operation, self.settings_path, values, self._owner)
            if not isinstance(future, Future):
                raise TypeError("Host executor supplied no completion handle")
        except Exception:
            self.faulted = self._unconfirmed = True
            self.reason = "Submission failed; retain host ownership and connection evidence."
            return False
        self._future = future
        self.reason = operation.phase
        return True

    def discard_connection(self):
        self._check_owner()
        self._invalidate_baseline()
        self.connection_review = None
        if not self.pending:
            self.reason = "Connection selection changed. Review again before connecting."

    @property
    def can_request(self):
        return bool(self.choice and self.executor and not self.close_held and not self.closed and not self.faulted)

    @property
    def can_apply(self):
        return bool(self.can_request and self.apply_authorized and self.review and self.review.can_apply
                    and self.choice.observation_status in {"supplied", "current"})

    @property
    def capability_state(self):
        choice = self.choice
        if choice is None or choice.native_adapter is None or choice.native_adapter.foundation_proposal is None:
            return "not_applicable"
        report = choice.capability_report
        if report is None:
            return "missing"
        if (self.pending or report.version != 1 or report.source_inventory != choice.capability_inventory
                or choice.capability_revision != choice.transactions.session.binding.authority_revision
                or choice.observation_status != "current"):
            return "stale"
        if not report.preservation_supported:
            return "held"
        if self.review is not None:
            return "preview"
        return "installed" if choice.installed else "observed"

    @property
    def phase(self):
        if self._operation is None:
            return self.reason
        with self._operation.lock:
            return self._operation.phase

    @property
    def cancel_requested(self):
        return bool(self.pending and self._operation.cancelled.is_set())

    def select(self, index):
        self._check_owner()
        if self.close_held or self.closed:
            return False
        if not 0 <= index < len(self.choices):
            raise ValueError("Unknown approved catalog")
        self._invalidate_baseline()
        self.index = index
        self.review = self.result = None
        self.error = ""
        self.reason = "Selection changed. Request a fresh review."
        return True

    def replace_choice(self, index, choice):
        self._check_owner()
        if self.close_held or self.closed:
            raise RuntimeError("Drain current operation before replacing approved inputs")
        if type(choice) is not TagDatabaseChoice or not 0 <= index < len(self.choices):
            raise ValueError("Explicit approved choice required")
        self._invalidate_baseline()
        rows = list(self.choices); rows[index] = choice; self.choices = tuple(rows)
        self.select(self.index)

    def request(self, kind, *, confirmed=False, receipt=None):
        self._check_owner()
        if kind not in {"preview", "apply", "recover", "reload", "observe"}:
            raise ValueError("Unknown Tag operation")
        if not self.can_request:
            return False
        if kind == "apply" and (not confirmed or not self.can_apply):
            self.reason = "Confirm the exact current review before applying."
            return False
        if kind == "recover" and (not self.apply_authorized or receipt not in self.choice.recovery_receipts):
            self.reason = "Select an explicitly approved native receipt; recovery authority is required."
            return False
        choice, review = self.choice, self.review
        self._invalidate_baseline()
        action = DatabaseAction.MERGE_UPDATE if choice.installed else DatabaseAction.INSTALL_DEFAULT
        operation = _Operation(kind)
        self._binding = choice.transactions.session.binding
        self.connection_review = None
        self.review = self.result = None
        self.error = ""
        self._operation = operation
        try:
            future = self.executor.submit(_execute, operation, choice, action, review, receipt, self._owner)
            if not isinstance(future, Future):
                raise TypeError("Host executor supplied no completion handle")
        except Exception:
            operation.cancel()
            self.faulted = self._unconfirmed = True
            self.reason = "Submission failed; completion is unknown. Retain host ownership and recovery evidence."
            return False
        self._future = future
        self.reason = operation.phase
        return True

    def cancel(self):
        self._check_owner()
        self.review = None
        self.connection_review = None
        if self._operation and self.pending:
            before = self._operation.cancel()
            self.reason = ("Cancellation requested before admission; waiting for worker completion." if before
                else "Cancellation requested. Native atomic work must finish; retain its saved outcome and recovery.")
        else:
            self.reason = "Preview discarded. No operation requested."

    def poll(self):
        self._check_owner()
        if not self.pending or not self._future.done():
            return False
        self._invalidate_baseline()
        future, operation = self._future, self._operation
        self._future = self._operation = None
        self.last_operation = operation.kind
        try:
            result = future.result()
        except Exception as error:
            self.error = str(error)
            self.reason = "Operation held or interrupted. Review the reason and exact native recovery evidence."
            return True
        if isinstance(result, _Completion):
            rows = list(self.choices); rows[self.index] = result.choice; self.choices = tuple(rows)
            if result.error:
                self.result = result.result
                self.error = result.error
                self.reason = ("Native changes saved; host observation held. Refresh observations; do not apply again." if result.result is not None else
                    "Operation held or interrupted. Refresh observations or recover the exact selected import.")
                return True
            result = result.result
        if result is None:
            self.reason = "Cancelled before admission. No native operation was started."
        elif operation.kind == "connection_review":
            if operation.cancelled.is_set() or self.closed:
                self.reason = "Connection review discarded. Review again before connecting."
            else:
                self.connection_review = result
                self.reason = "Connection review ready. Confirm the source, catalog and creation decision."
        elif operation.kind in {"connection_confirm", "connection_open"}:
            self.choices = (result.choice,)
            self.index = 0
            self.result = result
            self.reason = "Catalog connected. Preview approved content or recover the selected interrupted import."
            if operation.cancelled.is_set():
                self.reason += " Cancellation arrived after admission; the saved connection is retained."
        elif operation.kind == "preview":
            if operation.cancelled.is_set() or self.closed or self.choice.transactions.session.binding != self._binding:
                self.reason = "Cancelled or stale preview discarded. Request a fresh review."
            else:
                self.review = result
                self.reason = "Review ready. Inspect changes and protected edits before confirming." if result.can_apply else "Review held. Inspect conflicts and native reasons."
        else:
            self.result = result
            if operation.kind == "apply" and result.publication_error:
                self.error = result.publication_error
                self.reason = "Native changes saved; snapshot refresh failed. Reload saved catalog; do not apply again."
            else:
                self.reason = ("Host observations refreshed. Review the installed release or exact pending recovery." if operation.kind == "observe" else
                    "Recovery verified; saved catalog and snapshot reloaded." if operation.kind == "recover" else "Saved catalog verified and snapshot reloaded.")
            if operation.cancelled.is_set():
                self.reason += " Cancellation arrived after admission; the saved outcome is retained."
        return True

    def begin_close(self):
        self._check_owner()
        self.closed = True
        self.cancel()
