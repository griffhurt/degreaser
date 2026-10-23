"""What happens to the decision state when a project opens (product-spec §3.3 step 1, §11).

`sync_on_open` runs on a worker and either finishes or raises something the UI has to ask
the user about (LockHeld, Conflict); the UI then calls it again with their answer.
"""

import sqlite3
from enum import StrEnum
from typing import Literal

from degreaser.core.sync.service import RemoteState, SyncError, SyncService, SyncState


class OpenOutcome(StrEnum):
    IN_SYNC = "in_sync"
    PULLED = "pulled"  # the folder's copy was newer (or the user chose it)
    PUSHED = "pushed"  # this device's copy was newer, or the folder had none yet
    KEPT_LOCAL = "kept_local"  # read-only, with local changes the folder doesn't have


class Conflict(SyncError):
    """Both this device and the folder changed since they last synced."""

    def __init__(self, remote: RemoteState) -> None:
        super().__init__(f"both this device and {remote.device_name} changed the project")
        self.remote = remote


Resolution = Literal["keep_local", "keep_remote"]


def sync_on_open(
    service: SyncService,
    conn: sqlite3.Connection,
    *,
    read_only: bool = False,
    take_over: bool = False,
    resolve: Resolution | None = None,
) -> OpenOutcome:
    """Check the project out (unless read-only), then bring local and folder state together.

    Read-only opens never write to the folder: they pull a newer folder copy only when this
    device has nothing unsynced, and otherwise show the local copy.
    """
    if not read_only:
        service.acquire(take_over=take_over)
    state, remote = service.compare(conn)
    if read_only:
        if state is SyncState.PULL:
            service.pull(conn)
            return OpenOutcome.PULLED
        if state in (SyncState.PUSH, SyncState.CONFLICT):
            return OpenOutcome.KEPT_LOCAL
        return OpenOutcome.IN_SYNC
    match state:
        case SyncState.IN_SYNC:
            return OpenOutcome.IN_SYNC
        case SyncState.NO_REMOTE | SyncState.PUSH:
            service.push(conn)
            return OpenOutcome.PUSHED
        case SyncState.PULL:
            service.pull(conn)
            return OpenOutcome.PULLED
        case SyncState.CONFLICT:
            assert remote is not None
            if resolve is None:
                raise Conflict(remote)
            if resolve == "keep_local":
                service.push(conn)
                return OpenOutcome.PUSHED
            service.pull(conn)
            return OpenOutcome.PULLED
