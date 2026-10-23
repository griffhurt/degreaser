import codecs
import io
import stat
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from degreaser.core.preview.cache import PreviewCache, wipe_all_project_caches
from degreaser.core.preview.loaders import (
    Cancelled,
    CancelToken,
    TextReader,
    exif_thumbnail,
    format_hex,
    line_endings,
    zip_listing,
)
from degreaser.core.sources.local import LocalSource
from tests.fixtures.preview.helpers import RemoteLikeSource, jpeg_bytes, jpeg_with_exif_thumbnail


def test_text_reader_incremental_utf8(tmp_path: Path) -> None:
    text = "ünïcödé line\n" * 5000
    (tmp_path / "t.txt").write_text(text, encoding="utf-8", newline="")
    src = LocalSource(tmp_path)
    size = src.stat("t.txt").size or 0
    reader = TextReader(src, "t.txt", size, "utf-8", bom=False)
    first = reader.consume(src.read_range("t.txt", 0, 1001))  # splits a multibyte char
    rest = reader.read(10**9)
    assert reader.eof
    assert first + rest == text


def test_text_reader_strips_utf16_bom(tmp_path: Path) -> None:
    body = "hello\r\nworld\r\n"
    (tmp_path / "w.txt").write_bytes(codecs.BOM_UTF16_LE + body.encode("utf-16-le"))
    src = LocalSource(tmp_path)
    reader = TextReader(src, "w.txt", src.stat("w.txt").size or 0, "utf-16-le", bom=True)
    assert reader.read(3) + reader.read(100) == body


def test_text_reader_cancel(tmp_path: Path) -> None:
    (tmp_path / "t.txt").write_text("x" * 1000)
    src = LocalSource(tmp_path)
    reader = TextReader(src, "t.txt", 1000, "ascii", bom=False)
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        reader.read(100, token)


def test_line_endings() -> None:
    assert line_endings("a\r\nb\r\n") == "CRLF"
    assert line_endings("a\nb\n") == "LF"
    assert line_endings("a\rb") == "CR"
    assert line_endings("a\r\nb\n") == "Mixed (CRLF, LF)"
    assert line_endings("abc") == "None"


def test_format_hex() -> None:
    out = format_hex(bytes(range(20)), base_offset=0x100).splitlines()
    assert out[0].startswith("00000100  00 01 02 03 04 05 06 07  08 09")
    assert out[0].endswith("|................|")
    assert out[1].startswith("00000110  10 11 12 13")


def test_exif_thumbnail_from_prefix_only() -> None:
    data = jpeg_with_exif_thumbnail((1200, 900), (64, 48))
    thumb = exif_thumbnail(data[: 64 * 1024])
    assert thumb is not None
    assert Image.open(io.BytesIO(thumb)).size == (64, 48)
    assert exif_thumbnail(jpeg_bytes((10, 10))) is None
    assert exif_thumbnail(b"not a jpeg") is None
    assert exif_thumbnail(data[:30]) is None  # truncated mid-segment


def test_zip_listing_reads_only_the_tail(tmp_path: Path) -> None:
    payload = bytes(range(256)) * 8192  # 2 MiB, stored so it really takes space
    with zipfile.ZipFile(tmp_path / "a.zip", "w", zipfile.ZIP_STORED) as z:
        z.writestr("big.bin", payload)
        for i in range(50):
            z.writestr(f"dir/file{i}.txt", f"hello {i}")
        z.writestr("dir/", "")
    src = RemoteLikeSource(tmp_path)
    listing = zip_listing(src, "a.zip", src.stat("a.zip"))
    assert listing.count == 52
    assert listing.total_uncompressed == len(payload) + sum(len(f"hello {i}") for i in range(50))
    assert not listing.any_encrypted
    assert any(e.is_dir for e in listing.entries)
    assert src.bytes_read < 128 * 1024, src.bytes_read
    assert listing.bytes_read == src.bytes_read


def test_zip_listing_truncates_entries(tmp_path: Path) -> None:
    with zipfile.ZipFile(tmp_path / "many.zip", "w") as z:
        for i in range(30):
            z.writestr(f"f{i}", "")
    src = LocalSource(tmp_path)
    listing = zip_listing(src, "many.zip", src.stat("many.zip"), max_entries=10)
    assert (listing.count, len(listing.entries), listing.truncated) == (30, 10, True)


def test_cache_returns_local_paths_without_copying(tmp_path: Path) -> None:
    (tmp_path / "f.bin").write_bytes(b"abc")
    src = LocalSource(tmp_path)
    cache = PreviewCache(tmp_path / "cache")
    assert cache.fetch(src, "f.bin", src.stat("f.bin")) == tmp_path / "f.bin"
    assert not (tmp_path / "cache").exists()


def test_cache_downloads_remote_and_follows_lifecycle(tmp_path: Path) -> None:
    root = tmp_path / "share"
    root.mkdir()
    (root / "f.bin").write_bytes(b"x" * 9_000_000)
    src = RemoteLikeSource(root)
    cache = PreviewCache(tmp_path / "cache")
    seen: list[tuple[int, int]] = []
    path = cache.fetch(src, "f.bin", src.stat("f.bin"), progress=lambda d, t: seen.append((d, t)))
    assert path.read_bytes() == b"x" * 9_000_000
    assert not path.stat().st_mode & stat.S_IWUSR  # read-only copy
    assert seen[-1] == (9_000_000, 9_000_000) and len(seen) == 3  # 4 MiB chunks
    assert cache.fetch(src, "f.bin", src.stat("f.bin")) == path  # cached
    assert src.bytes_read == 9_000_000

    cache.mark("f.bin", on_screen=True, decided=True)
    cache.sweep()
    assert path.exists()  # still on screen
    cache.mark("f.bin", on_screen=False)
    cache.sweep()
    assert not path.exists()


def test_cache_cancel_keeps_only_a_partial_file(tmp_path: Path) -> None:
    root = tmp_path / "share"
    root.mkdir()
    (root / "f.bin").write_bytes(b"x" * 9_000_000)
    src = RemoteLikeSource(root)
    cache = PreviewCache(tmp_path / "cache")
    token = CancelToken()
    with pytest.raises(Cancelled):
        cache.fetch(
            src, "f.bin", src.stat("f.bin"), progress=lambda d, t: token.cancel(), cancel=token
        )
    # The .part stays so a later fetch resumes (test_downloads.py); nothing looks complete.
    [part] = (tmp_path / "cache").iterdir()
    assert part.name.endswith(".part") and cache.cached("f.bin") is None


def test_wipe_all_project_caches(isolated_app_data: Path) -> None:
    cache_dir = isolated_app_data / "projects" / "p1" / "cache"
    cache_dir.mkdir(parents=True)
    leftover = cache_dir / "x.jpg"
    leftover.write_bytes(b"1")
    leftover.chmod(stat.S_IRUSR)
    wipe_all_project_caches()
    assert not cache_dir.exists()
