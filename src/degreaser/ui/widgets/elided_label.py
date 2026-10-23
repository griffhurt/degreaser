"""A QLabel that shrinks: too little room shows "…" instead of widening the window."""

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPainter, QPaintEvent
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget


class ElidedLabel(QLabel):
    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight,
    ) -> None:
        super().__init__(text, parent)
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        ellipsis = self.fontMetrics().horizontalAdvance("…") + 4
        return QSize(min(hint.width(), ellipsis), hint.height())

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setToolTip(text if text else "")

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.setFont(self.font())
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), self._mode, rect.width())
        painter.drawText(rect, int(self.alignment()) | Qt.AlignmentFlag.AlignVCenter, text)
