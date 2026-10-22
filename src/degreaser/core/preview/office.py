"""Office documents for preview: OOXML (docx/xlsx/pptx), OpenDocument (odt/ods/odp) and iWork
(pages/numbers/key, via the preview image they embed). Pure Python; no Qt. Blocking: call from a
worker thread.

Everything is read through FileSource range reads: only the zip's central directory and the XML
parts a preview needs are fetched (docs/previews.md). Hard caps guard against zip bombs and huge
files, and content caps (paragraphs, rows, slides) keep previews quick; `expand()` re-parses the
already-fetched parts with larger caps for "Load more".

The kind of document comes from the zip's contents, not the extension. Anything unexpected
raises an OfficeError subclass so the caller can fall back to a plain zip listing.
"""

import datetime as dt
import io
import posixpath
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Protocol
from xml.etree import ElementTree as ET

from degreaser.core.preview.loaders import CancelToken, RangeReader
from degreaser.core.preview.metadata import MetaRow
from degreaser.core.sources.base import Entry, FileSource

READ_CHUNK = 256 * 1024


# Errors ---------------------------------------------------------------------------------------


class OfficeError(Exception):
    """The file can't be previewed as a document (fall back to a zip listing)."""


class NotOfficeDocument(OfficeError):
    pass


class OfficeTooLarge(OfficeError):
    pass


# Limits ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OfficeLimits:
    part_bytes: int = 16 * 1024 * 1024  # decompressed size of any one XML part / image
    total_bytes: int = 64 * 1024 * 1024  # decompressed bytes across all parts read
    blocks: int = 400  # paragraphs + tables (Word, OpenDocument text)
    rows: int = 500  # per sheet
    cols: int = 60  # per sheet (not expandable)
    sheets: int = 30
    slides: int = 100
    table_rows: int = 200  # rows of a table inside a text document
    table_cols: int = 30

    def expanded(self) -> OfficeLimits:
        """Caps for one "Load more". Equal to self once the ceilings are reached."""
        return OfficeLimits(
            part_bytes=self.part_bytes,
            total_bytes=self.total_bytes,
            blocks=min(self.blocks * 5, 20_000),
            rows=min(self.rows * 10, 20_000),
            cols=self.cols,
            sheets=min(self.sheets * 4, 200),
            slides=min(self.slides * 5, 2_000),
            table_rows=min(self.table_rows * 5, 5_000),
            table_cols=self.table_cols,
        )


# Content model --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Run:
    text: str
    bold: bool = False
    italic: bool = False


@dataclass(frozen=True, slots=True)
class Paragraph:
    runs: tuple[Run, ...]
    style: str = "p"  # "p", "title", "subtitle", "h1".."h6"
    list_level: int | None = None  # set for list items (0 = top level)
    list_marker: str = "•"

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)


@dataclass(frozen=True, slots=True)
class Table:
    rows: tuple[tuple[str, ...], ...]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class PageBreak:
    pass


Block = Paragraph | Table | PageBreak


@dataclass(slots=True)
class TextContent:
    blocks: list[Block] = field(default_factory=list)
    shown: int = 0  # paragraphs + tables included in `blocks`
    total: int = 0  # paragraphs + tables in the whole document

    @property
    def truncated(self) -> bool:
        return self.shown < self.total


@dataclass(slots=True)
class Sheet:
    name: str
    rows: list[tuple[int, list[str]]] = field(default_factory=list)  # (row number, cells)
    columns: int = 0  # cells per row in `rows`
    total_rows: int = 0  # non-empty rows in the sheet
    max_col: int = 0  # widest row in the sheet (1-based)
    hidden: bool = False
    note: str = ""  # why the sheet couldn't be read, if it couldn't

    @property
    def truncated_rows(self) -> bool:
        return len(self.rows) < self.total_rows

    @property
    def truncated_cols(self) -> bool:
        return self.columns < self.max_col


@dataclass(slots=True)
class SheetContent:
    sheets: list[Sheet] = field(default_factory=list)
    total_sheets: int = 0

    @property
    def truncated(self) -> bool:
        return len(self.sheets) < self.total_sheets or any(s.truncated_rows for s in self.sheets)


@dataclass(slots=True)
class Slide:
    number: int
    title: str = ""
    bullets: list[tuple[int, str]] = field(default_factory=list)  # (indent level, text)


@dataclass(slots=True)
class SlidesContent:
    slides: list[Slide] = field(default_factory=list)
    total_slides: int = 0
    thumbnail: bytes | None = None  # image the app saved into the file, if any

    @property
    def truncated(self) -> bool:
        return len(self.slides) < self.total_slides


@dataclass(slots=True)
class ImageContent:
    """iWork files: just the preview image the app embeds."""

    image: bytes
    name: str

    @property
    def truncated(self) -> bool:
        return False


Content = TextContent | SheetContent | SlidesContent | ImageContent


class DocKind(StrEnum):
    WORD = "word"
    SHEET = "sheet"
    SLIDES = "slides"
    ODF_TEXT = "odf_text"
    ODF_SHEET = "odf_sheet"
    ODF_SLIDES = "odf_slides"
    IWORK = "iwork"


DOC_LABELS = {
    DocKind.WORD: "Word document",
    DocKind.SHEET: "Excel workbook",
    DocKind.SLIDES: "PowerPoint presentation",
    DocKind.ODF_TEXT: "OpenDocument text",
    DocKind.ODF_SHEET: "OpenDocument spreadsheet",
    DocKind.ODF_SLIDES: "OpenDocument presentation",
    DocKind.IWORK: "iWork document",
}


@dataclass(slots=True)
class OfficeDocument:
    kind: DocKind
    content: Content
    metadata: list[MetaRow]
    limits: OfficeLimits
    parts: dict[str, bytes]  # decompressed parts already fetched, reused by expand()
    bytes_read: int = 0  # compressed bytes fetched from the source

    @property
    def label(self) -> str:
        return DOC_LABELS[self.kind]

    @property
    def can_expand(self) -> bool:
        return self.content.truncated and self.limits.expanded() != self.limits

    def expand(self, cancel: CancelToken | None = None) -> OfficeDocument:
        """Re-parse the cached parts with larger caps (no further reads from the source)."""
        limits = self.limits.expanded()
        parts = _CachedParts(self.parts, cancel)
        doc = _parse(self.kind, parts, limits)
        doc.bytes_read = self.bytes_read
        return doc


# Reading parts --------------------------------------------------------------------------------


class _Parts(Protocol):
    def names(self) -> set[str]: ...
    def read(self, name: str, required: bool = True) -> bytes | None: ...
    def cache(self) -> dict[str, bytes]: ...


class _ZipParts:
    """Reads named entries from the zip, enforcing the size caps on decompressed bytes."""

    def __init__(self, zf: zipfile.ZipFile, limits: OfficeLimits, cancel: CancelToken | None):
        self.zf = zf
        self.limits = limits
        self.cancel = cancel
        self.total = 0
        self._cache: dict[str, bytes] = {}
        self._by_lower = {info.filename.lower(): info for info in zf.infolist()}

    def names(self) -> set[str]:
        return set(self._by_lower)

    def cache(self) -> dict[str, bytes]:
        return self._cache

    def read(self, name: str, required: bool = True) -> bytes | None:
        key = name.lower()
        if key in self._cache:
            return self._cache[key]
        info = self._by_lower.get(key)
        if info is None:
            if required:
                raise NotOfficeDocument(f"missing {name}")
            return None
        if info.flag_bits & 0x1:
            raise OfficeError("the document is encrypted")
        cap = self.limits.part_bytes
        if info.file_size > cap:
            raise OfficeTooLarge(f"{name} is too large to preview")
        chunks: list[bytes] = []
        size = 0
        # Read in chunks and stop past the cap: the header's size can't be trusted (zip bombs).
        with self.zf.open(info) as f:
            while True:
                if self.cancel:
                    self.cancel.check()
                chunk = f.read(READ_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > cap:
                    raise OfficeTooLarge(f"{name} expands to more than {cap // (1024 * 1024)} MB")
                chunks.append(chunk)
        self.total += size
        if self.total > self.limits.total_bytes:
            raise OfficeTooLarge("the document is too large to preview")
        data = b"".join(chunks)
        self._cache[key] = data
        return data


class _CachedParts:
    def __init__(self, parts: dict[str, bytes], cancel: CancelToken | None):
        self._parts = parts
        self.cancel = cancel

    def names(self) -> set[str]:
        return set(self._parts)

    def cache(self) -> dict[str, bytes]:
        return self._parts

    def read(self, name: str, required: bool = True) -> bytes | None:
        if self.cancel:
            self.cancel.check()
        data = self._parts.get(name.lower())
        if data is None and required:
            raise NotOfficeDocument(f"missing {name}")
        return data


def _check_xml(data: bytes) -> None:
    # Office XML never needs a DTD; refusing one rules out entity tricks entirely.
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise OfficeError("XML with a DOCTYPE is not accepted")


def _xml(data: bytes) -> ET.Element:
    _check_xml(data)
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise OfficeError(f"malformed XML ({exc})") from exc


def _iterparse(
    data: bytes, events: tuple[Literal["start", "end"], ...] = ("start", "end")
) -> Iterator[tuple[str, ET.Element]]:
    _check_xml(data)
    try:
        yield from ET.iterparse(io.BytesIO(data), events=events)
    except ET.ParseError as exc:
        raise OfficeError(f"malformed XML ({exc})") from exc


PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
DOC_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _rels(parts: _Parts, path: str) -> dict[str, str]:
    """Relationship id → absolute part name, for the part at `path`."""
    rels_path = posixpath.join(posixpath.dirname(path), "_rels", posixpath.basename(path) + ".rels")
    data = parts.read(rels_path, required=False)
    if data is None:
        return {}
    out = {}
    for rel in _xml(data).findall(PKG_REL + "Relationship"):
        if rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target") or ""
        if target.startswith("/"):
            resolved = target.lstrip("/")
        else:
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
        out[rel.get("Id") or ""] = resolved
    return out


# Entry point ----------------------------------------------------------------------------------


IWORK_PREVIEWS = ["preview.jpg", "quicklook/thumbnail.jpg", "preview-web.jpg", "preview-micro.jpg"]
IWORK_MARKERS = ["index/document.iwa", "index.xml", "index.xml.gz", "index.apxl", "index.apxl.gz"]
ODF_KINDS = {
    "application/vnd.oasis.opendocument.text": DocKind.ODF_TEXT,
    "application/vnd.oasis.opendocument.text-template": DocKind.ODF_TEXT,
    "application/vnd.oasis.opendocument.spreadsheet": DocKind.ODF_SHEET,
    "application/vnd.oasis.opendocument.spreadsheet-template": DocKind.ODF_SHEET,
    "application/vnd.oasis.opendocument.presentation": DocKind.ODF_SLIDES,
    "application/vnd.oasis.opendocument.presentation-template": DocKind.ODF_SLIDES,
}


def load_office(
    source: FileSource,
    rel: str,
    entry: Entry,
    cancel: CancelToken | None = None,
    limits: OfficeLimits | None = None,
) -> OfficeDocument:
    """Open an Office/OpenDocument/iWork file and parse what the preview needs."""
    limits = limits or OfficeLimits()
    raw = RangeReader(source, rel, entry.size or 0, cancel)
    try:
        with (
            io.BufferedReader(raw, buffer_size=64 * 1024) as buffered,
            zipfile.ZipFile(buffered) as zf,
        ):
            parts = _ZipParts(zf, limits, cancel)
            doc = _parse(classify(parts), parts, limits)
    except zipfile.BadZipFile as exc:
        raise NotOfficeDocument(f"not a zip file ({exc})") from exc
    doc.bytes_read = raw.bytes_read
    return doc


def classify(parts: _Parts) -> DocKind:
    """Which kind of document this is, from the zip's contents."""
    names = parts.names()
    if "word/document.xml" in names:
        return DocKind.WORD
    if "xl/workbook.xml" in names:
        return DocKind.SHEET
    if "ppt/presentation.xml" in names:
        return DocKind.SLIDES
    if "mimetype" in names and "content.xml" in names:
        mimetype = (parts.read("mimetype") or b"").decode("ascii", "replace").strip()
        if mimetype in ODF_KINDS:
            return ODF_KINDS[mimetype]
    if any(m in names for m in IWORK_MARKERS) or any(p in names for p in IWORK_PREVIEWS):
        if any(p in names for p in IWORK_PREVIEWS):
            return DocKind.IWORK
        raise NotOfficeDocument("this iWork file has no preview image inside")
    raise NotOfficeDocument("not an Office, OpenDocument or iWork file")


def _parse(kind: DocKind, parts: _Parts, limits: OfficeLimits) -> OfficeDocument:
    content: Content
    match kind:
        case DocKind.WORD:
            content = _parse_word(parts, limits)
            meta = _ooxml_meta(parts)
        case DocKind.SHEET:
            content = _parse_workbook(parts, limits)
            meta = _ooxml_meta(parts)
            meta.append(MetaRow("Document", "Sheets", f"{content.total_sheets:,}"))
        case DocKind.SLIDES:
            content = _parse_presentation(parts, limits)
            meta = _ooxml_meta(parts)
            if not any(r.label == "Slides" for r in meta):
                meta.append(MetaRow("Document", "Slides", f"{content.total_slides:,}"))
        case DocKind.ODF_TEXT | DocKind.ODF_SHEET | DocKind.ODF_SLIDES:
            content = _parse_odf(kind, parts, limits)
            meta = _odf_meta(parts)
        case DocKind.IWORK:
            name = next(p for p in IWORK_PREVIEWS if p in parts.names())
            image = parts.read(name)
            assert image is not None
            content = ImageContent(image, name)
            meta = []
    return OfficeDocument(kind, content, meta, limits, dict(parts.cache()))


# Word (docx) ----------------------------------------------------------------------------------

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_HEADING = re.compile(r"^heading\s*(\d)$", re.IGNORECASE)
_RUN_CONTAINERS = {
    W + t
    for t in (
        "hyperlink",
        "ins",
        "smartTag",
        "sdt",
        "sdtContent",
        "fldSimple",
        "customXml",
        "moveTo",
    )
}


def _on(el: ET.Element | None) -> bool:
    """A w:b / w:i toggle: present and not explicitly switched off."""
    return el is not None and el.get(W + "val", "true").lower() not in ("0", "false", "off")


def _style_kind(name: str, outline: int | None) -> str:
    lowered = name.strip().lower()
    if lowered == "title":
        return "title"
    if lowered == "subtitle":
        return "subtitle"
    if m := _HEADING.match(lowered):
        return f"h{min(int(m.group(1)), 6)}"
    if outline is not None and outline < 9:
        return f"h{min(outline + 1, 6)}"
    return "p"


def _word_styles(data: bytes | None) -> dict[str, str]:
    """styleId → paragraph kind ("p", "title", "h1"...)."""
    if data is None:
        return {}
    out = {}
    for style in _xml(data).findall(W + "style"):
        sid = style.get(W + "styleId")
        if not sid:
            continue
        name_el = style.find(W + "name")
        outline_el = style.find(f"{W}pPr/{W}outlineLvl")
        outline = int(outline_el.get(W + "val", "9")) if outline_el is not None else None
        out[sid] = _style_kind(name_el.get(W + "val", sid) if name_el is not None else sid, outline)
    return out


def _word_numbering(data: bytes | None) -> dict[tuple[str, int], str]:
    """(numId, ilvl) → number format ("decimal", "bullet", "lowerLetter", …)."""
    if data is None:
        return {}
    root = _xml(data)
    abstract: dict[str, dict[int, str]] = {}
    for an in root.findall(W + "abstractNum"):
        levels = {}
        for lvl in an.findall(W + "lvl"):
            fmt = lvl.find(W + "numFmt")
            levels[int(lvl.get(W + "ilvl", "0"))] = (
                fmt.get(W + "val", "bullet") if fmt is not None else "bullet"
            )
        abstract[an.get(W + "abstractNumId", "")] = levels
    out = {}
    for num in root.findall(W + "num"):
        ref = num.find(W + "abstractNumId")
        levels = abstract.get(ref.get(W + "val", "") if ref is not None else "", {})
        for ilvl, fmt in levels.items():
            out[(num.get(W + "numId", ""), ilvl)] = fmt
    return out


def _marker(fmt: str, n: int) -> str:
    match fmt:
        case "decimal" | "decimalZero":
            return f"{n}."
        case "lowerLetter":
            return f"{_letters(n).lower()}."
        case "upperLetter":
            return f"{_letters(n)}."
        case "lowerRoman":
            return f"{_roman(n).lower()}."
        case "upperRoman":
            return f"{_roman(n)}."
        case _:
            return "•"


def _letters(n: int) -> str:
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out or "A"


def _roman(n: int) -> str:
    vals = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"),
            (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]  # fmt: skip
    out = ""
    for v, s in vals:
        while n >= v:
            out += s
            n -= v
    return out or "I"


def _word_runs(el: ET.Element) -> list[ET.Element]:
    """Runs of a paragraph, in order, through hyperlinks/insertions/content controls."""
    runs: list[ET.Element] = []
    for child in el:
        if child.tag == W + "r":
            runs.append(child)
        elif child.tag in _RUN_CONTAINERS:
            runs.extend(_word_runs(child))
    return runs


def _run_text(run: ET.Element) -> tuple[str, bool]:
    """(text, ends with a page break)."""
    parts: list[str] = []
    page_break = False
    for child in run:
        tag = child.tag
        if tag == W + "t":
            parts.append(child.text or "")
        elif tag == W + "tab":
            parts.append("\t")
        elif tag in (W + "br", W + "cr"):
            if child.get(W + "type") == "page":
                page_break = True
            else:
                parts.append("\n")
        elif tag == W + "noBreakHyphen":
            parts.append("-")
    return "".join(parts), page_break


def _word_paragraph(
    p: ET.Element,
    styles: dict[str, str],
    numbering: dict[tuple[str, int], str],
    counters: dict[str, list[int]],
) -> tuple[Paragraph, bool, bool]:
    """(paragraph, page break before, page break after)."""
    ppr = p.find(W + "pPr")
    style = "p"
    level: int | None = None
    marker = "•"
    before = False
    if ppr is not None:
        sid = ppr.find(W + "pStyle")
        if sid is not None:
            sval = sid.get(W + "val", "")
            style = styles.get(sval) or _style_kind(sval, None)
        before = _on(ppr.find(W + "pageBreakBefore"))
        num = ppr.find(W + "numPr")
        if num is not None:
            num_id_el, ilvl_el = num.find(W + "numId"), num.find(W + "ilvl")
            num_id = num_id_el.get(W + "val", "0") if num_id_el is not None else "0"
            if num_id != "0":
                level = int(ilvl_el.get(W + "val", "0")) if ilvl_el is not None else 0
                levels = counters.setdefault(num_id, [0] * 10)
                level = max(0, min(level, 9))
                levels[level] += 1
                for deeper in range(level + 1, 10):
                    levels[deeper] = 0
                marker = _marker(numbering.get((num_id, level), "bullet"), levels[level])
    runs: list[Run] = []
    after = False
    for r in _word_runs(p):
        text, brk = _run_text(r)
        after |= brk
        if not text:
            continue
        rpr = r.find(W + "rPr")
        bold = _on(rpr.find(W + "b")) if rpr is not None else False
        italic = _on(rpr.find(W + "i")) if rpr is not None else False
        if runs and runs[-1].bold == bold and runs[-1].italic == italic:
            runs[-1] = Run(runs[-1].text + text, bold, italic)  # merge like-formatted runs
        else:
            runs.append(Run(text, bold, italic))
    if style != "p" and level is not None:
        level = None  # numbered headings read better as headings
    return Paragraph(tuple(runs), style, level, marker), before, after


def _cell_text(cell: ET.Element) -> str:
    lines = []
    for p in cell.iter(W + "p"):
        lines.append("".join(_run_text(r)[0] for r in _word_runs(p)))
    return "\n".join(lines).strip()


def _word_table(tbl: ET.Element, limits: OfficeLimits) -> Table:
    rows: list[tuple[str, ...]] = []
    all_rows = tbl.findall(W + "tr")
    truncated = len(all_rows) > limits.table_rows
    for tr in all_rows[: limits.table_rows]:
        cells = tr.findall(W + "tc")
        truncated |= len(cells) > limits.table_cols
        rows.append(tuple(_cell_text(tc) for tc in cells[: limits.table_cols]))
    return Table(tuple(rows), truncated)


def _append_paragraph(content: TextContent, para: Paragraph) -> None:
    # Collapse runs of empty paragraphs (Word uses them for spacing) into one gap.
    if not para.text.strip() and para.list_level is None:
        last = content.blocks[-1] if content.blocks else None
        if (
            last is None
            or isinstance(last, PageBreak)
            or (isinstance(last, Paragraph) and not last.text.strip())
        ):
            return
    content.blocks.append(para)


def _parse_word(parts: _Parts, limits: OfficeLimits) -> TextContent:
    styles = _word_styles(parts.read("word/styles.xml", required=False))
    numbering = _word_numbering(parts.read("word/numbering.xml", required=False))
    data = parts.read("word/document.xml")
    assert data is not None
    content = TextContent()
    counters: dict[str, list[int]] = {}
    depth = 0
    for event, el in _iterparse(data):
        if event == "start":
            depth += 1
            continue
        depth -= 1
        # document (1) > body (2) > block: a block element ends when depth drops back to 2.
        if depth != 2 or el.tag not in (W + "p", W + "tbl"):
            continue
        content.total += 1
        if content.shown < limits.blocks:
            content.shown += 1
            if el.tag == W + "tbl":
                content.blocks.append(_word_table(el, limits))
            else:
                para, before, after = _word_paragraph(el, styles, numbering, counters)
                if before and content.blocks:
                    content.blocks.append(PageBreak())
                _append_paragraph(content, para)
                if after:
                    content.blocks.append(PageBreak())
        el.clear()
    while content.blocks and isinstance(content.blocks[-1], PageBreak):
        content.blocks.pop()
    return content


# Excel (xlsx) ---------------------------------------------------------------------------------

S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_CELL_REF = re.compile(r"^([A-Za-z]+)(\d+)$")
_BUILTIN_DATE_FORMATS = set(range(14, 23)) | {45, 46, 47}
_DATE_CODE = re.compile(r"[dy]|m{3,}", re.IGNORECASE)


def column_letters(index: int) -> str:
    """0 → A, 25 → Z, 26 → AA."""
    return _letters(index + 1)


def _column_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _date_styles(data: bytes | None) -> set[int]:
    """Indexes into cellXfs whose number format is a date."""
    if data is None:
        return set()
    root = _xml(data)
    custom = {}
    fmts = root.find(S + "numFmts")
    if fmts is not None:
        for f in fmts.findall(S + "numFmt"):
            code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", f.get("formatCode", ""))
            custom[int(f.get("numFmtId", "0"))] = bool(_DATE_CODE.search(code))
    out = set()
    xfs = root.find(S + "cellXfs")
    if xfs is not None:
        for i, xf in enumerate(xfs.findall(S + "xf")):
            fid = int(xf.get("numFmtId", "0"))
            if fid in _BUILTIN_DATE_FORMATS or custom.get(fid, False):
                out.add(i)
    return out


def _shared_strings(data: bytes | None) -> list[str]:
    if data is None:
        return []
    out: list[str] = []
    for _event, el in _iterparse(data, ("end",)):
        if el.tag == S + "si":
            out.append(_rich_text(el))
            el.clear()
    return out


def _rich_text(el: ET.Element) -> str:
    """Text of an si / is element: plain t, or r/t runs; phonetic runs (rPh) are skipped."""
    direct = el.find(S + "t")
    if direct is not None:
        return direct.text or ""
    parts = []
    for run in el.findall(S + "r"):
        t = run.find(S + "t")
        if t is not None:
            parts.append(t.text or "")
    return "".join(parts)


def _number(text: str) -> str:
    try:
        x = float(text)
    except ValueError:
        return text
    if x.is_integer() and abs(x) < 1e15:
        return str(int(x))
    return f"{x:.10g}"


def _excel_date(serial: str, date1904: bool) -> str:
    try:
        days = float(serial)
        base = dt.datetime(1904, 1, 1) if date1904 else dt.datetime(1899, 12, 30)
        when = base + dt.timedelta(days=days)
    except ValueError, OverflowError:
        return serial
    if days.is_integer():
        return when.strftime("%Y-%m-%d")
    return when.strftime("%Y-%m-%d %H:%M")


def _parse_sheet(
    name: str,
    data: bytes,
    shared: list[str],
    date_styles: set[int],
    date1904: bool,
    limits: OfficeLimits,
) -> Sheet:
    sheet = Sheet(name)
    current: dict[int, str] = {}
    row_number = 0
    col_cursor = 0
    widest = 0
    for event, el in _iterparse(data):
        tag = el.tag
        if event == "start":
            if tag == S + "row":
                row_number = int(el.get("r") or row_number + 1)
                col_cursor = 0
                current = {}
            continue
        if tag == S + "c":
            ref = el.get("r")
            m = _CELL_REF.match(ref) if ref else None
            col = _column_index(m.group(1)) if m else col_cursor
            col_cursor = col + 1
            value = _cell_value(el, shared, date_styles, date1904)
            if value != "":
                sheet.max_col = max(sheet.max_col, col + 1)
                if col < limits.cols:
                    current[col] = value
            el.clear()
        elif tag == S + "row":
            if current:
                sheet.total_rows += 1
                if len(sheet.rows) < limits.rows:
                    widest = max(widest, max(current) + 1)
                    sheet.rows.append(
                        (row_number, [current.get(i, "") for i in range(max(current) + 1)])
                    )
            el.clear()
    sheet.columns = min(widest, limits.cols)
    sheet.rows = [(n, cells + [""] * (sheet.columns - len(cells))) for n, cells in sheet.rows]
    return sheet


def _cell_value(c: ET.Element, shared: list[str], date_styles: set[int], date1904: bool) -> str:
    kind = c.get("t", "n")
    v = c.find(S + "v")
    text = v.text if v is not None and v.text is not None else ""
    match kind:
        case "s":
            try:
                return shared[int(text)]
            except ValueError, IndexError:
                return ""
        case "inlineStr":
            is_ = c.find(S + "is")
            return _rich_text(is_) if is_ is not None else ""
        case "b":
            return "TRUE" if text == "1" else "FALSE"
        case "e" | "str":
            return text
        case _:
            if not text:
                return ""
            if int(c.get("s", "0") or 0) in date_styles:
                return _excel_date(text, date1904)
            return _number(text)


def _parse_workbook(parts: _Parts, limits: OfficeLimits) -> SheetContent:
    workbook = _xml(parts.read("xl/workbook.xml") or b"")
    pr = workbook.find(S + "workbookPr")
    date1904 = pr is not None and pr.get("date1904", "0").lower() in ("1", "true")
    rels = _rels(parts, "xl/workbook.xml")
    shared = _shared_strings(parts.read("xl/sharedStrings.xml", required=False))
    date_styles = _date_styles(parts.read("xl/styles.xml", required=False))
    sheets_el = workbook.find(S + "sheets")
    entries = sheets_el.findall(S + "sheet") if sheets_el is not None else []
    content = SheetContent(total_sheets=len(entries))
    for sheet_el in entries[: limits.sheets]:
        name = sheet_el.get("name", "Sheet")
        path = rels.get(sheet_el.get(DOC_REL + "id", ""))
        hidden = sheet_el.get("state", "visible") != "visible"
        try:
            data = parts.read(path, required=False) if path else None
        except OfficeTooLarge as exc:
            content.sheets.append(Sheet(name, hidden=hidden, note=str(exc)))
            continue
        if data is None:
            content.sheets.append(
                Sheet(
                    name, hidden=hidden, note="This sheet has no cell data (chart or macro sheet)."
                )
            )
            continue
        sheet = _parse_sheet(name, data, shared, date_styles, date1904, limits)
        sheet.hidden = hidden
        content.sheets.append(sheet)
    return content


# PowerPoint (pptx) ----------------------------------------------------------------------------

P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
THUMBNAILS = ["docprops/thumbnail.jpeg", "docprops/thumbnail.jpg", "docprops/thumbnail.png"]


def _drawing_paragraph(p: ET.Element) -> tuple[int, str]:
    ppr = p.find(A + "pPr")
    level = int(ppr.get("lvl", "0")) if ppr is not None else 0
    parts = []
    for child in p:
        if child.tag in (A + "r", A + "fld"):
            t = child.find(A + "t")
            parts.append(t.text or "" if t is not None else "")
        elif child.tag == A + "br":
            parts.append(" ")
    return level, "".join(parts).strip()


def _parse_slide(number: int, data: bytes) -> Slide:
    slide = Slide(number)
    root = _xml(data)
    for sp in root.iter(P + "sp"):
        body = sp.find(P + "txBody")
        if body is None:
            continue
        ph = sp.find(f"{P}nvSpPr/{P}nvPr/{P}ph")
        is_title = ph is not None and ph.get("type") in ("title", "ctrTitle")
        lines = [_drawing_paragraph(p) for p in body.findall(A + "p")]
        lines = [(lvl, text) for lvl, text in lines if text]
        if is_title and not slide.title:
            slide.title = " ".join(text for _, text in lines)
        else:
            slide.bullets.extend(lines)
    for tbl in root.iter(A + "tbl"):
        for tr in tbl.findall(A + "tr"):
            cells = [
                " ".join(_drawing_paragraph(p)[1] for p in tc.iter(A + "p")).strip()
                for tc in tr.findall(A + "tc")
            ]
            if any(cells):
                slide.bullets.append((0, " | ".join(cells)))
    return slide


def _thumbnail(parts: _Parts, names: list[str]) -> bytes | None:
    for name in names:
        if name in parts.names():
            try:
                return parts.read(name, required=False)
            except OfficeTooLarge:
                return None
    return None


def _parse_presentation(parts: _Parts, limits: OfficeLimits) -> SlidesContent:
    pres = _xml(parts.read("ppt/presentation.xml") or b"")
    rels = _rels(parts, "ppt/presentation.xml")
    lst = pres.find(P + "sldIdLst")
    ids = [el.get(DOC_REL + "id", "") for el in lst.findall(P + "sldId")] if lst is not None else []
    content = SlidesContent(total_slides=len(ids), thumbnail=_thumbnail(parts, THUMBNAILS))
    for number, rid in enumerate(ids[: limits.slides], start=1):
        path = rels.get(rid)
        data = parts.read(path, required=False) if path else None
        content.slides.append(_parse_slide(number, data) if data else Slide(number))
    return content


# OpenDocument (odt/ods/odp) -------------------------------------------------------------------

_NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "presentation": "urn:oasis:names:tc:opendocument:xmlns:presentation:1.0",
    "meta": "urn:oasis:names:tc:opendocument:xmlns:meta:1.0",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def _q(prefixed: str) -> str:
    prefix, local = prefixed.split(":")
    return f"{{{_NS[prefix]}}}{local}"


def _odf_text(el: ET.Element) -> str:
    """Text of an ODF element, honouring text:s (spaces), text:tab and text:line-break."""
    out = [el.text or ""]
    for child in el:
        tag = child.tag
        if tag == _q("text:s"):
            out.append(" " * int(child.get(_q("text:c"), "1")))
        elif tag == _q("text:tab"):
            out.append("\t")
        elif tag == _q("text:line-break"):
            out.append("\n")
        elif tag in (_q("text:note"), _q("office:annotation")):
            pass
        else:
            out.append(_odf_text(child))
        out.append(child.tail or "")
    return "".join(out)


def _odf_blocks(
    parent: ET.Element, content: TextContent, limits: OfficeLimits, level: int | None = None
) -> None:
    for el in parent:
        if content.shown >= limits.blocks and el.tag in (
            _q("text:p"),
            _q("text:h"),
            _q("table:table"),
        ):
            content.total += 1
            continue
        tag = el.tag
        if tag == _q("text:h"):
            content.total += 1
            content.shown += 1
            outline = int(el.get(_q("text:outline-level"), "1"))
            _append_paragraph(content, Paragraph((Run(_odf_text(el)),), f"h{min(outline, 6)}"))
        elif tag == _q("text:p"):
            content.total += 1
            content.shown += 1
            text = _odf_text(el)
            _append_paragraph(content, Paragraph((Run(text),), "p", level))
        elif tag == _q("text:list"):
            next_level = 0 if level is None else level + 1
            for item in el:
                if item.tag in (_q("text:list-item"), _q("text:list-header")):
                    _odf_blocks(item, content, limits, next_level)
        elif tag == _q("table:table"):
            content.total += 1
            content.shown += 1
            rows, truncated, _total, _widest = _odf_table_rows(
                el, limits.table_rows, limits.table_cols
            )
            content.blocks.append(Table(tuple(tuple(r) for _, r in rows), truncated))
        elif tag in (_q("text:section"), _q("text:soft-page-break")):
            _odf_blocks(el, content, limits, level)


def _odf_cell_value(cell: ET.Element) -> str:
    # text:p holds the cell as displayed (formatted numbers, currencies, dates): prefer it.
    shown = "\n".join(_odf_text(p) for p in cell.findall(_q("text:p")))
    if shown:
        return shown
    vtype = cell.get(_q("office:value-type"))
    if vtype in ("float", "percentage", "currency"):
        value = cell.get(_q("office:value"))
        if value is not None:
            return _number(value)
    if vtype == "date":
        return (cell.get(_q("office:date-value")) or "").replace("T", " ")
    if vtype == "boolean":
        return "TRUE" if cell.get(_q("office:boolean-value")) == "true" else "FALSE"
    return ""


def _odf_table_rows(
    table: ET.Element, max_rows: int, max_cols: int
) -> tuple[list[tuple[int, list[str]]], bool, int, int]:
    """((row number, cells) for non-empty rows, truncated, non-empty row count, widest row)."""
    rows: list[tuple[int, list[str]]] = []
    row_number = 0
    total = 0
    widest = 0
    truncated = False
    row_elements = [r for r in table.iter(_q("table:table-row"))]
    for row in row_elements:
        repeat = min(int(row.get(_q("table:number-rows-repeated"), "1")), 1_000_000)
        cells: list[str] = []
        col = 0
        for cell in row:
            if cell.tag not in (_q("table:table-cell"), _q("table:covered-table-cell")):
                continue
            span = min(int(cell.get(_q("table:number-columns-repeated"), "1")), 16_384)
            value = _odf_cell_value(cell)
            if value:
                for _ in range(span):
                    if col < max_cols:
                        cells.extend([""] * (col - len(cells)))
                        cells.append(value)
                    col += 1
                widest = max(widest, col)
            else:
                col += span
        if not any(cells):
            row_number += repeat
            continue
        for _ in range(min(repeat, max_rows + 1)):
            row_number += 1
            total += 1
            if len(rows) < max_rows:
                rows.append((row_number, list(cells)))
            else:
                truncated = True
        row_number += max(0, repeat - (max_rows + 1))
    return rows, truncated, total, widest


def _parse_odf(kind: DocKind, parts: _Parts, limits: OfficeLimits) -> Content:
    root = _xml(parts.read("content.xml") or b"")
    body = root.find(_q("office:body"))
    if body is None:
        raise NotOfficeDocument("content.xml has no body")
    match kind:
        case DocKind.ODF_TEXT:
            text = body.find(_q("office:text"))
            content = TextContent()
            if text is not None:
                _odf_blocks(text, content, limits)
            return content
        case DocKind.ODF_SHEET:
            spreadsheet = body.find(_q("office:spreadsheet"))
            tables = spreadsheet.findall(_q("table:table")) if spreadsheet is not None else []
            sheets = SheetContent(total_sheets=len(tables))
            for table in tables[: limits.sheets]:
                rows, _truncated, total, widest = _odf_table_rows(table, limits.rows, limits.cols)
                columns = min(widest, limits.cols)
                sheets.sheets.append(
                    Sheet(
                        table.get(_q("table:name"), "Sheet"),
                        [(n, cells + [""] * (columns - len(cells))) for n, cells in rows],
                        columns=columns,
                        total_rows=total,
                        max_col=widest,
                    )
                )
            return sheets
        case _:
            presentation = body.find(_q("office:presentation"))
            pages = presentation.findall(_q("draw:page")) if presentation is not None else []
            slides = SlidesContent(
                total_slides=len(pages),
                thumbnail=_thumbnail(parts, ["thumbnails/thumbnail.png"]),
            )
            for number, page in enumerate(pages[: limits.slides], start=1):
                slide = Slide(number)
                for frame in page.iter(_q("draw:frame")):
                    role = frame.get(_q("presentation:class"))
                    lines = _odf_frame_lines(frame)
                    if role == "title" and not slide.title:
                        slide.title = " ".join(t for _, t in lines)
                    elif role not in ("notes", "page-number", "date-time", "footer"):
                        slide.bullets.extend(lines)
                slides.slides.append(slide)
            return slides


def _odf_frame_lines(frame: ET.Element) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []

    def walk(el: ET.Element, level: int) -> None:
        for child in el:
            if child.tag in (_q("text:p"), _q("text:h")):
                text = _odf_text(child).strip()
                if text:
                    lines.append((level, text))
            elif child.tag == _q("text:list"):
                for item in child:
                    walk(item, level + (1 if lines or level else 0))
            elif child.tag in (_q("draw:text-box"), _q("text:list-item")):
                walk(child, level)

    walk(frame, 0)
    return lines


# Metadata -------------------------------------------------------------------------------------

_CP = "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_DCTERMS = "{http://purl.org/dc/terms/}"
_APP = "{http://schemas.openxmlformats.org/officeDocument/2006/extended-properties}"


def _when(text: str | None) -> str | None:
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.strip().replace("Z", "+00:00")).strftime(
            "%Y-%m-%d %H:%M"
        )
    except ValueError:
        return text.strip()


def _rows(pairs: list[tuple[str, str | None]]) -> list[MetaRow]:
    return [
        MetaRow("Document", label, value.strip())
        for label, value in pairs
        if value and value.strip()
    ]


def _ooxml_meta(parts: _Parts) -> list[MetaRow]:
    pairs: list[tuple[str, str | None]] = []
    core = parts.read("docProps/core.xml", required=False)
    if core is not None:
        root = _xml(core)

        def text(tag: str) -> str | None:
            el = root.find(tag)
            return el.text if el is not None else None

        pairs += [
            ("Title", text(_DC + "title")),
            ("Subject", text(_DC + "subject")),
            ("Author", text(_DC + "creator")),
            ("Last modified by", text(_CP + "lastModifiedBy")),
            ("Created", _when(text(_DCTERMS + "created"))),
            ("Modified", _when(text(_DCTERMS + "modified"))),
        ]
    app = parts.read("docProps/app.xml", required=False)
    if app is not None:
        root = _xml(app)

        def app_text(tag: str) -> str | None:
            el = root.find(_APP + tag)
            return el.text if el is not None else None

        application = app_text("Application")
        version = app_text("AppVersion")
        pairs += [
            ("Application", f"{application} {version}" if application and version else application),
            ("Pages", app_text("Pages")),
            ("Words", _number(app_text("Words") or "") or None),
            ("Slides", app_text("Slides")),
        ]
    return _rows(pairs)


def _odf_meta(parts: _Parts) -> list[MetaRow]:
    data = parts.read("meta.xml", required=False)
    if data is None:
        return []
    meta = _xml(data).find(f"{_q('office:meta')}")
    if meta is None:
        return []

    def text(tag: str) -> str | None:
        el = meta.find(_q(tag))
        return el.text if el is not None else None

    stats = meta.find(_q("meta:document-statistic"))
    stat = lambda a: stats.get(_q(a)) if stats is not None else None  # noqa: E731
    return _rows(
        [
            ("Title", text("dc:title")),
            ("Subject", text("dc:subject")),
            ("Author", text("meta:initial-creator")),
            ("Last modified by", text("dc:creator")),
            ("Created", _when(text("meta:creation-date"))),
            ("Modified", _when(text("dc:date"))),
            ("Application", text("meta:generator")),
            ("Pages", stat("meta:page-count")),
            ("Words", stat("meta:word-count")),
        ]
    )
