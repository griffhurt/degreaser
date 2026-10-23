"""Startup rescan: detect added, removed and changed nodes (docs/scanning-and-commit.md §2).

Every folder that was already listed is listed again and diffed against the cache:

- **Added** children are inserted with `flag_new = 1`. A newly added folder is listed by
  this same rescan (and so on recursively), so the files inside a new folder are new too.
  Folders that were never listed (an incomplete index) are left to the indexer: their
  contents are unknown rather than new.
- Children added anywhere under a folder marked `delete` are *not* flagged new; they're
  counted in `added_under_deleted` (they'll be deleted with the folder).
- **Removed** children are deleted from `nodes` (cascading to descendants), along with
  every decision and category at or under their path.
- **Changed** files (size or mtime) are updated. If the file has a decision whose
  `seen_size`/`seen_mtime` no longer match: with re-triage on it gets `flag_changed = 1`;
  with re-triage off its `seen_*` values are silently refreshed.

A folder that can't be listed keeps its cached children (and gets `list_error`); a folder
that has vanished is left for its parent's diff to remove.
"""

import logging
import sqlite3
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from degreaser.core.db.connection import tx
from degreaser.core.scan.common import (
    CancelToken,
    call_with_retry,
    deleted_folder_marks,
    finish_scan,
    insert_entries,
    start_scan,
    usable_entries,
)
from degreaser.core.scan.indexer import compute_rollups, index_complete
from degreaser.core.sortkey import sort_key
from degreaser.core.sources.base import Entry, FileSource, ancestors, join, subtree_range
from degreaser.core.sources.errors import NotFound, SourceError, TransientSourceError

log = logging.getLogger(__name__)

# Filesystems report mtimes at different precisions; never call float noise a change.
MTIME_TOLERANCE = 1e-6


@dataclass(frozen=True, slots=True)
class RescanProgress:
    dirs_done: int
    dirs_total: int


@dataclass(slots=True)
class RescanSummary:
    status: str = "running"  # completed | cancelled | failed
    scan_id: int = 0
    dirs_done: int = 0
    dirs_total: int = 0
    new_files: int = 0
    new_dirs: int = 0
    changed_files: int = 0
    retriage_flagged: int = 0
    removed_nodes: int = 0
    dropped_decisions: int = 0
    added_under_deleted: int = 0
    errors: int = 0
    error: str | None = None
    error_paths: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Job:
    node_id: int
    path: str
    new: bool  # a folder added by this rescan: everything in it is new


def _differs(a: float | int | None, b: float | int | None) -> bool:
    if a is None or b is None:
        return a is not b
    return abs(a - b) > MTIME_TOLERANCE


class Rescanner:
    """Re-list every listed folder and reconcile the cache. `run()` blocks (worker thread)."""

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        source: FileSource,
        *,
        retriage_changed: bool,
        workers: int | None = None,
        batch_dirs: int = 200,
        retry_attempts: int = 5,
        on_progress: Callable[[RescanProgress], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._connect = connect
        self._source = source
        self._retriage = retriage_changed
        self._workers = workers or source.parallelism
        self._batch_dirs = batch_dirs
        self._attempts = retry_attempts
        self._on_progress = on_progress
        self._sleep = sleep

    def run(self, cancel: CancelToken | None = None) -> RescanSummary:
        cancel = cancel or CancelToken()
        conn = self._connect()
        try:
            return self._run(conn, cancel)
        finally:
            conn.close()

    def _list(self, path: str) -> tuple[list[Entry] | None, str | None]:
        try:
            entries = call_with_retry(
                lambda: self._source.list_dir(path), attempts=self._attempts, sleep=self._sleep
            )
            return entries, None
        except TransientSourceError:
            raise
        except NotFound:
            return None, None  # vanished: the parent's diff removes it
        except SourceError as exc:
            return None, f"{type(exc).__name__}: {exc}"

    def _run(self, conn: sqlite3.Connection, cancel: CancelToken) -> RescanSummary:
        jobs = deque(
            _Job(r[0], r[1], False)
            for r in conn.execute("SELECT id, path FROM nodes WHERE is_dir = 1 AND listed = 1")
        )
        s = RescanSummary(dirs_total=len(jobs))
        s.scan_id = start_scan(conn, "rescan", s.dirs_total)
        in_flight: dict[Future, _Job] = {}
        results: list[tuple[_Job, list[Entry] | None, str | None]] = []

        def apply_batch() -> None:
            if not results:
                return
            with tx(conn):
                for job, entries, error in results:
                    self._apply(conn, job, entries, error, s, jobs)
            s.dirs_done += len(results)
            results.clear()
            if self._on_progress:
                self._on_progress(RescanProgress(s.dirs_done, s.dirs_total))

        with ThreadPoolExecutor(self._workers, thread_name_prefix="rescan") as pool:
            while (jobs or in_flight) and not cancel.cancelled and s.error is None:
                while jobs and len(in_flight) < self._workers * 2:
                    job = jobs.popleft()
                    in_flight[pool.submit(self._list, job.path)] = job
                done, _ = wait(in_flight, timeout=0.1, return_when=FIRST_COMPLETED)
                for fut in done:
                    job = in_flight.pop(fut)
                    try:
                        entries, error = fut.result()
                    except TransientSourceError as exc:
                        s.error = f"{job.path}: {exc}"
                        continue
                    results.append((job, entries, error))
                if len(results) >= self._batch_dirs or not in_flight:
                    apply_batch()
            # Keep the results of folders already being checked.
            for fut, job in list(in_flight.items()):
                try:
                    entries, error = fut.result()
                except TransientSourceError:
                    continue
                results.append((job, entries, error))
            apply_batch()

        if s.error:
            s.status = "failed"
        elif cancel.cancelled:
            s.status = "cancelled"
        else:
            s.status = "completed"
            if index_complete(conn):
                compute_rollups(conn)
        finish_scan(conn, s.scan_id, s.status, s.dirs_done, s.dirs_total)
        return s

    def _apply(
        self,
        conn: sqlite3.Connection,
        job: _Job,
        entries: list[Entry] | None,
        error: str | None,
        s: RescanSummary,
        jobs: deque[_Job],
    ) -> None:
        row = conn.execute(
            "SELECT listed FROM nodes WHERE id = ? AND path = ?", (job.node_id, job.path)
        ).fetchone()
        if row is None:
            return  # removed earlier in this rescan
        if entries is None:
            if error is not None:
                s.errors += 1
                s.error_paths.append(job.path)
                conn.execute("UPDATE nodes SET list_error = ? WHERE id = ?", (error, job.node_id))
            return

        entries = usable_entries(job.path, entries)
        under_deleted = deleted_folder_marks(conn, [*ancestors(job.path), job.path])
        flag_new = not under_deleted

        if job.new:
            added = entries
        else:
            existing = {
                r["name"]: r
                for r in conn.execute(
                    "SELECT id, name, kind, size, mtime FROM nodes WHERE parent_id = ?",
                    (job.node_id,),
                )
            }
            added = []
            for e in entries:
                old = existing.pop(e.name, None)
                if old is not None and old["kind"] == e.kind:
                    self._update_existing(conn, job.path, old, e, s)
                else:
                    if old is not None:  # kind changed: treat as removed + added
                        existing[e.name] = old
                    added.append(e)
            for old in existing.values():
                self._remove(conn, join(job.path, old["name"]), old["id"], s)

        insert_entries(conn, job.node_id, job.path, added, s.scan_id, flag_new=flag_new)
        for e in added:
            if e.is_dir:
                s.new_dirs += flag_new
            elif flag_new:
                s.new_files += 1
            else:
                s.added_under_deleted += 1
        if job.new:
            conn.execute(
                "UPDATE nodes SET listed = 1, listed_at = ?, list_error = NULL WHERE id = ?",
                (time.time(), job.node_id),
            )
        else:
            conn.execute(
                "UPDATE nodes SET listed_at = ?, list_error = NULL WHERE id = ?",
                (time.time(), job.node_id),
            )
        for e in added:
            if e.is_dir:
                child = conn.execute(
                    "SELECT id FROM nodes WHERE path = ?", (join(job.path, e.name),)
                ).fetchone()
                jobs.append(_Job(child[0], join(job.path, e.name), True))
                s.dirs_total += 1

    def _update_existing(
        self, conn: sqlite3.Connection, dir_path: str, old: sqlite3.Row, e: Entry, s: RescanSummary
    ) -> None:
        if e.is_dir:
            if _differs(old["mtime"], e.mtime):
                conn.execute("UPDATE nodes SET mtime = ? WHERE id = ?", (e.mtime, old["id"]))
            return
        if not (_differs(old["size"], e.size) or _differs(old["mtime"], e.mtime)):
            return
        s.changed_files += 1
        path = join(dir_path, e.name)
        conn.execute(
            "UPDATE nodes SET size = ?, mtime = ?, ctime = ?, sort_key = ? WHERE id = ?",
            (e.size, e.mtime, e.ctime, sort_key(e.name), old["id"]),
        )
        decision = conn.execute(
            "SELECT seen_size, seen_mtime FROM decisions WHERE path = ? AND is_dir = 0", (path,)
        ).fetchone()
        if decision is None:
            return
        seen_size, seen_mtime = decision
        # A decision recorded without seen_* can't be compared; just start tracking it.
        known = seen_size is not None or seen_mtime is not None
        changed = _differs(seen_size, e.size) or _differs(seen_mtime, e.mtime)
        if self._retriage and known and changed:
            conn.execute("UPDATE nodes SET flag_changed = 1 WHERE id = ?", (old["id"],))
            s.retriage_flagged += 1
            return
        conn.execute(
            "UPDATE decisions SET seen_size = ?, seen_mtime = ? WHERE path = ?",
            (e.size, e.mtime, path),
        )

    def _remove(self, conn: sqlite3.Connection, path: str, node_id: int, s: RescanSummary) -> None:
        lo, hi = subtree_range(path)
        below = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE path >= ? AND path < ?", (lo, hi)
        ).fetchone()[0]
        conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        s.removed_nodes += 1 + below
        where = "path = ? OR (path >= ? AND path < ?)"
        s.dropped_decisions += conn.execute(
            f"DELETE FROM decisions WHERE {where}", (path, lo, hi)
        ).rowcount
        conn.execute(f"DELETE FROM node_categories WHERE {where}", (path, lo, hi))
