"""Effective-state precedence (docs/triage-engine.md §2) and walks (§8)."""

import pytest

from degreaser.core.model import DeleteTarget, Effective, Origin, WalkSummary
from degreaser.core.rules import ORPHAN_APPLEDOUBLE, RuleSet
from degreaser.core.triage.resolver import Resolver
from degreaser.core.triage.walk import (
    SummaryTracker,
    count_state,
    full_walk,
    subtree_summary,
)
from tests.core.triage_helpers import Tree


def resolver(t: Tree) -> Resolver:
    return Resolver(t.conn, RuleSet.load(t.conn), t.store)


def state(t: Tree, path: str) -> tuple[Effective, Origin, str | None]:
    s = resolver(t).resolve(path)
    return s.action, s.origin, s.decided_by


# §2.1 rows -----------------------------------------------------------------------------------


def test_untriaged_by_default() -> None:
    t = Tree().add("a/b.txt")
    assert state(t, "a/b.txt") == (Effective.UNTRIAGED, Origin.NONE, None)
    assert state(t, "a") == (Effective.UNTRIAGED, Origin.NONE, None)


def test_row1_folder_delete_beats_everything_inside() -> None:
    t = Tree().add("a/b/keep.psd", "a/b/kept.txt", "a/b/c/x.txt")
    t.rule("always_keep", "*.psd").decide("a/b/kept.txt", "keep").decide("a/b/c", "keep")
    t.categorise("a/b/kept.txt", ["Docs"]).decide("a", "delete")
    for f in ("a/b/keep.psd", "a/b/kept.txt", "a/b/c/x.txt"):
        assert state(t, f) == (Effective.DELETE, Origin.FOLDER, "a")
    assert state(t, "a") == (Effective.DELETE, Origin.DIRECT, "a")
    assert state(t, "a/b") == (Effective.DELETE, Origin.FOLDER, "a")


def test_row1_direct_delete() -> None:
    t = Tree().add("f.txt").rule("always_keep", "*.txt").decide("f.txt", "delete")
    assert state(t, "f.txt") == (Effective.DELETE, Origin.DIRECT, "f.txt")


@pytest.mark.parametrize(
    ("action", "effective"),
    [("keep", Effective.KEEP), ("skip", Effective.SKIP), ("deferred", Effective.DEFERRED)],
)
def test_row2_direct_decision_beats_rules(action: str, effective: Effective) -> None:
    t = Tree().add("node_modules/x.js").rule("always_delete", "node_modules/")
    t.decide("node_modules/x.js", action)
    assert state(t, "node_modules/x.js") == (effective, Origin.DIRECT, "node_modules/x.js")


def test_row3_always_delete_inside_kept_folder() -> None:
    t = Tree().add("proj/node_modules/lib/a.js", "proj/src/main.js", "proj/tmp.tmp")
    t.rule("always_delete", "node_modules/").rule("always_delete", "*.tmp")
    t.decide("proj", "keep").categorise("proj", ["Code"])
    assert state(t, "proj/node_modules/lib/a.js") == (
        Effective.DELETE,
        Origin.RULE,
        "node_modules/",
    )
    assert state(t, "proj/tmp.tmp") == (Effective.DELETE, Origin.RULE, "*.tmp")
    assert state(t, "proj/src/main.js") == (Effective.KEEP, Origin.FOLDER, "proj")
    assert state(t, "proj/node_modules") == (Effective.DELETE, Origin.RULE, "node_modules/")


def test_row3_before_row4() -> None:
    t = Tree().add("cache/art.psd").rule("always_keep", "*.psd").rule("always_delete", "cache/")
    assert state(t, "cache/art.psd") == (Effective.DELETE, Origin.RULE, "cache/")


def test_row4_always_keep_and_inherits_folder_category() -> None:
    t = Tree().add("photos/a.psd", "photos/b.jpg").rule("always_keep", "*.psd")
    t.add("loose.psd")
    assert state(t, "loose.psd") == (Effective.KEEP, Origin.RULE, "*.psd")
    t.decide("photos", "keep").categorise("photos", ["Photos"])
    s = resolver(t).resolve("photos/a.psd")
    assert (s.action, s.origin, s.category, s.category_from) == (
        Effective.KEEP,
        Origin.RULE,
        ("Photos",),
        "photos",
    )


def test_row5_folder_keep_with_nearest_category() -> None:
    t = Tree().add("v/hs/film/cut.mp4", "v/hs/other.mp4")
    t.decide("v", "keep").categorise("v", ["Videos"])
    t.decide("v/hs/film", "keep").categorise("v/hs/film", ["Videos", "High School", "Film"])
    r = resolver(t)
    cut = r.resolve("v/hs/film/cut.mp4")
    assert (cut.action, cut.origin, cut.decided_by) == (Effective.KEEP, Origin.FOLDER, "v/hs/film")
    assert (cut.category, cut.category_from) == (("Videos", "High School", "Film"), "v/hs/film")
    other = r.resolve("v/hs/other.mp4")
    assert (other.category, other.category_from, other.decided_by) == (("Videos",), "v", "v")


def test_own_category_beats_inherited() -> None:
    t = Tree().add("v/a.mp4").decide("v", "keep").categorise("v", ["Videos"])
    t.decide("v/a.mp4", "keep").categorise("v/a.mp4", ["Videos", "Family"])
    s = resolver(t).resolve("v/a.mp4")
    assert (s.origin, s.category, s.category_from) == (
        Origin.DIRECT,
        ("Videos", "Family"),
        "v/a.mp4",
    )


def test_skip_and_delete_have_no_category() -> None:
    t = Tree().add("v/a", "v/b").decide("v", "keep").categorise("v", ["Videos"])
    t.decide("v/a", "skip").decide("v/b", "delete")
    r = resolver(t)
    assert r.resolve("v/a").category is None
    assert r.resolve("v/b").category is None


# AppleDouble and exclusions -------------------------------------------------------------------


def test_appledouble_follows_companion() -> None:
    t = Tree().add("d/photo.jpg", "d/._photo.jpg", "d/._orphan", "d/sub/", "d/._sub")
    t.decide("d/photo.jpg", "keep")
    t.decide("d/sub", "delete")
    assert state(t, "d/._photo.jpg") == (Effective.KEEP, Origin.COMPANION, "d/photo.jpg")
    assert state(t, "d/._orphan") == (Effective.DELETE, Origin.RULE, ORPHAN_APPLEDOUBLE)
    assert state(t, "d/._sub") == (Effective.DELETE, Origin.COMPANION, "d/sub")
    t.decide("d/._photo.jpg", "delete")
    assert state(t, "d/._photo.jpg") == (Effective.DELETE, Origin.DIRECT, "d/._photo.jpg")


def test_appledouble_untriaged_companion() -> None:
    t = Tree().add("a.txt", "._a.txt")
    assert state(t, "._a.txt") == (Effective.UNTRIAGED, Origin.COMPANION, "a.txt")


def test_degreaser_folder_excluded() -> None:
    t = Tree().add(".degreaser/catalog.json", ".degreaser/trash/x/f.txt", "sub/.degreaser/f")
    assert state(t, ".degreaser/catalog.json")[1] is Origin.EXCLUDED
    assert state(t, ".degreaser/trash/x/f.txt")[1] is Origin.EXCLUDED
    assert state(t, "sub/.degreaser/f")[0] is Effective.UNTRIAGED  # only the root one is ours


# Walks ----------------------------------------------------------------------------------------


def build_mixed() -> Tree:
    t = Tree()
    t.add(
        "keep/a.txt",
        "keep/b.txt",
        "keep/node_modules/x/y.js",
        "keep/node_modules/z.js",
        "gone/one.bin",
        "gone/two/three.bin",
        "loose/u.txt",
        "loose/skip.txt",
        "loose/def.txt",
        "loose/del.txt",
        "loose/c.tmp",
        "loose/._u.txt",
        "loose/._missing",
        "nm/node_modules/kept.js",
        "nm/node_modules/sub/other.js",
        ".degreaser/catalog.json",
        size=100,
    )
    t.rule("always_delete", "node_modules/").rule("always_delete", "*.tmp")
    t.decide("keep", "keep").categorise("keep", ["Work"])
    t.decide("keep/b.txt", "keep").categorise("keep/b.txt", ["Work", "Notes"])
    t.decide("gone", "delete")
    t.decide("loose/skip.txt", "skip").decide("loose/def.txt", "deferred")
    t.decide("loose/del.txt", "delete")
    t.decide("nm/node_modules/kept.js", "keep")
    return t


def test_full_walk_figures_targets_and_catalog() -> None:
    t = build_mixed()
    result = full_walk(t.conn, RuleSet.load(t.conn), t.store)
    s = result.summary
    assert s.files_known == 15  # .degreaser excluded
    assert s.files_untriaged == 2  # loose/u.txt and its companion
    assert s.files_skip == 1 and s.files_deferred == 1
    assert s.files_keep == 3  # keep/a, keep/b, nm/.../kept.js
    assert s.files_categorised == 2
    assert s.files_delete == 8
    assert s.bytes_delete == 800
    assert s.files_triaged == 13
    targets = {(d.path, d.is_dir) for d in result.delete_targets}
    assert targets == {
        ("gone", True),
        ("keep/node_modules", True),
        ("loose/del.txt", False),
        ("loose/c.tmp", False),
        ("loose/._missing", False),
        # nm/node_modules holds a kept file, so it can't go in one operation.
        ("nm/node_modules/sub", True),
    }
    assert result.catalog_files == {"keep/a.txt": ("Work",), "keep/b.txt": ("Work", "Notes")}
    assert result.catalog_folders == {"keep": ("Work",)}


def test_walk_agrees_with_single_resolve() -> None:
    t = build_mixed()
    r = resolver(t)
    expected = WalkSummary()
    for path in t.files():
        count_state(expected, r.resolve(path), 100)
    assert full_walk(t.conn, RuleSet.load(t.conn), t.store).summary == expected


def test_walk_handles_children_before_parents() -> None:
    t = Tree()
    t.add("late/", node_id=500)
    t.add("late/child.txt", node_id=10)
    t.add("late/sub/", node_id=400)
    t.add("late/sub/f.txt", node_id=11)
    t.decide("late/sub", "delete")
    result = full_walk(t.conn, RuleSet.load(t.conn), t.store)
    assert result.summary.files_known == 2
    assert result.summary.files_delete == 1
    assert result.delete_targets == [DeleteTarget("late/sub", True, None, 1000.0)]


def test_subtree_summary() -> None:
    t = build_mixed()
    s = subtree_summary(t.conn, RuleSet.load(t.conn), t.store, "loose")
    assert (s.files_known, s.files_delete, s.files_untriaged) == (7, 3, 2)
    s = subtree_summary(t.conn, RuleSet.load(t.conn), t.store, "gone")
    assert (s.files_known, s.files_delete) == (2, 2)


def test_summary_tracker_generations() -> None:
    tracker = SummaryTracker()
    t = Tree().add("a")
    r = resolver(t)
    before = r.resolve("a")
    tracker.apply_file(before, before, 1)  # no summary yet: marks stale
    token = tracker.begin_walk()
    tracker.finish_walk(token, WalkSummary(files_known=1, files_untriaged=1))
    assert not tracker.stale
    token = tracker.begin_walk()
    t.decide("a", "keep")
    after = resolver(t).resolve("a")
    tracker.apply_file(before, after, 1)  # arrives mid-walk
    assert tracker.summary == WalkSummary(files_known=1, files_keep=1)
    tracker.finish_walk(token, WalkSummary(files_known=1, files_keep=1))
    assert tracker.stale  # the owner should walk again to be sure
