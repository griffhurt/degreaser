from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from degreaser.ui.theme import DARK, LIGHT, icon, pixmap
from degreaser.ui.theme.manager import ThemeManager, load_fonts
from degreaser.ui.theme.qss import stylesheet


def test_stylesheet_uses_tokens_for_both_themes() -> None:
    light, dark = stylesheet(LIGHT, "/x/arrow.svg"), stylesheet(DARK, "/x/arrow.svg")
    assert LIGHT.accent in light and DARK.accent in dark
    assert LIGHT.bg not in dark
    assert 'url("/x/arrow.svg")' in light
    assert "{" in light and "{{" not in light  # f-string braces fully rendered


def test_fonts_and_icons_load(qtbot: QtBot) -> None:
    assert load_fonts()
    assert "Inter" in QFontDatabase.families()
    pm = pixmap("trash-2", "#ff0000", 20)
    assert not pm.isNull() and pm.deviceIndependentSize().width() == 20
    assert not icon("check", "#00ff00").isNull()


def test_manager_applies_and_switches(qtbot: QtBot) -> None:
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    manager = ThemeManager()
    seen = []
    manager.changed.connect(seen.append)
    manager.install(app, dark=True)
    assert manager.tokens is DARK and DARK.bg in app.styleSheet()
    manager._override = False
    manager.apply(app)
    assert manager.tokens is LIGHT and seen[-1] is LIGHT
    app.setStyleSheet("")  # don't leak the theme into other tests
