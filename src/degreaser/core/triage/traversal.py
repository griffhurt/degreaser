"""Depth-first traversal to the next file that needs a decision (docs/triage-engine.md §1).

Order: within a folder, files then subfolders, each by (sort_key, name). Subtrees that can't
contain an UNTRIAGED file are pruned. Traversal never does I/O: if it needs the children of
a folder that hasn't been listed yet, it raises NeedsListing and the caller lists the folder
(core/scan) and retries. Per-folder queries on the nodes_children index keep this fast
(spikes/resolver: p50 0.12 ms from a cold cursor).
"""

import sqlite3
from dataclasses import dataclass

from degreaser.core.sources.base import ancestors, parent
from degreaser.core.triage.resolver import DirCtx, Resolver


class NeedsListing(Exception):
    """Traversal reached a folder whose children aren't known yet."""

    def __init__(self, path: str) -> None:
        super().__init__(f"folder not listed yet: {path!r}")
        self.path = path


@dataclass(frozen=True, slots=True)
class Lookahead:
    files: list[str]
    needs_listing: str | None  # folder that stopped the lookahead, if any


_FILES_AFTER = (
    "SELECT n.path, n.name, n.kind FROM nodes n LEFT JOIN decisions d ON d.path = n.path"
    " WHERE n.parent_id = ? AND n.is_dir = 0 AND d.path IS NULL"
)
_DIRS_AFTER = "SELECT id, path, listed FROM nodes WHERE parent_id = ? AND is_dir = 1"


class Traversal:
    def __init__(self, conn: sqlite3.Connection, resolver: Resolver) -> None:
        self.conn = conn
        self.resolver = resolver

    # Public ---------------------------------------------------------------------------------

    def first(self) -> str | None:
        """First file needing a decision in the whole tree (also the wrap-around sweep)."""
        root = self._node("")
        assert root is not None, "project tree has no root node"
        return self._first_in_subtree(root[0], "", bool(root[4]))

    def next_from(self, path: str | None, *, inclusive: bool = True) -> str | None:
        """Next file needing a decision at (inclusive) or after `path` in DFS order.

        For a folder, "after" means after its whole subtree; "at" means inside it. If `path`
        no longer exists, the search resumes from its nearest surviving ancestor (§1.6).
        """
        if path is None or path == "":
            return self.first()
        node = self._node(path)
        if node is None:
            for ancestor in reversed(ancestors(path)):
                if self._node(ancestor) is not None:
                    return self.next_from(ancestor, inclusive=True)
            return self.first()
        node_id, parent_id, is_dir, _kind, listed, name, sort_key = node
        dir_path = parent(path)
        if is_dir:
            if inclusive and (hit := self._first_in_subtree(node_id, path, bool(listed))):
                return hit
            return self._continue_after(parent_id, dir_path, (sort_key, name), after_dir=True)
        if inclusive and self.needs_decision(path):
            return path
        return self._continue_after(parent_id, dir_path, (sort_key, name), after_dir=False)

    def lookahead(self, path: str, n: int) -> Lookahead:
        """Up to n files after `path` that need a decision, stopping at unlisted folders."""
        files: list[str] = []
        current = path
        while len(files) < n:
            try:
                nxt = self.next_from(current, inclusive=False)
            except NeedsListing as signal:
                return Lookahead(files, signal.path)
            if nxt is None:
                break
            files.append(nxt)
            current = nxt
        return Lookahead(files, None)

    def needs_decision(self, path: str) -> bool:
        """Whether a single file currently needs a decision."""
        node = self._node(path)
        if node is None or node[2]:
            return False
        if self.conn.execute("SELECT 1 FROM decisions WHERE path = ?", (path,)).fetchone():
            return False
        ctx = self.resolver.dir_ctx(parent(path))
        return not ctx.prunes and self._file_untriaged(ctx, path, node[5], node[3])

    # Internals ------------------------------------------------------------------------------

    def _node(self, path: str) -> tuple | None:
        return self.conn.execute(
            "SELECT id, parent_id, is_dir, kind, listed, name, sort_key FROM nodes WHERE path = ?",
            (path,),
        ).fetchone()

    def _file_untriaged(self, ctx: DirCtx, path: str, name: str, kind: str) -> bool:
        return self.resolver.is_untriaged_candidate(ctx, path, name, kind)

    def _first_file(self, dir_id: int, ctx: DirCtx, after: tuple[str, str] | None) -> str | None:
        sql = _FILES_AFTER
        args: list[object] = [dir_id]
        if after is not None:
            sql += " AND (n.sort_key, n.name) > (?, ?)"
            args.extend(after)
        sql += " ORDER BY n.sort_key, n.name"
        for path, name, kind in self.conn.execute(sql, args):
            if self._file_untriaged(ctx, path, name, kind):
                return path
        return None

    def _scan_subdirs(self, dir_id: int, after: tuple[str, str] | None) -> str | None:
        sql = _DIRS_AFTER
        args: list[object] = [dir_id]
        if after is not None:
            sql += " AND (sort_key, name) > (?, ?)"
            args.extend(after)
        sql += " ORDER BY sort_key, name"
        for sub_id, sub_path, listed in self.conn.execute(sql, args).fetchall():
            if hit := self._first_in_subtree(sub_id, sub_path, bool(listed)):
                return hit
        return None

    def _first_in_subtree(self, dir_id: int, dir_path: str, listed: bool) -> str | None:
        ctx = self.resolver.dir_ctx(dir_path)
        if ctx.prunes:
            return None
        if not listed:
            raise NeedsListing(dir_path)
        return self._first_file(dir_id, ctx, None) or self._scan_subdirs(dir_id, None)

    def _continue_after(
        self,
        dir_id: int | None,
        dir_path: str,
        after: tuple[str, str],
        *,
        after_dir: bool,
    ) -> str | None:
        """Search the rest of folder `dir_id` after a child, then climb to the ancestors."""
        while dir_id is not None:
            ctx = self.resolver.dir_ctx(dir_path)
            if not ctx.prunes:
                hit = None
                if not after_dir:
                    hit = self._first_file(dir_id, ctx, after) or self._scan_subdirs(dir_id, None)
                else:
                    hit = self._scan_subdirs(dir_id, after)
                if hit:
                    return hit
            row = self.conn.execute(
                "SELECT parent_id, sort_key, name FROM nodes WHERE id = ?", (dir_id,)
            ).fetchone()
            dir_id = row[0]
            after, after_dir = (row[1], row[2]), True
            dir_path = parent(dir_path)
        return None
