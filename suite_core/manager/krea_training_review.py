"""Read-only Krea package review owned by the Manager window."""
from functools import partial
from pathlib import Path
from threading import Event

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from .resource_executor import ResourceExecutorOwner


class _StaleAnimaPackage(ValueError):
    pass


class _StaleAnimaInventory(ValueError):
    pass


class _StaleAnimaSetup(ValueError):
    pass


def resolve_manager_saved_sdxl_setup(settings_path, registry_path, *, should_cancel, progress=None):
    from core.model_selection import protected_model_roots
    from core.model_resource_registry import ModelResourceRegistry
    from core.settings import AppSettings
    from core.training_setup_resources import SavedTrainingSetupResolver
    from core.training_setup_selection import _require_disjoint_protected_paths
    from core.training_source_io import read_stable_training_source
    from qt_app.model_companion_workflow import resource_boundaries

    path = Path(settings_path)
    if should_cancel() or Path(registry_path) != path.parent / "model_resources.json":
        raise InterruptedError("The Manager review session ended.")
    raw, _ = read_stable_training_source(path, max_bytes=16 * 1024 * 1024)
    settings = AppSettings._from_bytes(raw)
    registry = ModelResourceRegistry(registry_path)
    _require_disjoint_protected_paths((registry.path,), protected_model_roots(settings))
    models, model_revision = registry.snapshot()
    protected, _ = resource_boundaries(models, settings)
    resolver = SavedTrainingSetupResolver(path.parent / "training-setup.json")
    _require_disjoint_protected_paths((resolver.selection.path, resolver.profile_path), protected)
    _, revision = resolver.selection.snapshot()
    result = resolver(should_cancel=should_cancel, progress=progress, protected_roots=protected)
    if should_cancel():
        raise InterruptedError("Saved Trainer Setup loading was cancelled.")
    current, _ = read_stable_training_source(path, max_bytes=16 * 1024 * 1024)
    if (current != raw or registry.snapshot()[1] != model_revision
            or resolver.selection.snapshot()[1] != revision):
        raise ValueError("Trainer Setup or profile settings changed. Load Saved Trainer again.")
    return result


class KreaTrainingReviewDialog(QDialog):
    def __init__(self, package_path, registry_path, parent, *, dataset_verifier=None, preview_jobs=None,
                 anima_settings_path=None, anima_review_allowed=None, anima_history_root=None,
                 expected_package_fingerprint=None, open_trainer_setup=None, open_sdxl_preparation=None):
        super().__init__(parent)
        self.setWindowTitle("Training Package Review")
        self.resize(850, 630)
        self._executor = ResourceExecutorOwner()
        self._cancel = Event()
        self._dataset_verifier = dataset_verifier
        self._verification = None
        self._expected_package_fingerprint = expected_package_fingerprint
        self._open_trainer_setup = open_trainer_setup
        self._open_sdxl_preparation = open_sdxl_preparation
        self._handoff_folder = None
        self._checking_dataset = False
        self._preparing_dataset = False
        self._prepared_folder = None
        self._retry_history_save = None
        self._saving_preparation = False
        self._dataset_overview = None
        self._checking_handoff = False
        self._handoff_fingerprint = None
        self._krea_package = False
        self._preview_jobs = preview_jobs
        self._registry_path = registry_path
        self._anima_settings_path = anima_settings_path
        self._anima_review_allowed = anima_review_allowed
        self._anima_history_root = anima_history_root
        self._anima_preflight = None
        self._anima_base_id = None
        self._anima_review = None
        self._checking_anima_setup = False
        self._adapter_preview = None
        self._preparation_setup = None

        layout = QVBoxLayout(self)
        heading = QLabel("Review a training package")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        guidance = ["Review a verified SDXL LoRA, Krea 2 LoRA, Anima LoRA, or collection-bound package for manual handoff."]
        if registry_path is not None:
            guidance.append("Krea 2 LoRA packages also get a registered-base check.")
        else:
            guidance.append("A Krea 2 registered-base check needs an admitted model registry.")
        if dataset_verifier is not None:
            guidance.append("When available, Verify SDXL Dataset inspects the package in an isolated worker.")
        guidance.append("Training cannot start here.")
        note = QLabel(" ".join(guidance))
        note.setWordWrap(True)
        layout.addWidget(note)
        self.status = QLabel("Checking the selected package…")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.tabs = QTabWidget()
        self.sections = []
        for title in ("Readiness", "Settings", "Captions"):
            section = QPlainTextEdit()
            section.setReadOnly(True)
            section.setAccessibleName(f"Training package {title.lower()}")
            self.tabs.addTab(section, title)
            self.sections.append(section)
        self.tabs.setEnabled(False)
        self.dataset_button = QPushButton("Verify SDXL Dataset…")
        self.dataset_button.hide()
        self.dataset_button.clicked.connect(self._verify_dataset)
        layout.addWidget(self.dataset_button, 0, Qt.AlignLeft)
        self.prepare_button = QPushButton("Prepare SDXL Dataset…")
        self.prepare_button.hide()
        self.prepare_button.clicked.connect(self._prepare_dataset)
        layout.addWidget(self.prepare_button, 0, Qt.AlignLeft)
        self.adapter_button = QPushButton("Review SDXL Trainer Settings…")
        self.adapter_button.hide()
        self.adapter_button.clicked.connect(self._open_adapter_preview)
        layout.addWidget(self.adapter_button, 0, Qt.AlignLeft)
        self.anima_button = QPushButton("Verify Anima Model Files…")
        self.anima_button.hide()
        self.anima_button.clicked.connect(self._open_anima_verification)
        layout.addWidget(self.anima_button, 0, Qt.AlignLeft)
        self.anima_setup_button = QPushButton("Check Anima Readiness")
        self.anima_setup_button.hide()
        self.anima_setup_button.clicked.connect(self._check_anima_setup)
        layout.addWidget(self.anima_setup_button, 0, Qt.AlignLeft)
        self.anima_prepare_button = QPushButton("Prepare Anima Run")
        self.anima_prepare_button.hide()
        self.anima_prepare_button.clicked.connect(self._prepare_anima)
        layout.addWidget(self.anima_prepare_button, 0, Qt.AlignLeft)
        layout.addWidget(self.tabs, 1)
        actions = QHBoxLayout()
        self.folder_button = QPushButton("Open Package Folder")
        self.folder_button.setToolTip("Find the verified package for use in your external trainer.")
        self.folder_button.hide()
        self.folder_button.clicked.connect(self._open_package_folder)
        actions.addWidget(self.folder_button)
        self.prepared_folder_button = QPushButton("Open Prepared Workspace")
        self.prepared_folder_button.hide()
        self.prepared_folder_button.clicked.connect(self._open_prepared_folder)
        actions.addWidget(self.prepared_folder_button)
        self.retry_history_button = QPushButton("Retry Saving to History")
        self.retry_history_button.hide()
        self.retry_history_button.clicked.connect(self._retry_preparation_history)
        actions.addWidget(self.retry_history_button)
        actions.addStretch()
        self.return_button = QPushButton("Cancel Check and Return")
        self.return_button.setMinimumWidth(self.return_button.fontMetrics().horizontalAdvance('Return to Applications') + 32)
        self.return_button.clicked.connect(self.reject)
        actions.addWidget(self.return_button)
        layout.addLayout(actions)

        self._future = self._executor.submit(self._review, package_path, registry_path)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._settle)
        self._timer.start(50)

    def _package_review_cancelled(self):
        return self._cancel.is_set() or (self._anima_review_allowed is not None
                                         and not self._anima_review_allowed())

    def _review(self, package_path, registry_path):
        from core.krea_training_compatibility import KreaTrainingCompatibilityService
        from core.model_resource_registry import ModelResourceError, ModelResourceRegistry
        from core.training_configuration import TrainingConfiguration
        from core.training_package import TrainingPackageService, TrainingPackageVerificationLimits

        limits = TrainingPackageVerificationLimits()
        verification = TrainingPackageService().verify(
            package_path, should_cancel=self._package_review_cancelled, limits=limits)
        if self._package_review_cancelled():
            raise InterruptedError("Training package review was cancelled.")
        if verification.inspection_limited:
            raise ValueError("The package exceeds the review limits. Choose a smaller package.")
        if not verification.valid:
            raise ValueError(
                "The selected package could not be verified. If any files changed or are missing, "
                "export a fresh package from Training. Then return to Applications and select "
                "the package folder again."
            )
        if (self._expected_package_fingerprint is not None
                and verification.package_fingerprint != self._expected_package_fingerprint):
            raise ValueError("The published package changed. Review its recovery receipt again.")
        configuration = TrainingConfiguration.from_dict(verification.manifest["training_configuration"])
        if configuration.target_model_family.casefold() not in {"krea 2", "krea2"} or configuration.output_kind != "lora":
            lora_review = (configuration.target_model_family.casefold() in {"sdxl", "anima"}
                           and configuration.output_kind == "lora")
            if "collection_version" not in verification.manifest and not lora_review:
                available = ["a collection-bound package"]
                if registry_path is not None:
                    available.append("a Krea 2 LoRA package")
                available.extend(("an SDXL LoRA package", "an Anima LoRA package"))
                raise ValueError("Return to Applications and choose " + " or ".join(available) + ".")
            registration = None
            if configuration.target_model_family.casefold() == "anima" and configuration.output_kind == "lora":
                registration = self._anima_registration(configuration, registry_path)
            return verification, configuration, registration
        if registry_path is None:
            return verification, configuration, None
        try:
            report = KreaTrainingCompatibilityService().report(
                verification, ModelResourceRegistry(registry_path), should_cancel=self._package_review_cancelled, limits=limits,
            )
        except ModelResourceError:
            current = TrainingPackageService().verify(
                package_path, should_cancel=self._package_review_cancelled, limits=limits,
                expected_package_fingerprint=verification.package_fingerprint,
            )
            if (not current.valid or current.inspection_limited
                    or current.package_fingerprint != verification.package_fingerprint):
                raise ValueError("The package changed or could not be rechecked. Select a current package.") from None
            return current, configuration, None

        return verification, configuration, report

    @staticmethod
    def _anima_registration(configuration, registry_path):
        from core.model_resource_registry import ModelResourceError, ModelResourceRegistry
        from core.training_anima import (
            AnimaError, RESOURCE_ADAPTER_ID, RESOURCE_PROFILE_ID, SOURCE_REVISION, anima_resource_contract,
        )

        try:
            contract = anima_resource_contract(
                workflow_kind="training", profile_id=RESOURCE_PROFILE_ID, adapter_id=RESOURCE_ADAPTER_ID,
                adapter_version=SOURCE_REVISION, main_resource_id=configuration.base_model_id,
            )
        except AnimaError:
            return "This package does not identify the exact supported Anima Base model."
        if registry_path is None:
            return "Model registrations are unavailable in this Manager session."
        try:
            snapshot, _ = ModelResourceRegistry(registry_path).snapshot()
        except (ModelResourceError, OSError, ValueError):
            return "The model registry could not be read. Use the app that manages this registry to resolve the problem, then reopen this package."

        labels = {"main": "Anima Base", "text_encoder": "Qwen3 text encoder", "vae": "Qwen-Image VAE"}
        resources = snapshot["resources"]
        lines = []
        for requirement in contract.requirements:
            record = resources.get(requirement.resource_id, {})
            if not record.get("locations"):
                status = "no local file registered"
            elif not any(
                declaration["role"] == requirement.role
                and declaration["family"].casefold() == requirement.family.casefold()
                for declaration in record["declarations"]
            ):
                status = "registered with a different or missing role or family"
            else:
                status = "registered for this role"
            lines.append(f"{labels[requirement.slot]}: {status}")
        lines.append("Registration only. Model files and trainer readiness have not been verified.")
        return "\n".join(lines)

    def _check_anima_setup(self):
        self._start_anima_check()

    def _prepare_anima(self):
        if self._anima_preflight is not None and self._anima_history_root is not None:
            self._start_anima_check(self._anima_preflight)

    def _start_anima_check(self, reviewed=None):
        if (self._checking_anima_setup or self._anima_base_id is None or self._anima_review is not None
                or self._retry_history_save is not None
                or self._cancel.is_set() or not self._anima_review_allowed()):
            return
        self._checking_anima_setup = True
        self._anima_preflight = None
        self.anima_prepare_button.hide()
        self.sections[0].setPlainText(self._anima_registration_overview)
        self.anima_setup_button.setEnabled(False)
        self.anima_button.setEnabled(False)
        self.folder_button.setEnabled(False)
        self.return_button.setText("Cancel and Return")
        self.status.setText("Preparing the reviewed Anima run…" if reviewed is not None
                            else "Checking the Anima package, trainer setup and model files…")
        self._future = self._executor.submit(partial(self._review_anima_setup, reviewed))
        self._timer.start(50)

    def _review_anima_setup(self, reviewed=None):
        from contextlib import nullcontext
        from hashlib import sha256
        from core.model_selection import protected_model_roots
        from core.settings import AppSettings
        from tempfile import TemporaryDirectory, mkdtemp

        from core.model_resource_registry import ModelResourceRegistry
        from core.training_anima import AnimaTrainingService
        from core.training_file_inventory import SoftwareInventoryService
        from qt_app.model_companion_workflow import resource_boundaries
        from qt_app.training_anima_dialog import anima_package_settings
        from core.training_setup_resources import SavedTrainingSetupResolver
        from core.training_setup_selection import _require_disjoint_protected_paths
        from core.training_source_io import _assert_unredirected, read_stable_training_source

        def cancelled():
            return self._cancel.is_set() or not self._anima_review_allowed()

        settings_path = Path(self._anima_settings_path)
        if cancelled() or Path(self._registry_path) != settings_path.parent / "model_resources.json":
            raise InterruptedError("The Manager review session ended.")
        raw, _ = read_stable_training_source(settings_path, max_bytes=16 * 1024 * 1024)
        settings = AppSettings._from_bytes(raw)
        protected = protected_model_roots(settings)
        registry = ModelResourceRegistry(self._registry_path)
        _require_disjoint_protected_paths((registry.path,), protected)
        model_snapshot, model_revision = registry.snapshot()
        protected, managed = resource_boundaries(model_snapshot, settings)
        resolver = SavedTrainingSetupResolver(settings_path.parent / "training-setup.json")
        _require_disjoint_protected_paths((resolver.selection.path, resolver.profile_path), protected)
        _, revision = resolver.selection.snapshot()

        def require_current():
            if cancelled():
                raise InterruptedError("The Manager review session ended.")
            current, _ = read_stable_training_source(settings_path, max_bytes=16 * 1024 * 1024)
            if (current != raw or resolver.selection.snapshot()[1] != revision
                    or registry.snapshot()[1] != model_revision):
                raise _StaleAnimaSetup

        setup = resolver(should_cancel=cancelled, protected_roots=protected)
        require_current()
        package = self._verification
        run_settings, _ = anima_package_settings(package)
        authority = (sha256(raw).hexdigest(), revision, model_revision)
        if reviewed is not None and authority != reviewed[3:]:
            raise _StaleAnimaSetup
        service = AnimaTrainingService(registry, protected_roots=protected, managed_roots=managed)
        context = TemporaryDirectory(prefix="collectigen-anima-review-") if reviewed is None else nullcontext()
        with context as temporary:
            if reviewed is None:
                output = Path(temporary)
            else:
                history_root = Path(self._anima_history_root)
                _require_disjoint_protected_paths((history_root,), (*protected, package.destination,
                    setup["source_root"], setup["environment_root"], setup["python_base_root"]))
                _assert_unredirected(history_root)
                require_current()
                history_root.mkdir(parents=True, exist_ok=True)
                _assert_unredirected(history_root)
                output = Path(mkdtemp(prefix="anima-run-", dir=history_root))
            selection = service.prepare_selection(
                package.destination, {**setup, "output_root": str(output)}, output / ".anima-selection-review",
                should_cancel=cancelled)
            inventories = (
                (selection.environment_root, selection.environment_inventory),
                (selection.python_base_root, selection.python_base_inventory),
            )
            inventory = SoftwareInventoryService()
            try:
                fingerprints = tuple(inventory.load(root, manifest).fingerprint for root, manifest in inventories)
            except (OSError, RuntimeError, ValueError) as exc:
                raise _StaleAnimaInventory from exc
            preview = service.preview(selection, run_settings, should_cancel=cancelled)
            require_current()
            if preview.package_fingerprint != self._handoff_fingerprint:
                raise _StaleAnimaPackage
            try:
                directory = self._verify_handoff_folder(
                    self._handoff_folder, self._handoff_fingerprint, should_cancel=cancelled)
            except InterruptedError:
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                raise _StaleAnimaPackage from exc
            require_current()
            if preview.ready:
                try:
                    verified = tuple(inventory.verify(root, manifest, should_cancel=cancelled)["inventory_sha256"]
                                     for root, manifest in inventories)
                except InterruptedError:
                    raise
                except (OSError, RuntimeError, ValueError) as exc:
                    raise _StaleAnimaInventory from exc
                if verified != fingerprints:
                    raise _StaleAnimaInventory
            require_current()
            current = (preview.fingerprint, fingerprints, directory, *authority)
            if reviewed is None:
                return preview, directory, current, None
            if current[1] != reviewed[1]:
                raise _StaleAnimaInventory
            if not preview.ready or current != reviewed:
                raise _StaleAnimaSetup
            prepared = service.prepare(preview, output, "anima", should_cancel=cancelled)
            review_path = prepared.preparation_root / "anima-review.json"
            from core.training_retained_history import upsert_retained_review
            from core.training_run_index import TrainingRunIndex

            def save_history():
                if cancelled():
                    raise InterruptedError("The Manager review session ended.")
                _assert_unredirected(history_root)
                upsert_retained_review(TrainingRunIndex(history_root / "training-runs.json"), "anima",
                    {"prepared": prepared, "outcome": None, "registered": False})

            require_current()
            try:
                save_history()
            except (OSError, RuntimeError, ValueError):
                return preview, directory, current, (review_path, save_history)
            return preview, directory, current, (review_path, None)

    def _open_anima_verification(self):
        if (self._anima_review is not None or self._cancel.is_set()
                or self._anima_base_id is None or self._anima_review_allowed is None
                or not self._anima_review_allowed()):
            return
        from core.model_companion_readiness import CompanionReadinessRequest, CompanionReadinessService
        from core.model_resource_registry import ModelResourceRegistry
        from core.settings import AppSettings
        from core.training_anima import RESOURCE_ADAPTER_ID, RESOURCE_PROFILE_ID, SOURCE_REVISION
        from core.training_source_io import read_stable_training_source
        from qt_app.model_companion_readiness_dialog import ModelCompanionReadinessDialog
        from qt_app.model_companion_workflow import resource_boundaries

        settings_path = Path(self._anima_settings_path)
        registry = ModelResourceRegistry(self._registry_path)

        def settings():
            if not self._anima_review_allowed() or registry.path != settings_path.parent / "model_resources.json":
                raise ValueError("The Manager model review session is no longer available.")
            try:
                raw, _ = read_stable_training_source(settings_path, max_bytes=16 * 1024 * 1024)
                return AppSettings._from_bytes(raw)
            except (OSError, RuntimeError, ValueError):
                raise ValueError("Manager settings could not be checked. Reopen a current profile and try again.") from None

        def service_factory(snapshot):
            try:
                protected, managed = resource_boundaries(snapshot, settings())
            except ImportError:
                raise ValueError("Managed model boundaries could not be checked in this Manager build.") from None
            return CompanionReadinessService(registry, protected_roots=protected, managed_roots=managed)

        try:
            profile_settings = settings()
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        request = CompanionReadinessRequest(self._anima_base_id, "training", RESOURCE_PROFILE_ID,
                                            RESOURCE_ADAPTER_ID, SOURCE_REVISION)
        dialog = ModelCompanionReadinessDialog(registry, self._preview_jobs, self._anima_base_id, self,
            settings=profile_settings, service_factory=service_factory, initial_request=request, verification_only=True,
            verification_allowed=self._anima_review_allowed)
        self._anima_review = dialog
        dialog.finished.connect(lambda _: setattr(self, "_anima_review", None))
        dialog.show()

    def _settle(self):
        if not self._future.done() or self._executor.close_held:
            return
        self._timer.stop()
        if self._cancel.is_set():
            super().reject()
            return
        self.return_button.setText("Return to Applications")
        if self._checking_anima_setup:
            from core.training_anima import AnimaError

            self._checking_anima_setup = False
            session_ready = self._anima_review_allowed()
            self.anima_setup_button.setEnabled(session_ready)
            self.anima_button.setEnabled(session_ready)
            self.folder_button.setEnabled(True)
            if not session_ready:
                self.status.setText("The Manager session ended. Reopen package review to check the trainer setup again.")
                return
            try:
                preview, directory, preflight, prepared = self._future.result()
                try:
                    if self._handoff_directory_state(self._handoff_folder) != directory:
                        raise _StaleAnimaPackage
                except (OSError, RuntimeError, ValueError) as exc:
                    raise _StaleAnimaPackage from exc
            except _StaleAnimaPackage:
                self.anima_setup_button.setEnabled(False)
                self.anima_button.setEnabled(False)
                self._invalidate_package_review(
                    "The Anima package changed or could not be rechecked. Return to Applications and review it again.")
            except _StaleAnimaInventory:
                self.status.setText("Trainer files changed during preflight. Recheck Trainer Setup, then check Anima readiness again.")
            except _StaleAnimaSetup:
                self.status.setText("Settings, trainer setup or model registrations changed. Check Anima Readiness again.")
            except InterruptedError:
                self.status.setText("The Manager session ended. Reopen package review to check the trainer setup again.")
            except AnimaError as exc:
                self.status.setText(f"{exc} Review the saved trainer setup before retrying.")
            except (OSError, RuntimeError, ValueError, TypeError):
                self.status.setText("Saved trainer setup could not be verified. Review your trainer setup or use the package in an external trainer.")
            else:
                if prepared is not None:
                    path, retry = prepared
                    self.sections[0].appendPlainText(f"\nRetained review: {path}")
                    if retry is not None:
                        self._retry_history_save = retry
                        self.retry_history_button.setEnabled(True)
                        self.retry_history_button.show()
                        self.anima_setup_button.setEnabled(False)
                    self.status.setText("Anima run prepared. Review Saved Training Runs to reopen it. Training did not start."
                                        if retry is None else "Anima preparation was retained. Choose Retry Saving to History "
                                        "to make it available in Saved Training Runs. Training did not start.")
                elif preview.ready:
                    self._anima_preflight = preflight
                    self.anima_prepare_button.setVisible(self._anima_history_root is not None)
                    self.status.setText("Anima preflight passed. Prepare the run to save a review in Manager Training History."
                                        if self._anima_history_root is not None else
                                        "Anima preflight passed. This review cannot prepare or start training.")
                else:
                    from qt_app.training_anima_dialog import ISSUE_MESSAGES

                    reasons = "\n".join(ISSUE_MESSAGES.get(issue, issue.replace("_", " "))
                                        for issue in preview.issues)
                    self.status.setText("Anima preflight is held. See Readiness for details.")
                    self.sections[0].appendPlainText("\nReadiness checks to resolve\n" + reasons)
                    self.tabs.setCurrentIndex(0)
            return
        if self._checking_handoff:
            self._finish_package_handoff()
            return
        if self._saving_preparation:
            self._saving_preparation = False
            for action in (self.prepare_button, self.dataset_button, self.adapter_button, self.folder_button):
                action.setEnabled(True)
            if self._anima_base_id is not None:
                self.anima_button.setEnabled(self._anima_review_allowed())
            try:
                self._future.result()
            except Exception:
                self.retry_history_button.setEnabled(True)
                self.status.setText("The prepared workspace is available. Saving to History still failed.")
            else:
                self._retry_history_save = None
                self.retry_history_button.hide()
                if self._anima_base_id is not None:
                    self.anima_setup_button.setEnabled(self._anima_review_allowed())
                    self.status.setText("Anima preparation saved to History. Review Saved Training Runs to reopen it.")
                else:
                    self.status.setText("SDXL preparation saved to History. Training has not started.")
            return
        if self._preparing_dataset:
            self._preparing_dataset = False
            for action in (self.prepare_button, self.dataset_button, self.adapter_button, self.folder_button):
                action.setEnabled(True)
            from core.training_dataset_binding import TrainingDatasetAdmissionHold, TrainingPreparationRetentionError
            from core.training_preflight import TrainingPreflightWorkspacePathError

            try:
                inspection, plan, request, receipt = self._future.result()
            except TrainingPreflightWorkspacePathError as exc:
                self.status.setText(str(exc))
            except TrainingDatasetAdmissionHold as exc:
                self.status.setText("This package is outside the reviewed SDXL preparation profile. Manual handoff is still available.")
                self.sections[0].appendPlainText("\n" + self._dataset_hold_details(exc.holds))
                self.tabs.setCurrentIndex(0)
            except TrainingPreparationRetentionError as exc:
                self._show_prepared_workspace(exc.workspace, str(exc))
                if exc.retry is not None:
                    self._retry_history_save = exc.retry
                    self.retry_history_button.setEnabled(True)
                    self.retry_history_button.show()
            except Exception:
                if self._package_review_cancelled():
                    self.prepare_button.setEnabled(False)
                    self._invalidate_package_review(
                        "The Manager session ended before preparation completed. Return to Applications to reopen the package.")
                else:
                    self.status.setText("Preparation did not finish. Check the selected folders, then choose Prepare SDXL Dataset to retry.")
            else:
                if plan is None:
                    self.status.setText("The selected trainer software is not ready for dataset preparation. Choose Prepare SDXL Dataset to review the folders.")
                else:
                    self._show_prepared_workspace(
                        plan.root, "SDXL dataset prepared in Manager’s Training workspace. Training has not started.")
            return
        if self._checking_dataset:
            self._checking_dataset = False
            if self._package_review_cancelled():
                self._invalidate_package_review("The Manager session ended. Return to Applications to reopen the package.")
                return
            self.adapter_button.setEnabled(True)
            self.folder_button.setEnabled(True)
            self._show_dataset_receipt()
            return
        if self._package_review_cancelled():
            self.status.setText("The Manager session ended. Return to Applications to reopen the package.")
            return
        try:
            verification, configuration, report = self._future.result()
        except ValueError as exc:
            self.status.setText(f"Compatibility review unavailable. {exc}")
            return
        except Exception:
            self.status.setText("Compatibility review could not finish. Return to Applications and select a current package.")
            return
        self._handoff_folder = verification.destination
        self._handoff_fingerprint = verification.package_fingerprint
        self._krea_package = (configuration.target_model_family.casefold() in {"krea 2", "krea2"}
                              and configuration.output_kind == "lora")
        if self._krea_package:
            self.folder_button.setToolTip(
                "Inspect the verified package files. The Krea 2 trainer cannot use this folder directly."
            )
        self.folder_button.show()
        identity = f"Package fingerprint: {verification.package_fingerprint}"
        binding = verification.manifest.get("collection_version")
        if binding is not None:
            identity += (f"\nCollection version: {binding['version_id']}"
                         f"\nCandidate plan: {binding['candidate_plan_fingerprint']}")
        anima = configuration.target_model_family.casefold() == "anima" and configuration.output_kind == "lora"
        if report is None or anima:
            status = "Package verified for manual handoff. Trainer readiness is unverified."
            if anima:
                status = "Anima package verified for manual handoff. Use the verified package in an external trainer."
            if self._krea_package:
                status = "Package verified for manual handoff. Krea 2 compatibility review needs an admitted model registry."
                if self._registry_path is not None:
                    status = "Package verified for manual handoff. The model registry needs attention before Krea 2 compatibility review."
            self.status.setText(status)
            self.tabs.setTabText(2, "Dataset")
            self.sections[2].setAccessibleName("Training dataset")
            self.sections[0].setPlainText(identity + (
                "\n\nRegistered base and trainer readiness are unverified." if self._krea_package else ""))
            if anima:
                from core.training_anima import BASE_ID
                self.sections[0].appendPlainText("\nModel registrations\n" + report)
                if (configuration.base_model_id == BASE_ID and self._registry_path is not None
                        and self._anima_settings_path is not None and self._preview_jobs is not None
                        and self._anima_review_allowed is not None):
                    self._verification = verification
                    self._anima_base_id = BASE_ID
                    self._anima_registration_overview = self.sections[0].toPlainText()
                    self.anima_button.show()
                    self.anima_setup_button.show()
            if (self._registry_path is not None and self._preview_jobs is not None
                    and configuration.target_model_family.casefold() == "sdxl"
                    and configuration.output_kind == "lora"):
                from core.training_adapter_preview import SdScriptsSdxlLoraPreviewService

                try:
                    SdScriptsSdxlLoraPreviewService.require_supported_configuration(configuration)
                except ValueError as exc:
                    self.status.setText(f"Trainer Settings unavailable. {exc}")
                else:
                    self._verification = verification
                    self.adapter_button.show()
            if (self._dataset_verifier is not None
                           and configuration.target_model_family.casefold() == "sdxl"
                           and configuration.output_kind == "lora"):
                self._verification = verification
                self.dataset_button.show()
                from core.training_dataset_binding import ReviewedDatasetVerifier

                if type(self._dataset_verifier) is ReviewedDatasetVerifier and self._preview_jobs is not None:
                    from core.training_adapter_preview import SdScriptsSdxlLoraPreviewService

                    try:
                        SdScriptsSdxlLoraPreviewService.require_supported_configuration(configuration)
                    except ValueError:
                        pass
                    else:
                        self.prepare_button.show()
            self.sections[1].setPlainText(
                f"Model family: {configuration.target_model_family}\n"
                f"Model version: {configuration.target_model_version or 'Unspecified'}\n"
                f"Base model reference: {configuration.base_model_id or 'Unspecified'}\n"
                f"Expected output: {configuration.output_kind}\n"
                f"Output name: {configuration.output_name}\n\n"
                "Requested settings for manual handoff\n"
                f"Package adapter: {configuration.adapter_id} (version {configuration.adapter_version})\n"
                f"Settings schema: {configuration.schema_version}\n"
                "Copy the requested settings into your trainer. This package does not contain a trainer-specific configuration.\n\n"
                f"Target resolution: {configuration.target_resolution}\n"
                f"Bucket policy: {configuration.bucket_policy}\n"
                f"Crop policy: {configuration.crop_policy}\n"
                f"Allow upscale: {'Yes' if configuration.allow_upscale else 'No'}\n"
                f"Image transform policy: {configuration.image_transform_policy}\n"
                f"Caption prefix policy: {configuration.caption_prefix_policy}\n"
                f"Trigger term: {configuration.trigger_term or 'None'}\n"
                f"Class term: {configuration.class_term or 'None'}\n"
                f"Maximum caption tokens: {configuration.max_caption_tokens}\n\n"
                f"Repeats: {configuration.repeats}\n"
                f"Epochs: {configuration.epochs}\n"
                f"Batch size: {configuration.batch_size}\n"
                f"Gradient accumulation: {configuration.gradient_accumulation}\n"
                f"Learning rate: {configuration.learning_rate}\n"
                f"Seed: {configuration.seed}\n"
                f"Precision: {configuration.precision}\n"
                f"Regularization policy: {configuration.regularization_policy}\n"
                f"Validation policy: {configuration.validation_policy}\n\n"
                "Verify these settings in your Trainer before starting a run.")
            if anima:
                from qt_app.training_anima_dialog import anima_package_settings

                try:
                    _, explanation = anima_package_settings(verification)
                except ValueError as exc:
                    explanation = f"Anima adapter preview unavailable. {exc}"
                self.sections[1].appendPlainText("\nAnima adapter preview\n" + explanation)
            self._append_companions(verification.manifest)
            self.sections[2].setPlainText(self._dataset_text(verification.manifest))
            captions = QPlainTextEdit()
            captions.setReadOnly(True)
            captions.setAccessibleName("Training captions")
            captions.setPlainText(self._manual_captions(verification.manifest))
            self.tabs.addTab(captions, "Captions")
            self.tabs.setEnabled(True)
            return
        base = ("Exact registered base bytes verified" if report.base_status == "identity_verified"
                else "Exact registered base unavailable")
        self.status.setText(f"{base}. Training readiness is unverified.")
        captions = []
        for item in report.images[:100]:
            captions.extend((
                f"{item.stable_id} ({item.role}, {item.effective_exposures} exposures)",
                item.caption[:4096],
            ))
            if len(item.caption) > 4096:
                captions.append("Caption preview shortened. See the verified caption file for the full text.")
            captions.append("")
        sections = (
            f"{identity}\nRegistered base: {report.base_resource_id}\n\n"
            + "\n\n".join(f"{item.summary}\n{item.reason}\n{item.next_action}" for item in report.requirements),
            "\n\n".join(f"{item.field} [{item.disposition}]\nRequested: {item.requested}\n{item.reason}"
                       for item in report.settings),
            "\n".join(captions),
        )
        for widget, text in zip(self.sections, sections):
            widget.setPlainText(text)
        self._append_companions(verification.manifest)
        if len(report.images) > 100:
            self.sections[2].appendPlainText(
                f"\nShowing 100 of {len(report.images):,} captions. See the verified package for the full set."
            )
        dataset = QPlainTextEdit()
        dataset.setReadOnly(True)
        dataset.setAccessibleName("Training dataset")
        dataset.setPlainText(self._dataset_text(verification.manifest))
        self.tabs.addTab(dataset, "Dataset")
        self.tabs.setEnabled(True)

    def _append_companions(self, manifest):
        companions = manifest["training_configuration"].get("model_guidance", {}).get("bindings", [])
        if companions:
            rows = []
            for binding in companions:
                role = binding.get("role")
                role = role if isinstance(role, str) and role.strip() else "role unspecified"
                resource_id = binding.get("resource_id") or "Unspecified"
                rows.append(f"{binding['slot']} ({role}): {resource_id}")
            self.sections[1].appendPlainText(
                "\nRecorded model companions (verify in your Trainer)\n" + "\n".join(rows)
            )

    def _show_prepared_workspace(self, path, status):
        self.sections[0].appendPlainText(f"\nPrepared workspace: {path}")
        self.tabs.setCurrentIndex(0)
        try:
            folder = Path(path)
            root = self._dataset_verifier.work_root
            if not folder.is_absolute() or ".." in folder.parts or folder == root or not folder.is_relative_to(root):
                raise ValueError("Preparation workspace outside the reviewed worker root")
            self._prepared_folder = folder, self._handoff_directory_state(folder)
        except (OSError, RuntimeError, ValueError):
            status += " The workspace could not be verified for opening."
        else:
            self.prepared_folder_button.show()
        self.status.setText(status)

    def _retry_preparation_history(self):
        if self._retry_history_save is None or self._executor.close_held or self._cancel.is_set():
            return
        if self._package_review_cancelled():
            self.retry_history_button.setEnabled(False)
            self.status.setText("The Manager session ended. The preparation is still retained at the path shown below.")
            return
        try:
            self._future = self._executor.submit(self._retry_history_save)
        except (RuntimeError, ValueError):
            self.status.setText("History save could not start. Choose Retry Saving to History to try again.")
            return
        self._saving_preparation = True
        for action in (self.retry_history_button, self.prepare_button, self.dataset_button, self.adapter_button, self.folder_button):
            action.setEnabled(False)
        if self._anima_base_id is not None:
            self.anima_button.setEnabled(False)
        self.return_button.setText("Wait for Save and Return")
        self.status.setText("Saving the completed preparation to History…")
        self._timer.start(50)

    def _open_prepared_folder(self):
        if self._prepared_folder is None or self._executor.close_held or self._cancel.is_set():
            return
        folder, identity = self._prepared_folder
        session_ready = self._anima_review_allowed
        try:
            if session_ready is not None and not session_ready():
                raise InterruptedError
            if self._handoff_directory_state(folder) != identity:
                raise ValueError("Preparation workspace changed")
            if session_ready is not None and not session_ready():
                raise InterruptedError
        except InterruptedError:
            self._prepared_folder = None
            self.prepared_folder_button.hide()
            self.status.setText("The Manager session ended. Reopen the saved preparation from Training History.")
            return
        except (OSError, RuntimeError, ValueError):
            self._prepared_folder = None
            self.prepared_folder_button.hide()
            self.status.setText("The prepared workspace changed and could not be opened. Prepare the dataset again to create a new workspace.")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            self.status.setText("The system could not open the prepared workspace. Choose Open Prepared Workspace to retry.")

    def _open_package_folder(self):
        if (not self.folder_button.isVisible() or not self.folder_button.isEnabled()
                or self._handoff_folder is None or self._checking_dataset or self._checking_handoff
                or self._adapter_preview is not None or self._cancel.is_set()):
            return
        session_ready = self._anima_review_allowed
        if session_ready is not None and not session_ready():
            self._invalidate_package_review("The Manager session ended. Reopen the package from Applications.")
            return
        try:
            self._future = self._executor.submit(partial(
                self._verify_handoff_folder, self._handoff_folder, self._handoff_fingerprint,
                should_cancel=lambda: self._cancel.is_set() or (session_ready is not None and not session_ready()),
            ))
        except (RuntimeError, ValueError):
            self.status.setText("Package recheck could not start. Return to Applications and review it again.")
            return
        self._checking_handoff = True
        for button in (self.folder_button, self.adapter_button, self.dataset_button):
            button.setEnabled(False)
        self.status.setText("Rechecking the reviewed package before opening its folder…")
        self.return_button.setText("Cancel Check and Return")
        self._timer.start(50)

    @staticmethod
    def _handoff_directory_state(folder):
        from core.training_package import _directory_state
        from core.training_source_io import _assert_unredirected

        _assert_unredirected(folder)
        return _directory_state(folder)

    def _verify_handoff_folder(self, folder, fingerprint, *, should_cancel=None):
        from core.training_package import TrainingPackageService, TrainingPackageVerificationLimits

        directory = self._handoff_directory_state(folder)
        current = TrainingPackageService().verify(
            folder, should_cancel=should_cancel or self._cancel.is_set, limits=TrainingPackageVerificationLimits(),
            expected_package_fingerprint=fingerprint,
        )
        if (not current.valid or current.inspection_limited
                or current.package_fingerprint != fingerprint
                or self._handoff_directory_state(folder) != directory):
            raise ValueError("Package identity changed")
        return directory

    def _finish_package_handoff(self):
        self._checking_handoff = False
        session_ready = self._anima_review_allowed
        try:
            if session_ready is not None and not session_ready():
                raise InterruptedError
            directory = self._future.result()
            if self._handoff_directory_state(self._handoff_folder) != directory:
                raise ValueError("Package identity changed")
            if session_ready is not None and not session_ready():
                raise InterruptedError
        except InterruptedError:
            self._invalidate_package_review("The Manager session ended. Reopen the package from Applications.")
            return
        except (OSError, RuntimeError, ValueError):
            self._invalidate_package_review(
                "Package changed or could not be rechecked. Return to Applications and review it again."
            )
            return
        for button in (self.folder_button, self.adapter_button, self.dataset_button):
            button.setEnabled(True)
        if QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._handoff_folder))):
            self.status.setText(
                "Package folder opened for inspection. The Krea 2 trainer cannot use this folder directly."
                if self._krea_package else
                "Package folder opened for manual handoff. Trainer readiness is unverified."
            )
        else:
            self.status.setText("The system could not open the verified package folder. Choose "
                                f"{self.folder_button.text()} to recheck it and try again.")

    def _invalidate_package_review(self, message):
        self._handoff_folder = None
        self._verification = None
        if self._dataset_overview is not None:
            self.sections[0].setPlainText(self._dataset_overview)
            self._dataset_overview = None
        self.tabs.setEnabled(False)
        for button in (self.folder_button, self.adapter_button, self.dataset_button):
            button.setEnabled(False)
        self.status.setText(message)

    def _open_adapter_preview(self):
        if (not self.adapter_button.isEnabled() or not self.adapter_button.isVisible() or self._checking_dataset
                or self._adapter_preview is not None or self._cancel.is_set()):
            return
        if self._package_review_cancelled():
            self._invalidate_package_review("The Manager session ended. Return to Applications to reopen the package.")
            return
        from core.model_resource_registry import ModelResourceRegistry
        from core.training_adapter_preview import SdScriptsSdxlLoraPreviewService
        from core.training_package import TrainingPackageVerificationLimits
        from core.training_workflow import _assets
        from qt_app.training_adapter_preview_dialog import TrainingAdapterPreviewDialog

        preview = TrainingAdapterPreviewDialog(
            self._verification, ModelResourceRegistry(self._registry_path),
            self._preview_jobs, self,
            service=SdScriptsSdxlLoraPreviewService(package_limits=TrainingPackageVerificationLimits()),
            availability=_assets if self._open_sdxl_preparation is not None else lambda _package, _recipe: False,
        )
        self._adapter_preview = preview
        try:
            preview.exec()
            if (preview.action == "local_training" and self._open_sdxl_preparation is not None
                    and not self._package_review_cancelled()):
                if self._open_sdxl_preparation(self._verification, preview.registry, preview.selected_recipe):
                    self.accept()
        finally:
            self._adapter_preview = None
            preview.deleteLater()
            if self._cancel.is_set():
                self.reject()

    def _resolve_saved_sdxl_setup(self, should_cancel):
        result = resolve_manager_saved_sdxl_setup(
            self._anima_settings_path, self._registry_path, should_cancel=should_cancel)
        return tuple(result[key] for key in ("source_root", "environment_root", "python_base_root"))

    def _prepare_dataset(self):
        if (not self.prepare_button.isVisible() or not self.prepare_button.isEnabled()
                or self._checking_dataset or self._checking_handoff or self._preparing_dataset
                or self._adapter_preview is not None or self._cancel.is_set()
                or self._verification is None or self._preparation_setup is not None):
            return
        if self._package_review_cancelled():
            self._invalidate_package_review("The Manager session ended. Return to Applications to reopen the package.")
            return
        stop_load = Event()
        load_future = None

        class PreparationDialog(QDialog):
            def reject(dialog):
                if load_future is not None:
                    stop_load.set()
                    load_status.setText("Stopping the saved trainer check…")
                    return
                super().reject()

        dialog = PreparationDialog(self)
        dialog.setWindowTitle("Prepare SDXL Dataset")
        layout = QVBoxLayout(dialog)
        note = QLabel("Select your existing SDXL trainer source, its Python environment, and the base Python directory. This prepares a separate dataset copy.")
        note.setWordWrap(True)
        layout.addWidget(note)
        load = QPushButton("Load Saved Trainer")
        load.setVisible(self._anima_settings_path is not None)
        layout.addWidget(load, 0, Qt.AlignLeft)
        setup = QPushButton("Review Existing Trainer…")
        setup.hide()
        layout.addWidget(setup, 0, Qt.AlignLeft)
        load_status = QLabel()
        load_status.setTextFormat(Qt.PlainText)
        load_status.setWordWrap(True)
        layout.addWidget(load_status)
        form = QFormLayout()
        layout.addLayout(form)
        fields = []
        edit_revision = 0

        def update_prepare():
            prepare.setEnabled(load_future is None and len(fields) == 3
                                and all(Path(field.text().strip()).is_absolute() for field in fields))

        def edited():
            nonlocal edit_revision
            edit_revision += 1
            update_prepare()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        prepare = buttons.button(QDialogButtonBox.Ok)
        prepare.setText("Prepare Dataset")
        prepare.setEnabled(False)
        for label in ("Trainer source", "Trainer environment", "Base Python"):
            row = QWidget(dialog)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            field = QLineEdit(row)
            field.setAccessibleName(label)
            browse = QPushButton("Browse…", row)
            browse.setAccessibleName(f"Browse {label.lower()}")
            def choose(_checked=False, field=field, label=label):
                folder = QFileDialog.getExistingDirectory(dialog, f"Select {label.lower()}")
                from shiboken6 import isValid

                if (folder and self._preparation_setup is dialog and isValid(dialog)
                        and dialog.isVisible() and not self._package_review_cancelled()):
                    field.setText(folder)
            browse.clicked.connect(choose)
            fields.append(field)
            field.textChanged.connect(edited)
            row_layout.addWidget(field)
            row_layout.addWidget(browse)
            form.addRow(label, row)
        load_timer = QTimer(dialog)

        def cancelled():
            return stop_load.is_set() or self._package_review_cancelled()

        def finish_load():
            nonlocal load_future
            if not load_future.done() or self._executor.close_held:
                return
            load_timer.stop()
            future, load_future = load_future, None
            load.setEnabled(True)
            update_prepare()
            if self._package_review_cancelled() and not self._cancel.is_set():
                self.status.setText("The Manager session ended. Return to Applications to reopen the package.")
            if stop_load.is_set() or self._package_review_cancelled():
                dialog.reject()
                return
            if edit_revision != loaded_at_revision:
                load_status.setText("Folder choices changed while loading. Your choices were kept.")
                return
            try:
                locations = future.result()
            except (OSError, RuntimeError, ValueError) as exc:
                setup.setVisible(callable(self._open_trainer_setup))
                load_status.setText(f"Saved trainer unavailable. Use Browse or review Trainer Setup. {exc}")
                return
            for field, location in zip(fields, locations, strict=True):
                field.setText(location)
            load_status.setText("Saved trainer loaded. Review these folders, then choose Prepare Dataset.")

        loaded_at_revision = 0

        def load_saved():
            nonlocal load_future, loaded_at_revision
            if load_future is not None or cancelled():
                return
            loaded_at_revision = edit_revision
            setup.hide()
            load_future = self._executor.submit(self._resolve_saved_sdxl_setup, cancelled)
            load.setEnabled(False)
            update_prepare()
            load_status.setText("Checking the saved trainer setup…")
            load_timer.start(50)

        def review_setup():
            if cancelled() or load_future is not None or not callable(self._open_trainer_setup):
                return
            if self._open_trainer_setup() and not cancelled():
                load_status.setText("Trainer Setup saved. Choose Load Saved Trainer to review the saved folders.")

        setup.clicked.connect(review_setup)
        load.clicked.connect(load_saved)
        load_timer.timeout.connect(finish_load)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        self._preparation_setup = dialog
        try:
            if dialog.exec() != QDialog.Accepted or self._cancel.is_set():
                return
            source, environment, python_base = (field.text().strip() for field in fields)
        finally:
            self._preparation_setup = None
            dialog.deleteLater()
            if self._cancel.is_set():
                self.reject()
        if self._package_review_cancelled():
            self._invalidate_package_review("The Manager session ended. Return to Applications to reopen the package.")
            return
        try:
            self._future = self._executor.submit(partial(
                self._dataset_verifier.prepare, self._verification, source=source,
                environment=environment, python_base=python_base, jobs=self._preview_jobs,
                should_cancel=self._package_review_cancelled,
            ))
        except (RuntimeError, ValueError):
            self.status.setText("Preparation could not start. Choose Prepare SDXL Dataset to retry.")
            return
        self._prepared_folder = None
        self.prepared_folder_button.hide()
        self._retry_history_save = None
        self.retry_history_button.hide()
        self._preparing_dataset = True
        for action in (self.prepare_button, self.dataset_button, self.adapter_button, self.folder_button):
            action.setEnabled(False)
        self.return_button.setText("Cancel Check and Return")
        self.status.setText("Preparing the selected SDXL dataset in the reviewed worker…")
        self._timer.start(50)

    def _verify_dataset(self):
        if (not self.dataset_button.isVisible() or not self.dataset_button.isEnabled()
                or self._checking_dataset or self._checking_handoff
                or self._adapter_preview is not None or self._cancel.is_set()
                or self._verification is None):
            return
        if self._package_review_cancelled():
            self._invalidate_package_review("The Manager session ended. Return to Applications to reopen the package.")
            return
        overview = self.sections[0]
        if self._dataset_overview is None:
            self._dataset_overview = overview.toPlainText()
        else:
            overview.setPlainText(self._dataset_overview)
        try:
            self._future = self._executor.submit(
                partial(self._dataset_verifier, self._verification, should_cancel=self._package_review_cancelled),
            )
        except (RuntimeError, ValueError):
            self.status.setText("Dataset verification could not start. Review the runtime before retrying.")
            return
        self._checking_dataset = True
        self.dataset_button.setEnabled(False)
        self.adapter_button.setEnabled(False)
        self.folder_button.setEnabled(False)
        self.return_button.setText("Cancel Check and Return")
        self.status.setText("Verifying the selected dataset in the isolated worker…")
        self._timer.start(50)

    def _show_dataset_receipt(self):
        from core.training_preflight import TrainingPreflightWorkspacePathError

        try:
            result = self._future.result()
        except TrainingPreflightWorkspacePathError as exc:
            self.status.setText(str(exc))
            self.dataset_button.setEnabled(True)
            return
        except Exception:
            self.status.setText("Dataset verification did not finish. The package review is still available. Choose Verify SDXL Dataset to retry.")
            self.dataset_button.setEnabled(True)
            return
        status = result.get("status") if isinstance(result, dict) else None
        digest = result.get("preflight_receipt_sha256") if isinstance(result, dict) else None
        if (status not in ("admitted", "held") or not isinstance(digest, str)
                or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
            self.status.setText("Dataset verification returned no usable receipt. Choose Verify SDXL Dataset to retry.")
            self.dataset_button.setEnabled(True)
            return
        binding = result.get("binding")
        if (not isinstance(binding, dict)
                or binding.get("package_fingerprint") != self._verification.package_fingerprint):
            self.status.setText("Dataset verification did not match this package. Verify SDXL Dataset again.")
            self.dataset_button.setEnabled(True)
            return
        summary = result.get("summary")
        if status == "admitted":
            labels = {
                "training_images": "Training images inspected",
                "evaluation_images": "Evaluation images inspected",
                "epoch_exposures": "Planned training exposures per epoch",
            }
            if (not isinstance(summary, dict)
                    or any(type(summary.get(field)) is not int or summary[field] < 0 for field in labels)
                    or not 0 < summary["training_images"] <= summary["epoch_exposures"]):
                self.status.setText("Dataset verification returned incomplete inspection counts. Verify SDXL Dataset again.")
                self.dataset_button.setEnabled(True)
                return
            manifest = self._verification.manifest
            package_counts = manifest["counts"]
            training = package_counts["training"]
            repeats = manifest["training_configuration"]["weighting"]["repeats"]
            expected_counts = {
                "training_images": training,
                "evaluation_images": package_counts["included_images"] - training,
                "epoch_exposures": training * repeats,
            }
            if any(summary[field] != expected_counts[field] for field in labels):
                self.status.setText("Dataset inspection counts did not match this package. Verify SDXL Dataset again.")
                self.dataset_button.setEnabled(True)
                return
            counts = [f"{label}: {summary[field]:,}" for field, label in labels.items()]
            self.sections[0].appendPlainText("\n" + "\n".join(counts))
        self.status.setText("Dataset inspection passed. Trainer readiness is unverified." if status == "admitted"
                            else "Dataset inspection is held. Trainer readiness is unverified.")
        if status == "held":
            reason = result.get("reason")
            holds = summary.get("holds") if isinstance(summary, dict) else None
            if reason == "package_unavailable_or_changed":
                self._invalidate_package_review(
                    "The selected package changed or could not be read. Export a fresh package from Training if needed, then return to Applications and select its folder."
                )
                return
            elif reason == "policy_held" and isinstance(holds, dict):
                detail = self._dataset_hold_details(holds)
            else:
                detail = "Return to Applications and select a verified SDXL LoRA package."
            self.sections[0].appendPlainText(f"\n{detail}")
        self.sections[0].appendPlainText(f"\nDataset inspection receipt: {digest}")
        self.tabs.setCurrentIndex(0)

    @staticmethod
    def _dataset_hold_details(holds):
        from core.training_package_effects import HOLD_LABELS

        labels = {
            "dataset_recipe_unmeasured": "The selected recipe is outside the verified SDXL dataset profile.",
            "dataset_scope_exceeded": "The dataset has too many training items for this check.",
            "dataset_work_budget_exceeded": "The dataset exceeds this check's image-processing limit.",
            "image_effects_unmeasured": "The check could not verify the requested image effects.",
            "pixel_preservation_unmeasured": "The check could not verify that image pixels are preserved.",
            "pixel_geometry_unmeasured": "The check could not verify the image dimensions or crop.",
        }
        reasons = [labels.get(code, HOLD_LABELS.get(code)) for code in sorted(holds)]
        if any(reason is None for reason in reasons):
            reasons = [reason for reason in reasons if reason is not None]
            reasons.append("The package includes an image or setting outside this verifier's supported profile.")
        return "\n".join([*reasons, "Review the Dataset and Settings tabs before preparing another package."])

    @staticmethod
    def _manual_captions(manifest):
        items = manifest["package_plan"]["items"]
        selected = {item["caption_path"] for item in items[:100]}
        captions = {row["path"]: row["package_caption"] for row in manifest["artifacts"]
                    if row["path"] in selected}
        lines = ["Verified package captions · read-only"]
        for item in items[:100]:
            caption = captions[item["caption_path"]]
            lines.extend(("", f"{item['role'].title()} · {item['image_path']}", caption[:4096]))
            if len(caption) > 4096:
                lines.append("Caption preview shortened. See the verified caption file for the full text.")
        if len(items) > 100:
            lines.extend(("", f"Showing 100 of {len(items):,} captions. See the verified package for the full set."))
        return "\n".join(lines)

    @staticmethod
    def _dataset_text(manifest):
        counts = manifest["counts"]
        roles = ("training", "validation", "holdout")
        items = manifest["package_plan"]["items"]
        lines = [
            f"Included images: {counts['included_images']:,}",
            f"Excluded images: {counts['excluded_images']:,}",
            *(f"{role.title()}: {counts[role]:,}" for role in roles),
        ]
        excluded = manifest["excluded"]
        if excluded:
            lines.extend(("", "Why images were excluded"))
            for item in excluded[:100]:
                lines.append(f"{item['stable_id']}: {item['reason'] or 'No reason recorded'}")
            if len(excluded) > 100:
                lines.append(f"Showing 100 of {len(excluded):,} exclusions. See the package manifest for the full list.")
        lines.extend(("", "Verified package files"))
        for item in items[:100]:
            lines.extend((
                "",
                f"{item['role'].title()} · {item['effective_exposures']} planned exposures",
                f"Image: {item['image_path']}",
                f"Caption: {item['caption_path']}",
            ))
        if len(items) > 100:
            lines.extend(("", f"Showing 100 of {len(items):,} items. See the package manifest for the full list."))
        return "\n".join(lines)

    def reject(self):
        self._cancel.set()
        if self._preparation_setup is not None:
            self._preparation_setup.reject()
            return
        if self._adapter_preview is not None:
            self._adapter_preview.reject()
            return
        if self._anima_review is not None:
            self._anima_review.reject()
            return
        self._executor.begin_close()
        if self._executor.close_held:
            self.status.setText("Stopping the package check. Waiting for the review worker to finish…")
            self.return_button.setEnabled(False)
            return
        self._timer.stop()
        super().reject()

    def closeEvent(self, event):
        if (self._preparation_setup is not None or self._adapter_preview is not None
                or self._anima_review is not None or self._executor.close_held):
            event.ignore()
            self.reject()
        else:
            super().closeEvent(event)
