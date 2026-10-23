import os
from pathlib import Path

from degreaser.core.duplicates import DuplicateFinder, DuplicateIndex, FindProgress, Pause
from degreaser.core.scan import CancelToken
from tests.core.fake_source import decide, index_all, make_project, touch
from tests.fixtures.preview.helpers import RemoteLikeSource

MB = 1024 * 1024
PHOTO = os.urandom(2 * MB)
VIDEO = os.urandom(3 * MB)
# Same size and same first/last 64 KB as VIDEO, different middle: only a full hash tells.
LOOKALIKE = VIDEO[:MB] + bytes(MB) + VIDEO[2 * MB :]


def tree() -> dict:
    return {
        "2014": {"photo.jpg": PHOTO, "clip.mov": VIDEO, "small.txt": "x" * 100},
        "2014 copy": {"photo.jpg": PHOTO, "clip.mov": VIDEO, "small.txt": "x" * 100},
        "other": {"not-a-copy.mov": LOOKALIKE, "unique.bin": os.urandom(MB + 1)},
        "node_modules": {"photo.jpg": PHOTO},
    }


def run(project, source, **kw):  # type: ignore[no-untyped-def]
    progress: list[FindProgress] = []
    finder = DuplicateFinder(
        project.new_connection, source, min_size=kw.pop("min_size", MB), on_progress=progress.append
    )
    return finder.run(kw.pop("cancel", None)), progress


def test_finds_groups_by_progressive_narrowing(tmp_path: Path) -> None:
    project, root = make_project(tmp_path, tree())
    source = RemoteLikeSource(root)
    index_all(project, source)
    summary, progress = run(project, source)
    assert summary.status == "completed"
    index = DuplicateIndex.load(project.conn)
    assert index.copies("2014/photo.jpg") == ["2014 copy/photo.jpg", "node_modules/photo.jpg"]
    assert index.copies("2014/clip.mov") == ["2014 copy/clip.mov"]
    assert index.copies("other/not-a-copy.mov") == []  # partial matched, full didn't
    assert index.copies("2014/small.txt") == []  # below the minimum size
    assert index.copies("other/unique.bin") == []  # unique size: never read
    assert summary.groups == 2 and summary.duplicate_files == 5
    assert progress[-1].done == progress[-1].total

    # Full reads only for same-size files whose edges match: 3 × 3 MB clips, 3 × 2 MB photos.
    # Everything else costs two 64 KB edges, and the unique size is never opened.
    assert source.bytes_read <= 15 * MB + 7 * 128 * 1024


def test_second_run_reads_nothing_and_changes_are_rehashed(tmp_path: Path) -> None:
    project, root = make_project(tmp_path, tree())
    source = RemoteLikeSource(root)
    index_all(project, source)
    run(project, source)
    before = source.bytes_read
    summary, _ = run(project, source)
    assert summary.hashed == 0 and source.bytes_read == before

    touch(root / "2014 copy" / "clip.mov", os.urandom(3 * MB))
    from degreaser.core.scan import Rescanner

    Rescanner(project.new_connection, source, retriage_changed=True).run()
    summary, _ = run(project, source)
    assert summary.hashed >= 1
    assert DuplicateIndex.load(project.conn).copies("2014/clip.mov") == []


def test_files_being_deleted_are_skipped(tmp_path: Path) -> None:
    project, root = make_project(tmp_path, tree())
    source = RemoteLikeSource(root)
    index_all(project, source)
    decide(project, "2014 copy", "delete", is_dir=True)
    decide(project, "node_modules/photo.jpg", "delete")
    run(project, source)
    index = DuplicateIndex.load(project.conn)
    assert index.copies("2014/photo.jpg") == []
    assert len(index) == 0


def test_cancel_keeps_what_was_hashed(tmp_path: Path) -> None:
    project, root = make_project(tmp_path, tree())
    source = RemoteLikeSource(root)
    index_all(project, source)
    cancel = CancelToken()
    pause = Pause()
    finder = DuplicateFinder(
        project.new_connection,
        source,
        min_size=MB,
        workers=1,
        pause=pause,
        on_progress=lambda p: cancel.cancel() if p.done >= 1 else None,
    )
    summary = finder.run(cancel)
    assert summary.status == "cancelled"
    hashed = project.conn.execute("SELECT count(*) FROM file_hashes").fetchone()[0]
    assert 1 <= hashed < 7
    assert run(project, source)[0].status == "completed"
