from degreaser.ui.format import human_bytes, relative_time


def test_human_bytes() -> None:
    assert human_bytes(None) == "—"
    assert human_bytes(512) == "512 B"
    assert human_bytes(41.7 * 1024**3) == "41.7 GB"  # type: ignore[arg-type]


def test_relative_time() -> None:
    assert relative_time(None) == "never"
    assert relative_time(1000, now=1030) == "just now"
    assert relative_time(0, now=7200) == "2 hours ago"
    assert relative_time(0, now=86400) == "1 day ago"
