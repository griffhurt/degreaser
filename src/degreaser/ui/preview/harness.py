"""Dev harness for eyeballing previews:

    uv run python -m degreaser.ui.preview.harness <folder>

↑/↓ move through files, Space expands, O opens in the default app.
"""

import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QListWidget, QMainWindow, QSplitter

from degreaser.core.sources.local import LocalSource
from degreaser.ui.preview.pane import PreviewPane
from degreaser.ui.widgets.metadata_table import MetadataTable

MAX_FILES = 5000


def list_files(root: Path, limit: int = MAX_FILES) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        for name in sorted(filenames):
            found.append(name if rel_dir == "." else f"{rel_dir}/{name}")
            if len(found) >= limit:
                return found
    return found


class HarnessWindow(QMainWindow):
    def __init__(self, root: Path) -> None:
        super().__init__()
        self.setWindowTitle(f"Preview harness — {root}")
        self.source = LocalSource(root)
        self.files = QListWidget()
        self.files.addItems(list_files(root))
        self.pane = PreviewPane()
        self.table = MetadataTable()
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.files)
        splitter.addWidget(self.pane)
        splitter.addWidget(self.table)
        splitter.setSizes([250, 700, 330])
        self.setCentralWidget(splitter)
        self.resize(1300, 800)

        self.files.currentTextChanged.connect(self.show_rel)
        self.pane.metadataChanged.connect(lambda _rel, rows: self.table.set_rows(rows))
        self.pane.openRequested.connect(self.open_rel)
        self.pane.failed.connect(lambda rel, msg: self.statusBar().showMessage(f"{rel}: {msg}"))
        self.pane.loaded.connect(lambda rel: self.statusBar().showMessage(f"Loaded {rel}", 3000))
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, self.pane.expand)
        QShortcut(QKeySequence(Qt.Key.Key_O), self, lambda: self.open_rel(self.pane.rel or ""))
        if self.files.count():
            self.files.setCurrentRow(0)

    def show_rel(self, rel: str) -> None:
        if rel:
            self.pane.show_file(self.source, rel, self.source.stat(rel))

    def open_rel(self, rel: str) -> None:
        path = self.source.local_path(rel) if rel else None
        if path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self.pane.shutdown()
        super().closeEvent(event)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")
    os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.*.info=false")
    app = QApplication(argv[:1])
    window = HarnessWindow(Path(argv[1]).resolve())
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
