"""LocalSource: a folder on a local or OS-mounted filesystem."""

import contextlib
import errno
import os
import shutil
import stat as stat_mod
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from degreaser.core.sources.base import Capability, Entry, Kind, check_rel
from degreaser.core.sources.errors import (
    AlreadyExists,
    NotADirectory,
    NotFound,
    PermissionDenied,
    SourceError,
)


@contextlib.contextmanager
def _mapped_errors(rel: str) -> Iterator[None]:
    try:
        yield
    except FileNotFoundError as exc:
        raise NotFound(str(exc), rel) from exc
    except PermissionError as exc:
        raise PermissionDenied(str(exc), rel) from exc
    except FileExistsError as exc:
        raise AlreadyExists(str(exc), rel) from exc
    except NotADirectoryError as exc:
        raise NotADirectory(str(exc), rel) from exc
    except OSError as exc:
        if exc.errno == errno.ENOTEMPTY:
            raise AlreadyExists(str(exc), rel) from exc
        raise SourceError(str(exc), rel) from exc


def _kind(mode: int) -> Kind:
    if stat_mod.S_ISLNK(mode):
        return "symlink"
    if stat_mod.S_ISDIR(mode):
        return "dir"
    if stat_mod.S_ISREG(mode):
        return "file"
    return "other"


def _entry(name: str, st: os.stat_result) -> Entry:
    kind = _kind(st.st_mode)
    return Entry(
        name=name,
        kind=kind,
        size=None if kind == "dir" else st.st_size,
        mtime=st.st_mtime,
        ctime=getattr(st, "st_birthtime", None),
    )


class LocalSource:
    capabilities = frozenset(
        {Capability.RANGE_READ, Capability.SERVER_RENAME, Capability.LOCAL_PATH, Capability.WRITE}
    )

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _abs(self, rel: str) -> Path:
        check_rel(rel)
        return self.root.joinpath(*rel.split("/")) if rel else self.root

    def connect(self) -> None:
        with _mapped_errors(""):
            if not self.root.is_dir():
                raise NotFound(f"folder not found: {self.root}", "")

    def close(self) -> None:
        pass

    def describe(self) -> str:
        return str(self.root)

    def list_dir(self, rel: str) -> list[Entry]:
        with _mapped_errors(rel), os.scandir(self._abs(rel)) as it:
            # Symlinks are reported as such and never followed.
            return [_entry(e.name, e.stat(follow_symlinks=False)) for e in it]

    def stat(self, rel: str) -> Entry:
        path = self._abs(rel)
        with _mapped_errors(rel):
            return _entry(path.name, path.lstat())

    def read_range(self, rel: str, offset: int, length: int) -> bytes:
        with _mapped_errors(rel), open(self._abs(rel), "rb") as f:
            f.seek(offset)
            return f.read(length)

    def open_read(self, rel: str) -> BinaryIO:
        with _mapped_errors(rel):
            return open(self._abs(rel), "rb")

    def local_path(self, rel: str) -> Path | None:
        return self._abs(rel)

    def rename(self, src: str, dst: str) -> None:
        dst_path = self._abs(dst)
        with _mapped_errors(src):
            # os.rename silently replaces files on POSIX; never overwrite.
            if dst_path.exists() or dst_path.is_symlink():
                raise AlreadyExists(f"destination exists: {dst}", dst)
            os.rename(self._abs(src), dst_path)

    def makedirs(self, rel: str) -> None:
        with _mapped_errors(rel):
            self._abs(rel).mkdir(parents=True, exist_ok=True)

    def delete_file(self, rel: str) -> None:
        with _mapped_errors(rel):
            self._abs(rel).unlink()

    def delete_tree(self, rel: str) -> None:
        if rel == "":
            raise ValueError("refusing to delete the project root")
        path = self._abs(rel)
        with _mapped_errors(rel):
            if path.is_symlink() or not path.is_dir():
                path.unlink()
            else:
                shutil.rmtree(path)

    def remove_dir(self, rel: str) -> None:
        """Remove an empty directory; raises AlreadyExists if it isn't empty."""
        with _mapped_errors(rel):
            self._abs(rel).rmdir()

    def write_file_atomic(self, rel: str, data: bytes) -> None:
        path = self._abs(rel)
        tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:8]}")
        with _mapped_errors(rel):
            try:
                tmp.write_bytes(data)
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
