"""Remappable triage shortcuts (docs/architecture.md §7)."""

from dataclasses import dataclass

from PySide6.QtGui import QKeySequence

from degreaser.core.settings import DEFAULT_KEYMAP


@dataclass(frozen=True)
class ActionInfo:
    id: str
    label: str
    # Single-key shortcuts are suspended while a text field (category input) has focus.
    in_text_fields: bool = False


ACTIONS: list[ActionInfo] = [
    ActionInfo("keep", "Keep"),
    ActionInfo("delete", "Delete"),
    ActionInfo("delete_folder", "Delete folder"),
    ActionInfo("tag", "Tag"),
    ActionInfo("tag_folder", "Tag folder"),
    ActionInfo("skip", "Skip"),
    ActionInfo("open", "Open in default app"),
    ActionInfo("expand_preview", "Expand preview"),
    ActionInfo("undo", "Undo", in_text_fields=True),
    ActionInfo("redo", "Redo", in_text_fields=True),
]

ACTION_IDS = [a.id for a in ACTIONS]


class Keymap:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._map = {a: QKeySequence(mapping.get(a, DEFAULT_KEYMAP[a])) for a in ACTION_IDS}

    def sequence(self, action: str) -> QKeySequence:
        return self._map[action]

    def label(self, action: str) -> str:
        """Platform-native text, e.g. '⇧D' on macOS, 'Shift+D' elsewhere."""
        return self._map[action].toString(QKeySequence.SequenceFormat.NativeText)

    def to_settings(self) -> dict[str, str]:
        return {
            a: s.toString(QKeySequence.SequenceFormat.PortableText) for a, s in self._map.items()
        }

    def with_binding(self, action: str, sequence: QKeySequence) -> Keymap:
        mapping = self.to_settings()
        mapping[action] = sequence.toString(QKeySequence.SequenceFormat.PortableText)
        return Keymap(mapping)

    def conflicts(self) -> dict[str, list[str]]:
        """Key text → actions bound to it, for keys bound more than once."""
        by_key: dict[str, list[str]] = {}
        for action, seq in self._map.items():
            if not seq.isEmpty():
                by_key.setdefault(seq.toString(), []).append(action)
        return {k: v for k, v in by_key.items() if len(v) > 1}
