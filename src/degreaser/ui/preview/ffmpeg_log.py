"""Quiet FFmpeg's own logging.

Qt's FFmpeg backend routes some messages through Qt logging (silenced with
QT_LOGGING_RULES="qt.multimedia.*.info=false"), but libav decoders also write straight to
stderr via av_log ("[h264 @ 0x…] hardware accelerator failed…"). The only switch for those is
av_log_set_level in the libavutil that Qt loaded, which we reach with ctypes.
"""

import ctypes
import sys
from pathlib import Path

import PySide6

AV_LOG_QUIET = -8
AV_LOG_FATAL = 8
AV_LOG_ERROR = 16


def _candidates() -> list[Path]:
    root = Path(PySide6.__file__).parent
    patterns = {
        "darwin": ["Qt/lib/libavutil.*.dylib"],
        "win32": ["avutil-*.dll", "Qt/bin/avutil-*.dll"],
    }.get(sys.platform, ["Qt/lib/libavutil.so*"])
    # Shortest name first: Qt links the soname (libavutil.61.dylib), and on macOS dlopen-ing
    # the fully versioned file instead maps a *second* copy whose settings Qt never sees.
    found = {p for pattern in patterns for p in root.glob(pattern)}
    return sorted(found, key=lambda p: (len(p.name), p.name))


def set_ffmpeg_log_level(level: int = AV_LOG_FATAL) -> bool:
    """Returns True if Qt's bundled libavutil was found and its log level set."""
    for path in _candidates():
        try:
            lib = ctypes.CDLL(str(path))
            lib.av_log_set_level.argtypes = [ctypes.c_int]
            lib.av_log_set_level(level)
        except OSError, AttributeError:
            continue
        return True
    return False
