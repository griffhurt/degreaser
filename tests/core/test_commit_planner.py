import json
import time
from pathlib import Path

import pytest

from degreaser.core.commit import CommitInProgress, find_unfinished_commit, plan_commit
from degreaser.core.commit.journal import cancel_planned_commit, ops
from degreaser.core.commit.planner import topmost
from degreaser.core.model import DeleteTarget, WalkResult, WalkSummary
from degreaser.core.project import ProjectSettings
from degreaser.core.scan import Rescanner
from tests.core.fake_source import FaultySource, decide, index_all, make_project, no_sleep

SPEC = {
    "a": {"only.txt": "1", "b": {"x.txt": "22", "y.txt": "333"}},
    "keep": {"k.txt": "k", "gone.txt": "g"},
    "junk": {"j1": "j", "j2": {"deep": "d"}},
    "root.txt": "r",
}


def target(project, path: str) -> DeleteTarget:  # type: ignore[no-untyped-def]
    row = project.conn.execute(
        "SELECT is_dir, size, mtime FROM nodes WHERE path = ?", (path,)
    ).fetchone()
    return DeleteTarget(path, bool(row["is_dir"]), row["size"], row["mtime"])


@pytest.fixture
def indexed(tmp_path: Path):  # type: ignore[no-untyped-def]
    project, root = make_project(tmp_path, SPEC)
    src = FaultySource(root)
    index_all(project, src)
    return project, root, src


def walk_of(project, paths: list[str], **summary) -> WalkResult:  # type: ignore[no-untyped-def]
    return WalkResult(
        summary=WalkSummary(**summary),
        delete_targets=[target(project, p) for p in paths],
        catalog_files={"keep/k.txt": ("Docs", "Misc")},
    )


def test_topmost_filters_nested_targets() -> None:
    t = [
        DeleteTarget("a/b", True, None, None),
        DeleteTarget("a/b/c.txt", False, 1, 1.0),
        DeleteTarget("a b.txt", False, 1, 1.0),
        DeleteTarget("a/bc.txt", False, 1, 1.0),
        DeleteTarget("a/b", True, None, None),
    ]
    assert [x.path for x in topmost(t)] == ["a b.txt", "a/b", "a/bc.txt"]


def test_trash_plan_ops_and_empty_dirs(indexed) -> None:  # type: ignore[no-untyped-def]
    project, _, _ = indexed
    walk = walk_of(
        project,
        ["a/only.txt", "a/b", "a/b/x.txt", "junk", "keep/gone.txt"],
        files_known=8,
        files_delete=6,
        bytes_delete=11,
    )
    now = time.mktime((2026, 10, 22, 18, 4, 11, 0, 0, -1))
    plan = plan_commit(project.conn, walk, ProjectSettings(), now=now)

    rows = ops(project.conn, plan.commit_id)
    assert [(o.op, o.path) for o in rows] == [
        ("trash", "a/b"),
        ("trash", "a/only.txt"),
        ("trash", "junk"),
        ("trash", "keep/gone.txt"),
        ("rmdir_if_empty", "a"),  # both children deleted → emptied; "keep" still has k.txt
        ("write_catalog", None),
    ]
    batch = f".degreaser/trash/20261022-180411-{plan.uuid[:8]}"
    assert rows[1].dest == f"{batch}/a/only.txt"
    assert rows[1].expect_size == 1 and rows[1].expect_mtime is not None
    assert rows[0].expect_size is None  # folders aren't size-checked
    assert plan.summary["folders_delete"] == 2 and plan.summary["file_targets"] == 2
    assert plan.summary["files_delete"] == 6 and plan.summary["files_categorised"] == 1
    assert plan.summary["trash_batch"] == batch
    stored = project.conn.execute("SELECT status, summary FROM commits").fetchone()
    assert stored["status"] == "planned" and json.loads(stored["summary"]) == plan.summary


def test_permanent_plan_and_no_empty_dir_removal(indexed) -> None:  # type: ignore[no-untyped-def]
    project, _, _ = indexed
    settings = ProjectSettings(deletion_mode="permanent", remove_empty_dirs=False)
    plan = plan_commit(project.conn, walk_of(project, ["a/b", "a/only.txt"]), settings)
    assert [(o.op, o.path, o.dest) for o in ops(project.conn, plan.commit_id)] == [
        ("delete_tree", "a/b", None),
        ("delete_file", "a/only.txt", None),
        ("write_catalog", None, None),
    ]


def test_nested_empty_dirs_deepest_first(tmp_path: Path) -> None:
    project, root = make_project(tmp_path, {"p": {"q": {"r": {"f.txt": "x"}}, "other": "o"}})
    index_all(project, FaultySource(root))
    plan = plan_commit(project.conn, walk_of(project, ["p/q/r/f.txt"]), ProjectSettings())
    rmdirs = [o.path for o in ops(project.conn, plan.commit_id) if o.op == "rmdir_if_empty"]
    assert rmdirs == ["p/q/r", "p/q"]  # "p" still holds "other"


def test_refuses_root_and_concurrent_plans(indexed) -> None:  # type: ignore[no-untyped-def]
    project, _, _ = indexed
    with pytest.raises(ValueError):
        plan_commit(
            project.conn,
            WalkResult(delete_targets=[DeleteTarget("", True, None, None)]),
            ProjectSettings(),
        )
    plan = plan_commit(project.conn, walk_of(project, ["root.txt"]), ProjectSettings())
    with pytest.raises(CommitInProgress):
        plan_commit(project.conn, walk_of(project, ["root.txt"]), ProjectSettings())
    info = find_unfinished_commit(project.conn)
    assert info is not None and info.id == plan.commit_id and info.ops_done == 0
    cancel_planned_commit(project.conn, plan.commit_id)
    assert find_unfinished_commit(project.conn) is None
    plan_commit(project.conn, walk_of(project, ["root.txt"]), ProjectSettings())


def test_added_since_marked(indexed) -> None:  # type: ignore[no-untyped-def]
    project, root, src = indexed
    decide(project, "junk", "delete", is_dir=True, decided_at=time.time() - 60)
    (root / "junk" / "late1").write_text("l")
    (root / "junk" / "j2" / "late2").write_text("l")
    Rescanner(project.new_connection, src, retriage_changed=True, sleep=no_sleep).run()
    plan = plan_commit(project.conn, walk_of(project, ["junk"]), ProjectSettings())
    assert plan.summary["added_since_marked"] == 2
