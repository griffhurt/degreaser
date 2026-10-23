"""SQLite connections with the pragmas every project database needs."""

import sqlite3
from pathlib import Path

from degreaser.core.db.schema import migrate


def connect(path: Path | str, *, migrate_schema: bool = True) -> sqlite3.Connection:
    """Open a project DB. Each thread uses its own connection (see docs/architecture.md §5)."""
    # isolation_level=None: autocommit; transactions are explicit via `with tx(conn)`.
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if migrate_schema:
        migrate(conn)
    return conn


def bump_revision(conn: sqlite3.Connection) -> None:
    """Every write to synced decision state bumps meta.state_revision, which is how sync
    knows this device has changes to upload (docs/data-model.md §2.4)."""
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('state_revision', '1')"
        " ON CONFLICT(key) DO UPDATE SET value = CAST(meta.value AS INTEGER) + 1"
    )


class tx:
    """Explicit transaction (BEGIN IMMEDIATE) on an autocommit connection: `with tx(conn):`."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
