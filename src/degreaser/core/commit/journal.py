"""Reading and managing the commit journal (`commits` / `commit_ops`)."""

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

UNFINISHED = ("planned", "running", "paused")
DELETE_OPS = ("trash", "delete_file", "delete_tree")


class CommitInProgress(Exception):
    """Another commit is planned/running/paused and must be resumed first."""


class CommitAlreadyStarted(Exception):
    """A plan can't be cancelled once any of its operations have run."""


@dataclass(frozen=True, slots=True)
class CommitInfo:
    id: int
    uuid: str
    kind: str
    status: str
    deletion_mode: str
    created_at: float
    finished_at: float | None
    summary: dict[str, Any]
    ops_total: int
    ops_done: int  # finished in any way: done, skipped or failed
    ops_failed: int


@dataclass(frozen=True, slots=True)
class OpRow:
    id: int
    seq: int
    op: str
    path: str | None
    dest: str | None
    expect_size: int | None
    expect_mtime: float | None
    status: str
    detail: str | None


def _info(conn: sqlite3.Connection, row: sqlite3.Row) -> CommitInfo:
    counts = dict(
        conn.execute(
            "SELECT status, COUNT(*) FROM commit_ops WHERE commit_id = ? GROUP BY status",
            (row["id"],),
        ).fetchall()
    )
    total = sum(counts.values())
    return CommitInfo(
        id=row["id"],
        uuid=row["uuid"],
        kind=row["kind"],
        status=row["status"],
        deletion_mode=row["deletion_mode"],
        created_at=row["created_at"],
        finished_at=row["finished_at"],
        summary=json.loads(row["summary"] or "{}"),
        ops_total=total,
        ops_done=total - counts.get("pending", 0),
        ops_failed=counts.get("failed", 0),
    )


def get_commit(conn: sqlite3.Connection, commit_id: int) -> CommitInfo:
    row = conn.execute("SELECT * FROM commits WHERE id = ?", (commit_id,)).fetchone()
    if row is None:
        raise KeyError(commit_id)
    return _info(conn, row)


def find_unfinished_commit(conn: sqlite3.Connection) -> CommitInfo | None:
    """The commit to offer for resuming when the project opens, if any."""
    row = conn.execute(
        f"SELECT * FROM commits WHERE status IN ({','.join('?' * len(UNFINISHED))})"
        " ORDER BY id DESC LIMIT 1",
        UNFINISHED,
    ).fetchone()
    return None if row is None else _info(conn, row)


def ops(conn: sqlite3.Connection, commit_id: int, status: str | None = None) -> list[OpRow]:
    sql = "SELECT * FROM commit_ops WHERE commit_id = ?"
    args: tuple[Any, ...] = (commit_id,)
    if status is not None:
        sql += " AND status = ?"
        args += (status,)
    return [
        OpRow(
            r["id"],
            r["seq"],
            r["op"],
            r["path"],
            r["dest"],
            r["expect_size"],
            r["expect_mtime"],
            r["status"],
            r["detail"],
        )
        for r in conn.execute(sql + " ORDER BY seq", args)
    ]


def set_status(conn: sqlite3.Connection, commit_id: int, status: str) -> None:
    finished = time.time() if status in ("completed", "cancelled") else None
    conn.execute(
        "UPDATE commits SET status = ?, finished_at = ? WHERE id = ?",
        (status, finished, commit_id),
    )


def cancel_planned_commit(conn: sqlite3.Connection, commit_id: int) -> None:
    """Abandon a plan before any operation has run."""
    info = get_commit(conn, commit_id)
    if info.ops_done:
        raise CommitAlreadyStarted(f"{info.ops_done} operations have already run")
    set_status(conn, commit_id, "cancelled")
