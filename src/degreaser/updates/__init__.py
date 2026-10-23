"""Auto-updates (docs/decisions.md D41): Sparkle on macOS, WinSparkle on Windows, and a feed
check with a download link elsewhere. Only packaged builds update themselves."""

import sys
from importlib import resources
from pathlib import Path


def public_key() -> str:
    """The EdDSA public key update archives are signed with ('' until one is generated with
    packaging/generate_update_key.py)."""
    return (resources.files("degreaser.updates") / "public_key.txt").read_text("utf-8").strip()


def bundle_root() -> Path | None:
    """The packaged app's folder (where the updater library sits), or None in development."""
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    if sys.platform == "darwin":
        return exe.parents[1]  # Degreaser.app/Contents
    return exe.parent


def probe() -> tuple[str, str]:
    """(which updater this build would use, error) without starting it, for --smoke-test:
    'sparkle' / 'winsparkle' when the library is bundled and loads, 'feed' for other packaged
    builds, 'none' in development."""
    root = bundle_root()
    if root is None:
        return "none", ""
    try:
        if sys.platform == "darwin" and (root / "Frameworks" / "Sparkle.framework").is_dir():
            from degreaser.updates.sparkle import load_framework

            load_framework(root / "Frameworks" / "Sparkle.framework")
            return "sparkle", ""
        if sys.platform == "win32":
            from degreaser.updates.winsparkle import DLL_NAME, load_dll

            if (root / DLL_NAME).is_file():
                load_dll(root / DLL_NAME)
                return "winsparkle", ""
    except (OSError, AttributeError) as exc:
        return "feed", f"updater library failed to load: {exc}"
    return "feed", ""
