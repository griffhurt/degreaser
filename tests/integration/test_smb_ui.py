"""Project dialog and opening sequence against the Samba container."""

from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from degreaser.core import credentials
from degreaser.core.project import Project
from degreaser.core.settings import Settings
from degreaser.ui.controllers.app_controller import AppController
from degreaser.ui.dialogs.project_edit import ProjectEditDialog
from tests.integration.conftest import SmbTestServer

pytestmark = pytest.mark.smb


def _fill(dialog: ProjectEditDialog, server: SmbTestServer, workdir: str, password: str) -> None:
    dialog.smb_radio.setChecked(True)
    dialog.name_edit.setText("NAS")
    dialog.host_edit.setText(f"{server.host}:{server.port}")
    dialog.share_edit.setText("data")
    dialog.share_path_edit.setText(workdir.rsplit("\\", 1)[1])
    dialog.username_edit.setText(server.username)
    dialog.password_edit.setText(password)


def test_test_connection_and_save(
    qtbot: QtBot, smb_server: SmbTestServer, smb_workdir: str
) -> None:
    dialog = ProjectEditDialog()
    qtbot.addWidget(dialog)
    _fill(dialog, smb_server, smb_workdir, "wrong")
    dialog.test_button.click()
    qtbot.waitUntil(lambda: dialog.test_button.isEnabled(), timeout=10000)
    assert "rejected" in dialog.test_label.text()

    dialog.password_edit.setText(smb_server.password)
    dialog.password_edit.textEdited.emit(smb_server.password)
    dialog._accept()  # runs the test first, then accepts
    qtbot.waitUntil(lambda: dialog.result() == ProjectEditDialog.DialogCode.Accepted, timeout=10000)
    assert dialog.test_label.text().startswith("Connected. 0 items")


def test_open_smb_project(qtbot: QtBot, smb_server: SmbTestServer, smb_workdir: str) -> None:
    source = {
        "type": "smb",
        "host": smb_server.host,
        "port": smb_server.port,
        "share": "data",
        "path": smb_workdir.rsplit("\\", 1)[1],
        "username": smb_server.username,
    }
    project = Project.create("NAS", source)
    credentials.set_password(project.uuid, smb_server.password)
    src = project.make_source()
    src.connect()
    src.write_file_atomic("hello.txt", b"hello")
    src.close()
    project.close()

    controller = AppController(Settings())
    qtbot.addWidget(controller.projects_window)
    controller.open_project(project.uuid)
    qtbot.waitUntil(lambda: project.uuid in controller.triage_windows, timeout=10000)
    window = controller.triage_windows[project.uuid]
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.session.current is not None, timeout=10000)
    assert window.session.current is not None and window.session.current.path == "hello.txt"
    window.close()


def test_smb_video_streams_through_the_media_server(
    qtbot: QtBot, smb_server: SmbTestServer, smb_workdir: str, tmp_path: Path
) -> None:
    from degreaser.core.preview.media_server import MediaServer
    from degreaser.core.sources.smb import SmbConfig, SmbSource
    from degreaser.ui.preview.media import MediaView
    from degreaser.ui.preview.pane import PreviewPane
    from tests.fixtures.preview.helpers import ffmpeg_available, make_video

    if not ffmpeg_available():
        pytest.skip("ffmpeg CLI not installed")
    clip = make_video(tmp_path / "clip.avi", "-c:v", "mpeg4", "-c:a", "pcm_s16le", seconds=3)
    folder = smb_workdir.rsplit("\\", 1)[1]
    config = SmbConfig(smb_server.host, "data", folder, smb_server.username, port=smb_server.port)
    source = SmbSource(config, smb_server.password)
    source.connect()
    source.write_file_atomic("clip.avi", clip.read_bytes())
    server = MediaServer()
    pane = PreviewPane(media_server=server)
    qtbot.addWidget(pane)
    loaded: list[str] = []
    pane.loaded.connect(loaded.append)
    pane.show_file(source, "clip.avi", source.stat("clip.avi"))
    qtbot.waitUntil(lambda: loaded == ["clip.avi"], timeout=15000)
    view = pane.view
    assert isinstance(view, MediaView) and view.is_ready()
    assert view.url.scheme() == "http"
    pane.clear()  # teardown while streaming must not hang
    server.close()
    source.close()
