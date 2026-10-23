"""Duplicate groups in memory, for the banner, details panel, review screen and resolver."""

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from degreaser.core.triage.order import dfs_key


@dataclass
class DuplicateIndex:
    """Files with at least one byte-identical copy, grouped by full hash."""

    groups: dict[bytes, list[str]] = field(default_factory=dict)  # copies in DFS order
    by_path: dict[str, bytes] = field(default_factory=dict)

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> DuplicateIndex:
        members: dict[bytes, list[str]] = defaultdict(list)
        for path, digest in conn.execute(
            "SELECT n.path, h.full FROM file_hashes h JOIN nodes n ON n.id = h.node_id"
            " WHERE h.full IN (SELECT full FROM file_hashes WHERE full IS NOT NULL"
            "                  GROUP BY full HAVING count(*) > 1)"
        ):
            members[digest].append(path)
        groups = {
            digest: sorted(paths, key=lambda p: dfs_key(p, False))
            for digest, paths in members.items()
            if len(paths) > 1  # a copy may have left the tree since
        }
        by_path = {p: digest for digest, paths in groups.items() for p in paths}
        return cls(groups, by_path)

    def copies(self, path: str) -> list[str]:
        """The other identical files, in traversal order (empty if none)."""
        digest = self.by_path.get(path)
        return [p for p in self.groups[digest] if p != path] if digest is not None else []

    def digest(self, path: str) -> bytes | None:
        return self.by_path.get(path)

    def __len__(self) -> int:
        return len(self.groups)
