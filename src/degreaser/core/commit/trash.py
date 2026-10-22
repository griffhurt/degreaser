"""The Degreaser trash, `.degreaser/trash/<batch>/<path>` (docs/scanning-and-commit.md §4)."""

import errno
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from degreaser.core.project import DEGREASER_DIR
from degreaser.core.sources.base import FileSource, join
from degreaser.core.sources.errors import NotFound

TRASH_DIR = f"{DEGREASER_DIR}/trash"

CROSS_DEVICE_HINT = (
    "This share spans more than one filesystem, so files can't be moved into the Degreaser"
    " trash. Switch this project to 'Delete permanently' to delete them instead."
)


@dataclass(frozen=True, slots=True)
class TrashBatch:
    name: str
    path: str
    created: datetime | None
    files: int
    bytes: int


def batch_name(commit_uuid: str, when: float) -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(when))}-{commit_uuid[:8]}"


def batch_path(commit_uuid: str, when: float) -> str:
    return f"{TRASH_DIR}/{batch_name(commit_uuid, when)}"


def _parse_created(name: str) -> datetime | None:
    try:
        return datetime.strptime(name[:15], "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def _totals(source: FileSource, path: str) -> tuple[int, int]:
    files = size = 0
    stack = [path]
    while stack:
        current = stack.pop()
        for e in source.list_dir(current):
            if e.is_dir:
                stack.append(join(current, e.name))
            else:
                files += 1
                size += e.size or 0
    return files, size


def list_trash_batches(source: FileSource) -> list[TrashBatch]:
    """Trash batches, newest first, with their sizes (walks each batch)."""
    try:
        entries = source.list_dir(TRASH_DIR)
    except NotFound:
        return []
    batches = []
    for e in entries:
        path = join(TRASH_DIR, e.name)
        if e.is_dir:
            files, size = _totals(source, path)
        else:
            files, size = 1, e.size or 0
        batches.append(TrashBatch(e.name, path, _parse_created(e.name), files, size))
    return sorted(batches, key=lambda b: b.name, reverse=True)


def empty_trash(source: FileSource, names: Iterable[str] | None = None) -> int:
    """Permanently delete the named batches (all of them if names is None)."""
    targets = [b.name for b in list_trash_batches(source)] if names is None else list(names)
    removed = 0
    for name in targets:
        if "/" in name or name in ("", ".", ".."):
            raise ValueError(f"invalid trash batch name: {name!r}")
        try:
            source.delete_tree(join(TRASH_DIR, name))
            removed += 1
        except NotFound:
            pass
    return removed


def is_cross_device_error(exc: BaseException) -> bool:
    """True if a rename failed because source and destination are on different filesystems."""
    for e in (exc, exc.__cause__):
        # Windows reports ERROR_NOT_SAME_DEVICE (17) as winerror.
        if isinstance(e, OSError) and (e.errno == errno.EXDEV or getattr(e, "winerror", 0) == 17):
            return True
    text = str(exc).lower()
    return "cross-device" in text or "different disk drive" in text
