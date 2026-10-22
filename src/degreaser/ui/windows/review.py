"""Review screen: every decision in a filterable table, editable before commit (§7)."""

import datetime as dt
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from degreaser.core.categories import format_category
from degreaser.core.model import EffectiveState
from degreaser.core.triage.session import TriageSession
from degreaser.ui.format import human_bytes, human_count
from degreaser.ui.widgets.icons import STATE_LABELS, state_icon

ROW_LIMIT = 100_000

# filter id → (label, SQL selecting (path, is_dir, action) rows)
FILTERS: dict[str, tuple[str, str]] = {
    "all": ("All decisions", "SELECT path, is_dir, action FROM decisions"),
    "delete": (
        "Marked for deletion",
        "SELECT path, is_dir, action FROM decisions WHERE action = 'delete'",
    ),
    "keep": ("Kept", "SELECT path, is_dir, action FROM decisions WHERE action = 'keep'"),
    "categorised": (
        "Categorised",
        "SELECT d.path, d.is_dir, d.action FROM node_categories c JOIN decisions d USING (path)",
    ),
    "skip": ("Skipped", "SELECT path, is_dir, action FROM decisions WHERE action = 'skip'"),
    "deferred": (
        "Deferred",
        "SELECT path, is_dir, action FROM decisions WHERE action = 'deferred'",
    ),
    "new": (
        "New files",
        "SELECT n.path, n.is_dir, d.action FROM nodes n LEFT JOIN decisions d USING (path)"
        " WHERE n.flag_new = 1",
    ),
    "changed": (
        "Changed files",
        "SELECT n.path, n.is_dir, d.action FROM nodes n LEFT JOIN decisions d USING (path)"
        " WHERE n.flag_changed = 1",
    ),
}

COLUMNS = ["Path", "Your decision", "Effective", "Category", "Size", "Modified"]


class ReviewModel(QAbstractTableModel):
    def __init__(self, session: TriageSession) -> None:
        super().__init__()
        self.session = session
        self.rows: list[tuple[str, int, str | None]] = []
        self.truncated = False
        self._state: dict[str, EffectiveState] = {}
        self._node: dict[str, tuple[int | None, float | None, int | None]] = {}

    def load(self, filter_id: str, text: str, category_prefix: str) -> None:
        sql = FILTERS[filter_id][1]
        params: list[Any] = []
        if text:
            sql = f"SELECT * FROM ({sql}) WHERE instr(lower(path), lower(?)) > 0"
            params.append(text)
        rows = self.session.conn.execute(
            f"{sql} ORDER BY path LIMIT {ROW_LIMIT + 1}", params
        ).fetchall()
        rows = [(r[0], r[1], r[2]) for r in rows]
        if category_prefix:
            prefix = category_prefix.casefold()
            rows = [
                r
                for r in rows
                if (c := self.state(r[0]).category)
                and format_category(c).casefold().startswith(prefix)
            ]
        self.beginResetModel()
        self.truncated = len(rows) > ROW_LIMIT
        self.rows = rows[:ROW_LIMIT]
        self._state.clear()
        self._node.clear()
        self.endResetModel()

    def state(self, path: str) -> EffectiveState:
        if path not in self._state:
            self._state[path] = self.session.state_of(path)
        return self._state[path]

    def node(self, path: str) -> tuple[int | None, float | None, int | None]:
        if path not in self._node:
            row = self.session.conn.execute(
                "SELECT size, mtime, subtree_bytes FROM nodes WHERE path = ?", (path,)
            ).fetchone()
            self._node[path] = (row[0], row[1], row[2]) if row else (None, None, None)
        return self._node[path]

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = 0) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = 0) -> Any:
        if not index.isValid():
            return None
        path, is_dir, action = self.rows[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            match column:
                case 0:
                    return f"{path}/" if is_dir else path
                case 1:
                    if action is None:
                        return "—"
                    return f"{action} folder" if is_dir else action
                case 2:
                    state = self.state(path)
                    label = STATE_LABELS[state.action]
                    return (
                        label
                        if state.origin.value == "direct"
                        else f"{label} ({state.origin.value})"
                    )
                case 3:
                    category = self.state(path).category
                    return format_category(category) if category else ""
                case 4:
                    size, _, subtree = self.node(path)
                    return human_bytes(subtree if is_dir else size)
                case 5:
                    mtime = self.node(path)[1]
                    return (
                        dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime else ""
                    )
        if role == Qt.ItemDataRole.DecorationRole and column == 2:
            state = self.state(path)
            return state_icon(state.action, state.origin, bool(state.category))
        if role == Qt.ItemDataRole.ToolTipRole and column == 2:
            state = self.state(path)
            return f"Decided by: {state.decided_by}" if state.decided_by else None
        return None


class ReviewWindow(QMainWindow):
    changed = Signal()
    commit_requested = Signal()

    def __init__(self, session: TriageSession, project_name: str) -> None:
        super().__init__()
        self.session = session
        self.setWindowTitle(f"Review — {project_name}")
        self.resize(1100, 700)

        central = QWidget()
        layout = QVBoxLayout(central)
        self.totals = QLabel()
        layout.addWidget(self.totals)

        filters = QHBoxLayout()
        self.filter_box = QComboBox()
        for filter_id, (label, _) in FILTERS.items():
            self.filter_box.addItem(label, filter_id)
        self.path_filter = QLineEdit()
        self.path_filter.setPlaceholderText("Path contains…")
        self.category_filter = QLineEdit()
        self.category_filter.setPlaceholderText("Category starts with… (e.g. Videos › High School)")
        for w in (self.filter_box, self.path_filter, self.category_filter):
            filters.addWidget(w)
        self.filter_box.currentIndexChanged.connect(self.reload)
        self.path_filter.returnPressed.connect(self.reload)
        self.category_filter.returnPressed.connect(self.reload)
        layout.addLayout(filters)

        self.model = ReviewModel(session)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        self.count_label = QLabel()
        layout.addWidget(self.count_label)

        buttons = QHBoxLayout()
        for text, action in (("Keep", "keep"), ("Delete", "delete"), ("Skip", "skip")):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, a=action: self.apply(a))
            buttons.addWidget(button)
        clear = QPushButton("Clear Decision")
        clear.clicked.connect(lambda: self.apply(None))
        buttons.addWidget(clear)
        buttons.addStretch()
        commit = QPushButton("Commit…")
        commit.setDefault(True)
        commit.clicked.connect(self.commit_requested.emit)
        buttons.addWidget(commit)
        layout.addLayout(buttons)
        self.setCentralWidget(central)

        self.reload()

    def reload(self) -> None:
        self.model.load(
            self.filter_box.currentData(),
            self.path_filter.text().strip(),
            self.category_filter.text().strip().replace(", ", " › "),
        )
        count = len(self.model.rows)
        shown = f"{human_count(count)} item{'' if count == 1 else 's'}"
        self.count_label.setText(
            f"Showing the first {shown} — narrow the filter to see the rest"
            if self.model.truncated
            else shown
        )
        self.update_totals()

    def update_totals(self) -> None:
        summary = self.session.tracker.summary
        if summary is None:
            self.totals.setText("Counting…")
            return
        stale = " (updating…)" if self.session.tracker.stale else ""
        self.totals.setText(
            f"<b>{human_count(summary.files_delete)}</b> files to delete"
            f" (<b>{human_bytes(summary.bytes_delete)}</b>) · "
            f"<b>{human_count(summary.files_categorised)}</b> categorised · "
            f"{human_count(summary.files_skip)} skipped · "
            f"{human_count(summary.files_untriaged)} untriaged{stale}"
        )

    def selected_paths(self) -> list[str]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.model.rows[r][0] for r in rows]

    def apply(self, action: str | None) -> None:
        paths = self.selected_paths()
        if not paths:
            return
        try:
            self.session.review_set(paths, action)
        except ValueError as exc:
            QMessageBox.information(self, "Can't apply to folders", str(exc))
            return
        self.reload()
        self.changed.emit()
