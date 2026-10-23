"""Duplicates in the triage and review windows (docs/duplicates.md §5)."""

import os
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QInputDialog, QMessageBox
from pytestqt.qtbot import QtBot

from degreaser.core.model import Effective, Origin
from degreaser.core.project import Project, ProjectSettings
from degreaser.core.settings import Settings
from degreaser.ui.windows.triage import TriageWindow

TIMEOUT = 5000
MB = 1024 * 1024


@pytest.fixture
def root(tmp_path: Path) -> Path:
    photo = os.urandom(MB + 10)
    root = tmp_path / "backup"
    for folder in ("a original", "b copy", "c copy"):
        (root / folder).mkdir(parents=True)
        (root / folder / "photo.jpg").write_bytes(photo)
    (root / "a original" / "notes.txt").write_bytes(b"unique")
    return root


def open_window(qtbot: QtBot, root: Path, **settings: object) -> TriageWindow:
    project = Project.create("Backup", {"type": "local", "path": str(root)})
    project.save_settings(ProjectSettings(sync_enabled=False, **settings))  # type: ignore[arg-type]
    window = TriageWindow(project, Settings())
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window.session.current is not None, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: not window._indexing and not window._dup_running, timeout=TIMEOUT)
    return window


def test_banner_and_details_name_the_copies(qtbot: QtBot, root: Path) -> None:
    window = open_window(qtbot, root)
    assert window.session.current is not None
    assert window.session.current.path == "a original/notes.txt"
    qtbot.keyClick(window, Qt.Key.Key_K)  # notes.txt
    assert window.session.current.path == "a original/photo.jpg"
    assert window.banner.text().endswith("Duplicate of b copy/photo.jpg and 1 other")
    rows = [r for r in window.metadata.rows() if r.group == "Duplicates"]
    assert [r.value for r in rows] == ["b copy/photo.jpg", "c copy/photo.jpg"]

    qtbot.keyClick(window, Qt.Key.Key_K)  # keep the original
    assert window.session.current.path == "b copy/photo.jpg"
    assert "Duplicate of a original/photo.jpg and 1 other — kept" in window.banner.text()
    window.close()


def test_delete_duplicates_rule_skips_copies(qtbot: QtBot, root: Path) -> None:
    window = open_window(qtbot, root, delete_duplicates=True)
    qtbot.keyClick(window, Qt.Key.Key_K)  # notes.txt
    qtbot.keyClick(window, Qt.Key.Key_K)  # the original photo
    state = window.session.state_of("c copy/photo.jpg")
    assert state.action is Effective.DELETE and state.origin is Origin.DUPLICATE
    assert window.session.current is None  # both copies were decided by the rule
    window.close()


def test_review_keep_one_delete_the_others(
    qtbot: QtBot, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = open_window(qtbot, root)
    window.open_review()
    review = window.review_window
    assert review is not None
    qtbot.addWidget(review)
    review.filter_box.setCurrentIndex(review.filter_box.findData("duplicates"))
    assert [r[0] for r in review.model.rows] == [
        "a original/photo.jpg",
        "b copy/photo.jpg",
        "c copy/photo.jpg",
    ]
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("Shortest path", True))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    review.keep_one_per_group()
    actions = {r[0]: r[2] for r in review.model.rows}
    assert actions == {
        "a original/photo.jpg": "delete",
        "b copy/photo.jpg": "keep",  # "b copy" is shorter than "a original"
        "c copy/photo.jpg": "delete",
    }
    review.close()
    window.close()


def test_review_rows_show_the_full_path_as_a_tooltip(qtbot: QtBot, root: Path) -> None:
    from PySide6.QtCore import Qt as QtCore

    window = open_window(qtbot, root)
    qtbot.keyClick(window, Qt.Key.Key_K)
    window.open_review()
    review = window.review_window
    assert review is not None
    qtbot.addWidget(review)
    model = review.model
    for column in range(model.columnCount()):
        tip = model.data(model.index(0, column), QtCore.ItemDataRole.ToolTipRole)
        assert tip.startswith("a original/notes.txt")
    assert review.table.textElideMode() == QtCore.TextElideMode.ElideMiddle
    review.close()
    window.close()


def test_double_click_in_review_opens_the_file_in_triage(qtbot: QtBot, root: Path) -> None:
    window = open_window(qtbot, root)
    qtbot.keyClick(window, Qt.Key.Key_K)  # notes.txt
    window.open_review()
    review = window.review_window
    assert review is not None
    qtbot.addWidget(review)
    assert window.session.current is not None
    assert window.session.current.path == "a original/photo.jpg"
    with qtbot.waitSignal(review.open_requested) as opened:
        review._open_row(review.model.index(0, 2))  # the kept notes.txt row
    assert opened.args == ["a original/notes.txt"]
    assert window.session.current.path == "a original/notes.txt"
    assert "a original/notes.txt" in window.path_bar._path
    review.close()
    window.close()
