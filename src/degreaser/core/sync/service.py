"""Checkout lock and decision-state snapshots in `<project root>/.degreaser/state/`
(product-spec §11, data-model §3, D17).

The live database never lives on the share (SQLite locking is unreliable over SMB). Each
device works on its local database and uploads a snapshot of the decision-state tables; the
tree cache is never synced. A lock file with a heartbeat makes sure only one device has the
project open for writing.

Whether to push or pull is decided from two device-local values recorded at every sync:
`sync_base_id` (the id of the snapshot both sides last agreed on) and `sync_local_revision`
(this device's state_revision at that moment). Local changes since then make the local copy
dirty; a different snapshot id on the share means another device synced since.

Everything here does source I/O: call it from worker threads only.
"""

import contextlib
import json
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from degreaser.core.db.connection import tx
from degreaser.core.settings import Device
from degreaser.core.sources.base import FileSource
from degreaser.core.sources.errors import NotFound

STATE_DIR = ".degreaser/state"
LOCK_PATH = f"{STATE_DIR}/lock.json"
STATE_DB = f"{STATE_DIR}/state.sqlite"
STATE_HEADER = f"{STATE_DIR}/state.json"

HEARTBEAT_SECONDS = 60
STALE_AFTER = 5 * 60  # a lock not refreshed for this long probably belongs to a crashed app
PUSH_INTERVAL = 5 * 60

# Decision-state tables, parents before children (foreign keys).
SYNCED_TABLES = (
    "rules",
    "tags",
    "categories",
    "node_categories",
    "decisions",
    "history",
    "history_changes",
    "commits",
    "commit_ops",
)
# meta keys that describe this device's view, not the project's decisions.
LOCAL_META_KEYS = frozenset(
    {
        "source",  # another device may reach the same folder differently (mount vs SMB)
        "last_opened",
        "last_summary",
        "sync_base_id",
        "sync_local_revision",
        "sync_unavailable",
    }
)


class SyncState(StrEnum):
    NO_REMOTE = "no_remote"  # nothing on the share yet
    IN_SYNC = "in_sync"
    PUSH = "push"  # only this device changed
    PULL = "pull"  # only the share changed
    CONFLICT = "conflict"  # both changed since they last agreed


@dataclass(frozen=True)
class Lock:
    device_id: str
    device_name: str
    acquired_at: float
    heartbeat_at: float
    app_version: str

    def is_stale(self, now: float | None = None) -> bool:
        return (now or time.time()) - self.heartbeat_at > STALE_AFTER

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Lock:
        return cls(
            device_id=str(data["device_id"]),
            device_name=str(data.get("device_name", "another device")),
            acquired_at=float(data.get("acquired_at", 0)),
            heartbeat_at=float(data.get("heartbeat_at", 0)),
            app_version=str(data.get("app_version", "")),
        )


@dataclass(frozen=True)
class RemoteState:
    project_uuid: str
    project_name: str
    revision: int
    state_id: str
    device_id: str
    device_name: str
    updated_at: float
    schema_version: int

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RemoteState:
        if data.get("format") != "degreaser-state":
            raise SyncError("the state header isn't a Degreaser state file")
        return cls(
            project_uuid=str(data["project_uuid"]),
            project_name=str(data.get("project_name", "")),
            revision=int(data["revision"]),
            state_id=str(data["state_id"]),
            device_id=str(data.get("device_id", "")),
            device_name=str(data.get("device_name", "another device")),
            updated_at=float(data.get("updated_at", 0)),
            schema_version=int(data.get("schema_version", 1)),
        )

    def to_json(self) -> dict[str, Any]:
        return {"format": "degreaser-state", "version": 1, **asdict(self)}


class SyncError(Exception):
    """Sync can't proceed (bad or incompatible state on the share)."""


class LockHeld(SyncError):
    def __init__(self, lock: Lock) -> None:
        super().__init__(f"the project is open on {lock.device_name}")
        self.lock = lock


class LockLost(SyncError):
    """Another device took the lock while this one had the project open."""

    def __init__(self, lock: Lock | None) -> None:
        holder = lock.device_name if lock else "another device"
        super().__init__(f"{holder} took over this project")
        self.lock = lock


class WrongProject(SyncError):
    def __init__(self, remote: RemoteState) -> None:
        super().__init__(
            f"the folder holds the state of a different Degreaser project "
            f"(“{remote.project_name or remote.project_uuid}”)"
        )
        self.remote = remote


def _meta(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return default if row is None else json.loads(row[0])


def _set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )


def is_dirty(conn: sqlite3.Connection) -> bool:
    """True if decisions changed locally since the last push or pull."""
    revision = _meta(conn, "state_revision", 0)
    synced = _meta(conn, "sync_local_revision")
    return revision != synced if synced is not None else revision > 0


def mark_clean(conn: sqlite3.Connection) -> None:
    """Record the current revision as synced (after a read-only session made no real
    changes, only bookkeeping like deferred → skip)."""
    _set_meta(conn, "sync_local_revision", _meta(conn, "state_revision", 0))


class SyncService:
    def __init__(
        self,
        source: FileSource,
        project_uuid: str,
        device: Device,
        *,
        app_version: str,
        scratch_dir: Path,
    ) -> None:
        self.source = source
        self.project_uuid = project_uuid
        self.device = device
        self.app_version = app_version
        self.scratch_dir = scratch_dir

    # Lock --------------------------------------------------------------------------------------

    def read_lock(self) -> Lock | None:
        data = self._read_json(LOCK_PATH)
        return Lock.from_json(data) if data is not None else None

    def acquire(self, *, take_over: bool = False) -> Lock:
        """Take the checkout lock. Raises LockHeld if another device has it (unless taking
        over), or a SourceError if the folder can't be written."""
        current = self.read_lock()
        if current is not None and current.device_id != self.device.id and not take_over:
            raise LockHeld(current)
        now = time.time()
        acquired = current.acquired_at if current and current.device_id == self.device.id else now
        lock = Lock(self.device.id, self.device.name, acquired, now, self.app_version)
        self.source.makedirs(STATE_DIR)
        self._write_json(LOCK_PATH, asdict(lock))
        confirmed = self.read_lock()
        if confirmed is None or confirmed.device_id != self.device.id:
            raise LockHeld(confirmed or lock)  # lost a race with another device
        return confirmed

    def heartbeat(self) -> None:
        current = self.read_lock()
        if current is None or current.device_id != self.device.id:
            raise LockLost(current)
        lock = Lock(
            self.device.id, self.device.name, current.acquired_at, time.time(), self.app_version
        )
        self._write_json(LOCK_PATH, asdict(lock))

    def release(self) -> None:
        current = self.read_lock()
        if current is not None and current.device_id == self.device.id:
            with contextlib.suppress(NotFound):
                self.source.delete_file(LOCK_PATH)

    # State -------------------------------------------------------------------------------------

    def remote(self) -> RemoteState | None:
        data = self._read_json(STATE_HEADER)
        return RemoteState.from_json(data) if data is not None else None

    def compare(self, conn: sqlite3.Connection) -> tuple[SyncState, RemoteState | None]:
        remote = self.remote()
        if remote is None:
            return SyncState.NO_REMOTE, None
        if remote.project_uuid != self.project_uuid:
            raise WrongProject(remote)
        changed_remotely = remote.state_id != _meta(conn, "sync_base_id")
        dirty = is_dirty(conn)
        match (changed_remotely, dirty):
            case (False, False):
                return SyncState.IN_SYNC, remote
            case (False, True):
                return SyncState.PUSH, remote
            case (True, False):
                return SyncState.PULL, remote
            case _:
                return SyncState.CONFLICT, remote

    def push(self, conn: sqlite3.Connection) -> RemoteState:
        """Upload a snapshot of the decision state. The database goes up first and the header
        last, so a reader that sees the new header always finds the matching database."""
        self.scratch_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.scratch_dir / f"push-{uuid.uuid4().hex}.sqlite"
        state_id = uuid.uuid4().hex
        try:
            revision = _snapshot(conn, tmp, state_id)
            data = tmp.read_bytes()
        finally:
            tmp.unlink(missing_ok=True)
        remote = RemoteState(
            project_uuid=self.project_uuid,
            project_name=_meta(conn, "name", ""),
            revision=revision,
            state_id=state_id,
            device_id=self.device.id,
            device_name=self.device.name,
            updated_at=time.time(),
            schema_version=conn.execute("PRAGMA user_version").fetchone()[0],
        )
        self.source.makedirs(STATE_DIR)
        self.source.write_file_atomic(STATE_DB, data)
        self._write_json(STATE_HEADER, remote.to_json())
        _set_meta(conn, "sync_base_id", state_id)
        _set_meta(conn, "sync_local_revision", revision)
        return remote

    def pull(self, conn: sqlite3.Connection) -> RemoteState:
        """Replace the local decision state with the share's snapshot. The tree cache is left
        alone; the rescan that follows reconciles decisions with what's on disk."""
        for _attempt in range(3):
            remote = self.remote()
            if remote is None:
                raise SyncError("there's no saved state in the project folder")
            if remote.project_uuid != self.project_uuid:
                raise WrongProject(remote)
            local_version = conn.execute("PRAGMA user_version").fetchone()[0]
            if remote.schema_version > local_version:
                raise SyncError(
                    f"{remote.device_name} saved this project with a newer version of "
                    "Degreaser; update Degreaser on this device to open it"
                )
            tmp = self._download(STATE_DB)
            try:
                if _snapshot_id(tmp) != remote.state_id:
                    continue  # caught between a push's database and its header: read again
                _restore(conn, tmp, remote.state_id)
                return remote
            finally:
                tmp.unlink(missing_ok=True)
        raise SyncError("the saved state kept changing while it was being read")

    # Helpers -----------------------------------------------------------------------------------

    def _read_json(self, rel: str) -> dict[str, Any] | None:
        try:
            raw = self.source.read_range(rel, 0, 1 << 20)
        except NotFound:
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SyncError(f"{rel} is damaged ({exc})") from exc
        if not isinstance(data, dict):
            raise SyncError(f"{rel} is damaged")
        return data

    def _write_json(self, rel: str, data: dict[str, Any]) -> None:
        self.source.write_file_atomic(rel, json.dumps(data, indent=2).encode("utf-8"))

    def _download(self, rel: str) -> Path:
        self.scratch_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.scratch_dir / f"pull-{uuid.uuid4().hex}.sqlite"
        size = self.source.stat(rel).size or 0
        with open(tmp, "wb") as out:
            done = 0
            while done < size:
                data = self.source.read_range(rel, done, min(8 << 20, size - done))
                if not data:
                    break
                out.write(data)
                done += len(data)
        return tmp


def _columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def _snapshot(conn: sqlite3.Connection, path: Path, state_id: str) -> int:
    """Copy the synced tables (and project-wide meta) into a new database at `path`.
    Returns the state_revision the copy reflects."""
    conn.execute("ATTACH DATABASE ? AS snap", (str(path),))
    try:
        conn.execute("BEGIN")  # one read snapshot of main for every table
        try:
            for table in ("meta", *SYNCED_TABLES):
                ddl = conn.execute(
                    "SELECT sql FROM main.sqlite_master WHERE type = 'table' AND name = ?",
                    (table,),
                ).fetchone()[0]
                prefix = f"CREATE TABLE {table}"
                assert ddl.startswith(prefix), ddl
                conn.execute(f"CREATE TABLE snap.{table}" + ddl[len(prefix) :])
                where = ""
                if table == "meta":
                    keys = ",".join("?" * len(LOCAL_META_KEYS))
                    where = f" WHERE key NOT IN ({keys})"
                conn.execute(
                    f"INSERT INTO snap.{table} SELECT * FROM main.{table}{where}",
                    sorted(LOCAL_META_KEYS) if where else (),
                )
            conn.execute(
                "INSERT INTO snap.meta(key, value) VALUES ('sync_state_id', ?)",
                (json.dumps(state_id),),
            )
            revision = _meta(conn, "state_revision", 0)
            version = conn.execute("PRAGMA main.user_version").fetchone()[0]
            conn.execute(f"PRAGMA snap.user_version = {int(version)}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("DETACH DATABASE snap")
    return revision


def _snapshot_id(path: Path) -> str | None:
    try:
        snap = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = snap.execute("SELECT value FROM meta WHERE key = 'sync_state_id'").fetchone()
        finally:
            snap.close()
    except sqlite3.DatabaseError as exc:
        raise SyncError(f"the saved state is damaged ({exc})") from exc
    return json.loads(row[0]) if row else None


def _restore(conn: sqlite3.Connection, path: Path, state_id: str) -> None:
    conn.execute("ATTACH DATABASE ? AS snap", (str(path),))
    try:
        with tx(conn):
            for table in reversed(SYNCED_TABLES):
                conn.execute(f"DELETE FROM main.{table}")
            for table in SYNCED_TABLES:
                # Columns both sides have: a snapshot from an older schema still restores.
                ours = _columns(conn, "main", table)
                theirs = set(_columns(conn, "snap", table))
                cols = ",".join(c for c in ours if c in theirs)
                if cols:
                    conn.execute(
                        f"INSERT INTO main.{table} ({cols}) SELECT {cols} FROM snap.{table}"
                    )
            keys = ",".join("?" * len(LOCAL_META_KEYS))
            for key, value in conn.execute(
                f"SELECT key, value FROM snap.meta WHERE key NOT IN ({keys})",
                sorted(LOCAL_META_KEYS),
            ).fetchall():
                if key != "sync_state_id":
                    conn.execute(
                        "INSERT INTO main.meta(key, value) VALUES (?, ?)"
                        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, value),
                    )
            _set_meta(conn, "sync_base_id", state_id)
            _set_meta(conn, "sync_local_revision", _meta(conn, "state_revision", 0))
    finally:
        conn.execute("DETACH DATABASE snap")


def probe_existing(source: FileSource) -> RemoteState:
    """For "Add existing project": the state header in a folder, or SyncError if none.
    Source errors (can't connect, permission denied) propagate as they are."""
    remote = SyncService(source, "", Device(), app_version="", scratch_dir=Path(".")).remote()
    if remote is None:
        raise SyncError(
            "this folder has no saved Degreaser project (.degreaser/state/state.json is missing)"
        )
    return remote
