"""Non-content preview states: loading, messages, confirmation prompts, unsupported files."""

from collections.abc import Callable, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget


class MessageView(QWidget):
    """A centred title, explanation and optional buttons."""

    def __init__(
        self,
        title: str,
        text: str = "",
        buttons: Sequence[tuple[str, Callable[[], None]]] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addStretch(1)
        self.title = QLabel(title)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # The app stylesheet sets fonts in pixels (pointSize is -1), and it would override
        # setFont() anyway, so size the title through the stylesheet.
        self.title.setStyleSheet("font-size: 17px; font-weight: 600;")
        layout.addWidget(self.title)
        self.text = QLabel(text)
        self.text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text.setWordWrap(True)
        self.text.setVisible(bool(text))
        layout.addWidget(self.text)
        self.buttons: list[QPushButton] = []
        if buttons:
            row = QHBoxLayout()
            row.addStretch(1)
            for label, callback in buttons:
                button = QPushButton(label)
                button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                button.clicked.connect(callback)
                row.addWidget(button)
                self.buttons.append(button)
            row.addStretch(1)
            layout.addLayout(row)
        layout.addStretch(1)

    def teardown(self) -> None:
        pass


class LoadingView(MessageView):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Loading…", parent=parent)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)  # busy until we know the size
        self.bar.setMaximumWidth(320)
        self.layout().insertWidget(3, self.bar, alignment=Qt.AlignmentFlag.AlignCenter)  # type: ignore[union-attr]

    def set_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(1000 * done / total))
