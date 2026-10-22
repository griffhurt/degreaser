"""Run blocking work on a thread pool and get results back on the UI thread."""

import itertools
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

ProgressFn = Callable[[int, int], None]


class _Job(QRunnable):
    def __init__(self, worker: Worker, job_id: int, fn: Callable[[ProgressFn], Any]) -> None:
        super().__init__()
        self.worker = worker
        self.job_id = job_id
        self.fn = fn

    def run(self) -> None:
        try:
            result = self.fn(
                lambda done, total: self.worker._progress.emit(self.job_id, done, total)
            )
        except BaseException as exc:
            self.worker._failed.emit(self.job_id, exc)
        else:
            self.worker._done.emit(self.job_id, result)


class Worker(QObject):
    """Owns its callbacks; results for forgotten jobs are dropped.

    The private signals are emitted from pool threads and received by this object, which lives
    on the UI thread, so the callbacks always run on the UI thread (queued connection).
    """

    _done = Signal(int, object)
    _failed = Signal(int, object)
    _progress = Signal(int, int, int)

    def __init__(self, parent: QObject | None = None, max_threads: int = 4) -> None:
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(max_threads)
        self._ids = itertools.count(1)
        self._callbacks: dict[
            int,
            tuple[Callable[[Any], None], Callable[[BaseException], None] | None, ProgressFn | None],
        ] = {}
        self._done.connect(self._on_done)
        self._failed.connect(self._on_failed)
        self._progress.connect(self._on_progress)

    def submit(
        self,
        fn: Callable[[ProgressFn], Any],
        on_done: Callable[[Any], None],
        on_failed: Callable[[BaseException], None] | None = None,
        on_progress: ProgressFn | None = None,
    ) -> int:
        """Run fn(progress) on the pool. Callbacks run on the UI thread."""
        job_id = next(self._ids)
        self._callbacks[job_id] = (on_done, on_failed, on_progress)
        self.pool.start(_Job(self, job_id, fn))
        return job_id

    def forget_all(self) -> None:
        """Drop callbacks for every in-flight job (their results will be ignored)."""
        self._callbacks.clear()

    def wait(self, msecs: int = 5000) -> bool:
        return self.pool.waitForDone(msecs)

    @Slot(int, object)
    def _on_done(self, job_id: int, result: object) -> None:
        callbacks = self._callbacks.pop(job_id, None)
        if callbacks:
            callbacks[0](result)

    @Slot(int, object)
    def _on_failed(self, job_id: int, exc: object) -> None:
        callbacks = self._callbacks.pop(job_id, None)
        if callbacks and callbacks[1]:
            callbacks[1](exc)  # type: ignore[arg-type]

    @Slot(int, int, int)
    def _on_progress(self, job_id: int, done: int, total: int) -> None:
        callbacks = self._callbacks.get(job_id)
        if callbacks and callbacks[2]:
            callbacks[2](done, total)
