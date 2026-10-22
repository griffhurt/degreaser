"""Office previews through the real PreviewPane (headless)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from degreaser.core.preview.office import PageBreak, Paragraph, Run, TextContent
from degreaser.core.sources.local import LocalSource
from degreaser.ui.preview.archive import ArchiveView
from degreaser.ui.preview.document import (
    DocumentView,
    SheetModel,
    _Slides,
    _TextDocument,
    _Workbook,
    text_html,
)
from degreaser.ui.preview.hex import HexView
from degreaser.ui.preview.image import ImageView
from degreaser.ui.preview.pane import PreviewPane
from degreaser.ui.theme import LIGHT
from tests.core.test_office import _zip, make_docx, make_iwork, make_odf, make_pptx, make_xlsx

TIMEOUT = 10_000


@pytest.fixture
def pane(qtbot: QtBot) -> Iterator[PreviewPane]:
    p = PreviewPane()
    qtbot.addWidget(p)
    p.resize(800, 600)
    p.show()
    yield p
    p.shutdown()


def show(qtbot: QtBot, pane: PreviewPane, path: Path) -> None:
    source = LocalSource(path.parent)
    with qtbot.waitSignal(pane.loaded, timeout=TIMEOUT):
        pane.show_file(source, path.name, source.stat(path.name))


def test_docx_renders_text_and_metadata(qtbot: QtBot, pane: PreviewPane, tmp_path: Path) -> None:
    show(qtbot, pane, make_docx(tmp_path / "notes.docx"))
    view = pane.view
    assert isinstance(view, DocumentView) and isinstance(view.body, _TextDocument)
    text = view.body.toPlainText()
    for expected in ("Old Laptop Notes", "Introduction", "bold", "Step two", "a.txt", "page break"):
        assert expected in text
    rows = {r.label: r.value for r in pane.metadata_rows() if r.group == "Document"}
    assert rows["Title"] == "Laptop notes" and rows["Pages"] == "3"
    assert not view.more.isVisibleTo(view)


def test_long_docx_loads_more_with_space(qtbot: QtBot, pane: PreviewPane, tmp_path: Path) -> None:
    show(qtbot, pane, make_docx(tmp_path / "long.docx", paragraphs=600))
    view = pane.view
    assert isinstance(view, DocumentView)
    assert view.more.isVisibleTo(view) and "Showing 400 of 612" in view.status.text()
    assert pane.expand()
    qtbot.waitUntil(
        lambda: isinstance(view.body, _TextDocument) and view.status.text().startswith("612"),
        timeout=TIMEOUT,
    )
    assert "Filler paragraph 599" in view.body.toPlainText()  # type: ignore[union-attr]
    assert not pane.expand()


def test_xlsx_tabs_and_cells(qtbot: QtBot, pane: PreviewPane, tmp_path: Path) -> None:
    show(qtbot, pane, make_xlsx(tmp_path / "inv.xlsx"))
    view = pane.view
    assert isinstance(view, DocumentView) and isinstance(view.body, _Workbook)
    book = view.body
    assert [book.tabText(i) for i in range(book.count())] == ["Inventory", "◌ Notes"]
    page = book.pages[0]
    assert page is not None and page.table is not None
    model = page.table.model()
    assert isinstance(model, SheetModel)
    assert model.headerData(2, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole) == "C"
    assert model.headerData(2, Qt.Orientation.Vertical, Qt.ItemDataRole.DisplayRole) == "4"
    assert model.index(1, 2).data() == "2023-03-15"
    assert "3 rows with data" in view.status.text()
    book.setCurrentIndex(1)
    assert book.pages[1] is not None
    assert "hidden sheet" in view.status.text()


def test_pptx_slide_cards(qtbot: QtBot, pane: PreviewPane, tmp_path: Path) -> None:
    show(qtbot, pane, make_pptx(tmp_path / "deck.pptx"))
    view = pane.view
    assert isinstance(view, DocumentView) and isinstance(view.body, _Slides)
    assert view.status.text() == "2 slides"


@pytest.mark.parametrize(
    "kind,ext", [("text", "odt"), ("spreadsheet", "ods"), ("presentation", "odp")]
)
def test_opendocument(qtbot: QtBot, pane: PreviewPane, tmp_path: Path, kind: str, ext: str) -> None:
    show(qtbot, pane, make_odf(tmp_path / f"f.{ext}", kind))
    assert isinstance(pane.view, DocumentView)


def test_iwork_shows_embedded_preview(qtbot: QtBot, pane: PreviewPane, tmp_path: Path) -> None:
    show(qtbot, pane, make_iwork(tmp_path / "essay.pages"))
    view = pane.view
    assert isinstance(view, DocumentView) and isinstance(view.body, ImageView)


def test_not_really_a_docx_falls_back_to_listing(
    qtbot: QtBot, pane: PreviewPane, tmp_path: Path
) -> None:
    show(qtbot, pane, _zip(tmp_path / "fake.docx", {"readme.txt": "hi", "data.bin": b"\x00" * 10}))
    view = pane.view
    assert isinstance(view, ArchiveView)
    assert view.note.isVisibleTo(view) and "No Word document preview" in view.note.text()
    assert any(r.label == "Preview" and r.warning for r in pane.metadata_rows())


def test_corrupt_docx_falls_back_to_hex(qtbot: QtBot, pane: PreviewPane, tmp_path: Path) -> None:
    path = tmp_path / "broken.docx"
    path.write_bytes(b"PK\x03\x04" + b"\x00" * 300)
    show(qtbot, pane, path)
    assert isinstance(pane.view, HexView)


def test_html_is_escaped() -> None:
    content = TextContent(
        [Paragraph((Run("<script>alert(1)</script> & <img src='http://x/y.png'>"),)), PageBreak()],
        shown=1,
        total=1,
    )
    page = text_html(content, LIGHT)
    assert "<script>" not in page and "<img" not in page
    assert "&lt;script&gt;" in page
