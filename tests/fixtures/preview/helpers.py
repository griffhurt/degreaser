"""Fixture builders for preview tests. Everything is generated at test time."""

import io
import shutil
import struct
import subprocess
from pathlib import Path

from PIL import Image

from degreaser.core.sources.base import Capability
from degreaser.core.sources.local import LocalSource


class RemoteLikeSource(LocalSource):
    """A LocalSource that pretends to be remote (no local paths) and counts bytes read."""

    capabilities = frozenset({Capability.RANGE_READ})

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.bytes_read = 0
        self.reads = 0

    def local_path(self, rel: str) -> Path | None:
        return None

    def read_range(self, rel: str, offset: int, length: int) -> bytes:
        data = super().read_range(rel, offset, length)
        self.bytes_read += len(data)
        self.reads += 1
        return data


def jpeg_bytes(size: tuple[int, int], color: str = "red", **save: object) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", **save)
    return buf.getvalue()


def png_bytes(size: tuple[int, int] = (32, 24)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "blue").save(buf, "PNG")
    return buf.getvalue()


def jpeg_with_exif_thumbnail(main: tuple[int, int], thumb: tuple[int, int]) -> bytes:
    """A JPEG whose APP1/Exif segment carries an IFD1 thumbnail (Pillow can't write those)."""
    thumb_jpeg = jpeg_bytes(thumb, "green")
    ifd1_at = 8 + 2 + 4  # after the header and an empty IFD0
    thumb_at = ifd1_at + 2 + 2 * 12 + 4
    tiff = (
        b"II*\x00"
        + struct.pack("<I", 8)
        + struct.pack("<H", 0)  # IFD0: no entries
        + struct.pack("<I", ifd1_at)  # next IFD = IFD1
        + struct.pack("<H", 2)
        + struct.pack("<HHII", 0x0201, 4, 1, thumb_at)  # JPEGInterchangeFormat
        + struct.pack("<HHII", 0x0202, 4, 1, len(thumb_jpeg))  # ...Length
        + struct.pack("<I", 0)
        + thumb_jpeg
    )
    app1 = b"\xff\xe1" + struct.pack(">H", 2 + 6 + len(tiff)) + b"Exif\x00\x00" + tiff
    main_jpeg = jpeg_bytes(main, "red")
    return main_jpeg[:2] + app1 + main_jpeg[2:]


def jpeg_with_camera_exif() -> bytes:
    image = Image.new("RGB", (40, 30), "white")
    exif = image.getexif()
    exif[0x010F] = "Canon"
    exif[0x0110] = "Canon PowerShot A80"
    exif[0x0112] = 6
    sub = exif.get_ifd(0x8769)
    sub[0x9003] = "2004:07:14 15:21:09"
    sub[0x829A] = 0.004
    sub[0x829D] = 2.8
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (40.0, 26.0, 46.0)
    gps[3] = "W"
    gps[4] = (79.0, 58.0, 56.0)
    buf = io.BytesIO()
    image.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def make_video(path: Path, *codec_args: str, seconds: float = 1.0) -> Path:
    """Tiny test video with tone audio via the ffmpeg CLI (callers skip if ffmpeg is missing)."""
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=25:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            *codec_args, "-shortest", str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


def make_wav(path: Path, seconds: float = 0.5, rate: int = 8000) -> Path:
    import math
    import wave

    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(int(seconds * rate))
        )
        w.writeframes(frames)
    return path
