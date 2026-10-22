"""TriageSession flows (docs/triage-engine.md §1.3, §3, §5, §6)."""

import json

import pytest

from degreaser.core.categories import InvalidCategory
from degreaser.core.model import Effective, Origin
from degreaser.core.rules import RuleSet
from degreaser.core.triage import (
    Banner,
    FolderActionRefused,
    ItemSource,
    Mode,
    NoCurrentItem,
    TriageSession,
    full_walk,
    sweep_first,
)
from tests.core.triage_helpers import Tree


def session(t: Tree, *, start: bool = True, **settings: object) -> TriageSession:
    t.conn.execute("UPDATE meta SET value = ? WHERE key = 'settings'", (json.dumps(settings),))
    s = TriageSession(t.conn)
    walk = full_walk(t.conn, s.rules, s.store)
    s.tracker.finish_walk(s.tracker.begin_walk(), walk.summary)
    if start:
        s.start()
    return s


def check_tracker(s: TriageSession) -> None:
    """The incremental figures must always equal a fresh full walk."""
    assert not s.tracker.stale
    assert s.tracker.summary == full_walk(s.conn, RuleSet.load(s.conn), s.store).summary


def current(s: TriageSession) -> str | None:
    return None if s.current is None else s.current.path


def revision(t: Tree) -> int:
    return int(t.conn.execute("SELECT value FROM meta WHERE key = 'state_revision'").fetchone()[0])


def test_basic_flow_keep_delete_skip() -> None:
    t = Tree().add("a.txt", "b.txt", "c.txt", "d/e.txt", size=7)
    s = session(t)
    assert current(s) == "a.txt"
    r = s.keep()
    assert r.label == "Keep a.txt" and r.advance and r.advance.item
    assert current(s) == "b.txt"
    s.delete()
    s.skip()
    assert current(s) == "d/e.txt"
    assert [t.decision(p) for p in ("a.txt", "b.txt", "c.txt")] == ["keep", "delete", "skip"]
    row = t.conn.execute(
        "SELECT seen_size, seen_mtime FROM decisions WHERE path='a.txt'"
    ).fetchone()
    assert tuple(row) == (7, 1000.0)
    s.keep()
    assert current(s) is None
    check_tracker(s)
    assert revision(t) == 4


def test_end_needs_sweep_and_sweep_finds_jumped_over_files() -> None:
    t = Tree().add("a", "b", "c")
    s = session(t)
    s.jump_to("b")
    s.keep()
    assert current(s) == "c"
    adv = s.keep()
    assert adv.advance is not None and adv.advance.needs_sweep
    assert sweep_first(t.conn) == "a"
    adv2 = s.apply_sweep(sweep_first(t.conn))
    assert current(s) == "a" and adv2.item is not None
    s.keep()
    adv3 = s.run_sweep()
    assert adv3.done


def test_cursor_persists_across_sessions() -> None:
    t = Tree().add("a", "b", "c")
    s = session(t)
    s.keep()
    assert current(s) == "b"
    s2 = TriageSession(t.conn)
    s2.start()
    assert current(s2) == "b"


def test_needs_listing_signal() -> None:
    t = Tree().add("a.txt").add("later/", listed=False)
    s = session(t)
    adv = s.keep().advance
    assert adv is not None and adv.needs_listing == "later" and not adv.done
    t.conn.execute("UPDATE nodes SET listed = 1 WHERE path = 'later'")
    t.add("later/x.txt")
    s.on_tree_changed()
    assert s.advance().item is not None
    assert current(s) == "later/x.txt"


def test_no_current_item() -> None:
    t = Tree()
    s = session(t)
    with pytest.raises(NoCurrentItem):
        s.keep()


# Folder actions --------------------------------------------------------------------------------


def test_delete_folder_warning_and_resume_after_subtree() -> None:
    t = Tree().add("a/1", "a/2", "a/sub/3", "b/4")
    s = session(t)
    s.keep()  # a/1
    check = s.check_delete_folder()
    assert check.folder == "a" and check.kept == 1 and check.needs_confirmation
    r = s.delete_folder()
    assert r.label == "Delete folder a"
    assert current(s) == "b/4"
    assert s.state_of("a/1").action is Effective.DELETE
    check_tracker(s)
    assert s.cursor == "b/4"


def test_delete_folder_refuses_root_and_files() -> None:
    t = Tree().add("top.txt", "d/x")
    s = session(t)
    with pytest.raises(FolderActionRefused):
        s.delete_folder()
    with pytest.raises(ValueError):
        s.delete_folder("top.txt")


def test_tag_folder_clears_delete_marks_and_handles_conflicts() -> None:
    t = Tree().add("v/a.mp4", "v/b.mp4", "v/c.mp4", "v/s/d.mp4", "w/w.txt")
    t.decide("v/b.mp4", "delete")
    t.decide("v/c.mp4", "keep").categorise("v/c.mp4", ["Videos", "Family"])  # refines
    t.decide("v/s/d.mp4", "keep").categorise("v/s/d.mp4", ["Music"])  # conflicts
    s = session(t)
    assert current(s) == "v/a.mp4"
    check = s.check_tag_folder("videos")  # existing capitalisation applies
    assert check.delete_marks == 1 and check.conflicting == ["v/s/d.mp4"]
    assert check.needs_confirmation and check.blocked_by is None
    s.tag_folder("Videos", replace_conflicting=True)
    assert current(s) == "w/w.txt"
    assert t.decision("v/b.mp4") is None
    for f, cat in [
        ("v/a.mp4", ("Videos",)),
        ("v/b.mp4", ("Videos",)),
        ("v/c.mp4", ("Videos", "Family")),
        ("v/s/d.mp4", ("Videos",)),
    ]:
        st = s.state_of(f)
        assert st.action is Effective.KEEP and st.category == cat, f
    check_tracker(s)


def test_tag_folder_keep_theirs() -> None:
    t = Tree().add("v/x", "v/y")
    t.decide("v/y", "keep").categorise("v/y", ["Music"])
    s = session(t)
    s.tag_folder("Videos", "v")
    assert s.state_of("v/y").category == ("Music",)
    assert s.state_of("v/x").category == ("Videos",)


def test_tag_folder_blocked_by_deleted_ancestor_then_unmark() -> None:
    t = Tree().add("a/b/c.txt", "z")
    t.decide("a", "delete")
    s = session(t)
    s.jump_to("a/b/c.txt")
    check = s.check_tag_folder("X")
    assert check.blocked_by == "a" and not check.needs_confirmation
    with pytest.raises(FolderActionRefused) as refused:
        s.tag_folder("X")
    assert refused.value.blocked_by == "a"
    r = s.unmark_folder("a")
    assert r.advance is None and t.decision("a") is None
    s.tag_folder("X")
    assert s.state_of("a/b/c.txt").category == ("X",)
    check_tracker(s)


def test_keep_folder_without_category_and_own_category_matching_inherited() -> None:
    t = Tree().add("p/q/r.txt", "p/s.txt")
    t.decide("p", "keep").categorise("p", ["Work"])
    s = session(t, start=False)
    s.jump_to("p/q/r.txt")
    s.tag_folder("", "p/q")
    assert t.decision("p/q") == "keep"
    assert (
        t.conn.execute("SELECT count(*) FROM node_categories WHERE path='p/q'").fetchone()[0] == 0
    )
    s.jump_to("p/s.txt")
    s.tag("work")  # same as inherited: no own category stored
    assert (
        t.conn.execute("SELECT count(*) FROM node_categories WHERE path='p/s.txt'").fetchone()[0]
        == 0
    )


# Tagging and categories --------------------------------------------------------------------------


def test_tag_creates_category_updates_index_and_keeps() -> None:
    t = Tree().add("a", "b")
    s = session(t)
    s.tag("Videos, High School")
    assert t.decision("a") == "keep"
    assert s.state_of("a").category == ("Videos", "High School")
    assert s.index.candidates(["Videos"], "") == ["High School"]
    s.tag(["videos", "high school", "Film"])
    assert s.state_of("b").category == ("Videos", "High School", "Film")
    with pytest.raises(InvalidCategory):
        s.tag("bad/tag")
    check_tracker(s)


def test_delete_removes_own_category() -> None:
    t = Tree().add("a").decide("a", "keep").categorise("a", ["X"])
    s = session(t, start=False)
    s.jump_to("a")
    s.delete()
    assert t.conn.execute("SELECT count(*) FROM node_categories").fetchone()[0] == 0
    assert s.index.usage("X") == 0


# Undo / redo -----------------------------------------------------------------------------------


def test_undo_returns_to_file_with_banner_and_redo() -> None:
    t = Tree().add("a", "b", flag_new=True)
    s = session(t)
    assert s.current is not None and s.current.banner is Banner.NEW
    s.tag("Docs")
    assert t.flags("a") == (0, 0)
    item = s.undo()
    assert item is not None and item.path == "a" and item.banner is Banner.NEW
    assert item.source is ItemSource.UNDO
    assert t.decision("a") is None and s.index.usage("Docs") == 0
    check_tracker(s)
    adv = s.redo()
    assert adv is not None and current(s) == "b"
    assert s.state_of("a").category == ("Docs",)
    check_tracker(s)
    assert s.undo_label() == "Tag a"


def test_undo_folder_action_restores_everything() -> None:
    t = Tree().add("v/a", "v/b", "v/s/c", "z/z")
    t.decide("v/b", "delete")
    s = session(t)
    s.tag_folder("Videos")
    assert current(s) == "z/z"
    item = s.undo()
    assert item is not None and item.path == "v/a"
    assert t.decision("v") is None and t.decision("v/b") == "delete"
    assert s.state_of("v/a").action is Effective.UNTRIAGED
    check_tracker(s)
    s.redo()
    assert t.decision("v/b") is None
    check_tracker(s)


def test_undo_moves_cursor_back_not_forward() -> None:
    t = Tree().add("a", "b", "c", "d")
    s = session(t)
    s.keep()
    s.keep()  # cursor now c
    s.jump_to("a")
    s.undo()  # undoes "Keep b"
    assert current(s) == "b"
    s.keep()
    assert current(s) == "c"


def test_clear_history() -> None:
    t = Tree().add("a")
    s = session(t)
    s.keep()
    s.clear_history()
    assert s.undo() is None


# Queues and banners ------------------------------------------------------------------------------


def test_queue_priority_ready_then_new_then_changed_then_main() -> None:
    t = Tree().add("m1", "m2", "x/def")
    t.add("z/new", flag_new=True)
    t.add("y/changed", flag_changed=True)
    t.decide("y/changed", "keep", seen_size=1)
    t.decide("x/def", "deferred")
    s = session(t, jump_back=True)
    assert current(s) == "z/new" and s.current is not None and s.current.banner is Banner.NEW
    s.mark_deferred_ready("x/def")
    s.keep()  # z/new -> next is the ready deferred file
    assert current(s) == "x/def" and s.current is not None
    assert s.current.banner is Banner.LOADED_SKIP
    s.delete()
    assert current(s) == "y/changed" and s.current is not None
    assert s.current.banner is Banner.CHANGED
    assert s.current.previous is not None and s.current.previous.action is Effective.KEEP
    s.delete()
    assert t.flags("y/changed") == (0, 0)
    assert current(s) == "m1"
    check_tracker(s)


def test_changed_queue_ignored_when_retriage_off() -> None:
    t = Tree().add("a", flag_changed=True).add("b")
    t.decide("a", "keep")
    s = session(t, retriage_changed=False)
    assert current(s) == "b"


def test_new_files_inside_deleted_folder_not_queued() -> None:
    t = Tree().add("gone/new", flag_new=True).add("x")
    t.decide("gone", "delete")
    s = session(t)
    assert current(s) == "x"


def test_defer_with_and_without_jump_back() -> None:
    t = Tree().add("a", "b")
    s = session(t, jump_back=True)
    r = s.skip(defer=True)
    assert r.deferred and t.decision("a") == "deferred"
    assert s.deferred_paths() == ["a"]
    s.convert_deferred_to_skip("a")
    assert t.decision("a") == "skip"
    check_tracker(s)

    t2 = Tree().add("a")
    s2 = session(t2, jump_back=False)
    r2 = s2.skip(defer=True)
    assert not r2.deferred and t2.decision("a") == "skip"


def test_start_converts_deferred_when_jump_back_off() -> None:
    t = Tree().add("a", "b").decide("a", "deferred")
    session(t, jump_back=False)
    assert t.decision("a") == "skip"


def test_review_skipped_pass() -> None:
    t = Tree().add("a", "b", "c", "d/e")
    t.decide("a", "skip").decide("c", "skip").decide("d/e", "skip").decide("b", "keep")
    t.decide("d", "delete")  # d/e is now deleted by its folder: not offered for review
    s = session(t)
    assert s.skipped_count() == 3
    adv = s.start_review_skipped()
    assert s.mode is Mode.REVIEW_SKIPPED
    assert adv.item is not None and adv.item.banner is Banner.PREVIOUSLY_SKIPPED
    assert current(s) == "a"
    s.skip()  # skip again: stays skipped, moves on
    assert current(s) == "c"
    s.keep()
    assert current(s) is None and s.mode is Mode.NORMAL
    assert [t.decision(p) for p in ("a", "c")] == ["skip", "keep"]


def test_jump_to_and_lookahead() -> None:
    t = Tree().add("a", "b", "c", "d")
    s = session(t)
    item = s.jump_to("c")
    assert item.source is ItemSource.JUMP
    assert s.lookahead(5) == ["d"]
    with pytest.raises(ValueError):
        s.jump_to("missing")


def test_appledouble_follows_through_session() -> None:
    t = Tree().add("p.jpg", "._p.jpg", "q.jpg")
    s = session(t)
    assert current(s) == "p.jpg"
    s.delete()
    assert current(s) == "q.jpg"
    st = s.state_of("._p.jpg")
    assert (st.action, st.origin) == (Effective.DELETE, Origin.COMPANION)
    check_tracker(s)


def test_reload_rules_marks_tracker_stale() -> None:
    t = Tree().add("a.tmp", "b.txt")
    s = session(t)
    t.rule("always_delete", "*.tmp")
    s.reload_rules()
    assert s.tracker.stale
    assert s.state_of("a.tmp").action is Effective.DELETE


def test_folder_action_updates_folder_companion() -> None:
    t = Tree().add("d/x", "._d", "e/y")
    s = session(t)
    s.delete_folder("d")
    st = s.state_of("._d")
    assert (st.action, st.origin) == (Effective.DELETE, Origin.COMPANION)
    check_tracker(s)
    s.undo()
    check_tracker(s)


def test_mark_ready_ignored_without_jump_back_or_deferral() -> None:
    t = Tree().add("a", "b").decide("a", "skip")
    s = session(t, jump_back=True)
    s.mark_deferred_ready("a")  # not deferred
    assert current(s) == "b"
    s.keep()
    assert s.advance().needs_sweep


def test_tag_folder_from_path_bar_on_ancestor() -> None:
    t = Tree().add("a/b/c/f.txt", "a/b/g.txt", "z/z.txt")
    s = session(t)
    s.jump_to("a/b/c/f.txt")
    s.tag_folder("Archive", "a")
    assert current(s) == "z/z.txt"
    assert s.state_of("a/b/g.txt").category == ("Archive",)
    check_tracker(s)
