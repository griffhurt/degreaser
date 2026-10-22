"""The FileSource protocol and project-relative path helpers.

Paths are relative POSIX strings; the project root is "". They are kept exactly as the
source returns names (no Unicode normalisation) because identity must match the server.
"""

from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import BinaryIO, Literal, Protocol

Kind = Literal["file", "dir", "symlink", "other"]


@dataclass(frozen=True, slots=True)
class Entry:
    name: str
    kind: Kind
    size: int | None  # None for dirs
    mtime: float | None  # POSIX seconds
    ctime: float | None  # creation time where the source provides it

    @property
    def is_dir(self) -> bool:
        return self.kind == "dir"


class Capability(Enum):
    RANGE_READ = auto()
    SERVER_RENAME = auto()
    LOCAL_PATH = auto()
    WRITE = auto()


class FileSource(Protocol):
    """Blocking file access. Only ever called from worker threads, never the UI thread."""

    capabilities: frozenset[Capability]

    def connect(self) -> None: ...
    def close(self) -> None: ...
    def describe(self) -> str: ...

    def list_dir(self, rel: str) -> list[Entry]: ...
    def stat(self, rel: str) -> Entry: ...
    def read_range(self, rel: str, offset: int, length: int) -> bytes: ...
    def open_read(self, rel: str) -> BinaryIO: ...
    def local_path(self, rel: str) -> Path | None: ...

    def rename(self, src: str, dst: str) -> None: ...
    def makedirs(self, rel: str) -> None: ...
    def delete_file(self, rel: str) -> None: ...
    def delete_tree(self, rel: str) -> None: ...
    def remove_dir(self, rel: str) -> None: ...
    def write_file_atomic(self, rel: str, data: bytes) -> None: ...


def check_rel(rel: str) -> str:
    """Validate a project-relative path and return it unchanged."""
    if rel == "":
        return rel
    if rel.startswith("/") or rel.endswith("/") or "\\" in rel or "\0" in rel:
        raise ValueError(f"invalid relative path: {rel!r}")
    if any(part in ("", ".", "..") for part in rel.split("/")):
        raise ValueError(f"invalid relative path: {rel!r}")
    return rel


def join(rel: str, name: str) -> str:
    return name if rel == "" else f"{rel}/{name}"


def parent(rel: str) -> str:
    return rel.rpartition("/")[0]


def basename(rel: str) -> str:
    return rel.rpartition("/")[2]


def ancestors(rel: str) -> list[str]:
    """Proper ancestors from the root down, excluding rel itself: "a/b/c" -> ["", "a", "a/b"]."""
    if rel == "":
        return []
    parts = rel.split("/")
    return ["", *("/".join(parts[:i]) for i in range(1, len(parts)))]


def is_within(rel: str, folder: str) -> bool:
    """True if rel is folder itself or anywhere beneath it."""
    return folder == "" or rel == folder or rel.startswith(folder + "/")
