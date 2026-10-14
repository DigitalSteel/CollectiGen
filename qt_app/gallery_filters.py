"""One attached home for gallery search, stage and existing filter authorities."""
from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import QBoxLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from .tag_editor import FlowLayout


class GalleryFilters(QFrame):
    def __init__(self, stage_tabs, search, clear_callback, parent=None):
        super().__init__(parent)
        self.setObjectName("galleryToolbar")
        self.setAccessibleName("Search and Filters")
        self.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Maximum)
        self._choices=[]
        self._chips={}
        self.search=search
        layout=QVBoxLayout(self)
        layout.setContentsMargins(9,7,9,7)
        layout.setSpacing(6)
        heading=QHBoxLayout()
        heading.addWidget(search,1)
        self.filters_button=QToolButton()
        self.filters_button.setText("Filters")
        self.filters_button.setCheckable(True)
        self.filters_button.setAccessibleName("Show or hide detailed gallery filters")
        self.filters_button.setToolTip("Show detailed filters. Active filters remain visible when closed.")
        heading.addWidget(self.filters_button)
        self.clear_button=QPushButton("Clear All Filters")
        self.clear_button.setProperty("compact",True)
        self.clear_button.setToolTip("Clear search and detail filters in the current preparation stage.")
        self.clear_button.clicked.connect(clear_callback)
        heading.addWidget(self.clear_button)
        layout.addWidget(stage_tabs)
        layout.addLayout(heading)
        self.details=QWidget()
        self.details.setAccessibleName("Detailed gallery filters")
        self.controls=QGridLayout(self.details)
        self.controls.setContentsMargins(0,0,0,0)
        self.controls.setColumnStretch(0,1)
        self.controls.setColumnStretch(1,1)
        layout.addWidget(self.details)
        self.details.hide()
        self.filters_button.toggled.connect(self.details.setVisible)
        self.active=QWidget()
        self.active.setAccessibleName("Active filters")
        self.flow=FlowLayout(self.active)
        layout.addWidget(self.active)
        search.textChanged.connect(self.sync)
        stage_tabs.currentChanged.connect(self.sync)
        self.sync()

    def add_filter(self, name, widget, combo, default="all"):
        if isinstance(widget.layout(),QBoxLayout):
            widget.layout().setDirection(QBoxLayout.LeftToRight)
        label=widget.findChild(QLabel)
        if label is not None:
            label.setBuddy(combo)
        index=len(self._choices)
        self.controls.addWidget(widget,index//2,index%2)
        # Clear the child's initial hidden flag even while the disclosure is closed.
        # Page owners may subsequently hide stage-inapplicable controls explicitly.
        widget.show()
        self._choices.append((name,combo,default,widget))
        combo.currentIndexChanged.connect(self.sync)
        self.sync()

    def reset_values(self):
        controls=[self.search]+[combo for _,combo,_,_ in self._choices]
        blockers=[QSignalBlocker(control) for control in controls]
        self.search.clear()
        for _,combo,default,_ in self._choices:
            combo.setCurrentIndex(max(0,combo.findData(default)))
        del blockers
        self.sync()

    def sync(self,*_):
        values=[]
        query=self.search.text().strip()
        if query:
            values.append(("Search",query,self.search.clear))
        visible=[]
        for name,combo,default,widget in self._choices:
            self.controls.removeWidget(widget)
            if not widget.isHidden():
                visible.append(widget)
            value=combo.currentData()
            if value is not None and value!=default:
                values.append((name,combo.currentText(),lambda _checked=False,c=combo,d=default:c.setCurrentIndex(max(0,c.findData(d)))))
        for index,widget in enumerate(visible):
            self.controls.addWidget(widget,index//2,index%2,1,2 if len(visible)==1 else 1)
        keep=[]
        for name,value,callback in values:
            button=self._chips.get(name)
            if button is None:
                button=QToolButton()
                button.setObjectName("activeFilterChip")
                button.setProperty("compact",True)
                button.clicked.connect(callback)
                self._chips[name]=button
            text=f"{name}: {value}"
            # Keep arbitrarily long search terms from widening the gallery.
            label=button.fontMetrics().elidedText(text,Qt.ElideRight,max(80,min(440,self.width()-60)))
            button.setText(f"{label.replace('&','&&')} ×")
            button.setToolTip(f"Remove {text}")
            button.setAccessibleName(f"Remove {text} filter")
            keep.append(button)
        self._chips={name:self._chips[name] for name,_,_ in values}
        self.flow.replace_widgets(keep)
        self.active.setVisible(bool(keep))
        self.clear_button.setEnabled(bool(keep))
