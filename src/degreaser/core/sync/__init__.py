"""Multi-device sync: checkout lock and decision-state snapshots (product-spec §11)."""

from degreaser.core.sync.opening import Conflict, OpenOutcome, Resolution, sync_on_open
from degreaser.core.sync.service import (
    HEARTBEAT_SECONDS,
    LOCK_PATH,
    PUSH_INTERVAL,
    STATE_DIR,
    Lock,
    LockHeld,
    LockLost,
    RemoteState,
    SyncError,
    SyncService,
    SyncState,
    WrongProject,
    is_dirty,
    mark_clean,
    probe_existing,
)

__all__ = [
    "HEARTBEAT_SECONDS",
    "LOCK_PATH",
    "PUSH_INTERVAL",
    "STATE_DIR",
    "Conflict",
    "Lock",
    "LockHeld",
    "LockLost",
    "OpenOutcome",
    "RemoteState",
    "Resolution",
    "SyncError",
    "SyncService",
    "SyncState",
    "WrongProject",
    "is_dirty",
    "mark_clean",
    "probe_existing",
    "sync_on_open",
]
