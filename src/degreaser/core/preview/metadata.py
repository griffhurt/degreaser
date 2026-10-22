"""Metadata rows for the table beside the preview (docs/previews.md §6).

Qt-only sources (QMediaMetaData, QPdfDocument) are added by ui/preview; this module covers
everything readable with plain Python from the file head.
"""

import io
import os
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image, UnidentifiedImageError

from degreaser.core.preview.containers import ContainerInfo, sniff_container_file
from degreaser.core.preview.detect import Detection, Kind
from degreaser.core.preview.loaders import ZipListing, line_endings
from degreaser.core.sources.base import Entry, FileSource, basename


@dataclass(frozen=True, slots=True)
class MetaRow:
    group: str
    label: str
    value: str
    warning: bool = False  # shown highlighted (e.g. extension mismatch)


def dimensions(width: int, height: int) -> str:
    return f"{width} \u00d7 {height}"


# Display order of groups; unknown groups go last in insertion order.
GROUP_ORDER = ["File", "Triage", "Text", "Image", "Camera", "Media", "Document", "Archive", "Link"]


def format_size(n: int | None) -> str:
    if n is None:
        return "—"
    if n < 1024:
        return "1 byte" if n == 1 else f"{n:,} bytes"
    size = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit} ({n:,} bytes)"
    raise AssertionError("unreachable")


def format_time(ts: float | None) -> str:
    if ts is None:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except OverflowError, OSError, ValueError:
        return "—"


def format_duration(ms: int | float) -> str:
    seconds = int(ms // 1000)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def common_rows(rel: str, entry: Entry, detection: Detection | None) -> list[MetaRow]:
    rows = [
        MetaRow("File", "Name", basename(rel) or "/"),
        MetaRow("File", "Path", rel or "/"),
    ]
    if entry.kind != "dir":
        rows.append(MetaRow("File", "Size", format_size(entry.size)))
    rows.append(MetaRow("File", "Modified", format_time(entry.mtime)))
    if entry.ctime is not None:
        rows.append(MetaRow("File", "Created", format_time(entry.ctime)))
    if detection is not None:
        rows.append(MetaRow("File", "Type", detection.label))
        if detection.mime:
            rows.append(MetaRow("File", "MIME type", detection.mime))
        if detection.extension_mismatch:
            claimed = detection.extension_label or "something else"
            rows.append(
                MetaRow("File", "Extension", f"says {claimed}, contents are not", warning=True)
            )
    return rows


def text_rows(detection: Detection, sample: str, sample_bytes: int, total: int) -> list[MetaRow]:
    partial = sample_bytes < total
    lines = sample.count("\n") + (1 if sample and not sample.endswith("\n") else 0)
    rows = [
        MetaRow(
            "Text", "Encoding", (detection.encoding or "?") + (" (BOM)" if detection.bom else "")
        ),
        MetaRow("Text", "Line endings", line_endings(sample)),
        MetaRow(
            "Text",
            "Lines",
            f"{lines:,} in first {format_size(sample_bytes)}" if partial else f"{lines:,}",
        ),
    ]
    return rows


_ORIENTATION = {
    1: "Normal",
    2: "Mirrored",
    3: "Rotated 180°",
    4: "Mirrored vertically",
    5: "Mirrored, rotated 90° CCW",
    6: "Rotated 90° CW",
    7: "Mirrored, rotated 90° CW",
    8: "Rotated 90° CCW",
}


def image_rows(data: bytes | Path) -> list[MetaRow]:
    """Image dimensions and EXIF. `data` may be just the file head; Pillow reads lazily."""
    rows: list[MetaRow] = []
    try:
        with Image.open(io.BytesIO(data) if isinstance(data, bytes) else data) as im:
            rows.append(MetaRow("Image", "Dimensions", dimensions(im.width, im.height)))
            rows.append(MetaRow("Image", "Colour mode", im.mode))
            if im.format:
                rows.append(MetaRow("Image", "Format", im.format))
            frames = getattr(im, "n_frames", 1)
            if frames > 1:
                rows.append(MetaRow("Image", "Frames", str(frames)))
            rows += _exif_rows(im.getexif())
    except UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError:
        pass
    return rows


def _exif_rows(exif: Image.Exif) -> list[MetaRow]:
    if not exif:
        return []
    rows: list[MetaRow] = []
    base = ExifTags.Base
    sub = exif.get_ifd(ExifTags.IFD.Exif)

    camera = " ".join(
        str(v).strip("\x00 ") for v in (exif.get(base.Make), exif.get(base.Model)) if v
    )
    if camera:
        rows.append(MetaRow("Camera", "Camera", camera))
    if lens := sub.get(base.LensModel):
        rows.append(MetaRow("Camera", "Lens", str(lens).strip("\x00 ")))
    taken = sub.get(base.DateTimeOriginal) or exif.get(base.DateTime)
    if taken:
        rows.append(MetaRow("Camera", "Date taken", str(taken).strip("\x00 ")))
    if (orientation := exif.get(base.Orientation)) in _ORIENTATION:
        rows.append(MetaRow("Camera", "Orientation", _ORIENTATION[orientation]))
    if (exposure := sub.get(base.ExposureTime)) is not None:
        rows.append(MetaRow("Camera", "Exposure", _exposure(exposure)))
    if (fnumber := sub.get(base.FNumber)) is not None:
        rows.append(MetaRow("Camera", "Aperture", f"f/{float(fnumber):.1f}"))
    if (iso := sub.get(base.ISOSpeedRatings)) is not None:
        rows.append(MetaRow("Camera", "ISO", str(iso)))
    if (focal := sub.get(base.FocalLength)) is not None:
        rows.append(MetaRow("Camera", "Focal length", f"{float(focal):g} mm"))
    if gps := _gps(exif.get_ifd(ExifTags.IFD.GPSInfo)):
        rows.append(MetaRow("Camera", "GPS", gps))
    return rows


def _exposure(value: Any) -> str:
    seconds = float(value)
    if 0 < seconds < 1:
        return f"1/{round(1 / seconds)} s"
    return f"{seconds:g} s"


def _gps(gps: dict[int, Any]) -> str | None:
    try:
        lat = _dms(gps[2]) * (-1 if gps.get(1) in ("S", b"S") else 1)
        lon = _dms(gps[4]) * (-1 if gps.get(3) in ("W", b"W") else 1)
    except KeyError, TypeError, ValueError, ZeroDivisionError:
        return None
    return f"{lat:.6f}, {lon:.6f}"


def _dms(value: Any) -> float:
    d, m, s = (float(Fraction(str(v))) if not isinstance(v, float) else v for v in value)
    return d + m / 60 + s / 3600


def zip_rows(listing: ZipListing) -> list[MetaRow]:
    rows = [
        MetaRow("Archive", "Entries", f"{listing.count:,}"),
        MetaRow("Archive", "Uncompressed size", format_size(listing.total_uncompressed)),
        MetaRow("Archive", "Encrypted", "Yes" if listing.any_encrypted else "No"),
    ]
    if listing.comment.strip():
        rows.append(MetaRow("Archive", "Comment", listing.comment.strip()[:200]))
    return rows


def container_rows(info: ContainerInfo) -> list[MetaRow]:
    rows = []
    if info.container:
        rows.append(MetaRow("Media", "Container", info.container))
    if info.video:
        rows.append(MetaRow("Media", "Video codec", ", ".join(info.video)))
    if info.audio:
        rows.append(MetaRow("Media", "Audio codec", ", ".join(info.audio)))
    return rows


def symlink_rows(source: FileSource, rel: str) -> list[MetaRow]:
    local = source.local_path(rel)
    if local is None:
        return []
    try:
        return [MetaRow("Link", "Target", os.readlink(local))]
    except OSError:
        return []


@dataclass(slots=True)
class BasicMetadata:
    rows: list[MetaRow]
    container: ContainerInfo | None = None  # media only; the UI merges it with Qt's view


def collect(
    source: FileSource, rel: str, entry: Entry, detection: Detection, head: bytes
) -> BasicMetadata:
    """Everything readable without Qt. Blocking; call from a worker thread."""
    rows = common_rows(rel, entry, detection)
    container: ContainerInfo | None = None
    match detection.kind:
        case Kind.TEXT | Kind.CODE:
            text = head.decode(detection.encoding or "utf-8", errors="replace")
            if detection.bom:
                text = text.lstrip("\ufeff")
            rows += text_rows(detection, text, len(head), entry.size or 0)
        case Kind.IMAGE:
            local = source.local_path(rel)
            rows += image_rows(local if local is not None else head)
        case Kind.VIDEO | Kind.AUDIO:
            container = sniff_container_file(source, rel, entry.size or 0, head)
            rows += container_rows(container)
        case _:
            pass
    if entry.kind == "symlink":
        rows += symlink_rows(source, rel)
    return BasicMetadata(rows, container)
