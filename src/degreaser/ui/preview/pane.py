"""PreviewPane: shows one file at a time, loading it on a worker pool (docs/previews.md).

Lifecycle per file: show_file() → detect + metadata + plan on a worker → a renderer is swapped
in → `loaded` once there is something worth looking at (the deferred-skip logic keys off
`is_loading`). Every callback is tagged with a generation number, so results that arrive after
the user has moved on are dropped, and the CancelToken stops long reads early.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QStackedLayout, QWidget

from degreaser.core.preview.cache import PreviewCache
from degreaser.core.preview.detect import Detection, Kind, detect
from degreaser.core.preview.loaders import (
    Cancelled,
    CancelToken,
    TextReader,
    exif_thumbnail,
    read_sample,
    zip_listing,
)
from degreaser.core.preview.metadata import (
    BasicMetadata,
    MetaRow,
    collect,
    common_rows,
    format_size,
    symlink_rows,
    zip_rows,
)
from degreaser.core.preview.plan import EXIF_PREFIX, Confirm, Plan, Strategy, plan_preview
from degreaser.core.settings import Downloads, Video
from degreaser.core.sources.base import Entry, FileSource
from degreaser.ui.preview.archive import ArchiveView
from degreaser.ui.preview.hex import HexView
from degreaser.ui.preview.image import ImageView, decode_image
from degreaser.ui.preview.media import MediaView
from degreaser.ui.preview.pdf import PdfView
from degreaser.ui.preview.states import LoadingView, MessageView
from degreaser.ui.preview.text import TextView
from degreaser.ui.preview.worker import ProgressFn, Worker

# Given (source, rel, entry), return a URL QMediaPlayer can stream, or None. M2's loopback
# media server plugs in here for SMB; without one, remote media falls back to a full download.
MediaUrlProvider = Callable[[FileSource, str, Entry], QUrl | None]


@dataclass(slots=True)
class _Prepared:
    detection: Detection
    head: bytes
    plan: Plan
    meta: BasicMetadata
    thumbnail: QImage | None


class PreviewPane(QWidget):
    loadingStarted = Signal(str)  # rel
    progress = Signal(int, int)  # bytes done, total (full-file loads)
    loaded = Signal(str)  # rel: something useful is on screen
    failed = Signal(str, str)  # rel, message
    metadataChanged = Signal(str, list)  # rel, list[MetaRow]
    openRequested = Signal(str)  # rel: user clicked "Open in default app"

    def __init__(
        self,
        limits: Downloads | None = None,
        video: Video | None = None,
        cache: PreviewCache | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.limits = limits or Downloads()
        self.video_settings = video or Video()
        self.cache = cache
        self.media_url_provider: MediaUrlProvider | None = None
        self.worker = Worker(self)
        self._stack = QStackedLayout(self)
        self._view: QWidget | None = None
        self._gen = 0
        self._cancel = CancelToken()
        self._source: FileSource | None = None
        self._entry: Entry | None = None
        self.rel: str | None = None
        self.detection: Detection | None = None
        self.plan: Plan | None = None
        self.is_loading = False
        self._rows: list[MetaRow] = []
        self._pending_confirm: Callable[[], None] | None = None
        self._expand: Callable[[], None] | None = None
        self._text_reader: TextReader | None = None
        self._set_view(QWidget())

    # Public API ----------------------------------------------------------------------------------

    def show_file(
        self,
        source: FileSource,
        rel: str,
        entry: Entry,
        detection: Detection | None = None,
        head: bytes | None = None,
    ) -> None:
        """Start previewing a file. Safe to call again at any time; the old load is dropped."""
        self._reset()
        self._source, self.rel, self._entry = source, rel, entry
        if self.cache:
            self.cache.mark(rel, on_screen=True)
        self.is_loading = True
        self._set_view(LoadingView())
        self.loadingStarted.emit(rel)
        gen, cancel = self._gen, self._cancel

        if entry.kind != "file":
            self._show_special(source, rel, entry)
            return

        def prepare(_progress: ProgressFn) -> _Prepared:
            det, hd = (
                (detection, head) if detection and head is not None else detect(source, rel, entry)
            )
            cancel.check()
            plan = plan_preview(det, entry, self.limits, local=source.local_path(rel) is not None)
            meta = collect(source, rel, entry, det, hd)
            thumb = None
            if plan.exif_thumbnail_first:
                raw = exif_thumbnail(hd[:EXIF_PREFIX])
                image = decode_image(raw) if raw else None
                thumb = image if image is not None and not image.isNull() else None
            return _Prepared(det, hd, plan, meta, thumb)

        self.worker.submit(
            prepare,
            lambda r: self._guard(gen, self._on_prepared, r),
            lambda e: self._guard(gen, self._on_failed, e),
        )

    def clear(self) -> None:
        self._reset()
        self._set_view(QWidget())

    def expand(self) -> bool:
        """Space: accept a pending prompt, or load more. False if there was nothing to do."""
        if self._pending_confirm is not None:
            confirm, self._pending_confirm = self._pending_confirm, None
            confirm()
            return True
        if self._expand is not None:
            self._expand()
            return True
        return False

    def metadata_rows(self) -> list[MetaRow]:
        return list(self._rows)

    @property
    def view(self) -> QWidget | None:
        return self._view

    def shutdown(self) -> None:
        """Stop everything (window closing). Waits briefly for workers to finish."""
        self._reset()
        self.worker.wait(2000)

    # Internals ---------------------------------------------------------------------------------

    def _reset(self) -> None:
        self._gen += 1
        self._cancel.cancel()
        self._cancel = CancelToken()
        self.worker.forget_all()
        if self.cache and self.rel is not None:
            self.cache.mark(self.rel, on_screen=False)
        self.rel = None
        self.detection = None
        self.plan = None
        self.is_loading = False
        self._rows = []
        self._pending_confirm = None
        self._expand = None
        self._text_reader = None

    def _guard(self, gen: int, fn: Callable[..., None], *args: object) -> None:
        if gen == self._gen:
            fn(*args)

    def _set_view(self, view: QWidget) -> None:
        old = self._view
        self._view = view
        self._stack.addWidget(view)
        self._stack.setCurrentWidget(view)
        if old is not None:
            teardown = getattr(old, "teardown", None)
            if teardown:
                teardown()
            self._stack.removeWidget(old)
            old.deleteLater()

    def _finish_loading(self) -> None:
        if self.is_loading:
            self.is_loading = False
            if self.rel is not None:
                self.loaded.emit(self.rel)

    def _emit_rows(self) -> None:
        if self.rel is not None:
            self.metadataChanged.emit(self.rel, list(self._rows))

    def _replace_group(self, group: str, rows: list[MetaRow]) -> None:
        self._rows = [r for r in self._rows if r.group != group] + rows
        self._emit_rows()

    def _on_failed(self, exc: BaseException) -> None:
        if isinstance(exc, Cancelled):
            return
        message = str(exc) or type(exc).__name__
        self._show_message("Preview failed", message, with_open=True)
        self.is_loading = False
        if self.rel is not None:
            self.failed.emit(self.rel, message)

    def _show_message(self, title: str, text: str = "", *, with_open: bool = False) -> None:
        rel = self.rel
        buttons = (
            [("Open in default app", lambda: self.openRequested.emit(rel))]
            if with_open and rel
            else []
        )
        self._set_view(MessageView(title, text, buttons))

    def _show_special(self, source: FileSource, rel: str, entry: Entry) -> None:
        self._rows = common_rows(rel, entry, None)
        if entry.kind == "symlink":
            self._rows += symlink_rows(source, rel)
            target = next((r.value for r in self._rows if r.label == "Target"), "")
            self._show_message("Symbolic link", f"→ {target}" if target else "")
        elif entry.kind == "dir":
            self._show_message("Folder")
        else:
            self._show_message("Special file", "Devices, sockets and pipes can't be previewed.")
        self._emit_rows()
        self._finish_loading()

    def _on_prepared(self, prepared: _Prepared) -> None:
        self.detection, self.plan = prepared.detection, prepared.plan
        self._rows = list(prepared.meta.rows)
        self._emit_rows()
        if self._entry is not None and self._entry.size == 0:
            self._show_message("Empty file", "This file has no contents.")
            self._finish_loading()
            return
        match prepared.plan.strategy:
            case Strategy.TEXT_SAMPLE:
                self._start_text(prepared)
            case Strategy.HEX_SAMPLE:
                self._start_hex(prepared)
            case Strategy.IMAGE:
                self._start_image(prepared)
            case Strategy.PDF:
                self._start_pdf(prepared)
            case Strategy.MEDIA:
                self._start_media(prepared)
            case Strategy.ZIP_LISTING:
                self._start_zip(prepared)
            case Strategy.NONE:
                size = self._entry.size if self._entry else 0
                if size == 0:
                    self._show_message("Empty file", "This file has no contents.")
                else:
                    self._show_message(
                        "No preview", "This file type can't be previewed.", with_open=True
                    )
                self._finish_loading()

    # Text and hex --------------------------------------------------------------------------------

    def _start_text(self, prepared: _Prepared) -> None:
        source, rel, entry, det = self._source, self.rel, self._entry, prepared.detection
        assert source and rel is not None and entry
        size = entry.size or 0
        view = TextView(monospace=det.kind == Kind.CODE, wrap=det.kind != Kind.CODE)
        reader = TextReader(source, rel, size, det.encoding or "utf-8", det.bom)
        self._text_reader = reader
        view.append(reader.consume(prepared.head[: prepared.plan.initial_bytes]))
        ceiling = prepared.plan.ceiling_bytes

        def update() -> None:
            more = not reader.eof and reader.offset < ceiling
            view.set_progress(reader.offset, reader.size, can_load_more=more)
            self._expand = load_more if more else None

        def load_more() -> None:
            gen, cancel = self._gen, self._cancel
            step = min(prepared.plan.step_bytes, ceiling - reader.offset)
            view.set_busy(True)
            self._expand = None

            def done(text: str) -> None:
                view.set_busy(False)
                view.append(text)
                update()

            self.worker.submit(
                lambda _p: reader.read(step, cancel),
                lambda text: self._guard(gen, done, text),
                lambda e: self._guard(gen, self._on_failed, e),
            )

        view.loadMoreRequested.connect(lambda: self._expand() if self._expand else None)
        self._set_view(view)
        update()
        self._finish_loading()

    def _start_hex(self, prepared: _Prepared) -> None:
        source, rel, entry = self._source, self.rel, self._entry
        assert source and rel is not None and entry
        size = entry.size or 0
        plan = prepared.plan
        view = HexView()
        view.append_bytes(prepared.head[: plan.initial_bytes])
        limit = min(size, plan.ceiling_bytes)

        def update() -> None:
            more = view.offset < limit
            view.set_progress(view.offset, size, can_load_more=more)
            self._expand = load_more if more else None

        def load_more() -> None:
            gen, offset = self._gen, view.offset
            length = min(plan.step_bytes, limit - offset)
            view.set_busy(True)
            self._expand = None

            def done(data: bytes) -> None:
                view.set_busy(False)
                view.append_bytes(data)
                update()

            self.worker.submit(
                lambda _p: read_sample(source, rel, offset, length),
                lambda data: self._guard(gen, done, data),
                lambda e: self._guard(gen, self._on_failed, e),
            )

        view.loadMoreRequested.connect(lambda: self._expand() if self._expand else None)
        self._set_view(view)
        update()
        self._finish_loading()

    # Full-file kinds -----------------------------------------------------------------------------

    def _fetch_then(
        self, then: Callable[[Path], object], on_result: Callable[[object], None]
    ) -> None:
        """Get a local path for the whole file (no copy for local sources), then `then(path)`
        on the worker; `on_result` gets its result on the UI thread."""
        source, rel, entry = self._source, self.rel, self._entry
        assert source and rel is not None and entry
        gen, cancel, cache = self._gen, self._cancel, self.cache

        def job(progress: ProgressFn) -> object:
            local = source.local_path(rel)
            if local is None:
                if cache is None:
                    raise RuntimeError("Previewing remote files needs a preview cache.")
                local = cache.fetch(source, rel, entry, progress, cancel)
            cancel.check()
            return then(local)

        def on_progress(done: int, total: int) -> None:
            self.progress.emit(done, total)
            if isinstance(self._view, LoadingView):
                self._view.set_progress(done, total)

        self.worker.submit(
            job,
            lambda r: self._guard(gen, on_result, r),
            lambda e: self._guard(gen, self._on_failed, e),
            lambda d, t: self._guard(gen, on_progress, d, t),
        )

    def _confirm_prompt(self, confirm: Confirm, what: str, accept: Callable[[], None]) -> None:
        size = format_size(self._entry.size if self._entry else 0).split(" (")[0]
        if confirm == Confirm.LARGE_DOWNLOAD:
            title = f"This file is {size} and must be fully downloaded to preview."
        else:
            title = f"This {what} is {size}."
        rel = self.rel

        def go() -> None:
            self._pending_confirm = None
            accept()

        def skip() -> None:
            self._pending_confirm = None
            self._show_message(
                "Preview skipped", "Use Open to view it in another app.", with_open=True
            )

        label = "Download (Space)" if confirm == Confirm.LARGE_DOWNLOAD else "Load it (Space)"
        self._set_view(
            MessageView(
                title,
                "",
                [
                    (label, go),
                    ("Don't preview", skip),
                    ("Open in default app", lambda: self.openRequested.emit(rel)),
                ],
            )
        )
        self._pending_confirm = go
        self._finish_loading()

    def _start_image(self, prepared: _Prepared) -> None:
        view: ImageView | None = None
        if prepared.thumbnail is not None:
            view = ImageView()
            view.set_image(prepared.thumbnail, thumbnail=True)
            self._set_view(view)
            self._finish_loading()  # a thumbnail is enough to make a decision

        def load_full() -> None:
            nonlocal view
            if view is None or self._view is not view:
                view = ImageView()
                self._set_view(LoadingView())
            else:
                view.set_status("Loading full image…")
            target = view

            def show(image: object) -> None:
                assert isinstance(image, QImage)
                if image.isNull():
                    self._on_failed(RuntimeError("This image couldn't be decoded."))
                    return
                if self._view is not target:
                    self._set_view(target)
                target.set_image(image)
                self._finish_loading()

            self._fetch_then(decode_image, show)

        if prepared.plan.confirm is not None:
            if view is not None:
                size = format_size(self._entry.size if self._entry else 0).split(" (")[0]
                view.set_status(
                    f"Embedded thumbnail · full image is {size} — press Space to load it"
                )
                self._pending_confirm = load_full
            else:
                self._confirm_prompt(prepared.plan.confirm, "image", load_full)
        else:
            load_full()

    def _start_pdf(self, prepared: _Prepared) -> None:
        def load() -> None:
            self._set_view(LoadingView())

            def show(path: object) -> None:
                assert isinstance(path, Path)
                view = PdfView()
                error = view.load(path)
                if error:
                    view.teardown()
                    self._on_failed(RuntimeError(error))
                    return
                self._set_view(view)
                self._replace_group("Document", view.metadata_rows())
                self._finish_loading()

            self._fetch_then(lambda p: p, show)

        if prepared.plan.confirm is not None:
            self._confirm_prompt(prepared.plan.confirm, "PDF", load)
        else:
            load()

    def _start_media(self, prepared: _Prepared) -> None:
        source, rel, entry = self._source, self.rel, self._entry
        assert source and rel is not None and entry
        container = prepared.meta.container

        def play(url: QUrl) -> None:
            view = MediaView(
                url, autoplay_muted=self.video_settings.autoplay_muted, container=container
            )
            gen = self._gen
            view.ready.connect(lambda: self._guard(gen, self._finish_loading))
            view.failed.connect(lambda msg: self._guard(gen, self._on_failed, RuntimeError(msg)))
            view.metadataChanged.connect(
                lambda: self._guard(gen, self._replace_group, "Media", view.metadata_rows())
            )
            self._set_view(view)

        local = source.local_path(rel)
        if local is not None:
            play(QUrl.fromLocalFile(os.fspath(local)))
            return
        url = self.media_url_provider(source, rel, entry) if self.media_url_provider else None
        if url is not None:
            play(url)
            return

        # No streaming available: progressive download fallback (docs/previews.md §2).
        def download() -> None:
            self._set_view(LoadingView())
            self._fetch_then(lambda p: p, lambda p: play(QUrl.fromLocalFile(os.fspath(p))))  # type: ignore[arg-type]

        if (entry.size or 0) > self.limits.large_warning_bytes:
            self._confirm_prompt(Confirm.LARGE_DOWNLOAD, "file", download)
        else:
            download()

    def _start_zip(self, prepared: _Prepared) -> None:
        source, rel, entry = self._source, self.rel, self._entry
        assert source and rel is not None and entry
        gen, cancel = self._gen, self._cancel

        def show(listing: object) -> None:
            from degreaser.core.preview.loaders import ZipListing

            assert isinstance(listing, ZipListing)
            self._set_view(ArchiveView(listing))
            self._replace_group("Archive", zip_rows(listing))
            self._finish_loading()

        def fallback(exc: BaseException) -> None:
            if isinstance(exc, Cancelled):
                return
            # Not a readable zip after all (truncated, self-extracting, …): show bytes instead.
            self._rows.append(MetaRow("Archive", "Listing", f"unreadable ({exc})", warning=True))
            self._emit_rows()
            hex_plan = plan_preview(
                Detection(Kind.BINARY, prepared.detection.label), entry, self.limits, local=True
            )
            self._start_hex(
                _Prepared(prepared.detection, prepared.head, hex_plan, prepared.meta, None)
            )

        self.worker.submit(
            lambda _p: zip_listing(source, rel, entry, cancel),
            lambda r: self._guard(gen, show, r),
            lambda e: self._guard(gen, fallback, e),
        )
