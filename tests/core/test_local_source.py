import os
import sys
from pathlib import Path

import pytest

from degreaser.core.sources import AlreadyExists, NotFound
from degreaser.core.sources.base import check_rel
from degreaser.core.sources.local import LocalSource


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "root"
    (r / "sub" / "deep").mkdir(parents=True)
    (r / "a.txt").write_bytes(b"hello world")
    (r / "sub" / "b.bin").write_bytes(bytes(range(256)))
    return r


def test_list_dir_and_stat(root: Path) -> None:
    src = LocalSource(root)
    entries = {e.name: e for e in src.list_dir("")}
    assert set(entries) == {"a.txt", "sub"}
    assert entries["a.txt"].kind == "file" and entries["a.txt"].size == 11
    assert entries["sub"].is_dir and entries["sub"].size is None
    assert src.stat("sub/b.bin").size == 256


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlinks_are_not_followed(root: Path) -> None:
    os.symlink(root / "sub", root / "link")
    src = LocalSource(root)
    assert src.stat("link").kind == "symlink"
    src.delete_tree("link")
    assert (root / "sub").is_dir()


def test_read_range(root: Path) -> None:
    assert LocalSource(root).read_range("sub/b.bin", 10, 3) == bytes([10, 11, 12])


def test_errors_are_mapped(root: Path) -> None:
    src = LocalSource(root)
    with pytest.raises(NotFound):
        src.list_dir("missing")
    with pytest.raises(NotFound):
        src.delete_file("missing.txt")


def test_rename_never_overwrites(root: Path) -> None:
    src = LocalSource(root)
    (root / "c.txt").write_bytes(b"c")
    with pytest.raises(AlreadyExists):
        src.rename("a.txt", "c.txt")
    src.makedirs(".degreaser/trash/x")
    src.rename("a.txt", ".degreaser/trash/x/a.txt")
    assert (root / ".degreaser/trash/x/a.txt").read_bytes() == b"hello world"


def test_remove_dir_only_when_empty(root: Path) -> None:
    src = LocalSource(root)
    with pytest.raises(AlreadyExists):
        src.remove_dir("sub")
    src.remove_dir("sub/deep")
    assert not (root / "sub" / "deep").exists()


def test_delete_tree_refuses_root(root: Path) -> None:
    with pytest.raises(ValueError):
        LocalSource(root).delete_tree("")


def test_write_file_atomic(root: Path) -> None:
    src = LocalSource(root)
    src.write_file_atomic("cat.json", b"{}")
    src.write_file_atomic("cat.json", b'{"v": 2}')
    assert (root / "cat.json").read_bytes() == b'{"v": 2}'
    assert [p.name for p in root.iterdir() if ".tmp-" in p.name] == []


@pytest.mark.parametrize("bad", ["/abs", "a/../b", "a//b", "a/", "./a", "a\\b"])
def test_check_rel_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        check_rel(bad)
