"""Projects: creation, opening, metadata and settings (docs/product-spec.md §3)."""

import json
import shutil
import sqlite3
import sys
import time
import uuid
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import IO, Any, Self

from degreaser.core import paths
from degreaser.core.db.connection import connect, tx
from degreaser.core.sources.base import FileSource
from degreaser.core.sources.errors import Unsupported
from degreaser.core.sources.local import LocalSource

# Seeded always-delete rules (docs/triage-engine.md §4.2).
DEFAULT_DELETE_RULES = [
    ".DS_Store",
    "Thumbs.db",
    "ehthumbs.db",
    "desktop.ini",
    "Icon\r",
    ".Spotlight-V100/",
    ".Trashes/",
    ".fseventsd/",
    ".TemporaryItems/",
    "$RECYCLE.BIN/",
]

# Degreaser's own folder at the project root; never scanned (D8).
DEGREASER_DIR = ".degreaser"


@dataclass
class ProjectSettings:
    jump_back: bool = True
    retriage_changed: bool = True
    deletion_mode: str = "trash"  # "trash" | "permanent"
    remove_empty_dirs: bool = True
    sync_enabled: bool = True

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

    @property
    def source_description(self) -> str:
        return describe_source(self.source)


def describe_source(source: dict[str, Any]) -> str:
    match source.get("type"):
        case "local":
            return str(source["path"])
        case "smb":
            path = source.get("path", "").strip("/")
            return f"smb://{source['host']}/{source['share']}" + (f"/{path}" if path else "")
        case other:
            return f"unknown source {other!r}"


def make_source(source: dict[str, Any]) -> FileSource:
    match source.get("type"):
        case "local":
            return LocalSource(Path(source["path"]))
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


class Project:
    """An open project. `conn` belongs to the thread that opened it (the UI thread)."""

    def __init__(self, project_uuid: str, conn: sqlite3.Connection, lock: ProjectLock) -> None:
        self.uuid = project_uuid
        self.conn = conn
        self._lock = lock

    # Lifecycle ---------------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        name: str,
        source: dict[str, Any],
        settings: ProjectSettings | None = None,
    ) -> Self:
        project_uuid = uuid.uuid4().hex
        directory = paths.project_dir(project_uuid)
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

    @property
    def cache_dir(self) -> Path:
        return paths.project_cache_dir(self.uuid)

    def make_source(self) -> FileSource:
        return make_source(self.source_config)


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
            )
        )
    return sorted(infos, key=lambda p: p.last_opened or 0, reverse=True)


def remove_project(project_uuid: str) -> None:
    """Delete Degreaser's local data for a project. Never touches the project's files."""
    shutil.rmtree(paths.project_dir(project_uuid))
