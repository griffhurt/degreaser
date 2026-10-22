import errno
from datetime import datetime
from pathlib import Path

import pytest

from degreaser.core.commit import TRASH_DIR, empty_trash, list_trash_batches
from degreaser.core.commit.trash import batch_name, is_cross_device_error
from degreaser.core.sources.errors import SourceError
from degreaser.core.sources.local import LocalSource
from tests.core.fake_source import build_tree


def test_list_and_empty(tmp_path: Path) -> None:
    src = LocalSource(tmp_path)
    assert list_trash_batches(src) == []
    build_tree(
        tmp_path / TRASH_DIR,
        {
            "20261020-090000-aaaaaaaa": {"a": {"x.txt": "123"}, "y.txt": "45"},
            "20261022-180411-bbbbbbbb": {"z.bin": b"\0" * 10},
        },
    )
    batches = list_trash_batches(src)
    assert [b.name for b in batches] == ["20261022-180411-bbbbbbbb", "20261020-090000-aaaaaaaa"]
    assert (batches[1].files, batches[1].bytes) == (2, 5)
    assert batches[0].created == datetime(2026, 10, 22, 18, 4, 11)

    assert empty_trash(src, ["20261020-090000-aaaaaaaa"]) == 1
    assert [b.name for b in list_trash_batches(src)] == ["20261022-180411-bbbbbbbb"]
    assert empty_trash(src) == 1
    assert list_trash_batches(src) == []
    with pytest.raises(ValueError):
        empty_trash(src, ["../escape"])


def test_batch_name_format() -> None:
    import time

    when = time.mktime((2026, 1, 2, 3, 4, 5, 0, 0, -1))
    assert batch_name("0123456789abcdef", when) == "20260102-030405-01234567"


def test_cross_device_detection() -> None:
    cause = OSError(errno.EXDEV, "Invalid cross-device link")
    err = SourceError("rename failed", "x")
    err.__cause__ = cause
    assert is_cross_device_error(err)
    windows = "[WinError 17] The system cannot move the file to a different disk drive"
    assert is_cross_device_error(SourceError(windows))
    assert not is_cross_device_error(SourceError("permission denied"))
