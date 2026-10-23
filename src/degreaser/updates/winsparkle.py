"""WinSparkle on Windows, through ctypes. WinSparkle downloads the new installer, checks its
EdDSA signature, asks the app to quit (on its own thread) and runs the installer."""

import ctypes
from collections.abc import Callable
from ctypes import CFUNCTYPE, c_char_p, c_int, c_wchar_p
from pathlib import Path
from typing import Any

DLL_NAME = "WinSparkle.dll"
_CAN_SHUTDOWN = CFUNCTYPE(c_int)
_SHUTDOWN_REQUEST = CFUNCTYPE(None)

_PROTOTYPES: dict[str, tuple[Any, list[Any]]] = {
    "win_sparkle_set_appcast_url": (None, [c_char_p]),
    "win_sparkle_set_eddsa_public_key": (c_int, [c_char_p]),
    "win_sparkle_set_app_details": (None, [c_wchar_p, c_wchar_p, c_wchar_p]),
    "win_sparkle_set_automatic_check_for_updates": (None, [c_int]),
    "win_sparkle_set_can_shutdown_callback": (None, [_CAN_SHUTDOWN]),
    "win_sparkle_set_shutdown_request_callback": (None, [_SHUTDOWN_REQUEST]),
    "win_sparkle_init": (None, []),
    "win_sparkle_check_update_with_ui": (None, []),
    "win_sparkle_cleanup": (None, []),
}


def load_dll(path: Path) -> Any:
    """Load WinSparkle.dll and declare its functions; raises OSError if it can't be."""
    lib = getattr(ctypes, "WinDLL")(str(path))  # noqa: B009 (WinDLL only exists on Windows)
    for name, (restype, argtypes) in _PROTOTYPES.items():
        fn = getattr(lib, name)
        fn.restype = restype
        fn.argtypes = argtypes
    return lib


class WinSparkleUpdater:
    name = "winsparkle"

    def __init__(
        self,
        dll: Path,
        *,
        feed: str,
        public_key: str,
        version: str,
        automatic: bool,
        request_shutdown: Callable[[], None],
    ) -> None:
        """`request_shutdown` is called on WinSparkle's thread when an update is ready to
        install; it must get the app to quit (from the UI thread)."""
        lib = load_dll(dll)
        self._lib = lib
        lib.win_sparkle_set_appcast_url(feed.encode())
        if lib.win_sparkle_set_eddsa_public_key(public_key.encode()) != 1:
            raise OSError("WinSparkle rejected the update public key")
        lib.win_sparkle_set_app_details("Degreaser", "Degreaser", version)
        lib.win_sparkle_set_automatic_check_for_updates(int(automatic))
        # Kept referenced: WinSparkle calls these later, from its own thread.
        self._can_shutdown = _CAN_SHUTDOWN(lambda: 1)
        self._shutdown_request = _SHUTDOWN_REQUEST(request_shutdown)
        lib.win_sparkle_set_can_shutdown_callback(self._can_shutdown)
        lib.win_sparkle_set_shutdown_request_callback(self._shutdown_request)
        lib.win_sparkle_init()

    def set_automatic(self, automatic: bool) -> None:
        self._lib.win_sparkle_set_automatic_check_for_updates(int(automatic))

    def check_now(self) -> None:
        self._lib.win_sparkle_check_update_with_ui()

    def shutdown(self) -> None:
        self._lib.win_sparkle_cleanup()
