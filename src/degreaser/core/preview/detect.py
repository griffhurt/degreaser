"""File-type detection: extension map, magic bytes, then a text heuristic (docs/previews.md §1.1).

Old backups often have wrong or missing extensions, so magic bytes win over the extension.
"""

import codecs
from dataclasses import dataclass
from enum import StrEnum

import charset_normalizer
import puremagic

from degreaser.core.sources.base import Entry, FileSource, basename

# How much of the file detection reads: magic needs 4 KB, the text heuristic up to 64 KB.
HEAD_BYTES = 64 * 1024
MAGIC_BYTES = 4 * 1024


class Kind(StrEnum):
    TEXT = "text"
    CODE = "code"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    PDF = "pdf"
    DOCUMENT = "document"  # Office / OpenDocument / iWork: zip-based, confirmed by contents
    ZIP = "zip"
    BINARY = "binary"


@dataclass(frozen=True, slots=True)
class Detection:
    kind: Kind
    label: str  # human-readable type, e.g. "JPEG image"
    mime: str | None = None
    encoding: str | None = None  # text kinds only (Python codec name)
    bom: bool = False
    extension_mismatch: bool = False
    extension_label: str | None = None  # what the extension claimed, when it disagrees

    @property
    def is_text(self) -> bool:
        return self.kind in (Kind.TEXT, Kind.CODE)


# extension → (kind, mime, label)
_EXT: dict[str, tuple[Kind, str | None, str]] = {}


def _add(kind: Kind, label: str, mime: str | None, *exts: str) -> None:
    for ext in exts:
        _EXT[ext] = (kind, mime, label)


_add(Kind.IMAGE, "JPEG image", "image/jpeg", "jpg", "jpeg", "jpe", "jfif")
_add(Kind.IMAGE, "PNG image", "image/png", "png")
_add(Kind.IMAGE, "GIF image", "image/gif", "gif")
_add(Kind.IMAGE, "BMP image", "image/bmp", "bmp", "dib")
_add(Kind.IMAGE, "TIFF image", "image/tiff", "tif", "tiff")
_add(Kind.IMAGE, "WebP image", "image/webp", "webp")
_add(Kind.IMAGE, "Icon", "image/x-icon", "ico", "cur")
_add(Kind.IMAGE, "macOS icon", "image/icns", "icns")
_add(Kind.IMAGE, "TGA image", "image/x-tga", "tga")
_add(Kind.IMAGE, "SVG image", "image/svg+xml", "svg", "svgz")
_add(Kind.IMAGE, "HEIC image", "image/heic", "heic", "heif")
_add(Kind.IMAGE, "AVIF image", "image/avif", "avif")
_add(Kind.IMAGE, "Photoshop image", "image/vnd.adobe.photoshop", "psd")
_add(Kind.IMAGE, "PCX image", "image/x-pcx", "pcx")
_add(Kind.IMAGE, "Netpbm image", "image/x-portable-anymap", "pbm", "pgm", "ppm", "pnm")
_add(Kind.IMAGE, "X bitmap", "image/x-xbitmap", "xbm", "xpm")

_add(Kind.VIDEO, "AVI video", "video/x-msvideo", "avi", "divx")
_add(Kind.VIDEO, "Windows Media video", "video/x-ms-wmv", "wmv", "asf")
_add(Kind.VIDEO, "Matroska video", "video/x-matroska", "mkv")
_add(Kind.VIDEO, "WebM video", "video/webm", "webm")
_add(Kind.VIDEO, "MPEG video", "video/mpeg", "mpg", "mpeg", "mpe", "m1v", "m2v", "vob")
_add(Kind.VIDEO, "MPEG transport stream", "video/mp2t", "mts", "m2ts", "m2t")
_add(Kind.VIDEO, "Flash video", "video/x-flv", "flv", "f4v")
_add(Kind.VIDEO, "3GP video", "video/3gpp", "3gp", "3g2")
_add(Kind.VIDEO, "QuickTime video", "video/quicktime", "mov", "qt")
_add(Kind.VIDEO, "MPEG-4 video", "video/mp4", "mp4", "m4v")
_add(Kind.VIDEO, "Ogg video", "video/ogg", "ogv")
_add(Kind.VIDEO, "DV video", "video/dv", "dv")
_add(Kind.VIDEO, "RealMedia", "application/vnd.rn-realmedia", "rm", "rmvb")

_add(Kind.AUDIO, "MP3 audio", "audio/mpeg", "mp3")
_add(Kind.AUDIO, "MPEG audio", "audio/mpeg", "mp2", "mpa")
_add(Kind.AUDIO, "WAV audio", "audio/wav", "wav")
_add(Kind.AUDIO, "AAC audio", "audio/aac", "aac")
_add(Kind.AUDIO, "MPEG-4 audio", "audio/mp4", "m4a", "m4b")
_add(Kind.AUDIO, "FLAC audio", "audio/flac", "flac")
_add(Kind.AUDIO, "Ogg audio", "audio/ogg", "ogg", "oga")
_add(Kind.AUDIO, "Opus audio", "audio/opus", "opus")
_add(Kind.AUDIO, "Windows Media audio", "audio/x-ms-wma", "wma")
_add(Kind.AUDIO, "AIFF audio", "audio/aiff", "aif", "aiff", "aifc")
_add(Kind.AUDIO, "AMR audio", "audio/amr", "amr")
_add(Kind.AUDIO, "AC-3 audio", "audio/ac3", "ac3")
_add(Kind.AUDIO, "RealAudio", "audio/x-pn-realaudio", "ra")

_add(Kind.PDF, "PDF document", "application/pdf", "pdf")

_add(Kind.ZIP, "Zip archive", "application/zip", "zip")
_add(Kind.ZIP, "Java archive", "application/java-archive", "jar", "war", "ear")
_OOXML = "application/vnd.openxmlformats-officedocument."
_ODF = "application/vnd.oasis.opendocument."
_add(Kind.DOCUMENT, "Word document", _OOXML + "wordprocessingml.document", "docx", "docm")
_add(Kind.DOCUMENT, "Word template", _OOXML + "wordprocessingml.template", "dotx", "dotm")
_add(Kind.DOCUMENT, "Excel workbook", _OOXML + "spreadsheetml.sheet", "xlsx", "xlsm")
_add(Kind.DOCUMENT, "Excel template", _OOXML + "spreadsheetml.template", "xltx", "xltm")
_add(
    Kind.DOCUMENT, "PowerPoint presentation", _OOXML + "presentationml.presentation", "pptx", "pptm"
)
_add(Kind.DOCUMENT, "PowerPoint slide show", _OOXML + "presentationml.slideshow", "ppsx", "ppsm")
_add(Kind.DOCUMENT, "PowerPoint template", _OOXML + "presentationml.template", "potx", "potm")
_add(Kind.DOCUMENT, "OpenDocument text", _ODF + "text", "odt", "ott")
_add(Kind.DOCUMENT, "OpenDocument spreadsheet", _ODF + "spreadsheet", "ods", "ots")
_add(Kind.DOCUMENT, "OpenDocument presentation", _ODF + "presentation", "odp", "otp")
_add(Kind.DOCUMENT, "Pages document", "application/vnd.apple.pages", "pages")
_add(Kind.DOCUMENT, "Numbers spreadsheet", "application/vnd.apple.numbers", "numbers")
_add(Kind.DOCUMENT, "Keynote presentation", "application/vnd.apple.keynote", "key")
_add(Kind.ZIP, "EPUB book", "application/epub+zip", "epub")
_add(Kind.ZIP, "Android package", "application/vnd.android.package-archive", "apk")
_add(Kind.ZIP, "Comic book archive", "application/vnd.comicbook+zip", "cbz")
_add(Kind.ZIP, "Python wheel", "application/zip", "whl")
_add(Kind.ZIP, "Firefox extension", "application/x-xpinstall", "xpi")

_add(
    Kind.CODE, "Source code", None,
    "py", "pyw", "js", "mjs", "cjs", "jsx", "tsx", "c", "h", "cc", "cpp", "cxx", "hpp", "hh",
    "m", "mm", "java", "kt", "kts", "scala", "cs", "vb", "fs", "go", "rs", "rb", "php", "pl",
    "pm", "lua", "r", "swift", "dart", "sh", "bash", "zsh", "fish", "bat", "cmd", "ps1", "psm1",
    "sql", "asm", "s", "pas", "f90", "hs", "clj", "ex", "exs", "erl", "el", "lisp", "scm", "vue",
    "svelte", "gradle", "cmake", "mk", "groovy", "tcl", "awk", "sed", "coffee", "ino",
)  # fmt: skip
_add(Kind.CODE, "TypeScript", None, "ts")  # or MPEG-TS video: magic bytes decide
_add(Kind.CODE, "HTML document", "text/html", "html", "htm", "xhtml", "shtml")
_add(Kind.CODE, "Stylesheet", "text/css", "css", "scss", "sass", "less")
_add(Kind.CODE, "JSON", "application/json", "json", "jsonl", "geojson")
_add(Kind.CODE, "XML", "application/xml", "xml", "xsl", "xsd", "plist", "csproj", "vcxproj")
_add(Kind.CODE, "YAML", "application/yaml", "yml", "yaml")
_add(Kind.CODE, "TOML", "application/toml", "toml")
_add(Kind.CODE, "Config file", None, "ini", "cfg", "conf", "properties", "env", "reg", "inf")
_add(Kind.CODE, "Markdown", "text/markdown", "md", "markdown", "rst", "tex")

_add(Kind.TEXT, "Text file", "text/plain", "txt", "text", "log", "nfo", "diz", "me", "1st")
_add(Kind.TEXT, "CSV table", "text/csv", "csv", "tsv")
_add(Kind.TEXT, "Subtitles", None, "srt", "sub", "ass", "ssa", "vtt")
_add(Kind.TEXT, "Rich text", "application/rtf", "rtf")
_add(Kind.TEXT, "Playlist", None, "m3u", "m3u8", "pls", "cue")
_add(Kind.TEXT, "Contact / calendar", None, "vcf", "ics", "eml")

# Well-known extensionless text files.
_TEXT_NAMES = {"readme", "license", "copying", "makefile", "dockerfile", "changelog", "authors"}

# puremagic labels a match by extension; map those onto our kinds.
_MAGIC_EXT_ALIASES = {"jfif": "jpg", "jpe": "jpg", "fdf": "pdf"}

_BOMS: list[tuple[bytes, str]] = [
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
]

_CONTROL_OK = set("\t\n\r\f\v\x1b")


def _extension(name: str) -> str:
    stem, dot, ext = name.rpartition(".")
    return ext.lower() if dot and stem else ""


def by_extension(name: str) -> tuple[Kind, str | None, str] | None:
    ext = _extension(name)
    if ext in _EXT:
        return _EXT[ext]
    if name.lower() in _TEXT_NAMES:
        return (Kind.TEXT, "text/plain", "Text file")
    return None


def _is_mpeg_ts(head: bytes) -> bool:
    # 188-byte packets (MPEG-TS) or 192-byte packets with a 4-byte timecode (M2TS).
    for step, start in ((188, 0), (192, 4)):
        if len(head) > start + 2 * step and all(head[start + i * step] == 0x47 for i in range(3)):
            return True
    return False


def _by_magic(
    head: bytes, ext: str, claimed: tuple[Kind, str | None, str] | None
) -> tuple[Kind, str | None, str] | None:
    """Kind from magic bytes. Returns None when nothing binary is recognised."""
    if _is_mpeg_ts(head):
        return _EXT["mts"]
    if head[:4] == b"\x00\x00\x01\xba":
        return _EXT["mpg"]
    if head[:4] == b".RMF":
        return _EXT["rm"]
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return _EXT["avi"]
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return _EXT["wav"]
    try:
        matches = puremagic.magic_string(head[:MAGIC_BYTES])
    except puremagic.PureError, ValueError:
        return None
    candidates: list[tuple[Kind, str | None, str]] = []
    for m in matches:
        mext = (m.extension or "").lstrip(".").lower()
        mext = _MAGIC_EXT_ALIASES.get(mext, mext)
        known = _EXT.get(mext)
        if known is None or known[0] in (Kind.TEXT, Kind.CODE):
            # Text-ish magic (HTML, XML, …) is left to the text heuristic.
            continue
        if mext == ext:
            return known  # the extension agrees with one of the matches: trust it
        candidates.append(known)
    if not candidates:
        return None
    if claimed is not None and claimed[0] == Kind.IMAGE and candidates[0][2] == "Icon":
        # The ICO signature (00 00 01 00) is weak; TGA and others match it by accident.
        return claimed
    if claimed is not None and any(_compatible(claimed, c) for c in candidates):
        # e.g. a .zip whose magic also matches docx/xlsx: keep what the extension says.
        return claimed
    if candidates[0][0] in (Kind.ZIP, Kind.DOCUMENT):
        # Every zip-based format matches; without a matching extension it's just a zip.
        return _EXT["zip"]
    return candidates[0]


def sniff_text(sample: bytes) -> tuple[str, bool] | None:
    """Return (codec name, has BOM) if the sample looks like human-readable text."""
    if not sample:
        return ("utf-8", False)
    encoding: str | None = None
    bom = False
    for mark, name in _BOMS:
        if sample.startswith(mark):
            encoding, bom = name, True
            break
    if encoding is None:
        if b"\x00" in sample:
            return None
        encoding = _utf8_or_ascii(sample)
    if encoding is None:
        best = charset_normalizer.from_bytes(sample).best()
        if best is None or best.chaos > 0.25:
            return None
        encoding = best.encoding
    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    text = decoder.decode(sample, final=False)
    if not text:
        return None
    if bom and encoding != "utf-8-sig":
        text = text.lstrip("\ufeff")
    bad = sum(1 for ch in text if not ch.isprintable() and ch not in _CONTROL_OK)
    bad += text.count("�")
    if bad > 0.05 * len(text):
        return None
    return (encoding, bom)


def _utf8_or_ascii(sample: bytes) -> str | None:
    try:
        # final=False tolerates a multibyte sequence cut off at the end of the sample.
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
    except UnicodeDecodeError:
        return None
    return "ascii" if sample.isascii() else "utf-8"


def detect_bytes(name: str, head: bytes) -> Detection:
    """Classify from the file name and the first bytes (≤ HEAD_BYTES). Pure; no I/O."""
    ext = _extension(name)
    claimed = by_extension(name)
    magic = _by_magic(head, ext, claimed) if head else None

    if magic is not None:
        kind, mime, label = magic
        mismatch = (
            claimed is not None
            and not _compatible(claimed, magic)
            and not (ext == "ts" and kind == Kind.VIDEO)
        )
        return Detection(
            kind,
            label,
            mime,
            extension_mismatch=mismatch,
            extension_label=claimed[2] if mismatch and claimed else None,
        )

    text = sniff_text(head)
    if text is not None:
        encoding, bom = text
        if claimed is not None and claimed[0] in (Kind.TEXT, Kind.CODE):
            kind, mime, label = claimed
            return Detection(kind, label, mime, encoding=encoding, bom=bom)
        # ".key" is also the usual name for PEM/SSH key files, which are text: no warning.
        mismatch = claimed is not None and ext != "key"
        return Detection(
            Kind.TEXT,
            "Text file",
            "text/plain",
            encoding=encoding,
            bom=bom,
            extension_mismatch=mismatch,
            extension_label=claimed[2] if claimed else None,
        )

    if claimed is not None and claimed[0] not in (Kind.TEXT, Kind.CODE):
        # Magic didn't recognise it but the extension names a media type (e.g. WMA without
        # a matching puremagic signature): let the renderer try.
        kind, mime, label = claimed
        return Detection(kind, label, mime)

    mismatch = claimed is not None
    return Detection(
        Kind.BINARY,
        "Binary file",
        "application/octet-stream",
        extension_mismatch=mismatch,
        extension_label=claimed[2] if claimed else None,
    )


def _compatible(claimed: tuple[Kind, str | None, str], found: tuple[Kind, str | None, str]) -> bool:
    if claimed == found:
        return True
    if claimed[0] == found[0] and claimed[0] in (Kind.ZIP, Kind.DOCUMENT, Kind.VIDEO, Kind.AUDIO):
        # docx-vs-xlsx, mp4-vs-mov, m4a-vs-mp4 and friends share containers.
        return True
    if {claimed[0], found[0]} == {Kind.ZIP, Kind.DOCUMENT}:
        # Office/OpenDocument/iWork files are zips; the document loader checks the contents.
        return True
    # Audio-only files in video containers (and vice versa) are fine.
    return {claimed[0], found[0]} == {Kind.AUDIO, Kind.VIDEO}


def detect(source: FileSource, rel: str, entry: Entry) -> tuple[Detection, bytes]:
    """Read the head of the file and classify it. Returns the head too, for reuse."""
    if entry.kind == "dir":
        raise ValueError(f"cannot detect a directory: {rel}")
    if entry.kind != "file":
        label = "Symbolic link" if entry.kind == "symlink" else "Special file"
        return Detection(Kind.BINARY, label), b""
    length = HEAD_BYTES if entry.size is None else min(HEAD_BYTES, entry.size)
    head = source.read_range(rel, 0, length) if length else b""
    return detect_bytes(basename(rel), head), head
