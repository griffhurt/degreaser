"""Commit planning: turn a resolver walk into a journal of operations.

The plan is written in one transaction before anything runs (docs/scanning-and-commit.md
§3.1), and the confirmation dialog is built from `CommitPlan.summary`, so what the user
confirms is exactly what will run.
"""

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any

from degreaser.core.commit.journal import CommitInProgress, find_unfinished_commit
from degreaser.core.commit.trash import batch_path
from degreaser.core.db.connection import bump_revision, tx
from degreaser.core.model import DeleteTarget, WalkResult
from degreaser.core.project import ProjectSettings
from degreaser.core.sources.base import ancestors, subtree_range


@dataclass(frozen=True, slots=True)
class CommitPlan:
    commit_id: int
    uuid: str
    summary: dict[str, Any]
    ops_total: int


def topmost(targets: list[DeleteTarget]) -> list[DeleteTarget]:
    """Drop targets that lie inside a folder target; sorted by path."""
    dirs = {t.path for t in targets if t.is_dir}
    seen: set[str] = set()
    out = []
    for t in sorted(targets, key=lambda t: t.path):
        if t.path in seen or any(a in dirs for a in ancestors(t.path)):
            continue
        seen.add(t.path)
        out.append(t)
    return out


def emptied_dirs(conn: sqlite3.Connection, targets: list[DeleteTarget]) -> list[str]:
    """Folders left empty once every target is gone, deepest first. Never the root.

    Only folders whose children are fully known (listed, no listing error) qualify.
    """
    deleted = {t.path for t in targets}
    candidates = {a for t in targets for a in ancestors(t.path) if a != ""}
    emptied: set[str] = set()
    for path in sorted(candidates, key=lambda p: (-p.count("/"), p)):
        row = conn.execute(
            "SELECT id, listed, list_error FROM nodes WHERE path = ? AND is_dir = 1", (path,)
        ).fetchone()
        if row is None or not row["listed"] or row["list_error"] is not None:
            continue
        children = [
            r[0] for r in conn.execute("SELECT path FROM nodes WHERE parent_id = ?", (row["id"],))
        ]
        if children and all(c in deleted or c in emptied for c in children):
            emptied.add(path)
    return sorted(emptied, key=lambda p: (-p.count("/"), p))


def _duplicate_guard(conn: sqlite3.Connection, keep: str) -> str:
    """What must still hold for a duplicate to be deleted: the kept copy exists unchanged
    (size and mtime as hashed) and still has the same content (docs/duplicates.md §5.4)."""
    row = conn.execute("SELECT size, mtime FROM nodes WHERE path = ?", (keep,)).fetchone()
    size, mtime = (row["size"], row["mtime"]) if row is not None else (None, None)
    return json.dumps({"keep": keep, "size": size, "mtime": mtime})


def _added_since_marked(conn: sqlite3.Connection, folder: str) -> int:
    """Files found by a rescan inside a folder after it was marked for deletion."""
    decision = conn.execute(
        "SELECT decided_at FROM decisions WHERE path = ? AND is_dir = 1 AND action = 'delete'",
        (folder,),
    ).fetchone()
    if decision is None:
        return 0
    lo, hi = subtree_range(folder)
    return conn.execute(
        "SELECT COUNT(*) FROM nodes n JOIN scans s ON s.id = n.first_seen_scan"
        " WHERE n.is_dir = 0 AND n.path >= ? AND n.path < ?"
        " AND s.kind = 'rescan' AND s.started_at > ?",
        (lo, hi, decision[0]),
    ).fetchone()[0]


def plan_commit(
    conn: sqlite3.Connection,
    walk: WalkResult,
    settings: ProjectSettings,
    *,
    now: float | None = None,
) -> CommitPlan:
    """Write a planned commit for `walk`. Raises CommitInProgress if one is unfinished."""
    if find_unfinished_commit(conn) is not None:
        raise CommitInProgress("finish or resume the unfinished commit first")
    now = time.time() if now is None else now
    commit_uuid = uuid.uuid4().hex
    mode = settings.deletion_mode
    if mode not in ("trash", "permanent"):
        raise ValueError(f"unknown deletion mode {mode!r}")
    trash_root = batch_path(commit_uuid, now)

    targets = topmost(walk.delete_targets)
    if any(t.path == "" for t in targets):
        raise ValueError("refusing to delete the project root")

    ops: list[tuple[str, str | None, str | None, int | None, float | None, str | None]] = []
    for t in targets:
        guard = _duplicate_guard(conn, t.keep_copy) if t.keep_copy else None
        size, mtime = t.size, t.mtime
        if not t.is_dir and (size is None or mtime is None):
            row = conn.execute("SELECT size, mtime FROM nodes WHERE path = ?", (t.path,)).fetchone()
            if row is not None:
                size, mtime = row["size"], row["mtime"]
        if t.is_dir:
            size = mtime = None
        if mode == "trash":
            ops.append(("trash", t.path, f"{trash_root}/{t.path}", size, mtime, guard))
        else:
            op = "delete_tree" if t.is_dir else "delete_file"
            ops.append((op, t.path, None, size, mtime, guard))

    empties = emptied_dirs(conn, targets) if settings.remove_empty_dirs else []
    ops += [("rmdir_if_empty", p, None, None, None, None) for p in empties]
    ops.append(("write_catalog", None, None, None, None, None))

    dir_targets = [t for t in targets if t.is_dir]
    summary = {
        "files_delete": walk.summary.files_delete,
        "bytes_delete": walk.summary.bytes_delete,
        "folders_delete": len(dir_targets),
        "file_targets": len(targets) - len(dir_targets),
        "files_categorised": len(walk.catalog_files),
        "added_since_marked": sum(_added_since_marked(conn, t.path) for t in dir_targets),
        "empty_dirs": len(empties),
        "deletion_mode": mode,
        "trash_batch": trash_root if mode == "trash" else None,
        "ops_total": len(ops),
    }

    with tx(conn):
        cur = conn.execute(
            "INSERT INTO commits(uuid, kind, created_at, status, deletion_mode, summary)"
            " VALUES (?, 'triage', ?, 'planned', ?, ?)",
            (commit_uuid, now, mode, json.dumps(summary)),
        )
        commit_id = cur.lastrowid
        assert commit_id is not None
        conn.executemany(
            "INSERT INTO commit_ops(commit_id, seq, op, path, dest, expect_size, expect_mtime,"
            " guard, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
            [(commit_id, seq, *op) for seq, op in enumerate(ops)],
        )
        bump_revision(conn)
    return CommitPlan(commit_id, commit_uuid, summary, len(ops))
