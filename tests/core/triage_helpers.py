"""Build small project trees directly in SQLite for triage-engine tests."""

import json
import sqlite3
import time
from collections.abc import Sequence

from degreaser.core.categories.store import CategoryStore
from degreaser.core.db.connection import connect
from degreaser.core.sortkey import sort_key
from degreaser.core.sources.base import basename, parent


class Tree:
    """Paths ending in '/' are folders; missing parent folders are created (listed)."""

    def __init__(
        self, conn: sqlite3.Connection | None = None, settings: dict | None = None
    ) -> None:
        self.conn = conn or connect(":memory:")
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('settings', ?)",
            (json.dumps(settings or {}),),
        )
        self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('state_revision', '0')")
        if self.id("") is None:
            self.conn.execute(
                "INSERT INTO nodes(parent_id, path, name, is_dir, kind, sort_key, listed)"
                " VALUES (NULL, '', '', 1, 'dir', '', 1)"
            )
        self.store = CategoryStore(self.conn)

    def id(self, path: str) -> int | None:
        row = self.conn.execute("SELECT id FROM nodes WHERE path = ?", (path,)).fetchone()
        return None if row is None else row[0]

    def add(
        self,
        *paths: str,
        size: int = 10,
        mtime: float = 1000.0,
        listed: bool = True,
        kind: str | None = None,
        flag_new: bool = False,
        flag_changed: bool = False,
        node_id: int | None = None,
    ) -> Tree:
        for raw in paths:
            is_dir = raw.endswith("/")
            path = raw.rstrip("/")
            self._ensure_dir(parent(path))
            if self.id(path) is not None:
                continue
            self.conn.execute(
                "INSERT INTO nodes(id, parent_id, path, name, is_dir, kind, size, mtime, sort_key,"
                " listed, flag_new, flag_changed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    node_id,
                    self.id(parent(path)),
                    path,
                    basename(path),
                    int(is_dir),
                    kind or ("dir" if is_dir else "file"),
                    None if is_dir else size,
                    mtime,
                    sort_key(basename(path)),
                    int(listed) if is_dir else 0,
                    int(flag_new),
                    int(flag_changed),
                ),
            )
        return self

    def _ensure_dir(self, path: str) -> None:
        if path == "" or self.id(path) is not None:
            return
        self.add(path + "/")

    def decide(self, path: str, action: str, *, seen_size: int | None = None) -> Tree:
        is_dir = self.conn.execute("SELECT is_dir FROM nodes WHERE path = ?", (path,)).fetchone()[0]
        self.conn.execute(
            "INSERT OR REPLACE INTO decisions(path, is_dir, action, decided_at, seen_size,"
            " seen_mtime) VALUES (?, ?, ?, ?, ?, NULL)",
            (path, is_dir, action, time.time(), seen_size),
        )
        return self

    def categorise(self, path: str, names: Sequence[str]) -> Tree:
        cid, _ = self.store.get_or_create(list(names))
        self.conn.execute(
            "INSERT OR REPLACE INTO node_categories(path, category_id) VALUES (?, ?)", (path, cid)
        )
        return self

    def rule(self, kind: str, pattern: str) -> Tree:
        pos = self.conn.execute("SELECT count(*) FROM rules").fetchone()[0]
        self.conn.execute(
            "INSERT INTO rules(kind, pattern, position) VALUES (?, ?, ?)", (kind, pattern, pos)
        )
        return self

    def decision(self, path: str) -> str | None:
        row = self.conn.execute("SELECT action FROM decisions WHERE path = ?", (path,)).fetchone()
        return None if row is None else row[0]

    def flags(self, path: str) -> tuple[int, int]:
        row = self.conn.execute(
            "SELECT flag_new, flag_changed FROM nodes WHERE path = ?", (path,)
        ).fetchone()
        return (row[0], row[1])

    def files(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT path FROM nodes WHERE is_dir = 0")]
