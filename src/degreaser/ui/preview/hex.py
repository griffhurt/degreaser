"""Hex dump preview for binary and unknown files."""

from PySide6.QtWidgets import QWidget

from degreaser.core.preview.loaders import format_hex
from degreaser.ui.preview.text import TextView


class HexView(TextView):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(monospace=True, wrap=False, parent=parent)
        self.offset = 0

    def append_bytes(self, data: bytes) -> None:
        text = format_hex(data, self.offset)
        self.append(("\n" if self.offset else "") + text)
        self.offset += len(data)
