import codecs
import io
import os
import zipfile

import pytest
from PIL import Image

from degreaser.core.preview.detect import Kind, detect, detect_bytes, sniff_text
from degreaser.core.sources.local import LocalSource
from tests.fixtures.preview.helpers import jpeg_bytes, png_bytes


def zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.txt", "hello")
    return buf.getvalue()


@pytest.mark.parametrize(
    ("name", "data", "kind", "encoding"),
    [
        ("notes.txt", b"hello world\n", Kind.TEXT, "ascii"),
        ("notes.txt", "naïve café\n".encode(), Kind.TEXT, "utf-8"),
        ("script.py", b"print('hi')\n", Kind.CODE, "ascii"),
        ("README", b"Read me\n", Kind.TEXT, "ascii"),
        ("data.csv", b"a,b\n1,2\n", Kind.TEXT, "ascii"),
        ("app.ts", b"const x: number = 1;\n", Kind.CODE, "ascii"),
    ],
)
def test_text_files(name: str, data: bytes, kind: Kind, encoding: str) -> None:
    d = detect_bytes(name, data)
    assert (d.kind, d.encoding, d.extension_mismatch) == (kind, encoding, False)


def test_legacy_8bit_encoding_detected() -> None:
    text = "Résumé de l'été, déjà vu à Noël. Ça coûte très cher, n'est-ce pas ? " * 20
    d = detect_bytes("lettre.txt", text.encode("cp1252"))
    assert d.kind == Kind.TEXT
    assert d.encoding is not None
    assert codecs.lookup(d.encoding).name != "utf-8"
    assert text[:20] in text.encode("cp1252").decode(d.encoding)


def test_utf16_with_bom_is_text_without_bom_is_binary() -> None:
    body = "Hello, world\r\n" * 10
    d = detect_bytes("log.txt", codecs.BOM_UTF16_LE + body.encode("utf-16-le"))
    assert (d.kind, d.bom, d.encoding) == (Kind.TEXT, True, "utf-16-le")
    assert detect_bytes("log.txt", body.encode("utf-16-le")).kind == Kind.BINARY


def test_binary_and_empty() -> None:
    assert detect_bytes("blob.bin", os.urandom(4096)).kind == Kind.BINARY
    empty = detect_bytes("empty.txt", b"")
    assert empty.kind == Kind.TEXT


def test_magic_beats_extension() -> None:
    d = detect_bytes("photo.jpg", png_bytes())
    assert d.kind == Kind.IMAGE
    assert d.label == "PNG image"
    assert d.extension_mismatch and d.extension_label == "JPEG image"

    d = detect_bytes("photo.jpg", b"definitely just text\n")
    assert d.kind == Kind.TEXT and d.extension_mismatch

    d = detect_bytes("noext", jpeg_bytes((8, 8)))
    assert (d.kind, d.label, d.extension_mismatch) == (Kind.IMAGE, "JPEG image", False)


def test_zip_family_labels() -> None:
    data = zip_bytes()
    assert detect_bytes("archive.zip", data).label == "Zip archive"
    docx = detect_bytes("report.docx", data)
    assert (docx.kind, docx.label, docx.extension_mismatch) == (Kind.ZIP, "Word document", False)
    assert detect_bytes("lib.jar", data).label == "Java archive"
    assert detect_bytes("mystery", data).label == "Zip archive"


def test_tga_is_not_mistaken_for_an_icon() -> None:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, "TGA")
    d = detect_bytes("sprite.tga", buf.getvalue())
    assert (d.label, d.extension_mismatch) == ("TGA image", False)


def test_mpeg_ts_by_sync_bytes() -> None:
    packets = (b"\x47" + bytes(187)) * 4
    d = detect_bytes("clip.ts", packets)
    assert (d.kind, d.extension_mismatch) == (Kind.VIDEO, False)
    m2ts = (bytes(4) + b"\x47" + bytes(187)) * 4
    assert detect_bytes("00001.m2ts", m2ts).kind == Kind.VIDEO


def test_media_extension_trusted_when_magic_is_silent() -> None:
    assert detect_bytes("song.wma", os.urandom(512)).kind == Kind.AUDIO


def test_sniff_text_tolerates_cut_multibyte_char() -> None:
    data = ("é" * 100).encode()[:-1]  # ends mid-character
    assert sniff_text(data) == ("utf-8", False)


def test_detect_reads_only_head(tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "big.txt").write_bytes(b"x" * 200_000)
    src = LocalSource(tmp_path)
    detection, head = detect(src, "big.txt", src.stat("big.txt"))
    assert detection.kind == Kind.TEXT
    assert len(head) == 64 * 1024
