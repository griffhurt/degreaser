"""End-to-end: real folder, real project, triage window driven by keys, then commit."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from PySide6.QtCore import QItemSelectionModel, Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QWidget
from pytestqt.qtbot import QtBot

from degreaser.core.model import Effective
from degreaser.core.project import Project
from degreaser.core.settings import Settings
from degreaser.ui.dialogs.commit import CommitRunDialog
from degreaser.ui.windows.triage import TriageWindow


class ModalResponder:
    """Answers modal dialogs as they appear: handler(widget) -> True when handled."""

    def __init__(self, handlers: list[Callable[[QWidget], bool]]) -> None:
        self.handlers = handlers
        self.seen: list[str] = []
        self.timer = QTimer(interval=30)
        self.timer.timeout.connect(self._poll)
        self.timer.start()

    def _poll(self) -> None:
        widget = QApplication.activeModalWidget()
        if widget is None or not widget.isVisible():
            return
        for handler in self.handlers:
            if handler(widget):
                self.seen.append(type(widget).__name__)
                return

    def stop(self) -> None:
        self.timer.stop()


def click_button(text: str) -> Callable[[QWidget], bool]:
    def handler(widget: QWidget) -> bool:
        if not isinstance(widget, QMessageBox):
            return False
        for button in widget.buttons():
            if button.text().replace("&", "") == text:
                button.click()
                return True
        return False

    return handler


def fail_on_message(widget: QWidget) -> bool:
    """Unexpected message box: record its text and dismiss it so the test can fail cleanly."""
    if isinstance(widget, QMessageBox):
        UNEXPECTED.append(f"{widget.windowTitle()}: {widget.text()}")
        widget.done(0)
        return True
    return False


UNEXPECTED: list[str] = []


def close_commit_dialog(widget: QWidget) -> bool:
    if isinstance(widget, CommitRunDialog) and widget.outcome is not None:
        widget.close_button.click()
        return True
    return False


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "backup"
    (root / "docs").mkdir(parents=True)
    (root / "junk").mkdir()
    (root / "a.txt").write_text("alpha")
    (root / "b.txt").write_text("bravo")
    (root / "docs" / "essay.txt").write_text("an essay")
    (root / "junk" / "x.tmp").write_text("x")
    (root / "junk" / "y.tmp").write_text("y")
    (root / ".DS_Store").write_bytes(b"\0")
    return root


def open_window(qtbot: QtBot, root: Path) -> tuple[Project, TriageWindow]:
    project = Project.create("Backup", {"type": "local", "path": str(root)})
    window = TriageWindow(project, Settings())
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window.session.current is not None, timeout=5000)
    return project, window


def press(
    qtbot: QtBot, window: TriageWindow, key: Qt.Key, mod=Qt.KeyboardModifier.NoModifier
) -> None:  # type: ignore[no-untyped-def]
    qtbot.keyClick(window, key, mod)


def current(window: TriageWindow) -> str | None:
    item = window.session.current
    return None if item is None else item.path


def test_triage_flow_and_commit(qtbot: QtBot, tree: Path) -> None:
    project, window = open_window(qtbot, tree)
    # Files first, natural order; .DS_Store is an always-delete rule and never shown.
    assert current(window) == "a.txt"
    assert window.path_bar.segments[0].text() == "Backup"

    press(qtbot, window, Qt.Key.Key_K)
    assert current(window) == "b.txt"
    press(qtbot, window, Qt.Key.Key_D)
    qtbot.waitUntil(lambda: current(window) == "docs/essay.txt", timeout=5000)

    # Tag with a new category.
    press(qtbot, window, Qt.Key.Key_T)
    assert window.category_input.isVisible()
    qtbot.keyClicks(window.category_input.edit, "School, English")
    qtbot.keyClick(window.category_input.edit, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: current(window) == "junk/x.tmp", timeout=5000)
    assert window.session.state_of("docs/essay.txt").category == ("School", "English")

    # Delete the whole junk folder (nothing kept inside, so no warning).
    press(qtbot, window, Qt.Key.Key_D, Qt.KeyboardModifier.ShiftModifier)
    qtbot.waitUntil(lambda: window.session.current is None, timeout=5000)
    qtbot.waitUntil(
        lambda: window.stack.currentIndex() == 1 and window.review_commit_button.isVisible(),
        timeout=5000,
    )

    # Undo brings the folder action back; redo re-applies it.
    window.undo()
    assert current(window) == "junk/x.tmp"
    assert window.session.state_of("junk/y.tmp").action is Effective.UNTRIAGED
    window.redo()
    assert window.session.state_of("junk/y.tmp").action is Effective.DELETE

    # Review screen shows the decisions; un-delete b.txt there.
    window.open_review()
    review = window.review_window
    assert review is not None
    paths = [r[0] for r in review.model.rows]
    assert {"a.txt", "b.txt", "docs/essay.txt", "junk"} <= set(paths)
    # selectRow() depends on live keyboard modifiers in an extended-selection view; be explicit.
    flags = (
        QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows
    )
    review.table.selectionModel().select(review.model.index(paths.index("b.txt"), 0), flags)
    review.apply("keep")
    assert window.session.state_of("b.txt").action is Effective.KEEP

    qtbot.waitUntil(
        lambda: window.session.tracker.summary is not None and not window.session.tracker.stale,
        timeout=5000,
    )
    summary = window.session.tracker.summary
    assert summary is not None and summary.files_delete == 3  # junk/x, junk/y, .DS_Store

    responder = ModalResponder([click_button("Commit"), close_commit_dialog, fail_on_message])
    try:
        window.commit()
    finally:
        responder.stop()
    assert UNEXPECTED == []

    assert not (tree / "junk").exists()
    assert not (tree / ".DS_Store").exists()
    assert (tree / "a.txt").exists() and (tree / "b.txt").exists()
    trash = list((tree / ".degreaser" / "trash").iterdir())
    assert len(trash) == 1 and (trash[0] / "junk" / "x.tmp").exists()
    catalog = json.loads((tree / ".degreaser" / "catalog.json").read_text())
    assert catalog["files"] == {"docs/essay.txt": ["School", "English"]}
    assert not window.undo_action.isEnabled()

    window.close()
    project.close()


def test_delete_folder_with_kept_files_warns(qtbot: QtBot, tree: Path) -> None:
    project, window = open_window(qtbot, tree)
    window.jump_to("junk/x.tmp")
    press(qtbot, window, Qt.Key.Key_K)
    assert current(window) == "junk/y.tmp"

    responder = ModalResponder([click_button("Cancel")])
    try:
        press(qtbot, window, Qt.Key.Key_D, Qt.KeyboardModifier.ShiftModifier)
    finally:
        responder.stop()
    assert responder.seen == ["QMessageBox"]
    assert window.session.state_of("junk/y.tmp").action is Effective.UNTRIAGED

    # The offscreen platform doesn't hand activation back after a modal closes (a real
    # desktop does), and window shortcuts need an active window.
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow)
    responder = ModalResponder([click_button("Yes")])
    try:
        press(qtbot, window, Qt.Key.Key_D, Qt.KeyboardModifier.ShiftModifier)
    finally:
        responder.stop()
    assert responder.seen == ["QMessageBox"]
    assert window.session.state_of("junk/x.tmp").action is Effective.DELETE
    window.close()
    project.close()


def test_action_bar_buttons_have_no_focus_and_dropdown_lists_children(
    qtbot: QtBot, tree: Path
) -> None:
    project, window = open_window(qtbot, tree)
    listing = window.folder_listing("")
    names = [e.name for e in listing.entries]
    assert names[:2] == [".DS_Store", "a.txt"] and "docs" in names
    ds = listing.entries[0]
    assert ds.state is Effective.DELETE and ds.origin.value == "rule"
    assert all(
        b.focusPolicy() == Qt.FocusPolicy.NoFocus
        for b in window.action_bar.buttons.values()
        if isinstance(b, QPushButton)
    )
    window.close()
    project.close()


def test_keep_folder_keeps_rest_and_moves_on(qtbot: QtBot, tree: Path) -> None:
    project, window = open_window(qtbot, tree)
    window.jump_to("junk/x.tmp")
    window.session.tag(["Keepers"])  # an own category on a file inside
    window.jump_to("junk/x.tmp")
    press(qtbot, window, Qt.Key.Key_K, Qt.KeyboardModifier.ShiftModifier)
    assert window.session.state_of("junk/y.tmp").action is Effective.KEEP
    assert window.session.state_of("junk/x.tmp").category == ("Keepers",)
    # Traversal continues after the folder: nothing else is left in this tree but the root files.
    assert current(window) != "junk/y.tmp"
    window.close()
    project.close()
