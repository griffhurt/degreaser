"""The state banner above the preview (docs/product-spec.md §4.2)."""

from PySide6.QtCore import QEasingCurve, QPropertyAnimation
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QWidget

from degreaser.core.triage.session import Banner as BannerKind
from degreaser.ui.theme import pixmap, theme
from degreaser.ui.theme.tokens import Tokens

# kind: (text, icon, tone)
_KINDS: dict[BannerKind, tuple[str, str, str]] = {
    BannerKind.NEW: ("Newly added file", "sparkles", "info"),
    BannerKind.CHANGED: ("Changed since you triaged it", "refresh-cw", "warning"),
    BannerKind.LOADED_SKIP: ("Skipped earlier — now loaded", "history", "purple"),
    BannerKind.PREVIOUSLY_SKIPPED: ("Previously skipped", "skip-forward", "warning"),
}


def _tone(t: Tokens, tone: str) -> tuple[str, str]:
    return {
        "info": (t.info, t.info_tint),
        "warning": (t.warning, t.warning_tint),
        "purple": (t.purple, t.purple_tint),
    }[tone]


class Banner(QFrame):
    HEIGHT = 46

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 14, 0)
        layout.setSpacing(10)
        self.icon = QLabel()
        self.title = QLabel()
        self.detail = QLabel()
        layout.addWidget(self.icon)
        layout.addWidget(self.title)
        layout.addWidget(self.detail, 1)
        self.kind: BannerKind | None = None
        self._slide = QPropertyAnimation(self, b"maximumHeight", self)
        self._slide.setDuration(160)
        self._slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        theme().changed.connect(lambda _t: self._restyle())
        self.setMaximumHeight(0)
        self.hide()

    def text(self) -> str:
        detail = self.detail.text()
        return f"{self.title.text()}  ·  {detail}" if detail else self.title.text()

    def show_kind(self, kind: BannerKind | None, detail: str = "") -> None:
        """Show a banner (detail is appended, e.g. the previous decision); None/NONE hides it."""
        self.kind = kind
        if kind is None or kind is BannerKind.NONE:
            self._slide.stop()
            self.setMaximumHeight(0)
            self.hide()
            return
        text, _icon, _tone_name = _KINDS[kind]
        self.title.setText(text.upper())
        self.detail.setText(detail)
        self._restyle()
        was_hidden = self.isHidden()
        self.show()
        if was_hidden:
            self._slide.stop()
            self._slide.setStartValue(0)
            self._slide.setEndValue(self.HEIGHT)
            self._slide.start()
        else:
            self.setMaximumHeight(self.HEIGHT)
        self.setMinimumHeight(0)

    def _restyle(self) -> None:
        if self.kind is None or self.kind is BannerKind.NONE:
            return
        t = theme().tokens
        _text, icon_name, tone = _KINDS[self.kind]
        fg, tint = _tone(t, tone)
        self.setStyleSheet(
            f"Banner {{ background: {tint}; border: 1px solid {fg};"
            f" border-radius: {t.radius_lg}px; }}"
        )
        self.icon.setPixmap(pixmap(icon_name, fg, 18))
        self.title.setStyleSheet(f"color: {fg}; font-weight: 700; letter-spacing: 0.6px;")
        self.detail.setStyleSheet(f"color: {t.text_muted};")
