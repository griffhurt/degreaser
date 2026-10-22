"""Clickable key-hint buttons for the triage actions (docs/product-spec.md §4.3)."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

from degreaser.ui.keymap import ACTIONS, Keymap

# Action bar order; undo/redo live in the Edit menu instead.
BAR_ACTIONS = [
    "keep",
    "delete",
    "delete_folder",
    "tag",
    "tag_folder",
    "skip",
    "open",
    "expand_preview",
]


class ActionBar(QWidget):
    triggered = Signal(str)  # action id

    def __init__(self, keymap: Keymap, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        labels = {a.id: a.label for a in ACTIONS}
        self.buttons: dict[str, QPushButton] = {}
        for action in BAR_ACTIONS:
            button = QPushButton()
            # Keyboard handling belongs to the window's shortcuts, not focus on these buttons.
            button.setFocusPolicy(button.focusPolicy().NoFocus)
            button.clicked.connect(lambda _=False, a=action: self.triggered.emit(a))
            self.buttons[action] = button
            layout.addWidget(button)
        layout.addStretch()
        self._labels = labels
        self.set_keymap(keymap)

    def set_keymap(self, keymap: Keymap) -> None:
        for action, button in self.buttons.items():
            button.setText(f"{self._labels[action]}  [{keymap.label(action)}]")

    def set_enabled_actions(self, enabled: set[str]) -> None:
        for action, button in self.buttons.items():
            button.setEnabled(action in enabled)
