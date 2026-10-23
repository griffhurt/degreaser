"""Archive listings from range reads (docs/previews.md §1.4).

- **Zip:** the central directory at the end of the file.
- **7z:** the header at the end, LZMA/LZMA2-compressed in most archives (parsed here; the only
  Python 7z library is LGPL and heavy).
- **RAR 3/5:** `rarfile` walks the per-file headers, skipping the data between them.
- **Tar:** `tarfile` walks the 512-byte headers, skipping file data.
- **Compressed tar and single compressed files** (gzip, bzip2, xz, zstd): these can only be
  read from the start, so listing stops once `budget` bytes have been read.

Nothing is extracted, and encrypted archives are listed as far as their headers allow.
"""

import bz2
import io
import lzma
import struct
import tarfile
import zipfile
import zlib
from collections.abc import Callable
from compression import zstd
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import rarfile

from degreaser.core.preview.loaders import CancelToken, RangeReader
from degreaser.core.sources.base import Entry, FileSource, basename

BUFFER = 64 * 1024  # turns small header reads into fewer, larger range reads
MAX_HEADER = 64 * 1024 * 1024  # a 7z header bigger than this isn't read


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    name: str
    size: int | None  # None when the format doesn't record it (bzip2, xz, …)
    modified: datetime | None
    is_dir: bool
    encrypted: bool = False
    compressed: int | None = None


@dataclass(slots=True)
class ArchiveListing:
    format: str = ""  # e.g. "Zip", "7z", "RAR 5", "Tar (gzip)"
    entries: list[ArchiveEntry] = field(default_factory=list)
    count: int = 0  # entries seen (all of them unless `stopped`)
    total_uncompressed: int = 0
    any_encrypted: bool = False
    names_hidden: bool = False  # encrypted headers: nothing to list without the password
    truncated: bool = False  # more entries than were kept in `entries`
    stopped: str = ""  # why listing stopped before the end, if it did
    comment: str = ""
    bytes_read: int = 0

    def add(self, entry: ArchiveEntry, max_entries: int) -> None:
        self.count += 1
        self.total_uncompressed += entry.size or 0
        self.any_encrypted |= entry.encrypted
        if len(self.entries) < max_entries:
            self.entries.append(entry)
        else:
            self.truncated = True


class ArchiveError(Exception):
    """Not a readable archive of the expected kind."""


class _OverBudget(Exception):
    pass


class _BudgetReader(RangeReader):
    """A RangeReader that stops (raises _OverBudget) once `budget` bytes have been read."""

    def __init__(
        self, source: FileSource, rel: str, size: int, cancel: CancelToken | None, budget: int
    ) -> None:
        super().__init__(source, rel, size, cancel)
        self.budget = budget

    def readinto(self, buffer: bytearray | memoryview) -> int:  # type: ignore[override]
        if self.bytes_read >= self.budget:
            raise _OverBudget
        return super().readinto(buffer)


# Detection ------------------------------------------------------------------------------------

SEVEN_ZIP_MAGIC = b"7z\xbc\xaf\x27\x1c"
RAR_MAGIC = b"Rar!\x1a\x07"
GZIP_MAGIC = b"\x1f\x8b"
BZIP2_MAGIC = b"BZh"
XZ_MAGIC = b"\xfd7zXZ\x00"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def is_tar_header(block: bytes) -> bool:
    """A POSIX/GNU tar header ("ustar" at 257), or an old-style one with a valid checksum."""
    if len(block) < 512:
        return False
    if block[257:262] == b"ustar":
        return True
    try:
        stored = int(block[148:156].split(b"\0")[0].strip() or b"-1", 8)
    except ValueError:
        return False
    computed = sum(block[:148]) + 8 * 32 + sum(block[156:512])
    return stored == computed and block[0] != 0


def archive_format(head: bytes) -> str | None:
    """'zip', '7z', 'rar', 'tar', 'gzip', 'bzip2', 'xz' or 'zstd' from the first bytes."""
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return "zip"
    if head.startswith(SEVEN_ZIP_MAGIC):
        return "7z"
    if head.startswith(RAR_MAGIC):
        return "rar"
    if head.startswith(GZIP_MAGIC):
        return "gzip"
    if head.startswith(XZ_MAGIC):
        return "xz"
    if head.startswith(ZSTD_MAGIC):
        return "zstd"
    if head.startswith(BZIP2_MAGIC) and head[3:4].isdigit():
        return "bzip2"
    if is_tar_header(head[:512]):
        return "tar"
    return None


# Entry point ----------------------------------------------------------------------------------


def archive_listing(
    source: FileSource,
    rel: str,
    entry: Entry,
    cancel: CancelToken | None = None,
    *,
    max_entries: int = 10_000,
    budget: int = 256 * 1024 * 1024,
) -> ArchiveListing:
    """List an archive. Raises ArchiveError if it isn't one this module can read."""
    size = entry.size or 0
    head = source.read_range(rel, 0, min(size, 1024))
    kind = archive_format(head)
    raw = _BudgetReader(source, rel, size, cancel, budget)
    lister: Callable[[], ArchiveListing]
    match kind:
        case "zip":
            lister = lambda: _zip(raw, max_entries)  # noqa: E731
        case "7z":
            lister = lambda: _seven_zip(raw, max_entries)  # noqa: E731
        case "rar":
            lister = lambda: _rar(raw, head, max_entries)  # noqa: E731
        case "tar":
            lister = lambda: _tar(raw, "r:", "Tar", max_entries)  # noqa: E731
        case "gzip" | "bzip2" | "xz" | "zstd":
            lister = lambda: _compressed(raw, kind, basename(rel), max_entries)  # noqa: E731
        case _:
            raise ArchiveError("not a recognised archive")
    listing = lister()
    listing.bytes_read = raw.bytes_read
    return listing


# Zip ------------------------------------------------------------------------------------------


def _zip(raw: RangeReader, max_entries: int) -> ArchiveListing:
    try:
        with io.BufferedReader(raw, BUFFER) as buffered, zipfile.ZipFile(buffered) as zf:
            listing = ArchiveListing("Zip", comment=zf.comment.decode("utf-8", "replace"))
            for info in zf.infolist():
                listing.add(
                    ArchiveEntry(
                        name=info.filename,
                        size=info.file_size,
                        compressed=info.compress_size,
                        modified=_dos_time(info.date_time),
                        is_dir=info.is_dir(),
                        encrypted=bool(info.flag_bits & 0x1),
                    ),
                    max_entries,
                )
            return listing
    except zipfile.BadZipFile as exc:
        raise ArchiveError(str(exc)) from exc


def _dos_time(dt: tuple[int, int, int, int, int, int]) -> datetime | None:
    try:
        return datetime(*dt)
    except ValueError:
        return None


# RAR ------------------------------------------------------------------------------------------


def _rar(raw: RangeReader, head: bytes, max_entries: int) -> ArchiveListing:
    try:
        with io.BufferedReader(raw, BUFFER) as buffered:
            rf = rarfile.RarFile(buffered)  # type: ignore[arg-type]
            infos = rf.infolist()
            version = 5 if head[6:7] == b"\x01" else 3
            listing = ArchiveListing(f"RAR {version}", comment=(rf.comment or "").strip())
            if rf.needs_password() and not infos:
                listing.names_hidden = listing.any_encrypted = True
            for info in infos:
                listing.add(
                    ArchiveEntry(
                        name=info.filename or "",
                        size=info.file_size,
                        compressed=info.compress_size,
                        modified=_dos_time(info.date_time),
                        is_dir=info.is_dir(),
                        encrypted=info.needs_password(),
                    ),
                    max_entries,
                )
            return listing
    except rarfile.Error as exc:
        raise ArchiveError(str(exc) or "unreadable RAR archive") from exc


# Tar and compressed files ---------------------------------------------------------------------

_COMPRESSED_NAME = {"gzip": "gzip", "bzip2": "bzip2", "xz": "XZ", "zstd": "Zstandard"}
_TAR_STREAM_MODE = {"gzip": "r|gz", "bzip2": "r|bz2", "xz": "r|xz", "zstd": "r|zst"}
_SUFFIXES = {"gzip": (".gz", ".tgz"), "bzip2": (".bz2", ".tbz2", ".tbz"), "xz": (".xz", ".txz")}


def _tar(raw: RangeReader, mode: str, label: str, max_entries: int) -> ArchiveListing:
    listing = ArchiveListing(label)
    try:
        with (
            io.BufferedReader(raw, BUFFER) as buffered,
            tarfile.open(fileobj=buffered, mode=mode) as tf,  # type: ignore[call-overload]
        ):
            for member in tf:
                listing.add(
                    ArchiveEntry(
                        name=member.name + ("/" if member.isdir() else ""),
                        size=None if member.isdir() else member.size,
                        modified=_unix_time(member.mtime),
                        is_dir=member.isdir(),
                    ),
                    max_entries,
                )
    except _OverBudget:
        listing.stopped = "read limit reached"
    except (tarfile.TarError, EOFError, OSError, lzma.LZMAError, zstd.ZstdError) as exc:
        if listing.count == 0:
            raise ArchiveError(str(exc) or "unreadable tar archive") from exc
        listing.stopped = "the rest couldn't be read"
    return listing


def _unix_time(ts: float | None) -> datetime | None:
    try:
        return None if ts is None else datetime.fromtimestamp(ts)
    except OverflowError, OSError, ValueError:
        return None


def _compressed(raw: RangeReader, kind: str, name: str, max_entries: int) -> ArchiveListing:
    """A compressed tar, listed by streaming; otherwise a single compressed file. (Checking
    the first decompressed block for a tar header doesn't work for bzip2, which outputs
    nothing until a whole block of up to 900 KB has been read, so the tar reader just tries.)"""
    label = f"Tar ({_COMPRESSED_NAME[kind]})"
    try:
        listing = _tar(raw, _TAR_STREAM_MODE[kind], label, max_entries)
        if listing.count or not listing.stopped:
            return listing
    except ArchiveError:
        pass
    name_ = _COMPRESSED_NAME[kind]
    listing = ArchiveListing(f"{name_[0].upper()}{name_[1:]}-compressed file")
    inner, size, modified = _single_file_info(raw, kind, name)
    listing.add(ArchiveEntry(inner, size, modified, is_dir=False), max_entries)
    return listing


def _single_file_info(
    raw: RangeReader, kind: str, name: str
) -> tuple[str, int | None, datetime | None]:
    """Inner name, size and time where the format records them (gzip does; the others don't
    store a name, and only gzip has the size at a fixed place)."""
    inner = name
    for suffix in _SUFFIXES.get(kind, (".zst", ".zstd")):
        if name.lower().endswith(suffix):
            inner = name[: -len(suffix)] + (".tar" if suffix.startswith(".t") else "")
            break
    size: int | None = None
    modified: datetime | None = None
    if kind == "gzip":
        header = raw.source.read_range(raw.rel, 0, min(raw.size, 1024))
        flags, mtime = header[3], struct.unpack_from("<I", header, 4)[0]
        modified = _unix_time(mtime) if mtime else None
        if flags & 0x08:  # FNAME
            pos = 10
            if flags & 0x04:  # FEXTRA
                pos += 2 + struct.unpack_from("<H", header, pos)[0]
            end = header.find(b"\0", pos)
            if end > pos:
                inner = header[pos:end].decode("latin-1")
        if raw.size < 2**32:  # ISIZE is the size mod 2^32
            size = struct.unpack("<I", raw.source.read_range(raw.rel, raw.size - 4, 4))[0]
    return inner, size, modified


# 7z -------------------------------------------------------------------------------------------
# Format: 7-Zip's DOC/7zFormat.txt. Only what a listing needs is decoded.

_K_END, _K_HEADER, _K_ARCHIVE_PROPERTIES, _K_ADDITIONAL_STREAMS = 0x00, 0x01, 0x02, 0x03
_K_MAIN_STREAMS, _K_FILES_INFO, _K_PACK_INFO, _K_UNPACK_INFO = 0x04, 0x05, 0x06, 0x07
_K_SUBSTREAMS, _K_SIZE, _K_CRC, _K_FOLDER = 0x08, 0x09, 0x0A, 0x0B
_K_CODERS_UNPACK_SIZE, _K_NUM_UNPACK_STREAM, _K_EMPTY_STREAM = 0x0C, 0x0D, 0x0E
_K_EMPTY_FILE, _K_NAME, _K_MTIME, _K_WIN_ATTRIBUTES = 0x0F, 0x11, 0x14, 0x15
_K_ENCODED_HEADER, _K_DUMMY = 0x17, 0x19

_COPY, _LZMA, _LZMA2 = b"\x00", b"\x03\x01\x01", b"\x21"
_DEFLATE, _BZIP2 = b"\x04\x01\x08", b"\x04\x02\x02"
_AES = b"\x06\xf1\x07\x01"
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)


class _Buf:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def byte(self) -> int:
        if self.pos >= len(self.data):
            raise ArchiveError("7z header ends early")
        b = self.data[self.pos]
        self.pos += 1
        return b

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise ArchiveError("7z header ends early")
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def number(self) -> int:
        first = self.byte()
        mask = 0x80
        value = 0
        for i in range(8):
            if first & mask == 0:
                return value | ((first & (mask - 1)) << (8 * i))
            value |= self.byte() << (8 * i)
            mask >>= 1
        return value

    def bits(self, n: int) -> list[bool]:
        out: list[bool] = []
        byte = mask = 0
        for _ in range(n):
            if mask == 0:
                byte, mask = self.byte(), 0x80
            out.append(bool(byte & mask))
            mask >>= 1
        return out

    def defined(self, n: int) -> list[bool]:
        """An "all defined" byte, then a bit vector if not."""
        return [True] * n if self.byte() else self.bits(n)


@dataclass
class _Coder:
    method: bytes
    props: bytes
    num_in: int
    num_out: int


@dataclass
class _Folder:
    coders: list[_Coder]
    bind_pairs: list[tuple[int, int]]  # (in index, out index)
    unpack_sizes: list[int] = field(default_factory=list)
    substreams: int = 1
    crc_known: bool = False  # the folder's CRC is in the unpack info

    @property
    def encrypted(self) -> bool:
        return any(c.method == _AES for c in self.coders)

    def unpack_size(self) -> int:
        """Size of the folder's final output: the out stream no bind pair consumes."""
        bound = {out for _in, out in self.bind_pairs}
        for i, size in enumerate(self.unpack_sizes):
            if i not in bound:
                return size
        return self.unpack_sizes[-1] if self.unpack_sizes else 0


@dataclass
class _Streams:
    pack_pos: int = 0
    pack_sizes: list[int] = field(default_factory=list)
    folders: list[_Folder] = field(default_factory=list)
    sizes: list[int] = field(default_factory=list)  # one per file stream, in order
    stream_folder: list[int] = field(default_factory=list)  # each stream's folder


def _seven_zip(raw: RangeReader, max_entries: int) -> ArchiveListing:
    start = raw.source.read_range(raw.rel, 0, 32)
    if len(start) < 32 or not start.startswith(SEVEN_ZIP_MAGIC):
        raise ArchiveError("not a 7z archive")
    offset, length = struct.unpack_from("<QQ", start, 12)
    if length == 0:
        return ArchiveListing("7z")  # an empty archive
    if length > MAX_HEADER or 32 + offset + length > raw.size:
        raise ArchiveError("7z header is missing or too large (incomplete download?)")
    raw.bytes_read += 32
    data = _read(raw, 32 + offset, length)
    listing = ArchiveListing("7z")
    for _ in range(4):  # an encoded header decodes to a header (in practice, once)
        buf = _Buf(data)
        kind = buf.byte()
        if kind == _K_HEADER:
            _parse_header(buf, listing, max_entries)
            return listing
        if kind != _K_ENCODED_HEADER:
            raise ArchiveError("unexpected 7z header type")
        streams = _parse_streams(buf)
        if any(f.encrypted for f in streams.folders):
            listing.names_hidden = listing.any_encrypted = True
            return listing
        data = _decode_folder(raw, streams)
    raise ArchiveError("7z header nests too deeply")


def _read(raw: RangeReader, offset: int, length: int) -> bytes:
    if raw.cancel:
        raw.cancel.check()
    data = raw.source.read_range(raw.rel, offset, length)
    raw.bytes_read += len(data)
    if len(data) != length:
        raise ArchiveError("7z archive is truncated")
    return data


def _parse_header(buf: _Buf, listing: ArchiveListing, max_entries: int) -> None:
    streams = _Streams()
    prop = buf.byte()
    if prop == _K_ARCHIVE_PROPERTIES:
        while buf.byte() != _K_END:
            buf.take(buf.number())
        prop = buf.byte()
    if prop == _K_ADDITIONAL_STREAMS:
        _parse_streams(buf)
        prop = buf.byte()
    if prop == _K_MAIN_STREAMS:
        streams = _parse_streams(buf)
        prop = buf.byte()
    if prop == _K_FILES_INFO:
        _parse_files(buf, streams, listing, max_entries)
        prop = buf.byte()
    if prop != _K_END:
        raise ArchiveError("unexpected data in 7z header")


def _parse_streams(buf: _Buf) -> _Streams:
    streams = _Streams()
    prop = buf.byte()
    if prop == _K_PACK_INFO:
        streams.pack_pos = buf.number()
        count = buf.number()
        while (prop := buf.byte()) != _K_END:
            if prop == _K_SIZE:
                streams.pack_sizes = [buf.number() for _ in range(count)]
            elif prop == _K_CRC:
                defined = buf.defined(count)
                buf.take(4 * sum(defined))
            else:
                raise ArchiveError("unexpected 7z pack info")
        prop = buf.byte()
    if prop == _K_UNPACK_INFO:
        if buf.byte() != _K_FOLDER:
            raise ArchiveError("unexpected 7z unpack info")
        count = buf.number()
        if buf.byte() != 0:
            raise ArchiveError("external 7z folders aren't supported")
        streams.folders = [_parse_folder(buf) for _ in range(count)]
        if buf.byte() != _K_CODERS_UNPACK_SIZE:
            raise ArchiveError("unexpected 7z unpack info")
        for folder in streams.folders:
            outs = sum(c.num_out for c in folder.coders)
            folder.unpack_sizes = [buf.number() for _ in range(outs)]
        while (prop := buf.byte()) != _K_END:
            if prop == _K_CRC:
                defined = buf.defined(count)
                buf.take(4 * sum(defined))
                for folder, known in zip(streams.folders, defined, strict=True):
                    folder.crc_known = known
            else:
                raise ArchiveError("unexpected 7z unpack info")
        prop = buf.byte()
    if prop == _K_SUBSTREAMS:
        prop = buf.byte()
        if prop == _K_NUM_UNPACK_STREAM:
            for folder in streams.folders:
                folder.substreams = buf.number()
            prop = buf.byte()
        for index, folder in enumerate(streams.folders):
            if folder.substreams == 0:
                continue
            given = []
            if prop == _K_SIZE:
                given = [buf.number() for _ in range(folder.substreams - 1)]
            streams.sizes += [*given, folder.unpack_size() - sum(given)]
            streams.stream_folder += [index] * folder.substreams
        if prop == _K_SIZE:
            prop = buf.byte()
        while prop != _K_END:
            if prop == _K_CRC:
                # Digests for every stream whose CRC the folder doesn't already give.
                unknown = sum(
                    f.substreams for f in streams.folders if not (f.substreams == 1 and f.crc_known)
                )
                defined = buf.defined(unknown)
                buf.take(4 * sum(defined))
            else:
                raise ArchiveError("unexpected 7z substreams info")
            prop = buf.byte()
        prop = buf.byte()
    elif streams.folders:
        streams.sizes = [f.unpack_size() for f in streams.folders]
        streams.stream_folder = list(range(len(streams.folders)))
    if prop != _K_END:
        raise ArchiveError("unexpected data in 7z streams info")
    return streams


def _parse_folder(buf: _Buf) -> _Folder:
    coders = []
    for _ in range(buf.number()):
        flags = buf.byte()
        if flags & 0x80:
            raise ArchiveError("7z alternative coder methods aren't supported")
        method = buf.take(flags & 0x0F)
        num_in, num_out = (buf.number(), buf.number()) if flags & 0x10 else (1, 1)
        props = buf.take(buf.number()) if flags & 0x20 else b""
        coders.append(_Coder(method, props, num_in, num_out))
    total_in = sum(c.num_in for c in coders)
    total_out = sum(c.num_out for c in coders)
    pairs = [(buf.number(), buf.number()) for _ in range(total_out - 1)]
    packed = total_in - len(pairs)
    if packed > 1:
        for _ in range(packed):
            buf.number()
    return _Folder(coders, pairs)


def _decode_folder(raw: RangeReader, streams: _Streams) -> bytes:
    """Decode the encoded header: one folder with one coder (LZMA, LZMA2, …)."""
    if len(streams.folders) != 1 or not streams.pack_sizes:
        raise ArchiveError("unsupported 7z header encoding")
    folder = streams.folders[0]
    if len(folder.coders) != 1:
        raise ArchiveError("unsupported 7z header encoding")
    packed_size = streams.pack_sizes[0]
    size = folder.unpack_size()
    if packed_size > MAX_HEADER or size > MAX_HEADER:
        raise ArchiveError("7z header is too large")
    packed = _read(raw, 32 + streams.pack_pos, packed_size)
    coder = folder.coders[0]
    method, props = coder.method, coder.props
    try:
        if method == _COPY:
            return packed[:size]
        if method == _LZMA:
            d = props[0]
            lc, lp, pb = d % 9, (d // 9) % 5, d // 45
            dict_size = int.from_bytes(props[1:5], "little")
            f = {"id": lzma.FILTER_LZMA1, "lc": lc, "lp": lp, "pb": pb, "dict_size": dict_size}
            return lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[f]).decompress(packed, size)
        if method == _LZMA2:
            p = props[0]
            dict_size = 0xFFFFFFFF if p >= 40 else (2 | (p & 1)) << (p // 2 + 11)
            f = {"id": lzma.FILTER_LZMA2, "dict_size": dict_size}
            return lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[f]).decompress(packed, size)
        if method == _DEFLATE:
            return zlib.decompressobj(-15).decompress(packed, size)
        if method == _BZIP2:
            return bz2.BZ2Decompressor().decompress(packed, size)
    except (lzma.LZMAError, zlib.error, OSError, IndexError) as exc:
        raise ArchiveError(f"7z header couldn't be decompressed ({exc})") from exc
    raise ArchiveError(f"unsupported 7z header method {coder.method.hex()}")


def _parse_files(buf: _Buf, streams: _Streams, listing: ArchiveListing, max_entries: int) -> None:
    count = buf.number()
    empty_stream = [False] * count
    empty_file: list[bool] = []
    names: list[str] = []
    mtimes: list[datetime | None] = [None] * count
    attributes: list[int | None] = [None] * count
    while (prop := buf.number()) != _K_END:
        part = _Buf(buf.take(buf.number()))
        if prop == _K_EMPTY_STREAM:
            empty_stream = part.bits(count)
        elif prop == _K_EMPTY_FILE:
            empty_file = part.bits(sum(empty_stream))
        elif prop == _K_NAME:
            if part.byte() != 0:
                raise ArchiveError("external 7z names aren't supported")
            names = part.data[part.pos :].decode("utf-16-le", "replace").split("\0")[:count]
        elif prop == _K_MTIME:
            defined = part.defined(count)
            part.byte()  # external
            for i, has in enumerate(defined):
                if has:
                    ticks = struct.unpack("<Q", part.take(8))[0]
                    mtimes[i] = _filetime(ticks)
        elif prop == _K_WIN_ATTRIBUTES:
            defined = part.defined(count)
            part.byte()  # external
            for i, has in enumerate(defined):
                if has:
                    attributes[i] = struct.unpack("<I", part.take(4))[0]
        # Everything else (ctime, atime, anti, start pos, dummy padding) isn't needed.
    stream = empty_index = 0
    for i in range(count):
        name = names[i] if i < len(names) else f"(file {i + 1})"
        attr = attributes[i]
        if empty_stream[i]:
            is_file = empty_index < len(empty_file) and empty_file[empty_index]
            empty_index += 1
            is_dir = (attr is not None and bool(attr & 0x10)) or not is_file
            listing.add(
                ArchiveEntry(
                    name + ("/" if is_dir else ""), None if is_dir else 0, mtimes[i], is_dir
                ),
                max_entries,
            )
            continue
        size = streams.sizes[stream] if stream < len(streams.sizes) else None
        folder = streams.stream_folder[stream] if stream < len(streams.stream_folder) else -1
        encrypted = 0 <= folder < len(streams.folders) and streams.folders[folder].encrypted
        stream += 1
        listing.add(ArchiveEntry(name, size, mtimes[i], False, encrypted), max_entries)


def _filetime(ticks: int) -> datetime | None:
    try:
        utc = _FILETIME_EPOCH + timedelta(microseconds=ticks // 10)
        return utc.astimezone().replace(tzinfo=None)
    except OverflowError, ValueError:
        return None
