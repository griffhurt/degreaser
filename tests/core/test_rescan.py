import shutil
from pathlib import Path

import pytest

from degreaser.core.db.connection import tx
from degreaser.core.scan import CancelToken, Rescanner
from degreaser.core.sources.errors import PermissionDenied
from tests.core.fake_source import (
    FaultySource,
    decide,
    index_all,
    make_project,
    no_sleep,
    node_paths,
    touch,
)

SPEC = {
    "keep.txt": "k",
    "docs": {"a.txt": "a", "b.txt": "b", "sub": {"c.txt": "c"}},
    "old": {"x.txt": "x", "inner": {"y.txt": "y"}},
    "junk": {"j.txt": "j"},
}


def rescan(project, src, *, retriage: bool = True, **kw):  # type: ignore[no-untyped-def]
    return Rescanner(
        project.new_connection, src, retriage_changed=retriage, sleep=no_sleep, **kw
    ).run()


@pytest.fixture
def indexed(tmp_path: Path):  # type: ignore[no-untyped-def]
    project, root = make_project(tmp_path, SPEC)
    src = FaultySource(root)
    index_all(project, src)
    return project, root, src


def test_no_changes(indexed) -> None:  # type: ignore[no-untyped-def]
    project, _, src = indexed
    s = rescan(project, src)
    assert s.status == "completed"
    assert (s.new_files, s.changed_files, s.removed_nodes, s.dirs_done) == (0, 0, 0, s.dirs_total)
    assert node_paths(project, "flag_new = 1 OR flag_changed = 1") == set()


def test_added_files_and_new_folder_subtree(indexed) -> None:  # type: ignore[no-untyped-def]
    project, root, src = indexed
    (root / "docs" / "new.txt").write_text("n")
    (root / "fresh" / "deep").mkdir(parents=True)
    (root / "fresh" / "f1.txt").write_text("1")
    (root / "fresh" / "deep" / "f2.txt").write_text("2")

    s = rescan(project, src)
    assert s.new_files == 3 and s.new_dirs == 2
    assert node_paths(project, "flag_new = 1") == {
        "docs/new.txt",
        "fresh",
        "fresh/deep",
        "fresh/f1.txt",
        "fresh/deep/f2.txt",
    }
    assert node_paths(project, "is_dir = 1 AND listed = 0") == set()
    scan_kind = project.conn.execute(
        "SELECT s.kind FROM nodes n JOIN scans s ON s.id = n.first_seen_scan"
        " WHERE n.path = 'fresh/deep/f2.txt'"
    ).fetchone()[0]
    assert scan_kind == "rescan"


def test_unlisted_folders_are_not_new(tmp_path: Path) -> None:
    project, root = make_project(tmp_path, {"a": {"f": "x"}})
    src = FaultySource(root)
    from degreaser.core.scan import list_dir_into_db

    list_dir_into_db(project.conn, src, "")  # "a" is known but never listed
    (root / "a" / "g").write_text("y")
    s = rescan(project, src)
    assert s.new_files == 0
    assert node_paths(project, "flag_new = 1") == set()


def test_removed_paths_drop_decisions(indexed) -> None:  # type: ignore[no-untyped-def]
    project, root, src = indexed
    decide(project, "docs/a.txt", "keep")
    decide(project, "old", "keep", is_dir=True)
    decide(project, "old/inner/y.txt", "delete")
    decide(project, "keep.txt", "keep")
    with tx(project.conn):
        project.conn.execute("INSERT INTO tags(id, name) VALUES (1, 'T')")
        project.conn.execute("INSERT INTO categories(id, tag_ids) VALUES (1, '1')")
        project.conn.execute("INSERT INTO node_categories VALUES ('old', 1), ('keep.txt', 1)")
    (root / "docs" / "a.txt").unlink()
    shutil.rmtree(root / "old")

    s = rescan(project, src)
    assert s.removed_nodes == 1 + 4  # docs/a.txt + old, old/x.txt, old/inner, old/inner/y.txt
    assert s.dropped_decisions == 3
    assert not any(p.startswith("old") for p in node_paths(project))
    decisions = {r[0] for r in project.conn.execute("SELECT path FROM decisions")}
    assert decisions == {"keep.txt"}
    cats = {r[0] for r in project.conn.execute("SELECT path FROM node_categories")}
    assert cats == {"keep.txt"}


def test_changed_file_retriage_on(indexed) -> None:  # type: ignore[no-untyped-def]
    project, root, src = indexed
    decide(project, "docs/b.txt", "delete")
    decide(project, "docs/sub/c.txt", "keep")
    touch(root / "docs" / "b.txt", b"bigger now")
    touch(root / "keep.txt", b"no decision here")

    s = rescan(project, src, retriage=True)
    assert s.changed_files == 2 and s.retriage_flagged == 1
    assert node_paths(project, "flag_changed = 1") == {"docs/b.txt"}
    size = project.conn.execute("SELECT size FROM nodes WHERE path = 'docs/b.txt'").fetchone()[0]
    assert size == len(b"bigger now")
    seen = project.conn.execute(
        "SELECT seen_size FROM decisions WHERE path = 'docs/b.txt'"
    ).fetchone()[0]
    assert seen == 1  # untouched: the old decision is shown with the CHANGED banner


def test_changed_file_retriage_off_refreshes_seen(indexed) -> None:  # type: ignore[no-untyped-def]
    project, root, src = indexed
    decide(project, "docs/b.txt", "delete")
    touch(root / "docs" / "b.txt", b"bigger now")
    s = rescan(project, src, retriage=False)
    assert s.retriage_flagged == 0
    assert node_paths(project, "flag_changed = 1") == set()
    row = project.conn.execute(
        "SELECT d.seen_size, d.seen_mtime, n.mtime FROM decisions d JOIN nodes n USING (path)"
        " WHERE path = 'docs/b.txt'"
    ).fetchone()
    assert row[0] == len(b"bigger now") and row[1] == row[2]


def test_adds_under_deleted_folder_counted_not_new(indexed) -> None:  # type: ignore[no-untyped-def]
    project, root, src = indexed
    decide(project, "junk", "delete", is_dir=True)
    (root / "junk" / "more.txt").write_text("m")
    (root / "junk" / "newdir").mkdir()
    (root / "junk" / "newdir" / "n.txt").write_text("n")
    s = rescan(project, src)
    assert s.new_files == 0 and s.added_under_deleted == 2
    assert node_paths(project, "flag_new = 1") == set()
    assert {"junk/more.txt", "junk/newdir/n.txt"} <= node_paths(project)


def test_kind_change_is_remove_plus_add(indexed) -> None:  # type: ignore[no-untyped-def]
    project, root, src = indexed
    decide(project, "keep.txt", "keep")
    (root / "keep.txt").unlink()
    (root / "keep.txt").mkdir()
    s = rescan(project, src)
    row = project.conn.execute(
        "SELECT is_dir, flag_new FROM nodes WHERE path = 'keep.txt'"
    ).fetchone()
    assert tuple(row) == (1, 1)
    assert s.dropped_decisions == 1


def test_unlistable_folder_keeps_children(indexed) -> None:  # type: ignore[no-untyped-def]
    project, _, src = indexed
    src.fail[("list_dir", "docs")] = PermissionDenied("nope", "docs")
    s = rescan(project, src)
    assert s.errors == 1 and s.error_paths == ["docs"]
    assert {"docs/a.txt", "docs/b.txt"} <= node_paths(project)
    err = project.conn.execute("SELECT list_error FROM nodes WHERE path = 'docs'").fetchone()[0]
    assert "PermissionDenied" in err


def test_transient_failure_aborts_with_status_failed(indexed) -> None:  # type: ignore[no-untyped-def]
    project, _, src = indexed
    src.transient[("list_dir", "docs")] = 99
    s = rescan(project, src, retry_attempts=2)
    assert s.status == "failed" and "docs" in (s.error or "")
    assert {"docs/a.txt", "docs/b.txt"} <= node_paths(project)


def test_cancel_keeps_finished_results(indexed) -> None:  # type: ignore[no-untyped-def]
    project, root, src = indexed
    (root / "new_at_root.txt").write_text("n")
    cancel = CancelToken()
    s = Rescanner(
        project.new_connection,
        src,
        retriage_changed=True,
        workers=1,
        batch_dirs=1,
        on_progress=lambda p: cancel.cancel(),
        sleep=no_sleep,
    ).run(cancel)
    assert s.status == "cancelled"
    assert 0 < s.dirs_done < 6
    status = project.conn.execute("SELECT status FROM scans WHERE id = ?", (s.scan_id,)).fetchone()[
        0
    ]
    assert status == "cancelled"
