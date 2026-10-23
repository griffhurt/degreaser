from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QMenu
from pytestqt.qtbot import QtBot

from degreaser.core import settings as settings_mod
from degreaser.core.project import Project
from degreaser.core.settings import DEFAULT_KEYMAP, Settings
from degreaser.ui.dialogs.preferences import PreferencesDialog
from degreaser.ui.dialogs.project_edit import ProjectEditDialog
from degreaser.ui.keymap import Keymap
from degreaser.ui.theme import theme_override
from degreaser.ui.windows.projects import new_project_settings
from degreaser.ui.windows.triage import TriageWindow


def test_result_reflects_every_page(qtbot: QtBot) -> None:
    dialog = PreferencesDialog(Settings())
    qtbot.addWidget(dialog)
    dialog.theme_combo.setCurrentIndex(dialog.theme_combo.findData("dark"))
    dialog.device_name.setText("  Study iMac ")
    dialog.check_updates.setChecked(False)
    dialog.default_sync.setChecked(False)
    dialog.default_permanent.setChecked(True)
    dialog.auto_load.setValue(100)
    dialog.large_warning.setValue(2)
    dialog.max_concurrent.setValue(5)
    dialog.autoplay.setChecked(True)

    s = dialog.result_settings()
    assert s.appearance.theme == "dark"
    assert s.device.name == "Study iMac"
    assert not s.updates.check_automatically
    assert not s.defaults.sync_enabled
    assert s.defaults.deletion_mode == "permanent"
    assert s.downloads.auto_load_bytes == 100 * 1024**2
    assert s.downloads.large_warning_bytes == 2 * 1024**3
    assert s.downloads.max_concurrent == 5
    assert s.video.autoplay_muted


def test_cancelled_edits_leave_settings_alone(qtbot: QtBot) -> None:
    settings = Settings()
    dialog = PreferencesDialog(settings)
    qtbot.addWidget(dialog)
    dialog.auto_load.setValue(1)
    dialog._rebind("keep", QKeySequence("J"))
    assert settings.downloads.auto_load_bytes == 250 * 1024**2
    assert settings.keymap["keep"] == "K"


def test_recording_a_shortcut(qtbot: QtBot) -> None:
    dialog = PreferencesDialog(Settings())
    qtbot.addWidget(dialog)
    dialog.show()
    recorder = dialog.recorders["keep"]
    recorder.click()
    assert recorder.recording and recorder.text() == "Press a shortcut…"
    qtbot.keyClick(recorder, Qt.Key.Key_Shift)  # a lone modifier keeps waiting
    assert recorder.recording
    qtbot.keyClick(recorder, Qt.Key.Key_Tab)  # unbindable: ignored, still recording
    assert recorder.recording
    qtbot.keyClick(recorder, Qt.Key.Key_J, Qt.KeyboardModifier.AltModifier)
    assert not recorder.recording
    assert dialog.result_settings().keymap["keep"] == "Alt+J"
    assert dialog.reset_buttons["keep"].isEnabled()

    recorder.click()
    qtbot.keyClick(recorder, Qt.Key.Key_Escape)  # cancels without changing it
    assert not recorder.recording
    assert dialog.result_settings().keymap["keep"] == "Alt+J"

    dialog.reset_buttons["keep"].click()
    assert dialog.result_settings().keymap["keep"] == "K"
    assert not dialog.reset_buttons["keep"].isEnabled()


def test_conflicts_block_saving(qtbot: QtBot) -> None:
    dialog = PreferencesDialog(Settings())
    qtbot.addWidget(dialog)
    dialog._rebind("skip", QKeySequence("K"))
    assert not dialog.save_button.isEnabled()
    assert dialog.recorders["keep"].property("invalid") is True
    assert dialog.recorders["skip"].property("invalid") is True
    assert "used by both Keep and Skip" in dialog.error_label.text()

    dialog._rebind("skip", QKeySequence("Ctrl+,"))  # the Preferences shortcut
    assert not dialog.save_button.isEnabled()
    assert "Preferences shortcut" in dialog.error_label.text()

    dialog.reset_all_button.click()
    assert dialog.save_button.isEnabled()
    assert dialog.error_label.text() == ""
    assert dialog.result_settings().keymap == DEFAULT_KEYMAP


def test_keymap_problems() -> None:
    assert Keymap(DEFAULT_KEYMAP).problems() == []
    assert Keymap({**DEFAULT_KEYMAP, "tag": "Ctrl+Q"}).problems() != []


def test_update_from_keeps_shared_parts(tmp_path: Path) -> None:
    settings = Settings()
    downloads = settings.downloads  # windows hold this object
    other = Settings()
    other.downloads.auto_load_bytes = 1
    other.keymap["keep"] = "J"
    other.appearance.theme = "light"
    settings.update_from(other)
    assert settings.downloads is downloads and downloads.auto_load_bytes == 1
    assert settings.keymap["keep"] == "J"
    assert settings.appearance.theme == "light"

    path = tmp_path / "settings.json"
    settings_mod.save(settings, path)
    loaded = settings_mod.load(path)
    assert loaded.appearance.theme == "light"
    assert loaded.updates.check_automatically


def test_theme_override() -> None:
    assert theme_override("system") is None
    assert theme_override("dark") is True
    assert theme_override("light") is False


def test_new_projects_start_from_defaults(qtbot: QtBot) -> None:
    settings = Settings()
    settings.defaults.sync_enabled = False
    settings.defaults.deletion_mode = "permanent"
    dialog = ProjectEditDialog(defaults=new_project_settings(settings))
    qtbot.addWidget(dialog)
    assert not dialog.sync.isChecked()
    assert dialog.mode_permanent.isChecked()


def test_open_window_picks_up_new_keymap(qtbot: QtBot, tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    settings = Settings()
    project = Project.create("P", {"type": "local", "path": str(tmp_path)})
    window = TriageWindow(project, settings)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window.session.current is not None, timeout=5000)

    changed = Settings()
    changed.keymap["keep"] = "J"
    settings.update_from(changed)
    window.apply_preferences()
    assert window.shortcuts["keep"].key().toString() == "J"
    assert window.action_bar.buttons["keep"].key == "J"
    qtbot.keyClick(window, Qt.Key.Key_J)
    qtbot.waitUntil(
        lambda: window.session.current is not None and window.session.current.path == "b.txt",
        timeout=5000,
    )


def test_triage_window_has_a_help_menu(qtbot: QtBot, tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    project = Project.create("P", {"type": "local", "path": str(tmp_path)})
    window = TriageWindow(project, Settings())
    qtbot.addWidget(window)
    menus = {a.text().replace("&", ""): a.menu() for a in window.menuBar().actions()}
    help_menu = menus["Help"]
    assert isinstance(help_menu, QMenu)
    labels = [a.text() for a in help_menu.actions()]
    assert labels == [
        "Degreaser User Guide",
        "Release Notes",
        "Report a Problem…",
        "",
        "About Degreaser",
    ]


def test_run_modal_deletes_the_dialog(qtbot: QtBot) -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QWidget

    from degreaser.ui.dialogs.modal import run_modal

    parent = QWidget()
    qtbot.addWidget(parent)
    dialog = PreferencesDialog(Settings(), parent)
    QTimer.singleShot(0, dialog.reject)
    with qtbot.waitSignal(dialog.destroyed, timeout=2000):
        assert run_modal(dialog) is False
