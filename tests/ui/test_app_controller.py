from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox
from pytestqt.qtbot import QtBot

from degreaser.core.project import Project
from degreaser.core.scan import list_dir_into_db
from degreaser.core.settings import Settings
from degreaser.ui.controllers.app_controller import AppController


def test_reopen_runs_rescan_then_opens_triage(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_warnings(_parent: object, title: str, text: str, *args: object) -> None:
        raise AssertionError(f"unexpected warning: {title}: {text}")

    monkeypatch.setattr(QMessageBox, "warning", no_warnings)
    root = tmp_path / "files"
    root.mkdir()
    (root / "a.txt").write_text("a")
    project = Project.create("P", {"type": "local", "path": str(root)})
    list_dir_into_db(project.conn, project.make_source(), "")  # listed root ⇒ reopen rescans
    project.close()
    (root / "new.txt").write_text("new")

    controller = AppController(Settings())
    qtbot.addWidget(controller.projects_window)
    controller.open_project(project.uuid)
    qtbot.waitUntil(lambda: project.uuid in controller.triage_windows, timeout=5000)
    window = controller.triage_windows[project.uuid]
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.session.current is not None, timeout=5000)
    current = window.session.current
    assert current is not None and current.path == "new.txt"  # new files come first
    window.close()
