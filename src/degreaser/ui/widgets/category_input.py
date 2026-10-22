"""Category input with terminal-style ghost completion (docs/product-spec.md §4.4).

A thin shell over core.categories.Completer: the widget paints the ghost suffix, routes
Tab/Shift+Tab to the completer and validates with parse_category before submitting.
"""

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from degreaser.core.categories import Completer, format_category, input_text, parse_category


class _GhostLineEdit(QLineEdit):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ghost = ""

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        if not self.ghost or self.hasSelectedText() or self.cursorPosition() != len(self.text()):
            return
        painter = QPainter(self)
        color = QColor(self.palette().text().color())
        color.setAlphaF(0.35)
        painter.setPen(color)
        cursor = self.cursorRect()
        x = cursor.x() + cursor.width() // 2 + 1
        painter.drawText(
            x,
            cursor.y(),
            self.width() - x,
            cursor.height(),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self.ghost,
        )
        painter.end()


class CategoryInput(QWidget):
    submitted = Signal(list)  # list[str]: the category's tags, possibly empty
    cancelled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._completer: Completer | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.title = QLabel()
        self.inherited = QLabel()
        self.inherited.setEnabled(False)
        self.edit = _GhostLineEdit()
        self.edit.setPlaceholderText(
            "Broad, …, specific — Tab completes, Enter applies, Esc cancels"
        )
        row.addWidget(self.title)
        row.addWidget(self.inherited)
        row.addWidget(self.edit, 1)
        layout.addLayout(row)
        self.issues = QLabel()
        self.issues.setStyleSheet("color: #d93025;")
        self.issues.hide()
        layout.addWidget(self.issues)
        self.edit.textEdited.connect(self._on_edited)
        self.edit.installEventFilter(self)

    def begin(
        self,
        completer: Completer,
        title: str,
        initial: list[str] | None = None,
        inherited: list[str] | None = None,
    ) -> None:
        """Open the input. `initial` pre-fills the field; `inherited` is shown read-only."""
        self._completer = completer
        completer.reset()
        self.title.setText(f"<b>{title}</b>")
        self.inherited.setText(
            f"inherited: {format_category(tuple(inherited))}  " if inherited else ""
        )
        self.inherited.setVisible(bool(inherited))
        text = input_text(tuple(initial)) if initial else ""
        self.edit.setText(text + (", " if text else ""))
        self._refresh()
        self.show()
        self.edit.setFocus()
        self.edit.end(False)

    def _on_edited(self, _text: str) -> None:
        if self._completer:
            self._completer.reset()
        self._refresh()

    def _refresh(self) -> None:
        text = self.edit.text()
        self.edit.ghost = self._completer.ghost(text) if self._completer else ""
        parsed = parse_category(text)
        self.issues.setText("; ".join(i.message for i in parsed.issues))
        self.issues.setVisible(bool(parsed.issues))
        self.edit.update()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # QLineEdit would move focus on Tab; take it before the focus chain does.
        if watched is self.edit and event.type() == QEvent.Type.KeyPress:
            assert isinstance(event, QKeyEvent)
            key = event.key()
            if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                self._tab(backwards=key == Qt.Key.Key_Backtab)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._submit()
                return True
            if key == Qt.Key.Key_Escape:
                self.hide()
                self.cancelled.emit()
                return True
        return super().eventFilter(watched, event)

    def _tab(self, backwards: bool) -> None:
        if self._completer is None:
            return
        produced = self._completer.tab(self.edit.text(), backwards=backwards)
        if produced is not None:
            self.edit.setText(produced)
            self.edit.end(False)
        self._refresh()

    def _submit(self) -> None:
        parsed = parse_category(self.edit.text())
        if parsed.issues:
            self._refresh()
            return
        self.hide()
        self.submitted.emit(list(parsed.tags))
