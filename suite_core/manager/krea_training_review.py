"""Read-only Krea package review owned by the Manager window."""
from functools import partial
from threading import Event

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout,
)

from .resource_executor import ResourceExecutorOwner


class KreaTrainingReviewDialog(QDialog):
    def __init__(self, package_path, registry_path, parent, *, dataset_verifier=None, preview_jobs=None):
        super().__init__(parent)
        self.setWindowTitle("Training Package Review")
        self.resize(850, 630)
        self._executor = ResourceExecutorOwner()
        self._cancel = Event()
        self._dataset_verifier = dataset_verifier
        self._verification = None
        self._handoff_folder = None
        self._checking_dataset = False
        self._dataset_overview = None
        self._checking_handoff = False
        self._handoff_fingerprint = None
        self._krea_package = False
        self._preview_jobs = preview_jobs
        self._registry_path = registry_path
        self._adapter_preview = None

        layout = QVBoxLayout(self)
        heading = QLabel("Review a training package")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        guidance = ["Review a verified SDXL LoRA, Krea 2 LoRA, or collection-bound package for manual handoff."]
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
        self.adapter_button = QPushButton("Review SDXL Trainer Settings?")
        self.adapter_button.hide()
        self.adapter_button.clicked.connect(self._open_adapter_preview)
        layout.addWidget(self.adapter_button, 0, Qt.AlignLeft)
        layout.addWidget(self.tabs, 1)
        actions = QHBoxLayout()
        self.folder_button = QPushButton("Open Package Folder")
        self.folder_button.setToolTip("Find the verified package for use in your external trainer.")
        self.folder_button.hide()
        self.folder_button.clicked.connect(self._open_package_folder)
        actions.addWidget(self.folder_button)
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

    def _review(self, package_path, registry_path):
        from core.krea_training_compatibility import KreaTrainingCompatibilityService
        from core.model_resource_registry import ModelResourceRegistry
        from core.training_configuration import TrainingConfiguration
        from core.training_package import TrainingPackageService, TrainingPackageVerificationLimits

        limits = TrainingPackageVerificationLimits()
        verification = TrainingPackageService().verify(
            package_path, should_cancel=self._cancel.is_set, limits=limits)
        if verification.inspection_limited:
            raise ValueError("The package exceeds the review limits. Choose a smaller package.")
        if not verification.valid:
            raise ValueError(
                "The selected package could not be verified. If any files changed or are missing, "
                "export a fresh package from Training. Then return to Applications and select "
                "the package folder again."
            )
        configuration = TrainingConfiguration.from_dict(verification.manifest["training_configuration"])
        if configuration.target_model_family.casefold() not in {"krea 2", "krea2"} or configuration.output_kind != "lora":
            sdxl_review = (configuration.target_model_family.casefold() == "sdxl"
                           and configuration.output_kind == "lora")
            if "collection_version" not in verification.manifest and not sdxl_review:
                available = ["a collection-bound package"]
                if registry_path is not None:
                    available.append("a Krea 2 LoRA package")
                available.append("an SDXL LoRA package")
                raise ValueError("Return to Applications and choose " + " or ".join(available) + ".")
            return verification, configuration, None
        if registry_path is None:
            return verification, configuration, None
        report = KreaTrainingCompatibilityService().report(
            verification, ModelResourceRegistry(registry_path), should_cancel=self._cancel.is_set, limits=limits,
        )

        return verification, configuration, report

    def _settle(self):
        if not self._future.done() or self._executor.close_held:
            return
        self._timer.stop()
        if self._cancel.is_set():
            super().reject()
            return
        self.return_button.setText("Return to Applications")
        if self._checking_handoff:
            self._finish_package_handoff()
            return
        if self._checking_dataset:
            self._checking_dataset = False
            self.adapter_button.setEnabled(True)
            self.folder_button.setEnabled(True)
            self._show_dataset_receipt()
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
        if report is None:
            self.status.setText(
                "Package verified for manual handoff. Krea 2 compatibility review needs an admitted model registry."
                if self._krea_package else "Package verified for manual handoff. Trainer readiness is unverified."
            )
            self.tabs.setTabText(2, "Dataset")
            self.sections[2].setAccessibleName("Training dataset")
            self.sections[0].setPlainText(identity + (
                "\n\nRegistered base and trainer readiness are unverified." if self._krea_package else ""))
            if (self._registry_path is not None and self._preview_jobs is not None
                    and configuration.target_model_family.casefold() == "sdxl"
                    and configuration.output_kind == "lora"):
                self._verification = verification
                self.adapter_button.show()
            if (self._dataset_verifier is not None
                           and configuration.target_model_family.casefold() == "sdxl"
                           and configuration.output_kind == "lora"):
                self._verification = verification
                self.dataset_button.show()
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
        sections = (
            f"{identity}\nRegistered base: {report.base_resource_id}\n\n"
            + "\n\n".join(f"{item.summary}\n{item.reason}\n{item.next_action}" for item in report.requirements),
            "\n\n".join(f"{item.field} [{item.disposition}]\nRequested: {item.requested}\n{item.reason}"
                       for item in report.settings),
            "\n\n".join(f"{item.stable_id} ({item.role}, {item.effective_exposures} exposures)\n{item.caption}"
                       for item in report.images[:100]),
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

    def _open_package_folder(self):
        if (self._handoff_folder is None or self._checking_dataset or self._checking_handoff
                or self._adapter_preview is not None or self._cancel.is_set()):
            return
        from core.training_package import TrainingPackageService, TrainingPackageVerificationLimits

        try:
            self._future = self._executor.submit(
                partial(TrainingPackageService().verify, self._handoff_folder,
                        should_cancel=self._cancel.is_set, limits=TrainingPackageVerificationLimits()),
            )
        except (RuntimeError, ValueError):
            self.status.setText("Package recheck could not start. Return to Applications and review it again.")
            return
        self._checking_handoff = True
        for button in (self.folder_button, self.adapter_button, self.dataset_button):
            button.setEnabled(False)
        self.status.setText("Rechecking the reviewed package before opening its folder…")
        self.return_button.setText("Cancel Check and Return")
        self._timer.start(50)

    def _finish_package_handoff(self):
        self._checking_handoff = False
        try:
            current = self._future.result()
            if (not current.valid or current.inspection_limited
                    or current.package_fingerprint != self._handoff_fingerprint):
                raise ValueError("Package identity changed")
            from core.training_source_io import _assert_unredirected

            _assert_unredirected(self._handoff_folder)
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
            self.status.setText("Package folder could not be opened. Return to Applications and select a current package.")

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
        from core.model_resource_registry import ModelResourceRegistry
        from qt_app.training_adapter_preview_dialog import TrainingAdapterPreviewDialog

        preview = TrainingAdapterPreviewDialog(
            self._verification, ModelResourceRegistry(self._registry_path),
            self._preview_jobs, self, availability=lambda _package, _recipe: False,
        )
        self._adapter_preview = preview
        try:
            preview.exec()
        finally:
            self._adapter_preview = None
            preview.deleteLater()
            if self._cancel.is_set():
                self.reject()

    def _verify_dataset(self):
        if (not self.dataset_button.isVisible() or not self.dataset_button.isEnabled()
                or self._checking_dataset or self._checking_handoff
                or self._adapter_preview is not None or self._cancel.is_set()
                or self._verification is None):
            return
        overview = self.sections[0]
        if self._dataset_overview is None:
            self._dataset_overview = overview.toPlainText()
        else:
            overview.setPlainText(self._dataset_overview)
        try:
            self._future = self._executor.submit(
                partial(self._dataset_verifier, self._verification, should_cancel=self._cancel.is_set),
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
        try:
            result = self._future.result()
        except Exception:
            self.status.setText("Dataset verification did not finish. The package review is still available.")
            self.dataset_button.setEnabled(True)
            return
        status = result.get("status") if isinstance(result, dict) else None
        digest = result.get("preflight_receipt_sha256") if isinstance(result, dict) else None
        if (status not in ("admitted", "held") or not isinstance(digest, str)
                or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
            self.status.setText("Dataset verification returned no usable receipt.")
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
                    "The selected package changed or could not be read. Return to Applications and choose a current package."
                )
                return
            elif reason == "policy_held" and isinstance(holds, dict):
                labels = {
                    "dataset_recipe_unmeasured": "The selected recipe is outside the verified SDXL dataset profile.",
                    "dataset_scope_exceeded": "The dataset has too many training items for this check.",
                    "dataset_work_budget_exceeded": "The dataset exceeds this check's image-processing limit.",
                    "image_effects_unmeasured": "The check could not verify the requested image effects.",
                    "pixel_preservation_unmeasured": "The check could not verify that image pixels are preserved.",
                    "pixel_geometry_unmeasured": "The check could not verify the image dimensions or crop.",
                }
                reasons = [labels[code] for code in labels if code in holds]
                if any(code not in labels for code in holds):
                    reasons.append("The package includes an image or setting outside this verifier's supported profile.")
                detail = "\n".join([*reasons, "Review the Dataset and Settings tabs before preparing another package."])
            else:
                detail = "Return to Applications and select a verified SDXL LoRA package."
            self.sections[0].appendPlainText(f"\n{detail}")
        self.sections[0].appendPlainText(f"\nDataset inspection receipt: {digest}")
        self.tabs.setCurrentIndex(0)

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
        if self._adapter_preview is not None:
            self._adapter_preview.reject()
            return
        self._executor.begin_close()
        if self._executor.close_held:
            self.status.setText("Stopping the package check. Waiting for the review worker to finish…")
            self.return_button.setEnabled(False)
            return
        self._timer.stop()
        super().reject()

    def closeEvent(self, event):
        if self._adapter_preview is not None or self._executor.close_held:
            event.ignore()
            self.reject()
        else:
            super().closeEvent(event)
