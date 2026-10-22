"""QApplication setup and top-level window management."""

import argparse
import json
import os

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from degreaser import __version__
from degreaser.ui.windows.projects import ProjectsWindow


def build_main_window() -> ProjectsWindow:
    return ProjectsWindow()


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="degreaser")
    parser.add_argument("--version", action="version", version=f"degreaser {__version__}")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="show the main window, run self-checks, print them as JSON and exit (used by CI)",
    )
    parser.add_argument(
        "--report-file",
        # Windowed Windows builds have no stdout, so CI reads the report from a file.
        help="with --smoke-test, also write the JSON report to this file",
    )
    args, qt_args = parser.parse_known_args(argv[1:])

    # Qt also ships a native macOS backend, but it can't play AVI/MKV/WMV and delivers no
    # frames headless (spikes/codecs/RESULTS.md). Must be set before QtMultimedia loads.
    os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

    app = QApplication([argv[0], *qt_args])
    app.setApplicationName("Degreaser")
    app.setApplicationVersion(__version__)

    window = build_main_window()
    window.show()

    if args.smoke_test:
        from degreaser import diagnostics

        report = diagnostics.collect()
        text = json.dumps(report, indent=2)
        print(text)
        if args.report_file:
            with open(args.report_file, "w", encoding="utf-8") as f:
                f.write(text)
        QTimer.singleShot(500, lambda: app.exit(1 if report["errors"] else 0))

    return app.exec()
