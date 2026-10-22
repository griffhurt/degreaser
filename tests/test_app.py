from pytestqt.qtbot import QtBot

from degreaser import __version__
from degreaser.app import build_main_window


def test_main_window_shows_version(qtbot: QtBot) -> None:
    window = build_main_window()
    qtbot.addWidget(window)
    window.show()

    assert window.windowTitle() == "Degreaser"
    assert __version__ in window.centralWidget().text()  # type: ignore[union-attr]
