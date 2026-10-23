"""Loopback HTTP server that streams remote media to QMediaPlayer (D27, docs/previews.md §2).

QMediaPlayer gets `http://127.0.0.1:<port>/<token>/<name>`, so FFmpeg's own HTTP client does
the reading on Qt's threads and Python only runs on this server's threads. Qt never waits on
Python, which is what makes it safe where a Python QIODevice deadlocks.

Two limits keep FFmpeg from reading far ahead of playback (spikes/smb_streaming):
open-ended ranges are answered with at most WINDOW bytes (FFmpeg asks again for the rest),
and each socket's send buffer is small, so an abandoned response stops pulling from the
source almost at once.
"""

import contextlib
import logging
import mimetypes
import re
import secrets
import socket
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote

from degreaser.core.sources.base import FileSource, basename
from degreaser.core.sources.errors import NotFound, SourceError

log = logging.getLogger(__name__)

WINDOW = 8 * 1024 * 1024  # largest answer to an open-ended range
PIECE = 512 * 1024  # bytes read from the source per write
SEND_BUFFER = 256 * 1024

_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")


@dataclass(frozen=True, slots=True)
class _Served:
    source: FileSource
    rel: str
    size: int


class MediaServer:
    """Serves files that were explicitly registered, read-only, to loopback clients only."""

    def __init__(self) -> None:
        self._files: dict[str, _Served] = {}
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self.bytes_served = 0  # for tests and diagnostics

    @property
    def port(self) -> int:
        return self._start().server_address[1]

    def _start(self) -> ThreadingHTTPServer:
        with self._lock:
            if self._server is None:
                server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
                server.daemon_threads = True
                threading.Thread(
                    target=server.serve_forever, name="media-server", daemon=True
                ).start()
                self._server = server
            return self._server

    def register(self, source: FileSource, rel: str, size: int) -> str:
        """A URL for this file, valid until revoked. The name keeps the extension, which
        FFmpeg uses as a hint when probing."""
        token = secrets.token_urlsafe(18)
        with self._lock:
            self._files[token] = _Served(source, rel, size)
        name = quote(basename(rel) or "media")
        return f"http://127.0.0.1:{self.port}/{token}/{name}"

    def revoke(self, url: str) -> None:
        token = url.split("/")[3] if url.count("/") >= 4 else ""
        with self._lock:
            self._files.pop(token, None)

    def revoke_all(self) -> None:
        with self._lock:
            self._files.clear()

    def lookup(self, token: str) -> _Served | None:
        with self._lock:
            return self._files.get(token)

    def close(self) -> None:
        with self._lock:
            server, self._server = self._server, None
            self._files.clear()
        if server is not None:
            server.shutdown()
            server.server_close()


def parse_range(header: str | None, size: int) -> tuple[int, int, bool] | None:
    """(start, end inclusive, partial) for a Range header, or None if unsatisfiable.

    Open-ended ranges are capped at WINDOW bytes; a client asking for a specific range gets
    exactly that, and one sending no Range at all gets the whole file.
    """
    if not header:
        return (0, size - 1, False) if size else None
    m = _RANGE.match(header.strip())
    if not m or (not m.group(1) and not m.group(2)):
        return None
    if not m.group(1):  # suffix: the last N bytes
        start = max(0, size - int(m.group(2)))
        end = size - 1
    else:
        start = int(m.group(1))
        end = min(int(m.group(2)), size - 1) if m.group(2) else min(size, start + WINDOW) - 1
    if start >= size or end < start:
        return None
    return start, end, True


def _handler(server: MediaServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            pass

        def setup(self) -> None:
            self.request.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SEND_BUFFER)
            super().setup()

        def do_HEAD(self) -> None:
            self._serve(body=False)

        def do_GET(self) -> None:
            self._serve(body=True)

        def _serve(self, *, body: bool) -> None:
            parts = self.path.split("/")
            served = server.lookup(parts[1]) if len(parts) == 3 else None
            if served is None:
                self._empty(404)
                return
            size = served.size
            span = parse_range(self.headers.get("Range"), size)
            if span is None:
                self._empty(416 if size else 200, {"Content-Range": f"bytes */{size}"})
                return
            start, end, partial = span
            data = b""
            if body:
                # Read the first piece before answering, so a missing file is a clean 404
                # instead of a truncated 206.
                try:
                    data = served.source.read_range(served.rel, start, min(PIECE, end - start + 1))
                except NotFound:
                    self._empty(404)
                    return
                except SourceError as exc:
                    log.warning("media server: %s: %s", served.rel, exc)
                    self._empty(503)
                    return
            self.send_response(206 if partial else 200)
            ctype = mimetypes.guess_type(unquote(parts[2]))[0] or "application/octet-stream"
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if body:
                self._stream(served, start, end, data)

        def _stream(self, served: _Served, start: int, end: int, first: bytes) -> None:
            pos, data = start, first
            try:
                while True:
                    if not data:
                        break  # the file shrank; the client sees a short response
                    self.wfile.write(data)  # blocks on backpressure: the read-ahead limit
                    server.bytes_served += len(data)
                    pos += len(data)
                    if pos > end:
                        break
                    data = served.source.read_range(served.rel, pos, min(PIECE, end - pos + 1))
            except BrokenPipeError, ConnectionResetError:
                pass  # the player seeked or closed
            except SourceError as exc:
                # Mid-stream failure: drop the connection so FFmpeg retries or reports it.
                log.warning("media server: %s: %s", served.rel, exc)
                self.close_connection = True

        def _empty(self, status: int, headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()
            with contextlib.suppress(OSError):
                self.wfile.flush()

    return Handler
