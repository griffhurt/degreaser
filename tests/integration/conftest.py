"""Fixtures for SMB integration tests against the Samba container.

Start it with: docker compose -f tests/integration/docker-compose.yml up -d --build
Tests marked `smb` are skipped when the server isn't reachable.
"""

import os
import socket
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
import smbclient
import smbclient.shutil


@dataclass(frozen=True)
class SmbTestServer:
    host: str
    port: int
    username: str
    password: str

    @property
    def kwargs(self) -> dict[str, object]:
        # smbclient's path-based calls don't remember the port from register_session,
        # so every call must carry these.
        return {"username": self.username, "password": self.password, "port": self.port}

    def unc(self, share: str, *parts: str) -> str:
        return "\\".join([f"\\\\{self.host}", share, *parts])


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def smb_server() -> SmbTestServer:
    server = SmbTestServer(
        host=os.environ.get("DEGREASER_SMB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DEGREASER_SMB_PORT", "1445")),
        username=os.environ.get("DEGREASER_SMB_USER", "degreaser"),
        password=os.environ.get("DEGREASER_SMB_PASS", "degreaser"),
    )
    if not _reachable(server.host, server.port):
        pytest.skip(f"Samba test server not reachable at {server.host}:{server.port}")
    smbclient.register_session(server.host, **server.kwargs)  # type: ignore[arg-type]
    return server


@pytest.fixture
def smb_workdir(smb_server: SmbTestServer) -> Iterator[str]:
    """A fresh folder on the `data` share, removed after the test."""
    path = smb_server.unc("data", f"itest-{uuid.uuid4().hex[:12]}")
    smbclient.makedirs(path, **smb_server.kwargs)  # type: ignore[arg-type]
    yield path
    smbclient.shutil.rmtree(path, **smb_server.kwargs)  # type: ignore[arg-type]
