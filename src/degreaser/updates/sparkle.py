"""Sparkle 2 on macOS, driven through the Objective-C runtime with ctypes.

Sparkle reads the feed URL (SUFeedURL) and public key (SUPublicEDKey) from the app's
Info.plist, which packaging/degreaser.spec fills in. Everything here runs on the main thread.
"""

import ctypes
import ctypes.util
from ctypes import c_bool, c_char_p, c_void_p
from pathlib import Path
from typing import Any

CONTROLLER_CLASS = "SPUStandardUpdaterController"


class _ObjC:
    def __init__(self) -> None:
        path = ctypes.util.find_library("objc")
        if path is None:
            raise OSError("Objective-C runtime not found")
        self.lib = ctypes.cdll.LoadLibrary(path)
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Foundation.framework/Foundation")
        self.lib.objc_getClass.restype = c_void_p
        self.lib.objc_getClass.argtypes = [c_char_p]
        self.lib.sel_registerName.restype = c_void_p
        self.lib.sel_registerName.argtypes = [c_char_p]
        address = ctypes.cast(self.lib.objc_msgSend, c_void_p).value
        if address is None:
            raise OSError("objc_msgSend not found")
        self._msg_send: int = address

    def cls(self, name: str) -> int:
        found = self.lib.objc_getClass(name.encode())
        if not found:
            raise OSError(f"Objective-C class {name} not found")
        return found

    def send(
        self,
        receiver: int,
        selector: str,
        *args: Any,
        restype: Any = c_void_p,
        argtypes: tuple = (),
    ) -> Any:
        # objc_msgSend must be called through a prototype matching the method exactly
        # (arm64 passes variadic arguments differently).
        fn = ctypes.CFUNCTYPE(restype, c_void_p, c_void_p, *argtypes)(self._msg_send)
        return fn(receiver, self.lib.sel_registerName(selector.encode()), *args)

    def string(self, text: str) -> int:
        return self.send(
            self.cls("NSString"), "stringWithUTF8String:", text.encode(), argtypes=(c_char_p,)
        )


def load_framework(framework: Path) -> _ObjC:
    """Load Sparkle.framework; raises OSError if it can't be."""
    objc = _ObjC()
    bundle = objc.send(
        objc.cls("NSBundle"), "bundleWithPath:", objc.string(str(framework)), argtypes=(c_void_p,)
    )
    if not bundle or not objc.send(bundle, "load", restype=c_bool):
        raise OSError(f"couldn't load {framework}")
    objc.cls(CONTROLLER_CLASS)
    return objc


class SparkleUpdater:
    name = "sparkle"

    def __init__(self, framework: Path, *, automatic: bool) -> None:
        objc = load_framework(framework)
        self._objc = objc
        controller = objc.send(objc.cls(CONTROLLER_CLASS), "alloc")
        # Owned (+1) and never released: the controller lives as long as the app.
        self._controller = objc.send(
            controller,
            "initWithStartingUpdater:updaterDelegate:userDriverDelegate:",
            True,
            None,
            None,
            argtypes=(c_bool, c_void_p, c_void_p),
        )
        if not self._controller:
            raise OSError("Sparkle didn't start")
        self._updater = objc.send(self._controller, "updater")
        self.set_automatic(automatic)

    def set_automatic(self, automatic: bool) -> None:
        self._objc.send(
            self._updater,
            "setAutomaticallyChecksForUpdates:",
            automatic,
            restype=None,
            argtypes=(c_bool,),
        )

    def check_now(self) -> None:
        """Sparkle shows its own windows: checking, up to date, or the update with notes."""
        self._objc.send(
            self._controller, "checkForUpdates:", None, restype=None, argtypes=(c_void_p,)
        )

    def shutdown(self) -> None:
        pass
