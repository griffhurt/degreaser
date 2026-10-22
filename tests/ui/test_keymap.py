from PySide6.QtGui import QKeySequence
from pytestqt.qtbot import QtBot

from degreaser.core.settings import DEFAULT_KEYMAP
from degreaser.ui.keymap import Keymap


def test_defaults_round_trip(qtbot: QtBot) -> None:
    assert Keymap(DEFAULT_KEYMAP).to_settings() == DEFAULT_KEYMAP


def test_rebinding_and_conflicts(qtbot: QtBot) -> None:
    km = Keymap(DEFAULT_KEYMAP).with_binding("keep", QKeySequence("D"))
    assert km.to_settings()["keep"] == "D"
    assert km.conflicts() == {"D": ["keep", "delete"]}
