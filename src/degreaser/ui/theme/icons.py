"""Lucide icons (ISC licence), recoloured to the current theme at render time."""

from functools import cache
from importlib import resources

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


@cache
def _svg(name: str) -> str:
    return (resources.files("degreaser.ui.assets") / "icons" / f"{name}.svg").read_text("utf-8")


@cache
def pixmap(name: str, color: str, size: int = 16, stroke: float = 2.0) -> QPixmap:
    """Render icon `name` in `color` at `size` logical pixels (crisp on high-DPI screens)."""
    svg = _svg(name).replace("currentColor", QColor(color).name())
    if stroke != 2.0:
        svg = svg.replace('stroke-width="2"', f'stroke-width="{stroke}"')
    ratio = (
        QGuiApplication.primaryScreen().devicePixelRatio()
        if QGuiApplication.primaryScreen()
        else 1.0
    )
    pm = QPixmap(round(size * ratio), round(size * ratio))
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    QSvgRenderer(QByteArray(svg.encode())).render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pm


def icon(name: str, color: str, size: int = 16, disabled_color: str | None = None) -> QIcon:
    result = QIcon(pixmap(name, color, size))
    if disabled_color:
        result.addPixmap(pixmap(name, disabled_color, size), QIcon.Mode.Disabled)
    return result
