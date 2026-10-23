import io
import os
import subprocess
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QMarginsF, QPointF
from PySide6.QtGui import QPageSize, QPainter, QPdfWriter
from pytestqt.qtbot import QtBot

from degreaser.core.preview.cache import PreviewCache
from degreaser.core.preview.metadata import MetaRow
from degreaser.core.settings import Downloads
from degreaser.core.sources.local import LocalSource
from degreaser.ui.preview.archive import ArchiveView
from degreaser.ui.preview.ffmpeg_log import set_ffmpeg_log_level
from degreaser.ui.preview.harness import HarnessWindow
from degreaser.ui.preview.hex import HexView
from degreaser.ui.preview.image import ImageView
from degreaser.ui.preview.media import MediaView
from degreaser.ui.preview.pane import PreviewPane
from degreaser.ui.preview.pdf import PdfView
from degreaser.ui.preview.states import MessageView
from degreaser.ui.preview.text import TextView
from degreaser.ui.widgets.metadata_table import MetadataTable
from tests.fixtures.preview.helpers import (
    RemoteLikeSource,
    ffmpeg_available,
    jpeg_with_exif_thumbnail,
    make_video,
    make_wav,
    png_bytes,
)

os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.*.info=false")
TIMEOUT = 10_000


@pytest.fixture
def pane(qtbot: QtBot) -> Iterator[PreviewPane]:
    p = PreviewPane()
    qtbot.addWidget(p)
    p.resize(640, 480)
    p.show()
    yield p
    p.shutdown()


def show(qtbot: QtBot, pane: PreviewPane, source: LocalSource, rel: str) -> None:
    with qtbot.waitSignal(pane.loaded, timeout=TIMEOUT, check_params_cb=lambda r: r == rel):
        pane.show_file(source, rel, source.stat(rel))
    assert not pane.is_loading


def make_pdf(path: Path, pages: int = 2) -> Path:
    writer = QPdfWriter(str(path))
    writer.setTitle("Quarterly report")
    writer.setCreator("Degreaser tests")
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A6))
    writer.setPageMargins(QMarginsF(10, 10, 10, 10))
    painter = QPainter(writer)
    for i in range(pages):
        if i:
            writer.newPage()
        painter.drawText(QPointF(100, 200), f"Page {i + 1}")
    painter.end()
    return path


def test_text_preview_and_load_more(qtbot: QtBot, pane: PreviewPane, tmp_path: Path) -> None:
    line = "the quick brown fox ünïcödé\n"
    body = line * 5000  # ~150 KB: more than the first 64 KB sample
    (tmp_path / "story.txt").write_text(body, encoding="utf-8", newline="")
    src = LocalSource(tmp_path)
    show(qtbot, pane, src, "story.txt")
    view = pane.view
    assert isinstance(view, TextView)
    assert len(view.text().encode()) <= 64 * 1024
    assert view.more.isVisibleTo(view)
    assert pane.expand()
    qtbot.waitUntil(lambda: view.text() == body, timeout=TIMEOUT)
    assert not view.more.isVisibleTo(view)
    assert not pane.expand()


def test_code_is_monospace_and_unwrapped(qtbot: QtBot, pane: PreviewPane, tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hi')\n")
    show(qtbot, pane, LocalSource(tmp_path), "main.py")
    view = pane.view
    assert isinstance(view, TextView)
    assert view.edit.lineWrapMode() == view.edit.LineWrapMode.NoWrap


def test_binary_shows_hex(qtbot: QtBot, pane: PreviewPane, tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(os.urandom(10_000))
    show(qtbot, pane, LocalSource(tmp_path), "blob.bin")
    view = pane.view
    assert isinstance(view, HexView)
    assert view.offset == 4096
    pane.expand()
    qtbot.waitUntil(lambda: view.offset == 10_000, timeout=TIMEOUT)


def test_empty_file(qtbot: QtBot, pane: PreviewPane, tmp_path: Path) -> None:
    (tmp_path / "empty.dat").write_bytes(b"")
    show(qtbot, pane, LocalSource(tmp_path), "empty.dat")
    assert isinstance(pane.view, MessageView)


def test_image_thumbnail_first_then_full_on_space(qtbot: QtBot, tmp_path: Path) -> None:
    (tmp_path / "photo.jpg").write_bytes(jpeg_with_exif_thumbnail((1200, 900), (64, 48)))
    pane = PreviewPane(limits=Downloads(auto_load_bytes=1000))  # force the prompt
    qtbot.addWidget(pane)
    src = LocalSource(tmp_path)
    show(qtbot, pane, src, "photo.jpg")
    view = pane.view
    assert isinstance(view, ImageView) and view.is_thumbnail
    assert pane.expand()
    qtbot.waitUntil(lambda: not view.is_thumbnail, timeout=TIMEOUT)
    assert view.full_size == (1200, 900)
    pane.shutdown()


def test_image_without_thumbnail_prompts_when_large(qtbot: QtBot, tmp_path: Path) -> None:
    (tmp_path / "big.png").write_bytes(png_bytes((400, 300)))
    pane = PreviewPane(limits=Downloads(auto_load_bytes=10))
    qtbot.addWidget(pane)
    show(qtbot, pane, LocalSource(tmp_path), "big.png")
    assert isinstance(pane.view, MessageView)
    assert "Load it" in pane.view.buttons[0].text()
    pane.expand()
    qtbot.waitUntil(lambda: isinstance(pane.view, ImageView), timeout=TIMEOUT)
    pane.shutdown()


def test_small_image_loads_directly(qtbot: QtBot, pane: PreviewPane, tmp_path: Path) -> None:
    (tmp_path / "small.png").write_bytes(png_bytes((50, 40)))
    show(qtbot, pane, LocalSource(tmp_path), "small.png")
    view = pane.view
    assert isinstance(view, ImageView) and view.full_size == (50, 40)


def test_pdf(qtbot: QtBot, pane: PreviewPane, tmp_path: Path) -> None:
    make_pdf(tmp_path / "report.pdf", pages=3)
    show(qtbot, pane, LocalSource(tmp_path), "report.pdf")
    assert isinstance(pane.view, PdfView)
    rows = {r.label: r.value for r in pane.metadata_rows() if r.group == "Document"}
    assert rows["Pages"] == "3"
    assert rows["Title"] == "Quarterly report"


def test_zip_listing_and_corrupt_zip_fallback(
    qtbot: QtBot, pane: PreviewPane, tmp_path: Path
) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i in range(5):
            z.writestr(f"docs/{i}.txt", "x" * i)
    (tmp_path / "a.zip").write_bytes(buf.getvalue())
    (tmp_path / "broken.zip").write_bytes(buf.getvalue()[:40])
    src = LocalSource(tmp_path)
    show(qtbot, pane, src, "a.zip")
    view = pane.view
    assert isinstance(view, ArchiveView) and view.table.topLevelItemCount() == 5
    assert any(r.label == "Entries" and r.value == "5" for r in pane.metadata_rows())
    show(qtbot, pane, src, "broken.zip")
    assert isinstance(pane.view, HexView)


def test_seven_zip_and_encrypted_rar(qtbot: QtBot, pane: PreviewPane, tmp_path: Path) -> None:
    fixtures = Path(__file__).parent.parent / "fixtures" / "archives"
    for name in ("sample-lzma2.7z", "rar5-hpsw.rar"):
        (tmp_path / name).write_bytes((fixtures / name).read_bytes())
    src = LocalSource(tmp_path)
    show(qtbot, pane, src, "sample-lzma2.7z")
    assert isinstance(pane.view, ArchiveView) and pane.view.table.topLevelItemCount() == 5
    show(qtbot, pane, src, "rar5-hpsw.rar")  # names encrypted: a message, not an empty table
    assert isinstance(pane.view, MessageView)
    rows = {r.label: r.value for r in pane.metadata_rows() if r.group == "Archive"}
    assert rows == {"Format": "RAR 5", "Encrypted": "Yes, including file names"}


def test_audio_wav(qtbot: QtBot, pane: PreviewPane, tmp_path: Path) -> None:
    make_wav(tmp_path / "tone.wav")
    show(qtbot, pane, LocalSource(tmp_path), "tone.wav")
    view = pane.view
    assert isinstance(view, MediaView)
    assert not view.video.isVisibleTo(view)
    qtbot.waitUntil(
        lambda: any(r.label == "Duration" for r in pane.metadata_rows()), timeout=TIMEOUT
    )


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg CLI not installed")
def test_video_first_frame_seek_and_teardown(
    qtbot: QtBot, pane: PreviewPane, tmp_path: Path
) -> None:
    set_ffmpeg_log_level()
    try:
        make_video(tmp_path / "clip.avi", "-c:v", "msmpeg4", "-c:a", "pcm_s16le", seconds=3)
    except subprocess.CalledProcessError:
        pytest.skip("this ffmpeg build can't encode msmpeg4")
    show(qtbot, pane, LocalSource(tmp_path), "clip.avi")  # waits for the first frame
    view = pane.view
    assert isinstance(view, MediaView) and view.is_ready()
    rows = {r.label: r.value for r in pane.metadata_rows()}
    qtbot.waitUntil(
        lambda: "Resolution" in {r.label for r in pane.metadata_rows()}, timeout=TIMEOUT
    )
    assert rows["Video codec"] == "MS MPEG-4 v3"  # from the AVI header; Qt says "Unspecified"
    view.seek(2000)
    qtbot.waitUntil(lambda: view.position() >= 1500, timeout=TIMEOUT)
    pane.clear()  # teardown must not hang
    assert pane.view is not view


def test_remote_media_streams_through_the_media_server(qtbot: QtBot, tmp_path: Path) -> None:
    from degreaser.core.preview.media_server import MediaServer

    make_wav(tmp_path / "tone.wav", seconds=2)
    src = RemoteLikeSource(tmp_path)
    server = MediaServer()
    pane = PreviewPane(media_server=server)
    qtbot.addWidget(pane)
    show(qtbot, pane, src, "tone.wav")
    view = pane.view
    assert isinstance(view, MediaView)
    assert view.url.scheme() == "http"
    qtbot.waitUntil(lambda: server.bytes_served > 0, timeout=TIMEOUT)
    assert src.bytes_read > 0
    url = view.url.toString()
    pane.clear()
    assert server.lookup(url.split("/")[3]) is None  # revoked when the preview went away
    server.close()


def test_switching_quickly_drops_stale_results(
    qtbot: QtBot, pane: PreviewPane, tmp_path: Path
) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text(f"contents of {name}\n")
    src = LocalSource(tmp_path)
    loaded: list[str] = []
    pane.loaded.connect(loaded.append)
    pane.show_file(src, "a.txt", src.stat("a.txt"))
    pane.show_file(src, "b.txt", src.stat("b.txt"))
    show(qtbot, pane, src, "c.txt")
    qtbot.wait(100)
    assert loaded == ["c.txt"]
    view = pane.view
    assert isinstance(view, TextView) and view.text() == "contents of c.txt\n"


def test_remote_source_downloads_into_cache(qtbot: QtBot, tmp_path: Path) -> None:
    share = tmp_path / "share"
    share.mkdir()
    make_pdf(share / "doc.pdf")
    src = RemoteLikeSource(share)
    cache = PreviewCache(tmp_path / "cache")
    pane = PreviewPane(cache=cache)
    qtbot.addWidget(pane)
    progress: list[int] = []
    pane.progress.connect(lambda done, _total: progress.append(done))
    show(qtbot, pane, src, "doc.pdf")
    assert isinstance(pane.view, PdfView)
    assert list((tmp_path / "cache").glob("*.pdf"))
    assert progress
    pane.shutdown()


def test_symlink_shows_target(qtbot: QtBot, pane: PreviewPane, tmp_path: Path) -> None:
    if not hasattr(os, "symlink") or os.name == "nt":
        pytest.skip("symlinks need privileges on Windows")
    os.symlink("somewhere/else.txt", tmp_path / "link")
    show(qtbot, pane, LocalSource(tmp_path), "link")
    assert isinstance(pane.view, MessageView)
    assert "somewhere/else.txt" in pane.view.text.text()


def test_failed_preview_offers_open(qtbot: QtBot, pane: PreviewPane, tmp_path: Path) -> None:
    (tmp_path / "fake.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
    src = LocalSource(tmp_path)
    with qtbot.waitSignal(pane.failed, timeout=TIMEOUT):
        pane.show_file(src, "fake.png", src.stat("fake.png"))
    assert isinstance(pane.view, MessageView)
    with qtbot.waitSignal(pane.openRequested, check_params_cb=lambda r: r == "fake.png"):
        pane.view.buttons[0].click()


def test_metadata_table_groups_and_extra_rows(qtbot: QtBot) -> None:
    table = MetadataTable()
    qtbot.addWidget(table)
    table.set_rows([MetaRow("Image", "Dimensions", "4 × 3"), MetaRow("File", "Name", "a.jpg")])
    table.set_extra_rows(
        [MetaRow("Duplicates", "Keep", "b/a.jpg"), MetaRow("", "Decision", "Keep (folder)")]
    )
    headings = [table.topLevelItem(i).text(0) for i in range(table.topLevelItemCount())]  # type: ignore[union-attr]
    assert headings == [
        "File",
        "Name",
        "Triage",
        "Decision",
        "Duplicates",
        "Keep",
        "Image",
        "Dimensions",
    ]
    table.set_rows([MetaRow("File", "Name", "b.jpg")])
    assert table.value("Decision") == "Keep (folder)"
    assert table.value("Name") == "b.jpg"


def test_harness_constructs_and_walks(qtbot: QtBot, tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("hello\n")
    (tmp_path / "z.bin").write_bytes(b"\x00\x01")
    window = HarnessWindow(tmp_path)
    qtbot.addWidget(window)
    assert [window.files.item(i).text() for i in range(window.files.count())] == [
        "z.bin",
        "sub/a.txt",
    ]
    qtbot.waitUntil(lambda: not window.pane.is_loading, timeout=TIMEOUT)
    window.files.setCurrentRow(1)
    qtbot.waitUntil(lambda: isinstance(window.pane.view, TextView), timeout=TIMEOUT)
    assert window.table.value("Name") == "a.txt"
    window.close()
