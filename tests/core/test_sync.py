"""Sync between two simulated devices sharing one project folder (product-spec §11)."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from degreaser.core.categories import CategoryStore
from degreaser.core.db.connection import bump_revision
from degreaser.core.project import Project, ProjectSettings
from degreaser.core.rules.presets import RuleConfig
from degreaser.core.settings import Device
from degreaser.core.sources.local import LocalSource
from degreaser.core.sync import (
    LOCK_PATH,
    LockHeld,
    LockLost,
    SyncError,
    SyncService,
    SyncState,
    WrongProject,
    is_dirty,
    probe_existing,
)
from degreaser.core.sync.service import STALE_AFTER, STATE_HEADER

LAPTOP = Device("laptop-id", "Laptop")
DESKTOP = Device("desktop-id", "Desktop")


class Devices:
    """Each device has its own app data dir; switch() points Degreaser at one of them."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path, self.monkeypatch = tmp_path, monkeypatch

    def switch(self, device: Device) -> None:
        self.monkeypatch.setenv("DEGREASER_DATA_DIR", str(self.tmp_path / device.id))


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    root = tmp_path / "files"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.txt").write_text("a")
    return root


@pytest.fixture
def devices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Devices:
    return Devices(tmp_path, monkeypatch)


def service(project: Project, root: Path, device: Device, tmp_path: Path) -> SyncService:
    return SyncService(
        LocalSource(root),
        project.uuid,
        device,
        app_version="test",
        scratch_dir=tmp_path / f"scratch-{device.id}",
    )


@pytest.fixture
def laptop(folder: Path, devices: Devices, tmp_path: Path) -> Iterator[tuple[Project, SyncService]]:
    devices.switch(LAPTOP)
    project = Project.create("Backups", {"type": "local", "path": str(folder)})
    yield project, service(project, folder, LAPTOP, tmp_path)
    project.close()


def decide(project: Project, path: str, action: str) -> None:
    project.conn.execute(
        "INSERT OR REPLACE INTO decisions(path, is_dir, action, decided_at) VALUES (?, 0, ?, 1)",
        (path, action),
    )
    bump_revision(project.conn)


def add_desktop(laptop_project: Project, folder: Path, devices: Devices, tmp_path: Path):  # type: ignore[no-untyped-def]
    devices.switch(DESKTOP)
    project = Project.create(
        "?", {"type": "local", "path": str(folder)}, project_uuid=laptop_project.uuid
    )
    return project, service(project, folder, DESKTOP, tmp_path)


def test_push_then_pull_on_another_device(
    laptop: tuple[Project, SyncService], folder: Path, devices: Devices, tmp_path: Path
) -> None:
    project, sync = laptop
    project.save_rule_config(RuleConfig({"system-files", "photos"}, ["*.bak"]))
    project.save_settings(ProjectSettings(deletion_mode="permanent"))
    decide(project, "docs/a.txt", "keep")
    cid, _ = CategoryStore(project.conn).get_or_create(["Work", "Notes"])
    project.conn.execute(
        "INSERT INTO node_categories(path, category_id) VALUES ('docs', ?)", (cid,)
    )
    assert sync.compare(project.conn)[0] is SyncState.NO_REMOTE
    remote = sync.push(project.conn)
    assert remote.device_name == "Laptop" and remote.project_name == "Backups"
    assert sync.compare(project.conn)[0] is SyncState.IN_SYNC
    assert not is_dirty(project.conn)

    assert probe_existing(LocalSource(folder)).project_uuid == project.uuid
    desktop, desk_sync = add_desktop(project, folder, devices, tmp_path)
    assert desk_sync.compare(desktop.conn)[0] is SyncState.PULL
    desk_sync.pull(desktop.conn)
    assert desktop.name == "Backups"
    assert desktop.settings.deletion_mode == "permanent"
    assert desktop.rule_config() == RuleConfig({"system-files", "photos"}, ["*.bak"])
    assert desktop.conn.execute("SELECT action FROM decisions").fetchone()[0] == "keep"
    own = CategoryStore(desktop.conn).own_category("docs")
    assert own is not None and own[1] == ("Work", "Notes")
    assert desktop.source_config == {"type": "local", "path": str(folder)}  # stays local
    assert desk_sync.compare(desktop.conn)[0] is SyncState.IN_SYNC
    desktop.close()


def test_states_after_each_side_changes(
    laptop: tuple[Project, SyncService], folder: Path, devices: Devices, tmp_path: Path
) -> None:
    project, sync = laptop
    sync.push(project.conn)
    decide(project, "docs/a.txt", "delete")
    assert sync.compare(project.conn)[0] is SyncState.PUSH
    sync.push(project.conn)

    desktop, desk_sync = add_desktop(project, folder, devices, tmp_path)
    desk_sync.pull(desktop.conn)
    decide(desktop, "docs/a.txt", "keep")
    desk_sync.push(desktop.conn)
    desktop.close()

    devices.switch(LAPTOP)
    assert sync.compare(project.conn)[0] is SyncState.PULL
    decide(project, "docs/b.txt", "skip")
    state, remote = sync.compare(project.conn)
    assert state is SyncState.CONFLICT and remote is not None and remote.device_name == "Desktop"


def test_settings_and_rule_edits_make_the_state_dirty(laptop: tuple[Project, SyncService]) -> None:
    project, sync = laptop
    sync.push(project.conn)
    project.set_meta("name", "Renamed")
    assert is_dirty(project.conn)
    sync.push(project.conn)
    project.set_meta("last_opened", 123.0)  # device-local: not a change to sync
    assert not is_dirty(project.conn)


def test_lock_take_over_and_lost(
    laptop: tuple[Project, SyncService], folder: Path, devices: Devices, tmp_path: Path
) -> None:
    project, sync = laptop
    lock = sync.acquire()
    assert lock.device_name == "Laptop" and not lock.is_stale()
    sync.acquire()  # re-acquiring our own lock is fine

    desktop, desk_sync = add_desktop(project, folder, devices, tmp_path)
    with pytest.raises(LockHeld) as held:
        desk_sync.acquire()
    assert held.value.lock.device_name == "Laptop"
    assert held.value.lock.is_stale(now=lock.heartbeat_at + STALE_AFTER + 1)
    desk_sync.acquire(take_over=True)
    desk_sync.release()
    desk_sync.acquire()
    desktop.close()

    devices.switch(LAPTOP)
    with pytest.raises(LockLost):
        sync.heartbeat()
    sync.release()  # not ours any more: left alone
    assert (folder / LOCK_PATH).exists()


def test_release_removes_our_lock(laptop: tuple[Project, SyncService], folder: Path) -> None:
    _project, sync = laptop
    sync.acquire()
    sync.heartbeat()
    sync.release()
    assert not (folder / LOCK_PATH).exists()


def test_other_projects_state_is_refused(
    laptop: tuple[Project, SyncService], folder: Path, tmp_path: Path
) -> None:
    project, sync = laptop
    sync.push(project.conn)
    other = Project.create("Other", {"type": "local", "path": str(folder)})
    other_sync = service(other, folder, LAPTOP, tmp_path)
    with pytest.raises(WrongProject):
        other_sync.compare(other.conn)
    other.close()


def test_mismatched_header_and_newer_schema_are_refused(
    laptop: tuple[Project, SyncService], folder: Path
) -> None:
    project, sync = laptop
    sync.push(project.conn)
    header = json.loads((folder / STATE_HEADER).read_text())
    (folder / STATE_HEADER).write_text(json.dumps({**header, "state_id": "something-else"}))
    with pytest.raises(SyncError, match="kept changing"):
        sync.pull(project.conn)
    (folder / STATE_HEADER).write_text(json.dumps({**header, "schema_version": 99}))
    with pytest.raises(SyncError, match="newer version"):
        sync.pull(project.conn)


def test_probe_without_state(folder: Path) -> None:
    with pytest.raises(SyncError, match="no saved Degreaser project"):
        probe_existing(LocalSource(folder))


def test_sync_on_open_paths(
    laptop: tuple[Project, SyncService], folder: Path, devices: Devices, tmp_path: Path
) -> None:
    from degreaser.core.sync import Conflict, OpenOutcome, sync_on_open

    project, sync = laptop
    assert sync_on_open(sync, project.conn) is OpenOutcome.PUSHED  # nothing in the folder yet
    assert sync_on_open(sync, project.conn) is OpenOutcome.IN_SYNC
    sync.release()

    desktop, desk_sync = add_desktop(project, folder, devices, tmp_path)
    assert sync_on_open(desk_sync, desktop.conn) is OpenOutcome.PULLED
    decide(desktop, "docs/a.txt", "delete")
    desk_sync.push(desktop.conn)
    desk_sync.release()
    desktop.close()

    devices.switch(LAPTOP)
    decide(project, "docs/a.txt", "keep")
    with pytest.raises(Conflict) as conflict:
        sync_on_open(sync, project.conn)
    assert conflict.value.remote.device_name == "Desktop"
    # Read-only never writes to the folder, and keeps unsynced local changes on screen.
    assert sync_on_open(sync, project.conn, read_only=True) is OpenOutcome.KEPT_LOCAL
    outcome = sync_on_open(sync, project.conn, resolve="keep_remote")
    assert outcome is OpenOutcome.PULLED
    assert project.conn.execute("SELECT action FROM decisions").fetchone()[0] == "delete"
