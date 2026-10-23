"""Commit confirmation, progress, report and resume (docs/product-spec.md §8)."""

import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from degreaser.core.categories import CategoryStore
from degreaser.core.commit import (
    CommitInfo,
    CommitInProgress,
    CommitPlan,
    CommitProgress,
    CommitReport,
    CommitRunner,
    RetryState,
    RunControl,
    RunOutcome,
    cancel_planned_commit,
    plan_commit,
    report_csv,
)
from degreaser.core.commit.runner import Checkpoint
from degreaser.core.model import Category
from degreaser.core.project import Project
from degreaser.core.rules import RuleSet
from degreaser.core.sources.base import FileSource
from degreaser.core.triage import full_walk
from degreaser.ui.controllers.tasks import Task, submit
from degreaser.ui.dialogs.progress import ProgressDialog
from degreaser.ui.format import human_bytes

OP_VERBS = {
    "trash": "Moving to trash",
    "delete_file": "Deleting",
    "delete_tree": "Deleting folder",
    "rmdir_if_empty": "Removing empty folder",
    "write_catalog": "Writing catalog",
    "move": "Moving",
}


def confirmation_text(summary: dict[str, Any]) -> str:
    files = summary.get("files_delete", 0)
    folders = summary.get("folders_delete", 0)
    size = human_bytes(summary.get("bytes_delete", 0))
    categorised = summary.get("files_categorised", 0)
    verb = (
        "permanently delete"
        if summary.get("deletion_mode") == "permanent"
        else "move to the Degreaser trash"
    )
    folder_text = f" ({folders:,} folder{'s' if folders != 1 else ''})" if folders else ""
    text = (
        f"This will <b>{verb} {files:,} file{'s' if files != 1 else ''}{folder_text} — {size}</b>"
        f" and <b>record categories for {categorised:,} file{'s' if categorised != 1 else ''}</b>."
    )
    if added := summary.get("added_since_marked"):
        text += (
            f"<p>{added:,} file(s) were added to folders marked for deletion since they were "
            "marked; they will be deleted too.</p>"
        )
    return text


def _catalog_provider(project: Project) -> Any:
    def provide() -> tuple[dict[str, Category], dict[str, Category]]:
        conn = project.new_connection()
        try:
            walk = full_walk(conn, RuleSet.load(conn), CategoryStore(conn))
        finally:
            conn.close()
        return walk.catalog_files, walk.catalog_folders

    return provide


class CommitRunDialog(QDialog):
    """Runs (or resumes) a planned commit, then shows the report."""

    def __init__(
        self,
        project: Project,
        source: FileSource,
        commit_id: int,
        parent: QWidget | None = None,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        super().__init__(parent)
        self.project = project
        self.source = source
        self.commit_id = commit_id
        self.checkpoint = checkpoint
        self.control = RunControl()
        self.outcome: RunOutcome | None = None
        self._task: Task | None = None
        self._retry_deadline = 0.0
        self.setWindowTitle("Committing")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        self.heading = QLabel("<b>Committing…</b>")
        self.bar = QProgressBar()
        self.current = QLabel()
        self.current.setTextFormat(Qt.TextFormat.PlainText)
        self.counts = QLabel()
        self.retry_label = QLabel()
        self.retry_label.setProperty("role", "warning")
        self.retry_label.hide()
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.hide()
        for w in (self.heading, self.bar, self.current, self.counts, self.retry_label):
            layout.addWidget(w)
        layout.addWidget(self.report, 1)

        buttons = QHBoxLayout()
        self.retry_now = QPushButton("Retry Now")
        self.stop_retrying = QPushButton("Stop")
        self.pause_button = QPushButton("Pause")
        self.cancel_button = QPushButton("Cancel Commit…")
        self.export_button = QPushButton("Export Report…")
        self.close_button = QPushButton("Close")
        self.retry_now.clicked.connect(self.control.retry_now)
        self.stop_retrying.clicked.connect(self.control.stop_retrying)
        self.pause_button.clicked.connect(self._pause)
        self.cancel_button.clicked.connect(self._cancel)
        self.export_button.clicked.connect(self._export)
        self.close_button.clicked.connect(self.accept)
        for b in (self.retry_now, self.stop_retrying, self.export_button, self.close_button):
            b.hide()
        buttons.addWidget(self.retry_now)
        buttons.addWidget(self.stop_retrying)
        buttons.addStretch()
        for b in (self.export_button, self.pause_button, self.cancel_button, self.close_button):
            buttons.addWidget(b)
        layout.addLayout(buttons)

        self._countdown = QTimer(self, interval=500)
        self._countdown.timeout.connect(self._tick_retry)

    def start(self) -> None:
        project, source, control = self.project, self.source, self.control
        commit_id = self.commit_id
        name = project.name  # reads the UI thread's connection: never inside work()

        def work(progress: Any) -> RunOutcome:
            conn = project.new_connection()
            try:
                runner = CommitRunner(
                    conn,
                    source,
                    commit_id,
                    project_name=name,
                    catalog_provider=_catalog_provider(project),
                    on_progress=progress,
                    on_retry=progress,
                    checkpoint=self.checkpoint,
                )
                return runner.run(control)
            finally:
                conn.close()

        self._task = submit(
            work, on_result=self._finished, on_error=self._failed, on_progress=self._progress
        )

    def _progress(self, update: CommitProgress | RetryState) -> None:
        if isinstance(update, RetryState):
            self._retry_deadline = time.monotonic() + update.delay
            self.retry_label.setToolTip(update.error)
            self._tick_retry()
            for w in (self.retry_label, self.retry_now, self.stop_retrying):
                w.show()
            self._countdown.start()
            return
        self._countdown.stop()
        for w in (self.retry_label, self.retry_now, self.stop_retrying):
            w.hide()
        self.bar.setRange(0, max(update.total, 1))
        self.bar.setValue(update.done)
        self.counts.setText(
            f"{update.done:,} of {update.total:,} operations"
            f" · {update.skipped:,} skipped · {update.failed:,} failed"
        )
        if update.current is not None:
            verb = OP_VERBS.get(update.current.op, update.current.op)
            self.current.setText(f"{verb}: {update.current.path or ''}")

    def _tick_retry(self) -> None:
        remaining = max(0, round(self._retry_deadline - time.monotonic()))
        self.retry_label.setText(f"Connection lost — retrying in {remaining} s")

    def _pause(self) -> None:
        self.pause_button.setEnabled(False)
        self.pause_button.setText("Pausing…")
        self.control.pause()

    def _cancel(self) -> None:
        answer = QMessageBox.question(
            self,
            "Cancel commit?",
            "Stop after the current operation? Operations already done stay done; the rest of "
            "your decisions are kept for a later commit.",
            QMessageBox.StandardButton.No | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.cancel_button.setEnabled(False)
            self.control.cancel()

    def _finished(self, outcome: RunOutcome) -> None:
        self.outcome = outcome
        self._countdown.stop()
        for w in (self.pause_button, self.cancel_button, self.retry_now, self.stop_retrying):
            w.hide()
        self.retry_label.hide()
        self.close_button.show()
        if outcome.status == "paused":
            self.heading.setText(
                "<b>Commit paused.</b> It will be offered again the next time you open the project."
            )
            return
        self.heading.setText(
            "<b>Commit complete.</b>"
            if outcome.status == "completed"
            else "<b>Commit cancelled.</b>"
        )
        if outcome.report is not None:
            self.report.setPlainText(format_report(outcome.report))
            self.report.show()
            self.export_button.show()

    def _failed(self, exc: BaseException) -> None:
        self._finished(RunOutcome("paused"))
        QMessageBox.warning(self, "Commit stopped", f"{type(exc).__name__}: {exc}")

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export commit report", "degreaser-commit-report.csv", "CSV (*.csv)"
        )
        if path:
            Path(path).write_text(report_csv(self.project.conn, self.commit_id), encoding="utf-8")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.outcome is None:
            # Closing mid-run pauses; the dialog closes once the runner stops.
            self._pause()
            event.ignore()
            return
        super().closeEvent(event)

    def reject(self) -> None:
        if self.outcome is None:
            self._pause()
            return
        super().reject()


def format_report(report: CommitReport) -> str:
    moved = "Moved to Degreaser trash" if report.deletion_mode == "trash" else "Deleted"
    lines = [
        f"{moved}: {report.files_deleted:,} files, {report.folders_deleted:,} folders"
        f" ({human_bytes(report.bytes_deleted)})",
        f"Empty folders removed: {report.empty_dirs_removed:,}",
        f"Catalog written: {'yes' if report.catalog_written else 'no'}",
    ]
    if report.skipped_changed:
        changed = len(report.skipped_changed)
        lines.append(f"\nLeft alone because they changed since the last scan ({changed:,}):")
        lines += [f"  {p}" for p in report.skipped_changed[:200]]
    if report.skipped_other:
        lines.append(f"\nSkipped ({len(report.skipped_other):,}):")
        lines += [f"  {p}: {why}" for p, why in report.skipped_other[:200]]
    if report.failures:
        lines.append(f"\nFailed ({len(report.failures):,}):")
        lines += [f"  {p}: {why}" for p, why in report.failures[:200]]
    return "\n".join(lines)


def start_commit(
    parent: QWidget,
    project: Project,
    source: FileSource,
    checkpoint: Checkpoint | None = None,
) -> RunOutcome | None:
    """Walk + plan on a worker, confirm, then run. Returns the outcome, or None if not run."""
    holder: dict[str, Any] = {}
    progress = ProgressDialog("Preparing commit", "Working out what to delete…", parent=parent)
    progress.cancel_button.hide()
    settings = project.settings

    def work(_progress: Any) -> CommitPlan:
        conn = project.new_connection()
        try:
            walk = full_walk(conn, RuleSet.load(conn), CategoryStore(conn))
            return plan_commit(conn, walk, settings)
        finally:
            conn.close()

    def done(plan: CommitPlan) -> None:
        holder["plan"] = plan
        progress.finish()

    def failed(exc: BaseException) -> None:
        holder["error"] = exc
        progress.finish()

    task = submit(work, on_result=done, on_error=failed)
    progress.start()
    progress.exec()
    del task
    if error := holder.get("error"):
        title = "Can't commit yet" if isinstance(error, CommitInProgress) else "Planning failed"
        QMessageBox.warning(parent, title, str(error))
        return None
    plan: CommitPlan = holder["plan"]
    if plan.ops_total <= 1 and not plan.summary.get("files_categorised"):
        cancel_planned_commit(project.conn, plan.commit_id)
        QMessageBox.information(parent, "Nothing to commit", "No files are marked for deletion.")
        return None

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("Commit triage?")
    box.setText("<b>Commit triage?</b>")
    box.setInformativeText(confirmation_text(plan.summary))
    commit_button = box.addButton("Commit", QMessageBox.ButtonRole.DestructiveRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.exec()
    if box.clickedButton() is not commit_button:
        cancel_planned_commit(project.conn, plan.commit_id)
        return None

    return run_commit(parent, project, source, plan.commit_id, checkpoint)


def run_commit(
    parent: QWidget,
    project: Project,
    source: FileSource,
    commit_id: int,
    checkpoint: Checkpoint | None = None,
) -> RunOutcome | None:
    dialog = CommitRunDialog(project, source, commit_id, parent, checkpoint)
    dialog.start()
    dialog.exec()
    return dialog.outcome


def resume_unfinished_commit(
    parent: QWidget,
    project: Project,
    source: FileSource,
    info: CommitInfo,
    checkpoint: Checkpoint | None = None,
) -> bool:
    """Offer to resume. True once the commit is resolved and triage may open."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("Unfinished commit")
    box.setText(f"<b>“{project.name}” has an unfinished commit.</b>")
    box.setInformativeText(
        f"{info.ops_done:,} of {info.ops_total:,} operations are done. "
        "Resume it to finish deleting and recording categories."
    )
    resume = box.addButton("Resume Commit", QMessageBox.ButtonRole.AcceptRole)
    discard = None
    if info.ops_done == 0:
        discard = box.addButton("Discard Plan", QMessageBox.ButtonRole.DestructiveRole)
    box.addButton("Not Now", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    clicked = box.clickedButton()
    if discard is not None and clicked is discard:
        cancel_planned_commit(project.conn, info.id)
        return True
    if clicked is not resume:
        return False
    outcome = run_commit(parent, project, source, info.id, checkpoint)
    return outcome is not None and outcome.status in ("completed", "cancelled")
