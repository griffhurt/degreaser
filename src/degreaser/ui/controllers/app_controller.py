"""Window management and the project-opening sequence (docs/product-spec.md §3.3)."""

import time
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QInputDialog, QLineEdit, QMessageBox, QWidget

from degreaser import __version__
from degreaser.core import credentials
from degreaser.core.commit import find_unfinished_commit
from degreaser.core.project import Project, ProjectInUse
from degreaser.core.scan import CancelToken, Rescanner, RescanProgress, RescanSummary
from degreaser.core.settings import Settings
from degreaser.core.sources import AuthenticationFailed, SourceError
from degreaser.core.sources.base import Capability, FileSource
from degreaser.core.sync import (
    Conflict,
    LockHeld,
    OpenOutcome,
    Resolution,
    SyncError,
    SyncService,
    sync_on_open,
)
from degreaser.ui.controllers.sync_controller import SyncController
from degreaser.ui.controllers.tasks import Task, submit
from degreaser.ui.dialogs.progress import ProgressDialog
from degreaser.ui.windows.projects import ProjectsWindow
from degreaser.ui.windows.triage import TriageWindow


def rescan_message(summary: RescanSummary) -> str:
    parts = []
    if summary.new_files:
        parts.append(f"{summary.new_files:,} new files")
    if summary.changed_files:
        parts.append(f"{summary.changed_files:,} changed")
    if summary.removed_nodes:
        dropped = (
            f" (decisions dropped for {summary.dropped_decisions:,})"
            if summary.dropped_decisions
            else ""
        )
        parts.append(f"{summary.removed_nodes:,} removed{dropped}")
    if summary.errors:
        parts.append(f"{summary.errors:,} folders couldn't be read")
    return ", ".join(parts) if parts else "No changes since last time"


@dataclass
class _Opening:
    """A project on its way through the opening sequence."""

    project: Project
    source: FileSource
    sync: SyncController | None = None
    notes: list[str] = field(default_factory=list)  # toasts once the window is up

    def close(self) -> None:
        self.source.close()
        self.project.close()


def _ago(timestamp: float) -> str:
    minutes = max(0, int((time.time() - timestamp) / 60))
    if minutes < 1:
        return "less than a minute ago"
    if minutes < 120:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    return time.strftime("%d %b %Y, %H:%M", time.localtime(timestamp))


class AppController(QObject):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self.projects_window = ProjectsWindow()
        self.projects_window.open_requested.connect(self.open_project)
        self.triage_windows: dict[str, TriageWindow] = {}
        self._tasks: set[Task] = set()

    def show(self) -> None:
        self.projects_window.show()

    def open_project(self, project_uuid: str) -> None:
        if window := self.triage_windows.get(project_uuid):
            window.raise_()
            window.activateWindow()
            return
        try:
            project = Project.open(project_uuid)
        except ProjectInUse as exc:
            QMessageBox.information(self.projects_window, "Project is open", str(exc))
            return
        self._connect(project)

    def _connect(self, project: Project) -> None:
        """Connect to the source on a worker (SMB can take seconds), asking for the password
        if there isn't one saved or the server rejects it."""
        parent: QWidget = self.projects_window
        try:
            source = project.make_source()
        except AuthenticationFailed:
            if self._ask_password(project, "No password is saved for this project."):
                self._connect(project)
            else:
                project.close()
            return

        dialog = ProgressDialog("Connecting", f"Connecting to {source.describe()}…", parent=parent)
        cancelled = False

        def cancel() -> None:
            nonlocal cancelled
            cancelled = True

        def done(_result: object) -> None:
            dialog.finish()
            if cancelled:
                source.close()
                project.close()
                return
            self._after_connect(project, source)

        def failed(exc: BaseException) -> None:
            dialog.finish()
            source.close()
            if cancelled:
                project.close()
            elif isinstance(exc, AuthenticationFailed) and self._ask_password(project, str(exc)):
                self._connect(project)
            else:
                project.close()
                QMessageBox.warning(parent, "Can't reach the project folder", str(exc))

        dialog.cancel_requested.connect(cancel)
        submit(
            lambda _progress: source.connect(),
            on_result=done,
            on_error=failed,
            keep_alive=self._tasks,
        )
        dialog.start()

    def _ask_password(self, project: Project, reason: str) -> bool:
        """Prompt for the SMB password and save it. False if the user cancelled."""
        config = project.source_config
        who = f"{config.get('username', '')}@{config.get('host', '')}"
        password, ok = QInputDialog.getText(
            self.projects_window,
            "Password needed",
            f"{reason}\n\nPassword for {who}. It's saved in your system keychain.",
            QLineEdit.EchoMode.Password,
        )
        if not ok:
            return False
        credentials.set_password(project.uuid, password)
        return True

    def _after_connect(self, project: Project, source: FileSource) -> None:
        opening = _Opening(project, source)
        settings = project.settings
        if settings.sync_enabled and Capability.WRITE in source.capabilities:
            self._sync(opening)
        else:
            self._after_sync(opening)

    # Sync (checkout + pull/push) ---------------------------------------------------------------

    def _sync(
        self,
        opening: _Opening,
        *,
        read_only: bool = False,
        take_over: bool = False,
        resolve: Resolution | None = None,
        holder: str = "",
    ) -> None:
        project = opening.project
        service = SyncService(
            opening.source,
            project.uuid,
            self.settings.device,
            app_version=__version__,
            scratch_dir=project.cache_dir / "sync",
        )
        dialog = ProgressDialog(
            "Syncing",
            "Checking the project folder for newer decisions…",
            parent=self.projects_window,
        )
        dialog.cancel_button.hide()

        def work(_progress: Any) -> OpenOutcome:
            conn = project.new_connection()
            try:
                return sync_on_open(
                    service, conn, read_only=read_only, take_over=take_over, resolve=resolve
                )
            finally:
                conn.close()

        def done(outcome: OpenOutcome) -> None:
            dialog.finish()
            opening.sync = SyncController(service, project, read_only=read_only, holder=holder)
            if outcome is OpenOutcome.PULLED:
                opening.notes.append("Loaded the latest decisions from the project folder")
            elif outcome is OpenOutcome.KEPT_LOCAL:
                opening.notes.append(
                    "Showing this device's copy: it has changes the project folder doesn't"
                )
            self._after_sync(opening)

        def failed(exc: BaseException) -> None:
            dialog.finish()
            match exc:
                case LockHeld():
                    self._lock_held(opening, exc)
                case Conflict():
                    self._conflict(opening, service, exc)
                case _:
                    self._sync_unavailable(opening, service, exc, read_only=read_only)

        submit(work, on_result=done, on_error=failed, keep_alive=self._tasks)
        dialog.start()

    def _lock_held(self, opening: _Opening, exc: LockHeld) -> None:
        lock = exc.lock
        seen = _ago(lock.heartbeat_at)
        stale = (
            " It hasn't checked in for a while, so it probably crashed." if lock.is_stale() else ""
        )
        box = QMessageBox(self.projects_window)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Project is open elsewhere")
        box.setText(f"<b>“{opening.project.name}” is open on {lock.device_name}.</b>")
        box.setInformativeText(
            f"It last checked in {seen}.{stale} Only one device can make decisions at a time."
        )
        read_only = box.addButton("Open Read-Only", QMessageBox.ButtonRole.AcceptRole)
        take_over = box.addButton("Take Over…", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is read_only:
            self._sync(opening, read_only=True, holder=lock.device_name)
        elif clicked is take_over:
            answer = QMessageBox.warning(
                self.projects_window,
                "Take over?",
                f"Anything {lock.device_name} changed since it last saved to the project folder "
                "will be lost, and it will become read-only if it's still running.",
                QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
                QMessageBox.StandardButton.Cancel,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._sync(opening, take_over=True)
            else:
                opening.close()
        else:
            opening.close()

    def _conflict(self, opening: _Opening, service: SyncService, exc: Conflict) -> None:
        remote = exc.remote
        box = QMessageBox(self.projects_window)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Two versions of this project")
        box.setText(
            f"<b>This device and {remote.device_name} both changed “{opening.project.name}” "
            "since they last synced.</b>"
        )
        box.setInformativeText(
            f"{remote.device_name}'s copy was saved to the project folder "
            f"{_ago(remote.updated_at)}. Keep one; the other copy's changes since the last sync "
            "are lost."
        )
        local = box.addButton("Keep This Device's", QMessageBox.ButtonRole.AcceptRole)
        theirs = box.addButton(f"Keep {remote.device_name}'s", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is local:
            self._sync(opening, resolve="keep_local")
        elif clicked is theirs:
            self._sync(opening, resolve="keep_remote")
        else:
            self._release_then_close(opening, service)

    def _sync_unavailable(
        self, opening: _Opening, service: SyncService, exc: BaseException, *, read_only: bool
    ) -> None:
        """The folder can't hold sync state (read-only share, other project's state, …):
        continue on this device only, after checking with the user."""
        reason = str(exc) or type(exc).__name__
        if not isinstance(exc, (SourceError, SyncError)):
            raise exc
        opening.project.set_meta("sync_unavailable", reason)
        answer = QMessageBox.warning(
            self.projects_window,
            "Can't sync this project",
            f"Degreaser couldn't use the project folder to sync: {reason}.\n\n"
            "You can keep working on this device; other devices won't see the changes.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok,
            QMessageBox.StandardButton.Ok,
        )
        if answer != QMessageBox.StandardButton.Ok:
            self._release_then_close(opening, service)
            return
        if not read_only:
            submit(lambda _p: service.release(), keep_alive=self._tasks)
        opening.notes.append("Sync is off for this session")
        self._after_sync(opening)

    def _release_then_close(self, opening: _Opening, service: SyncService) -> None:
        def release(_progress: Any) -> None:
            service.release()

        submit(release, on_finished=opening.close, keep_alive=self._tasks)

    def _after_sync(self, opening: _Opening) -> None:
        project, source = opening.project, opening.source
        parent: QWidget = self.projects_window
        read_only = opening.sync is not None and opening.sync.read_only
        if opening.sync is not None and not read_only:
            project.set_meta("sync_unavailable", None)  # it works (again)
        unfinished = find_unfinished_commit(project.conn)
        if unfinished is not None and not read_only:
            # The commit dialog takes over; triage opens once it's resolved.
            from degreaser.ui.dialogs.commit import resume_unfinished_commit

            checkpoint = opening.sync.checkpoint if opening.sync else None
            if not resume_unfinished_commit(parent, project, source, unfinished, checkpoint):
                self._abandon(opening)
                return

        root_listed = project.conn.execute("SELECT listed FROM nodes WHERE path = ''").fetchone()
        if root_listed and root_listed[0]:
            self._rescan_then_open(opening)
        else:
            self._open_triage(opening, None)

    def _abandon(self, opening: _Opening) -> None:
        """Stop the opening sequence after sync: upload and release, then close."""
        if opening.sync is not None:
            opening.sync.finish(self.projects_window)
        opening.close()

    def _rescan_then_open(self, opening: _Opening) -> None:
        project, source = opening.project, opening.source
        cancel = CancelToken()
        # Read on this (UI) thread: project.settings uses the UI thread's connection.
        retriage_changed = project.settings.retriage_changed
        dialog = ProgressDialog(
            "Checking for changes",
            f"Checking “{project.name}” for files added, removed or changed since last time…",
            cancel_text="Skip Check This Time",
            parent=self.projects_window,
        )
        dialog.cancel_requested.connect(cancel.cancel)

        def work(progress: Any) -> RescanSummary:
            return Rescanner(
                project.new_connection,
                source,
                retriage_changed=retriage_changed,
                on_progress=progress,
            ).run(cancel)

        def on_progress(p: RescanProgress) -> None:
            dialog.set_progress(
                p.dirs_done, p.dirs_total, f"{p.dirs_done:,} / {p.dirs_total:,} folders"
            )

        def done(summary: RescanSummary) -> None:
            dialog.finish()
            self._open_triage(opening, summary)

        def failed(exc: BaseException) -> None:
            dialog.finish()
            QMessageBox.warning(self.projects_window, "Check failed", f"{exc}")
            self._open_triage(opening, None)

        submit(
            work,
            on_result=done,
            on_error=failed,
            on_progress=on_progress,
            keep_alive=self._tasks,
        )
        dialog.start()

    def _open_triage(self, opening: _Opening, rescan: RescanSummary | None) -> None:
        project, source = opening.project, opening.source
        window = TriageWindow(project, self.settings, source, opening.sync)
        self.triage_windows[project.uuid] = window
        window.closed.connect(lambda: self._triage_closed(opening))
        window.show()
        notes = list(opening.notes)
        if rescan is not None:
            notes.append(
                "Change check skipped" if rescan.status == "cancelled" else rescan_message(rescan)
            )
        if notes:
            window.notify(" · ".join(notes), ms=6000)
        self.projects_window.hide()

    def _triage_closed(self, opening: _Opening) -> None:
        self.triage_windows.pop(opening.project.uuid, None)
        opening.close()
        self.projects_window.refresh()
        self.projects_window.show()
