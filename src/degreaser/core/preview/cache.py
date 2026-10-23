"""Temporary copies of remote files for preview and Open (docs/previews.md §3, §4).

Local sources return the real path and nothing is copied. Remote files download here; the
DownloadQueue (core/preview/downloads.py) adds priorities and concurrency on top of `fetch`.
"""

import contextlib
import hashlib
import os
import shutil
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from degreaser.core import paths
from degreaser.core.preview.loaders import CancelToken
from degreaser.core.sources.base import Entry, FileSource
from degreaser.core.sources.errors import SourceError

DOWNLOAD_CHUNK = 4 * 1024 * 1024  # an SMB read_range of this size is one round trip

Progress = Callable[[int, int], None]  # (bytes done, bytes total)


@dataclass(slots=True)
class _CacheEntry:
    path: Path
    decided: bool = False
    on_screen: bool = False
    open_pending: bool = False


class PreviewCache:
    """One per open project. Thread-safe."""

    def __init__(self, cache_dir: Path) -> None:
        self.dir = cache_dir
        self._entries: dict[str, _CacheEntry] = {}
        self._retry: set[Path] = set()
        self._lock = threading.Lock()
        self._file_locks: dict[str, threading.Lock] = {}

    def path_for(self, rel: str) -> Path:
        digest = hashlib.sha1(rel.encode("utf-8", "surrogatepass")).hexdigest()[:20]
        suffix = Path(rel).suffix[:16]
        return self.dir / f"{digest}{suffix}"

    def fetch(
        self,
        source: FileSource,
        rel: str,
        entry: Entry,
        progress: Progress | None = None,
        cancel: CancelToken | None = None,
    ) -> Path:
        """A local path with the file's full contents, downloading into the cache if needed.

        One download per file at a time: a second caller waits for the first (checking its
        own cancel token meanwhile). A cancelled download keeps its `.part` file, and the next
        fetch resumes from there, so a preview the user skipped past can carry on in the
        background without starting over.
        """
        local = source.local_path(rel)
        if local is not None:
            return local
        lock = self._lock_for(rel)
        while not lock.acquire(timeout=0.1):
            if cancel:
                cancel.check()
        try:
            if (done_path := self.cached(rel)) is not None:
                return done_path
            return self._download(source, rel, entry, progress, cancel)
        finally:
            lock.release()

    def _lock_for(self, rel: str) -> threading.Lock:
        with self._lock:
            return self._file_locks.setdefault(rel, threading.Lock())

    def _download(
        self,
        source: FileSource,
        rel: str,
        entry: Entry,
        progress: Progress | None,
        cancel: CancelToken | None,
    ) -> Path:
        target = self.path_for(rel)
        self.dir.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        total = entry.size or 0
        done = part.stat().st_size if part.exists() else 0
        if done > total:
            part.unlink()
            done = 0
        with open(part, "ab") as out:
            while done < total:
                if cancel:
                    cancel.check()
                data = source.read_range(rel, done, min(DOWNLOAD_CHUNK, total - done))
                if not data:
                    break
                out.write(data)
                done += len(data)
                if progress:
                    progress(done, total)
        if done < total:
            part.unlink(missing_ok=True)
            raise SourceError(f"{rel} got shorter while it was downloading", rel)
        os.replace(part, target)
        # Read-only: edits made in another app must not look like they'd be saved back.
        target.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        with self._lock:
            self._entries[rel] = _CacheEntry(target)
        return target

    def size_bytes(self) -> int:
        """Bytes on disk in the cache, including partial downloads."""
        if not self.dir.exists():
            return 0
        return sum(p.stat().st_size for p in self.dir.iterdir() if p.is_file())

    def rels(self) -> list[str]:
        """Files with a complete copy in the cache."""
        with self._lock:
            return list(self._entries)

    def cached(self, rel: str) -> Path | None:
        """The complete local copy of rel, if one has been downloaded."""
        with self._lock:
            known = self._entries.get(rel)
        return known.path if known is not None and known.path.exists() else None

    def mark(
        self,
        rel: str,
        *,
        decided: bool | None = None,
        on_screen: bool | None = None,
        open_pending: bool | None = None,
    ) -> None:
        with self._lock:
            e = self._entries.get(rel)
            if e is None:
                return
            if decided is not None:
                e.decided = decided
            if on_screen is not None:
                e.on_screen = on_screen
            if open_pending is not None:
                e.open_pending = open_pending

    def sweep(self) -> None:
        """Delete copies that are decided, off screen and not waiting to be opened."""
        with self._lock:
            done = [
                rel
                for rel, e in self._entries.items()
                if e.decided and not e.on_screen and not e.open_pending
            ]
            victims = [self._entries.pop(rel).path for rel in done] + list(self._retry)
            self._retry.clear()
        for path in victims:
            if not _remove(path):
                with self._lock:
                    self._retry.add(path)  # e.g. still open in another app on Windows

    def clear_all(self) -> None:
        """Wipe the whole cache folder (project close)."""
        with self._lock:
            self._entries.clear()
            self._retry.clear()
        wipe_dir(self.dir)


def _remove(path: Path) -> bool:
    try:
        with contextlib.suppress(FileNotFoundError):
            path.chmod(stat.S_IWUSR | stat.S_IRUSR)
            path.unlink()
        return True
    except OSError:
        return False


def wipe_dir(directory: Path) -> None:
    if not directory.exists():
        return

    def make_writable_and_retry(func: Callable[..., object], path: str, _exc: object) -> None:
        with contextlib.suppress(OSError):
            os.chmod(path, stat.S_IWUSR | stat.S_IRUSR)
            func(path)

    shutil.rmtree(directory, onexc=make_writable_and_retry)


def wipe_all_project_caches() -> None:
    """Crash recovery at app start: clear every project's cache folder."""
    root = paths.projects_dir()
    if root.exists():
        for cache in root.glob("*/cache"):
            wipe_dir(cache)
