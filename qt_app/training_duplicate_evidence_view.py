from __future__ import annotations

from PySide6.QtCore import Qt,Signal
from PySide6.QtWidgets import (
    QComboBox,QHBoxLayout,QHeaderView,QLabel,QPlainTextEdit,QPushButton,
    QTreeWidget,QTreeWidgetItem,QVBoxLayout,QWidget,
)


class TrainingDuplicateEvidenceView(QWidget):
    """Bounded pair observations inside the existing frozen training review."""

    inspect_requested=Signal(str)
    review_requested=Signal()
    PAGE_SIZE=100

    def __init__(self,parent=None):
        super().__init__(parent)
        self.plan=None; self._pairs=(); self._page=0; self._by_id={}
        layout=QVBoxLayout(self)
        self.summary=QLabel(); self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.PlainText)
        self.summary.setAccessibleName("Cached duplicate evidence coverage")
        layout.addWidget(self.summary)
        note=QLabel("Compare related images before choosing their training roles. Similarity leaves your inclusion and split choices unchanged.")
        note.setWordWrap(True); note.setObjectName("mutedLabel"); layout.addWidget(note)
        row=QHBoxLayout()
        self.filter=QComboBox()
        for label,value in (("All Cached Relations","all"),("Decoded Pixel Matches","pixel_exact"),
                            ("Visual Similarity","similar"),("Byte-identical Images","byte_exact"),
                            ("Reviewed Distinct","distinct"),("Included Across Roles","cross_role")):
            self.filter.addItem(label,value)
        self.filter.setAccessibleName("Training similarity filter")
        self.filter.currentIndexChanged.connect(self._filter_changed)
        self.review=QPushButton("Compare Similar Images…")
        self.review.setToolTip("Check and compare the exact frozen training scope using Duplicate Review, then return here with refreshed evidence.")
        self.review.clicked.connect(self.review_requested)
        row.addWidget(self.filter); row.addStretch(1); row.addWidget(self.review)
        layout.addLayout(row)
        self.tree=QTreeWidget(); self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(("Evidence","First Image","Second Image","Review","Current Roles"))
        self.tree.setRootIsDecorated(False); self.tree.setSelectionMode(QTreeWidget.SingleSelection)
        self.tree.setAccessibleName("Cached related training image pairs")
        for column in range(5):self.tree.header().setSectionResizeMode(column,QHeaderView.Stretch)
        self.tree.itemSelectionChanged.connect(self._selected)
        self.tree.itemActivated.connect(lambda *_:self._inspect(False))
        layout.addWidget(self.tree,1)
        pager=QHBoxLayout()
        self.previous=QPushButton("Previous"); self.next=QPushButton("Next"); self.page_label=QLabel()
        self.previous.setAccessibleName("Previous related image pairs")
        self.next.setAccessibleName("Next related image pairs")
        self.previous.clicked.connect(lambda:self._change_page(-1))
        self.next.clicked.connect(lambda:self._change_page(1))
        pager.addWidget(self.previous); pager.addWidget(self.page_label,1); pager.addWidget(self.next)
        layout.addLayout(pager)
        actions=QHBoxLayout()
        self.first=QPushButton("Inspect First Image"); self.second=QPushButton("Inspect Second Image")
        self.first.clicked.connect(lambda:self._inspect(False)); self.second.clicked.connect(lambda:self._inspect(True))
        actions.addWidget(self.first); actions.addWidget(self.second); actions.addStretch(1)
        layout.addLayout(actions)
        self.details=QPlainTextEdit(); self.details.setReadOnly(True); self.details.setMaximumHeight(95)
        self.details.setAccessibleName("Exact related image evidence")
        layout.addWidget(self.details)

    def set_plan(self,plan):
        self.plan=plan; self._by_id={item.stable_id:item for item in plan.candidates}
        snapshot=getattr(plan.analysis,"duplicate_evidence",None)
        if snapshot is None:
            self.summary.setText("No cached similarity evidence was supplied for this plan. Compare the frozen images to check them.")
        else:
            self.summary.setText(
                f"Current cached fingerprints: {snapshot.covered_count:,} of {snapshot.eligible_count:,} images • "
                f"{snapshot.missing_count:,} unchecked • {len(snapshot.relations):,} surfaced relations • "
                f"{snapshot.lower_similarity:g}–{snapshot.upper_similarity:g}% similarity range."
                +(" Results reached a work limit; additional pairs remain unchecked." if snapshot.truncated else "")
                +(" Reviewed-distinct decisions are unavailable." if not snapshot.decision_keys_available else "")
            )
        self._filter_changed()

    def _filter_changed(self):
        snapshot=getattr(getattr(self.plan,"analysis",None),"duplicate_evidence",None)
        kind=self.filter.currentData()
        def matches(pair):
            if kind=="all":return True
            if kind=="distinct":return pair.reviewed_distinct
            if kind=="cross_role":
                first=self._by_id[pair.first_stable_id]; second=self._by_id[pair.second_stable_id]
                return first.decision==second.decision=="include" and first.role!=second.role
            return pair.kind==kind
        self._pairs=tuple(pair for pair in (snapshot.relations if snapshot else ()) if matches(pair))
        self._page=0; self._render()

    def _change_page(self,delta):
        pages=max(1,(len(self._pairs)+self.PAGE_SIZE-1)//self.PAGE_SIZE)
        self._page=max(0,min(pages-1,self._page+delta)); self._render()

    def _render(self):
        self.tree.clear()
        start=self._page*self.PAGE_SIZE
        labels={"byte_exact":"Byte-identical","pixel_exact":"Decoded Pixels Match","similar":"Visual Similarity"}
        for pair in self._pairs[start:start+self.PAGE_SIZE]:
            first=self._by_id[pair.first_stable_id]; second=self._by_id[pair.second_stable_id]
            item=QTreeWidgetItem((labels[pair.kind],first.image.name,second.image.name,
                                 "Reviewed Distinct" if pair.reviewed_distinct else "Compare",
                                 f"{first.decision}: {first.role} / {second.decision}: {second.role}"))
            item.setData(0,Qt.UserRole,pair)
            item.setToolTip(1,first.stable_id); item.setToolTip(2,second.stable_id)
            self.tree.addTopLevelItem(item)
        end=min(start+self.PAGE_SIZE,len(self._pairs))
        self.page_label.setText(f"{start+1:,}–{end:,} of {len(self._pairs):,} relations" if self._pairs else "No cached relations match this view; this does not prove visual independence.")
        self.page_label.setWordWrap(True)
        self.previous.setEnabled(self._page>0); self.next.setEnabled(end<len(self._pairs))
        self._selected()

    def _pair(self):
        item=self.tree.currentItem()
        return item.data(0,Qt.UserRole) if item is not None else None

    def _selected(self):
        pair=self._pair(); self.first.setEnabled(pair is not None); self.second.setEnabled(pair is not None)
        self.details.setVisible(pair is not None)
        self.details.setPlainText(
            f"{pair.evidence}\nSimilarity: {pair.similarity:g}%\nFirst: {pair.first_stable_id}\nSecond: {pair.second_stable_id}\n"
            +("Keep Both records a current-content comparison decision; it does not choose training inclusion or roles."
              if pair.reviewed_distinct else "Inspect both candidates, then use a reasoned override if their inclusion or roles should change.")
            if pair else "Choose a relation to inspect its exact evidence."
        )

    def _inspect(self,second):
        pair=self._pair()
        if pair:self.inspect_requested.emit(pair.second_stable_id if second else pair.first_stable_id)
