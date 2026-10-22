"""Codec names from container headers, for old codecs Qt reports as "Unspecified".

Best effort from the first bytes of the file (the same head detection reads); every parser
gives up quietly on anything unexpected. Tables are deliberately plain dicts.
"""

import contextlib
import re
import struct
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from degreaser.core.sources.base import FileSource
from degreaser.core.sources.errors import SourceError


@dataclass(slots=True)
class ContainerInfo:
    container: str | None = None
    video: list[str] = field(default_factory=list)
    audio: list[str] = field(default_factory=list)

    def add(self, kind: str, name: str) -> None:
        target = self.video if kind == "video" else self.audio
        if name not in target:
            target.append(name)


# AVI / ASF video FourCCs (case-insensitive).
FOURCC_VIDEO = {
    "div3": "DivX 3 (MS MPEG-4 v3)", "div4": "DivX 3 (MS MPEG-4 v3)", "mp43": "MS MPEG-4 v3",
    "mp42": "MS MPEG-4 v2", "mpg4": "MS MPEG-4 v1", "divx": "DivX 4 (MPEG-4 Part 2)",
    "dx50": "DivX 5 (MPEG-4 Part 2)", "xvid": "Xvid (MPEG-4 Part 2)", "fmp4": "MPEG-4 Part 2",
    "mp4v": "MPEG-4 Part 2", "3iv2": "3ivx", "mjpg": "Motion JPEG", "jpeg": "Motion JPEG",
    "h264": "H.264", "x264": "H.264", "avc1": "H.264", "hevc": "H.265/HEVC",
    "wmv1": "Windows Media Video 7", "wmv2": "Windows Media Video 8",
    "wmv3": "Windows Media Video 9", "wvc1": "VC-1", "wmva": "WMV 9 Advanced",
    "mss1": "Windows Media Screen", "mss2": "Windows Media Screen 9",
    "iv32": "Indeo 3", "iv41": "Indeo 4", "iv50": "Indeo 5", "cvid": "Cinepak",
    "msvc": "Microsoft Video 1", "cram": "Microsoft Video 1", "dvsd": "DV", "dv25": "DV",
    "hfyu": "HuffYUV", "flv1": "Sorenson Spark (H.263)", "vp60": "VP6", "vp62": "VP6",
    "vp80": "VP8", "theo": "Theora", "mpg1": "MPEG-1", "mpg2": "MPEG-2", "h263": "H.263",
    "tscc": "TechSmith Screen", "rle ": "RLE", "\x00\x00\x00\x00": "Uncompressed",
}  # fmt: skip

# WAVEFORMATEX wFormatTag.
WAVE_FORMATS = {
    0x0001: "PCM", 0x0002: "MS ADPCM", 0x0003: "IEEE float PCM", 0x0006: "A-law",
    0x0007: "μ-law", 0x0011: "IMA ADPCM", 0x0031: "GSM 6.10", 0x0050: "MPEG audio (Layer 1/2)",
    0x0055: "MP3", 0x00FF: "AAC", 0x0160: "Windows Media Audio 1",
    0x0161: "Windows Media Audio 2", 0x0162: "WMA Pro", 0x0163: "WMA Lossless",
    0x000A: "WMA Voice", 0x2000: "AC-3", 0x2001: "DTS", 0x1610: "AAC (ADTS)",
    0x0270: "ATRAC3", 0x0401: "Intel Music Coder", 0x0402: "Indeo Audio", 0x674F: "Vorbis",
    0xF1AC: "FLAC", 0xFFFE: "Extensible PCM",
}  # fmt: skip

FLV_AUDIO = {
    0: "PCM", 1: "ADPCM", 2: "MP3", 3: "PCM", 4: "Nellymoser", 5: "Nellymoser", 6: "Nellymoser",
    7: "G.711 A-law", 8: "G.711 μ-law", 10: "AAC", 11: "Speex", 14: "MP3 (8 kHz)",
}  # fmt: skip
FLV_VIDEO = {
    2: "Sorenson Spark (H.263)", 3: "Screen video", 4: "VP6", 5: "VP6 with alpha",
    6: "Screen video 2", 7: "H.264", 12: "H.265/HEVC",
}  # fmt: skip

# MPEG-TS PMT stream_type.
TS_STREAMS: dict[int, tuple[str, str]] = {
    0x01: ("video", "MPEG-1"), 0x02: ("video", "MPEG-2"), 0x03: ("audio", "MPEG-1 audio"),
    0x04: ("audio", "MPEG-2 audio"), 0x0F: ("audio", "AAC"), 0x11: ("audio", "AAC (LATM)"),
    0x10: ("video", "MPEG-4 Part 2"), 0x1B: ("video", "H.264"), 0x24: ("video", "H.265/HEVC"),
    0x80: ("audio", "LPCM"), 0x81: ("audio", "AC-3"), 0x82: ("audio", "DTS"),
    0x83: ("audio", "Dolby TrueHD"), 0x84: ("audio", "E-AC-3"), 0x85: ("audio", "DTS-HD"),
    0x86: ("audio", "DTS-HD MA"), 0x87: ("audio", "E-AC-3"), 0xEA: ("video", "VC-1"),
}  # fmt: skip

# MP4/MOV sample-entry formats.
MP4_FORMATS: dict[str, tuple[str, str]] = {
    "avc1": ("video", "H.264"), "avc3": ("video", "H.264"), "hvc1": ("video", "H.265/HEVC"),
    "hev1": ("video", "H.265/HEVC"), "mp4v": ("video", "MPEG-4 Part 2"),
    "s263": ("video", "H.263"), "h263": ("video", "H.263"), "svq1": ("video", "Sorenson Video"),
    "svq3": ("video", "Sorenson Video 3"), "cvid": ("video", "Cinepak"),
    "jpeg": ("video", "Motion JPEG"), "mjpa": ("video", "Motion JPEG"),
    "mjpb": ("video", "Motion JPEG"), "dvc ": ("video", "DV"), "dvcp": ("video", "DV"),
    "ap4h": ("video", "ProRes 4444"), "apch": ("video", "ProRes 422 HQ"),
    "apcn": ("video", "ProRes 422"), "apcs": ("video", "ProRes 422 LT"),
    "apco": ("video", "ProRes 422 Proxy"), "av01": ("video", "AV1"), "vp09": ("video", "VP9"),
    "rle ": ("video", "Animation (RLE)"), "rpza": ("video", "Apple Video"),
    "mp4a": ("audio", "AAC"), "ac-3": ("audio", "AC-3"), "ec-3": ("audio", "E-AC-3"),
    "alac": ("audio", "Apple Lossless"), "ima4": ("audio", "IMA ADPCM"),
    "samr": ("audio", "AMR"), "sawb": ("audio", "AMR-WB"), "sowt": ("audio", "PCM"),
    "twos": ("audio", "PCM"), "lpcm": ("audio", "PCM"), "in24": ("audio", "PCM"),
    ".mp3": ("audio", "MP3"), "mac3": ("audio", "MACE 3"), "mac6": ("audio", "MACE 6"),
    "qdm2": ("audio", "QDesign Music 2"), "qdmc": ("audio", "QDesign Music"),
    "ulaw": ("audio", "μ-law"), "alaw": ("audio", "A-law"), "opus": ("audio", "Opus"),
    "flac": ("audio", "FLAC"),
}  # fmt: skip

# Matroska CodecID prefixes.
MKV_CODECS: list[tuple[str, str]] = [
    ("V_MPEG4/ISO/AVC", "H.264"), ("V_MPEGH/ISO/HEVC", "H.265/HEVC"),
    ("V_MPEG4/ISO", "MPEG-4 Part 2"), ("V_MPEG4/MS/V3", "MS MPEG-4 v3"), ("V_MPEG1", "MPEG-1"),
    ("V_MPEG2", "MPEG-2"), ("V_VP8", "VP8"), ("V_VP9", "VP9"), ("V_AV1", "AV1"),
    ("V_THEORA", "Theora"), ("V_MS/VFW/FOURCC", "VfW codec"), ("V_REAL/RV", "RealVideo"),
    ("V_MJPEG", "Motion JPEG"), ("A_AAC", "AAC"), ("A_AC3", "AC-3"), ("A_EAC3", "E-AC-3"),
    ("A_DTS", "DTS"), ("A_MPEG/L3", "MP3"), ("A_MPEG/L2", "MPEG Layer 2"),
    ("A_VORBIS", "Vorbis"), ("A_OPUS", "Opus"), ("A_FLAC", "FLAC"), ("A_PCM", "PCM"),
    ("A_TRUEHD", "Dolby TrueHD"), ("A_ALAC", "Apple Lossless"), ("A_REAL", "RealAudio"),
    ("A_MS/ACM", "ACM codec"),
]  # fmt: skip

REAL_AUDIO = {
    b"cook": "RealAudio Cook", b"sipr": "RealAudio Sipro", b"atrc": "ATRAC3",
    b"dnet": "RealAudio (AC-3)", b"28_8": "RealAudio 28.8", b"14_4": "RealAudio 14.4",
    b"raac": "RealAudio AAC", b"racp": "RealAudio AAC+", b"ralf": "RealAudio Lossless",
}  # fmt: skip
REAL_VIDEO = {b"RV10": "RealVideo 1", b"RV20": "RealVideo 2", b"RV30": "RealVideo 3",
              b"RV40": "RealVideo 4"}  # fmt: skip

_ASF_HEADER = uuid.UUID("75b22630-668e-11cf-a6d9-00aa0062ce6c")
_ASF_STREAM_PROPS = uuid.UUID("b7dc0791-a9b7-11cf-8ee6-00c00c205365")
_ASF_HEADER_EXT = uuid.UUID("5fbf03b5-a92e-11cf-8ee3-00c00c205365")
_ASF_AUDIO = uuid.UUID("f8699e40-5b4d-11cf-a8fd-00805f5c442b")
_ASF_VIDEO = uuid.UUID("bc19efc0-5b4d-11cf-a8fd-00805f5c442b")


def sniff_container(head: bytes) -> ContainerInfo:
    """Identify the container and stream codecs from the first bytes of a media file."""
    for check, parser in _PARSERS:
        if check(head):
            info = ContainerInfo()
            with contextlib.suppress(struct.error, IndexError, ValueError):
                parser(head, info)
            return info
    return ContainerInfo()


def _fourcc_name(raw: bytes) -> str:
    code = raw.decode("latin-1")
    return FOURCC_VIDEO.get(code.lower(), code.strip() or "unknown")


def _wave_name(tag: int) -> str:
    return WAVE_FORMATS.get(tag, f"format 0x{tag:04x}")


def _riff(head: bytes, info: ContainerInfo) -> None:
    form = head[8:12]
    if form == b"WAVE":
        info.container = "WAV"
        pos = 12
        while pos + 8 <= len(head):
            cid, size = head[pos : pos + 4], struct.unpack("<I", head[pos + 4 : pos + 8])[0]
            if cid == b"fmt ":
                info.add("audio", _wave_name(struct.unpack("<H", head[pos + 8 : pos + 10])[0]))
                return
            pos += 8 + size + (size & 1)
        return
    info.container = "AVI"
    # Walk strh/strf pairs anywhere in the header (they live inside LIST hdrl/strl).
    for m in re.finditer(rb"strh", head):
        pos = m.start()
        size = struct.unpack("<I", head[pos + 4 : pos + 8])[0]
        fcc_type, handler = head[pos + 8 : pos + 12], head[pos + 12 : pos + 16]
        strf = pos + 8 + size + (size & 1)
        has_strf = head[strf : strf + 4] == b"strf"
        if fcc_type == b"vids":
            # BITMAPINFOHEADER.biCompression is the reliable codec; fccHandler is often blank.
            compression = head[strf + 24 : strf + 28] if has_strf else handler
            info.add("video", _fourcc_name(compression if compression.strip(b"\0") else handler))
        elif fcc_type == b"auds" and has_strf:
            info.add("audio", _wave_name(struct.unpack("<H", head[strf + 8 : strf + 10])[0]))


def _asf(head: bytes, info: ContainerInfo) -> None:
    info.container = "ASF (Windows Media)"
    count = struct.unpack("<I", head[24:28])[0]
    _asf_objects(head, 30, len(head), count, info)


def _asf_objects(head: bytes, pos: int, end: int, limit: int, info: ContainerInfo) -> None:
    for _ in range(limit):
        if pos + 24 > end:
            return
        guid = uuid.UUID(bytes_le=head[pos : pos + 16])
        size = struct.unpack("<Q", head[pos + 16 : pos + 24])[0]
        if size < 24:
            return
        if guid == _ASF_STREAM_PROPS:
            _asf_stream(head[pos : pos + size], info)
        elif guid == _ASF_HEADER_EXT:
            # Extended stream properties can embed more Stream Properties objects.
            data_size = struct.unpack("<I", head[pos + 42 : pos + 46])[0]
            _asf_objects(head, pos + 46, min(pos + 46 + data_size, end), 64, info)
        pos += size


def _asf_stream(obj: bytes, info: ContainerInfo) -> None:
    stream_type = uuid.UUID(bytes_le=obj[24:40])
    specific = obj[78:]
    if stream_type == _ASF_AUDIO:
        info.add("audio", _wave_name(struct.unpack("<H", specific[0:2])[0]))
    elif stream_type == _ASF_VIDEO:
        # width(4) height(4) flags(1) format size(2), then a BITMAPINFOHEADER.
        info.add("video", _fourcc_name(specific[11 + 16 : 11 + 20]))


def _flv(head: bytes, info: ContainerInfo) -> None:
    info.container = "Flash Video"
    pos = struct.unpack(">I", head[5:9])[0] + 4
    while pos + 12 <= len(head) and not (info.video and info.audio):
        tag_type = head[pos] & 0x1F
        size = int.from_bytes(head[pos + 1 : pos + 4], "big")
        first = head[pos + 11] if size else 0
        if tag_type == 8:
            info.add("audio", FLV_AUDIO.get(first >> 4, f"FLV audio {first >> 4}"))
        elif tag_type == 9:
            if first & 0x80:  # enhanced RTMP: FourCC follows
                info.add("video", _fourcc_name(head[pos + 12 : pos + 16]))
            else:
                info.add("video", FLV_VIDEO.get(first & 0x0F, f"FLV video {first & 0x0F}"))
        pos += 11 + size + 4


def _realmedia(head: bytes, info: ContainerInfo) -> None:
    info.container = "RealMedia"
    for code, name in REAL_VIDEO.items():
        if b"VIDO" + code in head:
            info.add("video", name)
    if b".ra\xfd" in head:
        found = [name for code, name in REAL_AUDIO.items() if code in head]
        for name in found or ["RealAudio"]:
            info.add("audio", name)


def _mpeg_ps(head: bytes, info: ContainerInfo) -> None:
    mpeg2 = (head[4] & 0xC0) == 0x40
    info.container = "MPEG-2 program stream" if mpeg2 else "MPEG-1 system stream"
    for m in re.finditer(rb"\x00\x00\x01([\xb3\xc0-\xef\xbd])", head):
        sid = m.group(1)[0]
        if sid == 0xB3 or 0xE0 <= sid <= 0xEF:
            # A sequence extension start code (00 00 01 B5) means MPEG-2 video.
            info.add("video", "MPEG-2" if b"\x00\x00\x01\xb5" in head else "MPEG-1")
        elif 0xC0 <= sid <= 0xDF:
            info.add("audio", "MPEG audio")
        elif sid == 0xBD:
            header_len = head[m.start() + 8] if m.start() + 8 < len(head) else 0
            sub_at = m.start() + 9 + header_len
            if sub_at < len(head):
                sub = head[sub_at]
                if 0x80 <= sub <= 0x87:
                    info.add("audio", "AC-3")
                elif 0x88 <= sub <= 0x8F:
                    info.add("audio", "DTS")
                elif 0xA0 <= sub <= 0xA7:
                    info.add("audio", "LPCM")


def _mpeg_ts(head: bytes, info: ContainerInfo) -> None:
    step, start = (192, 4) if head[4] == 0x47 and head[196:197] == b"\x47" else (188, 0)
    info.container = "MPEG transport stream" + (" (M2TS)" if step == 192 else "")
    packets = [head[i : i + 188] for i in range(start, len(head) - 187, step)]
    pmt_pids: set[int] = set()
    for pkt in packets:
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        if not pkt[1] & 0x40:  # payload_unit_start: tables begin here
            continue
        payload = pkt[5 + pkt[4] :] if pkt[3] & 0x20 else pkt[4:]  # skip any adaptation field
        section = payload[1 + payload[0] :] if payload else b""
        if pid == 0 and section[:1] == b"\x00":
            length = ((section[1] & 0x0F) << 8) | section[2]
            for i in range(8, 3 + length - 4, 4):
                program = (section[i] << 8) | section[i + 1]
                if program:
                    pmt_pids.add(((section[i + 2] & 0x1F) << 8) | section[i + 3])
        elif pid in pmt_pids and section[:1] == b"\x02":
            length = ((section[1] & 0x0F) << 8) | section[2]
            info_len = ((section[10] & 0x0F) << 8) | section[11]
            i, end = 12 + info_len, 3 + length - 4
            while i + 5 <= end:
                stream_type = section[i]
                es_len = ((section[i + 3] & 0x0F) << 8) | section[i + 4]
                kind, name = TS_STREAMS.get(stream_type, ("", ""))
                if stream_type == 0x06:
                    kind, name = _ts_private(section[i + 5 : i + 5 + es_len])
                if kind:
                    info.add(kind, name)
                i += 5 + es_len
            return


def _ts_private(descriptors: bytes) -> tuple[str, str]:
    i = 0
    while i + 2 <= len(descriptors):
        tag, length = descriptors[i], descriptors[i + 1]
        if tag == 0x6A:
            return ("audio", "AC-3")
        if tag == 0x7A:
            return ("audio", "E-AC-3")
        if tag in (0x7B, 0x73):
            return ("audio", "DTS")
        if tag == 0x05 and descriptors[i + 2 : i + 6] == b"AC-3":
            return ("audio", "AC-3")
        i += 2 + length
    return ("", "")


def _mp4(head: bytes, info: ContainerInfo) -> None:
    brand = head[8:12]
    info.container = "QuickTime" if brand == b"qt  " else "MPEG-4"
    # Sample descriptions only appear here when the moov atom is at the front.
    for m in re.finditer(rb"stsd", head):
        pos = m.start() + 4 + 4  # skip "stsd", version+flags
        count = struct.unpack(">I", head[pos : pos + 4])[0]
        pos += 4
        for _ in range(min(count, 8)):
            size = struct.unpack(">I", head[pos : pos + 4])[0]
            fmt = head[pos + 4 : pos + 8].decode("latin-1")
            kind, name = MP4_FORMATS.get(fmt.lower(), MP4_FORMATS.get(fmt, ("", "")))
            if kind:
                info.add(kind, name)
            if size < 8:
                break
            pos += size


_MKV_CODEC = re.compile(rb"\x86([\x81-\xbf])([VA]_[A-Z0-9/_.\-]+)")


def _matroska(head: bytes, info: ContainerInfo) -> None:
    info.container = "WebM" if b"webm" in head[:64] else "Matroska"
    for m in _MKV_CODEC.finditer(head):
        length = m.group(1)[0] & 0x7F
        codec_id = m.group(2)[:length].decode("ascii", "replace")
        kind = "video" if codec_id.startswith("V_") else "audio"
        name = next((n for prefix, n in MKV_CODECS if codec_id.startswith(prefix)), codec_id)
        info.add(kind, name)


def _ogg(head: bytes, info: ContainerInfo) -> None:
    info.container = "Ogg"
    for marker, kind, name in (
        (b"\x80theora", "video", "Theora"),
        (b"\x01vorbis", "audio", "Vorbis"),
        (b"OpusHead", "audio", "Opus"),
        (b"\x7fFLAC", "audio", "FLAC"),
        (b"Speex   ", "audio", "Speex"),
    ):
        if marker in head:
            info.add(kind, name)


def _mpeg_audio(head: bytes, info: ContainerInfo) -> None:
    pos = 0
    if head[:3] == b"ID3":
        size = head[6:10]
        pos = 10 + ((size[0] << 21) | (size[1] << 14) | (size[2] << 7) | size[3])
    if pos + 2 > len(head) or head[pos] != 0xFF or (head[pos + 1] & 0xE0) != 0xE0:
        return
    layer = (head[pos + 1] >> 1) & 0x3
    info.container = "MPEG audio" if layer else "ADTS"
    info.add("audio", {0: "AAC", 1: "MP3", 2: "MPEG Layer 2", 3: "MPEG Layer 1"}[layer])


def _aiff(head: bytes, info: ContainerInfo) -> None:
    info.container = "AIFF"
    if head[8:12] == b"AIFF":
        info.add("audio", "PCM")
        return
    at = head.find(b"COMM")
    if at >= 0:
        code = head[at + 26 : at + 30]
        names = {b"NONE": "PCM", b"sowt": "PCM", b"twos": "PCM", b"fl32": "PCM (float)",
                 b"ima4": "IMA ADPCM", b"ulaw": "μ-law", b"alaw": "A-law", b"MAC3": "MACE 3",
                 b"MAC6": "MACE 6"}  # fmt: skip
        info.add("audio", names.get(code, code.decode("latin-1")))


def _single(container: str, kind: str, name: str) -> Callable[[bytes, ContainerInfo], None]:
    def parse(_head: bytes, info: ContainerInfo) -> None:
        info.container = container
        info.add(kind, name)

    return parse


def _is_ts(head: bytes) -> bool:
    return any(
        len(head) > start + 2 * step and all(head[start + i * step] == 0x47 for i in range(3))
        for step, start in ((188, 0), (192, 4))
    )


_PARSERS: list[tuple[Callable[[bytes], bool], Callable[[bytes, ContainerInfo], None]]] = [
    (lambda h: h[:4] == b"RIFF" and h[8:12] in (b"AVI ", b"WAVE"), _riff),
    (lambda h: len(h) >= 30 and uuid.UUID(bytes_le=h[:16]) == _ASF_HEADER, _asf),
    (lambda h: h[:3] == b"FLV", _flv),
    (lambda h: h[:4] == b".RMF", _realmedia),
    (lambda h: h[:4] == b"\x00\x00\x01\xba" and len(h) > 4, _mpeg_ps),
    (_is_ts, _mpeg_ts),
    (lambda h: h[4:8] in (b"ftyp", b"moov", b"mdat", b"wide", b"free"), _mp4),
    (lambda h: h[:4] == b"\x1a\x45\xdf\xa3", _matroska),
    (lambda h: h[:4] == b"OggS", _ogg),
    (lambda h: h[:4] == b"FORM" and h[8:12] in (b"AIFF", b"AIFC"), _aiff),
    (lambda h: h[:4] == b"fLaC", _single("FLAC", "audio", "FLAC")),
    (lambda h: h[:2] == b"\x0b\x77", _single("AC-3", "audio", "AC-3")),
    (lambda h: h[:3] == b"\x1f\x07\x00", _single("DV", "video", "DV")),
    (
        lambda h: h[:3] == b"ID3" or (len(h) > 1 and h[0] == 0xFF and h[1] & 0xE0 == 0xE0),
        _mpeg_audio,
    ),
]

_MP4_FAMILY = {"MPEG-4", "QuickTime"}
MOOV_READ_LIMIT = 4 * 1024 * 1024


def sniff_container_file(source: FileSource, rel: str, size: int, head: bytes) -> ContainerInfo:
    """sniff_container, plus a range read of the moov atom when an MP4/MOV keeps it at the end."""
    info = sniff_container(head)
    if info.container in _MP4_FAMILY and not (info.video or info.audio) and size > len(head):
        try:
            moov = _find_moov(source, rel, size)
        except struct.error, ValueError, SourceError:
            moov = None
        if moov is not None:
            offset, length = moov
            data = source.read_range(rel, offset, min(length, MOOV_READ_LIMIT))
            with contextlib.suppress(struct.error, IndexError, ValueError):
                _mp4(b"\x00" * 4 + b"ftyp" + head[8:12] + data, info)
    return info


def _find_moov(source: FileSource, rel: str, size: int) -> tuple[int, int] | None:
    """Walk top-level MP4 atoms with 16-byte range reads to locate moov."""
    pos = 0
    for _ in range(64):
        if pos + 8 > size:
            return None
        header = source.read_range(rel, pos, 16)
        length, kind = struct.unpack(">I4s", header[:8])
        if length == 1:
            length = struct.unpack(">Q", header[8:16])[0]
        elif length == 0:
            length = size - pos
        if length < 8:
            return None
        if kind == b"moov":
            return pos, length
        pos += length
    return None
