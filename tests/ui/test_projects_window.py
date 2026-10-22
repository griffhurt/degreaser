from pathlib import Path

from pytestqt.qtbot import QtBot

from degreaser.core.project import Project, ProjectSettings
from degreaser.ui.dialogs.project_edit import ProjectEditDialog, ProjectForm
from degreaser.ui.windows.projects import ProjectsWindow


def test_lists_projects_and_opens(qtbot: QtBot, tmp_path: Path) -> None:
    project = Project.create("Old laptop", {"type": "local", "path": str(tmp_path)})
    project.set_meta(
        "last_summary", {"files_known": 200, "files_triaged": 50, "bytes_delete": 3 * 1024**3}
    )
    project.close()

    window = ProjectsWindow()
    qtbot.addWidget(window)
    [card] = window.cards
    assert card.name_label.text() == "Old laptop"
    assert card.progress_label.text() == "25% triaged"
    assert card.delete_label.text() == "3.0 GB to delete"

    with qtbot.waitSignal(window.open_requested) as blocker:
        window.open_button.click()
    assert blocker.args == [project.uuid]


def test_empty_state(qtbot: QtBot) -> None:
    window = ProjectsWindow()
    qtbot.addWidget(window)
    window.show()
    assert window.empty_label.isVisible()
    assert not window.open_button.isEnabled()


def test_dialog_new_project_form(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = ProjectEditDialog()
    qtbot.addWidget(dialog)
    dialog.name_edit.setText("  Backups ")
    dialog.path_edit.setText(str(tmp_path))
    dialog.mode_permanent.setChecked(True)
    dialog.text_toggle.setChecked(True)  # "Edit as Text": replaces the whole lists
    dialog.delete_rules.setPlainText("node_modules/\n\n*.tmp\n")
    dialog.keep_rules.setPlainText("*.psd")
    form = dialog.result_form()
    assert form.name == "Backups"
    assert form.source == {"type": "local", "path": str(tmp_path.resolve())}
    assert form.settings.deletion_mode == "permanent"
    assert form.always_delete == ["node_modules/", "*.tmp"]
    assert form.always_keep == ["*.psd"]


def test_dialog_rejects_missing_folder(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = ProjectEditDialog()
    qtbot.addWidget(dialog)
    dialog.name_edit.setText("x")
    dialog.path_edit.setText(str(tmp_path / "nope"))
    dialog._accept()
    assert "exists" in dialog.error_label.text()


def test_dialog_existing_locks_source(qtbot: QtBot, tmp_path: Path) -> None:
    form = ProjectForm("P", {"type": "local", "path": str(tmp_path)}, ProjectSettings())
    dialog = ProjectEditDialog(existing=form)
    qtbot.addWidget(dialog)
    assert not dialog.path_edit.isEnabled()
    assert dialog.result_form().source["path"] == str(tmp_path.resolve())


def test_replace_rules_round_trip(tmp_path: Path) -> None:
    project = Project.create("P", {"type": "local", "path": str(tmp_path)})
    project.replace_rules(["node_modules/"], ["*.psd", "Photos/"])
    assert project.rules() == (["node_modules/"], ["*.psd", "Photos/"])
    project.close()


def test_empty_state_keeps_header_at_top(qtbot: QtBot) -> None:
    window = ProjectsWindow()
    qtbot.addWidget(window)
    window.resize(900, 600)
    window.show()
    qtbot.waitExposed(window)
    title = window.title_label
    assert title.y() < 40
    assert title.height() <= title.sizeHint().height() + 2  # text isn't centred in a tall box


def test_dialog_presets_and_text_round_trip(qtbot: QtBot) -> None:
    from degreaser.core.rules.presets import PRESETS_BY_ID

    dialog = ProjectEditDialog()
    qtbot.addWidget(dialog)
    # Defaults: only System files is on.
    assert dialog.delete_column.selected() == {"system-files"}
    dialog.delete_column.checks["dependencies"].setChecked(True)
    dialog.keep_column.checks["photos"].setChecked(True)
    config = dialog.rule_config()
    assert "node_modules/" in config.patterns("always_delete")
    assert "*.heic" in config.patterns("always_keep")

    # Text view shows the flattened lists; edits there flow back into the presets.
    dialog.text_toggle.setChecked(True)
    text = dialog.delete_rules.toPlainText().splitlines()
    assert ".DS_Store" in text and "node_modules/" in text
    removed = set(PRESETS_BY_ID["dependencies"].patterns)
    dialog.delete_rules.setPlainText("\n".join([*(p for p in text if p not in removed), "*.bak"]))
    dialog.text_toggle.setChecked(False)
    assert dialog.delete_column.selected() == {"system-files"}
    assert dialog.delete_column.custom == ["*.bak"]
    assert "1 custom pattern" in dialog.delete_column.custom_label.text()


def test_dialog_rejects_invalid_pattern(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = ProjectEditDialog()
    qtbot.addWidget(dialog)
    dialog.name_edit.setText("x")
    dialog.path_edit.setText(str(tmp_path))
    dialog.text_toggle.setChecked(True)
    dialog.delete_rules.setPlainText("!keep-me")
    dialog._accept()
    assert "Negated" in dialog.error_label.text()


def test_text_mode_keeps_carriage_return_patterns(qtbot: QtBot) -> None:
    # Regression: "Icon\r" (a System files pattern) turned into a line break in the text
    # editor, so toggling to text and back silently switched the preset off.
    dialog = ProjectEditDialog()
    qtbot.addWidget(dialog)
    dialog.text_toggle.setChecked(True)
    assert "Icon\\r" in dialog.delete_rules.toPlainText()
    dialog.text_toggle.setChecked(False)
    assert dialog.delete_column.selected() == {"system-files"}
    assert dialog.delete_column.custom == []
