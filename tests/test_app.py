from pytestqt.qtbot import QtBot

from degreaser.app import build_main_window


def test_main_window_is_project_list(qtbot: QtBot) -> None:
    window = build_main_window()
    qtbot.addWidget(window)
    window.show()

    assert window.windowTitle() == "Degreaser"
    assert window.new_button.isEnabled()
