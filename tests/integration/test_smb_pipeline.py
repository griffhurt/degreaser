"""The core pipeline (index → rescan → commit → catalog) running unchanged over SmbSource."""

import json
from collections.abc import Iterator

import pytest

from degreaser.core import credentials
from degreaser.core.commit import CATALOG_PATH, CommitRunner, plan_commit
from degreaser.core.model import DeleteTarget, WalkResult
from degreaser.core.project import Project, ProjectSettings
from degreaser.core.scan import Indexer, Rescanner
from degreaser.core.sources import smb as smb_module
from degreaser.core.sources.smb import SmbSource
from tests.integration.conftest import SmbTestServer

pytestmark = pytest.mark.smb


@pytest.fixture
def project(smb_server: SmbTestServer, smb_workdir: str) -> Iterator[tuple[Project, SmbSource]]:
    source_config = {
        "type": "smb",
        "host": smb_server.host,
        "port": smb_server.port,
        "share": "data",
        "path": smb_workdir.rsplit("\\", 1)[1],
        "username": smb_server.username,
    }
    project = Project.create("SMB", source_config)
    credentials.set_password(project.uuid, smb_server.password)
    source = project.make_source()
    assert isinstance(source, SmbSource)
    source.connect()
    for rel, data in {
        "keep/k.txt": b"k",
        "keep/gone.txt": b"gone",
        "junk/j1": b"j",
        "junk/j2/deep": b"d",
        "root.txt": b"r",
    }.items():
        source.makedirs(rel.rpartition("/")[0])
        source.write_file_atomic(rel, data)
    yield project, source
    source.close()
    project.close()


def _paths(project: Project) -> set[str]:
    return {r[0] for r in project.conn.execute("SELECT path FROM nodes")}


def test_index_rescan_commit_over_smb(project: tuple[Project, SmbSource]) -> None:
    proj, source = project
    assert Indexer(proj.new_connection, source).run().status == "completed"
    assert _paths(proj) == {
        "",
        "keep",
        "keep/k.txt",
        "keep/gone.txt",
        "junk",
        "junk/j1",
        "junk/j2",
        "junk/j2/deep",
        "root.txt",
    }

    source.write_file_atomic("keep/new.txt", b"new")
    summary = Rescanner(proj.new_connection, source, retriage_changed=True).run()
    assert summary.new_files == 1

    targets = []
    for path in ("junk", "keep/gone.txt"):
        row = proj.conn.execute(
            "SELECT is_dir, size, mtime FROM nodes WHERE path = ?", (path,)
        ).fetchone()
        targets.append(DeleteTarget(path, bool(row["is_dir"]), row["size"], row["mtime"]))
    plan = plan_commit(proj.conn, WalkResult(delete_targets=targets), ProjectSettings())
    outcome = CommitRunner(
        proj.conn,
        source,
        plan.commit_id,
        project_name="SMB",
        catalog_provider=lambda: ({"keep/k.txt": ("Docs",)}, {}),
    ).run()

    assert outcome.status == "completed"
    assert outcome.report is not None and not outcome.report.failures
    names = {e.name for e in source.list_dir("")}
    assert names == {"keep", "root.txt", ".degreaser"}
    batch = plan.summary["trash_batch"]
    assert source.read_range(f"{batch}/keep/gone.txt", 0, 10) == b"gone"
    assert source.stat(f"{batch}/junk/j2/deep").size == 1
    catalog = json.loads(source.read_range(CATALOG_PATH, 0, 1 << 20))
    assert catalog["files"] == {"keep/k.txt": ["Docs"]}


def test_missing_password_is_reported(smb_server: SmbTestServer) -> None:
    from degreaser.core.sources import AuthenticationFailed

    proj = Project.create(
        "No password",
        {"type": "smb", "host": smb_server.host, "share": "data", "username": "x"},
    )
    with pytest.raises(AuthenticationFailed):
        proj.make_source()
    proj.close()


@pytest.mark.parametrize("in_flight", [False, True])
def test_commit_survives_a_dropped_connection(
    project: tuple[Project, SmbSource], monkeypatch: pytest.MonkeyPatch, in_flight: bool
) -> None:
    """The connection dies at a rename. Dropped between requests, the source reconnects
    before sending, invisibly. Dropped while the rename is in flight, the runner pauses and
    retries the (idempotent) op on a fresh connection."""
    proj, source = project
    assert Indexer(proj.new_connection, source).run().status == "completed"
    targets = []
    for path in ("keep/gone.txt", "junk/j1", "root.txt"):
        row = proj.conn.execute("SELECT size, mtime FROM nodes WHERE path = ?", (path,)).fetchone()
        targets.append(DeleteTarget(path, False, row["size"], row["mtime"]))
    plan = plan_commit(
        proj.conn, WalkResult(delete_targets=targets), ProjectSettings(remove_empty_dirs=False)
    )

    original_rename, original_compound = source.rename, SmbSource._compound
    drops = {"left": 1}

    def lost_in_flight(link, messages, tolerate=()):  # type: ignore[no-untyped-def]
        original_compound(link, messages, tolerate)  # the server did the rename...
        link.connection.disconnect(close=False)
        raise smb_module._ConnectionLost("connection reset")  # ...but the reply never came

    def flaky_rename(src: str, dst: str) -> None:
        if src == "junk/j1" and drops["left"]:
            drops["left"] -= 1
            if in_flight:
                monkeypatch.setattr(SmbSource, "_compound", staticmethod(lost_in_flight))
                try:
                    return original_rename(src, dst)
                finally:
                    monkeypatch.setattr(SmbSource, "_compound", staticmethod(original_compound))
            link = source._link
            assert link is not None
            link.connection.disconnect(close=False)
        original_rename(src, dst)

    source.rename = flaky_rename  # type: ignore[method-assign]
    retries: list[object] = []
    outcome = CommitRunner(
        proj.conn,
        source,
        plan.commit_id,
        project_name="SMB",
        catalog_provider=lambda: ({}, {}),
        on_retry=retries.append,
        wait=lambda seconds, control: None,
    ).run()

    assert outcome.status == "completed" and outcome.report is not None
    assert not outcome.report.failures and not outcome.report.skipped_other
    assert len(retries) == (1 if in_flight else 0)
    batch = plan.summary["trash_batch"]
    for path in ("keep/gone.txt", "junk/j1", "root.txt"):
        assert source.stat(f"{batch}/{path}").kind == "file"
