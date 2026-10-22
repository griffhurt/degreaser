"""DFS order, pruning and lazy listing (docs/triage-engine.md §1)."""

import pytest

from degreaser.core.rules import RuleSet
from degreaser.core.triage.order import dfs_key
from degreaser.core.triage.resolver import Resolver
from degreaser.core.triage.traversal import NeedsListing, Traversal
from tests.core.triage_helpers import Tree


def trav(t: Tree) -> Traversal:
    return Traversal(t.conn, Resolver(t.conn, RuleSet.load(t.conn), t.store))


def walk_all(tr: Traversal) -> list[str]:
    out, cur = [], tr.first()
    while cur is not None:
        out.append(cur)
        cur = tr.next_from(cur, inclusive=False)
    return out


def test_files_first_then_folders_in_natural_order() -> None:
    t = Tree().add(
        "b/x.txt", "a/z.txt", "file10.txt", "File2.txt", "file1.txt", "a/sub/q.txt", "a/b.txt"
    )
    assert walk_all(trav(t)) == [
        "file1.txt",
        "File2.txt",
        "file10.txt",
        "a/b.txt",
        "a/z.txt",
        "a/sub/q.txt",
        "b/x.txt",
    ]


def test_dfs_key_matches_traversal_order() -> None:
    t = Tree().add("b/x", "a/z", "f10", "f2", "a/sub/q", "a/b", "a/sub2/")
    order = walk_all(trav(t))
    assert order == sorted(order, key=lambda p: dfs_key(p, False))
    assert dfs_key("a", True) < dfs_key("a/z", False) < dfs_key("a/sub", True) < dfs_key("b", True)


def test_skips_decided_and_prunes_subtrees() -> None:
    t = Tree().add(
        "a.txt",
        "b.txt",
        "gone/x.txt",
        "kept/y.txt",
        "proj/node_modules/m.js",
        "proj/main.c",
        "proj/junk.tmp",
        "pics/._p.jpg",
        "pics/p.jpg",
        ".degreaser/catalog.json",
    )
    t.rule("always_delete", "node_modules/").rule("always_delete", "*.tmp")
    t.decide("a.txt", "keep").decide("gone", "delete").decide("kept", "keep")
    assert walk_all(trav(t)) == ["b.txt", "pics/p.jpg", "proj/main.c"]


def test_needs_listing_signal_then_retry() -> None:
    t = Tree().add("a.txt", "later/", listed=False)
    t.add("z.txt")
    tr = trav(t)
    assert tr.first() == "a.txt"
    with pytest.raises(NeedsListing) as signal:
        tr.next_from("z.txt", inclusive=False)
    assert signal.value.path == "later"
    # The caller lists the folder, then retries.
    t.conn.execute("UPDATE nodes SET listed = 1 WHERE path = 'later'")
    t.add("later/inside.txt")
    assert tr.next_from("z.txt", inclusive=False) == "later/inside.txt"


def test_pruned_unlisted_folders_never_need_listing() -> None:
    t = Tree().add("big/", listed=False).add("x.txt")
    t.decide("big", "delete")
    assert walk_all(trav(t)) == ["x.txt"]


def test_inclusive_and_exclusive() -> None:
    t = Tree().add("a/1.txt", "a/2.txt", "b/3.txt")
    tr = trav(t)
    assert tr.next_from("a/1.txt") == "a/1.txt"
    assert tr.next_from("a/1.txt", inclusive=False) == "a/2.txt"
    assert tr.next_from("a") == "a/1.txt"  # inside the folder
    assert tr.next_from("a", inclusive=False) == "b/3.txt"  # after its subtree
    t.decide("a/1.txt", "keep")
    assert tr.next_from("a/1.txt") == "a/2.txt"


def test_missing_cursor_resumes_from_nearest_ancestor() -> None:
    t = Tree().add("a/b/1.txt", "a/2.txt", "c.txt")
    tr = trav(t)
    assert tr.next_from("a/b/gone/deep.txt") == "a/b/1.txt"
    assert tr.next_from("nowhere/x") == "c.txt"
    assert tr.next_from(None) == "c.txt"


def test_lookahead_stops_at_unlisted() -> None:
    t = Tree().add("1", "2", "3", "d/", listed=False)
    tr = trav(t)
    la = tr.lookahead("1", 5)
    assert la.files == ["2", "3"]
    assert la.needs_listing == "d"


def test_needs_decision() -> None:
    t = Tree().add("a", "b", "._a", "k/x").decide("b", "skip").decide("k", "keep")
    tr = trav(t)
    assert tr.needs_decision("a")
    assert not tr.needs_decision("b")
    assert not tr.needs_decision("._a")
    assert not tr.needs_decision("k/x")
    assert not tr.needs_decision("k")
    assert not tr.needs_decision("missing")


def test_end_of_tree_returns_none() -> None:
    t = Tree().add("only.txt").decide("only.txt", "keep")
    assert trav(t).first() is None
