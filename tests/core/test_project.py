from pathlib import Path

import pytest

from degreaser.core.project import (
    DEFAULT_DELETE_RULES,
    Project,
    ProjectInUse,
    ProjectSettings,
    list_projects,
    remove_project,
)


def test_create_open_list_remove(tmp_path: Path) -> None:
    files = tmp_path / "files"
    files.mkdir()
    p = Project.create("Backups", {"type": "local", "path": str(files)})
    assert p.name == "Backups"
    assert p.settings == ProjectSettings()
    rules = [r[0] for r in p.conn.execute("SELECT pattern FROM rules ORDER BY position")]
    assert rules == DEFAULT_DELETE_RULES
    root = p.conn.execute("SELECT path, is_dir, listed FROM nodes").fetchall()
    assert [tuple(r) for r in root] == [("", 1, 0)]
    assert p.make_source().list_dir("") == []

    with pytest.raises(ProjectInUse):
        Project.open(p.uuid)

    p.save_settings(ProjectSettings(deletion_mode="permanent"))
    p.close()

    [info] = list_projects()
    assert (info.uuid, info.name, info.source_description) == (p.uuid, "Backups", str(files))

    reopened = Project.open(p.uuid)
    assert reopened.settings.deletion_mode == "permanent"
    reopened.close()

    remove_project(p.uuid)
    assert list_projects() == []
    assert files.exists()


def test_main_connection_is_guarded_against_other_threads(tmp_path: Path) -> None:
    import threading

    from degreaser.core.project import WrongThread

    p = Project.create("P", {"type": "local", "path": str(tmp_path)})
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            _ = p.settings
        except BaseException as exc:
            errors.append(exc)
        conn = p.new_connection()  # the supported way
        conn.execute("SELECT 1")
        conn.close()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert len(errors) == 1 and isinstance(errors[0], WrongThread)
    p.close()
