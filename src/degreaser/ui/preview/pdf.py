"""PDF preview with QtPdf."""

from pathlib import Path

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import QVBoxLayout, QWidget

from degreaser.core.preview.metadata import MetaRow


class PdfView(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.document = QPdfDocument(self)
        self.view = QPdfView()
        self.view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.view.setDocument(self.document)
        self.view.setPageMode(QPdfView.PageMode.MultiPage)
        self.view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        layout.addWidget(self.view)

    def load(self, path: Path) -> str | None:
        """Load synchronously; returns an error message, or None on success."""
        error = self.document.load(str(path))
        if error != QPdfDocument.Error.None_:
            return {
                QPdfDocument.Error.IncorrectPassword: "This PDF is password-protected.",
                QPdfDocument.Error.UnsupportedSecurityScheme: "This PDF's encryption isn't supported.",  # noqa: E501
                QPdfDocument.Error.InvalidFileFormat: "This doesn't look like a valid PDF.",
            }.get(error, "The PDF couldn't be opened.")
        return None

    def metadata_rows(self) -> list[MetaRow]:
        rows = [MetaRow("Document", "Pages", f"{self.document.pageCount():,}")]
        field = QPdfDocument.MetaDataField
        for key, label in (
            (field.Title, "Title"),
            (field.Author, "Author"),
            (field.Subject, "Subject"),
            (field.Creator, "Creator"),
            (field.Producer, "Producer"),
            (field.CreationDate, "Created"),
            (field.ModificationDate, "Modified"),
        ):
            value = self.document.metaData(key)
            if isinstance(value, QDateTime):
                text = value.toString("yyyy-MM-dd HH:mm:ss") if value.isValid() else ""
            else:
                text = str(value or "").strip()
            if text:
                rows.append(MetaRow("Document", label, text))
        return rows

    def teardown(self) -> None:
        self.document.close()
