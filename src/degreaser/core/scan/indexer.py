"""Indexing: listing folders into the tree cache (docs/scanning-and-commit.md §1).

Two writers can list folders at once (the background Indexer and on-demand listing for
traversal). Both go through `_apply_listing`, which re-checks `listed` inside the write
transaction, so a folder is only ever filled once.
"""

import logging
import sqlite3
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass

from degreaser.core.db.connection import tx
from degreaser.core.scan.common import (
    CancelToken,
    call_with_retry,
    finish_scan,
    insert_entries,
    start_scan,
    usable_entries,
)
from degreaser.core.sources.base import Entry, FileSource
from degreaser.core.sources.errors import NotFound, SourceError, TransientSourceError

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ListOutcome:
    path: str
    already_listed: bool = False
    inserted: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class IndexProgress:
    dirs_listed: int
    dirs_pending: int
    entries_found: int


@dataclass(frozen=True, slots=True)
class IndexSummary:
    status: str  # completed | cancelled | failed | incomplete
    scan_id: int
    dirs_listed: int
    entries_found: int
    errors: int
    error: str | None = None


def _apply_listing(
    conn: sqlite3.Connection,
    node_id: int,
    dir_path: str,
    entries: list[Entry] | None,
    error: str | None,
    scan_id: int | None,
) -> ListOutcome:
    """Store a listing (or a listing error). Caller holds the write transaction."""
    row = conn.execute("SELECT listed FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if row is None or row[0]:
        return ListOutcome(dir_path, already_listed=True)
    inserted = 0
    if entries is not None:
        inserted = insert_entries(
            conn, node_id, dir_path, usable_entries(dir_path, entries), scan_id, flag_new=False
        )
    conn.execute(
        "UPDATE nodes SET listed = 1, listed_at = ?, list_error = ? WHERE id = ?",
        (time.time(), error, node_id),
    )
    return ListOutcome(dir_path, inserted=inserted, error=error)


def _list(
    source: FileSource, dir_path: str, attempts: int, sleep: Callable[[float], None]
) -> tuple[list[Entry] | None, str | None]:
    """List a folder. Returns (entries, None) or (None, error message) for permanent errors."""
    try:
        entries = call_with_retry(lambda: source.list_dir(dir_path), attempts=attempts, sleep=sleep)
        return entries, None
    except TransientSourceError:
        raise
    except NotFound:
        return None, "not found"
    except SourceError as exc:
        return None, f"{type(exc).__name__}: {exc}"


def list_dir_into_db(
    conn: sqlite3.Connection,
    source: FileSource,
    dir_path: str,
    *,
    scan_id: int | None = None,
    attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> ListOutcome:
    """On-demand listing of one folder for traversal. Blocking; call from a worker thread.

    Raises KeyError if the folder isn't in the tree cache, and TransientSourceError if the
    source stays unreachable.
    """
    row = conn.execute(
        "SELECT id, listed FROM nodes WHERE path = ? AND is_dir = 1", (dir_path,)
    ).fetchone()
    if row is None:
        raise KeyError(dir_path)
    if row["listed"]:
        return ListOutcome(dir_path, already_listed=True)
    entries, error = _list(source, dir_path, attempts, sleep)
    with tx(conn):
        return _apply_listing(conn, row["id"], dir_path, entries, error, scan_id)


def compute_rollups(conn: sqlite3.Connection) -> None:
    """Fill subtree_files / subtree_bytes bottom-up.

    Relies on children having larger ids than their parents, which indexing and rescans
    guarantee (path rewrites in M4 won't; see docs/triage-engine.md §8).
    """
    files: defaultdict[int, int] = defaultdict(int)
    nbytes: defaultdict[int, int] = defaultdict(int)
    dirs: list[tuple[int, int, int]] = []
    for node_id, parent_id, is_dir, size in conn.execute(
        "SELECT id, parent_id, is_dir, size FROM nodes ORDER BY id DESC"
    ):
        if is_dir:
            f, b = files.pop(node_id, 0), nbytes.pop(node_id, 0)
            dirs.append((f, b, node_id))
        else:
            f, b = 1, size or 0
        if parent_id is not None:
            files[parent_id] += f
            nbytes[parent_id] += b
    with tx(conn):
        conn.executemany("UPDATE nodes SET subtree_files = ?, subtree_bytes = ? WHERE id = ?", dirs)


def index_complete(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT 1 FROM nodes WHERE is_dir = 1 AND listed = 0 LIMIT 1").fetchone()
    return row is None


class Indexer:
    """Background breadth-first indexing of every unlisted folder.

    `run()` blocks; call it on a worker thread. It opens its own DB connection via `connect`.
    `request(path)` asks for a folder to be listed next and returns a Future that resolves
    once its children are in the DB.
    """

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        source: FileSource,
        *,
        workers: int | None = None,
        batch_rows: int = 1000,
        retry_attempts: int = 5,
        on_progress: Callable[[IndexProgress], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._connect = connect
        self._source = source
        self._workers = workers or source.parallelism
        self._batch_rows = batch_rows
        self._attempts = retry_attempts
        self._on_progress = on_progress
        self._sleep = sleep
        self._lock = threading.Lock()
        self._priority: deque[tuple[str, Future[ListOutcome]]] = deque()
        self._wake = threading.Event()

    def request(self, dir_path: str) -> Future[ListOutcome]:
        future: Future[ListOutcome] = Future()
        with self._lock:
            self._priority.append((dir_path, future))
        self._wake.set()
        return future

    def run(self, cancel: CancelToken | None = None) -> IndexSummary:
        cancel = cancel or CancelToken()
        conn = self._connect()
        try:
            return self._run(conn, cancel)
        finally:
            conn.close()

    def _run(self, conn: sqlite3.Connection, cancel: CancelToken) -> IndexSummary:
        scan_id = start_scan(conn, "index")
        pending: deque[tuple[int, str]] = deque(
            (r[0], r[1])
            for r in conn.execute(
                "SELECT id, path FROM nodes WHERE is_dir = 1 AND listed = 0 ORDER BY id"
            )
        )
        queued = {path for _, path in pending}
        in_flight: dict[Future, tuple[int, str]] = {}
        waiters: defaultdict[str, list[Future[ListOutcome]]] = defaultdict(list)
        buffer: list[tuple[int, str, list[Entry] | None, str | None]] = []
        buffered_rows = 0
        dirs_listed = entries_found = errors = 0
        failure: str | None = None

        def submit(pool: ThreadPoolExecutor, node_id: int, path: str) -> None:
            fut = pool.submit(_list, self._source, path, self._attempts, self._sleep)
            in_flight[fut] = (node_id, path)

        def flush() -> None:
            nonlocal buffered_rows, dirs_listed, entries_found, errors
            if not buffer:
                return
            outcomes = []
            with tx(conn):
                for node_id, path, entries, error in buffer:
                    outcomes.append(_apply_listing(conn, node_id, path, entries, error, scan_id))
            for (node_id, path, _, error), outcome in zip(buffer, outcomes, strict=True):
                if not outcome.already_listed:
                    dirs_listed += 1
                    entries_found += outcome.inserted
                    errors += error is not None
                for r in conn.execute(
                    "SELECT id, path FROM nodes WHERE parent_id = ? AND is_dir = 1 AND listed = 0"
                    " ORDER BY id",
                    (node_id,),
                ):
                    if r[1] not in queued:
                        queued.add(r[1])
                        pending.append((r[0], r[1]))
                for fut in waiters.pop(path, []):
                    fut.set_result(outcome)
            buffer.clear()
            buffered_rows = 0
            if self._on_progress:
                self._on_progress(IndexProgress(dirs_listed, len(pending), entries_found))

        def take_priority(pool: ThreadPoolExecutor) -> None:
            with self._lock:
                requests = list(self._priority)
                self._priority.clear()
                self._wake.clear()
            in_flight_paths = {path for _, path in in_flight.values()}
            for path, fut in requests:
                row = conn.execute(
                    "SELECT id, listed FROM nodes WHERE path = ? AND is_dir = 1", (path,)
                ).fetchone()
                if row is None:
                    fut.set_exception(KeyError(path))
                elif row["listed"]:
                    fut.set_result(ListOutcome(path, already_listed=True))
                else:
                    waiters[path].append(fut)
                    if path not in in_flight_paths and not any(b[1] == path for b in buffer):
                        submit(pool, row["id"], path)
                        in_flight_paths.add(path)

        with ThreadPoolExecutor(self._workers, thread_name_prefix="indexer") as pool:
            while True:
                if cancel.cancelled or failure:
                    break
                take_priority(pool)
                while len(in_flight) < self._workers and pending:
                    node_id, path = pending.popleft()
                    submit(pool, node_id, path)
                if not in_flight:
                    flush()
                    if pending:
                        continue
                    # Folders listed on demand may have added unlisted children we haven't seen.
                    for r in conn.execute(
                        "SELECT id, path FROM nodes WHERE is_dir = 1 AND listed = 0 ORDER BY id"
                    ):
                        if r[1] not in queued:
                            queued.add(r[1])
                            pending.append((r[0], r[1]))
                    if not pending:
                        break
                    continue
                done, _ = wait(in_flight, timeout=0.05, return_when=FIRST_COMPLETED)
                for fut in done:
                    node_id, path = in_flight.pop(fut)
                    try:
                        entries, error = fut.result()
                    except TransientSourceError as exc:
                        failure = f"{path}: {exc}"
                        for w in waiters.pop(path, []):
                            w.set_exception(exc)
                        continue
                    buffer.append((node_id, path, entries, error))
                    buffered_rows += len(entries or ())
                if buffer and (
                    buffered_rows >= self._batch_rows
                    or not in_flight
                    or any(b[1] in waiters for b in buffer)
                ):
                    flush()
            # Let in-flight listings finish so their results aren't wasted.
            for fut, (node_id, path) in list(in_flight.items()):
                try:
                    entries, error = fut.result()
                except TransientSourceError:
                    continue
                buffer.append((node_id, path, entries, error))
            in_flight.clear()
            flush()

        with self._lock:
            leftover = list(self._priority)
            self._priority.clear()
        for path, fut in leftover:
            waiters[path].append(fut)
        for futs in waiters.values():
            for fut in futs:
                if not fut.done():
                    fut.set_exception(RuntimeError("indexer stopped"))

        complete = index_complete(conn)
        if failure:
            status = "failed"
        elif cancel.cancelled:
            status = "cancelled"
        else:
            status = "completed" if complete else "incomplete"
        if complete:
            compute_rollups(conn)
        finish_scan(
            conn,
            scan_id,
            "completed" if status == "incomplete" else status,
            dirs_listed,
            dirs_listed + len(pending),
        )
        return IndexSummary(status, scan_id, dirs_listed, entries_found, errors, failure)
