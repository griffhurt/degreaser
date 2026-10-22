"""Global per-device settings stored in settings.json (docs/data-model.md §5)."""

import json
import platform
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from degreaser.core import paths

DEFAULT_KEYMAP: dict[str, str] = {
    "keep": "K",
    "delete": "D",
    "keep_folder": "Shift+K",
    "delete_folder": "Shift+D",
    "tag": "T",
    "tag_folder": "Shift+T",
    "skip": "S",
    "open": "O",
    "expand_preview": "Space",
    "undo": "Ctrl+Z",
    "redo": "Ctrl+Shift+Z",
}


@dataclass
class Device:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = field(default_factory=platform.node)


@dataclass
class ProjectDefaults:
    jump_back: bool = True
    retriage_changed: bool = True
    deletion_mode: str = "trash"  # "trash" | "permanent"
    remove_empty_dirs: bool = True
    sync_enabled: bool = True


@dataclass
class Downloads:
    max_concurrent: int = 3
    cache_cap_bytes: int = 20 * 1024**3
    auto_load_bytes: int = 250 * 1024**2
    text_ceiling_bytes: int = 50 * 1024**2
    large_warning_bytes: int = 10 * 1024**3


@dataclass
class Video:
    autoplay_muted: bool = False


@dataclass
class Settings:
    version: int = 1
    device: Device = field(default_factory=Device)
    keymap: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_KEYMAP))
    defaults: ProjectDefaults = field(default_factory=ProjectDefaults)
    downloads: Downloads = field(default_factory=Downloads)
    video: Video = field(default_factory=Video)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        def pick(dc: type, raw: Any) -> Any:
            # Ignore unknown keys so older builds can read newer files.
            raw = raw if isinstance(raw, dict) else {}
            known = {k: v for k, v in raw.items() if k in dc.__dataclass_fields__}
            return dc(**known)

        return cls(
            version=data.get("version", 1),
            device=pick(Device, data.get("device")),
            keymap={**DEFAULT_KEYMAP, **data.get("keymap", {})},
            defaults=pick(ProjectDefaults, data.get("defaults")),
            downloads=pick(Downloads, data.get("downloads")),
            video=pick(Video, data.get("video")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load(path: Path | None = None) -> Settings:
    """Load settings, creating the file (with a fresh device id) if it doesn't exist."""
    path = path or paths.settings_path()
    if path.exists():
        settings = Settings.from_dict(json.loads(path.read_text(encoding="utf-8")))
    else:
        settings = Settings()
        save(settings, path)
    return settings


def save(settings: Settings, path: Path | None = None) -> None:
    path = path or paths.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings.to_dict(), indent=2), encoding="utf-8")
    tmp.replace(path)
