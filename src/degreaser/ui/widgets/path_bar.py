"""Breadcrumb path bar; clicking a folder opens its dropdown (docs/product-spec.md §4.1)."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QResizeEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLayout, QMenu, QToolButton, QWidget

from degreaser.core.model import Effective, Origin
from degreaser.core.sources.base import basename, join
from degreaser.ui.format import human_bytes
from degreaser.ui.theme import icon, pixmap, theme
from degreaser.ui.widgets.elided_label import ElidedLabel
from degreaser.ui.widgets.icons import STATE_LABELS, state_icon

_EXT_ICONS = {
    "file-image": {"jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp", "heic", "svg", "ico"},
    "file-video": {
        "mp4",
        "mov",
        "m4v",
        "avi",
        "mkv",
        "wmv",
        "asf",
        "flv",
        "mpg",
        "mpeg",
        "vob",
        "3gp",
        "webm",
        "mts",
        "m2ts",
        "rm",
    },
    "file-audio": {"mp3", "wav", "aac", "m4a", "flac", "ogg", "opus", "wma", "aif", "aiff"},
    "file-archive": {
        "zip",
        "rar",
        "7z",
        "tar",
        "gz",
        "tgz",
        "bz2",
        "xz",
        "zst",
        "cbr",
        "cbz",
        "dmg",
        "iso",
    },
    "file-code": {
        "py",
        "js",
        "ts",
        "html",
        "css",
        "c",
        "h",
        "cpp",
        "java",
        "rs",
        "go",
        "sh",
        "json",
        "xml",
        "yml",
        "yaml",
    },
    "file-text": {"txt", "md", "rtf", "doc", "docx", "pdf", "csv", "log", "pages"},
}


def file_icon_name(name: str) -> str:
    ext = name.rpartition(".")[2].lower() if "." in name else ""
    return next((icon for icon, exts in _EXT_ICONS.items() if ext in exts), "file")


MAX_MENU_ITEMS = 500


@dataclass(frozen=True)
class FolderEntry:
    """One child row in a folder's dropdown."""

    name: str
    path: str
    is_dir: bool
    state: Effective
    origin: Origin
    categorised: bool = False
    size: int | None = None  # files: size; folders: subtree bytes if indexed
    item_count: int | None = None  # folders only


@dataclass(frozen=True)
class FolderListing:
    entries: list[FolderEntry]
    total: int  # may exceed len(entries) when the provider truncates


ChildrenProvider = Callable[[str], FolderListing]


class _Segment(QToolButton):
    """A folder in the breadcrumb. Its dropdown opens on click only (hover was too twitchy)."""


class PathBar(QWidget):
    jump_requested = Signal(str)  # file path
    keep_folder_requested = Signal(str)  # folder path
    delete_folder_requested = Signal(str)  # folder path
    tag_folder_requested = Signal(str)  # folder path

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        # Deep paths mustn't widen the window: middle folders collapse into "…" instead
        # (see _fit), so the layout may not impose its children's full width as a minimum.
        self._layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._crumbs: list[tuple[QLabel | None, _Segment]] = []  # (separator before, segment)
        self._ellipsis: tuple[QLabel, _Segment] | None = None
        self._file_parts: list[QWidget] = []
        self._hidden: list[_Segment] = []
        self._provider: ChildrenProvider | None = None
        self._path = ""
        self._root_label = "Project"
        theme().changed.connect(lambda _t: self.set_path(self._path, self._root_label))
        self._menu: QMenu | None = None
        self.segments: list[_Segment] = []

    def set_children_provider(self, provider: ChildrenProvider) -> None:
        self._provider = provider

    def set_path(self, file_path: str, root_label: str = "Project") -> None:
        """Show the breadcrumb for a file. Every segment except the last is a folder."""
        self._path = file_path
        self._root_label = root_label
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item is not None and (widget := item.widget()):
                # deleteLater() alone leaves the old segment painted until the event loop runs.
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self.segments = []
        self._crumbs = []
        self._file_parts = []

        parts = file_path.split("/") if file_path else []
        folders = [""] + ["/".join(parts[: i + 1]) for i in range(len(parts) - 1)]
        for i, folder in enumerate(folders):
            separator = self._separator() if i else None
            if separator is not None:
                self._layout.addWidget(separator)
            text = root_label if folder == "" else basename(folder)
            segment = self._add_folder_segment(folder, text)
            self._crumbs.append((separator, segment))
            if i == 0:
                # "…" stands in for collapsed folders, right after the root.
                more = _Segment()
                more.setText("…")
                more.setProperty("variant", "crumb")
                more.setCursor(Qt.CursorShape.PointingHandCursor)
                more.setToolTip("More folders")
                more.clicked.connect(lambda _=False, m=more: self._open_hidden_menu(m))
                more_sep = self._separator()
                self._layout.addWidget(more_sep)
                self._layout.addWidget(more)
                self._ellipsis = (more_sep, more)
        if parts:
            t = theme().tokens
            sep = self._separator()
            self._layout.addWidget(sep)
            file_icon = QLabel()
            file_icon.setPixmap(pixmap(file_icon_name(parts[-1]), t.accent, 16))
            file_icon.setContentsMargins(6, 0, 0, 0)
            self._layout.addWidget(file_icon)
            name = ElidedLabel(parts[-1], mode=Qt.TextElideMode.ElideMiddle)
            name.setStyleSheet(
                f"font-weight: 600; font-size: 14px; color: {t.text}; padding-left: 6px;"
            )
            self._layout.addWidget(name, 1)
            self._file_parts = [sep, file_icon, name]
        else:
            self._layout.addStretch()
        self._fit()

    def _separator(self) -> QLabel:
        label = QLabel()
        label.setPixmap(pixmap("chevron-right", theme().tokens.text_faint, 14))
        return label

    # Fitting ----------------------------------------------------------------------------------

    def minimumSizeHint(self) -> QSize:
        """Root, "…", the file's folder and a little of its name."""
        if not self._crumbs:
            return super().minimumSizeHint()
        widths = [self._crumbs[0][1].sizeHint().width(), 40, 120]
        return QSize(sum(widths) + 3 * 20, super().minimumSizeHint().height())

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        """Hide middle folders (nearest the root first) until the bar fits its width."""
        if not self._crumbs or self._ellipsis is None:
            return
        available = self.width()
        # The file name matters most: room for all of it, up to half the bar (at least 120 px).
        file_min = 0
        if self._file_parts:
            name_w = self._file_parts[2].sizeHint().width()
            file_min = min(name_w, max(120, available // 2))

        def width(widgets: Sequence[QWidget | None]) -> int:
            return sum(w.sizeHint().width() for w in widgets if w is not None)

        folders = [w for pair in self._crumbs for w in pair]
        file_w = width(self._file_parts[:2]) + file_min
        hidden: list[int] = []
        middle = list(range(1, len(self._crumbs) - 1))
        needed = width(folders) + file_w
        ellipsis_w = width(list(self._ellipsis))
        while middle and needed > available:
            index = middle.pop(0)
            if not hidden:
                needed += ellipsis_w
            hidden.append(index)
            needed -= width(list(self._crumbs[index]))
        for i, (separator, segment) in enumerate(self._crumbs):
            visible = i not in hidden
            segment.setVisible(visible)
            if separator is not None:
                separator.setVisible(visible)
        for widget in self._ellipsis:
            widget.setVisible(bool(hidden))
        self._hidden = [self._crumbs[i][1] for i in hidden]

    def _open_hidden_menu(self, anchor: QToolButton) -> None:
        """The "…" crumb: pick a collapsed folder, then get its usual dropdown."""
        menu = QMenu(self)
        for segment in self._hidden:
            folder: str = segment.property("folder")
            action = menu.addAction(segment.text())
            action.triggered.connect(lambda _=False, f=folder: self._popup_folder(f, anchor))
        menu.popup(anchor.mapToGlobal(QPoint(0, anchor.height())))
        self._menu = menu

    def _popup_folder(self, folder: str, anchor: QToolButton) -> None:
        menu = self.build_menu(folder)
        self._menu = menu
        menu.popup(anchor.mapToGlobal(QPoint(0, anchor.height())))

    def _add_folder_segment(self, folder: str, text: str) -> _Segment:
        segment = _Segment()
        segment.setText(text)
        segment.setProperty("variant", "crumb")
        segment.setCursor(Qt.CursorShape.PointingHandCursor)
        if folder == "":
            t = theme().tokens
            segment.setIcon(QIcon(pixmap("hard-drive", t.text_muted, 15)))
            segment.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        segment.setProperty("folder", folder)
        segment.setToolTip(folder or "Project root")
        segment.clicked.connect(lambda _=False, s=segment: self.open_menu(s))
        self.segments.append(segment)
        self._layout.addWidget(segment)
        return segment

    def open_menu(self, segment: _Segment) -> QMenu:
        if self._menu is not None:
            self._menu.close()
        folder: str = segment.property("folder")
        menu = self.build_menu(folder)
        self._menu = menu
        menu.popup(segment.mapToGlobal(QPoint(0, segment.height())))
        return menu

    def build_menu(self, folder: str) -> QMenu:
        menu = QMenu(self)
        title = basename(folder) or "project root"
        t = theme().tokens
        keep = menu.addAction(
            icon("folder-check", t.success, 16, t.text_faint), f"Keep this folder ({title})"
        )
        keep.triggered.connect(lambda: self.keep_folder_requested.emit(folder))
        delete = menu.addAction(
            icon("folder-x", t.danger, 16, t.text_faint), f"Delete this folder ({title})"
        )
        delete.triggered.connect(lambda: self.delete_folder_requested.emit(folder))
        tag = menu.addAction(icon("tags", t.accent, 16, t.text_faint), "Tag this folder…")
        tag.triggered.connect(lambda: self.tag_folder_requested.emit(folder))
        if folder == "":
            # Folder actions never apply to the project root.
            for action in (keep, delete, tag):
                action.setEnabled(False)
        menu.addSeparator()
        if self._provider is None:
            return menu
        listing = self._provider(folder)
        current = self._path
        for entry in listing.entries[:MAX_MENU_ITEMS]:
            if entry.is_dir:
                count = (
                    f"{entry.item_count:,} item{'' if entry.item_count == 1 else 's'}"
                    if entry.item_count is not None
                    else ""
                )
                size = human_bytes(entry.size) if entry.size is not None else ""
                detail = " · ".join(x for x in (count, size) if x)
                text = f"{entry.name}/" + (f"    {detail}" if detail else "")
            else:
                text = f"{entry.name}    {human_bytes(entry.size)}"
            action = menu.addAction(state_icon(entry.state, entry.origin, entry.categorised), text)
            action.setToolTip(f"{STATE_LABELS[entry.state]} ({entry.origin.value})")
            if entry.is_dir:
                action.setEnabled(False)
            else:
                action.triggered.connect(lambda _=False, p=entry.path: self.jump_requested.emit(p))
            if entry.path == current or join(folder, entry.name) == current:
                font = action.font()
                font.setBold(True)
                action.setFont(font)
        hidden = listing.total - min(len(listing.entries), MAX_MENU_ITEMS)
        if hidden > 0:
            more = menu.addAction(f"… {hidden:,} more")
            more.setEnabled(False)
        return menu
