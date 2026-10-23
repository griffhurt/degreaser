"""Update feeds and the feed-checking controller (docs/decisions.md D41)."""

import base64
import http.server
import importlib.util
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from PySide6.QtWidgets import QMessageBox
from pytestqt.qtbot import QtBot

from degreaser import __version__
from degreaser.core.settings import Settings
from degreaser.ui.controllers import update_controller
from degreaser.ui.controllers.update_controller import UpdateController
from degreaser.updates import probe
from degreaser.updates.feed import (
    feed_url,
    fetch_latest,
    is_newer,
    parse_appcast,
    user_agent,
    version_key,
)

ROOT = Path(__file__).resolve().parent.parent


def load_appcast_script():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("appcast", ROOT / "packaging" / "appcast.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_versions() -> None:
    assert version_key("v1.2") == version_key("1.2.0")
    assert is_newer("0.10.0", "0.9.9")
    assert not is_newer("0.1.0", "0.1.0")
    assert is_newer("1.0.0", "1.0.0b1") and not is_newer("1.0.0b1", "1.0.0")


def test_feed_urls_and_user_agent() -> None:
    assert feed_url("macos").endswith("/releases/latest/download/appcast-macos.xml")
    assert user_agent().startswith(f"Degreaser/{__version__} (")


def test_appcast_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """packaging/appcast.py writes feeds that parse, and whose signatures verify."""
    appcast = load_appcast_script()
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    monkeypatch.setattr(appcast, "public_key", lambda: base64.b64encode(public).decode())
    monkeypatch.setenv("SPARKLE_ED_PRIVATE_KEY", base64.b64encode(seed).decode())
    dmg = tmp_path / "Degreaser-9.9.9-macOS.dmg"
    dmg.write_bytes(b"pretend disk image")
    setup = tmp_path / "Degreaser-9.9.9-Windows-Setup.exe"
    setup.write_bytes(b"pretend installer")
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n## 9.9.9\n\n- **Faster** previews\n- `7z` listings\n\n## 1.0\n- old\n"
    )
    monkeypatch.setattr(appcast, "CHANGELOG", changelog)
    out = tmp_path / "feeds"
    monkeypatch.setattr(
        sys, "argv", ["appcast.py", "9.9.9", str(out), "--macos", str(dmg), "--windows", str(setup)]
    )
    assert appcast.main() == 0

    mac_xml = (out / "appcast-macos.xml").read_text()
    [release] = parse_appcast(mac_xml)
    assert release.version == "9.9.9"
    assert release.download_url.endswith("/releases/download/v9.9.9/Degreaser-9.9.9-macOS.dmg")
    assert release.page_url.endswith("/releases/tag/v9.9.9")
    assert "<b>Faster</b>" in release.notes_html and "<code>7z</code>" in release.notes_html
    assert "old" not in release.notes_html
    assert 'sparkle:minimumSystemVersion="14.0"' in mac_xml
    signature = mac_xml.split('sparkle:edSignature="')[1].split('"')[0]
    Ed25519PublicKey.from_public_bytes(public).verify(base64.b64decode(signature), dmg.read_bytes())

    win_xml = (out / "appcast-windows.xml").read_text()
    assert 'sparkle:os="windows"' in win_xml and "/SILENT" in win_xml
    assert not (out / "appcast-linux.xml").exists()


def test_appcast_refuses_a_mismatched_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    appcast = load_appcast_script()
    other = Ed25519PrivateKey.generate().public_key()
    raw = other.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(appcast, "public_key", lambda: base64.b64encode(raw).decode())
    seed = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    monkeypatch.setenv("SPARKLE_ED_PRIVATE_KEY", base64.b64encode(seed).decode())
    monkeypatch.setattr(sys, "argv", ["appcast.py", "1.0.0", str(tmp_path)])
    with pytest.raises(SystemExit, match="doesn't match"):
        appcast.main()


def feed_xml(version: str) -> bytes:
    return f"""<?xml version="1.0"?>
<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">
<channel><item><title>Degreaser {version}</title>
<link>https://example.com/v{version}</link>
<sparkle:version>{version}</sparkle:version>
<description><![CDATA[<p>Notes</p>]]></description>
<enclosure url="https://example.com/x.AppImage" length="1" type="application/octet-stream"/>
</item></channel></rss>""".encode()


@pytest.fixture
def feed_server() -> Iterator[tuple[str, dict[str, object]]]:
    state: dict[str, object] = {"body": feed_xml("99.0.0"), "agents": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            agents = state["agents"]
            assert isinstance(agents, list)
            agents.append(self.headers.get("User-Agent"))
            body = state["body"]
            assert isinstance(body, bytes)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/appcast.xml", state
    server.shutdown()


def test_fetch_latest(feed_server: tuple[str, dict[str, object]]) -> None:
    url, state = feed_server
    release = fetch_latest(url)
    assert release is not None and release.version == "99.0.0"
    assert state["agents"] == [user_agent()]  # nothing else is sent


def test_controller_offers_a_newer_release(
    qtbot: QtBot, feed_server: tuple[str, dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, state = feed_server
    monkeypatch.setenv(update_controller.FEED_OVERRIDE, url)
    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: shown.append(box.text()) or 0)
    monkeypatch.setattr(
        QMessageBox, "information", lambda *a: shown.append(a[2]) or QMessageBox.StandardButton.Ok
    )
    settings = Settings()
    controller = UpdateController(settings)
    assert controller.available and controller.backend is None

    controller.check_now()
    qtbot.waitUntil(lambda: bool(shown), timeout=5000)
    assert shown[-1] == f"Degreaser 99.0.0 is available. You have {__version__}."
    assert settings.updates.dismissed == "99.0.0"  # "Later" (exec returned no button)

    # Automatic checks don't offer a release the user put off, and run at most daily.
    shown.clear()
    controller._automatic_check()
    qtbot.waitUntil(lambda: settings.updates.last_check > 0, timeout=5000)
    assert shown == []
    state["body"] = feed_xml(__version__)
    controller.check_now()
    qtbot.waitUntil(lambda: bool(shown), timeout=5000)
    assert shown[-1] == f"Degreaser {__version__} is the latest version."


def test_development_runs_have_no_updater(qtbot: QtBot) -> None:
    controller = UpdateController(Settings())
    assert not controller.available
    assert probe() == ("none", "")
