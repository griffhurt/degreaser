# PyInstaller spec for Degreaser. Build with:
#   uv run --group build pyinstaller packaging/degreaser.spec --noconfirm
# Output: dist/Degreaser/ (onedir) and, on macOS, dist/Degreaser.app.
# Onedir keeps Qt/FFmpeg as separate replaceable libraries (LGPL compliance, see docs/architecture.md §9).
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

from degreaser import __version__
from degreaser.updates import public_key
from degreaser.updates.feed import feed_url

ICONS = Path(SPECPATH) / "icons"  # noqa: F821 (SPECPATH is defined by PyInstaller)
# Release builds sign while collecting (packaging/macos/sign.sh adds Sparkle afterwards).
CODESIGN_IDENTITY = os.environ.get("DEGREASER_CODESIGN_IDENTITY") or None
ENTITLEMENTS = str(Path(SPECPATH) / "macos" / "entitlements.plist") if CODESIGN_IDENTITY else None  # noqa: F821

block_cipher = None

hiddenimports = [
    # Imported lazily (diagnostics / preview code), so PyInstaller can't see them statically.
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtSvg",
    *collect_submodules("smbclient"),
    *collect_submodules("spnego"),  # NTLM/Kerberos providers are imported by name
    *collect_submodules("keyring.backends"),
]

# Large Qt modules we don't use.
excludes = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.QtQuick3D",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtDesigner",
    "PySide6.QtLocation",
    "PySide6.QtBluetooth",
    "PySide6.QtSerialPort",
    "PySide6.QtSensors",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtTextToSpeech",
    "PySide6.QtHttpServer",
    "PySide6.QtWebSockets",
    "tkinter",
]

# Fonts (Inter, OFL) and icons (Lucide, ISC), with their licence files; the update key.
datas = collect_data_files("degreaser.ui.assets") + collect_data_files("degreaser.updates")

a = Analysis(
    ["entry.py"],
    pathex=[],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Degreaser" if sys.platform != "linux" else "degreaser",
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    codesign_identity=CODESIGN_IDENTITY,
    entitlements_file=ENTITLEMENTS,
    icon=str(ICONS / ("degreaser.ico" if sys.platform == "win32" else "degreaser.icns")),
)

coll = COLLECT(exe, a.binaries, a.datas, name="Degreaser")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Degreaser.app",
        bundle_identifier="com.griffhurt.degreaser",
        version=__version__,
        icon=str(ICONS / "degreaser.icns"),
        codesign_identity=CODESIGN_IDENTITY,
        entitlements_file=ENTITLEMENTS,
        info_plist={
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "14.0",
            "LSApplicationCategoryType": "public.app-category.utilities",
            "NSHumanReadableCopyright": "MIT License",
            # Sparkle (docs/decisions.md D41). Automatic checks are on by default, which also
            # skips Sparkle's own permission prompt; Preferences turns them off.
            "SUFeedURL": feed_url("macos"),
            "SUPublicEDKey": public_key(),
            "SUEnableAutomaticChecks": True,
        },
    )
