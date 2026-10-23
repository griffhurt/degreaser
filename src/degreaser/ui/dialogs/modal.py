"""Running modal dialogs without leaving them alive until the app quits."""

from PySide6.QtWidgets import QDialog


def run_modal(dialog: QDialog) -> bool:
    """exec() the dialog and return whether it was accepted, then schedule its deletion.

    A dialog parented to a window otherwise lives until that window does, and PySide's
    cleanup at interpreter exit can then free its child widgets a second time, crashing on
    quit ("Degreaser quit unexpectedly"). Deletion waits for the event loop, so the caller
    can still read the dialog's results straight after this returns.
    """
    try:
        return dialog.exec() == QDialog.DialogCode.Accepted
    finally:
        dialog.deleteLater()
