"""Archive listings (core/preview/archives.py). 7z and RAR samples are in tests/fixtures/archives:
the 7z ones were made with libarchive (bsdtar) and py7zr, the RAR ones come from rarfile's
test suite (LICENSE-rarfile.txt)."""

import gzip
import io
import shutil
import tarfile
import zipfile
from compression import zstd
from pathlib import Path

import pytest

from degreaser.core.preview.archives import (
    ArchiveError,
    ArchiveListing,
    archive_format,
    archive_listing,
)
from degreaser.core.preview.detect import Kind, detect_bytes
from degreaser.core.preview.metadata import archive_rows
from degreaser.core.sources.local import LocalSource
from tests.fixtures.preview.helpers import RemoteLikeSource

FIXTURES = Path(__file__).parent.parent / "fixtures" / "archives"


def listing_of(
    root: Path, name: str, *, max_entries: int = 10_000, budget: int = 256 * 1024 * 1024
) -> ArchiveListing:
    src = LocalSource(root)
    return archive_listing(src, name, src.stat(name), max_entries=max_entries, budget=budget)


def names(listing: ArchiveListing) -> list[str]:
    return [e.name for e in listing.entries]


# Zip ------------------------------------------------------------------------------------------


def test_zip_reads_only_the_tail(tmp_path: Path) -> None:
    payload = bytes(range(256)) * 8192  # 2 MiB, stored so it really takes space
    with zipfile.ZipFile(tmp_path / "a.zip", "w", zipfile.ZIP_STORED) as z:
        z.writestr("big.bin", payload)
        for i in range(50):
            z.writestr(f"dir/file{i}.txt", f"hello {i}")
        z.writestr("dir/", "")
    src = RemoteLikeSource(tmp_path)
    listing = archive_listing(src, "a.zip", src.stat("a.zip"))
    assert listing.format == "Zip"
    assert listing.count == 52
    assert listing.total_uncompressed == len(payload) + sum(len(f"hello {i}") for i in range(50))
    assert not listing.any_encrypted
    assert any(e.is_dir for e in listing.entries)
    assert src.bytes_read < 128 * 1024, src.bytes_read


def test_zip_truncates_entries(tmp_path: Path) -> None:
    with zipfile.ZipFile(tmp_path / "many.zip", "w") as z:
        for i in range(30):
            z.writestr(f"f{i}", "")
    listing = listing_of(tmp_path, "many.zip", max_entries=10)
    assert (listing.count, len(listing.entries), listing.truncated) == (30, 10, True)


# 7z -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["lzma2", "lzma1", "store", "deflate", "bzip2"])
def test_seven_zip_methods(method: str) -> None:
    src = RemoteLikeSource(FIXTURES)
    name = f"sample-{method}.7z"
    listing = archive_listing(src, name, src.stat(name))
    assert listing.format == "7z"
    by = {e.name: e for e in listing.entries}
    assert set(by) == {"a.txt", "docs/long.txt", "docs/zero.txt", "docs/empty/", "docs/"}
    assert by["a.txt"].size == 6 and by["docs/long.txt"].size == 5000
    assert by["docs/zero.txt"].size == 0 and not by["docs/zero.txt"].is_dir
    assert by["docs/empty/"].is_dir
    assert by["a.txt"].modified is not None and by["a.txt"].modified.year == 2020
    assert listing.total_uncompressed == 5006
    # Only the signature header and the (encoded) header are read, never the file data.
    assert src.reads <= 4


def test_seven_zip_several_folders() -> None:
    listing = listing_of(FIXTURES, "sample-multifolder.7z")
    assert [(e.name, e.size) for e in listing.entries] == [
        ("one.txt", 400),
        ("sub/two.bin", 10240),
    ]


def test_seven_zip_encrypted_files_and_names() -> None:
    files = listing_of(FIXTURES, "sample-encrypted-files.7z")
    assert not files.names_hidden and files.any_encrypted
    by = {e.name: e for e in files.entries}
    assert by["one.txt"].encrypted and not by["sub/"].encrypted
    rows = {r.label: r.value for r in archive_rows(files)}
    assert rows["Encrypted"] == "Yes"

    hidden = listing_of(FIXTURES, "sample-encrypted-names.7z")
    assert hidden.names_hidden and hidden.entries == []
    assert {r.label: r.value for r in archive_rows(hidden)} == {
        "Format": "7z",
        "Encrypted": "Yes, including file names",
    }


def test_seven_zip_truncated_download(tmp_path: Path) -> None:
    data = (FIXTURES / "sample-store.7z").read_bytes()
    (tmp_path / "cut.7z").write_bytes(data[: len(data) // 2])
    with pytest.raises(ArchiveError):
        listing_of(tmp_path, "cut.7z")


# RAR ------------------------------------------------------------------------------------------


def test_rar3_and_rar5() -> None:
    rar3 = listing_of(FIXTURES, "rar3-subdirs.rar")
    assert rar3.format == "RAR 3"
    assert "sub/with space/long fn.txt" in names(rar3)
    assert any(e.is_dir for e in rar3.entries)

    rar5 = listing_of(FIXTURES, "rar5-crc.rar")
    assert rar5.format == "RAR 5"
    assert [(e.name, e.size) for e in rar5.entries] == [("stest1.txt", 2048), ("stest2.txt", 2048)]
    assert rar5.comment == "RAR5 archive - crc"


def test_rar_encrypted() -> None:
    files = listing_of(FIXTURES, "rar5-psw.rar")
    assert all(e.encrypted for e in files.entries) and len(files.entries) == 2
    hidden = listing_of(FIXTURES, "rar5-hpsw.rar")
    assert hidden.names_hidden and hidden.entries == []


# Tar and compressed files ---------------------------------------------------------------------


def make_tree(root: Path) -> Path:
    (root / "src" / "docs").mkdir(parents=True)
    (root / "src" / "a.txt").write_text("alpha")
    (root / "src" / "docs" / "b.txt").write_text("bravo!")
    return root / "src"


@pytest.mark.parametrize(
    ("name", "mode", "label"),
    [
        ("t.tar", "w", "Tar"),
        ("t.tar.gz", "w:gz", "Tar (gzip)"),
        ("t.tgz", "w:gz", "Tar (gzip)"),
        ("t.tar.bz2", "w:bz2", "Tar (bzip2)"),
        ("t.tar.xz", "w:xz", "Tar (XZ)"),
        ("t.tar.zst", "w:zst", "Tar (Zstandard)"),
    ],
)
def test_tar_variants(tmp_path: Path, name: str, mode: str, label: str) -> None:
    src = make_tree(tmp_path)
    with tarfile.open(tmp_path / name, mode) as tf:  # type: ignore[call-overload]
        tf.add(src, arcname="backup")
    listing = listing_of(tmp_path, name)
    assert listing.format == label
    assert set(names(listing)) == {"backup/", "backup/a.txt", "backup/docs/", "backup/docs/b.txt"}
    assert listing.total_uncompressed == 11
    assert not listing.stopped


def test_uncompressed_tar_skips_file_data(tmp_path: Path) -> None:
    big = tmp_path / "big.bin"
    big.write_bytes(b"\0" * (8 * 1024 * 1024))
    with tarfile.open(tmp_path / "t.tar", "w") as tf:
        tf.add(big, arcname="big.bin")
        tf.add(big, arcname="again.bin")
    src = RemoteLikeSource(tmp_path)
    listing = archive_listing(src, "t.tar", src.stat("t.tar"))
    assert names(listing) == ["big.bin", "again.bin"]
    assert src.bytes_read < 512 * 1024, src.bytes_read


def test_compressed_tar_stops_at_the_budget(tmp_path: Path) -> None:
    import os

    with tarfile.open(tmp_path / "t.tar.gz", "w:gz") as tf:
        for i in range(40):
            data = os.urandom(64 * 1024)  # incompressible, so the archive is ~2.5 MB
            info = tarfile.TarInfo(f"f{i}.bin")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    listing = listing_of(tmp_path, "t.tar.gz", budget=512 * 1024)
    assert listing.stopped == "read limit reached"
    assert 0 < listing.count < 40
    rows = {r.label: r.value for r in archive_rows(listing)}
    assert rows["Entries"].startswith("at least ")


def test_single_compressed_files(tmp_path: Path) -> None:
    text = b"log line\n" * 1000
    with gzip.GzipFile(tmp_path / "server.log.gz", "wb", mtime=1_600_000_000) as gz:
        gz.write(text)
    gz_listing = listing_of(tmp_path, "server.log.gz")
    assert gz_listing.format == "Gzip-compressed file"
    [entry] = gz_listing.entries
    assert (entry.name, entry.size) == ("server.log", len(text))
    assert entry.modified is not None

    (tmp_path / "notes.txt.zst").write_bytes(zstd.compress(text))
    zst = listing_of(tmp_path, "notes.txt.zst")
    assert zst.format == "Zstandard-compressed file"
    assert [(e.name, e.size) for e in zst.entries] == [("notes.txt", None)]
    assert {r.label: r.value for r in archive_rows(zst)}["Uncompressed size"] == "—"


def test_not_an_archive(tmp_path: Path) -> None:
    (tmp_path / "x.7z").write_bytes(b"definitely not")
    with pytest.raises(ArchiveError):
        listing_of(tmp_path, "x.7z")


# Detection ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "label"),
    [
        ("sample-lzma2.7z", "7-Zip archive"),
        ("rar5-crc.rar", "RAR archive"),
        ("rar3-subdirs.rar", "RAR archive"),
    ],
)
def test_detects_archives(fixture: str, label: str) -> None:
    head = (FIXTURES / fixture).read_bytes()[:4096]
    detection = detect_bytes(fixture, head)
    assert (detection.kind, detection.label) == (Kind.ARCHIVE, label)
    # Wrong or missing extensions still work: magic bytes decide.
    assert detect_bytes("mystery.bin", head).kind == Kind.ARCHIVE
    assert detect_bytes("noext", head).kind == Kind.ARCHIVE


def test_detects_tar_and_compressed(tmp_path: Path) -> None:
    src = make_tree(tmp_path)
    with tarfile.open(tmp_path / "t.tar", "w") as tf:
        tf.add(src, arcname="x")
    head = (tmp_path / "t.tar").read_bytes()[:4096]
    assert detect_bytes("t.tar", head).kind == Kind.ARCHIVE
    assert archive_format(head) == "tar"
    gz = gzip.compress(b"hello")
    assert detect_bytes("a.txt.gz", gz).kind == Kind.ARCHIVE
    assert not detect_bytes("a.txt.gz", gz).extension_mismatch


def test_wrapped_formats_trust_the_extension() -> None:
    # .svgz is a gzip-compressed SVG: an image, not an archive, and no mismatch warning.
    detection = detect_bytes("drawing.svgz", gzip.compress(b"<svg/>"))
    assert detection.kind == Kind.IMAGE and not detection.extension_mismatch


def test_comic_rar_that_is_really_a_zip(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("page1.jpg", b"x")
    detection = detect_bytes("issue1.cbr", buf.getvalue())
    assert not detection.extension_mismatch
    shutil.copy(FIXTURES / "rar5-crc.rar", tmp_path / "issue2.cbr")
    assert listing_of(tmp_path, "issue2.cbr").format == "RAR 5"
