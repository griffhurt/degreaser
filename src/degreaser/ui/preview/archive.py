"""Archive listing preview (zip, 7z, RAR, tar, compressed files; core/preview/archives.py)."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHeaderView,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from degreaser.core.preview.archives import ArchiveListing
from degreaser.core.preview.metadata import format_size

MAX_ROWS = 5_000


def _short_size(n: int) -> str:
    return format_size(n).split(" (")[0]


def _summary(listing: ArchiveListing, shown: int) -> str:
    at_least = "at least " if listing.stopped else ""
    parts = [listing.format, f"{at_least}{listing.count:,} entries"]
    if all(e.size is not None or e.is_dir for e in listing.entries):
        parts.append(f"{at_least}{_short_size(listing.total_uncompressed)} uncompressed")
    if shown < listing.count:
        parts.append(f"first {shown:,} shown")
    if listing.stopped:
        parts.append(f"listing stopped: {listing.stopped}")
    return " · ".join(p for p in parts if p)


class ArchiveView(QWidget):
    def __init__(
        self, listing: ArchiveListing, note: str = "", parent: QWidget | None = None
    ) -> None:
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
                    "" if entry.is_dir or entry.size is None else _short_size(entry.size),
                    entry.modified.strftime("%Y-%m-%d %H:%M") if entry.modified else "",
                ]
            )
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            items.append(item)
        self.table.addTopLevelItems(items)
        layout.addWidget(self.table, 1)

        self.status = QLabel(_summary(listing, len(items)))
        self.status.setContentsMargins(6, 2, 6, 4)
        layout.addWidget(self.status)

    def teardown(self) -> None:
        pass
