"""Breadcrumb path bar; clicking a folder opens its dropdown (docs/product-spec.md §4.1)."""

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QToolButton, QWidget

from degreaser.core.model import Effective, Origin
from degreaser.core.sources.base import basename, join
from degreaser.ui.format import human_bytes
from degreaser.ui.theme import icon, pixmap, theme
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
    "file-archive": {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "dmg", "iso"},
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

        parts = file_path.split("/") if file_path else []
        folders = [""] + ["/".join(parts[: i + 1]) for i in range(len(parts) - 1)]
        for i, folder in enumerate(folders):
            if i:
                self._layout.addWidget(self._separator())
            self._add_folder_segment(folder, root_label if folder == "" else basename(folder))
        if parts:
            t = theme().tokens
            self._layout.addWidget(self._separator())
            file_icon = QLabel()
            file_icon.setPixmap(pixmap(file_icon_name(parts[-1]), t.accent, 16))
            self._layout.addSpacing(6)
            self._layout.addWidget(file_icon)
            name = QLabel(parts[-1])
            name.setStyleSheet(
                f"font-weight: 600; font-size: 14px; color: {t.text}; padding-left: 6px;"
            )
            name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self._layout.addWidget(name)
        self._layout.addStretch()

    def _separator(self) -> QLabel:
        label = QLabel()
        label.setPixmap(pixmap("chevron-right", theme().tokens.text_faint, 14))
        return label

    def _add_folder_segment(self, folder: str, text: str) -> None:
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
