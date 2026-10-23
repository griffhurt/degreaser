"""Write the per-platform update feeds (Sparkle appcasts) for a release (docs/decisions.md D41).

    SPARKLE_ED_PRIVATE_KEY=... uv run python packaging/appcast.py VERSION OUTDIR \\
        --macos Degreaser-0.1.0-macOS.dmg --windows Degreaser-0.1.0-Windows-Setup.exe \\
        --linux Degreaser-0.1.0-x86_64.AppImage

Each feed has one item: the release's notes (from CHANGELOG.md) and its download, signed with
the EdDSA key whose public half is src/degreaser/updates/public_key.txt.
"""

import argparse
import base64
import html
import os
import re
import sys
from email.utils import formatdate
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from degreaser.links import REPO  # noqa: E402
from degreaser.updates import public_key  # noqa: E402

CHANGELOG = ROOT / "CHANGELOG.md"
MIN_MACOS = "14.0"
# Inno Setup flags for updates WinSparkle runs: no wizard pages or prompts, and close the app
# if it's still running. The installer relaunches it afterwards (packaging/windows).
WINDOWS_INSTALLER_ARGS = "/SILENT /SP- /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS"


def notes_markdown(version: str) -> str:
    """The CHANGELOG.md section for `version`."""
    text = CHANGELOG.read_text("utf-8")
    match = re.search(rf"^## \[?{re.escape(version)}\]?.*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if match is None:
        raise SystemExit(f"CHANGELOG.md has no section for {version}")
    return match.group(1).strip()


def release_notes(version: str) -> str:
    """The CHANGELOG.md section for `version`, as simple HTML."""
    out: list[str] = []
    in_list = False
    for line in notes_markdown(version).splitlines():
        line = line.rstrip()
        bullet = re.match(r"^\s*[-*] (.*)", line)
        if bullet and not in_list:
            out.append("<ul>")
            in_list = True
        if not bullet and in_list:
            out.append("</ul>")
            in_list = False
        if bullet:
            out.append(f"<li>{_inline(bullet.group(1))}</li>")
        elif line.startswith("### "):
            out.append(f"<h3>{_inline(line[4:])}</h3>")
        elif line:
            out.append(f"<p>{_inline(line)}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    return re.sub(r"`(.+?)`", r"<code>\1</code>", text)


def sign(path: Path, private_key_b64: str) -> str:
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64))
    return base64.b64encode(key.sign(path.read_bytes())).decode()


def appcast(version: str, notes: str, item_xml: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">
  <channel>
    <title>Degreaser</title>
    <link>https://github.com/{REPO}</link>
    <item>
      <title>Degreaser {escape(version)}</title>
      <link>https://github.com/{REPO}/releases/tag/v{escape(version)}</link>
      <pubDate>{formatdate(usegmt=True)}</pubDate>
      <sparkle:version>{escape(version)}</sparkle:version>
      <sparkle:shortVersionString>{escape(version)}</sparkle:shortVersionString>
      <description><![CDATA[{notes}]]></description>
{item_xml}
    </item>
  </channel>
</rss>
"""


def enclosure(version: str, path: Path, key: str | None, extra: dict[str, str]) -> str:
    url = f"https://github.com/{REPO}/releases/download/v{version}/{path.name}"
    attrs = {"url": url, "length": str(path.stat().st_size), "type": "application/octet-stream"}
    if key is not None:
        attrs["sparkle:edSignature"] = sign(path, key)
    attrs.update(extra)
    joined = " ".join(f"{k}={quoteattr(v)}" for k, v in attrs.items())
    return f"      <enclosure {joined} />"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    parser.add_argument("outdir", type=Path)
    parser.add_argument("--macos", type=Path)
    parser.add_argument("--windows", type=Path)
    parser.add_argument("--linux", type=Path)
    parser.add_argument("--notes-out", type=Path, help="also write the notes as Markdown here")
    args = parser.parse_args()
    if args.notes_out is not None:
        args.notes_out.write_text(notes_markdown(args.version) + "\n", encoding="utf-8")

    key = os.environ.get("SPARKLE_ED_PRIVATE_KEY", "").strip()
    if not key or not public_key():
        raise SystemExit(
            "SPARKLE_ED_PRIVATE_KEY and src/degreaser/updates/public_key.txt are both needed "
            "(see packaging/generate_update_key.py)"
        )
    derived = Ed25519PrivateKey.from_private_bytes(base64.b64decode(key)).public_key()
    raw = derived.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if base64.b64encode(raw).decode() != public_key():
        raise SystemExit("SPARKLE_ED_PRIVATE_KEY doesn't match public_key.txt")

    notes = release_notes(args.version)
    args.outdir.mkdir(parents=True, exist_ok=True)
    feeds = {
        "macos": (args.macos, {"sparkle:minimumSystemVersion": MIN_MACOS}),
        "windows": (
            args.windows,
            {"sparkle:os": "windows", "sparkle:installerArguments": WINDOWS_INSTALLER_ARGS},
        ),
        # Nothing on Linux verifies the archive (the app links to the download), but the
        # signature costs nothing and keeps the feeds alike.
        "linux": (args.linux, {"sparkle:os": "linux"}),
    }
    for name, (path, extra) in feeds.items():
        if path is None:
            continue
        xml = appcast(args.version, notes, enclosure(args.version, path, key, extra))
        (args.outdir / f"appcast-{name}.xml").write_text(xml, encoding="utf-8")
        print(f"appcast-{name}.xml → {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
