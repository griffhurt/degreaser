"""Home window: the project list (docs/product-spec.md §3.1)."""

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QKeyEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from degreaser.core import credentials
from degreaser.core.project import (
    Project,
    ProjectExists,
    ProjectInfo,
    ProjectInUse,
    ProjectSettings,
    list_projects,
    remove_project,
)
from degreaser.core.settings import Settings
from degreaser.links import USER_GUIDE_URL
from degreaser.ui.dialogs.modal import run_modal
from degreaser.ui.dialogs.project_edit import ProjectEditDialog, ProjectForm
from degreaser.ui.format import human_bytes, relative_time
from degreaser.ui.theme import icon, pixmap, theme
from degreaser.ui.widgets.screen import fit_to_screen


def _restyle(widget: QWidget) -> None:
    """Re-apply the stylesheet after a dynamic property changed."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def save_source_changes(project: Project, form: ProjectForm) -> None:
    """The parts of an existing project's source that may change: SMB username and password."""
    if form.source.get("type") == "smb":
        project.set_meta("source", form.source)
        if form.password is not None:
            credentials.set_password(project.uuid, form.password)


class ProjectCard(QFrame):
    def __init__(self, info: ProjectInfo) -> None:
        super().__init__()
        self.info = info
        self.setProperty("role", "card")
        self.setProperty("selected", False)
        t = theme().tokens
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 14, 18, 14)
        layout.setSpacing(14)

        badge = QLabel()
        badge.setFixedSize(40, 40)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet(f"background: {t.accent_tint}; border-radius: 10px;")
        kind = "server" if info.source.get("type") == "smb" else "hard-drive"
        badge.setPixmap(pixmap(kind, t.accent, 20))
        layout.addWidget(badge)

        text = QVBoxLayout()
        text.setSpacing(2)
        self.name_label = QLabel(info.name)
        self.name_label.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.source_label = QLabel(info.source_description)
        self.source_label.setProperty("role", "muted")
        self.source_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        text.addWidget(self.name_label)
        text.addWidget(self.source_label)
        layout.addLayout(text, 1)

        summary = info.last_summary or {}
        known, triaged = summary.get("files_known"), summary.get("files_triaged")
        stats = QVBoxLayout()
        stats.setSpacing(5)
        if known and triaged is not None:
            fraction = triaged / known
            self.progress_label = QLabel(f"{fraction:.0%} triaged")
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setValue(round(fraction * 1000))
        else:
            self.progress_label = QLabel("Not started")
            bar = QProgressBar()
            bar.setRange(0, 1)
            bar.setValue(0)
        bar.setFixedWidth(170)
        self.progress_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.progress_label.setStyleSheet("font-weight: 500;")
        bytes_delete = summary.get("bytes_delete")
        self.delete_label = QLabel(
            f"{human_bytes(bytes_delete)} to delete"
            if bytes_delete
            else "Nothing marked for deletion"
        )
        self.delete_label.setProperty("role", "faint")
        self.delete_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.opened_label = QLabel(f"Opened {relative_time(info.last_opened)}")
        self.opened_label.setProperty("role", "faint")
        self.opened_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        stats.addWidget(self.progress_label)
        stats.addWidget(bar, 0, Qt.AlignmentFlag.AlignRight)
        stats.addWidget(self.delete_label)
        stats.addWidget(self.opened_label)
        layout.addLayout(stats)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        _restyle(self)


class _CardList(QListWidget):
    activated_project = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.activated_project.emit()
            return
        super().keyPressEvent(event)


def preferences_shortcut() -> QKeySequence:
    """⌘, on macOS; Ctrl+, elsewhere (Windows has no standard key for Preferences)."""
    standard = QKeySequence(QKeySequence.StandardKey.Preferences)
    return standard if not standard.isEmpty() else QKeySequence("Ctrl+,")


def new_project_settings(settings: Settings) -> ProjectSettings:
    d = settings.defaults
    return ProjectSettings(
        jump_back=d.jump_back,
        retriage_changed=d.retriage_changed,
        deletion_mode=d.deletion_mode,
        remove_empty_dirs=d.remove_empty_dirs,
        sync_enabled=d.sync_enabled,
    )


class ProjectsWindow(QMainWindow):
    open_requested = Signal(str)  # project uuid
    preferences_requested = Signal()

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__()
        self.settings = settings or Settings()
        self.setWindowTitle("Degreaser")
        fit_to_screen(self, 920, 620)
        self._infos: list[ProjectInfo] = []
        self.cards: list[ProjectCard] = []
        t = theme().tokens

        central = QWidget()
        central.setObjectName("central")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(16)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.title_label = QLabel("Projects")
        self.title_label.setProperty("role", "title")
        # Never grow vertically: a taller label centres its text and the header drifts down.
        self.title_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        subtitle = QLabel("Work through a folder one file at a time, then commit.")
        subtitle.setProperty("role", "muted")
        titles.addWidget(self.title_label)
        titles.addWidget(subtitle)
        header.addLayout(titles, 1)
        self.new_button = QPushButton("New Project")
        self.new_button.setProperty("variant", "primary")
        self.new_button.setIcon(icon("plus", t.accent_text, 16))
        self.new_button.clicked.connect(self._new)
        self.add_existing_button = QPushButton("Add Existing…")
        self.add_existing_button.setToolTip(
            "Add a project another computer created, from its project folder"
        )
        self.add_existing_button.clicked.connect(self._add_existing)
        self.preferences_button = QPushButton()
        self.preferences_button.setProperty("variant", "ghost")
        self.preferences_button.setIcon(icon("settings", t.text_muted, 18))
        self.preferences_button.setIconSize(QSize(18, 18))
        self.preferences_button.setToolTip(
            "Preferences ("
            + preferences_shortcut().toString(QKeySequence.SequenceFormat.NativeText)
            + ")"
        )
        self.preferences_button.clicked.connect(self.preferences_requested)
        QShortcut(preferences_shortcut(), self, self.preferences_requested.emit)
        side = self.new_button.sizeHint().height()
        self.preferences_button.setFixedSize(side, side)
        header.addWidget(self.preferences_button, 0, Qt.AlignmentFlag.AlignTop)
        header.addWidget(self.add_existing_button, 0, Qt.AlignmentFlag.AlignTop)
        header.addWidget(self.new_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)

        self.list = _CardList()
        self.list.setProperty("variant", "cards")
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.list.itemDoubleClicked.connect(lambda _item: self._open())
        self.list.activated_project.connect(self._open)
        self.list.currentRowChanged.connect(self._selection_changed)
        layout.addWidget(self.list, 1)

        # Empty state (replaces the list when there are no projects).
        self.empty_label = QWidget()
        empty = QVBoxLayout(self.empty_label)
        empty.addStretch()
        empty_icon = QLabel()
        empty_icon.setPixmap(pixmap("inbox", t.text_faint, 48, 1.5))
        empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_title = QLabel("No projects yet")
        empty_title.setProperty("role", "heading")
        empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_text = QLabel("Create a project for a folder you want to clean up.")
        empty_text.setProperty("role", "muted")
        empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.guide_link = QLabel(f'New here? <a href="{USER_GUIDE_URL}">Read the user guide</a>')
        self.guide_link.setProperty("role", "muted")
        self.guide_link.setOpenExternalLinks(True)
        self.guide_link.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.guide_link.setContentsMargins(0, 6, 0, 0)
        for w in (empty_icon, empty_title, empty_text, self.guide_link):
            empty.addWidget(w)
        empty.addStretch()
        layout.addWidget(self.empty_label, 1)

        buttons = QHBoxLayout()
        self.edit_button = QPushButton("Edit…")
        self.remove_button = QPushButton("Remove…")
        self.remove_button.setProperty("variant", "danger")
        self.open_button = QPushButton("Open")
        self.open_button.setProperty("variant", "primary")
        self.edit_button.clicked.connect(self._edit)
        self.remove_button.clicked.connect(self._remove)
        self.open_button.clicked.connect(self._open)
        buttons.addWidget(self.edit_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch()
        buttons.addWidget(self.open_button)
        layout.addLayout(buttons)
        self.setCentralWidget(central)

        self.refresh()

    def refresh(self) -> None:
        self._infos = list_projects()
        self.list.clear()
        self.cards = []
        for info in self._infos:
            card = ProjectCard(info)
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, card.sizeHint().height() + 8))
            self.list.addItem(item)
            self.list.setItemWidget(item, card)
            self.cards.append(card)
        has = bool(self._infos)
        self.empty_label.setVisible(not has)
        self.list.setVisible(has)
        if has:
            self.list.setCurrentRow(0)
        self._update_buttons()

    def _selection_changed(self, row: int) -> None:
        for i, card in enumerate(self.cards):
            card.set_selected(i == row)
        self._update_buttons()

    def _selected(self) -> ProjectInfo | None:
        row = self.list.currentRow()
        return self._infos[row] if 0 <= row < len(self._infos) else None

    def _update_buttons(self) -> None:
        has = self._selected() is not None
        for b in (self.open_button, self.edit_button, self.remove_button):
            b.setVisible(bool(self._infos))
            b.setEnabled(has)

    def _new(self) -> None:
        dialog = ProjectEditDialog(self, defaults=new_project_settings(self.settings))
        if not run_modal(dialog):
            return
        form = dialog.result_form()
        project = Project.create(form.name, form.source, form.settings)
        project.save_rule_config(form.rules)
        if form.password is not None:
            credentials.set_password(project.uuid, form.password)
        project.close()
        self.refresh()
        self.open_requested.emit(project.uuid)

    def _add_existing(self) -> None:
        dialog = ProjectEditDialog(self, adding_existing=True)
        if not run_modal(dialog) or dialog.found is None:
            return
        form, remote = dialog.result_form(), dialog.found
        try:
            # Same uuid as on the other computer; opening it pulls the saved state.
            project = Project.create(form.name, form.source, project_uuid=remote.project_uuid)
        except ProjectExists:
            QMessageBox.information(
                self, "Already added", f"“{form.name}” is already in your project list."
            )
            return
        if form.password is not None:
            credentials.set_password(project.uuid, form.password)
        project.close()
        self.refresh()
        self.open_requested.emit(project.uuid)

    def _open(self) -> None:
        if info := self._selected():
            self.open_requested.emit(info.uuid)

    def _edit(self) -> None:
        info = self._selected()
        if info is None:
            return
        try:
            project = Project.open(info.uuid)
        except ProjectInUse as exc:
            QMessageBox.information(self, "Project is open", str(exc))
            return
        try:
            form = ProjectForm(
                project.name,
                project.source_config,
                project.settings,
                project.rule_config(),
                sync_note=project.meta("sync_unavailable") or "",
            )
            dialog = ProjectEditDialog(self, existing=form)
            if run_modal(dialog):
                result = dialog.result_form()
                project.set_meta("name", result.name)
                project.save_settings(result.settings)
                project.save_rule_config(result.rules)
                save_source_changes(project, result)
        finally:
            project.close()
        self.refresh()

    def _remove(self) -> None:
        info = self._selected()
        if info is None:
            return
        answer = QMessageBox.question(
            self,
            "Remove project?",
            f"Remove “{info.name}” from Degreaser?\n\n"
            "This deletes Degreaser's local data for the project (decisions, categories, "
            "cached tree). Your files and the .degreaser folder are not touched.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            remove_project(info.uuid)
            self.refresh()
