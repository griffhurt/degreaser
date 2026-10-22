"""Create or edit a project (docs/product-spec.md §3.2)."""

from dataclasses import dataclass, field
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
from degreaser.core.rules import validate_pattern
from degreaser.core.rules.presets import Preset, RuleConfig, RuleKind, presets_for


@dataclass
class ProjectForm:
    name: str
    source: dict[str, Any]
    settings: ProjectSettings
    rules: RuleConfig = field(default_factory=RuleConfig)

    @property
    def always_delete(self) -> list[str]:
        return self.rules.patterns("always_delete")

    @property
    def always_keep(self) -> list[str]:
        return self.rules.patterns("always_keep")


def _to_text(patterns: list[str]) -> str:
    # A real carriage return (macOS "Icon\r") would become a line break in the editor; show it
    # as the two characters \r instead, and turn it back in _patterns().
    return "\n".join(p.replace("\r", "\\r") for p in patterns)


def _patterns(edit: QPlainTextEdit) -> list[str]:
    # Keep leading/trailing spaces inside patterns; only drop blank lines.
    lines = edit.toPlainText().split("\n")
    return [line.replace("\\r", "\r") for line in lines if line.strip()]


class _RuleColumn(QWidget):
    """One list (always delete / always keep): preset checkboxes plus a custom-pattern note."""

    def __init__(self, kind: RuleKind, title: str) -> None:
        super().__init__()
        self.kind = kind
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        heading = QLabel(title)
        heading.setProperty("role", "heading")
        layout.addWidget(heading)
        self.checks: dict[str, QCheckBox] = {}
        for preset in presets_for(kind):
            layout.addWidget(self._row(preset))
        self.custom_label = QLabel()
        self.custom_label.setProperty("role", "muted")
        self.custom_label.setWordWrap(True)
        layout.addWidget(self.custom_label)
        layout.addStretch()
        self.custom: list[str] = []

    def _row(self, preset: Preset) -> QWidget:
        # Compact rows (checkbox + one short line) so the dialog fits on a laptop screen; the
        # full description and every pattern live in the tooltip.
        row = QWidget()
        layout = QVBoxLayout(row)
        layout.setContentsMargins(0, 1, 0, 1)
        layout.setSpacing(0)
        check = QCheckBox(preset.name)
        patterns = "\n".join(p.replace("\r", "\\r") for p in preset.patterns)
        row.setToolTip(f"{preset.description}\n\nPatterns:\n{patterns}")
        check.setToolTip(row.toolTip())
        summary = QLabel(preset.summary or preset.description)
        summary.setProperty("role", "faint")
        summary.setContentsMargins(26, 0, 0, 0)  # line up under the checkbox label
        layout.addWidget(check)
        layout.addWidget(summary)
        row.setMinimumHeight(row.sizeHint().height())  # never squeezed into overlap
        self.checks[preset.id] = check
        return row

    def load(self, config: RuleConfig) -> None:
        for preset_id, check in self.checks.items():
            check.setChecked(preset_id in config.presets)
        self.custom = list(
            config.custom_delete if self.kind == "always_delete" else config.custom_keep
        )
        if self.custom:
            count = len(self.custom)
            self.custom_label.setText(
                f"+ {count} custom pattern{'' if count == 1 else 's'}: "
                + ", ".join(self.custom[:6])
                + ("…" if count > 6 else "")
            )
            self.custom_label.setToolTip("\n".join(self.custom))
        else:
            self.custom_label.setText("No custom patterns. Use “Edit as text” to add your own.")
            self.custom_label.setToolTip("")

    def selected(self) -> set[str]:
        return {pid for pid, check in self.checks.items() if check.isChecked()}


class ProjectEditDialog(QDialog):
    """`existing` locks the source (changing it would invalidate the cached tree)."""

    def __init__(self, parent: QWidget | None = None, existing: ProjectForm | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Project" if existing else "New Project")
        self.setMinimumWidth(720)
        form = existing or ProjectForm(
            name="",
            source={"type": "local", "path": ""},
            settings=ProjectSettings(),
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
            note = QLabel("The source can't be changed after a project is created.")
            note.setProperty("role", "faint")
            source_layout.addWidget(note)
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

        # Rules: preset checkboxes, or the full lists as text.
        rules_box = QGroupBox("Rules")
        rules_layout = QVBoxLayout(rules_box)
        header = QHBoxLayout()
        explain = QLabel("Matching files are decided automatically and never shown in triage.")
        explain.setProperty("role", "muted")
        explain.setWordWrap(True)
        header.addWidget(explain, 1)
        self.text_toggle = QPushButton("Edit as Text")
        self.text_toggle.setCheckable(True)
        self.text_toggle.setProperty("variant", "ghost")
        self.text_toggle.toggled.connect(self._toggle_text_mode)
        header.addWidget(self.text_toggle)
        rules_layout.addLayout(header)

        # Two pages toggled by visibility. (QStackedWidget would drop the wrapped preset
        # descriptions' height-for-width, squashing the rows on top of each other.)
        presets_page = QWidget()
        columns = QHBoxLayout(presets_page)
        columns.setContentsMargins(0, 4, 0, 0)
        columns.setSpacing(24)
        self.delete_column = _RuleColumn("always_delete", "Always delete")
        self.keep_column = _RuleColumn("always_keep", "Always keep")
        columns.addWidget(self.delete_column, 1)
        columns.addWidget(self.keep_column, 1)
        self.presets_page = presets_page
        rules_layout.addWidget(presets_page)

        text_page = QWidget()
        text_layout = QVBoxLayout(text_page)
        text_layout.setContentsMargins(0, 4, 0, 0)
        hint = QLabel(
            "gitignore syntax, one pattern per line. Presets are switched on when all "
            "of their patterns are present."
        )
        hint.setProperty("role", "faint")
        hint.setWordWrap(True)
        text_layout.addWidget(hint)
        text_columns = QHBoxLayout()
        self.delete_rules = QPlainTextEdit()
        self.keep_rules = QPlainTextEdit()
        for title, edit, example in (
            ("Always delete", self.delete_rules, "node_modules/\n*.tmp"),
            ("Always keep", self.keep_rules, "*.psd\nPhotos/"),
        ):
            column = QVBoxLayout()
            heading = QLabel(title)
            heading.setProperty("role", "heading")
            column.addWidget(heading)
            edit.setPlaceholderText(example)
            edit.setProperty("mono", True)
            column.addWidget(edit)
            text_columns.addLayout(column)
        text_layout.addLayout(text_columns, 1)
        self.text_page = text_page
        text_page.hide()
        rules_layout.addWidget(text_page)
        layout.addWidget(rules_box, 1)

        self._config = form.rules
        self.delete_column.load(self._config)
        self.keep_column.load(self._config)

        self.error_label = QLabel()
        self.error_label.setProperty("role", "error")
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

    # Rules ------------------------------------------------------------------------------------

    def _config_from_checkboxes(self) -> RuleConfig:
        return RuleConfig(
            presets=self.delete_column.selected() | self.keep_column.selected(),
            custom_delete=list(self.delete_column.custom),
            custom_keep=list(self.keep_column.custom),
        )

    def _toggle_text_mode(self, text: bool) -> None:
        if text:
            config = self._config_from_checkboxes()
            self.delete_rules.setPlainText(_to_text(config.patterns("always_delete")))
            self.keep_rules.setPlainText(_to_text(config.patterns("always_keep")))
            self.presets_page.hide()
            self.text_page.show()
            self.text_toggle.setText("Show Presets")
        else:
            config = RuleConfig.infer(_patterns(self.delete_rules), _patterns(self.keep_rules))
            self.delete_column.load(config)
            self.keep_column.load(config)
            self.text_page.hide()
            self.presets_page.show()
            self.text_toggle.setText("Edit as Text")

    def rule_config(self) -> RuleConfig:
        if self.text_toggle.isChecked():
            return RuleConfig.infer(_patterns(self.delete_rules), _patterns(self.keep_rules))
        return self._config_from_checkboxes()

    # Dialog -----------------------------------------------------------------------------------

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
        config = self.rule_config()
        for pattern in config.patterns("always_delete") + config.patterns("always_keep"):
            if error := validate_pattern(pattern):
                self.error_label.setText(f"Rule “{pattern}”: {error}")
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
            rules=self.rule_config(),
        )
