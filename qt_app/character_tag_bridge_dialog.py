from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,QComboBox,QDialog,QDialogButtonBox,QHeaderView,QHBoxLayout,QLabel,
    QLineEdit,QMessageBox,QPushButton,QTreeView,QVBoxLayout,
)
from .status_notifications import FooterMessageBox as QMessageBox

from core import CharacterDatabase,CharacterTagBridge,TagDatabase
from .components import IncrementalResultModel,IncrementalResultRow
from .modal_feedback import FeedbackEnabledDialog
from .tag_relationship_editor import TagReferencePickerDialog


class CharacterTagBridgeDialog(FeedbackEnabledDialog):
    """Review safe Character Source/Character/Outfit to reusable Tag links."""

    feedback_workflow = "Character tag link review"

    PAGE_SIZE = 200

    def __init__(
        self, bridge: CharacterTagBridge, tags: TagDatabase,
        characters: CharacterDatabase, categories=None, parent=None, *, tag_id: str = "",
    ) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.report = bridge.report()
        self.tags = tags
        self.characters = characters
        self.categories = categories
        self.tag_id = tag_id
        self.tag_kind = ""
        if tag_id:
            tag = tags.tags.get(tag_id)
            role = categories.effective_semantic_role(tag.category) if tag is not None and categories is not None else ""
            self.tag_kind = {"character_source": "series", "character_identity": "character", "character_outfit": "outfit"}.get(role, "")
            if not self.tag_kind:
                raise ValueError("Choose an existing Character Source, Identity, or Outfit Tag.")
        self.applied = False
        self.setWindowTitle("Review Character Tag Links")
        self.resize(1180, 720)

        layout = QVBoxLayout(self)
        heading = QLabel("Character Source, character, and outfit Tag links")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        intro = QLabel(
            "Character Sources, characters, and outfits remain typed records behind their matching reusable Tags. "
            "Sources use Character Source tags, characters use Character Identity tags, and named outfits use "
            "Character Outfit tags. Outfit "
            "associations describe known relationships and never add a character or outfit to an image automatically."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.summary = QLabel()
        layout.addWidget(self.summary)
        boundary = QLabel(
            f"{self.report.character_source_count:,} typed Character Source records are available for stable Tag links. "
            "Save exact matches directly, create or choose a Tag for missing matches, and compare candidates for "
            "ambiguous or conflicting matches. Tag creation and Character Knowledge linking are separate reviewed saves; "
            "no image data, captions, Metadata, or image files are rewritten."
        )
        boundary.setObjectName("mutedLabel")
        boundary.setWordWrap(True)
        if self.tag_id:
            self.setWindowTitle(f"Link Existing Record — {tags.tags[tag_id].display}")
            intro.setText(
                f'Choose the existing {self._kind_label(self.tag_kind)} record represented by '
                f'“{tags.tags[tag_id].display}”. Review its name and Character Source (series/world) before linking.'
            )
            boundary.setText(
                "Matching records are suggestions, not confirmed links. If none matches, search All compatible records "
                "for a different name, or close this window and choose Create Record in Character Knowledge. "
                "Only the selected link is saved after review; image facts remain unchanged."
            )
        layout.addWidget(boundary)

        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search identity records, Tags, sources, or associated characters…")
        self.search.setAccessibleName("Search character tag links")
        self.filter = QComboBox()
        self.filter.setAccessibleName("Character tag link status")
        for label, value in (
            ("All records", ""), ("Needs review", "review"), ("Ready to link", "ready"), ("Linked", "linked"),
            ("Missing Tag", "missing"), ("Ambiguous", "ambiguous"), ("Conflicting", "conflicting"),
            ("Character Sources", "series"), ("Characters", "character"), ("Outfits", "outfit"),
        ):
            self.filter.addItem(label, value)
        if self.report.needs_review_count:
            self.filter.setCurrentIndex(self.filter.findData("review"))
        elif self.report.ready_count:
            self.filter.setCurrentIndex(self.filter.findData("ready"))
        if self.tag_id:
            self.filter.clear()
            self.filter.addItem("Matching records", "tag_matches")
            self.filter.addItem("All compatible records", "tag_all")
        filters.addWidget(self.search, 1)
        filters.addWidget(self.filter)
        layout.addLayout(filters)

        self.model = IncrementalResultModel(
            ("Type", "Typed record", "Link status", "Tag", "Character Source", "Associated characters"),
            page_size=self.PAGE_SIZE, parent=self,
        )
        self.table = QTreeView()
        self.table.setModel(self.model)
        self.table.setRootIsDecorated(False)
        self.table.setUniformRowHeights(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAccessibleName("Character Source, character, and outfit Tag link preview")
        header = self.table.header()
        for column in (0, 2):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        for column in (1, 3, 4, 5):
            header.setSectionResizeMode(column, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        result_row = QHBoxLayout()
        self.result_status = QLabel()
        self.result_status.setObjectName("mutedLabel")
        self.load_more = QPushButton("Load More…")
        self.load_more.setProperty("compact", True)
        self.load_more.clicked.connect(self.model.fetch_more)
        result_row.addWidget(self.result_status, 1)
        result_row.addWidget(self.load_more)
        layout.addLayout(result_row)
        detail_row = QHBoxLayout()
        self.details = QLabel("Select a row to see why it linked or needs review.")
        self.details.setObjectName("mutedLabel")
        self.details.setWordWrap(True)
        self.details.setMinimumHeight(42)
        self.review_button = QPushButton("Review Selected Link…")
        self.review_button.setProperty("compact", True)
        self.review_button.setToolTip("Complete the next safe action for the selected typed record.")
        self.review_button.setAccessibleName("Review selected Character Knowledge Tag link")
        self.review_button.setEnabled(False)
        self.review_button.clicked.connect(self.review_selected_link)
        self.choose_existing_button = QPushButton("Choose Differently Named Existing Tag…")
        self.choose_existing_button.setProperty("compact", True)
        self.choose_existing_button.setToolTip(
            "Search all role-compatible Tags when the reusable Tag has a different name."
        )
        self.choose_existing_button.setAccessibleName(
            "Choose a differently named existing role-compatible Tag"
        )
        self.choose_existing_button.setEnabled(False)
        self.choose_existing_button.clicked.connect(self.choose_existing_for_selected)
        detail_row.addWidget(self.details, 1)
        detail_row.addWidget(self.choose_existing_button)
        detail_row.addWidget(self.review_button)
        layout.addLayout(detail_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.apply_button = buttons.addButton("Save Verified Links", QDialogButtonBox.ActionRole)
        self.apply_button.setProperty("primary", True)
        self.apply_button.setVisible(not bool(self.tag_id))
        self.apply_button.clicked.connect(self.apply_verified_links)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.search.textChanged.connect(self.refresh)
        self.filter.currentIndexChanged.connect(self.refresh)
        self.model.counts_changed.connect(self._update_counts)
        self.table.selectionModel().currentChanged.connect(self._show_details)
        self._update_summary()
        self.refresh()

    def _update_summary(self) -> None:
        report = self.report
        if self.tag_id:
            matches = sum(row.entity_kind == self.tag_kind and self.tag_id in row.tag_ids for row in report.rows)
            text = f"{matches:,} matching records for this Tag. Choose a record to review its link."
            self.summary.setText(text)
            self.summary.setAccessibleName(text)
            self.apply_button.setEnabled(False)
            return
        text = (
            f"{report.linked_source_count:,} sources linked  •  "
            f"{report.linked_character_count:,} characters linked  •  "
            f"{report.linked_outfit_count:,} outfits linked  •  "
            f"{report.ready_count:,} ready to link  •  {report.needs_review_count:,} need review"
        )
        self.summary.setText(text)
        self.summary.setAccessibleName(text.replace("•", ";"))
        self.apply_button.setEnabled(bool(report.ready_count))

    def refresh(self, *_args, preserve_id: str = "") -> None:
        query = self.search.text().strip().casefold()
        selected_filter = str(self.filter.currentData() or "")
        rows = []
        for row in self.report.rows:
            if self.tag_id and row.entity_kind != self.tag_kind:
                continue
            force_include = bool(preserve_id and row.stable_id == preserve_id)
            if not force_include:
                if selected_filter == "tag_matches" and self.tag_id not in row.tag_ids:
                    continue
                if selected_filter == "review" and row.status not in {"missing", "ambiguous", "conflicting"}:
                    continue
                if selected_filter in {"ready", "linked", "missing", "ambiguous", "conflicting"} and row.status != selected_filter:
                    continue
                if selected_filter in {"series", "character", "outfit"} and row.entity_kind != selected_filter:
                    continue
            tag_names = tuple(self.tags.tags[tag_id].display for tag_id in row.tag_ids if tag_id in self.tags.tags)
            source = self.characters.series.get(row.character_source_id or "")
            source_name = source.name if source is not None else "—"
            related_names = tuple(
                self.characters.characters[character_id].name
                for character_id in row.related_character_ids if character_id in self.characters.characters
            )
            records = {"series": self.characters.series, "character": self.characters.characters, "outfit": self.characters.outfits}[row.entity_kind]
            record = records.get(row.entity_id)
            aliases = tuple(record.aliases) if record is not None else ()
            searchable = " ".join((row.entity_name, *aliases, *tag_names, source_name, *related_names, row.review_reason)).casefold()
            if query and query not in searchable and not force_include:
                continue
            status = {
                "linked": "Linked", "ready": "Ready to link", "missing": "Missing Tag",
                "ambiguous": "Ambiguous", "conflicting": "Conflicting",
            }[row.status]
            rows.append(IncrementalResultRow(
                row.stable_id,
                (
                    {
                        "series": "Character Source",
                        "character": "Character",
                        "outfit": "Outfit",
                    }[row.entity_kind], row.entity_name, status,
                    ", ".join(tag_names) or "—", source_name, ", ".join(related_names) or "—",
                ),
                tooltip=row.review_reason,
            ))
        self.model.set_rows(rows)
        self.details.setText("Select a row to see why it linked or needs review.")
        self.review_button.setEnabled(False)
        self.review_button.setVisible(False)
        self.choose_existing_button.setEnabled(False)
        self.choose_existing_button.setVisible(False)
        if preserve_id:
            index = self.model.index_for_id(preserve_id)
            if index.isValid():
                self.table.setCurrentIndex(index)
                self.table.scrollTo(index)

    def _update_counts(self, shown: int, total: int) -> None:
        if shown < total:
            self.result_status.setText(f"Showing {shown:,} of {total:,} matching records. Load more to continue.")
            self.load_more.setText(f"Load {min(self.PAGE_SIZE, total - shown):,} More…")
        else:
            self.result_status.setText(f"Showing all {total:,} matching records.")
        self.load_more.setVisible(shown < total)

    def _show_details(self, current, _previous) -> None:
        stable_id = self.model.stable_id(current)
        row = next((item for item in self.report.rows if item.stable_id == stable_id), None)
        if self.tag_id:
            self.choose_existing_button.hide()
            self.review_button.setVisible(row is not None)
            self.review_button.setEnabled(False)
            self.review_button.setText("Review and Link Selected Record…")
            self.review_button.setAccessibleName("Review and link selected record to this Tag")
            if row is None:
                self.details.setText("Choose a matching record, or search All compatible records.")
                return
            try:
                plan = self.bridge.reviewed_link_plan(row.entity_kind, row.entity_id, self.tag_id)
            except ValueError as error:
                self.details.setText(str(error))
                return
            self.details.setText(f'Link “{plan.entity_name}” to “{plan.tag_name}”. The next step previews any competing matches before saving.')
            self.review_button.setEnabled(True)
            return
        self.review_button.setVisible(row is not None and row.status != "linked")
        self.review_button.setEnabled(row is not None and row.status != "linked")
        self.choose_existing_button.setVisible(row is not None and row.status == "missing")
        self.choose_existing_button.setEnabled(row is not None and row.status == "missing")
        if row is None:
            self.details.setText("Select a row to see why it linked or needs review.")
        elif row.status == "linked":
            self.details.setText(
                "Reviewed stable-ID link. Typed Character Source, character, and outfit relationships remain intact."
            )
        elif row.status == "ready":
            basis = "preferred name" if row.match_basis == "preferred_name" else "declared alias"
            self.details.setText(f"Exact match by {basis}; ready to save as a reviewed stable-ID link. Typed relationships remain intact.")
            self.review_button.setText("Save Verified Link")
            self.review_button.setToolTip("Save this exact role-compatible match as a reviewed stable-ID link.")
            self.review_button.setAccessibleName("Save selected verified Character Knowledge Tag link")
        elif row.status == "missing":
            creation_category = self.bridge.creation_category_for_role(self._role_for_row(row))
            self.review_button.setText("Create and Link Tag…")
            self.review_button.setToolTip(
                "Create the exact reusable Tag in System Knowledge, then review its separate Character Knowledge link."
            )
            self.review_button.setAccessibleName("Create and link missing reusable Tag")
            self.review_button.setEnabled(bool(creation_category))
            self.details.setText(row.review_reason + (
                " Create an exact reusable Tag, or choose an existing role-compatible Tag with a different name."
                if creation_category else
                " Choose a differently named existing Tag; this role does not have one unambiguous creation Category."
            ))
        else:
            self.review_button.setText("Compare and Choose Tag…")
            self.review_button.setToolTip("Compare the bounded role-compatible candidates and choose one reviewed link.")
            self.review_button.setAccessibleName("Compare and choose Character Knowledge Tag link")
            self.details.setText(
                row.review_reason + " Compare the role-compatible candidates before saving one reviewed link."
            )

    def _selected_row(self):
        stable_id = self.model.stable_id(self.table.currentIndex())
        return next((item for item in self.report.rows if item.stable_id == stable_id), None)

    def review_selected_link(self) -> None:
        row = self._selected_row()
        if self.tag_id:
            if row is not None and row.entity_kind == self.tag_kind:
                self._save_reviewed_link(row, self.tag_id)
            return
        if row is None or row.status == "linked":
            return
        if row.status == "ready":
            if row.tag_ids:
                self._save_reviewed_link(row, row.tag_ids[0])
            return
        role = self._role_for_row(row)
        role_label = self._role_label(row)
        if row.status == "missing":
            creation_category = self.bridge.creation_category_for_role(role)
            if not creation_category:
                QMessageBox.warning(
                    self, "Reusable Tag cannot be created here",
                    "This semantic role does not have one unambiguous creation Category. "
                    "Choose a differently named existing Tag or correct the protected Category role first.",
                )
                return
            self._choose_tag_for_row(
                row,
                allow_creation=True,
                search_text=row.entity_name,
                creation_category=creation_category,
                context_notice=(
                    f'No {role_label} Tag currently matches “{row.entity_name}”. '
                    "The reusable Tag and its Character Knowledge link are two separate recoverable saves."
                ),
            )
            return
        candidates = ", ".join(
            self.tags.tags[tag_id].display for tag_id in row.tag_ids if tag_id in self.tags.tags
        )
        self._choose_tag_for_row(
            row,
            search_text=row.entity_name,
            context_notice=(
                f"Review the bounded {role_label} candidates"
                f"{': ' + candidates if candidates else ''}. {row.review_reason}"
            ),
        )

    def choose_existing_for_selected(self) -> None:
        row = self._selected_row()
        if row is None or row.status != "missing":
            return
        self._choose_tag_for_row(
            row,
            search_text="",
            context_notice=(
                f'No exact Tag matches “{row.entity_name}”. Search all role-compatible '
                "Tags below when the correct reusable identity has a different name. No Tag will be created."
            ),
        )

    def _choose_tag_for_row(
        self, row, *, allow_creation: bool = False, search_text: str = "",
        creation_category: str = "", context_notice: str = "",
    ) -> None:
        role = self._role_for_row(row)
        role_label = self._role_label(row)
        prior_tag_ids = set(self.tags.tags)
        picker = TagReferencePickerDialog(
            self.tags, self.categories, title=f"Choose {role_label} Tag — {row.entity_name}",
            category_ids=self.bridge.category_ids_for_role(role),
            allow_identity_creation=allow_creation,
            identity_source="character_tag_bridge",
            identity_create_label="Create and Link Tag…",
            identity_category_id=creation_category,
            identity_consequence=(
                "The typed Character Knowledge record changes only after a separate link review. "
                "No image data changes."
            ),
            context_notice=context_notice,
            parent=self,
        )
        picker.search.setText(search_text)
        if picker.exec() != QDialog.Accepted:
            return
        tag_id = picker.selected_tag_id()
        created_tag_ids = getattr(picker, "created_tag_ids", lambda: ())()
        system_knowledge_saved = bool(created_tag_ids or tag_id not in prior_tag_ids)
        if system_knowledge_saved:
            self.bridge = CharacterTagBridge(self.tags, self.characters, self.categories)
            self.report = self.bridge.report()
            self._update_summary()
            row = next((item for item in self.report.rows if item.stable_id == row.stable_id), row)
        self._save_reviewed_link(row, tag_id, system_knowledge_saved=system_knowledge_saved)

    def _save_reviewed_link(self, row, tag_id: str, *, system_knowledge_saved: bool = False) -> None:
        role_label = self._role_label(row)
        try:
            plan = self.bridge.reviewed_link_plan(row.entity_kind, row.entity_id, tag_id)
        except Exception as error:
            consequence = (
                "\n\nThe independently created reusable Tag remains saved; the typed Character Knowledge "
                "record was not changed."
                if system_knowledge_saved else ""
            )
            QMessageBox.warning(self, "Character Tag Link cannot be saved", str(error) + consequence)
            if system_knowledge_saved:
                self.refresh(preserve_id=row.stable_id)
            return
        competitors = plan.excluded_competitors
        competitor_names = ", ".join(item.entity_name for item in competitors[:5])
        competitor_text = ""
        if competitors:
            competitor_text = (
                f"\n\nThis also records that {len(competitors):,} competing typed record"
                f"{'s do' if len(competitors) != 1 else ' does'} not use this same tag"
                f" ({competitor_names}{'…' if len(competitors) > 5 else ''}). "
                f"Those records remain intact and still need another {role_label} Tag."
            )
        message = (
            f'Link {self._kind_label(plan.entity_kind)} “{plan.entity_name}” '
            f'to the existing Tag “{plan.tag_name}”?'
            f"{competitor_text}\n\nA recoverable Character Knowledge backup is created first. "
            "No tag, image data, caption, Metadata, TXT file, or raw evidence changes."
        )
        if QMessageBox.question(self, "Save Reviewed Character Tag Link", message) != QMessageBox.Yes:
            if system_knowledge_saved:
                self.refresh(preserve_id=row.stable_id)
                QMessageBox.information(
                    self, "Reusable Tag saved; link not saved",
                    "The reusable System Knowledge Tag remains saved. The typed Character Knowledge record "
                    "was not changed, and you can review its link later.",
                )
            return
        self.review_button.setEnabled(False)
        self.apply_button.setEnabled(False)
        try:
            result = self.bridge.apply_reviewed_link(row.entity_kind, row.entity_id, tag_id)
        except Exception as error:
            consequence = (
                "\n\nThe independently created reusable Tag remains saved; the typed Character Knowledge "
                "record was not changed."
                if system_knowledge_saved else ""
            )
            QMessageBox.critical(self, "Character Tag Link was not saved", str(error) + consequence)
            self._update_summary()
            self.refresh(preserve_id=row.stable_id)
            return
        self.applied = True
        self.report = self.bridge.report()
        self._update_summary()
        self.refresh(preserve_id=plan.stable_id)
        QMessageBox.information(
            self, "Reviewed Character Tag Link saved",
            f'Saved the stable-ID link to “{plan.tag_name}” and {result.exclusion_decisions:,} reviewed '
            f"collision decision{'s' if result.exclusion_decisions != 1 else ''}. No image data changed.",
        )

    def apply_verified_links(self) -> None:
        if self.tag_id:
            return
        count = self.report.ready_count
        if not count:
            return
        message = (
            f"Save {count:,} reviewed stable link{'s' if count != 1 else ''} between typed Character Source, "
            "character, or outfit records and existing role-compatible Tags?\n\n"
            "A recoverable backup of Character Knowledge is created first. This does not create tags or rewrite image data, captions, Metadata, TXT files, or raw evidence."
        )
        if QMessageBox.question(self, "Save Character Tag Links", message) != QMessageBox.Yes:
            return
        self.apply_button.setEnabled(False)
        try:
            result = self.bridge.apply_safe_links()
        except Exception as error:
            QMessageBox.critical(self, "Character Tag Links were not saved", str(error))
            self.apply_button.setEnabled(True)
            return
        self.applied = True
        self.report = self.bridge.report()
        self._update_summary()
        self.refresh()
        QMessageBox.information(
            self, "Character Tag Links saved",
            f"Saved {result.source_links:,} Character Source link{'s' if result.source_links != 1 else ''}, "
            f"{result.character_links:,} character link{'s' if result.character_links != 1 else ''}, and "
            f"{result.outfit_links:,} outfit link{'s' if result.outfit_links != 1 else ''}. No image data changed.",
        )

    @staticmethod
    def _kind_label(entity_kind: str) -> str:
        return {
            "series": "Character Source",
            "character": "character",
            "outfit": "outfit",
        }.get(entity_kind, entity_kind)

    @staticmethod
    def _role_for_row(row) -> str:
        return (
            "character_source" if row.entity_kind == "series" else
            "character_outfit" if row.entity_kind == "outfit" else
            "character_identity"
        )

    @staticmethod
    def _role_label(row) -> str:
        return (
            "Character Source" if row.entity_kind == "series" else
            "Character Outfit" if row.entity_kind == "outfit" else
            "Character Identity"
        )
