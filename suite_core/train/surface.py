"""Focused entry and retained-review presentation for the Train module.

Package selection, verification and execution stay with the existing training
controllers. Presentation rows are not another run store or job queue.
"""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QMenu, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from qt_app.components import PageHeader


class TrainSurface(QWidget):
    package_requested = Signal()
    draft_requested = Signal()
    history_requested = Signal()
    preparations_requested = Signal()
    comparison_requested = Signal()
    experiments_requested = Signal()
    interrupted_package_requested = Signal()
    review_requested = Signal(str)
    manager_requested = Signal(str)
    help_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName("Train workflow")
        self._busy = False
        layout = QVBoxLayout(self)
        layout.addWidget(PageHeader("Train", "Open a prepared package, review it, then choose when to train."))
        self.guidance = QLabel(
            "Prepare and select images in Organizer. Open its training package here to review exact data, "
            "model, trainer and resource requirements before starting."
        )
        self.guidance.setWordWrap(True)
        layout.addWidget(self.guidance)
        actions = QHBoxLayout()
        self.open_package = QPushButton("Open Training Package…")
        self.open_package.setProperty("primary", True)
        self.open_package.clicked.connect(self.package_requested.emit)
        self.history = QPushButton("Saved Preparations…")
        self.history.clicked.connect(self.preparations_requested.emit)
        self.more = QToolButton()
        self.more.setText("More…")
        self.more.setAccessibleName("Other training workflows and Manager setup")
        self.more.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self.more)
        menu.addAction("Saved Runs…", self.history_requested.emit)
        self._another_package = menu.addAction("Open Another Training Package…", self.package_requested.emit)
        self._another_package.setVisible(False)
        menu.addAction("Open Saved Training Draft…", self.draft_requested.emit)
        menu.addAction("Compare Samples…", self.comparison_requested.emit)
        menu.addAction("Training Experiments…", self.experiments_requested.emit)
        menu.addAction("Review Interrupted Package…", self.interrupted_package_requested.emit)
        menu.addSeparator()
        menu.addAction("Save and Return to Manager Setup…", lambda: self.manager_requested.emit("settings"))
        self.more.setMenu(menu)
        self.help = QPushButton('Help')
        self.help.setAccessibleName('Help for Train')
        self.help.clicked.connect(self.help_requested.emit)
        for widget in (self.open_package, self.history, self.more, self.help):
            actions.addWidget(widget)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.review_row = QWidget()
        review_layout = QHBoxLayout(self.review_row)
        review_layout.setContentsMargins(0, 0, 0, 0)
        self.reviews = QComboBox()
        self.reviews.setAccessibleName("Open training reviews")
        self.continue_review = QPushButton("Continue Review")
        self.continue_review.setProperty("primary", True)
        self.continue_review.clicked.connect(self._continue_review)
        review_layout.addWidget(self.reviews, 1)
        review_layout.addWidget(self.continue_review)
        layout.addWidget(self.review_row)
        self.review_row.hide()
        self.status = QLabel("No training package is open. Opening a package does not start training.")
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Training workflow status")
        layout.addWidget(self.status)
        layout.addStretch(1)

    def set_reviews(self, rows):
        """Project controller-owned (identity, label) pairs, preserving selection."""
        selected = self.reviews.currentData()
        self.reviews.clear()
        for identity, label in rows:
            self.reviews.addItem(label, identity)
        index = self.reviews.findData(selected)
        if index >= 0:
            self.reviews.setCurrentIndex(index)
        active = self.reviews.count() > 0
        self._another_package.setVisible(active)
        self.review_row.setVisible(active)
        self.open_package.setVisible(not active)
        self.guidance.setVisible(not active)
        self.continue_review.setEnabled(active and not self._busy)

    def _continue_review(self):
        identity = self.reviews.currentData()
        if isinstance(identity, str) and identity:
            self.review_requested.emit(identity)

    def set_status(self, message, *, busy=False):
        self._busy = bool(busy)
        self.status.setText(message)
        self.open_package.setEnabled(not self._busy)
        self.history.setEnabled(not self._busy)
        self.more.setEnabled(not self._busy)
        self.continue_review.setEnabled(self.reviews.count() > 0 and not self._busy)

    def set_closing(self):
        self.help.setEnabled(False)
        self.set_status("Waiting for the original training work and saved reviews to close safely…", busy=True)
