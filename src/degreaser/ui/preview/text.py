"""Plain-text preview with incremental 'Load more'."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontDatabase, QTextCursor
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from degreaser.core.preview.highlight import Span
from degreaser.core.preview.metadata import format_size
from degreaser.ui.preview.highlight import SpanHighlighter


class TextView(QWidget):
    """Read-only text. `loadMoreRequested` asks the pane to fetch the next chunk."""

    loadMoreRequested = Signal()

    def __init__(self, monospace: bool, wrap: bool = True, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.edit = QPlainTextEdit()
        self.edit.setReadOnly(True)
        self.edit.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.edit.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth if wrap else QPlainTextEdit.LineWrapMode.NoWrap
        )
        if monospace:
            self.edit.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
            # The app stylesheet sets Inter on everything; this property opts back into mono.
            self.edit.setProperty("mono", True)
        layout.addWidget(self.edit, 1)

        footer = QHBoxLayout()
        footer.setContentsMargins(6, 2, 6, 4)
        self.status = QLabel()
        footer.addWidget(self.status, 1)
        self.more = QPushButton("Load more (Space)")
        self.more.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.more.clicked.connect(self.loadMoreRequested)
        footer.addWidget(self.more)
        layout.addLayout(footer)
        self.highlighter: SpanHighlighter | None = None

    def set_spans(self, spans: list[Span]) -> None:
        """Syntax colours for the text so far (core/preview/highlight.py)."""
        if self.highlighter is None:
            self.highlighter = SpanHighlighter(self.edit.document())
        self.highlighter.set_spans(spans)

    def append(self, text: str) -> None:
        cursor = self.edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)  # a cursor copy, so the view doesn't jump to the end

    def set_progress(self, shown_bytes: int, total_bytes: int, *, can_load_more: bool) -> None:
        if shown_bytes >= total_bytes:
            self.status.setText(f"Whole file ({format_size(total_bytes)})")
        else:
            self.status.setText(
                f"Showing first {format_size(shown_bytes)} of {format_size(total_bytes)}"
            )
        self.more.setVisible(can_load_more)

    def set_busy(self, busy: bool) -> None:
        self.more.setEnabled(not busy)
        self.more.setText("Loading…" if busy else "Load more (Space)")

    def text(self) -> str:
        return self.edit.toPlainText()

    def teardown(self) -> None:
        pass
