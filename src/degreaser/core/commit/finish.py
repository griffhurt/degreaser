"""Finishing a commit: report, then drop deleted paths from the project (§3.3)."""

import csv
import io
import json
import sqlite3
from dataclasses import asdict, dataclass, field

from degreaser.core.commit.journal import DELETE_OPS, get_commit, ops, set_status
from degreaser.core.db.connection import tx
from degreaser.core.sources.base import subtree_range


@dataclass(slots=True)
class CommitReport:
    commit_id: int
    status: str
    deletion_mode: str
    files_deleted: int = 0
    folders_deleted: int = 0
    bytes_deleted: int = 0  # freed, or moved to the Degreaser trash
    empty_dirs_removed: int = 0
    skipped_changed: list[str] = field(default_factory=list)
    skipped_other: list[tuple[str, str]] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    catalog_written: bool = False


def _subtree_files(conn: sqlite3.Connection, path: str) -> tuple[int, int]:
    lo, hi = subtree_range(path)
    count, size = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM nodes"
        " WHERE is_dir = 0 AND path >= ? AND path < ?",
        (lo, hi),
    ).fetchone()
    return count, size


def build_report(conn: sqlite3.Connection, commit_id: int, status: str) -> CommitReport:
    """Summarise the journal. Must run before deleted paths are dropped from `nodes`."""
    info = get_commit(conn, commit_id)
    report = CommitReport(commit_id, status, info.deletion_mode)
    for op in ops(conn, commit_id):
        path = op.path or ""
        if op.status == "failed":
            report.failures.append((path, op.detail or ""))
        elif op.status == "skipped":
            if op.detail == "changed":
                report.skipped_changed.append(path)
            else:
                report.skipped_other.append((path, op.detail or ""))
        elif op.status == "done":
            if op.op == "write_catalog":
                report.catalog_written = True
            elif op.op == "rmdir_if_empty":
                report.empty_dirs_removed += 1
            elif op.op in DELETE_OPS:
                node = conn.execute(
                    "SELECT is_dir, size FROM nodes WHERE path = ?", (path,)
                ).fetchone()
                if node is not None and node["is_dir"]:
                    files, size = _subtree_files(conn, path)
                    report.folders_deleted += 1
                    report.files_deleted += files
                    report.bytes_deleted += size
                else:
                    report.files_deleted += 1
                    size = node["size"] if node is not None else op.expect_size
                    report.bytes_deleted += size or 0
    return report


def _forget(conn: sqlite3.Connection, path: str) -> None:
    lo, hi = subtree_range(path)
    conn.execute("DELETE FROM nodes WHERE path = ?", (path,))  # cascades to descendants
    for table in ("decisions", "node_categories"):
        conn.execute(
            f"DELETE FROM {table} WHERE path = ? OR (path >= ? AND path < ?)", (path, lo, hi)
        )


def finish_commit(conn: sqlite3.Connection, commit_id: int, status: str) -> CommitReport:
    """Close a commit as `completed` or `cancelled`.

    Paths whose delete/rmdir ops are done are removed from the tree cache and decision
    state, and undo history is cleared (undoing past a commit is meaningless).
    """
    if status not in ("completed", "cancelled"):
        raise ValueError(status)
    report = build_report(conn, commit_id, status)
    with tx(conn):
        for op in ops(conn, commit_id, "done"):
            if op.path is not None and op.op in (*DELETE_OPS, "rmdir_if_empty"):
                _forget(conn, op.path)
        conn.execute("DELETE FROM history_changes")
        conn.execute("DELETE FROM history")
        info = get_commit(conn, commit_id)
        summary = {**info.summary, "report": asdict(report)}
        conn.execute(
            "UPDATE commits SET summary = ? WHERE id = ?", (json.dumps(summary), commit_id)
        )
        set_status(conn, commit_id, status)
    return report


def report_csv(conn: sqlite3.Connection, commit_id: int) -> str:
    """Every operation of a commit as CSV, for 'Export report'."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["seq", "op", "path", "dest", "status", "detail"])
    for op in ops(conn, commit_id):
        writer.writerow([op.seq, op.op, op.path or "", op.dest or "", op.status, op.detail or ""])
    return out.getvalue()
