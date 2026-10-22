"""Project database schema and migrations (docs/data-model.md §2).

The schema version lives in `PRAGMA user_version`. Migrations are append-only: never edit a
released step, add a new one.
"""

import sqlite3

V1 = """
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL                      -- JSON
) WITHOUT ROWID;

-- Tree cache: device-local, never synced.
CREATE TABLE nodes (
  id         INTEGER PRIMARY KEY,
  parent_id  INTEGER REFERENCES nodes(id) ON DELETE CASCADE,
  path       TEXT NOT NULL UNIQUE,
  name       TEXT NOT NULL,
  is_dir     INTEGER NOT NULL,
  kind       TEXT NOT NULL CHECK (kind IN ('file','dir','symlink','other')),
  size       INTEGER,
  mtime      REAL,
  ctime      REAL,
  sort_key   TEXT NOT NULL,
  listed     INTEGER NOT NULL DEFAULT 0,
  listed_at  REAL,
  list_error TEXT,                          -- dirs: why listing failed (permission denied, ...)
  first_seen_scan INTEGER,
  flag_new     INTEGER NOT NULL DEFAULT 0,
  flag_changed INTEGER NOT NULL DEFAULT 0,
  subtree_files INTEGER,
  subtree_bytes INTEGER
);
CREATE INDEX nodes_children ON nodes(parent_id, is_dir, sort_key, name);
CREATE INDEX nodes_unlisted ON nodes(listed) WHERE is_dir = 1 AND listed = 0;
CREATE INDEX nodes_new ON nodes(flag_new) WHERE flag_new = 1;
CREATE INDEX nodes_changed ON nodes(flag_changed) WHERE flag_changed = 1;

CREATE TABLE scans (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('index','rescan')),
  started_at REAL NOT NULL,
  finished_at REAL,
  status TEXT NOT NULL CHECK (status IN ('running','completed','cancelled','failed')),
  dirs_done INTEGER,
  dirs_total INTEGER
);

-- Decision state: keyed on path, synced between devices.
CREATE TABLE decisions (
  path        TEXT PRIMARY KEY,
  is_dir      INTEGER NOT NULL,
  action      TEXT NOT NULL CHECK (action IN ('keep','delete','skip','deferred')),
  decided_at  REAL NOT NULL,
  seen_size   INTEGER,
  seen_mtime  REAL
) WITHOUT ROWID;

CREATE TABLE tags (
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE categories (
  id      INTEGER PRIMARY KEY,
  tag_ids TEXT NOT NULL UNIQUE              -- ordered, '/'-joined tag ids
);

CREATE TABLE node_categories (
  path        TEXT PRIMARY KEY,
  category_id INTEGER NOT NULL REFERENCES categories(id)
) WITHOUT ROWID;

CREATE TABLE rules (
  id       INTEGER PRIMARY KEY,
  kind     TEXT NOT NULL CHECK (kind IN ('always_delete','always_keep')),
  pattern  TEXT NOT NULL,
  enabled  INTEGER NOT NULL DEFAULT 1,
  position INTEGER NOT NULL
);

CREATE TABLE history (
  id            INTEGER PRIMARY KEY,
  created_at    REAL NOT NULL,
  label         TEXT NOT NULL,
  cursor_before TEXT,
  state         TEXT NOT NULL CHECK (state IN ('done','undone')),
  change_count  INTEGER NOT NULL
);

CREATE TABLE history_changes (
  history_id INTEGER NOT NULL REFERENCES history(id) ON DELETE CASCADE,
  seq        INTEGER NOT NULL,
  tbl        TEXT NOT NULL CHECK (tbl IN ('decisions','node_categories','nodes_flags')),
  key        TEXT NOT NULL,
  before     TEXT,
  after      TEXT,
  PRIMARY KEY (history_id, seq)
) WITHOUT ROWID;

CREATE TABLE commits (
  id            INTEGER PRIMARY KEY,
  uuid          TEXT NOT NULL UNIQUE,
  kind          TEXT NOT NULL CHECK (kind IN ('triage','reorganize')),
  created_at    REAL NOT NULL,
  finished_at   REAL,
  status        TEXT NOT NULL CHECK (status IN ('planned','running','paused','completed','cancelled')),
  deletion_mode TEXT NOT NULL CHECK (deletion_mode IN ('trash','permanent')),
  summary       TEXT
);

CREATE TABLE commit_ops (
  id           INTEGER PRIMARY KEY,
  commit_id    INTEGER NOT NULL REFERENCES commits(id) ON DELETE CASCADE,
  seq          INTEGER NOT NULL,
  op           TEXT NOT NULL CHECK (op IN ('trash','delete_file','delete_tree','move','rmdir_if_empty','write_catalog')),
  path         TEXT,
  dest         TEXT,
  expect_size  INTEGER,
  expect_mtime REAL,
  status       TEXT NOT NULL CHECK (status IN ('pending','done','skipped','failed')),
  detail       TEXT,
  finished_at  REAL,
  UNIQUE (commit_id, seq)
);
CREATE INDEX commit_ops_pending ON commit_ops(commit_id, status, seq);
"""

MIGRATIONS: list[str] = [V1]

LATEST = len(MIGRATIONS)


def migrate(conn: sqlite3.Connection) -> None:
    """Bring the database up to LATEST. Each step runs in its own transaction."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > LATEST:
        raise RuntimeError(
            f"project database is version {current}, newer than this app supports ({LATEST})"
        )
    for version in range(current + 1, LATEST + 1):
        with conn:
            script = MIGRATIONS[version - 1]
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};")
