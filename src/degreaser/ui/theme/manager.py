"""Applies the theme to the application and follows the OS light/dark setting."""

from importlib import resources

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

from degreaser.core import paths
from degreaser.ui.theme.qss import stylesheet
from degreaser.ui.theme.tokens import DARK, LIGHT, Tokens

FONT_FILES = ["Inter-Regular.ttf", "Inter-Medium.ttf", "Inter-SemiBold.ttf", "Inter-Bold.ttf"]


def load_fonts() -> bool:
    fonts = resources.files("degreaser.ui.assets") / "fonts"
    ok = True
    for name in FONT_FILES:
        data = (fonts / name).read_bytes()
        ok &= QFontDatabase.addApplicationFontFromData(data) != -1
    return ok


# Preferred fixed-width fonts per platform, best first. Qt's FixedFont can come back as the
# generic name "monospace", which doesn't resolve everywhere (it fell back to a
# proportional font on macOS), so pick a concrete installed family.
MONO_CANDIDATES = [
    "SF Mono",
    "Menlo",
    "Cascadia Mono",
    "Consolas",
    "JetBrains Mono",
    "DejaVu Sans Mono",
    "Ubuntu Mono",
    "Liberation Mono",
    "Noto Sans Mono",
    "Courier New",
]


def mono_family() -> str:
    installed = set(QFontDatabase.families())
    for family in MONO_CANDIDATES:
        if family in installed:
            return family
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()


def palette(t: Tokens) -> QPalette:
    """Palette for the parts Fusion draws itself (checkboxes, radio buttons, focus, links)."""
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: t.bg,
        QPalette.ColorRole.WindowText: t.text,
        QPalette.ColorRole.Base: t.surface,
        QPalette.ColorRole.AlternateBase: t.surface_alt,
        QPalette.ColorRole.Text: t.text,
        QPalette.ColorRole.PlaceholderText: t.text_faint,
        QPalette.ColorRole.Button: t.surface,
        QPalette.ColorRole.ButtonText: t.text,
        QPalette.ColorRole.Highlight: t.accent,
        QPalette.ColorRole.HighlightedText: t.accent_text,
        QPalette.ColorRole.ToolTipBase: t.text,
        QPalette.ColorRole.ToolTipText: t.bg,
        QPalette.ColorRole.Link: t.accent,
        QPalette.ColorRole.Mid: t.border_strong,
        QPalette.ColorRole.Midlight: t.border,
        QPalette.ColorRole.Dark: t.border_strong,
        QPalette.ColorRole.Light: t.surface,
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    for role in (
        QPalette.ColorRole.Text,
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.ButtonText,
    ):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(t.text_faint))
    return p


class ThemeManager(QObject):
    """Owns the current tokens. Custom-painted widgets listen to `changed` to repaint icons."""

    changed = Signal(object)  # Tokens

    def __init__(self) -> None:
        super().__init__()
        self.tokens: Tokens = LIGHT
        self._override: bool | None = None  # None = follow the OS

    def install(self, app: QApplication, dark: bool | None = None) -> None:
        load_fonts()
        app.setStyle(QStyleFactory.create("Fusion"))
        font = QFont("Inter")
        font.setPixelSize(LIGHT.font_size)
        font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        app.setFont(font)
        self._override = dark
        app.styleHints().colorSchemeChanged.connect(lambda _scheme: self.apply(app))
        self.apply(app)

    @staticmethod
    def _themed_asset(name: str, color: str, t: Tokens, stroke: int = 2) -> str:
        """Write a recoloured icon to app data for QSS `image: url(...)`; returns its path."""
        directory = paths.app_data_dir() / "theme"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{name}-{'dark' if t.dark else 'light'}.svg"
        svg = (resources.files("degreaser.ui.assets") / "icons" / f"{name}.svg").read_text("utf-8")
        svg = svg.replace("currentColor", color).replace(
            'stroke-width="2"', f'stroke-width="{stroke}"'
        )
        target.write_text(svg, encoding="utf-8")
        return target.as_posix()

    def system_is_dark(self) -> bool:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark

    def apply(self, app: QApplication) -> None:
        dark = self.system_is_dark() if self._override is None else self._override
        self.tokens = DARK if dark else LIGHT
        app.setPalette(palette(self.tokens))
        t = self.tokens
        app.setStyleSheet(
            stylesheet(
                t,
                arrow_path=self._themed_asset("chevron-down", t.text_muted, t),
                check_path=self._themed_asset("check", t.accent_text, t, stroke=3),
                mono_family=mono_family(),
            )
        )
        self.changed.emit(self.tokens)


_manager: ThemeManager | None = None


def theme() -> ThemeManager:
    """The app's ThemeManager (created on first use; defaults to light tokens until installed)."""
    global _manager
    if _manager is None:
        _manager = ThemeManager()
    return _manager
