"""Create or edit a project (docs/product-spec.md §3.2)."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from degreaser.core.project import ProjectSettings


@dataclass
class ProjectForm:
    name: str
    source: dict[str, Any]
    settings: ProjectSettings
    always_delete: list[str]
    always_keep: list[str]


def _patterns(edit: QPlainTextEdit) -> list[str]:
    # Keep leading/trailing spaces inside patterns; only drop blank lines.
    return [line for line in edit.toPlainText().splitlines() if line.strip()]


class ProjectEditDialog(QDialog):
    """`existing` locks the source (changing it would invalidate the cached tree)."""

    def __init__(self, parent: QWidget | None = None, existing: ProjectForm | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Project" if existing else "New Project")
        self.setMinimumWidth(640)
        form = existing or ProjectForm(
            name="",
            source={"type": "local", "path": ""},
            settings=ProjectSettings(),
            always_delete=[],
            always_keep=[],
        )
        layout = QVBoxLayout(self)

        top = QFormLayout()
        self.name_edit = QLineEdit(form.name)
        self.name_edit.setPlaceholderText("e.g. Laptop backups")
        top.addRow("Name", self.name_edit)
        layout.addLayout(top)

        # Source
        source_box = QGroupBox("Source")
        source_layout = QVBoxLayout(source_box)
        type_row = QHBoxLayout()
        self.local_radio = QRadioButton("Local folder")
        smb_radio = QRadioButton("SMB share")
        webdav_radio = QRadioButton("WebDAV")
        for radio, tip in ((smb_radio, "Coming in milestone 2"), (webdav_radio, "Coming later")):
            radio.setEnabled(False)
            radio.setToolTip(tip)
        self.local_radio.setChecked(True)
        group = QButtonGroup(self)
        for radio in (self.local_radio, smb_radio, webdav_radio):
            group.addButton(radio)
            type_row.addWidget(radio)
        type_row.addStretch()
        source_layout.addLayout(type_row)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(form.source.get("path", ""))
        self.path_edit.setPlaceholderText("Folder to triage (a mounted share works too)")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        source_layout.addLayout(path_row)
        if existing:
            for w in (self.local_radio, self.path_edit, browse):
                w.setEnabled(False)
            source_layout.addWidget(
                QLabel("The source can't be changed after a project is created.")
            )
        layout.addWidget(source_box)

        # Settings
        settings_box = QGroupBox("Settings")
        settings_layout = QVBoxLayout(settings_box)
        s = form.settings
        self.retriage = QCheckBox("Re-triage files that changed since they were triaged")
        self.retriage.setChecked(s.retriage_changed)
        self.remove_empty = QCheckBox("Remove folders left empty by commit")
        self.remove_empty.setChecked(s.remove_empty_dirs)
        self.jump_back = QCheckBox("Jump back to skipped files once their preview has loaded")
        self.jump_back.setChecked(s.jump_back)
        self.jump_back.setToolTip("Only matters for remote sources, where previews can be slow.")
        self.sync = QCheckBox("Sync project state to the project folder")
        self.sync.setChecked(s.sync_enabled)
        self.sync.setEnabled(False)
        self.sync.setToolTip("Coming in milestone 2")
        for w in (self.retriage, self.remove_empty, self.jump_back, self.sync):
            settings_layout.addWidget(w)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("When committing deletions:"))
        self.mode_trash = QRadioButton("Move to Degreaser trash")
        self.mode_permanent = QRadioButton("Delete permanently")
        permanent = s.deletion_mode == "permanent"
        (self.mode_permanent if permanent else self.mode_trash).setChecked(True)
        mode_group = QButtonGroup(self)
        for radio in (self.mode_trash, self.mode_permanent):
            mode_group.addButton(radio)
            mode_row.addWidget(radio)
        mode_row.addStretch()
        settings_layout.addLayout(mode_row)
        layout.addWidget(settings_box)

        # Rules
        rules_box = QGroupBox("Rules (gitignore syntax, one pattern per line)")
        rules_layout = QHBoxLayout(rules_box)
        self.delete_rules = QPlainTextEdit("\n".join(form.always_delete))
        self.keep_rules = QPlainTextEdit("\n".join(form.always_keep))
        for title, edit, hint in (
            ("Always delete", self.delete_rules, "node_modules/\n*.tmp"),
            ("Always keep", self.keep_rules, "*.psd\nPhotos/"),
        ):
            column = QVBoxLayout()
            column.addWidget(QLabel(f"<b>{title}</b>"))
            edit.setPlaceholderText(hint)
            column.addWidget(edit)
            rules_layout.addLayout(column)
        layout.addWidget(rules_box, 1)

        self.error_label = QLabel()
        self.error_label.setStyleSheet("color: palette(link);")
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        save = buttons.button(QDialogButtonBox.StandardButton.Save)
        if save is not None:
            save.setProperty("variant", "primary")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose folder", self.path_edit.text())
        if folder:
            self.path_edit.setText(folder)
            if not self.name_edit.text():
                self.name_edit.setText(Path(folder).name)

    def _accept(self) -> None:
        if not self.name_edit.text().strip():
            self.error_label.setText("Give the project a name.")
            return
        if self.path_edit.isEnabled() and not Path(self.path_edit.text()).is_dir():
            self.error_label.setText("Choose a folder that exists.")
            return
        self.accept()

    def result_form(self) -> ProjectForm:
        return ProjectForm(
            name=self.name_edit.text().strip(),
            source={"type": "local", "path": str(Path(self.path_edit.text()).resolve())},
            settings=ProjectSettings(
                jump_back=self.jump_back.isChecked(),
                retriage_changed=self.retriage.isChecked(),
                deletion_mode="permanent" if self.mode_permanent.isChecked() else "trash",
                remove_empty_dirs=self.remove_empty.isChecked(),
                sync_enabled=self.sync.isChecked(),
            ),
            always_delete=_patterns(self.delete_rules),
            always_keep=_patterns(self.keep_rules),
        )
