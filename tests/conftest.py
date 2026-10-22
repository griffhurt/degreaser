import os
from pathlib import Path

import pytest

# Run Qt headless unless a platform was chosen explicitly.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def isolated_app_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own app data dir, so nothing touches the real one."""
    data_dir = tmp_path / "appdata"
    monkeypatch.setenv("DEGREASER_DATA_DIR", str(data_dir))
    return data_dir
