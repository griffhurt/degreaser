import os
import sys
import threading
from pathlib import Path

import pytest

from degreaser.core.scan import CancelToken, Indexer, list_dir_into_db
from degreaser.core.sortkey import sort_key
from degreaser.core.sources.errors import PermissionDenied
from tests.core.fake_source import FaultySource, index_all, make_project, no_sleep, node_paths

SPEC = {
    "a.txt": "hello",
    "Photos 2010": {"IMG_2.jpg": b"x" * 10, "IMG_10.jpg": b"x" * 20},
    "Photos": {"deep": {"deeper": {"z.bin": b"\0" * 5}}},
    "naïve café": {"日本語.txt": "u"},
    "empty": {},
    ".degreaser": {"catalog.json": "{}"},
}


def test_full_index(tmp_path: Path) -> None:
    project, root = make_project(tmp_path, SPEC)
    index_all(project, FaultySource(root))

    paths = node_paths(project)
    assert paths == {
        "",
        "a.txt",
        "Photos 2010",
        "Photos 2010/IMG_2.jpg",
        "Photos 2010/IMG_10.jpg",
        "Photos",
        "Photos/deep",
        "Photos/deep/deeper",
        "Photos/deep/deeper/z.bin",
        "naïve café",
        "naïve café/日本語.txt",
        "empty",
    }  # .degreaser at the root is never indexed
    assert node_paths(project, "is_dir = 1 AND listed = 0") == set()

    row = project.conn.execute(
        "SELECT n.size, n.sort_key, n.kind, p.path AS parent, n.first_seen_scan, n.flag_new"
        " FROM nodes n JOIN nodes p ON p.id = n.parent_id WHERE n.path = 'Photos 2010/IMG_10.jpg'"
    ).fetchone()
    assert row["size"] == 20 and row["kind"] == "file" and row["parent"] == "Photos 2010"
    assert row["sort_key"] == sort_key("IMG_10.jpg") and row["flag_new"] == 0
    scan = project.conn.execute("SELECT kind, status FROM scans").fetchone()
    assert tuple(scan) == ("index", "completed")
    assert row["first_seen_scan"] is not None

    rollup = {
        r["path"]: (r["subtree_files"], r["subtree_bytes"])
        for r in project.conn.execute(
            "SELECT path, subtree_files, subtree_bytes FROM nodes WHERE is_dir = 1"
        )
    }
    assert rollup[""] == (5, 5 + 10 + 20 + 5 + 1)
    assert rollup["Photos 2010"] == (2, 30)
    assert rollup["empty"] == (0, 0)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlink_indexed_not_followed(tmp_path: Path) -> None:
    project, root = make_project(tmp_path, {"real": {"f.txt": "x"}})
    os.symlink(root / "real", root / "link")
    index_all(project, FaultySource(root))
    row = project.conn.execute("SELECT kind, is_dir FROM nodes WHERE path = 'link'").fetchone()
    assert tuple(row) == ("symlink", 0)
    assert "link/f.txt" not in node_paths(project)


def test_permission_error_marks_folder(tmp_path: Path) -> None:
    project, root = make_project(tmp_path, {"locked": {"x.txt": "x"}, "ok": {"y.txt": "y"}})
    src = FaultySource(root)
    src.fail[("list_dir", "locked")] = PermissionDenied("nope", "locked")
    index_all(project, src)
    row = project.conn.execute(
        "SELECT listed, list_error FROM nodes WHERE path = 'locked'"
    ).fetchone()
    assert row["listed"] == 1 and "PermissionDenied" in row["list_error"]
    assert "ok/y.txt" in node_paths(project)


def test_transient_errors_retry_then_succeed(tmp_path: Path) -> None:
    project, root = make_project(tmp_path, {"d": {"f": "x"}})
    src = FaultySource(root)
    src.transient[("list_dir", "d")] = 2
    sleeps: list[float] = []
    summary = Indexer(project.new_connection, src, sleep=sleeps.append).run()
    assert summary.status == "completed"
    assert sleeps == [1.0, 2.0]
    assert "d/f" in node_paths(project)


def test_transient_errors_exhausted_fail_and_resume_later(tmp_path: Path) -> None:
    project, root = make_project(tmp_path, {"d": {"f": "x"}, "e": {"g": "y"}})
    src = FaultySource(root)
    src.transient[("list_dir", "d")] = 99
    summary = Indexer(project.new_connection, src, retry_attempts=3, sleep=no_sleep).run()
    assert summary.status == "failed" and "d" in (summary.error or "")
    assert node_paths(project, "is_dir = 1 AND listed = 0") == {"d"}

    src.transient.clear()
    assert Indexer(project.new_connection, src, sleep=no_sleep).run().status == "completed"
    assert "d/f" in node_paths(project)


def test_cancel_leaves_resumable_state(tmp_path: Path) -> None:
    project, root = make_project(
        tmp_path, {f"d{i}": {f"s{j}": {"f": "x"} for j in range(5)} for i in range(5)}
    )
    cancel = CancelToken()
    summary = Indexer(
        project.new_connection,
        FaultySource(root),
        workers=1,
        batch_rows=1,
        on_progress=lambda p: cancel.cancel(),
    ).run(cancel)
    assert summary.status == "cancelled"
    assert node_paths(project, "is_dir = 1 AND listed = 0")
    assert Indexer(project.new_connection, FaultySource(root)).run().status == "completed"
    assert len(node_paths(project, "is_dir = 0")) == 25


def test_on_demand_listing_is_idempotent(tmp_path: Path) -> None:
    project, root = make_project(tmp_path, {"d": {"f": "x", "s": {}}})
    src = FaultySource(root)
    first = list_dir_into_db(project.conn, src, "")
    assert first.inserted == 1 and not first.already_listed
    assert list_dir_into_db(project.conn, src, "").already_listed
    out = list_dir_into_db(project.conn, src, "d")
    assert out.inserted == 2
    with pytest.raises(KeyError):
        list_dir_into_db(project.conn, src, "nope")
    # The background indexer picks up from there without duplicating anything.
    index_all(project, src)
    assert node_paths(project) == {"", "d", "d/f", "d/s"}


def test_priority_request_resolves(tmp_path: Path) -> None:
    spec = {f"bulk{i}": {"f": "x"} for i in range(30)} | {"target": {"t1": "a", "t2": "b"}}
    project, root = make_project(tmp_path, spec)
    list_dir_into_db(project.conn, FaultySource(root), "")
    indexer = Indexer(project.new_connection, FaultySource(root), workers=1)
    future = indexer.request("target")
    missing = indexer.request("does/not/exist")
    thread = threading.Thread(target=indexer.run)
    thread.start()
    outcome = future.result(timeout=10)
    thread.join(timeout=10)
    assert outcome.path == "target"
    assert {"target/t1", "target/t2"} <= node_paths(project)
    with pytest.raises(KeyError):
        missing.result(timeout=1)


def test_concurrent_on_demand_and_background(tmp_path: Path) -> None:
    spec = {f"d{i}": {f"f{j}": "x" for j in range(10)} for i in range(40)}
    project, root = make_project(tmp_path, spec)
    src = FaultySource(root)
    list_dir_into_db(project.conn, src, "")
    thread = threading.Thread(target=Indexer(project.new_connection, src, workers=4).run)
    thread.start()
    for i in range(40):
        list_dir_into_db(project.conn, src, f"d{i}")
    thread.join(timeout=30)
    count = project.conn.execute("SELECT COUNT(*) FROM nodes WHERE is_dir = 0").fetchone()[0]
    assert count == 400


def test_progress_reports_overall_fraction(tmp_path: Path) -> None:
    from degreaser.core.scan import IndexProgress

    project, root = make_project(tmp_path, {"a": {"b": {"c": {"x.txt": "1"}}}, "d": {"y": "2"}})
    seen: list[IndexProgress] = []
    Indexer(project.new_connection, FaultySource(root), workers=1, on_progress=seen.append).run()
    last = seen[-1]
    assert last.dirs_done == last.dirs_known == 5  # root, a, a/b, a/b/c, d
    assert last.fraction == 1.0
    assert all(0 <= p.fraction <= 1 for p in seen)
