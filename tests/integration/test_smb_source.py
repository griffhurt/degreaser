"""SmbSource against the Samba container: the LocalSource contract plus SMB specifics."""

import os
from collections.abc import Iterator

import pytest
import smbclient

from degreaser.core.sources import (
    AlreadyExists,
    AuthenticationFailed,
    NotFound,
    TransientSourceError,
)
from degreaser.core.sources import smb as smb_module
from degreaser.core.sources.smb import SmbConfig, SmbSource, connected, normalise_share_path
from tests.integration.conftest import SmbTestServer

pytestmark = pytest.mark.smb


def _config(server: SmbTestServer, workdir: str, **overrides: object) -> SmbConfig:
    values: dict[str, object] = {
        "host": server.host,
        "share": "data",
        "path": workdir.rsplit("\\", 1)[1],
        "username": server.username,
        "port": server.port,
    }
    return SmbConfig(**(values | overrides))  # type: ignore[arg-type]


@pytest.fixture
def source(smb_server: SmbTestServer, smb_workdir: str) -> Iterator[SmbSource]:
    kw = smb_server.kwargs
    smbclient.makedirs(smb_workdir + "\\sub\\deep", **kw)  # type: ignore[arg-type]
    for name, data in (("a.txt", b"hello world"), ("sub\\b.bin", bytes(range(256)))):
        with smbclient.open_file(f"{smb_workdir}\\{name}", mode="wb", **kw) as f:  # type: ignore[arg-type]
            f.write(data)
    src = SmbSource(_config(smb_server, smb_workdir), smb_server.password)
    src.connect()
    yield src
    src.close()


def test_list_dir_and_stat(source: SmbSource) -> None:
    entries = {e.name: e for e in source.list_dir("")}
    assert set(entries) == {"a.txt", "sub"}
    assert entries["a.txt"].kind == "file" and entries["a.txt"].size == 11
    assert entries["a.txt"].mtime and entries["a.txt"].mtime > 1e9
    assert entries["sub"].is_dir and entries["sub"].size is None
    assert source.stat("sub/b.bin").size == 256
    assert source.stat("sub").kind == "dir"
    assert source.stat("").kind == "dir"
    assert source.list_dir("sub/deep") == []


def test_unicode_and_spaces_round_trip(source: SmbSource) -> None:
    name = "héllo wörld – ✓.txt"
    source.write_file_atomic(name, b"x")
    assert name in {e.name for e in source.list_dir("")}
    assert source.read_range(name, 0, 10) == b"x"


def test_read_range_edges(source: SmbSource) -> None:
    assert source.read_range("sub/b.bin", 10, 3) == bytes([10, 11, 12])
    assert source.read_range("sub/b.bin", 250, 100) == bytes(range(250, 256))  # short at EOF
    assert source.read_range("sub/b.bin", 256, 10) == b""
    assert source.read_range("sub/b.bin", 1000, 10) == b""
    source.write_file_atomic("empty", b"")
    assert source.read_range("empty", 0, 10) == b""


def test_large_read_and_write(source: SmbSource) -> None:
    data = os.urandom(9 * 1024 * 1024 + 123)  # more than one compound each way
    source.write_file_atomic("big.bin", data)
    assert source.stat("big.bin").size == len(data)
    assert source.read_range("big.bin", 0, len(data)) == data
    assert source.read_range("big.bin", 5_000_000, 4_000_000) == data[5_000_000:9_000_000]


def test_open_read_is_seekable(source: SmbSource) -> None:
    with source.open_read("sub/b.bin") as f:
        f.seek(100)
        assert f.read(2) == bytes([100, 101])
        f.seek(-1, os.SEEK_END)
        assert f.read() == bytes([255])


def test_errors_are_mapped(source: SmbSource) -> None:
    with pytest.raises(NotFound):
        source.list_dir("missing")
    with pytest.raises(NotFound):
        source.stat("missing.txt")
    with pytest.raises(NotFound):
        source.delete_file("missing.txt")
    with pytest.raises(NotFound):
        source.read_range("nope/x.bin", 0, 1)


def test_rename_never_overwrites(source: SmbSource) -> None:
    source.write_file_atomic("c.txt", b"c")
    with pytest.raises(AlreadyExists):
        source.rename("a.txt", "c.txt")
    source.makedirs(".degreaser/trash/x")
    source.rename("a.txt", ".degreaser/trash/x/a.txt")
    assert source.read_range(".degreaser/trash/x/a.txt", 0, 100) == b"hello world"
    with pytest.raises(NotFound):
        source.stat("a.txt")
    source.rename("sub", ".degreaser/trash/x/sub")  # folders move whole
    assert source.stat(".degreaser/trash/x/sub/b.bin").size == 256


def test_remove_dir_only_when_empty(source: SmbSource) -> None:
    with pytest.raises(AlreadyExists):
        source.remove_dir("sub")
    source.remove_dir("sub/deep")
    assert {e.name for e in source.list_dir("sub")} == {"b.bin"}


def test_delete_tree(source: SmbSource) -> None:
    source.write_file_atomic("sub/deep/c.txt", b"c")
    source.delete_tree("sub")
    assert {e.name for e in source.list_dir("")} == {"a.txt"}
    with pytest.raises(ValueError):
        source.delete_tree("")


def test_write_file_atomic_replaces_and_leaves_no_temp(source: SmbSource) -> None:
    source.makedirs(".degreaser")
    source.write_file_atomic(".degreaser/catalog.json", b"{}")
    source.write_file_atomic(".degreaser/catalog.json", b'{"v": 2}')
    assert source.read_range(".degreaser/catalog.json", 0, 100) == b'{"v": 2}'
    assert [e.name for e in source.list_dir(".degreaser")] == ["catalog.json"]


def test_large_folder_falls_back_to_paging(
    source: SmbSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(40):
        source.write_file_atomic(f"sub/deep/file-{i:03}.txt", b"")
    monkeypatch.setattr(smb_module, "LIST_BUFFER", 1024)  # ~10 entries per buffer
    names = sorted(e.name for e in source.list_dir("sub/deep"))
    assert names == [f"file-{i:03}.txt" for i in range(40)]


def test_reconnects_after_the_connection_drops(source: SmbSource) -> None:
    link = source._link
    assert link is not None
    link.connection.disconnect(close=False)  # as if the network went away
    assert {e.name for e in source.list_dir("")} == {"a.txt", "sub"}
    assert source._link is not link


def test_project_root_inside_share_is_confined(smb_server: SmbTestServer, smb_workdir: str) -> None:
    config = _config(smb_server, smb_workdir)
    with connected(config, smb_server.password) as src:
        src.write_file_atomic("only-here.txt", b"1")
    names = smbclient.listdir(smb_workdir, **smb_server.kwargs)  # type: ignore[arg-type]
    assert names == ["only-here.txt"]


def test_connection_errors(smb_server: SmbTestServer, smb_workdir: str) -> None:
    with pytest.raises(AuthenticationFailed):
        SmbSource(_config(smb_server, smb_workdir), "wrong password").connect()
    with pytest.raises(NotFound, match="share"):
        SmbSource(_config(smb_server, smb_workdir, share="nope"), smb_server.password).connect()
    with pytest.raises(NotFound):
        SmbSource(_config(smb_server, smb_workdir, path="missing"), smb_server.password).connect()
    with pytest.raises(TransientSourceError):
        SmbSource(_config(smb_server, smb_workdir, port=1), smb_server.password).connect()


def test_config_round_trip() -> None:
    config = SmbConfig("nas", "backups", "Laptops/2014", "griff", "HOME", 1445)
    assert SmbConfig.from_source(config.to_source()) == config
    assert config.describe() == "smb://nas:1445/backups/Laptops/2014"
    assert config.login == "HOME\\griff"
    assert normalise_share_path("\\Laptops\\2014/") == "Laptops/2014"


def test_probe_is_read_only_and_reports_quirks(
    source: SmbSource, smb_server: SmbTestServer, smb_workdir: str
) -> None:
    from degreaser.core.sources.smb_probe import probe, summarise

    source.write_file_atomic("Case.txt", b"1")
    source.write_file_atomic("name\uf03f.txt", b"1")  # how a Mac writes "name?.txt"
    before = sorted(e.name for e in source.list_dir(""))
    result = probe(_config(smb_server, smb_workdir), smb_server.password, report=lambda _l: None)
    assert result["listing"]["complete"] and result["listing"]["files"] == 4
    assert result["connection"]["dialect"].startswith("SMB")
    assert result["case_sensitive"] is False  # Samba's default
    assert result["findings"]["names_windows_cant_hold"]
    assert any("Windows can't use" in note for note in summarise(result))
    assert sorted(e.name for e in source.list_dir("")) == before  # nothing written
