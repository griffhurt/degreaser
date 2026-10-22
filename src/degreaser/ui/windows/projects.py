"""Home window: the project list (docs/product-spec.md §3.1)."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from degreaser.core.project import (
    Project,
    ProjectInfo,
    ProjectInUse,
    list_projects,
    remove_project,
)
from degreaser.ui.dialogs.project_edit import ProjectEditDialog, ProjectForm
from degreaser.ui.format import human_bytes, relative_time

COLUMNS = ["Name", "Source", "Triaged", "Marked for delete", "Last opened"]


class ProjectsWindow(QMainWindow):
    open_requested = Signal(str)  # project uuid

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Degreaser")
        self.resize(900, 480)
        self._infos: list[ProjectInfo] = []

        central = QWidget()
        layout = QVBoxLayout(central)
        self.title_label = QLabel("<h2>Projects</h2>")
        # Never grow vertically: a taller label centres its text and the header drifts down.
        self.title_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.title_label)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.doubleClicked.connect(lambda _: self._open())
        self.table.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.table, 1)

        self.empty_label = QLabel("No projects yet. Create one to start triaging a folder.")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Takes the table's space when there are no projects, so the header stays at the top.
        layout.addWidget(self.empty_label, 1)

        buttons = QHBoxLayout()
        self.new_button = QPushButton("New Project…")
        self.open_button = QPushButton("Open")
        self.edit_button = QPushButton("Edit…")
        self.remove_button = QPushButton("Remove…")
        self.new_button.clicked.connect(self._new)
        self.open_button.clicked.connect(self._open)
        self.edit_button.clicked.connect(self._edit)
        self.remove_button.clicked.connect(self._remove)
        self.open_button.setDefault(True)
        for b in (self.new_button, self.edit_button, self.remove_button):
            buttons.addWidget(b)
        buttons.addStretch()
        buttons.addWidget(self.open_button)
        layout.addLayout(buttons)
        self.setCentralWidget(central)

        self.refresh()

    def refresh(self) -> None:
        self._infos = list_projects()
        self.table.setRowCount(len(self._infos))
        for row, info in enumerate(self._infos):
            summary = info.last_summary or {}
            known = summary.get("files_known")
            triaged = summary.get("files_triaged")
            progress = f"{triaged / known:.0%}" if known and triaged is not None else "—"
            values = [
                info.name,
                info.source_description,
                progress,
                human_bytes(summary.get("bytes_delete")),
                relative_time(info.last_opened),
            ]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(value))
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.empty_label.setVisible(not self._infos)
        self.table.setVisible(bool(self._infos))
        if self._infos:
            self.table.selectRow(0)
        self._update_buttons()

    def _selected(self) -> ProjectInfo | None:
        rows = self.table.selectionModel().selectedRows()
        return self._infos[rows[0].row()] if rows else None

    def _update_buttons(self) -> None:
        has = self._selected() is not None
        for b in (self.open_button, self.edit_button, self.remove_button):
            b.setEnabled(has)

    def _new(self) -> None:
        dialog = ProjectEditDialog(self)
        if dialog.exec() != ProjectEditDialog.DialogCode.Accepted:
            return
        form = dialog.result_form()
        project = Project.create(form.name, form.source, form.settings)
        project.replace_rules(form.always_delete, form.always_keep)
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
            always_delete, always_keep = project.rules()
            form = ProjectForm(
                project.name, project.source_config, project.settings, always_delete, always_keep
            )
            dialog = ProjectEditDialog(self, existing=form)
            if dialog.exec() == ProjectEditDialog.DialogCode.Accepted:
                result = dialog.result_form()
                project.set_meta("name", result.name)
                project.save_settings(result.settings)
                project.replace_rules(result.always_delete, result.always_keep)
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
