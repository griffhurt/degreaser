# ruff: noqa: E501  (fixture XML reads better unwrapped)
"""Office document previews (core/preview/office.py): detection, parsing, caps, range reads.

The make_* builders write minimal but valid packages; tests/ui/test_preview_office.py reuses them.
"""

import io
import os
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from degreaser.core.preview.detect import Kind, detect_bytes
from degreaser.core.preview.office import (
    DocKind,
    ImageContent,
    NotOfficeDocument,
    OfficeError,
    OfficeLimits,
    OfficeTooLarge,
    PageBreak,
    Paragraph,
    SheetContent,
    SlidesContent,
    Table,
    TextContent,
    column_letters,
    load_office,
)
from degreaser.core.preview.plan import Strategy, plan_preview
from degreaser.core.settings import Downloads
from degreaser.core.sources.base import Entry
from degreaser.core.sources.local import LocalSource

W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
CT = '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
CORE = (
    '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
    ' xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/">'
    "<dc:title>{title}</dc:title><dc:creator>Griffin</dc:creator>"
    "<cp:lastModifiedBy>Someone Else</cp:lastModifiedBy>"
    "<dcterms:created>2014-03-02T10:15:00Z</dcterms:created>"
    "<dcterms:modified>2015-06-01T08:00:00Z</dcterms:modified></cp:coreProperties>"
)
APP = (
    '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
    "<Application>Microsoft Office Word</Application><AppVersion>16.0000</AppVersion>{extra}</Properties>"
)
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _zip(path: Path, parts: dict[str, bytes | str], stored: tuple[str, ...] = ()) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            raw = data.encode() if isinstance(data, str) else data
            compress = zipfile.ZIP_STORED if name in stored else zipfile.ZIP_DEFLATED
            zf.writestr(zipfile.ZipInfo(name), raw, compress_type=compress)
    return path


def _p(text: str, style: str = "", num: tuple[int, int] | None = None, runs: str = "") -> str:
    ppr = ""
    if style or num:
        inner = f'<w:pStyle w:val="{style}"/>' if style else ""
        if num:
            inner += f'<w:numPr><w:ilvl w:val="{num[1]}"/><w:numId w:val="{num[0]}"/></w:numPr>'
        ppr = f"<w:pPr>{inner}</w:pPr>"
    body = runs or (f"<w:r><w:t xml:space='preserve'>{text}</w:t></w:r>" if text else "")
    return f"<w:p>{ppr}{body}</w:p>"


def word_body(paragraphs: int = 0) -> str:
    blocks = [
        _p("Old Laptop Notes", "Title"),
        _p("Introduction", "Heading1"),
        _p(
            "",
            runs="<w:r><w:t xml:space='preserve'>Plain, </w:t></w:r>"
            "<w:r><w:rPr><w:b/></w:rPr><w:t>bold</w:t></w:r>"
            "<w:r><w:rPr><w:b w:val='0'/><w:i/></w:rPr><w:t xml:space='preserve'> and italic</w:t></w:r>"
            "<w:hyperlink><w:r><w:t xml:space='preserve'> link</w:t></w:r></w:hyperlink>",
        ),
        _p("", ""),
        _p("", ""),
        _p("Bullet one", num=(1, 0)),
        _p("Nested bullet", num=(1, 1)),
        _p("Step one", num=(2, 0)),
        _p("Step two", num=(2, 0)),
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Name</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Size</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>a.txt</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>4 KB</w:t></w:r></w:p></w:tc></w:tr></w:tbl>",
        _p("", runs="<w:r><w:t>Before the break</w:t><w:br w:type='page'/></w:r>"),
        _p("After the break"),
    ]
    blocks += [_p(f"Filler paragraph {i}") for i in range(paragraphs)]
    return f"<w:document {W_NS}><w:body>{''.join(blocks)}<w:sectPr/></w:body></w:document>"


STYLES = (
    f"<w:styles {W_NS}>"
    '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
    "</w:styles>"
)
NUMBERING = (
    f"<w:numbering {W_NS}>"
    '<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/></w:lvl>'
    '<w:lvl w:ilvl="1"><w:numFmt w:val="bullet"/></w:lvl></w:abstractNum>'
    '<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/></w:lvl></w:abstractNum>'
    '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
    '<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>'
    "</w:numbering>"
)


def make_docx(path: Path, paragraphs: int = 0, extra: dict[str, bytes | str] | None = None) -> Path:
    parts: dict[str, bytes | str] = {
        "[Content_Types].xml": CT,
        "word/document.xml": word_body(paragraphs),
        "word/styles.xml": STYLES,
        "word/numbering.xml": NUMBERING,
        "docProps/core.xml": CORE.format(title="Laptop notes"),
        "docProps/app.xml": APP.format(extra="<Pages>3</Pages><Words>1234</Words>"),
    }
    parts.update(extra or {})
    return _zip(path, parts, stored=tuple((extra or {}).keys()))


S_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
R_NS = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def _sheet(rows: list[str]) -> str:
    return f"<worksheet {S_NS}><sheetData>{''.join(rows)}</sheetData></worksheet>"


def make_xlsx(path: Path, extra_rows: int = 0) -> Path:
    sheet1 = [
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c></row>',
        '<row r="2"><c r="A2" t="inlineStr"><is><t>Camera</t></is></c><c r="B2"><v>3.5</v></c>'
        '<c r="C2" s="1"><v>45000</v></c><c r="E2" t="b"><v>1</v></c></row>',
        '<row r="4"><c r="A4" t="s"><v>3</v></c><c r="B4"><v>12</v></c><c r="C4" t="e"><v>#DIV/0!</v></c></row>',
    ]
    sheet1 += [f'<row r="{5 + i}"><c r="A{5 + i}"><v>{i}</v></c></row>' for i in range(extra_rows)]
    parts: dict[str, bytes | str] = {
        "[Content_Types].xml": CT,
        "xl/workbook.xml": f"<workbook {S_NS} {R_NS}><sheets>"
        '<sheet name="Inventory" sheetId="1" r:id="rId1"/>'
        '<sheet name="Notes" sheetId="2" r:id="rId2" state="hidden"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{REL_NS}">'
        '<Relationship Id="rId1" Type="x" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="x" Target="/xl/worksheets/sheet2.xml"/></Relationships>',
        "xl/sharedStrings.xml": f"<sst {S_NS}><si><t>Item</t></si><si><t>Price</t></si>"
        "<si><t>Bought</t></si><si><r><t>Rich </t></r><r><t>text</t></r><rPh><t>x</t></rPh></si></sst>",
        "xl/styles.xml": f'<styleSheet {S_NS}><cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>',
        "xl/worksheets/sheet1.xml": _sheet(sheet1),
        "xl/worksheets/sheet2.xml": _sheet(
            ['<row r="1"><c r="A1" t="inlineStr"><is><t>hello</t></is></c></row>']
        ),
        "docProps/core.xml": CORE.format(title="Inventory"),
    }
    return _zip(path, parts)


P_NS = (
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" ' + R_NS
)


def _shape(text_paras: list[tuple[int, str]], ph: str | None = None) -> str:
    ph_xml = (
        f'<p:nvSpPr><p:nvPr><p:ph type="{ph}"/></p:nvPr></p:nvSpPr>'
        if ph
        else "<p:nvSpPr><p:nvPr/></p:nvSpPr>"
    )
    paras = "".join(
        f'<a:p><a:pPr lvl="{lvl}"/><a:r><a:t>{t}</a:t></a:r></a:p>' for lvl, t in text_paras
    )
    return f"<p:sp>{ph_xml}<p:txBody>{paras}</p:txBody></p:sp>"


def jpeg(size: tuple[int, int] = (64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (80, 90, 200)).save(buf, "JPEG")
    return buf.getvalue()


def make_pptx(path: Path, slides: int = 2, thumbnail: bool = True) -> Path:
    # sldIdLst order differs from the file names: slide order must come from the list.
    ids = "".join(f'<p:sldId id="{256 + i}" r:id="rId{i + 1}"/>' for i in range(slides))
    rels = "".join(
        f'<Relationship Id="rId{i + 1}" Type="x" Target="slides/slide{slides - i}.xml"/>'
        for i in range(slides)
    )
    parts: dict[str, bytes | str] = {
        "[Content_Types].xml": CT,
        "ppt/presentation.xml": f"<p:presentation {P_NS}><p:sldIdLst>{ids}</p:sldIdLst></p:presentation>",
        "ppt/_rels/presentation.xml.rels": f'<Relationships xmlns="{REL_NS}">{rels}</Relationships>',
        "docProps/app.xml": APP.format(extra=f"<Slides>{slides}</Slides>"),
    }
    for i in range(slides):
        number = slides - i  # file name
        position = i + 1  # place in the deck
        body = _shape([(0, f"Title {position}")], "title" if position > 1 else "ctrTitle")
        body += _shape([(0, f"Point A{position}"), (1, f"Detail {position}"), (0, "")])
        if position == 2:
            body += (
                "<p:graphicFrame><a:graphic><a:graphicData><a:tbl><a:tr><a:tc><a:txBody><a:p><a:r><a:t>Q1</a:t>"
                "</a:r></a:p></a:txBody></a:tc><a:tc><a:txBody><a:p><a:r><a:t>Q2</a:t></a:r></a:p></a:txBody>"
                "</a:tc></a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
            )
        parts[f"ppt/slides/slide{number}.xml"] = (
            f"<p:sld {P_NS}><p:cSld><p:spTree>{body}</p:spTree></p:cSld></p:sld>"
        )
    if thumbnail:
        parts["docProps/thumbnail.jpeg"] = jpeg()
    return _zip(path, parts)


ODF_NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" '
    'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/"'
)


def make_odf(path: Path, kind: str) -> Path:
    mimetype = f"application/vnd.oasis.opendocument.{kind}"
    if kind == "text":
        body = (
            '<office:text><text:h text:outline-level="1">Chapter</text:h>'
            "<text:p>Hello<text:s text:c='3'/>world</text:p>"
            "<text:list><text:list-item><text:p>Item one</text:p></text:list-item></text:list>"
            "<table:table><table:table-row><table:table-cell><text:p>A</text:p></table:table-cell>"
            "<table:table-cell><text:p>B</text:p></table:table-cell></table:table-row></table:table>"
            "</office:text>"
        )
    elif kind == "spreadsheet":
        body = (
            '<office:spreadsheet><table:table table:name="Budget">'
            "<table:table-row><table:table-cell><text:p>Rent</text:p></table:table-cell>"
            '<table:table-cell office:value-type="float" office:value="1200"><text:p>$1,200.00</text:p></table:table-cell>'
            '<table:table-cell table:number-columns-repeated="16000"/></table:table-row>'
            '<table:table-row table:number-rows-repeated="1000000"><table:table-cell/></table:table-row>'
            "</table:table></office:spreadsheet>"
        )
    else:
        body = (
            "<office:presentation><draw:page draw:name='p1'>"
            '<draw:frame presentation:class="title"><draw:text-box><text:p>Welcome</text:p></draw:text-box></draw:frame>'
            '<draw:frame presentation:class="outline"><draw:text-box><text:list><text:list-item>'
            "<text:p>First point</text:p></text:list-item></text:list></draw:text-box></draw:frame>"
            "</draw:page></office:presentation>"
        )
    content = f"<office:document-content {ODF_NS}><office:body>{body}</office:body></office:document-content>"
    meta = (
        f"<office:document-meta {ODF_NS}><office:meta><dc:title>ODF {kind}</dc:title>"
        "<meta:initial-creator>Griffin</meta:initial-creator><meta:generator>LibreOffice/7.6</meta:generator>"
        '<meta:document-statistic meta:page-count="2" meta:word-count="40"/></office:meta></office:document-meta>'
    )
    return _zip(
        path, {"mimetype": mimetype, "content.xml": content, "meta.xml": meta}, stored=("mimetype",)
    )


def make_iwork(path: Path, preview: bool = True) -> Path:
    parts: dict[str, bytes | str] = {"Index/Document.iwa": b"\x00" * 64}
    if preview:
        parts["preview.jpg"] = jpeg((120, 90))
    return _zip(path, parts)


def load(path: Path, limits: OfficeLimits | None = None):  # type: ignore[no-untyped-def]
    source = LocalSource(path.parent)
    return load_office(source, path.name, source.stat(path.name), limits=limits)


# Detection and plan ---------------------------------------------------------------------------


def test_office_extensions_detect_as_documents(tmp_path: Path) -> None:
    data = make_docx(tmp_path / "a.docx").read_bytes()
    for name, label in (
        ("report.docx", "Word document"),
        ("sheet.xlsx", "Excel workbook"),
        ("deck.pptx", "PowerPoint presentation"),
        ("deck.ppsx", "PowerPoint slide show"),
        ("notes.odt", "OpenDocument text"),
        ("essay.pages", "Pages document"),
    ):
        d = detect_bytes(name, data)
        assert (d.kind, d.label, d.extension_mismatch) == (Kind.DOCUMENT, label, False)
    assert detect_bytes("archive.zip", data).kind == Kind.ZIP
    d = detect_bytes("id_rsa.key", b"-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n")
    assert d.kind == Kind.TEXT and not d.extension_mismatch
    entry = Entry("a.docx", "file", 10**9, None, None)
    plan = plan_preview(detect_bytes("a.docx", data), entry, Downloads(), local=False)
    assert plan.strategy == Strategy.DOCUMENT and plan.confirm is None


def test_column_letters() -> None:
    assert [column_letters(i) for i in (0, 25, 26, 701, 702)] == ["A", "Z", "AA", "ZZ", "AAA"]


# Word ---------------------------------------------------------------------------------------------


def test_docx_structure_and_metadata(tmp_path: Path) -> None:
    doc = load(make_docx(tmp_path / "notes.docx"))
    assert doc.kind is DocKind.WORD and isinstance(doc.content, TextContent)
    blocks = doc.content.blocks
    paras = [b for b in blocks if isinstance(b, Paragraph)]
    assert (paras[0].style, paras[0].text) == ("title", "Old Laptop Notes")
    assert (paras[1].style, paras[1].text) == ("h1", "Introduction")
    runs = paras[2].runs
    assert [(r.text, r.bold, r.italic) for r in runs] == [
        ("Plain, ", False, False),
        ("bold", True, False),
        (" and italic", False, True),
        (" link", False, False),
    ]
    # Two empty paragraphs collapse into one gap.
    assert sum(1 for p in paras if not p.text) == 1
    items = [(p.text, p.list_level, p.list_marker) for p in paras if p.list_level is not None]
    assert items == [
        ("Bullet one", 0, "•"),
        ("Nested bullet", 1, "•"),
        ("Step one", 0, "1."),
        ("Step two", 0, "2."),
    ]
    table = next(b for b in blocks if isinstance(b, Table))
    assert table.rows == (("Name", "Size"), ("a.txt", "4 KB"))
    i = next(i for i, b in enumerate(blocks) if isinstance(b, PageBreak))
    before = blocks[i - 1]
    assert isinstance(before, Paragraph) and before.text == "Before the break"
    assert doc.content.total == doc.content.shown == 12 and not doc.can_expand
    meta = {r.label: r.value for r in doc.metadata}
    assert meta["Title"] == "Laptop notes"
    assert meta["Author"] == "Griffin"
    assert meta["Last modified by"] == "Someone Else"
    assert meta["Created"] == "2014-03-02 10:15"
    assert meta["Application"] == "Microsoft Office Word 16.0000"
    assert (meta["Pages"], meta["Words"]) == ("3", "1234")
    assert all(r.group == "Document" for r in doc.metadata)


def test_docx_paragraph_cap_and_expand_without_rereading(tmp_path: Path) -> None:
    path = make_docx(tmp_path / "long.docx", paragraphs=50)
    doc = load(path, OfficeLimits(blocks=10))
    assert isinstance(doc.content, TextContent)
    assert (doc.content.shown, doc.content.total) == (10, 62)
    assert doc.can_expand
    path.unlink()  # expand() must not touch the source again
    bigger = doc.expand()
    assert isinstance(bigger.content, TextContent)
    assert bigger.content.shown == 50 and bigger.can_expand
    assert bigger.expand().content.shown == 62  # type: ignore[union-attr]


# Excel --------------------------------------------------------------------------------------------


def test_xlsx_sheets_values_and_dates(tmp_path: Path) -> None:
    doc = load(make_xlsx(tmp_path / "inv.xlsx"))
    assert doc.kind is DocKind.SHEET and isinstance(doc.content, SheetContent)
    inv, notes = doc.content.sheets
    assert (inv.name, notes.name, notes.hidden) == ("Inventory", "Notes", True)
    assert inv.columns == 5 and inv.total_rows == 3
    assert inv.rows[0] == (1, ["Item", "Price", "Bought", "", ""])
    assert inv.rows[1] == (2, ["Camera", "3.5", "2023-03-15", "", "TRUE"])
    assert inv.rows[2] == (4, ["Rich text", "12", "#DIV/0!", "", ""])
    assert notes.rows == [(1, ["hello"])]
    assert {r.label: r.value for r in doc.metadata}["Sheets"] == "2"


def test_xlsx_row_cap(tmp_path: Path) -> None:
    doc = load(make_xlsx(tmp_path / "big.xlsx", extra_rows=40), OfficeLimits(rows=10))
    assert isinstance(doc.content, SheetContent)
    sheet = doc.content.sheets[0]
    assert len(sheet.rows) == 10 and sheet.total_rows == 43 and sheet.truncated_rows
    assert doc.can_expand
    assert len(doc.expand().content.sheets[0].rows) == 43  # type: ignore[union-attr]


# PowerPoint ---------------------------------------------------------------------------------------


def test_pptx_slides_in_deck_order_with_thumbnail(tmp_path: Path) -> None:
    doc = load(make_pptx(tmp_path / "deck.pptx"))
    assert doc.kind is DocKind.SLIDES and isinstance(doc.content, SlidesContent)
    first, second = doc.content.slides
    assert (first.number, first.title) == (1, "Title 1")
    assert first.bullets == [(0, "Point A1"), (1, "Detail 1")]
    assert second.title == "Title 2" and (0, "Q1 | Q2") in second.bullets
    assert doc.content.thumbnail is not None and doc.content.thumbnail[:2] == b"\xff\xd8"
    assert {r.label: r.value for r in doc.metadata}["Slides"] == "2"


def test_pptx_slide_cap(tmp_path: Path) -> None:
    doc = load(make_pptx(tmp_path / "deck.pptx", slides=7, thumbnail=False), OfficeLimits(slides=3))
    assert isinstance(doc.content, SlidesContent)
    assert len(doc.content.slides) == 3 and doc.content.total_slides == 7 and doc.can_expand
    assert doc.content.thumbnail is None


# OpenDocument and iWork -----------------------------------------------------------------------


def test_odf_text_sheet_and_presentation(tmp_path: Path) -> None:
    odt = load(make_odf(tmp_path / "a.odt", "text"))
    assert odt.kind is DocKind.ODF_TEXT and isinstance(odt.content, TextContent)
    paras = [b for b in odt.content.blocks if isinstance(b, Paragraph)]
    assert [(p.style, p.text, p.list_level) for p in paras] == [
        ("h1", "Chapter", None),
        ("p", "Hello   world", None),
        ("p", "Item one", 0),
    ]
    assert any(isinstance(b, Table) and b.rows == (("A", "B"),) for b in odt.content.blocks)
    meta = {r.label: r.value for r in odt.metadata}
    assert (meta["Title"], meta["Author"], meta["Pages"]) == ("ODF text", "Griffin", "2")

    ods = load(make_odf(tmp_path / "b.ods", "spreadsheet"))
    assert isinstance(ods.content, SheetContent)
    sheet = ods.content.sheets[0]
    # Huge repeated empty rows/columns don't blow up; the display text wins over the raw value.
    assert sheet.name == "Budget" and sheet.rows == [(1, ["Rent", "$1,200.00"])]

    odp = load(make_odf(tmp_path / "c.odp", "presentation"))
    assert isinstance(odp.content, SlidesContent)
    assert odp.content.slides[0].title == "Welcome"
    assert odp.content.slides[0].bullets == [(0, "First point")]


def test_iwork_preview_image(tmp_path: Path) -> None:
    doc = load(make_iwork(tmp_path / "essay.pages"))
    assert doc.kind is DocKind.IWORK and isinstance(doc.content, ImageContent)
    assert doc.content.image[:2] == b"\xff\xd8"
    with pytest.raises(NotOfficeDocument, match="no preview image"):
        load(make_iwork(tmp_path / "old.pages", preview=False))


# Safety and fallbacks -------------------------------------------------------------------------


def test_zip_bomb_part_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bomb.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CT)
        zf.writestr("word/document.xml", b"\0" * (17 * 1024 * 1024))
    assert path.stat().st_size < 100_000
    with pytest.raises(OfficeTooLarge):
        load(path)


def test_lying_size_header_fails_cleanly(tmp_path: Path) -> None:
    # Under-declare word/document.xml's size in the central directory. zipfile then stops at
    # the declared size and the CRC check fails: an OfficeError, never an unbounded read.
    path = make_docx(tmp_path / "liar.docx")
    data = bytearray(path.read_bytes())
    name = b"word/document.xml"
    at = data.find(b"PK\x01\x02")
    while at != -1:
        name_len = int.from_bytes(data[at + 28 : at + 30], "little")
        if bytes(data[at + 46 : at + 46 + name_len]) == name:
            data[at + 24 : at + 28] = (10).to_bytes(4, "little")  # uncompressed size
            break
        at = data.find(b"PK\x01\x02", at + 4)
    assert at != -1
    path.write_bytes(bytes(data))
    with pytest.raises(OfficeError):
        load(path)


def test_doctype_is_refused(tmp_path: Path) -> None:
    evil = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>' + word_body()
    path = _zip(tmp_path / "evil.docx", {"[Content_Types].xml": CT, "word/document.xml": evil})
    with pytest.raises(OfficeError, match="DOCTYPE"):
        load(path)


def test_not_a_document(tmp_path: Path) -> None:
    with pytest.raises(NotOfficeDocument):
        load(_zip(tmp_path / "fake.docx", {"readme.txt": "hi"}))
    (tmp_path / "ole.docx").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(600))
    with pytest.raises(NotOfficeDocument):
        load(tmp_path / "ole.docx")


def test_only_needed_parts_are_read(tmp_path: Path) -> None:
    media = os.urandom(3 * 1024 * 1024)  # incompressible, stored: big on disk
    path = make_docx(tmp_path / "photos.docx", extra={"word/media/image1.png": media})
    doc = load(path)
    size = path.stat().st_size
    assert size > 3 * 1024 * 1024
    assert doc.bytes_read < 200_000, doc.bytes_read
