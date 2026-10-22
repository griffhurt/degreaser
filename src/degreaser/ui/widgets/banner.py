"""The large state banner above the preview (docs/product-spec.md §4.2)."""

from enum import StrEnum

from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget


class BannerKind(StrEnum):
    NEW = "new"
    CHANGED = "changed"
    LOADED_SKIP = "loaded_skip"
    PREVIOUSLY_SKIPPED = "previously_skipped"


_STYLES: dict[BannerKind, tuple[str, str, str]] = {
    # kind: (text, background, foreground)
    BannerKind.NEW: ("★  NEWLY ADDED FILE", "#1a73e8", "white"),
    BannerKind.CHANGED: ("⟳  CHANGED SINCE YOU TRIAGED IT", "#e8710a", "white"),
    BannerKind.LOADED_SKIP: ("↩  SKIPPED EARLIER — NOW LOADED", "#8e63ce", "white"),
    BannerKind.PREVIOUSLY_SKIPPED: ("⏭  PREVIOUSLY SKIPPED", "#e8a10a", "black"),
}


class Banner(QLabel):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setMargin(10)
        self.kind: BannerKind | None = None
        self.hide()

    def show_kind(self, kind: BannerKind | None, detail: str = "") -> None:
        """Show a banner (detail is appended, e.g. the previous decision), or hide for None."""
        self.kind = kind
        if kind is None:
            self.hide()
            return
        text, bg, fg = _STYLES[kind]
        self.setText(f"{text}  ·  {detail}" if detail else text)
        self.setStyleSheet(
            f"QLabel {{ background: {bg}; color: {fg}; font-size: 18px; font-weight: 700;"
            " border-radius: 6px; }"
        )
        self.show()
