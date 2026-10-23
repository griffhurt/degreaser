"""Triage over a remote-like source: deferred skips, jump-back, prefetch and Open."""

import threading
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from pytestqt.qtbot import QtBot

from degreaser.core.project import Project
from degreaser.core.settings import Settings
from degreaser.core.triage.session import Banner
from degreaser.ui.windows.triage import TriageWindow
from tests.fixtures.preview.helpers import RemoteLikeSource, png_bytes

TIMEOUT = 5000


class GatedSource(RemoteLikeSource):
    """Reads of `slow` files, other than type detection, block until the gate opens."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.gate = threading.Event()
        self.slow: set[str] = set()

    def read_range(self, rel: str, offset: int, length: int) -> bytes:
        if rel in self.slow and (offset > 0 or length > 64 * 1024):
            self.gate.wait(10)
        return super().read_range(rel, offset, length)


@pytest.fixture
def share(tmp_path: Path) -> Path:
    root = tmp_path / "share"
    root.mkdir()
    (root / "a_photo.png").write_bytes(png_bytes((640, 480)) + b"\0" * 2_000_000)
    (root / "b.txt").write_text("bravo\n")
    (root / "c.txt").write_text("charlie\n")
    return root


def open_window(qtbot: QtBot, root: Path, source: RemoteLikeSource) -> TriageWindow:
    project = Project.create("Remote", {"type": "local", "path": str(root)})
    window = TriageWindow(project, Settings(), source)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window.session.current is not None, timeout=TIMEOUT)
    return window


def current(window: TriageWindow) -> str | None:
    item = window.session.current
    return item.path if item else None


def test_skip_while_loading_defers_then_jumps_back(qtbot: QtBot, share: Path) -> None:
    source = GatedSource(share)
    source.slow.add("a_photo.png")
    window = open_window(qtbot, share, source)
    assert window.downloads is not None
    assert current(window) == "a_photo.png" and window.preview.is_loading

    qtbot.keyClick(window, Qt.Key.Key_S)  # still loading → deferred, not skipped
    assert current(window) == "b.txt"
    assert window.session.state_of("a_photo.png").action.value == "deferred"

    source.gate.set()  # the download finishes in the background
    qtbot.waitUntil(lambda: "a_photo.png" in window.session._ready, timeout=TIMEOUT)
    qtbot.keyClick(window, Qt.Key.Key_K)
    item = window.session.current
    assert item is not None and item.path == "a_photo.png" and item.banner is Banner.LOADED_SKIP
    window.close()


def test_loaded_preview_skips_normally(qtbot: QtBot, share: Path) -> None:
    window = open_window(qtbot, share, RemoteLikeSource(share))
    qtbot.waitUntil(lambda: not window.preview.is_loading, timeout=TIMEOUT)
    qtbot.keyClick(window, Qt.Key.Key_S)
    assert window.session.state_of("a_photo.png").action.value == "skip"
    window.close()


def test_prefetch_prepares_upcoming_files(qtbot: QtBot, share: Path) -> None:
    window = open_window(qtbot, share, RemoteLikeSource(share))
    downloads = window.downloads
    assert downloads is not None
    qtbot.waitUntil(lambda: downloads.prepared("c.txt") is not None, timeout=TIMEOUT)
    prepared = downloads.prepared("b.txt")
    assert prepared is not None and prepared.head == b"bravo\n"
    window.close()


def test_open_downloads_a_read_only_copy(
    qtbot: QtBot, share: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[QUrl] = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url) or True)
    window = open_window(qtbot, share, RemoteLikeSource(share))
    qtbot.keyClick(window, Qt.Key.Key_O)
    qtbot.waitUntil(lambda: bool(opened), timeout=TIMEOUT)
    path = Path(opened[0].toLocalFile())
    assert path.parent == window.preview_cache.dir
    assert path.read_bytes() == (share / "a_photo.png").read_bytes()
    assert not path.stat().st_mode & 0o222  # read-only
    qtbot.keyClick(window, Qt.Key.Key_K)  # deciding doesn't delete an opened copy
    assert path.exists()
    window.close()
    assert not path.exists()  # the cache is wiped when the project closes
