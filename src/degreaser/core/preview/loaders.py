"""Preview loaders. Blocking; called from worker threads. Everything reads through FileSource."""

import codecs
import io
import struct
import threading

from degreaser.core.sources.base import FileSource

READ_CHUNK = 256 * 1024


class Cancelled(Exception):
    pass


class CancelToken:
    """Set by the UI when the user moves on; loaders check it between reads."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled()


# Text ---------------------------------------------------------------------------------------


class TextReader:
    """Decodes a text file incrementally from the start, so Expand can append more."""

    def __init__(self, source: FileSource, rel: str, size: int, encoding: str, bom: bool) -> None:
        self.source = source
        self.rel = rel
        self.size = size
        self.encoding = encoding
        self.offset = 0
        self._strip_bom = bom and encoding != "utf-8-sig"
        self._decoder = codecs.getincrementaldecoder(encoding)(errors="replace")

    @property
    def eof(self) -> bool:
        return self.offset >= self.size

    def consume(self, data: bytes) -> str:
        """Decode bytes already read from the start of the file (detection's head)."""
        if self.offset != 0:
            raise ValueError("consume() only applies to the start of the file")
        self.offset = len(data)
        return self._finish(self._decoder.decode(data, final=self.eof))

    def _finish(self, text: str) -> str:
        if self._strip_bom and text.startswith("\ufeff"):
            text = text[1:]
        if text:
            self._strip_bom = False
        return text

    def read(self, length: int, cancel: CancelToken | None = None) -> str:
        """Read and decode up to `length` more bytes."""
        parts: list[str] = []
        end = min(self.size, self.offset + length)
        while self.offset < end:
            if cancel:
                cancel.check()
            n = min(READ_CHUNK, end - self.offset)
            data = self.source.read_range(self.rel, self.offset, n)
            if not data:
                self.size = self.offset  # file shrank
                break
            self.offset += len(data)
            parts.append(self._decoder.decode(data, final=self.eof))
        return self._finish("".join(parts))


def line_endings(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    present = [name for name, n in (("CRLF", crlf), ("LF", lf), ("CR", cr)) if n]
    if not present:
        return "None"
    return present[0] if len(present) == 1 else "Mixed (" + ", ".join(present) + ")"


# Hex ----------------------------------------------------------------------------------------


def read_sample(source: FileSource, rel: str, offset: int, length: int) -> bytes:
    return source.read_range(rel, offset, length)


def format_hex(data: bytes, base_offset: int = 0) -> str:
    lines = []
    for i in range(0, len(data), 16):
        row = data[i : i + 16]
        hexes = " ".join(f"{b:02x}" for b in row)
        hexes = hexes[:23] + " " + hexes[23:] if len(row) > 8 else hexes
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append(f"{base_offset + i:08x}  {hexes:<48}  |{text}|")
    return "\n".join(lines)


# JPEG EXIF thumbnail --------------------------------------------------------------------------


def exif_thumbnail(prefix: bytes) -> bytes | None:
    """Extract the IFD1 JPEG thumbnail from the APP1/Exif segment using only the file prefix."""
    try:
        return _exif_thumbnail(prefix)
    except struct.error, IndexError:
        return None


def _exif_thumbnail(prefix: bytes) -> bytes | None:
    if prefix[:2] != b"\xff\xd8":
        return None
    pos = 2
    while pos + 4 <= len(prefix) and prefix[pos] == 0xFF:
        marker = prefix[pos + 1]
        seglen = struct.unpack(">H", prefix[pos + 2 : pos + 4])[0]
        seg = prefix[pos + 4 : pos + 2 + seglen]
        if marker == 0xE1 and seg[:6] == b"Exif\x00\x00":
            tiff = seg[6:]
            end = "<" if tiff[:2] == b"II" else ">"
            ifd0 = struct.unpack(end + "I", tiff[4:8])[0]
            n0 = struct.unpack(end + "H", tiff[ifd0 : ifd0 + 2])[0]
            next_at = ifd0 + 2 + n0 * 12
            ifd1 = struct.unpack(end + "I", tiff[next_at : next_at + 4])[0]
            if not ifd1:
                return None
            n1 = struct.unpack(end + "H", tiff[ifd1 : ifd1 + 2])[0]
            off = length = None
            for i in range(n1):
                e = tiff[ifd1 + 2 + i * 12 : ifd1 + 14 + i * 12]
                tag = struct.unpack(end + "H", e[:2])[0]
                val = struct.unpack(end + "I", e[8:12])[0]
                if tag == 0x0201:
                    off = val
                elif tag == 0x0202:
                    length = val
            if off is not None and length and off + length <= len(tiff):
                thumb = tiff[off : off + length]
                return thumb if thumb[:2] == b"\xff\xd8" else None
            return None
        if marker == 0xDA:  # start of scan: no more metadata segments
            return None
        pos += 2 + seglen
    return None


# Range reads as a file ---------------------------------------------------------------------------


class RangeReader(io.RawIOBase):
    """Seekable read-only file object over FileSource.read_range. Counts what it reads."""

    def __init__(
        self, source: FileSource, rel: str, size: int, cancel: CancelToken | None = None
    ) -> None:
        self.source = source
        self.rel = rel
        self.size = size
        self.pos = 0
        self.bytes_read = 0
        self.calls = 0
        self.cancel = cancel

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}[whence]
        self.pos = max(0, base + offset)
        return self.pos

    def readinto(self, buffer: bytearray | memoryview) -> int:  # type: ignore[override]
        if self.cancel:
            self.cancel.check()
        n = min(len(buffer), self.size - self.pos)
        if n <= 0:
            return 0
        data = self.source.read_range(self.rel, self.pos, n)
        self.calls += 1
        self.bytes_read += len(data)
        buffer[: len(data)] = data
        self.pos += len(data)
        return len(data)
