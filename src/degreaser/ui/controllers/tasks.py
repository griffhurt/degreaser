"""Run blocking core work on a thread pool and get results back on the UI thread.

The UI thread never performs source I/O (docs/architecture.md §5). Signals live on a QObject
created on the calling (UI) thread, so emissions from the worker are queued to it.
"""

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

ProgressFn = Callable[[Any], None]


class _Signals(QObject):
    result = Signal(object)
    error = Signal(object)
    progress = Signal(object)
    finished = Signal()


class Task(QRunnable):
    """Calls fn(progress) on a worker thread; `progress(x)` emits `signals.progress`."""

    def __init__(self, fn: Callable[[ProgressFn], Any]) -> None:
        super().__init__()
        self.fn = fn
        self.signals = _Signals()

    def run(self) -> None:
        try:
            value = self.fn(self.signals.progress.emit)
        except BaseException as exc:
            self.signals.error.emit(exc)
        else:
            self.signals.result.emit(value)
        finally:
            self.signals.finished.emit()


def submit(
    fn: Callable[[ProgressFn], Any],
    *,
    on_result: Callable[[Any], None] | None = None,
    on_error: Callable[[BaseException], None] | None = None,
    on_progress: Callable[[Any], None] | None = None,
    on_finished: Callable[[], None] | None = None,
    pool: QThreadPool | None = None,
) -> Task:
    task = Task(fn)
    task.setAutoDelete(False)  # keep `signals` alive until the caller drops the Task
    for signal, slot in (
        (task.signals.result, on_result),
        (task.signals.error, on_error),
        (task.signals.progress, on_progress),
        (task.signals.finished, on_finished),
    ):
        if slot is not None:
            signal.connect(slot)
    (pool or QThreadPool.globalInstance()).start(task)
    return task
