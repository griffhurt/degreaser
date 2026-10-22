"""Zip listing preview (read from the central directory only)."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHeaderView,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from degreaser.core.preview.loaders import ZipListing
from degreaser.core.preview.metadata import format_size

MAX_ROWS = 5_000


class ArchiveView(QWidget):
    def __init__(self, listing: ZipListing, note: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        # e.g. why a document preview fell back to the listing
        self.note = QLabel(note)
        self.note.setProperty("role", "muted")
        self.note.setWordWrap(True)
        self.note.setContentsMargins(8, 6, 8, 6)
        self.note.setVisible(bool(note))
        layout.addWidget(self.note)
        self.table = QTreeWidget()
        self.table.setRootIsDecorated(False)
        self.table.setUniformRowHeights(True)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setHeaderLabels(["Name", "Size", "Modified"])
        header = self.table.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(False)
        items = []
        for entry in listing.entries[:MAX_ROWS]:
            item = QTreeWidgetItem(
                [
                    entry.name + (" 🔒" if entry.encrypted else ""),
                    "" if entry.is_dir else format_size(entry.size).split(" (")[0],
                    entry.modified.strftime("%Y-%m-%d %H:%M") if entry.modified else "",
                ]
            )
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            items.append(item)
        self.table.addTopLevelItems(items)
        layout.addWidget(self.table, 1)

        hidden = listing.count - len(items)
        summary = f"{listing.count:,} entries · {format_size(listing.total_uncompressed).split(' (')[0]} uncompressed"  # noqa: E501
        if hidden > 0:
            summary += f" · first {len(items):,} shown"
        self.status = QLabel(summary)
        self.status.setContentsMargins(6, 2, 6, 4)
        layout.addWidget(self.status)

    def teardown(self) -> None:
        pass
