"""Review screen: every decision in a filterable table, editable before commit (§7)."""

import datetime as dt
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
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
from degreaser.core.duplicates import KEEP_RULES, pick_copy
from degreaser.core.model import EffectiveState
from degreaser.core.triage.order import dfs_key
from degreaser.core.triage.session import TriageSession
from degreaser.ui.format import human_bytes, human_count
from degreaser.ui.theme import icon, theme
from degreaser.ui.widgets.icons import STATE_LABELS, state_icon
from degreaser.ui.widgets.screen import fit_to_screen

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
    # Shown grouped (identical files together), whatever their decisions.
    "duplicates": (
        "Duplicates",
        "SELECT n.path, n.is_dir, d.action FROM file_hashes h JOIN nodes n ON n.id = h.node_id"
        " LEFT JOIN decisions d USING (path) WHERE h.full IN (SELECT full FROM file_hashes"
        " WHERE full IS NOT NULL GROUP BY full HAVING count(*) > 1)",
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
        if filter_id == "duplicates":
            # Group identical files, groups in traversal order of their first copy.
            index = self.session.duplicates
            first = {d: dfs_key(paths[0], False) for d, paths in index.groups.items()}
            rows.sort(
                key=lambda r: (first.get(index.digest(r[0]) or b"", ()), dfs_key(r[0], False))
            )
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


class StatChip(QFrame):
    """A pill with a coloured dot, a value and a caption."""

    def __init__(self, caption: str, tone: str) -> None:
        super().__init__()
        self.setProperty("role", "pill")
        self.tone = tone
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 5, 14, 5)
        layout.setSpacing(8)
        self.dot = QLabel()
        self.value = QLabel("—")
        self.value.setStyleSheet("font-weight: 600;")
        self.caption = QLabel(caption)
        self.caption.setProperty("role", "muted")
        for w in (self.dot, self.value, self.caption):
            layout.addWidget(w)
        theme().changed.connect(lambda _t: self._paint_dot())
        self._paint_dot()

    def _paint_dot(self) -> None:
        t = theme().tokens
        color = {"danger": t.danger, "accent": t.accent, "warning": t.warning}.get(
            self.tone, t.text_faint
        )
        self.dot.setFixedSize(8, 8)
        self.dot.setStyleSheet(f"background: {color}; border-radius: 4px;")

    def set_value(self, value: str) -> None:
        self.value.setText(value)


class ReviewWindow(QMainWindow):
    changed = Signal()
    commit_requested = Signal()

    def __init__(self, session: TriageSession, project_name: str) -> None:
        super().__init__()
        self.session = session
        self.setWindowTitle(f"Review — {project_name}")
        fit_to_screen(self, 1120, 720)
        t = theme().tokens

        central = QWidget()
        central.setObjectName("central")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(14)

        header = QHBoxLayout()
        title = QLabel("Review")
        title.setProperty("role", "title")
        header.addWidget(title)
        header.addSpacing(12)
        self.chip_delete = StatChip("to delete", "danger")
        self.chip_categorised = StatChip("categorised", "accent")
        self.chip_skipped = StatChip("skipped", "warning")
        self.chip_untriaged = StatChip("untriaged", "neutral")
        for chip in (
            self.chip_delete,
            self.chip_categorised,
            self.chip_skipped,
            self.chip_untriaged,
        ):
            header.addWidget(chip)
        header.addStretch()
        self.updating = QLabel()
        self.updating.setProperty("role", "faint")
        header.addWidget(self.updating)
        layout.addLayout(header)
        # Kept for tests and callers that read the totals as one line of text.
        self.totals = QLabel()
        self.totals.hide()

        filters = QHBoxLayout()
        filters.setSpacing(8)
        self.filter_box = QComboBox()
        self.filter_box.setMinimumWidth(190)
        for filter_id, (label, _) in FILTERS.items():
            self.filter_box.addItem(label, filter_id)
        self.path_filter = QLineEdit()
        self.path_filter.setPlaceholderText("Filter by path…")
        self.path_filter.addAction(
            icon("search", t.text_faint, 15), QLineEdit.ActionPosition.LeadingPosition
        )
        self.category_filter = QLineEdit()
        self.category_filter.setPlaceholderText("Category starts with… (e.g. Videos, High School)")
        self.category_filter.addAction(
            icon("tag", t.text_faint, 15), QLineEdit.ActionPosition.LeadingPosition
        )
        filters.addWidget(self.filter_box)
        filters.addWidget(self.path_filter, 1)
        filters.addWidget(self.category_filter, 1)
        self.filter_box.currentIndexChanged.connect(self.reload)
        self.path_filter.returnPressed.connect(self.reload)
        self.category_filter.returnPressed.connect(self.reload)
        layout.addLayout(filters)

        self.model = ReviewModel(session)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(32)
        header_view = self.table.horizontalHeader()
        header_view.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in ((1, 120), (2, 150), (3, 200), (4, 90), (5, 140)):
            header_view.resizeSection(column, width)
        layout.addWidget(self.table, 1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.count_label = QLabel()
        self.count_label.setProperty("role", "muted")
        footer.addWidget(self.count_label)
        footer.addSpacing(12)
        self.edit_buttons: list[QPushButton] = []
        for text, action, icon_name, color in (
            ("Keep", "keep", "check", t.success),
            ("Delete", "delete", "trash-2", t.danger),
            ("Skip", "skip", "skip-forward", t.warning),
            ("Clear", None, "x", t.text_muted),
        ):
            button = QPushButton(text)
            button.setIcon(icon(icon_name, color, 15))
            button.setToolTip("Apply to the selected rows")
            button.clicked.connect(lambda _=False, a=action: self.apply(a))
            footer.addWidget(button)
            self.edit_buttons.append(button)
        self.keep_one_button = QPushButton("Keep One, Delete the Others…")
        self.keep_one_button.setIcon(icon("copy", t.text_muted, 15))
        self.keep_one_button.setToolTip(
            "For each group of identical files among the selected rows (or all of them), keep "
            "one copy and mark the rest for deletion"
        )
        self.keep_one_button.clicked.connect(self.keep_one_per_group)
        footer.addWidget(self.keep_one_button)
        self.edit_buttons.append(self.keep_one_button)
        footer.addStretch()
        commit = QPushButton("Commit…")
        self.commit_button = commit
        commit.setProperty("variant", "primary")
        commit.setIcon(icon("git-commit-horizontal", t.accent_text, 16))
        commit.clicked.connect(self.commit_requested.emit)
        footer.addWidget(commit)
        layout.addLayout(footer)
        self.setCentralWidget(central)

        self.reload()

    def keep_one_per_group(self) -> None:
        """Keep one copy per duplicate group (chosen by a rule) and delete the others."""
        index = self.session.duplicates
        paths = self.selected_paths() or [r[0] for r in self.model.rows]
        digests = list(dict.fromkeys(d for p in paths if (d := index.digest(p)) is not None))
        if not digests:
            QMessageBox.information(
                self, "No duplicates selected", "Select rows from the Duplicates filter first."
            )
            return
        choice = QInputDialog.getItem(
            self,
            "Keep one, delete the others",
            f"{len(digests):,} group(s) of identical files. Which copy should be kept?",
            list(KEEP_RULES.values()),
            0,
            False,
        )
        if not choice[1]:
            return
        how = next(k for k, v in KEEP_RULES.items() if v == choice[0])
        keep, delete = [], []
        for digest in digests:
            group = index.groups[digest]
            mtimes = {p: self.model.node(p)[1] for p in group}
            kept = pick_copy(group, how, mtimes)
            keep.append(kept)
            delete += [p for p in group if p != kept]
        answer = QMessageBox.question(
            self,
            "Keep one, delete the others?",
            f"Keep {len(keep):,} file(s) and mark {len(delete):,} identical copies for deletion?",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.session.review_set(keep, "keep")
        self.session.review_set(delete, "delete")
        self.reload()
        self.changed.emit()

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
            self.updating.setText("Counting…")
            self.totals.setText("Counting…")
            return
        self.updating.setText("Updating…" if self.session.tracker.stale else "")
        files = summary.files_delete
        plural = "" if files == 1 else "s"
        size = human_bytes(summary.bytes_delete)
        self.chip_delete.set_value(f"{human_count(files)} file{plural} · {size}")
        self.chip_categorised.set_value(human_count(summary.files_categorised))
        self.chip_skipped.set_value(human_count(summary.files_skip))
        self.chip_untriaged.set_value(human_count(summary.files_untriaged))
        self.totals.setText(
            f"{human_count(files)} files to delete ({human_bytes(summary.bytes_delete)}) · "
            f"{human_count(summary.files_categorised)} categorised · "
            f"{human_count(summary.files_skip)} skipped · "
            f"{human_count(summary.files_untriaged)} untriaged"
        )

    def set_read_only(self, read_only: bool) -> None:
        """Another device has the project checked out: look, don't touch."""
        note = "Read-only: another device has this project open"
        for button in self.edit_buttons:
            if button.property("tip") is None:
                button.setProperty("tip", button.toolTip())  # restored when editable again
            button.setEnabled(not read_only)
            button.setToolTip(note if read_only else button.property("tip"))
        self.commit_button.setEnabled(not read_only)
        self.commit_button.setToolTip(note if read_only else "")

    def selected_paths(self) -> list[str]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.model.rows[r][0] for r in rows]

    def apply(self, action: str | None) -> None:
        paths = self.selected_paths()
        if not paths or not self.commit_button.isEnabled():
            return
        try:
            self.session.review_set(paths, action)
        except ValueError as exc:
            QMessageBox.information(self, "Can't apply to folders", str(exc))
            return
        self.reload()
        self.changed.emit()
