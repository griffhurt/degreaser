"""Image preview: EXIF thumbnail first, then the full image, with zoom and pan."""

import io
from pathlib import Path

from PIL import Image, ImageOps
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QImage, QImageReader, QPainter, QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from degreaser.core.preview.metadata import dimensions

# Qt refuses to allocate big images by default (256 MB); full-resolution previews of large
# scans and panoramas need more. Images beyond this are decoded downscaled.
ALLOCATION_LIMIT_MB = 1024
MAX_PIXELS = 200_000_000

QImageReader.setAllocationLimit(ALLOCATION_LIMIT_MB)


def decode_image(data: bytes | Path) -> QImage:
    """Decode with Qt (honouring EXIF orientation), falling back to Pillow. Thread-safe."""
    if isinstance(data, Path):
        reader = QImageReader(str(data))
    else:
        buffer = QBuffer()
        buffer.setData(QByteArray(data))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and size.width() * size.height() > MAX_PIXELS:
        scale = (MAX_PIXELS / (size.width() * size.height())) ** 0.5
        reader.setScaledSize(size * scale)
    image = reader.read()
    if not image.isNull():
        return image
    return _decode_with_pillow(data)


def _decode_with_pillow(data: bytes | Path) -> QImage:
    with Image.open(io.BytesIO(data) if isinstance(data, bytes) else data) as im:
        im = ImageOps.exif_transpose(im)
        if im.width * im.height > MAX_PIXELS:
            side = int(MAX_PIXELS**0.5)
            im.thumbnail((side, side))
        rgba = im.convert("RGBA")
        raw = rgba.tobytes()
        image = QImage(raw, rgba.width, rgba.height, 4 * rgba.width, QImage.Format.Format_RGBA8888)
        return image.copy()  # detach from `raw`


class _View(QGraphicsView):
    def __init__(self, owner: ImageView) -> None:
        super().__init__()
        self.owner = owner
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def wheelEvent(self, event: QWheelEvent) -> None:
        factor = 1.25 if event.angleDelta().y() > 0 else 0.8
        self.owner.fit = False
        self.scale(factor, factor)

    def mouseDoubleClickEvent(self, event: object) -> None:
        self.owner.toggle_fit()

    def resizeEvent(self, event: object) -> None:
        super().resizeEvent(event)  # type: ignore[arg-type]
        if self.owner.fit:
            self.owner.fit_to_view()


class ImageView(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.scene = QGraphicsScene(self)
        self.view = _View(self)
        self.view.setScene(self.scene)
        self.item = QGraphicsPixmapItem()
        self.item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self.scene.addItem(self.item)
        layout.addWidget(self.view, 1)
        self.status = QLabel()
        self.status.setContentsMargins(6, 2, 6, 4)
        layout.addWidget(self.status)
        self.fit = True
        self.is_thumbnail = False
        self.full_size: tuple[int, int] | None = None

    def set_image(self, image: QImage, *, thumbnail: bool = False, note: str = "") -> None:
        self.item.setPixmap(QPixmap.fromImage(image))
        self.scene.setSceneRect(self.item.boundingRect())
        self.is_thumbnail = thumbnail
        if not thumbnail:
            self.full_size = (image.width(), image.height())
        label = dimensions(image.width(), image.height())
        self.status.setText(
            ("Embedded thumbnail — loading full image…" if thumbnail else label)
            + (f" · {note}" if note else "")
        )
        self.fit = True
        self.fit_to_view()

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def fit_to_view(self) -> None:
        if not self.item.pixmap().isNull():
            self.view.resetTransform()
            rect = self.item.boundingRect()
            # Never upscale small images beyond 100 % (except tiny thumbnails, which we do).
            port = self.view.viewport()
            too_big = rect.width() > port.width() or rect.height() > port.height()
            if self.is_thumbnail or too_big:
                self.view.fitInView(self.item, Qt.AspectRatioMode.KeepAspectRatio)

    def toggle_fit(self) -> None:
        if self.fit:
            self.fit = False
            self.view.resetTransform()
        else:
            self.fit = True
            self.fit_to_view()

    def teardown(self) -> None:
        self.item.setPixmap(QPixmap())
