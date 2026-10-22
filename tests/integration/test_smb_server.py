"""Sanity checks that the Samba test container behaves as the app will expect."""

import pytest
import smbclient

from tests.integration.conftest import SmbTestServer

pytestmark = pytest.mark.smb


def test_write_list_read_roundtrip(smb_server: SmbTestServer, smb_workdir: str) -> None:
    kw = smb_server.kwargs
    with smbclient.open_file(smb_workdir + "\\héllo wörld.txt", mode="wb", **kw) as f:  # type: ignore[arg-type]
        f.write(b"degreaser")
    smbclient.mkdir(smb_workdir + "\\sub", **kw)  # type: ignore[arg-type]

    entries = {e.name: e for e in smbclient.scandir(smb_workdir, **kw)}  # type: ignore[arg-type]

    assert set(entries) == {"héllo wörld.txt", "sub"}
    assert entries["sub"].is_dir()
    assert entries["héllo wörld.txt"].smb_info.end_of_file == len(b"degreaser")


def test_range_read(smb_server: SmbTestServer, smb_workdir: str) -> None:
    kw = smb_server.kwargs
    path = smb_workdir + "\\data.bin"
    with smbclient.open_file(path, mode="wb", **kw) as f:  # type: ignore[arg-type]
        f.write(bytes(range(256)) * 16)

    with smbclient.open_file(path, mode="rb", **kw) as f:  # type: ignore[arg-type]
        f.seek(1000)
        assert f.read(4) == bytes([1000 % 256, 1001 % 256, 1002 % 256, 1003 % 256])


def test_server_side_rename(smb_server: SmbTestServer, smb_workdir: str) -> None:
    kw = smb_server.kwargs
    smbclient.makedirs(smb_workdir + "\\trash\\batch", **kw)  # type: ignore[arg-type]
    with smbclient.open_file(smb_workdir + "\\a.txt", mode="wb", **kw) as f:  # type: ignore[arg-type]
        f.write(b"x")

    smbclient.rename(smb_workdir + "\\a.txt", smb_workdir + "\\trash\\batch\\a.txt", **kw)  # type: ignore[arg-type]

    assert smbclient.listdir(smb_workdir + "\\trash\\batch", **kw) == ["a.txt"]  # type: ignore[arg-type]
