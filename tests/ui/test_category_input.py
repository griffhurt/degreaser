import sqlite3

import pytest
from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from degreaser.core.categories import CategoryIndex, CategoryStore, Completer
from degreaser.core.db.connection import connect
from degreaser.ui.widgets.category_input import CategoryInput


@pytest.fixture
def completer() -> Completer:
    conn: sqlite3.Connection = connect(":memory:")
    store = CategoryStore(conn)
    paths = {
        "a": ["Videos", "High School", "Senior Film Project"],
        "b": ["Videos", "High School"],
        "c": ["Videos", "Family"],
        "d": ["ProgrammingProject"],
    }
    for path, names in paths.items():
        category_id, _ = store.get_or_create(names)
        conn.execute("INSERT INTO decisions VALUES (?, 0, 'keep', 0, NULL, NULL)", (path,))
        conn.execute("INSERT INTO node_categories VALUES (?, ?)", (path, category_id))
    return Completer(CategoryIndex.build(conn, store))


def test_ghost_tab_and_submit(qtbot: QtBot, completer: Completer) -> None:
    widget = CategoryInput()
    qtbot.addWidget(widget)
    widget.begin(completer, "Tag file")
    qtbot.keyClicks(widget.edit, "Programmingp")
    assert widget.edit.ghost == "roject"

    qtbot.keyClick(widget.edit, Qt.Key.Key_Tab)
    assert widget.edit.text() == "ProgrammingProject, "
    assert widget.edit.hasFocus() or not widget.isActiveWindow()

    with qtbot.waitSignal(widget.submitted) as sig:
        qtbot.keyClick(widget.edit, Qt.Key.Key_Return)
    assert sig.args == [["ProgrammingProject"]]


def test_contextual_completion_and_cycling(qtbot: QtBot, completer: Completer) -> None:
    widget = CategoryInput()
    qtbot.addWidget(widget)
    widget.begin(completer, "Tag file", initial=["Videos"])
    assert widget.edit.text() == "Videos, "
    qtbot.keyClick(widget.edit, Qt.Key.Key_Tab)
    first = widget.edit.text()
    qtbot.keyClick(widget.edit, Qt.Key.Key_Tab)
    second = widget.edit.text()
    assert {first, second} == {"Videos, High School, ", "Videos, Family, "}
    qtbot.keyClick(widget.edit, Qt.Key.Key_Backtab)
    assert widget.edit.text() == first


def test_new_tag_with_comma_and_invalid_blocked(qtbot: QtBot, completer: Completer) -> None:
    widget = CategoryInput()
    qtbot.addWidget(widget)
    widget.begin(completer, "Tag file")
    qtbot.keyClicks(widget.edit, "Brand New, Bad:Name")
    assert widget.issues.isVisibleTo(widget)
    submitted: list[list[str]] = []
    widget.submitted.connect(submitted.append)
    qtbot.keyClick(widget.edit, Qt.Key.Key_Return)
    assert submitted == []

    widget.edit.setText("Brand New, Fine")
    qtbot.keyClick(widget.edit, Qt.Key.Key_Return)
    assert submitted == [["Brand New", "Fine"]]


def test_escape_cancels(qtbot: QtBot, completer: Completer) -> None:
    widget = CategoryInput()
    qtbot.addWidget(widget)
    widget.begin(completer, "Tag folder", inherited=["Videos"])
    assert "Videos" in widget.inherited.text()
    with qtbot.waitSignal(widget.cancelled):
        qtbot.keyClick(widget.edit, Qt.Key.Key_Escape)
    assert widget.isHidden()
