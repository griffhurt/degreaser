"""Render the app icons from docs/assets/logo.svg (run after changing the logo):

    uv run python packaging/make_icons.py

Writes packaging/icons/degreaser.icns (macOS), degreaser.ico (Windows) and degreaser.png
(Linux, 512 px). The outputs are committed so release builds don't need to render them.
"""

import io
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parent.parent
LOGO = ROOT / "docs" / "assets" / "logo.svg"
OUT = ROOT / "packaging" / "icons"


def render(renderer: QSvgRenderer, size: int) -> Image.Image:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    renderer.render(painter)
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return Image.open(io.BytesIO(bytes(data.data()))).convert("RGBA")


def main() -> None:
    QGuiApplication([])
    renderer = QSvgRenderer(str(LOGO))
    OUT.mkdir(parents=True, exist_ok=True)
    big = render(renderer, 1024)
    big.resize((512, 512), Image.Resampling.LANCZOS).save(OUT / "degreaser.png")
    big.save(
        OUT / "degreaser.icns",
        sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)],
    )
    # Small Windows sizes render straight from the SVG: downscaling 1024 px blurs them.
    small = {s: render(renderer, s) for s in (16, 24, 32, 48, 64, 128, 256)}
    small[256].save(
        OUT / "degreaser.ico",
        sizes=[(s, s) for s in small],
        append_images=[small[s] for s in small if s != 256],
    )
    for name in ("degreaser.png", "degreaser.icns", "degreaser.ico"):
        print(name, (OUT / name).stat().st_size)


if __name__ == "__main__":
    main()
