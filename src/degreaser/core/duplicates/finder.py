"""Finding byte-identical files by progressive narrowing (docs/duplicates.md §3).

1. Group by size (free: the index has every size). A file with a unique size can't have a
   duplicate and is never read.
2. Partial hash of files that share a size: BLAKE2b-128 of the first and last 64 KB, two
   range reads whatever the file's size.
3. Full hash only for files whose partial hashes still collide.

Hashes are device-local derived data in `file_hashes`, keyed on node id. A row is valid while
the node's size and mtime match it, so the job resumes for free and rescans only cost the
files that changed. Files below the project's minimum size, and files that are being deleted
anyway, are skipped.
"""

import hashlib
import sqlite3
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass

from degreaser.core.categories.store import CategoryStore
from degreaser.core.db.connection import tx
from degreaser.core.model import Effective, Origin
from degreaser.core.rules.ruleset import RuleSet
from degreaser.core.scan.common import CancelToken
from degreaser.core.sources.base import FileSource
from degreaser.core.sources.errors import SourceError
from degreaser.core.triage.resolver import Resolver

EDGE = 64 * 1024  # bytes from each end for the partial hash
CHUNK = 4 * 1024 * 1024  # full-hash read size (one SMB round trip)
BATCH = 50  # hashes per write transaction


def _hasher() -> hashlib.blake2b:
    return hashlib.blake2b(digest_size=16)


def partial_hash(source: FileSource, rel: str, size: int) -> bytes:
    """Hash of the first and last 64 KB (the whole file if it's no bigger than that)."""
    h = _hasher()
    if size <= 2 * EDGE:
        h.update(source.read_range(rel, 0, size))
    else:
        h.update(source.read_range(rel, 0, EDGE))
        h.update(source.read_range(rel, size - EDGE, EDGE))
    return h.digest()


def full_hash(source: FileSource, rel: str, size: int, cancel: CancelToken | None = None) -> bytes:
    h = _hasher()
    done = 0
    while done < size:
        if cancel is not None and cancel.cancelled:
            raise _Stopped
        data = source.read_range(rel, done, min(CHUNK, size - done))
        if not data:
            break
        h.update(data)
        done += len(data)
    return h.digest()


def covers_whole_file(size: int) -> bool:
    """For small files the partial hash already reads every byte."""
    return size <= 2 * EDGE


class _Stopped(Exception):
    pass


@dataclass(frozen=True, slots=True)
class FindProgress:
    done: int
    total: int


@dataclass(frozen=True, slots=True)
class FindSummary:
    status: str  # completed | cancelled
    hashed: int
    groups: int  # duplicate groups known afterwards
    duplicate_files: int  # files that have at least one identical copy


class Pause:
    """Lets the UI pause the search; workers wait between files."""

    def __init__(self) -> None:
        self._running = threading.Event()
        self._running.set()

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    def wait(self, cancel: CancelToken) -> None:
        while not self._running.wait(0.2):
            if cancel.cancelled:
                return


class DuplicateFinder:
    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        source: FileSource,
        *,
        min_size: int,
        workers: int = 2,
        on_progress: Callable[[FindProgress], None] | None = None,
        pause: Pause | None = None,
    ) -> None:
        self._connect = connect
        self._source = source
        self._min_size = max(1, min_size)
        self._workers = workers
        self._on_progress = on_progress
        self._pause = pause or Pause()
        self._rows: list[tuple[object, ...]] = []

    def run(self, cancel: CancelToken | None = None) -> FindSummary:
        cancel = cancel or CancelToken()
        conn = self._connect()
        try:
            return self._run(conn, cancel)
        finally:
            conn.close()

    def _run(self, conn: sqlite3.Connection, cancel: CancelToken) -> FindSummary:
        purge_stale(conn)
        candidates = self._candidates(conn)
        # Phase 1: partial hashes for every candidate that lacks one.
        need_partial = [c for c in candidates if c.partial is None]
        # Phase 2 isn't known until phase 1 is done; estimate it as the work so far.
        total = len(need_partial)
        done = 0
        hashed = 0
        self._report(done, total)

        def work_partial(c: _Candidate) -> tuple[_Candidate, bytes | None]:
            self._pause.wait(cancel)
            if cancel.cancelled:
                raise _Stopped
            try:
                return c, partial_hash(self._source, c.path, c.size)
            except SourceError:
                return c, None  # gone or unreadable: left unhashed (the rescan notices)

        try:
            for c, digest in self._map(work_partial, need_partial, cancel):
                done += 1
                if digest is not None:
                    c.partial = digest
                    full = digest if covers_whole_file(c.size) else None
                    self._store(conn, c, digest, full)
                    hashed += 1
                self._report(done, total)
            self._flush(conn)

            # Phase 2: full hashes where size + partial still collide.
            groups: dict[tuple[int, bytes], list[_Candidate]] = defaultdict(list)
            for c in candidates:
                if c.partial is not None:
                    groups[(c.size, c.partial)].append(c)
            need_full = [
                c
                for members in groups.values()
                if len(members) > 1
                for c in members
                if c.full is None and not covers_whole_file(c.size)
            ]
            total = done + len(need_full)
            self._report(done, total)

            def work_full(c: _Candidate) -> tuple[_Candidate, bytes | None]:
                self._pause.wait(cancel)
                if cancel.cancelled:
                    raise _Stopped
                try:
                    return c, full_hash(self._source, c.path, c.size, cancel)
                except SourceError:
                    return c, None

            for c, digest in self._map(work_full, need_full, cancel):
                done += 1
                if digest is not None:
                    self._store(conn, c, c.partial, digest)
                    hashed += 1
                self._report(done, total)
            self._flush(conn)
        except _Stopped:
            self._flush(conn)
            return FindSummary("cancelled", hashed, *_counts(conn))
        return FindSummary("completed", hashed, *_counts(conn))

    # Helpers -----------------------------------------------------------------------------------

    def _candidates(self, conn: sqlite3.Connection) -> list[_Candidate]:
        """Files at least min_size bytes that share their size with another such file and
        aren't being deleted anyway (rules, folder marks, direct deletes)."""
        resolver = Resolver(conn, RuleSet.load(conn), CategoryStore(conn))
        by_size: dict[int, list[_Candidate]] = defaultdict(list)
        rows = conn.execute(
            "SELECT n.id, n.path, n.size, n.mtime, h.partial, h.full FROM nodes n"
            " LEFT JOIN file_hashes h ON h.node_id = n.id"
            " WHERE n.is_dir = 0 AND n.kind = 'file' AND n.size >= ? AND n.size IN ("
            "   SELECT size FROM nodes WHERE is_dir = 0 AND kind = 'file' AND size >= ?"
            "   GROUP BY size HAVING count(*) > 1)",
            (self._min_size, self._min_size),
        ).fetchall()
        for node_id, path, size, mtime, partial, full in rows:
            state = resolver.resolve(path)
            if state.action is Effective.DELETE or state.origin is Origin.EXCLUDED:
                continue
            by_size[size].append(_Candidate(node_id, path, size, mtime, partial, full))
        return [c for members in by_size.values() if len(members) > 1 for c in members]

    def _map(
        self,
        fn: Callable[[_Candidate], tuple[_Candidate, bytes | None]],
        items: list[_Candidate],
        cancel: CancelToken,
    ) -> Iterator[tuple[_Candidate, bytes | None]]:
        """fn over items on a small pool, results in completion order, few in flight."""
        if not items:
            return
        with ThreadPoolExecutor(self._workers, thread_name_prefix="hash") as pool:
            pending = iter(items)
            in_flight: set[Future[tuple[_Candidate, bytes | None]]] = set()
            try:
                while True:
                    while len(in_flight) < self._workers * 2:
                        item = next(pending, None)
                        if item is None:
                            break
                        in_flight.add(pool.submit(fn, item))
                    if not in_flight:
                        return
                    done, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
                    for future in done:
                        yield future.result()  # _Stopped propagates
                    if cancel.cancelled:
                        raise _Stopped
            finally:
                for future in in_flight:
                    future.cancel()

    def _store(
        self, conn: sqlite3.Connection, c: _Candidate, partial: bytes | None, full: bytes | None
    ) -> None:
        self._rows.append((c.node_id, c.size, c.mtime, partial, full, time.time()))
        c.full = full
        if len(self._rows) >= BATCH:
            self._flush(conn)

    def _flush(self, conn: sqlite3.Connection) -> None:
        if not self._rows:
            return
        with tx(conn):
            conn.executemany(
                "INSERT INTO file_hashes(node_id, size, mtime, partial, full, hashed_at)"
                " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(node_id) DO UPDATE SET"
                " size = excluded.size, mtime = excluded.mtime, partial = excluded.partial,"
                " full = excluded.full, hashed_at = excluded.hashed_at",
                self._rows,
            )
        self._rows = []

    def _report(self, done: int, total: int) -> None:
        if self._on_progress is not None:
            self._on_progress(FindProgress(done, total))


@dataclass(slots=True)
class _Candidate:
    node_id: int
    path: str
    size: int
    mtime: float | None
    partial: bytes | None
    full: bytes | None


def purge_stale(conn: sqlite3.Connection) -> int:
    """Drop hashes of files whose size or mtime changed since they were hashed."""
    with tx(conn):
        return conn.execute(
            "DELETE FROM file_hashes WHERE node_id IN ("
            " SELECT h.node_id FROM file_hashes h JOIN nodes n ON n.id = h.node_id"
            " WHERE n.size IS NOT h.size OR n.mtime IS NOT h.mtime)"
        ).rowcount


def _counts(conn: sqlite3.Connection) -> tuple[int, int]:
    row = conn.execute(
        "SELECT count(*), coalesce(sum(n), 0) FROM ("
        " SELECT count(*) AS n FROM file_hashes WHERE full IS NOT NULL"
        " GROUP BY full HAVING count(*) > 1)"
    ).fetchone()
    return row[0], row[1]
