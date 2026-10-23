"""Opening synced projects: lock prompts, read-only, take over, conflicts, Add Existing."""

import json
import time
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox
from pytestqt.qtbot import QtBot

from degreaser.core.db.connection import bump_revision
from degreaser.core.project import Project
from degreaser.core.settings import Device, Settings
from degreaser.core.sources.local import LocalSource
from degreaser.core.sync import LOCK_PATH, SyncService
from degreaser.ui.controllers.app_controller import AppController
from degreaser.ui.dialogs.project_edit import ProjectEditDialog
from degreaser.ui.windows.triage import TriageWindow
from tests.ui.test_triage_e2e import ModalResponder, click_button

TIMEOUT = 5000
OTHER = Device("other-device", "Desktop")


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    root = tmp_path / "files"
    root.mkdir()
    (root / "a.txt").write_text("a")
    (root / "b.txt").write_text("b")
    return root


def other_device_lock(folder: Path, heartbeat: float | None = None) -> None:
    now = heartbeat or time.time()
    lock = {
        "device_id": OTHER.id,
        "device_name": OTHER.name,
        "acquired_at": now,
        "heartbeat_at": now,
        "app_version": "x",
    }
    (folder / ".degreaser" / "state").mkdir(parents=True, exist_ok=True)
    (folder / LOCK_PATH).write_text(json.dumps(lock))


def open_with(
    qtbot: QtBot, controller: AppController, project_uuid: str, answers: list[str]
) -> TriageWindow:
    responder = ModalResponder([click_button(a) for a in answers])
    try:
        controller.open_project(project_uuid)
        qtbot.waitUntil(lambda: project_uuid in controller.triage_windows, timeout=TIMEOUT)
    finally:
        responder.stop()
    window = controller.triage_windows[project_uuid]
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.session.current is not None, timeout=TIMEOUT)
    return window


@pytest.fixture
def controller(qtbot: QtBot) -> AppController:
    controller = AppController(Settings(device=Device("this-device", "Laptop")))
    qtbot.addWidget(controller.projects_window)
    return controller


def test_open_checks_out_pushes_and_releases_on_close(
    qtbot: QtBot, controller: AppController, folder: Path
) -> None:
    project = Project.create("P", {"type": "local", "path": str(folder)})
    project.close()
    window = open_with(qtbot, controller, project.uuid, [])
    lock = json.loads((folder / LOCK_PATH).read_text())
    assert lock["device_name"] == "Laptop"
    assert (folder / ".degreaser" / "state" / "state.json").exists()  # first open pushes
    assert not window.read_only

    qtbot.keyClick(window, Qt.Key.Key_K)
    window.close()
    assert not (folder / LOCK_PATH).exists()
    header = json.loads((folder / ".degreaser" / "state" / "state.json").read_text())
    assert header["revision"] >= 1


def test_lock_held_open_read_only(qtbot: QtBot, controller: AppController, folder: Path) -> None:
    project = Project.create("P", {"type": "local", "path": str(folder)})
    project.close()
    other_device_lock(folder)
    window = open_with(qtbot, controller, project.uuid, ["Open Read-Only"])
    assert window.read_only and "Desktop" in window.mode_label.text()
    qtbot.keyClick(window, Qt.Key.Key_K)
    assert window.session.state_of("a.txt").action.value == "untriaged"
    window.close()
    assert json.loads((folder / LOCK_PATH).read_text())["device_id"] == OTHER.id  # untouched


def test_lock_held_take_over(qtbot: QtBot, controller: AppController, folder: Path) -> None:
    project = Project.create("P", {"type": "local", "path": str(folder)})
    project.close()
    other_device_lock(folder, heartbeat=time.time() - 3600)  # stale
    window = open_with(qtbot, controller, project.uuid, ["Take Over…", "Yes"])
    assert not window.read_only
    assert json.loads((folder / LOCK_PATH).read_text())["device_name"] == "Laptop"
    window.close()


def test_conflict_keep_the_other_devices_copy(
    qtbot: QtBot, controller: AppController, folder: Path, tmp_path: Path
) -> None:
    project = Project.create("P", {"type": "local", "path": str(folder)})
    sync = SyncService(
        LocalSource(folder), project.uuid, OTHER, app_version="x", scratch_dir=tmp_path / "s"
    )
    sync.push(project.conn)  # shared baseline
    # Another device's newer state: decided b.txt...
    project.conn.execute(
        "INSERT INTO decisions(path, is_dir, action, decided_at) VALUES ('b.txt', 0, 'delete', 1)"
    )
    bump_revision(project.conn)
    sync.push(project.conn)
    # ...while this device, still on the baseline, decided a.txt.
    project.conn.execute("DELETE FROM decisions")
    project.conn.execute(
        "INSERT INTO decisions(path, is_dir, action, decided_at) VALUES ('a.txt', 0, 'keep', 1)"
    )
    project.conn.execute("""UPDATE meta SET value = '"stale-base"' WHERE key = 'sync_base_id'""")
    bump_revision(project.conn)
    project.close()

    window = open_with(qtbot, controller, project.uuid, ["Keep Desktop's"])
    assert window.session.state_of("b.txt").action.value == "delete"
    assert window.session.state_of("a.txt").action.value == "untriaged"
    window.close()


def test_lock_lost_makes_the_window_read_only(
    qtbot: QtBot, controller: AppController, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _p, title, text, *a: warnings.append(text))
    project = Project.create("P", {"type": "local", "path": str(folder)})
    project.close()
    window = open_with(qtbot, controller, project.uuid, [])
    other_device_lock(folder)  # someone took over
    assert window.sync is not None
    window.sync._heartbeat()
    qtbot.waitUntil(lambda: window.read_only, timeout=TIMEOUT)
    assert warnings and "Desktop took over" in warnings[0]
    window.close()


def test_add_existing_project_pulls_its_state(
    qtbot: QtBot, controller: AppController, folder: Path, tmp_path: Path
) -> None:
    original = Project.create("Old laptop", {"type": "local", "path": str(folder)})
    original.conn.execute(
        "INSERT INTO decisions(path, is_dir, action, decided_at) VALUES ('a.txt', 0, 'delete', 1)"
    )
    bump_revision(original.conn)
    SyncService(
        LocalSource(folder), original.uuid, OTHER, app_version="x", scratch_dir=tmp_path / "s"
    ).push(original.conn)
    original.close()
    from degreaser.core.project import remove_project

    remove_project(original.uuid)  # this "device" has never seen it

    dialog = ProjectEditDialog(adding_existing=True)
    qtbot.addWidget(dialog)
    dialog.path_edit.setText(str(folder))
    dialog._accept()
    qtbot.waitUntil(lambda: dialog.found is not None, timeout=TIMEOUT)
    assert dialog.result_form().name == "Old laptop"

    found = dialog.found
    assert found is not None
    project = Project.create(
        "Old laptop", dialog.result_form().source, project_uuid=found.project_uuid
    )
    project.close()
    window = open_with(qtbot, controller, project.uuid, [])
    assert window.session.state_of("a.txt").action.value == "delete"
    assert window.session.current is not None and window.session.current.path == "b.txt"
    window.close()


def test_add_existing_rejects_a_folder_without_state(qtbot: QtBot, folder: Path) -> None:
    dialog = ProjectEditDialog(adding_existing=True)
    qtbot.addWidget(dialog)
    dialog.path_edit.setText(str(folder))
    dialog._accept()
    qtbot.waitUntil(lambda: bool(dialog.error_label.text()), timeout=TIMEOUT)
    assert "no saved Degreaser project" in dialog.error_label.text()
