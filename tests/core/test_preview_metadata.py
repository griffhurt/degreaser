import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from degreaser.core.preview.archives import archive_listing
from degreaser.core.preview.containers import sniff_container, sniff_container_file
from degreaser.core.preview.detect import Detection, Kind, detect
from degreaser.core.preview.metadata import (
    archive_rows,
    collect,
    common_rows,
    dimensions,
    format_duration,
    format_size,
    image_rows,
)
from degreaser.core.sources.base import Entry
from degreaser.core.sources.local import LocalSource
from tests.fixtures.preview.helpers import (
    RemoteLikeSource,
    ffmpeg_available,
    jpeg_with_camera_exif,
    make_video,
    make_wav,
)


def rows_by_label(rows: list) -> dict[str, str]:  # type: ignore[type-arg]
    return {r.label: r.value for r in rows}


def test_formatting() -> None:
    assert format_size(512) == "512 bytes"
    assert format_size(1536) == "1.5 KB (1,536 bytes)"
    assert format_size(None) == "—"
    assert format_duration(65_000) == "1:05"
    assert format_duration(3_725_000) == "1:02:05"


def test_common_rows_flag_extension_mismatch() -> None:
    entry = Entry("x.jpg", "file", 2048, 0.0, None)
    det = Detection(
        Kind.IMAGE, "PNG image", "image/png", extension_mismatch=True, extension_label="JPEG image"
    )
    rows = common_rows("photos/x.jpg", entry, det)
    by = rows_by_label(rows)
    assert by["Path"] == "photos/x.jpg"
    assert by["Type"] == "PNG image"
    warning = next(r for r in rows if r.label == "Extension")
    assert warning.warning and "JPEG image" in warning.value


def test_camera_exif_rows() -> None:
    by = rows_by_label(image_rows(jpeg_with_camera_exif()))
    assert by["Dimensions"] == dimensions(40, 30)
    assert by["Camera"] == "Canon Canon PowerShot A80"
    assert by["Date taken"] == "2004:07:14 15:21:09"
    assert by["Orientation"] == "Rotated 90° CW"
    assert by["Exposure"] == "1/250 s"
    assert by["Aperture"] == "f/2.8"
    lat, lon = (float(v) for v in by["GPS"].split(", "))
    assert lat == pytest.approx(40.4461, abs=1e-3)
    assert lon == pytest.approx(-79.9822, abs=1e-3)


def test_image_rows_ignore_garbage() -> None:
    assert image_rows(b"nope") == []


def test_collect_text_and_image(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"one\r\ntwo\r\n")
    (tmp_path / "p.jpg").write_bytes(jpeg_with_camera_exif())
    src = LocalSource(tmp_path)
    for rel, expected in (
        ("a.txt", {"Line endings": "CRLF", "Lines": "2"}),
        ("p.jpg", {"Camera": "Canon Canon PowerShot A80"}),
    ):
        entry = src.stat(rel)
        det, head = detect(src, rel, entry)
        by = rows_by_label(collect(src, rel, entry, det, head).rows)
        for key, value in expected.items():
            assert by[key] == value


def test_archive_rows(tmp_path: Path) -> None:
    with zipfile.ZipFile(tmp_path / "z.zip", "w") as z:
        z.writestr("a", "12345")
        z.comment = b"backup 2009"
    src = LocalSource(tmp_path)
    by = rows_by_label(archive_rows(archive_listing(src, "z.zip", src.stat("z.zip"))))
    assert by == {
        "Format": "Zip",
        "Entries": "1",
        "Uncompressed size": "5 bytes",
        "Encrypted": "No",
        "Comment": "backup 2009",
    }


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlink_target(tmp_path: Path) -> None:
    os.symlink("elsewhere/target.txt", tmp_path / "link")
    src = LocalSource(tmp_path)
    entry = src.stat("link")
    det, head = detect(src, "link", entry)
    by = rows_by_label(collect(src, "link", entry, det, head).rows)
    assert by["Target"] == "elsewhere/target.txt"


def test_wav_container(tmp_path: Path) -> None:
    head = make_wav(tmp_path / "t.wav").read_bytes()[:65536]
    info = sniff_container(head)
    assert (info.container, info.audio) == ("WAV", ["PCM"])


def test_garbage_is_not_a_container() -> None:
    info = sniff_container(b"RIFF\x00\x00\x00\x00AVI " + b"\xff" * 10)
    assert info.container == "AVI" and info.video == []
    assert sniff_container(os.urandom(64)).container is None


VIDEO_CASES = [
    ("divx3.avi", ["-c:v", "msmpeg4", "-c:a", "libmp3lame"], "AVI", "MS MPEG-4 v3", "MP3"),
    (
        "xvid.avi",
        ["-c:v", "mpeg4", "-vtag", "XVID", "-c:a", "pcm_s16le"],
        "AVI",
        "Xvid (MPEG-4 Part 2)",
        "PCM",
    ),
    (
        "wmv8.wmv",
        ["-c:v", "wmv2", "-c:a", "wmav2"],
        "ASF (Windows Media)",
        "Windows Media Video 8",
        "Windows Media Audio 2",
    ),
    (
        "spark.flv",
        ["-c:v", "flv1", "-c:a", "libmp3lame", "-ar", "44100"],
        "Flash Video",
        "Sorenson Spark (H.263)",
        "MP3",
    ),
    (
        "mpeg2.mpg",
        ["-c:v", "mpeg2video", "-c:a", "mp2", "-f", "vob"],
        "MPEG-2 program stream",
        "MPEG-2",
        "MPEG audio",
    ),
    ("clip.mkv", ["-c:v", "mpeg4", "-c:a", "flac"], "Matroska", "MPEG-4 Part 2", "FLAC"),
    ("rv.rm", ["-s", "176x144", "-c:v", "rv10", "-c:a", "ac3"], "RealMedia", "RealVideo 1", None),
]


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg CLI not installed")
@pytest.mark.parametrize(("name", "args", "container", "video", "audio"), VIDEO_CASES)
def test_video_containers(
    tmp_path: Path, name: str, args: list[str], container: str, video: str, audio: str | None
) -> None:
    try:
        make_video(tmp_path / name, *args)
    except subprocess.CalledProcessError:
        pytest.skip(f"this ffmpeg build can't encode {name}")
    info = sniff_container((tmp_path / name).read_bytes()[:65536])
    assert info.container == container
    assert info.video == [video]
    if audio:
        assert info.audio == [audio]


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg CLI not installed")
def test_mp4_with_moov_at_end_is_found_by_range_reads(tmp_path: Path) -> None:
    make_video(tmp_path / "late.mov", "-c:v", "mjpeg", "-c:a", "pcm_s16le", seconds=3)
    src = RemoteLikeSource(tmp_path)
    size = src.stat("late.mov").size or 0
    head = src.read_range("late.mov", 0, 4096)
    assert b"stsd" not in head
    info = sniff_container_file(src, "late.mov", size, head)
    assert info.video == ["Motion JPEG"] and info.audio == ["PCM"]
    assert src.bytes_read < size  # never downloaded the whole file
