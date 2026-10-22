# PyInstaller spec for Degreaser. Build with:
#   uv run --group build pyinstaller packaging/degreaser.spec --noconfirm
# Output: dist/Degreaser/ (onedir) and, on macOS, dist/Degreaser.app.
# Onedir keeps Qt/FFmpeg as separate replaceable libraries (LGPL compliance, see docs/architecture.md §9).
import sys

from PyInstaller.utils.hooks import collect_submodules

from degreaser import __version__

block_cipher = None

hiddenimports = [
    # Imported lazily (diagnostics / preview code), so PyInstaller can't see them statically.
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    *collect_submodules("smbclient"),
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

a = Analysis(
    ["entry.py"],
    pathex=[],
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
    codesign_identity=None,  # signing happens in the release workflow (M3)
    entitlements_file=None,
)

coll = COLLECT(exe, a.binaries, a.datas, name="Degreaser")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Degreaser.app",
        bundle_identifier="io.github.griffhurt.degreaser",
        version=__version__,
        info_plist={
            "CFBundleShortVersionString": __version__,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "14.0",
        },
    )
