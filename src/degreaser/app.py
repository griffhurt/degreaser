"""QApplication setup and top-level window management."""

import argparse
import json
import os

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from degreaser import __version__
from degreaser.core import settings as settings_mod
from degreaser.ui.controllers.app_controller import AppController
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
    parser.add_argument(
        "--theme",
        choices=["system", "light", "dark"],
        help="colour theme for this run (default: the one chosen in Preferences)",
    )
    parser.add_argument(
        "--smb-probe",
        metavar="URL",
        help="read-only check of an SMB server (smb://host/share/folder): listing speed, "
        "latency and name quirks; prints a JSON report and exits",
    )
    parser.add_argument("--username", help="with --smb-probe")
    parser.add_argument("--domain", default="", help="with --smb-probe")
    parser.add_argument(
        "--seconds", type=float, default=60.0, help="with --smb-probe: listing time limit"
    )
    args, qt_args = parser.parse_known_args(argv[1:])

    if args.smb_probe:
        return run_smb_probe(args)

    # Qt also ships a native macOS backend, but it can't play AVI/MKV/WMV and delivers no
    # frames headless (spikes/codecs/RESULTS.md). Must be set before QtMultimedia loads.
    os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")
    # Hide FFmpeg stream dumps; keep warnings.
    os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.*.info=false")

    app = QApplication([argv[0], *qt_args])
    app.setApplicationName("Degreaser")
    app.setApplicationVersion(__version__)

    from degreaser.ui.theme import theme, theme_override

    settings = settings_mod.load()
    theme().install(app, dark=theme_override(args.theme or settings.appearance.theme))

    from degreaser.core.preview.cache import wipe_all_project_caches
    from degreaser.ui.preview.ffmpeg_log import set_ffmpeg_log_level

    set_ffmpeg_log_level()  # libav's own "[h264 @ 0x…]" lines bypass Qt logging
    wipe_all_project_caches()  # previews left behind by a crash

    controller = AppController(settings)
    controller.show()

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


def run_smb_probe(args: argparse.Namespace) -> int:
    """`--smb-probe`: no windows, nothing written to the server. The password comes from
    DEGREASER_SMB_PASSWORD or a prompt."""
    import getpass
    import sys

    from degreaser.core.sources.errors import SourceError
    from degreaser.core.sources.smb import SmbConfig, parse_smb_location
    from degreaser.core.sources.smb_probe import probe, summarise

    location = parse_smb_location(args.smb_probe)
    if location is None or not location[2] or not args.username:
        usage = "usage: degreaser --smb-probe smb://host/share[/folder] --username NAME"
        print(usage, file=sys.stderr)
        return 2
    host, port, share, path = location
    config = SmbConfig(host, share, path, args.username, args.domain, port or 445)
    password = os.environ.get("DEGREASER_SMB_PASSWORD") or getpass.getpass(
        f"Password for {args.username}@{host}: "
    )

    def report(line: str) -> None:
        print(line, file=sys.stderr, flush=True)

    try:
        result = probe(config, password, seconds=args.seconds, report=report)
    except SourceError as exc:
        print(f"Probe failed: {exc}", file=sys.stderr)
        return 1
    for note in summarise(result):
        report(f"• {note}")
    text = json.dumps(result, indent=2, ensure_ascii=False)
    print(text)
    if args.report_file:
        with open(args.report_file, "w", encoding="utf-8") as f:
            f.write(text)
    return 0
