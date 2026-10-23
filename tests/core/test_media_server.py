import http.client
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from degreaser.core.preview import media_server as ms
from degreaser.core.preview.media_server import MediaServer, parse_range
from tests.fixtures.preview.helpers import RemoteLikeSource

DATA = bytes(range(256)) * 100  # 25,600 bytes


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[MediaServer, str, RemoteLikeSource]]:
    (tmp_path / "clip one.mp4").write_bytes(DATA)
    source = RemoteLikeSource(tmp_path)
    server = MediaServer()
    url = server.register(source, "clip one.mp4", len(DATA))
    yield server, url, source
    server.close()


def fetch(url: str, method: str = "GET", headers: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=5)
    conn.request(method, parts.path, headers=headers or {})
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response, body


def test_parse_range() -> None:
    assert parse_range(None, 100) == (0, 99, False)
    assert parse_range("bytes=10-19", 100) == (10, 19, True)
    assert parse_range("bytes=90-200", 100) == (90, 99, True)
    assert parse_range("bytes=-10", 100) == (90, 99, True)
    assert parse_range("bytes=100-", 100) is None
    assert parse_range("bytes=0-", 3 * ms.WINDOW) == (0, ms.WINDOW - 1, True)
    assert parse_range("items=1-2", 100) is None


def test_serves_ranges(served: tuple[MediaServer, str, RemoteLikeSource]) -> None:
    _server, url, _source = served
    assert url.startswith("http://127.0.0.1:") and url.endswith("/clip%20one.mp4")
    response, body = fetch(url)
    assert response.status == 200 and body == DATA
    assert response.getheader("Content-Type") == "video/mp4"
    response, body = fetch(url, headers={"Range": "bytes=1000-1009"})
    assert response.status == 206 and body == DATA[1000:1010]
    assert response.getheader("Content-Range") == f"bytes 1000-1009/{len(DATA)}"
    response, body = fetch(url, "HEAD")
    assert response.status == 200 and body == b""
    assert response.getheader("Content-Length") == str(len(DATA))
    response, _ = fetch(url, headers={"Range": f"bytes={len(DATA)}-"})
    assert response.status == 416


def test_open_ended_ranges_are_windowed(
    served: tuple[MediaServer, str, RemoteLikeSource], monkeypatch: pytest.MonkeyPatch
) -> None:
    _server, url, _source = served
    monkeypatch.setattr(ms, "WINDOW", 4096)
    response, body = fetch(url, headers={"Range": "bytes=100-"})
    assert response.status == 206 and body == DATA[100:4196]


def test_unknown_and_revoked_tokens_are_404(
    served: tuple[MediaServer, str, RemoteLikeSource],
) -> None:
    server, url, _source = served
    response, _ = fetch(url.replace(url.split("/")[3], "nope"))
    assert response.status == 404
    server.revoke(url)
    response, _ = fetch(url)
    assert response.status == 404


def test_missing_file_is_404(tmp_path: Path) -> None:
    server = MediaServer()
    url = server.register(RemoteLikeSource(tmp_path), "gone.mp4", 100)
    response, _ = fetch(url, headers={"Range": "bytes=0-"})
    assert response.status == 404
    server.close()
