"""Create or edit a project (docs/product-spec.md §3.2)."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from degreaser.core.project import ProjectSettings
from degreaser.core.rules import validate_pattern
from degreaser.core.rules.presets import Preset, RuleConfig, RuleKind, presets_for
from degreaser.core.sources.local import LocalSource
from degreaser.core.sources.smb import (
    SmbConfig,
    SmbSource,
    connected,
    normalise_share_path,
    parse_smb_location,
)
from degreaser.core.sync import RemoteState, probe_existing
from degreaser.ui.controllers.tasks import Task, submit
from degreaser.ui.widgets.screen import fit_to_screen

MB = 1024 * 1024


@dataclass
class ProjectForm:
    name: str
    source: dict[str, Any]
    settings: ProjectSettings
    rules: RuleConfig = field(default_factory=RuleConfig)
    # SMB: a password to save in the OS credential store. None leaves the saved one alone.
    password: str | None = None
    # Why sync couldn't be used last time (device-local), shown under the sync checkbox.
    sync_note: str = ""

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


def sentence(exc: BaseException) -> str:
    """Core error messages are lowercase clauses; show them as a sentence."""
    text = str(exc) or type(exc).__name__
    return text[:1].upper() + text[1:] + ("" if text.endswith((".", ")")) else ".")


def test_connection(config: SmbConfig, password: str) -> str:
    """Connect, list the project folder and describe what was found (runs on a worker)."""
    with connected(config, password) as source:
        count = len(source.list_dir(""))
    where = f"/{config.path}" if config.path else "the share"
    return f"Connected. {count:,} item{'s' if count != 1 else ''} in {where}."


class _Megabytes(QDoubleSpinBox):
    """MB with up to three decimals, shown without trailing zeros ("1 MB", "0.004 MB")."""

    def textFromValue(self, value: float) -> str:
        return f"{value:g}"


class _RuleColumn(QWidget):
    """One list (always delete / always keep) as preset checkboxes. Rules that aren't pure
    presets are only ever shown as text (see ProjectEditDialog._update_text_lock)."""

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
        layout.addStretch()

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

    def selected(self) -> set[str]:
        return {pid for pid, check in self.checks.items() if check.isChecked()}


class ProjectEditDialog(QDialog):
    """`existing` locks the source (changing it would invalidate the cached tree)."""

    def __init__(
        self,
        parent: QWidget | None = None,
        existing: ProjectForm | None = None,
        *,
        adding_existing: bool = False,
    ) -> None:
        """`adding_existing`: only ask where the folder is; its name, settings and rules come
        from the state another device saved there (Add Existing Project)."""
        super().__init__(parent)
        self.adding_existing = adding_existing
        self.found: RemoteState | None = None  # adding_existing: what the folder holds
        title = "Add Existing Project" if adding_existing else "New Project"
        self.setWindowTitle("Edit Project" if existing else title)
        self.setMinimumWidth(720)
        # Everything but the error line and the buttons scrolls: on a short screen the
        # settings and rules don't fit (the buttons must stay reachable).
        outer = QVBoxLayout(self)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setObjectName("dialogScroll")
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("dialogContent")
        self.scroll_area.setWidget(content)
        outer.addWidget(self.scroll_area, 1)
        form = existing or ProjectForm(
            name="",
            source={"type": "local", "path": ""},
            settings=ProjectSettings(),
        )
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 6, 0)  # room for the scroll bar

        if adding_existing:
            intro = QLabel(
                "Choose a project folder that another computer already syncs Degreaser state "
                "to (it has a .degreaser/state folder). Its decisions, rules and settings are "
                "loaded from there."
            )
            intro.setWordWrap(True)
            intro.setProperty("role", "muted")
            layout.addWidget(intro)
        name_row = QWidget()
        top = QFormLayout(name_row)
        top.setContentsMargins(0, 0, 0, 0)
        self.name_edit = QLineEdit(form.name)
        self.name_edit.setPlaceholderText("e.g. Laptop backups")
        top.addRow("Name", self.name_edit)
        layout.addWidget(name_row)

        # Source
        source_box = QGroupBox("Source")
        source_layout = QVBoxLayout(source_box)
        type_row = QHBoxLayout()
        self.local_radio = QRadioButton("Local folder")
        self.smb_radio = QRadioButton("SMB share")
        webdav_radio = QRadioButton("WebDAV")
        webdav_radio.setEnabled(False)
        webdav_radio.setToolTip("Coming later")
        is_smb = form.source.get("type") == "smb"
        (self.smb_radio if is_smb else self.local_radio).setChecked(True)
        group = QButtonGroup(self)
        for radio in (self.local_radio, self.smb_radio, webdav_radio):
            group.addButton(radio)
            type_row.addWidget(radio)
        type_row.addStretch()
        source_layout.addLayout(type_row)

        self.local_page = QWidget()
        path_row = QHBoxLayout(self.local_page)
        path_row.setContentsMargins(0, 0, 0, 0)
        self.path_edit = QLineEdit(form.source.get("path", "") if not is_smb else "")
        self.path_edit.setPlaceholderText("Folder to triage (a mounted share works too)")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        source_layout.addWidget(self.local_page)

        self.smb_page = QWidget()
        smb_form = QFormLayout(self.smb_page)
        smb_form.setContentsMargins(0, 0, 0, 0)
        src = form.source if is_smb else {}
        port = src.get("port")
        self.host_edit = QLineEdit(src.get("host", "") + (f":{port}" if port else ""))
        self.host_edit.setPlaceholderText("nas.local, or paste smb://nas/share/folder")
        self.host_edit.editingFinished.connect(self._split_pasted_location)
        self.share_edit = QLineEdit(src.get("share", ""))
        self.share_edit.setPlaceholderText("e.g. backups")
        self.share_path_edit = QLineEdit(src.get("path", ""))
        self.share_path_edit.setPlaceholderText("Optional, e.g. Laptops/2014")
        self.username_edit = QLineEdit(src.get("username", ""))
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText(
            "Saved in your system keychain — leave blank to keep it" if is_smb and existing else ""
        )
        self.domain_edit = QLineEdit(src.get("domain", ""))
        self.domain_edit.setPlaceholderText("Optional")
        smb_form.addRow("Server", self.host_edit)
        smb_form.addRow("Share", self.share_edit)
        smb_form.addRow("Folder in share", self.share_path_edit)
        smb_form.addRow("Username", self.username_edit)
        smb_form.addRow("Password", self.password_edit)
        smb_form.addRow("Domain", self.domain_edit)
        self.encrypt_check = QCheckBox("Always encrypt")
        self.encrypt_check.setChecked(bool(src.get("encrypt", False)))
        self.encrypt_check.setToolTip(
            "Encrypt everything sent to and from the server, even if it doesn't ask for it. "
            "Worth it on networks you don't trust; needs SMB 3. Otherwise the server decides, "
            "and traffic is signed so it can't be tampered with."
        )
        self.encrypt_check.toggled.connect(self._connection_changed)
        smb_form.addRow("", self.encrypt_check)
        test_row = QHBoxLayout()
        self.test_button = QPushButton("Test Connection")
        self.test_button.clicked.connect(self._test_connection)
        self.test_label = QLabel()
        self.test_label.setProperty("role", "muted")
        self.test_label.setWordWrap(True)
        test_row.addWidget(self.test_button)
        test_row.addWidget(self.test_label, 1)
        smb_form.addRow("", test_row)
        source_layout.addWidget(self.smb_page)
        for edit in (self.host_edit, self.share_edit, self.share_path_edit, self.username_edit):
            edit.textEdited.connect(self._connection_changed)
        for edit in (self.password_edit, self.domain_edit):
            edit.textEdited.connect(self._connection_changed)

        self._tasks: set[Task] = set()
        self._verified: SmbConfig | None = None  # last config that passed Test Connection
        self._accept_after_test = False
        self._save_anyway = False
        self.local_radio.toggled.connect(self._source_type_changed)
        self._source_type_changed()
        if existing:
            # Where the files are can't change; credentials can (passwords get rotated).
            locked = [self.local_radio, self.smb_radio, self.path_edit, browse, self.host_edit]
            for w in [*locked, self.share_edit, self.share_path_edit, self.domain_edit]:
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
        self.sync.setToolTip(
            "Saves decisions to .degreaser/state in the project folder so other computers can "
            "open the project. Only one computer can make decisions at a time."
        )
        self.sync_note = QLabel(f"Sync wasn't available last time: {form.sync_note}")
        self.sync_note.setProperty("role", "faint")
        self.sync_note.setWordWrap(True)
        self.sync_note.setContentsMargins(26, 0, 0, 0)
        self.sync_note.setVisible(bool(form.sync_note))
        for w in (self.retriage, self.remove_empty, self.jump_back, self.sync, self.sync_note):
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

        dup_row = QHBoxLayout()
        dup_row.addWidget(QLabel("Duplicates: ignore files smaller than"))
        self.dup_min = _Megabytes()
        self.dup_min.setRange(0.001, 100_000)
        self.dup_min.setDecimals(3)
        self.dup_min.setSuffix(" MB")
        self.dup_min.setValue(s.duplicate_min_bytes / MB)
        self.dup_min.setToolTip(
            "Smaller files aren't checked for duplicates. Tiny files are cheap to hash but make "
            "lots of noisy matches (icons, empty documents)."
        )
        dup_row.addWidget(self.dup_min)
        dup_row.addStretch()
        settings_layout.addLayout(dup_row)
        self.delete_duplicates = QCheckBox("Delete exact duplicates of files I've kept")
        self.delete_duplicates.setChecked(s.delete_duplicates)
        self.delete_duplicates.setToolTip(
            "A file identical to one you kept (directly or with Keep/Tag folder) is marked for "
            "deletion and skipped in triage. It beats always-keep rules but never overrides a "
            "kept folder or your own decision on that file."
        )
        settings_layout.addWidget(self.delete_duplicates)
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
        hint = QLabel("gitignore syntax, one pattern per line.")
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
        self.lock_note = QLabel(
            "These rules include patterns that aren't part of a preset, so they can only be "
            "edited as text. Make them match presets exactly to switch back."
        )
        self.lock_note.setProperty("role", "faint")
        self.lock_note.setWordWrap(True)
        self.lock_note.hide()
        text_layout.addWidget(self.lock_note)
        self.delete_rules.textChanged.connect(self._update_text_lock)
        self.keep_rules.textChanged.connect(self._update_text_lock)
        self.text_page = text_page
        text_page.hide()
        rules_layout.addWidget(text_page)
        layout.addWidget(rules_box, 1)
        if adding_existing:
            # Name, settings and rules come from the folder: hidden, but still owned by the
            # dialog (an unparented box would be deleted along with its widgets).
            for w in (name_row, settings_box, rules_box):
                w.hide()
            layout.addStretch(1)

        self.delete_column.load(form.rules)
        self.keep_column.load(form.rules)
        if form.rules.custom_delete or form.rules.custom_keep:
            # Not expressible as presets: open straight into (locked) text mode.
            self._show_text(form.rules)

        self.error_label = QLabel()
        self.error_label.setProperty("role", "error")
        outer.addWidget(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        save = buttons.button(QDialogButtonBox.StandardButton.Save)
        assert save is not None
        save.setProperty("variant", "primary")
        if adding_existing:
            save.setText("Add")
        self.save_button = save
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        # Open as tall as the content wants, but no taller than the screen.
        chrome = buttons.sizeHint().height() + self.error_label.sizeHint().height() + 48
        fit_to_screen(self, 760, content.sizeHint().height() + chrome)

    # Rules ------------------------------------------------------------------------------------

    def _config_from_checkboxes(self) -> RuleConfig:
        return RuleConfig(presets=self.delete_column.selected() | self.keep_column.selected())

    def _text_config(self) -> RuleConfig:
        return RuleConfig.infer(_patterns(self.delete_rules), _patterns(self.keep_rules))

    def _show_text(self, config: RuleConfig) -> None:
        self.delete_rules.setPlainText(_to_text(config.patterns("always_delete")))
        self.keep_rules.setPlainText(_to_text(config.patterns("always_keep")))
        self.presets_page.hide()
        self.text_page.show()
        self.text_toggle.blockSignals(True)
        self.text_toggle.setChecked(True)
        self.text_toggle.blockSignals(False)
        self.text_toggle.setText("Show Presets")
        self._update_text_lock()

    def _toggle_text_mode(self, text: bool) -> None:
        if text:
            self._show_text(self._config_from_checkboxes())
        else:
            config = self._text_config()
            self.delete_column.load(config)
            self.keep_column.load(config)
            self.text_page.hide()
            self.presets_page.show()
            self.text_toggle.setText("Edit as Text")

    def _update_text_lock(self) -> None:
        """Rules that aren't an exact set of presets can't be shown as checkboxes, so the
        dialog stays in text mode until they are."""
        if not self.text_toggle.isChecked():
            return
        config = self._text_config()
        locked = bool(config.custom_delete or config.custom_keep)
        self.text_toggle.setEnabled(not locked)
        self.text_toggle.setToolTip(
            "Only available when the rules match presets exactly" if locked else ""
        )
        self.lock_note.setVisible(locked)

    def rule_config(self) -> RuleConfig:
        if self.text_toggle.isChecked():
            return self._text_config()
        return self._config_from_checkboxes()

    # Source -----------------------------------------------------------------------------------

    @property
    def is_smb(self) -> bool:
        return self.smb_radio.isChecked()

    def _source_type_changed(self) -> None:
        self.local_page.setVisible(not self.is_smb)
        self.smb_page.setVisible(self.is_smb)

    def _split_pasted_location(self) -> None:
        parsed = parse_smb_location(self.host_edit.text())
        if parsed is None:
            return
        host, port, share, path = parsed
        self.host_edit.setText(host + (f":{port}" if port else ""))
        if share:
            self.share_edit.setText(share)
            self.share_path_edit.setText(path)
        self._connection_changed()

    def smb_config(self) -> SmbConfig:
        host, _, port = self.host_edit.text().strip().partition(":")
        return SmbConfig(
            host=host.strip(),
            share=self.share_edit.text().strip().strip("/\\"),
            path=normalise_share_path(self.share_path_edit.text()),
            username=self.username_edit.text().strip(),
            domain=self.domain_edit.text().strip(),
            port=int(port) if port.strip().isdigit() else 445,
            encrypt=self.encrypt_check.isChecked(),
        )

    def _connection_changed(self) -> None:
        self._verified = None
        self._save_anyway = False
        self.save_button.setText("Save")
        self.test_label.setText("")

    def _smb_problem(self) -> str | None:
        config = self.smb_config()
        if not config.host:
            return "Enter the server's name or address."
        if not config.share:
            return "Enter the share name."
        if not config.username:
            return "Enter a username."
        if not self.password_edit.text() and not self._has_saved_password:
            return "Enter the password."
        return None

    @property
    def _has_saved_password(self) -> bool:
        return not self.host_edit.isEnabled()  # existing SMB project

    def _test_connection(self) -> None:
        if problem := self._smb_problem():
            self._set_test_result(False, problem)
            return
        if not self.password_edit.text():
            self._set_test_result(False, "Enter the password to test the connection.")
            return
        config, password = self.smb_config(), self.password_edit.text()
        self.test_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.test_label.setProperty("role", "muted")
        self.test_label.setText("Connecting…")
        self._restyle(self.test_label)

        def done(message: str) -> None:
            self._verified = config
            self._set_test_result(True, message)
            if self._accept_after_test:
                self._accept_after_test = False
                self._accept()

        def failed(exc: BaseException) -> None:
            self._accept_after_test = False
            self._set_test_result(False, sentence(exc))
            self._save_anyway = True
            self.save_button.setText("Save Anyway")

        submit(
            lambda _progress: test_connection(config, password),
            on_result=done,
            on_error=failed,
            keep_alive=self._tasks,
        )

    def _set_test_result(self, ok: bool, message: str) -> None:
        self.test_button.setEnabled(True)
        self.save_button.setEnabled(True)
        self.test_label.setProperty("role", "success" if ok else "error")
        self.test_label.setText(message)
        self._restyle(self.test_label)

    @staticmethod
    def _restyle(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    # Dialog -----------------------------------------------------------------------------------

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose folder", self.path_edit.text())
        if folder:
            self.path_edit.setText(folder)
            if not self.name_edit.text():
                self.name_edit.setText(Path(folder).name)

    def _accept(self) -> None:
        if self.adding_existing:
            self._accept_existing()
            return
        if not self.name_edit.text().strip():
            self.error_label.setText("Give the project a name.")
            return
        if self.is_smb:
            if problem := self._smb_problem():
                self.error_label.setText(problem)
                return
        elif self.path_edit.isEnabled() and not Path(self.path_edit.text()).is_dir():
            self.error_label.setText("Choose a folder that exists.")
            return
        config = self.rule_config()
        for pattern in config.patterns("always_delete") + config.patterns("always_keep"):
            if error := validate_pattern(pattern):
                self.error_label.setText(f"Rule “{pattern}”: {error}")
                return
        needs_test = self.is_smb and bool(self.password_edit.text())
        if needs_test and self._verified != self.smb_config() and not self._save_anyway:
            # New or changed credentials: check them first; Save again to skip the check.
            self.error_label.setText("")
            self._accept_after_test = True
            self._test_connection()
            return
        self.accept()

    def _accept_existing(self) -> None:
        """Check the folder holds a saved project before adding it (on a worker)."""
        if self.is_smb:
            if problem := self._smb_problem():
                self.error_label.setText(problem)
                return
            config, password = self.smb_config(), self.password_edit.text()
            make: Any = lambda: SmbSource(config, password)  # noqa: E731
        else:
            folder = Path(self.path_edit.text())
            if not folder.is_dir():
                self.error_label.setText("Choose a folder that exists.")
                return
            make = lambda: LocalSource(folder)  # noqa: E731

        def probe(_progress: Any) -> RemoteState:
            source = make()
            try:
                source.connect()
                return probe_existing(source)
            finally:
                source.close()

        def found(remote: RemoteState) -> None:
            self.save_button.setEnabled(True)
            self.found = remote
            self.accept()

        def failed(exc: BaseException) -> None:
            self.save_button.setEnabled(True)
            self.error_label.setText(sentence(exc))

        self.save_button.setEnabled(False)
        self.error_label.setText("")
        submit(probe, on_result=found, on_error=failed, keep_alive=self._tasks)

    def result_form(self) -> ProjectForm:
        if self.is_smb:
            source = self.smb_config().to_source()
            password = self.password_edit.text() or None
        else:
            source = {"type": "local", "path": str(Path(self.path_edit.text()).resolve())}
            password = None
        name = self.name_edit.text().strip()
        if self.adding_existing and self.found is not None:
            name = self.found.project_name or "Project"
        return ProjectForm(
            name=name,
            source=source,
            settings=ProjectSettings(
                jump_back=self.jump_back.isChecked(),
                retriage_changed=self.retriage.isChecked(),
                deletion_mode="permanent" if self.mode_permanent.isChecked() else "trash",
                remove_empty_dirs=self.remove_empty.isChecked(),
                sync_enabled=self.sync.isChecked(),
                duplicate_min_bytes=max(1, round(self.dup_min.value() * MB)),
                delete_duplicates=self.delete_duplicates.isChecked(),
            ),
            rules=self.rule_config(),
            password=password,
        )
