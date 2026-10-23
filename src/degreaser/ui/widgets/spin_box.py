"""Spin boxes with the same left inset as line edits.

Qt's stylesheet engine ignores left padding on spin boxes (the inner text field always starts
at the frame edge), so the inset is set on the text field itself.
"""

from PySide6.QtWidgets import QDoubleSpinBox, QSpinBox, QWidget

TEXT_INSET = 9  # matches QLineEdit's 10px padding minus the 1px border


class SpinBox(QSpinBox):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.lineEdit().setTextMargins(TEXT_INSET, 0, 0, 0)


class DoubleSpinBox(QDoubleSpinBox):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.lineEdit().setTextMargins(TEXT_INSET, 0, 0, 0)
