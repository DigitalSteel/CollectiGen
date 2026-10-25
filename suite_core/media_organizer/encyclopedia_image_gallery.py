"""Read-only local gallery for an explicitly attached Organizer-hosted browser."""
from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton

from core.organizer_example_images import OrganizerExampleImages
from qt_app.gallery import EmbeddedGallery
from suite_core.manager.ui_cards import compact_action


class HostedEncyclopediaImageGallery(QWidget):
    def __init__(self, browser, binding, organizer, parent=None):
        super().__init__(parent)
        if binding.window is not browser:
            raise ValueError('The original hosted browser is required')
        self.browser = browser
        self.bridge = OrganizerExampleImages(binding, organizer)
        self._subscription = None
        self._next = 0
        self._references = {}
        self._selected_path = ''
        body = QVBoxLayout(self)
        body.setContentsMargins(0, 0, 0, 0)
        self.explanation = QLabel('Your included local library images stay in Organizer. Choose Find Examples in Encyclopedia; opening an image uses the existing Dataset viewer.')
        self.explanation.setWordWrap(True)
        self.explanation.setTextFormat(Qt.PlainText)
        body.addWidget(self.explanation)
        self.gallery = EmbeddedGallery(page_size=12, card_width=136,
            show_open_dataset=True, compact_empty=True)
        body.addWidget(self.gallery, 1)
        self.page_status = QLabel()
        self.page_status.setWordWrap(True)
        self.page_status.setTextFormat(Qt.PlainText)
        body.addWidget(self.page_status)
        self.next_page = compact_action(QPushButton('Next examples'))
        self.next_page.clicked.connect(self.show_next_page)
        body.addWidget(self.next_page, 0, Qt.AlignLeft)
        self.gallery.chosen.connect(self._select)
        self.gallery.open_dataset_requested.connect(self.open_selected)
        self._subscription = browser.subscribe_example_results(self._accepted, self.invalidate)
        browser.destroyed.connect(self.detach)
        self.invalidate()

    def invalidate(self):
        self.bridge.clear()
        self._references.clear()
        self._next = 0
        self._selected_path = ''
        self.next_page.setEnabled(False)
        self.page_status.clear()
        self.gallery.set_unavailable(self._recovery_message('No examples are displayed.'))

    def _recovery_message(self, message):
        # Borrow the original command's admission/readiness; never invent a
        # reconnect capability or relax the host's scope to make a retry work.
        ready = False
        if self._subscription is not None:
            try:
                self.bridge._check_host()
                # Inspect admission without replacing the browser's query outcome.
                binding = self.bridge.binding
                ready = bool(binding.example_queries_admitted()
                    and self.browser._example_libraries and self.browser._example_tag_id
                    and not self.browser.stale and self.browser._example_pending is None
                    and binding.catalog_token == self.browser.example_catalog_token())
            except Exception:
                pass
        if ready and callable(getattr(self.browser, 'find_examples', None)):
            return message + ' Choose Find Examples for the current Tag to try again.'
        return message + ' Finding examples is unavailable in the current hosted library scope.'

    def _accepted(self, result):
        if self._subscription is None:
            return
        try:
            self.bridge.capture(result)
            self._next = 0
            self.show_next_page()
        except Exception:
            self._held()

    def _held(self):
        self.invalidate()
        self.gallery.set_unavailable(self._recovery_message('Examples are unavailable or changed.'))

    def show_next_page(self):
        try:
            page = self.bridge.page(self._next)
            self._references = {str(path): reference for path, reference in zip(page.paths, page.references)}
            self._next = page.next_offset
            self._selected_path = ''
            self.gallery.set_paths(page.paths, context='Local example images', configure=self._configure)
            self.gallery.open_dataset.setEnabled(False)
            if page.total == 0:
                self.gallery.set_unavailable('No matching example images were found in the included libraries.')
                self.page_status.setText('No matching examples.')
            elif not page.paths:
                self.gallery.set_unavailable(self._recovery_message('No example images in this page are currently available.'))
                self.page_status.setText(f'{page.unavailable} unavailable in this page; {page.total} matches in total.')
            else:
                self.page_status.setText(f'Matches {page.offset + 1}–{page.next_offset} of {page.total}; {page.unavailable} unavailable in this page.')
            self.next_page.setEnabled(page.next_offset < page.total)
        except Exception:
            self._held()

    def _configure(self, path, card):
        reference = self._references.get(str(path))
        if reference is not None:
            from core.library_profiles import LibraryProfileRegistry
            registry = getattr(self.bridge.binding.dataset, 'registry', None)
            profile = registry.get(reference.library_id) if isinstance(registry, LibraryProfileRegistry) else None
            name = (profile.name.strip() if profile is not None
                    and profile.stable_id == reference.library_id
                    and isinstance(profile.name, str) else '')
            card.set_source_library(name or 'Included library', 'Included library' if name else '', '')
            card.library_label.setTextFormat(Qt.PlainText)
            card.library_label.setToolTip(
                card.library_label.toolTip() + '\nLibrary ID: ' + reference.library_id)
        card.allow_data_edit = False
        card.allow_ai_actions = False
        card.allow_duplicate_check = False
        card.selection_enabled = False
        card.set_generation_actions(False)
        card.set_file_actions(())
        # Shared cards retain content-safety rendering, but their generic context
        # and default preview actions are not part of this borrowed read-only route.
        # Events go through the original host's revalidated included-image viewer.
        for widget in (card, *card.findChildren(QWidget)):
            widget.setProperty('hostedExamplePath', str(path))
            widget.installEventFilter(self)
            widget.setContextMenuPolicy(Qt.NoContextMenu)

    def eventFilter(self, watched, event):
        path = watched.property('hostedExamplePath')
        if path:
            if event.type() == QEvent.ContextMenu:
                return True
            if event.type() == QEvent.MouseButtonDblClick:
                if event.button() == Qt.LeftButton:
                    self.open_path(path)
                return True
            if event.type() == QEvent.KeyPress:
                if event.key() in {Qt.Key_Return, Qt.Key_Enter}:
                    self.open_path(path)
                    return True
                if event.key() == Qt.Key_Menu or (event.key() == Qt.Key_F10 and event.modifiers() & Qt.ShiftModifier):
                    return True
        return super().eventFilter(watched, event)

    def open_path(self, path):
        reference = self._references.get(str(path))
        if reference is None:
            return False

        try:
            opened = self.bridge.open_image(reference)
            if opened is False:
                self.page_status.setText('This image is unavailable or the Dataset handoff was declined; no image was changed.')
            return opened
        except Exception:
            self._held()
            return False

    def _select(self, path):
        self._selected_path = str(path) if str(path) in self._references else ''
        self.gallery.open_dataset.setEnabled(bool(self._selected_path))

    def open_selected(self):
        return self.open_path(self._selected_path)

    def detach(self, *unused):
        if self._subscription is not None:
            self.browser.unsubscribe_example_results(self._subscription)
            self._subscription = None
        self.bridge.close()
        self._references.clear()
        self.gallery.set_unavailable('Hosted library session detached. No local images are connected.')
        self.next_page.setEnabled(False)

    def closeEvent(self, event):
        self.detach()
        super().closeEvent(event)
