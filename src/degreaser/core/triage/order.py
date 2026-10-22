"""Traversal order keys and special file names."""

from degreaser.core.project import DEGREASER_DIR
from degreaser.core.sortkey import sort_key
from degreaser.core.sources.base import join, parent

DfsKey = tuple[tuple[int, str, str], ...]


def dfs_key(path: str, is_dir: bool) -> DfsKey:
    """Sortable key giving triage order (docs/triage-engine.md §1.1) for any path.

    Within a folder, files (0) come before subfolders (1), each in natural order with the raw
    name breaking ties. A folder sorts before everything inside it, and its whole subtree
    sorts before its next sibling.
    """
    if path == "":
        return ()
    parts = path.split("/")
    key = tuple((1, sort_key(p), p) for p in parts[:-1])
    last = parts[-1]
    return (*key, (1 if is_dir else 0, sort_key(last), last))


def is_excluded(path: str) -> bool:
    """Degreaser's own folder at the project root, and everything inside it."""
    return path == DEGREASER_DIR or path.startswith(DEGREASER_DIR + "/")


def is_appledouble(name: str, kind: str) -> bool:
    return kind == "file" and name.startswith("._") and len(name) > 2


def companion_of(path: str, name: str) -> str:
    """'a/._x' -> 'a/x' (D20)."""
    return join(parent(path), name[2:])
