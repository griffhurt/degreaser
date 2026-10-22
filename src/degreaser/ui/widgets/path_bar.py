"""Breadcrumb path bar with hover dropdowns per folder (docs/product-spec.md §4.1)."""

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QEnterEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QToolButton, QWidget

from degreaser.core.model import Effective, Origin
from degreaser.core.sources.base import basename, join
from degreaser.ui.format import human_bytes
from degreaser.ui.widgets.icons import STATE_LABELS, state_icon

HOVER_DELAY_MS = 350
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
    hovered = Signal()
    unhovered = Signal()

    def enterEvent(self, event: QEnterEvent) -> None:
        self.hovered.emit()
        super().enterEvent(event)

    def leaveEvent(self, event: object) -> None:
        self.unhovered.emit()
        super().leaveEvent(event)  # type: ignore[arg-type]


class PathBar(QWidget):
    jump_requested = Signal(str)  # file path
    delete_folder_requested = Signal(str)  # folder path
    tag_folder_requested = Signal(str)  # folder path

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(0)
        self._provider: ChildrenProvider | None = None
        self._path = ""
        self._hover_timer = QTimer(self, singleShot=True, interval=HOVER_DELAY_MS)
        self._hover_timer.timeout.connect(self._open_hovered)
        self._hovered: _Segment | None = None
        self._menu: QMenu | None = None
        self.segments: list[_Segment] = []

    def set_children_provider(self, provider: ChildrenProvider) -> None:
        self._provider = provider

    def set_path(self, file_path: str, root_label: str = "Project") -> None:
        """Show the breadcrumb for a file. Every segment except the last is a folder."""
        self._path = file_path
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item is not None and (widget := item.widget()):
                widget.deleteLater()
        self.segments = []

        parts = file_path.split("/") if file_path else []
        folders = [""] + ["/".join(parts[: i + 1]) for i in range(len(parts) - 1)]
        for i, folder in enumerate(folders):
            if i:
                self._layout.addWidget(self._separator())
            self._add_folder_segment(folder, root_label if folder == "" else basename(folder))
        if parts:
            self._layout.addWidget(self._separator())
            name = QLabel(f"<b>{parts[-1]}</b>")
            name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self._layout.addWidget(name)
        self._layout.addStretch()

    def _separator(self) -> QLabel:
        label = QLabel(" ▸ ")
        label.setEnabled(False)
        return label

    def _add_folder_segment(self, folder: str, text: str) -> None:
        segment = _Segment()
        segment.setText(text)
        segment.setAutoRaise(True)
        segment.setProperty("folder", folder)
        segment.setToolTip(folder or "Project root")
        segment.clicked.connect(lambda _=False, s=segment: self.open_menu(s))
        segment.hovered.connect(lambda s=segment: self._start_hover(s))
        segment.unhovered.connect(self._hover_timer.stop)
        self.segments.append(segment)
        self._layout.addWidget(segment)

    def _start_hover(self, segment: _Segment) -> None:
        self._hovered = segment
        self._hover_timer.start()

    def _open_hovered(self) -> None:
        if self._hovered is not None and self._hovered.underMouse():
            self.open_menu(self._hovered)

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
        menu.addAction(f"Delete this folder ({title})").triggered.connect(
            lambda: self.delete_folder_requested.emit(folder)
        )
        menu.addAction("Tag this folder…").triggered.connect(
            lambda: self.tag_folder_requested.emit(folder)
        )
        menu.addSeparator()
        if self._provider is None:
            return menu
        listing = self._provider(folder)
        current = self._path
        for entry in listing.entries[:MAX_MENU_ITEMS]:
            if entry.is_dir:
                count = f"{entry.item_count:,} items" if entry.item_count is not None else ""
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
