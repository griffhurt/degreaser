"""The triage window (docs/product-spec.md §4) and the glue between it and the core session."""

import time
from dataclasses import asdict
from typing import Any

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QResizeEvent, QShortcut
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from degreaser.core.categories import CategoryStore, Completer, format_category
from degreaser.core.commit import empty_trash, list_trash_batches
from degreaser.core.model import Effective, EffectiveState, WalkResult
from degreaser.core.preview.cache import PreviewCache
from degreaser.core.preview.metadata import MetaRow
from degreaser.core.project import Project
from degreaser.core.rules import RuleSet
from degreaser.core.scan import CancelToken, Indexer, IndexProgress, IndexSummary, list_dir_into_db
from degreaser.core.settings import Settings
from degreaser.core.sources.base import Entry, FileSource, basename, join
from degreaser.core.triage import full_walk, sweep_first
from degreaser.core.triage.session import (
    ActionResult,
    Advance,
    Banner,
    FolderActionRefused,
    Item,
    Mode,
    TriageSession,
)
from degreaser.ui.controllers.tasks import Task, submit
from degreaser.ui.dialogs.commit import start_commit
from degreaser.ui.dialogs.project_edit import ProjectEditDialog, ProjectForm
from degreaser.ui.format import human_bytes, human_count
from degreaser.ui.keymap import Keymap
from degreaser.ui.preview.pane import PreviewPane
from degreaser.ui.theme import pixmap, theme
from degreaser.ui.widgets.action_bar import ActionBar
from degreaser.ui.widgets.banner import Banner as BannerWidget
from degreaser.ui.widgets.category_input import CategoryInput
from degreaser.ui.widgets.icons import STATE_LABELS
from degreaser.ui.widgets.metadata_table import MetadataTable
from degreaser.ui.widgets.path_bar import FolderEntry, FolderListing, PathBar
from degreaser.ui.widgets.toast import Toast
from degreaser.ui.windows.review import ReviewWindow

PATH_BAR_LIMIT = 500


_PAST = {
    "Keep folder": "Kept folder",
    "Delete folder": "Marked folder for deletion:",
    "Tag folder": "Tagged folder",
    "Unmark folder": "Unmarked folder",
    "Keep": "Kept",
    "Delete": "Marked for deletion:",
    "Skip": "Skipped",
    "Defer": "Deferred",
    "Tag": "Tagged",
}


def past_tense(label: str) -> str:
    """'Keep a.txt' → 'Kept a.txt' for action feedback."""
    for verb, past in _PAST.items():
        if label.startswith(verb + " "):
            return past + label[len(verb) :]
    return label


def describe_state(state: EffectiveState) -> str:
    text = STATE_LABELS[state.action]
    if state.origin.value in ("folder", "rule", "companion") and state.decided_by:
        text += f" (by {state.origin.value}: {state.decided_by or 'project root'})"
    return text


class TriageWindow(QMainWindow):
    closed = Signal()

    def __init__(self, project: Project, settings: Settings) -> None:
        super().__init__()
        self.project = project
        self.app_settings = settings
        self.source: FileSource = project.make_source()
        self.session = TriageSession(project.conn, settings=project.settings)
        self.keymap = Keymap(settings.keymap)
        self._tasks: set[Task] = set()
        self._index_cancel = CancelToken()
        self._indexing = False
        self._walk_running = False
        self._pending_tag_folder: str | None = None
        self.review_window: ReviewWindow | None = None
        self._closing = False

        self.setWindowTitle(f"{project.name} — Degreaser")
        self.resize(1280, 820)
        self._build_ui()
        self._build_menus()
        self._build_shortcuts()

        self.show_advance(self.session.start())
        self._start_indexer()
        self._request_walk()

    # UI construction ----------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("central")
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Top bar: breadcrumb, progress pill, review button.
        toolbar = QFrame()
        toolbar.setProperty("role", "toolbar")
        top = QHBoxLayout(toolbar)
        top.setContentsMargins(12, 8, 12, 8)
        top.setSpacing(10)
        self.path_bar = PathBar()
        self.path_bar.set_children_provider(self.folder_listing)
        self.path_bar.jump_requested.connect(self.jump_to)
        self.path_bar.keep_folder_requested.connect(self.keep_folder)
        self.path_bar.delete_folder_requested.connect(self.delete_folder)
        self.path_bar.tag_folder_requested.connect(self.tag_folder)
        top.addWidget(self.path_bar, 1)
        pill = QFrame()
        pill.setProperty("role", "pill")
        pill_layout = QHBoxLayout(pill)
        pill_layout.setContentsMargins(12, 4, 12, 4)
        self.summary_label = QLabel()
        self.summary_label.setProperty("role", "muted")
        pill_layout.addWidget(self.summary_label)
        top.addWidget(pill)
        self.mode_label = QLabel()
        self.mode_label.setProperty("role", "muted")
        self.index_label = QLabel()
        self.index_label.setProperty("role", "faint")
        top.addWidget(self.mode_label)
        top.addWidget(self.index_label)
        self.review_button = QPushButton("Review && Commit")
        self.review_button.setProperty("variant", "primary")
        self.review_button.clicked.connect(self.open_review)
        top.addWidget(self.review_button)
        root.addWidget(toolbar)

        body = QVBoxLayout()
        body.setContentsMargins(14, 12, 14, 12)
        body.setSpacing(10)
        self.banner = BannerWidget()
        body.addWidget(self.banner)

        # Page 0: file under triage; page 1: messages (loading folder, sweep, done).
        self.stack = QStackedWidget()
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.preview = self._make_preview()
        self.metadata = self._make_metadata_table()
        splitter.addWidget(self._card(self.preview, padding=1))
        details = QFrame()
        details.setProperty("role", "card")
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(14, 12, 6, 6)
        heading = QLabel("Details")
        heading.setProperty("role", "heading")
        details_layout.addWidget(heading)
        details_layout.addWidget(self.metadata, 1)
        splitter.addWidget(details)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([900, 340])
        self.stack.addWidget(splitter)

        self.message_page = QWidget()
        message_layout = QVBoxLayout(self.message_page)
        message_layout.addStretch()
        self.message_icon = QLabel()
        self.message_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        message_layout.addWidget(self.message_icon)
        self.message_label = QLabel()
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message_label.setWordWrap(True)
        message_layout.addWidget(self.message_label)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.review_skipped_button = QPushButton()
        self.review_skipped_button.clicked.connect(self.start_review_skipped)
        self.review_commit_button = QPushButton("Review && Commit…")
        self.review_commit_button.setProperty("variant", "primary")
        self.review_commit_button.clicked.connect(self.open_review)
        buttons.addWidget(self.review_skipped_button)
        buttons.addWidget(self.review_commit_button)
        buttons.addStretch()
        message_layout.addSpacing(8)
        message_layout.addLayout(buttons)
        message_layout.addStretch()
        self.stack.addWidget(self.message_page)
        body.addWidget(self.stack, 1)

        self.category_input = CategoryInput()
        self.category_input.hide()
        self.category_input.submitted.connect(self._category_submitted)
        self.category_input.cancelled.connect(self._category_cancelled)
        self.category_input.opened.connect(lambda: self.toast.hide())
        body.addWidget(self.category_input)
        root.addLayout(body, 1)

        self.action_bar = ActionBar(self.keymap)
        self.action_bar.triggered.connect(self.trigger)
        root.addWidget(self.action_bar)
        self.setCentralWidget(central)

        self.toast = Toast(central)

        # Status lives in the top bar; no status-bar strip under the action bar.
        self.statusBar().hide()
        self._update_summary()

    @staticmethod
    def _card(widget: QWidget, padding: int = 0) -> QFrame:
        card = QFrame()
        card.setProperty("role", "card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(padding, padding, padding, padding)
        layout.addWidget(widget)
        return card

    def notify(self, message: str, hint: str = "", ms: int = 1800) -> None:
        self.toast.bottom_margin = self.action_bar.height() + 14
        self.toast.show_message(message, hint, ms)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.toast.bottom_margin = self.action_bar.height() + 14
        self.toast.reposition()

    def _make_preview(self) -> PreviewPane:
        self.preview_cache = PreviewCache(self.project.cache_dir)
        pane = PreviewPane(self.app_settings.downloads, self.app_settings.video, self.preview_cache)
        pane.metadataChanged.connect(self._metadata_changed)
        pane.openRequested.connect(lambda _rel: self.open_current())
        return pane

    def _make_metadata_table(self) -> MetadataTable:
        return MetadataTable()

    def _build_menus(self) -> None:
        project_menu = self.menuBar().addMenu("&Project")
        project_menu.addAction("Review && Commit…", self.open_review)
        project_menu.addAction("Review Skipped Files", self.start_review_skipped)
        project_menu.addSeparator()
        project_menu.addAction("Project Settings…", self.edit_settings)
        project_menu.addAction("Empty Degreaser Trash…", self.empty_trash)
        project_menu.addSeparator()
        project_menu.addAction("Close Project", self.close)

        edit_menu = self.menuBar().addMenu("&Edit")
        self.undo_action = QAction("Undo", self)
        self.redo_action = QAction("Redo", self)
        self.undo_action.triggered.connect(self.undo)
        self.redo_action.triggered.connect(self.redo)
        edit_menu.addAction(self.undo_action)
        edit_menu.addAction(self.redo_action)
        edit_menu.aboutToShow.connect(self._update_undo_labels)
        self._update_undo_labels()

    def _build_shortcuts(self) -> None:
        self.shortcuts: dict[str, QShortcut] = {}
        for action in (
            "keep",
            "delete",
            "keep_folder",
            "delete_folder",
            "tag",
            "tag_folder",
            "skip",
            "open",
        ):
            self._shortcut(action, lambda a=action: self.trigger(a))
        self._shortcut("expand_preview", self.expand_preview)
        # Undo/redo also live on the Edit menu actions; shortcuts keep them working everywhere.
        self.undo_action.setShortcut(self.keymap.sequence("undo"))
        self.redo_action.setShortcut(self.keymap.sequence("redo"))

    def _shortcut(self, action: str, slot: Any) -> None:
        shortcut = QShortcut(self.keymap.sequence(action), self)
        shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        shortcut.activated.connect(slot)
        self.shortcuts[action] = shortcut

    # Showing items ------------------------------------------------------------------------

    def show_advance(self, advance: Advance | None) -> None:
        if advance is None:
            return
        if advance.item is not None:
            self.show_item(advance.item)
        elif advance.needs_listing is not None:
            self._show_message("Loading folder…", advance.needs_listing or self.project.name)
            self._list_then_advance(advance.needs_listing)
        elif advance.needs_sweep:
            self._show_message("Checking for anything missed…", icon="search")
            self._run_sweep()
        else:
            self._show_done()
        self._update_summary()

    def show_item(self, item: Item) -> None:
        self._update_undo_labels()
        self.stack.setCurrentIndex(0)
        self.action_bar.set_enabled_actions(set(self.action_bar.buttons))
        self.path_bar.set_path(item.path, root_label=self.project.name)
        detail = ""
        if item.banner is Banner.CHANGED and item.previous is not None:
            detail = f"was: {describe_state(item.previous)}"
        self.banner.show_kind(item.banner, detail)
        entry = self._entry(item.path)
        self.show_preview(item.path, entry)
        self.show_metadata(item.path, entry)
        self.mode_label.setText(
            "Reviewing skipped files" if self.session.mode is Mode.REVIEW_SKIPPED else ""
        )

    def show_preview(self, path: str, entry: Entry | None) -> None:
        if entry is None:
            self.preview.clear()
            return
        self.preview.show_file(self.source, path, entry)

    def show_metadata(self, path: str, entry: Entry | None) -> None:
        self.metadata.set_rows(self.preview.metadata_rows() if entry else [])
        self.metadata.set_extra_rows(
            [MetaRow("Triage", label, value) for label, value in self.decision_rows(path)]
        )

    def _metadata_changed(self, rel: str, rows: list[MetaRow]) -> None:
        item = self.session.current
        if item is not None and item.path == rel:
            self.metadata.set_rows(rows)

    def decision_rows(self, path: str) -> list[tuple[str, str]]:
        """Rows the triage window adds to the metadata table: decision and category."""
        state = self.session.state_of(path)
        rows = [("Decision", describe_state(state))]
        if state.category:
            where = "this file" if state.category_from == path else state.category_from
            rows.append(("Category", f"{format_category(state.category)}  (from {where})"))
        return rows

    def _show_message(
        self, title: str, body: str = "", *, icon: str = "loader-circle", done: bool = False
    ) -> None:
        t = theme().tokens
        self.stack.setCurrentIndex(1)
        self.message_icon.setPixmap(pixmap(icon, t.success if done else t.text_faint, 44, 1.5))
        body_html = f"<p style='color:{t.text_muted}'>{body}</p>" if body else ""
        self.message_label.setText(
            f"<p style='font-size:20px; font-weight:600'>{title}</p>{body_html}"
        )
        self.banner.show_kind(None)
        for button in (self.review_skipped_button, self.review_commit_button):
            button.setVisible(done)
        self.action_bar.set_enabled_actions(set() if not done else {"open"} - {"open"})

    def _show_done(self) -> None:
        if self.session.mode is Mode.REVIEW_SKIPPED:
            self.session.end_review()
        skipped = self.session.skipped_count()
        self.review_skipped_button.setText(f"Review skipped ({skipped:,})")
        self.review_skipped_button.setEnabled(skipped > 0)
        summary = self.session.tracker.summary
        detail = ""
        if summary is not None:
            files, size = human_count(summary.files_delete), human_bytes(summary.bytes_delete)
            categorised = human_count(summary.files_categorised)
            detail = f"{files} files ({size}) to delete · {categorised} categorised"
        self._show_message("Nothing left to triage", detail, icon="circle-check", done=True)
        self.mode_label.setText("")

    def _entry(self, path: str) -> Entry | None:
        row = self.project.conn.execute(
            "SELECT name, kind, size, mtime, ctime FROM nodes WHERE path = ?", (path,)
        ).fetchone()
        return None if row is None else Entry(row[0], row[1], row[2], row[3], row[4])

    def folder_listing(self, folder: str) -> FolderListing:
        conn = self.project.conn
        row = conn.execute("SELECT id FROM nodes WHERE path = ?", (folder,)).fetchone()
        if row is None:
            return FolderListing([], 0)
        total = conn.execute("SELECT count(*) FROM nodes WHERE parent_id = ?", (row[0],)).fetchone()
        children = conn.execute(
            "SELECT name, is_dir, size, subtree_files, subtree_bytes FROM nodes"
            " WHERE parent_id = ? ORDER BY is_dir, sort_key, name LIMIT ?",
            (row[0], PATH_BAR_LIMIT),
        ).fetchall()
        entries = []
        for name, is_dir, size, sub_files, sub_bytes in children:
            path = join(folder, name)
            state = self.session.state_of(path)
            entries.append(
                FolderEntry(
                    name=name,
                    path=path,
                    is_dir=bool(is_dir),
                    state=state.action,
                    origin=state.origin,
                    categorised=state.action is Effective.KEEP and bool(state.category),
                    size=sub_bytes if is_dir else size,
                    item_count=sub_files if is_dir else None,
                )
            )
        return FolderListing(entries, total[0])

    # Actions ------------------------------------------------------------------------------

    def trigger(self, action: str) -> None:
        if self.category_input.isVisible() or self.session.current is None:
            return
        match action:
            case "keep":
                self._apply(self.session.keep())
            case "delete":
                self._apply(self.session.delete())
            case "skip":
                self._apply(self.session.skip())
            case "tag":
                self._begin_tag(None)
            case "tag_folder":
                self.tag_folder(self.session.default_folder())
            case "keep_folder":
                self.keep_folder(self.session.default_folder())
            case "delete_folder":
                self.delete_folder(self.session.default_folder())
            case "open":
                self.open_current()
            case "expand_preview":
                self.expand_preview()

    def _apply(self, result: ActionResult) -> None:
        self.notify(past_tense(result.label), f"{self.keymap.label('undo')} to undo")
        self.show_advance(result.advance)

    def jump_to(self, path: str) -> None:
        self.show_item(self.session.jump_to(path))

    def delete_folder(self, folder: str) -> None:
        if self.session.current is None:
            return
        check = self.session.check_delete_folder(folder)
        if check.needs_confirmation:
            name = folder or self.project.name
            answer = QMessageBox.warning(
                self,
                "Delete folder?",
                f"“{name}” contains {check.kept:,} kept file(s), {check.categorised:,} of them "
                "categorised.\n\nMarking the folder for deletion overrides those decisions. "
                "They come back if you undo.",
                QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._apply(self.session.delete_folder(folder))
        self._after_folder_action()

    def tag_folder(self, folder: str) -> None:
        if self.session.current is None:
            return
        self._pending_tag_folder = folder
        state = self.session.state_of(folder) if folder else None
        category = list(state.category) if state and state.category else None
        inherited = category if state and state.category_from != folder else None
        name = basename(folder) or self.project.name
        self.category_input.begin(
            Completer(self.session.index), f"Tag folder “{name}”:", category, inherited
        )

    def _begin_tag(self, _unused: None) -> None:
        item = self.session.current
        if item is None:
            return
        self._pending_tag_folder = None
        state = self.session.state_of(item.path)
        category = list(state.category) if state.category else None
        inherited = category if state.category_from not in (None, item.path) else None
        self.category_input.begin(Completer(self.session.index), "Tag:", category, inherited)

    def keep_folder(self, folder: str) -> None:
        """Keep everything in a folder (a tag-folder that leaves its category as it is)."""
        if self.session.current is None or folder == "":
            return
        state = self.session.state_of(folder)
        own = list(state.category) if state.category and state.category_from == folder else []
        self._tag_folder_with(folder, own)

    def _category_submitted(self, tags: list[str]) -> None:
        folder = self._pending_tag_folder
        self._pending_tag_folder = None
        if folder is None:
            self._apply(self.session.tag(tags))
            return
        self._tag_folder_with(folder, tags)

    def _tag_folder_with(self, folder: str, tags: list[str]) -> None:
        check = self.session.check_tag_folder(tags, folder)
        if check.blocked_by is not None:
            blocked = check.blocked_by or self.project.name
            answer = QMessageBox.question(
                self,
                "Folder is inside a deleted folder",
                f"“{blocked}” is marked for deletion, so nothing inside it can be kept.\n\n"
                "Unmark it and tag this folder?",
                QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.session.unmark_folder(check.blocked_by)
            check = self.session.check_tag_folder(tags, folder)
        replace = False
        if check.needs_confirmation:
            replace_choice = self._confirm_tag_folder(check.delete_marks, check.conflicting, tags)
            if replace_choice is None:
                return
            replace = replace_choice
        try:
            self._apply(self.session.tag_folder(tags, folder, replace_conflicting=replace))
        except FolderActionRefused as exc:
            QMessageBox.information(self, "Can't tag folder", str(exc))
            return
        self._after_folder_action()

    def _confirm_tag_folder(
        self, delete_marks: int, conflicting: list[str], tags: list[str]
    ) -> bool | None:
        """Returns replace_conflicting, or None if cancelled."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Tag folder?")
        lines = []
        if delete_marks:
            lines.append(
                f"{delete_marks:,} file(s) inside are marked for deletion; they'll be kept."
            )
        if conflicting:
            lines.append(
                f"{len(conflicting):,} item(s) inside have their own category that isn't under "
                f"“{format_category(tuple(tags))}”."
            )
        box.setText("\n".join(lines))
        if conflicting:
            keep = box.addButton("Keep Their Categories", QMessageBox.ButtonRole.AcceptRole)
            replace = box.addButton("Replace with Folder's", QMessageBox.ButtonRole.AcceptRole)
        else:
            keep = box.addButton("Tag Folder", QMessageBox.ButtonRole.AcceptRole)
            replace = None
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is keep:
            return False
        if replace is not None and clicked is replace:
            return True
        return None

    def _category_cancelled(self) -> None:
        self._pending_tag_folder = None

    def _after_folder_action(self) -> None:
        if self.session.tracker.stale:
            self._request_walk()

    def start_review_skipped(self) -> None:
        self.show_advance(self.session.start_review_skipped())

    def undo(self) -> None:
        if self.category_input.isVisible():
            return
        item = self.session.undo()
        if item is not None:
            self.show_item(item)
            self._update_summary()

    def redo(self) -> None:
        if self.category_input.isVisible():
            return
        self.show_advance(self.session.redo())

    def _update_undo_labels(self) -> None:
        if not hasattr(self, "undo_action"):
            return  # still building the window; menus come after the body
        undo, redo = self.session.undo_label(), self.session.redo_label()
        self.undo_action.setText(f"Undo {undo}" if undo else "Undo")
        self.redo_action.setText(f"Redo {redo}" if redo else "Redo")
        self.undo_action.setEnabled(undo is not None)
        self.redo_action.setEnabled(redo is not None)

    def open_current(self) -> None:
        item = self.session.current
        if item is None:
            return
        local = self.source.local_path(item.path)
        if local is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(local)))

    def expand_preview(self) -> None:
        if self.session.current is not None and not self.category_input.isVisible():
            self.preview.expand()

    # Background work ----------------------------------------------------------------------

    def _submit(self, fn: Any, **slots: Any) -> Task:
        return submit(fn, keep_alive=self._tasks, **slots)

    def _list_then_advance(self, folder: str) -> None:
        source = self.source

        def work(_progress: Any) -> None:
            conn = self.project.new_connection()
            try:
                list_dir_into_db(conn, source, folder)
            finally:
                conn.close()

        def done(_result: Any) -> None:
            if self._closing:
                return
            self.session.on_tree_changed()
            self.show_advance(self.session.advance())

        self._submit(work, on_result=done, on_error=self._background_error)

    def _run_sweep(self) -> None:
        def work(_progress: Any) -> str | None:
            conn = self.project.new_connection()
            try:
                return sweep_first(conn)
            finally:
                conn.close()

        def done(path: str | None) -> None:
            if not self._closing:
                self.show_advance(self.session.apply_sweep(path))

        self._submit(work, on_result=done, on_error=self._background_error)

    def _start_indexer(self) -> None:
        cancel = self._index_cancel
        source = self.source
        project = self.project

        def work(progress: Any) -> IndexSummary:
            return Indexer(project.new_connection, source, on_progress=progress).run(cancel)

        def on_progress(p: IndexProgress) -> None:
            self.index_label.setText(f"Indexing… {p.dirs_listed:,} folders")

        def done(summary: IndexSummary) -> None:
            self._indexing = False
            if self._closing:
                return
            text = {"completed": "", "cancelled": ""}.get(summary.status, "Index incomplete")
            self.index_label.setText(text)
            self.session.on_tree_changed()
            self._request_walk()

        self._indexing = True
        self.index_label.setText("Indexing…")
        self._submit(work, on_result=done, on_progress=on_progress, on_error=self._background_error)

    def _request_walk(self) -> None:
        """Full effective-state walk on a worker; refreshes the status-bar figures (§8)."""
        if self._walk_running:
            return
        self._walk_running = True
        tracker = self.session.tracker
        token = tracker.begin_walk()

        def work(_progress: Any) -> WalkResult:
            conn = self.project.new_connection()
            try:
                return full_walk(conn, RuleSet.load(conn), CategoryStore(conn))
            finally:
                conn.close()

        def done(result: WalkResult) -> None:
            self._walk_running = False
            if self._closing:
                return
            tracker.finish_walk(token, result.summary)
            self._update_summary()

        def failed(exc: BaseException) -> None:
            self._walk_running = False
            self._background_error(exc)

        self._submit(work, on_result=done, on_error=failed)

    def _background_error(self, exc: BaseException) -> None:
        if not self._closing:
            QMessageBox.warning(self, "Something went wrong", f"{type(exc).__name__}: {exc}")

    def _update_summary(self) -> None:
        # Every decision change ends here, so keep Undo/Redo enabled-ness current too: a
        # disabled QAction ignores its shortcut (⌘Z did nothing until the Edit menu opened).
        self._update_undo_labels()
        summary = self.session.tracker.summary
        if summary is None:
            self.summary_label.setText("Counting files…")
            return
        stale = " …" if self.session.tracker.stale else ""
        self.summary_label.setText(
            f"{human_count(summary.files_triaged)} / {human_count(summary.files_known)} triaged"
            f"  ·  {human_bytes(summary.bytes_delete)} to delete{stale}"
        )

    # Review, commit, settings ---------------------------------------------------------------

    def open_review(self) -> None:
        if self.review_window is None:
            self.review_window = ReviewWindow(self.session, self.project.name)
            self.review_window.changed.connect(self._decisions_changed_elsewhere)
            self.review_window.commit_requested.connect(self.commit)
        self.review_window.reload()
        self.review_window.show()
        self.review_window.raise_()

    def _decisions_changed_elsewhere(self) -> None:
        if self.session.tracker.stale:
            self._request_walk()
        self._update_summary()
        item = self.session.current
        if item is not None:
            self.show_metadata(item.path, self._entry(item.path))

    def commit(self) -> None:
        parent: QWidget = self.review_window if self.review_window else self
        outcome = start_commit(parent, self.project)
        if outcome is None:
            return
        # Deleted paths left the tree and history was cleared: rebuild everything derived.
        self.session.on_tree_changed()
        self.session.refresh_queues()
        self._update_undo_labels()
        self.session.tracker.mark_stale()
        self._request_walk()
        if self.review_window is not None:
            self.review_window.reload()
        self.show_advance(self.session.advance())

    def edit_settings(self) -> None:
        always_delete, always_keep = self.project.rules()
        form = ProjectForm(
            self.project.name,
            self.project.source_config,
            self.project.settings,
            self.project.rule_config(),
        )
        dialog = ProjectEditDialog(self, existing=form)
        if dialog.exec() != ProjectEditDialog.DialogCode.Accepted:
            return
        result = dialog.result_form()
        self.project.set_meta("name", result.name)
        self.project.save_settings(result.settings)
        self.session.settings = result.settings
        self.project.save_rule_config(result.rules)
        if (result.always_delete, result.always_keep) != (always_delete, always_keep):
            self.session.reload_rules()
            self._request_walk()
            # The current file may now be decided by a rule.
            if self.session.current is not None and not self.session.traversal.needs_decision(
                self.session.current.path
            ):
                self.show_advance(self.session.advance())
        self.setWindowTitle(f"{result.name} — Degreaser")

    def empty_trash(self) -> None:
        batches = list_trash_batches(self.source)
        if not batches:
            QMessageBox.information(self, "Degreaser trash", "The Degreaser trash is empty.")
            return
        files = sum(b.files for b in batches)
        size = sum(b.bytes for b in batches)
        answer = QMessageBox.warning(
            self,
            "Empty Degreaser trash?",
            f"Permanently delete {files:,} files ({human_bytes(size)}) from {len(batches)} "
            "commit(s) in the Degreaser trash? This can't be undone.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            empty_trash(self.source)
            self.notify(f"Freed {human_bytes(size)}", ms=4000)

    # Closing --------------------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closing = True
        self._index_cancel.cancel()
        if self.review_window is not None:
            self.review_window.close()
        self.preview.shutdown()
        self.preview_cache.clear_all()
        summary = self.session.tracker.summary
        if summary is not None:
            self.project.set_meta(
                "last_summary",
                {**asdict(summary), "files_triaged": summary.files_triaged, "at": time.time()},
            )
        super().closeEvent(event)
        self.closed.emit()
