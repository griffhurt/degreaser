"""Window management and the project-opening sequence (docs/product-spec.md §3.3)."""

from typing import Any

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QMessageBox, QWidget

from degreaser.core.commit import find_unfinished_commit
from degreaser.core.project import Project, ProjectInUse
from degreaser.core.scan import CancelToken, Rescanner, RescanProgress, RescanSummary
from degreaser.core.settings import Settings
from degreaser.core.sources import SourceError
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
        parent: QWidget = self.projects_window
        try:
            project = Project.open(project_uuid)
        except ProjectInUse as exc:
            QMessageBox.information(parent, "Project is open", str(exc))
            return
        try:
            project.make_source().connect()
        except SourceError as exc:
            project.close()
            QMessageBox.warning(parent, "Can't reach the project folder", str(exc))
            return

        unfinished = find_unfinished_commit(project.conn)
        if unfinished is not None:
            # The commit dialog takes over; triage opens once it's resolved.
            from degreaser.ui.dialogs.commit import resume_unfinished_commit

            if not resume_unfinished_commit(parent, project, unfinished):
                project.close()
                return

        root_listed = project.conn.execute("SELECT listed FROM nodes WHERE path = ''").fetchone()
        if root_listed and root_listed[0]:
            self._rescan_then_open(project)
        else:
            self._open_triage(project, None)

    def _rescan_then_open(self, project: Project) -> None:
        cancel = CancelToken()
        source = project.make_source()
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
                retriage_changed=project.settings.retriage_changed,
                on_progress=progress,
            ).run(cancel)

        def on_progress(p: RescanProgress) -> None:
            dialog.set_progress(
                p.dirs_done, p.dirs_total, f"{p.dirs_done:,} / {p.dirs_total:,} folders"
            )

        def done(summary: RescanSummary) -> None:
            dialog.finish()
            self._open_triage(project, summary)

        def failed(exc: BaseException) -> None:
            dialog.finish()
            QMessageBox.warning(self.projects_window, "Check failed", f"{exc}")
            self._open_triage(project, None)

        task = submit(work, on_result=done, on_error=failed, on_progress=on_progress)
        self._tasks.add(task)
        task.signals.finished.connect(lambda: self._tasks.discard(task))
        dialog.start()

    def _open_triage(self, project: Project, rescan: RescanSummary | None) -> None:
        window = TriageWindow(project, self.settings)
        self.triage_windows[project.uuid] = window
        window.closed.connect(lambda: self._triage_closed(project))
        window.show()
        if rescan is not None:
            message = rescan_message(rescan)
            if rescan.status == "cancelled":
                message = "Change check skipped"
            window.statusBar().showMessage(message, 10_000)
        self.projects_window.hide()

    def _triage_closed(self, project: Project) -> None:
        self.triage_windows.pop(project.uuid, None)
        project.close()
        self.projects_window.refresh()
        self.projects_window.show()
