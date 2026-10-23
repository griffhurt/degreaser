"""Executing a commit journal, resumably (docs/scanning-and-commit.md §3.2).

Every op is idempotent and its status is written as soon as it finishes, so a crash loses
at most the op in flight, which simply runs again on resume. `run()` blocks; call it on a
worker thread with a connection opened on that thread.
"""

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from degreaser.core.commit.catalog import build_catalog, read_catalog, write_catalog
from degreaser.core.commit.finish import CommitReport, finish_commit
from degreaser.core.commit.journal import DELETE_OPS, OpRow, get_commit, ops, set_status
from degreaser.core.commit.trash import CROSS_DEVICE_HINT, is_cross_device_error
from degreaser.core.db.connection import bump_revision
from degreaser.core.duplicates.finder import partial_hash
from degreaser.core.model import Category
from degreaser.core.sources.base import FileSource, parent
from degreaser.core.sources.errors import (
    AlreadyExists,
    NotFound,
    SourceError,
    TransientSourceError,
)

log = logging.getLogger(__name__)

# Called at the write_catalog op: (files, folders) effective categories from a fresh walk.
CatalogProvider = Callable[[], tuple[dict[str, Category], dict[str, Category]]]
# Uploads the decision state (sync) from the runner's thread and connection; never raises.
Checkpoint = Callable[[sqlite3.Connection], None]

CHECKPOINT_EVERY = 500  # ops between sync uploads, so another device can resume (§3.2)

MTIME_TOLERANCE = 1e-6


@dataclass(frozen=True, slots=True)
class CommitProgress:
    done: int
    total: int
    failed: int
    skipped: int
    current: OpRow | None


@dataclass(frozen=True, slots=True)
class RetryState:
    attempt: int
    delay: float
    error: str
    op: OpRow


@dataclass(frozen=True, slots=True)
class RunOutcome:
    status: str  # completed | paused | cancelled
    report: CommitReport | None = None


class RunControl:
    """Thread-safe controls the UI uses while a commit runs."""

    def __init__(self) -> None:
        self._pause = threading.Event()
        self._cancel = threading.Event()
        self._stop_retrying = threading.Event()
        self._wake = threading.Event()

    def pause(self) -> None:
        """Stop after the current op; the commit stays resumable."""
        self._pause.set()
        self._wake.set()

    def cancel(self) -> None:
        """Stop after the current op and close the commit; remaining ops never run."""
        self._cancel.set()
        self._wake.set()

    def retry_now(self) -> None:
        self._wake.set()

    def stop_retrying(self) -> None:
        """Give up waiting for the connection; the commit is left paused."""
        self._stop_retrying.set()
        self._wake.set()

    @property
    def pause_requested(self) -> bool:
        return self._pause.is_set()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    @property
    def stop_requested(self) -> bool:
        return self._stop_retrying.is_set()

    def wait(self, seconds: float) -> None:
        """Sleep for a backoff delay, waking early on retry/stop/pause/cancel."""
        self._wake.wait(seconds)
        self._wake.clear()


def _differs(a: float | int | None, b: float | int | None) -> bool:
    if a is None or b is None:
        return False  # nothing recorded to compare against
    return abs(a - b) > MTIME_TOLERANCE


class CommitRunner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        source: FileSource,
        commit_id: int,
        *,
        project_name: str,
        catalog_provider: CatalogProvider,
        on_progress: Callable[[CommitProgress], None] | None = None,
        on_retry: Callable[[RetryState], None] | None = None,
        wait: Callable[[float, RunControl], None] | None = None,
        max_backoff: float = 60.0,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        self._conn = conn
        self._source = source
        self._commit_id = commit_id
        self._project_name = project_name
        self._catalog_provider = catalog_provider
        self._on_progress = on_progress
        self._on_retry = on_retry
        self._wait = wait or (lambda seconds, control: control.wait(seconds))
        self._max_backoff = max_backoff
        self._checkpoint = checkpoint

    # Running ---------------------------------------------------------------------------------

    def run(self, control: RunControl | None = None) -> RunOutcome:
        """Run the pending ops. With a checkpoint, the state is uploaded every
        CHECKPOINT_EVERY ops and whenever the run stops."""
        try:
            return self._run(control or RunControl())
        finally:
            if self._checkpoint is not None:
                self._checkpoint(self._conn)

    def _run(self, control: RunControl) -> RunOutcome:
        info = get_commit(self._conn, self._commit_id)
        if info.status in ("completed", "cancelled"):
            raise ValueError(f"commit {self._commit_id} is already {info.status}")
        set_status(self._conn, self._commit_id, "running")

        pending = ops(self._conn, self._commit_id, "pending")
        total = info.ops_total
        done = total - len(pending)
        failed = info.ops_failed
        skipped = len(ops(self._conn, self._commit_id, "skipped"))
        self._progress(done, total, failed, skipped, None)

        for op in pending:
            if control.cancel_requested:
                report = finish_commit(self._conn, self._commit_id, "cancelled")
                return RunOutcome("cancelled", report)
            if control.pause_requested:
                set_status(self._conn, self._commit_id, "paused")
                return RunOutcome("paused")
            self._progress(done, total, failed, skipped, op)

            result = self._run_with_retry(op, control)
            if result is None:  # stopped while waiting for the source
                if control.cancel_requested:
                    report = finish_commit(self._conn, self._commit_id, "cancelled")
                    return RunOutcome("cancelled", report)
                set_status(self._conn, self._commit_id, "paused")
                return RunOutcome("paused")
            status, detail = result
            self._conn.execute(
                "UPDATE commit_ops SET status = ?, detail = ?, finished_at = ? WHERE id = ?",
                (status, detail, time.time(), op.id),
            )
            bump_revision(self._conn)
            done += 1
            failed += status == "failed"
            skipped += status == "skipped"
            if self._checkpoint is not None and done % CHECKPOINT_EVERY == 0:
                self._checkpoint(self._conn)
            self._progress(done, total, failed, skipped, None)

        return RunOutcome("completed", finish_commit(self._conn, self._commit_id, "completed"))

    def _progress(self, done: int, total: int, failed: int, skipped: int, op: OpRow | None) -> None:
        if self._on_progress:
            self._on_progress(CommitProgress(done, total, failed, skipped, op))

    def _run_with_retry(self, op: OpRow, control: RunControl) -> tuple[str, str | None] | None:
        delay = 1.0
        attempt = 0
        while True:
            try:
                return self._execute(op)
            except TransientSourceError as exc:
                attempt += 1
                set_status(self._conn, self._commit_id, "paused")
                log.warning("commit op %s hit %s; retrying in %.0fs", op.path, exc, delay)
                if self._on_retry:
                    self._on_retry(RetryState(attempt, delay, str(exc), op))
                self._wait(delay, control)
                if control.stop_requested or control.pause_requested or control.cancel_requested:
                    return None
                set_status(self._conn, self._commit_id, "running")
                delay = min(self._max_backoff, delay * 2)
            except SourceError as exc:
                detail = f"{type(exc).__name__}: {exc}"
                if op.op == "trash" and is_cross_device_error(exc):
                    detail = f"{detail}. {CROSS_DEVICE_HINT}"
                log.error("commit op %s %s failed: %s", op.op, op.path, detail)
                return "failed", detail

    # Operations ------------------------------------------------------------------------------

    def _execute(self, op: OpRow) -> tuple[str, str | None]:
        match op.op:
            case "trash" | "delete_file" | "delete_tree":
                return self._delete(op)
            case "rmdir_if_empty":
                return self._rmdir_if_empty(op)
            case "write_catalog":
                self._write_catalog()
                return "done", None
            case _:
                return "failed", f"operation {op.op!r} is not supported"

    def _verify(self, op: OpRow) -> tuple[str, str | None] | None:
        """Stat the target. Returns a final status if the op shouldn't proceed."""
        assert op.path is not None
        try:
            entry = self._source.stat(op.path)
        except NotFound:
            if op.op == "trash" and op.dest is not None:
                try:
                    self._source.stat(op.dest)
                    return "done", "already in trash"
                except NotFound:
                    pass
            return "done", "already gone"
        if not entry.is_dir and (
            _differs(op.expect_size, entry.size) or _differs(op.expect_mtime, entry.mtime)
        ):
            self._conn.execute("UPDATE nodes SET flag_changed = 1 WHERE path = ?", (op.path,))
            return "skipped", "changed"
        return None

    def _delete(self, op: OpRow) -> tuple[str, str | None]:
        assert op.path is not None
        verdict = self._verify(op)
        if verdict is not None:
            return verdict
        if op.guard is not None and (problem := self._check_kept_copy(op)) is not None:
            return "skipped", problem
        match op.op:
            case "trash":
                assert op.dest is not None
                self._source.makedirs(parent(op.dest))
                try:
                    self._source.rename(op.path, op.dest)
                except AlreadyExists:
                    return "failed", f"{op.dest} already exists in the trash"
            case "delete_file":
                self._source.delete_file(op.path)
            case "delete_tree":
                self._source.delete_tree(op.path)
        return "done", None

    def _check_kept_copy(self, op: OpRow) -> str | None:
        """A file deleted as a duplicate goes only if the copy being kept is still there,
        unchanged, with the same content (compared by first + last 64 KB, which is what
        stays cheap over SMB; size and mtime cover the rest)."""
        assert op.guard is not None and op.path is not None
        keep = op.guard["keep"]
        try:
            entry = self._source.stat(keep)
        except NotFound:
            return f"duplicate kept: {keep} (the copy being kept) is gone"
        if entry.size != op.guard["size"] or _differs(entry.mtime, op.guard["mtime"]):
            return f"duplicate kept: {keep} (the copy being kept) has changed"
        size = entry.size or 0
        if partial_hash(self._source, keep, size) != partial_hash(self._source, op.path, size):
            return f"duplicate kept: its contents no longer match {keep}"
        return None

    def _rmdir_if_empty(self, op: OpRow) -> tuple[str, str | None]:
        assert op.path is not None
        try:
            if self._source.list_dir(op.path):
                return "skipped", "not empty"
            self._source.remove_dir(op.path)
        except NotFound:
            return "done", "already gone"
        except AlreadyExists:  # something appeared between listing and removing
            return "skipped", "not empty"
        return "done", None

    def _write_catalog(self) -> None:
        removed = [
            o.path
            for o in ops(self._conn, self._commit_id, "done")
            if o.op in DELETE_OPS and o.path is not None
        ]
        files, folders = self._catalog_provider()

        def is_known(path: str) -> bool:
            row = self._conn.execute("SELECT 1 FROM nodes WHERE path = ?", (path,)).fetchone()
            return row is not None

        catalog = build_catalog(
            read_catalog(self._source),
            files,
            folders,
            project_name=self._project_name,
            is_known=is_known,
            removed=removed,
        )
        write_catalog(self._source, catalog)
