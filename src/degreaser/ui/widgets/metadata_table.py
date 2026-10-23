"""Grouped two-column metadata table shown beside the preview."""

from collections.abc import Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QFont, QGuiApplication, QKeySequence, QPalette
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTreeWidget, QTreeWidgetItem, QWidget

from degreaser.core.preview.metadata import GROUP_ORDER, MetaRow

TRIAGE_GROUP = "Triage"


class MetadataTable(QTreeWidget):
    """`set_rows` replaces file metadata; `set_extra_rows` holds rows the triage window owns
    (effective decision, category and where it comes from), which survive `set_rows`."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setColumnCount(2)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(False)
        self.setIndentation(0)
        self.setUniformRowHeights(False)
        self.setWordWrap(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = self.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._rows: list[MetaRow] = []
        self._extra: list[MetaRow] = []

    def set_rows(self, rows: Sequence[MetaRow]) -> None:
        self._rows = list(rows)
        self._rebuild()

    def set_extra_rows(self, rows: Sequence[MetaRow]) -> None:
        # Rows keep their own group (Triage, Duplicates); ungrouped ones go under Triage.
        self._extra = [MetaRow(r.group or TRIAGE_GROUP, r.label, r.value, r.warning) for r in rows]
        self._rebuild()

    def rows(self) -> list[MetaRow]:
        return self._extra + self._rows

    def value(self, label: str, group: str | None = None) -> str | None:
        for row in self.rows():
            if row.label == label and (group is None or row.group == group):
                return row.value
        return None

    def clear_rows(self) -> None:
        self._rows = []
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear()
        groups: dict[str, list[MetaRow]] = {}
        for row in self._extra + self._rows:
            groups.setdefault(row.group, []).append(row)
        order = [g for g in GROUP_ORDER if g in groups] + [
            g for g in groups if g not in GROUP_ORDER
        ]
        bold = QFont(self.font())
        bold.setBold(True)
        dim = self.palette().brush(QPalette.ColorGroup.Active, QPalette.ColorRole.PlaceholderText)
        warn = QBrush(Qt.GlobalColor.darkYellow)
        for group in order:
            heading = QTreeWidgetItem([group, ""])
            heading.setFont(0, bold)
            heading.setFirstColumnSpanned(True)
            heading.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.addTopLevelItem(heading)
            heading.setFirstColumnSpanned(True)
            for row in groups[group]:
                item = QTreeWidgetItem([row.label, row.value])
                item.setForeground(0, dim)
                item.setToolTip(1, row.value)
                if row.warning:
                    item.setForeground(1, warn)
                    item.setText(1, "⚠ " + row.value)
                self.addTopLevelItem(item)

    def keyPressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.matches(QKeySequence.StandardKey.Copy):
            lines = [
                f"{item.text(0)}\t{item.text(1)}" if item.text(1) else item.text(0)
                for item in self.selectedItems()
            ]
            QGuiApplication.clipboard().setText("\n".join(lines))
            return
        super().keyPressEvent(event)
