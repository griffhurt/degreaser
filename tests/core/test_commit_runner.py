import json
from pathlib import Path

import pytest

from degreaser.core.commit import (
    CATALOG_PATH,
    CommitRunner,
    RunControl,
    find_unfinished_commit,
    get_commit,
    plan_commit,
    report_csv,
)
from degreaser.core.commit.journal import ops
from degreaser.core.db.connection import tx
from degreaser.core.model import DeleteTarget, WalkResult
from degreaser.core.project import ProjectSettings
from degreaser.core.sources.errors import PermissionDenied
from tests.core.fake_source import (
    FaultySource,
    SimulatedCrash,
    decide,
    index_all,
    make_project,
    node_paths,
    touch,
)

SPEC = {
    "a": {"only.txt": "1", "b": {"x.txt": "22", "y.txt": "333"}},
    "keep": {"k.txt": "k", "gone.txt": "g"},
    "junk": {"j1": "j", "j2": {"deep": "d"}},
    "root.txt": "r",
}
TARGETS = ["a/only.txt", "a/b", "junk", "keep/gone.txt"]
CATALOG = ({"keep/k.txt": ("Docs", "Misc")}, {"keep": ("Docs",)})


@pytest.fixture
def setup(tmp_path: Path):  # type: ignore[no-untyped-def]
    project, root = make_project(tmp_path, SPEC)
    src = FaultySource(root)
    index_all(project, src)
    return project, root, src


def plan(project, paths=TARGETS, **settings):  # type: ignore[no-untyped-def]
    targets = []
    for p in paths:
        row = project.conn.execute(
            "SELECT is_dir, size, mtime FROM nodes WHERE path = ?", (p,)
        ).fetchone()
        targets.append(DeleteTarget(p, bool(row["is_dir"]), row["size"], row["mtime"]))
    return plan_commit(
        project.conn, WalkResult(delete_targets=targets), ProjectSettings(**settings)
    )


def runner(project, src, commit_id, **kw):  # type: ignore[no-untyped-def]
    kw.setdefault("wait", lambda seconds, control: None)
    return CommitRunner(
        project.conn, src, commit_id, project_name="Test", catalog_provider=lambda: CATALOG, **kw
    )


def test_trash_commit_end_to_end(setup) -> None:  # type: ignore[no-untyped-def]
    project, root, src = setup
    decide(project, "a/only.txt", "delete")
    decide(project, "junk", "delete", is_dir=True)
    decide(project, "junk/j1", "keep")  # overridden by the folder mark; dropped too
    with tx(project.conn):
        project.conn.execute(
            "INSERT INTO history(created_at, label, state, change_count) VALUES (0, 'x', 'done', 0)"
        )
    p = plan(project)
    progress = []
    outcome = runner(project, src, p.commit_id, on_progress=progress.append).run()

    assert outcome.status == "completed"
    batch = root / p.summary["trash_batch"]
    assert (batch / "a" / "only.txt").read_text() == "1"
    assert (batch / "a" / "b" / "y.txt").read_text() == "333"
    assert (batch / "junk" / "j2" / "deep").exists()
    assert not (root / "a").exists()  # left empty → removed
    assert (root / "keep" / "k.txt").exists() and not (root / "keep" / "gone.txt").exists()

    report = outcome.report
    assert report is not None
    assert (report.files_deleted, report.folders_deleted) == (2 + 3 + 1, 2)
    assert report.bytes_deleted == 1 + 2 + 3 + 1 + 1 + 1
    assert report.empty_dirs_removed == 1 and report.catalog_written and not report.failures

    assert node_paths(project) == {"", "keep", "keep/k.txt", "root.txt"}
    assert project.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    assert project.conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 0
    assert get_commit(project.conn, p.commit_id).status == "completed"
    assert find_unfinished_commit(project.conn) is None
    assert progress[-1].done == progress[-1].total == p.ops_total

    catalog = json.loads((root / CATALOG_PATH).read_text())
    assert catalog["files"] == {"keep/k.txt": ["Docs", "Misc"]}
    assert catalog["folders"] == {"keep": ["Docs"]}
    assert "trash,a/b" in report_csv(project.conn, p.commit_id).replace("\r", "")


def test_permanent_commit(setup) -> None:  # type: ignore[no-untyped-def]
    project, root, src = setup
    p = plan(project, deletion_mode="permanent")
    assert runner(project, src, p.commit_id).run().status == "completed"
    assert not (root / "junk").exists() and not (root / ".degreaser" / "trash").exists()
    assert (root / "keep" / "k.txt").exists()


def test_changed_file_is_skipped_and_flagged(setup) -> None:  # type: ignore[no-untyped-def]
    project, root, src = setup
    p = plan(project)
    touch(root / "keep" / "gone.txt", b"edited after the scan")
    outcome = runner(project, src, p.commit_id).run()
    assert outcome.report is not None and outcome.report.skipped_changed == ["keep/gone.txt"]
    assert (root / "keep" / "gone.txt").exists()
    flag = project.conn.execute(
        "SELECT flag_changed FROM nodes WHERE path = 'keep/gone.txt'"
    ).fetchone()[0]
    assert flag == 1


def test_crash_and_resume_is_idempotent(setup) -> None:  # type: ignore[no-untyped-def]
    project, root, src = setup
    p = plan(project)
    src.crash_after_mutations = 2  # two renames succeed, then the "app crashes"
    with pytest.raises(SimulatedCrash):
        runner(project, src, p.commit_id).run()
    info = find_unfinished_commit(project.conn)
    assert info is not None and info.status == "running" and info.ops_done == 2

    # The rename for op 3 might also have happened just before the crash: simulate that.
    pending = ops(project.conn, p.commit_id, "pending")
    src.inner.makedirs(pending[0].dest.rpartition("/")[0])  # type: ignore[union-attr]
    src.inner.rename(pending[0].path, pending[0].dest)  # type: ignore[arg-type]

    src.crash_after_mutations = None
    outcome = runner(project, src, p.commit_id).run()
    assert outcome.status == "completed" and outcome.report is not None
    assert not outcome.report.failures
    statuses = {o.path: (o.status, o.detail) for o in ops(project.conn, p.commit_id)}
    assert statuses[pending[0].path] == ("done", "already in trash")
    assert not (root / "junk").exists() and not (root / "a").exists()


def test_transient_error_retries_with_backoff(setup) -> None:  # type: ignore[no-untyped-def]
    project, root, src = setup
    p = plan(project)
    src.transient[("rename", "junk")] = 3
    waits, retries = [], []
    outcome = runner(
        project,
        src,
        p.commit_id,
        wait=lambda seconds, control: waits.append(seconds),
        on_retry=retries.append,
    ).run()
    assert outcome.status == "completed"
    assert waits == [1.0, 2.0, 4.0]
    assert [r.attempt for r in retries] == [1, 2, 3] and retries[0].op.path == "junk"
    assert not (root / "junk").exists()


def test_stop_retrying_leaves_paused_then_resumes(setup) -> None:  # type: ignore[no-untyped-def]
    project, root, src = setup
    p = plan(project)
    src.transient[("rename", "junk")] = 1
    control = RunControl()
    outcome = runner(project, src, p.commit_id, wait=lambda seconds, c: c.stop_retrying()).run(
        control
    )
    assert outcome.status == "paused"
    assert get_commit(project.conn, p.commit_id).status == "paused"
    assert (root / "junk").exists()
    assert runner(project, src, p.commit_id).run().status == "completed"
    assert not (root / "junk").exists()


def test_pause_and_cancel(setup) -> None:  # type: ignore[no-untyped-def]
    project, root, src = setup
    p = plan(project)
    control = RunControl()
    seen = []

    def on_progress(prog):  # type: ignore[no-untyped-def]
        seen.append(prog)
        if prog.done == 1:
            control.pause()

    assert (
        runner(project, src, p.commit_id, on_progress=on_progress).run(control).status == "paused"
    )
    assert get_commit(project.conn, p.commit_id).ops_done == 1

    control = RunControl()
    control.cancel()
    outcome = runner(project, src, p.commit_id).run(control)
    assert outcome.status == "cancelled"
    info = get_commit(project.conn, p.commit_id)
    assert info.status == "cancelled" and info.ops_done == 1
    # The one op that ran was applied and its path forgotten; the rest are untouched.
    assert "a/b" not in node_paths(project)
    assert (root / "junk").exists() and "junk" in node_paths(project)


def test_permanent_errors_fail_op_and_continue(setup) -> None:  # type: ignore[no-untyped-def]
    project, root, src = setup
    p = plan(project, deletion_mode="permanent")
    src.fail[("delete_tree", "junk")] = PermissionDenied("denied", "junk")
    outcome = runner(project, src, p.commit_id).run()
    assert outcome.status == "completed" and outcome.report is not None
    assert outcome.report.failures and outcome.report.failures[0][0] == "junk"
    assert (root / "junk").exists() and "junk" in node_paths(project)
    assert not (root / "keep" / "gone.txt").exists()


def test_already_deleted_counts_as_done(setup) -> None:  # type: ignore[no-untyped-def]
    project, root, src = setup
    p = plan(project, deletion_mode="permanent")
    (root / "root.txt").unlink()
    (root / "keep" / "gone.txt").unlink()
    outcome = runner(project, src, p.commit_id).run()
    statuses = {o.path: (o.status, o.detail) for o in ops(project.conn, p.commit_id)}
    assert statuses["keep/gone.txt"] == ("done", "already gone")
    assert outcome.report is not None and not outcome.report.failures


def test_rmdir_skips_folder_that_gained_files(setup) -> None:  # type: ignore[no-untyped-def]
    project, root, src = setup
    p = plan(project)
    (root / "a" / "surprise.txt").write_text("s")
    runner(project, src, p.commit_id).run()
    statuses = {o.path: (o.status, o.detail) for o in ops(project.conn, p.commit_id)}
    assert statuses["a"] == ("skipped", "not empty")
    assert (root / "a" / "surprise.txt").exists()


def test_cross_device_failure_explains(setup) -> None:  # type: ignore[no-untyped-def]
    import errno

    project, _, src = setup
    p = plan(project)
    err = OSError(errno.EXDEV, "Invalid cross-device link")
    from degreaser.core.sources.errors import SourceError

    wrapped = SourceError(str(err), "junk")
    wrapped.__cause__ = err
    src.fail[("rename", "junk")] = wrapped
    outcome = runner(project, src, p.commit_id).run()
    assert outcome.report is not None
    [(path, detail)] = outcome.report.failures
    assert path == "junk" and "Delete permanently" in detail


def test_rollups_shrink_after_commit(setup) -> None:  # type: ignore[no-untyped-def]
    project, _root, src = setup
    conn = project.conn
    from degreaser.core.scan import compute_rollups

    compute_rollups(conn)
    p = plan(project, ["a/b/x.txt", "junk"])
    assert runner(project, src, p.commit_id).run().status == "completed"

    def rollups() -> dict[str, tuple[int, int]]:
        return {
            r["path"]: (r["subtree_files"], r["subtree_bytes"])
            for r in conn.execute(
                "SELECT path, subtree_files, subtree_bytes FROM nodes WHERE is_dir"
            )
        }

    after = rollups()
    compute_rollups(conn)  # a fresh bottom-up count must agree with the incremental update
    assert after == rollups()
    assert after[""] == (5, 1 + 3 + 1 + 1 + 1)  # only.txt, y.txt, k.txt, gone.txt, root.txt
    assert after["a/b"] == (1, 3)
