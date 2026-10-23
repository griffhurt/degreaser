"""Update feeds (Sparkle appcasts) published with each release (docs/decisions.md D41).

The release workflow publishes one appcast per platform as a release asset, so the
"latest/download" URL always points at the newest published release's feed. macOS (Sparkle)
and Windows (WinSparkle) read their feed themselves; this module reads one for the platforms
without an updater library (Linux), and for tests.
"""

import platform
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from degreaser import __version__
from degreaser.links import RELEASES_URL as RELEASES_URL
from degreaser.links import REPO

__all__ = ["RELEASES_URL", "REPO", "Release", "feed_url", "fetch_latest", "is_newer"]
_SPARKLE = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"


def platform_name() -> str:
    return {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")


def feed_url(name: str | None = None) -> str:
    return f"{RELEASES_URL}/latest/download/appcast-{name or platform_name()}.xml"


def user_agent() -> str:
    """All a check sends: the app version and platform."""
    return f"Degreaser/{__version__} ({platform.system()}; {platform.machine()})"


@dataclass(frozen=True, slots=True)
class Release:
    version: str
    title: str
    notes_html: str
    download_url: str
    page_url: str


def version_key(version: str) -> tuple[int, ...]:
    """A sortable key: '0.10.2' → (0, 10, 2, 0, 0). Missing parts count as 0 ('1.2' equals
    '1.2.0'), and a pre-release suffix sorts before its release ('1.0.0b1' < '1.0.0')."""
    numbers: list[int] = []
    release = 0
    for piece in version.strip().lstrip("v").split("."):
        match = re.match(r"(\d+)(.*)", piece)
        if match is None:
            break
        numbers.append(int(match.group(1)))
        if match.group(2):
            release = -1
            break
    return (*numbers[:4], *[0] * (4 - len(numbers[:4])), release)


def is_newer(candidate: str, current: str = __version__) -> bool:
    return version_key(candidate) > version_key(current)


def parse_appcast(xml: bytes | str) -> list[Release]:
    """Every item in an appcast, newest first."""
    root = ET.fromstring(xml)
    releases: list[Release] = []
    for item in root.iter("item"):
        enclosure = item.find("enclosure")
        version = (
            item.findtext(f"{_SPARKLE}shortVersionString")
            or item.findtext(f"{_SPARKLE}version")
            or (enclosure.get(f"{_SPARKLE}shortVersionString") if enclosure is not None else None)
            or (enclosure.get(f"{_SPARKLE}version") if enclosure is not None else None)
        )
        if not version:
            continue
        releases.append(
            Release(
                version=version.strip(),
                title=(item.findtext("title") or f"Degreaser {version}").strip(),
                notes_html=(item.findtext("description") or "").strip(),
                download_url=enclosure.get("url", "") if enclosure is not None else "",
                page_url=(item.findtext("link") or RELEASES_URL).strip(),
            )
        )
    releases.sort(key=lambda r: version_key(r.version), reverse=True)
    return releases


def fetch_latest(url: str | None = None, timeout: float = 15) -> Release | None:
    """The newest release in the feed, or None if it has none. Raises OSError (including
    urllib's errors) when the feed can't be fetched, and ET.ParseError when it's malformed."""
    request = urllib.request.Request(url or feed_url(), headers={"User-Agent": user_agent()})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        releases = parse_appcast(response.read())
    return releases[0] if releases else None
