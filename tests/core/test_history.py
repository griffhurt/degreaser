import pytest

from degreaser.core.db.connection import tx
from degreaser.core.triage import history
from tests.core.triage_helpers import Tree


def record(t: Tree, label: str, path: str, action: str | None) -> int | None:
    with tx(t.conn):
        rec = history.Recorder(t.conn, label, path)
        rec.set(
            "decisions",
            path,
            None
            if action is None
            else {
                "is_dir": 0,
                "action": action,
                "decided_at": 1.0,
                "seen_size": 1,
                "seen_mtime": 2.0,
            },
        )
        rec.set("nodes_flags", path, {"flag_new": 0, "flag_changed": 0})
        return rec.finish()


def test_undo_redo_roundtrip() -> None:
    t = Tree().add("a", flag_new=True)
    record(t, "Keep a", "a", "keep")
    record(t, "Delete a", "a", "delete")
    assert t.decision("a") == "delete"
    assert history.undo_label(t.conn) == "Delete a"

    with tx(t.conn):
        entry = history.undo(t.conn)
    assert entry is not None and entry.label == "Delete a" and entry.cursor_before == "a"
    assert t.decision("a") == "keep"
    with tx(t.conn):
        history.undo(t.conn)
    assert t.decision("a") is None
    assert t.flags("a") == (1, 0)
    assert history.undo_label(t.conn) is None
    assert history.redo_label(t.conn) == "Keep a"

    with tx(t.conn):
        history.redo(t.conn)
    assert t.decision("a") == "keep"
    assert t.flags("a") == (0, 0)


def test_new_action_discards_redo() -> None:
    t = Tree().add("a", "b")
    record(t, "Keep a", "a", "keep")
    with tx(t.conn):
        history.undo(t.conn)
    record(t, "Keep b", "b", "keep")
    assert history.redo_label(t.conn) is None
    assert t.conn.execute("SELECT count(*) FROM history").fetchone()[0] == 1


def test_no_op_actions_are_not_recorded() -> None:
    t = Tree().add("a")
    with tx(t.conn):
        rec = history.Recorder(t.conn, "Nothing", "a")
        rec.set("node_categories", "a", None)
        rec.set("nodes_flags", "missing", {"flag_new": 0, "flag_changed": 0})
        assert rec.finish() is None


def test_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(history, "MAX_ENTRIES", 3)
    t = Tree().add(*[f"f{i}" for i in range(6)])
    for i in range(6):
        record(t, f"Keep f{i}", f"f{i}", "keep")
    labels = [r[0] for r in t.conn.execute("SELECT label FROM history ORDER BY id")]
    assert labels == ["Keep f3", "Keep f4", "Keep f5"]
    orphans = t.conn.execute(
        "SELECT count(*) FROM history_changes WHERE history_id NOT IN (SELECT id FROM history)"
    ).fetchone()[0]
    assert orphans == 0


def test_change_row_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(history, "MAX_CHANGE_ROWS", 5)
    t = Tree().add(*[f"f{i}" for i in range(4)])
    for i in range(4):
        record(t, f"Keep f{i}", f"f{i}", "keep")  # 1 change each (flags unchanged)
    t.add("g", flag_new=True)
    record(t, "Keep g", "g", "keep")  # 2 changes
    total = t.conn.execute("SELECT sum(change_count) FROM history").fetchone()[0]
    assert total <= 5
    assert history.undo_label(t.conn) == "Keep g"


def test_clear() -> None:
    t = Tree().add("a")
    record(t, "Keep a", "a", "keep")
    with tx(t.conn):
        history.clear(t.conn)
    assert history.undo_label(t.conn) is None
    assert t.conn.execute("SELECT count(*) FROM history_changes").fetchone()[0] == 0
