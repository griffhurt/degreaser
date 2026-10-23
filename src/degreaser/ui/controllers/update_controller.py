"""Picks the update backend for this build and runs checks (docs/decisions.md D41).

- macOS with Sparkle.framework bundled → Sparkle, which shows its own windows.
- Windows with WinSparkle.dll bundled → WinSparkle, likewise.
- Any other packaged build (Linux AppImage) → a feed check here: at most daily, then a dialog
  with the release notes and a link to the download.
- Development runs → nothing (Check Now is hidden).
"""

import os
import sys
import time
from typing import Any, Protocol

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from degreaser import __version__
from degreaser.core import settings as settings_mod
from degreaser.core.settings import Settings
from degreaser.ui.controllers.tasks import Task, submit
from degreaser.updates import bundle_root, public_key
from degreaser.updates.feed import Release, feed_url, fetch_latest, is_newer

FEED_CHECK_INTERVAL = 24 * 3600
FIRST_CHECK_DELAY_MS = 5_000
# Testing a feed without a packaged build: DEGREASER_UPDATE_FEED=http://localhost:8000/x.xml
FEED_OVERRIDE = "DEGREASER_UPDATE_FEED"


class Backend(Protocol):
    name: str

    def set_automatic(self, automatic: bool) -> None: ...
    def check_now(self) -> None: ...
    def shutdown(self) -> None: ...


class UpdateController(QObject):
    # Emitted from WinSparkle's thread; queued to the UI thread, which quits the app.
    _shutdown_requested = Signal()

    def __init__(self, settings: Settings, parent_widget: QWidget | None = None) -> None:
        super().__init__()
        self.settings = settings
        self.parent_widget = parent_widget
        self.backend: Backend | None = None
        self.feed: str | None = None  # set when this controller checks the feed itself
        self.error = ""  # why the native updater couldn't start, if it couldn't
        self._tasks: set[Task] = set()
        self._shutdown_requested.connect(self._quit)
        self._choose()

    @property
    def available(self) -> bool:
        return self.backend is not None or self.feed is not None

    def _choose(self) -> None:
        if override := os.environ.get(FEED_OVERRIDE):
            self.feed = override
            return
        root = bundle_root()
        if root is None or QGuiApplication.platformName() == "offscreen":
            return  # development, or a headless smoke test (the updaters need a real GUI)
        key = public_key()
        automatic = self.settings.updates.check_automatically
        try:
            if sys.platform == "darwin" and key:
                framework = root / "Frameworks" / "Sparkle.framework"
                if framework.is_dir():
                    from degreaser.updates.sparkle import SparkleUpdater

                    self.backend = SparkleUpdater(framework, automatic=automatic)
                    return
            if sys.platform == "win32" and key:
                from degreaser.updates.winsparkle import DLL_NAME, WinSparkleUpdater

                dll = root / DLL_NAME
                if dll.is_file():
                    self.backend = WinSparkleUpdater(
                        dll,
                        feed=feed_url("windows"),
                        public_key=key,
                        version=__version__,
                        automatic=automatic,
                        request_shutdown=self._shutdown_requested.emit,
                    )
                    return
        except OSError as exc:
            self.error = str(exc)  # fall back to the feed check
        self.feed = feed_url()

    # Lifecycle ------------------------------------------------------------------------------

    def start(self) -> None:
        """Schedule the first automatic feed check (the native updaters schedule their own)."""
        if self.feed is not None:
            QTimer.singleShot(FIRST_CHECK_DELAY_MS, self, self._automatic_check)

    def apply_settings(self) -> None:
        if self.backend is not None:
            self.backend.set_automatic(self.settings.updates.check_automatically)

    def shutdown(self) -> None:
        if self.backend is not None:
            self.backend.shutdown()

    def _quit(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.quit()

    # Checks ---------------------------------------------------------------------------------

    def check_now(self) -> None:
        if self.backend is not None:
            self.backend.check_now()
        elif self.feed is not None:
            self._fetch(interactive=True)

    def _automatic_check(self) -> None:
        updates = self.settings.updates
        if not updates.check_automatically:
            return
        if time.time() - updates.last_check < FEED_CHECK_INTERVAL:
            return
        self._fetch(interactive=False)

    def _fetch(self, *, interactive: bool) -> None:
        url = self.feed
        assert url is not None

        def done(release: Any) -> None:
            if not interactive:
                self.settings.updates.last_check = time.time()
                settings_mod.save(self.settings)
            self._show(release if isinstance(release, Release) else None, interactive)

        def failed(exc: BaseException) -> None:
            if interactive:
                QMessageBox.warning(
                    self.parent_widget,
                    "Couldn't check for updates",
                    f"The update feed couldn't be read: {exc}",
                )

        submit(
            lambda _p: fetch_latest(url),
            on_result=done,
            on_error=failed,
            keep_alive=self._tasks,
        )

    def _show(self, release: Release | None, interactive: bool) -> None:
        if release is None or not is_newer(release.version):
            if interactive:
                QMessageBox.information(
                    self.parent_widget,
                    "No updates",
                    f"Degreaser {__version__} is the latest version.",
                )
            return
        if not interactive and release.version == self.settings.updates.dismissed:
            return
        box = QMessageBox(self.parent_widget)
        box.setWindowTitle("Update available")
        box.setText(f"Degreaser {release.version} is available. You have {__version__}.")
        if release.notes_html:
            box.setInformativeText(release.notes_html)
        download = box.addButton("Download", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is download:
            QDesktopServices.openUrl(QUrl(release.page_url))
        else:
            self.settings.updates.dismissed = release.version
            settings_mod.save(self.settings)
