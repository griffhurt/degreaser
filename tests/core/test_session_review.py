"""TriageSession.review_set: bulk decision edits from the review screen."""

import pytest

from degreaser.core.model import Effective
from degreaser.core.triage import FolderActionRefused
from tests.core.test_session import check_tracker, current, session
from tests.core.triage_helpers import Tree


def test_bulk_set_and_clear_files_without_moving_cursor() -> None:
    t = Tree().add("a.txt", "b.txt", "c.txt", size=5)
    s = session(t)
    s.keep()
    s.keep()
    assert current(s) == "c.txt"

    r = s.review_set(["a.txt", "b.txt"], "delete")
    assert r.label == "Delete 2 items" and r.advance is None
    assert current(s) == "c.txt"
    assert s.state_of("a.txt").action is Effective.DELETE
    check_tracker(s)

    s.review_set(["a.txt"], None)
    assert s.state_of("a.txt").action is Effective.UNTRIAGED
    check_tracker(s)

    s.undo()
    assert s.state_of("a.txt").action is Effective.DELETE


def test_keep_review_drops_nothing_but_delete_drops_category() -> None:
    t = Tree().add("a.txt", size=1)
    s = session(t)
    s.tag(["Videos"])
    s.review_set(["a.txt"], "keep")
    assert s.state_of("a.txt").category == ("Videos",)
    s.review_set(["a.txt"], "delete")
    assert s.state_of("a.txt").category is None
    check_tracker(s)


def test_folders_only_delete_or_clear() -> None:
    t = Tree().add("d/x.txt", "d/y.txt", size=3)
    s = session(t)
    with pytest.raises(ValueError):
        s.review_set(["d"], "keep")
    s.review_set(["d"], "delete")
    assert s.state_of("d/x.txt").action is Effective.DELETE
    assert s.tracker.stale
    s.review_set(["d"], None)
    assert s.state_of("d/x.txt").action is Effective.UNTRIAGED


def test_tag_folder_keeps_skipped_files_inside() -> None:
    t = Tree().add("d/a.txt", "d/b.txt", size=1)
    s = session(t)
    s.skip()
    assert s.state_of("d/a.txt").action is Effective.SKIP
    s.jump_to("d/b.txt")
    s.tag_folder(["Docs"])
    assert s.state_of("d/a.txt").action is Effective.KEEP
    assert s.state_of("d/a.txt").category == ("Docs",)
    check_tracker(s)


def test_root_folder_actions_are_refused() -> None:
    t = Tree().add("a.txt", "d/b.txt", size=2)
    s = session(t)
    with pytest.raises(FolderActionRefused):
        s.delete_folder("")
    with pytest.raises(FolderActionRefused):
        s.tag_folder(["All"], "")
