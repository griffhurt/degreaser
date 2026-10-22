import json
from datetime import UTC, datetime
from pathlib import Path

from degreaser.core.commit import CATALOG_PATH, build_catalog, read_catalog, write_catalog
from degreaser.core.sources.local import LocalSource

NOW = datetime(2026, 10, 22, 18, 4, 11, tzinfo=UTC)


def test_build_fresh_catalog() -> None:
    cat = build_catalog(
        None,
        {"b.mp4": ("Videos", "HS"), "a.doc": ("School",)},
        {"vids": ("Videos",)},
        project_name="P",
        is_known=lambda p: True,
        now=NOW,
    )
    assert cat == {
        "format": "degreaser-catalog",
        "version": 1,
        "project": "P",
        "generated_at": "2026-10-22T18:04:11Z",
        "folders": {"vids": ["Videos"]},
        "files": {"a.doc": ["School"], "b.mp4": ["Videos", "HS"]},
    }
    assert list(cat["files"]) == ["a.doc", "b.mp4"]


def test_merge_is_cumulative_but_respects_known_and_removed() -> None:
    existing = {
        "format": "degreaser-catalog",
        "files": {
            "unknown/old.txt": ["Old"],  # unknown to this project: kept
            "known/untagged.txt": ["Gone"],  # known but no longer categorised: dropped
            "deleted/dir/f.txt": ["X"],  # under a path this commit deleted: dropped
            "known/retagged.txt": ["Before"],
        },
        "folders": {"deleted/dir": ["X"], "unknown": ["Old"]},
    }
    known = {"known/untagged.txt", "known/retagged.txt"}
    cat = build_catalog(
        existing,
        {"known/retagged.txt": ("After",)},
        {},
        project_name="P",
        is_known=known.__contains__,
        removed=["deleted"],
        now=NOW,
    )
    assert cat["files"] == {"known/retagged.txt": ["After"], "unknown/old.txt": ["Old"]}
    assert cat["folders"] == {"unknown": ["Old"]}


def test_write_and_read_roundtrip(tmp_path: Path) -> None:
    src = LocalSource(tmp_path)
    assert read_catalog(src) is None
    cat = build_catalog(None, {"日本.txt": ("Ünï",)}, {}, project_name="P", is_known=lambda p: True)
    write_catalog(src, cat)
    raw = (tmp_path / CATALOG_PATH).read_text(encoding="utf-8")
    assert "日本.txt" in raw  # not \u-escaped
    assert read_catalog(src) == json.loads(raw)


def test_unreadable_catalog_is_ignored(tmp_path: Path) -> None:
    (tmp_path / ".degreaser").mkdir()
    (tmp_path / CATALOG_PATH).write_text("{not json")
    assert read_catalog(LocalSource(tmp_path)) is None
    (tmp_path / CATALOG_PATH).write_text('{"format": "something-else"}')
    assert read_catalog(LocalSource(tmp_path)) is None
