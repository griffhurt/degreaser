import json
from pathlib import Path

from degreaser.core import settings


def test_load_creates_defaults_with_device_id(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    s = settings.load(path)
    assert path.exists()
    assert len(s.device.id) == 32
    assert s.keymap["delete_folder"] == "Shift+D"
    assert settings.load(path).device.id == s.device.id


def test_unknown_keys_ignored_and_keymap_merged(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"keymap": {"keep": "J"}, "defaults": {"future": 1}, "x": 2}))
    s = settings.load(path)
    assert s.keymap["keep"] == "J"
    assert s.keymap["delete"] == "D"
    assert s.defaults.deletion_mode == "trash"
