"""Undo/redo (docs/triage-engine.md §6, docs/data-model.md §2.3).

Every user action writes through a Recorder, which applies each row change and remembers
its before/after values. finish() stores them as one `history` entry plus one
`history_changes` row per changed row. Undo applies the `before` values in reverse; redo
applies the `after` values in order.
"""

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Literal

Table = Literal["decisions", "node_categories", "nodes_flags"]

MAX_ENTRIES = 2_000
MAX_CHANGE_ROWS = 500_000


@dataclass(frozen=True, slots=True)
class Change:
    tbl: Table
    key: str  # path
    before: dict[str, Any] | None  # None = row absent
    after: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class Entry:
    id: int
    label: str
    cursor_before: str | None
    changes: list[Change]


# Row access --------------------------------------------------------------------------------


def read_row(conn: sqlite3.Connection, tbl: Table, key: str) -> dict[str, Any] | None:
    if tbl == "decisions":
        row = conn.execute(
            "SELECT is_dir, action, decided_at, seen_size, seen_mtime FROM decisions"
            " WHERE path = ?",
            (key,),
        ).fetchone()
        return None if row is None else dict(zip(_DECISION_COLS, row, strict=True))
    if tbl == "node_categories":
        row = conn.execute(
            "SELECT category_id FROM node_categories WHERE path = ?", (key,)
        ).fetchone()
        return None if row is None else {"category_id": row[0]}
    row = conn.execute("SELECT flag_new, flag_changed FROM nodes WHERE path = ?", (key,)).fetchone()
    return None if row is None else {"flag_new": row[0], "flag_changed": row[1]}


_DECISION_COLS = ("is_dir", "action", "decided_at", "seen_size", "seen_mtime")


def write_row(conn: sqlite3.Connection, tbl: Table, key: str, value: dict[str, Any] | None) -> None:
    if tbl == "decisions":
        if value is None:
            conn.execute("DELETE FROM decisions WHERE path = ?", (key,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO decisions(path, is_dir, action, decided_at, seen_size,"
                " seen_mtime) VALUES (?, ?, ?, ?, ?, ?)",
                (key, *(value[c] for c in _DECISION_COLS)),
            )
    elif tbl == "node_categories":
        if value is None:
            conn.execute("DELETE FROM node_categories WHERE path = ?", (key,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO node_categories(path, category_id) VALUES (?, ?)",
                (key, value["category_id"]),
            )
    elif value is not None:
        # Flags live on the device-local tree; a node missing here (e.g. history synced from
        # another device) is simply skipped.
        conn.execute(
            "UPDATE nodes SET flag_new = ?, flag_changed = ? WHERE path = ?",
            (value["flag_new"], value["flag_changed"], key),
        )


# Recording ---------------------------------------------------------------------------------


class Recorder:
    """Collects the changes of one user action. The caller owns the transaction."""

    def __init__(self, conn: sqlite3.Connection, label: str, cursor_before: str | None) -> None:
        self.conn = conn
        self.label = label
        self.cursor_before = cursor_before
        self.changes: list[Change] = []

    def set(self, tbl: Table, key: str, value: dict[str, Any] | None) -> Change | None:
        before = read_row(self.conn, tbl, key)
        if before == value or (tbl == "nodes_flags" and before is None):
            return None
        write_row(self.conn, tbl, key, value)
        change = Change(tbl, key, before, value)
        self.changes.append(change)
        return change

    def finish(self) -> int | None:
        """Store the entry (if anything changed), discard redo, apply caps. -> history id."""
        if not self.changes:
            return None
        conn = self.conn
        conn.execute("DELETE FROM history WHERE state = 'undone'")
        hid = conn.execute(
            "INSERT INTO history(created_at, label, cursor_before, state, change_count)"
            " VALUES (?, ?, ?, 'done', ?)",
            (time.time(), self.label, self.cursor_before, len(self.changes)),
        ).lastrowid
        conn.executemany(
            "INSERT INTO history_changes(history_id, seq, tbl, key, before, after)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (hid, seq, c.tbl, c.key, _dump(c.before), _dump(c.after))
                for seq, c in enumerate(self.changes)
            ],
        )
        _apply_caps(conn)
        return hid


def _dump(value: dict[str, Any] | None) -> str | None:
    return None if value is None else json.dumps(value)


def _load(value: str | None) -> dict[str, Any] | None:
    return None if value is None else json.loads(value)


def _apply_caps(conn: sqlite3.Connection) -> None:
    entries, rows = conn.execute(
        "SELECT count(*), coalesce(sum(change_count), 0) FROM history"
    ).fetchone()
    if entries <= MAX_ENTRIES and rows <= MAX_CHANGE_ROWS:
        return
    # Drop oldest entries until both caps hold (the newest entry is always kept).
    cut = None
    for hid, count in conn.execute("SELECT id, change_count FROM history ORDER BY id"):
        if entries <= MAX_ENTRIES and rows <= MAX_CHANGE_ROWS:
            break
        cut, entries, rows = hid, entries - 1, rows - count
    newest = conn.execute("SELECT max(id) FROM history").fetchone()[0]
    if cut is not None:
        conn.execute("DELETE FROM history WHERE id <= ? AND id < ?", (cut, newest))


# Undo / redo -------------------------------------------------------------------------------


def _entry(conn: sqlite3.Connection, hid: int) -> Entry:
    label, cursor = conn.execute(
        "SELECT label, cursor_before FROM history WHERE id = ?", (hid,)
    ).fetchone()
    changes = [
        Change(tbl, key, _load(before), _load(after))
        for tbl, key, before, after in conn.execute(
            "SELECT tbl, key, before, after FROM history_changes WHERE history_id = ? ORDER BY seq",
            (hid,),
        )
    ]
    return Entry(hid, label, cursor, changes)


def undo_label(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT label FROM history WHERE state = 'done' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return None if row is None else row[0]


def redo_label(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT label FROM history WHERE state = 'undone' ORDER BY id LIMIT 1"
    ).fetchone()
    return None if row is None else row[0]


def undo(conn: sqlite3.Connection) -> Entry | None:
    """Revert the newest done entry. The caller owns the transaction."""
    row = conn.execute(
        "SELECT id FROM history WHERE state = 'done' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    entry = _entry(conn, row[0])
    for change in reversed(entry.changes):
        write_row(conn, change.tbl, change.key, change.before)
    conn.execute("UPDATE history SET state = 'undone' WHERE id = ?", (entry.id,))
    return entry


def redo(conn: sqlite3.Connection) -> Entry | None:
    """Re-apply the oldest undone entry. The caller owns the transaction."""
    row = conn.execute(
        "SELECT id FROM history WHERE state = 'undone' ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    entry = _entry(conn, row[0])
    for change in entry.changes:
        write_row(conn, change.tbl, change.key, change.after)
    conn.execute("UPDATE history SET state = 'done' WHERE id = ?", (entry.id,))
    return entry


def clear(conn: sqlite3.Connection) -> None:
    """Forget all history (at commit: undoing past a commit is meaningless)."""
    conn.execute("DELETE FROM history")
