"""How much of a file a preview needs, and whether to ask first (docs/previews.md §1.2, §2)."""

from dataclasses import dataclass
from enum import StrEnum

from degreaser.core.preview.detect import Detection, Kind
from degreaser.core.settings import Downloads
from degreaser.core.sources.base import Entry

TEXT_INITIAL = 64 * 1024
TEXT_STEP = 1024 * 1024
HEX_INITIAL = 4 * 1024
HEX_STEP = 64 * 1024
HEX_CEILING = 1024 * 1024
EXIF_PREFIX = 64 * 1024


class Strategy(StrEnum):
    TEXT_SAMPLE = "text_sample"  # range reads from the start; Expand reads more
    HEX_SAMPLE = "hex_sample"
    IMAGE = "image"  # (JPEG: EXIF thumbnail from the prefix first) then the full file
    PDF = "pdf"  # needs the full file
    MEDIA = "media"  # streamed: local path now, loopback media server for SMB (M2)
    DOCUMENT = "document"  # Office/OpenDocument/iWork: range reads of the needed zip parts
    ARCHIVE_LISTING = "archive_listing"  # range reads of the headers (core/preview/archives.py)
    NONE = "none"  # metadata + Open only


class Confirm(StrEnum):
    """Why a preview asks before loading."""

    AUTO_LOAD_LIMIT = "auto_load_limit"  # needs the whole file and it's above auto_load_bytes
    LARGE_DOWNLOAD = "large_download"  # remote, needs the whole file, above large_warning_bytes


@dataclass(frozen=True, slots=True)
class Plan:
    strategy: Strategy
    initial_bytes: int = 0  # for sample strategies
    step_bytes: int = 0  # how much each Expand adds
    ceiling_bytes: int = 0  # Expand stops here
    needs_full_file: bool = False
    exif_thumbnail_first: bool = False
    confirm: Confirm | None = None  # ask before fetching/decoding the full file

    @property
    def expandable(self) -> bool:
        return self.step_bytes > 0


def plan_preview(detection: Detection, entry: Entry, limits: Downloads, *, local: bool) -> Plan:
    """Decide the preview strategy. `local` = the source exposes local paths (no download)."""
    size = entry.size or 0
    match detection.kind:
        case Kind.TEXT | Kind.CODE:
            return Plan(
                Strategy.TEXT_SAMPLE,
                initial_bytes=TEXT_INITIAL,
                step_bytes=TEXT_STEP,
                ceiling_bytes=limits.text_ceiling_bytes,
            )
        case Kind.IMAGE:
            return Plan(
                Strategy.IMAGE,
                needs_full_file=True,
                exif_thumbnail_first=detection.mime == "image/jpeg",
                confirm=_full_file_confirm(size, limits, local=local),
            )
        case Kind.PDF:
            return Plan(
                Strategy.PDF,
                needs_full_file=True,
                confirm=_full_file_confirm(size, limits, local=local),
            )
        case Kind.VIDEO | Kind.AUDIO:
            # Streaming works for every tested container (spikes/codecs, spikes/smb_streaming),
            # so media never needs the whole file up front.
            return Plan(Strategy.MEDIA)
        case Kind.DOCUMENT:
            # Only the central directory and a few XML parts are read, with hard caps
            # (core/preview/office.py), so no size prompt is needed.
            return Plan(Strategy.DOCUMENT)
        case Kind.ZIP | Kind.ARCHIVE:
            return Plan(Strategy.ARCHIVE_LISTING)
        case Kind.BINARY:
            if size == 0:
                return Plan(Strategy.NONE)
            return Plan(
                Strategy.HEX_SAMPLE,
                initial_bytes=HEX_INITIAL,
                step_bytes=HEX_STEP,
                ceiling_bytes=HEX_CEILING,
            )


def _full_file_confirm(size: int, limits: Downloads, *, local: bool) -> Confirm | None:
    if not local and size > limits.large_warning_bytes:
        return Confirm.LARGE_DOWNLOAD
    if size > limits.auto_load_bytes:
        return Confirm.AUTO_LOAD_LIMIT
    return None
