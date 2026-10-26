from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,QComboBox,QDialog,QDialogButtonBox,QFileDialog,QFormLayout,
    QHBoxLayout,QLabel,QLineEdit,QListWidget,QListWidgetItem,QMessageBox,
    QPushButton,QVBoxLayout,QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from core import (
    LIBRARY_NAMING_IMAGE_HASH,LIBRARY_NAMING_KEEP,LIBRARY_ROLE_DETAILS,
    SHARE_SAFETY_POLICY_LABELS,LibraryProfile,LibraryProfileRegistry,LibraryRole,
    normalize_library_naming_policy,
)
from .components import DisclosureButton,compact_button


class LibraryProfileDialog(QDialog):
    """Create or edit one named image-library profile."""

    def __init__(
        self,
        registry: LibraryProfileRegistry,
        parent=None,
        *,
        profile: LibraryProfile | None = None,
    ) -> None:
        super().__init__(parent)
        self.registry = registry
        self.original = profile
        self.profile: LibraryProfile | None = None
        self.setWindowTitle("Edit Library" if profile is not None else "Add Library")
        self.resize(620, 300)

        layout = QVBoxLayout(self)
        self.intro = QLabel(
            "Set this library's identity and safeguards. Saving does not scan, move, copy, "
            "upload, or rewrite files."
        )
        self.intro.setWordWrap(True)
        self.intro.setAccessibleName("Library type purpose")
        layout.addWidget(self.intro)

        form = QFormLayout()
        self.name = QLineEdit(profile.name if profile is not None else "")
        self.name.setAccessibleName("Library name")
        self.name.setToolTip("The name shown for this library throughout CollectiGen.")
        self.root = QLineEdit(profile.root if profile is not None else "")
        self.root.setAccessibleName("Library folder")
        self.root.setToolTip(
            "The root folder for this image library. Saving this setting does not scan or move files."
        )
        browse = compact_button(QPushButton("Browse…"))
        browse.setToolTip("Choose the library's root folder.")
        browse.clicked.connect(self.choose_root)
        root_row = QWidget()
        root_layout = QHBoxLayout(root_row)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.addWidget(self.root, 1)
        root_layout.addWidget(browse)

        self.role = QComboBox()
        self.role.setAccessibleName("Library type")
        for role, details in LIBRARY_ROLE_DETAILS.items():
            self.role.addItem(details.label, role.value)
        selected_role = profile.role if profile is not None else LibraryRole.LOCAL_ONLY.value
        index = self.role.findData(selected_role)
        if index < 0 and profile is not None:
            self.role.addItem(f"Unsupported ({profile.role})", profile.role)
            index = self.role.count() - 1
        self.role.setCurrentIndex(max(0, index))

        self.name_label=QLabel("Name")
        self.folder_label=QLabel("Folder")
        self.role_label=QLabel("Type")
        self.name_label.setToolTip(self.name.toolTip())
        self.folder_label.setToolTip(self.root.toolTip())
        form.addRow(self.name_label, self.name)
        form.addRow(self.folder_label, root_row)
        form.addRow(self.role_label, self.role)
        self.share_safety=QComboBox()
        self.share_safety.setAccessibleName("Destination share safety limit")
        for policy_id,label in SHARE_SAFETY_POLICY_LABELS.items():
            self.share_safety.addItem(label,policy_id)
        selected_safety=(
            profile.share_safety_policy if profile is not None else
            LibraryProfile.create("Temporary",Path.cwd(),selected_role).share_safety_policy
        )
        safety_index=self.share_safety.findData(selected_safety)
        self.share_safety.setCurrentIndex(max(0,safety_index))
        self._share_safety_touched=False
        self.share_safety_label=QLabel("Share safety")
        form.addRow(self.share_safety_label,self.share_safety)
        layout.addLayout(form)

        self.guidance = QLabel()
        self.guidance.setWordWrap(True)
        self.guidance.setObjectName("mutedLabel")
        self.guidance.setAccessibleName("Library type consequences")
        layout.addWidget(self.guidance)
        layout.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.role.currentIndexChanged.connect(self._role_changed)
        self.share_safety.activated.connect(self._share_safety_changed)
        self.share_safety.currentIndexChanged.connect(self._update_guidance)
        self._update_guidance()

    def _share_safety_changed(self,*_args) -> None:
        self._share_safety_touched=True

    def _role_changed(self,*_args) -> None:
        if self.original is None and not self._share_safety_touched:
            default=LibraryProfile.create(
                "Temporary",Path.cwd(),str(self.role.currentData()),
            ).share_safety_policy
            index=self.share_safety.findData(default)
            if index>=0:
                self.share_safety.setCurrentIndex(index)
        self._update_guidance()

    def choose_root(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "Choose Image Library Folder",
            self.root.text().strip() or str(Path.cwd()),
        )
        if selected:
            self.root.setText(selected)

    def _update_guidance(self) -> None:
        try:
            role = LibraryRole(str(self.role.currentData()))
        except ValueError:
            self.guidance.setText(
                "This saved library type is not supported by this CollectiGen version. "
                "Choose a current type before saving."
            )
            self.role.setToolTip(self.guidance.text())
            self.role_label.setToolTip(self.guidance.text())
            return
        details = LIBRARY_ROLE_DETAILS[role]
        safety_label=self.share_safety.currentText()
        role_tooltip=(
            f"{details.summary}\n\n{details.consequences}\n\n"
            "This choice classifies storage; it does not configure the provider."
        )
        safety_tooltip=(
            f"{safety_label}. Sets which confirmed ratings produce warnings in Move to Library previews.\n\n"
            "Library moves do not require Mark Reviewed. Confirmed prohibited-content signals involving "
            "minors still block outward moves; suspected signals require correction or review."
        )
        self.guidance.setText(details.summary)
        self.guidance.setToolTip(role_tooltip)
        self.role.setToolTip(role_tooltip)
        self.role_label.setToolTip(role_tooltip)
        self.share_safety.setToolTip(safety_tooltip)
        self.share_safety_label.setToolTip(safety_tooltip)
        self.guidance.setAccessibleDescription(
            f"{details.label}. {details.summary} {details.consequences} {safety_tooltip}"
        )
        self.role.setAccessibleDescription(role_tooltip)
        self.share_safety.setAccessibleDescription(safety_tooltip)

    def accept(self) -> None:
        stable_id = self.original.stable_id if self.original is not None else ""
        candidate = LibraryProfile.create(
            self.name.text(), self.root.text(), str(self.role.currentData()),
            stable_id=stable_id,
        )
        if self.original is not None:
            candidate=candidate.with_changes(extra=dict(self.original.extra))
        candidate=candidate.with_share_safety_policy(str(self.share_safety.currentData()))
        # Preserve the retired serialized preference for older readers, but it
        # has no effect on library moves and no longer has a UI control.
        if self.original is not None:
            candidate=candidate.with_reviewed_image_knowledge_required(
                self.original.require_reviewed_image_knowledge
            )
        issues = self.registry.validate(candidate, replacing_id=stable_id)
        if issues:
            QMessageBox.warning(
                self,
                "Library Details Need Attention",
                "\n".join(f"• {issue.message}" for issue in issues),
            )
            return
        self.profile = candidate
        super().accept()


class LibraryOpenDialog(QDialog):
    """Choose one configured peer library without scanning or moving files."""

    def __init__(
        self,registry: LibraryProfileRegistry,parent=None,*,active_library_id: str="",
    ) -> None:
        super().__init__(parent)
        self.registry=registry
        self.profile: LibraryProfile|None=None
        self.choose_folder=False
        self.setWindowTitle("Open Named Library")
        self.resize(680,340)
        layout=QVBoxLayout(self)
        intro=QLabel(
            "Choose the personal, shared, synchronized, or removable collection to open in Dataset. "
            "Every named library uses the same Dataset tools; changing the active view does not mix, copy, or move images."
        )
        intro.setWordWrap(True); layout.addWidget(intro)
        form=QFormLayout()
        self.library=QComboBox(); self.library.setAccessibleName("Named library to open")
        for profile in registry.profiles:
            if registry.validate(profile,replacing_id=profile.stable_id):
                continue
            marker=" (current)" if profile.stable_id==active_library_id else ""
            self.library.addItem(
                f"{profile.name} — {profile.display_role}{marker}",profile.stable_id,
            )
        selected=self.library.findData(active_library_id)
        self.library.setCurrentIndex(selected if selected>=0 else 0)
        form.addRow("Library",self.library); layout.addLayout(form)
        self.root=QLabel(); self.root.setWordWrap(True)
        self.root.setTextInteractionFlags(
            self.root.textInteractionFlags()|Qt.TextSelectableByMouse
        )
        self.root.setAccessibleName("Selected library folder")
        layout.addWidget(self.root)
        self.guidance=QLabel(); self.guidance.setWordWrap(True)
        self.guidance.setObjectName("mutedLabel")
        self.guidance.setAccessibleName("Selected library storage consequences")
        layout.addWidget(self.guidance)
        layout.addStretch(1)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        self.choose_folder_button=buttons.addButton(
            "Choose Another Folder…",QDialogButtonBox.ActionRole,
        )
        self.open_button=buttons.addButton("Open Library",QDialogButtonBox.AcceptRole)
        self.choose_folder_button.clicked.connect(self._choose_another_folder)
        self.open_button.clicked.connect(self.accept); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.library.currentIndexChanged.connect(self._update_library)
        self._update_library()

    def _selected(self) -> LibraryProfile|None:
        return self.registry.get(str(self.library.currentData() or ""))

    def _update_library(self) -> None:
        profile=self._selected()
        self.open_button.setEnabled(profile is not None)
        if profile is None:
            self.root.setText("No valid named library is configured.")
            self.guidance.setText(
                "Choose another folder, or add and classify peer libraries in Setup > Library & Storage > Locations."
            )
            return
        details=profile.role_details
        self.root.setText(f"Folder: {profile.root}")
        self.guidance.setText(
            f"{profile.display_role}. {details.summary} {details.consequences}"
            if details is not None else
            "This storage type is unsupported and cannot be opened."
        )

    def _choose_another_folder(self) -> None:
        self.choose_folder=True
        super().accept()

    def accept(self) -> None:
        profile=self._selected()
        if profile is None:
            return
        self.profile=profile
        super().accept()


class LibraryScopeDialog(QDialog):
    """Choose the shared application-wide subset of peer image libraries."""

    def __init__(
        self,registry: LibraryProfileRegistry,parent=None,*,
        selected_library_ids=(),default_library_id: str="",counts_by_id=None,
    ) -> None:
        super().__init__(parent)
        self.registry=registry
        self.scope_library_ids: tuple[str,...]=()
        self.choose_folder=False
        self.counts_by_id=dict(counts_by_id or {})
        self.profiles=tuple(
            profile for profile in registry.profiles
            if not registry.validate(profile,replacing_id=profile.stable_id)
        )
        explicit=tuple(
            value for value in dict.fromkeys(str(item) for item in selected_library_ids)
            if registry.get(value) is not None
        )
        selected=set(explicit or (profile.stable_id for profile in self.profiles))

        first_library=not self.profiles
        self.setWindowTitle("Choose Your First Image Library" if first_library else "Filter Image Libraries")
        if first_library:self.resize(680,180)
        else:self.resize(720,430)
        layout=QVBoxLayout(self)
        intro=QLabel(
            "Choose the folder for your first image library. This saves its location; "
            "CollectiGen will not move or change your images. You can add more libraries "
            "or filter them later."
            if first_library else
            "Choose which peer collections the whole app should consider. All Libraries "
            "is the normal view. Filtering changes only what is shown and considered; it "
            "does not mix, copy, move, upload, or rewrite images."
        )
        intro.setWordWrap(True); layout.addWidget(intro)
        self.all_libraries=QCheckBox(
            f"All Libraries ({len(self.profiles):,} configured)"
        )
        self.all_libraries.setChecked(not explicit or len(selected)==len(self.profiles))
        self.all_libraries.setAccessibleDescription(
            "Include every valid named image library in Dataset and other image-working sections."
        )
        self.all_libraries.setVisible(not first_library)
        layout.addWidget(self.all_libraries)
        self.libraries=QListWidget()
        self.libraries.setAccessibleName("Included image libraries")
        for profile in self.profiles:
            count=max(0,int(self.counts_by_id.get(profile.stable_id,0)))
            marker=" • default" if profile.stable_id==default_library_id else ""
            item=QListWidgetItem(
                f"{profile.name} — {profile.display_role} — {count:,} image(s){marker}"
            )
            item.setData(Qt.UserRole,profile.stable_id)
            item.setFlags(item.flags()|Qt.ItemIsUserCheckable)
            item.setCheckState(
                Qt.Checked if profile.stable_id in selected else Qt.Unchecked
            )
            details=profile.role_details
            item.setToolTip(
                f"{profile.root}\n{details.consequences if details is not None else profile.display_role}"
            )
            self.libraries.addItem(item)
        layout.addWidget(self.libraries,0 if first_library else 1)
        self.libraries.setVisible(not first_library)
        self.summary=QLabel(); self.summary.setWordWrap(True)
        self.summary.setObjectName("mutedLabel")
        self.summary.setAccessibleName("Current image library filter")
        self.summary.setVisible(not first_library)
        layout.addWidget(self.summary)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        self.choose_folder_button=buttons.addButton(
            "Choose Library Folder…" if first_library else "Add Another Folder…",QDialogButtonBox.ActionRole,
        )
        if first_library:
            self.choose_folder_button.setDefault(True)
            self.choose_folder_button.setProperty("primary",True)
        self.apply_button=buttons.addButton(
            "Apply Library Filter",QDialogButtonBox.AcceptRole,
        )
        self.apply_button.setVisible(not first_library)
        self.choose_folder_button.clicked.connect(self._choose_another_folder)
        self.apply_button.clicked.connect(self.accept)
        buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.all_libraries.toggled.connect(self._all_toggled)
        self.libraries.itemChanged.connect(lambda _item:self._update_state())
        self._update_state()

    def _checked_ids(self) -> tuple[str,...]:
        return tuple(
            str(self.libraries.item(row).data(Qt.UserRole) or "")
            for row in range(self.libraries.count())
            if self.libraries.item(row).checkState()==Qt.Checked
        )

    def _all_toggled(self,checked: bool) -> None:
        self.libraries.setEnabled(not checked)
        if checked:
            self.libraries.blockSignals(True)
            for row in range(self.libraries.count()):
                self.libraries.item(row).setCheckState(Qt.Checked)
            self.libraries.blockSignals(False)
        self._update_state()

    def _update_state(self) -> None:
        all_selected=self.all_libraries.isChecked()
        selected=(
            tuple(profile.stable_id for profile in self.profiles)
            if all_selected else self._checked_ids()
        )
        count=sum(max(0,int(self.counts_by_id.get(value,0))) for value in selected)
        self.summary.setText(
            f"All Libraries • {len(selected):,} collections • {count:,} known image(s)"
            if all_selected else
            f"Filtered view • {len(selected):,} of {len(self.profiles):,} collections • {count:,} known image(s)"
        )
        self.summary.setAccessibleDescription(self.summary.text())
        self.apply_button.setEnabled(bool(selected))
        self.libraries.setEnabled(not all_selected)

    def _choose_another_folder(self) -> None:
        self.choose_folder=True
        super().accept()

    def accept(self) -> None:
        selected=self._checked_ids()
        if not self.all_libraries.isChecked() and not selected:
            return
        # Empty is the durable representation for All Libraries so newly added
        # peer collections are included automatically.
        self.scope_library_ids=(
            () if self.all_libraries.isChecked() else selected
        )
        super().accept()


class LibraryDestinationDialog(QDialog):
    """Choose one configured image library without probing remote storage."""

    def __init__(
        self,
        registry: LibraryProfileRegistry,
        parent=None,
        *,
        active_library_id: str = "",
        source_count: int = 0,
        naming_policy: str = LIBRARY_NAMING_KEEP,
    ) -> None:
        super().__init__(parent)
        self.registry=registry
        self.profile: LibraryProfile | None=None
        self.naming_policy=normalize_library_naming_policy(naming_policy)
        self.setWindowTitle("Choose Named Library")
        self.resize(680,300)
        layout=QVBoxLayout(self)
        scope=(
            "1 selected image family"
            if source_count==1 else f"{source_count:,} selected image families"
        )
        intro=QLabel(
            f"Choose where {scope} should move. Nothing changes until you confirm the exact preview."
        )
        intro.setWordWrap(True); layout.addWidget(intro)
        form=QFormLayout()
        self.destination=QComboBox()
        self.destination.setAccessibleName("Destination named library")
        for profile in registry.profiles:
            issues=registry.validate(profile,replacing_id=profile.stable_id)
            if issues:
                continue
            self.destination.addItem(
                f"{profile.name} — {profile.display_role}",profile.stable_id,
            )
        selected=self.destination.findData(active_library_id)
        self.destination.setCurrentIndex(selected if selected>=0 else 0)
        form.addRow("Destination",self.destination)
        self.naming=QComboBox()
        self.naming.setAccessibleName("Destination image naming policy")
        self.naming.addItem("Keep current filename",LIBRARY_NAMING_KEEP)
        self.naming.addItem("Rename using image hash",LIBRARY_NAMING_IMAGE_HASH)
        selected_naming=self.naming.findData(self.naming_policy)
        self.naming.setCurrentIndex(selected_naming if selected_naming>=0 else 0)
        form.addRow("File naming",self.naming)
        layout.addLayout(form)
        self.root=QLabel(); self.root.setWordWrap(True)
        self.root.setTextInteractionFlags(
            self.root.textInteractionFlags() | Qt.TextSelectableByMouse
        )
        self.root.setAccessibleName("Destination folder")
        layout.addWidget(self.root)
        self.guidance=QLabel(); self.guidance.setWordWrap(True)
        self.guidance.setObjectName("mutedLabel")
        self.guidance.setAccessibleName("Move consequence summary")
        layout.addWidget(self.guidance)
        self.storage_details=QLabel(); self.storage_details.setWordWrap(True)
        self.storage_details.setObjectName("mutedLabel")
        self.storage_details.setAccessibleName("Complete destination storage and naming details")
        self.details_toggle=DisclosureButton(
            "Show Storage Details…","Hide Storage Details",self.storage_details,
        )
        self.details_toggle.setAccessibleDescription(
            "Show the complete storage-role, availability, transfer, and naming consequences."
        )
        layout.addWidget(self.details_toggle,0,Qt.AlignLeft)
        layout.addWidget(self.storage_details)
        layout.addStretch(1)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        self.continue_button=buttons.addButton(
            "Review Move…",QDialogButtonBox.AcceptRole,
        )
        self.continue_button.clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.destination.currentIndexChanged.connect(self._update_destination)
        self.naming.currentIndexChanged.connect(self._update_destination)
        self._update_destination()

    def _selected(self) -> LibraryProfile | None:
        return self.registry.get(str(self.destination.currentData() or ""))

    def _update_destination(self) -> None:
        profile=self._selected()
        self.continue_button.setEnabled(profile is not None)
        if profile is None:
            self.root.setText("No valid named library is configured.")
            self.guidance.setText(
                "Open Setup > Library & Storage > Locations to add or correct a library definition."
            )
            self.storage_details.clear()
            self.details_toggle.hide()
            return
        self.details_toggle.show()
        details=profile.role_details
        naming_note=(
            "Images and every recognized related file will use the full SHA-256 of the exact image bytes; duplicates and conflicting families stop for review. This is an operation-only choice; the reusable default remains in Rules & Automation."
            if self.naming.currentData()==LIBRARY_NAMING_IMAGE_HASH else
            "Images and every recognized related file will keep the current image stem. This is an operation-only choice; the reusable default remains in Rules & Automation."
        )
        self.root.setText(f"Folder: {profile.root}")
        role_summary={
            LibraryRole.LOCAL_ONLY:(
                "Local destination. The exact preview will choose safe rename or verified copy."
            ),
            LibraryRole.NETWORK_SHARED:(
                "Shared destination. Move uses verified copy followed by source removal."
            ),
            LibraryRole.CLOUD_SYNCED:(
                "Cloud-synced destination. Online-only families will be held; available files use verified copy."
            ),
            LibraryRole.REMOVABLE_ARCHIVE:(
                "Removable or archive destination. Move uses verified copy followed by source removal."
            ),
        }.get(profile.role,"Unsupported destination role.")
        naming_summary=(
            "Names use the full image SHA-256."
            if self.naming.currentData()==LIBRARY_NAMING_IMAGE_HASH else
            "Current filenames are kept."
        )
        self.guidance.setText(
            f"{role_summary} {naming_summary} This choice applies to this move only."
        )
        self.storage_details.setText(
            f"{profile.display_role}. {details.summary} {details.consequences} {naming_note}"
            if details is not None else
            "This storage type is unsupported and cannot be selected."
        )

    def accept(self) -> None:
        profile=self._selected()
        if profile is None:
            return
        self.profile=profile
        self.naming_policy=normalize_library_naming_policy(self.naming.currentData())
        super().accept()
