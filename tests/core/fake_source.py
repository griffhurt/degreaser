"""Test helpers for scan/commit: a fault-injecting FileSource and tree/DB builders."""

import time
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, BinaryIO

from degreaser.core.db.connection import tx
from degreaser.core.project import Project
from degreaser.core.scan import Indexer
from degreaser.core.sources.base import Entry
from degreaser.core.sources.errors import TransientSourceError
from degreaser.core.sources.local import LocalSource


class SimulatedCrash(Exception):
    """Not a SourceError: escapes the runner like a real crash would, leaving the op pending."""


class FaultySource:
    """Wraps LocalSource and injects failures by (method, path).

    - `fail[(method, path)] = exc` raises exc on every matching call.
    - `transient[(method, path)] = n` raises TransientSourceError for the next n calls.
    - `crash_after_mutations = n` raises SimulatedCrash on the (n+1)th mutating call.
    """

    MUTATING = ("rename", "delete_file", "delete_tree", "remove_dir", "write_file_atomic")

    def __init__(self, root: Path) -> None:
        self.inner = LocalSource(root)
        self.capabilities = self.inner.capabilities
        self.fail: dict[tuple[str, str], Exception] = {}
        self.transient: Counter[tuple[str, str]] = Counter()
        self.crash_after_mutations: int | None = None
        self.mutations = 0
        self.calls: list[tuple[str, str]] = []

    def _check(self, method: str, path: str) -> None:
        self.calls.append((method, path))
        key = (method, path)
        if key in self.fail:
            raise self.fail[key]
        if self.transient[key] > 0:
            self.transient[key] -= 1
            raise TransientSourceError(f"simulated connection drop on {method} {path}", path)
        if method in self.MUTATING and self.crash_after_mutations is not None:
            if self.mutations >= self.crash_after_mutations:
                raise SimulatedCrash(f"crash before {method} {path}")
            self.mutations += 1

    def connect(self) -> None:
        self.inner.connect()

    def close(self) -> None:
        self.inner.close()

    def describe(self) -> str:
        return f"faulty:{self.inner.describe()}"

    def list_dir(self, rel: str) -> list[Entry]:
        self._check("list_dir", rel)
        return self.inner.list_dir(rel)

    def stat(self, rel: str) -> Entry:
        self._check("stat", rel)
        return self.inner.stat(rel)

    def read_range(self, rel: str, offset: int, length: int) -> bytes:
        self._check("read_range", rel)
        return self.inner.read_range(rel, offset, length)

    def open_read(self, rel: str) -> BinaryIO:
        self._check("open_read", rel)
        return self.inner.open_read(rel)

    def local_path(self, rel: str) -> Path | None:
        return self.inner.local_path(rel)

    def rename(self, src: str, dst: str) -> None:
        self._check("rename", src)
        self.inner.rename(src, dst)

    def makedirs(self, rel: str) -> None:
        self._check("makedirs", rel)
        self.inner.makedirs(rel)

    def delete_file(self, rel: str) -> None:
        self._check("delete_file", rel)
        self.inner.delete_file(rel)

    def delete_tree(self, rel: str) -> None:
        self._check("delete_tree", rel)
        self.inner.delete_tree(rel)

    def remove_dir(self, rel: str) -> None:
        self._check("remove_dir", rel)
        self.inner.remove_dir(rel)

    def write_file_atomic(self, rel: str, data: bytes) -> None:
        self._check("write_file_atomic", rel)
        self.inner.write_file_atomic(rel, data)


def build_tree(root: Path, spec: Mapping[str, Any]) -> None:
    """{"a.txt": b"..." | str, "dir": {...}} → files and folders under root."""
    root.mkdir(parents=True, exist_ok=True)
    for name, value in spec.items():
        path = root / name
        if isinstance(value, Mapping):
            build_tree(path, value)
        else:
            path.write_bytes(value.encode() if isinstance(value, str) else value)


def make_project(tmp_path: Path, spec: Mapping[str, Any]) -> tuple[Project, Path]:
    root = tmp_path / "files"
    build_tree(root, spec)
    return Project.create("Test", {"type": "local", "path": str(root)}), root


def index_all(project: Project, source: Any) -> None:
    summary = Indexer(project.new_connection, source, workers=2).run()
    assert summary.status == "completed", summary


def decide(
    project: Project,
    path: str,
    action: str,
    *,
    is_dir: bool = False,
    seen: tuple[int | None, float | None] | None = None,
    decided_at: float | None = None,
) -> None:
    """Record a decision directly (the triage engine isn't part of these tests)."""
    conn = project.conn
    if seen is None and not is_dir:
        row = conn.execute("SELECT size, mtime FROM nodes WHERE path = ?", (path,)).fetchone()
        seen = (row["size"], row["mtime"]) if row else (None, None)
    seen = seen or (None, None)
    with tx(conn):
        conn.execute(
            "INSERT OR REPLACE INTO decisions(path, is_dir, action, decided_at, seen_size,"
            " seen_mtime) VALUES (?, ?, ?, ?, ?, ?)",
            (path, int(is_dir), action, decided_at or time.time(), *seen),
        )


def no_sleep(_: float) -> None:
    pass


def node_paths(project: Project, where: str = "1") -> set[str]:
    return {r[0] for r in project.conn.execute(f"SELECT path FROM nodes WHERE {where}")}


def touch(path: Path, data: bytes, mtime_delta: float = 10.0) -> None:
    """Rewrite a file and push its mtime forward so it's detectably changed."""
    st = path.stat()
    path.write_bytes(data)
    import os

    os.utime(path, (st.st_atime, st.st_mtime + mtime_delta))


Waiter = Callable[[float, Any], None]
