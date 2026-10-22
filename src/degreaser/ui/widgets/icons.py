"""Small painted state icons used in the path bar, review table and status bar."""

from functools import cache

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

from degreaser.core.model import Effective, Origin

STATE_COLORS: dict[Effective, str] = {
    Effective.UNTRIAGED: "#9aa0a6",
    Effective.KEEP: "#2e9d55",
    Effective.DELETE: "#d93025",
    Effective.SKIP: "#e8a10a",
    Effective.DEFERRED: "#8e63ce",
}
CATEGORISED_COLOR = "#1a73e8"

STATE_LABELS: dict[Effective, str] = {
    Effective.UNTRIAGED: "Untriaged",
    Effective.KEEP: "Keep",
    Effective.DELETE: "Delete",
    Effective.SKIP: "Skipped",
    Effective.DEFERRED: "Loading (deferred)",
}


@cache
def state_icon(
    state: Effective, origin: Origin = Origin.DIRECT, categorised: bool = False
) -> QIcon:
    """A filled dot for the user's own or folder decisions; a ring for rule-made ones."""
    size = 14
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    color = QColor(CATEGORISED_COLOR if categorised else STATE_COLORS[state])
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    rect = QRectF(2, 2, size - 4, size - 4)
    if origin in (Origin.RULE, Origin.COMPANION) or state == Effective.UNTRIAGED:
        painter.setPen(QPen(color, 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(rect.adjusted(1, 1, -1, -1))
    else:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(rect)
    painter.end()
    return QIcon(pixmap)
