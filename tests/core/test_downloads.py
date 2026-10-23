import threading
from pathlib import Path

import pytest

from degreaser.core.preview.cache import PreviewCache
from degreaser.core.preview.detect import Kind
from degreaser.core.preview.downloads import DownloadQueue, NotDeferrable, Prepared, Priority
from degreaser.core.preview.loaders import Cancelled, CancelToken
from degreaser.core.settings import Downloads
from tests.fixtures.preview.helpers import RemoteLikeSource, png_bytes


class Recorder:
    def __init__(self) -> None:
        self.done: dict[str, tuple[Priority, Prepared]] = {}
        self.failed: dict[str, tuple[Priority, BaseException]] = {}
        self.event = threading.Condition()

    def on_done(self, rel: str, priority: Priority, prepared: Prepared) -> None:
        with self.event:
            self.done[rel] = (priority, prepared)
            self.event.notify_all()

    def on_failed(self, rel: str, priority: Priority, exc: BaseException) -> None:
        with self.event:
            self.failed[rel] = (priority, exc)
            self.event.notify_all()

    def wait(self, *rels: str) -> None:
        with self.event:
            ok = self.event.wait_for(
                lambda: all(r in self.done or r in self.failed for r in rels), timeout=5
            )
        assert ok, f"timed out waiting for {rels}"


@pytest.fixture
def share(tmp_path: Path) -> Path:
    root = tmp_path / "share"
    root.mkdir()
    (root / "notes.txt").write_text("hello\n" * 100)
    (root / "photo.png").write_bytes(png_bytes((64, 48)))
    (root / "big.bin").write_bytes(b"\x00\x01" * 50_000)
    return root


def make_queue(share: Path, tmp_path: Path, **limits: int) -> tuple[DownloadQueue, Recorder]:
    rec = Recorder()
    source = RemoteLikeSource(share)
    queue = DownloadQueue(
        source,
        PreviewCache(tmp_path / "cache"),
        Downloads(**limits),
        on_done=rec.on_done,
        on_failed=rec.on_failed,
    )
    return queue, rec


def test_prefetch_downloads_only_what_previews_need(share: Path, tmp_path: Path) -> None:
    queue, rec = make_queue(share, tmp_path)
    src = queue.source
    for rel in ("notes.txt", "photo.png", "big.bin"):
        queue.request(rel, src.stat(rel), Priority.PREFETCH)
    rec.wait("notes.txt", "photo.png", "big.bin")
    text = rec.done["notes.txt"][1]
    assert text.detection is not None and text.detection.kind is Kind.TEXT
    assert text.path is None and text.head.startswith(b"hello")
    image = rec.done["photo.png"][1]
    assert image.path is not None and image.path.read_bytes() == (share / "photo.png").read_bytes()
    assert rec.done["big.bin"][1].path is None  # hex previews read samples only
    assert queue.prepared("photo.png") == image
    queue.close()


def test_open_always_downloads_and_reuses_prepared(share: Path, tmp_path: Path) -> None:
    queue, rec = make_queue(share, tmp_path)
    queue.request("notes.txt", queue.source.stat("notes.txt"), Priority.PREFETCH)
    rec.wait("notes.txt")
    del rec.done["notes.txt"]
    queue.request("notes.txt", queue.source.stat("notes.txt"), Priority.OPEN)
    rec.wait("notes.txt")
    priority, prepared = rec.done["notes.txt"]
    assert priority is Priority.OPEN and prepared.path is not None
    queue.close()


def test_deferral_that_needs_confirming_fails(share: Path, tmp_path: Path) -> None:
    queue, rec = make_queue(share, tmp_path, auto_load_bytes=10)  # the PNG is "too big"
    queue.request("photo.png", queue.source.stat("photo.png"), Priority.DEFERRED)
    rec.wait("photo.png")
    assert isinstance(rec.failed["photo.png"][1], NotDeferrable)
    queue.close()


def test_prefetch_respects_the_cache_cap(share: Path, tmp_path: Path) -> None:
    queue, rec = make_queue(share, tmp_path, cache_cap_bytes=10)
    queue.request("photo.png", queue.source.stat("photo.png"), Priority.PREFETCH)
    rec.wait("photo.png")
    assert rec.done["photo.png"][1].path is None
    queue.close()


def test_retain_only_cancels_stale_prefetch(share: Path, tmp_path: Path) -> None:
    gate = threading.Event()
    queue, rec = make_queue(share, tmp_path, max_concurrent=1)
    source = queue.source
    original = source.read_range

    def slow(rel: str, offset: int, length: int) -> bytes:
        gate.wait(5)
        return original(rel, offset, length)

    source.read_range = slow  # type: ignore[method-assign]
    queue.request("notes.txt", source.stat("notes.txt"), Priority.PREFETCH)
    queue.request("big.bin", source.stat("big.bin"), Priority.PREFETCH)
    queue.retain_only(Priority.PREFETCH, {"notes.txt"})
    assert queue.pending() == ["notes.txt"]
    gate.set()
    rec.wait("notes.txt")
    assert "big.bin" not in rec.done
    queue.close()


def test_cancelled_download_resumes(share: Path, tmp_path: Path) -> None:
    (share / "movie.bin").write_bytes(bytes(range(256)) * 40_000)  # ~10 MB: three chunks
    source = RemoteLikeSource(share)
    cache = PreviewCache(tmp_path / "cache")
    entry = source.stat("movie.bin")
    cancel = CancelToken()

    def stop_after_first(done: int, _total: int) -> None:
        cancel.cancel()

    with pytest.raises(Cancelled):
        cache.fetch(source, "movie.bin", entry, stop_after_first, cancel)
    read_before = source.bytes_read
    path = cache.fetch(source, "movie.bin", entry)
    assert path.read_bytes() == (share / "movie.bin").read_bytes()
    assert source.bytes_read - read_before < entry.size  # type: ignore[operator]
