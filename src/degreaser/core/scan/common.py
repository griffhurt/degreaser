"""Helpers shared by the indexer and the rescanner."""

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable

from degreaser.core.project import DEGREASER_DIR
from degreaser.core.sortkey import sort_key
from degreaser.core.sources.base import Entry, join
from degreaser.core.sources.errors import TransientSourceError

log = logging.getLogger(__name__)

INSERT_NODE = (
    "INSERT OR IGNORE INTO nodes(parent_id, path, name, is_dir, kind, size, mtime, ctime,"
    " sort_key, listed, first_seen_scan, flag_new) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)"
)


class CancelToken:
    """Thread-safe cancellation flag shared between a job and whoever started it."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


def usable_entries(dir_path: str, entries: Iterable[Entry]) -> list[Entry]:
    """Drop names we can't store and Degreaser's own folder at the project root."""
    out = []
    for e in entries:
        if e.name in ("", ".", "..") or "\0" in e.name or "/" in e.name:
            log.warning("skipping unusable name %r in %r", e.name, dir_path)
            continue
        if dir_path == "" and e.name == DEGREASER_DIR:
            continue
        out.append(e)
    return out


def insert_entries(
    conn: sqlite3.Connection,
    parent_id: int,
    dir_path: str,
    entries: Iterable[Entry],
    scan_id: int | None,
    flag_new: bool,
) -> int:
    """Insert children of a folder (dirs unlisted). Returns rows inserted."""
    rows = [
        (
            parent_id,
            join(dir_path, e.name),
            e.name,
            int(e.is_dir),
            e.kind,
            None if e.is_dir else e.size,
            e.mtime,
            e.ctime,
            sort_key(e.name),
            scan_id,
            int(flag_new),
        )
        for e in entries
    ]
    before = conn.total_changes
    conn.executemany(INSERT_NODE, rows)
    return conn.total_changes - before


def call_with_retry[T](
    fn: Callable[[], T],
    *,
    attempts: int,
    sleep: Callable[[float], None],
    cancel: CancelToken | None = None,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
) -> T:
    """Call fn, retrying TransientSourceError with exponential backoff."""
    delay = base_delay
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except TransientSourceError:
            if attempt == attempts or (cancel is not None and cancel.cancelled):
                raise
            sleep(delay)
            delay = min(max_delay, delay * 2)
    raise AssertionError("unreachable")


def start_scan(conn: sqlite3.Connection, kind: str, dirs_total: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO scans(kind, started_at, status, dirs_done, dirs_total)"
        " VALUES (?, ?, 'running', 0, ?)",
        (kind, time.time(), dirs_total),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def finish_scan(
    conn: sqlite3.Connection, scan_id: int, status: str, dirs_done: int, dirs_total: int
) -> None:
    conn.execute(
        "UPDATE scans SET status = ?, finished_at = ?, dirs_done = ?, dirs_total = ? WHERE id = ?",
        (status, time.time(), dirs_done, dirs_total, scan_id),
    )


def deleted_folder_marks(conn: sqlite3.Connection, paths: list[str]) -> bool:
    """True if any of these folder paths carries a `delete` decision."""
    if not paths:
        return False
    marks = ",".join("?" * len(paths))
    row = conn.execute(
        f"SELECT 1 FROM decisions WHERE is_dir = 1 AND action = 'delete' AND path IN ({marks})"
        " LIMIT 1",
        paths,
    ).fetchone()
    return row is not None
