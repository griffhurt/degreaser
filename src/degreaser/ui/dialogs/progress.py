"""Modal progress dialog for rescans, commit planning and similar long jobs."""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class ProgressDialog(QDialog):
    """Shown only if the job is still running after `show_after_ms` (no flash for quick jobs)."""

    cancel_requested = Signal()

    def __init__(
        self,
        title: str,
        message: str,
        cancel_text: str = "Cancel",
        parent: QWidget | None = None,
        show_after_ms: int = 500,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        self.message = QLabel(message)
        self.message.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.detail = QLabel()
        self.detail.setEnabled(False)
        layout.addWidget(self.message)
        layout.addWidget(self.bar)
        layout.addWidget(self.detail)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.cancel_button = QPushButton(cancel_text)
        self.cancel_button.clicked.connect(self._cancel)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)
        self._finished = False
        self._show_timer = QTimer(self, singleShot=True, interval=show_after_ms)
        self._show_timer.timeout.connect(self._maybe_show)

    def start(self) -> None:
        self._show_timer.start()

    def _maybe_show(self) -> None:
        if not self._finished:
            self.show()

    def set_progress(self, done: int, total: int | None, detail: str = "") -> None:
        if total:
            self.bar.setRange(0, total)
            self.bar.setValue(min(done, total))
        else:
            self.bar.setRange(0, 0)
        self.detail.setText(detail)

    def finish(self) -> None:
        self._finished = True
        self._show_timer.stop()
        self.accept()

    def _cancel(self) -> None:
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Stopping…")
        self.cancel_requested.emit()

    def reject(self) -> None:
        # Esc / window close behave like Cancel; the owner calls finish() when the job stops.
        if not self._finished:
            self._cancel()
        else:
            super().reject()
