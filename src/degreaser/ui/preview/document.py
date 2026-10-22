"""Office document previews: Word/OpenDocument text, spreadsheets, presentations, iWork images.

Content comes pre-parsed from core/preview/office.py. Text is always escaped before it goes into
HTML, and the HTML never references external resources.
"""

import html
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableView,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from degreaser.core.preview.office import (
    ImageContent,
    OfficeDocument,
    PageBreak,
    Paragraph,
    Sheet,
    SheetContent,
    SlidesContent,
    Table,
    TextContent,
    column_letters,
)
from degreaser.ui.preview.image import ImageView, decode_image
from degreaser.ui.theme import theme
from degreaser.ui.theme.tokens import Tokens

HEADING_SIZES = {
    "title": 26,
    "subtitle": 17,
    "h1": 22,
    "h2": 18,
    "h3": 16,
    "h4": 14,
    "h5": 13,
    "h6": 13,
}
THUMBNAIL_MAX_WIDTH = 560


# Text documents -------------------------------------------------------------------------------


def _runs_html(paragraph: Paragraph) -> str:
    out = []
    for run in paragraph.runs:
        text = html.escape(run.text).replace("\t", "&nbsp;&nbsp;&nbsp;&nbsp;").replace("\n", "<br>")
        if run.bold:
            text = f"<b>{text}</b>"
        if run.italic:
            text = f"<i>{text}</i>"
        out.append(text)
    return "".join(out)


def text_html(content: TextContent, t: Tokens) -> str:
    """The document as simple, self-contained HTML for QTextBrowser."""
    parts = [f"<html><body style='color:{t.text};'>"]
    for block in content.blocks:
        if isinstance(block, PageBreak):
            parts.append(
                f"<p align='center' style='color:{t.text_faint}; font-size:11px; margin:14px 0;'>"
                "— page break —</p>"
            )
        elif isinstance(block, Table):
            parts.append(_table_html(block, t))
        else:
            parts.append(_paragraph_html(block, t))
    if not content.blocks:
        parts.append(f"<p style='color:{t.text_muted};'>This document has no text.</p>")
    parts.append("</body></html>")
    return "".join(parts)


def _paragraph_html(p: Paragraph, t: Tokens) -> str:
    body = _runs_html(p)
    if p.style in HEADING_SIZES:
        weight = 500 if p.style == "subtitle" else 600
        color = t.text_muted if p.style == "subtitle" else t.text
        return (
            f"<p style='font-size:{HEADING_SIZES[p.style]}px; font-weight:{weight}; color:{color};"
            f" margin-top:14px; margin-bottom:6px;'>{body}</p>"
        )
    if p.list_level is not None:
        indent = 18 + 22 * p.list_level
        marker = html.escape(p.list_marker)
        return (
            f"<p style='margin-left:{indent}px; margin-top:2px; margin-bottom:2px;'>"
            f"<span style='color:{t.text_muted};'>{marker}</span>&nbsp;&nbsp;{body}</p>"
        )
    if not p.text.strip():
        return "<p style='margin:4px 0;'>&nbsp;</p>"
    return f"<p style='margin-top:0; margin-bottom:8px; line-height:140%;'>{body}</p>"


def _table_html(table: Table, t: Tokens) -> str:
    rows = []
    for i, row in enumerate(table.rows):
        cells = "".join(
            f"<td style='padding:4px 8px; border:1px solid {t.border};"
            f"{' font-weight:600;' if i == 0 else ''}'>"
            f"{html.escape(cell).replace(chr(10), '<br>')}</td>"
            for cell in row
        )
        rows.append(f"<tr>{cells}</tr>")
    note = (
        f"<p style='color:{t.text_faint}; font-size:11px;'>Table truncated in preview</p>"
        if table.truncated
        else ""
    )
    return (
        f"<table cellspacing='0' cellpadding='0' style='border-collapse:collapse;"
        f" border:1px solid {t.border}; margin:8px 0;'>{''.join(rows)}</table>{note}"
    )


class _TextDocument(QTextBrowser):
    def __init__(self, content: TextContent) -> None:
        super().__init__()
        self.content = content
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.document().setDocumentMargin(22)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.render_html()

    def render_html(self) -> None:
        bar = self.verticalScrollBar()
        position = bar.value()
        self.setHtml(text_html(self.content, theme().tokens))
        bar.setValue(position)

    def loadResource(self, type_: int, name: Any) -> Any:
        return None  # never fetch anything the HTML might name


# Spreadsheets -----------------------------------------------------------------------------------


class SheetModel(QAbstractTableModel):
    def __init__(self, sheet: Sheet) -> None:
        super().__init__()
        self.sheet = sheet

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.sheet.rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else self.sheet.columns

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = 0) -> Any:
        if not index.isValid():
            return None
        value = self.sheet.rows[index.row()][1][index.column()]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return value if role == Qt.ItemDataRole.DisplayRole or len(value) > 40 else None
        if role == Qt.ItemDataRole.TextAlignmentRole and _looks_numeric(value):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = 0) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return column_letters(section)
        return str(self.sheet.rows[section][0])


def _looks_numeric(value: str) -> bool:
    try:
        float(value.replace(",", ""))
    except ValueError:
        return False
    return True


def _sheet_summary(sheet: Sheet) -> str:
    if sheet.note:
        return sheet.note
    parts = [f"{sheet.total_rows:,} row{'s' if sheet.total_rows != 1 else ''} with data"]
    if sheet.truncated_rows:
        parts.append(f"showing the first {len(sheet.rows):,}")
    if sheet.truncated_cols:
        parts.append(f"first {sheet.columns} of {sheet.max_col} columns")
    if sheet.hidden:
        parts.append("hidden sheet")
    return " · ".join(parts)


class _SheetPage(QWidget):
    def __init__(self, sheet: Sheet) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        if sheet.note or not sheet.rows:
            message = QLabel(sheet.note or "This sheet is empty.")
            message.setProperty("role", "muted")
            message.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(message, 1)
            self.table = None
            return
        self.table = QTableView()
        self.table.setModel(SheetModel(sheet))
        self.table.setFrameShape(QFrame.Shape.NoFrame)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.table.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.horizontalHeader().setDefaultSectionSize(110)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.table, 1)


class _Workbook(QTabWidget):
    def __init__(self, content: SheetContent) -> None:
        super().__init__()
        self.setDocumentMode(True)
        self.setUsesScrollButtons(True)
        self.tabBar().setExpanding(False)
        self.content = content
        self.pages: list[_SheetPage | None] = []
        for sheet in content.sheets:
            # Pages are built when first shown: big workbooks open instantly.
            self.addTab(QWidget(), ("◌ " if sheet.hidden else "") + sheet.name)
            self.pages.append(None)
        self.currentChanged.connect(self._ensure_page)
        theme().changed.connect(lambda _t: self._restyle())
        self._restyle()
        if content.sheets:
            self._ensure_page(0)

    def _ensure_page(self, index: int) -> None:
        if 0 <= index < len(self.pages) and self.pages[index] is None:
            page = _SheetPage(self.content.sheets[index])
            self.pages[index] = page
            self.blockSignals(True)
            name = self.tabText(index)
            self.removeTab(index)
            self.insertTab(index, page, name)
            self.setCurrentIndex(index)
            self.blockSignals(False)

    def _restyle(self) -> None:
        t = theme().tokens
        self.setStyleSheet(
            f"QTabWidget::pane {{ border: none; border-top: 1px solid {t.border}; }}"
            f"QTabBar::tab {{ background: transparent; color: {t.text_muted}; border: none;"
            f" padding: 6px 12px; margin: 6px 2px 6px 0; border-radius: 6px; }}"
            f"QTabBar::tab:selected {{ background: {t.surface_alt}; color: {t.text};"
            f" font-weight: 600; }}"
            f"QTabBar::tab:hover:!selected {{ color: {t.text}; }}"
        )

    def summary(self) -> str:
        index = self.currentIndex()
        if not (0 <= index < len(self.content.sheets)):
            return ""
        return _sheet_summary(self.content.sheets[index])


# Presentations --------------------------------------------------------------------------------


class _Slides(QScrollArea):
    def __init__(self, content: SlidesContent) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        if content.thumbnail:
            image = decode_image(content.thumbnail)
            if image is not None and not image.isNull():
                layout.addWidget(self._thumbnail(image), 0, Qt.AlignmentFlag.AlignHCenter)
        for slide in content.slides:
            card = QFrame()
            card.setProperty("role", "card")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(16, 12, 16, 14)
            card_layout.setSpacing(6)
            number = QLabel(f"Slide {slide.number}")
            number.setProperty("role", "faint")
            card_layout.addWidget(number)
            if slide.title:
                title = QLabel(slide.title)
                title.setWordWrap(True)
                title.setTextFormat(Qt.TextFormat.PlainText)
                title.setStyleSheet("font-size: 15px; font-weight: 600;")
                card_layout.addWidget(title)
            if slide.bullets:
                lines = "".join(
                    f"<p style='margin:2px 0 2px {14 + 18 * level}px;'>•&nbsp;&nbsp;"
                    f"{html.escape(text)}</p>"
                    for level, text in slide.bullets
                )
                body = QLabel(lines)
                body.setTextFormat(Qt.TextFormat.RichText)
                body.setWordWrap(True)
                body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                card_layout.addWidget(body)
            if not slide.title and not slide.bullets:
                empty = QLabel("No text on this slide")
                empty.setProperty("role", "muted")
                card_layout.addWidget(empty)
            layout.addWidget(card)
        if not content.slides:
            empty = QLabel("This presentation has no slides.")
            empty.setProperty("role", "muted")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(empty)
        layout.addStretch(1)
        self.setWidget(inner)

    @staticmethod
    def _thumbnail(image: QImage) -> QLabel:
        label = QLabel()
        pixmap = QPixmap.fromImage(image)
        if pixmap.width() > THUMBNAIL_MAX_WIDTH:
            pixmap = pixmap.scaledToWidth(
                THUMBNAIL_MAX_WIDTH, Qt.TransformationMode.SmoothTransformation
            )
        label.setPixmap(pixmap)
        label.setToolTip("Preview image saved in the file")
        label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        return label


# The view -----------------------------------------------------------------------------------------


class DocumentView(QWidget):
    """Shows an OfficeDocument; `loadMoreRequested` asks the pane for a bigger parse."""

    loadMoreRequested = Signal()

    def __init__(self, doc: OfficeDocument, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self.body: QWidget = QWidget()
        self._layout.addWidget(self.body, 1)
        footer = QHBoxLayout()
        footer.setContentsMargins(6, 2, 6, 4)
        self.status = QLabel()
        footer.addWidget(self.status, 1)
        self.more = QPushButton("Load more (Space)")
        self.more.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.more.clicked.connect(self.loadMoreRequested)
        footer.addWidget(self.more)
        self._layout.addLayout(footer)
        self.doc = doc
        self.set_document(doc)
        theme().changed.connect(lambda _t: self._retheme())

    def set_document(self, doc: OfficeDocument) -> None:
        """Show `doc` (also used after Load more: keeps the selected sheet tab)."""
        previous = self.body
        tab = previous.currentIndex() if isinstance(previous, _Workbook) else 0
        self.doc = doc
        content = doc.content
        body: QWidget
        if isinstance(content, TextContent):
            body = _TextDocument(content)
        elif isinstance(content, SheetContent):
            body = _Workbook(content)
            body.currentChanged.connect(lambda _i: self._update_status())
            if 0 < tab < body.count():
                body.setCurrentIndex(tab)
        elif isinstance(content, SlidesContent):
            body = _Slides(content)
        else:
            assert isinstance(content, ImageContent)
            view = ImageView()
            image = decode_image(content.image)
            if image is not None and not image.isNull():
                view.set_image(image)
            view.set_status("Preview image saved in the file by the app that made it")
            body = view
        self._layout.replaceWidget(previous, body)
        previous.hide()
        previous.deleteLater()
        self.body = body
        self._update_status()

    def _retheme(self) -> None:
        if isinstance(self.body, _TextDocument):
            self.body.render_html()

    def _update_status(self) -> None:
        content = self.doc.content
        if isinstance(content, TextContent):
            text = f"{content.total:,} paragraphs and tables"
            if content.truncated:
                text = f"Showing {content.shown:,} of {content.total:,} paragraphs and tables"
        elif isinstance(content, SheetContent):
            assert isinstance(self.body, _Workbook)
            text = self.body.summary()
            if len(content.sheets) < content.total_sheets:
                text += f" · first {len(content.sheets)} of {content.total_sheets} sheets"
        elif isinstance(content, SlidesContent):
            text = f"{content.total_slides:,} slide{'s' if content.total_slides != 1 else ''}"
            if content.truncated:
                text = (
                    f"Showing the first {len(content.slides):,} of {content.total_slides:,} slides"
                )
        else:
            text = ""
        self.status.setText(text)
        self.status.setVisible(bool(text))
        self.more.setVisible(self.doc.can_expand)

    def set_busy(self, busy: bool) -> None:
        self.more.setEnabled(not busy)
        self.more.setText("Loading…" if busy else "Load more (Space)")

    def teardown(self) -> None:
        teardown = getattr(self.body, "teardown", None)
        if teardown:
            teardown()
