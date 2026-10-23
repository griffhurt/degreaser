"""Keeps a checked-out project's lock alive and its decision state uploaded (product-spec §11).

Lives on the UI thread; every source call runs on the thread pool. The lock heartbeat runs
every minute and the state is pushed every few minutes while it has changes, and once more
when the project closes.
"""

import logging
import sqlite3
import time
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QMessageBox, QWidget

from degreaser.core.project import Project
from degreaser.core.sources.errors import SourceError
from degreaser.core.sync import (
    HEARTBEAT_SECONDS,
    PUSH_INTERVAL,
    LockLost,
    SyncError,
    SyncService,
    is_dirty,
    mark_clean,
)
from degreaser.ui.controllers.tasks import Task, submit
from degreaser.ui.dialogs.modal import run_modal
from degreaser.ui.dialogs.progress import ProgressDialog

log = logging.getLogger(__name__)


class SyncController(QObject):
    lock_lost = Signal(str)  # message for the user; the window becomes read-only
    status = Signal(str)  # short text for the top bar

    def __init__(
        self,
        service: SyncService,
        project: Project,
        *,
        read_only: bool,
        holder: str = "",
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.project = project
        self.read_only = read_only
        self.holder = holder  # who has it checked out, when read-only
        self._clean_at_open = not is_dirty(project.conn)
        self._tasks: set[Task] = set()
        self._pushing = False
        self._heartbeat_timer = QTimer(self, interval=HEARTBEAT_SECONDS * 1000)
        self._heartbeat_timer.timeout.connect(self._heartbeat)
        self._push_timer = QTimer(self, interval=PUSH_INTERVAL * 1000)
        self._push_timer.timeout.connect(self.push_if_dirty)
        if not read_only:
            self._heartbeat_timer.start()
            self._push_timer.start()

    def _heartbeat(self) -> None:
        def failed(exc: BaseException) -> None:
            if isinstance(exc, LockLost):
                self._become_read_only(exc)
            else:
                log.warning("sync heartbeat failed: %s", exc)
                self.status.emit("Sync: can't reach the project folder")

        submit(
            lambda _progress: self.service.heartbeat(),
            on_error=failed,
            keep_alive=self._tasks,
        )

    def _become_read_only(self, exc: LockLost) -> None:
        self.read_only = True
        self.holder = exc.lock.device_name if exc.lock else "another device"
        self._heartbeat_timer.stop()
        self._push_timer.stop()
        self.lock_lost.emit(
            f"{self.holder} took over this project, so this window is now read-only. "
            "Changes made here since the last sync weren't uploaded; they stay on this device."
        )

    def push_if_dirty(self) -> None:
        if self.read_only or self._pushing:
            return
        self._pushing = True
        project, service = self.project, self.service

        def work(_progress: Any) -> bool:
            conn = project.new_connection()
            try:
                if not is_dirty(conn):
                    return False
                service.push(conn)
                return True
            finally:
                conn.close()

        def done(pushed: bool) -> None:
            self._pushing = False
            if pushed:
                self.status.emit(f"Synced at {time.strftime('%H:%M')}")

        def failed(exc: BaseException) -> None:
            self._pushing = False
            log.warning("sync push failed: %s", exc)
            self.status.emit("Sync: couldn't upload; will retry")

        submit(work, on_result=done, on_error=failed, keep_alive=self._tasks)

    def checkpoint(self, conn: sqlite3.Connection) -> None:
        """Push from a worker that already has a connection (the commit runner). Never
        raises: a failed upload mustn't stop a commit."""
        if self.read_only:
            return
        try:
            self.service.push(conn)
        except (SourceError, SyncError, OSError) as exc:
            log.warning("sync checkpoint failed: %s", exc)

    def finish(self, parent: QWidget) -> None:
        """At close: upload what's left and release the lock (blocking, with progress)."""
        self._heartbeat_timer.stop()
        self._push_timer.stop()
        if self.read_only:
            if self._clean_at_open:
                # Nothing the user did could change the state; only bookkeeping did.
                mark_clean(self.project.conn)
            return
        project, service = self.project, self.service
        holder: dict[str, BaseException] = {}
        dialog = ProgressDialog(
            "Saving",
            "Saving the project state to the project folder…",
            parent=parent,
        )
        dialog.cancel_button.hide()

        def work(_progress: Any) -> None:
            conn = project.new_connection()
            try:
                if is_dirty(conn):
                    service.push(conn)
            finally:
                conn.close()
                try:
                    service.release()
                except (SourceError, SyncError) as exc:
                    log.warning("couldn't release the sync lock: %s", exc)

        def failed(exc: BaseException) -> None:
            holder["error"] = exc

        task = submit(work, on_error=failed, on_finished=dialog.finish)
        dialog.start()
        run_modal(dialog)
        del task
        if error := holder.get("error"):
            QMessageBox.warning(
                parent,
                "Couldn't save to the project folder",
                f"{error}\n\nYour decisions are safe on this device. They'll be uploaded the "
                "next time you open the project here.",
            )
