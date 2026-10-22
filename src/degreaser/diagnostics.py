"""Runtime self-check of the components most likely to break in a packaged build.

Used by `degreaser --smoke-test` in CI, and later by Help → Diagnostics.
"""

import platform
import sys
from typing import Any

from degreaser import __version__


def collect() -> dict[str, Any]:
    """Return a JSON-serialisable report. Requires a QApplication to exist."""
    report: dict[str, Any] = {
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "errors": [],
    }

    try:
        from PySide6 import __version__ as pyside_version
        from PySide6.QtMultimedia import QMediaFormat

        report["pyside6"] = pyside_version
        decoders = QMediaFormat().supportedVideoCodecs(QMediaFormat.ConversionMode.Decode)
        report["video_decoders"] = sorted(c.name for c in decoders)
        if not decoders:
            report["errors"].append("Qt Multimedia reports no video decoders")
    except Exception as exc:
        report["errors"].append(f"QtMultimedia: {exc!r}")

    try:
        from PySide6.QtGui import QFontDatabase

        from degreaser.ui.theme.icons import pixmap
        from degreaser.ui.theme.manager import load_fonts

        if not load_fonts() or "Inter" not in QFontDatabase.families():
            report["errors"].append("theme: Inter font failed to load")
        if pixmap("check", "#000000", 16).isNull():
            report["errors"].append("theme: icons failed to render")
    except Exception as exc:
        report["errors"].append(f"theme: {exc!r}")

    try:
        from degreaser.ui.preview.ffmpeg_log import set_ffmpeg_log_level

        # Not an error if False: only FFmpeg's stderr noise comes back.
        report["ffmpeg_log_hook"] = set_ffmpeg_log_level()
    except Exception as exc:
        report["errors"].append(f"ffmpeg_log: {exc!r}")

    try:
        from PySide6.QtPdf import QPdfDocument

        QPdfDocument()
        report["qtpdf"] = True
    except Exception as exc:
        report["errors"].append(f"QtPdf: {exc!r}")

    try:
        from PySide6.QtGui import QImageReader

        report["image_formats"] = sorted(
            bytes(f.data()).decode() for f in QImageReader.supportedImageFormats()
        )
    except Exception as exc:
        report["errors"].append(f"QImageReader: {exc!r}")

    for module in ("smbclient", "pathspec", "puremagic", "charset_normalizer", "keyring", "PIL"):
        try:
            __import__(module)
        except Exception as exc:
            report["errors"].append(f"{module}: {exc!r}")

    return report
