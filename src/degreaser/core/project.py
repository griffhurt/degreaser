"""Projects: creation, opening, metadata and settings (docs/product-spec.md §3)."""

import json
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import IO, Any, Self

from degreaser.core import credentials, paths
from degreaser.core.db.connection import bump_revision, connect, tx
from degreaser.core.rules.presets import RuleConfig
from degreaser.core.sources.base import FileSource
from degreaser.core.sources.errors import AuthenticationFailed, Unsupported
from degreaser.core.sources.local import LocalSource
from degreaser.core.sources.smb import SmbConfig, SmbSource

# Seeded always-delete rules: the default presets (core/rules/presets.py).
DEFAULT_DELETE_RULES = RuleConfig().patterns("always_delete")

# Project-wide meta that other devices must see: changing it makes the state need a push.
SYNCED_SETTINGS_KEYS = frozenset({"name", "settings", "rule_config"})

# Degreaser's own folder at the project root; never scanned (D8).
DEGREASER_DIR = ".degreaser"


@dataclass
class ProjectSettings:
    jump_back: bool = True
    retriage_changed: bool = True
    deletion_mode: str = "trash"  # "trash" | "permanent"
    remove_empty_dirs: bool = True
    sync_enabled: bool = True
    # Duplicates (docs/duplicates.md): files below this size are never hashed, and the
    # optional rule deletes exact copies of files the user kept.
    duplicate_min_bytes: int = 1024 * 1024
    delete_duplicates: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass(frozen=True)
class ProjectInfo:
    uuid: str
    name: str
    source: dict[str, Any]
    last_opened: float | None
    # Status figures cached when the project was last closed (files_known, files_triaged, ...).
    last_summary: dict[str, Any] | None = None

    @property
    def source_description(self) -> str:
        return describe_source(self.source)


def describe_source(source: dict[str, Any]) -> str:
    match source.get("type"):
        case "local":
            return str(source["path"])
        case "smb":
            return SmbConfig.from_source(source).describe()
        case other:
            return f"unknown source {other!r}"


def make_source(source: dict[str, Any], password: str | None = None) -> FileSource:
    """Build (but don't connect) the source a project's config describes."""
    match source.get("type"):
        case "local":
            return LocalSource(Path(source["path"]))
        case "smb":
            return SmbSource(SmbConfig.from_source(source), password or "")
        case other:
            raise Unsupported(f"source type {other!r} is not supported yet")


class ProjectLock:
    """Stops two app instances on this device from opening the same project."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: IO[str] | None = None

    def acquire(self) -> bool:
        f = open(self.path, "a+")  # noqa: SIM115 - held open for the lock's lifetime
        try:
            if sys.platform == "win32":
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._file = f
        return True

    def release(self) -> None:
        if self._file is None:
            return
        if sys.platform == "win32":
            import msvcrt

            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        self._file.close()
        self._file = None


class ProjectInUse(Exception):
    pass


class ProjectExists(Exception):
    pass


class WrongThread(RuntimeError):
    pass


class Project:
    """An open project. `conn` belongs to the thread that opened it (the UI thread)."""

    def __init__(self, project_uuid: str, conn: sqlite3.Connection, lock: ProjectLock) -> None:
        self.uuid = project_uuid
        self._conn = conn
        self._lock = lock
        self._thread = threading.get_ident()

    @property
    def conn(self) -> sqlite3.Connection:
        """The owning thread's connection. Workers must use new_connection() instead."""
        if threading.get_ident() != self._thread:
            raise WrongThread(
                "Project.conn (and meta/settings/name/rules) used off the thread that opened "
                "the project; read what the worker needs first, or use new_connection()"
            )
        return self._conn

    # Lifecycle ---------------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        name: str,
        source: dict[str, Any],
        settings: ProjectSettings | None = None,
        *,
        project_uuid: str | None = None,
    ) -> Self:
        """A new project. `project_uuid` is given when adding a project another device
        created (its state is then pulled from the project folder)."""
        project_uuid = project_uuid or uuid.uuid4().hex
        directory = paths.project_dir(project_uuid)
        if directory.exists():
            raise ProjectExists(f"this project is already in Degreaser ({project_uuid})")
        directory.mkdir(parents=True)
        conn = connect(paths.project_db_path(project_uuid))
        with tx(conn):
            for key, value in {
                "project_uuid": project_uuid,
                "name": name,
                "source": source,
                "settings": asdict(settings or ProjectSettings()),
                "cursor": None,
                "created_at": time.time(),
                "last_opened": None,
                "state_revision": 0,
                "rule_config": RuleConfig().to_dict(),
            }.items():
                conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (key, json.dumps(value)))
            conn.executemany(
                "INSERT INTO rules(kind, pattern, position) VALUES ('always_delete', ?, ?)",
                [(p, i) for i, p in enumerate(DEFAULT_DELETE_RULES)],
            )
            conn.execute(
                "INSERT INTO nodes(parent_id, path, name, is_dir, kind, sort_key)"
                " VALUES (NULL, '', '', 1, 'dir', '')"
            )
        conn.close()
        return cls.open(project_uuid)

    @classmethod
    def open(cls, project_uuid: str) -> Self:
        directory = paths.project_dir(project_uuid)
        if not paths.project_db_path(project_uuid).exists():
            raise FileNotFoundError(f"no project {project_uuid}")
        lock = ProjectLock(directory / "project.lock")
        if not lock.acquire():
            raise ProjectInUse("this project is already open in another Degreaser window")
        conn = connect(paths.project_db_path(project_uuid))
        project = cls(project_uuid, conn, lock)
        project.set_meta("last_opened", time.time())
        return project

    def close(self) -> None:
        self.conn.close()
        self._lock.release()

    def new_connection(self) -> sqlite3.Connection:
        """A separate connection for a worker thread."""
        return connect(paths.project_db_path(self.uuid), migrate_schema=False)

    # Metadata ------------------------------------------------------------------------------

    def meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        if key in SYNCED_SETTINGS_KEYS:
            bump_revision(self.conn)

    @property
    def name(self) -> str:
        return self.meta("name")

    @property
    def source_config(self) -> dict[str, Any]:
        return self.meta("source")

    @property
    def settings(self) -> ProjectSettings:
        return ProjectSettings.from_dict(self.meta("settings", {}))

    def save_settings(self, settings: ProjectSettings) -> None:
        self.set_meta("settings", asdict(settings))

    def rules(self) -> tuple[list[str], list[str]]:
        """(always_delete patterns, always_keep patterns) in order, including disabled ones."""
        rows = self.conn.execute("SELECT kind, pattern FROM rules ORDER BY position, id").fetchall()
        return (
            [r["pattern"] for r in rows if r["kind"] == "always_delete"],
            [r["pattern"] for r in rows if r["kind"] == "always_keep"],
        )

    def rule_config(self) -> RuleConfig:
        """Presets + custom patterns. Older projects have none stored: infer from the rules."""
        stored = self.meta("rule_config")
        if stored is not None:
            return RuleConfig.from_dict(stored)
        return RuleConfig.infer(*self.rules())

    def save_rule_config(self, config: RuleConfig) -> None:
        self.replace_rules(
            config.patterns("always_delete"), config.patterns("always_keep"), config=config
        )

    def replace_rules(
        self,
        always_delete: list[str],
        always_keep: list[str],
        config: RuleConfig | None = None,
    ) -> None:
        """Write the flattened rules (and the preset view of them, inferred if not given)."""
        config = config or RuleConfig.infer(always_delete, always_keep)
        self.set_meta("rule_config", config.to_dict())
        with tx(self.conn):
            bump_revision(self.conn)
            self.conn.execute("DELETE FROM rules")
            self.conn.executemany(
                "INSERT INTO rules(kind, pattern, position) VALUES (?, ?, ?)",
                [("always_delete", p, i) for i, p in enumerate(always_delete)]
                + [("always_keep", p, i) for i, p in enumerate(always_keep)],
            )

    @property
    def cache_dir(self) -> Path:
        return paths.project_cache_dir(self.uuid)

    def make_source(self) -> FileSource:
        """Callable on the UI thread (it reads meta); the source itself is used on workers."""
        config = self.source_config
        password = None
        if config.get("type") == "smb":
            password = credentials.get_password(self.uuid)
            if password is None:
                raise AuthenticationFailed("no password saved for this project")
        return make_source(config, password)


def list_projects() -> list[ProjectInfo]:
    """Every project on this device, most recently opened first."""
    infos = []
    root = paths.projects_dir()
    for db_path in root.glob("*/project.sqlite") if root.exists() else []:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            meta = {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM meta")}
            conn.close()
        except sqlite3.Error:
            continue
        infos.append(
            ProjectInfo(
                uuid=db_path.parent.name,
                name=meta.get("name", "?"),
                source=meta.get("source", {}),
                last_opened=meta.get("last_opened"),
                last_summary=meta.get("last_summary"),
            )
        )
    return sorted(infos, key=lambda p: p.last_opened or 0, reverse=True)


def remove_project(project_uuid: str) -> None:
    """Delete Degreaser's local data for a project. Never touches the project's files."""
    shutil.rmtree(paths.project_dir(project_uuid))
    credentials.delete_password(project_uuid)
