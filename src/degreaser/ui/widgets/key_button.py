"""A button that shows an icon, a label and its keyboard shortcut as a key cap."""

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QAbstractButton, QSizePolicy, QWidget

from degreaser.ui.theme import pixmap, theme
from degreaser.ui.theme.tokens import Tokens

ICON = 16
PAD_X = 10
GAP = 8
KEY_PAD_X = 6
HEIGHT = 34


def tone_colors(t: Tokens, tone: str) -> tuple[str, str]:
    """(foreground, hover tint) for a tone."""
    return {
        "success": (t.success, t.success_tint),
        "danger": (t.danger, t.danger_tint),
        "accent": (t.accent, t.accent_tint),
        "warning": (t.warning, t.warning_tint),
    }.get(tone, (t.text_muted, t.surface_alt))


class KeyButton(QAbstractButton):
    def __init__(
        self, label: str, icon_name: str, tone: str = "neutral", parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.label = label
        self.icon_name = icon_name
        self.tone = tone
        self.key = ""
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        theme().changed.connect(lambda _t: self.update())

    def text(self) -> str:  # type: ignore[override]
        return f"{self.label}  [{self.key}]"

    def set_key(self, key: str) -> None:
        self.key = key
        self.updateGeometry()
        self.update()

    def _key_font(self) -> QFont:
        font = QFont(self.font())
        font.setPixelSize(11)
        font.setWeight(QFont.Weight.DemiBold)
        return font

    def _label_font(self) -> QFont:
        font = QFont(self.font())
        font.setWeight(QFont.Weight.Medium)
        return font

    def sizeHint(self) -> QSize:
        label_w = QFontMetrics(self._label_font()).horizontalAdvance(self.label)
        key_w = QFontMetrics(self._key_font()).horizontalAdvance(self.key) + 2 * KEY_PAD_X
        width = PAD_X + ICON + GAP + label_w + (GAP + key_w if self.key else 0) + PAD_X
        return QSize(width, HEIGHT)

    def paintEvent(self, event: QPaintEvent) -> None:
        t = theme().tokens
        fg, tint = tone_colors(t, self.tone)
        enabled = self.isEnabled()
        hovered = enabled and self.underMouse()
        down = enabled and self.isDown()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        bg = QColor(tint if (hovered or down) else t.surface)
        p.setPen(QPen(QColor(fg if down else t.border), 1))
        p.setBrush(bg)
        p.drawRoundedRect(rect, t.radius, t.radius)

        x = PAD_X
        color = fg if enabled else t.text_faint
        p.drawPixmap(int(x), (self.height() - ICON) // 2, pixmap(self.icon_name, color, ICON))
        x += ICON + GAP

        p.setFont(self._label_font())
        p.setPen(QColor(t.text if enabled else t.text_faint))
        label_w = QFontMetrics(self._label_font()).horizontalAdvance(self.label)
        p.drawText(
            QRectF(x, 0, label_w + 2, self.height()), Qt.AlignmentFlag.AlignVCenter, self.label
        )
        x += label_w + GAP

        if self.key:
            p.setFont(self._key_font())
            key_w = QFontMetrics(self._key_font()).horizontalAdvance(self.key) + 2 * KEY_PAD_X
            cap = QRectF(x, (self.height() - 20) / 2, key_w, 20)
            p.setPen(QPen(QColor(t.border_strong), 1))
            p.setBrush(QColor(t.surface_alt))
            p.drawRoundedRect(cap, 5, 5)
            p.setPen(QColor(t.text_muted if enabled else t.text_faint))
            p.drawText(cap, Qt.AlignmentFlag.AlignCenter, self.key)
        p.end()
