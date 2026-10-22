"""Key-cap buttons for the triage actions (docs/product-spec.md §4.3)."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QWidget

from degreaser.ui.keymap import ACTIONS, Keymap
from degreaser.ui.widgets.key_button import KeyButton

# (action id, icon, tone); None marks a gap between groups. Undo/redo live in the Edit menu.
LAYOUT: list[tuple[str, str, str] | None] = [
    ("keep", "check", "success"),
    ("keep_folder", "folder-check", "success"),
    None,
    ("delete", "trash-2", "danger"),
    ("delete_folder", "folder-x", "danger"),
    None,
    ("tag", "tag", "accent"),
    ("tag_folder", "tags", "accent"),
    None,
    ("skip", "skip-forward", "warning"),
]
TRAILING = [("open", "external-link", "neutral"), ("expand_preview", "maximize-2", "neutral")]

BAR_ACTIONS = [entry[0] for entry in [*LAYOUT, *TRAILING] if entry is not None]


class ActionBar(QFrame):
    triggered = Signal(str)  # action id

    def __init__(self, keymap: Keymap, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "actionbar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(6)
        labels = {a.id: a.label for a in ACTIONS}
        labels["expand_preview"] = "Expand"
        labels["open"] = "Open"
        self.buttons: dict[str, KeyButton] = {}

        def add(action: str, icon_name: str, tone: str) -> None:
            button = KeyButton(labels[action], icon_name, tone)
            button.clicked.connect(lambda _=False, a=action: self.triggered.emit(a))
            self.buttons[action] = button
            layout.addWidget(button)

        for entry in LAYOUT:
            if entry is None:
                layout.addSpacing(10)
            else:
                add(*entry)
        layout.addStretch()
        for entry in TRAILING:
            add(*entry)
        self.set_keymap(keymap)

    def set_keymap(self, keymap: Keymap) -> None:
        for action, button in self.buttons.items():
            button.set_key(keymap.label(action))

    def set_enabled_actions(self, enabled: set[str]) -> None:
        for action, button in self.buttons.items():
            button.setEnabled(action in enabled)
