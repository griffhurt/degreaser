"""Brief action feedback that floats above the action bar and fades away."""

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer
from PySide6.QtWidgets import QGraphicsOpacityEffect, QHBoxLayout, QLabel, QWidget

from degreaser.ui.theme import theme
from degreaser.ui.theme.tokens import Tokens


class Toast(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(10)
        self.message = QLabel()
        self.hint = QLabel()
        layout.addWidget(self.message)
        layout.addWidget(self.hint)
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)
        self._fade = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._hide_timer = QTimer(self, singleShot=True)
        self._hide_timer.timeout.connect(lambda: self._animate(0.0, 260))
        self.bottom_margin = 16
        theme().changed.connect(self._restyle)
        self._restyle(theme().tokens)
        self.hide()

    def _restyle(self, t: Tokens) -> None:
        # Inverted colours so it reads as an overlay in both themes.
        self.setStyleSheet(f"Toast {{ background: {t.text}; border-radius: 10px; }}")
        self.message.setStyleSheet(f"color: {t.bg}; font-weight: 600;")
        self.hint.setStyleSheet(f"color: {t.text_faint};")

    def show_message(self, message: str, hint: str = "", ms: int = 1800) -> None:
        self.message.setText(message)
        self.hint.setText(hint)
        self.hint.setVisible(bool(hint))
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()
        self._animate(1.0, 120)
        self._hide_timer.start(ms)

    def reposition(self) -> None:
        parent = self.parentWidget()
        if parent is not None:
            self.move(
                (parent.width() - self.width()) // 2,
                parent.height() - self.height() - self.bottom_margin,
            )

    def _animate(self, target: float, ms: int) -> None:
        self._fade.stop()
        self._fade.setDuration(ms)
        self._fade.setStartValue(self._effect.opacity())
        self._fade.setEndValue(target)
        if target == 0.0:
            self._fade.finished.connect(self._hide_when_faded)
        self._fade.start()

    def _hide_when_faded(self) -> None:
        self._fade.finished.disconnect(self._hide_when_faded)
        if self._effect.opacity() == 0.0:
            self.hide()
