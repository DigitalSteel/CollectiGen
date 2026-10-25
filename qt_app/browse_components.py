"""Shared read-only Qt result model; no application initialization."""
from dataclasses import dataclass
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QBrush, QColor

@dataclass(frozen=True)
class IncrementalResultRow:
    """One stable record presented by a bounded model-backed result view."""

    stable_id: str
    columns: tuple[str, ...]
    tooltip: str = ""
    accessible_text: str = ""
    foreground: str = ""


class IncrementalResultModel(QAbstractTableModel):
    """Reusable stable-ID table model with explicit bounded continued fetch."""

    counts_changed=Signal(int,int)

    def __init__(self,headers,*,page_size=200,parent=None):
        super().__init__(parent); self.headers=tuple(str(value) for value in headers); self.page_size=max(1,int(page_size)); self._rows=[]; self._loaded=0; self._positions={}; self._explicit_fetch=False

    @property
    def loaded_count(self):return self._loaded

    @property
    def total_count(self):return len(self._rows)

    @property
    def has_more(self):return self._loaded<len(self._rows)

    def set_rows(self,rows,*,preserve_count=0,ensure_ids=()):
        rows=list(rows); positions={row.stable_id:index for index,row in enumerate(rows)}
        required=max(self.page_size,int(preserve_count or 0))
        for stable_id in ensure_ids:
            if stable_id in positions:required=max(required,positions[stable_id]+1)
        self.beginResetModel(); self._rows=rows; self._positions=positions; self._loaded=min(len(rows),required); self.endResetModel(); self.counts_changed.emit(self._loaded,len(self._rows))

    def rowCount(self,parent=QModelIndex()):return 0 if parent.isValid() else self._loaded

    def columnCount(self,parent=QModelIndex()):return 0 if parent.isValid() else len(self.headers)

    def headerData(self,section,orientation,role=Qt.DisplayRole):
        if role==Qt.DisplayRole and orientation==Qt.Horizontal and 0<=section<len(self.headers):return self.headers[section]
        return None

    def data(self,index,role=Qt.DisplayRole):
        if not index.isValid() or not (0<=index.row()<self._loaded):return None
        row=self._rows[index.row()]
        if role==Qt.DisplayRole:return row.columns[index.column()] if index.column()<len(row.columns) else ""
        if role==Qt.UserRole:return row.stable_id
        if role==Qt.ToolTipRole:return row.tooltip
        if role==Qt.AccessibleTextRole:return row.accessible_text or "; ".join(row.columns)
        if role==Qt.ForegroundRole and index.column()==0 and row.foreground:return QBrush(QColor(row.foreground))
        return None

    def flags(self,index):
        return Qt.NoItemFlags if not index.isValid() else Qt.ItemIsEnabled|Qt.ItemIsSelectable

    def canFetchMore(self,parent=QModelIndex()):return not parent.isValid() and self._explicit_fetch and self.has_more

    def fetchMore(self,parent=QModelIndex()):
        if not self.canFetchMore(parent):return
        self._explicit_fetch=False
        first=self._loaded; count=min(self.page_size,len(self._rows)-first)
        self.beginInsertRows(QModelIndex(),first,first+count-1); self._loaded+=count; self.endInsertRows(); self.counts_changed.emit(self._loaded,len(self._rows))

    def fetch_more(self):
        self._explicit_fetch=True
        try:self.fetchMore(QModelIndex())
        finally:self._explicit_fetch=False

    def stable_id(self,index):return str(index.data(Qt.UserRole) or "") if index.isValid() else ""

    def index_for_id(self,stable_id,column=0):
        row=self._positions.get(str(stable_id),-1)
        return self.index(row,column) if 0<=row<self._loaded else QModelIndex()

    def ensure_visible_row(self,row):
        """Insert one newly created result at the loaded boundary without reset."""
        if self.update_row(row):return
        position=self._loaded
        self.beginInsertRows(QModelIndex(),position,position)
        self._rows.insert(position,row)
        self._loaded+=1
        for index in range(position,len(self._rows)):
            self._positions[self._rows[index].stable_id]=index
        self.endInsertRows()
        self.counts_changed.emit(self._loaded,len(self._rows))

    def update_row(self,row):
        position=self._positions.get(row.stable_id,-1)
        if position<0:return False
        self._rows[position]=row
        if position<self._loaded:self.dataChanged.emit(self.index(position,0),self.index(position,len(self.headers)-1))
        return True


