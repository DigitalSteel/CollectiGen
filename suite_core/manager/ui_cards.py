"""Small presentation-only Qt card helpers; palette follows the existing theme."""
from PySide6.QtWidgets import QWidget, QGridLayout, QFrame, QLabel, QVBoxLayout, QHBoxLayout, QToolButton, QLayout, QSizePolicy, QAbstractButton
from PySide6.QtCore import Qt, QSize, QEvent


def workspace_layout(body):
    """Common workspace content inset; inherit the selected theme and font."""
    layout = QVBoxLayout(body)
    layout.setContentsMargins(12, 12, 12, 12)
    return layout


def workspace_heading(title):
    heading = QLabel(title)
    heading.setObjectName("workspaceHeading")
    heading.setStyleSheet("font-size:26px;font-weight:600")
    heading.setWordWrap(True)
    return heading


def unavailable_action_style(widget):
    # Reuse live main-app palette roles rather than freezing pre-polish colors.
    widget.setStyleSheet("QAbstractButton:disabled {"
        "color:palette(text);background:palette(button);"
        "border:1px dashed palette(mid);padding:5px;}")


def card_style(widget, *, compact=False):
    widget.setStyleSheet(
        "QFrame#visualCard {background:palette(base);color:palette(text);"
        "border:1px solid palette(midlight);border-radius:9px;}"
        "QFrame#visualCard QLabel {color:palette(text);}"
        "QLabel#cardTitle {font-size:20px;font-weight:600;}"
        "QLabel#metricValue {font-size:27px;font-weight:600;}"
        "QLabel#statusBadge {color:palette(text);background:palette(alternate-base);"
        "font-size:11px;font-weight:600;padding:4px 8px;border-radius:6px;}"
        + ("QFrame#visualCard:hover {border-color:palette(highlight);}"
           "QLabel#cardTitle {font-size:17px;font-weight:600;}"
           "QLabel#cardIcon {background:palette(alternate-base);"
           "border-left:3px solid palette(highlight);border-radius:6px;padding:6px;}"
           if compact else ""))
    if compact:
        widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)


def compact_action(button):
    """Existing main-app compact button convention, without eager core imports."""
    button.setProperty("compact", True)
    button.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
    button.ensurePolished()
    button.setMinimumWidth(max(button.sizeHint().width(),
        button.fontMetrics().horizontalAdvance(button.text()) + 24))
    return button


def development_card(component):
    """A roadmap preview only; never grants inventory, package or launch status."""
    from PySide6.QtWidgets import QPushButton
    from qt_app.navigation_icons import navigation_icon
    card = QFrame(); card.setObjectName("visualCard"); card_style(card, compact=True)
    card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    card.setProperty("componentKey", component.key)
    card.setAccessibleName(component.title + ": In development")
    body = QVBoxLayout(card); body.setContentsMargins(14, 14, 14, 14); body.setSpacing(8)
    header = QHBoxLayout()
    icon = QLabel(); icon.setObjectName("cardIcon")
    glyph = {"media-organizer": "Dataset", "everday": "Rules & Automation",
             "default-tag-catalog": "Tag Encyclopedia"}.get(component.key, "Setup")
    icon.setPixmap(navigation_icon(glyph).pixmap(28, 28))
    icon.setToolTip("Application identity symbol, not an official logo")
    header.addWidget(icon)
    title = QLabel(component.title); title.setObjectName("cardTitle"); title.setWordWrap(True)
    header.addWidget(title, 1); body.addLayout(header)
    for value, name in (("In development", "statusBadge"), (component.description, "")):
        label = QLabel(value); label.setObjectName(name); label.setWordWrap(True)
        label.setTextFormat(Qt.PlainText)
        if not name:
            label.setToolTip(value)
            policy = label.sizePolicy(); policy.setHorizontalPolicy(QSizePolicy.Ignored)
            policy.setHeightForWidth(True); label.setSizePolicy(policy)
            label.setMinimumWidth(0); label.updateGeometry()
        if name: body.addWidget(label, 0, Qt.AlignLeft)
        else: body.addWidget(label)
    install = compact_action(QPushButton("Install"))
    install.setObjectName("developmentInstall:" + component.key)
    install.setEnabled(False)
    install.setAccessibleName("Install " + component.title + " unavailable")
    install.setAccessibleDescription("No compatible installation package is available yet.")
    install.setToolTip("No compatible installation package is available yet.")
    version = getattr(component, "source_version", "")
    from core.component_release_identity import display_component_version
    version = display_component_version(component.key, version, include_legacy=True)
    date = getattr(component, "release_date", "")
    source_detail = ("\n\nSource version: " + version + (" (" + date + ")" if date else "")) if version else ""
    detail = details_panel("Details", component.requirement + source_detail +
        "\n\nNo compatible installation package available yet. Installed status: unknown.")
    detail.findChild(QToolButton).setAccessibleName("Details for " + component.title)
    install.setParent(card); install.hide()  # Hidden diagnostic control, no primary install offer.
    body.addStretch()
    detail.findChild(QToolButton).setText("View details")
    body.addWidget(detail); card.updateGeometry()
    return card


class ResponsiveCardGrid(QWidget):
    def __init__(self, *, minimum_card_width=320, maximum_columns=3):
        super().__init__()
        self.grid = QGridLayout(self); self.grid.setContentsMargins(0, 0, 0, 0); self.grid.setSpacing(18)
        self.grid.setSizeConstraints(QLayout.SetNoConstraint, QLayout.SetMinimumSize)
        self.cards = []; self.columns = 1
        self.minimum_card_width = minimum_card_width; self.maximum_columns = maximum_columns

    def set_cards(self, cards):
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        self.cards = list(cards); self.reflow()

    def reflow(self):
        while self.grid.count(): self.grid.takeAt(0)
        for row in range(self.grid.rowCount()): self.grid.setRowStretch(row, 0)
        gap = self.grid.horizontalSpacing()
        self.columns = max(1, min(self.maximum_columns,
            (self.width() + gap) // (self.minimum_card_width + gap)))
        for index, card in enumerate(self.cards):
            if isinstance(card, QAbstractButton):
                self.grid.addWidget(card, index // self.columns, index % self.columns, Qt.AlignLeft)
            else:
                self.grid.addWidget(card, index // self.columns, index % self.columns)
        for column in range(self.maximum_columns):
            self.grid.setColumnStretch(column, 1 if column < self.columns else 0)
            self.grid.setColumnMinimumWidth(column,
                min(self.minimum_card_width, self.width()) if column < self.columns else 0)
        content_rows = (len(self.cards) + self.columns - 1) // self.columns
        self.grid.setRowStretch(content_rows, 1)
        self.grid.setAlignment(Qt.AlignTop)
        self._card_height_hints = tuple(card.sizeHint().height() for card in self.cards)

    def event(self, event):
        if event.type() == QEvent.LayoutRequest and getattr(self, "cards", ()):
            heights = tuple(card.sizeHint().height() for card in self.cards)
            if heights != getattr(self, "_card_height_hints", None):
                self.reflow()
        return super().event(event)

    def showEvent(self, event):
        super().showEvent(event); self.reflow()

    def resizeEvent(self, event):
        super().resizeEvent(event); self.reflow()

    def minimumSizeHint(self):
        return QSize(0, self.grid.minimumSize().height())


def details_panel(title, text):
    from PySide6.QtWidgets import QTextBrowser
    browser = QTextBrowser(); browser.setPlainText(text); browser.setMaximumHeight(220)
    browser.setMinimumHeight(min(220, browser.fontMetrics().lineSpacing() * 12
        + 2 * browser.frameWidth() + 2 * int(browser.document().documentMargin())))
    return disclosure_panel(title, browser)


def disclosure_panel(title, target):
    """Retain an existing optional view behind one keyboard-accessible control."""
    root = QWidget(); layout = QVBoxLayout(root); layout.setContentsMargins(0, 0, 0, 0)
    toggle = QToolButton(); toggle.setText(title); toggle.setCheckable(True)
    toggle.setProperty("compact", True); toggle.setAccessibleName(title)
    toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon); toggle.setArrowType(Qt.RightArrow)
    target.hide()
    def change(opened):
        target.setVisible(opened); toggle.setArrowType(Qt.DownArrow if opened else Qt.RightArrow)
        layout.invalidate(); root.updateGeometry()
        parent = root.parentWidget()
        if parent is not None:
            parent.updateGeometry()
    toggle.toggled.connect(change); layout.addWidget(toggle); layout.addWidget(target)
    return root
