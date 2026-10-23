"""Window sizes that fit the screen they open on."""

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget

MARGIN = 40  # leave a little of the desktop visible around a new window


def fit_to_screen(widget: QWidget, width: int, height: int) -> None:
    """Resize to (width, height), or smaller if the screen's free area is smaller."""
    screen = widget.screen() or QGuiApplication.primaryScreen()
    if screen is not None:
        area = screen.availableGeometry()
        width = min(width, area.width() - MARGIN)
        height = min(height, area.height() - MARGIN)
    widget.resize(width, height)
